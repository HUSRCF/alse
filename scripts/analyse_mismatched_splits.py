#!/usr/bin/env python3
"""Decide docs/prereg-mismatched-splits.md from the raw runs.

The rules are the pre-registration's, not this file's, and they were
fixed before the first cell ran:

  * a run whose controls do not come back within 10% of ``solo_before``
    is an INSTRUMENT FAILURE and its penalty is not read -- that is the
    rule that disqualified the previous comparator set, and it applies
    to this campaign's own output identically;
  * a solo more than 10% off ``MEASURED_QUOTA_SECONDS`` for its width
    and model is how a mask that failed to apply shows up;
  * the comparator ``same(w)`` is this campaign's same-model run at the
    identical widths, never one read off an older directory;
  * a side beats its rotation share iff ``solo(w) x ext < 2 x solo(32)``.

Nothing here is computed by hand. 1.9's intervals, 3.6's percentages and
all of 3.7 were once ad-hoc scripts outside version control, and
committing them found three defects; a published number that cannot be
regenerated is a number on trust.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from burstserve.trace_sim import (  # noqa: E402
    EXTERNALITY_TABLES_BY_SOURCE,
    MEASURED_QUOTA_SECONDS,
    MEASURED_QUOTA_SECONDS_GFX90A,
)

CONTROL_TOLERANCE = 0.10        # prereg: "within 10% of solo_before"
CURVE_TOLERANCE = 0.10          # prereg: "more than 10% off the curve"
STANDS_BAND = 1.06              # 1.5's own upper bound
MISMATCH_BAR = 2.0              # prereg verdict 2

CURVES = {"gfx1201": MEASURED_QUOTA_SECONDS,
          "gfx90a": MEASURED_QUOTA_SECONDS_GFX90A}


def load(path: Path) -> dict:
    d = json.loads(path.read_text())
    d["_name"] = path.stem
    return d


def by_slice(rows, key="p50_s"):
    return {r["slice"]: r[key] for r in rows or []}


def controls(run: dict) -> tuple[bool, list[str]]:
    """Every control must return to the pre-episode solo. Returns
    (passed, lines) and the lines are printed whether it passed or not."""
    before = by_slice(run["solo_before"])
    checks = (("after", run.get("solo_after")),
              ("emptied", run.get("solo_after_empty_cache")),
              ("dropped", run.get("solo_after_peers_dropped")))
    ok, lines = True, []
    for label, rows in checks:
        if not rows:
            lines.append(f"    {label:8s} not measured")
            continue
        worst, worst_slice = 0.0, None
        for s, v in by_slice(rows).items():
            drift = abs(v - before[s]) / before[s]
            if drift > worst:
                worst, worst_slice = drift, s
        flag = "" if worst <= CONTROL_TOLERANCE else "   <-- FAILS"
        if worst > CONTROL_TOLERANCE:
            ok = False
        lines.append(f"    {label:8s} worst drift {worst * 100:5.1f}% "
                     f"(slice {worst_slice}){flag}")
    return ok, lines


def curve_check(run: dict, device: str) -> list[str]:
    curve = CURVES[device]
    lines = []
    for r in run["solo_before"]:
        model = run["slice_models"][r["slice"]]
        want = curve.get(model, {}).get(r["units"])
        if want is None:
            lines.append(f"    slice {r['slice']} {model} @{r['units']}u "
                         f"{r['p50_s'] * 1000:7.1f} ms  (no curve entry)")
            continue
        off = (r["p50_s"] - want) / want
        flag = "" if abs(off) <= CURVE_TOLERANCE else "   <-- OFF CURVE"
        lines.append(f"    slice {r['slice']} {model:13s} @{r['units']:2d}u "
                     f"{r['p50_s'] * 1000:7.1f} ms vs curve "
                     f"{want * 1000:7.1f}  {off * 100:+5.1f}%{flag}")
    return lines


def last_episode(run: dict) -> dict[int, dict]:
    return {r["slice"]: r for r in run["per_episode"][-1]["rows"]}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("run_dir", type=Path)
    p.add_argument("--device", default="gfx1201")
    p.add_argument("--solo-from", type=Path, action="append", default=[],
                   help="another directory to source whole-die solos "
                        "from. SDXL's on gfx1201 was measured by the "
                        "N-way sweep's 1-way cell on this same "
                        "instrument -- nway_walled/w1.json -- and the "
                        "pre-registration names it rather than leaving "
                        "the rotation column to be filled in later.")
    p.add_argument("--json", type=Path)
    args = p.parse_args()

    runs = {r["_name"]: r for r in (load(f)
                                    for f in sorted(args.run_dir.glob("*.json")))}
    if not runs:
        raise SystemExit(f"no runs under {args.run_dir}")
    die = next(iter(runs.values()))["maskable_units"]
    calls = EXTERNALITY_TABLES_BY_SOURCE["calls"].get(args.device, {})

    # ---------------------------------------------------------- controls
    print("=" * 72)
    print("CONTROLS -- a run that fails here is an instrument failure and")
    print("its penalty is not read (prereg, and it is why the previous")
    print("comparator set was thrown away)")
    print("=" * 72)
    valid = {}
    for name, run in sorted(runs.items()):
        ok, lines = controls(run)
        valid[name] = ok
        print(f"  {name}  {'PASS' if ok else 'FAIL'}")
        for line in lines:
            print(line)
        for line in curve_check(run, args.device):
            print(line)

    # ------------------------------------------------------- solo at 32u
    solo32 = {}
    sources = dict(runs)
    for extra in args.solo_from:
        for f in sorted(extra.glob("*.json")):
            sources[f"{extra.name}/{f.stem}"] = load(f)
    for name, run in sources.items():
        if run["ways"] == 1 and run["slice_widths"] == [die]:
            solo32[run["slice_models"][0]] = run["solo_before"][0]["p50_s"]
    print()
    print("whole-die solo, this instrument:",
          {m: round(v * 1000, 1) for m, v in solo32.items()})
    missing = {m for r in runs.values() for m in r["slice_models"]} - set(solo32)
    if missing:
        print(f"  NO whole-die solo for {sorted(missing)} -- the rotation "
              "column cannot be filled for it; pass --solo-from")

    # ------------------------------------------------------- the splits
    print()
    print("=" * 72)
    print("THE SPLITS.  same(w) is this campaign's same-model run at the")
    print("identical widths.  calls is MEASURED_EXTERNALITY, whole-call.")
    print("=" * 72)
    header = (f"{'split':>7} {'model':>13} {'u':>3} {'solo':>7} {'corun':>8} "
              f"{'ext':>6} {'same(w)':>8} {'ratio':>6} {'calls':>6} "
              f"{'rotation':>9}")
    print(header)

    table, verdict_rows = [], []
    for name, run in sorted(runs.items()):
        if not name.startswith("mm_"):
            continue
        widths = run["slice_widths"]
        tag = "_".join(str(w) for w in widths)
        rev = run.get("reverse_offsets")
        same = runs.get(f"same_sdxl_{tag}")
        rows = last_episode(run)
        for s, r in sorted(rows.items()):
            model = run["slice_models"][s]
            w = r["units"]
            ext = r["externality"]
            # the comparator: the same-model run's slice of the SAME width
            comp = None
            src = f"same_sdxl_{tag}" if model == "sdxl" else "same_cog_16_16"
            cand = runs.get(src)
            if cand:
                for cs, cr in last_episode(cand).items():
                    if cr["units"] == w:
                        comp = cr["externality"]
                        break
            ratio = ext / comp if comp else None
            base = solo32.get(model)
            corun = r["corun_p50_s"]
            beats = (corun < 2 * base) if base else None
            print(f"{tag + ('r' if rev else ''):>7} {model:>13} {w:>3} "
                  f"{r['solo_at_quota_s'] * 1000:7.1f} {corun * 1000:8.1f} "
                  f"{ext:6.3f} "
                  f"{(f'{comp:.3f}' if comp else '--'):>8} "
                  f"{(f'{ratio:.2f}x' if ratio else '--'):>6} "
                  f"{(f'{calls[(w, die - w)]:.3f}' if (w, die - w) in calls else '--'):>6} "
                  f"{('beats' if beats else 'LOSES' if beats is not None else '--'):>9}")
            table.append({"split": tag, "reversed": bool(rev), "model": model,
                          "units": w, "solo_s": r["solo_at_quota_s"],
                          "corun_s": corun, "externality": ext,
                          "same_model_at_width": comp, "ratio": ratio,
                          "call_level": calls.get((w, die - w)),
                          "beats_rotation": beats,
                          "controls_ok": valid[name]})
            if not rev:
                verdict_rows.append(table[-1])

    # ------------------------------------------------------ step rate
    print()
    print("aggregate step rate for the pair, steps/s (1.13's comparison:")
    print("externality ratios at different widths have different baselines)")
    if solo32:
        rot = sum(0.5 / v for v in solo32.values())
        print(f"  rotation, whole die each half the time      {rot:6.3f}")
    for name, run in sorted(runs.items()):
        if not name.startswith("mm_"):
            continue
        rate = sum(1.0 / r["corun_p50_s"] for r in last_episode(run).values())
        print(f"  {name:20s} partitioned                {rate:6.3f}")

    # -------------------------------------------------------- verdict
    print()
    print("=" * 72)
    usable = [r for r in verdict_rows if r["controls_ok"]]
    if len(usable) < len(verdict_rows):
        print(f"NOTE: {len(verdict_rows) - len(usable)} of {len(verdict_rows)}"
              " slice-rows come from runs whose controls failed and are")
        print("excluded from the verdict, per the pre-registration.")
    band = [r for r in usable if r["externality"] > STANDS_BAND]
    breached = [r for r in usable
                if r["model"] == "sdxl" and r["ratio"]
                and r["ratio"] >= MISMATCH_BAR]
    lost = [r for r in usable if r["beats_rotation"] is False]
    if not band and not lost:
        verdict = 1
        text = ("VERDICT 1: 1.5 stands on gfx1201 and 1.12 is a CDNA2 "
                "phenomenon.")
    elif breached:
        verdict = 2
        text = ("VERDICT 2: 1.5 is withdrawn. "
                + ", ".join(f"{r['split']} sdxl@{r['units']}u "
                            f"{r['ratio']:.2f}x its same-model control"
                            for r in breached))
    else:
        verdict = 3
        text = ("VERDICT 3: 1.5's numeric band is withdrawn; the "
                "Pareto-against-rotation claim is judged separately and "
                + ("also fails." if lost else "survives."))
    print(text)
    print(f"  splits outside 1.00-1.06: {len(band)} of {len(usable)} slices")
    print(f"  sides losing to rotation: {len(lost)}")
    print("=" * 72)

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(
            {"device": args.device, "verdict": verdict, "text": text,
             "controls": valid, "solo_whole_die_s": solo32,
             "rows": table}, indent=1) + "\n")
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
