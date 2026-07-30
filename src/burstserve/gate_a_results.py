"""Deterministically validate a declared Gate-A0 baseline evidence matrix.

The validator is intentionally driven by a versioned evidence specification.
It never guesses which runs are formal evidence and never embeds run IDs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .provenance import EventRecord, RunManifest, canonical_json, write_json_atomic


EVIDENCE_SPEC_SCHEMA_VERSION = "burstserve.gate-a0-evidence-spec/v1"
REPORT_SCHEMA_VERSION = "burstserve.gate-a0-evidence-report/v1"
CELL_SCHEMA_VERSION = "burstserve.smid-probe-cell/v1"
OUTCOME_SCHEMA_VERSION = "burstserve.smid-probe-outcome/v1"
NATIVE_SCHEMA_VERSION = "burstserve.smid-probe-native/v1"
EXPECTED_BLOCKS = 4096
REQUIRED_GATE_A0_GPU_COUNT = 8

_EVIDENCE_FILES = (
    "manifest.json",
    "outcome.json",
    "native.json",
    "events.jsonl",
    "command.json",
    "stdout.log",
    "stderr.log",
)
_REJECTION_EVIDENCE_FILES = tuple(
    name for name in _EVIDENCE_FILES if name != "native.json"
)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_object(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as source:
        value = json.load(source)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    normalized = json.loads(canonical_json(value))
    if not isinstance(normalized, dict):  # Defensive after the canonical round trip.
        raise ValueError(f"{path} must contain a JSON object")
    return normalized


def _integer(value: Any, *, field: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    return value


def _string(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _string_list(value: Any, *, field: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    result = [_string(item, field=f"{field}[]") for item in value]
    if len(result) != len(set(result)):
        raise ValueError(f"{field} contains duplicates")
    return result


def load_evidence_spec(path: Path) -> dict[str, Any]:
    """Load and strictly normalize a Gate-A0 evidence specification.

    Schema::

      {
        "schema_version": "burstserve.gate-a0-evidence-spec/v1",
        "evidence_id": "...",
        "source_revision": "burstserve-...;libsmctrl-...",
        "seed": 1,
        "declared_gpus": [
          {"physical_gpu": 0, "gpu_uuid": "GPU-..."}
        ],
        "selected_gpu_indices": [0],
        "required_trials": [0, 1, 2],
        "excluded_runs": [
          {"run_id": "bs1-...", "reason": "exploratory pre-registration run"}
        ],
        "sealed_rejection_run_ids": ["bs1-..."]
      }
    """

    value = _json_object(path)
    if value.get("schema_version") != EVIDENCE_SPEC_SCHEMA_VERSION:
        raise ValueError(
            "unsupported evidence spec schema: "
            f"{value.get('schema_version')!r}"
        )
    evidence_id = _string(value.get("evidence_id"), field="evidence_id")
    source_revision = _string(
        value.get("source_revision"),
        field="source_revision",
    )
    seed = _integer(value.get("seed"), field="seed")

    declared_raw = value.get("declared_gpus")
    if not isinstance(declared_raw, list) or not declared_raw:
        raise ValueError("declared_gpus must be a non-empty array")
    declared_gpus: list[dict[str, Any]] = []
    for position, item in enumerate(declared_raw):
        if not isinstance(item, Mapping):
            raise ValueError(f"declared_gpus[{position}] must be an object")
        declared_gpus.append(
            {
                "physical_gpu": _integer(
                    item.get("physical_gpu"),
                    field=f"declared_gpus[{position}].physical_gpu",
                ),
                "gpu_uuid": _string(
                    item.get("gpu_uuid"),
                    field=f"declared_gpus[{position}].gpu_uuid",
                ),
            }
        )
    declared_gpus.sort(key=lambda item: int(item["physical_gpu"]))
    indices = [int(item["physical_gpu"]) for item in declared_gpus]
    uuids = [str(item["gpu_uuid"]) for item in declared_gpus]
    if len(indices) != len(set(indices)):
        raise ValueError("declared_gpus contains duplicate physical_gpu values")
    if len(uuids) != len(set(uuids)):
        raise ValueError("declared_gpus contains duplicate gpu_uuid values")

    selected_raw = value.get("selected_gpu_indices")
    if not isinstance(selected_raw, list) or not selected_raw:
        raise ValueError("selected_gpu_indices must be a non-empty array")
    selected = sorted(
        _integer(item, field="selected_gpu_indices[]") for item in selected_raw
    )
    if len(selected) != len(set(selected)):
        raise ValueError("selected_gpu_indices contains duplicates")
    undeclared = sorted(set(selected) - set(indices))
    if undeclared:
        raise ValueError(
            f"selected_gpu_indices contains undeclared GPUs: {undeclared}"
        )

    trials_raw = value.get("required_trials")
    if not isinstance(trials_raw, list) or not trials_raw:
        raise ValueError("required_trials must be a non-empty array")
    trials = sorted(_integer(item, field="required_trials[]") for item in trials_raw)
    if len(trials) != len(set(trials)):
        raise ValueError("required_trials contains duplicates")

    excluded_raw = value.get("excluded_runs")
    if not isinstance(excluded_raw, list):
        raise ValueError("excluded_runs must be an array")
    excluded: list[dict[str, str]] = []
    for position, item in enumerate(excluded_raw):
        if not isinstance(item, Mapping):
            raise ValueError(f"excluded_runs[{position}] must be an object")
        excluded.append(
            {
                "run_id": _string(
                    item.get("run_id"),
                    field=f"excluded_runs[{position}].run_id",
                ),
                "reason": _string(
                    item.get("reason"),
                    field=f"excluded_runs[{position}].reason",
                ),
            }
        )
    excluded.sort(key=lambda item: item["run_id"])
    excluded_ids = [item["run_id"] for item in excluded]
    if len(excluded_ids) != len(set(excluded_ids)):
        raise ValueError("excluded_runs contains duplicate run IDs")

    sealed = sorted(
        _string_list(
            value.get("sealed_rejection_run_ids"),
            field="sealed_rejection_run_ids",
        )
    )
    overlap = sorted(set(excluded_ids) & set(sealed))
    if overlap:
        raise ValueError(
            f"run IDs cannot be both excluded and sealed rejections: {overlap}"
        )

    return {
        "schema_version": EVIDENCE_SPEC_SCHEMA_VERSION,
        "evidence_id": evidence_id,
        "source_revision": source_revision,
        "seed": seed,
        "declared_gpus": declared_gpus,
        "selected_gpu_indices": selected,
        "required_trials": trials,
        "excluded_runs": excluded,
        "sealed_rejection_run_ids": sealed,
    }


def _file_hashes(run_directory: Path) -> dict[str, str | None]:
    return {
        name: _sha256_file(run_directory / name)
        if (run_directory / name).is_file()
        else None
        for name in _EVIDENCE_FILES
    }


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _load_event_records(path: Path) -> tuple[list[EventRecord], list[str]]:
    records: list[EventRecord] = []
    errors: list[str] = []
    try:
        with path.open("r", encoding="utf-8") as source:
            for line_number, line in enumerate(source, start=1):
                if not line.strip():
                    errors.append(f"events line {line_number} is blank")
                    continue
                try:
                    value = json.loads(line)
                    if not isinstance(value, Mapping):
                        raise TypeError("record must be an object")
                    records.append(EventRecord.from_dict(value))
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    errors.append(f"invalid events line {line_number}: {exc}")
    except OSError as exc:
        errors.append(f"invalid events.jsonl: {exc}")
    return records, errors


def _validate_event_identity(
    records: Sequence[EventRecord],
    *,
    expected_run_id: str,
    errors: list[str],
) -> None:
    sequences = [record.sequence for record in records]
    expected_sequences = list(range(len(records)))
    if sequences != expected_sequences:
        errors.append(
            f"event sequences are {sequences}, expected {expected_sequences}"
        )
    mismatched = [
        position
        for position, record in enumerate(records)
        if record.run_id != expected_run_id
    ]
    if mismatched:
        errors.append(f"events have mismatched run_id at positions {mismatched}")


def _stdout_native(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        lines = [
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except OSError as exc:
        return None, f"invalid stdout.log: {exc}"
    if len(lines) != 1:
        return (
            None,
            "stdout.log must contain exactly one non-empty JSON line, "
            f"found {len(lines)}",
        )
    try:
        value = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        return None, f"invalid native JSON in stdout.log: {exc}"
    if not isinstance(value, dict):
        return None, "native JSON in stdout.log must be an object"
    return json.loads(canonical_json(value)), None


def _flag_value(
    argv: Sequence[str],
    flag: str,
    *,
    errors: list[str],
) -> str | None:
    positions = [index for index, value in enumerate(argv) if value == flag]
    if len(positions) != 1:
        errors.append(f"command argv must contain {flag} exactly once")
        return None
    position = positions[0]
    if position + 1 >= len(argv) or argv[position + 1].startswith("--"):
        errors.append(f"command argv {flag} is missing its value")
        return None
    return argv[position + 1]


def _validate_baseline_command(
    value: Mapping[str, Any],
    *,
    expected_uuid: str,
    errors: list[str],
) -> None:
    argv_value = value.get("argv")
    if (
        not isinstance(argv_value, list)
        or not argv_value
        or any(not isinstance(item, str) or not item for item in argv_value)
    ):
        errors.append("command.argv must be a non-empty string array")
        argv: list[str] = []
    else:
        argv = list(argv_value)
    mode = _flag_value(argv, "--mode", errors=errors)
    if mode is not None and mode != "baseline":
        errors.append(f"command --mode={mode!r}, expected 'baseline'")
    iterations = _flag_value(argv, "--iterations", errors=errors)
    if iterations is not None and iterations != str(EXPECTED_BLOCKS):
        errors.append(
            f"command --iterations={iterations!r}, expected {EXPECTED_BLOCKS!r}"
        )
    for forbidden in ("--enabled-tpc", "--allow-unsupported-driver"):
        if forbidden in argv:
            errors.append(f"baseline command must not contain {forbidden}")

    environment = value.get("environment_overrides")
    if not isinstance(environment, Mapping):
        errors.append("command.environment_overrides must be an object")
        return
    expected_environment = {
        "CUDA_VISIBLE_DEVICES": expected_uuid,
        "CUDA_MPS_PIPE_DIRECTORY": "",
        "MASK_OFF": None,
    }
    for field, expected in expected_environment.items():
        if environment.get(field) != expected:
            errors.append(
                "command.environment_overrides."
                f"{field}={environment.get(field)!r}, expected {expected!r}"
            )


def _all_true_checks(value: Any, *, field: str, errors: list[str]) -> None:
    if not isinstance(value, Mapping) or not value:
        errors.append(f"{field} must be a non-empty object")
        return
    false_or_invalid = sorted(
        str(key) for key, item in value.items() if item is not True
    )
    if false_or_invalid:
        errors.append(f"{field} has non-true checks: {false_or_invalid}")


def _candidate_cell(
    manifest_value: Mapping[str, Any],
    *,
    source_revision: str,
    seed: int,
    declared_indices: set[int],
    required_trials: set[int],
) -> tuple[int, int] | None:
    config = manifest_value.get("config")
    if not isinstance(config, Mapping):
        return None
    if manifest_value.get("source_revision") != source_revision:
        return None
    if manifest_value.get("seed") != seed or config.get("seed") != seed:
        return None
    if config.get("mode") != "baseline":
        return None
    gpu = config.get("physical_gpu")
    trial = config.get("trial")
    if (
        isinstance(gpu, bool)
        or not isinstance(gpu, int)
        or isinstance(trial, bool)
        or not isinstance(trial, int)
    ):
        return None
    if gpu not in declared_indices or trial not in required_trials:
        return None
    return gpu, trial


def _validate_baseline_run(
    run_directory: Path,
    *,
    expected_source_revision: str,
    expected_seed: int,
    expected_gpu: int,
    expected_trial: int,
    expected_uuid: str,
) -> dict[str, Any]:
    errors: list[str] = []
    hashes = _file_hashes(run_directory)
    missing_files = [
        name for name in _EVIDENCE_FILES if hashes.get(name) is None
    ]
    if missing_files:
        errors.append(f"missing evidence files: {missing_files}")
    manifest_value: dict[str, Any] | None = None
    outcome: dict[str, Any] | None = None
    native: dict[str, Any] | None = None
    manifest: RunManifest | None = None
    key_metrics: dict[str, Any] = {
        "observed_sm_count": None,
        "device_sm_count": None,
        "coverage": None,
        "reported_blocks": None,
        "observed_blocks": None,
        "native_blocks": None,
    }

    try:
        manifest_value = _json_object(run_directory / "manifest.json")
        manifest = RunManifest.from_dict(manifest_value)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"invalid manifest.json: {exc}")
    if manifest is not None:
        if manifest.run_id != run_directory.name:
            errors.append("manifest run_id does not match directory name")
        if manifest.source_revision != expected_source_revision:
            errors.append("source_revision does not match evidence spec")
        if manifest.seed != expected_seed:
            errors.append("manifest seed does not match evidence spec")
        config = manifest.config
        expected_config = {
            "schema_version": CELL_SCHEMA_VERSION,
            "mode": "baseline",
            "physical_gpu": expected_gpu,
            "trial": expected_trial,
            "seed": expected_seed,
            "iterations": EXPECTED_BLOCKS,
            "allow_busy_gpu": False,
            "experimental_allow_unsupported_driver": False,
            "experimental_mask_off": None,
        }
        for field, expected in expected_config.items():
            if config.get(field) != expected:
                errors.append(
                    f"config.{field}={config.get(field)!r}, expected {expected!r}"
                )
        if manifest.metadata.get("purpose") != "phase1-libsmctrl-gate-a":
            errors.append("manifest purpose is not phase1-libsmctrl-gate-a")

        environment = manifest.environment
        initial_gpu = environment.get("selected_gpu_initial_preflight")
        launch_gpu = environment.get("selected_gpu_launch_preflight")
        for field, gpu_value in (
            ("initial preflight", initial_gpu),
            ("launch preflight", launch_gpu),
        ):
            if not isinstance(gpu_value, Mapping):
                errors.append(f"missing selected GPU {field}")
            elif gpu_value.get("uuid") != expected_uuid:
                errors.append(f"selected GPU {field} UUID mismatch")
        binary = environment.get("native_binary")
        build = environment.get("native_build")
        binary_sha = binary.get("sha256") if isinstance(binary, Mapping) else None
        build_sha = build.get("sha256") if isinstance(build, Mapping) else None
        if not _is_sha256(binary_sha):
            errors.append("native binary SHA256 is missing or malformed")
        if (
            not isinstance(build, Mapping)
            or build.get("found") is not True
            or not _is_sha256(build_sha)
        ):
            errors.append("native build stamp SHA256 is missing or malformed")
        elif not isinstance(build.get("content"), str):
            errors.append("native build stamp content is missing")
        elif _sha256_bytes(build["content"].encode("utf-8")) != build_sha:
            errors.append("native build stamp content SHA256 mismatch")

        gate_record = config.get("gate_manifest")
        if not isinstance(gate_record, Mapping):
            errors.append("config.gate_manifest must be an object")
        else:
            gate_content = gate_record.get("content")
            gate_sha = gate_record.get("sha256")
            if not isinstance(gate_content, Mapping):
                errors.append("config.gate_manifest.content must be an object")
            elif not _is_sha256(gate_sha):
                errors.append("Gate-A manifest content SHA256 is missing or malformed")
            elif (
                _sha256_bytes(canonical_json(gate_content).encode("utf-8"))
                != gate_sha
            ):
                errors.append("Gate-A manifest content SHA256 mismatch")
    else:
        binary_sha = None
        build_sha = None

    try:
        outcome = _json_object(run_directory / "outcome.json")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"invalid outcome.json: {exc}")
    if outcome is not None:
        scalar_expectations = {
            "schema_version": OUTCOME_SCHEMA_VERSION,
            "exit_code": 0,
            "process_exit_code": 0,
            "timed_out": False,
            "native_output_found": True,
            "native_status": "ok",
            "driver_policy_permitted": True,
            "manifest_policy_permitted": True,
            "preflight_permitted": True,
            "local_probe_passed": True,
            "requires_matrix_validation": False,
            "accepted": True,
        }
        for field, expected in scalar_expectations.items():
            if outcome.get(field) != expected:
                errors.append(
                    f"outcome.{field}={outcome.get(field)!r}, expected {expected!r}"
                )
        for field in (
            "driver_policy",
            "manifest_policy",
            "safety_policy",
            "semantic_acceptance",
        ):
            _all_true_checks(outcome.get(field), field=f"outcome.{field}", errors=errors)
        post = outcome.get("post_health")
        if not isinstance(post, Mapping):
            errors.append("outcome.post_health must be an object")
        else:
            _all_true_checks(
                post.get("checks"),
                field="outcome.post_health.checks",
                errors=errors,
            )
            post_gpu = post.get("gpu")
            if not isinstance(post_gpu, Mapping) or post_gpu.get("uuid") != expected_uuid:
                errors.append("post-health GPU UUID mismatch")
        metrics = outcome.get("semantic_metrics")
        if not isinstance(metrics, Mapping):
            errors.append("outcome.semantic_metrics must be an object")
        else:
            key_metrics.update(
                {
                    "observed_sm_count": metrics.get("observed_sm_count"),
                    "device_sm_count": metrics.get("device_sm_count"),
                    "coverage": metrics.get("sm_coverage_ratio"),
                    "reported_blocks": metrics.get("reported_blocks"),
                    "observed_blocks": metrics.get("observed_blocks"),
                }
            )
            if metrics.get("device_uuid") != expected_uuid:
                errors.append("semantic metric GPU UUID mismatch")
            if metrics.get("reported_blocks") != EXPECTED_BLOCKS:
                errors.append("semantic reported_blocks is not 4096")
            if metrics.get("observed_blocks") != EXPECTED_BLOCKS:
                errors.append("semantic observed_blocks is not 4096")
            if metrics.get("reported_iterations") != EXPECTED_BLOCKS:
                errors.append("semantic reported_iterations is not 4096")
            coverage = metrics.get("sm_coverage_ratio")
            minimum_coverage: Any = None
            if manifest is not None:
                gate_record = manifest.config.get("gate_manifest")
                if isinstance(gate_record, Mapping):
                    content = gate_record.get("content")
                    if isinstance(content, Mapping):
                        baseline = content.get("baseline")
                        if isinstance(baseline, Mapping):
                            minimum_coverage = baseline.get(
                                "minimum_sm_coverage_fraction"
                            )
            if (
                isinstance(coverage, bool)
                or not isinstance(coverage, (int, float))
                or isinstance(minimum_coverage, bool)
                or not isinstance(minimum_coverage, (int, float))
                or float(coverage) < float(minimum_coverage)
            ):
                errors.append("semantic SM coverage is below manifest minimum")

    try:
        native = _json_object(run_directory / "native.json")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"invalid native.json: {exc}")
    if native is not None:
        key_metrics["native_blocks"] = native.get("blocks")
        if native.get("schema_version") != NATIVE_SCHEMA_VERSION:
            errors.append("native schema version mismatch")
        if native.get("status") != "ok" or native.get("mode") != "baseline":
            errors.append("native status/mode mismatch")
        if native.get("blocks") != EXPECTED_BLOCKS:
            errors.append("native blocks is not 4096")
        if native.get("iterations") != EXPECTED_BLOCKS:
            errors.append("native iterations is not 4096")
        device = native.get("device")
        if not isinstance(device, Mapping) or device.get("uuid") != expected_uuid:
            errors.append("native GPU UUID mismatch")
            native_device_sm_count: Any = None
        else:
            native_device_sm_count = device.get("sm_count")
            if (
                isinstance(native_device_sm_count, bool)
                or not isinstance(native_device_sm_count, int)
                or native_device_sm_count <= 0
            ):
                errors.append("native device SM count is invalid")
        histogram = native.get("observed_histogram")
        if not isinstance(histogram, Mapping):
            errors.append("native histogram must be an object")
        else:
            counts = list(histogram.values())
            if any(
                isinstance(count, bool)
                or not isinstance(count, int)
                or count < 0
                for count in counts
            ):
                errors.append("native histogram counts are invalid")
            elif sum(counts) != EXPECTED_BLOCKS:
                errors.append("native histogram count is not 4096")
            sm_ids: list[int] = []
            for sm_id in histogram:
                try:
                    parsed_sm_id = int(sm_id)
                except (TypeError, ValueError):
                    errors.append(f"native histogram SM ID is invalid: {sm_id!r}")
                    continue
                if str(parsed_sm_id) != str(sm_id) or parsed_sm_id < 0:
                    errors.append(f"native histogram SM ID is invalid: {sm_id!r}")
                    continue
                sm_ids.append(parsed_sm_id)
            if (
                isinstance(native_device_sm_count, int)
                and not isinstance(native_device_sm_count, bool)
                and any(sm_id >= native_device_sm_count for sm_id in sm_ids)
            ):
                errors.append("native histogram SM ID is outside the device range")

            if not any(
                isinstance(count, bool)
                or not isinstance(count, int)
                or count < 0
                for count in counts
            ):
                observed_sm_count = sum(count > 0 for count in counts)
                if key_metrics["observed_sm_count"] != observed_sm_count:
                    errors.append(
                        "semantic observed_sm_count does not match native histogram"
                    )
                if key_metrics["device_sm_count"] != native_device_sm_count:
                    errors.append(
                        "semantic device_sm_count does not match native device"
                    )
                if (
                    isinstance(native_device_sm_count, int)
                    and not isinstance(native_device_sm_count, bool)
                    and native_device_sm_count > 0
                ):
                    native_coverage = observed_sm_count / native_device_sm_count
                    recorded_coverage = key_metrics["coverage"]
                    if (
                        isinstance(recorded_coverage, bool)
                        or not isinstance(recorded_coverage, (int, float))
                        or abs(float(recorded_coverage) - native_coverage) > 1e-12
                    ):
                        errors.append(
                            "semantic SM coverage does not match native histogram"
                        )
        if key_metrics["reported_blocks"] != native.get("blocks"):
            errors.append("semantic reported_blocks does not match native blocks")

    stdout_native, stdout_error = _stdout_native(run_directory / "stdout.log")
    if stdout_error is not None:
        errors.append(stdout_error)
    elif native is not None and canonical_json(stdout_native) != canonical_json(native):
        errors.append("stdout native JSON does not match native.json")

    try:
        stderr = (run_directory / "stderr.log").read_bytes()
    except OSError as exc:
        errors.append(f"invalid stderr.log: {exc}")
    else:
        if stderr:
            errors.append("stderr.log must be empty for an accepted baseline")

    try:
        command = _json_object(run_directory / "command.json")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"invalid command.json: {exc}")
    else:
        _validate_baseline_command(
            command,
            expected_uuid=expected_uuid,
            errors=errors,
        )

    events, event_errors = _load_event_records(run_directory / "events.jsonl")
    errors.extend(event_errors)
    _validate_event_identity(
        events,
        expected_run_id=run_directory.name,
        errors=errors,
    )
    event_types = [record.event_type for record in events]
    expected_event_types = ["run.preflight", "run.started", "run.completed"]
    if event_types != expected_event_types:
        errors.append(
            f"baseline event types are {event_types}, expected {expected_event_types}"
        )
    if (
        events
        and events[-1].event_type == "run.completed"
        and outcome is not None
        and canonical_json(events[-1].payload) != canonical_json(outcome)
    ):
        errors.append("terminal run.completed payload does not match outcome.json")

    return {
        "run_id": run_directory.name,
        "cell": {
            "physical_gpu": expected_gpu,
            "trial": expected_trial,
            "gpu_uuid": expected_uuid,
        },
        "file_sha256": hashes,
        "binary_sha256": binary_sha,
        "build_sha256": build_sha,
        "metrics": key_metrics,
        "valid": not errors,
        "validation_errors": errors,
    }


def _validate_sealed_rejection(
    run_directory: Path,
    *,
    expected_source_revision: str,
) -> dict[str, Any]:
    errors: list[str] = []
    hashes = _file_hashes(run_directory)
    missing_files = [
        name
        for name in _REJECTION_EVIDENCE_FILES
        if hashes.get(name) is None
    ]
    if missing_files:
        errors.append(f"missing sealed rejection evidence files: {missing_files}")
    manifest: RunManifest | None = None
    manifest_mode: str | None = None
    try:
        manifest = RunManifest.from_dict(_json_object(run_directory / "manifest.json"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"invalid manifest.json: {exc}")
    if manifest is not None:
        if manifest.run_id != run_directory.name:
            errors.append("manifest run_id does not match directory name")
        if manifest.source_revision != expected_source_revision:
            errors.append("source_revision does not match evidence spec")
        raw_mode = manifest.config.get("mode")
        manifest_mode = raw_mode if isinstance(raw_mode, str) else None
        if manifest_mode not in {"global", "next", "stream"}:
            errors.append("sealed rejection is not a masked mode")

    outcome: dict[str, Any] | None = None
    try:
        outcome = _json_object(run_directory / "outcome.json")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"invalid outcome.json: {exc}")
    if outcome is not None:
        expected_values = {
            "schema_version": OUTCOME_SCHEMA_VERSION,
            "exit_code": 4,
            "process_exit_code": None,
            "timed_out": False,
            "native_output_found": False,
            "native_status": None,
            "preflight_permitted": False,
            "local_probe_passed": False,
            "accepted": False,
        }
        for field, expected in expected_values.items():
            if outcome.get(field) != expected:
                errors.append(
                    f"outcome.{field}={outcome.get(field)!r}, expected {expected!r}"
                )
    if (run_directory / "native.json").exists():
        errors.append("sealed rejection unexpectedly contains native.json")

    try:
        command = _json_object(run_directory / "command.json")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"invalid command.json: {exc}")
    else:
        argv_value = command.get("argv")
        if (
            not isinstance(argv_value, list)
            or not argv_value
            or any(not isinstance(item, str) or not item for item in argv_value)
        ):
            errors.append("sealed rejection command.argv must be a string array")
            argv: list[str] = []
        else:
            argv = list(argv_value)
        command_mode = _flag_value(argv, "--mode", errors=errors)
        if command_mode not in {"global", "next", "stream"}:
            errors.append("sealed rejection command does not name a masked mode")
        if manifest_mode is not None and command_mode != manifest_mode:
            errors.append(
                "sealed rejection command mode does not match manifest mode"
            )

    stdout = run_directory / "stdout.log"
    if not stdout.is_file() or stdout.stat().st_size != 0:
        errors.append("sealed rejection stdout.log must exist and be empty")
    try:
        stderr = (run_directory / "stderr.log").read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        errors.append(f"invalid sealed rejection stderr.log: {exc}")
    else:
        if not stderr.strip() or "reject" not in stderr.lower():
            errors.append(
                "sealed rejection stderr.log must contain non-empty rejection text"
            )
    events, event_errors = _load_event_records(run_directory / "events.jsonl")
    errors.extend(event_errors)
    _validate_event_identity(
        events,
        expected_run_id=run_directory.name,
        errors=errors,
    )
    event_types = [record.event_type for record in events]
    if "run.started" in event_types:
        errors.append("sealed rejection contains run.started event")
    expected_event_types = ["run.preflight", "run.rejected"]
    if event_types != expected_event_types:
        errors.append(
            "sealed rejection event types are "
            f"{event_types}, expected {expected_event_types}"
        )
    if (
        events
        and events[-1].event_type == "run.rejected"
        and outcome is not None
        and canonical_json(events[-1].payload) != canonical_json(outcome)
    ):
        errors.append("terminal run.rejected payload does not match outcome.json")
    return {
        "run_id": run_directory.name,
        "file_sha256": hashes,
        "valid": not errors,
        "validation_errors": errors,
    }


def _excluded_record(
    run_root: Path,
    *,
    run_id: str,
    reason: str,
) -> dict[str, Any]:
    directory = run_root / run_id
    exists = directory.is_dir()
    return {
        "run_id": run_id,
        "reason": reason,
        "exists": exists,
        "file_sha256": _file_hashes(directory) if exists else {},
        "valid": exists,
        "validation_errors": [] if exists else ["excluded run directory is missing"],
    }


def validate_gate_a0(
    run_root: Path,
    evidence_spec: Mapping[str, Any],
    *,
    evidence_spec_sha256: str | None = None,
) -> dict[str, Any]:
    """Validate selected cells and report whether all eight declared GPUs pass."""

    if not run_root.is_dir():
        raise FileNotFoundError(f"run root is not a directory: {run_root}")
    spec = json.loads(canonical_json(evidence_spec))
    if not isinstance(spec, dict):
        raise TypeError("evidence_spec must be an object")
    # Reuse the public parser's normalization rules without requiring a temp file.
    if spec.get("schema_version") != EVIDENCE_SPEC_SCHEMA_VERSION:
        raise ValueError("invalid evidence spec schema")

    source_revision = _string(spec.get("source_revision"), field="source_revision")
    seed = _integer(spec.get("seed"), field="seed")
    declared = spec.get("declared_gpus")
    selected = spec.get("selected_gpu_indices")
    trials = spec.get("required_trials")
    excluded_values = spec.get("excluded_runs")
    sealed_ids = spec.get("sealed_rejection_run_ids")
    if not all(
        isinstance(value, list)
        for value in (declared, selected, trials, excluded_values, sealed_ids)
    ):
        raise ValueError("evidence spec arrays are malformed")

    gpu_by_index = {
        int(item["physical_gpu"]): str(item["gpu_uuid"])
        for item in declared
        if isinstance(item, Mapping)
    }
    selected_indices = [int(value) for value in selected]
    required_trials = [int(value) for value in trials]
    excluded_ids = {
        str(item["run_id"])
        for item in excluded_values
        if isinstance(item, Mapping)
    }

    candidates: dict[tuple[int, int], list[Path]] = {
        (gpu, trial): []
        for gpu in gpu_by_index
        for trial in required_trials
    }
    discovered = 0
    for run_directory in sorted(
        (
            path
            for path in run_root.iterdir()
            if path.is_dir() and not path.name.startswith(".")
        ),
        key=lambda path: path.name,
    ):
        discovered += 1
        if run_directory.name in excluded_ids:
            continue
        try:
            manifest_value = _json_object(run_directory / "manifest.json")
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        cell = _candidate_cell(
            manifest_value,
            source_revision=source_revision,
            seed=seed,
            declared_indices=set(gpu_by_index),
            required_trials=set(required_trials),
        )
        if cell is not None:
            candidates[cell].append(run_directory)

    cell_reports: list[dict[str, Any]] = []
    cell_status: dict[tuple[int, int], str] = {}
    for gpu in sorted(gpu_by_index):
        for trial in required_trials:
            paths = candidates[(gpu, trial)]
            if not paths:
                cell_status[(gpu, trial)] = "missing"
                cell_reports.append(
                    {
                        "physical_gpu": gpu,
                        "gpu_uuid": gpu_by_index[gpu],
                        "trial": trial,
                        "status": "missing",
                        "run_ids": [],
                        "runs": [],
                    }
                )
                continue
            run_reports = [
                _validate_baseline_run(
                    path,
                    expected_source_revision=source_revision,
                    expected_seed=seed,
                    expected_gpu=gpu,
                    expected_trial=trial,
                    expected_uuid=gpu_by_index[gpu],
                )
                for path in paths
            ]
            status = (
                "duplicate"
                if len(paths) != 1
                else ("valid" if run_reports[0]["valid"] else "invalid")
            )
            cell_status[(gpu, trial)] = status
            cell_reports.append(
                {
                    "physical_gpu": gpu,
                    "gpu_uuid": gpu_by_index[gpu],
                    "trial": trial,
                    "status": status,
                    "run_ids": sorted(path.name for path in paths),
                    "runs": sorted(run_reports, key=lambda item: item["run_id"]),
                }
            )

    excluded_reports = [
        _excluded_record(
            run_root,
            run_id=str(item["run_id"]),
            reason=str(item["reason"]),
        )
        for item in excluded_values
        if isinstance(item, Mapping)
    ]
    excluded_reports.sort(key=lambda item: item["run_id"])
    rejection_reports = [
        _validate_sealed_rejection(
            run_root / str(run_id),
            expected_source_revision=source_revision,
        )
        if (run_root / str(run_id)).is_dir()
        else {
            "run_id": str(run_id),
            "file_sha256": {},
            "valid": False,
            "validation_errors": ["sealed rejection run directory is missing"],
        }
        for run_id in sealed_ids
    ]
    rejection_reports.sort(key=lambda item: item["run_id"])

    selected_cells = [
        (gpu, trial) for gpu in selected_indices for trial in required_trials
    ]
    selected_cell_reports = [
        report
        for report in cell_reports
        if int(report["physical_gpu"]) in set(selected_indices)
    ]
    selected_valid_runs = [
        report["runs"][0]
        for report in selected_cell_reports
        if report["status"] == "valid"
    ]
    selected_binary_hashes = sorted(
        {
            str(run["binary_sha256"])
            for run in selected_valid_runs
            if run.get("binary_sha256")
        }
    )
    selected_build_hashes = sorted(
        {
            str(run["build_sha256"])
            for run in selected_valid_runs
            if run.get("build_sha256")
        }
    )
    selected_consistent = (
        len(selected_binary_hashes) == 1 and len(selected_build_hashes) == 1
    )
    exclusions_valid = all(item["valid"] for item in excluded_reports)
    rejections_valid = all(item["valid"] for item in rejection_reports)
    selected_accepted = (
        all(cell_status[cell] == "valid" for cell in selected_cells)
        and selected_consistent
        and exclusions_valid
        and rejections_valid
    )

    missing_declared_gpus: list[dict[str, Any]] = []
    complete_declared_gpus: list[int] = []
    for gpu in sorted(gpu_by_index):
        missing_trials = [
            trial
            for trial in required_trials
            if cell_status[(gpu, trial)] == "missing"
        ]
        invalid_trials = [
            trial
            for trial in required_trials
            if cell_status[(gpu, trial)] in {"invalid", "duplicate"}
        ]
        if missing_trials or invalid_trials:
            missing_declared_gpus.append(
                {
                    "physical_gpu": gpu,
                    "gpu_uuid": gpu_by_index[gpu],
                    "missing_trials": missing_trials,
                    "invalid_trials": invalid_trials,
                }
            )
        else:
            complete_declared_gpus.append(gpu)

    all_valid_runs = [
        report["runs"][0]
        for report in cell_reports
        if report["status"] == "valid"
    ]
    all_binary_hashes = sorted(
        {
            str(run["binary_sha256"])
            for run in all_valid_runs
            if run.get("binary_sha256")
        }
    )
    all_build_hashes = sorted(
        {
            str(run["build_sha256"])
            for run in all_valid_runs
            if run.get("build_sha256")
        }
    )
    full_consistent = len(all_binary_hashes) == 1 and len(all_build_hashes) == 1
    gate_complete = (
        len(gpu_by_index) == REQUIRED_GATE_A0_GPU_COUNT
        and not missing_declared_gpus
        and full_consistent
        and exclusions_valid
        and rejections_valid
    )

    relevant_run_records: dict[str, dict[str, Any]] = {}
    for cell in cell_reports:
        for run in cell["runs"]:
            relevant_run_records[run["run_id"]] = {
                "run_id": run["run_id"],
                "file_sha256": run["file_sha256"],
            }
    for item in (*excluded_reports, *rejection_reports):
        relevant_run_records[item["run_id"]] = {
            "run_id": item["run_id"],
            "file_sha256": item["file_sha256"],
        }
    spec_digest = evidence_spec_sha256 or _sha256_bytes(
        canonical_json(spec).encode("utf-8")
    )
    aggregate_input = {
        "evidence_spec_sha256": spec_digest,
        "runs": [
            relevant_run_records[key] for key in sorted(relevant_run_records)
        ],
    }

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "evidence_spec": {
            "schema_version": EVIDENCE_SPEC_SCHEMA_VERSION,
            "evidence_id": spec.get("evidence_id"),
            "sha256": spec_digest,
            "source_revision": source_revision,
            "seed": seed,
        },
        "scan": {
            "run_root": str(run_root.resolve()),
            "discovered_run_directories": discovered,
        },
        "selected_subset": {
            "gpu_indices": selected_indices,
            "required_trials": required_trials,
            "expected_cell_count": len(selected_cells),
            "valid_cell_count": sum(
                cell_status[cell] == "valid" for cell in selected_cells
            ),
            "binary_sha256_values": selected_binary_hashes,
            "build_sha256_values": selected_build_hashes,
            "binary_and_build_consistent": selected_consistent,
            "accepted": selected_accepted,
        },
        "gate_a0": {
            "required_gpu_count": REQUIRED_GATE_A0_GPU_COUNT,
            "declared_gpu_count": len(gpu_by_index),
            "complete_declared_gpus": complete_declared_gpus,
            "missing_declared_gpus": missing_declared_gpus,
            "binary_sha256_values": all_binary_hashes,
            "build_sha256_values": all_build_hashes,
            "binary_and_build_consistent": full_consistent,
            "complete": gate_complete,
        },
        "cells": cell_reports,
        "excluded_runs": {
            "valid": exclusions_valid,
            "runs": excluded_reports,
        },
        "sealed_rejections": {
            "valid": rejections_valid,
            "runs": rejection_reports,
        },
        "aggregate_inputs": aggregate_input,
        "aggregate_input_sha256": _sha256_bytes(
            canonical_json(aggregate_input).encode("utf-8")
        ),
    }


def aggregate_from_spec(run_root: Path, evidence_spec_path: Path) -> dict[str, Any]:
    spec = load_evidence_spec(evidence_spec_path)
    spec_sha = _sha256_bytes(canonical_json(spec).encode("utf-8"))
    return validate_gate_a0(
        run_root.resolve(),
        spec,
        evidence_spec_sha256=spec_sha,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-spec", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, default=Path("experiments/runs"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--require-full",
        action="store_true",
        help="also require the complete declared 8-GPU Gate-A0 matrix",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = aggregate_from_spec(
        args.run_root.resolve(),
        args.evidence_spec.resolve(),
    )
    write_json_atomic(args.output.resolve(), report)
    accepted = bool(report["selected_subset"]["accepted"])
    if args.require_full:
        accepted = accepted and bool(report["gate_a0"]["complete"])
    return 0 if accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
