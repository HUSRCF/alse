#!/usr/bin/env python3
"""One campaign, one method, one opponent, reported the way the pre-regs ask.

Every campaign since P has been analysed by hand -- the seed cluster
bootstrap, the wins/losses/ties, the sign test, the fast-state
recomputation, the grant-shape count -- and none of that analysis was
committed. So a published interval could not be regenerated, and one of
them turned out not to be: A3's lower bound moved by 0.003 the first time
the bootstrap was written down, because the ad-hoc version had sorted the
seeds differently. The number was immaterial; not being able to check it
was not.

This does all of it from the raw cells, per regime and load, and prints
the same set of quantities for any campaign so two campaigns can be read
side by side.

What it deliberately does NOT do is pick the opponent. That is the rule
this project keeps relearning: the comparator is chosen before the
measurement, by a declared rule, and here it is a command-line argument
that a pre-registration is supposed to have fixed in advance.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import Counter
from pathlib import Path

sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from burstserve.matrix_results import (  # noqa: E402
    Cell,
    cluster_bootstrap_ci,
    load_cells,
    paired_differences,
)

METRICS = (("miss_rate", "urgent miss", True),
           ("video_goodput", "video goodput", False))


def sign_p(wins: int, losses: int) -> float:
    """Two-sided sign test over the non-tied pairs.

    Reported beside the interval because 1.9's effect is asymmetric in
    magnitude and not in frequency -- 13 wins to 8 losses, p = 0.383 --
    and an interval alone would read as a consistent win.
    """
    n = wins + losses
    if n == 0:
        return 1.0
    total = 2 ** n
    k = min(wins, losses)
    tail = sum(math.comb(n, i) for i in range(k + 1))
    return min(1.0, 2.0 * tail / total)


def describe(pairs, lower_is_better: bool, baseline_mean=None) -> dict:
    values = [v for _, v in pairs]
    mean, low, high = cluster_bootstrap_ci(pairs)
    better = [v for v in values if (v < 0) == lower_is_better and v != 0]
    worse = [v for v in values if (v > 0) == lower_is_better and v != 0]
    ties = [v for v in values if v == 0]
    return {
        "n_pairs": len(values),
        "n_seeds": len({key[-1] for key, _ in pairs}),
        "mean": mean, "low": low, "high": high,
        "excludes_zero": (low > 0) or (high < 0),
        "wins": len(better), "losses": len(worse), "ties": len(ties),
        "worst_loss": max(worse, key=abs) if worse else 0.0,
        "best_win": max(better, key=abs) if better else 0.0,
        "sign_p": sign_p(len(better), len(worse)),
        # Published as a percentage of the opponent's own level, because
        # "+0.35 miss" means nothing without knowing what it is 0.35 of.
        "baseline_mean": baseline_mean,
        "relative_pct": (100.0 * mean / baseline_mean
                         if baseline_mean else None),
    }


def grant_shapes(cells) -> Counter:
    total: Counter = Counter()
    for cell in cells:
        for shape, count in ((cell.ledger or {}).get("grant_shapes")
                             or {}).items():
            total[shape] += count
    return total


# The configuration key. Regime belongs in it because a directory can
# hold arrivals and backlog cells at the same (load, burst, seed), and
# the runtime factors belong in it because the 2x2 varied one of them.
# The seed stays last so the cluster bootstrap still finds it.
def configuration(cell) -> tuple:
    return (cell.regime, cell.max_steps_per_round, cell.requests_per_tenant)


def report(cells, method: str, against: str, label: str) -> dict:
    out = {"group": label, "method": method, "against": against,
           "cells": len(cells)}
    states = Counter(c.co_run_state for c in cells)
    out["co_run_states"] = dict(states)
    print(f"\n=== {label} ===")
    print(f"  cells {len(cells)}   drawn state {dict(states)}")
    for policy in sorted({c.policy for c in cells}):
        shapes = grant_shapes([c for c in cells if c.policy == policy])
        if shapes:
            top = ", ".join(f"{k}:{v}" for k, v in shapes.most_common(6))
            print(f"  grants {policy:26s} {top}")
    for metric, name, lower_is_better in METRICS:
        pairs = paired_differences(cells, method, against, metric,
                                   extra_key=configuration)
        if not pairs:
            print(f"  {name:14s} no paired cells")
            continue
        keys = {key for key, _ in pairs}
        opponent = [getattr(c, metric) for c in cells
                    if c.policy == against
                    and configuration(c) + (c.load, c.burst, c.seed) in keys
                    and getattr(c, metric) is not None]
        d = describe(pairs, lower_is_better,
                     statistics.mean(opponent) if opponent else None)
        direction = "better" if (d["mean"] < 0) == lower_is_better else "worse"
        star = "*" if d["excludes_zero"] else " "
        rel = ("" if d["relative_pct"] is None
               else f" ({d['relative_pct']:+.1f}%)")
        print(f"  {name:14s} {d['mean']:+.4f}{rel} "
              f"[{d['low']:+.4f}, {d['high']:+.4f}]{star} {direction}"
              f"   w/l/t {d['wins']}/{d['losses']}/{d['ties']}"
              f"  worst loss {d['worst_loss']:+.4f}"
              f"  sign p {d['sign_p']:.3f}"
              f"  n {d['n_pairs']} in {d['n_seeds']} seeds")
        out[metric] = d
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("directory", type=Path)
    ap.add_argument("--method", required=True,
                    help="the arm under test, fixed by a pre-registration")
    ap.add_argument("--against", required=True,
                    help="the opponent, fixed by the same pre-registration")
    ap.add_argument("--fast-only", action="store_true",
                    help="also recompute on cells that drew the fast "
                         "co-run state, which several pre-registrations "
                         "declare in advance")
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()

    cells = load_cells(args.directory)
    if not cells:
        print(f"no matrix cells in {args.directory}")
        return 1
    print(f"{len(cells)} cells, policies "
          f"{sorted({c.policy for c in cells})}")

    # Group by every factor the campaign actually varied. A factor that
    # is constant adds nothing; a factor that varies and is pooled over
    # averages two arms of a factorial design into one number, which is
    # what the 2x2 would suffer from if only regime and load were used.
    factors = [("regime", lambda c: c.regime),
               ("load", lambda c: c.load),
               ("cap", lambda c: c.max_steps_per_round),
               ("rpt", lambda c: c.requests_per_tenant)]
    varying = [(name, get) for name, get in factors
               if len({get(c) for c in cells}) > 1]
    print("  varying factors: "
          + (", ".join(name for name, _ in varying) or "none"))

    out = [report(cells, args.method, args.against, "all cells")]
    # Each factor on its own first -- that is how 3.6 reports arrivals
    # against backlog -- then the full cross.
    for name, get in varying:
        for value in sorted({get(c) for c in cells}, key=repr):
            subset = [c for c in cells if get(c) == value]
            out.append(report(subset, args.method, args.against,
                              f"{name} {value}"))
    if len(varying) > 1:
        groups: dict[tuple, list[Cell]] = {}
        for cell in cells:
            groups.setdefault(tuple(get(cell) for _, get in varying),
                              []).append(cell)
        for key in sorted(groups, key=repr):
            label = " ".join(f"{n} {v}" for (n, _), v in zip(varying, key))
            out.append(report(groups[key], args.method, args.against, label))
    if args.fast_only:
        fast = [c for c in cells if c.co_run_state == "fast"]
        if len(fast) == len(cells):
            print("\n(every cell drew fast; the fast-only split is the "
                  "same analysis and is not repeated)")
        elif fast:
            out.append(report(fast, args.method, args.against,
                              "fast-state cells only"))
    if args.json:
        args.json.write_text(json.dumps(out, indent=1, default=str))
        print(f"\n-> {args.json}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
