"""Immutable, versioned data model for pure-CPU BurstServe simulations.

The simulator deliberately contains no CUDA objects, tensors, wall-clock
handles, or backend masks.  Integer nanoseconds/bytes represent observations.
Quantities that require division are stored as normalized rational pairs, so
fairness and resource accounting never depend on binary floating point.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from functools import total_ordering
from hashlib import sha256
import json
from math import gcd
from types import NotImplementedType
from typing import Any, ClassVar


SIM_SCHEMA_VERSION = 2
SUPPORTED_SIM_SCHEMA_VERSIONS = frozenset({2})
SIM_ENVELOPE_SCHEMA = "burstserve.sim"
NANOSECONDS_PER_SECOND = 1_000_000_000

_REQUEST_STATUSES = frozenset(
    {
        "queued",
        "runnable",
        "running",
        "suspended",
        "completed",
        "rejected",
    }
)


def _integer(value: Any, *, field_name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    if value < minimum:
        raise ValueError(f"{field_name} must be at least {minimum}")
    return value


def _signed_integer(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    return value


def _nonempty(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not value.strip():
        raise ValueError(f"{field_name} must be non-empty")
    return value


def _normalized_ids(value: Any, *, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise TypeError(f"{field_name} must be a tuple")
    normalized = tuple(
        _nonempty(item, field_name=f"{field_name} item") for item in value
    )
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{field_name} must not contain duplicates")
    return tuple(sorted(normalized))


def _canonical_payload(payload: Any) -> Any:
    if hasattr(payload, "to_key"):
        return _canonical_payload(payload.to_key())
    if isinstance(payload, dict):
        normalized: dict[str, Any] = {}
        for key, value in payload.items():
            if not isinstance(key, str):
                raise TypeError("canonical mappings require string keys")
            normalized[key] = _canonical_payload(value)
        return normalized
    if isinstance(payload, (tuple, list)):
        return [_canonical_payload(item) for item in payload]
    if payload is None or isinstance(payload, (str, bool)):
        return payload
    if isinstance(payload, int):
        return payload
    raise TypeError(
        "canonical payloads permit only model keys, mappings, sequences, "
        "and non-floating scalar values"
    )


def canonical_json(payload: Any) -> str:
    """Serialize a canonical-key payload reproducibly.

    Public model objects expose ``to_key``; callers must serialize that result
    rather than relying on dataclass ``repr`` or unordered containers.
    """

    return json.dumps(
        _canonical_payload(payload),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _stable_id(prefix: str, payload: Any) -> str:
    encoded = canonical_json(payload).encode("utf-8")
    return f"{prefix}-{sha256(encoded).hexdigest()}"


def _versioned_stable_id(
    kind_prefix: str,
    schema_version: int,
    payload: Any,
) -> str:
    _nonempty(kind_prefix, field_name="kind_prefix")
    version = _integer(
        schema_version,
        field_name="schema_version",
        minimum=1,
    )
    return _stable_id(f"{kind_prefix}{version}", payload)


_ENVELOPE_KINDS = frozenset(
    {
        "action",
        "quantum_result",
        "request_spec",
        "request_state",
        "residency_state",
        "scheduler_state",
        "trace_event",
        "workload_signature",
    }
)
_V2_PAYLOAD_FIELDS = {
    "action": frozenset(
        {"allocations", "schema_version", "target_residency_id"}
    ),
    "quantum_result": frozenset(
        {
            "action_id",
            "completed_steps",
            "error",
            "finished_ns",
            "resource_usage_by_tenant",
            "schema_version",
            "started_ns",
            "success",
            "total_resource_usage",
        }
    ),
    "request_spec": frozenset(
        {
            "arrival_ns",
            "deadline_ns",
            "kind",
            "request_id",
            "schema_version",
            "signature_id",
            "tenant_id",
        }
    ),
    "request_state": frozenset(
        {
            "completed_steps",
            "last_progress_ns",
            "request_spec_id",
            "schema_version",
            "status",
        }
    ),
    "residency_state": frozenset(
        {
            "device_continuation_ids",
            "device_immutable_ids",
            "dirty_continuation_ids",
            "host_continuation_ids",
            "schema_version",
        }
    ),
    "scheduler_state": frozenset(
        {
            "current_time_ns",
            "global_fair",
            "requests",
            "schema_version",
            "tenants",
        }
    ),
    "trace_event": frozenset(
        {
            "kind",
            "payload",
            "schema_version",
            "sequence",
            "subject_id",
            "timestamp_ns",
        }
    ),
    "workload_signature": frozenset(
        {
            "attention_backend",
            "batch_size",
            "cfg_mode",
            "dtype",
            "frame_count",
            "height",
            "model",
            "profile_id",
            "revision",
            "scheduler",
            "schema_version",
            "streaming_mode",
            "total_steps",
            "width",
        }
    ),
}
_V2_TENANT_FIELDS = frozenset({"active", "ledger", "policy"})
_V2_TENANT_POLICY_FIELDS = frozenset(
    {"tenant_id", "weight_denominator", "weight_numerator"}
)
_V2_TENANT_LEDGER_FIELDS = frozenset(
    {
        "canonical_service_ns",
        "fair_service_coordinate",
        "last_active_ns",
        "resource_decay_policy_id",
        "resource_decay_remainder_ns",
        "resource_debt",
        "resource_debt_updated_ns",
        "tenant_id",
    }
)
_V2_RATIO_FIELDS = frozenset({"denominator", "numerator"})
_V2_RESOURCE_VECTOR_FIELDS = frozenset(
    {"compute_ns", "hbm_ns", "pcie_d2h_ns", "pcie_h2d_ns"}
)
_V2_REQUEST_ALLOCATION_FIELDS = frozenset(
    {
        "quantum_steps",
        "quota_denominator",
        "quota_numerator",
        "request_id",
        "tile_count",
    }
)
_V2_RAW_RESOURCE_FIELDS = frozenset(
    {"hbm_bytes", "pcie_d2h_bytes", "pcie_h2d_bytes", "sm_ns"}
)
_V2_TENANT_RESOURCE_USAGE_FIELDS = frozenset({"tenant_id", "usage"})


def _v2_wire_list(value: Any, *, field_name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must use a JSON list")
    return value


def _v2_stable_reference(
    value: Any,
    *,
    prefix: str,
    field_name: str,
) -> str:
    reference = _nonempty(value, field_name=field_name)
    expected_prefix = f"{prefix}2-"
    digest = reference[len(expected_prefix):]
    if (
        not reference.startswith(expected_prefix)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ValueError(
            f"{field_name} must be a canonical {expected_prefix} reference"
        )
    return reference


def _validate_v2_workload_signature(payload: dict[str, Any]) -> None:
    signature = WorkloadSignature(
        model=payload["model"],
        revision=payload["revision"],
        height=payload["height"],
        width=payload["width"],
        frame_count=payload["frame_count"],
        batch_size=payload["batch_size"],
        dtype=payload["dtype"],
        cfg_mode=payload["cfg_mode"],
        scheduler=payload["scheduler"],
        total_steps=payload["total_steps"],
        attention_backend=payload["attention_backend"],
        streaming_mode=payload["streaming_mode"],
        profile_id=payload["profile_id"],
    )
    if signature.to_key() != payload:
        raise ValueError("workload_signature record is not canonical")


def _validate_v2_request_spec(payload: dict[str, Any]) -> None:
    _nonempty(payload["request_id"], field_name="request_spec.request_id")
    _nonempty(payload["tenant_id"], field_name="request_spec.tenant_id")
    _nonempty(payload["kind"], field_name="request_spec.kind")
    _v2_stable_reference(
        payload["signature_id"],
        prefix="wls",
        field_name="request_spec.signature_id",
    )
    arrival = _integer(
        payload["arrival_ns"],
        field_name="request_spec.arrival_ns",
    )
    deadline = payload["deadline_ns"]
    if deadline is not None:
        deadline = _integer(
            deadline,
            field_name="request_spec.deadline_ns",
        )
        if deadline <= arrival:
            raise ValueError(
                "request_spec deadline_ns must be later than arrival_ns"
            )


def _validate_v2_request_state_record(
    payload: Any,
    *,
    field_name: str,
) -> str:
    expected = _V2_PAYLOAD_FIELDS["request_state"]
    if not isinstance(payload, dict) or set(payload) != expected:
        raise ValueError(f"{field_name} fields do not match v2")
    if payload["schema_version"] != 2:
        raise ValueError(f"{field_name} schema_version must be v2")
    completed_steps = _integer(
        payload["completed_steps"],
        field_name=f"{field_name}.completed_steps",
    )
    status = payload["status"]
    if status not in _REQUEST_STATUSES:
        raise ValueError(f"{field_name} status is invalid")
    if status == "completed" and completed_steps == 0:
        raise ValueError(
            f"{field_name} completed status requires positive progress"
        )
    last_progress = payload["last_progress_ns"]
    if last_progress is not None:
        _integer(
            last_progress,
            field_name=f"{field_name}.last_progress_ns",
        )
    return _v2_stable_reference(
        payload["request_spec_id"],
        prefix="req",
        field_name=f"{field_name}.request_spec_id",
    )


def _validate_v2_request_state(payload: dict[str, Any]) -> None:
    _validate_v2_request_state_record(
        payload,
        field_name="request_state",
    )


def _validate_v2_residency_state(payload: dict[str, Any]) -> None:
    id_fields = (
        "device_immutable_ids",
        "device_continuation_ids",
        "host_continuation_ids",
        "dirty_continuation_ids",
    )
    values = {
        field_name: tuple(
            _v2_wire_list(
                payload[field_name],
                field_name=f"residency_state.{field_name}",
            )
        )
        for field_name in id_fields
    }
    residency = ResidencyState(**values)
    if residency.to_key() != payload:
        raise ValueError(
            "residency_state ID lists must be unique and lexicographically sorted"
        )


def _v2_request_allocation_record(
    payload: Any,
    *,
    field_name: str,
) -> "RequestAllocation":
    if (
        not isinstance(payload, dict)
        or set(payload) != _V2_REQUEST_ALLOCATION_FIELDS
    ):
        raise ValueError(f"{field_name} fields do not match v2")
    allocation = RequestAllocation(
        request_id=payload["request_id"],
        quantum_steps=payload["quantum_steps"],
        quota_numerator=payload["quota_numerator"],
        quota_denominator=payload["quota_denominator"],
        tile_count=payload["tile_count"],
    )
    if allocation.to_key() != payload:
        raise ValueError(f"{field_name} is not canonical")
    return allocation


def _validate_v2_action(payload: dict[str, Any]) -> None:
    allocation_records = _v2_wire_list(
        payload["allocations"],
        field_name="action.allocations",
    )
    allocations = tuple(
        _v2_request_allocation_record(
            record,
            field_name=f"action.allocations[{index}]",
        )
        for index, record in enumerate(allocation_records)
    )
    normalized = Action(allocations, ResidencyState())
    expected_allocations = [
        allocation.to_key() for allocation in normalized.allocations
    ]
    if allocation_records != expected_allocations:
        raise ValueError(
            "action allocations must be unique and sorted by request_id"
        )
    _v2_stable_reference(
        payload["target_residency_id"],
        prefix="res",
        field_name="action.target_residency_id",
    )


def _v2_raw_resource_usage_record(
    payload: Any,
    *,
    field_name: str,
) -> "RawResourceUsage":
    if not isinstance(payload, dict) or set(payload) != _V2_RAW_RESOURCE_FIELDS:
        raise ValueError(f"{field_name} fields do not match v2")
    usage = RawResourceUsage(**payload)
    if usage.to_key() != payload:
        raise ValueError(f"{field_name} is not canonical")
    return usage


def _validate_v2_quantum_result(payload: dict[str, Any]) -> None:
    _v2_stable_reference(
        payload["action_id"],
        prefix="act",
        field_name="quantum_result.action_id",
    )
    completed_records = _v2_wire_list(
        payload["completed_steps"],
        field_name="quantum_result.completed_steps",
    )
    completed_steps: list[tuple[str, int]] = []
    for index, record in enumerate(completed_records):
        if not isinstance(record, list) or len(record) != 2:
            raise ValueError(
                "quantum_result completed_steps entries must be two-item lists"
            )
        completed_steps.append((record[0], record[1]))

    attribution_records = _v2_wire_list(
        payload["resource_usage_by_tenant"],
        field_name="quantum_result.resource_usage_by_tenant",
    )
    attributions: list[TenantResourceUsage] = []
    for index, record in enumerate(attribution_records):
        if (
            not isinstance(record, dict)
            or set(record) != _V2_TENANT_RESOURCE_USAGE_FIELDS
        ):
            raise ValueError(
                "quantum_result tenant resource usage fields do not match v2"
            )
        attributions.append(
            TenantResourceUsage(
                tenant_id=record["tenant_id"],
                usage=_v2_raw_resource_usage_record(
                    record["usage"],
                    field_name=(
                        "quantum_result.resource_usage_by_tenant"
                        f"[{index}].usage"
                    ),
                ),
            )
        )
    result = QuantumResult(
        action_id=payload["action_id"],
        started_ns=payload["started_ns"],
        finished_ns=payload["finished_ns"],
        completed_steps=tuple(completed_steps),
        total_resource_usage=_v2_raw_resource_usage_record(
            payload["total_resource_usage"],
            field_name="quantum_result.total_resource_usage",
        ),
        resource_usage_by_tenant=tuple(attributions),
        success=payload["success"],
        error=payload["error"],
    )
    if not result.success and result.completed_steps:
        raise ValueError("failed quantum_result cannot report completed steps")
    if result.to_key() != payload:
        raise ValueError(
            "quantum_result lists must use canonical request/tenant ordering"
        )


def _validate_v2_trace_event(payload: dict[str, Any]) -> None:
    payload_records = _v2_wire_list(
        payload["payload"],
        field_name="trace_event.payload",
    )
    entries: list[tuple[str, str | int | bool | None]] = []
    for record in payload_records:
        if not isinstance(record, list) or len(record) != 2:
            raise ValueError(
                "trace_event payload entries must be two-item lists"
            )
        entries.append((record[0], record[1]))
    event = TraceEvent(
        sequence=payload["sequence"],
        timestamp_ns=payload["timestamp_ns"],
        kind=payload["kind"],
        subject_id=payload["subject_id"],
        payload=tuple(entries),
    )
    if event.to_key() != payload:
        raise ValueError(
            "trace_event payload must have unique keys sorted lexicographically"
        )


def _v2_exact_ratio_record(value: Any, *, field_name: str) -> "ExactRatio":
    if not isinstance(value, dict) or set(value) != _V2_RATIO_FIELDS:
        raise ValueError(f"{field_name} must be an exact-ratio record")
    ratio = ExactRatio(
        value["numerator"],
        value["denominator"],
    )
    if ratio.to_key() != value:
        raise ValueError(f"{field_name} exact ratio is not normalized")
    return ratio


def _v2_resource_vector_record(
    value: Any,
    *,
    field_name: str,
) -> "ResourceTimeVector":
    if (
        not isinstance(value, dict)
        or set(value) != _V2_RESOURCE_VECTOR_FIELDS
    ):
        raise ValueError(f"{field_name} must be a resource-vector record")
    vector = ResourceTimeVector(
        **{
            component: _v2_exact_ratio_record(
                value[component],
                field_name=f"{field_name}.{component}",
            )
            for component in _V2_RESOURCE_VECTOR_FIELDS
        }
    )
    if vector.to_key() != value:
        raise ValueError(f"{field_name} resource vector is not canonical")
    return vector


def _validate_v2_scheduler_tenant_ledgers(payload: dict[str, Any]) -> None:
    """Validate scheduler records without claiming full reference-graph decode.

    Request records contain only ``request_spec_id`` and therefore cannot be
    linked back to tenant/signature objects from this envelope alone.  This
    validator reconstructs every self-contained policy, ratio, resource vector,
    and tenant ledger, plus the global watermark; it deliberately does not
    claim to materialize a fully typed SchedulerState graph.
    """

    current_time = _integer(
        payload.get("current_time_ns"),
        field_name="scheduler_state.current_time_ns",
    )
    global_fair = payload.get("global_fair")
    if not isinstance(global_fair, dict) or set(global_fair) != {"virtual_time"}:
        raise ValueError("scheduler_state global_fair fields do not match v2")
    virtual_time = _v2_exact_ratio_record(
        global_fair["virtual_time"],
        field_name="scheduler_state.global_fair.virtual_time",
    )
    reconstructed_global = GlobalFairState(virtual_time)
    if reconstructed_global.to_key() != global_fair:
        raise ValueError("scheduler_state global_fair is not canonical")

    tenants = payload.get("tenants")
    if not isinstance(tenants, list):
        raise ValueError("scheduler_state tenants must be a list")
    reconstructed_tenants: list[TenantState] = []
    for tenant in tenants:
        if not isinstance(tenant, dict) or set(tenant) != _V2_TENANT_FIELDS:
            raise ValueError("scheduler_state tenant fields do not match v2")
        policy_record = tenant.get("policy")
        if (
            not isinstance(policy_record, dict)
            or set(policy_record) != _V2_TENANT_POLICY_FIELDS
        ):
            raise ValueError("scheduler_state TenantPolicy fields do not match v2")
        policy = TenantPolicy(
            tenant_id=policy_record["tenant_id"],
            weight_numerator=policy_record["weight_numerator"],
            weight_denominator=policy_record["weight_denominator"],
        )
        if policy.to_key() != policy_record:
            raise ValueError("scheduler_state TenantPolicy is not canonical")
        ledger = tenant.get("ledger")
        if (
            not isinstance(ledger, dict)
            or set(ledger) != _V2_TENANT_LEDGER_FIELDS
        ):
            raise ValueError(
                "scheduler_state TenantLedger fields do not match v2"
            )
        reconstructed_ledger = TenantLedger(
            tenant_id=ledger["tenant_id"],
            canonical_service_ns=ledger["canonical_service_ns"],
            fair_service_coordinate=_v2_exact_ratio_record(
                ledger["fair_service_coordinate"],
                field_name="TenantLedger.fair_service_coordinate",
            ),
            resource_debt=_v2_resource_vector_record(
                ledger["resource_debt"],
                field_name="TenantLedger.resource_debt",
            ),
            resource_decay_remainder_ns=ledger[
                "resource_decay_remainder_ns"
            ],
            resource_decay_policy_id=ledger["resource_decay_policy_id"],
            resource_debt_updated_ns=ledger["resource_debt_updated_ns"],
            last_active_ns=ledger["last_active_ns"],
        )
        if reconstructed_ledger.to_key() != ledger:
            raise ValueError("scheduler_state TenantLedger is not canonical")
        reconstructed_tenant = TenantState(
            policy=policy,
            ledger=reconstructed_ledger,
            active=tenant["active"],
        )
        if reconstructed_tenant.to_key() != tenant:
            raise ValueError("scheduler_state TenantState is not canonical")
        for field_name, timestamp in (
            ("last_active_ns", reconstructed_ledger.last_active_ns),
            (
                "resource_debt_updated_ns",
                reconstructed_ledger.resource_debt_updated_ns,
            ),
        ):
            if timestamp is not None and timestamp > current_time:
                raise ValueError(
                    f"TenantLedger {field_name} exceeds current_time_ns"
                )
        reconstructed_tenants.append(reconstructed_tenant)
    tenant_ids = [tenant.tenant_id for tenant in reconstructed_tenants]
    if len(set(tenant_ids)) != len(tenant_ids):
        raise ValueError("scheduler_state contains duplicate tenant IDs")
    if tenant_ids != sorted(tenant_ids):
        raise ValueError(
            "scheduler_state tenants must be sorted by tenant_id"
        )

    requests = payload.get("requests")
    if not isinstance(requests, list):
        raise ValueError("scheduler_state requests must be a list")
    request_spec_ids: list[str] = []
    for index, request in enumerate(requests):
        request_spec_ids.append(
            _validate_v2_request_state_record(
                request,
                field_name=f"scheduler_state.requests[{index}]",
            )
        )
        last_progress = request["last_progress_ns"]
        if last_progress is not None:
            if last_progress > current_time:
                raise ValueError(
                    "RequestState progress exceeds scheduler current_time_ns"
                )
    if len(set(request_spec_ids)) != len(request_spec_ids):
        raise ValueError("scheduler_state repeats a request_spec_id")
    if request_spec_ids != sorted(request_spec_ids):
        raise ValueError(
            "scheduler_state requests must be sorted by request_spec_id"
        )


def _validate_envelope_payload(
    version: int,
    kind: str,
    payload: dict[str, Any],
) -> None:
    if version == 2:
        expected = _V2_PAYLOAD_FIELDS[kind]
        if set(payload) != expected:
            raise ValueError(
                f"{kind!r} payload fields do not match the v2 schema"
            )
        validators = {
            "action": _validate_v2_action,
            "quantum_result": _validate_v2_quantum_result,
            "request_spec": _validate_v2_request_spec,
            "request_state": _validate_v2_request_state,
            "residency_state": _validate_v2_residency_state,
            "scheduler_state": _validate_v2_scheduler_tenant_ledgers,
            "trace_event": _validate_v2_trace_event,
            "workload_signature": _validate_v2_workload_signature,
        }
        validators[kind](payload)
        return
    raise UnsupportedSchemaVersionError(
        f"unsupported simulator schema version: {version}"
    )


class UnsupportedSchemaVersionError(ValueError):
    """A versioned simulator artifact uses an unsupported schema version."""


@dataclass(frozen=True, slots=True)
class CanonicalEnvelope:
    """Record-level validated v2 bytes retained for identical re-encoding.

    Envelope decoding validates every self-contained record it can reconstruct.
    Reference-only fields such as ``request_spec_id`` are not a complete object
    graph and are deliberately not advertised as a typed SchedulerState decode.
    """

    schema_version: int
    kind: str
    payload_json: str

    def __post_init__(self) -> None:
        version = _integer(
            self.schema_version,
            field_name="schema_version",
            minimum=1,
        )
        if version not in SUPPORTED_SIM_SCHEMA_VERSIONS:
            raise UnsupportedSchemaVersionError(
                f"unsupported simulator schema version: {version}"
            )
        if self.kind not in _ENVELOPE_KINDS:
            raise ValueError(f"unsupported simulator envelope kind: {self.kind!r}")
        if not isinstance(self.payload_json, str):
            raise TypeError("payload_json must be a string")
        payload = _load_json_no_duplicates(self.payload_json)
        if not isinstance(payload, dict):
            raise ValueError("simulator envelope payload must be an object")
        if payload.get("schema_version") != version:
            raise ValueError(
                "envelope and payload schema versions must match exactly"
            )
        _validate_envelope_payload(version, self.kind, payload)
        if canonical_json(payload) != self.payload_json:
            raise ValueError("payload_json must already be canonical")

    def encode(self) -> str:
        return canonical_json(
            {
                "kind": self.kind,
                "payload": _load_json_no_duplicates(self.payload_json),
                "schema": SIM_ENVELOPE_SCHEMA,
                "schema_version": self.schema_version,
            }
        )


def _load_json_no_duplicates(encoded: str) -> Any:
    if not isinstance(encoded, str):
        raise TypeError("encoded simulator artifact must be a string")

    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key!r}")
            result[key] = value
        return result

    try:
        return json.loads(encoded, object_pairs_hook=pairs_hook)
    except json.JSONDecodeError as error:
        raise ValueError("invalid simulator JSON artifact") from error


def encode_versioned(obj: Any) -> str:
    """Encode one v2 model object in a fail-closed canonical envelope."""

    kind = getattr(obj, "ENVELOPE_KIND", None)
    version = getattr(obj, "SCHEMA_VERSION", None)
    if kind not in _ENVELOPE_KINDS:
        raise TypeError("object does not expose a supported envelope kind")
    if version not in SUPPORTED_SIM_SCHEMA_VERSIONS:
        raise UnsupportedSchemaVersionError(
            f"unsupported simulator schema version: {version!r}"
        )
    if not hasattr(obj, "to_key"):
        raise TypeError("versioned object must expose to_key")
    payload_json = canonical_json(obj.to_key())
    return CanonicalEnvelope(version, kind, payload_json).encode()


def decode_versioned(encoded: str) -> CanonicalEnvelope:
    """Perform fail-closed canonical, version, and record-level validation.

    The return value is an envelope, not a materialized full reference graph.
    """

    decoded = _load_json_no_duplicates(encoded)
    if not isinstance(decoded, dict):
        raise ValueError("simulator envelope must be an object")
    expected_keys = {"kind", "payload", "schema", "schema_version"}
    if set(decoded) != expected_keys:
        raise ValueError("simulator envelope fields do not match the v2 schema")
    if decoded["schema"] != SIM_ENVELOPE_SCHEMA:
        raise ValueError("unsupported simulator envelope schema")
    version = decoded["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise TypeError("schema_version must be an integer")
    if version not in SUPPORTED_SIM_SCHEMA_VERSIONS:
        raise UnsupportedSchemaVersionError(
            f"unsupported simulator schema version: {version}"
        )
    kind = decoded["kind"]
    if kind not in _ENVELOPE_KINDS:
        raise ValueError(f"unsupported simulator envelope kind: {kind!r}")
    payload = decoded["payload"]
    if not isinstance(payload, dict):
        raise ValueError("simulator envelope payload must be an object")
    if payload.get("schema_version") != version:
        raise ValueError(
            "envelope and payload schema versions must match exactly"
        )
    _validate_envelope_payload(version, kind, payload)
    if canonical_json(decoded) != encoded:
        raise ValueError("simulator envelope must use canonical encoding")
    return CanonicalEnvelope(version, kind, canonical_json(payload))


@total_ordering
@dataclass(frozen=True, slots=True)
class ExactRatio:
    """A normalized, immutable rational number with canonical serialization."""

    numerator: int = 0
    denominator: int = 1

    def __post_init__(self) -> None:
        numerator = _signed_integer(self.numerator, field_name="numerator")
        denominator = _integer(
            self.denominator,
            field_name="denominator",
            minimum=1,
        )
        common = gcd(abs(numerator), denominator)
        object.__setattr__(self, "numerator", numerator // common)
        object.__setattr__(self, "denominator", denominator // common)

    @classmethod
    def from_int(cls, value: int) -> "ExactRatio":
        return cls(_signed_integer(value, field_name="value"), 1)

    @classmethod
    def from_fraction(cls, value: Fraction) -> "ExactRatio":
        if not isinstance(value, Fraction):
            raise TypeError("value must be a Fraction")
        return cls(value.numerator, value.denominator)

    @property
    def fraction(self) -> Fraction:
        return Fraction(self.numerator, self.denominator)

    def _coerce(
        self,
        other: object,
    ) -> ExactRatio | NotImplementedType:
        if isinstance(other, ExactRatio):
            return other
        if isinstance(other, int) and not isinstance(other, bool):
            return ExactRatio(other)
        return NotImplemented

    def __add__(self, other: object) -> "ExactRatio":
        converted = self._coerce(other)
        if converted is NotImplemented:
            return NotImplemented
        return ExactRatio(
            self.numerator * converted.denominator
            + converted.numerator * self.denominator,
            self.denominator * converted.denominator,
        )

    def __radd__(self, other: object) -> "ExactRatio":
        return self.__add__(other)

    def __sub__(self, other: object) -> "ExactRatio":
        converted = self._coerce(other)
        if converted is NotImplemented:
            return NotImplemented
        return ExactRatio(
            self.numerator * converted.denominator
            - converted.numerator * self.denominator,
            self.denominator * converted.denominator,
        )

    def __rsub__(self, other: object) -> "ExactRatio":
        converted = self._coerce(other)
        if converted is NotImplemented:
            return NotImplemented
        return converted.__sub__(self)

    def __mul__(self, other: object) -> "ExactRatio":
        converted = self._coerce(other)
        if converted is NotImplemented:
            return NotImplemented
        return ExactRatio(
            self.numerator * converted.numerator,
            self.denominator * converted.denominator,
        )

    def __rmul__(self, other: object) -> "ExactRatio":
        return self.__mul__(other)

    def __truediv__(self, other: object) -> "ExactRatio":
        converted = self._coerce(other)
        if converted is NotImplemented:
            return NotImplemented
        if converted.numerator == 0:
            raise ZeroDivisionError("cannot divide by zero")
        sign = -1 if converted.numerator < 0 else 1
        return ExactRatio(
            sign * self.numerator * converted.denominator,
            self.denominator * abs(converted.numerator),
        )

    def __neg__(self) -> "ExactRatio":
        return ExactRatio(-self.numerator, self.denominator)

    def __eq__(self, other: object) -> bool:
        converted = self._coerce(other)
        if converted is NotImplemented:
            return False
        return (
            self.numerator == converted.numerator
            and self.denominator == converted.denominator
        )

    def __lt__(self, other: object) -> bool:
        converted = self._coerce(other)
        if converted is NotImplemented:
            return NotImplemented
        return (
            self.numerator * converted.denominator
            < converted.numerator * self.denominator
        )

    def to_key(self) -> dict[str, int]:
        return {
            "denominator": self.denominator,
            "numerator": self.numerator,
        }


@dataclass(frozen=True, slots=True)
class WorkloadSignature:
    """Semantic and profile lookup key for one diffusion workload shape."""

    SCHEMA_VERSION: ClassVar[int] = 2
    ENVELOPE_KIND: ClassVar[str] = "workload_signature"

    model: str
    revision: str
    height: int
    width: int
    frame_count: int
    batch_size: int
    dtype: str
    cfg_mode: str
    scheduler: str
    total_steps: int
    attention_backend: str
    streaming_mode: str
    profile_id: str

    def __post_init__(self) -> None:
        for name in (
            "model",
            "revision",
            "dtype",
            "cfg_mode",
            "scheduler",
            "attention_backend",
            "streaming_mode",
            "profile_id",
        ):
            _nonempty(getattr(self, name), field_name=name)
        for name in (
            "height",
            "width",
            "frame_count",
            "batch_size",
            "total_steps",
        ):
            _integer(getattr(self, name), field_name=name, minimum=1)

    def to_key(self) -> dict[str, str | int]:
        return {
            "attention_backend": self.attention_backend,
            "batch_size": self.batch_size,
            "cfg_mode": self.cfg_mode,
            "dtype": self.dtype,
            "frame_count": self.frame_count,
            "height": self.height,
            "model": self.model,
            "profile_id": self.profile_id,
            "revision": self.revision,
            "scheduler": self.scheduler,
            "schema_version": self.SCHEMA_VERSION,
            "streaming_mode": self.streaming_mode,
            "total_steps": self.total_steps,
            "width": self.width,
        }

    @property
    def stable_id(self) -> str:
        return _versioned_stable_id(
            "wls",
            self.SCHEMA_VERSION,
            self.to_key(),
        )


@dataclass(frozen=True, slots=True)
class RequestSpec:
    """Immutable request identity and externally supplied timing contract."""

    SCHEMA_VERSION: ClassVar[int] = 2
    ENVELOPE_KIND: ClassVar[str] = "request_spec"

    request_id: str
    tenant_id: str
    signature: WorkloadSignature
    arrival_ns: int
    deadline_ns: int | None
    kind: str

    def __post_init__(self) -> None:
        _nonempty(self.request_id, field_name="request_id")
        _nonempty(self.tenant_id, field_name="tenant_id")
        if not isinstance(self.signature, WorkloadSignature):
            raise TypeError("signature must be a WorkloadSignature")
        _integer(self.arrival_ns, field_name="arrival_ns")
        if self.deadline_ns is not None:
            _integer(self.deadline_ns, field_name="deadline_ns")
            if self.deadline_ns <= self.arrival_ns:
                raise ValueError("deadline_ns must be later than arrival_ns")
        _nonempty(self.kind, field_name="kind")

    def to_key(self) -> dict[str, str | int | None]:
        return {
            "arrival_ns": self.arrival_ns,
            "deadline_ns": self.deadline_ns,
            "kind": self.kind,
            "request_id": self.request_id,
            "schema_version": self.SCHEMA_VERSION,
            "signature_id": self.signature.stable_id,
            "tenant_id": self.tenant_id,
        }

    @property
    def stable_id(self) -> str:
        return _versioned_stable_id(
            "req",
            self.SCHEMA_VERSION,
            self.to_key(),
        )


@dataclass(frozen=True, slots=True)
class RequestState:
    """One immutable snapshot of a request state machine."""

    SCHEMA_VERSION: ClassVar[int] = 2
    ENVELOPE_KIND: ClassVar[str] = "request_state"

    spec: RequestSpec
    completed_steps: int = 0
    status: str = "queued"
    last_progress_ns: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.spec, RequestSpec):
            raise TypeError("spec must be a RequestSpec")
        _integer(self.completed_steps, field_name="completed_steps")
        if self.completed_steps > self.spec.signature.total_steps:
            raise ValueError("completed_steps exceeds signature.total_steps")
        if self.status not in _REQUEST_STATUSES:
            raise ValueError(f"unsupported request status: {self.status!r}")
        is_complete = self.completed_steps == self.spec.signature.total_steps
        if (self.status == "completed") != is_complete:
            raise ValueError(
                "status must be 'completed' exactly when all steps are complete"
            )
        if self.last_progress_ns is not None:
            _integer(self.last_progress_ns, field_name="last_progress_ns")
            if self.last_progress_ns < self.spec.arrival_ns:
                raise ValueError("last_progress_ns cannot precede arrival_ns")

    @property
    def remaining_steps(self) -> int:
        return self.spec.signature.total_steps - self.completed_steps

    @property
    def is_terminal(self) -> bool:
        return self.status in {"completed", "rejected"}

    @property
    def is_backlogged(self) -> bool:
        return not self.is_terminal

    def to_key(self) -> dict[str, str | int | None]:
        return {
            "completed_steps": self.completed_steps,
            "last_progress_ns": self.last_progress_ns,
            "request_spec_id": self.spec.stable_id,
            "schema_version": self.SCHEMA_VERSION,
            "status": self.status,
        }

    @property
    def stable_id(self) -> str:
        return _versioned_stable_id(
            "rqs",
            self.SCHEMA_VERSION,
            self.to_key(),
        )


@dataclass(frozen=True, slots=True)
class RawResourceUsage:
    """Additive physical observations before capacity normalization.

    ``sm_ns`` is attributed occupied/assigned SM-time, not active-cycle
    utilization.  It may be below a logical allocation because kernels can
    leave assigned SM capacity idle, but accounting rejects values above the
    action's quota-time upper bound.  The byte fields are measured traffic, not
    normalized shares.  VRAM capacity is intentionally absent because it
    remains a hard constraint.
    """

    sm_ns: int = 0
    hbm_bytes: int = 0
    pcie_h2d_bytes: int = 0
    pcie_d2h_bytes: int = 0

    COMPONENTS: ClassVar[tuple[str, ...]] = (
        "sm_ns",
        "hbm_bytes",
        "pcie_h2d_bytes",
        "pcie_d2h_bytes",
    )

    def __post_init__(self) -> None:
        for name in self.COMPONENTS:
            _integer(getattr(self, name), field_name=name)

    def __add__(self, other: object) -> "RawResourceUsage":
        if not isinstance(other, RawResourceUsage):
            return NotImplemented
        return RawResourceUsage(
            **{
                name: getattr(self, name) + getattr(other, name)
                for name in self.COMPONENTS
            }
        )

    def componentwise_min(self, other: "RawResourceUsage") -> "RawResourceUsage":
        if not isinstance(other, RawResourceUsage):
            raise TypeError("other must be a RawResourceUsage")
        return RawResourceUsage(
            **{
                name: min(getattr(self, name), getattr(other, name))
                for name in self.COMPONENTS
            }
        )

    def subtract(self, other: "RawResourceUsage") -> "RawResourceUsage":
        if not isinstance(other, RawResourceUsage):
            raise TypeError("other must be a RawResourceUsage")
        values = {
            name: getattr(self, name) - getattr(other, name)
            for name in self.COMPONENTS
        }
        if any(value < 0 for value in values.values()):
            raise ValueError("resource subtraction would produce a negative component")
        return RawResourceUsage(**values)

    def to_key(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in self.COMPONENTS}


@dataclass(frozen=True, slots=True)
class ResourceCapacities:
    """Exact capacities used to normalize raw work into resource-time."""

    total_sms: int
    hbm_bytes_per_second: int
    pcie_h2d_bytes_per_second: int
    pcie_d2h_bytes_per_second: int

    def __post_init__(self) -> None:
        for name in (
            "total_sms",
            "hbm_bytes_per_second",
            "pcie_h2d_bytes_per_second",
            "pcie_d2h_bytes_per_second",
        ):
            _integer(getattr(self, name), field_name=name, minimum=1)

    def to_key(self) -> dict[str, int]:
        return {
            "hbm_bytes_per_second": self.hbm_bytes_per_second,
            "pcie_d2h_bytes_per_second": self.pcie_d2h_bytes_per_second,
            "pcie_h2d_bytes_per_second": self.pcie_h2d_bytes_per_second,
            "total_sms": self.total_sms,
        }


@dataclass(frozen=True, slots=True)
class ResourceTimeVector:
    """Full-resource-equivalent nanoseconds for comparable resource debt."""

    compute_ns: ExactRatio = field(default_factory=ExactRatio)
    hbm_ns: ExactRatio = field(default_factory=ExactRatio)
    pcie_h2d_ns: ExactRatio = field(default_factory=ExactRatio)
    pcie_d2h_ns: ExactRatio = field(default_factory=ExactRatio)

    COMPONENTS: ClassVar[tuple[str, ...]] = (
        "compute_ns",
        "hbm_ns",
        "pcie_h2d_ns",
        "pcie_d2h_ns",
    )

    def __post_init__(self) -> None:
        for name in self.COMPONENTS:
            value = getattr(self, name)
            if not isinstance(value, ExactRatio):
                raise TypeError(f"{name} must be an ExactRatio")
            if value < 0:
                raise ValueError(f"{name} must be non-negative")

    def __add__(self, other: object) -> "ResourceTimeVector":
        if not isinstance(other, ResourceTimeVector):
            return NotImplemented
        return ResourceTimeVector(
            **{
                name: getattr(self, name) + getattr(other, name)
                for name in self.COMPONENTS
            }
        )

    def scale(self, factor: ExactRatio) -> "ResourceTimeVector":
        if not isinstance(factor, ExactRatio):
            raise TypeError("factor must be an ExactRatio")
        if factor < 0:
            raise ValueError("factor must be non-negative")
        return ResourceTimeVector(
            **{
                name: getattr(self, name) * factor
                for name in self.COMPONENTS
            }
        )

    def subtract_floor_zero(
        self,
        other: "ResourceTimeVector",
    ) -> "ResourceTimeVector":
        if not isinstance(other, ResourceTimeVector):
            raise TypeError("other must be a ResourceTimeVector")
        return ResourceTimeVector(
            **{
                name: max(
                    ExactRatio(),
                    getattr(self, name) - getattr(other, name),
                )
                for name in self.COMPONENTS
            }
        )

    @property
    def dominant_ns(self) -> ExactRatio:
        return max(getattr(self, name) for name in self.COMPONENTS)

    def to_key(self) -> dict[str, dict[str, int]]:
        return {
            name: getattr(self, name).to_key()
            for name in self.COMPONENTS
        }


@dataclass(frozen=True, slots=True)
class TenantPolicy:
    """Tenant entitlement represented as a reduced positive rational weight."""

    tenant_id: str
    weight_numerator: int = 1
    weight_denominator: int = 1

    def __post_init__(self) -> None:
        _nonempty(self.tenant_id, field_name="tenant_id")
        numerator = _integer(
            self.weight_numerator,
            field_name="weight_numerator",
            minimum=1,
        )
        denominator = _integer(
            self.weight_denominator,
            field_name="weight_denominator",
            minimum=1,
        )
        common = gcd(numerator, denominator)
        object.__setattr__(self, "weight_numerator", numerator // common)
        object.__setattr__(self, "weight_denominator", denominator // common)

    @property
    def weight(self) -> ExactRatio:
        return ExactRatio(self.weight_numerator, self.weight_denominator)

    def to_key(self) -> dict[str, str | int]:
        return {
            "tenant_id": self.tenant_id,
            "weight_denominator": self.weight_denominator,
            "weight_numerator": self.weight_numerator,
        }


@dataclass(frozen=True, slots=True)
class TenantLedger:
    """Audited work and a separate fairness coordinate for one tenant."""

    tenant_id: str
    canonical_service_ns: int = 0
    fair_service_coordinate: ExactRatio = field(default_factory=ExactRatio)
    resource_debt: ResourceTimeVector = field(default_factory=ResourceTimeVector)
    resource_decay_remainder_ns: int = 0
    resource_decay_policy_id: str | None = None
    resource_debt_updated_ns: int | None = None
    last_active_ns: int | None = None

    def __post_init__(self) -> None:
        _nonempty(self.tenant_id, field_name="tenant_id")
        _integer(
            self.canonical_service_ns,
            field_name="canonical_service_ns",
        )
        if not isinstance(self.fair_service_coordinate, ExactRatio):
            raise TypeError("fair_service_coordinate must be an ExactRatio")
        if self.fair_service_coordinate < 0:
            raise ValueError("fair_service_coordinate must be non-negative")
        if not isinstance(self.resource_debt, ResourceTimeVector):
            raise TypeError("resource_debt must be a ResourceTimeVector")
        _integer(
            self.resource_decay_remainder_ns,
            field_name="resource_decay_remainder_ns",
        )
        if self.resource_decay_policy_id is not None:
            _nonempty(
                self.resource_decay_policy_id,
                field_name="resource_decay_policy_id",
            )
        if self.resource_debt_updated_ns is not None:
            _integer(
                self.resource_debt_updated_ns,
                field_name="resource_debt_updated_ns",
            )
        has_resource_state = (
            self.resource_debt != ResourceTimeVector()
            or self.resource_decay_remainder_ns != 0
        )
        if has_resource_state and (
            self.resource_decay_policy_id is None
            or self.resource_debt_updated_ns is None
        ):
            raise ValueError(
                "non-empty resource debt state requires a policy and update epoch"
            )
        if (self.resource_decay_policy_id is None) != (
            self.resource_debt_updated_ns is None
        ):
            raise ValueError(
                "resource decay policy and update epoch must be bound together"
            )
        if self.last_active_ns is not None:
            _integer(self.last_active_ns, field_name="last_active_ns")

    def virtual_service(self, policy: TenantPolicy) -> ExactRatio:
        if not isinstance(policy, TenantPolicy):
            raise TypeError("policy must be a TenantPolicy")
        if policy.tenant_id != self.tenant_id:
            raise ValueError("ledger and policy must belong to the same tenant")
        return self.fair_service_coordinate / policy.weight

    def to_key(self) -> dict[str, Any]:
        return {
            "canonical_service_ns": self.canonical_service_ns,
            "fair_service_coordinate": self.fair_service_coordinate.to_key(),
            "last_active_ns": self.last_active_ns,
            "resource_decay_policy_id": self.resource_decay_policy_id,
            "resource_decay_remainder_ns": self.resource_decay_remainder_ns,
            "resource_debt": self.resource_debt.to_key(),
            "resource_debt_updated_ns": self.resource_debt_updated_ns,
            "tenant_id": self.tenant_id,
        }


@dataclass(frozen=True, slots=True)
class TenantState:
    """One unique tenant policy/ledger and its scheduler activation state."""

    policy: TenantPolicy
    ledger: TenantLedger
    active: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.policy, TenantPolicy):
            raise TypeError("policy must be a TenantPolicy")
        if not isinstance(self.ledger, TenantLedger):
            raise TypeError("ledger must be a TenantLedger")
        if self.policy.tenant_id != self.ledger.tenant_id:
            raise ValueError("tenant policy and ledger IDs must match")
        if not isinstance(self.active, bool):
            raise TypeError("active must be a bool")

    @property
    def tenant_id(self) -> str:
        return self.policy.tenant_id

    def to_key(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "ledger": self.ledger.to_key(),
            "policy": self.policy.to_key(),
        }


@dataclass(frozen=True, slots=True)
class GlobalFairState:
    """Exact global virtual time shared by all active tenant groups."""

    virtual_time: ExactRatio = field(default_factory=ExactRatio)

    def __post_init__(self) -> None:
        if not isinstance(self.virtual_time, ExactRatio):
            raise TypeError("virtual_time must be an ExactRatio")
        if self.virtual_time < 0:
            raise ValueError("virtual_time must be non-negative")

    def to_key(self) -> dict[str, Any]:
        return {"virtual_time": self.virtual_time.to_key()}


@dataclass(frozen=True, slots=True)
class SchedulerState:
    """Replay-ready immutable state with unique tenant and request identities."""

    SCHEMA_VERSION: ClassVar[int] = 2
    ENVELOPE_KIND: ClassVar[str] = "scheduler_state"

    global_fair: GlobalFairState = field(default_factory=GlobalFairState)
    current_time_ns: int = 0
    tenants: tuple[TenantState, ...] = ()
    requests: tuple[RequestState, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.global_fair, GlobalFairState):
            raise TypeError("global_fair must be a GlobalFairState")
        current_time = _integer(
            self.current_time_ns,
            field_name="current_time_ns",
        )
        if not isinstance(self.tenants, tuple):
            raise TypeError("tenants must be a tuple")
        if not all(isinstance(item, TenantState) for item in self.tenants):
            raise TypeError("tenants must contain TenantState values")
        tenant_ids = [item.tenant_id for item in self.tenants]
        if len(set(tenant_ids)) != len(tenant_ids):
            raise ValueError("scheduler state cannot contain duplicate tenant IDs")
        normalized_tenants = tuple(
            sorted(self.tenants, key=lambda item: item.tenant_id)
        )
        object.__setattr__(self, "tenants", normalized_tenants)

        if not isinstance(self.requests, tuple):
            raise TypeError("requests must be a tuple")
        if not all(isinstance(item, RequestState) for item in self.requests):
            raise TypeError("requests must contain RequestState values")
        request_ids = [item.spec.request_id for item in self.requests]
        if len(set(request_ids)) != len(request_ids):
            raise ValueError("scheduler state cannot contain duplicate request IDs")
        known_tenants = set(tenant_ids)
        unknown_tenants = {
            item.spec.tenant_id for item in self.requests
        } - known_tenants
        if unknown_tenants:
            raise ValueError(
                "requests reference unknown tenant IDs: "
                f"{sorted(unknown_tenants)}"
            )
        for request in self.requests:
            if request.spec.arrival_ns > current_time:
                raise ValueError(
                    "request arrival cannot exceed scheduler current_time_ns"
                )
            if (
                request.last_progress_ns is not None
                and request.last_progress_ns > current_time
            ):
                raise ValueError(
                    "request progress cannot exceed scheduler current_time_ns"
                )
        for tenant in normalized_tenants:
            for field_name, value in (
                ("last_active_ns", tenant.ledger.last_active_ns),
                (
                    "resource_debt_updated_ns",
                    tenant.ledger.resource_debt_updated_ns,
                ),
            ):
                if value is not None and value > current_time:
                    raise ValueError(
                        f"tenant {field_name} cannot exceed "
                        "scheduler current_time_ns"
                    )
        backlogged_tenants = {
            item.spec.tenant_id
            for item in self.requests
            if item.is_backlogged
        }
        active_tenants = {
            item.tenant_id for item in normalized_tenants if item.active
        }
        if active_tenants != backlogged_tenants:
            raise ValueError(
                "tenant active set must exactly equal the request backlog; "
                f"active={sorted(active_tenants)}, "
                f"backlogged={sorted(backlogged_tenants)}"
            )
        object.__setattr__(
            self,
            "requests",
            tuple(sorted(self.requests, key=lambda item: item.spec.request_id)),
        )

    def tenant(self, tenant_id: str) -> TenantState:
        tenant_id = _nonempty(tenant_id, field_name="tenant_id")
        for tenant in self.tenants:
            if tenant.tenant_id == tenant_id:
                return tenant
        raise KeyError(tenant_id)

    def request(self, request_id: str) -> RequestState:
        request_id = _nonempty(request_id, field_name="request_id")
        for request in self.requests:
            if request.spec.request_id == request_id:
                return request
        raise KeyError(request_id)

    def to_key(self) -> dict[str, Any]:
        return {
            "current_time_ns": self.current_time_ns,
            "global_fair": self.global_fair.to_key(),
            "requests": [
                item.to_key()
                for item in sorted(
                    self.requests,
                    key=lambda request: request.spec.stable_id,
                )
            ],
            "schema_version": self.SCHEMA_VERSION,
            "tenants": [
                item.to_key()
                for item in sorted(
                    self.tenants,
                    key=lambda tenant: tenant.tenant_id,
                )
            ],
        }

    @property
    def stable_id(self) -> str:
        return _versioned_stable_id(
            "sch",
            self.SCHEMA_VERSION,
            self.to_key(),
        )


@dataclass(frozen=True, slots=True)
class ResidencyState:
    """Logical device/host residency without runtime object references.

    A continuation present only on the device must be dirty.  Dirty means that
    any host copy is stale and a D2H transfer is required before discarding the
    last up-to-date device copy.
    """

    SCHEMA_VERSION: ClassVar[int] = 2
    ENVELOPE_KIND: ClassVar[str] = "residency_state"

    device_immutable_ids: tuple[str, ...] = ()
    device_continuation_ids: tuple[str, ...] = ()
    host_continuation_ids: tuple[str, ...] = ()
    dirty_continuation_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "device_immutable_ids",
            "device_continuation_ids",
            "host_continuation_ids",
            "dirty_continuation_ids",
        ):
            object.__setattr__(
                self,
                name,
                _normalized_ids(getattr(self, name), field_name=name),
            )
        device = set(self.device_continuation_ids)
        host = set(self.host_continuation_ids)
        dirty = set(self.dirty_continuation_ids)
        if not dirty <= device:
            raise ValueError(
                "dirty_continuation_ids must be a subset of device continuation IDs"
            )
        if not (device - host) <= dirty:
            raise ValueError("a device-only continuation must be marked dirty")

    def to_key(self) -> dict[str, Any]:
        return {
            "device_continuation_ids": list(self.device_continuation_ids),
            "device_immutable_ids": list(self.device_immutable_ids),
            "dirty_continuation_ids": list(self.dirty_continuation_ids),
            "host_continuation_ids": list(self.host_continuation_ids),
            "schema_version": self.SCHEMA_VERSION,
        }

    @property
    def stable_id(self) -> str:
        return _versioned_stable_id(
            "res",
            self.SCHEMA_VERSION,
            self.to_key(),
        )


@dataclass(frozen=True, slots=True)
class RequestAllocation:
    """One request's logical allocation inside a simulator action."""

    request_id: str
    quantum_steps: int
    quota_numerator: int
    quota_denominator: int
    tile_count: int

    def __post_init__(self) -> None:
        _nonempty(self.request_id, field_name="request_id")
        _integer(self.quantum_steps, field_name="quantum_steps", minimum=1)
        numerator = _integer(
            self.quota_numerator,
            field_name="quota_numerator",
            minimum=1,
        )
        denominator = _integer(
            self.quota_denominator,
            field_name="quota_denominator",
            minimum=1,
        )
        if numerator > denominator:
            raise ValueError("logical compute quota must not exceed one")
        common = gcd(numerator, denominator)
        object.__setattr__(self, "quota_numerator", numerator // common)
        object.__setattr__(self, "quota_denominator", denominator // common)
        _integer(self.tile_count, field_name="tile_count", minimum=1)

    @property
    def quota(self) -> ExactRatio:
        return ExactRatio(self.quota_numerator, self.quota_denominator)

    def to_key(self) -> dict[str, str | int]:
        return {
            "quantum_steps": self.quantum_steps,
            "quota_denominator": self.quota_denominator,
            "quota_numerator": self.quota_numerator,
            "request_id": self.request_id,
            "tile_count": self.tile_count,
        }


@dataclass(frozen=True, slots=True)
class Action:
    """A runtime-independent logical compute/residency decision.

    Quotas are logical shares, not NVIDIA SM/TPC masks.  A later, versioned
    runtime action schema must bind the logical allocation to measured masks.
    """

    SCHEMA_VERSION: ClassVar[int] = 2
    ENVELOPE_KIND: ClassVar[str] = "action"

    allocations: tuple[RequestAllocation, ...]
    target_residency: ResidencyState

    def __post_init__(self) -> None:
        if not isinstance(self.allocations, tuple):
            raise TypeError("allocations must be a tuple")
        if not all(
            isinstance(allocation, RequestAllocation)
            for allocation in self.allocations
        ):
            raise TypeError("allocations must contain RequestAllocation values")
        request_ids = [allocation.request_id for allocation in self.allocations]
        if len(set(request_ids)) != len(request_ids):
            raise ValueError("an action cannot allocate one request more than once")
        total_quota = sum(
            (
                Fraction(
                    allocation.quota_numerator,
                    allocation.quota_denominator,
                )
                for allocation in self.allocations
            ),
            Fraction(),
        )
        if total_quota > 1:
            raise ValueError("an action's total logical compute quota exceeds one")
        object.__setattr__(
            self,
            "allocations",
            tuple(sorted(self.allocations, key=lambda item: item.request_id)),
        )
        if not isinstance(self.target_residency, ResidencyState):
            raise TypeError("target_residency must be a ResidencyState")

    def to_key(self) -> dict[str, Any]:
        return {
            "allocations": [
                allocation.to_key() for allocation in self.allocations
            ],
            "schema_version": self.SCHEMA_VERSION,
            "target_residency_id": self.target_residency.stable_id,
        }

    @property
    def stable_id(self) -> str:
        return _versioned_stable_id(
            "act",
            self.SCHEMA_VERSION,
            self.to_key(),
        )

    @property
    def action_id(self) -> str:
        return self.stable_id

    @property
    def is_corun(self) -> bool:
        return len(self.allocations) > 1


@dataclass(frozen=True, slots=True)
class TenantResourceUsage:
    """Raw resource attribution for exactly one tenant in a quantum."""

    tenant_id: str
    usage: RawResourceUsage

    def __post_init__(self) -> None:
        _nonempty(self.tenant_id, field_name="tenant_id")
        if not isinstance(self.usage, RawResourceUsage):
            raise TypeError("usage must be a RawResourceUsage")

    def to_key(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "usage": self.usage.to_key(),
        }


@dataclass(frozen=True, slots=True)
class QuantumResult:
    """Observed result with strict raw-resource conservation.

    ``total_resource_usage`` is the independent device-level observation.
    Per-tenant entries are a separate attribution whose componentwise sum must
    equal that total exactly; state/action-aware participant checks live in the
    accounting validator.
    """

    SCHEMA_VERSION: ClassVar[int] = 2
    ENVELOPE_KIND: ClassVar[str] = "quantum_result"

    action_id: str
    started_ns: int
    finished_ns: int
    completed_steps: tuple[tuple[str, int], ...]
    total_resource_usage: RawResourceUsage
    resource_usage_by_tenant: tuple[TenantResourceUsage, ...] = ()
    success: bool = True
    error: str | None = None

    def __post_init__(self) -> None:
        _nonempty(self.action_id, field_name="action_id")
        _integer(self.started_ns, field_name="started_ns")
        _integer(self.finished_ns, field_name="finished_ns")
        if self.finished_ns < self.started_ns:
            raise ValueError("finished_ns cannot precede started_ns")
        if not isinstance(self.completed_steps, tuple):
            raise TypeError("completed_steps must be a tuple")
        normalized: list[tuple[str, int]] = []
        for item in self.completed_steps:
            if not isinstance(item, tuple) or len(item) != 2:
                raise TypeError(
                    "completed_steps entries must be (request_id, count) tuples"
                )
            request_id, count = item
            normalized.append(
                (
                    _nonempty(request_id, field_name="completed request_id"),
                    _integer(
                        count,
                        field_name="completed step count",
                        minimum=1,
                    ),
                )
            )
        request_ids = [request_id for request_id, _ in normalized]
        if len(set(request_ids)) != len(request_ids):
            raise ValueError("completed_steps contains a duplicate request")
        object.__setattr__(self, "completed_steps", tuple(sorted(normalized)))

        if not isinstance(self.resource_usage_by_tenant, tuple):
            raise TypeError("resource_usage_by_tenant must be a tuple")
        if not all(
            isinstance(item, TenantResourceUsage)
            for item in self.resource_usage_by_tenant
        ):
            raise TypeError(
                "resource_usage_by_tenant must contain TenantResourceUsage values"
            )
        tenant_ids = [item.tenant_id for item in self.resource_usage_by_tenant]
        if len(set(tenant_ids)) != len(tenant_ids):
            raise ValueError("resource attribution contains a duplicate tenant")
        object.__setattr__(
            self,
            "resource_usage_by_tenant",
            tuple(
                sorted(
                    self.resource_usage_by_tenant,
                    key=lambda item: item.tenant_id,
                )
            ),
        )
        if not isinstance(self.total_resource_usage, RawResourceUsage):
            raise TypeError("total_resource_usage must be a RawResourceUsage")
        attributed_total = sum(
            (item.usage for item in self.resource_usage_by_tenant),
            RawResourceUsage(),
        )
        if attributed_total != self.total_resource_usage:
            raise ValueError(
                "per-tenant resource attribution must exactly conserve the "
                "independently measured total_resource_usage"
            )
        if self.completed_steps and not self.resource_usage_by_tenant:
            raise ValueError(
                "completed work requires explicit per-tenant resource attribution"
            )
        if not isinstance(self.success, bool):
            raise TypeError("success must be a bool")
        if self.success and self.error is not None:
            raise ValueError("a successful quantum cannot carry an error")
        if not self.success:
            _nonempty(self.error, field_name="error")

    @property
    def elapsed_ns(self) -> int:
        return self.finished_ns - self.started_ns

    @property
    def resource_usage(self) -> RawResourceUsage:
        return self.total_resource_usage

    def to_key(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "completed_steps": [list(item) for item in self.completed_steps],
            "error": self.error,
            "finished_ns": self.finished_ns,
            "resource_usage_by_tenant": [
                item.to_key() for item in self.resource_usage_by_tenant
            ],
            "schema_version": self.SCHEMA_VERSION,
            "started_ns": self.started_ns,
            "success": self.success,
            "total_resource_usage": self.total_resource_usage.to_key(),
        }

    @property
    def stable_id(self) -> str:
        return _versioned_stable_id(
            "qrs",
            self.SCHEMA_VERSION,
            self.to_key(),
        )


_CANONICAL_SCALAR_TYPES = (str, int, bool, type(None))


@dataclass(frozen=True, slots=True)
class TraceEvent:
    """Versioned canonical input event suitable for byte-identical replay."""

    SCHEMA_VERSION: ClassVar[int] = 2
    ENVELOPE_KIND: ClassVar[str] = "trace_event"

    sequence: int
    timestamp_ns: int
    kind: str
    subject_id: str
    payload: tuple[tuple[str, str | int | bool | None], ...] = ()

    def __post_init__(self) -> None:
        _integer(self.sequence, field_name="sequence")
        _integer(self.timestamp_ns, field_name="timestamp_ns")
        _nonempty(self.kind, field_name="kind")
        _nonempty(self.subject_id, field_name="subject_id")
        if not isinstance(self.payload, tuple):
            raise TypeError("payload must be a tuple")
        normalized: list[tuple[str, str | int | bool | None]] = []
        for item in self.payload:
            if not isinstance(item, tuple) or len(item) != 2:
                raise TypeError("payload entries must be (key, scalar) tuples")
            key, value = item
            key = _nonempty(key, field_name="payload key")
            if not isinstance(value, _CANONICAL_SCALAR_TYPES):
                raise TypeError("payload values must be canonical scalar values")
            normalized.append((key, value))
        keys = [key for key, _ in normalized]
        if len(set(keys)) != len(keys):
            raise ValueError("payload must not contain duplicate keys")
        object.__setattr__(self, "payload", tuple(sorted(normalized)))

    def to_key(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "payload": [list(item) for item in self.payload],
            "schema_version": self.SCHEMA_VERSION,
            "sequence": self.sequence,
            "subject_id": self.subject_id,
            "timestamp_ns": self.timestamp_ns,
        }

    @property
    def stable_id(self) -> str:
        return _versioned_stable_id(
            "evt",
            self.SCHEMA_VERSION,
            self.to_key(),
        )


__all__ = [
    "Action",
    "CanonicalEnvelope",
    "ExactRatio",
    "GlobalFairState",
    "NANOSECONDS_PER_SECOND",
    "QuantumResult",
    "RawResourceUsage",
    "RequestAllocation",
    "RequestSpec",
    "RequestState",
    "ResidencyState",
    "ResourceCapacities",
    "ResourceTimeVector",
    "SIM_ENVELOPE_SCHEMA",
    "SIM_SCHEMA_VERSION",
    "SUPPORTED_SIM_SCHEMA_VERSIONS",
    "SchedulerState",
    "TenantLedger",
    "TenantPolicy",
    "TenantResourceUsage",
    "TenantState",
    "TraceEvent",
    "UnsupportedSchemaVersionError",
    "WorkloadSignature",
    "canonical_json",
    "decode_versioned",
    "encode_versioned",
]
