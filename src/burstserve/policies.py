"""Scheduling policies and the baselines they have to beat.

The claim this work makes is about utilisation, and the measured quota
tables say where the headroom comes from. SDXL spends 39.1% of its
full-die step time in work that does not shrink with quota; CogVideoX-2b
spends 27.6%. Under time-slicing that serial portion is paid once per
request in series. Under spatial partitioning two tenants pay theirs at
the same time, so the die finishes more work per second than it does
running one tenant at full width -- even though each tenant is slower.

That is why the baselines here are not strawmen. ``exclusive_fcfs`` is
what a runtime without spatial partitioning does, and it is the strongest
single-tenant policy available: every request gets the whole die. If
partitioning did not overlap serial work, it could not beat that.
"""

from __future__ import annotations

from typing import Sequence

from .trace_sim import (
    MEASURED_EXTERNALITY,
    SLOW_PAIRING_EXTERNALITY,
    FAST_PAIRING_EXTERNALITY,
    RequestState,
)


def exclusive_fcfs(states: Sequence[RequestState], units: int,
                   now: float) -> dict[int, int]:
    """One request at a time on the whole die -- the time-slicing baseline.

    Not a weak comparator: it gives every request the maximum quota the
    hardware can offer, and its per-request latency is the best achievable.
    What it cannot do is overlap two tenants' serial work.
    """
    if not states:
        return {}
    return {states[0].request.request_id: units}


def static_even(states: Sequence[RequestState], units: int,
                now: float) -> dict[int, int]:
    """Split the die evenly across runnable requests.

    Capped at two concurrent tenants, because that is what the externality
    table measures. Admitting a third would make every step cost a number
    the simulator has to invent, and the measured penalties span 1.22x to
    1.93x, so an invented one is not a small error.
    """
    if not states:
        return {}
    concurrent = min(2, len(states))
    share = units // concurrent
    return {
        state.request.request_id: share
        for state in states[:concurrent]
    }


def measured_pairs_only(states: Sequence[RequestState], units: int,
                        now: float) -> dict[int, int]:
    """Prefer splits the externality table actually covers.

    A scheduler that only ever chooses measured configurations is one whose
    predicted cost is never an extrapolation. It is also a real constraint
    on what the table has to contain, which is the point of building one.
    """
    if not states:
        return {}
    if len(states) == 1:
        return {states[0].request.request_id: units}
    for left, right in sorted(MEASURED_EXTERNALITY):
        if left + right == units and left == right:
            return {
                states[0].request.request_id: left,
                states[1].request.request_id: right,
            }
    return exclusive_fcfs(states, units, now)


def oracle_shortest_remaining(states: Sequence[RequestState], units: int,
                              now: float) -> dict[int, int]:
    """Whole die to the request with the fewest steps left.

    An oracle only in the sense that it uses remaining work, which a real
    scheduler learns from a predictor rather than reads. It bounds what
    latency-optimal exclusive scheduling can achieve, so a partitioning
    policy that beats it on throughput is not merely beating a poor
    ordering.
    """
    if not states:
        return {}
    best = min(
        states,
        key=lambda s: (s.request.steps - s.steps_done, s.request.request_id),
    )
    return {best.request.request_id: units}


BASELINES = {
    "exclusive_fcfs": exclusive_fcfs,
    "oracle_shortest_remaining": oracle_shortest_remaining,
    "static_even": static_even,
    "measured_pairs_only": measured_pairs_only,
}


