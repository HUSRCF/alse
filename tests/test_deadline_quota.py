"""The first policy here that can issue an asymmetric grant.

step_matched_pairing and deadline_aware both return either an even split
or the whole die -- an action space of exactly two -- while 1.5 measures
five splits that all Pareto-dominate rotation. These tests pin that
deadline_quota actually reaches the other four, and that it picks the
smallest quota that still makes the deadline rather than the largest.
"""

import unittest
from dataclasses import dataclass, field

from burstserve.policies import (
    deadline_quota, step_matched_pairing, deadline_aware,
)


@dataclass
class FakeRequest:
    request_id: int
    tenant: str
    deadline_s: float | None
    steps: int = 8
    model: str = "sdxl"
    arrival_s: float = 0.0


@dataclass
class FakeState:
    request: FakeRequest
    steps_done: int = 0
    predicted_step_seconds: dict = field(default_factory=dict)
    tenant_quota_seconds: float = 0.0


# A step cost that falls with quota, as the real cost model does.
def costs(base: float) -> dict:
    return {u: base * 32.0 / u for u in (4, 8, 12, 16, 20, 24, 28, 32)}


def urgent(rid, deadline, steps=8, base=0.1, done=0):
    return FakeState(FakeRequest(rid, "urgent", deadline, steps=steps),
                     steps_done=done, predicted_step_seconds=costs(base))


def video(rid, base=0.5):
    return FakeState(FakeRequest(rid, "video", None, steps=30),
                     predicted_step_seconds=costs(base))


class DeadlineQuotaTest(unittest.TestCase):

    def test_reaches_the_smallest_split_when_the_deadline_is_loose(self):
        # 8 steps at 4 units cost 8 x 0.8 = 6.4 s; a 100 s deadline is met.
        grant = deadline_quota([urgent(1, 100.0), video(2)], 32, 0.0)
        self.assertEqual(grant, {1: 4, 2: 28})

    def test_climbs_only_as_far_as_the_deadline_requires(self):
        # 4 units: 6.4 s. 8 units: 3.2 s. A 4 s deadline needs 8, not 16.
        grant = deadline_quota([urgent(1, 4.0), video(2)], 32, 0.0)
        self.assertEqual(grant, {1: 8, 2: 24})

    def test_reaches_an_asymmetric_split_the_two_action_policies_cannot(self):
        states = [urgent(1, 1.5), video(2)]
        grant = deadline_quota(states, 32, 0.0)
        self.assertEqual(grant, {1: 24, 2: 8})
        # The existing policies cannot produce this grant at all.
        for other in (step_matched_pairing, deadline_aware):
            g = other(states, 32, 0.0)
            self.assertIn(sorted(g.values()), ([16, 16], [32]),
                          f"{other.__name__} produced {g}")

    def test_whole_die_when_no_split_makes_it(self):
        # 32 units: 8 x 0.1 = 0.8 s. A 0.5 s deadline is unmeetable.
        grant = deadline_quota([urgent(1, 0.5), video(2)], 32, 0.0)
        self.assertEqual(grant, {1: 32})

    def test_progress_moves_the_choice(self):
        loose = deadline_quota([urgent(1, 4.0, done=6), video(2)], 32, 0.0)
        self.assertEqual(loose, {1: 4, 2: 28})   # 2 steps left, 4 units is enough

    def test_grants_are_disjoint_and_exhaust_the_die(self):
        for deadline in (0.4, 1.0, 2.0, 4.0, 8.0, 100.0):
            grant = deadline_quota([urgent(1, deadline), video(2)], 32, 0.0)
            self.assertEqual(sum(grant.values()), 32, deadline)
            self.assertTrue(all(v >= 1 for v in grant.values()))

    def test_earliest_deadline_is_the_critical_one(self):
        grant = deadline_quota([urgent(1, 90.0), urgent(2, 4.0)], 32, 0.0)
        self.assertEqual(grant[2], 8)

    def test_no_deadline_anywhere_falls_back_to_an_even_split(self):
        grant = deadline_quota([video(1), video(2)], 32, 0.0)
        self.assertEqual(grant, {1: 16, 2: 16})

    def test_single_request_gets_the_whole_die(self):
        self.assertEqual(deadline_quota([urgent(1, 4.0)], 32, 0.0), {1: 32})

    def test_empty(self):
        self.assertEqual(deadline_quota([], 32, 0.0), {})

    def test_action_space_is_six_not_two(self):
        """Recorded as the *urgent* tenant's quota, not as a sorted pair.

        A sorted pair cannot tell 8+24 from 24+8, which is exactly the
        distinction this policy exists to make.
        """
        seen = {}
        for deadline in (100.0, 5.0, 2.0, 1.2, 1.0, 0.5):
            g = deadline_quota([urgent(1, deadline), video(2)], 32, 0.0)
            seen[deadline] = g[1]
        self.assertEqual(sorted(set(seen.values())), [4, 8, 16, 24, 28, 32],
                         seen)
        # And the two-action policies reach none of the asymmetric ones.
        two_action = set()
        for deadline in (100.0, 5.0, 2.0, 1.2, 1.0, 0.5):
            states = [urgent(1, deadline), video(2)]
            for other in (step_matched_pairing, deadline_aware):
                two_action.add(other(states, 32, 0.0).get(1))
        self.assertTrue(two_action <= {16, 32, None}, two_action)

    def test_it_is_a_method_not_a_baseline(self):
        from burstserve.matrix_results import (
            BASELINE_POLICIES, METHOD_POLICIES,
        )
        self.assertIn("deadline_quota", METHOD_POLICIES)
        self.assertNotIn("deadline_quota", BASELINE_POLICIES)


if __name__ == "__main__":
    unittest.main()
