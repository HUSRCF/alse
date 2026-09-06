"""1.14 pinned to its raw runs, and 3.11's withdrawal with it.

Every number in claim 1.14 is read here out of
``experiments/probes/gfx1201/pairs_fixed/``. A published number that
cannot be regenerated is a number on trust -- committing the campaign
analyses on 2026-09-04 regenerated three published intervals and found
three defects doing it.

The controls come first on purpose. 1.5 was withdrawn because its
instrument was stale, and the rule that caught it applies to its
replacement identically: a run whose post-episode solo does not come
back is an instrument failure, not a penalty.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RUNS = REPO / "experiments" / "probes" / "gfx1201" / "pairs_fixed"
SPLITS = ("4_28", "8_24", "16_16", "24_8", "28_4")


def load(name: str) -> dict:
    return json.loads((RUNS / f"{name}.json").read_text())


def last(run: dict) -> dict[str, dict]:
    return {r["model"]: r for r in run["per_episode"][-1]["rows"]}


def at_width(run: dict, units: int) -> dict:
    """The last episode's row for the slice of a given width.

    Same-model runs have two slices with the SAME model name, so keying
    the rows by model keeps only the second one. The comparator has to be
    the slice whose WIDTH matches the mismatched run's image tenant --
    that is what makes the ratio a comparison rather than two numbers.
    """
    for r in run["per_episode"][-1]["rows"]:
        if r["units"] == units:
            return r
    raise KeyError(units)


def solo_of(run: dict, model: str) -> float:
    for r in run["solo_before"]:
        if r["model"] == model:
            return r["p50_s"]
    raise KeyError(model)


class ControlsTest(unittest.TestCase):
    """The rule that disqualified the old comparator set, applied here."""

    def test_every_run_returns_to_solo(self):
        for path in sorted(RUNS.glob("*.json")):
            run = json.loads(path.read_text())
            before = {r["slice"]: r["p50_s"] for r in run["solo_before"]}
            for key in ("solo_after", "solo_after_empty_cache",
                        "solo_after_peers_dropped"):
                for row in run.get(key) or []:
                    if "p50_s" not in row:
                        continue
                    drift = abs(row["p50_s"] - before[row["slice"]])
                    self.assertLess(
                        drift / before[row["slice"]], 0.10,
                        f"{path.name} {key} slice {row['slice']} did not "
                        "return to solo; the penalty must not be read")

    def test_thirteen_runs_and_six_episodes_each(self):
        runs = sorted(RUNS.glob("*.json"))
        self.assertEqual(len(runs), 13)
        for path in runs:
            self.assertEqual(len(json.loads(path.read_text())["per_episode"]),
                             6, path.name)

    def test_masks_were_read_back_and_disjoint(self):
        for path in sorted(RUNS.glob("*.json")):
            run = json.loads(path.read_text())
            for entry in run["stream_attestation"]:
                self.assertTrue(entry["matches_request"], path.name)


class TheImageTenantPaysTheVideoStepTest(unittest.TestCase):
    """1.14's table, and the law that makes it a claim rather than five
    numbers."""

    EXPECTED = {          # split: (sdxl externality, cogvideox externality)
        "4_28": (1.689, 1.064),
        "8_24": (2.975, 1.057),
        "16_16": (5.987, 1.050),
        "24_8": (13.386, 1.025),
        "28_4": (26.137, 1.006),
    }

    def test_the_five_splits(self):
        for split, (sdxl, cog) in self.EXPECTED.items():
            rows = last(load(f"mm_{split}"))
            self.assertAlmostEqual(rows["sdxl"]["externality"], sdxl,
                                   places=2, msg=split)
            self.assertAlmostEqual(rows["cogvideox-2b"]["externality"], cog,
                                   places=2, msg=split)

    def test_more_die_makes_the_image_tenant_worse(self):
        """4 units to 28: solo 4.1x better, co-run 3.8x worse. This is
        the claim's point -- widening the slice cannot help, because the
        term that dominates is the peer's step."""
        narrow = last(load("mm_4_28"))["sdxl"]
        wide = last(load("mm_28_4"))["sdxl"]
        self.assertAlmostEqual(narrow["solo_at_quota_s"]
                               / wide["solo_at_quota_s"], 4.1, places=1)
        self.assertAlmostEqual(wide["corun_p50_s"] / narrow["corun_p50_s"],
                               3.8, places=1)

    def test_the_pacing_law(self):
        """sdxl_corun = cog_corun + k * sdxl_solo, k within 0.53-0.56
        across all six mismatched runs including the reversed one."""
        ks = []
        for name in [f"mm_{s}" for s in SPLITS] + ["mm_16_16_rev"]:
            rows = last(load(name))
            sdxl, cog = rows["sdxl"], rows["cogvideox-2b"]
            k = ((sdxl["corun_p50_s"] - cog["corun_p50_s"])
                 / sdxl["solo_at_quota_s"])
            ks.append(k)
            self.assertGreater(k, 0.52, name)
            self.assertLess(k, 0.57, name)
        self.assertLess((max(ks) - min(ks)) / (sum(ks) / len(ks)), 0.06)

    def test_it_follows_the_model_not_the_die_position(self):
        forward = last(load("mm_16_16"))
        reverse = last(load("mm_16_16_rev"))
        self.assertLess(abs(forward["sdxl"]["externality"]
                            - reverse["sdxl"]["externality"]), 0.1)

    def test_the_video_tenant_prefers_a_mismatched_peer(self):
        """1.050 beside SDXL against 1.276 beside another CogVideoX, at
        the same width. The cost is one-directional."""
        mismatched = last(load("mm_16_16"))["cogvideox-2b"]["externality"]
        same = load("same_cog_16_16")["externality_mean_last_episode"]
        self.assertAlmostEqual(mismatched, 1.050, places=2)
        self.assertAlmostEqual(same, 1.286, places=2)
        self.assertLess(mismatched / same, 0.85)


