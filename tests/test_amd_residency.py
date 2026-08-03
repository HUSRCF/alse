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

    def test_the_duration_bound_is_a_bound_not_an_estimate(self):
        """Bytes are bounded by copy time times bandwidth.

        At 20 GB/s, 2.5 us of copying can have moved at most 50 KB, which
        is four orders below SDXL's 6.94 GB of weights. Weaker than a
        measured byte count, and the report labels it as such, but it is a
        real claim -- unlike a zero read off a column that is not there.
        """
        nanoseconds, bandwidth = 2500, 20e9
        upper = nanoseconds / 1e9 * bandwidth
        self.assertAlmostEqual(upper, 50_000.0)
        self.assertLess(upper, 6_937_676_966 * 0.01)

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

    REAL_HEADER = ('"Kind","Direction","Stream_Id","Source_Agent_Id",'
                   '"Destination_Agent_Id","Correlation_Id",'
                   '"Start_Timestamp","End_Timestamp"\n')
    REAL_ROW = ('"MEMORY_COPY","MEMORY_COPY_HOST_TO_DEVICE",0,"Agent 0",'
                '"Agent 1",{n},{start},{end}\n')

    def _real_trace(self, copies):
        rows = "".join(
            self.REAL_ROW.format(n=i, start=s, end=e)
            for i, (s, e) in enumerate(copies)
        )
        return self.REAL_HEADER + rows

    def test_a_missing_size_column_yields_none_not_zero(self):
        """The exact defect: rocprofv3 7.x emits no size column.

        Every row was skipped for want of a size, the total came out zero,
        and zero satisfies the zero-weight-traffic clause. A run that
        measured nothing passed. So an absent size column must produce
        None, which no comparison can mistake for a passing measurement.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, "trace_memory_copy_trace.csv",
                        self._real_trace([(1000, 2000), (3000, 4500)]))
            result = residency.parse_copy_trace(root)
            self.assertTrue(result["found"])
            self.assertFalse(result["size_column_present"])
            self.assertIsNone(result["host_to_device_bytes"])

    def test_the_real_trace_format_is_still_counted_and_timed(self):
        """Losing the sizes must not lose the copies as well."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, "trace_memory_copy_trace.csv",
                        self._real_trace([(1000, 2000), (3000, 4500)]))
            result = residency.parse_copy_trace(root)
            self.assertEqual(result["host_to_device_count"], 2)
            self.assertEqual(result["host_to_device_nanoseconds"], 2500)
            self.assertEqual(result["other_directions_seen"], [])

    def test_sizes_are_still_used_when_the_profiler_reports_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, "trace_memory_copy_trace.csv",
                        "Direction,Size,Start_Timestamp,End_Timestamp\n"
                        "HOST_TO_DEVICE,4096,10,20\n")
            result = residency.parse_copy_trace(root)
            self.assertTrue(result["size_column_present"])
            self.assertEqual(result["host_to_device_bytes"], 4096)
            self.assertEqual(result["host_to_device_nanoseconds"], 10)

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
