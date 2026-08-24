#!/usr/bin/env bash
# Experiment A: is choosing the split at run time worth anything?
#
# Criterion, direction and all three verdicts are fixed in
# docs/prereg-experiment-a.md, written before this ran.
#
# Ten policies per cell, all in one process, so every arm sees the same
# drawn co-run state and the same thermal history. Order rotates per
# group: a fixed order across groups confounds position with policy.
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

# Five static splits, three adaptive policies, floor and ceiling.
POLS=fixed_split_4,fixed_split_8,fixed_split_16,fixed_split_24,fixed_split_28,deadline_aware,step_matched_pairing,slo_aware_partitioning,exclusive_fcfs,oracle_shortest_remaining
N=10

done_group () {  # $1 = prefix, $2 = expected count
  [ "$(ls "$R/$1"*.json 2>/dev/null | wc -l)" -ge "$2" ]
}

G=0
for load in 0.6 1.05; do
  for seed in 0 1 2 3 4; do
    # Incremented before the skip, so a resumed run keeps the same
    # rotation a fresh one would have used.
    G=$((G+1))
    if done_group "cell_l${load}_b4_s${seed}_" $N; then
      echo "skip l${load} s${seed} (already $N files)"
      continue
    fi
    ORDER=$(python3 -c "
p='$POLS'.split(',')
g=$((G-1)) % len(p)
print(','.join(p[g:]+p[:g]))")
    echo "=== group $G: load $load seed $seed ==="
    timeout 7200 python scripts/run_amd_matrix_cell.py \
      --policies "$ORDER" --load "$load" --burst 4 \
      --deadline-slack 1.5 --deadline-base burst --seed "$seed" \
      --urgent-count 40 \
      --out "$R/cell_l${load}_b4_s${seed}_POLICY.json" \
      > "/tmp/expA_${load}_${seed}.log" 2>&1
    grep -E "drawn co-run|urgent [0-9]+/" "/tmp/expA_${load}_${seed}.log" | tail -3
    echo "    -> $(ls $R/cell_l${load}_b4_s${seed}_*.json 2>/dev/null | wc -l) files"
  done
done
echo "=== expA: $(ls $R/*.json 2>/dev/null | wc -l) / 100 files ==="
