#!/usr/bin/env python3
"""Measure co-run overlap with the device's own events, no profiler.

plan.md's week 9-10 clause asks for a timeline confirming the expected
overlap. The obvious instrument, rocprofv3, does not work here: the
traced run stopped reproducing the effect under test -- externalities
went 10.5, 1.00, 1.01, 5.92 where the untraced harness gives 6.3, 6.2,
1.01, 1.01 in four processes out of four -- and it then took SIGSEGV in
``hsa_signal_wait_relaxed`` at teardown without writing a trace at all.
A timeline that changes what it observes answers a different question,
and the run's own validity check is what said so.

The measurement does not need a profiler. Both sides already bracket
every step with CUDA events on their own stream; all that is missing is a
common origin. Record one event before the episode, and after the episode
every event's offset from it puts both sides' intervals on a single device
timeline. Then overlap is arithmetic:

  * **busy** -- device time with at least one side inside a step.
  * **overlapped** -- device time with both sides inside a step.

Three properties this has and the profiler did not: it perturbs nothing
during the measured region -- the only synchronise is after the episode
ends; it measures the same quantity the externality is computed from,
since it is the same events; and it costs 28 events per side per episode.

The prediction, stated before the numbers were read and unchanged from
the rocprof attempt: the first two episodes overlap near zero and the
later ones do not. If both overlap, the step-time account of the
transient -- each side costing the sum of the two solo steps, fitted
across five splits within 3-9% -- is right about the arithmetic and wrong
about the cause.
"""

from __future__ import annotations

import argparse
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

from burstserve.executor import StepExecutor                    # noqa: E402
from burstserve.masked_streams import MaskedStreamPool          # noqa: E402
import run_amd_mismatched_corun as harness                      # noqa: E402


def coverage(intervals_a, intervals_b):
    """Busy and both-live milliseconds over two sets of intervals."""
    events = []
    for start, end in intervals_a:
        events += [(start, 1, "a"), (end, -1, "a")]
    for start, end in intervals_b:
        events += [(start, 1, "b"), (end, -1, "b")]
    events.sort()
    live = {"a": 0, "b": 0}
    busy = both = 0.0
    previous = None
    for stamp, delta, side in events:
        if previous is not None and stamp > previous:
            span = stamp - previous
            sides = sum(1 for v in live.values() if v > 0)
            if sides >= 1:
                busy += span
            if sides == 2:
                both += span
        live[side] += delta
        previous = stamp
    return busy, both


