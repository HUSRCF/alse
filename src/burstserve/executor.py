"""Denoising as a state machine the scheduler drives one step at a time.

The ASLE baseline serves an urgent request by calling ``run_urgent()``
inside a pipeline callback, which blocks until that request finishes.
Nothing can be decided in between: the scheduler has no point at which
to change a quota, admit another tenant, or stop. plan.md's week 7-8
work is to replace that with executors exposing ``prepare``,
``run_step``, ``suspend``, ``resume`` and ``finalize``, so the decision
points are explicit and land on step boundaries.

The property everything else rests on is that suspension is invisible in
the output. A run interrupted after any step, resumed, and finished must
produce exactly what an uninterrupted run produces -- not approximately,
and not "within tolerance". Gate C's acceptance asks for a latent hash
identical to the ASLE seed's in deterministic mode, and a scheduler that
perturbs results when it preempts cannot deliver that however good its
decisions are.

That is why suspension captures the whole of what the next step reads:
the latent, the step index, and the generator state. Saving the latent
alone is the mistake this design exists to avoid -- the sampler advances
its own RNG per step, so a resume without it diverges from step one and
does so silently, since the output is still a plausible image.

This module is deliberately free of CUDA. The state machine, its
invariants and its failure modes are testable on a CPU, and a real
adapter is a subclass that fills in the three tensor operations.
"""

from __future__ import annotations

import enum
import hashlib
from dataclasses import dataclass, field
from typing import Any, Protocol


class Phase(enum.Enum):
    """Where a request is in its lifecycle.

    Ordered so that an illegal transition is a comparison rather than a
    table lookup: a request only ever moves forward, except RUNNING and
    SUSPENDED which alternate.
    """

    CREATED = 0
    PREPARED = 1
    RUNNING = 2
    SUSPENDED = 3
    FINISHED = 4
    FAILED = 5


class ExecutorError(RuntimeError):
    """An illegal transition, named rather than assert-ed.

    A scheduler bug that calls run_step on a finished request should
    fail loudly at the call, not corrupt a ledger three rounds later.
    """


@dataclass
class StepState:
    """Everything the next step reads.

    ``rng_state`` is not optional in practice. A sampler draws noise per
    step, so resuming with the right latent and the wrong generator
    produces a different image from the same seed -- and produces it
    without any error, which is the worst way for this to fail.
    """

    step_index: int
    latent: Any
    rng_state: Any
    extra: dict[str, Any] = field(default_factory=dict)


class ModelAdapter(Protocol):
    """What a model must provide to be scheduled.

    Three tensor operations and nothing about scheduling: the executor
    owns the lifecycle, the adapter owns the model.
    """

    def initial_state(self, request: Any) -> StepState:
        ...

    def denoise_one(self, state: StepState, *, quota_units: int) -> StepState:
        ...

    def decode(self, state: StepState) -> Any:
        ...


