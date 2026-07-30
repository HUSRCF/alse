from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from burstserve.asle_runner import (
    build_child_environment,
    build_command,
    ensure_gpu_available,
    execute,
)


def _config() -> dict[str, object]:
    return {
        "schema_version": "burstserve.asle-cell/v1",
        "physical_gpu": 3,
        "arm": "stepswap",
        "arrival": "poisson",
        "seed": 1,
        "horizon_s": 1.0,
        "arrival_rate": 1.0,
        "long_model": "cog2b",
        "urgent_model": "sdxl",
        "budget_gb": 0.0,
        "frames": 9,
        "height": 480,
        "width": 720,
        "video_steps": 1,
        "urgent_steps": 1,
        "tiles": 8,
    }


class PreflightTest(unittest.TestCase):
    def test_busy_gpu_is_rejected(self) -> None:
        gpu = {"index": 2, "memory_used_mib": 4096}
        with self.assertRaisesRegex(RuntimeError, "busy"):
            ensure_gpu_available(gpu, maximum_used_mib=1024, allow_busy=False)
        ensure_gpu_available(gpu, maximum_used_mib=1024, allow_busy=True)


class CommandTest(unittest.TestCase):
    def test_tiny_cell_command_is_explicit(self) -> None:
        command = build_command(
            python=Path("/python"),
            vendor_root=Path("/vendor"),
            logdir=Path("/logs"),
            config=_config(),
        )
        self.assertEqual(command[:3], ["/python", "-u", "/vendor/r1_driver.py"])
        self.assertEqual(command[command.index("--frames") + 1], "9")
        self.assertEqual(command[command.index("--usteps") + 1], "1")

    def test_offload_arm_registers_debug_module(self) -> None:
        environment = build_child_environment(
            physical_gpu=4,
            model_root=Path("/models"),
            arm="offload_tiled",
        )
        self.assertEqual(environment["CUDA_VISIBLE_DEVICES"], "4")
        self.assertEqual(environment["R1_DEBUG_ARM"], "arm_offload_tiled")
        self.assertEqual(environment["HF_HUB_OFFLINE"], "1")


class _FakeProcess:
    pid = 12345
    returncode = 0

    def communicate(self, timeout: float | None = None) -> tuple[str, None]:
        return ("fake baseline output\n", None)


class ExecuteTest(unittest.TestCase):
    @mock.patch("burstserve.asle_runner.capture_environment", return_value={"env": "test"})
    @mock.patch("burstserve.asle_runner.source_revision", return_value="test-revision")
    @mock.patch("burstserve.asle_runner.subprocess.Popen", return_value=_FakeProcess())
    @mock.patch(
        "burstserve.asle_runner.query_gpu",
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
    def test_run_directory_is_unique_and_complete(
        self,
        _gpu: mock.Mock,
        _popen: mock.Mock,
        _revision: mock.Mock,
        _environment: mock.Mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vendor = root / "vendor" / "asle"
            vendor.mkdir(parents=True)
            (vendor / "r1_driver.py").write_text("# fake")
            (root / "vendor" / "ASLE_SOURCE.json").write_text(
                '{"archive_sha256":"abc"}'
            )
            run_root = root / "runs"

            code, run_directory = execute(
                repo_root=root,
                python=Path("/python"),
                vendor_root=vendor,
                model_root=root / "models",
                run_root=run_root,
                config=_config(),
                timeout_s=10,
                maximum_used_mib=1024,
                allow_busy_gpu=False,
            )

            self.assertEqual(code, 0)
            self.assertTrue((run_directory / "manifest.json").is_file())
            self.assertTrue((run_directory / "events.jsonl").is_file())
            self.assertTrue((run_directory / "outcome.json").is_file())
            outcome = json.loads((run_directory / "outcome.json").read_text())
            self.assertFalse(outcome["summary_found"])
            with self.assertRaises(FileExistsError):
                execute(
                    repo_root=root,
                    python=Path("/python"),
                    vendor_root=vendor,
                    model_root=root / "models",
                    run_root=run_root,
                    config=_config(),
                    timeout_s=10,
                    maximum_used_mib=1024,
                    allow_busy_gpu=False,
                )


if __name__ == "__main__":
    unittest.main()
