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


def exclusive_priority(states: Sequence[RequestState], units: int,
                       now: float) -> dict[int, int]:
    """Whole die to the latency-critical tenant whenever it has work.

    The heuristic anyone would write first for a latency-critical tenant
    beside a batch one, and the one this project's comparator set never
    contained. ``exclusive_fcfs`` is not it: the registry rotates every
    round, so that policy is whole-die time-slicing *between* tenants and
    hands the die to the batch tenant every other round.

    No partitioning at all. If spatial partitioning is worth anything on
    the SLO metric, it has to beat this -- and if it does not, the
    scheduling claim reduces to "prioritise the urgent tenant", which
    needs no masks.

    Latency-critical is read as "carries a deadline" rather than as a
    tenant name, and EDF breaks ties inside that class. Making it a name
    check would let the policy work only on this workload's labels.
    """
    if not states:
        return {}
    critical = [s for s in states if s.request.deadline_s is not None]
    if critical:
        target = min(critical,
                     key=lambda s: (s.request.deadline_s, s.request.request_id))
    else:
        target = states[0]
    return {target.request.request_id: units}


def deadline_quota(states: Sequence[RequestState], units: int,
                   now: float,
                   splits: Sequence[int] = (4, 8, 16, 24, 28)) -> dict[int, int]:
    """Give the latency-critical tenant the *smallest* quota that still
    makes its deadline, and everything else to the batch tenant.

    The action space this project actually measured. 1.5 sweeps five
    splits on the mismatched workload and every one of them
    Pareto-dominates rotation, with the most asymmetric giving the
    largest urgent gain: 4+28 is +71.4% urgent against +1.4% video.
    ``step_matched_pairing`` and ``deadline_aware`` cannot issue any of
    them -- both return either an even split or the whole die, an action
    space of exactly two -- so nothing measured before this policy tested
    dynamic quota selection at all. It tested switching between 16+16 and
    32+0.

    The rule is deadline-driven rather than throughput-driven because the
    metric is a deadline: the smallest quota that still makes it is the
    one that leaves the most die for the tenant with no deadline, and a
    larger quota buys the critical tenant nothing it can be scored on.
    Predictions, never true costs, so the choice degrades with the
    predictor like every other decision here.

    When no split makes the deadline the whole die goes to the critical
    tenant -- the same rescue ``deadline_aware`` performs, reached after
    five candidates instead of one.
    """
    if not states:
        return {}
    if len(states) == 1:
        return {states[0].request.request_id: units}

    critical = [s for s in states if s.request.deadline_s is not None]
    if not critical:
        return static_even(states, units, now)
    target = min(critical,
                 key=lambda s: (s.request.deadline_s, s.request.request_id))
    others = [s for s in states if s.request.request_id
              != target.request.request_id]
    if not others:
        return {target.request.request_id: units}
    peer = others[0]

    left = target.request.deadline_s - now
    remaining = target.request.steps - target.steps_done

    def believed(state: RequestState, quota: int) -> float:
        per_step = state.predicted_step_seconds.get(quota)
        return float("inf") if per_step is None else per_step

    for split in sorted(splits):
        quota = max(1, min(units - 1, round(split * units / 32)))
        if remaining * believed(target, quota) <= left:
            return {target.request.request_id: quota,
                    peer.request.request_id: units - quota}
    return {target.request.request_id: units}


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
                         slow_factor: float = 1.3) -> dict[int, int]:
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

    ``slow_factor`` multiplies the *paired* expectation. The states are
    1.445x apart, and 1.3 is chosen to fail safe rather than to sit at
    the midpoint:

      * a fast pairing under a -20% prediction reads as 1.25x, below the
        threshold, so it is not thrown away;
      * a slow pairing under a +20% prediction reads as 1.20x, also below
        it, so it is not caught.

    Those two bands overlap, so at +/-20% no threshold both catches slow
    pairings and spares fast ones. 1.3 resolves that toward doing
    nothing: past roughly 10% predictor error the probe degrades to a
    no-op and the policy behaves exactly like ``slo_aware_partitioning``,
    which is the safe direction. Acting on a prediction too noisy to
    support the action is how a probe turns into damage.
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
    #
    # Only an observation taken at the quota now being considered counts.
    # A step measured while running alone says nothing about how a
    # pairing performs, and on hardware a request's first step also
    # carries kernel compilation -- 1.86 s against a 0.16 s steady step.
    # Reading either as evidence about a pairing deadlocks the probe: the
    # verdict stops the request being paired, so no newer observation
    # ever arrives, so the verdict stands forever.
    #
    # The comparison is against the *paired* expectation, not the solo
    # one. The prediction is a solo step cost; a pairing in the fast state
    # already costs 1.24x that, so comparing observed to the raw
    # prediction left only 13% of headroom under a 1.4 threshold and a
    # -20% prediction error crossed it. The states are 1.445x apart, so
    # the midpoint between them is the natural threshold. The test itself
    # lives in ``_pairing_reads_slow`` so the sticky variant cannot drift
    # from it.
    if _pairing_reads_slow(states, assignment, slow_factor):
        # Drop the pairing for one round. Re-forming it next round is a
        # fresh draw; holding it is not.
        #
        # 2026-08-08: the hardware says otherwise. Eight processes
        # re-formed their pairings nine times each, with fresh adapters
        # and re-acquired streams, and none redrew; the state is keyed by
        # the mask pair and survives even interposing a different pair. So
        # this drops for one round and pays for the same answer again next
        # round, indefinitely. The frozen algorithm is left as it is and
        # the alternative lives beside it in
        # ``make_sticky_probing_partitioning`` -- a frozen baseline is
        # worth more as a comparison than as a thing to quietly improve.
        behind = min(
            states,
            key=lambda s: (s.tenant_quota_seconds, s.request.request_id),
        )
        return {behind.request.request_id: units}
    return assignment


