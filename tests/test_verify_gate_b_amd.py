from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import verify_gate_b_amd as verify  # noqa: E402


def _row(units, *, role="canonical", p50=None, cv=0.01, eligible=True,
         saturating=True, **overrides):
    row = {
        "status": "ok",
        "role": role,
        "requested_units": units,
        "batch": 1,
        "cu_mask": hex((1 << units) - 1),
        "p50_s": p50 if p50 is not None else 0.2 + 8.0 / units,
        "cv": cv,
        "escalations": 0,
        "samples": 30,
        "saturating_regime": saturating,
        "quota_monotone": True,
        "meets_cv_threshold": cv <= 0.05,
        "canonical_eligible": eligible,
        "device_name": "AMD Radeon AI PRO R9700",
        "gcn_arch": "gfx1201",
        "amdgpu_driver": "6.19.14.31400000",
        "rocm": "7.2",
        "torch": "2.9.1",
        "model_revision": "462165984030",
        "schema_version": "burstserve.amd-profile-cell/v1",
        "cu_mask_attestation": {
            "readback": {"mask": hex((1 << units) - 1)},
            "readback_matches_request": True,
        },
    }
    row.update(overrides)
    return row


def _rows():
    return [_row(u) for u in verify.REQUIRED_QUOTAS]


class NotMeasuredTest(unittest.TestCase):
    """An absent measurement must block acceptance exactly like a failure.

    This is the property that keeps a gate from being accepted on evidence
    nobody produced, so each clause that depends on an external artefact is
    checked with that artefact absent.
    """

    def test_absent_artefacts_are_not_measured_rather_than_passing(self):
        for name, fn in (
            ("corun", verify.check_corun),
            ("residency", verify.check_residency),
            ("transition", verify.check_transition),
            ("cold model", verify.check_cold_model),
        ):
            with self.subTest(name):
                self.assertEqual(fn(None)["status"], verify.NOT_MEASURED)

    def test_not_measured_is_distinct_from_pass_and_from_fail(self):
        self.assertNotEqual(verify.NOT_MEASURED, verify.PASS)
        self.assertNotEqual(verify.NOT_MEASURED, verify.FAIL)

    def test_a_report_present_but_without_its_number_is_not_measured(self):
        self.assertEqual(
            verify.check_transition({"note": "ran"})["status"],
            verify.NOT_MEASURED,
        )
        self.assertEqual(
            verify.check_cold_model({"note": "ran"})["status"],
            verify.NOT_MEASURED,
        )


