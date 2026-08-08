"""The scheduler action that survives not knowing why pairings degrade.

The same 16+16 pairing, measured 44 times, lands in one of two states:
fast (+21.8 to +26.4% per-step penalty, cv 0.25%) or slow (+72.9 to
+83.4%, cv 1-3%), drawn at roughly 30% fast. Four causes were proposed
and retracted -- working set, power cap, uncontrollable bistability,
launch stagger -- before ten runs at one fixed setting showed the state
is simply drawn per run.

No hardware quantity measured so far predicts the draw. What the
scheduler can do instead is notice: the states are 46% apart and each is
internally tight, so one step distinguishes them.

**What this file covers, and what it no longer claims.** The header here
used to say the bistability belonged to the two-process measurement
harness rather than to the die, on 17 in-process trials that all came out
fast. Those were whole-``pipeline(...)`` calls. Measured per step, in the
one-process two-mask arrangement the runtime uses, two of eight processes
latched slow -- so the die has it, and the call-level harness was seeing
the VAE decode dilute it.

The unlatched model these tests run under is therefore not "the harness's
artefact"; it is the model in which **re-forming a pairing draws again**,
which is the assumption ``probing_partitioning`` is built on. Keeping it
tested here is what makes the frozen policy a usable comparison. The
measured alternative -- draw keyed by mask pair, no redraw -- lives in
``test_latched_pairing``, together with the policy that does not assume
the redraw.
"""

from __future__ import annotations

import statistics
import sys
import unittest

sys.dont_write_bytecode = True

from burstserve.policies import (
    exclusive_fcfs,
    probing_partitioning,
    slo_aware_partitioning,
)
from burstserve.trace_sim import (
    FAST_PAIRING_EXTERNALITY,
    PAIRING_STATE_RATE,
    SLOW_PAIRING_EXTERNALITY,
    PairingStates,
    Request,
    Trace,
    simulate,
)

SEEDS = range(12)


def backlogged() -> Trace:
    return Trace([
        Request(request_id=i, tenant=f"t{i % 2}", model="sdxl",
                arrival_s=0.0, steps=40)
        for i in range(6)
    ])


def utilisations(policy) -> list[float]:
    return [
        simulate(backlogged(), policy, horizon_s=1200.0,
                 pairing_states=PairingStates(seed=s, enabled=True)
                 ).utilisation()
        for s in SEEDS
    ]


class ProbingBeatsBlindPairing(unittest.TestCase):
    def test_blind_pairing_loses_to_the_whole_die(self):
        """Which is the problem the probe exists to solve.

        If this ever passes, the bistability has gone away and the probe
        is unnecessary -- so it is asserted rather than assumed.
        """
        blind = statistics.mean(utilisations(slo_aware_partitioning))
        self.assertLess(blind, 1.0)

    def test_probing_beats_the_whole_die(self):
        probing = statistics.mean(utilisations(probing_partitioning))
        self.assertGreater(probing, 1.05)

    def test_probing_beats_blind_pairing_on_every_seed(self):
        blind = utilisations(slo_aware_partitioning)
        probing = utilisations(probing_partitioning)
        for seed, (b, p) in enumerate(zip(blind, probing)):
            with self.subTest(seed=seed):
                self.assertGreater(p, b)

    def test_probing_is_steadier_than_blind_pairing(self):
        """A policy at the mercy of the draw has a wide spread.

        Probing narrows it because it stops accepting bad draws, which is
        worth as much to a scheduler as the mean.
        """
        blind = utilisations(slo_aware_partitioning)
        probing = utilisations(probing_partitioning)
        self.assertLess(max(probing) - min(probing), max(blind) - min(blind))

    def test_it_still_holds_the_lag_bound(self):
        for seed in SEEDS:
            with self.subTest(seed=seed):
                result = simulate(
                    backlogged(), probing_partitioning, horizon_s=1200.0,
                    pairing_states=PairingStates(seed=seed, enabled=True))
                self.assertLessEqual(result.peak_service_lag_quanta(), 2.0)


class TheModelMatchesTheMeasurement(unittest.TestCase):
    def test_the_two_states_are_the_measured_ones(self):
        self.assertAlmostEqual(FAST_PAIRING_EXTERNALITY, 1.2367, places=4)
        self.assertAlmostEqual(SLOW_PAIRING_EXTERNALITY, 1.7866, places=4)
        self.assertAlmostEqual(PAIRING_STATE_RATE, 0.30, places=2)

    def test_the_states_are_far_enough_apart_to_detect(self):
        """The probe's threshold sits between them and is fitted to
        neither."""
        separation = SLOW_PAIRING_EXTERNALITY / FAST_PAIRING_EXTERNALITY
        self.assertGreater(separation, 1.4)

    def test_re_forming_a_pairing_draws_again(self):
        """Without this the probe would be pointless -- it could detect a
        bad pairing and never escape it."""
        states = PairingStates(seed=3, enabled=True)
        members = frozenset({0, 1})
        draws = set()
        for _ in range(40):
            draws.add(states.factor_for(members))
            states.forget(members)
        self.assertEqual(len(draws), 2)

    def test_holding_a_pairing_does_not_redraw(self):
        """And with this, a policy cannot launder a slow pairing by simply
        asking again."""
        states = PairingStates(seed=3, enabled=True)
        members = frozenset({0, 1})
        first = states.factor_for(members)
        for _ in range(20):
            self.assertEqual(states.factor_for(members), first)

    def test_a_solo_request_has_no_pairing_penalty(self):
        states = PairingStates(seed=3, enabled=True)
        self.assertEqual(states.factor_for(frozenset({0})),
                         FAST_PAIRING_EXTERNALITY)


class ProbingIsHarmlessWithoutBistability(unittest.TestCase):
    """If the hardware stopped doing this, the probe must not cost anything.

    A scheduler action justified by a measurement has to degrade to a
    no-op when the measurement no longer holds, or it becomes a permanent
    tax paid for a problem that has gone.
    """

    def test_it_matches_blind_pairing_when_every_draw_is_fast(self):
        """Which is the arrangement the runtime actually uses.

        17 in-process trials, all fast. The probe must therefore be free
        there, or it is a permanent tax for a problem the target
        architecture does not have.
        """
        def run(policy):
            return simulate(
                backlogged(), policy, horizon_s=1200.0,
                pairing_states=PairingStates(seed=0, enabled=False),
            ).utilisation()

        self.assertAlmostEqual(run(probing_partitioning),
                               run(slo_aware_partitioning), places=6)

    def test_and_still_beats_the_whole_die_there(self):
        probing = simulate(
            backlogged(), probing_partitioning, horizon_s=1200.0,
            pairing_states=PairingStates(seed=0, enabled=False),
        ).utilisation()
        whole = simulate(
            backlogged(), exclusive_fcfs, horizon_s=1200.0,
            pairing_states=PairingStates(seed=0, enabled=False),
        ).utilisation()
        self.assertGreater(probing / whole, 1.15)


if __name__ == "__main__":
    unittest.main()
