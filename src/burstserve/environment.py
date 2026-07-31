"""Environment snapshotting for reproducible BurstServe experiments."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from typing import Any, Mapping, Sequence

from .git_provenance import capture_repository
from .provenance import collect_environment, write_json_atomic


_PACKAGES = (
    "torch",
    "diffusers",
    "transformers",
    "accelerate",
    "safetensors",
    "sentencepiece",
)
_TRUSTED_GIT = "/usr/bin/git"
_TRUSTED_NVIDIA_SMI = "/usr/bin/nvidia-smi"
_ASLE_METADATA_PATH = Path("vendor/ASLE_SOURCE.json")
_ASLE_ARCHIVE_PATH = Path("ASLE.tar.gz")
_MAX_ASLE_METADATA_BYTES = 64 * 1024
_ASLE_METADATA_KEYS = frozenset(
    {
        "schema_version",
        "source_archive",
        "archive_sha256",
        "archive_size_bytes",
        "archive_top_level",
        "imported_path",
        "imported_file_count",
        "imported_tree_sha256",
        "imported_at",
        "policy",
    }
)
_BASE_TRUSTED_ENVIRONMENT = {
    "PATH": "/usr/bin:/bin",
    "LANG": "C",
    "LC_ALL": "C",
    "TZ": "UTC",
}
_TRUSTED_GIT_ENVIRONMENT = {
    **_BASE_TRUSTED_ENVIRONMENT,
    "XDG_CONFIG_HOME": "/nonexistent",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_ATTR_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_PAGER": "cat",
}


def _read_regular_nofollow_bounded(path: Path, maximum_bytes: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise RuntimeError(f"not a regular file: {path}")
        if before.st_size <= 0 or before.st_size > maximum_bytes:
            raise RuntimeError(
                f"file size is outside the bounded contract: {path}"
            )
        chunks: list[bytes] = []
        offset = 0
        while offset < before.st_size:
            chunk = os.pread(
                descriptor,
                min(1024 * 1024, before.st_size - offset),
                offset,
            )
            if not chunk:
                raise RuntimeError(f"file changed while read: {path}")
            chunks.append(chunk)
            offset += len(chunk)
        if os.pread(descriptor, 1, before.st_size):
            raise RuntimeError(f"file grew while read: {path}")
        after = os.fstat(descriptor)
        identity_fields = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns")
        if any(
            getattr(before, field) != getattr(after, field)
            for field in identity_fields
        ):
            raise RuntimeError(f"file identity changed while read: {path}")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _reject_duplicate_json(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise RuntimeError(f"non-finite JSON constant: {value}")


def load_asle_source_metadata(repo_root: Path) -> dict[str, Any]:
    """Strictly load the tracked metadata that pins the untracked ASLE archive."""

    path = repo_root.resolve() / _ASLE_METADATA_PATH
    content = _read_regular_nofollow_bounded(
        path,
        _MAX_ASLE_METADATA_BYTES,
    )
    try:
        value = json.loads(
            content.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_json,
            parse_constant=_reject_json_constant,
            parse_int=lambda text: (
                int(text)
                if len(text.lstrip("-")) <= 20
                else (_ for _ in ()).throw(
                    RuntimeError("ASLE metadata integer is too large")
                )
            ),
        )
    except (
        UnicodeError,
        json.JSONDecodeError,
        RuntimeError,
        ValueError,
    ) as error:
        raise RuntimeError(f"invalid ASLE source metadata: {error}") from error
    if not isinstance(value, dict) or set(value) != _ASLE_METADATA_KEYS:
        raise RuntimeError("ASLE source metadata keys do not match schema")
    if value.get("schema_version") != 1:
        raise RuntimeError("ASLE source metadata schema_version must be 1")
    if value.get("source_archive") != _ASLE_ARCHIVE_PATH.as_posix():
        raise RuntimeError("ASLE source archive path is not canonical")
    for key in ("archive_sha256", "imported_tree_sha256"):
        digest = value.get(key)
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise RuntimeError(f"ASLE metadata {key} is not SHA-256")
    for key in ("archive_size_bytes", "imported_file_count"):
        number = value.get(key)
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or number <= 0
        ):
            raise RuntimeError(f"ASLE metadata {key} must be positive")
    if value["archive_size_bytes"] > 256 * 1024 * 1024:
        raise RuntimeError("ASLE archive exceeds the formal size limit")
    if value.get("archive_top_level") != "ASLE":
        raise RuntimeError("ASLE archive_top_level is not canonical")
    if value.get("imported_path") != "vendor/asle":
        raise RuntimeError("ASLE imported_path is not canonical")
    for key in ("imported_at", "policy"):
        if not isinstance(value.get(key), str) or not value[key]:
            raise RuntimeError(f"ASLE metadata {key} must be nonempty text")
    return value


def verify_asle_archive_snapshot(
    metadata: Mapping[str, Any],
    git_snapshot: Any,
) -> dict[str, Any]:
    """Match the scanner's raw untracked archive image to its tracked pin."""

    matches = [
        entry
        for entry in git_snapshot.untracked_entries
        if entry.path == os.fsencode(_ASLE_ARCHIVE_PATH.as_posix())
    ]
    entry = matches[0] if len(matches) == 1 else None
    checks = {
        "archive_entry_unique": len(matches) == 1,
        "archive_is_regular": entry is not None and entry.kind == "regular",
        "archive_size_exact": (
            entry is not None
            and entry.size == metadata.get("archive_size_bytes")
        ),
        "archive_sha256_exact": (
            entry is not None
            and entry.sha256 == metadata.get("archive_sha256")
        ),
        "archive_mode_read_only_data": (
            entry is not None and entry.mode_octal == "0644"
        ),
    }
    return {
        "path": _ASLE_ARCHIVE_PATH.as_posix(),
        "metadata_path": _ASLE_METADATA_PATH.as_posix(),
        "expected": {
            "sha256": metadata.get("archive_sha256"),
            "size": metadata.get("archive_size_bytes"),
            "mode_octal": "0644",
        },
        "entry": entry.to_dict() if entry is not None else None,
        "checks": checks,
        "passed": all(checks.values()),
    }


