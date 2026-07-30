from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest


REPOSITORY = Path(__file__).resolve().parents[1]
SIM_ROOT = REPOSITORY / "src" / "burstserve" / "sim"
FORBIDDEN_ROOTS = {
    "asle",
    "diffusers",
    "libsmctrl",
    "numpy",
    "torch",
    "vendor",
}
ALLOWED_ABSOLUTE_ROOTS = {
    "__future__",
    "dataclasses",
    "fractions",
    "functools",
    "hashlib",
    "io",
    "json",
    "math",
    "types",
    "typing",
}
ALLOWED_RELATIVE_MODULES = {
    "accounting",
    "io_model",
    "model",
    "protocols",
    "trace",
}


class PureSimulatorImportTest(unittest.TestCase):
    def test_source_import_graph_has_no_runtime_or_numeric_dependencies(self) -> None:
        imported_roots: set[str] = set()
        relative_modules: set[str] = set()
        forbidden_float_constructs: list[tuple[Path, int]] = []
        for source_path in sorted(SIM_ROOT.glob("*.py")):
            tree = ast.parse(source_path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, float):
                    forbidden_float_constructs.append(
                        (source_path, node.lineno)
                    )
                if isinstance(node, ast.Call):
                    if isinstance(node.func, ast.Name) and node.func.id == "float":
                        forbidden_float_constructs.append(
                            (source_path, node.lineno)
                        )
                    if (
                        isinstance(node.func, ast.Attribute)
                        and node.func.attr in {"exp", "exp2"}
                    ):
                        forbidden_float_constructs.append(
                            (source_path, node.lineno)
                        )
                if isinstance(node, ast.Import):
                    imported_roots.update(
                        alias.name.split(".", 1)[0] for alias in node.names
                    )
                elif isinstance(node, ast.ImportFrom):
                    if node.level == 0 and node.module:
                        imported_roots.add(node.module.split(".", 1)[0])
                    elif node.level == 1 and node.module:
                        relative_modules.add(node.module.split(".", 1)[0])
                    else:
                        self.fail(
                            f"{source_path} has an escaping/ambiguous relative import"
                        )
        self.assertTrue(imported_roots.isdisjoint(FORBIDDEN_ROOTS))
        self.assertTrue(imported_roots <= ALLOWED_ABSOLUTE_ROOTS)
        self.assertTrue(relative_modules <= ALLOWED_RELATIVE_MODULES)
        self.assertEqual(forbidden_float_constructs, [])

    def test_importing_public_simulator_does_not_load_gpu_frameworks(self) -> None:
        environment = dict(os.environ)
        python_path = str(REPOSITORY / "src")
        if environment.get("PYTHONPATH"):
            python_path = python_path + os.pathsep + environment["PYTHONPATH"]
        environment["PYTHONPATH"] = python_path
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import json, sys; import burstserve.sim; "
                    "print(json.dumps(sorted(name for name in sys.modules "
                    "if name.split('.', 1)[0] in "
                    "{'torch', 'diffusers', 'numpy', 'libsmctrl', 'asle'} "
                    "or name in {'burstserve.smctrl_runner', "
                    "'burstserve.nvml_events'})))"
                ),
            ],
            cwd=REPOSITORY,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(json.loads(completed.stdout), [])

    def test_ids_and_canonical_state_are_cross_process_hash_seed_stable(self) -> None:
        script = """
import json
from burstserve.sim import (
    Action, ExactRatio, GlobalFairState, RequestAllocation, ResidencyState,
    SchedulerState, TenantLedger, TenantPolicy, TenantState, TraceEvent,
    WorkloadSignature, canonical_json,
)
signature = WorkloadSignature(
    model="toy-dit", revision="r1", height=512, width=512, frame_count=1,
    batch_size=1, dtype="bf16", cfg_mode="batched", scheduler="euler",
    total_steps=4, attention_backend="sdpa", streaming_mode="resident",
    profile_id="profile-v1",
)
residency = ResidencyState(device_immutable_ids=tuple({"weights-b", "weights-a"}))
action = Action(
    allocations=(
        RequestAllocation("request-b", 1, 1, 4, 4),
        RequestAllocation("request-a", 2, 3, 4, 8),
    ),
    target_residency=residency,
)
state = SchedulerState(
    global_fair=GlobalFairState(ExactRatio(7, 3)),
    tenants=(
        TenantState(
            TenantPolicy("tenant-b"),
            TenantLedger("tenant-b", fair_service_coordinate=ExactRatio(7, 3)),
        ),
        TenantState(
            TenantPolicy("tenant-a", 2, 1),
            TenantLedger("tenant-a", fair_service_coordinate=ExactRatio(14, 3)),
        ),
    ),
)
event = TraceEvent(
    2, 100, "arrival", "request-a", tuple({("z", 2), ("a", "x")})
)
print(canonical_json({
    "action": action.stable_id,
    "event": event.stable_id,
    "signature": signature.stable_id,
    "state": state.stable_id,
}))
"""
        outputs: list[str] = []
        for seed in ("0", "1", "42"):
            environment = dict(os.environ)
            environment["PYTHONHASHSEED"] = seed
            python_path = str(REPOSITORY / "src")
            if environment.get("PYTHONPATH"):
                python_path = python_path + os.pathsep + environment["PYTHONPATH"]
            environment["PYTHONPATH"] = python_path
            completed = subprocess.run(
                [sys.executable, "-c", script],
                cwd=REPOSITORY,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
            outputs.append(completed.stdout.strip())
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[1], outputs[2])
        self.assertEqual(
            json.loads(outputs[0]),
            {
                # Golden IDs make a schema change explicit rather than silently
                # redefining a supposedly stable experiment key.
                "action": (
                    "act2-04c0b2d652ac73ac9332b744e12ae51f4d2e267e7923da"
                    "541b21dbc89dfddad9"
                ),
                "event": (
                    "evt2-1c6fc2a09142661a1645bb99fa071d2e77eae730f1aef"
                    "daff2abcc51acf49d1c"
                ),
                "signature": (
                    "wls2-54301ee6e8d88f4a2c4339e2e8cde3b1b548be1b470d7"
                    "c903fca2bd5a8f95298"
                ),
                "state": (
                    "sch2-347cabe8e128eb920716217dfe6a49110904df11ba861"
                    "b9132c13e925149adbc"
                ),
            },
        )


if __name__ == "__main__":
    unittest.main()
