from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

import burstserve.gate_a_results as gate_a_results
from burstserve.gate_a_results import (
    EVIDENCE_SPEC_SCHEMA_VERSION,
    EVIDENCE_SPEC_SCHEMA_VERSION_V2,
    aggregate_from_spec,
    main,
    validate_masked_tpc_matrix,
    validate_masked_cell_contract,
)
from burstserve.provenance import (
    EventRecord,
    RunManifest,
    append_jsonl_atomic,
    canonical_json,
    write_json_atomic,
)
from burstserve.smctrl_runner import (
    FORMAL_LAUNCHER_THREAT_BOUNDARIES,
    evaluate_probe,
)


_SOURCE = "burstserve-test;libsmctrl-test"
_BINARY_SHA = "a" * 64
_BUILD_CONTENT = "CUDA_ARCH=89\n"
_BUILD_SHA = hashlib.sha256(_BUILD_CONTENT.encode("utf-8")).hexdigest()
_GATE_CONTENT = {
    "schema_version": "burstserve.gate-a-manifest/v1",
    "hardware": {"sm_count": 128},
    "source": {"revision": "synthetic"},
    "safety": {"timeout_s": 30.0},
    "baseline": {
        "blocks_per_sm": 32,
        "iterations": 4096,
        "minimum_sm_coverage_fraction": 0.75,
        "trials_per_gpu": 3,
    },
}
_GATE_SHA = hashlib.sha256(
    canonical_json(_GATE_CONTENT).encode("utf-8")
).hexdigest()
_REAL_SHA = "b" * 64
_ATTESTATION_SHA = "c" * 64


def _synthetic_libcuda_identity() -> dict[str, object]:
    return {
        "link_path": "/usr/lib/x86_64-linux-gnu/libcuda.so.1",
        "link_target": "libcuda.so.synthetic",
        "link_identity": {
            "device": 1,
            "inode": 44,
            "mode": stat.S_IFLNK | 0o777,
            "uid": 0,
            "gid": 0,
            "nlink": 1,
            "size": 20,
            "mtime_ns": 1,
            "ctime_ns": 2,
        },
        "resolved_path": (
            "/usr/lib/x86_64-linux-gnu/libcuda.so.synthetic"
        ),
        "target_identity": {
            "device": 1,
            "inode": 45,
            "mode": stat.S_IFREG | 0o755,
            "uid": 0,
            "gid": 0,
            "nlink": 1,
            "size": 100,
            "mtime_ns": 3,
            "ctime_ns": 4,
            "sha256": "9" * 64,
        },
        "trusted_directories": [],
    }
_GATE_V2_CONTENT = {
    "schema_version": "burstserve.gate-a-manifest/v2",
    "hardware": {
        "gpu_name": "Synthetic GPU",
        "physical_gpu_indices": list(range(8)),
        "compute_capability": [8, 9],
        "sm_count": 128,
        "expected_tpc_count": 64,
        "driver_api_version": 13030,
        "runtime_api_version": 13000,
    },
    "source": {
        "libsmctrl_commit": "synthetic",
        "approved_launcher_sha256": _BINARY_SHA,
        "approved_real_probe_sha256": _REAL_SHA,
        "approved_build_stamp_sha256": _BUILD_SHA,
        "approved_build_attestation_sha256": _ATTESTATION_SHA,
    },
    "safety": {
        "timeout_s": 30.0,
        "maximum_preexisting_gpu_memory_mib": 1024,
        "experimental_mask_enabled": False,
        "approved_mask_modes": [],
        "reserved_gpu_uuids": [
            f"GPU-00000000-0000-0000-0000-{index:012d}"
            for index in range(8)
        ],
        "exclusive_reservation_evidence": {
            "schema_version": "burstserve.gpu-reservation/v1",
            "status": "active",
            "gpu_uuid": "GPU-00000000-0000-0000-0000-000000000000",
            "physical_gpu": 0,
            "reservation_id": "synthetic",
            "owner": "unit-test",
            "valid_from_utc": "2026-01-01T00:00:00Z",
            "valid_until_utc": "2099-01-01T00:00:00Z",
        },
        "xid_monitoring": {
            "available": True,
            "method": "nvmlEventSetWait_v2_exact_xid",
            "quiet_ms": 1000,
            "library_path": "/synthetic/libnvidia-ml.so.1",
            "library_sha256": "e" * 64,
            "library_version": "synthetic",
        },
        "stream_offset_search_enabled": False,
        "stream_mask_off_candidates": [],
        "global_next_matrix_accepted": False,
        "mps_allowed": False,
        "mps_bypass": "CUDA_MPS_PIPE_DIRECTORY_empty",
    },
    "baseline": {
        "blocks_per_sm": 32,
        "iterations": 4096,
        "threads_per_block": 256,
        "minimum_sm_coverage_fraction": 0.75,
        "trials_per_gpu": 3,
    },
    "single_tpc_matrix_after_explicit_promotion": {
        "modes": ["global", "next", "stream"],
        "tpc_bits": [0, 31, 32, 63],
        "trials_per_cell": 3,
        "allowed_observed_sm_count": [1, 2],
        "iterations": 4096,
        "blocks": 4096,
        "threads_per_block": 256,
    },
}
_GATE_V2_SHA = hashlib.sha256(
    canonical_json(_GATE_V2_CONTENT).encode("utf-8")
).hexdigest()


def _gpu_uuid(index: int) -> str:
    return f"GPU-00000000-0000-0000-0000-{index:012d}"


def _gpu_record(index: int) -> dict[str, object]:
    """The complete trusted nvidia-smi record the runner actually stores."""

    return {
        "index": index,
        "name": "Synthetic GPU",
        "uuid": _gpu_uuid(index),
        "pci_bus_id": f"00000000:{index + 1:02X}:00.0",
        "memory_total_mib": 24564,
        "memory_used_mib": 2,
        "utilization_gpu_percent": 0,
        "driver_version": "580.65.06",
    }


def _gpu_hardware_identity(index: int) -> dict[str, object]:
    """Mirrors query_gpu_hardware_identity for a board in this box."""

    gigabyte = index in {0, 1, 3}
    return {
        "vbios_version": (
            "95.02.18.C0.8B" if gigabyte else "95.02.3C.40.40"
        ),
        "subsystem_vendor_id": "0x1458" if gigabyte else "0x10de",
        "subsystem_device_id": "0x40de" if gigabyte else "0x16f3",
        "numa_node": (3, 2, 1, 0, 7, 6, 5, 4)[index],
        "power_limit_w": 450.0,
        "power_default_limit_w": 450.0,
        "power_max_limit_w": 479.0 if gigabyte else 450.0,
        "max_sm_clock_mhz": 3105,
        "max_memory_clock_mhz": 10501,
        "max_pcie_link_gen": 4,
        "max_pcie_link_width": 16,
    }


def _mps_processes() -> list[dict[str, object]]:
    """Recorded host MPS daemons; isolation is the empty pipe directory."""

    return [
        {
            "pid": 4242,
            "command": "nvidia-cuda-mps",
            "arguments": "nvidia-cuda-mps-control -d",
        }
    ]


_CLOSED_MANIFEST_FALSE_CHECKS = (
    # Measured from evaluate_gate_manifest_policy against the checked-in
    # closed experiments/manifests/gate_a_4090.json: a closed manifest fails
    # every masked authorization prerequisite at once.
    "masked_experiment_promoted",
    "masked_mode_approved",
    "masked_gpu_is_reserved",
    "masked_gpu_has_current_reservation_evidence",
    "masked_reservation_covers_required_horizon",
    "masked_reservation_gpu_uuid_exact",
    "masked_reservation_identity_recorded",
    "masked_reservation_interval_ordered",
    "masked_reservation_not_expired",
    "masked_reservation_physical_gpu_exact",
    "masked_reservation_schema_exact",
    "masked_reservation_started",
    "masked_reservation_status_active",
    "masked_reservation_valid_from_parseable",
    "masked_reservation_valid_until_parseable",
    "masked_xid_library_hash_is_pinned",
    "masked_xid_library_path_is_absolute",
    "masked_xid_library_version_is_pinned",
    "masked_xid_monitoring_is_available",
    "masked_xid_monitoring_method_is_exact",
)
_CLOSED_MANIFEST_STREAM_FALSE_CHECKS = (
    # Stream mode additionally fails the CUDA-stream offset gates, which are
    # authorization gates without the "masked_" prefix.
    "stream_offset_is_8byte_aligned",
    "stream_offset_is_declared",
    "stream_offset_search_promoted",
    "stream_prerequisites_accepted",
)


def _closed_manifest_policy(mode: str) -> dict[str, object]:
    """The false-check set the checked-in closed manifest actually produces."""

    policy: dict[str, object] = {
        "manifest_schema_v2_exact": True,
        "driver_api_matches_manifest": True,
        "gpu_name_matches_manifest": True,
        "physical_gpu_is_declared": True,
        "masked_threads_are_native_canonical": True,
    }
    for name in _CLOSED_MANIFEST_FALSE_CHECKS:
        policy[name] = False
    if mode == "stream":
        for name in _CLOSED_MANIFEST_STREAM_FALSE_CHECKS:
            policy[name] = False
    return policy


def _child_environment(uuid: str) -> dict[str, str]:
    """The exact ``env -i`` allowlist ``build_child_environment`` emits."""

    return {
        "LANG": "C",
        "LC_ALL": "C",
        "TZ": "UTC",
        "CUDA_CACHE_DISABLE": "1",
        "CUDA_VISIBLE_DEVICES": uuid,
        "CUDA_MPS_PIPE_DIRECTORY": "",
    }


def _mps_bypass() -> dict[str, str]:
    return {
        "CUDA_MPS_PIPE_DIRECTORY": "",
        "basis": "NVIDIA-documented empty-pipe-directory bypass",
    }


def _libcuda_binding_checks() -> dict[str, bool]:
    return {
        "runtime_libcuda_build_stamp_fields_present": True,
        "runtime_libcuda_resolved_path_matches_build_stamp": True,
        "runtime_libcuda_sha256_matches_build_stamp": True,
        "runtime_libcuda_link_path_is_fixed": True,
        "runtime_libcuda_target_is_root_owned_regular": True,
    }


def _final_preflight_checks() -> dict[str, bool]:
    return {
        "health_queries_completed": True,
        "gpu_accessible": True,
        "gpu_ordinal_exact": True,
        "gpu_uuid_stable": True,
        "gpu_uuid_matches_held_lease": True,
        "memory_safe_or_explicit_busy_baseline": True,
        "compute_processes_absent_or_explicit_busy_baseline": True,
        "empty_mps_pipe_bypass_exact": True,
        "reservation_not_required_for_baseline": True,
        "reservation_valid_for_complete_run_horizon": True,
    }


def _post_health_checks() -> dict[str, bool]:
    return {
        "health_queries_completed": True,
        "gpu_accessible_after_probe": True,
        "gpu_ordinal_exact_after_probe": True,
        "gpu_uuid_stable_after_probe": True,
        "memory_safe_after_probe": True,
        "compute_processes_absent_after_probe_or_baseline_override": True,
        "host_mps_state_recorded_after_probe": True,
        "process_group_reaped": True,
        "reservation_valid_at_gpu_safety_end": True,
    }


def _post_health(gpu: int) -> dict[str, object]:
    return {
        "gpu": _gpu_record(gpu),
        "compute_processes": [],
        "mps_processes": _mps_processes(),
        "error": None,
        "checks": _post_health_checks(),
        "reservation_revalidation": {
            "captured_at_utc": "2026-01-01T00:00:00.900000Z",
            "required_for_mode": False,
            "checks": {"reservation_not_required_for_baseline": True},
            "passed": True,
        },
    }


def _spec(
    *,
    selected: list[int] | None = None,
    trials: list[int] | None = None,
    excluded: list[dict[str, str]] | None = None,
    rejections: list[str] | None = None,
    schema_version: str = EVIDENCE_SPEC_SCHEMA_VERSION,
) -> dict[str, object]:
    return {
        "schema_version": schema_version,
        "evidence_id": "synthetic-gate-a0",
        "source_revision": _SOURCE,
        "seed": 1,
        "declared_gpus": [
            {"physical_gpu": index, "gpu_uuid": _gpu_uuid(index)}
            for index in range(8)
        ],
        "selected_gpu_indices": selected if selected is not None else [0],
        "required_trials": trials if trials is not None else [0],
        "excluded_runs": excluded if excluded is not None else [],
        "sealed_rejection_run_ids": rejections if rejections is not None else [],
    }


def _spec_v2(**kwargs) -> dict[str, object]:
    return _spec(
        **kwargs,
        schema_version=EVIDENCE_SPEC_SCHEMA_VERSION_V2,
    )


def _config(*, gpu: int, trial: int, mode: str = "baseline") -> dict[str, object]:
    return {
        "schema_version": "burstserve.smid-probe-cell/v1",
        "physical_gpu": gpu,
        "mode": mode,
        "enabled_tpc": 0,
        "iterations": 4096,
        "trial": trial,
        "seed": 1,
        "timeout_s": 30.0,
        "maximum_used_mib": 1024,
        "allow_busy_gpu": False,
        "experimental_allow_unsupported_driver": False,
        "experimental_mask_off": None,
        "gate_manifest": {
            "path": "experiments/manifests/synthetic.json",
            "git_blob": "c" * 40,
            "sha256": _GATE_SHA,
            "content": _GATE_CONTENT,
        },
    }


def _manifest(*, gpu: int, trial: int, mode: str = "baseline") -> RunManifest:
    uuid = _gpu_uuid(gpu)
    return RunManifest.create(
        config=_config(gpu=gpu, trial=trial, mode=mode),
        seed=1,
        source_revision=_SOURCE,
        environment={
            "selected_gpu_initial_preflight": {
                "index": gpu,
                "uuid": uuid,
            },
            "selected_gpu_launch_preflight": {
                "index": gpu,
                "uuid": uuid,
            },
            "native_binary": {
                "path": "/synthetic/smid_probe",
                "sha256": _BINARY_SHA,
            },
            "native_build": {
                "found": True,
                "path": "/synthetic/build-config.stamp",
                "sha256": _BUILD_SHA,
                "content": _BUILD_CONTENT,
            },
        },
        metadata={
            "purpose": "phase1-libsmctrl-gate-a",
            "runner": "synthetic",
        },
        created_at_utc="2026-01-01T00:00:00.000000Z",
    )


def _native(*, gpu: int, mode: str = "baseline") -> dict[str, object]:
    histogram = {str(index): 32 for index in range(128)}
    return {
        "schema_version": "burstserve.smid-probe-native/v1",
        "status": "ok",
        "mode": mode,
        "driver_version": 13030,
        "runtime_version": 13000,
        "device": {
            "ordinal": 0,
            "name": "Synthetic GPU",
            "uuid": _gpu_uuid(gpu),
            "cc_major": 8,
            "cc_minor": 9,
            "sm_count": 128,
        },
        "requested_enabled_tpc": None if mode == "baseline" else 0,
        "tpc_count": None if mode == "baseline" else 64,
        "blocks": 4096,
        "threads_per_block": 256,
        "iterations": 4096,
        "observed_histogram": histogram,
    }


def _all_true() -> dict[str, bool]:
    return {"synthetic_check": True}


def _driver_policy() -> dict[str, bool]:
    return {
        "driver_is_pinned_or_explicitly_allowed": True,
        "stream_unknown_driver_has_explicit_mask_off": True,
        "mask_off_only_used_for_stream": True,
        "mask_off_requires_experimental_allow": True,
    }


def _accepted_outcome(*, gpu: int) -> dict[str, object]:
    return {
        "schema_version": "burstserve.smid-probe-outcome/v1",
        "completed_at_utc": "2026-01-01T00:00:01.000000Z",
        "exit_code": 0,
        "process_exit_code": 0,
        "timed_out": False,
        "native_output_found": True,
        "native_output_error": None,
        "native_status": "ok",
        "driver_policy": _driver_policy(),
        "driver_policy_permitted": True,
        "manifest_policy": _all_true(),
        "manifest_policy_permitted": True,
        "safety_policy": _all_true(),
        "preflight_permitted": True,
        "post_health": {
            "gpu": {"uuid": _gpu_uuid(gpu)},
            "compute_processes": [],
            "mps_processes": [],
            "error": None,
            "checks": _all_true(),
        },
        "semantic_acceptance": _all_true(),
        "semantic_metrics": {
            "device_uuid": _gpu_uuid(gpu),
            "device_sm_count": 128,
            "observed_sm_count": 128,
            "sm_coverage_ratio": 1.0,
            "reported_blocks": 4096,
            "observed_blocks": 4096,
            "reported_iterations": 4096,
        },
        "local_probe_passed": True,
        "requires_matrix_validation": False,
        "accepted": True,
    }


def _command(*, gpu: int, mode: str = "baseline") -> dict[str, object]:
    argv = ["/synthetic/smid_probe", "--mode", mode]
    if mode != "baseline":
        argv.extend(["--enabled-tpc", "0"])
    argv.extend(["--iterations", "4096"])
    return {
        "argv": argv,
        "cwd": "/synthetic/repository",
        "started_at_utc": "2026-01-01T00:00:00.000000Z",
        "environment_overrides": {
            "CUDA_VISIBLE_DEVICES": _gpu_uuid(gpu),
            "CUDA_MPS_PIPE_DIRECTORY": "",
            "MASK_OFF": None,
            "removed_mps_variables_except_empty_pipe_bypass": True,
        },
    }


def _append_event(
    directory: Path,
    *,
    run_id: str,
    sequence: int,
    event_type: str,
    payload: dict[str, object],
) -> None:
    append_jsonl_atomic(
        directory / "events.jsonl",
        EventRecord.create(
            run_id=run_id,
            sequence=sequence,
            event_type=event_type,
            payload=payload,
            timestamp_utc=f"2026-01-01T00:00:0{sequence}.000000Z",
        ),
    )


def _write_baseline(
    run_root: Path,
    *,
    gpu: int,
    trial: int,
    extra_config: dict[str, object] | None = None,
) -> Path:
    manifest = _manifest(gpu=gpu, trial=trial)
    if extra_config:
        config = dict(manifest.config)
        config.update(extra_config)
        manifest = RunManifest.create(
            config=config,
            seed=manifest.seed,
            source_revision=manifest.source_revision,
            environment=manifest.environment,
            metadata=manifest.metadata,
            created_at_utc=manifest.created_at_utc,
        )
    directory = run_root / manifest.run_id
    directory.mkdir(parents=True)
    native = _native(gpu=gpu)
    outcome = _accepted_outcome(gpu=gpu)
    command = _command(gpu=gpu)
    write_json_atomic(directory / "manifest.json", manifest.to_dict())
    write_json_atomic(directory / "outcome.json", outcome)
    write_json_atomic(directory / "native.json", native)
    write_json_atomic(directory / "command.json", command)
    (directory / "stdout.log").write_text(
        canonical_json(native) + "\n",
        encoding="utf-8",
    )
    (directory / "stderr.log").write_text("", encoding="utf-8")
    _append_event(
        directory,
        run_id=manifest.run_id,
        sequence=0,
        event_type="run.preflight",
        payload={},
    )
    _append_event(
        directory,
        run_id=manifest.run_id,
        sequence=1,
        event_type="run.started",
        payload=command,
    )
    _append_event(
        directory,
        run_id=manifest.run_id,
        sequence=2,
        event_type="run.completed",
        payload=outcome,
    )
    return directory


