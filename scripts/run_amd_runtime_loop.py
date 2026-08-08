#!/usr/bin/env python3
"""The whole loop on the card: frozen policy, real SDXL, measured charges.

Everything before this ran either in a simulator or as a single request.
This is the first time the pieces meet: tenant queues, the frozen Gate C
policy, step executors driving real SDXL, and a ledger charged from
device time rather than from the cost model.

What it is for is the week 7-8 acceptance that cannot be checked any
other way:

  * scheduler p99 under 1 ms -- measured around the policy call alone, on
    a wall clock, with the model actually running. The unit tests use an
    injected clock and prove the timing brackets the right code; they
    cannot say what the policy costs in practice.
  * a same-model burst moves no weight bytes after residency.
  * the accounting is charged from measurement. A run that fell back to
    the cost model anywhere is a simulation of a runtime, and the script
    fails rather than reporting its numbers.

Two tenants share one pipeline. That is the arrangement the in-process
co-run established as the runtime's own -- one copy of the weights, two
masked streams -- and holding a second copy here would measure a
deployment nobody intends to run.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

from burstserve.executor import StepExecutor            # noqa: E402
from burstserve.policies import probing_partitioning    # noqa: E402
from burstserve.queues import Discipline, QueuedRequest  # noqa: E402
from burstserve.runtime import Runtime                  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenants", type=int, default=2)
    parser.add_argument("--requests-per-tenant", type=int, default=2)
    parser.add_argument("--steps", type=int, default=6)
    parser.add_argument("--height", type=int, default=768)
    parser.add_argument("--width", type=int, default=768)
    parser.add_argument("--seed", type=int, default=0)
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
    print(f"  weights {torch.cuda.max_memory_allocated() / 2**30:.1f} GB",
          flush=True)

    runtime = Runtime(probing_partitioning, discipline=Discipline.FCFS)
    rid = 0
    for tenant_index in range(args.tenants):
        for _ in range(args.requests_per_tenant):
            request = QueuedRequest(
                request_id=rid, tenant=f"t{tenant_index}", model="sdxl",
                arrival_s=0.0, steps=args.steps,
            )
            # One adapter per request: the conditioning is shared through
            # the pipeline, the progress is not.
            adapter = SdxlStepAdapter(
                pipeline, height=args.height, width=args.width,
                steps=args.steps, seed=args.seed + rid,
            )
            runtime.submit(request, StepExecutor(request, adapter,
                                                 total_steps=args.steps))
            rid += 1
    print(f"  {rid} requests over {args.tenants} tenants", flush=True)

    now = 0.0
    rounds = 0
    while not runtime.all_finished():
        record = runtime.tick(now)
        rounds += 1
        now += 0.25
        if rounds % 5 == 0 or not record.granted:
            print(f"  round {rounds}: granted={record.granted} "
                  f"decision={record.decision_seconds * 1e6:.0f} us",
                  flush=True)
        if rounds > 500:
            print("round limit reached", flush=True)
            break

    decisions = [r.decision_seconds for r in runtime.ledger]
    p99 = runtime.decision_p99_seconds()
    errors = [e for r in runtime.ledger for e in r.prediction_error.values()]
    payload = {
        "schema_version": "burstserve.amd-runtime-loop/v1",
        "policy": "policies.probing_partitioning",
        "arrangement": "one process, one pipeline, per-request executors",
        "tenants": args.tenants,
        "requests": rid,
        "steps_each": args.steps,
        "workpoint": f"{args.width}x{args.height}",
        "rounds": rounds,
        "scheduler_decision_seconds": {
            "p50": statistics.median(decisions) if decisions else None,
            "p99": p99,
            "max": max(decisions) if decisions else None,
            "n": len(decisions),
        },
        "scheduler_p99_under_1ms": p99 < 1e-3,
        "charged_from_measurement": runtime.charged_from_measurement(),
        "weight_bytes_after_first_round": (
            runtime.weight_bytes_after_first_round()
        ),
        "resident_models": list(runtime.ledger[-1].resident_models)
        if runtime.ledger else [],
        "quota_seconds_by_tenant": runtime.quota_seconds_by_tenant,
        "service_seconds_by_tenant": runtime.service_seconds_by_tenant,
        "prediction_error": {
            "n": len(errors),
            "mean": statistics.mean(errors) if errors else None,
            "max": max(errors) if errors else None,
        },
        "all_finished": runtime.all_finished(),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2) + "\n")

    print(f"\nrounds {rounds}, all finished {runtime.all_finished()}")
    print(f"scheduler p50 {statistics.median(decisions) * 1e6:.0f} us, "
          f"p99 {p99 * 1e6:.0f} us, under 1 ms: {p99 < 1e-3}")
    print(f"charged from measurement: {runtime.charged_from_measurement()}")
    print(f"weight bytes after round 0: "
          f"{runtime.weight_bytes_after_first_round()}")
    print(f"quota-seconds: {runtime.quota_seconds_by_tenant}")
    print(f"-> {args.out}")
    return 0 if (p99 < 1e-3 and runtime.charged_from_measurement()
                 and runtime.all_finished()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
