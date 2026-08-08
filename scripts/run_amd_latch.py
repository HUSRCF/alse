#!/usr/bin/env python3
"""What draws the co-run state, and can a scheduler tell which it got?

The first co-run in a process costs 1.541x solo at 16+16, sd 0.043 over
twelve processes -- the most reproducible number this project has. Every
co-run after it lands on one of two plateaus, 1.149 or 1.73, and stays
there for the rest of the process. The verdict on partitioning follows
the plateau: +16.4% on one, -22.6% on the other.

A scheduler cannot use a constant against that. It can use a measurement,
which is what the dual ledger is for -- but only if four things are known,
and this measures all four:

  * **how often each state is drawn.** Two processes is two coin flips.
  * **whether it ever flips mid-process.** If it does, a scheduler that
    decides once is wrong later; if it does not, one decision per process
    is enough and the probe can be cheap.
  * **how many steps identify it.** ``probing_partitioning`` pays for its
    evidence in degraded steps, so a state that takes twenty steps to see
    is a state the policy cannot afford to look for. The per-step series
    is kept whole rather than reduced to a median, because the answer is
    in the first few steps and a median throws exactly those away.
  * **whether the latch is per mask-pair or per process.** Episodes 5 and
    9 run 8+24 instead, so a state that survives them is a property of
    the process and a state that resets is a property of the pairing.
    The five-split harness reading 1.808 at the same stream count as a
    1.149 process is the reason to ask.

Every episode is kept, including the first, and solo is remeasured before
each one so the externality is against a contemporaneous baseline rather
than against a solo taken 30 C ago.
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


def new_adapter(pipeline, args, seed):
    from amd_sdxl_adapter import SdxlStepAdapter

    return SdxlStepAdapter(pipeline, height=args.height, width=args.width,
                           steps=args.steps, seed=seed)


def solo(pipeline, pool, units, args, seed) -> list[float]:
    adapter = new_adapter(pipeline, args, seed)
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


def episode(pipeline, pool, units, args, seed):
    """One co-run, with every step of both sides kept in order."""
    left, right = pool.disjoint_pair(units[0], units[1])
    series: dict[str, list[float]] = {"a": [], "b": []}
    barrier = threading.Barrier(2)
    prepared = {}
    for name, stream, offset in (("a", left, 0), ("b", right, 1)):
        adapter = new_adapter(pipeline, args, seed + offset)
        adapter.stream = stream.handle
        executor = StepExecutor(object(), adapter, total_steps=args.steps)
        executor.prepare()
        prepared[name] = (adapter, executor)

    def side(name, quota):
        adapter, executor = prepared[name]
        # Warm before the barrier so neither side measures the other's
        # start-up. The step it costs is not in the series.
        executor.run_step(quota_units=quota)
        barrier.wait()
        for _ in range(args.steps - 1):
            executor.run_step(quota_units=quota)
            if adapter.last_step_seconds:
                series[name].append(adapter.last_step_seconds)
        adapter.drain_timing()
        if adapter.last_step_seconds:
            series[name].append(adapter.last_step_seconds)

    threads = [threading.Thread(target=side, args=(n, u))
               for n, u in (("a", units[0]), ("b", units[1]))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return series


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--steps", type=int, default=14)
    parser.add_argument("--warmup", type=int, default=4)
    parser.add_argument("--height", type=int, default=768)
    parser.add_argument("--width", type=int, default=768)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--episodes", type=int, default=12)
    parser.add_argument("--interpose", default="5,9",
                        help="episodes that run 8+24 instead of 16+16, to "
                             "test whether the latch is per pair or per "
                             "process")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    import torch
    from diffusers import DiffusionPipeline

    print("loading sdxl ...", flush=True)
    pipeline = DiffusionPipeline.from_pretrained(
        "stabilityai/stable-diffusion-xl-base-1.0",
        torch_dtype=torch.float16, variant="fp16", use_safetensors=True,
    ).to("cuda")
    pipeline.set_progress_bar_config(disable=True)

    pool = MaskedStreamPool(make_stream)
    # Kernel compilation is a property of the process. The first width
    # used once read 1773 ms for an 8-unit step.
    print("warming ...", flush=True)
    for width in (8, 16, 24, 32):
        adapter = new_adapter(pipeline, args, 99991)
        adapter.stream = pool.for_quota(width).handle
        executor = StepExecutor(object(), adapter, total_steps=3)
        executor.prepare()
        for _ in range(3):
            if not executor.run_step(quota_units=width):
                break
        adapter.drain_timing()

    interpose = {int(x) for x in args.interpose.split(",") if x}
    episodes = []
    for index in range(1, args.episodes + 1):
        units = (8, 24) if index in interpose else (16, 16)
        base = solo(pipeline, pool, units[0], args, args.seed + index)
        began = time.perf_counter()
        series = episode(pipeline, pool, units, args, args.seed + index)
        kept_a = series["a"][args.warmup:]
        kept_b = series["b"][args.warmup:]
        solo_p50 = statistics.median(base)
        record = {
            "episode": index,
            "units": list(units),
            "solo_p50_s": solo_p50,
            "a_series_s": series["a"],
            "b_series_s": series["b"],
            "a_p50_s": statistics.median(kept_a) if kept_a else None,
            "b_p50_s": statistics.median(kept_b) if kept_b else None,
            "externality_a": (statistics.median(kept_a) / solo_p50
                              if kept_a else None),
            "live_streams": pool.live,
            "elapsed_s": time.perf_counter() - began,
        }
        episodes.append(record)
        print(f"  ep {index:2d} {units[0]:2d}+{units[1]:<2d}  "
              f"solo {solo_p50 * 1000:6.1f}  "
              f"a {record['a_p50_s'] * 1000:6.1f}  "
              f"b {record['b_p50_s'] * 1000:6.1f}  "
              f"ext {record['externality_a']:5.3f}  "
              f"first steps "
              + " ".join(f"{v * 1000:.0f}" for v in series['a'][:5]),
              flush=True)

    same_pair = [e for e in episodes if e["units"] == [16, 16]]
    payload = {
        "schema_version": "burstserve.amd-latch/v1",
        "question": ("how often each co-run state is drawn, whether it "
                     "flips, how fast it can be identified, and whether it "
                     "survives a different mask pair"),
        "workpoint": f"{args.width}x{args.height}",
        "steps": args.steps,
        "warmup_dropped": args.warmup,
        "seed": args.seed,
        "interposed_episodes": sorted(interpose),
        "episodes": episodes,
        "first_16x16_externality": same_pair[0]["externality_a"],
        "later_16x16_externality": [e["externality_a"]
                                    for e in same_pair[1:]],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"-> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
