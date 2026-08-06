"""Why one scalar cannot be both a fair charge and a true cost.

A virtual-service currency has two jobs. It has to say what a tenant owes
for the capacity it was given, so that backlogged tenants can be kept
even; and it has to say what that capacity actually bought, so that
admission and deadline decisions are sound. The plan's claim is that no
single scalar does both once the cost of capacity is non-linear and
depends on who else is running.

This module makes that concrete with the measured externality table
rather than with a constructed example. The table is the counterexample:
the same 32 units, divided differently, produce penalties from 1.22x to
1.93x that are not monotone in either tenant's share. Any currency that is
a function of one tenant's own allocation -- which every scalar considered
here is -- cannot track that, because the quantity it must track depends
on the other tenant.

The conclusion is narrow on purpose. It says a scalar cannot be exact for
both roles simultaneously, not that scalars are useless: the utilisation
result stands on quota-seconds, and quota-seconds remain the right charge.
What fails is using the same number as the cost.
"""

from __future__ import annotations

from dataclasses import dataclass

from .trace_sim import (
    MEASURED_EXTERNALITY,
    MEASURED_QUOTA_SECONDS,
    QuotaCostModel,
)


@dataclass(frozen=True)
class TenantOutcome:
    """One tenant's side of a co-run, in every currency at once."""

    units: int
    peer_units: int
    model: str
    solo_speed: float          # normalised to the full die
    corun_speed: float
    externality: float

    @property
    def quota_share(self) -> float:
        """The charge: fraction of the die held."""
        return self.units / (self.units + self.peer_units)

    @property
    def progress_share(self) -> float:
        """The cost: fraction of the pair's total progress achieved."""
        return self.corun_speed


def outcome(units: int, peer_units: int, model: str = "sdxl") -> TenantOutcome:
    cost = QuotaCostModel.for_model(model)
    full = cost.step_seconds(cost.maskable_units)
    solo = full / cost.step_seconds(units)
    factor = MEASURED_EXTERNALITY[(units, peer_units)]
    return TenantOutcome(
        units=units, peer_units=peer_units, model=model,
        solo_speed=solo, corun_speed=solo / factor, externality=factor,
    )


def pair_outcomes(left: int, right: int, model: str = "sdxl"):
    return outcome(left, right, model), outcome(right, left, model)


def charge_to_cost_ratio(left: int, right: int, model: str = "sdxl") -> float:
    """How far the charge diverges from what it bought, for one pair.

    1.0 means a tenant holding twice the die makes twice the progress, so
    a single scalar would serve both roles. Anything else is the gap the
    counterexample is about.
    """
    a, b = pair_outcomes(left, right, model)
    quota_ratio = a.units / b.units
    progress_ratio = a.corun_speed / b.corun_speed
    return quota_ratio / progress_ratio


def survey(model: str = "sdxl") -> list[dict]:
    """Every measured pair, in both currencies."""
    rows = []
    seen = set()
    for left, right in sorted(MEASURED_EXTERNALITY):
        if (right, left) in seen:
            continue
        if (right, left) not in MEASURED_EXTERNALITY:
            continue
        seen.add((left, right))
        a, b = pair_outcomes(left, right, model)
        rows.append({
            "split": (left, right),
            "quota_ratio": a.units / b.units,
            "progress_ratio": a.corun_speed / b.corun_speed,
            "divergence": charge_to_cost_ratio(left, right, model),
            "externality": (a.externality, b.externality),
        })
    return rows


def equal_charge_unequal_progress(model: str = "sdxl") -> dict:
    """The sharpest form: equal quota-seconds, unequal progress.

    Two tenants are charged the same when they hold the same units for the
    same time. Under an even split that is also equal progress. Under an
    uneven one it is not, and the gap is set by a peer neither tenant
    chose -- so a scheduler equalising the charge does not equalise
    progress, and one equalising progress does not equalise the charge.
    """
    even = pair_outcomes(16, 16, model)
    uneven = pair_outcomes(8, 24, model)
    return {
        "even_split_progress_ratio": even[0].corun_speed / even[1].corun_speed,
        "even_split_quota_ratio": 1.0,
        "uneven_split_progress_ratio": (
            uneven[0].corun_speed / uneven[1].corun_speed
        ),
        "uneven_split_quota_ratio": uneven[0].units / uneven[1].units,
        # What a tenant gets per unit of charge, in each configuration.
        "progress_per_unit_even": [o.corun_speed / o.units for o in even],
        "progress_per_unit_uneven": [o.corun_speed / o.units for o in uneven],
    }


def is_currency_separable(model: str = "sdxl", tolerance: float = 0.05) -> bool:
    """Whether one scalar can serve as both charge and cost.

    True only if holding k times the die yields k times the progress in
    every measured configuration. The measured table decides this, not an
    assumption about the hardware.
    """
    return all(
        abs(row["divergence"] - 1.0) <= tolerance for row in survey(model)
    )
