"""A trace-driven simulator whose costs come from measurement, not guesses.

Gate C asks for byte-identical results from the same seed, and for
canonical service accounting to track SM quota within 1%. Both are
properties of the simulator's construction rather than of its tuning, so
they are built in here:

* every quantity that varies is drawn from a seeded PRNG owned by the
  simulator, and the event loop breaks ties by a total order over
  (time, sequence, tenant), so no result depends on dict iteration,
  floating-point summation order, or wall-clock timing;

* step costs come from the Gate B quota tables via
  :class:`QuotaCostModel`, and co-run costs from the measured externality
  table. Where the table has no entry the simulator says so rather than
  interpolating silently -- the measured penalty spans 22.3% to 192.6%
  across four pairs, so an invented value in between could be wrong by a
  factor of eight.

The scheduler itself is deliberately not decided here. This module
provides the world; policies are separate so that baselines and an oracle
can be compared on identical traces.
"""

from __future__ import annotations

import heapq
import random
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

# Measured on gfx1201, 2026-08-03/04. serial is the fraction of solo time
# that does not shrink with quota, taken from the fitted Amdahl parameters.
MEASURED_MODELS: dict[str, dict[str, float]] = {
    "sdxl": {
        "serial_fraction": 0.391,
        "step_seconds_at_full": 0.1521,   # 1024x1024, 32 units
        "maskable_units": 32,
    },
    "cogvideox-2b": {
        "serial_fraction": 0.276,
        "step_seconds_at_full": 0.5171,   # 9 frames, 32 units
        "maskable_units": 32,
    },
}

# The measured p50 at each quota, from the Gate B tables. Preferred over
# the Amdahl fit wherever an entry exists: the fit exists to extrapolate to
# quotas that were never run, and using it where a measurement is available
# throws away accuracy for no reason. Its residual is not neutral either --
# the fit under-predicts speed at low quota, which under-states exactly the
# effect this work claims, putting the partitioning gain at 1.6% where the
# measurements put it at 8.7%.
MEASURED_QUOTA_SECONDS: dict[str, dict[int, float]] = {
    "sdxl": {4: 24.828, 8: 13.058, 12: 9.208, 16: 7.393,
             20: 6.376, 24: 5.721, 28: 5.231, 32: 4.919},
    "cogvideox-2b": {4: 41.770, 8: 21.348, 12: 14.604, 16: 11.283,
                     20: 9.669, 24: 8.630, 28: 7.889, 32: 7.413},
}

# Measured pairwise externality: (own units, peer units) -> slowdown factor
# applied to this tenant's step time. From the 2026-08-06 table.
MEASURED_EXTERNALITY: dict[tuple[int, int], float] = {
    (16, 16): 1.223,
    (8, 24): 1.495,
    (24, 8): 1.280,
    (4, 28): 1.307,
    (28, 4): 1.926,
}


class UnmeasuredPairing(LookupError):
    """Raised when a co-run pairing has no measured externality.

    Deliberately not interpolated. Across the measured pairs the penalty
    ranges from 1.22x to 1.93x and is not monotone in either quota, so a
    value invented between two entries could be wrong by a large factor in
    an unknown direction.
    """


@dataclass(frozen=True)
class QuotaCostModel:
    """Step time as a function of quota, from a measured quota table."""

    serial_fraction: float
    step_seconds_at_full: float
    maskable_units: int = 32
    measured_curve: tuple[tuple[int, float], ...] = ()

    def __post_init__(self) -> None:
        if not 0.0 <= self.serial_fraction < 1.0:
            raise ValueError("serial_fraction must be in [0, 1)")
        if self.step_seconds_at_full <= 0:
            raise ValueError("step_seconds_at_full must be positive")

    def step_seconds(self, units: int) -> float:
        """Measured where measured, Amdahl only where it is not."""
        if not 1 <= units <= self.maskable_units:
            raise ValueError(
                f"quota {units} outside 1..{self.maskable_units}"
            )
        curve = dict(self.measured_curve)
        if units in curve:
            # Scale the measured p50 to a per-step time using the full-die
            # cell as the reference, so the ratios are exactly the measured
            # ones rather than the fit's approximation of them.
            return self.step_seconds_at_full * (
                curve[units] / curve[self.maskable_units]
            )
        share = units / self.maskable_units
        serial = self.serial_fraction
        return self.step_seconds_at_full * (serial + (1.0 - serial) / share)

    def is_measured(self, units: int) -> bool:
        """Whether this quota came from a measurement or from the fit."""
        return units in dict(self.measured_curve)

    @classmethod
    def for_model(cls, name: str) -> "QuotaCostModel":
        if name not in MEASURED_MODELS:
            raise KeyError(
                f"no measured quota table for {name!r}; "
                f"have {sorted(MEASURED_MODELS)}"
            )
        curve = MEASURED_QUOTA_SECONDS.get(name, {})
        return cls(**MEASURED_MODELS[name],
                   measured_curve=tuple(sorted(curve.items())))


