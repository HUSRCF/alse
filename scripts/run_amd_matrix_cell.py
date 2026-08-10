#!/usr/bin/env python3
"""One cell of the main matrix, on the card, end to end.

plan.md's week 13-14 runs the matrix; this runs one point of it. Two
tenants -- urgent SDXL against a CogVideoX-2b video stream -- arrive on a
Poisson-burst trace at a given offered load, and the frozen scheduler
serves them through the real executors while the ledger records what it
believed and what it cost.

**Isolated service is measured here, not looked up.** The trace's offered
load is defined against a request running alone on the whole die, and the
urgent deadline is a multiple of the isolated p99. Both come from a
warm-up pass in this process, so a cell's load axis means the same thing
on a cold card and a hot one -- the die warms 30 C over a few minutes and
solo step time rises 5% with it.

**Arrival time is wall time.** The trace is built from measured service
times, so its clock and the card's are the same clock; a request is
admitted when it arrives, not up front. Admitting the whole trace at
once would turn every cell into a backlog test and delete the load axis.

The first cell run is also the answer to a scheduling question: the
matrix was planned for four cards in parallel and now has one, so what a
cell costs in wall time decides how many cells the two-week window holds.
That is why the payload records its own durations.
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

from burstserve.executor import StepExecutor                    # noqa: E402
from burstserve.masked_streams import MaskedStreamPool          # noqa: E402
from burstserve.policies import BASELINES, POLICY_FACTORIES     # noqa: E402
from burstserve.queues import QueuedRequest                     # noqa: E402
from burstserve.runtime import Runtime                          # noqa: E402
from burstserve.workload import (                               # noqa: E402
    CellSpec,
    build_trace,
    horizon_for_urgent_count,
)
import run_amd_mismatched_corun as harness                      # noqa: E402


def isolated_service(adapter, pool, steps, args, torch):
    """Whole-request service on the whole die, and the step p99.

    Runs the request twice: the first pays whatever this process has not
    paid yet, and only the second is measured. A cell whose load axis was
    calibrated against a kernel compilation would be a different cell from
    every one after it.
    """
    from burstserve.executor import StepExecutor as Executor

    per_step = []
    for attempt in range(2):
        adapter.stream = pool.for_quota(32).handle
        executor = Executor(object(), adapter, total_steps=steps)
        executor.prepare()
        wrapper = torch.cuda.ExternalStream(adapter.stream.value)
        torch.cuda.synchronize()
        origin = torch.cuda.Event(enable_timing=True)
        origin.record()
        marks = []
        while True:
            before = torch.cuda.Event(enable_timing=True)
            after = torch.cuda.Event(enable_timing=True)
            before.record(wrapper)
            more = executor.run_step(quota_units=32)
            after.record(wrapper)
            marks.append((before, after))
            if not more:
                break
        torch.cuda.synchronize()
        adapter.drain_timing()
        if attempt == 1:
            per_step = [origin.elapsed_time(b2) - origin.elapsed_time(b1)
                        for b1, b2 in marks]
    per_step.sort()
    p99 = per_step[min(len(per_step) - 1, int(0.99 * len(per_step)))] / 1000.0
    return sum(per_step) / 1000.0, p99


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", default="probing_partitioning",
                        help="one policy, or use --policies for a group")
    parser.add_argument("--video-model", default="cogvideox-2b",
                        help="the long-running tenant's model. sdxl makes "
                             "the co-location same-model, which is the only "
                             "arrangement the die was measured bistable in: "
                             "self-paired 16+16 draws 1.274 or 1.871 per "
                             "process, while sdxl against cogvideox measured "
                             "1.02 and 1.05 with no draw in four processes. "
                             "The probe fires on the slow state, so a "
                             "mismatched workload cannot exercise it.")
    parser.add_argument("--drift-tolerance", type=float, default=0.15,
                        help="the runtime's serial-fallback threshold. The "
                             "campaign found it firing for static_even, "
                             "deadline_aware and measured_pairs_only and "
                             "never for the pairing family, so it has to "
                             "be switchable: an envelope that fires on "
                             "three baselines and no method arm could be "
                             "manufacturing the comparison.")
    parser.add_argument("--per-policy-isolated", action="store_true",
                        help="re-measure isolated service for every policy "
                             "instead of once per group. Off by default: "
                             "the deadline comes from that measurement, so "
                             "per-policy measurement hands the first arm a "
                             "colder card and a 2.4% tighter deadline.")
    parser.add_argument("--policies", default="",
                        help="comma-separated policies run back to back in "
                             "one process. This is how a group is run, not "
                             "a convenience: the die warms 30 C over a few "
                             "minutes and solo step time rises 5% with it, "
                             "so running one arm to completion and then the "
                             "next puts the whole drift on one of them. "
                             "Interleaving inside a group pairs them in "
                             "time, which is the rule that replaced "
                             "'compare on the same card' when the matrix "
                             "moved to one card.")
    parser.add_argument("--load", type=float, default=0.6)
    parser.add_argument("--burst", type=int, default=4)
    parser.add_argument("--deadline-slack", type=float, default=1.5)
    parser.add_argument("--deadline-base", default="burst",
                        choices=("burst", "request"),
                        help="what the slack multiplies; measured on "
                             "2026-08-09, 'request' saturates the miss "
                             "rate at burst 4 and no policy can move it")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--urgent-steps", type=int, default=8)
    parser.add_argument("--video-steps", type=int, default=30)
    parser.add_argument("--urgent-count", type=int, default=40,
                        help="expected urgent requests; sizes the horizon")
    parser.add_argument("--height", type=int, default=768)
    parser.add_argument("--width", type=int, default=768)
    parser.add_argument("--video-height", type=int, default=480)
    parser.add_argument("--video-width", type=int, default=720)
    parser.add_argument("--frames", type=int, default=9)
    parser.add_argument("--steps", type=int, default=8)   # adapter default
    parser.add_argument("--warmup", type=int, default=0)
    parser.add_argument("--drain-grace-s", type=float, default=120.0)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.keep_text_encoders = False

    import torch

    began_all = time.perf_counter()
    models = {"urgent": "sdxl", "video": args.video_model}
    pool = MaskedStreamPool(harness.make_stream)

    pipelines, warm_adapters = {}, {}
    for tenant, model in models.items():
        if model not in pipelines:
            print(f"loading {model} ...", flush=True)
            pipelines[model] = harness.build_pipeline(
                model, drop_text_encoders=False)
        # One adapter per tenant is enough here: each tenant runs at most
        # one request at a time, which is what TenantRegistry offers.
        args.steps = (args.urgent_steps if tenant == "urgent"
                      else args.video_steps)
        warm_adapters[tenant] = harness.make_adapter(model, pipelines[model],
                                                     args, seed=1000)
    for model in pipelines:
        harness.free_text_encoders(pipelines[model])
    loaded_s = time.perf_counter() - began_all

    # Which co-run state this process drew, measured before any policy
    # runs and shared by the whole group. One step identifies it -- the
    # per-step series inside an episode is constant to 0.00% over 72
    # episodes -- so it costs seconds, and it turns "the probe never
    # fired" into "the probe never fired, and here is the state it would
    # have fired on".
    # Warm both widths the cells will actually use, including the full
    # die, before measuring the state. Two reasons, and the second is the
    # one that made the old probe useless. Kernel compilation is a
    # property of the process, and a state measured through it is
    # measuring the compile. And the policies grant 32 units to a single
    # runnable request -- exclusive_fcfs always does -- so a state probe
    # that has never masked the full die is not sampling the process the
    # cells will run in. The old probe reported fast in 30 of 30
    # processes where the span harness, which warms 32, drew slow in 7 of
    # 30; at the pooled 22.7% rate, 0 of 30 has probability 0.0005.
    for tenant in models:
        for width in (16, 32):
            harness.warm(warm_adapters[tenant], pool, width, args)
    drawn = drawn_state(args, torch, models, pool, warm_adapters)
    print(f"  drawn co-run state: {drawn['externality']:.3f} "
          f"({drawn['state']})", flush=True)

    policies = ([p.strip() for p in args.policies.split(",") if p.strip()]
                or [args.policy])
    out_template = str(args.out)

    # Isolated service once per group, shared by every arm in it. The
    # deadline is derived from it, so measuring per arm gives the arm that
    # runs first a colder card and a shorter deadline: measured, the first
    # position read 882 ms against 900-905 for the rest, a 2.4% tighter
    # window for whichever policy happens to go first. Arms being compared
    # must face the same deadline or they are not the same experiment.
    shared = None
    if not args.per_policy_isolated and len(policies) > 1:
        shared = measure_isolated(args, torch, models, pool, warm_adapters,
                                  label="group")
    for policy_name in policies:
        run_one(policy_name, args, torch, models, pool, pipelines,
                warm_adapters, loaded_s, began_all, out_template,
                len(policies) > 1, shared, drawn)
    return 0


def _span_series(adapter, torch, stream_handle, steps):
    """Run steps on a stream, returning each step's bracketing events."""
    from burstserve.executor import StepExecutor as Executor

    # Never ask for more steps than the adapter's own schedule holds:
    # the urgent adapter carries 8 timesteps and stepping past them
    # indexes off the end of the tensor.
    steps = min(steps, getattr(adapter, "steps", steps))
    adapter.stream = stream_handle
    wrapper = torch.cuda.ExternalStream(stream_handle.value)
    executor = Executor(object(), adapter, total_steps=steps)
    executor.prepare()
    marks = []
    for _ in range(steps):
        before = torch.cuda.Event(enable_timing=True)
        after = torch.cuda.Event(enable_timing=True)
        before.record(wrapper)
        more = executor.run_step(quota_units=16)
        after.record(wrapper)
        marks.append((before, after))
        if not more:
            break
    return marks


