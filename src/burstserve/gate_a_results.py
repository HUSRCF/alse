"""Deterministically validate a declared Gate-A0 baseline evidence matrix.

The validator is intentionally driven by a versioned evidence specification.
It never guesses which runs are formal evidence and never embeds run IDs.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
from typing import Any, Mapping, Sequence

from .provenance import EventRecord, RunManifest, canonical_json, write_json_atomic
from .smctrl_runner import (
    MASKED_MODES,
    CELL_SCHEMA_VERSION as CELL_SCHEMA_VERSION_V2,
    GATE_MANIFEST_SCHEMA_VERSION as GATE_MANIFEST_SCHEMA_VERSION_V2,
    NATIVE_SCHEMA_VERSION as NATIVE_SCHEMA_VERSION_V2,
    OUTCOME_SCHEMA_VERSION as OUTCOME_SCHEMA_VERSION_V2,
    evaluate_probe,
)


EVIDENCE_SPEC_SCHEMA_VERSION = "burstserve.gate-a0-evidence-spec/v1"
EVIDENCE_SPEC_SCHEMA_VERSION_V2 = "burstserve.gate-a0-evidence-spec/v2"
REPORT_SCHEMA_VERSION = "burstserve.gate-a0-evidence-report/v1"
REPORT_SCHEMA_VERSION_V2 = "burstserve.gate-a0-evidence-report/v2"
CELL_SCHEMA_VERSION = "burstserve.smid-probe-cell/v1"
OUTCOME_SCHEMA_VERSION = "burstserve.smid-probe-outcome/v1"
NATIVE_SCHEMA_VERSION = "burstserve.smid-probe-native/v1"
EXPECTED_BLOCKS = 4096
# The v1 evidence programme was scoped to the box's full eight 4090s. That
# number is part of the already-published v1 report and is therefore frozen.
REQUIRED_GATE_A0_GPU_COUNT = 8
# 2026-07-31 scope change (see plan.md decision log): the paper's main matrix
# is re-scoped to a four-card fleet plus one spare/clean-control card, so the
# v2 programme requires those five 4090s rather than all eight. Declaring more
# than the minimum is allowed and still has to be complete.
REQUIRED_GATE_A0_GPU_COUNT_V2 = 5

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

_EVIDENCE_FILE_SIZE_LIMITS = {
    "manifest.json": 16 * 1024 * 1024,
    "outcome.json": 16 * 1024 * 1024,
    "native.json": 4 * 1024 * 1024,
    "events.jsonl": 32 * 1024 * 1024,
    "command.json": 4 * 1024 * 1024,
    "stdout.log": 4 * 1024 * 1024,
    "stderr.log": 4 * 1024 * 1024,
}
_MAX_JSON_DEPTH = 128
_MAX_JSON_INTEGER_DIGITS = 128
_MAX_JSON_FLOAT_CHARACTERS = 256
_MAX_EVENT_RECORDS = 128
_MAX_EVENT_LINE_BYTES = 8 * 1024 * 1024
_MAX_DECLARED_GPUS = 64
_MAX_SELECTED_GPUS = 64
_MAX_REQUIRED_TRIALS = 1024
_MAX_MATRIX_CELLS = 4096
_MAX_EXCLUDED_RUNS = 1024
_MAX_SEALED_REJECTIONS = 1024
_MAX_CANDIDATE_SNAPSHOTS = 512
_MAX_CANDIDATE_SNAPSHOT_BYTES = 512 * 1024 * 1024
_MAX_RUN_ROOT_ENTRIES = 4096
_MAX_DISCOVERED_RUN_DIRECTORIES = 4096
_MAX_SCANNED_SNAPSHOTS = 4096
_MAX_SCANNED_SNAPSHOT_BYTES = 1024 * 1024 * 1024
_MAX_AUXILIARY_SNAPSHOT_BYTES = 512 * 1024 * 1024

_RUN_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "run_id",
        "created_at_utc",
        "seed",
        "source_revision",
        "config",
        "environment",
        "metadata",
    }
)
_EVENT_RECORD_KEYS = frozenset(
    {
        "schema_version",
        "run_id",
        "sequence",
        "timestamp_utc",
        "event_type",
        "payload",
    }
)
_V2_NATIVE_KEYS = frozenset(
    {
        "schema_version",
        "status",
        "mode",
        "driver_version",
        "runtime_version",
        "parent_guard",
        "device",
        "requested_enabled_tpc",
        "tpc_count",
        "blocks",
        "threads_per_block",
        "iterations",
        "observed_histogram",
        "error",
    }
)
_V2_NATIVE_REQUIRED_KEYS = _V2_NATIVE_KEYS - {"error"}
_V2_NATIVE_DEVICE_KEYS = frozenset(
    {"ordinal", "name", "uuid", "cc_major", "cc_minor", "sm_count"}
)
_V2_NATIVE_PARENT_GUARD_KEYS = frozenset(
    {
        "mode",
        "status",
        "expected_parent_pid",
        "observed_parent_pid",
        "inherited_pdeath_signal",
        "pdeath_signal",
    }
)
_V2_COMMAND_KEYS = frozenset(
    {
        "argv",
        "cwd",
        "prepared_at_utc",
        "environment_overrides",
        "environment_policy",
        "parent_death_protection",
        "dynamic_loader_policy",
        "signal_mask_policy",
        "launcher_fd",
        "launcher_fd_final",
        "cuda_driver_probe",
        "libcuda_final_revalidation",
        "formal_launcher_threat_boundaries",
    }
)
_V2_OUTCOME_KEYS = frozenset(
    {
        "schema_version",
        "completed_at_utc",
        "exit_code",
        "process_exit_code",
        "raw_process_return_code",
        "timed_out",
        "child_launch_error",
        "child_interruption",
        "process_group_reaped",
        "process_group_health",
        "quarantine_required",
        "quarantine_reasons",
        "gpu_lease",
        "native_output_found",
        "native_output_error",
        "native_status",
        "driver_policy",
        "driver_policy_permitted",
        "manifest_policy",
        "manifest_policy_permitted",
        "safety_policy",
        "preflight_permitted",
        "formal_source_binding",
        "formal_source_checks",
        "formal_source_required_checks",
        "formal_source_preflight_permitted",
        "source_eligible_for_local_pass",
        "source_prelaunch_revalidation",
        "source_preexec_revalidation",
        "source_postrun_revalidation",
        "final_launch_preflight",
        "launch_commit_reservation_revalidation",
        "libcuda_final_revalidation",
        "launcher_fd_final",
        "formal_launcher_threat_boundaries",
        "post_health",
        "semantic_acceptance",
        "semantic_metrics",
        "masked_health_monitor_status",
        "masked_health_monitor",
        "masked_health_monitor_checks",
        "local_probe_passed",
        "requires_matrix_validation",
        "accepted",
    }
)
_V2_CELL_CONFIG_KEYS = frozenset(
    {
        "schema_version",
        "physical_gpu",
        "mode",
        "enabled_tpc",
        "iterations",
        "blocks",
        "threads_per_block",
        "trial",
        "seed",
        "timeout_s",
        "maximum_used_mib",
        "allow_busy_gpu",
        "experimental_allow_unsupported_driver",
        "experimental_mask_off",
        "gate_manifest",
    }
)
_GATE_MANIFEST_RECORD_KEYS = frozenset(
    {"path", "git_blob", "sha256", "content"}
)
_EVIDENCE_SPEC_KEYS = frozenset(
    {
        "schema_version",
        "evidence_id",
        "source_revision",
        "seed",
        "declared_gpus",
        "selected_gpu_indices",
        "required_trials",
        "excluded_runs",
        "sealed_rejection_run_ids",
    }
)
_DRIVER_POLICY_KEYS = frozenset(
    {
        "driver_is_pinned_or_explicitly_allowed",
        "stream_unknown_driver_has_explicit_mask_off",
        "mask_off_only_used_for_stream",
        "mask_off_requires_experimental_allow",
    }
)
_GPU_RECORD_KEYS = frozenset(
    {
        "index",
        "name",
        "uuid",
        "pci_bus_id",
        "memory_total_mib",
        "memory_used_mib",
        "utilization_gpu_percent",
        "driver_version",
    }
)
_COMPUTE_PROCESS_RECORD_KEYS = frozenset(
    {"gpu_uuid", "pid", "used_gpu_memory_mib", "process_name"}
)
# Host MPS control/server daemons are recorded as provenance.  Formal
# isolation is proven by the child's explicit empty CUDA_MPS_PIPE_DIRECTORY
# bypass, not by daemon absence, so this list is validated for exact record
# shape rather than required to be empty.
_MPS_PROCESS_RECORD_KEYS = frozenset({"pid", "command", "arguments"})
_V2_FINAL_PREFLIGHT_KEYS = frozenset(
    {
        "captured_at_utc",
        "required_horizon_s",
        "required_until_utc",
        "gpu",
        "compute_processes",
        "mps_processes",
        "errors",
        "checks",
        "passed",
    }
)
_V2_BASELINE_FINAL_PREFLIGHT_CHECK_KEYS = frozenset(
    {
        "health_queries_completed",
        "gpu_accessible",
        "gpu_ordinal_exact",
        "gpu_uuid_stable",
        "gpu_uuid_matches_held_lease",
        "memory_safe_or_explicit_busy_baseline",
        "compute_processes_absent_or_explicit_busy_baseline",
        "empty_mps_pipe_bypass_exact",
        "reservation_not_required_for_baseline",
        "reservation_valid_for_complete_run_horizon",
    }
)
_V2_POST_HEALTH_KEYS = frozenset(
    {
        "gpu",
        "compute_processes",
        "mps_processes",
        "error",
        "checks",
        "reservation_revalidation",
    }
)
_V2_BASELINE_POST_HEALTH_CHECK_KEYS = frozenset(
    {
        "health_queries_completed",
        "gpu_accessible_after_probe",
        "gpu_ordinal_exact_after_probe",
        "gpu_uuid_stable_after_probe",
        "memory_safe_after_probe",
        "compute_processes_absent_after_probe_or_baseline_override",
        "host_mps_state_recorded_after_probe",
        "process_group_reaped",
        "reservation_valid_at_gpu_safety_end",
    }
)
_V2_RESERVATION_REVALIDATION_KEYS = frozenset(
    {"captured_at_utc", "required_for_mode", "checks", "passed"}
)
# A sealed rejection is evidence that the promotion lock fired.  A closed
# manifest fails every masked/experimental authorization prerequisite at once,
# and those prerequisites do not all share one name prefix: the CUDA-stream
# offset gates and the runner's masked health-monitor requirement authorize
# masked execution just as much as the `masked_*` family does.  A false check
# outside this family means the run was stopped by something other than
# authorization, which disqualifies it as lock evidence.  The family is
# explicit rather than heuristic; if the producer adds an authorization check
# outside it, the rejection fails closed and names the offending check.
_SEALED_REJECTION_AUTHORIZATION_PREFIXES = ("masked_",)
_SEALED_REJECTION_AUTHORIZATION_CHECKS = frozenset(
    {
        "runner_masked_health_monitor_is_implemented",
        "stream_offset_is_8byte_aligned",
        "stream_offset_is_declared",
        "stream_offset_search_promoted",
        "stream_prerequisites_accepted",
    }
)
# Prefix membership is not sufficient on its own: this check compares the
# manifest's declared thread count against the native probe constant, so it is
# a manifest-correctness check rather than an authorization gate and its
# failure must disqualify the rejection.
_SEALED_REJECTION_NON_AUTHORIZATION_CHECKS = frozenset(
    {
        "masked_threads_are_native_canonical",
    }
)
_V2_GPU_HARDWARE_IDENTITY_KEYS = frozenset(
    {
        "vbios_version",
        "subsystem_vendor_id",
        "subsystem_device_id",
        "numa_node",
        "power_limit_w",
        "power_default_limit_w",
        "power_max_limit_w",
        "max_sm_clock_mhz",
        "max_memory_clock_mhz",
        "max_pcie_link_gen",
        "max_pcie_link_width",
    }
)
_V2_LIBCUDA_BINDING_CHECK_KEYS = frozenset(
    {
        "runtime_libcuda_build_stamp_fields_present",
        "runtime_libcuda_resolved_path_matches_build_stamp",
        "runtime_libcuda_sha256_matches_build_stamp",
        "runtime_libcuda_link_path_is_fixed",
        "runtime_libcuda_target_is_root_owned_regular",
    }
)
_COMMAND_NESTED_KEYS = {
    "environment_policy": frozenset(
        {"mode", "allowed_names", "inherited_names"}
    ),
    "parent_death_protection": frozenset(
        {
            "mechanism",
            "expected_parent_pid",
            "runner_signal_handlers",
            "residual",
        }
    ),
    "dynamic_loader_policy": frozenset(
        {
            "mode",
            "inherited_environment",
            "loader_and_cuda_tuning_variables_absent",
        }
    ),
    "signal_mask_policy": frozenset(
        {
            "runner_blocks_during_popen",
            "child_mask_reset_before_exec",
            "cleanup_policy",
            "residual",
        }
    ),
}
_EVENT_PAYLOAD_ALLOWED_KEYS = {
    "run.preflight": frozenset(
        {
            "gpu_initial",
            "gpu_launch",
            "gpu_hardware_identity",
            "compute_processes_initial",
            "compute_processes_launch",
            "mps_processes_initial",
            "mps_processes_launch",
            "driver_version",
            "cuda_driver_probe",
            "runtime_libcuda_build_binding_checks",
            "latest_pinned_driver_version",
            "driver_policy",
            "driver_policy_permitted",
            "manifest_policy",
            "manifest_policy_permitted",
            "safety_policy",
            "preflight_permitted",
            "formal_source_binding",
            "formal_source_checks",
            "formal_source_required_checks",
            "formal_source_preflight_permitted",
            "source_eligible_for_local_pass",
            "source_prelaunch_revalidation",
            "source_preexec_revalidation",
            "source_postrun_revalidation",
            "libcuda_final_revalidation",
            "launcher_fd_final",
            "formal_launcher_threat_boundaries",
        }
    ),
    "run.source_revalidated": frozenset(
        {
            "phase",
            "completed",
            "error",
            "build_record",
            "binding",
            "checks",
            "required_checks",
            "canonical_paths_selected",
            "source_eligible_for_local_pass",
            "formal_source_launch_permitted",
            "expected_snapshot_sha256",
            "observed_snapshot_sha256",
            "snapshot_matches_initial",
            "passed_for_launch",
            "passed_for_local_acceptance",
        }
    ),
    "run.source_preexec_revalidated": frozenset(
        {
            "phase",
            "completed",
            "error",
            "build_record",
            "binding",
            "checks",
            "required_checks",
            "canonical_paths_selected",
            "source_eligible_for_local_pass",
            "formal_source_launch_permitted",
            "expected_snapshot_sha256",
            "observed_snapshot_sha256",
            "snapshot_matches_initial",
            "passed_for_launch",
            "passed_for_local_acceptance",
        }
    ),
    "run.source_postvalidated": frozenset(
        {
            "phase",
            "completed",
            "error",
            "build_record",
            "binding",
            "checks",
            "required_checks",
            "canonical_paths_selected",
            "source_eligible_for_local_pass",
            "formal_source_launch_permitted",
            "expected_snapshot_sha256",
            "observed_snapshot_sha256",
            "snapshot_matches_initial",
            "passed_for_launch",
            "passed_for_local_acceptance",
        }
    ),
    "run.final_launch_preflight": frozenset(
        {
            "captured_at_utc",
            "required_horizon_s",
            "required_until_utc",
            "gpu",
            "compute_processes",
            "mps_processes",
            "errors",
            "checks",
            "passed",
        }
    ),
    "run.started": frozenset(
        {
            "argv",
            "executed_argv0",
            "pid",
            "started_at_utc",
            "launcher_fd_identity",
            "launcher_fd_final",
            "libcuda_final_revalidation",
            "final_launch_preflight",
            "launch_commit_reservation_revalidation",
        }
    ),
}


@dataclass(frozen=True, slots=True)
class _EvidenceFileSnapshot:
    name: str
    content: bytes
    sha256: str
    identity: dict[str, int | str]


@dataclass(frozen=True, slots=True)
class _EvidenceDirectorySnapshot:
    path: Path
    directory_identity: dict[str, int]
    files: dict[str, _EvidenceFileSnapshot | None]
    errors: tuple[str, ...]

    @property
    def hashes(self) -> dict[str, str | None]:
        return {
            name: (
                snapshot.sha256
                if isinstance(snapshot, _EvidenceFileSnapshot)
                else None
            )
            for name, snapshot in self.files.items()
        }

    @property
    def identities(self) -> dict[str, dict[str, int | str] | None]:
        return {
            name: (
                dict(snapshot.identity)
                if isinstance(snapshot, _EvidenceFileSnapshot)
                else None
            )
            for name, snapshot in self.files.items()
        }


def _stat_identity(status: os.stat_result) -> dict[str, int]:
    return {
        "device": int(status.st_dev),
        "inode": int(status.st_ino),
        "mode": int(status.st_mode),
        "uid": int(status.st_uid),
        "gid": int(status.st_gid),
        "nlink": int(status.st_nlink),
        "size": int(status.st_size),
        "mtime_ns": int(status.st_mtime_ns),
        "ctime_ns": int(status.st_ctime_ns),
    }


def _read_evidence_fd_content(
    descriptor: int,
    *,
    name: str,
    expected_status: os.stat_result,
) -> bytes:
    remaining = int(expected_status.st_size)
    offset = 0
    chunks: list[bytes] = []
    while remaining:
        chunk = os.pread(descriptor, min(1024 * 1024, remaining), offset)
        if not chunk:
            raise ValueError(f"{name} changed while its snapshot was read")
        chunks.append(chunk)
        offset += len(chunk)
        remaining -= len(chunk)
    if os.pread(descriptor, 1, offset):
        raise ValueError(f"{name} grew while its snapshot was read")
    content = b"".join(chunks)
    if len(content) != expected_status.st_size:
        raise ValueError(f"{name} snapshot length differs from its identity")
    return content


def _snapshot_evidence_directory(path: Path) -> _EvidenceDirectorySnapshot:
    """Freeze one globally consistent, name-bound evidence FD set."""

    directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    directory_flags |= getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    parent_descriptor = os.open(path.parent, directory_flags)
    directory_descriptor = -1
    opened: dict[str, int] = {}
    initial_statuses: dict[str, os.stat_result] = {}
    initially_missing: set[str] = set()
    files: dict[str, _EvidenceFileSnapshot | None] = {}
    errors: list[str] = []
    try:
        parent_before = os.fstat(parent_descriptor)
        directory_descriptor = os.open(
            path.name,
            directory_flags,
            dir_fd=parent_descriptor,
        )
        directory_before = os.fstat(directory_descriptor)
        if not stat.S_ISDIR(directory_before.st_mode):
            raise ValueError(f"evidence path is not a directory: {path}")
        initial_name_status = os.stat(
            path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if _stat_identity(initial_name_status) != _stat_identity(
            directory_before
        ):
            raise ValueError(
                "run-root directory name is not bound to its opened FD"
            )

        # Acquire every expected file before consuming any bytes.
        for name in _EVIDENCE_FILES:
            file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            file_flags |= getattr(os, "O_NOFOLLOW", 0)
            file_flags |= getattr(os, "O_NONBLOCK", 0)
            try:
                descriptor = os.open(
                    name,
                    file_flags,
                    dir_fd=directory_descriptor,
                )
            except OSError as error:
                files[name] = None
                if error.errno == errno.ENOENT:
                    initially_missing.add(name)
                else:
                    errors.append(
                        f"could not snapshot {name}: "
                        f"{type(error).__name__}: {error}"
                    )
                continue
            opened[name] = descriptor
            try:
                status = os.fstat(descriptor)
                if not stat.S_ISREG(status.st_mode):
                    raise ValueError(f"{name} is not a regular file")
                if status.st_nlink != 1:
                    raise ValueError(
                        f"{name} must have exactly one hard link"
                    )
                maximum = _EVIDENCE_FILE_SIZE_LIMITS[name]
                if status.st_size < 0 or status.st_size > maximum:
                    raise ValueError(
                        f"{name} size {status.st_size} exceeds "
                        f"{maximum} bytes"
                    )
                initial_statuses[name] = status
            except (OSError, ValueError) as error:
                files[name] = None
                errors.append(
                    f"could not snapshot {name}: "
                    f"{type(error).__name__}: {error}"
                )

        contents: dict[str, bytes] = {}
        for name, descriptor in opened.items():
            status = initial_statuses.get(name)
            if status is None:
                continue
            try:
                contents[name] = _read_evidence_fd_content(
                    descriptor,
                    name=name,
                    expected_status=status,
                )
            except (OSError, ValueError) as error:
                errors.append(
                    f"could not snapshot {name}: "
                    f"{type(error).__name__}: {error}"
                )

        second_contents: dict[str, bytes] = {}
        for name, descriptor in opened.items():
            status = initial_statuses.get(name)
            content = contents.get(name)
            if status is None or content is None:
                continue
            try:
                second_contents[name] = _read_evidence_fd_content(
                    descriptor,
                    name=name,
                    expected_status=status,
                )
            except (OSError, ValueError) as error:
                errors.append(
                    f"could not rehash {name}: "
                    f"{type(error).__name__}: {error}"
                )

        # Commit only after all second reads, when every held FD retains its
        # initial identity and bytes and each name still resolves to that FD.
        for name, descriptor in opened.items():
            status = initial_statuses.get(name)
            content = contents.get(name)
            second_content = second_contents.get(name)
            if status is None or content is None or second_content is None:
                files[name] = None
                continue
            try:
                final_status = os.fstat(descriptor)
                if _stat_identity(status) != _stat_identity(final_status):
                    raise ValueError(
                        f"{name} identity changed across the global snapshot"
                    )
                if second_content != content:
                    raise ValueError(
                        f"{name} bytes changed across the global snapshot"
                    )
                name_status = os.stat(
                    name,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
                if _stat_identity(name_status) != _stat_identity(final_status):
                    raise ValueError(
                        f"{name} directory entry no longer names its held FD"
                    )
                sha256 = hashlib.sha256(content).hexdigest()
                files[name] = _EvidenceFileSnapshot(
                    name=name,
                    content=content,
                    sha256=sha256,
                    identity={
                        **_stat_identity(final_status),
                        "sha256": sha256,
                    },
                )
            except (OSError, ValueError) as error:
                files[name] = None
                errors.append(
                    f"could not commit {name} snapshot: "
                    f"{type(error).__name__}: {error}"
                )

        for name in initially_missing:
            try:
                os.stat(
                    name,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            except OSError as error:
                errors.append(
                    f"could not revalidate missing {name}: "
                    f"{type(error).__name__}: {error}"
                )
            else:
                errors.append(
                    f"{name} appeared during the global evidence snapshot"
                )

        directory_after = os.fstat(directory_descriptor)
        if _stat_identity(directory_before) != _stat_identity(directory_after):
            errors.append(
                "evidence directory identity changed while files were "
                "snapshotted"
            )
        final_name_status = os.stat(
            path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if _stat_identity(final_name_status) != _stat_identity(
            directory_after
        ):
            errors.append(
                "run-root directory name changed during evidence snapshot"
            )
        parent_after = os.fstat(parent_descriptor)
        if _stat_identity(parent_before) != _stat_identity(parent_after):
            errors.append(
                "run-root directory identity changed during evidence snapshot"
            )
        directory_identity = _stat_identity(directory_before)
    finally:
        for descriptor in opened.values():
            os.close(descriptor)
        if directory_descriptor >= 0:
            os.close(directory_descriptor)
        os.close(parent_descriptor)
    return _EvidenceDirectorySnapshot(
        path=path,
        directory_identity=directory_identity,
        files=files,
        errors=tuple(errors),
    )


def _snapshot_regular_path(
    path: Path,
    *,
    maximum_bytes: int,
) -> _EvidenceFileSnapshot:
    directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    directory_flags |= getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    parent_descriptor = os.open(path.parent, directory_flags)
    descriptor = -1
    try:
        parent_before = os.fstat(parent_descriptor)
        file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        file_flags |= getattr(os, "O_NOFOLLOW", 0)
        file_flags |= getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(
            path.name,
            file_flags,
            dir_fd=parent_descriptor,
        )
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError(f"{path} must be a single-link regular file")
        if before.st_size < 0 or before.st_size > maximum_bytes:
            raise ValueError(
                f"{path} size {before.st_size} exceeds {maximum_bytes}"
            )
        first = _read_evidence_fd_content(
            descriptor,
            name=path.name,
            expected_status=before,
        )
        second = _read_evidence_fd_content(
            descriptor,
            name=path.name,
            expected_status=before,
        )
        after = os.fstat(descriptor)
        name_status = os.stat(
            path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        parent_after = os.fstat(parent_descriptor)
        if (
            first != second
            or _stat_identity(before) != _stat_identity(after)
            or _stat_identity(name_status) != _stat_identity(after)
            or _stat_identity(parent_before) != _stat_identity(parent_after)
        ):
            raise ValueError(f"{path} changed while it was snapshotted")
        digest = hashlib.sha256(first).hexdigest()
        return _EvidenceFileSnapshot(
            name=path.name,
            content=first,
            sha256=digest,
            identity={
                **_stat_identity(after),
                "sha256": digest,
            },
        )
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_descriptor)


def _snapshot_file(
    evidence: _EvidenceDirectorySnapshot,
    name: str,
) -> _EvidenceFileSnapshot:
    value = evidence.files.get(name)
    if not isinstance(value, _EvidenceFileSnapshot):
        raise ValueError(f"{name} has no valid frozen snapshot")
    return value


def _json_object_pairs(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _bounded_json_integer(token: str) -> int:
    digits = token[1:] if token.startswith("-") else token
    if len(digits) > _MAX_JSON_INTEGER_DIGITS:
        raise ValueError(
            "JSON integer exceeds "
            f"{_MAX_JSON_INTEGER_DIGITS} decimal digits"
        )
    return int(token)


def _bounded_json_float(token: str) -> float:
    if len(token) > _MAX_JSON_FLOAT_CHARACTERS:
        raise ValueError(
            "JSON float token exceeds "
            f"{_MAX_JSON_FLOAT_CHARACTERS} characters"
        )
    value = float(token)
    if not math.isfinite(value):
        raise ValueError("JSON float must be finite")
    return value


def _reject_json_constant(token: str) -> None:
    raise ValueError(f"invalid non-finite JSON constant: {token}")


def _check_json_depth(text: str, *, field: str) -> None:
    depth = 0
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > _MAX_JSON_DEPTH:
                raise ValueError(
                    f"{field} exceeds JSON depth {_MAX_JSON_DEPTH}"
                )
        elif character in "]}":
            depth -= 1
            if depth < 0:
                raise ValueError(f"{field} has unmatched JSON delimiter")
    if depth != 0 or in_string or escaped:
        raise ValueError(f"{field} has unterminated JSON structure")


def _strict_json_value(
    content: bytes,
    *,
    field: str,
    canonical_document: bool,
) -> Any:
    try:
        text = content.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ValueError(f"{field} is not strict UTF-8: {error}") from error
    _check_json_depth(text, field=field)
    try:
        value = json.loads(
            text,
            object_pairs_hook=_json_object_pairs,
            parse_int=_bounded_json_integer,
            parse_float=_bounded_json_float,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise ValueError(f"{field} is not strict JSON: {error}") from error
    if canonical_document:
        expected = canonical_json(value).encode("utf-8") + b"\n"
        if content != expected:
            raise ValueError(
                f"{field} bytes are not the canonical JSON document"
            )
    return value


def _strict_snapshot_json_object(
    evidence: _EvidenceDirectorySnapshot,
    name: str,
    *,
    allowed_keys: frozenset[str],
    required_keys: frozenset[str] | None = None,
) -> dict[str, Any]:
    snapshot = _snapshot_file(evidence, name)
    value = _strict_json_value(
        snapshot.content,
        field=name,
        canonical_document=True,
    )
    if not isinstance(value, dict):
        raise ValueError(f"{name} must contain a JSON object")
    unknown = sorted(set(value) - set(allowed_keys))
    if unknown:
        raise ValueError(f"{name} contains unknown keys: {unknown}")
    required = required_keys if required_keys is not None else allowed_keys
    missing = sorted(set(required) - set(value))
    if missing:
        raise ValueError(f"{name} is missing required keys: {missing}")
    return value


def _strict_snapshot_text(
    evidence: _EvidenceDirectorySnapshot,
    name: str,
) -> str:
    snapshot = _snapshot_file(evidence, name)
    try:
        return snapshot.content.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ValueError(f"{name} is not strict UTF-8: {error}") from error


def _classify_manifest_snapshot(
    evidence: _EvidenceDirectorySnapshot,
) -> tuple[dict[str, Any], str]:
    """Read only the explicit cell schema from bounded frozen manifest bytes."""

    value = _strict_json_value(
        _snapshot_file(evidence, "manifest.json").content,
        field="manifest.json",
        canonical_document=False,
    )
    if not isinstance(value, dict) or set(value) != set(_RUN_MANIFEST_KEYS):
        raise ValueError("manifest.json top-level keys are not exact")
    config = value.get("config")
    if not isinstance(config, Mapping):
        raise ValueError("manifest.json config must be an object")
    schema = config.get("schema_version")
    if schema not in {CELL_SCHEMA_VERSION, CELL_SCHEMA_VERSION_V2}:
        raise ValueError("manifest.json cell schema is unsupported")
    return value, str(schema)


def _strict_snapshot_events(
    evidence: _EvidenceDirectorySnapshot,
) -> tuple[list[EventRecord], list[str]]:
    records: list[EventRecord] = []
    errors: list[str] = []
    try:
        snapshot = _snapshot_file(evidence, "events.jsonl")
    except ValueError as error:
        return [], [str(error)]
    content = snapshot.content
    if content and not content.endswith(b"\n"):
        errors.append("events.jsonl is not newline terminated")
        return records, errors
    lines = content.splitlines(keepends=True)
    if len(lines) > _MAX_EVENT_RECORDS:
        errors.append(
            "events.jsonl contains "
            f"{len(lines)} records, limit is {_MAX_EVENT_RECORDS}"
        )
        return records, errors
    for line_number, line in enumerate(lines, start=1):
        if len(line) > _MAX_EVENT_LINE_BYTES:
            errors.append(
                f"events line {line_number} exceeds "
                f"{_MAX_EVENT_LINE_BYTES} bytes"
            )
            continue
        if line == b"\n":
            errors.append(f"events line {line_number} is blank")
            continue
        try:
            value = _strict_json_value(
                line,
                field=f"events line {line_number}",
                canonical_document=True,
            )
            if not isinstance(value, dict):
                raise TypeError("record must be an object")
            if set(value) != set(_EVENT_RECORD_KEYS):
                raise ValueError(
                    "event record keys are not exact: "
                    f"{sorted(value)}"
                )
            record = EventRecord.from_dict(value)
            allowed_payload = _EVENT_PAYLOAD_ALLOWED_KEYS.get(
                record.event_type
            )
            if (
                allowed_payload is not None
                and set(record.payload) - set(allowed_payload)
            ):
                raise ValueError(
                    f"{record.event_type} payload contains unknown keys: "
                    f"{sorted(set(record.payload) - set(allowed_payload))}"
                )
            records.append(record)
        except (TypeError, ValueError) as error:
            errors.append(f"invalid events line {line_number}: {error}")
    return records, errors


def _require_positive_json_integer(
    value: Any,
    *,
    field: str,
    errors: list[str],
) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        errors.append(f"{field} is not a positive integer")


def _require_optional_json_integer(
    value: Any,
    *,
    field: str,
    errors: list[str],
) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        errors.append(f"{field} is not an integer or null")


def _validate_v2_native_schema(
    value: Mapping[str, Any],
    *,
    errors: list[str],
) -> None:
    unknown = sorted(set(value) - set(_V2_NATIVE_KEYS))
    missing = sorted(set(_V2_NATIVE_REQUIRED_KEYS) - set(value))
    if unknown:
        errors.append(f"native.json contains unknown keys: {unknown}")
    if missing:
        errors.append(f"native.json is missing required keys: {missing}")
    for name in ("schema_version", "status", "mode"):
        if not isinstance(value.get(name), str) or not value.get(name):
            errors.append(f"native.json {name} is not a non-empty string")
    for name in ("blocks", "threads_per_block", "iterations"):
        _require_positive_json_integer(
            value.get(name), field=f"native.json {name}", errors=errors
        )
    for name in (
        "driver_version",
        "runtime_version",
        "requested_enabled_tpc",
        "tpc_count",
    ):
        _require_optional_json_integer(
            value.get(name), field=f"native.json {name}", errors=errors
        )
    error_value = value.get("error")
    if error_value is not None and not isinstance(error_value, str):
        errors.append("native.json error is not a string or null")
    histogram = value.get("observed_histogram")
    if not isinstance(histogram, Mapping):
        errors.append("native.json observed_histogram must be an object")
    else:
        for key, count in histogram.items():
            if not isinstance(key, str) or not re.fullmatch(
                r"0|[1-9][0-9]*", key
            ):
                errors.append(
                    "native.json observed_histogram key is not a decimal "
                    "SM id"
                )
            _require_positive_json_integer(
                count,
                field="native.json observed_histogram value",
                errors=errors,
            )
    device = value.get("device")
    if (
        not isinstance(device, Mapping)
        or set(device) != set(_V2_NATIVE_DEVICE_KEYS)
    ):
        errors.append("native.json device keys are not exact")
    else:
        for name in ("name", "uuid"):
            text = device.get(name)
            if text is not None and (
                not isinstance(text, str) or not text
            ):
                errors.append(
                    f"native.json device.{name} is not a string or null"
                )
        for name in ("ordinal", "cc_major", "cc_minor", "sm_count"):
            _require_optional_json_integer(
                device.get(name),
                field=f"native.json device.{name}",
                errors=errors,
            )
    parent_guard = value.get("parent_guard")
    if (
        not isinstance(parent_guard, Mapping)
        or set(parent_guard) != set(_V2_NATIVE_PARENT_GUARD_KEYS)
    ):
        errors.append("native.json parent_guard keys are not exact")
    else:
        for name in ("mode", "status"):
            text = parent_guard.get(name)
            if not isinstance(text, str) or not text:
                errors.append(
                    f"native.json parent_guard.{name} is not a "
                    "non-empty string"
                )
        for name in (
            "expected_parent_pid",
            "observed_parent_pid",
            "inherited_pdeath_signal",
            "pdeath_signal",
        ):
            _require_optional_json_integer(
                parent_guard.get(name),
                field=f"native.json parent_guard.{name}",
                errors=errors,
            )


def _strict_snapshot_stdout_native(
    evidence: _EvidenceDirectorySnapshot,
) -> tuple[dict[str, Any] | None, str | None]:
    try:
        snapshot = _snapshot_file(evidence, "stdout.log")
        content = snapshot.content
        if not content.endswith(b"\n"):
            return None, "stdout.log is not newline terminated"
        lines = content.splitlines(keepends=True)
        nonempty = [line for line in lines if line.strip()]
        if len(nonempty) != 1 or len(lines) != 1:
            return (
                None,
                "stdout.log must contain exactly one non-empty JSON line, "
                f"found {len(nonempty)}",
            )
        value = _strict_json_value(
            nonempty[0],
            field="stdout native JSON",
            canonical_document=False,
        )
        if not isinstance(value, dict):
            return None, "native JSON in stdout.log must be an object"
        compact = (
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=False,
            ).encode("utf-8")
            + b"\n"
        )
        if content != compact:
            return (
                None,
                "stdout native JSON is not the canonical compact encoding",
            )
        unknown = sorted(set(value) - set(_V2_NATIVE_KEYS))
        missing = sorted(set(_V2_NATIVE_REQUIRED_KEYS) - set(value))
        if unknown or missing:
            return (
                None,
                "stdout native JSON keys are not exact: "
                f"unknown={unknown}, missing={missing}",
            )
        return value, None
    except (TypeError, ValueError) as error:
        return None, f"invalid native JSON in stdout.log: {error}"


def _legacy_snapshot_json_object(
    evidence: _EvidenceDirectorySnapshot,
    name: str,
) -> dict[str, Any]:
    value = _strict_json_value(
        _snapshot_file(evidence, name).content,
        field=name,
        canonical_document=False,
    )
    if not isinstance(value, dict):
        raise ValueError(f"{name} must contain a JSON object")
    normalized = json.loads(canonical_json(value))
    if not isinstance(normalized, dict):
        raise ValueError(f"{name} must contain a JSON object")
    return normalized


def _legacy_snapshot_stdout_native(
    evidence: _EvidenceDirectorySnapshot,
) -> tuple[dict[str, Any] | None, str | None]:
    try:
        content = _snapshot_file(evidence, "stdout.log").content
        text = content.decode("utf-8", errors="strict")
        lines = [line for line in text.splitlines() if line.strip()]
        if len(lines) != 1:
            return (
                None,
                "stdout.log must contain exactly one non-empty JSON line, "
                f"found {len(lines)}",
            )
        value = _strict_json_value(
            lines[0].encode("utf-8"),
            field="stdout native JSON",
            canonical_document=False,
        )
        if not isinstance(value, dict):
            return None, "native JSON in stdout.log must be an object"
        normalized = json.loads(canonical_json(value))
        return normalized, None
    except (UnicodeError, TypeError, ValueError) as error:
        return None, f"invalid native JSON in stdout.log: {error}"


def _legacy_snapshot_events(
    evidence: _EvidenceDirectorySnapshot,
) -> tuple[list[EventRecord], list[str]]:
    records: list[EventRecord] = []
    errors: list[str] = []
    try:
        content = _snapshot_file(evidence, "events.jsonl").content
    except ValueError as error:
        return [], [str(error)]
    lines = content.splitlines()
    if len(lines) > _MAX_EVENT_RECORDS:
        return [], [
            f"events.jsonl contains {len(lines)} records, "
            f"limit is {_MAX_EVENT_RECORDS}"
        ]
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        if len(line) > _MAX_EVENT_LINE_BYTES:
            errors.append(
                f"events line {line_number} exceeds "
                f"{_MAX_EVENT_LINE_BYTES} bytes"
            )
            continue
        try:
            value = _strict_json_value(
                line,
                field=f"events line {line_number}",
                canonical_document=False,
            )
            if not isinstance(value, dict):
                raise TypeError("record must be an object")
            records.append(EventRecord.from_dict(value))
        except (TypeError, ValueError) as error:
            errors.append(f"invalid events line {line_number}: {error}")
    return records, errors


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


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


def _safe_spec_run_id(
    value: Any,
    *,
    field: str,
    schema_version: str,
) -> str:
    run_id = _string(value, field=field)
    if (
        run_id in {".", ".."}
        or "\x00" in run_id
        or "/" in run_id
        or Path(run_id).name != run_id
    ):
        raise ValueError(f"{field} must be one safe path component")
    if schema_version == EVIDENCE_SPEC_SCHEMA_VERSION_V2:
        digest = run_id.removeprefix("bs1-")
        if (
            not run_id.startswith("bs1-")
            or len(digest) != 64
            or any(
                character not in "0123456789abcdef"
                for character in digest
            )
        ):
            raise ValueError(
                f"{field} must be an exact bs1-<lowercase sha256> run ID"
            )
    return run_id


def _validate_spec_cardinality(
    *,
    declared_count: int,
    selected_count: int,
    trial_count: int,
    excluded_count: int,
    sealed_count: int,
) -> None:
    limits = (
        ("declared_gpus", declared_count, _MAX_DECLARED_GPUS),
        ("selected_gpu_indices", selected_count, _MAX_SELECTED_GPUS),
        ("required_trials", trial_count, _MAX_REQUIRED_TRIALS),
        ("excluded_runs", excluded_count, _MAX_EXCLUDED_RUNS),
        (
            "sealed_rejection_run_ids",
            sealed_count,
            _MAX_SEALED_REJECTIONS,
        ),
    )
    for field, count, maximum in limits:
        if count > maximum:
            raise ValueError(
                f"{field} contains {count} entries, limit is {maximum}"
            )
    for field, count in (
        ("declared_gpus x required_trials", declared_count * trial_count),
        (
            "selected_gpu_indices x required_trials",
            selected_count * trial_count,
        ),
    ):
        if count > _MAX_MATRIX_CELLS:
            raise ValueError(
                f"{field} contains {count} cells, limit is "
                f"{_MAX_MATRIX_CELLS}"
            )


def _normalize_evidence_spec_value(
    value: Any,
    *,
    raw_content: bytes | None = None,
) -> dict[str, Any]:
    """Strictly normalize one already duplicate-safe spec value.

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

    if not isinstance(value, dict):
        raise ValueError("evidence specification must be a JSON object")
    if set(value) != set(_EVIDENCE_SPEC_KEYS):
        raise ValueError(
            "evidence specification keys are not exact: "
            f"{sorted(value)}"
        )
    schema_version = value.get("schema_version")
    if schema_version not in {
        EVIDENCE_SPEC_SCHEMA_VERSION,
        EVIDENCE_SPEC_SCHEMA_VERSION_V2,
    }:
        raise ValueError(
            "unsupported evidence spec schema: "
            f"{schema_version!r}"
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
    selected_raw = value.get("selected_gpu_indices")
    trials_raw = value.get("required_trials")
    excluded_raw = value.get("excluded_runs")
    sealed_raw = value.get("sealed_rejection_run_ids")
    if not isinstance(selected_raw, list) or not selected_raw:
        raise ValueError("selected_gpu_indices must be a non-empty array")
    if not isinstance(trials_raw, list) or not trials_raw:
        raise ValueError("required_trials must be a non-empty array")
    if not isinstance(excluded_raw, list):
        raise ValueError("excluded_runs must be an array")
    if not isinstance(sealed_raw, list):
        raise ValueError("sealed_rejection_run_ids must be an array")
    _validate_spec_cardinality(
        declared_count=len(declared_raw),
        selected_count=len(selected_raw),
        trial_count=len(trials_raw),
        excluded_count=len(excluded_raw),
        sealed_count=len(sealed_raw),
    )
    declared_gpus: list[dict[str, Any]] = []
    for position, item in enumerate(declared_raw):
        if not isinstance(item, Mapping):
            raise ValueError(f"declared_gpus[{position}] must be an object")
        if set(item) != {"physical_gpu", "gpu_uuid"}:
            raise ValueError(
                f"declared_gpus[{position}] keys are not exact"
            )
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

    trials = sorted(_integer(item, field="required_trials[]") for item in trials_raw)
    if len(trials) != len(set(trials)):
        raise ValueError("required_trials contains duplicates")

    excluded: list[dict[str, str]] = []
    for position, item in enumerate(excluded_raw):
        if not isinstance(item, Mapping):
            raise ValueError(f"excluded_runs[{position}] must be an object")
        if set(item) != {"run_id", "reason"}:
            raise ValueError(
                f"excluded_runs[{position}] keys are not exact"
            )
        excluded.append(
            {
                "run_id": _string(
                    _safe_spec_run_id(
                        item.get("run_id"),
                        field=f"excluded_runs[{position}].run_id",
                        schema_version=str(schema_version),
                    ),
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
        _safe_spec_run_id(
            item,
            field="sealed_rejection_run_ids[]",
            schema_version=str(schema_version),
        )
        for item in sealed_raw
    )
    if len(sealed) != len(set(sealed)):
        raise ValueError("sealed_rejection_run_ids contains duplicates")
    overlap = sorted(set(excluded_ids) & set(sealed))
    if overlap:
        raise ValueError(
            f"run IDs cannot be both excluded and sealed rejections: {overlap}"
        )

    normalized = {
        "schema_version": schema_version,
        "evidence_id": evidence_id,
        "source_revision": source_revision,
        "seed": seed,
        "declared_gpus": declared_gpus,
        "selected_gpu_indices": selected,
        "required_trials": trials,
        "excluded_runs": excluded,
        "sealed_rejection_run_ids": sealed,
    }
    if (
        schema_version == EVIDENCE_SPEC_SCHEMA_VERSION_V2
        and raw_content is not None
        and raw_content
        != canonical_json(normalized).encode("utf-8") + b"\n"
    ):
        raise ValueError(
            "v2 evidence specification bytes are not canonical normalized "
            "JSON"
        )
    if (
        schema_version == EVIDENCE_SPEC_SCHEMA_VERSION_V2
        and raw_content is None
        and canonical_json(value) != canonical_json(normalized)
    ):
        raise ValueError(
            "v2 evidence specification value is not canonically ordered"
        )
    return normalized


def _load_evidence_spec_snapshot(
    path: Path,
) -> tuple[dict[str, Any], str]:
    snapshot = _snapshot_regular_path(path, maximum_bytes=4 * 1024 * 1024)
    value = _strict_json_value(
        snapshot.content,
        field="evidence specification",
        canonical_document=False,
    )
    normalized = _normalize_evidence_spec_value(
        value,
        raw_content=snapshot.content,
    )
    return normalized, snapshot.sha256


def load_evidence_spec(path: Path) -> dict[str, Any]:
    """Load and strictly normalize a Gate-A0 evidence specification."""

    value, _raw_sha256 = _load_evidence_spec_snapshot(path)
    return value


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_canonical_utc_microsecond(value: Any) -> bool:
    """Accept only the runner's exact UTC timestamp serialization."""

    if (
        not isinstance(value, str)
        or re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T"
            r"[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z",
            value,
        )
        is None
    ):
        return False
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        return False
    return parsed.strftime("%Y-%m-%dT%H:%M:%S.%fZ") == value


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


def _validate_v2_baseline_child_contract(
    command: Mapping[str, Any],
    *,
    expected_uuid: str,
    errors: list[str],
) -> None:
    """Validate the recorded child environment and launcher policies.

    The empty ``CUDA_MPS_PIPE_DIRECTORY`` bypass is the control that makes
    recorded host MPS daemons acceptable as provenance, so the v2 cell must
    prove it from the recorded child environment and not only from a
    producer-asserted boolean.
    """

    environment = command.get("environment_overrides")
    expected_environment = {
        "LANG": "C",
        "LC_ALL": "C",
        "TZ": "UTC",
        "CUDA_CACHE_DISABLE": "1",
        "CUDA_VISIBLE_DEVICES": expected_uuid,
        "CUDA_MPS_PIPE_DIRECTORY": "",
    }
    if not _exact_json_equal(environment, expected_environment):
        errors.append(
            "command.environment_overrides is not the exact baseline "
            f"child allowlist: {environment!r}"
        )

    policy = command.get("environment_policy")
    if not isinstance(policy, Mapping):
        errors.append("command.environment_policy must be an object")
    else:
        _check_exact_scalars(
            policy,
            {
                "mode": "env-i exact allowlist",
                "inherited_names": [],
            },
            field="command.environment_policy",
            errors=errors,
        )
        if not _exact_json_equal(
            policy.get("allowed_names"),
            sorted(expected_environment),
        ):
            errors.append(
                "command.environment_policy.allowed_names is not the exact "
                "baseline allowlist"
            )

    loader = command.get("dynamic_loader_policy")
    _check_exact_scalars(
        loader,
        {
            "mode": "env-i exact allowlist for every mode",
            "inherited_environment": False,
            "loader_and_cuda_tuning_variables_absent": True,
        },
        field="command.dynamic_loader_policy",
        errors=errors,
    )

    signals = command.get("signal_mask_policy")
    if not isinstance(signals, Mapping):
        errors.append("command.signal_mask_policy must be an object")
    else:
        _check_exact_scalars(
            signals,
            {
                "runner_blocks_during_popen": ["SIGINT", "SIGHUP", "SIGTERM"],
                "child_mask_reset_before_exec": True,
                "residual": None,
            },
            field="command.signal_mask_policy",
            errors=errors,
        )
        cleanup = signals.get("cleanup_policy")
        if not isinstance(cleanup, str) or not cleanup:
            errors.append(
                "command.signal_mask_policy.cleanup_policy is missing"
            )

    guard = command.get("parent_death_protection")
    _check_exact_scalars(
        guard,
        {
            "mechanism": "not_required",
            "expected_parent_pid": None,
            "runner_signal_handlers": ["SIGINT", "SIGHUP", "SIGTERM"],
            "residual": None,
        },
        field="command.parent_death_protection",
        errors=errors,
    )

    cwd = command.get("cwd")
    if not isinstance(cwd, str) or not Path(cwd).is_absolute():
        errors.append("command.cwd is not an absolute path")
    if not _is_canonical_utc_microsecond(command.get("prepared_at_utc")):
        errors.append("command.prepared_at_utc is not canonical UTC")


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


def _exact_json_equal(left: Any, right: Any, *, depth: int = 0) -> bool:
    """Compare two decoded JSON values by exact type and structure.

    Container comparison with ``==`` inherits Python's numeric equality, so a
    ``{"ok": 1}`` policy record would compare equal to ``{"ok": true}``.  Every
    node must therefore match its exact type; JSON object keys are always
    strings, which also keeps dict lookup from matching a ``1``/``True`` key.
    """

    if depth > _MAX_JSON_DEPTH:
        return False
    if isinstance(left, Mapping) or isinstance(right, Mapping):
        # Any mapping flavour is accepted on either side, but scalars stay
        # strictly typed below.
        if not (isinstance(left, Mapping) and isinstance(right, Mapping)):
            return False
        if len(left) != len(right):
            return False
        for key, value in left.items():
            if not isinstance(key, str) or key not in right:
                return False
            if not _exact_json_equal(value, right[key], depth=depth + 1):
                return False
        return all(isinstance(key, str) for key in right)
    if isinstance(left, list) or isinstance(right, list):
        if not (isinstance(left, list) and isinstance(right, list)):
            return False
        if len(left) != len(right):
            return False
        return all(
            _exact_json_equal(item, other, depth=depth + 1)
            for item, other in zip(left, right)
        )
    if type(left) is not type(right):
        return False
    return left == right


_LAUNCHER_IDENTITY_FIELDS = (
    "path",
    "device",
    "inode",
    "mode",
    "size",
    "mtime_ns",
    "sha256",
)


def _validate_launcher_identity_types(
    identity: Any,
    *,
    field: str,
    errors: list[str],
) -> None:
    """Type-check the stat identity so copies cannot substitute float for int."""

    if not isinstance(identity, Mapping):
        errors.append(f"{field} must be an object")
        return
    if set(identity) != set(_LAUNCHER_IDENTITY_FIELDS):
        errors.append(f"{field} keys are not exact")
        return
    path = identity.get("path")
    if not isinstance(path, str) or not Path(path).is_absolute():
        errors.append(f"{field}.path is not an absolute path")
    if not _is_sha256(identity.get("sha256")):
        errors.append(f"{field}.sha256 is invalid")
    for name in ("device", "inode", "mode", "size", "mtime_ns"):
        number = identity.get(name)
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or number < 0
        ):
            errors.append(f"{field}.{name} is not a non-negative integer")
    mode = identity.get("mode")
    if (
        not isinstance(mode, bool)
        and isinstance(mode, int)
        and not stat.S_ISREG(mode)
    ):
        errors.append(f"{field}.mode is not a regular file")


def _is_sealed_rejection_authorization_check(name: str) -> bool:
    if name in _SEALED_REJECTION_NON_AUTHORIZATION_CHECKS:
        return False
    return (
        name.startswith(_SEALED_REJECTION_AUTHORIZATION_PREFIXES)
        or name in _SEALED_REJECTION_AUTHORIZATION_CHECKS
    )


def _validate_libcuda_identity_types(
    identity: Any,
    *,
    field: str,
    errors: list[str],
) -> None:
    """Absolutely type-pin the libcuda stat records, not just cross-copy them."""

    if not isinstance(identity, Mapping):
        errors.append(f"{field} must be an object")
        return
    for name in ("link_identity", "target_identity"):
        record = identity.get(name)
        if not isinstance(record, Mapping):
            errors.append(f"{field}.{name} must be an object")
            continue
        for number_field in (
            "device",
            "inode",
            "mode",
            "uid",
            "gid",
            "nlink",
            "size",
            "mtime_ns",
            "ctime_ns",
        ):
            number = record.get(number_field)
            if (
                isinstance(number, bool)
                or not isinstance(number, int)
                or number < 0
            ):
                errors.append(
                    f"{field}.{name}.{number_field} is not a "
                    "non-negative integer"
                )


def _is_positive_zero_float(value: Any) -> bool:
    """Require the producer's literal ``0.0``; ``-0.0`` and ``0`` are not it."""

    return (
        type(value) is float
        and math.isfinite(value)
        and value == 0.0
        and math.copysign(1.0, value) > 0.0
    )


def _exact_scalar(actual: Any, expected: Any) -> bool:
    """Compare one JSON scalar by exact type.

    Python treats ``True == 1 == 1.0`` and ``False == 0`` as equal, so a plain
    ``!=`` comparison would accept a boolean substituted for an exit code, an
    integer substituted for a policy flag, or a float substituted for a count.
    Formal v2 evidence must match the producer's exact JSON type.
    """

    if expected is None:
        return actual is None
    if type(actual) is not type(expected):
        return False
    return actual == expected


def _check_exact_scalars(
    value: Any,
    expected: Mapping[str, Any],
    *,
    field: str,
    errors: list[str],
) -> None:
    if not isinstance(value, Mapping):
        errors.append(f"{field} must be an object")
        return
    for name, wanted in expected.items():
        actual = value.get(name)
        if not _exact_scalar(actual, wanted):
            errors.append(
                f"{field}.{name}={actual!r}, expected {wanted!r}"
            )


def _validate_exact_gpu_record(
    value: Any,
    *,
    field: str,
    expected_index: int,
    expected_uuid: str,
    errors: list[str],
) -> None:
    """Require the complete trusted nvidia-smi GPU record for one device."""

    if not isinstance(value, Mapping):
        errors.append(f"{field} must be an object")
        return
    if set(value) != set(_GPU_RECORD_KEYS):
        errors.append(f"{field} keys are not exact: {sorted(value)}")
        return
    index = value.get("index")
    if (
        isinstance(index, bool)
        or not isinstance(index, int)
        or index != expected_index
    ):
        errors.append(
            f"{field}.index={index!r}, expected {expected_index!r}"
        )
    uuid = value.get("uuid")
    if not isinstance(uuid, str) or uuid != expected_uuid:
        errors.append(f"{field}.uuid does not match the declared GPU")
    for name in (
        "memory_total_mib",
        "memory_used_mib",
        "utilization_gpu_percent",
    ):
        number = value.get(name)
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or number < 0
        ):
            errors.append(f"{field}.{name} is not a non-negative integer")
    for name in ("name", "pci_bus_id", "driver_version"):
        text = value.get(name)
        if not isinstance(text, str) or not text:
            errors.append(f"{field}.{name} is not a non-empty string")


