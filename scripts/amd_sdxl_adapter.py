#!/usr/bin/env python3
"""SDXL as a step-at-a-time adapter, and proof that stepping is exact.

The executor needs ``initial_state``, ``denoise_one`` and ``decode``.
diffusers offers neither: ``pipeline(...)`` runs every step and returns,
and ``callback_on_step_end`` fires *inside* that call, so a scheduler
using it is still trapped in someone else's loop. The way out is to
drive the unet and the scheduler directly, which is what a runtime with
step-granular preemption has to do anyway.

Doing that means owning three pieces of state, and the reason each one
matters is a way this can fail silently:

  * the latent -- obvious, and the only one people remember.
  * the scheduler's ``_step_index``. EulerDiscreteScheduler advances it
    per call and reads ``sigmas[_step_index]``. Restore the latent
    without it and every remaining step uses the wrong sigma: the image
    is still an image, and nothing raises.
  * the generator. Ancestral samplers draw noise per step; the same seed
    with a re-created generator produces a different sequence from the
    point of resumption.

So the acceptance is not "does it run" but "does interrupting it change
the result". This script measures exactly that: one uninterrupted run,
one interrupted after every step, and a byte comparison of the latents.
A tolerance would defeat the purpose -- Gate C's runtime criterion is a
hash, and a hash does not have a tolerance.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from burstserve.executor import StepExecutor, StepState  # noqa: E402

WORDS = 4
hip = ctypes.CDLL("libamdhip64.so")
hip.hipExtStreamCreateWithCUMask.restype = ctypes.c_int
hip.hipExtStreamCreateWithCUMask.argtypes = [
    ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint32,
    ctypes.POINTER(ctypes.c_uint32),
]


def masked_stream(units: int):
    mask = (1 << units) - 1
    handle = ctypes.c_void_p()
    words = (ctypes.c_uint32 * WORDS)(
        *[(mask >> (32 * i)) & 0xFFFFFFFF for i in range(WORDS)]
    )
    if hip.hipExtStreamCreateWithCUMask(ctypes.byref(handle), WORDS,
                                        words) != 0:
        raise RuntimeError(f"could not create a {units}-unit stream")
    return handle


class SdxlStepAdapter:
    """Drives SDXL one denoising step per call.

    Holds the pipeline and the immutable conditioning; everything that
    changes between steps lives in the StepState the executor passes
    back, so two requests can share one adapter without sharing progress.
    """

    def __init__(self, pipeline, *, height: int, width: int, steps: int,
                 seed: int, guidance_scale: float = 5.0):
        import torch

        self.pipeline = pipeline
        self.height = height
        self.width = width
        self.steps = steps
        self.seed = seed
        self.guidance_scale = guidance_scale
        self.device = pipeline._execution_device
        self.torch = torch

        with torch.no_grad():
            (self.prompt_embeds, self.negative_prompt_embeds,
             self.pooled, self.negative_pooled) = pipeline.encode_prompt(
                prompt="a quiet street at dusk",
                device=self.device,
                num_images_per_prompt=1,
                do_classifier_free_guidance=True,
            )
        add_time_ids = pipeline._get_add_time_ids(
            (height, width), (0, 0), (height, width),
            dtype=self.prompt_embeds.dtype,
            text_encoder_projection_dim=int(self.pooled.shape[-1]),
        ).to(self.device)
        self.embeds = torch.cat(
            [self.negative_prompt_embeds, self.prompt_embeds]
        )
        self.added = {
            "text_embeds": torch.cat([self.negative_pooled, self.pooled]),
            "time_ids": torch.cat([add_time_ids, add_time_ids]),
        }

    # -- ModelAdapter --------------------------------------------------

    def initial_state(self, request) -> StepState:
        torch = self.torch
        scheduler = self.pipeline.scheduler
        scheduler.set_timesteps(self.steps, device=self.device)
        generator = torch.Generator(device=self.device).manual_seed(self.seed)
        channels = self.pipeline.unet.config.in_channels
        factor = self.pipeline.vae_scale_factor
        latents = torch.randn(
            (1, channels, self.height // factor, self.width // factor),
            generator=generator, device=self.device,
            dtype=self.prompt_embeds.dtype,
        ) * scheduler.init_noise_sigma
        return StepState(
            step_index=0,
            latent=latents,
            rng_state=generator.get_state(),
            extra={
                "timesteps": scheduler.timesteps.clone(),
                # Saved because restoring the latent without it makes
                # every later step read the wrong sigma, silently.
                "scheduler_step_index": scheduler._step_index,
            },
        )

    def denoise_one(self, state: StepState, *, quota_units: int) -> StepState:
        torch = self.torch
        scheduler = self.pipeline.scheduler
        timesteps = state.extra["timesteps"]
        timestep = timesteps[state.step_index]

        # Restore the scheduler and generator before touching the model:
        # one adapter serves several requests, so whatever ran last left
        # its own state behind.
        scheduler.timesteps = timesteps
        scheduler._step_index = state.extra["scheduler_step_index"]
        generator = torch.Generator(device=self.device)
        generator.set_state(state.rng_state)

        latents = state.latent
        with torch.no_grad():
            model_input = torch.cat([latents] * 2)
            model_input = scheduler.scale_model_input(model_input, timestep)
            noise_pred = self.pipeline.unet(
                model_input, timestep,
                encoder_hidden_states=self.embeds,
                added_cond_kwargs=self.added,
                return_dict=False,
            )[0]
            uncond, cond = noise_pred.chunk(2)
            noise_pred = uncond + self.guidance_scale * (cond - uncond)
            latents = scheduler.step(noise_pred, timestep, latents,
                                     generator=generator,
                                     return_dict=False)[0]

        return StepState(
            step_index=state.step_index + 1,
            latent=latents,
            rng_state=generator.get_state(),
            extra={
                "timesteps": timesteps,
                "scheduler_step_index": scheduler._step_index,
            },
        )

    def decode(self, state: StepState):
        """Return the latent, not an image.

        The acceptance compares latents. Decoding introduces the VAE,
        whose cost does not scale with quota the way the steps do and
        whose output would blur a comparison meant to be exact.
        """
        return state.latent


def digest_of(latent) -> str:
    return hashlib.sha256(
        latent.detach().to("cpu").float().numpy().tobytes()
    ).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--steps", type=int, default=8)
    parser.add_argument("--height", type=int, default=768)
    parser.add_argument("--width", type=int, default=768)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--units", type=int, default=32)
    parser.add_argument("--interrupt-units", default="4,8,16,32",
                        help="quotas to cycle through on the interrupted "
                             "run, so it is preempted AND rescheduled at a "
                             "different width every step")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    import torch
    from diffusers import DiffusionPipeline

    pipeline = DiffusionPipeline.from_pretrained(
        "stabilityai/stable-diffusion-xl-base-1.0",
        torch_dtype=torch.float16, variant="fp16", use_safetensors=True,
    ).to("cuda")
    pipeline.set_progress_bar_config(disable=True)

    def fresh_adapter():
        return SdxlStepAdapter(pipeline, height=args.height, width=args.width,
                               steps=args.steps, seed=args.seed)

    # Uninterrupted, whole die.
    plain = StepExecutor(object(), fresh_adapter(), total_steps=args.steps)
    plain.prepare()
    while plain.run_step(quota_units=args.units):
        pass
    plain_latent = plain.finalize()
    plain_digest = digest_of(plain_latent)
    print(f"uninterrupted: {plain_digest[:32]}", flush=True)

    # Interrupted after every step, and rescheduled at a different quota
    # each time. Varying the quota matters: if a step's result depended
    # on the width it ran at, every scheduling decision would also be an
    # image decision.
    quotas = [int(q) for q in args.interrupt_units.split(",")]
    broken = StepExecutor(object(), fresh_adapter(), total_steps=args.steps)
    broken.prepare()
    index = 0
    while True:
        more = broken.run_step(quota_units=quotas[index % len(quotas)])
        index += 1
        if not more:
            break
        broken.resume(broken.suspend())
    broken_latent = broken.finalize()
    broken_digest = digest_of(broken_latent)
    print(f"interrupted:   {broken_digest[:32]}", flush=True)

    identical = plain_digest == broken_digest
    max_abs = float((plain_latent - broken_latent).abs().max())
    payload = {
        "schema_version": "burstserve.amd-step-adapter/v1",
        "question": ("does suspending and rescheduling between denoising "
                     "steps change the latent"),
        "steps": args.steps,
        "workpoint": f"{args.width}x{args.height}",
        "seed": args.seed,
        "uninterrupted_quota": args.units,
        "interrupted_quotas": quotas,
        "suspensions": broken.suspensions,
        "resumptions": broken.resumptions,
        "uninterrupted_digest": plain_digest,
        "interrupted_digest": broken_digest,
        "identical": identical,
        "max_abs_difference": max_abs,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"\nsuspensions {broken.suspensions}, "
          f"identical={identical}, max|diff|={max_abs:g}")
    print(f"-> {args.out}")
    return 0 if identical else 1


if __name__ == "__main__":
    raise SystemExit(main())