class ItLosesToRotationTest(unittest.TestCase):
    """1.5 said partitioning Pareto-dominates rotation. It is the other
    way round on every split."""

    def solo32(self) -> dict[str, float]:
        cog = solo_of(load("solo_cog_32"), "cogvideox-2b")
        walled = json.loads((REPO / "experiments" / "probes" / "gfx1201"
                             / "nway_walled" / "w1.json").read_text())
        return {"cogvideox-2b": cog,
                "sdxl": walled["solo_before"][0]["p50_s"]}

    def test_the_image_tenant_loses_at_every_split(self):
        base = self.solo32()["sdxl"]
        for split in SPLITS:
            corun = last(load(f"mm_{split}"))["sdxl"]["corun_p50_s"]
            self.assertGreater(corun, 2 * base,
                               f"{split}: the image tenant beat rotation")

    def test_the_video_tenant_loses_only_when_its_own_slice_is_narrow(self):
        base = self.solo32()["cogvideox-2b"]
        beats = {s for s in SPLITS
                 if last(load(f"mm_{s}"))["cogvideox-2b"]["corun_p50_s"]
                 < 2 * base}
        self.assertEqual(beats, {"4_28", "8_24", "16_16"})

    def test_aggregate_step_rate(self):
        solo = self.solo32()
        rotation = sum(0.5 / v for v in solo.values())
        self.assertAlmostEqual(rotation, 5.608, places=2)
        rates = {s: sum(1.0 / r["corun_p50_s"]
                        for r in last(load(f"mm_{s}")).values())
                 for s in SPLITS}
        self.assertAlmostEqual(rates["4_28"], 2.889, places=2)
        self.assertAlmostEqual(rates["28_4"], 0.629, places=2)
        # best split 1.94x worse, worst 8.9x worse
        self.assertAlmostEqual(rotation / max(rates.values()), 1.94, places=1)
        self.assertAlmostEqual(rotation / min(rates.values()), 8.9, places=1)


class TheComparatorWasMeasuredHereTest(unittest.TestCase):
    """The five same-model pairs on disk before this campaign are from
    the pre-fix instrument and their control fails. The test asserts BOTH
    halves: that the old ones fail, and that this campaign's own pass."""

    OLD = REPO / "experiments" / "probes" / "gfx1201" / "nway_steps"

    def test_the_old_comparator_set_fails_its_control(self):
        run = json.loads((self.OLD / "nway_steps_sdxl_32u_16_16.json")
                         .read_text())
        before = run["solo_before"][0]["p50_s"]
        after = run["solo_after"][0]["p50_s"]
        self.assertGreater(after / before, 1.25,
                           "the pre-fix run's control was expected to fail")

    def test_this_campaigns_comparator_holds(self):
        expected = {"4_28": 1.139, "8_24": 1.235, "16_16": 1.324,
                    "24_8": 1.202, "28_4": 1.115}
        for split, value in expected.items():
            width = last(load(f"mm_{split}"))["sdxl"]["units"]
            row = at_width(load(f"same_sdxl_{split}"), width)
            self.assertAlmostEqual(row["externality"], value, places=2,
                                   msg=split)

    def test_the_ratio_that_triggers_verdict_two(self):
        """The pre-registered bar was mm(w) >= 2 x same(w). Four of five
        splits clear it and the widest clears it 23-fold."""
        ratios = {}
        for split in SPLITS:
            image = last(load(f"mm_{split}"))["sdxl"]
            same = at_width(load(f"same_sdxl_{split}"), image["units"])
            ratios[split] = image["externality"] / same["externality"]
        self.assertLess(ratios["4_28"], 2.0)
        for split in ("8_24", "16_16", "24_8", "28_4"):
            self.assertGreaterEqual(ratios[split], 2.0, split)
        self.assertAlmostEqual(ratios["28_4"], 23.4, places=0)


if __name__ == "__main__":
    unittest.main()
