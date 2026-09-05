"""The step-level co-run table, withdrawn the day it was measured.

Measured 2026-09-04 after the whole-call harness was caught charging
`hipMalloc`-class device drains as contention, and withdrawn hours later
when its own control showed it charging *allocator fragmentation* as
contention. These pin the withdrawal and the control that produced it.

See docs/claims-and-evidence.md 3.10.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from burstserve import trace_sim                       # noqa: E402
from burstserve.trace_sim import (                     # noqa: E402
    EXTERNALITY_TABLES_BY_SOURCE,
    WITHDRAWN_EXTERNALITY_GFX90A_STEPS,
    WITHDRAWN_SOURCES,
    externality,
)

DIAG = REPO / "experiments" / "probes" / "gfx1201" / "nway_steps_diag"


class TheControlThatKilledItTest(unittest.TestCase):
    """Read from the raw runs, so the pin cannot drift from the evidence."""

    @classmethod
    def setUpClass(cls):
        if not DIAG.is_dir():
            raise unittest.SkipTest(f"{DIAG} not present")
        cls.runs = {}
        for path in DIAG.glob("*.json"):
            d = json.loads(path.read_text())
            mean = lambda rows: sum(r["p50_s"] for r in rows) / len(rows)
            cls.runs[d["ways"]] = {
                "solo": mean(d["solo_before"]),
                "corun": sum(r["corun_p50_s"] for r in d["verdict"])
                / len(d["verdict"]),
                "after": mean(d["solo_after"]),
                "emptied": mean(d["solo_after_empty_cache"]),
            }

    def test_all_three_widths_are_present(self):
        self.assertEqual(sorted(self.runs), [2, 4, 8])

    def test_the_post_episode_solo_reads_the_corun(self):
        # The peers are idle -- run_solo walks the adapters one at a
        # time -- so a penalty that is still there is not contention.
        for ways, r in self.runs.items():
            with self.subTest(ways=ways):
                self.assertAlmostEqual(r["after"] / r["corun"], 1.0,
                                       delta=0.05)

    def test_empty_cache_restores_the_solo_at_every_width(self):
        for ways, r in self.runs.items():
            with self.subTest(ways=ways):
                self.assertAlmostEqual(r["emptied"] / r["solo"], 1.0,
                                       delta=0.03)

    def test_the_apparent_penalty_was_large_before_the_control(self):
        self.assertGreater(self.runs[8]["corun"] / self.runs[8]["solo"], 3.5)


class TheWithdrawalIsEnforcedInCodeTest(unittest.TestCase):

    def test_asking_for_the_step_source_raises_with_the_reason(self):
        with self.assertRaises(KeyError) as caught:
            externality(26, 78, device="gfx90a", source="steps")
        self.assertIn("allocator", str(caught.exception))

    def test_it_does_not_silently_fall_back_to_the_call_level_table(self):
        with self.assertRaises(KeyError):
            externality(78, 26, device="gfx90a", source="steps")

    def test_calls_is_the_only_live_source(self):
        self.assertEqual(sorted(EXTERNALITY_TABLES_BY_SOURCE), ["calls"])
        self.assertIn("steps", WITHDRAWN_SOURCES)

    def test_the_table_is_renamed_so_a_reader_has_to_notice(self):
        self.assertFalse(hasattr(trace_sim,
                                 "MEASURED_EXTERNALITY_GFX90A_STEPS"))
        self.assertEqual(len(WITHDRAWN_EXTERNALITY_GFX90A_STEPS), 5)


class WhatSurvivesInTheBurstArithmeticTest(unittest.TestCase):
    """The floor rows read no co-run table, so 3.10 does not touch them."""

    def best(self, *flags):
        out = subprocess.run(
            [sys.executable, str(REPO / "scripts" / "burst_feasibility.py"),
             *flags],
            capture_output=True, text=True, check=True).stdout
        return [l for l in out.splitlines() if "best partitioned" in l][-1]

    def test_gfx1201_misses_at_the_floor(self):
        self.assertIn("6.30 s, +13.6%", self.best("--device", "gfx1201"))

    def test_gfx90a_is_marginal_at_the_floor(self):
        self.assertIn("5.81 s, +1.2%", self.best("--device", "gfx90a"))

    def test_exclusive_still_beats_every_split_by_far(self):
        for device, exclusive in (("gfx1201", "3.70 s"), ("gfx90a", "3.83 s")):
            with self.subTest(device=device):
                self.assertIn(f"exclusive {exclusive}",
                              self.best("--device", device))

    def test_the_withdrawn_source_is_refused_by_the_script(self):
        done = subprocess.run(
            [sys.executable, str(REPO / "scripts" / "burst_feasibility.py"),
             "--device", "gfx90a", "--externality",
             "--externality-source", "steps"],
            capture_output=True, text=True)
        self.assertNotEqual(done.returncode, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