def deadline_aware(states: Sequence[RequestState], units: int,
                   now: float) -> dict[int, int]:
    """Split by default; go exclusive only when it changes an outcome.

    Splitting is worth 8.8% throughput, so giving the whole die to one
    request has to buy something. It buys nothing for a request that will
    miss either way, and nothing for one that will make it either way. It
    is worth doing only in the band between: the request misses if it
    shares and makes it if it does not.

    Every comparison uses ``predicted_step_seconds``, never the true cost,
    so an inaccurate predictor mislabels which band a request is in --
    which is what "safe degradation" has to be measured against. A policy
    that peeked at the truth would look perfectly robust while being
    nothing of the kind.
    """
    if not states:
        return {}
    half = units // 2

    def believed(state: RequestState, quota: int) -> float:
        per_step = state.predicted_step_seconds.get(quota)
        if per_step is None:
            return float("inf")
        return (state.request.steps - state.steps_done) * per_step

    rescuable = []
    for state in states:
        deadline = state.request.deadline_s
        if deadline is None:
            continue
        left = deadline - now
        if believed(state, half) <= left:
            continue                      # makes it while sharing
        if believed(state, units) > left:
            continue                      # misses even with the whole die
        rescuable.append(state)

    if rescuable:
        # Tightest first, and only one -- the die cannot be given twice.
        target = min(
            rescuable,
            key=lambda s: (s.request.deadline_s - now, s.request.request_id),
        )
        return {target.request.request_id: units}
    return static_even(states, units, now)


def step_matched_pairing(states: Sequence[RequestState], units: int,
                         now: float, tolerance: float = 1.6) -> dict[int, int]:
    """Share the die only between tenants whose steps take similar time.

    ``static_even`` wins 8.8% on matched tenants and *loses* on mismatched
    ones, and the reason is structural rather than incidental: a round is
    as long as its slowest participant, so an SDXL tenant at 0.152 s/step
    beside CogVideoX-2b at 0.517 s/step spends two thirds of every round
    holding units it is not using. Pairing is not free, and a scheduler
    that pairs unconditionally inherits that loss.

    So the pairing is conditional. Candidates are ranked by predicted step
    time at half the die -- predicted, not true, so the choice degrades
    with the predictor like every other decision here -- and a pair is
    admitted only when the slower step is within ``tolerance`` of the
    faster. The default 1.6 sits between the measured extremes: SDXL
    against itself is 1.0 and SDXL against CogVideoX-2b is 3.4.

    Failing the test means the whole die goes to one request -- but to the
    tenant furthest behind on charge, not to whoever arrived first. That
    distinction is the whole bound: FCFS exclusive reaches full throughput
    on mismatched tenants and a peak lag of 12.17 quanta doing it, because
    it runs one request to completion before starting the next. Rotating on
    deficit keeps the same throughput with lag bounded by one round.

    Pairing is across tenants, never within one. Ranking every request by
    predicted cost and taking the first two looks equivalent while the
    predictor is exact -- equal predictions tie-break on request id, which
    happens to alternate tenants -- and starves a tenant outright once the
    predictor is not. Under +/-5% error two of one tenant's requests can
    both predict cheapest, take the whole die between them, and leave the
    other tenant unserved for the entire run: 22.37 quanta of lag at
    unchanged throughput, which is why utilisation alone cannot detect it.
    """
    if not states:
        return {}
    half = units // 2

    def believed_step(state: RequestState) -> float:
        per_step = state.predicted_step_seconds.get(half)
        return float("inf") if per_step is None else per_step

    # One candidate per tenant -- its own cheapest request -- so a pair can
    # never be one tenant with itself.
    cheapest_per_tenant: dict[str, RequestState] = {}
    for state in states:
        held = cheapest_per_tenant.get(state.request.tenant)
        if held is None or (
            (believed_step(state), state.request.request_id)
            < (believed_step(held), held.request.request_id)
        ):
            cheapest_per_tenant[state.request.tenant] = state

    # Furthest behind first: pairing is also the moment to correct a
    # deficit, and picking by predicted cost alone would let a tenant that
    # is already ahead keep the seat.
    candidates = sorted(
        cheapest_per_tenant.values(),
        key=lambda s: (s.tenant_quota_seconds, believed_step(s),
                       s.request.request_id),
    )
    if len(candidates) >= 2:
        first, second = candidates[0], candidates[1]
        quick = min(believed_step(first), believed_step(second))
        sluggish = max(believed_step(first), believed_step(second))
        if quick > 0 and sluggish / quick <= tolerance:
            return {
                first.request.request_id: half,
                second.request.request_id: units - half,
            }
    behind = min(
        states,
        key=lambda s: (s.tenant_quota_seconds, s.request.request_id),
    )
    return {behind.request.request_id: units}