def _pairing_reads_slow(states: Sequence[RequestState],
                        assignment: dict[int, int],
                        slow_factor: float) -> bool:
    """Does a formed pairing's own measurement say it is in the slow state?

    Extracted from ``probing_partitioning`` verbatim so the two policies
    cannot drift apart on the threshold. The behavioural lock covers the
    frozen policy, so a change here that altered it would fail there.
    """
    for state in states:
        if state.request.request_id not in assignment:
            continue
        quota = assignment[state.request.request_id]
        if getattr(state, "observed_at_units", None) != quota:
            continue
        observed = state.observed_step_seconds
        expected = state.predicted_step_seconds.get(quota)
        if observed is None or not expected:
            continue
        if observed > expected * FAST_PAIRING_EXTERNALITY * slow_factor:
            return True
    return False


def make_sticky_probing_partitioning(*, tolerance: float = 1.6,
                                     slow_factor: float = 1.3,
                                     base_backoff_s: float = 1.0,
                                     max_backoff_s: float = 60.0):
    """``probing_partitioning``, but it remembers which pairings are slow.

    The frozen policy re-forms a slow pairing every round because
    re-forming was believed to redraw the state. Measured on the die, it
    does not: the draw is keyed by the mask pair, survives fresh adapters,
    fresh streams and an interposed different pair, and did not flip once
    in eight processes over nine episodes each. A policy that keeps
    re-forming therefore pays one degraded paired round in every two, for
    as long as the pairing is offered.

    So a slow verdict here is remembered against the pair of granted
    widths -- the mask pair, which is what the hardware keys on, not the
    request ids, which change as requests complete.

    **The backoff is not decoration.** Nothing measured rules out the
    state changing on timescales longer than the two minutes observed, and
    a policy that never re-probes could not find out. Doubling from one
    second to a minute costs one degraded round per doubling and keeps the
    recovery path open. A permanent verdict would be a stronger claim than
    the evidence supports.

    Returns a fresh closure per call: the memory is per run, and sharing
    one instance between simulations would carry a verdict across
    experiments that have nothing to do with each other.
    """
    memory: dict[tuple[int, ...], dict[str, float]] = {}

    def policy(states: Sequence[RequestState], units: int,
               now: float) -> dict[int, int]:
        if not states:
            return {}
        provisional = slo_aware_partitioning(states, units, now,
                                             tolerance=tolerance)
        pair_key = tuple(sorted(provisional.values()))
        if len(provisional) >= 2:
            entry = memory.get(pair_key)
            if entry is not None and now < entry["until"]:
                # Known slow and still within its backoff. Rotating beats a
                # slow pairing by 25.9%, so this is the cheaper answer and
                # it is already paid for.
                behind = min(states, key=lambda s: (s.tenant_quota_seconds,
                                                    s.request.request_id))
                return {behind.request.request_id: units}

        decided = probing_partitioning(states, units, now,
                                       tolerance=tolerance,
                                       slow_factor=slow_factor)
        # Record only what the pairing's own measurement says. The frozen
        # policy also goes exclusive to rescue a deadline, and attributing
        # that to a slow draw would poison a pairing that is fine.
        if len(provisional) >= 2 and _pairing_reads_slow(states, provisional,
                                                         slow_factor):
            entry = memory.get(pair_key)
            backoff = (base_backoff_s if entry is None
                       else min(max_backoff_s, entry["backoff"] * 2))
            memory[pair_key] = {"backoff": backoff, "until": now + backoff}
        return decided

    policy.memory = memory
    policy.__name__ = "sticky_probing_partitioning"
    return policy


