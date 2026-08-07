"""Canonical service accounting and the bound on service lag.

Gate C asks for two numbers that are easy to compute wrongly in ways that
always look like a pass. Accounting error compares what tenants were
charged against what the die granted; if the charge is derived from the
grant the two agree by construction and the check is decorative. Service
lag has to hold at every instant, and the end-of-run figure does not: a
policy that runs one tenant to completion and then the other finishes
exactly even, having been maximally unfair throughout.

So these tests spend most of their effort showing the metrics *can* fail.
"""

from __future__ import annotations

import sys
import unittest

sys.dont_write_bytecode = True

from burstserve.policies import (
    BASELINES,
    exclusive_fcfs,
    static_even,
    step_matched_pairing,
)
from burstserve.trace_sim import (
    PairingStates,
    Request,
    Trace,
    simulate,
)


def backlogged_trace(per_tenant: int = 3, steps: int = 40,
                     model: str = "sdxl") -> Trace:
    """Two tenants, equal demand, everything queued at t=0.

    An equal share is what each tenant is owed only while both are
    backlogged, so lag is only meaningful against a trace like this one.
    """
    return Trace([
        Request(request_id=i, tenant=f"t{i % 2}", model=model,
                arrival_s=0.0, steps=steps)
        for i in range(per_tenant * 2)
    ])


class AccountingTest(unittest.TestCase):
    def test_charges_match_the_die_that_was_granted(self):
        for name, policy in BASELINES.items():
            with self.subTest(policy=name):
                result = simulate(backlogged_trace(), policy, horizon_s=300.0)
                self.assertLess(result.accounting_error(), 0.01)

    def test_the_check_can_fail(self):
        """Perturb the charge and the error has to show it.

        Without this the 0.0000% above proves only that the number was
        computed, not that it measures anything.
        """
        result = simulate(backlogged_trace(), static_even, horizon_s=300.0)
        self.assertLess(result.accounting_error(), 0.01)
        tenant = next(iter(result.quota_seconds_by_tenant))
        result.quota_seconds_by_tenant[tenant] *= 1.10
        self.assertGreater(result.accounting_error(), 0.01)

    def test_held_units_are_charged_even_when_no_step_completes(self):
        """A tenant holding a quota it cannot finish a step in still pays.

        This is the case where a naive accounting -- charge per completed
        step -- silently under-bills, and it is reachable: a 16+16 split of
        CogVideoX has a 0.517 s step against a 0.25 s quantum.
        """
        trace = Trace([
            Request(request_id=i, tenant=f"t{i % 2}", model="cogvideox-2b",
                    arrival_s=0.0, steps=4)
            for i in range(2)
        ])
        result = simulate(trace, static_even, horizon_s=300.0)
        self.assertGreater(result.granted_unit_seconds, 0.0)
        self.assertLess(result.accounting_error(), 0.01)


class ServiceLagTest(unittest.TestCase):
    def test_partitioning_holds_the_two_quantum_bound(self):
        result = simulate(backlogged_trace(), static_even, horizon_s=300.0)
        self.assertLessEqual(result.peak_service_lag_quanta(), 2.0)

    def test_exclusive_scheduling_violates_it(self):
        """The bound is not vacuous.

        Giving one request the whole die until it finishes is exactly the
        behaviour the bound is meant to exclude, and it has to be excluded
        by the number, not by assertion.
        """
        result = simulate(backlogged_trace(), exclusive_fcfs, horizon_s=300.0)
        self.assertGreater(result.peak_service_lag_quanta(), 2.0)

    def test_the_final_gap_is_not_the_bound(self):
        """Exclusive FCFS ends even and was never even.

        If lag were measured at the end, the policy that most clearly
        violates the bound would pass it.
        """
        result = simulate(backlogged_trace(), exclusive_fcfs, horizon_s=300.0)
        self.assertAlmostEqual(
            result.service_lag_quanta(["t0", "t1"]), 0.0, places=6
        )
        self.assertGreater(result.peak_service_lag_quanta(), 2.0)

    def test_unequal_demand_is_not_charged_as_unfairness(self):
        """A tenant that asked for less is not behind.

        Lag is distance from the share a tenant was owed, and a tenant with
        nothing queued is owed nothing. Measuring max-minus-min instead
        would make every heterogeneous trace look like a fairness failure.
        """
        trace = Trace([
            Request(request_id=0, tenant="heavy", model="sdxl",
                    arrival_s=0.0, steps=60),
            Request(request_id=1, tenant="light", model="sdxl",
                    arrival_s=0.0, steps=4),
        ])
        result = simulate(trace, static_even, horizon_s=300.0)
        charged = result.quota_seconds_by_tenant
        self.assertGreater(charged["heavy"], charged["light"] * 2)
        # While both were runnable they were served evenly; the divergence
        # afterwards is demand, not lag.
        self.assertLessEqual(result.peak_service_lag_quanta(), 2.0)

    def test_lag_needs_two_tenants(self):
        trace = Trace([Request(request_id=0, tenant="solo", model="sdxl",
                               arrival_s=0.0, steps=10)])
        result = simulate(trace, static_even, horizon_s=300.0)
        self.assertEqual(result.peak_service_lag_quanta(), 0.0)
        self.assertEqual(result.service_lag_quanta(), 0.0)


