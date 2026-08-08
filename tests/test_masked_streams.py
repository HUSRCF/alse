"""Stream pooling, and the two failures that made it necessary.

Destroying a stream between quotas hung a measurement process for 2.5
hours at 97% GPU while producing nothing, because torch's ExternalStream
and its allocator still referred to it. And a mask the runtime declines
to install produces a co-run with an unusually *low* externality, which
reads as good news. Both are guarded here without a GPU.
"""

from __future__ import annotations

import sys
import unittest

sys.dont_write_bytecode = True

from burstserve.masked_streams import (
    MaskRefused,
    MaskedStreamPool,
)


def obedient(mask: int) -> tuple[object, int]:
    """A runtime that installs what it is asked for."""
    return (f"stream-{hex(mask)}", mask)


def truncating(mask: int) -> tuple[object, int]:
    """A runtime that silently widens a mask to the full die.

    This is the failure that matters: the co-run still runs, still
    reports numbers, and the numbers are better than the truth.
    """
    return (f"stream-{hex(mask)}", (1 << 32) - 1)


class StreamsAreReused(unittest.TestCase):
    def test_the_same_mask_returns_the_same_stream(self):
        pool = MaskedStreamPool(obedient)
        first = pool.for_quota(16)
        second = pool.for_quota(16)
        self.assertIs(first, second)
        self.assertEqual(pool.creations, 1)

    def test_different_offsets_are_different_streams(self):
        """Two 16-unit masks on opposite halves are the same width and
        not the same set. A pool keyed on width would hand them one
        stream and silently un-partition them."""
        pool = MaskedStreamPool(obedient)
        left = pool.for_quota(16, offset=0)
        right = pool.for_quota(16, offset=16)
        self.assertIsNot(left, right)
        self.assertEqual(left.units, right.units)
        self.assertEqual(left.installed_mask & right.installed_mask, 0)

    def test_a_run_creates_each_stream_once(self):
        pool = MaskedStreamPool(obedient)
        for _ in range(50):
            for units in (4, 8, 16, 32):
                pool.for_quota(units)
        self.assertEqual(pool.creations, 4)
        self.assertEqual(pool.live, 4)


class AMaskThatIsNotInstalledIsFatal(unittest.TestCase):
    def test_a_widened_mask_raises(self):
        pool = MaskedStreamPool(truncating)
        with self.assertRaises(MaskRefused):
            pool.for_quota(16)

    def test_it_raises_rather_than_warning(self):
        """Continuing would measure a partition that does not exist, and
        the error is in the flattering direction: an un-partitioned
        co-run shows less interference, not more."""
        pool = MaskedStreamPool(truncating)
        try:
            pool.for_quota(8)
        except MaskRefused as exc:
            self.assertIn("not a partition", str(exc))
        else:
            self.fail("a refused mask must not be usable")

    def test_nothing_is_cached_when_the_mask_is_refused(self):
        pool = MaskedStreamPool(truncating)
        with self.assertRaises(MaskRefused):
            pool.for_quota(16)
        self.assertEqual(pool.live, 0)


class DisjointPairsAreConstructedNotAssumed(unittest.TestCase):
    def test_a_pair_shares_no_unit(self):
        pool = MaskedStreamPool(obedient)
        left, right = pool.disjoint_pair(16, 16)
        self.assertEqual(left.installed_mask & right.installed_mask, 0)
        self.assertEqual(left.popcount + right.popcount, 32)

    def test_an_asymmetric_pair_still_partitions_the_die(self):
        pool = MaskedStreamPool(obedient)
        left, right = pool.disjoint_pair(8, 24)
        self.assertEqual(left.installed_mask & right.installed_mask, 0)
        self.assertEqual(left.popcount, 8)
        self.assertEqual(right.popcount, 24)

    def test_a_pair_that_would_not_fit_is_refused(self):
        pool = MaskedStreamPool(obedient)
        with self.assertRaises(ValueError):
            pool.disjoint_pair(24, 16)

    def test_a_quota_outside_the_die_is_refused(self):
        pool = MaskedStreamPool(obedient)
        for units in (0, -1, 33):
            with self.subTest(units=units):
                with self.assertRaises(ValueError):
                    pool.for_quota(units)

    def test_an_offset_running_past_the_die_is_refused(self):
        pool = MaskedStreamPool(obedient)
        with self.assertRaises(ValueError):
            pool.for_quota(16, offset=20)


class AttestationReportsSetsNotWidths(unittest.TestCase):
    """The clause is that the measured SM set matches the manifest
    exactly. Two 16-unit masks are the same width and different sets."""

    def test_masks_are_reported_not_just_counts(self):
        pool = MaskedStreamPool(obedient)
        pool.disjoint_pair(16, 16)
        rows = pool.attestation()
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["requested_mask"] for r in rows},
                         {"0xffff", "0xffff0000"})
        self.assertTrue(all(r["matches_request"] for r in rows))

    def test_popcount_is_recorded_alongside_the_mask(self):
        pool = MaskedStreamPool(obedient)
        pool.for_quota(12)
        row = pool.attestation()[0]
        self.assertEqual(row["popcount"], 12)
        self.assertEqual(row["units"], 12)


if __name__ == "__main__":
    unittest.main()
