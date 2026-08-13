"""The loop, and whether the frozen policy survives contact with it.

Gate C froze a policy against a simulator. If the runtime had to reshape
that policy to use it, the freeze would have been undone quietly, so
these tests drive the frozen function unchanged and check the properties
week 7-8 is accepted on.
"""

from __future__ import annotations

import itertools
import statistics
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


class FinishedRequestsAreReleased(unittest.TestCase):
    """An hour of serving found this and nothing shorter would have.

    Each request holds an executor, each executor an adapter, and on the
    card an adapter holds conditioning tensors. Keeping them after
    completion leaked 1.284 MB per request -- 478 MB over 372 requests --
    and it tracked the completion count rather than the clock, which is
    what distinguishes a leak from an allocator settling.
    """

    def test_a_finished_request_leaves_the_executor_map(self):
        runtime = build(steps=3, tenants=("a",))
        drain(runtime)
        self.assertEqual(runtime.executors, {})
        self.assertEqual(runtime.completed, 1)

    def test_its_numbers_are_kept(self):
        """Releasing the object must not lose the accounting; the ledger
        is what every acceptance clause reads."""
        runtime = build(steps=4, tenants=("a", "b"))
        drain(runtime)
        self.assertEqual(len(runtime.retired), 2)
        for summary in runtime.retired.values():
            self.assertEqual(summary["steps_done"], 4)
            self.assertEqual(summary["phase"], "FINISHED")
        self.assertTrue(runtime.quota_seconds_by_tenant)

    def test_the_adapter_is_dropped(self):
        """Explicitly, rather than by hoping the executor goes out of
        scope -- the caller may still hold a reference."""
        runtime = build(steps=3, tenants=("a",))
        executor = runtime.executors[0]
        drain(runtime)
        self.assertIsNone(executor.adapter)
        self.assertIsNone(executor.state)

    def test_all_finished_still_means_finished(self):
        """Retirement empties the map, so "no executors" must not be
        confused with "never started"."""
        runtime = build(steps=3, tenants=("a",))
        self.assertFalse(runtime.all_finished())
        drain(runtime)
        self.assertTrue(runtime.all_finished())
        self.assertEqual(runtime.completed, 1)


