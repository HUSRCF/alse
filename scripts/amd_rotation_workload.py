"""Load a model once, then serve it ``--rotations`` times.

The point of the shape is that the load happens exactly once regardless of
the rotation count, so a regression of copied bytes against that count
separates the one-off weight transfer from the per-request cost without
needing the profiler's clock to line up with this process's clock.

It also reports the model's own weight size, because "zero weight traffic"
is a claim about weights and has to be judged against them rather than
against an absolute byte count.
"""

from __future__ import annotations

import argparse
import json
import sys

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
    parser.add_argument("--rotations", type=int, default=1)
    parser.add_argument("--steps", type=int, default=20)
    parser.add_argument("--height", type=int, default=1024)
    parser.add_argument("--width", type=int, default=1024)
    parser.add_argument("--frames", type=int, default=49)
    parser.add_argument("--prompt", default="a quiet street at dusk")
    args = parser.parse_args()

    import torch
    from diffusers import DiffusionPipeline

    repo = MODEL_REPOS[args.model]
    kwargs = {"torch_dtype": torch.float16, "use_safetensors": True}
    if args.model in MODEL_VARIANT:
        kwargs["variant"] = MODEL_VARIANT[args.model]
    pipeline = DiffusionPipeline.from_pretrained(repo, **kwargs).to("cuda")
    pipeline.set_progress_bar_config(disable=True)

    weight_bytes = 0
    for component in vars(pipeline).values():
        if isinstance(component, torch.nn.Module):
            weight_bytes += sum(
                p.numel() * p.element_size() for p in component.parameters()
            )
            weight_bytes += sum(
                b.numel() * b.element_size() for b in component.buffers()
            )

    call = {
        "prompt": [args.prompt],
        "num_inference_steps": args.steps,
    }
    if args.model.startswith("cogvideox"):
        call["num_frames"] = args.frames
    else:
        call["height"] = args.height
        call["width"] = args.width

    for index in range(args.rotations):
        generator = torch.Generator(device="cuda").manual_seed(index)
        with torch.inference_mode():
            pipeline(generator=generator, **call)
    torch.cuda.synchronize()

    print(json.dumps({
        "status": "ok",
        "model": args.model,
        "repo": repo,
        "rotations": args.rotations,
        "weight_bytes": weight_bytes,
        "peak_memory_bytes": torch.cuda.max_memory_allocated(),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
