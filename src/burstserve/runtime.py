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
                 stream_pool=None, drift_tolerance: float = 0.15,
                 predictor_error: float = 0.0,
                 externality_blind: bool = False,
                 charge_currency: str = "quota-seconds",
                 fallback_backoff_s: float = 1.0,
                 max_fallback_backoff_s: float = 60.0):
        self.policy = policy
        # Optional so the loop stays testable without a GPU. When present,
        # each granted request runs on a stream carrying its own CU mask,
        # and the masks of a co-running pair are constructed disjoint
        # rather than assumed to be.
        self.stream_pool = stream_pool
        # Multiplies every prediction the policy is shown, and nothing
        # else. plan.md's week 15 acceptance is that +/-10% causes no
        # safety failure and +/-20% degrades conservatively, and a
        # scheduler can only be tested against a wrong belief if the
        # belief can be made wrong on purpose.
        #
        # It is applied where the policy reads, not where the ledger
        # charges. Perturbing the charge too would test a runtime whose
        # measurements are also wrong, which is a different and much
        # weaker claim: the dual ledger exists precisely so a wrong
        # prediction meets a right measurement.
        self.predictor_error = predictor_error
        # Ablations, week 15. Both exist so a claim can be shown to
        # depend on the thing it is claimed to depend on.
        #
        # externality_blind makes the runtime believe a co-run costs what
        # a solo step costs. The drift envelope then cannot tell "this
        # pairing costs more because pairings do" from "the cost model is
        # wrong about this pairing", which is the distinction the
        # externality term exists to draw -- and getting that wrong once
        # already made the envelope fire on every round of a runtime
        # behaving exactly as predicted.
        self.externality_blind = externality_blind
        # The accounting currency. quota-seconds is units x seconds, and
        # the fairness claim rests on it: a tenant holding 8 units for a
        # second has not consumed what a tenant holding 24 did. Charging
        # wall-seconds ignores the width, and step-count ignores both.
        if charge_currency not in ("quota-seconds", "wall-seconds",
                                   "step-count"):
            raise ValueError(f"unknown currency {charge_currency!r}")
        self.charge_currency = charge_currency
        # plan.md: fall back to a serial action past 15% drift.
        self.drift_tolerance = drift_tolerance
        self.fallback_backoff_s = fallback_backoff_s
        self.max_fallback_backoff_s = max_fallback_backoff_s
        self.mask_attestations: list[dict] = []
        self.maskable_units = maskable_units
        self.registry = TenantRegistry(discipline=discipline)
        self.executors: dict[int, StepExecutor] = {}
        self.requests: dict[int, QueuedRequest] = {}
        self.ledger: list[RoundRecord] = []
        self.quota_seconds_by_tenant: dict[str, float] = {}
        self.die_seconds_by_tenant: dict[str, float] = {}
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
        # Pairings the runtime has refused, by the pair of granted
        # widths, with the backoff after which each is retried.
        self._fallbacks: dict[tuple[int, ...], dict] = {}
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

    def _serial_fallback(self, granted: dict[int, int],
                         now_s: float) -> tuple[dict[int, int], str | None]:
        """Refuse a co-run the runtime cannot vouch for, and say why.

        plan.md's week 9-10 clause: fall back to a conservative serial
        action when the profile is missing or drift exceeds 15%, recording
        the reason. This sits in the runtime rather than the policy
        because the policy is frozen, and because it is a safety envelope
        rather than a scheduling choice -- it can only ever narrow a
        grant to one request holding the whole die.

        **The fallback expires, and that is not a softening of the
        clause.** A pairing that drifts badly is not necessarily a pairing
        that will keep drifting: SDXL against CogVideoX-2b runs at 6.29x
        predicted for two rounds -- a drift of 529%, five times over any
        threshold -- and then at 1.01x for the rest of the process, where
        it beats rotation by 41.7%. Four processes did the same thing to
        within 0.02. A permanent refusal there scores 0.9982 against a
        whole die's 1.0000: worse than never partitioning. So the refusal
        holds for a backoff that doubles, and the pairing is retried.

        Keyed by the pair of granted widths, because that is what the die
        was measured to key its co-run state on.
        """
        if len(granted) < 2:
            return granted, None
        key = tuple(sorted(granted.values()))
        held = self._fallbacks.get(key)
        if held is not None and now_s < held["until"]:
            return self._to_serial(granted), held["reason"]

        # Profile missing: the cost model declines to invent a factor for
        # a pairing it has never measured, and a co-run charged at 1.0 is
        # an invention with a ledger entry.
        models = {self.requests[rid].model for rid in granted}
        widths = sorted(granted.values())
        if len(widths) == 2:
            for rid, units in granted.items():
                peer = next(u for r, u in granted.items() if r != rid)
                try:
                    externality(units, peer, self.requests[rid].model)
                except Exception:
                    reason = (f"no measured profile for {widths[0]}+"
                              f"{widths[1]} across {sorted(models)}")
                    self._hold(key, reason, now_s)
                    return self._to_serial(granted), reason
        return granted, None

    def _hold(self, key, reason: str, now_s: float) -> None:
        held = self._fallbacks.get(key)
        backoff = (self.fallback_backoff_s if held is None
                   else min(self.max_fallback_backoff_s,
                            held["backoff"] * 2))
        self._fallbacks[key] = {"backoff": backoff,
                                "until": now_s + backoff,
                                "reason": reason}

    def _to_serial(self, granted: dict[int, int]) -> dict[int, int]:
        """The whole die to one request: the conservative action.

        The one kept is the one furthest behind on its tenant's charge,
        matching what the policy does when its own probe refuses a
        pairing, so a fallback does not also become a fairness event.
        """
        behind = min(
            granted,
            key=lambda rid: (
                self.quota_seconds_by_tenant.get(
                    self.requests[rid].tenant, 0.0), rid),
        )
        return {behind: self.maskable_units}


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
        skew = 1.0 + self.predictor_error
        state.predicted_step_seconds = {
            units: cost.step_seconds(units) * skew
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

        granted, fallback_reason = self._serial_fallback(granted, now_s)

        predicted = {s.request.request_id: s.predicted_step_seconds.get(
            granted.get(s.request.request_id, self.maskable_units))
            for s in states}
        observed: dict[int, float] = {}
        charge_sources: set[str] = set()
        # Requests whose only available measurement was taken at a
        # different quota. Recorded rather than silently absorbed:
        # a charge falling back to the model is exactly what the
        # dual ledger is not supposed to do, so it must be visible.
        stale_charges: set[int] = set()

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
            # A measurement taken at a different quota is not a
            # measurement of this step. Adapters that report the width
            # alongside the time are checked against it here rather than
            # trusted: the reading is deferred by one step and only lands
            # when the previous step's events have retired, so on hardware
            # a re-granted request read the old width's cost for a whole
            # run -- 107 ms for a 16-unit step, which is the 32-unit cost.
            # The adapters drain on a width change now; this is the second
            # line, because the ledger is the thing that must not be
            # quietly wrong. Adapters that report no width are unaffected.
            measured_units = getattr(executor.adapter, "last_step_units",
                                     None)
            if (measured is not None and measured_units is not None
                    and measured_units != units):
                measured = None
                stale_charges.add(rid)
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

            # The accounting currency, and the fairness claim rests on
            # which one this is. units x seconds says a tenant holding 8
            # units for a second has not consumed what one holding 24
            # did; wall-seconds says they have; step-count says a cheap
            # step and an expensive one are the same. The alternatives
            # exist to be measured, not offered.
            if self.charge_currency == "wall-seconds":
                charge = spent
            elif self.charge_currency == "step-count":
                charge = 1.0
            else:
                charge = units * spent
            self.quota_seconds_by_tenant[queued.tenant] = (
                self.quota_seconds_by_tenant.get(queued.tenant, 0.0)
                + charge
            )
            # Always units x seconds, whatever the currency. The charge
            # above is what the policy is shown and therefore what it
            # equalises; this is what fairness is scored on. Keeping only
            # the first would make every currency perfectly fair in its
            # own units, which is exactly the question the ablation asks.
            self.die_seconds_by_tenant[queued.tenant] = (
                self.die_seconds_by_tenant.get(queued.tenant, 0.0)
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

        # Drift, judged on this round's own ledger and acted on next
        # round. plan.md asks for a serial fallback past 15%; the runtime
        # cannot un-run the round it just measured, so what a drift
        # verdict buys is the round after it.
        # Against the *paired* expectation, not the solo one. The
        # prediction is a solo step cost and a co-run legitimately costs
        # more than it, so comparing to the raw prediction reports a
        # 23.7% drift for a runtime doing exactly what the cost model
        # says -- the envelope would fire on every round and the fallback
        # would be permanent. plan.md's clause is the co-run prediction
        # error, which is this one.
        drift_hold = None
        if len(granted) >= 2 and fallback_reason is None:
            worst = 0.0
            for rid, spent in observed.items():
                belief = predicted.get(rid)
                if not belief:
                    continue
                peer_units = next((u for r, u in granted.items()
                                   if r != rid), None)
                if not self.externality_blind:
                    try:
                        belief *= externality(granted[rid], peer_units,
                                              self.requests[rid].model)
                    except Exception:
                        continue
                # Over-run only. Using abs() here fired the envelope
                # when a pairing came in *faster* than its paired
                # expectation, which is a pessimisation and not a
                # conservative action: what threatens a deadline is a
                # step costing more than believed, never less.
                #
                # Measured, and it inverted an ablation. With the paired
                # expectation at 1.2367x solo, an observed step at 1.00x
                # -- a pairing that turned out nearly free, which happens
                # whenever one side finishes and the other runs on at a
                # paired quota -- reads as 19% drift and takes the whole
                # die serial. The externality-blind arm, whose belief is
                # smaller, read the same round as 0% and did not. That is
                # why blind held *less* on hardware while a simulation at
                # 1.28x showed it holding more.
                worst = max(worst, spent / belief - 1.0)
            if worst > self.drift_tolerance:
                key = tuple(sorted(granted.values()))
                drift_hold = (f"drift {worst * 100:.1f}% over "
                              f"{self.drift_tolerance * 100:.0f}% on "
                              f"{key[0]}+{key[1]}")
                self._hold(key, drift_hold, now_s)

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
            notes={"charged_from": sorted(charge_sources),
                   "stale_quota_measurements": sorted(stale_charges),
                   "serial_fallback": fallback_reason,
                   "drift_hold": drift_hold},
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
