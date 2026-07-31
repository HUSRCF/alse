from __future__ import annotations

from contextlib import ExitStack, nullcontext
from dataclasses import replace
import json
import hashlib
import importlib.util
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import burstserve.provenance as provenance
import burstserve.smctrl_runner as smctrl_runner
from burstserve.git_provenance import (
    FilesystemEntry,
    GitlinkState,
    RepositorySnapshot,
)
from burstserve.smctrl_runner import (
    NATIVE_SCHEMA_VERSION,
    NativeOutputError,
    build_child_environment,
    build_probe_command,
    evaluate_driver_policy,
    evaluate_gate_manifest_policy,
    evaluate_probe,
    execute,
    load_gate_manifest_record,
    normalize_histogram,
    parse_native_output,
    query_compute_processes,
    query_mps_processes,
    source_revision,
)
from burstserve.provenance import canonical_json


TEST_LIBSMCTRL_COMMIT = "1" * 40


def _gate_manifest(*, promoted: bool = False) -> dict[str, object]:
    content = {
        "schema_version": "burstserve.gate-a-manifest/v2",
        "manifest_id": "test-gate-a",
        "hardware": {
            "gpu_name": "GPU",
            "physical_gpu_indices": [3],
            "compute_capability": [8, 9],
            "sm_count": 128,
            "expected_tpc_count": 64,
            "driver_version": "test",
            "driver_api_version": 13030,
            "runtime_api_version": 13000,
            "toolkit_version": "test",
        },
        "source": {
            "libsmctrl_commit": TEST_LIBSMCTRL_COMMIT,
            "libsmctrl_metadata": "vendor/LIBSMCTRL_SOURCE.json",
            "approved_launcher_sha256": "a" * 64,
            "approved_real_probe_sha256": "b" * 64,
            "approved_build_stamp_sha256": "c" * 64,
            "approved_build_attestation_sha256": "d" * 64,
        },
        "safety": {
            "protocol": "test",
            "timeout_s": 10,
            "maximum_preexisting_gpu_memory_mib": 1024,
            "unknown_driver_policy": "fail-closed",
            "experimental_mask_enabled": promoted,
            "approved_mask_modes": (
                ["global", "next", "stream"] if promoted else []
            ),
            "reserved_gpu_uuids": ["uuid"] if promoted else [],
            "exclusive_reservation_evidence": {
                "schema_version": "burstserve.gpu-reservation/v1",
                "status": "active",
                "gpu_uuid": "uuid",
                "physical_gpu": 3,
                "reservation_id": "test",
                "owner": "unit-test",
                "valid_from_utc": "2026-01-01T00:00:00Z",
                "valid_until_utc": "2099-01-01T00:00:00Z",
            },
            "xid_monitoring": {
                "available": promoted,
                "method": "nvmlEventSetWait_v2_exact_xid",
                "quiet_ms": 1000,
                "library_path": "/usr/lib/libnvidia-ml.so.1",
                "library_sha256": "e" * 64,
                "library_version": "test",
            },
            "stream_offset_search_enabled": promoted,
            "stream_mask_off_candidates": [-16] if promoted else [],
            "global_next_matrix_accepted": promoted,
            "mps_allowed": False,
            "mps_bypass": "CUDA_MPS_PIPE_DIRECTORY_empty",
        },
        "baseline": {
            "blocks_per_sm": 32,
            "iterations": 100,
            "minimum_sm_coverage_fraction": 0.75,
            "trials_per_gpu": 3,
            "threads_per_block": 256,
        },
        "single_tpc_matrix_after_explicit_promotion": {
            "modes": ["global", "next", "stream"],
            "tpc_bits": [0, 31, 32, 63],
            "trials_per_cell": 3,
            "allowed_observed_sm_count": [1, 2],
            "iterations": 100,
            "blocks": 4096,
            "threads_per_block": 256,
        },
        "promotion_requirements": ["synthetic unit-test promotion"],
    }
    return {
        "path": "test-gate-a.json",
        "sha256": hashlib.sha256(
            canonical_json(content).encode("utf-8")
        ).hexdigest(),
        "content": content,
    }


def _native(
    *,
    mode: str = "baseline",
    sm_count: int = 8,
    histogram: object | None = None,
    status: str = "ok",
    enabled_tpc: int = 0,
    iterations: int = 100,
    blocks: int | None = None,
    parent_pid: int = 123,
) -> dict[str, object]:
    observed = (
        histogram
        if histogram is not None
        else {str(index): 5 for index in range(sm_count)}
    )
    inferred_blocks = (
        sum(int(value) for value in observed.values())
        if isinstance(observed, dict)
        else 1
    )
    parent_guard = (
        {
            "mode": "linux_pdeathsig_sigkill",
            "status": "armed",
            "expected_parent_pid": parent_pid,
            "observed_parent_pid": parent_pid,
            "inherited_pdeath_signal": signal.SIGKILL,
            "pdeath_signal": signal.SIGKILL,
        }
        if mode in {"global", "next", "stream"}
        else {
            "mode": "not_required",
            "status": "not_required",
            "expected_parent_pid": None,
            "observed_parent_pid": parent_pid,
            "inherited_pdeath_signal": None,
            "pdeath_signal": None,
        }
    )
    return {
        "schema_version": NATIVE_SCHEMA_VERSION,
        "status": status,
        "mode": mode,
        "driver_version": 13030,
        "runtime_version": 13000,
        "parent_guard": parent_guard,
        "device": {
            "ordinal": 0,
            "name": "Fake GPU",
            "uuid": "uuid",
            "cc_major": 8,
            "cc_minor": 9,
            "sm_count": sm_count,
        },
        "requested_enabled_tpc": enabled_tpc,
        "tpc_count": (
            64 if mode in {"global", "next", "stream"} else None
        ),
        "blocks": blocks if blocks is not None else inferred_blocks,
        "threads_per_block": 256,
        "iterations": iterations,
        "observed_histogram": observed,
    }


def _native_stdout(value: object) -> str:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=False,
        )
        + "\n"
    )


def _config(
    *,
    mode: str = "baseline",
    experimental_allow: bool = False,
    mask_off: int | None = None,
    promoted: bool = False,
    trial: int = 0,
    allow_busy: bool = False,
) -> dict[str, object]:
    return {
        "schema_version": "burstserve.smid-probe-cell/v2",
        "physical_gpu": 3,
        "mode": mode,
        "enabled_tpc": 0,
        "iterations": 100,
        "blocks": 4096,
        "threads_per_block": 256,
        "trial": trial,
        "seed": 0,
        "timeout_s": 10,
        "maximum_used_mib": 1024,
        "allow_busy_gpu": allow_busy,
        "experimental_allow_unsupported_driver": experimental_allow,
        "experimental_mask_off": mask_off,
        "gate_manifest": _gate_manifest(promoted=promoted),
    }


def _write_formal_source_fixture(
    root: Path,
    binary: Path,
    config: dict[str, object],
    *,
    libsmctrl: Path | None = None,
    include_approval_pins: bool,
    stamp_overrides: dict[str, str] | None = None,
) -> Path:
    native_source = root / "native" / "smctrl_probe"
    native_source.mkdir(parents=True, exist_ok=True)
    source_files = {
        "MAKEFILE_SHA256": native_source / "Makefile",
        "PROBE_CU_SHA256": native_source / "smid_probe.cu",
    }
    source_files["MAKEFILE_SHA256"].write_text(
        "makefile\n", encoding="utf-8"
    )
    source_files["PROBE_CU_SHA256"].write_text(
        "probe\n", encoding="utf-8"
    )
    selected_libsmctrl = libsmctrl or root / "vendor" / "libsmctrl"
    selected_libsmctrl.mkdir(parents=True, exist_ok=True)
    source_files["LIBSMCTRL_C_SHA256"] = (
        selected_libsmctrl / "libsmctrl.c"
    )
    source_files["LIBSMCTRL_H_SHA256"] = (
        selected_libsmctrl / "libsmctrl.h"
    )
    source_files["LIBSMCTRL_C_SHA256"].write_text(
        "source\n", encoding="utf-8"
    )
    source_files["LIBSMCTRL_H_SHA256"].write_text(
        "header\n", encoding="utf-8"
    )
    stamp_fields = {
        key: hashlib.sha256(path.read_bytes()).hexdigest()
        for key, path in source_files.items()
    }
    stamp_fields.update(
        {
            "LIBSMCTRL_DIR": str(selected_libsmctrl.resolve()),
            "LIBSMCTRL_GIT_COMMIT": TEST_LIBSMCTRL_COMMIT,
            "LIBSMCTRL_GIT_DIRTY": "clean",
            "LIBSMCTRL_GIT_STATUS_SHA256": hashlib.sha256(b"").hexdigest(),
        }
    )
    stamp_fields.update(stamp_overrides or {})
    stamp = binary.parent / "build-config.stamp"
    stamp.write_text(
        "".join(f"{key}={value}\n" for key, value in stamp_fields.items()),
        encoding="utf-8",
    )
    if include_approval_pins:
        gate_manifest = config["gate_manifest"]
        assert isinstance(gate_manifest, dict)
        content = gate_manifest["content"]
        assert isinstance(content, dict)
        source = content["source"]
        assert isinstance(source, dict)
        source.update(
            {
                "approved_native_binary_sha256": hashlib.sha256(
                    binary.read_bytes()
                ).hexdigest(),
                "approved_build_stamp_sha256": hashlib.sha256(
                    stamp.read_bytes()
                ).hexdigest(),
            }
        )
    return selected_libsmctrl


class DriverPolicyTest(unittest.TestCase):
    def test_cuda_13_masked_run_fails_closed(self) -> None:
        checks, allowed = evaluate_driver_policy(
            mode="global",
            driver_version=13030,
            latest_pinned_version=12080,
            experimental_allow_unsupported_driver=False,
            experimental_mask_off=None,
        )
        self.assertFalse(allowed)
        self.assertFalse(
            checks["driver_is_pinned_or_explicitly_allowed"]
        )

    def test_unknown_intermediate_driver_also_fails_closed(self) -> None:
        _, allowed = evaluate_driver_policy(
            mode="next",
            driver_version=12015,
            latest_pinned_version=12080,
            experimental_allow_unsupported_driver=False,
            experimental_mask_off=None,
        )
        self.assertFalse(allowed)

    def test_cuda_13_stream_requires_allow_and_mask_off(self) -> None:
        _, allowed_without_offset = evaluate_driver_policy(
            mode="stream",
            driver_version=13030,
            latest_pinned_version=12080,
            experimental_allow_unsupported_driver=True,
            experimental_mask_off=None,
        )
        checks, allowed = evaluate_driver_policy(
            mode="stream",
            driver_version=13030,
            latest_pinned_version=12080,
            experimental_allow_unsupported_driver=True,
            experimental_mask_off=-16,
        )
        self.assertFalse(allowed_without_offset)
        self.assertTrue(allowed)
        self.assertTrue(all(checks.values()))

    def test_baseline_is_safe_on_new_driver(self) -> None:
        _, allowed = evaluate_driver_policy(
            mode="baseline",
            driver_version=13030,
            latest_pinned_version=12080,
            experimental_allow_unsupported_driver=False,
            experimental_mask_off=None,
        )
        self.assertTrue(allowed)

    def test_gate_manifest_seals_stream_offset_until_promoted(self) -> None:
        gpu = {"name": "GPU", "uuid": "uuid"}
        disabled = _gate_manifest()["content"]
        _, allowed = evaluate_gate_manifest_policy(
            disabled,
            mode="stream",
            physical_gpu=3,
            gpu=gpu,
            driver_version=13030,
            experimental_mask_off=-16,
            timeout_s=10,
            maximum_used_mib=1024,
            iterations=100,
            blocks=4096,
            threads_per_block=256,
            trial=0,
            enabled_tpc=0,
        )
        self.assertFalse(allowed)

        promoted = _gate_manifest(promoted=True)["content"]
        with mock.patch(
            "burstserve.smctrl_runner.MASKED_HEALTH_MONITOR_IMPLEMENTED",
            True,
        ):
            checks, allowed = evaluate_gate_manifest_policy(
                promoted,
                mode="stream",
                physical_gpu=3,
                gpu=gpu,
                driver_version=13030,
                experimental_mask_off=-16,
                timeout_s=10,
                maximum_used_mib=1024,
                iterations=100,
                blocks=4096,
                threads_per_block=256,
                trial=0,
                enabled_tpc=0,
            )
        self.assertTrue(allowed)
        self.assertTrue(all(checks.values()))
        with mock.patch(
            "burstserve.smctrl_runner.MASKED_HEALTH_MONITOR_IMPLEMENTED",
            True,
        ):
            _, undeclared_allowed = evaluate_gate_manifest_policy(
                promoted,
                mode="stream",
                physical_gpu=3,
                gpu=gpu,
                driver_version=13030,
                experimental_mask_off=-8,
                timeout_s=10,
                maximum_used_mib=1024,
                iterations=100,
                blocks=4096,
                threads_per_block=256,
                trial=0,
                enabled_tpc=0,
            )
        self.assertFalse(undeclared_allowed)

    def test_masked_matrix_rejects_unregistered_tpc_trial_and_duplicates(
        self,
    ) -> None:
        gpu = {"name": "GPU", "uuid": "uuid"}
        base = _gate_manifest(promoted=True)["content"]
        cases = (
            ("unregistered_tpc", 7, 0, None),
            ("unregistered_trial", 0, 3, None),
            ("duplicate_tpc", 0, 0, [0, 0]),
        )
        for name, enabled_tpc, trial, replacement_bits in cases:
            with self.subTest(name=name):
                manifest = json.loads(json.dumps(base))
                if replacement_bits is not None:
                    manifest[
                        "single_tpc_matrix_after_explicit_promotion"
                    ]["tpc_bits"] = replacement_bits
                checks, allowed = evaluate_gate_manifest_policy(
                    manifest,
                    mode="global",
                    physical_gpu=3,
                    gpu=gpu,
                    driver_version=13030,
                    experimental_mask_off=None,
                    timeout_s=10,
                    maximum_used_mib=1024,
                    iterations=100,
                    blocks=4096,
                    threads_per_block=256,
                    trial=trial,
                    enabled_tpc=enabled_tpc,
                )
                self.assertFalse(allowed)
                if name == "unregistered_tpc":
                    self.assertFalse(
                        checks["masked_enabled_tpc_is_registered"]
                    )
                elif name == "unregistered_trial":
                    self.assertFalse(
                        checks["masked_trial_is_registered"]
                    )
                else:
                    self.assertFalse(
                        checks[
                            "single_tpc_matrix_tpc_bits_are_valid_unique_integers"
                        ]
                    )

    def test_manifest_requires_exact_mps_prohibition_and_bypass(self) -> None:
        manifest = _gate_manifest(promoted=True)["content"]
        manifest["safety"]["mps_allowed"] = True
        manifest["safety"]["mps_bypass"] = "unset"
        checks, allowed = evaluate_gate_manifest_policy(
            manifest,
            mode="global",
            physical_gpu=3,
            gpu={"name": "GPU", "uuid": "uuid"},
            driver_version=13030,
            experimental_mask_off=None,
            timeout_s=10,
            maximum_used_mib=1024,
            iterations=100,
            blocks=4096,
            threads_per_block=256,
            trial=0,
            enabled_tpc=0,
        )

        self.assertFalse(allowed)
        self.assertFalse(checks["manifest_forbids_mps"])
        self.assertFalse(checks["manifest_mps_bypass_is_exact"])


class CommandTest(unittest.TestCase):
    def test_command_and_child_environment_are_explicit(self) -> None:
        command = build_probe_command(
            binary=Path("/probe"),
            mode="stream",
            enabled_tpc=7,
            iterations=10000,
            blocks=4096,
            experimental_allow_unsupported_driver=True,
        )
        self.assertEqual(command[0], "/probe")
        self.assertEqual(command[command.index("--enabled-tpc") + 1], "7")
        self.assertIn("--allow-unsupported-driver", command)
        baseline = build_probe_command(
            binary=Path("/probe"),
            mode="baseline",
            enabled_tpc=0,
            iterations=10,
            blocks=4096,
            experimental_allow_unsupported_driver=False,
        )
        self.assertNotIn("--enabled-tpc", baseline)

        with mock.patch.dict(
            os.environ,
            {
                "CUDA_VISIBLE_DEVICES": "old",
                "MASK_OFF": "old",
                "CUDA_MPS_PIPE_DIRECTORY": "/tmp/mps-pipe",
                "CUDA_MPS_LOG_DIRECTORY": "/tmp/mps-log",
                "CUDA_MPS_ACTIVE_THREAD_PERCENTAGE": "50",
                "BURSTSERVE_PARENT_PID": "stale",
                "LD_PRELOAD": "/tmp/injected.so",
                "LD_AUDIT": "/tmp/audit.so",
                "LD_LIBRARY_PATH": "/tmp/injected-libs",
                "CUDA_INJECTION32_PATH": "/tmp/inject32.so",
                "CUDA_INJECTION64_PATH": "/tmp/inject64.so",
            },
            clear=False,
        ):
            environment = build_child_environment(
                selected_gpu_uuid="GPU-selected",
                experimental_mask_off=12,
                parent_pid=4321,
            )
            baseline_environment = build_child_environment(
                selected_gpu_uuid="GPU-selected",
                experimental_mask_off=None,
            )
        self.assertEqual(environment["CUDA_VISIBLE_DEVICES"], "GPU-selected")
        self.assertEqual(environment["MASK_OFF"], "12")
        self.assertEqual(environment["BURSTSERVE_PARENT_PID"], "4321")
        self.assertNotIn("BURSTSERVE_PARENT_PID", baseline_environment)
        self.assertEqual(
            environment,
            {
                "LANG": "C",
                "LC_ALL": "C",
                "TZ": "UTC",
                "CUDA_CACHE_DISABLE": "1",
                "CUDA_VISIBLE_DEVICES": "GPU-selected",
                "CUDA_MPS_PIPE_DIRECTORY": "",
                "MASK_OFF": "12",
                "BURSTSERVE_PARENT_PID": "4321",
            },
        )
        for name in (
            "CUDA_INJECTION32_PATH",
            "CUDA_INJECTION64_PATH",
            "LD_PRELOAD",
            "LD_AUDIT",
            "LD_LIBRARY_PATH",
        ):
            self.assertNotIn(name, environment)
            self.assertNotIn(name, baseline_environment)
        self.assertEqual(
            environment["CUDA_MPS_PIPE_DIRECTORY"],
            "",
        )
        self.assertFalse(
            any(
                name.startswith("CUDA_MPS_")
                and name != "CUDA_MPS_PIPE_DIRECTORY"
                for name in environment
            )
        )


