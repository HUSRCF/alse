"""Power and throughput against quota, sampled from outside the load.

Two rules, both learned the hard way on 2026-08-05.

The sampler must not share a process with the code being timed. A thread
calling rocm-smi every 0.25 s inside the timing loop contends for the GIL,
and the interference scales with how slow the kernels are -- at 4 units it
collected 552 samples against 85 at 32, and manufactured a quota-dependent
efficiency trend that a kernel trace later showed did not exist. Here the
load runs in a child process and the parent samples it.

And the load must saturate. A four-deep matmul chain spills its
intermediates to memory and never reaches the power cap: 53.95 TFLOPS at
150 W, against 122.58 at 300 W for a single matmul of the same dtype. Any
efficiency conclusion drawn from the chain describes a bandwidth-bound
kernel rather than the card, which is how "24 units is the efficiency
optimum" was published and then withdrawn.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from burstserve.provenance import canonical_json  # noqa: E402

SCHEMA_VERSION = "burstserve.amd-power-sweep/v1"
MASKABLE_UNITS = 32


def read_power() -> float | None:
    try:
        out = subprocess.run(["rocm-smi", "--showpower"], capture_output=True,
                             text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.stdout.splitlines():
        if "Package Power" in line and ":" in line:
            try:
                return float(line.rsplit(":", 1)[1].strip())
            except ValueError:
                continue
    return None


class PowerSampler(threading.Thread):
    """Samples in the parent while the load runs in a child."""

    def __init__(self, interval: float):
        super().__init__(daemon=True)
        self.interval = interval
        self.samples: list[float] = []
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.is_set():
            value = read_power()
            if value is not None:
                self.samples.append(value)
            self._stop.wait(self.interval)

    def halt(self) -> None:
        self._stop.set()
        self.join(timeout=5)


def run_quota(units: int, args) -> dict:
    env = dict(os.environ)
    env["ROC_GLOBAL_CU_MASK"] = hex((1 << units) - 1)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    argv = [sys.executable, str(REPO / "scripts/amd_matmul_load.py"),
            "--size", str(args.size), "--seconds", str(args.seconds)]
    sampler = PowerSampler(args.sample_interval)
    proc = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True)
    # Start sampling only once the child is past its warmup, so idle time
    # during model setup does not drag the median down.
    time.sleep(args.settle_seconds)
    sampler.start()
    out, _ = proc.communicate()
    sampler.halt()
    body = [line for line in out.splitlines() if line.startswith("{")]
    record = json.loads(body[-1]) if body else {"status": "failed"}
    record.update({
        "units": units,
        "power_samples": len(sampler.samples),
        "power_median_w": statistics.median(sampler.samples)
        if sampler.samples else None,
        "power_peak_w": max(sampler.samples) if sampler.samples else None,
    })
    return record


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quotas", default="4,8,12,16,20,24,28,32")
    parser.add_argument("--size", type=int, default=4096)
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--settle-seconds", type=float, default=6.0)
    parser.add_argument("--sample-interval", type=float, default=0.25)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    rows = []
    for units in [int(q) for q in args.quotas.split(",") if q.strip()]:
        row = run_quota(units, args)
        rows.append(row)
        tflops = row.get("tflops")
        watts = row.get("power_median_w")
        print(f"  {units:2d} units  {tflops if tflops else 0:7.2f} TFLOPS  "
              f"{watts if watts else 0:5.0f} W  "
              f"{(tflops / watts) if tflops and watts else 0:6.3f} TFLOPS/W  "
              f"(n={row['power_samples']})", flush=True)

    good = [r for r in rows if r.get("tflops") and r.get("power_median_w")]
    report = {
        "schema_version": SCHEMA_VERSION,
        "matrix": args.size,
        "seconds_per_quota": args.seconds,
        "sampling": "parent process samples while load runs in a child",
        "rows": rows,
    }
    if good:
        best = max(good, key=lambda r: r["tflops"] / r["power_median_w"])
        report["most_efficient_units"] = best["units"]
        report["efficiency_range"] = {
            "min": min(r["tflops"] / r["power_median_w"] for r in good),
            "max": max(r["tflops"] / r["power_median_w"] for r in good),
        }
        # Whether the card sits on its cap regardless of quota, which is
        # what decides if partitioning saves any power at all.
        watts = [r["power_median_w"] for r in good]
        report["power_spread_w"] = max(watts) - min(watts)
        print(f"\nmost efficient at {best['units']} units; power spread "
              f"across quotas {max(watts) - min(watts):.0f} W")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(canonical_json(report) + "\n")
    print(f"report: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
