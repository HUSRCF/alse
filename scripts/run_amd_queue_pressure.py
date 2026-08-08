#!/usr/bin/env python3
"""Does holding masked streams make a co-run slower?

Two co-run anomalies in this project share one signature: solo unchanged,
co-run inflated.

  * a five-repeat campaign read externality 2.15, 2.10, 2.04 and then
    1.54, 1.57, with a five-hour-old hung process resident on the card.
  * a campaign that measured five splits in one process read 16+16 at
    1.79 and 1.82, where a campaign measuring only 16+16 read 1.54 --
    with solo at 16 units agreeing to within 1% (162.9 vs 161.6 ms).

The candidate both point at is the number of live queues on the device.
``MaskedStreamPool`` creates one stream per distinct mask and never
destroys it -- destroying between quotas hung a measurement for 2.5
hours -- so a process that measures many splits accumulates streams, and
a stale process holds its own. AMD's hardware scheduler multiplexes when
more queues are live than there are slots, and multiplexing costs most
where two queues are actually contending: a solo run has one queue in
flight and a co-run has two.

**Stated before running, so the result cannot be read to fit.** If queue
pressure is the mechanism, co-run p50 at 16+16 rises monotonically with
the number of live masked streams while solo p50 at 16 and 32 units stays
within its noise. If co-run is flat in the stream count, the mechanism is
something else and the externality table cannot be rebuilt until that
something else is found -- the wrong move would be to take whichever
number is convenient.

Streams are made live rather than merely created: a HIP stream may not be
given a hardware queue until work is submitted on it, so each extra
stream gets a trivial kernel. A test that only constructed them could
report "no effect" while never having created the condition.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import statistics
import sys
import threading
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
    if hip.hipExtStreamCreateWithCUMask(ctypes.byref(handle), WORDS,
                                        words) != 0:
        raise RuntimeError(f"could not create a stream for {hex(mask)}")
    buffer = (ctypes.c_uint32 * WORDS)()
    if hip.hipExtStreamGetCUMask(handle, WORDS, buffer) != 0:
        raise RuntimeError("mask readback failed")
    installed = 0
    for index, word in enumerate(buffer):
        installed |= word << (32 * index)
    return handle, installed


def solo(pipeline, pool, units, args, seed) -> list[float]:
    from amd_sdxl_adapter import SdxlStepAdapter

    adapter = SdxlStepAdapter(pipeline, height=args.height, width=args.width,
                              steps=args.steps, seed=seed)
    adapter.stream = pool.for_quota(units).handle
    executor = StepExecutor(object(), adapter, total_steps=args.steps)
    executor.prepare()
    seen: list[float] = []
    while executor.run_step(quota_units=units):
        if adapter.last_step_seconds:
            seen.append(adapter.last_step_seconds)
    adapter.drain_timing()
    if adapter.last_step_seconds:
        seen.append(adapter.last_step_seconds)
    return seen[args.warmup:]


def pair(pipeline, pool, args, seed) -> list[float]:
    from amd_sdxl_adapter import SdxlStepAdapter

    left, right = pool.disjoint_pair(16, 16)
    out: dict[str, list[float]] = {"a": [], "b": []}
    barrier = threading.Barrier(2)
    prepared = {}
    for name, stream, offset in (("a", left, 0), ("b", right, 1)):
        adapter = SdxlStepAdapter(pipeline, height=args.height,
                                  width=args.width, steps=args.steps,
                                  seed=seed + offset)
        adapter.stream = stream.handle
        executor = StepExecutor(object(), adapter, total_steps=args.steps)
        executor.prepare()
        prepared[name] = (adapter, executor)

    def side(name):
        adapter, executor = prepared[name]
        executor.run_step(quota_units=16)
        barrier.wait()
        for _ in range(args.steps - 1):
            executor.run_step(quota_units=16)
            if adapter.last_step_seconds:
                out[name].append(adapter.last_step_seconds)
        adapter.drain_timing()
        if adapter.last_step_seconds:
            out[name].append(adapter.last_step_seconds)

    threads = [threading.Thread(target=side, args=(n,)) for n in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return out["a"][args.warmup:] + out["b"][args.warmup:]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--steps", type=int, default=14)
    parser.add_argument("--warmup", type=int, default=4)
    parser.add_argument("--height", type=int, default=768)
    parser.add_argument("--width", type=int, default=768)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--levels", default="0,4,8,16,28",
                        help="extra live masked streams held at each point")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    import torch
    from diffusers import DiffusionPipeline
    from amd_sdxl_adapter import SdxlStepAdapter

    print("loading sdxl ...", flush=True)
    pipeline = DiffusionPipeline.from_pretrained(
        "stabilityai/stable-diffusion-xl-base-1.0",
        torch_dtype=torch.float16, variant="fp16", use_safetensors=True,
    ).to("cuda")
    pipeline.set_progress_bar_config(disable=True)

    pool = MaskedStreamPool(make_stream)
    # Kernel compilation is a property of the process, not of the quota:
    # the first width used once read 1773 ms for an 8-unit step. Pay it
    # for every width before anything is measured.
    print("warming ...", flush=True)
    for width in (16, 32):
        adapter = SdxlStepAdapter(pipeline, height=args.height,
                                  width=args.width, steps=3, seed=99991)
        adapter.stream = pool.for_quota(width).handle
        executor = StepExecutor(object(), adapter, total_steps=3)
        executor.prepare()
        for _ in range(3):
            if not executor.run_step(quota_units=width):
                break
        adapter.drain_timing()
    pool.disjoint_pair(16, 16)

    baseline_streams = pool.live
    held: list[object] = []
    points = []
    for level in [int(x) for x in args.levels.split(",")]:
        # Single-bit masks are distinct from every mask the measurement
        # uses, so holding them adds queues without changing which units
        # the measured work runs on.
        while len(held) < level:
            bit = len(held)
            stream = pool.acquire(1 << bit)
            # Submit work so the queue is really allocated. A stream that
            # never ran might hold nothing, and a test that only
            # constructed streams could report no effect while never
            # having created the condition it is testing.
            with torch.cuda.stream(torch.cuda.ExternalStream(
                    stream.handle.value)):
                torch.empty(4096, device="cuda").add_(1.0)
            held.append(stream)
        torch.cuda.synchronize()

        began = time.perf_counter()
        s16 = solo(pipeline, pool, 16, args, args.seed)
        s32 = solo(pipeline, pool, 32, args, args.seed)
        co = pair(pipeline, pool, args, args.seed)
        point = {
            "extra_live_streams": level,
            "total_live_streams": pool.live,
            "solo16_p50_s": statistics.median(s16),
            "solo32_p50_s": statistics.median(s32),
            "corun_p50_s": statistics.median(co),
            "externality": statistics.median(co) / statistics.median(s16),
            "elapsed_s": time.perf_counter() - began,
        }
        points.append(point)
        print(f"  +{level:3d} streams (live {pool.live:3d})  "
              f"solo16 {point['solo16_p50_s'] * 1000:7.1f}  "
              f"solo32 {point['solo32_p50_s'] * 1000:7.1f}  "
              f"corun {point['corun_p50_s'] * 1000:7.1f}  "
              f"ext {point['externality']:6.3f}", flush=True)

    first, last = points[0], points[-1]
    payload = {
        "schema_version": "burstserve.amd-queue-pressure/v1",
        "question": ("does holding live masked streams inflate a co-run "
                     "while leaving solo alone"),
        "criterion_stated_before_running": (
            "if queue pressure is the mechanism, corun p50 rises "
            "monotonically with live streams while solo16 and solo32 stay "
            "within noise; if corun is flat the mechanism is something "
            "else and the externality table cannot be rebuilt yet"),
        "workpoint": f"{args.width}x{args.height}",
        "steps": args.steps,
        "warmup_dropped": args.warmup,
        "baseline_live_streams": baseline_streams,
        "points": points,
        "corun_change": last["corun_p50_s"] / first["corun_p50_s"] - 1,
        "solo16_change": last["solo16_p50_s"] / first["solo16_p50_s"] - 1,
        "solo32_change": last["solo32_p50_s"] / first["solo32_p50_s"] - 1,
        "corun_monotone": all(
            b["corun_p50_s"] >= a["corun_p50_s"] * 0.98
            for a, b in zip(points, points[1:])),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2) + "\n")

    print()
    print(f"corun  {payload['corun_change'] * 100:+6.1f}%   "
          f"solo16 {payload['solo16_change'] * 100:+6.1f}%   "
          f"solo32 {payload['solo32_change'] * 100:+6.1f}%   "
          f"monotone={payload['corun_monotone']}")
    print(f"-> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
