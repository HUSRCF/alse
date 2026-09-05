"""Claim 1.11's corrected table, and the controls that license reading it.

Measured 2026-09-05 after two timing defects were found and fixed: the
adapter's `last_step_seconds` goes stale when the CPU runs ahead, and a
wall clock stopped without a device synchronise measures submission
rather than execution. See docs/claims-and-evidence.md 1.11 and 3.10.
"""

from __future__ import annotations

import json
import statistics
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from burstserve.trace_sim import (  # noqa: E402
    MEASURED_NWAY_PENALTY_GFX1201,
    externality,
)

RUNS = REPO / "experiments" / "probes" / "gfx1201" / "nway_walled"
SLICE = {1: 32, 2: 16, 4: 8, 8: 4}


class TheTableMatchesTheRawRunsTest(unittest.TestCase):
    """A published number that cannot be regenerated is a number on trust."""

    @classmethod
    def setUpClass(cls):
        if not RUNS.is_dir():
            raise unittest.SkipTest(f"{RUNS} not present")
        cls.runs = {}
        for path in RUNS.glob("*.json"):
            d = json.loads(path.read_text())
            cls.runs[d["ways"]] = d

    def mean(self, rows, key):
        return statistics.mean(r[key] for r in rows)

    def test_every_way_in_the_table_has_a_raw_run(self):
        self.assertEqual(sorted(self.runs), sorted(MEASURED_NWAY_PENALTY_GFX1201))

    def test_the_table_is_the_runs(self):
        for ways, value in MEASURED_NWAY_PENALTY_GFX1201.items():
            with self.subTest(ways=ways):
                measured = self.mean(self.runs[ways]["verdict"], "externality")
                self.assertAlmostEqual(measured, value, places=3)

    def test_the_solos_land_on_the_measured_quota_curve(self):
        from burstserve.trace_sim import QuotaCostModel
        model = QuotaCostModel.for_model("sdxl", device="gfx1201")
        for ways, d in self.runs.items():
            with self.subTest(ways=ways):
                solo = self.mean(d["solo_before"], "p50_s")
                # Consistently 3-6% fast at every width: the curve was
                # measured on a colder card. A mask that failed to apply
                # would be off by a factor, not by 5%.
                self.assertAlmostEqual(solo / model.step_seconds(SLICE[ways]),
                                       1.0, delta=0.10)


class TheControlsThatLicenseItTest(unittest.TestCase):
    """A co-run penalty must vanish when the peers stop. This one does."""

    @classmethod
    def setUpClass(cls):
        if not RUNS.is_dir():
            raise unittest.SkipTest(f"{RUNS} not present")
        cls.runs = {json.loads(p.read_text())["ways"]: json.loads(p.read_text())
                    for p in RUNS.glob("*.json")}

    def mean(self, rows):
        return statistics.mean(r["p50_s"] for r in rows)

    def test_the_post_episode_solo_returns_to_the_solo(self):
        for ways, d in self.runs.items():
            with self.subTest(ways=ways):
                self.assertAlmostEqual(
                    self.mean(d["solo_after"]) / self.mean(d["solo_before"]),
                    1.0, delta=0.05)

    def test_dropping_the_allocator_cache_changes_nothing(self):
        # The 2-way cell has one noisy pass at 205 ms against a 153 ms
        # solo, which is why this tolerance is wider than the one above.
        for ways, d in self.runs.items():
            with self.subTest(ways=ways):
                self.assertAlmostEqual(
                    self.mean(d["solo_after_empty_cache"])
                    / self.mean(d["solo_before"]), 1.0, delta=0.20)


class WhatAPairwiseTableWouldMissTest(unittest.TestCase):

    def excess(self, ways):
        width = SLICE[ways]
        return (MEASURED_NWAY_PENALTY_GFX1201[ways]
                / externality(width, 32 - width, device="gfx1201") - 1)

    def test_two_ways_is_the_pairwise_arrangement_and_is_close_to_it(self):
        self.assertAlmostEqual(self.excess(2), 0.105, delta=0.02)

    def test_the_error_grows_with_the_number_of_slices(self):
        self.assertLess(self.excess(2), self.excess(4))
        self.assertLess(self.excess(4), self.excess(8))
        self.assertAlmostEqual(self.excess(4), 0.773, delta=0.02)
        self.assertAlmostEqual(self.excess(8), 2.378, delta=0.03)

    def test_the_penalty_itself_grows_with_ways(self):
        values = [MEASURED_NWAY_PENALTY_GFX1201[w] for w in (1, 2, 4, 8)]
        self.assertEqual(values, sorted(values))

    def test_the_one_way_control_is_near_a_solo(self):
        # Not exactly 1.0: six episodes warm the card by about 5%.
        self.assertAlmostEqual(MEASURED_NWAY_PENALTY_GFX1201[1], 1.0,
                               delta=0.06)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