def drawn_state(args, torch, models, pool, warm_adapters):
    """The co-run externality this process drew, by device span.

    Measured on the pairing the runtime will actually form -- both
    tenants at 16+16 -- rather than on a canonical self-pairing, because
    the die keys its state on the mask pair and the two arrangements
    differ: self-paired is bistable at 1.274 or 1.871, mismatched measured
    1.02 and 1.05 with no draw at all.

    Uses the device-span instrument, not the adapter's deferred reading,
    which was found on 2026-08-08 to hold one stale value for a whole
    episode whenever the measured side's steps are short.
    """
    left, right = pool.disjoint_pair(16, 16)
    sides = (("a", "urgent", left), ("b", "video", right))

    # Both sides run the same number of steps, so they overlap for the
    # whole episode. With 8 against 14 the urgent side finishes first and
    # the video side's remaining steps run alone, which pulls its ratio
    # toward 1.0 and halves the gap between the states: measured, the two
    # clusters came out at 1.136 and 1.47 -- exactly the means of
    # (1.30, 1.0) and (1.95, 1.0) -- where the reference distribution is
    # 1.297 and 1.949. The states were still there; the probe was
    # averaging each of them with an unmeasured solo run.
    episode_steps = min(14, *(getattr(warm_adapters[t], "steps", 14)
                              for _, t, _ in sides))

    solo = {}
    # Longer than the first version's 6 steps. The span harness that
    # draws slow 23% of the time uses 14 with 4 dropped, and a probe that
    # samples less of the episode than the thing it is being compared
    # against is not comparable to it.
    for name, tenant, _ in sides:
        torch.cuda.synchronize()
        origin = torch.cuda.Event(enable_timing=True)
        origin.record()
        marks = _span_series(warm_adapters[tenant], torch,
                             pool.for_quota(16).handle,
                             episode_steps)
        torch.cuda.synchronize()
        warm_adapters[tenant].drain_timing()
        spans = [origin.elapsed_time(y) - origin.elapsed_time(x)
                 for x, y in marks]
        solo[name] = (statistics.median(spans[1:]) if len(spans) > 1
                      else spans[0])

    marks = {}

    def side(name, tenant, stream):
        marks[name] = _span_series(warm_adapters[tenant], torch,
                                   stream.handle, episode_steps)
        warm_adapters[tenant].drain_timing()

    torch.cuda.synchronize()
    origin = torch.cuda.Event(enable_timing=True)
    origin.record()
    threads = [threading.Thread(target=side, args=s) for s in sides]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    torch.cuda.synchronize()

    per_side = {}
    for name, _, _ in sides:
        got = marks.get(name) or []
        drop = min(4, max(1, len(got) // 3))
        spans = [origin.elapsed_time(y) - origin.elapsed_time(x)
                 for x, y in got][drop:]
        if spans and solo[name]:
            per_side[name] = statistics.median(spans) / solo[name]
    ext = statistics.mean(per_side.values()) if per_side else None
    # 1.57 is the midpoint of the two measured states, 1.274 and 1.871.
    return {"externality": ext, "per_side": per_side,
            "state": ("slow" if ext and ext > 1.57 else "fast")}


def measure_isolated(args, torch, models, pool, warm_adapters, *, label):
    service, p99 = {}, {}
    print(f"measuring isolated service [{label}] ...", flush=True)
    for tenant in models:
        steps = (args.urgent_steps if tenant == "urgent" else args.video_steps)
        service[tenant], p99[tenant] = isolated_service(
            warm_adapters[tenant], pool, steps, args, torch)
        print(f"  {tenant:6s} service {service[tenant]:7.2f} s   "
              f"step p99 {p99[tenant] * 1000:7.1f} ms", flush=True)
    return service, p99


def run_one(policy_name, args, torch, models, pool, pipelines,
            warm_adapters, loaded_s, began_all, out_template, many,
            shared=None, drawn=None):
    began_all = time.perf_counter() - loaded_s
    if shared is not None:
        service, p99 = shared
    else:
        service, p99 = measure_isolated(args, torch, models, pool,
                                        warm_adapters, label=policy_name)
    warmed_s = time.perf_counter() - began_all

    sizing = CellSpec(load=args.load, burst=args.burst,
                      deadline_slack=args.deadline_slack, seed=args.seed,
                      horizon_s=1.0, urgent_steps=args.urgent_steps,
                      video_steps=args.video_steps,
                      deadline_base=args.deadline_base)
    horizon = horizon_for_urgent_count(sizing,
                                       urgent_service_s=service["urgent"],
                                       wanted=args.urgent_count)
    spec = CellSpec(load=args.load, burst=args.burst,
                    deadline_slack=args.deadline_slack, seed=args.seed,
                    horizon_s=horizon, urgent_steps=args.urgent_steps,
                    video_steps=args.video_steps,
                    deadline_base=args.deadline_base)
    trace = build_trace(spec, urgent_service_s=service["urgent"],
                        video_service_s=service["video"],
                        # The request's isolated latency, not its step
                        # p99. A deadline of 1.5 x 0.110 s on a request
                        # needing 0.87 s alone is unmeetable by
                        # construction, and the first run reported miss
                        # rate 1.0 because of exactly that.
                        urgent_isolated_latency_p99_s=service["urgent"])
    print(f"cell {spec.cell_id}: horizon {horizon:.0f} s, "
          f"{len(trace.requests)} requests", flush=True)

    policy = (POLICY_FACTORIES[policy_name]() if policy_name
              in POLICY_FACTORIES else BASELINES[policy_name])
    runtime = Runtime(policy, stream_pool=pool,
                      drift_tolerance=args.drift_tolerance)
    # Warm-up is residency, not a tenant's debt: on the card a first step
    # measured 1.855 s against a 0.157 s steady step, and charging it made
    # one tenant's ledger 10.8x the other's for identical work.
    for tenant, model in models.items():
        runtime.warm(model, 0.0)
        runtime.resident_models.add(model)

    pending = list(trace.requests)
    admitted, finished = {}, {}
    began = time.perf_counter()
    rounds = 0
    while True:
        now = time.perf_counter() - began
        while pending and pending[0].arrival_s <= now:
            request = pending.pop(0)
            tenant = request.tenant
            # One resident adapter per tenant, reused for every request of
            # that tenant. That is the runtime's own shape -- weights and
            # conditioning are resident and a request is a state passed
            # through them -- and TenantRegistry offers one request per
            # tenant at a time, so two requests never need one adapter at
            # once. It also means every request of a tenant denoises the
            # same seed and produces the same latent: a nuisance variable
            # removed deliberately, and harmless here because the
            # scheduler's cost does not depend on the latent's content.
            adapter = warm_adapters[tenant]
            queued = QueuedRequest(request_id=request.request_id,
                                   tenant=tenant, model=models[tenant],
                                   arrival_s=request.arrival_s,
                                   steps=request.steps,
                                   deadline_s=request.deadline_s)
            runtime.submit(queued, StepExecutor(queued, adapter,
                                                total_steps=request.steps))
            admitted[request.request_id] = (request, now)
        if not pending and runtime.all_finished():
            break
        if now > horizon + args.drain_grace_s:
            print("  drain grace exceeded; stopping with work outstanding",
                  flush=True)
            break
        record = runtime.tick(now)
        rounds += 1
        # Completions are read from the runtime's own retirement, not
        # from its executors: tick() retires a finished request itself,
        # so an executor is gone by the time a caller could see it
        # complete. Watching the executors instead reported 0 of 40
        # urgent requests done while the ledger showed 1360 quota-seconds
        # charged to that tenant -- a metric wrong in the direction that
        # looks like a scheduling failure.
        for rid in runtime.retired:
            if rid not in finished:
                finished[rid] = time.perf_counter() - began
        # Idle rounds are not free. Without this the loop spun 4.9 million
        # times in 106 seconds, which is the same defect the hour soak
        # found: a watchdog measuring rounds rather than work.
        if not record.granted:
            following = (pending[0].arrival_s - (time.perf_counter() - began)
                         if pending else 0.05)
            time.sleep(max(0.0, min(0.05, following)))
    ran_s = time.perf_counter() - began

    urgent = [(r, finished.get(r.request_id))
              for r, _ in admitted.values() if r.tenant == "urgent"]
    video = [(r, finished.get(r.request_id))
             for r, _ in admitted.values() if r.tenant == "video"]
    latencies = [done - r.arrival_s for r, done in urgent if done is not None]
    misses = [1 for r, done in urgent
              if done is None or (r.deadline_s is not None
                                  and done > r.deadline_s)]
    video_steps_done = sum(
        runtime.retired.get(r.request_id, {}).get("steps_done", 0)
        for r, _ in video)
    urgent_steps_done = sum(
        runtime.retired.get(r.request_id, {}).get("steps_done", 0)
        for r, _ in urgent)

    payload = {
        "schema_version": "burstserve.amd-matrix-cell/v1",
        "cell": spec.cell_id,
        "policy": policy_name,
        "drift_tolerance": args.drift_tolerance,
        "spec": {"load": spec.load, "burst": spec.burst,
                 "deadline_slack": spec.deadline_slack,
                 "deadline_base": spec.deadline_base, "seed": spec.seed,
                 "horizon_s": horizon, "urgent_steps": args.urgent_steps,
                 "video_steps": args.video_steps},
        "one_adapter_per_tenant": True,
        "identical_seeds_within_tenant": True,
        "isolated_service_s": service,
        "isolated_step_p99_s": p99,
        "isolated_shared_across_group": shared is not None,
        "drawn_co_run_state": drawn,
        "video_model": args.video_model,
        "deadline_base_s": service["urgent"],
        "deadline_base": "isolated request latency, not step p99",
        "requests": {"total": len(trace.requests),
                     "urgent": len(urgent), "video": len(video),
                     "admitted": len(admitted), "finished": len(finished),
                     "outstanding": len(trace.requests) - len(finished)},
        "urgent": {
            # Per-request latency and slack, kept whole. A miss rate is a
            # threshold applied to these, so keeping them lets any
            # deadline be evaluated afterwards for the policies that do
            # not read deadlines -- which is most of the baselines -- and
            # keeps a pre-registration honest about where the metric can
            # discriminate at all.
            "latencies_s": sorted(latencies),
            "deadline_s_from_arrival": (
                spec.deadline_slack * service["urgent"]
                * (spec.burst if spec.deadline_base == "burst" else 1)),
            "steps_done": urgent_steps_done,
            "completed": len(latencies),
            "miss_rate": len(misses) / len(urgent) if urgent else None,
            "latency_p50_s": statistics.median(latencies) if latencies else None,
            "latency_p99_s": (sorted(latencies)[min(len(latencies) - 1,
                                                    int(0.99 * len(latencies)))]
                              if latencies else None),
        },
        "video": {"requests": len(video), "steps_done": video_steps_done,
                  "goodput_steps_per_s": video_steps_done / ran_s if ran_s
                  else None},
        "ledger": {
            "rounds": rounds,
            "decision_p99_s": runtime.decision_p99_seconds(),
            "quota_seconds_by_tenant": runtime.quota_seconds_by_tenant,
            "charged_from_measurement": runtime.charged_from_measurement(),
            "serial_fallbacks": sum(
                1 for r in runtime.ledger if r.notes.get("serial_fallback")),
            "stale_quota_measurements": sum(
                len(r.notes.get("stale_quota_measurements", []))
                for r in runtime.ledger),
        },
        # The wall-clock question the single-SKU decision opened: four
        # cards in parallel became one card serially, and how many cells
        # the two-week window holds follows from this number.
        "wall_clock_s": {"model_load": loaded_s,
                         "isolated_measurement": warmed_s - loaded_s,
                         "cell_run": ran_s,
                         "total": time.perf_counter() - began_all},
    }
    out = Path(out_template.replace("POLICY", policy_name) if many
               else out_template)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n")

    print(f"\ncell {spec.cell_id} [{policy_name}]")
    print(f"  urgent {len(latencies)}/{len(urgent)} done, "
          f"miss rate {payload['urgent']['miss_rate']}")
    print(f"  video  {video_steps_done} steps, "
          f"{payload['video']['goodput_steps_per_s']:.3f} steps/s")
    print(f"  rounds {rounds}, decision p99 "
          f"{(runtime.decision_p99_seconds() or 0) * 1e6:.1f} us")
    print(f"  wall clock: load {loaded_s:.0f}s + isolated "
          f"{warmed_s - loaded_s:.0f}s + run {ran_s:.0f}s")
    print(f"-> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
