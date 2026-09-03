"""The second SKU's cost tables, and the shape they refuse to have.

gfx1201's tables are Amdahl: latency = serial + parallel/quota, fitted by
``fit_quota_latency`` and scored by Gate B at a 10% held-out MAPE. The
same fit on gfx90a returns a negative serial term, which is not a
physical quantity. These tests pin that as a measured property of the
device rather than leaving it in a comment, because the recorded
``serial_fraction`` of 0.0 looks like a fit and is not one.
"""

from __future__ import annotations

import sys
import unittest

sys.dont_write_bytecode = True

from burstserve.quota_model import fit_quota_latency
from burstserve.trace_sim import (
    MEASURED_EXTERNALITY,
    MEASURED_EXTERNALITY_GFX90A,
    MEASURED_MODELS_GFX90A,
    MEASURED_QUOTA_SECONDS,
    MEASURED_QUOTA_SECONDS_GFX90A,
    QuotaCostModel,
    externality,
)

MODELS = ("sdxl", "cogvideox-2b")
MASKABLE = 104


class TablesLoadTest(unittest.TestCase):
    def test_both_models_load_for_the_second_device(self):
        for name in MODELS:
            model = QuotaCostModel.for_model(name, device="gfx90a")
            self.assertEqual(model.maskable_units, MASKABLE)

    def test_an_unmeasured_device_still_raises(self):
        with self.assertRaises(KeyError):
            QuotaCostModel.for_model("sdxl", device="gfx1100")

    def test_every_measured_quota_reads_back_as_measured(self):
        for name in MODELS:
            model = QuotaCostModel.for_model(name, device="gfx90a")
            for quota in MEASURED_QUOTA_SECONDS_GFX90A[name]:
                self.assertTrue(model.is_measured(quota), (name, quota))

    def test_the_measured_quotas_are_gfx1201_die_fractions(self):
        # 13/104 = 4/32 and so on, so the two architectures are compared at
        # equal shares of their own die rather than at equal unit counts.
        for name in MODELS:
            here = sorted(MEASURED_QUOTA_SECONDS_GFX90A[name])
            there = sorted(MEASURED_QUOTA_SECONDS[name])
            fractions_here = [q / MASKABLE for q in here]
            fractions_there = [q / 32 for q in there]
            for fraction in fractions_here:
                self.assertIn(round(fraction, 4),
                              [round(f, 4) for f in fractions_there])

    def test_the_measured_set_is_closed_under_complement(self):
        # A two-tenant split must have BOTH sides measured, or the policy
        # reads a fit the device contradicts. 13+91, 26+78, 52+52.
        for name in MODELS:
            quotas = set(MEASURED_QUOTA_SECONDS_GFX90A[name])
            partitions = [q for q in quotas if q != MASKABLE]
            for quota in partitions:
                self.assertIn(MASKABLE - quota, quotas, (name, quota))


class AmdahlIsRefutedHereTest(unittest.TestCase):
    def test_the_fitted_serial_term_is_negative_on_gfx90a(self):
        for name in MODELS:
            fit = fit_quota_latency(
                list(MEASURED_QUOTA_SECONDS_GFX90A[name].items()))
            self.assertLess(fit.serial_s, 0.0, name)

    def test_the_fitted_serial_term_is_positive_on_gfx1201(self):
        for name in MODELS:
            fit = fit_quota_latency(list(MEASURED_QUOTA_SECONDS[name].items()))
            self.assertGreater(fit.serial_s, 0.0, name)

    def test_unit_seconds_per_step_rise_with_quota_on_gfx1201(self):
        # Amdahl says q*t(q) = serial*q + parallel, strictly increasing.
        for name in MODELS:
            curve = MEASURED_QUOTA_SECONDS[name]
            work = [q * curve[q] for q in sorted(curve)]
            self.assertEqual(work, sorted(work), name)

    def test_unit_seconds_per_step_do_not_rise_with_quota_on_gfx90a(self):
        # They fall from 13 to 26 units on both models, which no positive
        # serial term can produce. This is the refutation; the negative fit
        # is only its symptom.
        for name in MODELS:
            curve = MEASURED_QUOTA_SECONDS_GFX90A[name]
            work = [q * curve[q] for q in sorted(curve)]
            self.assertNotEqual(work, sorted(work), name)
            self.assertLess(26 * curve[26], 13 * curve[13], name)

    def test_the_recorded_serial_fraction_is_zero_not_a_fit(self):
        for name in MODELS:
            self.assertEqual(MEASURED_MODELS_GFX90A[name]["serial_fraction"],
                             0.0)

    def test_the_leftover_amdahl_error_has_no_consistent_sign(self):
        # The two models disagree about the direction on the same device,
        # because the efficiency optimum sits at a different width for
        # each. So an unmeasured quota here is not conservative either way.
        errors = {}
        for name in MODELS:
            model = QuotaCostModel.for_model(name, device="gfx90a")
            measured = MEASURED_QUOTA_SECONDS_GFX90A[name][13]
            amdahl_at_13 = model.step_seconds_at_full * (MASKABLE / 13)
            errors[name] = amdahl_at_13 - measured
        self.assertGreater(errors["sdxl"], 0.0)          # pessimistic
        self.assertLess(errors["cogvideox-2b"], 0.0)     # optimistic


