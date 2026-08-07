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
    MEASURED_EXTERNALITY_BY_MODEL,
    MEASURED_MODELS,
    MEASURED_QUOTA_SECONDS,
    MEASURED_WORKPOINT,
    externality,
)

REPO = pathlib.Path(__file__).resolve().parent.parent
PROBES = REPO / "experiments" / "probes" / "amd-r9700-cu-mask"
SDXL_CURVE = PROBES / "step_ratio_sdxl_768_perstep_curve_20260806.json"
COGVIDEOX_CURVE = (
    PROBES / "step_ratio_cogvideox2b_perstep_curve_20260806.json"
)
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


STAGGERED = sorted(PROBES.glob("sdxl_768_st*_*_20260806.json"))


def _staggered_sides(low: bool) -> list[float]:
    """Externality sides from co-runs inside or outside the 2-5 s window."""
    out = []
    for path in STAGGERED:
        payload = json.loads(path.read_text())
        overlap = payload.get("overlap", {})
        if "per_step_externality" not in overlap.get("a", {}):
            continue
        sides = [overlap["a"]["per_step_externality"],
                 overlap["b"]["per_step_externality"]]
        is_low = sum(sides) / len(sides) < 0.4
        if is_low == low:
            out.extend(sides)
    return out


class ExternalityMatchesItsProbe(unittest.TestCase):
    def test_the_entry_is_the_mean_of_the_staggered_runs(self):
        """Not a single co-run: the measurement is bimodal.

        Nine co-runs launched 2-5 s apart give one state, eleven launched
        together or 8+ s apart give another, and the two do not overlap.
        A table entry taken from one run records which state that run
        happened to land in.
        """
        sides = _staggered_sides(low=True)
        if len(sides) < 6:
            self.skipTest("staggered probes not present")
        measured = 1.0 + sum(sides) / len(sides)
        self.assertAlmostEqual(MEASURED_EXTERNALITY[(16, 16)], measured,
                               places=3)

    def test_the_two_states_do_not_overlap(self):
        """If they ever did, the bimodal reading would be wrong."""
        low = _staggered_sides(low=True)
        high = _staggered_sides(low=False)
        if not low or not high:
            self.skipTest("staggered probes not present")
        self.assertLess(max(low), min(high))

    def test_the_entry_is_the_low_state_and_not_a_blend(self):
        """A mean over both states would satisfy neither and look sober."""
        low = _staggered_sides(low=True)
        high = _staggered_sides(low=False)
        if not low or not high:
            self.skipTest("staggered probes not present")
        blended = 1.0 + (sum(low) + sum(high)) / (len(low) + len(high))
        self.assertLess(MEASURED_EXTERNALITY[(16, 16)], blended)

    def test_the_split_the_scheduler_reads_came_from_the_corun(self):
        if not SDXL_CORUN.exists():
            self.skipTest(f"probe not present: {SDXL_CORUN.name}")
        payload = json.loads(SDXL_CORUN.read_text())
        overlap = payload["overlap"]
        sides = [overlap["a"]["per_step_externality"],
                 overlap["b"]["per_step_externality"]]
        # The original unstaggered co-run happened to land low; it agrees
        # with the staggered mean to under 1%, which is why the entry did
        # not move when the bistability was found.
        self.assertAlmostEqual(1.0 + sum(sides) / len(sides),
                               MEASURED_EXTERNALITY[(16, 16)], places=2)

    def test_the_corun_actually_overlapped(self):
        """A pair that never ran together shows zero externality honestly.

        Without this, a co-run whose processes took turns would supply a
        penalty of ~1.0 and look like a measurement.
        """
        if not SDXL_CORUN.exists():
            self.skipTest(f"probe not present: {SDXL_CORUN.name}")
        payload = json.loads(SDXL_CORUN.read_text())
        self.assertTrue(payload["overlap"]["sufficient_overlap"])


