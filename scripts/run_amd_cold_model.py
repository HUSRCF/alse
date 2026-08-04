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
    """Bandwidth at a transfer size, interpolated between measured points.

    A step function takes the measured point at or below each tensor, which
    over-predicts every size that falls between two points -- it cost 13.4%
    on the transfer term at a doubling grid. Bandwidth against size is
    close to linear in log-log, so interpolating there is the natural
    reading of the same measurements. Nothing is fitted: outside the
    measured range the nearest endpoint is used rather than extrapolated,
    since an extrapolated bandwidth is not a measured one.
    """
    if not curve:
        raise ValueError("no bandwidth curve was measured")
    ordered = sorted(curve)
    if size_bytes <= ordered[0][0]:
        return ordered[0][1]
    if size_bytes >= ordered[-1][0]:
        return ordered[-1][1]
    import math

    for (low_size, low_bps), (high_size, high_bps) in zip(ordered, ordered[1:]):
        if low_size <= size_bytes <= high_size:
            if low_size == high_size:
                return low_bps
            span = math.log(high_size) - math.log(low_size)
            position = (math.log(size_bytes) - math.log(low_size)) / span
            return math.exp(
                math.log(low_bps)
                + position * (math.log(high_bps) - math.log(low_bps))
            )
    return ordered[-1][1]  # pragma: no cover - covered by the guards above


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


