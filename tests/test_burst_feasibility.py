"""3.8's published table, as an executable check.

The structural result -- no spatial split can meet this burst deadline --
was published as a hand-computed table in docs/claims-and-evidence.md. A
hand-computed table cannot notice when a cost table under it changes. So
the same arithmetic lives in scripts/burst_feasibility.py, and this pins
its output against the numbers that were published.
"""

from __future__ import annotations

import importlib.util
import math
import pathlib
import sys
import unittest

sys.dont_write_bytecode = True

REPO = pathlib.Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "burst_feasibility", REPO / "scripts" / "burst_feasibility.py")
bf = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bf)

from burstserve.trace_sim import QuotaCostModel  # noqa: E402

# docs/claims-and-evidence.md 3.8, externality off, cap 16, 8 steps,
# burst 4. The 12+20 and 20+12 splits are in the curve but were not in
# the published table; they are checked to be no better than 24+8.
PUBLISHED_GFX1201 = {
    "4+28": 17.66, "8+24": 9.73, "16+16": 6.44,
    "24+8": 6.30, "28+4": 12.47, "32+0": 3.70,
}


def table(device, externality=False):
    urgent = QuotaCostModel.for_model("sdxl", device=device)
    video = QuotaCostModel.for_model("cogvideox-2b", device=device)
    rows = [bf.row(urgent, video, q, 8, 4, 16, externality)
            for q in bf.splits_for(urgent)]
    rows.append(bf.exclusive(urgent, 8, 4))
    return {r["split"]: r for r in rows}, urgent


class ReproducesThePublishedTableTest(unittest.TestCase):
    def test_every_published_row_matches_to_ten_milliseconds(self):
        rows, _ = table("gfx1201")
        for split, burst in PUBLISHED_GFX1201.items():
            self.assertAlmostEqual(rows[split]["burst_s"], burst, delta=0.01,
                                   msg=split)

    def test_the_two_unpublished_splits_are_no_better(self):
        rows, _ = table("gfx1201")
        best = rows["24+8"]["burst_s"]
        for split in ("12+20", "20+12"):
            self.assertGreaterEqual(rows[split]["burst_s"], best, split)

    def test_no_split_meets_the_deadline_on_gfx1201(self):
        rows, urgent = table("gfx1201")
        deadline = 1.5 * 8 * urgent.step_seconds(32) * 4
        partitioned = [r["burst_s"] for s, r in rows.items() if s != "32+0"]
        self.assertGreater(min(partitioned), deadline)
        self.assertLess(rows["32+0"]["burst_s"], deadline)

    def test_raising_the_cap_cannot_help(self):
        # A request has eight steps, so a budget above eight buys nothing.
        urgent = QuotaCostModel.for_model("sdxl", device="gfx1201")
        video = QuotaCostModel.for_model("cogvideox-2b", device="gfx1201")
        for quota in bf.splits_for(urgent):
            at16 = bf.row(urgent, video, quota, 8, 4, 16, False)["burst_s"]
            at256 = bf.row(urgent, video, quota, 8, 4, 256, False)["burst_s"]
            self.assertAlmostEqual(at16, at256, places=9, msg=str(quota))


class TheSecondDeviceMissesByFarLessTest(unittest.TestCase):
    """The structural result holds on gfx90a too, and only just.

    gfx1201's best partitioned burst is 13.6% over its deadline; gfx90a's
    is 1.2% over. Both miss, so 3.8 stands on both architectures -- but a
    margin that thin is a different sentence from a margin that wide, and
    it is the measured co-run externality that decides which.
    """

    def margin(self, device):
        rows, urgent = table(device)
        full = urgent.maskable_units
        deadline = 1.5 * 8 * urgent.step_seconds(full) * 4
        partitioned = min(r["burst_s"] for s, r in rows.items()
                          if s != f"{full}+0")
        return (partitioned - deadline) / deadline

    def test_both_devices_miss(self):
        self.assertGreater(self.margin("gfx1201"), 0.0)
        self.assertGreater(self.margin("gfx90a"), 0.0)

    def test_gfx90a_misses_by_far_less(self):
        self.assertLess(self.margin("gfx90a"), 0.05)
        self.assertGreater(self.margin("gfx1201"), 0.10)
        self.assertLess(self.margin("gfx90a"), self.margin("gfx1201") / 5)

    def test_exclusive_makes_it_on_both(self):
        for device in ("gfx1201", "gfx90a"):
            rows, urgent = table(device)
            full = urgent.maskable_units
            deadline = 1.5 * 8 * urgent.step_seconds(full) * 4
            self.assertLess(rows[f"{full}+0"]["burst_s"], deadline, device)

    def test_the_best_split_differs_between_the_devices(self):
        # 24+8 is three quarters of gfx1201's die; 78+26 is three
        # quarters of gfx90a's, so the fractions agree here -- but the
        # margins do not, which is the point.
        for device, expected in (("gfx1201", "24+8"), ("gfx90a", "78+26")):
            rows, urgent = table(device)
            full = urgent.maskable_units
            best = min((r for s, r in rows.items() if s != f"{full}+0"),
                       key=lambda r: r["burst_s"])
            self.assertEqual(best["split"], expected, device)


class ExternalityIsAFloorTest(unittest.TestCase):
    def test_applying_the_measured_penalty_never_shortens_a_burst(self):
        urgent = QuotaCostModel.for_model("sdxl", device="gfx1201")
        video = QuotaCostModel.for_model("cogvideox-2b", device="gfx1201")
        for quota in (4, 8, 16, 24, 28):
            floor = bf.row(urgent, video, quota, 8, 4, 16, False)["burst_s"]
            real = bf.row(urgent, video, quota, 8, 4, 16, True)["burst_s"]
            self.assertGreaterEqual(real, floor - 1e-9, str(quota))
        self.assertTrue(math.isfinite(floor))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
