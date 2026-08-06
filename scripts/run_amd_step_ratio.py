#!/usr/bin/env python3
"""Measure step-time ratios between tenant workpoints at a half-die quota.

Gate C froze a pairing rule: share the die only when two tenants' predicted
step times are within 1.6x. That constant has no measurement behind it. The
two models profiled so far sit at 1.0 (SDXL against itself) and 3.4 (SDXL
against CogVideoX-2b), and 1.6 was placed between them because nothing was
measured in between. The decision log records this as the reason the
behavioural half of the freeze cannot see a change to that constant.

Rather than acquire a third model, this varies the workpoint of one. SDXL
at a larger resolution is slower per step by a controllable factor, so a
single model spans the range continuously. What matters for the pairing
rule is the ratio of step times, not which checkpoint produced them.

Everything runs at a fixed half-die quota (16 of 32 units) because that is
the quota the rule evaluates: ``step_matched_pairing`` compares
``predicted_step_seconds[16]``. Measuring at full width and dividing would
assume the ratio is quota-invariant, which is what Amdahl's form says it is
not -- the serial fraction differs per workpoint, so the ratio at 32 units
is not the ratio at 16.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time

sys.dont_write_bytecode = True

REPO = pathlib.Path(__file__).resolve().parent.parent
MASKABLE_UNITS = 32


def mask_for(units: int) -> str:
    if not 1 <= units <= MASKABLE_UNITS:
        raise ValueError(f"quota {units} outside 1..{MASKABLE_UNITS}")
    return hex((1 << units) - 1)


def run_cell(*, model: str, height: int, width: int, frames: int, units: int,
             steps: int, warmup: int, samples: int, target_cv: float,
             max_samples: int, seed: int, vae_tiling: bool) -> dict:
    env = dict(os.environ)
    env["ROC_GLOBAL_CU_MASK"] = mask_for(units)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    argv = [
        sys.executable, str(REPO / "scripts" / "amd_profile_cell.py"),
        "--model", model,
        "--batch", "1",
        "--steps", str(steps),
        "--warmup", str(warmup),
        "--samples", str(samples),
        "--target-cv", str(target_cv),
        "--max-samples", str(max_samples),
        "--height", str(height),
        "--width", str(width),
        "--frames", str(frames),
        "--seed", str(seed),
    ]
    if vae_tiling:
        argv.append("--vae-tiling")
    started = time.time()
    proc = subprocess.run(argv, env=env, capture_output=True, text=True)
    body = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
    if proc.returncode != 0 or not body:
        return {
            "status": "cell_failed",
            "returncode": proc.returncode,
            "model": model, "height": height, "width": width,
            "frames": frames, "requested_units": units,
            "stderr_tail": proc.stderr[-2000:],
            "stdout_tail": proc.stdout[-800:],
            "wall_s": time.time() - started,
        }
    record = json.loads(body[-1])
    record["status"] = "ok"
    record["requested_units"] = units
    # Video pipelines ignore --height/--width and run at their native
    # resolution, so labelling one with a resolution it did not use would
    # describe the cell by a parameter that had no effect. The cell itself
    # already records None for those fields; the label has to agree.
    shape = (f"{record.get('frames')}f" if record.get("frames") is not None
             else f"{width}x{height}")
    record["workpoint"] = f"{model}@{shape}/{units}u"
    record["shape"] = shape
    record["wall_s"] = time.time() - started
    return record


def per_step_seconds(row: dict) -> float | None:
    """The quantity the pairing rule compares, and nothing else.

    ``p50_s`` is the whole call including VAE decode, which a scheduler
    does not re-decide every step. The cell reports the denoising steps
    separately, timed with CUDA events, and that is what the pairing rule
    compares. There is no fallback to the call time: substituting it for
    rows that lack the per-step figure would make those ratios mean
    something different from the rest, while looking identical.
    """
    total = row.get("per_step_total_s")
    count = row.get("per_step_count")
    if not total or not count:
        return None
    if not row.get("per_step_timing_consistent", True):
        # The cell already found the per-step clock inconsistent with the
        # call it sits inside; a ratio built on it would be meaningless.
        return None
    return total / count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="sdxl")
    parser.add_argument("--sizes", default="1024,1152,1280,1408,1536",
                        help="square resolutions to profile")
    parser.add_argument("--units", default="16",
                        help="comma-separated quotas to measure at. The "
                             "pairing rule reads the half-die prediction, "
                             "so 16 is the meaningful one for tolerance "
                             "work; a full list rebuilds the per-step "
                             "quota curve without going through call times")
    parser.add_argument("--steps", type=int, default=8)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--samples", type=int, default=8)
    parser.add_argument("--target-cv", type=float, default=0.05)
    parser.add_argument("--max-samples", type=int, default=40)
    parser.add_argument("--frames", type=int, default=49)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--vae-tiling", action="store_true")
    parser.add_argument("--out", type=pathlib.Path, required=True)
    args = parser.parse_args()

    sizes = [int(s) for s in args.sizes.split(",") if s.strip()]
    quotas = [int(u) for u in str(args.units).split(",") if u.strip()]
    rows = []
    for size in sizes:
      for units in quotas:
        print(f"[{time.strftime('%H:%M:%S')}] {args.model} @ {size}x{size}, "
              f"{units} units", flush=True)
        row = run_cell(
            model=args.model, height=size, width=size, frames=args.frames,
            units=units, steps=args.steps, warmup=args.warmup,
            samples=args.samples, target_cv=args.target_cv,
            max_samples=args.max_samples, seed=args.seed,
            vae_tiling=args.vae_tiling,
        )
        row["size"] = size
        rows.append(row)
        step = per_step_seconds(row)
        if row["status"] == "ok" and step is not None:
            print(f"    per-step p50 {step * 1000:8.2f} ms   "
                  f"cv={row.get('cv')}", flush=True)
        else:
            print(f"    FAILED rc={row.get('returncode')} "
                  f"{row.get('stderr_tail', '')[-200:]}", flush=True)

    usable = [r for r in rows
              if r["status"] == "ok" and per_step_seconds(r) is not None]
    # Ratios only within one quota: comparing a 16-unit cell against a
    # 32-unit one is a quota curve, not a tenant-pairing ratio, and the
    # pairing rule never compares across quotas.
    ratios = []
    for i, left in enumerate(usable):
        for right in usable[i + 1:]:
            if left["requested_units"] != right["requested_units"]:
                continue
            a, b = per_step_seconds(left), per_step_seconds(right)
            lo, hi = min(a, b), max(a, b)
            ratios.append({
                "pair": [left["workpoint"], right["workpoint"]],
                "sizes": [left["size"], right["size"]],
                "step_seconds": [a, b],
                "ratio": hi / lo if lo > 0 else None,
            })
    ratios.sort(key=lambda r: r["ratio"] if r["ratio"] else 0.0)

    curve = {}
    for row in usable:
        # Keyed by the shape that actually varied, which for a video model
        # is the frame count rather than the ignored --sizes value.
        key = row.get("shape") or str(row["size"])
        curve.setdefault(key, {})[row["requested_units"]] = (
            per_step_seconds(row)
        )

    payload = {
        "schema_version": "burstserve.amd-step-ratio/v2",
        "per_step_quota_curve": curve,
        "purpose": (
            "supply measured step-time ratios between 1.0 and 3.4 so the "
            "frozen 1.6 pairing tolerance can be tested rather than assumed"
        ),
        "model": args.model,
        "units": quotas,
        "note": (
            "measured at the half-die quota the pairing rule reads; ratios "
            "are not quota-invariant under Amdahl's form, so a full-die "
            "measurement would not substitute"
        ),
        "cells": rows,
        "ratios": ratios,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2) + "\n")

    print(f"\n{'pair':<34} {'ratio':>7}")
    for row in ratios:
        print(f"{row['pair'][0]} / {row['pair'][1]:<12} {row['ratio']:7.3f}")
    print(f"\n-> {args.out}")
    return 0 if usable else 1


if __name__ == "__main__":
    raise SystemExit(main())
