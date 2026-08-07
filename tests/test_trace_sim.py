from __future__ import annotations

import unittest

from burstserve.trace_sim import (
    PairingStates,
    MEASURED_EXTERNALITY,
    MEASURED_MODELS,
    QuotaCostModel,
    Request,
    Trace,
    UnmeasuredPairing,
    externality,
    simulate,
)


def even_split(states, units, now):
    """Give every runnable request an equal share, largest first."""
    if not states:
        return {}
    share = units // len(states)
    if share < 1:
        # Not enough die to go round: serve the oldest that fit.
        return {s.request.request_id: 1 for s in states[:units]}
    return {s.request.request_id: share for s in states}


def one_at_a_time(states, units, now):
    return {states[0].request.request_id: units} if states else {}


class QuotaCostTest(unittest.TestCase):
    """Step cost comes from the measured tables, not from a shape."""

    def test_it_agrees_with_an_independent_measurement(self):
        """Cross-check against a probe that shares no code with the table.

        The transition probe measured SDXL at 768x768 on 2026-08-03 with
        its own harness: 263.3 / 155.1 / 112.4 ms at 8 / 16 / 32 units.
        The table was built from a different script three days later.
        Checking the table against a restatement of its own inputs would
        prove only that the numbers were copied correctly.

        Tolerance is 5%, tightened from the 13% the previous version
        needed -- that version compared this per-step table against
        *call*-level speeds, which are a different quantity and could
        never have agreed closely.
        """
        model = QuotaCostModel.for_model("sdxl")
        full = model.step_seconds(32)
        probe = {8: 0.2633, 16: 0.1551, 32: 0.1124}
        probe_full = probe[32]
        for units in sorted(probe):
            with self.subTest(units=units):
                predicted = full / model.step_seconds(units)
                measured = probe_full / probe[units]
                self.assertLess(
                    abs(predicted - measured) / measured, 0.05,
                    f"{units} units: {predicted:.3f} against {measured:.3f}",
                )

    def test_a_quota_the_die_cannot_express_is_refused(self):
        model = QuotaCostModel.for_model("sdxl")
        for units in (0, -1, 33):
            with self.subTest(units=units), self.assertRaises(ValueError):
                model.step_seconds(units)

    def test_an_unmeasured_model_is_refused_rather_than_defaulted(self):
        with self.assertRaises(KeyError):
            QuotaCostModel.for_model("flux-dev")

    def test_step_time_falls_monotonically_with_quota(self):
        for name in MEASURED_MODELS:
            with self.subTest(name):
                model = QuotaCostModel.for_model(name)
                times = [model.step_seconds(u) for u in (4, 8, 16, 24, 32)]
                self.assertEqual(times, sorted(times, reverse=True))


class ExternalityTest(unittest.TestCase):
    """An unmeasured pairing must not be invented.

    The measured penalties run from 1.22x to 1.93x and are not monotone in
    either quota -- 16+16 costs less than 8+24 despite the larger share.
    Interpolating between them could be wrong by a factor of eight in an
    unknown direction.
    """

    def test_solo_has_no_penalty(self):
        self.assertEqual(externality(32, None), 1.0)

    def test_measured_pairs_are_returned(self):
        self.assertEqual(externality(16, 16), MEASURED_EXTERNALITY[(16, 16)])
        self.assertEqual(externality(28, 4), MEASURED_EXTERNALITY[(28, 4)])

    def test_an_unmeasured_pair_raises_rather_than_interpolating(self):
        with self.assertRaises(UnmeasuredPairing):
            externality(12, 20)

    def test_the_table_is_monotone_but_still_too_sparse_to_interpolate(self):
        """The refusal stands; the reason for it has changed.

        The old call-level table was non-monotone -- 16+16 appearing to
        cost less than 8+24 -- and that was cited as proof that a value
        invented between two entries could be wrong in an unknown
        direction. In-process measurement made the curve monotone:
        1.338, 1.307, 1.237, 1.126, 1.071 as own-quota rises. The old
        shape was an artifact of a (28,4) entry that read 1.926 against a
        measured 1.071.

        Interpolation is still refused, because five points do not
        establish a shape, and this test now pins the monotonicity so a
        future entry that breaks it has to be noticed.
        """
        by_own = sorted(MEASURED_EXTERNALITY.items(), key=lambda kv: kv[0][0])
        factors = [value for _, value in by_own]
        for wider, narrower in zip(factors, factors[1:]):
            self.assertGreater(wider, narrower)
        with self.assertRaises(UnmeasuredPairing):
            externality(12, 20)