def externality(own_units: int, peer_units: int | None) -> float:
    """Slowdown factor for a tenant sharing the die with one peer."""
    if peer_units is None:
        return 1.0
    key = (own_units, peer_units)
    if key not in MEASURED_EXTERNALITY:
        raise UnmeasuredPairing(
            f"no measured externality for {own_units}+{peer_units}; "
            f"measured pairs are {sorted(MEASURED_EXTERNALITY)}"
        )
    return MEASURED_EXTERNALITY[key]


@dataclass(frozen=True)
class Request:
    """One inference request. Immutable so a trace cannot drift."""

    request_id: int
    tenant: str
    model: str
    arrival_s: float
    steps: int
    deadline_s: float | None = None

    def __post_init__(self) -> None:
        if self.steps <= 0:
            raise ValueError("a request needs at least one step")
        if self.arrival_s < 0:
            raise ValueError("arrival must not be negative")


@dataclass
class RequestState:
    request: Request
    steps_done: int = 0
    started_s: float | None = None
    finished_s: float | None = None
    service_seconds: float = 0.0
    quota_seconds: float = 0.0     # units x seconds, the accounting currency
    # What the scheduler was told, refreshed before each decision. Never
    # used to charge or to advance time -- only to choose.
    predicted_step_seconds: dict[int, float] = field(default_factory=dict)
    # This tenant's running charge, refreshed before each decision. A policy
    # that has to keep tenants even needs to see how far apart they already
    # are, and deriving it from steps_done would be wrong: a step is worth a
    # different amount of die-time in every model and at every quota.
    tenant_quota_seconds: float = 0.0

    @property
    def complete(self) -> bool:
        return self.steps_done >= self.request.steps


@dataclass(order=True)
class _Event:
    """Ordered by (time, sequence) so ties never depend on insertion luck."""

    time_s: float
    sequence: int
    kind: str = field(compare=False)
    payload: object = field(compare=False, default=None)


class Trace:
    """A reproducible request stream.

    Generated from a seed rather than from wall-clock timing, and sorted
    into a total order, so the same seed yields the same trace on any host.
    """

    def __init__(self, requests: Sequence[Request]):
        self.requests = tuple(
            sorted(requests, key=lambda r: (r.arrival_s, r.request_id))
        )

    def __len__(self) -> int:
        return len(self.requests)

    def __iter__(self):
        return iter(self.requests)

    @classmethod
    def poisson(
        cls,
        *,
        seed: int,
        tenants: Sequence[tuple[str, str]],
        rate_per_s: float,
        horizon_s: float,
        steps: int,
        deadline_slack: float | None = None,
    ) -> "Trace":
        """A Poisson arrival stream, round-robin across tenants.

        Uses its own Random instance seeded explicitly: the global random
        module is process state, and a simulation that reads it is not
        reproducible in a process that used random for anything else.
        """
        if rate_per_s <= 0:
            raise ValueError("rate must be positive")
        rng = random.Random(seed)
        requests: list[Request] = []
        now = 0.0
        index = 0
        while True:
            now += rng.expovariate(rate_per_s)
            if now > horizon_s:
                break
            tenant, model = tenants[index % len(tenants)]
            deadline = None
            if deadline_slack is not None:
                nominal = QuotaCostModel.for_model(model).step_seconds(
                    MEASURED_MODELS[model]["maskable_units"]
                ) * steps
                deadline = now + nominal * deadline_slack
            requests.append(Request(
                request_id=index, tenant=tenant, model=model,
                arrival_s=now, steps=steps, deadline_s=deadline,
            ))
            index += 1
        return cls(requests)


# A policy sees the runnable requests and the die, and returns a quota per
# request. Returning fewer entries than requests leaves the rest unserved.
Policy = Callable[[Sequence[RequestState], int, float], dict[int, int]]


