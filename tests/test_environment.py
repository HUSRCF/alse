from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

from git_support import GIT as _GIT
from git_support import require_supported_git
from unittest import mock

from burstserve.environment import (
    _framework_runtime,
    _model_inventory,
    _run,
    capture_environment,
    main,
)


class CommandProbeTest(unittest.TestCase):
    def test_command_failure_is_data_not_exception(self) -> None:
        result = _run(["/definitely/not/a/command"])
        self.assertFalse(result["ok"])
        self.assertIn("error", result)


class ModelInventoryTest(unittest.TestCase):
    def test_inventory_is_sorted_and_marks_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "z-model").mkdir()
            (root / "A-model").mkdir()
            (root / "A-model" / "model_index.json").write_text("{}")

            result = _model_inventory(root)

        self.assertEqual(
            [entry["name"] for entry in result["directories"]],
            ["A-model", "z-model"],
        )
        self.assertTrue(result["directories"][0]["has_model_index"])


class CaptureEnvironmentTest(unittest.TestCase):
    def setUp(self) -> None:
        require_supported_git(self)

    @mock.patch(
        "burstserve.environment._framework_runtime",
        return_value={"ok": True},
    )
    @mock.patch("burstserve.environment._run", return_value={"ok": True})
    @mock.patch("burstserve.environment._nvcc_path", return_value=None)
    def test_git_capture_never_executes_repository_clean_filter(
        self,
        _nvcc: mock.Mock,
        _run_probe: mock.Mock,
        _framework: mock.Mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            root = temporary_root / "repo"
            subprocess.run(
                [str(_GIT), "init", "-q", str(root)],
                check=True,
            )
            (root / ".gitattributes").write_text(
                "payload.txt filter=evil\n",
                encoding="utf-8",
            )
            (root / "payload.txt").write_text(
                "payload\n",
                encoding="utf-8",
            )
            subprocess.run(
                [
                    str(_GIT),
                    "-C",
                    str(root),
                    "add",
                    ".gitattributes",
                    "payload.txt",
                ],
                check=True,
            )
            subprocess.run(
                [
                    str(_GIT),
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
            marker = temporary_root / "filter.marker"
            filter_script = temporary_root / "evil-filter.sh"
            filter_script.write_text(
                "#!/bin/sh\n"
                f"/usr/bin/touch {marker}\n"
                "/bin/cat\n",
                encoding="utf-8",
            )
            filter_script.chmod(0o700)
            subprocess.run(
                [
                    str(_GIT),
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
                    str(_GIT),
                    "-C",
                    str(root),
                    "config",
                    "filter.evil.required",
                    "true",
                ],
                check=True,
            )
            marker.unlink(missing_ok=True)

            snapshot = capture_environment(repo_root=root)

            self.assertTrue(snapshot["git"]["complete"])
            self.assertTrue(snapshot["git"]["clean"])
            self.assertFalse(marker.exists())

    @mock.patch(
        "burstserve.environment._framework_runtime",
        return_value={"ok": True},
    )
    @mock.patch("burstserve.environment._run", return_value={"ok": True})
    @mock.patch("burstserve.environment._nvcc_path", return_value=None)
    def test_formal_asle_binding_rejects_mismatch_symlink_fifo_and_oversize(
        self,
        _nvcc: mock.Mock,
        _run_probe: mock.Mock,
        _framework: mock.Mock,
    ) -> None:
        for kind in ("mismatch", "symlink", "fifo", "oversize"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                temporary_root = Path(temporary)
                root = temporary_root / "repo"
                source_path = root / "vendor/ASLE_SOURCE.json"
                source_path.parent.mkdir(parents=True)
                archive = root / "ASLE.tar.gz"
                archive.write_bytes(b"archive")
                archive.chmod(0o644)
                metadata = {
                    "schema_version": 1,
                    "source_archive": "ASLE.tar.gz",
                    "archive_sha256": hashlib.sha256(b"archive").hexdigest(),
                    "archive_size_bytes": len(b"archive"),
                    "archive_top_level": "ASLE",
                    "imported_path": "vendor/asle",
                    "imported_file_count": 1,
                    "imported_tree_sha256": "a" * 64,
                    "imported_at": "2026-07-30T00:00:00Z",
                    "policy": "immutable",
                }
                if kind == "mismatch":
                    archive.write_bytes(b"different")
                    archive.chmod(0o644)
                elif kind == "symlink":
                    archive.unlink()
                    target = temporary_root / "outside-archive"
                    target.write_bytes(b"archive")
                    archive.symlink_to(target)
                elif kind == "fifo":
                    archive.unlink()
                    os.mkfifo(archive)
                elif kind == "oversize":
                    metadata["archive_size_bytes"] = 257 * 1024 * 1024
                source_path.write_text(
                    json.dumps(metadata),
                    encoding="utf-8",
                )
                subprocess.run(
                    [str(_GIT), "init", "-q", str(root)],
                    check=True,
                )
                subprocess.run(
                    [
                        str(_GIT),
                        "-C",
                        str(root),
                        "add",
                        "vendor/ASLE_SOURCE.json",
                    ],
                    check=True,
                )
                subprocess.run(
                    [
                        str(_GIT),
                        "-C",
                        str(root),
                        "-c",
                        "user.name=Test",
                        "-c",
                        "user.email=test@example.invalid",
                        "commit",
                        "-q",
                        "-m",
                        "metadata",
                    ],
                    check=True,
                )
                started = time.monotonic()
                with self.assertRaises(RuntimeError):
                    capture_environment(
                        repo_root=root,
                        require_asle_binding=True,
                    )
                self.assertLess(time.monotonic() - started, 1.0)

    @mock.patch("burstserve.environment._run")
    def test_isolated_framework_probe_gets_cpu_only_exact_environment(
        self,
        run: mock.Mock,
    ) -> None:
        run.return_value = {"ok": False, "error": "synthetic"}
        environment = {
            "PATH": "/usr/bin:/bin",
            "LANG": "C",
            "LC_ALL": "C",
            "TZ": "UTC",
            "CUDA_VISIBLE_DEVICES": "",
            "CUDA_MPS_PIPE_DIRECTORY": "",
        }

        _framework_runtime(
            command_environment=environment,
            gpu_probe=False,
            isolated_python=True,
        )

        command = run.call_args.args[0]
        self.assertEqual(command[1:3], ["-I", "-S"])
        self.assertEqual(run.call_args.kwargs["environment"], environment)
        program = command[-1]
        self.assertIn("gpu_probe_enabled = False", program)
        self.assertNotIn("GPU-target", program)

    @mock.patch("burstserve.environment._run")
    def test_formal_capture_passes_exact_cpu_only_env_to_every_child(
        self,
        run: mock.Mock,
    ) -> None:
        run.return_value = {
            "ok": True,
            "returncode": 0,
            "stdout": "{}",
            "stderr": "",
        }
        environment = {
            "PATH": "/usr/bin:/bin",
            "LANG": "C",
            "LC_ALL": "C",
            "TZ": "UTC",
            "CUDA_VISIBLE_DEVICES": "",
            "CUDA_MPS_PIPE_DIRECTORY": "",
        }
        with tempfile.TemporaryDirectory() as temporary:
            capture_environment(
                repo_root=Path(temporary),
                command_environment=environment,
                framework_gpu_probe=False,
                allow_nvcc_path_search=False,
                isolated_python=True,
            )

        self.assertGreaterEqual(run.call_count, 3)
        for call in run.call_args_list:
            command = call.args[0]
            child_environment = call.kwargs["environment"]
            self.assertTrue(Path(command[0]).is_absolute())
            self.assertEqual(
                child_environment["CUDA_VISIBLE_DEVICES"],
                "",
            )
            self.assertEqual(
                child_environment["CUDA_MPS_PIPE_DIRECTORY"],
                "",
            )
            self.assertNotIn("LD_PRELOAD", child_environment)
            self.assertNotIn("PYTHONPATH", child_environment)

    @mock.patch(
        "burstserve.environment._framework_runtime",
        return_value={"ok": True, "value": {"torch": {"version": "test"}}},
    )
    @mock.patch("burstserve.environment._run")
    @mock.patch("burstserve.environment._nvcc_path", return_value=None)
    def test_snapshot_contains_source_and_schema(
        self,
        _nvcc: mock.Mock,
        run: mock.Mock,
        _framework: mock.Mock,
    ) -> None:
        run.return_value = {"ok": True, "stdout": "probe"}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_path = root / "vendor" / "ASLE_SOURCE.json"
            source_path.parent.mkdir(parents=True)
            archive = root / "ASLE.tar.gz"
            archive.write_bytes(b"archive")
            archive.chmod(0o644)
            source = {
                "schema_version": 1,
                "source_archive": "ASLE.tar.gz",
                "archive_sha256": hashlib.sha256(
                    archive.read_bytes()
                ).hexdigest(),
                "archive_size_bytes": archive.stat().st_size,
                "archive_top_level": "ASLE",
                "imported_path": "vendor/asle",
                "imported_file_count": 1,
                "imported_tree_sha256": "a" * 64,
                "imported_at": "2026-07-30T00:00:00Z",
                "policy": "immutable",
            }
            source_path.write_text(json.dumps(source), encoding="utf-8")
            subprocess.run(
                [str(_GIT), "init", "-q", str(root)],
                check=True,
            )
            subprocess.run(
                [
                    str(_GIT),
                    "-C",
                    str(root),
                    "add",
                    "vendor/ASLE_SOURCE.json",
                ],
                check=True,
            )
            subprocess.run(
                [
                    str(_GIT),
                    "-C",
                    str(root),
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "-q",
                    "-m",
                    "metadata",
                ],
                check=True,
            )

            snapshot = capture_environment(repo_root=root)

        self.assertEqual(snapshot["schema_version"], "burstserve.environment/v1")
        self.assertEqual(
            snapshot["asle_source"]["archive_sha256"],
            source["archive_sha256"],
        )
        self.assertTrue(snapshot["asle_archive"]["passed"])
        self.assertIn("packages", snapshot)
        self.assertTrue(snapshot["framework_runtime"]["ok"])
        self.assertIn("nvidia_smi", snapshot)

    @mock.patch("burstserve.environment.capture_environment")
    def test_cli_writes_json(self, capture: mock.Mock) -> None:
        capture.return_value = {"schema_version": "test/v1"}
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "environment.json"
            code = main(["--output", str(output), "--repo-root", temporary])
            payload = json.loads(output.read_text())

        self.assertEqual(code, 0)
        self.assertEqual(payload, {"schema_version": "test/v1"})


if __name__ == "__main__":
    unittest.main()
