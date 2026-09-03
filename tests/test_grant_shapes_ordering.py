"""Grant shapes name which tenant got which width, or they name nothing.

The counter was added because Experiment 2x2 could not tell whether its
six-action policy ever issued an asymmetric grant. It then sorted the
widths descending, which throws away exactly that: expB's
``pipelined_quota`` records "24+8" in its shapes and 8 in its
urgent-quota histogram, and both are right -- the urgent tenant had 8.
A reader taking the shape as (urgent, video) would conclude the policy
issued the split 3.8 says it never did.

3.8 quotes the histogram, so the published claim is sound. The counter
was not, and this pins the repair.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import types
import unittest

sys.dont_write_bytecode = True

REPO = pathlib.Path(__file__).resolve().parent.parent


def load_grant_shapes():
    """Import just the two counters, without the runner's HIP imports."""
    source = (REPO / "scripts" / "run_amd_matrix_cell.py").read_text()
    start = source.index("def _grant_shapes(")
    end = source.index("def isolated_service(")
    module = types.ModuleType("grant_shape_counters")
    exec(compile(source[start:end], "run_amd_matrix_cell.py", "exec"),
         module.__dict__)
    return module


counters = load_grant_shapes()


def record(granted):
    return types.SimpleNamespace(granted=granted)


class OrderingTest(unittest.TestCase):
    # request id -> tenant. 1 is urgent, 2 is video.
    TENANTS = {1: "urgent", 2: "video", 3: "urgent", 4: "urgent", 5: "urgent"}

    def test_the_urgent_width_comes_first_even_when_it_is_smaller(self):
        ledger = [record({1: 8, 2: 24})]
        self.assertEqual(counters._grant_shapes(ledger, self.TENANTS),
                         {"8+24": 1})

    def test_the_old_sort_would_have_reversed_it(self):
        ledger = [record({1: 8, 2: 24})]
        self.assertEqual(counters._grant_shapes(ledger), {"24+8": 1})

    def test_the_two_orders_disagree_and_that_is_the_defect(self):
        ledger = [record({1: 8, 2: 24})]
        self.assertNotEqual(counters._grant_shapes(ledger, self.TENANTS),
                            counters._grant_shapes(ledger))

    def test_a_symmetric_grant_reads_the_same_either_way(self):
        ledger = [record({1: 16, 2: 16})]
        self.assertEqual(counters._grant_shapes(ledger, self.TENANTS),
                         counters._grant_shapes(ledger))

    def test_intra_tenant_slices_are_grouped_before_the_peer(self):
        # concurrent_quota_c4: four urgent slices of 6 beside video's 8.
        ledger = [record({1: 6, 3: 6, 4: 6, 5: 6, 2: 8})]
        self.assertEqual(counters._grant_shapes(ledger, self.TENANTS),
                         {"6+6+6+6+8": 1})
        # The old sort hides which side the 8 belongs to.
        self.assertEqual(counters._grant_shapes(ledger), {"8+6+6+6+6": 1})

    def test_the_histogram_answers_what_the_shape_cannot(self):
        ledger = [record({1: 8, 2: 24})]
        self.assertEqual(counters._urgent_units(ledger, self.TENANTS),
                         {"8": 1})

    def test_a_round_with_no_grant_is_not_counted(self):
        self.assertEqual(counters._grant_shapes([record({})], self.TENANTS),
                         {})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
