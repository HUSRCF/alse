"""Capture and verify a relocatable BurstServe runtime lock.

The general environment snapshot records everything observed at run time,
including mutable facts such as timestamps and environment paths.  This module
extracts the reproducibility-critical subset into a deterministic lock:

* exact conda package URLs;
* exact pip-managed distributions;
* Python and CUDA framework versions;
* the GPU class used for the Phase-0 evidence; and
* the immutable ASLE source identity.

The lock deliberately excludes the environment prefix, repository Git state,
and capture time so that a cloned environment can be verified at a new path.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from packaging.markers import default_environment
from packaging.requirements import InvalidRequirement, Requirement

from .environment import capture_environment
from .provenance import read_json, write_json_atomic, write_text_atomic


RUNTIME_LOCK_SCHEMA_VERSION = "burstserve.runtime-lock/v1"
RUNTIME_LOCK_REPORT_SCHEMA_VERSION = "burstserve.runtime-lock-report/v1"

_NAME_SEPARATOR = re.compile(r"[-_.]+")
_FREEZE_NAME = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:==| @ )")
_ROOT_DISTRIBUTIONS = (
    "accelerate",
    "diffusers",
    "numpy",
    "pillow",
    "safetensors",
    "sentencepiece",
    "torch",
    "transformers",
)


def _canonical_distribution_name(name: str) -> str:
    return _NAME_SEPARATOR.sub("-", name).lower()


def _required_stdout(probe: Mapping[str, Any], label: str) -> str:
    if not probe.get("ok"):
        raise ValueError(f"{label} probe failed: {probe.get('error') or probe}")
    stdout = probe.get("stdout")
    if not isinstance(stdout, str):
        raise ValueError(f"{label} probe did not return text")
    return stdout


def _nonempty_lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


def _lines_sha256(lines: Sequence[str]) -> str:
    payload = "".join(f"{line}\n" for line in lines).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _pip_distribution_name(line: str) -> str | None:
    match = _FREEZE_NAME.match(line)
    return (
        _canonical_distribution_name(match.group(1))
        if match is not None
        else None
    )


def _conda_packages(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    package_lock = snapshot.get("package_lock")
    if not isinstance(package_lock, Mapping):
        raise ValueError("environment snapshot is missing package_lock")
    probe = package_lock.get("conda_json")
    if not isinstance(probe, Mapping):
        raise ValueError("environment snapshot is missing conda_json")
    try:
        value = json.loads(_required_stdout(probe, "conda list --json"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"conda list --json returned invalid JSON: {exc}") from exc
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError("conda list --json did not return a package list")
    return value


def _conda_explicit_urls(snapshot: Mapping[str, Any]) -> list[str]:
    package_lock = snapshot.get("package_lock")
    if not isinstance(package_lock, Mapping):
        raise ValueError("environment snapshot is missing package_lock")
    probe = package_lock.get("conda_explicit")
    if not isinstance(probe, Mapping):
        raise ValueError("environment snapshot is missing conda_explicit")
    return [
        line
        for line in _nonempty_lines(_required_stdout(probe, "conda list --explicit"))
        if not line.startswith("#") and line != "@EXPLICIT"
    ]


def _installed_dependency_closure(roots: Sequence[str]) -> set[str]:
    """Resolve active, non-extra dependencies from installed metadata."""

    environment = default_environment()
    environment["extra"] = ""
    pending = [_canonical_distribution_name(root) for root in roots]
    visited: set[str] = set()
    while pending:
        name = pending.pop()
        if name in visited:
            continue
        try:
            distribution = importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError as exc:
            raise ValueError(f"required distribution is not installed: {name}") from exc
        visited.add(name)
        for requirement_text in distribution.requires or ():
            try:
                requirement = Requirement(requirement_text)
            except InvalidRequirement as exc:
                raise ValueError(
                    f"invalid requirement metadata for {name}: {requirement_text}"
                ) from exc
            if requirement.marker is not None and not requirement.marker.evaluate(
                environment
            ):
                continue
            dependency = _canonical_distribution_name(requirement.name)
            if dependency not in visited:
                pending.append(dependency)
    return visited


def _pip_requirements(
    snapshot: Mapping[str, Any],
    *,
    included_distributions: set[str] | None = None,
) -> list[str]:
    package_lock = snapshot.get("package_lock")
    if not isinstance(package_lock, Mapping):
        raise ValueError("environment snapshot is missing package_lock")
    probe = package_lock.get("pip_freeze")
    if not isinstance(probe, Mapping):
        raise ValueError("environment snapshot is missing pip_freeze")

    conda_managed = {
        _canonical_distribution_name(str(package["name"]))
        for package in _conda_packages(snapshot)
        if str(package.get("channel", "")).lower() != "pypi"
    }
    requirements: list[str] = []
    for line in _nonempty_lines(_required_stdout(probe, "pip freeze")):
        distribution = _pip_distribution_name(line)
        if distribution is not None and distribution in conda_managed:
            continue
        if included_distributions is not None and distribution not in included_distributions:
            continue
        requirements.append(line)
    return sorted(requirements, key=str.casefold)


def _framework_contract(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    probe = snapshot.get("framework_runtime")
    if not isinstance(probe, Mapping) or not probe.get("ok"):
        raise ValueError("environment snapshot is missing a successful framework probe")
    value = probe.get("value")
    if not isinstance(value, Mapping):
        raise ValueError("framework probe value is not an object")
    torch = value.get("torch")
    if not isinstance(torch, Mapping):
        raise ValueError("framework probe is missing torch")

    devices = torch.get("devices")
    if not isinstance(devices, list) or not devices:
        raise ValueError("framework probe did not observe a CUDA device")
    device_classes = {
        (
            str(device.get("name")),
            tuple(device.get("capability", [])),
            int(device.get("sm_count", 0)),
            int(device.get("total_memory_bytes", 0)),
        )
        for device in devices
        if isinstance(device, Mapping)
    }
    if len(device_classes) != 1:
        raise ValueError(f"expected one homogeneous GPU class, got {device_classes!r}")
    name, capability, sm_count, total_memory_bytes = next(iter(device_classes))

    libraries = {
        name: value.get(name)
        for name in ("diffusers", "transformers", "accelerate")
    }
    return {
        "libraries": libraries,
        "torch": {
            "version": torch.get("version"),
            "cuda_built": torch.get("cuda_built"),
            "cudnn": torch.get("cudnn"),
        },
        "gpu": {
            "count": len(devices),
            "name": name,
            "capability": list(capability),
            "sm_count": sm_count,
            "total_memory_bytes": total_memory_bytes,
        },
    }


def build_runtime_lock(
    snapshot: Mapping[str, Any],
    *,
    included_distributions: set[str] | None = None,
) -> dict[str, Any]:
    """Extract the deterministic runtime contract from an environment snapshot."""

    python = snapshot.get("python")
    platform = snapshot.get("platform")
    if not isinstance(python, Mapping) or not isinstance(platform, Mapping):
        raise ValueError("environment snapshot is missing Python or platform data")

    conda_urls = _conda_explicit_urls(snapshot)
    pip_requirements = _pip_requirements(
        snapshot,
        included_distributions=included_distributions,
    )
    cuda_probe = snapshot.get("cuda_toolkit")
    if not isinstance(cuda_probe, Mapping):
        raise ValueError("environment snapshot is missing cuda_toolkit")
    asle_source = snapshot.get("asle_source")
    if not isinstance(asle_source, Mapping):
        raise ValueError("environment snapshot is missing ASLE source metadata")

    return {
        "schema_version": RUNTIME_LOCK_SCHEMA_VERSION,
        "python": {
            "implementation": python.get("implementation"),
            "version": python.get("version"),
        },
        "platform": {
            "machine": platform.get("machine"),
            "system": platform.get("system"),
        },
        "conda": {
            "explicit_urls": conda_urls,
            "sha256": _lines_sha256(conda_urls),
        },
        "pip": {
            "dependency_roots": list(_ROOT_DISTRIBUTIONS),
            "included_distributions": (
                sorted(included_distributions)
                if included_distributions is not None
                else sorted(
                    distribution
                    for distribution in (
                        _pip_distribution_name(line) for line in pip_requirements
                    )
                    if distribution is not None
                )
            ),
            "requirements": pip_requirements,
            "sha256": _lines_sha256(pip_requirements),
        },
        "framework": _framework_contract(snapshot),
        "cuda_toolkit": {
            "nvcc_version": _required_stdout(cuda_probe, "nvcc --version"),
        },
        "asle_source": {
            "archive_sha256": asle_source.get("archive_sha256"),
            "imported_file_count": asle_source.get("imported_file_count"),
            "imported_tree_sha256": asle_source.get("imported_tree_sha256"),
        },
    }


def compare_runtime_locks(
    expected: Mapping[str, Any],
    observed: Mapping[str, Any],
) -> dict[str, Any]:
    """Return an exact, deterministic comparison report for two locks."""

    fields = (
        "schema_version",
        "python",
        "platform",
        "conda",
        "pip",
        "framework",
        "cuda_toolkit",
        "asle_source",
    )
    mismatches = [
        {
            "field": field,
            "expected": expected.get(field),
            "observed": observed.get(field),
        }
        for field in fields
        if expected.get(field) != observed.get(field)
    ]
    return {
        "schema_version": RUNTIME_LOCK_REPORT_SCHEMA_VERSION,
        "matches": not mismatches,
        "mismatches": mismatches,
    }


def materialize_install_files(lock: Mapping[str, Any], output_dir: Path) -> None:
    """Write conda and pip installer inputs derived from *lock*."""

    if lock.get("schema_version") != RUNTIME_LOCK_SCHEMA_VERSION:
        raise ValueError(f"unsupported runtime lock: {lock.get('schema_version')}")
    conda = lock.get("conda")
    pip = lock.get("pip")
    if not isinstance(conda, Mapping) or not isinstance(pip, Mapping):
        raise ValueError("runtime lock is missing conda or pip sections")
    conda_urls = conda.get("explicit_urls")
    requirements = pip.get("requirements")
    if not isinstance(conda_urls, list) or not all(
        isinstance(item, str) for item in conda_urls
    ):
        raise ValueError("conda explicit_urls must be a string list")
    if not isinstance(requirements, list) or not all(
        isinstance(item, str) for item in requirements
    ):
        raise ValueError("pip requirements must be a string list")

    write_text_atomic(
        output_dir / "conda-explicit.txt",
        "@EXPLICIT\n" + "".join(f"{line}\n" for line in conda_urls),
    )
    write_text_atomic(
        output_dir / "pip-requirements.txt",
        "".join(f"{line}\n" for line in requirements),
    )


def capture_lock(*, repo_root: Path) -> dict[str, Any]:
    included_distributions = _installed_dependency_closure(_ROOT_DISTRIBUTIONS)
    return build_runtime_lock(
        capture_environment(repo_root=repo_root.resolve(), model_root=None),
        included_distributions=included_distributions,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    capture = subparsers.add_parser("capture")
    capture.add_argument("--repo-root", type=Path, default=Path.cwd())
    capture.add_argument("--output", type=Path, required=True)
    capture.add_argument("--materialize-dir", type=Path)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--repo-root", type=Path, default=Path.cwd())
    verify.add_argument("--lock", type=Path, required=True)
    verify.add_argument("--report", type=Path)

    materialize = subparsers.add_parser("materialize")
    materialize.add_argument("--lock", type=Path, required=True)
    materialize.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "capture":
        lock = capture_lock(repo_root=args.repo_root)
        write_json_atomic(args.output, lock)
        if args.materialize_dir is not None:
            materialize_install_files(lock, args.materialize_dir)
        print(args.output.resolve())
        return 0

    lock = read_json(args.lock)
    if not isinstance(lock, dict):
        raise ValueError("runtime lock must be a JSON object")
    if args.command == "materialize":
        materialize_install_files(lock, args.output_dir)
        print(args.output_dir.resolve())
        return 0

    observed = capture_lock(repo_root=args.repo_root)
    report = compare_runtime_locks(lock, observed)
    if args.report is not None:
        write_json_atomic(args.report, report)
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0 if report["matches"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
