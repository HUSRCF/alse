"""Provenance-complete runner for the AMD CU-mask probe.

This is a deliberately reduced contract, scoped to one setup: a single
`gfx1201` card, one operator, masking through documented HIP interfaces. The
reduction and the conditions it depends on are enumerated in
``docs/amd-reduced-contract.md``.

The module is separate from :mod:`burstserve.smctrl_runner` on purpose. It
imports provenance and validation primitives and modifies none of them, so no
CUDA gate can be loosened by a change made for AMD. What it keeps is
everything that makes evidence checkable -- content-addressed identity, Git
binding, a declared matrix, an attested binary -- and what it drops is
machinery that exists to survive a blind write on a shared machine.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .git_provenance import capture_repository
from .provenance import canonical_json, derive_run_id

# Distinct from the CUDA cell schema so an AMD cell can never be counted in a
# CUDA aggregate: those validators pin their schema string exactly.
AMD_CELL_SCHEMA_VERSION = "burstserve.amd-cu-cell/v1"
AMD_MANIFEST_SCHEMA_VERSION = "burstserve.amd-cu-manifest/v1"
AMD_PROBE_SCHEMA_VERSION = "burstserve.cu-probe-amd/v1"

AMD_MODES = ("baseline", "stream_cu_mask", "global_cu_mask")
MASKED_AMD_MODES = frozenset({"stream_cu_mask", "global_cu_mask"})
CANONICAL_AMD_MASKED_MODE_ORDER = ("global_cu_mask", "stream_cu_mask")

MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "manifest_id",
        "hardware",
        "source",
        "matrix",
        "reduced_contract",
    }
)
HARDWARE_KEYS = frozenset(
    {"gpu_name", "gcn_arch", "gpu_uuid", "maskable_units", "device_ordinal"}
)
MATRIX_KEYS = frozenset(
    {
        "modes",
        "mask_bits",
        "trials_per_cell",
        "allowed_observed_unit_count",
        "iterations",
        "blocks",
        "threads_per_block",
    }
)


class AmdContractError(RuntimeError):
    """A cell or manifest violated the declared AMD contract."""


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_amd_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Reject a manifest that does not declare its matrix exactly."""

    if not isinstance(manifest, Mapping) or set(manifest) != set(MANIFEST_KEYS):
        raise AmdContractError("AMD manifest keys are not exact")
    if manifest["schema_version"] != AMD_MANIFEST_SCHEMA_VERSION:
        raise AmdContractError("AMD manifest schema is not v1")
    hardware = manifest["hardware"]
    matrix = manifest["matrix"]
    if not isinstance(hardware, Mapping) or set(hardware) != set(HARDWARE_KEYS):
        raise AmdContractError("AMD manifest hardware keys are not exact")
    if not isinstance(matrix, Mapping) or set(matrix) != set(MATRIX_KEYS):
        raise AmdContractError("AMD manifest matrix keys are not exact")

    modes = matrix["modes"]
    # The same shape the CUDA line requires: a canonically ordered subset with
    # at least two mechanisms, because a mapping produced by one mechanism is
    # confirmed only by the observation it came from. This is not part of the
    # reduction and must not become part of it.
    if (
        not isinstance(modes, list)
        or len(modes) < 2
        or modes
        != [m for m in CANONICAL_AMD_MASKED_MODE_ORDER if m in set(modes)]
    ):
        raise AmdContractError(
            "AMD matrix must declare at least two masking mechanisms in "
            "canonical order"
        )
    bits = matrix["mask_bits"]
    units = hardware["maskable_units"]
    if (
        not isinstance(units, int)
        or isinstance(units, bool)
        or units <= 0
    ):
        raise AmdContractError("maskable_units must be a positive integer")
    if (
        not isinstance(bits, list)
        or len(bits) < 2
        or len(set(bits)) != len(bits)
        or any(
            isinstance(b, bool) or not isinstance(b, int) or not 0 <= b < units
            for b in bits
        )
    ):
        raise AmdContractError(
            "AMD matrix mask_bits must be at least two distinct in-range bits"
        )
    trials = matrix["trials_per_cell"]
    if isinstance(trials, bool) or not isinstance(trials, int) or trials < 2:
        raise AmdContractError("AMD matrix requires at least two trials")

    contract = manifest["reduced_contract"]
    # The manifest must name what it drops and where the ledger lives, so an
    # AMD cell can never be read as having met the CUDA contract.
    if (
        not isinstance(contract, Mapping)
        or contract.get("document") != "docs/amd-reduced-contract.md"
        or contract.get("applies_to") != "single-card single-operator gfx1201"
        or not isinstance(contract.get("dropped_guards"), list)
        or not contract["dropped_guards"]
    ):
        raise AmdContractError(
            "AMD manifest must declare its reduced contract and point at the "
            "ledger"
        )
    return dict(manifest)


