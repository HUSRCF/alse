"""Predict a cold model's residency cost from missing bytes and bandwidth.

Gate B requires the cold-model prediction to use the missing byte count and
a measured bandwidth, and nothing else. That wording rules out the shortcut
that would otherwise pass: fitting a constant to the observed load times
and calling it a prediction. So the predictor here has no free parameters
at all --

    predicted_seconds = weight_bytes / measured_host_to_device_bandwidth

-- and the bandwidth comes from a separate transfer benchmark that never
sees a model. If that under-predicts, the honest conclusion is that a
single aggregate bandwidth is the wrong term, not that the predictor needs
a fudge factor.

Both pinned and pageable bandwidths are measured because a pipeline moved
with .to(device) uses pageable staging unless the caller arranged
otherwise, and quoting the pinned figure for a pageable transfer would
overstate what the scheduler can rely on.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from burstserve.provenance import canonical_json  # noqa: E402

SCHEMA_VERSION = "burstserve.amd-cold-model/v1"

MODEL_REPOS = {
    "sdxl": "stabilityai/stable-diffusion-xl-base-1.0",
    "cogvideox-2b": "THUDM/CogVideoX-2b",
    "cogvideox-5b": "THUDM/CogVideoX-5b",
    "flux-dev": "black-forest-labs/FLUX.1-dev",
}
MODEL_VARIANT = {"sdxl": "fp16"}


def measure_bandwidth(torch, *, megabytes: float, pinned: bool, repeats: int):
    count = int(megabytes * 1024 * 1024)
    host = torch.empty(count, dtype=torch.uint8, pin_memory=pinned)
    device = torch.empty(count, dtype=torch.uint8, device="cuda")
    torch.cuda.synchronize()
    for _ in range(2):
        device.copy_(host)
    torch.cuda.synchronize()
    samples = []
    for _ in range(repeats):
        started = time.perf_counter()
        device.copy_(host)
        torch.cuda.synchronize()
        samples.append(time.perf_counter() - started)
    del host, device
    torch.cuda.empty_cache()
    median = statistics.median(samples)
    return {
        "megabytes": megabytes,
        "bytes": count,
        "pinned": pinned,
        "repeats": repeats,
        "median_seconds": median,
        "bytes_per_second": count / median,
        "samples_seconds": samples,
    }


def predict_cold_seconds(weight_bytes: int, bandwidth_bps: float) -> float:
    """The whole predictor. Two inputs, no fitted constants.

    Kept as its own function so that "no free parameters" is a property a
    test can check rather than a claim in a docstring.
    """
    if weight_bytes < 0:
        raise ValueError("weight bytes cannot be negative")
    if bandwidth_bps <= 0:
        raise ValueError("bandwidth must be positive")
    return weight_bytes / bandwidth_bps


def bandwidth_for_size(curve: list[tuple[int, float]], size_bytes: int) -> float:
    """The measured bandwidth at the transfer size closest from below.

    A single aggregate bandwidth is the wrong term for a pipeline move.
    .to(device) copies each parameter separately, and a model's tensors are
    mostly far smaller than the multi-hundred-megabyte block a bandwidth
    benchmark uses, so one big-block figure over-predicts badly -- measured
    at 72.5% error on SDXL, against a 10% gate.

    Using bandwidth as a function of size keeps the predictor free of fitted
    constants: every value here was measured, just at more than one size.
    """
    if not curve:
        raise ValueError("no bandwidth curve was measured")
    ordered = sorted(curve)
    chosen = ordered[0][1]
    for measured_size, bps in ordered:
        if measured_size <= size_bytes:
            chosen = bps
        else:
            break
    return chosen


def predict_cold_seconds_by_tensor(
    tensor_bytes: list[int], curve: list[tuple[int, float]]
) -> float:
    """Sum each tensor's own transfer time at its own measured bandwidth."""
    return sum(
        predict_cold_seconds(size, bandwidth_for_size(curve, size))
        for size in tensor_bytes
        if size > 0
    )


def widest_transfer(bandwidth: list[dict], *, pinned: bool) -> dict:
    """The largest measured transfer of the requested kind.

    A whole model most resembles the largest transfer, so that is the
    figure the predictor uses; the smaller ones stay in the record to show
    where per-transfer overhead stops mattering.
    """
    candidates = [e for e in bandwidth if e["pinned"] is pinned]
    if not candidates:
        raise ValueError(f"no {'pinned' if pinned else 'pageable'} measurements")
    return max(candidates, key=lambda e: e["megabytes"])


def weight_bytes_of(torch, pipeline) -> int:
    return sum(tensor_sizes_of(torch, pipeline))