def _validate_gpu_hardware_identity(
    value: Any,
    *,
    field: str,
    errors: list[str],
) -> None:
    """Require the board identity a GPU state record does not carry.

    Boards sharing a die, SM count and driver can still differ in VBIOS,
    vendor, settable power ceiling and NUMA attachment, all of which move
    sustained clocks or host-transfer bandwidth.  Binding them into the cell
    stops a profile from being reused on a board it does not describe, and
    pins the power ceiling to the vendor default so a raised limit cannot
    silently invalidate a comparison.
    """

    if not isinstance(value, Mapping):
        errors.append(f"{field} must be an object")
        return
    if set(value) != set(_V2_GPU_HARDWARE_IDENTITY_KEYS):
        errors.append(f"{field} keys are not exact: {sorted(value)}")
        return
    for name in (
        "vbios_version",
        "subsystem_vendor_id",
        "subsystem_device_id",
    ):
        text = value.get(name)
        if not isinstance(text, str) or not text:
            errors.append(f"{field}.{name} is not a non-empty string")
    numa = value.get("numa_node")
    if isinstance(numa, bool) or not isinstance(numa, int):
        errors.append(f"{field}.numa_node is not an integer")
    for name in (
        "max_sm_clock_mhz",
        "max_memory_clock_mhz",
        "max_pcie_link_gen",
        "max_pcie_link_width",
    ):
        _require_positive_json_integer(
            value.get(name), field=f"{field}.{name}", errors=errors
        )
    limit = value.get("power_limit_w")
    default = value.get("power_default_limit_w")
    ceiling = value.get("power_max_limit_w")
    for name, number in (
        ("power_limit_w", limit),
        ("power_default_limit_w", default),
        ("power_max_limit_w", ceiling),
    ):
        if type(number) is not float or not math.isfinite(number) or number <= 0:
            errors.append(f"{field}.{name} is not a positive float")
    if type(limit) is float and type(default) is float and limit != default:
        errors.append(
            f"{field} power limit {limit} is not the vendor default {default}"
        )