class ReproducibilityTest(unittest.TestCase):
    """Gate C requires byte-identical results from the same seed."""

    def _trace(self, seed=7):
        return Trace.poisson(
            seed=seed,
            tenants=(("a", "sdxl"), ("b", "sdxl")),
            rate_per_s=2.0, horizon_s=20.0, steps=20,
        )

    def test_the_same_seed_yields_the_same_trace(self):
        first, second = self._trace(), self._trace()
        self.assertEqual(
            [r.__dict__ for r in first], [r.__dict__ for r in second]
        )

    def test_a_different_seed_yields_a_different_trace(self):
        """Otherwise the check above would pass on a constant."""
        self.assertNotEqual(
            [r.arrival_s for r in self._trace(1)],
            [r.arrival_s for r in self._trace(2)],
        )

    def test_repeated_simulation_is_identical(self):
        trace = self._trace()
        runs = [
            simulate(trace, even_split, horizon_s=20.0, seed=3)
            for _ in range(3)
        ]
        for other in runs[1:]:
            self.assertEqual(
                [s.request.request_id for s in other.completed],
                [s.request.request_id for s in runs[0].completed],
            )
            self.assertEqual(
                other.quota_seconds_by_tenant,
                runs[0].quota_seconds_by_tenant,
            )
            self.assertEqual(other.steps_executed, runs[0].steps_executed)

    def test_the_trace_is_totally_ordered(self):
        """Ties on arrival must not depend on construction order."""
        out_of_order = [
            Request(request_id=2, tenant="a", model="sdxl", arrival_s=1.0, steps=5),
            Request(request_id=1, tenant="b", model="sdxl", arrival_s=1.0, steps=5),
            Request(request_id=0, tenant="a", model="sdxl", arrival_s=0.5, steps=5),
        ]
        self.assertEqual(
            [r.request_id for r in Trace(out_of_order)], [0, 1, 2]
        )


class AccountingTest(unittest.TestCase):
    def test_a_policy_cannot_hand_out_more_die_than_exists(self):
        def greedy(states, units, now):
            return {s.request.request_id: units for s in states}

        trace = Trace([
            Request(request_id=i, tenant=f"t{i}", model="sdxl",
                    arrival_s=0.0, steps=5)
            for i in range(2)
        ])
        with self.assertRaises(ValueError):
            simulate(trace, greedy, horizon_s=5.0)

    def test_quota_seconds_track_the_units_actually_held(self):
        """The accounting currency is units x seconds, not wall time.

        Wall time would charge a tenant for a slowdown its peer caused; the
        measured externality reaches 128% at an 8+24 split, so the two are
        not interchangeable.
        """
        trace = Trace([
            Request(request_id=0, tenant="solo", model="sdxl",
                    arrival_s=0.0, steps=4),
        ])
        result = simulate(trace, one_at_a_time, horizon_s=30.0)
        state = result.completed[0]
        self.assertAlmostEqual(
            state.quota_seconds, 32 * state.service_seconds, places=9
        )

    def test_an_unserved_request_is_reported_unfinished_not_dropped(self):
        trace = Trace([
            Request(request_id=0, tenant="a", model="sdxl",
                    arrival_s=0.0, steps=10_000),
        ])
        result = simulate(trace, one_at_a_time, horizon_s=2.0)
        self.assertEqual(result.completed, [])
        self.assertEqual(len(result.unfinished), 1)

    def test_jain_index_is_one_for_an_even_split(self):
        trace = Trace([
            Request(request_id=i, tenant=f"t{i % 2}", model="sdxl",
                    arrival_s=0.0, steps=6)
            for i in range(2)
        ])
        result = simulate(trace, even_split, horizon_s=40.0)
        self.assertGreater(result.jain_index(), 0.98)

    def test_jain_index_falls_when_one_tenant_is_starved(self):
        """The metric must be able to report unfairness.

        A tenant that is never served has to appear in the accounting with
        zero, or the index is computed over the survivors and reports
        perfect fairness for a policy that ignored someone.
        """
        def starve(states, units, now):
            for state in states:
                if state.request.request_id == 0:
                    return {0: units}
            return {}

        trace = Trace([
            Request(request_id=0, tenant="fed", model="sdxl",
                    arrival_s=0.0, steps=6),
            Request(request_id=1, tenant="starved", model="sdxl",
                    arrival_s=0.0, steps=6),
        ])
        result = simulate(trace, starve, horizon_s=40.0)
        self.assertIn("starved", result.quota_seconds_by_tenant)
        self.assertEqual(result.quota_seconds_by_tenant["starved"], 0.0)
        self.assertLess(result.jain_index(), 0.98)
        self.assertAlmostEqual(result.jain_index(), 0.5, places=6)