def slo_aware_partitioning(states: Sequence[RequestState], units: int,
                           now: float, tolerance: float = 1.6) -> dict[int, int]:
    """The scheduler Gate C is about, composed from what each half proves.

    The two preceding policies each hold part of the gate and break the
    other. ``deadline_aware`` meets every avoidable deadline and pairs
    tenants whose steps do not match, losing 30% of the die when they
    differ by 3.4x. ``step_matched_pairing`` bounds service lag and keeps
    throughput on both matched and mismatched traffic, and misses
    deadlines it could have met. Neither ordering of the two is arbitrary:

    1. A deadline that exclusivity would save and sharing would lose takes
       the die. This is the only decision that overrides the others, and
       Gate C exempts it from the lag bound for exactly that reason.
    2. Otherwise pair, but only within ``tolerance`` on predicted step
       time, because pairing mismatched tenants costs more than it earns.
    3. Otherwise the whole die to the tenant furthest behind on charge --
       never to whoever arrived first, which is what turns a 12.17-quantum
       lag into a bounded one.

    Every branch reads ``predicted_step_seconds``, so predictor error
    degrades the choice rather than being hidden by ground truth.
    """
    if not states:
        return {}
    rescued = deadline_aware(states, units, now)
    if len(rescued) == 1 and next(iter(rescued.values())) == units:
        # deadline_aware went exclusive, which it does only to save a
        # deadline that is otherwise lost. Honour it.
        return rescued
    return step_matched_pairing(states, units, now, tolerance=tolerance)


def probing_partitioning(states: Sequence[RequestState], units: int,
                         now: float, tolerance: float = 1.6,
                         slow_factor: float = 1.4) -> dict[int, int]:
    """Pair, check what the pairing actually cost, re-form it if it is slow.

    The same 16+16 pairing measured 44 times lands in one of two states,
    drawn per pairing at roughly 30% for the fast one, 46% apart, with no
    hardware quantity yet found that predicts which. Four explanations
    were proposed and retracted before the null was measured.

    A scheduler does not need the explanation. The states are far apart
    and each is internally tight -- cv 0.25% in the fast state -- so one
    step is enough to tell them apart, and re-forming the pairing draws
    again. Pairing blindly loses 10% against the whole die; pairing with
    a probe gains 19%.

    ``slow_factor`` sits between the two states rather than being fitted
    to either: 1.4 against a separation of 1.45x. A threshold tuned to
    the observed values would be a threshold tuned to this card.
    """
    if not states:
        return {}
    assignment = slo_aware_partitioning(states, units, now,
                                        tolerance=tolerance)
    if len(assignment) < 2:
        return assignment

    # Deadlines are budgeted against the slow state, not the fast one.
    # A pairing that only meets a deadline if it draws well is a deadline
    # met by luck: the draw is 30% fast, so budgeting on the fast figure
    # misses seven times in ten. Anything that cannot afford the slow
    # draw gets the die to itself, where there is no draw to lose.
    slow_penalty = SLOW_PAIRING_EXTERNALITY / FAST_PAIRING_EXTERNALITY
    for state in states:
        quota = assignment.get(state.request.request_id)
        deadline = state.request.deadline_s
        if quota is None or deadline is None:
            continue
        per_step = state.predicted_step_seconds.get(quota)
        if per_step is None:
            continue
        todo = state.request.steps - state.steps_done
        if todo * per_step * slow_penalty > deadline - now:
            return exclusive_fcfs([state], units, now)

    # Judge the pairing that is actually running by what its steps cost.
    for state in states:
        if state.request.request_id not in assignment:
            continue
        observed = state.observed_step_seconds
        expected = state.predicted_step_seconds.get(
            assignment[state.request.request_id]
        )
        if observed is None or not expected:
            continue
        if observed > expected * slow_factor:
            # Drop the pairing for one round. Re-forming it next round is
            # a fresh draw; holding it is not.
            behind = min(
                states,
                key=lambda s: (s.tenant_quota_seconds, s.request.request_id),
            )
            return {behind.request.request_id: units}
    return assignment


BASELINES["deadline_aware"] = deadline_aware
BASELINES["probing_partitioning"] = probing_partitioning
BASELINES["step_matched_pairing"] = step_matched_pairing
BASELINES["slo_aware_partitioning"] = slo_aware_partitioning
