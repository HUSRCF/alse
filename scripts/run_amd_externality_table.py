"""Build the pairwise externality table Gate B asks for.

One co-run measures one pair. The table is the claim, and it has to cover
the axes along which the externality was found to vary rather than a
single point extrapolated everywhere:

  * quota split -- symmetric against asymmetric, because a scheduler
    hands out unequal shares and there is no reason the penalty is shared
    equally;
  * workload class -- pure matmul lost 47% per tenant against 23% for
    SDXL, so contention depends on how much of the work is compute
    (2026-08-05);
  * model pairing -- two tenants of the same model contend for the same
    resources at the same moments, which is not the general case.

Pairs that do not fit in memory are recorded as such rather than omitted.
On this card that is not a corner case: CogVideoX peaks near 28.5 GB, so
it cannot be paired with anything, and saying so is part of the table.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from burstserve.provenance import canonical_json  # noqa: E402

SCHEMA_VERSION = "burstserve.amd-externality-table/v1"

# (units_a, units_b, model_a, model_b, height, width, label)
DEFAULT_PAIRS = [
    (16, 16, "sdxl", "sdxl", 512, 512, "symmetric-half"),
    (8, 24, "sdxl", "sdxl", 512, 512, "asymmetric-1-to-3"),
    (4, 28, "sdxl", "sdxl", 512, 512, "asymmetric-1-to-7"),
    (24, 8, "sdxl", "sdxl", 512, 512, "asymmetric-3-to-1"),
]


def run_pair(entry, args, out_dir: Path) -> dict:
    units_a, units_b, model_a, model_b, height, width, label = entry
    report_path = out_dir / f"corun_{label}_{units_a}_{units_b}.json"
    argv = [
        sys.executable, str(REPO / "scripts/run_amd_corun.py"),
        "--model-a", model_a, "--model-b", model_b,
        "--units-a", str(units_a), "--units-b", str(units_b),
        "--height", str(height), "--width", str(width),
        "--steps-a", str(args.steps), "--steps-b", str(args.steps),
        "--samples", str(args.samples), "--warmup", str(args.warmup),
        "--co-run-seconds", str(args.co_run_seconds),
        "--out", str(report_path),
    ]
    started = time.time()
    proc = subprocess.run(argv, capture_output=True, text=True)
    record = {
        "label": label,
        "units_a": units_a, "units_b": units_b,
        "model_a": model_a, "model_b": model_b,
        "height": height, "width": width,
        "returncode": proc.returncode,
        "wall_s": time.time() - started,
    }
    if report_path.is_file():
        report = json.loads(report_path.read_text())
        record["status"] = report.get("status")
        record["report"] = str(report_path.name)
        overlap = report.get("overlap") or {}
        record["overlap_seconds"] = overlap.get("overlap_seconds")
        record["sufficient_overlap"] = overlap.get("sufficient_overlap")
        for side in ("a", "b"):
            data = overlap.get(side) or {}
            record[f"externality_{side}"] = data.get("externality")
            record[f"solo_p50_{side}"] = data.get("solo_p50_s")
            record[f"corun_p50_{side}"] = data.get("p50_s")
        headroom = report.get("memory_headroom") or {}
        record["memory_fits"] = headroom.get("fits")
        record["memory_required_bytes"] = headroom.get("required_bytes")
    else:
        record["status"] = "no_report"
        record["stderr_tail"] = proc.stderr[-1500:]
    return record


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--steps", type=int, default=20)
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--co-run-seconds", type=float, default=180.0)
    parser.add_argument("--out", required=True)
    parser.add_argument("--report-dir",
                        default=str(REPO / "experiments/runs"))
    args = parser.parse_args()

    out_dir = Path(args.report_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = []
    for entry in DEFAULT_PAIRS:
        row = run_pair(entry, args, out_dir)
        rows.append(row)
        if row.get("externality_a") is not None:
            print(f"  {row['label']:20s} {row['units_a']:2d}+{row['units_b']:2d} "
                  f"-> a {row['externality_a']*100:+6.1f}%  "
                  f"b {row['externality_b']*100:+6.1f}%  "
                  f"overlap {row['overlap_seconds']:.0f}s", flush=True)
        else:
            print(f"  {row['label']:20s} {row['units_a']:2d}+{row['units_b']:2d} "
                  f"-> {row.get('status')}"
                  + (f" (needs {row['memory_required_bytes']/1e9:.1f} GB)"
                     if row.get("memory_required_bytes") else ""), flush=True)

    measured = [r for r in rows if r.get("externality_a") is not None]
    report = {
        "schema_version": SCHEMA_VERSION,
        "pairs": rows,
        "measured": len(measured),
        "attempted": len(rows),
    }
    if measured:
        # Whether the penalty depends on how the die is split, which is
        # what decides if a scheduler can carry one coefficient.
        values = [v for r in measured
                  for v in (r["externality_a"], r["externality_b"])]
        report["externality_range"] = {"min": min(values), "max": max(values)}
        print(f"\nexternality across {len(measured)} pairs: "
              f"{min(values)*100:+.1f}% to {max(values)*100:+.1f}%")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(canonical_json(report) + "\n")
    print(f"table: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
