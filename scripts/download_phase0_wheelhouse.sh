#!/usr/bin/env bash
set -euo pipefail

repo_root="${1:-$PWD}"
wheelhouse="${2:-$repo_root/artifacts/phase0-wheelhouse}"
python_bin="${PYTHON_BIN:-python3.11}"
requirements="$repo_root/environments/phase0/pip-requirements.txt"

if [[ ! -f "$requirements" ]]; then
  echo "missing lock file: $requirements" >&2
  exit 2
fi

mkdir -p "$wheelhouse"
"$python_bin" -m pip download \
  --only-binary=:all: \
  --no-deps \
  --requirement "$requirements" \
  --dest "$wheelhouse"

echo "wheelhouse ready: $wheelhouse"
