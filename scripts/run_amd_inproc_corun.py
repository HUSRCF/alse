#!/usr/bin/env python3
"""Co-run two tenants as two streams in one process, and look for the state.

The pairing bistability was found with two processes, each holding a
whole-process CU mask. Measured 44 times, a 16+16 pairing lands in a fast
state (+21.8 to +26.4% per-step penalty) or a slow one (+72.9 to +83.4%),
drawn per run at roughly 30% fast, with no hardware quantity yet found
that predicts the draw.

The scheduler's answer to that is to probe and re-form, which is only
implementable if re-forming redraws. What the hardware has shown is that
*relaunching the processes* redraws. The runtime being designed is one
process with a stream per model, where re-forming means changing a
stream's CU mask. This measures that arrangement:

  * does the bistability exist at all with two streams in one process?
  * if it does, does replacing a stream redraw the state?

Both are open, and the probing policy is unimplementable if either is
no. Streams carry the mask per-stream, established 2026-08-03 and reused
here from run_amd_transition.py.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import statistics
import sys
import threading
import time
from pathlib import Path

sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

WORDS = 4
MASKABLE_UNITS = 32

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
    """A stream restricted to ``mask``, with the mask read back.

    Read back rather than assumed: a mask the runtime silently declines
    to install produces a co-run that is not a partition at all, and
    would read as an unusually low externality.
    """
    handle = ctypes.c_void_p()
    words = (ctypes.c_uint32 * WORDS)(
        *[(mask >> (32 * i)) & 0xFFFFFFFF for i in range(WORDS)]
    )
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


def build(model: str, height: int, width: int, steps: int, seed: int,
          frames: int = 9):
    import torch
    from diffusers import DiffusionPipeline

    # Same loader and arguments as amd_profile_cell, so the two harnesses
    # measure the same model. local_files_only is deliberately not set:
    # the cached snapshot is missing 27 files the pipeline does not need
    # (licences, sample images), and refusing to load without them would
    # measure the cache rather than the model.
    repos = {
        "sdxl": "stabilityai/stable-diffusion-xl-base-1.0",
        "cogvideox-2b": "THUDM/CogVideoX-2b",
    }
    variants = {"sdxl": "fp16"}
    kwargs = {"torch_dtype": torch.float16, "use_safetensors": True}
    if model in variants:
        kwargs["variant"] = variants[model]
    pipeline = DiffusionPipeline.from_pretrained(repos[model],
                                                 **kwargs).to("cuda")
    pipeline.set_progress_bar_config(disable=True)
    video = model.startswith("cogvideox")
    if video and hasattr(pipeline, "vae"):
        # The decode does not fit otherwise, established under Gate B.
        pipeline.vae.enable_tiling()
    call = {
        "prompt": "a quiet street at dusk",
        "num_inference_steps": steps,
        "generator": torch.Generator(device="cuda").manual_seed(seed),
    }
    # Encode the prompt once, outside the measured loop. Two pipelines
    # sharing one tokenizer raise "Already borrowed" when they encode
    # concurrently -- the Rust tokenizer is not reentrant -- and a
    # serving runtime does not re-encode a prompt per denoising round
    # either. Passing embeddings also keeps the text encoder out of the
    # measurement, which is not part of what a quota decision affects.
    if hasattr(pipeline, "encode_prompt"):
        try:
            with torch.no_grad():
                encoded = pipeline.encode_prompt(
                    call["prompt"], device=pipeline._execution_device
                    if hasattr(pipeline, "_execution_device") else "cuda",
                )
        except Exception:
            encoded = None
        if isinstance(encoded, tuple) and encoded and encoded[0] is not None:
            call.pop("prompt")
            call["prompt_embeds"] = encoded[0]
            if len(encoded) > 1 and encoded[1] is not None:
                call["negative_prompt_embeds"] = encoded[1]
            # SDXL's encode_prompt returns four tensors, not two, and its
            # check_inputs refuses prompt_embeds without the pooled pair.
            # Dropping them raised on gfx90a at the first warm call; the
            # branch had never been reached on gfx1201, where
            # encode_prompt itself raises and the plain-prompt fallback
            # runs instead. Carrying all four is what the pipeline asks
            # for, and it leaves the fallback path untouched.
            if len(encoded) > 2 and encoded[2] is not None:
                call["pooled_prompt_embeds"] = encoded[2]
                if len(encoded) > 3 and encoded[3] is not None:
                    call["negative_pooled_prompt_embeds"] = encoded[3]
                elif "negative_prompt_embeds" in call:
                    # A negative embedding without its pooled half is
                    # refused by the same check, so drop the half we have
                    # rather than pass a pair the pipeline will reject.
                    call.pop("negative_prompt_embeds")
    if video:
        call["num_frames"] = frames
    else:
        call["height"] = height
        call["width"] = width
    return pipeline, call


def run_side(pipeline, call, stream, rounds: int, out: list, barrier,
             stop_at: float):
    """Run the pipeline on ``stream``, timing each call on the device."""
    import torch

    with torch.cuda.stream(stream):
        pipeline(**call)                      # warm
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


def one_trial(args, *, replace_streams: bool) -> dict:
    import torch

    left_mask = (1 << args.units_a) - 1
    right_mask = ((1 << args.units_b) - 1) << args.units_a
    handles = []
    streams = []
    for mask in (left_mask, right_mask):
        handle, installed = masked_stream(mask)
        handles.append(handle)
        streams.append(torch.cuda.ExternalStream(handle.value))

    # A solo baseline per side, each on its own mask. Measuring one side
    # and dividing both by it made an 8+24 pair read -44.6% for the wide
    # side: it was being compared against the narrow side's solo cost.
    # Externality is a tenant's slowdown against *its own* quota alone.
    #
    # Warm first: the process's first call pays for kernel selection --
    # 10.4 s against a steady 1.82 s -- and a baseline measured there
    # makes every co-run look faster than solo.
    solos = []
    for index, stream in enumerate(streams):
        pipeline, call = args.pipelines[index]
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

    left, right = [], []
    barrier = threading.Barrier(2)
    # Both sides run for the whole window rather than a fixed round
    # count. With a count, the faster side finishes early and the slower
    # one keeps measuring with the die to itself, which is not a co-run
    # -- an 8+24 pair showed 12.7% cv from exactly that.
    stop_at = time.time() + args.seconds
    threads = [
        threading.Thread(target=run_side,
                         args=(pipe, call, stream, args.rounds, out, barrier,
                               stop_at))
        for (pipe, call), stream, out in zip(args.pipelines, streams,
                                             (left, right))
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    for handle in handles:
        hip.hipStreamDestroy(handle)

    # Only samples that ran while both sides were active. A sample that
    # started before the peer's first call, or ended after its last, was
    # not measuring a co-run -- the same guard the two-process harness
    # applies, for the same reason.
    if left and right:
        overlap_start = max(left[0]["wall"] - left[0]["s"],
                            right[0]["wall"] - right[0]["s"])
        overlap_end = min(left[-1]["wall"], right[-1]["wall"])
    else:
        overlap_start, overlap_end = 0.0, 0.0

    def summarise(values):
        inside = [v["s"] for v in values
                  if v["wall"] - v["s"] >= overlap_start
                  and v["wall"] <= overlap_end]
        if len(inside) < 2:
            return {"n_total": len(values), "n_in_overlap": len(inside)}
        durations = sorted(inside)
        return {
            "n_total": len(values),
            "n_in_overlap": len(durations),
            "p50_s": durations[len(durations) // 2],
            "cv": statistics.stdev(durations) / statistics.mean(durations),
        }

    out = {
        "solo_p50_s": {"left": solos[0], "right": solos[1]},
        "overlap_seconds": max(0.0, overlap_end - overlap_start),
        "left": summarise(left),
        "right": summarise(right),
        "masks": [hex(left_mask), hex(right_mask)],
        "streams_replaced": replace_streams,
    }
    for index, side in enumerate(("left", "right")):
        if "p50_s" in out[side]:
            out[side]["externality"] = out[side]["p50_s"] / solos[index] - 1.0
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="sdxl")
    parser.add_argument("--units-a", type=int, default=16)
    parser.add_argument("--units-b", type=int, default=16)
    parser.add_argument("--height", type=int, default=768)
    parser.add_argument("--width", type=int, default=768)
    parser.add_argument("--steps", type=int, default=8)
    parser.add_argument("--frames", type=int, default=9,
                        help="video models only; images ignore it")
    parser.add_argument("--rounds", type=int, default=12)
    parser.add_argument("--seconds", type=float, default=45.0)
    parser.add_argument("--trials", type=int, default=6)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--share-weights", action="store_true",
                        help="two pipeline objects over one copy of the "
                             "weights, as a serving runtime would hold "
                             "them. Required for CogVideoX-2b, where two "
                             "full pipelines do not fit")
    parser.add_argument("--solo-warmup", type=int, default=2,
                        help="untimed calls before the solo baseline; the "
                             "process's first call costs 5x the steady one")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    import torch

    if not hasattr(torch.cuda, "ExternalStream"):
        print(json.dumps({"status": "no_external_stream"}))
        return 2

    print(f"loading {args.model} ...", flush=True)
    first = build(args.model, args.height, args.width, args.steps,
                  args.seed, frames=args.frames)
    if args.share_weights:
        # One copy of the weights, two pipeline objects. This is what a
        # serving runtime does -- it does not hold a second transformer
        # and a second T5 to serve a second tenant -- and it is the
        # difference between measurable and not: two full CogVideoX-2b
        # pipelines take 29.2 GB of a 31.9 GB budget and OOM during
        # inference, while the modules themselves are stateless under
        # concurrent forward passes.
        # type(...)(**components) rather than from_pipe: the latter
        # dispatches on the base class and rejects it. Passing the
        # components explicitly builds the same concrete pipeline over
        # the same tensors.
        second_pipe = type(first[0])(**first[0].components)
        second_pipe.set_progress_bar_config(disable=True)
        second_call = dict(first[1])
        second_call["generator"] = torch.Generator(
            device="cuda").manual_seed(args.seed + 1)
        second = (second_pipe, second_call)
    else:
        second = build(args.model, args.height, args.width, args.steps,
                       args.seed + 1, frames=args.frames)
    args.pipelines = [first, second]
    print(f"  peak memory {torch.cuda.max_memory_allocated() / 2**30:.1f} GB",
          flush=True)

    trials = []
    for index in range(args.trials):
        # Streams are created and destroyed per trial, which is the
        # in-process equivalent of re-forming a pairing. If the state is
        # drawn per stream pair, trials will differ; if it is fixed for
        # the process, they will not. Either answer decides whether the
        # probing policy is implementable.
        row = one_trial(args, replace_streams=True)
        row["trial"] = index
        trials.append(row)
        left = row["left"].get("externality")
        right = row["right"].get("externality")
        # A side with too few samples inside the overlap window has no
        # externality at all. Printing it as a number crashed the run and
        # threw away the trials that had already succeeded, which is the
        # wrong response to a thin measurement.
        def pct(value):
            return "n/a" if value is None else f"{value * 100:+.1f}%"

        print(f"  trial {index}: solo "
              f"{row['solo_p50_s']['left']:.3f}/"
              f"{row['solo_p50_s']['right']:.3f}s  "
              f"left {pct(left)}  right {pct(right)}  "
              f"cv {row['left'].get('cv', 0) * 100:.2f}%  "
              f"n {row['left'].get('n_in_overlap')}/"
              f"{row['right'].get('n_in_overlap')}", flush=True)

    # A top-level summary in the shape verify_gate_b_amd reads, so a
    # stream-form co-run can be checked by the same clause as a
    # process-form one. The per-trial detail stays below it: the summary
    # is the median trial, not an average that could hide a bad one.
    usable = [t for t in trials
              if "externality" in t.get("left", {})
              and "externality" in t.get("right", {})]
    summary = {}
    if usable:
        by_left = sorted(usable, key=lambda t: t["left"]["externality"])
        median = by_left[len(by_left) // 2]
        left_mask = int(median["masks"][0], 16)
        right_mask = int(median["masks"][1], 16)
        overlap_seconds = median.get("overlap_seconds", 0.0)
        summary = {
            "status": "ok",
            "masks": {"a": median["masks"][0], "b": median["masks"][1],
                      "units_a": args.units_a, "units_b": args.units_b,
                      "disjoint": (left_mask & right_mask) == 0},
            "overlap": {
                "overlap_seconds": overlap_seconds,
                # Both workers run the whole window and only samples
                # inside the peer's active period are counted, so the
                # fraction is against the window that was asked for.
                "overlap_fraction_of_longer_window": (
                    overlap_seconds / args.seconds if args.seconds else 0.0),
                "sufficient_overlap": overlap_seconds >= 0.5 * args.seconds,
                "a": {"externality": median["left"]["externality"],
                      "p50_s": median["left"].get("p50_s"),
                      "cv": median["left"].get("cv")},
                "b": {"externality": median["right"]["externality"],
                      "p50_s": median["right"].get("p50_s"),
                      "cv": median["right"].get("cv")},
            },
            "trials_used": len(usable),
        }

    payload = {
        "schema_version": "burstserve.amd-inproc-corun/v2",
        **summary,
        "question": (
            "does the pairing bistability exist with two streams in one "
            "process, and does replacing the streams redraw it"
        ),
        "arrangement": ("one process, one context, two masked streams"
                        + (", shared weights" if args.share_weights
                           else ", two weight copies")),
        "shared_weights": args.share_weights,
        "units": {"a": args.units_a, "b": args.units_b},
        "workpoint": (f"{args.frames}f" if args.model.startswith("cogvideox")
                      else f"{args.width}x{args.height}"),
        "trials": trials,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"\n-> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
