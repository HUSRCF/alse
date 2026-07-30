"""Deterministic, versioned lifecycle traces for the pure-CPU simulator.

This module intentionally stops before scheduling.  A trace can introduce
tenants and requests, mark an application deadline, and assert an abstract
cancel/complete transition.  Replaying those records only advances the
immutable :class:`SchedulerState` lifecycle; it never chooses an ``Action``,
consults a profile, or executes runtime work.

The wire format is canonical UTF-8 JSONL.  It has one header, zero or more
sorted workload-signature definitions, and then a totally ordered event
stream.  Decoding is fail closed: duplicate JSON keys, non-canonical bytes,
unknown fields, invalid references, and semantically inconsistent generator
metadata are all rejected.

The v2 request model has a generic ``rejected`` terminal state but no distinct
``cancelled`` state.  Replay therefore projects ``request_cancel`` into that
generic terminal state.  The immutable ``TraceEvent.kind`` remains the source
of truth for client cancellation; admission/rejection metrics must never be
inferred from the projected ``RequestState.status``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from hashlib import sha256
from io import BytesIO
import json
from typing import Any, ClassVar, Iterator

from .model import (
    SIM_SCHEMA_VERSION,
    ExactRatio,
    GlobalFairState,
    RequestSpec,
    RequestState,
    SchedulerState,
    TenantLedger,
    TenantPolicy,
    TenantState,
    TraceEvent,
    WorkloadSignature,
    canonical_json,
)


TRACE_SCHEMA = "burstserve.sim.trace"
TRACE_SCHEMA_VERSION = 1
SUPPORTED_TRACE_SCHEMA_VERSIONS = frozenset({1})

MANUAL_TRACE_ALGORITHM_VERSION = "manual-lifecycle-v1"
ARRIVAL_GENERATOR_ALGORITHM_VERSION = (
    "splitmix64-uniform-interarrival-v1"
)

TENANT_ARRIVAL = "tenant_arrival"
REQUEST_ARRIVAL = "request_arrival"
REQUEST_DEADLINE = "request_deadline"
REQUEST_COMPLETE = "request_complete"
REQUEST_CANCEL = "request_cancel"

_EVENT_ORDER = {
    TENANT_ARRIVAL: 0,
    REQUEST_ARRIVAL: 1,
    REQUEST_DEADLINE: 2,
    REQUEST_COMPLETE: 3,
    REQUEST_CANCEL: 4,
}
_SUPPORTED_EVENT_KINDS = frozenset(_EVENT_ORDER)
_HEADER_FIELDS = frozenset(
    {
        "generator_algorithm",
        "generator_parameters",
        "generator_seed",
        "maximum_sleeper_credit_ns",
        "model_schema_version",
        "record_type",
        "schema",
        "schema_version",
    }
)
_SIGNATURE_RECORD_FIELDS = frozenset(
    {
        "record_type",
        "schema_version",
        "signature",
        "signature_id",
    }
)
_EVENT_RECORD_FIELDS = frozenset(
    {"event", "record_type", "schema_version"}
)
_TRACE_EVENT_FIELDS = frozenset(
    {
        "kind",
        "payload",
        "schema_version",
        "sequence",
        "subject_id",
        "timestamp_ns",
    }
)
_WORKLOAD_FIELDS = frozenset(
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
)
_TENANT_ARRIVAL_PAYLOAD_FIELDS = frozenset(
    {"weight_denominator", "weight_numerator"}
)
_REQUEST_ARRIVAL_PAYLOAD_FIELDS = frozenset(
    {"deadline_ns", "request_kind", "signature_id", "tenant_id"}
)
_GENERATOR_PARAMETER_FIELDS = frozenset(
    {
        "deadline_offset_ns",
        "interarrival_jitter_ns",
        "minimum_interarrival_ns",
        "request_count",
        "request_id_prefix",
        "request_kind",
        "start_ns",
    }
)

_UINT64_MASK = (1 << 64) - 1
_UINT64_SPACE = 1 << 64
_UINT64_DECIMAL = "18446744073709551615"
_UINT64_DECIMAL_DIGITS = len(_UINT64_DECIMAL)
_SPLITMIX64_INCREMENT = 0x9E3779B97F4A7C15
_SPLITMIX64_MULTIPLIER_1 = 0xBF58476D1CE4E5B9
_SPLITMIX64_MULTIPLIER_2 = 0x94D049BB133111EB

_MAX_TRACE_BYTES = 64 * 1024 * 1024
_MAX_TRACE_RECORDS = 1_000_000
_MAX_TRACE_LINE_BYTES = 2 * 1024 * 1024
_MAX_JSON_NESTING = 64
# A positive generated workload needs at least one header, one signature
# definition, and one tenant-arrival record in addition to two records per
# request (arrival + deadline).  Extra tenants/signatures are checked before
# event materialization by ``_canonical_inputs``.
_MAX_GENERATED_REQUESTS = (_MAX_TRACE_RECORDS - 3) // 2
# With one-byte request-kind/prefix/tenant IDs, one-digit integers, and the
# fixed 69-byte workload-signature ID, the canonical arrival and deadline
# records require at least 311 and 167 bytes including their LFs.  Header,
# signature, and tenant records are deliberately omitted, so this remains a
# conservative lower bound for every generated trace.
_MIN_GENERATED_REQUEST_WIRE_BYTES = 478

_SCALAR_TYPES = (str, int, bool, type(None))


class UnsupportedTraceSchemaVersionError(ValueError):
    """A lifecycle trace uses an unsupported schema or algorithm version."""


def _integer(
    value: Any,
    *,
    field_name: str,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    if value < minimum:
        raise ValueError(f"{field_name} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{field_name} must be at most {maximum}")
    return value


def _wire_uint64(value: Any, *, field_name: str) -> int:
    return _integer(
        value,
        field_name=field_name,
        maximum=_UINT64_MASK,
    )


def _nonempty(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not value.strip():
        raise ValueError(f"{field_name} must be non-empty")
    _validate_unicode_scalar_string(value, field_name=field_name)
    return value


def _validate_unicode_scalar_string(value: str, *, field_name: str) -> None:
    for character in value:
        codepoint = ord(character)
        if 0xD800 <= codepoint <= 0xDFFF:
            raise ValueError(
                f"{field_name} must contain Unicode scalar values only"
            )


def _validate_unicode_tree(value: Any, *, field_name: str = "JSON") -> None:
    if isinstance(value, str):
        _validate_unicode_scalar_string(value, field_name=field_name)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_unicode_tree(
                item,
                field_name=f"{field_name}[{index}]",
            )
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _validate_unicode_scalar_string(
                key,
                field_name=f"{field_name} key",
            )
            _validate_unicode_tree(
                item,
                field_name=f"{field_name}.{key}",
            )


def _validate_wire_integer_tree(
    value: Any,
    *,
    field_name: str = "trace wire value",
) -> None:
    """Require every programmatic/wire integer to be an unsigned 64-bit value."""

    if isinstance(value, bool) or value is None or isinstance(value, str):
        return
    if isinstance(value, int):
        _wire_uint64(value, field_name=field_name)
        return
    if isinstance(value, (tuple, list)):
        for index, item in enumerate(value):
            _validate_wire_integer_tree(
                item,
                field_name=f"{field_name}[{index}]",
            )
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _validate_wire_integer_tree(
                item,
                field_name=f"{field_name}.{key}",
            )


def _parse_bounded_json_integer(encoded: str) -> int:
    """Parse JSON integers without consulting ``sys.int_max_str_digits``."""

    negative = encoded.startswith("-")
    digits = encoded[1:] if negative else encoded
    if (
        not digits
        or any(character < "0" or character > "9" for character in digits)
    ):
        raise ValueError("invalid trace JSON integer")
    if (
        len(digits) > _UINT64_DECIMAL_DIGITS
        or (
            len(digits) == _UINT64_DECIMAL_DIGITS
            and digits > _UINT64_DECIMAL
        )
    ):
        raise ValueError("trace JSON integer magnitude exceeds uint64")
    value = 0
    for character in digits:
        value = value * 10 + (ord(character) - ord("0"))
    return -value if negative else value


def _strict_json_loads(encoded: str) -> Any:
    if not isinstance(encoded, str):
        raise TypeError("JSON record must be a string")

    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key!r}")
            result[key] = value
        return result

    def reject_float(_: str) -> Any:
        raise ValueError("floating-point values are forbidden in trace JSON")

    def reject_constant(_: str) -> Any:
        raise ValueError("non-finite values are forbidden in trace JSON")

    try:
        result = json.loads(
            encoded,
            object_pairs_hook=pairs_hook,
            parse_int=_parse_bounded_json_integer,
            parse_float=reject_float,
            parse_constant=reject_constant,
        )
    except json.JSONDecodeError as error:
        raise ValueError("invalid trace JSON record") from error
    except RecursionError as error:
        raise ValueError("trace JSON nesting exceeds the supported limit") from error
    try:
        _validate_unicode_tree(result)
        _validate_wire_integer_tree(result)
    except RecursionError as error:
        raise ValueError("trace JSON nesting exceeds the supported limit") from error
    return result


def _parameter_tuple(
    value: Any,
) -> tuple[tuple[str, str | int | bool | None], ...]:
    if not isinstance(value, tuple):
        raise TypeError("generator_parameters must be a tuple")
    normalized: list[tuple[str, str | int | bool | None]] = []
    for item in value:
        if not isinstance(item, tuple) or len(item) != 2:
            raise TypeError(
                "generator_parameters entries must be (key, scalar) tuples"
            )
        key, scalar = item
        key = _nonempty(key, field_name="generator parameter key")
        if not isinstance(scalar, _SCALAR_TYPES):
            raise TypeError("generator parameter values must be scalars")
        if isinstance(scalar, str):
            _validate_unicode_scalar_string(
                scalar,
                field_name=f"generator parameter {key}",
            )
        normalized.append((key, scalar))
    keys = [key for key, _ in normalized]
    if len(set(keys)) != len(keys):
        raise ValueError("generator_parameters must not repeat a key")
    return tuple(sorted(normalized))


def _parameters_dict(
    parameters: tuple[tuple[str, str | int | bool | None], ...],
) -> dict[str, str | int | bool | None]:
    return dict(parameters)


@dataclass(frozen=True, slots=True)
class ArrivalGeneratorConfig:
    """All inputs to the integer-only v1 workload arrival algorithm."""

    seed: int
    start_ns: int
    request_count: int
    minimum_interarrival_ns: int
    interarrival_jitter_ns: int
    deadline_offset_ns: int
    request_kind: str
    request_id_prefix: str = "request"

    ALGORITHM_VERSION: ClassVar[str] = (
        ARRIVAL_GENERATOR_ALGORITHM_VERSION
    )

    def __post_init__(self) -> None:
        _integer(
            self.seed,
            field_name="seed",
            maximum=_UINT64_MASK,
        )
        _wire_uint64(self.start_ns, field_name="start_ns")
        _integer(
            self.request_count,
            field_name="request_count",
            maximum=_MAX_GENERATED_REQUESTS,
        )
        _integer(
            self.minimum_interarrival_ns,
            field_name="minimum_interarrival_ns",
            minimum=1,
            maximum=_UINT64_MASK,
        )
        _integer(
            self.interarrival_jitter_ns,
            field_name="interarrival_jitter_ns",
            maximum=_UINT64_MASK,
        )
        _integer(
            self.deadline_offset_ns,
            field_name="deadline_offset_ns",
            minimum=1,
            maximum=_UINT64_MASK,
        )
        _nonempty(self.request_kind, field_name="request_kind")
        _nonempty(self.request_id_prefix, field_name="request_id_prefix")

    def parameter_tuple(
        self,
    ) -> tuple[tuple[str, str | int | bool | None], ...]:
        return tuple(
            sorted(
                {
                    "deadline_offset_ns": self.deadline_offset_ns,
                    "interarrival_jitter_ns": self.interarrival_jitter_ns,
                    "minimum_interarrival_ns": self.minimum_interarrival_ns,
                    "request_count": self.request_count,
                    "request_id_prefix": self.request_id_prefix,
                    "request_kind": self.request_kind,
                    "start_ns": self.start_ns,
                }.items()
            )
        )


@dataclass(frozen=True, slots=True)
class TraceHeader:
    """The trace contract and deterministic generator provenance."""

    generator_algorithm: str = MANUAL_TRACE_ALGORITHM_VERSION
    generator_seed: int | None = None
    generator_parameters: tuple[
        tuple[str, str | int | bool | None], ...
    ] = ()
    maximum_sleeper_credit_ns: int = 0

    SCHEMA_VERSION: ClassVar[int] = TRACE_SCHEMA_VERSION
    MODEL_SCHEMA_VERSION: ClassVar[int] = SIM_SCHEMA_VERSION

    def __post_init__(self) -> None:
        algorithm = _nonempty(
            self.generator_algorithm,
            field_name="generator_algorithm",
        )
        if algorithm not in {
            MANUAL_TRACE_ALGORITHM_VERSION,
            ARRIVAL_GENERATOR_ALGORITHM_VERSION,
        }:
            raise UnsupportedTraceSchemaVersionError(
                f"unsupported trace generator algorithm: {algorithm!r}"
            )
        parameters = _parameter_tuple(self.generator_parameters)
        object.__setattr__(self, "generator_parameters", parameters)
        _wire_uint64(
            self.maximum_sleeper_credit_ns,
            field_name="maximum_sleeper_credit_ns",
        )

        if algorithm == MANUAL_TRACE_ALGORITHM_VERSION:
            if self.generator_seed is not None:
                raise ValueError("manual traces must not carry a generator seed")
            if parameters:
                raise ValueError(
                    "manual traces must not carry generator parameters"
                )
            return

        seed = _integer(
            self.generator_seed,
            field_name="generator_seed",
            maximum=_UINT64_MASK,
        )
        parameter_map = _parameters_dict(parameters)
        if set(parameter_map) != _GENERATOR_PARAMETER_FIELDS:
            raise ValueError(
                "arrival-generator parameters do not match the v1 contract"
            )
        ArrivalGeneratorConfig(
            seed=seed,
            start_ns=parameter_map["start_ns"],
            request_count=parameter_map["request_count"],
            minimum_interarrival_ns=parameter_map[
                "minimum_interarrival_ns"
            ],
            interarrival_jitter_ns=parameter_map[
                "interarrival_jitter_ns"
            ],
            deadline_offset_ns=parameter_map["deadline_offset_ns"],
            request_kind=parameter_map["request_kind"],
            request_id_prefix=parameter_map["request_id_prefix"],
        )

    @classmethod
    def from_generator(
        cls,
        config: ArrivalGeneratorConfig,
        *,
        maximum_sleeper_credit_ns: int = 0,
    ) -> "TraceHeader":
        if not isinstance(config, ArrivalGeneratorConfig):
            raise TypeError("config must be an ArrivalGeneratorConfig")
        return cls(
            generator_algorithm=config.ALGORITHM_VERSION,
            generator_seed=config.seed,
            generator_parameters=config.parameter_tuple(),
            maximum_sleeper_credit_ns=maximum_sleeper_credit_ns,
        )

    def generator_config(self) -> ArrivalGeneratorConfig | None:
        if self.generator_algorithm == MANUAL_TRACE_ALGORITHM_VERSION:
            return None
        parameters = _parameters_dict(self.generator_parameters)
        return ArrivalGeneratorConfig(
            seed=self.generator_seed,
            start_ns=parameters["start_ns"],
            request_count=parameters["request_count"],
            minimum_interarrival_ns=parameters[
                "minimum_interarrival_ns"
            ],
            interarrival_jitter_ns=parameters[
                "interarrival_jitter_ns"
            ],
            deadline_offset_ns=parameters["deadline_offset_ns"],
            request_kind=parameters["request_kind"],
            request_id_prefix=parameters["request_id_prefix"],
        )

    def to_record(self) -> dict[str, Any]:
        return {
            "generator_algorithm": self.generator_algorithm,
            "generator_parameters": [
                [key, value] for key, value in self.generator_parameters
            ],
            "generator_seed": self.generator_seed,
            "maximum_sleeper_credit_ns": self.maximum_sleeper_credit_ns,
            "model_schema_version": self.MODEL_SCHEMA_VERSION,
            "record_type": "header",
            "schema": TRACE_SCHEMA,
            "schema_version": self.SCHEMA_VERSION,
        }


@dataclass(frozen=True, slots=True)
class TraceDocument:
    """A semantically closed, canonically ordered lifecycle trace."""

    header: TraceHeader = field(default_factory=TraceHeader)
    signatures: tuple[WorkloadSignature, ...] = ()
    events: tuple[TraceEvent, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.header, TraceHeader):
            raise TypeError("header must be a TraceHeader")
        if not isinstance(self.signatures, tuple):
            raise TypeError("signatures must be a tuple")
        if not all(
            isinstance(signature, WorkloadSignature)
            for signature in self.signatures
        ):
            raise TypeError(
                "signatures must contain WorkloadSignature values"
            )
        if not isinstance(self.events, tuple):
            raise TypeError("events must be a tuple")
        if not all(isinstance(event, TraceEvent) for event in self.events):
            raise TypeError("events must contain TraceEvent values")
        _validate_trace_document(self)

    @property
    def stable_id(self) -> str:
        digest = sha256()
        for chunk in _iter_document_record_chunks(self):
            digest.update(chunk)
        return "trc1-" + digest.hexdigest()


@dataclass(frozen=True, slots=True)
class ReplayFrame:
    """Bounded incremental state changes from one lifecycle event.

    A frame retains at most the one request and one tenant changed by its
    event.  It intentionally does not retain a full ``SchedulerState``;
    ``TraceReplayResult.final_state`` is materialized once after replay.
    """

    event: TraceEvent
    request_state_after: RequestState | None = None
    tenant_state_after: TenantState | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.event, TraceEvent):
            raise TypeError("event must be a TraceEvent")
        if self.event.kind not in _SUPPORTED_EVENT_KINDS:
            raise ValueError(
                f"unsupported lifecycle event: {self.event.kind!r}"
            )
        if (
            self.request_state_after is not None
            and not isinstance(self.request_state_after, RequestState)
        ):
            raise TypeError("request_state_after must be a RequestState")
        if (
            self.tenant_state_after is not None
            and not isinstance(self.tenant_state_after, TenantState)
        ):
            raise TypeError("tenant_state_after must be a TenantState")

        if self.event.kind == TENANT_ARRIVAL:
            if self.request_state_after is not None:
                raise ValueError(
                    "tenant_arrival cannot carry a request-state delta"
                )
            if self.tenant_state_after is None:
                raise ValueError(
                    "tenant_arrival must carry a tenant-state delta"
                )
            if self.tenant_state_after.policy != _policy_from_event(self.event):
                raise ValueError(
                    "tenant-arrival delta does not match the event policy"
                )
            if self.tenant_state_after.active:
                raise ValueError(
                    "a newly arrived tenant cannot be active before backlog"
                )
            if self.tenant_state_after.ledger != TenantLedger(
                tenant_id=self.event.subject_id
            ):
                raise ValueError(
                    "a newly arrived tenant must start with a pristine ledger"
                )
        elif self.event.kind == REQUEST_DEADLINE:
            if (
                self.request_state_after is not None
                or self.tenant_state_after is not None
            ):
                raise ValueError(
                    "request_deadline cannot carry lifecycle state deltas"
                )
            if self.event.payload:
                raise ValueError("request_deadline payload must be empty")
        else:
            request = self.request_state_after
            if request is None:
                raise ValueError(
                    f"{self.event.kind} must carry a request-state delta"
                )
            if request.spec.request_id != self.event.subject_id:
                raise ValueError(
                    "replay request delta must belong to the event subject"
                )
            if request.spec.arrival_ns > self.event.timestamp_ns:
                raise ValueError(
                    "replay request delta cannot precede request arrival"
                )
            if (
                request.last_progress_ns is not None
                and request.last_progress_ns > self.event.timestamp_ns
            ):
                raise ValueError(
                    "request progress cannot exceed the replay event time"
                )
            if self.event.kind == REQUEST_ARRIVAL:
                expected_spec = _request_from_event(
                    self.event,
                    {request.spec.signature.stable_id: request.spec.signature},
                )
                if request.spec != expected_spec:
                    raise ValueError(
                        "request-arrival delta does not match the event"
                    )
                if (
                    request.status != "queued"
                    or request.completed_steps != 0
                    or request.last_progress_ns is not None
                ):
                    raise ValueError(
                        "request_arrival must produce a fresh queued request"
                    )
            else:
                if self.event.payload:
                    raise ValueError(
                        f"{self.event.kind} payload must be empty"
                    )
                if self.event.kind == REQUEST_CANCEL:
                    if request.status != "rejected":
                        raise ValueError(
                            "request_cancel must produce a rejected request"
                        )
                elif (
                    request.status != "completed"
                    or request.completed_steps
                    != request.spec.signature.total_steps
                    or request.last_progress_ns != self.event.timestamp_ns
                ):
                    raise ValueError(
                        "request_complete must produce a fully completed "
                        "request at the event timestamp"
                    )

        if self.tenant_state_after is not None:
            expected_tenant_id = (
                self.event.subject_id
                if self.event.kind == TENANT_ARRIVAL
                else (
                    self.request_state_after.spec.tenant_id
                    if self.request_state_after is not None
                    else None
                )
            )
            if self.tenant_state_after.tenant_id != expected_tenant_id:
                raise ValueError(
                    "replay tenant delta does not match the event transition"
                )
            expected_active = self.event.kind == REQUEST_ARRIVAL
            if self.event.kind != TENANT_ARRIVAL and (
                self.tenant_state_after.active != expected_active
            ):
                raise ValueError(
                    "replay tenant delta has the wrong activation state"
                )
            for field_name, timestamp_ns in (
                (
                    "last_active_ns",
                    self.tenant_state_after.ledger.last_active_ns,
                ),
                (
                    "resource_debt_updated_ns",
                    self.tenant_state_after.ledger.resource_debt_updated_ns,
                ),
            ):
                if (
                    timestamp_ns is not None
                    and timestamp_ns > self.event.timestamp_ns
                ):
                    raise ValueError(
                        f"tenant {field_name} cannot exceed the replay "
                        "event time"
                    )

    @property
    def current_time_ns(self) -> int:
        return self.event.timestamp_ns


@dataclass(frozen=True, slots=True)
class TraceReplayResult:
    """A document-bound, self-verifying lifecycle replay result."""

    document: TraceDocument
    frames: tuple[ReplayFrame, ...]
    final_state: SchedulerState
    trace_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.document, TraceDocument):
            raise TypeError("document must be a TraceDocument")
        if not isinstance(self.frames, tuple):
            raise TypeError("frames must be a tuple")
        if not all(isinstance(frame, ReplayFrame) for frame in self.frames):
            raise TypeError("frames must contain ReplayFrame values")
        if not isinstance(self.final_state, SchedulerState):
            raise TypeError("final_state must be a SchedulerState")
        if len(self.frames) != len(self.document.events):
            raise ValueError(
                "replay frames must cover every document event exactly once"
            )

        verifier = _ReplayBuilder(self.document)
        for event, frame in zip(self.document.events, self.frames):
            if frame.event != event:
                raise ValueError(
                    "replay frame events must match the bound document"
                )
            if frame != verifier.apply(event):
                raise ValueError(
                    "replay frame delta does not match the lifecycle "
                    "transition"
                )

        expected_tenants = tuple(
            sorted(
                verifier.tenants.values(),
                key=lambda tenant: tenant.tenant_id,
            )
        )
        expected_requests = tuple(
            sorted(
                verifier.requests.values(),
                key=lambda request: request.spec.request_id,
            )
        )
        if (
            self.final_state.global_fair != verifier.global_fair
            or self.final_state.current_time_ns != verifier.current_time_ns
            or self.final_state.tenants != expected_tenants
            or self.final_state.requests != expected_requests
        ):
            raise ValueError(
                "final_state does not match the replayed lifecycle deltas"
            )
        object.__setattr__(self, "trace_id", self.document.stable_id)


def make_tenant_arrival(
    *,
    sequence: int,
    timestamp_ns: int,
    policy: TenantPolicy,
) -> TraceEvent:
    if not isinstance(policy, TenantPolicy):
        raise TypeError("policy must be a TenantPolicy")
    _wire_uint64(sequence, field_name="sequence")
    _wire_uint64(timestamp_ns, field_name="timestamp_ns")
    _validate_wire_integer_tree(
        policy.to_key(),
        field_name="tenant arrival policy",
    )
    return TraceEvent(
        sequence=sequence,
        timestamp_ns=timestamp_ns,
        kind=TENANT_ARRIVAL,
        subject_id=policy.tenant_id,
        payload=(
            ("weight_denominator", policy.weight_denominator),
            ("weight_numerator", policy.weight_numerator),
        ),
    )


def make_request_arrival(
    *,
    sequence: int,
    spec: RequestSpec,
) -> TraceEvent:
    if not isinstance(spec, RequestSpec):
        raise TypeError("spec must be a RequestSpec")
    _wire_uint64(sequence, field_name="sequence")
    _validate_wire_integer_tree(
        spec.signature.to_key(),
        field_name="request arrival signature",
    )
    _validate_wire_integer_tree(
        spec.to_key(),
        field_name="request arrival spec",
    )
    return TraceEvent(
        sequence=sequence,
        timestamp_ns=spec.arrival_ns,
        kind=REQUEST_ARRIVAL,
        subject_id=spec.request_id,
        payload=(
            ("deadline_ns", spec.deadline_ns),
            ("request_kind", spec.kind),
            ("signature_id", spec.signature.stable_id),
            ("tenant_id", spec.tenant_id),
        ),
    )


def make_request_deadline(
    *,
    sequence: int,
    timestamp_ns: int,
    request_id: str,
) -> TraceEvent:
    return _terminal_or_deadline_event(
        sequence=sequence,
        timestamp_ns=timestamp_ns,
        request_id=request_id,
        kind=REQUEST_DEADLINE,
    )


def make_request_complete(
    *,
    sequence: int,
    timestamp_ns: int,
    request_id: str,
) -> TraceEvent:
    return _terminal_or_deadline_event(
        sequence=sequence,
        timestamp_ns=timestamp_ns,
        request_id=request_id,
        kind=REQUEST_COMPLETE,
    )


def make_request_cancel(
    *,
    sequence: int,
    timestamp_ns: int,
    request_id: str,
) -> TraceEvent:
    return _terminal_or_deadline_event(
        sequence=sequence,
        timestamp_ns=timestamp_ns,
        request_id=request_id,
        kind=REQUEST_CANCEL,
    )


def _terminal_or_deadline_event(
    *,
    sequence: int,
    timestamp_ns: int,
    request_id: str,
    kind: str,
) -> TraceEvent:
    _wire_uint64(sequence, field_name="sequence")
    _wire_uint64(timestamp_ns, field_name="timestamp_ns")
    return TraceEvent(
        sequence=sequence,
        timestamp_ns=timestamp_ns,
        kind=kind,
        subject_id=request_id,
    )


def event_order_key(event: TraceEvent) -> tuple[int, int, str, str]:
    """Return the frozen total-order key, excluding assigned sequence."""

    if not isinstance(event, TraceEvent):
        raise TypeError("event must be a TraceEvent")
    try:
        priority = _EVENT_ORDER[event.kind]
    except KeyError as error:
        raise ValueError(f"unsupported lifecycle event: {event.kind!r}") from error
    return (
        event.timestamp_ns,
        priority,
        event.subject_id,
        canonical_json(event.to_key()["payload"]),
    )


class _SplitMix64:
    """Small pinned PRNG; its transition is part of the algorithm version."""

    __slots__ = ("_state",)

    def __init__(self, seed: int) -> None:
        self._state = _integer(
            seed,
            field_name="seed",
            maximum=_UINT64_MASK,
        )

    def next_u64(self) -> int:
        self._state = (
            self._state + _SPLITMIX64_INCREMENT
        ) & _UINT64_MASK
        value = self._state
        value = (
            (value ^ (value >> 30)) * _SPLITMIX64_MULTIPLIER_1
        ) & _UINT64_MASK
        value = (
            (value ^ (value >> 27)) * _SPLITMIX64_MULTIPLIER_2
        ) & _UINT64_MASK
        return value ^ (value >> 31)

    def uniform_below(self, bound: int) -> int:
        bound = _integer(
            bound,
            field_name="bound",
            minimum=1,
            maximum=_UINT64_SPACE,
        )
        if bound == _UINT64_SPACE:
            return self.next_u64()
        acceptance_limit = _UINT64_SPACE - (_UINT64_SPACE % bound)
        while True:
            candidate = self.next_u64()
            if candidate < acceptance_limit:
                return candidate % bound


def _canonical_inputs(
    tenants: tuple[TenantPolicy, ...],
    signatures: tuple[WorkloadSignature, ...],
    *,
    request_count: int,
) -> tuple[tuple[TenantPolicy, ...], tuple[WorkloadSignature, ...]]:
    if not isinstance(tenants, tuple):
        raise TypeError("tenants must be a tuple")
    if not all(isinstance(item, TenantPolicy) for item in tenants):
        raise TypeError("tenants must contain TenantPolicy values")
    for index, tenant in enumerate(tenants):
        _validate_wire_integer_tree(
            tenant.to_key(),
            field_name=f"generator tenant[{index}]",
        )
    tenant_ids = [item.tenant_id for item in tenants]
    if len(set(tenant_ids)) != len(tenant_ids):
        raise ValueError("generator tenants must have unique IDs")

    if not isinstance(signatures, tuple):
        raise TypeError("signatures must be a tuple")
    if not all(
        isinstance(item, WorkloadSignature) for item in signatures
    ):
        raise TypeError(
            "signatures must contain WorkloadSignature values"
        )
    for index, signature in enumerate(signatures):
        _validate_wire_integer_tree(
            signature.to_key(),
            field_name=f"generator signature[{index}]",
        )
    signature_ids = [item.stable_id for item in signatures]
    if len(set(signature_ids)) != len(signature_ids):
        raise ValueError("generator signatures must be unique")
    if request_count and (not tenants or not signatures):
        raise ValueError(
            "positive request_count requires tenants and signatures"
        )
    final_record_count = (
        1 + len(signatures) + len(tenants) + 2 * request_count
    )
    if final_record_count > _MAX_TRACE_RECORDS:
        raise ValueError(
            "generator inputs exceed the trace record-count limit"
        )
    return (
        tuple(sorted(tenants, key=lambda item: item.tenant_id)),
        tuple(sorted(signatures, key=lambda item: item.stable_id)),
    )


def _iter_generated_request_specs(
    config: ArrivalGeneratorConfig,
    tenants: tuple[TenantPolicy, ...],
    signatures: tuple[WorkloadSignature, ...],
) -> Iterator[RequestSpec]:
    tenants, signatures = _canonical_inputs(
        tenants,
        signatures,
        request_count=config.request_count,
    )
    prng = _SplitMix64(config.seed)
    timestamp_ns = config.start_ns
    for index in range(config.request_count):
        delta_ns = (
            config.minimum_interarrival_ns
            + prng.uniform_below(config.interarrival_jitter_ns + 1)
        )
        if (
            delta_ns > _UINT64_MASK
            or timestamp_ns > _UINT64_MASK - delta_ns
        ):
            raise ValueError(
                "generated request timestamp exceeds uint64"
            )
        timestamp_ns += delta_ns
        if timestamp_ns > _UINT64_MASK - config.deadline_offset_ns:
            raise ValueError(
                "generated request deadline exceeds uint64"
            )
        tenant = tenants[prng.uniform_below(len(tenants))]
        signature = signatures[prng.uniform_below(len(signatures))]
        request_id = f"{config.request_id_prefix}-{index:08d}"
        yield RequestSpec(
            request_id=request_id,
            tenant_id=tenant.tenant_id,
            signature=signature,
            arrival_ns=timestamp_ns,
            deadline_ns=timestamp_ns + config.deadline_offset_ns,
            kind=config.request_kind,
        )


def _iter_generated_events(
    config: ArrivalGeneratorConfig,
    tenants: tuple[TenantPolicy, ...],
    signatures: tuple[WorkloadSignature, ...],
) -> Iterator[TraceEvent]:
    """Yield canonical generated events with O(1) merge lookahead."""

    tenants, signatures = _canonical_inputs(
        tenants,
        signatures,
        request_count=config.request_count,
    )
    sequence = 0
    for tenant in tenants:
        yield make_tenant_arrival(
            sequence=sequence,
            timestamp_ns=config.start_ns,
            policy=tenant,
        )
        sequence += 1

    arrival_specs = _iter_generated_request_specs(
        config,
        tenants,
        signatures,
    )
    deadline_specs = _iter_generated_request_specs(
        config,
        tenants,
        signatures,
    )
    arrival_spec = next(arrival_specs, None)
    deadline_spec = next(deadline_specs, None)
    while arrival_spec is not None or deadline_spec is not None:
        arrival_event = (
            make_request_arrival(sequence=0, spec=arrival_spec)
            if arrival_spec is not None
            else None
        )
        deadline_event = (
            make_request_deadline(
                sequence=0,
                timestamp_ns=deadline_spec.deadline_ns,
                request_id=deadline_spec.request_id,
            )
            if deadline_spec is not None
            else None
        )
        if deadline_event is None or (
            arrival_event is not None
            and event_order_key(arrival_event)
            <= event_order_key(deadline_event)
        ):
            yield replace(arrival_event, sequence=sequence)
            sequence += 1
            arrival_spec = next(arrival_specs, None)
        else:
            yield replace(deadline_event, sequence=sequence)
            sequence += 1
            deadline_spec = next(deadline_specs, None)


def _generate_events(
    config: ArrivalGeneratorConfig,
    tenants: tuple[TenantPolicy, ...],
    signatures: tuple[WorkloadSignature, ...],
) -> tuple[TraceEvent, ...]:
    return tuple(_iter_generated_events(config, tenants, signatures))


def generate_arrival_trace(
    config: ArrivalGeneratorConfig,
    *,
    tenants: tuple[TenantPolicy, ...],
    signatures: tuple[WorkloadSignature, ...],
    maximum_sleeper_credit_ns: int = 0,
) -> TraceDocument:
    """Generate a byte-stable integer-only workload arrival trace."""

    if not isinstance(config, ArrivalGeneratorConfig):
        raise TypeError("config must be an ArrivalGeneratorConfig")
    _preflight_generated_trace_minimum_size(config.request_count)
    canonical_tenants, canonical_signatures = _canonical_inputs(
        tenants,
        signatures,
        request_count=config.request_count,
    )
    header = TraceHeader.from_generator(
        config,
        maximum_sleeper_credit_ns=maximum_sleeper_credit_ns,
    )
    _preflight_generated_trace_size(
        header,
        canonical_signatures,
        config,
        canonical_tenants,
    )
    return TraceDocument(
        header=header,
        signatures=canonical_signatures,
        events=_generate_events(
            config,
            canonical_tenants,
            canonical_signatures,
        ),
    )


def _event_payload(event: TraceEvent) -> dict[str, Any]:
    return dict(event.payload)


def _policy_from_event(event: TraceEvent) -> TenantPolicy:
    payload = _event_payload(event)
    if set(payload) != _TENANT_ARRIVAL_PAYLOAD_FIELDS:
        raise ValueError(
            "tenant_arrival payload fields do not match the v1 contract"
        )
    policy = TenantPolicy(
        tenant_id=event.subject_id,
        weight_numerator=payload["weight_numerator"],
        weight_denominator=payload["weight_denominator"],
    )
    expected = {
        "weight_denominator": policy.weight_denominator,
        "weight_numerator": policy.weight_numerator,
    }
    if payload != expected:
        raise ValueError("tenant_arrival weight must be normalized")
    return policy


def _request_from_event(
    event: TraceEvent,
    signatures: dict[str, WorkloadSignature],
) -> RequestSpec:
    payload = _event_payload(event)
    if set(payload) != _REQUEST_ARRIVAL_PAYLOAD_FIELDS:
        raise ValueError(
            "request_arrival payload fields do not match the v1 contract"
        )
    signature_id = _nonempty(
        payload["signature_id"],
        field_name="request_arrival.signature_id",
    )
    try:
        signature = signatures[signature_id]
    except KeyError as error:
        raise ValueError(
            "request_arrival references an unknown signature"
        ) from error
    return RequestSpec(
        request_id=event.subject_id,
        tenant_id=payload["tenant_id"],
        signature=signature,
        arrival_ns=event.timestamp_ns,
        deadline_ns=payload["deadline_ns"],
        kind=payload["request_kind"],
    )


def _validate_trace_document(document: TraceDocument) -> None:
    _validate_wire_integer_tree(
        document.header.to_record(),
        field_name="trace header",
    )
    for index, signature in enumerate(document.signatures):
        _validate_wire_integer_tree(
            signature.to_key(),
            field_name=f"trace signature[{index}]",
        )
    for index, event in enumerate(document.events):
        _validate_wire_integer_tree(
            event.to_key(),
            field_name=f"trace event[{index}]",
        )

    signature_ids = [
        signature.stable_id for signature in document.signatures
    ]
    if len(set(signature_ids)) != len(signature_ids):
        raise ValueError("trace contains duplicate workload signatures")
    if signature_ids != sorted(signature_ids):
        raise ValueError(
            "workload signatures must be sorted by signature_id"
        )
    signatures = dict(zip(signature_ids, document.signatures))

    if len(document.events) > _MAX_TRACE_RECORDS - 1 - len(signature_ids):
        raise ValueError("trace exceeds the record-count limit")
    previous_order_key: tuple[int, int, str, str] | None = None
    for expected_sequence, event in enumerate(document.events):
        current_order_key = event_order_key(event)
        if (
            previous_order_key is not None
            and current_order_key < previous_order_key
        ):
            raise ValueError(
                "events must use canonical timestamp/kind/subject ordering"
            )
        previous_order_key = current_order_key
        if event.sequence != expected_sequence:
            raise ValueError(
                "event sequences must be contiguous and start at zero"
            )
    _validate_document_byte_budget(document)

    policies: dict[str, TenantPolicy] = {}
    requests: dict[str, RequestSpec] = {}
    deadline_events: set[str] = set()
    terminal_events: set[str] = set()
    for event in document.events:
        if event.kind not in _SUPPORTED_EVENT_KINDS:
            raise ValueError(
                f"unsupported lifecycle event: {event.kind!r}"
            )
        if event.kind == TENANT_ARRIVAL:
            policy = _policy_from_event(event)
            if policy.tenant_id in policies:
                raise ValueError("tenant cannot arrive more than once")
            policies[policy.tenant_id] = policy
            continue
        if event.kind == REQUEST_ARRIVAL:
            spec = _request_from_event(event, signatures)
            if spec.request_id in requests:
                raise ValueError("request cannot arrive more than once")
            if spec.tenant_id not in policies:
                raise ValueError(
                    "request_arrival references a tenant that has not arrived"
                )
            requests[spec.request_id] = spec
            continue

        if event.payload:
            raise ValueError(f"{event.kind} payload must be empty")
        try:
            request = requests[event.subject_id]
        except KeyError as error:
            raise ValueError(
                f"{event.kind} references a request that has not arrived"
            ) from error
        if event.kind == REQUEST_DEADLINE:
            if request.deadline_ns is None:
                raise ValueError(
                    "request_deadline references a request without a deadline"
                )
            if event.timestamp_ns != request.deadline_ns:
                raise ValueError(
                    "request_deadline timestamp must equal deadline_ns"
                )
            if request.request_id in deadline_events:
                raise ValueError(
                    "request cannot have more than one deadline event"
                )
            deadline_events.add(request.request_id)
            continue

        if request.request_id in terminal_events:
            raise ValueError(
                "request cannot have more than one terminal event"
            )
        terminal_events.add(request.request_id)

    expected_deadlines = {
        request_id
        for request_id, request in requests.items()
        if request.deadline_ns is not None
    }
    if deadline_events != expected_deadlines:
        missing = sorted(expected_deadlines - deadline_events)
        extra = sorted(deadline_events - expected_deadlines)
        raise ValueError(
            "deadline event closure mismatch: "
            f"missing={missing}, extra={extra}"
        )

    config = document.header.generator_config()
    if config is not None:
        # Reject a small inconsistent wire artifact before materializing the
        # generator's claimed event count.  Without this cross-check, a header
        # near the record cap plus one tenant/signature could amplify into
        # roughly one million in-memory events during validation.
        if len(requests) != config.request_count:
            raise ValueError(
                "generated trace request_count does not match observed "
                "request_arrival records"
            )
        expected_count = 0
        for index, expected_event in enumerate(
            _iter_generated_events(
                config,
                tuple(policies.values()),
                document.signatures,
            )
        ):
            if (
                index >= len(document.events)
                or document.events[index] != expected_event
            ):
                raise ValueError(
                    "generated trace events do not match "
                    "seed/algorithm/parameters"
                )
            expected_count = index + 1
        if expected_count != len(document.events):
            raise ValueError(
                "generated trace events do not match seed/algorithm/parameters"
            )


def _signature_record(signature: WorkloadSignature) -> dict[str, Any]:
    return {
        "record_type": "workload_signature",
        "schema_version": TRACE_SCHEMA_VERSION,
        "signature": signature.to_key(),
        "signature_id": signature.stable_id,
    }


def _event_record(event: TraceEvent) -> dict[str, Any]:
    return {
        "event": event.to_key(),
        "record_type": "event",
        "schema_version": TRACE_SCHEMA_VERSION,
    }


def _canonical_record_payload(record: dict[str, Any]) -> bytes:
    _validate_wire_integer_tree(record)
    try:
        payload = canonical_json(record).encode(
            "utf-8",
            errors="strict",
        )
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise ValueError(
            "trace record is not canonically encodable"
        ) from error
    if len(payload) > _MAX_TRACE_LINE_BYTES:
        raise ValueError("trace record exceeds the line-size limit")
    return payload


def _canonical_record_chunk(record: dict[str, Any]) -> bytes:
    return _canonical_record_payload(record) + b"\n"


def _iter_canonical_record_chunks(
    records: Iterator[dict[str, Any]],
) -> Iterator[bytes]:
    """Yield canonical lines while enforcing the exact aggregate byte cap."""

    total_bytes = 0
    for record in records:
        chunk = _canonical_record_chunk(record)
        total_bytes += len(chunk)
        if total_bytes > _MAX_TRACE_BYTES:
            raise ValueError("encoded trace exceeds the byte-size limit")
        yield chunk


def _preflight_generated_trace_minimum_size(request_count: int) -> None:
    """Reject an impossible byte budget without touching generator streams."""

    count = _integer(
        request_count,
        field_name="request_count",
        maximum=_MAX_GENERATED_REQUESTS,
    )
    if count > _MAX_TRACE_BYTES // _MIN_GENERATED_REQUEST_WIRE_BYTES:
        raise ValueError("encoded trace exceeds the byte-size limit")


def _preflight_generated_trace_size(
    header: TraceHeader,
    signatures: tuple[WorkloadSignature, ...],
    config: ArrivalGeneratorConfig,
    tenants: tuple[TenantPolicy, ...],
) -> int:
    """Stream exact canonical sizes before the generated tuple is allocated."""

    def records() -> Iterator[dict[str, Any]]:
        yield header.to_record()
        for signature in signatures:
            yield _signature_record(signature)
        for event in _iter_generated_events(config, tenants, signatures):
            yield _event_record(event)

    return sum(
        len(chunk)
        for chunk in _iter_canonical_record_chunks(records())
    )


def _iter_document_records(
    document: TraceDocument,
) -> Iterator[dict[str, Any]]:
    yield document.header.to_record()
    for signature in document.signatures:
        yield _signature_record(signature)
    for event in document.events:
        yield _event_record(event)


def _validate_document_byte_budget(document: TraceDocument) -> int:
    return sum(len(chunk) for chunk in _iter_document_record_chunks(document))


def _iter_document_record_chunks(
    document: TraceDocument,
) -> Iterator[bytes]:
    return _iter_canonical_record_chunks(_iter_document_records(document))


def encode_trace_jsonl(document: TraceDocument) -> bytes:
    """Encode one validated trace to canonical UTF-8 JSONL bytes."""

    if not isinstance(document, TraceDocument):
        raise TypeError("document must be a TraceDocument")
    destination = BytesIO()
    for chunk in _iter_document_record_chunks(document):
        destination.write(chunk)
    return destination.getvalue()


def _decode_header(record: Any) -> TraceHeader:
    if not isinstance(record, dict) or set(record) != _HEADER_FIELDS:
        raise ValueError("trace header fields do not match the v1 schema")
    if record["record_type"] != "header":
        raise ValueError("the first trace record must be a header")
    if record["schema"] != TRACE_SCHEMA:
        raise ValueError("unsupported trace schema")
    version = record["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise TypeError("trace schema_version must be an integer")
    if version not in SUPPORTED_TRACE_SCHEMA_VERSIONS:
        raise UnsupportedTraceSchemaVersionError(
            f"unsupported trace schema version: {version}"
        )
    if record["model_schema_version"] != SIM_SCHEMA_VERSION:
        raise UnsupportedTraceSchemaVersionError(
            "trace model_schema_version is unsupported"
        )
    parameters = record["generator_parameters"]
    if not isinstance(parameters, list):
        raise ValueError("generator_parameters must use a JSON list")
    decoded_parameters: list[
        tuple[str, str | int | bool | None]
    ] = []
    for item in parameters:
        if not isinstance(item, list) or len(item) != 2:
            raise ValueError(
                "generator_parameters entries must be two-item JSON lists"
            )
        decoded_parameters.append((item[0], item[1]))
    header = TraceHeader(
        generator_algorithm=record["generator_algorithm"],
        generator_seed=record["generator_seed"],
        generator_parameters=tuple(decoded_parameters),
        maximum_sleeper_credit_ns=record[
            "maximum_sleeper_credit_ns"
        ],
    )
    if header.to_record() != record:
        raise ValueError("trace header is not canonical")
    return header


def _workload_from_record(record: Any) -> WorkloadSignature:
    if (
        not isinstance(record, dict)
        or set(record) != _SIGNATURE_RECORD_FIELDS
    ):
        raise ValueError(
            "workload-signature record fields do not match the v1 schema"
        )
    if record["record_type"] != "workload_signature":
        raise ValueError("invalid workload-signature record_type")
    record_version = record["schema_version"]
    if (
        isinstance(record_version, bool)
        or not isinstance(record_version, int)
        or record_version != TRACE_SCHEMA_VERSION
    ):
        raise UnsupportedTraceSchemaVersionError(
            "unsupported workload-signature record version"
        )
    payload = record["signature"]
    if not isinstance(payload, dict) or set(payload) != _WORKLOAD_FIELDS:
        raise ValueError(
            "embedded workload signature fields do not match model v2"
        )
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
        raise ValueError("embedded workload signature is not canonical")
    if record["signature_id"] != signature.stable_id:
        raise ValueError("workload signature_id does not match its payload")
    return signature


def _event_from_record(record: Any) -> TraceEvent:
    if not isinstance(record, dict) or set(record) != _EVENT_RECORD_FIELDS:
        raise ValueError("event record fields do not match the v1 schema")
    if record["record_type"] != "event":
        raise ValueError("invalid event record_type")
    record_version = record["schema_version"]
    if (
        isinstance(record_version, bool)
        or not isinstance(record_version, int)
        or record_version != TRACE_SCHEMA_VERSION
    ):
        raise UnsupportedTraceSchemaVersionError(
            "unsupported event record version"
        )
    payload = record["event"]
    if not isinstance(payload, dict) or set(payload) != _TRACE_EVENT_FIELDS:
        raise ValueError("embedded event fields do not match model v2")
    wire_payload = payload["payload"]
    if not isinstance(wire_payload, list):
        raise ValueError("event payload must use a JSON list")
    entries: list[tuple[str, str | int | bool | None]] = []
    for item in wire_payload:
        if not isinstance(item, list) or len(item) != 2:
            raise ValueError(
                "event payload entries must be two-item JSON lists"
            )
        entries.append((item[0], item[1]))
    event = TraceEvent(
        sequence=payload["sequence"],
        timestamp_ns=payload["timestamp_ns"],
        kind=payload["kind"],
        subject_id=payload["subject_id"],
        payload=tuple(entries),
    )
    if event.to_key() != payload:
        raise ValueError("embedded event is not canonical")
    return event


def _prescan_trace_wire(encoded: bytes) -> int:
    """Validate raw JSONL framing and nesting in one O(1)-space byte pass."""

    record_count = 0
    line_bytes = 0
    nesting = 0
    in_string = False
    escaped = False
    for byte in encoded:
        if byte == 0x0D:
            raise ValueError("canonical trace JSONL must use LF line endings")
        if byte == 0x0A:
            if line_bytes == 0:
                raise ValueError("trace must not contain blank JSONL records")
            if in_string or nesting != 0:
                raise ValueError("invalid trace JSON record")
            record_count += 1
            if record_count > _MAX_TRACE_RECORDS:
                raise ValueError("trace exceeds the record-count limit")
            line_bytes = 0
            nesting = 0
            in_string = False
            escaped = False
            continue

        line_bytes += 1
        if line_bytes > _MAX_TRACE_LINE_BYTES:
            raise ValueError("trace record exceeds the line-size limit")
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:
                escaped = True
            elif byte == 0x22:
                in_string = False
            continue
        if byte == 0x22:
            in_string = True
        elif byte in (0x5B, 0x7B):
            nesting += 1
            if nesting > _MAX_JSON_NESTING:
                raise ValueError(
                    "trace JSON nesting exceeds the supported limit"
                )
        elif byte in (0x5D, 0x7D):
            nesting -= 1
            if nesting < 0:
                raise ValueError("invalid trace JSON record")

    if line_bytes != 0:
        raise ValueError("canonical trace JSONL must end with one newline")
    if record_count == 0:
        raise ValueError("encoded trace must be non-empty")
    return record_count


def _iter_wire_record_payloads(
    encoded: bytes,
    record_count: int,
) -> Iterator[bytes]:
    start = 0
    for _ in range(record_count):
        end = encoded.find(b"\n", start)
        if end < 0:
            raise ValueError("canonical trace JSONL framing changed after scan")
        yield encoded[start:end]
        start = end + 1


def _decode_trace_jsonl_impl(encoded: bytes) -> TraceDocument:
    if not isinstance(encoded, bytes):
        raise TypeError("encoded trace must be bytes")
    if not encoded:
        raise ValueError("encoded trace must be non-empty")
    if len(encoded) > _MAX_TRACE_BYTES:
        raise ValueError("encoded trace exceeds the byte-size limit")
    if encoded.startswith(b"\xef\xbb\xbf"):
        raise ValueError("UTF-8 BOM is not canonical")
    record_count = _prescan_trace_wire(encoded)

    header: TraceHeader | None = None
    signatures: list[WorkloadSignature] = []
    events: list[TraceEvent] = []
    event_phase = False
    for record_index, line_bytes in enumerate(
        _iter_wire_record_payloads(encoded, record_count)
    ):
        try:
            line = line_bytes.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise ValueError("trace is not valid UTF-8") from error
        record = _strict_json_loads(line)
        try:
            canonical = _canonical_record_payload(record)
        except (TypeError, ValueError, UnicodeEncodeError) as error:
            raise ValueError(
                "trace record is not canonically encodable"
            ) from error
        if canonical != line_bytes:
            raise ValueError("trace record does not use canonical JSON")
        if record_index == 0:
            header = _decode_header(record)
            continue
        if not isinstance(record, dict):
            raise ValueError("every JSONL record must be an object")
        record_type = record.get("record_type")
        if record_type == "workload_signature":
            if event_phase:
                raise ValueError(
                    "workload signatures must precede all event records"
                )
            signatures.append(_workload_from_record(record))
        elif record_type == "event":
            event_phase = True
            events.append(_event_from_record(record))
        else:
            raise ValueError(f"unsupported trace record_type: {record_type!r}")
    if header is None:
        raise ValueError("the first trace record must be a header")
    return TraceDocument(
        header=header,
        signatures=tuple(signatures),
        events=tuple(events),
    )


def decode_trace_jsonl(encoded: bytes) -> TraceDocument:
    """Decode canonical JSONL with bounded per-record working memory."""

    try:
        return _decode_trace_jsonl_impl(encoded)
    except RecursionError as error:
        raise ValueError(
            "trace JSON nesting exceeds the supported limit"
        ) from error


class _ReplayBuilder:
    """Mutable O(1)-expected-time lifecycle replay accumulator."""

    __slots__ = (
        "backlog_by_tenant",
        "current_time_ns",
        "global_fair",
        "maximum_credit_ns",
        "requests",
        "signatures",
        "tenants",
    )

    def __init__(self, document: TraceDocument) -> None:
        self.global_fair = GlobalFairState()
        self.current_time_ns = 0
        self.maximum_credit_ns = (
            document.header.maximum_sleeper_credit_ns
        )
        self.signatures = {
            signature.stable_id: signature
            for signature in document.signatures
        }
        self.tenants: dict[str, TenantState] = {}
        self.requests: dict[str, RequestState] = {}
        self.backlog_by_tenant: dict[str, int] = {}

    def _advance(self, timestamp_ns: int) -> None:
        timestamp = _wire_uint64(timestamp_ns, field_name="timestamp_ns")
        if timestamp < self.current_time_ns:
            raise ValueError("replay timestamp cannot regress")
        self.current_time_ns = timestamp

    def _wake_tenant(self, tenant: TenantState) -> TenantState:
        zero_lag = (
            tenant.policy.weight * self.global_fair.virtual_time
        )
        credit_floor = max(
            ExactRatio(),
            zero_lag - ExactRatio(self.maximum_credit_ns),
        )
        ledger = replace(
            tenant.ledger,
            fair_service_coordinate=max(
                tenant.ledger.fair_service_coordinate,
                credit_floor,
            ),
        )
        return replace(tenant, ledger=ledger, active=True)

    def apply(self, event: TraceEvent) -> ReplayFrame:
        self._advance(event.timestamp_ns)
        request_after: RequestState | None = None
        tenant_after: TenantState | None = None

        if event.kind == TENANT_ARRIVAL:
            policy = _policy_from_event(event)
            if policy.tenant_id in self.tenants:
                raise ValueError("tenant cannot arrive more than once")
            tenant_after = TenantState(
                policy=policy,
                ledger=TenantLedger(
                    tenant_id=policy.tenant_id,
                    fair_service_coordinate=(
                        policy.weight * self.global_fair.virtual_time
                    ),
                ),
                active=False,
            )
            self.tenants[policy.tenant_id] = tenant_after
            self.backlog_by_tenant[policy.tenant_id] = 0

        elif event.kind == REQUEST_ARRIVAL:
            spec = _request_from_event(event, self.signatures)
            if spec.request_id in self.requests:
                raise ValueError("request cannot arrive more than once")
            try:
                tenant = self.tenants[spec.tenant_id]
            except KeyError as error:
                raise ValueError(
                    "request_arrival references a tenant that has not arrived"
                ) from error
            request_after = RequestState(spec=spec, status="queued")
            self.requests[spec.request_id] = request_after
            backlog = self.backlog_by_tenant[spec.tenant_id]
            if backlog == 0:
                tenant_after = self._wake_tenant(tenant)
                self.tenants[spec.tenant_id] = tenant_after
            self.backlog_by_tenant[spec.tenant_id] = backlog + 1

        elif event.kind in (REQUEST_CANCEL, REQUEST_COMPLETE):
            try:
                request = self.requests[event.subject_id]
            except KeyError as error:
                raise ValueError(
                    f"{event.kind} references a request that has not arrived"
                ) from error
            if request.is_terminal:
                raise ValueError(
                    "terminal request cannot receive another terminal event"
                )
            if event.kind == REQUEST_CANCEL:
                request_after = replace(request, status="rejected")
            else:
                request_after = replace(
                    request,
                    completed_steps=request.spec.signature.total_steps,
                    status="completed",
                    last_progress_ns=event.timestamp_ns,
                )
            self.requests[event.subject_id] = request_after
            tenant_id = request.spec.tenant_id
            backlog = self.backlog_by_tenant[tenant_id] - 1
            if backlog < 0:
                raise ValueError("tenant replay backlog became negative")
            self.backlog_by_tenant[tenant_id] = backlog
            tenant = self.tenants[tenant_id]
            if backlog == 0 and tenant.active:
                tenant_after = replace(tenant, active=False)
                self.tenants[tenant_id] = tenant_after

        elif event.kind == REQUEST_DEADLINE:
            if event.subject_id not in self.requests:
                raise ValueError(
                    "request_deadline references a request that has not arrived"
                )
        else:
            raise ValueError(
                f"unsupported lifecycle event: {event.kind!r}"
            )

        return ReplayFrame(
            event=event,
            request_state_after=request_after,
            tenant_state_after=tenant_after,
        )

    def final_state(self) -> SchedulerState:
        return SchedulerState(
            global_fair=self.global_fair,
            current_time_ns=self.current_time_ns,
            tenants=tuple(self.tenants.values()),
            requests=tuple(self.requests.values()),
        )


def replay_trace(document: TraceDocument) -> TraceReplayResult:
    """Replay lifecycle deltas and materialize one final scheduler state.

    ``request_cancel`` uses the model's generic ``rejected`` terminal
    projection.  Consumers that distinguish client cancellation from
    admission rejection must inspect the preserved event kind, not status.
    """

    if not isinstance(document, TraceDocument):
        raise TypeError("document must be a TraceDocument")
    builder = _ReplayBuilder(document)
    frames: list[ReplayFrame] = []
    for event in document.events:
        frames.append(builder.apply(event))
    final_state = builder.final_state()
    return TraceReplayResult(
        document=document,
        frames=tuple(frames),
        final_state=final_state,
    )


__all__ = [
    "ARRIVAL_GENERATOR_ALGORITHM_VERSION",
    "ArrivalGeneratorConfig",
    "MANUAL_TRACE_ALGORITHM_VERSION",
    "REQUEST_ARRIVAL",
    "REQUEST_CANCEL",
    "REQUEST_COMPLETE",
    "REQUEST_DEADLINE",
    "ReplayFrame",
    "SUPPORTED_TRACE_SCHEMA_VERSIONS",
    "TENANT_ARRIVAL",
    "TRACE_SCHEMA",
    "TRACE_SCHEMA_VERSION",
    "TraceDocument",
    "TraceHeader",
    "TraceReplayResult",
    "UnsupportedTraceSchemaVersionError",
    "decode_trace_jsonl",
    "encode_trace_jsonl",
    "event_order_key",
    "generate_arrival_trace",
    "make_request_arrival",
    "make_request_cancel",
    "make_request_complete",
    "make_request_deadline",
    "make_tenant_arrival",
    "replay_trace",
]
