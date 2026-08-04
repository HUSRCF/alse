"""Measure a model load's transfer time directly, instead of by difference.

Every attempt to isolate the transfer with a constructed control failed,
and failed in a way that says the approach is wrong rather than the
construction. Replaying the same sizes out of one large host buffer
measured 0.351 s; replaying them out of separately allocated host tensors,
the way real weights are laid out, measured 0.493 s; and the real
``.to(device)`` sat between them at 0.454 s. Two defensible controls
disagree by 40% and neither brackets the truth, because ``.to()`` stages
and pipelines its copies in ways a naive loop does not reproduce.

So the transfer is read out of the profiler instead. rocprofv3's
memory-copy trace timestamps every host-to-device copy, and a process that
only loads a model performs no others, so the sum of those durations is
the transfer -- measured, not inferred, and with no control to disagree
with.

What remains, observed minus that sum, is the framework term.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

sys.path.insert(0, str(REPO / "scripts"))
from run_amd_residency import parse_copy_trace  # noqa: E402

from burstserve.provenance import canonical_json  # noqa: E402

SCHEMA_VERSION = "burstserve.amd-load-transfer/v1"


def run_once(args, workdir: Path, index: int) -> dict:
    output = workdir / f"load{index}"
    output.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    argv = [
        "rocprofv3", "--memory-copy-trace", "--output-format", "csv",
        "-d", str(output), "-o", "trace", "--",
        sys.executable, str(REPO / "scripts/amd_load_workload.py"),
        "--model", args.model,
        "--reloads", str(args.reloads),
    ]
    # From outside the repository: rocprofv3 writes .rocprofv3 into its
    # working directory, and an untracked path in the bound tree stops the
    # next run from binding its source.
    proc = subprocess.run(argv, env=env, capture_output=True, text=True,
                          cwd=str(workdir))
    body = [line for line in proc.stdout.splitlines() if line.startswith("{")]
    return {
        "returncode": proc.returncode,
        "workload": json.loads(body[-1]) if body else None,
        "trace": parse_copy_trace(output),
        "stderr_tail": proc.stderr[-1500:] if proc.returncode else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="sdxl")
    parser.add_argument("--reloads", type=int, default=4)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    workdir = Path(tempfile.mkdtemp(prefix="amd-load-"))
    run = run_once(args, workdir, 0)
    if run["returncode"] != 0 or not run["workload"]:
        print(f"load workload failed: {run['stderr_tail']}")
        return 1

    workload = run["workload"]
    trace = run["trace"]
    if trace["host_to_device_nanoseconds"] is None:
        print("the trace carries no timestamps; cannot measure the transfer")
        return 1

    # The workload reloads several times; the profiler sums across all of
    # them, so the per-load transfer is the total over the reload count.
    reloads = workload["reloads"]
    transfer_total = trace["host_to_device_nanoseconds"] / 1e9
    transfer_per_load = transfer_total / reloads

    observations = workload["to_device_seconds"]
    first_load = observations[0]
    warm = observations[1:] or observations
    warm_median = statistics.median(warm)

    report = {
        "schema_version": SCHEMA_VERSION,
        "model": args.model,
        "repo": workload["repo"],
        "tensors": workload["tensors"],
        "weight_bytes": workload["weight_bytes"],
        "reloads": reloads,
        "to_device_seconds": observations,
        "first_load_seconds": first_load,
        "warm_load_median_seconds": warm_median,
        "profiler": {
            "host_to_device_copies": trace["host_to_device_count"],
            "host_to_device_seconds_total": transfer_total,
            "transfer_seconds_per_load": transfer_per_load,
            "size_column_present": trace["size_column_present"],
        },
        # Transfer is measured; framework is what the wall clock has that
        # the copies do not account for.
        "framework_seconds_warm": warm_median - transfer_per_load,
        "framework_seconds_first": first_load - transfer_per_load,
        "framework_us_per_tensor_warm": (
            (warm_median - transfer_per_load) / workload["tensors"] * 1e6
        ),
        "transfer_share_warm": transfer_per_load / warm_median,
    }

    print(f"  {workload['tensors']} tensors, "
          f"{workload['weight_bytes']/1e9:.2f} GB, {reloads} reloads")
    print(f"  .to(device):  first {first_load:.3f}s   warm median "
          f"{warm_median:.3f}s   all {[round(x, 3) for x in observations]}")
    print(f"  profiler:     {trace['host_to_device_count']} HtoD copies, "
          f"{transfer_per_load:.3f}s per load "
          f"({report['transfer_share_warm']*100:.1f}% of warm wall clock)")
    print(f"  framework:    warm {report['framework_seconds_warm']:.3f}s "
          f"({report['framework_us_per_tensor_warm']:.1f} us/tensor)   "
          f"first {report['framework_seconds_first']:.3f}s")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(canonical_json(report) + "\n")
    print(f"report: {out}")
    import shutil

    shutil.rmtree(workdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
