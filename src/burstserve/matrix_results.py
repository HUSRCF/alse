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
    # Strict priority on the whole die. Added 2026-08-26 after noticing
    # that ``exclusive_fcfs`` rotates and is therefore time-slicing, not
    # priority, so the obvious production heuristic was in no comparator
    # set anywhere in this project.
    "exclusive_priority",
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
    # Dynamic quota selection over the five measured splits. Added
    # 2026-08-27: it is the first policy in this project that can issue
    # an asymmetric grant, so it is a method rather than a baseline.
    "deadline_quota",
    "pipelined_quota",
    "slo_aware_partitioning",
    "probing_partitioning",
    "sticky_probing_partitioning",
)
ORACLE_POLICIES = ("oracle_shortest_remaining",)


def output_path(out_template: str, policy_name: str, many: bool) -> Path:
    """Where this policy's cell is written.

    Substitutes whenever the template asks for it, not only when this
    process holds several policies. expC could not put its arms in one
    process -- ``requests_per_tenant`` is a runtime setting, so one
    process cannot hold two values of it -- and so passed one
    ``--policies`` per process. ``many`` was therefore False, all four
    arms of every group wrote to the same literal ``..._POLICY.json``
    path, and the last arm overwrote the other three. It ran that way for
    28 groups: the file existed, the campaign's guard counted files
    rather than arms, and nothing said a word.

    A template that names POLICY wants POLICY substituted. There is no
    case where it does not.
    """
    if many or "POLICY" in out_template:
        return Path(out_template.replace("POLICY", policy_name))
    return Path(out_template)


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
    # Added 2026-09-03 so one directory can hold two regimes and the
    # drawn state can be reported per cell, which every campaign since P
    # has had to do by reading the payloads again outside this module.
    video_backlog: bool = False
    drawn_co_run_state: dict | None = None
    ledger: dict | None = None
    # Runtime factors a campaign may vary. They are not in ``spec``
    # because they are properties of the runtime rather than the trace,
    # and the 2x2 varied one of them: pooling across it would average two
    # arms of a factorial design into one number.
    max_steps_per_round: int = 1
    requests_per_tenant: int = 1

    @property
    def co_run_state(self) -> str | None:
        """``fast``, ``slow`` or None -- 1.3's draw, as this cell saw it.

        Every campaign since P has had to report the draw per cell, and
        every one of them reopened the payloads to do it.
        """
        if not self.drawn_co_run_state:
            return None
        return self.drawn_co_run_state.get("state")

    @property
    def regime(self) -> str:
        return "backlog" if self.video_backlog else "arrivals"

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
            video_backlog=bool(spec.get("video_backlog", False)),
            drawn_co_run_state=payload.get("drawn_co_run_state"),
            ledger=payload.get("ledger"),
            max_steps_per_round=int(payload.get("max_steps_per_round") or 1),
            requests_per_tenant=int(payload.get("requests_per_tenant") or 1),
        ))
    return cells


def paired_differences(cells: Sequence[Cell], policy: str, against: str,
                       metric: str, *, extra_key=None
                       ) -> list[tuple[tuple, float]]:
    """Per configuration, one policy's metric minus another's.

    Only configurations where *both* policies have a cell contribute. A
    missing cell drops the pair rather than the seed's other half, which
    would compare a policy's easy configurations against another's hard
    ones.

    The configuration is ``(load, burst, seed)`` by default, which was
    enough while every campaign varied nothing else. It is **not** enough
    for a directory holding two regimes: `expP`, the 2x2 and `expB` all
    put arrivals and backlog cells side by side, and there the same
    ``(load, burst, seed)`` names two different traces. The index would
    silently keep one of them and halve the comparison without saying so.

    ``extra_key`` is a callable on a cell whose result is prepended, so
    the seed stays last and ``cluster_bootstrap_ci`` still finds it.
    Every published number was computed per regime and so is unaffected,
    but a pooled one would not have been.
    """
    if extra_key is None:
        def extra_key(_cell):
            return ()
    index: dict[tuple, Cell] = {}
    for cell in cells:
        key = (cell.policy,) + tuple(extra_key(cell)) + (
            cell.load, cell.burst, cell.seed)
        if key in index:
            raise ValueError(
                f"two cells share the configuration {key}; the key does "
                f"not separate them -- {index[key].path} and {cell.path}")
        index[key] = cell
    # Natural order, with repr only as a fallback for keys that have
    # none. The order is not cosmetic: ``bootstrap_ci`` indexes into the
    # returned list, so a different order is a different resample and
    # A3's published cell interval moves from -0.1361 to -0.1387.
    try:
        ordered = sorted(index.items())
    except TypeError:
        ordered = sorted(index.items(), key=repr)
    out = []
    for key, cell in ordered:
        if key[0] != policy:
            continue
        other = index.get((against,) + key[1:])
        if other is None:
            continue
        mine, theirs = getattr(cell, metric), getattr(other, metric)
        if mine is None or theirs is None:
            continue
        out.append((key[1:], mine - theirs))
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


def cluster_bootstrap_ci(keyed: Sequence[tuple[tuple, float]], *,
                         seed: int = 0, resamples: int = 10000,
                         confidence: float = 0.95,
                         cluster=lambda key: key[-1]
                         ) -> tuple[float, float, float]:
    """Percentile bootstrap resampling whole SEEDS, not cells.

    ``build_trace`` seeds its generator with ``spec.seed`` alone and the
    load enters only as a rate, so one seed at load 0.6 and at load 1.05
    is the same arrival sequence rescaled in time -- verified, the ratio
    of inter-burst gaps is constant to 1e-9. A cell bootstrap therefore
    treats two views of one trace as two independent units and reports an
    interval that is too tight: on A3 it gave [-0.1361, -0.0161] where
    the cluster bootstrap gives [-0.1521, -0.0044], three and a half
    times thinner at the near end. Experiment A's verdict 3 did not
    survive the correction.

    Every claim in this project made since 2026-08-26 uses this interval
    and until now it was computed ad hoc, once per campaign, outside the
    repository. That is exactly the kind of thing this project has
    learned not to leave uncommitted.

    ``keyed`` is what ``paired_differences`` returns: ``(key, value)``
    pairs whose key ends in the seed. Returns (mean, low, high) of the
    pooled differences, so a seed contributing more cells carries more
    weight -- which is the right thing when the cells are the repeated
    measures of that one trace.
    """
    if not keyed:
        raise ValueError("no paired differences to bootstrap")
    groups: dict[object, list[float]] = {}
    for key, value in keyed:
        groups.setdefault(cluster(key), []).append(value)
    # Sorted by the cluster's own order where it has one. Sorting by
    # ``repr`` instead puts seed 10 before seed 5, which changes which
    # cluster each draw lands on and moves A3's lower bound from -0.1521
    # to -0.1488 -- immaterial to the verdict, and still a published
    # number a committed tool could not regenerate.
    try:
        names = sorted(groups)
    except TypeError:            # mixed key types have no natural order
        names = sorted(groups, key=repr)
    clusters = [groups[name] for name in names]
    rng = random.Random(seed)
    n = len(clusters)
    means = []
    for _ in range(resamples):
        pooled: list[float] = []
        for _ in range(n):
            pooled.extend(clusters[rng.randrange(n)])
        means.append(sum(pooled) / len(pooled))
    means.sort()
    tail = (1.0 - confidence) / 2.0
    low = means[int(tail * resamples)]
    high = means[min(resamples - 1, int((1.0 - tail) * resamples))]
    flat = [value for _, value in keyed]
    return statistics.mean(flat), low, high


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
