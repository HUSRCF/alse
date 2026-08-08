"""The die's bistability latches, and the frozen probe assumes it does not.

``test_pairing_probe`` exercises the two-process behaviour: a pairing
draws fast or slow, and re-forming it draws again. That model came from
whole-``pipeline(...)`` measurements, and it is the reason
``probing_partitioning`` drops a slow pairing for exactly one round --
"re-forming it next round is a fresh draw".

Measured per step, in the arrangement the runtime actually uses (one
process, two disjoint CU masks), the die says otherwise. Eight processes
ran twelve co-run episodes each:

  * six latched fast at 1.176 (n=54, 1.163-1.202), two slow at 1.776
    (n=18, 1.732-1.850) -- so the bistability is the die's, not the
    harness's;
  * none flipped, across nine later episodes each, though every episode
    built fresh adapters and re-acquired its streams. Re-forming does not
    redraw;
  * interposing an 8+24 pairing never disturbed the 16+16 state --
    sixteen before/after checks, sixteen unchanged -- and 8+24 carried
    its own independent state. The draw is keyed by the mask pair;
  * each episode's first step matched its own median to 0.00% over 72
    episodes. One step identifies the state.

These tests cover both halves of that: ``PairingStates(latched=True)``
models it, and ``make_sticky_probing_partitioning`` is the policy that
does not assume the redraw. The frozen policy is left alone and compared
against, which is what a frozen baseline is for.
"""

from __future__ import annotations

import statistics
import sys
import unittest
from dataclasses import replace

sys.dont_write_bytecode = True

from burstserve.policies import (
    exclusive_fcfs,
    make_sticky_probing_partitioning,
    probing_partitioning,
    slo_aware_partitioning,
)
from burstserve.trace_sim import (
    MEASURED_STEP_PAIRING_FAST,
    MEASURED_STEP_PAIRING_SLOW,
    PairingStates,
    Request,
    RequestState,
    Trace,
    simulate,
)

SEEDS = range(12)


def slow_pairing_states() -> list[RequestState]:
    """Two backlogged requests whose last step read as the slow state.

    Solo prediction 0.163 s at 16 units, observed 0.29 s -- the plateau
    the die actually produced (1.776x) rather than a number chosen to
    clear the 1.608x threshold by a comfortable margin.
    """
    return [
        RequestState(
            request=Request(request_id=i, tenant=f"t{i}", model="sdxl",
                            arrival_s=0.0, steps=200),
            steps_done=5,
            predicted_step_seconds={4: 0.521, 8: 0.283, 16: 0.163,
                                    24: 0.124, 32: 0.109},
            observed_step_seconds=0.29,
            observed_at_units=16,
        )
        for i in range(2)
    ]


def backlogged() -> Trace:
    return Trace([
        Request(request_id=i, tenant=f"t{i % 2}", model="sdxl",
                arrival_s=0.0, steps=40)
        for i in range(6)
    ])


def utilisations(policy_or_factory, *, calls_factory: bool = False):
    out = []
    for seed in SEEDS:
        policy = (policy_or_factory() if calls_factory else policy_or_factory)
        out.append(simulate(
            backlogged(), policy, horizon_s=1200.0,
            pairing_states=PairingStates(seed=seed, enabled=True,
                                         latched=True),
        ).utilisation())
    return out


class LatchedStatesModelTheDie(unittest.TestCase):
    def test_it_uses_the_per_step_measurements(self):
        states = PairingStates(seed=0, enabled=True, latched=True)
        self.assertEqual(states.fast, MEASURED_STEP_PAIRING_FAST)
        self.assertEqual(states.slow, MEASURED_STEP_PAIRING_SLOW)

    def test_unlatched_still_uses_the_call_level_pair(self):
        """The old model is not silently repointed at the new numbers.

        test_pairing_probe asserts against the two-process behaviour and
        must keep measuring it.
        """
        states = PairingStates(seed=0, enabled=True)
        self.assertNotEqual(states.fast, MEASURED_STEP_PAIRING_FAST)

    def test_the_draw_is_keyed_by_the_mask_pair(self):
        """Different requests, same widths -- one state, as measured."""
        states = PairingStates(seed=3, enabled=True, latched=True)
        first = states.factor_for(frozenset({1, 2}), quotas=[16, 16])
        # Different request ids entirely; the hardware does not know them.
        second = states.factor_for(frozenset({7, 9}), quotas=[16, 16])
        self.assertEqual(first, second)

    def test_a_different_mask_pair_draws_independently(self):
        """8+24 carried its own state while 16+16 kept its own."""
        drawn = set()
        for seed in range(40):
            states = PairingStates(seed=seed, enabled=True, latched=True)
            drawn.add((
                states.factor_for(frozenset({1, 2}), quotas=[16, 16]),
                states.factor_for(frozenset({3, 4}), quotas=[8, 24]),
            ))
        # If the two pairs shared a state only the two diagonal
        # combinations would ever appear.
        self.assertGreater(len(drawn), 2)

    def test_re_forming_does_not_redraw(self):
        """0 of 8 processes flipped across nine re-forms each."""
        states = PairingStates(seed=5, enabled=True, latched=True)
        members = frozenset({1, 2})
        before = states.factor_for(members, quotas=[16, 16])
        for _ in range(20):
            states.forget(members)
            self.assertEqual(states.factor_for(members, quotas=[16, 16]),
                             before)

    def test_interposing_another_pair_does_not_disturb_it(self):
        """Sixteen before/after checks on the die, sixteen unchanged."""
        states = PairingStates(seed=11, enabled=True, latched=True)
        before = states.factor_for(frozenset({1, 2}), quotas=[16, 16])
        states.factor_for(frozenset({3, 4}), quotas=[8, 24])
        after = states.factor_for(frozenset({1, 2}), quotas=[16, 16])
        self.assertEqual(before, after)

    def test_both_states_are_reachable(self):
        seen = {PairingStates(seed=s, enabled=True,
                              latched=True).factor_for(frozenset({1, 2}),
                                                       quotas=[16, 16])
                for s in range(40)}
        self.assertEqual(seen, {MEASURED_STEP_PAIRING_FAST,
                                MEASURED_STEP_PAIRING_SLOW})


