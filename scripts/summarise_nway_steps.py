#!/usr/bin/env python3
"""Read the step-level N-way runs and print 1.11's table from them.

Every number in the claim comes out of here rather than out of a log,
because a published number that cannot be regenerated is a number on
trust. Prints the transient as well as the steady state: the finding of
2026-09-04 is that the first episodes of a co-run can carry device
drains, so a summariser that shows only the verdict hides the thing that
went wrong last time.

The pairwise column is the same slice width with **one** peer filling the
rest of the die, which is the comparison the claim is about.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from burstserve.trace_sim import EXTERNALITY_TABLES  # noqa: E402


def pairwise(units: int, die: int, device: str):
    table = EXTERNALITY_TABLES.get(device)
    if not table:
        return None
    return table.get((units, die - units))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--device", default="gfx90a")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()

    runs = sorted(args.run_dir.glob("*.json"),
                  key=lambda p: json.loads(p.read_text())["ways"])
    if not runs:
        raise SystemExit(f"no runs under {args.run_dir}")

    rows = []
    print(f"{'ways':>5} {'slice':>6} {'solo ms':>9} {'steady':>8} "
          f"{'ep1':>7} {'pairwise':>9} {'vs pair':>9}  per-slice spread")
    for path in runs:
        d = json.loads(path.read_text())
        ways, die = d["ways"], d["maskable_units"]
        widths = d.get("slice_widths") or [d["slice_units"]] * ways
        per_slice = [row["externality"] for row in d["verdict"]]
        steady = statistics.mean(per_slice)
        first = statistics.mean(r["externality"]
                                for r in d["per_episode"][0]["rows"])
        solo = statistics.mean(s["p50_s"] for s in d["solo_before"]) * 1000
        # Each slice against the pairwise entry for its OWN width. For an
        # equal N-way split those are all the same entry; for a measured
        # pair they are the two entries the table actually holds.
        pairs = [pairwise(w, die, args.device) for w in widths]
        vs = [(e / q - 1) * 100 if q else None
              for e, q in zip(per_slice, pairs)]
        shown = statistics.mean(v for v in vs if v is not None) \
            if any(v is not None for v in vs) else None
        label = (f"{widths[0]}" if len(set(widths)) == 1
                 else "+".join(str(w) for w in widths))
        rows.append({"ways": ways, "slice_widths": widths,
                     "reverse_offsets": d.get("reverse_offsets", False),
                     "steady_mean": steady, "episode_1_mean": first,
                     "solo_ms": solo, "pairwise": pairs,
                     "vs_pairwise_pct": vs,
                     "per_slice": per_slice,
                     "run": path.name})
        print(f"{ways:>5} {label:>6} {solo:>9.1f} {steady:>8.4f} "
              f"{first:>7.3f} "
              f"{(f'{pairs[0]:.4f}' if pairs[0] else '--'):>9} "
              f"{(f'{shown:+.1f}%' if shown is not None else '--'):>9}  "
              + " ".join(f"{v:.3f}" for v in per_slice))

    print("\nsteady = the last episode, which is the verdict 1.5 publishes.")
    print("ep1    = the first co-run episode, kept because a transient here")
    print("         is what turned out to be measuring device drains.")
    if args.json:
        args.json.write_text(json.dumps(rows, indent=1) + "\n")
        print(f"-> {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
