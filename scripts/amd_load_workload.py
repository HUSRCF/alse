"""Load a model to the device, several times, and do nothing else.

Nothing else is the point. Run under a memory-copy profiler, a process
that only loads performs no host-to-device copies except the ones the load
performs, so the profiler's total is the transfer without any need to
align its clock with this process's, or to construct a control that
reproduces what ``.to(device)`` does internally.

Host pages are faulted in before the timed move: safetensors maps the
checkpoint, so an untouched pipeline's first move would also be a disk
read.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

sys.dont_write_bytecode = True

MODEL_REPOS = {
    "sdxl": "stabilityai/stable-diffusion-xl-base-1.0",
    "cogvideox-2b": "THUDM/CogVideoX-2b",
    "cogvideox-5b": "THUDM/CogVideoX-5b",
    "flux-dev": "black-forest-labs/FLUX.1-dev",
}
MODEL_VARIANT = {"sdxl": "fp16"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="sdxl")
    parser.add_argument("--reloads", type=int, default=4)
    args = parser.parse_args()

    import torch
    from diffusers import DiffusionPipeline

    repo = MODEL_REPOS[args.model]
    kwargs = {"torch_dtype": torch.float16, "use_safetensors": True}
    if args.model in MODEL_VARIANT:
        kwargs["variant"] = MODEL_VARIANT[args.model]

    torch.zeros(1, device="cuda")  # context up before anything is timed

    seconds, tensors, weight_bytes = [], 0, 0
    for _ in range(max(1, args.reloads)):
        pipeline = DiffusionPipeline.from_pretrained(repo, **kwargs)

        sizes = []
        for component in vars(pipeline).values():
            if isinstance(component, torch.nn.Module):
                for tensor in list(component.parameters()) + list(
                    component.buffers()
                ):
                    sizes.append(tensor.numel() * tensor.element_size())
                    tensor.data = tensor.data.clone()  # fault in, detach map
        tensors, weight_bytes = len(sizes), sum(sizes)

        torch.cuda.synchronize()
        started = time.perf_counter()
        pipeline = pipeline.to("cuda")
        torch.cuda.synchronize()
        seconds.append(time.perf_counter() - started)

        del pipeline
        torch.cuda.empty_cache()

    print(json.dumps({
        "status": "ok",
        "model": args.model,
        "repo": repo,
        "reloads": len(seconds),
        "tensors": tensors,
        "weight_bytes": weight_bytes,
        "to_device_seconds": seconds,
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