class GrantedMasksArePartitions(unittest.TestCase):
    """Week 9-10: the measured SM set must match the manifest exactly.

    Laying the masks out together rather than per request is what makes
    that true by construction. Two 16-unit grants assigned independently
    would both take the low half, share every unit, and still be recorded
    as a partition -- the ledger would show two tenants on half a die
    each while they contended over the same sixteen.
    """

    def _pool(self):
        from burstserve.masked_streams import MaskedStreamPool
        return MaskedStreamPool(lambda mask: (f"s{hex(mask)}", mask))

    def test_a_pair_is_given_disjoint_masks(self):
        pool = self._pool()
        runtime = Runtime(probing_partitioning, stream_pool=pool)
        for index in range(2):
            request = QueuedRequest(request_id=index, tenant=f"t{index}",
                                    model="sdxl", arrival_s=0.0, steps=4)
            runtime.submit(request, StepExecutor(request, CountingAdapter(),
                                                 total_steps=4))
        drain(runtime)
        paired = [a for a in runtime.mask_attestations if len(a["masks"]) == 2]
        self.assertTrue(paired)
        for attestation in paired:
            with self.subTest(round=attestation["round"]):
                self.assertTrue(attestation["disjoint"])
                left, right = attestation["masks"].values()
                self.assertEqual(int(left, 16) & int(right, 16), 0)

    def test_the_masks_cover_exactly_the_granted_widths(self):
        pool = self._pool()
        runtime = Runtime(probing_partitioning, stream_pool=pool)
        for index in range(2):
            request = QueuedRequest(request_id=index, tenant=f"t{index}",
                                    model="sdxl", arrival_s=0.0, steps=4)
            runtime.submit(request, StepExecutor(request, CountingAdapter(),
                                                 total_steps=4))
        drain(runtime)
        for attestation in runtime.mask_attestations:
            for rid, mask in attestation["masks"].items():
                with self.subTest(round=attestation["round"], rid=rid):
                    self.assertEqual(bin(int(mask, 16)).count("1"),
                                     attestation["units"][rid])

    def test_overlapping_masks_would_raise(self):
        """The check has to be able to fire.

        A pool that hands out the same mask regardless of offset is what
        an independent per-request assignment amounts to.
        """
        from burstserve.masked_streams import MaskedStreamPool

        class ColludingPool(MaskedStreamPool):
            def for_quota(self, units, *, offset=0):
                return super().for_quota(units, offset=0)

        pool = ColludingPool(lambda mask: (f"s{hex(mask)}", mask))
        runtime = Runtime(probing_partitioning, stream_pool=pool)
        for index in range(2):
            request = QueuedRequest(request_id=index, tenant=f"t{index}",
                                    model="sdxl", arrival_s=0.0, steps=4)
            runtime.submit(request, StepExecutor(request, CountingAdapter(),
                                                 total_steps=4))
        with self.assertRaises(RuntimeError):
            drain(runtime)

    def test_streams_are_reused_across_rounds(self):
        """Creating one per round would be the destroy-per-quota pattern
        that hung a measurement process for 2.5 hours."""
        pool = self._pool()
        runtime = Runtime(probing_partitioning, stream_pool=pool)
        for index in range(2):
            request = QueuedRequest(request_id=index, tenant=f"t{index}",
                                    model="sdxl", arrival_s=0.0, steps=8)
            runtime.submit(request, StepExecutor(request, CountingAdapter(),
                                                 total_steps=8))
        drain(runtime)
        self.assertGreater(len(runtime.ledger), pool.creations)
        self.assertLessEqual(pool.creations, 4)


class VideoStallIsBounded(unittest.TestCase):
    """plan.md: stall within the configured budget plus one unpreemptable
    step.

    The allowance is exactly one step because a decision to stop only
    takes effect at a step boundary. Allowing more would excuse the
    scheduler for something it does control.
    """

    def test_a_request_never_preempted_has_no_stalls(self):
        """Not a stall of zero -- there was no gap to measure."""
        runtime = build(steps=3, tenants=("a",))
        drain(runtime)
        stalls = runtime.stalls_by_request()
        self.assertEqual(stalls.get(0, []), [0.25, 0.25])

    def test_the_worst_stall_is_reported_not_the_mean(self):
        """A stall budget is about the worst frame, not the average one."""
        def alternate(states, units, now):
            if not states:
                return {}
            index = int(now / 0.25) % len(states)
            return {states[index].request.request_id: units}

        runtime = build(alternate, steps=4, tenants=("a", "b"))
        drain(runtime)
        worst = runtime.worst_stall_seconds()
        every = [s for gaps in runtime.stalls_by_request().values()
                 for s in gaps]
        self.assertEqual(worst, max(every))
        self.assertGreater(worst, statistics.mean(every) * 0.5)

    def test_the_bound_admits_one_step_of_slack(self):
        runtime = build(steps=3, tenants=("a", "b"))
        drain(runtime)
        worst = runtime.worst_stall_seconds()
        self.assertTrue(runtime.stall_within_budget(worst, 0.0))
        self.assertFalse(runtime.stall_within_budget(worst - 0.1, 0.0))
        self.assertTrue(runtime.stall_within_budget(worst - 0.1, 0.1))

    def test_a_request_abandoned_mid_flight_breaks_the_bound(self):
        """The check has to be able to fail, and the failure has a shape.

        A stall is a gap between services of a request that has already
        started -- the viewer sees a frozen frame, not a late one. A
        request that has not begun is not stalling, it is queued, and
        that is what the fairness bound and the lag bound are for.

        So the failure this must catch is a request served once and then
        abandoned while another runs to completion, which is exactly what
        preemption without a bound looks like.
        """
        def start_both_then_abandon_one(states, units, now):
            if not states:
                return {}
            by_id = {s.request.request_id: s for s in states}
            # Round 0 starts request 1 so it has a first service to
            # measure from; every round after that belongs to request 0.
            if now < 0.125:
                return {1: units} if 1 in by_id else {}
            if 0 in by_id:
                return {0: units}
            return {min(by_id): units}

        runtime = build(start_both_then_abandon_one, steps=6,
                        tenants=("a", "b"))
        drain(runtime)
        # Request 1 is served at round 0 and not again until request 0
        # has finished all six of its steps.
        self.assertGreater(runtime.worst_stall_seconds(), 1.0)
        self.assertFalse(runtime.stall_within_budget(0.25, 0.25))


