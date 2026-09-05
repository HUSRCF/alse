#!/usr/bin/env python3
"""The co-run penalty, with one tenant per PROCESS.

Every co-run number this project has ever published was measured with
two threads of **one** process. On 2026-09-04 that arrangement was caught
twice: once charging `hipMalloc`-class device drains as contention
(claim 1.5's 7.3x, which is 0.995 when the allocator is warm), and once
charging allocator *fragmentation* as contention -- claim 1.11, where
`torch.cuda.empty_cache()` restores every slice to its solo exactly with
the peers still resident. Both are process-local software costs wearing
a hardware number's clothes.

One process per tenant removes the shared caching allocator, the shared
GIL and the shared HIP context in one move, and it is what a deployed
system looks like anyway: tenants are separate model servers. What is
left, if anything, is the die.

**How the masking works here.** `ROC_GLOBAL_CU_MASK` in the child's
environment, which is the only masking route verified to reach PyTorch on
this stack (2026-08-02, Gate B-AMD). It masks the whole process, so the
worker uses the default stream and no masked-stream machinery at all.
The mask cannot be read back from HIP, so it is verified the only way
available: a masked process's solo step time must land on that width's
measured quota curve, and the coordinator prints predicted against
measured for every slice and refuses the run if any is out by more than
`--quota-tolerance`. A mask that silently failed to apply would show up
as a slice running at full-die speed.

**How "solo" is guaranteed to be solo.** Not by assumption. The workers
share a timetable of absolute wall-clock times, and their solo slots are
staggered so no two overlap. Every worker records the wall interval of
every phase it runs, and the coordinator **checks after the fact** that
each solo interval intersects no other worker's interval, and that the
co-run intervals do intersect. A run whose schedule slipped is reported
as invalid rather than averaged.

**What is measured.** Per slice: solo before, co-run inside the
intersection of every worker's co-run interval, solo after, and -- with
`--diagnose` -- solo after `torch.cuda.empty_cache()`. The last is the
control that killed 1.11 and it is kept here so that a repeat is caught
in the same run rather than a day later.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

SCHEMA_VERSION = "burstserve.amd-xproc-corun/v1"


def mask_for(units: int, offset: int, maskable: int) -> str:
    if units < 1 or offset < 0 or offset + units > maskable:
        raise ValueError(f"{units} units at offset {offset} does not fit "
                         f"in {maskable}")
    return hex(((1 << units) - 1) << offset)


def p50(values):
    return statistics.median(values) if values else None


def wall_per_step(phase):
    """Seconds per step from the phase's wall clock.

    **This is the primary measurement and the event p50 is not.** The
    adapter reports a step's duration only when the previous step's end
    event has already been passed by the device (`previous_end.query()`);
    when the CPU runs ahead -- which it does in any tight step loop --
    the read is skipped and `last_step_seconds` keeps its OLD value. A
    caller that appends it every iteration therefore records one stale
    reading N times, and its median is that stale reading.

    Caught 2026-09-04 in the first cross-process run: slice 1's solo read
    306.7 ms by events and 156.0 ms by the wall over the same 14 steps,
    and the phase took 2.2 s, which only the wall figure can produce.
    """
    samples = phase["samples"]
    # The drain reading is appended after the loop and is not a step.
    steps = max(1, len(samples) - (1 if phase.get("drained") else 0))
    began, ended = phase["wall"]
    return (ended - began) / steps


def event_p50(phase):
    return p50([s["s"] for s in phase["samples"]])


# --------------------------------------------------------------------- worker

def wait_until(when: float) -> None:
    """Sleep to just before the mark, then spin. Slots are seconds apart
    and a 50 ms scheduling slip would put two solos on the die at once."""
    while True:
        remaining = when - time.time()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 0.5) if remaining > 0.05 else 0)


def run_phase(adapter, executor_for, units, count):
    """`count` steps, timed by the adapter, with wall stamps.

    The adapter reports the PREVIOUS step, so a reading appears one step
    late; synchronising on the step just issued would drain the pipeline
    and charge the drain to the measurement. The wall stamps recorded
    beside a reading are therefore the stamps of the step after the one
    the reading describes, which over a window of hundreds of steps moves
    an interval edge by one step and nothing else.
    """
    samples, began_at = [], time.time()
    executor = executor_for()
    done = 0
    while done < count:
        began = time.time()
        # False means that was the last step of this request, not a
        # failure; the next one needs a fresh executor, which is what a
        # runtime does when a request completes.
        more = executor.run_step(quota_units=units)
        ended = time.time()
        done += 1
        if adapter.last_step_seconds:
            samples.append({"s": adapter.last_step_seconds,
                            "began": began, "ended": ended})
        if not more:
            executor = executor_for()
    # Synchronise BEFORE stopping the clock: the loop only enqueued.
    import torch
    adapter.drain_timing()
    torch.cuda.synchronize()
    ended_at = time.time()
    drained = False
    if adapter.last_step_seconds:
        samples.append({"s": adapter.last_step_seconds,
                        "began": ended_at, "ended": ended_at})
        drained = True
    return {"samples": samples, "wall": [began_at, ended_at],
            "steps": done, "drained": drained}


def run_window(adapter, executor_for, units, seconds):
    """Steps back to back for a fixed window, rather than a fixed count.

    A count lets the fast slice finish early, and the rest then measure
    with more of the die than they were given, which is not the
    arrangement.
    """
    samples, began_at = [], time.time()
    stop_at = began_at + seconds
    executor = executor_for()
    while time.time() < stop_at:
        began = time.time()
        more = executor.run_step(quota_units=units)
        ended = time.time()
        if adapter.last_step_seconds:
            samples.append({"s": adapter.last_step_seconds,
                            "began": began, "ended": ended})
        if not more:
            executor = executor_for()
    import torch
    adapter.drain_timing()
    torch.cuda.synchronize()
    ended_at = time.time()
    return {"samples": samples, "wall": [began_at, ended_at],
            "steps": len(samples), "drained": False}


def worker(args) -> int:
    import torch
    from burstserve.executor import StepExecutor
    import run_amd_mismatched_corun as harness

    rundir = Path(args.rundir)
    pipeline = harness.build_pipeline(args.model, drop_text_encoders=False)
    adapter = harness.make_adapter(args.model, pipeline, args,
                                   seed=args.seed + args.slice_index)
    released = harness.free_text_encoders(pipeline)
    adapter.stream = None          # the whole process is masked already

    def executor_for():
        made = StepExecutor(object(), adapter, total_steps=args.steps)
        made.prepare()
        return made

    # Kernel selection is a property of the process and costs seconds the
    # first time; paying it here keeps it out of every measured phase.
    run_phase(adapter, executor_for, args.units, args.warmup_steps)

    ready = {"slice": args.slice_index, "pid": os.getpid(),
             "model": args.model,
             "mask": os.environ.get("ROC_GLOBAL_CU_MASK"),
             "units": args.units, "offset": args.offset,
             "device": torch.cuda.get_device_name(0),
             "arch": torch.cuda.get_device_properties(0).gcnArchName,
             "multi_processor_count":
                 torch.cuda.get_device_properties(0).multi_processor_count,
             "resident_gb": torch.cuda.memory_allocated() / 2 ** 30,
             "text_encoders_released_bytes": released,
             "at": time.time()}
    (rundir / f"ready_{args.slice_index}.json").write_text(json.dumps(ready))

    timetable_path = rundir / "timetable.json"
    deadline = time.time() + args.startup_timeout
    while not timetable_path.exists():
        if time.time() > deadline:
            raise SystemExit("timetable never appeared")
        time.sleep(0.2)
    timetable = json.loads(timetable_path.read_text())

    wait_until(timetable["solo_before"][args.slice_index])
    solo_before = run_phase(adapter, executor_for, args.units, args.steps)

    wait_until(timetable["corun"])
    corun = run_window(adapter, executor_for, args.units, args.window_s)

    wait_until(timetable["solo_after"][args.slice_index])
    solo_after = run_phase(adapter, executor_for, args.units, args.steps)

    emptied = None
    if args.diagnose:
        import gc
        gc.collect()
        torch.cuda.empty_cache()
        wait_until(timetable["solo_emptied"][args.slice_index])
        emptied = run_phase(adapter, executor_for, args.units, args.steps)

    (rundir / f"result_{args.slice_index}.json").write_text(json.dumps({
        "ready": ready, "solo_before": solo_before, "corun": corun,
        "solo_after": solo_after, "solo_emptied": emptied,
        "torch": torch.__version__,
    }))
    return 0


# ---------------------------------------------------------------- coordinator

def spans(phase):
    return tuple(phase["wall"]) if phase else None


def overlaps(a, b, slack=0.0):
    return a and b and a[0] < b[1] - slack and b[0] < a[1] - slack


def curve_for(model_name, device):
    """The measured quota curve, on trees that have a device dimension.

    X570's tree predates it -- `QuotaCostModel.for_model` there takes no
    `device` -- and syncing `trace_sim` into a hardware tree to satisfy a
    probe is how a campaign stops being commensurable with the ones
    before it. The fallback keeps the check working there.
    """
    from burstserve.trace_sim import QuotaCostModel
    try:
        return QuotaCostModel.for_model(model_name, device=device)
    except TypeError:
        return QuotaCostModel.for_model(model_name)


def summarise(rundir: Path, args) -> int:
    """Everything after the workers have written their results.

    Separate so that a crash in the reporting cannot destroy a
    measurement that already ran, which is exactly what happened the
    first time this was used: the workers finished, the coordinator died
    on a keyword argument, and only the raw files survived.
    """
    results, widths, offsets, models = [], [], [], []
    for path in sorted(rundir.glob("result_*.json"),
                       key=lambda q: int(q.stem.split("_")[1])):
        result = json.loads(path.read_text())
        results.append(result)
        widths.append(result["ready"]["units"])
        offsets.append(result["ready"]["offset"])
        models.append(result["ready"].get("model", args.model))
    if not results:
        raise SystemExit(f"no result_*.json under {rundir}")
    return report(results, widths, offsets, models, args)


def coordinator(args) -> int:
    models = ([m.strip() for m in args.models.split(",")]
              if args.models else None)
    if args.widths:
        widths = [int(w) for w in args.widths.split(",")]
    else:
        if args.maskable_units % args.ways:
            raise SystemExit(f"{args.maskable_units} units do not divide "
                             f"into {args.ways} equal slices")
        widths = [args.maskable_units // args.ways] * args.ways
    if sum(widths) > args.maskable_units:
        raise SystemExit(f"{widths} exceeds {args.maskable_units} units")
    if models is None:
        models = [args.model] * len(widths)
    if len(models) != len(widths):
        raise SystemExit(f"{len(models)} models for {len(widths)} slices")
    offsets, cursor = [], 0
    for w in widths:
        offsets.append(cursor)
        cursor += w

    rundir = args.out.parent / (args.out.stem + ".d")
    rundir.mkdir(parents=True, exist_ok=True)
    for stale in rundir.glob("*.json"):
        stale.unlink()

    children = []
    for index, (units, offset) in enumerate(zip(widths, offsets)):
        env = dict(os.environ)
        env["ROC_GLOBAL_CU_MASK"] = mask_for(units, offset,
                                             args.maskable_units)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        argv = [sys.executable, str(Path(__file__).resolve()),
                "--role", "worker", "--slice-index", str(index),
                "--units", str(units), "--offset", str(offset),
                "--rundir", str(rundir), "--model", models[index],
                "--steps", str(args.steps),
                "--warmup-steps", str(args.warmup_steps),
                "--window-s", str(args.window_s),
                "--seed", str(args.seed),
                "--height", str(args.height), "--width", str(args.width),
                "--video-height", str(args.video_height),
                "--video-width", str(args.video_width),
                "--frames", str(args.frames),
                "--startup-timeout", str(args.startup_timeout),
                "--out", str(args.out)]
        if args.diagnose:
            argv.append("--diagnose")
        log = open(rundir / f"worker_{index}.log", "w")
        children.append((index, subprocess.Popen(argv, env=env, stdout=log,
                                                 stderr=subprocess.STDOUT),
                         log))
        print(f"  slice {index}: {models[index]} on {units}u at offset "
              f"{offset}, mask {env['ROC_GLOBAL_CU_MASK']}, pid "
              f"{children[-1][1].pid}",
              flush=True)

    print("waiting for every worker to load and warm ...", flush=True)
    deadline = time.time() + args.startup_timeout
    while True:
        ready = sorted(rundir.glob("ready_*.json"))
        if len(ready) == len(widths):
            break
        for index, child, _ in children:
            if child.poll() not in (None, 0):
                raise SystemExit(f"worker {index} died before ready; see "
                                 f"{rundir / f'worker_{index}.log'}")
        if time.time() > deadline:
            raise SystemExit(f"only {len(ready)} of {len(widths)} workers "
                             f"became ready")
        time.sleep(0.5)

    # A timetable of absolute times. Solo slots are staggered so that no
    # two are ever on the die together, and the check afterwards is
    # against the intervals the workers actually recorded.
    now = time.time()
    slot = args.slot_s
    base = now + args.lead_s
    solo_before = [base + i * slot for i in range(len(widths))]
    corun = solo_before[-1] + slot + args.lead_s
    after_base = corun + args.window_s + args.lead_s
    solo_after = [after_base + i * slot for i in range(len(widths))]
    emptied_base = solo_after[-1] + slot + args.lead_s
    solo_emptied = [emptied_base + i * slot for i in range(len(widths))]
    (rundir / "timetable.json").write_text(json.dumps({
        "solo_before": solo_before, "corun": corun,
        "solo_after": solo_after, "solo_emptied": solo_emptied,
    }))
    print(f"timetable written; co-run at +{corun - now:.0f} s, "
          f"window {args.window_s:.0f} s", flush=True)

    for index, child, log in children:
        child.wait()
        log.close()
        if child.returncode != 0:
            raise SystemExit(f"worker {index} exited {child.returncode}; "
                             f"see {rundir / f'worker_{index}.log'}")

    results = [json.loads((rundir / f"result_{i}.json").read_text())
               for i in range(len(widths))]
    return report(results, widths, offsets, models, args)


def report(results, widths, offsets, models, args) -> int:

    # --- verify, before reporting a single number ----------------------
    problems = []
    for i, left in enumerate(results):
        for phase in ("solo_before", "solo_after", "solo_emptied"):
            mine = spans(left.get(phase))
            if mine is None:
                continue
            for j, right in enumerate(results):
                if i == j:
                    continue
                for other in ("solo_before", "corun", "solo_after",
                              "solo_emptied"):
                    if overlaps(mine, spans(right.get(other))):
                        problems.append(
                            f"slice {i}'s {phase} overlapped slice {j}'s "
                            f"{other}")
    corun_spans = [spans(r["corun"]) for r in results]
    lo = max(s[0] for s in corun_spans)
    hi = min(s[1] for s in corun_spans)
    if hi <= lo:
        problems.append("the co-run windows do not all intersect")

    quota_check = []
    for index, (units, result) in enumerate(zip(widths, results)):
        curve = curve_for(models[index], args.device)
        measured = wall_per_step(result["solo_before"])
        predicted = curve.step_seconds(units)
        error = measured / predicted - 1 if predicted else None
        quota_check.append({"slice": index, "units": units,
                            "model": models[index],
                            "measured_s": measured,
                            "predicted_s": predicted, "error": error})
        if error is None or abs(error) > args.quota_tolerance:
            problems.append(
                f"slice {index} at {units}u ran at {measured * 1000:.1f} ms "
                f"against a measured-curve {predicted * 1000:.1f} ms "
                f"({error:+.1%}); the process mask may not have applied")

    rows = []
    for index, (units, result) in enumerate(zip(widths, results)):
        before = wall_per_step(result["solo_before"])
        after = wall_per_step(result["solo_after"])
        emptied = (wall_per_step(result["solo_emptied"])
                   if result.get("solo_emptied") else None)
        # The co-run rate over the intersection, from the wall clock:
        # count the steps whose interval lies inside it and divide by the
        # span they actually occupy.
        inside = [s for s in result["corun"]["samples"]
                  if s["began"] >= lo and s["ended"] <= hi]
        co = ((inside[-1]["ended"] - inside[0]["began"]) / len(inside)
              if len(inside) > 1 else None)
        co_events = p50([s["s"] for s in inside])
        rows.append({
            "slice": index, "units": units, "offset": offsets[index],
            "model": models[index],
            "mask": result["ready"]["mask"],
            "solo_before_s": before, "corun_p50_s": co,
            "corun_event_p50_s": co_events,
            "solo_before_event_p50_s": event_p50(result["solo_before"]),
            "n_in_overlap": len(inside),
            "n_total": len(result["corun"]["samples"]),
            "solo_after_s": after, "solo_emptied_s": emptied,
            "externality": (co / before if co and before else None),
            "solo_after_over_before": (after / before
                                       if after and before else None),
        })

    payload = {
        "schema_version": SCHEMA_VERSION,
        "question": ("the co-run penalty with one tenant per process, "
                     "which removes the shared caching allocator that "
                     "3.10 found charged as contention"),
        "arrangement": ("one process per slice, ROC_GLOBAL_CU_MASK per "
                        "process, default stream, staggered solo slots "
                        "verified disjoint after the fact"),
        "model": args.model, "models": models,
        "device_requested": args.device,
        "slice_widths": widths, "slice_offsets": offsets,
        "maskable_units": args.maskable_units,
        "steps": args.steps, "window_s": args.window_s,
        "overlap_seconds": hi - lo if hi > lo else 0.0,
        "quota_curve_check": quota_check,
        "problems": problems,
        "valid": not problems,
        "rows": rows,
        "device": {k: results[0]["ready"][k]
                   for k in ("device", "arch", "multi_processor_count")},
        "torch": results[0]["torch"],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=1, sort_keys=True))

    print()
    print(f"{'slice':>5} {'model':>13} {'units':>6} {'solo ms':>9} "
          f"{'co-run ms':>10} {'ext':>7} {'after ms':>9} "
          f"{'emptied ms':>11} {'n':>5}")
    for row in rows:
        print(f"{row['slice']:>5} {row['model']:>13} {row['units']:>6} "
              f"{row['solo_before_s'] * 1000:>9.1f} "
              f"{(row['corun_p50_s'] or 0) * 1000:>10.1f} "
              f"{(row['externality'] or 0):>7.3f} "
              f"{(row['solo_after_s'] or 0) * 1000:>9.1f} "
              f"{((row['solo_emptied_s'] or 0) * 1000):>11.1f} "
              f"{row['n_in_overlap']:>5}")
    print(f"\noverlap {payload['overlap_seconds']:.1f} s of a "
          f"{args.window_s:.0f} s window")
    if problems:
        print("\nINVALID -- reported rather than averaged:")
        for problem in problems:
            print(f"  * {problem}")
    else:
        print("\nvalid: solo slots disjoint, co-run windows intersect, "
              "every slice on its width's measured quota curve")
    print(f"-> {args.out}")
    return 0 if not problems else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", default="coordinator",
                        choices=("coordinator", "worker"))
    parser.add_argument("--model", default="sdxl")
    parser.add_argument("--models", default=None,
                        help="comma-separated model per slice, e.g. "
                             "`sdxl,cogvideox-2b`. This is the "
                             "arrangement claim 1.5 is about, and 1.5 "
                             "was measured with both models in ONE "
                             "process; here they are two, with a "
                             "separate copy of the weights each, which "
                             "is also how they would really be served.")
    parser.add_argument("--device", default="gfx1201",
                        help="which measured quota curve to check the "
                             "solo step times against")
    parser.add_argument("--ways", type=int, default=2)
    parser.add_argument("--widths", default=None,
                        help="comma-separated slice widths, e.g. `8,24`")
    parser.add_argument("--maskable-units", type=int, default=32)
    parser.add_argument("--steps", type=int, default=14)
    parser.add_argument("--warmup-steps", type=int, default=8)
    parser.add_argument("--window-s", type=float, default=60.0)
    parser.add_argument("--slot-s", type=float, default=40.0,
                        help="how long each staggered solo slot lasts")
    parser.add_argument("--lead-s", type=float, default=10.0)
    parser.add_argument("--startup-timeout", type=float, default=900.0)
    parser.add_argument("--quota-tolerance", type=float, default=0.15,
                        help="how far a slice's solo may sit from its "
                             "width's measured curve before the run is "
                             "called invalid. The mask cannot be read "
                             "back, so this is the readback.")
    parser.add_argument("--diagnose", action="store_true",
                        help="a fourth solo after empty_cache, which is "
                             "the control that killed 1.11")
    parser.add_argument("--height", type=int, default=768)
    parser.add_argument("--width", type=int, default=768)
    parser.add_argument("--video-height", type=int, default=480)
    parser.add_argument("--video-width", type=int, default=720)
    parser.add_argument("--frames", type=int, default=9)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--slice-index", type=int, default=0)
    parser.add_argument("--units", type=int, default=16)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--rundir", default=None)
    parser.add_argument("--summarise-only", type=Path, default=None,
                        help="re-report an existing run directory instead "
                             "of measuring. The reporting is separate "
                             "from the measurement so that a crash in it "
                             "cannot cost a run that already happened.")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.summarise_only:
        return summarise(args.summarise_only, args)
    return worker(args) if args.role == "worker" else coordinator(args)


if __name__ == "__main__":
    raise SystemExit(main())