class HeterogeneousTenantsTest(unittest.TestCase):
    """Partitioning's gain is not unconditional, and the code should say so.

    Pairing tenants whose step lengths differ sharply wastes the die: a
    round is as long as its slowest participant, so an SDXL tenant at
    0.152 s/step sitting beside CogVideoX-2b at 0.517 s/step idles for two
    thirds of every round. This is a real property of a fixed-round
    scheduler, not a simulator artifact, and it bounds the claim.
    """

    def test_matched_tenants_are_where_partitioning_wins(self):
        """In the fast pairing state, which is pinned rather than drawn.

        Blind pairing loses against the whole die once the measured
        bistability is in play; that this is a statement about the fast
        state is the point of pinning it here. The policy that copes with
        the draw is tested in test_pairing_probe.py.
        """
        fast = lambda: PairingStates(seed=0, enabled=False)
        same = simulate(backlogged_trace(model="sdxl"), static_even,
                        horizon_s=600.0, pairing_states=fast())
        excl = simulate(backlogged_trace(model="sdxl"), exclusive_fcfs,
                        horizon_s=600.0, pairing_states=fast())
        self.assertGreater(same.utilisation(), excl.utilisation())

    def test_mismatched_step_lengths_erode_the_gain(self):
        mixed = Trace([
            Request(request_id=0, tenant="fast", model="sdxl",
                    arrival_s=0.0, steps=40),
            Request(request_id=1, tenant="slow", model="cogvideox-2b",
                    arrival_s=0.0, steps=40),
        ])
        shared = simulate(mixed, static_even, horizon_s=1200.0)
        exclusive = simulate(mixed, exclusive_fcfs, horizon_s=1200.0)
        self.assertLess(shared.utilisation(), exclusive.utilisation())

    def test_the_erosion_is_step_length_mismatch_not_the_model(self):  # noqa: E501
        """Two CogVideoX tenants pair fine; it is the ratio that hurts.

        If the loss came from CogVideoX being expensive rather than from
        the mismatch, this would fail.
        """
        both_slow = backlogged_trace(per_tenant=2, steps=20,
                                     model="cogvideox-2b")
        fast = lambda: PairingStates(seed=0, enabled=False)
        shared = simulate(both_slow, static_even, horizon_s=1200.0,
                          pairing_states=fast())
        exclusive = simulate(both_slow, exclusive_fcfs, horizon_s=1200.0,
                             pairing_states=fast())
        self.assertGreater(shared.utilisation(), exclusive.utilisation())