class CogVideoXCurveMatchesItsProbe(unittest.TestCase):
    def setUp(self):
        if not COGVIDEOX_CURVE.exists():
            self.skipTest(f"probe not present: {COGVIDEOX_CURVE.name}")
        payload = json.loads(COGVIDEOX_CURVE.read_text())
        curves = payload["per_step_quota_curve"]
        self.curve = {int(u): float(s)
                      for u, s in next(iter(curves.values())).items()}

    def test_every_quota_in_the_table_came_from_this_run(self):
        table = MEASURED_QUOTA_SECONDS["cogvideox-2b"]
        self.assertEqual(sorted(table), sorted(self.curve))
        for units, seconds in sorted(table.items()):
            with self.subTest(units=units):
                self.assertAlmostEqual(seconds, self.curve[units], places=5)

    def test_the_full_die_constant_is_the_full_die_measurement(self):
        self.assertAlmostEqual(
            MEASURED_MODELS["cogvideox-2b"]["step_seconds_at_full"],
            self.curve[32], places=5,
        )

    def test_the_curve_is_monotone_in_quota(self):
        ordered = [self.curve[u] for u in sorted(self.curve)]
        for faster, slower in zip(ordered, ordered[1:]):
            self.assertLess(slower, faster)

    def test_the_cells_record_frames_and_no_resolution(self):
        """CogVideoX ignores height/width, and its cells say so.

        The curve's top-level key reads "768" because this run predates
        the driver fix that labels video curves by frame count; the cells
        underneath are what carry the truth, and they record height and
        width as null. Checking the cells rather than the key tests the
        measurement instead of the label that was written over it.
        """
        payload = json.loads(COGVIDEOX_CURVE.read_text())
        cells = [c for c in payload["cells"] if c.get("status") == "ok"]
        self.assertTrue(cells)
        for cell in cells:
            with self.subTest(units=cell.get("requested_units")):
                self.assertIsNone(cell.get("height"))
                self.assertIsNone(cell.get("width"))
                self.assertEqual(cell.get("frames"), 9)
                self.assertTrue(cell.get("vae_tiling"))


class TablesDeclareWhatTheyAre(unittest.TestCase):
    def test_both_models_are_now_the_same_quantity(self):
        """Mixed units are acceptable; undocumented mixed units are not.

        Until 2026-08-06 SDXL's curve was per-step and CogVideoX's was
        call p50s, and nothing in the source or the tests said so. Both
        are per-step now, and this asserts the source still declares what
        they are rather than leaving a reader to assume.
        """
        source = (REPO / "src" / "burstserve" / "trace_sim.py").read_text()
        self.assertIn("per denoising step", source)
        self.assertNotIn("# call p50", source)

    def test_the_two_models_stay_far_apart_in_cost(self):
        """CogVideoX's step is roughly 4.5x SDXL's, both per-step.

        The ratio was ~64x while the tables held different quantities. If
        it drifts back toward that, one table has been rebuilt without the
        other.
        """
        sdxl = MEASURED_QUOTA_SECONDS["sdxl"][32]
        cogvideox = MEASURED_QUOTA_SECONDS["cogvideox-2b"][32]
        self.assertGreater(cogvideox / sdxl, 3.0)
        self.assertLess(cogvideox / sdxl, 8.0)


