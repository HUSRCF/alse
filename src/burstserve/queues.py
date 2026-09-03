"""Two layers of scheduling, kept apart on purpose.

ASLE has no queue. Requests are a list of ``{id, arrival, served}`` and
"pending" is a filter over it, so there is no tenant, no deadline, and no
order to choose. plan.md's week 7-8 work adds tenant and request queues
with FCFS/EDF *within* a tenant.

Within is the important word. The scheduler this project is building
makes two different decisions and they must not be confused:

  * between tenants -- who gets how much of the die. That is the frozen
    Gate C policy, and it answers to fairness, the service ledger and
    the deadline bound.
  * within a tenant -- which of that tenant's own requests runs next.
    That answers only to the tenant's own ordering, and a tenant
    reordering its own queue cannot take service from anyone else.

Collapsing them into one ordering is how a tenant with many small
requests starves one with few large ones while every individual decision
looks locally reasonable. Keeping them apart is also what makes the
fairness claim checkable: quota-seconds are charged per tenant, so the
inner order cannot move the charge.

EDF here breaks ties by arrival and puts deadline-free requests last
rather than treating a missing deadline as infinitely far away. The two
are equivalent for ordering and different for reading: "last, because it
has no deadline" is a statement the ledger can carry, and "sorted after
a request due in the year 9999" is not.
"""

from __future__ import annotations

import enum
from collections import deque
from dataclasses import dataclass, field
from typing import Iterable, Iterator


class Discipline(enum.Enum):
    """How a tenant orders its own requests."""

    FCFS = "fcfs"
    EDF = "edf"


@dataclass(frozen=True)
class QueuedRequest:
    """A request as the queue sees it.

    Frozen: the queue reorders references, it does not edit requests. A
    discipline that mutated arrival or deadline to force an order would
    make the ledger's record of why a request ran unreconstructable.
    """

    request_id: int
    tenant: str
    model: str
    arrival_s: float
    steps: int
    deadline_s: float | None = None

    def sort_key(self, discipline: Discipline) -> tuple:
        if discipline is Discipline.FCFS:
            return (self.arrival_s, self.request_id)
        # EDF: deadline first, then arrival, then id. The leading flag
        # puts deadline-free requests after every dated one without
        # inventing a deadline for them.
        return (self.deadline_s is None,
                self.deadline_s if self.deadline_s is not None else 0.0,
                self.arrival_s, self.request_id)


class QueueError(RuntimeError):
    """A queue operation that would lose or duplicate a request."""


class TenantQueue:
    """One tenant's requests, in that tenant's own order.

    Holds admitted-but-not-finished requests. A request leaves only by
    completing or by being explicitly dropped, so a request that
    disappears is a bug rather than a silent policy.
    """

    def __init__(self, tenant: str, *,
                 discipline: Discipline = Discipline.FCFS):
        self.tenant = tenant
        self.discipline = discipline
        self._waiting: list[QueuedRequest] = []
        self._in_flight: dict[int, QueuedRequest] = {}
        self._finished: list[int] = []

    def admit(self, request: QueuedRequest) -> None:
        if request.tenant != self.tenant:
            raise QueueError(
                f"request {request.request_id} belongs to "
                f"{request.tenant!r}, not {self.tenant!r}"
            )
        if self.knows(request.request_id):
            raise QueueError(f"request {request.request_id} already admitted")
        self._waiting.append(request)

    def knows(self, request_id: int) -> bool:
        return (any(r.request_id == request_id for r in self._waiting)
                or request_id in self._in_flight
                or request_id in self._finished)

    def ready(self, now: float) -> list[QueuedRequest]:
        """Waiting requests that have arrived, in discipline order.

        Sorted on read rather than kept sorted on insert. The order
        depends on the discipline, and a tenant may change discipline
        between rounds; a structure that baked the order in at admission
        would answer the old question.
        """
        arrived = [r for r in self._waiting if r.arrival_s <= now]
        return sorted(arrived, key=lambda r: r.sort_key(self.discipline))

    def next_ready(self, now: float) -> QueuedRequest | None:
        candidates = self.ready(now)
        return candidates[0] if candidates else None

    def start(self, request_id: int) -> QueuedRequest:
        """Move a request from waiting to in-flight."""
        for index, request in enumerate(self._waiting):
            if request.request_id == request_id:
                self._waiting.pop(index)
                self._in_flight[request_id] = request
                return request
        raise QueueError(f"request {request_id} is not waiting in "
                         f"{self.tenant!r}")

    def suspend(self, request_id: int) -> None:
        """Return an in-flight request to the waiting set.

        Preemption is not failure: the request keeps its identity, its
        arrival and its place in the discipline. A suspend that pushed it
        to the back would make preemption cost latency in a way the
        discipline never sanctioned.
        """
        request = self._in_flight.pop(request_id, None)
        if request is None:
            raise QueueError(f"request {request_id} is not in flight in "
                             f"{self.tenant!r}")
        self._waiting.append(request)

    def finish(self, request_id: int) -> None:
        if request_id not in self._in_flight:
            raise QueueError(f"request {request_id} is not in flight in "
                             f"{self.tenant!r}")
        del self._in_flight[request_id]
        self._finished.append(request_id)

    def drop(self, request_id: int, reason: str) -> QueuedRequest:
        """Remove a request without completing it -- an admission
        rejection or a deadline abandonment. Recorded distinctly from a
        completion because a rejected request is not a served one."""
        for index, request in enumerate(self._waiting):
            if request.request_id == request_id:
                self._waiting.pop(index)
                self._dropped = getattr(self, "_dropped", {})
                self._dropped[request_id] = reason
                return request
        raise QueueError(f"request {request_id} is not waiting in "
                         f"{self.tenant!r}")

    @property
    def waiting(self) -> tuple[QueuedRequest, ...]:
        return tuple(self._waiting)

    @property
    def in_flight(self) -> tuple[QueuedRequest, ...]:
        return tuple(self._in_flight.values())

    @property
    def backlog(self) -> int:
        """Requests owed service: waiting plus in-flight.

        The fairness clauses are about backlogged tenants, and a tenant
        whose only request is mid-step is still backlogged.
        """
        return len(self._waiting) + len(self._in_flight)

    def checkpoint(self) -> dict:
        return {
            "tenant": self.tenant,
            "discipline": self.discipline.value,
            "waiting": [vars(r) for r in self._waiting],
            "in_flight": [vars(r) for r in self._in_flight.values()],
            "finished": list(self._finished),
        }

    @classmethod
    def restore(cls, payload: dict) -> "TenantQueue":
        queue = cls(payload["tenant"],
                    discipline=Discipline(payload["discipline"]))
        queue._waiting = [QueuedRequest(**r) for r in payload["waiting"]]
        queue._in_flight = {r["request_id"]: QueuedRequest(**r)
                            for r in payload["in_flight"]}
        queue._finished = list(payload["finished"])
        return queue


