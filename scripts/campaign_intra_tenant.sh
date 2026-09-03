#!/usr/bin/env bash
# Experiment C: does intra-tenant concurrency make partitioning viable?
#
# Pre-registered in docs/prereg-intra-tenant.md before any cell ran.
#
# fixed_split_24 is the c=1 control: same 24+8 split, serialisation left
# in, so the difference against c4 is intra-tenant concurrency alone.
set -u
cd /home/husrcf/Code/alse
source ~/anaconda3/bin/activate 2>/dev/null

echo "waiting for the card"
while pgrep -f "run_amd_matrix_cell[.]py" > /dev/null; do sleep 60; done
echo "card free at $(date)"

R=experiments/runs/expC
mkdir -p $R
CAP=16
NODRIFT="--drift-tolerance 1000000"

# policy:requests_per_tenant. Each arm runs in its own process because
# requests_per_tenant is a runtime setting, not a policy one, so one
# process cannot hold two values of it. That is also why the output path
# must carry the policy name explicitly: run_amd_matrix_cell substitutes
# POLICY into --out only when it is given more than one policy.
SPECS="exclusive_priority:1 fixed_split_24:1 concurrent_quota_c2:2 concurrent_quota_c4:4"
ARMS=$(for s in $SPECS; do echo "${s%%:*}"; done)

# Counts the ARMS present, not the files. Run 1 counted files against a
# path that every arm shared, so the guard never fired and the campaign
# happily overwrote three cells in four for 28 groups.
done_group () {
  local prefix=$1; shift
  for pol in "$@"; do
    [ -s "$R/${prefix}${pol}.json" ] || return 1
  done
  return 0
}

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
      if done_group "$PREFIX" $ARMS; then
        echo "skip ${regime} l${load} s${seed}"; continue
      fi
      echo "=== C group $G/40: ${regime} load $load seed $seed ==="
      # Each arm runs in its own process because requests-per-tenant is a
      # runtime setting, not a policy one: one process cannot hold two
      # values of it. The drawn co-run state is therefore per arm here
      # rather than per group, and is reported per cell.
      for spec in $SPECS; do
        pol="${spec%%:*}"; rpt="${spec##*:}"
        [ -s "$R/${PREFIX}${pol}.json" ] && continue
        timeout 7200 python scripts/run_amd_matrix_cell.py \
          --policies "$pol" --load "$load" --burst 4 $FLAGS $NODRIFT \
          --max-steps-per-round $CAP --requests-per-tenant "$rpt" \
          --deadline-slack 1.5 --deadline-base burst --seed "$seed" \
          --urgent-count 40 \
          --out "$R/${PREFIX}${pol}.json" \
          > "/tmp/expC_${TAG}${load}_${seed}_${pol}.log" 2>&1
        grep -E "drawn co-run|urgent [0-9]+/" "/tmp/expC_${TAG}${load}_${seed}_${pol}.log" | tail -2
      done
      echo "    -> $(ls $R/${PREFIX}*.json 2>/dev/null | wc -l) files"
    done
  done
done
echo "=== expC: $(ls $R/*.json 2>/dev/null | wc -l) / 160 files ==="
