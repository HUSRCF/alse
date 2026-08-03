from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import run_amd_gate_b as gate_b  # noqa: E402


def _cell(units, batch, items_per_s, *, cv=0.01, stable=True, honoured=True):
    return {
        "status": "ok",
        "requested_units": units,
        "batch": batch,
        "items_per_s": items_per_s,
        "p50_s": batch / items_per_s,
        "cv": cv,
        "meets_cv_threshold": cv <= 0.05,
        "cu_mask_stable": stable,
        "cu_mask_attestation": {"readback_matches_request": honoured},
    }


class MaskPolicyTest(unittest.TestCase):
    def test_the_mask_is_a_contiguous_low_run_of_the_requested_width(self):
        self.assertEqual(gate_b.mask_for(4), "0xf")
        self.assertEqual(gate_b.mask_for(32), "0xffffffff")
        self.assertEqual(bin(int(gate_b.mask_for(12), 0)).count("1"), 12)

    def test_a_quota_the_die_cannot_express_is_refused(self):
        """Bits 32..63 were silently ignored by the runtime on gfx1201.

        A quota above the maskable width must fail here rather than produce a
        cell labelled with a width the hardware never installed.
        """
        for units in (0, -1, 33, 64):
            with self.subTest(units=units), self.assertRaises(ValueError):
                gate_b.mask_for(units)


class BytecodeWritingTest(unittest.TestCase):
    """A driver must not create the untracked files that fail its own gate.

    Importing burstserve writes .pyc into the tree the run is about to bind,
    and the source policy then refuses the run for paths it created itself.
    Exporting PYTHONDONTWRITEBYTECODE from the caller has already failed
    three times, so the guard has to live in the script -- and it only works
    if it runs before the first burstserve import, which is what this checks.
    """

    DRIVERS = ("run_amd_gate_b.py", "run_amd_matrix.py")

    def _positions(self, source: str):
        import ast

        tree = ast.parse(source)
        guard = first_import = None
        for node in tree.body:
            if (
                guard is None
                and isinstance(node, ast.Assign)
                and any(
                    isinstance(t, ast.Attribute)
                    and t.attr == "dont_write_bytecode"
                    for t in node.targets
                )
                and isinstance(node.value, ast.Constant)
                and node.value.value is True
            ):
                guard = node.lineno
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            if first_import is None and any(
                n.startswith("burstserve") for n in names
            ):
                first_import = node.lineno
        return guard, first_import

    def test_every_driver_disables_bytecode_before_importing_burstserve(self):
        root = Path(__file__).resolve().parent.parent / "scripts"
        for name in self.DRIVERS:
            with self.subTest(name):
                guard, first_import = self._positions(
                    (root / name).read_text(encoding="utf-8")
                )
                self.assertIsNotNone(
                    guard, f"{name} never sets sys.dont_write_bytecode = True"
                )
                self.assertIsNotNone(
                    first_import, f"{name} no longer imports burstserve"
                )
                self.assertLess(
                    guard,
                    first_import,
                    f"{name} sets the guard after importing burstserve, "
                    "which is too late to stop the .pyc it just wrote",
                )

    def test_the_check_rejects_a_guard_placed_after_the_import(self):
        """The assertion above must be able to fail."""
        guard, first_import = self._positions(
            "import sys\n"
            "from burstserve.provenance import canonical_json\n"
            "sys.dont_write_bytecode = True\n"
        )
        self.assertGreater(guard, first_import)


class SourceRebindTest(unittest.TestCase):
    """A driver must notice a tree that moved while it was running.

    source_revision binds once, at the start, but every cell is a fresh
    subprocess that re-reads its script from disk. A tree edited mid-run --
    a sync, a hotfix -- is therefore executed under a revision the report
    still claims, and the opening bind cannot see it. Each driver has to
    bind again at the end and record whether the two agree.
    """

    DRIVERS = ("run_amd_gate_b.py", "run_amd_matrix.py", "run_amd_corun.py")

    def _source(self, name):
        root = Path(__file__).resolve().parent.parent / "scripts"
        return (root / name).read_text(encoding="utf-8")

    def test_every_driver_binds_the_tree_twice(self):
        import ast

        for name in self.DRIVERS:
            with self.subTest(name):
                tree = ast.parse(self._source(name))
                calls = [
                    node for node in ast.walk(tree)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "source_revision"
                ]
                self.assertGreaterEqual(
                    len(calls), 2,
                    f"{name} binds the source tree only {len(calls)} time(s); "
                    "a mid-run edit would go unnoticed",
                )

    def test_every_driver_records_and_reports_the_comparison(self):
        for name in self.DRIVERS:
            with self.subTest(name):
                source = self._source(name)
                self.assertIn("source_revision_after", source)
                self.assertIn("source_revision_stable", source)

    def test_a_moved_revision_is_a_distinct_nonzero_exit(self):
        """Exit 2, not the same code as an ordinary rejection.

        The rows are still written -- they have diagnostic value -- so the
        exit status is the only thing that separates 'ran, but not on the
        revision claimed' from 'ran and failed the gate'.
        """
        for name in self.DRIVERS:
            with self.subTest(name):
                self.assertIn("return 2", self._source(name))