if __name__ == "__main__":
    unittest.main()


class MeasurementsAreCheckedAgainstTheirQuota(unittest.TestCase):
    """A step measured at one width must not be charged as another.

    The adapters report their step time one step late, and the reading
    only lands once the previous step's events have retired -- so on the
    card a request re-granted 16 units after running at 32 reported 107
    ms, the 32-unit cost, for its whole 16-unit run. The adapters drain on
    a width change now. This is the ledger's own check, because a charge
    that is quietly the wrong width is the one thing the dual ledger
    exists to make impossible.
    """

    class StaleAdapter(CountingAdapter):
        """Reports a time, and a width that never matches the grant."""

        def __init__(self, reported_units):
            super().__init__()
            self.last_step_seconds = 0.999
            self.last_step_units = reported_units

    def _runtime_with(self, adapter_units):
        runtime = Runtime(probing_partitioning)
        for index in range(2):
            request = QueuedRequest(request_id=index, tenant=f"t{index}",
                                    model="sdxl", arrival_s=0.0, steps=4)
            runtime.submit(request, StepExecutor(
                request, self.StaleAdapter(adapter_units), total_steps=4))
        return runtime

    def test_a_mismatched_width_is_not_charged_as_a_measurement(self):
        runtime = self._runtime_with(adapter_units=31)   # never granted
        record = runtime.tick(0.0)
        self.assertNotIn("measurement", record.notes["charged_from"])
        self.assertTrue(record.notes["stale_quota_measurements"],
                        "the ledger says which requests it refused")

    def test_the_refusal_is_visible_rather_than_silent(self):
        """Falling back to the model is what the ledger must never hide."""
        runtime = self._runtime_with(adapter_units=31)
        record = runtime.tick(0.0)
        self.assertEqual(sorted(record.notes["stale_quota_measurements"]),
                         sorted(record.granted))

    def test_a_matching_width_is_charged_from_measurement(self):
        runtime = self._runtime_with(adapter_units=16)
        record = runtime.tick(0.0)
        if 16 in record.granted.values():
            self.assertIn("measurement", record.notes["charged_from"])
            self.assertEqual(record.notes["stale_quota_measurements"], [])

    def test_an_adapter_without_a_width_is_unaffected(self):
        """CountingAdapter reports no width; it must not be penalised."""
        runtime = build(steps=4)
        record = runtime.tick(0.0)
        self.assertEqual(record.notes["stale_quota_measurements"], [])


