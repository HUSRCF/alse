from __future__ import annotations

import unittest

from burstserve.trace_sim import (
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

    def test_it_reproduces_the_measured_speed_curve(self):
        model = QuotaCostModel.for_model("sdxl")
        full = model.step_seconds(32)
        # Measured normalised speeds, 2026-08-03.
        for units, measured in ((4, 0.198), (8, 0.377), (16, 0.665),
                                (24, 0.860), (32, 1.000)):
            with self.subTest(units=units):
                predicted = full / model.step_seconds(units)
                self.assertLess(
                    abs(predicted - measured) / measured, 0.13,
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

    def test_the_table_is_not_monotone_so_interpolation_is_unsound(self):
        """The reason the refusal above is not merely cautious."""
        self.assertGreater(
            MEASURED_EXTERNALITY[(8, 24)], MEASURED_EXTERNALITY[(16, 16)]
        )
        self.assertGreater(
            MEASURED_EXTERNALITY[(28, 4)], MEASURED_EXTERNALITY[(24, 8)]
        )


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


if __name__ == "__main__":
    unittest.main()
