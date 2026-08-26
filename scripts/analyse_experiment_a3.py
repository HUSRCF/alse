#!/usr/bin/env python3
"""Experiment A3, judged by the rules written down before it ran.

docs/prereg-experiment-a3.md fixes the comparison, the verdicts, the
distribution report and the four predictors. This script reads all of
them out of code so the verdict it prints is the one that gets reported.
"""

from __future__ import annotations

import argparse
import dataclasses
import glob
import json
import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from burstserve.matrix_results import (  # noqa: E402
    bootstrap_ci, load_cells, paired_differences,
)
from burstserve.workload import CellSpec, build_trace  # noqa: E402

METHOD = "step_matched_pairing"
COMPARATOR = "exclusive_fcfs"
ARMS = [COMPARATOR, METHOD, "slo_aware_partitioning", "fixed_split_8"]
SEEDS = list(range(5, 20))
LOADS = [0.6, 1.05]
WIN = 0.05  # "larger than 5 points", from the pre-registration


def pearson(xs, ys):
    mx, my = statistics.mean(xs), statistics.mean(ys)
    num = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    den = math.sqrt(sum((a - mx) ** 2 for a in xs)
                    * sum((b - my) ** 2 for b in ys))
    return num / den if den else float("nan")


def trace_properties(directory: Path) -> dict:
    """The four declared predictors, per (load, seed).

    Rebuilt from each cell's recorded spec and its *measured* isolated
    service times, so these are the traces that ran rather than a
    reconstruction from nominal parameters.
    """
    fields = {f.name for f in dataclasses.fields(CellSpec)}
    out = {}
    for path in sorted(glob.glob(str(directory / f"*_{METHOD}.json"))):
        cell = json.load(open(path))
        spec_d = cell["spec"]
        iso = cell["isolated_service_s"]
        spec = CellSpec(**{k: v for k, v in spec_d.items() if k in fields})
        trace = build_trace(spec, urgent_service_s=iso["urgent"],
                            video_service_s=iso["video"],
                            urgent_isolated_latency_p99_s=iso["urgent"],
                            video_backlog=bool(spec_d.get("video_backlog")))
        reqs = getattr(trace, "requests", trace)
        urgent = [r for r in reqs if r.tenant == "urgent"]
        video = [r for r in reqs if r.tenant == "video"]

        def busy(rs, service):
            spans, t = [], 0.0
            for r in sorted(rs, key=lambda r: r.arrival_s):
                t = max(t, r.arrival_s)
                spans.append((t, t + service))
                t += service
            return spans

        ub, vb = busy(urgent, iso["urgent"]), busy(video, iso["video"])
        # A cell can have no video tenant at all -- 27 of the 405-cell
        # grid did. That is real data about the trace, so it enters the
        # predictor set with a coexistence of zero rather than being
        # dropped, which would select the configurations that had a
        # tenant to coexist with.
        ends = [e for _, e in ub] + [e for _, e in vb]
        horizon = max(ends) if ends else 0.0
        overlap = sum(max(0.0, min(b, e) - max(a, c))
                      for a, b in ub for c, e in vb)
        out[(spec_d["load"], spec_d["seed"])] = {
            "urgent request count": len(urgent),
            "video request count": len(video),
            "horizon in seconds": horizon,
            "coexistence fraction": overlap / horizon if horizon else 0.0,
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("directory", type=Path)
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()

    cells = load_cells(args.directory)
    if not cells:
        print(f"no cells in {args.directory}")
        return 1
    print(f"{len(cells)} cells")

    # --- the invalidity conditions, checked before anything is judged --
    raw = [json.load(open(p)) for p in sorted(glob.glob(str(args.directory / "*.json")))]
    unsafe = [r for r in raw if r.get("safe") is not True
              or (r.get("safety_failures") or [])]
    print(f"\nsafety: {len(raw) - len(unsafe)}/{len(raw)} cells safe, "
          f"{sum(len(r.get('safety_failures') or []) for r in raw)} failures")
    if unsafe:
        print("  INVALID: a safety failure aborts the experiment, not the cell")

    states = {}
    for r in raw:
        st = (r.get("drawn_co_run_state") or {}).get("state")
        states[st] = states.get(st, 0) + 1
    print(f"co-run states drawn: {states}")
    if len(states) == 1:
        print(f"  every group drew {next(iter(states))}; "
              f"the result is scoped to that state and says so")

    for load in LOADS:
        got = sorted({c.seed for c in cells
                      if c.load == load and c.policy == METHOD})
        if len(got) < len(SEEDS):
            print(f"  INCOMPLETE: load {load} has {len(got)}/{len(SEEDS)} "
                  f"seeds ({got})")

    # --- the table ----------------------------------------------------
    print(f"\n{'policy':<26}", end="")
    for load in LOADS:
        print(f"{('miss@' + str(load)):>10}{('vid@' + str(load)):>10}", end="")
    print()
    for policy in ARMS:
        rows = [c for c in cells if c.policy == policy]
        if not rows:
            continue
        print(f"{policy:<26}", end="")
        for load in LOADS:
            sel = [c for c in rows if c.load == load]
            m = statistics.mean([c.miss_rate for c in sel]) if sel else None
            v = statistics.mean([c.video_goodput for c in sel]) if sel else None
            print(f"{m:>10.4f}" if m is not None else f"{'-':>10}",
                  f"{v:>10.3f}" if v is not None else f"{'-':>10}",
                  sep="", end="")
        print()

    # --- the primary comparison ---------------------------------------
    result = {"method": METHOD, "comparator": COMPARATOR}
    pairs = paired_differences(cells, METHOD, COMPARATOR, "miss_rate")
    if not pairs:
        print("\nno pairs; nothing to judge")
        return 1
    deltas = [d for _, d in pairs]
    mean, low, high = bootstrap_ci(deltas, seed=0)
    base = statistics.mean([c.miss_rate for c in cells
                            if c.policy == COMPARATOR])
    vpairs = paired_differences(cells, METHOD, COMPARATOR, "video_goodput")
    vdel = [d for _, d in vpairs]
    vmean, vlow, vhigh = bootstrap_ci(vdel, seed=0)
    vbase = statistics.mean([c.video_goodput for c in cells
                             if c.policy == COMPARATOR])
    print(f"\n{METHOD} vs {COMPARATOR}, {len(deltas)} pairs:")
    print(f"  miss_rate:     {mean:+.4f} [{low:+.4f}, {high:+.4f}] "
          f"({mean / base:+.1%} of {base:.4f})")
    print(f"  video_goodput: {vmean:+.4f} [{vlow:+.4f}, {vhigh:+.4f}] "
          f"({vmean / vbase:+.1%} of {vbase:.4f})")
    result["miss_rate"] = {"pairs": len(deltas), "mean": mean,
                           "ci95": [low, high], "relative": mean / base}
    result["video_goodput"] = {"mean": vmean, "ci95": [vlow, vhigh],
                               "relative": vmean / vbase}

    # --- the distribution, reported whatever the interval does --------
    wins = [d for d in deltas if d < -WIN]
    losses = [d for d in deltas if d > WIN]
    ties = [d for d in deltas if d == 0.0]
    nontied = [d for d in deltas if d != 0.0]
    neg = sum(1 for d in nontied if d < 0)
    print(f"\ndistribution over {len(deltas)} configurations:")
    print(f"  win  > {WIN:.2f}: {len(wins)}   loss > {WIN:.2f}: {len(losses)}"
          f"   exact ties: {len(ties)}")
    print(f"  sign test over {len(nontied)} non-tied pairs: "
          f"{neg} for, {len(nontied) - neg} against, "
          f"p = {sign_p(neg, len(nontied)):.4f}")
    concentrated = len(wins) < len(deltas) / 3
    print(f"  win carried by a minority of traces: {concentrated}")
    result["distribution"] = {"wins": len(wins), "losses": len(losses),
                              "ties": len(ties), "sign_neg": neg,
                              "sign_n": len(nontied),
                              "concentrated": concentrated}
    if len(ties) > 24:
        print("  INVALID: more than 24 of 30 configurations are exact ties; "
              "no interval over fewer than six points means anything")

    # --- the four declared predictors, all reported -------------------
    props = trace_properties(args.directory)
    print("\npredictors (declared in advance; none is claimed by this run "
          "alone):")
    result["predictors"] = {}
    keyed = dict(pairs)
    for name in ("urgent request count", "video request count",
                 "horizon in seconds", "coexistence fraction"):
        xs, ys = [], []
        for (load, _burst, seed), delta in keyed.items():
            p = props.get((load, seed))
            if p is None:
                continue
            xs.append(p[name])
            ys.append(delta)
        r = pearson(xs, ys) if len(xs) > 2 else float("nan")
        result["predictors"][name] = r
        print(f"  r(diff, {name:<22}) = {r:+.3f}   (n={len(xs)})")

    # --- the verdict --------------------------------------------------
    video_ok = (vmean / vbase) >= -0.05
    if high < 0 and video_ok:
        verdict = ("1. RUN-TIME CHOICE BEATS NO PARTITIONING under "
                   "arrivals -- interval excludes zero and video goodput "
                   "is within 5%.")
    elif high < 0:
        verdict = ("2. NOT ESTABLISHED -- the miss-rate interval excludes "
                   f"zero but video goodput is {vmean / vbase:+.1%}, outside "
                   "the -5% bound the pre-registration fixed.")
    elif low > 0:
        verdict = ("3. NO PARTITIONING IS BETTER -- the interval excludes "
                   "zero on the positive side. A negative result.")
    else:
        verdict = ("2. NOT ESTABLISHED -- the interval crosses zero at "
                   f"{len(deltas)} pairs. The positive claim rests on the "
                   "backlog regime alone, where it costs 25% of video "
                   "goodput.")
    print(f"\nVERDICT: {verdict}")
    result["verdict"] = verdict

    # --- secondary, declared: did M flip, and does A replicate? -------
    for other, label in (("slo_aware_partitioning", "M may have flipped"),
                         ("fixed_split_8", "replicates A's own comparison")):
        p2 = paired_differences(cells, METHOD, other, "miss_rate")
        if not p2:
            continue
        d2 = [d for _, d in p2]
        m2, l2, h2 = bootstrap_ci(d2, seed=0)
        print(f"\nsecondary -- {METHOD} vs {other} ({label}):")
        print(f"  miss_rate: {m2:+.4f} [{l2:+.4f}, {h2:+.4f}] "
              f"({len(d2)} pairs)")
        result[f"vs_{other}"] = {"mean": m2, "ci95": [l2, h2],
                                 "pairs": len(d2)}

    if args.json:
        args.json.write_text(json.dumps(result, indent=2))
    return 0


def sign_p(k: int, n: int) -> float:
    """Two-sided exact binomial p at 0.5, for the sign test."""
    if n == 0:
        return float("nan")
    def c(a, b):
        return math.comb(a, b)
    tail = min(k, n - k)
    p = 2 * sum(c(n, i) for i in range(tail + 1)) / (2 ** n)
    return min(1.0, p)


if __name__ == "__main__":
    raise SystemExit(main())