class ConservativeSerialFallback(unittest.TestCase):
    """plan.md: serial when the profile is missing or drift exceeds 15%.

    The runtime enforces it, not the policy: the policy is frozen, and
    this is a safety envelope that can only narrow a grant to one request
    on the whole die.

    The refusal expires, and that is the clause being met rather than
    softened. SDXL against CogVideoX-2b runs at 6.29x predicted for two
    rounds -- 529% drift, five times any threshold -- and then at 1.01x
    for the rest of the process, where it beats rotation by 41.7%, in
    four processes out of four. A permanent refusal there scores 0.9982
    against a whole die's 1.0000.
    """

    class DriftingAdapter(CountingAdapter):
        """Drifts past the runtime's envelope but under the probe's.

        The two thresholds are deliberately different and the gap is
        where this envelope does its work: the policy's probe fires at
        1.608x the solo prediction, the runtime's at 1.15x the *paired*
        expectation. An adapter drifting past both would be caught by the
        probe first and would test nothing here. At 16+16 the paired
        expectation is 0.1575 x 1.2367 = 0.1948 s, so 0.23 s is 18%
        drift -- over the envelope, under the probe's 0.2533 s.
        """

        def __init__(self, seconds=0.23):
            super().__init__()
            self.last_step_seconds = seconds

    def _runtime(self, adapter_factory, **kwargs):
        runtime = Runtime(probing_partitioning, **kwargs)
        for index in range(2):
            request = QueuedRequest(request_id=index, tenant=f"t{index}",
                                    model="sdxl", arrival_s=0.0, steps=40)
            runtime.submit(request, StepExecutor(request, adapter_factory(),
                                                 total_steps=40))
        return runtime

    def test_drift_past_the_tolerance_holds_the_pairing(self):
        runtime = self._runtime(self.DriftingAdapter)
        first = runtime.tick(0.0)
        self.assertEqual(len(first.granted), 2, "the pairing formed once")
        self.assertIsNotNone(first.notes["drift_hold"])
        second = runtime.tick(0.25)
        self.assertEqual(len(second.granted), 1, "serial after the drift")
        self.assertIn("drift", second.notes["serial_fallback"])

    def test_the_whole_die_goes_to_one_request(self):
        """Conservative means serial, not smaller."""
        runtime = self._runtime(self.DriftingAdapter)
        runtime.tick(0.0)
        second = runtime.tick(0.25)
        self.assertEqual(list(second.granted.values()),
                         [runtime.maskable_units])

    def test_the_hold_expires_so_the_pairing_is_retried(self):
        """A pairing that drifts is not a pairing that will keep drifting."""
        runtime = self._runtime(self.DriftingAdapter,
                                fallback_backoff_s=1.0)
        runtime.tick(0.0)
        self.assertEqual(len(runtime.tick(0.5).granted), 1, "held")
        self.assertEqual(len(runtime.tick(1.5).granted), 2, "retried")

    def test_the_backoff_doubles_while_the_drift_persists(self):
        runtime = self._runtime(self.DriftingAdapter,
                                fallback_backoff_s=1.0,
                                max_fallback_backoff_s=8.0)
        seen, now = [], 0.0
        for _ in range(4):
            runtime.tick(now)
            key = next(iter(runtime._fallbacks))
            seen.append(runtime._fallbacks[key]["backoff"])
            now = runtime._fallbacks[key]["until"] + 0.01
        self.assertEqual(seen, [1.0, 2.0, 4.0, 8.0])

    def test_a_faithful_pairing_is_never_held(self):
        """The envelope must not fire on a runtime that is behaving."""
        runtime = build(steps=40)
        record = runtime.tick(0.0)
        self.assertIsNone(record.notes["drift_hold"])
        self.assertIsNone(record.notes["serial_fallback"])
        self.assertEqual(len(runtime.tick(0.25).granted), 2)

    def test_a_reason_is_always_recorded_with_a_refusal(self):
        """'Explicitly record the reason' is part of the clause."""
        runtime = self._runtime(self.DriftingAdapter)
        runtime.tick(0.0)
        second = runtime.tick(0.25)
        self.assertTrue(second.notes["serial_fallback"])
        self.assertIn("%", second.notes["serial_fallback"])


