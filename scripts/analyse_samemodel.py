"""Decompose the same-model result: deadline actions vs the probe.

step_matched_pairing   pairing only
slo_aware_partitioning pairing + deadline actions
probing_partitioning   the above + the probe

step_matched -> slo_aware is the deadline actions.
slo_aware   -> probing   is the probe.

Reported separately by drawn co-run state, because the probe's threshold
is only crossed in the slow one and pooling the two would let a gain in
either be read as a gain overall.
"""
import json
import pathlib
import statistics
import sys

sys.path.insert(0, "src")
from burstserve.matrix_results import bootstrap_ci  # noqa: E402

ARMS = ["step_matched_pairing", "slo_aware_partitioning",
        "probing_partitioning"]
d = pathlib.Path(sys.argv[1] if len(sys.argv) > 1
                 else "experiments/runs/matrix_samemodel")

rows = {}
state = {}
for f in sorted(d.glob("cell_s*.json")):
    j = json.loads(f.read_text())
    seed = j["spec"]["seed"]
    rows.setdefault(seed, {})[j["policy"]] = j
    drawn = j.get("drawn_co_run_state") or {}
    state[seed] = (drawn.get("state"), drawn.get("externality"))

full = [s for s in sorted(rows) if all(a in rows[s] for a in ARMS)]
print(f"{len(full)} complete seeds of {len(rows)}")
by_state = {}
for s in full:
    by_state.setdefault(state[s][0], []).append(s)
print("drawn states: " + ", ".join(f"{k}={len(v)}" for k, v in
                                   sorted(by_state.items())))
print()

print(f'{"seed":>5}{"state":>7}{"ext":>7}'
      + "".join(f'{a.split("_")[0]:>13}' for a in ARMS)
      + f'{"deadline":>11}{"probe":>9}')
for s in full:
    miss = {a: rows[s][a]["urgent"]["miss_rate"] for a in ARMS}
    print(f'{s:5d}{state[s][0] or "?":>7}{state[s][1] or 0:7.3f}'
          + "".join(f'{miss[a]:13.4f}' for a in ARMS)
          + f'{miss[ARMS[1]] - miss[ARMS[0]]:+11.4f}'
          + f'{miss[ARMS[2]] - miss[ARMS[1]]:+9.4f}')

print()
for label, seeds in sorted(by_state.items()):
    if not seeds:
        continue
    print(f"=== drawn state: {label}  (n={len(seeds)}) ===")
    for name, lo, hi in (("deadline actions", 0, 1), ("probe", 1, 2),
                         ("both", 0, 2)):
        deltas = [rows[s][ARMS[hi]]["urgent"]["miss_rate"]
                  - rows[s][ARMS[lo]]["urgent"]["miss_rate"] for s in seeds]
        base = statistics.mean(rows[s][ARMS[lo]]["urgent"]["miss_rate"]
                               for s in seeds)
        mean, low, high = bootstrap_ci(deltas, seed=0)
        rel = mean / base if base else float("nan")
        print(f'  {name:<18} {mean:+.4f}  [{low:+.4f},{high:+.4f}]  '
              f'{rel * 100:+7.2f}%   excludes zero: {high < 0 or low > 0}')
    goods = [rows[s][ARMS[2]]["video"]["goodput_steps_per_s"]
             - rows[s][ARMS[0]]["video"]["goodput_steps_per_s"]
             for s in seeds]
    gbase = statistics.mean(rows[s][ARMS[0]]["video"]["goodput_steps_per_s"]
                            for s in seeds)
    print(f'  video goodput      {statistics.mean(goods):+.4f}  '
          f'{statistics.mean(goods) / gbase * 100:+7.2f}%')
    fb = {a: sum(rows[s][a]["ledger"]["serial_fallbacks"] for s in seeds)
          for a in ARMS}
    print(f'  serial fallbacks   ' + "  ".join(f"{a.split('_')[0]}={fb[a]}"
                                               for a in ARMS))
    print()
