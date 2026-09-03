"""Intra-tenant concurrency: the only path 3.8 leaves open.

3.8 shows no split meets a 5.34 s burst deadline while the registry
serves one request per tenant, because the burst is serial whatever the
split. These tests pin that the policy divides the critical tenant's
quota among its own requests, keeps the batch tenant's slice, and never
over-commits the die.
"""

import unittest
from dataclasses import dataclass, field

from burstserve.policies import make_concurrent_quota


@dataclass
class FakeRequest:
    request_id: int
    tenant: str
    deadline_s: float | None
    steps: int = 8


@dataclass
class FakeState:
    request: FakeRequest
    steps_done: int = 0
    predicted_step_seconds: dict = field(default_factory=dict)
    tenant_quota_seconds: float = 0.0


def urgent(rid, deadline=5.0):
    return FakeState(FakeRequest(rid, "urgent", deadline))


def video(rid):
    return FakeState(FakeRequest(rid, "video", None))


class ConcurrentQuotaTest(unittest.TestCase):

    def setUp(self):
        self.p4 = make_concurrent_quota(concurrency=4, urgent_units=24)
        self.p2 = make_concurrent_quota(concurrency=2, urgent_units=24)

    def test_four_urgent_requests_share_the_urgent_quota(self):
        g = self.p4([urgent(1), urgent(2), urgent(3), urgent(4), video(9)],
                    32, 0.0)
        self.assertEqual({g[i] for i in (1, 2, 3, 4)}, {6})
        self.assertEqual(g[9], 8)
        self.assertEqual(sum(g.values()), 32)

    def test_two_at_a_time_when_concurrency_is_two(self):
        g = self.p2([urgent(1), urgent(2), urgent(3), urgent(4), video(9)],
                    32, 0.0)
        self.assertEqual(sorted(g), [1, 2, 9])
        self.assertEqual(g[1], 12)
        self.assertEqual(g[2], 12)
        self.assertEqual(g[9], 8)

    def test_fewer_runnable_than_the_concurrency(self):
        g = self.p4([urgent(1), urgent(2), video(9)], 32, 0.0)
        self.assertEqual(g[1], 12)
        self.assertEqual(g[2], 12)
        self.assertEqual(g[9], 8)
        self.assertEqual(sum(g.values()), 32)

    def test_batch_tenant_is_never_starved(self):
        for n in range(1, 6):
            states = [urgent(i) for i in range(1, n + 1)] + [video(99)]
            g = self.p4(states, 32, 0.0)
            self.assertGreaterEqual(g[99], 1, n)
            self.assertEqual(sum(g.values()), 32, n)

    def test_never_over_commits_the_die(self):
        for c in (1, 2, 3, 4, 8):
            p = make_concurrent_quota(concurrency=c, urgent_units=24)
            states = [urgent(i) for i in range(1, 9)] + [video(99)]
            g = p(states, 32, 0.0)
            self.assertEqual(sum(g.values()), 32, c)
            self.assertTrue(all(v >= 1 for v in g.values()), c)

    def test_earliest_deadline_requests_are_the_ones_run(self):
        states = [urgent(1, 9.0), urgent(2, 4.0), urgent(3, 5.0), video(9)]
        g = self.p2(states, 32, 0.0)
        self.assertEqual(sorted(k for k in g if k != 9), [2, 3])

    def test_whole_die_divided_when_there_is_no_batch_tenant(self):
        g = self.p4([urgent(1), urgent(2), urgent(3), urgent(4)], 32, 0.0)
        self.assertEqual(sum(g.values()), 32)
        self.assertEqual(len(g), 4)

    def test_no_deadline_anywhere_falls_back_to_an_even_split(self):
        g = self.p4([video(1), video(2)], 32, 0.0)
        self.assertEqual(g, {1: 16, 2: 16})

    def test_empty(self):
        self.assertEqual(self.p4([], 32, 0.0), {})

    def test_rejects_a_split_that_leaves_nothing(self):
        with self.assertRaises(ValueError):
            make_concurrent_quota(concurrency=2, urgent_units=32)
        with self.assertRaises(ValueError):
            make_concurrent_quota(concurrency=0)

    def test_it_is_registered_as_a_factory(self):
        from burstserve.policies import POLICY_FACTORIES
        for name in ("concurrent_quota_c2", "concurrent_quota_c4"):
            self.assertIn(name, POLICY_FACTORIES)
            self.assertTrue(callable(POLICY_FACTORIES[name]()))


class RegistryOffersSeveralTest(unittest.TestCase):
    """The registry change this policy needs, and its default."""

    def test_default_is_still_one_per_tenant(self):
        from burstserve.queues import TenantRegistry, QueuedRequest
        reg = TenantRegistry()
        for i in range(4):
            reg.admit(QueuedRequest(request_id=i, tenant="urgent",
                                    model="sdxl", arrival_s=0.0, steps=8,
                                    deadline_s=5.0))
        self.assertEqual(len(reg.ready(1.0)), 1)
        self.assertEqual(len(reg.ready(1.0, per_tenant=4)), 4)
        self.assertEqual(len(reg.ready(1.0, per_tenant=99)), 4)

    def test_zero_is_refused(self):
        from burstserve.queues import TenantRegistry
        with self.assertRaises(ValueError):
            TenantRegistry().ready(0.0, per_tenant=0)


if __name__ == "__main__":
    unittest.main()
