"""The step-level co-run table, and what it does to 3.8.

Measured 2026-09-04 after the whole-call harness was shown to charge
`hipMalloc`-class device drains as contention. These pin the finding that
matters: the drains fall on the NARROW slice, and the entry that paces
gfx90a's best split is a narrow-slice entry.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from burstserve.trace_sim import (  # noqa: E402
    EXTERNALITY_TABLES_BY_SOURCE,
    MEASURED_EXTERNALITY_GFX90A,
    MEASURED_EXTERNALITY_GFX90A_STEPS,
    externality,
)


class TheTwoHarnessesDisagreeOnNarrowSlicesOnlyTest(unittest.TestCase):
    """Wide slices agree; narrow slices are out by a quarter and more."""

    WIDE = ((78, 26), (91, 13))
    NARROW = ((13, 91), (26, 78))

    def test_wide_slices_agree(self):
        for key in self.WIDE:
            with self.subTest(key=key):
                self.assertAlmostEqual(
                    MEASURED_EXTERNALITY_GFX90A_STEPS[key],
                    MEASURED_EXTERNALITY_GFX90A[key], delta=0.02)

    def test_narrow_slices_do_not(self):
        for key in self.NARROW:
            with self.subTest(key=key):
                self.assertGreater(
                    MEASURED_EXTERNALITY_GFX90A[key]
                    - MEASURED_EXTERNALITY_GFX90A_STEPS[key], 0.25)

    def test_the_narrow_step_level_entries_are_no_penalty_at_all(self):
        for key in self.NARROW:
            with self.subTest(key=key):
                self.assertAlmostEqual(
                    MEASURED_EXTERNALITY_GFX90A_STEPS[key], 1.0, delta=0.005)

    def test_the_even_split_is_the_one_place_they_nearly_agree(self):
        # 1.1954 and 1.2012 over two runs against a call-level 1.2176.
        self.assertAlmostEqual(MEASURED_EXTERNALITY_GFX90A_STEPS[(52, 52)],
                               MEASURED_EXTERNALITY_GFX90A[(52, 52)],
                               delta=0.02)


class TheSourceSelectorTest(unittest.TestCase):

    def test_calls_is_the_default_so_every_prior_caller_is_unchanged(self):
        self.assertEqual(externality(26, 78, device="gfx90a"),
                         MEASURED_EXTERNALITY_GFX90A[(26, 78)])

    def test_gfx1201_has_no_step_level_table_and_says_so(self):
        with self.assertRaises(KeyError) as caught:
            externality(16, 16, device="gfx1201", source="steps")
        self.assertIn("gfx90a", str(caught.exception))

    def test_an_unknown_source_raises_rather_than_falling_back(self):
        with self.assertRaises(KeyError):
            externality(26, 78, device="gfx90a", source="guess")

    def test_the_per_model_correction_is_call_level_and_stays_there(self):
        # It is a whole-call measurement; applying it to a step-level
        # lookup would mix the two quantities silently.
        self.assertEqual(
            externality(16, 16, model="cogvideox-2b", device="gfx1201"),
            1.2891)

    def test_both_sources_are_registered(self):
        self.assertEqual(sorted(EXTERNALITY_TABLES_BY_SOURCE),
                         ["calls", "steps"])


class WhatItDoesToTheBurstArithmeticTest(unittest.TestCase):
    """3.8's rows, as the program prints them, from each table."""

    def best(self, *flags):
        out = subprocess.run(
            [sys.executable, str(REPO / "scripts" / "burst_feasibility.py"),
             *flags],
            capture_output=True, text=True, check=True).stdout
        line = [l for l in out.splitlines() if "best partitioned" in l][-1]
        return line

    def test_gfx1201_misses_even_at_the_floor(self):
        # No co-run table can go below a solo, so gfx1201's half of 3.8
        # is harness-independent.
        self.assertIn("6.30 s, +13.6%", self.best("--device", "gfx1201"))

    def test_gfx90a_is_at_its_floor_under_the_step_level_table(self):
        floor = self.best("--device", "gfx90a")
        steps = self.best("--device", "gfx90a", "--externality",
                          "--externality-source", "steps")
        self.assertIn("5.81 s, +1.2%", floor)
        self.assertIn("5.81 s, +1.1%", steps)

    def test_the_call_level_table_put_gfx90a_at_29_percent(self):
        self.assertIn("7.42 s, +29.2%",
                      self.best("--device", "gfx90a", "--externality"))

    def test_exclusive_still_beats_every_split_by_far(self):
        for device, exclusive in (("gfx1201", "3.70 s"), ("gfx90a", "3.83 s")):
            with self.subTest(device=device):
                self.assertIn(f"exclusive {exclusive}",
                              self.best("--device", device))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
