"""Feasibility computed the way the round actually runs.

deadline_quota asked `remaining x own_step <= left`, which ignores the
peer. A round is paced by its slowest member, so at 16+16 an urgent
request advances one step per 0.805 s round rather than per its own
0.158 s: a 32-step burst takes 25.8 s, not 5.06 s. The old rule called
16+16 feasible against a 5.34 s deadline. These tests pin the corrected
question and the split it selects.
"""

import unittest
from dataclasses import dataclass, field

from burstserve.policies import make_pipelined_quota, deadline_quota
from burstserve.trace_sim import QuotaCostModel

QUOTAS = (4, 8, 12, 16, 20, 24, 28, 32)


@dataclass
class FakeRequest:
    request_id: int
    tenant: str
    deadline_s: float | None
    steps: int = 32
    model: str = "m"
    arrival_s: float = 0.0


@dataclass
class FakeState:
    request: FakeRequest
    steps_done: int = 0
    predicted_step_seconds: dict = field(default_factory=dict)
    tenant_quota_seconds: float = 0.0


def measured(model):
    cost = QuotaCostModel.for_model(model)
    return {q: cost.step_seconds(q) for q in QUOTAS}


def urgent(deadline, steps=32, done=0):
    return FakeState(FakeRequest(1, "urgent", deadline, steps=steps),
                     steps_done=done, predicted_step_seconds=measured("sdxl"))


def video():
    return FakeState(FakeRequest(2, "video", None, steps=30),
                     predicted_step_seconds=measured("cogvideox-2b"))


class PipelinedQuotaTest(unittest.TestCase):

    def setUp(self):
        self.policy = make_pipelined_quota(16)

    def test_the_workload_deadline_selects_24_plus_8(self):
        """5.34 s is this project's burst deadline on the measured model."""
        self.assertEqual(self.policy([urgent(5.34), video()], 32, 0.0),
                         {1: 24, 2: 8})

    def test_the_old_rule_disagrees_and_is_wrong(self):
        """deadline_quota calls 16+16 feasible at the same deadline.

        Its arithmetic: 32 x 0.158 = 5.06 <= 5.34. The round is paced by
        video's 0.805 s step, so the burst really takes 25.8 s.
        """
        old = deadline_quota([urgent(5.34), video()], 32, 0.0)
        self.assertEqual(old, {1: 16, 2: 16})
        self.assertNotEqual(old, self.policy([urgent(5.34), video()], 32, 0.0))

    def test_it_walks_the_whole_action_space_as_slack_grows(self):
        got = {dl: self.policy([urgent(dl), video()], 32, 0.0)[1]
               for dl in (4.7, 5.34, 6.0, 10.0, 20.0)}
        self.assertEqual(got, {4.7: 32, 5.34: 24, 6.0: 16, 10.0: 8, 20.0: 4})

    def test_a_deadline_no_split_can_meet_gets_the_whole_die(self):
        self.assertEqual(self.policy([urgent(3.0), video()], 32, 0.0), {1: 32})

    def test_a_smaller_cap_loses_24_plus_8(self):
        """The 2x2 ran at a cap of 8. At 24+8 the ratio is 12.6, so a cap
        of 8 gives 8 steps per round and the burst takes 6.3 s, outside
        the deadline. This is the experiment's own design error, pinned.
        """
        self.assertEqual(make_pipelined_quota(8)(
            [urgent(5.34), video()], 32, 0.0), {1: 32})

    def test_progress_relaxes_the_choice(self):
        late = self.policy([urgent(5.34, done=24), video()], 32, 0.0)
        self.assertLess(late[1], 24)

    def test_grants_are_disjoint_and_exhaust_the_die(self):
        for dl in (3.0, 5.34, 6.0, 10.0, 20.0, 100.0):
            g = self.policy([urgent(dl), video()], 32, 0.0)
            self.assertEqual(sum(g.values()), 32, dl)
            self.assertTrue(all(v >= 1 for v in g.values()), dl)

    def test_cap_must_be_at_least_one(self):
        with self.assertRaises(ValueError):
            make_pipelined_quota(0)

    def test_registered_as_a_method(self):
        from burstserve.matrix_results import (
            BASELINE_POLICIES, METHOD_POLICIES,
        )
        from burstserve.policies import BASELINES
        self.assertIn("pipelined_quota", BASELINES)
        self.assertIn("pipelined_quota", METHOD_POLICIES)
        self.assertNotIn("pipelined_quota", BASELINE_POLICIES)

    def test_empty_and_single(self):
        self.assertEqual(self.policy([], 32, 0.0), {})
        self.assertEqual(self.policy([urgent(5.34)], 32, 0.0), {1: 32})


if __name__ == "__main__":
    unittest.main()
