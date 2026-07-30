from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from burstserve.provenance import RunManifest, write_json_atomic
from burstserve.results import RESULTS_SCHEMA_VERSION, aggregate_runs, main


def _manifest(
    *,
    arm: str,
    seed: int,
    source_revision: str = "source-a",
) -> RunManifest:
    return RunManifest.create(
        config={"arm": arm, "seed": seed},
        seed=seed,
        source_revision=source_revision,
        environment={},
        metadata={},
        created_at_utc="2026-07-30T00:00:00Z",
    )


def _run_directory(root: Path, manifest: RunManifest) -> Path:
    directory = root / manifest.run_id
    directory.mkdir()
    write_json_atomic(directory / "manifest.json", manifest.to_dict())
    return directory


class AggregateRunsTest(unittest.TestCase):
    def test_keeps_accepted_failed_incomplete_and_invalid_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            accepted_manifest = _manifest(arm="stepswap", seed=0)
            accepted = _run_directory(root, accepted_manifest)
            write_json_atomic(
                accepted / "outcome.json",
                {
                    "exit_code": 0,
                    "timed_out": False,
                    "smoke_accepted": True,
                },
            )
            write_json_atomic(
                accepted / "summary.json",
                {
                    "n_urgent": 3,
                    "urgent_p99_s": 1.25,
                    "raw_log": "/machine-specific/path",
                },
            )

            failed_manifest = _manifest(arm="stepswap", seed=1)
            failed = _run_directory(root, failed_manifest)
            write_json_atomic(
                failed / "outcome.json",
                {
                    "exit_code": 7,
                    "timed_out": False,
                    "smoke_accepted": False,
                },
            )

            incomplete_manifest = _manifest(arm="offload_tiled", seed=2)
            _run_directory(root, incomplete_manifest)

            mismatched_manifest = _manifest(arm="stepswap", seed=3)
            mismatched = root / "wrong-directory-name"
            mismatched.mkdir()
            write_json_atomic(mismatched / "manifest.json", mismatched_manifest.to_dict())

            aggregate = aggregate_runs(root)

            self.assertEqual(aggregate["schema_version"], RESULTS_SCHEMA_VERSION)
            self.assertEqual(aggregate["counts"]["discovered"], 4)
            self.assertEqual(aggregate["counts"]["selected"], 4)
            self.assertEqual(
                aggregate["counts"]["by_status"],
                {
                    "accepted": 1,
                    "completed": 0,
                    "failed": 1,
                    "incomplete": 1,
                    "invalid": 1,
                    "timed_out": 0,
                },
            )
            rows = {row["run_id"]: row for row in aggregate["rows"]}
            self.assertEqual(rows[accepted_manifest.run_id]["status"], "accepted")
            self.assertTrue(
                rows[accepted_manifest.run_id]["semantic_accepted"]
            )
            self.assertEqual(rows[failed_manifest.run_id]["status"], "failed")
            self.assertEqual(rows[incomplete_manifest.run_id]["status"], "incomplete")
            self.assertEqual(rows["wrong-directory-name"]["status"], "invalid")
            self.assertIn(
                "run directory does not match",
                rows["wrong-directory-name"]["validation_errors"][0],
            )
            self.assertEqual(
                rows[accepted_manifest.run_id]["summary_metrics"],
                {"n_urgent": 3, "urgent_p99_s": 1.25},
            )

    def test_filters_valid_manifests_but_retains_invalid_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selected_manifest = _manifest(arm="stepswap", seed=0)
            selected = _run_directory(root, selected_manifest)
            write_json_atomic(
                selected / "outcome.json",
                {"exit_code": 4, "timed_out": False, "smoke_accepted": False},
            )
            _run_directory(root, _manifest(arm="offload_tiled", seed=1))
            (root / "missing-manifest").mkdir()

            aggregate = aggregate_runs(
                root,
                source_revision="source-a",
                arm="stepswap",
            )

            self.assertEqual(aggregate["counts"]["discovered"], 3)
            self.assertEqual(aggregate["counts"]["filtered_out"], 1)
            self.assertEqual(aggregate["counts"]["selected"], 2)
            self.assertEqual(
                [row["run_id"] for row in aggregate["rows"]],
                [selected_manifest.run_id, "missing-manifest"],
            )
            self.assertEqual(aggregate["rows"][0]["status"], "failed")
            self.assertEqual(aggregate["rows"][1]["status"], "invalid")

    def test_timed_out_and_legacy_completed_are_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            timed_out = _run_directory(root, _manifest(arm="stepswap", seed=0))
            write_json_atomic(
                timed_out / "outcome.json",
                {"exit_code": 124, "timed_out": True, "smoke_accepted": False},
            )
            legacy = _run_directory(root, _manifest(arm="stepswap", seed=1))
            write_json_atomic(
                legacy / "outcome.json",
                {"exit_code": 0, "timed_out": False},
            )

            aggregate = aggregate_runs(root)

            self.assertEqual(aggregate["counts"]["by_status"]["timed_out"], 1)
            self.assertEqual(aggregate["counts"]["by_status"]["completed"], 1)

    def test_output_is_deterministic_and_cli_writes_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "runs"
            root.mkdir()
            run = _run_directory(root, _manifest(arm="stepswap", seed=0))
            write_json_atomic(
                run / "outcome.json",
                {"exit_code": 0, "timed_out": False, "smoke_accepted": True},
            )
            output = Path(temporary) / "nested" / "aggregate.json"

            self.assertEqual(
                main(
                    [
                        "--run-root",
                        str(root),
                        "--output",
                        str(output),
                        "--arm",
                        "stepswap",
                    ]
                ),
                0,
            )
            first = output.read_bytes()
            self.assertEqual(
                main(
                    [
                        "--run-root",
                        str(root),
                        "--output",
                        str(output),
                        "--arm",
                        "stepswap",
                    ]
                ),
                0,
            )

            self.assertEqual(output.read_bytes(), first)
            self.assertEqual(json.loads(first)["counts"]["selected"], 1)
            self.assertEqual(list(output.parent.glob(f".{output.name}.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
