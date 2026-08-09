"""Aggregating matrix cells into a claim, and the ways that goes wrong.

The first real cell showed the same method beating FCFS by 30%, the
strongest baseline by 22% and its own ablation by 4.5%. Which opponent a
headline used is the easiest thing to lose between a table and a
sentence, so the choice is a rule here and the rule is tested.
"""

from __future__ import annotations

import json
import random
import statistics
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True

from burstserve.matrix_results import (
    BASELINE_POLICIES,
    METHOD_POLICIES,
    Cell,
    bootstrap_ci,
    completeness,
    load_cells,
    paired_differences,
    primary_claim,
    strongest_baseline,
)


def cell(policy, seed, miss, goodput=0.55, load=0.6, burst=4):
    return Cell(policy=policy, load=load, burst=burst, seed=seed,
                miss_rate=miss, video_goodput=goodput, urgent_p99_s=1.0,
                urgent_completed=40, path="")


def population(method_miss, baseline_miss, seeds=range(5), **kwargs):
    out = []
    for seed in seeds:
        out.append(cell("probing_partitioning", seed, method_miss[seed],
                        **kwargs))
        out.append(cell("static_even", seed, baseline_miss[seed], **kwargs))
        out.append(cell("exclusive_fcfs", seed, baseline_miss[seed] + 0.1,
                        **kwargs))
    return out


class TheOpponentIsChosenByRule(unittest.TestCase):
    def test_the_strongest_baseline_is_the_best_baseline(self):
        cells = population([0.5] * 5, [0.7] * 5)
        self.assertEqual(strongest_baseline(cells, "miss_rate"),
                         "static_even")

    def test_a_method_variant_is_never_the_baseline(self):
        """Otherwise the comparison is against the method."""
        cells = population([0.5] * 5, [0.7] * 5)
        cells += [cell("slo_aware_partitioning", s, 0.52) for s in range(5)]
        self.assertEqual(strongest_baseline(cells, "miss_rate"),
                         "static_even")

    def test_the_two_sets_do_not_overlap(self):
        self.assertFalse(set(BASELINE_POLICIES) & set(METHOD_POLICIES))

    def test_the_claim_names_which_opponent_it_used(self):
        cells = population([0.5] * 5, [0.7] * 5)
        claim = primary_claim(cells, method="probing_partitioning")
        self.assertEqual(claim["strongest_baseline"], "static_even")
        self.assertIn("exclusive_fcfs", claim["per_baseline"])
        self.assertIn("static_even", claim["per_baseline"])


class PairingIsBySeed(unittest.TestCase):
    def test_only_configurations_both_policies_ran_are_paired(self):
        cells = population([0.5] * 5, [0.7] * 5)
        cells = [c for c in cells
                 if not (c.policy == "static_even" and c.seed == 3)]
        pairs = paired_differences(cells, "probing_partitioning",
                                   "static_even", "miss_rate")
        self.assertEqual(len(pairs), 4)
        self.assertNotIn(3, [key[2] for key, _ in pairs])

    def test_pairing_survives_more_than_one_configuration(self):
        cells = population([0.5] * 5, [0.7] * 5)
        cells += population([0.4] * 5, [0.6] * 5, burst=8)
        pairs = paired_differences(cells, "probing_partitioning",
                                   "static_even", "miss_rate")
        self.assertEqual(len(pairs), 10)
        self.assertEqual({key[1] for key, _ in pairs}, {4, 8})

    def test_the_paired_interval_is_tighter_than_the_unpaired_one(self):
        """The reason to pair at all.

        Seeds differ a lot; the difference between the arms at a seed
        barely does. Bootstrapping the arms separately would hide that.
        """
        rng = random.Random(0)
        base = [0.5 + rng.random() * 0.4 for _ in range(20)]
        method = [b - 0.15 for b in base]
        _, low, high = bootstrap_ci([m - b for m, b in zip(method, base)])
        paired_width = high - low
        _, mlow, mhigh = bootstrap_ci(method)
        _, blow, bhigh = bootstrap_ci(base)
        unpaired_width = (mhigh - mlow) + (bhigh - blow)
        self.assertLess(paired_width, unpaired_width / 10)


