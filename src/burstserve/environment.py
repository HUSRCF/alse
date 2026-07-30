"""Environment snapshotting for reproducible BurstServe experiments."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Sequence

from .provenance import collect_environment, write_json_atomic


_PACKAGES = (
    "torch",
    "diffusers",
    "transformers",
    "accelerate",
    "safetensors",
    "sentencepiece",
)


def _run(command: Sequence[str], *, cwd: Path | None = None) -> dict[str, Any]:
    """Run a read-only probe and return a JSON-safe result."""

    try:
        result = subprocess.run(
            list(command),
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "command": list(command),
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "command": list(command),
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def _package_versions() -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for name in _PACKAGES:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def _nvcc_path() -> str | None:
    candidates = [
        shutil.which("nvcc"),
        "/usr/local/cuda/bin/nvcc",
        "/usr/local/cuda-13.3/bin/nvcc",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    return None


def _framework_runtime() -> dict[str, Any]:
    """Probe the active ML runtime in a subprocess.

    Keeping this out of the capture process avoids retaining a CUDA context in
    a caller that wants to snapshot provenance immediately before inference.
    """

    program = """
import json
result = {}
try:
    import torch
    result["torch"] = {
        "version": torch.__version__,
        "cuda_built": torch.version.cuda,
        "cudnn": torch.backends.cudnn.version(),
        "cuda_available": torch.cuda.is_available(),
        "devices": [],
    }
    if torch.cuda.is_available():
        for index in range(torch.cuda.device_count()):
            properties = torch.cuda.get_device_properties(index)
            result["torch"]["devices"].append({
                "index": index,
                "name": properties.name,
                "capability": list(torch.cuda.get_device_capability(index)),
                "sm_count": properties.multi_processor_count,
                "total_memory_bytes": properties.total_memory,
            })
except Exception as exc:
    result["torch_error"] = f"{type(exc).__name__}: {exc}"
for name in ("diffusers", "transformers", "accelerate"):
    try:
        module = __import__(name)
        result[name] = getattr(module, "__version__", "unknown")
    except Exception as exc:
        result[f"{name}_error"] = f"{type(exc).__name__}: {exc}"
print(json.dumps(result, sort_keys=True))
""".strip()
    probe = _run([sys.executable, "-c", program])
    if probe.get("ok"):
        try:
            return {
                "ok": True,
                "value": json.loads(str(probe.get("stdout", ""))),
            }
        except json.JSONDecodeError as exc:
            return {
                "ok": False,
                "error": f"invalid framework probe JSON: {exc}",
                "probe": probe,
            }
    return probe


def _package_lock() -> dict[str, Any]:
    """Capture exact package inventories for the active interpreter."""

    result: dict[str, Any] = {
        "pip_freeze": _run([sys.executable, "-m", "pip", "freeze", "--all"]),
    }
    prefix = Path(sys.prefix).resolve()
    conda_candidates = [
        prefix / "bin" / "conda",
        prefix.parent.parent / "bin" / "conda",
        Path("/data/zhuoxu/miniconda3/bin/conda"),
    ]
    conda = next((path for path in conda_candidates if path.is_file()), None)
    result["conda_explicit"] = (
        _run([str(conda), "list", "--explicit", "-p", str(prefix)])
        if conda
        else {"ok": False, "error": "conda executable not found"}
    )
    return result


def _model_inventory(model_root: Path | None) -> dict[str, Any]:
    if model_root is None:
        return {"root": None, "exists": False, "directories": []}
    root = model_root.resolve()
    directories: list[dict[str, Any]] = []
    if root.is_dir():
        for path in sorted(root.iterdir(), key=lambda item: item.name.casefold()):
            if not path.is_dir():
                continue
            directories.append(
                {
                    "name": path.name,
                    "has_model_index": (path / "model_index.json").is_file(),
                    "has_config": (path / "config.json").is_file(),
                }
            )
    return {
        "root": str(root),
        "exists": root.is_dir(),
        "directories": directories,
    }


def capture_environment(
    *,
    repo_root: Path,
    model_root: Path | None = None,
) -> dict[str, Any]:
    """Capture software, GPU, source, and local model provenance."""

    snapshot: dict[str, Any] = collect_environment()
    snapshot.update(
        {
            "schema_version": "burstserve.environment/v1",
            "captured_at_utc": datetime.now(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z"),
            "packages": _package_versions(),
            "package_lock": _package_lock(),
            "framework_runtime": _framework_runtime(),
            "models": _model_inventory(model_root),
        }
    )

    nvcc = _nvcc_path()
    snapshot["cuda_toolkit"] = (
        _run([nvcc, "--version"]) if nvcc else {"ok": False, "error": "nvcc not found"}
    )
    snapshot["nvidia_smi"] = {
        "gpus": _run(
            [
                "nvidia-smi",
                "--query-gpu=index,name,uuid,pci.bus_id,memory.total,memory.used,"
                "driver_version,compute_cap,pstate",
                "--format=csv,noheader,nounits",
            ]
        ),
        "topology": _run(["nvidia-smi", "topo", "-m"]),
    }
    snapshot["git"] = {
        "head": _run(["git", "rev-parse", "HEAD"], cwd=repo_root),
        "status": _run(["git", "status", "--short", "--branch"], cwd=repo_root),
    }

    source_metadata = repo_root / "vendor" / "ASLE_SOURCE.json"
    if source_metadata.is_file():
        with source_metadata.open("r", encoding="utf-8") as source:
            snapshot["asle_source"] = json.load(source)
    else:
        snapshot["asle_source"] = None
    return snapshot


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--model-root", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    snapshot = capture_environment(
        repo_root=args.repo_root.resolve(),
        model_root=args.model_root,
    )
    write_json_atomic(args.output, snapshot)
    print(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
