#!/usr/bin/env python3
"""Verify Gate C, one criterion at a time, and say what each rests on.

plan.md lists seven acceptance conditions for the simulator stage. This
reports PASS, FAIL or NOT_MEASURED per criterion and writes the evidence
to JSON. NOT_MEASURED is a distinct outcome from PASS: a criterion no
run covered has not been satisfied, and collapsing the two is how a gate
gets declared closed on evidence that was never produced.

Scope, stated once so no reader has to infer it: this is a simulation.
Its cost tables are real -- measured on an AMD R9700 (gfx1201) under
Gate B-AMD -- but no GPU runs here. Gate C is a gate on the scheduler's
logic given measured costs, and nothing it reports is a hardware claim.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys

sys.dont_write_bytecode = True

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from burstserve.deadline_feasibility import (          # noqa: E402
    avoidable_misses,
    edf_whole_die,
    feasible_deadline_trace,
)
from burstserve.policies import probing_partitioning  # noqa: E402
from burstserve.trace_sim import (                      # noqa: E402
    Predictor,
    Request,
    Trace,
    simulate,
)

PASS, FAIL, NOT_MEASURED = "PASS", "FAIL", "NOT_MEASURED"


def matched() -> Trace:
    return Trace([
        Request(request_id=i, tenant=f"t{i % 2}", model="sdxl",
                arrival_s=0.0, steps=40)
        for i in range(6)
    ])


def mismatched() -> Trace:
    return Trace([
        Request(request_id=0, tenant="fast", model="sdxl",
                arrival_s=0.0, steps=40),
        Request(request_id=1, tenant="slow", model="cogvideox-2b",
                arrival_s=0.0, steps=40),
    ])


def c1_byte_identical_replay() -> dict:
    script = (
        "import sys; sys.dont_write_bytecode = True\n"
        "from burstserve.trace_sim import Trace, simulate\n"
        "from burstserve.policies import probing_partitioning\n"
        "t = Trace.poisson(seed=7, tenants=[('t0','sdxl'),"
        "('t1','cogvideox-2b')], rate_per_s=1.2, horizon_s=30.0,"
        " steps=30, deadline_slack=2.0)\n"
        "print(simulate(t, probing_partitioning,"
        " horizon_s=400.0).digest())\n"
    )
    digests = []
    for hash_seed in ("0", "1", "12345"):
        proc = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True,
            cwd=REPO,
            env={"PYTHONPATH": "src", "PYTHONHASHSEED": hash_seed,
                 "PATH": "/usr/bin:/bin"},
        )
        if proc.returncode != 0:
            return {"status": FAIL, "detail": proc.stderr[-400:]}
        digests.append(proc.stdout.strip())
    other_seed = simulate(
        Trace.poisson(seed=8, tenants=[("t0", "sdxl"),
                                       ("t1", "cogvideox-2b")],
                      rate_per_s=1.2, horizon_s=30.0, steps=30,
                      deadline_slack=2.0),
        probing_partitioning, horizon_s=400.0,
    ).digest()
    unique = set(digests)
    return {
        "status": PASS if len(unique) == 1 and digests[0] != other_seed
                  else FAIL,
        "digest": digests[0],
        "processes": len(digests),
        "hash_seeds": ["0", "1", "12345"],
        "differs_under_a_different_trace_seed": digests[0] != other_seed,
        "note": "separate processes, so an unsorted set would show up",
    }


def c2_canonical_accounting() -> dict:
    errors = {}
    for label, trace, horizon in (("matched", matched(), 600.0),
                                  ("mismatched", mismatched(), 1200.0),
                                  ("deadline", feasible_deadline_trace(),
                                   400.0)):
        errors[label] = simulate(
            trace, probing_partitioning, horizon_s=horizon
        ).accounting_error()
    worst = max(errors.values())
    return {
        "status": PASS if worst < 0.01 else FAIL,
        "threshold": 0.01,
        "worst_relative_error": worst,
        "by_trace": errors,
        "definition": "charged quota-seconds vs unit-seconds granted, "
                      "including units held through a round no step "
                      "completed in",
    }


def c3_backlogged_jain() -> dict:
    result = simulate(matched(), probing_partitioning, horizon_s=600.0)
    return {
        "status": PASS if result.jain_index() >= 0.98 else FAIL,
        "threshold": 0.98,
        "jain_index": result.jain_index(),
        "trace": "two tenants, equal demand, all queued at t=0",
        "note": "unserved tenants are seeded at zero, so starvation "
                "cannot score 1.0 by absence",
    }


def c4_service_lag() -> dict:
    rows = {}
    for label, trace, horizon in (("matched", matched(), 600.0),
                                  ("mismatched", mismatched(), 1200.0)):
        result = simulate(trace, probing_partitioning, horizon_s=horizon)
        rows[label] = {
            "peak_lag_quanta": result.peak_service_lag_quanta(),
            "deadline_override_rounds": result.deadline_override_rounds,
            "exclusive_rounds": result.exclusive_rounds,
        }
    clean = all(r["deadline_override_rounds"] == 0 for r in rows.values())
    within = all(r["peak_lag_quanta"] <= 2.0 for r in rows.values())
    return {
        "status": PASS if clean and within else FAIL,
        "threshold_quanta": 2.0,
        "by_trace": rows,
        "definition": "peak over rounds of the worst deviation from an "
                      "equal share among backlogged tenants, in full-die "
                      "quanta; sampled per round because the end-of-run "
                      "gap is zero for a policy that was never fair",
        "exemption": "overrides are detected by the simulator from the "
                     "predictions the policy saw, not self-reported",
    }


def c5_no_avoidable_miss() -> dict:
    trace = feasible_deadline_trace()
    report = edf_whole_die(trace)
    if not report.feasible:
        return {"status": NOT_MEASURED,
                "detail": "constructed trace is not feasible; the "
                          "criterion says nothing about it",
                "edf_missed": list(report.missed)}
    result = simulate(trace, probing_partitioning, horizon_s=400.0)
    missed = avoidable_misses(result, trace)
    return {
        "status": PASS if not missed else FAIL,
        "avoidable_misses": list(missed),
        "requests": len(trace.requests),
        "feasibility_witness": report.witness,
        "note": "deadlines derived from what EDF achieved, not from "
                "nominal per-request cost, which ignores the queue",
    }


def c6_safe_degradation() -> dict:
    from burstserve.policies import slo_aware_partitioning
    blind = simulate(matched(), slo_aware_partitioning,
                     horizon_s=600.0).utilisation()
    exact = simulate(matched(), probing_partitioning, horizon_s=600.0)
    rows = {}
    ok = True
    for error in (0.05, 0.10, 0.20):
        noisy_matched = simulate(
            matched(), probing_partitioning, horizon_s=600.0,
            predictor=Predictor(relative_error=error, seed=11))
        noisy_mismatched = simulate(
            mismatched(), probing_partitioning, horizon_s=1200.0,
            predictor=Predictor(relative_error=error, seed=11))
        row = {
            "accounting_error": noisy_matched.accounting_error(),
            "peak_lag_quanta": max(
                noisy_matched.peak_service_lag_quanta(),
                noisy_mismatched.peak_service_lag_quanta()),
            "utilisation": noisy_matched.utilisation(),
            "utilisation_ratio_to_exact": (
                noisy_matched.utilisation() / exact.utilisation()),
        }
        row["utilisation_vs_blind_pairing"] = (
            noisy_matched.utilisation() / blind
        )
        ok &= (row["accounting_error"] < 0.01
               and row["peak_lag_quanta"] <= 2.0
               and row["utilisation_ratio_to_exact"] >= 0.95
               # And never below the policy that consults no predictor,
               # which is the floor a probe must not fall through.
               and row["utilisation_vs_blind_pairing"] >= 1.0)
        rows[f"+/-{int(error * 100)}%"] = row
    return {
        "status": PASS if ok else FAIL,
        "exact_utilisation": exact.utilisation(),
        "blind_pairing_utilisation": blind,
        "by_error_level": rows,
        "definition": (
            "safe = accounting still exact (it is measured, not "
            "predicted), lag still bounded, utilisation within 5% of the "
            "exact-predictor run, and never below the policy that "
            "consults no predictor. The probe degrades to a no-op past "
            "roughly 10% error rather than acting on a prediction too "
            "noisy to support the action, so the strict form holds."
        ),
    }


def c7_algorithm_freeze() -> dict:
    proc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "freeze_gate_c_algorithm.py")],
        capture_output=True, text=True, cwd=REPO,
    )
    log = REPO / "docs" / "gate-c-decision-log.md"
    return {
        "status": PASS if proc.returncode == 0 and log.exists() else FAIL,
        "verifier": "scripts/freeze_gate_c_algorithm.py",
        "manifest": "experiments/manifests/gate_c_algorithm_freeze.json",
        "decision_log": str(log.relative_to(REPO)),
        "decision_log_present": log.exists(),
        "output": proc.stdout.strip(),
    }


CRITERIA = [
    ("c1_byte_identical_replay", "同 seed 仿真结果逐字节可复现",
     c1_byte_identical_replay),
    ("c2_canonical_accounting", "canonical service 对 SM quota 的记账差异小于 1%",
     c2_canonical_accounting),
    ("c3_backlogged_jain", "backlogged workload 的 Jain index 不低于 0.98",
     c3_backlogged_jain),
    ("c4_service_lag", "无 deadline override 时 service lag 不超过两个最大 quantum",
     c4_service_lag),
    ("c5_no_avoidable_miss", "构造的可行 deadline trace 中不存在可避免 miss",
     c5_no_avoidable_miss),
    ("c6_safe_degradation", "predictor error {±5%,±10%,±20%} 下能够安全降级",
     c6_safe_degradation),
    ("c7_algorithm_freeze", "算法、公式和 action 顺序冻结，修改须写 decision log",
     c7_algorithm_freeze),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=pathlib.Path,
                        default=REPO / "experiments" / "aggregates"
                        / "gate_c_verification.json")
    args = parser.parse_args()

    results = {}
    for key, statement, fn in CRITERIA:
        try:
            row = fn()
        except Exception as exc:                      # noqa: BLE001
            row = {"status": FAIL, "detail": f"{type(exc).__name__}: {exc}"}
        row["criterion"] = statement
        results[key] = row

    statuses = [row["status"] for row in results.values()]
    overall = (PASS if all(s == PASS for s in statuses)
               else FAIL if FAIL in statuses else NOT_MEASURED)
    payload = {
        "schema_version": "burstserve.gate-c-verification/v1",
        "gate": "Gate C (plan.md week 5-6, simulator stage)",
        "overall": overall,
        "scope": (
            "Simulation. Cost tables are measured on an AMD R9700 "
            "(gfx1201) under Gate B-AMD; no GPU runs in this verifier. "
            "Nothing here is a hardware claim."
        ),
        "policy_under_test": "policies.probing_partitioning",
        "criteria": results,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, ensure_ascii=False)
                        + "\n")

    width = max(len(k) for k in results)
    for key, row in results.items():
        mark = {PASS: "PASS", FAIL: "FAIL",
                NOT_MEASURED: "NOT_MEASURED"}[row["status"]]
        print(f"{key:<{width}}  {mark:<12}  {row['criterion']}")
    print(f"\nGate C: {overall}")
    print(f"evidence -> {args.out.relative_to(REPO)}")
    return 0 if overall == PASS else 1


if __name__ == "__main__":
    raise SystemExit(main())