# Stateful policies are built per run rather than shared, so they are kept
# out of BASELINES -- tests iterate that dict, and a policy carrying a
# verdict from one simulation into the next would be a silent coupling.
def make_fixed_split(urgent_units: int):
    """A static partition of the die: the urgent tenant always gets
    ``urgent_units``, the video tenant the rest.

    This is the baseline that decides whether the scheduler has a
    contribution at all. Every adaptive policy in this file chooses a
    split at run time; if a split chosen once at deployment time matches
    them, then the measured benefit belongs to spatial partitioning and
    not to any decision we make about it, and the honest claim is
    "partition the die" rather than "schedule the partition".

    Two details keep the comparison from being rigged in our favour:

    * With one tenant runnable the lone request gets the whole die. A
      strict partition that idled the absent tenant's slice would be a
      strawman -- it would lose to everything, and it is not what anyone
      deploys.
    * ``make_fixed_split(16)`` is exactly ``static_even``, including on
      same-tenant pairs, which it handles by falling back to the even
      split. The sweep therefore contains the known baseline as a special
      case, so a disagreement between them at 16 is a bug and not a
      result.
    """
    if not 0 < urgent_units < 32:
        raise ValueError("a split has to leave both tenants something")

    def fixed_split(states: Sequence[RequestState], units: int,
                    now: float) -> dict[int, int]:
        if not states:
            return {}
        if len(states) == 1:
            return {states[0].request.request_id: units}
        left, right = states[0], states[1]
        if left.request.tenant == right.request.tenant:
            share = units // 2
            return {left.request.request_id: share,
                    right.request.request_id: share}
        # Scaled, so the knob means the same fraction of whatever die the
        # runtime was built with rather than 32 units specifically.
        share = max(1, min(units - 1, round(urgent_units * units / 32)))
        if left.request.tenant == "urgent":
            return {left.request.request_id: share,
                    right.request.request_id: units - share}
        return {left.request.request_id: units - share,
                right.request.request_id: share}

    fixed_split.__name__ = f"fixed_split_{urgent_units}"
    return fixed_split


POLICY_FACTORIES = {
    "sticky_probing_partitioning": make_sticky_probing_partitioning,
}

# The five splits the pairing table was measured at. Anything else would
# make the simulator interpolate an externality, and the measured ones
# span 1.002x to 1.063x, so an interpolation is not a small error.
FIXED_SPLITS = (4, 8, 16, 24, 28)
for _u in FIXED_SPLITS:
    POLICY_FACTORIES[f"fixed_split_{_u}"] = (lambda u=_u: make_fixed_split(u))


BASELINES["deadline_aware"] = deadline_aware
BASELINES["deadline_quota"] = deadline_quota
BASELINES["exclusive_priority"] = exclusive_priority
BASELINES["probing_partitioning"] = probing_partitioning
BASELINES["step_matched_pairing"] = step_matched_pairing
BASELINES["slo_aware_partitioning"] = slo_aware_partitioning