def _validate_empty_compute_processes(
    value: Any,
    *,
    field: str,
    errors: list[str],
) -> None:
    if not isinstance(value, list):
        errors.append(f"{field} must be a list")
        return
    if value:
        errors.append(
            f"{field} must be empty for an accepted formal cell"
        )


def _validate_recorded_mps_processes(
    value: Any,
    *,
    field: str,
    errors: list[str],
) -> None:
    """Validate recorded host MPS daemons without requiring their absence.

    The frozen Gate-A decision records host MPS control daemons as provenance
    and proves isolation with the NVIDIA-documented empty-pipe-directory
    bypass, which the surrounding checks enforce separately.
    """

    if not isinstance(value, list):
        errors.append(f"{field} must be a list")
        return
    for index, item in enumerate(value):
        if (
            not isinstance(item, Mapping)
            or set(item) != set(_MPS_PROCESS_RECORD_KEYS)
        ):
            errors.append(f"{field}[{index}] keys are not exact")
            continue
        pid = item.get("pid")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            errors.append(f"{field}[{index}].pid is invalid")
        for name in ("command", "arguments"):
            text = item.get(name)
            if not isinstance(text, str) or not text:
                errors.append(f"{field}[{index}].{name} is invalid")


def _validate_baseline_reservation_revalidation(
    value: Any,
    *,
    field: str,
    errors: list[str],
) -> None:
    if not isinstance(value, Mapping):
        errors.append(f"{field} must be an object")
        return
    if set(value) != set(_V2_RESERVATION_REVALIDATION_KEYS):
        errors.append(f"{field} keys are not exact: {sorted(value)}")
    if not _is_canonical_utc_microsecond(value.get("captured_at_utc")):
        errors.append(f"{field}.captured_at_utc is not canonical UTC")
    if (
        value.get("required_for_mode") is not False
        or not _exact_json_equal(
            value.get("checks"),
            {"reservation_not_required_for_baseline": True},
        )
        or value.get("passed") is not True
    ):
        errors.append(
            f"{field} is not the exact baseline no-reservation contract"
        )


def _validate_baseline_final_launch_preflight(
    value: Any,
    *,
    field: str,
    expected_gpu: int,
    expected_uuid: str,
    errors: list[str],
) -> None:
    """Require the complete zero-horizon baseline final-preflight record.

    Event sequence is the logical lifecycle order.  Wall clocks may step
    between syscalls, so this validator enforces the producer's exact
    timestamp encoding and the baseline zero-horizon identity instead of
    inferring chronology from cross-event timestamps.
    """

    if not isinstance(value, Mapping):
        errors.append(f"{field} must be an object")
        return
    if set(value) != set(_V2_FINAL_PREFLIGHT_KEYS):
        errors.append(f"{field} keys are not exact: {sorted(value)}")
    captured_at = value.get("captured_at_utc")
    required_until = value.get("required_until_utc")
    horizon = value.get("required_horizon_s")
    if not _is_canonical_utc_microsecond(captured_at):
        errors.append(f"{field}.captured_at_utc is not canonical UTC")
    if not _is_canonical_utc_microsecond(required_until):
        errors.append(f"{field}.required_until_utc is not canonical UTC")
    if not _is_positive_zero_float(horizon) or required_until != captured_at:
        errors.append(
            f"{field} is not the exact baseline zero-horizon contract"
        )
    if value.get("errors") != []:
        errors.append(f"{field}.errors must be empty")
    if value.get("passed") is not True:
        errors.append(f"{field}.passed is not true")
    checks = value.get("checks")
    _all_true_checks(checks, field=f"{field}.checks", errors=errors)
    if (
        not isinstance(checks, Mapping)
        or set(checks) != set(_V2_BASELINE_FINAL_PREFLIGHT_CHECK_KEYS)
    ):
        errors.append(
            f"{field}.checks are not the exact baseline check contract"
        )
    _validate_exact_gpu_record(
        value.get("gpu"),
        field=f"{field}.gpu",
        expected_index=expected_gpu,
        expected_uuid=expected_uuid,
        errors=errors,
    )
    _validate_empty_compute_processes(
        value.get("compute_processes"),
        field=f"{field}.compute_processes",
        errors=errors,
    )
    _validate_recorded_mps_processes(
        value.get("mps_processes"),
        field=f"{field}.mps_processes",
        errors=errors,
    )


