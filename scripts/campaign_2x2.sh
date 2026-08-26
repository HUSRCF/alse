#!/usr/bin/env bash
# Experiment 2x2: do the two named defects explain the loss to priority?
#
# Pre-registered in docs/prereg-experiment-2x2.md before any cell ran,
# including the two predictions, the cluster-bootstrap analysis and the
# wiring check.
#
#   barrier on  (--max-steps-per-round 1)  x  2 actions (step_matched_pairing)
#   barrier off (--max-steps-per-round 8)  x  6 actions (deadline_quota)
#
# exclusive_priority is the fixed opponent in every group and is also the
# wiring check: it grants one request per round, so the barrier flag
# cannot reach it and its numbers must agree across the two columns.
#
# TEN seeds, not five. build_trace seeds on spec.seed alone, so the two
# loads of one seed are the same arrival sequence rescaled in time and the
# independent unit is the seed. Experiment A ran five and its one positive
# verdict did not survive that correction.
set -u
cd /home/husrcf/Code/alse
source ~/anaconda3/bin/activate 2>/dev/null

echo "waiting for the card"
while pgrep -f "run_amd_matrix_cell[.]py" > /dev/null; do sleep 60; done
echo "card free at $(date)"

R=experiments/runs/exp2x2
mkdir -p $R

POLS=exclusive_priority,step_matched_pairing,deadline_quota
N=3
NODRIFT="--drift-tolerance 1000000"

done_group () { [ "$(ls "$R/$1"*.json 2>/dev/null | wc -l)" -ge "$2" ]; }

G=0
for seed in 0 1 2 3 4 5 6 7 8 9; do
  # Barrier order flips on odd seeds, regime order flips on odd seeds, and
  # the policy order rotates per group, so no column, regime or arm is
  # confounded with position in the thermal history.
  if [ $((seed % 2)) -eq 0 ]; then bars="1 8"; regimes="arrivals backlog"
  else bars="8 1"; regimes="backlog arrivals"; fi
  for bar in $bars; do
    for regime in $regimes; do
      if [ "$regime" = backlog ]; then
        FLAGS="--video-backlog --drain-grace-s 10"; TAG="bl_"
      else
        FLAGS=""; TAG=""
      fi
      for load in 0.6 1.05; do
        G=$((G+1))
        PREFIX="cell_m${bar}_${TAG}l${load}_b4_s${seed}_"
        if done_group "$PREFIX" $N; then
          echo "skip m${bar} ${regime} l${load} s${seed}"
          continue
        fi
        ORDER=$(python3 -c "
p='$POLS'.split(',')
g=$((G-1)) % len(p)
print(','.join(p[g:]+p[:g]))")
        echo "=== 2x2 group $G/80: max_steps=$bar ${regime} load $load seed $seed ==="
        timeout 7200 python scripts/run_amd_matrix_cell.py \
          --policies "$ORDER" --load "$load" --burst 4 $FLAGS $NODRIFT \
          --max-steps-per-round "$bar" \
          --deadline-slack 1.5 --deadline-base burst --seed "$seed" \
          --urgent-count 40 \
          --out "$R/${PREFIX}POLICY.json" \
          > "/tmp/exp2x2_m${bar}_${TAG}${load}_${seed}.log" 2>&1
        grep -E "drawn co-run|urgent [0-9]+/" "/tmp/exp2x2_m${bar}_${TAG}${load}_${seed}.log" | tail -3
        echo "    -> $(ls $R/${PREFIX}*.json 2>/dev/null | wc -l) files"
      done
    done
  done
done
echo "=== exp2x2: $(ls $R/*.json 2>/dev/null | wc -l) / 240 files ==="