class Predictor:
    """What the scheduler believes a step costs, which is not what it costs.

    Gate C requires safe degradation at +/-5%, 10% and 20% predictor error.
    Modelling that needs the belief and the truth kept apart: a simulator
    where the policy reads the true cost cannot degrade at all, and would
    report perfect robustness for a scheduler that has none.

    Error is drawn once per (request, quota) and cached, so a policy that
    asks twice gets one answer. A predictor that returned a fresh sample
    each call would let a policy average the noise away by asking
    repeatedly, which no real predictor permits.
    """

    def __init__(self, *, relative_error: float = 0.0, seed: int = 0):
        if relative_error < 0:
            raise ValueError("relative_error is a magnitude; use >= 0")
        self.relative_error = relative_error
        self._rng = random.Random(seed)
        self._cache: dict[tuple[int, int], float] = {}

    def step_seconds(self, request_id: int, model: str, units: int) -> float:
        key = (request_id, units)
        if key not in self._cache:
            true_cost = QuotaCostModel.for_model(model).step_seconds(units)
            if self.relative_error:
                factor = 1.0 + self._rng.uniform(
                    -self.relative_error, self.relative_error
                )
            else:
                factor = 1.0
            self._cache[key] = true_cost * factor
        return self._cache[key]

    def is_exact(self) -> bool:
        return self.relative_error == 0.0


@dataclass
class SimulationResult:
    """Everything a Gate C criterion is judged on, and nothing derived."""

    completed: list[RequestState]
    unfinished: list[RequestState]
    horizon_s: float
    quota_seconds_by_tenant: dict[str, float]
    service_seconds_by_tenant: dict[str, float]
    steps_executed: int
    unmeasured_pairings: list[tuple[int, int]]
    predictor_relative_error: float = 0.0
    granted_unit_seconds: float = 0.0
    quantum_s: float = 0.25
    peak_lag_unit_seconds: float = 0.0

    def jain_index(self) -> float:
        """Fairness over the accounting currency, not over wall time.

        Wall time charges a tenant for being slowed by a peer; quota-seconds
        charge it for capacity it was given. The 2026-08-06 externality
        table makes the difference concrete -- at an 8+24 split the larger
        tenant is slowed 128% by a peer it did not choose.
        """
        values = [v for v in self.quota_seconds_by_tenant.values()]
        if not values:
            return 1.0
        total = sum(values)
        if total <= 0:
            return 1.0
        return total ** 2 / (len(values) * sum(v ** 2 for v in values))

    def full_die_equivalent_seconds(self) -> float:
        """Work completed, expressed as time it would take on the full die.

        This is the utilisation numerator. Counting wall time served, or
        quota-seconds, would both credit a policy for holding the die
        rather than for finishing work -- and a partitioned policy holds
        the die exactly as much as an exclusive one does.
        """
        total = 0.0
        for state in self.completed + self.unfinished:
            model = MEASURED_MODELS[state.request.model]
            full_step = model["step_seconds_at_full"]
            total += state.steps_done * full_step
        return total

    def utilisation(self) -> float:
        """Full-die-equivalent work per second of horizon.

        Can exceed 1.0, and that is the finding rather than a bug: two
        tenants overlap their serial phases, so the die completes more
        full-die-equivalent work per second than one tenant at full width.
        """
        if self.horizon_s <= 0:
            return 0.0
        return self.full_die_equivalent_seconds() / self.horizon_s

    def accounting_error(self) -> float:
        """Relative gap between what was charged and what was handed out.

        Gate C bounds this at 1%. It is a self-consistency check, not a
        performance metric: if the sum of every tenant's quota-seconds does
        not match the units the scheduler actually granted, some tenant is
        being charged for capacity it did not get, or is holding capacity
        nobody is charged for -- and the fairness index cannot see either.
        """
        charged = sum(self.quota_seconds_by_tenant.values())
        if self.granted_unit_seconds <= 0:
            return 0.0 if charged == 0 else float("inf")
        return abs(charged - self.granted_unit_seconds) / self.granted_unit_seconds

    def service_lag_quanta(self, backlogged: Sequence[str] | None = None) -> float:
        """Worst deviation from an equal share, in quanta, among backlogged
        tenants.

        Gate C bounds this at two quanta absent deadline overrides, and the
        definition matters. The gap between the largest and smallest total
        is not lag: two tenants submitting different amounts of work should
        receive different amounts of service, and charging that as unfairness
        would make every heterogeneous trace look broken. Lag is the
        distance from the share a tenant was owed.

        A tenant is owed an equal share only while it is backlogged. One
        that had nothing to run cannot be behind, so the caller names which
        tenants were continuously demanding; with no such list the measure
        covers every tenant and is an upper bound.
        """
        names = (
            [t for t in self.quota_seconds_by_tenant if t in set(backlogged)]
            if backlogged is not None
            else list(self.quota_seconds_by_tenant)
        )
        if len(names) < 2 or self.quantum_s <= 0:
            return 0.0
        values = [self.quota_seconds_by_tenant[n] for n in names]
        fair_share = sum(values) / len(values)
        full_die_quantum = 32 * self.quantum_s
        return max(abs(v - fair_share) for v in values) / full_die_quantum

    def peak_service_lag_quanta(self) -> float:
        """The bound Gate C actually states: worst lag at any instant.

        Sampled every round over the tenants that were backlogged at that
        moment. The end-of-run figure cannot serve: a policy that runs one
        tenant to completion and then the other finishes exactly even,
        having been maximally unfair the whole way.
        """
        if self.quantum_s <= 0:
            return 0.0
        return self.peak_lag_unit_seconds / (32 * self.quantum_s)

    def deadline_misses(self) -> list[RequestState]:
        missed = []
        for state in self.completed:
            deadline = state.request.deadline_s
            if deadline is not None and state.finished_s is not None:
                if state.finished_s > deadline:
                    missed.append(state)
        # An unfinished request with a deadline in the past has missed it.
        for state in self.unfinished:
            if state.request.deadline_s is not None:
                if self.horizon_s > state.request.deadline_s:
                    missed.append(state)
        return missed


