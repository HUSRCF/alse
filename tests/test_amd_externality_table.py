from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import run_amd_externality_table as table  # noqa: E402


class PairCoverageTest(unittest.TestCase):
    """The table has to vary the axes the externality was found to vary on.

    A single 16+16 measurement extrapolated to every split would be one
    point presented as a surface. Measured so far: pure matmul cost 47%
    per tenant against 23% for SDXL, so the penalty depends on the
    workload; whether it depends on the split is what these pairs ask.
    """

    def test_the_pairs_include_an_asymmetric_split(self):
        splits = {(a, b) for a, b, *_ in table.DEFAULT_PAIRS}
        self.assertTrue(
            any(a != b for a, b in splits),
            "every pair is symmetric, so the table cannot say whether the "
            "penalty depends on how the die is divided",
        )

    def test_asymmetry_is_measured_in_both_directions(self):
        """8+24 and 24+8 differ: the masks occupy different physical units.

        Gate A established that all 32 units behave alike, so a difference
        between the two would be evidence against that, and their agreement
        is a check on it.
        """
        splits = {(a, b) for a, b, *_ in table.DEFAULT_PAIRS}
        mirrored = [(a, b) for a, b in splits if a != b and (b, a) in splits]
        self.assertTrue(mirrored, f"no mirrored pair among {sorted(splits)}")

    def test_every_pair_fits_within_the_die(self):
        for a, b, *_ in table.DEFAULT_PAIRS:
            with self.subTest(pair=(a, b)):
                self.assertLessEqual(a + b, 32)
                self.assertGreaterEqual(min(a, b), 1)

    def test_labels_are_unique_so_reports_do_not_overwrite(self):
        labels = [entry[6] for entry in table.DEFAULT_PAIRS]
        self.assertEqual(len(labels), len(set(labels)))

    def test_report_paths_are_unique_per_pair(self):
        paths = {
            f"corun_{entry[6]}_{entry[0]}_{entry[1]}.json"
            for entry in table.DEFAULT_PAIRS
        }
        self.assertEqual(len(paths), len(table.DEFAULT_PAIRS))


class UnfitPairsAreRecordedTest(unittest.TestCase):
    """A pair that does not fit is a row, not an omission.

    CogVideoX peaks near 28.5 GB on a 34.2 GB card, so it cannot be paired
    with anything. Dropping such pairs silently would leave a table that
    looks complete and quietly excludes the models that constrain the
    scheduler most.
    """

    def test_a_missing_report_is_recorded_with_a_status(self):
        source = ast.unparse(ast.parse(
            (SCRIPTS / "run_amd_externality_table.py").read_text("utf-8")))
        self.assertIn("no_report", source)
        self.assertIn("memory_fits", source)

    def test_rows_without_a_measurement_are_still_counted(self):
        source = (SCRIPTS / "run_amd_externality_table.py").read_text("utf-8")
        # attempted and measured are reported separately, so a table with
        # unfit pairs cannot be read as if every pair had been measured.
        self.assertIn('"measured": len(measured)', source)
        self.assertIn('"attempted": len(rows)', source)

    def test_the_summary_only_ranges_over_measured_pairs(self):
        tree = ast.parse(
            (SCRIPTS / "run_amd_externality_table.py").read_text("utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and any(getattr(t, "id", None) == "measured" for t in node.targets)
            ):
                expression = ast.unparse(node.value)
                self.assertIn("externality_a", expression)
                self.assertIn("is not None", expression)
                return
        self.fail("`measured` is never computed")


if __name__ == "__main__":
    unittest.main()
