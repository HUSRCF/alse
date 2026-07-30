"""Build and run the native Gate-A SM-ID probe with complete provenance.

Masked probes deliberately fail closed on every CUDA driver API version absent
from the pinned libsmctrl source.  An experimental run must opt in explicitly;
stream masking additionally requires an explicit ``MASK_OFF``.
"""

from __future__ import annotations

import argparse
import ctypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
from typing import Any, Mapping, Sequence

from .asle_runner import query_gpu
from .environment import capture_environment
from .provenance import (
    EventRecord,
    RunManifest,
    append_jsonl_atomic,
    canonical_json,
    write_json_atomic,
    write_text_atomic,
)


DEFAULT_BINARY = Path("build/smctrl_probe/smid_probe")
DEFAULT_BUILD_SOURCE = Path("native/smctrl_probe")
DEFAULT_RUN_ROOT = Path("experiments/runs")
DEFAULT_LIBSMCTRL_ROOT = Path("vendor/libsmctrl")
DEFAULT_SOURCE_METADATA = Path("vendor/LIBSMCTRL_SOURCE.json")
DEFAULT_GATE_MANIFEST = Path("experiments/manifests/gate_a_4090.json")

NATIVE_SCHEMA_VERSION = "burstserve.smid-probe-native/v1"
CELL_SCHEMA_VERSION = "burstserve.smid-probe-cell/v1"
OUTCOME_SCHEMA_VERSION = "burstserve.smid-probe-outcome/v1"
GATE_MANIFEST_SCHEMA_VERSION = "burstserve.gate-a-manifest/v1"
RUNNER_VERSION = "burstserve.smctrl-runner/v1"
MASKED_HEALTH_MONITOR_IMPLEMENTED = False

PROBE_MODES = ("baseline", "global", "next", "stream")
MASKED_MODES = frozenset({"global", "next", "stream"})
BASELINE_MIN_SM_COVERAGE = 0.75
PINNED_VALIDATED_DRIVER_VERSIONS = frozenset(
    {
        6050,
        7000,
        7050,
        8000,
        9000,
        9010,
        9020,
        10000,
        10010,
        10020,
        11000,
        11010,
        11020,
        11030,
        11040,
        11050,
        11060,
        11070,
        11080,
        12000,
        12010,
        12020,
        12030,
        12040,
        12050,
        12060,
        12070,
        12080,
    }
)


