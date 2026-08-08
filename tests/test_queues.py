"""Two scheduling layers, and the separation between them.

The between-tenant decision answers to fairness and the service ledger;
the within-tenant decision answers only to that tenant's own ordering.
Collapsing them lets a tenant with many small requests starve one with
few large ones while every individual decision looks reasonable, so the
separation is tested rather than assumed.
"""

from __future__ import annotations

import sys
import unittest

sys.dont_write_bytecode = True

from burstserve.queues import (
    Discipline,
    QueueError,
    QueuedRequest,
    TenantQueue,
    TenantRegistry,
)


def request(rid, tenant="t0", arrival=0.0, deadline=None, steps=20):
    return QueuedRequest(request_id=rid, tenant=tenant, model="sdxl",
                         arrival_s=arrival, steps=steps, deadline_s=deadline)


class FcfsOrdersByArrival(unittest.TestCase):
    def test_arrival_order_not_admission_order(self):
        queue = TenantQueue("t0", discipline=Discipline.FCFS)
        queue.admit(request(3, arrival=3.0))
        queue.admit(request(1, arrival=1.0))
        queue.admit(request(2, arrival=2.0))
        self.assertEqual([r.request_id for r in queue.ready(now=10.0)],
                         [1, 2, 3])

    def test_unarrived_requests_are_not_ready(self):
        queue = TenantQueue("t0")
        queue.admit(request(1, arrival=5.0))
        self.assertEqual(queue.ready(now=1.0), [])
        self.assertEqual(len(queue.ready(now=5.0)), 1)

    def test_ties_break_by_id_so_the_order_is_total(self):
        """Two requests at the same instant must still have an order, or
        a replay from the same trace could differ between runs."""
        queue = TenantQueue("t0")
        for rid in (5, 2, 9):
            queue.admit(request(rid, arrival=1.0))
        self.assertEqual([r.request_id for r in queue.ready(now=2.0)],
                         [2, 5, 9])


class EdfOrdersByDeadline(unittest.TestCase):
    def test_earliest_deadline_first(self):
        queue = TenantQueue("t0", discipline=Discipline.EDF)
        queue.admit(request(1, arrival=0.0, deadline=30.0))
        queue.admit(request(2, arrival=1.0, deadline=10.0))
        queue.admit(request(3, arrival=2.0, deadline=20.0))
        self.assertEqual([r.request_id for r in queue.ready(now=5.0)],
                         [2, 3, 1])

    def test_deadline_free_requests_go_last(self):
        """Last because they have no deadline, not because they were
        sorted after a request due in the year 9999."""
        queue = TenantQueue("t0", discipline=Discipline.EDF)
        queue.admit(request(1, arrival=0.0, deadline=None))
        queue.admit(request(2, arrival=5.0, deadline=100.0))
        self.assertEqual([r.request_id for r in queue.ready(now=10.0)],
                         [2, 1])

    def test_deadline_free_requests_keep_fcfs_among_themselves(self):
        queue = TenantQueue("t0", discipline=Discipline.EDF)
        queue.admit(request(2, arrival=2.0))
        queue.admit(request(1, arrival=1.0))
        self.assertEqual([r.request_id for r in queue.ready(now=5.0)],
                         [1, 2])

    def test_the_discipline_can_change_between_rounds(self):
        """Sorting on read rather than on insert is what allows this; a
        structure that baked the order in at admission would answer the
        question that was asked when the request arrived."""
        queue = TenantQueue("t0", discipline=Discipline.FCFS)
        queue.admit(request(1, arrival=0.0, deadline=30.0))
        queue.admit(request(2, arrival=1.0, deadline=10.0))
        self.assertEqual([r.request_id for r in queue.ready(now=5.0)], [1, 2])
        queue.discipline = Discipline.EDF
        self.assertEqual([r.request_id for r in queue.ready(now=5.0)], [2, 1])


class PreemptionKeepsAPlace(unittest.TestCase):
    def test_a_suspended_request_returns_to_its_discipline_position(self):
        """Preemption is not failure.

        Pushing a preempted request to the back would make being
        preempted cost latency in a way the discipline never sanctioned,
        and the cost would fall on whoever the scheduler preempted most.
        """
        queue = TenantQueue("t0", discipline=Discipline.FCFS)
        queue.admit(request(1, arrival=1.0))
        queue.admit(request(2, arrival=2.0))
        queue.start(1)
        queue.suspend(1)
        self.assertEqual([r.request_id for r in queue.ready(now=5.0)], [1, 2])

    def test_in_flight_requests_are_not_offered_again(self):
        queue = TenantQueue("t0")
        queue.admit(request(1, arrival=0.0))
        queue.start(1)
        self.assertEqual(queue.ready(now=5.0), [])
        self.assertEqual(len(queue.in_flight), 1)

    def test_a_finished_request_leaves(self):
        queue = TenantQueue("t0")
        queue.admit(request(1, arrival=0.0))
        queue.start(1)
        queue.finish(1)
        self.assertEqual(queue.backlog, 0)
        self.assertEqual(queue.ready(now=5.0), [])


class BacklogIsWhatFairnessIsMeasuredOver(unittest.TestCase):
    def test_a_mid_step_request_still_counts_as_backlog(self):
        """The fairness clauses are about backlogged tenants, and a
        tenant whose only request is in flight is still owed service."""
        queue = TenantQueue("t0")
        queue.admit(request(1, arrival=0.0))
        queue.start(1)
        self.assertEqual(queue.backlog, 1)

    def test_backlogged_tenants_are_the_ones_with_work(self):
        registry = TenantRegistry()
        registry.admit(request(1, tenant="a", arrival=0.0))
        registry.admit(request(2, tenant="b", arrival=0.0))
        registry.queue_for("b").start(2)
        registry.queue_for("b").finish(2)
        self.assertEqual(registry.backlogged(), ("a",))


