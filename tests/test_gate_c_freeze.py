"""Gate C criterion 7: the algorithm freeze holds.

A freeze is a claim about the repository, so this runs the real verifier
against the real manifest rather than reimplementing the comparison. It
also checks the verifier can fail, because a freeze that passes on a
changed algorithm is worse than none -- it certifies the change.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import unittest

sys.dont_write_bytecode = True

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "freeze_gate_c_algorithm.py"
MANIFEST = REPO / "experiments" / "manifests" / "gate_c_algorithm_freeze.json"
LOG = REPO / "docs" / "gate-c-decision-log.md"


def run_verifier():
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True, text=True, cwd=REPO,
    )


class FreezeHoldsTest(unittest.TestCase):
    def test_the_freeze_verifies(self):
        proc = run_verifier()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_the_manifest_records_the_action_order(self):
        manifest = json.loads(MANIFEST.read_text())
        order = manifest["action_order"]
        self.assertEqual(len(order), 5)
        self.assertIn("deadline override", order[0])
        self.assertIn("matched pairing", order[1])
        self.assertIn("deficit rotation", order[2])
        # Added when the pairing bistability was measured: budget
        # deadlines against the slow state, and drop a pairing that
        # lands in it.
        self.assertIn("slow-state budget", order[3])
        self.assertIn("probe", order[4])

    def test_the_decision_log_exists_and_is_referenced(self):
        self.assertTrue(LOG.exists())
        manifest = json.loads(MANIFEST.read_text())
        self.assertEqual(manifest["decision_log"],
                         "docs/gate-c-decision-log.md")
        self.assertIn("2026-08-06", LOG.read_text())

    def test_every_frozen_function_has_all_three_lock_sections(self):
        manifest = json.loads(MANIFEST.read_text())
        self.assertGreaterEqual(len(manifest["structural"]), 14)
        self.assertEqual(set(manifest["tables"]),
                         {"MEASURED_MODELS", "MEASURED_QUOTA_SECONDS",
                          "MEASURED_EXTERNALITY"})
        self.assertEqual(set(manifest["behavioural"]),
                         {"matched_tenants", "mismatched_tenants",
                          "feasible_deadline",
                          "matched_tenants_5pct_predictor_error"})

    def test_a_behavioural_trace_covers_predictor_error(self):
        """At least one trace has to run off the intended path.

        The three branch-coverage traces use an exact predictor, and a
        starvation defect fixed on 2026-08-06 left all three digests
        byte-identical because the defective and correct algorithms agree
        exactly when costs are exact.
        """
        manifest = json.loads(MANIFEST.read_text())
        self.assertIn("matched_tenants_5pct_predictor_error",
                      manifest["behavioural"])


class FreezeCanFailTest(unittest.TestCase):
    """The lock has to bite, and only on what it claims to lock."""

    def _with_source(self, path: pathlib.Path, replacement):
        """Apply an edit, run the verifier, always restore.

        Asserts the edit actually changed the file. A replacement whose
        target string has drifted silently becomes a no-op, and a no-op
        edit makes "the freeze detects this" untestable while still
        producing a verdict -- which is how a broken lock passes.
        """
        original = path.read_text()
        edited = replacement(original)
        self.assertNotEqual(
            edited, original,
            f"the edit did not match anything in {path.name}; its target "
            f"has drifted and this test is no longer exercising the lock",
        )
        try:
            path.write_text(edited)
            return run_verifier()
        finally:
            path.write_text(original)

    def test_a_changed_constant_breaks_it(self):
        proc = self._with_source(
            REPO / "src" / "burstserve" / "policies.py",
            lambda s: s.replace("now: float, tolerance: float = 1.6)",
                                "now: float, tolerance: float = 1.7)", 1),
        )
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("step_matched_pairing", proc.stdout)

    def test_a_changed_action_order_breaks_it(self):
        """Swapping the branches is the change the gate names outright."""
        proc = self._with_source(
            REPO / "src" / "burstserve" / "policies.py",
            lambda s: s.replace(
                "    rescued = deadline_aware(states, units, now)",
                "    rescued = {}  # order swapped", 1),
        )
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("slo_aware_partitioning", proc.stdout)

    def test_a_changed_measured_table_breaks_it(self):
        proc = self._with_source(
            REPO / "src" / "burstserve" / "trace_sim.py",
            lambda s: s.replace('"serial_fraction": 0.4419',
                                '"serial_fraction": 0.4420', 1),
        )
        self.assertEqual(proc.returncode, 1, proc.stdout)

    def test_reworded_prose_does_not_break_it(self):
        """A freeze that fires on comments gets re-run without reading."""
        proc = self._with_source(
            REPO / "src" / "burstserve" / "policies.py",
            lambda s: s.replace(
                "    ``static_even`` wins 8.8% on matched tenants",
                "    Even splitting wins 8.8% on matched tenants", 1),
        )
        self.assertEqual(proc.returncode, 0, proc.stdout)


if __name__ == "__main__":
    unittest.main()
