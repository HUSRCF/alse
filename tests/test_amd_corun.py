from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import amd_profile_cell as cell  # noqa: E402
import run_amd_corun as corun  # noqa: E402


def _record(windows):
    return {
        "sample_windows": [
            {"start_wall": s, "end_wall": e, "s": e - s} for s, e in windows
        ],
        "window_start_wall": windows[0][0],
        "window_end_wall": windows[-1][1],
    }


class DisjointMaskTest(unittest.TestCase):
    def test_the_two_masks_never_share_a_unit(self):
        a, b = corun.disjoint_masks(12, 20)
        self.assertEqual(int(a, 0) & int(b, 0), 0)
        self.assertEqual(bin(int(a, 0)).count("1"), 12)
        self.assertEqual(bin(int(b, 0)).count("1"), 20)

    def test_a_pair_that_would_oversubscribe_the_die_is_refused(self):
        """Two masks summing past 32 would overlap, making it not a partition."""
        with self.assertRaises(ValueError):
            corun.disjoint_masks(20, 20)

    def test_an_empty_side_is_refused(self):
        for pair in ((0, 16), (16, 0), (-4, 16)):
            with self.subTest(pair=pair), self.assertRaises(ValueError):
                corun.disjoint_masks(*pair)


class BarrierTest(unittest.TestCase):
    """Neither side may start measuring before the other is warmed up.

    Without the barrier the faster process finishes its whole sample set
    and exits before the slower one starts, and the result is reported as
    a co-run while measuring nothing of the kind.
    """

    def test_neither_side_is_released_until_both_arrive(self):
        import tempfile
        import threading

        with tempfile.TemporaryDirectory() as tmp:
            released = {}

            def side(name, peer, delay):
                time.sleep(delay)
                released[name] = cell.wait_at_barrier(tmp, name, peer, 30.0)

            threads = [
                threading.Thread(target=side, args=("a", "b", 0.0)),
                threading.Thread(target=side, args=("b", "a", 0.25)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)

            self.assertEqual(set(released), {"a", "b"})
            # The early arrival must not have been released at its own
            # ready time -- it has to have waited for the late one.
            self.assertGreaterEqual(
                released["a"]["released_at"], released["b"]["self_ready_at"]
            )

    def test_a_peer_that_never_arrives_times_out_rather_than_proceeding(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(TimeoutError):
                cell.wait_at_barrier(tmp, "a", "never", 0.2)

    def test_each_side_records_when_both_became_ready(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "b.ready").write_text(repr(time.time()), encoding="utf-8")
            record = cell.wait_at_barrier(tmp, "a", "b", 5.0)
            self.assertIn("self_ready_at", record)
            self.assertIn("peer_ready_at", record)
            self.assertGreaterEqual(record["released_at"],
                                    record["self_ready_at"])


class DeadlineSamplingTest(unittest.TestCase):
    """Co-run cells sample for a duration, not for a count.

    Equal sample counts would let the faster side finish early and leave
    the slower one running alone for the tail, which is the part that would
    read as contention-free.
    """

    def test_it_stops_on_the_deadline_not_on_a_count(self):
        calls = []
        samples = cell.measure(
            lambda: (time.sleep(0.01), calls.append(1)),
            warmup=0, samples=0, sync=lambda: None,
            deadline=time.time() + 0.15,
        )
        self.assertGreater(len(samples), 2)
        self.assertLess(len(samples), 40)
        self.assertEqual(len(samples), len(calls))

    def test_every_sample_carries_the_window_it_ran_in(self):
        samples = cell.measure(
            lambda: time.sleep(0.005), warmup=0, samples=3,
            sync=lambda: None,
        )
        self.assertEqual(len(samples), 3)
        for entry in samples:
            self.assertLessEqual(entry["start_wall"], entry["end_wall"])
            self.assertAlmostEqual(
                entry["end_wall"] - entry["start_wall"], entry["s"], places=6
            )

    def test_the_windows_are_ordered_and_do_not_overlap_each_other(self):
        samples = cell.measure(
            lambda: time.sleep(0.005), warmup=0, samples=4,
            sync=lambda: None,
        )
        for earlier, later in zip(samples, samples[1:]):
            self.assertLessEqual(earlier["end_wall"], later["start_wall"])

    def test_warmup_samples_are_not_returned(self):
        calls = []
        samples = cell.measure(
            lambda: calls.append(1), warmup=5, samples=3, sync=lambda: None
        )
        self.assertEqual(len(samples), 3)
        self.assertEqual(len(calls), 8)


class MemoryHeadroomTest(unittest.TestCase):
    """A co-run that does not fit measures the wrong thing.

    Two SDXL cells at 16 units each peaked at 19.92 GB apiece on a 32 GB
    card (2026-08-03), so the pair cannot be resident. Run anyway it either
    dies of OOM, producing nothing, or thrashes the allocator and produces
    an externality that is really memory pressure wearing a CU-contention
    label. The second is worse, so the check runs before the pair does.
    """

    def _solo(self, reserved, total=34_359_738_368):
        return {"peak_memory_reserved_bytes": reserved,
                "total_memory_bytes": total}

    def test_a_pair_that_fits_is_allowed(self):
        result = corun.memory_headroom(
            self._solo(10_000_000_000), self._solo(10_000_000_000), safety=0.9
        )
        self.assertTrue(result["fits"])

    def test_the_measured_sdxl_pair_does_not_fit(self):
        result = corun.memory_headroom(
            self._solo(19_920_000_000), self._solo(19_920_000_000), safety=0.9
        )
        self.assertFalse(result["fits"])

    def test_the_budget_is_the_declared_fraction_of_the_card(self):
        card = 34_359_738_368
        half = int(card * 0.45)
        self.assertTrue(
            corun.memory_headroom(self._solo(half), self._solo(half),
                                  safety=0.95)["fits"]
        )
        self.assertFalse(
            corun.memory_headroom(self._solo(half), self._solo(half),
                                  safety=0.85)["fits"]
        )

    def test_an_unknown_peak_is_reported_as_unknown_not_as_fitting(self):
        """Missing data must not read as permission to proceed."""
        for a, b in (
            ({}, self._solo(1)),
            (self._solo(1), {}),
            ({"peak_memory_reserved_bytes": 1}, {"peak_memory_reserved_bytes": 1}),
        ):
            with self.subTest():
                result = corun.memory_headroom(a, b, safety=0.9)
                self.assertFalse(result["known"])
                self.assertNotIn("fits", result)


class OverlapTest(unittest.TestCase):
    """Two processes that took turns produce a clean-looking co-run.

    Neither slowed the other down, so the externality reads as zero. The
    overlap window is what distinguishes that from a real co-run, so these
    tests pin it in both directions.
    """

    def test_processes_that_never_ran_together_have_no_overlap(self):
        a = _record([(0.0, 1.0), (1.0, 2.0)])
        b = _record([(10.0, 11.0), (11.0, 12.0)])
        result = corun.restrict_to_overlap(a, b, 0.8)
        self.assertEqual(result["overlap_seconds"], 0.0)
        self.assertFalse(result["sufficient_overlap"])
        self.assertEqual(result["a"]["samples_in_overlap"], 0)
        self.assertNotIn("p50_s", result["a"])

    def test_a_fully_concurrent_pair_keeps_every_sample(self):
        a = _record([(0.0, 1.0), (1.0, 2.0), (2.0, 3.0)])
        b = _record([(0.0, 1.5), (1.5, 3.0)])
        result = corun.restrict_to_overlap(a, b, 0.8)
        self.assertEqual(result["overlap_seconds"], 3.0)
        self.assertTrue(result["sufficient_overlap"])
        self.assertEqual(result["a"]["samples_in_overlap"], 3)
        self.assertEqual(result["b"]["samples_in_overlap"], 2)

    def test_samples_straddling_the_window_edge_are_dropped(self):
        """A sample only partly contended would dilute the externality.

        It would do so towards zero, which is the direction that makes the
        result look better, so partial samples are excluded rather than
        prorated.
        """
        a = _record([(0.0, 1.0), (1.0, 2.0), (2.0, 3.0)])
        b = _record([(0.9, 2.1)])
        result = corun.restrict_to_overlap(a, b, 0.1)
        self.assertEqual(result["a"]["samples_in_overlap"], 1)

    def test_a_barely_overlapping_pair_is_flagged_not_silently_reported(self):
        a = _record([(0.0, 1.0), (1.0, 10.0)])
        b = _record([(9.5, 10.5)])
        result = corun.restrict_to_overlap(a, b, 0.8)
        self.assertLess(result["overlap_fraction_of_longer_window"], 0.8)
        self.assertFalse(result["sufficient_overlap"])

    def test_the_overlap_threshold_is_the_declared_one(self):
        a = _record([(0.0, 10.0)])
        b = _record([(0.0, 8.5)])
        self.assertTrue(
            corun.restrict_to_overlap(a, b, 0.80)["sufficient_overlap"]
        )
        self.assertFalse(
            corun.restrict_to_overlap(a, b, 0.90)["sufficient_overlap"]
        )

    def test_statistics_need_at_least_two_samples_in_the_window(self):
        a = _record([(0.0, 1.0)])
        b = _record([(0.0, 1.0)])
        result = corun.restrict_to_overlap(a, b, 0.5)
        self.assertEqual(result["a"]["samples_in_overlap"], 1)
        self.assertNotIn("cv", result["a"])


if __name__ == "__main__":
    unittest.main()
