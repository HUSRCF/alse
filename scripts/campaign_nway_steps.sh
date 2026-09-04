#!/usr/bin/env bash
# The step-level co-run sweep: N-way equal splits, and the pairs the
# externality table holds, measured on resident step adapters.
#
# Written 2026-09-04 after the whole-call harness was shown to charge
# `hipMalloc`-class device drains as contention -- 7.3x on a mismatched
# pair that reads 0.995 when stepped in place, and 1.3559 on a 13-unit
# slice that reads 0.998. The wide-slice entries survive that; the
# narrow ones do not.
#
# Two things this produces that nothing else does:
#   * the N-way penalty measured without per-call allocation, which is
#     1.11's falsifier, and
#   * harness-matched pairwise entries, so the N-way column and the
#     pairwise column it is compared against are the same quantity.
#
# On gfx1201 it also produces the number `docs/prereg-intra-tenant.md`'s
# prediction turns on: four ways at 8 units each, which that document
# stands in for with 1.297 -- 1.3's PAIR at 16+16 -- and says so.
#
#   MASKABLE=32  bash scripts/campaign_nway_steps.sh     # gfx1201
#   MASKABLE=104 bash scripts/campaign_nway_steps.sh     # gfx90a
set -u
cd "$(dirname "$0")/.."

MASKABLE=${MASKABLE:-32}
OUT=${OUT:-runs/nway_steps_${MASKABLE}u}
EPISODES=${EPISODES:-6}
STEPS=${STEPS:-14}
MODEL=${MODEL:-sdxl}

if [ "$MASKABLE" = 32 ]; then
  WAYS=${WAYS:-"1 2 4 8"}
  PAIRS=${PAIRS:-"4,28 8,24 16,16 24,8 28,4"}
else
  WAYS=${WAYS:-"1 2 4 8"}
  # 52,52 is the same arrangement as `--ways 2`; kept as a repeatability
  # check, which is how 1.1954 and 1.2012 came to be two numbers.
  PAIRS=${PAIRS:-"13,91 26,78 52,52 78,26 91,13"}
fi
# The reversed layouts are the falsifier for the position gradient found
# on gfx90a: offset 0 pays about 7% more than the top offset at four
# ways, and reversing the layout moved the gradient with the position.
REVERSED=${REVERSED:-"4 8"}

echo "waiting for the card"
while pgrep -f "run_amd_matrix_cell[.]py|run_amd_nway|run_amd_mismatched" \
      > /dev/null; do sleep 60; done
echo "card free at $(date)"

mkdir -p "$OUT"
run () {
  local out=$1; shift
  [ -s "$out" ] && { echo "skip $out"; return 0; }
  python scripts/run_amd_nway_steps.py --model "$MODEL" \
    --maskable-units "$MASKABLE" --episodes "$EPISODES" --steps "$STEPS" \
    --warmup 4 --seed 0 --out "$out" "$@" || exit 1
}

for w in $WAYS; do
  echo "=== ways $w ==="
  run "$OUT/nway_steps_${MODEL}_${MASKABLE}u_${w}way.json" --ways "$w"
done
for p in $PAIRS; do
  tag=$(echo "$p" | tr , _)
  echo "=== pair $p ==="
  run "$OUT/nway_steps_${MODEL}_${MASKABLE}u_${tag}.json" --widths "$p"
done
for w in $REVERSED; do
  echo "=== ways $w reversed ==="
  run "$OUT/nway_steps_${MODEL}_${MASKABLE}u_${w}way_rev.json" \
    --ways "$w" --reverse-offsets
done
echo "=== step-level sweep at ${MASKABLE}u: $(ls "$OUT"/*.json | wc -l) runs ==="
