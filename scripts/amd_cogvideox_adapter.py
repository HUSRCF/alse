#!/usr/bin/env python3
"""CogVideoX-2b as a step-at-a-time adapter, symmetric with the SDXL one.

Every per-step measurement so far has paired SDXL with itself, which is
the least favourable case partitioning can be given: two tenants wanting
the same resource in the same way at the same instant. The scheduler was
designed for the opposite -- tenants whose demands differ -- and no
per-step measurement covers that, because ``run_amd_inproc_corun`` drives
CogVideoX through a whole ``pipeline(...)`` call and a call is the wrong
granularity. Call-level numbers are what the externality table was built
from, and applying them to a per-step decision is the error that made
this project report a throughput gain where the die loses 12.8%.

So the pairing needs an adapter with the same contract as
``SdxlStepAdapter``: ``initial_state`` / ``denoise_one`` / ``decode``,
device-time measurement read one step late, and a masked stream the
runtime assigns per step. Three things differ from SDXL and each is a
way a copied implementation would be quietly wrong:

  * **the scheduler is DDIM, not Euler.** ``CogVideoXDDIMScheduler.step``
    derives the previous timestep from the timestep it is given rather
    than from an internal cursor, so there is less hidden state -- but
    ``_step_index`` is saved and restored anyway where it exists, because
    "this scheduler happens not to need it" is a fact about one version.
  * **the latent is 5-D** (batch, frames, channels, h, w), and the frame
    count is a latent count derived from the pixel count. Getting that
    wrong produces a working run at the wrong shape, which reads as a
    perfectly plausible cost measurement.
  * **rotary embeddings are positional state built once per shape.**
    CogVideoX-2b does not use them and 5b does, so it is computed from
    the transformer's own config rather than assumed absent.

``decode`` returns the latent, as SDXL's does. The VAE is what diluted
the call-level numbers in the first place.
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from burstserve.executor import StepState  # noqa: E402


class CogVideoXStepAdapter:
    """Drives CogVideoX-2b one denoising step per call."""

    def __init__(self, pipeline, *, frames: int = 9, height: int = 480,
                 width: int = 720, steps: int = 8, seed: int = 0,
                 guidance_scale: float = 6.0):
        import torch

        self.pipeline = pipeline
        # Its own scheduler, for the reason SdxlStepAdapter documents:
        # two adapters stepping concurrently write scheduler state on
        # every call, and sharing one produced a hang at 3% GPU rather
        # than an exception.
        self.scheduler = type(pipeline.scheduler).from_config(
            pipeline.scheduler.config)
        self.frames = frames
        self.height = height
        self.width = width
        self.steps = steps
        self.seed = seed
        self.guidance_scale = guidance_scale
        self.device = pipeline._execution_device
        self.torch = torch

        self.last_step_seconds: float | None = None
        # The quota that reading was taken at. Without it, an
        # adapter re-granted a different width reports the old
        # width's cost -- the deferred read usually finds the
        # previous step still in flight, so nothing overwrites the
        # stale value. See the SDXL adapter for the measurement.
        self.last_step_units: int | None = None
        self._pending_events: tuple | None = None
        self.stream = None
        self._external_streams: dict[int, object] = {}
        self._previous_stream = None

        # Encoded once, on whichever thread builds the adapter. The Rust
        # tokenizer is not reentrant and two adapters encoding
        # concurrently raise "Already borrowed", so adapters are
        # constructed on the main thread and only stepped on workers.
        with torch.no_grad():
            prompt_embeds, negative_prompt_embeds = pipeline.encode_prompt(
                prompt="a quiet street at dusk",
                negative_prompt=None,
                do_classifier_free_guidance=True,
                num_videos_per_prompt=1,
                device=self.device,
                dtype=pipeline.transformer.dtype,
            )
        self.embeds = torch.cat([negative_prompt_embeds, prompt_embeds],
                                dim=0)

        # The pipeline pads the frame count up when the transformer
        # patches over time. CogVideoX-2b sets patch_size_t to None, so
        # this is a no-op there -- kept because a silently different
        # frame count is a silently different cost.
        temporal = pipeline.vae_scale_factor_temporal
        latent_frames = (frames - 1) // temporal + 1
        patch_size_t = getattr(pipeline.transformer.config, "patch_size_t",
                               None)
        self.pixel_frames = frames
        if patch_size_t is not None and latent_frames % patch_size_t:
            self.pixel_frames += (
                (patch_size_t - latent_frames % patch_size_t) * temporal)

    def _external(self, handle):
        pointer = getattr(handle, "value", handle)
        wrapper = self._external_streams.get(pointer)
        if wrapper is None:
            wrapper = self.torch.cuda.ExternalStream(pointer)
            self._external_streams[pointer] = wrapper
        return wrapper

    # -- ModelAdapter --------------------------------------------------

    def initial_state(self, request) -> StepState:
        torch = self.torch
        pipeline = self.pipeline
        scheduler = self.scheduler
        scheduler.set_timesteps(self.steps, device=self.device)
        generator = torch.Generator(device=self.device).manual_seed(self.seed)

        # prepare_latents is the pipeline's own, so the shape and the
        # noise scaling come from the model rather than from a
        # reconstruction of them here.
        latents = pipeline.prepare_latents(
            1, pipeline.transformer.config.in_channels, self.pixel_frames,
            self.height, self.width, self.embeds.dtype, self.device,
            generator, None,
        )

        rotary = None
        if pipeline.transformer.config.use_rotary_positional_embeddings:
            rotary = pipeline._prepare_rotary_positional_embeddings(
                self.height, self.width, latents.size(1), self.device)

        return StepState(
            step_index=0,
            latent=latents,
            rng_state=generator.get_state(),
            extra={
                "timesteps": scheduler.timesteps.clone(),
                "scheduler_step_index": getattr(scheduler, "_step_index",
                                                None),
                "rotary": rotary,
            },
        )

    def denoise_one(self, state: StepState, *, quota_units: int) -> StepState:
        torch = self.torch
        scheduler = self.scheduler
        timesteps = state.extra["timesteps"]
        timestep = timesteps[state.step_index]

        scheduler.timesteps = timesteps
        if state.extra["scheduler_step_index"] is not None:
            scheduler._step_index = state.extra["scheduler_step_index"]
        generator = torch.Generator(device=self.device)
        generator.set_state(state.rng_state)

        # Read the events from the step before last. Synchronising on the
        # step just issued would drain the pipeline and charge the drain
        # to the measurement.
        if self._pending_events is not None:
            previous_start, previous_end, previous_units = self._pending_events
            if previous_end.query():
                self.last_step_seconds = (
                    previous_start.elapsed_time(previous_end) / 1000.0)
                self.last_step_units = previous_units
                self._pending_events = None

        latents = state.latent
        current = (self._external(self.stream)
                   if self.stream is not None else None)
        if (current is not None and self._previous_stream is not None
                and current != self._previous_stream):
            # A step reads what the previous step wrote. Switching masks
            # without ordering the change produced NaN, not a slightly
            # different result.
            handover = torch.cuda.Event()
            handover.record(self._previous_stream)
            current.wait_event(handover)
        context = (torch.cuda.stream(current)
                   if current is not None else contextlib.nullcontext())

        cache = getattr(self.pipeline.transformer, "cache_context", None)
        start = torch.cuda.Event(enable_timing=True)
        end = torch.cuda.Event(enable_timing=True)
        with context:
            start.record()
            with torch.no_grad():
                model_input = torch.cat([latents] * 2)
                model_input = scheduler.scale_model_input(model_input,
                                                          timestep)
                broadcast = timestep.expand(model_input.shape[0])
                caching = (cache("cond_uncond") if cache is not None
                           else contextlib.nullcontext())
                with caching:
                    noise_pred = self.pipeline.transformer(
                        hidden_states=model_input,
                        encoder_hidden_states=self.embeds,
                        timestep=broadcast,
                        image_rotary_emb=state.extra["rotary"],
                        return_dict=False,
                    )[0]
                noise_pred = noise_pred.float()
                uncond, cond = noise_pred.chunk(2)
                noise_pred = uncond + self.guidance_scale * (cond - uncond)
                latents = scheduler.step(noise_pred, timestep, latents,
                                         eta=0.0, generator=generator,
                                         return_dict=False)[0]
                # The pipeline's own ``latents = latents.to(
                # prompt_embeds.dtype)``. Without it the latent comes back
                # fp32 -- the scheduler mixes an fp16 sample with the
                # fp32 model output and does not cast -- and the next
                # step's first Linear fails on fp32 activations against
                # fp16 weights. Copied rather than reasoned about, because
                # any other choice here silently changes the arithmetic
                # relative to the reference implementation.
                latents = latents.to(self.embeds.dtype)
            end.record()
        self._pending_events = (start, end, quota_units)
        if current is not None:
            self._previous_stream = current
        if (self.last_step_seconds is None
                or self.last_step_units != quota_units):
            # The first step has nothing in front of it to hide the wait
            # behind, and an unmeasured step would be charged from the
            # cost model -- the one thing the ledger must never do.
            self.drain_timing()

        return StepState(
            step_index=state.step_index + 1,
            latent=latents,
            rng_state=generator.get_state(),
            extra={
                "timesteps": timesteps,
                "scheduler_step_index": getattr(scheduler, "_step_index",
                                                None),
                "rotary": state.extra["rotary"],
            },
        )

    def drain_timing(self) -> float | None:
        if self._pending_events is None:
            return self.last_step_seconds
        start, end, units = self._pending_events
        end.synchronize()
        self.last_step_seconds = start.elapsed_time(end) / 1000.0
        self.last_step_units = units
        self._pending_events = None
        return self.last_step_seconds

    def decode(self, state: StepState):
        """Return the latent. Decoding is what diluted the call-level
        numbers this adapter exists to replace."""
        return state.latent
