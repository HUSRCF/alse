#!/usr/bin/env python3
"""A minute-long, GPU-free check that the whole scheduling path works.

Builds a trace, runs every policy through the simulator, and asserts the
invariants that a broken wiring would break. It is not evidence about
the hardware and prints nothing that should be quoted as a result: the
cost model it runs on is measured, but the whole point of this project's
log is that a number from a model is not a number from a card.

What it is for is an artifact check on a clean machine, where the
question is whether the code runs at all.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from burstserve.policies import BASELINES, POLICY_FACTORIES  # noqa: E402
from burstserve.trace_sim import PairingStates, simulate  # noqa: E402
from burstserve.workload import (  # noqa: E402
    CellSpec, build_trace, horizon_for_urgent_count,
)

# Nominal, not measured here: the cell runner measures these on the card
# before every group. A smoke test that needed a GPU to size its own
# trace would not run on a clean machine, which is the only thing it is
# for.
URGENT_SERVICE_S = 0.90
VIDEO_SERVICE_S = 15.47


def build(load: float, burst: int, seed: int, wanted: int, backlog: bool):
    sizing = CellSpec(load=load, burst=burst, deadline_slack=1.5, seed=seed,
                      horizon_s=1.0, urgent_steps=8, video_steps=30,
                      deadline_base="burst")
    horizon = horizon_for_urgent_count(sizing,
                                       urgent_service_s=URGENT_SERVICE_S,
                                       wanted=wanted)
    spec = CellSpec(load=load, burst=burst, deadline_slack=1.5, seed=seed,
                    horizon_s=horizon, urgent_steps=8, video_steps=30,
                    deadline_base="burst")
    trace = build_trace(spec, urgent_service_s=URGENT_SERVICE_S,
                        video_service_s=VIDEO_SERVICE_S,
                        urgent_isolated_latency_p99_s=URGENT_SERVICE_S,
                        video_backlog=backlog)
    return spec, trace


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--urgent-count", type=int, default=40)
    ap.add_argument("--seeds", type=int, default=3)
    args = ap.parse_args()

    policies = sorted(set(BASELINES) | set(POLICY_FACTORIES))
    failures: list[str] = []
    digests: dict[tuple, bytes] = {}
    # Under arrivals the video tenant is a Poisson process with a mean of
    # about two requests per cell, so a seed with none is a property of
    # the trace and not a fault -- 27 of the 405 grid cells had exactly
    # that. It is only a fault if *no* seed produces one, because then
    # nothing here ever exercised co-tenancy.
    arrivals_with_video = 0

    for backlog in (False, True):
        for seed in range(args.seeds):
            spec, trace = build(0.6, 4, seed, args.urgent_count, backlog)
            urgent = [r for r in trace.requests if r.tenant == "urgent"]
            video = [r for r in trace.requests if r.tenant == "video"]
            if not urgent:
                failures.append(f"trace has no urgent requests "
                                f"(backlog={backlog}, seed={seed})")
                continue
            if backlog and not video:
                failures.append("backlog produced no video requests")
                continue
            if backlog and not all(r.arrival_s == 0.0 for r in video):
                failures.append("backlog video requests do not all arrive at 0")
            if not backlog and video:
                arrivals_with_video += 1
            for name in policies:
                policy = (POLICY_FACTORIES[name]() if name in POLICY_FACTORIES
                          else BASELINES[name])
                result = simulate(trace, policy, horizon_s=spec.horizon_s,
                                  seed=seed,
                                  pairing_states=PairingStates(seed=seed))
                digests[(name, backlog, seed)] = result.canonical_bytes()
                if result.steps_executed <= 0:
                    failures.append(f"{name}: no steps executed")
                if result.unmeasured_pairings:
                    failures.append(f"{name}: {len(result.unmeasured_pairings)} "
                                    "pairings outside the measured table")
                # Determinism: the same seed twice has to be the same bytes.
                again = simulate(trace, (POLICY_FACTORIES[name]()
                                         if name in POLICY_FACTORIES
                                         else BASELINES[name]),
                                 horizon_s=spec.horizon_s, seed=seed,
                                 pairing_states=PairingStates(seed=seed))
                if again.canonical_bytes() != result.canonical_bytes():
                    failures.append(f"{name}: not deterministic at seed {seed}")

    # The identity that makes the fixed-split sweep readable: a 16-unit
    # split is static_even, so a disagreement is a wiring fault and not a
    # result.
    for key, value in digests.items():
        if key[0] != "fixed_split_16":
            continue
        peer = digests.get(("static_even",) + key[1:])
        if peer is not None and peer != value:
            failures.append(f"fixed_split_16 != static_even at {key[1:]}")

    if not arrivals_with_video:
        failures.append(f"no arrival-driven seed under {args.seeds} produced a "
                        "video request, so co-tenancy was never exercised "
                        "outside the backlog regime; raise --seeds or "
                        "--urgent-count")

    print(f"{len(policies)} policies x {args.seeds} seeds x 2 regimes = "
          f"{len(digests)} simulations "
          f"({arrivals_with_video}/{args.seeds} arrival seeds had a video "
          f"tenant)")
    for name in policies:
        print(f"  {name}")
    if failures:
        print(f"\nFAILED ({len(failures)}):")
        for line in failures:
            print(f"  {line}")
        return 1
    print("\nsmoke OK -- simulations ran, were deterministic, stayed inside "
          "the measured pairing table, and fixed_split_16 == static_even")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
