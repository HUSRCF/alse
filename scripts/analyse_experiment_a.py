#!/usr/bin/env python3
"""Experiment A, judged by the rules written down before it ran.

Reads docs/prereg-experiment-a.md's three verdicts out of code rather
than out of a reading of the table. The verdict this prints is the one
that gets reported.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from burstserve.matrix_results import (  # noqa: E402
    Cell, bootstrap_ci, load_cells, paired_differences,
)

FIXED = [f"fixed_split_{u}" for u in (4, 8, 16, 24, 28)]
ADAPTIVE = ["deadline_aware", "step_matched_pairing", "slo_aware_partitioning"]
REFERENCE = ["exclusive_fcfs", "oracle_shortest_remaining"]


def mean_by(cells, policy, metric, config=None):
    values = [getattr(c, metric) for c in cells
              if c.policy == policy and getattr(c, metric) is not None
              and (config is None or c.config == config)]
    return statistics.mean(values) if values else None


def oracle_static_cells(cells: list[Cell]) -> list[Cell]:
    """A synthetic policy: per configuration, the best fixed split.

    Chosen on the mean over seeds at that configuration, not per seed. A
    per-seed choice would let the oracle see the noise as well as the
    workload, which is a bound on nothing.
    """
    out = []
    for config in sorted({c.config for c in cells}):
        scored = {p: mean_by(cells, p, "miss_rate", config) for p in FIXED}
        scored = {p: v for p, v in scored.items() if v is not None}
        if not scored:
            continue
        best = min(scored, key=scored.get)
        for cell in cells:
            if cell.policy == best and cell.config == config:
                out.append(Cell(policy="static_oracle", load=cell.load,
                                burst=cell.burst, seed=cell.seed,
                                miss_rate=cell.miss_rate,
                                video_goodput=cell.video_goodput,
                                urgent_p99_s=cell.urgent_p99_s,
                                urgent_completed=cell.urgent_completed,
                                path=cell.path))
    return out


def compare(cells, method, against, seed=0):
    row = {}
    for metric in ("miss_rate", "video_goodput"):
        pairs = paired_differences(cells, method, against, metric)
        if not pairs:
            row[metric] = None
            continue
        deltas = [d for _, d in pairs]
        mean, low, high = bootstrap_ci(deltas, seed=seed)
        base = mean_by(cells, against, metric)
        row[metric] = {"pairs": len(pairs), "mean": mean, "ci95": [low, high],
                       "baseline_mean": base,
                       "relative": mean / base if base else None}
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("directory", type=Path)
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()

    cells = load_cells(args.directory)
    if not cells:
        print(f"no cells in {args.directory}")
        return 1
    configs = sorted({c.config for c in cells})
    print(f"{len(cells)} cells, configs {configs}")

    # --- completeness, and the invalidity conditions -------------------
    incomplete = []
    for config in configs:
        for policy in FIXED + ADAPTIVE + REFERENCE:
            n = len([c for c in cells
                     if c.policy == policy and c.config == config])
            if n < 5:
                incomplete.append((config, policy, n))
    if incomplete:
        print(f"\nINCOMPLETE ({len(incomplete)} policy-configs under 5 seeds):")
        for config, policy, n in incomplete[:12]:
            print(f"  {config} {policy:<28} {n}/5")

    # --- the table ----------------------------------------------------
    print(f"\n{'policy':<28}", end="")
    for config in configs:
        print(f"{('miss@' + str(config[0])):>10}"
              f"{('vid@' + str(config[0])):>10}", end="")
    print()
    for policy in FIXED + ["static_even"] + ADAPTIVE + REFERENCE:
        if not any(c.policy == policy for c in cells):
            continue
        print(f"{policy:<28}", end="")
        for config in configs:
            m = mean_by(cells, policy, "miss_rate", config)
            v = mean_by(cells, policy, "video_goodput", config)
            print(f"{m:>10.4f}" if m is not None else f"{'-':>10}",
                  f"{v:>10.3f}" if v is not None else f"{'-':>10}",
                  sep="", end="")
        print()

    # --- the prediction the simulator made ----------------------------
    print("\nprediction: all five fixed splits share one miss rate?")
    for config in configs:
        values = [mean_by(cells, p, "miss_rate", config) for p in FIXED]
        values = [v for v in values if v is not None]
        if len(values) < 2:
            continue
        spread = max(values) - min(values)
        vid = [mean_by(cells, p, "video_goodput", config) for p in FIXED]
        vid = [v for v in vid if v is not None]
        ratio = (max(vid) / min(vid)) if vid and min(vid) else float("nan")
        print(f"  {config}: miss spread {spread:.4f} over "
              f"[{min(values):.4f}, {max(values):.4f}], "
              f"video goodput ratio {ratio:.2f}x")

    # --- the two static comparators, by rule --------------------------
    scored = {p: mean_by(cells, p, "miss_rate") for p in FIXED}
    scored = {p: v for p, v in scored.items() if v is not None}
    if not scored:
        print("\nno fixed splits present; nothing to judge")
        return 1
    deploy = min(scored, key=scored.get)
    print(f"\nstatic-deploy (best fixed split overall) = {deploy} "
          f"({scored[deploy]:.4f})")
    per_config = {}
    for config in configs:
        s = {p: mean_by(cells, p, "miss_rate", config) for p in FIXED}
        s = {p: v for p, v in s.items() if v is not None}
        if s:
            per_config[config] = min(s, key=s.get)
    print(f"static-oracle (best fixed split per config) = {per_config}")
    moved = len(set(per_config.values())) > 1
    print(f"  best split moves with load: {moved}")

    with_oracle = cells + oracle_static_cells(cells)

    adaptive_scored = {p: mean_by(cells, p, "miss_rate") for p in ADAPTIVE}
    adaptive_scored = {p: v for p, v in adaptive_scored.items()
                       if v is not None}
    if not adaptive_scored:
        print("\nno adaptive policies present; nothing to judge")
        return 1
    best = min(adaptive_scored, key=adaptive_scored.get)
    print(f"best adaptive policy M = {best} ({adaptive_scored[best]:.4f})")

    result = {"static_deploy": deploy, "static_oracle_per_config":
              {str(k): v for k, v in per_config.items()},
              "best_split_moves_with_load": moved, "method": best}
    for name, opponent in (("vs_static_deploy", deploy),
                           ("vs_static_oracle", "static_oracle")):
        row = compare(with_oracle, best, opponent)
        result[name] = row
        print(f"\n{best} {name}:")
        for metric, r in row.items():
            if r is None:
                print(f"  {metric}: no pairs")
                continue
            print(f"  {metric}: {r['mean']:+.4f} "
                  f"[{r['ci95'][0]:+.4f}, {r['ci95'][1]:+.4f}] "
                  f"({r['relative']:+.1%} of {r['baseline_mean']:.4f}, "
                  f"{r['pairs']} pairs)")

    # --- the verdict --------------------------------------------------
    def beats(row):
        r = row.get("miss_rate")
        return bool(r and r["ci95"][1] < 0)

    def video_ok(row):
        r = row.get("video_goodput")
        return bool(r and r["relative"] is not None and r["relative"] >= -0.05)

    d, o = result["vs_static_deploy"], result["vs_static_oracle"]
    if not beats(d):
        verdict = ("1. NO SCHEDULING CONTRIBUTION -- the best adaptive policy "
                   "does not beat a split chosen once at deployment time.")
    elif not beats(o):
        verdict = ("2. AUTO-TUNING ONLY -- beats a deployed split but not the "
                   "best split per configuration. Claim is 'finds the right "
                   "split without knowing the workload', not 'beats static "
                   "partitioning'.")
    elif not video_ok(o):
        verdict = ("1. NO SCHEDULING CONTRIBUTION -- the miss-rate win over "
                   "static-oracle costs more than 5% of video goodput, which "
                   "the pre-registration does not accept as a win.")
    else:
        verdict = ("3. SCHEDULING CONTRIBUTION -- beats the best split per "
                   "configuration with the interval excluding zero and video "
                   "goodput within 5%.")
    result["verdict"] = verdict
    print(f"\nVERDICT: {verdict}")

    if args.json:
        args.json.write_text(json.dumps(result, indent=2, sort_keys=True))
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