class ProducerConsumerAgreementTest(unittest.TestCase):
    """The verifier must read fields the producers actually write.

    Every fixture in this file is hand-written, so it proves the verifier is
    self-consistent and nothing more. A field renamed in the producer would
    leave the fixtures passing while the clause reads None off a real row --
    which, for most of these clauses, reads as a failure whose cause is
    invisible. So the field names are extracted from the producer sources
    and compared, rather than trusted.
    """

    SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

    def _produced_row_fields(self) -> set[str]:
        import ast

        produced: set[str] = set()
        for name in ("amd_profile_cell.py", "run_amd_gate_b.py"):
            tree = ast.parse((self.SCRIPTS / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                # keys of dict literals, e.g. record = {"p50_s": ...}
                if isinstance(node, ast.Dict):
                    produced.update(
                        key.value for key in node.keys
                        if isinstance(key, ast.Constant)
                        and isinstance(key.value, str)
                    )
                # subscript assignment, e.g. row["saturating_regime"] = ...
                elif isinstance(node, ast.Assign):
                    for target in node.targets:
                        if (
                            isinstance(target, ast.Subscript)
                            and isinstance(target.slice, ast.Constant)
                            and isinstance(target.slice.value, str)
                        ):
                            produced.add(target.slice.value)
        return produced

    def test_every_consumed_row_field_is_produced_somewhere(self):
        produced = self._produced_row_fields()
        missing = [
            field for field in verify.ROW_FIELDS_CONSUMED
            if field not in produced
        ]
        self.assertEqual(
            missing, [],
            f"the verifier reads fields no producer writes: {missing}",
        )

    def test_every_consumed_header_field_is_produced(self):
        produced = self._produced_row_fields()
        missing = [
            field for field in verify.HEADER_FIELDS_CONSUMED
            if field not in produced
        ]
        self.assertEqual(missing, [], f"header fields never written: {missing}")

    def test_the_fixtures_carry_every_field_the_verifier_consumes(self):
        """A fixture missing a field would exercise the absent-value path."""
        row = _row(16)
        missing = [
            field for field in verify.ROW_FIELDS_CONSUMED if field not in row
        ]
        self.assertEqual(missing, [], f"fixture is missing: {missing}")

    def test_the_extraction_would_notice_a_rename(self):
        """The comparison above must be able to fail."""
        produced = self._produced_row_fields()
        self.assertNotIn("definitely_not_a_field_name", produced)

    def _keys_written_by(self, script: str) -> set[str]:
        import ast

        keys: set[str] = set()
        tree = ast.parse((self.SCRIPTS / script).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                keys.update(k.value for k in node.keys
                            if isinstance(k, ast.Constant)
                            and isinstance(k.value, str))
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "update":
                for argument in node.args:
                    if isinstance(argument, ast.Dict):
                        keys.update(k.value for k in argument.keys
                                    if isinstance(k, ast.Constant)
                                    and isinstance(k.value, str))
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Subscript) and isinstance(
                        target.slice, ast.Constant
                    ) and isinstance(target.slice.value, str):
                        keys.add(target.slice.value)
        return keys

    def test_the_residency_clause_reads_fields_that_script_writes(self):
        """The defect this catches actually happened.

        The residency script gained a duration_upper_bound branch that
        emits no "fit" key, and the clause went on reading report["fit"] --
        so the first real run of the verifier died with a KeyError instead
        of judging anything.
        """
        produced = self._keys_written_by("run_amd_residency.py")
        for field in ("status", "basis", "weight_bytes", "tolerance_bytes",
                      "zero_weight_traffic", "upper_bound_bytes_per_rotation",
                      "copies_per_rotation", "bound_bandwidth_bps"):
            with self.subTest(field):
                self.assertIn(field, produced)

    def test_the_corun_clause_reads_fields_that_script_writes(self):
        produced = self._keys_written_by("run_amd_corun.py")
        for field in ("status", "masks", "disjoint", "overlap",
                      "sufficient_overlap", "overlap_seconds", "externality",
                      "overlap_fraction_of_longer_window"):
            with self.subTest(field):
                self.assertIn(field, produced)


class ResidencyBasisTest(unittest.TestCase):
    """A bound and a measurement are both real, and are not the same."""

    def _report(self, **overrides):
        report = {"status": "ok", "basis": "duration_upper_bound",
                  "weight_bytes": 6_937_676_966,
                  "tolerance_bytes": 69_376_769,
                  "upper_bound_bytes_per_rotation": 27_320_000,
                  "copies_per_rotation": 0.0,
                  "bound_bandwidth_bps": 28.28e9,
                  "zero_weight_traffic": True}
        report.update(overrides)
        return report

    def test_a_bound_that_clears_the_tolerance_passes(self):
        result = verify.check_residency(self._report())
        self.assertEqual(result["status"], verify.PASS)
        self.assertEqual(result["detail"]["basis"], "duration_upper_bound")

    def test_a_bound_that_exceeds_the_tolerance_fails(self):
        result = verify.check_residency(
            self._report(upper_bound_bytes_per_rotation=7e9,
                         zero_weight_traffic=False))
        self.assertEqual(result["status"], verify.FAIL)

    def test_a_measured_byte_count_is_accepted_too(self):
        result = verify.check_residency({
            "status": "ok", "basis": "measured_bytes",
            "fit": {"bytes_per_rotation": 1000.0},
            "weight_bytes": 6_937_676_966, "tolerance_bytes": 69_376_769,
            "zero_weight_traffic": True})
        self.assertEqual(result["status"], verify.PASS)
        self.assertEqual(result["detail"]["basis"], "measured_bytes")

    def test_which_basis_was_used_is_always_recorded(self):
        """A bound must not be reportable as if it were a measurement."""
        for basis in ("duration_upper_bound", "measured_bytes"):
            with self.subTest(basis):
                report = (self._report() if basis == "duration_upper_bound"
                          else {"status": "ok", "basis": "measured_bytes",
                                "fit": {"bytes_per_rotation": 0.0},
                                "weight_bytes": 1, "tolerance_bytes": 1,
                                "zero_weight_traffic": True})
                self.assertEqual(
                    verify.check_residency(report)["detail"]["basis"], basis
                )

    def test_an_unknown_basis_is_not_measured_rather_than_passing(self):
        result = verify.check_residency(self._report(basis="something_new"))
        self.assertEqual(result["status"], verify.NOT_MEASURED)


class QuotaCoverageTest(unittest.TestCase):
    def test_the_full_declared_quota_list_passes(self):
        self.assertEqual(
            verify.check_quota_coverage({"quotas": verify.REQUIRED_QUOTAS},
                                        _rows())["status"],
            verify.PASS,
        )

    def test_a_missing_quota_point_fails(self):
        rows = [r for r in _rows() if r["requested_units"] != 20]
        result = verify.check_quota_coverage({}, rows)
        self.assertEqual(result["status"], verify.FAIL)
        self.assertEqual(result["detail"]["missing_quotas"], [20])

    def test_a_cell_that_carried_no_mask_fails(self):
        rows = _rows()
        rows[3]["cu_mask"] = None
        self.assertEqual(
            verify.check_quota_coverage({}, rows)["status"], verify.FAIL
        )


class SaturationClauseTest(unittest.TestCase):
    def test_an_unlabelled_cell_fails(self):
        rows = _rows()
        rows[2]["saturating_regime"] = None
        self.assertEqual(verify.check_saturation(rows)["status"], verify.FAIL)

    def test_a_non_saturating_cell_is_reported_but_does_not_fail_the_clause(self):
        """The clause is that the regime is recorded, not that it is saturating.

        A non-saturating cell is a legitimate measurement; what the plan
        forbids is an entry whose regime nobody wrote down.
        """
        rows = _rows()
        rows[0]["saturating_regime"] = False
        result = verify.check_saturation(rows)
        self.assertEqual(result["status"], verify.PASS)
        self.assertIn(rows[0]["requested_units"],
                      result["detail"]["not_saturating"])


class CvClauseTest(unittest.TestCase):
    def test_a_cell_over_the_threshold_fails(self):
        rows = _rows()
        rows[4]["cv"] = 0.075
        result = verify.check_cv(rows)
        self.assertEqual(result["status"], verify.FAIL)
        self.assertEqual(result["detail"]["cells_over_threshold"][0]["cv"],
                         0.075)

    def test_a_cell_that_did_not_record_its_cv_fails(self):
        """The clause requires the achieved CV, not a claim of 30 samples."""
        rows = _rows()
        del rows[1]["cv"]
        self.assertEqual(verify.check_cv(rows)["status"], verify.FAIL)

    def test_a_cell_that_did_not_record_its_escalations_fails(self):
        rows = _rows()
        del rows[1]["escalations"]
        self.assertEqual(verify.check_cv(rows)["status"], verify.FAIL)


class MapeClauseTest(unittest.TestCase):
    def test_amdahl_shaped_data_predicts_its_held_out_points(self):
        result = verify.check_holdout_mape(_rows(), [12, 20])
        self.assertEqual(result["status"], verify.PASS)
        self.assertLess(result["detail"]["mape"], 0.10)

    def test_data_the_model_cannot_describe_fails(self):
        rows = _rows()
        for row in rows:
            if row["requested_units"] == 20:
                row["p50_s"] = 40.0
        self.assertEqual(
            verify.check_holdout_mape(rows, [20])["status"], verify.FAIL
        )

    def test_too_few_eligible_cells_is_not_measured_rather_than_passing(self):
        rows = _rows()[:3]
        self.assertEqual(
            verify.check_holdout_mape(rows, [12])["status"],
            verify.NOT_MEASURED,
        )

    def test_ineligible_cells_are_excluded_from_the_fit(self):
        """Cells that failed CV or saturation must not prop up the score."""
        rows = _rows()
        for row in rows:
            if row["requested_units"] in (4, 8):
                row["canonical_eligible"] = False
        result = verify.check_holdout_mape(rows, [20])
        self.assertNotIn(4, result["detail"]["model"]["fitted_quotas"])
        self.assertNotIn(8, result["detail"]["model"]["fitted_quotas"])


class MetadataClauseTest(unittest.TestCase):
    def test_a_complete_cell_passes(self):
        self.assertEqual(verify.check_metadata(_rows())["status"], verify.PASS)

    def test_a_missing_readback_fails(self):
        rows = _rows()
        rows[0]["cu_mask_attestation"]["readback"]["mask"] = None
        self.assertEqual(verify.check_metadata(rows)["status"], verify.FAIL)

    def test_a_readback_that_disagreed_with_the_request_fails(self):
        rows = _rows()
        rows[0]["cu_mask_attestation"]["readback_matches_request"] = False
        self.assertEqual(verify.check_metadata(rows)["status"], verify.FAIL)

    def test_each_required_provenance_field_is_actually_required(self):
        for field in verify.REQUIRED_METADATA:
            with self.subTest(field):
                rows = _rows()
                rows[0][field] = None
                self.assertEqual(
                    verify.check_metadata(rows)["status"], verify.FAIL
                )


class CorunClauseTest(unittest.TestCase):
    def _report(self, **overrides):
        report = {
            "status": "ok",
            "masks": {"disjoint": True},
            "overlap": {"sufficient_overlap": True, "overlap_seconds": 170.0,
                        "a": {"externality": 0.02}, "b": {"externality": 0.03}},
        }
        report.update(overrides)
        return report

    def test_a_genuinely_overlapping_disjoint_pair_passes(self):
        self.assertEqual(
            verify.check_corun(self._report())["status"], verify.PASS
        )

    def test_two_processes_that_took_turns_fail(self):
        """Non-overlapping runs give the same numbers as a perfect co-run."""
        report = self._report()
        report["overlap"] = {"sufficient_overlap": False,
                             "overlap_fraction_of_longer_window": 0.04}
        self.assertEqual(verify.check_corun(report)["status"], verify.FAIL)

    def test_overlapping_masks_fail(self):
        report = self._report(masks={"disjoint": False})
        self.assertEqual(verify.check_corun(report)["status"], verify.FAIL)


class SourceStabilityTest(unittest.TestCase):
    def _run(self, header, rows):
        stable = header.get("source_revision_stable")
        return verify.clause(
            "source_tree_did_not_move_during_the_sweep",
            verify.PASS if stable
            else (verify.NOT_MEASURED if stable is None else verify.FAIL),
            {},
        )["status"]

    def test_a_moved_tree_fails_and_an_unrecorded_one_is_not_measured(self):
        self.assertEqual(self._run({"source_revision_stable": True}, []),
                         verify.PASS)
        self.assertEqual(self._run({"source_revision_stable": False}, []),
                         verify.FAIL)
        self.assertEqual(self._run({}, []), verify.NOT_MEASURED)


if __name__ == "__main__":
    unittest.main()
