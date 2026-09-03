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


class TheSecondDeviceMissesUnconditionallyTest(unittest.TestCase):
    """With the measured co-run penalty, gfx90a's 1.2% becomes 29.2%.

    The floor rows are optimistic by construction. The bar for the second
    SKU to join the first without qualification was a penalty above 1.012
    at 78+26; the measured one is 1.2770.
    """

    def margin(self, device):
        urgent = QuotaCostModel.for_model("sdxl", device=device)
        video = QuotaCostModel.for_model("cogvideox-2b", device=device)
        full = urgent.maskable_units
        deadline = 1.5 * 8 * urgent.step_seconds(full) * 4
        bursts = []
        for quota in bf.splits_for(urgent):
            try:
                bursts.append(bf.row(urgent, video, quota, 8, 4, 16, True,
                                     device=device)["burst_s"])
            except Exception:
                continue           # no measured pairing at this split
        return (min(bursts) - deadline) / deadline

    def test_gfx90a_misses_by_about_thirty_percent_once_co_run_is_real(self):
        self.assertAlmostEqual(self.margin("gfx90a"), 0.292, delta=0.01)

    def test_gfx1201_misses_by_about_fifty(self):
        self.assertAlmostEqual(self.margin("gfx1201"), 0.484, delta=0.01)

    def test_the_measured_penalty_clears_the_stated_bar(self):
        from burstserve.trace_sim import externality
        self.assertGreater(externality(26, 78, device="gfx90a"), 1.012)


class IntraTenantConcurrencyTest(unittest.TestCase):
    """The whole die split among the priority tenant's own requests.

    The penalty used here is 1.3's **pair** at 16+16 standing in for an
    N-way arrangement, which is what prereg-intra-tenant.md does and says
    it does. scripts/run_amd_nway_corun.py measures the real one; until
    it has, these are pinned as the arithmetic, not as a claim.
    """

    PENALTY = {"gfx1201": 1.297, "gfx90a": 1.2336}

    def burst(self, device, ways, burst=4):
        urgent = QuotaCostModel.for_model("sdxl", device=device)
        penalty = 1.0 if ways == 1 else self.PENALTY[device]
        return bf.exclusive(urgent, 8, burst, ways, penalty)["burst_s"]

    def test_four_ways_beats_serial_on_both_devices(self):
        for device, serial, four in (("gfx1201", 3.70, 2.79),
                                     ("gfx90a", 3.83, 3.12)):
            self.assertAlmostEqual(self.burst(device, 1), serial, delta=0.01)
            self.assertAlmostEqual(self.burst(device, 4), four, delta=0.01)
            self.assertLess(self.burst(device, 4), self.burst(device, 1))

    def test_asking_for_more_slices_than_requests_changes_nothing(self):
        # The policy takes critical[:concurrency] and divides among what
        # it took, so eight slices for four requests is four slices.
        for device in ("gfx1201", "gfx90a"):
            self.assertAlmostEqual(self.burst(device, 8),
                                   self.burst(device, 4), places=9)

    def test_the_optimum_number_of_ways_differs_between_the_devices(self):
        # Visible only at a burst that can fill eight slices.
        gfx1201 = [self.burst("gfx1201", n, burst=8) for n in (1, 2, 4, 8)]
        gfx90a = [self.burst("gfx90a", n, burst=8) for n in (1, 2, 4, 8)]
        self.assertEqual(gfx1201.index(min(gfx1201)), 3)   # eight ways
        self.assertEqual(gfx90a.index(min(gfx90a)), 2)     # four ways
        # And overshooting on CDNA2 is worse than not splitting at all.
        self.assertGreater(gfx90a[3], gfx90a[0])
        self.assertLess(gfx1201[3], gfx1201[0])


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