def source_revision(
    repo_root: Path,
    *,
    expected_gitlinks: Mapping[str, str] | None = None,
    git: Path = Path("/usr/bin/git"),
) -> str:
    """Bind the run to the exact source tree, refusing an ambiguous one.

    Registered submodules must be declared with the commit they are pinned
    at. The AMD line does not use the vendored CUDA library, but it shares a
    repository with it, and an unregistered gitlink means the tree cannot be
    described exactly -- which is a refusal, not a detail to skip.
    """

    snapshot = capture_repository(
        repo_root.resolve(),
        expected_gitlinks=dict(expected_gitlinks or {}),
        allowed_untracked_roots=("experiments/runs", "related_work"),
        allow_untracked_regular_files=False,
        git=git,
    )
    if not snapshot.complete:
        raise AmdContractError(
            "could not bind the source tree: " + ";".join(snapshot.errors)
        )
    head = snapshot.head_oid
    if not head:
        raise AmdContractError("source tree has no HEAD commit")
    if not snapshot.clean:
        # Record the exact departure rather than refusing outright: a run from
        # a modified tree is still evidence, but it must never be mistaken for
        # one from the committed source.
        divergence = {
            "staged": sorted(str(p) for p in snapshot.staged_changes),
            "unstaged": sorted(str(p) for p in snapshot.unstaged_changes),
        }
        digest = hashlib.sha256(
            canonical_json(divergence).encode("utf-8")
        ).hexdigest()[:16]
        return f"{head}+dirty-{digest}"
    return str(head)


def run_cell(
    *,
    probe: Path,
    mode: str,
    mask_bit: int | None,
    trial: int,
    seed: int,
    manifest: Mapping[str, Any],
    repo_root: Path,
    run_root: Path,
    revision: str,
) -> dict[str, Any]:
    """Run one probe invocation and write a content-addressed cell."""

    if mode not in AMD_MODES:
        raise AmdContractError(f"unsupported AMD mode: {mode}")
    if (mode in MASKED_AMD_MODES) != (mask_bit is not None):
        raise AmdContractError("mask_bit is required exactly for masked modes")

    hardware = manifest["hardware"]
    matrix = manifest["matrix"]
    config = {
        "schema_version": AMD_CELL_SCHEMA_VERSION,
        "mode": mode,
        "mask_bit": mask_bit,
        "trial": trial,
        "seed": seed,
        "device_ordinal": hardware["device_ordinal"],
        "gpu_uuid": hardware["gpu_uuid"],
        "maskable_units": hardware["maskable_units"],
        "iterations": matrix["iterations"],
        "blocks": matrix["blocks"],
        "threads_per_block": matrix["threads_per_block"],
        "probe_sha256": _sha256_file(probe),
        "manifest": {
            "sha256": hashlib.sha256(
                canonical_json(dict(manifest)).encode("utf-8")
            ).hexdigest(),
            "content": dict(manifest),
        },
    }
    run_id = derive_run_id(config, seed, revision)
    directory = run_root / run_id
    directory.mkdir(parents=True, exist_ok=False)

    argv = [
        str(probe),
        "--mode",
        "cu_mask" if mode == "stream_cu_mask" else mode,
        "--device",
        str(hardware["device_ordinal"]),
        "--blocks",
        str(matrix["blocks"]),
        "--iterations",
        str(matrix["iterations"]),
        "--maskable-units",
        str(hardware["maskable_units"]),
    ]
    if mask_bit is not None:
        argv += ["--enabled-cu", str(mask_bit)]

    # An explicit allowlist, like the CUDA child environment: the global
    # mechanism is programmed here and must not leak into the other modes.
    environment = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C",
        "LC_ALL": "C",
        "TZ": "UTC",
        "HOME": os.environ.get("HOME", "/tmp"),
    }
    if mode == "global_cu_mask":
        environment["ROC_GLOBAL_CU_MASK"] = hex(1 << int(mask_bit))

    completed = subprocess.run(
        argv, env=environment, capture_output=True, text=True,
        cwd=str(repo_root), check=False, timeout=300,
    )
    native: dict[str, Any] | None = None
    for line in completed.stdout.splitlines():
        if line.startswith("{"):
            try:
                native = json.loads(line)
            except json.JSONDecodeError:
                native = None

    outcome = {
        "schema_version": AMD_CELL_SCHEMA_VERSION,
        "run_id": run_id,
        "source_revision": revision,
        "exit_code": completed.returncode,
        "stderr": completed.stderr,
        "native": native,
        "argv": argv[1:],
        "environment_overrides": environment,
    }
    (directory / "config.json").write_text(canonical_json(config) + "\n")
    (directory / "outcome.json").write_text(canonical_json(outcome) + "\n")
    (directory / "stdout.log").write_text(completed.stdout)
    (directory / "stderr.log").write_text(completed.stderr)
    return {"run_id": run_id, "directory": directory, "outcome": outcome}


