#!/usr/bin/env bash
# Experiment A: is choosing the split at run time worth anything?
#
# Criterion, direction and all three verdicts are fixed in
# docs/prereg-experiment-a.md, written before this ran.
#
# Two workload regimes, because the question needs both:
#
#   arrivals  the 405-cell grid's own trace. Commensurable with it, and
#             the tenants are both runnable for only 26% of the horizon
#             at load 0.6 and 45% at 1.05 -- measured, not assumed.
#   backlog   the video tenant has a standing queue, so it is never idle
#             and the split decision is live for the whole horizon. This
#             is also what spatial partitioning exists for: a
#             latency-critical tenant beside throughput work that always
#             has more to do.
#
# Ten policies per cell, all in one process, so every arm sees the same
# drawn co-run state and the same thermal history. Order rotates per
# group: a fixed order across groups confounds position with policy.
# Regime order flips on odd seeds so that drift over the campaign does
# not land on one regime.
set -u
cd /home/husrcf/Code/alse
source ~/anaconda3/bin/activate 2>/dev/null

# Refuse to start on top of another run. The pattern is the cell
# runner's filename, which never appears in this script's own command
# line: a guard written inline in an ssh command matched the ssh command
# itself and refused to start anything at all.
if pgrep -f "run_amd_matrix_cell[.]py" > /dev/null; then
  echo "REFUSING: a cell runner is already on this card"
  exit 1
fi

R=experiments/runs/expA
mkdir -p $R

POLS=fixed_split_4,fixed_split_8,fixed_split_16,fixed_split_24,fixed_split_28,deadline_aware,step_matched_pairing,slo_aware_partitioning,exclusive_fcfs,oracle_shortest_remaining
N=10

done_group () {  # $1 = prefix, $2 = expected count
  [ "$(ls "$R/$1"*.json 2>/dev/null | wc -l)" -ge "$2" ]
}

G=0
for seed in 0 1 2 3 4; do
  if [ $((seed % 2)) -eq 0 ]; then regimes="backlog arrivals"; else regimes="arrivals backlog"; fi
  for regime in $regimes; do
    # Goodput is steps per second of actual run, and a backlogged video
    # tenant never drains, so the run keeps going for the whole grace
    # period after urgent arrivals stop -- with the default 120 s that
    # tail is half the cell, video running solo, diluting exactly the
    # contention the regime exists to create. 10 s lets the last urgent
    # burst finish and leaves the tail under a tenth of the horizon.
    if [ "$regime" = backlog ]; then
      FLAGS="--video-backlog --drain-grace-s 10"; TAG="bl_"
    else
      FLAGS=""; TAG=""
    fi
    for load in 0.6 1.05; do
      # Incremented before the skip, so a resumed run keeps the same
      # rotation a fresh one would have used.
      G=$((G+1))
      PREFIX="cell_${TAG}l${load}_b4_s${seed}_"
      if done_group "$PREFIX" $N; then
        echo "skip ${regime} l${load} s${seed} (already $N files)"
        continue
      fi
      ORDER=$(python3 -c "
p='$POLS'.split(',')
g=$((G-1)) % len(p)
print(','.join(p[g:]+p[:g]))")
      echo "=== group $G: ${regime} load $load seed $seed ==="
      timeout 7200 python scripts/run_amd_matrix_cell.py \
        --policies "$ORDER" --load "$load" --burst 4 $FLAGS \
        --deadline-slack 1.5 --deadline-base burst --seed "$seed" \
        --urgent-count 40 \
        --out "$R/${PREFIX}POLICY.json" \
        > "/tmp/expA_${TAG}${load}_${seed}.log" 2>&1
      grep -E "drawn co-run|urgent [0-9]+/" "/tmp/expA_${TAG}${load}_${seed}.log" | tail -3
      echo "    -> $(ls $R/${PREFIX}*.json 2>/dev/null | wc -l) files"
    done
  done
done
echo "=== expA: $(ls $R/*.json 2>/dev/null | wc -l) / 200 files ==="
