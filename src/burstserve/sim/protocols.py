"""Pure protocol boundaries between simulator semantics and future backends."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .model import (
    Action,
    QuantumResult,
    RequestState,
    ResidencyState,
    WorkloadSignature,
)


@runtime_checkable
class ProfileProvider(Protocol):
    """Read-only performance/profile interface consumed by scheduling logic.

    A deterministic dictionary-backed toy provider and a future SQLite-backed
    measured provider can implement the same contract.  Implementations must
    return integer nanoseconds/bytes and must not mutate request state.
    """

    def canonical_step_ns(
        self,
        signature: WorkloadSignature,
        step_index: int,
    ) -> int:
        """Return resident, full-device canonical solo time for one step."""

    def execution_ns(
        self,
        requests: tuple[RequestState, ...],
        action: Action,
    ) -> int:
        """Return predicted elapsed time for one complete action."""

    def remaining_p99_ns(
        self,
        request: RequestState,
        action: Action,
    ) -> int:
        """Return conservative remaining completion time under an action."""

    def transition_p99_ns(
        self,
        before: ResidencyState,
        after: ResidencyState,
    ) -> int:
        """Return conservative residency transition time."""

    def externality_ns(
        self,
        action: Action,
        active_requests: tuple[RequestState, ...],
    ) -> int:
        """Return predicted marginal delay imposed on other work."""

    def memory_peak_bytes(self, action: Action) -> int:
        """Return predicted peak device memory for feasibility filtering."""


@runtime_checkable
class Executor(Protocol):
    """Future execution boundary; the pure simulator provides no implementation."""

    def prepare(self, action: Action) -> None:
        """Prepare an action without executing a diffusion step."""

    def run_quantum(self, action: Action) -> QuantumResult:
        """Execute exactly one action quantum."""

    def suspend(self, request: RequestState) -> RequestState:
        """Return an immutable snapshot representing a suspended request."""

    def resume(self, request: RequestState) -> RequestState:
        """Return an immutable snapshot representing a resumed request."""

    def finalize(self, request: RequestState) -> None:
        """Release backend state after request completion or rejection."""


__all__ = ["Executor", "ProfileProvider"]
