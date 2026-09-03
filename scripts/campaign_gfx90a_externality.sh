#!/usr/bin/env bash
# The pairwise externality table for gfx90a -- the second table the
# scheduler reads, and the last thing missing before a scheduling
# experiment can run on the second SKU.
#
# INSTRUMENT: run_amd_inproc_corun.py, which is what actually produced
# gfx1201's MEASURED_EXTERNALITY on 2026-08-06. Checked rather than
# assumed: experiments/probes/amd-r9700-cu-mask/inproc_sdxl_768_4_28_long_20260806.json
# carries schema burstserve.amd-inproc-corun/v1, n_in_overlap 21, and an
# externality of 0.3369-0.3423 -- the 1.3383 in the table. One process,
# one context, two masked streams, self-paired SDXL at 768x768. A table
# half-measured with a different harness is two quantities in one column.
#
# Splits are the die fractions gfx1201's table already covers, so the two
# architectures are compared at equal shares rather than equal unit
# counts: 13/104 = 4/32, 26 = 8, 52 = 16. Each co-run yields BOTH sides,
# so three physical splits fill five table entries: (13,91) (91,13)
# (26,78) (78,26) (52,52).
#
# The window is long for the same reason the gfx1201 asymmetric pairs
# needed a "long" run: at 13+91 the narrow side completes a call every
# 6.8 s, so a 30 s window leaves it two samples inside the overlap. 150 s
# gives it about twenty.
#
# The stream path is the only one gfx90a honours at these widths -- the
# process path silently rounds 52 to 64 and 78 to 104 (see
# mask_contract_20260903.json) -- and masked_stream reads every mask back
# and raises on a mismatch.
#
# GCD rotates because the 2026-08-25 probe resolved a deterministic 0.6%
# device-to-device difference here. Serially, never two at once: GCDs 4
# and 5 share a package, and a contention measurement run beside another
# contention measurement is not the quantity wanted. GCDs 0..3 are
# another user's job.
set -u
cd "$(dirname "$0")/.." || exit 1

PY=${PY:-$HOME/anaconda3/envs/alse/bin/python}
OUT=${OUT:-runs/gfx90a_externality}
SPLITS=${SPLITS:-"13:91 26:78 52:52"}
TRIALS=${TRIALS:-3}
WINDOW=${WINDOW:-150}
GCDS=${GCDS:-"4 5"}

export LD_LIBRARY_PATH=${LD_LIBRARY_PATH:-}:/opt/rocm/lib
export HF_HOME=${HF_HOME:-/media/PM983/alse/hf}
export HF_HUB_OFFLINE=1

mkdir -p "$OUT"
for split in $SPLITS; do
  a=${split%%:*}; b=${split##*:}
  for gcd in $GCDS; do
    out="$OUT/inproc_sdxl_768_${a}_${b}_g${gcd}.json"
    if [ -s "$out" ]; then echo "skip $out"; continue; fi
    echo "=== ${a}+${b} on GCD $gcd ==="
    HIP_VISIBLE_DEVICES=$gcd timeout 3600 "$PY" scripts/run_amd_inproc_corun.py \
      --model sdxl --units-a "$a" --units-b "$b" \
      --height 768 --width 768 --seconds "$WINDOW" \
      --trials "$TRIALS" --seed $(( 40 + gcd )) --out "$out" \
      > "/tmp/gfx90a_ext_${a}_${b}_g${gcd}.log" 2>&1
    echo "  rc=$? -> $(test -s "$out" && echo ok || echo MISSING)"
    tail -4 "/tmp/gfx90a_ext_${a}_${b}_g${gcd}.log"
  done
done
echo "=== done ==="
