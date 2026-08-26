"""The baseline the comparator set never contained.

``exclusive_fcfs`` was read as the no-partitioning floor for the whole
project. It is not priority: the registry rotates every round, so it
hands the whole die to the batch tenant every other round. These tests
pin the difference so the two are never conflated again.
"""

import unittest
from dataclasses import dataclass

from burstserve.policies import exclusive_fcfs, exclusive_priority


@dataclass(frozen=True)
class FakeRequest:
    request_id: int
    tenant: str
    deadline_s: float | None
    steps: int = 8


@dataclass(frozen=True)
class FakeState:
    request: FakeRequest
    steps_done: int = 0
    predicted_step_seconds: dict | None = None


def urgent(rid, deadline):
    return FakeState(FakeRequest(rid, "urgent", deadline))


def video(rid):
    return FakeState(FakeRequest(rid, "video", None))


class ExclusivePriorityTest(unittest.TestCase):

    def test_whole_die_to_the_deadline_carrying_request(self):
        grant = exclusive_priority([video(1), urgent(2, 5.0)], 32, 0.0)
        self.assertEqual(grant, {2: 32})

    def test_order_does_not_decide_it(self):
        """The whole point: rotation must not hand the die to the batch
        tenant. Same two requests, opposite order, same grant."""
        a = exclusive_priority([video(1), urgent(2, 5.0)], 32, 0.0)
        b = exclusive_priority([urgent(2, 5.0), video(1)], 32, 0.0)
        self.assertEqual(a, b)

    def test_exclusive_fcfs_does_the_opposite(self):
        """Documents what the old floor actually is."""
        first = exclusive_fcfs([video(1), urgent(2, 5.0)], 32, 0.0)
        second = exclusive_fcfs([urgent(2, 5.0), video(1)], 32, 0.0)
        self.assertEqual(first, {1: 32})
        self.assertEqual(second, {2: 32})
        self.assertNotEqual(first, second)

    def test_earliest_deadline_wins_inside_the_critical_class(self):
        grant = exclusive_priority([urgent(1, 9.0), urgent(2, 4.0)], 32, 0.0)
        self.assertEqual(grant, {2: 32})

    def test_ties_broken_by_request_id_so_it_is_deterministic(self):
        grant = exclusive_priority([urgent(7, 4.0), urgent(3, 4.0)], 32, 0.0)
        self.assertEqual(grant, {3: 32})

    def test_batch_tenant_gets_the_die_when_nothing_is_critical(self):
        grant = exclusive_priority([video(1), video(2)], 32, 0.0)
        self.assertEqual(grant, {1: 32})

    def test_never_partitions(self):
        for states in ([urgent(1, 5.0), video(2)],
                       [video(1), video(2)],
                       [urgent(1, 5.0), urgent(2, 6.0), video(3)]):
            grant = exclusive_priority(states, 32, 0.0)
            self.assertEqual(len(grant), 1)
            self.assertEqual(sum(grant.values()), 32)

    def test_empty(self):
        self.assertEqual(exclusive_priority([], 32, 0.0), {})

    def test_it_is_a_baseline_not_a_method(self):
        from burstserve.matrix_results import (
            BASELINE_POLICIES, METHOD_POLICIES,
        )
        self.assertIn("exclusive_priority", BASELINE_POLICIES)
        self.assertNotIn("exclusive_priority", METHOD_POLICIES)


if __name__ == "__main__":
    unittest.main()
