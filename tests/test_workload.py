"""The matrix's arrival traces, and the properties the cells depend on.

Offered load, burst size and deadline slack are the axes the whole matrix
is indexed on. If burst size moved the mean rate, no cell would separate
burst from load -- the same defect as a repeat campaign that varied seed
with position, which cost a day. So the properties are asserted here
rather than assumed at the point of use.
"""

from __future__ import annotations

import statistics
import sys
import unittest

sys.dont_write_bytecode = True

from burstserve.workload import (
    CellSpec,
    build_trace,
    horizon_for_urgent_count,
)

URGENT_S = 0.9        # 8 SDXL steps on the whole die
VIDEO_S = 15.3        # 30 CogVideoX steps on the whole die
URGENT_LATENCY_P99 = 1.2   # a request alone on the die, not a step


def trace_for(**kwargs):
    spec = CellSpec(**{"load": 0.6, "burst": 4, "deadline_slack": 1.5,
                       "seed": 0, "horizon_s": 600.0, **kwargs})
    return spec, build_trace(spec, urgent_service_s=URGENT_S,
                             video_service_s=VIDEO_S,
                             urgent_isolated_latency_p99_s=URGENT_LATENCY_P99)


def urgent(trace):
    return [r for r in trace.requests if r.tenant == "urgent"]


class OfferedLoadIsWhatItSays(unittest.TestCase):
    def test_the_rate_matches_the_requested_load(self):
        """rho = rate x isolated service, against the whole die."""
        for load in (0.3, 0.6, 0.85, 1.05):
            with self.subTest(load=load):
                spec, trace = trace_for(load=load, horizon_s=4000.0)
                rate = len(urgent(trace)) / spec.horizon_s
                self.assertAlmostEqual(rate * URGENT_S, load * 0.5,
                                       delta=load * 0.5 * 0.12)

    def test_overload_is_reachable(self):
        """1.05 must genuinely exceed the die, not be renormalised away."""
        spec, trace = trace_for(load=1.05, horizon_s=4000.0)
        offered = (len(urgent(trace)) * URGENT_S
                   + len([r for r in trace.requests
                          if r.tenant == "video"]) * VIDEO_S)
        self.assertGreater(offered / spec.horizon_s, 1.0)

    def test_both_tenants_are_present(self):
        _, trace = trace_for()
        self.assertTrue(urgent(trace))
        self.assertTrue([r for r in trace.requests if r.tenant == "video"])


class BurstSizeHoldsTheMeanRate(unittest.TestCase):
    def test_the_urgent_rate_is_the_same_at_every_burst_size(self):
        """Otherwise burst and load move together and no cell separates them."""
        counts = []
        for burst in (1, 2, 4, 8):
            _, trace = trace_for(burst=burst, horizon_s=8000.0)
            counts.append(len(urgent(trace)))
        spread = (max(counts) - min(counts)) / statistics.mean(counts)
        self.assertLess(spread, 0.15, f"counts drifted with burst: {counts}")

    def test_a_burst_arrives_together(self):
        _, trace = trace_for(burst=4, horizon_s=600.0)
        arrivals = [r.arrival_s for r in urgent(trace)]
        groups = {}
        for stamp in arrivals:
            groups[stamp] = groups.get(stamp, 0) + 1
        self.assertTrue(groups)
        self.assertEqual(set(groups.values()), {4})

    def test_burst_one_is_not_bursty(self):
        _, trace = trace_for(burst=1, horizon_s=600.0)
        arrivals = [r.arrival_s for r in urgent(trace)]
        self.assertEqual(len(arrivals), len(set(arrivals)))


class DeadlinesAreRelativeToTheIsolatedP99(unittest.TestCase):
    def test_slack_multiplies_the_p99_not_the_request(self):
        for slack in (1.25, 1.5, 2.0):
            with self.subTest(slack=slack):
                _, trace = trace_for(deadline_slack=slack)
                for request in urgent(trace):
                    self.assertAlmostEqual(
                        request.deadline_s - request.arrival_s,
                        slack * URGENT_LATENCY_P99, places=9)

    def test_video_carries_no_deadline(self):
        """Its SLO is stall, a property of the schedule not the request."""
        _, trace = trace_for()
        for request in trace.requests:
            if request.tenant == "video":
                self.assertIsNone(request.deadline_s)


