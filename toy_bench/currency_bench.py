#!/usr/bin/env python3
"""Toy experiments for virtual-service currencies in bursty GPU scheduling.

The model is deliberately small and dependency-free.  It is not intended to
predict a particular GPU.  Instead, it stress-tests the accounting semantics
that a real profiler-backed scheduler would use:

* wall time;
* allocated SM time;
* full-GPU-equivalent (FGE) progress;
* dominant-resource time.

Run:
    python currency_bench.py --out results
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


CURRENCIES = ("wall", "sm_time", "fge_progress", "dominant_time")


@dataclass(frozen=True)
class Workload:
    name: str
    curve: str
    solo_step_ms: float
    bw_pressure: float
    pcie_pressure: float = 0.0
    contention_sensitivity: float = 0.5
    sm_partition_exponent: float = 1.08
    bandwidth_saturation_share: float = 0.35
    # Fraction of solo time that does not shrink with quota, taken from the
    # fitted Amdahl parameters rather than from the observed 32-unit cell:
    # serial / (serial + parallel/32). Measured on gfx1201, 0.391 for SDXL
    # and 0.276 for CogVideoX-2b (2026-08-03/04).
    # None keeps the historical power-law behaviour for the synthetic
    # workloads, whose curves were never calibrated against hardware.
    serial_fraction: float | None = None

    def solo_speed(self, sm_share: float) -> float:
        """Normalized work rate relative to solo execution on the full GPU.

        With a measured serial fraction this is Amdahl's law, which fits
        the hardware to 2-3% where the power law below misses by 18-22%
        (2026-08-06). The power law's error has a direction: it
        under-predicts speed at small quotas, so it over-predicts what
        partitioning costs, and a scheduler reading it is too conservative
        to split a die that in fact keeps 65% of its throughput across four
        tenants.
        """
        sm_share = min(1.0, max(1e-6, sm_share))
        if self.serial_fraction is not None:
            # t(q) = serial + parallel/q, normalised so t(1) == 1.
            serial = self.serial_fraction
            return 1.0 / (serial + (1.0 - serial) / sm_share)
        compute = sm_share**self.sm_partition_exponent
        bandwidth = min(1.0, sm_share / self.bandwidth_saturation_share)
        if self.curve == "compute":
            return compute
        if self.curve == "bandwidth":
            return bandwidth
        if self.curve == "mixed":
            # A simple serial composition: tensor-core work plus a streamed
            # component whose bandwidth saturates with fewer SMs.
            return 1.0 / (0.72 / compute + 0.28 / bandwidth)
        raise ValueError(f"unknown curve {self.curve!r}")

    def speed(
        self,
        sm_share: float,
        corunners: Sequence[Tuple["Workload", float]] = (),
    ) -> float:
        """Normalized work rate including residual shared-memory contention."""
        speed = self.solo_speed(sm_share)
        other_pressure = 0.0
        for other, other_share in corunners:
            other_pressure += other.bw_pressure * other.solo_speed(other_share)
            other_pressure += 0.5 * other.pcie_pressure
        penalty = 0.34 * self.contention_sensitivity * min(1.0, other_pressure)
        return speed * max(0.45, 1.0 - penalty)

    def dominant_fraction(self, sm_share: float, speed: float) -> float:
        """Approximate dominant instantaneous resource share."""
        bw = min(1.0, self.bw_pressure * speed)
        pcie = min(1.0, self.pcie_pressure * speed)
        return max(sm_share, bw, pcie)


COMPUTE_DIT = Workload(
    name="compute_dit",
    curve="compute",
    solo_step_ms=24.0,
    bw_pressure=0.25,
    contention_sensitivity=0.28,
)

STREAMED_DIT = Workload(
    name="streamed_dit",
    curve="mixed",
    solo_step_ms=28.0,
    bw_pressure=0.55,
    pcie_pressure=0.75,
    contention_sensitivity=0.65,
)

BANDWIDTH_JOB = Workload(
    name="bandwidth_job",
    curve="bandwidth",
    solo_step_ms=6.0,
    bw_pressure=0.95,
    contention_sensitivity=0.95,
)

# Calibrated against the Gate B quota tables rather than assumed. Serial
# fractions and step times come from the measured 32-unit cells; the
# contention sensitivities from the co-run externality of about 23% at an
# even split (experiments/probes/amd-r9700-cu-mask/).
MEASURED_SDXL = Workload(
    name="sdxl_measured",
    curve="compute",
    solo_step_ms=115.52,         # per denoising step at 32 units, 768x768
    bw_pressure=0.45,
    contention_sensitivity=0.68,
    serial_fraction=0.4419,      # refit against the per-step curve
)

MEASURED_COGVIDEOX_2B = Workload(
    name="cogvideox2b_measured",
    curve="compute",
    solo_step_ms=517.1,          # per denoising step at 32 units, 9 frames
    bw_pressure=0.60,
    contention_sensitivity=0.70,
    serial_fraction=0.276,
)


def currency_delta(
    currency: str,
    workload: Workload,
    dt_ms: float,
    sm_share: float,
    work_done_ms: float,
    speed: float,
    weight: float = 1.0,
) -> float:
    if currency == "wall":
        value = dt_ms
    elif currency == "sm_time":
        value = sm_share * dt_ms
    elif currency == "fge_progress":
        value = work_done_ms
    elif currency == "dominant_time":
        value = workload.dominant_fraction(sm_share, speed) * dt_ms
    else:
        raise ValueError(currency)
    return value / weight


def write_csv(path: Path, rows: Iterable[Dict[str, object]]) -> None:
    rows = list(rows)
    if not rows:
        return
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def quota_invariance() -> List[Dict[str, object]]:
    """Charge one identical completed step under several SM quotas."""
    rows: List[Dict[str, object]] = []
    shares = (1.0, 0.75, 0.5, 0.25)
    for workload in (COMPUTE_DIT, STREAMED_DIT, BANDWIDTH_JOB):
        raw: Dict[str, Dict[float, float]] = {c: {} for c in CURRENCIES}
        durations: Dict[float, float] = {}
        for share in shares:
            speed = workload.speed(share)
            dt = workload.solo_step_ms / speed
            durations[share] = dt
            for currency in CURRENCIES:
                raw[currency][share] = currency_delta(
                    currency,
                    workload,
                    dt,
                    share,
                    workload.solo_step_ms,
                    speed,
                )
        for share in shares:
            row: Dict[str, object] = {
                "workload": workload.name,
                "sm_share": share,
                "wall_ms": round(durations[share], 4),
            }
            for currency in CURRENCIES:
                base = raw[currency][1.0]
                row[f"{currency}_charge"] = round(raw[currency][share], 4)
                row[f"{currency}_ratio_to_full"] = round(
                    raw[currency][share] / base, 4
                )
            rows.append(row)
    return rows


def contention_accounting() -> List[Dict[str, object]]:
    """Compare charges for the same completed work with and without a corunner."""
    scenarios = (
        ("compute_victim_vs_bandwidth", COMPUTE_DIT, BANDWIDTH_JOB),
        ("streamed_victim_vs_bandwidth", STREAMED_DIT, BANDWIDTH_JOB),
        ("bandwidth_victim_vs_streamed", BANDWIDTH_JOB, STREAMED_DIT),
    )
    rows: List[Dict[str, object]] = []
    for scenario, victim, other in scenarios:
        share = 0.5
        base_speed = victim.speed(share)
        contended_speed = victim.speed(share, ((other, 0.5),))
        base_dt = victim.solo_step_ms / base_speed
        contended_dt = victim.solo_step_ms / contended_speed
        for currency in CURRENCIES:
            base_charge = currency_delta(
                currency,
                victim,
                base_dt,
                share,
                victim.solo_step_ms,
                base_speed,
            )
            contended_charge = currency_delta(
                currency,
                victim,
                contended_dt,
                share,
                victim.solo_step_ms,
                contended_speed,
            )
            rows.append(
                {
                    "scenario": scenario,
                    "currency": currency,
                    "base_wall_ms": round(base_dt, 4),
                    "contended_wall_ms": round(contended_dt, 4),
                    "slowdown": round(contended_dt / base_dt, 4),
                    "charge_ratio": round(contended_charge / base_charge, 4),
                }
            )
    return rows


@dataclass
class InfiniteTenant:
    name: str
    workload: Workload
    vruntime: float = 0.0
    progress_ms: float = 0.0
    sm_ms: float = 0.0
    dominant_ms: float = 0.0
    boost_epochs: int = 0


def boost_rotation(
    currency: str,
    epochs: int = 2000,
    epoch_ms: float = 1.0,
    progress_scale: Optional[Dict[str, float]] = None,
) -> Dict[str, object]:
    """Rotate a 50% SM boost above a 25% base allocation.

    Both tenants are perpetually backlogged.  This isolates what each currency
    equalizes: elapsed time, SM allocation, completed FGE work, or dominant
    resource occupancy.
    """
    progress_scale = progress_scale or {}
    tenants = [
        InfiniteTenant("compute", COMPUTE_DIT),
        InfiniteTenant("streamed", STREAMED_DIT),
    ]
    for _ in range(epochs):
        # Stable tie-breaking alternates by giving the boost to the tenant that
        # has received fewer boosts when virtual values are equal.
        boosted = min(tenants, key=lambda x: (x.vruntime, x.boost_epochs, x.name))
        boosted.boost_epochs += 1
        shares = {boosted.name: 0.75}
        for tenant in tenants:
            if tenant is not boosted:
                shares[tenant.name] = 0.25

        for tenant in tenants:
            share = shares[tenant.name]
            other = next(x for x in tenants if x is not tenant)
            speed = tenant.workload.speed(
                share, ((other.workload, shares[other.name]),)
            )
            work = speed * epoch_ms
            tenant.progress_ms += work
            tenant.sm_ms += share * epoch_ms
            tenant.dominant_ms += (
                tenant.workload.dominant_fraction(share, speed) * epoch_ms
            )
            accounted_work = work * progress_scale.get(tenant.name, 1.0)
            tenant.vruntime += currency_delta(
                currency,
                tenant.workload,
                epoch_ms,
                share,
                accounted_work,
                speed,
            )

    progresses = [x.progress_ms for x in tenants]
    jain = sum(progresses) ** 2 / (len(progresses) * sum(x * x for x in progresses))
    total_progress = sum(progresses)
    return {
        "currency": currency,
        "epochs": epochs,
        "compute_progress_share": round(tenants[0].progress_ms / total_progress, 4),
        "streamed_progress_share": round(tenants[1].progress_ms / total_progress, 4),
        "progress_jain": round(jain, 4),
        "compute_sm_share": round(
            tenants[0].sm_ms / sum(x.sm_ms for x in tenants), 4
        ),
        "streamed_sm_share": round(
            tenants[1].sm_ms / sum(x.sm_ms for x in tenants), 4
        ),
        "compute_dominant_share": round(
            tenants[0].dominant_ms / sum(x.dominant_ms for x in tenants), 4
        ),
        "streamed_dominant_share": round(
            tenants[1].dominant_ms / sum(x.dominant_ms for x in tenants), 4
        ),
        "compute_boost_epochs": tenants[0].boost_epochs,
        "streamed_boost_epochs": tenants[1].boost_epochs,
        "total_fge_progress_ms": round(total_progress, 2),
    }


def predictor_bias_sensitivity() -> List[Dict[str, object]]:
    """Show how systematic FGE prediction bias changes actual service shares."""
    rows: List[Dict[str, object]] = []
    for scale in (0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.3):
        row = boost_rotation(
            "fge_progress",
            progress_scale={"streamed": scale},
        )
        rows.append(
            {
                "streamed_accounting_scale": scale,
                "compute_actual_progress_share": row["compute_progress_share"],
                "streamed_actual_progress_share": row["streamed_progress_share"],
                "progress_jain": row["progress_jain"],
                "compute_boost_epochs": row["compute_boost_epochs"],
                "streamed_boost_epochs": row["streamed_boost_epochs"],
            }
        )
    return rows


@dataclass
class Job:
    name: str
    workload: Workload
    arrival_ms: float
    work_ms: float
    deadline_ms: Optional[float]
    kind: str
    weight: float = 1.0
    remaining_ms: float = field(init=False)
    vruntime: float = 0.0
    finish_ms: Optional[float] = None
    last_progress_ms: Optional[float] = None
    max_no_progress_ms: float = 0.0

    def __post_init__(self) -> None:
        self.remaining_ms = self.work_ms


def predicted_remaining(job: Job, share: float) -> float:
    return job.remaining_ms / job.workload.solo_speed(share)


def make_burst_jobs() -> List[Job]:
    """A feasible burst whose deadlines require ordering, not more capacity."""
    return [
        Job("video", COMPUTE_DIT, 0.0, 420.0, None, "video", weight=1.0),
        Job("u0", COMPUTE_DIT, 35.0, 30.0, 110.0, "urgent", weight=2.0),
        Job("u1", STREAMED_DIT, 35.0, 30.0, 180.0, "urgent", weight=2.0),
        Job("u2", COMPUTE_DIT, 35.0, 20.0, 135.0, "urgent", weight=2.0),
        Job("u3", STREAMED_DIT, 35.0, 25.0, 155.0, "urgent", weight=2.0),
    ]


def optimistic_edf_check(jobs: Sequence[Job]) -> Tuple[bool, List[Dict[str, object]]]:
    """Necessary feasibility check: urgent jobs run solo on the full GPU."""
    urgent = sorted(
        (j for j in jobs if j.kind == "urgent"),
        key=lambda j: (j.deadline_ms or math.inf, j.name),
    )
    now = min(j.arrival_ms for j in urgent)
    rows: List[Dict[str, object]] = []
    feasible = True
    for job in urgent:
        now = max(now, job.arrival_ms) + job.work_ms
        met = now <= (job.deadline_ms or math.inf)
        feasible = feasible and met
        rows.append(
            {
                "job": job.name,
                "work_fge_ms": job.work_ms,
                "deadline_ms": job.deadline_ms,
                "optimistic_edf_finish_ms": round(now, 3),
                "optimistic_met_deadline": met,
            }
        )
    return feasible, rows


def burst_deadline_sim(
    currency: str,
    use_slack_guard: bool,
    epoch_ms: float = 1.0,
    end_ms: float = 650.0,
) -> Tuple[Dict[str, object], List[Dict[str, object]]]:
    """Toy video plus a heterogeneous urgent burst.

    At most two jobs co-run.  One receives 75% SMs and the other 25%.  The
    fairness-only variant picks the smallest virtual service.  The hybrid
    variant promotes an urgent job when its predicted slack drops below 12 ms.
    """
    jobs = make_burst_jobs()
    optimistic_feasible, _ = optimistic_edf_check(jobs)
    now = 0.0
    while now < end_ms and any(j.finish_ms is None for j in jobs):
        active = [
            j
            for j in jobs
            if j.arrival_ms <= now and j.finish_ms is None and j.remaining_ms > 1e-9
        ]
        if not active:
            now = min(j.arrival_ms for j in jobs if j.arrival_ms > now)
            continue

        primary: Job
        if use_slack_guard:
            urgent = [j for j in active if j.kind == "urgent"]
            slack = [
                (
                    (j.deadline_ms or math.inf)
                    - now
                    - predicted_remaining(j, 0.75),
                    j,
                )
                for j in urgent
            ]
            risky = [item for item in slack if item[0] < 12.0]
            if risky:
                primary = min(risky, key=lambda x: (x[0], x[1].vruntime))[1]
            else:
                primary = min(active, key=lambda j: (j.vruntime, j.arrival_ms, j.name))
        else:
            primary = min(active, key=lambda j: (j.vruntime, j.arrival_ms, j.name))

        others = [j for j in active if j is not primary]
        running = [(primary, 1.0 if not others else 0.75)]
        if others:
            # Preserve a video progress floor: if it has made no progress for
            # 20 ms, force it into the secondary slot.  Otherwise use virtual
            # service for the second selection as well.
            video = next((j for j in others if j.kind == "video"), None)
            if (
                video is not None
                and video.last_progress_ms is not None
                and now - video.last_progress_ms >= 20.0
            ):
                secondary = video
            else:
                secondary = min(
                    others, key=lambda j: (j.vruntime, j.arrival_ms, j.name)
                )
            running.append((secondary, 0.25))

        for job in active:
            if job.last_progress_ms is not None:
                job.max_no_progress_ms = max(
                    job.max_no_progress_ms, now - job.last_progress_ms
                )

        for job, share in running:
            corunners = [
                (other.workload, other_share)
                for other, other_share in running
                if other is not job
            ]
            speed = job.workload.speed(share, corunners)
            work = min(job.remaining_ms, speed * epoch_ms)
            effective_dt = work / speed
            job.remaining_ms -= work
            job.last_progress_ms = now + effective_dt
            job.vruntime += currency_delta(
                currency,
                job.workload,
                effective_dt,
                share,
                work,
                speed,
                job.weight,
            )
            if job.remaining_ms <= 1e-9:
                job.finish_ms = now + effective_dt
        now += epoch_ms

    urgent = [j for j in jobs if j.kind == "urgent"]
    video = next(j for j in jobs if j.kind == "video")
    latencies = [(j.finish_ms or end_ms) - j.arrival_ms for j in urgent]
    misses = sum(
        1 for j in urgent if j.finish_ms is None or j.finish_ms > (j.deadline_ms or math.inf)
    )
    summary = {
        "currency": currency,
        "slack_guard": use_slack_guard,
        "optimistic_edf_feasible": optimistic_feasible,
        "urgent_deadline_misses": misses,
        "urgent_mean_latency_ms": round(sum(latencies) / len(latencies), 3),
        "urgent_max_latency_ms": round(max(latencies), 3),
        "video_finish_ms": round(video.finish_ms or end_ms, 3),
        "video_max_no_progress_ms": round(video.max_no_progress_ms, 3),
        "makespan_ms": round(max(j.finish_ms or end_ms for j in jobs), 3),
    }
    details = [
        {
            "currency": currency,
            "slack_guard": use_slack_guard,
            "job": job.name,
            "kind": job.kind,
            "work_fge_ms": job.work_ms,
            "deadline_ms": job.deadline_ms,
            "finish_ms": round(job.finish_ms or end_ms, 3),
            "latency_ms": round((job.finish_ms or end_ms) - job.arrival_ms, 3),
            "met_deadline": (
                job.deadline_ms is None
                or (
                    job.finish_ms is not None
                    and job.finish_ms <= job.deadline_ms
                )
            ),
        }
        for job in jobs
    ]
    return summary, details


def summarize(
    invariance: List[Dict[str, object]],
    contention: List[Dict[str, object]],
    rotation: List[Dict[str, object]],
    predictor_bias: List[Dict[str, object]],
    burst: List[Dict[str, object]],
) -> Dict[str, object]:
    max_quota_bias: Dict[str, float] = {}
    for currency in CURRENCIES:
        ratios = [
            float(row[f"{currency}_ratio_to_full"])
            for row in invariance
        ]
        max_quota_bias[currency] = round(max(abs(x - 1.0) for x in ratios), 4)

    max_contention_bias: Dict[str, float] = {}
    for currency in CURRENCIES:
        ratios = [
            float(row["charge_ratio"])
            for row in contention
            if row["currency"] == currency
        ]
        max_contention_bias[currency] = round(
            max(abs(x - 1.0) for x in ratios), 4
        )

    return {
        "max_quota_charge_bias": max_quota_bias,
        "max_contention_charge_bias": max_contention_bias,
        "boost_rotation": rotation,
        "predictor_bias_sensitivity": predictor_bias,
        "burst_deadline": burst,
        "interpretation": {
            "fge_progress": (
                "Best invariant service currency: completed work receives the same "
                "charge across SM quotas and residual contention."
            ),
            "sm_time": (
                "Correct currency for allocated compute-resource fairness, but not "
                "for equal completed progress when scaling is nonlinear."
            ),
            "dominant_time": (
                "Useful as a separate resource/admission ledger for bandwidth-heavy "
                "or PCIe-heavy jobs."
            ),
            "wall": (
                "Overcharges slowed victims and ignores how much spatial GPU capacity "
                "was assigned; unsuitable as the primary virtual-service currency."
            ),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path("results"))
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    invariance = quota_invariance()
    contention = contention_accounting()
    rotation = [boost_rotation(currency) for currency in CURRENCIES]
    predictor_bias = predictor_bias_sensitivity()
    burst: List[Dict[str, object]] = []
    burst_jobs: List[Dict[str, object]] = []
    for slack_guard in (False, True):
        for currency in CURRENCIES:
            run_summary, run_jobs = burst_deadline_sim(currency, slack_guard)
            burst.append(run_summary)
            burst_jobs.extend(run_jobs)
    optimistic_feasible, optimistic_edf = optimistic_edf_check(make_burst_jobs())
    if not optimistic_feasible:
        raise RuntimeError("burst scenario is infeasible even under optimistic EDF")
    summary = summarize(
        invariance,
        contention,
        rotation,
        predictor_bias,
        burst,
    )

    write_csv(args.out / "quota_invariance.csv", invariance)
    write_csv(args.out / "contention_accounting.csv", contention)
    write_csv(args.out / "boost_rotation.csv", rotation)
    write_csv(args.out / "predictor_bias_sensitivity.csv", predictor_bias)
    write_csv(args.out / "burst_deadline.csv", burst)
    write_csv(args.out / "burst_jobs.csv", burst_jobs)
    write_csv(args.out / "optimistic_edf.csv", optimistic_edf)
    with (args.out / "summary.json").open("w") as f:
        json.dump(summary, f, indent=2)

    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