class PredictorErrorIsInjectableAndOnlyReachesTheBelief(unittest.TestCase):
    """plan.md week 15: +/-10% causes no safety failure, +/-20% degrades.

    A scheduler can only be tested against a wrong belief if the belief
    can be made wrong on purpose, so the error is injected where the
    policy reads and nowhere else. Perturbing the charge as well would
    test a runtime whose measurements are also wrong -- a different and
    much weaker claim, since the dual ledger exists exactly so that a
    wrong prediction meets a right measurement.
    """

    def test_the_policy_sees_the_skewed_prediction(self):
        clean = build(steps=4)
        skewed = Runtime(probing_partitioning, predictor_error=0.2)
        for index in range(2):
            request = QueuedRequest(request_id=index, tenant=f"t{index}",
                                    model="sdxl", arrival_s=0.0, steps=4)
            skewed.submit(request, StepExecutor(request, CountingAdapter(),
                                                total_steps=4))
        a = clean.tick(0.0).predicted_step_seconds
        b = skewed.tick(0.0).predicted_step_seconds
        for rid in a:
            self.assertAlmostEqual(b[rid] / a[rid], 1.2, places=6)

    def test_a_negative_error_skews_the_other_way(self):
        runtime = Runtime(probing_partitioning, predictor_error=-0.1)
        for index in range(2):
            request = QueuedRequest(request_id=index, tenant=f"t{index}",
                                    model="sdxl", arrival_s=0.0, steps=4)
            runtime.submit(request, StepExecutor(request, CountingAdapter(),
                                                 total_steps=4))
        clean = build(steps=4)
        a = clean.tick(0.0).predicted_step_seconds
        b = runtime.tick(0.0).predicted_step_seconds
        for rid in a:
            self.assertAlmostEqual(b[rid] / a[rid], 0.9, places=6)

    def test_the_charge_is_not_skewed(self):
        """The measurement side must stay honest, or the test is vacuous."""
        class Measuring(CountingAdapter):
            def __init__(self):
                super().__init__()
                self.last_step_seconds = 0.2
                self.last_step_units = None

        for error in (0.0, 0.2, -0.2):
            with self.subTest(error=error):
                runtime = Runtime(probing_partitioning,
                                  predictor_error=error)
                for index in range(2):
                    request = QueuedRequest(request_id=index,
                                            tenant=f"t{index}", model="sdxl",
                                            arrival_s=0.0, steps=4)
                    runtime.submit(request, StepExecutor(
                        request, Measuring(), total_steps=4))
                record = runtime.tick(0.0)
                for rid in record.observed_step_seconds:
                    self.assertAlmostEqual(
                        record.observed_step_seconds[rid], 0.2, places=6)

    def test_the_grant_never_exceeds_the_die_under_error(self):
        """The safety invariant, asserted across the tested range."""
        for error in (-0.2, -0.1, 0.0, 0.1, 0.2):
            with self.subTest(error=error):
                runtime = Runtime(probing_partitioning,
                                  predictor_error=error)
                for index in range(3):
                    request = QueuedRequest(request_id=index,
                                            tenant=f"t{index % 2}",
                                            model="sdxl", arrival_s=0.0,
                                            steps=6)
                    runtime.submit(request, StepExecutor(
                        request, CountingAdapter(), total_steps=6))
                for step in range(40):
                    if runtime.all_finished():
                        break
                    record = runtime.tick(step * 0.25)
                    self.assertLessEqual(sum(record.granted.values()),
                                         runtime.maskable_units)


