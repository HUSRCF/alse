"""Every published campaign number, regenerated from the raw cells.

3.6, 3.7 and 3.8 were computed by hand, once, and the analysis was not
committed. This regenerates each of them from ``experiments/runs`` and
fails if a number moves. ``experiments/runs`` is gitignored, so a clean
checkout skips rather than failing on absent raw data -- the cells are
never deleted, but they are not in the repository either.

It also pins the defect found while writing the analyser: the
configuration key was ``(load, burst, seed)``, which does not separate an
arrivals cell from a backlog one, so a directory holding both silently
kept half the cells. No published number used a pooled comparison, so
none was wrong -- but nothing would have said so.
"""

from __future__ import annotations

import pathlib
import statistics
import sys
import unittest

sys.dont_write_bytecode = True

from burstserve.matrix_results import (
    cluster_bootstrap_ci,
    load_cells,
    paired_differences,
)

REPO = pathlib.Path(__file__).resolve().parent.parent
RUNS = REPO / "experiments" / "runs"


def configuration(cell):
    return (cell.regime, cell.max_steps_per_round, cell.requests_per_tenant)


def compare(cells, method, against, metric, subset=None):
    chosen = [c for c in cells if subset is None or subset(c)]
    pairs = paired_differences(chosen, method, against, metric,
                               extra_key=configuration)
    mean, low, high = cluster_bootstrap_ci(pairs)
    keys = {key for key, _ in pairs}
    base = [getattr(c, metric) for c in chosen
            if c.policy == against
            and configuration(c) + (c.load, c.burst, c.seed) in keys
            and getattr(c, metric) is not None]
    return {"mean": mean, "low": low, "high": high, "n": len(pairs),
            "pct": 100.0 * mean / statistics.mean(base)}


class KeyMustSeparateRegimesTest(unittest.TestCase):
    @unittest.skipUnless((RUNS / "expP").is_dir(), "expP cells absent")
    def test_the_default_key_refuses_a_two_regime_directory(self):
        cells = load_cells(RUNS / "expP")
        with self.assertRaises(ValueError):
            paired_differences(cells, "step_matched_pairing",
                               "exclusive_priority", "miss_rate")

    @unittest.skipUnless((RUNS / "expP").is_dir(), "expP cells absent")
    def test_the_regime_key_keeps_every_cell(self):
        cells = load_cells(RUNS / "expP")
        pairs = paired_differences(cells, "step_matched_pairing",
                                   "exclusive_priority", "miss_rate",
                                   extra_key=configuration)
        # 5 seeds x 2 loads x 2 regimes.
        self.assertEqual(len(pairs), 20)

    @unittest.skipUnless((RUNS / "expA3").is_dir(), "expA3 cells absent")
    def test_a_single_regime_directory_is_unaffected(self):
        cells = load_cells(RUNS / "expA3")
        pairs = paired_differences(cells, "step_matched_pairing",
                                   "exclusive_fcfs", "miss_rate")
        self.assertEqual(len(pairs), 30)


@unittest.skipUnless((RUNS / "expP").is_dir(), "expP cells absent")
class Claim36Test(unittest.TestCase):
    """3.6: strict priority beats the scheduler, +134% and +84.5%."""

    def setUp(self):
        self.cells = load_cells(RUNS / "expP")

    def test_the_two_published_percentages(self):
        for regime, expected in (("arrivals", 134.0), ("backlog", 84.5)):
            got = compare(self.cells, "step_matched_pairing",
                          "exclusive_priority", "miss_rate",
                          lambda c, r=regime: c.regime == r)
            self.assertAlmostEqual(got["pct"], expected, delta=0.1, msg=regime)
            self.assertGreater(got["low"], 0.0, regime)

    def test_better_in_fifteen_of_twenty_tied_in_three(self):
        pairs = paired_differences(self.cells, "step_matched_pairing",
                                   "exclusive_priority", "miss_rate",
                                   extra_key=configuration)
        values = [v for _, v in pairs]
        self.assertEqual(len(values), 20)
        self.assertEqual(sum(1 for v in values if v > 0), 15)
        self.assertEqual(sum(1 for v in values if v == 0), 3)


@unittest.skipUnless((RUNS / "exp2x2").is_dir(), "exp2x2 cells absent")
class Claim37Test(unittest.TestCase):
    """3.7: all eight cells of the 2x2 lose, +82.7% to +434.0%."""

    PUBLISHED = {
        (1, "arrivals", "step_matched_pairing"): (131.3, 0.1355, 0.3931),
        (8, "arrivals", "step_matched_pairing"): (139.8, 0.1376, 0.3758),
        (1, "backlog", "step_matched_pairing"): (106.5, 0.1130, 0.2762),
        (8, "backlog", "step_matched_pairing"): (82.7, 0.0719, 0.2685),
        (1, "arrivals", "deadline_quota"): (191.9, 0.2562, 0.5163),
        (8, "arrivals", "deadline_quota"): (213.1, 0.2662, 0.5207),
        (1, "backlog", "deadline_quota"): (434.0, 0.7178, 0.8884),
        (8, "backlog", "deadline_quota"): (374.4, 0.7037, 0.8646),
    }

    def test_every_published_row_regenerates(self):
        cells = load_cells(RUNS / "exp2x2")
        for (cap, regime, policy), (pct, low, high) in self.PUBLISHED.items():
            with self.subTest(f"m={cap} {regime} {policy}"):
                got = compare(
                    cells, policy, "exclusive_priority", "miss_rate",
                    lambda c, k=cap, r=regime: (c.max_steps_per_round == k
                                                and c.regime == r))
                self.assertEqual(got["n"], 20)
                self.assertAlmostEqual(got["pct"], pct, delta=0.1)
                self.assertAlmostEqual(got["low"], low, places=4)
                self.assertAlmostEqual(got["high"], high, places=4)


@unittest.skipUnless((RUNS / "expB").is_dir(), "expB cells absent")
class Claim38Test(unittest.TestCase):
    """3.8: fixed_split_24 is dominated on both axes at both loads."""

    PUBLISHED = {
        ("arrivals", 0.6): (250.4, -13.3),
        ("arrivals", 1.05): (160.6, -23.1),
        ("backlog", 0.6): (524.1, -43.5),
        ("backlog", 1.05): (206.6, -36.7),
    }

    def test_every_published_row_regenerates(self):
        cells = load_cells(RUNS / "expB")
        for (regime, load), (miss, video) in self.PUBLISHED.items():
            with self.subTest(f"{regime} {load}"):
                pick = (lambda c, r=regime, l=load:
                        c.regime == r and c.load == l)
                got = compare(cells, "fixed_split_24", "exclusive_priority",
                              "miss_rate", pick)
                self.assertAlmostEqual(got["pct"], miss, delta=0.1)
                self.assertGreater(got["low"], 0.0)
                got = compare(cells, "fixed_split_24", "exclusive_priority",
                              "video_goodput", pick)
                self.assertAlmostEqual(got["pct"], video, delta=0.1)
                self.assertLess(got["high"], 0.0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