class ProcessPreflightTest(unittest.TestCase):
    def test_formal_environment_capture_is_cpu_only_without_parent_mutation(
        self,
    ) -> None:
        original_visible = "GPU-parent-visible"
        original_mps = "/tmp/parent-mps"
        with (
            mock.patch.dict(
                os.environ,
                {
                    "CUDA_VISIBLE_DEVICES": original_visible,
                    "CUDA_MPS_PIPE_DIRECTORY": original_mps,
                },
                clear=False,
            ),
            mock.patch.object(
                smctrl_runner,
                "capture_environment",
                return_value={"schema_version": "synthetic"},
            ) as capture,
        ):
            snapshot = smctrl_runner.capture_probe_environment(
                repo_root=Path("/repo"),
                selected_gpu_uuid="GPU-target",
                expected_libsmctrl_commit=TEST_LIBSMCTRL_COMMIT,
            )
            self.assertEqual(
                os.environ["CUDA_VISIBLE_DEVICES"],
                original_visible,
            )
            self.assertEqual(
                os.environ["CUDA_MPS_PIPE_DIRECTORY"],
                original_mps,
            )

        capture.assert_called_once_with(
            repo_root=Path("/repo"),
            model_root=None,
            command_environment=(
                smctrl_runner.FORMAL_ENVIRONMENT_CAPTURE_SUBPROCESS_ENVIRONMENT
            ),
            framework_gpu_probe=False,
            allow_nvcc_path_search=False,
            isolated_python=True,
            git_expected_gitlinks={
                "vendor/libsmctrl": TEST_LIBSMCTRL_COMMIT,
            },
            git_allowed_untracked_roots=(
                smctrl_runner.FORMAL_GIT_ALLOWED_UNTRACKED_ROOTS
            ),
            git_allow_untracked_regular_files=False,
            require_asle_binding=True,
        )
        policy = snapshot["formal_gpu_capture_policy"]
        self.assertFalse(policy["framework_gpu_probe_enabled"])
        self.assertEqual(policy["subprocess_cuda_visible_devices"], "")
        self.assertFalse(
            policy["target_gpu_exposed_to_framework_subprocess"]
        )
        self.assertFalse(policy["parent_environment_mutated"])

    @mock.patch("burstserve.smctrl_runner.subprocess.run")
    def test_filters_compute_processes_by_gpu_uuid(self, run: mock.Mock) -> None:
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                "GPU-a, 10, 100, worker-a\n"
                "GPU-b, 20, 200, worker,b\n"
            ),
            stderr="",
        )

        self.assertEqual(
            query_compute_processes("GPU-b"),
            [
                {
                    "gpu_uuid": "GPU-b",
                    "pid": 20,
                    "used_gpu_memory_mib": 200,
                    "process_name": "worker,b",
                }
            ],
        )
        self.assertEqual(
            run.call_args.args[0][0],
            str(smctrl_runner.TRUSTED_NVIDIA_SMI_EXECUTABLE),
        )
        self.assertEqual(
            run.call_args.kwargs["env"],
            smctrl_runner.TRUSTED_TOOL_ENVIRONMENT,
        )

    @mock.patch("burstserve.smctrl_runner.subprocess.run")
    def test_finds_mps_control_and_server_processes(self, run: mock.Mock) -> None:
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                "  10 nvidia-cuda-mps-control /usr/bin/nvidia-cuda-mps-control -d\n"
                "  11 python python worker.py\n"
                "  12 nvidia-cuda-mps-server /usr/bin/nvidia-cuda-mps-server\n"
            ),
            stderr="",
        )

        self.assertEqual(
            [process["pid"] for process in query_mps_processes()],
            [10, 12],
        )
        self.assertEqual(
            run.call_args.args[0][0],
            str(smctrl_runner.TRUSTED_PS_EXECUTABLE),
        )
        self.assertEqual(
            run.call_args.kwargs["env"],
            smctrl_runner.TRUSTED_TOOL_ENVIRONMENT,
        )

    @mock.patch("burstserve.smctrl_runner.subprocess.run")
    def test_gpu_query_uses_absolute_tool_and_exact_environment(
        self,
        run: mock.Mock,
    ) -> None:
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                "3, GPU, GPU-uuid, 0000:01:00.0, 24564, 0, 0, test\n"
            ),
            stderr="",
        )
        contaminants = {
            "PATH": "/tmp/attacker",
            "LD_PRELOAD": "/tmp/attacker.so",
            "PYTHONPATH": "/tmp/attacker-python",
        }
        with mock.patch.dict(os.environ, contaminants, clear=False):
            record = smctrl_runner.query_gpu(3)

        self.assertEqual(record["uuid"], "GPU-uuid")
        self.assertEqual(
            run.call_args.args[0][0],
            str(smctrl_runner.TRUSTED_NVIDIA_SMI_EXECUTABLE),
        )
        self.assertEqual(
            run.call_args.kwargs["env"],
            smctrl_runner.TRUSTED_TOOL_ENVIRONMENT,
        )
        self.assertTrue(
            {"LD_PRELOAD", "PYTHONPATH"}.isdisjoint(
                run.call_args.kwargs["env"]
            )
        )

    def test_cuda_driver_query_loads_only_verified_descriptor_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            library_path = Path(temporary) / "libcuda.so.synthetic"
            library_path.write_bytes(b"synthetic libcuda")
            descriptor = os.open(library_path, os.O_RDONLY)

            class GetVersion:
                argtypes = None
                restype = None

                def __call__(self, pointer):
                    pointer._obj.value = 13030
                    return 0

            fake_library = type(
                "FakeCuda",
                (),
                {"cuDriverGetVersion": GetVersion()},
            )()
            with (
                mock.patch.object(
                    smctrl_runner,
                    "_open_verified_libcuda",
                    return_value=(
                        descriptor,
                        _synthetic_libcuda_identity(),
                    ),
                ),
                mock.patch.object(
                    smctrl_runner.ctypes,
                    "CDLL",
                    return_value=fake_library,
                ) as loader,
            ):
                record = smctrl_runner.query_cuda_driver()

        self.assertEqual(record["version"], 13030)
        self.assertEqual(
            record["library_identity"],
            _synthetic_libcuda_identity(),
        )
        load_path = loader.call_args.args[0]
        self.assertEqual(load_path, f"/proc/self/fd/{descriptor}")
        self.assertNotEqual(load_path, "libcuda.so.1")
        with self.assertRaises(OSError):
            os.fstat(descriptor)
        self.assertIn(
            "before this module",
            record["python_pre_main_threat_boundary"],
        )

    def test_libcuda_link_must_be_root_owned(self):
        link_status = mock.Mock(
            st_mode=stat.S_IFLNK | 0o777,
            st_uid=os.geteuid(),
        )
        with (
            mock.patch.object(
                smctrl_runner,
                "_trusted_libcuda_directory_records",
                return_value=[],
            ),
            mock.patch.object(
                smctrl_runner.os,
                "lstat",
                return_value=link_status,
            ),
            mock.patch.object(smctrl_runner.os, "open") as open_mock,
        ):
            with self.assertRaisesRegex(RuntimeError, "root-owned symlink"):
                smctrl_runner._open_verified_libcuda()
        open_mock.assert_not_called()

    def test_libcuda_path_is_fixed_not_caller_selected(self):
        with self.assertRaisesRegex(RuntimeError, "formal libcuda path"):
            smctrl_runner._open_verified_libcuda(
                Path("/tmp/libcuda.so.1")
            )

    def test_cuda_driver_final_identity_mismatch_fails_closed(self):
        expected = _synthetic_libcuda_identity()
        observed = _synthetic_libcuda_identity()
        observed["target_identity"] = dict(observed["target_identity"])
        observed["target_identity"]["sha256"] = "8" * 64
        with mock.patch.object(
            smctrl_runner,
            "inspect_cuda_driver_library",
            return_value=observed,
        ):
            record = smctrl_runner.revalidate_cuda_driver_library(expected)
        self.assertTrue(record["completed"])
        self.assertFalse(record["matches_initial"])
        self.assertFalse(record["passed"])

    def test_runtime_libcuda_is_bound_to_attested_build_stamp(self):
        identity = _synthetic_libcuda_identity()
        binding = {
            "build_stamp": {
                "fields": {
                    "LIBCUDA_LINK_LIBRARY": identity["resolved_path"],
                    "LIBCUDA_LINK_LIBRARY_SHA256": identity[
                        "target_identity"
                    ]["sha256"],
                }
            }
        }
        checks = smctrl_runner.evaluate_libcuda_build_binding(
            identity,
            binding,
        )
        self.assertTrue(all(checks.values()))
        binding["build_stamp"]["fields"][
            "LIBCUDA_LINK_LIBRARY_SHA256"
        ] = "8" * 64
        checks = smctrl_runner.evaluate_libcuda_build_binding(
            identity,
            binding,
        )
        self.assertFalse(
            checks["runtime_libcuda_sha256_matches_build_stamp"]
        )

    def test_final_launch_preflight_covers_complete_masked_horizon(self):
        reservation = _gate_manifest()["content"]["safety"][
            "exclusive_reservation_evidence"
        ]
        with (
            mock.patch.object(
                smctrl_runner,
                "query_gpu",
                return_value={
                    "index": 3,
                    "name": "GPU",
                    "uuid": "uuid",
                    "memory_used_mib": 0,
                },
            ),
            mock.patch.object(
                smctrl_runner,
                "query_compute_processes",
                return_value=[],
            ),
            mock.patch.object(
                smctrl_runner,
                "query_mps_processes",
                return_value=[],
            ),
        ):
            record = smctrl_runner.capture_final_launch_preflight(
                physical_gpu=3,
                expected_gpu_uuid="uuid",
                expected_lease_uuid="uuid",
                reservation_evidence=reservation,
                mode="global",
                timeout_s=10,
                maximum_used_mib=1024,
                allow_busy_gpu=False,
                mps_pipe_directory="",
            )
        self.assertTrue(record["passed"])
        self.assertEqual(
            record["required_horizon_s"],
            smctrl_runner.FINAL_PREFLIGHT_QUERY_BUDGET_S
            + 10
            + smctrl_runner.PROCESS_SUPERVISION_CLEANUP_BUDGET_S
            + smctrl_runner.MASKED_XID_TOTAL_BUDGET_MS / 1000
            + smctrl_runner.POST_HEALTH_QUERY_BUDGET_S
            + smctrl_runner.RESERVATION_SAFETY_MARGIN_S,
        )

    def test_baseline_final_preflight_requires_no_reservation(self):
        with (
            mock.patch.object(
                smctrl_runner,
                "query_gpu",
                return_value={
                    "index": 3,
                    "name": "GPU",
                    "uuid": "uuid",
                    "memory_used_mib": 0,
                },
            ),
            mock.patch.object(
                smctrl_runner,
                "query_compute_processes",
                return_value=[],
            ),
            mock.patch.object(
                smctrl_runner,
                "query_mps_processes",
                return_value=[],
            ),
        ):
            record = smctrl_runner.capture_final_launch_preflight(
                physical_gpu=3,
                expected_gpu_uuid="uuid",
                expected_lease_uuid="uuid",
                reservation_evidence=None,
                mode="baseline",
                timeout_s=10,
                maximum_used_mib=1024,
                allow_busy_gpu=False,
                mps_pipe_directory="",
            )
        self.assertTrue(record["passed"])
        self.assertEqual(record["required_horizon_s"], 0.0)
        self.assertEqual(
            record["checks"]["reservation_not_required_for_baseline"],
            True,
        )
        self.assertTrue(
            record["checks"][
                "reservation_valid_for_complete_run_horizon"
            ]
        )

    def test_busy_baseline_never_exempts_uuid_and_records_host_mps(self):
        reservation = _gate_manifest()["content"]["safety"][
            "exclusive_reservation_evidence"
        ]
        with (
            mock.patch.object(
                smctrl_runner,
                "query_gpu",
                return_value={
                    "index": 3,
                    "name": "GPU",
                    "uuid": "GPU-other",
                    "memory_used_mib": 99999,
                },
            ),
            mock.patch.object(
                smctrl_runner,
                "query_compute_processes",
                return_value=[{"pid": 1}],
            ),
            mock.patch.object(
                smctrl_runner,
                "query_mps_processes",
                return_value=[{"pid": 2}],
            ),
        ):
            record = smctrl_runner.capture_final_launch_preflight(
                physical_gpu=3,
                expected_gpu_uuid="uuid",
                expected_lease_uuid="uuid",
                reservation_evidence=reservation,
                mode="baseline",
                timeout_s=10,
                maximum_used_mib=0,
                allow_busy_gpu=True,
                mps_pipe_directory="",
            )
        self.assertTrue(
            record["checks"]["memory_safe_or_explicit_busy_baseline"]
        )
        self.assertTrue(
            record["checks"][
                "compute_processes_absent_or_explicit_busy_baseline"
            ]
        )
        self.assertFalse(record["checks"]["gpu_uuid_stable"])
        self.assertTrue(record["checks"]["empty_mps_pipe_bypass_exact"])
        self.assertEqual(record["mps_processes"], [{"pid": 2}])
        self.assertFalse(record["passed"])

    def test_final_launch_malformed_records_fail_closed(self):
        reservation = _gate_manifest()["content"]["safety"][
            "exclusive_reservation_evidence"
        ]
        with (
            mock.patch.object(
                smctrl_runner,
                "query_gpu",
                return_value={
                    "index": True,
                    "name": "GPU",
                    "uuid": "uuid",
                    "memory_used_mib": 0,
                },
            ),
            mock.patch.object(
                smctrl_runner,
                "query_compute_processes",
                return_value={},
            ),
            mock.patch.object(
                smctrl_runner,
                "query_mps_processes",
                return_value=None,
            ),
        ):
            record = smctrl_runner.capture_final_launch_preflight(
                physical_gpu=1,
                expected_gpu_uuid="uuid",
                expected_lease_uuid="uuid",
                reservation_evidence=reservation,
                mode="baseline",
                timeout_s=10,
                maximum_used_mib=1024,
                allow_busy_gpu=True,
                mps_pipe_directory="",
            )
        self.assertFalse(record["passed"])
        self.assertFalse(record["checks"]["health_queries_completed"])
        self.assertFalse(record["checks"]["gpu_ordinal_exact"])
        self.assertTrue(
            any("GPU index is malformed" in error for error in record["errors"])
        )
        self.assertTrue(
            any(
                "compute process query returned a malformed record" in error
                for error in record["errors"]
            )
        )
        self.assertTrue(
            any(
                "MPS process query returned a malformed record" in error
                for error in record["errors"]
            )
        )


class NativeContractTest(unittest.TestCase):
    def test_single_line_and_schema_are_strict(self) -> None:
        value = _native()
        self.assertEqual(
            parse_native_output(_native_stdout(value)),
            value,
        )
        with self.assertRaises(NativeOutputError):
            parse_native_output("{}\n{}\n")
        native_spelling = dict(value)
        native_spelling["schema"] = native_spelling.pop("schema_version")
        with self.assertRaises(NativeOutputError):
            parse_native_output(_native_stdout(native_spelling))
        value["schema_version"] = "wrong"
        with self.assertRaises(NativeOutputError):
            parse_native_output(_native_stdout(value))

    def test_stdout_rejects_duplicate_nonfinite_and_huge_integer(self):
        valid = _native_stdout(_native())
        duplicate = valid.replace(
            '"status":"ok"',
            '"status":"ok","status":"ok"',
            1,
        )
        nonfinite = valid.replace(
            '"driver_version":13030',
            '"driver_version":NaN',
            1,
        )
        huge_integer = valid.replace(
            '"driver_version":13030',
            '"driver_version":' + ("9" * 129),
            1,
        )
        for name, candidate in (
            ("duplicate", duplicate),
            ("nonfinite", nonfinite),
            ("huge_integer", huge_integer),
        ):
            with self.subTest(name=name):
                with self.assertRaises(NativeOutputError):
                    parse_native_output(candidate)

    def test_stdout_rejects_depth_extras_whitespace_and_wrong_order(self):
        value = _native()
        deep = dict(value)
        nested: object = 0
        for _ in range(smctrl_runner.MAX_FORMAL_JSON_DEPTH + 2):
            nested = [nested]
        deep["extra"] = nested
        extra = dict(value)
        extra["extra"] = True
        reordered = dict(reversed(tuple(value.items())))
        candidates = {
            "depth": _native_stdout(deep),
            "extra": _native_stdout(extra),
            "whitespace": json.dumps(value) + "\n",
            "missing_newline": _native_stdout(value).removesuffix("\n"),
            "wrong_order": _native_stdout(reordered),
        }
        for name, candidate in candidates.items():
            with self.subTest(name=name):
                with self.assertRaises(NativeOutputError):
                    parse_native_output(candidate)

    def test_stdout_rejects_unknown_nested_key(self):
        value = _native()
        value["device"]["extra"] = True
        with self.assertRaises(NativeOutputError):
            parse_native_output(_native_stdout(value))

    def test_pre_kernel_error_accepts_canonical_empty_histogram_but_fails(
        self,
    ) -> None:
        value = _native(
            status="error",
            histogram={},
            blocks=4096,
            parent_pid=os.getpid(),
        )
        value["error"] = "cuInit failed before kernel launch"

        parsed = parse_native_output(_native_stdout(value))
        checks, _metrics, accepted = evaluate_probe(
            parsed,
            expected_mode="baseline",
            expected_enabled_tpc=0,
            expected_driver_version=13030,
            expected_runtime_version=13000,
            expected_iterations=100,
            process_exit_code=1,
            expected_blocks=4096,
            expected_parent_pid=os.getpid(),
            expected_device_uuid="uuid",
        )

        self.assertEqual(parsed["observed_histogram"], {})
        self.assertFalse(checks["status_ok"])
        self.assertFalse(accepted)

        missing_error = _native(status="error", histogram={})
        success_with_error = _native()
        success_with_error["error"] = "not permitted on success"
        for candidate in (missing_error, success_with_error):
            with self.assertRaises(NativeOutputError):
                parse_native_output(_native_stdout(candidate))

    def test_histogram_object_dense_array_and_records(self) -> None:
        self.assertEqual(normalize_histogram({"0": 2, "3": 4}), {0: 2, 3: 4})
        self.assertEqual(normalize_histogram([2, 0, 4]), {0: 2, 1: 0, 2: 4})
        self.assertEqual(
            normalize_histogram(
                [{"sm_id": 2, "count": 4}, {"sm": 5, "hits": 9}]
            ),
            {2: 4, 5: 9},
        )

    def test_baseline_requires_broad_coverage(self) -> None:
        acceptance, metrics, accepted = evaluate_probe(
            _native(sm_count=8, histogram={"0": 4, "1": 4}),
            expected_mode="baseline",
            expected_enabled_tpc=0,
            expected_driver_version=13030,
            expected_runtime_version=13000,
            expected_iterations=100,
            process_exit_code=0,
            expected_parent_pid=123,
        )
        self.assertFalse(accepted)
        self.assertFalse(acceptance["baseline_broad_sm_coverage"])
        self.assertEqual(metrics["observed_sm_count"], 2)

        _, metrics, accepted = evaluate_probe(
            _native(
                sm_count=8,
                histogram={str(index): 4 for index in range(6)},
            ),
            expected_mode="baseline",
            expected_enabled_tpc=0,
            expected_driver_version=13030,
            expected_runtime_version=13000,
            expected_iterations=100,
            process_exit_code=0,
            expected_parent_pid=123,
        )
        self.assertTrue(accepted)
        self.assertEqual(metrics["sm_coverage_ratio"], 0.75)

    def test_masked_probe_requires_one_or_two_sms_and_ok_status(self) -> None:
        _, _, accepted = evaluate_probe(
            _native(mode="stream", histogram={"17": 40, "18": 40}, sm_count=128),
            expected_mode="stream",
            expected_enabled_tpc=0,
            expected_driver_version=13030,
            expected_runtime_version=13000,
            expected_iterations=100,
            process_exit_code=0,
            expected_parent_pid=123,
            expected_tpc_count=64,
        )
        self.assertTrue(accepted)

        acceptance, _, accepted = evaluate_probe(
            _native(
                mode="stream",
                histogram={"17": 40, "18": 40, "19": 1},
                sm_count=128,
            ),
            expected_mode="stream",
            expected_enabled_tpc=0,
            expected_driver_version=13030,
            expected_runtime_version=13000,
            expected_iterations=100,
            process_exit_code=0,
            expected_tpc_count=64,
        )
        self.assertFalse(accepted)
        self.assertFalse(acceptance["single_tpc_observed_one_or_two_sms"])

        _, _, accepted = evaluate_probe(
            _native(
                mode="stream",
                histogram={"17": 40},
                sm_count=128,
                status="unsupported",
            ),
            expected_mode="stream",
            expected_enabled_tpc=0,
            expected_driver_version=13030,
            expected_runtime_version=13000,
            expected_iterations=100,
            process_exit_code=0,
            expected_tpc_count=64,
        )
        self.assertFalse(accepted)

    def test_masked_probe_requires_exact_armed_parent_guard(self) -> None:
        native = _native(
            mode="global",
            histogram={"17": 40},
            sm_count=128,
            parent_pid=999,
        )
        acceptance, _, accepted = evaluate_probe(
            native,
            expected_mode="global",
            expected_enabled_tpc=0,
            expected_driver_version=13030,
            expected_runtime_version=13000,
            expected_iterations=100,
            process_exit_code=0,
            expected_parent_pid=123,
            expected_tpc_count=64,
        )

        self.assertFalse(accepted)
        self.assertFalse(
            acceptance["parent_guard_expected_pid_matches"]
        )
        self.assertFalse(
            acceptance["parent_guard_observed_pid_matches"]
        )


class BoundProcessGroupSafetyTest(unittest.TestCase):
    class FakeWaitProcess:
        pid = 424242

        def __init__(self, returncode=None):
            self.returncode = returncode

        def wait(self, timeout):
            self.returncode = 0
            return 0

    def test_reaped_leader_is_never_used_as_a_pgid_target(self):
        process = self.FakeWaitProcess(returncode=0)
        with mock.patch("os.killpg") as killpg:
            error = smctrl_runner._send_process_group_signal(
                process,
                signal.SIGKILL,
            )
            health = smctrl_runner._verify_completed_process_group(process)
        killpg.assert_not_called()
        self.assertIn("after its leader was reaped", error)
        self.assertFalse(health["process_group_reaped"])

    def test_identity_mismatch_refuses_group_signal(self):
        process = self.FakeWaitProcess(returncode=None)
        identity = {
            "pid": process.pid,
            "process_group_id": process.pid,
            "session_id": process.pid,
            "starttime_ticks": 99,
        }
        with (
            mock.patch.object(
                smctrl_runner,
                "_identity_still_matches",
                return_value=False,
            ),
            mock.patch("os.killpg") as killpg,
        ):
            error = smctrl_runner._signal_bound_process_group(
                process,
                identity,
                signal.SIGKILL,
            )
        killpg.assert_not_called()
        self.assertIn("identity mismatch", error)

    def test_waitid_supervisor_scans_before_final_reap(self):
        process = self.FakeWaitProcess(returncode=None)
        identity = {
            "pid": process.pid,
            "process_group_id": process.pid,
            "session_id": process.pid,
            "starttime_ticks": 99,
        }
        with (
            mock.patch.object(
                smctrl_runner,
                "_waitid_wnowait",
                return_value=True,
            ) as waitid,
            mock.patch.object(
                smctrl_runner,
                "_identity_still_matches",
                return_value=True,
            ),
            mock.patch.object(
                smctrl_runner,
                "_scan_bound_process_group",
                return_value=[],
            ) as scan,
            mock.patch("os.killpg") as killpg,
        ):
            health = smctrl_runner._supervise_process(
                process,
                identity,
                timeout_s=1,
            )
        waitid.assert_called_once_with(process.pid, 1)
        scan.assert_called_once_with(identity)
        killpg.assert_not_called()
        self.assertTrue(health["process_group_reaped"])
        self.assertEqual(
            health["wait_strategy"],
            "waitid(WNOWAIT)+bound-session-scan+final-wait",
        )

    def test_supervisor_preserves_first_base_exception_and_still_reaps(self):
        process = self.FakeWaitProcess(returncode=None)
        identity = {
            "pid": process.pid,
            "process_group_id": process.pid,
            "session_id": process.pid,
            "starttime_ticks": 99,
        }
        interruption = KeyboardInterrupt("wait interrupted")
        with (
            mock.patch.object(
                smctrl_runner,
                "_waitid_wnowait",
                side_effect=[interruption, True],
            ),
            mock.patch.object(
                smctrl_runner,
                "_signal_bound_process_group",
                return_value=None,
            ),
            mock.patch.object(
                smctrl_runner,
                "_identity_still_matches",
                return_value=True,
            ),
            mock.patch.object(
                smctrl_runner,
                "_scan_bound_process_group",
                return_value=[],
            ),
            mock.patch("os.killpg") as killpg,
        ):
            health = smctrl_runner._supervise_process(
                process,
                identity,
                timeout_s=1,
            )
        killpg.assert_not_called()
        self.assertIs(health["pending_base_exception"], interruption)
        self.assertTrue(health["child_reaped"])
        self.assertFalse(health["process_group_reaped"])

    def test_identityless_reap_preserves_kill_interrupt_and_retries(self):
        process = self.FakeWaitProcess(returncode=None)
        interruption = KeyboardInterrupt("kill interrupted")
        kill_attempts = 0

        def successful_kill():
            nonlocal kill_attempts
            kill_attempts += 1
            if kill_attempts == 1:
                raise interruption
            process.returncode = -signal.SIGKILL

        process.kill = mock.Mock(side_effect=successful_kill)
        with mock.patch("os.killpg") as killpg:
            health = smctrl_runner._reap_spawn_without_identity(process)
        killpg.assert_not_called()
        self.assertIs(health["pending_base_exception"], interruption)
        self.assertTrue(health["child_reaped"])
        self.assertEqual(process.kill.call_count, 2)

    def test_spawn_without_identity_uses_direct_pid_only_and_fails_closed(self):
        process = self.FakeWaitProcess(returncode=None)
        process.kill = mock.Mock(
            side_effect=lambda: setattr(
                process,
                "returncode",
                -signal.SIGKILL,
            )
        )
        with mock.patch("os.killpg") as killpg:
            health = smctrl_runner._reap_spawn_without_identity(process)
        killpg.assert_not_called()
        process.kill.assert_called_once_with()
        self.assertTrue(health["child_reaped"])
        self.assertFalse(health["process_group_quiesced"])
        self.assertFalse(health["process_group_reaped"])
        self.assertIsNone(health["identity"])
        self.assertEqual(
            health["wait_strategy"],
            "direct-child-kill+wait;group-untrusted",
        )