def validate_amd_cell(
    *,
    config: Mapping[str, Any],
    outcome: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, list[str]]:
    """Check one AMD cell and extract its observation.

    The readback check has no CUDA counterpart and is an addition to the
    contract, not a substitute for anything dropped: a mask the runtime did
    not honour is reported here rather than inferred from the histogram.
    """

    errors: list[str] = []
    if config.get("schema_version") != AMD_CELL_SCHEMA_VERSION:
        errors.append("cell schema is not the AMD v1 schema")
    if outcome.get("exit_code") != 0:
        errors.append(f"probe exited {outcome.get('exit_code')!r}")
    native = outcome.get("native")
    if not isinstance(native, Mapping):
        return None, errors + ["probe emitted no parseable report"]
    if native.get("schema_version") != AMD_PROBE_SCHEMA_VERSION:
        errors.append("probe report schema is not the AMD probe schema")
    if native.get("status") != "ok":
        errors.append(f"probe status {native.get('status')!r}")
    if native.get("mode") != config.get("mode"):
        errors.append("probe mode does not match the requested mode")

    mode = config.get("mode")
    if mode in MASKED_AMD_MODES:
        if native.get("requested_enabled_cu") != config.get("mask_bit"):
            errors.append("probe did not mask the requested bit")
        if mode == "stream_cu_mask":
            if native.get("readback_supported") is not True:
                errors.append("stream cell lacks a mask readback")
            if native.get("readback_matches_request") is not True:
                errors.append(
                    "the runtime did not honour the requested CU mask"
                )
        if mode == "global_cu_mask":
            expected = hex(1 << int(config["mask_bit"]))
            if outcome.get("environment_overrides", {}).get(
                "ROC_GLOBAL_CU_MASK"
            ) != expected:
                errors.append("global cell did not set the declared mask")
    else:
        if "ROC_GLOBAL_CU_MASK" in (outcome.get("environment_overrides") or {}):
            errors.append("a global mask leaked into a non-global cell")

    histogram = native.get("observed_histogram")
    if not isinstance(histogram, Mapping) or not histogram:
        return None, errors + ["probe produced an empty histogram"]
    if sum(histogram.values()) != config.get("blocks"):
        errors.append("histogram counts do not sum to the requested blocks")
    if errors:
        return None, errors
    return {
        "mode": mode,
        "raw_unit_ids": sorted(int(k) for k in histogram),
        "blocks": config["blocks"],
        "observed_blocks": sum(histogram.values()),
        "mask_bit": config.get("mask_bit"),
        "trial": config["trial"],
        "gpu_uuid": config["gpu_uuid"],
    }, []


def dense_index_map(baseline_raw_ids: Sequence[int]) -> dict[int, int]:
    """Map opaque hardware identifiers onto the baseline's own index space.

    ``HW_ID1`` is an encoding, not an index, so downstream range checks need a
    dense space. The baseline defines it: any masked identifier absent from
    the baseline is a contradiction rather than a new unit.
    """

    ordered = sorted(set(int(value) for value in baseline_raw_ids))
    return {raw: index for index, raw in enumerate(ordered)}
