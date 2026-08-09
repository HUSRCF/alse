"""The per-step, per-side co-run table, and what it deliberately omits.

The width-keyed ``MEASURED_EXTERNALITY`` is call-level -- it was measured
across whole ``pipeline(...)`` calls, VAE decode included -- and applying
it to a per-step decision is the error that produced this project's
retracted +18.6%. The per-step table replaces it for the pairings that
have been measured that way, and is keyed by both models because a
mismatched pairing does not penalise its two tenants equally.

What the table cannot hold, and what these tests pin down so nobody
later reads it as the whole story:

  * the first two co-run episodes of a mismatched pairing, which run
    serialised at 1.89x to 26.3x depending on the split;
  * the self-paired bistability, where the die draws 1.176 or 1.776 per
    mask pair and latches it.

Both are handled by measuring at runtime rather than by looking up, which
is the design the dual ledger exists for.
"""

from __future__ import annotations

import sys
import unittest

sys.dont_write_bytecode = True

from burstserve.trace_sim import (
    MEASURED_EXTERNALITY,
    MEASURED_MISMATCHED_SETTLE_EPISODES,
    MEASURED_STEP_PAIR_EXTERNALITY,
    MEASURED_STEP_PAIRING_FAST,
    MEASURED_STEP_PAIRING_SLOW,
    step_pair_externality,
)


class TheTableIsPerSideAndPerModelPair(unittest.TestCase):
    def test_the_two_sides_of_a_pairing_differ(self):
        """Averaging them would hide who pays."""
        mine = step_pair_externality("sdxl", 16, "cogvideox-2b", 16)
        theirs = step_pair_externality("cogvideox-2b", 16, "sdxl", 16)
        self.assertNotEqual(mine, theirs)

    def test_every_mismatched_pairing_has_both_sides(self):
        pairs = {(m, u, p, v) for (m, u, p, v)
                 in MEASURED_STEP_PAIR_EXTERNALITY if m != p}
        for model, units, peer, peer_units in pairs:
            with self.subTest(pair=(model, units, peer, peer_units)):
                self.assertIn((peer, peer_units, model, units),
                              MEASURED_STEP_PAIR_EXTERNALITY,
                              "a one-sided entry is a pairing half measured")

    def test_the_settled_mismatched_factors_are_near_one(self):
        """Both tenants at very nearly solo speed is the whole result."""
        for key, factor in MEASURED_STEP_PAIR_EXTERNALITY.items():
            if key[0] == key[2]:
                continue
            with self.subTest(key=key):
                self.assertLess(abs(factor - 1.0), 0.1)

    def test_an_unmeasured_pairing_returns_none(self):
        """Not the width-only table, which measures something else."""
        self.assertIsNone(
            step_pair_externality("sdxl", 12, "cogvideox-2b", 20))

    def test_it_does_not_fall_back_to_the_call_level_table(self):
        """The substitution would be the granularity error, again."""
        self.assertIn((16, 16), MEASURED_EXTERNALITY)
        self.assertIsNone(step_pair_externality("sdxl", 16, "unknown", 16))


class WhatTheTableCannotHold(unittest.TestCase):
    def test_the_self_paired_entry_is_the_fast_state_only(self):
        """A single number cannot represent a draw.

        The entry exists so a lookup does not fail, and it carries the
        state the die takes six times in eight. The other state is 51%
        worse and no lookup can say which one this process got -- which is
        why the scheduler measures.
        """
        self.assertEqual(step_pair_externality("sdxl", 16, "sdxl", 16),
                         MEASURED_STEP_PAIRING_FAST)
        self.assertGreater(MEASURED_STEP_PAIRING_SLOW,
                           MEASURED_STEP_PAIRING_FAST * 1.4)

    def test_the_transient_is_recorded_beside_the_table(self):
        """1.89x to 26.3x for two episodes is the largest effect measured.

        A module that held the settled factors and not this would read as
        if partitioning were free from the first round.
        """
        self.assertEqual(MEASURED_MISMATCHED_SETTLE_EPISODES, 2)

    def test_the_settled_factors_do_not_describe_the_transient(self):
        """Stated as an assertion so it cannot be forgotten quietly.

        Measured: SDXL at 28 units beside CogVideoX at 4 runs at 26.3x
        for two episodes and 1.006 after. No entry keyed on widths can be
        both.
        """
        settled = step_pair_externality("sdxl", 28, "cogvideox-2b", 4)
        transient = 26.3
        self.assertGreater(transient / settled, 20)


if __name__ == "__main__":
    unittest.main()
