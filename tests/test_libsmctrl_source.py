from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import unittest


class LibsmctrlSourceTest(unittest.TestCase):
    def test_pinned_submodule_and_critical_file_hashes(self) -> None:
        root = Path(__file__).resolve().parents[1]
        metadata_path = root / "vendor" / "LIBSMCTRL_SOURCE.json"
        metadata = json.loads(metadata_path.read_text())
        source = root / metadata["path"]
        self.assertTrue(
            (source / "README.md").is_file(),
            "libsmctrl submodule is not initialized; run "
            "`git submodule update --init --recursive`",
        )

        result = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.stdout.strip(), metadata["source_commit"])
        status = subprocess.run(
            ["git", "-C", str(source), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            status.stdout,
            "",
            "vendor/libsmctrl is immutable; put adaptations outside the submodule",
        )

        for relative_path, expected in metadata["files"].items():
            digest = hashlib.sha256((source / relative_path).read_bytes()).hexdigest()
            self.assertEqual(digest, expected, relative_path)

    def test_cuda_13_target_is_not_silently_claimed_supported(self) -> None:
        root = Path(__file__).resolve().parents[1]
        metadata = json.loads(
            (root / "vendor" / "LIBSMCTRL_SOURCE.json").read_text()
        )
        compatibility = metadata["compatibility"]

        self.assertLess(
            compatibility["latest_x86_64_stream_case"],
            compatibility["target_driver_api_version"],
        )
        self.assertEqual(
            compatibility["target_stream_mask_status"],
            "unsupported-until-semantically-probed",
        )


if __name__ == "__main__":
    unittest.main()
