#!/usr/bin/env python3
"""Read the step-level N-way runs and print 1.11's table from them.

**Read `ratio` only from runs timed by the wall clock.** The `after` and
`emptied` columns are the controls that decide whether a ratio means
anything: `after` is a solo re-measured once the episodes are over with
every peer idle, and `emptied` is the same solo after
`torch.cuda.empty_cache()`. Both must come back to the pre-episode solo.
On 2026-09-04 they appeared not to, and 1.11 was withdrawn for a day on
the strength of it; the elevation was a stale `last_step_seconds` being
repeated, not the allocator. See 3.10, which is the withdrawn entry now.

Runs written before the timing fix have an `event_p50_s` that can differ
from the wall figure by 2x and should not be read.
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

from burstserve.trace_sim import (  # noqa: E402
    EXTERNALITY_TABLES_BY_SOURCE,
)


def pairwise(units: int, die: int, device: str, source: str):
    table = EXTERNALITY_TABLES_BY_SOURCE.get(source, {}).get(device)
    if not table:
        return None
    return table.get((units, die - units))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--device", default="gfx90a")
    parser.add_argument("--pairwise-source", default="calls",
                        choices=("calls",),
                        help="which harness measured the pairwise column. "
                             "`steps` was withdrawn 2026-09-04 with the "
                             "rest of 3.10 and is no longer offered.")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()

    runs = sorted(args.run_dir.glob("*.json"),
                  key=lambda p: json.loads(p.read_text())["ways"])
    if not runs:
        raise SystemExit(f"no runs under {args.run_dir}")

    rows = []
    print("after and emptied are controls: both must return to `solo ms`")
    print("  after   = solo re-measured with every peer IDLE")
    print("  emptied = the same solo after torch.cuda.empty_cache()")
    print()
    print(f"{'ways':>5} {'slice':>6} {'solo ms':>9} {'ratio':>8} "
          f"{'ep1':>7} {'after':>9} {'emptied':>9}  per-slice spread")
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
        pairs = [pairwise(w, die, args.device,
                          args.pairwise_source) for w in widths]
        vs = [(e / q - 1) * 100 if q else None
              for e, q in zip(per_slice, pairs)]
        shown = statistics.mean(v for v in vs if v is not None) \
            if any(v is not None for v in vs) else None
        label = (f"{widths[0]}" if len(set(widths)) == 1
                 else "+".join(str(w) for w in widths))
        models = d.get("slice_models") or []
        if len(set(models)) > 1:
            # A mean solo over two different models is not a number; the
            # per-slice column is the one to read in a mismatched run.
            label += " mix"
        rows.append({"ways": ways, "slice_widths": widths,
                     "reverse_offsets": d.get("reverse_offsets", False),
                     "steady_mean": steady, "episode_1_mean": first,
                     "solo_ms": solo, "pairwise": pairs,
                     "vs_pairwise_pct": vs,
                     "per_slice": per_slice,
                     "run": path.name})
        after = d.get("solo_after")
        after_ms = (statistics.mean(s["p50_s"] for s in after) * 1000
                    if after else None)
        emptied = d.get("solo_after_empty_cache")
        emptied_ms = (statistics.mean(s["p50_s"] for s in emptied) * 1000
                      if emptied else None)
        rows[-1]["solo_after_ms"] = after_ms
        rows[-1]["solo_after_empty_cache_ms"] = emptied_ms
        print(f"{ways:>5} {label:>6} {solo:>9.1f} {steady:>8.4f} "
              f"{first:>7.3f} "
              f"{(f'{after_ms:.1f}' if after_ms else '--'):>9} "
              f"{(f'{emptied_ms:.1f}' if emptied_ms else '--'):>9}  "
              + " ".join(f"{v:.3f}" for v in per_slice))

    print("\nratio   = last episode's co-run over the pre-episode solo.")
    print("ep1     = the first co-run episode.")
    print("A run whose `after` or `emptied` does NOT return to `solo ms`")
    print("is not measuring co-run and its ratio must not be published.")
    if args.json:
        args.json.write_text(json.dumps(rows, indent=1) + "\n")
        print(f"-> {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