def tensor_sizes_of(torch, pipeline) -> list[int]:
    """Every parameter and buffer's size, separately.

    The distribution matters, not just the total: .to(device) issues one
    copy per tensor, so a model made of many small tensors moves far slower
    than its byte count over a large-block bandwidth would suggest.
    """
    sizes = []
    for component in vars(pipeline).values():
        if isinstance(component, torch.nn.Module):
            for tensor in list(component.parameters()) + list(component.buffers()):
                sizes.append(tensor.numel() * tensor.element_size())
    return sizes


def replay_transfers(torch, tensor_sizes: list[int]) -> float:
    """Move the same sizes as raw copies, without the framework in between.

    .to(device) does more than copy: it walks the module tree in Python,
    allocates a destination per tensor, and rebinds each parameter. This
    replays only the transfers, so the difference between the two isolates
    what is not transfer -- which decides whether a bytes-and-bandwidth
    predictor can reach the gate at all, or whether the residual is
    framework cost that no bandwidth model can express.
    """
    if not tensor_sizes:
        return 0.0
    largest = max(tensor_sizes)
    host = torch.empty(largest, dtype=torch.uint8)
    device = torch.empty(largest, dtype=torch.uint8, device="cuda")
    torch.cuda.synchronize()
    started = time.perf_counter()
    for size in tensor_sizes:
        if size:
            device[:size].copy_(host[:size])
    torch.cuda.synchronize()
    elapsed = time.perf_counter() - started
    del host, device
    torch.cuda.empty_cache()
    return elapsed