class RequestsAreNeverLostOrDuplicated(unittest.TestCase):
    def test_admitting_the_same_id_twice_is_refused(self):
        queue = TenantQueue("t0")
        queue.admit(request(1))
        with self.assertRaises(QueueError):
            queue.admit(request(1))

    def test_a_request_for_another_tenant_is_refused(self):
        """Routing errors have to fail at the queue, or a tenant's
        service ledger silently absorbs another's work."""
        queue = TenantQueue("t0")
        with self.assertRaises(QueueError):
            queue.admit(request(1, tenant="t1"))

    def test_starting_a_request_that_is_not_waiting_is_refused(self):
        queue = TenantQueue("t0")
        with self.assertRaises(QueueError):
            queue.start(99)

    def test_suspending_a_request_that_is_not_in_flight_is_refused(self):
        queue = TenantQueue("t0")
        queue.admit(request(1))
        with self.assertRaises(QueueError):
            queue.suspend(1)

    def test_a_dropped_request_is_not_a_finished_one(self):
        """Rejection and completion must be distinguishable, or an
        admission-control rejection reads as service delivered."""
        queue = TenantQueue("t0")
        queue.admit(request(1))
        queue.drop(1, "infeasible")
        self.assertEqual(queue.backlog, 0)
        self.assertNotIn(1, queue._finished)


class TenantsAreOfferedFairly(unittest.TestCase):
    def test_one_candidate_per_tenant(self):
        """Offering several from one tenant would let the between-tenant
        policy choose within a tenant, which is the inner discipline's
        job."""
        registry = TenantRegistry()
        for rid in (1, 2, 3):
            registry.admit(request(rid, tenant="a", arrival=0.0))
        registry.admit(request(9, tenant="b", arrival=0.0))
        offered = registry.ready(now=5.0)
        self.assertEqual(len(offered), 2)
        self.assertEqual({t for t, _ in offered}, {"a", "b"})

    def test_the_offer_order_rotates(self):
        """A tie-break repeated every round is a bias, not a tie-break."""
        registry = TenantRegistry()
        registry.admit(request(1, tenant="a", arrival=0.0))
        registry.admit(request(2, tenant="b", arrival=0.0))
        first = [t for t, _ in registry.ready(now=5.0)]
        registry.rotate()
        second = [t for t, _ in registry.ready(now=5.0)]
        self.assertNotEqual(first, second)
        self.assertEqual(set(first), set(second))

    def test_the_inner_order_does_not_change_what_a_tenant_is_offered(self):
        """The separation, stated as a test.

        Changing a tenant's discipline changes which of its own requests
        it offers, and changes nothing about the other tenant.
        """
        registry = TenantRegistry(discipline=Discipline.FCFS)
        registry.admit(request(1, tenant="a", arrival=0.0, deadline=50.0))
        registry.admit(request(2, tenant="a", arrival=1.0, deadline=10.0))
        registry.admit(request(3, tenant="b", arrival=0.0))
        before = dict(registry.ready(now=5.0))
        registry.queue_for("a").discipline = Discipline.EDF
        after = dict(registry.ready(now=5.0))
        self.assertEqual(before["a"].request_id, 1)
        self.assertEqual(after["a"].request_id, 2)
        self.assertEqual(before["b"], after["b"])


class CheckpointsRoundTrip(unittest.TestCase):
    """plan.md asks for full checkpoints; a queue that cannot be restored
    exactly makes the runtime's crash recovery a different scheduler."""

    def test_a_registry_survives_a_round_trip(self):
        registry = TenantRegistry(discipline=Discipline.EDF)
        registry.admit(request(1, tenant="a", arrival=0.0, deadline=20.0))
        registry.admit(request(2, tenant="a", arrival=1.0))
        registry.admit(request(3, tenant="b", arrival=2.0, deadline=5.0))
        registry.queue_for("a").start(1)
        registry.rotate()

        restored = TenantRegistry.restore(registry.checkpoint())
        self.assertEqual(list(restored.tenants()), list(registry.tenants()))
        self.assertEqual(restored.backlogged(), registry.backlogged())
        self.assertEqual(
            [(t, r.request_id) for t, r in restored.ready(now=10.0)],
            [(t, r.request_id) for t, r in registry.ready(now=10.0)],
        )

    def test_in_flight_state_survives(self):
        """A request mid-step must come back in flight, not waiting, or
        recovery would run it twice."""
        registry = TenantRegistry()
        registry.admit(request(1, tenant="a", arrival=0.0))
        registry.queue_for("a").start(1)
        restored = TenantRegistry.restore(registry.checkpoint())
        self.assertEqual(len(restored.queue_for("a").in_flight), 1)
        self.assertEqual(restored.queue_for("a").ready(now=10.0), [])

    def test_the_discipline_survives(self):
        registry = TenantRegistry(discipline=Discipline.EDF)
        registry.admit(request(1, tenant="a", arrival=0.0))
        restored = TenantRegistry.restore(registry.checkpoint())
        self.assertIs(restored.queue_for("a").discipline, Discipline.EDF)


if __name__ == "__main__":
    unittest.main()
