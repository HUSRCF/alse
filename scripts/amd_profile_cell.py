"""Run one AMD profiling cell and report its latency distribution.

One cell is one process with a fixed CU quota in ``ROC_GLOBAL_CU_MASK``,
because that is the only masking route verified to reach PyTorch. The cell
reports the coefficient of variation it actually achieved rather than assuming
the sample count in the plan is enough: a single-card screen on the NVIDIA
side already showed 5.5% CV at 22 samples, above the 5% Gate B threshold.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time


def measure(fn, *, warmup: int, samples: int, sync) -> list[float]:
    for _ in range(warmup):
        fn()
    sync()
    out = []
    for _ in range(samples):
        sync()
        started = time.perf_counter()
        fn()
        sync()
        out.append(time.perf_counter() - started)
    return out


def synthetic(torch, device, batch: int):
    """A dense fp16 matmul chain: deterministic, and it saturates the die."""
    torch.manual_seed(0)
    a = torch.randn(batch * 1024, 4096, device=device, dtype=torch.float16)
    b = torch.randn(4096, 4096, device=device, dtype=torch.float16)

    def step():
        x = a
        for _ in range(4):
            x = torch.mm(x, b)
        return x

    return step, {"flops_per_step": 2 * (batch * 1024) * 4096 * 4096 * 4}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="synthetic")
    parser.add_argument("--batch", type=int, default=1)
    parser.add_argument("--steps", type=int, default=1)
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--target-cv", type=float, default=0.05)
    parser.add_argument("--max-samples", type=int, default=300)
    args = parser.parse_args()

    import torch

    device = torch.device("cuda")
    mask = os.environ.get("ROC_GLOBAL_CU_MASK")
    units = bin(int(mask, 0)).count("1") if mask else None

    if args.model == "synthetic":
        step, extra = synthetic(torch, device, args.batch)
    else:
        print(json.dumps({"status": "unsupported_model", "model": args.model}))
        return 2

    sync = torch.cuda.synchronize
    samples = measure(step, warmup=args.warmup, samples=args.samples, sync=sync)

    # Escalate rather than report a CV that misses the gate: the plan requires
    # each cell to record the CV it reached, and to raise the sample count when
    # it does not meet the threshold.
    escalations = 0
    while True:
        mean = statistics.mean(samples)
        cv = statistics.stdev(samples) / mean if len(samples) > 1 and mean else float("inf")
        if cv <= args.target_cv or len(samples) >= args.max_samples:
            break
        samples += measure(step, warmup=0, samples=len(samples), sync=sync)
        escalations += 1

    samples.sort()
    pick = lambda q: samples[min(len(samples) - 1, int(q * len(samples)))]
    record = {
        "status": "ok",
        "model": args.model,
        "batch": args.batch,
        "cu_mask": mask,
        "units": units,
        "samples": len(samples),
        "escalations": escalations,
        "p50_s": pick(0.50),
        "p95_s": pick(0.95),
        "p99_s": pick(0.99),
        "mean_s": statistics.mean(samples),
        "cv": statistics.stdev(samples) / statistics.mean(samples),
        "meets_cv_threshold": (
            statistics.stdev(samples) / statistics.mean(samples)
        ) <= args.target_cv,
        "peak_memory_bytes": torch.cuda.max_memory_allocated(),
        "torch": torch.__version__,
        "device_name": torch.cuda.get_device_name(0),
        **extra,
    }
    record["tflops"] = record["flops_per_step"] / record["p50_s"] / 1e12
    print(json.dumps(record))
    return 0


if __name__ == "__main__":
    sys.exit(main())
