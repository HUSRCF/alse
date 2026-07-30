from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from burstserve.correctness_runner import (
    build_child_environment,
    build_command,
    compare_runs,
    evaluate_correctness,
    execute,
)


_HASH_A = "a" * 64
_HASH_B = "b" * 64


def _config(*, mode: str = "stock", trial: int = 0) -> dict[str, object]:
    return {
        "schema_version": "burstserve.correctness-cell/v1",
        "physical_gpu": 3,
        "trial": trial,
        "model": "cog2b",
        "mode": mode,
        "budget_gb": 0.0,
        "frames": 9,
        "height": 480,
        "width": 720,
        "video_steps": 1,
        "tiles": 8,
        "seed": 777,
    }


class CommandTest(unittest.TestCase):
    def test_pc1_command_has_registered_tiny_defaults(self) -> None:
        command = build_command(
            python=Path("/python"),
            vendor_root=Path("/vendor"),
            logdir=Path("/logs"),
            config=_config(),
        )
        self.assertEqual(command[:3], ["/python", "-u", "/vendor/pc1_gate.py"])
        self.assertEqual(command[command.index("--model") + 1], "cog2b")
        self.assertEqual(command[command.index("--mode") + 1], "stock")
        self.assertEqual(command[command.index("--frames") + 1], "9")
        self.assertEqual(command[command.index("--height") + 1], "480")
        self.assertEqual(command[command.index("--width") + 1], "720")
        self.assertEqual(command[command.index("--vsteps") + 1], "1")
        self.assertEqual(command[command.index("--G") + 1], "8")
        self.assertEqual(command[command.index("--seed") + 1], "777")

    def test_child_environment_is_offline_and_pins_physical_gpu(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {"HTTPS_PROXY": "http://proxy", "HTTP_PROXY": "http://proxy"},
        ):
            environment = build_child_environment(
                physical_gpu=4,
                model_root=Path("/models"),
            )
        self.assertEqual(environment["CUDA_VISIBLE_DEVICES"], "4")
        self.assertEqual(environment["STEPSWAP_MODELS"], "/models")
        self.assertEqual(environment["HF_HUB_OFFLINE"], "1")
        self.assertEqual(environment["TRANSFORMERS_OFFLINE"], "1")
        self.assertNotIn("HTTPS_PROXY", environment)
        self.assertNotIn("HTTP_PROXY", environment)


class AcceptanceTest(unittest.TestCase):
    def test_semantic_contract_requires_video_and_latent_hash(self) -> None:
        acceptance, accepted = evaluate_correctness(
            {
                "runnable": True,
                "video_done": 1,
                "latent_sha256": _HASH_A,
            },
            process_exit_code=0,
        )
        self.assertTrue(accepted)
        self.assertTrue(all(acceptance.values()))

        acceptance, accepted = evaluate_correctness(
            {
                "runnable": True,
                "video_done": 0,
                "latent_sha256": "not-a-hash",
            },
            process_exit_code=0,
        )
        self.assertFalse(accepted)
        self.assertFalse(acceptance["minimum_video_met"])
        self.assertFalse(acceptance["latent_sha256_present"])


class _FakeProcess:
    pid = 12345
    returncode = 0

    def __init__(self, vendor_logdir: Path) -> None:
        self.vendor_logdir = vendor_logdir

    def communicate(self, timeout: float | None = None) -> tuple[str, None]:
        summary = {
            "mode": "stock",
            "runnable": True,
            "video_done": 1,
            "latent_sha256": _HASH_A,
        }
        (self.vendor_logdir / "summary_stock.json").write_text(
            json.dumps(summary),
            encoding="utf-8",
        )
        (self.vendor_logdir / "latent_stock.npy").write_bytes(b"fake-npy")
        return ("fake PC1 output\n", None)


