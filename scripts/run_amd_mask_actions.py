#!/usr/bin/env python3
"""Week 9-10 on the card: do the masks hold, and does changing them hurt?

Two acceptance clauses need hardware and cannot be checked apart:

  * every action's measured SM set matches the manifest exactly. The
    runtime lays a round's quotas across the die and reads back what the
    driver installed; this compares that readback against the manifest's
    own bit list, so "the mask we asked for" and "the mask the die has"
    are two separate claims rather than one assumption.

  * reconfiguration affects only later kernels. A mask change that
    disturbed work already in flight would show up as a corrupted
    latent, so the test is not a timing curve but a byte comparison: the
    same request, once with the quota changing under it every step and
    once at a fixed width, must produce the same latent.

The second is the more interesting of the two. A runtime can change
masks between steps and still be wrong if the change lands while a
kernel is running, and the symptom is not an error -- it is an image
that is slightly different and entirely plausible.

Each round's attestation is kept whether it passes or not, so a partial
failure is locatable rather than a single verdict.
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
sys.path.insert(0, str(REPO / "scripts"))

from burstserve.executor import StepExecutor              # noqa: E402
from burstserve.masked_streams import MaskedStreamPool    # noqa: E402
from burstserve.policies import probing_partitioning      # noqa: E402
from burstserve.queues import QueuedRequest               # noqa: E402
from burstserve.runtime import Runtime                    # noqa: E402

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


def hip_stream_factory(mask: int):
    """Create a masked stream and report the mask the driver installed.

    The readback is the evidence for the manifest clause. A driver that
    quietly widened a mask would produce a co-run with an unusually low
    externality -- a number that reads as good news.
    """
    handle = ctypes.c_void_p()
    words = (ctypes.c_uint32 * WORDS)(
        *[(mask >> (32 * i)) & 0xFFFFFFFF for i in range(WORDS)]
    )
    rc = hip.hipExtStreamCreateWithCUMask(ctypes.byref(handle), WORDS, words)
    if rc != 0:
        raise RuntimeError(f"hipExtStreamCreateWithCUMask({hex(mask)}) "
                           f"rc={rc}")
    buffer = (ctypes.c_uint32 * WORDS)()
    if hip.hipExtStreamGetCUMask(handle, WORDS, buffer) != 0:
        raise RuntimeError("mask readback failed")
    installed = 0
    for index, word in enumerate(buffer):
        installed |= word << (32 * index)
    return handle, installed


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
    parser.add_argument("--manifest", type=Path,
                        default=REPO / "experiments" / "manifests"
                        / "amd_r9700_gfx1201.json")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    import torch
    from diffusers import DiffusionPipeline
    from amd_sdxl_adapter import SdxlStepAdapter

    manifest = json.loads(args.manifest.read_text())
    declared_bits = manifest["matrix"]["mask_bits"]
    declared_units = manifest["hardware"]["maskable_units"]

    print("loading sdxl ...", flush=True)
    pipeline = DiffusionPipeline.from_pretrained(
        "stabilityai/stable-diffusion-xl-base-1.0",
        torch_dtype=torch.float16, variant="fp16", use_safetensors=True,
    ).to("cuda")
    pipeline.set_progress_bar_config(disable=True)

    def fresh_adapter(seed_offset=0):
        return SdxlStepAdapter(pipeline, height=args.height,
                               width=args.width, steps=args.steps,
                               seed=args.seed + seed_offset)

    # --- clause 1: the installed masks are the manifest's bits ---------
    pool = MaskedStreamPool(hip_stream_factory,
                            maskable_units=declared_units)
    runtime = Runtime(probing_partitioning, stream_pool=pool)
    for index in range(2):
        request = QueuedRequest(request_id=index, tenant=f"t{index}",
                                model="sdxl", arrival_s=0.0,
                                steps=args.steps)
        runtime.submit(request, StepExecutor(request, fresh_adapter(index),
                                             total_steps=args.steps))
    now = 0.0
    while not runtime.all_finished():
        runtime.tick(now)
        now += 0.25

    covered: set[int] = set()
    disjoint_every_round = True
    for attestation in runtime.mask_attestations:
        disjoint_every_round &= attestation["disjoint"]
        for mask in attestation["masks"].values():
            value = int(mask, 16)
            covered |= {b for b in range(declared_units)
                        if value >> b & 1}
    outside = sorted(b for b in covered if b not in declared_bits)
    print(f"  masks touched {len(covered)} distinct bits, "
          f"outside the manifest: {outside or 'none'}", flush=True)

    # --- clause 2: reconfiguration does not disturb in-flight work -----
    # Same request twice: once at a fixed width, once with the width
    # changing every step. A change landing mid-kernel would corrupt the
    # latent, and the corruption would look like a plausible image.
    fixed = StepExecutor(object(), fresh_adapter(), total_steps=args.steps)
    fixed.prepare()
    fixed_stream = pool.for_quota(32)
    fixed.adapter.stream = fixed_stream.handle
    while fixed.run_step(quota_units=32):
        pass
    fixed_latent = fixed.finalize()

    varying = StepExecutor(object(), fresh_adapter(), total_steps=args.steps)
    varying.prepare()
    widths = [4, 32, 8, 24, 16, 32, 12, 28]
    for index in range(args.steps):
        units = widths[index % len(widths)]
        # A different mask for every step, from the pool, while the
        # request is mid-flight.
        varying.adapter.stream = pool.for_quota(units).handle
        more = varying.run_step(quota_units=units)
        if more:
            varying.resume(varying.suspend())
    varying_latent = varying.finalize()

    identical = digest_of(fixed_latent) == digest_of(varying_latent)
    max_abs = float((fixed_latent - varying_latent).abs().max())
    print(f"  fixed vs varying mask: identical={identical} "
          f"max|diff|={max_abs:g}", flush=True)

    payload = {
        "schema_version": "burstserve.amd-mask-actions/v1",
        "manifest": str(args.manifest.relative_to(REPO)),
        "declared_maskable_units": declared_units,
        "rounds_attested": len(runtime.mask_attestations),
        "bits_touched": sorted(covered),
        "bits_outside_manifest": outside,
        "masks_match_manifest": not outside,
        "disjoint_every_round": disjoint_every_round,
        "mask_attestations": runtime.mask_attestations,
        "streams_created": pool.creations,
        "stream_attestation": pool.attestation(),
        "reconfiguration": {
            "fixed_width": 32,
            "varying_widths": widths[:args.steps],
            "fixed_digest": digest_of(fixed_latent),
            "varying_digest": digest_of(varying_latent),
            "identical": identical,
            "max_abs_difference": max_abs,
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2) + "\n")

    ok = (not outside) and disjoint_every_round and identical
    print(f"\nmasks match manifest: {not outside}")
    print(f"disjoint every round: {disjoint_every_round}")
    print(f"reconfiguration safe: {identical}")
    print(f"streams created: {pool.creations}")
    print(f"-> {args.out}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