def _write_baseline_v2(
    run_root: Path,
    *,
    gpu: int,
    trial: int,
) -> Path:
    uuid = _gpu_uuid(gpu)
    gate_record = {
        "path": "experiments/manifests/synthetic-v2.json",
        "git_blob": "d" * 40,
        "sha256": _GATE_V2_SHA,
        "content": _GATE_V2_CONTENT,
    }
    config = {
        "schema_version": "burstserve.smid-probe-cell/v2",
        "physical_gpu": gpu,
        "mode": "baseline",
        "enabled_tpc": 0,
        "iterations": 4096,
        "blocks": 4096,
        "threads_per_block": 256,
        "trial": trial,
        "seed": 1,
        "timeout_s": 30.0,
        "maximum_used_mib": 1024,
        "allow_busy_gpu": False,
        "experimental_allow_unsupported_driver": False,
        "experimental_mask_off": None,
        "gate_manifest": gate_record,
    }
    launcher_identity = {
        "path": "/synthetic/smid_probe",
        "device": 11,
        "inode": 22,
        "mode": stat.S_IFREG | 0o500,
        "size": 12345,
        "mtime_ns": 67890,
        "sha256": _BINARY_SHA,
    }
    formal_source_binding = {
        "launcher_fd_identity": launcher_identity,
        "build_stamp": {
            "fields": {
                "LIBCUDA_LINK_LIBRARY": (
                    _synthetic_libcuda_identity()["resolved_path"]
                ),
                "LIBCUDA_LINK_LIBRARY_SHA256": (
                    _synthetic_libcuda_identity()["target_identity"][
                        "sha256"
                    ]
                ),
            }
        },
    }
    cuda_driver_probe = {
        "version": 13030,
        "library_identity": _synthetic_libcuda_identity(),
        "load_path": "/proc/self/fd/88",
        "load_binding": "verified /proc/self/fd descriptor",
        "creates_cuda_context": False,
        "python_pre_main_threat_boundary": (
            FORMAL_LAUNCHER_THREAT_BOUNDARIES[
                "python_pre_main_injection"
            ]
        ),
    }
    environment = {
        "selected_gpu_initial_preflight": _gpu_record(gpu),
        "selected_gpu_launch_preflight": _gpu_record(gpu),
        "selected_gpu_hardware_identity": _gpu_hardware_identity(gpu),
        "selected_gpu_compute_processes_initial": [],
        "selected_gpu_compute_processes_launch": [],
        "host_mps_processes_initial": _mps_processes(),
        "host_mps_processes_launch": _mps_processes(),
        "mps_bypass": _mps_bypass(),
        "runtime_libcuda_build_binding_checks": _libcuda_binding_checks(),
        "native_binary": {
            "path": "/synthetic/smid_probe",
            "sha256": _BINARY_SHA,
            "opened_fd_identity": launcher_identity,
        },
        "native_build": {
            "found": True,
            "path": "/synthetic/build-config.stamp",
            "sha256": _BUILD_SHA,
            "content": _BUILD_CONTENT,
        },
        "native_build_attestation": {
            "identity": {
                "path": "/synthetic/build-attestation.json",
                "sha256": _ATTESTATION_SHA,
            },
            "content": {},
        },
        "formal_source_binding": formal_source_binding,
        "cuda_driver_probe": cuda_driver_probe,
        "formal_launcher_threat_boundaries": (
            FORMAL_LAUNCHER_THREAT_BOUNDARIES
        ),
    }
    manifest = RunManifest.create(
        config=config,
        seed=1,
        source_revision=_SOURCE,
        environment=environment,
        metadata={
            "purpose": "phase1-libsmctrl-gate-a",
            "runner": "synthetic-v2",
        },
        created_at_utc="2026-01-01T00:00:00.000000Z",
    )
    native = _native(gpu=gpu)
    native.update(
        {
            "schema_version": "burstserve.smid-probe-native/v2",
            "parent_guard": {
                "mode": "not_required",
                "status": "not_required",
                "expected_parent_pid": None,
                "observed_parent_pid": 123,
                "inherited_pdeath_signal": None,
                "pdeath_signal": None,
            },
        }
    )
    semantic_checks, semantic_metrics, semantic_accepted = evaluate_probe(
        native,
        expected_mode="baseline",
        expected_enabled_tpc=0,
        expected_driver_version=13030,
        expected_runtime_version=13000,
        expected_iterations=4096,
        process_exit_code=0,
        expected_device_uuid=uuid,
        expected_device_name="Synthetic GPU",
        expected_sm_count=128,
        expected_compute_capability=[8, 9],
        expected_blocks=4096,
        expected_threads_per_block=256,
        expected_device_ordinal=0,
        expected_tpc_count=None,
        stderr="",
        minimum_sm_coverage=0.75,
    )
    if not semantic_accepted:  # pragma: no cover - fixture self-check.
        raise AssertionError(semantic_checks)
    def revalidation(phase: str) -> dict[str, object]:
        return {
            "phase": phase,
            "completed": True,
            "error": None,
            "build_record": {"found": True},
            "binding": {
                "source_snapshot_sha256": "f" * 64,
            },
            "checks": _all_true(),
            "required_checks": _all_true(),
            "canonical_paths_selected": True,
            "source_eligible_for_local_pass": True,
            "formal_source_launch_permitted": True,
            "expected_snapshot_sha256": "f" * 64,
            "observed_snapshot_sha256": "f" * 64,
            "snapshot_matches_initial": True,
            "passed_for_launch": True,
            "passed_for_local_acceptance": True,
        }
    prelaunch_revalidation = revalidation("prelaunch")
    preexec_revalidation = revalidation(
        "preexec_after_monitor_and_signal_block"
    )
    postrun_revalidation = revalidation("postrun")
    libcuda_final = {
        "completed": True,
        "error": None,
        "expected_identity": _synthetic_libcuda_identity(),
        "observed_identity": _synthetic_libcuda_identity(),
        "matches_initial": True,
        "passed": True,
    }
    launcher_fd_final = {
        "captured_at_utc": "2026-01-01T00:00:00.500000Z",
        "completed": True,
        "error": None,
        "identity": launcher_identity,
        "checks": _all_true(),
        "passed": True,
        "same_uid_out_of_band_write_threat_boundary": (
            FORMAL_LAUNCHER_THREAT_BOUNDARIES[
                "same_uid_out_of_band_launcher_write"
            ]
        ),
    }
    final_launch_preflight = {
        "captured_at_utc": "2026-01-01T00:00:00.400000Z",
        "required_horizon_s": 0.0,
        "required_until_utc": "2026-01-01T00:00:00.400000Z",
        "gpu": _gpu_record(gpu),
        "compute_processes": [],
        "mps_processes": _mps_processes(),
        "errors": [],
        "checks": _final_preflight_checks(),
        "passed": True,
    }
    launch_commit_reservation_revalidation = {
        "captured_at_utc": "2026-01-01T00:00:00.450000Z",
        "required_for_mode": False,
        "required_horizon_s": 0.0,
        "required_until_utc": "2026-01-01T00:00:00.450000Z",
        "checks": {
            "reservation_not_required_for_baseline": True,
        },
        "passed": True,
        "error": None,
    }
    outcome = {
        "schema_version": "burstserve.smid-probe-outcome/v2",
        "completed_at_utc": "2026-01-01T00:00:01.000000Z",
        "exit_code": 0,
        "process_exit_code": 0,
        "raw_process_return_code": 0,
        "timed_out": False,
        "child_launch_error": None,
        "child_interruption": None,
        "process_group_reaped": True,
        "process_group_health": {
            "child_reaped": True,
            "process_group_quiesced": True,
            "process_group_reaped": True,
            "errors": [],
        },
        "gpu_lease": {
            "kind": "gpu_uuid",
            "gpu_uuid": uuid,
            "preexisting_quarantine": False,
        },
        "native_output_found": True,
        "native_output_error": None,
        "native_status": "ok",
        "driver_policy": _driver_policy(),
        "driver_policy_permitted": True,
        "manifest_policy": _all_true(),
        "manifest_policy_permitted": True,
        "safety_policy": _all_true(),
        "preflight_permitted": True,
        "formal_source_checks": _all_true(),
        "formal_source_required_checks": _all_true(),
        "formal_source_preflight_permitted": True,
        "source_eligible_for_local_pass": True,
        "formal_source_binding": formal_source_binding,
        "source_prelaunch_revalidation": prelaunch_revalidation,
        "source_preexec_revalidation": preexec_revalidation,
        "source_postrun_revalidation": postrun_revalidation,
        "post_health": _post_health(gpu),
        "final_launch_preflight": final_launch_preflight,
        "launch_commit_reservation_revalidation": (
            launch_commit_reservation_revalidation
        ),
        "libcuda_final_revalidation": libcuda_final,
        "launcher_fd_final": launcher_fd_final,
        "formal_launcher_threat_boundaries": (
            FORMAL_LAUNCHER_THREAT_BOUNDARIES
        ),
        "semantic_acceptance": semantic_checks,
        "semantic_metrics": semantic_metrics,
        "masked_health_monitor_status": "not_applicable",
        "masked_health_monitor": None,
        "masked_health_monitor_checks": {},
        "local_probe_passed": True,
        "requires_matrix_validation": False,
        "quarantine_required": False,
        "quarantine_reasons": [],
        "accepted": True,
    }
    child_environment = _child_environment(uuid)
    command = {
        "argv": [
            "/synthetic/smid_probe",
            "--mode",
            "baseline",
            "--iterations",
            "4096",
            "--blocks",
            "4096",
        ],
        "cwd": "/synthetic/repo",
        "prepared_at_utc": "2026-01-01T00:00:00.300000Z",
        "environment_overrides": child_environment,
        "environment_policy": {
            "mode": "env-i exact allowlist",
            "allowed_names": sorted(child_environment),
            "inherited_names": [],
        },
        "parent_death_protection": {
            "mechanism": "not_required",
            "expected_parent_pid": None,
            "runner_signal_handlers": ["SIGINT", "SIGHUP", "SIGTERM"],
            "residual": None,
        },
        "dynamic_loader_policy": {
            "mode": "env-i exact allowlist for every mode",
            "inherited_environment": False,
            "loader_and_cuda_tuning_variables_absent": True,
        },
        "signal_mask_policy": {
            "runner_blocks_during_popen": ["SIGINT", "SIGHUP", "SIGTERM"],
            "child_mask_reset_before_exec": True,
            "cleanup_policy": (
                "identity-bound SIGTERM/SIGKILL while waitid WNOWAIT "
                "retains the leader"
            ),
            "residual": None,
        },
        "launcher_fd": {
            "fd": 7,
            **launcher_identity,
            "execution_path": "/proc/self/fd/7",
            "passed_explicitly": True,
        },
        "launcher_fd_final": launcher_fd_final,
        "cuda_driver_probe": cuda_driver_probe,
        "libcuda_final_revalidation": libcuda_final,
        "formal_launcher_threat_boundaries": (
            FORMAL_LAUNCHER_THREAT_BOUNDARIES
        ),
    }
    directory = run_root / manifest.run_id
    directory.mkdir(parents=True)
    write_json_atomic(directory / "manifest.json", manifest.to_dict())
    write_json_atomic(directory / "outcome.json", outcome)
    write_json_atomic(directory / "native.json", native)
    write_json_atomic(directory / "command.json", command)
    (directory / "stdout.log").write_text(
        canonical_json(native) + "\n",
        encoding="utf-8",
    )
    (directory / "stderr.log").write_text("", encoding="utf-8")
    event_payloads = [
        (
            "run.preflight",
            {
                "gpu_initial": _gpu_record(gpu),
                "gpu_launch": _gpu_record(gpu),
                "gpu_hardware_identity": _gpu_hardware_identity(gpu),
                "compute_processes_initial": [],
                "compute_processes_launch": [],
                "mps_processes_initial": _mps_processes(),
                "mps_processes_launch": _mps_processes(),
                "driver_version": 13030,
                "cuda_driver_probe": cuda_driver_probe,
                "runtime_libcuda_build_binding_checks": (
                    _libcuda_binding_checks()
                ),
                "latest_pinned_driver_version": 13030,
                "driver_policy": _driver_policy(),
                "driver_policy_permitted": True,
                "manifest_policy": _all_true(),
                "manifest_policy_permitted": True,
                "safety_policy": _all_true(),
                "preflight_permitted": True,
                "formal_source_binding": formal_source_binding,
                "formal_source_checks": _all_true(),
                "formal_source_required_checks": _all_true(),
                "formal_source_preflight_permitted": True,
                "source_eligible_for_local_pass": True,
                "source_prelaunch_revalidation": None,
                "source_preexec_revalidation": None,
                "source_postrun_revalidation": None,
                "libcuda_final_revalidation": None,
                "launcher_fd_final": None,
                "formal_launcher_threat_boundaries": (
                    FORMAL_LAUNCHER_THREAT_BOUNDARIES
                ),
            },
        ),
        ("run.source_revalidated", prelaunch_revalidation),
        ("run.source_preexec_revalidated", preexec_revalidation),
        (
            "run.final_launch_preflight",
            final_launch_preflight,
        ),
        (
            "run.started",
            {
                "argv": command["argv"],
                "executed_argv0": "/proc/self/fd/7",
                "pid": 123,
                "started_at_utc": "2026-01-01T00:00:00Z",
                "launcher_fd_identity": launcher_identity,
                "launcher_fd_final": launcher_fd_final,
                "libcuda_final_revalidation": libcuda_final,
                "final_launch_preflight": outcome[
                    "final_launch_preflight"
                ],
                "launch_commit_reservation_revalidation": (
                    launch_commit_reservation_revalidation
                ),
            },
        ),
        ("run.source_postvalidated", postrun_revalidation),
        ("run.completed", outcome),
    ]
    for sequence, (event_type, payload) in enumerate(event_payloads):
        _append_event(
            directory,
            run_id=manifest.run_id,
            sequence=sequence,
            event_type=event_type,
            payload=payload,
        )
    return directory


def _write_rejection_v2(
    run_root: Path,
    *,
    gpu: int = 0,
    mode: str = "global",
    manifest_policy: dict[str, object] | None = None,
    experimental_allow_unsupported_driver: bool = False,
) -> Path:
    uuid = _gpu_uuid(gpu)
    gate_record = {
        "path": "experiments/manifests/synthetic-v2.json",
        "git_blob": "d" * 40,
        "sha256": _GATE_V2_SHA,
        "content": _GATE_V2_CONTENT,
    }
    config = {
        "schema_version": "burstserve.smid-probe-cell/v2",
        "physical_gpu": gpu,
        "mode": mode,
        "enabled_tpc": 0,
        "iterations": 4096,
        "blocks": 4096,
        "threads_per_block": 256,
        "trial": 0,
        "seed": 1,
        "timeout_s": 30.0,
        "maximum_used_mib": 1024,
        "allow_busy_gpu": False,
        "experimental_allow_unsupported_driver": (
            experimental_allow_unsupported_driver
        ),
        "experimental_mask_off": None,
        "gate_manifest": gate_record,
    }
    launcher_identity = {
        "path": "/synthetic/smid_probe",
        "device": 11,
        "inode": 22,
        "mode": stat.S_IFREG | 0o500,
        "size": 12345,
        "mtime_ns": 67890,
        "sha256": _BINARY_SHA,
    }
    formal_source_binding = {
        "launcher_fd_identity": launcher_identity,
    }
    environment = {
        "selected_gpu_initial_preflight": {
            "index": gpu,
            "uuid": uuid,
        },
        "selected_gpu_launch_preflight": {
            "index": gpu,
            "uuid": uuid,
        },
        "native_binary": {
            "path": "/synthetic/smid_probe",
            "sha256": _BINARY_SHA,
            "opened_fd_identity": launcher_identity,
        },
        "native_build": {
            "found": True,
            "path": "/synthetic/build-config.stamp",
            "sha256": _BUILD_SHA,
            "content": _BUILD_CONTENT,
        },
        "native_build_attestation": {
            "identity": {
                "path": "/synthetic/build-attestation.json",
                "sha256": _ATTESTATION_SHA,
            },
            "content": {},
        },
        "formal_source_binding": formal_source_binding,
    }
    manifest = RunManifest.create(
        config=config,
        seed=1,
        source_revision=_SOURCE,
        environment=environment,
        metadata={
            "purpose": "phase1-libsmctrl-gate-a",
            "runner": "synthetic-v2",
        },
        created_at_utc="2026-01-01T00:00:00.000000Z",
    )
    outcome = {
        "schema_version": "burstserve.smid-probe-outcome/v2",
        "completed_at_utc": "2026-01-01T00:00:00.000000Z",
        "exit_code": 4,
        "process_exit_code": None,
        "timed_out": False,
        "native_output_found": False,
        "native_status": None,
        "driver_policy": _driver_policy(),
        "driver_policy_permitted": True,
        # A closed promotion manifest fails every masked authorization
        # prerequisite at once, exactly as the checked-in Gate-A manifest does.
        "manifest_policy": (
            manifest_policy
            if manifest_policy is not None
            else _closed_manifest_policy(mode)
        ),
        "manifest_policy_permitted": False,
        "safety_policy": {"synthetic_safety_check": True},
        "preflight_permitted": False,
        "formal_source_binding": formal_source_binding,
        "formal_source_checks": {"synthetic_source_check": True},
        "formal_source_required_checks": {
            "synthetic_required_source_check": True,
        },
        "formal_source_preflight_permitted": True,
        "source_eligible_for_local_pass": True,
        "local_probe_passed": False,
        "quarantine_required": False,
        "accepted": False,
    }
    # The producer writes the same 13-key command record for a rejected run;
    # it is prepared before the promotion lock refuses to spawn the child.
    child_environment = {
        **_child_environment(uuid),
        "BURSTSERVE_PARENT_PID": "4242",
    }
    command = {
        "argv": [
            "/synthetic/smid_probe",
            "--mode",
            mode,
            "--enabled-tpc",
            "0",
            "--iterations",
            "4096",
            "--blocks",
            "4096",
            *(
                ["--allow-unsupported-driver"]
                if experimental_allow_unsupported_driver
                else []
            ),
        ],
        "cwd": "/synthetic/repo",
        "prepared_at_utc": "2026-01-01T00:00:00.300000Z",
        "environment_overrides": child_environment,
        "environment_policy": {
            "mode": "env-i exact allowlist",
            "allowed_names": sorted(child_environment),
            "inherited_names": [],
        },
        "parent_death_protection": {
            "mechanism": "native_linux_prctl_pdeathsig_sigkill",
            "expected_parent_pid": 4242,
            "runner_signal_handlers": ["SIGINT", "SIGHUP", "SIGTERM"],
            "residual": (
                "runner handlers and bounded process-group reap supplement "
                "the native PR_SET_PDEATHSIG guard"
            ),
        },
        "dynamic_loader_policy": {
            "mode": "env-i exact allowlist for every mode",
            "inherited_environment": False,
            "loader_and_cuda_tuning_variables_absent": True,
        },
        "signal_mask_policy": {
            "runner_blocks_during_popen": ["SIGINT", "SIGHUP", "SIGTERM"],
            "child_mask_reset_before_exec": True,
            "cleanup_policy": (
                "identity-bound SIGTERM/SIGKILL while waitid WNOWAIT "
                "retains the leader"
            ),
            "residual": None,
        },
        "launcher_fd": {
            "fd": 7,
            **launcher_identity,
            "execution_path": "/proc/self/fd/7",
            "passed_explicitly": True,
        },
        "launcher_fd_final": None,
        "cuda_driver_probe": None,
        "libcuda_final_revalidation": None,
        "formal_launcher_threat_boundaries": (
            FORMAL_LAUNCHER_THREAT_BOUNDARIES
        ),
    }
    directory = run_root / manifest.run_id
    directory.mkdir(parents=True)
    write_json_atomic(directory / "manifest.json", manifest.to_dict())
    write_json_atomic(directory / "outcome.json", outcome)
    write_json_atomic(directory / "command.json", command)
    (directory / "stdout.log").write_text("", encoding="utf-8")
    (directory / "stderr.log").write_text(
        "probe rejected by fail-closed preflight policy\n",
        encoding="utf-8",
    )
    _append_event(
        directory,
        run_id=manifest.run_id,
        sequence=0,
        event_type="run.preflight",
        payload={},
    )
    _append_event(
        directory,
        run_id=manifest.run_id,
        sequence=1,
        event_type="run.rejected",
        payload=outcome,
    )
    return directory


