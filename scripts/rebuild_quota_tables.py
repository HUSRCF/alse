#!/usr/bin/env python3
"""Rebuild the simulator's cost table from per-step measurements.

The table the simulator ships was assembled from call-level p50s plus a
full-die per-step constant, and both parts turned out to be wrong in ways
that only showed up when the per-step curve was measured directly:

  * ``step_seconds_at_full`` is recorded as 0.1521 s with the comment
    "1024x1024, 32 units". Gate B measured SDXL at 768x768, where the
    full-die per-step figure is 0.1124 s. 0.1521 matches neither that nor
    the measured 1024 figure of 0.2005 s; it is closest to 768 at *16*
    units, a half-die number standing in for a full-die one.

  * ``step_seconds`` derives the per-step time at other quotas by scaling
    that constant by the ratio of *call* p50s. A call is denoising steps
    plus a VAE decode, and the decode does not scale with quota the way
    the steps do, so the call ratio is diluted by a term the per-step
    ratio does not contain. At 768 the call ratio is 1.503 and the
    measured per-step ratio is 1.380.

This script does the conversion and reports what changes. It does not
edit the frozen table: the freeze exists so a change like this goes
through the decision log, and a script that rewrote the source would
route around the thing that makes the number trustworthy.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.dont_write_bytecode = True

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from burstserve.trace_sim import (                     # noqa: E402
    MEASURED_MODELS,
    MEASURED_QUOTA_SECONDS,
    QuotaCostModel,
)


def load_curve(path: pathlib.Path) -> tuple[int, dict[int, float]]:
    """Read one resolution's per-step quota curve from a step-ratio run."""
    payload = json.loads(path.read_text())
    curve_by_size = payload.get("per_step_quota_curve")
    if not curve_by_size:
        raise SystemExit(f"{path} has no per_step_quota_curve; it was "
                         f"produced before the v2 schema")
    if len(curve_by_size) != 1:
        raise SystemExit(f"{path} covers {len(curve_by_size)} resolutions; "
                         f"rebuild one at a time so the resolution the "
                         f"table claims is unambiguous")
    key = next(iter(curve_by_size))
    curve = {int(u): float(s) for u, s in curve_by_size[key].items()}
    return key, curve


def compare(model: str, size, curve: dict[int, float]) -> dict:
    """What the simulator believes against what was measured."""
    cost = QuotaCostModel.for_model(model)
    rows = []
    for units in sorted(curve):
        believed = cost.step_seconds(units)
        measured = curve[units]
        rows.append({
            "units": units,
            "believed_s": believed,
            "measured_s": measured,
            "relative_error": (believed - measured) / measured,
            "was_measured_quota": cost.is_measured(units),
        })
    full = curve.get(max(curve))
    return {
        "model": model,
        "shape": str(size),
        "believed_step_seconds_at_full": (
            MEASURED_MODELS[model]["step_seconds_at_full"]
        ),
        "measured_step_seconds_at_full": full,
        "rows": rows,
        "worst_relative_error": max(abs(r["relative_error"]) for r in rows),
    }


def proposed_tables(model: str, size, curve: dict[int, float]) -> dict:
    """The replacement, in the form the source file holds it.

    The quota table becomes the per-step curve itself rather than call
    p50s, which removes the call-to-step conversion entirely instead of
    correcting it. ``step_seconds`` already normalises by the full-die
    entry, so a curve of per-step seconds passes through unchanged.
    """
    full = curve[max(curve)]
    return {
        "MEASURED_MODELS[%s]" % model: {
            "step_seconds_at_full": round(full, 6),
            "shape": str(size),
        },
        "MEASURED_QUOTA_SECONDS[%s]" % model: {
            int(u): round(s, 6) for u, s in sorted(curve.items())
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--curve", type=pathlib.Path, required=True,
                        help="a step-ratio run with per_step_quota_curve")
    parser.add_argument("--model", default="sdxl")
    parser.add_argument("--out", type=pathlib.Path)
    args = parser.parse_args()

    size, curve = load_curve(args.curve)
    report = compare(args.model, size, curve)
    report["proposed"] = proposed_tables(args.model, size, curve)
    report["current_quota_table"] = MEASURED_QUOTA_SECONDS.get(args.model)
    report["note"] = (
        "Nothing is written to the source. Changing a frozen table "
        "requires a decision-log entry and a re-freeze; a script that "
        "edited it directly would bypass the check that makes it "
        "trustworthy."
    )

    print(f"{args.model} at {size}\n")
    print(f"{'units':>6} {'believed':>10} {'measured':>10} {'error':>9}  src")
    for row in report["rows"]:
        print(f"{row['units']:6d} {row['believed_s'] * 1000:9.2f}ms "
              f"{row['measured_s'] * 1000:9.2f}ms "
              f"{row['relative_error'] * 100:+8.2f}%  "
              f"{'measured' if row['was_measured_quota'] else 'fit'}")
    print(f"\nfull-die per-step: believed "
          f"{report['believed_step_seconds_at_full'] * 1000:.2f} ms, "
          f"measured {report['measured_step_seconds_at_full'] * 1000:.2f} ms")
    print(f"worst error: {report['worst_relative_error'] * 100:.2f}%")

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2) + "\n")
        print(f"\n-> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