class LifecycleLeaseTest(unittest.TestCase):
    def test_dangling_quarantine_symlink_blocks_lease_acquisition(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lease = smctrl_runner._GpuLease(root, "GPU-test")
            lease.quarantine_path.symlink_to(root / "missing-target")

            with self.assertRaisesRegex(
                RuntimeError,
                "persistently quarantined",
            ):
                with lease:
                    self.fail("dangling quarantine link was ignored")

    def test_dangling_quarantine_symlink_blocks_poison_clear(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lease = smctrl_runner._GpuLease(root, "GPU-test")
            lease.__enter__()
            try:
                lease.arm_masked_poison(run_id=f"bs1-{'d' * 64}")
                lease.quarantine_path.symlink_to(root / "missing-target")
                with self.assertRaisesRegex(
                    RuntimeError,
                    "refusing to clear",
                ):
                    lease.clear_masked_poison()
                self.assertGreater(lease.path.stat().st_size, 0)
                lease.mark_terminal()
            finally:
                lease.close()

    def test_uuid_lock_is_nonblocking_and_quarantine_persists(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = smctrl_runner._GpuLease(root, "GPU-test")
            with first:
                with self.assertRaises(BlockingIOError):
                    with smctrl_runner._GpuLease(root, "GPU-test"):
                        self.fail("a second formal owner acquired the UUID")
                first.quarantine(["xid_observed"], xid=79)
                first.mark_terminal()
            marker = first.quarantine_path
            self.assertTrue(marker.is_file())
            record = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual(record["reasons"], ["xid_observed"])
            self.assertFalse(record["auto_clear_permitted"])
            with self.assertRaisesRegex(RuntimeError, "persistently quarantined"):
                with smctrl_runner._GpuLease(root, "GPU-test"):
                    self.fail("quarantine auto-cleared")

    def test_unfinished_gpu_lease_quarantines_before_unlock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lease = smctrl_runner._GpuLease(root, "GPU-test")
            with lease:
                pass
            record = json.loads(
                lease.quarantine_path.read_text(encoding="utf-8")
            )
            self.assertIn(
                "supervisor_exited_without_terminal_artifact",
                record["reasons"],
            )

    @unittest.skipUnless(hasattr(os, "fork"), "requires POSIX fork")
    def test_masked_poison_survives_supervisor_sigkill(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            probe = smctrl_runner._GpuLease(root, "GPU-test")
            read_descriptor, write_descriptor = os.pipe()
            pid = os.fork()
            if pid == 0:
                os.close(read_descriptor)
                try:
                    lease = smctrl_runner._GpuLease(root, "GPU-test")
                    lease.__enter__()
                    lease.arm_masked_poison(run_id=f"bs1-{'a' * 64}")
                    os.write(write_descriptor, b"armed")
                    signal.pause()
                except BaseException:
                    os._exit(2)
                os._exit(3)
            os.close(write_descriptor)
            try:
                self.assertEqual(os.read(read_descriptor, 5), b"armed")
                os.kill(pid, signal.SIGKILL)
                waited_pid, status = os.waitpid(pid, 0)
                self.assertEqual(waited_pid, pid)
                self.assertTrue(os.WIFSIGNALED(status))
                self.assertEqual(os.WTERMSIG(status), signal.SIGKILL)
            finally:
                os.close(read_descriptor)
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    pass
            poison = probe.path.read_text(encoding="utf-8")
            self.assertIn(
                smctrl_runner.GPU_MASKED_ARMED_POISON_SCHEMA_VERSION,
                poison,
            )
            with self.assertRaisesRegex(
                RuntimeError,
                "persistently quarantined",
            ):
                with smctrl_runner._GpuLease(root, "GPU-test"):
                    self.fail("SIGKILL poison was auto-cleared")

    def test_exact_masked_poison_can_clear_only_while_lease_is_held(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lease = smctrl_runner._GpuLease(root, "GPU-test")
            with lease:
                lease.arm_masked_poison(run_id=f"bs1-{'b' * 64}")
                self.assertGreater(lease.path.stat().st_size, 0)
                lease.clear_masked_poison()
                self.assertEqual(lease.path.stat().st_size, 0)
                lease.mark_terminal()
            with smctrl_runner._GpuLease(root, "GPU-test") as next_lease:
                next_lease.mark_terminal()

    def test_failed_nonempty_poison_replacement_never_empties_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lease = smctrl_runner._GpuLease(root, "GPU-test")
            lease.__enter__()
            try:
                lease.arm_masked_poison(run_id=f"bs1-{'c' * 64}")
                armed = lease.path.read_bytes()
                self.assertTrue(armed)
                with mock.patch.object(
                    smctrl_runner.os,
                    "pwrite",
                    side_effect=OSError("synthetic replacement crash"),
                ):
                    with self.assertRaisesRegex(
                        OSError,
                        "synthetic replacement crash",
                    ):
                        lease._persist_quarantine_in_lock(
                            ["synthetic_failure"],
                            OSError("marker failed"),
                        )
                self.assertEqual(lease.path.read_bytes(), armed)
            finally:
                lease.close()
            with self.assertRaisesRegex(
                RuntimeError,
                "persistently quarantined",
            ):
                with smctrl_runner._GpuLease(root, "GPU-test"):
                    self.fail("failed replacement emptied armed poison")

    def test_terminal_guard_writes_minimal_artifact_on_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            run_directory = Path(directory) / "run"
            run_directory.mkdir()
            error = RuntimeError("escape")
            with self.assertRaises(RuntimeError):
                with smctrl_runner._TerminalArtifactGuard(
                    run_directory,
                    "run",
                ):
                    raise error
            outcome = json.loads(
                (run_directory / "outcome.json").read_text(encoding="utf-8")
            )
            self.assertTrue(outcome["minimal_terminal_artifact"])
            self.assertEqual(
                error.burstserve_run_directory,
                run_directory,
            )

    def test_terminal_guard_downgrades_an_existing_accepted_outcome(self):
        with tempfile.TemporaryDirectory() as directory:
            run_directory = Path(directory) / "run"
            run_directory.mkdir()
            original = {
                "schema_version": smctrl_runner.OUTCOME_SCHEMA_VERSION,
                "completed_at_utc": "2026-07-30T00:00:00.000000Z",
                "exit_code": 0,
                "process_exit_code": 0,
                "local_probe_passed": True,
                "accepted": True,
                "quarantine_required": False,
                "quarantine_reasons": [],
            }
            (run_directory / "outcome.json").write_text(
                json.dumps(original),
                encoding="utf-8",
            )
            error = OSError("terminal event fsync failed")
            with self.assertRaises(OSError):
                with smctrl_runner._TerminalArtifactGuard(
                    run_directory,
                    "run",
                ):
                    raise error
            downgraded = json.loads(
                (run_directory / "outcome.json").read_text(encoding="utf-8")
            )
            self.assertFalse(downgraded["accepted"])
            self.assertFalse(downgraded["local_probe_passed"])
            self.assertTrue(downgraded["quarantine_required"])
            self.assertEqual(
                downgraded["prior_terminal_summary"]["accepted"],
                True,
            )
            self.assertRegex(
                downgraded["prior_terminal_summary"]["sha256"],
                r"^[0-9a-f]{64}$",
            )

    def test_quarantine_marker_failure_poisons_lock_before_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lease = smctrl_runner._GpuLease(root, "GPU-test")
            with mock.patch.object(
                smctrl_runner,
                "write_json_atomic",
                side_effect=OSError("marker denied"),
            ):
                with self.assertRaisesRegex(OSError, "marker denied"):
                    with lease:
                        pass
            fallback = lease.path.read_text(encoding="utf-8")
            self.assertIn(
                "burstserve.gpu-quarantine-lock-fallback/v1",
                fallback,
            )
            with self.assertRaisesRegex(RuntimeError, "persistently quarantined"):
                with smctrl_runner._GpuLease(root, "GPU-test"):
                    self.fail("fallback poison record was auto-cleared")

    def test_total_quarantine_persistence_failure_retains_uuid_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lease = smctrl_runner._GpuLease(root, "GPU-test")
            lease.__enter__()
            try:
                with (
                    mock.patch.object(
                        smctrl_runner,
                        "write_json_atomic",
                        side_effect=OSError("marker denied"),
                    ),
                    mock.patch.object(
                        smctrl_runner.os,
                        "pwrite",
                        side_effect=OSError("lock denied"),
                    ),
                ):
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "intentionally remains held",
                    ):
                        lease.__exit__(None, None, None)
                self.assertIsNotNone(lease.descriptor)
                with self.assertRaises(BlockingIOError):
                    with smctrl_runner._GpuLease(root, "GPU-test"):
                        self.fail("unsafe owner acquired an unpoisoned UUID")
            finally:
                lease.close()


class ExecuteV2BoundaryTest(unittest.TestCase):
    def test_default_manifest_is_v2_unpromoted_and_zero_pinned(self):
        repo_root = Path(__file__).resolve().parents[1]
        content = json.loads(
            (repo_root / smctrl_runner.DEFAULT_GATE_MANIFEST).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            content["schema_version"],
            smctrl_runner.GATE_MANIFEST_SCHEMA_VERSION,
        )
        self.assertFalse(
            content["safety"]["experimental_mask_enabled"]
        )
        self.assertEqual(content["safety"]["approved_mask_modes"], [])
        for field in (
            "approved_launcher_sha256",
            "approved_real_probe_sha256",
            "approved_build_stamp_sha256",
            "approved_build_attestation_sha256",
        ):
            self.assertEqual(content["source"][field], "0" * 64)

    def test_zero_pinned_default_manifest_rejects_before_gpu_access(self):
        repo_root = Path(__file__).resolve().parents[1]
        content = json.loads(
            (repo_root / smctrl_runner.DEFAULT_GATE_MANIFEST).read_text(
                encoding="utf-8"
            )
        )
        config = _config(mode="baseline")
        config["gate_manifest"] = {
            "path": str(smctrl_runner.DEFAULT_GATE_MANIFEST),
            "git_blob": "0" * 40,
            "sha256": hashlib.sha256(
                canonical_json(content).encode("utf-8")
            ).hexdigest(),
            "content": content,
        }
        with (
            mock.patch.object(
                smctrl_runner,
                "_load_attestation_bootstrap",
                side_effect=RuntimeError(
                    "build attestation does not match all-zero approval pin"
                ),
            ) as bootstrap,
            mock.patch.object(smctrl_runner, "query_gpu") as query_gpu,
            mock.patch.object(
                smctrl_runner.subprocess,
                "Popen",
            ) as popen,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "all-zero approval pin",
            ):
                execute(
                    repo_root=repo_root,
                    binary=repo_root / smctrl_runner.DEFAULT_BINARY,
                    libsmctrl_root=(
                        repo_root / smctrl_runner.DEFAULT_LIBSMCTRL_ROOT
                    ),
                    run_root=repo_root / "experiments" / "runs",
                    config=config,
                    timeout_s=30,
                    maximum_used_mib=1024,
                    allow_busy_gpu=False,
                )
        bootstrap.assert_called_once()
        query_gpu.assert_not_called()
        popen.assert_not_called()

    def test_cli_malformed_v2_manifest_is_a_controlled_pre_gpu_rejection(self):
        malformed = {
            "schema_version": smctrl_runner.GATE_MANIFEST_SCHEMA_VERSION,
            "hardware": {},
            "source": {},
            "safety": {},
            "baseline": {},
        }
        record = {
            "path": str(smctrl_runner.DEFAULT_GATE_MANIFEST),
            "git_blob": "0" * 40,
            "sha256": "0" * 64,
            "content": malformed,
        }
        with (
            mock.patch.object(
                smctrl_runner,
                "load_gate_manifest_record",
                return_value=record,
            ),
            mock.patch.object(smctrl_runner, "execute") as execute_mock,
            mock.patch.object(smctrl_runner, "query_gpu") as query_gpu,
        ):
            code = smctrl_runner.main(
                [
                    "run",
                    "--repo-root",
                    str(Path(__file__).resolve().parents[1]),
                    "--physical-gpu",
                    "0",
                    "--mode",
                    "baseline",
                ]
            )
        self.assertEqual(code, 1)
        execute_mock.assert_not_called()
        query_gpu.assert_not_called()

    def test_external_binary_is_rejected_before_attestation_or_gpu_access(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with (
                mock.patch.object(
                    smctrl_runner,
                    "_load_attestation_bootstrap",
                ) as bootstrap,
                mock.patch.object(smctrl_runner, "query_gpu") as query_gpu,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "rejects external binaries",
                ):
                    execute(
                        repo_root=root,
                        binary=root / "external" / "smid_probe",
                        libsmctrl_root=(
                            root / smctrl_runner.DEFAULT_LIBSMCTRL_ROOT
                        ),
                        run_root=root / "runs",
                        config=_config(mode="baseline"),
                        timeout_s=1,
                        maximum_used_mib=0,
                        allow_busy_gpu=False,
                    )
            bootstrap.assert_not_called()
            query_gpu.assert_not_called()

    def test_v1_manifest_is_rejected_before_attestation_or_gpu_access(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            config = _config(mode="baseline")
            config["gate_manifest"]["content"]["schema_version"] = (
                "burstserve.smctrl-gate-manifest/v1"
            )
            with (
                mock.patch.object(
                    smctrl_runner,
                    "_load_attestation_bootstrap",
                ) as bootstrap,
                mock.patch.object(smctrl_runner, "query_gpu") as query_gpu,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "requires Gate-A manifest schema v2",
                ):
                    execute(
                        repo_root=root,
                        binary=root / smctrl_runner.DEFAULT_BINARY,
                        libsmctrl_root=(
                            root / smctrl_runner.DEFAULT_LIBSMCTRL_ROOT
                        ),
                        run_root=root / "runs",
                        config=config,
                        timeout_s=1,
                        maximum_used_mib=0,
                        allow_busy_gpu=False,
                    )
            bootstrap.assert_not_called()
            query_gpu.assert_not_called()

    def test_missing_attestation_pin_is_rejected_before_gpu_access(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            config = _config(mode="baseline")
            del config["gate_manifest"]["content"]["source"][
                "approved_build_attestation_sha256"
            ]
            with (
                mock.patch.object(
                    smctrl_runner,
                    "_load_attestation_bootstrap",
                ) as bootstrap,
                mock.patch.object(smctrl_runner, "query_gpu") as query_gpu,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "requires a Gate-A v2 build-attestation pin",
                ):
                    execute(
                        repo_root=root,
                        binary=root / smctrl_runner.DEFAULT_BINARY,
                        libsmctrl_root=(
                            root / smctrl_runner.DEFAULT_LIBSMCTRL_ROOT
                        ),
                        run_root=root / "runs",
                        config=config,
                        timeout_s=1,
                        maximum_used_mib=0,
                        allow_busy_gpu=False,
                    )
            bootstrap.assert_not_called()
            query_gpu.assert_not_called()

    def test_launcher_open_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.write_bytes(b"launcher")
            link = root / "launcher"
            link.symlink_to(target)
            with self.assertRaises(OSError):
                smctrl_runner._open_regular_nofollow(link)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX FIFO")
    def test_artifact_fifo_is_rejected_without_blocking_open(self):
        with tempfile.TemporaryDirectory() as directory:
            fifo = Path(directory) / "build-attestation.json"
            os.mkfifo(fifo)
            with self.assertRaisesRegex(
                RuntimeError,
                "not a regular file",
            ):
                smctrl_runner._open_regular_nofollow(fifo)

    def test_all_terminal_signal_handlers_defer_and_restore(self):
        signums = (signal.SIGINT, signal.SIGHUP, signal.SIGTERM)
        original = {
            signum: signal.getsignal(signum) for signum in signums
        }
        handlers = smctrl_runner._install_child_signal_handlers()
        try:
            handlers.defer_interrupts()
            for signum in signums:
                installed = signal.getsignal(signum)
                self.assertTrue(callable(installed))
                installed(signum, None)
            self.assertEqual(handlers.pending_signum, signal.SIGINT)
        finally:
            smctrl_runner._restore_signal_handlers(handlers)
        self.assertEqual(
            {
                signum: signal.getsignal(signum)
                for signum in signums
            },
            original,
        )

    def test_exit_codes_are_always_normalized_to_one_byte(self):
        self.assertEqual(smctrl_runner._normalize_exit_code(0), 0)
        self.assertEqual(
            smctrl_runner._normalize_exit_code(-signal.SIGKILL),
            128 + signal.SIGKILL,
        )
        self.assertEqual(smctrl_runner._normalize_exit_code(999), 255)


class MaskedMonitorContractTest(unittest.TestCase):
    def provenance(self):
        return {
            "schema_version": "burstserve.nvml-xid-monitor/v2",
            "method": "nvmlEventSetWait_v2_exact_xid",
            "physical_uuid": "GPU-test",
            "registered_device_handle": 0xD00D,
            "library": {
                "path": "/usr/lib/libnvidia-ml.so.1",
                "sha256": "a" * 64,
                "expected_sha256": "a" * 64,
                "version": "610.43.02",
                "expected_version": "610.43.02",
                "identity": {
                    "device": 1,
                    "inode": 2,
                    "mode": stat.S_IFREG | 0o644,
                    "uid": os.geteuid(),
                    "gid": os.getegid(),
                    "nlink": 1,
                    "size": 123,
                    "mtime_ns": 4,
                    "ctime_ns": 5,
                },
                "sealed_snapshot": {
                    "device": 6,
                    "inode": 7,
                    "mode": stat.S_IFREG | 0o500,
                    "size": 123,
                    "sha256": "a" * 64,
                    "seals": 0x0F,
                    "required_seals": 0x0F,
                    "exec_seal": 0x20,
                    "exec_seal_applied": False,
                    "mfd_exec_used": True,
                    "copy_limit_bytes": (
                        smctrl_runner.MAX_NVML_LIBRARY_BYTES
                    ),
                },
                "load_path": "/proc/self/fd/17",
                "symbols": {
                    "init": "nvmlInit_v2",
                    "shutdown": "nvmlShutdown",
                    "system_get_nvml_version": (
                        "nvmlSystemGetNVMLVersion"
                    ),
                    "device_get_handle_by_uuid": (
                        "nvmlDeviceGetHandleByUUID_v2"
                    ),
                    "event_set_create": "nvmlEventSetCreate",
                    "event_set_free": "nvmlEventSetFree",
                    "device_get_supported_event_types": (
                        "nvmlDeviceGetSupportedEventTypes"
                    ),
                    "device_register_events": (
                        "nvmlDeviceRegisterEvents"
                    ),
                    "event_set_wait_v2": "nvmlEventSetWait_v2",
                },
            },
            "xid_event_bit": 8,
            "supported_event_bits": 8,
            "registered_event_bits": 8,
            "setup": {
                "started_at_utc": "2026-07-30T12:00:00.000000Z",
                "completed_at_utc": "2026-07-30T12:00:00.010000Z",
                "started_monotonic_s": 1.0,
                "completed_monotonic_s": 1.01,
                "succeeded": True,
                "error": None,
            },
            "drain": {
                "started_at_utc": "2026-07-30T12:00:01.000000Z",
                "completed_at_utc": "2026-07-30T12:00:02.000000Z",
                "started_monotonic_s": 2.0,
                "completed_monotonic_s": 3.0,
                "quiet_started_monotonic_s": 2.0,
                "quiet_completed_monotonic_s": 3.0,
                "timeout_ms": 1000,
                "requested_quiet_ms": 1000,
                "observed_quiet_ms": 1000.0,
                "wait_calls": 1,
                "succeeded": True,
                "error": None,
            },
            "cleanup": {
                "completed_at_utc": "2026-07-30T12:00:02.010000Z",
                "completed_monotonic_s": 3.01,
                "errors": [],
            },
            "events": [],
            "xids_seen": 0,
            "safe_for_acceptance": True,
        }

    def evaluate(self, provenance):
        return smctrl_runner.evaluate_masked_health_monitor(
            provenance,
            expected_gpu_uuid="GPU-test",
            expected_library_path="/usr/lib/libnvidia-ml.so.1",
            expected_library_sha256="a" * 64,
            expected_library_version="610.43.02",
        )

    def test_exact_fd_bound_monitor_provenance_is_accepted(self):
        checks, accepted = self.evaluate(self.provenance())
        self.assertTrue(accepted)
        self.assertTrue(all(checks.values()))

    def test_method_handle_identity_and_order_are_independently_checked(self):
        cases = {
            "monitor_method_exact": ("method", "legacy_wait"),
            "registered_device_handle_valid": (
                "registered_device_handle",
                0,
            ),
        }
        for failed_check, (field, value) in cases.items():
            with self.subTest(check=failed_check):
                provenance = self.provenance()
                provenance[field] = value
                checks, accepted = self.evaluate(provenance)
                self.assertFalse(accepted)
                self.assertFalse(checks[failed_check])

        provenance = self.provenance()
        provenance["library"]["load_path"] = "/tmp/reopened-library"
        checks, accepted = self.evaluate(provenance)
        self.assertFalse(accepted)
        self.assertFalse(
            checks["library_loaded_from_sealed_snapshot_fd"]
        )

        provenance = self.provenance()
        provenance["cleanup"]["completed_monotonic_s"] = 1.5
        checks, accepted = self.evaluate(provenance)
        self.assertFalse(accepted)
        self.assertFalse(checks["lifecycle_monotonic_ordered"])


class _RecordingGpuLease:
    def __init__(self, gpu_uuid: str) -> None:
        self.gpu_uuid = gpu_uuid
        self.descriptor: int | None = 777
        self.finalized = False
        self.record = {
            "kind": "gpu_uuid",
            "gpu_uuid": gpu_uuid,
            "path": "/synthetic/gpu.lock",
        }
        self.quarantines: list[tuple[list[str], dict[str, object]]] = []
        self.masked_poison_armed = False
        self.masked_poison_cleared = False

    def __enter__(self):
        return self

    def quarantine(self, reasons, **evidence):
        normalized = sorted(set(str(reason) for reason in reasons))
        self.quarantines.append((normalized, dict(evidence)))
        self.record["quarantine_reasons"] = normalized

    def arm_masked_poison(self, *, run_id):
        self.masked_poison_armed = True
        self.record["masked_poison_armed"] = True
        self.record["masked_poison_run_id"] = run_id

    def clear_masked_poison(self):
        if not self.masked_poison_armed:
            raise RuntimeError("synthetic poison is not armed")
        self.masked_poison_armed = False
        self.masked_poison_cleared = True
        self.record["masked_poison_cleared"] = True

    def mark_terminal(self):
        self.finalized = True
        self.record["terminal_artifact_written"] = True

    def __exit__(self, _type, _value, _traceback):
        self.descriptor = None
        return False


def _synthetic_libcuda_identity() -> dict[str, object]:
    return {
        "link_path": str(smctrl_runner.DEFAULT_LIBCUDA_LINK),
        "link_target": "libcuda.so.synthetic",
        "link_identity": {
            "device": 1,
            "inode": 2,
            "mode": stat.S_IFLNK | 0o777,
            "uid": 0,
            "gid": 0,
            "nlink": 1,
            "size": 20,
            "mtime_ns": 3,
            "ctime_ns": 4,
        },
        "resolved_path": (
            f"{smctrl_runner.TRUSTED_LIBCUDA_DIRECTORY}/"
            "libcuda.so.synthetic"
        ),
        "target_identity": {
            "device": 1,
            "inode": 3,
            "mode": stat.S_IFREG | 0o755,
            "uid": 0,
            "gid": 0,
            "nlink": 1,
            "size": 100,
            "mtime_ns": 5,
            "ctime_ns": 6,
            "sha256": "9" * 64,
        },
        "trusted_directories": [],
    }


def _synthetic_cuda_driver_probe() -> dict[str, object]:
    return {
        "version": 13030,
        "library_identity": _synthetic_libcuda_identity(),
        "load_path": "/proc/self/fd/88",
        "load_binding": "verified /proc/self/fd descriptor",
        "creates_cuda_context": False,
        "python_pre_main_threat_boundary": (
            smctrl_runner.FORMAL_LAUNCHER_THREAT_BOUNDARIES[
                "python_pre_main_injection"
            ]
        ),
    }


def _run_v2_real_child_integration(
    *,
    mode: str = "baseline",
    post_gpu: object | None = None,
    terminal_event_failure: bool = False,
    terminal_directory_failure: str | None = None,
    terminal_directory_target: str = "outcome",
    reject_preexec_source_binding: bool = False,
    reject_launch_commit_reservation: bool = False,
    mutate_launcher_during_final_preflight: bool = False,
    restore_handler_failure: bool = False,
    mask_restore_failure: bool = False,
    pending_signum: int | None = None,
    deliver_signum_during_unmask: int | None = None,
    oversized_stdout: bool = False,
) -> tuple[
    int | BaseException,
    dict[str, object],
    list[dict[str, object]],
    dict[str, object],
    _RecordingGpuLease,
]:
    masked = mode in smctrl_runner.MASKED_MODES
    native = _native(
        mode=mode,
        sm_count=128,
        histogram=(
            {"1": 4096}
            if masked
            else {str(index): 32 for index in range(128)}
        ),
        iterations=100,
        blocks=4096,
        parent_pid=os.getpid(),
    )
    native["device"]["name"] = "GPU"
    valid_gpu = {
        "index": 3,
        "name": "GPU",
        "uuid": "uuid",
        "pci_bus_id": "0000:01:00.0",
        "memory_total_mib": 24564,
        "memory_used_mib": 0,
        "utilization_gpu_percent": 0,
        "driver_version": "test",
    }
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        binary = root / smctrl_runner.DEFAULT_BINARY
        binary.parent.mkdir(parents=True)
        child_print = (
            "print('x' * "
            f"{smctrl_runner.MAX_NATIVE_STDOUT_BYTES + 1}"
            ", flush=True)\n"
            if oversized_stdout
            else (
                "print("
                f"{_native_stdout(native).removesuffix(chr(10))!r}"
                ", flush=True)\n"
            )
        )
        binary.write_text(
            "#!/usr/bin/python3\n"
            "import time\n"
            "time.sleep(0.05)\n"
            + child_print,
            encoding="utf-8",
        )
        binary.chmod(0o500)
        libsmctrl = root / smctrl_runner.DEFAULT_LIBSMCTRL_ROOT
        libsmctrl.mkdir(parents=True)
        build_lock = root / "locks" / "build.lock"
        build_lock.parent.mkdir(mode=0o700)
        build_lock.write_text("", encoding="utf-8")
        build_lock.chmod(0o600)
        config = _config(
            mode=mode,
            experimental_allow=masked,
            promoted=masked,
        )
        bootstrap = {
            "schema_version": (
                smctrl_runner.NATIVE_BUILD_ATTESTATION_SCHEMA_VERSION
            ),
            "build_lock": {"path": str(build_lock)},
        }
        bootstrap_identity = {"sha256": "d" * 64}
        lease = _RecordingGpuLease("uuid")
        query_count = 0

        def query_gpu(_physical_gpu):
            nonlocal query_count
            query_count += 1
            if (
                query_count == 3
                and mutate_launcher_during_final_preflight
            ):
                content = binary.read_bytes()
                replacement = (
                    b"X" if not content.startswith(b"X") else b"Y"
                )
                binary.chmod(0o700)
                try:
                    with binary.open("r+b") as stream:
                        stream.seek(0)
                        stream.write(replacement)
                        stream.flush()
                        os.fsync(stream.fileno())
                finally:
                    binary.chmod(0o500)
            if query_count == 4 and post_gpu is not None:
                return post_gpu
            return dict(valid_gpu)

        def source_binding(**kwargs):
            libcuda = _synthetic_libcuda_identity()
            return (
                {
                    "source_snapshot_sha256": "f" * 64,
                    "launcher_fd_identity": dict(
                        kwargs["launcher_identity"]
                    ),
                    "build_stamp": {
                        "fields": {
                            "LIBCUDA_LINK_LIBRARY": libcuda[
                                "resolved_path"
                            ],
                            "LIBCUDA_LINK_LIBRARY_SHA256": libcuda[
                                "target_identity"
                            ]["sha256"],
                        },
                    },
                },
                {
                    "binary_is_repo_canonical": True,
                    "libsmctrl_is_repo_canonical": True,
                    "synthetic_binding_complete": True,
                    **{
                        name: True
                        for name in (
                            smctrl_runner._FORMAL_GIT_TREE_REQUIRED_CHECKS
                        )
                    },
                },
            )

        def revalidation(**kwargs):
            rejected = (
                reject_preexec_source_binding
                and kwargs["phase"]
                == "preexec_after_monitor_and_signal_block"
            )
            return {
                "phase": kwargs["phase"],
                "completed": True,
                "error": None,
                "expected_snapshot_sha256": kwargs[
                    "expected_snapshot_sha256"
                ],
                "observed_snapshot_sha256": (
                    "0" * 64
                    if rejected
                    else kwargs["expected_snapshot_sha256"]
                ),
                "snapshot_matches_initial": not rejected,
                "passed_for_launch": not rejected,
                "passed_for_local_acceptance": not rejected,
            }

        def libcuda_revalidation(expected_identity):
            expected = dict(expected_identity)
            return {
                "completed": True,
                "error": None,
                "expected_identity": expected,
                "observed_identity": expected,
                "matches_initial": True,
                "passed": True,
            }

        original_record_event = smctrl_runner._record_event
        original_write_json_atomic = smctrl_runner.write_json_atomic
        original_restore_handlers = (
            smctrl_runner._restore_signal_handlers
        )
        original_pthread_sigmask = signal.pthread_sigmask
        original_os_open = os.open
        original_os_fsync = os.fsync
        original_validate_reservation = (
            smctrl_runner.validate_reservation_evidence
        )
        setmask_calls = 0
        reservation_validation_calls = 0
        terminal_directory_failure_fired = False

        def call_with_terminal_directory_failure(callback, *args, **kwargs):
            nonlocal terminal_directory_failure_fired
            terminal_directory_failure_fired = True
            if terminal_directory_failure == "open":
                def fail_directory_open(target, flags, *open_args, **open_kwargs):
                    if flags & getattr(os, "O_DIRECTORY", 0):
                        raise OSError(
                            "synthetic terminal directory open failure"
                        )
                    return original_os_open(
                        target,
                        flags,
                        *open_args,
                        **open_kwargs,
                    )

                with mock.patch.object(
                    provenance.os,
                    "open",
                    side_effect=fail_directory_open,
                ):
                    return callback(*args, **kwargs)
            if terminal_directory_failure == "fsync":
                def fail_directory_fsync(descriptor):
                    if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                        raise OSError(
                            "synthetic terminal directory fsync failure"
                        )
                    return original_os_fsync(descriptor)

                with mock.patch.object(
                    provenance.os,
                    "fsync",
                    side_effect=fail_directory_fsync,
                ):
                    return callback(*args, **kwargs)
            raise AssertionError(
                "terminal_directory_failure must be 'open' or 'fsync'"
            )

        def record_event(*args, **kwargs):
            if (
                terminal_event_failure
                and kwargs.get("event_type")
                in {"run.completed", "run.failed"}
            ):
                raise OSError("synthetic terminal event write failure")
            if (
                terminal_directory_failure is not None
                and terminal_directory_target == "event"
                and not terminal_directory_failure_fired
                and kwargs.get("event_type")
                in {"run.completed", "run.failed"}
            ):
                return call_with_terminal_directory_failure(
                    original_record_event,
                    *args,
                    **kwargs,
                )
            return original_record_event(*args, **kwargs)

        def write_json_atomic(*args, **kwargs):
            path = Path(args[0] if args else kwargs["path"])
            value = args[1] if len(args) > 1 else kwargs["value"]
            if (
                terminal_directory_failure is not None
                and terminal_directory_target == "outcome"
                and not terminal_directory_failure_fired
                and path.name == "outcome.json"
                and isinstance(value, dict)
                and "launch_commit_reservation_revalidation" in value
            ):
                return call_with_terminal_directory_failure(
                    original_write_json_atomic,
                    *args,
                    **kwargs,
                )
            return original_write_json_atomic(*args, **kwargs)

        def restore_handlers(previous):
            result = original_restore_handlers(previous)
            if restore_handler_failure:
                raise OSError("synthetic signal handler restore failure")
            return result

        def pthread_sigmask(how, mask):
            nonlocal setmask_calls
            if how == signal.SIG_SETMASK:
                setmask_calls += 1
                if mask_restore_failure and setmask_calls == 2:
                    raise OSError("synthetic signal mask restore failure")
            result = original_pthread_sigmask(how, mask)
            if (
                how == signal.SIG_SETMASK
                and setmask_calls == 2
                and deliver_signum_during_unmask is not None
            ):
                # The runner's deferring handlers must still own this exact
                # post-unmask/pre-handler-restore window.
                os.kill(os.getpid(), deliver_signum_during_unmask)
            return result

        def validate_reservation(*args, **kwargs):
            nonlocal reservation_validation_calls
            reservation_validation_calls += 1
            checks, passed = original_validate_reservation(
                *args,
                **kwargs,
            )
            if (
                reject_launch_commit_reservation
                and reservation_validation_calls == 3
            ):
                checks = dict(checks)
                checks["synthetic_launch_commit_rejection"] = False
                return checks, False
            return checks, passed

        popen_context = (
            mock.patch.object(smctrl_runner.subprocess, "Popen")
            if (
                reject_preexec_source_binding
                or reject_launch_commit_reservation
                or mutate_launcher_during_final_preflight
            )
            else nullcontext(None)
        )
        auxiliary_patches = ExitStack()
        auxiliary_patches.enter_context(
            mock.patch.object(
                smctrl_runner,
                "_restore_signal_handlers",
                side_effect=restore_handlers,
            )
        )
        auxiliary_patches.enter_context(
            mock.patch.object(
                smctrl_runner,
                "write_json_atomic",
                side_effect=write_json_atomic,
            )
        )
        auxiliary_patches.enter_context(
            mock.patch.object(
                signal,
                "pthread_sigmask",
                side_effect=pthread_sigmask,
            )
        )
        auxiliary_patches.enter_context(
            mock.patch.object(
                smctrl_runner,
                "validate_reservation_evidence",
                side_effect=validate_reservation,
            )
        )
        if masked:
            monitor = _FakeXidMonitor([])
            monitor.physical_uuid = "uuid"
            auxiliary_patches.enter_context(
                mock.patch.object(
                    smctrl_runner,
                    "NvmlXidMonitor",
                    return_value=monitor,
                )
            )
            auxiliary_patches.enter_context(
                mock.patch.object(
                    smctrl_runner,
                    "evaluate_masked_health_monitor",
                    return_value=(
                        {"synthetic_monitor_valid": True},
                        True,
                    ),
                )
            )
        auxiliary_patches.enter_context(
            mock.patch.object(
                signal,
                "sigpending",
                return_value=(
                    {pending_signum}
                    if pending_signum is not None
                    else set()
                ),
            )
        )
        popen_mock = auxiliary_patches.enter_context(popen_context)
        with (
            auxiliary_patches,
            mock.patch.dict(
                os.environ,
                {"CUDA_VISIBLE_DEVICES": ""},
                clear=False,
            ),
            mock.patch.object(
                smctrl_runner,
                "_load_attestation_bootstrap",
                return_value=(bootstrap, bootstrap_identity),
            ),
            mock.patch.object(
                smctrl_runner,
                "_GpuLease",
                side_effect=lambda _root, _uuid: lease,
            ),
            mock.patch.object(
                smctrl_runner,
                "validate_gate_manifest_record",
                side_effect=lambda value, **_: value["content"],
            ),
            mock.patch.object(
                smctrl_runner,
                "query_gpu",
                side_effect=query_gpu,
            ),
            mock.patch.object(
                smctrl_runner,
                "query_compute_processes",
                return_value=[],
            ),
            mock.patch.object(
                smctrl_runner,
                "query_mps_processes",
                return_value=[],
            ),
            mock.patch.object(
                smctrl_runner,
                "query_cuda_driver",
                return_value=_synthetic_cuda_driver_probe(),
            ),
            mock.patch.object(
                smctrl_runner,
                "revalidate_cuda_driver_library",
                side_effect=libcuda_revalidation,
            ),
            mock.patch.object(
                smctrl_runner,
                "latest_pinned_driver_version",
                return_value=13030,
            ),
            mock.patch.object(
                smctrl_runner,
                "source_revision",
                return_value="test-revision",
            ),
            mock.patch.object(
                smctrl_runner,
                "capture_probe_environment",
                return_value={"synthetic": True},
            ),
            mock.patch.object(
                smctrl_runner,
                "native_build_record",
                return_value={
                    "found": True,
                    "path": str(binary.parent / "build-config.stamp"),
                    "sha256": "c" * 64,
                    "content": "synthetic build stamp\n",
                },
            ),
            mock.patch.object(
                smctrl_runner,
                "formal_source_binding",
                side_effect=source_binding,
            ),
            mock.patch.object(
                smctrl_runner,
                "revalidate_formal_source_binding",
                side_effect=revalidation,
            ),
            mock.patch.object(
                smctrl_runner,
                "_record_event",
                side_effect=record_event,
            ),
        ):
            try:
                result, run_directory = execute(
                    repo_root=root,
                    binary=binary,
                    libsmctrl_root=libsmctrl,
                    run_root=root / "runs",
                    config=config,
                    timeout_s=10,
                    maximum_used_mib=1024,
                    allow_busy_gpu=False,
                )
            except BaseException as error:
                result = error
                run_directory = getattr(
                    error,
                    "burstserve_run_directory",
                    None,
                )
                if not isinstance(run_directory, Path):
                    raise
        lease.test_popen_mock = popen_mock
        outcome = json.loads(
            (run_directory / "outcome.json").read_text(encoding="utf-8")
        )
        events = [
            json.loads(line)
            for line in (run_directory / "events.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        command = json.loads(
            (run_directory / "command.json").read_text(encoding="utf-8")
        )
        return result, outcome, events, command, lease


class ExecuteV2IntegrationTest(unittest.TestCase):
    def test_real_child_completes_full_mocked_v2_execute(self):
        code, outcome, events, command, lease = (
            _run_v2_real_child_integration()
        )

        self.assertEqual(code, 0)
        self.assertTrue(outcome["local_probe_passed"])
        self.assertTrue(outcome["accepted"])
        self.assertFalse(outcome["quarantine_required"])
        self.assertTrue(outcome["final_launch_preflight"]["passed"])
        self.assertTrue(outcome["libcuda_final_revalidation"]["passed"])
        self.assertTrue(outcome["launcher_fd_final"]["passed"])
        self.assertTrue(
            outcome["launch_commit_reservation_revalidation"]["passed"]
        )
        self.assertFalse(
            outcome["launch_commit_reservation_revalidation"][
                "required_for_mode"
            ]
        )
        commit = outcome["launch_commit_reservation_revalidation"]
        self.assertEqual(
            set(commit),
            {
                "captured_at_utc",
                "required_for_mode",
                "required_horizon_s",
                "required_until_utc",
                "checks",
                "passed",
                "error",
            },
        )
        self.assertEqual(commit["required_horizon_s"], 0.0)
        self.assertEqual(
            commit["captured_at_utc"],
            commit["required_until_utc"],
        )
        self.assertEqual(
            commit["checks"],
            {"reservation_not_required_for_baseline": True},
        )
        self.assertIsNone(commit["error"])
        self.assertTrue(outcome["process_group_reaped"])
        self.assertTrue(
            outcome["post_health"]["checks"][
                "reservation_valid_at_gpu_safety_end"
            ]
        )
        self.assertFalse(
            outcome["post_health"]["reservation_revalidation"][
                "required_for_mode"
            ]
        )
        self.assertEqual(lease.quarantines, [])
        self.assertEqual(
            [event["event_type"] for event in events],
            [
                "run.preflight",
                "run.source_revalidated",
                "run.source_preexec_revalidated",
                "run.final_launch_preflight",
                "run.started",
                "run.source_postvalidated",
                "run.completed",
            ],
        )
        started = next(
            event for event in events
            if event["event_type"] == "run.started"
        )
        self.assertEqual(
            started["payload"][
                "launch_commit_reservation_revalidation"
            ],
            commit,
        )
        self.assertGreaterEqual(
            smctrl_runner._parse_utc(commit["captured_at_utc"]),
            smctrl_runner._parse_utc(
                outcome["final_launch_preflight"]["captured_at_utc"]
            ),
        )
        self.assertEqual(
            command["environment_overrides"],
            {
                "LANG": "C",
                "LC_ALL": "C",
                "TZ": "UTC",
                "CUDA_CACHE_DISABLE": "1",
                "CUDA_VISIBLE_DEVICES": "uuid",
                "CUDA_MPS_PIPE_DIRECTORY": "",
            },
        )
        self.assertEqual(
            command["launcher_fd_final"],
            outcome["launcher_fd_final"],
        )
        self.assertEqual(
            command["formal_launcher_threat_boundaries"],
            smctrl_runner.FORMAL_LAUNCHER_THREAT_BOUNDARIES,
        )

    def test_full_preexec_source_binding_rejects_before_popen(self):
        code, outcome, events, _command, lease = (
            _run_v2_real_child_integration(
                reject_preexec_source_binding=True
            )
        )

        self.assertEqual(code, 3)
        self.assertFalse(outcome["accepted"])
        self.assertFalse(outcome["local_probe_passed"])
        self.assertFalse(
            outcome["source_preexec_revalidation"]["passed_for_launch"]
        )
        self.assertEqual(lease.test_popen_mock.call_count, 0)
        event_types = [event["event_type"] for event in events]
        self.assertIn("run.source_preexec_revalidated", event_types)
        self.assertNotIn("run.final_launch_preflight", event_types)
        self.assertNotIn("run.started", event_types)

    def test_in_place_mutation_after_final_source_check_rejects_popen(self):
        code, outcome, events, command, lease = (
            _run_v2_real_child_integration(
                mutate_launcher_during_final_preflight=True
            )
        )

        self.assertEqual(code, 3)
        self.assertEqual(lease.test_popen_mock.call_count, 0)
        self.assertTrue(outcome["final_launch_preflight"]["passed"])
        self.assertTrue(
            outcome["libcuda_final_revalidation"]["passed"]
        )
        self.assertFalse(outcome["launcher_fd_final"]["passed"])
        self.assertFalse(
            outcome["launcher_fd_final"]["checks"][
                "identity_matches_initial_open"
            ]
        )
        self.assertEqual(
            command["launcher_fd_final"],
            outcome["launcher_fd_final"],
        )
        event_types = [event["event_type"] for event in events]
        self.assertIn("run.final_launch_preflight", event_types)
        self.assertNotIn("run.started", event_types)

    def test_malformed_post_gpu_record_is_structured_and_quarantined(self):
        malformed = {
            "index": 3,
            "name": "GPU",
            "uuid": "uuid",
            "memory_used_mib": True,
        }
        code, outcome, _events, _command, lease = (
            _run_v2_real_child_integration(post_gpu=malformed)
        )

        self.assertEqual(code, 3)
        self.assertFalse(outcome["local_probe_passed"])
        self.assertTrue(outcome["quarantine_required"])
        self.assertIn(
            "malformed fields: memory_used_mib",
            outcome["post_health"]["error"],
        )
        self.assertFalse(
            outcome["post_health"]["checks"]["memory_safe_after_probe"]
        )
        self.assertIn(
            "gpu_memory_record_invalid_after_probe",
            outcome["quarantine_reasons"],
        )
        self.assertIn(
            "post_health_query_failed",
            outcome["quarantine_reasons"],
        )
        self.assertTrue(lease.quarantines)

    def test_oversized_stdout_is_bounded_before_text_allocation(self):
        code, outcome, _events, _command, _lease = (
            _run_v2_real_child_integration(oversized_stdout=True)
        )
        self.assertEqual(code, 3)
        self.assertFalse(outcome["local_probe_passed"])
        self.assertFalse(outcome["accepted"])
        self.assertIn(
            "post-child bounded log capture failed",
            outcome["child_launch_error"],
        )
        self.assertIn(
            "exceeds the",
            outcome["child_launch_error"],
        )

    def test_terminal_event_write_fault_downgrades_and_quarantines(self):
        raised, outcome, _events, _command, lease = (
            _run_v2_real_child_integration(terminal_event_failure=True)
        )

        self.assertIsInstance(raised, OSError)
        self.assertFalse(outcome["accepted"])
        self.assertFalse(outcome["local_probe_passed"])
        self.assertTrue(outcome["quarantine_required"])
        self.assertIn(
            "terminal_artifact_write_failed",
            outcome["quarantine_reasons"],
        )
        self.assertEqual(outcome["exit_code"], 3)
        self.assertEqual(raised.burstserve_exit_code, 3)
        self.assertTrue(lease.quarantines)
        self.assertIn(
            "terminal_artifact_write_failed",
            lease.quarantines[-1][0],
        )

    def test_terminal_directory_durability_fault_keeps_masked_poison(self):
        for target in ("outcome", "event"):
            for failure in ("open", "fsync"):
                with self.subTest(target=target, failure=failure):
                    raised, outcome, events, _command, lease = (
                        _run_v2_real_child_integration(
                            mode="global",
                            terminal_directory_failure=failure,
                            terminal_directory_target=target,
                        )
                    )
                    self.assertIsInstance(raised, OSError)
                    self.assertEqual(raised.burstserve_exit_code, 3)
                    self.assertEqual(outcome["exit_code"], 3)
                    self.assertFalse(outcome["accepted"])
                    self.assertFalse(outcome["local_probe_passed"])
                    self.assertTrue(outcome["quarantine_required"])
                    self.assertIn(
                        "terminal_artifact_write_failed",
                        outcome["quarantine_reasons"],
                    )
                    self.assertEqual(
                        events[-1]["event_type"],
                        "run.failed",
                    )
                    self.assertEqual(events[-1]["payload"], outcome)
                    self.assertTrue(lease.masked_poison_armed)
                    self.assertFalse(lease.masked_poison_cleared)
                    self.assertTrue(lease.quarantines)

    def test_handler_restore_failure_downgrades_disk_and_exit(self):
        raised, outcome, events, _command, lease = (
            _run_v2_real_child_integration(
                restore_handler_failure=True,
            )
        )
        self.assertIsInstance(raised, OSError)
        self.assertEqual(raised.burstserve_exit_code, 3)
        self.assertEqual(outcome["exit_code"], 3)
        self.assertFalse(outcome["accepted"])
        self.assertFalse(outcome["local_probe_passed"])
        self.assertIn(
            "terminal_signal_handler_restore_failed",
            outcome["quarantine_reasons"],
        )
        self.assertEqual(events[-1]["event_type"], "run.failed")
        self.assertEqual(events[-1]["payload"], outcome)
        self.assertTrue(lease.quarantines)

    def test_signal_mask_restore_failure_downgrades_disk_and_exit(self):
        raised, outcome, events, _command, _lease = (
            _run_v2_real_child_integration(
                mask_restore_failure=True,
            )
        )
        self.assertIsInstance(raised, OSError)
        self.assertEqual(raised.burstserve_exit_code, 3)
        self.assertEqual(outcome["exit_code"], 3)
        self.assertIn(
            "terminal_signal_mask_restore_failed",
            outcome["quarantine_reasons"],
        )
        self.assertEqual(events[-1]["event_type"], "run.failed")
        self.assertEqual(events[-1]["payload"], outcome)

    def test_pending_terminal_signal_is_durable_before_unmask(self):
        raised, outcome, events, _command, _lease = (
            _run_v2_real_child_integration(
                pending_signum=signal.SIGTERM,
            )
        )
        self.assertIsInstance(
            raised,
            smctrl_runner._ChildWindowInterrupted,
        )
        self.assertEqual(
            raised.burstserve_exit_code,
            128 + signal.SIGTERM,
        )
        self.assertEqual(outcome["exit_code"], 128 + signal.SIGTERM)
        self.assertEqual(
            outcome["child_interruption"]["signal"],
            signal.SIGTERM,
        )
        self.assertIn(
            "terminal_signal_pending_before_restore",
            outcome["quarantine_reasons"],
        )
        self.assertEqual(events[-1]["event_type"], "run.failed")
        self.assertEqual(events[-1]["payload"], outcome)

    def test_masked_launch_commit_reservation_rejects_before_popen(self):
        code, outcome, events, _command, lease = (
            _run_v2_real_child_integration(
                mode="global",
                reject_launch_commit_reservation=True,
            )
        )
        commit = outcome["launch_commit_reservation_revalidation"]
        self.assertEqual(code, 3)
        self.assertEqual(lease.test_popen_mock.call_count, 0)
        self.assertTrue(outcome["final_launch_preflight"]["passed"])
        self.assertTrue(commit["required_for_mode"])
        self.assertGreater(commit["required_horizon_s"], 10.0)
        self.assertFalse(commit["passed"])
        self.assertFalse(
            commit["checks"]["synthetic_launch_commit_rejection"]
        )
        self.assertIn(
            "reservation_invalid_at_popen_commit",
            outcome["quarantine_reasons"],
        )
        self.assertTrue(lease.masked_poison_armed)
        self.assertFalse(lease.masked_poison_cleared)
        self.assertNotIn(
            "run.started",
            [event["event_type"] for event in events],
        )

    def test_signal_delivered_during_unmask_window_is_durable(self):
        raised, outcome, events, _command, _lease = (
            _run_v2_real_child_integration(
                deliver_signum_during_unmask=signal.SIGTERM,
            )
        )
        self.assertIsInstance(
            raised,
            smctrl_runner._ChildWindowInterrupted,
        )
        self.assertEqual(
            raised.burstserve_exit_code,
            128 + signal.SIGTERM,
        )
        self.assertEqual(outcome["exit_code"], 128 + signal.SIGTERM)
        self.assertIn(
            "terminal_signal_delivered_before_handler_restore",
            outcome["quarantine_reasons"],
        )
        self.assertEqual(events[-1]["event_type"], "run.failed")
        self.assertEqual(events[-1]["payload"], outcome)


class RealProcessLifecycleTest(unittest.TestCase):
    def _spawn(self, source: str) -> subprocess.Popen[str]:
        return subprocess.Popen(
            ["/usr/bin/python3", "-c", source],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )

    def test_real_child_normal_completion_is_quiescent_and_reaped(self):
        process = self._spawn("import time; time.sleep(0.05)")
        identity = smctrl_runner._capture_process_identity(process.pid)
        health = smctrl_runner._supervise_process(
            process,
            identity,
            timeout_s=2,
        )
        self.assertFalse(health["timed_out"])
        self.assertTrue(health["process_group_reaped"])
        self.assertIsNotNone(process.returncode)

    def test_real_child_timeout_is_killed_and_reaped(self):
        process = self._spawn("import time; time.sleep(30)")
        identity = smctrl_runner._capture_process_identity(process.pid)
        health = smctrl_runner._supervise_process(
            process,
            identity,
            timeout_s=0.01,
        )
        self.assertTrue(health["timed_out"])
        self.assertTrue(health["process_group_reaped"])
        self.assertIsNotNone(process.returncode)

    def test_real_identityless_cleanup_kills_only_its_own_child(self):
        process = self._spawn("import time; time.sleep(30)")
        health = smctrl_runner._reap_spawn_without_identity(process)
        self.assertTrue(health["child_reaped"])
        self.assertFalse(health["process_group_reaped"])
        self.assertIsNotNone(process.returncode)

    def test_real_descendant_is_removed_before_leader_reap(self):
        source = (
            "import subprocess,time;"
            "subprocess.Popen(['/usr/bin/python3','-c',"
            "'import time; time.sleep(30)']);"
            "time.sleep(0.05)"
        )
        process = self._spawn(source)
        identity = smctrl_runner._capture_process_identity(process.pid)
        health = smctrl_runner._supervise_process(
            process,
            identity,
            timeout_s=2,
        )
        self.assertTrue(health["process_group_reaped"])
        self.assertEqual(health["descendants_after_waitable"], [])
        self.assertIsNotNone(process.returncode)


def _minimal_native_build_attestation() -> dict[str, object]:
    value: dict[str, object] = {
        key: {}
        for key in smctrl_runner.NATIVE_BUILD_ATTESTATION_TOP_LEVEL_KEYS
    }
    value["schema_version"] = (
        smctrl_runner.NATIVE_BUILD_ATTESTATION_SCHEMA_VERSION
    )
    return value


class BuildNativeSafetyTest(unittest.TestCase):
    def test_python_version_recheck_uses_isolated_no_site_argv(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=b"Python 3.test\n",
            stderr=b"",
        )
        environment = {
            "PATH": "/usr/bin:/bin",
            "LANG": "C",
            "LC_ALL": "C",
            "TZ": "UTC",
        }
        with mock.patch.object(
            smctrl_runner.subprocess,
            "run",
            return_value=completed,
        ) as run:
            digest = smctrl_runner._command_version_sha256(
                str(Path(sys.executable).resolve()),
                environment=environment,
                version_arguments=(
                    smctrl_runner._tool_version_arguments(
                        "PYTHON_EXECUTABLE"
                    )
                ),
            )

        self.assertEqual(
            run.call_args.args[0],
            [
                str(Path(sys.executable).resolve()),
                "-I",
                "-S",
                "-B",
                "--version",
            ],
        )
        self.assertEqual(run.call_args.kwargs["env"], environment)
        self.assertEqual(
            digest,
            hashlib.sha256(
                b"Python 3.test\n\0"
            ).hexdigest(),
        )

    def test_pinned_attestation_verifier_uses_isolated_no_site_python(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            script = root / "native" / "smctrl_probe" / "build_attestation.py"
            script.parent.mkdir(parents=True)
            script.write_text("raise SystemExit(0)\n", encoding="utf-8")
            stamp = root / "build" / "build-config.stamp"
            stamp.parent.mkdir(parents=True)
            stamp.write_text("synthetic\n", encoding="utf-8")
            attestation = stamp.parent / "build-attestation.json"
            attestation.write_text("{}\n", encoding="utf-8")
            fields = {
                name: "synthetic"
                for name in smctrl_runner.BUILD_STAMP_FIELDS
            }
            fields.update(
                {
                    "PYTHON_EXECUTABLE": str(Path(sys.executable).resolve()),
                    "BUILD_TMPDIR": str(root / "tmp"),
                    "BUILD_LOCK_PATH": str(root / "build.lock"),
                    "HERMETIC_PATH": "/usr/bin:/bin",
                    "CUDA_HOME": "/usr/local/cuda-13.3",
                    "CUDA_ARCH": "89",
                    "NVCC": "/usr/local/cuda-13.3/bin/nvcc",
                    "CC": "/usr/bin/cc",
                    "AR": "/usr/bin/ar",
                    "LIBSMCTRL_DIR": str(root / "vendor/libsmctrl"),
                }
            )
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=b"",
                stderr=b"",
            )
            contaminants = {
                "PYTHONPATH": str(root / "attacker"),
                "PYTHONHOME": str(root / "attacker-home"),
                "LD_PRELOAD": str(root / "attacker.so"),
            }
            with (
                mock.patch.dict(os.environ, contaminants, clear=False),
                mock.patch.object(
                    smctrl_runner.subprocess,
                    "run",
                    return_value=completed,
                ) as run,
            ):
                result = (
                    smctrl_runner._verify_attestation_with_pinned_builder(
                        repo_root=root,
                        stamp_fields=fields,
                        stamp_path=stamp,
                        attestation_path=attestation,
                    )
                )

        self.assertTrue(result["passed"])
        command = run.call_args.args[0]
        self.assertEqual(
            command[:5],
            [
                str(Path(sys.executable).resolve()),
                "-I",
                "-S",
                "-B",
                mock.ANY,
            ],
        )
        self.assertRegex(command[4], r"^/proc/self/fd/[0-9]+$")
        environment = run.call_args.kwargs["env"]
        self.assertTrue(set(contaminants).isdisjoint(environment))
        self.assertNotIn("HOME", environment)
        self.assertEqual(
            run.call_args.kwargs["pass_fds"],
            (int(command[4].rsplit("/", 1)[1]),),
        )

    def test_isolated_no_site_python_ignores_sitecustomize_and_pth(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "injected"
            injection = (
                "from pathlib import Path; "
                f"Path({str(marker)!r}).write_text('injected')\n"
            )
            (root / "sitecustomize.py").write_text(
                injection,
                encoding="utf-8",
            )
            (root / "attacker.pth").write_text(
                f"import sitecustomize\n",
                encoding="utf-8",
            )
            environment = {
                "PATH": "/usr/bin:/bin",
                "LANG": "C",
                "LC_ALL": "C",
                "TZ": "UTC",
                "PYTHONPATH": str(root),
                "PYTHONUSERBASE": str(root),
            }
            completed = subprocess.run(
                [
                    str(Path(sys.executable).resolve()),
                    "-I",
                    "-S",
                    "-c",
                    "import sys; assert 'sitecustomize' not in sys.modules",
                ],
                check=False,
                capture_output=True,
                env=environment,
            )
            injected = marker.exists()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse(injected)

    def test_native_attestation_parser_accepts_only_canonical_schema(self):
        value = _minimal_native_build_attestation()
        content = smctrl_runner._native_attestation_canonical_bytes(value)
        self.assertEqual(
            smctrl_runner._parse_native_build_attestation(content),
            value,
        )

    def test_exact_sha_does_not_bypass_duplicate_attestation_key(self):
        value = _minimal_native_build_attestation()
        canonical = smctrl_runner._native_attestation_canonical_bytes(value)
        duplicate = canonical.replace(
            b'{"build_environment":{}',
            b'{"build_environment":{},"build_environment":{}',
            1,
        )
        self.assertNotEqual(duplicate, canonical)
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary) / "smid_probe"
            binary.parent.joinpath("build-attestation.json").write_bytes(
                duplicate
            )
            with self.assertRaisesRegex(RuntimeError, "duplicate key"):
                smctrl_runner._load_attestation_bootstrap(
                    binary,
                    expected_sha256=hashlib.sha256(duplicate).hexdigest(),
                )

    def test_exact_sha_does_not_bypass_noncanonical_attestation_bytes(self):
        value = _minimal_native_build_attestation()
        canonical = smctrl_runner._native_attestation_canonical_bytes(value)
        noncanonical = b" " + canonical
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary) / "smid_probe"
            binary.parent.joinpath("build-attestation.json").write_bytes(
                noncanonical
            )
            with self.assertRaisesRegex(RuntimeError, "not canonical"):
                smctrl_runner._load_attestation_bootstrap(
                    binary,
                    expected_sha256=hashlib.sha256(
                        noncanonical
                    ).hexdigest(),
                )

    def test_native_attestation_parser_rejects_unknown_top_level_key(self):
        value = _minimal_native_build_attestation()
        value["unknown"] = {}
        content = smctrl_runner._native_attestation_canonical_bytes(value)
        with self.assertRaisesRegex(RuntimeError, "top-level keys mismatch"):
            smctrl_runner._parse_native_build_attestation(content)

    def test_native_attestation_parser_rejects_wrong_schema(self):
        value = _minimal_native_build_attestation()
        value["schema_version"] = "burstserve.native-build-attestation/v999"
        content = smctrl_runner._native_attestation_canonical_bytes(value)
        with self.assertRaisesRegex(RuntimeError, "unsupported.*schema"):
            smctrl_runner._parse_native_build_attestation(content)

    def test_native_attestation_parser_rejects_oversized_document(self):
        content = b" " * (
            smctrl_runner.MAX_NATIVE_BUILD_ATTESTATION_BYTES + 1
        )
        with self.assertRaisesRegex(RuntimeError, "size limit"):
            smctrl_runner._parse_native_build_attestation(content)

    def test_native_attestation_parser_rejects_oversized_integer(self):
        value = _minimal_native_build_attestation()
        content = smctrl_runner._native_attestation_canonical_bytes(value)
        huge = b"9" * (
            smctrl_runner.MAX_NATIVE_BUILD_ATTESTATION_INTEGER_DIGITS + 1
        )
        content = content.replace(
            b'"build_environment":{}',
            b'"build_environment":{"huge":' + huge + b"}",
            1,
        )
        with self.assertRaisesRegex(RuntimeError, "digit limit"):
            smctrl_runner._parse_native_build_attestation(content)

    def test_native_attestation_parser_rejects_nonfinite_number(self):
        value = _minimal_native_build_attestation()
        canonical = smctrl_runner._native_attestation_canonical_bytes(value)
        for number in (b"NaN", b"Infinity", b"1e999999"):
            with self.subTest(number=number):
                content = canonical.replace(
                    b'"build_environment":{}',
                    b'"build_environment":{"bad":' + number + b"}",
                    1,
                )
                with self.assertRaisesRegex(
                    RuntimeError,
                    "non-standard constant|non-finite number",
                ):
                    smctrl_runner._parse_native_build_attestation(content)

    def test_native_attestation_parser_rejects_non_utf8_bytes(self):
        value = _minimal_native_build_attestation()
        content = smctrl_runner._native_attestation_canonical_bytes(value)
        with self.assertRaisesRegex(RuntimeError, "invalid.*JSON"):
            smctrl_runner._parse_native_build_attestation(
                content[:-1] + b"\xff\n"
            )

    def test_postbuild_reopen_rejects_same_bytes_on_new_inode(self):
        value = _minimal_native_build_attestation()
        content = smctrl_runner._native_attestation_canonical_bytes(value)
        original_load = smctrl_runner._load_attestation_bootstrap
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            binary = root / smctrl_runner.DEFAULT_BINARY
            binary.parent.mkdir(parents=True)
            attestation = binary.parent / "build-attestation.json"
            attestation.write_bytes(content)

            def replace_then_load(*args, **kwargs):
                replacement = attestation.with_name("replacement.json")
                replacement.write_bytes(content)
                os.replace(replacement, attestation)
                return original_load(*args, **kwargs)

            with mock.patch.object(
                smctrl_runner,
                "_load_attestation_bootstrap",
                side_effect=replace_then_load,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "identity changed after it was hashed",
                ):
                    smctrl_runner._validate_postbuild_attestation(root)

    def test_runner_stamp_schema_exactly_matches_native_order(self):
        path = (
            Path(__file__).resolve().parents[1]
            / "native"
            / "smctrl_probe"
            / "build_attestation.py"
        )
        specification = importlib.util.spec_from_file_location(
            "burstserve_test_native_attestation_schema",
            path,
        )
        self.assertIsNotNone(specification)
        self.assertIsNotNone(specification.loader)
        module = importlib.util.module_from_spec(specification)
        sys.modules[specification.name] = module
        try:
            specification.loader.exec_module(module)
        finally:
            sys.modules.pop(specification.name, None)

        self.assertEqual(
            len(smctrl_runner.BUILD_STAMP_FIELDS),
            len(set(smctrl_runner.BUILD_STAMP_FIELDS)),
        )
        self.assertEqual(
            smctrl_runner.BUILD_STAMP_FIELDS,
            tuple(module.STAMP_FIELD_ORDER),
        )

    def test_parent_guard_test_source_is_in_the_formal_stamp_contract(self):
        field = "TEST_NATIVE_PARENT_GUARD_PY_SHA256"
        self.assertIn(field, smctrl_runner.BUILD_STAMP_FIELDS)
        self.assertEqual(
            smctrl_runner.BUILD_SOURCE_PATHS[field],
            Path("tests/test_native_parent_guard.py"),
        )

    def test_external_build_source_is_rejected_before_make(self):
        with mock.patch.object(smctrl_runner.subprocess, "run") as run:
            with self.assertRaisesRegex(
                RuntimeError,
                "repository-canonical source",
            ):
                smctrl_runner.build_native(
                    repo_root=Path("/synthetic/repo"),
                    source_directory=Path("/synthetic/external"),
                    jobs=None,
                )
        run.assert_not_called()

    def test_formal_make_uses_exact_environment_and_attestation_parser(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                b"some build output\n"
                + smctrl_runner.NATIVE_GATE_COMPLETION_SENTINEL.encode()
                + b"\n"
            ),
            stderr=b"",
        )
        contaminants = {
            "BASH_ENV": "/tmp/evil-bash-env",
            "ENV": "/tmp/evil-sh-env",
            "LD_PRELOAD": "/tmp/evil.so",
            "GLIBC_TUNABLES": "glibc.rtld.nns=99",
            "GCONV_PATH": "/tmp/gconv",
            "LOCPATH": "/tmp/locale",
            "NLSPATH": "/tmp/messages",
            "SHELLOPTS": "xtrace",
            "BASHOPTS": "extdebug",
            "BASH_FUNC_evil%%": "() { :; }",
            "MAKEFLAGS": "-n",
            "GNUMAKEFLAGS": "-i",
            "MAKEFILES": "/tmp/evil.mk",
            "MFLAGS": "-t",
            "MAKEOVERRIDES": "CC",
        }
        with (
            mock.patch.dict(os.environ, contaminants, clear=False),
            mock.patch.object(
                smctrl_runner.subprocess,
                "run",
                return_value=completed,
            ) as run,
            mock.patch.object(
                smctrl_runner,
                "_validate_postbuild_attestation",
            ) as validate,
        ):
            code = smctrl_runner.build_native(
                repo_root=Path("/synthetic/repo"),
                source_directory=Path("/synthetic/repo/native/smctrl_probe"),
                jobs=4,
            )

        self.assertEqual(code, 0)
        validate.assert_called_once_with(Path("/synthetic/repo"))
        command = run.call_args.args[0]
        self.assertEqual(command[0], "/usr/bin/make")
        self.assertIn("gate-required-check", command)
        self.assertEqual(
            run.call_args.kwargs["env"],
            smctrl_runner.FORMAL_BUILD_ENVIRONMENT,
        )
        self.assertTrue(
            set(contaminants).isdisjoint(run.call_args.kwargs["env"])
        )

    def test_rc_zero_without_completion_sentinel_fails_closed(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=b"recipes were skipped\n",
            stderr=b"",
        )
        with (
            mock.patch.object(
                smctrl_runner.subprocess,
                "run",
                return_value=completed,
            ),
            mock.patch.object(
                smctrl_runner,
                "_validate_postbuild_attestation",
            ) as validate,
        ):
            code = smctrl_runner.build_native(
                repo_root=Path("/synthetic/repo"),
                source_directory=Path(
                    "/synthetic/repo/native/smctrl_probe"
                ),
                jobs=None,
            )
        self.assertEqual(code, 2)
        validate.assert_not_called()

    def test_attestation_parser_failure_rejects_a_sentinel_build(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                smctrl_runner.NATIVE_GATE_COMPLETION_SENTINEL.encode()
                + b"\n"
            ),
            stderr=b"",
        )
        with (
            mock.patch.object(
                smctrl_runner.subprocess,
                "run",
                return_value=completed,
            ),
            mock.patch.object(
                smctrl_runner,
                "_validate_postbuild_attestation",
                side_effect=RuntimeError("synthetic stale attestation"),
            ),
        ):
            code = smctrl_runner.build_native(
                repo_root=Path("/synthetic/repo"),
                source_directory=Path(
                    "/synthetic/repo/native/smctrl_probe"
                ),
                jobs=None,
            )
        self.assertEqual(code, 2)

    def test_real_make_cannot_inherit_bash_or_make_control_variables(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "native" / "smctrl_probe"
            source.mkdir(parents=True)
            source.joinpath("Makefile").write_text(
                "SHELL := /bin/bash\n"
                ".PHONY: gate-required-check\n"
                "gate-required-check:\n"
                "\t@printf '%s\\n' "
                f"'{smctrl_runner.NATIVE_GATE_COMPLETION_SENTINEL}'\n",
                encoding="utf-8",
            )
            bash_env = root / "bash-env"
            bash_env.write_text("exit 0\n", encoding="utf-8")
            contaminants = {
                "BASH_ENV": str(bash_env),
                "ENV": str(bash_env),
                "LD_PRELOAD": str(root / "missing-preload.so"),
                "MAKEFLAGS": "-n",
                "GNUMAKEFLAGS": "-i",
                "MAKEFILES": str(root / "missing.mk"),
            }
            with (
                mock.patch.dict(os.environ, contaminants, clear=False),
                mock.patch.object(
                    smctrl_runner,
                    "_validate_postbuild_attestation",
                ) as validate,
            ):
                code = smctrl_runner.build_native(
                    repo_root=root,
                    source_directory=source,
                    jobs=None,
                )
        self.assertEqual(code, 0)
        validate.assert_called_once_with(root)


class _FakeProcess:
    pid = 12345
    returncode = 0

    def __init__(self, stdout: str, stderr: str = "") -> None:
        self.stdout = stdout
        self.stderr = stderr

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        return self.stdout, self.stderr

    def kill(self) -> None:
        self.returncode = -signal.SIGKILL

    def wait(self, timeout: float | None = None) -> int:
        assert self.returncode is not None
        return self.returncode


class _FakeXidMonitor:
    def __init__(
        self,
        order: list[object],
        *,
        setup_error: BaseException | None = None,
        drain_error: BaseException | None = None,
        cleanup_error: BaseException | None = None,
        xid: int | None = None,
        provenance_overrides: dict[str, object] | None = None,
        enter_callback=None,
        drain_callback=None,
    ) -> None:
        self.order = order
        self.setup_error = setup_error
        self.drain_error = drain_error
        self.cleanup_error = cleanup_error
        self.xid = xid
        self.provenance_overrides = provenance_overrides or {}
        self.enter_callback = enter_callback
        self.drain_callback = drain_callback
        self.physical_uuid = ""
        self.setup_succeeded = False
        self.drain_succeeded = False
        self.closed = False
        self.cleanup_errors: list[dict[str, object]] = []

    def __enter__(self):
        self.order.append(("monitor.enter", self.physical_uuid))
        if self.setup_error is not None:
            raise self.setup_error
        self.setup_succeeded = True
        if self.enter_callback is not None:
            self.enter_callback()
        return self

    def drain(
        self,
        timeout_ms: int,
        *,
        max_events: int = 1024,
        maximum_total_ms: int | None = None,
        fail_fast_on_event: bool = False,
    ):
        self.order.append(("monitor.drain", timeout_ms))
        self.drain_options = {
            "max_events": max_events,
            "maximum_total_ms": maximum_total_ms,
            "fail_fast_on_event": fail_fast_on_event,
        }
        if self.drain_error is not None:
            raise self.drain_error
        if self.drain_callback is not None:
            self.drain_callback()
        self.drain_succeeded = True
        return []

    def close(self) -> None:
        self.order.append("monitor.close")
        if self.cleanup_error is not None:
            self.cleanup_errors.append(
                {
                    "operation": "nvmlEventSetFree",
                    "message": str(self.cleanup_error),
                }
            )
        self.closed = True

    def to_provenance(self) -> dict[str, object]:
        events = (
            [
                {
                    "event_type_bits": 8,
                    "xid_code": self.xid,
                    "gpu_instance_id": 0xFFFFFFFF,
                    "compute_instance_id": 0xFFFFFFFF,
                }
            ]
            if self.xid is not None
            else []
        )
        safe = (
            self.setup_succeeded
            and self.drain_succeeded
            and self.closed
            and self.drain_error is None
            and self.xid is None
            and not self.cleanup_errors
        )
        provenance = {
            "schema_version": "burstserve.nvml-xid-monitor/v1",
            "physical_uuid": self.physical_uuid,
            "xid_event_bit": 8,
            "supported_event_bits": 8 if self.setup_succeeded else None,
            "registered_event_bits": 8 if self.setup_succeeded else 0,
            "setup": {
                "succeeded": self.setup_succeeded,
                "error": (
                    str(self.setup_error)
                    if self.setup_error is not None
                    else None
                ),
            },
            "drain": {
                "timeout_ms": 1000 if self.drain_succeeded else None,
                "requested_quiet_ms": (
                    1000 if self.drain_succeeded else None
                ),
                "observed_quiet_ms": (
                    1000.0 if self.drain_succeeded else None
                ),
                "succeeded": self.drain_succeeded,
                "error": (
                    str(self.drain_error)
                    if self.drain_error is not None
                    else None
                ),
            },
            "cleanup": {"errors": self.cleanup_errors},
            "events": events,
            "xids_seen": len(events),
            "safe_for_acceptance": safe,
        }
        provenance.update(self.provenance_overrides)
        return provenance


class _TermThenKillProcess:
    pid = 23456
    returncode = -signal.SIGKILL

    def __init__(self) -> None:
        self.communicate_timeouts: list[float | None] = []

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        self.communicate_timeouts.append(timeout)
        if len(self.communicate_timeouts) == 1:
            raise RuntimeError("child pipe failed")
        if len(self.communicate_timeouts) == 2:
            raise subprocess.TimeoutExpired(["probe"], timeout)
        return "", ""


class _KeyboardInterruptProcess:
    pid = 34567

    def __init__(
        self,
        interruption: BaseException | None = None,
    ) -> None:
        self.returncode: int | None = None
        self.communicate_timeouts: list[float | None] = []
        self.interruption = interruption or KeyboardInterrupt()

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        self.communicate_timeouts.append(timeout)
        if len(self.communicate_timeouts) == 1:
            raise self.interruption
        self.returncode = -signal.SIGTERM
        return "", ""


class _NeverReapedProcess:
    pid = 45678
    returncode = None

    def __init__(self) -> None:
        self.communicate_timeouts: list[float | None] = []

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        self.communicate_timeouts.append(timeout)
        if len(self.communicate_timeouts) == 1:
            raise RuntimeError("pipe failed permanently")
        raise subprocess.TimeoutExpired(["probe"], timeout)


def _run_masked_execute(
    monitor: _FakeXidMonitor,
    *,
    process_override: object | None = None,
    external_binary: bool = False,
    external_libsmctrl: bool = False,
    stamp_overrides: dict[str, str] | None = None,
    include_approval_pins: bool = True,
    mutate_binary_during_environment_capture: bool = False,
    mutate_binary_during_monitor_setup: bool = False,
    capture_base_exception: bool = False,
) -> tuple[
    int | BaseException,
    dict[str, object],
    list[dict[str, object]],
    mock.Mock,
    list[object],
]:
    order = monitor.order
    native = _native(
        mode="global",
        sm_count=128,
        histogram={"17": 4096},
        blocks=4096,
        parent_pid=os.getpid(),
    )
    native["device"]["name"] = "GPU"
    process = process_override or _FakeProcess(json.dumps(native) + "\n")
    gpu = {
        "index": 3,
        "name": "GPU",
        "uuid": "uuid",
        "pci_bus_id": "bus",
        "memory_total_mib": 24564,
        "memory_used_mib": 0,
        "utilization_gpu_percent": 0,
        "driver_version": "test",
    }

    def monitor_factory(uuid: str):
        order.append(("monitor.construct", uuid))
        monitor.physical_uuid = uuid
        return monitor

    def popen_factory(*_args, **_kwargs):
        order.append("popen")
        return process

    gpu_query_count = 0

    def query_gpu_factory(_physical_gpu: int):
        nonlocal gpu_query_count
        gpu_query_count += 1
        if gpu_query_count == 3:
            order.append("post_health.gpu")
        return gpu

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        binary = (
            root / "external" / "smid_probe"
            if external_binary
            else root / "build" / "smctrl_probe" / "smid_probe"
        )
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"fake")
        config = _config(
            mode="global",
            experimental_allow=True,
            promoted=True,
        )
        libsmctrl = _write_formal_source_fixture(
            root,
            binary,
            config,
            libsmctrl=(
                root / "external" / "libsmctrl"
                if external_libsmctrl
                else None
            ),
            include_approval_pins=include_approval_pins,
            stamp_overrides=stamp_overrides,
        )

        def capture_environment_factory(**_kwargs):
            if mutate_binary_during_environment_capture:
                binary.write_bytes(b"mutated-after-initial-binding")
            return {"env": "test"}

        if mutate_binary_during_monitor_setup:
            monitor.enter_callback = lambda: binary.write_bytes(
                b"mutated-after-monitor-registration"
            )

        with (
            mock.patch(
                "burstserve.smctrl_runner.latest_pinned_driver_version",
                return_value=12080,
            ),
            mock.patch(
                "burstserve.smctrl_runner.query_cuda_driver_version",
                return_value=13030,
            ),
            mock.patch(
                "burstserve.smctrl_runner.capture_probe_environment",
                side_effect=capture_environment_factory,
            ),
            mock.patch(
                "burstserve.smctrl_runner.source_revision",
                return_value="test-revision",
            ),
            mock.patch(
                "burstserve.smctrl_runner.query_mps_processes",
                return_value=[],
            ),
            mock.patch(
                "burstserve.smctrl_runner.query_compute_processes",
                return_value=[],
            ),
            mock.patch(
                "burstserve.smctrl_runner.query_gpu",
                side_effect=query_gpu_factory,
            ),
            mock.patch(
                "burstserve.smctrl_runner.validate_gate_manifest_record",
                side_effect=lambda value, **_: value["content"],
            ),
            mock.patch(
                "burstserve.smctrl_runner.NvmlXidMonitor",
                side_effect=monitor_factory,
            ),
            mock.patch(
                "burstserve.smctrl_runner.subprocess.Popen",
                side_effect=popen_factory,
            ) as popen,
        ):
            try:
                code, run_directory = execute(
                    repo_root=root,
                    binary=binary,
                    libsmctrl_root=libsmctrl,
                    run_root=root / "runs",
                    config=config,
                    timeout_s=10,
                    maximum_used_mib=1024,
                    allow_busy_gpu=False,
                )
            except BaseException as error:
                if not capture_base_exception:
                    raise
                code = error
                run_directories = list((root / "runs").iterdir())
                if len(run_directories) != 1:
                    raise AssertionError(
                        "interrupted execution did not leave exactly one run"
                    ) from error
                run_directory = run_directories[0]
        outcome = json.loads((run_directory / "outcome.json").read_text())
        events = [
            json.loads(line)
            for line in (run_directory / "events.jsonl").read_text().splitlines()
        ]
    return code, outcome, events, popen, order


@unittest.skip(
    "legacy fake-Popen harness predates Gate-A v2 attestation, regular log "
    "files, UUID leases, and waitid-bound process identity; retained only as "
    "a historical fixture until it is removed"
)
class ExecuteTest(unittest.TestCase):
    def test_masked_monitor_registers_before_popen_and_clean_is_local_only(
        self,
    ) -> None:
        order: list[object] = []
        monitor = _FakeXidMonitor(order)

        code, outcome, events, popen, observed_order = _run_masked_execute(
            monitor
        )

        self.assertEqual(code, 0)
        self.assertEqual(popen.call_count, 1)
        self.assertLess(
            observed_order.index(("monitor.enter", "uuid")),
            observed_order.index("popen"),
        )
        self.assertEqual(
            observed_order[-2:],
            [("monitor.drain", 1000), "monitor.close"],
        )
        self.assertLess(
            observed_order.index("post_health.gpu"),
            observed_order.index(("monitor.drain", 1000)),
        )
        self.assertEqual(
            popen.call_args.kwargs["env"]["BURSTSERVE_PARENT_PID"],
            str(os.getpid()),
        )
        self.assertTrue(outcome["local_probe_passed"])
        self.assertFalse(outcome["accepted"])
        self.assertTrue(outcome["requires_matrix_validation"])
        self.assertEqual(outcome["masked_health_monitor_status"], "clean")
        self.assertTrue(
            outcome["masked_health_monitor"]["provenance"][
                "safe_for_acceptance"
            ]
        )
        self.assertEqual(
            outcome["masked_health_monitor"]["post_probe_drain_timeout_ms"],
            1000,
        )
        self.assertTrue(outcome["masked_health_monitor_checks"])
        self.assertTrue(
            all(outcome["masked_health_monitor_checks"].values())
        )
        event_types = [event["event_type"] for event in events]
        self.assertEqual(
            event_types,
            [
                "run.preflight",
                "run.source_revalidated",
                "masked_health_monitor.registered",
                "run.source_preexec_revalidated",
                "run.started",
                "masked_health_monitor.drained",
                "run.source_postvalidated",
                "run.completed",
            ],
        )

    def test_masked_xid_rejects_local_probe(self) -> None:
        monitor = _FakeXidMonitor([], xid=79)

        code, outcome, _events, popen, _order = _run_masked_execute(monitor)

        self.assertEqual(popen.call_count, 1)
        self.assertEqual(code, 3)
        self.assertEqual(
            outcome["masked_health_monitor_status"], "xid_observed"
        )
        self.assertEqual(
            outcome["masked_health_monitor"]["provenance"]["events"][0][
                "xid_code"
            ],
            79,
        )
        self.assertFalse(outcome["local_probe_passed"])
        self.assertFalse(outcome["accepted"])

    def test_masked_monitor_setup_failure_prevents_popen(self) -> None:
        monitor = _FakeXidMonitor(
            [],
            setup_error=RuntimeError("registration denied"),
        )

        code, outcome, events, popen, order = _run_masked_execute(monitor)

        self.assertEqual(code, 3)
        self.assertEqual(popen.call_count, 0)
        self.assertNotIn("popen", order)
        self.assertIn("monitor.close", order)
        self.assertEqual(
            outcome["masked_health_monitor_status"], "setup_failed"
        )
        self.assertIsNone(outcome["process_exit_code"])
        self.assertFalse(outcome["local_probe_passed"])
        self.assertFalse(outcome["accepted"])
        self.assertEqual(
            [event["event_type"] for event in events],
            [
                "run.preflight",
                "run.source_revalidated",
                "masked_health_monitor.setup_failed",
                "run.failed",
            ],
        )

    def test_base_exception_during_monitor_setup_closes_and_writes_evidence(
        self,
    ) -> None:
        monitor = _FakeXidMonitor(
            [],
            setup_error=KeyboardInterrupt(),
        )

        raised, outcome, events, popen, order = _run_masked_execute(
            monitor,
            capture_base_exception=True,
        )

        self.assertIsInstance(raised, KeyboardInterrupt)
        self.assertEqual(popen.call_count, 0)
        self.assertIn("monitor.close", order)
        self.assertEqual(outcome["exit_code"], 130)
        self.assertEqual(
            outcome["masked_health_monitor_status"], "setup_failed"
        )
        self.assertEqual(events[-1]["event_type"], "run.failed")

    def test_masked_monitor_drain_and_cleanup_failures_reject_local_probe(
        self,
    ) -> None:
        cases = (
            _FakeXidMonitor([], drain_error=RuntimeError("wait failed")),
            _FakeXidMonitor([], cleanup_error=RuntimeError("free failed")),
        )
        for monitor in cases:
            with self.subTest(
                failure=(
                    "drain"
                    if monitor.drain_error is not None
                    else "cleanup"
                )
            ):
                code, outcome, _events, popen, _order = (
                    _run_masked_execute(monitor)
                )
                self.assertEqual(popen.call_count, 1)
                self.assertEqual(code, 3)
                self.assertEqual(
                    outcome["masked_health_monitor_status"],
                    "monitor_failed",
                )
                self.assertFalse(outcome["local_probe_passed"])
                self.assertFalse(outcome["accepted"])

    def test_tampered_monitor_verdict_cannot_bypass_runner_checks(self) -> None:
        cases = (
            (
                "unregistered_xid",
                {
                    "registered_event_bits": 0,
                    "safe_for_acceptance": True,
                },
                "xid_bit_registered",
            ),
            (
                "hidden_xid_event",
                {
                    "events": [
                        {
                            "event_type_bits": 8,
                            "xid_code": 79,
                            "gpu_instance_id": 0xFFFFFFFF,
                            "compute_instance_id": 0xFFFFFFFF,
                        }
                    ],
                    "xids_seen": 0,
                    "safe_for_acceptance": True,
                },
                "events_contain_no_xid",
            ),
            (
                "wrong_uuid",
                {
                    "physical_uuid": "GPU-other",
                    "safe_for_acceptance": True,
                },
                "physical_gpu_uuid_exact",
            ),
        )
        for name, overrides, failed_check in cases:
            with self.subTest(name=name):
                monitor = _FakeXidMonitor(
                    [],
                    provenance_overrides=overrides,
                )
                code, outcome, _events, popen, _order = (
                    _run_masked_execute(monitor)
                )
                self.assertEqual(popen.call_count, 1)
                self.assertEqual(code, 3)
                self.assertTrue(
                    outcome["masked_health_monitor"]["provenance"][
                        "safe_for_acceptance"
                    ]
                )
                self.assertFalse(
                    outcome["masked_health_monitor_checks"][failed_check]
                )
                self.assertFalse(outcome["local_probe_passed"])
                self.assertFalse(outcome["accepted"])

    def test_child_cleanup_escalates_term_timeout_to_kill(self) -> None:
        process = _TermThenKillProcess()
        with (
            mock.patch("burstserve.smctrl_runner.os.killpg") as killpg,
            mock.patch(
                "burstserve.smctrl_runner._process_group_exists",
                return_value=(False, None),
            ),
        ):
            code, outcome, _events, popen, _order = _run_masked_execute(
                _FakeXidMonitor([]),
                process_override=process,
            )

        self.assertEqual(popen.call_count, 1)
        self.assertEqual(code, -signal.SIGKILL)
        self.assertEqual(
            killpg.call_args_list,
            [
                mock.call(process.pid, signal.SIGTERM),
                mock.call(process.pid, signal.SIGKILL),
            ],
        )
        self.assertEqual(process.communicate_timeouts, [10, 5.0])
        self.assertIn("RuntimeError: child pipe failed", outcome["child_launch_error"])
        self.assertFalse(outcome["local_probe_passed"])
        self.assertFalse(outcome["accepted"])

    def test_keyboard_interrupt_reaps_child_writes_artifacts_then_reraises(
        self,
    ) -> None:
        process = _KeyboardInterruptProcess()
        order: list[object] = []
        with (
            mock.patch("burstserve.smctrl_runner.os.killpg") as killpg,
            mock.patch(
                "burstserve.smctrl_runner._process_group_exists",
                return_value=(False, None),
            ),
        ):
            raised, outcome, events, popen, observed_order = (
                _run_masked_execute(
                    _FakeXidMonitor(order),
                    process_override=process,
                    capture_base_exception=True,
                )
            )

        self.assertIsInstance(raised, KeyboardInterrupt)
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(
            killpg.call_args_list,
            [
                mock.call(process.pid, signal.SIGTERM),
                mock.call(process.pid, signal.SIGKILL),
            ],
        )
        self.assertEqual(process.communicate_timeouts, [10, 5.0])
        self.assertTrue(outcome["process_group_reaped"])
        self.assertFalse(outcome["quarantine_required"])
        self.assertEqual(outcome["exit_code"], 130)
        self.assertEqual(
            outcome["child_interruption"]["type"], "KeyboardInterrupt"
        )
        self.assertFalse(outcome["local_probe_passed"])
        self.assertEqual(
            observed_order[-3:],
            ["post_health.gpu", ("monitor.drain", 1000), "monitor.close"],
        )
        self.assertEqual(events[-1]["event_type"], "run.failed")

    def test_system_exit_reaps_child_writes_artifacts_then_reraises(
        self,
    ) -> None:
        process = _KeyboardInterruptProcess(SystemExit(23))
        with (
            mock.patch("burstserve.smctrl_runner.os.killpg"),
            mock.patch(
                "burstserve.smctrl_runner._process_group_exists",
                return_value=(False, None),
            ),
        ):
            raised, outcome, events, popen, _order = _run_masked_execute(
                _FakeXidMonitor([]),
                process_override=process,
                capture_base_exception=True,
            )

        self.assertIsInstance(raised, SystemExit)
        self.assertEqual(raised.code, 23)
        self.assertEqual(popen.call_count, 1)
        self.assertTrue(outcome["process_group_reaped"])
        self.assertEqual(outcome["exit_code"], 23)
        self.assertEqual(
            outcome["child_interruption"]["type"], "SystemExit"
        )
        self.assertFalse(outcome["local_probe_passed"])
        self.assertEqual(events[-1]["event_type"], "run.failed")

    def test_sigterm_during_monitor_drain_is_deferred_until_evidence(
        self,
    ) -> None:
        monitor = _FakeXidMonitor([])

        def deliver_sigterm():
            handler = signal.getsignal(signal.SIGTERM)
            assert callable(handler)
            handler(signal.SIGTERM, None)

        monitor.drain_callback = deliver_sigterm
        raised, outcome, events, popen, order = _run_masked_execute(
            monitor,
            capture_base_exception=True,
        )

        self.assertIsInstance(
            raised,
            smctrl_runner._ChildWindowInterrupted,
        )
        self.assertEqual(raised.signum, signal.SIGTERM)
        self.assertEqual(popen.call_count, 1)
        self.assertIn("monitor.close", order)
        self.assertEqual(outcome["exit_code"], 128 + signal.SIGTERM)
        self.assertEqual(
            outcome["child_interruption"]["signal"], signal.SIGTERM
        )
        self.assertFalse(outcome["local_probe_passed"])
        self.assertTrue((events[-1]["event_type"] == "run.failed"))

    def test_permanently_unreaped_group_is_bounded_and_quarantined(
        self,
    ) -> None:
        process = _NeverReapedProcess()
        with (
            mock.patch("burstserve.smctrl_runner.os.killpg") as killpg,
            mock.patch(
                "burstserve.smctrl_runner._process_group_exists",
                return_value=(True, None),
            ),
            mock.patch(
                "burstserve.smctrl_runner._wait_for_process_group_exit",
                return_value=(False, None),
            ) as wait_for_group,
        ):
            code, outcome, _events, popen, _order = _run_masked_execute(
                _FakeXidMonitor([]),
                process_override=process,
            )

        self.assertEqual(popen.call_count, 1)
        self.assertEqual(code, 3)
        self.assertEqual(process.communicate_timeouts, [10, 5.0])
        self.assertGreaterEqual(
            killpg.call_args_list.count(
                mock.call(process.pid, signal.SIGKILL)
            ),
            2,
        )
        wait_for_group.assert_called_once_with(
            process.pid,
            timeout_s=5.0,
        )
        self.assertFalse(outcome["process_group_reaped"])
        self.assertTrue(outcome["quarantine_required"])
        self.assertEqual(
            outcome["masked_health_monitor_status"],
            "quarantine_unreaped_process_group",
        )
        self.assertFalse(
            outcome["masked_health_monitor"]["provenance"][
                "safe_for_acceptance"
            ]
        )
        self.assertFalse(outcome["local_probe_passed"])

    def test_xid_classification_has_priority_over_child_failure(self) -> None:
        process = _TermThenKillProcess()
        with (
            mock.patch("burstserve.smctrl_runner.os.killpg"),
            mock.patch(
                "burstserve.smctrl_runner._process_group_exists",
                return_value=(False, None),
            ),
        ):
            code, outcome, _events, _popen, _order = _run_masked_execute(
                _FakeXidMonitor([], xid=79),
                process_override=process,
            )

        self.assertEqual(code, -signal.SIGKILL)
        self.assertIn("RuntimeError", outcome["child_launch_error"])
        self.assertEqual(
            outcome["masked_health_monitor_status"], "xid_observed"
        )
        self.assertFalse(outcome["local_probe_passed"])

    def test_canonical_dirty_stamp_fails_closed_before_launch(self) -> None:
        code, outcome, _events, popen, order = _run_masked_execute(
            _FakeXidMonitor([]),
            stamp_overrides={"LIBSMCTRL_GIT_DIRTY": "dirty"},
        )

        self.assertEqual(code, 4)
        self.assertEqual(popen.call_count, 0)
        self.assertNotIn("popen", order)
        self.assertFalse(
            outcome["formal_source_checks"][
                "build_stamp_git_dirty_is_clean"
            ]
        )
        self.assertFalse(outcome["formal_source_preflight_permitted"])
        self.assertFalse(outcome["source_eligible_for_local_pass"])

    def test_canonical_masked_run_requires_approved_binary_and_stamp_pins(
        self,
    ) -> None:
        code, outcome, _events, popen, _order = _run_masked_execute(
            _FakeXidMonitor([]),
            include_approval_pins=False,
        )

        self.assertEqual(code, 4)
        self.assertEqual(popen.call_count, 0)
        self.assertFalse(
            outcome["formal_source_checks"][
                "approved_native_binary_sha256_declared"
            ]
        )
        self.assertFalse(
            outcome["formal_source_checks"][
                "approved_build_stamp_sha256_declared"
            ]
        )
        self.assertFalse(outcome["formal_source_preflight_permitted"])

    def test_binary_mutation_after_initial_binding_is_rejected_prelaunch(
        self,
    ) -> None:
        code, outcome, events, popen, order = _run_masked_execute(
            _FakeXidMonitor([]),
            mutate_binary_during_environment_capture=True,
        )

        self.assertEqual(code, 4)
        self.assertEqual(popen.call_count, 0)
        self.assertNotIn("popen", order)
        revalidation = outcome["source_prelaunch_revalidation"]
        self.assertTrue(revalidation["completed"])
        self.assertFalse(revalidation["snapshot_matches_initial"])
        self.assertFalse(revalidation["passed_for_launch"])
        self.assertEqual(
            [event["event_type"] for event in events],
            [
                "run.preflight",
                "run.source_revalidated",
                "run.rejected",
            ],
        )

    def test_mutation_after_monitor_setup_is_rejected_in_final_preexec(
        self,
    ) -> None:
        code, outcome, events, popen, order = _run_masked_execute(
            _FakeXidMonitor([]),
            mutate_binary_during_monitor_setup=True,
        )

        self.assertEqual(code, 3)
        self.assertEqual(popen.call_count, 0)
        self.assertNotIn("popen", order)
        final_check = outcome["source_preexec_revalidation"]
        self.assertTrue(final_check["completed"])
        self.assertFalse(final_check["snapshot_matches_initial"])
        self.assertFalse(final_check["passed_for_launch"])
        self.assertFalse(outcome["local_probe_passed"])
        self.assertNotIn(
            "run.started",
            [event["event_type"] for event in events],
        )

    def test_external_binary_is_rejected_before_masked_launch(self) -> None:
        code, outcome, _events, popen, _order = _run_masked_execute(
            _FakeXidMonitor([]),
            external_binary=True,
        )

        self.assertEqual(popen.call_count, 0)
        self.assertEqual(code, 4)
        self.assertFalse(outcome["formal_source_preflight_permitted"])
        self.assertFalse(outcome["source_eligible_for_local_pass"])
        self.assertFalse(
            outcome["formal_source_checks"]["binary_is_repo_canonical"]
        )
        self.assertTrue(
            outcome["formal_source_checks"][
                "libsmctrl_is_repo_canonical"
            ]
        )
        self.assertFalse(outcome["local_probe_passed"])

    def test_external_libsmctrl_is_rejected_before_masked_launch(
        self,
    ) -> None:
        code, outcome, _events, popen, _order = _run_masked_execute(
            _FakeXidMonitor([]),
            external_libsmctrl=True,
        )

        self.assertEqual(popen.call_count, 0)
        self.assertEqual(code, 4)
        self.assertFalse(outcome["formal_source_preflight_permitted"])
        self.assertFalse(outcome["source_eligible_for_local_pass"])
        self.assertTrue(
            outcome["formal_source_checks"]["binary_is_repo_canonical"]
        )
        self.assertFalse(
            outcome["formal_source_checks"][
                "libsmctrl_is_repo_canonical"
            ]
        )
        self.assertFalse(outcome["local_probe_passed"])

    def test_signal_handlers_raise_private_interruption_and_restore(self) -> None:
        original = {
            signum: signal.getsignal(signum)
            for signum in (signal.SIGTERM, signal.SIGHUP)
        }
        previous = smctrl_runner._install_child_signal_handlers()
        try:
            with self.assertRaises(smctrl_runner._ChildWindowInterrupted):
                handler = signal.getsignal(signal.SIGTERM)
                assert callable(handler)
                handler(signal.SIGTERM, None)
        finally:
            smctrl_runner._restore_signal_handlers(previous)
        self.assertEqual(
            {
                signum: signal.getsignal(signum)
                for signum in (signal.SIGTERM, signal.SIGHUP)
            },
            original,
        )

    @mock.patch(
        "burstserve.smctrl_runner.latest_pinned_driver_version",
        return_value=12080,
    )
    @mock.patch(
        "burstserve.smctrl_runner.query_cuda_driver_version",
        return_value=13030,
    )
    @mock.patch(
        "burstserve.smctrl_runner.capture_probe_environment",
        return_value={"env": "test"},
    )
    @mock.patch("burstserve.smctrl_runner.source_revision", return_value="test-revision")
    @mock.patch("burstserve.smctrl_runner.query_mps_processes", return_value=[])
    @mock.patch(
        "burstserve.smctrl_runner.query_compute_processes",
        return_value=[],
    )
    @mock.patch(
        "burstserve.smctrl_runner.query_gpu",
        return_value={
            "index": 3,
            "name": "GPU",
            "uuid": "uuid",
            "pci_bus_id": "bus",
            "memory_total_mib": 24564,
            "memory_used_mib": 0,
            "utilization_gpu_percent": 0,
            "driver_version": "test",
        },
    )
    def test_unknown_driver_rejection_is_provenance_complete(
        self,
        _gpu: mock.Mock,
        _processes: mock.Mock,
        _mps: mock.Mock,
        _revision: mock.Mock,
        _environment: mock.Mock,
        _driver: mock.Mock,
        _latest: mock.Mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "build" / "smctrl_probe" / "smid_probe"
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"fake")
            (binary.parent / "build-config.stamp").write_text(
                "test build\n",
                encoding="utf-8",
            )
            libsmctrl = root / "vendor" / "libsmctrl"
            libsmctrl.mkdir(parents=True)

            with mock.patch(
                "burstserve.smctrl_runner.validate_gate_manifest_record",
                side_effect=lambda value, **_: value["content"],
            ):
                code, run_directory = execute(
                    repo_root=root,
                    binary=binary,
                    libsmctrl_root=libsmctrl,
                    run_root=root / "runs",
                    config=_config(mode="stream"),
                    timeout_s=10,
                    maximum_used_mib=1024,
                    allow_busy_gpu=False,
                )

            self.assertEqual(code, 4)
            self.assertTrue((run_directory / "manifest.json").is_file())
            self.assertTrue((run_directory / "command.json").is_file())
            self.assertTrue((run_directory / "stdout.log").is_file())
            self.assertTrue((run_directory / "stderr.log").is_file())
            outcome = json.loads((run_directory / "outcome.json").read_text())
            self.assertFalse(outcome["driver_policy_permitted"])
            self.assertFalse(outcome["accepted"])

    @mock.patch(
        "burstserve.smctrl_runner.latest_pinned_driver_version",
        return_value=12080,
    )
    @mock.patch(
        "burstserve.smctrl_runner.query_cuda_driver_version",
        return_value=13030,
    )
    @mock.patch(
        "burstserve.smctrl_runner.capture_probe_environment",
        return_value={"env": "test"},
    )
    @mock.patch("burstserve.smctrl_runner.source_revision", return_value="test-revision")
    @mock.patch("burstserve.smctrl_runner.query_mps_processes", return_value=[])
    @mock.patch(
        "burstserve.smctrl_runner.query_compute_processes",
        return_value=[],
    )
    @mock.patch(
        "burstserve.smctrl_runner.query_gpu",
        return_value={
            "index": 3,
            "name": "GPU",
            "uuid": "uuid",
            "pci_bus_id": "bus",
            "memory_total_mib": 24564,
            "memory_used_mib": 0,
            "utilization_gpu_percent": 0,
            "driver_version": "test",
        },
    )
    def test_accepted_run_records_separate_stdout_and_stderr(
        self,
        _gpu: mock.Mock,
        _processes: mock.Mock,
        _mps: mock.Mock,
        _revision: mock.Mock,
        _environment: mock.Mock,
        _driver: mock.Mock,
        _latest: mock.Mock,
    ) -> None:
        native = _native(
            mode="baseline",
            sm_count=128,
            histogram={str(index): 32 for index in range(128)},
        )
        native["device"]["name"] = "GPU"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "build" / "smctrl_probe" / "smid_probe"
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"fake")
            baseline_config = _config(mode="baseline")
            libsmctrl = _write_formal_source_fixture(
                root,
                binary,
                baseline_config,
                include_approval_pins=False,
            )
            fake_process = _FakeProcess(json.dumps(native) + "\n", "diagnostic\n")

            with (
                mock.patch(
                    "burstserve.smctrl_runner.validate_gate_manifest_record",
                    side_effect=lambda value, **_: value["content"],
                ),
                mock.patch(
                    "burstserve.smctrl_runner.subprocess.Popen",
                    return_value=fake_process,
                ) as popen,
                mock.patch(
                    "burstserve.smctrl_runner.NvmlXidMonitor",
                ) as monitor_constructor,
            ):
                code, run_directory = execute(
                    repo_root=root,
                    binary=binary,
                    libsmctrl_root=libsmctrl,
                    run_root=root / "runs",
                    config=baseline_config,
                    timeout_s=10,
                    maximum_used_mib=1024,
                    allow_busy_gpu=False,
                )

            self.assertEqual(code, 0)
            monitor_constructor.assert_not_called()
            self.assertEqual(
                (run_directory / "stdout.log").read_text(),
                json.dumps(native) + "\n",
            )
            self.assertEqual(
                (run_directory / "stderr.log").read_text(),
                "diagnostic\n",
            )
            outcome = json.loads((run_directory / "outcome.json").read_text())
            self.assertTrue(outcome["local_probe_passed"])
            self.assertTrue(outcome["accepted"])
            self.assertFalse(outcome["requires_matrix_validation"])
            self.assertFalse(
                outcome["formal_source_checks"][
                    "approved_native_binary_sha256_declared"
                ]
            )
            self.assertNotIn(
                "approved_native_binary_sha256_declared",
                outcome["formal_source_required_checks"],
            )
            self.assertEqual(
                outcome["masked_health_monitor_status"],
                "not_applicable",
            )
            self.assertIsNone(outcome["masked_health_monitor"])
            child_environment = popen.call_args.kwargs["env"]
            self.assertEqual(child_environment["CUDA_VISIBLE_DEVICES"], "uuid")
            self.assertNotIn("MASK_OFF", child_environment)
            self.assertNotIn("BURSTSERVE_PARENT_PID", child_environment)

            _processes.side_effect = [
                [],
                [],
                RuntimeError("post-health query unavailable"),
            ]
            with (
                mock.patch(
                    "burstserve.smctrl_runner.validate_gate_manifest_record",
                    side_effect=lambda value, **_: value["content"],
                ),
                mock.patch(
                    "burstserve.smctrl_runner.subprocess.Popen",
                    return_value=fake_process,
                ),
            ):
                failed_code, failed_directory = execute(
                    repo_root=root,
                    binary=binary,
                    libsmctrl_root=libsmctrl,
                    run_root=root / "runs",
                    config=_config(mode="baseline", trial=1),
                    timeout_s=10,
                    maximum_used_mib=1024,
                    allow_busy_gpu=False,
                )
            failed = json.loads(
                (failed_directory / "outcome.json").read_text()
            )
            self.assertEqual(failed_code, 3)
            self.assertFalse(failed["local_probe_passed"])
            self.assertFalse(
                failed["post_health"]["checks"]["health_queries_completed"]
            )

            _processes.side_effect = None
            _processes.return_value = []
            with (
                mock.patch(
                    "burstserve.smctrl_runner.validate_gate_manifest_record",
                    side_effect=lambda value, **_: value["content"],
                ),
                mock.patch(
                    "burstserve.smctrl_runner.subprocess.Popen",
                    return_value=fake_process,
                ),
            ):
                busy_code, busy_directory = execute(
                    repo_root=root,
                    binary=binary,
                    libsmctrl_root=libsmctrl,
                    run_root=root / "runs",
                    config=_config(
                        mode="baseline",
                        trial=2,
                        allow_busy=True,
                    ),
                    timeout_s=10,
                    maximum_used_mib=1024,
                    allow_busy_gpu=True,
                )
            busy = json.loads((busy_directory / "outcome.json").read_text())
            self.assertEqual(busy_code, 0)
            self.assertTrue(busy["local_probe_passed"])
            self.assertFalse(busy["accepted"])


class FormalBuildInventoryPolicyTest(unittest.TestCase):
    OUTPUTS = {
        "launcher": ("build/smctrl_probe/smid_probe", 0o500),
        "real_probe": ("build/smctrl_probe/smid_probe.real", 0o500),
        "parent_guard_test_helper": (
            "build/smctrl_probe/parent_guard_test_helper",
            0o500,
        ),
        "real_probe_identity_header": (
            "build/smctrl_probe/real_probe_identity.h",
            0o400,
        ),
        "guard_exec_test_launcher": (
            "build/smctrl_probe/guard_exec_test_launcher",
            0o500,
        ),
        "guard_exec_test_fixture": (
            "build/smctrl_probe/guard_exec_test_launcher.real",
            0o500,
        ),
        "guard_exec_test_identity_header": (
            "build/smctrl_probe/guard_exec_test_identity.h",
            0o400,
        ),
    }

    def _entry(self, root: Path, relative: str) -> FilesystemEntry:
        path = root / relative
        status = os.lstat(path)
        if stat.S_ISDIR(status.st_mode):
            return FilesystemEntry(
                path=os.fsencode(relative),
                kind="directory",
                mode_octal=f"{stat.S_IMODE(status.st_mode):04o}",
                size=status.st_size,
                device=status.st_dev,
                inode=status.st_ino,
            )
        content = path.read_bytes()
        return FilesystemEntry(
            path=os.fsencode(relative),
            kind="regular",
            mode_octal=f"{stat.S_IMODE(status.st_mode):04o}",
            size=status.st_size,
            git_mode="100755" if status.st_mode & stat.S_IXUSR else "100644",
            git_oid=None,
            sha256=hashlib.sha256(content).hexdigest(),
            device=status.st_dev,
            inode=status.st_ino,
        )

    def _record(self, path: Path) -> dict[str, object]:
        status = os.lstat(path)
        return {
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size_bytes": status.st_size,
            "metadata": {
                "mode_octal": f"{stat.S_IMODE(status.st_mode):04o}",
                "device": status.st_dev,
                "inode": status.st_ino,
            },
        }

    def _fixture(
        self,
        root: Path,
    ) -> tuple[
        RepositorySnapshot,
        dict[str, object],
        dict[str, object],
        dict[str, object],
    ]:
        directories = (
            "build",
            "build/smctrl_probe",
            "build/smctrl_probe/tmp",
        )
        for relative in directories:
            path = root / relative
            path.mkdir(exist_ok=True)
            path.chmod(0o700)
        outputs: dict[str, object] = {}
        for name, (relative, mode) in self.OUTPUTS.items():
            path = root / relative
            path.write_bytes(f"{name}\n".encode())
            path.chmod(mode)
            outputs[name] = self._record(path)
        stamp_path = root / "build/smctrl_probe/build-config.stamp"
        stamp_path.write_bytes(b"stamp\n")
        stamp_path.chmod(0o400)
        stamp_record = self._record(stamp_path)
        stamp_record["fields"] = {}
        attestation_path = root / "build/smctrl_probe/build-attestation.json"
        attestation_path.write_bytes(b"attestation\n")
        attestation_path.chmod(0o400)
        attestation_identity = self._record(attestation_path)
        metadata = attestation_identity.pop("metadata")
        assert isinstance(metadata, dict)
        attestation_identity.update(
            {
                "mode": os.lstat(attestation_path).st_mode,
                "device": metadata["device"],
                "inode": metadata["inode"],
                "size": attestation_identity.pop("size_bytes"),
            }
        )
        archive = root / "ASLE.tar.gz"
        archive.write_bytes(b"archive\n")
        archive.chmod(0o644)
        archive_sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()
        asle_archive = {
            "path": "ASLE.tar.gz",
            "passed": True,
            "expected": {
                "sha256": archive_sha256,
                "size": archive.stat().st_size,
                "mode_octal": "0644",
            },
        }
        all_paths = [
            *directories,
            *(relative for relative, _mode in self.OUTPUTS.values()),
            "build/smctrl_probe/build-config.stamp",
            "build/smctrl_probe/build-attestation.json",
            "ASLE.tar.gz",
        ]
        snapshot = RepositorySnapshot(
            worktree=str(root),
            git_dir=None,
            common_dir=None,
            object_format="sha1",
            head_oid="1" * 40,
            index_sha256="2" * 64,
            untracked_entries=tuple(
                self._entry(root, relative) for relative in all_paths
            ),
            complete=True,
        )
        return (
            snapshot,
            {
                "build_stamp": stamp_record,
                "outputs": outputs,
            },
            attestation_identity,
            asle_archive,
        )

    def _evaluate(
        self,
        root: Path,
        snapshot: RepositorySnapshot,
        attestation: dict[str, object],
        identity: dict[str, object],
        asle: dict[str, object],
    ) -> tuple[dict[str, object], dict[str, bool]]:
        return smctrl_runner._formal_build_untracked_policy(
            repo_root=root,
            snapshot=snapshot,
            attestation=attestation,
            attestation_identity=identity,
            asle_archive=asle,
        )

    def test_exact_inventory_passes_and_every_stale_leaf_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            snapshot, attestation, identity, asle = self._fixture(root)
            _record, checks = self._evaluate(
                root, snapshot, attestation, identity, asle
            )
            self.assertTrue(all(checks.values()), checks)
            formal_tree_checks = {
                name: True
                for name in smctrl_runner._FORMAL_GIT_TREE_REQUIRED_CHECKS
            }
            formal_tree_checks.update(checks)
            self.assertTrue(
                smctrl_runner._formal_source_tree_policy_clean(
                    formal_tree_checks
                )
            )

            for relative in (
                "build/smctrl_probe/stale.o",
                "build/smctrl_probe/libsmctrl.a",
                "build/.smctrl_probe-build.lock",
                "build/smctrl_probe/tmp/stale",
                "build/unrelated",
            ):
                with self.subTest(relative=relative):
                    extra = FilesystemEntry(
                        path=os.fsencode(relative),
                        kind="regular",
                        mode_octal="0600",
                        size=1,
                        sha256="f" * 64,
                        device=1,
                        inode=1,
                    )
                    changed = replace(
                        snapshot,
                        untracked_entries=(
                            *snapshot.untracked_entries,
                            extra,
                        ),
                    )
                    _record, changed_checks = self._evaluate(
                        root, changed, attestation, identity, asle
                    )
                    self.assertFalse(
                        changed_checks[
                            "formal_git_build_exception_paths_exact"
                        ]
                    )
                    changed_tree_checks = dict(formal_tree_checks)
                    changed_tree_checks.update(changed_checks)
                    self.assertFalse(
                        smctrl_runner._formal_source_tree_policy_clean(
                            changed_tree_checks
                        )
                    )

    def test_record_paths_and_all_file_identity_fields_are_exact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            snapshot, attestation, identity, asle = self._fixture(root)
            outputs = attestation["outputs"]
            assert isinstance(outputs, dict)
            launcher = outputs["launcher"]
            assert isinstance(launcher, dict)
            original_path = launcher["path"]
            launcher["path"] = str(root / "build/elsewhere")
            _record, checks = self._evaluate(
                root, snapshot, attestation, identity, asle
            )
            self.assertFalse(
                checks["formal_git_build_records_canonical"]
            )
            launcher["path"] = original_path

            target_index = next(
                index
                for index, entry in enumerate(snapshot.untracked_entries)
                if entry.path == b"build/smctrl_probe/smid_probe"
            )
            target = snapshot.untracked_entries[target_index]
            variants = {
                "kind": replace(target, kind="directory"),
                "hash": replace(target, sha256="0" * 64),
                "size": replace(target, size=target.size + 1),
                "mode": replace(target, mode_octal="0700"),
                "device": replace(target, device=(target.device or 0) + 1),
                "inode": replace(target, inode=(target.inode or 0) + 1),
            }
            for name, replacement in variants.items():
                with self.subTest(name=name):
                    entries = list(snapshot.untracked_entries)
                    entries[target_index] = replacement
                    changed = replace(
                        snapshot,
                        untracked_entries=tuple(entries),
                    )
                    _record, changed_checks = self._evaluate(
                        root, changed, attestation, identity, asle
                    )
                    self.assertFalse(
                        changed_checks[
                            "formal_git_build_exception_files_attested"
                        ]
                    )

    def test_directory_mode_owner_and_scanner_identity_are_independent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            snapshot, attestation, identity, asle = self._fixture(root)
            build = root / "build"
            build.chmod(0o755)
            _record, checks = self._evaluate(
                root, snapshot, attestation, identity, asle
            )
            self.assertFalse(
                checks["formal_git_build_exception_directories_private"]
            )
            build.chmod(0o700)

            with mock.patch.object(
                smctrl_runner.os,
                "geteuid",
                return_value=os.geteuid() + 1,
            ):
                _record, owner_checks = self._evaluate(
                    root, snapshot, attestation, identity, asle
                )
            self.assertFalse(
                owner_checks[
                    "formal_git_build_exception_directories_private"
                ]
            )

            index = next(
                index
                for index, entry in enumerate(snapshot.untracked_entries)
                if entry.path == b"build"
            )
            entries = list(snapshot.untracked_entries)
            entries[index] = replace(
                entries[index],
                inode=(entries[index].inode or 0) + 1,
            )
            _record, identity_checks = self._evaluate(
                root,
                replace(snapshot, untracked_entries=tuple(entries)),
                attestation,
                identity,
                asle,
            )
            self.assertFalse(
                identity_checks[
                    "formal_git_build_exception_directories_private"
                ]
            )

    def test_directory_xattr_is_rejected_when_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            snapshot, attestation, identity, asle = self._fixture(root)
            build = root / "build"
            try:
                os.setxattr(build, "user.burstserve_test", b"1")
            except OSError as error:
                self.skipTest(f"filesystem xattrs unavailable: {error}")
            _record, checks = self._evaluate(
                root, snapshot, attestation, identity, asle
            )
            self.assertFalse(
                checks["formal_git_build_exception_directories_private"]
            )

    def test_asle_pin_mismatch_is_not_a_build_exception(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            snapshot, attestation, identity, asle = self._fixture(root)
            expected = asle["expected"]
            assert isinstance(expected, dict)
            expected["sha256"] = "0" * 64
            _record, checks = self._evaluate(
                root, snapshot, attestation, identity, asle
            )
            self.assertFalse(
                checks["formal_git_build_exception_files_attested"]
            )

    def test_makefile_intermediate_cleanup_and_private_root_are_exact(
        self,
    ) -> None:
        makefile = (
            Path(__file__).resolve().parents[1]
            / "native/smctrl_probe/Makefile"
        ).read_text(encoding="utf-8")
        intermediate = makefile[
            makefile.index("override INTERMEDIATE_FILES :=") :
            makefile.index(
                "\n\n.PHONY:",
                makefile.index("override INTERMEDIATE_FILES :="),
            )
        ]
        tokens = {
            token
            for token in intermediate.replace("\\\n", " ").split()
            if token.startswith("$(")
        }
        self.assertEqual(
            tokens,
            {
                "$(PROBE_OBJECT)",
                "$(LIBSMCTRL_OBJECT)",
                "$(LIBSMCTRL_ARCHIVE)",
                "$(LAUNCHER_OBJECT)",
                "$(PARENT_GUARD_OBJECT)",
                "$(SEALED_EXEC_OBJECT)",
                "$(SHA256_OBJECT)",
                "$(TEST_HELPER_OBJECT)",
                "$(EXEC_TEST_LAUNCHER_OBJECT)",
                "$(BUILD_ROOT)/.smctrl_probe-build.lock",
            },
        )
        self.assertIn(
            "_locked-all: require-build-lock $(ATTESTATION)\n"
            "\t$(RM) -f -- "
            "$(foreach file,$(INTERMEDIATE_FILES),'$(file)')",
            makefile,
        )
        gate_body = makefile[
            makefile.index("_locked-gate-required-check:") :
            makefile.index(
                "\n_locked-verify-attestation:",
                makefile.index("_locked-gate-required-check:"),
            )
        ]
        first_verify = gate_body.index("'$(ATTESTATION_SCRIPT)' verify")
        cleanup = gate_body.index(
            "$(foreach file,$(INTERMEDIATE_FILES),'$(file)')"
        )
        native_tests = gate_body.index(
            "'$(REPO_ROOT)/tests/test_native_parent_guard.py' -v"
        )
        second_verify = gate_body.rindex("'$(ATTESTATION_SCRIPT)' verify")
        self.assertLess(first_verify, cleanup)
        self.assertLess(cleanup, native_tests)
        self.assertLess(native_tests, second_verify)
        for fragment in (
            "override BUILD_ROOT := $(REPO_ROOT)/build",
            "$(INSTALL) -d -m 0700 -- '$(BUILD_ROOT)'",
            "test -d '$(BUILD_ROOT)' && test ! -L '$(BUILD_ROOT)'",
            "$(CHMOD) 0700 -- '$(BUILD_ROOT)'",
            "'$(BUILD_EUID):700'",
        ):
            self.assertIn(fragment, makefile)


class SourceRevisionTest(unittest.TestCase):
    def test_source_revision_uses_safe_registered_gitlink_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            child = temporary_root / "child"
            root = temporary_root / "main"
            commit_environment = {
                **os.environ,
                "GIT_AUTHOR_NAME": "Test",
                "GIT_AUTHOR_EMAIL": "test@example.invalid",
                "GIT_COMMITTER_NAME": "Test",
                "GIT_COMMITTER_EMAIL": "test@example.invalid",
            }
            subprocess.run(
                ["/usr/bin/git", "init", "-q", str(child)],
                check=True,
            )
            (child / "libsmctrl.c").write_text(
                "source\n",
                encoding="utf-8",
            )
            subprocess.run(
                [
                    "/usr/bin/git",
                    "-C",
                    str(child),
                    "add",
                    "libsmctrl.c",
                ],
                check=True,
            )
            subprocess.run(
                [
                    "/usr/bin/git",
                    "-C",
                    str(child),
                    "commit",
                    "-q",
                    "-m",
                    "child",
                ],
                check=True,
                env=commit_environment,
            )
            child_head = subprocess.check_output(
                ["/usr/bin/git", "-C", str(child), "rev-parse", "HEAD"],
                text=True,
            ).strip()
            subprocess.run(
                ["/usr/bin/git", "init", "-q", str(root)],
                check=True,
            )
            (root / "payload.txt").write_text("payload\n", encoding="utf-8")
            subprocess.run(
                [
                    "/usr/bin/git",
                    "-C",
                    str(root),
                    "-c",
                    "protocol.file.allow=always",
                    "submodule",
                    "add",
                    "-q",
                    str(child),
                    "vendor/libsmctrl",
                ],
                check=True,
            )
            subprocess.run(
                [
                    "/usr/bin/git",
                    "-C",
                    str(root),
                    "add",
                    "payload.txt",
                ],
                check=True,
            )
            subprocess.run(
                [
                    "/usr/bin/git",
                    "-C",
                    str(root),
                    "commit",
                    "-q",
                    "-m",
                    "main",
                ],
                check=True,
                env=commit_environment,
            )
            main_head = subprocess.check_output(
                ["/usr/bin/git", "-C", str(root), "rev-parse", "HEAD"],
                text=True,
            ).strip()

            value = source_revision(
                root,
                root / "vendor/libsmctrl",
                expected_libsmctrl_commit=child_head,
            )

            self.assertIn(f"burstserve-{main_head}+raw-clean-", value)
            self.assertIn(f"libsmctrl-{child_head}+raw-clean", value)
            self.assertFalse(hasattr(smctrl_runner, "_git_output"))

    def test_invalid_expected_commit_fails_before_safe_capture(self) -> None:
        with mock.patch.object(
            smctrl_runner,
            "capture_formal_git_snapshot",
        ) as capture:
            with self.assertRaisesRegex(RuntimeError, "full Git OID"):
                source_revision(
                    Path("/repo"),
                    Path("/repo/vendor/libsmctrl"),
                    expected_libsmctrl_commit="not-an-oid",
                )
        capture.assert_not_called()


class LibsmctrlSourceMetadataContractTest(unittest.TestCase):
    def _fixture(
        self,
        root: Path,
    ) -> tuple[Path, dict[str, object]]:
        source_root = root / "vendor/libsmctrl"
        source_root.mkdir(parents=True)
        files: dict[str, str] = {}
        for name in ("README.md", "libsmctrl.c", "libsmctrl.h"):
            path = source_root / name
            path.write_text(f"{name}\n", encoding="utf-8")
            files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        metadata: dict[str, object] = {
            "schema_version": "burstserve.libsmctrl-source/v1",
            "source_url": "https://example.invalid/libsmctrl.git",
            "source_commit": "1" * 40,
            "retrieved_on": "2026-07-30",
            "path": "vendor/libsmctrl",
            "files": files,
            "compatibility": {
                "upstream_readme_cuda_range": "6.5 through 12.6",
                "latest_x86_64_stream_case": 12080,
                "target_driver_api_version": 13030,
                "target_stream_mask_status": "unsupported",
            },
            "policy": "immutable",
        }
        metadata_path = root / smctrl_runner.DEFAULT_SOURCE_METADATA
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        metadata_path.write_text(
            json.dumps(metadata),
            encoding="utf-8",
        )
        return metadata_path, metadata

    def test_valid_metadata_binds_every_canonical_source_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            _path, metadata = self._fixture(root)
            record = smctrl_runner._load_libsmctrl_source_metadata(root)

            self.assertEqual(record["content"], metadata)
            self.assertEqual(
                smctrl_runner.latest_pinned_driver_version(root),
                12080,
            )

    def test_noncanonical_metadata_path_is_rejected_before_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self._fixture(root)
            for path in (
                Path("elsewhere.json"),
                Path("vendor/../vendor/LIBSMCTRL_SOURCE.json"),
            ):
                with self.subTest(path=path), self.assertRaisesRegex(
                    RuntimeError,
                    "exactly",
                ):
                    smctrl_runner.latest_pinned_driver_version(
                        root,
                        path,
                    )

    def test_scalar_types_oid_and_declared_digests_are_strict(self) -> None:
        mutations = {
            "commit": ("source_commit", "NOT-AN-OID"),
            "path": ("path", "vendor/other"),
            "url": ("source_url", 7),
            "date": ("retrieved_on", "30-07-2026"),
            "policy": ("policy", ""),
        }
        for name, (field, value) in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                path, metadata = self._fixture(root)
                metadata[field] = value
                path.write_text(json.dumps(metadata), encoding="utf-8")
                with self.assertRaises(RuntimeError):
                    smctrl_runner._load_libsmctrl_source_metadata(root)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path, metadata = self._fixture(root)
            files = metadata["files"]
            assert isinstance(files, dict)
            files["libsmctrl.c"] = "not-a-digest"
            path.write_text(json.dumps(metadata), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "SHA-256"):
                smctrl_runner._load_libsmctrl_source_metadata(root)

    def test_each_actual_source_byte_mismatch_is_rejected(self) -> None:
        for name in ("README.md", "libsmctrl.c", "libsmctrl.h"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                self._fixture(root)
                (root / "vendor/libsmctrl" / name).write_text(
                    "tampered\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(RuntimeError, "hashes mismatch"):
                    smctrl_runner._load_libsmctrl_source_metadata(root)

    def test_metadata_symlink_fifo_and_oversize_fail_without_blocking(
        self,
    ) -> None:
        for kind in ("symlink", "fifo", "oversize"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                path, _metadata = self._fixture(root)
                path.unlink()
                if kind == "symlink":
                    target = root / "metadata-target.json"
                    target.write_text("{}\n", encoding="utf-8")
                    path.symlink_to(target)
                elif kind == "fifo":
                    os.mkfifo(path)
                else:
                    path.write_bytes(
                        b"x" * (
                            smctrl_runner.MAX_SOURCE_METADATA_BYTES + 1
                        )
                    )
                started = time.monotonic()
                with self.assertRaises(RuntimeError):
                    smctrl_runner._load_libsmctrl_source_metadata(root)
                self.assertLess(time.monotonic() - started, 1.0)

    def test_formal_policy_requires_metadata_manifest_gitlink_commit_equality(
        self,
    ) -> None:
        expected = "1" * 40
        child = RepositorySnapshot(
            worktree="/repo/vendor/libsmctrl",
            git_dir="/repo/vendor/libsmctrl/.git",
            common_dir="/repo/vendor/libsmctrl/.git",
            object_format="sha1",
            head_oid=expected,
            index_sha256="2" * 64,
            complete=True,
        )
        snapshot = RepositorySnapshot(
            worktree="/repo",
            git_dir="/repo/.git",
            common_dir="/repo/.git",
            object_format="sha1",
            head_oid="3" * 40,
            index_sha256="4" * 64,
            gitlinks=(
                GitlinkState(
                    path=b"vendor/libsmctrl",
                    recorded_oid=expected,
                    required_oid=expected,
                    snapshot=child,
                    object_format_matches=True,
                ),
            ),
            complete=True,
        )
        manifest = _gate_manifest()["content"]
        assert isinstance(manifest, dict)
        source = manifest["source"]
        assert isinstance(source, dict)
        source["libsmctrl_commit"] = expected
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.object(
                smctrl_runner,
                "capture_formal_git_snapshot",
                return_value=snapshot,
            ),
            mock.patch.object(
                smctrl_runner,
                "load_asle_source_metadata",
                return_value={},
            ),
            mock.patch.object(
                smctrl_runner,
                "verify_asle_archive_snapshot",
                return_value={"checks": {}, "passed": True},
            ),
            mock.patch.object(
                smctrl_runner,
                "_formal_build_untracked_policy",
                return_value=({}, {}),
            ),
            mock.patch.object(
                smctrl_runner,
                "_load_libsmctrl_source_metadata",
                return_value={
                    "content": {"source_commit": "5" * 40},
                },
            ),
        ):
            _record, checks, _snapshot = (
                smctrl_runner._formal_git_source_policy(
                    repo_root=Path(temporary),
                    gate_manifest=manifest,
                    attestation={},
                    attestation_identity={},
                )
            )
        self.assertFalse(
            checks["formal_git_source_metadata_commit_exact"]
        )


class GateManifestRecordTest(unittest.TestCase):
    def test_exact_default_manifest_bytes_load_from_clean_repository(self):
        repository_manifest = (
            Path(__file__).resolve().parents[1]
            / smctrl_runner.DEFAULT_GATE_MANIFEST
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / smctrl_runner.DEFAULT_GATE_MANIFEST
            path.parent.mkdir(parents=True)
            path.write_bytes(repository_manifest.read_bytes())
            subprocess.run(
                ["/usr/bin/git", "init", "-q", str(root)],
                check=True,
            )
            subprocess.run(
                [
                    "/usr/bin/git",
                    "-C",
                    str(root),
                    "add",
                    smctrl_runner.DEFAULT_GATE_MANIFEST.as_posix(),
                ],
                check=True,
            )
            subprocess.run(
                [
                    "/usr/bin/git",
                    "-C",
                    str(root),
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "-q",
                    "-m",
                    "manifest",
                ],
                check=True,
            )

            record = load_gate_manifest_record(path, repo_root=root)

            self.assertEqual(
                record["content"]["schema_version"],
                smctrl_runner.GATE_MANIFEST_SCHEMA_VERSION,
            )
            self.assertTrue(
                smctrl_runner._valid_git_oid(
                    record["content"]["source"]["libsmctrl_commit"]
                )
            )
            self.assertEqual(
                record["content"]["source"]["libsmctrl_metadata"],
                smctrl_runner.DEFAULT_SOURCE_METADATA.as_posix(),
            )
            self.assertTrue(smctrl_runner._valid_sha256(record["sha256"]))

    def test_malformed_source_oid_fails_before_git_or_gpu_queries(self):
        record = _gate_manifest()
        content = record["content"]
        assert isinstance(content, dict)
        source = content["source"]
        assert isinstance(source, dict)
        source["libsmctrl_commit"] = "malformed"
        record["git_blob"] = "a" * 40
        record["sha256"] = hashlib.sha256(
            canonical_json(content).encode("utf-8")
        ).hexdigest()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                mock.patch.object(
                    smctrl_runner,
                    "capture_repository",
                ) as capture,
                mock.patch.object(smctrl_runner, "query_gpu") as query_gpu,
            ):
                with self.assertRaisesRegex(RuntimeError, "full Git object ID"):
                    smctrl_runner.validate_gate_manifest_record(
                        record,
                        repo_root=root,
                    )
            capture.assert_not_called()
            query_gpu.assert_not_called()

    def test_manifest_source_paths_and_digests_are_exact(self):
        content = _gate_manifest()["content"]
        assert isinstance(content, dict)
        source = content["source"]
        assert isinstance(source, dict)
        source["libsmctrl_metadata"] = "vendor/other.json"
        with self.assertRaisesRegex(RuntimeError, "not canonical"):
            smctrl_runner._validate_gate_manifest_schema(content)

        content = _gate_manifest()["content"]
        assert isinstance(content, dict)
        source = content["source"]
        assert isinstance(source, dict)
        source["approved_launcher_sha256"] = "not-a-digest"
        with self.assertRaisesRegex(RuntimeError, "SHA-256"):
            smctrl_runner._validate_gate_manifest_schema(content)

    def test_checked_in_default_manifest_and_metadata_are_compatible(self):
        root = Path(__file__).resolve().parents[1]
        # The v2 manifest upgrade is intentionally uncommitted in this
        # workspace, so the public loader must reject its HEAD mismatch.
        # Exercise the complete bounded semantic contract directly here.
        content = smctrl_runner._read_bounded_regular_bytes(
            root / smctrl_runner.DEFAULT_GATE_MANIFEST,
            label="Gate-A manifest",
            maximum_bytes=smctrl_runner.MAX_GATE_MANIFEST_BYTES,
        )
        parsed = smctrl_runner._parse_strict_json_document(
            content,
            label="Gate-A manifest",
            maximum_bytes=smctrl_runner.MAX_GATE_MANIFEST_BYTES,
            sort_keys=True,
            trailing_newline=True,
            require_canonical=False,
        )
        record = {
            "content": smctrl_runner._validate_gate_manifest_schema(
                parsed
            )
        }
        self.assertEqual(
            record["content"]["schema_version"],
            smctrl_runner.GATE_MANIFEST_SCHEMA_VERSION,
        )
        self.assertEqual(
            smctrl_runner.latest_pinned_driver_version(root),
            12080,
        )

    def test_formal_json_parser_rejects_adversarial_numbers_and_depth(self):
        nested: object = 0
        for _ in range(smctrl_runner.MAX_FORMAL_JSON_DEPTH + 2):
            nested = [nested]
        candidates = {
            "duplicate": b'{"a":1,"a":2}\n',
            "nonfinite": b'{"a":NaN}\n',
            "huge_integer": (
                b'{"a":' + (b"9" * 129) + b"}\n"
            ),
            "depth": (
                json.dumps(
                    {"a": nested},
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
            ),
        }
        for name, content in candidates.items():
            with self.subTest(name=name):
                with self.assertRaises(RuntimeError):
                    smctrl_runner._parse_strict_json_document(
                        content,
                        label="test manifest",
                        maximum_bytes=smctrl_runner.MAX_GATE_MANIFEST_BYTES,
                        sort_keys=True,
                        trailing_newline=True,
                        require_canonical=False,
                    )

    def test_manifest_and_embedded_record_reject_unknown_keys(self):
        content = json.loads(canonical_json(_gate_manifest()["content"]))
        content["unknown"] = True
        with self.assertRaisesRegex(RuntimeError, "keys"):
            smctrl_runner._validate_gate_manifest_schema(content)

        content = json.loads(canonical_json(_gate_manifest()["content"]))
        content["safety"]["xid_monitoring"]["unknown"] = True
        with self.assertRaisesRegex(RuntimeError, "keys"):
            smctrl_runner._validate_gate_manifest_schema(content)

        record = {
            **_gate_manifest(),
            "git_blob": "a" * 40,
            "unknown": True,
        }
        with self.assertRaisesRegex(RuntimeError, "record keys"):
            smctrl_runner.validate_gate_manifest_record(
                record,
                repo_root=Path("/repo"),
            )

    def test_manifest_loader_never_executes_repository_clean_filter(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(
                ["/usr/bin/git", "init", "-q", str(root)],
                check=True,
            )
            path = root / "experiments" / "manifests" / "gate.json"
            path.parent.mkdir(parents=True)
            path.write_text(
                canonical_json(_gate_manifest()["content"]) + "\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["/usr/bin/git", "-C", str(root), "add", "."],
                check=True,
            )
            commit_environment = {
                **os.environ,
                "GIT_AUTHOR_NAME": "Test",
                "GIT_AUTHOR_EMAIL": "test@example.invalid",
                "GIT_COMMITTER_NAME": "Test",
                "GIT_COMMITTER_EMAIL": "test@example.invalid",
            }
            subprocess.run(
                [
                    "/usr/bin/git",
                    "-C",
                    str(root),
                    "commit",
                    "-q",
                    "-m",
                    "manifest",
                ],
                check=True,
                env=commit_environment,
            )
            attributes = root / ".gitattributes"
            attributes.write_text(
                "experiments/manifests/gate.json filter=evil\n",
                encoding="utf-8",
            )
            subprocess.run(
                [
                    "/usr/bin/git",
                    "-C",
                    str(root),
                    "add",
                    ".gitattributes",
                ],
                check=True,
            )
            subprocess.run(
                [
                    "/usr/bin/git",
                    "-C",
                    str(root),
                    "commit",
                    "-q",
                    "-m",
                    "attributes",
                ],
                check=True,
                env=commit_environment,
            )
            marker = root / "filter.marker"
            filter_script = root.parent / f"evil-filter-{root.name}.sh"
            filter_script.write_text(
                "#!/bin/sh\n"
                f"/usr/bin/printf invoked >> {marker}\n"
                "/bin/cat\n",
                encoding="utf-8",
            )
            filter_script.chmod(0o700)
            subprocess.run(
                [
                    "/usr/bin/git",
                    "-C",
                    str(root),
                    "config",
                    "filter.evil.clean",
                    str(filter_script),
                ],
                check=True,
            )
            subprocess.run(
                [
                    "/usr/bin/git",
                    "-C",
                    str(root),
                    "config",
                    "filter.evil.required",
                    "true",
                ],
                check=True,
            )
            marker.unlink(missing_ok=True)

            record = load_gate_manifest_record(path, repo_root=root)

            self.assertEqual(
                record["content"]["schema_version"],
                smctrl_runner.GATE_MANIFEST_SCHEMA_VERSION,
            )
            self.assertFalse(marker.exists())

    def test_manifest_safe_git_capture_failure_is_not_clean(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "experiments" / "manifests" / "gate.json"
            path.parent.mkdir(parents=True)
            path.write_text(
                canonical_json(_gate_manifest()["content"]) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "safe Git provenance",
            ):
                load_gate_manifest_record(path, repo_root=root)

    def test_manifest_must_be_clean_tracked_and_inside_registered_directory(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            path = root / "experiments" / "manifests" / "gate.json"
            path.parent.mkdir(parents=True)
            path.write_text(
                canonical_json(_gate_manifest()["content"]) + "\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "-C", str(root), "add", "."], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "-q",
                    "-m",
                    "manifest",
                ],
                check=True,
            )

            record = load_gate_manifest_record(path, repo_root=root)
            self.assertEqual(record["path"], "experiments/manifests/gate.json")

            outside = root / "outside.json"
            outside.write_text(path.read_text(), encoding="utf-8")
            with self.assertRaises(RuntimeError):
                load_gate_manifest_record(outside, repo_root=root)

            path.write_text(path.read_text() + "\n", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                load_gate_manifest_record(path, repo_root=root)


if __name__ == "__main__":
    unittest.main()
