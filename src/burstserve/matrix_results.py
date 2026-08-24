"""Turn matrix cells into the numbers a claim is made of.

plan.md's primary claim is a Pareto improvement against the strongest
non-oracle baseline, with paired-seed bootstrap 95% intervals. Three
things about that shape decide whether the aggregate is honest, and each
is handled here rather than at the point of writing a sentence.

**Pairing is by seed, not by policy.** Two policies at the same seed saw
the same arrival trace, so their difference is a paired quantity and its
interval is much tighter than the difference of two independent means.
Resampling the policies' results separately would widen the interval and
also destroy the pairing that makes the comparison valid at all.

**The baseline is chosen before the comparison, and by rule.** "Strongest
non-oracle baseline" is a definition, so it is computed -- the best
baseline mean on the metric -- rather than picked. Picking would let the
comparison choose its own opponent, which is how a 4.5% result gets
reported as 30%: on the first real cell, probing beat FCFS by 30%, the
strongest baseline by 22%, and this method's own ablation by 4.5%.

**The method's own variants are not baselines.** They are ablations, and
counting them as baselines would make the strongest "baseline" be the
method itself. The split is declared in ``BASELINE_POLICIES`` and
``METHOD_POLICIES`` rather than inferred from a name.
"""

from __future__ import annotations

import json
import random
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

# Declared, not inferred. A policy that is a component of the method is
# an ablation even when it looks like a plausible baseline.
BASELINE_POLICIES = (
    "exclusive_fcfs",
    "static_even",
    "deadline_aware",
    "step_matched_pairing",
    "measured_pairs_only",
    # A split chosen once at deployment time is a baseline: it contains no
    # decision made at run time, so anything an adaptive policy wins over
    # it is what the scheduling actually bought. Adding them here makes
    # ``strongest_baseline`` pick the best fixed split by rule, which is
    # the comparison the method has to survive.
    "fixed_split_4",
    "fixed_split_8",
    "fixed_split_16",
    "fixed_split_24",
    "fixed_split_28",
)
METHOD_POLICIES = (
    "slo_aware_partitioning",
    "probing_partitioning",
    "sticky_probing_partitioning",
)
ORACLE_POLICIES = ("oracle_shortest_remaining",)


@dataclass(frozen=True)
class Cell:
    policy: str
    load: float
    burst: int
    seed: int
    miss_rate: float | None
    video_goodput: float | None
    urgent_p99_s: float | None
    urgent_completed: int
    path: str

    @property
    def config(self) -> tuple[float, int]:
        return (self.load, self.burst)


def load_cells(directory: Path) -> list[Cell]:
    """Read every cell payload in a directory, keeping the incomplete ones.

    A cell that finished with no urgent requests is real data about the
    trace, not a file to skip: dropping it silently would make a
    completion-rate acceptance impossible to check.
    """
    cells = []
    for path in sorted(Path(directory).glob("*.json")):
        try:
            payload = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        if payload.get("schema_version") != "burstserve.amd-matrix-cell/v1":
            continue
        spec = payload["spec"]
        cells.append(Cell(
            policy=payload["policy"], load=spec["load"], burst=spec["burst"],
            seed=spec["seed"], miss_rate=payload["urgent"]["miss_rate"],
            video_goodput=payload["video"]["goodput_steps_per_s"],
            urgent_p99_s=payload["urgent"]["latency_p99_s"],
            urgent_completed=payload["urgent"]["completed"],
            path=str(path),
        ))
    return cells


def paired_differences(cells: Sequence[Cell], policy: str, against: str,
                       metric: str) -> list[tuple[tuple[float, int, int],
                                                  float]]:
    """Per (load, burst, seed), one policy's metric minus another's.

    Only configurations where *both* policies have a cell contribute. A
    missing cell drops the pair rather than the seed's other half, which
    would compare a policy's easy configurations against another's hard
    ones.
    """
    index: dict[tuple[str, float, int, int], Cell] = {}
    for cell in cells:
        index[(cell.policy, cell.load, cell.burst, cell.seed)] = cell
    out = []
    for (name, load, burst, seed), cell in sorted(index.items()):
        if name != policy:
            continue
        other = index.get((against, load, burst, seed))
        if other is None:
            continue
        mine, theirs = getattr(cell, metric), getattr(other, metric)
        if mine is None or theirs is None:
            continue
        out.append(((load, burst, seed), mine - theirs))
    return out