class PartitioningGainHasAnOptimumHereTest(unittest.TestCase):
    """Aggregate solo throughput against the number of equal ways.

    On gfx1201 it rises monotonically and saturates; on gfx90a it peaks at
    four ways and then collapses below the whole die. That is a statement
    about the hardware, not about any policy, and it is the reason a
    scheduler cannot carry a split across these two devices.
    """

    def aggregate(self, curve, maskable, ways):
        quota = maskable // ways
        return ways * curve[maskable] / curve[quota]

    def test_gfx1201_gain_is_monotone_in_the_number_of_ways(self):
        for name in MODELS:
            curve = MEASURED_QUOTA_SECONDS[name]
            gains = [self.aggregate(curve, 32, n) for n in (1, 2, 4, 8)]
            self.assertEqual(gains, sorted(gains), name)

    def test_gfx90a_gain_peaks_at_four_ways_and_falls(self):
        for name in MODELS:
            curve = MEASURED_QUOTA_SECONDS_GFX90A[name]
            one, two, four, eight = (
                self.aggregate(curve, MASKABLE, n) for n in (1, 2, 4, 8))
            self.assertGreater(four, two, name)
            self.assertGreater(two, one, name)
            self.assertLess(eight, four, name)

    def test_eight_ways_loses_to_the_whole_die_on_cogvideox_gfx90a(self):
        curve = MEASURED_QUOTA_SECONDS_GFX90A["cogvideox-2b"]
        self.assertLess(self.aggregate(curve, MASKABLE, 8), 1.0)


class TheCoRunPenaltyTravelsTest(unittest.TestCase):
    """The externality curve is nearly gfx1201's; the quota curve is not.

    Measured at the same die fractions with the harness that built
    gfx1201's table. The two agree to within 0.03 everywhere, while the
    quota curves disagree in functional form. Whatever produces the
    penalty is therefore not what makes gfx90a's efficiency peak at a
    quarter of the die -- which is why a scheduler carrying a split
    across these devices is wrong about the curve and not about the
    contention.
    """

    FRACTIONS = ((13, 91, 4, 28), (26, 78, 8, 24), (52, 52, 16, 16),
                 (78, 26, 24, 8), (91, 13, 28, 4))

    def test_both_tables_are_monotone_in_own_quota(self):
        for table, maskable in ((MEASURED_EXTERNALITY_GFX90A, 104),
                                (MEASURED_EXTERNALITY, 32)):
            keys = sorted(table, key=lambda k: k[0])
            values = [table[k] for k in keys]
            self.assertEqual(values, sorted(values, reverse=True),
                             str(maskable))

    def test_the_two_devices_agree_within_four_points(self):
        # 0.039 at the widest slice is the largest gap; "within 0.03"
        # was written first and is not true of 26+78 or 91+13.
        for own90, peer90, own12, peer12 in self.FRACTIONS:
            with self.subTest(f"{own90}+{peer90} vs {own12}+{peer12}"):
                self.assertAlmostEqual(
                    MEASURED_EXTERNALITY_GFX90A[(own90, peer90)],
                    MEASURED_EXTERNALITY[(own12, peer12)], delta=0.04)

    def test_gfx90a_is_higher_only_at_the_narrowest_slice(self):
        # The shape of the small disagreement, which is more informative
        # than its size: gfx90a's penalty curve is the steeper one.
        self.assertGreater(MEASURED_EXTERNALITY_GFX90A[(13, 91)],
                           MEASURED_EXTERNALITY[(4, 28)])
        for own90, peer90, own12, peer12 in self.FRACTIONS[1:]:
            self.assertLess(MEASURED_EXTERNALITY_GFX90A[(own90, peer90)],
                            MEASURED_EXTERNALITY[(own12, peer12)],
                            f"{own90}+{peer90}")

    def test_the_fractions_really_are_the_same(self):
        for own90, peer90, own12, peer12 in self.FRACTIONS:
            self.assertAlmostEqual(own90 / 104, own12 / 32, places=6)
            self.assertAlmostEqual(peer90 / 104, peer12 / 32, places=6)

    def test_the_device_argument_is_required_to_reach_the_new_table(self):
        # The default stays gfx1201, so every existing caller is
        # unchanged and the Gate C behavioural digests hold.
        self.assertEqual(externality(16, 16), MEASURED_EXTERNALITY[(16, 16)])
        self.assertEqual(externality(52, 52, device="gfx90a"),
                         MEASURED_EXTERNALITY_GFX90A[(52, 52)])
        with self.assertRaises(Exception):
            externality(52, 52)

    def test_the_model_specific_table_is_not_borrowed_across_devices(self):
        # (16, 16) is CogVideoX-2b's own entry on gfx1201. On gfx90a
        # there is no 16+16 at all, and the model table must not stand in.
        self.assertNotEqual(externality(16, 16, model="cogvideox-2b"),
                            MEASURED_EXTERNALITY[(16, 16)])
        with self.assertRaises(Exception):
            externality(16, 16, model="cogvideox-2b", device="gfx90a")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
