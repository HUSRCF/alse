"""Validate the formal masked matrix through the per-cell and matrix gates.

Every cell must pass validate_masked_cell_contract before it is allowed to
contribute an observation, and only then does the matrix gate get to see it.
"""

import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, "/data/zhuoxu/alse/src")

from burstserve.gate_a_results import (  # noqa: E402
    validate_masked_cell_contract,
    validate_masked_tpc_matrix,
)

RUNS = Path("/data/zhuoxu/alse/experiments/runs")
S = Path(__file__).parent
GPU = 1
UUID = "GPU-4cc58bdd-dfba-b754-4ddf-976885e4abfb"
BASELINE_RUN = "bs1-2dc25f5f934c5b77c88ac707eb4c017c10b608ae51d3bed3062f7d28467a1838"


def load(run_id):
    d = RUNS / run_id
    return {
        name: json.loads((d / f"{name}.json").read_text())
        for name in ("manifest", "outcome", "command")
    }


def native_of(parts):
    for key in ("native", "native_report", "native_output"):
        if key in parts["outcome"]:
            return parts["outcome"][key]
    stdout = (RUNS / parts["manifest"]["run_id"] / "stdout.log").read_text()
    for line in stdout.splitlines():
        if line.startswith("{"):
            return json.loads(line)
    return {}


baseline = load(BASELINE_RUN)
baseline_native = native_of(baseline)
baseline_sms = sorted(int(k) for k in baseline_native["observed_histogram"])
print(f"baseline cell {BASELINE_RUN[:16]}: {len(baseline_sms)} SMs")

observations, rejected = [], []
with (S / "masked_matrix_results.tsv").open() as handle:
    for row in csv.DictReader(handle, delimiter="\t"):
        if row["exit"] != "0":
            rejected.append(row)
            continue
        run_id = Path(row["run_id"]).name
        parts = load(run_id)
        config = parts["manifest"]["config"]
        obs, errors = validate_masked_cell_contract(
            config=config,
            outcome=parts["outcome"],
            native=native_of(parts),
            gate_content=config["gate_manifest"]["content"],
            command=parts["command"],
            expected_gpu=GPU,
            expected_uuid=UUID,
        )
        tag = f"{row['mode']:6s} bit={row['bit']:>2s} trial={row['trial']}"
        if errors:
            rejected.append({**row, "errors": errors})
            print(f"  REJECTED {tag}: {errors}")
            continue
        observations.append(obs)
        print(f"  {tag} -> SMs {sorted(obs['observed_sms'])}")

matrix = json.loads(
    Path("/data/zhuoxu/alse/experiments/manifests/"
         "gate_a_4090_masked_global_next.json").read_text()
)["single_tpc_matrix_after_explicit_promotion"]

verdict = validate_masked_tpc_matrix(
    observations,
    matrix=matrix,
    hardware={"sm_count": 128, "expected_tpc_count": 64},
    baseline_observed_sm_count=len(baseline_sms),
    baseline_observed_sms=baseline_sms,
    baseline_gpu_uuid=UUID,
)

print("\n=== validate_masked_tpc_matrix ===")
for name, ok in sorted(verdict["checks"].items()):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
print(f"  mapping: {verdict['tpc_sm_mapping']}")
for error in verdict["errors"]:
    print(f"  ERROR: {error}")
print(f"  cells accepted: {len(observations)}   rejected: {len(rejected)}")
print(f"  ACCEPTED: {verdict['accepted']}")

(S / "masked_matrix_verdict.json").write_text(json.dumps(
    {"baseline_run": BASELINE_RUN, "baseline_sm_count": len(baseline_sms),
     "cells": len(observations), "rejected": rejected, "verdict": verdict},
    indent=2))
