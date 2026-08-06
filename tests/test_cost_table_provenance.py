"""The cost tables must equal the measurements they claim to come from.

The 2026-08-06 correction happened because nothing connected
MEASURED_QUOTA_SECONDS to a file on disk. Its full-die constant was a
half-die measurement, its comment named a resolution the model was never
profiled at, and every quota was 32-50% off -- none of which any test
could see, because the table was only ever compared against itself.

A comment cannot be checked. A probe file can. These tests bind each
table entry to the run that produced it, so a value that drifts from its
evidence fails here rather than surviving into a gate.
"""

from __future__ import annotations

import json
import pathlib
import sys
import unittest

sys.dont_write_bytecode = True

from burstserve.trace_sim import (
    MEASURED_EXTERNALITY,
    MEASURED_MODELS,
    MEASURED_QUOTA_SECONDS,
)

REPO = pathlib.Path(__file__).resolve().parent.parent
PROBES = REPO / "experiments" / "probes" / "amd-r9700-cu-mask"
SDXL_CURVE = PROBES / "step_ratio_sdxl_768_perstep_curve_20260806.json"
SDXL_CORUN = PROBES / "corun_sdxl_768_perstep_16_16_20260806.json"


class SdxlCurveMatchesItsProbe(unittest.TestCase):
    def setUp(self):
        if not SDXL_CURVE.exists():
            self.skipTest(f"probe not present: {SDXL_CURVE.name}")
        payload = json.loads(SDXL_CURVE.read_text())
        curves = payload["per_step_quota_curve"]
        self.curve = {int(u): float(s)
                      for u, s in next(iter(curves.values())).items()}

    def test_every_quota_in_the_table_came_from_this_run(self):
        table = MEASURED_QUOTA_SECONDS["sdxl"]
        self.assertEqual(sorted(table), sorted(self.curve))
        for units, seconds in sorted(table.items()):
            with self.subTest(units=units):
                # Five decimals: the table is rounded for legibility, and
                # anything looser would let a real edit pass.
                self.assertAlmostEqual(seconds, self.curve[units], places=5)

    def test_the_full_die_constant_is_the_full_die_measurement(self):
        """The exact mistake that was shipped.

        step_seconds_at_full held 0.1521, which is this model at *16*
        units. Checking it against the 32-unit entry of the probe is what
        would have caught that.
        """
        self.assertAlmostEqual(
            MEASURED_MODELS["sdxl"]["step_seconds_at_full"],
            self.curve[32], places=5,
        )

    def test_it_is_not_any_other_quota_in_the_curve(self):
        """Guards the specific confusion rather than only its instance."""
        full = MEASURED_MODELS["sdxl"]["step_seconds_at_full"]
        for units, seconds in self.curve.items():
            if units == 32:
                continue
            with self.subTest(units=units):
                self.assertNotAlmostEqual(full, seconds, places=4)

    def test_the_curve_is_monotone_in_quota(self):
        """More die is never slower.

        A table assembled from mismatched sources can violate this without
        any single entry looking wrong.
        """
        ordered = [self.curve[u] for u in sorted(self.curve)]
        for faster, slower in zip(ordered, ordered[1:]):
            self.assertLess(slower, faster)


class ExternalityMatchesItsProbe(unittest.TestCase):
    def test_the_split_the_scheduler_reads_came_from_the_corun(self):
        if not SDXL_CORUN.exists():
            self.skipTest(f"probe not present: {SDXL_CORUN.name}")
        payload = json.loads(SDXL_CORUN.read_text())
        overlap = payload["overlap"]
        sides = [overlap["a"]["per_step_externality"],
                 overlap["b"]["per_step_externality"]]
        measured = 1.0 + sum(sides) / len(sides)
        self.assertAlmostEqual(MEASURED_EXTERNALITY[(16, 16)], measured,
                               places=3)

    def test_the_corun_actually_overlapped(self):
        """A pair that never ran together shows zero externality honestly.

        Without this, a co-run whose processes took turns would supply a
        penalty of ~1.0 and look like a measurement.
        """
        if not SDXL_CORUN.exists():
            self.skipTest(f"probe not present: {SDXL_CORUN.name}")
        payload = json.loads(SDXL_CORUN.read_text())
        self.assertTrue(payload["overlap"]["sufficient_overlap"])


class TablesDeclareWhatTheyAre(unittest.TestCase):
    def test_cogvideox_is_still_call_level_and_says_so(self):
        """Mixed units are acceptable; undocumented mixed units are not.

        CogVideoX's curve is call p50s while SDXL's is per-step. That is a
        deliberate limit -- its per-step data has three points -- and the
        source has to say so, because the two are not interchangeable.
        """
        source = (REPO / "src" / "burstserve" / "trace_sim.py").read_text()
        self.assertIn("call p50", source)
        self.assertIn("per denoising step", source)

    def test_the_two_models_are_not_silently_the_same_scale(self):
        """SDXL is seconds-per-step, CogVideoX is seconds-per-call.

        They differ by about two orders of magnitude, which is the visible
        symptom of the documented difference. If they ever converge,
        someone has changed one without the other.
        """
        sdxl = MEASURED_QUOTA_SECONDS["sdxl"][32]
        cogvideox = MEASURED_QUOTA_SECONDS["cogvideox-2b"][32]
        self.assertGreater(cogvideox / sdxl, 10)


if __name__ == "__main__":
    unittest.main()