class AblationsChangeOnlyWhatTheyName(unittest.TestCase):
    """Week 15: every performance claim needs an ablation or a mechanism.

    Both switches exist so a claim can be shown to depend on the thing it
    is claimed to depend on, and both are asserted to touch nothing else.
    """

    def test_an_unknown_currency_is_refused(self):
        with self.assertRaises(ValueError):
            Runtime(probing_partitioning, charge_currency="dollars")

    def test_wall_seconds_ignores_the_width(self):
        """Which is the whole of what it ablates."""
        class Fixed(CountingAdapter):
            def __init__(self):
                super().__init__()
                self.last_step_seconds = 0.1

        charged = {}
        for currency in ("quota-seconds", "wall-seconds", "step-count"):
            runtime = Runtime(exclusive_fcfs, charge_currency=currency)
            request = QueuedRequest(request_id=0, tenant="t", model="sdxl",
                                    arrival_s=0.0, steps=1)
            runtime.submit(request, StepExecutor(request, Fixed(),
                                                 total_steps=1))
            runtime.tick(0.0)
            charged[currency] = runtime.quota_seconds_by_tenant["t"]
        # exclusive_fcfs grants the whole die, so 32 x 0.1 against 0.1.
        self.assertAlmostEqual(charged["quota-seconds"], 3.2, places=6)
        self.assertAlmostEqual(charged["wall-seconds"], 0.1, places=6)
        self.assertAlmostEqual(charged["step-count"], 1.0, places=6)

    def test_the_currency_does_not_change_what_runs(self):
        """An accounting change that altered the schedule would confound
        the fairness measurement with a scheduling one."""
        grants = {}
        for currency in ("quota-seconds", "wall-seconds"):
            runtime = Runtime(exclusive_fcfs, charge_currency=currency)
            for index in range(2):
                request = QueuedRequest(request_id=index, tenant=f"t{index}",
                                        model="sdxl", arrival_s=0.0, steps=3)
                runtime.submit(request, StepExecutor(
                    request, CountingAdapter(), total_steps=3))
            grants[currency] = [dict(runtime.tick(i * 0.25).granted)
                                for i in range(4)]
        self.assertEqual(grants["quota-seconds"], grants["wall-seconds"])

    def test_externality_blind_leaves_the_charge_alone(self):
        """It ablates the belief, not the measurement."""
        class Measuring(CountingAdapter):
            def __init__(self):
                super().__init__()
                self.last_step_seconds = 0.3

        seen = {}
        for blind in (False, True):
            runtime = Runtime(probing_partitioning, externality_blind=blind)
            for index in range(2):
                request = QueuedRequest(request_id=index, tenant=f"t{index}",
                                        model="sdxl", arrival_s=0.0, steps=4)
                runtime.submit(request, StepExecutor(
                    request, Measuring(), total_steps=4))
            record = runtime.tick(0.0)
            seen[blind] = record.observed_step_seconds
        self.assertEqual(seen[False], seen[True])

    def test_externality_blind_makes_the_envelope_fire_on_a_faithful_run(self):
        """The mechanism the ablation is meant to expose.

        A co-run legitimately costs about 1.24x a solo step, so a runtime
        that does not know that reads a faithful pairing as 24% drift and
        holds it. That is the distinction the externality term draws, and
        it is why getting it wrong once made the envelope fire every
        round.
        """
        class Faithful(CountingAdapter):
            def __init__(self):
                super().__init__()
                # 0.1575 solo at 16 units x the fast-state externality.
                self.last_step_seconds = 0.1575 * 1.2367

        held = {}
        for blind in (False, True):
            runtime = Runtime(probing_partitioning, externality_blind=blind)
            for index in range(2):
                request = QueuedRequest(request_id=index, tenant=f"t{index}",
                                        model="sdxl", arrival_s=0.0, steps=6)
                runtime.submit(request, StepExecutor(
                    request, Faithful(), total_steps=6))
            runtime.tick(0.0)
            held[blind] = runtime.tick(0.25).notes["serial_fallback"]
        self.assertIsNone(held[False], "a faithful pairing is not held")
        self.assertIsNotNone(held[True], "blind to externality, it is held")


