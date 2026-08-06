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
# rocprofv3 7.x does NOT emit a size column for memory copies -- the row is
# Kind, Direction, Stream_Id, agents, Correlation_Id, Start_Timestamp,
# End_Timestamp. These names are kept for versions that do, but their
# absence is reported, never treated as zero bytes: an unread size column
# would make every run pass the zero-traffic clause for the wrong reason.
SIZE_KEYS = ("Size", "SIZE", "Bytes", "BYTES", "Size (B)")
START_KEYS = ("Start_Timestamp", "START_TIMESTAMP", "Start", "start_ns")
END_KEYS = ("End_Timestamp", "END_TIMESTAMP", "End", "end_ns")
HOST_TO_DEVICE = ("HOST_TO_DEVICE", "HtoD", "HOST_TO_DEVICE_COPY",
                  "MEMORY_COPY_HOST_TO_DEVICE")


def _column_of(fields, candidates) -> str | None:
    for key in candidates:
        if key in fields:
            return key
    return None


def parse_copy_trace(directory: Path) -> dict:
    """Summarise host-to-device copies across every memory-copy CSV.

    Returns bytes when the profiler reports them and ``None`` when it does
    not, alongside the copy count and the total time the copies occupied.
    Duration is the fallback quantity because it bounds the bytes: at any
    achievable bandwidth, a transfer that occupied t seconds moved at most
    t * bandwidth. A bound is a weaker claim than a measurement, and it is
    reported as such, but it is a real one -- unlike a zero produced by
    reading a column that is not there.
    """
    files = sorted(directory.rglob("*memory_copy*.csv"))
    if not files:
        return {"found": False, "files": [], "host_to_device_bytes": None,
                "host_to_device_count": None,
                "host_to_device_nanoseconds": None}
    total_bytes = 0
    size_column_present = False
    count = 0
    nanoseconds = 0
    rows_seen = 0
    timing_column_present = False
    unmatched_directions: set[str] = set()
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        # From the header, not from a matched row. Whether the profiler can
        # report sizes is a property of the format; deciding it from the
        # rows would make a trace with no host-to-device copies at all look
        # like one whose sizes could not be read.
        fields = reader.fieldnames or []
        direction_key = _column_of(fields, DIRECTION_KEYS)
        size_key = _column_of(fields, SIZE_KEYS)
        start_key = _column_of(fields, START_KEYS)
        end_key = _column_of(fields, END_KEYS)
        if size_key is not None:
            size_column_present = True
        if start_key is not None and end_key is not None:
            timing_column_present = True
        for row in reader:
            rows_seen += 1
            if direction_key is None:
                continue
            direction = (row[direction_key] or "").strip()
            if not any(token in direction for token in HOST_TO_DEVICE):
                unmatched_directions.add(direction)
                continue
            count += 1
            if size_key is not None:
                try:
                    total_bytes += int(float(row[size_key]))
                except (TypeError, ValueError):
                    pass
            if start_key is not None and end_key is not None:
                try:
                    nanoseconds += int(row[end_key]) - int(row[start_key])
                except (TypeError, ValueError):
                    pass
    return {
        "found": True,
        "files": [str(p.relative_to(directory)) for p in files],
        "rows": rows_seen,
        "size_column_present": size_column_present,
        # None, not 0: the profiler did not report bytes, which is not the
        # same as having reported none.
        "host_to_device_bytes": total_bytes if size_column_present else None,
        "host_to_device_count": count,
        "host_to_device_nanoseconds": nanoseconds if timing_column_present else None,
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
    if args.vae_tiling:
        argv.append("--vae-tiling")
    # Run from outside the repository: rocprofv3 drops a .rocprofv3 directory
    # into its working directory, and an untracked path inside the bound tree
    # makes the next run refuse to bind -- a profiler should not be able to
    # invalidate the provenance of the thing it is profiling.
    proc = subprocess.run(argv, env=env, capture_output=True, text=True,
                          cwd=str(workdir))
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
    # rocprofv3 7.x reports no copy sizes, only timestamps. Supplying a
    # measured bandwidth lets the copy durations bound the bytes.
    parser.add_argument("--bound-bandwidth-bps", type=float, default=0.0)
    parser.add_argument("--vae-tiling", action="store_true")
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
    weights = next(
        (r["workload"].get("weight_bytes") for r in runs
         if r.get("workload") and r["workload"].get("weight_bytes")),
        None,
    )
    good = [r for r in runs if r["returncode"] == 0 and r["trace"]["found"]]
    report["weight_bytes"] = weights

    if len(good) < 2 or not weights:
        report["status"] = "incomplete"
        print("\nincomplete: could not fit (missing traces or weight size)")
    elif all(r["trace"]["host_to_device_bytes"] is not None for r in good):
        # The profiler reported bytes: measure them directly.
        fit = fit_slope([(r["rotations"], r["trace"]["host_to_device_bytes"])
                         for r in good])
        tolerance = weights * args.tolerance_fraction_of_weights
        report.update({
            "status": "ok",
            "basis": "measured_bytes",
            "fit": fit,
            "tolerance_bytes": tolerance,
            "zero_weight_traffic": fit["bytes_per_rotation"] <= tolerance,
        })
        print(f"\nweights {weights/1e9:.2f} GB   per-rotation HtoD "
              f"{fit['bytes_per_rotation']/1e6:.2f} MB   tolerance "
              f"{tolerance/1e6:.2f} MB   "
              f"zero_weight_traffic={report['zero_weight_traffic']}")
    elif all(r["trace"]["host_to_device_nanoseconds"] is not None
             for r in good):
        # No size column. Bound the bytes by the time the copies occupied:
        # at any achievable bandwidth, t seconds of copying moves at most
        # t * bandwidth. Weaker than a measurement, and labelled as such.
        if not args.bound_bandwidth_bps:
            report["status"] = "incomplete"
            report["basis"] = "duration_bound_without_bandwidth"
            print("\nincomplete: the profiler reports no copy sizes, so the "
                  "bytes can only be bounded, and --bound-bandwidth-bps was "
                  "not supplied")
        else:
            fit = fit_slope(
                [(r["rotations"], r["trace"]["host_to_device_nanoseconds"])
                 for r in good])
            count_fit = fit_slope(
                [(r["rotations"], r["trace"]["host_to_device_count"])
                 for r in good])
            ns = fit["bytes_per_rotation"]
            upper = max(0.0, ns) / 1e9 * args.bound_bandwidth_bps
            tolerance = weights * args.tolerance_fraction_of_weights
            report.update({
                "status": "ok",
                "basis": "duration_upper_bound",
                "bound_bandwidth_bps": args.bound_bandwidth_bps,
                "nanoseconds_per_rotation": ns,
                "copies_per_rotation": count_fit["bytes_per_rotation"],
                "upper_bound_bytes_per_rotation": upper,
                "tolerance_bytes": tolerance,
                "zero_weight_traffic": upper <= tolerance,
            })
            print(f"\nweights {weights/1e9:.2f} GB   per-rotation HtoD "
                  f"{count_fit['bytes_per_rotation']:.2f} copies occupying "
                  f"{ns/1e6:.3f} ms -> at most {upper/1e6:.2f} MB   "
                  f"tolerance {tolerance/1e6:.2f} MB   "
                  f"zero_weight_traffic={report['zero_weight_traffic']}")
    else:
        report["status"] = "incomplete"
        report["basis"] = "no_usable_quantity"
        print("\nincomplete: the trace carries neither sizes nor timestamps")

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