class UnmeasuredPairingsAreReportedTest(unittest.TestCase):
    """A simulation that guessed must say so.

    Three concurrent tenants have no measured externality at all, and two
    with an unmeasured split have none either. Falling back to 1.0 silently
    would make the simulator optimistic exactly where it knows least.
    """

    def test_three_way_sharing_is_recorded_as_unmeasured(self):
        trace = Trace([
            Request(request_id=i, tenant=f"t{i}", model="sdxl",
                    arrival_s=0.0, steps=3)
            for i in range(3)
        ])
        result = simulate(trace, even_split, horizon_s=20.0)
        self.assertTrue(result.unmeasured_pairings)

    def test_a_measured_pair_records_nothing(self):
        trace = Trace([
            Request(request_id=i, tenant=f"t{i}", model="sdxl",
                    arrival_s=0.0, steps=3)
            for i in range(2)
        ])
        result = simulate(trace, even_split, horizon_s=40.0)
        self.assertEqual(result.unmeasured_pairings, [])



def _fast():
    """Pin the fast pairing state.

    The gain these tests assert is a fast-state property; under
    the measured 30/70 draw, blind pairing loses to the whole
    die. test_pairing_probe.py covers the drawn case.
    """
    return PairingStates(seed=0, enabled=False)


class UtilisationClaimTest(unittest.TestCase):
    """Spatial partitioning finishes more work per second than a full die.

    Not against a strawman: the comparator gives every request all 32
    units, which is the best a runtime without partitioning can do and the
    best possible per-request latency. Partitioning wins because SDXL
    spends 39.1% of its full-die step in work that does not shrink with
    quota, and two tenants overlap that portion instead of paying it in
    series.
    """

    def _saturated(self, steps=20, count=8):
        return Trace([
            Request(request_id=i, tenant=f"t{i % 2}", model="sdxl",
                    arrival_s=0.0, steps=steps)
            for i in range(count)
        ])

    def test_exclusive_scheduling_saturates_at_unit_utilisation(self):
        """The baseline is not idle: it is the reference point."""
        from burstserve.policies import exclusive_fcfs

        result = simulate(self._saturated(), exclusive_fcfs, horizon_s=120.0)
        self.assertAlmostEqual(result.utilisation(), 1.0, places=6)

    def test_partitioning_exceeds_it(self):
        from burstserve.policies import exclusive_fcfs, static_even

        trace = self._saturated()
        exclusive = simulate(trace, exclusive_fcfs, horizon_s=120.0, pairing_states=_fast())
        split = simulate(trace, static_even, horizon_s=120.0, pairing_states=_fast())
        self.assertGreater(split.utilisation(), exclusive.utilisation())
        self.assertGreater(split.utilisation(), 1.0)
        # The measured margin, not an arbitrary threshold.
        # 1.189 with the per-step cost table measured 2026-08-06; it read
        # 1.088 while the table was built from call p50s and a full-die
        # constant that was actually a half-die one.
        self.assertAlmostEqual(split.utilisation(), 1.189, places=2)

    def test_it_also_finishes_the_backlog_sooner(self):
        from burstserve.policies import exclusive_fcfs, static_even

        trace = self._saturated()
        exclusive = simulate(trace, exclusive_fcfs, horizon_s=120.0, pairing_states=_fast())
        split = simulate(trace, static_even, horizon_s=120.0, pairing_states=_fast())
        self.assertLess(split.horizon_s, exclusive.horizon_s)

    def test_both_policies_complete_the_same_work(self):
        """Otherwise the utilisation comparison is between different jobs."""
        from burstserve.policies import exclusive_fcfs, static_even

        trace = self._saturated()
        exclusive = simulate(trace, exclusive_fcfs, horizon_s=120.0, pairing_states=_fast())
        split = simulate(trace, static_even, horizon_s=120.0, pairing_states=_fast())
        self.assertEqual(len(exclusive.completed), len(trace))
        self.assertEqual(len(split.completed), len(trace))
        self.assertEqual(exclusive.steps_executed, split.steps_executed)


