#!/usr/bin/env python3
"""Test the frozen 1.6 pairing tolerance against measured co-runs.

`step_matched_pairing` shares the die only when two tenants' predicted
step times are within 1.6x. That constant was placed between the two
ratios the project had measured -- 1.0 for a model against itself and 3.4
for SDXL against CogVideoX-2b -- with nothing in between, and the freeze's
behavioural lock cannot see a change to it for the same reason.

Varying one model's resolution fills the gap: SDXL at 1024 against 1280
is a ratio of 1.693, at 1152 against 1408 it is 1.635. This script turns
each measured co-run into the decision the rule is making, and reports
where pairing actually stops paying.

The comparison is makespan on equal step counts, which is what the
scheduler trades:

    pairing   completes both in  max(t_a(16)*e_a, t_b(16)*e_b)
    rotating  completes both in  t_a(32) + t_b(32)

Pairing wins when the first is smaller. The externality factors come from
the co-run rather than from the table, because the table has one entry
for 16+16 measured on identical tenants, and whether it holds for
mismatched ones is precisely what is in question.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.dont_write_bytecode = True

REPO = pathlib.Path(__file__).resolve().parent.parent


def per_step_solo(curve_payload: dict, units: int) -> dict[str, float]:
    """Map workpoint label -> solo per-step seconds at ``units``."""
    out = {}
    for cell in curve_payload.get("cells", []):
        if cell.get("status") != "ok":
            continue
        if cell.get("requested_units") != units:
            continue
        total = cell.get("per_step_total_s")
        count = cell.get("per_step_count")
        if not total or not count:
            continue
        label = cell.get("shape") or str(cell.get("size"))
        out[label] = total / count
    return out


def analyse(corun: dict, solo16: dict[str, float],
            solo32: dict[str, float]) -> dict:
    sides = corun["sides"]
    overlap = corun["overlap"]

    def label(side: dict) -> str:
        return f"{side['width']}x{side['height']}"

    rows = {}
    for key in ("a", "b"):
        name = label(sides[key])
        rows[key] = {
            "workpoint": name,
            "solo_16": solo16.get(name),
            "solo_32": solo32.get(name),
            "corun_16": overlap[key].get("per_step_p50_s"),
            "externality": (
                1.0 + overlap[key]["per_step_externality"]
                if "per_step_externality" in overlap[key] else None
            ),
        }

    a, b = rows["a"], rows["b"]
    missing = [k for k, v in {**a, **b}.items() if v is None]
    if any(v is None for v in (a["solo_32"], b["solo_32"],
                               a["corun_16"], b["corun_16"])):
        return {"status": "incomplete", "missing": missing, "sides": rows}

    ratio_hi = max(a["solo_16"], b["solo_16"])
    ratio_lo = min(a["solo_16"], b["solo_16"])
    paired = max(a["corun_16"], b["corun_16"])
    rotated = a["solo_32"] + b["solo_32"]
    return {
        "status": "ok",
        "sides": rows,
        "step_time_ratio": ratio_hi / ratio_lo,
        "paired_makespan_per_step_s": paired,
        "rotating_makespan_per_step_s": rotated,
        "pairing_gain": rotated / paired - 1.0,
        "pairing_wins": paired < rotated,
        "rule_would_pair": (ratio_hi / ratio_lo) <= 1.6,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--curve-16", type=pathlib.Path, required=True,
                        help="step-ratio run at 16 units")
    parser.add_argument("--curve-32", type=pathlib.Path, required=True,
                        help="step-ratio run at 32 units")
    parser.add_argument("--corun", type=pathlib.Path, nargs="+",
                        required=True)
    parser.add_argument("--out", type=pathlib.Path)
    args = parser.parse_args()

    solo16 = per_step_solo(json.loads(args.curve_16.read_text()), 16)
    solo32 = per_step_solo(json.loads(args.curve_32.read_text()), 32)

    results = []
    for path in args.corun:
        row = analyse(json.loads(path.read_text()), solo16, solo32)
        row["report"] = path.name
        results.append(row)
    results.sort(key=lambda r: r.get("step_time_ratio") or 0.0)

    usable = [r for r in results if r["status"] == "ok"]
    verdict = {
        "schema_version": "burstserve.pairing-tolerance/v1",
        "frozen_tolerance": 1.6,
        "results": results,
    }
    if usable:
        agree = [r for r in usable if r["rule_would_pair"] == r["pairing_wins"]]
        verdict["rule_agrees_with_measurement"] = len(agree) == len(usable)
        verdict["disagreements"] = [
            {"ratio": r["step_time_ratio"], "rule_pairs": r["rule_would_pair"],
             "pairing_wins": r["pairing_wins"], "report": r["report"]}
            for r in usable if r["rule_would_pair"] != r["pairing_wins"]
        ]
        winners = [r["step_time_ratio"] for r in usable if r["pairing_wins"]]
        losers = [r["step_time_ratio"] for r in usable if not r["pairing_wins"]]
        verdict["highest_ratio_where_pairing_wins"] = (
            max(winners) if winners else None
        )
        verdict["lowest_ratio_where_pairing_loses"] = (
            min(losers) if losers else None
        )

    print(f"{'ratio':>7} {'paired':>9} {'rotating':>9} {'gain':>8} "
          f"{'wins':>5} {'rule':>5}  report")
    for row in results:
        if row["status"] != "ok":
            print(f"{'--':>7} incomplete: {row['report']}")
            continue
        print(f"{row['step_time_ratio']:7.3f} "
              f"{row['paired_makespan_per_step_s'] * 1000:8.1f}ms "
              f"{row['rotating_makespan_per_step_s'] * 1000:8.1f}ms "
              f"{row['pairing_gain'] * 100:+7.1f}% "
              f"{str(row['pairing_wins']):>5} "
              f"{str(row['rule_would_pair']):>5}  {row['report']}")

    if usable:
        print(f"\nrule agrees with measurement: "
              f"{verdict['rule_agrees_with_measurement']}")
        if verdict["disagreements"]:
            for d in verdict["disagreements"]:
                print(f"  ratio {d['ratio']:.3f}: rule pairs="
                      f"{d['rule_pairs']} but pairing wins="
                      f"{d['pairing_wins']}")

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(verdict, indent=2) + "\n")
        print(f"\n-> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
