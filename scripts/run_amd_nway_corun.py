#!/usr/bin/env python3
"""The same-model co-run penalty with N peers, not one.

Every externality this project has measured is **pairwise**: two masked
streams, one peer each. Every arithmetic about intra-tenant concurrency
uses those numbers anyway. `docs/prereg-intra-tenant.md` says so out
loud -- it charges 1.297, which is 1.3's *pair* at 16+16, to a **four**
-way arrangement at 8 units each, where each slice has three peers rather
than one. That approximation is the single number its prediction turns
on, and it has never been measured.

This measures it. N disjoint equal slices of the die, one process, one
copy of the weights, one adapter per slice, all running the same model at
once for a fixed window. Reported per slice and as a mean, against a solo
baseline taken **at the same slice width on its own mask**, because
dividing every side by one side's baseline is the defect that once made
an 8+24 pair read -44.6%.

Why it matters beyond expC. On the measured curves the whole die split
four ways beats serving a burst serially on both architectures -- 2.79 s
against 3.70 s on gfx1201 at the pairwise fast penalty, 3.12 s against
3.83 s on gfx90a. At a burst of eight, where eight slices can actually
be used, gfx1201 keeps improving to 5.41 s while gfx90a **peaks at four
ways** (6.24 s) and eight ways costs 8.43 s -- worse than not splitting
at all, which is 7.66 s. So the optimum concurrency is the
architecture's, the curve predicts where it is, and the N-way penalty is
what turns that prediction into a claim.

Everything is read back rather than assumed: each mask after
installation, and every pair of masks for disjointness. A runtime that
quietly widens a mask produces an unusually LOW penalty, which reads as
good news.
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

from burstserve.provenance import canonical_json  # noqa: E402
import run_amd_mismatched_corun as harness       # noqa: E402

SCHEMA_VERSION = "burstserve.amd-nway-corun/v1"
WORDS = 4  # 128 mask bits, enough for gfx90a's 104

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


def masked_stream(mask: int):
    """A stream restricted to ``mask``, with the mask read back."""
    handle = ctypes.c_void_p()
    words = (ctypes.c_uint32 * WORDS)(
        *[(mask >> (32 * i)) & 0xFFFFFFFF for i in range(WORDS)])
    rc = hip.hipExtStreamCreateWithCUMask(ctypes.byref(handle), WORDS, words)
    if rc != 0:
        raise RuntimeError(f"hipExtStreamCreateWithCUMask({hex(mask)}) rc={rc}")
    buffer = (ctypes.c_uint32 * WORDS)()
    if hip.hipExtStreamGetCUMask(handle, WORDS, buffer) != 0:
        raise RuntimeError("readback failed")
    got = 0
    for index, word in enumerate(buffer):
        got |= word << (32 * index)
    if got != mask:
        raise RuntimeError(f"runtime installed {hex(got)} for {hex(mask)}")
    return handle, got


def slice_masks(ways: int, maskable_units: int) -> list[int]:
    """``ways`` disjoint equal slices, low bits first.

    The remainder goes nowhere: an unequal slice would make the mean
    penalty a number about a shape nobody asked for. If the die does not
    divide, the leftover units are left idle and that is recorded.
    """
    width = maskable_units // ways
    if width < 1:
        raise ValueError(f"{ways} ways do not fit in {maskable_units} units")
    return [((1 << width) - 1) << (width * index) for index in range(ways)]


def make_call(pipeline, args, seed: int) -> dict:
    """Call kwargs for one slice, sharing the pipeline with the others.

    The prompt is encoded once here rather than inside the measured loop.
    Two slices encoding concurrently raise "Already borrowed" -- the Rust
    tokenizer is not reentrant -- and a serving runtime does not re-encode
    a prompt per denoising round either. SDXL's encode_prompt returns
    FOUR tensors and its check_inputs refuses prompt_embeds without the
    pooled pair, so all four are carried.
    """
    import torch

    video = args.model.startswith("cogvideox")
    call = {
        "prompt": "a quiet street at dusk",
        "num_inference_steps": args.steps,
        "generator": torch.Generator(device="cuda").manual_seed(seed),
    }
    if hasattr(pipeline, "encode_prompt"):
        try:
            with torch.no_grad():
                encoded = pipeline.encode_prompt(
                    call["prompt"],
                    device=getattr(pipeline, "_execution_device", "cuda"))
        except Exception:
            encoded = None
        if isinstance(encoded, tuple) and encoded and encoded[0] is not None:
            call.pop("prompt")
            call["prompt_embeds"] = encoded[0]
            if len(encoded) > 1 and encoded[1] is not None:
                call["negative_prompt_embeds"] = encoded[1]
            if len(encoded) > 2 and encoded[2] is not None:
                call["pooled_prompt_embeds"] = encoded[2]
                if len(encoded) > 3 and encoded[3] is not None:
                    call["negative_pooled_prompt_embeds"] = encoded[3]
            elif "negative_prompt_embeds" in call:
                call.pop("negative_prompt_embeds")
    if video:
        call["num_frames"] = args.frames
        call["height"] = args.video_height
        call["width"] = args.video_width
    else:
        call["height"] = args.height
        call["width"] = args.width
    return call


def run_side(pipeline, call, stream, out: list, barrier, stop_at: float):
    import torch

    with torch.cuda.stream(stream):
        pipeline(**call)                      # warm, outside the window
    torch.cuda.synchronize()
    barrier.wait()
    while time.time() < stop_at:
        start = torch.cuda.Event(enable_timing=True)
        end = torch.cuda.Event(enable_timing=True)
        with torch.cuda.stream(stream):
            start.record(stream)
            pipeline(**call)
            end.record(stream)
        end.synchronize()
        out.append({"s": start.elapsed_time(end) / 1000.0,
                    "wall": time.time()})


def one_trial(args, pipelines) -> dict:
    import torch

    masks = slice_masks(args.ways, args.maskable_units)
    for i, left in enumerate(masks):
        for right in masks[i + 1:]:
            if left & right:
                raise RuntimeError("slices overlap before installation")
    handles, streams = [], []
    for mask in masks:
        handle, _ = masked_stream(mask)
        handles.append(handle)
        streams.append(torch.cuda.ExternalStream(handle.value))

    # A solo baseline per slice, each on its own mask, warm first. The
    # process's first call pays for kernel selection -- 10.4 s against a
    # steady 1.82 s on gfx1201 -- and a baseline measured there makes
    # every co-run look faster than solo.
    solos = []
    for index, stream in enumerate(streams):
        pipeline, call = pipelines[index]
        with torch.cuda.stream(stream):
            for _ in range(args.solo_warmup):
                pipeline(**call)
        torch.cuda.synchronize()
        samples = []
        with torch.cuda.stream(stream):
            for _ in range(3):
                start = torch.cuda.Event(enable_timing=True)
                end = torch.cuda.Event(enable_timing=True)
                start.record(stream)
                pipeline(**call)
                end.record(stream)
                end.synchronize()
                samples.append(start.elapsed_time(end) / 1000.0)
        solos.append(sorted(samples)[len(samples) // 2])

    outs = [[] for _ in streams]
    barrier = threading.Barrier(args.ways)
    # A window, not a round count. With a count the fast slice finishes
    # early and the rest measure with more of the die than they were
    # given, which is not the arrangement.
    stop_at = time.time() + args.seconds
    threads = [
        threading.Thread(target=run_side,
                         args=(pipelines[i][0], pipelines[i][1], streams[i],
                               outs[i], barrier, stop_at))
        for i in range(args.ways)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    for handle in handles:
        hip.hipStreamDestroy(handle)

    # Only samples that ran while EVERY slice was active. With N sides the
    # window is the intersection of all of them, not of a chosen pair.
    if all(outs):
        overlap_start = max(o[0]["wall"] - o[0]["s"] for o in outs)
        overlap_end = min(o[-1]["wall"] for o in outs)
    else:
        overlap_start = overlap_end = 0.0

    def summarise(values, solo):
        inside = [v["s"] for v in values
                  if v["wall"] - v["s"] >= overlap_start
                  and v["wall"] <= overlap_end]
        if len(inside) < 2:
            return {"n_total": len(values), "n_in_overlap": len(inside)}
        durations = sorted(inside)
        p50 = durations[len(durations) // 2]
        return {
            "n_total": len(values),
            "n_in_overlap": len(durations),
            "p50_s": p50,
            "cv": statistics.stdev(durations) / statistics.mean(durations),
            "externality": p50 / solo,
        }

    sides = [summarise(outs[i], solos[i]) for i in range(args.ways)]
    scored = [s["externality"] for s in sides if "externality" in s]
    return {
        "ways": args.ways,
        "slice_units": args.maskable_units // args.ways,
        "idle_units": args.maskable_units % args.ways,
        "masks": [hex(m) for m in masks],
        "solo_p50_s": solos,
        "overlap_seconds": max(0.0, overlap_end - overlap_start),
        "sides": sides,
        "externality_mean": statistics.mean(scored) if scored else None,
        "externality_min": min(scored) if scored else None,
        "externality_max": max(scored) if scored else None,
        "sides_scored": len(scored),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="sdxl")
    parser.add_argument("--ways", type=int, default=4)
    parser.add_argument("--maskable-units", type=int, default=32,
                        help="32 on gfx1201 (R9700), 104 on gfx90a (MI250X)")
    parser.add_argument("--height", type=int, default=768)
    parser.add_argument("--width", type=int, default=768)
    parser.add_argument("--video-height", type=int, default=480)
    parser.add_argument("--video-width", type=int, default=720)
    parser.add_argument("--frames", type=int, default=9)
    parser.add_argument("--steps", type=int, default=8)
    parser.add_argument("--seconds", type=float, default=150.0)
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--solo-warmup", type=int, default=2)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.keep_text_encoders = True

    import torch

    print(f"loading {args.model} ...", flush=True)
    pipeline = harness.build_pipeline(args.model, drop_text_encoders=False)
    pipeline.set_progress_bar_config(disable=True)
    if args.model.startswith("cogvideox") and hasattr(pipeline, "vae"):
        pipeline.vae.enable_tiling()   # the decode does not fit otherwise

    # One copy of the weights, N *pipeline objects*. Sharing a single
    # pipeline across the threads shares its scheduler, and two threads
    # stepping one EulerDiscreteScheduler clobber ``_step_index``:
    # "IndexError: index 9 is out of bounds for dimension 0 with size 9",
    # on the second trial, after the first has already reported numbers.
    # prereg-intra-tenant.md names this hazard for the adapters; it is the
    # same hazard here.
    #
    # A sibling built from ``components`` holds the same module objects --
    # the same UNet, the same VAE -- with its own scheduler and its own
    # per-call state. So the extra cost per way is a scheduler built
    # ``from_config`` and one set of prompt embeddings, not a second copy
    # of the weights, which is why N slices of one tenant are cheap enough
    # to be worth asking about at all.
    scheduler_class = type(pipeline.scheduler)
    pipelines = []
    for index in range(args.ways):
        if index == 0:
            sibling = pipeline
        else:
            components = dict(pipeline.components)
            components["scheduler"] = scheduler_class.from_config(
                pipeline.scheduler.config)
            sibling = type(pipeline)(**components)
            sibling.set_progress_bar_config(disable=True)
            if args.model.startswith("cogvideox") and hasattr(sibling, "vae"):
                sibling.vae.enable_tiling()
        pipelines.append((sibling, make_call(sibling, args,
                                             args.seed + index)))
    weights_gb = torch.cuda.memory_allocated() / 2**30
    print(f"  {args.ways} pipelines sharing one copy of the weights, "
          f"{weights_gb:.1f} GB resident", flush=True)

    print(f"  peak memory {torch.cuda.max_memory_allocated() / 2**30:.1f} GB",
          flush=True)

    trials = []
    for index in range(args.trials):
        row = one_trial(args, pipelines)
        row["trial"] = index
        trials.append(row)
        mean = row["externality_mean"]
        print(f"  trial {index}: slice {row['slice_units']}u  "
              f"solo {statistics.median(row['solo_p50_s']):.3f}s  "
              f"externality mean "
              f"{'n/a' if mean is None else f'{mean:.4f}'}  "
              f"range {row['externality_min']}..{row['externality_max']}  "
              f"n {[s.get('n_in_overlap') for s in row['sides']]}",
              flush=True)

    scored = [t["externality_mean"] for t in trials
              if t["externality_mean"] is not None]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "question": "what does a slice pay when it has N-1 same-model "
                    "peers rather than one",
        "arrangement": "one process, one copy of the weights, "
                       f"{args.ways} disjoint equal masked streams",
        "model": args.model,
        "ways": args.ways,
        "maskable_units": args.maskable_units,
        "workpoint": (f"{args.height}x{args.width}" if args.model == "sdxl"
                      else f"{args.frames} frames"),
        "steps": args.steps,
        "window_seconds": args.seconds,
        "device": {
            "name": torch.cuda.get_device_properties(0).name,
            "arch": torch.cuda.get_device_properties(0).gcnArchName,
            "multi_processor_count":
                torch.cuda.get_device_properties(0).multi_processor_count,
        },
        "torch": torch.__version__,
        "externality_mean_over_trials":
            statistics.mean(scored) if scored else None,
        "trials": trials,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(canonical_json(payload))
    print(f"-> {args.out}", flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
