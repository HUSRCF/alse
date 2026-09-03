#!/usr/bin/env python3
"""What the N-way penalty is, and what it does to the burst arithmetic.

Every statement this project has made about intra-tenant concurrency used
1.3's **pair** at 16+16 in place of an N-way penalty --
`prereg-intra-tenant.md` says so, and it is the single number its
prediction turns on. This reads the measured ones and recomputes the
burst with them, beside the stand-in, so the difference between the two
is visible rather than assumed.

One way is a solo and must come back at 1.000. If it does not, nothing
below means anything and the run says so rather than being averaged in.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path

sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from burstserve.trace_sim import (  # noqa: E402
    QuotaCostModel,
    UnmeasuredPairing,
    externality,
)

# The pairwise stand-in each device's arithmetic has been using.
PAIRWISE = {"gfx1201": 1.297, "gfx90a": 1.2336}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("directory", type=Path)
    ap.add_argument("--device", default="gfx90a")
    ap.add_argument("--model", default="sdxl")
    ap.add_argument("--steps", type=int, default=8)
    ap.add_argument("--burst", type=int, default=8,
                    help="requests in the burst. Eight, because a burst "
                         "of four cannot use eight slices: the policy "
                         "takes critical[:concurrency] and divides among "
                         "what it took")
    args = ap.parse_args()

    rows = {}
    for path in sorted(args.directory.glob("*.json")):
        payload = json.loads(path.read_text())
        if payload.get("schema_version") != "burstserve.amd-nway-corun/v1":
            continue
        scored = [t["externality_mean"] for t in payload["trials"]
                  if t["externality_mean"] is not None]
        if not scored:
            print(f"  no scored trial in {path.name}")
            continue
        rows[payload["ways"]] = {
            "slice": payload["trials"][0]["slice_units"],
            "idle": payload["trials"][0]["idle_units"],
            "mean": statistics.mean(scored),
            "sd": statistics.stdev(scored) if len(scored) > 1 else 0.0,
            "min": min(t["externality_min"] for t in payload["trials"]
                       if t["externality_min"] is not None),
            "max": max(t["externality_max"] for t in payload["trials"]
                       if t["externality_max"] is not None),
            "solo_s": statistics.mean(
                statistics.mean(t["solo_p50_s"]) for t in payload["trials"]),
            "trials": len(scored),
        }
    if not rows:
        print(f"no N-way payloads in {args.directory}")
        return 1

    control = rows.get(1)
    if control is None:
        print("  WARNING: no one-way control in this directory")
    elif abs(control["mean"] - 1.0) > 0.01:
        print(f"  REFUSED: the one-way control reads "
              f"{control['mean']:.4f}, not 1.000. Nothing below is "
              f"interpretable until that is explained.")
        return 1

    model = QuotaCostModel.for_model(args.model, device=args.device)
    stand_in = PAIRWISE[args.device]
    print(f"{args.device} {args.model}, burst {args.burst} x {args.steps} "
          f"steps on the whole die")
    print(f"  {'ways':>4} {'slice':>6} {'idle':>4}  {'penalty':>8} "
          f"{'sd':>7}  {'measured':>9} {'stand-in':>9}   burst, seconds")
    for ways in sorted(rows):
        row = rows[ways]
        active = max(1, min(ways, args.burst))
        width = model.maskable_units // active
        step = model.step_seconds(width)
        batches = math.ceil(args.burst / active)
        measured = batches * args.steps * step * (
            row["mean"] if active > 1 else 1.0)
        assumed = batches * args.steps * step * (
            stand_in if active > 1 else 1.0)
        mark = "" if model.is_measured(width) else "  * fitted width"
        print(f"  {ways:4d} {row['slice']:6d} {row['idle']:4d}  "
              f"{row['mean']:8.4f} {row['sd']:7.4f}  "
              f"{measured:9.2f} {assumed:9.2f}{mark}")

    # The comparison the pairwise table cannot make. At N ways a slice of
    # width w has N-1 peers occupying maskable-w units; the pairwise
    # entry (w, maskable-w) has ONE peer occupying exactly the same
    # units. Same slice, same busy fraction, different number of
    # independent contexts. If the two agree, the penalty is a function
    # of how much of the die is busy; if they diverge, it is a function
    # of how many things are running, and a pairwise table cannot express
    # it at all.
    print("\n  same slice, same busy die, N-1 peers against one:")
    print(f"    {'ways':>4} {'slice':>6}  {'N-way':>7} {'pairwise':>8}"
          f" {'ratio':>7}")
    for ways in sorted(rows):
        if ways < 2:
            continue
        width = rows[ways]["slice"]
        try:
            pair = externality(width, model.maskable_units - width,
                               device=args.device)
        except (UnmeasuredPairing, KeyError):
            print(f"    {ways:4d} {width:6d}  {rows[ways]['mean']:7.4f} "
                  f"{'--':>8}   no measured pairing at this width")
            continue
        print(f"    {ways:4d} {width:6d}  {rows[ways]['mean']:7.4f} "
              f"{pair:8.4f} {rows[ways]['mean'] / pair:7.3f}")

    best = min(rows, key=lambda w: (
        math.ceil(args.burst / max(1, min(w, args.burst))) * args.steps
        * model.step_seconds(model.maskable_units
                             // max(1, min(w, args.burst)))
        * (rows[w]["mean"] if min(w, args.burst) > 1 else 1.0)))
    print(f"\n  best measured concurrency: {best} ways")
    print("  solo per-call p50 by slice width, a cross-check on the curve:")
    for ways in sorted(rows):
        row = rows[ways]
        per_step = row["solo_s"] / args.steps
        curve = model.step_seconds(row["slice"])
        print(f"    {row['slice']:4d}u  {per_step * 1000:8.2f} ms/step  "
              f"curve {curve * 1000:8.2f} ms  "
              f"{100 * (per_step - curve) / curve:+6.2f}%")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