def simulate(
    trace: Trace,
    policy: Policy,
    *,
    horizon_s: float,
    maskable_units: int = 32,
    quantum_s: float = 0.25,
    seed: int = 0,
    predictor: "Predictor | None" = None,
) -> SimulationResult:
    """Run a trace under a policy.

    Time advances in fixed quanta rather than to the next step boundary.
    A step-boundary loop would let a tenant with short steps be rescheduled
    more often than one with long steps, which silently favours it; a fixed
    quantum gives every tenant the same decision points. The quantum is
    recorded in the result because Gate C bounds service lag in units of it.
    """
    if horizon_s <= 0:
        raise ValueError("horizon must be positive")
    if quantum_s <= 0:
        raise ValueError("quantum must be positive")

    # The scheduler's beliefs. Execution always uses the true cost below,
    # so an inaccurate predictor degrades the decisions without corrupting
    # the measurement of what those decisions cost.
    if predictor is None:
        predictor = Predictor(relative_error=0.0, seed=seed)

    costs = {name: QuotaCostModel.for_model(name) for name in MEASURED_MODELS}
    pending = list(trace.requests)
    states: dict[int, RequestState] = {}
    runnable: list[RequestState] = []
    completed: list[RequestState] = []
    # Seeded with every tenant in the trace, at zero. A tenant that is
    # never served would otherwise be absent from the dict, and a fairness
    # index computed over the survivors cannot see starvation at all -- it
    # reports 1.0 for a policy that fed one tenant and ignored the other.
    tenants_in_trace = {r.tenant for r in trace.requests}
    quota_seconds: dict[str, float] = {t: 0.0 for t in tenants_in_trace}
    service_seconds: dict[str, float] = {t: 0.0 for t in tenants_in_trace}
    unmeasured: list[tuple[int, int]] = []
    steps_executed = 0
    # Units actually handed out, times the seconds they were held. The
    # accounting must reconcile against this: a tenant charged less than it
    # held is being subsidised by the others, and the fairness index would
    # not see it.
    granted_unit_seconds = 0.0
    # Peak lag, sampled every round. The final lag is not the bound: a
    # policy that serves one tenant to completion and then the other ends
    # perfectly even while having been maximally unfair throughout.
    peak_lag_unit_seconds = 0.0

    now = 0.0
    while now < horizon_s:
        # Admit everything that has arrived, in trace order.
        while pending and pending[0].arrival_s <= now:
            request = pending.pop(0)
            state = RequestState(request=request)
            states[request.request_id] = state
            runnable.append(state)

        for state in runnable:
            state.tenant_quota_seconds = quota_seconds.get(
                state.request.tenant, 0.0
            )
        if not runnable:
            if not pending:
                break
            now = max(now + quantum_s, pending[0].arrival_s)
            continue

        # Sorted before the policy sees it: a policy must not be able to
        # depend on the order requests happened to be appended in.
        ordered = sorted(
            runnable, key=lambda s: (s.request.arrival_s, s.request.request_id)
        )
        for state in ordered:
            state.predicted_step_seconds = {
                units: predictor.step_seconds(
                    state.request.request_id, state.request.model, units
                )
                for units in (4, 8, 12, 16, 20, 24, 28, 32)
            }
        assignment = policy(ordered, maskable_units, now)
        granted = {
            rid: units for rid, units in sorted(assignment.items())
            if units > 0
        }
        if sum(granted.values()) > maskable_units:
            raise ValueError(
                f"policy assigned {sum(granted.values())} of "
                f"{maskable_units} units"
            )

        if not granted:
            now += quantum_s
            continue

        round_seconds = 0.0
        round_spent: dict[int, float] = {}

        # Externality needs each tenant's peer. Only pairs are measured, so
        # a triple is reported rather than approximated.
        active = sorted(granted.items())
        for rid, units in active:
            peer = None
            if len(active) == 2:
                peer = next(u for r, u in active if r != rid)
            elif len(active) > 2:
                unmeasured.append((units, -len(active)))
                peer = None
            state = states[rid]
            try:
                factor = externality(units, peer)
            except UnmeasuredPairing:
                unmeasured.append((units, peer if peer is not None else -1))
                factor = 1.0
            step_cost = costs[state.request.model].step_seconds(units) * factor
            # Whole steps only: a partially executed denoising step is not
            # a state the runtime can checkpoint. At least one step runs
            # even when it outlasts the quantum -- truncating instead
            # deadlocks any configuration whose step exceeds the quantum,
            # which a 16+16 split does at 0.299 s against 0.25.
            affordable = max(1, int(quantum_s // step_cost))
            remaining = state.request.steps - state.steps_done
            taken = min(affordable, remaining)
            if taken == 0:
                continue
            if state.started_s is None:
                state.started_s = now
            state.steps_done += taken
            spent = taken * step_cost
            state.service_seconds += spent
            state.quota_seconds += units * spent
            steps_executed += taken
            quota_seconds[state.request.tenant] = (
                quota_seconds.get(state.request.tenant, 0.0) + units * spent
            )
            service_seconds[state.request.tenant] = (
                service_seconds.get(state.request.tenant, 0.0) + spent
            )
            if state.complete:
                state.finished_s = now + spent
            round_seconds = max(round_seconds, spent)
            round_spent[rid] = spent

        # Advance by the work actually done, not by a fixed quantum. A
        # fixed advance charges the horizon for time nobody used: at 0.152 s
        # per step against a 0.25 s quantum the die would idle 39% of every
        # round, and utilisation would read 0.61 for a policy that never
        # left it idle.
        advance = round_seconds if round_seconds > 0 else quantum_s
        # Everyone holding units holds them for the whole round, including
        # a tenant whose own step finished early. Charging only the time a
        # tenant was computing would let a fast tenant occupy the die for
        # free while a slower peer sets the round length.
        granted_unit_seconds += sum(granted.values()) * advance
        for rid, units in granted.items():
            tenant = states[rid].request.tenant
            held_over = advance - round_spent.get(rid, 0.0)
            if held_over > 0:
                quota_seconds[tenant] = (
                    quota_seconds.get(tenant, 0.0) + units * held_over
                )
        now += advance
        backlogged_now = {s.request.tenant for s in runnable}
        if len(backlogged_now) > 1:
            charged = [quota_seconds.get(t, 0.0) for t in sorted(backlogged_now)]
            fair = sum(charged) / len(charged)
            peak_lag_unit_seconds = max(
                peak_lag_unit_seconds, max(abs(c - fair) for c in charged)
            )
        newly_done = [s for s in runnable if s.complete]
        for state in newly_done:
            runnable.remove(state)
            completed.append(state)

    return SimulationResult(
        completed=sorted(completed, key=lambda s: s.request.request_id),
        unfinished=sorted(runnable, key=lambda s: s.request.request_id),
        horizon_s=min(now, horizon_s),
        quota_seconds_by_tenant=dict(sorted(quota_seconds.items())),
        service_seconds_by_tenant=dict(sorted(service_seconds.items())),
        steps_executed=steps_executed,
        unmeasured_pairings=sorted(unmeasured),
        predictor_relative_error=predictor.relative_error,
        granted_unit_seconds=granted_unit_seconds,
        quantum_s=quantum_s,
        peak_lag_unit_seconds=peak_lag_unit_seconds,
    )