class FairnessIsScoredInOneCurrencyWhateverIsCharged(unittest.TestCase):
    """The trap the currency ablation exists to avoid.

    Charging wall-seconds and then scoring fairness in wall-seconds makes
    wall-seconds perfectly fair by construction. die_seconds_by_tenant is
    always units x seconds, so every currency is scored on die time
    consumed and the ablation can say something.
    """

    class Fixed(CountingAdapter):
        def __init__(self):
            super().__init__()
            self.last_step_seconds = 0.1

    def test_the_canonical_accumulator_ignores_the_currency(self):
        seen = {}
        for currency in ("quota-seconds", "wall-seconds", "step-count"):
            runtime = Runtime(exclusive_fcfs, charge_currency=currency)
            request = QueuedRequest(request_id=0, tenant="t", model="sdxl",
                                    arrival_s=0.0, steps=1)
            runtime.submit(request, StepExecutor(request, self.Fixed(),
                                                 total_steps=1))
            runtime.tick(0.0)
            seen[currency] = runtime.die_seconds_by_tenant["t"]
        self.assertEqual(len(set(round(v, 9) for v in seen.values())), 1)
        self.assertAlmostEqual(seen["wall-seconds"], 3.2, places=6)

    def test_the_charged_currency_still_differs(self):
        """Otherwise the ablation would be a no-op with extra steps."""
        runtime = Runtime(exclusive_fcfs, charge_currency="wall-seconds")
        request = QueuedRequest(request_id=0, tenant="t", model="sdxl",
                                arrival_s=0.0, steps=1)
        runtime.submit(request, StepExecutor(request, self.Fixed(),
                                             total_steps=1))
        runtime.tick(0.0)
        self.assertNotAlmostEqual(runtime.quota_seconds_by_tenant["t"],
                                  runtime.die_seconds_by_tenant["t"])


class TheEnvelopeFiresOnOverRunOnly(unittest.TestCase):
    """Being faster than predicted is not a reason to go serial.

    The check used abs(), so a pairing that came in cheaper than its
    paired expectation -- which happens whenever one side finishes and
    the other runs on at a paired quota -- read as drift and took the
    whole die serial. Measured in simulation: at a paired expectation of
    1.2367x solo, an observed step at 1.00x held 293 of 300 rounds. That
    is a pessimisation, and it inverted an ablation on hardware.
    """

    class AtRatio(CountingAdapter):
        def __init__(self, ratio):
            super().__init__()
            self.ratio = ratio
            self.last_step_seconds = None
            self.last_step_units = None

        def denoise_one(self, state, *, quota_units):
            solo = {4: 0.521, 8: 0.283, 16: 0.1575, 24: 0.1249,
                    32: 0.1155}.get(quota_units, 0.1575)
            self.last_step_seconds = solo * self.ratio
            self.last_step_units = quota_units
            return super().denoise_one(state, quota_units=quota_units)

    def _held(self, ratio, rounds=12):
        runtime = Runtime(probing_partitioning)
        for index in range(2):
            request = QueuedRequest(request_id=index, tenant=f"t{index}",
                                    model="sdxl", arrival_s=0.0, steps=400)
            runtime.submit(request, StepExecutor(request,
                                                 self.AtRatio(ratio),
                                                 total_steps=400))
        return sum(1 for k in range(rounds)
                   if runtime.tick(k * 0.25).notes.get("serial_fallback"))

    def test_a_pairing_cheaper_than_expected_is_not_held(self):
        """The defect. 1.00x solo against a 1.2367x expectation."""
        self.assertEqual(self._held(1.00), 0)

    def test_a_pairing_much_cheaper_is_still_not_held(self):
        self.assertEqual(self._held(0.80), 0)

    def test_a_pairing_dearer_than_the_tolerance_is_held(self):
        """The behaviour that must survive the fix."""
        self.assertGreater(self._held(1.95), 0)

    def test_the_boundary_is_on_the_over_run_side(self):
        """1.2367 x 1.15 = 1.422; just under holds nothing, just over holds."""
        self.assertEqual(self._held(1.40), 0)
        self.assertGreater(self._held(1.50), 0)
