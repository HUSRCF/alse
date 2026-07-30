from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from burstserve.smctrl_runner import (
    NATIVE_SCHEMA_VERSION,
    NativeOutputError,
    _git_revision,
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


def _gate_manifest(*, promoted: bool = False) -> dict[str, object]:
    content = {
        "schema_version": "burstserve.gate-a-manifest/v1",
        "manifest_id": "test-gate-a",
        "hardware": {
            "gpu_name": "GPU",
            "physical_gpu_indices": [3],
            "compute_capability": [8, 9],
            "sm_count": 128,
            "driver_api_version": 13030,
        },
        "source": {},
        "safety": {
            "timeout_s": 10,
            "maximum_preexisting_gpu_memory_mib": 1024,
            "experimental_mask_enabled": promoted,
            "approved_mask_modes": (
                ["global", "next", "stream"] if promoted else []
            ),
            "reserved_gpu_uuids": ["uuid"] if promoted else [],
            "exclusive_reservation_evidence": (
                "test reservation" if promoted else None
            ),
            "xid_monitoring_available": promoted,
            "xid_monitoring_method": (
                "test kernel log monitor" if promoted else None
            ),
            "stream_offset_search_enabled": promoted,
            "stream_mask_off_candidates": [-16] if promoted else [],
            "global_next_matrix_accepted": promoted,
        },
        "baseline": {
            "blocks_per_sm": 32,
            "iterations": 100,
            "minimum_sm_coverage_fraction": 0.75,
            "trials_per_gpu": 3,
        },
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
    return {
        "schema_version": NATIVE_SCHEMA_VERSION,
        "status": status,
        "mode": mode,
        "driver_version": 13030,
        "runtime_version": 13000,
        "device": {
            "name": "Fake GPU",
            "uuid": "uuid",
            "cc_major": 8,
            "cc_minor": 9,
            "sm_count": sm_count,
        },
        "requested_enabled_tpc": enabled_tpc,
        "blocks": blocks if blocks is not None else inferred_blocks,
        "iterations": iterations,
        "observed_histogram": observed,
    }


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
        "schema_version": "burstserve.smid-probe-cell/v1",
        "physical_gpu": 3,
        "mode": mode,
        "enabled_tpc": 0,
        "iterations": 100,
        "trial": trial,
        "seed": 0,
        "timeout_s": 10,
        "maximum_used_mib": 1024,
        "allow_busy_gpu": allow_busy,
        "experimental_allow_unsupported_driver": experimental_allow,
        "experimental_mask_off": mask_off,
        "gate_manifest": _gate_manifest(promoted=promoted),
    }


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
            trial=0,
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
                trial=0,
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
                trial=0,
            )
        self.assertFalse(undeclared_allowed)


class CommandTest(unittest.TestCase):
    def test_command_and_child_environment_are_explicit(self) -> None:
        command = build_probe_command(
            binary=Path("/probe"),
            mode="stream",
            enabled_tpc=7,
            iterations=10000,
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
            },
            clear=False,
        ):
            environment = build_child_environment(
                selected_gpu_uuid="GPU-selected",
                experimental_mask_off=12,
            )
        self.assertEqual(environment["CUDA_VISIBLE_DEVICES"], "GPU-selected")
        self.assertEqual(environment["MASK_OFF"], "12")
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


class NativeContractTest(unittest.TestCase):
    def test_single_line_and_schema_are_strict(self) -> None:
        value = _native()
        self.assertEqual(
            parse_native_output(json.dumps(value) + "\n"),
            value,
        )
        with self.assertRaises(NativeOutputError):
            parse_native_output("{}\n{}\n")
        native_spelling = dict(value)
        native_spelling["schema"] = native_spelling.pop("schema_version")
        self.assertEqual(
            parse_native_output(json.dumps(native_spelling)),
            native_spelling,
        )
        value["schema_version"] = "wrong"
        with self.assertRaises(NativeOutputError):
            parse_native_output(json.dumps(value))

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
            expected_iterations=100,
            process_exit_code=0,
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
            expected_iterations=100,
            process_exit_code=0,
        )
        self.assertTrue(accepted)
        self.assertEqual(metrics["sm_coverage_ratio"], 0.75)

    def test_masked_probe_requires_one_or_two_sms_and_ok_status(self) -> None:
        _, _, accepted = evaluate_probe(
            _native(mode="stream", histogram={"17": 40, "18": 40}, sm_count=128),
            expected_mode="stream",
            expected_enabled_tpc=0,
            expected_driver_version=13030,
            expected_iterations=100,
            process_exit_code=0,
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
            expected_iterations=100,
            process_exit_code=0,
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
            expected_iterations=100,
            process_exit_code=0,
        )
        self.assertFalse(accepted)


class _FakeProcess:
    pid = 12345
    returncode = 0

    def __init__(self, stdout: str, stderr: str = "") -> None:
        self.stdout = stdout
        self.stderr = stderr

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        return self.stdout, self.stderr


class ExecuteTest(unittest.TestCase):
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
            libsmctrl = root / "vendor" / "libsmctrl"
            libsmctrl.mkdir(parents=True)
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
            ):
                code, run_directory = execute(
                    repo_root=root,
                    binary=binary,
                    libsmctrl_root=libsmctrl,
                    run_root=root / "runs",
                    config=_config(mode="baseline"),
                    timeout_s=10,
                    maximum_used_mib=1024,
                    allow_busy_gpu=False,
                )

            self.assertEqual(code, 0)
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
            child_environment = popen.call_args.kwargs["env"]
            self.assertEqual(child_environment["CUDA_VISIBLE_DEVICES"], "uuid")
            self.assertNotIn("MASK_OFF", child_environment)

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


class SourceRevisionTest(unittest.TestCase):
    @mock.patch(
        "burstserve.smctrl_runner._git_revision",
        side_effect=["main-head", "lib-head"],
    )
    def test_source_revision_contains_both_repositories(
        self,
        _revision: mock.Mock,
    ) -> None:
        value = source_revision(Path("/repo"), Path("/repo/vendor/libsmctrl"))
        self.assertEqual(value, "burstserve-main-head;libsmctrl-lib-head")

    def test_untracked_file_content_changes_dirty_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(
                ["git", "init", "-q", str(root)],
                check=True,
            )
            tracked = root / "tracked.txt"
            tracked.write_text("tracked\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "tracked.txt"], check=True)
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
                    "initial",
                ],
                check=True,
            )
            untracked = root / "new.txt"
            untracked.write_text("first\n", encoding="utf-8")
            first = _git_revision(root)
            untracked.write_text("second\n", encoding="utf-8")
            second = _git_revision(root)

            self.assertNotEqual(first, second)
            self.assertIn("+dirty-", first)


class GateManifestRecordTest(unittest.TestCase):
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
