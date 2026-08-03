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
