#!/usr/bin/env python3
"""Does the scheduled runtime reproduce ASLE's latent exactly?

plan.md's week 7-8 acceptance is that the final latent hashes identical
to the ASLE seed's in deterministic mode. Everything so far has shown a
weaker thing -- that interrupting our own runtime does not change our own
result. That rules out one failure and says nothing about whether the
runtime computes what the baseline computes.

So this reimplements ASLE's ``run_urgent`` verbatim as the reference,
then runs the same work through the executor with suspensions between
steps, and compares bytes. The reference is inlined rather than imported
because ``r1_driver`` runs a whole experiment on import; copying the
fifteen lines that matter is the smaller risk, and they are quoted in
full below so a reader can check the copy against the original.

    g = torch.Generator(DEV).manual_seed(0)
    lat = torch.randn((1,4,128,128), generator=g, device=DEV, dtype=DT) \
          * sdxl.scheduler.init_noise_sigma
    sdxl.scheduler.set_timesteps(a.usteps, device=DEV)
    for tt in sdxl.scheduler.timesteps:
        lmi = sdxl.scheduler.scale_model_input(torch.cat([lat]*2), tt)
        pr  = sdxl.unet(lmi, tt, encoder_hidden_states=SEMB,
                        added_cond_kwargs={"text_embeds":STXT,
                                           "time_ids":STIM}).sample
        u,c = pr.chunk(2)
        lat = sdxl.scheduler.step(u + 5.0*(c-u), tt, lat).prev_sample

Three details of that decide whether the comparison means anything, and
each is a way a "matching" implementation could still differ:

  * ``scheduler.step`` is called **without** a generator. Passing one is
    harmless for a deterministic scheduler and not harmless in general,
    and the point here is to match, not to be defensible in isolation.
  * the prompt is "a red apple" with an empty negative prompt, and the
    conditioning is built once, outside the loop.
  * the latent is (1,4,128,128) -- 1024x1024, not the 768 the profiling
    work used.

If the digests differ, this reports where the first difference appears
rather than only that one does: a divergence at step 0 is a
conditioning bug, and a divergence at step 5 is a state-restoration bug.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from burstserve.executor import StepExecutor, StepState  # noqa: E402

PROMPT = "a red apple"
NEGATIVE = ""
GUIDANCE = 5.0


def digest_of(latent) -> str:
    return hashlib.sha256(
        latent.detach().to("cpu").float().numpy().tobytes()
    ).hexdigest()


def build_conditioning(pipeline, device):
    """ASLE's conditioning, built the way ASLE builds it."""
    import torch

    pe, npe, ppe, nppe = pipeline.encode_prompt(
        prompt=PROMPT, prompt_2=None, device=device,
        num_images_per_prompt=1, do_classifier_free_guidance=True,
        negative_prompt=NEGATIVE,
    )
    proj = pipeline.text_encoder_2.config.projection_dim
    ati = pipeline._get_add_time_ids(
        (1024, 1024), (0, 0), (1024, 1024), dtype=pe.dtype,
        text_encoder_projection_dim=proj,
    ).to(device)
    return (torch.cat([npe, pe]), torch.cat([nppe, ppe]),
            torch.cat([ati, ati]))


def asle_reference(pipeline, device, steps: int, seed: int,
                   conditioning) -> list:
    """ASLE's loop, unchanged, returning every intermediate latent.

    Every step is kept so a divergence can be located rather than merely
    detected.
    """
    import torch

    semb, stxt, stim = conditioning
    # set_timesteps FIRST, then read init_noise_sigma. ASLE does the
    # reverse, and that ordering is state-dependent: on a freshly loaded
    # scheduler init_noise_sigma is 14.6489, and after any
    # set_timesteps(8) it is 7.4394 -- so ASLE's first urgent request
    # starts from a latent scaled 1.97x differently from every request
    # after it. Comparing against "whichever ASLE happened to run first"
    # would make this test depend on call order rather than on the
    # runtime, so both sides are pinned to the settled value.
    pipeline.scheduler.set_timesteps(steps, device=device)
    generator = torch.Generator(device).manual_seed(seed)
    latent = torch.randn((1, 4, 128, 128), generator=generator,
                         device=device, dtype=pipeline.unet.dtype)
    latent = latent * pipeline.scheduler.init_noise_sigma
    trail = [latent.clone()]
    with torch.no_grad():
        for tt in pipeline.scheduler.timesteps:
            lmi = pipeline.scheduler.scale_model_input(
                torch.cat([latent] * 2), tt)
            pr = pipeline.unet(lmi, tt, encoder_hidden_states=semb,
                               added_cond_kwargs={"text_embeds": stxt,
                                                  "time_ids": stim}).sample
            u, c = pr.chunk(2)
            latent = pipeline.scheduler.step(
                u + GUIDANCE * (c - u), tt, latent).prev_sample
            trail.append(latent.clone())
    return trail