def _validate_baseline_post_health(
    value: Any,
    *,
    field: str,
    expected_gpu: int,
    expected_uuid: str,
    errors: list[str],
) -> None:
    """Require the complete baseline post-probe health record."""

    if not isinstance(value, Mapping):
        errors.append(f"{field} must be an object")
        return
    if set(value) != set(_V2_POST_HEALTH_KEYS):
        errors.append(f"{field} keys are not exact: {sorted(value)}")
    if value.get("error") is not None:
        errors.append(f"{field}.error must be null")
    checks = value.get("checks")
    _all_true_checks(checks, field=f"{field}.checks", errors=errors)
    if (
        not isinstance(checks, Mapping)
        or set(checks) != set(_V2_BASELINE_POST_HEALTH_CHECK_KEYS)
    ):
        errors.append(
            f"{field}.checks are not the exact baseline check contract"
        )
    _validate_exact_gpu_record(
        value.get("gpu"),
        field=f"{field}.gpu",
        expected_index=expected_gpu,
        expected_uuid=expected_uuid,
        errors=errors,
    )
    _validate_empty_compute_processes(
        value.get("compute_processes"),
        field=f"{field}.compute_processes",
        errors=errors,
    )
    _validate_recorded_mps_processes(
        value.get("mps_processes"),
        field=f"{field}.mps_processes",
        errors=errors,
    )
    _validate_baseline_reservation_revalidation(
        value.get("reservation_revalidation"),
        field=f"{field}.reservation_revalidation",
        errors=errors,
    )


def _validate_source_revalidation(
    value: Any,
    *,
    field: str,
    expected_phase: str,
    errors: list[str],
) -> None:
    expected_keys = _EVENT_PAYLOAD_ALLOWED_KEYS[
        {
            "prelaunch": "run.source_revalidated",
            "preexec_after_monitor_and_signal_block": (
                "run.source_preexec_revalidated"
            ),
            "postrun": "run.source_postvalidated",
        }[expected_phase]
    ]
    if not isinstance(value, Mapping) or set(value) != set(expected_keys):
        errors.append(f"{field} keys are not exact")
        return
    _check_exact_scalars(
        value,
        {
            "phase": expected_phase,
            "completed": True,
            "error": None,
            "canonical_paths_selected": True,
            "source_eligible_for_local_pass": True,
            "formal_source_launch_permitted": True,
            "snapshot_matches_initial": True,
            "passed_for_launch": True,
            "passed_for_local_acceptance": True,
        },
        field=field,
        errors=errors,
    )
    for name in ("build_record", "binding", "checks", "required_checks"):
        if not isinstance(value.get(name), Mapping):
            errors.append(f"{field}.{name} must be an object")
    _all_true_checks(
        value.get("checks"),
        field=f"{field}.checks",
        errors=errors,
    )
    _all_true_checks(
        value.get("required_checks"),
        field=f"{field}.required_checks",
        errors=errors,
    )
    expected_digest = value.get("expected_snapshot_sha256")
    observed_digest = value.get("observed_snapshot_sha256")
    if (
        not _is_sha256(expected_digest)
        or observed_digest != expected_digest
    ):
        errors.append(f"{field} snapshot SHA256 binding is invalid")


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
    evidence_snapshot: _EvidenceDirectorySnapshot | None = None,
) -> dict[str, Any]:
    errors: list[str] = []
    evidence = (
        evidence_snapshot
        if evidence_snapshot is not None
        else _snapshot_evidence_directory(run_directory)
    )
    hashes = evidence.hashes
    errors.extend(evidence.errors)
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
        manifest_value = _legacy_snapshot_json_object(
            evidence,
            "manifest.json",
        )
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
        outcome = _legacy_snapshot_json_object(evidence, "outcome.json")
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
        native = _legacy_snapshot_json_object(evidence, "native.json")
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

    stdout_native, stdout_error = _legacy_snapshot_stdout_native(evidence)
    if stdout_error is not None:
        errors.append(stdout_error)
    elif native is not None and canonical_json(stdout_native) != canonical_json(native):
        errors.append("stdout native JSON does not match native.json")

    try:
        stderr = _snapshot_file(evidence, "stderr.log").content
    except ValueError as exc:
        errors.append(f"invalid stderr.log: {exc}")
    else:
        if stderr:
            errors.append("stderr.log must be empty for an accepted baseline")

    try:
        command = _legacy_snapshot_json_object(evidence, "command.json")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"invalid command.json: {exc}")
    else:
        _validate_baseline_command(
            command,
            expected_uuid=expected_uuid,
            errors=errors,
        )

    events, event_errors = _legacy_snapshot_events(evidence)
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
        "runner_contract_schema": CELL_SCHEMA_VERSION,
        "formal_identity": {
            "source_revision": expected_source_revision,
            "launcher_sha256": binary_sha,
            "build_stamp_sha256": build_sha,
        },
        "binary_sha256": binary_sha,
        "build_sha256": build_sha,
        "metrics": key_metrics,
        "valid": not errors,
        "validation_errors": errors,
    }


