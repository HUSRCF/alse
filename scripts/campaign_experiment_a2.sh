#!/usr/bin/env bash
# Experiment A2: the same design with the drift envelope off.
#
# A1's first group showed the fixed-split sweep is not measuring the
# split. Four of the five splits fall back to serial about 300 times per
# cell because the envelope fires; only 16+16 pairs continuously, and it
# is the worst policy in the group. So the sweep is ranking splits by
# whether they happen to trip a mechanism of ours that claim 3.3 already
# showed to be a net cost.
#
# The envelope-off configuration is this project's best known one. This
# campaign is additive: A1 runs to completion under its pre-registration
# and both are reported. Deciding to add an arm after seeing a
# confounder is not the same as choosing a criterion after seeing a
# result, and the distinction is only worth anything if the added arm is
# declared before it runs -- which is what the dated note in
# docs/prereg-experiment-a.md is for.
#
# Waits for the card rather than refusing it, so this can be queued
# behind A1 and run unattended.
set -u
cd /home/husrcf/Code/alse
source ~/anaconda3/bin/activate 2>/dev/null

echo "waiting for the card"
while pgrep -f "run_amd_matrix_cell[.]py" > /dev/null; do sleep 60; done
echo "card free at $(date)"

R=experiments/runs/expA2
mkdir -p $R

POLS=fixed_split_4,fixed_split_8,fixed_split_16,fixed_split_24,fixed_split_28,deadline_aware,step_matched_pairing,slo_aware_partitioning,exclusive_fcfs,oracle_shortest_remaining
N=10
NODRIFT="--drift-tolerance 1000000"

done_group () { [ "$(ls "$R/$1"*.json 2>/dev/null | wc -l)" -ge "$2" ]; }

G=0
for seed in 0 1 2 3 4; do
  if [ $((seed % 2)) -eq 0 ]; then regimes="backlog arrivals"; else regimes="arrivals backlog"; fi
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
      echo "=== A2 group $G: ${regime} load $load seed $seed ==="
      timeout 7200 python scripts/run_amd_matrix_cell.py \
        --policies "$ORDER" --load "$load" --burst 4 $FLAGS $NODRIFT \
        --deadline-slack 1.5 --deadline-base burst --seed "$seed" \
        --urgent-count 40 \
        --out "$R/${PREFIX}POLICY.json" \
        > "/tmp/expA2_${TAG}${load}_${seed}.log" 2>&1
      grep -E "drawn co-run|urgent [0-9]+/" "/tmp/expA2_${TAG}${load}_${seed}.log" | tail -3
      echo "    -> $(ls $R/${PREFIX}*.json 2>/dev/null | wc -l) files"
    done
  done
done
echo "=== expA2: $(ls $R/*.json 2>/dev/null | wc -l) / 200 files ==="