def fault_in_host_pages(torch, pipeline) -> float:
    """Touch every weight so the timed transfer is not also a disk read.

    safetensors maps the checkpoint rather than copying it, so a freshly
    loaded pipeline's pages are still on disk and the first .to(device)
    faults them in on the way past. Timing that and dividing by a
    host-to-device bandwidth would attribute storage latency to PCIe, and
    the resulting error would look like a bad bandwidth measurement rather
    than a mismeasured quantity.
    """
    started = time.perf_counter()
    touched = 0
    for component in vars(pipeline).values():
        if isinstance(component, torch.nn.Module):
            for tensor in list(component.parameters()) + list(component.buffers()):
                # A read of one element per page would do; summing is
                # simpler and still cheap next to the transfer being timed.
                touched += int(tensor.reshape(-1)[:1].abs().sum() >= 0)
                tensor.data = tensor.data.clone()
    return time.perf_counter() - started


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", default="sdxl")
    # Down to a quarter megabyte: a diffusion pipeline's tensors are
    # mostly far smaller than a bandwidth benchmark's block, and the
    # curve has to cover the sizes actually transferred.
    parser.add_argument("--sizes-mb",
                        default="0.0625,0.25,1,4,16,64,256,1024")
    parser.add_argument("--bandwidth-repeats", type=int, default=15)
    parser.add_argument("--mape-threshold", type=float, default=0.10)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    import torch
    from diffusers import DiffusionPipeline

    torch.zeros(1, device="cuda")
    sizes = [float(s) for s in args.sizes_mb.split(",") if s.strip()]

    bandwidth = [
        measure_bandwidth(torch, megabytes=size, pinned=pinned,
                          repeats=args.bandwidth_repeats)
        for pinned in (True, False)
        for size in sizes
    ]
    for entry in bandwidth:
        print(f"  bandwidth {'pinned  ' if entry['pinned'] else 'pageable'} "
              f"{entry['megabytes']:8.3f} MB -> "
              f"{entry['bytes_per_second']/1e9:6.2f} GB/s", flush=True)

    pinned_bw = widest_transfer(bandwidth, pinned=True)["bytes_per_second"]
    pageable_bw = widest_transfer(bandwidth, pinned=False)["bytes_per_second"]
    pinned_curve = [(e["bytes"], e["bytes_per_second"]) for e in bandwidth
                    if e["pinned"]]
    pageable_curve = [(e["bytes"], e["bytes_per_second"]) for e in bandwidth
                      if not e["pinned"]]

    results = []
    for name in [m.strip() for m in args.models.split(",") if m.strip()]:
        repo = MODEL_REPOS[name]
        kwargs = {"torch_dtype": torch.float16, "use_safetensors": True}
        if name in MODEL_VARIANT:
            kwargs["variant"] = MODEL_VARIANT[name]
        # Load to host first: the cold cost the scheduler models is the
        # host->device move, not the disk read and the safetensors parse.
        pipeline = DiffusionPipeline.from_pretrained(repo, **kwargs)
        tensor_sizes = tensor_sizes_of(torch, pipeline)
        weights = sum(tensor_sizes)
        fault_seconds = fault_in_host_pages(torch, pipeline)

        replay_seconds = replay_transfers(torch, tensor_sizes)

        torch.cuda.synchronize()
        started = time.perf_counter()
        pipeline = pipeline.to("cuda")
        torch.cuda.synchronize()
        observed = time.perf_counter() - started

        entry = {
            "model": name,
            "repo": repo,
            "weight_bytes": weights,
            "tensor_count": len(tensor_sizes),
            "tensor_median_bytes": statistics.median(tensor_sizes),
            "tensor_p90_bytes": sorted(tensor_sizes)[int(0.9 * len(tensor_sizes))],
            "observed_seconds": observed,
            # Reported so the transfer figure can be checked against the
            # cost of getting the pages into host memory in the first place.
            "host_page_fault_seconds": fault_seconds,
            # Same sizes, raw copies only. observed minus this is the cost
            # that is not transfer.
            "replay_transfer_seconds": replay_seconds,
            "framework_overhead_seconds": observed - replay_seconds,
            "framework_overhead_per_tensor_s": (
                (observed - replay_seconds) / len(tensor_sizes)
                if tensor_sizes else None
            ),
            # Aggregate: one bandwidth for the whole byte count. Kept
            # because it is the form the plan states, and because its error
            # is the evidence that the form is wrong.
            "predicted_seconds_pinned": predict_cold_seconds(weights, pinned_bw),
            "predicted_seconds_pageable": predict_cold_seconds(weights,
                                                               pageable_bw),
            # Per-tensor: each tensor at the bandwidth measured for its own
            # size. Still no fitted constant -- the bandwidth is just a
            # function of size rather than a single number.
            "predicted_seconds_per_tensor_pinned": predict_cold_seconds_by_tensor(
                tensor_sizes, pinned_curve),
            "predicted_seconds_per_tensor_pageable": predict_cold_seconds_by_tensor(
                tensor_sizes, pageable_curve),
        }
        for label in ("pinned", "pageable", "per_tensor_pinned",
                      "per_tensor_pageable"):
            predicted = entry[f"predicted_seconds_{label}"]
            entry[f"absolute_percentage_error_{label}"] = (
                abs(predicted - observed) / observed
            )
        results.append(entry)
        print(f"  {name}: weights {weights/1e9:.2f} GB in "
              f"{len(tensor_sizes)} tensors (median "
              f"{statistics.median(tensor_sizes)/1e3:.1f} KB)  observed "
              f"{observed:.3f}s\n"
              f"      aggregate  {entry['predicted_seconds_pageable']:.3f}s "
              f"APE {entry['absolute_percentage_error_pageable']*100:5.1f}%\n"
              f"      per-tensor {entry['predicted_seconds_per_tensor_pageable']:.3f}s "
              f"APE {entry['absolute_percentage_error_per_tensor_pageable']*100:5.1f}%\n"
              f"      replay(raw copies) {replay_seconds:.3f}s   "
              f"framework {observed - replay_seconds:.3f}s "
              f"({(observed-replay_seconds)/len(tensor_sizes)*1e6:.1f} us/tensor)",
              flush=True)
        del pipeline
        torch.cuda.empty_cache()

    report = {
        "schema_version": SCHEMA_VERSION,
        "device_name": torch.cuda.get_device_name(0),
        "torch": torch.__version__,
        "bandwidth": bandwidth,
        "bandwidth_used_pinned_bps": pinned_bw,
        "bandwidth_used_pageable_bps": pageable_bw,
        "predictor": "weight_bytes / measured_host_to_device_bandwidth",
        "predictor_free_parameters": 0,
        "results": results,
        # This host is a single-socket desktop, so the plan's local/remote
        # NUMA axis has no remote node to measure. Recorded rather than
        # quietly skipped.
        "numa_note": "single-socket host: no remote NUMA node exists to measure",
    }
    if results:
        for label in ("pinned", "pageable", "per_tensor_pinned",
                      "per_tensor_pageable"):
            report[f"mape_{label}"] = statistics.mean(
                r[f"absolute_percentage_error_{label}"] for r in results
            )
        # Pageable is the honest default: .to(device) stages through
        # pageable memory unless the caller pinned it. Per-tensor is the
        # headline because .to(device) issues one copy per tensor, which is
        # the transfer the scheduler actually pays for.
        report["mape"] = report["mape_per_tensor_pageable"]
        report["predictor_form"] = "sum_i bytes_i / measured_bandwidth(bytes_i)"
        report["meets_threshold"] = report["mape"] <= args.mape_threshold
        print(f"\nMAPE  aggregate pageable {report['mape_pageable']*100:5.1f}%   "
              f"per-tensor pageable {report['mape_per_tensor_pageable']*100:5.1f}%   "
              f"threshold {args.mape_threshold*100:.0f}%")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(canonical_json(report) + "\n")
    print(f"report: {out}")
    return 0 if report.get("meets_threshold") else 1


if __name__ == "__main__":
    raise SystemExit(main())
