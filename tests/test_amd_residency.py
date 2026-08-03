from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import run_amd_residency as residency  # noqa: E402


class CopyTraceParsingTest(unittest.TestCase):
    def _write(self, directory: Path, name: str, text: str) -> None:
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_it_sums_only_host_to_device_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, "trace_memory_copy_trace.csv",
                        "Direction,Size\n"
                        "HOST_TO_DEVICE,1000\n"
                        "DEVICE_TO_HOST,7777\n"
                        "HOST_TO_DEVICE,234\n"
                        "DEVICE_TO_DEVICE,5555\n")
            result = residency.parse_copy_trace(root)
            self.assertTrue(result["found"])
            self.assertEqual(result["host_to_device_bytes"], 1234)
            self.assertEqual(result["rows"], 4)

    def test_an_absent_trace_is_reported_not_counted_as_zero(self):
        """No trace is not evidence of no traffic.

        Zero bytes would satisfy the gate, so a profiler that produced
        nothing must not be indistinguishable from one that observed
        silence.
        """
        with tempfile.TemporaryDirectory() as tmp:
            result = residency.parse_copy_trace(Path(tmp))
            self.assertFalse(result["found"])
            self.assertIsNone(result["host_to_device_bytes"])

    def test_unrecognised_directions_are_surfaced_rather_than_dropped(self):
        """A renamed column would otherwise silently total zero."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, "trace_memory_copy_trace.csv",
                        "Direction,Size\nSOMETHING_NEW,4096\n")
            result = residency.parse_copy_trace(root)
            self.assertEqual(result["host_to_device_bytes"], 0)
            self.assertEqual(result["other_directions_seen"], ["SOMETHING_NEW"])

    def test_it_accepts_the_alternate_column_spellings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, "a/trace_memory_copy_trace.csv",
                        "Kind,Bytes\nHtoD,512\n")
            self.assertEqual(
                residency.parse_copy_trace(root)["host_to_device_bytes"], 512
            )

    def test_multiple_trace_files_are_combined(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, "a/trace_memory_copy_trace.csv",
                        "Direction,Size\nHOST_TO_DEVICE,100\n")
            self._write(root, "b/other_memory_copy_trace.csv",
                        "Direction,Size\nHOST_TO_DEVICE,25\n")
            self.assertEqual(
                residency.parse_copy_trace(root)["host_to_device_bytes"], 125
            )


class SlopeTest(unittest.TestCase):
    def test_it_separates_the_one_off_load_from_the_per_rotation_cost(self):
        load, per = 7_000_000_000, 4_000_000
        points = [(r, load + per * r) for r in (1, 3, 5)]
        fit = residency.fit_slope(points)
        self.assertAlmostEqual(fit["bytes_per_rotation"], per, delta=1)
        self.assertAlmostEqual(fit["load_bytes"], load, delta=1)

    def test_a_resident_model_has_a_slope_of_zero(self):
        points = [(r, 7_000_000_000) for r in (1, 3, 5)]
        fit = residency.fit_slope(points)
        self.assertAlmostEqual(fit["bytes_per_rotation"], 0.0, places=6)

    def test_reloading_the_weights_every_rotation_is_visible(self):
        """The failing case has to be distinguishable from the passing one."""
        weights = 7_000_000_000
        points = [(r, weights * r) for r in (1, 3, 5)]
        fit = residency.fit_slope(points)
        self.assertAlmostEqual(fit["bytes_per_rotation"], weights, delta=1)
        self.assertGreater(fit["bytes_per_rotation"], weights * 0.01)

    def test_one_rotation_count_cannot_separate_the_two(self):
        with self.assertRaises(ValueError):
            residency.fit_slope([(3, 100), (3, 101), (3, 99)])


if __name__ == "__main__":
    unittest.main()
