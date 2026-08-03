"""Show that a resident model's rotation moves no weight bytes.

Gate B requires zero weight traffic when the same model serves request
after request. On this card there is no hardware PCIe counter to read
(rsmi_dev_pci_throughput_get returns NOT_SUPPORTED and every PCIe field in
rocm-smi --showmetrics is N/A, 2026-08-03), so the evidence comes from
``rocprofv3 --memory-copy-trace``, which attributes copied bytes to the
process that copied them.

The measurement is a difference rather than a phase split. Splitting a
trace into "load" and "serve" needs the profiler's clock and the process's
clock to agree, and a misalignment would silently move bytes across the
boundary in whichever direction the alignment error points. Instead the
same program is run at several rotation counts and the bytes are regressed
against the count: the slope is the per-rotation cost and the intercept is
the one-off load. A resident model has a slope near zero, and "near zero"
is judged against the model's own weight size, not an absolute byte count.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# Before the first burstserve import: see the note in run_amd_gate_b.py.
sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from burstserve.provenance import canonical_json  # noqa: E402

SCHEMA_VERSION = "burstserve.amd-residency/v1"

# rocprofv3 names the direction column differently across versions; accept
# the ones seen rather than guessing one and silently matching nothing.
DIRECTION_KEYS = ("Direction", "DIRECTION", "Kind", "KIND", "Operation")
SIZE_KEYS = ("Size", "SIZE", "Bytes", "BYTES", "Size (B)")
HOST_TO_DEVICE = ("HOST_TO_DEVICE", "HtoD", "HOST_TO_DEVICE_COPY",
                  "MEMORY_COPY_HOST_TO_DEVICE")


def _column(row: dict, candidates) -> str | None:
    for key in candidates:
        if key in row:
            return key
    return None


def parse_copy_trace(directory: Path) -> dict:
    """Total host-to-device bytes across every memory-copy CSV in a run."""
    files = sorted(directory.rglob("*memory_copy*.csv"))
    if not files:
        return {"found": False, "files": [], "host_to_device_bytes": None}
    total = 0
    rows_seen = 0
    unmatched_directions: set[str] = set()
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            rows_seen += 1
            direction_key = _column(row, DIRECTION_KEYS)
            size_key = _column(row, SIZE_KEYS)
            if direction_key is None or size_key is None:
                continue
            direction = (row[direction_key] or "").strip()
            if not any(token in direction for token in HOST_TO_DEVICE):
                unmatched_directions.add(direction)
                continue
            try:
                total += int(float(row[size_key]))
            except (TypeError, ValueError):
                continue
    return {
        "found": True,
        "files": [str(p.relative_to(directory)) for p in files],
        "rows": rows_seen,
        "host_to_device_bytes": total,
        "other_directions_seen": sorted(unmatched_directions),
    }


def run_once(args, rotations: int, workdir: Path) -> dict:
    output = workdir / f"rot{rotations}"
    output.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if args.units:
        env["ROC_GLOBAL_CU_MASK"] = hex((1 << args.units) - 1)
    argv = [
        "rocprofv3", "--memory-copy-trace", "--output-format", "csv",
        "-d", str(output), "-o", "trace", "--",
        sys.executable, str(REPO / "scripts/amd_rotation_workload.py"),
        "--model", args.model,
        "--rotations", str(rotations),
        "--steps", str(args.steps),
        "--height", str(args.height),
        "--width", str(args.width),
        "--frames", str(args.frames),
    ]
    proc = subprocess.run(argv, env=env, capture_output=True, text=True)
    body = [line for line in proc.stdout.splitlines() if line.startswith("{")]
    record = {
        "rotations": rotations,
        "returncode": proc.returncode,
        "workload": json.loads(body[-1]) if body else None,
        "trace": parse_copy_trace(output),
    }
    if proc.returncode != 0:
        record["stderr_tail"] = proc.stderr[-2000:]
    return record


def fit_slope(points: list[tuple[int, int]]) -> dict:
    """Least squares of bytes against rotation count."""
    if len({r for r, _ in points}) < 2:
        raise ValueError("need at least two distinct rotation counts")
    n = len(points)
    mean_x = sum(r for r, _ in points) / n
    mean_y = sum(b for _, b in points) / n
    variance = sum((r - mean_x) ** 2 for r, _ in points)
    slope = sum((r - mean_x) * (b - mean_y) for r, b in points) / variance
    return {"bytes_per_rotation": slope, "load_bytes": mean_y - slope * mean_x}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="sdxl")
    parser.add_argument("--steps", type=int, default=20)
    parser.add_argument("--height", type=int, default=1024)
    parser.add_argument("--width", type=int, default=1024)
    parser.add_argument("--frames", type=int, default=49)
    parser.add_argument("--units", type=int, default=0,
                        help="0 leaves the die unmasked")
    parser.add_argument("--rotations", default="1,3,5")
    # Judged against the model's own weight size: "no weight traffic" is a
    # claim about weights, so the tolerance has to scale with them.
    parser.add_argument("--tolerance-fraction-of-weights", type=float,
                        default=0.01)
    parser.add_argument("--keep-traces", action="store_true")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    counts = [int(c) for c in args.rotations.split(",") if c.strip()]
    if len(set(counts)) < 2:
        print("need at least two distinct rotation counts", file=sys.stderr)
        return 2

    workdir = Path(tempfile.mkdtemp(prefix="amd-residency-"))
    runs = [run_once(args, count, workdir) for count in counts]
    for run in runs:
        trace = run["trace"]
        print(f"  rotations={run['rotations']} rc={run['returncode']} "
              f"HtoD={trace['host_to_device_bytes']} "
              f"rows={trace.get('rows')}", flush=True)

    report = {
        "schema_version": SCHEMA_VERSION,
        "model": args.model,
        "units": args.units or None,
        "runs": runs,
    }
    usable = [
        (r["rotations"], r["trace"]["host_to_device_bytes"])
        for r in runs
        if r["returncode"] == 0 and r["trace"]["host_to_device_bytes"] is not None
    ]
    weights = next(
        (r["workload"].get("weight_bytes") for r in runs
         if r.get("workload") and r["workload"].get("weight_bytes")),
        None,
    )
    if len(usable) >= 2 and weights:
        fit = fit_slope(usable)
        tolerance = weights * args.tolerance_fraction_of_weights
        report["fit"] = fit
        report["weight_bytes"] = weights
        report["tolerance_bytes"] = tolerance
        report["zero_weight_traffic"] = fit["bytes_per_rotation"] <= tolerance
        report["status"] = "ok"
        print(f"\nweights {weights/1e9:.2f} GB   "
              f"per-rotation HtoD {fit['bytes_per_rotation']/1e6:.2f} MB   "
              f"tolerance {tolerance/1e6:.2f} MB   "
              f"zero_weight_traffic={report['zero_weight_traffic']}")
    else:
        report["status"] = "incomplete"
        print("\nincomplete: could not fit (missing traces or weight size)")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(canonical_json(report) + "\n")
    print(f"report: {out}")
    if not args.keep_traces:
        import shutil

        shutil.rmtree(workdir, ignore_errors=True)
    else:
        print(f"traces kept in {workdir}")
    return 0 if report.get("zero_weight_traffic") else 1


if __name__ == "__main__":
    raise SystemExit(main())
