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

from .trace_sim import MEASURED_EXTERNALITY, RequestState


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


BASELINES["deadline_aware"] = deadline_aware
