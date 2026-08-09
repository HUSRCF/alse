#!/usr/bin/env python3
"""Run a mismatched co-run under rocprofv3, so the timeline can be read.

plan.md's week 9-10 acceptance includes a profiler timeline confirming
the expected overlap. On this hardware that is rocprofv3 rather than
Nsight, and the clause has acquired real content since it was written:
the mismatched pairing's first two episodes cost about the *sum* of the
two solo steps, across five splits, within 3-9%. That is what full
serialisation looks like, and a kernel timeline says directly whether it
is one -- no overlap for two episodes, overlap afterwards -- rather than
by inference from step times.

**This run carries its own validity check.** Profilers can serialise what
they observe, which here would manufacture exactly the finding being
tested. So the episodes report their externalities as the untraced
harness does: if the traced run does not reproduce ~6.3x for two episodes
and ~1.01 after, the timeline underneath it is describing the profiler.

Episodes are separated by an idle gap so the trace can be segmented on
the kernel timeline itself, without having to align rocprof's clock to
the host's.

Usage:
    rocprofv3 --kernel-trace --output-format csv -d DIR -o run -- \\
        python scripts/run_amd_overlap_trace.py --out run.json
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
sys.path.insert(0, str(REPO / "scripts"))

from burstserve.masked_streams import MaskedStreamPool          # noqa: E402
import run_amd_mismatched_corun as harness                      # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--steps", type=int, default=8)
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--height", type=int, default=768)
    parser.add_argument("--width", type=int, default=768)
    parser.add_argument("--video-height", type=int, default=480)
    parser.add_argument("--video-width", type=int, default=720)
    parser.add_argument("--frames", type=int, default=9)
    parser.add_argument("--seed", type=int, default=31)
    parser.add_argument("--split", default="16+16")
    parser.add_argument("--episodes", type=int, default=4)
    parser.add_argument("--gap-s", type=float, default=1.5,
                        help="idle time between episodes, so the kernel "
                             "timeline segments itself")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.keep_text_encoders = False

    import torch

    units = [int(u) for u in args.split.split("+")]
    models = ["sdxl", "cogvideox-2b"]
    pool = MaskedStreamPool(harness.make_stream)

    pipelines, adapters = {}, {}
    for index, model in enumerate(models):
        print(f"loading {model} ...", flush=True)
        pipelines[model] = harness.build_pipeline(model,
                                                  drop_text_encoders=False)
        adapters[model] = harness.make_adapter(model, pipelines[model], args,
                                               seed=args.seed + index)
        harness.free_text_encoders(pipelines[model])

    for model in models:
        for width in sorted({units[0], units[1], 32}):
            harness.warm(adapters[model], pool, width, args)

    solo = {}
    for index, model in enumerate(models):
        for width in (units[index], 32):
            samples = harness.run_solo(adapters[model], pool, width, args)
            solo[(model, width)] = statistics.median(samples)
            print(f"  solo {model:13s} {width:2d}u: "
                  f"{solo[(model, width)] * 1000:8.1f} ms", flush=True)

    torch.cuda.synchronize()
    episodes = []
    for index in range(1, args.episodes + 1):
        # The gap is what segments the trace. It goes before the episode
        # as well as after, so the first one is bounded too.
        time.sleep(args.gap_s)
        torch.cuda.synchronize()
        began = time.clock_gettime(time.CLOCK_MONOTONIC)
        paired = harness.run_pair(models, adapters, pool, units, args)
        torch.cuda.synchronize()
        ended = time.clock_gettime(time.CLOCK_MONOTONIC)
        row = {"episode": index, "monotonic_start": began,
               "monotonic_end": ended}
        for name, model, quota in (("a", models[0], units[0]),
                                   ("b", models[1], units[1])):
            step = statistics.median(paired[f"{name}_all"])
            row[model] = {"corun_p50_s": step,
                          "externality": step / solo[(model, quota)]}
        episodes.append(row)
        print(f"  ep {index}  "
              + "  ".join(f"{m} {row[m]['externality']:6.3f}"
                          for m in models), flush=True)
    time.sleep(args.gap_s)
    torch.cuda.synchronize()

    early = [e for e in episodes if e["episode"] <= 2]
    late = [e for e in episodes if e["episode"] > 2]
    payload = {
        "schema_version": "burstserve.amd-overlap-trace/v1",
        "question": ("does the kernel timeline show no overlap for the "
                     "first two episodes and overlap afterwards"),
        "models": models,
        "split": args.split,
        "steps": args.steps,
        "gap_s": args.gap_s,
        "solo_p50_s": {f"{m}@{u}": v for (m, u), v in solo.items()},
        "episodes": episodes,
        # The validity check. A profiler that serialised what it observed
        # would flatten these, and the timeline under it would then be
        # describing the profiler rather than the die.
        "transient_reproduced": bool(
            early and late
            and statistics.mean(e[models[0]]["externality"] for e in early)
            > 3.0
            and statistics.mean(e[models[0]]["externality"] for e in late)
            < 1.5),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"transient reproduced under the profiler: "
          f"{payload['transient_reproduced']}")
    print(f"-> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