def _validate_baseline_run_v2(
    run_directory: Path,
    *,
    expected_source_revision: str,
    expected_seed: int,
    expected_gpu: int,
    expected_trial: int,
    expected_uuid: str,
    evidence_snapshot: _EvidenceDirectorySnapshot | None = None,
) -> dict[str, Any]:
    """Strictly re-evaluate one formal runner-v2 baseline cell."""

    errors: list[str] = []
    evidence = (
        evidence_snapshot
        if evidence_snapshot is not None
        else _snapshot_evidence_directory(run_directory)
    )
    hashes = evidence.hashes
    errors.extend(evidence.errors)
    missing = [name for name in _EVIDENCE_FILES if hashes.get(name) is None]
    if missing:
        errors.append(f"missing evidence files: {missing}")

    manifest: RunManifest | None = None
    outcome: dict[str, Any] | None = None
    native: dict[str, Any] | None = None
    gate_content: Mapping[str, Any] = {}
    formal_identity: dict[str, Any] = {}
    native_binary_record: Mapping[str, Any] = {}
    manifest_formal_binding: Mapping[str, Any] = {}
    binary_sha: str | None = None
    build_sha: str | None = None
    key_metrics: dict[str, Any] = {
        "observed_sm_count": None,
        "device_sm_count": None,
        "coverage": None,
        "reported_blocks": None,
        "observed_blocks": None,
        "native_blocks": None,
    }

    try:
        manifest_value = _strict_snapshot_json_object(
            evidence,
            "manifest.json",
            allowed_keys=_RUN_MANIFEST_KEYS,
        )
        manifest_config = manifest_value.get("config")
        if (
            not isinstance(manifest_config, Mapping)
            or set(manifest_config) != set(_V2_CELL_CONFIG_KEYS)
        ):
            errors.append(
                "manifest config keys are not exact: "
                f"{sorted(manifest_config) if isinstance(manifest_config, Mapping) else manifest_config!r}"
            )
        manifest = RunManifest.from_dict(manifest_value)
    except (TypeError, ValueError) as error:
        errors.append(f"invalid manifest.json: {error}")

    config: Mapping[str, Any] = {}
    if manifest is not None:
        config = manifest.config
        if manifest.run_id != run_directory.name:
            errors.append("manifest run_id does not match directory name")
        if manifest.source_revision != expected_source_revision:
            errors.append("source_revision does not match evidence spec")
        if manifest.seed != expected_seed:
            errors.append("manifest seed does not match evidence spec")
        _check_exact_scalars(
            config,
            {
                "schema_version": CELL_SCHEMA_VERSION_V2,
                "mode": "baseline",
                "physical_gpu": expected_gpu,
                "trial": expected_trial,
                "seed": expected_seed,
                "allow_busy_gpu": False,
                "experimental_allow_unsupported_driver": False,
                "experimental_mask_off": None,
            },
            field="config",
            errors=errors,
        )
        gate_record = config.get("gate_manifest")
        if not isinstance(gate_record, Mapping):
            errors.append("config.gate_manifest must be an object")
        else:
            if set(gate_record) != set(_GATE_MANIFEST_RECORD_KEYS):
                errors.append(
                    "config.gate_manifest keys are not exact: "
                    f"{sorted(gate_record)}"
                )
            content = gate_record.get("content")
            digest = gate_record.get("sha256")
            if not isinstance(content, Mapping):
                errors.append("Gate-A v2 manifest content must be an object")
            else:
                gate_content = content
                if (
                    content.get("schema_version")
                    != GATE_MANIFEST_SCHEMA_VERSION_V2
                ):
                    errors.append("Gate-A manifest schema is not v2")
            if not _is_sha256(digest) or (
                isinstance(content, Mapping)
                and _sha256_bytes(
                    canonical_json(content).encode("utf-8")
                )
                != digest
            ):
                errors.append("Gate-A manifest content SHA256 mismatch")

        hardware = gate_content.get("hardware")
        baseline = gate_content.get("baseline")
        source = gate_content.get("source")
        if not all(
            isinstance(value, Mapping)
            for value in (hardware, baseline, source)
        ):
            errors.append("Gate-A v2 hardware/baseline/source is malformed")
        else:
            expected_blocks = int(hardware["sm_count"]) * int(
                baseline["blocks_per_sm"]
            )
            for field, expected in (
                ("iterations", baseline.get("iterations")),
                ("blocks", expected_blocks),
                ("threads_per_block", baseline.get("threads_per_block")),
            ):
                actual = config.get(field)
                if (
                    isinstance(actual, bool)
                    or not isinstance(actual, int)
                    or actual <= 0
                    or not _exact_scalar(actual, expected)
                ):
                    errors.append(
                        f"config.{field} does not match Gate-A v2 manifest"
                    )

            environment = manifest.environment
            for label, field in (
                ("initial", "selected_gpu_initial_preflight"),
                ("launch", "selected_gpu_launch_preflight"),
            ):
                _validate_exact_gpu_record(
                    environment.get(field),
                    field=f"environment.{field}",
                    expected_index=expected_gpu,
                    expected_uuid=expected_uuid,
                    errors=errors,
                )
                del label
            _validate_gpu_hardware_identity(
                environment.get("selected_gpu_hardware_identity"),
                field="environment.selected_gpu_hardware_identity",
                errors=errors,
            )
            for field in (
                "selected_gpu_compute_processes_initial",
                "selected_gpu_compute_processes_launch",
            ):
                _validate_empty_compute_processes(
                    environment.get(field),
                    field=f"environment.{field}",
                    errors=errors,
                )
            for field in (
                "host_mps_processes_initial",
                "host_mps_processes_launch",
            ):
                _validate_recorded_mps_processes(
                    environment.get(field),
                    field=f"environment.{field}",
                    errors=errors,
                )
            if environment.get("mps_bypass") != {
                "CUDA_MPS_PIPE_DIRECTORY": "",
                "basis": "NVIDIA-documented empty-pipe-directory bypass",
            }:
                errors.append(
                    "environment.mps_bypass is not the exact empty-pipe "
                    "bypass record"
                )
            libcuda_binding_checks = environment.get(
                "runtime_libcuda_build_binding_checks"
            )
            _all_true_checks(
                libcuda_binding_checks,
                field="environment.runtime_libcuda_build_binding_checks",
                errors=errors,
            )
            if (
                not isinstance(libcuda_binding_checks, Mapping)
                or set(libcuda_binding_checks)
                != set(_V2_LIBCUDA_BINDING_CHECK_KEYS)
            ):
                errors.append(
                    "environment.runtime_libcuda_build_binding_checks keys "
                    "are not exact"
                )
            native_binary = environment.get("native_binary")
            native_binary_record = (
                native_binary
                if isinstance(native_binary, Mapping)
                else {}
            )
            environment_formal = environment.get("formal_source_binding")
            manifest_formal_binding = (
                environment_formal
                if isinstance(environment_formal, Mapping)
                else {}
            )
            native_build = environment.get("native_build")
            _check_exact_scalars(
                native_build,
                {"found": True},
                field="environment.native_build",
                errors=errors,
            )
            attestation = environment.get("native_build_attestation")
            binary_sha = (
                native_binary.get("sha256")
                if isinstance(native_binary, Mapping)
                else None
            )
            build_sha = (
                native_build.get("sha256")
                if isinstance(native_build, Mapping)
                else None
            )
            attestation_identity = (
                attestation.get("identity")
                if isinstance(attestation, Mapping)
                else None
            )
            attestation_sha = (
                attestation_identity.get("sha256")
                if isinstance(attestation_identity, Mapping)
                else None
            )
            formal_identity = {
                "source_revision": expected_source_revision,
                "gate_manifest_sha256": gate_record.get("sha256"),
                "launcher_sha256": binary_sha,
                "real_probe_sha256": source.get(
                    "approved_real_probe_sha256"
                ),
                "build_stamp_sha256": build_sha,
                "build_attestation_sha256": attestation_sha,
            }
            for name, digest in formal_identity.items():
                if name != "source_revision" and not _is_sha256(digest):
                    errors.append(f"formal identity {name} is malformed")
            pin_pairs = {
                "launcher": (
                    binary_sha,
                    source.get("approved_launcher_sha256"),
                ),
                "build_stamp": (
                    build_sha,
                    source.get("approved_build_stamp_sha256"),
                ),
                "build_attestation": (
                    attestation_sha,
                    source.get("approved_build_attestation_sha256"),
                ),
            }
            for label, (actual, expected) in pin_pairs.items():
                if actual != expected:
                    errors.append(f"{label} does not match manifest pin")

    try:
        outcome = _strict_snapshot_json_object(
            evidence,
            "outcome.json",
            allowed_keys=_V2_OUTCOME_KEYS,
            required_keys=frozenset(
                {
                    "schema_version",
                    "completed_at_utc",
                    "exit_code",
                    "process_exit_code",
                    "raw_process_return_code",
                    "timed_out",
                    "child_launch_error",
                    "child_interruption",
                    "process_group_reaped",
                    "process_group_health",
                    "gpu_lease",
                    "native_output_found",
                    "native_output_error",
                    "native_status",
                    "driver_policy",
                    "driver_policy_permitted",
                    "manifest_policy",
                    "manifest_policy_permitted",
                    "safety_policy",
                    "preflight_permitted",
                    "formal_source_binding",
                    "formal_source_checks",
                    "formal_source_required_checks",
                    "formal_source_preflight_permitted",
                    "source_eligible_for_local_pass",
                    "source_prelaunch_revalidation",
                    "source_preexec_revalidation",
                    "source_postrun_revalidation",
                    "final_launch_preflight",
                    "launch_commit_reservation_revalidation",
                    "libcuda_final_revalidation",
                    "launcher_fd_final",
                    "formal_launcher_threat_boundaries",
                    "post_health",
                    "semantic_acceptance",
                    "semantic_metrics",
                    "masked_health_monitor_status",
                    "masked_health_monitor",
                    "masked_health_monitor_checks",
                    "local_probe_passed",
                    "requires_matrix_validation",
                    "quarantine_required",
                    "quarantine_reasons",
                    "accepted",
                }
            ),
        )
    except ValueError as error:
        errors.append(f"invalid outcome.json: {error}")
    if outcome is not None:
        _check_exact_scalars(
            outcome,
            {
                "schema_version": OUTCOME_SCHEMA_VERSION_V2,
                "exit_code": 0,
                "process_exit_code": 0,
                "raw_process_return_code": 0,
                "timed_out": False,
                "child_launch_error": None,
                "child_interruption": None,
                "process_group_reaped": True,
                "native_output_found": True,
                "native_output_error": None,
                "native_status": "ok",
                "driver_policy_permitted": True,
                "manifest_policy_permitted": True,
                "preflight_permitted": True,
                "formal_source_preflight_permitted": True,
                "source_eligible_for_local_pass": True,
                "local_probe_passed": True,
                "requires_matrix_validation": False,
                "accepted": True,
                "quarantine_required": False,
                "quarantine_reasons": [],
                "masked_health_monitor_status": "not_applicable",
            },
            field="outcome",
            errors=errors,
        )
        if not _is_canonical_utc_microsecond(
            outcome.get("completed_at_utc")
        ):
            errors.append("outcome.completed_at_utc is not canonical UTC")
        for field in (
            "driver_policy",
            "manifest_policy",
            "safety_policy",
            "formal_source_checks",
            "formal_source_required_checks",
            "semantic_acceptance",
        ):
            _all_true_checks(
                outcome.get(field),
                field=f"outcome.{field}",
                errors=errors,
            )
        driver_policy = outcome.get("driver_policy")
        if (
            not isinstance(driver_policy, Mapping)
            or set(driver_policy) != set(_DRIVER_POLICY_KEYS)
        ):
            errors.append("outcome.driver_policy keys are not exact")
        outcome_formal_binding = outcome.get("formal_source_binding")
        if not isinstance(outcome_formal_binding, Mapping):
            errors.append("outcome.formal_source_binding must be an object")
        process_group_health = outcome.get("process_group_health")
        if not isinstance(process_group_health, Mapping):
            errors.append("outcome.process_group_health must be an object")
        else:
            for name in (
                "child_reaped",
                "process_group_quiesced",
                "process_group_reaped",
            ):
                if process_group_health.get(name) is not True:
                    errors.append(
                        f"outcome.process_group_health.{name} is not true"
                    )
            if process_group_health.get("errors") != []:
                errors.append(
                    "outcome.process_group_health contains errors"
                )
        gpu_lease = outcome.get("gpu_lease")
        if (
            not isinstance(gpu_lease, Mapping)
            or gpu_lease.get("kind") != "gpu_uuid"
            or gpu_lease.get("gpu_uuid") != expected_uuid
            or gpu_lease.get("preexisting_quarantine") is not False
        ):
            errors.append("outcome.gpu_lease is not a clean held UUID lease")
        if outcome.get("masked_health_monitor") is not None:
            errors.append(
                "baseline outcome.masked_health_monitor must be null"
            )
        if outcome.get("masked_health_monitor_checks") != {}:
            errors.append(
                "baseline masked_health_monitor_checks must be empty"
            )
        for phase, expected_phase in (
            ("source_prelaunch_revalidation", "prelaunch"),
            (
                "source_preexec_revalidation",
                "preexec_after_monitor_and_signal_block",
            ),
            ("source_postrun_revalidation", "postrun"),
        ):
            _validate_source_revalidation(
                outcome.get(phase),
                field=f"outcome.{phase}",
                expected_phase=expected_phase,
                errors=errors,
            )
        _validate_baseline_final_launch_preflight(
            outcome.get("final_launch_preflight"),
            field="outcome.final_launch_preflight",
            expected_gpu=expected_gpu,
            expected_uuid=expected_uuid,
            errors=errors,
        )
        launch_commit = outcome.get(
            "launch_commit_reservation_revalidation"
        )
        expected_launch_commit_keys = {
            "captured_at_utc",
            "required_for_mode",
            "required_horizon_s",
            "required_until_utc",
            "checks",
            "passed",
            "error",
        }
        if not isinstance(launch_commit, Mapping):
            errors.append(
                "outcome.launch_commit_reservation_revalidation "
                "must be an object"
            )
        else:
            if set(launch_commit) != expected_launch_commit_keys:
                errors.append(
                    "outcome.launch_commit_reservation_revalidation "
                    "keys are not exact"
                )
            captured_at = launch_commit.get("captured_at_utc")
            required_until = launch_commit.get("required_until_utc")
            required_horizon = launch_commit.get("required_horizon_s")
            # Event sequence is the logical lifecycle order.  Wall clocks may
            # step between syscalls, so this validator enforces the producer's
            # exact timestamp encoding and baseline zero-horizon identity but
            # does not infer chronology from cross-event timestamps.
            if not _is_canonical_utc_microsecond(captured_at):
                errors.append(
                    "baseline launch-commit captured_at_utc is invalid"
                )
            if not _is_canonical_utc_microsecond(required_until):
                errors.append(
                    "baseline launch-commit required_until_utc is invalid"
                )
            if (
                launch_commit.get("required_for_mode") is not False
                or not _is_positive_zero_float(required_horizon)
                or required_until != captured_at
                or not _exact_json_equal(
                    launch_commit.get("checks"),
                    {"reservation_not_required_for_baseline": True},
                )
                or launch_commit.get("passed") is not True
                or launch_commit.get("error") is not None
            ):
                errors.append(
                    "baseline launch-commit reservation evidence "
                    "is not the exact no-reservation contract"
                )
        _validate_baseline_post_health(
            outcome.get("post_health"),
            field="outcome.post_health",
            expected_gpu=expected_gpu,
            expected_uuid=expected_uuid,
            errors=errors,
        )

    try:
        native = _strict_snapshot_json_object(
            evidence,
            "native.json",
            allowed_keys=_V2_NATIVE_KEYS,
            required_keys=_V2_NATIVE_REQUIRED_KEYS,
        )
        _validate_v2_native_schema(native, errors=errors)
    except ValueError as error:
        errors.append(f"invalid native.json: {error}")
    try:
        stderr_text = _strict_snapshot_text(evidence, "stderr.log")
    except ValueError as error:
        stderr_text = ""
        errors.append(f"invalid stderr.log: {error}")

    if native is not None and manifest is not None:
        hardware = gate_content.get("hardware")
        baseline = gate_content.get("baseline")
        if (
            native.get("schema_version") != NATIVE_SCHEMA_VERSION_V2
            or not isinstance(hardware, Mapping)
            or not isinstance(baseline, Mapping)
        ):
            errors.append("native/Gate-A v2 contract is malformed")
        else:
            checks, metrics, accepted = evaluate_probe(
                native,
                expected_mode="baseline",
                expected_enabled_tpc=int(config.get("enabled_tpc", 0)),
                expected_driver_version=int(hardware["driver_api_version"]),
                expected_runtime_version=int(
                    hardware["runtime_api_version"]
                ),
                expected_iterations=int(config["iterations"]),
                process_exit_code=int(
                    outcome.get("process_exit_code", -1)
                    if outcome is not None
                    else -1
                ),
                expected_device_uuid=expected_uuid,
                expected_device_name=str(hardware["gpu_name"]),
                expected_sm_count=int(hardware["sm_count"]),
                expected_compute_capability=hardware[
                    "compute_capability"
                ],
                expected_blocks=int(config["blocks"]),
                expected_threads_per_block=int(
                    config["threads_per_block"]
                ),
                expected_device_ordinal=0,
                expected_tpc_count=None,
                expected_parent_pid=None,
                stderr=stderr_text,
                allowed_observed_sm_counts=(1, 2),
                minimum_sm_coverage=float(
                    baseline["minimum_sm_coverage_fraction"]
                ),
            )
            if not accepted:
                errors.extend(
                    f"recomputed semantic check failed: {name}"
                    for name, passed in checks.items()
                    if not passed
                )
            if outcome is not None:
                if not _exact_json_equal(
                    outcome.get("semantic_acceptance"), checks
                ):
                    errors.append(
                        "outcome semantic_acceptance differs from recomputation"
                    )
                if not _exact_json_equal(
                    outcome.get("semantic_metrics"), metrics
                ):
                    errors.append(
                        "outcome semantic_metrics differs from recomputation"
                    )
            key_metrics.update(
                {
                    "observed_sm_count": metrics["observed_sm_count"],
                    "device_sm_count": metrics["device_sm_count"],
                    "coverage": metrics["sm_coverage_ratio"],
                    "reported_blocks": metrics["reported_blocks"],
                    "observed_blocks": metrics["observed_blocks"],
                    "native_blocks": native.get("blocks"),
                }
            )

    stdout_native, stdout_error = _strict_snapshot_stdout_native(evidence)
    if stdout_error is not None:
        errors.append(stdout_error)
    elif (
        native is not None
        and canonical_json(stdout_native) != canonical_json(native)
    ):
        errors.append("stdout native JSON does not match native.json")
    if stderr_text:
        errors.append("stderr.log must be empty for an accepted baseline")

    command: dict[str, Any] | None = None
    try:
        command = _strict_snapshot_json_object(
            evidence,
            "command.json",
            allowed_keys=_V2_COMMAND_KEYS,
            required_keys=frozenset(
                {
                    "argv",
                    "cwd",
                    "prepared_at_utc",
                    "environment_overrides",
                    "environment_policy",
                    "parent_death_protection",
                    "dynamic_loader_policy",
                    "signal_mask_policy",
                    "launcher_fd",
                    "launcher_fd_final",
                    "cuda_driver_probe",
                    "libcuda_final_revalidation",
                    "formal_launcher_threat_boundaries",
                }
            ),
        )
    except ValueError as error:
        errors.append(f"invalid command.json: {error}")
    else:
        _validate_v2_baseline_child_contract(
            command,
            expected_uuid=expected_uuid,
            errors=errors,
        )
        argv = command.get("argv")
        if manifest is not None:
            expected_argv = [
                str(manifest.environment["native_binary"]["path"]),
                "--mode",
                "baseline",
                "--iterations",
                str(config["iterations"]),
                "--blocks",
                str(config["blocks"]),
            ]
            if argv != expected_argv:
                errors.append("v2 baseline command argv mismatch")
        launcher_fd = command.get("launcher_fd")
        for field, allowed_keys in _COMMAND_NESTED_KEYS.items():
            nested = command.get(field)
            if (
                nested is not None
                and (
                    not isinstance(nested, Mapping)
                    or set(nested) != set(allowed_keys)
                )
            ):
                errors.append(f"command.{field} keys are not exact")
        environment_overrides = command.get("environment_overrides")
        if (
            isinstance(environment_overrides, Mapping)
            and set(environment_overrides)
            - {
                "LANG",
                "LC_ALL",
                "TZ",
                "CUDA_CACHE_DISABLE",
                "CUDA_VISIBLE_DEVICES",
                "CUDA_MPS_PIPE_DIRECTORY",
                "MASK_OFF",
                "BURSTSERVE_PARENT_PID",
            }
        ):
            errors.append(
                "command.environment_overrides contains unknown keys"
            )
        expected_launcher_fd_keys = {
            "fd",
            "path",
            "device",
            "inode",
            "mode",
            "size",
            "mtime_ns",
            "sha256",
            "execution_path",
            "passed_explicitly",
        }
        if not isinstance(launcher_fd, Mapping):
            errors.append("v2 launcher FD evidence is invalid")
        else:
            if set(launcher_fd) != expected_launcher_fd_keys:
                errors.append("command.launcher_fd keys are not exact")
            fd_value = launcher_fd.get("fd")
            if (
                isinstance(fd_value, bool)
                or not isinstance(fd_value, int)
                or fd_value < 3
            ):
                errors.append("command.launcher_fd.fd is invalid")
            if launcher_fd.get("passed_explicitly") is not True:
                errors.append(
                    "command.launcher_fd was not explicitly passed"
                )
            if (
                isinstance(fd_value, int)
                and launcher_fd.get("execution_path")
                != f"/proc/self/fd/{fd_value}"
            ):
                errors.append(
                    "command.launcher_fd.execution_path is not FD-bound"
                )
            if not _is_sha256(launcher_fd.get("sha256")):
                errors.append("command.launcher_fd SHA256 is invalid")
            mode = launcher_fd.get("mode")
            if (
                isinstance(mode, bool)
                or not isinstance(mode, int)
                or not stat.S_ISREG(mode)
            ):
                errors.append("command.launcher_fd mode is not regular")

    events, event_errors = _strict_snapshot_events(evidence)
    errors.extend(event_errors)
    _validate_event_identity(
        events,
        expected_run_id=run_directory.name,
        errors=errors,
    )
    expected_event_types = [
        "run.preflight",
        "run.source_revalidated",
        "run.source_preexec_revalidated",
        "run.final_launch_preflight",
        "run.started",
        "run.source_postvalidated",
        "run.completed",
    ]
    event_types = [event.event_type for event in events]
    if event_types != expected_event_types:
        errors.append(
            f"v2 baseline event types are {event_types}, "
            f"expected {expected_event_types}"
        )
    for event in events:
        expected_payload_keys = _EVENT_PAYLOAD_ALLOWED_KEYS.get(
            event.event_type
        )
        if (
            expected_payload_keys is not None
            and set(event.payload) != set(expected_payload_keys)
        ):
            errors.append(
                f"{event.event_type} payload keys are not exact"
            )
    source_event_bindings = (
        (
            "run.source_revalidated",
            "source_prelaunch_revalidation",
        ),
        (
            "run.source_preexec_revalidated",
            "source_preexec_revalidation",
        ),
        (
            "run.source_postvalidated",
            "source_postrun_revalidation",
        ),
    )
    for event_type, outcome_field in source_event_bindings:
        matching = [
            event for event in events if event.event_type == event_type
        ]
        if (
            len(matching) != 1
            or outcome is None
            or canonical_json(matching[0].payload)
            != canonical_json(outcome.get(outcome_field))
        ):
            errors.append(
                f"{event_type} differs from outcome.{outcome_field}"
            )
    preflight_events = [
        event for event in events if event.event_type == "run.preflight"
    ]
    if len(preflight_events) == 1 and outcome is not None:
        preflight_payload = preflight_events[0].payload
        for field in (
            "driver_policy",
            "driver_policy_permitted",
            "manifest_policy",
            "manifest_policy_permitted",
            "safety_policy",
            "preflight_permitted",
            "formal_source_binding",
            "formal_source_checks",
            "formal_source_required_checks",
            "formal_source_preflight_permitted",
            "source_eligible_for_local_pass",
        ):
            if canonical_json(preflight_payload.get(field)) != canonical_json(
                outcome.get(field)
            ):
                errors.append(
                    f"run.preflight.{field} differs from outcome"
                )
        if manifest is not None:
            for payload_field, environment_field in (
                ("gpu_initial", "selected_gpu_initial_preflight"),
                ("gpu_launch", "selected_gpu_launch_preflight"),
                (
                    "gpu_hardware_identity",
                    "selected_gpu_hardware_identity",
                ),
                (
                    "compute_processes_initial",
                    "selected_gpu_compute_processes_initial",
                ),
                (
                    "compute_processes_launch",
                    "selected_gpu_compute_processes_launch",
                ),
                ("mps_processes_initial", "host_mps_processes_initial"),
                ("mps_processes_launch", "host_mps_processes_launch"),
                (
                    "runtime_libcuda_build_binding_checks",
                    "runtime_libcuda_build_binding_checks",
                ),
            ):
                if canonical_json(
                    preflight_payload.get(payload_field)
                ) != canonical_json(
                    manifest.environment.get(environment_field)
                ):
                    errors.append(
                        f"run.preflight.{payload_field} differs from "
                        f"manifest environment.{environment_field}"
                    )
    final_events = [
        event
        for event in events
        if event.event_type == "run.final_launch_preflight"
    ]
    if (
        len(final_events) != 1
        or outcome is None
        or canonical_json(final_events[0].payload)
        != canonical_json(outcome.get("final_launch_preflight"))
    ):
        errors.append(
            "final launch preflight event differs from outcome evidence"
        )
    if (
        events
        and events[-1].event_type == "run.completed"
        and outcome is not None
        and canonical_json(events[-1].payload) != canonical_json(outcome)
    ):
        errors.append(
            "terminal run.completed payload does not match outcome.json"
        )

    launcher_fd = (
        command.get("launcher_fd")
        if isinstance(command, Mapping)
        and isinstance(command.get("launcher_fd"), Mapping)
        else {}
    )
    identity_fields = _LAUNCHER_IDENTITY_FIELDS
    command_identity = {
        field: launcher_fd.get(field) for field in identity_fields
    }
    _validate_launcher_identity_types(
        command_identity,
        field="command.launcher_fd identity",
        errors=errors,
    )
    opened_identity = native_binary_record.get("opened_fd_identity")
    manifest_launcher_identity = manifest_formal_binding.get(
        "launcher_fd_identity"
    )
    outcome_formal = (
        outcome.get("formal_source_binding")
        if isinstance(outcome, Mapping)
        else None
    )
    outcome_launcher_identity = (
        outcome_formal.get("launcher_fd_identity")
        if isinstance(outcome_formal, Mapping)
        else None
    )
    started_records = [
        event for event in events if event.event_type == "run.started"
    ]
    started_payload = (
        started_records[0].payload
        if len(started_records) == 1
        else {}
    )
    started_identity = (
        started_payload.get("launcher_fd_identity")
        if isinstance(started_payload, Mapping)
        else None
    )
    for label, identity in (
        ("manifest native_binary opened FD", opened_identity),
        ("manifest formal launcher", manifest_launcher_identity),
        ("outcome formal launcher", outcome_launcher_identity),
        ("run.started launcher", started_identity),
    ):
        if not isinstance(identity, Mapping):
            errors.append(f"{label} identity is missing")
        elif set(identity) != set(identity_fields):
            errors.append(f"{label} identity keys are not exact")
        elif not _exact_json_equal(
            {field: identity.get(field) for field in identity_fields},
            command_identity,
        ):
            errors.append(f"{label} identity differs from command launcher")

    command_launcher_final = (
        command.get("launcher_fd_final")
        if isinstance(command, Mapping)
        else None
    )
    expected_launcher_final_keys = {
        "captured_at_utc",
        "completed",
        "error",
        "identity",
        "checks",
        "passed",
        "same_uid_out_of_band_write_threat_boundary",
    }
    if not isinstance(command_launcher_final, Mapping):
        errors.append("command.launcher_fd_final must be an object")
    else:
        if set(command_launcher_final) != expected_launcher_final_keys:
            errors.append("command.launcher_fd_final keys are not exact")
        if (
            command_launcher_final.get("completed") is not True
            or command_launcher_final.get("error") is not None
            or command_launcher_final.get("passed") is not True
        ):
            errors.append("command.launcher_fd_final did not pass")
        _all_true_checks(
            command_launcher_final.get("checks"),
            field="command.launcher_fd_final.checks",
            errors=errors,
        )
        final_identity = command_launcher_final.get("identity")
        if not isinstance(final_identity, Mapping):
            errors.append(
                "command.launcher_fd_final.identity is missing"
            )
        elif set(final_identity) != set(identity_fields):
            errors.append(
                "command.launcher_fd_final.identity keys are not exact"
            )
        elif not _exact_json_equal(
            {
                field: final_identity.get(field)
                for field in identity_fields
            },
            command_identity,
        ):
            errors.append(
                "command.launcher_fd_final identity differs from initial FD"
            )
        boundary = command_launcher_final.get(
            "same_uid_out_of_band_write_threat_boundary"
        )
        if not isinstance(boundary, str) or not boundary:
            errors.append(
                "command.launcher_fd_final threat boundary is missing"
            )

    outcome_launcher_final = (
        outcome.get("launcher_fd_final")
        if isinstance(outcome, Mapping)
        else None
    )
    started_launcher_final = (
        started_payload.get("launcher_fd_final")
        if isinstance(started_payload, Mapping)
        else None
    )
    for label, value in (
        ("outcome launcher_fd_final", outcome_launcher_final),
        ("run.started launcher_fd_final", started_launcher_final),
    ):
        if not isinstance(value, Mapping):
            errors.append(f"{label} is missing")
        elif canonical_json(value) != canonical_json(
            command_launcher_final
        ):
            errors.append(f"{label} differs from command evidence")

    started_final_preflight = (
        started_payload.get("final_launch_preflight")
        if isinstance(started_payload, Mapping)
        else None
    )
    if not isinstance(started_final_preflight, Mapping):
        errors.append(
            "run.started final launch preflight evidence is missing"
        )
    elif outcome is None or canonical_json(
        started_final_preflight
    ) != canonical_json(outcome.get("final_launch_preflight")):
        errors.append(
            "run.started final launch preflight differs from outcome"
        )
    elif (
        len(final_events) != 1
        or canonical_json(started_final_preflight)
        != canonical_json(final_events[0].payload)
    ):
        errors.append(
            "run.started final launch preflight differs from the "
            "run.final_launch_preflight event"
        )

    outcome_launch_commit = (
        outcome.get("launch_commit_reservation_revalidation")
        if isinstance(outcome, Mapping)
        else None
    )
    started_launch_commit = (
        started_payload.get("launch_commit_reservation_revalidation")
        if isinstance(started_payload, Mapping)
        else None
    )
    if not isinstance(outcome_launch_commit, Mapping):
        errors.append(
            "outcome launch-commit reservation evidence is missing"
        )
    if not isinstance(started_launch_commit, Mapping):
        errors.append(
            "run.started launch-commit reservation evidence is missing"
        )
    elif canonical_json(started_launch_commit) != canonical_json(
        outcome_launch_commit
    ):
        errors.append(
            "run.started launch-commit reservation evidence differs "
            "from outcome"
        )

    command_driver_probe = (
        command.get("cuda_driver_probe")
        if isinstance(command, Mapping)
        else None
    )
    manifest_environment = (
        manifest.environment if manifest is not None else {}
    )
    manifest_driver_probe = manifest_environment.get("cuda_driver_probe")
    if (
        not isinstance(command_driver_probe, Mapping)
        or not isinstance(manifest_driver_probe, Mapping)
        or canonical_json(command_driver_probe)
        != canonical_json(manifest_driver_probe)
    ):
        errors.append(
            "CUDA driver probe is missing or differs across evidence"
        )
    driver_hardware = gate_content.get("hardware")
    driver_hardware = (
        driver_hardware
        if isinstance(driver_hardware, Mapping)
        else {}
    )
    if isinstance(command_driver_probe, Mapping):
        probe_version = command_driver_probe.get("version")
        if (
            isinstance(probe_version, bool)
            or not isinstance(probe_version, int)
            or not _exact_scalar(
                probe_version,
                driver_hardware.get("driver_api_version"),
            )
        ):
            errors.append("CUDA driver probe version differs from manifest")
        load_path = command_driver_probe.get("load_path")
        if (
            not isinstance(load_path, str)
            or not re.fullmatch(r"/proc/self/fd/[0-9]+", load_path)
        ):
            errors.append("CUDA driver probe was not FD-bound")
        if command_driver_probe.get("creates_cuda_context") is not False:
            errors.append(
                "CUDA driver version probe context policy is invalid"
            )
        python_boundary = command_driver_probe.get(
            "python_pre_main_threat_boundary"
        )
        if not isinstance(python_boundary, str) or not python_boundary:
            errors.append(
                "CUDA driver probe pre-main threat boundary is missing"
            )
    initial_libcuda_identity = (
        command_driver_probe.get("library_identity")
        if isinstance(command_driver_probe, Mapping)
        else None
    )
    if not isinstance(initial_libcuda_identity, Mapping):
        errors.append("initial libcuda identity is missing")
    else:
        _validate_libcuda_identity_types(
            initial_libcuda_identity,
            field="cuda_driver_probe.library_identity",
            errors=errors,
        )
        link_path = initial_libcuda_identity.get("link_path")
        resolved_path = initial_libcuda_identity.get("resolved_path")
        link_identity = initial_libcuda_identity.get("link_identity")
        target_identity = initial_libcuda_identity.get("target_identity")
        if (
            not isinstance(link_path, str)
            or not Path(link_path).is_absolute()
            or not isinstance(resolved_path, str)
            or not Path(resolved_path).is_absolute()
        ):
            errors.append("libcuda paths are not absolute")
        if (
            not isinstance(link_identity, Mapping)
            or not _exact_scalar(link_identity.get("uid"), 0)
            or isinstance(link_identity.get("mode"), bool)
            or not isinstance(link_identity.get("mode"), int)
            or not stat.S_ISLNK(int(link_identity["mode"]))
        ):
            errors.append("libcuda link identity is not root-owned symlink")
        target_mode = (
            target_identity.get("mode")
            if isinstance(target_identity, Mapping)
            else None
        )
        if (
            not isinstance(target_identity, Mapping)
            or not _exact_scalar(target_identity.get("uid"), 0)
            or isinstance(target_mode, bool)
            or not isinstance(target_mode, int)
            or not stat.S_ISREG(target_mode)
            or stat.S_IMODE(target_mode) & 0o022
            or not _is_sha256(target_identity.get("sha256"))
        ):
            errors.append(
                "libcuda target identity is not a root-owned read-only "
                "regular artifact"
            )
        formal_build_stamp = manifest_formal_binding.get("build_stamp")
        formal_build_stamp = (
            formal_build_stamp
            if isinstance(formal_build_stamp, Mapping)
            else {}
        )
        formal_stamp_fields = formal_build_stamp.get("fields")
        formal_stamp_fields = (
            formal_stamp_fields
            if isinstance(formal_stamp_fields, Mapping)
            else {}
        )
        if (
            initial_libcuda_identity.get("resolved_path")
            != formal_stamp_fields.get("LIBCUDA_LINK_LIBRARY")
            or not isinstance(target_identity, Mapping)
            or target_identity.get("sha256")
            != formal_stamp_fields.get("LIBCUDA_LINK_LIBRARY_SHA256")
        ):
            errors.append(
                "runtime libcuda identity differs from attested build stamp"
            )
    command_libcuda_final = (
        command.get("libcuda_final_revalidation")
        if isinstance(command, Mapping)
        else None
    )
    if not isinstance(command_libcuda_final, Mapping):
        errors.append(
            "command.libcuda_final_revalidation must be an object"
        )
    elif (
        command_libcuda_final.get("completed") is not True
        or command_libcuda_final.get("error") is not None
        or command_libcuda_final.get("matches_initial") is not True
        or command_libcuda_final.get("passed") is not True
        or not _exact_json_equal(
            command_libcuda_final.get("expected_identity"),
            initial_libcuda_identity,
        )
        or not _exact_json_equal(
            command_libcuda_final.get("observed_identity"),
            initial_libcuda_identity,
        )
    ):
        errors.append("final libcuda identity revalidation did not pass")
    outcome_libcuda_final = (
        outcome.get("libcuda_final_revalidation")
        if isinstance(outcome, Mapping)
        else None
    )
    started_libcuda_final = (
        started_payload.get("libcuda_final_revalidation")
        if isinstance(started_payload, Mapping)
        else None
    )
    for label, value in (
        ("outcome libcuda final", outcome_libcuda_final),
        ("run.started libcuda final", started_libcuda_final),
    ):
        if not isinstance(value, Mapping):
            errors.append(f"{label} is missing")
        elif canonical_json(value) != canonical_json(
            command_libcuda_final
        ):
            errors.append(f"{label} differs from command evidence")

    command_boundaries = (
        command.get("formal_launcher_threat_boundaries")
        if isinstance(command, Mapping)
        else None
    )
    manifest_boundaries = manifest_environment.get(
        "formal_launcher_threat_boundaries"
    )
    outcome_boundaries = (
        outcome.get("formal_launcher_threat_boundaries")
        if isinstance(outcome, Mapping)
        else None
    )
    if (
        not isinstance(command_boundaries, Mapping)
        or set(command_boundaries)
        != {
            "python_pre_main_injection",
            "same_uid_out_of_band_launcher_write",
        }
        or any(
            not isinstance(value, str) or not value
            for value in command_boundaries.values()
        )
        or command_boundaries != manifest_boundaries
        or command_boundaries != outcome_boundaries
    ):
        errors.append(
            "formal launcher threat boundaries are missing or inconsistent"
        )
    if isinstance(command, Mapping) and isinstance(started_payload, Mapping):
        if started_payload.get("argv") != command.get("argv"):
            errors.append("run.started argv differs from command")
        if (
            started_payload.get("executed_argv0")
            != launcher_fd.get("execution_path")
        ):
            errors.append(
                "run.started executed_argv0 differs from launcher FD path"
            )
        pid = started_payload.get("pid")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            errors.append("run.started PID is invalid")
    source_for_pin = gate_content.get("source")
    source_for_pin = (
        source_for_pin if isinstance(source_for_pin, Mapping) else {}
    )
    if (
        command_identity.get("path") != native_binary_record.get("path")
        or command_identity.get("sha256") != binary_sha
        or command_identity.get("sha256")
        != source_for_pin.get("approved_launcher_sha256")
    ):
        errors.append(
            "launcher path/hash is not bound to manifest environment and pin"
        )

    return {
        "run_id": run_directory.name,
        "cell": {
            "physical_gpu": expected_gpu,
            "trial": expected_trial,
            "gpu_uuid": expected_uuid,
        },
        "runner_contract_schema": CELL_SCHEMA_VERSION_V2,
        "formal_identity": formal_identity,
        "file_sha256": hashes,
        "file_identity": evidence.identities,
        "directory_identity": dict(evidence.directory_identity),
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
    evidence_snapshot: _EvidenceDirectorySnapshot | None = None,
) -> dict[str, Any]:
    errors: list[str] = []
    evidence = (
        evidence_snapshot
        if evidence_snapshot is not None
        else _snapshot_evidence_directory(run_directory)
    )
    hashes = evidence.hashes
    errors.extend(evidence.errors)
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
        manifest = RunManifest.from_dict(
            _legacy_snapshot_json_object(evidence, "manifest.json")
        )
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
        outcome = _legacy_snapshot_json_object(evidence, "outcome.json")
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
    if evidence.files.get("native.json") is not None:
        errors.append("sealed rejection unexpectedly contains native.json")

    try:
        command = _legacy_snapshot_json_object(evidence, "command.json")
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

    stdout_snapshot = evidence.files.get("stdout.log")
    if (
        not isinstance(stdout_snapshot, _EvidenceFileSnapshot)
        or stdout_snapshot.content
    ):
        errors.append("sealed rejection stdout.log must exist and be empty")
    try:
        stderr = _snapshot_file(evidence, "stderr.log").content.decode(
            "utf-8",
            errors="strict",
        )
    except (ValueError, UnicodeError) as exc:
        errors.append(f"invalid sealed rejection stderr.log: {exc}")
    else:
        if not stderr.strip() or "reject" not in stderr.lower():
            errors.append(
                "sealed rejection stderr.log must contain non-empty rejection text"
            )
    events, event_errors = _legacy_snapshot_events(evidence)
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
        "runner_contract_schema": CELL_SCHEMA_VERSION,
        "formal_identity": {
            "source_revision": expected_source_revision,
        },
        "file_sha256": hashes,
        "valid": not errors,
        "validation_errors": errors,
    }