class TheIntervalIsUsedAsAnInterval(unittest.TestCase):
    def test_a_reduction_whose_interval_crosses_zero_is_not_met(self):
        rng = random.Random(1)
        method = [0.5 + rng.uniform(-0.3, 0.3) for _ in range(5)]
        baseline = [0.7 for _ in range(5)]
        cells = population(method, baseline)
        claim = primary_claim(cells, method="probing_partitioning")
        if not claim["pareto_branch_one"]["interval_excludes_zero"]:
            self.assertFalse(claim["pareto_branch_one"]["met"])

    def test_a_clean_reduction_is_met(self):
        cells = population([0.5] * 5, [0.7] * 5)
        claim = primary_claim(cells, method="probing_partitioning")
        branch = claim["pareto_branch_one"]
        self.assertAlmostEqual(branch["miss_relative_change"], -0.2857,
                               places=3)
        self.assertTrue(branch["interval_excludes_zero"])
        self.assertTrue(branch["met"])

    def test_a_goodput_collapse_fails_the_branch(self):
        """Pareto means both halves, so one alone must not pass it."""
        cells = []
        for seed in range(5):
            cells.append(cell("probing_partitioning", seed, 0.5,
                              goodput=0.30))
            cells.append(cell("static_even", seed, 0.7, goodput=0.55))
        claim = primary_claim(cells, method="probing_partitioning")
        self.assertLess(claim["pareto_branch_one"]["miss_relative_change"],
                        -0.20)
        self.assertFalse(claim["pareto_branch_one"]["met"])

    def test_a_small_reduction_fails_the_branch(self):
        cells = population([0.66] * 5, [0.7] * 5)
        claim = primary_claim(cells, method="probing_partitioning")
        self.assertFalse(claim["pareto_branch_one"]["met"])

    def test_the_bootstrap_is_reproducible(self):
        values = [-0.2, -0.1, -0.3, -0.15, -0.25]
        self.assertEqual(bootstrap_ci(values, seed=5),
                         bootstrap_ci(values, seed=5))

    def test_five_seeds_give_a_coarse_interval(self):
        """A property of plan.md's "at least 5 seeds", asserted not assumed.

        With five paired differences the resampled mean can only take a
        few hundred distinct values, so the 2.5th percentile lands on the
        same number for different bootstrap seeds -- the interval is
        quantised, not merely noisy. Twenty differences separate them.
        Recorded here because an interval that looks precise and is
        quantised invites over-reading, and 5 seeds is the floor plan.md
        sets rather than a target.
        """
        five = [-0.2, -0.1, -0.3, -0.15, -0.25]
        self.assertEqual(bootstrap_ci(five, seed=5)[1],
                         bootstrap_ci(five, seed=6)[1])
        rng = random.Random(3)
        twenty = [-0.2 + rng.uniform(-0.1, 0.1) for _ in range(20)]
        self.assertNotEqual(bootstrap_ci(twenty, seed=5)[1],
                            bootstrap_ci(twenty, seed=6)[1])

    def test_an_empty_set_of_differences_is_refused(self):
        with self.assertRaises(ValueError):
            bootstrap_ci([])


class CompletenessIsCountedNotAssumed(unittest.TestCase):
    def test_missing_cells_are_listed(self):
        cells = population([0.5] * 5, [0.7] * 5)
        report = completeness(cells, policies=["probing_partitioning",
                                               "static_even"],
                              loads=[0.6], bursts=[4], seeds=range(5))
        self.assertEqual(report["completion"], 1.0)
        report = completeness(cells, policies=["probing_partitioning",
                                               "static_even"],
                              loads=[0.6, 0.85], bursts=[4], seeds=range(5))
        self.assertAlmostEqual(report["completion"], 0.5)
        self.assertEqual(len(report["missing"]), 10)


class LoadingKeepsWhatRan(unittest.TestCase):
    def test_a_cell_with_no_urgent_requests_is_kept(self):
        """Real data about the trace, not a file to skip.

        Dropping it would make a completion-rate acceptance uncheckable.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json"
            path.write_text(json.dumps({
                "schema_version": "burstserve.amd-matrix-cell/v1",
                "policy": "exclusive_fcfs",
                "spec": {"load": 0.6, "burst": 4, "seed": 0},
                "urgent": {"miss_rate": None, "latency_p99_s": None,
                           "completed": 0},
                "video": {"goodput_steps_per_s": 0.0},
            }))
            cells = load_cells(Path(tmp))
        self.assertEqual(len(cells), 1)
        self.assertIsNone(cells[0].miss_rate)

    def test_a_foreign_json_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "other.json").write_text('{"schema_version": "x"}')
            self.assertEqual(load_cells(Path(tmp)), [])


if __name__ == "__main__":
    unittest.main()