class ExecuteTest(unittest.TestCase):
    @mock.patch(
        "burstserve.correctness_runner.capture_environment",
        return_value={"env": "test"},
    )
    @mock.patch(
        "burstserve.correctness_runner.source_revision",
        return_value="test-revision",
    )
    @mock.patch(
        "burstserve.correctness_runner.query_gpu",
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
    def test_run_is_unique_and_captures_all_artifacts(
        self,
        _gpu: mock.Mock,
        _revision: mock.Mock,
        _environment: mock.Mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vendor = root / "vendor" / "asle"
            vendor.mkdir(parents=True)
            (vendor / "pc1_gate.py").write_text("# fake", encoding="utf-8")
            (root / "vendor" / "ASLE_SOURCE.json").write_text(
                '{"archive_sha256":"abc"}',
                encoding="utf-8",
            )
            model_root = root / "models"
            (model_root / "CogVideoX-2b").mkdir(parents=True)
            python = root / "python"
            python.write_text("# fake", encoding="utf-8")
            run_root = root / "runs"

            def fake_popen(command: list[str], **_kwargs: object) -> _FakeProcess:
                logdir = Path(command[command.index("--logdir") + 1])
                return _FakeProcess(logdir)

            with mock.patch(
                "burstserve.correctness_runner.subprocess.Popen",
                side_effect=fake_popen,
            ):
                code, run_directory = execute(
                    repo_root=root,
                    python=python,
                    vendor_root=vendor,
                    model_root=model_root,
                    run_root=run_root,
                    config=_config(trial=7),
                    timeout_s=10,
                    maximum_used_mib=1024,
                    allow_busy_gpu=False,
                )

            self.assertEqual(code, 0)
            for name in (
                "manifest.json",
                "events.jsonl",
                "command.json",
                "stdout.log",
                "summary.json",
                "latent.npy",
                "outcome.json",
            ):
                self.assertTrue((run_directory / name).is_file(), name)
            manifest = json.loads((run_directory / "manifest.json").read_text())
            self.assertEqual(manifest["config"]["trial"], 7)
            outcome = json.loads((run_directory / "outcome.json").read_text())
            self.assertTrue(outcome["semantic_accepted"])
            self.assertTrue(outcome["artifact_captured"])
            self.assertTrue(outcome["accepted"])
            self.assertEqual(
                outcome["latent_artifact"]["file_sha256"],
                hashlib.sha256(b"fake-npy").hexdigest(),
            )

            with mock.patch(
                "burstserve.correctness_runner.subprocess.Popen",
                side_effect=AssertionError("duplicate must fail before inference"),
            ):
                with self.assertRaises(FileExistsError):
                    execute(
                        repo_root=root,
                        python=python,
                        vendor_root=vendor,
                        model_root=model_root,
                        run_root=run_root,
                        config=_config(trial=7),
                        timeout_s=10,
                        maximum_used_mib=1024,
                        allow_busy_gpu=False,
                    )


def _write_comparison_run(
    directory: Path,
    *,
    run_id: str,
    mode: str,
    latent_sha256: str,
    values: list[float] | None = None,
) -> None:
    directory.mkdir(parents=True)
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "source_revision": "source",
                "config": {"mode": mode, "seed": 777},
            }
        ),
        encoding="utf-8",
    )
    (directory / "summary.json").write_text(
        json.dumps({"latent_sha256": latent_sha256}),
        encoding="utf-8",
    )
    if values is not None:
        try:
            import numpy as np
        except ImportError:
            return
        np.save(directory / "latent.npy", np.asarray(values, dtype=np.float32))


class CompareTest(unittest.TestCase):
    def test_same_mode_requires_exact_sha(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            left = root / "left"
            right = root / "right"
            _write_comparison_run(
                left, run_id="left", mode="stock", latent_sha256=_HASH_A
            )
            _write_comparison_run(
                right, run_id="right", mode="stock", latent_sha256=_HASH_B
            )
            result = compare_runs(left, right)
            self.assertEqual(result["comparison_kind"], "same_mode_repeat")
            self.assertTrue(result["comparable"])
            self.assertEqual(result["verdict"], "fail")
            self.assertFalse(result["sha256_equal"])
            self.assertNotIn("numeric_difference", result)

    def test_cross_mode_is_report_only_with_fp64_differences(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            left = root / "left"
            right = root / "right"
            _write_comparison_run(
                left,
                run_id="left",
                mode="stock",
                latent_sha256=_HASH_A,
                values=[1.0, 2.0],
            )
            _write_comparison_run(
                right,
                run_id="right",
                mode="tiled",
                latent_sha256=_HASH_B,
                values=[1.5, 1.0],
            )
            result = compare_runs(left, right)
            self.assertEqual(result["comparison_kind"], "cross_mode")
            self.assertTrue(result["comparable"])
            self.assertEqual(result["verdict"], "report_only")
            numeric = result["numeric_difference"]
            if numeric["available"]:
                self.assertEqual(numeric["calculation_dtype"], "float64")
                self.assertEqual(numeric["max_abs"], 1.0)
                self.assertEqual(numeric["mean_abs"], 0.75)


if __name__ == "__main__":
    unittest.main()
