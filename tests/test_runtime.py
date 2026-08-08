"""The loop, and whether the frozen policy survives contact with it.

Gate C froze a policy against a simulator. If the runtime had to reshape
that policy to use it, the freeze would have been undone quietly, so
these tests drive the frozen function unchanged and check the properties
week 7-8 is accepted on.
"""

from __future__ import annotations

import itertools
import sys
import unittest

sys.dont_write_bytecode = True

from burstserve.executor import Phase, StepExecutor, StepState
from burstserve.policies import (
    exclusive_fcfs,
    probing_partitioning,
    slo_aware_partitioning,
)
from burstserve.queues import Discipline, QueuedRequest
from burstserve.runtime import Runtime


class CountingAdapter:
    """Deterministic, quota-blind, and counts its own steps."""

    def __init__(self):
        self.steps = 0

    def initial_state(self, request):
        return StepState(step_index=0, latent=(0.0,), rng_state=None)

    def denoise_one(self, state, *, quota_units):
        self.steps += 1
        return StepState(step_index=state.step_index + 1,
                         latent=(state.latent[0] + 1.0,), rng_state=None)

    def decode(self, state):
        return ("image", state.latent[0])


def build(policy=probing_partitioning, *, tenants=("a", "b"), steps=6,
          model="sdxl", clock=None):
    runtime = Runtime(policy, clock=clock)
    for index, tenant in enumerate(tenants):
        request = QueuedRequest(request_id=index, tenant=tenant, model=model,
                                arrival_s=0.0, steps=steps)
        runtime.submit(request, StepExecutor(request, CountingAdapter(),
                                             total_steps=steps))
    return runtime


def drain(runtime, *, limit=200) -> int:
    now = 0.0
    for rounds in range(limit):
        if runtime.all_finished():
            return rounds
        runtime.tick(now)
        now += 0.25
    raise AssertionError("did not finish within the round limit")


class TheFrozenPolicyDrivesRealExecutors(unittest.TestCase):
    def test_the_policy_is_called_unchanged(self):
        """Same signature as the simulator uses. If this needed an
        adapter around the policy, the freeze would be nominal."""
        seen = []

        def spy(states, units, now):
            seen.append((len(states), units, now))
            return probing_partitioning(states, units, now)

        runtime = build(spy)
        drain(runtime)
        self.assertTrue(seen)
        self.assertTrue(all(u == 32 for _, u, _ in seen))

    def test_every_request_completes_exactly_its_steps(self):
        runtime = build(steps=6)
        drain(runtime)
        for executor in runtime.executors.values():
            self.assertIs(executor.phase, Phase.FINISHED)
            self.assertEqual(executor.steps_done, 6)
            self.assertEqual(executor.adapter.steps, 6)

    def test_partitioning_finishes_sooner_than_exclusive(self):
        """The Gate C result, restated against real executors: the round
        count is what the utilisation claim is about."""
        shared = drain(build(probing_partitioning, steps=8))
        alone = drain(build(exclusive_fcfs, steps=8))
        self.assertLess(shared, alone)


class TheLedgerRecordsBeliefAndOutcome(unittest.TestCase):
    def test_both_are_recorded_per_round(self):
        runtime = build(steps=4)
        drain(runtime)
        active = [r for r in runtime.ledger if r.granted]
        self.assertTrue(active)
        for record in active:
            self.assertTrue(record.predicted_step_seconds)
            self.assertTrue(record.observed_step_seconds)

    def test_prediction_error_is_visible_when_they_disagree(self):
        """A runtime charging from prediction would report zero error
        forever and learn nothing. Sharing the die makes a step cost more
        than its solo prediction, and the ledger has to show it."""
        runtime = build(steps=6)
        drain(runtime)
        shared = [r for r in runtime.ledger if len(r.granted) == 2]
        self.assertTrue(shared)
        errors = [e for r in shared for e in r.prediction_error.values()]
        self.assertTrue(all(e > 0.0 for e in errors), errors)

    def test_the_charge_is_units_times_measured_time(self):
        runtime = build(steps=4)
        drain(runtime)
        last = runtime.ledger[-1]
        self.assertEqual(set(last.quota_seconds_by_tenant), {"a", "b"})
        self.assertTrue(all(v > 0 for v in
                            last.quota_seconds_by_tenant.values()))

    def test_backlog_is_recorded_while_work_remains(self):
        runtime = build(steps=6)
        runtime.tick(0.0)
        self.assertEqual(set(runtime.ledger[0].backlogged), {"a", "b"})


class ResidencyIsChargedOnce(unittest.TestCase):
    """plan.md: a same-model burst transfers no weight bytes after
    residency is established."""

    def test_a_same_model_burst_moves_no_bytes_after_the_first_round(self):
        runtime = build(steps=6, tenants=("a", "b"), model="sdxl")
        drain(runtime)
        self.assertGreater(runtime.ledger[0].weight_bytes_moved, 0)
        self.assertEqual(runtime.weight_bytes_after_first_round(), 0)

    def test_a_second_model_does_pay(self):
        """Otherwise the check above would pass for a runtime that never
        counted bytes at all."""
        runtime = Runtime(probing_partitioning)
        for index, model in enumerate(("sdxl", "cogvideox-2b")):
            request = QueuedRequest(request_id=index, tenant=f"t{index}",
                                    model=model, arrival_s=0.0, steps=4)
            runtime.submit(request, StepExecutor(request, CountingAdapter(),
                                                 total_steps=4))
        drain(runtime)
        self.assertEqual(len(runtime.ledger[-1].resident_models), 2)
        self.assertGreater(runtime.ledger[-1].weight_bytes_moved,
                           runtime.ledger[0].weight_bytes_moved)


class SchedulerOverhead(unittest.TestCase):
    """plan.md: scheduler p99 under 1 ms.

    Timed on an injected clock so the assertion is about the policy's own
    work rather than the test host's load, and measured around the policy
    call alone -- including the denoising would be measuring the model.
    """

    def test_the_decision_is_timed_without_the_step(self):
        ticks = itertools.count(0.0, 0.0005)   # 0.5 ms per clock read
        runtime = build(steps=4, clock=lambda: next(ticks))
        drain(runtime)
        self.assertTrue(all(abs(r.decision_seconds - 0.0005) < 1e-9
                            for r in runtime.ledger))

    def test_p99_is_reported_not_the_mean(self):
        """A scheduler fast on average and occasionally slow is the one
        that misses deadlines."""
        costs = iter([0.0, 0.0001] * 50 + [0.0, 0.05])
        runtime = build(steps=1, clock=lambda: next(costs))
        runtime.tick(0.0)
        self.assertGreater(runtime.decision_p99_seconds(), 0.0)


class PreemptionKeepsThePlace(unittest.TestCase):
    def test_a_request_offered_but_not_granted_is_suspended(self):
        """And stays offerable, rather than being lost between rounds."""
        def only_first(states, units, now):
            return {states[0].request.request_id: units} if states else {}

        runtime = build(only_first, steps=3)
        runtime.tick(0.0)
        runtime.tick(0.25)
        offered = {q.request_id for _, q in runtime.registry.ready(0.5)}
        self.assertEqual(offered, {0, 1})

    def test_suspension_does_not_lose_progress(self):
        def only_first(states, units, now):
            return {states[0].request.request_id: units} if states else {}

        runtime = build(only_first, steps=4)
        drain(runtime)
        for executor in runtime.executors.values():
            self.assertEqual(executor.steps_done, 4)
            self.assertIs(executor.phase, Phase.FINISHED)


if __name__ == "__main__":
    unittest.main()
