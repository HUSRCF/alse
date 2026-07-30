from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from burstserve.runtime_lock import (
    RUNTIME_LOCK_SCHEMA_VERSION,
    build_runtime_lock,
    compare_runtime_locks,
    materialize_install_files,
)


def _snapshot() -> dict:
    conda_packages = [
        {"name": "python", "channel": "conda-forge"},
        {"name": "packaging", "channel": "conda-forge"},
        {"name": "torch", "channel": "pypi"},
    ]
    return {
        "python": {"implementation": "CPython", "version": "3.11.15"},
        "platform": {"machine": "x86_64", "system": "Linux"},
        "package_lock": {
            "conda_explicit": {
                "ok": True,
                "stdout": "# platform: linux-64\n@EXPLICIT\nhttps://example/python.conda",
            },
            "conda_json": {
                "ok": True,
                "stdout": json.dumps(conda_packages),
            },
            "pip_freeze": {
                "ok": True,
                "stdout": "\n".join(
                    [
                        "torch==2.11.0",
                        "packaging @ file:///build/packaging",
                        "diffusers==0.38.0",
                    ]
                ),
            },
        },
        "framework_runtime": {
            "ok": True,
            "value": {
                "accelerate": "1.14.0",
                "diffusers": "0.38.0",
                "transformers": "5.12.1",
                "torch": {
                    "version": "2.11.0+cu130",
                    "cuda_built": "13.0",
                    "cudnn": 91900,
                    "devices": [
                        {
                            "name": "NVIDIA GeForce RTX 4090",
                            "capability": [8, 9],
                            "sm_count": 128,
                            "total_memory_bytes": 24_000_000_000,
                        },
                        {
                            "name": "NVIDIA GeForce RTX 4090",
                            "capability": [8, 9],
                            "sm_count": 128,
                            "total_memory_bytes": 24_000_000_000,
                        },
                    ],
                },
            },
        },
        "cuda_toolkit": {"ok": True, "stdout": "Cuda compilation tools, 13.3"},
        "asle_source": {
            "archive_sha256": "archive",
            "imported_file_count": 347,
            "imported_tree_sha256": "tree",
        },
    }


class BuildRuntimeLockTest(unittest.TestCase):
    def test_filters_conda_managed_distributions_from_pip_lock(self) -> None:
        lock = build_runtime_lock(_snapshot())

        self.assertEqual(lock["schema_version"], RUNTIME_LOCK_SCHEMA_VERSION)
        self.assertEqual(
            lock["pip"]["requirements"],
            ["diffusers==0.38.0", "torch==2.11.0"],
        )
        self.assertEqual(
            lock["conda"]["explicit_urls"],
            ["https://example/python.conda"],
        )
        self.assertEqual(lock["framework"]["gpu"]["count"], 2)
        self.assertEqual(lock["framework"]["gpu"]["sm_count"], 128)

    def test_filters_unrelated_pip_distributions_from_minimal_closure(self) -> None:
        lock = build_runtime_lock(
            _snapshot(),
            included_distributions={"diffusers", "packaging"},
        )

        self.assertEqual(lock["pip"]["requirements"], ["diffusers==0.38.0"])
        self.assertEqual(
            lock["pip"]["included_distributions"],
            ["diffusers", "packaging"],
        )

    def test_rejects_heterogeneous_gpu_classes(self) -> None:
        snapshot = _snapshot()
        snapshot["framework_runtime"]["value"]["torch"]["devices"][1]["sm_count"] = 120

        with self.assertRaisesRegex(ValueError, "homogeneous GPU class"):
            build_runtime_lock(snapshot)


class CompareRuntimeLockTest(unittest.TestCase):
    def test_exact_match_passes_and_changed_field_fails(self) -> None:
        expected = build_runtime_lock(_snapshot())
        observed = build_runtime_lock(_snapshot())

        self.assertTrue(compare_runtime_locks(expected, observed)["matches"])
        observed["python"]["version"] = "3.12.0"
        report = compare_runtime_locks(expected, observed)
        self.assertFalse(report["matches"])
        self.assertEqual(report["mismatches"][0]["field"], "python")


class MaterializeRuntimeLockTest(unittest.TestCase):
    def test_writes_reproducible_installer_inputs(self) -> None:
        lock = build_runtime_lock(_snapshot())
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            materialize_install_files(lock, output)

            self.assertEqual(
                (output / "conda-explicit.txt").read_text(),
                "@EXPLICIT\nhttps://example/python.conda\n",
            )
            self.assertEqual(
                (output / "pip-requirements.txt").read_text(),
                "diffusers==0.38.0\ntorch==2.11.0\n",
            )


if __name__ == "__main__":
    unittest.main()
