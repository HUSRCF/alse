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
import hashlib
import random
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

# Measured on gfx1201, 2026-08-03/04. serial is the fraction of solo time
# that does not shrink with quota, taken from the fitted Amdahl parameters.
MEASURED_MODELS: dict[str, dict[str, float]] = {
    "sdxl": {
        # Refitted 2026-08-06 against the per-step curve. 0.391 came from
        # the call-level fit, where the VAE decode contributes a serial
        # term the denoising steps do not have; carried onto the per-step
        # curve it put the 15-unit extrapolation *below* the measured
        # 12-unit cost.
        "serial_fraction": 0.4419,
        # 768x768, 32 units, measured per denoising step with CUDA events
        # on 2026-08-06. The previous value, 0.1521, was 768 at *16* units
        # -- a half-die figure recorded as the full-die one, with a comment
        # claiming a resolution Gate B never used for this model. See
        # docs/gate-c-decision-log.md.
        "step_seconds_at_full": 0.11552,
        "maskable_units": 32,
    },
    "cogvideox-2b": {
        # Refitted with the curve below, same reason as SDXL: 0.276 came
        # from the call-level fit.
        "serial_fraction": 0.2051,
        # 9 frames, 32 units, measured per denoising step with CUDA events
        # on 2026-08-06. The previous 0.5171 was right to 0.3% -- this
        # model's constant was never the problem, only its curve.
        "step_seconds_at_full": 0.51549,
        "maskable_units": 32,
    },
}

# The measured cost at each quota. Preferred over the Amdahl fit wherever
# an entry exists: the fit exists to extrapolate to quotas that were never
# run, and using it where a measurement is available throws away accuracy
# for no reason. Its residual is not neutral either -- the fit
# under-predicts speed at low quota, which under-states exactly the effect
# this work claims.
#
# SDXL is per denoising step, measured directly at 768x768 on 2026-08-06.
# It was previously the Gate B *call* p50s, which include a VAE decode that
# does not scale with quota the way the steps do, so their ratios are
# diluted by a term the per-step ratios do not contain: 1.503 at 16/32
# where the per-step measurement says 1.363. Combined with the wrong
# full-die constant, the shipped table ran 32% to 50% high at every quota.
#
# CogVideoX-2b was rebuilt the same way on the same day. Its error was
# far smaller -- the call table ran 6.5% low at 4 units falling to 0.3%
# high at 32, against SDXL's 32% to 50% -- because its full-die constant
# was correct and only the curve was diluted. Both models are now the
# same quantity, which they were not before.
#
# Video cells are labelled by frame count: CogVideoX runs at its
# pipeline's native resolution and ignores height/width, so calling this
# curve "768x768" would name a parameter that had no effect on it.
MEASURED_QUOTA_SECONDS: dict[str, dict[int, float]] = {
    # per denoising step, seconds, 768x768
    "sdxl": {4: 0.52141, 8: 0.26875, 12: 0.19350, 16: 0.15750,
             20: 0.13867, 24: 0.12493, 28: 0.12056, 32: 0.11552},
    # per denoising step, seconds, 9 frames
    "cogvideox-2b": {4: 3.11634, 8: 1.57411, 12: 1.05900, 16: 0.80519,
                     20: 0.68764, 24: 0.60812, 28: 0.55175, 32: 0.51549},
}

# Measured pairwise externality: (own units, peer units) -> slowdown factor
# applied to this tenant's step time.
#
# (16,16) is the per-step penalty at 768x768, the mean of 18 side
# measurements across nine co-runs launched 2-5 s apart (+21.8% to
# +24.9%, mean +23.62%). It is the only entry the scheduler reads, since
# the policy either splits evenly or gives the whole die to one tenant.
#
# The launch stagger is not a detail. Co-runs whose processes start
# together, or more than 8 s apart, land in a different state entirely --
# 20 side measurements, +72.9% to +83.4%, mean +78.66% -- and in that
# state partitioning loses 17.8% instead of gaining 20.8%. The state is
# fixed before the first step and is not distinguished by temperature,
# power, any clock, GPU utilisation or the CU mask. See
# docs/gate-c-decision-log.md; the value here is the staggered one, and a
# runtime that does not control launch alignment does not get it.
#
# The remaining entries are call-level penalties measured at 512x512 and
# are used only by the currency counterexample, whose argument is about
# non-linearity rather than about absolute values.
#
# One entry per split, not per model: CogVideoX-2b cannot be co-run on this
# card at all -- one process peaks at 28.54 GB and two need 57 on 34.2 GB --
# so applying these to it is an extrapolation across models, recorded in
# the decision log rather than hidden here.
# The same 16+16 pairing measured 44 times falls into two disjoint states,
# drawn independently per pairing at roughly a 30% rate for the fast one:
# ten consecutive runs at one fixed setting gave low at positions 5, 7 and
# 10, with no ordering or drift. Four explanations for the split were
# proposed and retracted (working set, power cap, uncontrollable
# bistability, launch stagger) before the null was measured; see
# docs/gate-c-decision-log.md.
#
# The states are 46% apart and each is internally tight (cv 0.25% low,
# 1-3% high), so a scheduler can tell which one a pairing landed in from
# its first step without knowing why.
PAIRING_STATE_RATE = 0.30           # probability of the fast state
FAST_PAIRING_EXTERNALITY = 1.2362   # 18 side measurements, +21.8 to +26.4%
SLOW_PAIRING_EXTERNALITY = 1.7866   # 20 side measurements, +72.9 to +83.4%

