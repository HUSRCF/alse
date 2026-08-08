"""The amended co-run clause, and the precondition that makes it honest.

plan.md required two processes with a process-wide CU mask because a
per-stream mask binds only the kernels that reach that stream. Work the
framework launches elsewhere would be unconstrained and the partition
nominal. On 2026-08-08 the clause was amended to accept streams, after
that risk was measured and excluded.

The amendment is only defensible with its precondition attached: a
stream-form co-run counts as evidence when accompanied by mask-
penetration evidence for that card and framework version, and not
otherwise. These tests hold the verifier to that, including on evidence
that shows a leak -- the case where accepting it would be worst.
"""

from __future__ import annotations

import json
import pathlib
import sys
import unittest

sys.dont_write_bytecode = True

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import importlib.util

_spec = importlib.util.spec_from_file_location(
    "verify_gate_b_amd", REPO / "scripts" / "verify_gate_b_amd.py"
)
verifier = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verifier)

PROBES = REPO / "experiments" / "probes" / "amd-r9700-cu-mask"
PENETRATION = PROBES / "stream_mask_penetration_20260807.json"


def stream_corun(**overrides) -> dict:
    report = {
        "status": "ok",
        "arrangement": "one process, one context, two masked streams",
        "shared_weights": True,
        "masks": {"a": "0xffff", "b": "0xffff0000", "disjoint": True},
        "overlap": {"overlap_seconds": 111.0, "sufficient_overlap": True,
                    "a": {"externality": 0.272}, "b": {"externality": 0.314}},
    }
    report.update(overrides)
    return report


def process_corun() -> dict:
    return {
        "status": "ok",
        "masks": {"a": "0xffff", "b": "0xffff0000", "disjoint": True},
        "overlap": {"overlap_seconds": 150.0, "sufficient_overlap": True,
                    "a": {"externality": 0.224}, "b": {"externality": 0.245}},
    }


def leaky_penetration() -> dict:
    """What a mask that does not bind the pipeline looks like.

    A leak pays most where the mask is tightest, so the low-quota cell
    runs disproportionately fast against the process-mask curve.
    """
    return {"rows": [
        {"units": 4, "ratio_to_two_process": 0.62},
        {"units": 8, "ratio_to_two_process": 0.78},
        {"units": 16, "ratio_to_two_process": 0.93},
        {"units": 32, "ratio_to_two_process": 0.99},
    ]}


class StreamFormNeedsPenetrationEvidence(unittest.TestCase):
    def test_a_stream_corun_without_evidence_is_not_measured(self):
        result = verifier.check_corun(stream_corun(), None)
        self.assertEqual(result["status"], verifier.NOT_MEASURED)

    def test_a_stream_corun_with_leaky_evidence_is_not_measured(self):
        """The case the precondition exists for.

        Accepting this would record a partition that was never a
        partition, with an externality diluted toward zero -- the
        direction that flatters the result.
        """
        result = verifier.check_corun(stream_corun(), leaky_penetration())
        self.assertEqual(result["status"], verifier.NOT_MEASURED)
        self.assertFalse(
            result["detail"]["mask_penetration"]["trend_excludes_a_leak"]
        )

    def test_a_stream_corun_with_the_measured_evidence_passes(self):
        if not PENETRATION.exists():
            self.skipTest(f"probe not present: {PENETRATION.name}")
        evidence = json.loads(PENETRATION.read_text())
        result = verifier.check_corun(stream_corun(), evidence)
        self.assertEqual(result["status"], verifier.PASS)
        self.assertEqual(result["detail"]["form"], "streams")

    def test_the_evidence_is_judged_directionally(self):
        """Not on closeness alone.

        A leak that shifted every quota equally would satisfy "the curves
        agree" while binding nothing. What distinguishes it is that
        agreement deteriorates as quota falls.
        """
        if not PENETRATION.exists():
            self.skipTest(f"probe not present: {PENETRATION.name}")
        verdict = verifier.check_mask_penetration(
            json.loads(PENETRATION.read_text())
        )
        self.assertEqual(verdict["status"], "ok")
        self.assertGreaterEqual(verdict["ratio_at_lowest_quota"],
                                verdict["ratio_at_highest_quota"])

    def test_a_process_corun_needs_no_such_evidence(self):
        """A process mask binds every kernel the process launches, which
        is why the original clause named processes."""
        result = verifier.check_corun(process_corun(), None)
        self.assertEqual(result["status"], verifier.PASS)
        self.assertEqual(result["detail"]["form"], "processes")

    def test_overlap_is_still_required_in_either_form(self):
        """Two workers that never overlapped give exactly the numbers a
        zero-externality co-run gives."""
        if not PENETRATION.exists():
            self.skipTest(f"probe not present: {PENETRATION.name}")
        evidence = json.loads(PENETRATION.read_text())
        report = stream_corun()
        report["overlap"]["sufficient_overlap"] = False
        self.assertEqual(verifier.check_corun(report, evidence)["status"],
                         verifier.FAIL)

    def test_disjointness_is_still_required(self):
        if not PENETRATION.exists():
            self.skipTest(f"probe not present: {PENETRATION.name}")
        evidence = json.loads(PENETRATION.read_text())
        report = stream_corun()
        report["masks"]["disjoint"] = False
        self.assertEqual(verifier.check_corun(report, evidence)["status"],
                         verifier.FAIL)


class TheCogVideoXVerdictUsesIt(unittest.TestCase):
    VERDICT = PROBES / "gate_b_amd_verdict_cogvideox2b_20260808.json"

    def test_the_corun_clause_now_passes(self):
        if not self.VERDICT.exists():
            self.skipTest(f"verdict not present: {self.VERDICT.name}")
        payload = json.loads(self.VERDICT.read_text())
        clause = next(c for c in payload["clauses"]
                      if c["clause"].startswith("corun_"))
        self.assertEqual(clause["status"], "PASS")

    def test_cold_model_is_the_only_remaining_failure(self):
        """Which is shared with SDXL and deferred to the runtime stage,
        so the two models are now at the same point."""
        if not self.VERDICT.exists():
            self.skipTest(f"verdict not present: {self.VERDICT.name}")
        payload = json.loads(self.VERDICT.read_text())
        self.assertEqual(payload["failed"],
                         ["cold_model_predicts_transfer_and_framework_"
                          "separately"])
        self.assertEqual(payload.get("not_measured"), [])


if __name__ == "__main__":
    unittest.main()
