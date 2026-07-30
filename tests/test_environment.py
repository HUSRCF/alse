from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from burstserve.environment import _model_inventory, _run, capture_environment, main


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
            source_path.write_text('{"archive_sha256":"abc"}')

            snapshot = capture_environment(repo_root=root)

        self.assertEqual(snapshot["schema_version"], "burstserve.environment/v1")
        self.assertEqual(snapshot["asle_source"]["archive_sha256"], "abc")
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
