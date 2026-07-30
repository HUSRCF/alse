from __future__ import annotations

import json
import math
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from burstserve.provenance import (
    EVENT_RECORD_SCHEMA_VERSION,
    RUN_MANIFEST_SCHEMA_VERSION,
    EventRecord,
    RunManifest,
    append_jsonl_atomic,
    canonical_json,
    collect_environment,
    derive_run_id,
    read_json,
    read_jsonl,
    write_json_atomic,
    write_jsonl_atomic,
)


class RunManifestTest(unittest.TestCase):
    def test_run_id_is_independent_of_mapping_order(self) -> None:
        first = {"model": "sdxl", "shape": {"width": 512, "height": 512}}
        second = {"shape": {"height": 512, "width": 512}, "model": "sdxl"}

        self.assertEqual(
            derive_run_id(first, 17, "git:abc123"),
            derive_run_id(second, 17, "git:abc123"),
        )

    def test_run_id_changes_with_each_semantic_input(self) -> None:
        base = derive_run_id({"model": "sdxl"}, 17, "git:abc123")
        self.assertNotEqual(base, derive_run_id({"model": "flux"}, 17, "git:abc123"))
        self.assertNotEqual(base, derive_run_id({"model": "sdxl"}, 18, "git:abc123"))
        self.assertNotEqual(base, derive_run_id({"model": "sdxl"}, 17, "git:def456"))

    def test_manifest_round_trip_and_input_detachment(self) -> None:
        config = {"models": ["cogvideox", "sdxl"]}
        manifest = RunManifest.create(
            config=config,
            seed=0,
            source_revision="archive:sha256",
            environment={"host": {"hostname": "test-host"}},
            metadata={"purpose": "unit-test"},
            created_at_utc="2026-07-30T00:00:00Z",
        )
        config["models"].append("mutated")

        restored = RunManifest.from_dict(manifest.to_dict())
        self.assertEqual(restored, manifest)
        self.assertEqual(manifest.schema_version, RUN_MANIFEST_SCHEMA_VERSION)
        self.assertEqual(manifest.config["models"], ["cogvideox", "sdxl"])

    def test_manifest_rejects_tampered_run_id(self) -> None:
        manifest = RunManifest.create(
            config={"model": "sdxl"},
            seed=0,
            source_revision="git:abc",
            environment={},
        )
        value = manifest.to_dict()
        value["run_id"] = "tampered"
        with self.assertRaisesRegex(ValueError, "does not match"):
            RunManifest.from_dict(value)

    def test_invalid_json_numbers_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            canonical_json({"invalid": math.nan})


class EnvironmentTest(unittest.TestCase):
    def test_collects_cuda_visibility_without_torch(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"CUDA_VISIBLE_DEVICES": "3,1", "BURSTSERVE_CUDA_TAG": "test"},
            clear=False,
        ):
            environment = collect_environment(["BURSTSERVE_CUDA_TAG"])

        self.assertEqual(
            environment["cuda_environment"]["CUDA_VISIBLE_DEVICES"], "3,1"
        )
        self.assertEqual(
            environment["cuda_environment"]["BURSTSERVE_CUDA_TAG"], "test"
        )
        self.assertIn("version", environment["python"])
        self.assertIn("platform", environment["platform"])
        self.assertIn("hostname", environment["host"])


class EventRecordTest(unittest.TestCase):
    def test_event_round_trip(self) -> None:
        record = EventRecord.create(
            run_id="bs1-test",
            sequence=2,
            event_type="quantum.completed",
            payload={"steps": 1, "duration_ms": 19.5},
            timestamp_utc="2026-07-30T00:00:01Z",
        )

        self.assertEqual(EventRecord.from_dict(record.to_dict()), record)
        self.assertEqual(record.schema_version, EVENT_RECORD_SCHEMA_VERSION)

    def test_event_validates_sequence(self) -> None:
        with self.assertRaises(ValueError):
            EventRecord.create(
                run_id="bs1-test",
                sequence=-1,
                event_type="invalid",
            )


class AtomicOutputTest(unittest.TestCase):
    def test_atomic_json_round_trip_and_canonical_format(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "nested" / "manifest.json"
            value = {"z": 1, "a": {"b": 2}}

            write_json_atomic(path, value)

            self.assertEqual(read_json(path), value)
            self.assertEqual(path.read_text(), '{"a":{"b":2},"z":1}\n')
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

    def test_serialization_failure_preserves_existing_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manifest.json"
            write_json_atomic(path, {"state": "accepted"})

            with self.assertRaises(ValueError):
                write_json_atomic(path, {"state": math.inf})

            self.assertEqual(read_json(path), {"state": "accepted"})

    def test_atomic_jsonl_replace_and_append(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            first = EventRecord.create(
                run_id="bs1-test",
                sequence=0,
                event_type="run.started",
                timestamp_utc="2026-07-30T00:00:00Z",
            )
            second = EventRecord.create(
                run_id="bs1-test",
                sequence=1,
                event_type="run.completed",
                timestamp_utc="2026-07-30T00:00:01Z",
            )

            write_jsonl_atomic(path, [first])
            append_jsonl_atomic(path, second)

            values = read_jsonl(path)
            self.assertEqual(values, [first.to_dict(), second.to_dict()])
            self.assertTrue(path.read_bytes().endswith(b"\n"))
            for line in path.read_text().splitlines():
                json.loads(line)


if __name__ == "__main__":
    unittest.main()