def episode(models, adapters, pool, units, args, torch, *, sync=True):
    """One co-run, keeping every step's start and end event."""
    left, right = pool.disjoint_pair(units[0], units[1])
    streams = {"a": left, "b": right}
    marks: dict[str, list] = {"a": [], "b": []}
    reported: dict[str, list] = {"a": [], "b": []}
    barrier = threading.Barrier(2)

    prepared = {}
    wrappers = {}
    for name, index in (("a", 0), ("b", 1)):
        adapter = adapters[index]
        adapter.stream = streams[name].handle
        wrappers[name] = torch.cuda.ExternalStream(streams[name].handle.value)
        executor = StepExecutor(object(), adapter, total_steps=args.steps)
        executor.prepare()
        prepared[name] = (adapter, executor)

    # The common origin, recorded on the default stream once both sides
    # are prepared and before either starts stepping.
    #
    # ``sync`` is a variable, not a detail. This harness synchronised here
    # and saw no transient at all, where the mismatched harness -- which
    # does not -- sees 6.3x for two episodes in four processes of four.
    # Two things differ between them and this is one; the other is whether
    # a full-die solo ran beforehand. Leaving the synchronise hard-coded
    # would have made the difference unattributable.
    if sync:
        torch.cuda.synchronize()
    origin = torch.cuda.Event(enable_timing=True)
    origin.record()

    def side(name, quota):
        adapter, executor = prepared[name]
        # Warm outside the marked region, as the other harnesses do, so
        # neither side's first step carries the other's start-up.
        executor.run_step(quota_units=quota)
        barrier.wait()
        for _ in range(args.steps - 1):
            before = torch.cuda.Event(enable_timing=True)
            after = torch.cuda.Event(enable_timing=True)
            before.record(wrappers[name])
            executor.run_step(quota_units=quota)
            after.record(wrappers[name])
            marks[name].append((before, after))
            # The adapter's own deferred reading, collected alongside.
            # The two harnesses disagree -- the mismatched one sees 6.3x
            # for two episodes in twelve processes and this one sees
            # nothing under any of four configurations -- and they differ
            # in which instrument they read. Reading both in one run says
            # whether the transient is in the die or in the reading.
            if adapter.last_step_seconds:
                reported[name].append(adapter.last_step_seconds)

    threads = [threading.Thread(target=side, args=(n, u))
               for n, u in (("a", units[0]), ("b", units[1]))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # The only synchronise, and it is after the episode. Reading the
    # offsets during it would drain the pipeline into the measurement --
    # the mistake that once made a 0.1155 s step read as 0.256 s.
    torch.cuda.synchronize()
    intervals = {
        name: [(origin.elapsed_time(before), origin.elapsed_time(after))
               for before, after in marks[name]]
        for name in ("a", "b")
    }
    for name in ("a", "b"):
        prepared[name][0].drain_timing()
    return intervals, reported


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--steps", type=int, default=12)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--height", type=int, default=768)
    parser.add_argument("--width", type=int, default=768)
    parser.add_argument("--video-height", type=int, default=480)
    parser.add_argument("--video-width", type=int, default=720)
    parser.add_argument("--frames", type=int, default=9)
    parser.add_argument("--seed", type=int, default=41)
    parser.add_argument("--split", default="16+16")
    parser.add_argument("--episodes", type=int, default=5)
    parser.add_argument("--models", default="sdxl,cogvideox-2b",
                        help="the two tenants; sdxl,sdxl measures the "
                             "self-paired case the latch campaign used")
    parser.add_argument("--no-sync-before-episode", action="store_true",
                        help="skip the device synchronise before each "
                             "episode, as the mismatched harness does")
    parser.add_argument("--also-solo-32", action="store_true",
                        help="run a full-die solo before the episodes, as "
                             "the mismatched harness does")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.keep_text_encoders = False

    import torch

    units = [int(u) for u in args.split.split("+")]
    models = [m.strip() for m in args.models.split(",")]
    pool = MaskedStreamPool(harness.make_stream)

    # Keyed by side, not by model: a self-paired run needs two adapters
    # of the same model, and one adapter cannot hold two requests'
    # streams at once.
    pipelines, adapters = {}, {}
    for index, model in enumerate(models):
        if model not in pipelines:
            print(f"loading {model} ...", flush=True)
            pipelines[model] = harness.build_pipeline(
                model, drop_text_encoders=False)
        adapters[index] = harness.make_adapter(model, pipelines[model], args,
                                               seed=args.seed + index)
    for model in pipelines:
        harness.free_text_encoders(pipelines[model])
    for index in range(2):
        for width in sorted({units[0], units[1], 32}):
            harness.warm(adapters[index], pool, width, args)

    solo = {}
    for index, model in enumerate(models):
        samples = harness.run_solo(adapters[index], pool, units[index], args)
        solo[index] = statistics.median(samples)
        if args.also_solo_32:
            harness.run_solo(adapters[index], pool, 32, args)
        print(f"  solo {model:13s} {units[index]:2d}u: "
              f"{solo[index] * 1000:8.1f} ms", flush=True)

    rows = []
    for index in range(1, args.episodes + 1):
        began = time.perf_counter()
        intervals, reported = episode(
            models, adapters, pool, units, args, torch,
            sync=not args.no_sync_before_episode)
        kept = {name: values[args.warmup:]
                for name, values in intervals.items()}
        busy, both = coverage(kept["a"], kept["b"])
        durations = {name: [end - start for start, end in kept[name]]
                     for name in ("a", "b")}
        row = {
            "episode": index,
            "busy_ms": busy,
            "overlapped_ms": both,
            "overlap_fraction": both / busy if busy else 0.0,
            "steps_kept": {n: len(kept[n]) for n in ("a", "b")},
            "elapsed_s": time.perf_counter() - began,
        }
        for name, side in (("a", 0), ("b", 1)):
            model = f"{models[side]}#{side}"
            step_ms = statistics.median(durations[name])
            said = reported[name][args.warmup:]
            row[model] = {
                "step_ms": step_ms,
                # The series, not just its median. The two instruments
                # disagree by 10x on the same steps, and a median cannot
                # show whether that is a few very slow steps or all of
                # them -- which is the whole question.
                "span_series_ms": durations[name],
                "adapter_series_s": said,
                "externality": step_ms / 1000.0 / solo[side],
                "adapter_step_s": statistics.median(said) if said else None,
                "adapter_externality": (statistics.median(said) / solo[side]
                                        if said else None),
            }
        rows.append(row)
        print(f"  ep {index}  overlap {row['overlap_fraction']:6.3f}   "
              + "  ".join(
                  f"{m} span {row[m]['externality']:6.3f} / adapter "
                  f"{row[m]['adapter_externality'] or float('nan'):6.3f}"
                  for m in (f"{models[0]}#0", f"{models[1]}#1")), flush=True)

    early = [r for r in rows if r["episode"] <= 2]
    late = [r for r in rows if r["episode"] > 2]
    payload = {
        "schema_version": "burstserve.amd-overlap-events/v1",
        "question": ("does the device timeline show the first two co-run "
                     "episodes running without overlap"),
        "prediction_stated_before_reading": (
            "first two episodes overlap near zero, later ones do not; if "
            "both overlap, the sum-of-solo-steps fit is right about the "
            "arithmetic and wrong about the cause"),
        "instrument": ("the sides' own CUDA events against a common "
                       "origin, one synchronise per episode and it is "
                       "after the episode"),
        "sync_before_episode": not args.no_sync_before_episode,
        "full_die_solo_before_episodes": bool(args.also_solo_32),
        "models": models,
        "split": args.split,
        "steps": args.steps,
        "warmup_dropped": args.warmup,
        "solo_p50_s": {str(k): v for k, v in solo.items()},
        "episodes": rows,
    }
    if early and late:
        payload["early_overlap"] = statistics.mean(
            r["overlap_fraction"] for r in early)
        payload["late_overlap"] = statistics.mean(
            r["overlap_fraction"] for r in late)
        payload["serialised_then_overlapping"] = (
            payload["early_overlap"] < 0.05
            and payload["late_overlap"] > 0.30)
        # The same validity check the profiler run failed.
        payload["transient_reproduced"] = (
            statistics.mean(r[f"{models[0]}#0"]["adapter_externality"]
                            for r in early) > 3.0
            and statistics.mean(r[f"{models[0]}#0"]["adapter_externality"]
                                for r in late) < 1.5)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2) + "\n")
    if "early_overlap" in payload:
        print(f"\nearly overlap {payload['early_overlap']:.3f}   "
              f"late overlap {payload['late_overlap']:.3f}")
        print(f"serialised then overlapping: "
              f"{payload['serialised_then_overlapping']}")
        print(f"transient reproduced: {payload['transient_reproduced']}")
    print(f"-> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
