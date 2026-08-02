"""Produce the formal AMD masked matrix.

Runs the declared matrix plus an unmasked baseline through
:mod:`burstserve.amd_cu_runner`, then judges it with the same
``validate_masked_tpc_matrix`` the CUDA line uses, unmodified.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from burstserve.amd_cu_runner import (  # noqa: E402
    dense_index_map,
    run_cell,
    source_revision,
    validate_amd_cell,
    validate_amd_manifest,
)
from burstserve.gate_a_results import validate_masked_tpc_matrix  # noqa: E402
from burstserve.provenance import canonical_json  # noqa: E402

# Provenance capture hardens its git invocations with flags that arrived in
# git 2.45. The host may ship an older one; point at a newer binary rather
# than relaxing the capture, which the CUDA line also depends on.
GIT = Path(os.environ.get("BURSTSERVE_GIT", "/usr/bin/git"))


def main() -> int:
    manifest = validate_amd_manifest(
        json.loads(
            (REPO / "experiments/manifests/amd_r9700_gfx1201.json").read_text()
        )
    )
    probe = REPO / "build/amd_cu_probe/cu_probe"
    run_root = REPO / "experiments/runs"
    run_root.mkdir(parents=True, exist_ok=True)
    # The repository carries the vendored CUDA library as a submodule. The
    # AMD line does not use it, but the tree cannot be bound exactly while a
    # gitlink is unaccounted for, so declare it at the commit it is pinned at.
    libsmctrl_pin = json.loads(
        (REPO / "vendor/LIBSMCTRL_SOURCE.json").read_text()
    )["source_commit"]
    import hashlib
    probe_digest = hashlib.sha256(probe.read_bytes()).hexdigest()
    revision = source_revision(
        REPO,
        expected_gitlinks={"vendor/libsmctrl": libsmctrl_pin},
        attested_build_files={"build/amd_cu_probe/cu_probe": probe_digest},
        git=GIT,
    )
    print(f"attested probe: {probe_digest}")
    print(f"source revision: {revision}")

    matrix = manifest["matrix"]
    hardware = manifest["hardware"]
    seed = 1
    common = dict(probe=probe, seed=seed, manifest=manifest, repo_root=REPO,
                  run_root=run_root, revision=revision)

    baseline = run_cell(mode="baseline", mask_bit=None, trial=0, **common)
    observation, errors = validate_amd_cell(
        config=json.loads((baseline["directory"] / "config.json").read_text()),
        outcome=baseline["outcome"], manifest=manifest)
    if errors:
        print("BASELINE REJECTED:", errors)
        return 1
    baseline_raw = observation["raw_unit_ids"]
    dense = dense_index_map(baseline_raw)
    print(f"baseline {baseline['run_id'][:16]}: {len(baseline_raw)} units")
    if len(dense) != hardware["maskable_units"]:
        print(f"BASELINE covers {len(dense)} units, manifest declares "
              f"{hardware['maskable_units']}")
        return 1

    observations, rejected = [], []
    for mode in matrix["modes"]:
        for bit in matrix["mask_bits"]:
            for trial in range(matrix["trials_per_cell"]):
                cell = run_cell(mode=mode, mask_bit=bit, trial=trial, **common)
                config = json.loads(
                    (cell["directory"] / "config.json").read_text())
                obs, errs = validate_amd_cell(
                    config=config, outcome=cell["outcome"], manifest=manifest)
                tag = f"{mode:15s} bit={bit:2d} trial={trial}"
                if errs:
                    rejected.append({"run_id": cell["run_id"], "errors": errs})
                    print(f"  REJECTED {tag}: {errs}")
                    continue
                # An identifier the unmasked baseline never saw is a
                # contradiction, not a newly discovered unit.
                unknown = [r for r in obs["raw_unit_ids"] if r not in dense]
                if unknown:
                    rejected.append({"run_id": cell["run_id"],
                                     "errors": [f"unit absent from baseline: {unknown}"]})
                    print(f"  REJECTED {tag}: unit absent from baseline")
                    continue
                observations.append({
                    "mode": mode,
                    "tpc_bit": bit,
                    "trial": trial,
                    "physical_gpu": hardware["device_ordinal"],
                    "gpu_uuid": hardware["gpu_uuid"],
                    "blocks": matrix["blocks"],
                    "observed_blocks": obs["observed_blocks"],
                    "observed_sms": sorted(dense[r] for r in obs["raw_unit_ids"]),
                })
                print(f"  {tag} -> unit {sorted(dense[r] for r in obs['raw_unit_ids'])}")

    verdict = validate_masked_tpc_matrix(
        observations,
        matrix={
            "modes": matrix["modes"],
            "tpc_bits": matrix["mask_bits"],
            "trials_per_cell": matrix["trials_per_cell"],
            "allowed_observed_sm_count": matrix["allowed_observed_unit_count"],
            "iterations": matrix["iterations"],
            "blocks": matrix["blocks"],
            "threads_per_block": matrix["threads_per_block"],
        },
        hardware={"sm_count": len(dense), "expected_tpc_count": len(dense)},
        baseline_observed_sm_count=len(dense),
        baseline_observed_sms=list(range(len(dense))),
        baseline_gpu_uuid=hardware["gpu_uuid"],
    )

    print("\n=== validate_masked_tpc_matrix (shared with the CUDA line) ===")
    for name, ok in sorted(verdict["checks"].items()):
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print(f"  mapping: {verdict['tpc_sm_mapping']}")
    for error in verdict["errors"]:
        print(f"  ERROR: {error}")
    print(f"  cells accepted: {len(observations)}  rejected: {len(rejected)}")
    print(f"  ACCEPTED: {verdict['accepted']}")

    report = {
        "schema_version": "burstserve.amd-cu-aggregate/v1",
        "manifest_id": manifest["manifest_id"],
        "source_revision": revision,
        "baseline_run_id": baseline["run_id"],
        "baseline_unit_count": len(dense),
        "dense_index_map": {str(k): v for k, v in sorted(dense.items())},
        "cells_accepted": len(observations),
        "rejected": rejected,
        "verdict": verdict,
        "reduced_contract": manifest["reduced_contract"],
    }
    # Write inside an allowed untracked root, never into the tracked tree the
    # run just bound: an aggregate committed there would make the next run see
    # its own previous output as a modification and label itself dirty.
    out = run_root / f"{manifest['manifest_id']}-aggregate.json"
    out.write_text(canonical_json(report) + "\n")
    print(f"aggregate: {out}")
    return 0 if verdict["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
