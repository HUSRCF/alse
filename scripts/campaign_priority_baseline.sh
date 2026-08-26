#!/usr/bin/env bash
# Experiment P: does spatial partitioning beat simply prioritising?
#
# Pre-registered in docs/prereg-priority-baseline.md before any cell ran,
# including the prediction and its mechanism.
#
# exclusive_fcfs has been read as "no partitioning at all" in every
# comparator set this project has built. It is not: the registry rotates
# every round, so it is whole-die time-slicing BETWEEN tenants. The
# obvious production heuristic -- whole die to the deadline-carrying
# tenant whenever it has work -- has never been measured. This campaign
# measures it.
#
# Waits for the card rather than refusing it, so it can be queued. The
# guard is keyed on the cell runner's filename so it cannot match the ssh
# command that launched this script.
set -u
cd /home/husrcf/Code/alse
source ~/anaconda3/bin/activate 2>/dev/null

echo "waiting for the card"
while pgrep -f "run_amd_matrix_cell[.]py" > /dev/null; do sleep 60; done
echo "card free at $(date)"

R=experiments/runs/expP
mkdir -p $R

POLS=exclusive_priority,exclusive_fcfs,step_matched_pairing,slo_aware_partitioning
N=4
NODRIFT="--drift-tolerance 1000000"

done_group () { [ "$(ls "$R/$1"*.json 2>/dev/null | wc -l)" -ge "$2" ]; }

G=0
for seed in 0 1 2 3 4; do
  # Regime order flips on odd seeds and the policy order rotates per
  # group, so neither the thermal history nor the arm order is
  # confounded with the condition.
  if [ $((seed % 2)) -eq 0 ]; then regimes="arrivals backlog"; else regimes="backlog arrivals"; fi
  for regime in $regimes; do
    if [ "$regime" = backlog ]; then
      FLAGS="--video-backlog --drain-grace-s 10"; TAG="bl_"
    else
      FLAGS=""; TAG=""
    fi
    for load in 0.6 1.05; do
      G=$((G+1))
      PREFIX="cell_${TAG}l${load}_b4_s${seed}_"
      if done_group "$PREFIX" $N; then
        echo "skip ${regime} l${load} s${seed}"
        continue
      fi
      ORDER=$(python3 -c "
p='$POLS'.split(',')
g=$((G-1)) % len(p)
print(','.join(p[g:]+p[:g]))")
      echo "=== P group $G/20: ${regime} load $load seed $seed ==="
      timeout 7200 python scripts/run_amd_matrix_cell.py \
        --policies "$ORDER" --load "$load" --burst 4 $FLAGS $NODRIFT \
        --deadline-slack 1.5 --deadline-base burst --seed "$seed" \
        --urgent-count 40 \
        --out "$R/${PREFIX}POLICY.json" \
        > "/tmp/expP_${TAG}${load}_${seed}.log" 2>&1
      grep -E "drawn co-run|urgent [0-9]+/" "/tmp/expP_${TAG}${load}_${seed}.log" | tail -3
      echo "    -> $(ls $R/${PREFIX}*.json 2>/dev/null | wc -l) files"
    done
  done
done
echo "=== expP: $(ls $R/*.json 2>/dev/null | wc -l) / 80 files ==="
