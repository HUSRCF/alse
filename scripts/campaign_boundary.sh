#!/usr/bin/env bash
# Experiment Boundary: where partitioning stops losing to priority.
#
# Pre-registered in docs/prereg-boundary.md with the derivation, the
# prediction (loss at 0.6, win at 1.05) and the criterion -- plan.md's own
# second branch: same miss rate, video goodput up at least 10%.
#
# --max-steps-per-round 16 must match the policy's cap of 16. The 2x2 ran
# at 8, which is exactly what prevented 24+8 from meeting the deadline.
set -u
cd /home/husrcf/Code/alse
source ~/anaconda3/bin/activate 2>/dev/null

echo "waiting for the card"
while pgrep -f "run_amd_matrix_cell[.]py" > /dev/null; do sleep 60; done
echo "card free at $(date)"

R=experiments/runs/expB
mkdir -p $R

POLS=exclusive_priority,pipelined_quota,fixed_split_24,step_matched_pairing
N=4
CAP=16

done_group () { [ "$(ls "$R/$1"*.json 2>/dev/null | wc -l)" -ge "$2" ]; }

G=0
for seed in 0 1 2 3 4 5 6 7 8 9; do
  if [ $((seed % 2)) -eq 0 ]; then regimes="arrivals backlog"; loads="0.6 1.05"
  else regimes="backlog arrivals"; loads="1.05 0.6"; fi
  for regime in $regimes; do
    if [ "$regime" = backlog ]; then
      FLAGS="--video-backlog --drain-grace-s 10"; TAG="bl_"
    else
      FLAGS=""; TAG=""
    fi
    for load in $loads; do
      G=$((G+1))
      PREFIX="cell_${TAG}l${load}_b4_s${seed}_"
      if done_group "$PREFIX" $N; then
        echo "skip ${regime} l${load} s${seed}"; continue
      fi
      ORDER=$(python3 -c "
p='$POLS'.split(',')
g=$((G-1)) % len(p)
print(','.join(p[g:]+p[:g]))")
      echo "=== B group $G/40: ${regime} load $load seed $seed ==="
      timeout 7200 python scripts/run_amd_matrix_cell.py \
        --policies "$ORDER" --load "$load" --burst 4 $FLAGS \
        --drift-tolerance 1000000 --max-steps-per-round $CAP \
        --deadline-slack 1.5 --deadline-base burst --seed "$seed" \
        --urgent-count 40 \
        --out "$R/${PREFIX}POLICY.json" \
        > "/tmp/expB_${TAG}${load}_${seed}.log" 2>&1
      grep -E "drawn co-run|urgent [0-9]+/" "/tmp/expB_${TAG}${load}_${seed}.log" | tail -3
      echo "    -> $(ls $R/${PREFIX}*.json 2>/dev/null | wc -l) files"
    done
  done
done
echo "=== expB: $(ls $R/*.json 2>/dev/null | wc -l) / 160 files ==="
