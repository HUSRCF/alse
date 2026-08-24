#!/usr/bin/env bash
# Three campaigns, each answering one reviewer objection on a related
# submission whose setting is nearly identical to ours.
#
# Written as a file and copied over rather than piped through a heredoc.
# The previous chain used `cat > f <<"EOS"` with escaped dollars, which a
# quoted heredoc turns into literal backslash-dollars: one loop died on a
# syntax error, every run in the other handed argparse the string "\$e",
# and both marker echoes still printed. It read as success for a week.
#
# Every stage counts its own output files and prints the count. A marker
# that prints regardless of what failed is not a check.
set -u
cd /home/husrcf/Code/alse
source ~/anaconda3/bin/activate 2>/dev/null

R=experiments/runs
COMMON="--load 0.6 --burst 4 --deadline-slack 1.5 --deadline-base burst --urgent-count 40"

# ---------------------------------------------------------------- 1 ----
# The arm that answers "why not just use the primitives that exist".
# Both tenants on separate full-die streams, hardware arbitrating: no CU
# masking at all. Run on the mismatched workload, which is plan.md's
# primary one and where partitioning's advantage is largest, so the
# comparison is made where the claim is strongest rather than where it is
# most comfortable.
#
# Masked and unmasked cannot share a process -- the flag is process-wide
# -- so the arms alternate per seed and the order flips on odd seeds, and
# thermal drift is shared rather than landing on one arm.
mkdir -p $R/unmasked_base
for seed in 0 1 2 3 4 5 6 7 8 9 10 11; do
  if [ $((seed % 2)) -eq 0 ]; then order="masked unmasked"; else order="unmasked masked"; fi
  for arm in $order; do
    if [ "$arm" = unmasked ]; then
      FLAGS="--unmasked"; POLS="step_matched_pairing"
    else
      FLAGS=""; POLS="exclusive_fcfs,step_matched_pairing"
    fi
    timeout 3600 python scripts/run_amd_matrix_cell.py \
      --policies "$POLS" $FLAGS $COMMON --seed "$seed" \
      --out "$R/unmasked_base/cell_${arm}_s${seed}_POLICY.json" \
      > "/tmp/um_${arm}_${seed}.log" 2>&1
    grep -E "urgent [0-9]+/|drawn co-run" "/tmp/um_${arm}_${seed}.log" | tail -2
  done
done
echo "=== 1 unmasked: $(ls $R/unmasked_base/*.json 2>/dev/null | wc -l) files ==="

# ---------------------------------------------------------------- 2 ----
# load 0.3, the one point of plan.md's grid never measured. Bursts 2, 4
# and 8 so the row is complete rather than sampled.
mkdir -p $R/load03
G=0
POLS=exclusive_fcfs,static_even,deadline_aware,step_matched_pairing,measured_pairs_only,slo_aware_partitioning,probing_partitioning,sticky_probing_partitioning,oracle_shortest_remaining
for seed in 0 1 2 3 4; do
  for burst in 2 4 8; do
    # Rotate the policy order per group, as the main grid does: a fixed
    # order across groups confounds position with policy.
    ORDER=$(python3 -c "
import sys
p='$POLS'.split(',')
g=$G % len(p)
print(','.join(p[g:]+p[:g]))")
    timeout 3600 python scripts/run_amd_matrix_cell.py \
      --policies "$ORDER" --load 0.3 --burst "$burst" \
      --deadline-slack 1.5 --deadline-base burst --seed "$seed" \
      --urgent-count 40 \
      --out "$R/load03/cell_l0.3_b${burst}_s${seed}_POLICY.json" \
      > "/tmp/l03_${burst}_${seed}.log" 2>&1
    grep -E "urgent [0-9]+/" "/tmp/l03_${burst}_${seed}.log" | tail -1
    G=$((G+1))
  done
done
echo "=== 2 load0.3: $(ls $R/load03/*.json 2>/dev/null | wc -l) files ==="

# ---------------------------------------------------------------- 3 ----
# 30 denoising steps for the urgent tenant instead of 8. A reviewer on
# the related submission objected to 5-6 steps where 30-50 is usual; 8 is
# ASLE's default and draws the same objection.
mkdir -p $R/urgent30
G=0
for seed in 0 1 2 3 4; do
  ORDER=$(python3 -c "
p='$POLS'.split(',')
g=$G % len(p)
print(','.join(p[g:]+p[:g]))")
  timeout 5400 python scripts/run_amd_matrix_cell.py \
    --policies "$ORDER" --urgent-steps 30 $COMMON --seed "$seed" \
    --out "$R/urgent30/cell_s${seed}_POLICY.json" \
    > "/tmp/u30_${seed}.log" 2>&1
  grep -E "urgent [0-9]+/" "/tmp/u30_${seed}.log" | tail -1
  G=$((G+1))
done
echo "=== 3 urgent30: $(ls $R/urgent30/*.json 2>/dev/null | wc -l) files ==="
echo "=== all done ==="