class WorkpointIsDeclared(unittest.TestCase):
    """The tables hold one workpoint per model, and must say which.

    Co-runs on 2026-08-06 measured the per-step penalty at +22.4%/+24.5%
    for SDXL at 768 and +76.6%/+78.9% at 832, with the step-time ratio
    held at 1.000 -- and the partitioning result flips sign across that
    step, from +16.6% to -21.9%. The simulator has no workpoint
    dimension, so the only defence against a result being carried
    somewhere it does not hold is saying plainly where it was measured.
    """

    def test_every_model_declares_its_workpoint(self):
        self.assertEqual(sorted(MEASURED_WORKPOINT),
                         sorted(MEASURED_QUOTA_SECONDS))
        for model, workpoint in MEASURED_WORKPOINT.items():
            with self.subTest(model=model):
                self.assertTrue(workpoint.strip())

    def test_the_declared_workpoint_matches_the_probe(self):
        """SDXL's curve came from a 768 run; the declaration has to agree."""
        if not SDXL_CURVE.exists():
            self.skipTest(f"probe not present: {SDXL_CURVE.name}")
        payload = json.loads(SDXL_CURVE.read_text())
        cells = [c for c in payload["cells"] if c.get("status") == "ok"]
        self.assertTrue(cells)
        for cell in cells:
            with self.subTest(units=cell.get("requested_units")):
                self.assertEqual(cell["height"], 768)
                self.assertEqual(cell["width"], 768)
        self.assertEqual(MEASURED_WORKPOINT["sdxl"], "768x768")

    def test_the_externality_probe_is_at_the_same_workpoint(self):
        """A penalty from one resolution applied to costs from another is
        the defect this whole section exists to prevent."""
        if not SDXL_CORUN.exists():
            self.skipTest(f"probe not present: {SDXL_CORUN.name}")
        payload = json.loads(SDXL_CORUN.read_text())
        for key in ("a", "b"):
            with self.subTest(side=key):
                self.assertEqual(payload["sides"][key]["height"], 768)
                self.assertEqual(payload["sides"][key]["width"], 768)


class PerModelExternalityMatchesItsProbe(unittest.TestCase):
    """CogVideoX-2b's own co-run, which Gate B recorded as impossible.

    Two processes need 57 GB on a 34.2 GB card. One process holding one
    copy of the weights for two masked streams needs 14.7 GB, so the
    measurement Gate B could not take is available in the arrangement the
    runtime specifies.
    """

    CVX = (PROBES / "inproc_cogvideox2b_9f_shared_20260806.json")

    def test_the_entry_is_the_mean_of_its_probe(self):
        if not self.CVX.exists():
            self.skipTest(f"probe not present: {self.CVX.name}")
        payload = json.loads(self.CVX.read_text())
        sides = [t[s]["externality"] for t in payload["trials"]
                 for s in ("left", "right") if "externality" in t[s]]
        self.assertGreaterEqual(len(sides), 4)
        measured = 1.0 + sum(sides) / len(sides)
        self.assertAlmostEqual(
            MEASURED_EXTERNALITY_BY_MODEL["cogvideox-2b"][(16, 16)],
            measured, places=3,
        )

    def test_the_probe_shared_one_copy_of_the_weights(self):
        """Which is the whole reason it fits, and is the runtime's own
        arrangement rather than a measurement trick."""
        if not self.CVX.exists():
            self.skipTest(f"probe not present: {self.CVX.name}")
        payload = json.loads(self.CVX.read_text())
        self.assertTrue(payload["shared_weights"])

    def test_the_penalty_is_model_dependent(self):
        """If it were not, one table would do and the override would be
        dead weight."""
        shared = MEASURED_EXTERNALITY[(16, 16)]
        own = MEASURED_EXTERNALITY_BY_MODEL["cogvideox-2b"][(16, 16)]
        self.assertGreater(abs(own / shared - 1.0), 0.02)

    def test_a_model_with_its_own_measurement_uses_it(self):
        self.assertAlmostEqual(
            externality(16, 16, "cogvideox-2b"),
            MEASURED_EXTERNALITY_BY_MODEL["cogvideox-2b"][(16, 16)],
        )

    def test_a_model_without_one_falls_back(self):
        self.assertAlmostEqual(externality(16, 16, "sdxl"),
                               MEASURED_EXTERNALITY[(16, 16)])
        self.assertAlmostEqual(externality(16, 16, None),
                               MEASURED_EXTERNALITY[(16, 16)])

    def test_an_unmeasured_split_falls_back_rather_than_inventing(self):
        """CogVideoX has one measured split; the rest are the shared
        table, which is an extrapolation across models."""
        self.assertAlmostEqual(externality(8, 24, "cogvideox-2b"),
                               MEASURED_EXTERNALITY[(8, 24)])


if __name__ == "__main__":
    unittest.main()
