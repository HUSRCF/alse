"""The interval every claim since 2026-08-26 rests on, now in the repo.

It was computed ad hoc, once per campaign, outside version control. The
first thing asked of it here is that it reproduce the numbers that were
published from it -- and the first time it was written down it did not,
because it sorted the clusters by ``repr`` and so put seed 10 before
seed 5. That moved A3's lower bound from -0.1521 to -0.1488: immaterial
to the verdict, and still a published number a committed tool could not
regenerate.
"""

from __future__ import annotations

import pathlib
import random
import sys
import unittest

sys.dont_write_bytecode = True

from burstserve.matrix_results import (
    bootstrap_ci,
    cluster_bootstrap_ci,
    load_cells,
    paired_differences,
)

REPO = pathlib.Path(__file__).resolve().parent.parent
A3 = REPO / "experiments" / "runs" / "expA3"

# docs/claims-and-evidence.md 1.9. experiments/runs/ is gitignored, so a
# clean checkout skips these rather than failing on absent raw data.
PUBLISHED_A3 = [
    ("step_matched_pairing", "exclusive_fcfs", "miss_rate",
     -0.0687, -0.1521, -0.0044),
    ("step_matched_pairing", "fixed_split_8", "miss_rate",
     -0.1061, -0.2040, -0.0276),
    ("step_matched_pairing", "fixed_split_8", "video_goodput",
     +0.0495, +0.0257, +0.0774),
]


class ShapeTest(unittest.TestCase):
    def test_an_empty_comparison_is_refused(self):
        with self.assertRaises(ValueError):
            cluster_bootstrap_ci([])

    def test_one_cell_per_cluster_reduces_to_the_cell_bootstrap(self):
        values = [(( 0.6, 4, seed), seed / 10.0) for seed in range(12)]
        flat = [v for _, v in values]
        a = cluster_bootstrap_ci(values, seed=3)
        b = bootstrap_ci(flat, seed=3)
        for left, right in zip(a, b):
            self.assertAlmostEqual(left, right, places=12)

    def test_it_is_deterministic_in_its_seed(self):
        values = [((0.6, 4, s), s * 0.01) for s in range(15)]
        values += [((1.05, 4, s), s * 0.01 + 0.5) for s in range(15)]
        self.assertEqual(cluster_bootstrap_ci(values, seed=7),
                         cluster_bootstrap_ci(values, seed=7))

    def test_the_mean_is_the_pooled_mean_not_a_mean_of_means(self):
        # One seed contributes three cells, the rest one. The cells are
        # the repeated measures of one trace, so the pooled mean is what
        # the point estimate should be.
        values = [((0.6, 4, 0), 1.0), ((1.05, 4, 0), 1.0),
                  ((2.0, 4, 0), 1.0), ((0.6, 4, 1), 0.0)]
        mean, _, _ = cluster_bootstrap_ci(values, seed=0, resamples=200)
        self.assertAlmostEqual(mean, 0.75)

    def test_correlated_cells_widen_the_interval(self):
        # Two perfectly correlated cells per seed carry no more
        # information than one, and the cluster interval must not pretend
        # otherwise. The cell bootstrap does.
        rng = random.Random(11)
        values = []
        for seed in range(12):
            draw = rng.gauss(0.0, 1.0)
            values.append(((0.6, 4, seed), draw))
            values.append(((1.05, 4, seed), draw))
        _, clow, chigh = cluster_bootstrap_ci(values, seed=1)
        _, low, high = bootstrap_ci([v for _, v in values], seed=1)
        self.assertGreater(chigh - clow, high - low)


@unittest.skipUnless(A3.is_dir(), "expA3 raw cells are not in this tree")
class ReproducesThePublishedIntervalsTest(unittest.TestCase):
    def setUp(self):
        self.cells = load_cells(A3)

    def test_the_campaign_is_the_one_that_was_published(self):
        self.assertEqual(len(self.cells), 120)
        self.assertEqual(len({c.seed for c in self.cells}), 15)

    def test_every_published_interval_regenerates(self):
        for method, base, metric, mean, low, high in PUBLISHED_A3:
            with self.subTest(f"{method} vs {base} on {metric}"):
                pairs = paired_differences(self.cells, method, base, metric)
                got = cluster_bootstrap_ci(pairs)
                for expected, actual in zip((mean, low, high), got):
                    self.assertAlmostEqual(actual, expected, places=4)

    def test_the_cell_bootstrap_is_the_tighter_one_that_was_corrected(self):
        pairs = paired_differences(self.cells, "step_matched_pairing",
                                   "exclusive_fcfs", "miss_rate")
        _, low, high = bootstrap_ci([v for _, v in pairs])
        self.assertAlmostEqual(low, -0.1361, places=4)
        self.assertAlmostEqual(high, -0.0161, places=4)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
