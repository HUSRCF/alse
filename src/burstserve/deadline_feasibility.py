"""Whether a deadline trace could have been met, and by what.

Gate C asks that a constructed feasible trace contain no avoidable miss.
Both halves of that need a definition. A miss is avoidable only if some
schedule would have prevented it, so the gate is empty without a way to
decide feasibility -- and a policy trivially satisfies "no avoidable
miss" on a trace where nothing was achievable in the first place.

Feasibility here is decided by preemptive EDF on the whole die. On a
single preemptable resource EDF is optimal: if it misses, no ordering of
exclusive service succeeds. Preemption is legitimate in this model
because the scheduler switches at step boundaries, which is the same
granularity EDF is run at here.

That makes the test sufficient rather than necessary. Spatial
partitioning finishes more work per second than exclusive service does,
so a trace EDF cannot meet may still be met by splitting the die. A
trace EDF *can* meet is therefore certainly feasible, which is the
direction the gate needs: it asks for traces known to be feasible, and
this proves that for the ones it constructs.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from .trace_sim import QuotaCostModel, Request, Trace

sys.dont_write_bytecode = True


@dataclass(frozen=True)
class FeasibilityReport:
    feasible: bool
    missed: tuple[int, ...]
    completion_s: dict[int, float]
    witness: str

    def __bool__(self) -> bool:
        return self.feasible


def edf_whole_die(trace: Trace, quantum_s: float = 0.25) -> FeasibilityReport:
    """Run preemptive EDF at full width and report what it missed.

    Requests without a deadline are still executed -- they consume the
    die and so affect whether the others make it -- but they are ranked
    last and cannot be missed.
    """
    units = 32
    remaining = {
        request.request_id: request.steps for request in trace.requests
    }
    step_cost = {
        request.request_id: QuotaCostModel.for_model(
            request.model
        ).step_seconds(units)
        for request in trace.requests
    }
    by_id = {request.request_id: request for request in trace.requests}
    completion: dict[int, float] = {}
    now = 0.0
    # Bound the loop by total work: every iteration completes at least one
    # step, so this cannot spin even if a cost table is degenerate.
    for _ in range(sum(remaining.values()) + 1):
        ready = [
            rid for rid, left in remaining.items()
            if left > 0 and by_id[rid].arrival_s <= now
        ]
        if not ready:
            pending = [
                by_id[rid].arrival_s for rid, left in remaining.items()
                if left > 0
            ]
            if not pending:
                break
            now = min(pending)
            continue
        chosen = min(
            ready,
            key=lambda rid: (
                by_id[rid].deadline_s
                if by_id[rid].deadline_s is not None else float("inf"),
                rid,
            ),
        )
        # One step, not one quantum: preemption happens at step boundaries,
        # so a step is the smallest unit that can be scheduled. Charging a
        # quantum instead would let EDF switch mid-step and claim a
        # feasibility the runtime cannot deliver.
        now += step_cost[chosen]
        remaining[chosen] -= 1
        if remaining[chosen] == 0:
            completion[chosen] = now

    missed = tuple(sorted(
        rid for rid, request in by_id.items()
        if request.deadline_s is not None
        and completion.get(rid, float("inf")) > request.deadline_s
    ))
    return FeasibilityReport(
        feasible=not missed,
        missed=missed,
        completion_s=completion,
        witness="preemptive EDF, whole die, step-granular preemption",
    )


def feasible_deadline_trace(
    *,
    slack: float = 1.35,
    per_tenant: int = 3,
    steps: int = 20,
    models: tuple[str, str] = ("sdxl", "sdxl"),
) -> Trace:
    """Build a trace whose feasibility is established, not assumed.

    Deadlines are set from what EDF at full width actually achieves,
    multiplied by ``slack``. Deriving them from the achieved completion
    time rather than from a nominal per-request runtime is what makes the
    trace feasible by construction: a deadline computed from one
    request's own cost ignores the queueing behind it and produces traces
    that are quietly impossible.
    """
    plain = Trace([
        Request(request_id=i, tenant=f"t{i % 2}", model=models[i % 2],
                arrival_s=0.0, steps=steps)
        for i in range(per_tenant * 2)
    ])
    achieved = edf_whole_die(plain).completion_s
    return Trace([
        Request(
            request_id=request.request_id,
            tenant=request.tenant,
            model=request.model,
            arrival_s=request.arrival_s,
            steps=request.steps,
            deadline_s=achieved[request.request_id] * slack,
        )
        for request in plain.requests
    ])


def avoidable_misses(result, trace: Trace) -> tuple[int, ...]:
    """Misses a policy caused that a feasible schedule would not have.

    Only meaningful on a trace EDF can meet; on an infeasible one every
    miss is unavoidable and this returns nothing, which is why callers
    have to check feasibility first rather than read a zero here as a
    pass.
    """
    report = edf_whole_die(trace)
    if not report.feasible:
        return ()
    finished = {
        state.request.request_id: state.finished_s
        for state in result.completed
    }
    missed = []
    for request in trace.requests:
        if request.deadline_s is None:
            continue
        done = finished.get(request.request_id)
        if done is None or done > request.deadline_s:
            missed.append(request.request_id)
    return tuple(sorted(missed))