def _run(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Run a read-only probe and return a JSON-safe result."""

    try:
        result = subprocess.run(
            list(command),
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env=(dict(environment) if environment is not None else None),
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


def _nvcc_path(*, allow_path_search: bool = True) -> str | None:
    candidates = [
        shutil.which("nvcc") if allow_path_search else None,
        "/usr/local/cuda/bin/nvcc",
        "/usr/local/cuda-13.3/bin/nvcc",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    return None


def _framework_runtime(
    *,
    command_environment: Mapping[str, str] | None = None,
    gpu_probe: bool = True,
    isolated_python: bool = False,
) -> dict[str, Any]:
    """Probe the active ML runtime in a subprocess.

    Keeping this out of the capture process avoids retaining a CUDA context in
    a caller that wants to snapshot provenance immediately before inference.
    """

    program = f"""
import json
import os
result = {{}}
gpu_probe_enabled = {gpu_probe!r}
try:
    import torch
    result["torch"] = {{
        "version": torch.__version__,
        "cuda_built": torch.version.cuda,
        "gpu_probe_enabled": gpu_probe_enabled,
        "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
        "cudnn": None,
        "cuda_available": None,
        "devices": [],
    }}
    if gpu_probe_enabled:
        result["torch"]["cudnn"] = torch.backends.cudnn.version()
        result["torch"]["cuda_available"] = torch.cuda.is_available()
    if gpu_probe_enabled and result["torch"]["cuda_available"]:
        for index in range(torch.cuda.device_count()):
            properties = torch.cuda.get_device_properties(index)
            result["torch"]["devices"].append({{
                "index": index,
                "name": properties.name,
                "capability": list(torch.cuda.get_device_capability(index)),
                "sm_count": properties.multi_processor_count,
                "total_memory_bytes": properties.total_memory,
            }})
except Exception as exc:
    result["torch_error"] = f"{{type(exc).__name__}}: {{exc}}"
for name in ("diffusers", "transformers", "accelerate"):
    try:
        module = __import__(name)
        result[name] = getattr(module, "__version__", "unknown")
    except Exception as exc:
        result[f"{{name}}_error"] = f"{{type(exc).__name__}}: {{exc}}"
print(json.dumps(result, sort_keys=True))
""".strip()
    python_command = [str(Path(sys.executable).resolve())]
    if isolated_python:
        python_command.extend(["-I", "-S"])
    python_command.extend(["-c", program])
    probe = _run(
        python_command,
        environment=command_environment,
    )
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


def _package_lock(
    *,
    command_environment: Mapping[str, str] | None = None,
    isolated_python: bool = False,
) -> dict[str, Any]:
    """Capture exact package inventories for the active interpreter."""

    if isolated_python:
        disabled = {
            "ok": False,
            "error": (
                "disabled in formal capture: package entrypoints can import "
                "same-UID sitecustomize/.pth content before their main logic"
            ),
        }
        return {
            "pip_freeze": dict(disabled),
            "conda_explicit": dict(disabled),
            "conda_json": dict(disabled),
        }
    result: dict[str, Any] = {
        "pip_freeze": _run(
            [
                str(Path(sys.executable).resolve()),
                "-m",
                "pip",
                "freeze",
                "--all",
            ],
            environment=command_environment,
        ),
    }
    prefix = Path(sys.prefix).resolve()
    conda_candidates = [
        prefix / "bin" / "conda",
        prefix.parent.parent / "bin" / "conda",
        Path("/data/zhuoxu/miniconda3/bin/conda"),
    ]
    conda = next((path for path in conda_candidates if path.is_file()), None)
    result["conda_explicit"] = (
        _run(
            [str(conda), "list", "--explicit", "-p", str(prefix)],
            environment=command_environment,
        )
        if conda
        else {"ok": False, "error": "conda executable not found"}
    )
    result["conda_json"] = (
        _run(
            [str(conda), "list", "--json", "-p", str(prefix)],
            environment=command_environment,
        )
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
    command_environment: Mapping[str, str] | None = None,
    framework_gpu_probe: bool = True,
    allow_nvcc_path_search: bool = True,
    isolated_python: bool = False,
    git_expected_gitlinks: Mapping[str, str | None] | None = None,
    git_allowed_untracked_roots: Sequence[str] = (),
    git_allow_untracked_regular_files: bool = False,
    require_asle_binding: bool = False,
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
            "package_lock": _package_lock(
                command_environment=command_environment,
                isolated_python=isolated_python,
            ),
            "framework_runtime": _framework_runtime(
                command_environment=command_environment,
                gpu_probe=framework_gpu_probe,
                isolated_python=isolated_python,
            ),
            "models": _model_inventory(model_root),
            "subprocess_capture_policy": {
                "exact_environment": (
                    dict(command_environment)
                    if command_environment is not None
                    else None
                ),
                "framework_gpu_probe_enabled": framework_gpu_probe,
                "isolated_python": isolated_python,
                "framework_target_gpu_exposure": (
                    command_environment.get("CUDA_VISIBLE_DEVICES")
                    if command_environment is not None
                    else os.environ.get("CUDA_VISIBLE_DEVICES")
                ),
                "trusted_git_executable": _TRUSTED_GIT,
                "trusted_nvidia_smi_executable": _TRUSTED_NVIDIA_SMI,
            },
        }
    )

    nvcc = _nvcc_path(allow_path_search=allow_nvcc_path_search)
    snapshot["cuda_toolkit"] = (
        _run(
            [nvcc, "--version"],
            environment=command_environment,
        )
        if nvcc
        else {"ok": False, "error": "nvcc not found"}
    )
    snapshot["nvidia_smi"] = {
        "gpus": _run(
            [
                _TRUSTED_NVIDIA_SMI,
                "--query-gpu=index,name,uuid,pci.bus_id,memory.total,memory.used,"
                "driver_version,compute_cap,pstate",
                "--format=csv,noheader,nounits",
            ],
            environment=command_environment,
        ),
        "topology": _run(
            [_TRUSTED_NVIDIA_SMI, "topo", "-m"],
            environment=command_environment,
        ),
    }
    git_snapshot = capture_repository(
        repo_root,
        expected_gitlinks=git_expected_gitlinks,
        allowed_untracked_roots=git_allowed_untracked_roots,
        allow_untracked_regular_files=(
            git_allow_untracked_regular_files
        ),
        git=Path(_TRUSTED_GIT),
    )
    snapshot["git"] = git_snapshot.to_dict()

    try:
        asle_source = load_asle_source_metadata(repo_root)
        asle_archive = verify_asle_archive_snapshot(
            asle_source,
            git_snapshot,
        )
        if require_asle_binding and not asle_archive["passed"]:
            raise RuntimeError("ASLE archive does not match its tracked pin")
        snapshot["asle_source"] = asle_source
        snapshot["asle_archive"] = asle_archive
    except (OSError, RuntimeError) as error:
        if require_asle_binding:
            raise
        snapshot["asle_source"] = None
        snapshot["asle_archive"] = {
            "passed": False,
            "error": f"{type(error).__name__}: {error}",
        }
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
