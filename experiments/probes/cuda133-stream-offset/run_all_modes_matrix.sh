#!/bin/bash
# Masked Gate-A matrix: {global,next} x TPC bit {0,31,32,63} x 3 trials = 24 cells.
#
# NOT RUN YET. Preconditions that must all hold at invocation time:
#   1. The promoted manifest is committed and the tree is clean, because
#      load_gate_manifest_record requires head == index == worktree.
#   2. exclusive_reservation_evidence covers the WHOLE matrix window. The
#      reservation timestamps are part of the formal run identity, so a window
#      that expires mid-matrix would split the 24 cells across two identities
#      and the matrix could never be accepted as one.
#   3. GPU 1 has no other compute process (recheck immediately before, not
#      once at the top of the session).
#
# The runner refuses a masked run from a dirty tree, on a busy GPU, or with a
# stale reservation; this script does not try to work around any of that.
set -u
cd /data/zhuoxu/alse

MANIFEST=experiments/manifests/gate_a_4090_masked_all_modes.json
# Reverse-engineered CUDA 13.3 CUstream mask offset, absolute: 0x5fc.
# The probe refuses anything but the constant compiled into it, and
# the manifest declares only this value.
STREAM_MASK_OFF=1532
GPU=1
SEED=1
S=/tmp/claude-1005/-data-zhuoxu-alse/bda35aff-72c5-4f5e-ab9d-b70d8c02190a/scratchpad/streamoff
RESULTS="$S/all_modes_matrix_results.tsv"

# Fail before burning 24 runs if the reservation cannot cover the matrix.
# Cells take roughly 5-15s each, so 30 minutes of remaining window is ample.
PYTHONPATH=src /usr/bin/python3 -B -c "
import json, sys
from datetime import datetime, timedelta, timezone
m = json.load(open('$MANIFEST'))
r = m['safety']['exclusive_reservation_evidence']
now = datetime.now(timezone.utc)
f = lambda s: datetime.fromisoformat(s.replace('Z', '+00:00'))
start, end = f(r['valid_from_utc']), f(r['valid_until_utc'])
if not (start <= now < end):
    sys.exit(f'reservation window {start}..{end} does not contain {now}')
if end - now < timedelta(minutes=30):
    sys.exit(f'only {(end-now).total_seconds()/60:.1f} min of reservation left; '
             'refresh and re-commit the manifest before running the matrix')
print(f'reservation ok: {(end-now).total_seconds()/3600:.1f}h remaining')
" || exit 1

if [ -n "$(git status --porcelain)" ]; then
  echo "ABORT: formal masked evidence requires a clean tree" >&2
  git status --short >&2
  exit 1
fi

if find src -name __pycache__ -type d | grep -q .; then
  echo "ABORT: bytecode caches in src/ will fail the formal source policy" >&2
  exit 1
fi

printf 'mode\tbit\ttrial\texit\twall_s\trun_id\n' > "$RESULTS"

for mode in global next stream; do
  for bit in 0 31 32 63; do
    for trial in 0 1 2; do
      # Fail closed if anyone took the card since the last cell.
      busy=$(nvidia-smi --id=$GPU --query-compute-apps=pid --format=csv,noheader | wc -l)
      if [ "$busy" -ne 0 ]; then
        echo "ABORT: GPU $GPU acquired $busy foreign process(es) mid-matrix" >&2
        exit 1
      fi

      # Only stream writes the opaque struct, so only stream carries the
      # offset; the gate rejects MASK_OFF on any other mode.
      offset_args=()
      if [ "$mode" = stream ]; then
        offset_args=(--experimental-mask-off "$STREAM_MASK_OFF")
      fi

      start=$(date +%s)
      out=$(PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 \
        /usr/bin/python3 -B -m burstserve.smctrl_runner run \
        --physical-gpu "$GPU" \
        --mode "$mode" \
        "${offset_args[@]}" \
        --enabled-tpc "$bit" \
        --trial "$trial" \
        --seed "$SEED" \
        --gate-manifest "$MANIFEST" \
        --experimental-allow-unsupported-driver 2>&1)
      code=$?
      wall=$(( $(date +%s) - start ))

      run_id=$(echo "$out" | tail -1)
      printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$mode" "$bit" "$trial" "$code" "$wall" "$run_id" >> "$RESULTS"
      echo "$mode bit=$bit trial=$trial -> exit $code (${wall}s)"
      if [ "$code" -ne 0 ]; then
        echo "--- runner output ---" >&2
        echo "$out" >&2
        # Keep going: a rejected cell is evidence too, and the matrix
        # validator must see the gap rather than have it hidden.
      fi
    done
  done
done
echo "MATRIX ATTEMPTED (24 cells); see $RESULTS"
