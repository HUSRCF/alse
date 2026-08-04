"""Find out what .to(device) costs beyond the transfer, by measuring the
pieces directly instead of subtracting.

Five calibrations failed before this one, all of them differences: framework
was taken as ``.to(device)`` minus a replay of the same transfers. That
works only where transfer is small. Where it is not -- and for a model
averaging 2.6 MB per tensor it is not -- the difference is two large
numbers cancelling, and their noise is larger than the quantity. One
attempt reported 18215 us/tensor at 64 MB for exactly that reason.

So the two components are measured on their own:

  * traversal -- move a module that is *already* on the device. PyTorch
    still walks every parameter, but ``.to()`` on a tensor already at the
    target device and dtype returns it unchanged, so nothing is copied and
    nothing is allocated. What remains is the Python walk.

  * allocation -- time ``torch.empty(size, device=...)`` directly. No
    transfer is involved at all.

Their sum is what the difference was trying to measure, and neither
subtraction is between comparable magnitudes.

Everything here runs on synthetic modules. The target model's tensor sizes
may be supplied to predict it, because a scheduler knows a model's tensor
inventory from its metadata; none of the target's timings are read.
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

SCHEMA_VERSION = "burstserve.amd-framework-calibration/v1"


def build_module(torch, *, count: int, size_bytes: int, depth: int,
                 components: int):
    """A container of `components` modules nested `depth` deep."""
    per_component = max(1, count // max(1, components))
    holders = []
    for _ in range(max(1, components)):
        leaf = torch.nn.Module()
        for index in range(per_component):
            leaf.register_parameter(
                f"p{index}",
                torch.nn.Parameter(
                    torch.empty(max(1, size_bytes // 2), dtype=torch.float16),
                    requires_grad=False,
                ),
            )
        node = leaf
        for _ in range(max(0, depth)):
            node = torch.nn.Sequential(node)
        holders.append(node)
    return holders, max(1, components) * per_component


def time_move(torch, holders, device: str, repeats: int) -> float:
    samples = []
    for _ in range(repeats):
        torch.cuda.synchronize()
        started = time.perf_counter()
        for holder in holders:
            holder.to(device)
        torch.cuda.synchronize()
        samples.append(time.perf_counter() - started)
    return statistics.median(samples)


def measure_traversal(torch, *, counts, size_bytes, depth, components,
                      repeats) -> list[dict]:
    """Per-tensor cost of walking a module that needs no data movement."""
    rows = []
    for count in counts:
        holders, actual = build_module(
            torch, count=count, size_bytes=size_bytes, depth=depth,
            components=components,
        )
        for holder in holders:
            holder.to("cuda")          # already resident from here on
        torch.cuda.synchronize()
        time_move(torch, holders, "cuda", 2)   # warm the path
        seconds = time_move(torch, holders, "cuda", repeats)
        rows.append({"tensors": actual, "seconds": seconds,
                     "seconds_per_tensor": seconds / actual,
                     "depth": depth, "components": components})
        del holders
    return rows


def measure_allocation(torch, *, sizes, count, repeats) -> list[dict]:
    """Cost of obtaining device memory, with no transfer involved."""
    rows = []
    for size in sizes:
        elements = max(1, size // 2)
        # Warm the allocator at this size so the cached-block path is what
        # gets measured; the target observation runs warm too.
        warm = [torch.empty(elements, dtype=torch.float16, device="cuda")
                for _ in range(count)]
        del warm
        torch.cuda.synchronize()
        samples = []
        for _ in range(repeats):
            torch.cuda.synchronize()
            started = time.perf_counter()
            buffers = [
                torch.empty(elements, dtype=torch.float16, device="cuda")
                for _ in range(count)
            ]
            torch.cuda.synchronize()
            samples.append(time.perf_counter() - started)
            del buffers
        seconds = statistics.median(samples)
        rows.append({"size_bytes": size, "count": count, "seconds": seconds,
                     "seconds_per_tensor": seconds / count})
    return rows


def interpolate(curve: list[tuple[int, float]], size_bytes: int) -> float:
    """Log-log interpolation between measured points, never extrapolated."""
    if not curve:
        raise ValueError("empty curve")
    ordered = sorted(curve)
    if size_bytes <= ordered[0][0]:
        return ordered[0][1]
    if size_bytes >= ordered[-1][0]:
        return ordered[-1][1]
    import math

    for (low_x, low_y), (high_x, high_y) in zip(ordered, ordered[1:]):
        if low_x <= size_bytes <= high_x:
            if low_x == high_x or low_y <= 0 or high_y <= 0:
                return low_y
            span = math.log(high_x) - math.log(low_x)
            position = (math.log(size_bytes) - math.log(low_x)) / span
            return math.exp(
                math.log(low_y) + position * (math.log(high_y) - math.log(low_y))
            )
    return ordered[-1][1]  # pragma: no cover


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--counts", default="128,512,2048")
    parser.add_argument("--traversal-size-bytes", type=int, default=2048)
    parser.add_argument("--alloc-sizes",
                        default="2048,16384,131072,1048576,8388608,67108864")
    parser.add_argument("--alloc-count", type=int, default=64)
    parser.add_argument("--repeats", type=int, default=7)
    parser.add_argument("--depths", default="0,2,6")
    parser.add_argument("--components", default="1,8,32")
    parser.add_argument("--predict-model", default="sdxl")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    import torch

    counts = [int(c) for c in args.counts.split(",") if c.strip()]
    torch.zeros(1, device="cuda")

    print("== traversal: cost of walking a module that needs no movement ==",
          flush=True)
    traversal = measure_traversal(
        torch, counts=counts, size_bytes=args.traversal_size_bytes,
        depth=0, components=8, repeats=args.repeats,
    )
    for row in traversal:
        print(f"  {row['tensors']:5d} tensors -> "
              f"{row['seconds_per_tensor']*1e6:7.2f} us/tensor", flush=True)

    # Does the module's shape matter, or only the tensor count?
    print("== structure sensitivity (fixed tensor count) ==", flush=True)
    structure = []
    for depth in [int(d) for d in args.depths.split(",") if d.strip()]:
        for components in [int(c) for c in args.components.split(",")
                           if c.strip()]:
            rows = measure_traversal(
                torch, counts=[counts[-1]],
                size_bytes=args.traversal_size_bytes, depth=depth,
                components=components, repeats=args.repeats,
            )
            structure.extend(rows)
            print(f"  depth={depth:2d} components={components:3d} -> "
                  f"{rows[0]['seconds_per_tensor']*1e6:7.2f} us/tensor",
                  flush=True)

    print("== allocation: cost of obtaining device memory ==", flush=True)
    allocation = measure_allocation(
        torch,
        sizes=[int(s) for s in args.alloc_sizes.split(",") if s.strip()],
        count=args.alloc_count, repeats=args.repeats,
    )
    for row in allocation:
        print(f"  {row['size_bytes']//1024:6d} KB -> "
              f"{row['seconds_per_tensor']*1e6:7.2f} us/tensor", flush=True)

    traversal_per_tensor = statistics.median(
        r["seconds_per_tensor"] for r in traversal
    )
    alloc_curve = [(r["size_bytes"], r["seconds_per_tensor"])
                   for r in allocation]

    report = {
        "schema_version": SCHEMA_VERSION,
        "device_name": torch.cuda.get_device_name(0),
        "torch": torch.__version__,
        "traversal": traversal,
        "structure": structure,
        "allocation": allocation,
        "traversal_seconds_per_tensor": traversal_per_tensor,
        "allocation_curve": [{"size_bytes": s, "seconds_per_tensor": v}
                             for s, v in alloc_curve],
        "model": "framework = traversal_per_tensor * count "
                 "+ sum_i allocation(size_i)",
    }

    # Predict a real model's framework cost from its tensor inventory alone.
    if args.predict_model:
        from diffusers import DiffusionPipeline

        repos = {
            "sdxl": "stabilityai/stable-diffusion-xl-base-1.0",
            "cogvideox-2b": "THUDM/CogVideoX-2b",
            "cogvideox-5b": "THUDM/CogVideoX-5b",
        }
        kwargs = {"torch_dtype": torch.float16, "use_safetensors": True}
        if args.predict_model == "sdxl":
            kwargs["variant"] = "fp16"
        pipeline = DiffusionPipeline.from_pretrained(
            repos[args.predict_model], **kwargs
        )
        sizes = []
        for component in vars(pipeline).values():
            if isinstance(component, torch.nn.Module):
                for tensor in list(component.parameters()) + list(
                    component.buffers()
                ):
                    sizes.append(tensor.numel() * tensor.element_size())
        predicted = (
            traversal_per_tensor * len(sizes)
            + sum(interpolate(alloc_curve, size) for size in sizes)
        )
        report["prediction"] = {
            "model": args.predict_model,
            "tensors": len(sizes),
            "bytes": sum(sizes),
            "traversal_seconds": traversal_per_tensor * len(sizes),
            "allocation_seconds": sum(
                interpolate(alloc_curve, size) for size in sizes
            ),
            "predicted_framework_seconds": predicted,
            "predicted_per_tensor_us": predicted / len(sizes) * 1e6,
        }
        print(f"\n== prediction for {args.predict_model} ==")
        print(f"  {len(sizes)} tensors, {sum(sizes)/1e9:.2f} GB")
        print(f"  traversal  {traversal_per_tensor*len(sizes):.4f}s")
        print(f"  allocation {sum(interpolate(alloc_curve, s) for s in sizes):.4f}s")
        print(f"  framework  {predicted:.4f}s "
              f"({predicted/len(sizes)*1e6:.1f} us/tensor)")
        del pipeline

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(canonical_json(report) + "\n")
    print(f"report: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