class SaturationTest(unittest.TestCase):
    def test_a_flat_throughput_response_to_more_work_is_saturating(self):
        rows = [_cell(32, 1, 0.214), _cell(32, 2, 0.216)]
        gate_b.classify(rows, 0.05)
        self.assertIs(rows[0]["saturating_regime"], True)

    def test_throughput_that_still_climbs_with_more_work_is_not_saturating(self):
        """The 2026-08-02 synthetic sweep: 1024 rows left 32 units idle.

        Doubling the problem raised throughput by a third, which means the
        smaller problem was not filling the units it held.
        """
        rows = [_cell(32, 1, 0.20), _cell(32, 2, 0.27)]
        gate_b.classify(rows, 0.05)
        self.assertIs(rows[0]["saturating_regime"], False)

    def test_the_boundary_is_the_declared_epsilon_and_nothing_else(self):
        for gain, expected in ((0.049, True), (0.051, False)):
            with self.subTest(gain=gain):
                rows = [_cell(16, 1, 1.0), _cell(16, 2, 1.0 + gain)]
                gate_b.classify(rows, 0.05)
                self.assertIs(rows[0]["saturating_regime"], expected)

    def test_a_cell_with_no_larger_problem_is_undetermined_not_saturating(self):
        """Absence of the comparison is not evidence that it would pass.

        The largest problem size has nothing above it, so its regime is
        unknown, and Gate B-AMD forbids an unlabelled cell in the canonical
        table -- which means undetermined must not read as True.
        """
        rows = [_cell(32, 1, 0.2), _cell(32, 2, 0.2)]
        gate_b.classify(rows, 0.05)
        self.assertIsNone(rows[1]["saturating_regime"])
        self.assertFalse(rows[1]["canonical_eligible"])


class MonotonicityTest(unittest.TestCase):
    def test_a_quota_that_lost_throughput_to_a_smaller_one_is_not_monotone(self):
        rows = [
            _cell(16, 1, 0.30), _cell(32, 1, 0.21),
            _cell(16, 2, 0.30), _cell(32, 2, 0.21),
        ]
        gate_b.classify(rows, 0.05)
        by_units = {r["requested_units"]: r for r in rows if r["batch"] == 1}
        self.assertTrue(by_units[16]["quota_monotone"])
        self.assertFalse(by_units[32]["quota_monotone"])
        self.assertFalse(by_units[32]["canonical_eligible"])

    def test_saturation_and_monotonicity_are_independent_claims(self):
        """A cell can be saturating and still non-monotone, and vice versa.

        Collapsing them into one flag would hide exactly the case the plan
        cares about: quota rose, throughput fell, and the units were busy.
        """
        rows = [
            _cell(16, 1, 0.30), _cell(32, 1, 0.21),
            _cell(16, 2, 0.30), _cell(32, 2, 0.21),
        ]
        gate_b.classify(rows, 0.05)
        by_units = {r["requested_units"]: r for r in rows if r["batch"] == 1}
        self.assertIs(by_units[32]["saturating_regime"], True)
        self.assertIs(by_units[32]["quota_monotone"], False)


class CanonicalEligibilityTest(unittest.TestCase):
    def _sweep(self, **kw):
        rows = [_cell(16, 1, 0.30, **kw), _cell(32, 1, 0.50, **kw),
                _cell(16, 2, 0.30), _cell(32, 2, 0.50)]
        gate_b.classify(rows, 0.05)
        return {r["requested_units"]: r for r in rows if r["batch"] == 1}

    def test_a_clean_saturating_monotone_cell_is_eligible(self):
        self.assertTrue(self._sweep()[32]["canonical_eligible"])

    def test_a_cell_that_missed_its_cv_target_is_not_eligible(self):
        self.assertFalse(self._sweep(cv=0.075)[32]["canonical_eligible"])

    def test_a_cell_whose_mask_the_runtime_ignored_is_not_eligible(self):
        self.assertFalse(self._sweep(honoured=False)[32]["canonical_eligible"])

    def test_a_cell_whose_mask_changed_mid_run_is_not_eligible(self):
        self.assertFalse(self._sweep(stable=False)[32]["canonical_eligible"])

    def test_failed_cells_never_acquire_flags(self):
        rows = [{"status": "cell_failed", "requested_units": 8, "batch": 1}]
        gate_b.classify(rows, 0.05)
        self.assertNotIn("canonical_eligible", rows[0])


if __name__ == "__main__":
    unittest.main()