def _validate_sealed_rejection_v2(
    run_directory: Path,
    *,
    expected_source_revision: str,
    evidence_snapshot: _EvidenceDirectorySnapshot | None = None,
) -> dict[str, Any]:
    """Validate an authorization-only v2 rejection with no child launch."""

    errors: list[str] = []
    evidence = (
        evidence_snapshot
        if evidence_snapshot is not None
        else _snapshot_evidence_directory(run_directory)
    )
    hashes = evidence.hashes
    errors.extend(evidence.errors)
    missing = [
        name
        for name in _REJECTION_EVIDENCE_FILES
        if hashes.get(name) is None
    ]
    if missing:
        errors.append(f"missing sealed rejection evidence files: {missing}")
    if evidence.files.get("native.json") is not None:
        errors.append("sealed rejection unexpectedly contains native.json")

    manifest: RunManifest | None = None
    config: Mapping[str, Any] = {}
    gate_record: Mapping[str, Any] = {}
    gate: Mapping[str, Any] = {}
    source: Mapping[str, Any] = {}
    environment: Mapping[str, Any] = {}
    try:
        manifest_value = _strict_snapshot_json_object(
            evidence,
            "manifest.json",
            allowed_keys=_RUN_MANIFEST_KEYS,
        )
        manifest_config = manifest_value.get("config")
        if (
            not isinstance(manifest_config, Mapping)
            or set(manifest_config) != set(_V2_CELL_CONFIG_KEYS)
        ):
            errors.append(
                "manifest config keys are not exact: "
                f"{sorted(manifest_config) if isinstance(manifest_config, Mapping) else manifest_config!r}"
            )
        manifest = RunManifest.from_dict(manifest_value)
    except (TypeError, ValueError) as error:
        errors.append(f"invalid manifest.json: {error}")
    if manifest is not None:
        config = manifest.config
        environment = manifest.environment
        if manifest.run_id != run_directory.name:
            errors.append("manifest run_id does not match directory name")
        if manifest.source_revision != expected_source_revision:
            errors.append("source_revision does not match evidence spec")
        if config.get("schema_version") != CELL_SCHEMA_VERSION_V2:
            errors.append("sealed rejection cell schema is not v2")
        mode = config.get("mode")
        if mode not in {"global", "next", "stream"}:
            errors.append("v2 sealed rejection is not a masked mode")
        gate_value = config.get("gate_manifest")
        if isinstance(gate_value, Mapping):
            gate_record = gate_value
            if set(gate_value) != set(_GATE_MANIFEST_RECORD_KEYS):
                errors.append(
                    "config.gate_manifest keys are not exact: "
                    f"{sorted(gate_value)}"
                )
            content = gate_value.get("content")
            if isinstance(content, Mapping):
                gate = content
                source_value = content.get("source")
                source = (
                    source_value
                    if isinstance(source_value, Mapping)
                    else {}
                )
            digest = gate_value.get("sha256")
            if (
                not _is_sha256(digest)
                or not isinstance(content, Mapping)
                or _sha256_bytes(canonical_json(content).encode("utf-8"))
                != digest
            ):
                errors.append("Gate-A v2 manifest content SHA256 mismatch")
        else:
            errors.append("config.gate_manifest must be an object")
        if gate.get("schema_version") != GATE_MANIFEST_SCHEMA_VERSION_V2:
            errors.append("Gate-A manifest schema is not v2")
        safety = gate.get("safety")
        if not isinstance(safety, Mapping):
            errors.append("Gate-A v2 safety policy is malformed")
        else:
            approved_modes = safety.get("approved_mask_modes")
            if (
                safety.get("experimental_mask_enabled") is not False
                or not isinstance(approved_modes, list)
                or config.get("mode") in approved_modes
            ):
                errors.append(
                    "v2 sealed rejection is not bound to an unpromoted "
                    "masked mode"
                )
        matrix = gate.get("single_tpc_matrix_after_explicit_promotion")
        if not isinstance(matrix, Mapping):
            errors.append("Gate-A v2 masked matrix is malformed")
        else:
            for field, expected in (
                ("iterations", matrix.get("iterations")),
                ("blocks", matrix.get("blocks")),
                ("threads_per_block", matrix.get("threads_per_block")),
            ):
                actual = config.get(field)
                if (
                    isinstance(actual, bool)
                    or not isinstance(actual, int)
                    or actual <= 0
                    or not _exact_scalar(actual, expected)
                ):
                    errors.append(
                        f"sealed rejection config.{field} differs from matrix"
                    )
            if config.get("mode") not in matrix.get("modes", []):
                errors.append("sealed rejection mode is outside matrix")
            if config.get("enabled_tpc") not in matrix.get("tpc_bits", []):
                errors.append("sealed rejection TPC is outside matrix")
            trials = matrix.get("trials_per_cell")
            trial = config.get("trial")
            if (
                isinstance(trials, bool)
                or not isinstance(trials, int)
                or isinstance(trial, bool)
                or not isinstance(trial, int)
                or not 0 <= trial < trials
            ):
                errors.append("sealed rejection trial is outside matrix")

    outcome: dict[str, Any] | None = None
    try:
        outcome = _strict_snapshot_json_object(
            evidence,
            "outcome.json",
            allowed_keys=_V2_OUTCOME_KEYS,
            required_keys=frozenset(
                {
                    "schema_version",
                    "completed_at_utc",
                    "exit_code",
                    "process_exit_code",
                    "timed_out",
                    "native_output_found",
                    "native_status",
                    "driver_policy",
                    "driver_policy_permitted",
                    "manifest_policy",
                    "manifest_policy_permitted",
                    "safety_policy",
                    "preflight_permitted",
                    "formal_source_binding",
                    "formal_source_checks",
                    "formal_source_required_checks",
                    "formal_source_preflight_permitted",
                    "source_eligible_for_local_pass",
                    "local_probe_passed",
                    "accepted",
                    "quarantine_required",
                }
            ),
        )
    except ValueError as error:
        errors.append(f"invalid outcome.json: {error}")
    if outcome is not None:
        _check_exact_scalars(
            outcome,
            {
                "schema_version": OUTCOME_SCHEMA_VERSION_V2,
                "exit_code": 4,
                "process_exit_code": None,
                "timed_out": False,
                "native_output_found": False,
                "native_status": None,
                "driver_policy_permitted": True,
                "manifest_policy_permitted": False,
                "preflight_permitted": False,
                "formal_source_preflight_permitted": True,
                "source_eligible_for_local_pass": True,
                "local_probe_passed": False,
                "accepted": False,
                "quarantine_required": False,
            },
            field="outcome",
            errors=errors,
        )
        if not _is_canonical_utc_microsecond(
            outcome.get("completed_at_utc")
        ):
            errors.append("outcome.completed_at_utc is not canonical UTC")
        for field in (
            "driver_policy",
            "safety_policy",
            "formal_source_checks",
            "formal_source_required_checks",
        ):
            _all_true_checks(
                outcome.get(field),
                field=f"outcome.{field}",
                errors=errors,
            )
        driver_policy = outcome.get("driver_policy")
        if (
            not isinstance(driver_policy, Mapping)
            or set(driver_policy) != set(_DRIVER_POLICY_KEYS)
        ):
            errors.append("outcome.driver_policy keys are not exact")
        policy = outcome.get("manifest_policy")
        if not isinstance(policy, Mapping):
            errors.append("outcome.manifest_policy must be an object")
        elif not policy:
            errors.append("outcome.manifest_policy must be a non-empty object")
        else:
            if any(not isinstance(value, bool) for value in policy.values()):
                errors.append(
                    "outcome.manifest_policy values must all be booleans"
                )
            false_checks = {
                str(name)
                for name, passed in policy.items()
                if passed is not True
            }
            # A closed promotion manifest legitimately fails every masked
            # authorization prerequisite at once — no approved mode, no
            # reserved GPU, no reservation lease, no pinned Xid monitor, and
            # for stream mode no promoted offset — so the exact false set is
            # not fixed and differs per mode.  What must hold is that the run
            # was stopped by the promotion lock itself and by nothing else:
            # every false check belongs to the authorization family, and the
            # two promotion checks are among them.
            non_authorization = sorted(
                name
                for name in false_checks
                if not _is_sealed_rejection_authorization_check(name)
            )
            if non_authorization:
                errors.append(
                    "sealed rejection has non-authorization false checks: "
                    f"{non_authorization}"
                )
            missing_lock = sorted(
                {"masked_experiment_promoted", "masked_mode_approved"}
                - false_checks
            )
            if missing_lock:
                errors.append(
                    "sealed rejection was not stopped by the promotion lock: "
                    f"{missing_lock}"
                )

    native_binary = environment.get("native_binary")
    native_build = environment.get("native_build")
    attestation = environment.get("native_build_attestation")
    attestation_identity = (
        attestation.get("identity")
        if isinstance(attestation, Mapping)
        else None
    )
    formal_identity = {
        "source_revision": expected_source_revision,
        "gate_manifest_sha256": gate_record.get("sha256"),
        "launcher_sha256": (
            native_binary.get("sha256")
            if isinstance(native_binary, Mapping)
            else None
        ),
        "real_probe_sha256": source.get("approved_real_probe_sha256"),
        "build_stamp_sha256": (
            native_build.get("sha256")
            if isinstance(native_build, Mapping)
            else None
        ),
        "build_attestation_sha256": (
            attestation_identity.get("sha256")
            if isinstance(attestation_identity, Mapping)
            else None
        ),
    }
    for name, digest in formal_identity.items():
        if name != "source_revision" and not _is_sha256(digest):
            errors.append(f"formal identity {name} is malformed")
    for label, actual, expected in (
        (
            "launcher",
            formal_identity["launcher_sha256"],
            source.get("approved_launcher_sha256"),
        ),
        (
            "build stamp",
            formal_identity["build_stamp_sha256"],
            source.get("approved_build_stamp_sha256"),
        ),
        (
            "build attestation",
            formal_identity["build_attestation_sha256"],
            source.get("approved_build_attestation_sha256"),
        ),
    ):
        if actual != expected:
            errors.append(f"{label} does not match Gate-A manifest pin")

    command: dict[str, Any] | None = None
    launcher: Mapping[str, Any] = {}
    try:
        command = _strict_snapshot_json_object(
            evidence,
            "command.json",
            allowed_keys=_V2_COMMAND_KEYS,
            required_keys=frozenset({"argv", "launcher_fd"}),
        )
    except ValueError as error:
        errors.append(f"invalid command.json: {error}")
    if command is not None:
        argv = command.get("argv")
        for field, allowed_keys in _COMMAND_NESTED_KEYS.items():
            nested = command.get(field)
            if (
                nested is not None
                and (
                    not isinstance(nested, Mapping)
                    or set(nested) != set(allowed_keys)
                )
            ):
                errors.append(f"command.{field} keys are not exact")
        native_path = (
            native_binary.get("path")
            if isinstance(native_binary, Mapping)
            else None
        )
        expected_argv = [
            native_path,
            "--mode",
            config.get("mode"),
            "--enabled-tpc",
            str(config.get("enabled_tpc")),
            "--iterations",
            str(config.get("iterations")),
            "--blocks",
            str(config.get("blocks")),
        ]
        # The strongest sealed rejection is one the operator explicitly opted
        # into on an unpinned driver, because then only the checked-in
        # promotion manifest can be what refused it.  The producer appends
        # this flag exactly when the config declares it, so the expected argv
        # must mirror that instead of forbidding the stronger evidence.
        if config.get("experimental_allow_unsupported_driver") is True:
            expected_argv.append("--allow-unsupported-driver")
        if argv != expected_argv:
            errors.append("sealed rejection command argv is not exact")
        launcher_value = command.get("launcher_fd")
        launcher = (
            launcher_value if isinstance(launcher_value, Mapping) else {}
        )
        expected_keys = {
            "fd",
            "path",
            "device",
            "inode",
            "mode",
            "size",
            "mtime_ns",
            "sha256",
            "execution_path",
            "passed_explicitly",
        }
        if not isinstance(launcher_value, Mapping) or set(launcher) != expected_keys:
            errors.append("sealed rejection launcher FD evidence is invalid")
        else:
            fd_value = launcher.get("fd")
            if (
                isinstance(fd_value, bool)
                or not isinstance(fd_value, int)
                or fd_value < 3
                or launcher.get("execution_path")
                != f"/proc/self/fd/{fd_value}"
            ):
                errors.append("sealed rejection launcher FD path is invalid")
            if launcher.get("passed_explicitly") is not True:
                errors.append(
                    "sealed rejection launcher FD was not explicitly passed"
                )
            mode_value = launcher.get("mode")
            if (
                isinstance(mode_value, bool)
                or not isinstance(mode_value, int)
                or not stat.S_ISREG(mode_value)
            ):
                errors.append(
                    "sealed rejection launcher FD mode is not regular"
                )
            if (
                launcher.get("path") != native_path
                or launcher.get("sha256")
                != formal_identity["launcher_sha256"]
            ):
                errors.append(
                    "sealed rejection launcher FD differs from formal identity"
                )

    identity_fields = (
        "path",
        "device",
        "inode",
        "mode",
        "size",
        "mtime_ns",
        "sha256",
    )
    command_identity = {
        field: launcher.get(field) for field in identity_fields
    }
    environment_binding = environment.get("formal_source_binding")
    outcome_binding = (
        outcome.get("formal_source_binding")
        if isinstance(outcome, Mapping)
        else None
    )
    for label, identity in (
        (
            "manifest native_binary opened FD",
            (
                native_binary.get("opened_fd_identity")
                if isinstance(native_binary, Mapping)
                else None
            ),
        ),
        (
            "manifest formal launcher",
            (
                environment_binding.get("launcher_fd_identity")
                if isinstance(environment_binding, Mapping)
                else None
            ),
        ),
        (
            "outcome formal launcher",
            (
                outcome_binding.get("launcher_fd_identity")
                if isinstance(outcome_binding, Mapping)
                else None
            ),
        ),
    ):
        if not isinstance(identity, Mapping):
            errors.append(f"{label} identity is missing")
        elif set(identity) != set(identity_fields):
            errors.append(f"{label} identity keys are not exact")
        elif not _exact_json_equal(
            {field: identity.get(field) for field in identity_fields},
            command_identity,
        ):
            errors.append(f"{label} identity differs from command launcher")

    try:
        stdout = _snapshot_file(evidence, "stdout.log").content
        stderr = _strict_snapshot_text(evidence, "stderr.log")
    except ValueError as error:
        errors.append(f"invalid sealed rejection logs: {error}")
    else:
        if stdout:
            errors.append("sealed rejection stdout.log must be empty")
        if not stderr.strip() or "reject" not in stderr.lower():
            errors.append(
                "sealed rejection stderr.log must contain rejection text"
            )

    events, event_errors = _strict_snapshot_events(evidence)
    errors.extend(event_errors)
    _validate_event_identity(
        events,
        expected_run_id=run_directory.name,
        errors=errors,
    )
    event_types = [event.event_type for event in events]
    if event_types != ["run.preflight", "run.rejected"]:
        errors.append(
            "v2 sealed rejection events are not exactly "
            "run.preflight,run.rejected"
        )
    if "run.started" in event_types:
        errors.append("sealed rejection contains run.started event")
    if (
        events
        and outcome is not None
        and canonical_json(events[-1].payload) != canonical_json(outcome)
    ):
        errors.append(
            "terminal run.rejected payload does not match outcome.json"
        )

    return {
        "run_id": run_directory.name,
        "runner_contract_schema": CELL_SCHEMA_VERSION_V2,
        "formal_identity": formal_identity,
        "file_sha256": hashes,
        "file_identity": evidence.identities,
        "directory_identity": dict(evidence.directory_identity),
        "valid": not errors,
        "validation_errors": errors,
    }


