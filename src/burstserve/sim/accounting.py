"""Atomic, exact fairness and resource accounting for the CPU simulator.

The public transitions in this module preserve a strict invariant:
``TenantState.active`` is exactly the set of tenants with non-terminal
requests.  New arrivals wake a tenant with bounded credit, and a quantum that
finishes its last request atomically removes it from the active-weight sum.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
from typing import Any, Iterable

from .model import (
    Action,
    ExactRatio,
    GlobalFairState,
    NANOSECONDS_PER_SECOND,
    QuantumResult,
    RawResourceUsage,
    RequestState,
    ResourceCapacities,
    ResourceTimeVector,
    SchedulerState,
    TenantLedger,
    TenantPolicy,
    TenantState,
    canonical_json,
)
from .protocols import ProfileProvider


RESOURCE_DEBT_DECAY_SCHEMA_VERSION = "resource-debt-fixed-v2"
_Q64 = 1 << 64
# ceil(exp(-1 ms / 60 s) * 2**64), generated once with an 80-digit Decimal
# context. Runtime code never evaluates exp or uses binary floating point.
_DEFAULT_Q64_DECAY_NUMERATOR = 18_446_436_630_537_023_345


def _integer(value: Any, *, field_name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    if value < minimum:
        raise ValueError(f"{field_name} must be at least {minimum}")
    return value


def _replace_tenant_tuple(
    tenants: tuple[TenantState, ...],
    replacements: dict[str, TenantState],
) -> tuple[TenantState, ...]:
    known = {tenant.tenant_id for tenant in tenants}
    unknown = set(replacements) - known
    if unknown:
        raise KeyError(f"unknown tenant replacements: {sorted(unknown)}")
    return tuple(
        replacements.get(tenant.tenant_id, tenant) for tenant in tenants
    )


def _replace_request_tuple(
    requests: tuple[RequestState, ...],
    replacements: dict[str, RequestState],
) -> tuple[RequestState, ...]:
    known = {request.spec.request_id for request in requests}
    unknown = set(replacements) - known
    if unknown:
        raise KeyError(f"unknown request replacements: {sorted(unknown)}")
    return tuple(
        replacements.get(request.spec.request_id, request)
        for request in requests
    )


def register_tenant(
    state: SchedulerState,
    policy: TenantPolicy,
) -> SchedulerState:
    """Register one sleeping tenant at zero lag without fabricating service."""

    if not isinstance(state, SchedulerState):
        raise TypeError("state must be a SchedulerState")
    if not isinstance(policy, TenantPolicy):
        raise TypeError("policy must be a TenantPolicy")
    try:
        existing = state.tenant(policy.tenant_id)
    except KeyError:
        coordinate = policy.weight * state.global_fair.virtual_time
        tenant = TenantState(
            policy=policy,
            ledger=TenantLedger(
                tenant_id=policy.tenant_id,
                fair_service_coordinate=coordinate,
            ),
            active=False,
        )
        return SchedulerState(
            global_fair=state.global_fair,
            current_time_ns=state.current_time_ns,
            tenants=state.tenants + (tenant,),
            requests=state.requests,
        )
    if existing.policy != policy:
        raise ValueError(
            f"tenant {policy.tenant_id!r} cannot change weight after registration"
        )
    return state


def bounded_sleeper_credit(
    requested_credit_ns: int,
    maximum_credit_ns: int,
) -> int:
    requested = _integer(
        requested_credit_ns,
        field_name="requested_credit_ns",
    )
    maximum = _integer(
        maximum_credit_ns,
        field_name="maximum_credit_ns",
    )
    return min(requested, maximum)


def _wake_coordinate(
    state: SchedulerState,
    tenant: TenantState,
    maximum_credit_ns: int,
) -> ExactRatio:
    maximum = _integer(
        maximum_credit_ns,
        field_name="maximum_credit_ns",
    )
    zero_lag = tenant.policy.weight * state.global_fair.virtual_time
    credit_floor = max(ExactRatio(), zero_lag - ExactRatio(maximum))
    return max(tenant.ledger.fair_service_coordinate, credit_floor)


def wake_tenant(
    state: SchedulerState,
    tenant_id: str,
    request: RequestState,
    *,
    maximum_credit_ns: int,
) -> SchedulerState:
    """Atomically add a new backlogged request and wake its sleeping tenant."""

    if not isinstance(state, SchedulerState):
        raise TypeError("state must be a SchedulerState")
    if not isinstance(request, RequestState):
        raise TypeError("request must be a RequestState")
    tenant = state.tenant(tenant_id)
    if tenant.active:
        raise ValueError(f"tenant {tenant_id!r} is already active")
    if request.spec.tenant_id != tenant_id:
        raise ValueError("request and tenant IDs must match")
    if not request.is_backlogged:
        raise ValueError("wake requires a non-terminal request")
    if request.spec.arrival_ns < state.current_time_ns:
        raise ValueError(
            "request arrival cannot regress scheduler current_time_ns"
        )
    try:
        state.request(request.spec.request_id)
    except KeyError:
        pass
    else:
        raise ValueError(
            f"request ID {request.spec.request_id!r} is already registered"
        )
    ledger = replace(
        tenant.ledger,
        fair_service_coordinate=_wake_coordinate(
            state,
            tenant,
            maximum_credit_ns,
        ),
    )
    replacement = replace(tenant, ledger=ledger, active=True)
    return SchedulerState(
        global_fair=state.global_fair,
        current_time_ns=request.spec.arrival_ns,
        tenants=_replace_tenant_tuple(
            state.tenants,
            {tenant_id: replacement},
        ),
        requests=state.requests + (request,),
    )


def register_request(
    state: SchedulerState,
    request: RequestState,
    *,
    maximum_credit_ns: int,
) -> SchedulerState:
    """Register an arrival; sleeping tenants must wake through the credit cap."""

    if not isinstance(state, SchedulerState):
        raise TypeError("state must be a SchedulerState")
    if not isinstance(request, RequestState):
        raise TypeError("request must be a RequestState")
    tenant = state.tenant(request.spec.tenant_id)
    try:
        existing = state.request(request.spec.request_id)
    except KeyError:
        pass
    else:
        if existing != request:
            raise ValueError(
                f"request ID {request.spec.request_id!r} is already registered"
            )
        return state
    if request.spec.arrival_ns < state.current_time_ns:
        raise ValueError(
            "request arrival cannot regress scheduler current_time_ns"
        )
    if request.is_backlogged and not tenant.active:
        return wake_tenant(
            state,
            tenant.tenant_id,
            request,
            maximum_credit_ns=maximum_credit_ns,
        )
    return SchedulerState(
        global_fair=state.global_fair,
        current_time_ns=request.spec.arrival_ns,
        tenants=state.tenants,
        requests=state.requests + (request,),
    )


def reject_request(
    state: SchedulerState,
    request_id: str,
    *,
    at_ns: int,
) -> SchedulerState:
    """Atomically reject a request and sleep its tenant if backlog becomes empty."""

    rejected_at = _integer(at_ns, field_name="at_ns")
    if rejected_at < state.current_time_ns:
        raise ValueError("rejection time cannot regress scheduler current_time_ns")
    request = state.request(request_id)
    if request.is_terminal:
        raise ValueError("terminal requests cannot be rejected again")
    rejected = replace(request, status="rejected")
    requests = _replace_request_tuple(
        state.requests,
        {request_id: rejected},
    )
    backlogged = {
        item.spec.tenant_id for item in requests if item.is_backlogged
    }
    tenants = tuple(
        replace(tenant, active=tenant.tenant_id in backlogged)
        for tenant in state.tenants
    )
    return SchedulerState(
        global_fair=state.global_fair,
        current_time_ns=rejected_at,
        tenants=tenants,
        requests=requests,
    )


def service_lag(state: SchedulerState, tenant_id: str) -> ExactRatio:
    tenant = state.tenant(tenant_id)
    return (
        tenant.policy.weight * state.global_fair.virtual_time
        - tenant.ledger.fair_service_coordinate
    )


@dataclass(frozen=True, slots=True)
class RequestCanonicalCharge:
    request_id: str
    tenant_id: str
    step_indices: tuple[int, ...]
    canonical_charge_ns: int

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, str) or not self.request_id.strip():
            raise ValueError("request_id must be non-empty")
        if not isinstance(self.tenant_id, str) or not self.tenant_id.strip():
            raise ValueError("tenant_id must be non-empty")
        if not isinstance(self.step_indices, tuple) or not self.step_indices:
            raise ValueError("step_indices must be a non-empty tuple")
        if tuple(sorted(set(self.step_indices))) != self.step_indices:
            raise ValueError("step_indices must be strictly increasing")
        _integer(
            self.canonical_charge_ns,
            field_name="canonical_charge_ns",
            minimum=1,
        )

    def to_key(self) -> dict[str, Any]:
        return {
            "canonical_charge_ns": self.canonical_charge_ns,
            "request_id": self.request_id,
            "step_indices": list(self.step_indices),
            "tenant_id": self.tenant_id,
        }


@dataclass(frozen=True, slots=True)
class QuantumCanonicalAccountingResult:
    state_before: SchedulerState
    state_after: SchedulerState
    source: str
    action_id: str | None
    quantum_result_id: str | None
    completion_time_ns: int
    charges: tuple[RequestCanonicalCharge, ...]
    start_active_tenant_ids: tuple[str, ...]
    total_canonical_charge_ns: int

    def __post_init__(self) -> None:
        if not isinstance(self.state_before, SchedulerState):
            raise TypeError("state_before must be a SchedulerState")
        if not isinstance(self.state_after, SchedulerState):
            raise TypeError("state_after must be a SchedulerState")
        if self.source not in {"standalone", "quantum"}:
            raise ValueError("canonical evidence source must be standalone/quantum")
        if self.source == "standalone":
            if self.action_id is not None or self.quantum_result_id is not None:
                raise ValueError(
                    "standalone canonical evidence cannot claim quantum IDs"
                )
        else:
            for name, value in (
                ("action_id", self.action_id),
                ("quantum_result_id", self.quantum_result_id),
            ):
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(
                        f"quantum canonical evidence requires {name}"
                    )
        completed_at = _integer(
            self.completion_time_ns,
            field_name="completion_time_ns",
        )
        if completed_at < self.state_before.current_time_ns:
            raise ValueError(
                "canonical evidence time regresses state_before watermark"
            )
        if not isinstance(self.charges, tuple):
            raise TypeError("charges must be a tuple")
        if not all(isinstance(item, RequestCanonicalCharge) for item in self.charges):
            raise TypeError("charges must contain RequestCanonicalCharge values")
        if tuple(sorted(self.charges, key=lambda item: item.request_id)) != self.charges:
            raise ValueError("charges must be sorted by request_id")
        if len({item.request_id for item in self.charges}) != len(self.charges):
            raise ValueError("charges contains a duplicate request")
        if not isinstance(self.start_active_tenant_ids, tuple):
            raise TypeError("start_active_tenant_ids must be a tuple")
        if tuple(sorted(set(self.start_active_tenant_ids))) != (
            self.start_active_tenant_ids
        ):
            raise ValueError("start active tenant IDs must be sorted and unique")
        expected_active = tuple(
            tenant.tenant_id
            for tenant in self.state_before.tenants
            if tenant.active
        )
        if self.start_active_tenant_ids != expected_active:
            raise ValueError(
                "canonical start_active_tenant_ids must exactly match "
                "state_before"
            )
        total = _integer(
            self.total_canonical_charge_ns,
            field_name="total_canonical_charge_ns",
        )
        if sum(item.canonical_charge_ns for item in self.charges) != total:
            raise ValueError("canonical charge total is not conserved")

        active_ids = set(expected_active)
        request_replacements: dict[str, RequestState] = {}
        tenant_charges: dict[str, int] = {}
        for charge in self.charges:
            try:
                request = self.state_before.request(charge.request_id)
            except KeyError as error:
                raise ValueError(
                    "canonical charge references an unknown request"
                ) from error
            if request.spec.tenant_id != charge.tenant_id:
                raise ValueError("canonical charge tenant does not own request")
            if charge.tenant_id not in active_ids:
                raise ValueError("canonical charge tenant was not start-active")
            if request.status not in {"runnable", "running"}:
                raise ValueError(
                    "canonical charge requires runnable/running request"
                )
            expected_indices = tuple(
                range(
                    request.completed_steps,
                    request.completed_steps + len(charge.step_indices),
                )
            )
            if charge.step_indices != expected_indices:
                raise ValueError(
                    "canonical charge step indices do not match request progress"
                )
            new_completed = request.completed_steps + len(charge.step_indices)
            if new_completed > request.spec.signature.total_steps:
                raise ValueError("canonical charge exceeds request total steps")
            request_replacements[charge.request_id] = replace(
                request,
                completed_steps=new_completed,
                status=(
                    "completed"
                    if new_completed == request.spec.signature.total_steps
                    else "runnable"
                ),
                last_progress_ns=completed_at,
            )
            tenant_charges[charge.tenant_id] = (
                tenant_charges.get(charge.tenant_id, 0)
                + charge.canonical_charge_ns
            )

        requests_after = _replace_request_tuple(
            self.state_before.requests,
            request_replacements,
        )
        backlogged_after = {
            request.spec.tenant_id
            for request in requests_after
            if request.is_backlogged
        }
        tenant_replacements: dict[str, TenantState] = {}
        for tenant in self.state_before.tenants:
            charge_ns = tenant_charges.get(tenant.tenant_id, 0)
            ledger = tenant.ledger
            if charge_ns:
                ledger = replace(
                    ledger,
                    canonical_service_ns=ledger.canonical_service_ns + charge_ns,
                    fair_service_coordinate=(
                        ledger.fair_service_coordinate + ExactRatio(charge_ns)
                    ),
                    last_active_ns=completed_at,
                )
            tenant_replacements[tenant.tenant_id] = replace(
                tenant,
                ledger=ledger,
                active=tenant.tenant_id in backlogged_after,
            )
        global_after = self.state_before.global_fair
        if total:
            active_weight = sum(
                (
                    self.state_before.tenant(tenant_id).policy.weight
                    for tenant_id in expected_active
                ),
                ExactRatio(),
            )
            global_after = GlobalFairState(
                self.state_before.global_fair.virtual_time
                + ExactRatio(total) / active_weight
            )
        expected_state_after = SchedulerState(
            global_fair=global_after,
            current_time_ns=completed_at,
            tenants=_replace_tenant_tuple(
                self.state_before.tenants,
                tenant_replacements,
            ),
            requests=requests_after,
        )
        if self.state_after != expected_state_after:
            raise ValueError(
                "canonical state delta does not match charges/lifecycle/global time"
            )

    def to_key(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "charges": [item.to_key() for item in self.charges],
            "completion_time_ns": self.completion_time_ns,
            "quantum_result_id": self.quantum_result_id,
            "source": self.source,
            "start_active_tenant_ids": list(self.start_active_tenant_ids),
            "state_after_id": self.state_after.stable_id,
            "state_before_id": self.state_before.stable_id,
            "total_canonical_charge_ns": self.total_canonical_charge_ns,
        }


@dataclass(frozen=True, slots=True)
class CanonicalAccountingResult:
    """Compatibility view for a one-request atomic quantum."""

    quantum: QuantumCanonicalAccountingResult
    request_before: RequestState
    request_after: RequestState

    @property
    def state_before(self) -> SchedulerState:
        return self.quantum.state_before

    @property
    def state_after(self) -> SchedulerState:
        return self.quantum.state_after

    @property
    def charged_step_indices(self) -> tuple[int, ...]:
        return self.quantum.charges[0].step_indices

    @property
    def canonical_charge_ns(self) -> int:
        return self.quantum.total_canonical_charge_ns

    @property
    def ledger_before(self) -> TenantLedger:
        return self.state_before.tenant(
            self.request_before.spec.tenant_id
        ).ledger

    @property
    def ledger_after(self) -> TenantLedger:
        return self.state_after.tenant(
            self.request_after.spec.tenant_id
        ).ledger


def _account_quantum_completed_steps(
    state: SchedulerState,
    completed_steps: tuple[tuple[str, int], ...],
    *,
    completion_time_ns: int,
    profile: ProfileProvider,
    source: str,
    action_id: str | None,
    quantum_result_id: str | None,
) -> QuantumCanonicalAccountingResult:
    """Atomically charge completed work using the quantum-start active set.

    An empty completion tuple is a valid zero-progress canonical observation:
    no request, fairness coordinate, or lifecycle state changes, but the
    returned evidence still records the quantum-start active set.
    """

    if not isinstance(state, SchedulerState):
        raise TypeError("state must be a SchedulerState")
    if not isinstance(completed_steps, tuple):
        raise TypeError("completed_steps must be a tuple")
    normalized: list[tuple[str, int]] = []
    for item in completed_steps:
        if not isinstance(item, tuple) or len(item) != 2:
            raise TypeError("completed_steps entries must be (request_id, count)")
        request_id, count = item
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("completed request_id must be non-empty")
        normalized.append(
            (
                request_id,
                _integer(count, field_name="completed step count", minimum=1),
            )
        )
    if len({request_id for request_id, _ in normalized}) != len(normalized):
        raise ValueError("completed_steps contains a duplicate request")
    normalized.sort()
    completed_at = _integer(
        completion_time_ns,
        field_name="completion_time_ns",
    )
    if completed_at < state.current_time_ns:
        raise ValueError(
            "completion time cannot regress scheduler current_time_ns"
        )
    if not isinstance(profile, ProfileProvider):
        raise TypeError("profile must implement ProfileProvider")

    start_active = tuple(tenant for tenant in state.tenants if tenant.active)
    if normalized and not start_active:
        raise ValueError("completed work requires an active backlog")
    active_ids = {tenant.tenant_id for tenant in start_active}
    active_weights = sum(
        (tenant.policy.weight for tenant in start_active),
        ExactRatio(),
    )

    request_replacements: dict[str, RequestState] = {}
    tenant_charges: dict[str, int] = {}
    charges: list[RequestCanonicalCharge] = []
    for request_id, count in normalized:
        request = state.request(request_id)
        tenant = state.tenant(request.spec.tenant_id)
        if tenant.tenant_id not in active_ids:
            raise ValueError("completed request tenant was not active at quantum start")
        if request.status in {"queued", "suspended", "completed", "rejected"}:
            raise ValueError(
                f"request status {request.status!r} cannot complete a quantum"
            )
        if count > request.remaining_steps:
            raise ValueError("completed steps exceed request.remaining_steps")
        if completed_at < request.spec.arrival_ns:
            raise ValueError("completion time cannot precede request arrival")
        if (
            request.last_progress_ns is not None
            and completed_at < request.last_progress_ns
        ):
            raise ValueError("completion time cannot precede prior progress")
        if (
            tenant.ledger.last_active_ns is not None
            and completed_at < tenant.ledger.last_active_ns
        ):
            raise ValueError("completion time cannot precede tenant activity")
        step_indices = tuple(
            range(request.completed_steps, request.completed_steps + count)
        )
        charge = 0
        for step_index in step_indices:
            value = profile.canonical_step_ns(
                request.spec.signature,
                step_index,
            )
            charge += _integer(
                value,
                field_name="profile canonical_step_ns result",
                minimum=1,
            )
        new_completed = request.completed_steps + count
        request_replacements[request_id] = replace(
            request,
            completed_steps=new_completed,
            status=(
                "completed"
                if new_completed == request.spec.signature.total_steps
                else "runnable"
            ),
            last_progress_ns=completed_at,
        )
        tenant_charges[tenant.tenant_id] = (
            tenant_charges.get(tenant.tenant_id, 0) + charge
        )
        charges.append(
            RequestCanonicalCharge(
                request_id=request_id,
                tenant_id=tenant.tenant_id,
                step_indices=step_indices,
                canonical_charge_ns=charge,
            )
        )

    requests_after = _replace_request_tuple(
        state.requests,
        request_replacements,
    )
    backlogged_after = {
        request.spec.tenant_id
        for request in requests_after
        if request.is_backlogged
    }
    tenant_replacements: dict[str, TenantState] = {}
    for tenant in state.tenants:
        charge = tenant_charges.get(tenant.tenant_id, 0)
        ledger = tenant.ledger
        if charge:
            ledger = replace(
                ledger,
                canonical_service_ns=ledger.canonical_service_ns + charge,
                fair_service_coordinate=(
                    ledger.fair_service_coordinate + ExactRatio(charge)
                ),
                last_active_ns=completed_at,
            )
        tenant_replacements[tenant.tenant_id] = replace(
            tenant,
            ledger=ledger,
            active=tenant.tenant_id in backlogged_after,
        )

    total_charge = sum(item.canonical_charge_ns for item in charges)
    global_after = state.global_fair
    if total_charge:
        global_after = GlobalFairState(
            state.global_fair.virtual_time
            + ExactRatio(total_charge) / active_weights
        )
    state_after = SchedulerState(
        global_fair=global_after,
        current_time_ns=completed_at,
        tenants=_replace_tenant_tuple(
            state.tenants,
            tenant_replacements,
        ),
        requests=requests_after,
    )
    return QuantumCanonicalAccountingResult(
        state_before=state,
        state_after=state_after,
        source=source,
        action_id=action_id,
        quantum_result_id=quantum_result_id,
        completion_time_ns=completed_at,
        charges=tuple(sorted(charges, key=lambda item: item.request_id)),
        start_active_tenant_ids=tuple(
            sorted(tenant.tenant_id for tenant in start_active)
        ),
        total_canonical_charge_ns=total_charge,
    )


def account_quantum_completed_steps(
    state: SchedulerState,
    completed_steps: tuple[tuple[str, int], ...],
    *,
    completion_time_ns: int,
    profile: ProfileProvider,
) -> QuantumCanonicalAccountingResult:
    """Create explicitly standalone canonical-service evidence."""

    return _account_quantum_completed_steps(
        state,
        completed_steps,
        completion_time_ns=completion_time_ns,
        profile=profile,
        source="standalone",
        action_id=None,
        quantum_result_id=None,
    )


def account_completed_steps(
    state: SchedulerState,
    request_id: str,
    *,
    completed_steps: int,
    completion_time_ns: int,
    profile: ProfileProvider,
) -> CanonicalAccountingResult:
    request_before = state.request(request_id)
    quantum = account_quantum_completed_steps(
        state,
        ((request_id, completed_steps),),
        completion_time_ns=completion_time_ns,
        profile=profile,
    )
    return CanonicalAccountingResult(
        quantum=quantum,
        request_before=request_before,
        request_after=quantum.state_after.request(request_id),
    )


def compare_virtual_service(
    left_ledger: TenantLedger,
    left_policy: TenantPolicy,
    right_ledger: TenantLedger,
    right_policy: TenantPolicy,
) -> int:
    left = left_ledger.virtual_service(left_policy)
    right = right_ledger.virtual_service(right_policy)
    return (left > right) - (left < right)


def least_served_tenant(
    candidates: Iterable[TenantState],
) -> TenantState:
    materialized = tuple(candidates)
    if not materialized:
        raise ValueError("candidates must be non-empty")
    if not all(isinstance(item, TenantState) for item in materialized):
        raise TypeError("candidates must contain TenantState values")
    selected = materialized[0]
    for candidate in materialized[1:]:
        comparison = compare_virtual_service(
            candidate.ledger,
            candidate.policy,
            selected.ledger,
            selected.policy,
        )
        if comparison < 0 or (
            comparison == 0 and candidate.tenant_id < selected.tenant_id
        ):
            selected = candidate
    return selected


def validate_quantum_result(
    state: SchedulerState,
    action: Action,
    result: QuantumResult,
    *,
    capacities: ResourceCapacities | None = None,
) -> tuple[str, ...]:
    """Bind a result to its state/action and validate complete attribution.

    When capacities are supplied, ``sm_ns`` is checked as an upper-bounded
    assigned/occupied SM-time observation.  It may be lower than the action
    quota because assigned capacity can idle, but it may not exceed either the
    whole-device time budget or the participant tenant's summed quota budget.
    """

    if not isinstance(state, SchedulerState):
        raise TypeError("state must be a SchedulerState")
    if not isinstance(action, Action):
        raise TypeError("action must be an Action")
    if not isinstance(result, QuantumResult):
        raise TypeError("result must be a QuantumResult")
    if result.action_id != action.action_id:
        raise ValueError("quantum result action_id does not match the action")
    if result.started_ns < state.current_time_ns:
        raise ValueError(
            "quantum start cannot regress scheduler current_time_ns"
        )
    if capacities is not None and not isinstance(
        capacities,
        ResourceCapacities,
    ):
        raise TypeError("capacities must be ResourceCapacities")
    if action.allocations and result.elapsed_ns == 0:
        raise ValueError("a non-empty action requires positive elapsed_ns")
    if not result.success and result.completed_steps:
        raise ValueError("a failed quantum cannot report completed steps")

    start_active = tuple(tenant for tenant in state.tenants if tenant.active)
    for tenant in start_active:
        for field_name, timestamp in (
            ("last_active_ns", tenant.ledger.last_active_ns),
            (
                "resource_debt_updated_ns",
                tenant.ledger.resource_debt_updated_ns,
            ),
        ):
            if timestamp is not None and result.started_ns < timestamp:
                raise ValueError(
                    f"quantum start precedes active tenant {field_name}"
                )
        for request in state.requests:
            if (
                request.spec.tenant_id != tenant.tenant_id
                or not request.is_backlogged
            ):
                continue
            if result.started_ns < request.spec.arrival_ns:
                raise ValueError(
                    "quantum start precedes an active request arrival"
                )
            if (
                request.last_progress_ns is not None
                and result.started_ns < request.last_progress_ns
            ):
                raise ValueError(
                    "quantum start precedes active request progress"
                )

    allocations = {
        allocation.request_id: allocation for allocation in action.allocations
    }
    participant_tenants: set[str] = set()
    quota_by_tenant: dict[str, ExactRatio] = {}
    for request_id in allocations:
        try:
            request = state.request(request_id)
        except KeyError as error:
            raise ValueError(
                f"action allocates unknown request {request_id!r}"
            ) from error
        tenant = state.tenant(request.spec.tenant_id)
        if request.status not in {"runnable", "running"} or not tenant.active:
            raise ValueError(
                "compute allocations require active runnable/running requests"
            )
        if result.started_ns < request.spec.arrival_ns:
            raise ValueError("quantum cannot start before request arrival")
        if (
            request.last_progress_ns is not None
            and result.started_ns < request.last_progress_ns
        ):
            raise ValueError("quantum cannot start before prior request progress")
        participant_tenants.add(tenant.tenant_id)
        quota_by_tenant[tenant.tenant_id] = (
            quota_by_tenant.get(tenant.tenant_id, ExactRatio())
            + allocations[request_id].quota
        )

    for request_id, count in result.completed_steps:
        if request_id not in allocations:
            raise ValueError("completed request is absent from action allocations")
        allocation = allocations[request_id]
        if count > allocation.quantum_steps:
            raise ValueError("completed steps exceed the allocated quantum")
        if count > state.request(request_id).remaining_steps:
            raise ValueError("completed steps exceed request.remaining_steps")

    attributed_tenants = {
        item.tenant_id for item in result.resource_usage_by_tenant
    }
    if attributed_tenants != participant_tenants:
        missing = sorted(participant_tenants - attributed_tenants)
        extra = sorted(attributed_tenants - participant_tenants)
        raise ValueError(
            "resource attribution must contain every and only action "
            f"participant tenant; missing={missing}, extra={extra}"
        )
    if capacities is not None:
        device_budget_sm_ns = capacities.total_sms * result.elapsed_ns
        if result.total_resource_usage.sm_ns > device_budget_sm_ns:
            raise ValueError(
                "total sm_ns exceeds total_sms times quantum elapsed_ns"
            )
        for item in result.resource_usage_by_tenant:
            quota_budget = (
                ExactRatio(device_budget_sm_ns)
                * quota_by_tenant[item.tenant_id]
            )
            if ExactRatio(item.usage.sm_ns) > quota_budget:
                raise ValueError(
                    f"tenant {item.tenant_id!r} sm_ns exceeds its action "
                    "quota-time upper bound"
                )
    return tuple(sorted(participant_tenants))


def normalize_resource_usage(
    usage: RawResourceUsage,
    capacities: ResourceCapacities,
) -> ResourceTimeVector:
    if not isinstance(usage, RawResourceUsage):
        raise TypeError("usage must be a RawResourceUsage")
    if not isinstance(capacities, ResourceCapacities):
        raise TypeError("capacities must be ResourceCapacities")
    return ResourceTimeVector(
        compute_ns=ExactRatio(usage.sm_ns, capacities.total_sms),
        hbm_ns=ExactRatio(
            usage.hbm_bytes * NANOSECONDS_PER_SECOND,
            capacities.hbm_bytes_per_second,
        ),
        pcie_h2d_ns=ExactRatio(
            usage.pcie_h2d_bytes * NANOSECONDS_PER_SECOND,
            capacities.pcie_h2d_bytes_per_second,
        ),
        pcie_d2h_ns=ExactRatio(
            usage.pcie_d2h_bytes * NANOSECONDS_PER_SECOND,
            capacities.pcie_d2h_bytes_per_second,
        ),
    )


def resource_entitlement(
    elapsed_ns: int,
    policy: TenantPolicy,
    active_policies: Iterable[TenantPolicy],
) -> ResourceTimeVector:
    elapsed = _integer(elapsed_ns, field_name="elapsed_ns")
    if not isinstance(policy, TenantPolicy):
        raise TypeError("policy must be a TenantPolicy")
    active = tuple(active_policies)
    if not active:
        raise ValueError("active_policies must be non-empty")
    if not all(isinstance(item, TenantPolicy) for item in active):
        raise TypeError("active_policies must contain TenantPolicy values")
    tenant_ids = [item.tenant_id for item in active]
    if len(set(tenant_ids)) != len(tenant_ids):
        raise ValueError("active_policies contains a duplicate tenant")
    if policy.tenant_id not in tenant_ids:
        raise ValueError("policy must be present in active_policies")
    total_weight = sum((item.weight for item in active), ExactRatio())
    share_ns = ExactRatio(elapsed) * policy.weight / total_weight
    return ResourceTimeVector(
        compute_ns=share_ns,
        hbm_ns=share_ns,
        pcie_h2d_ns=share_ns,
        pcie_d2h_ns=share_ns,
    )


def _ceil_div(numerator: int, denominator: int) -> int:
    return (numerator + denominator - 1) // denominator


def _fixed_point_power_ceil(
    numerator: int,
    denominator: int,
    exponent: int,
) -> int:
    result = denominator
    base = numerator
    remaining = exponent
    while remaining:
        if remaining & 1:
            result = _ceil_div(result * base, denominator)
        remaining >>= 1
        if remaining:
            base = _ceil_div(base * base, denominator)
    return result


@dataclass(frozen=True, slots=True)
class DebtDecayPolicy:
    """Full identity of the deterministic conservative decay approximation."""

    ALGORITHM_VERSION = 2

    tick_ns: int = 1_000_000
    factor_numerator: int = _DEFAULT_Q64_DECAY_NUMERATOR
    factor_denominator: int = _Q64
    debt_scale_denominator: int = _Q64

    def __post_init__(self) -> None:
        _integer(self.tick_ns, field_name="tick_ns", minimum=1)
        numerator = _integer(
            self.factor_numerator,
            field_name="factor_numerator",
            minimum=1,
        )
        denominator = _integer(
            self.factor_denominator,
            field_name="factor_denominator",
            minimum=1,
        )
        _integer(
            self.debt_scale_denominator,
            field_name="debt_scale_denominator",
            minimum=1,
        )
        if numerator > denominator:
            raise ValueError("decay factor must not exceed one")

    @property
    def schema_version(self) -> str:
        return RESOURCE_DEBT_DECAY_SCHEMA_VERSION

    def to_key(self) -> dict[str, str | int]:
        return {
            "algorithm_version": self.ALGORITHM_VERSION,
            "debt_scale_denominator": self.debt_scale_denominator,
            "factor_denominator": self.factor_denominator,
            "factor_numerator": self.factor_numerator,
            "schema_version": self.schema_version,
            "tick_ns": self.tick_ns,
        }

    @property
    def policy_id(self) -> str:
        digest = sha256(canonical_json(self.to_key()).encode("utf-8")).hexdigest()
        return f"rdp{self.ALGORITHM_VERSION}-{digest}"

    def factor_for_elapsed(
        self,
        elapsed_ns: int,
        *,
        carried_remainder_ns: int = 0,
    ) -> tuple[ExactRatio, int, int]:
        elapsed = _integer(elapsed_ns, field_name="elapsed_ns")
        carried = _integer(
            carried_remainder_ns,
            field_name="carried_remainder_ns",
        )
        if carried >= self.tick_ns:
            raise ValueError("carried remainder must be smaller than one tick")
        ticks, remainder = divmod(elapsed + carried, self.tick_ns)
        powered = _fixed_point_power_ceil(
            self.factor_numerator,
            self.factor_denominator,
            ticks,
        )
        return ExactRatio(powered, self.factor_denominator), ticks, remainder

    def quantize_up(self, value: ExactRatio) -> ExactRatio:
        if not isinstance(value, ExactRatio):
            raise TypeError("value must be an ExactRatio")
        if value < 0:
            raise ValueError("debt values must be non-negative")
        scaled_numerator = value.numerator * self.debt_scale_denominator
        units = _ceil_div(scaled_numerator, value.denominator)
        return ExactRatio(units, self.debt_scale_denominator)

    def quantize_vector_up(
        self,
        value: ResourceTimeVector,
    ) -> ResourceTimeVector:
        if not isinstance(value, ResourceTimeVector):
            raise TypeError("value must be a ResourceTimeVector")
        return ResourceTimeVector(
            **{
                name: self.quantize_up(getattr(value, name))
                for name in ResourceTimeVector.COMPONENTS
            }
        )


@dataclass(frozen=True, slots=True)
class ResourceDebtUpdate:
    before: ResourceTimeVector
    usage: ResourceTimeVector
    entitlement: ResourceTimeVector
    decayed_before: ResourceTimeVector
    after: ResourceTimeVector
    decay_factor: ExactRatio
    decay_elapsed_ns: int
    entitlement_elapsed_ns: int
    carried_remainder_ns: int
    applied_ticks: int
    new_remainder_ns: int
    decay_policy: DebtDecayPolicy

    def __post_init__(self) -> None:
        for name in (
            "before",
            "usage",
            "entitlement",
            "decayed_before",
            "after",
        ):
            if not isinstance(getattr(self, name), ResourceTimeVector):
                raise TypeError(f"{name} must be a ResourceTimeVector")
        if not isinstance(self.decay_factor, ExactRatio):
            raise TypeError("decay_factor must be an ExactRatio")
        if not isinstance(self.decay_policy, DebtDecayPolicy):
            raise TypeError("decay_policy must be a DebtDecayPolicy")
        _integer(
            self.decay_elapsed_ns,
            field_name="decay_elapsed_ns",
        )
        _integer(
            self.entitlement_elapsed_ns,
            field_name="entitlement_elapsed_ns",
        )
        entitlement_values = tuple(
            getattr(self.entitlement, component)
            for component in ResourceTimeVector.COMPONENTS
        )
        if any(value != entitlement_values[0] for value in entitlement_values):
            raise ValueError(
                "resource entitlement must use one equal share in every "
                "resource component"
            )
        entitlement_limit = ExactRatio(self.entitlement_elapsed_ns)
        if any(value > entitlement_limit for value in entitlement_values):
            raise ValueError(
                "resource entitlement share exceeds entitlement_elapsed_ns"
            )
        factor, ticks, remainder = self.decay_policy.factor_for_elapsed(
            self.decay_elapsed_ns,
            carried_remainder_ns=self.carried_remainder_ns,
        )
        if (factor, ticks, remainder) != (
            self.decay_factor,
            self.applied_ticks,
            self.new_remainder_ns,
        ):
            raise ValueError("decay metadata does not match the full policy")
        expected_decayed = self.decay_policy.quantize_vector_up(
            self.before.scale(self.decay_factor)
        )
        if self.decayed_before != expected_decayed:
            raise ValueError("decayed_before does not match policy quantization")
        expected_after = self.decay_policy.quantize_vector_up(
            (self.decayed_before + self.usage).subtract_floor_zero(
                self.entitlement
            )
        )
        if self.after != expected_after:
            raise ValueError("resource debt update does not match exact equation")

    @property
    def decay_policy_id(self) -> str:
        return self.decay_policy.policy_id

    def to_key(self) -> dict[str, Any]:
        return {
            "after": self.after.to_key(),
            "applied_ticks": self.applied_ticks,
            "before": self.before.to_key(),
            "carried_remainder_ns": self.carried_remainder_ns,
            "decay_factor": self.decay_factor.to_key(),
            "decay_policy": self.decay_policy.to_key(),
            "decay_policy_id": self.decay_policy_id,
            "decayed_before": self.decayed_before.to_key(),
            "decay_elapsed_ns": self.decay_elapsed_ns,
            "entitlement": self.entitlement.to_key(),
            "entitlement_elapsed_ns": self.entitlement_elapsed_ns,
            "new_remainder_ns": self.new_remainder_ns,
            "usage": self.usage.to_key(),
        }


def update_resource_debt(
    before: ResourceTimeVector,
    usage: ResourceTimeVector,
    entitlement: ResourceTimeVector,
    *,
    decay_elapsed_ns: int,
    entitlement_elapsed_ns: int,
    carried_remainder_ns: int = 0,
    decay_policy: DebtDecayPolicy | None = None,
) -> ResourceDebtUpdate:
    """Apply debt decay and one entitlement window as separate semantics.

    ``decay_elapsed_ns`` is wall-clock time since the prior debt epoch and may
    include inactive/sleep time.  ``entitlement_elapsed_ns`` describes only
    the current active quantum used to construct ``entitlement``; it is
    evidence and never expands to cover an idle gap.
    """

    for name, value in (
        ("before", before),
        ("usage", usage),
        ("entitlement", entitlement),
    ):
        if not isinstance(value, ResourceTimeVector):
            raise TypeError(f"{name} must be a ResourceTimeVector")
    policy = DebtDecayPolicy() if decay_policy is None else decay_policy
    if not isinstance(policy, DebtDecayPolicy):
        raise TypeError("decay_policy must be a DebtDecayPolicy")
    decay_elapsed = _integer(
        decay_elapsed_ns,
        field_name="decay_elapsed_ns",
    )
    entitlement_elapsed = _integer(
        entitlement_elapsed_ns,
        field_name="entitlement_elapsed_ns",
    )
    factor, ticks, remainder = policy.factor_for_elapsed(
        decay_elapsed,
        carried_remainder_ns=carried_remainder_ns,
    )
    decayed = policy.quantize_vector_up(before.scale(factor))
    after = policy.quantize_vector_up(
        (decayed + usage).subtract_floor_zero(entitlement)
    )
    return ResourceDebtUpdate(
        before=before,
        usage=usage,
        entitlement=entitlement,
        decayed_before=decayed,
        after=after,
        decay_factor=factor,
        decay_elapsed_ns=decay_elapsed,
        entitlement_elapsed_ns=entitlement_elapsed,
        carried_remainder_ns=carried_remainder_ns,
        applied_ticks=ticks,
        new_remainder_ns=remainder,
        decay_policy=policy,
    )


@dataclass(frozen=True, slots=True)
class TenantQuantumDebtUpdate:
    tenant_id: str
    raw_usage: RawResourceUsage
    normalized_usage: ResourceTimeVector
    update: ResourceDebtUpdate

    def __post_init__(self) -> None:
        if not isinstance(self.tenant_id, str) or not self.tenant_id.strip():
            raise ValueError("tenant_id must be non-empty")
        if not isinstance(self.raw_usage, RawResourceUsage):
            raise TypeError("raw_usage must be RawResourceUsage")
        if not isinstance(self.normalized_usage, ResourceTimeVector):
            raise TypeError("normalized_usage must be ResourceTimeVector")
        if not isinstance(self.update, ResourceDebtUpdate):
            raise TypeError("update must be ResourceDebtUpdate")

    def to_key(self) -> dict[str, Any]:
        return {
            "normalized_usage": self.normalized_usage.to_key(),
            "raw_usage": self.raw_usage.to_key(),
            "tenant_id": self.tenant_id,
            "update": self.update.to_key(),
        }


@dataclass(frozen=True, slots=True)
class QuantumResourceAccountingResult:
    state_before: SchedulerState
    state_after: SchedulerState
    action_id: str
    quantum_result_id: str
    started_ns: int
    finished_ns: int
    capacities: ResourceCapacities
    decay_policy: DebtDecayPolicy
    start_active_tenant_ids: tuple[str, ...]
    tenant_updates: tuple[TenantQuantumDebtUpdate, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.state_before, SchedulerState):
            raise TypeError("state_before must be a SchedulerState")
        if not isinstance(self.state_after, SchedulerState):
            raise TypeError("state_after must be a SchedulerState")
        for name, value in (
            ("action_id", self.action_id),
            ("quantum_result_id", self.quantum_result_id),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-empty")
        started = _integer(self.started_ns, field_name="started_ns")
        finished = _integer(self.finished_ns, field_name="finished_ns")
        if started < self.state_before.current_time_ns:
            raise ValueError(
                "resource evidence start regresses state_before watermark"
            )
        if finished < started:
            raise ValueError("resource evidence finish precedes start")
        if not isinstance(self.capacities, ResourceCapacities):
            raise TypeError("capacities must be ResourceCapacities")
        if not isinstance(self.decay_policy, DebtDecayPolicy):
            raise TypeError("decay_policy must be DebtDecayPolicy")
        if not isinstance(self.start_active_tenant_ids, tuple):
            raise TypeError("start_active_tenant_ids must be a tuple")
        expected_active = tuple(
            tenant.tenant_id
            for tenant in self.state_before.tenants
            if tenant.active
        )
        if self.start_active_tenant_ids != expected_active:
            raise ValueError(
                "resource start_active_tenant_ids must exactly match state_before"
            )
        if not isinstance(self.tenant_updates, tuple):
            raise TypeError("tenant_updates must be a tuple")
        if not all(
            isinstance(update, TenantQuantumDebtUpdate)
            for update in self.tenant_updates
        ):
            raise TypeError(
                "tenant_updates must contain TenantQuantumDebtUpdate values"
            )
        if (
            tuple(update.tenant_id for update in self.tenant_updates)
            != expected_active
        ):
            raise ValueError(
                "every quantum-start active tenant must be updated exactly once"
            )
        elapsed_ns = finished - started
        active_policies = tuple(
            self.state_before.tenant(tenant_id).policy
            for tenant_id in expected_active
        )
        replacements: dict[str, TenantState] = {}
        for tenant_update in self.tenant_updates:
            tenant = self.state_before.tenant(tenant_update.tenant_id)
            ledger = tenant.ledger
            if tenant_update.normalized_usage != normalize_resource_usage(
                tenant_update.raw_usage,
                self.capacities,
            ):
                raise ValueError(
                    "resource normalized usage does not match raw/capacities"
                )
            expected_entitlement = resource_entitlement(
                elapsed_ns,
                tenant.policy,
                active_policies,
            )
            if tenant_update.update.entitlement != expected_entitlement:
                raise ValueError(
                    "resource entitlement does not match active weights/elapsed"
                )
            if tenant_update.update.entitlement_elapsed_ns != elapsed_ns:
                raise ValueError(
                    "resource entitlement window does not match quantum elapsed"
                )
            if tenant_update.update.before != ledger.resource_debt:
                raise ValueError("resource debt before does not match ledger")
            if tenant_update.update.usage != tenant_update.normalized_usage:
                raise ValueError("resource debt usage does not match normalization")
            if tenant_update.update.decay_policy != self.decay_policy:
                raise ValueError("resource update decay policy does not match evidence")
            if (
                ledger.resource_decay_policy_id is not None
                and ledger.resource_decay_policy_id
                != self.decay_policy.policy_id
            ):
                raise ValueError("resource evidence changes a bound decay policy")
            if ledger.resource_debt == ResourceTimeVector():
                expected_decay_elapsed = 0
                expected_remainder = 0
            else:
                if ledger.resource_debt_updated_ns is None:
                    raise ValueError("non-empty debt lacks an update epoch")
                expected_decay_elapsed = finished - ledger.resource_debt_updated_ns
                expected_remainder = ledger.resource_decay_remainder_ns
            expected_update = update_resource_debt(
                ledger.resource_debt,
                tenant_update.normalized_usage,
                expected_entitlement,
                decay_elapsed_ns=expected_decay_elapsed,
                entitlement_elapsed_ns=elapsed_ns,
                carried_remainder_ns=expected_remainder,
                decay_policy=self.decay_policy,
            )
            if tenant_update.update != expected_update:
                raise ValueError(
                    "resource debt update does not match epoch/remainder/equation"
                )
            replacements[tenant.tenant_id] = replace(
                tenant,
                ledger=replace(
                    ledger,
                    resource_debt=expected_update.after,
                    resource_decay_remainder_ns=(
                        expected_update.new_remainder_ns
                    ),
                    resource_decay_policy_id=self.decay_policy.policy_id,
                    resource_debt_updated_ns=finished,
                ),
            )
        if expected_active:
            for component in ResourceTimeVector.COMPONENTS:
                total_share = sum(
                    (
                        getattr(update.update.entitlement, component)
                        for update in self.tenant_updates
                    ),
                    ExactRatio(),
                )
                if total_share != ExactRatio(elapsed_ns):
                    raise ValueError(
                        "resource entitlement shares do not conserve elapsed time"
                    )
        expected_state_after = SchedulerState(
            global_fair=self.state_before.global_fair,
            current_time_ns=finished,
            tenants=_replace_tenant_tuple(
                self.state_before.tenants,
                replacements,
            ),
            requests=self.state_before.requests,
        )
        if self.state_after != expected_state_after:
            raise ValueError(
                "resource state delta does not match verified tenant updates"
            )

    @property
    def decay_policy_id(self) -> str:
        return self.decay_policy.policy_id

    def to_key(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "capacities": self.capacities.to_key(),
            "decay_policy": self.decay_policy.to_key(),
            "decay_policy_id": self.decay_policy_id,
            "finished_ns": self.finished_ns,
            "quantum_result_id": self.quantum_result_id,
            "started_ns": self.started_ns,
            "start_active_tenant_ids": list(self.start_active_tenant_ids),
            "state_after_id": self.state_after.stable_id,
            "state_before_id": self.state_before.stable_id,
            "tenant_updates": [item.to_key() for item in self.tenant_updates],
        }


def account_quantum_resource_usage(
    state: SchedulerState,
    action: Action,
    result: QuantumResult,
    capacities: ResourceCapacities,
    *,
    decay_policy: DebtDecayPolicy | None = None,
) -> QuantumResourceAccountingResult:
    """Update every quantum-start active tenant exactly once.

    Old debt decays lazily over wall-clock time since its persisted update
    epoch, including sleep.  Usage and entitlement cover only this quantum;
    inactive time therefore ages debt but never grants entitlement.
    """

    if not isinstance(capacities, ResourceCapacities):
        raise TypeError("capacities must be ResourceCapacities")
    validate_quantum_result(
        state,
        action,
        result,
        capacities=capacities,
    )
    policy = DebtDecayPolicy() if decay_policy is None else decay_policy
    if not isinstance(policy, DebtDecayPolicy):
        raise TypeError("decay_policy must be a DebtDecayPolicy")
    start_active = tuple(tenant for tenant in state.tenants if tenant.active)
    active_policies = tuple(tenant.policy for tenant in start_active)
    attributed = {
        item.tenant_id: item.usage
        for item in result.resource_usage_by_tenant
    }
    replacements: dict[str, TenantState] = {}
    updates: list[TenantQuantumDebtUpdate] = []
    for tenant in start_active:
        ledger = tenant.ledger
        if (
            ledger.resource_decay_policy_id is not None
            and ledger.resource_decay_policy_id != policy.policy_id
        ):
            raise ValueError(
                f"tenant {tenant.tenant_id!r} resource decay policy drift"
            )
        if (
            ledger.resource_decay_policy_id is None
            and (
                ledger.resource_debt != ResourceTimeVector()
                or ledger.resource_decay_remainder_ns != 0
                or ledger.resource_debt_updated_ns is not None
            )
        ):
            raise ValueError(
                "resource debt state lacks a bound policy identity"
            )
        if ledger.resource_debt_updated_ns is not None:
            if result.started_ns < ledger.resource_debt_updated_ns:
                raise ValueError(
                    f"tenant {tenant.tenant_id!r} quantum time regresses "
                    "behind its resource debt update epoch"
                )
            wall_clock_elapsed_ns = (
                result.finished_ns - ledger.resource_debt_updated_ns
            )
        else:
            wall_clock_elapsed_ns = 0

        # With no old debt there is nothing to age.  Start a fresh decay epoch
        # at this result's finish so sub-tick time before newly charged usage is
        # never applied retroactively to that usage.
        if ledger.resource_debt == ResourceTimeVector():
            decay_elapsed_ns = 0
            carried_remainder_ns = 0
        else:
            decay_elapsed_ns = wall_clock_elapsed_ns
            carried_remainder_ns = ledger.resource_decay_remainder_ns
        raw = attributed.get(tenant.tenant_id, RawResourceUsage())
        normalized = normalize_resource_usage(raw, capacities)
        entitlement = resource_entitlement(
            result.elapsed_ns,
            tenant.policy,
            active_policies,
        )
        update = update_resource_debt(
            ledger.resource_debt,
            normalized,
            entitlement,
            decay_elapsed_ns=decay_elapsed_ns,
            entitlement_elapsed_ns=result.elapsed_ns,
            carried_remainder_ns=carried_remainder_ns,
            decay_policy=policy,
        )
        replacements[tenant.tenant_id] = replace(
            tenant,
            ledger=replace(
                ledger,
                resource_debt=update.after,
                resource_decay_remainder_ns=update.new_remainder_ns,
                resource_decay_policy_id=policy.policy_id,
                resource_debt_updated_ns=result.finished_ns,
            ),
        )
        updates.append(
            TenantQuantumDebtUpdate(
                tenant_id=tenant.tenant_id,
                raw_usage=raw,
                normalized_usage=normalized,
                update=update,
            )
        )
    state_after = SchedulerState(
        global_fair=state.global_fair,
        current_time_ns=result.finished_ns,
        tenants=_replace_tenant_tuple(state.tenants, replacements),
        requests=state.requests,
    )
    sorted_updates = tuple(sorted(updates, key=lambda item: item.tenant_id))
    active_ids = tuple(item.tenant_id for item in sorted_updates)
    return QuantumResourceAccountingResult(
        state_before=state,
        state_after=state_after,
        action_id=action.action_id,
        quantum_result_id=result.stable_id,
        started_ns=result.started_ns,
        finished_ns=result.finished_ns,
        capacities=capacities,
        decay_policy=policy,
        start_active_tenant_ids=active_ids,
        tenant_updates=sorted_updates,
    )


@dataclass(frozen=True, slots=True)
class DualLedgerQuantumAccountingResult:
    """One indivisible resource-then-canonical quantum transition."""

    state_before: SchedulerState
    state_after: SchedulerState
    action_id: str
    quantum_result_id: str
    resource_accounting: QuantumResourceAccountingResult
    canonical_accounting: QuantumCanonicalAccountingResult

    def __post_init__(self) -> None:
        if not isinstance(self.state_before, SchedulerState):
            raise TypeError("state_before must be a SchedulerState")
        if not isinstance(self.state_after, SchedulerState):
            raise TypeError("state_after must be a SchedulerState")
        for name, value in (
            ("action_id", self.action_id),
            ("quantum_result_id", self.quantum_result_id),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-empty")
        if not isinstance(
            self.resource_accounting,
            QuantumResourceAccountingResult,
        ):
            raise TypeError(
                "resource_accounting must be QuantumResourceAccountingResult"
            )
        if not isinstance(
            self.canonical_accounting,
            QuantumCanonicalAccountingResult,
        ):
            raise TypeError(
                "canonical_accounting must be "
                "QuantumCanonicalAccountingResult"
            )
        if self.resource_accounting.state_before != self.state_before:
            raise ValueError("resource accounting must start at state_before")
        if (
            self.canonical_accounting.state_before
            != self.resource_accounting.state_after
        ):
            raise ValueError(
                "canonical accounting must consume the resource-updated state"
            )
        if self.canonical_accounting.state_after != self.state_after:
            raise ValueError("canonical accounting must produce state_after")
        if self.resource_accounting.action_id != self.action_id:
            raise ValueError("resource evidence action does not match")
        if (
            self.resource_accounting.quantum_result_id
            != self.quantum_result_id
        ):
            raise ValueError("resource evidence quantum result does not match")
        if self.canonical_accounting.source != "quantum":
            raise ValueError("dual canonical evidence must have quantum source")
        if self.canonical_accounting.action_id != self.action_id:
            raise ValueError("canonical evidence action does not match")
        if (
            self.canonical_accounting.quantum_result_id
            != self.quantum_result_id
        ):
            raise ValueError("canonical evidence quantum result does not match")
        if (
            self.canonical_accounting.completion_time_ns
            != self.resource_accounting.finished_ns
        ):
            raise ValueError(
                "dual resource and canonical evidence must use one finish time"
            )
        if (
            self.resource_accounting.start_active_tenant_ids
            != self.canonical_accounting.start_active_tenant_ids
        ):
            raise ValueError(
                "resource and canonical accounting must share one "
                "quantum-start active set"
            )

    @property
    def start_active_tenant_ids(self) -> tuple[str, ...]:
        return self.resource_accounting.start_active_tenant_ids

    def to_key(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "canonical_accounting": self.canonical_accounting.to_key(),
            "quantum_result_id": self.quantum_result_id,
            "resource_accounting": self.resource_accounting.to_key(),
            "state_after_id": self.state_after.stable_id,
            "state_before_id": self.state_before.stable_id,
        }


def _verify_quantum_canonical_intermediate_shape(
    start_state: SchedulerState,
    intermediate_state: SchedulerState,
    result: QuantumResult,
) -> None:
    """Bind a canonical phase to a resource-only quantum intermediate.

    This deliberately verifies only the *shape* of the preceding resource
    phase.  It does not prove the resource-debt equation or usage attribution;
    callers needing that guarantee must also verify the resource evidence.
    """

    if not isinstance(intermediate_state, SchedulerState):
        raise TypeError("state must be a SchedulerState")
    if start_state.current_time_ns > result.started_ns:
        raise ValueError(
            "quantum start cannot regress scheduler current_time_ns"
        )
    if intermediate_state.current_time_ns != result.finished_ns:
        raise ValueError(
            "canonical/resource intermediate current_time_ns must equal "
            "the quantum finish"
        )
    if intermediate_state.global_fair != start_state.global_fair:
        raise ValueError(
            "canonical/resource intermediate changed global fairness state"
        )
    if intermediate_state.requests != start_state.requests:
        raise ValueError(
            "canonical/resource intermediate changed request state"
        )

    start_tenant_ids = tuple(
        tenant.tenant_id for tenant in start_state.tenants
    )
    intermediate_tenant_ids = tuple(
        tenant.tenant_id for tenant in intermediate_state.tenants
    )
    if intermediate_tenant_ids != start_tenant_ids:
        raise ValueError(
            "canonical/resource intermediate changed tenant count, IDs, "
            "or order"
        )

    canonical_ledger_fields = (
        "canonical_service_ns",
        "fair_service_coordinate",
        "last_active_ns",
    )
    resource_ledger_fields = (
        "resource_debt",
        "resource_decay_remainder_ns",
        "resource_decay_policy_id",
        "resource_debt_updated_ns",
    )
    for before, after in zip(
        start_state.tenants,
        intermediate_state.tenants,
        strict=True,
    ):
        if after.policy != before.policy:
            raise ValueError(
                "canonical/resource intermediate changed tenant policy"
            )
        if after.active != before.active:
            raise ValueError(
                "canonical/resource intermediate changed tenant active state"
            )
        if after.ledger.tenant_id != before.ledger.tenant_id:
            raise ValueError(
                "canonical/resource intermediate changed ledger tenant ID"
            )
        for field_name in canonical_ledger_fields:
            if getattr(after.ledger, field_name) != getattr(
                before.ledger,
                field_name,
            ):
                raise ValueError(
                    "canonical/resource intermediate changed canonical "
                    f"ledger field {field_name}"
                )

        if not before.active:
            for field_name in resource_ledger_fields:
                if getattr(after.ledger, field_name) != getattr(
                    before.ledger,
                    field_name,
                ):
                    raise ValueError(
                        "canonical/resource intermediate changed inactive "
                        f"tenant resource field {field_name}"
                    )
            continue

        if after.ledger.resource_debt_updated_ns != result.finished_ns:
            raise ValueError(
                "active tenant resource update epoch must equal the "
                "quantum finish"
            )
        if (
            before.ledger.resource_decay_policy_id is not None
            and after.ledger.resource_decay_policy_id
            != before.ledger.resource_decay_policy_id
        ):
            raise ValueError(
                "active tenant resource decay policy drift"
            )
        if not after.ledger.resource_decay_policy_id:
            raise ValueError(
                "active tenant resource intermediate requires a decay policy"
            )


def verify_quantum_canonical_accounting(
    evidence: QuantumCanonicalAccountingResult,
    state: SchedulerState,
    completed_steps: tuple[tuple[str, int], ...],
    *,
    completion_time_ns: int,
    profile: ProfileProvider,
    action: Action | None = None,
    result: QuantumResult | None = None,
    quantum_start_state: SchedulerState | None = None,
) -> QuantumCanonicalAccountingResult:
    """Recompute canonical evidence from its original semantic inputs.

    In quantum mode, ``quantum_start_state`` is required.  The verifier binds
    ``state`` to that start state as a resource-only intermediate, then proves
    the canonical phase.  It intentionally does not recompute the resource
    usage/debt equation; use :func:`verify_quantum_resource_accounting` for
    that phase, or :func:`verify_dual_ledger_quantum_accounting` for the
    complete chained transition.
    """

    if not isinstance(evidence, QuantumCanonicalAccountingResult):
        raise TypeError(
            "evidence must be QuantumCanonicalAccountingResult"
        )
    if (action is None) != (result is None):
        raise ValueError("action and result must be supplied together")
    if action is None:
        if quantum_start_state is not None:
            raise ValueError(
                "standalone canonical evidence has no quantum_start_state"
            )
        source = "standalone"
        action_id = None
        quantum_result_id = None
    else:
        if not isinstance(action, Action) or not isinstance(result, QuantumResult):
            raise TypeError("action/result have invalid types")
        if quantum_start_state is None:
            raise ValueError(
                "quantum canonical verification requires quantum_start_state"
            )
        start_state = quantum_start_state
        if not isinstance(start_state, SchedulerState):
            raise TypeError("quantum_start_state must be SchedulerState")
        validate_quantum_result(start_state, action, result)
        _verify_quantum_canonical_intermediate_shape(
            start_state,
            state,
            result,
        )
        expected_start_active = tuple(
            tenant.tenant_id
            for tenant in start_state.tenants
            if tenant.active
        )
        canonical_start_active = tuple(
            tenant.tenant_id for tenant in state.tenants if tenant.active
        )
        if canonical_start_active != expected_start_active:
            raise ValueError(
                "canonical/resource intermediate changed start-active tenants"
            )
        if result.completed_steps != completed_steps:
            raise ValueError("completed_steps do not match the supplied result")
        if result.finished_ns != completion_time_ns:
            raise ValueError("completion time does not match the supplied result")
        source = "quantum"
        action_id = action.action_id
        quantum_result_id = result.stable_id
    expected = _account_quantum_completed_steps(
        state,
        completed_steps,
        completion_time_ns=completion_time_ns,
        profile=profile,
        source=source,
        action_id=action_id,
        quantum_result_id=quantum_result_id,
    )
    if evidence != expected:
        raise ValueError(
            "canonical accounting evidence does not exactly recompute"
        )
    return evidence


def verify_quantum_resource_accounting(
    evidence: QuantumResourceAccountingResult,
    state: SchedulerState,
    action: Action,
    result: QuantumResult,
    capacities: ResourceCapacities,
    *,
    decay_policy: DebtDecayPolicy | None = None,
) -> QuantumResourceAccountingResult:
    """Recompute resource evidence from state/action/result/profile inputs."""

    if not isinstance(evidence, QuantumResourceAccountingResult):
        raise TypeError("evidence must be QuantumResourceAccountingResult")
    expected = account_quantum_resource_usage(
        state,
        action,
        result,
        capacities,
        decay_policy=decay_policy,
    )
    if evidence != expected:
        raise ValueError(
            "resource accounting evidence does not exactly recompute"
        )
    return evidence


def verify_dual_ledger_quantum_accounting(
    evidence: DualLedgerQuantumAccountingResult,
    state: SchedulerState,
    action: Action,
    result: QuantumResult,
    capacities: ResourceCapacities,
    *,
    profile: ProfileProvider,
    decay_policy: DebtDecayPolicy | None = None,
) -> DualLedgerQuantumAccountingResult:
    """Fail closed unless both evidence ledgers exactly recompute and chain."""

    if not isinstance(evidence, DualLedgerQuantumAccountingResult):
        raise TypeError("evidence must be DualLedgerQuantumAccountingResult")
    if evidence.state_before != state:
        raise ValueError("dual evidence state_before does not match input state")
    if evidence.action_id != action.action_id:
        raise ValueError("dual evidence action_id does not match input action")
    if evidence.quantum_result_id != result.stable_id:
        raise ValueError(
            "dual evidence quantum_result_id does not match input result"
        )
    verify_quantum_resource_accounting(
        evidence.resource_accounting,
        state,
        action,
        result,
        capacities,
        decay_policy=decay_policy,
    )
    verify_quantum_canonical_accounting(
        evidence.canonical_accounting,
        evidence.resource_accounting.state_after,
        result.completed_steps,
        completion_time_ns=result.finished_ns,
        profile=profile,
        action=action,
        result=result,
        quantum_start_state=state,
    )
    if evidence.state_after != evidence.canonical_accounting.state_after:
        raise ValueError("dual evidence final state does not match canonical state")
    return evidence


def account_dual_ledger_quantum(
    state: SchedulerState,
    action: Action,
    result: QuantumResult,
    capacities: ResourceCapacities,
    *,
    profile: ProfileProvider,
    decay_policy: DebtDecayPolicy | None = None,
) -> DualLedgerQuantumAccountingResult:
    """Atomically bind and update both ledgers for one observed quantum.

    Resource accounting runs against the quantum-start active set first.
    Canonical completion/lifecycle accounting then consumes that immutable
    intermediate state.  If either phase rejects its input, no state is
    returned.  Failed and successful zero-progress results retain resource
    charges while producing empty canonical charges.
    """

    resource = account_quantum_resource_usage(
        state,
        action,
        result,
        capacities,
        decay_policy=decay_policy,
    )
    canonical = _account_quantum_completed_steps(
        resource.state_after,
        result.completed_steps,
        completion_time_ns=result.finished_ns,
        profile=profile,
        source="quantum",
        action_id=action.action_id,
        quantum_result_id=result.stable_id,
    )
    evidence = DualLedgerQuantumAccountingResult(
        state_before=state,
        state_after=canonical.state_after,
        action_id=action.action_id,
        quantum_result_id=result.stable_id,
        resource_accounting=resource,
        canonical_accounting=canonical,
    )
    return verify_dual_ledger_quantum_accounting(
        evidence,
        state,
        action,
        result,
        capacities,
        profile=profile,
        decay_policy=decay_policy,
    )


def account_quantum(
    state: SchedulerState,
    action: Action,
    result: QuantumResult,
    capacities: ResourceCapacities,
    *,
    profile: ProfileProvider,
    decay_policy: DebtDecayPolicy | None = None,
) -> DualLedgerQuantumAccountingResult:
    """Public short name for :func:`account_dual_ledger_quantum`."""

    return account_dual_ledger_quantum(
        state,
        action,
        result,
        capacities,
        profile=profile,
        decay_policy=decay_policy,
    )


__all__ = [
    "CanonicalAccountingResult",
    "DebtDecayPolicy",
    "DualLedgerQuantumAccountingResult",
    "QuantumCanonicalAccountingResult",
    "QuantumResourceAccountingResult",
    "RESOURCE_DEBT_DECAY_SCHEMA_VERSION",
    "RequestCanonicalCharge",
    "ResourceDebtUpdate",
    "TenantQuantumDebtUpdate",
    "account_completed_steps",
    "account_dual_ledger_quantum",
    "account_quantum",
    "account_quantum_completed_steps",
    "account_quantum_resource_usage",
    "bounded_sleeper_credit",
    "compare_virtual_service",
    "least_served_tenant",
    "normalize_resource_usage",
    "register_request",
    "register_tenant",
    "reject_request",
    "resource_entitlement",
    "service_lag",
    "update_resource_debt",
    "validate_quantum_result",
    "verify_dual_ledger_quantum_accounting",
    "verify_quantum_canonical_accounting",
    "verify_quantum_resource_accounting",
    "wake_tenant",
]
