"""Predict what a step costs after its tenant's quota changes.

Gate B wants transition prediction within 10% MAPE. The scheduler's move is
to hand a running tenant a different CU quota between denoising steps, so
the measurement has to do exactly that -- inside one process, mid-request,
without reloading the model.

That is possible because hipExtStreamCreateWithCUMask composes with
torch.cuda.ExternalStream and the mask stays per-stream (2026-08-03), so
the process holds one stream per quota and moves the work between them.

The prediction has no free parameters: a step at quota q is predicted to
cost what steps at quota q cost when nothing changed. Any transient the
change itself introduces shows up as error rather than being absorbed by a
fitted term, which is the point -- the question is whether a scheduler can
plan with the steady-state table it already has.

Two things the measurement must not do:

  * time the steps on the host. Launches are asynchronous, so a host timer
    reports queueing speed until the queue fills. CUDA events, recorded on
    the stream, measure the device.
  * change stream without ordering the change. The next step reads tensors
    the previous one wrote, so the new stream waits on an event recorded on
    the old one. A bare switch would race, and a full synchronize would
    charge the transition for a barrier the scheduler would not pay.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import statistics
import sys
from pathlib import Path

sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from burstserve.provenance import canonical_json  # noqa: E402
from burstserve.quota_model import mape  # noqa: E402

SCHEMA_VERSION = "burstserve.amd-transition/v1"
WORDS = 4

MODEL_REPOS = {
    "sdxl": "stabilityai/stable-diffusion-xl-base-1.0",
    "cogvideox-2b": "THUDM/CogVideoX-2b",
    "cogvideox-5b": "THUDM/CogVideoX-5b",
    "flux-dev": "black-forest-labs/FLUX.1-dev",
}
MODEL_VARIANT = {"sdxl": "fp16"}

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
    return handle, mask


def schedule_for(steps: int, quotas: list[int], dwell: int) -> list[int]:
    """Which quota each step runs at: dwell steps per quota, round robin."""
    return [quotas[(index // dwell) % len(quotas)] for index in range(steps)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="sdxl")
    parser.add_argument("--quotas", default="8,16,32")
    parser.add_argument("--steps", type=int, default=30)
    parser.add_argument("--dwell", type=int, default=5)
    parser.add_argument("--height", type=int, default=768)
    parser.add_argument("--width", type=int, default=768)
    parser.add_argument("--frames", type=int, default=49)
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--steady-repeats", type=int, default=3)
    parser.add_argument("--switch-repeats", type=int, default=3)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--prompt", default="a quiet street at dusk")
    parser.add_argument("--mape-threshold", type=float, default=0.10)
    parser.add_argument("--vae-tiling", action="store_true")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    import torch
    from diffusers import DiffusionPipeline

    if not hasattr(torch.cuda, "ExternalStream"):
        print(json.dumps({"status": "no_external_stream"}))
        return 2

    quotas = [int(q) for q in args.quotas.split(",") if q.strip()]
    repo = MODEL_REPOS[args.model]
    kwargs = {"torch_dtype": torch.float16, "use_safetensors": True}
    if args.model in MODEL_VARIANT:
        kwargs["variant"] = MODEL_VARIANT[args.model]
    pipeline = DiffusionPipeline.from_pretrained(repo, **kwargs).to("cuda")
    pipeline.set_progress_bar_config(disable=True)
    if args.vae_tiling and hasattr(pipeline, "vae") and hasattr(
        pipeline.vae, "enable_tiling"
    ):
        pipeline.vae.enable_tiling()

    handles, streams, masks = {}, {}, {}
    for units in quotas:
        handle, mask = masked_stream(units)
        handles[units] = handle
        masks[units] = hex(mask)
        streams[units] = torch.cuda.ExternalStream(handle.value)

    # Latent output: the denoising steps are what is being timed, and
    # skipping the VAE keeps the comparison to the steps themselves. It
    # also gives a tensor to checksum, which is how the run proves that
    # moving work between streams did not change the result -- the caching
    # allocator does not know about the event dependency used to order the
    # handover, so correctness has to be demonstrated rather than argued.
    call = {"prompt": [args.prompt], "num_inference_steps": args.steps,
            "generator": None, "output_type": "latent"}
    if args.model.startswith("cogvideox"):
        call["num_frames"] = args.frames
    else:
        call["height"] = args.height
        call["width"] = args.width

    def run(plan: list[int] | None, fixed: int | None):
        """One pipeline call; returns (quota, seconds) per step boundary."""
        events, at_quota = [], []
        current = [fixed if fixed is not None else plan[0]]

        def on_step(pipe, index, timestep, cb):
            event = torch.cuda.Event(enable_timing=True)
            event.record()
            events.append(event)
            if plan is not None and index + 1 < len(plan):
                nxt = plan[index + 1]
                if nxt != current[0]:
                    # Order the handover: the next step reads what this one
                    # wrote. An event is the cheap way to say so; a full
                    # synchronize would bill the transition for a barrier.
                    done = torch.cuda.Event()
                    done.record(streams[current[0]])
                    streams[nxt].wait_event(done)
                    current[0] = nxt
                    torch.cuda.set_stream(streams[nxt])
            # After the handover, not before. This callback fires when step
            # `index` ends, so the interval that starts here is step
            # index+1, and it runs at the quota in force *after* the switch.
            # Recording it before attributed every interval to the previous
            # step's quota -- an off-by-one that made the step following a
            # change look accurately predicted and every settled step look
            # badly predicted.
            at_quota.append(current[0])
            return cb

        start = fixed if fixed is not None else plan[0]
        torch.cuda.set_stream(streams[start])
        call["generator"] = torch.Generator(device="cuda").manual_seed(args.seed)
        with torch.inference_mode():
            result = pipeline(callback_on_step_end=on_step, **call)
        torch.cuda.synchronize()
        torch.cuda.set_stream(torch.cuda.default_stream())
        # Pipelines name their output differently -- images for image
        # pipelines, frames for video ones -- and neither is guaranteed to
        # be a bare tensor.
        latent = None
        for attribute in ("images", "frames"):
            latent = getattr(result, attribute, None)
            if latent is not None:
                break
        if latent is None:
            raise RuntimeError(
                f"{type(result).__name__} exposes neither images nor frames"
            )
        while not torch.is_tensor(latent):
            latent = latent[0]
        digest = hashlib.sha256(
            latent.detach().to(torch.float32).cpu().numpy().tobytes()
        ).hexdigest()
        # Boundary i..i+1 ran at the quota in force at boundary i.
        return digest, [
            (at_quota[i], events[i].elapsed_time(events[i + 1]) / 1000.0)
            for i in range(len(events) - 1)
        ]

    # Steady state: the table a scheduler would already have.
    steady: dict[int, list[float]] = {}
    digests: dict[str, str] = {}
    for units in quotas:
        for _ in range(args.warmup):
            run(None, units)
        samples: list[float] = []
        for _ in range(args.steady_repeats):
            digest, rows = run(None, units)
            digests[f"steady_{units}"] = digest
            samples += [seconds for _, seconds in rows]
        steady[units] = samples
        print(f"  steady {units:2d} units: per-step median "
              f"{statistics.median(samples)*1000:7.2f} ms  n={len(samples)}",
              flush=True)

    steady_median = {u: statistics.median(v) for u, v in steady.items()}

    plan = schedule_for(args.steps, quotas, args.dwell)
    for _ in range(args.warmup):
        run(plan, None)
    observed: list[tuple[int, float]] = []
    for _ in range(args.switch_repeats):
        digest, rows = run(plan, None)
        digests["switching"] = digest
        observed += rows

    # A step is predicted to cost what that quota costs when nothing moved.
    predicted = [steady_median[units] for units, _ in observed]
    actual = [seconds for _, seconds in observed]
    score = mape(actual, predicted)

    # Split by whether the step is the first at a new quota: a transient
    # confined to the first step is a different fact from a persistent one.
    boundaries = {
        index for index in range(1, len(plan)) if plan[index] != plan[index - 1]
    }
    per_call = len(plan) - 1
    first_after, settled = [], []
    for position, (units, seconds) in enumerate(observed):
        # Interval `position` within a call is step position+1, so it is the
        # first step at a new quota exactly when position+1 is a boundary.
        step_index = (position % per_call) + 1
        (first_after if step_index in boundaries else settled).append(
            (seconds, steady_median[units])
        )

    report = {
        "schema_version": SCHEMA_VERSION,
        "model": args.model,
        "repo": repo,
        "quotas": quotas,
        "masks": {str(k): v for k, v in masks.items()},
        "dwell": args.dwell,
        "steps": args.steps,
        "height": args.height,
        "width": args.width,
        "predictor": "steady-state per-step median at the step's own quota",
        "predictor_free_parameters": 0,
        "steady_per_step_median_s": {str(k): v for k, v in
                                     steady_median.items()},
        "steady_samples": {str(u): len(v) for u, v in steady.items()},
        "observed_steps": len(observed),
        "mape": score,
        "meets_threshold": score <= args.mape_threshold,
        "torch": torch.__version__,
        "device_name": torch.cuda.get_device_name(0),
        "latent_digests": digests,
        # Same seed, same schedule of arithmetic, so moving the work between
        # streams must not change the result. If it does, the timings are
        # measuring something that is not the same computation.
        "switching_matches_steady": (
            digests.get("switching") is not None
            and digests["switching"] in {
                v for k, v in digests.items() if k.startswith("steady_")
            }
        ),
    }
    for label, rows in (("first_step_after_a_change", first_after),
                        ("settled_steps", settled)):
        if rows:
            report[label] = {
                "steps": len(rows),
                "mape": mape([a for a, _ in rows], [p for _, p in rows]),
            }

    print(f"\nMAPE {score*100:.2f}%   threshold {args.mape_threshold*100:.0f}%"
          f"   steps={len(observed)}")
    for label in ("first_step_after_a_change", "settled_steps"):
        if label in report:
            print(f"  {label:28s} n={report[label]['steps']:4d}  "
                  f"MAPE {report[label]['mape']*100:.2f}%")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(canonical_json(report) + "\n")
    print(f"report: {out}")
    return 0 if report["meets_threshold"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
