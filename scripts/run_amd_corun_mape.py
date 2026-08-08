#!/usr/bin/env python3
"""Co-run step prediction against what the steps actually cost.

plan.md's week 9-10 clause is co-run step prediction MAPE under 15%. The
runtime loop already suggests it will not pass as things stand: its
ledger reported predicted against observed at -0.313, a 31%
overstatement, and the loop's own warm-up measured a steady step at
104.2 ms where the cost table says 115.52 ms.

That gap has a likely cause worth stating before measuring, so the
measurement can refute it. The table was built with amd_profile_cell,
which times steps inside a full ``pipeline(...)`` call; the runtime
drives the unet and scheduler directly. If the pipeline carries
per-step overhead the bare loop does not, the table is describing a
different quantity from the one the scheduler pays -- and the scheduler
would be systematically pessimistic, which is the direction that makes
it decline pairings it should take.

So this measures both quantities the same way the runtime does, through
the adapter, and reports:

  * solo per-step at each quota, against the table
  * co-run per-step for a pair, against table x externality
  * MAPE for each, so a failure says which half is wrong

Both sides come from CUDA events on the stream the work ran on. Timing
the host would measure queueing, and under a co-run queueing is not
when the work ran.
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

from burstserve.executor import StepExecutor              # noqa: E402
from burstserve.masked_streams import MaskedStreamPool    # noqa: E402
from burstserve.trace_sim import QuotaCostModel, externality  # noqa: E402

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
        raise RuntimeError(f"create({hex(mask)}) failed")
    buffer = (ctypes.c_uint32 * WORDS)()
    if hip.hipExtStreamGetCUMask(handle, WORDS, buffer) != 0:
        raise RuntimeError("readback failed")
    installed = 0
    for index, word in enumerate(buffer):
        installed |= word << (32 * index)
    return handle, installed


def mape(pairs: list[tuple[float, float]]) -> float:
    """Mean absolute percentage error of predicted against observed."""
    return statistics.mean(
        abs(predicted - observed) / observed for predicted, observed in pairs
    ) if pairs else float("nan")


def run_solo(pipeline, pool, units, args, seed_offset=0) -> list[float]:
    from amd_sdxl_adapter import SdxlStepAdapter

    adapter = SdxlStepAdapter(pipeline, height=args.height, width=args.width,
                              steps=args.steps, seed=args.seed + seed_offset)
    adapter.stream = pool.for_quota(units).handle
    executor = StepExecutor(object(), adapter, total_steps=args.steps)
    executor.prepare()
    seen: list[float] = []
    for index in range(args.steps):
        executor.run_step(quota_units=units)
        # The adapter reports the previous step, so a reading appears one
        # step late; the warm-up steps are dropped below either way.
        if adapter.last_step_seconds:
            seen.append(adapter.last_step_seconds)
    adapter.drain_timing()
    if adapter.last_step_seconds:
        seen.append(adapter.last_step_seconds)
    return seen[args.warmup:]


def run_pair(pipeline, pool, units_a, units_b, args) -> tuple[list, list]:
    """Two requests on disjoint masks, stepping concurrently."""
    from amd_sdxl_adapter import SdxlStepAdapter

    left_stream, right_stream = pool.disjoint_pair(units_a, units_b)
    out: dict[str, list[float]] = {"a": [], "b": []}
    barrier = threading.Barrier(2)

    def side(name, units, stream, seed_offset):
        adapter = SdxlStepAdapter(pipeline, height=args.height,
                                  width=args.width, steps=args.steps,
                                  seed=args.seed + seed_offset)
        adapter.stream = stream.handle
        executor = StepExecutor(object(), adapter, total_steps=args.steps)
        executor.prepare()
        # Warm before the barrier so neither side measures the other's
        # start-up, then step in lockstep so every measured step overlaps
        # a step on the other side.
        executor.run_step(quota_units=units)
        barrier.wait()
        for _ in range(args.steps - 1):
            executor.run_step(quota_units=units)
            if adapter.last_step_seconds:
                out[name].append(adapter.last_step_seconds)
        adapter.drain_timing()
        if adapter.last_step_seconds:
            out[name].append(adapter.last_step_seconds)

    threads = [
        threading.Thread(target=side, args=("a", units_a, left_stream, 0)),
        threading.Thread(target=side, args=("b", units_b, right_stream, 1)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return out["a"][args.warmup:], out["b"][args.warmup:]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--steps", type=int, default=14)
    parser.add_argument("--warmup", type=int, default=4)
    parser.add_argument("--height", type=int, default=768)
    parser.add_argument("--width", type=int, default=768)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--solo-quotas", default="8,16,24,32")
    parser.add_argument("--pairs", default="16+16,8+24,24+8")
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
    cost = QuotaCostModel.for_model("sdxl")

    solo_rows = []
    solo_pairs: list[tuple[float, float]] = []
    for units in [int(q) for q in args.solo_quotas.split(",")]:
        observed = run_solo(pipeline, pool, units, args)
        median = statistics.median(observed)
        predicted = cost.step_seconds(units)
        solo_rows.append({
            "units": units, "predicted_s": predicted,
            "observed_p50_s": median, "n": len(observed),
            "relative_error": predicted / median - 1.0,
        })
        solo_pairs.append((predicted, median))
        print(f"  solo {units:2d}u: predicted {predicted*1000:6.1f} ms  "
              f"observed {median*1000:6.1f} ms  "
              f"{(predicted/median-1)*100:+6.1f}%", flush=True)

    corun_rows = []
    corun_pairs: list[tuple[float, float]] = []
    for spec in args.pairs.split(","):
        left, right = (int(x) for x in spec.split("+"))
        a_times, b_times = run_pair(pipeline, pool, left, right, args)
        for name, units, peer, times in (("a", left, right, a_times),
                                         ("b", right, left, b_times)):
            if not times:
                continue
            median = statistics.median(times)
            factor = externality(units, peer, "sdxl")
            predicted = cost.step_seconds(units) * factor
            corun_rows.append({
                "pair": spec, "side": name, "units": units, "peer": peer,
                "externality": factor, "predicted_s": predicted,
                "observed_p50_s": median, "n": len(times),
                "relative_error": predicted / median - 1.0,
            })
            corun_pairs.append((predicted, median))
            print(f"  corun {spec} {name}: predicted {predicted*1000:6.1f} ms"
                  f"  observed {median*1000:6.1f} ms  "
                  f"{(predicted/median-1)*100:+6.1f}%", flush=True)

    solo_mape = mape(solo_pairs)
    corun_mape = mape(corun_pairs)
    payload = {
        "schema_version": "burstserve.amd-corun-mape/v1",
        "clause": "co-run step prediction MAPE under 15%",
        "measured_through": ("the runtime's own adapter, so predicted and "
                             "observed describe the same quantity"),
        "workpoint": f"{args.width}x{args.height}",
        "steps": args.steps, "warmup_dropped": args.warmup,
        "solo": solo_rows,
        "corun": corun_rows,
        "solo_mape": solo_mape,
        "corun_mape": corun_mape,
        "corun_mape_under_15pct": corun_mape < 0.15,
        # Reported together because a co-run prediction is the solo table
        # times an externality: if solo is already off, the co-run figure
        # inherits it and fixing the externality would be fixing the
        # wrong term.
        "solo_mape_under_10pct": solo_mape < 0.10,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2) + "\n")

    print(f"\nsolo MAPE  {solo_mape*100:5.2f}%   (clause: under 10%)")
    print(f"corun MAPE {corun_mape*100:5.2f}%   (clause: under 15%)")
    print(f"-> {args.out}")
    return 0 if payload["corun_mape_under_15pct"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