def _write_rejection(run_root: Path, *, gpu: int = 0) -> Path:
    manifest = _manifest(gpu=gpu, trial=0, mode="global")
    directory = run_root / manifest.run_id
    directory.mkdir(parents=True)
    outcome = {
        "schema_version": "burstserve.smid-probe-outcome/v1",
        "completed_at_utc": "2026-01-01T00:00:00.000000Z",
        "exit_code": 4,
        "process_exit_code": None,
        "timed_out": False,
        "native_output_found": False,
        "native_status": None,
        "preflight_permitted": False,
        "local_probe_passed": False,
        "accepted": False,
    }
    write_json_atomic(directory / "manifest.json", manifest.to_dict())
    write_json_atomic(directory / "outcome.json", outcome)
    write_json_atomic(directory / "command.json", _command(gpu=gpu, mode="global"))
    (directory / "stdout.log").write_text("", encoding="utf-8")
    (directory / "stderr.log").write_text(
        "probe rejected by fail-closed preflight policy\n",
        encoding="utf-8",
    )
    _append_event(
        directory,
        run_id=manifest.run_id,
        sequence=0,
        event_type="run.preflight",
        payload={},
    )
    _append_event(
        directory,
        run_id=manifest.run_id,
        sequence=1,
        event_type="run.rejected",
        payload=outcome,
    )
    return directory


