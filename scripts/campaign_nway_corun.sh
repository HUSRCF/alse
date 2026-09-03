#!/usr/bin/env bash
# The same-model co-run penalty as a function of how many ways the die is
# split -- the number every arithmetic about intra-tenant concurrency has
# been using a PAIRWISE measurement for.
#
# prereg-intra-tenant charges 1.297, which is 1.3's pair at 16+16, to a
# four-way arrangement at 8 units each. It says so; it is the single
# number its prediction turns on; it has never been measured. This is
# that measurement, and it is also what decides the follow-up: on the
# measured curves the whole die split four ways beats serving a burst
# serially on BOTH architectures (2.79 s vs 3.70 s on gfx1201 at the
# pairwise fast penalty, 3.12 s vs 3.83 s on gfx90a), and on gfx90a eight
# ways -- which needs a burst of eight, since the policy takes only as
# many slices as it has requests -- costs 8.43 s where four costs 6.24 s
# and not splitting at all costs 7.66 s. gfx1201 at the same burst
# improves all the way to eight ways, 5.41 s. **The optimum concurrency
# is the architecture's**, and on CDNA2 overshooting it is worse than
# doing nothing. The curve says where it is; this measurement says
# whether the curve is allowed to.
# The N-way penalty is what turns that into a claim.
#
# Ways are chosen so every slice width is a MEASURED quota on both dice:
#   gfx1201  32/{1,2,4,8} = {32,16,8,4}
#   gfx90a  104/{1,2,4,8} = {104,52,26,13}
# One way is a solo and is carried as a control: it must come back at
# 1.000, and if it does not the instrument is measuring something else.
set -u
cd "$(dirname "$0")/.." || exit 1

PY=${PY:-$HOME/anaconda3/envs/alse/bin/python}
OUT=${OUT:-runs/nway_corun}
WAYS=${WAYS:-"1 2 4 8"}
UNITS=${UNITS:-32}
MODEL=${MODEL:-sdxl}
WINDOW=${WINDOW:-120}
TRIALS=${TRIALS:-3}
GCDS=${GCDS:-""}          # empty: use whatever device the env selects

mkdir -p "$OUT"
i=0
for ways in $WAYS; do
  gcd=""
  if [ -n "$GCDS" ]; then
    set -- $GCDS
    n=$#
    idx=$(( i % n ))
    shift "$idx"
    gcd=$1
  fi
  i=$(( i + 1 ))
  out="$OUT/nway_${MODEL}_${UNITS}u_${ways}way.json"
  if [ -s "$out" ]; then echo "skip $out"; continue; fi
  echo "=== ${ways} ways of ${UNITS} units ${gcd:+on GCD $gcd} ==="
  env ${gcd:+HIP_VISIBLE_DEVICES=$gcd} timeout 3600 "$PY" \
    scripts/run_amd_nway_corun.py \
    --model "$MODEL" --ways "$ways" --maskable-units "$UNITS" \
    --height 768 --width 768 --steps 8 \
    --seconds "$WINDOW" --trials "$TRIALS" --seed 100 --out "$out" \
    > "/tmp/nway_${MODEL}_${UNITS}_${ways}.log" 2>&1
  echo "  rc=$? -> $(test -s "$out" && echo ok || echo MISSING)"
  grep -E "trial |Error|Traceback" "/tmp/nway_${MODEL}_${UNITS}_${ways}.log" | tail -4
done
echo "=== done ==="
