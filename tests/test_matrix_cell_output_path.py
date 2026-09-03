"""Where a cell is written, and the campaign that lost 28 groups to it.

expC's four arms each ran in their own process, because
``requests_per_tenant`` is a runtime setting and one process cannot hold
two values of it. ``run_amd_matrix_cell`` substituted POLICY into --out
only when it was given more than one policy, so every arm of a group
wrote to the same literal ``..._POLICY.json`` path and the last one
overwrote the other three. The campaign's own guard counted files rather
than arms, so it never noticed, and it ran that way for 28 groups.
"""

from __future__ import annotations

import sys
import unittest

sys.dont_write_bytecode = True

# The rule lives in the library rather than in the cell runner, because
# the runner opens libamdhip64 at import time and cannot be imported on a
# host without it -- which is every host where this suite runs.
from burstserve.matrix_results import output_path


class OutputPathTest(unittest.TestCase):
    def test_one_policy_still_substitutes_when_the_template_asks(self):
        got = output_path("runs/expC/cell_l0.6_s0_POLICY.json",
                               "concurrent_quota_c4", many=False)
        self.assertEqual(got.name, "cell_l0.6_s0_concurrent_quota_c4.json")

    def test_several_policies_substitute_as_they_always_did(self):
        got = output_path("runs/expA/cell_s0_POLICY.json",
                               "exclusive_priority", many=True)
        self.assertEqual(got.name, "cell_s0_exclusive_priority.json")

    def test_a_template_without_the_marker_is_left_alone(self):
        got = output_path("runs/one_cell.json", "static_even",
                               many=False)
        self.assertEqual(got.name, "one_cell.json")

    def test_distinct_policies_never_share_a_path(self):
        template = "runs/expC/cell_bl_l1.05_b4_s7_POLICY.json"
        arms = ("exclusive_priority", "fixed_split_24",
                "concurrent_quota_c2", "concurrent_quota_c4")
        paths = {output_path(template, arm, many=False) for arm in arms}
        self.assertEqual(len(paths), len(arms))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