class StepExecutor:
    """One request, advanced a step at a time.

    The scheduler calls ``run_step`` when it has decided this request
    should run, with the quota it decided on. Nothing here chooses; the
    executor's job is to make each step separable and each suspension
    exact.
    """

    def __init__(self, request: Any, adapter: ModelAdapter, *,
                 total_steps: int):
        if total_steps < 1:
            raise ValueError("a request with no steps has nothing to schedule")
        self.request = request
        self.adapter = adapter
        self.total_steps = total_steps
        self.phase = Phase.CREATED
        self.state: StepState | None = None
        self.output: Any = None
        # Counters the ledger reads. Kept here rather than derived later
        # because a suspension that is not recorded where it happened
        # cannot be reconciled against the scheduler's own log.
        self.steps_done = 0
        self.suspensions = 0
        self.resumptions = 0
        self.quota_history: list[tuple[int, int]] = []

    # -- lifecycle ----------------------------------------------------

    def prepare(self) -> None:
        if self.phase is not Phase.CREATED:
            raise ExecutorError(f"prepare from {self.phase.name}")
        self.state = self.adapter.initial_state(self.request)
        if self.state.step_index != 0:
            raise ExecutorError("initial state must start at step 0")
        self.phase = Phase.PREPARED

    def run_step(self, *, quota_units: int) -> bool:
        """Advance exactly one denoising step. True if more remain.

        One step, never a batch: a partially executed step is not a state
        the runtime can checkpoint, so the smallest unit the scheduler
        can grant has to be the smallest unit that leaves a resumable
        state behind.
        """
        if self.phase not in (Phase.PREPARED, Phase.SUSPENDED, Phase.RUNNING):
            raise ExecutorError(f"run_step from {self.phase.name}")
        if quota_units < 1:
            raise ValueError("a step needs at least one unit")
        if self.steps_done >= self.total_steps:
            raise ExecutorError("run_step past the last step")
        if self.phase is Phase.SUSPENDED:
            # Running straight from SUSPENDED is an implicit resume, and
            # counts as one. An explicit resume() has already counted
            # itself and left the phase RUNNING, so this cannot
            # double-count.
            self.resumptions += 1
        self.phase = Phase.RUNNING
        self.state = self.adapter.denoise_one(self.state,
                                              quota_units=quota_units)
        self.steps_done += 1
        self.quota_history.append((self.steps_done, quota_units))
        if self.state.step_index != self.steps_done:
            raise ExecutorError(
                f"adapter reports step {self.state.step_index} after "
                f"{self.steps_done} steps; a step that does not advance "
                f"the index cannot be resumed correctly"
            )
        return self.steps_done < self.total_steps

    def suspend(self) -> StepState:
        """Stop at the current step boundary and hand back the state.

        Legal from RUNNING or PREPARED -- a request can be preempted
        before its first step, which is the case a scheduler hits when it
        admits a request and then changes its mind in the same round.
        """
        if self.phase not in (Phase.RUNNING, Phase.PREPARED):
            raise ExecutorError(f"suspend from {self.phase.name}")
        self.phase = Phase.SUSPENDED
        self.suspensions += 1
        return self.state

    def resume(self, state: StepState | None = None) -> None:
        """Restore and continue.

        Passing a state explicitly is how a runtime that moved the
        request between processes or devices restores it; omitting it
        resumes in place. Either way the phase, not the caller, decides
        whether this is legal.
        """
        if self.phase is not Phase.SUSPENDED:
            raise ExecutorError(f"resume from {self.phase.name}")
        if state is not None:
            if state.step_index != self.steps_done:
                raise ExecutorError(
                    f"resuming at step {state.step_index} with "
                    f"{self.steps_done} done would replay or skip work"
                )
            self.state = state
        self.resumptions += 1
        self.phase = Phase.RUNNING

    def finalize(self) -> Any:
        if self.phase not in (Phase.RUNNING, Phase.SUSPENDED):
            raise ExecutorError(f"finalize from {self.phase.name}")
        if self.steps_done != self.total_steps:
            raise ExecutorError(
                f"finalize with {self.steps_done} of {self.total_steps} "
                f"steps done"
            )
        self.output = self.adapter.decode(self.state)
        self.phase = Phase.FINISHED
        return self.output

    def fail(self, reason: str) -> None:
        """Terminal, and recorded. A request that died is not a request
        that never ran, and the ledger has to be able to tell them
        apart."""
        self.phase = Phase.FAILED
        self.failure_reason = reason

    # -- properties the scheduler reads --------------------------------

    @property
    def complete(self) -> bool:
        return self.steps_done >= self.total_steps

    @property
    def steps_remaining(self) -> int:
        return max(0, self.total_steps - self.steps_done)

    @property
    def runnable(self) -> bool:
        return (self.phase in (Phase.PREPARED, Phase.RUNNING, Phase.SUSPENDED)
                and not self.complete)

    def digest(self) -> str:
        """A hash of the finished output, for the bit-exactness check.

        Deliberately over the output rather than over the schedule: the
        acceptance criterion is that scheduling does not change results,
        so the digest must be blind to how the steps were interleaved.
        """
        if self.phase is not Phase.FINISHED:
            raise ExecutorError("digest before finalize")
        return hashlib.sha256(repr(self.output).encode()).hexdigest()
