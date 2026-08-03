"""Sweep the Gate B-AMD solo profile matrix for one model.

Each cell is a separate process with a fixed ``ROC_GLOBAL_CU_MASK``, which
is the only masking route verified to reach PyTorch (2026-08-02). The sweep
covers the declared quota list at two problem sizes, because Gate B-AMD
requires every cell to record whether it is in the saturating regime and
that is not decidable from a single problem size:

    a cell at quota q is saturating iff doubling the work at the same quota
    does not raise throughput by more than ``--saturation-epsilon``.

If the larger problem is materially faster in items/second, the smaller one
was not filling the units it was given, and its quota->latency point must not
enter the canonical table -- the 2026-08-02 synthetic sweep showed such a
point going *down* with more quota (16 units peaked, 32 fell to 69%), which
would contradict the scheduler's monotonicity assumption with no way to tell
a model error from a measurement error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# Before the first burstserve import, not on the command line: importing this
# package writes .pyc files into the tree the run is about to bind, and the
# source policy then refuses the run for untracked paths it created itself.
# Relying on the caller to export PYTHONDONTWRITEBYTECODE has already failed
# three times.
sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from burstserve.amd_cu_runner import source_revision  # noqa: E402
from burstserve.provenance import canonical_json  # noqa: E402

GIT = Path(os.environ.get("BURSTSERVE_GIT", "/usr/bin/git"))
TABLE_SCHEMA_VERSION = "burstserve.amd-gate-b-table/v1"

# The die has 32 maskable units (established by the Gate A sweep, which also
# showed bits 32..63 being silently ignored). All 32 behave identically, so a
# contiguous low mask is a reproducible choice rather than an arbitrary one.
MASKABLE_UNITS = 32
DEFAULT_QUOTAS = [4, 8, 12, 16, 20, 24, 28, 32]


def mask_for(units: int) -> str:
    if not 1 <= units <= MASKABLE_UNITS:
        raise ValueError(f"quota {units} outside 1..{MASKABLE_UNITS}")
    return hex((1 << units) - 1)


def run_cell(cell_script: Path, *, units: int, batch: int, args, samples: int,
             warmup: int) -> dict:
    env = dict(os.environ)
    env["ROC_GLOBAL_CU_MASK"] = mask_for(units)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    argv = [
        sys.executable, str(cell_script),
        "--model", args.model,
        "--batch", str(batch),
        "--steps", str(args.steps),
        "--warmup", str(warmup),
        "--samples", str(samples),
        "--target-cv", str(args.target_cv),
        "--max-samples", str(args.max_samples),
        "--height", str(args.height),
        "--width", str(args.width),
        "--frames", str(args.frames),
        "--seed", str(args.seed),
    ]
    started = time.time()
    proc = subprocess.run(argv, env=env, capture_output=True, text=True)
    body = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
    if proc.returncode != 0 or not body:
        return {
            "status": "cell_failed",
            "returncode": proc.returncode,
            "requested_units": units,
            "batch": batch,
            "stderr_tail": proc.stderr[-2000:],
            "stdout_tail": proc.stdout[-1000:],
            "wall_s": time.time() - started,
        }
    record = json.loads(body[-1])
    record["requested_units"] = units
    record["wall_s"] = time.time() - started
    return record


def classify(rows: list[dict], epsilon: float) -> None:
    """Attach the saturation and monotonicity flags to every accepted cell.

    Both are recorded, and they are different claims: saturation says the
    units are the bottleneck at this cell's own problem size, monotonicity
    says throughput did not fall when quota rose. The scheduler assumes the
    second; the first is what explains it when it fails.
    """

    ok = [r for r in rows if r.get("status") == "ok"]
    by_batch: dict[int, dict[int, dict]] = {}
    for row in ok:
        by_batch.setdefault(row["batch"], {})[row["requested_units"]] = row
    batches = sorted(by_batch)

    for index, batch in enumerate(batches):
        larger = batches[index + 1] if index + 1 < len(batches) else None
        for units, row in by_batch[batch].items():
            peer = by_batch.get(larger, {}).get(units) if larger else None
            if peer is None:
                row["saturating_regime"] = None
                row["saturation_basis"] = "no larger problem measured"
            else:
                gain = peer["items_per_s"] / row["items_per_s"] - 1.0
                row["saturating_regime"] = gain <= epsilon
                row["saturation_basis"] = {
                    "compared_batch": larger,
                    "throughput_gain": gain,
                    "epsilon": epsilon,
                }
            lower = [
                other for q, other in by_batch[batch].items() if q < units
            ]
            row["quota_monotone"] = all(
                row["items_per_s"] >= other["items_per_s"] for other in lower
            ) if lower else True
            # What the canonical p50 table is allowed to contain.
            row["canonical_eligible"] = bool(
                row["saturating_regime"]
                and row["quota_monotone"]
                and row["meets_cv_threshold"]
                and row.get("cu_mask_stable")
                and row["cu_mask_attestation"]["readback_matches_request"]
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--steps", type=int, default=20)
    parser.add_argument("--height", type=int, default=1024)
    parser.add_argument("--width", type=int, default=1024)
    parser.add_argument("--frames", type=int, default=49)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--quotas", default=",".join(str(q) for q in DEFAULT_QUOTAS))
    parser.add_argument("--canonical-batch", type=int, default=1)
    parser.add_argument("--probe-batch", type=int, default=2)
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--warmup", type=int, default=5)
    # The larger problem only has to decide a boolean about the regime, not
    # produce a canonical latency, so it is sampled to the precision that
    # decision needs and is labelled a probe wherever it is reported.
    parser.add_argument("--probe-samples", type=int, default=12)
    parser.add_argument("--probe-warmup", type=int, default=3)
    parser.add_argument("--target-cv", type=float, default=0.05)
    parser.add_argument("--max-samples", type=int, default=300)
    parser.add_argument("--saturation-epsilon", type=float, default=0.05)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    quotas = [int(q) for q in args.quotas.split(",") if q.strip()]
    cell_script = REPO / "scripts/amd_profile_cell.py"

    libsmctrl_pin = json.loads(
        (REPO / "vendor/LIBSMCTRL_SOURCE.json").read_text()
    )["source_commit"]
    attested = {}
    for relative in ("build/amd_cu_probe/cu_probe",
                     "build/amd_cu_probe/gate_a_probe"):
        path = REPO / relative
        if path.is_file():
            attested[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    revision = source_revision(
        REPO,
        expected_gitlinks={"vendor/libsmctrl": libsmctrl_pin},
        attested_build_files=attested,
        git=GIT,
    )
    print(f"source revision: {revision}", flush=True)

    rows: list[dict] = []
    plan = [(args.canonical_batch, args.samples, args.warmup, "canonical")]
    if args.probe_batch and args.probe_batch != args.canonical_batch:
        plan.append((args.probe_batch, args.probe_samples, args.probe_warmup,
                     "saturation_probe"))

    for batch, samples, warmup, role in plan:
        for units in quotas:
            row = run_cell(cell_script, units=units, batch=batch, args=args,
                           samples=samples, warmup=warmup)
            row["role"] = role
            rows.append(row)
            if row.get("status") == "ok":
                print(
                    f"  {role:17s} batch={batch} units={units:2d} "
                    f"p50={row['p50_s']:8.3f}s cv={row['cv']*100:5.2f}% "
                    f"n={row['samples']:3d} esc={row['escalations']} "
                    f"thr={row['items_per_s']:.4f}/s "
                    f"mem={row['peak_memory_bytes']/1e9:5.2f}GB "
                    f"({row['wall_s']:.0f}s)",
                    flush=True,
                )
            else:
                print(f"  {role:17s} batch={batch} units={units:2d} FAILED "
                      f"rc={row.get('returncode')}", flush=True)

    classify(rows, args.saturation_epsilon)

    # Bind the tree again now that the sweep is over. Each cell is a fresh
    # subprocess that re-reads the profile script from disk, so a tree edited
    # while the sweep was running would have been executed under a revision
    # the header no longer describes -- and the opening bind cannot see that.
    revision_after = source_revision(
        REPO,
        expected_gitlinks={"vendor/libsmctrl": libsmctrl_pin},
        attested_build_files=attested,
        git=GIT,
    )
    stable = revision_after == revision
    if not stable:
        print(f"SOURCE MOVED DURING THE SWEEP: {revision} -> {revision_after}",
              flush=True)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    header = {
        "schema_version": TABLE_SCHEMA_VERSION,
        "record": "header",
        "model": args.model,
        "source_revision": revision,
        "source_revision_after": revision_after,
        "source_revision_stable": stable,
        "maskable_units": MASKABLE_UNITS,
        "quotas": quotas,
        "mask_policy": "contiguous low bits ((1<<q)-1)",
        "saturation_epsilon": args.saturation_epsilon,
        "steps": args.steps,
        "height": args.height,
        "width": args.width,
        "canonical_batch": args.canonical_batch,
        "probe_batch": args.probe_batch,
        "reduced_contract": "docs/amd-reduced-contract.md",
    }
    with out.open("w", encoding="utf-8") as handle:
        handle.write(canonical_json(header) + "\n")
        for row in rows:
            handle.write(canonical_json(row) + "\n")

    ok = [r for r in rows if r.get("status") == "ok"]
    canonical = [r for r in ok if r.get("canonical_eligible")]
    print(f"\ncells ok: {len(ok)}/{len(rows)}   canonical-eligible: {len(canonical)}")
    for row in sorted(ok, key=lambda r: (r["batch"], r["requested_units"])):
        flag = "CANON" if row.get("canonical_eligible") else "-----"
        print(f"  {flag} batch={row['batch']} units={row['requested_units']:2d} "
              f"p50={row['p50_s']:8.3f} p99={row['p99_s']:8.3f} "
              f"cv={row['cv']*100:5.2f}% sat={row['saturating_regime']} "
              f"mono={row['quota_monotone']}")
    print(f"table: {out}")
    if not stable:
        # The rows are kept -- they still have diagnostic value -- but the
        # sweep cannot be cited as evidence for a revision it did not run on.
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
