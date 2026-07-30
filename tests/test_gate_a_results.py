from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from burstserve.gate_a_results import (
    EVIDENCE_SPEC_SCHEMA_VERSION,
    aggregate_from_spec,
    main,
)
from burstserve.provenance import (
    EventRecord,
    RunManifest,
    append_jsonl_atomic,
    canonical_json,
    write_json_atomic,
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


def _gpu_uuid(index: int) -> str:
    return f"GPU-00000000-0000-0000-0000-{index:012d}"


def _spec(
    *,
    selected: list[int] | None = None,
    trials: list[int] | None = None,
    excluded: list[dict[str, str]] | None = None,
    rejections: list[str] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": EVIDENCE_SPEC_SCHEMA_VERSION,
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
        "driver_policy": _all_true(),
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


if __name__ == "__main__":
    unittest.main()
