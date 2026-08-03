"""Judge the Gate B-AMD clauses against the evidence that exists.

Three outcomes per clause, never two: PASS, FAIL, and NOT_MEASURED. The
third is the one that matters. A gate that reports only pass and fail has
to decide what an absent measurement is, and the convenient answer is that
it passes -- which is how a gate ends up accepted on evidence nobody
produced. Here an absent measurement blocks acceptance exactly like a
failure, and says which artefact is missing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from burstserve.provenance import canonical_json  # noqa: E402
from burstserve.quota_model import QuotaModelError, holdout_score  # noqa: E402

SCHEMA_VERSION = "burstserve.gate-b-amd-verdict/v1"

PASS, FAIL, NOT_MEASURED = "PASS", "FAIL", "NOT_MEASURED"

REQUIRED_QUOTAS = [4, 8, 12, 16, 20, 24, 28, 32]
CV_THRESHOLD = 0.05
MAPE_THRESHOLD = 0.10

# The clause names hardware, driver, ROCm and Torch separately, so the
# kernel driver is required alongside the runtime -- they move
# independently, and a masking behaviour that changed with the driver
# would otherwise be unattributable.
REQUIRED_METADATA = (
    "device_name", "gcn_arch", "amdgpu_driver", "rocm", "torch",
    "model_revision", "schema_version",
)

# Every row field this verifier reads. Declared rather than left implicit so
# that a rename in the producer is caught by a test instead of by a clause
# that silently starts reading None -- which, for most of these, would read
# as a failure whose cause is invisible.
ROW_FIELDS_CONSUMED = (
    "status", "role", "requested_units", "batch", "cu_mask", "p50_s", "cv",
    "escalations", "samples", "saturating_regime", "canonical_eligible",
    "cu_mask_attestation",
) + REQUIRED_METADATA

HEADER_FIELDS_CONSUMED = (
    "record", "model", "source_revision", "source_revision_after",
    "source_revision_stable",
)


def load_table(path: Path) -> tuple[dict, list[dict]]:
    header, rows = None, []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("record") == "header":
            header = record
        else:
            rows.append(record)
    if header is None:
        raise ValueError(f"{path} has no header record")
    return header, rows


def clause(name: str, status: str, detail) -> dict:
    return {"clause": name, "status": status, "detail": detail}


def check_quota_coverage(header, rows) -> dict:
    if header is None:
        return clause("quota_list_and_per_process_mask", NOT_MEASURED,
                      "no quota table")
    ok = [r for r in rows if r.get("status") == "ok"]
    measured = sorted({r["requested_units"] for r in ok
                       if r.get("role") == "canonical"})
    missing = [q for q in REQUIRED_QUOTAS if q not in measured]
    # The mask has to be per-process, which is what having it in the cell's
    # own environment shows; a cell that inherited it from the driver would
    # be a different arrangement.
    unmasked = [
        r["requested_units"] for r in ok
        if not r.get("cu_mask")
    ]
    if missing or unmasked:
        return clause("quota_list_and_per_process_mask", FAIL,
                      {"missing_quotas": missing,
                       "cells_without_a_mask": unmasked})
    return clause("quota_list_and_per_process_mask", PASS,
                  {"quotas": measured})


def check_saturation(rows) -> dict:
    canonical = [r for r in rows
                 if r.get("status") == "ok" and r.get("role") == "canonical"]
    if not canonical:
        return clause("every_cell_records_its_regime", NOT_MEASURED,
                      "no canonical cells")
    unlabelled = [r["requested_units"] for r in canonical
                  if r.get("saturating_regime") is None]
    non_saturating = [r["requested_units"] for r in canonical
                      if r.get("saturating_regime") is False]
    if unlabelled:
        return clause("every_cell_records_its_regime", FAIL,
                      {"cells_without_a_regime_label": unlabelled})
    return clause("every_cell_records_its_regime", PASS,
                  {"saturating": [r["requested_units"] for r in canonical
                                  if r["saturating_regime"]],
                   "not_saturating": non_saturating})


def check_cv(rows) -> dict:
    canonical = [r for r in rows
                 if r.get("status") == "ok" and r.get("role") == "canonical"]
    if not canonical:
        return clause("steady_state_cv_within_threshold", NOT_MEASURED,
                      "no canonical cells")
    missing_fields = [
        r["requested_units"] for r in canonical
        if "cv" not in r or "escalations" not in r
    ]
    if missing_fields:
        return clause("steady_state_cv_within_threshold", FAIL,
                      {"cells_not_recording_cv_or_escalations": missing_fields})
    over = [{"units": r["requested_units"], "cv": r["cv"],
             "escalations": r["escalations"], "samples": r["samples"]}
            for r in canonical if r["cv"] > CV_THRESHOLD]
    if over:
        return clause("steady_state_cv_within_threshold", FAIL,
                      {"cells_over_threshold": over})
    return clause("steady_state_cv_within_threshold", PASS,
                  {"worst_cv": max(r["cv"] for r in canonical),
                   "total_escalations": sum(r["escalations"]
                                            for r in canonical)})


def check_holdout_mape(rows, holdout) -> dict:
    eligible = [r for r in rows
                if r.get("status") == "ok" and r.get("role") == "canonical"
                and r.get("canonical_eligible")]
    if len(eligible) < 4:
        return clause("held_out_solo_p50_mape", NOT_MEASURED,
                      {"canonical_eligible_cells": len(eligible),
                       "reason": "too few eligible cells to fit and hold out"})
    points = [(r["requested_units"], r["p50_s"]) for r in eligible]
    available = {q for q, _ in points}
    chosen = [q for q in holdout if q in available]
    if not chosen:
        return clause("held_out_solo_p50_mape", NOT_MEASURED,
                      {"requested_holdout": holdout,
                       "available_quotas": sorted(available)})
    try:
        score = holdout_score(points, chosen)
    except QuotaModelError as error:
        return clause("held_out_solo_p50_mape", NOT_MEASURED, str(error))
    status = PASS if score["mape"] <= MAPE_THRESHOLD else FAIL
    return clause("held_out_solo_p50_mape", status, score)


def check_metadata(rows) -> dict:
    ok = [r for r in rows if r.get("status") == "ok"]
    if not ok:
        return clause("profiles_carry_full_provenance", NOT_MEASURED,
                      "no cells")
    incomplete = []
    for row in ok:
        missing = [field for field in REQUIRED_METADATA if not row.get(field)]
        attestation = row.get("cu_mask_attestation") or {}
        # The readback is the AMD-only clause: request and readback both.
        if attestation.get("readback", {}).get("mask") is None:
            missing.append("cu_mask_readback")
        if attestation.get("readback_matches_request") is not True:
            missing.append("readback_matches_request")
        if missing:
            incomplete.append({"units": row.get("requested_units"),
                               "batch": row.get("batch"), "missing": missing})
    if incomplete:
        return clause("profiles_carry_full_provenance", FAIL,
                      {"incomplete_cells": incomplete})
    return clause("profiles_carry_full_provenance", PASS,
                  {"cells": len(ok)})


def check_corun(report) -> dict:
    if report is None:
        return clause("corun_is_two_disjointly_masked_processes", NOT_MEASURED,
                      "no co-run report")
    if report.get("status") != "ok":
        return clause("corun_is_two_disjointly_masked_processes", FAIL,
                      {"status": report.get("status"),
                       "failed_cells": report.get("failed_cells")})
    overlap = report.get("overlap") or {}
    if not report.get("masks", {}).get("disjoint"):
        return clause("corun_is_two_disjointly_masked_processes", FAIL,
                      "the two masks overlap")
    if not overlap.get("sufficient_overlap"):
        return clause("corun_is_two_disjointly_masked_processes", FAIL,
                      {"reason": "the two processes did not overlap enough to "
                                 "have contended",
                       "overlap_fraction":
                           overlap.get("overlap_fraction_of_longer_window")})
    return clause("corun_is_two_disjointly_masked_processes", PASS,
                  {"overlap_seconds": overlap.get("overlap_seconds"),
                   "externality": {k: overlap.get(k, {}).get("externality")
                                   for k in ("a", "b")}})


def check_residency(report) -> dict:
    """Accept either a measured byte count or a bound on it.

    rocprofv3 reports no copy sizes on this platform, so the bytes may only
    be available as an upper bound from copy duration. A bound that clears
    the tolerance is a real pass -- the true value is at most the bound --
    but it is a weaker claim than a measurement, so which one was used is
    recorded rather than flattened away.
    """
    name = "resident_rotation_moves_no_weight_bytes"
    if report is None:
        return clause(name, NOT_MEASURED, "no residency report")
    if report.get("status") != "ok":
        return clause(name, NOT_MEASURED, {"status": report.get("status")})

    basis = report.get("basis")
    detail = {"basis": basis,
              "weight_bytes": report.get("weight_bytes"),
              "tolerance_bytes": report.get("tolerance_bytes")}
    if basis == "measured_bytes":
        detail["bytes_per_rotation"] = report["fit"]["bytes_per_rotation"]
    elif basis == "duration_upper_bound":
        detail["upper_bound_bytes_per_rotation"] = (
            report["upper_bound_bytes_per_rotation"])
        detail["copies_per_rotation"] = report.get("copies_per_rotation")
        detail["bound_bandwidth_bps"] = report.get("bound_bandwidth_bps")
    else:
        return clause(name, NOT_MEASURED,
                      {"reason": "unrecognised basis", "basis": basis})
    if report.get("zero_weight_traffic") is None:
        return clause(name, NOT_MEASURED, detail)
    return clause(name, PASS if report["zero_weight_traffic"] else FAIL, detail)


def check_transition(report) -> dict:
    if report is None:
        return clause("transition_prediction_mape", NOT_MEASURED,
                      "no transition report")
    mape = report.get("mape")
    if mape is None:
        return clause("transition_prediction_mape", NOT_MEASURED,
                      "report carries no mape")
    status = PASS if mape <= MAPE_THRESHOLD else FAIL
    return clause("transition_prediction_mape", status, report)


def check_cold_model(report) -> dict:
    if report is None:
        return clause("cold_model_uses_missing_bytes_and_measured_bandwidth",
                      NOT_MEASURED, "no cold-model report")
    mape = report.get("mape")
    if mape is None:
        return clause("cold_model_uses_missing_bytes_and_measured_bandwidth",
                      NOT_MEASURED, "report carries no mape")
    status = PASS if mape <= MAPE_THRESHOLD else FAIL
    return clause("cold_model_uses_missing_bytes_and_measured_bandwidth",
                  status, report)


def _maybe(path: str | None):
    if not path:
        return None
    candidate = Path(path)
    if not candidate.is_file():
        return None
    return json.loads(candidate.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--table", required=True,
                        help="the quota table JSONL from run_amd_gate_b.py")
    parser.add_argument("--corun")
    parser.add_argument("--residency")
    parser.add_argument("--transition")
    parser.add_argument("--cold-model")
    parser.add_argument("--holdout", default="12,20")
    parser.add_argument("--out")
    args = parser.parse_args()

    header, rows = load_table(Path(args.table))
    holdout = [int(q) for q in args.holdout.split(",") if q.strip()]

    clauses = [
        check_quota_coverage(header, rows),
        check_saturation(rows),
        check_cv(rows),
        check_holdout_mape(rows, holdout),
        check_transition(_maybe(args.transition)),
        check_residency(_maybe(args.residency)),
        check_cold_model(_maybe(args.cold_model)),
        check_metadata(rows),
        check_corun(_maybe(args.corun)),
    ]

    # A source tree that moved mid-sweep makes every row above describe a
    # revision the header no longer names, so it is its own clause rather
    # than a footnote.
    stable = header.get("source_revision_stable")
    clauses.append(clause(
        "source_tree_did_not_move_during_the_sweep",
        PASS if stable else (NOT_MEASURED if stable is None else FAIL),
        {"source_revision": header.get("source_revision"),
         "source_revision_after": header.get("source_revision_after")},
    ))

    accepted = all(c["status"] == PASS for c in clauses)
    verdict = {
        "schema_version": SCHEMA_VERSION,
        "model": header.get("model"),
        "source_revision": header.get("source_revision"),
        "accepted": accepted,
        "clauses": clauses,
        "not_measured": [c["clause"] for c in clauses
                         if c["status"] == NOT_MEASURED],
        "failed": [c["clause"] for c in clauses if c["status"] == FAIL],
    }

    width = max(len(c["clause"]) for c in clauses)
    print(f"=== Gate B-AMD: {header.get('model')} @ "
          f"{str(header.get('source_revision'))[:12]} ===")
    for entry in clauses:
        print(f"  {entry['status']:12s}  {entry['clause']:<{width}}")
    print(f"\nACCEPTED: {accepted}")
    if verdict["not_measured"]:
        print(f"not measured ({len(verdict['not_measured'])}): "
              f"{', '.join(verdict['not_measured'])}")
    if verdict["failed"]:
        print(f"failed ({len(verdict['failed'])}): "
              f"{', '.join(verdict['failed'])}")

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(canonical_json(verdict) + "\n")
        print(f"verdict: {out}")
    return 0 if accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