class NativeOutputError(ValueError):
    """Raised when the native probe does not honor its stdout contract."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _git_output(worktree: Path, arguments: Sequence[str]) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(worktree), *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _git_revision(worktree: Path) -> str:
    head = _git_output(worktree, ["rev-parse", "HEAD"]) or "unavailable"
    dirty = _git_output(worktree, ["diff", "--binary", "HEAD"])
    staged = _git_output(worktree, ["diff", "--binary", "--cached", "HEAD"])
    status = _git_output(
        worktree,
        ["status", "--porcelain", "--untracked-files=all"],
    )
    untracked = _git_output(
        worktree,
        ["ls-files", "--others", "--exclude-standard", "-z"],
    )
    untracked_fingerprints: list[str] = []
    for relative in (untracked or "").split("\0"):
        if not relative:
            continue
        path = worktree / relative
        try:
            if path.is_symlink():
                digest = hashlib.sha256(
                    os.readlink(path).encode("utf-8")
                ).hexdigest()
                kind = "symlink"
            elif path.is_file():
                digest_object = hashlib.sha256()
                with path.open("rb") as source:
                    for chunk in iter(
                        lambda: source.read(1024 * 1024),
                        b"",
                    ):
                        digest_object.update(chunk)
                digest = digest_object.hexdigest()
                kind = "file"
            else:
                digest = "unreadable"
                kind = "other"
        except OSError as exc:
            digest = f"error-{type(exc).__name__}"
            kind = "error"
        untracked_fingerprints.append(f"{kind} {relative} {digest}")
    payload = "\n".join(
        item
        for item in (
            dirty,
            staged,
            status,
            "\n".join(untracked_fingerprints),
        )
        if item
    )
    if not payload:
        return head
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"{head}+dirty-{digest}"


def source_revision(repo_root: Path, libsmctrl_root: Path) -> str:
    """Return an identity containing both BurstServe and libsmctrl revisions."""

    return (
        f"burstserve-{_git_revision(repo_root)};"
        f"libsmctrl-{_git_revision(libsmctrl_root)}"
    )


def query_cuda_driver_version() -> int:
    """Read ``cuDriverGetVersion`` without creating a CUDA context."""

    try:
        cuda = ctypes.CDLL("libcuda.so.1")
    except OSError as exc:
        raise RuntimeError(f"cannot load libcuda.so.1: {exc}") from exc
    version = ctypes.c_int()
    get_version = cuda.cuDriverGetVersion
    get_version.argtypes = [ctypes.POINTER(ctypes.c_int)]
    get_version.restype = ctypes.c_int
    status = int(get_version(ctypes.byref(version)))
    if status != 0:
        raise RuntimeError(f"cuDriverGetVersion failed with CUDA status {status}")
    if version.value <= 0:
        raise RuntimeError(f"cuDriverGetVersion returned {version.value}")
    return int(version.value)


def query_compute_processes(gpu_uuid: str) -> list[dict[str, Any]]:
    """Return compute processes attached to one GPU UUID."""

    result = subprocess.run(
        [
            "nvidia-smi",
            "--query-compute-apps=gpu_uuid,pid,used_gpu_memory,process_name",
            "--format=csv,noheader,nounits",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"nvidia-smi compute-process query failed: {result.stderr.strip()}"
        )
    processes: list[dict[str, Any]] = []
    for row in result.stdout.splitlines():
        if not row.strip():
            continue
        fields = [field.strip() for field in row.split(",", maxsplit=3)]
        if len(fields) != 4:
            raise RuntimeError(f"unexpected compute-process row: {row!r}")
        if fields[0] != gpu_uuid:
            continue
        processes.append(
            {
                "gpu_uuid": fields[0],
                "pid": int(fields[1]),
                "used_gpu_memory_mib": int(fields[2]),
                "process_name": fields[3],
            }
        )
    return processes


def query_mps_processes() -> list[dict[str, Any]]:
    """Return host NVIDIA MPS control/server processes.

    Removing ``CUDA_MPS_*`` variables is insufficient because an MPS daemon
    may use its default pipe directory.  Gate A therefore records and rejects
    any live daemon instead of assuming that environment isolation disables
    MPS.
    """

    result = subprocess.run(
        ["ps", "-eo", "pid=,comm=,args="],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"host process query failed: {result.stderr.strip()}")

    names = frozenset(
        {
            "nvidia-cuda-mps-control",
            "nvidia-cuda-mps-server",
        }
    )
    processes: list[dict[str, Any]] = []
    for row in result.stdout.splitlines():
        fields = row.strip().split(maxsplit=2)
        if len(fields) < 2:
            continue
        pid_text, command = fields[:2]
        arguments = fields[2] if len(fields) == 3 else command
        executable = Path(arguments.split(maxsplit=1)[0]).name
        if (
            command not in names
            and executable not in names
            and not command.startswith("nvidia-cuda-mps")
        ):
            continue
        try:
            pid = int(pid_text)
        except ValueError as exc:
            raise RuntimeError(f"unexpected host process row: {row!r}") from exc
        processes.append(
            {
                "pid": pid,
                "command": command,
                "arguments": arguments,
            }
        )
    return processes


def load_gate_manifest_record(
    path: Path,
    *,
    repo_root: Path,
) -> dict[str, Any]:
    """Load a Gate-A manifest and bind its canonical content to the run ID."""

    resolved = path.resolve()
    manifest_root = (repo_root / "experiments" / "manifests").resolve()
    try:
        relative_to_manifest_root = resolved.relative_to(manifest_root)
        relative_to_repo = resolved.relative_to(repo_root.resolve())
    except ValueError as exc:
        raise RuntimeError(
            f"Gate-A manifest must be inside {manifest_root}: {resolved}"
        ) from exc
    if not relative_to_manifest_root.parts:
        raise RuntimeError("Gate-A manifest path must name a file")

    status = _git_output(
        repo_root,
        ["status", "--porcelain", "--", str(relative_to_repo)],
    )
    if status:
        raise RuntimeError(
            f"Gate-A manifest must be clean in the current commit: {status}"
        )
    git_blob = _git_output(
        repo_root,
        ["rev-parse", f"HEAD:{relative_to_repo.as_posix()}"],
    )
    if not git_blob:
        raise RuntimeError(
            "Gate-A manifest must be tracked by the current commit: "
            f"{relative_to_repo}"
        )

    try:
        content = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid Gate-A manifest {path}: {exc}") from exc
    if not isinstance(content, dict):
        raise RuntimeError(f"Gate-A manifest {path} must contain a JSON object")
    if content.get("schema_version") != GATE_MANIFEST_SCHEMA_VERSION:
        raise RuntimeError(
            f"unsupported Gate-A manifest schema: "
            f"{content.get('schema_version')!r}"
        )
    for section in ("hardware", "source", "safety", "baseline"):
        if not isinstance(content.get(section), dict):
            raise RuntimeError(f"Gate-A manifest is missing object {section!r}")
    canonical = canonical_json(content)
    try:
        recorded_path = str(resolved.relative_to(repo_root.resolve()))
    except ValueError:  # pragma: no cover - constrained above.
        recorded_path = str(resolved)
    return {
        "path": recorded_path,
        "git_blob": git_blob,
        "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "content": content,
    }


def validate_gate_manifest_record(
    value: Any,
    *,
    repo_root: Path,
) -> Mapping[str, Any]:
    """Validate an embedded Gate-A manifest record and return its content."""

    if not isinstance(value, Mapping):
        raise RuntimeError("run config is missing its Gate-A manifest record")
    content = value.get("content")
    digest = value.get("sha256")
    path_value = value.get("path")
    git_blob = value.get("git_blob")
    if (
        not isinstance(content, Mapping)
        or not isinstance(digest, str)
        or not isinstance(path_value, str)
        or not isinstance(git_blob, str)
    ):
        raise RuntimeError("Gate-A manifest record is malformed")
    if content.get("schema_version") != GATE_MANIFEST_SCHEMA_VERSION:
        raise RuntimeError(
            f"unsupported Gate-A manifest schema: "
            f"{content.get('schema_version')!r}"
        )
    observed = hashlib.sha256(
        canonical_json(content).encode("utf-8")
    ).hexdigest()
    if observed != digest:
        raise RuntimeError("Gate-A manifest content does not match its SHA256")

    relative = Path(path_value)
    if relative.is_absolute():
        raise RuntimeError("Gate-A manifest record path must be repository-relative")
    resolved = (repo_root / relative).resolve()
    allowed_root = (repo_root / "experiments" / "manifests").resolve()
    try:
        resolved.relative_to(allowed_root)
    except ValueError as exc:
        raise RuntimeError(
            "Gate-A manifest record points outside experiments/manifests"
        ) from exc
    current_record = load_gate_manifest_record(resolved, repo_root=repo_root)
    if current_record["git_blob"] != git_blob:
        raise RuntimeError("Gate-A manifest Git blob does not match current HEAD")
    if current_record["sha256"] != digest:
        raise RuntimeError("Gate-A manifest file differs from embedded content")
    return content


def evaluate_gate_manifest_policy(
    manifest: Mapping[str, Any],
    *,
    mode: str,
    physical_gpu: int,
    gpu: Mapping[str, Any],
    driver_version: int,
    experimental_mask_off: int | None,
    timeout_s: float,
    maximum_used_mib: int,
    iterations: int,
    trial: int,
) -> tuple[dict[str, bool], bool]:
    """Enforce the versioned Gate-A safety/promotion manifest."""

    hardware = manifest.get("hardware")
    safety = manifest.get("safety")
    baseline = manifest.get("baseline")
    if (
        not isinstance(hardware, Mapping)
        or not isinstance(safety, Mapping)
        or not isinstance(baseline, Mapping)
    ):
        raise RuntimeError(
            "Gate-A manifest hardware/safety/baseline sections are invalid"
        )

    masked = mode in MASKED_MODES
    approved_modes = safety.get("approved_mask_modes", [])
    reserved_uuids = safety.get("reserved_gpu_uuids", [])
    stream_candidates = safety.get("stream_mask_off_candidates", [])
    checks = {
        "physical_gpu_is_declared": physical_gpu
        in hardware.get("physical_gpu_indices", []),
        "gpu_name_matches_manifest": gpu.get("name") == hardware.get("gpu_name"),
        "driver_api_matches_manifest": (
            driver_version == hardware.get("driver_api_version")
        ),
        "timeout_matches_manifest": timeout_s == safety.get("timeout_s"),
        "memory_limit_matches_manifest": (
            maximum_used_mib
            == safety.get("maximum_preexisting_gpu_memory_mib")
        ),
        "baseline_iterations_match_manifest": (
            mode != "baseline" or iterations == baseline.get("iterations")
        ),
        "baseline_trial_is_registered": (
            mode != "baseline"
            or (
                isinstance(baseline.get("trials_per_gpu"), int)
                and 0 <= trial < int(baseline["trials_per_gpu"])
            )
        ),
        "masked_experiment_promoted": (
            not masked or safety.get("experimental_mask_enabled") is True
        ),
        "masked_mode_approved": not masked or mode in approved_modes,
        "masked_gpu_is_reserved": (
            not masked or gpu.get("uuid") in reserved_uuids
        ),
        "masked_gpu_has_reservation_evidence": (
            not masked
            or (
                isinstance(
                    safety.get("exclusive_reservation_evidence"),
                    str,
                )
                and bool(safety.get("exclusive_reservation_evidence"))
            )
        ),
        "masked_xid_monitoring_is_available": (
            not masked or safety.get("xid_monitoring_available") is True
        ),
        "masked_xid_monitoring_method_is_recorded": (
            not masked
            or (
                isinstance(safety.get("xid_monitoring_method"), str)
                and bool(safety.get("xid_monitoring_method"))
            )
        ),
        "runner_masked_health_monitor_is_implemented": (
            not masked or MASKED_HEALTH_MONITOR_IMPLEMENTED
        ),
        "stream_offset_search_promoted": (
            mode != "stream"
            or safety.get("stream_offset_search_enabled") is True
        ),
        "stream_prerequisites_accepted": (
            mode != "stream"
            or safety.get("global_next_matrix_accepted") is True
        ),
        "stream_offset_is_declared": (
            mode != "stream" or experimental_mask_off in stream_candidates
        ),
        "stream_offset_is_8byte_aligned": (
            mode != "stream"
            or (
                experimental_mask_off is not None
                and experimental_mask_off % 8 == 0
            )
        ),
    }
    return checks, all(checks.values())


def latest_pinned_driver_version(
    repo_root: Path,
    metadata_path: Path = DEFAULT_SOURCE_METADATA,
) -> int:
    """Return the newest driver API case declared by pinned source metadata."""

    path = metadata_path if metadata_path.is_absolute() else repo_root / metadata_path
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
        value = metadata["compatibility"]["latest_x86_64_stream_case"]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("latest_x86_64_stream_case must be positive integer")
        return value
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid libsmctrl source metadata {path}: {exc}") from exc


def evaluate_driver_policy(
    *,
    mode: str,
    driver_version: int,
    latest_pinned_version: int,
    experimental_allow_unsupported_driver: bool,
    experimental_mask_off: int | None,
) -> tuple[dict[str, bool], bool]:
    """Evaluate the fail-closed policy before launching a masked probe."""

    if mode not in PROBE_MODES:
        raise ValueError(f"unsupported probe mode: {mode}")
    masked = mode in MASKED_MODES
    driver_is_pinned = (
        driver_version in PINNED_VALIDATED_DRIVER_VERSIONS
        and driver_version <= latest_pinned_version
        and (
            mode != "stream"
            or driver_version >= 9000
        )
    )
    unsupported = masked and not driver_is_pinned
    offset_is_explicit = experimental_mask_off is not None
    checks = {
        "driver_is_pinned_or_explicitly_allowed": (
            not unsupported or experimental_allow_unsupported_driver
        ),
        "stream_unknown_driver_has_explicit_mask_off": (
            not (unsupported and mode == "stream") or offset_is_explicit
        ),
        "mask_off_only_used_for_stream": (
            not offset_is_explicit or mode == "stream"
        ),
        "mask_off_requires_experimental_allow": (
            not offset_is_explicit or experimental_allow_unsupported_driver
        ),
    }
    return checks, all(checks.values())


def build_child_environment(
    *,
    selected_gpu_uuid: str,
    experimental_mask_off: int | None,
) -> dict[str, str]:
    """Construct the isolated native child environment."""

    environment = dict(os.environ)
    for name in tuple(environment):
        if name.startswith("CUDA_MPS_"):
            environment.pop(name)
    environment["CUDA_VISIBLE_DEVICES"] = selected_gpu_uuid
    # NVIDIA documents an empty/nonexistent pipe directory as the explicit
    # way to bypass MPS. Merely deleting the variable would select the default
    # /tmp/nvidia-mps daemon.
    environment["CUDA_MPS_PIPE_DIRECTORY"] = ""
    if experimental_mask_off is None:
        environment.pop("MASK_OFF", None)
    else:
        environment["MASK_OFF"] = str(experimental_mask_off)
    return environment


def capture_probe_environment(
    *,
    repo_root: Path,
    selected_gpu_uuid: str,
) -> dict[str, Any]:
    """Capture provenance while explicitly bypassing MPS on the target GPU."""

    sentinel = object()
    overrides = {
        "CUDA_VISIBLE_DEVICES": selected_gpu_uuid,
        "CUDA_MPS_PIPE_DIRECTORY": "",
    }
    previous: dict[str, object | str] = {
        name: os.environ.get(name, sentinel) for name in overrides
    }
    try:
        os.environ.update(overrides)
        return capture_environment(repo_root=repo_root, model_root=None)
    finally:
        for name, value in previous.items():
            if value is sentinel:
                os.environ.pop(name, None)
            else:
                os.environ[name] = str(value)


def build_probe_command(
    *,
    binary: Path,
    mode: str,
    enabled_tpc: int,
    iterations: int,
    experimental_allow_unsupported_driver: bool,
) -> list[str]:
    """Construct the native probe command without shell interpolation."""

    if mode not in PROBE_MODES:
        raise ValueError(f"unsupported probe mode: {mode}")
    if isinstance(enabled_tpc, bool) or enabled_tpc < 0:
        raise ValueError("enabled_tpc must be a non-negative bit index")
    if isinstance(iterations, bool) or iterations <= 0:
        raise ValueError("iterations must be positive")
    command = [str(binary), "--mode", mode]
    if mode in MASKED_MODES:
        command.extend(["--enabled-tpc", str(enabled_tpc)])
    command.extend(["--iterations", str(iterations)])
    if experimental_allow_unsupported_driver:
        command.append("--allow-unsupported-driver")
    return command


def parse_native_output(stdout: str) -> dict[str, Any]:
    """Parse the required single non-empty JSON stdout line."""

    lines = [line for line in stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise NativeOutputError(
            f"expected exactly one non-empty stdout line, got {len(lines)}"
        )
    try:
        value = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise NativeOutputError(f"invalid native JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise NativeOutputError("native JSON must be an object")
    schema = value.get("schema_version", value.get("schema"))
    if schema != NATIVE_SCHEMA_VERSION:
        raise NativeOutputError(
            f"unsupported native schema: {schema!r}"
        )
    return value


def _integer(value: Any, *, field: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise NativeOutputError(
            f"{field} must be an integer greater than or equal to {minimum}"
        )
    return value


def normalize_histogram(value: Any) -> dict[int, int]:
    """Normalize native object, dense-array, or record-array histograms."""

    result: dict[int, int] = {}

    def add(sm_id_value: Any, count_value: Any) -> None:
        if isinstance(sm_id_value, str):
            try:
                sm_id_value = int(sm_id_value, 10)
            except ValueError as exc:
                raise NativeOutputError(
                    f"histogram SM key is not an integer: {sm_id_value!r}"
                ) from exc
        sm_id = _integer(sm_id_value, field="histogram sm_id")
        count = _integer(count_value, field=f"histogram[{sm_id}]")
        if sm_id in result:
            raise NativeOutputError(f"duplicate histogram SM id {sm_id}")
        result[sm_id] = count

    if isinstance(value, Mapping):
        for sm_id, count in value.items():
            add(sm_id, count)
    elif isinstance(value, list):
        if all(
            isinstance(item, int) and not isinstance(item, bool) for item in value
        ):
            for sm_id, count in enumerate(value):
                add(sm_id, count)
        else:
            for item in value:
                if isinstance(item, Mapping):
                    sm_id = item.get("sm_id", item.get("sm", item.get("id")))
                    count = item.get("count", item.get("hits"))
                    add(sm_id, count)
                elif isinstance(item, list) and len(item) == 2:
                    add(item[0], item[1])
                else:
                    raise NativeOutputError(
                        "histogram array must be dense counts, records, or pairs"
                    )
    else:
        raise NativeOutputError("observed_histogram must be an object or array")
    if not result:
        raise NativeOutputError("observed_histogram must not be empty")
    return result


def evaluate_probe(
    native: Mapping[str, Any],
    *,
    expected_mode: str,
    expected_enabled_tpc: int,
    expected_driver_version: int,
    expected_iterations: int,
    process_exit_code: int,
    expected_device_uuid: str | None = None,
    expected_device_name: str | None = None,
    expected_sm_count: int | None = None,
    expected_compute_capability: Sequence[int] | None = None,
    expected_blocks: int | None = None,
    minimum_sm_coverage: float = BASELINE_MIN_SM_COVERAGE,
) -> tuple[dict[str, bool], dict[str, Any], bool]:
    """Evaluate Gate-A baseline or single-TPC semantics."""

    device = native.get("device")
    device_ok = isinstance(device, Mapping)
    try:
        sm_count = _integer(
            device.get("sm_count") if device_ok else None,
            field="device.sm_count",
            minimum=1,
        )
    except NativeOutputError:
        sm_count = 0
        device_ok = False
    device_uuid = device.get("uuid") if device_ok else None
    device_name = device.get("name") if device_ok else None
    cc_major = device.get("cc_major") if device_ok else None
    cc_minor = device.get("cc_minor") if device_ok else None

    histogram_error: str | None = None
    try:
        histogram = normalize_histogram(native.get("observed_histogram"))
    except NativeOutputError as exc:
        histogram = {}
        histogram_error = str(exc)
    positive_ids = sorted(sm_id for sm_id, count in histogram.items() if count > 0)
    ids_in_range = bool(sm_count) and all(sm_id < sm_count for sm_id in histogram)
    coverage = len(positive_ids) / sm_count if sm_count else 0.0

    mode_matches = native.get("mode") == expected_mode
    status_ok = native.get("status") == "ok"
    try:
        native_driver = _integer(
            native.get("driver_version"),
            field="driver_version",
            minimum=1,
        )
    except NativeOutputError:
        native_driver = -1

    try:
        native_iterations = _integer(
            native.get("iterations"),
            field="iterations",
            minimum=1,
        )
    except NativeOutputError:
        native_iterations = -1
    try:
        native_blocks = _integer(
            native.get("blocks"),
            field="blocks",
            minimum=1,
        )
    except NativeOutputError:
        native_blocks = -1
    observed_blocks = sum(histogram.values())

    baseline_broad = (
        expected_mode != "baseline"
        or (
            len(positive_ids) >= 2
            and coverage >= minimum_sm_coverage
        )
    )
    requested_matches = True
    single_tpc_scope = True
    if expected_mode in MASKED_MODES:
        try:
            requested = _integer(
                native.get("requested_enabled_tpc"),
                field="requested_enabled_tpc",
            )
        except NativeOutputError:
            requested = -1
        requested_matches = requested == expected_enabled_tpc
        single_tpc_scope = 1 <= len(positive_ids) <= 2

    acceptance = {
        "process_exit_zero": process_exit_code == 0,
        "status_ok": status_ok,
        "mode_matches": mode_matches,
        "driver_version_matches_preflight": native_driver
        == expected_driver_version,
        "iterations_match_request": native_iterations == expected_iterations,
        "device_valid": device_ok,
        "device_uuid_matches_preflight": (
            expected_device_uuid is None or device_uuid == expected_device_uuid
        ),
        "device_name_matches_manifest": (
            expected_device_name is None or device_name == expected_device_name
        ),
        "sm_count_matches_manifest": (
            expected_sm_count is None or sm_count == expected_sm_count
        ),
        "compute_capability_matches_manifest": (
            expected_compute_capability is None
            or [cc_major, cc_minor] == list(expected_compute_capability)
        ),
        "histogram_valid": histogram_error is None,
        "observed_sm_ids_in_range": ids_in_range,
        "samples_observed": bool(positive_ids),
        "histogram_count_matches_reported_blocks": (
            native_blocks > 0 and observed_blocks == native_blocks
        ),
        "reported_blocks_match_manifest": (
            expected_blocks is None or native_blocks == expected_blocks
        ),
        "baseline_broad_sm_coverage": baseline_broad,
        "requested_tpc_matches": requested_matches,
        "single_tpc_observed_one_or_two_sms": single_tpc_scope,
    }
    metrics = {
        "device_sm_count": sm_count,
        "device_uuid": device_uuid,
        "device_name": device_name,
        "compute_capability": [cc_major, cc_minor],
        "observed_sm_count": len(positive_ids),
        "observed_sm_ids": positive_ids,
        "sm_coverage_ratio": coverage,
        "reported_blocks": native_blocks,
        "observed_blocks": observed_blocks,
        "reported_iterations": native_iterations,
        "histogram_error": histogram_error,
    }
    return acceptance, metrics, all(acceptance.values())


def _record_event(
    path: Path,
    *,
    run_id: str,
    sequence: int,
    event_type: str,
    payload: Mapping[str, Any] | None = None,
) -> None:
    append_jsonl_atomic(
        path,
        EventRecord.create(
            run_id=run_id,
            sequence=sequence,
            event_type=event_type,
            payload=payload,
        ),
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def native_build_record(binary: Path) -> dict[str, Any]:
    """Return the generated native build configuration beside *binary*."""

    stamp = binary.parent / "build-config.stamp"
    if not stamp.is_file():
        return {
            "found": False,
            "path": str(stamp),
        }
    content = stamp.read_text(encoding="utf-8")
    return {
        "found": True,
        "path": str(stamp),
        "sha256": _sha256_file(stamp),
        "content": content,
    }


def execute(
    *,
    repo_root: Path,
    binary: Path,
    libsmctrl_root: Path,
    run_root: Path,
    config: Mapping[str, Any],
    timeout_s: float,
    maximum_used_mib: int,
    allow_busy_gpu: bool,
) -> tuple[int, Path]:
    """Run one probe and return ``(runner_exit_code, run_directory)``."""

    if timeout_s <= 0:
        raise ValueError("timeout_s must be positive")
    if not binary.is_file():
        raise FileNotFoundError(f"native SM-ID probe not found: {binary}")

    physical_gpu = int(config["physical_gpu"])
    mode = str(config["mode"])
    enabled_tpc = int(config["enabled_tpc"])
    iterations = int(config["iterations"])
    experimental_allow = bool(config["experimental_allow_unsupported_driver"])
    mask_off_raw = config.get("experimental_mask_off")
    experimental_mask_off = (
        int(mask_off_raw) if mask_off_raw is not None else None
    )
    gate_manifest = validate_gate_manifest_record(
        config.get("gate_manifest"),
        repo_root=repo_root,
    )
    hardware = gate_manifest.get("hardware")
    if not isinstance(hardware, Mapping):
        raise RuntimeError("Gate-A manifest hardware section is invalid")

    gpu_preflight = query_gpu(physical_gpu)
    effective_allow_busy = allow_busy_gpu and mode == "baseline"
    compute_processes_preflight = query_compute_processes(
        str(gpu_preflight["uuid"])
    )
    mps_processes_preflight = query_mps_processes()
    driver_version = query_cuda_driver_version()
    latest_supported = latest_pinned_driver_version(repo_root)
    driver_policy_checks, driver_policy_permitted = evaluate_driver_policy(
        mode=mode,
        driver_version=driver_version,
        latest_pinned_version=latest_supported,
        experimental_allow_unsupported_driver=experimental_allow,
        experimental_mask_off=experimental_mask_off,
    )
    revision = source_revision(repo_root, libsmctrl_root)
    build_record = native_build_record(binary)
    environment = capture_probe_environment(
        repo_root=repo_root,
        selected_gpu_uuid=str(gpu_preflight["uuid"]),
    )

    # The environment snapshot is intentionally comprehensive and can take
    # seconds. Re-run the physical-device checks immediately before launch.
    gpu_launch = query_gpu(physical_gpu)
    compute_processes_launch = query_compute_processes(str(gpu_launch["uuid"]))
    mps_processes_launch = query_mps_processes()
    manifest_policy_checks, manifest_policy_permitted = (
        evaluate_gate_manifest_policy(
            gate_manifest,
            mode=mode,
            physical_gpu=physical_gpu,
            gpu=gpu_launch,
            driver_version=driver_version,
            experimental_mask_off=experimental_mask_off,
            timeout_s=timeout_s,
            maximum_used_mib=maximum_used_mib,
            iterations=iterations,
            trial=int(config["trial"]),
        )
    )
    safety_policy_checks = {
        "gpu_uuid_stable_during_preflight": (
            gpu_preflight.get("uuid") == gpu_launch.get("uuid")
        ),
        "mps_explicitly_bypassed": True,
        "busy_override_only_for_baseline": (
            not allow_busy_gpu or mode == "baseline"
        ),
        "memory_safe_at_initial_preflight": (
            int(gpu_preflight["memory_used_mib"]) <= maximum_used_mib
            or effective_allow_busy
        ),
        "memory_safe_at_launch_preflight": (
            int(gpu_launch["memory_used_mib"]) <= maximum_used_mib
            or effective_allow_busy
        ),
        "compute_processes_absent_at_initial_preflight_or_baseline_override": (
            not compute_processes_preflight or effective_allow_busy
        ),
        "compute_processes_absent_at_launch_preflight_or_baseline_override": (
            not compute_processes_launch or effective_allow_busy
        ),
        "timeout_recorded_in_config": config.get("timeout_s") == timeout_s,
        "memory_limit_recorded_in_config": (
            config.get("maximum_used_mib") == maximum_used_mib
        ),
        "busy_override_recorded_in_config": (
            config.get("allow_busy_gpu") is allow_busy_gpu
        ),
        "source_tree_is_clean_commit": (
            "+dirty-" not in revision and "unavailable" not in revision
        ),
        "native_build_stamp_is_present": build_record.get("found") is True,
    }
    preflight_permitted = (
        driver_policy_permitted
        and manifest_policy_permitted
        and all(safety_policy_checks.values())
    )

    environment.update(
        {
            "selected_gpu_initial_preflight": gpu_preflight,
            "selected_gpu_launch_preflight": gpu_launch,
            "selected_gpu_compute_processes_initial": (
                compute_processes_preflight
            ),
            "selected_gpu_compute_processes_launch": compute_processes_launch,
            "host_mps_processes_initial": mps_processes_preflight,
            "host_mps_processes_launch": mps_processes_launch,
            "mps_bypass": {
                "CUDA_MPS_PIPE_DIRECTORY": "",
                "basis": "NVIDIA-documented empty-pipe-directory bypass",
            },
            "cuda_driver_api_version_preflight": driver_version,
            "libsmctrl_latest_pinned_driver_api_version": latest_supported,
            "native_binary": {
                "path": str(binary),
                "sha256": _sha256_file(binary),
            },
            "native_build": build_record,
        }
    )
    manifest = RunManifest.create(
        config=config,
        seed=int(config.get("seed", 0)),
        source_revision=revision,
        environment=environment,
        metadata={
            "purpose": "phase1-libsmctrl-gate-a",
            "runner": RUNNER_VERSION,
        },
    )

    run_directory = run_root / manifest.run_id
    run_directory.mkdir(parents=True, exist_ok=False)
    events_path = run_directory / "events.jsonl"
    write_json_atomic(run_directory / "manifest.json", manifest.to_dict())
    _record_event(
        events_path,
        run_id=manifest.run_id,
        sequence=0,
        event_type="run.preflight",
        payload={
            "gpu_initial": gpu_preflight,
            "gpu_launch": gpu_launch,
            "compute_processes_initial": compute_processes_preflight,
            "compute_processes_launch": compute_processes_launch,
            "mps_processes_initial": mps_processes_preflight,
            "mps_processes_launch": mps_processes_launch,
            "driver_version": driver_version,
            "latest_pinned_driver_version": latest_supported,
            "driver_policy": driver_policy_checks,
            "driver_policy_permitted": driver_policy_permitted,
            "manifest_policy": manifest_policy_checks,
            "manifest_policy_permitted": manifest_policy_permitted,
            "safety_policy": safety_policy_checks,
            "preflight_permitted": preflight_permitted,
        },
    )

    command = build_probe_command(
        binary=binary,
        mode=mode,
        enabled_tpc=enabled_tpc,
        iterations=iterations,
        experimental_allow_unsupported_driver=experimental_allow,
    )
    command_record = {
        "argv": command,
        "cwd": str(repo_root),
        "started_at_utc": _utc_now(),
        "environment_overrides": {
            "CUDA_VISIBLE_DEVICES": str(gpu_launch["uuid"]),
            "CUDA_MPS_PIPE_DIRECTORY": "",
            "MASK_OFF": (
                str(experimental_mask_off)
                if experimental_mask_off is not None
                else None
            ),
            "removed_mps_variables_except_empty_pipe_bypass": True,
        },
    }
    write_json_atomic(run_directory / "command.json", command_record)

    if not preflight_permitted:
        write_text_atomic(run_directory / "stdout.log", "")
        write_text_atomic(
            run_directory / "stderr.log",
            "probe rejected by fail-closed preflight policy\n",
        )
        outcome = {
            "schema_version": OUTCOME_SCHEMA_VERSION,
            "completed_at_utc": _utc_now(),
            "exit_code": 4,
            "process_exit_code": None,
            "timed_out": False,
            "native_output_found": False,
            "native_status": None,
            "driver_policy": driver_policy_checks,
            "driver_policy_permitted": driver_policy_permitted,
            "manifest_policy": manifest_policy_checks,
            "manifest_policy_permitted": manifest_policy_permitted,
            "safety_policy": safety_policy_checks,
            "preflight_permitted": False,
            "semantic_acceptance": {},
            "semantic_metrics": {},
            "local_probe_passed": False,
            "requires_matrix_validation": mode in MASKED_MODES,
            "accepted": False,
        }
        write_json_atomic(run_directory / "outcome.json", outcome)
        _record_event(
            events_path,
            run_id=manifest.run_id,
            sequence=1,
            event_type="run.rejected",
            payload=outcome,
        )
        return 4, run_directory

    child_environment = build_child_environment(
        selected_gpu_uuid=str(gpu_launch["uuid"]),
        experimental_mask_off=experimental_mask_off,
    )
    _record_event(
        events_path,
        run_id=manifest.run_id,
        sequence=1,
        event_type="run.started",
        payload={"argv": command},
    )
    process = subprocess.Popen(
        command,
        cwd=repo_root,
        env=child_environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()

    stdout = stdout or ""
    stderr = stderr or ""
    write_text_atomic(run_directory / "stdout.log", stdout)
    write_text_atomic(run_directory / "stderr.log", stderr)
    process_exit_code = 124 if timed_out else int(process.returncode or 0)

    post_health_error: str | None = None
    post_gpu: dict[str, Any] | None = None
    post_compute_processes: list[dict[str, Any]] = []
    post_mps_processes: list[dict[str, Any]] = []
    try:
        post_gpu = query_gpu(physical_gpu)
        post_compute_processes = query_compute_processes(str(post_gpu["uuid"]))
        post_mps_processes = query_mps_processes()
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        post_health_error = f"{type(exc).__name__}: {exc}"
    post_health_checks = {
        "health_queries_completed": post_health_error is None,
        "gpu_accessible_after_probe": post_gpu is not None,
        "gpu_uuid_stable_after_probe": (
            post_gpu is not None
            and post_gpu.get("uuid") == gpu_launch.get("uuid")
        ),
        "memory_safe_after_probe": (
            post_gpu is not None
            and (
                int(post_gpu["memory_used_mib"]) <= maximum_used_mib
                or effective_allow_busy
            )
        ),
        "compute_processes_absent_after_probe_or_baseline_override": (
            not post_compute_processes or effective_allow_busy
        ),
    }

    native: dict[str, Any] | None = None
    native_error: str | None = None
    acceptance: dict[str, bool] = {}
    metrics: dict[str, Any] = {}
    semantic_accepted = False
    try:
        native = parse_native_output(stdout)
        write_json_atomic(run_directory / "native.json", native)
        acceptance, metrics, semantic_accepted = evaluate_probe(
            native,
            expected_mode=mode,
            expected_enabled_tpc=enabled_tpc,
            expected_driver_version=driver_version,
            expected_iterations=iterations,
            process_exit_code=process_exit_code,
            expected_device_uuid=str(gpu_launch["uuid"]),
            expected_device_name=str(hardware["gpu_name"]),
            expected_sm_count=int(hardware["sm_count"]),
            expected_compute_capability=hardware["compute_capability"],
            expected_blocks=(
                int(hardware["sm_count"])
                * int(gate_manifest["baseline"]["blocks_per_sm"])
            ),
            minimum_sm_coverage=float(
                gate_manifest["baseline"]["minimum_sm_coverage_fraction"]
            ),
        )
    except NativeOutputError as exc:
        native_error = str(exc)

    local_probe_passed = (
        preflight_permitted
        and not timed_out
        and native is not None
        and semantic_accepted
        and all(post_health_checks.values())
    )
    # A single masked process can establish only local semantics. Cross-trial
    # stability, disjoint TPC maps, a follow-up baseline, and next-mask
    # non-leakage require a matrix validator before any masked cell is accepted.
    accepted = (
        local_probe_passed
        and mode == "baseline"
        and not allow_busy_gpu
    )
    exit_code = (
        process_exit_code
        if process_exit_code != 0
        else (0 if local_probe_passed else 3)
    )
    outcome = {
        "schema_version": OUTCOME_SCHEMA_VERSION,
        "completed_at_utc": _utc_now(),
        "exit_code": exit_code,
        "process_exit_code": process_exit_code,
        "timed_out": timed_out,
        "native_output_found": native is not None,
        "native_output_error": native_error,
        "native_status": native.get("status") if native else None,
        "driver_policy": driver_policy_checks,
        "driver_policy_permitted": driver_policy_permitted,
        "manifest_policy": manifest_policy_checks,
        "manifest_policy_permitted": manifest_policy_permitted,
        "safety_policy": safety_policy_checks,
        "preflight_permitted": preflight_permitted,
        "post_health": {
            "gpu": post_gpu,
            "compute_processes": post_compute_processes,
            "mps_processes": post_mps_processes,
            "error": post_health_error,
            "checks": post_health_checks,
        },
        "semantic_acceptance": acceptance,
        "semantic_metrics": metrics,
        "local_probe_passed": local_probe_passed,
        "requires_matrix_validation": mode in MASKED_MODES,
        "accepted": accepted,
    }
    write_json_atomic(run_directory / "outcome.json", outcome)
    _record_event(
        events_path,
        run_id=manifest.run_id,
        sequence=2,
        event_type="run.completed" if local_probe_passed else "run.failed",
        payload=outcome,
    )
    return exit_code, run_directory


def build_native(
    *,
    repo_root: Path,
    source_directory: Path,
    jobs: int | None,
) -> int:
    """Build the pinned native probe through its Makefile."""

    command = ["make", "-C", str(source_directory)]
    if jobs is not None:
        if jobs <= 0:
            raise ValueError("jobs must be positive")
        command.extend(["-j", str(jobs)])
    return subprocess.run(command, cwd=repo_root, check=False).returncode


def _resolve(repo_root: Path, path: Path) -> Path:
    return path.resolve() if path.is_absolute() else (repo_root / path).resolve()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="build the native SM-ID probe")
    build.add_argument("--repo-root", type=Path, default=Path.cwd())
    build.add_argument("--source-dir", type=Path, default=DEFAULT_BUILD_SOURCE)
    build.add_argument("--jobs", type=int)

    run = subparsers.add_parser("run", help="run one provenance-complete probe")
    run.add_argument("--repo-root", type=Path, default=Path.cwd())
    run.add_argument("--binary", type=Path, default=DEFAULT_BINARY)
    run.add_argument("--libsmctrl-root", type=Path, default=DEFAULT_LIBSMCTRL_ROOT)
    run.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    run.add_argument(
        "--gate-manifest",
        type=Path,
        default=DEFAULT_GATE_MANIFEST,
        help="versioned safety/promotion manifest embedded into the run ID",
    )
    run.add_argument("--physical-gpu", type=int, required=True)
    run.add_argument("--mode", choices=PROBE_MODES, required=True)
    run.add_argument("--enabled-tpc", type=int, default=0)
    run.add_argument("--iterations", type=int, default=4096)
    run.add_argument("--trial", type=int, default=0)
    run.add_argument("--seed", type=int, default=0)
    run.add_argument(
        "--timeout-s",
        type=float,
        help="must match the Gate-A manifest (default: manifest value)",
    )
    run.add_argument(
        "--maximum-used-mib",
        type=int,
        help="must match the Gate-A manifest (default: manifest value)",
    )
    run.add_argument(
        "--allow-busy-gpu",
        action="store_true",
        help="baseline-only exploratory override; forbidden for masked probes",
    )
    run.add_argument(
        "--experimental-allow-unsupported-driver",
        action="store_true",
        help="explicitly permit a masked probe on a newer unpinned driver",
    )
    run.add_argument(
        "--experimental-mask-off",
        type=int,
        help="stream-mask offset passed as MASK_OFF; requires experimental allow",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    if args.command == "build":
        return build_native(
            repo_root=repo_root,
            source_directory=_resolve(repo_root, args.source_dir),
            jobs=args.jobs,
        )

    binary = _resolve(repo_root, args.binary)
    libsmctrl_root = _resolve(repo_root, args.libsmctrl_root)
    run_root = _resolve(repo_root, args.run_root)
    gate_manifest_record = load_gate_manifest_record(
        _resolve(repo_root, args.gate_manifest),
        repo_root=repo_root,
    )
    gate_safety = gate_manifest_record["content"]["safety"]
    timeout_s = (
        float(args.timeout_s)
        if args.timeout_s is not None
        else float(gate_safety["timeout_s"])
    )
    maximum_used_mib = (
        int(args.maximum_used_mib)
        if args.maximum_used_mib is not None
        else int(gate_safety["maximum_preexisting_gpu_memory_mib"])
    )
    config = {
        "schema_version": CELL_SCHEMA_VERSION,
        "physical_gpu": args.physical_gpu,
        "mode": args.mode,
        "enabled_tpc": args.enabled_tpc,
        "iterations": args.iterations,
        "trial": args.trial,
        "seed": args.seed,
        "timeout_s": timeout_s,
        "maximum_used_mib": maximum_used_mib,
        "allow_busy_gpu": args.allow_busy_gpu,
        "experimental_allow_unsupported_driver": (
            args.experimental_allow_unsupported_driver
        ),
        "experimental_mask_off": args.experimental_mask_off,
        "gate_manifest": gate_manifest_record,
    }
    code, run_directory = execute(
        repo_root=repo_root,
        binary=binary,
        libsmctrl_root=libsmctrl_root,
        run_root=run_root,
        config=config,
        timeout_s=timeout_s,
        maximum_used_mib=maximum_used_mib,
        allow_busy_gpu=args.allow_busy_gpu,
    )
    print(run_directory)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
