#!/usr/bin/env python3
"""Does a per-stream CU mask actually constrain the whole pipeline?

plan.md chose two processes with ``ROC_GLOBAL_CU_MASK`` for Gate B's
co-run cells, and gave a reason: it needs no change to PyTorch's stream
management, "绕开了 per-stream 掩码与框架内部发射的耦合问题". A
process-wide mask binds every kernel the process launches. A per-stream
mask binds only the kernels that actually go to that stream, and a
framework that launches some work on the default stream would leave that
work unconstrained -- so the partition would be nominal.

Everything measured in-process on 2026-08-06 rests on the per-stream
mask, including the externality entries the simulator now uses and the
CogVideoX-2b co-run that Gate B could not take. Reading back the mask
with ``hipExtStreamGetCUMask`` proves the stream carries it; it does not
prove the pipeline's kernels went there.

This measures that directly. One process, one masked stream, one tenant,
per-step times from CUDA events -- the same quantity the two-process
quota curve holds. If the mask constrains the pipeline, the two curves
agree. If kernels escape to an unmasked stream, the in-process figures
are faster than the two-process ones, and by more at low quota, where
escaping to the whole die is worth most.

The comparison is per-step rather than per-call: a call includes a VAE
decode whose cost does not scale with quota the same way, which would
blur exactly the difference being looked for.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import statistics
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

WORDS = 4

# The two-process per-step curve for SDXL at 768x768, measured with
# ROC_GLOBAL_CU_MASK: experiments/probes/amd-r9700-cu-mask/
# step_ratio_sdxl_768_perstep_curve_20260806.json
TWO_PROCESS_CURVE = {
    4: 0.52141, 8: 0.26875, 12: 0.19350, 16: 0.15750,
    20: 0.13867, 24: 0.12493, 28: 0.12056, 32: 0.11552,
}

hip = ctypes.CDLL("libamdhip64.so")
hip.hipExtStreamCreateWithCUMask.restype = ctypes.c_int
hip.hipExtStreamCreateWithCUMask.argtypes = [
    ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint32,
    ctypes.POINTER(ctypes.c_uint32),
]
hip.hipExtStreamGetCUMask.restype = ctypes.c_int
hip.hipExtStreamGetCUMask.argtypes = [
    ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
]
hip.hipStreamDestroy.restype = ctypes.c_int
hip.hipStreamDestroy.argtypes = [ctypes.c_void_p]


def masked_stream(units: int):
    mask = (1 << units) - 1
    handle = ctypes.c_void_p()
    words = (ctypes.c_uint32 * WORDS)(
        *[(mask >> (32 * i)) & 0xFFFFFFFF for i in range(WORDS)]
    )
    rc = hip.hipExtStreamCreateWithCUMask(ctypes.byref(handle), WORDS, words)
    if rc != 0:
        raise RuntimeError(f"hipExtStreamCreateWithCUMask({units}) rc={rc}")
    buffer = (ctypes.c_uint32 * WORDS)()
    if hip.hipExtStreamGetCUMask(handle, WORDS, buffer) != 0:
        raise RuntimeError("readback failed")
    got = 0
    for index, word in enumerate(buffer):
        got |= word << (32 * index)
    if got != mask:
        raise RuntimeError(f"runtime installed {hex(got)} for {hex(mask)}")
    return handle, got


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="sdxl")
    parser.add_argument("--quotas", default="4,8,16,32")
    parser.add_argument("--height", type=int, default=768)
    parser.add_argument("--width", type=int, default=768)
    parser.add_argument("--steps", type=int, default=8)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--samples", type=int, default=8)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    import torch
    from diffusers import DiffusionPipeline

    if not hasattr(torch.cuda, "ExternalStream"):
        print(json.dumps({"status": "no_external_stream"}))
        return 2

    pipeline = DiffusionPipeline.from_pretrained(
        "stabilityai/stable-diffusion-xl-base-1.0",
        torch_dtype=torch.float16, variant="fp16", use_safetensors=True,
    ).to("cuda")
    pipeline.set_progress_bar_config(disable=True)

    events: list = []

    def on_step(pipe, index, timestep, kwargs):
        events.append(torch.cuda.Event(enable_timing=True))
        events[-1].record(torch.cuda.current_stream())
        return kwargs

    rows = []
    for units in [int(q) for q in args.quotas.split(",")]:
        handle, mask = masked_stream(units)
        stream = torch.cuda.ExternalStream(handle.value)
        call = {
            "prompt": "a quiet street at dusk",
            "num_inference_steps": args.steps,
            "height": args.height,
            "width": args.width,
            "generator": torch.Generator(device="cuda").manual_seed(args.seed),
            "callback_on_step_end": on_step,
        }

        per_step: list[float] = []
        with torch.cuda.stream(stream):
            for index in range(args.warmup + args.samples):
                events.clear()
                # A marker before the first step, so the first interval is
                # a step and not the setup that precedes it.
                events.append(torch.cuda.Event(enable_timing=True))
                events[0].record(stream)
                with torch.inference_mode():
                    pipeline(**call)
                torch.cuda.synchronize()
                if index < args.warmup:
                    continue
                per_step.extend(
                    events[i].elapsed_time(events[i + 1]) / 1000.0
                    for i in range(len(events) - 1)
                )
        hip.hipStreamDestroy(handle)

        ordered = sorted(per_step)
        measured = ordered[len(ordered) // 2] if ordered else None
        reference = TWO_PROCESS_CURVE.get(units)
        row = {
            "units": units,
            "mask": hex(mask),
            "per_step_p50_s": measured,
            "per_step_n": len(per_step),
            "cv": (statistics.stdev(per_step) / statistics.mean(per_step)
                   if len(per_step) > 1 else None),
            "two_process_per_step_s": reference,
        }
        if measured and reference:
            row["ratio_to_two_process"] = measured / reference
        rows.append(row)
        print(f"  {units:2d} units: in-process {measured * 1000:7.2f} ms  "
              f"two-process {reference * 1000:7.2f} ms  "
              f"ratio {row.get('ratio_to_two_process', 0):.4f}  "
              f"cv {row['cv'] * 100:.2f}%", flush=True)

    usable = [r for r in rows if "ratio_to_two_process" in r]
    verdict = {}
    if usable:
        ratios = [r["ratio_to_two_process"] for r in usable]
        low = min(usable, key=lambda r: r["units"])
        verdict = {
            "worst_ratio": max(ratios, key=lambda v: abs(v - 1.0)),
            "mean_ratio": statistics.mean(ratios),
            # Escaping kernels would help most where the mask is
            # tightest, so a mask that leaks shows up as the in-process
            # curve pulling ahead at low quota.
            "ratio_at_lowest_quota": low["ratio_to_two_process"],
            "lowest_quota": low["units"],
        }

    payload = {
        "schema_version": "burstserve.amd-stream-mask-penetration/v1",
        "question": ("does a per-stream CU mask constrain the whole "
                     "pipeline, or do framework-launched kernels escape "
                     "to an unmasked stream"),
        "arrangement": "one process, one masked stream, one tenant",
        "reference": ("two-process ROC_GLOBAL_CU_MASK per-step curve, "
                      "step_ratio_sdxl_768_perstep_curve_20260806.json"),
        "workpoint": f"{args.width}x{args.height}",
        "rows": rows,
        "verdict": verdict,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"\n-> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