class StepMatchedPairingTest(unittest.TestCase):
    """The one policy that holds both Gate C conditions at once.

    Every other baseline fails one of them on one of the two traces.
    Exclusive scheduling reaches full throughput and a peak lag of 12.17
    quanta. Unconditional splitting holds lag at zero and loses 30% of the
    die when the tenants' steps do not match. Pairing conditionally and
    rotating on deficit when it declines to pair holds both.
    """

    def _both_conditions(self, trace, horizon):
        result = simulate(trace, step_matched_pairing, horizon_s=horizon,
                          pairing_states=PairingStates(seed=0,
                                                       enabled=False))
        self.assertLessEqual(result.peak_service_lag_quanta(), 2.0)
        self.assertLess(result.accounting_error(), 0.01)
        return result

    def test_matched_tenants_keep_the_partitioning_gain(self):
        trace = backlogged_trace()
        mine = self._both_conditions(trace, 600.0)
        theirs = simulate(trace, exclusive_fcfs, horizon_s=600.0,
                          pairing_states=PairingStates(seed=0, enabled=False))
        self.assertGreater(mine.utilisation(), theirs.utilisation() * 1.05)

    def test_mismatched_tenants_do_not_cost_throughput(self):
        trace = Trace([
            Request(request_id=0, tenant="fast", model="sdxl",
                    arrival_s=0.0, steps=40),
            Request(request_id=1, tenant="slow", model="cogvideox-2b",
                    arrival_s=0.0, steps=40),
        ])
        mine = self._both_conditions(trace, 1200.0)
        fast = lambda: PairingStates(seed=0, enabled=False)
        split = simulate(trace, static_even, horizon_s=1200.0,
                         pairing_states=fast())
        excl = simulate(trace, exclusive_fcfs, horizon_s=1200.0,
                        pairing_states=fast())
        self.assertGreater(mine.utilisation(), split.utilisation())
        self.assertGreaterEqual(mine.utilisation(), excl.utilisation() - 1e-9)

    def test_no_baseline_holds_both_conditions_on_both_traces(self):
        """What makes the result a result.

        If some existing baseline already did this, the policy would be
        redundant and the test should say so by failing.
        """
        matched = backlogged_trace()
        mismatched = Trace([
            Request(request_id=0, tenant="fast", model="sdxl",
                    arrival_s=0.0, steps=40),
            Request(request_id=1, tenant="slow", model="cogvideox-2b",
                    arrival_s=0.0, steps=40),
        ])
        excl_util = simulate(mismatched, exclusive_fcfs,
                             horizon_s=1200.0).utilisation()
        # Only the policies that predate this one. slo_aware_partitioning
        # is built on top of it and holds both conditions by inheriting
        # them, which is the intended outcome rather than a counterexample.
        prior = ("exclusive_fcfs", "oracle_shortest_remaining",
                 "static_even", "measured_pairs_only", "deadline_aware")
        for name in prior:
            policy = BASELINES[name]
            with self.subTest(policy=name):
                a = simulate(matched, policy, horizon_s=600.0)
                b = simulate(mismatched, policy, horizon_s=1200.0)
                holds = (
                    a.peak_service_lag_quanta() <= 2.0
                    and b.peak_service_lag_quanta() <= 2.0
                    and a.utilisation() > 1.05
                    and b.utilisation() >= excl_util - 1e-9
                )
                self.assertFalse(holds)

    def test_the_deficit_rotation_is_what_bounds_the_lag(self):
        """Not the pairing test -- the fallback.

        Declining to pair is only half the policy. Handing the die to
        whoever arrived first instead of whoever is furthest behind gives
        the same throughput and breaks the bound, so this pins the half
        that does the work.
        """
        mismatched = Trace([
            Request(request_id=0, tenant="fast", model="sdxl",
                    arrival_s=0.0, steps=40),
            Request(request_id=1, tenant="slow", model="cogvideox-2b",
                    arrival_s=0.0, steps=40),
        ])
        mine = simulate(mismatched, step_matched_pairing, horizon_s=1200.0)
        fcfs = simulate(mismatched, exclusive_fcfs, horizon_s=1200.0)
        self.assertAlmostEqual(mine.utilisation(), fcfs.utilisation(),
                               places=6)
        self.assertLessEqual(mine.peak_service_lag_quanta(), 2.0)
        self.assertGreater(fcfs.peak_service_lag_quanta(), 2.0)

    def test_the_tolerance_is_what_decides(self):
        """A tolerance above the measured 3.4x ratio pairs everything.

        Which turns the policy back into static_even and loses the
        mismatched case -- so the threshold is doing the work, not an
        accident of the trace.
        """
        mismatched = Trace([
            Request(request_id=0, tenant="fast", model="sdxl",
                    arrival_s=0.0, steps=40),
            Request(request_id=1, tenant="slow", model="cogvideox-2b",
                    arrival_s=0.0, steps=40),
        ])
        permissive = simulate(
            mismatched,
            lambda st, u, n: step_matched_pairing(st, u, n, tolerance=10.0),
            horizon_s=1200.0,
        )
        split = simulate(mismatched, static_even, horizon_s=1200.0)
        self.assertAlmostEqual(permissive.utilisation(), split.utilisation(),
                               places=6)


if __name__ == "__main__":
    unittest.main()
