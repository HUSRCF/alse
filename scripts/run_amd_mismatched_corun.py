#!/usr/bin/env python3
"""Does partitioning pay when the two tenants are not the same model?

Every per-step co-run measurement in this project has paired SDXL with
itself, and that is the case partitioning should find hardest: two
tenants wanting the same units, the same cache and the same bandwidth at
the same instant. On 2026-08-08 that case came out at -12.8% -- the die
finishes both tenants' steps sooner by giving each the whole card in
turn than by splitting it. The scheduler, though, exists for tenants
whose demands differ, and no measurement covers that at step
granularity: ``run_amd_inproc_corun`` runs CogVideoX through a whole
``pipeline(...)`` call, and a call includes a VAE decode that does not
scale with quota. Applying call-level numbers to a per-step decision is
precisely the error that produced the refuted +18.6%.

**The comparison this reports.** Under rotation a tenant holding share
``f`` of the schedule runs alone on the full die for ``f`` of the wall
clock, so its step rate is ``f / solo32``. Under partitioning it holds
``f`` of the die continuously, so its rate is ``1 / corun``. Partitioning
helps that tenant exactly when

    corun  <  solo32 / f

and at an even split that is ``corun < 2 x solo32``. Both tenants are
reported separately, because a split that helps one and hurts the other
is a fairness question rather than a throughput one, and averaging them
would hide it.

**What is deliberately not decided here.** A mismatched pair that wins
does not restore the general claim -- it would establish that
partitioning pays for *this* pair at *this* workpoint, which is a
narrower statement than the one that was refuted. The point of measuring
it is that the refutation covered one pairing and the scheduler is aimed
at another.
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
    """Create a masked stream and report what the driver installed.

    The readback is not a sanity check. A stream that quietly carries the
    whole die produces a co-run with an unusually *low* externality, and
    that reads as good news.
    """
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


def build_pipeline(model: str, *, drop_text_encoders: bool):
    import torch
    from diffusers import DiffusionPipeline

    repos = {"sdxl": "stabilityai/stable-diffusion-xl-base-1.0",
             "cogvideox-2b": "THUDM/CogVideoX-2b"}
    kwargs = {"torch_dtype": torch.float16, "use_safetensors": True}
    if model == "sdxl":
        kwargs["variant"] = "fp16"
    pipeline = DiffusionPipeline.from_pretrained(repos[model],
                                                 **kwargs).to("cuda")
    pipeline.set_progress_bar_config(disable=True)
    return pipeline


def free_text_encoders(pipeline) -> int:
    """Drop the text encoders once prompts are encoded.

    Both models resident at once is the point of the measurement, and
    CogVideoX carries a 4.7B T5. The encoders are outside the measured
    loop either way -- a serving runtime does not re-encode a prompt per
    denoising round -- so holding them would cost VRAM without changing
    what is measured. Returns the bytes released, so the payload records
    that this happened rather than leaving it to the reader.
    """
    import gc
    import torch

    before = torch.cuda.memory_allocated()
    for name in ("text_encoder", "text_encoder_2", "text_encoder_3"):
        if getattr(pipeline, name, None) is not None:
            setattr(pipeline, name, None)
    gc.collect()
    torch.cuda.empty_cache()
    return before - torch.cuda.memory_allocated()


def make_adapter(model: str, pipeline, args, seed: int):
    if model == "sdxl":
        from amd_sdxl_adapter import SdxlStepAdapter
        return SdxlStepAdapter(pipeline, height=args.height, width=args.width,
                               steps=args.steps, seed=seed)
    from amd_cogvideox_adapter import CogVideoXStepAdapter
    return CogVideoXStepAdapter(pipeline, frames=args.frames,
                                height=args.video_height,
                                width=args.video_width,
                                steps=args.steps, seed=seed)


def warm(adapter, pool, units, args, rounds: int = 3) -> None:
    """Pay kernel compilation before anything is measured.

    The first width used in a process paid it once and read 1773 ms for
    an 8-unit step, which moved a solo MAPE from 3% to 24%. It is a
    property of the process, not of the quota.
    """
    adapter.stream = pool.for_quota(units).handle
    executor = StepExecutor(object(), adapter, total_steps=rounds)
    executor.prepare()
    for _ in range(rounds):
        if not executor.run_step(quota_units=units):
            break
    adapter.drain_timing()


def run_solo(adapter, pool, units, args) -> list[float]:
    adapter.stream = pool.for_quota(units).handle
    executor = StepExecutor(object(), adapter, total_steps=args.steps)
    executor.prepare()
    seen: list[float] = []
    while executor.run_step(quota_units=units):
        # The adapter reports the previous step, so a reading appears one
        # step late; synchronising on the step just issued would drain the
        # pipeline and charge the drain to the measurement.
        if adapter.last_step_seconds:
            seen.append(adapter.last_step_seconds)
    adapter.drain_timing()
    if adapter.last_step_seconds:
        seen.append(adapter.last_step_seconds)
    return seen[args.warmup:]


def run_pair(models, adapters, pool, units, args):
    """Two models on disjoint masks, stepping concurrently.

    One adapter per model, reused rather than rebuilt. That is what the
    runtime does -- a model's weights and conditioning are resident and a
    request is a state passed through them -- and it is also what makes
    this measurable at all: the adapter constructors encode prompts, and
    both text encoders cannot stay resident beside both models.
    """
    left, right = pool.disjoint_pair(units[0], units[1])
    streams = {"a": left, "b": right}
    out: dict[str, list[float]] = {"a": [], "b": []}
    windows: dict[str, list[tuple[float, float, float]]] = {"a": [], "b": []}
    barrier = threading.Barrier(2)

    prepared = {}
    for name, model in (("a", models[0]), ("b", models[1])):
        adapter = adapters[model]
        adapter.stream = streams[name].handle
        executor = StepExecutor(object(), adapter, total_steps=args.steps)
        executor.prepare()
        prepared[name] = (adapter, executor)

    def side(name, quota):
        adapter, executor = prepared[name]
        # Warm before the barrier so neither side measures the other's
        # start-up.
        executor.run_step(quota_units=quota)
        barrier.wait()
        for _ in range(args.steps - 1):
            began = time.perf_counter()
            executor.run_step(quota_units=quota)
            ended = time.perf_counter()
            if adapter.last_step_seconds:
                out[name].append(adapter.last_step_seconds)
                windows[name].append((began, ended, adapter.last_step_seconds))
        adapter.drain_timing()
        if adapter.last_step_seconds:
            out[name].append(adapter.last_step_seconds)

    threads = [threading.Thread(target=side, args=("a", units[0])),
               threading.Thread(target=side, args=("b", units[1]))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # A mismatched pair finishes at different times by construction -- a
    # CogVideoX step is several times an SDXL step -- so the side that
    # runs on past its peer would otherwise report free-running steps as
    # contended ones. Only steps that began after the peer started and
    # ended before it finished are kept for the overlap figure.
    def overlapped(name, peer):
        mine, theirs = windows[name], windows[peer]
        if not mine or not theirs:
            return []
        peer_start, peer_end = theirs[0][0], theirs[-1][1]
        return [seconds for began, ended, seconds in mine
                if began >= peer_start and ended <= peer_end]

    return {
        "a_all": out["a"][args.warmup:], "b_all": out["b"][args.warmup:],
        "a_overlap": overlapped("a", "b")[args.warmup:],
        "b_overlap": overlapped("b", "a")[args.warmup:],
    }


def p50(values):
    return statistics.median(values) if values else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--steps", type=int, default=14)
    parser.add_argument("--warmup", type=int, default=4)
    parser.add_argument("--height", type=int, default=768)
    parser.add_argument("--width", type=int, default=768)
    parser.add_argument("--video-height", type=int, default=480)
    parser.add_argument("--video-width", type=int, default=720)
    parser.add_argument("--frames", type=int, default=9)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--split", default="16+16")
    parser.add_argument("--keep-text-encoders", action="store_true")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    import torch

    units = [int(u) for u in args.split.split("+")]
    models = ["sdxl", "cogvideox-2b"]
    pool = MaskedStreamPool(make_stream)

    pipelines = {}
    adapters = {}
    released = {}
    for model in models:
        print(f"loading {model} ...", flush=True)
        pipelines[model] = build_pipeline(model, drop_text_encoders=False)
        # Built while its encoder is still resident; the embeddings it
        # holds are all that is needed afterwards.
        adapters[model] = make_adapter(model, pipelines[model], args,
                                       seed=args.seed + models.index(model))
        if not args.keep_text_encoders:
            released[model] = free_text_encoders(pipelines[model])
            print(f"  {model}: released "
                  f"{released[model] / 2**30:.2f} GB of text encoder",
                  flush=True)
    print(f"  resident {torch.cuda.memory_allocated() / 2**30:.2f} GB",
          flush=True)

    widths = sorted({units[0], units[1], 32})
    print(f"warming widths {widths} for both models ...", flush=True)
    for model in models:
        for width in widths:
            warm(adapters[model], pool, width, args)

    solo: list[dict] = []
    for index, model in enumerate(models):
        for width in (units[index], 32):
            samples = run_solo(adapters[model], pool, width, args)
            solo.append({"model": model, "units": width,
                         "p50_s": p50(samples), "samples": len(samples)})
            print(f"  solo {model:13s} {width:2d}u: "
                  f"{p50(samples) * 1000:8.1f} ms", flush=True)

    print(f"co-run {models[0]}@{units[0]} + {models[1]}@{units[1]} ...",
          flush=True)
    paired = run_pair(models, adapters, pool, units, args)

    def solo_at(model, width):
        for row in solo:
            if row["model"] == model and row["units"] == width:
                return row["p50_s"]
        return None

    verdict = []
    for name, model, quota in (("a", models[0], units[0]),
                               ("b", models[1], units[1])):
        corun = p50(paired[f"{name}_all"])
        overlap = p50(paired[f"{name}_overlap"])
        alone_at_quota = solo_at(model, quota)
        alone_full = solo_at(model, 32)
        share = quota / 32.0
        # Under rotation this tenant runs alone on the whole die for
        # `share` of the wall clock; under partitioning it holds `share`
        # of the die all the time.
        rotating_equivalent = alone_full / share if alone_full else None
        verdict.append({
            "side": name, "model": model, "units": quota, "share": share,
            "corun_p50_s": corun,
            "corun_overlap_p50_s": overlap,
            "solo_at_quota_s": alone_at_quota,
            "solo_full_die_s": alone_full,
            "externality": (corun / alone_at_quota
                            if corun and alone_at_quota else None),
            "rotating_equivalent_s": rotating_equivalent,
            "partitioning_gain": ((rotating_equivalent / corun - 1)
                                  if corun and rotating_equivalent else None),
        })

    payload = {
        "schema_version": "burstserve.amd-mismatched-corun/v1",
        "question": ("does spatial partitioning pay when the two tenants "
                     "are different models, measured per step"),
        "measured_through": ("each model's own step adapter, so predicted "
                             "and observed describe the same quantity"),
        "models": models,
        "split": args.split,
        "steps": args.steps,
        "warmup_dropped": args.warmup,
        "sdxl_workpoint": f"{args.width}x{args.height}",
        "cogvideox_workpoint": (f"{args.video_width}x{args.video_height}"
                                f"x{args.frames}f"),
        "text_encoders_released_bytes": released,
        "stream_attestation": pool.attestation(),
        "solo": solo,
        "verdict": verdict,
        "comparison": ("rotating gives a tenant the full die for its share "
                       "of the wall clock; partitioning gives it its share "
                       "of the die all the time. partitioning_gain > 0 "
                       "means partitioning advances that tenant faster."),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2) + "\n")

    print()
    for row in verdict:
        gain = row["partitioning_gain"]
        print(f"{row['model']:13s} {row['units']:2d}u  "
              f"co-run {row['corun_p50_s'] * 1000:8.1f} ms   "
              f"solo@quota {row['solo_at_quota_s'] * 1000:8.1f} ms   "
              f"ext {row['externality']:5.3f}   "
              f"rotating-equivalent {row['rotating_equivalent_s'] * 1000:8.1f}"
              f" ms   partitioning {gain * 100:+6.1f}%")
    print(f"-> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
