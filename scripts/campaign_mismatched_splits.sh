#!/usr/bin/env bash
# The mismatched pair at gfx1201's five splits, on the corrected
# instrument, with its own same-model comparator measured in the same
# sweep. Pre-registered in docs/prereg-mismatched-splits.md, 2026-09-06.
#
# Why the comparator is re-measured rather than read off disk: the five
# same-model pairs already in experiments/probes/gfx1201/nway_steps/ come
# from the pre-fix instrument, and their allocator control fails -- at
# eight ways the post-episode solo reads 2048 ms against a solo_before of
# 504. A number whose control failed is not a comparator.
#
#   bash scripts/campaign_mismatched_splits.sh
set -u
cd "$(dirname "$0")/.."

OUT=${OUT:-experiments/probes/gfx1201/pairs_fixed}
# Activate the interpreter the sweep was launched under. On 2026-09-06 a
# resume inherited a shell without it, `python` was not on PATH, and the
# only reason that was not a silent skip is the file check in `run`.
if ! command -v python > /dev/null; then
  # shellcheck disable=SC1091
  source "$HOME/anaconda3/bin/activate"
fi

MASKABLE=${MASKABLE:-32}
EPISODES=${EPISODES:-6}
STEPS=${STEPS:-14}
PAIRS=${PAIRS:-"4,28 8,24 16,16 24,8 28,4"}

# Match the python invocation, not the harness name: a guard that can
# match its own launcher is not a guard. See campaign_nway_steps.sh.
echo "waiting for the card"
while pgrep -f "python .*(run_amd_matrix_cell|run_amd_nway|run_amd_mismatched)" \
      > /dev/null; do sleep 60; done
echo "card free at $(date)"

mkdir -p "$OUT"
run () {
  local out=$1; shift
  [ -s "$out" ] && { echo "skip $out"; return 0; }
  echo "=== $(basename "$out") $(date +%H:%M:%S) ==="
  # Not `|| exit 1`. On 2026-09-06 one run wrote its file, printed its
  # verdict and then hung in teardown; killing it returned 143 here and
  # took the remaining four cells with it. A run that produced its output
  # has succeeded whatever its exit status says, so the check is the file.
  python scripts/run_amd_nway_steps.py \
    --maskable-units "$MASKABLE" --episodes "$EPISODES" --steps "$STEPS" \
    --warmup 4 --seed 0 --diagnose --out "$out" "$@"
  [ -s "$out" ] || { echo "FAILED: $out was not written"; exit 1; }
}

# E: the rotation baseline for the video tenant. SDXL's is
# nway_walled/w1.json on this same instrument.
run "$OUT/solo_cog_32.json"   --widths 32    --models cogvideox-2b

# A: the comparator half.
for p in $PAIRS; do
  run "$OUT/same_sdxl_$(echo "$p" | tr , _).json" --widths "$p" --model sdxl
done

# B: the question.
for p in $PAIRS; do
  run "$OUT/mm_$(echo "$p" | tr , _).json" --widths "$p" \
      --models sdxl,cogvideox-2b
done

# C: does the penalty follow the model or the die position.
run "$OUT/mm_16_16_rev.json" --widths 16,16 --models sdxl,cogvideox-2b \
    --reverse-offsets

# D: the video side's own same-model control, which 1.12 did not have.
run "$OUT/same_cog_16_16.json" --widths 16,16 \
    --models cogvideox-2b,cogvideox-2b

echo "=== done: $(ls "$OUT"/*.json | wc -l) runs at $(date) ==="
