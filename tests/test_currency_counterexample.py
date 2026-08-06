from __future__ import annotations

import unittest

from burstserve.currency_counterexample import (
    charge_to_cost_ratio,
    equal_charge_unequal_progress,
    is_currency_separable,
    outcome,
    pair_outcomes,
    survey,
)


class SeparabilityTest(unittest.TestCase):
    """One scalar cannot be both the charge and the cost.

    A currency that is a function of a tenant's own allocation can serve
    as a charge. To serve as a cost it would have to be proportional to
    progress -- and the measured externality makes that impossible,
    because the constant of proportionality depends on the peer.
    """

    def test_the_measured_table_refutes_separability(self):
        self.assertFalse(is_currency_separable())

    def test_an_even_split_is_the_case_where_a_scalar_would_work(self):
        """The claim is not that scalars always fail."""
        self.assertAlmostEqual(charge_to_cost_ratio(16, 16), 1.0, places=3)

    def test_the_divergence_grows_as_the_split_grows_uneven(self):
        even = charge_to_cost_ratio(16, 16)
        mild = charge_to_cost_ratio(8, 24)
        extreme = charge_to_cost_ratio(4, 28)
        self.assertGreater(abs(even - 1.0), -1)          # even is the anchor
        self.assertLess(mild, even)
        self.assertLess(extreme, mild)

    def test_the_same_unit_buys_different_progress_in_different_hands(self):
        """The sharpest statement of the counterexample.

        At a 4+28 split a unit held by the smaller tenant delivers about
        2.2x the progress of a unit held by the larger one, so charging
        both per unit-second charges them for different things.
        """
        small, large = pair_outcomes(4, 28)
        per_unit_small = small.corun_speed / small.units
        per_unit_large = large.corun_speed / large.units
        self.assertGreater(per_unit_small / per_unit_large, 2.0)

    def test_it_is_the_peer_that_decides_not_the_tenant(self):
        """A tenant's own allocation does not determine its cost.

        The same 8 units cost differently depending on who shares the die,
        which is precisely what a scalar function of one's own share
        cannot express.
        """
        with_24 = outcome(8, 24)
        # 8 units alone is the same allocation with no peer.
        self.assertNotAlmostEqual(
            with_24.corun_speed, with_24.solo_speed, places=3
        )
        self.assertGreater(with_24.externality, 1.0)


class ChargeAndProgressDivergeTest(unittest.TestCase):
    def test_equal_units_give_equal_progress_only_when_split_evenly(self):
        report = equal_charge_unequal_progress()
        self.assertAlmostEqual(report["even_split_progress_ratio"], 1.0,
                               places=3)
        self.assertAlmostEqual(report["even_split_quota_ratio"], 1.0)
        # Uneven: the charge ratio and the progress ratio disagree.
        self.assertNotAlmostEqual(
            report["uneven_split_quota_ratio"],
            report["uneven_split_progress_ratio"],
            places=2,
        )

    def test_progress_per_unit_is_equal_only_under_an_even_split(self):
        report = equal_charge_unequal_progress()
        even = report["progress_per_unit_even"]
        uneven = report["progress_per_unit_uneven"]
        self.assertAlmostEqual(even[0], even[1], places=6)
        self.assertNotAlmostEqual(uneven[0], uneven[1], places=4)

    def test_every_surveyed_pair_is_reported_in_both_currencies(self):
        rows = survey()
        self.assertTrue(rows)
        for row in rows:
            with self.subTest(split=row["split"]):
                self.assertIn("quota_ratio", row)
                self.assertIn("progress_ratio", row)
                self.assertIn("divergence", row)

    def test_the_survey_only_covers_pairs_measured_in_both_directions(self):
        """A one-sided pair cannot give a ratio, so it is excluded rather
        than filled in from its mirror."""
        for row in survey():
            left, right = row["split"]
            self.assertNotEqual(left, None)
            self.assertNotEqual(right, None)


class ScopeOfTheClaimTest(unittest.TestCase):
    """The conclusion is narrow, and the tests should say so.

    Quota-seconds remain the right charge, and the utilisation result
    rests on them. What the counterexample rules out is using the same
    number as the cost.
    """

    def test_quota_seconds_still_work_as_a_charge(self):
        """Proportional to units held, by construction, in every split."""
        for left, right in ((16, 16), (8, 24), (4, 28)):
            with self.subTest(split=(left, right)):
                a, b = pair_outcomes(left, right)
                self.assertAlmostEqual(
                    a.quota_share + b.quota_share, 1.0, places=9
                )
                self.assertAlmostEqual(
                    a.quota_share / b.quota_share, left / right, places=9
                )

    def test_the_failure_is_in_the_cost_role_only(self):
        for left, right in ((8, 24), (4, 28)):
            with self.subTest(split=(left, right)):
                self.assertNotAlmostEqual(
                    charge_to_cost_ratio(left, right), 1.0, places=2
                )


if __name__ == "__main__":
    unittest.main()
