#!/usr/bin/env python3
"""Freeze the Gate C algorithm, formulas and action order.

plan.md ends this stage by freezing the algorithm: changes afterwards
require a decision-log entry. A freeze is only as good as what it
notices, so this records two independent locks.

The structural lock hashes each frozen function's AST with docstrings
removed. Hashing the source text would fire on reflowed comments, which
teaches people to re-freeze without reading; hashing the AST fires on a
changed branch order, a changed constant, a changed comparison -- the
things "algorithm, formula and action order" actually names.

The behavioural lock is the digest of a fixed trace under the frozen
policy. It catches what the structural lock cannot: a change in a
function the freeze does not list, in a default argument resolved
elsewhere, or in the measured tables the decisions read. Either lock
failing means the freeze is broken; the two failing differently says
where to look.

Run with --write to (re)generate the manifest. Without it, verifies.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import inspect
import json
import pathlib
import sys
import textwrap

sys.dont_write_bytecode = True

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from burstserve import policies, trace_sim                      # noqa: E402
from burstserve.deadline_feasibility import edf_whole_die       # noqa: E402
from burstserve.trace_sim import (                              # noqa: E402
    Predictor,
    Request,
    Trace,
    simulate,
)

MANIFEST = REPO / "experiments" / "manifests" / "gate_c_algorithm_freeze.json"

# The scheduler and everything its decisions are computed from. Listing
# them explicitly rather than sweeping the module is deliberate: a new
# helper must be added here consciously, and until it is, the behavioural
# lock is what catches it.
FROZEN_FUNCTIONS = [
    ("policies.slo_aware_partitioning", policies.slo_aware_partitioning),
    ("policies.deadline_aware", policies.deadline_aware),
    ("policies.step_matched_pairing", policies.step_matched_pairing),
    ("policies.static_even", policies.static_even),
    ("policies.exclusive_fcfs", policies.exclusive_fcfs),
    ("policies.measured_pairs_only", policies.measured_pairs_only),
    ("policies.oracle_shortest_remaining",
     policies.oracle_shortest_remaining),
    ("trace_sim.simulate", trace_sim.simulate),
    ("trace_sim.QuotaCostModel.step_seconds",
     trace_sim.QuotaCostModel.step_seconds),
    ("trace_sim.SimulationResult.jain_index",
     trace_sim.SimulationResult.jain_index),
    ("trace_sim.SimulationResult.accounting_error",
     trace_sim.SimulationResult.accounting_error),
    ("trace_sim.SimulationResult.peak_service_lag_quanta",
     trace_sim.SimulationResult.peak_service_lag_quanta),
    ("trace_sim.SimulationResult.canonical_bytes",
     trace_sim.SimulationResult.canonical_bytes),
    ("deadline_feasibility.edf_whole_die", edf_whole_die),
]

FROZEN_TABLES = [
    "MEASURED_MODELS",
    "MEASURED_QUOTA_SECONDS",
    "MEASURED_EXTERNALITY",
]


def structural_hash(func) -> str:
    """AST of the function with its docstring stripped."""
    # dedent, not cleandoc: cleandoc unindents the first line fully and
    # the rest by their common prefix, which leaves a method's def at
    # column 0 with its body there too.
    source = textwrap.dedent(inspect.getsource(func))
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef, ast.Module)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:] or [ast.Pass()]
    return hashlib.sha256(
        ast.dump(tree, annotate_fields=True).encode()
    ).hexdigest()


def table_hash(name: str) -> str:
    """Canonical hash of a measured table.

    Sorted by repr so dict insertion order -- which is a property of how
    the table was typed, not of its contents -- cannot change the hash.
    """
    table = getattr(trace_sim, name)
    if isinstance(table, dict):
        payload = sorted(
            (repr(k), repr(v)) for k, v in table.items()
        )
    else:
        payload = sorted(repr(v) for v in table)
    return hashlib.sha256(repr(payload).encode()).hexdigest()


def behavioural_digests() -> dict[str, str]:
    """Frozen policy on fixed traces -- what the freeze is really about.

    Four traces. Three exercise the branches of the action order --
    matched tenants take the pairing branch, mismatched take the deficit
    rotation, the deadline trace takes the override. All three run an
    exact predictor, and that is precisely the regime in which a
    starvation defect fixed on 2026-08-06 was invisible: with exact costs
    the defective and correct algorithms agree exactly. The fourth runs
    matched tenants under a +/-5% predictor, which is what separates them.

    Traces chosen to demonstrate intended behaviour miss defects that
    appear off the intended path, so at least one has to be chosen for
    where things go wrong.
    """
    matched = Trace([
        Request(request_id=i, tenant=f"t{i % 2}", model="sdxl",
                arrival_s=0.0, steps=40)
        for i in range(6)
    ])
    mismatched = Trace([
        Request(request_id=0, tenant="fast", model="sdxl",
                arrival_s=0.0, steps=40),
        Request(request_id=1, tenant="slow", model="cogvideox-2b",
                arrival_s=0.0, steps=40),
    ])
    plain = Trace([
        Request(request_id=i, tenant=f"t{i % 2}", model="sdxl",
                arrival_s=0.0, steps=20)
        for i in range(6)
    ])
    achieved = edf_whole_die(plain).completion_s
    deadline = Trace([
        Request(request_id=r.request_id, tenant=r.tenant, model=r.model,
                arrival_s=r.arrival_s, steps=r.steps,
                deadline_s=achieved[r.request_id] * 1.35)
        for r in plain.requests
    ])
    policy = policies.slo_aware_partitioning
    return {
        "matched_tenants": simulate(
            matched, policy, horizon_s=600.0).digest(),
        "mismatched_tenants": simulate(
            mismatched, policy, horizon_s=1200.0).digest(),
        "feasible_deadline": simulate(
            deadline, policy, horizon_s=400.0).digest(),
        "matched_tenants_5pct_predictor_error": simulate(
            matched, policy, horizon_s=600.0,
            predictor=Predictor(relative_error=0.05, seed=11)).digest(),
    }


def build() -> dict:
    return {
        "schema_version": "burstserve.gate-c-algorithm-freeze/v1",
        "manifest_id": "gate-c-algorithm-freeze-20260806",
        "frozen_at_stage": "plan.md week 5-6, Gate C",
        "decision_log": "docs/gate-c-decision-log.md",
        "policy_under_freeze": "policies.slo_aware_partitioning",
        "action_order": [
            "deadline override: exclusive to a request that misses shared "
            "and makes it whole-die",
            "matched pairing: even split when predicted step times are "
            "within 1.6x",
            "deficit rotation: whole die to the tenant furthest behind on "
            "quota-seconds",
        ],
        "structural": {
            name: structural_hash(func) for name, func in FROZEN_FUNCTIONS
        },
        "tables": {name: table_hash(name) for name in FROZEN_TABLES},
        "behavioural": behavioural_digests(),
    }


def verify() -> int:
    if not MANIFEST.exists():
        print(f"FAIL: no freeze manifest at {MANIFEST}")
        return 1
    frozen = json.loads(MANIFEST.read_text())
    current = build()
    broken = []
    for section in ("structural", "tables", "behavioural"):
        was, now = frozen.get(section, {}), current[section]
        for key in sorted(set(was) | set(now)):
            if was.get(key) != now.get(key):
                broken.append(
                    f"{section}.{key}: {was.get(key, '<absent>')[:12]} -> "
                    f"{now.get(key, '<absent>')[:12]}"
                )
    if frozen.get("action_order") != current["action_order"]:
        broken.append("action_order changed")
    if broken:
        print("FAIL: the Gate C freeze is broken")
        for line in broken:
            print(f"  {line}")
        print(f"\nRecord the change in {current['decision_log']}, then "
              f"re-run with --write.")
        return 1
    print(f"PASS: {len(current['structural'])} functions, "
          f"{len(current['tables'])} tables and "
          f"{len(current['behavioural'])} behavioural digests match the "
          f"freeze")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true",
                        help="regenerate the manifest (requires a "
                             "decision-log entry for any change)")
    args = parser.parse_args()
    if args.write:
        MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        MANIFEST.write_text(json.dumps(build(), indent=2) + "\n")
        print(f"wrote {MANIFEST}")
        return 0
    return verify()


if __name__ == "__main__":
    raise SystemExit(main())