class TheTraceIsReproducibleAndOrdered(unittest.TestCase):
    def test_the_same_seed_gives_the_same_trace(self):
        _, first = trace_for(seed=7)
        _, second = trace_for(seed=7)
        self.assertEqual([(r.tenant, r.arrival_s, r.steps)
                          for r in first.requests],
                         [(r.tenant, r.arrival_s, r.steps)
                          for r in second.requests])

    def test_different_seeds_differ(self):
        _, first = trace_for(seed=1)
        _, second = trace_for(seed=2)
        self.assertNotEqual([r.arrival_s for r in first.requests],
                            [r.arrival_s for r in second.requests])

    def test_ids_follow_arrival_order(self):
        """Policies tie-break on id; an id order that disagreed with
        arrival would make the tie-break depend on generation order."""
        _, trace = trace_for()
        ids = [r.request_id for r in trace.requests]
        self.assertEqual(ids, sorted(ids))
        arrivals = [r.arrival_s for r in trace.requests]
        self.assertEqual(arrivals, sorted(arrivals))

    def test_nothing_arrives_after_the_horizon(self):
        spec, trace = trace_for()
        self.assertLess(max(r.arrival_s for r in trace.requests),
                        spec.horizon_s)

    def test_the_cell_id_names_every_axis(self):
        spec, _ = trace_for(load=0.85, burst=8, deadline_slack=2.0, seed=3)
        for piece in ("0.85", "8", "2", "3"):
            self.assertIn(piece, spec.cell_id)


class SizingTheHorizon(unittest.TestCase):
    def test_it_delivers_about_the_wanted_count(self):
        """plan.md asks for 200 urgent requests per tail point across
        seeds, and the rate differs 3.5x between the load extremes."""
        for load in (0.3, 1.05):
            with self.subTest(load=load):
                spec = CellSpec(load=load, burst=4, deadline_slack=1.5,
                                seed=0, horizon_s=1.0)
                horizon = horizon_for_urgent_count(
                    spec, urgent_service_s=URGENT_S, wanted=40)
                sized = CellSpec(load=load, burst=4, deadline_slack=1.5,
                                 seed=0, horizon_s=horizon)
                trace = build_trace(sized, urgent_service_s=URGENT_S,
                                    video_service_s=VIDEO_S,
                                    urgent_isolated_latency_p99_s=URGENT_LATENCY_P99)
                self.assertGreater(len(urgent(trace)), 40 * 0.6)
                self.assertLess(len(urgent(trace)), 40 * 1.6)

    def test_a_lighter_load_needs_a_longer_horizon(self):
        light = horizon_for_urgent_count(
            CellSpec(load=0.3, burst=4, deadline_slack=1.5, seed=0,
                     horizon_s=1.0), urgent_service_s=URGENT_S, wanted=40)
        heavy = horizon_for_urgent_count(
            CellSpec(load=1.05, burst=4, deadline_slack=1.5, seed=0,
                     horizon_s=1.0), urgent_service_s=URGENT_S, wanted=40)
        self.assertGreater(light, heavy * 3.0)


class BadSpecsAreRefused(unittest.TestCase):
    def test_a_zero_load_is_refused(self):
        with self.assertRaises(ValueError):
            CellSpec(load=0.0, burst=4, deadline_slack=1.5, seed=0,
                     horizon_s=10.0)

    def test_a_zero_burst_is_refused(self):
        with self.assertRaises(ValueError):
            CellSpec(load=0.6, burst=0, deadline_slack=1.5, seed=0,
                     horizon_s=10.0)

    def test_a_share_outside_the_open_interval_is_refused(self):
        spec = CellSpec(load=0.6, burst=4, deadline_slack=1.5, seed=0,
                        horizon_s=10.0)
        for share in (0.0, 1.0, -0.1, 1.5):
            with self.subTest(share=share):
                with self.assertRaises(ValueError):
                    build_trace(spec, urgent_service_s=URGENT_S,
                                video_service_s=VIDEO_S,
                                urgent_isolated_latency_p99_s=URGENT_LATENCY_P99,
                                video_share=share)

    def test_a_non_positive_service_time_is_refused(self):
        spec = CellSpec(load=0.6, burst=4, deadline_slack=1.5, seed=0,
                        horizon_s=10.0)
        with self.assertRaises(ValueError):
            build_trace(spec, urgent_service_s=0.0, video_service_s=VIDEO_S,
                        urgent_isolated_latency_p99_s=URGENT_LATENCY_P99)


if __name__ == "__main__":
    unittest.main()
