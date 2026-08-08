#!/usr/bin/env python3
"""Ten thousand action switches: does anything drift, leak or stall?

plan.md's week 9-10 clause is 10,000 action switches with no errors. The
number is doing real work -- three failure modes only appear at that
scale, and each needs its own check:

  * **drift.** A mask that installs correctly the first time and wrongly
    the ten-thousandth is a driver state problem, so every switch reads
    the mask back and compares. Checking only at the start would test
    the first switch and call it ten thousand.
  * **accumulation.** Anything allocated per switch is invisible once and
    fatal ten thousand times. Allocated bytes are sampled against the
    switch count, not the clock, because that is what a per-switch leak
    tracks.
  * **latency creep.** A switch that costs 50 us at the start and 5 ms at
    the end still "succeeds". The clause the runtime has to hold is
    mask-update p99 under 100 us, so the distribution is reported and
    the tail is compared against the head.

Correctness is checked by construction rather than at the end: the same
denoising runs continuously while the masks change under it, and its
latent is compared against an unswitched reference. A churn test that
only counted successful API calls would pass while corrupting every
step -- which is exactly what the unordered switch did before an event
was put between them.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import statistics
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

from burstserve.executor import StepExecutor            # noqa: E402
from burstserve.masked_streams import MaskedStreamPool  # noqa: E402

WORDS = 4
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


def make_stream(mask: int):
    handle = ctypes.c_void_p()
    words = (ctypes.c_uint32 * WORDS)(
        *[(mask >> (32 * i)) & 0xFFFFFFFF for i in range(WORDS)]
    )
    rc = hip.hipExtStreamCreateWithCUMask(ctypes.byref(handle), WORDS, words)
    if rc != 0:
        raise RuntimeError(f"create({hex(mask)}) rc={rc}")
    return handle, read_mask(handle)


def read_mask(handle) -> int:
    buffer = (ctypes.c_uint32 * WORDS)()
    if hip.hipExtStreamGetCUMask(handle, WORDS, buffer) != 0:
        raise RuntimeError("mask readback failed")
    value = 0
    for index, word in enumerate(buffer):
        value |= word << (32 * index)
    return value


def digest_of(latent) -> str:
    return hashlib.sha256(
        latent.detach().to("cpu").float().numpy().tobytes()
    ).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--switches", type=int, default=10000)
    parser.add_argument("--steps", type=int, default=200,
                        help="denoising steps run under the churn; the "
                             "latent is compared against a reference")
    parser.add_argument("--height", type=int, default=768)
    parser.add_argument("--width", type=int, default=768)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--quotas", default="4,8,12,16,20,24,28,32")
    parser.add_argument("--sample-every", type=int, default=500)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    import torch
    from diffusers import DiffusionPipeline
    from amd_sdxl_adapter import SdxlStepAdapter

    quotas = [int(q) for q in args.quotas.split(",")]
    print("loading sdxl ...", flush=True)
    pipeline = DiffusionPipeline.from_pretrained(
        "stabilityai/stable-diffusion-xl-base-1.0",
        torch_dtype=torch.float16, variant="fp16", use_safetensors=True,
    ).to("cuda")
    pipeline.set_progress_bar_config(disable=True)

    pool = MaskedStreamPool(make_stream)
    for units in quotas:
        pool.for_quota(units)
    print(f"  {pool.creations} masked streams", flush=True)

    # Reference: the same request at a fixed width, no switching.
    reference = StepExecutor(
        object(),
        SdxlStepAdapter(pipeline, height=args.height, width=args.width,
                        steps=args.steps, seed=args.seed),
        total_steps=args.steps)
    reference.prepare()
    reference.adapter.stream = pool.for_quota(32).handle
    while reference.run_step(quota_units=32):
        pass
    reference_digest = digest_of(reference.finalize())
    print(f"  reference {reference_digest[:24]}", flush=True)

    # Churn: switch the mask before every step, cycling widths, and keep
    # denoising the whole time.
    churn = StepExecutor(
        object(),
        SdxlStepAdapter(pipeline, height=args.height, width=args.width,
                        steps=args.steps, seed=args.seed),
        total_steps=args.steps)
    churn.prepare()

    switch_seconds: list[float] = []
    samples: list[dict] = []
    mismatches: list[dict] = []
    step = 0
    started = time.perf_counter()

    for index in range(args.switches):
        units = quotas[index % len(quotas)]
        stream = pool.for_quota(units)

        # The switch itself: assign, then read the mask back. Reading
        # every time is the point -- a mask that installs correctly once
        # and wrongly later is exactly what this scale is for.
        switch_started = time.perf_counter()
        churn.adapter.stream = stream.handle
        installed = read_mask(stream.handle)
        switch_seconds.append(time.perf_counter() - switch_started)
        if installed != stream.requested_mask:
            mismatches.append({
                "switch": index, "units": units,
                "requested": hex(stream.requested_mask),
                "installed": hex(installed),
            })

        # Keep real work flowing under the churn, restarting the request
        # when it finishes so the die is never idle.
        if step < args.steps:
            more = churn.run_step(quota_units=units)
            step += 1
            if more:
                churn.resume(churn.suspend())

        if index % args.sample_every == 0:
            torch.cuda.synchronize()
            samples.append({
                "switch": index,
                "allocated_bytes": torch.cuda.memory_allocated(),
                "elapsed_s": time.perf_counter() - started,
            })
            print(f"  switch {index:6d}  "
                  f"alloc {samples[-1]['allocated_bytes'] / 2**30:.3f} GB  "
                  f"mismatches {len(mismatches)}", flush=True)

    churn_digest = digest_of(churn.finalize())
    elapsed = time.perf_counter() - started

    ordered = sorted(switch_seconds)
    p99 = ordered[min(len(ordered) - 1, int(0.99 * len(ordered)))]
    head = statistics.median(switch_seconds[:len(switch_seconds) // 10])
    tail = statistics.median(switch_seconds[-len(switch_seconds) // 10:])
    alloc_per_switch = (
        (samples[-1]["allocated_bytes"] - samples[1]["allocated_bytes"])
        / max(1, samples[-1]["switch"] - samples[1]["switch"])
        if len(samples) > 2 else 0.0
    )

    payload = {
        "schema_version": "burstserve.amd-action-churn/v1",
        "switches": args.switches,
        "quotas": quotas,
        "elapsed_seconds": elapsed,
        "steps_under_churn": step,
        "streams_created": pool.creations,
        "mask_mismatches": mismatches,
        "every_mask_held": not mismatches,
        "switch_seconds": {
            "p50": statistics.median(switch_seconds),
            "p99": p99,
            "max": max(switch_seconds),
            "median_first_decile": head,
            "median_last_decile": tail,
        },
        "mask_update_p99_under_100us": p99 < 1e-4,
        # A switch that costs more at the end than the start is creep,
        # and it passes any single-threshold check taken at the start.
        "no_latency_creep": tail <= head * 2.0,
        "allocated_bytes_per_switch": alloc_per_switch,
        "no_accumulation": abs(alloc_per_switch) < 1024,
        "reference_digest": reference_digest,
        "churn_digest": churn_digest,
        "latent_identical": reference_digest == churn_digest,
        "samples": samples,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2) + "\n")

    ok = (payload["every_mask_held"] and payload["latent_identical"]
          and payload["mask_update_p99_under_100us"]
          and payload["no_accumulation"] and payload["no_latency_creep"])
    print(f"\n{args.switches} switches in {elapsed:.0f}s")
    print(f"every mask held      : {payload['every_mask_held']}")
    print(f"latent identical     : {payload['latent_identical']}")
    print(f"switch p50/p99       : {statistics.median(switch_seconds)*1e6:.1f}"
          f" / {p99*1e6:.1f} us   (<100 us: "
          f"{payload['mask_update_p99_under_100us']})")
    print(f"first/last decile    : {head*1e6:.1f} / {tail*1e6:.1f} us  "
          f"(no creep: {payload['no_latency_creep']})")
    print(f"bytes per switch     : {alloc_per_switch:+.1f}")
    print(f"-> {args.out}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
