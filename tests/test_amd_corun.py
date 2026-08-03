from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import run_amd_corun as corun  # noqa: E402


def _record(windows):
    return {
        "sample_windows": [
            {"start_wall": s, "end_wall": e, "s": e - s} for s, e in windows
        ],
        "window_start_wall": windows[0][0],
        "window_end_wall": windows[-1][1],
    }


class DisjointMaskTest(unittest.TestCase):
    def test_the_two_masks_never_share_a_unit(self):
        a, b = corun.disjoint_masks(12, 20)
        self.assertEqual(int(a, 0) & int(b, 0), 0)
        self.assertEqual(bin(int(a, 0)).count("1"), 12)
        self.assertEqual(bin(int(b, 0)).count("1"), 20)

    def test_a_pair_that_would_oversubscribe_the_die_is_refused(self):
        """Two masks summing past 32 would overlap, making it not a partition."""
        with self.assertRaises(ValueError):
            corun.disjoint_masks(20, 20)

    def test_an_empty_side_is_refused(self):
        for pair in ((0, 16), (16, 0), (-4, 16)):
            with self.subTest(pair=pair), self.assertRaises(ValueError):
                corun.disjoint_masks(*pair)


class OverlapTest(unittest.TestCase):
    """Two processes that took turns produce a clean-looking co-run.

    Neither slowed the other down, so the externality reads as zero. The
    overlap window is what distinguishes that from a real co-run, so these
    tests pin it in both directions.
    """

    def test_processes_that_never_ran_together_have_no_overlap(self):
        a = _record([(0.0, 1.0), (1.0, 2.0)])
        b = _record([(10.0, 11.0), (11.0, 12.0)])
        result = corun.restrict_to_overlap(a, b, 0.8)
        self.assertEqual(result["overlap_seconds"], 0.0)
        self.assertFalse(result["sufficient_overlap"])
        self.assertEqual(result["a"]["samples_in_overlap"], 0)
        self.assertNotIn("p50_s", result["a"])

    def test_a_fully_concurrent_pair_keeps_every_sample(self):
        a = _record([(0.0, 1.0), (1.0, 2.0), (2.0, 3.0)])
        b = _record([(0.0, 1.5), (1.5, 3.0)])
        result = corun.restrict_to_overlap(a, b, 0.8)
        self.assertEqual(result["overlap_seconds"], 3.0)
        self.assertTrue(result["sufficient_overlap"])
        self.assertEqual(result["a"]["samples_in_overlap"], 3)
        self.assertEqual(result["b"]["samples_in_overlap"], 2)

    def test_samples_straddling_the_window_edge_are_dropped(self):
        """A sample only partly contended would dilute the externality.

        It would do so towards zero, which is the direction that makes the
        result look better, so partial samples are excluded rather than
        prorated.
        """
        a = _record([(0.0, 1.0), (1.0, 2.0), (2.0, 3.0)])
        b = _record([(0.9, 2.1)])
        result = corun.restrict_to_overlap(a, b, 0.1)
        self.assertEqual(result["a"]["samples_in_overlap"], 1)

    def test_a_barely_overlapping_pair_is_flagged_not_silently_reported(self):
        a = _record([(0.0, 1.0), (1.0, 10.0)])
        b = _record([(9.5, 10.5)])
        result = corun.restrict_to_overlap(a, b, 0.8)
        self.assertLess(result["overlap_fraction_of_longer_window"], 0.8)
        self.assertFalse(result["sufficient_overlap"])

    def test_the_overlap_threshold_is_the_declared_one(self):
        a = _record([(0.0, 10.0)])
        b = _record([(0.0, 8.5)])
        self.assertTrue(
            corun.restrict_to_overlap(a, b, 0.80)["sufficient_overlap"]
        )
        self.assertFalse(
            corun.restrict_to_overlap(a, b, 0.90)["sufficient_overlap"]
        )

    def test_statistics_need_at_least_two_samples_in_the_window(self):
        a = _record([(0.0, 1.0)])
        b = _record([(0.0, 1.0)])
        result = corun.restrict_to_overlap(a, b, 0.5)
        self.assertEqual(result["a"]["samples_in_overlap"], 1)
        self.assertNotIn("cv", result["a"])


if __name__ == "__main__":
    unittest.main()
