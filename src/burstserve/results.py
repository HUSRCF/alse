"""Deterministically aggregate provenance-complete experiment run directories.

The aggregator is intentionally tolerant of failed and interrupted experiments:
every immediate child directory is represented unless a valid manifest is
explicitly excluded by a requested filter. Corrupt identities and documents are
reported as invalid rows instead of disappearing from the aggregate.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

from .provenance import RunManifest, canonical_json, write_json_atomic


RESULTS_SCHEMA_VERSION = "burstserve.results-aggregate/v1"

_STATUSES = (
    "accepted",
    "completed",
    "failed",
    "incomplete",
    "invalid",
    "timed_out",
)
_SUMMARY_METRIC_KEYS = (
    "runnable",
    "n_urgent",
    "n_video",
    "elapsed_s",
    "urgent_mean_s",
    "urgent_p50_s",
    "urgent_p95_s",
    "urgent_p99_s",
    "video_peak_GB",
    "peak_alloc_GB_incl_ballast",
    "n_rollbacks",
    "recompute_steps",
    "video_restarts",
    "oom",
    "oom_phase",
)


def _load_json_object(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as source:
        value = json.load(source)
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object in {path}")
    # Reject non-portable JSON values such as NaN and detach mutable input.
    normalized = json.loads(canonical_json(value))
    if not isinstance(normalized, dict):  # Defensive; canonical round-trip above.
        raise ValueError(f"expected a JSON object in {path}")
    return normalized


def _optional_int(value: Any, *, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer or null")
    return value


def _optional_bool(value: Any, *, field: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean or null")
    return value


def _status(
    *,
    errors: list[str],
    outcome_present: bool,
    exit_code: int | None,
    timed_out: bool | None,
    semantic_accepted: bool | None,
) -> str:
    if errors:
        return "invalid"
    if not outcome_present or exit_code is None:
        return "incomplete"
    if timed_out is True:
        return "timed_out"
    if exit_code != 0 or semantic_accepted is False:
        return "failed"
    if semantic_accepted is True:
        return "accepted"
    # Older outcome schemas may record successful completion without the
    # semantic smoke verdict. Preserve that distinction instead of inferring it.
    return "completed"


def _read_run(run_directory: Path) -> tuple[dict[str, Any], RunManifest | None]:
    manifest_path = run_directory / "manifest.json"
    outcome_path = run_directory / "outcome.json"
    summary_path = run_directory / "summary.json"
    errors: list[str] = []
    manifest: RunManifest | None = None
    source_revision: str | None = None
    config: dict[str, Any] | None = None

    if not manifest_path.is_file():
        errors.append("missing manifest.json")
    else:
        try:
            manifest_value = _load_json_object(manifest_path)
            manifest = RunManifest.from_dict(manifest_value)
            source_revision = manifest.source_revision
            config = manifest.config
            if manifest.run_id != run_directory.name:
                errors.append(
                    "run directory does not match manifest run_id: "
                    f"{run_directory.name!r} != {manifest.run_id!r}"
                )
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            errors.append(f"invalid manifest.json: {error}")

    outcome: dict[str, Any] | None = None
    if outcome_path.is_file():
        try:
            outcome = _load_json_object(outcome_path)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            errors.append(f"invalid outcome.json: {error}")

    exit_code: int | None = None
    timed_out: bool | None = None
    smoke_accepted: bool | None = None
    semantic_accepted: bool | None = None
    if outcome is not None:
        try:
            exit_code = _optional_int(outcome.get("exit_code"), field="exit_code")
            timed_out = _optional_bool(outcome.get("timed_out"), field="timed_out")
            smoke_accepted = _optional_bool(
                outcome.get("smoke_accepted"),
                field="smoke_accepted",
            )
            semantic_accepted = _optional_bool(
                outcome.get("accepted", smoke_accepted),
                field="accepted",
            )
        except ValueError as error:
            errors.append(f"invalid outcome.json: {error}")

    summary_metrics: dict[str, Any] = {}
    if summary_path.is_file():
        try:
            summary = _load_json_object(summary_path)
            summary_metrics = {
                key: summary[key] for key in _SUMMARY_METRIC_KEYS if key in summary
            }
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            errors.append(f"invalid summary.json: {error}")

    row = {
        "run_id": run_directory.name,
        "source_revision": source_revision,
        "config": config,
        "status": _status(
            errors=errors,
            outcome_present=outcome_path.is_file(),
            exit_code=exit_code,
            timed_out=timed_out,
            semantic_accepted=semantic_accepted,
        ),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "smoke_accepted": smoke_accepted,
        "semantic_accepted": semantic_accepted,
        "summary_metrics": summary_metrics,
        "files_present": {
            "manifest": manifest_path.is_file(),
            "outcome": outcome_path.is_file(),
            "summary": summary_path.is_file(),
        },
        "validation_errors": errors,
    }
    return row, manifest


def aggregate_runs(
    run_root: Path,
    *,
    source_revision: str | None = None,
    arm: str | None = None,
) -> dict[str, Any]:
    """Return a deterministic aggregate of immediate run directories.

    A filter excludes only valid manifests that do not match. Invalid or
    missing manifests remain visible so filtering cannot conceal provenance
    corruption.
    """

    if not run_root.is_dir():
        raise FileNotFoundError(f"run root is not a directory: {run_root}")

    rows: list[dict[str, Any]] = []
    discovered = 0
    filtered_out = 0
    for run_directory in sorted(
        (
            child
            for child in run_root.iterdir()
            if child.is_dir() and not child.name.startswith(".")
        ),
        key=lambda path: path.name,
    ):
        discovered += 1
        row, manifest = _read_run(run_directory)
        if manifest is not None and not row["validation_errors"]:
            if source_revision is not None and manifest.source_revision != source_revision:
                filtered_out += 1
                continue
            if arm is not None and manifest.config.get("arm") != arm:
                filtered_out += 1
                continue
        rows.append(row)

    by_status = {status: 0 for status in _STATUSES}
    for row in rows:
        by_status[str(row["status"])] += 1

    return {
        "schema_version": RESULTS_SCHEMA_VERSION,
        "filters": {
            "source_revision": source_revision,
            "arm": arm,
        },
        "counts": {
            "discovered": discovered,
            "selected": len(rows),
            "filtered_out": filtered_out,
            "by_status": by_status,
        },
        "rows": rows,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, default=Path("experiments/runs"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-revision")
    parser.add_argument("--arm")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    aggregate = aggregate_runs(
        args.run_root.resolve(),
        source_revision=args.source_revision,
        arm=args.arm,
    )
    write_json_atomic(args.output.resolve(), aggregate)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