class StickyProbeDoesNotPayTwiceForTheSameAnswer(unittest.TestCase):
    def test_each_factory_call_has_its_own_memory(self):
        """Sharing one instance would carry a verdict between runs."""
        a = make_sticky_probing_partitioning()
        b = make_sticky_probing_partitioning()
        a.memory[(16, 16)] = {"backoff": 1.0, "until": 99.0}
        self.assertEqual(b.memory, {})

    def test_it_is_not_in_the_baselines_dict(self):
        """Tests iterate BASELINES; a stateful entry there couples them."""
        from burstserve.policies import BASELINES, POLICY_FACTORIES

        self.assertNotIn("sticky_probing_partitioning", BASELINES)
        self.assertIn("sticky_probing_partitioning", POLICY_FACTORIES)

    def test_it_beats_the_frozen_probe_when_the_state_latches(self):
        """The point of the change, asserted rather than assumed.

        If this ever fails, re-forming has become worth its cost again and
        the sticky variant should be dropped rather than kept.
        """
        frozen = statistics.mean(utilisations(probing_partitioning))
        sticky = statistics.mean(
            utilisations(make_sticky_probing_partitioning, calls_factory=True))
        self.assertGreater(sticky, frozen)

    def test_it_never_loses_to_the_frozen_probe_on_any_seed(self):
        frozen = utilisations(probing_partitioning)
        sticky = utilisations(make_sticky_probing_partitioning,
                              calls_factory=True)
        for seed, (f, s) in enumerate(zip(frozen, sticky)):
            with self.subTest(seed=seed):
                self.assertGreaterEqual(s, f * 0.999)

    def test_it_still_beats_blind_pairing(self):
        blind = statistics.mean(utilisations(slo_aware_partitioning))
        sticky = statistics.mean(
            utilisations(make_sticky_probing_partitioning, calls_factory=True))
        self.assertGreater(sticky, blind)

    def test_it_beats_the_whole_die(self):
        """Otherwise partitioning is not worth doing under a latched die."""
        exclusive = statistics.mean(utilisations(exclusive_fcfs))
        sticky = statistics.mean(
            utilisations(make_sticky_probing_partitioning, calls_factory=True))
        self.assertGreater(sticky, exclusive)

    def test_a_slow_observation_is_remembered_and_stops_the_re_form(self):
        """The whole defect, on the policy rather than on a dict."""
        policy = make_sticky_probing_partitioning(base_backoff_s=4.0)
        slow = slow_pairing_states()

        first = policy(slow, 32, now=0.0)
        self.assertEqual(len(first), 1, "a slow pairing is dropped")
        self.assertEqual(policy.memory[(16, 16)]["backoff"], 4.0)

        # The frozen policy re-forms here, because it believes the next
        # round is a fresh draw. The die says it is the same draw.
        again = policy(slow, 32, now=1.0)
        self.assertEqual(len(again), 1,
                         "still inside the backoff, so still not paired")

    def test_the_frozen_probe_re_forms_where_this_one_does_not(self):
        """Names the difference, so a regression in either shows up here."""
        slow = slow_pairing_states()
        # Given no observation, the frozen policy pairs.
        fresh = [replace(s, observed_step_seconds=None,
                         observed_at_units=None) for s in slow]
        self.assertEqual(len(probing_partitioning(fresh, 32, now=1.0)), 2)
        sticky = make_sticky_probing_partitioning(base_backoff_s=4.0)
        sticky(slow, 32, now=0.0)
        self.assertEqual(len(sticky(fresh, 32, now=1.0)), 1,
                         "the memory outlives the observation that made it")

    def test_the_backoff_expires_so_the_state_can_be_re_probed(self):
        """A permanent verdict would claim more than the evidence does.

        Nothing measured rules out the state changing on a timescale
        longer than the two minutes observed.
        """
        policy = make_sticky_probing_partitioning(base_backoff_s=4.0)
        slow = slow_pairing_states()
        policy(slow, 32, now=0.0)
        fresh = [replace(s, observed_step_seconds=None,
                         observed_at_units=None) for s in slow]
        self.assertEqual(len(policy(fresh, 32, now=3.9)), 1, "still held")
        self.assertEqual(len(policy(fresh, 32, now=4.1)), 2, "re-probed")

    def test_the_backoff_doubles_up_to_its_cap(self):
        policy = make_sticky_probing_partitioning(base_backoff_s=1.0,
                                                  max_backoff_s=4.0)
        slow = slow_pairing_states()
        seen = []
        now = 0.0
        for _ in range(5):
            policy(slow, 32, now=now)
            seen.append(policy.memory[(16, 16)]["backoff"])
            now = policy.memory[(16, 16)]["until"] + 0.01
        self.assertEqual(seen, [1.0, 2.0, 4.0, 4.0, 4.0])

    def test_a_fast_pairing_is_never_remembered(self):
        policy = make_sticky_probing_partitioning()
        fast = [replace(s, observed_step_seconds=0.19) for s in
                slow_pairing_states()]
        self.assertEqual(len(policy(fast, 32, now=0.0)), 2)
        self.assertEqual(policy.memory, {})


if __name__ == "__main__":
    unittest.main()
