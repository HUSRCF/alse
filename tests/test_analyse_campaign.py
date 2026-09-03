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

# The sign test lives in the analyser script, which imports nothing that
# needs a GPU, so it is loaded the same way here.
import importlib.util as _importlib_util

_SPEC = _importlib_util.spec_from_file_location(
    "analyse_campaign",
    pathlib.Path(__file__).resolve().parent.parent
    / "scripts" / "analyse_campaign.py")
_ANALYSER = _importlib_util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_ANALYSER)
sign_p = _ANALYSER.sign_p

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


class CrossedFactorsTest(unittest.TestCase):
    """A runtime factor a policy is fixed at belongs to the arm.

    The 2x2 ran every policy at both caps, so the cap is crossed with the
    arms and is part of the configuration. expC's arms each have their own
    ``requests_per_tenant`` -- it is a runtime setting, so one process
    cannot hold two values of it, and ``exclusive_priority`` exists only
    at 1 while ``concurrent_quota_c4`` exists only at 4. Putting that in
    the key gives the two arms different configurations and pairs nothing
    at all, which is how an analysis reports "no paired cells" for a
    campaign that ran fine.
    """

    @unittest.skipUnless((RUNS / "exp2x2").is_dir(), "exp2x2 cells absent")
    def test_the_2x2_crosses_the_cap_with_its_arms(self):
        cells = load_cells(RUNS / "exp2x2")
        names = [name for name, _ in _ANALYSER.crossed_factors(cells)]
        self.assertIn("cap", names)
        self.assertNotIn("rpt", names)

    def test_a_factor_fixed_per_policy_is_not_crossed(self):
        from burstserve.matrix_results import Cell

        def cell(policy, rpt):
            return Cell(policy=policy, load=0.6, burst=4, seed=0,
                        miss_rate=0.0, video_goodput=1.0, urgent_p99_s=1.0,
                        urgent_completed=1, path="x",
                        requests_per_tenant=rpt)

        arms = [cell("exclusive_priority", 1), cell("concurrent_quota_c4", 4)]
        self.assertEqual(_ANALYSER.crossed_factors(arms), [])
        crossed = arms + [cell("exclusive_priority", 4)]
        self.assertEqual([n for n, _ in _ANALYSER.crossed_factors(crossed)],
                         ["rpt"])

    def test_the_key_always_carries_the_regime(self):
        from burstserve.matrix_results import Cell
        cells = [Cell(policy="a", load=0.6, burst=4, seed=0, miss_rate=0.0,
                      video_goodput=1.0, urgent_p99_s=1.0,
                      urgent_completed=1, path="x", video_backlog=backlog)
                 for backlog in (False, True)]
        key, names = _ANALYSER.make_configuration(cells)
        self.assertEqual(names, [])
        self.assertNotEqual(key(cells[0]), key(cells[1]))


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


@unittest.skipUnless((RUNS / "expA3").is_dir(), "expA3 cells absent")
class Claim19ShapeTest(unittest.TestCase):
    """1.9's shape: asymmetric in magnitude, not in frequency.

    The interval excludes zero and the sign test says nothing. Reporting
    either alone misrepresents it, so both are pinned.
    """

    def test_the_counts_and_the_sign_test(self):
        cells = load_cells(RUNS / "expA3")
        pairs = paired_differences(cells, "step_matched_pairing",
                                   "exclusive_fcfs", "miss_rate",
                                   extra_key=configuration)
        values = [v for _, v in pairs]
        wins = sum(1 for v in values if v < 0)
        losses = sum(1 for v in values if v > 0)
        ties = sum(1 for v in values if v == 0)
        self.assertEqual((wins, losses, ties), (13, 8, 9))
        self.assertAlmostEqual(sign_p(wins, losses), 0.383, places=3)

    def test_no_loss_exceeds_five_points_and_wins_do(self):
        cells = load_cells(RUNS / "expA3")
        pairs = paired_differences(cells, "step_matched_pairing",
                                   "exclusive_fcfs", "miss_rate",
                                   extra_key=configuration)
        values = [v for _, v in pairs]
        self.assertAlmostEqual(max(v for v in values), 0.0455, places=4)
        self.assertGreaterEqual(sum(1 for v in values if v < -0.10), 5)
        # Two wins are at least 60 points: -0.7500 and exactly -0.6000.
        # 1.9 first said "two exceed 60", which the second one does not.
        self.assertGreaterEqual(sum(1 for v in values if v <= -0.60), 2)
        self.assertEqual(sum(1 for v in values if v < -0.60), 1)


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


@unittest.skipUnless((RUNS / "expB").is_dir(), "expB cells absent")
class Claim38InvalidityConditionTest(unittest.TestCase):
    """3.8's pre-registered invalidity condition fired, and here is why.

    ``pipelined_quota`` was required to issue 24+8 and never did. The
    published histogram is the evidence; a claim that a policy never took
    an action is only as good as the counter behind it.
    """

    PUBLISHED = {"4": 394, "8": 255, "16": 58, "32": 12173}

    def test_the_urgent_quota_histogram_regenerates(self):
        from collections import Counter
        cells = load_cells(RUNS / "expB")
        total = Counter()
        for cell in cells:
            if cell.policy != "pipelined_quota":
                continue
            for units, count in ((cell.ledger or {}).get(
                    "urgent_units_histogram") or {}).items():
                total[units] += count
        self.assertEqual(dict(total), self.PUBLISHED)

    def test_it_never_gave_the_urgent_tenant_twenty_four_units(self):
        # The histogram, not the shapes. expB's cells were written by a
        # tree whose grant_shapes sorted the widths DESCENDING, so a
        # round that gave urgent 8 and video 24 is recorded as "24+8" and
        # a reader taking that as (urgent, video) would conclude the
        # opposite of the truth. 3.8 quotes the histogram, so the
        # published claim is sound and the counter was not.
        cells = load_cells(RUNS / "expB")
        for cell in cells:
            if cell.policy != "pipelined_quota":
                continue
            histogram = (cell.ledger or {}).get(
                "urgent_units_histogram") or {}
            self.assertNotIn("24", histogram, cell.path)

    def test_the_old_shape_format_is_ambiguous_and_that_is_recorded(self):
        # Pinned so nobody reads an old payload's shapes as tenant
        # ordered: the same cell shows "24+8" in shapes and 8 in the
        # histogram, and both are correct.
        cells = load_cells(RUNS / "expB")
        contradictions = 0
        for cell in cells:
            if cell.policy != "pipelined_quota":
                continue
            shapes = (cell.ledger or {}).get("grant_shapes") or {}
            histogram = (cell.ledger or {}).get(
                "urgent_units_histogram") or {}
            if "24+8" in shapes and "24" not in histogram:
                contradictions += 1
        self.assertGreater(contradictions, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