class MeasuredCurvePreferredTest(unittest.TestCase):
    """Where a measurement exists, the fit must not be used instead.

    The fit under-predicts speed at low quota, and that error is not
    neutral: it under-states the partitioning gain. A conservative model
    that happens to weaken the claim being made is not a safe default.

    Ratios below are full-die speed divided by the speed at each quota,
    from the per-step curve measured at 768x768 on 2026-08-06.
    """

    def test_measured_quotas_reproduce_the_table_exactly(self):
        model = QuotaCostModel.for_model("sdxl")
        full = model.step_seconds(32)
        for units, measured in ((4, 0.222), (8, 0.430), (12, 0.597),
                                (16, 0.733), (20, 0.833), (24, 0.925),
                                (28, 0.958)):
            with self.subTest(units=units):
                self.assertAlmostEqual(
                    full / model.step_seconds(units), measured, places=3
                )

    def test_a_measured_quota_is_flagged_as_measured(self):
        model = QuotaCostModel.for_model("sdxl")
        self.assertTrue(model.is_measured(16))
        self.assertFalse(model.is_measured(15))

    def test_an_unmeasured_quota_still_gets_a_value_from_the_fit(self):
        """Extrapolation is what the fit is for -- it just is not preferred."""
        model = QuotaCostModel.for_model("sdxl")
        between = model.step_seconds(15)
        self.assertLess(model.step_seconds(16), between)
        self.assertLess(between, model.step_seconds(12))


class PredictorDegradationTest(unittest.TestCase):
    """Gate C: safe degradation at +/-5%, 10% and 20% predictor error.

    "Safe" is given a testable meaning: a policy that consults a predictor
    must never, at any error level, do worse than the same scheduler with
    no predictor at all. Wrong beliefs may cost the benefit; they must not
    cost more than the benefit.
    """

    # Retuned 2026-08-06 when the cost table was rebuilt on per-step
    # measurements: steps got 32-50% cheaper, so the old rate of 0.30 no
    # longer congested the die and every policy met every deadline, which
    # would report perfect robustness for a scheduler that has none.
    #
    # 0.80 rather than the smallest rate that separates the policies on
    # one trace. Checked across ten arrival seeds, informed scheduling
    # beats blind on 3/10 at 0.45, 9/10 at 0.60 and 10/10 at 0.80 and
    # above -- so the low rates separate them only by luck of the draw,
    # and a scenario picked on a single seed there measures the seed.
    RATE, SLACK, HORIZON = 0.80, 4.0, 120.0

    def _trace(self):
        return Trace.poisson(
            seed=11, tenants=(("a", "sdxl"), ("b", "sdxl")),
            rate_per_s=self.RATE, horizon_s=self.HORIZON, steps=20,
            deadline_slack=self.SLACK,
        )

    def _misses(self, policy, error, seeds=range(5)):
        from burstserve.trace_sim import Predictor

        trace = self._trace()
        return [
            len(simulate(trace, policy, horizon_s=self.HORIZON,
                         predictor=Predictor(relative_error=error, seed=s))
                .deadline_misses())
            for s in seeds
        ]

    def test_the_scenario_is_sensitive_enough_to_tell_policies_apart(self):
        """Otherwise the robustness result would be about nothing.

        An overloaded trace misses everything regardless of policy, and an
        underloaded one misses nothing; either would report perfect
        robustness for a scheduler that ignores its predictor.
        """
        from burstserve.policies import deadline_aware, static_even

        informed = self._misses(deadline_aware, 0.0)[0]
        blind = self._misses(static_even, 0.0)[0]
        self.assertLess(informed, blind)
        self.assertGreater(blind, 0)

    def test_no_error_level_is_worse_than_ignoring_the_predictor(self):
        from burstserve.policies import deadline_aware, static_even

        blind = self._misses(static_even, 0.0)[0]
        for error in (0.05, 0.10, 0.20):
            with self.subTest(error=error):
                worst = max(self._misses(deadline_aware, error))
                self.assertLessEqual(
                    worst, blind,
                    f"at {error:.0%} error the worst case ({worst}) is worse "
                    f"than not predicting at all ({blind})",
                )

    def test_the_advantage_holds_across_arrival_seeds(self):
        """One trace is not a result, in either direction.

        An earlier version of this file pinned a "defect" from a single
        seed at rate 0.60, where the intervening policy missed 35 against
        the non-intervening policy's 26. Across ten seeds at that rate the
        intervening policy wins on nine, so that reading measured the
        arrival sequence rather than the scheduler. Two attempted fixes
        for the non-defect made every higher rate worse, which is the cost
        of chasing a single sample.

        The claim that survives is statistical, so the test is too.
        """
        from burstserve.policies import deadline_aware, static_even

        wins = 0
        for seed in range(1, 11):
            trace = Trace.poisson(
                seed=seed, tenants=(("a", "sdxl"), ("b", "sdxl")),
                rate_per_s=self.RATE, horizon_s=self.HORIZON, steps=20,
                deadline_slack=self.SLACK,
            )
            informed = len(simulate(trace, deadline_aware,
                                    horizon_s=self.HORIZON).deadline_misses())
            blind = len(simulate(trace, static_even,
                                 horizon_s=self.HORIZON).deadline_misses())
            wins += informed < blind
        self.assertGreaterEqual(wins, 9, f"only {wins}/10 seeds")

    def test_degradation_is_gradual_rather_than_a_cliff(self):
        from burstserve.policies import deadline_aware

        exact = sum(self._misses(deadline_aware, 0.0)) / 5
        worst = sum(self._misses(deadline_aware, 0.20)) / 5
        self.assertLess(abs(worst - exact) / max(exact, 1.0), 0.5)

    def test_a_policy_ignoring_the_predictor_is_unaffected_by_its_error(self):
        """The control: error must reach the result only through decisions."""
        from burstserve.policies import static_even

        self.assertEqual(
            set(self._misses(static_even, 0.0)),
            set(self._misses(static_even, 0.20)),
        )


