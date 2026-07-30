#!/usr/bin/env python3
"""Measure pinned-memory H2D, D2H, and simultaneous bidirectional copies."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

import torch


def measure(copy_fn, reps: int, warmups: int) -> list[float]:
    samples = []
    for iteration in range(reps + warmups):
        torch.cuda.synchronize()
        start = time.perf_counter()
        copy_fn()
        torch.cuda.synchronize()
        elapsed = time.perf_counter() - start
        if iteration >= warmups:
            samples.append(elapsed)
    return samples


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gpu", type=int, default=0)
    parser.add_argument("--size-mib", type=int, default=1024)
    parser.add_argument("--reps", type=int, default=8)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    torch.cuda.set_device(args.gpu)
    device = torch.device("cuda", args.gpu)
    size_bytes = args.size_mib * 1024 * 1024

    host_in = torch.empty(size_bytes, dtype=torch.uint8, pin_memory=True)
    host_out = torch.empty(size_bytes, dtype=torch.uint8, pin_memory=True)
    gpu_in = torch.empty(size_bytes, dtype=torch.uint8, device=device)
    gpu_out = torch.empty(size_bytes, dtype=torch.uint8, device=device)
    h2d_stream = torch.cuda.Stream(device=device)
    d2h_stream = torch.cuda.Stream(device=device)

    def h2d() -> None:
        with torch.cuda.stream(h2d_stream):
            gpu_in.copy_(host_in, non_blocking=True)

    def d2h() -> None:
        with torch.cuda.stream(d2h_stream):
            host_out.copy_(gpu_out, non_blocking=True)

    def duplex() -> None:
        h2d()
        d2h()

    h2d_samples = measure(h2d, args.reps, args.warmups)
    d2h_samples = measure(d2h, args.reps, args.warmups)
    duplex_samples = measure(duplex, args.reps, args.warmups)
    size_gb = size_bytes / 1e9

    def median_ms(samples: list[float]) -> float:
        return statistics.median(samples) * 1e3

    result = {
        "gpu": torch.cuda.get_device_name(device),
        "torch": torch.__version__,
        "bytes_per_direction": size_bytes,
        "h2d_median_ms": round(median_ms(h2d_samples), 3),
        "h2d_gbps": round(size_gb / statistics.median(h2d_samples), 4),
        "d2h_median_ms": round(median_ms(d2h_samples), 3),
        "d2h_gbps": round(size_gb / statistics.median(d2h_samples), 4),
        "duplex_median_ms": round(median_ms(duplex_samples), 3),
        "duplex_aggregate_gbps": round(
            2.0 * size_gb / statistics.median(duplex_samples), 4
        ),
        "duplex_each_direction_gbps": round(
            size_gb / statistics.median(duplex_samples), 4
        ),
        "samples_ms": {
            "h2d": [round(x * 1e3, 3) for x in h2d_samples],
            "d2h": [round(x * 1e3, 3) for x in d2h_samples],
            "duplex": [round(x * 1e3, 3) for x in duplex_samples],
        },
    }

    rendered = json.dumps(result, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + "\n")
    print(rendered)


if __name__ == "__main__":
    main()