def bootstrap_ci(values: Sequence[float], *, seed: int = 0,
                 resamples: int = 10000,
                 confidence: float = 0.95) -> tuple[float, float, float]:
    """Percentile bootstrap over the paired differences.

    Resampling the differences, not the two arms, is what keeps the
    pairing. Returns (mean, low, high).
    """
    if not values:
        raise ValueError("no paired differences to bootstrap")
    rng = random.Random(seed)
    n = len(values)
    means = []
    for _ in range(resamples):
        means.append(sum(values[rng.randrange(n)] for _ in range(n)) / n)
    means.sort()
    tail = (1.0 - confidence) / 2.0
    low = means[int(tail * resamples)]
    high = means[min(resamples - 1, int((1.0 - tail) * resamples))]
    return statistics.mean(values), low, high


def strongest_baseline(cells: Sequence[Cell], metric: str,
                       *, lower_is_better: bool = True) -> str | None:
    """The baseline with the best mean on the metric, by rule not by eye.

    Computed over baselines only. Letting the method's own ablations into
    this set would make the comparison be against the method.
    """
    means: dict[str, list[float]] = {}
    for cell in cells:
        if cell.policy not in BASELINE_POLICIES:
            continue
        value = getattr(cell, metric)
        if value is not None:
            means.setdefault(cell.policy, []).append(value)
    if not means:
        return None
    scored = {name: statistics.mean(values) for name, values in means.items()}
    return min(scored, key=scored.get) if lower_is_better else max(
        scored, key=scored.get)


def primary_claim(cells: Sequence[Cell], *, method: str,
                  bootstrap_seed: int = 0) -> dict:
    """plan.md's first Pareto branch, with its interval and its opponent.

    Reports the comparison against the strongest baseline *and* against
    every baseline separately, because which opponent a headline used is
    the single easiest thing to lose between a table and a sentence.
    """
    baseline = strongest_baseline(cells, "miss_rate")
    result: dict = {"method": method, "strongest_baseline": baseline,
                    "per_baseline": {}}
    for name in BASELINE_POLICIES:
        pairs = paired_differences(cells, method, name, "miss_rate")
        if not pairs:
            continue
        deltas = [d for _, d in pairs]
        mean, low, high = bootstrap_ci(deltas, seed=bootstrap_seed)
        base_mean = statistics.mean(
            getattr(c, "miss_rate") for c in cells
            if c.policy == name and c.miss_rate is not None)
        result["per_baseline"][name] = {
            "pairs": len(pairs),
            "mean_absolute_change": mean,
            "ci95": [low, high],
            "baseline_mean": base_mean,
            "relative_change": mean / base_mean if base_mean else None,
        }
    goodput = {}
    if baseline:
        pairs = paired_differences(cells, method, baseline, "video_goodput")
        if pairs:
            deltas = [d for _, d in pairs]
            mean, low, high = bootstrap_ci(deltas, seed=bootstrap_seed)
            base_mean = statistics.mean(
                c.video_goodput for c in cells
                if c.policy == baseline and c.video_goodput is not None)
            goodput = {"pairs": len(pairs), "mean_absolute_change": mean,
                       "ci95": [low, high], "baseline_mean": base_mean,
                       "relative_change": (mean / base_mean if base_mean
                                           else None)}
    result["video_goodput_vs_strongest"] = goodput

    headline = result["per_baseline"].get(baseline or "", {})
    miss_rel = headline.get("relative_change")
    good_rel = goodput.get("relative_change")
    result["pareto_branch_one"] = {
        "criterion": ("urgent miss rate down at least 20% relative, video "
                      "goodput down at most 5%"),
        "miss_relative_change": miss_rel,
        "goodput_relative_change": good_rel,
        # An interval that crosses zero is not a reduction, whatever the
        # point estimate says.
        "interval_excludes_zero": (headline.get("ci95", [0, 0])[1] < 0
                                   if headline else None),
        "met": (miss_rel is not None and good_rel is not None
                and miss_rel <= -0.20 and good_rel >= -0.05
                and headline.get("ci95", [0, 0])[1] < 0),
    }
    return result


def completeness(cells: Sequence[Cell], *, policies: Iterable[str],
                 loads: Iterable[float], bursts: Iterable[int],
                 seeds: Iterable[int]) -> dict:
    """How much of the design actually ran.

    plan.md's acceptance is 95% completion with every missing cell either
    re-run or recorded as a deterministic failure, so the missing ones are
    listed rather than counted.
    """
    have = {(c.policy, c.load, c.burst, c.seed) for c in cells}
    wanted = [(p, l, b, s) for p in policies for l in loads
              for b in bursts for s in seeds]
    missing = [key for key in wanted if key not in have]
    return {"wanted": len(wanted), "have": len(wanted) - len(missing),
            "completion": (len(wanted) - len(missing)) / len(wanted)
            if wanted else None,
            "missing": missing}