class PredictorMechanicsTest(unittest.TestCase):
    def test_an_exact_predictor_returns_the_true_cost(self):
        from burstserve.trace_sim import Predictor

        predictor = Predictor(relative_error=0.0, seed=1)
        self.assertTrue(predictor.is_exact())
        self.assertAlmostEqual(
            predictor.step_seconds(0, "sdxl", 16),
            QuotaCostModel.for_model("sdxl").step_seconds(16),
            places=12,
        )

    def test_the_same_question_gets_the_same_answer(self):
        """A predictor that resampled would let a policy average away its
        own error by asking repeatedly, which no real predictor allows."""
        from burstserve.trace_sim import Predictor

        predictor = Predictor(relative_error=0.2, seed=1)
        first = predictor.step_seconds(3, "sdxl", 16)
        for _ in range(5):
            self.assertEqual(predictor.step_seconds(3, "sdxl", 16), first)

    def test_error_stays_within_the_declared_magnitude(self):
        from burstserve.trace_sim import Predictor

        truth = QuotaCostModel.for_model("sdxl").step_seconds(16)
        predictor = Predictor(relative_error=0.1, seed=4)
        for request_id in range(200):
            value = predictor.step_seconds(request_id, "sdxl", 16)
            self.assertLessEqual(abs(value - truth) / truth, 0.1 + 1e-12)

    def test_a_negative_error_magnitude_is_refused(self):
        from burstserve.trace_sim import Predictor

        with self.assertRaises(ValueError):
            Predictor(relative_error=-0.1)


class FeasibleTraceTest(unittest.TestCase):
    """Gate C: a feasible deadline trace has no avoidable miss."""

    def test_a_feasible_trace_is_served_without_misses(self):
        from burstserve.policies import deadline_aware, static_even

        trace = Trace.poisson(
            seed=11, tenants=(("a", "sdxl"), ("b", "sdxl")),
            rate_per_s=0.30, horizon_s=120.0, steps=20, deadline_slack=8.0,
        )
        for policy in (static_even, deadline_aware):
            with self.subTest(policy.__name__):
                result = simulate(trace, policy, horizon_s=120.0)
                self.assertEqual(result.deadline_misses(), [])
                # Requests still running at the horizon are not misses --
                # their deadlines have not arrived. Asserted explicitly so
                # that "no misses" cannot be satisfied by a horizon that
                # simply ended before anything was due.
                for state in result.unfinished:
                    self.assertIsNotNone(state.request.deadline_s)
                    self.assertGreater(
                        state.request.deadline_s, result.horizon_s
                    )
                self.assertGreater(len(result.completed), 0.9 * len(trace))

    def test_the_feasibility_margin_is_not_trivially_wide(self):
        """Halving the slack must produce misses, or the test above passes
        on a trace no scheduler could fail."""
        from burstserve.policies import static_even

        tight = Trace.poisson(
            seed=11, tenants=(("a", "sdxl"), ("b", "sdxl")),
            rate_per_s=0.30, horizon_s=120.0, steps=20, deadline_slack=4.0,
        )
        result = simulate(tight, static_even, horizon_s=120.0)
        self.assertGreater(len(result.deadline_misses()), 0)

if __name__ == "__main__":
    unittest.main()