class TenantRegistry:
    """Every tenant's queue, and the order tenants are offered in.

    Tenants are offered round-robin from a rotating deque, so the
    between-tenant policy sees a different first candidate each round.
    Without that, a policy that breaks ties by position would always
    break them the same way, and a tie-break repeated every round is a
    bias rather than a tie-break.
    """

    def __init__(self, *, discipline: Discipline = Discipline.FCFS):
        self.default_discipline = discipline
        self._queues: dict[str, TenantQueue] = {}
        self._order: deque[str] = deque()

    def queue_for(self, tenant: str) -> TenantQueue:
        if tenant not in self._queues:
            self._queues[tenant] = TenantQueue(
                tenant, discipline=self.default_discipline
            )
            self._order.append(tenant)
        return self._queues[tenant]

    def admit(self, request: QueuedRequest) -> None:
        self.queue_for(request.tenant).admit(request)

    def rotate(self) -> None:
        if self._order:
            self._order.rotate(-1)

    def tenants(self) -> Iterator[str]:
        return iter(tuple(self._order))

    def ready(self, now: float,
              per_tenant: int = 1) -> list[tuple[str, QueuedRequest]]:
        """Up to ``per_tenant`` candidates per tenant, in rotation order.

        One per tenant was the original rule, and the reason was that the
        between-tenant policy decides how much die each tenant gets while
        choosing *within* a tenant is the inner discipline's job.

        That rule is also what makes a burst serial. Every request of a
        burst carries the same absolute deadline, so with one candidate
        per tenant the burst's four requests are served one after
        another whatever the split -- and 3.8 shows the consequence: on
        the measured cost model no split can meet a 5.34 s burst deadline,
        because the best partitioned burst is 6.30 s against 3.70 s
        exclusive. That is a property of this rule, not of partitioning.

        ``per_tenant`` above one offers the policy several requests of one
        tenant so it can run them concurrently on slices of that tenant's
        quota. The inner discipline still decides *which* ones: they come
        off the same sorted queue in order.
        """
        if per_tenant < 1:
            raise ValueError("a tenant has to be offered at least one request")
        out = []
        for tenant in self._order:
            queue = self._queues[tenant]
            for candidate in queue.ready(now)[:per_tenant]:
                out.append((tenant, candidate))
        return out

    def backlogged(self) -> tuple[str, ...]:
        """Tenants owed service, which is what the fairness bound is
        measured over."""
        return tuple(t for t, q in self._queues.items() if q.backlog > 0)

    def checkpoint(self) -> dict:
        return {
            "default_discipline": self.default_discipline.value,
            "order": list(self._order),
            "queues": {t: q.checkpoint() for t, q in self._queues.items()},
        }

    @classmethod
    def restore(cls, payload: dict) -> "TenantRegistry":
        registry = cls(discipline=Discipline(payload["default_discipline"]))
        registry._queues = {t: TenantQueue.restore(q)
                            for t, q in payload["queues"].items()}
        registry._order = deque(payload["order"])
        return registry
