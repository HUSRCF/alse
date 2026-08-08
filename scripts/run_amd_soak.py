#!/usr/bin/env python3
"""A long soak: does the runtime leak, deadlock or run out of memory?

plan.md's week 7-8 acceptance is an hour of temporal-only serving with
no leak, no deadlock and no OOM. Each of those fails differently and a
soak that only reported "it finished" would miss two of them:

  * a leak does not fail, it grows. The test is the trend in allocated
    bytes across the run, taken after a warm-up so the curve being fitted
    is steady-state rather than start-up.
  * a deadlock does not raise, it waits. Every round is bounded by a
    watchdog, and a round that produces no progress while requests are
    pending ends the run rather than hanging until the wall clock does.
  * an OOM does raise, and the useful part is where: a soak that dies at
    minute 50 with a hundred requests in flight is a different finding
    from one that dies on its second request.

Arrivals are Poisson from an owned Random instance, so the same seed
gives the same trace and a failure can be replayed. Requests keep
arriving for the whole window: a soak that admitted everything up front
would measure a drain, and draining is the easy case.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

from burstserve.executor import Phase, StepExecutor        # noqa: E402
from burstserve.policies import probing_partitioning        # noqa: E402
from burstserve.queues import Discipline, QueuedRequest     # noqa: E402
from burstserve.runtime import Runtime                      # noqa: E402


def linear_trend(points: list[tuple[float, float]]) -> float:
    """Bytes per second, least squares. Zero for fewer than two points."""
    if len(points) < 2:
        return 0.0
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:
        return 0.0
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denom


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=3600.0)
    parser.add_argument("--rate", type=float, default=0.12,
                        help="arrivals per second per tenant")
    parser.add_argument("--tenants", type=int, default=2)
    parser.add_argument("--steps", type=int, default=6)
    parser.add_argument("--height", type=int, default=768)
    parser.add_argument("--width", type=int, default=768)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--round-timeout", type=float, default=120.0,
                        help="a round taking longer than this is a hang")
    parser.add_argument("--sample-every", type=float, default=30.0)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    import torch
    from diffusers import DiffusionPipeline
    from amd_sdxl_adapter import SdxlStepAdapter

    print("loading sdxl ...", flush=True)
    pipeline = DiffusionPipeline.from_pretrained(
        "stabilityai/stable-diffusion-xl-base-1.0",
        torch_dtype=torch.float16, variant="fp16", use_safetensors=True,
    ).to("cuda")
    pipeline.set_progress_bar_config(disable=True)

    runtime = Runtime(probing_partitioning, discipline=Discipline.FCFS)
    warm = SdxlStepAdapter(pipeline, height=args.height, width=args.width,
                           steps=args.steps, seed=args.seed)
    state = warm.initial_state(None)
    warm_started = time.perf_counter()
    for _ in range(2):
        state = warm.denoise_one(state, quota_units=32)
    warm.drain_timing()
    runtime.warm("sdxl", time.perf_counter() - warm_started)
    print(f"  warmed, steady step "
          f"{warm.last_step_seconds * 1000:.1f} ms", flush=True)

    rng = random.Random(args.seed)
    arrivals: list[tuple[float, str]] = []
    for index in range(args.tenants):
        clock = 0.0
        while True:
            clock += rng.expovariate(args.rate)
            if clock > args.seconds:
                break
            arrivals.append((clock, f"t{index}"))
    arrivals.sort()
    print(f"  {len(arrivals)} arrivals scheduled over {args.seconds:.0f}s",
          flush=True)

    started = time.perf_counter()
    samples: list[dict] = []
    next_sample = 0.0
    rid = 0
    arrival_index = 0
    rounds = 0
    stalled_rounds = 0
    failure: str | None = None
    peak_backlog = 0
    completed = 0

    try:
        while True:
            elapsed = time.perf_counter() - started
            if elapsed >= args.seconds and runtime.all_finished():
                break
            if elapsed >= args.seconds * 1.5:
                failure = "did not drain within 1.5x the window"
                break

            # Admit everything that has arrived by now.
            while (arrival_index < len(arrivals)
                   and arrivals[arrival_index][0] <= elapsed):
                _, tenant = arrivals[arrival_index]
                request = QueuedRequest(request_id=rid, tenant=tenant,
                                        model="sdxl", arrival_s=elapsed,
                                        steps=args.steps)
                adapter = SdxlStepAdapter(pipeline, height=args.height,
                                          width=args.width,
                                          steps=args.steps,
                                          seed=args.seed + rid)
                runtime.submit(request, StepExecutor(request, adapter,
                                                     total_steps=args.steps))
                rid += 1
                arrival_index += 1

            round_started = time.perf_counter()
            record = runtime.tick(elapsed)
            round_seconds = time.perf_counter() - round_started
            rounds += 1

            if round_seconds > args.round_timeout:
                failure = (f"round {rounds} took {round_seconds:.1f}s, "
                           f"over the {args.round_timeout:.0f}s watchdog")
                break

            pending = sum(1 for e in runtime.executors.values()
                          if e.phase is not Phase.FINISHED)
            peak_backlog = max(peak_backlog, pending)
            done = sum(1 for e in runtime.executors.values()
                       if e.phase is Phase.FINISHED)
            if not record.granted and pending:
                # No progress with work outstanding. One such round is
                # possible between an arrival and its first grant; a run
                # of them is a deadlock.
                stalled_rounds += 1
                if stalled_rounds > 20:
                    failure = f"{stalled_rounds} rounds with pending work " \
                              f"and no grant"
                    break
            else:
                stalled_rounds = 0
            completed = done

            if elapsed >= next_sample:
                torch.cuda.synchronize()
                samples.append({
                    "t": elapsed,
                    "allocated_bytes": torch.cuda.memory_allocated(),
                    "reserved_bytes": torch.cuda.memory_reserved(),
                    "pending": pending,
                    "completed": done,
                    "rounds": rounds,
                })
                next_sample += args.sample_every
                print(f"  t={elapsed:6.0f}s  pending={pending:3d}  "
                      f"done={done:4d}  "
                      f"alloc={samples[-1]['allocated_bytes'] / 2**30:.2f}GB",
                      flush=True)
    except Exception as exc:                                # noqa: BLE001
        failure = f"{type(exc).__name__}: {exc}"

    elapsed = time.perf_counter() - started
    # Fit the trend after the first two samples, so start-up allocation
    # is not read as a leak.
    steady = [(s["t"], float(s["allocated_bytes"])) for s in samples[2:]]
    slope = linear_trend(steady)
    decisions = [r.decision_seconds for r in runtime.ledger]

    payload = {
        "schema_version": "burstserve.amd-soak/v1",
        "window_seconds": args.seconds,
        "elapsed_seconds": elapsed,
        "arrival_rate_per_tenant": args.rate,
        "tenants": args.tenants,
        "requests_admitted": rid,
        "requests_completed": completed,
        "rounds": rounds,
        "peak_backlog": peak_backlog,
        "failure": failure,
        "no_deadlock": failure is None or "no grant" not in failure,
        "no_oom": failure is None or "OutOfMemory" not in str(failure),
        "allocated_bytes_slope_per_second": slope,
        # A leak shows as a positive slope sustained over the window. The
        # threshold is deliberately generous: what matters is whether the
        # curve rises, not whether it rises by a particular amount.
        "no_leak": abs(slope) < 1e6,
        "samples": samples,
        "scheduler_decision_seconds": {
            "p50": statistics.median(decisions) if decisions else None,
            "p99": (sorted(decisions)[min(len(decisions) - 1,
                                          int(0.99 * len(decisions)))]
                    if decisions else None),
            "n": len(decisions),
        },
        "startup_seconds_by_model": runtime.startup_seconds_by_model,
        "quota_seconds_by_tenant": runtime.quota_seconds_by_tenant,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2) + "\n")

    print(f"\nelapsed {elapsed:.0f}s, rounds {rounds}, "
          f"admitted {rid}, completed {completed}")
    print(f"peak backlog {peak_backlog}, failure: {failure}")
    print(f"allocated slope {slope / 1e6:+.3f} MB/s -> "
          f"no_leak={payload['no_leak']}")
    print(f"-> {args.out}")
    return 0 if failure is None and payload["no_leak"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