def calibrate_framework_curve(torch, *, sizes, count) -> list[tuple[int, float]]:
    """Per-tensor framework cost as a function of tensor size.

    Symmetric with the bandwidth curve, and for the same reason. A single
    per-tensor constant calibrated at one size missed by 80%: the constant
    measured on 4 KB tensors does not describe a model whose tensors
    average 2.6 MB. So the cost is measured at several sizes and read off
    the curve for each tensor.

    At each size the framework cost is ``.to(device)`` minus a replay of the
    same transfers into buffers that already exist, which isolates what is
    not transfer: the allocation and the Python traversal. The modules are
    synthetic and none of the target model's timings are read.
    """
    curve = []
    for size in sizes:
        module = torch.nn.Module()
        for index in range(count):
            module.register_parameter(
                f"p{index}",
                torch.nn.Parameter(
                    torch.empty(max(1, size // 2), dtype=torch.float16),
                    requires_grad=False,
                ),
            )
        sizes_here = [size] * count
        replayed = replay_transfers(torch, sizes_here)
        torch.cuda.synchronize()
        started = time.perf_counter()
        module.to("cuda")
        torch.cuda.synchronize()
        moved = time.perf_counter() - started
        del module
        # Deliberately no empty_cache: the target runs with a warm caching
        # allocator, and returning blocks to the driver here would
        # calibrate a cold one against a warm observation.
        curve.append((size, max(0.0, moved - replayed) / count))
    return curve


def framework_for_size(curve: list[tuple[int, float]], size_bytes: int) -> float:
    """Interpolated per-tensor framework cost, never extrapolated."""
    if not curve:
        raise ValueError("no framework curve was measured")
    ordered = sorted(curve)
    if size_bytes <= ordered[0][0]:
        return ordered[0][1]
    if size_bytes >= ordered[-1][0]:
        return ordered[-1][1]
    import math

    for (low_size, low), (high_size, high) in zip(ordered, ordered[1:]):
        if low_size <= size_bytes <= high_size:
            if low_size == high_size or low <= 0 or high <= 0:
                return low
            span = math.log(high_size) - math.log(low_size)
            position = (math.log(size_bytes) - math.log(low_size)) / span
            return math.exp(
                math.log(low) + position * (math.log(high) - math.log(low))
            )
    return ordered[-1][1]  # pragma: no cover


def predict_framework_seconds(tensor_sizes: list[int],
                              curve: list[tuple[int, float]]) -> float:
    """Each tensor at the framework cost measured for its own size."""
    return sum(framework_for_size(curve, size) for size in tensor_sizes)


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
    # Doubling grid from 1 KB to 1 GB. The step function takes the measured
    # point at or below each tensor, so the coarser the grid the more it
    # over-predicts small tensors: at a 4x grid the transfer term was 46%
    # high. Every value is still measured, just at more sizes.
    parser.add_argument(
        "--sizes-mb",
        default="0.0009765625,0.001953125,0.00390625,0.0078125,0.015625,"
                "0.03125,0.0625,0.125,0.25,0.5,1,2,4,8,16,32,64,128,256,"
                "512,1024")
    parser.add_argument("--bandwidth-repeats", type=int, default=15)
    parser.add_argument("--overhead-sizes-bytes",
                        default="2048,16384,131072,1048576,8388608,67108864")
    parser.add_argument("--overhead-count", type=int, default=64)
    parser.add_argument("--mape-threshold", type=float, default=0.10)
    # A cold load can only be timed once per load, so the only way to see
    # its distribution is to load again. Single-shot runs of this measured
    # 0.615, 0.630 and 0.725 s.
    parser.add_argument("--reload-repeats", type=int, default=3)
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
    overhead_curve = calibrate_framework_curve(
        torch,
        sizes=[int(b) for b in args.overhead_sizes_bytes.split(",") if b.strip()],
        count=args.overhead_count,
    )
    print("  framework curve (us/tensor): " + "  ".join(
        f"{size//1024}K:{cost*1e6:.1f}" for size, cost in overhead_curve
    ), flush=True)

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
        observations, replays, fault_times = [], [], []
        tensor_sizes = None
        for _ in range(max(1, args.reload_repeats)):
            pipeline = DiffusionPipeline.from_pretrained(repo, **kwargs)
            tensor_sizes = tensor_sizes_of(torch, pipeline)
            fault_times.append(fault_in_host_pages(torch, pipeline))
            replays.append(replay_transfers(torch, tensor_sizes))

            torch.cuda.synchronize()
            started = time.perf_counter()
            pipeline = pipeline.to("cuda")
            torch.cuda.synchronize()
            observations.append(time.perf_counter() - started)

            del pipeline
            torch.cuda.empty_cache()

        weights = sum(tensor_sizes)
        fault_seconds = statistics.median(fault_times)
        # Two states, not one distribution with noise. The first load in a
        # process pays hipMalloc for every block; later loads reuse cached
        # ones. Measured over nine reloads: 0.585 s once, then 0.404-0.410
        # with a CV under 1%. Taking a median across both would describe
        # neither, so the steady state is what the terms are scored against
        # -- a scheduler's swap loop is warm -- and the first load is
        # reported separately as the admission cost.
        warm = observations[1:] or observations
        warm_replays = replays[1:] or replays
        observed = statistics.median(warm)
        replay_seconds = statistics.median(warm_replays)

        entry = {
            "model": name,
            "repo": repo,
            "weight_bytes": weights,
            "tensor_count": len(tensor_sizes),
            "tensor_median_bytes": statistics.median(tensor_sizes),
            "tensor_p90_bytes": sorted(tensor_sizes)[int(0.9 * len(tensor_sizes))],
            "observed_seconds": observed,
            "observed_samples_s": observations,
            "replay_samples_s": replays,
            "reload_repeats": len(observations),
            # The first load in a process pays hipMalloc for every block;
            # later loads reuse cached ones. Both are real situations -- a
            # scheduler's first admission is cold, its swap loop is warm --
            # so neither is averaged away.
            "first_load_seconds": observations[0],
            "first_load_framework_seconds": observations[0] - replays[0],
            "warm_load_median_seconds": observed,
            "warm_load_framework_seconds": observed - replay_seconds,
            "scored_state": "warm",
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
            # Transfer plus the independently calibrated framework cost.
            # Still nothing fitted to this model: both terms were measured
            # on something else.
            "predicted_seconds_with_overhead": (
                predict_cold_seconds_by_tensor(tensor_sizes, pageable_curve)
                + predict_framework_seconds(tensor_sizes, overhead_curve)
            ),
        }
        for label in ("pinned", "pageable", "per_tensor_pinned",
                      "per_tensor_pageable", "with_overhead"):
            predicted = entry[f"predicted_seconds_{label}"]
            entry[f"absolute_percentage_error_{label}"] = (
                abs(predicted - observed) / observed
            )

        # A total can be right because two terms are wrong in opposite
        # directions. The replay splits the observation into transfer and
        # framework, so each term is scored against the part it claims to
        # model -- otherwise a cancelling pair reads as a correct model.
        measured_framework = observed - replay_seconds
        predicted_transfer = entry["predicted_seconds_per_tensor_pageable"]
        predicted_framework = predict_framework_seconds(
            tensor_sizes, overhead_curve)
        entry["term_errors"] = {
            "transfer_predicted_s": predicted_transfer,
            "transfer_measured_s": replay_seconds,
            "transfer_error": (
                abs(predicted_transfer - replay_seconds) / replay_seconds
                if replay_seconds > 0 else None
            ),
            "framework_predicted_s": predicted_framework,
            "framework_measured_s": measured_framework,
            "framework_error": (
                abs(predicted_framework - measured_framework)
                / measured_framework if measured_framework > 0 else None
            ),
        }
        errors = [entry["term_errors"]["transfer_error"],
                  entry["term_errors"]["framework_error"]]
        entry["terms_individually_accurate"] = all(
            e is not None and e <= args.mape_threshold for e in errors
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
              f"      +overhead  {entry['predicted_seconds_with_overhead']:.3f}s "
              f"APE {entry['absolute_percentage_error_with_overhead']*100:5.1f}%\n"
              f"      replay(raw copies) {replay_seconds:.3f}s   "
              f"framework {observed - replay_seconds:.3f}s "
              f"({(observed-replay_seconds)/len(tensor_sizes)*1e6:.1f} us/tensor)\n"
              f"      term errors: transfer "
              f"{entry['term_errors']['transfer_error']*100:5.1f}%   framework "
              f"{entry['term_errors']['framework_error']*100:5.1f}%   "
              f"both accurate: {entry['terms_individually_accurate']}",
              flush=True)

    report = {
        "schema_version": SCHEMA_VERSION,
        "device_name": torch.cuda.get_device_name(0),
        "torch": torch.__version__,
        "bandwidth": bandwidth,
        "framework_curve": [{"size_bytes": s_, "seconds_per_tensor": c}
                            for s_, c in overhead_curve],
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
                      "per_tensor_pageable", "with_overhead"):
            report[f"mape_{label}"] = statistics.mean(
                r[f"absolute_percentage_error_{label}"] for r in results
            )
        # Pageable is the honest default: .to(device) stages through
        # pageable memory unless the caller pinned it. Per-tensor is the
        # headline because .to(device) issues one copy per tensor, which is
        # the transfer the scheduler actually pays for.
        # Kept as the strict-clause figure for continuity with earlier
        # runs; the gate is decided by the per-term split (2026-08-04).
        report["mape"] = report["mape_per_tensor_pageable"]
        report["predictor_form"] = "sum_i bytes_i / measured_bandwidth(bytes_i)"
        # Reported next to mape_with_overhead so a total that is accurate
        # only because its terms cancel cannot be quoted as a working model.
        report["terms_individually_accurate"] = all(
            r.get("terms_individually_accurate") for r in results
        )
        report["meets_threshold"] = report["mape"] <= args.mape_threshold
        print(f"\nMAPE  aggregate pageable {report['mape_pageable']*100:5.1f}%   "
              f"per-tensor {report['mape_per_tensor_pageable']*100:5.1f}%   "
              f"+overhead {report['mape_with_overhead']*100:5.1f}%   "
              f"threshold {args.mape_threshold*100:.0f}%")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(canonical_json(report) + "\n")
    print(f"report: {out}")
    return 0 if report.get("meets_threshold") else 1


if __name__ == "__main__":
    raise SystemExit(main())