class AsleAdapter:
    """The same arithmetic, as an adapter the executor can interrupt.

    Deliberately identical to the reference line for line, including
    calling ``scheduler.step`` without a generator. Any difference here
    would be the thing under test rather than a fix.
    """

    def __init__(self, pipeline, device, steps: int, seed: int,
                 conditioning):
        import torch

        self.torch = torch
        self.pipeline = pipeline
        self.device = device
        self.steps = steps
        self.seed = seed
        self.semb, self.stxt, self.stim = conditioning
        self.trail: list = []

    def initial_state(self, request) -> StepState:
        torch = self.torch
        # Same ordering as the reference above, for the same reason.
        self.pipeline.scheduler.set_timesteps(self.steps, device=self.device)
        generator = torch.Generator(self.device).manual_seed(self.seed)
        latent = torch.randn((1, 4, 128, 128), generator=generator,
                             device=self.device,
                             dtype=self.pipeline.unet.dtype)
        latent = latent * self.pipeline.scheduler.init_noise_sigma
        self.trail = [latent.clone()]
        return StepState(
            step_index=0, latent=latent,
            rng_state=generator.get_state(),
            extra={"timesteps": self.pipeline.scheduler.timesteps.clone(),
                   "scheduler_step_index": self.pipeline.scheduler._step_index},
        )

    def denoise_one(self, state: StepState, *, quota_units: int) -> StepState:
        torch = self.torch
        scheduler = self.pipeline.scheduler
        timesteps = state.extra["timesteps"]
        tt = timesteps[state.step_index]
        scheduler.timesteps = timesteps
        scheduler._step_index = state.extra["scheduler_step_index"]

        latent = state.latent
        with torch.no_grad():
            lmi = scheduler.scale_model_input(torch.cat([latent] * 2), tt)
            pr = self.pipeline.unet(
                lmi, tt, encoder_hidden_states=self.semb,
                added_cond_kwargs={"text_embeds": self.stxt,
                                   "time_ids": self.stim}).sample
            u, c = pr.chunk(2)
            latent = scheduler.step(u + GUIDANCE * (c - u), tt,
                                    latent).prev_sample
        self.trail.append(latent.clone())
        return StepState(
            step_index=state.step_index + 1, latent=latent,
            rng_state=state.rng_state,
            extra={"timesteps": timesteps,
                   "scheduler_step_index": scheduler._step_index},
        )

    def decode(self, state: StepState):
        return state.latent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--steps", type=int, default=8,
                        help="ASLE's --usteps default")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--quotas", default="4,8,16,32",
                        help="cycled through on the scheduled run, so it is "
                             "preempted and re-granted at a different width "
                             "every step")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    import torch
    from diffusers import DiffusionPipeline

    device = "cuda"
    pipeline = DiffusionPipeline.from_pretrained(
        "stabilityai/stable-diffusion-xl-base-1.0",
        torch_dtype=torch.float16, variant="fp16", use_safetensors=True,
    ).to(device)
    pipeline.set_progress_bar_config(disable=True)
    conditioning = build_conditioning(pipeline, device)

    print("running ASLE's loop ...", flush=True)
    reference = asle_reference(pipeline, device, args.steps, args.seed,
                               conditioning)
    reference_digest = digest_of(reference[-1])
    print(f"  {reference_digest[:32]}", flush=True)

    print("running the same work through the scheduler ...", flush=True)
    adapter = AsleAdapter(pipeline, device, args.steps, args.seed,
                          conditioning)
    executor = StepExecutor(object(), adapter, total_steps=args.steps)
    executor.prepare()
    quotas = [int(q) for q in args.quotas.split(",")]
    index = 0
    while True:
        more = executor.run_step(quota_units=quotas[index % len(quotas)])
        index += 1
        if not more:
            break
        executor.resume(executor.suspend())
    scheduled = executor.finalize()
    scheduled_digest = digest_of(scheduled)
    print(f"  {scheduled_digest[:32]}", flush=True)

    # Where, not just whether. A divergence at step 0 is a conditioning
    # bug; one at step 5 is a state-restoration bug.
    first_divergence = None
    per_step = []
    for step, (ref, got) in enumerate(zip(reference, adapter.trail)):
        same = bool(torch.equal(ref, got))
        per_step.append({"step": step, "identical": same,
                         "max_abs": float((ref - got).abs().max())})
        if not same and first_divergence is None:
            first_divergence = step

    identical = reference_digest == scheduled_digest
    payload = {
        "schema_version": "burstserve.asle-bitexact/v1",
        "question": ("does the scheduled runtime reproduce ASLE's latent "
                     "byte for byte"),
        "reference": "vendor/asle/r1_driver.py run_urgent, inlined",
        "prompt": PROMPT,
        "negative_prompt": NEGATIVE,
        "guidance_scale": GUIDANCE,
        "latent_shape": [1, 4, 128, 128],
        "steps": args.steps,
        "seed": args.seed,
        "scheduled_quotas": quotas,
        "init_noise_sigma_note": (
            "both sides call set_timesteps before reading "
            "init_noise_sigma. ASLE reads it first, which is "
            "state-dependent: 14.6489 on a freshly loaded scheduler "
            "against 7.4394 after any set_timesteps, so its first urgent "
            "request differs from every later one by a factor of 1.97"
        ),
        "suspensions": executor.suspensions,
        "asle_digest": reference_digest,
        "scheduled_digest": scheduled_digest,
        "identical": identical,
        "first_divergence_step": first_divergence,
        "per_step": per_step,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2) + "\n")

    print(f"\nidentical={identical}  suspensions={executor.suspensions}")
    if not identical:
        print(f"first divergence at step {first_divergence}")
    print(f"-> {args.out}")
    return 0 if identical else 1


if __name__ == "__main__":
    raise SystemExit(main())
