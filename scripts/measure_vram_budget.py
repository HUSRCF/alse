#!/usr/bin/env python3
"""Peak VRAM for each residency regime, so the budget tiers are measured.

plan.md's memory budget still carries the 4090's numbers -- 24 GB
native, 20/16 GB emulated -- and asks for three tiers on the R9700's
34.2 GB covering weights fully resident, barely resident, and having to
be swapped out. Its own clause is that the values be justified rather
than mechanically converted, which means they have to come from
somewhere. This is where.

Reports, per stage, both what torch has allocated and what the driver
says the process holds. They differ: the caching allocator keeps freed
blocks, so ``memory_allocated`` understates what the card is actually
unable to give to anyone else, and a budget set from it would be a
budget nobody can honour.
"""

from __future__ import annotations

import argparse
import gc
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

# Imported inside main(), not here: the co-run harness loads
# libamdhip64 at module scope, so a top-level import makes --help fail
# on any machine without ROCm -- including the one this is written on.


def driver_bytes() -> int | None:
    """What the driver says this GPU holds, across all processes.

    Read from rocm-smi rather than torch, because the budget a runtime
    has to respect is the card's, not the allocator's view of it.
    """
    try:
        out = subprocess.run(["rocm-smi", "--showmeminfo", "vram"],
                             capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        if "Used" in line and "VRAM" in line:
            token = line.strip().split()[-1]
            if token.isdigit():
                return int(token)
    return None


def snapshot(torch, label: str) -> dict:
    torch.cuda.synchronize()
    return {"stage": label,
            "torch_allocated_B": torch.cuda.memory_allocated(),
            "torch_reserved_B": torch.cuda.memory_reserved(),
            "torch_max_allocated_B": torch.cuda.max_memory_allocated(),
            "torch_max_reserved_B": torch.cuda.max_memory_reserved(),
            "driver_used_B": driver_bytes()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="sdxl,cogvideox-2b")
    ap.add_argument("--steps", type=int, default=8)
    ap.add_argument("--units", type=int, default=32)
    ap.add_argument("--maskable-units", type=int, default=32)
    ap.add_argument("--height", type=int, default=768)
    ap.add_argument("--width", type=int, default=768)
    ap.add_argument("--frames", type=int, default=49)
    ap.add_argument("--video-height", type=int, default=480)
    ap.add_argument("--video-width", type=int, default=720)
    ap.add_argument("--drop-text-encoders", action="store_true", default=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    import torch

    from burstserve.executor import StepExecutor
    from burstserve.masked_streams import MaskedStreamPool
    import run_amd_mismatched_corun as harness

    total = torch.cuda.get_device_properties(0).total_memory
    stages = [snapshot(torch, "process start")]
    names = [m.strip() for m in args.models.split(",") if m.strip()]
    pool = MaskedStreamPool(maskable_units=args.maskable_units)

    pipelines, adapters = {}, {}
    for model in names:
        pipelines[model] = harness.build_pipeline(
            model, drop_text_encoders=args.drop_text_encoders)
        stages.append(snapshot(torch, f"{model}: weights loaded"))
        adapters[model] = harness.make_adapter(model, pipelines[model],
                                               args, seed=0)
        released = harness.free_text_encoders(pipelines[model])
        stages.append(snapshot(torch, f"{model}: encoders dropped "
                                      f"({released / 2**30:.2f} GiB)"))

    # Steps, one model at a time: the peak here is weights plus one
    # tenant's activations.
    for model, adapter in adapters.items():
        harness.warm(adapter, pool, args.units, args)
        adapter.stream = pool.for_quota(args.units).handle
        executor = StepExecutor(object(), adapter, total_steps=args.steps)
        executor.prepare()
        for _ in range(args.steps):
            if not executor.run_step(quota_units=args.units):
                break
        adapter.drain_timing()
        stages.append(snapshot(torch, f"{model}: {args.steps} steps solo"))

    # Both tenants stepping on disjoint masks -- what every matrix cell
    # actually holds, and the number a "fully resident" tier has to clear.
    if len(names) == 2:
        left, right = names
        half = args.maskable_units // 2
        execs = {}
        for model, units in ((left, half), (right, args.maskable_units - half)):
            adapters[model].stream = pool.for_quota(units).handle
            execs[model] = StepExecutor(object(), adapters[model],
                                        total_steps=args.steps)
            execs[model].prepare()
        for _ in range(args.steps):
            for model, units in ((left, half),
                                 (right, args.maskable_units - half)):
                execs[model].run_step(quota_units=units)
        for model in names:
            adapters[model].drain_timing()
        stages.append(snapshot(torch, f"both resident, {half}+"
                                      f"{args.maskable_units - half} masked"))

    gc.collect()
    torch.cuda.empty_cache()
    stages.append(snapshot(torch, "after empty_cache"))

    peak = max(s["torch_max_reserved_B"] for s in stages)
    payload = {
        "schema_version": "burstserve.vram-budget/v1",
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "device": torch.cuda.get_device_name(0),
        "total_memory_B": total,
        "models": names,
        "steps": args.steps,
        "stages": stages,
        "peak_reserved_B": peak,
        "headroom_at_peak_B": total - peak,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2) + "\n")

    print(f"{torch.cuda.get_device_name(0)}  total "
          f"{total / 1e9:.2f} GB")
    for s in stages:
        drv = (f"{s['driver_used_B'] / 1e9:>7.2f}"
               if s["driver_used_B"] is not None else "      -")
        print(f"  {s['stage']:<46} alloc {s['torch_allocated_B'] / 1e9:>6.2f}"
              f"  reserved {s['torch_reserved_B'] / 1e9:>6.2f}"
              f"  peak-reserved {s['torch_max_reserved_B'] / 1e9:>6.2f}"
              f"  driver {drv}  GB")
    print(f"\npeak reserved {peak / 1e9:.2f} GB, headroom "
          f"{(total - peak) / 1e9:.2f} GB ({(total - peak) / total:.1%})")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