def _sealed_rejection_validator(
    run_directory: Path,
    *,
    evidence_snapshot: _EvidenceDirectorySnapshot | None = None,
):
    evidence = (
        evidence_snapshot
        if evidence_snapshot is not None
        else _snapshot_evidence_directory(run_directory)
    )
    try:
        _manifest, schema = _classify_manifest_snapshot(evidence)
    except ValueError:
        return _validate_sealed_rejection_v2, evidence
    if schema == CELL_SCHEMA_VERSION_V2:
        return _validate_sealed_rejection_v2, evidence
    return _validate_sealed_rejection, evidence


_V2_MASKED_MONITOR_KEYS = frozenset(
    {"status", "post_probe_drain_timeout_ms", "provenance"}
)


def validate_masked_cell_contract(
    *,
    config: Mapping[str, Any],
    outcome: Mapping[str, Any],
    native: Mapping[str, Any],
    gate_content: Mapping[str, Any],
    command: Mapping[str, Any],
    expected_gpu: int,
    expected_uuid: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Check the masked-specific half of one cell and extract its observation.

    A masked cell can never be an accepted Gate result on its own: the runner
    computes ``accepted`` as ``local_probe_passed and mode == "baseline"``, so
    the strongest verdict a masked run can carry is ``local_probe_passed``.
    This function enforces exactly that, checks the parts of the contract that
    only exist for masked modes, and returns the observation in the shape
    :func:`validate_masked_tpc_matrix` consumes, so a cell can only reach the
    matrix through the per-cell checks.

    The shared launcher, libcuda, source-revalidation and board-identity
    checks are not repeated here; callers run them for every v2 cell.
    """

    errors: list[str] = []
    matrix = gate_content.get("single_tpc_matrix_after_explicit_promotion")
    safety = gate_content.get("safety")
    hardware = gate_content.get("hardware")
    if not all(
        isinstance(value, Mapping) for value in (matrix, safety, hardware)
    ):
        return None, ["Gate-A manifest masked sections are malformed"]

    mode = config.get("mode")
    bit = config.get("enabled_tpc")
    trial = config.get("trial")

    # A masked cell only exists if the checked-in manifest actually promoted
    # this mode.  Without that it is a sealed rejection, not a cell.
    if safety.get("experimental_mask_enabled") is not True:
        errors.append(
            "masked cell requires an explicitly promoted Gate-A manifest"
        )
    approved = safety.get("approved_mask_modes")
    if not isinstance(approved, list) or mode not in approved:
        errors.append(f"masked mode {mode!r} is not an approved mask mode")
    if mode not in MASKED_MODES:
        errors.append(f"mode {mode!r} is not a masked mode")

    bits = matrix.get("tpc_bits")
    if not isinstance(bits, list) or not any(
        _exact_scalar(bit, candidate) for candidate in bits
    ):
        errors.append(f"enabled_tpc {bit!r} is outside the declared matrix")
    trials = matrix.get("trials_per_cell")
    if (
        isinstance(trial, bool)
        or not isinstance(trial, int)
        or isinstance(trials, bool)
        or not isinstance(trials, int)
        or not 0 <= trial < trials
    ):
        errors.append(f"trial {trial!r} is outside the declared matrix")
    for field in ("iterations", "blocks", "threads_per_block"):
        if not _exact_scalar(config.get(field), matrix.get(field)):
            errors.append(f"config.{field} does not match the masked matrix")

    # The frozen rule: a masked run is never an accepted Gate result.
    _check_exact_scalars(
        outcome,
        {
            "accepted": False,
            "local_probe_passed": True,
            "requires_matrix_validation": True,
            "masked_health_monitor_status": "clean",
            "quarantine_required": False,
            "quarantine_reasons": [],
            "native_status": "ok",
            "timed_out": False,
        },
        field="outcome",
        errors=errors,
    )
    monitor = outcome.get("masked_health_monitor")
    if not isinstance(monitor, Mapping) or set(monitor) != set(
        _V2_MASKED_MONITOR_KEYS
    ):
        errors.append("outcome.masked_health_monitor keys are not exact")
    elif monitor.get("status") != "clean":
        errors.append("masked health monitor did not report a clean drain")
    _all_true_checks(
        outcome.get("masked_health_monitor_checks"),
        field="outcome.masked_health_monitor_checks",
        errors=errors,
    )

    # Masked execution must hold a reservation for the whole run horizon.
    preflight = outcome.get("final_launch_preflight")
    if not isinstance(preflight, Mapping):
        errors.append("outcome.final_launch_preflight must be an object")
    else:
        horizon = preflight.get("required_horizon_s")
        if (
            type(horizon) is not float
            or not math.isfinite(horizon)
            or horizon <= 0.0
        ):
            errors.append(
                "masked final preflight must reserve a positive horizon"
            )
        captured = preflight.get("captured_at_utc")
        until = preflight.get("required_until_utc")
        if not _is_canonical_utc_microsecond(
            captured
        ) or not _is_canonical_utc_microsecond(until):
            errors.append("masked final preflight timestamps are not canonical")
        elif not until > captured:
            errors.append(
                "masked final preflight required_until must follow captured_at"
            )
        if preflight.get("passed") is not True:
            errors.append("masked final preflight did not pass")
    commit = outcome.get("launch_commit_reservation_revalidation")
    if not isinstance(commit, Mapping):
        errors.append("masked launch-commit reservation must be an object")
    else:
        if commit.get("required_for_mode") is not True:
            errors.append(
                "masked launch-commit must declare a required reservation"
            )
        if commit.get("passed") is not True or commit.get("error") is not None:
            errors.append("masked launch-commit reservation did not pass")
        _all_true_checks(
            commit.get("checks"),
            field="outcome.launch_commit_reservation_revalidation.checks",
            errors=errors,
        )
    post = outcome.get("post_health")
    reservation = (
        post.get("reservation_revalidation")
        if isinstance(post, Mapping)
        else None
    )
    if not isinstance(reservation, Mapping):
        errors.append("masked post-health reservation must be an object")
    else:
        if reservation.get("required_for_mode") is not True:
            errors.append(
                "masked post-health reservation must be required for the mode"
            )
        if reservation.get("passed") is not True:
            errors.append("masked post-health reservation did not pass")

    # The parent-death guard is mandatory for masked execution.
    guard = native.get("parent_guard")
    if not isinstance(guard, Mapping):
        errors.append("native parent_guard must be an object")
    else:
        _check_exact_scalars(
            guard,
            {
                "mode": "linux_pdeathsig_sigkill",
                "status": "armed",
                "inherited_pdeath_signal": 9,
                "pdeath_signal": 9,
            },
            field="native.parent_guard",
            errors=errors,
        )
        expected_pid = guard.get("expected_parent_pid")
        observed_pid = guard.get("observed_parent_pid")
        if (
            isinstance(expected_pid, bool)
            or not isinstance(expected_pid, int)
            or expected_pid <= 0
            or not _exact_scalar(observed_pid, expected_pid)
        ):
            errors.append("native parent_guard PIDs are not a matching pair")

    # Native must report the mask that was requested.
    if not _exact_scalar(native.get("requested_enabled_tpc"), bit):
        errors.append("native requested_enabled_tpc differs from the config")
    if not _exact_scalar(
        native.get("tpc_count"), hardware.get("expected_tpc_count")
    ):
        errors.append("native tpc_count differs from the declared die")

    argv = command.get("argv")
    if not isinstance(argv, list) or "--enabled-tpc" not in argv:
        errors.append("masked command argv does not request a TPC bit")
    elif argv[argv.index("--enabled-tpc") + 1 :][:1] != [str(bit)]:
        errors.append("masked command argv requests a different TPC bit")
    environment = command.get("environment_overrides")
    if not isinstance(environment, Mapping):
        errors.append("masked command.environment_overrides must be an object")
    else:
        parent = environment.get("BURSTSERVE_PARENT_PID")
        if not isinstance(parent, str) or not parent.isdigit():
            errors.append(
                "masked child environment must carry the expected parent PID"
            )
        if ("MASK_OFF" in environment) != (mode == "stream"):
            errors.append(
                "MASK_OFF must be present for stream mode and absent otherwise"
            )

    histogram = native.get("observed_histogram")
    observation: dict[str, Any] | None = None
    if not isinstance(histogram, Mapping) or not histogram:
        errors.append("native observed_histogram is missing or empty")
    else:
        try:
            sms = sorted(int(key, 10) for key in histogram)
            observed_blocks = sum(int(value) for value in histogram.values())
        except (TypeError, ValueError):
            errors.append("native observed_histogram is malformed")
        else:
            observation = {
                "mode": mode,
                "tpc_bit": bit,
                "trial": trial,
                "physical_gpu": expected_gpu,
                "gpu_uuid": expected_uuid,
                "blocks": config.get("blocks"),
                "observed_blocks": observed_blocks,
                "observed_sms": sms,
            }
    if errors:
        return None, errors
    return observation, errors


_MASKED_OBSERVATION_KEYS = frozenset(
    {
        "mode",
        "tpc_bit",
        "trial",
        "physical_gpu",
        "gpu_uuid",
        "blocks",
        "observed_blocks",
        "observed_sms",
    }
)
_MASKED_MATRIX_KEYS = frozenset(
    {
        "modes",
        "tpc_bits",
        "trials_per_cell",
        "allowed_observed_sm_count",
        "iterations",
        "blocks",
        "threads_per_block",
    }
)


def validate_masked_tpc_matrix(
    observations: Sequence[Mapping[str, Any]],
    *,
    matrix: Mapping[str, Any],
    hardware: Mapping[str, Any],
    baseline_observed_sm_count: int | None = None,
    baseline_observed_sms: Sequence[int] | None = None,
    baseline_gpu_uuid: str | None = None,
) -> dict[str, Any]:
    """Decide whether a masked matrix establishes a real TPC->SM mapping.

    A single masked histogram proves nothing: the mapping it appears to show
    is derived from that same observation, so it cannot be contradicted.  The
    Gate-A claim is that a requested TPC bit confines the kernel to the SM set
    that bit denotes, and the evidence for it is cross-sectional rather than
    per run:

    * the same bit must yield the identical SM set on every trial, otherwise
      the mask is not deterministic;
    * the same bit must yield the identical SM set under every masking
      mechanism, because a TPC-to-SM mapping is a property of the die and not
      of the mechanism that programs it;
    * distinct bits must yield disjoint SM sets, which is the only available
      evidence that the bit selects rather than merely permits;
    * each set must hold exactly the die's SMs-per-TPC, and every SM must lie
      inside the device's range;
    * and the sets must be far smaller than the unmasked baseline's coverage
      on the same card, which is what shows the mask restricts at all.

    This function is deliberately pure and takes already validated per-cell
    observations, so it can be reviewed and tested long before any masked
    kernel is authorized to run.
    """

    errors: list[str] = []
    checks: dict[str, bool] = {}

    if not isinstance(matrix, Mapping) or set(matrix) != set(
        _MASKED_MATRIX_KEYS
    ):
        errors.append("masked matrix section keys are not exact")
        return {
            "checks": {"matrix_section_keys_exact": False},
            "tpc_sm_mapping": {},
            "errors": errors,
            "accepted": False,
        }
    checks["matrix_section_keys_exact"] = True

    modes = matrix.get("modes")
    bits = matrix.get("tpc_bits")
    trials = matrix.get("trials_per_cell")
    allowed_counts = matrix.get("allowed_observed_sm_count")
    expected_blocks = matrix.get("blocks")
    sm_count = hardware.get("sm_count")
    tpc_count = hardware.get("expected_tpc_count")

    def positive_int(value: Any) -> bool:
        return (
            not isinstance(value, bool)
            and isinstance(value, int)
            and value > 0
        )

    # Every cross-sectional check this function makes degenerates at a
    # cardinality of one: with one mode there is no mechanism to agree with,
    # with one bit the pairwise disjointness loop never executes, and with one
    # trial determinism is trivially satisfied. Such a matrix is not weak
    # evidence, it is a mapping confirmed only by the observation that
    # produced it, so refuse the shape outright.
    shape_ok = (
        isinstance(modes, list)
        and len(modes) >= 2
        and len(set(modes)) == len(modes)
        and all(isinstance(m, str) and m for m in modes)
        and isinstance(bits, list)
        and len(bits) >= 2
        and all(positive_int(b) or b == 0 for b in bits)
        and len(set(bits)) == len(bits)
        and positive_int(trials)
        and trials >= 2
        and isinstance(allowed_counts, list)
        and allowed_counts
        and all(positive_int(c) for c in allowed_counts)
        and positive_int(expected_blocks)
        and positive_int(sm_count)
        and positive_int(tpc_count)
    )
    checks["matrix_declaration_well_formed"] = shape_ok
    if not shape_ok:
        errors.append("masked matrix declaration is malformed")
        return {
            "checks": checks,
            "tpc_sm_mapping": {},
            "errors": errors,
            "accepted": False,
        }

    if sm_count % tpc_count:
        checks["die_sms_per_tpc_is_integral"] = False
        errors.append(
            f"sm_count {sm_count} is not a multiple of tpc_count {tpc_count}"
        )
        sms_per_tpc = None
    else:
        checks["die_sms_per_tpc_is_integral"] = True
        sms_per_tpc = sm_count // tpc_count

    # --- shape of the supplied observations -----------------------------
    parsed: dict[tuple[str, int, int], frozenset[int]] = {}
    identities: set[tuple[int, str]] = set()
    keys_exact = True
    for index, item in enumerate(observations):
        if not isinstance(item, Mapping) or set(item) != set(
            _MASKED_OBSERVATION_KEYS
        ):
            errors.append(f"masked observation[{index}] keys are not exact")
            keys_exact = False
            continue
        mode = item["mode"]
        bit = item["tpc_bit"]
        trial = item["trial"]
        sms = item["observed_sms"]
        blocks = item["blocks"]
        observed_blocks = item["observed_blocks"]
        gpu = item["physical_gpu"]
        uuid = item["gpu_uuid"]
        if (
            not isinstance(mode, str)
            or isinstance(bit, bool)
            or not isinstance(bit, int)
            or isinstance(trial, bool)
            or not isinstance(trial, int)
            or isinstance(gpu, bool)
            or not isinstance(gpu, int)
            or not isinstance(uuid, str)
            or not uuid
            or not isinstance(sms, (list, tuple, set, frozenset))
            or not _exact_scalar(blocks, expected_blocks)
            or not _exact_scalar(observed_blocks, expected_blocks)
        ):
            errors.append(f"masked observation[{index}] fields are invalid")
            keys_exact = False
            continue
        members = list(sms)
        if any(
            isinstance(s, bool) or not isinstance(s, int) for s in members
        ):
            errors.append(f"masked observation[{index}] SM ids are not integers")
            keys_exact = False
            continue
        if len(set(members)) != len(members):
            errors.append(f"masked observation[{index}] repeats an SM id")
            keys_exact = False
            continue
        cell = (mode, bit, trial)
        if cell in parsed:
            errors.append(
                f"masked matrix has a duplicate cell: mode={mode} "
                f"bit={bit} trial={trial}"
            )
            keys_exact = False
            continue
        parsed[cell] = frozenset(members)
        identities.add((gpu, uuid))
    checks["observation_records_well_formed"] = keys_exact

    checks["single_gpu_identity"] = len(identities) == 1
    if len(identities) != 1:
        errors.append(
            "masked matrix must describe exactly one GPU identity, "
            f"observed {len(identities)}"
        )

    # --- completeness ----------------------------------------------------
    expected_cells = {
        (mode, bit, trial)
        for mode in modes
        for bit in bits
        for trial in range(trials)
    }
    missing = sorted(expected_cells - set(parsed))
    extra = sorted(set(parsed) - expected_cells)
    checks["matrix_complete"] = not missing
    checks["no_unexpected_cells"] = not extra
    if missing:
        errors.append(f"masked matrix is missing cells: {missing[:8]}")
    if extra:
        errors.append(f"masked matrix has undeclared cells: {extra[:8]}")

    # --- per observation --------------------------------------------------
    counts_ok = True
    range_ok = True
    for (mode, bit, trial), sms in sorted(parsed.items()):
        if len(sms) not in set(allowed_counts):
            counts_ok = False
            errors.append(
                f"mode={mode} bit={bit} trial={trial} observed "
                f"{len(sms)} SMs, allowed {sorted(set(allowed_counts))}"
            )
        if any(s < 0 or s >= sm_count for s in sms):
            range_ok = False
            errors.append(
                f"mode={mode} bit={bit} trial={trial} observed an SM id "
                f"outside 0..{sm_count - 1}"
            )
    checks["observed_sm_count_within_allowed"] = counts_ok
    checks["observed_sms_within_device_range"] = range_ok

    # --- determinism across trials ---------------------------------------
    per_mode_bit: dict[tuple[str, int], frozenset[int]] = {}
    deterministic = True
    for mode in modes:
        for bit in bits:
            sets = {
                parsed[(mode, bit, trial)]
                for trial in range(trials)
                if (mode, bit, trial) in parsed
            }
            if not sets:
                continue
            if len(sets) != 1:
                deterministic = False
                errors.append(
                    f"mode={mode} bit={bit} is not deterministic across "
                    f"trials: {[sorted(s) for s in sorted(sets, key=sorted)]}"
                )
                continue
            per_mode_bit[(mode, bit)] = next(iter(sets))
    checks["deterministic_across_trials"] = deterministic

    # --- agreement across masking mechanisms ------------------------------
    # With a single declared mode there is nothing for the mapping to agree
    # with, and the loop below would report agreement for every bit because
    # each bit yields exactly one set. That is not weak evidence, it is no
    # evidence: the mapping would be confirmed only by the observation it was
    # derived from. Refuse the degenerate shape rather than pass it silently.
    mapping: dict[int, frozenset[int]] = {}
    consistent = len(set(modes)) >= 2
    if not consistent:
        errors.append(
            "masked matrix declares fewer than two masking mechanisms, so "
            "cross-mode agreement cannot be evidence of anything"
        )
    for bit in bits:
        sets = {
            per_mode_bit[(mode, bit)]
            for mode in modes
            if (mode, bit) in per_mode_bit
        }
        if not sets:
            continue
        if len(sets) != 1:
            consistent = False
            errors.append(
                f"bit={bit} maps to different SM sets under different "
                f"modes: {[sorted(s) for s in sorted(sets, key=sorted)]}"
            )
            continue
        mapping[bit] = next(iter(sets))
    checks["consistent_across_modes"] = consistent

    # --- disjointness between bits ----------------------------------------
    disjoint = True
    ordered = sorted(mapping)
    for left_index, left in enumerate(ordered):
        for right in ordered[left_index + 1 :]:
            shared = mapping[left] & mapping[right]
            if shared:
                disjoint = False
                errors.append(
                    f"bits {left} and {right} share SMs {sorted(shared)}"
                )
    checks["disjoint_across_bits"] = disjoint

    # --- mapping matches the die ------------------------------------------
    if sms_per_tpc is None:
        checks["mapping_matches_die_sms_per_tpc"] = False
    else:
        exact = all(len(sms) == sms_per_tpc for sms in mapping.values())
        checks["mapping_matches_die_sms_per_tpc"] = bool(mapping) and exact
        if mapping and not exact:
            errors.append(
                f"every TPC should hold {sms_per_tpc} SMs on this die: "
                + ", ".join(
                    f"bit {bit}->{len(sms)}"
                    for bit, sms in sorted(mapping.items())
                    if len(sms) != sms_per_tpc
                )
            )

    # --- the mask must actually restrict ----------------------------------
    # A cardinality comparison alone is nearly vacuous: two masked SMs are
    # "fewer than" a baseline of three, and a caller-supplied integer is not
    # tied to any observed card. Demand the baseline's actual SM set, from the
    # same GPU, and require containment -- a mask that confines to SMs the
    # unmasked run never touched has not restricted anything, it has moved.
    baseline_sms = (
        frozenset(baseline_observed_sms)
        if isinstance(baseline_observed_sms, (list, tuple, set, frozenset))
        and all(
            not isinstance(value, bool) and isinstance(value, int)
            for value in baseline_observed_sms
        )
        else None
    )
    baseline_uuid_matches = (
        isinstance(baseline_gpu_uuid, str)
        and bool(baseline_gpu_uuid)
        and len(identities) == 1
        and baseline_gpu_uuid == next(iter(identities))[1]
    )
    checks["baseline_is_from_the_same_gpu"] = baseline_uuid_matches
    if not baseline_uuid_matches:
        errors.append(
            "unmasked baseline must be identified by the same GPU UUID as "
            "the masked observations"
        )
    if baseline_sms is None or not baseline_sms:
        checks["restricts_relative_to_unmasked_baseline"] = False
        errors.append(
            "no unmasked baseline SM set was supplied for comparison"
        )
    elif baseline_observed_sm_count is not None and not _exact_scalar(
        baseline_observed_sm_count, len(baseline_sms)
    ):
        checks["restricts_relative_to_unmasked_baseline"] = False
        errors.append(
            f"unmasked baseline count {baseline_observed_sm_count!r} "
            f"disagrees with its own SM set of {len(baseline_sms)}"
        )
    else:
        restricts = bool(mapping) and all(
            sms < baseline_sms for sms in mapping.values()
        )
        checks["restricts_relative_to_unmasked_baseline"] = restricts
        if not restricts:
            errors.append(
                "masked observations are not a proper subset of the "
                f"unmasked baseline's {len(baseline_sms)} SMs"
            )

    return {
        "checks": checks,
        "tpc_sm_mapping": {
            str(bit): sorted(sms) for bit, sms in sorted(mapping.items())
        },
        "declared_matrix": {
            "modes": list(modes),
            "tpc_bits": list(bits),
            "trials_per_cell": trials,
            "expected_cell_count": len(expected_cells),
            "observed_cell_count": len(parsed),
        },
        "errors": errors,
        "accepted": not errors and all(checks.values()),
    }


def _excluded_record(
    run_root: Path,
    *,
    run_id: str,
    reason: str,
) -> dict[str, Any]:
    directory = run_root / run_id
    exists = directory.is_dir()
    if not exists:
        return {
            "run_id": run_id,
            "reason": reason,
            "exists": False,
            "file_sha256": {},
            "valid": False,
            "validation_errors": ["excluded run directory is missing"],
            "_snapshot_bytes": 0,
        }
    try:
        evidence = _snapshot_evidence_directory(directory)
        errors = list(evidence.errors)
        hashes = evidence.hashes
        snapshot_bytes = sum(
            len(snapshot.content)
            for snapshot in evidence.files.values()
            if isinstance(snapshot, _EvidenceFileSnapshot)
        )
    except (OSError, ValueError) as error:
        errors = [
            "excluded run snapshot failed: "
            f"{type(error).__name__}: {error}"
        ]
        hashes = {}
        snapshot_bytes = 0
    return {
        "run_id": run_id,
        "reason": reason,
        "exists": True,
        "file_sha256": hashes,
        "valid": not errors,
        "validation_errors": errors,
        "_snapshot_bytes": snapshot_bytes,
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
    detached = json.loads(canonical_json(evidence_spec))
    spec = _normalize_evidence_spec_value(detached)
    source_revision = str(spec["source_revision"])
    seed = int(spec["seed"])
    declared = spec["declared_gpus"]
    selected = spec["selected_gpu_indices"]
    trials = spec["required_trials"]
    excluded_values = spec["excluded_runs"]
    sealed_ids = spec["sealed_rejection_run_ids"]

    gpu_by_index = {
        int(item["physical_gpu"]): str(item["gpu_uuid"])
        for item in declared
    }
    selected_indices = [int(value) for value in selected]
    required_trials = [int(value) for value in trials]
    excluded_ids = {
        str(item["run_id"])
        for item in excluded_values
    }

    candidates: dict[tuple[int, int], list[Path]] = {
        (gpu, trial): []
        for gpu in gpu_by_index
        for trial in required_trials
    }
    candidate_snapshots: dict[Path, _EvidenceDirectorySnapshot] = {}
    candidate_schemas: dict[Path, Any] = {}
    candidate_snapshot_bytes = 0
    scanned_snapshot_count = 0
    scanned_snapshot_bytes = 0
    discovered = 0
    run_directories: list[Path] = []
    run_root_entries = 0
    for path in run_root.iterdir():
        run_root_entries += 1
        if run_root_entries > _MAX_RUN_ROOT_ENTRIES:
            raise ValueError(
                "run-root entry count exceeds "
                f"{_MAX_RUN_ROOT_ENTRIES}"
            )
        if not path.is_dir() or path.name.startswith("."):
            continue
        run_directories.append(path)
        if len(run_directories) > _MAX_DISCOVERED_RUN_DIRECTORIES:
            raise ValueError(
                "discovered run directory count exceeds "
                f"{_MAX_DISCOVERED_RUN_DIRECTORIES}"
            )
    for run_directory in sorted(run_directories, key=lambda path: path.name):
        discovered += 1
        if run_directory.name in excluded_ids:
            continue
        try:
            evidence = _snapshot_evidence_directory(run_directory)
            scanned_snapshot_count += 1
            if scanned_snapshot_count > _MAX_SCANNED_SNAPSHOTS:
                raise ValueError(
                    "scanned evidence snapshot count exceeds "
                    f"{_MAX_SCANNED_SNAPSHOTS}"
                )
            snapshot_bytes = sum(
                len(snapshot.content)
                for snapshot in evidence.files.values()
                if isinstance(snapshot, _EvidenceFileSnapshot)
            )
            scanned_snapshot_bytes += snapshot_bytes
            if (
                scanned_snapshot_bytes
                > _MAX_SCANNED_SNAPSHOT_BYTES
            ):
                raise ValueError(
                    "scanned evidence snapshot bytes exceed "
                    f"{_MAX_SCANNED_SNAPSHOT_BYTES}"
                )
            manifest_value, candidate_schema = (
                _classify_manifest_snapshot(evidence)
            )
        except OSError:
            continue
        except ValueError as error:
            if str(error).startswith("scanned evidence snapshot"):
                raise
            continue
        cell = _candidate_cell(
            manifest_value,
            source_revision=source_revision,
            seed=seed,
            declared_indices=set(gpu_by_index),
            required_trials=set(required_trials),
        )
        if cell is not None:
            if (
                len(candidate_snapshots) + 1
                > _MAX_CANDIDATE_SNAPSHOTS
            ):
                raise ValueError(
                    "candidate evidence count exceeds "
                    f"{_MAX_CANDIDATE_SNAPSHOTS}"
                )
            if (
                candidate_snapshot_bytes + snapshot_bytes
                > _MAX_CANDIDATE_SNAPSHOT_BYTES
            ):
                raise ValueError(
                    "candidate evidence snapshot bytes exceed "
                    f"{_MAX_CANDIDATE_SNAPSHOT_BYTES}"
                )
            candidate_snapshot_bytes += snapshot_bytes
            candidates[cell].append(run_directory)
            candidate_snapshots[run_directory] = evidence
            candidate_schemas[run_directory] = candidate_schema

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
            run_reports: list[dict[str, Any]] = []
            for path in paths:
                candidate_schema = candidate_schemas.get(path)
                evidence = candidate_snapshots[path]
                validator = (
                    _validate_baseline_run_v2
                    if candidate_schema == CELL_SCHEMA_VERSION_V2
                    else _validate_baseline_run
                )
                try:
                    report = validator(
                        path,
                        expected_source_revision=source_revision,
                        expected_seed=seed,
                        expected_gpu=gpu,
                        expected_trial=trial,
                        expected_uuid=gpu_by_index[gpu],
                        evidence_snapshot=evidence,
                    )
                except Exception as error:
                    # A malformed or adversarial v2 record is evidence of an
                    # invalid cell, never a reason to abort the aggregate and
                    # hide the remainder of the matrix.
                    report = {
                        "run_id": path.name,
                        "cell": {
                            "physical_gpu": gpu,
                            "trial": trial,
                            "gpu_uuid": gpu_by_index[gpu],
                        },
                        "runner_contract_schema": candidate_schema,
                        "formal_identity": {},
                        "file_sha256": evidence.hashes,
                        "file_identity": evidence.identities,
                        "directory_identity": dict(
                            evidence.directory_identity
                        ),
                        "binary_sha256": None,
                        "build_sha256": None,
                        "metrics": {},
                        "valid": False,
                        "validation_errors": [
                            "validator rejected malformed evidence: "
                            f"{type(error).__name__}: {error}"
                        ],
                    }
                if candidate_schema not in {
                    CELL_SCHEMA_VERSION,
                    CELL_SCHEMA_VERSION_V2,
                }:
                    report["valid"] = False
                    report["validation_errors"].append(
                        "unsupported runner contract schema"
                    )
                run_reports.append(report)
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

    auxiliary_snapshot_bytes = 0
    excluded_reports: list[dict[str, Any]] = []
    for item in excluded_values:
        record = _excluded_record(
            run_root,
            run_id=str(item["run_id"]),
            reason=str(item["reason"]),
        )
        auxiliary_snapshot_bytes += int(record.pop("_snapshot_bytes", 0))
        if auxiliary_snapshot_bytes > _MAX_AUXILIARY_SNAPSHOT_BYTES:
            raise ValueError(
                "auxiliary evidence snapshot bytes exceed "
                f"{_MAX_AUXILIARY_SNAPSHOT_BYTES}"
            )
        excluded_reports.append(record)
    excluded_reports.sort(key=lambda item: item["run_id"])
    rejection_reports: list[dict[str, Any]] = []
    for run_id in sealed_ids:
        directory = run_root / str(run_id)
        if not directory.is_dir():
            rejection_reports.append(
                {
                    "run_id": str(run_id),
                    "file_sha256": {},
                    "valid": False,
                    "validation_errors": [
                        "sealed rejection run directory is missing"
                    ],
                }
            )
            continue
        evidence: _EvidenceDirectorySnapshot | None = None
        try:
            evidence = _snapshot_evidence_directory(directory)
            auxiliary_snapshot_bytes += sum(
                len(snapshot.content)
                for snapshot in evidence.files.values()
                if isinstance(snapshot, _EvidenceFileSnapshot)
            )
            if (
                auxiliary_snapshot_bytes
                > _MAX_AUXILIARY_SNAPSHOT_BYTES
            ):
                raise ValueError(
                    "auxiliary evidence snapshot bytes exceed "
                    f"{_MAX_AUXILIARY_SNAPSHOT_BYTES}"
                )
            validator, evidence = _sealed_rejection_validator(
                directory,
                evidence_snapshot=evidence,
            )
            rejection_reports.append(
                validator(
                    directory,
                    expected_source_revision=source_revision,
                    evidence_snapshot=evidence,
                )
            )
        except Exception as error:
            if str(error).startswith(
                "auxiliary evidence snapshot bytes exceed"
            ):
                raise
            evidence_hashes = (
                evidence.hashes
                if isinstance(evidence, _EvidenceDirectorySnapshot)
                else {}
            )
            rejection_reports.append(
                {
                    "run_id": str(run_id),
                    "file_sha256": evidence_hashes,
                    "valid": False,
                    "validation_errors": [
                        "sealed rejection validator rejected malformed "
                        f"evidence: {type(error).__name__}: {error}"
                    ],
                }
            )
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
    selected_all_runs = [
        run
        for report in selected_cell_reports
        for run in report["runs"]
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
    selected_contract_schemas = sorted(
        {
            str(run["runner_contract_schema"])
            for run in selected_all_runs
            if run.get("runner_contract_schema")
        }
    )
    selected_formal_identities = sorted(
        {
            canonical_json(run["formal_identity"])
            for run in selected_valid_runs
            if isinstance(run.get("formal_identity"), Mapping)
        }
    )
    selected_consistent = (
        len(selected_binary_hashes) == 1
        and len(selected_build_hashes) == 1
        and len(selected_contract_schemas) == 1
        and len(selected_formal_identities) == 1
    )
    exclusions_valid = all(item["valid"] for item in excluded_reports)
    rejections_valid = all(item["valid"] for item in rejection_reports)
    selected_is_v2 = (
        selected_contract_schemas == [CELL_SCHEMA_VERSION_V2]
    )
    spec_is_v2 = (
        spec.get("schema_version") == EVIDENCE_SPEC_SCHEMA_VERSION_V2
    )
    expected_contract_schema = (
        CELL_SCHEMA_VERSION_V2 if spec_is_v2 else CELL_SCHEMA_VERSION
    )
    evidence_spec_schema_compatible = (
        selected_contract_schemas == [expected_contract_schema]
    )
    # Sealed rejections carry the same runner contract identity as cells.  A
    # v2 rejection mixed into a v1 spec/matrix (or the reverse) is a schema
    # downgrade, not a stronger record, so the pairing is enforced in both
    # directions and the rejection schema also participates in report-version
    # selection below.
    rejection_contract_schemas = sorted(
        {
            str(item["runner_contract_schema"])
            for item in rejection_reports
            if item.get("runner_contract_schema")
        }
    )
    rejection_schema_compatible = rejection_contract_schemas in (
        [],
        [expected_contract_schema],
    )
    if not rejection_schema_compatible:
        for rejection in rejection_reports:
            if (
                rejection.get("runner_contract_schema")
                != expected_contract_schema
            ):
                rejection["valid"] = False
                rejection.setdefault("validation_errors", []).append(
                    "sealed rejection runner contract schema does not pair "
                    "with the evidence spec and baseline matrix"
                )
        rejections_valid = all(item["valid"] for item in rejection_reports)
    if selected_is_v2:
        if not rejection_reports:
            rejections_valid = False
        expected_identity = (
            selected_formal_identities[0]
            if len(selected_formal_identities) == 1
            else None
        )
        for rejection in rejection_reports:
            mismatch = (
                rejection.get("runner_contract_schema")
                != CELL_SCHEMA_VERSION_V2
                or expected_identity is None
                or not isinstance(
                    rejection.get("formal_identity"),
                    Mapping,
                )
                or canonical_json(rejection["formal_identity"])
                != expected_identity
            )
            if mismatch:
                rejection["valid"] = False
                rejection.setdefault("validation_errors", []).append(
                    "v2 sealed rejection formal identity differs from "
                    "the selected v2 matrix"
                )
        rejections_valid = (
            bool(rejection_reports)
            and all(item["valid"] for item in rejection_reports)
        )
    selected_accepted = (
        all(cell_status[cell] == "valid" for cell in selected_cells)
        and selected_consistent
        and exclusions_valid
        and rejections_valid
        and evidence_spec_schema_compatible
        and rejection_schema_compatible
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
    all_candidate_runs = [
        run for report in cell_reports for run in report["runs"]
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
    all_contract_schemas = sorted(
        {
            str(run["runner_contract_schema"])
            for run in all_candidate_runs
            if run.get("runner_contract_schema")
        }
    )
    all_formal_identities = sorted(
        {
            canonical_json(run["formal_identity"])
            for run in all_valid_runs
            if isinstance(run.get("formal_identity"), Mapping)
        }
    )
    full_consistent = (
        len(all_binary_hashes) == 1
        and len(all_build_hashes) == 1
        and len(all_contract_schemas) == 1
        and len(all_formal_identities) == 1
    )
    all_contract_schemas_compatible = (
        all_contract_schemas == [expected_contract_schema]
    )
    # The required fleet size is a property of the evidence programme, not of
    # a single global constant: the published v1 report was produced under the
    # eight-card scope and must keep reporting it.  Declaring more than the
    # minimum stays legal, and every declared GPU still has to be complete, so
    # this is a floor rather than an exact shape.
    required_gpu_count = (
        REQUIRED_GATE_A0_GPU_COUNT_V2
        if spec_is_v2
        else REQUIRED_GATE_A0_GPU_COUNT
    )
    v2_matrix_shape_valid = (
        all_contract_schemas != [CELL_SCHEMA_VERSION_V2]
        or (
            len(gpu_by_index) >= required_gpu_count
            and required_trials == [0, 1, 2]
        )
    )
    gate_complete = (
        len(gpu_by_index) >= required_gpu_count
        and not missing_declared_gpus
        and full_consistent
        and all_contract_schemas_compatible
        and v2_matrix_shape_valid
        and evidence_spec_schema_compatible
        and rejection_schema_compatible
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

    report_schema_version = (
        REPORT_SCHEMA_VERSION_V2
        if (
            spec_is_v2
            or CELL_SCHEMA_VERSION_V2
            in set(
                all_contract_schemas
                + selected_contract_schemas
                + rejection_contract_schemas
            )
        )
        else REPORT_SCHEMA_VERSION
    )
    if report_schema_version == REPORT_SCHEMA_VERSION:
        # Preserve the already-published v1 report byte contract.  The
        # stronger runner-schema/formal-identity fields participate in the
        # internal downgrade checks above, but they belong only to report v2.
        for cell_report in cell_reports:
            for run_report in cell_report["runs"]:
                run_report.pop("runner_contract_schema", None)
                run_report.pop("formal_identity", None)
                run_report.pop("file_identity", None)
                run_report.pop("directory_identity", None)
        for rejection_report in rejection_reports:
            rejection_report.pop("runner_contract_schema", None)
            rejection_report.pop("formal_identity", None)
            rejection_report.pop("file_identity", None)
            rejection_report.pop("directory_identity", None)
    return {
        "schema_version": report_schema_version,
        "evidence_spec": {
            "schema_version": spec.get("schema_version"),
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
            **(
                {
                    "runner_contract_schemas": selected_contract_schemas,
                    "formal_identity_values": selected_formal_identities,
                }
                if report_schema_version == REPORT_SCHEMA_VERSION_V2
                else {}
            ),
            "binary_and_build_consistent": selected_consistent,
            **(
                {
                    "evidence_spec_schema_compatible": (
                        evidence_spec_schema_compatible
                    )
                }
                if report_schema_version == REPORT_SCHEMA_VERSION_V2
                else {}
            ),
            "accepted": selected_accepted,
        },
        "gate_a0": {
            "required_gpu_count": required_gpu_count,
            "declared_gpu_count": len(gpu_by_index),
            "complete_declared_gpus": complete_declared_gpus,
            "missing_declared_gpus": missing_declared_gpus,
            "binary_sha256_values": all_binary_hashes,
            "build_sha256_values": all_build_hashes,
            **(
                {
                    "runner_contract_schemas": all_contract_schemas,
                    "formal_identity_values": all_formal_identities,
                    "evidence_spec_schema_compatible": (
                        all_contract_schemas_compatible
                    ),
                    "v2_matrix_shape_valid": v2_matrix_shape_valid,
                }
                if report_schema_version == REPORT_SCHEMA_VERSION_V2
                else {}
            ),
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
            **(
                {
                    "runner_contract_schemas": rejection_contract_schemas,
                    "evidence_spec_schema_compatible": (
                        rejection_schema_compatible
                    ),
                }
                if report_schema_version == REPORT_SCHEMA_VERSION_V2
                else {}
            ),
            "runs": rejection_reports,
        },
        "aggregate_inputs": aggregate_input,
        "aggregate_input_sha256": _sha256_bytes(
            canonical_json(aggregate_input).encode("utf-8")
        ),
    }


def aggregate_from_spec(run_root: Path, evidence_spec_path: Path) -> dict[str, Any]:
    spec, raw_spec_sha = _load_evidence_spec_snapshot(evidence_spec_path)
    spec_sha = (
        raw_spec_sha
        if spec.get("schema_version") == EVIDENCE_SPEC_SCHEMA_VERSION_V2
        else _sha256_bytes(canonical_json(spec).encode("utf-8"))
    )
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
