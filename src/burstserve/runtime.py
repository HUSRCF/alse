"""The scheduling loop, driving real executors with the frozen policy.

Gate C froze a policy against a simulator. That is worth exactly nothing
if the policy cannot be handed the runtime's own state, so this loop
calls the frozen function unchanged -- same signature, same
``RequestState`` shape -- and the adaptation happens on this side.
Rewriting the policy to suit the runtime would have quietly unfrozen it.

Each round does four things in a fixed order, and the order is the point:

  1. refresh what the policy is allowed to see
  2. ask the policy, timing only that call
  3. execute the granted steps
  4. record what was decided, what it cost, and what changed

Step 2 is timed alone because plan.md's acceptance is "scheduler p99
under 1 ms", and a measurement that included the denoising would be
measuring the model. Step 4 is separate from step 3 because a ledger
written from the same values the decision consumed cannot detect a
decision that was not carried out.

The ledger records both the belief and the outcome per round. Charging
from prediction would make the accounting exact by construction and
useless: the point of a dual ledger is that predicted and actual can
disagree, and the disagreement is what admission control and the debt
model are supposed to read.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from .executor import Phase, StepExecutor
from .queues import Discipline, QueuedRequest, TenantRegistry
from .trace_sim import QuotaCostModel, Request, RequestState, externality


@dataclass
class RoundRecord:
    """One scheduling round, before and after.

    plan.md asks for the ledger, slack, resident set and measured result
    around every decision. Kept as one record per round rather than a
    stream of events, because the questions asked of it later -- did this
    decision cause that miss, was the charge consistent with the grant --
    are all per-round.
    """

    round_index: int
    now_s: float
    decision_seconds: float
    granted: dict[int, int]
    offered: list[int]
    backlogged: tuple[str, ...]
    predicted_step_seconds: dict[int, float]
    observed_step_seconds: dict[int, float]
    quota_seconds_by_tenant: dict[str, float]
    resident_models: tuple[str, ...]
    weight_bytes_moved: int = 0
    notes: dict[str, Any] = field(default_factory=dict)

    @property
    def prediction_error(self) -> dict[int, float]:
        """Per request, how wrong the belief was.

        The dual ledger exists so this can be non-zero and visible. A
        runtime that charged from prediction would report zero here for
        every round and learn nothing.
        """
        out = {}
        for rid, observed in self.observed_step_seconds.items():
            predicted = self.predicted_step_seconds.get(rid)
            if predicted:
                out[rid] = observed / predicted - 1.0
        return out


class Runtime:
    """One service process: queues, executors, a policy, and a ledger."""

    def __init__(self, policy: Callable, *, maskable_units: int = 32,
                 discipline: Discipline = Discipline.FCFS,
                 clock: Callable[[], float] | None = None,
                 stream_pool=None):
        self.policy = policy
        # Optional so the loop stays testable without a GPU. When present,
        # each granted request runs on a stream carrying its own CU mask,
        # and the masks of a co-running pair are constructed disjoint
        # rather than assumed to be.
        self.stream_pool = stream_pool
        self.mask_attestations: list[dict] = []
        self.maskable_units = maskable_units
        self.registry = TenantRegistry(discipline=discipline)
        self.executors: dict[int, StepExecutor] = {}
        self.requests: dict[int, QueuedRequest] = {}
        self.ledger: list[RoundRecord] = []
        self.quota_seconds_by_tenant: dict[str, float] = {}
        self.service_seconds_by_tenant: dict[str, float] = {}
        self.resident_models: set[str] = set()
        self.weight_bytes_moved = 0
        # Time spent making a model usable -- weight transfer, kernel
        # compilation, autotuning -- charged to residency rather than to
        # whichever tenant happened to arrive first.
        #
        # Without this the first request pays for the whole process's
        # warm-up: on the card a first step measured 1.855 s against a
        # 0.157 s steady step, and charging it made one tenant's ledger
        # 10.8x the other's for identical work.
        self.startup_seconds_by_model: dict[str, float] = {}
        # Summaries of finished requests. The executors themselves are
        # released: each one holds its adapter, and an adapter holds
        # conditioning tensors on the device. A soak measured 1.284 MB
        # leaked per completed request, tracking the completion count
        # rather than the clock -- 478 MB over 372 requests. Keeping the
        # numbers and dropping the objects is what a serving process has
        # to do; keeping the objects is a leak with a ledger attached.
        self.retired: dict[int, dict] = {}
        self._round = 0
        # Injectable so tests drive time rather than sleep through it.
        # Wall time in a test would make the p99 assertion measure the
        # test host's scheduler.
        self._clock = clock or time.perf_counter

    # -- admission -----------------------------------------------------

    def retire(self, request_id: int) -> None:
        """Release a finished request's executor, keeping its numbers.

        Called on completion. The ledger and the per-tenant charges are
        unaffected; what goes is the executor, its adapter and whatever
        device memory they hold.
        """
        executor = self.executors.pop(request_id, None)
        if executor is None:
            return
        self.retired[request_id] = {
            "steps_done": executor.steps_done,
            "suspensions": executor.suspensions,
            "resumptions": executor.resumptions,
            "phase": executor.phase.name,
        }
        # Drop the adapter's references explicitly rather than relying on
        # the executor going out of scope: the caller may still hold one.
        executor.adapter = None
        executor.state = None
        executor.output = None

    def warm(self, model: str, seconds: float) -> None:
        """Record one-off cost for a model, outside any tenant's charge.

        Called by a runner that warms a model before serving with it. A
        runtime that skipped this would not be wrong about the total work
        done, only about who owes for it -- which is the whole of what
        the fairness claim is.
        """
        self.startup_seconds_by_model[model] = (
            self.startup_seconds_by_model.get(model, 0.0) + seconds
        )

    def submit(self, request: QueuedRequest, executor: StepExecutor) -> None:
        self.registry.admit(request)
        self.requests[request.request_id] = request
        self.executors[request.request_id] = executor

    # -- the loop ------------------------------------------------------

    def _state_for(self, request: QueuedRequest,
                   executor: StepExecutor) -> RequestState:
        """The shape the frozen policy reads.

        Built here rather than stored, so the runtime cannot drift into
        keeping a second copy of the truth that the policy sees and the
        ledger does not.
        """
        cost = QuotaCostModel.for_model(request.model)
        state = RequestState(
            request=Request(
                request_id=request.request_id,
                tenant=request.tenant,
                model=request.model,
                arrival_s=request.arrival_s,
                steps=request.steps,
                deadline_s=request.deadline_s,
            ),
            steps_done=executor.steps_done,
        )
        state.predicted_step_seconds = {
            units: cost.step_seconds(units)
            for units in (4, 8, 12, 16, 20, 24, 28, 32)
        }
        state.tenant_quota_seconds = self.quota_seconds_by_tenant.get(
            request.tenant, 0.0
        )
        state.observed_step_seconds = getattr(
            executor, "last_step_seconds", None
        )
        state.observed_at_units = getattr(executor, "last_step_units", None)
        return state

    def tick(self, now_s: float) -> RoundRecord:
        """One decision and its execution."""
        offered = self.registry.ready(now_s)
        states = []
        for tenant, queued in offered:
            executor = self.executors[queued.request_id]
            if executor.phase is Phase.CREATED:
                executor.prepare()
            states.append(self._state_for(queued, executor))

        # Timed alone: the acceptance is about the scheduler's own cost.
        started = self._clock()
        granted = (self.policy(states, self.maskable_units, now_s)
                   if states else {})
        decision_seconds = self._clock() - started

        predicted = {s.request.request_id: s.predicted_step_seconds.get(
            granted.get(s.request.request_id, self.maskable_units))
            for s in states}
        observed: dict[int, float] = {}
        charge_sources: set[str] = set()

        active = sorted(granted.items())

        # Lay the granted quotas out across the die before running any of
        # them, so a pair gets disjoint masks by construction. Assigning
        # each request a mask independently would let two 16-unit grants
        # both take the low half, share every unit, and still be recorded
        # as a partition.
        streams: dict[int, object] = {}
        if self.stream_pool is not None and active:
            offset = 0
            for granted_id, granted_units in active:
                streams[granted_id] = self.stream_pool.for_quota(
                    granted_units, offset=offset)
                offset += granted_units
            handles = list(streams.values())
            overlap = 0
            for index, left in enumerate(handles):
                for right in handles[index + 1:]:
                    overlap |= left.installed_mask & right.installed_mask
            if overlap:
                raise RuntimeError(
                    f"granted masks overlap on {hex(overlap)}; this is not "
                    f"a partition"
                )
            self.mask_attestations.append({
                "round": self._round,
                "masks": {rid: hex(st.installed_mask)
                          for rid, st in streams.items()},
                "units": {rid: st.units for rid, st in streams.items()},
                "disjoint": overlap == 0,
            })
        for rid, units in active:
            if units < 1:
                continue
            queued = self.requests[rid]
            executor = self.executors[rid]
            queue = self.registry.queue_for(queued.tenant)
            if rid not in [r.request_id for r in queue.in_flight]:
                queue.start(rid)

            peer = None
            if len(active) == 2:
                peer = next(u for r, u in active if r != rid)

            stream = streams.get(rid)
            if stream is not None and hasattr(executor.adapter, "stream"):
                executor.adapter.stream = stream.handle
            more = executor.run_step(quota_units=units)

            # What the step actually cost. An adapter on hardware reports
            # its own device time; only a simulated adapter falls back to
            # the cost model.
            #
            # The fallback must never be preferred. Charging from the
            # model would make predicted and observed the same number, so
            # prediction_error would read zero forever and the dual
            # ledger would be one ledger written twice -- and admission
            # control and the debt model both read the difference.
            measured = getattr(executor.adapter, "last_step_seconds", None)
            if measured is None:
                factor = 1.0
                if peer is not None:
                    try:
                        factor = externality(units, peer, queued.model)
                    except Exception:
                        factor = 1.0
                measured = (QuotaCostModel.for_model(queued.model)
                            .step_seconds(units) * factor)
                charged_from = "model"
            else:
                charged_from = "measurement"
            spent = measured
            executor.last_step_seconds = spent
            executor.last_step_units = units
            observed[rid] = spent
            charge_sources.add(charged_from)

            self.quota_seconds_by_tenant[queued.tenant] = (
                self.quota_seconds_by_tenant.get(queued.tenant, 0.0)
                + units * spent
            )
            self.service_seconds_by_tenant[queued.tenant] = (
                self.service_seconds_by_tenant.get(queued.tenant, 0.0) + spent
            )
            # Residency is charged once per model, not per request: a
            # burst of the same model after the first pays no weight
            # bytes, which is the property week 7-8 has to demonstrate.
            if queued.model not in self.resident_models:
                self.resident_models.add(queued.model)
                self.weight_bytes_moved += MODEL_WEIGHT_BYTES.get(
                    queued.model, 0
                )
            if not more:
                executor.finalize()
                queue.finish(rid)
                self.retire(rid)

        # Every unfinished request returns to waiting at the round
        # boundary, whether it ran or not, so the next round decides
        # afresh over all of them.
        #
        # A round boundary is not a preemption. The executor stays
        # RUNNING and keeps its state in place; only the queue's view
        # changes, because "in flight" means "running right now" and
        # between rounds nothing is. Calling executor.suspend() here
        # would count a suspension per round per request and make the
        # ledger's preemption count meaningless -- and the number it
        # would inflate is the one the video-stall bound is judged on.
        for tenant in list(self.registry.tenants()):
            queue = self.registry.queue_for(tenant)
            for request in list(queue.in_flight):
                queue.suspend(request.request_id)

        record = RoundRecord(
            round_index=self._round,
            now_s=now_s,
            decision_seconds=decision_seconds,
            granted=dict(granted),
            offered=[q.request_id for _, q in offered],
            backlogged=self.registry.backlogged(),
            predicted_step_seconds={k: v for k, v in predicted.items() if v},
            observed_step_seconds=observed,
            quota_seconds_by_tenant=dict(self.quota_seconds_by_tenant),
            resident_models=tuple(sorted(self.resident_models)),
            weight_bytes_moved=self.weight_bytes_moved,
            notes={"charged_from": sorted(charge_sources)},
        )
        self.ledger.append(record)
        self._round += 1
        self.registry.rotate()
        return record

    # -- what the acceptance criteria read -----------------------------

    def decision_p99_seconds(self) -> float:
        """The criterion is p99, not mean: a scheduler that is fast on
        average and occasionally slow is the one that misses deadlines."""
        if not self.ledger:
            return 0.0
        costs = sorted(r.decision_seconds for r in self.ledger)
        index = min(len(costs) - 1, int(0.99 * len(costs)))
        return costs[index]

    def weight_bytes_after_first_round(self) -> int:
        """Bytes moved once residency is established.

        The acceptance is that a same-model burst transfers no weights
        after the first, so what matters is the total *after* the round
        that established residency, not the total.
        """
        if len(self.ledger) < 2:
            return 0
        return (self.ledger[-1].weight_bytes_moved
                - self.ledger[0].weight_bytes_moved)

    def all_finished(self) -> bool:
        """True when nothing is outstanding.

        Finished requests are retired out of ``executors``, so an empty
        map means done rather than never started -- ``retired`` is what
        says which it was.
        """
        return all(e.phase is Phase.FINISHED
                   for e in self.executors.values())

    @property
    def completed(self) -> int:
        return len(self.retired)

    def stalls_by_request(self) -> dict[int, list[float]]:
        """Gaps between consecutive services of the same request.

        A video request's stall is not "how long it waited to start" but
        how long it went unserved once it had started -- the viewer sees
        a frozen frame, not a late one. So the first gap counted is
        between the first and second step, and a request that was never
        preempted has no stalls rather than a stall of zero.

        The consequence is worth stating because it looks like a gap in
        the measure and is not: a request that never starts records no
        stall at all. That is correct here and covered elsewhere --
        never starting is what the fairness index and the service-lag
        bound are for. Folding it into the stall metric would let a
        scheduler trade a starved request against a smooth one and
        still report a small worst stall.
        """
        served_at: dict[int, list[float]] = {}
        for record in self.ledger:
            for rid in record.granted:
                served_at.setdefault(rid, []).append(record.now_s)
        return {
            rid: [later - earlier
                  for earlier, later in zip(times, times[1:])]
            for rid, times in served_at.items()
            if len(times) > 1
        }

    def worst_stall_seconds(self, request_id: int | None = None) -> float:
        stalls = self.stalls_by_request()
        if request_id is not None:
            return max(stalls.get(request_id, [0.0]), default=0.0)
        return max((s for gaps in stalls.values() for s in gaps),
                   default=0.0)

    def stall_within_budget(self, budget_s: float,
                            longest_step_s: float) -> bool:
        """plan.md: stall no more than the budget plus one unpreemptable
        step.

        The allowance is one step because that is the unit the runtime
        cannot break: a decision to stop can only take effect at a step
        boundary, so a request unlucky enough to be preempted just after
        one begins waits for it regardless of the budget. Allowing more
        than one step would excuse the scheduler for something it does
        control.
        """
        return self.worst_stall_seconds() <= budget_s + longest_step_s

    def charged_from_measurement(self) -> bool:
        """Whether every charge in this run came from a measured step.

        A run that fell back to the cost model anywhere is a simulation
        of a runtime, not a runtime, and its accounting cannot be
        offered as evidence about the hardware.
        """
        sources = {s for r in self.ledger for s in r.notes.get(
            "charged_from", [])}
        return sources == {"measurement"}


# Weight footprints, from the in-process co-run measurements: one copy of
# SDXL against two, and the same for CogVideoX-2b.
MODEL_WEIGHT_BYTES = {
    "sdxl": 6_500_000_000,
    "cogvideox-2b": 7_350_000_000,
}
