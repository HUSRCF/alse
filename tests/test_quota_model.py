from __future__ import annotations

import unittest

from burstserve.quota_model import (
    QuotaModelError,
    fit_quota_latency,
    holdout_score,
    mape,
)


def _amdahl(quotas, serial, parallel):
    return [(q, serial + parallel / q) for q in quotas]


class FitTest(unittest.TestCase):
    def test_it_recovers_the_parameters_it_was_generated_from(self):
        points = _amdahl([4, 8, 16, 32], serial=0.2, parallel=8.0)
        model = fit_quota_latency(points)
        self.assertAlmostEqual(model.serial_s, 0.2, places=6)
        self.assertAlmostEqual(model.parallel_s, 8.0, places=6)

    def test_two_points_cannot_be_fitted_by_a_two_parameter_model(self):
        """Two points reproduce a two-parameter model exactly.

        A score computed from such a fit says nothing about prediction, so
        the fit is refused rather than allowed to produce a flattering
        residual of zero.
        """
        with self.assertRaises(QuotaModelError):
            fit_quota_latency(_amdahl([8, 16], serial=0.2, parallel=8.0))

    def test_repeated_quotas_do_not_count_as_distinct_evidence(self):
        points = [(8, 1.2), (8, 1.21), (8, 1.19), (8, 1.2), (8, 1.2)]
        with self.assertRaises(QuotaModelError):
            fit_quota_latency(points)

    def test_impossible_inputs_are_refused(self):
        for label, points in (
            ("zero quota", [(0, 1.0), (8, 1.0), (16, 1.0)]),
            ("negative quota", [(-4, 1.0), (8, 1.0), (16, 1.0)]),
            ("zero latency", [(4, 0.0), (8, 1.0), (16, 1.0)]),
            ("negative latency", [(4, -1.0), (8, 1.0), (16, 1.0)]),
        ):
            with self.subTest(label), self.assertRaises(QuotaModelError):
                fit_quota_latency(points)

    def test_predicting_at_a_nonpositive_quota_is_refused(self):
        model = fit_quota_latency(_amdahl([4, 8, 16], 0.1, 4.0))
        with self.assertRaises(QuotaModelError):
            model.predict(0)


class ExtrapolationTest(unittest.TestCase):
    def test_a_quota_outside_the_fitted_range_is_marked(self):
        model = fit_quota_latency(_amdahl([8, 16, 24], 0.1, 4.0))
        self.assertTrue(model.extrapolates(4))
        self.assertTrue(model.extrapolates(32))
        self.assertFalse(model.extrapolates(12))

    def test_the_endpoints_themselves_are_interpolation(self):
        model = fit_quota_latency(_amdahl([8, 16, 24], 0.1, 4.0))
        self.assertFalse(model.extrapolates(8))
        self.assertFalse(model.extrapolates(24))


class MapeTest(unittest.TestCase):
    def test_an_empty_comparison_is_refused_rather_than_scored_zero(self):
        """Nothing measured is not the same as nothing wrong."""
        with self.assertRaises(QuotaModelError):
            mape([], [])

    def test_mismatched_lengths_are_refused(self):
        with self.assertRaises(QuotaModelError):
            mape([1.0, 2.0], [1.0])

    def test_a_zero_observation_has_no_percentage_error(self):
        with self.assertRaises(QuotaModelError):
            mape([0.0], [1.0])

    def test_it_is_a_fraction_not_a_percent(self):
        self.assertAlmostEqual(mape([1.0, 2.0], [1.1, 1.8]), 0.10, places=9)


class HoldoutTest(unittest.TestCase):
    def test_a_model_that_generated_the_data_scores_near_zero(self):
        points = _amdahl([4, 8, 12, 16, 20, 24, 28, 32], 0.2, 8.0)
        result = holdout_score(points, [12, 20])
        self.assertLess(result["mape"], 1e-6)
        self.assertEqual(result["holdout_points"], 2)
        self.assertEqual(result["train_points"], 6)
        self.assertFalse(result["any_extrapolated"])

    def test_the_held_out_points_are_genuinely_excluded_from_the_fit(self):
        """A leak would make the score measure memory, not prediction."""
        points = _amdahl([4, 8, 12, 16, 20, 24, 28, 32], 0.2, 8.0)
        result = holdout_score(points, [12, 20])
        self.assertNotIn(12, result["model"]["fitted_quotas"])
        self.assertNotIn(20, result["model"]["fitted_quotas"])

    def test_an_empty_holdout_is_refused(self):
        points = _amdahl([4, 8, 16, 32], 0.2, 8.0)
        with self.assertRaises(QuotaModelError):
            holdout_score(points, [])

    def test_holding_out_a_quota_that_was_never_measured_is_refused(self):
        points = _amdahl([4, 8, 16, 32], 0.2, 8.0)
        with self.assertRaises(QuotaModelError):
            holdout_score(points, [64])

    def test_holding_out_so_much_that_the_fit_degenerates_is_refused(self):
        points = _amdahl([4, 8, 16, 32], 0.2, 8.0)
        with self.assertRaises(QuotaModelError):
            holdout_score(points, [8, 16])  # leaves only two training quotas

    def test_data_that_does_not_follow_the_form_scores_badly(self):
        """The gate has to be failable by data the model cannot describe."""
        points = [(4, 1.0), (8, 1.0), (12, 1.0), (16, 1.0), (20, 5.0),
                  (24, 1.0), (28, 1.0), (32, 1.0)]
        result = holdout_score(points, [20])
        self.assertGreater(result["mape"], 0.10)

    def test_extrapolated_holdouts_are_reported_as_such(self):
        points = _amdahl([4, 8, 12, 16, 20, 24, 28, 32], 0.2, 8.0)
        result = holdout_score(points, [4, 32])
        self.assertTrue(result["any_extrapolated"])
        self.assertTrue(all(e["extrapolated"] for e in result["errors"]))


if __name__ == "__main__":
    unittest.main()
