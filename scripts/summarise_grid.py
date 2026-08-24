#!/usr/bin/env python3
"""Per-policy means, and how many configurations could tell them apart.

The second table is the one that matters. A paired bootstrap over 45
configurations reads as 45 pieces of evidence; when 42 of the paired
differences are exactly zero it is three. Worse, the 2.5th percentile is
then pinned at zero as arithmetic -- a resample that draws none of the
non-zero configurations has probability ((n-k)/n)^n, and when that
exceeds 0.025 the interval cannot exclude zero whatever the effect is.
That is printed for every comparison so no interval gets read as
significance it cannot carry.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from burstserve.matrix_results import (  # noqa: E402
    ORACLE_POLICIES, bootstrap_ci, load_cells, paired_differences,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("directories", type=Path, nargs="+")
    ap.add_argument("--against", default=None,
                    help="policy to compare every other against; default is "
                         "the one with the best mean miss rate")
    args = ap.parse_args()

    cells = []
    for directory in args.directories:
        cells += load_cells(directory)
    if not cells:
        print("no cells")
        return 1
    policies = sorted({c.policy for c in cells})
    configs = sorted({c.config for c in cells})
    seeds = sorted({c.seed for c in cells})
    print(f"{len(cells)} cells, {len(policies)} policies, "
          f"{len(configs)} configs, seeds {seeds}")

    def mean(policy, metric, config=None):
        values = [getattr(c, metric) for c in cells
                  if c.policy == policy and getattr(c, metric) is not None
                  and (config is None or c.config == config)]
        return statistics.mean(values) if values else None

    def cell(value: float | None, width: int, places: int) -> str:
        return (f"{value:>{width}.{places}f}" if value is not None
                else f"{'-':>{width}}")

    print(f"\n{'policy':<30} {'miss':>9} {'video':>9} {'p99 s':>9} {'n':>5}")
    for policy in sorted(policies, key=lambda p: mean(p, "miss_rate") or 9):
        n = len([c for c in cells if c.policy == policy])
        print(f"{policy:<30} "
              f"{cell(mean(policy, 'miss_rate'), 9, 4)}"
              f"{cell(mean(policy, 'video_goodput'), 9, 3)}"
              f"{cell(mean(policy, 'urgent_p99_s'), 9, 3)}"
              f"{n:>6}")

    # The oracle is the ceiling, not an opponent: defaulting to it would
    # make every row a restatement of "the oracle wins", which is true by
    # construction and says nothing about any policy.
    candidates = [p for p in policies if mean(p, "miss_rate") is not None
                  and p not in ORACLE_POLICIES]
    against = args.against or (min(candidates, key=lambda p: mean(p, "miss_rate"))
                               if candidates else policies[0])
    print(f"\npaired against {against}")
    print(f"{'policy':<30} {'differs':>9} {'mean':>10} {'ci95':>24} "
          f"{'rel':>8} {'P(all-zero resample)':>21}")
    base_mean = mean(against, "miss_rate")
    for policy in policies:
        if policy == against:
            continue
        deltas = [d for _, d in paired_differences(cells, policy, against,
                                                   "miss_rate")]
        if not deltas:
            continue
        n, k = len(deltas), sum(1 for d in deltas if d != 0.0)
        m, low, high = bootstrap_ci(deltas, seed=0)
        pinned = ((n - k) / n) ** n
        flag = "  PINNED" if pinned > 0.025 else ""
        print(f"{policy:<30} {k:>5}/{n:<3} {m:>+10.4f} "
              f"[{low:+.4f}, {high:+.4f}]".ljust(0)
              + f" {m / base_mean:>+7.2%} {pinned:>16.4f}{flag}")
    print("\nPINNED: a resample drawing none of the differing configurations "
          "has probability over 0.025, so the interval's lower bound is zero "
          "by arithmetic and cannot be read as significance.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
