from __future__ import annotations

import ast
import inspect
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import run_amd_cold_model as cold  # noqa: E402


def _bw(megabytes, pinned, bps):
    return {"megabytes": megabytes, "pinned": pinned, "bytes_per_second": bps}


class PredictorHasNoFreeParametersTest(unittest.TestCase):
    """Gate B requires the cold prediction to use missing bytes and a
    measured bandwidth, and nothing else.

    The shortcut that would otherwise satisfy the clause is fitting a
    constant to the observed load times and calling the result a
    prediction. These tests make the absence of such a constant checkable
    instead of asserted.
    """

    def test_it_is_exactly_the_quotient(self):
        self.assertEqual(cold.predict_cold_seconds(7_000_000_000, 7e9), 1.0)
        self.assertEqual(cold.predict_cold_seconds(0, 7e9), 0.0)

    def test_it_is_linear_in_bytes_and_inverse_in_bandwidth(self):
        base = cold.predict_cold_seconds(4_000_000_000, 8e9)
        self.assertAlmostEqual(
            cold.predict_cold_seconds(8_000_000_000, 8e9), 2 * base
        )
        self.assertAlmostEqual(
            cold.predict_cold_seconds(4_000_000_000, 16e9), base / 2
        )

    def test_the_source_contains_no_numeric_constant_at_all(self):
        """A fudge factor would have to appear as a literal somewhere."""
        tree = ast.parse(inspect.getsource(cold.predict_cold_seconds))
        body = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Return)
        ]
        literals = [
            node for stmt in body for node in ast.walk(stmt)
            if isinstance(node, ast.Constant) and isinstance(
                node.value, (int, float)
            )
        ]
        self.assertEqual(
            literals, [], "the predictor's return expression carries a constant"
        )

    def test_impossible_inputs_are_refused(self):
        for label, args in (
            ("negative bytes", (-1, 8e9)),
            ("zero bandwidth", (1000, 0.0)),
            ("negative bandwidth", (1000, -8e9)),
        ):
            with self.subTest(label), self.assertRaises(ValueError):
                cold.predict_cold_seconds(*args)


class SizeDependentBandwidthTest(unittest.TestCase):
    """One aggregate bandwidth is the wrong term for a pipeline move.

    .to(device) issues one copy per tensor, and SDXL's tensors have a
    median far below a bandwidth benchmark's block, so the big-block figure
    over-predicted by 72.5% against a 10% gate. Making bandwidth a function
    of size introduces no fitted constant: every value is still measured.
    """

    CURVE = [(65_536, 2e9), (1_048_576, 10e9), (268_435_456, 28e9)]

    def test_it_picks_the_measured_size_at_or_below_the_request(self):
        self.assertEqual(cold.bandwidth_for_size(self.CURVE, 65_536), 2e9)
        self.assertEqual(cold.bandwidth_for_size(self.CURVE, 100_000), 2e9)
        self.assertEqual(cold.bandwidth_for_size(self.CURVE, 1_048_576), 10e9)
        self.assertEqual(cold.bandwidth_for_size(self.CURVE, 1e9), 28e9)

    def test_a_tensor_below_every_measured_size_uses_the_smallest(self):
        """Never the large-block figure -- that is the direction that
        under-predicts the transfer time and flatters the gate."""
        self.assertEqual(cold.bandwidth_for_size(self.CURVE, 1024), 2e9)

    def test_many_small_tensors_cost_more_than_their_byte_count_suggests(self):
        total = 64 * 65_536
        aggregate = cold.predict_cold_seconds(total, 28e9)
        per_tensor = cold.predict_cold_seconds_by_tensor(
            [65_536] * 64, self.CURVE
        )
        self.assertGreater(per_tensor, aggregate)
        self.assertAlmostEqual(per_tensor, total / 2e9)

    def test_one_large_tensor_agrees_with_the_aggregate_form(self):
        """The two forms must not disagree where they should not."""
        self.assertAlmostEqual(
            cold.predict_cold_seconds_by_tensor([1_000_000_000], self.CURVE),
            cold.predict_cold_seconds(1_000_000_000, 28e9),
        )

    def test_zero_sized_tensors_do_not_break_the_sum(self):
        self.assertEqual(cold.predict_cold_seconds_by_tensor([], self.CURVE), 0)
        self.assertEqual(
            cold.predict_cold_seconds_by_tensor([0, 0], self.CURVE), 0
        )

    def test_an_unmeasured_curve_is_refused(self):
        with self.assertRaises(ValueError):
            cold.bandwidth_for_size([], 1024)

    def test_the_per_tensor_form_still_has_no_fitted_constant(self):
        import ast
        import inspect

        tree = ast.parse(
            inspect.getsource(cold.predict_cold_seconds_by_tensor)
        )
        literals = [
            n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, (int, float))
            and not isinstance(n.value, bool)
        ]
        # The only numeric literal permitted is the `size > 0` guard.
        self.assertEqual(literals, [0])


class BandwidthSelectionTest(unittest.TestCase):
    def test_it_takes_the_widest_transfer_of_the_requested_kind(self):
        samples = [
            _bw(64, True, 5e9), _bw(1024, True, 20e9),
            _bw(64, False, 3e9), _bw(1024, False, 9e9),
        ]
        self.assertEqual(
            cold.widest_transfer(samples, pinned=True)["bytes_per_second"], 20e9
        )
        self.assertEqual(
            cold.widest_transfer(samples, pinned=False)["bytes_per_second"], 9e9
        )

    def test_pinned_and_pageable_are_never_mixed(self):
        """Quoting the pinned figure for a pageable transfer would overstate
        what the scheduler can rely on, since .to(device) stages through
        pageable memory unless the caller pinned it."""
        samples = [_bw(1024, True, 20e9), _bw(256, False, 9e9)]
        self.assertTrue(cold.widest_transfer(samples, pinned=True)["pinned"])
        self.assertFalse(cold.widest_transfer(samples, pinned=False)["pinned"])

    def test_an_absent_kind_is_refused_rather_than_substituted(self):
        with self.assertRaises(ValueError):
            cold.widest_transfer([_bw(1024, True, 20e9)], pinned=False)


if __name__ == "__main__":
    unittest.main()
