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


def measure_bandwidth(torch, *, megabytes: int, pinned: bool, repeats: int):
    count = megabytes * 1024 * 1024
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
    total = 0
    for component in vars(pipeline).values():
        if isinstance(component, torch.nn.Module):
            total += sum(p.numel() * p.element_size()
                         for p in component.parameters())
            total += sum(b.numel() * b.element_size()
                         for b in component.buffers())
    return total


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", default="sdxl")
    parser.add_argument("--sizes-mb", default="64,256,1024")
    parser.add_argument("--bandwidth-repeats", type=int, default=15)
    parser.add_argument("--mape-threshold", type=float, default=0.10)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    import torch
    from diffusers import DiffusionPipeline

    torch.zeros(1, device="cuda")
    sizes = [int(s) for s in args.sizes_mb.split(",") if s.strip()]

    bandwidth = [
        measure_bandwidth(torch, megabytes=size, pinned=pinned,
                          repeats=args.bandwidth_repeats)
        for pinned in (True, False)
        for size in sizes
    ]
    for entry in bandwidth:
        print(f"  bandwidth {'pinned  ' if entry['pinned'] else 'pageable'} "
              f"{entry['megabytes']:5d} MB -> "
              f"{entry['bytes_per_second']/1e9:6.2f} GB/s", flush=True)

    pinned_bw = widest_transfer(bandwidth, pinned=True)["bytes_per_second"]
    pageable_bw = widest_transfer(bandwidth, pinned=False)["bytes_per_second"]

    results = []
    for name in [m.strip() for m in args.models.split(",") if m.strip()]:
        repo = MODEL_REPOS[name]
        kwargs = {"torch_dtype": torch.float16, "use_safetensors": True}
        if name in MODEL_VARIANT:
            kwargs["variant"] = MODEL_VARIANT[name]
        # Load to host first: the cold cost the scheduler models is the
        # host->device move, not the disk read and the safetensors parse.
        pipeline = DiffusionPipeline.from_pretrained(repo, **kwargs)
        weights = weight_bytes_of(torch, pipeline)

        torch.cuda.synchronize()
        started = time.perf_counter()
        pipeline = pipeline.to("cuda")
        torch.cuda.synchronize()
        observed = time.perf_counter() - started

        entry = {
            "model": name,
            "repo": repo,
            "weight_bytes": weights,
            "observed_seconds": observed,
            "predicted_seconds_pinned": predict_cold_seconds(weights, pinned_bw),
            "predicted_seconds_pageable": predict_cold_seconds(weights,
                                                               pageable_bw),
        }
        for label, predicted in (
            ("pinned", entry["predicted_seconds_pinned"]),
            ("pageable", entry["predicted_seconds_pageable"]),
        ):
            entry[f"absolute_percentage_error_{label}"] = (
                abs(predicted - observed) / observed
            )
        results.append(entry)
        print(f"  {name}: weights {weights/1e9:.2f} GB  observed "
              f"{observed:.3f}s  predicted pageable "
              f"{entry['predicted_seconds_pageable']:.3f}s  "
              f"APE {entry['absolute_percentage_error_pageable']*100:.1f}%",
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
        for label in ("pinned", "pageable"):
            report[f"mape_{label}"] = statistics.mean(
                r[f"absolute_percentage_error_{label}"] for r in results
            )
        # Pageable is the honest default: .to(device) stages through
        # pageable memory unless the caller pinned it.
        report["mape"] = report["mape_pageable"]
        report["meets_threshold"] = report["mape"] <= args.mape_threshold
        print(f"\nMAPE pageable {report['mape_pageable']*100:.1f}%  "
              f"pinned {report['mape_pinned']*100:.1f}%  "
              f"threshold {args.mape_threshold*100:.0f}%")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(canonical_json(report) + "\n")
    print(f"report: {out}")
    return 0 if report.get("meets_threshold") else 1


if __name__ == "__main__":
    raise SystemExit(main())
