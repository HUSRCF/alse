"""Pipelining a fast tenant inside a slow tenant's step.

One step per request per round is a barrier. The grants run on disjoint
masks and their streams overlap, so a round costs the maximum of its
steps rather than their sum, and advancing every request by exactly one
step hands the whole round to its slowest member. Measured on the card
on 2026-08-25: urgent steps of 0.113 s beside video steps of 0.521 s
gave 1.72 steps per 0.81 s round and 2.14 steps/s, against 1.00 steps
per 0.26 s round and 3.84 steps/s for the same policies when they
declined to pair -- a 44% scheduler cost on a cell whose measured
hardware co-run penalty was 3.4%.

These tests pin the arithmetic of the budget and, more importantly, that
turning it off reproduces the old behaviour exactly. Every number this
project has published was produced at one step per round, so the default
has to be indistinguishable from what it was.
"""

from __future__ import annotations

import sys
import unittest

sys.dont_write_bytecode = True

from burstserve.executor import StepExecutor, StepState
from burstserve.policies import static_even
from burstserve.queues import QueuedRequest
from burstserve.runtime import Runtime
from burstserve.trace_sim import QuotaCostModel


class CostedAdapter:
    """Counts steps and reports a fixed per-step cost, like a card would.

    ``last_step_seconds`` is what the runtime charges from, so a fake
    that omits it would send every charge through the cost model and
    make these tests measure the model instead of the loop.
    """

    def __init__(self, seconds: float):
        self.seconds = seconds
        self.steps = 0
        self.last_step_seconds = None
        self.last_step_units = None

    def initial_state(self, request):
        return StepState(step_index=0, latent=(0.0,), rng_state=None)

    def denoise_one(self, state, *, quota_units):
        self.steps += 1
        self.last_step_seconds = self.seconds
        self.last_step_units = quota_units
        return StepState(step_index=state.step_index + 1,
                         latent=(state.latent[0] + 1.0,), rng_state=None)

    def decode(self, state):
        return ("image", state.latent[0])


def build(*, max_steps_per_round, fast_s=0.113, slow_s=0.521, steps=40):
    runtime = Runtime(static_even, max_steps_per_round=max_steps_per_round)
    adapters = {}
    for index, (tenant, cost) in enumerate((("urgent", fast_s),
                                            ("video", slow_s))):
        request = QueuedRequest(request_id=index, tenant=tenant,
                                model="sdxl" if tenant == "urgent"
                                else "cogvideox-2b",
                                arrival_s=0.0, steps=steps)
        adapters[tenant] = CostedAdapter(cost)
        runtime.submit(request, StepExecutor(request, adapters[tenant],
                                             total_steps=steps))
    return runtime, adapters


class StepBudgetRuleTest(unittest.TestCase):
    """The rule, on synthetic beliefs.

    Driven through ``_step_budget`` directly rather than through a round,
    because the budget is a function of what the scheduler *believes* a
    step costs and not of what the adapter goes on to measure. A test
    that set the adapter's cost and asserted the budget would be
    asserting the cost model, which is a different claim.
    """

    def budget(self, predicted, cap=8):
        runtime = Runtime(static_even, max_steps_per_round=cap)
        active = [(rid, 16) for rid in predicted]
        return runtime._step_budget(active, predicted)

    def test_fast_side_fills_the_slow_side(self) -> None:
        self.assertEqual(self.budget({0: 0.113, 1: 0.521}), {0: 4, 1: 1})

    def test_floor_not_round(self) -> None:
        """1.9 floors to 1. Rounding to 2 would run the fast tenant past
        the end of its peer's step, which makes the round longer than its
        slowest member -- the one thing the barrier was honest about."""
        self.assertEqual(self.budget({0: 0.100, 1: 0.190}), {0: 1, 1: 1})

    def test_capped(self) -> None:
        self.assertEqual(self.budget({0: 0.001, 1: 0.500}, cap=3), {0: 3, 1: 1})

    def test_equal_costs_batch_nothing(self) -> None:
        self.assertEqual(self.budget({0: 0.25, 1: 0.25}), {0: 1, 1: 1})

    def test_missing_belief_batches_nothing(self) -> None:
        """An unpredicted step is not a licence to invent a ratio."""
        self.assertEqual(self.budget({0: None, 1: 0.5}), {0: 1, 1: 1})
        self.assertEqual(self.budget({0: 0.0, 1: 0.5}), {0: 1, 1: 1})

    def test_lone_request_is_never_batched(self) -> None:
        """Nothing to overlap with, and batching would only delay the
        next decision."""
        self.assertEqual(self.budget({0: 0.01}), {0: 1})

    def test_cap_of_one_is_the_barrier(self) -> None:
        self.assertEqual(self.budget({0: 0.113, 1: 0.521}, cap=1), {0: 1, 1: 1})

    def test_zero_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Runtime(static_even, max_steps_per_round=0)


class StepBudgetInRoundTest(unittest.TestCase):
    """The same rule reached through a real round, on the real models."""

    def expected_ratio(self, units=16) -> int:
        video = QuotaCostModel.for_model("cogvideox-2b").step_seconds(units)
        urgent = QuotaCostModel.for_model("sdxl").step_seconds(units)
        return int(video / urgent)

    def test_default_is_one_step_per_request(self) -> None:
        """The published behaviour, and it has to stay reachable."""
        runtime, adapters = build(max_steps_per_round=1)
        record = runtime.tick(0.0)
        self.assertEqual(record.steps_run, {0: 1, 1: 1})
        self.assertEqual(adapters["urgent"].steps, 1)
        self.assertEqual(adapters["video"].steps, 1)

    def test_fast_tenant_is_batched_to_the_slow_one(self) -> None:
        runtime, adapters = build(max_steps_per_round=8)
        record = runtime.tick(0.0)
        want = self.expected_ratio()
        self.assertGreater(want, 1, "the models have to differ for this "
                                    "test to be testing anything")
        self.assertEqual(adapters["video"].steps, 1)
        self.assertEqual(adapters["urgent"].steps, want)
        self.assertEqual(record.steps_run, {0: want, 1: 1})

class ChargingSurvivesBatchingTest(unittest.TestCase):

    def test_every_step_is_charged_from_measurement(self) -> None:
        """Four steps in a round are four charges, not one.

        A batch that charged once per round would make the fast tenant
        look free, which is exactly the direction that would flatter
        this change.
        """
        runtime, adapters = build(max_steps_per_round=8)
        record = runtime.tick(0.0)
        self.assertEqual(record.notes["charged_from"], ["measurement"])
        ran = record.steps_run
        self.assertGreater(ran[0], 1)
        # units x seconds, once per step actually run, from the adapter's
        # own measurement rather than from the cost model.
        self.assertAlmostEqual(runtime.die_seconds_by_tenant["urgent"],
                               ran[0] * 16 * 0.113, places=6)
        self.assertAlmostEqual(runtime.die_seconds_by_tenant["video"],
                               ran[1] * 16 * 0.521, places=6)
        self.assertAlmostEqual(runtime.service_seconds_by_tenant["urgent"],
                               ran[0] * 0.113, places=6)

    def test_a_request_that_finishes_mid_batch_stops(self) -> None:
        """The budget is an allowance, not an obligation."""
        runtime, adapters = build(max_steps_per_round=8, steps=2)
        runtime.tick(0.0)
        self.assertEqual(adapters["urgent"].steps, 2)
        self.assertTrue(runtime.retired)


if __name__ == "__main__":
    unittest.main()