MEASURED_EXTERNALITY: dict[tuple[int, int], float] = {
    (16, 16): 1.2362,
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


# Every cost in this module -- step times and co-run penalties alike --
# was measured at one workpoint per model: SDXL at 768x768, CogVideoX-2b
# at 9 frames. That is not a formality. Co-runs at identical step-time
# ratios measured 2026-08-06 put the per-step penalty at +22.4%/+24.5%
# for SDXL at 768 and +76.6%/+78.9% at 832, and the sign of the
# partitioning result flips with it: +16.6% at 768, -21.9% at 832.
#
# The simulator has no workpoint dimension, so it cannot represent that.
# Results computed here are statements about these workpoints, and a
# reader who carries them to another resolution is carrying a number the
# measurements contradict.
MEASURED_WORKPOINT: dict[str, str] = {
    "sdxl": "768x768",
    "cogvideox-2b": "9 frames",
}


def externality(own_units: int, peer_units: int | None) -> float:
    """Slowdown factor for a tenant sharing the die with one peer.

    Keyed by quota alone. The measured penalty also depends strongly on
    the workpoint -- see MEASURED_WORKPOINT -- and this function silently
    assumes the one the table was built at, because a trace does not
    carry a resolution to key on.
    """
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
    # The last step's measured cost. None before the first step. A policy
    # comparing this against predicted_step_seconds sees whether the
    # pairing it formed landed in the fast or slow state -- the two are
    # 46% apart, so the comparison needs no threshold tuning.
    observed_step_seconds: float | None = None

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


class PairingStates:
    """Which state each formed pairing landed in.

    Measured behaviour, not a modelling choice: the same configuration
    lands in a fast or a slow state, drawn per pairing, and the two are
    46% apart with no overlap. A simulator that used the fast figure
    unconditionally would report a gain the hardware supplies 30% of the
    time.

    A pairing is identified by the set of request ids sharing the die, so
    re-forming a pairing -- dropping it and building it again -- draws
    again. **This is an assumption, and it is the one this model is most
    exposed on.** What the hardware has shown is that relaunching the two
    processes draws afresh. The runtime being designed is one process
    with a stream per model, where re-forming means changing a stream's
    CU mask rather than relaunching; whether that redraws, or whether the
    bistability exists there at all, is unmeasured. See
    docs/gate-c-decision-log.md.
    """

    def __init__(self, *, seed: int = 0, rate: float = PAIRING_STATE_RATE,
                 enabled: bool = True):
        self._rng = random.Random(seed)
        self.rate = rate
        self.enabled = enabled
        self._state: dict[frozenset[int], bool] = {}

    def factor_for(self, members: frozenset[int]) -> float:
        """Externality multiplier for the pairing, drawing once per pairing."""
        if not self.enabled or len(members) < 2:
            return FAST_PAIRING_EXTERNALITY
        if members not in self._state:
            self._state[members] = self._rng.random() < self.rate
        return (FAST_PAIRING_EXTERNALITY if self._state[members]
                else SLOW_PAIRING_EXTERNALITY)

    def is_fast(self, members: frozenset[int]) -> bool | None:
        return self._state.get(members)

    def forget(self, members: frozenset[int]) -> None:
        """Re-form the pairing: the next draw is independent."""
        self._state.pop(members, None)


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
    deadline_override_rounds: int = 0
    exclusive_rounds: int = 0

    def canonical_bytes(self) -> bytes:
        """Byte-exact serialisation of everything the gate is judged on.

        Gate C asks for identical results from an identical seed, and
        "identical" has to mean bytes rather than a rounded summary --
        a scheduler whose decisions drift in the last place would pass a
        comparison at four decimals and be non-deterministic anyway.

        Floats go out as ``float.hex()``, which is exact and round-trips;
        ``repr`` round-trips too but its shortest-representation rules are
        a promise about Python's formatter, not about the value. Dicts are
        emitted in sorted key order because insertion order here follows
        arrival order, which is a property of the trace and not of the
        result.
        """
        def num(value: float) -> str:
            return float(value).hex()

        lines = [
            b"burstserve.trace_sim.SimulationResult\x00v1",
            f"horizon_s={num(self.horizon_s)}".encode(),
            f"quantum_s={num(self.quantum_s)}".encode(),
            f"steps_executed={self.steps_executed}".encode(),
            f"predictor_relative_error={num(self.predictor_relative_error)}"
            .encode(),
            f"granted_unit_seconds={num(self.granted_unit_seconds)}".encode(),
            f"peak_lag_unit_seconds={num(self.peak_lag_unit_seconds)}"
            .encode(),
            f"deadline_override_rounds={self.deadline_override_rounds}"
            .encode(),
            f"exclusive_rounds={self.exclusive_rounds}".encode(),
        ]
        for tenant in sorted(self.quota_seconds_by_tenant):
            lines.append(
                f"quota:{tenant}="
                f"{num(self.quota_seconds_by_tenant[tenant])}".encode()
            )
        for tenant in sorted(self.service_seconds_by_tenant):
            lines.append(
                f"service:{tenant}="
                f"{num(self.service_seconds_by_tenant[tenant])}".encode()
            )
        for label, states in (("done", self.completed),
                              ("open", self.unfinished)):
            for state in sorted(states, key=lambda s: s.request.request_id):
                finished = (
                    num(state.finished_s)
                    if state.finished_s is not None else "-"
                )
                started = (
                    num(state.started_s)
                    if state.started_s is not None else "-"
                )
                lines.append(
                    f"{label}:{state.request.request_id}"
                    f":{state.request.tenant}"
                    f":{state.request.model}"
                    f":steps={state.steps_done}/{state.request.steps}"
                    f":start={started}:end={finished}"
                    f":quota={num(state.quota_seconds)}"
                    f":service={num(state.service_seconds)}".encode()
                )
        for left, right in sorted(self.unmeasured_pairings):
            lines.append(f"unmeasured:{left}+{right}".encode())
        return b"\n".join(lines)

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

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
    pairing_states: "PairingStates | None" = None,
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
    if pairing_states is None:
        pairing_states = PairingStates(seed=seed)

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
    deadline_override_rounds = 0
    exclusive_rounds = 0

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
        members = frozenset(rid for rid, _ in active)
        # Pairings that are no longer formed are forgotten, so re-forming
        # one draws a fresh state. That is the whole basis of probing: the
        # hardware draws again when the processes are relaunched.
        for stale in [m for m in list(pairing_states._state) if m != members]:
            pairing_states.forget(stale)
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
                if peer is not None:
                    # The table entry is the fast state. Which state this
                    # pairing is actually in is drawn per pairing.
                    factor = pairing_states.factor_for(members)
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
            # What the scheduler can observe after the fact, which is how a
            # probe works: it does not read the state, it reads the step it
            # just paid for.
            state.observed_step_seconds = step_cost
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
        # Classify the round independently of what the policy says it did.
        # Gate C exempts deadline overrides from the lag bound, so a policy
        # that self-reported its overrides could exempt itself from the
        # bound by relabelling. The conditions are recomputed here from the
        # same predictions the policy saw.
        if len(runnable) > 1 and len(granted) == 1:
            exclusive_rounds += 1
            (chosen_id,) = granted
            chosen = next(
                s for s in runnable if s.request.request_id == chosen_id
            )
            half = units // 2
            deadline = chosen.request.deadline_s
            if deadline is not None:
                left = deadline - now
                shared_cost = chosen.predicted_step_seconds.get(half)
                whole_cost = chosen.predicted_step_seconds.get(units)
                todo = chosen.request.steps - chosen.steps_done
                if (shared_cost is not None and whole_cost is not None
                        and todo * shared_cost > left
                        and todo * whole_cost <= left):
                    deadline_override_rounds += 1

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
        deadline_override_rounds=deadline_override_rounds,
        exclusive_rounds=exclusive_rounds,
    )
