#!/usr/bin/env bash
# Experiment A3: does run-time choice beat not partitioning at all?
#
# Pre-registered in docs/prereg-experiment-a3.md before any cell ran.
# Independent replication on fifteen NEW seeds (5..19), arrivals only,
# envelope off, four arms. n = 30 paired configurations, fixed there.
# A's cells are not reanalysed and A's result does not change.
#
# The seed set is disjoint from A's 0..4 on purpose: adding seeds to a
# campaign whose interval crossed zero, and stopping when it stops
# crossing, is optional stopping.
#
# Waits for the card rather than refusing it, so this can be queued.
set -u
cd /home/husrcf/Code/alse
source ~/anaconda3/bin/activate 2>/dev/null

# The guard lives here and is keyed on the cell runner's filename, so it
# cannot match the ssh command that launched this script -- which is the
# defect that made an earlier campaign refuse to start.
echo "waiting for the card"
while pgrep -f "run_amd_matrix_cell[.]py" > /dev/null; do sleep 60; done
echo "card free at $(date)"

R=experiments/runs/expA3
mkdir -p $R

POLS=exclusive_fcfs,step_matched_pairing,slo_aware_partitioning,fixed_split_8
N=4
NODRIFT="--drift-tolerance 1000000"

done_group () { [ "$(ls "$R/$1"*.json 2>/dev/null | wc -l)" -ge "$2" ]; }

G=0
for seed in 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19; do
  # Load order flips on odd seeds and the policy order rotates per group,
  # so neither the thermal history nor the arm order is confounded with
  # the condition.
  if [ $((seed % 2)) -eq 0 ]; then loads="0.6 1.05"; else loads="1.05 0.6"; fi
  for load in $loads; do
    G=$((G+1))
    PREFIX="cell_l${load}_b4_s${seed}_"
    if done_group "$PREFIX" $N; then
      echo "skip l${load} s${seed}"
      continue
    fi
    ORDER=$(python3 -c "
p='$POLS'.split(',')
g=$((G-1)) % len(p)
print(','.join(p[g:]+p[:g]))")
    echo "=== A3 group $G/30: load $load seed $seed ==="
    timeout 7200 python scripts/run_amd_matrix_cell.py \
      --policies "$ORDER" --load "$load" --burst 4 $NODRIFT \
      --deadline-slack 1.5 --deadline-base burst --seed "$seed" \
      --urgent-count 40 \
      --out "$R/${PREFIX}POLICY.json" \
      > "/tmp/expA3_${load}_${seed}.log" 2>&1
    grep -E "drawn co-run|urgent [0-9]+/" "/tmp/expA3_${load}_${seed}.log" | tail -3
    echo "    -> $(ls $R/${PREFIX}*.json 2>/dev/null | wc -l) files"
  done
done
echo "=== expA3: $(ls $R/*.json 2>/dev/null | wc -l) / 120 files ==="