class GateAResultsTest(unittest.TestCase):
    def test_historical_pretty_v1_spec_remains_reproducible(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        spec_path = (
            repo_root
            / "experiments/manifests/"
            "gate_a0_4090_dd8c927_seed1.json"
        )
        run_root = repo_root / "experiments/runs"
        published = (
            repo_root
            / "experiments/aggregates/"
            "gate_a0_4090_dd8c927_seed1_partial_20260730.json"
        )
        if not spec_path.is_file() or not run_root.is_dir():
            self.skipTest("historical Gate-A0 evidence is unavailable")
        # As in the byte-preservation test: experiments/runs is untracked, so
        # its presence says nothing about which runs are in it.
        if published.is_file():
            covered = {
                entry["run_id"]
                for entry in json.loads(published.read_text())
                ["aggregate_inputs"]["runs"]
            }
            absent = [r for r in covered if not (run_root / r).is_dir()]
            if absent:  # pragma: no cover - depends on retained raw evidence
                self.skipTest(
                    f"{len(absent)} of {len(covered)} historical runs are not "
                    f"in {run_root}"
                )
        report = aggregate_from_spec(run_root, spec_path)
        self.assertTrue(report["selected_subset"]["accepted"])
        self.assertEqual(
            report["selected_subset"]["expected_cell_count"],
            15,
        )
        self.assertEqual(
            report["selected_subset"]["valid_cell_count"],
            15,
        )
        self.assertEqual(len(report["cells"]), 24)
        self.assertFalse(report["gate_a0"]["complete"])
        self.assertEqual(
            report["evidence_spec"]["schema_version"],
            EVIDENCE_SPEC_SCHEMA_VERSION,
        )
        self.assertNotIn(
            "evidence_spec_schema_compatible",
            report["selected_subset"],
        )

    def test_full_eight_gpu_matrix_passes_and_cli_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / "runs"
            runs.mkdir()
            for gpu in range(8):
                _write_baseline(runs, gpu=gpu, trial=0)
            excluded = runs / "bs1-excluded"
            excluded.mkdir()
            (excluded / "manifest.json").write_text("{}", encoding="utf-8")
            rejection = _write_rejection(runs)
            spec = _spec(
                selected=[0, 1, 2],
                excluded=[
                    {
                        "run_id": excluded.name,
                        "reason": "synthetic exploratory run",
                    }
                ],
                rejections=[rejection.name],
            )
            spec_path = root / "spec.json"
            output_path = root / "report.json"
            write_json_atomic(spec_path, spec)

            first = aggregate_from_spec(runs, spec_path)
            second = aggregate_from_spec(runs, spec_path)
            self.assertEqual(first, second)
            self.assertTrue(first["selected_subset"]["accepted"])
            self.assertTrue(first["gate_a0"]["complete"])
            self.assertEqual(first["gate_a0"]["missing_declared_gpus"], [])
            self.assertEqual(
                len(first["aggregate_input_sha256"]),
                64,
            )
            self.assertTrue(first["excluded_runs"]["valid"])
            self.assertTrue(first["sealed_rejections"]["valid"])
            self.assertIsNotNone(
                first["cells"][0]["runs"][0]["file_sha256"]["native.json"]
            )
            self.assertEqual(
                first["cells"][0]["runs"][0]["metrics"],
                {
                    "observed_sm_count": 128,
                    "device_sm_count": 128,
                    "coverage": 1.0,
                    "reported_blocks": 4096,
                    "observed_blocks": 4096,
                    "native_blocks": 4096,
                },
            )

            self.assertEqual(
                main(
                    [
                        "--evidence-spec",
                        str(spec_path),
                        "--run-root",
                        str(runs),
                        "--output",
                        str(output_path),
                    ]
                ),
                0,
            )
            self.assertEqual(
                json.loads(output_path.read_text(encoding="utf-8")),
                first,
            )

    def test_selected_subset_can_pass_while_declared_gpus_are_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / "runs"
            runs.mkdir()
            _write_baseline(runs, gpu=2, trial=0)
            _write_baseline(runs, gpu=2, trial=1)
            spec_path = root / "spec.json"
            write_json_atomic(
                spec_path,
                _spec(selected=[2], trials=[0, 1]),
            )

            report = aggregate_from_spec(runs, spec_path)
            self.assertTrue(report["selected_subset"]["accepted"])
            self.assertFalse(report["gate_a0"]["complete"])
            self.assertEqual(
                [
                    item["physical_gpu"]
                    for item in report["gate_a0"]["missing_declared_gpus"]
                ],
                [0, 1, 3, 4, 5, 6, 7],
            )
            output_path = root / "partial-report.json"
            self.assertEqual(
                main(
                    [
                        "--evidence-spec",
                        str(spec_path),
                        "--run-root",
                        str(runs),
                        "--output",
                        str(output_path),
                    ]
                ),
                0,
            )
            self.assertEqual(
                main(
                    [
                        "--evidence-spec",
                        str(spec_path),
                        "--run-root",
                        str(runs),
                        "--output",
                        str(output_path),
                        "--require-full",
                    ]
                ),
                1,
            )
            self.assertTrue(output_path.is_file())

    def test_missing_duplicate_and_tampered_cells_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / "runs"
            runs.mkdir()
            spec_path = root / "spec.json"
            write_json_atomic(spec_path, _spec(selected=[0], trials=[0, 1]))

            valid = _write_baseline(runs, gpu=0, trial=0)
            duplicate = _write_baseline(
                runs,
                gpu=0,
                trial=0,
                extra_config={"duplicate_variant": 1},
            )
            outcome = json.loads(
                (duplicate / "outcome.json").read_text(encoding="utf-8")
            )
            outcome["accepted"] = False
            write_json_atomic(duplicate / "outcome.json", outcome)

            report = aggregate_from_spec(runs, spec_path)
            self.assertFalse(report["selected_subset"]["accepted"])
            statuses = {
                (cell["physical_gpu"], cell["trial"]): cell["status"]
                for cell in report["cells"]
            }
            self.assertEqual(statuses[(0, 0)], "duplicate")
            self.assertEqual(statuses[(0, 1)], "missing")
            duplicate_report = next(
                run
                for cell in report["cells"]
                for run in cell["runs"]
                if run["run_id"] == duplicate.name
            )
            self.assertFalse(duplicate_report["valid"])
            self.assertTrue(
                any(
                    "outcome.accepted" in error
                    for error in duplicate_report["validation_errors"]
                )
            )
            self.assertTrue(valid.is_dir())
            output_path = root / "failed-report.json"
            self.assertEqual(
                main(
                    [
                        "--evidence-spec",
                        str(spec_path),
                        "--run-root",
                        str(runs),
                        "--output",
                        str(output_path),
                    ]
                ),
                1,
            )
            self.assertFalse(
                json.loads(output_path.read_text(encoding="utf-8"))[
                    "selected_subset"
                ]["accepted"]
            )

    def test_missing_auxiliary_baseline_artifacts_fail(self) -> None:
        for missing_name in (
            "command.json",
            "events.jsonl",
            "stdout.log",
            "stderr.log",
        ):
            with self.subTest(missing_name=missing_name):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    runs = root / "runs"
                    runs.mkdir()
                    directory = _write_baseline(runs, gpu=0, trial=0)
                    (directory / missing_name).unlink()
                    spec_path = root / "spec.json"
                    write_json_atomic(spec_path, _spec(selected=[0]))

                    report = aggregate_from_spec(runs, spec_path)
                    run = report["cells"][0]["runs"][0]
                    self.assertFalse(report["selected_subset"]["accepted"])
                    self.assertFalse(run["valid"])
                    self.assertTrue(
                        any(
                            "missing evidence files" in error
                            and missing_name in error
                            for error in run["validation_errors"]
                        )
                    )

    def test_stdout_stderr_and_event_chain_tampering_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / "runs"
            runs.mkdir()
            directory = _write_baseline(runs, gpu=0, trial=0)
            spec_path = root / "spec.json"
            write_json_atomic(spec_path, _spec(selected=[0]))

            stdout_native = _native(gpu=0)
            stdout_native["iterations"] = 7
            (directory / "stdout.log").write_text(
                canonical_json(stdout_native) + "\n",
                encoding="utf-8",
            )
            (directory / "stderr.log").write_text(
                "unexpected warning\n",
                encoding="utf-8",
            )
            events_path = directory / "events.jsonl"
            events = [
                json.loads(line)
                for line in events_path.read_text(encoding="utf-8").splitlines()
            ]
            events[1]["sequence"] = 9
            events[2]["run_id"] = "synthetic-wrong-run"
            events[2]["payload"]["accepted"] = False
            events_path.write_text(
                "".join(canonical_json(event) + "\n" for event in events),
                encoding="utf-8",
            )

            report = aggregate_from_spec(runs, spec_path)
            errors = report["cells"][0]["runs"][0]["validation_errors"]
            self.assertFalse(report["selected_subset"]["accepted"])
            self.assertIn(
                "stdout native JSON does not match native.json",
                errors,
            )
            self.assertIn(
                "stderr.log must be empty for an accepted baseline",
                errors,
            )
            self.assertTrue(
                any("event sequences are" in error for error in errors)
            )
            self.assertTrue(
                any("mismatched run_id" in error for error in errors)
            )
            self.assertIn(
                "terminal run.completed payload does not match outcome.json",
                errors,
            )
            native_line = canonical_json(_native(gpu=0))
            (directory / "stdout.log").write_text(
                native_line + "\n" + native_line + "\n",
                encoding="utf-8",
            )
            repeated = aggregate_from_spec(runs, spec_path)
            repeated_errors = repeated["cells"][0]["runs"][0][
                "validation_errors"
            ]
            self.assertTrue(
                any(
                    "exactly one non-empty JSON line" in error
                    for error in repeated_errors
                )
            )

    def test_command_and_embedded_content_hash_tampering_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / "runs"
            runs.mkdir()
            gate_record = {
                "path": "experiments/manifests/synthetic.json",
                "git_blob": "c" * 40,
                "sha256": "0" * 64,
                "content": _GATE_CONTENT,
            }
            directory = _write_baseline(
                runs,
                gpu=0,
                trial=0,
                extra_config={"gate_manifest": gate_record},
            )
            manifest = json.loads(
                (directory / "manifest.json").read_text(encoding="utf-8")
            )
            manifest["environment"]["native_build"]["content"] += "TAMPERED=1\n"
            write_json_atomic(directory / "manifest.json", manifest)
            command = json.loads(
                (directory / "command.json").read_text(encoding="utf-8")
            )
            command["argv"][2] = "stream"
            command["argv"][4] = "7"
            command["argv"].extend(
                ["--enabled-tpc", "0", "--allow-unsupported-driver"]
            )
            command["environment_overrides"]["CUDA_VISIBLE_DEVICES"] = _gpu_uuid(1)
            command["environment_overrides"]["CUDA_MPS_PIPE_DIRECTORY"] = "/tmp/mps"
            command["environment_overrides"]["MASK_OFF"] = "1234"
            write_json_atomic(directory / "command.json", command)
            spec_path = root / "spec.json"
            write_json_atomic(spec_path, _spec(selected=[0]))

            report = aggregate_from_spec(runs, spec_path)
            errors = report["cells"][0]["runs"][0]["validation_errors"]
            self.assertFalse(report["selected_subset"]["accepted"])
            self.assertIn("native build stamp content SHA256 mismatch", errors)
            self.assertIn("Gate-A manifest content SHA256 mismatch", errors)
            self.assertIn(
                "baseline command must not contain --enabled-tpc",
                errors,
            )
            self.assertIn(
                "baseline command must not contain --allow-unsupported-driver",
                errors,
            )
            self.assertTrue(
                any("command --mode" in error for error in errors)
            )
            self.assertTrue(
                any("command --iterations" in error for error in errors)
            )
            self.assertTrue(
                any("CUDA_VISIBLE_DEVICES" in error for error in errors)
            )
            self.assertTrue(
                any("CUDA_MPS_PIPE_DIRECTORY" in error for error in errors)
            )
            self.assertTrue(any("MASK_OFF" in error for error in errors))

    def test_sealed_rejection_detects_any_native_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / "runs"
            runs.mkdir()
            _write_baseline(runs, gpu=0, trial=0)
            rejection = _write_rejection(runs)
            spec_path = root / "spec.json"
            write_json_atomic(
                spec_path,
                _spec(selected=[0], rejections=[rejection.name]),
            )

            accepted = aggregate_from_spec(runs, spec_path)
            self.assertTrue(accepted["sealed_rejections"]["valid"])
            self.assertTrue(accepted["selected_subset"]["accepted"])

            outcome = json.loads(
                (rejection / "outcome.json").read_text(encoding="utf-8")
            )
            outcome["process_exit_code"] = 0
            write_json_atomic(rejection / "outcome.json", outcome)
            write_json_atomic(rejection / "native.json", _native(gpu=0, mode="global"))
            append_jsonl_atomic(
                rejection / "events.jsonl",
                EventRecord.create(
                    run_id=rejection.name,
                    sequence=2,
                    event_type="run.started",
                    payload={},
                    timestamp_utc="2026-01-01T00:00:01.000000Z",
                ),
            )
            rejected = aggregate_from_spec(runs, spec_path)
            self.assertFalse(rejected["sealed_rejections"]["valid"])
            self.assertFalse(rejected["selected_subset"]["accepted"])
            errors = rejected["sealed_rejections"]["runs"][0][
                "validation_errors"
            ]
            self.assertIn(
                "sealed rejection unexpectedly contains native.json",
                errors,
            )
            self.assertIn(
                "sealed rejection contains run.started event",
                errors,
            )
            self.assertTrue(
                any("outcome.process_exit_code" in error for error in errors)
            )

    def test_sealed_rejection_artifacts_and_command_are_fail_closed(self) -> None:
        required = (
            "manifest.json",
            "outcome.json",
            "events.jsonl",
            "command.json",
            "stdout.log",
            "stderr.log",
        )
        for missing_name in required:
            with self.subTest(missing_name=missing_name):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    runs = root / "runs"
                    runs.mkdir()
                    _write_baseline(runs, gpu=0, trial=0)
                    rejection = _write_rejection(runs)
                    (rejection / missing_name).unlink()
                    spec_path = root / "spec.json"
                    write_json_atomic(
                        spec_path,
                        _spec(selected=[0], rejections=[rejection.name]),
                    )

                    report = aggregate_from_spec(runs, spec_path)
                    errors = report["sealed_rejections"]["runs"][0][
                        "validation_errors"
                    ]
                    self.assertFalse(report["sealed_rejections"]["valid"])
                    self.assertTrue(
                        any(
                            "missing sealed rejection evidence files" in error
                            and missing_name in error
                            for error in errors
                        )
                    )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / "runs"
            runs.mkdir()
            _write_baseline(runs, gpu=0, trial=0)
            rejection = _write_rejection(runs)
            spec_path = root / "spec.json"
            write_json_atomic(
                spec_path,
                _spec(selected=[0], rejections=[rejection.name]),
            )
            command = json.loads(
                (rejection / "command.json").read_text(encoding="utf-8")
            )
            command["argv"][2] = "baseline"
            write_json_atomic(rejection / "command.json", command)
            (rejection / "stderr.log").write_text("", encoding="utf-8")

            report = aggregate_from_spec(runs, spec_path)
            errors = report["sealed_rejections"]["runs"][0][
                "validation_errors"
            ]
            self.assertFalse(report["sealed_rejections"]["valid"])
            self.assertIn(
                "sealed rejection command does not name a masked mode",
                errors,
            )
            self.assertIn(
                "sealed rejection command mode does not match manifest mode",
                errors,
            )
            self.assertIn(
                "sealed rejection stderr.log must contain non-empty rejection text",
                errors,
            )

    def test_v2_full_eight_by_three_a0_matrix_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / "runs"
            runs.mkdir()
            for gpu in range(8):
                for trial in range(3):
                    _write_baseline_v2(runs, gpu=gpu, trial=trial)
            rejection = _write_rejection_v2(runs)
            spec_path = root / "spec.json"
            write_json_atomic(
                spec_path,
                _spec_v2(
                    selected=list(range(8)),
                    trials=[0, 1, 2],
                    rejections=[rejection.name],
                ),
            )
            report = aggregate_from_spec(runs, spec_path)
            self.assertEqual(
                report["schema_version"],
                "burstserve.gate-a0-evidence-report/v2",
            )
            self.assertEqual(
                report["evidence_spec"]["schema_version"],
                EVIDENCE_SPEC_SCHEMA_VERSION_V2,
            )
            self.assertEqual(
                report["evidence_spec"]["sha256"],
                hashlib.sha256(spec_path.read_bytes()).hexdigest(),
            )
            self.assertTrue(report["selected_subset"]["accepted"])
            self.assertTrue(report["gate_a0"]["complete"])
            self.assertEqual(
                report["gate_a0"]["runner_contract_schemas"],
                ["burstserve.smid-probe-cell/v2"],
            )
            self.assertTrue(report["gate_a0"]["v2_matrix_shape_valid"])
            self.assertTrue(report["sealed_rejections"]["valid"])

            write_json_atomic(
                spec_path,
                _spec(
                    selected=list(range(8)),
                    trials=[0, 1, 2],
                    rejections=[rejection.name],
                ),
            )
            downgraded = aggregate_from_spec(runs, spec_path)
            self.assertFalse(downgraded["selected_subset"]["accepted"])
            self.assertFalse(
                downgraded["selected_subset"][
                    "evidence_spec_schema_compatible"
                ]
            )
            self.assertFalse(downgraded["gate_a0"]["complete"])

    def test_v2_selected_evidence_requires_a_sealed_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / "runs"
            runs.mkdir()
            _write_baseline_v2(runs, gpu=0, trial=0)
            spec_path = root / "spec.json"
            write_json_atomic(
                spec_path,
                _spec_v2(selected=[0], trials=[0]),
            )

            report = aggregate_from_spec(runs, spec_path)
            self.assertFalse(report["sealed_rejections"]["valid"])
            self.assertFalse(report["selected_subset"]["accepted"])

    def test_v2_launcher_fd_identity_tampering_is_rejected(self) -> None:
        tampered_values = {
            "path": "/synthetic/other-probe",
            "inode": 999,
            "sha256": "f" * 64,
            "execution_path": "/proc/self/fd/99",
        }
        for field, value in tampered_values.items():
            with self.subTest(field=field):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    runs = root / "runs"
                    runs.mkdir()
                    directory = _write_baseline_v2(
                        runs,
                        gpu=0,
                        trial=0,
                    )
                    rejection = _write_rejection_v2(runs)
                    command = json.loads(
                        (directory / "command.json").read_text(
                            encoding="utf-8"
                        )
                    )
                    command["launcher_fd"][field] = value
                    write_json_atomic(directory / "command.json", command)
                    spec_path = root / "spec.json"
                    write_json_atomic(
                        spec_path,
                        _spec_v2(
                            selected=[0],
                            trials=[0],
                            rejections=[rejection.name],
                        ),
                    )

                    report = aggregate_from_spec(runs, spec_path)
                    run = report["cells"][0]["runs"][0]
                    self.assertFalse(report["selected_subset"]["accepted"])
                    self.assertFalse(run["valid"])
                    self.assertTrue(
                        any(
                            "launcher" in error.lower()
                            or "FD-bound" in error
                            for error in run["validation_errors"]
                        )
                    )

    def test_v2_final_launcher_fd_tampering_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / "runs"
            runs.mkdir()
            directory = _write_baseline_v2(runs, gpu=0, trial=0)
            rejection = _write_rejection_v2(runs)
            command = json.loads(
                (directory / "command.json").read_text(encoding="utf-8")
            )
            command["launcher_fd_final"]["identity"]["sha256"] = "f" * 64
            write_json_atomic(directory / "command.json", command)
            spec_path = root / "spec.json"
            write_json_atomic(
                spec_path,
                _spec_v2(
                    selected=[0],
                    trials=[0],
                    rejections=[rejection.name],
                ),
            )

            report = aggregate_from_spec(runs, spec_path)
            run = report["cells"][0]["runs"][0]
            self.assertFalse(run["valid"])
            self.assertTrue(
                any(
                    "launcher_fd_final" in error
                    for error in run["validation_errors"]
                )
            )

    def test_v2_final_libcuda_identity_tampering_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / "runs"
            runs.mkdir()
            directory = _write_baseline_v2(runs, gpu=0, trial=0)
            rejection = _write_rejection_v2(runs)
            outcome = json.loads(
                (directory / "outcome.json").read_text(encoding="utf-8")
            )
            outcome["libcuda_final_revalidation"]["matches_initial"] = False
            outcome["libcuda_final_revalidation"]["passed"] = False
            write_json_atomic(directory / "outcome.json", outcome)
            events = [
                json.loads(line)
                for line in (directory / "events.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            events[-1]["payload"] = outcome
            (directory / "events.jsonl").write_text(
                "".join(canonical_json(event) + "\n" for event in events),
                encoding="utf-8",
            )
            spec_path = root / "spec.json"
            write_json_atomic(
                spec_path,
                _spec_v2(
                    selected=[0],
                    trials=[0],
                    rejections=[rejection.name],
                ),
            )

            report = aggregate_from_spec(runs, spec_path)
            run = report["cells"][0]["runs"][0]
            self.assertFalse(run["valid"])
            self.assertTrue(
                any(
                    "libcuda" in error.lower()
                    for error in run["validation_errors"]
                )
            )

    def test_v2_validation_uses_one_hash_bound_snapshot_under_toctou(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / "runs"
            runs.mkdir()
            directory = _write_baseline_v2(runs, gpu=0, trial=0)
            rejection = _write_rejection_v2(runs)
            outcome_path = directory / "outcome.json"
            events_path = directory / "events.jsonl"
            valid_outcome = outcome_path.read_bytes()
            valid_events = events_path.read_bytes()
            invalid_outcome = json.loads(valid_outcome)
            invalid_outcome["accepted"] = False
            write_json_atomic(outcome_path, invalid_outcome)
            invalid_sha = hashlib.sha256(outcome_path.read_bytes()).hexdigest()
            original_candidate = gate_a_results._candidate_cell
            mutated = False

            def mutate_after_snapshot(*args, **kwargs):
                nonlocal mutated
                manifest_value = args[0]
                if (
                    not mutated
                    and manifest_value.get("run_id") == directory.name
                ):
                    mutated = True
                    outcome_path.write_bytes(valid_outcome)
                    events_path.write_bytes(valid_events)
                return original_candidate(*args, **kwargs)

            spec_path = root / "spec.json"
            write_json_atomic(
                spec_path,
                _spec_v2(
                    selected=[0],
                    trials=[0],
                    rejections=[rejection.name],
                ),
            )
            with mock.patch.object(
                gate_a_results,
                "_candidate_cell",
                side_effect=mutate_after_snapshot,
            ):
                report = aggregate_from_spec(runs, spec_path)

            run = report["cells"][0]["runs"][0]
            self.assertTrue(mutated)
            self.assertFalse(run["valid"])
            self.assertFalse(report["selected_subset"]["accepted"])
            self.assertEqual(
                run["file_sha256"]["outcome.json"],
                invalid_sha,
            )
            self.assertNotEqual(
                run["file_sha256"]["outcome.json"],
                hashlib.sha256(outcome_path.read_bytes()).hexdigest(),
            )
            self.assertTrue(
                any(
                    "outcome.accepted=False" in error
                    for error in run["validation_errors"]
                )
            )

    def test_v2_strict_canonical_json_attacks_are_rejected(self) -> None:
        attacks = ("duplicate", "noncanonical", "nan", "integer", "depth")
        for attack in attacks:
            with self.subTest(attack=attack):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    runs = root / "runs"
                    runs.mkdir()
                    directory = _write_baseline_v2(
                        runs,
                        gpu=0,
                        trial=0,
                    )
                    rejection = _write_rejection_v2(runs)
                    outcome_path = directory / "outcome.json"
                    value = json.loads(outcome_path.read_text("utf-8"))
                    if attack == "duplicate":
                        original = outcome_path.read_bytes()
                        outcome_path.write_bytes(
                            b'{"accepted":true,' + original[1:]
                        )
                    elif attack == "noncanonical":
                        outcome_path.write_bytes(
                            b" " + outcome_path.read_bytes()
                        )
                    elif attack == "nan":
                        value["semantic_metrics"] = {"attack": float("nan")}
                        outcome_path.write_text(
                            json.dumps(
                                value,
                                allow_nan=True,
                                ensure_ascii=False,
                                separators=(",", ":"),
                                sort_keys=True,
                            )
                            + "\n",
                            encoding="utf-8",
                        )
                    elif attack == "integer":
                        value["raw_process_return_code"] = int("9" * 129)
                        write_json_atomic(outcome_path, value)
                    else:
                        nested: object = 0
                        for _ in range(130):
                            nested = [nested]
                        value["semantic_metrics"] = {"attack": nested}
                        write_json_atomic(outcome_path, value)
                    spec_path = root / "spec.json"
                    write_json_atomic(
                        spec_path,
                        _spec_v2(
                            selected=[0],
                            trials=[0],
                            rejections=[rejection.name],
                        ),
                    )

                    report = aggregate_from_spec(runs, spec_path)
                    run = report["cells"][0]["runs"][0]
                    self.assertFalse(run["valid"])
                    self.assertFalse(report["selected_subset"]["accepted"])
                    self.assertTrue(
                        any(
                            "outcome.json" in error
                            for error in run["validation_errors"]
                        )
                    )

    def test_v2_unknown_schema_fields_are_rejected_everywhere(self) -> None:
        for name in (
            "manifest_config",
            "outcome",
            "native",
            "command",
            "event",
        ):
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    runs = root / "runs"
                    runs.mkdir()
                    directory = _write_baseline_v2(
                        runs,
                        gpu=0,
                        trial=0,
                    )
                    rejection = _write_rejection_v2(runs)
                    if name == "manifest_config":
                        path = directory / "manifest.json"
                        value = json.loads(path.read_text("utf-8"))
                        value["config"]["unknown_attack"] = True
                        write_json_atomic(path, value)
                    elif name in {"outcome", "native", "command"}:
                        path = directory / f"{name}.json"
                        value = json.loads(path.read_text("utf-8"))
                        value["unknown_attack"] = True
                        write_json_atomic(path, value)
                    else:
                        path = directory / "events.jsonl"
                        lines = [
                            json.loads(line)
                            for line in path.read_text("utf-8").splitlines()
                        ]
                        lines[0]["unknown_attack"] = True
                        path.write_text(
                            "".join(
                                canonical_json(line) + "\n"
                                for line in lines
                            ),
                            encoding="utf-8",
                        )
                    spec_path = root / "spec.json"
                    write_json_atomic(
                        spec_path,
                        _spec_v2(
                            selected=[0],
                            trials=[0],
                            rejections=[rejection.name],
                        ),
                    )

                    report = aggregate_from_spec(runs, spec_path)
                    run = report["cells"][0]["runs"][0]
                    self.assertFalse(run["valid"])
                    self.assertFalse(report["selected_subset"]["accepted"])
                    self.assertTrue(
                        any(
                            "unknown" in error
                            or "keys are not exact" in error
                            for error in run["validation_errors"]
                        )
                    )

    def test_v2_nested_unknown_fields_and_stdout_whitespace_reject(
        self,
    ) -> None:
        attacks = (
            "gpu",
            "driver_policy",
            "command_policy",
            "preflight_event",
            "stdout_whitespace",
        )
        for attack in attacks:
            with self.subTest(attack=attack):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    runs = root / "runs"
                    runs.mkdir()
                    directory = _write_baseline_v2(
                        runs,
                        gpu=0,
                        trial=0,
                    )
                    rejection = _write_rejection_v2(runs)
                    if attack == "gpu":
                        path = directory / "manifest.json"
                        value = json.loads(path.read_text("utf-8"))
                        value["environment"][
                            "selected_gpu_initial_preflight"
                        ]["unknown_attack"] = True
                        write_json_atomic(path, value)
                    elif attack == "driver_policy":
                        path = directory / "outcome.json"
                        value = json.loads(path.read_text("utf-8"))
                        value["driver_policy"]["unknown_attack"] = True
                        write_json_atomic(path, value)
                    elif attack == "command_policy":
                        path = directory / "command.json"
                        value = json.loads(path.read_text("utf-8"))
                        value["environment_policy"] = {
                            "mode": "env-i exact allowlist",
                            "allowed_names": [],
                            "inherited_names": [],
                            "unknown_attack": True,
                        }
                        write_json_atomic(path, value)
                    elif attack == "preflight_event":
                        path = directory / "events.jsonl"
                        values = [
                            json.loads(line)
                            for line in path.read_text("utf-8").splitlines()
                        ]
                        values[0]["payload"]["unknown_attack"] = True
                        path.write_text(
                            "".join(
                                canonical_json(value) + "\n"
                                for value in values
                            ),
                            encoding="utf-8",
                        )
                    else:
                        path = directory / "stdout.log"
                        path.write_bytes(b" " + path.read_bytes())
                    spec_path = root / "spec.json"
                    write_json_atomic(
                        spec_path,
                        _spec_v2(
                            selected=[0],
                            trials=[0],
                            rejections=[rejection.name],
                        ),
                    )

                    report = aggregate_from_spec(runs, spec_path)
                    run = report["cells"][0]["runs"][0]
                    self.assertFalse(run["valid"])
                    self.assertFalse(report["selected_subset"]["accepted"])

    def test_evidence_spec_requires_strict_canonical_exact_schema(self) -> None:
        attacks = ("unknown", "duplicate", "pretty")
        for attack in attacks:
            with self.subTest(attack=attack):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    spec_path = root / "spec.json"
                    value = _spec(selected=[0], trials=[0])
                    if attack == "pretty":
                        value["schema_version"] = (
                            "burstserve.gate-a0-evidence-spec/v2"
                        )
                    if attack == "unknown":
                        value["unknown_attack"] = True
                        write_json_atomic(spec_path, value)
                    elif attack == "duplicate":
                        write_json_atomic(spec_path, value)
                        original = spec_path.read_bytes()
                        spec_path.write_bytes(
                            b'{"evidence_id":"duplicate",' + original[1:]
                        )
                    else:
                        spec_path.write_text(
                            json.dumps(value, indent=2) + "\n",
                            encoding="utf-8",
                        )
                    with self.assertRaises(ValueError):
                        gate_a_results.load_evidence_spec(spec_path)

    def test_evidence_spec_rejects_run_id_path_traversal(self) -> None:
        attacks = (
            ("excluded_runs", "../outside"),
            ("excluded_runs", "nested/outside"),
            ("sealed_rejection_run_ids", ".."),
            ("sealed_rejection_run_ids", "nested/outside"),
        )
        for field, run_id in attacks:
            with self.subTest(field=field, run_id=run_id):
                with tempfile.TemporaryDirectory() as temporary:
                    spec_path = Path(temporary) / "spec.json"
                    value = _spec(selected=[0], trials=[0])
                    if field == "excluded_runs":
                        value[field] = [
                            {"run_id": run_id, "reason": "attack"}
                        ]
                    else:
                        value[field] = [run_id]
                    write_json_atomic(spec_path, value)
                    with self.assertRaisesRegex(
                        ValueError,
                        "safe path component",
                    ):
                        gate_a_results.load_evidence_spec(spec_path)

    def test_v2_evidence_spec_rejects_non_hash_and_unsorted_run_ids(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec_path = root / "spec.json"
            invalid_id = _spec_v2(selected=[0], trials=[0])
            invalid_id["sealed_rejection_run_ids"] = ["bs1-exploratory"]
            write_json_atomic(spec_path, invalid_id)
            with self.assertRaisesRegex(ValueError, "lowercase sha256"):
                gate_a_results.load_evidence_spec(spec_path)

            unsorted = _spec_v2(selected=[1, 0], trials=[1, 0])
            unsorted["declared_gpus"] = list(
                reversed(unsorted["declared_gpus"])
            )
            unsorted["excluded_runs"] = [
                {"run_id": f"bs1-{'b' * 64}", "reason": "second"},
                {"run_id": f"bs1-{'a' * 64}", "reason": "first"},
            ]
            write_json_atomic(spec_path, unsorted)
            with self.assertRaisesRegex(
                ValueError,
                "canonical normalized JSON",
            ):
                gate_a_results.load_evidence_spec(spec_path)

    def test_evidence_spec_cardinality_and_matrix_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            spec_path = Path(temporary) / "spec.json"
            value = _spec(selected=[0, 1], trials=[0, 1])
            write_json_atomic(spec_path, value)
            with mock.patch.object(
                gate_a_results,
                "_MAX_MATRIX_CELLS",
                3,
            ):
                with self.assertRaisesRegex(ValueError, "cells, limit"):
                    gate_a_results.load_evidence_spec(spec_path)

            value = _spec(selected=[0], trials=[0])
            value["excluded_runs"] = [
                {"run_id": "bs1-first", "reason": "first"},
                {"run_id": "bs1-second", "reason": "second"},
            ]
            write_json_atomic(spec_path, value)
            with mock.patch.object(
                gate_a_results,
                "_MAX_EXCLUDED_RUNS",
                1,
            ):
                with self.assertRaisesRegex(ValueError, "entries, limit"):
                    gate_a_results.load_evidence_spec(spec_path)

    def test_candidate_snapshot_count_and_bytes_are_bounded(self) -> None:
        cases = ("count", "bytes")
        for case in cases:
            with self.subTest(case=case):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    runs = root / "runs"
                    runs.mkdir()
                    _write_baseline(runs, gpu=0, trial=0)
                    if case == "count":
                        _write_baseline(runs, gpu=0, trial=1)
                        spec = _spec(selected=[0], trials=[0, 1])
                        patch_name = "_MAX_CANDIDATE_SNAPSHOTS"
                        patch_value = 1
                        message = "candidate evidence count exceeds"
                    else:
                        spec = _spec(selected=[0], trials=[0])
                        patch_name = "_MAX_CANDIDATE_SNAPSHOT_BYTES"
                        patch_value = 1
                        message = "snapshot bytes exceed"
                    spec_path = root / "spec.json"
                    write_json_atomic(spec_path, spec)
                    with mock.patch.object(
                        gate_a_results,
                        patch_name,
                        patch_value,
                    ):
                        with self.assertRaisesRegex(ValueError, message):
                            aggregate_from_spec(runs, spec_path)

    def test_scan_and_auxiliary_work_are_globally_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / "runs"
            runs.mkdir()
            for name in ("noncandidate-a", "noncandidate-b"):
                directory = runs / name
                directory.mkdir()
                write_json_atomic(directory / "manifest.json", {})
            spec_path = root / "spec.json"
            write_json_atomic(spec_path, _spec(selected=[0], trials=[0]))
            with mock.patch.object(
                gate_a_results,
                "_MAX_DISCOVERED_RUN_DIRECTORIES",
                1,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "discovered run directory count exceeds",
                ):
                    aggregate_from_spec(runs, spec_path)

            with (
                mock.patch.object(
                    gate_a_results,
                    "_MAX_DISCOVERED_RUN_DIRECTORIES",
                    10,
                ),
                mock.patch.object(
                    gate_a_results,
                    "_MAX_SCANNED_SNAPSHOT_BYTES",
                    1,
                ),
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "scanned evidence snapshot bytes exceed",
                ):
                    aggregate_from_spec(runs, spec_path)

    def test_run_root_caps_every_entry_before_filtering(self) -> None:
        for kind in ("regular", "hidden", "dangling"):
            with self.subTest(kind=kind):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    runs = root / "runs"
                    runs.mkdir()
                    for index in range(2):
                        if kind == "regular":
                            (runs / f"file-{index}").write_text(
                                "not a run",
                                encoding="utf-8",
                            )
                        elif kind == "hidden":
                            (runs / f".hidden-{index}").mkdir()
                        else:
                            (runs / f"dangling-{index}").symlink_to(
                                runs / f"missing-{index}"
                            )
                    spec_path = root / "spec.json"
                    write_json_atomic(
                        spec_path,
                        _spec(selected=[0], trials=[0]),
                    )
                    with mock.patch.object(
                        gate_a_results,
                        "_MAX_RUN_ROOT_ENTRIES",
                        1,
                    ):
                        with self.assertRaisesRegex(
                            ValueError,
                            "run-root entry count exceeds",
                        ):
                            aggregate_from_spec(runs, spec_path)

    def test_public_validator_uses_complete_spec_normalization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runs = Path(temporary)
            attacks = []
            unknown = _spec(selected=[0], trials=[0])
            unknown["unknown"] = True
            attacks.append(unknown)
            duplicate = _spec(selected=[0, 0], trials=[0])
            attacks.append(duplicate)
            undeclared = _spec(selected=[99], trials=[0])
            attacks.append(undeclared)
            overlap_id = "bs1-overlap"
            overlap = _spec(
                selected=[0],
                trials=[0],
                excluded=[{"run_id": overlap_id, "reason": "overlap"}],
                rejections=[overlap_id],
            )
            attacks.append(overlap)
            unsorted_v2 = _spec_v2(selected=[1, 0], trials=[0])
            attacks.append(unsorted_v2)
            for value in attacks:
                with self.subTest(value=value):
                    with self.assertRaises(ValueError):
                        gate_a_results.validate_gate_a0(runs, value)

    def test_pretty_v1_baseline_is_retained_but_pretty_v2_is_invalid(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / "runs"
            runs.mkdir()
            legacy = _write_baseline(runs, gpu=0, trial=0)
            legacy_manifest = json.loads(
                (legacy / "manifest.json").read_text(encoding="utf-8")
            )
            (legacy / "manifest.json").write_text(
                json.dumps(legacy_manifest, indent=2) + "\n",
                encoding="utf-8",
            )
            spec_path = root / "spec.json"
            write_json_atomic(spec_path, _spec(selected=[0], trials=[0]))
            legacy_report = aggregate_from_spec(runs, spec_path)
            self.assertTrue(legacy_report["selected_subset"]["accepted"])

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / "runs"
            runs.mkdir()
            current = _write_baseline_v2(runs, gpu=0, trial=0)
            rejection = _write_rejection_v2(runs)
            current_manifest = json.loads(
                (current / "manifest.json").read_text(encoding="utf-8")
            )
            (current / "manifest.json").write_text(
                json.dumps(current_manifest, indent=2) + "\n",
                encoding="utf-8",
            )
            rejection_manifest = json.loads(
                (rejection / "manifest.json").read_text(encoding="utf-8")
            )
            (rejection / "manifest.json").write_text(
                json.dumps(rejection_manifest, indent=2) + "\n",
                encoding="utf-8",
            )
            spec_path = root / "spec.json"
            write_json_atomic(
                spec_path,
                _spec_v2(
                    selected=[0],
                    trials=[0],
                    rejections=[rejection.name],
                ),
            )
            report = aggregate_from_spec(runs, spec_path)
            self.assertFalse(report["selected_subset"]["accepted"])
            self.assertFalse(report["cells"][0]["runs"][0]["valid"])
            self.assertFalse(report["sealed_rejections"]["valid"])

    def test_v2_required_outcome_and_event_bindings_fail_closed(self) -> None:
        attacks = (
            "missing_raw_return",
            "bad_process_group",
            "source_payload_omission",
            "source_event_mismatch",
            "preflight_payload_omission",
            "missing_launch_commit",
            "launch_commit_event_mismatch",
            "invalid_launch_commit_contract",
            "invalid_launch_commit_timestamp",
            "integer_launch_commit_horizon",
        )
        for attack in attacks:
            with self.subTest(attack=attack):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    runs = root / "runs"
                    runs.mkdir()
                    directory = _write_baseline_v2(
                        runs,
                        gpu=0,
                        trial=0,
                    )
                    rejection = _write_rejection_v2(runs)
                    outcome_path = directory / "outcome.json"
                    outcome = json.loads(
                        outcome_path.read_text(encoding="utf-8")
                    )
                    events_path = directory / "events.jsonl"
                    events = [
                        json.loads(line)
                        for line in events_path.read_text(
                            encoding="utf-8"
                        ).splitlines()
                    ]
                    if attack == "missing_raw_return":
                        del outcome["raw_process_return_code"]
                        del events[-1]["payload"]["raw_process_return_code"]
                    elif attack == "bad_process_group":
                        outcome["process_group_health"][
                            "process_group_reaped"
                        ] = False
                        events[-1]["payload"]["process_group_health"][
                            "process_group_reaped"
                        ] = False
                    elif attack == "source_payload_omission":
                        del outcome["source_prelaunch_revalidation"]["phase"]
                        del events[1]["payload"]["phase"]
                        del events[-1]["payload"][
                            "source_prelaunch_revalidation"
                        ]["phase"]
                    elif attack == "source_event_mismatch":
                        events[1]["payload"][
                            "expected_snapshot_sha256"
                        ] = "e" * 64
                        events[1]["payload"][
                            "observed_snapshot_sha256"
                        ] = "e" * 64
                    elif attack == "preflight_payload_omission":
                        del events[0]["payload"]["driver_policy"]
                    elif attack == "missing_launch_commit":
                        del outcome[
                            "launch_commit_reservation_revalidation"
                        ]
                        del events[4]["payload"][
                            "launch_commit_reservation_revalidation"
                        ]
                        del events[-1]["payload"][
                            "launch_commit_reservation_revalidation"
                        ]
                    elif attack == "launch_commit_event_mismatch":
                        events[4]["payload"][
                            "launch_commit_reservation_revalidation"
                        ]["checks"][
                            "reservation_not_required_for_baseline"
                        ] = False
                    elif attack == "invalid_launch_commit_contract":
                        invalid = outcome[
                            "launch_commit_reservation_revalidation"
                        ]
                        invalid["required_for_mode"] = True
                        invalid["required_horizon_s"] = 1.0
                        invalid["required_until_utc"] = (
                            "2026-01-01T00:00:01.450000Z"
                        )
                        events[4]["payload"][
                            "launch_commit_reservation_revalidation"
                        ] = invalid
                        events[-1]["payload"][
                            "launch_commit_reservation_revalidation"
                        ] = invalid
                    elif attack == "invalid_launch_commit_timestamp":
                        invalid = outcome[
                            "launch_commit_reservation_revalidation"
                        ]
                        invalid["captured_at_utc"] = "not-a-timestamp"
                        invalid["required_until_utc"] = "not-a-timestamp"
                        events[4]["payload"][
                            "launch_commit_reservation_revalidation"
                        ] = invalid
                        events[-1]["payload"][
                            "launch_commit_reservation_revalidation"
                        ] = invalid
                    else:
                        invalid = outcome[
                            "launch_commit_reservation_revalidation"
                        ]
                        invalid["required_horizon_s"] = 0
                        events[4]["payload"][
                            "launch_commit_reservation_revalidation"
                        ] = invalid
                        events[-1]["payload"][
                            "launch_commit_reservation_revalidation"
                        ] = invalid
                    write_json_atomic(outcome_path, outcome)
                    events_path.write_text(
                        "".join(
                            canonical_json(value) + "\n"
                            for value in events
                        ),
                        encoding="utf-8",
                    )
                    spec_path = root / "spec.json"
                    write_json_atomic(
                        spec_path,
                        _spec_v2(
                            selected=[0],
                            trials=[0],
                            rejections=[rejection.name],
                        ),
                    )
                    report = aggregate_from_spec(runs, spec_path)
                    self.assertFalse(report["cells"][0]["runs"][0]["valid"])
                    self.assertFalse(
                        report["selected_subset"]["accepted"]
                    )

    def test_global_fd_snapshot_rejects_staged_cross_file_switch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = _write_baseline_v2(root, gpu=0, trial=0)
            good = {
                name: (directory / name).read_bytes()
                for name in gate_a_results._EVIDENCE_FILES
            }
            bad = {
                name: (
                    (b"X" + content[1:])
                    if content
                    else b"X"
                )
                for name, content in good.items()
            }
            for name, content in bad.items():
                (directory / name).write_bytes(content)
            original_read = gate_a_results._read_evidence_fd_content
            order = list(gate_a_results._EVIDENCE_FILES)
            previous: str | None = None

            def staged_read(descriptor, *, name, expected_status):
                nonlocal previous
                if previous is not None:
                    (directory / previous).write_bytes(bad[previous])
                (directory / name).write_bytes(good[name])
                previous = name
                return original_read(
                    descriptor,
                    name=name,
                    expected_status=expected_status,
                )

            with mock.patch.object(
                gate_a_results,
                "_read_evidence_fd_content",
                side_effect=staged_read,
            ):
                snapshot = gate_a_results._snapshot_evidence_directory(
                    directory
                )

            self.assertEqual(order, list(gate_a_results._EVIDENCE_FILES))
            self.assertTrue(snapshot.errors)
            self.assertTrue(
                any(
                    "identity changed" in error
                    or "bytes changed" in error
                    or "changed while" in error
                    for error in snapshot.errors
                )
            )
            self.assertFalse(
                all(
                    isinstance(
                        snapshot.files[name],
                        gate_a_results._EvidenceFileSnapshot,
                    )
                    for name in gate_a_results._EVIDENCE_FILES
                )
            )

    def test_snapshot_fifo_is_rejected_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = _write_baseline_v2(root, gpu=0, trial=0)
            path = directory / "manifest.json"
            path.unlink()
            os.mkfifo(path)
            snapshot = gate_a_results._snapshot_evidence_directory(
                directory
            )
            self.assertIsNone(snapshot.files["manifest.json"])
            self.assertTrue(
                any(
                    "not a regular file" in error
                    for error in snapshot.errors
                )
            )

    def test_oversized_sparse_evidence_is_never_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = _write_baseline_v2(root, gpu=0, trial=0)
            path = directory / "manifest.json"
            with path.open("wb") as output:
                output.truncate(1024 * 1024)
            original_read = gate_a_results._read_evidence_fd_content
            read_names: list[str] = []

            def guarded_read(descriptor, *, name, expected_status):
                read_names.append(name)
                if name == "manifest.json":
                    raise AssertionError("oversized file must not be read")
                return original_read(
                    descriptor,
                    name=name,
                    expected_status=expected_status,
                )

            limits = dict(gate_a_results._EVIDENCE_FILE_SIZE_LIMITS)
            limits["manifest.json"] = 1024
            with (
                mock.patch.object(
                    gate_a_results,
                    "_EVIDENCE_FILE_SIZE_LIMITS",
                    limits,
                ),
                mock.patch.object(
                    gate_a_results,
                    "_read_evidence_fd_content",
                    side_effect=guarded_read,
                ),
            ):
                snapshot = gate_a_results._snapshot_evidence_directory(
                    directory
                )
            self.assertNotIn("manifest.json", read_names)
            self.assertIsNone(snapshot.files["manifest.json"])
            self.assertTrue(
                any("exceeds 1024 bytes" in error for error in snapshot.errors)
            )

    def test_malformed_v2_hardware_becomes_an_invalid_cell(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / "runs"
            runs.mkdir()
            directory = _write_baseline_v2(runs, gpu=0, trial=0)
            rejection = _write_rejection_v2(runs)
            manifest_value = json.loads(
                (directory / "manifest.json").read_text(encoding="utf-8")
            )
            content = manifest_value["config"]["gate_manifest"]["content"]
            del content["hardware"]["sm_count"]
            manifest_value["config"]["gate_manifest"]["sha256"] = (
                hashlib.sha256(
                    canonical_json(content).encode("utf-8")
                ).hexdigest()
            )
            malformed = RunManifest.create(
                config=manifest_value["config"],
                seed=manifest_value["seed"],
                source_revision=manifest_value["source_revision"],
                environment=manifest_value["environment"],
                metadata=manifest_value["metadata"],
                created_at_utc=manifest_value["created_at_utc"],
            )
            malformed_directory = runs / malformed.run_id
            directory.rename(malformed_directory)
            write_json_atomic(
                malformed_directory / "manifest.json",
                malformed.to_dict(),
            )
            spec_path = root / "spec.json"
            write_json_atomic(
                spec_path,
                _spec_v2(
                    selected=[0],
                    trials=[0],
                    rejections=[rejection.name],
                ),
            )

            report = aggregate_from_spec(runs, spec_path)
            run = report["cells"][0]["runs"][0]
            self.assertFalse(run["valid"])
            self.assertTrue(
                any(
                    "validator rejected malformed evidence" in error
                    for error in run["validation_errors"]
                )
            )

    def test_v2_sealed_rejection_is_strictly_identity_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / "runs"
            runs.mkdir()
            _write_baseline_v2(runs, gpu=0, trial=0)
            rejection = _write_rejection_v2(runs)
            outcome = json.loads(
                (rejection / "outcome.json").read_text(encoding="utf-8")
            )
            outcome["manifest_policy"]["unexpected_false_check"] = False
            outcome["formal_source_binding"]["launcher_fd_identity"][
                "inode"
            ] = 999
            write_json_atomic(rejection / "outcome.json", outcome)
            write_json_atomic(
                rejection / "native.json",
                _native(gpu=0, mode="global"),
            )
            append_jsonl_atomic(
                rejection / "events.jsonl",
                EventRecord.create(
                    run_id=rejection.name,
                    sequence=2,
                    event_type="run.started",
                    payload={},
                    timestamp_utc="2026-01-01T00:00:02.000000Z",
                ),
            )
            spec_path = root / "spec.json"
            write_json_atomic(
                spec_path,
                _spec_v2(
                    selected=[0],
                    trials=[0],
                    rejections=[rejection.name],
                ),
            )

            report = aggregate_from_spec(runs, spec_path)
            sealed = report["sealed_rejections"]
            self.assertFalse(sealed["valid"])
            errors = sealed["runs"][0]["validation_errors"]
            self.assertIn(
                "sealed rejection unexpectedly contains native.json",
                errors,
            )
            self.assertIn(
                "sealed rejection contains run.started event",
                errors,
            )
            self.assertTrue(
                any(
                    "non-authorization false checks" in error
                    for error in errors
                )
            )
            self.assertTrue(
                any(
                    "outcome formal launcher identity differs" in error
                    for error in errors
                )
            )

    def test_one_report_rejects_mixed_v1_and_v2_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / "runs"
            runs.mkdir()
            _write_baseline(runs, gpu=0, trial=0)
            _write_baseline_v2(runs, gpu=1, trial=0)
            spec_path = root / "spec.json"
            write_json_atomic(
                spec_path,
                _spec_v2(selected=[0, 1], trials=[0]),
            )
            report = aggregate_from_spec(runs, spec_path)
            self.assertFalse(report["selected_subset"]["accepted"])
            self.assertEqual(
                report["selected_subset"]["runner_contract_schemas"],
                [
                    "burstserve.smid-probe-cell/v1",
                    "burstserve.smid-probe-cell/v2",
                ],
            )

    def test_v2_spec_cannot_downgrade_to_legacy_cells(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / "runs"
            runs.mkdir()
            _write_baseline(runs, gpu=0, trial=0)
            spec_path = root / "spec.json"
            write_json_atomic(
                spec_path,
                _spec_v2(selected=[0], trials=[0]),
            )

            report = aggregate_from_spec(runs, spec_path)

            self.assertEqual(
                report["schema_version"],
                "burstserve.gate-a0-evidence-report/v2",
            )
            selected = report["selected_subset"]
            self.assertFalse(selected["accepted"])
            self.assertFalse(
                selected["evidence_spec_schema_compatible"]
            )
            self.assertEqual(
                selected["runner_contract_schemas"],
                ["burstserve.smid-probe-cell/v1"],
            )

    def test_v1_matrix_with_a_valid_v2_sealed_rejection_is_rejected(
        self,
    ) -> None:
        """A stronger-looking v2 rejection must not ride into a v1 Gate."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / "runs"
            runs.mkdir()
            for gpu in range(8):
                _write_baseline(runs, gpu=gpu, trial=0)
            rejection = _write_rejection_v2(runs)
            spec_path = root / "spec.json"
            write_json_atomic(
                spec_path,
                _spec(
                    selected=list(range(8)),
                    trials=[0],
                    rejections=[rejection.name],
                ),
            )

            report = aggregate_from_spec(runs, spec_path)

            # Every v1 cell is individually valid, so only the rejection
            # schema pairing can stop this matrix.
            self.assertEqual(
                report["gate_a0"]["runner_contract_schemas"],
                ["burstserve.smid-probe-cell/v1"],
            )
            self.assertTrue(
                all(
                    cell["status"] == "valid" for cell in report["cells"]
                ),
                [cell["status"] for cell in report["cells"]],
            )
            # A v2 rejection forces the v2 report, never a silent v1 report.
            self.assertEqual(
                report["schema_version"],
                "burstserve.gate-a0-evidence-report/v2",
            )
            self.assertEqual(
                report["sealed_rejections"]["runner_contract_schemas"],
                ["burstserve.smid-probe-cell/v2"],
            )
            self.assertFalse(
                report["sealed_rejections"][
                    "evidence_spec_schema_compatible"
                ]
            )
            self.assertFalse(report["sealed_rejections"]["valid"])
            self.assertFalse(report["selected_subset"]["accepted"])
            self.assertFalse(report["gate_a0"]["complete"])

    def test_v2_matrix_with_a_legacy_sealed_rejection_is_rejected(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / "runs"
            runs.mkdir()
            _write_baseline_v2(runs, gpu=0, trial=0)
            rejection = _write_rejection(runs)
            spec_path = root / "spec.json"
            write_json_atomic(
                spec_path,
                _spec_v2(
                    selected=[0],
                    trials=[0],
                    rejections=[rejection.name],
                ),
            )

            report = aggregate_from_spec(runs, spec_path)

            self.assertEqual(
                report["sealed_rejections"]["runner_contract_schemas"],
                ["burstserve.smid-probe-cell/v1"],
            )
            self.assertFalse(
                report["sealed_rejections"][
                    "evidence_spec_schema_compatible"
                ]
            )
            self.assertFalse(report["selected_subset"]["accepted"])
            self.assertFalse(report["gate_a0"]["complete"])

    def test_v2_boolean_integer_and_float_substitutions_are_rejected(
        self,
    ) -> None:
        """``True``/``1``/``1.0`` must never satisfy a typed v2 field."""

        attacks = {
            "config_bool_for_int": ("config", "trial", False),
            "config_int_for_bool": ("config", "allow_busy_gpu", 0),
            "config_float_for_int": ("config", "physical_gpu", 0.0),
            "config_float_blocks": ("config", "blocks", 4096.0),
            "outcome_bool_for_exit": ("outcome", "exit_code", False),
            "outcome_int_for_bool": (
                "outcome",
                "requires_matrix_validation",
                0,
            ),
            "outcome_float_for_exit": (
                "outcome",
                "process_exit_code",
                0.0,
            ),
            "native_bool_for_blocks": ("native", "blocks", True),
            "native_float_for_iterations": ("native", "iterations", 4096.0),
            "native_bool_for_driver": ("native", "driver_version", True),
        }
        for attack, (target, field, value) in attacks.items():
            with self.subTest(attack=attack):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    runs = root / "runs"
                    runs.mkdir()
                    directory = _write_baseline_v2(runs, gpu=0, trial=0)
                    rejection = _write_rejection_v2(runs)
                    if target == "config":
                        path = directory / "manifest.json"
                        document = json.loads(
                            path.read_text(encoding="utf-8")
                        )
                        document["config"][field] = value
                        write_json_atomic(path, document)
                    elif target == "outcome":
                        path = directory / "outcome.json"
                        document = json.loads(
                            path.read_text(encoding="utf-8")
                        )
                        document[field] = value
                        write_json_atomic(path, document)
                        events_path = directory / "events.jsonl"
                        events = [
                            json.loads(line)
                            for line in events_path.read_text(
                                encoding="utf-8"
                            ).splitlines()
                        ]
                        events[-1]["payload"][field] = value
                        events_path.write_text(
                            "".join(
                                canonical_json(item) + "\n"
                                for item in events
                            ),
                            encoding="utf-8",
                        )
                    else:
                        path = directory / "native.json"
                        document = json.loads(
                            path.read_text(encoding="utf-8")
                        )
                        document[field] = value
                        write_json_atomic(path, document)
                        (directory / "stdout.log").write_text(
                            canonical_json(document) + "\n",
                            encoding="utf-8",
                        )
                    spec_path = root / "spec.json"
                    write_json_atomic(
                        spec_path,
                        _spec_v2(
                            selected=[0],
                            trials=[0],
                            rejections=[rejection.name],
                        ),
                    )
                    report = aggregate_from_spec(runs, spec_path)
                    # A substituted cell-identity type drops the run out of
                    # the matrix entirely; every other substitution keeps the
                    # run but marks it invalid.  Both are fail-closed.
                    cell = report["cells"][0]
                    self.assertNotEqual(cell["status"], "valid", cell)
                    self.assertFalse(
                        report["selected_subset"]["accepted"]
                    )

    def test_v2_sealed_rejection_type_substitutions_are_rejected(
        self,
    ) -> None:
        for field, value in (
            ("exit_code", True),
            ("accepted", 0),
            ("local_probe_passed", 0),
            ("timed_out", 0),
        ):
            with self.subTest(field=field, value=value):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    runs = root / "runs"
                    runs.mkdir()
                    _write_baseline_v2(runs, gpu=0, trial=0)
                    rejection = _write_rejection_v2(runs)
                    outcome_path = rejection / "outcome.json"
                    outcome = json.loads(
                        outcome_path.read_text(encoding="utf-8")
                    )
                    outcome[field] = value
                    write_json_atomic(outcome_path, outcome)
                    events_path = rejection / "events.jsonl"
                    events = [
                        json.loads(line)
                        for line in events_path.read_text(
                            encoding="utf-8"
                        ).splitlines()
                    ]
                    events[-1]["payload"][field] = value
                    events_path.write_text(
                        "".join(
                            canonical_json(item) + "\n" for item in events
                        ),
                        encoding="utf-8",
                    )
                    spec_path = root / "spec.json"
                    write_json_atomic(
                        spec_path,
                        _spec_v2(
                            selected=[0],
                            trials=[0],
                            rejections=[rejection.name],
                        ),
                    )
                    report = aggregate_from_spec(runs, spec_path)
                    self.assertFalse(
                        report["sealed_rejections"]["runs"][0]["valid"]
                    )
                    self.assertFalse(
                        report["selected_subset"]["accepted"]
                    )

    def test_v2_final_launch_preflight_contract_is_exact(self) -> None:
        attacks = (
            "extra_key",
            "missing_gpu_field",
            "wrong_gpu_index",
            "wrong_gpu_uuid",
            "nonempty_compute_processes",
            "malformed_mps_record",
            "integer_horizon",
            "nonzero_horizon",
            "required_until_drift",
            "noncanonical_timestamp",
            "extra_check",
            "missing_check",
            "nonempty_errors",
            "started_event_mismatch",
        )
        for attack in attacks:
            with self.subTest(attack=attack):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    runs = root / "runs"
                    runs.mkdir()
                    directory = _write_baseline_v2(runs, gpu=0, trial=0)
                    rejection = _write_rejection_v2(runs)
                    outcome_path = directory / "outcome.json"
                    outcome = json.loads(
                        outcome_path.read_text(encoding="utf-8")
                    )
                    events_path = directory / "events.jsonl"
                    events = [
                        json.loads(line)
                        for line in events_path.read_text(
                            encoding="utf-8"
                        ).splitlines()
                    ]
                    preflight = outcome["final_launch_preflight"]
                    if attack == "extra_key":
                        preflight["unexpected"] = True
                    elif attack == "missing_gpu_field":
                        del preflight["gpu"]["pci_bus_id"]
                    elif attack == "wrong_gpu_index":
                        preflight["gpu"]["index"] = 5
                    elif attack == "wrong_gpu_uuid":
                        preflight["gpu"]["uuid"] = _gpu_uuid(5)
                    elif attack == "nonempty_compute_processes":
                        preflight["compute_processes"] = [
                            {
                                "gpu_uuid": _gpu_uuid(0),
                                "pid": 999,
                                "used_gpu_memory_mib": 10,
                                "process_name": "intruder",
                            }
                        ]
                    elif attack == "malformed_mps_record":
                        preflight["mps_processes"] = [{"pid": 1}]
                    elif attack == "integer_horizon":
                        preflight["required_horizon_s"] = 0
                    elif attack == "nonzero_horizon":
                        preflight["required_horizon_s"] = 1.0
                    elif attack == "required_until_drift":
                        preflight["required_until_utc"] = (
                            "2026-01-01T00:00:01.400000Z"
                        )
                    elif attack == "noncanonical_timestamp":
                        preflight["captured_at_utc"] = (
                            "2026-01-01T00:00:00.400000+00:00"
                        )
                        preflight["required_until_utc"] = (
                            "2026-01-01T00:00:00.400000+00:00"
                        )
                    elif attack == "extra_check":
                        preflight["checks"]["unexpected_check"] = True
                    elif attack == "missing_check":
                        del preflight["checks"]["gpu_uuid_matches_held_lease"]
                    elif attack == "nonempty_errors":
                        preflight["errors"] = ["query_gpu: transient"]
                    outcome["final_launch_preflight"] = preflight
                    events[3]["payload"] = preflight
                    events[4]["payload"]["final_launch_preflight"] = (
                        preflight
                    )
                    if attack == "started_event_mismatch":
                        divergent = json.loads(canonical_json(preflight))
                        divergent["captured_at_utc"] = (
                            "2026-01-01T00:00:00.401000Z"
                        )
                        divergent["required_until_utc"] = (
                            "2026-01-01T00:00:00.401000Z"
                        )
                        events[4]["payload"]["final_launch_preflight"] = (
                            divergent
                        )
                    events[-1]["payload"] = outcome
                    write_json_atomic(outcome_path, outcome)
                    events_path.write_text(
                        "".join(
                            canonical_json(item) + "\n" for item in events
                        ),
                        encoding="utf-8",
                    )
                    spec_path = root / "spec.json"
                    write_json_atomic(
                        spec_path,
                        _spec_v2(
                            selected=[0],
                            trials=[0],
                            rejections=[rejection.name],
                        ),
                    )
                    report = aggregate_from_spec(runs, spec_path)
                    self.assertFalse(
                        report["cells"][0]["runs"][0]["valid"],
                        report["cells"][0]["runs"][0]["validation_errors"],
                    )
                    self.assertFalse(
                        report["selected_subset"]["accepted"]
                    )

    def test_v2_preflight_and_post_health_contracts_are_exact(self) -> None:
        attacks = (
            "environment_gpu_not_exact",
            "environment_compute_processes_present",
            "environment_mps_record_malformed",
            "environment_libcuda_check_false",
            "environment_libcuda_check_missing",
            "environment_mps_bypass_changed",
            "preflight_event_environment_drift",
            "post_health_error_present",
            "post_health_check_missing",
            "post_health_gpu_uuid_wrong",
            "post_health_compute_processes_present",
            "post_health_reservation_missing",
            "post_health_reservation_required",
        )
        for attack in attacks:
            with self.subTest(attack=attack):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    runs = root / "runs"
                    runs.mkdir()
                    directory = _write_baseline_v2(runs, gpu=0, trial=0)
                    rejection = _write_rejection_v2(runs)
                    manifest_path = directory / "manifest.json"
                    manifest = json.loads(
                        manifest_path.read_text(encoding="utf-8")
                    )
                    outcome_path = directory / "outcome.json"
                    outcome = json.loads(
                        outcome_path.read_text(encoding="utf-8")
                    )
                    events_path = directory / "events.jsonl"
                    events = [
                        json.loads(line)
                        for line in events_path.read_text(
                            encoding="utf-8"
                        ).splitlines()
                    ]
                    environment = manifest["environment"]
                    preflight_payload = events[0]["payload"]
                    if attack == "environment_gpu_not_exact":
                        del environment["selected_gpu_launch_preflight"][
                            "driver_version"
                        ]
                        del preflight_payload["gpu_launch"]["driver_version"]
                    elif attack == "environment_compute_processes_present":
                        intruder = [
                            {
                                "gpu_uuid": _gpu_uuid(0),
                                "pid": 999,
                                "used_gpu_memory_mib": 10,
                                "process_name": "intruder",
                            }
                        ]
                        environment[
                            "selected_gpu_compute_processes_initial"
                        ] = intruder
                        preflight_payload["compute_processes_initial"] = (
                            intruder
                        )
                    elif attack == "environment_mps_record_malformed":
                        environment["host_mps_processes_launch"] = [
                            {"pid": 0, "command": "", "arguments": ""}
                        ]
                        preflight_payload["mps_processes_launch"] = (
                            environment["host_mps_processes_launch"]
                        )
                    elif attack == "environment_libcuda_check_false":
                        environment[
                            "runtime_libcuda_build_binding_checks"
                        ]["runtime_libcuda_link_path_is_fixed"] = False
                        preflight_payload[
                            "runtime_libcuda_build_binding_checks"
                        ] = environment[
                            "runtime_libcuda_build_binding_checks"
                        ]
                    elif attack == "environment_libcuda_check_missing":
                        del environment[
                            "runtime_libcuda_build_binding_checks"
                        ]["runtime_libcuda_link_path_is_fixed"]
                        preflight_payload[
                            "runtime_libcuda_build_binding_checks"
                        ] = environment[
                            "runtime_libcuda_build_binding_checks"
                        ]
                    elif attack == "environment_mps_bypass_changed":
                        environment["mps_bypass"][
                            "CUDA_MPS_PIPE_DIRECTORY"
                        ] = "/tmp/mps"
                    elif attack == "preflight_event_environment_drift":
                        preflight_payload["gpu_initial"] = _gpu_record(5)
                    elif attack == "post_health_error_present":
                        outcome["post_health"]["error"] = "transient"
                    elif attack == "post_health_check_missing":
                        del outcome["post_health"]["checks"][
                            "reservation_valid_at_gpu_safety_end"
                        ]
                    elif attack == "post_health_gpu_uuid_wrong":
                        outcome["post_health"]["gpu"]["uuid"] = _gpu_uuid(5)
                    elif attack == "post_health_compute_processes_present":
                        outcome["post_health"]["compute_processes"] = [
                            {
                                "gpu_uuid": _gpu_uuid(0),
                                "pid": 999,
                                "used_gpu_memory_mib": 10,
                                "process_name": "intruder",
                            }
                        ]
                    elif attack == "post_health_reservation_missing":
                        del outcome["post_health"]["reservation_revalidation"]
                    else:
                        outcome["post_health"]["reservation_revalidation"][
                            "required_for_mode"
                        ] = True
                    events[-1]["payload"] = outcome
                    write_json_atomic(manifest_path, manifest)
                    write_json_atomic(outcome_path, outcome)
                    events_path.write_text(
                        "".join(
                            canonical_json(item) + "\n" for item in events
                        ),
                        encoding="utf-8",
                    )
                    spec_path = root / "spec.json"
                    write_json_atomic(
                        spec_path,
                        _spec_v2(
                            selected=[0],
                            trials=[0],
                            rejections=[rejection.name],
                        ),
                    )
                    report = aggregate_from_spec(runs, spec_path)
                    self.assertFalse(
                        report["cells"][0]["runs"][0]["valid"],
                        report["cells"][0]["runs"][0]["validation_errors"],
                    )
                    self.assertFalse(
                        report["selected_subset"]["accepted"]
                    )

    def test_v2_sealed_rejection_accepts_a_fully_closed_manifest(self) -> None:
        """A closed manifest fails every masked prerequisite, not just two."""

        closed_policy = {
            "synthetic_manifest_check": True,
            "masked_experiment_promoted": False,
            "masked_mode_approved": False,
            "masked_gpu_is_reserved": False,
            "masked_gpu_has_current_reservation_evidence": False,
            "masked_reservation_covers_required_horizon": False,
            "masked_reservation_gpu_uuid_exact": False,
            "masked_reservation_identity_recorded": False,
            "masked_reservation_interval_ordered": False,
            "masked_reservation_not_expired": False,
            "masked_reservation_physical_gpu_exact": False,
            "masked_reservation_schema_exact": False,
            "masked_reservation_started": False,
            "masked_reservation_status_active": False,
            "masked_reservation_valid_from_parseable": False,
            "masked_reservation_valid_until_parseable": False,
            "masked_xid_library_hash_is_pinned": False,
            "masked_xid_library_path_is_absolute": False,
            "masked_xid_library_version_is_pinned": False,
            "masked_xid_monitoring_is_available": False,
            "masked_xid_monitoring_method_is_exact": False,
        }
        promoted_policy = {
            "synthetic_manifest_check": True,
            "masked_experiment_promoted": True,
            "masked_mode_approved": True,
            "masked_gpu_is_reserved": False,
        }
        unrelated_failure_policy = {
            "synthetic_manifest_check": False,
            "masked_experiment_promoted": False,
            "masked_mode_approved": False,
        }
        cases = (
            ("closed-manifest", closed_policy, True, None),
            (
                "not-stopped-by-promotion-lock",
                promoted_policy,
                False,
                "was not stopped by the promotion lock",
            ),
            (
                "non-authorization-failure",
                unrelated_failure_policy,
                False,
                "non-authorization false checks",
            ),
        )
        for name, policy, expected, expected_error in cases:
            with self.subTest(case=name):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    runs = root / "runs"
                    runs.mkdir()
                    _write_baseline_v2(runs, gpu=0, trial=0)
                    rejection = _write_rejection_v2(runs)
                    outcome_path = rejection / "outcome.json"
                    outcome = json.loads(
                        outcome_path.read_text(encoding="utf-8")
                    )
                    outcome["manifest_policy"] = policy
                    write_json_atomic(outcome_path, outcome)
                    events_path = rejection / "events.jsonl"
                    events = [
                        json.loads(line)
                        for line in events_path.read_text(
                            encoding="utf-8"
                        ).splitlines()
                    ]
                    events[-1]["payload"] = outcome
                    events_path.write_text(
                        "".join(
                            canonical_json(item) + "\n" for item in events
                        ),
                        encoding="utf-8",
                    )
                    spec_path = root / "spec.json"
                    write_json_atomic(
                        spec_path,
                        _spec_v2(
                            selected=[0],
                            trials=[0],
                            rejections=[rejection.name],
                        ),
                    )
                    report = aggregate_from_spec(runs, spec_path)
                    record = report["sealed_rejections"]["runs"][0]
                    self.assertEqual(
                        record["valid"],
                        expected,
                        record["validation_errors"],
                    )
                    self.assertEqual(
                        report["selected_subset"]["accepted"], expected
                    )
                    if expected_error is not None:
                        self.assertTrue(
                            any(
                                expected_error in item
                                for item in record["validation_errors"]
                            ),
                            record["validation_errors"],
                        )

    def test_v2_requires_a_complete_undisturbed_board_identity(self) -> None:
        """Board identity is bound so a profile cannot cross boards blind."""

        def drop_key(identity: dict) -> None:
            del identity["vbios_version"]

        def extra_key(identity: dict) -> None:
            identity["overclocked"] = True

        def raised_power_ceiling(identity: dict) -> None:
            identity["power_limit_w"] = 479.0

        def lowered_power_limit(identity: dict) -> None:
            identity["power_limit_w"] = 300.0

        def integer_power_limit(identity: dict) -> None:
            identity["power_limit_w"] = 450
            identity["power_default_limit_w"] = 450

        def blank_vbios(identity: dict) -> None:
            identity["vbios_version"] = ""

        def boolean_numa(identity: dict) -> None:
            identity["numa_node"] = False

        attacks = {
            "missing_field": drop_key,
            "unknown_field": extra_key,
            "power_limit_raised_above_default": raised_power_ceiling,
            "power_limit_lowered_below_default": lowered_power_limit,
            "power_limit_as_integer": integer_power_limit,
            "blank_vbios": blank_vbios,
            "boolean_numa_node": boolean_numa,
        }
        for name, mutate in attacks.items():
            with self.subTest(attack=name):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    runs = root / "runs"
                    runs.mkdir()
                    directory = _write_baseline_v2(runs, gpu=0, trial=0)
                    rejection = _write_rejection_v2(runs)
                    manifest_path = directory / "manifest.json"
                    manifest = json.loads(
                        manifest_path.read_text(encoding="utf-8")
                    )
                    identity = manifest["environment"][
                        "selected_gpu_hardware_identity"
                    ]
                    mutate(identity)
                    write_json_atomic(manifest_path, manifest)
                    events_path = directory / "events.jsonl"
                    events = [
                        json.loads(line)
                        for line in events_path.read_text(
                            encoding="utf-8"
                        ).splitlines()
                    ]
                    events[0]["payload"]["gpu_hardware_identity"] = identity
                    events_path.write_text(
                        "".join(
                            canonical_json(item) + "\n" for item in events
                        ),
                        encoding="utf-8",
                    )
                    spec_path = root / "spec.json"
                    write_json_atomic(
                        spec_path,
                        _spec_v2(
                            selected=[0],
                            trials=[0],
                            rejections=[rejection.name],
                        ),
                    )
                    report = aggregate_from_spec(runs, spec_path)
                    self.assertFalse(
                        report["cells"][0]["runs"][0]["valid"],
                        report["cells"][0]["runs"][0]["validation_errors"],
                    )
                    self.assertFalse(
                        report["selected_subset"]["accepted"]
                    )

        # The run.preflight event copy must agree with the manifest.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / "runs"
            runs.mkdir()
            directory = _write_baseline_v2(runs, gpu=0, trial=0)
            rejection = _write_rejection_v2(runs)
            events_path = directory / "events.jsonl"
            events = [
                json.loads(line)
                for line in events_path.read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            events[0]["payload"]["gpu_hardware_identity"] = (
                _gpu_hardware_identity(5)
            )
            events_path.write_text(
                "".join(canonical_json(item) + "\n" for item in events),
                encoding="utf-8",
            )
            spec_path = root / "spec.json"
            write_json_atomic(
                spec_path,
                _spec_v2(
                    selected=[0],
                    trials=[0],
                    rejections=[rejection.name],
                ),
            )
            report = aggregate_from_spec(runs, spec_path)
            self.assertFalse(report["cells"][0]["runs"][0]["valid"])

    def test_v2_fleet_scope_is_a_floor_and_v1_keeps_the_old_scope(
        self,
    ) -> None:
        """The v2 programme requires five 4090s; v1 still reports eight."""

        selected = [1, 2, 3, 4, 7]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / "runs"
            runs.mkdir()
            for gpu in selected:
                for trial in range(3):
                    _write_baseline_v2(runs, gpu=gpu, trial=trial)
            rejection = _write_rejection_v2(runs)
            spec_path = root / "spec.json"
            spec = _spec_v2(
                selected=selected,
                trials=[0, 1, 2],
                rejections=[rejection.name],
            )
            spec["declared_gpus"] = [
                {"physical_gpu": index, "gpu_uuid": _gpu_uuid(index)}
                for index in selected
            ]
            write_json_atomic(spec_path, spec)

            report = aggregate_from_spec(runs, spec_path)

            self.assertEqual(report["gate_a0"]["required_gpu_count"], 5)
            self.assertEqual(report["gate_a0"]["declared_gpu_count"], 5)
            self.assertTrue(report["selected_subset"]["accepted"])
            self.assertTrue(report["gate_a0"]["complete"])
            self.assertTrue(report["gate_a0"]["v2_matrix_shape_valid"])

        # Fewer than the floor is still incomplete.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / "runs"
            runs.mkdir()
            for gpu in (1, 2, 3, 4):
                for trial in range(3):
                    _write_baseline_v2(runs, gpu=gpu, trial=trial)
            rejection = _write_rejection_v2(runs)
            spec_path = root / "spec.json"
            spec = _spec_v2(
                selected=[1, 2, 3, 4],
                trials=[0, 1, 2],
                rejections=[rejection.name],
            )
            spec["declared_gpus"] = [
                {"physical_gpu": index, "gpu_uuid": _gpu_uuid(index)}
                for index in (1, 2, 3, 4)
            ]
            write_json_atomic(spec_path, spec)

            report = aggregate_from_spec(runs, spec_path)

            self.assertEqual(report["gate_a0"]["declared_gpu_count"], 4)
            self.assertTrue(report["selected_subset"]["accepted"])
            self.assertFalse(
                report["gate_a0"]["complete"],
                "four declared GPUs are below the five-card floor",
            )

    def test_v2_sealed_rejection_supports_every_masked_mode(self) -> None:
        """Stream-mode locks are authorization gates without the prefix."""

        for mode in ("global", "next", "stream"):
            with self.subTest(mode=mode):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    runs = root / "runs"
                    runs.mkdir()
                    _write_baseline_v2(runs, gpu=0, trial=0)
                    rejection = _write_rejection_v2(runs, mode=mode)
                    spec_path = root / "spec.json"
                    write_json_atomic(
                        spec_path,
                        _spec_v2(
                            selected=[0],
                            trials=[0],
                            rejections=[rejection.name],
                        ),
                    )
                    report = aggregate_from_spec(runs, spec_path)
                    record = report["sealed_rejections"]["runs"][0]
                    self.assertTrue(
                        record["valid"], record["validation_errors"]
                    )
                    self.assertTrue(
                        report["selected_subset"]["accepted"]
                    )

        # A failure genuinely outside the authorization family still
        # disqualifies the rejection as promotion-lock evidence.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / "runs"
            runs.mkdir()
            _write_baseline_v2(runs, gpu=0, trial=0)
            policy = _closed_manifest_policy("stream")
            policy["stream_mask_off_candidates_are_valid_unique_integers"] = (
                False
            )
            rejection = _write_rejection_v2(
                runs,
                mode="stream",
                manifest_policy=policy,
            )
            spec_path = root / "spec.json"
            write_json_atomic(
                spec_path,
                _spec_v2(
                    selected=[0],
                    trials=[0],
                    rejections=[rejection.name],
                ),
            )
            report = aggregate_from_spec(runs, spec_path)
            record = report["sealed_rejections"]["runs"][0]
            self.assertFalse(record["valid"])
            self.assertTrue(
                any(
                    "non-authorization false checks" in item
                    for item in record["validation_errors"]
                ),
                record["validation_errors"],
            )

    def test_v2_sealed_rejection_accepts_the_explicit_driver_opt_in(
        self,
    ) -> None:
        """The strongest rejection opts into the unpinned driver explicitly.

        On a driver outside libsmctrl's validated table a masked request is
        refused twice over, so a rejection that did not opt in proves only
        that the driver guard fired.  Opting in leaves the checked-in
        promotion manifest as the only thing that can refuse, and the
        producer appends --allow-unsupported-driver in exactly that case.
        """

        cases = (
            ("opted-in", True, None, True),
            ("not-opted-in", False, None, True),
            ("flag-without-config", False, "add", False),
            ("config-without-flag", True, "remove", False),
        )
        for name, declared, tamper, expected in cases:
            with self.subTest(case=name):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    runs = root / "runs"
                    runs.mkdir()
                    _write_baseline_v2(runs, gpu=0, trial=0)
                    # The run ID is derived from the manifest, so the opt-in
                    # is built into the fixture rather than patched in.
                    rejection = _write_rejection_v2(
                        runs,
                        experimental_allow_unsupported_driver=declared,
                    )

                    if tamper is not None:
                        command_path = rejection / "command.json"
                        command = json.loads(
                            command_path.read_text(encoding="utf-8")
                        )
                        if tamper == "add":
                            command["argv"] = [
                                *command["argv"],
                                "--allow-unsupported-driver",
                            ]
                        else:
                            command["argv"] = [
                                item
                                for item in command["argv"]
                                if item != "--allow-unsupported-driver"
                            ]
                        write_json_atomic(command_path, command)

                    spec_path = root / "spec.json"
                    write_json_atomic(
                        spec_path,
                        _spec_v2(
                            selected=[0],
                            trials=[0],
                            rejections=[rejection.name],
                        ),
                    )
                    report = aggregate_from_spec(runs, spec_path)
                    record = report["sealed_rejections"]["runs"][0]
                    self.assertEqual(
                        record["valid"],
                        expected,
                        record["validation_errors"],
                    )
                    if not expected:
                        self.assertIn(
                            "sealed rejection command argv is not exact",
                            record["validation_errors"],
                        )

    def test_v2_child_environment_and_launcher_policies_are_validated(
        self,
    ) -> None:
        """The empty-MPS-pipe bypass must be proven from the child env."""

        def poison_mps(document: dict) -> None:
            document["environment_overrides"][
                "CUDA_MPS_PIPE_DIRECTORY"
            ] = "/tmp/attacker-mps-pipe"

        def poison_visible_devices(document: dict) -> None:
            document["environment_overrides"][
                "CUDA_VISIBLE_DEVICES"
            ] = _gpu_uuid(5)

        def inject_mask_off(document: dict) -> None:
            document["environment_overrides"]["MASK_OFF"] = "1"

        def drop_environment(document: dict) -> None:
            del document["environment_overrides"]

        def claim_loader_variables(document: dict) -> None:
            document["dynamic_loader_policy"][
                "loader_and_cuda_tuning_variables_absent"
            ] = False

        def inherit_loader_variable(document: dict) -> None:
            document["environment_policy"]["inherited_names"] = ["LD_PRELOAD"]

        def keep_child_signal_mask(document: dict) -> None:
            document["signal_mask_policy"][
                "child_mask_reset_before_exec"
            ] = False

        def drop_parent_guard(document: dict) -> None:
            document["parent_death_protection"]["mechanism"] = "none"

        def stale_allowlist(document: dict) -> None:
            document["environment_policy"]["allowed_names"] = ["LANG"]

        attacks = {
            "attacker_mps_pipe_directory": poison_mps,
            "wrong_visible_devices": poison_visible_devices,
            "mask_off_injected": inject_mask_off,
            "environment_overrides_missing": drop_environment,
            "loader_variables_claimed_present": claim_loader_variables,
            "loader_variable_inherited": inherit_loader_variable,
            "child_signal_mask_not_reset": keep_child_signal_mask,
            "parent_guard_mechanism_changed": drop_parent_guard,
            "allowlist_does_not_match_environment": stale_allowlist,
        }
        for name, mutate in attacks.items():
            with self.subTest(attack=name):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    runs = root / "runs"
                    runs.mkdir()
                    directory = _write_baseline_v2(runs, gpu=0, trial=0)
                    rejection = _write_rejection_v2(runs)
                    command_path = directory / "command.json"
                    command = json.loads(
                        command_path.read_text(encoding="utf-8")
                    )
                    mutate(command)
                    write_json_atomic(command_path, command)
                    spec_path = root / "spec.json"
                    write_json_atomic(
                        spec_path,
                        _spec_v2(
                            selected=[0],
                            trials=[0],
                            rejections=[rejection.name],
                        ),
                    )
                    report = aggregate_from_spec(runs, spec_path)
                    self.assertFalse(
                        report["cells"][0]["runs"][0]["valid"],
                        report["cells"][0]["runs"][0]["validation_errors"],
                    )
                    self.assertFalse(
                        report["selected_subset"]["accepted"]
                    )

    def test_v2_remaining_scalar_substitutions_are_rejected(self) -> None:
        """Container and identity comparisons must also be type-exact."""

        revalidation_flags = (
            "canonical_paths_selected",
            "formal_source_launch_permitted",
            "snapshot_matches_initial",
            "passed_for_launch",
            "passed_for_local_acceptance",
        )

        def source_flags_as_integers(
            manifest: dict, outcome: dict, command: dict, events: list
        ) -> None:
            for phase in (
                "source_prelaunch_revalidation",
                "source_preexec_revalidation",
                "source_postrun_revalidation",
            ):
                for flag in revalidation_flags:
                    outcome[phase][flag] = 1
            for index in (1, 2, 5):
                for flag in revalidation_flags:
                    events[index]["payload"][flag] = 1

        def reservation_checks_as_integers(
            manifest: dict, outcome: dict, command: dict, events: list
        ) -> None:
            outcome["post_health"]["reservation_revalidation"]["checks"][
                "reservation_not_required_for_baseline"
            ] = 1
            outcome["launch_commit_reservation_revalidation"]["checks"][
                "reservation_not_required_for_baseline"
            ] = 1
            events[4]["payload"][
                "launch_commit_reservation_revalidation"
            ] = outcome["launch_commit_reservation_revalidation"]

        def negative_zero_horizon(
            manifest: dict, outcome: dict, command: dict, events: list
        ) -> None:
            outcome["final_launch_preflight"]["required_horizon_s"] = -0.0
            outcome["launch_commit_reservation_revalidation"][
                "required_horizon_s"
            ] = -0.0
            events[3]["payload"]["required_horizon_s"] = -0.0
            events[4]["payload"]["final_launch_preflight"][
                "required_horizon_s"
            ] = -0.0
            events[4]["payload"][
                "launch_commit_reservation_revalidation"
            ]["required_horizon_s"] = -0.0

        def libcuda_uid_as_boolean(
            manifest: dict, outcome: dict, command: dict, events: list
        ) -> None:
            def poison(identity: dict) -> None:
                identity["link_identity"]["uid"] = False
                identity["target_identity"]["uid"] = False

            poison(
                manifest["environment"]["cuda_driver_probe"][
                    "library_identity"
                ]
            )
            poison(command["cuda_driver_probe"]["library_identity"])
            for key in ("expected_identity", "observed_identity"):
                poison(command["libcuda_final_revalidation"][key])

        def driver_probe_version_as_float(
            manifest: dict, outcome: dict, command: dict, events: list
        ) -> None:
            manifest["environment"]["cuda_driver_probe"]["version"] = 13030.0
            command["cuda_driver_probe"]["version"] = 13030.0
            events[0]["payload"]["cuda_driver_probe"]["version"] = 13030.0

        def identity_inode_as_float(
            manifest: dict, outcome: dict, command: dict, events: list
        ) -> None:
            manifest["environment"]["native_binary"]["opened_fd_identity"][
                "inode"
            ] = 22.0

        def semantic_metric_as_integer(
            manifest: dict, outcome: dict, command: dict, events: list
        ) -> None:
            outcome["semantic_metrics"]["sm_coverage_ratio"] = 1

        def libcuda_inode_as_float(
            manifest: dict, outcome: dict, command: dict, events: list
        ) -> None:
            def poison(identity: dict) -> None:
                identity["target_identity"]["inode"] = 45.0
                identity["target_identity"]["nlink"] = 1.0

            poison(
                manifest["environment"]["cuda_driver_probe"][
                    "library_identity"
                ]
            )
            poison(command["cuda_driver_probe"]["library_identity"])
            for key in ("expected_identity", "observed_identity"):
                poison(command["libcuda_final_revalidation"][key])
            # Propagate into every copy so the cross-copy equality check
            # cannot mask the absolute type pin under review.
            events[0]["payload"]["cuda_driver_probe"] = command[
                "cuda_driver_probe"
            ]
            outcome["libcuda_final_revalidation"] = command[
                "libcuda_final_revalidation"
            ]
            events[4]["payload"]["libcuda_final_revalidation"] = command[
                "libcuda_final_revalidation"
            ]

        def native_build_not_found(
            manifest: dict, outcome: dict, command: dict, events: list
        ) -> None:
            manifest["environment"]["native_build"]["found"] = False

        attacks = {
            "libcuda_inode_as_float": libcuda_inode_as_float,
            "native_build_not_found": native_build_not_found,
            "source_flags_as_integers": source_flags_as_integers,
            "reservation_checks_as_integers": reservation_checks_as_integers,
            "negative_zero_horizon": negative_zero_horizon,
            "libcuda_uid_as_boolean": libcuda_uid_as_boolean,
            "driver_probe_version_as_float": driver_probe_version_as_float,
            "identity_inode_as_float": identity_inode_as_float,
            "semantic_metric_as_integer": semantic_metric_as_integer,
        }
        for name, mutate in attacks.items():
            with self.subTest(attack=name):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    runs = root / "runs"
                    runs.mkdir()
                    directory = _write_baseline_v2(runs, gpu=0, trial=0)
                    rejection = _write_rejection_v2(runs)
                    manifest_path = directory / "manifest.json"
                    outcome_path = directory / "outcome.json"
                    command_path = directory / "command.json"
                    events_path = directory / "events.jsonl"
                    manifest = json.loads(
                        manifest_path.read_text(encoding="utf-8")
                    )
                    outcome = json.loads(
                        outcome_path.read_text(encoding="utf-8")
                    )
                    command = json.loads(
                        command_path.read_text(encoding="utf-8")
                    )
                    events = [
                        json.loads(line)
                        for line in events_path.read_text(
                            encoding="utf-8"
                        ).splitlines()
                    ]
                    mutate(manifest, outcome, command, events)
                    events[-1]["payload"] = outcome
                    write_json_atomic(manifest_path, manifest)
                    write_json_atomic(outcome_path, outcome)
                    write_json_atomic(command_path, command)
                    events_path.write_text(
                        "".join(
                            canonical_json(item) + "\n" for item in events
                        ),
                        encoding="utf-8",
                    )
                    spec_path = root / "spec.json"
                    write_json_atomic(
                        spec_path,
                        _spec_v2(
                            selected=[0],
                            trials=[0],
                            rejections=[rejection.name],
                        ),
                    )
                    report = aggregate_from_spec(runs, spec_path)
                    self.assertFalse(
                        report["cells"][0]["runs"][0]["valid"],
                        report["cells"][0]["runs"][0]["validation_errors"],
                    )
                    self.assertFalse(
                        report["selected_subset"]["accepted"]
                    )

    def test_historical_v1_report_bytes_are_preserved(self) -> None:
        """The published v1 aggregate must still rebuild byte-for-byte."""

        repository = Path(__file__).resolve().parents[1]
        run_root = repository / "experiments" / "runs"
        spec_path = (
            repository
            / "experiments"
            / "manifests"
            / "gate_a0_4090_dd8c927_seed1.json"
        )
        published = (
            repository
            / "experiments"
            / "aggregates"
            / "gate_a0_4090_dd8c927_seed1_partial_20260730.json"
        )
        if not (
            run_root.is_dir()
            and spec_path.is_file()
            and published.is_file()
        ):  # pragma: no cover - depends on retained raw evidence
            self.skipTest("historical Gate-A0 raw evidence is unavailable")

        published_bytes = published.read_bytes()
        # The directory existing is not the precondition; the runs this
        # aggregate covers being in it is. experiments/runs is untracked, so
        # a checkout on another host has a populated directory holding
        # entirely different runs -- and the test then rebuilt a different
        # aggregate and reported a digest mismatch, which reads as
        # reproducibility having broken rather than as evidence being absent.
        covered = {
            entry["run_id"]
            for entry in json.loads(published_bytes)["aggregate_inputs"]["runs"]
        }
        absent = sorted(r for r in covered if not (run_root / r).is_dir())
        if absent:  # pragma: no cover - depends on retained raw evidence
            self.skipTest(
                f"{len(absent)} of {len(covered)} runs this aggregate covers "
                f"are not in {run_root}"
            )
        # The published artifact itself is immutable evidence.
        self.assertEqual(
            hashlib.sha256(published_bytes).hexdigest(),
            "f35164ab85648e51525c88d214897f74d5736c6807a7a37ad57f8459a46b"
            "22bf",
        )

        report = aggregate_from_spec(run_root, spec_path)

        self.assertEqual(
            report["schema_version"],
            "burstserve.gate-a0-evidence-report/v1",
        )
        # aggregate_input_sha256 binds the evidence itself: the spec digest
        # and the per-file hashes of every cell, exclusion and rejection the
        # spec selects.  It must never drift.
        self.assertEqual(
            report["aggregate_input_sha256"],
            "08ef4053d9d5931c42b3f0199ddcf036b7501f55fc9f34c2ccfda56c42b0"
            "a188",
        )
        self.assertFalse(report["gate_a0"]["complete"])

        # Everything except the raw-directory scan count must still rebuild
        # byte-for-byte.  That count reports how many run directories exist
        # locally, and retained raw evidence only ever grows it, so it is not
        # a stable property of the v1 report contract.
        original = json.loads(published_bytes)
        regenerated = json.loads(canonical_json(report))
        self.assertGreaterEqual(
            regenerated["scan"].pop("discovered_run_directories"),
            original["scan"].pop("discovered_run_directories"),
        )
        self.assertEqual(regenerated, original)
        self.assertEqual(
            canonical_json(regenerated),
            canonical_json(original),
        )


if __name__ == "__main__":
    unittest.main()


_MASKED_MATRIX = {
    "modes": ["global", "next", "stream"],
    "tpc_bits": [0, 31, 32, 63],
    "trials_per_cell": 3,
    "allowed_observed_sm_count": [1, 2],
    "iterations": 4096,
    "blocks": 4096,
    "threads_per_block": 256,
}
_MASKED_HARDWARE = {"sm_count": 128, "expected_tpc_count": 64}
# A TPC holds two SMs on this die, so bit b denotes {2b, 2b+1}.
_TRUE_MAPPING = {bit: [2 * bit, 2 * bit + 1] for bit in (0, 31, 32, 63)}


def _masked_observations(mapping=None, **overrides):
    mapping = mapping or _TRUE_MAPPING
    rows = []
    for mode in _MASKED_MATRIX["modes"]:
        for bit in _MASKED_MATRIX["tpc_bits"]:
            for trial in range(_MASKED_MATRIX["trials_per_cell"]):
                rows.append(
                    {
                        "mode": mode,
                        "tpc_bit": bit,
                        "trial": trial,
                        "physical_gpu": 1,
                        "gpu_uuid": _gpu_uuid(1),
                        "blocks": 4096,
                        "observed_blocks": 4096,
                        "observed_sms": list(mapping[bit]),
                    }
                )
    for key, value in overrides.items():
        del key, value
    return rows


class MaskedTpcMatrixTest(unittest.TestCase):
    def validate(self, observations, **kwargs):
        # The unmasked baseline is the full die observed on the same card, so
        # every masked set must be a proper subset of it.
        kwargs.setdefault("baseline_observed_sm_count", 128)
        kwargs.setdefault("baseline_observed_sms", list(range(128)))
        kwargs.setdefault("baseline_gpu_uuid", _gpu_uuid(1))
        return validate_masked_tpc_matrix(
            observations,
            matrix=_MASKED_MATRIX,
            hardware=_MASKED_HARDWARE,
            **kwargs,
        )

    def test_degenerate_cardinalities_are_refused(self):
        """Every cross-sectional check is vacuous at a cardinality of one.

        With one mode there is no mechanism to agree with; with one bit the
        pairwise disjointness loop never runs; with one trial determinism is
        trivially true.  Such a matrix would report a mapping confirmed only
        by the observation that produced it, which is not weak evidence but
        none, so the shape itself must be refused.
        """

        for label, modes, bits, trials in (
            ("one mode", ["global"], [0, 31, 32, 63], 3),
            ("one bit", ["global", "next"], [0], 3),
            ("one trial", ["global", "next"], [0, 31, 32, 63], 1),
            ("all singular", ["global"], [0], 1),
        ):
            with self.subTest(label):
                matrix = dict(_MASKED_MATRIX)
                matrix.update(
                    modes=modes, tpc_bits=bits, trials_per_cell=trials
                )
                rows = [
                    {
                        "mode": mode,
                        "tpc_bit": bit,
                        "trial": trial,
                        "physical_gpu": 1,
                        "gpu_uuid": _gpu_uuid(1),
                        "blocks": 4096,
                        "observed_blocks": 4096,
                        "observed_sms": [2 * bit, 2 * bit + 1],
                    }
                    for mode in modes
                    for bit in bits
                    for trial in range(trials)
                ]
                report = validate_masked_tpc_matrix(
                    rows,
                    matrix=matrix,
                    hardware=_MASKED_HARDWARE,
                    baseline_observed_sm_count=128,
                    baseline_observed_sms=list(range(128)),
                    baseline_gpu_uuid=_gpu_uuid(1),
                )
                self.assertFalse(report["accepted"])
                self.assertFalse(
                    report["checks"]["matrix_declaration_well_formed"]
                )

    def test_restriction_requires_containment_in_the_same_card_baseline(self):
        """A smaller SM set is not evidence that the mask restricted.

        Comparing cardinalities alone accepts a mask that moved the kernel to
        SMs the unmasked run never touched, and accepts a baseline integer
        that belongs to no observed card at all.
        """

        # Masked sets that are small but NOT inside the baseline's coverage.
        report = self.validate(
            _masked_observations(),
            baseline_observed_sms=[5, 6, 7],
            baseline_observed_sm_count=3,
        )
        self.assertFalse(report["accepted"])
        self.assertFalse(
            report["checks"]["restricts_relative_to_unmasked_baseline"]
        )

        # A baseline measured on a different card proves nothing about this one.
        report = self.validate(
            _masked_observations(), baseline_gpu_uuid=_gpu_uuid(4)
        )
        self.assertFalse(report["accepted"])
        self.assertFalse(report["checks"]["baseline_is_from_the_same_gpu"])

        # A supplied count that contradicts its own set is a malformed claim.
        report = self.validate(
            _masked_observations(), baseline_observed_sm_count=9999
        )
        self.assertFalse(report["accepted"])
        self.assertFalse(
            report["checks"]["restricts_relative_to_unmasked_baseline"]
        )

    def test_a_consistent_disjoint_matrix_is_accepted(self):
        report = self.validate(_masked_observations())
        self.assertTrue(report["accepted"], report["errors"])
        self.assertEqual(report["errors"], [])
        self.assertTrue(all(report["checks"].values()), report["checks"])
        self.assertEqual(
            report["tpc_sm_mapping"],
            {str(b): sorted(v) for b, v in _TRUE_MAPPING.items()},
        )
        self.assertEqual(report["declared_matrix"]["expected_cell_count"], 36)
        self.assertEqual(report["declared_matrix"]["observed_cell_count"], 36)

    def test_a_single_nondeterministic_trial_is_rejected(self):
        rows = _masked_observations()
        # one trial of one cell drifts to a neighbouring TPC's SMs
        for row in rows:
            if row["mode"] == "next" and row["tpc_bit"] == 31 and row["trial"] == 2:
                row["observed_sms"] = [64, 65]
        report = self.validate(rows)
        self.assertFalse(report["accepted"])
        self.assertFalse(report["checks"]["deterministic_across_trials"])

    def test_modes_must_agree_on_the_same_bit(self):
        rows = _masked_observations()
        for row in rows:
            if row["mode"] == "stream" and row["tpc_bit"] == 0:
                row["observed_sms"] = [10, 11]
        report = self.validate(rows)
        self.assertFalse(report["accepted"])
        self.assertFalse(report["checks"]["consistent_across_modes"])

    def test_overlapping_bits_are_rejected_as_leakage(self):
        leaky = dict(_TRUE_MAPPING)
        leaky[32] = [62, 63]  # collides with bit 31 -> {62, 63}
        report = self.validate(_masked_observations(leaky))
        self.assertFalse(report["accepted"])
        self.assertFalse(report["checks"]["disjoint_across_bits"])

    def test_a_mask_covering_the_whole_die_is_rejected(self):
        wide = {bit: list(range(128)) for bit in _TRUE_MAPPING}
        report = self.validate(_masked_observations(wide))
        self.assertFalse(report["accepted"])
        self.assertFalse(report["checks"]["observed_sm_count_within_allowed"])
        self.assertFalse(
            report["checks"]["restricts_relative_to_unmasked_baseline"]
        )

    def test_missing_baseline_coverage_cannot_be_accepted(self):
        report = validate_masked_tpc_matrix(
            _masked_observations(),
            matrix=_MASKED_MATRIX,
            hardware=_MASKED_HARDWARE,
            baseline_observed_sm_count=None,
        )
        self.assertFalse(report["accepted"])
        self.assertFalse(
            report["checks"]["restricts_relative_to_unmasked_baseline"]
        )

    def test_incomplete_and_undeclared_cells_are_rejected(self):
        short = [r for r in _masked_observations() if r["trial"] != 2]
        report = self.validate(short)
        self.assertFalse(report["accepted"])
        self.assertFalse(report["checks"]["matrix_complete"])

        extra = _masked_observations()
        extra.append({**extra[0], "tpc_bit": 7})
        report = self.validate(extra)
        self.assertFalse(report["accepted"])
        self.assertFalse(report["checks"]["no_unexpected_cells"])

    def test_duplicate_cells_are_rejected(self):
        rows = _masked_observations()
        rows.append(dict(rows[0]))
        report = self.validate(rows)
        self.assertFalse(report["accepted"])
        self.assertFalse(report["checks"]["observation_records_well_formed"])

    def test_sm_ids_outside_the_device_are_rejected(self):
        bad = dict(_TRUE_MAPPING)
        bad[63] = [126, 128]
        report = self.validate(_masked_observations(bad))
        self.assertFalse(report["accepted"])
        self.assertFalse(report["checks"]["observed_sms_within_device_range"])

    def test_a_partial_tpc_is_allowed_by_count_but_not_by_the_die(self):
        half = dict(_TRUE_MAPPING)
        half[0] = [0]
        report = self.validate(_masked_observations(half))
        # one SM is inside allowed_observed_sm_count ...
        self.assertTrue(report["checks"]["observed_sm_count_within_allowed"])
        # ... but this die has exactly two SMs per TPC, so it still fails
        self.assertFalse(report["checks"]["mapping_matches_die_sms_per_tpc"])
        self.assertFalse(report["accepted"])

    def test_mixed_gpu_identities_are_rejected(self):
        rows = _masked_observations()
        rows[-1]["gpu_uuid"] = _gpu_uuid(2)
        report = self.validate(rows)
        self.assertFalse(report["accepted"])
        self.assertFalse(report["checks"]["single_gpu_identity"])

    def test_block_totals_must_match_the_declared_matrix(self):
        for field in ("blocks", "observed_blocks"):
            with self.subTest(field=field):
                rows = _masked_observations()
                rows[5][field] = 2048
                report = self.validate(rows)
                self.assertFalse(report["accepted"])
                self.assertFalse(
                    report["checks"]["observation_records_well_formed"]
                )

    def test_type_substitutions_in_observations_are_rejected(self):
        for field, value in (
            ("tpc_bit", True),
            ("trial", False),
            ("blocks", 4096.0),
            ("observed_blocks", True),
            ("physical_gpu", 1.0),
        ):
            with self.subTest(field=field):
                rows = _masked_observations()
                rows[0][field] = value
                report = self.validate(rows)
                self.assertFalse(report["accepted"])

    def test_malformed_matrix_or_die_is_rejected(self):
        report = validate_masked_tpc_matrix(
            _masked_observations(),
            matrix={**_MASKED_MATRIX, "extra": 1},
            hardware=_MASKED_HARDWARE,
            baseline_observed_sm_count=128,
        )
        self.assertFalse(report["accepted"])
        self.assertFalse(report["checks"]["matrix_section_keys_exact"])

        report = validate_masked_tpc_matrix(
            _masked_observations(),
            matrix=_MASKED_MATRIX,
            hardware={"sm_count": 127, "expected_tpc_count": 64},
            baseline_observed_sm_count=128,
        )
        self.assertFalse(report["accepted"])
        self.assertFalse(report["checks"]["die_sms_per_tpc_is_integral"])


_PROMOTED_GATE = {
    "safety": {
        "experimental_mask_enabled": True,
        "approved_mask_modes": ["global", "next", "stream"],
    },
    "hardware": {"sm_count": 128, "expected_tpc_count": 64},
    "single_tpc_matrix_after_explicit_promotion": _MASKED_MATRIX,
}


def _masked_cell(mode="global", bit=31, trial=0):
    config = {
        "mode": mode,
        "enabled_tpc": bit,
        "trial": trial,
        "iterations": 4096,
        "blocks": 4096,
        "threads_per_block": 256,
    }
    outcome = {
        "accepted": False,
        "local_probe_passed": True,
        "requires_matrix_validation": True,
        "masked_health_monitor_status": "clean",
        "quarantine_required": False,
        "quarantine_reasons": [],
        "native_status": "ok",
        "timed_out": False,
        "masked_health_monitor": {
            "status": "clean",
            "post_probe_drain_timeout_ms": 250,
            "provenance": {"setup": {}, "drain": {}},
        },
        "masked_health_monitor_checks": _all_true(),
        "final_launch_preflight": {
            "captured_at_utc": "2026-01-01T00:00:00.400000Z",
            "required_horizon_s": 95.5,
            "required_until_utc": "2026-01-01T00:01:35.900000Z",
            "passed": True,
        },
        "launch_commit_reservation_revalidation": {
            "required_for_mode": True,
            "checks": _all_true(),
            "passed": True,
            "error": None,
        },
        "post_health": {
            "reservation_revalidation": {
                "required_for_mode": True,
                "passed": True,
            }
        },
    }
    native = {
        "parent_guard": {
            "mode": "linux_pdeathsig_sigkill",
            "status": "armed",
            "expected_parent_pid": 4242,
            "observed_parent_pid": 4242,
            "inherited_pdeath_signal": 9,
            "pdeath_signal": 9,
        },
        "requested_enabled_tpc": bit,
        "tpc_count": 64,
        "observed_histogram": {str(2 * bit): 2048, str(2 * bit + 1): 2048},
    }
    environment = {"BURSTSERVE_PARENT_PID": "4242"}
    if mode == "stream":
        environment["MASK_OFF"] = "-16"
    command = {
        "argv": [
            "/synthetic/smid_probe", "--mode", mode,
            "--enabled-tpc", str(bit),
            "--iterations", "4096", "--blocks", "4096",
        ],
        "environment_overrides": environment,
    }
    return config, outcome, native, command


class MaskedCellContractTest(unittest.TestCase):
    def check(self, config, outcome, native, command, gate=None):
        return validate_masked_cell_contract(
            config=config, outcome=outcome, native=native,
            gate_content=gate or _PROMOTED_GATE, command=command,
            expected_gpu=1, expected_uuid=_gpu_uuid(1),
        )

    def test_a_clean_masked_cell_yields_a_matrix_observation(self):
        obs, errors = self.check(*_masked_cell())
        self.assertEqual(errors, [])
        self.assertIsNotNone(obs)
        self.assertEqual(
            set(obs),
            {
                "mode", "tpc_bit", "trial", "physical_gpu",
                "gpu_uuid", "blocks", "observed_blocks",
                "observed_sms",
            },
        )
        self.assertEqual(obs["observed_sms"], [62, 63])
        self.assertEqual(obs["observed_blocks"], 4096)
        self.assertEqual(obs["tpc_bit"], 31)

    def test_an_unpromoted_manifest_cannot_produce_a_cell(self):
        gate = json.loads(json.dumps(_PROMOTED_GATE))
        gate["safety"]["experimental_mask_enabled"] = False
        obs, errors = self.check(*_masked_cell(), gate=gate)
        self.assertIsNone(obs)
        self.assertTrue(any("explicitly promoted" in e for e in errors), errors)

    def test_an_unapproved_mode_cannot_produce_a_cell(self):
        gate = json.loads(json.dumps(_PROMOTED_GATE))
        gate["safety"]["approved_mask_modes"] = ["global"]
        obs, errors = self.check(*_masked_cell(mode="stream"), gate=gate)
        self.assertIsNone(obs)
        self.assertTrue(any("not an approved" in e for e in errors), errors)

    def test_a_masked_run_claiming_acceptance_is_rejected(self):
        config, outcome, native, command = _masked_cell()
        outcome["accepted"] = True
        obs, errors = self.check(config, outcome, native, command)
        self.assertIsNone(obs)
        self.assertTrue(any("outcome.accepted" in e for e in errors), errors)

    def test_matrix_validation_may_not_be_waived(self):
        config, outcome, native, command = _masked_cell()
        outcome["requires_matrix_validation"] = False
        obs, errors = self.check(config, outcome, native, command)
        self.assertIsNone(obs)

    def test_an_unarmed_or_mismatched_parent_guard_is_rejected(self):
        for field, value in (
            ("status", "not_required"),
            ("mode", "not_required"),
            ("pdeath_signal", 15),
            ("inherited_pdeath_signal", None),
            ("observed_parent_pid", 4243),
            ("expected_parent_pid", 0),
        ):
            with self.subTest(field=field):
                config, outcome, native, command = _masked_cell()
                native["parent_guard"][field] = value
                obs, errors = self.check(config, outcome, native, command)
                self.assertIsNone(obs, errors)

    def test_a_masked_run_without_a_reservation_horizon_is_rejected(self):
        for mutate in (
            lambda o: o["final_launch_preflight"].__setitem__("required_horizon_s", 0.0),
            lambda o: o["final_launch_preflight"].__setitem__("required_horizon_s", 95),
            lambda o: o["launch_commit_reservation_revalidation"].__setitem__("required_for_mode", False),
            lambda o: o["post_health"]["reservation_revalidation"].__setitem__("required_for_mode", False),
            lambda o: o["launch_commit_reservation_revalidation"]["checks"].__setitem__("reservation_not_expired", False),
        ):
            with self.subTest(mutate=mutate):
                config, outcome, native, command = _masked_cell()
                mutate(outcome)
                obs, errors = self.check(config, outcome, native, command)
                self.assertIsNone(obs, errors)

    def test_a_dirty_health_monitor_is_rejected(self):
        for mutate in (
            lambda o: o.__setitem__("masked_health_monitor_status", "xid_observed"),
            lambda o: o["masked_health_monitor"].__setitem__("status", "monitor_failed"),
            lambda o: o["masked_health_monitor_checks"].__setitem__("drain_succeeded", False),
            lambda o: o.__setitem__("masked_health_monitor", None),
        ):
            with self.subTest(mutate=mutate):
                config, outcome, native, command = _masked_cell()
                mutate(outcome)
                obs, errors = self.check(config, outcome, native, command)
                self.assertIsNone(obs, errors)

    def test_native_must_report_the_bit_that_was_requested(self):
        config, outcome, native, command = _masked_cell(bit=31)
        native["requested_enabled_tpc"] = 32
        obs, errors = self.check(config, outcome, native, command)
        self.assertIsNone(obs)

    def test_argv_and_child_environment_must_match_the_mode(self):
        config, outcome, native, command = _masked_cell(bit=31)
        command["argv"][4] = "32"
        obs, _ = self.check(config, outcome, native, command)
        self.assertIsNone(obs)

        config, outcome, native, command = _masked_cell(mode="global")
        command["environment_overrides"]["MASK_OFF"] = "-16"
        obs, errors = self.check(config, outcome, native, command)
        self.assertIsNone(obs, errors)

        config, outcome, native, command = _masked_cell(mode="stream")
        del command["environment_overrides"]["MASK_OFF"]
        obs, errors = self.check(config, outcome, native, command)
        self.assertIsNone(obs, errors)

        config, outcome, native, command = _masked_cell()
        del command["environment_overrides"]["BURSTSERVE_PARENT_PID"]
        obs, errors = self.check(config, outcome, native, command)
        self.assertIsNone(obs, errors)

    def test_a_bit_or_trial_outside_the_matrix_is_rejected(self):
        for config_key, value in (("enabled_tpc", 7), ("trial", 3), ("blocks", 2048)):
            with self.subTest(key=config_key):
                config, outcome, native, command = _masked_cell()
                config[config_key] = value
                obs, errors = self.check(config, outcome, native, command)
                self.assertIsNone(obs, errors)

    def test_cells_feed_the_matrix_validator_end_to_end(self):
        rows = []
        for mode in _MASKED_MATRIX["modes"]:
            for bit in _MASKED_MATRIX["tpc_bits"]:
                for trial in range(_MASKED_MATRIX["trials_per_cell"]):
                    obs, errors = self.check(*_masked_cell(mode, bit, trial))
                    self.assertEqual(errors, [])
                    rows.append(obs)
        report = validate_masked_tpc_matrix(
            rows, matrix=_MASKED_MATRIX, hardware=_MASKED_HARDWARE,
            baseline_observed_sm_count=128,
            baseline_observed_sms=list(range(128)),
            baseline_gpu_uuid=_gpu_uuid(1),
        )
        self.assertTrue(report["accepted"], report["errors"])
        self.assertEqual(
            report["tpc_sm_mapping"],
            {str(b): [2 * b, 2 * b + 1] for b in (0, 31, 32, 63)},
        )
