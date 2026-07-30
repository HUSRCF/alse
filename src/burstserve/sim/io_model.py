"""Byte-accurate residency transition model for the pure simulator.

Immutable model objects and mutable continuation state are intentionally
separate.  Evicting weights never fabricates a D2H copy: weights may be
discarded only when a valid host shadow is known.  Dirty continuations, in
contrast, must be copied back before their last up-to-date device copy is lost.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any, ClassVar, Iterable

from .model import (
    RawResourceUsage,
    ResidencyState,
    SchedulerState,
)


_PCIE_TRANSFERS_PER_SECOND = {
    1: 2_500_000_000,
    2: 5_000_000_000,
    3: 8_000_000_000,
    4: 16_000_000_000,
    5: 32_000_000_000,
}


def _integer(value: Any, *, field_name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    if value < minimum:
        raise ValueError(f"{field_name} must be at least {minimum}")
    return value


def _nonempty(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not value.strip():
        raise ValueError(f"{field_name} must be non-empty")
    return value


@dataclass(frozen=True, slots=True)
class ImmutableObject:
    """Immutable device object with an explicit reload source."""

    SCHEMA_VERSION: ClassVar[int] = 2

    object_id: str
    size_bytes: int
    host_shadow_available: bool = True

    def __post_init__(self) -> None:
        _nonempty(self.object_id, field_name="object_id")
        _integer(self.size_bytes, field_name="size_bytes", minimum=1)
        if not isinstance(self.host_shadow_available, bool):
            raise TypeError("host_shadow_available must be a bool")

    def to_key(self) -> dict[str, str | int | bool]:
        return {
            "host_shadow_available": self.host_shadow_available,
            "object_id": self.object_id,
            "schema_version": self.SCHEMA_VERSION,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True, slots=True)
class ContinuationObject:
    """Mutable request continuation with an explicit lifecycle owner."""

    SCHEMA_VERSION: ClassVar[int] = 2

    state_id: str
    size_bytes: int
    request_id: str

    def __post_init__(self) -> None:
        _nonempty(self.state_id, field_name="state_id")
        _integer(self.size_bytes, field_name="size_bytes", minimum=1)
        _nonempty(self.request_id, field_name="request_id")

    def to_key(self) -> dict[str, str | int]:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "request_id": self.request_id,
            "size_bytes": self.size_bytes,
            "state_id": self.state_id,
        }


@dataclass(frozen=True, slots=True)
class ResidencyTransition:
    """Exact traffic required to move between two logical residency states."""

    SCHEMA_VERSION: ClassVar[int] = 2

    kind: str
    immutable_h2d_bytes: int = 0
    immutable_d2h_bytes: int = 0
    continuation_h2d_bytes: int = 0
    continuation_d2h_bytes: int = 0

    def __post_init__(self) -> None:
        if self.kind not in {
            "resident",
            "zero_transfer",
            "state_only",
            "cold",
        }:
            raise ValueError(f"unsupported transition kind: {self.kind!r}")
        for name in (
            "immutable_h2d_bytes",
            "immutable_d2h_bytes",
            "continuation_h2d_bytes",
            "continuation_d2h_bytes",
        ):
            _integer(getattr(self, name), field_name=name)
        if self.immutable_d2h_bytes != 0:
            raise ValueError("immutable objects must never create D2H traffic")
        if self.kind in {"resident", "zero_transfer"} and self.total_bytes != 0:
            raise ValueError(
                "resident and zero_transfer transitions must have zero traffic"
            )
        if self.kind == "state_only":
            if self.immutable_h2d_bytes != 0 or (
                self.continuation_h2d_bytes + self.continuation_d2h_bytes == 0
            ):
                raise ValueError(
                    "state_only transitions require only continuation traffic"
                )
        if self.kind == "cold" and self.immutable_h2d_bytes == 0:
            raise ValueError("cold transitions require immutable H2D traffic")

    @property
    def total_h2d_bytes(self) -> int:
        return self.immutable_h2d_bytes + self.continuation_h2d_bytes

    @property
    def total_d2h_bytes(self) -> int:
        return self.immutable_d2h_bytes + self.continuation_d2h_bytes

    @property
    def total_bytes(self) -> int:
        return self.total_h2d_bytes + self.total_d2h_bytes

    @property
    def resource_vector(self) -> RawResourceUsage:
        return RawResourceUsage(
            pcie_h2d_bytes=self.total_h2d_bytes,
            pcie_d2h_bytes=self.total_d2h_bytes,
        )

    def to_key(self) -> dict[str, str | int]:
        return {
            "continuation_d2h_bytes": self.continuation_d2h_bytes,
            "continuation_h2d_bytes": self.continuation_h2d_bytes,
            "immutable_d2h_bytes": self.immutable_d2h_bytes,
            "immutable_h2d_bytes": self.immutable_h2d_bytes,
            "kind": self.kind,
            "schema_version": self.SCHEMA_VERSION,
        }


class TransitionInfeasibleError(ValueError):
    """The requested state cannot be reached without inventing or losing data."""


def _catalog(
    objects: Iterable[ImmutableObject] | Iterable[ContinuationObject],
    *,
    expected_type: type[ImmutableObject] | type[ContinuationObject],
    id_attribute: str,
    name: str,
) -> dict[str, ImmutableObject | ContinuationObject]:
    result: dict[str, ImmutableObject | ContinuationObject] = {}
    for item in objects:
        if not isinstance(item, expected_type):
            raise TypeError(f"{name} must contain {expected_type.__name__} values")
        item_id = getattr(item, id_attribute)
        if item_id in result:
            raise ValueError(f"{name} contains duplicate ID {item_id!r}")
        result[item_id] = item
    return result


def calculate_transition(
    before: ResidencyState,
    after: ResidencyState,
    *,
    immutable_objects: Iterable[ImmutableObject],
    continuation_objects: Iterable[ContinuationObject],
    discard_continuation_ids: tuple[str, ...] = (),
    lifecycle_state: SchedulerState | None = None,
) -> ResidencyTransition:
    """Calculate minimal truthful traffic between residency snapshots.

    Losing a continuation's final device/host copy is never inferred from byte
    state alone.  It requires an exact ID list and a matching completed or
    rejected owner in the supplied scheduler lifecycle state; unused or
    incomplete authorization fails closed.
    """

    if not isinstance(before, ResidencyState):
        raise TypeError("before must be a ResidencyState")
    if not isinstance(after, ResidencyState):
        raise TypeError("after must be a ResidencyState")
    immutable = _catalog(
        immutable_objects,
        expected_type=ImmutableObject,
        id_attribute="object_id",
        name="immutable_objects",
    )
    continuation = _catalog(
        continuation_objects,
        expected_type=ContinuationObject,
        id_attribute="state_id",
        name="continuation_objects",
    )
    if not isinstance(discard_continuation_ids, tuple):
        raise TypeError("discard_continuation_ids must be a tuple")
    discard_ids = tuple(
        _nonempty(item, field_name="discard continuation ID")
        for item in discard_continuation_ids
    )
    if len(set(discard_ids)) != len(discard_ids):
        raise ValueError("discard_continuation_ids contains duplicates")
    discard = set(discard_ids)
    if lifecycle_state is not None and not isinstance(
        lifecycle_state,
        SchedulerState,
    ):
        raise TypeError("lifecycle_state must be a SchedulerState")

    referenced_immutable = set(before.device_immutable_ids) | set(
        after.device_immutable_ids
    )
    unknown_immutable = referenced_immutable - immutable.keys()
    if unknown_immutable:
        raise ValueError(
            f"residency references unknown immutable IDs: {sorted(unknown_immutable)}"
        )
    referenced_continuation = (
        set(before.device_continuation_ids)
        | set(before.host_continuation_ids)
        | set(before.dirty_continuation_ids)
        | set(after.device_continuation_ids)
        | set(after.host_continuation_ids)
        | set(after.dirty_continuation_ids)
    )
    unknown_continuation = referenced_continuation - continuation.keys()
    if unknown_continuation:
        raise ValueError(
            "residency references unknown continuation IDs: "
            f"{sorted(unknown_continuation)}"
        )
    before_continuation = set(before.device_continuation_ids) | set(
        before.host_continuation_ids
    )
    invented_continuation = (
        set(after.device_continuation_ids) | set(after.host_continuation_ids)
    ) - before_continuation
    if invented_continuation:
        raise TransitionInfeasibleError(
            "transition has no source for new continuation IDs: "
            f"{sorted(invented_continuation)}"
        )

    after_continuation = set(after.device_continuation_ids) | set(
        after.host_continuation_ids
    )
    lost_last_copy = before_continuation - after_continuation
    if lost_last_copy != discard:
        missing = sorted(lost_last_copy - discard)
        unused = sorted(discard - lost_last_copy)
        raise TransitionInfeasibleError(
            "continuation last-copy discard requires an exact explicit "
            f"authorization; missing={missing}, unused={unused}"
        )
    if discard and lifecycle_state is None:
        raise TransitionInfeasibleError(
            "continuation discard requires a scheduler lifecycle state"
        )
    for state_id in discard:
        item = continuation.get(state_id)
        if not isinstance(item, ContinuationObject):
            raise ValueError(
                f"discard references unknown continuation ID {state_id!r}"
            )
        assert lifecycle_state is not None
        try:
            request = lifecycle_state.request(item.request_id)
        except KeyError as error:
            raise TransitionInfeasibleError(
                f"continuation {state_id!r} owner {item.request_id!r} "
                "is absent from the scheduler lifecycle state"
            ) from error
        if not request.is_terminal:
            raise TransitionInfeasibleError(
                f"continuation {state_id!r} owner {item.request_id!r} "
                "is not completed/rejected"
            )

    before_immutable = set(before.device_immutable_ids)
    after_immutable = set(after.device_immutable_ids)
    immutable_h2d_ids = after_immutable - before_immutable
    for object_id in immutable_h2d_ids:
        item = immutable[object_id]
        assert isinstance(item, ImmutableObject)
        if not item.host_shadow_available:
            raise TransitionInfeasibleError(
                f"immutable object {object_id!r} has no host reload source"
            )
    for object_id in before_immutable - after_immutable:
        item = immutable[object_id]
        assert isinstance(item, ImmutableObject)
        if not item.host_shadow_available:
            raise TransitionInfeasibleError(
                f"immutable object {object_id!r} cannot be safely discarded"
            )

    before_device = set(before.device_continuation_ids)
    before_host = set(before.host_continuation_ids)
    before_dirty = set(before.dirty_continuation_ids)
    after_device = set(after.device_continuation_ids)
    after_host = set(after.host_continuation_ids)
    after_dirty = set(after.dirty_continuation_ids)

    continuation_h2d_ids = after_device - before_device
    missing_sources = continuation_h2d_ids - before_host
    if missing_sources:
        raise TransitionInfeasibleError(
            "continuation H2D has no host source for IDs: "
            f"{sorted(missing_sources)}"
        )

    continuation_d2h_ids = {
        state_id
        for state_id in before_dirty
        if state_id in after_host and state_id not in after_dirty
    }

    immutable_h2d_bytes = sum(
        immutable[object_id].size_bytes for object_id in immutable_h2d_ids
    )
    continuation_h2d_bytes = sum(
        continuation[state_id].size_bytes
        for state_id in continuation_h2d_ids
    )
    continuation_d2h_bytes = sum(
        continuation[state_id].size_bytes
        for state_id in continuation_d2h_ids
    )
    if immutable_h2d_bytes:
        kind = "cold"
    elif continuation_h2d_bytes or continuation_d2h_bytes:
        kind = "state_only"
    elif before != after:
        # A pure eviction or metadata/dirty-state transition can require no PCIe
        # bytes, but calling it "resident" would falsely imply no state change.
        kind = "zero_transfer"
    else:
        kind = "resident"
    return ResidencyTransition(
        kind=kind,
        immutable_h2d_bytes=immutable_h2d_bytes,
        immutable_d2h_bytes=0,
        continuation_h2d_bytes=continuation_h2d_bytes,
        continuation_d2h_bytes=continuation_d2h_bytes,
    )


def pcie_payload_rate_bytes_per_second(
    *,
    generation: int,
    lanes: int,
    efficiency_numerator: int = 1,
    efficiency_denominator: int = 1,
) -> Fraction:
    """Return exact one-direction PCIe payload rate after link efficiency."""

    generation = _integer(generation, field_name="generation", minimum=1)
    if generation not in _PCIE_TRANSFERS_PER_SECOND:
        raise ValueError(f"unsupported PCIe generation: {generation}")
    lanes = _integer(lanes, field_name="lanes", minimum=1)
    numerator = _integer(
        efficiency_numerator,
        field_name="efficiency_numerator",
        minimum=1,
    )
    denominator = _integer(
        efficiency_denominator,
        field_name="efficiency_denominator",
        minimum=1,
    )
    if numerator > denominator:
        raise ValueError("PCIe efficiency cannot exceed one")
    encoding = Fraction(8, 10) if generation <= 2 else Fraction(128, 130)
    return (
        Fraction(_PCIE_TRANSFERS_PER_SECOND[generation] * lanes, 8)
        * encoding
        * Fraction(numerator, denominator)
    )


def _ceil_fraction(value: Fraction) -> int:
    return (value.numerator + value.denominator - 1) // value.denominator


def cold_transfer_time_ns(
    size_bytes: int,
    *,
    generation: int = 4,
    lanes: int = 16,
    efficiency_numerator: int = 7,
    efficiency_denominator: int = 10,
) -> int:
    """Estimate a one-direction *cold fixture* transfer, not every switch.

    The default represents PCIe 4.0 x16 at 70% effective payload bandwidth.
    Resident and state-only transitions must use their actual byte counts
    rather than charging this cold fixture unconditionally.
    """

    size = _integer(size_bytes, field_name="size_bytes")
    rate = pcie_payload_rate_bytes_per_second(
        generation=generation,
        lanes=lanes,
        efficiency_numerator=efficiency_numerator,
        efficiency_denominator=efficiency_denominator,
    )
    return _ceil_fraction(Fraction(size * 1_000_000_000, 1) / rate)


def serialized_cold_swap_time_ns(
    *,
    d2h_bytes: int,
    h2d_bytes: int,
    generation: int = 4,
    lanes: int = 16,
    efficiency_numerator: int = 7,
    efficiency_denominator: int = 10,
) -> int:
    """Estimate a deliberately serialized D2H-then-H2D cold-swap fixture."""

    common = {
        "generation": generation,
        "lanes": lanes,
        "efficiency_numerator": efficiency_numerator,
        "efficiency_denominator": efficiency_denominator,
    }
    return cold_transfer_time_ns(d2h_bytes, **common) + cold_transfer_time_ns(
        h2d_bytes,
        **common,
    )


__all__ = [
    "ContinuationObject",
    "ImmutableObject",
    "ResidencyTransition",
    "TransitionInfeasibleError",
    "calculate_transition",
    "cold_transfer_time_ns",
    "pcie_payload_rate_bytes_per_second",
    "serialized_cold_swap_time_ns",
]
