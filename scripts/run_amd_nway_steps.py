#!/usr/bin/env python3
"""1.11's falsifier: the N-way co-run penalty, measured per STEP.

`run_amd_nway_corun.py` measured it by issuing whole ``pipeline(...)``
calls from N threads of one process. On 2026-09-04 that arrangement was
shown to charge **device drains** as contention: the same mismatched pair
read 7.3x through it and 0.995 through a resident step adapter, and the
difference resolved into one `hipMalloc`-class drain per step, exactly
one peer step long. `run_side` warms with a single whole call, where the
step-level harness needed 28 co-run steps before the drains stopped.

Same-model slices bound that exposure -- a drain costs one peer step
rather than the 4.4 of them a CogVideoX peer costs -- but a drain waits
for **every** in-flight peer, so an artefact grows with N in precisely
the shape 1.11 reports. Inspection cannot separate them. This measures it
the way the number that survived was measured.

What is different from the call-level harness, and why:

  * **A resident adapter per slice**, stepped in place, rather than a
    fresh activation graph allocated per call. This is also what the
    runtime does.
  * **Episodes, all of them reported.** The transient is the finding of
    2026-09-04, not noise to be dropped; the verdict is the last episode
    as in 1.5, and every episode is in the payload.
  * **A solo per slice on its own mask**, before and after, because
    dividing every side by one side's baseline is the defect that once
    made an 8+24 pair read -44.6%, and because the die warms.

Each adapter carries a scheduler of its own (`amd_sdxl_adapter` builds
one with `from_config`), so N of them over one copy of the weights do not
clobber each other's step index.

Masks are read back after installation and every pair is checked for
overlap. A runtime that quietly widens a mask produces an unusually LOW
penalty, which reads as good news.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import statistics
import sys
import threading
import time
from pathlib import Path

sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

from burstserve.executor import StepExecutor            # noqa: E402
from burstserve.masked_streams import MaskedStreamPool  # noqa: E402
from burstserve.provenance import canonical_json        # noqa: E402
import run_amd_mismatched_corun as harness              # noqa: E402

SCHEMA_VERSION = "burstserve.amd-nway-steps/v1"

# `harness` loads libamdhip64 and declares the calls it uses; this
# one is only needed by --diagnose, so it is declared here.
harness.hip.hipStreamDestroy.restype = ctypes.c_int
harness.hip.hipStreamDestroy.argtypes = [ctypes.c_void_p]


def p50(values):
    return statistics.median(values) if values else None


def wall_per_step(began, ended, steps):
    """Seconds per step from the wall clock. **This is the measurement.**

    The adapter reports a step's duration only when the previous step's
    end event has already been passed by the device
    (`previous_end.query()`); when the CPU runs ahead -- which it does in
    any tight step loop -- the read is skipped and `last_step_seconds`
    keeps its OLD value. A caller that appends it every iteration records
    one stale reading N times and takes its median.

    Caught 2026-09-04 in the first cross-process run, where a slice read
    306.7 ms by events and 156.0 ms by the wall over the same 14 steps in
    a phase that took 2.2 s. Every event p50 in this file is kept beside
    the wall figure so the two can be compared, and they are reported
    separately rather than one silently standing in for the other.

    **The wall clock only works if the device is synchronised before it
    is stopped.** A step loop enqueues asynchronously, so without a sync
    the clock measures how fast the CPU could submit the work, not how
    long the work took -- 14 steps of a 269 ms quota came back at 156 ms
    the first time this was tried. Every caller here syncs first.
    """
    return (ended - began) / max(1, steps)


def run_solo(adapter, stream, units, args) -> dict:
    import torch
    adapter.stream = stream.handle
    executor = StepExecutor(object(), adapter, total_steps=args.steps)
    executor.prepare()
    seen: list[float] = []
    steps = 0
    began = time.perf_counter()
    while True:
        more = executor.run_step(quota_units=units)
        steps += 1
        # The adapter reports the previous step, so a reading appears one
        # step late -- and is simply not updated at all when the device
        # is behind, which is why the wall clock is the measurement.
        if adapter.last_step_seconds:
            seen.append(adapter.last_step_seconds)
        if not more:
            break
    # Synchronise BEFORE stopping the clock: the loop above only
    # enqueued the work.
    adapter.drain_timing()
    torch.cuda.synchronize()
    ended = time.perf_counter()
    if adapter.last_step_seconds:
        seen.append(adapter.last_step_seconds)
    return {"p50_s": wall_per_step(began, ended, steps),
            "event_p50_s": p50(seen[args.warmup:]),
            "steps": steps, "wall_s": ended - began}


def run_episode(adapters, streams, widths, args):
    """N adapters stepping concurrently on N disjoint masks."""
    import torch

    ways = len(adapters)
    out: list[list[float]] = [[] for _ in range(ways)]
    windows: list[list[tuple[float, float, float]]] = [[] for _ in range(ways)]
    barrier = threading.Barrier(ways)

    prepared = []
    for index, adapter in enumerate(adapters):
        adapter.stream = streams[index].handle
        executor = StepExecutor(object(), adapter, total_steps=args.steps)
        executor.prepare()
        prepared.append((adapter, executor))

    walls: list[tuple[float, float, int]] = [(0.0, 0.0, 0)] * ways

    def side(index):
        adapter, executor = prepared[index]
        # Warm before the barrier so no slice measures another's start-up.
        executor.run_step(quota_units=widths[index])
        barrier.wait()
        opened = time.perf_counter()
        counted = 0
        for _ in range(args.steps - 1):
            began = time.perf_counter()
            executor.run_step(quota_units=widths[index])
            ended = time.perf_counter()
            counted += 1
            if adapter.last_step_seconds:
                out[index].append(adapter.last_step_seconds)
                windows[index].append((began, ended,
                                       adapter.last_step_seconds))
        # `drain_timing` synchronises on THIS adapter's own event, which
        # is the right scope. `torch.cuda.synchronize()` here is
        # device-wide, and called from eight threads at once it hung the
        # eight-way cell for over two hours at 92% GPU with no progress.
        # A per-slice wall time must not wait on the other slices anyway:
        # it would make every slice's figure the slowest one's.
        adapter.drain_timing()
        closed = time.perf_counter()
        walls[index] = (opened, closed, counted)
        if adapter.last_step_seconds:
            out[index].append(adapter.last_step_seconds)

    threads = [threading.Thread(target=side, args=(i,)) for i in range(ways)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # Only steps that ran while EVERY slice was active. With N sides the
    # window is the intersection of all of them, not of a chosen pair.
    starts = [w[0][0] for w in windows if w]
    ends = [w[-1][1] for w in windows if w]
    lo = max(starts) if len(starts) == ways else None
    hi = min(ends) if len(ends) == ways else None

    def overlapped(index):
        if lo is None or hi is None:
            return []
        return [seconds for began, ended, seconds in windows[index]
                if began >= lo and ended <= hi]

    return {
        "all": [series[args.warmup:] for series in out],
        "wall_per_step": [wall_per_step(o, c, n) for o, c, n in walls],
        "wall": [[o, c, n] for o, c, n in walls],
        "overlap": [overlapped(i)[args.warmup:] for i in range(ways)],
        "overlap_seconds": (hi - lo) if lo is not None and hi is not None
        else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="sdxl")
    parser.add_argument("--models", default=None,
                        help="comma-separated model per slice, e.g. "
                             "`sdxl,cogvideox-2b`. One pipeline is built "
                             "per DISTINCT model and shared by the slices "
                             "that use it, which is what a runtime does. "
                             "This is claim 1.5's arrangement, measured "
                             "with the instrument fixed on 2026-09-04.")
    parser.add_argument("--ways", type=int, default=4)
    parser.add_argument("--maskable-units", type=int, default=32,
                        help="the whole die: 32 on gfx1201, 104 on gfx90a")
    parser.add_argument("--widths", default=None,
                        help="comma-separated slice widths instead of equal "
                             "division, e.g. `26,78`. The pairwise "
                             "externality table's entries are unequal pairs, "
                             "so without this the N-way column and the "
                             "pairwise column it is compared against come "
                             "from different harnesses measuring different "
                             "quantities. Overrides --ways.")
    parser.add_argument("--reverse-offsets", action="store_true",
                        help="lay the slices out from the top of the mask "
                             "down. The 2026-09-04 sweep found a monotone "
                             "gradient across slice index -- 1.474 at "
                             "offset 0 against 1.377 at the top, four ways "
                             "-- and this is its falsifier: if the gradient "
                             "follows the slice's position it reverses, if "
                             "it follows the thread it does not.")
    parser.add_argument("--steps", type=int, default=14)
    parser.add_argument("--warmup", type=int, default=4)
    parser.add_argument("--episodes", type=int, default=6,
                        help="all of them are reported. The transient is "
                             "the point: on 2026-09-04 a mismatched pair "
                             "read 5.36 in episode 1 and 0.995 by episode 3.")
    parser.add_argument("--height", type=int, default=768)
    parser.add_argument("--width", type=int, default=768)
    parser.add_argument("--video-height", type=int, default=480)
    parser.add_argument("--video-width", type=int, default=720)
    parser.add_argument("--frames", type=int, default=9)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--keep-text-encoders", action="store_true")
    parser.add_argument("--diagnose", action="store_true",
                        help="after the episodes, measure the solo a third "
                             "time with the allocator's cache dropped, and "
                             "a fourth after destroying every peer stream. "
                             "Added 2026-09-04 because solo_after came back "
                             "equal to the co-run in all 14 runs on both "
                             "architectures -- a co-run penalty must vanish "
                             "when the peers stop, and this one did not.")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    import torch

    if args.widths:
        widths = [int(w) for w in args.widths.split(",")]
        if sum(widths) > args.maskable_units:
            raise SystemExit(f"{widths} exceeds {args.maskable_units} units")
        args.ways = len(widths)
    else:
        if args.maskable_units % args.ways:
            raise SystemExit(f"{args.maskable_units} units do not divide "
                             f"into {args.ways} equal slices")
        widths = [args.maskable_units // args.ways] * args.ways
    units = widths[0] if len(set(widths)) == 1 else None

    pool = MaskedStreamPool(harness.make_stream,
                            maskable_units=args.maskable_units)

    slice_models = ([m.strip() for m in args.models.split(",")]
                    if args.models else [args.model] * args.ways)
    if len(slice_models) != args.ways:
        raise SystemExit(f"{len(slice_models)} models for {args.ways} slices")
    pipelines = {}
    for name in dict.fromkeys(slice_models):
        print(f"loading {name} ...", flush=True)
        pipelines[name] = harness.build_pipeline(name,
                                                 drop_text_encoders=False)
    print(f"  {len(pipelines)} pipeline(s) loaded; building adapters ...",
          flush=True)
    adapters = [harness.make_adapter(name, pipelines[name], args,
                                     seed=args.seed + i)
                for i, name in enumerate(slice_models)]
    print("  adapters built; releasing text encoders ...", flush=True)
    released = 0
    if not args.keep_text_encoders:
        for pipeline in pipelines.values():
            released += harness.free_text_encoders(pipeline)
    print(f"  {args.ways} adapters over {len(pipelines)} copy/copies of "
          f"the weights, {torch.cuda.memory_allocated() / 2**30:.2f} GB "
          f"resident, released {released / 2**30:.2f} GB of text encoder",
          flush=True)

    offsets, cursor = [], 0
    for width in widths:
        offsets.append(cursor)
        cursor += width
    if args.reverse_offsets:
        offsets = [args.maskable_units - o - w
                   for o, w in zip(offsets, widths)]
    streams = [pool.for_quota(w, offset=o)
               for w, o in zip(widths, offsets)]
    for i, left in enumerate(streams):
        for right in streams[i + 1:]:
            if left.installed_mask & right.installed_mask:
                raise SystemExit("slices overlap after installation")
        if left.installed_mask != left.requested_mask:
            raise SystemExit(f"mask readback differs for slice {i}")

    print(f"streams installed; warming {args.ways} slices of {widths} "
          f"units ...", flush=True)
    for adapter, width in zip(adapters, widths):
        harness.warm(adapter, pool, width, args)

    solo_before = []
    for index, adapter in enumerate(adapters):
        measured = run_solo(adapter, streams[index], widths[index], args)
        solo_before.append({"slice": index, "units": widths[index],
                            "offset": offsets[index],
                            "model": slice_models[index], **measured})
        print(f"  solo slice {index} @{widths[index]}u: "
              f"{solo_before[-1]['p50_s'] * 1000:8.1f} ms", flush=True)

    episodes = []
    for episode in range(1, args.episodes + 1):
        print(f"co-run {args.ways} x {widths}u [episode {episode}] ...",
              flush=True)
        result = run_episode(adapters, streams, widths, args)
        rows = []
        for index in range(args.ways):
            corun = result["wall_per_step"][index]
            overlap = p50(result["overlap"][index])
            alone = solo_before[index]["p50_s"]
            rows.append({
                "slice": index, "units": widths[index],
                "offset": offsets[index], "model": slice_models[index],
                "corun_p50_s": corun,
                "corun_event_p50_s": p50(result["all"][index]),
                "corun_overlap_p50_s": overlap,
                "solo_at_quota_s": alone,
                "externality": (corun / alone if corun and alone else None),
            })
            print(f"    slice {index} co-run {corun * 1000:8.1f} ms  "
                  f"ext {rows[-1]['externality']:6.3f}", flush=True)
        episodes.append({"episode": episode, "rows": rows,
                         "overlap_seconds": result["overlap_seconds"],
                         "series_s": result["all"]})

    # Solo again at the end: the die warms over a run and a ratio taken
    # against a colder baseline is not the ratio it looks like.
    solo_after = []
    for index, adapter in enumerate(adapters):
        measured = run_solo(adapter, streams[index], widths[index], args)
        solo_after.append({"slice": index, "units": widths[index],
                           **measured})
    print("  solo after episodes: "
          + " ".join(f"{r['p50_s'] * 1000:.1f}" for r in solo_after),
          flush=True)

    # Two more solos, each with one candidate cause removed. This is the
    # 2026-09-04 control: solo_after equalled the co-run everywhere, and
    # a penalty that survives the peers stopping is not an externality.
    solo_emptied: list[dict] = []
    solo_streams_gone: list[dict] = []
    if args.diagnose:
        import gc
        gc.collect()
        torch.cuda.empty_cache()
        for index, adapter in enumerate(adapters):
            measured = run_solo(adapter, streams[index], widths[index], args)
            solo_emptied.append({"slice": index, "units": widths[index],
                                 **measured})
        print("  solo, allocator cache dropped: "
              + " ".join(f"{r['p50_s'] * 1000:.1f}" for r in solo_emptied),
              flush=True)

        # Slice 0 alone with every peer stream destroyed. If N live
        # masked streams cost something by merely existing, this is where
        # it shows; solo_after already covers "peers resident but idle".
        # Wrapped because destroying a stream out from under the pool is
        # a one-way door and the two solos above are the result that
        # matters.
        try:
            for stream in streams[1:]:
                harness.hip.hipStreamDestroy(stream.handle)
            gc.collect()
            torch.cuda.empty_cache()
            measured = run_solo(adapters[0], streams[0], widths[0], args)
            solo_streams_gone.append({"slice": 0, "units": widths[0],
                                      **measured})
            print("  solo, peer streams destroyed: "
                  f"{solo_streams_gone[0]['p50_s'] * 1000:.1f}", flush=True)
        except Exception as failure:            # noqa: BLE001
            solo_streams_gone.append({"error": repr(failure)})
            print(f"  peer-stream drop failed: {failure!r}", flush=True)

    verdict = episodes[-1]["rows"]
    values = [row["externality"] for row in verdict if row["externality"]]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "question": ("the N-way same-model co-run penalty measured per "
                     "step on resident adapters, which is 1.11's falsifier"),
        "measured_through": ("one resident step adapter per slice, stepped "
                             "in place; no per-call activation graph, so a "
                             "warm allocator stays warm"),
        "model": args.model,
        "slice_models": slice_models,
        "ways": args.ways,
        "slice_units": units,
        "slice_widths": widths,
        "slice_offsets": offsets,
        "reverse_offsets": args.reverse_offsets,
        "maskable_units": args.maskable_units,
        "steps": args.steps,
        "warmup_dropped": args.warmup,
        "episodes": args.episodes,
        "seed": args.seed,
        "workpoint": (f"{args.width}x{args.height}" if args.model == "sdxl"
                      else f"{args.frames} frames "
                           f"{args.video_width}x{args.video_height}"),
        "text_encoders_released_bytes": released,
        "stream_attestation": pool.attestation(),
        "solo_before": solo_before,
        "solo_after": solo_after,
        "solo_after_empty_cache": solo_emptied,
        "solo_after_peers_dropped": solo_streams_gone,
        "per_episode": episodes,
        "verdict": verdict,
        "externality_mean_last_episode": (statistics.mean(values)
                                          if values else None),
        "torch": torch.__version__,
        "device": {"name": torch.cuda.get_device_name(0),
                   "arch": torch.cuda.get_device_properties(0).gcnArchName,
                   "multi_processor_count":
                       torch.cuda.get_device_properties(0)
                       .multi_processor_count},
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(canonical_json(payload))
    print(f"\n{args.ways} ways x {widths}u  last-episode mean externality "
          f"{payload['externality_mean_last_episode']:.4f}", flush=True)
    print(f"-> {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
