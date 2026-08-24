"""The static-partition baseline that decides whether the scheduler counts.

Two properties are worth a test rather than a reading. The first is an
identity: a 16-unit split has to be ``static_even`` on every input, so a
disagreement anywhere in the sweep is a bug and not a result. The second
is that the knob points the way it reads -- a larger ``urgent_units``
must hand the urgent tenant more of the die, whichever order the two
requests arrive in. Getting that backwards would invert the whole
experiment while every number still looked plausible.
"""

import unittest

from burstserve.policies import POLICY_FACTORIES, make_fixed_split, static_even
from burstserve.trace_sim import Request, RequestState


def state(request_id: int, tenant: str) -> RequestState:
    return RequestState(request=Request(
        request_id=request_id, tenant=tenant, model="m",
        arrival_s=0.0, steps=8))


class FixedSplitTest(unittest.TestCase):

    def test_sixteen_is_static_even(self) -> None:
        policy = make_fixed_split(16)
        cases = [
            [],
            [state(0, "urgent")],
            [state(0, "video")],
            [state(0, "urgent"), state(1, "video")],
            [state(0, "video"), state(1, "urgent")],
            [state(0, "urgent"), state(1, "urgent")],
            [state(0, "video"), state(1, "video")],
            [state(0, "urgent"), state(1, "video"), state(2, "urgent")],
        ]
        for states in cases:
            with self.subTest(n=len(states)):
                self.assertEqual(policy(states, 32, 0.0),
                                 static_even(states, 32, 0.0))

    def test_knob_direction(self) -> None:
        """More units for urgent means more units for urgent."""
        for order in (("urgent", "video"), ("video", "urgent")):
            states = [state(0, order[0]), state(1, order[1])]
            urgent_id = 0 if order[0] == "urgent" else 1
            granted = [make_fixed_split(u)(states, 32, 0.0)[urgent_id]
                       for u in (4, 8, 16, 24, 28)]
            with self.subTest(order=order):
                self.assertEqual(granted, [4, 8, 16, 24, 28])
                self.assertEqual(granted, sorted(granted))

    def test_whole_die_when_alone(self) -> None:
        for u in (4, 16, 28):
            for tenant in ("urgent", "video"):
                grant = make_fixed_split(u)([state(0, tenant)], 32, 0.0)
                self.assertEqual(grant, {0: 32})

    def test_split_is_exact_and_disjoint(self) -> None:
        states = [state(0, "urgent"), state(1, "video")]
        for u in (4, 8, 16, 24, 28):
            grant = make_fixed_split(u)(states, 32, 0.0)
            self.assertEqual(sum(grant.values()), 32)
            self.assertEqual(len(grant), 2)

    def test_factories_registered(self) -> None:
        for u in (4, 8, 16, 24, 28):
            policy = POLICY_FACTORIES[f"fixed_split_{u}"]()
            states = [state(0, "urgent"), state(1, "video")]
            self.assertEqual(policy(states, 32, 0.0)[0], u)

    def test_degenerate_splits_rejected(self) -> None:
        for u in (0, 32, -4, 40):
            with self.assertRaises(ValueError):
                make_fixed_split(u)


if __name__ == "__main__":
    unittest.main()
