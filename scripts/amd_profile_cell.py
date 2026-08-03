"""Run one AMD profiling cell and report its latency distribution.

One cell is one process with a fixed CU quota in ``ROC_GLOBAL_CU_MASK``,
because that is the only masking route verified to reach PyTorch. The cell
reports the coefficient of variation it actually achieved rather than assuming
the sample count in the plan is enough: a single-card screen on the NVIDIA
side already showed 5.5% CV at 22 samples, above the 5% Gate B threshold.

Gate B-AMD additionally requires every profile to carry the requested mask
*and* the ``hipExtStreamGetCUMask`` readback. PyTorch does not expose that
call, so it is reached through ctypes on the stream torch actually launches
on -- see :func:`read_cu_mask`. Without it a mask the runtime silently
dropped is indistinguishable from one it honoured, which is exactly how the
gfx1201 sweep caught bits 32..63 being ignored.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import statistics
import subprocess
import sys
import time

CELL_SCHEMA_VERSION = "burstserve.amd-profile-cell/v1"

MODEL_REPOS = {
    "sdxl": "stabilityai/stable-diffusion-xl-base-1.0",
    "cogvideox-2b": "THUDM/CogVideoX-2b",
    "cogvideox-5b": "THUDM/CogVideoX-5b",
    "flux-dev": "black-forest-labs/FLUX.1-dev",
}

# SDXL publishes fp32 and fp16 weights side by side; without the variant the
# pipeline loads the fp32 shards and then casts, which doubles load time and
# host memory for no change in what is measured.
MODEL_VARIANT = {"sdxl": "fp16"}


def read_cu_mask(stream_ptr: int, words: int = 4) -> dict:
    """Read a stream's CU mask back out of the HIP runtime.

    Returns the mask the runtime reports, not the one that was asked for.
    A disagreement between the two is the finding, so it is recorded rather
    than raised here.
    """
    try:
        hip = ctypes.CDLL("libamdhip64.so")
    except OSError as exc:  # pragma: no cover - depends on the host runtime
        return {"available": False, "error": str(exc)}
    fn = hip.hipExtStreamGetCUMask
    fn.restype = ctypes.c_int
    fn.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
    buffer = (ctypes.c_uint32 * words)()
    rc = fn(ctypes.c_void_p(stream_ptr), words, buffer)
    if rc != 0:
        return {"available": True, "rc": rc, "mask": None}
    value = 0
    for index, word in enumerate(buffer):
        value |= word << (32 * index)
    return {
        "available": True,
        "rc": 0,
        "mask": hex(value),
        "popcount": bin(value).count("1"),
    }


def mask_attestation(torch) -> dict:
    """Bind the requested quota to what the runtime actually installed."""
    requested = os.environ.get("ROC_GLOBAL_CU_MASK")
    requested_value = int(requested, 0) if requested else None
    stream = torch.cuda.current_stream()
    readback = read_cu_mask(stream.cuda_stream)
    record = {
        "requested_cu_mask": requested,
        "requested_units": (
            bin(requested_value).count("1") if requested_value is not None else None
        ),
        "readback": readback,
        "stream_ptr": stream.cuda_stream,
        # A second, independent in-process signal: a masked device reports
        # popcount/2 here, so it corroborates the readback without sharing
        # its code path.
        "multi_processor_count": torch.cuda.get_device_properties(
            0
        ).multi_processor_count,
    }
    if requested_value is None:
        record["readback_matches_request"] = None
    else:
        record["readback_matches_request"] = (
            readback.get("mask") is not None
            and int(readback["mask"], 16) == requested_value
        )
    record["units"] = readback.get("popcount") or record["requested_units"]
    return record


def rocm_version() -> str | None:
    try:
        out = subprocess.run(
            ["hipconfig", "--version"], capture_output=True, text=True, timeout=30
        )
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):  # pragma: no cover
        return None


def measure(fn, *, warmup: int, samples: int, sync, deadline: float | None = None):
    """Time ``fn``, recording when each sample ran and not only how long.

    Wall-clock start and end are kept because a co-run cell has to prove the
    two processes were actually resident at the same time. A pair of
    durations alone cannot distinguish genuine contention from two runs that
    politely took turns.
    """
    for _ in range(warmup):
        fn()
    sync()
    out: list[dict] = []
    while True:
        if deadline is not None:
            if time.time() >= deadline:
                break
        elif len(out) >= samples:
            break
        sync()
        start_wall = time.time()
        started = time.perf_counter()
        fn()
        sync()
        elapsed = time.perf_counter() - started
        out.append({"s": elapsed, "start_wall": start_wall,
                    "end_wall": start_wall + elapsed})
    return out


def wait_at_barrier(directory: str, name: str, peer: str, timeout: float) -> dict:
    """Hold until the peer process is also warmed up and ready to be measured.

    Without this the faster process finishes its warmup, runs its whole
    sample set, and exits before the slower one starts -- which would be
    reported as a co-run while measuring nothing of the kind.
    """
    from pathlib import Path

    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    mine = root / f"{name}.ready"
    mine.write_text(repr(time.time()), encoding="utf-8")
    theirs = root / f"{peer}.ready"
    deadline = time.time() + timeout
    while not theirs.exists():
        if time.time() > deadline:
            raise TimeoutError(f"peer {peer!r} never reached the barrier")
        time.sleep(0.005)
    return {
        "released_at": time.time(),
        "self_ready_at": float(mine.read_text(encoding="utf-8")),
        "peer_ready_at": float(theirs.read_text(encoding="utf-8")),
    }


def synthetic(torch, device, batch: int):
    """A dense fp16 matmul chain: deterministic, and it saturates the die."""
    torch.manual_seed(0)
    a = torch.randn(batch * 1024, 4096, device=device, dtype=torch.float16)
    b = torch.randn(4096, 4096, device=device, dtype=torch.float16)

    def step():
        x = a
        for _ in range(4):
            x = torch.mm(x, b)
        return x

    return step, {"flops_per_step": 2 * (batch * 1024) * 4096 * 4096 * 4}


def diffusion(torch, device, args):
    """Load a real pipeline and time a fixed number of denoising steps.

    Timing is per-step rather than end-to-end: the plan profiles early,
    middle and late step phases separately, and an end-to-end number cannot
    be decomposed into them afterwards.
    """

    from diffusers import DiffusionPipeline
    from huggingface_hub import model_info

    repo = MODEL_REPOS[args.model]
    try:
        revision = model_info(repo).sha
    except Exception:  # offline or rate-limited; the profile still records it
        revision = None

    load_started = time.perf_counter()
    kwargs = {"torch_dtype": torch.float16, "use_safetensors": True}
    if args.model in MODEL_VARIANT:
        kwargs["variant"] = MODEL_VARIANT[args.model]
    pipeline = DiffusionPipeline.from_pretrained(repo, **kwargs).to(device)
    pipeline.set_progress_bar_config(disable=True)
    load_seconds = time.perf_counter() - load_started

    # Per-step wall time, captured from inside the denoising loop so that
    # scheduler and VAE work are not folded into the step figure.
    step_times: list[float] = []
    last = [0.0]

    def on_step(pipe, index, timestep, kwargs):
        now = time.perf_counter()
        if last[0]:
            step_times.append(now - last[0])
        last[0] = now
        return kwargs

    generator = torch.Generator(device=device).manual_seed(args.seed)
    call = {
        "prompt": [args.prompt] * args.batch,
        "num_inference_steps": args.steps,
        "generator": generator,
        "callback_on_step_end": on_step,
    }
    if args.model.startswith("cogvideox"):
        call["num_frames"] = args.frames
    else:
        call["height"] = args.height
        call["width"] = args.width

    def run():
        step_times.clear()
        last[0] = 0.0
        with torch.inference_mode():
            pipeline(**call)
        return list(step_times)

    return run, {
        "repo": repo,
        "model_revision": revision,
        "load_seconds": load_seconds,
        "steps": args.steps,
        "height": args.height,
        "width": args.width,
    }


def phase_summary(step_times: list[float]) -> dict:
    """Split a denoising trajectory into early/middle/late thirds."""
    if not step_times:
        return {}
    third = max(1, len(step_times) // 3)
    parts = {
        "early": step_times[:third],
        "middle": step_times[third : 2 * third],
        "late": step_times[2 * third :],
    }
    return {
        f"{name}_step_mean_s": statistics.mean(values)
        for name, values in parts.items()
        if values
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="synthetic")
    parser.add_argument("--batch", type=int, default=1)
    parser.add_argument("--steps", type=int, default=1)
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--target-cv", type=float, default=0.05)
    parser.add_argument("--max-samples", type=int, default=300)
    parser.add_argument("--frames", type=int, default=49)
    parser.add_argument("--height", type=int, default=1024)
    parser.add_argument("--width", type=int, default=1024)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--prompt", default="a quiet street at dusk")
    # Co-run support. Gate B-AMD requires two processes with disjoint masks,
    # not one process with two streams, so the synchronisation has to be
    # out-of-process too.
    parser.add_argument("--barrier-dir")
    parser.add_argument("--barrier-name")
    parser.add_argument("--barrier-peer")
    parser.add_argument("--barrier-timeout", type=float, default=1800.0)
    parser.add_argument(
        "--co-run-seconds", type=float,
        help="sample for this long after the barrier instead of for a fixed "
             "sample count, so both processes stay resident for the whole "
             "measurement window",
    )
    args = parser.parse_args()

    import torch

    device = torch.device("cuda")
    torch.zeros(1, device=device)  # force context creation before the readback
    attestation = mask_attestation(torch)

    # A requested mask the runtime did not install makes every number below
    # unattributable, so refuse rather than emit a mislabelled cell.
    if attestation["readback_matches_request"] is False:
        print(
            json.dumps(
                {
                    "status": "mask_not_honoured",
                    "cu_mask": attestation,
                    "schema_version": CELL_SCHEMA_VERSION,
                }
            )
        )
        return 3

    if args.model == "synthetic":
        step, extra = synthetic(torch, device, args.batch)
        collect_steps = False
    elif args.model in MODEL_REPOS:
        step, extra = diffusion(torch, device, args)
        collect_steps = True
    else:
        print(json.dumps({"status": "unsupported_model", "model": args.model}))
        return 2

    last_steps: list[float] = []
    if collect_steps:
        inner = step

        def step():  # noqa: F811 - deliberate wrapper to capture step times
            last_steps.clear()
            last_steps.extend(inner())

    sync = torch.cuda.synchronize

    barrier = None
    if args.barrier_dir:
        # Warm up before the barrier: the first iterations pay for autotuning
        # and allocator growth, and paying for them inside the shared window
        # would be charged to the peer as contention.
        for _ in range(args.warmup):
            step()
        sync()
        barrier = wait_at_barrier(
            args.barrier_dir, args.barrier_name, args.barrier_peer,
            args.barrier_timeout,
        )

    if args.co_run_seconds:
        timed = measure(step, warmup=0, samples=0, sync=sync,
                        deadline=time.time() + args.co_run_seconds)
        escalations = 0
    else:
        timed = measure(step, warmup=0 if barrier else args.warmup,
                        samples=args.samples, sync=sync)
        # Escalate rather than report a CV that misses the gate: the plan
        # requires each cell to record the CV it reached, and to raise the
        # sample count when it does not meet the threshold.
        escalations = 0
        while True:
            values = [entry["s"] for entry in timed]
            mean = statistics.mean(values)
            cv = (
                statistics.stdev(values) / mean
                if len(values) > 1 and mean
                else float("inf")
            )
            if cv <= args.target_cv or len(values) >= args.max_samples:
                break
            timed += measure(step, warmup=0, samples=len(values), sync=sync)
            escalations += 1

    if not timed:
        print(json.dumps({"status": "no_samples",
                          "schema_version": CELL_SCHEMA_VERSION}))
        return 4
    samples = [entry["s"] for entry in timed]

    # The mask is read again after the workload: a quota that changed mid-cell
    # would otherwise be attributed to the quota the cell was labelled with.
    attestation_after = mask_attestation(torch)

    ordered = sorted(samples)
    pick = lambda q: ordered[min(len(ordered) - 1, int(q * len(ordered)))]
    record = {
        "schema_version": CELL_SCHEMA_VERSION,
        "status": "ok",
        "model": args.model,
        "batch": args.batch,
        "cu_mask": attestation["requested_cu_mask"],
        "units": attestation["units"],
        "cu_mask_attestation": attestation,
        "cu_mask_stable": attestation_after["readback"].get("mask")
        == attestation["readback"].get("mask"),
        "samples": len(samples),
        "escalations": escalations,
        "p50_s": pick(0.50),
        "p95_s": pick(0.95),
        "p99_s": pick(0.99),
        "mean_s": statistics.mean(samples),
        "cv": statistics.stdev(samples) / statistics.mean(samples),
        "meets_cv_threshold": (
            statistics.stdev(samples) / statistics.mean(samples)
        ) <= args.target_cv,
        "peak_memory_bytes": torch.cuda.max_memory_allocated(),
        "peak_memory_reserved_bytes": torch.cuda.max_memory_reserved(),
        # Recorded so a co-run can tell in advance whether two of these fit
        # on the card, rather than discovering it as an OOM mid-measurement.
        "total_memory_bytes": torch.cuda.get_device_properties(0).total_memory,
        **({"per_step_count": len(last_steps),
            "per_step_mean_s": statistics.mean(last_steps),
            **phase_summary(last_steps)} if last_steps else {}),
        "torch": torch.__version__,
        "rocm": rocm_version(),
        "device_name": torch.cuda.get_device_name(0),
        "gcn_arch": torch.cuda.get_device_properties(0).gcnArchName,
        **extra,
    }
    if barrier is not None:
        record["barrier"] = barrier
        # Every sample's window, so the driver can keep only the ones that
        # ran while the peer was demonstrably also running.
        record["sample_windows"] = [
            {"start_wall": e["start_wall"], "end_wall": e["end_wall"],
             "s": e["s"]}
            for e in timed
        ]
        record["window_start_wall"] = timed[0]["start_wall"]
        record["window_end_wall"] = timed[-1]["end_wall"]
    if "flops_per_step" in record:
        record["tflops"] = record["flops_per_step"] / record["p50_s"] / 1e12
    # Throughput in items per second is what the saturation test compares
    # across problem sizes; latency alone cannot answer it.
    record["items_per_s"] = args.batch / record["p50_s"]
    print(json.dumps(record))
    return 0


if __name__ == "__main__":
    sys.exit(main())
