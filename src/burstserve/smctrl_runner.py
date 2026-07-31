"""Build and run the native Gate-A SM-ID probe with complete provenance.

Masked probes deliberately fail closed on every CUDA driver API version absent
from the pinned libsmctrl source.  An experimental run must opt in explicitly;
stream masking additionally requires an explicit ``MASK_OFF``.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
import ctypes
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import shlex
import signal
import stat
import subprocess
import sys
import threading
import time
from typing import Any, Mapping, Sequence

from .environment import (
    capture_environment,
    load_asle_source_metadata,
    verify_asle_archive_snapshot,
)
from .git_provenance import (
    RepositorySnapshot,
    capture_repository,
)
from .nvml_events import (
    MAX_NVML_LIBRARY_BYTES,
    NVML_EVENT_TYPE_XID_CRITICAL_ERROR,
    NvmlXidMonitor,
    PROVENANCE_SCHEMA_VERSION,
)
from .provenance import (
    EventRecord,
    RunManifest,
    append_jsonl_atomic,
    canonical_json,
    write_json_atomic,
    write_text_atomic,
)


DEFAULT_BINARY = Path("build/smctrl_probe/smid_probe")
DEFAULT_BUILD_SOURCE = Path("native/smctrl_probe")
DEFAULT_RUN_ROOT = Path("experiments/runs")
DEFAULT_LIBSMCTRL_ROOT = Path("vendor/libsmctrl")
DEFAULT_SOURCE_METADATA = Path("vendor/LIBSMCTRL_SOURCE.json")
DEFAULT_GATE_MANIFEST = Path("experiments/manifests/gate_a_4090.json")
TRUSTED_GIT_EXECUTABLE = Path("/usr/bin/git")
TRUSTED_NVIDIA_SMI_EXECUTABLE = Path("/usr/bin/nvidia-smi")
TRUSTED_PS_EXECUTABLE = Path("/usr/bin/ps")
DEFAULT_LIBCUDA_LINK = Path(
    "/usr/lib/x86_64-linux-gnu/libcuda.so.1"
)
TRUSTED_LIBCUDA_DIRECTORY = Path("/usr/lib/x86_64-linux-gnu")

NATIVE_SCHEMA_VERSION = "burstserve.smid-probe-native/v2"
CELL_SCHEMA_VERSION = "burstserve.smid-probe-cell/v2"
OUTCOME_SCHEMA_VERSION = "burstserve.smid-probe-outcome/v2"
GATE_MANIFEST_SCHEMA_VERSION = "burstserve.gate-a-manifest/v2"
NATIVE_BUILD_ATTESTATION_SCHEMA_VERSION = (
    "burstserve.native-build-attestation/v2"
)
GPU_RESERVATION_SCHEMA_VERSION = "burstserve.gpu-reservation/v1"
GPU_QUARANTINE_SCHEMA_VERSION = "burstserve.gpu-quarantine/v1"
GPU_MASKED_ARMED_POISON_SCHEMA_VERSION = (
    "burstserve.gpu-masked-armed-poison/v1"
)
RUNNER_VERSION = "burstserve.smctrl-runner/v2"
MASKED_HEALTH_MONITOR_IMPLEMENTED = True
MASKED_XID_DRAIN_TIMEOUT_MS = 1000
MASKED_XID_TOTAL_BUDGET_MS = 2000
FINAL_REAP_TIMEOUT_S = 5.0
RESERVATION_SAFETY_MARGIN_S = 5.0
FINAL_PREFLIGHT_QUERY_BUDGET_S = 30.0
POST_HEALTH_QUERY_BUDGET_S = 30.0
PROCESS_SUPERVISION_CLEANUP_BUDGET_S = 3 * FINAL_REAP_TIMEOUT_S
PROBE_THREADS_PER_BLOCK = 256
GPU_LOCK_DIRECTORY_NAME = ".burstserve-gpu-locks"
NATIVE_GATE_COMPLETION_SENTINEL = (
    "burstserve-native-gate-required-check: verified"
)
NATIVE_BUILD_ATTESTATION_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "build_lock",
        "build_stamp",
        "build_environment",
        "toolchain",
        "finite_build_contract",
        "source_hashes",
        "outputs",
        "launcher_contract",
        "test_fixture_contract",
        "real_probe_runtime_dependencies",
        "guard_fixture_runtime_dependencies",
        "checks",
    }
)
MAX_NATIVE_BUILD_ATTESTATION_BYTES = 8 * 1024 * 1024
MAX_NATIVE_BUILD_ATTESTATION_INTEGER_DIGITS = 128
MAX_GATE_MANIFEST_BYTES = 1024 * 1024
MAX_SOURCE_METADATA_BYTES = 1024 * 1024
MAX_NATIVE_STDOUT_BYTES = 1024 * 1024
MAX_NATIVE_STDERR_BYTES = 1024 * 1024
MAX_BUILD_STAMP_BYTES = 1024 * 1024
MAX_FORMAL_SOURCE_FILE_BYTES = 64 * 1024 * 1024
MAX_FORMAL_JSON_INTEGER_DIGITS = 128
MAX_FORMAL_JSON_DEPTH = 64
MAX_FORMAL_JSON_NODES = 65536
MAX_FORMAL_REGULAR_ARTIFACT_BYTES = 1024 * 1024 * 1024
GATE_MANIFEST_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "manifest_id",
        "hardware",
        "source",
        "safety",
        "baseline",
        "single_tpc_matrix_after_explicit_promotion",
        "promotion_requirements",
    }
)
GATE_MANIFEST_HARDWARE_KEYS = frozenset(
    {
        "gpu_name",
        "physical_gpu_indices",
        "compute_capability",
        "sm_count",
        "expected_tpc_count",
        "driver_version",
        "driver_api_version",
        "runtime_api_version",
        "toolkit_version",
    }
)
GATE_MANIFEST_SOURCE_KEYS = frozenset(
    {
        "libsmctrl_commit",
        "libsmctrl_metadata",
        "approved_launcher_sha256",
        "approved_real_probe_sha256",
        "approved_build_stamp_sha256",
        "approved_build_attestation_sha256",
    }
)
GATE_MANIFEST_SAFETY_KEYS = frozenset(
    {
        "protocol",
        "timeout_s",
        "maximum_preexisting_gpu_memory_mib",
        "unknown_driver_policy",
        "experimental_mask_enabled",
        "approved_mask_modes",
        "reserved_gpu_uuids",
        "exclusive_reservation_evidence",
        "xid_monitoring",
        "stream_offset_search_enabled",
        "stream_mask_off_candidates",
        "global_next_matrix_accepted",
        "mps_allowed",
        "mps_bypass",
    }
)
GATE_MANIFEST_BASELINE_KEYS = frozenset(
    {
        "blocks_per_sm",
        "iterations",
        "minimum_sm_coverage_fraction",
        "trials_per_gpu",
        "threads_per_block",
    }
)
GATE_MANIFEST_MATRIX_KEYS = frozenset(
    {
        "modes",
        "tpc_bits",
        "trials_per_cell",
        "allowed_observed_sm_count",
        "iterations",
        "blocks",
        "threads_per_block",
    }
)
GATE_MANIFEST_RESERVATION_KEYS = frozenset(
    {
        "schema_version",
        "status",
        "gpu_uuid",
        "physical_gpu",
        "reservation_id",
        "owner",
        "valid_from_utc",
        "valid_until_utc",
    }
)
GATE_MANIFEST_XID_KEYS = frozenset(
    {
        "available",
        "method",
        "quiet_ms",
        "library_path",
        "library_sha256",
        "library_version",
    }
)
GATE_MANIFEST_RECORD_KEYS = frozenset(
    {"path", "git_blob", "sha256", "content"}
)
NATIVE_STDOUT_TOP_LEVEL_ORDER = (
    "schema_version",
    "status",
    "mode",
    "driver_version",
    "runtime_version",
    "parent_guard",
    "device",
    "requested_enabled_tpc",
    "tpc_count",
    "blocks",
    "threads_per_block",
    "iterations",
    "observed_histogram",
)
NATIVE_STDOUT_PARENT_GUARD_ORDER = (
    "mode",
    "status",
    "expected_parent_pid",
    "observed_parent_pid",
    "inherited_pdeath_signal",
    "pdeath_signal",
)
NATIVE_STDOUT_DEVICE_ORDER = (
    "ordinal",
    "name",
    "uuid",
    "cc_major",
    "cc_minor",
    "sm_count",
)
SOURCE_METADATA_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "source_url",
        "source_commit",
        "retrieved_on",
        "path",
        "files",
        "compatibility",
        "policy",
    }
)
SOURCE_METADATA_FILE_KEYS = frozenset(
    {"README.md", "libsmctrl.c", "libsmctrl.h"}
)
SOURCE_METADATA_COMPATIBILITY_KEYS = frozenset(
    {
        "upstream_readme_cuda_range",
        "latest_x86_64_stream_case",
        "target_driver_api_version",
        "target_stream_mask_status",
    }
)
_ATTESTATION_IDENTITY_FIELDS = (
    "path",
    "device",
    "inode",
    "mode",
    "size",
    "mtime_ns",
    "sha256",
)
FORMAL_BUILD_ENVIRONMENT = {
    "PATH": "/usr/local/cuda-13.3/bin:/usr/bin:/bin",
    "LANG": "C",
    "LC_ALL": "C",
    "TZ": "UTC",
}
TRUSTED_TOOL_ENVIRONMENT = {
    "PATH": "/usr/bin:/bin",
    "LANG": "C",
    "LC_ALL": "C",
    "TZ": "UTC",
}
TRUSTED_GIT_ENVIRONMENT = {
    **TRUSTED_TOOL_ENVIRONMENT,
    "XDG_CONFIG_HOME": "/nonexistent",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_ATTR_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_PAGER": "cat",
}
FORMAL_ENVIRONMENT_CAPTURE_SUBPROCESS_ENVIRONMENT = {
    **TRUSTED_TOOL_ENVIRONMENT,
    "CUDA_VISIBLE_DEVICES": "",
    "CUDA_MPS_PIPE_DIRECTORY": "",
}
FORMAL_GIT_ALLOWED_UNTRACKED_ROOTS = (
    "experiments/runs",
    "related_work",
    "vendor/asle",
)
FORMAL_LAUNCHER_THREAT_BOUNDARIES = {
    "python_pre_main_injection": (
        "The runner process must be launched by a trusted, sanitized "
        "supervisor. LD_PRELOAD, audit modules, sitecustomize, .pth files, "
        "or a replaced Python runtime can execute before this module can "
        "enforce its subprocess allowlists."
    ),
    "same_uid_out_of_band_launcher_write": (
        "The launcher FD is rehashed after the final GPU preflight and "
        "immediately before Popen. A same-UID process that can write the "
        "already-open inode after that check and before child exec remains "
        "outside the in-process protocol; OS-level UID/mount isolation or a "
        "sealed self-contained launcher is required to eliminate this race."
    ),
}

BUILD_STAMP_FIELDS = (
    "CUDA_HOME",
    "CUDA_ARCH",
    "NVCC",
    "CC",
    "AR",
    "CPPFLAGS",
    "CFLAGS",
    "NVCCFLAGS",
    "LDLIBS",
    "LAUNCHER_CFLAGS",
    "LIBSMCTRL_DIR",
    "BUILD_TMPDIR",
    "BUILD_LOCK_PATH",
    "HERMETIC_PATH",
    "BUILD_ENVIRONMENT_SHA256",
    "MAKEFILE_SHA256",
    "README_SHA256",
    "PROBE_CU_SHA256",
    "GUARD_LAUNCHER_C_SHA256",
    "GUARD_EXEC_TEST_FIXTURE_C_SHA256",
    "PARENT_GUARD_C_SHA256",
    "PARENT_GUARD_H_SHA256",
    "PARENT_GUARD_TEST_HELPER_C_SHA256",
    "SEALED_EXEC_C_SHA256",
    "SEALED_EXEC_H_SHA256",
    "SHA256_C_SHA256",
    "SHA256_H_SHA256",
    "BUILD_ATTESTATION_PY_SHA256",
    "GIT_PROVENANCE_PY_SHA256",
    "TEST_NATIVE_PARENT_GUARD_PY_SHA256",
    "LIBSMCTRL_C_SHA256",
    "LIBSMCTRL_H_SHA256",
    "LIBSMCTRL_GIT_COMMIT",
    "LIBSMCTRL_GIT_DIRTY",
    "LIBSMCTRL_GIT_STATUS_SHA256",
    "LIBSMCTRL_GIT_SNAPSHOT_SHA256",
    "CUDA_INCLUDE_ROOT",
    "CUDA_INCLUDE_TREE_SHA256",
    "CUDA_LIBDEVICE_ROOT",
    "CUDA_LIBDEVICE_TREE_SHA256",
    "CUDA_RUNTIME_LIBRARY",
    "CUDA_RUNTIME_LIBRARY_SHA256",
    "CUDA_VERSION_FILE",
    "CUDA_VERSION_FILE_SHA256",
    "LIBCUDA_LINK_LIBRARY",
    "LIBCUDA_LINK_LIBRARY_SHA256",
    "NVCC_EXECUTABLE",
    "NVCC_EXECUTABLE_SHA256",
    "NVCC_VERSION_SHA256",
    "NVCC_DRYRUN_SHA256",
    "CUDA_SUBTOOLS_SHA256",
    "CC_EXECUTABLE",
    "CC_EXECUTABLE_SHA256",
    "CC_VERSION_SHA256",
    "CC_SEARCH_PATHS_SHA256",
    "CC_INCLUDE_SEARCH_SHA256",
    "CC_DRYRUN_SHA256",
    "CC_SPECS_SHA256",
    "HOST_TOOLCHAIN_COMPONENTS_SHA256",
    "AR_EXECUTABLE",
    "AR_EXECUTABLE_SHA256",
    "AR_VERSION_SHA256",
    "PYTHON_EXECUTABLE",
    "PYTHON_EXECUTABLE_SHA256",
    "PYTHON_VERSION_SHA256",
)

BUILD_SOURCE_PATHS = {
    "MAKEFILE_SHA256": Path("native/smctrl_probe/Makefile"),
    "README_SHA256": Path("native/smctrl_probe/README.md"),
    "PROBE_CU_SHA256": Path("native/smctrl_probe/smid_probe.cu"),
    "GUARD_LAUNCHER_C_SHA256": Path(
        "native/smctrl_probe/guard_launcher.c"
    ),
    "GUARD_EXEC_TEST_FIXTURE_C_SHA256": Path(
        "native/smctrl_probe/guard_exec_test_fixture.c"
    ),
    "PARENT_GUARD_C_SHA256": Path("native/smctrl_probe/parent_guard.c"),
    "PARENT_GUARD_H_SHA256": Path("native/smctrl_probe/parent_guard.h"),
    "PARENT_GUARD_TEST_HELPER_C_SHA256": Path(
        "native/smctrl_probe/parent_guard_test_helper.c"
    ),
    "SEALED_EXEC_C_SHA256": Path("native/smctrl_probe/sealed_exec.c"),
    "SEALED_EXEC_H_SHA256": Path("native/smctrl_probe/sealed_exec.h"),
    "SHA256_C_SHA256": Path("native/smctrl_probe/sha256.c"),
    "SHA256_H_SHA256": Path("native/smctrl_probe/sha256.h"),
    "BUILD_ATTESTATION_PY_SHA256": Path(
        "native/smctrl_probe/build_attestation.py"
    ),
    "GIT_PROVENANCE_PY_SHA256": Path(
        "src/burstserve/git_provenance.py"
    ),
    "TEST_NATIVE_PARENT_GUARD_PY_SHA256": Path(
        "tests/test_native_parent_guard.py"
    ),
    "LIBSMCTRL_C_SHA256": Path("vendor/libsmctrl/libsmctrl.c"),
    "LIBSMCTRL_H_SHA256": Path("vendor/libsmctrl/libsmctrl.h"),
}

PROBE_MODES = ("baseline", "global", "next", "stream")
MASKED_MODES = frozenset({"global", "next", "stream"})
BASELINE_MIN_SM_COVERAGE = 0.75
PINNED_VALIDATED_DRIVER_VERSIONS = frozenset(
    {
        6050,
        7000,
        7050,
        8000,
        9000,
        9010,
        9020,
        10000,
        10010,
        10020,
        11000,
        11010,
        11020,
        11030,
        11040,
        11050,
        11060,
        11070,
        11080,
        12000,
        12010,
        12020,
        12030,
        12040,
        12050,
        12060,
        12070,
        12080,
    }
)


class NativeOutputError(ValueError):
    """Raised when the native probe does not honor its stdout contract."""


class _ChildWindowInterrupted(BaseException):
    """Raised by temporary signal handlers while the native child is live."""

    def __init__(self, signum: int) -> None:
        super().__init__(f"received signal {signum} while native child was live")
        self.signum = signum


class _SourceRevalidationRejected(RuntimeError):
    """Raised internally when the final pre-exec source snapshot changed."""


class _InstalledChildSignalHandlers(dict[int, Any]):
    """Previous handlers plus a deferrable interruption state."""

    def __init__(self) -> None:
        super().__init__()
        self.raise_immediately = True
        self.raised_once = False
        self.pending_signum: int | None = None

    def handle(self, signum: int, _frame: Any) -> None:
        if self.pending_signum is None:
            self.pending_signum = signum
        if self.raise_immediately and not self.raised_once:
            self.raised_once = True
            raise _ChildWindowInterrupted(signum)

    def defer_interrupts(self) -> None:
        """Record HUP/TERM without aborting evidence finalization."""

        self.raise_immediately = False


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _install_child_signal_handlers() -> _InstalledChildSignalHandlers:
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("child signal handlers require the Python main thread")
    previous = _InstalledChildSignalHandlers()

    try:
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, previous.handle)
    except BaseException:
        for signum, old_handler in previous.items():
            signal.signal(signum, old_handler)
        raise
    return previous


def _restore_signal_handlers(previous: Mapping[int, Any]) -> None:
    for signum, old_handler in previous.items():
        signal.signal(signum, old_handler)


def _valid_sha256(value: Any) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _valid_git_oid(value: Any) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) in {40, 64}
        and all(character in "0123456789abcdef" for character in value)
    )


def _sha256_fd(
    descriptor: int,
    *,
    maximum_bytes: int | None = None,
) -> str:
    digest = hashlib.sha256()
    offset = 0
    while True:
        chunk = os.pread(descriptor, 1024 * 1024, offset)
        if not chunk:
            break
        if maximum_bytes is not None and offset + len(chunk) > maximum_bytes:
            raise RuntimeError("artifact grew beyond its size limit while read")
        digest.update(chunk)
        offset += len(chunk)
    return digest.hexdigest()


def _xattr_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for name in sorted(os.listxattr(path, follow_symlinks=False)):
        value = os.getxattr(path, name, follow_symlinks=False)
        records.append(
            {
                "name": name,
                "size_bytes": len(value),
                "sha256": hashlib.sha256(value).hexdigest(),
            }
        )
    return records


def _open_regular_nofollow(
    path: Path,
    *,
    maximum_size: int = MAX_FORMAL_REGULAR_ARTIFACT_BYTES,
) -> tuple[int, dict[str, Any]]:
    if (
        isinstance(maximum_size, bool)
        or not isinstance(maximum_size, int)
        or maximum_size <= 0
    ):
        raise ValueError("maximum regular artifact size must be positive")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    # A hostile FIFO/device at an artifact path must reach fstat promptly
    # instead of blocking the formal supervisor before type validation.
    flags |= getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    try:
        identity = os.fstat(descriptor)
        if not stat.S_ISREG(identity.st_mode):
            raise RuntimeError(f"artifact is not a regular file: {path}")
        if identity.st_size > maximum_size:
            raise RuntimeError(
                f"artifact exceeds the {maximum_size}-byte size limit: {path}"
            )
        record = {
            "path": str(path),
            "device": int(identity.st_dev),
            "inode": int(identity.st_ino),
            "mode": int(identity.st_mode),
            "size": int(identity.st_size),
            "mtime_ns": int(identity.st_mtime_ns),
            "sha256": _sha256_fd(
                descriptor,
                maximum_bytes=maximum_size,
            ),
        }
        return descriptor, record
    except BaseException:
        os.close(descriptor)
        raise


def _strict_json_duplicate_guard(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise RuntimeError(f"duplicate JSON key: {key!r}")
        value[key] = item
    return value


def _strict_json_integer(value: str) -> int:
    digits = value[1:] if value.startswith("-") else value
    if len(digits) > MAX_FORMAL_JSON_INTEGER_DIGITS:
        raise RuntimeError("JSON integer exceeds the digit limit")
    return int(value, 10)


def _strict_json_float(value: str) -> float:
    if len(value) > MAX_FORMAL_JSON_INTEGER_DIGITS:
        raise RuntimeError("JSON float token exceeds the length limit")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise RuntimeError("JSON number is not finite")
    return parsed


def _strict_json_constant(value: str) -> Any:
    raise RuntimeError(f"non-standard JSON constant: {value}")


def _validate_json_complexity(value: Any, *, label: str) -> None:
    nodes = 0
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > MAX_FORMAL_JSON_NODES:
            raise RuntimeError(f"{label} exceeds the JSON node limit")
        if depth > MAX_FORMAL_JSON_DEPTH:
            raise RuntimeError(f"{label} exceeds the JSON depth limit")
        if isinstance(item, dict):
            stack.extend((nested, depth + 1) for nested in item.values())
        elif isinstance(item, list):
            stack.extend((nested, depth + 1) for nested in item)


def _parse_strict_json_document(
    content: bytes,
    *,
    label: str,
    maximum_bytes: int,
    sort_keys: bool,
    trailing_newline: bool,
    require_canonical: bool = True,
) -> Any:
    if not isinstance(content, bytes):
        raise TypeError(f"{label} content must be bytes")
    if not content:
        raise RuntimeError(f"{label} is empty")
    if len(content) > maximum_bytes:
        raise RuntimeError(f"{label} exceeds the {maximum_bytes}-byte limit")
    try:
        decoded = content.decode("utf-8", errors="strict")
        value = json.loads(
            decoded,
            object_pairs_hook=_strict_json_duplicate_guard,
            parse_constant=_strict_json_constant,
            parse_float=_strict_json_float,
            parse_int=_strict_json_integer,
        )
    except (
        UnicodeError,
        json.JSONDecodeError,
        OverflowError,
        RecursionError,
        RuntimeError,
        ValueError,
    ) as error:
        raise RuntimeError(f"invalid {label} JSON: {error}") from error
    _validate_json_complexity(value, label=label)
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=sort_keys,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError) as error:
        raise RuntimeError(
            f"{label} cannot be canonically encoded: {error}"
        ) from error
    if trailing_newline:
        encoded += b"\n"
    if require_canonical and content != encoded:
        raise RuntimeError(f"{label} is not canonical UTF-8 JSON")
    return value


def _read_bounded_regular_bytes(
    path: Path,
    *,
    label: str,
    maximum_bytes: int,
    allow_empty: bool = False,
) -> bytes:
    descriptor, identity = _open_regular_nofollow(
        path,
        maximum_size=maximum_bytes,
    )
    try:
        content = _read_bounded_descriptor_bytes(
            descriptor,
            label=label,
            maximum_bytes=maximum_bytes,
            allow_empty=allow_empty,
        )
        if hashlib.sha256(content).hexdigest() != identity["sha256"]:
            raise RuntimeError(f"{label} identity changed while it was read")
        return content
    finally:
        os.close(descriptor)


def _read_bounded_descriptor_bytes(
    descriptor: int,
    *,
    label: str,
    maximum_bytes: int,
    allow_empty: bool,
) -> bytes:
    initial = os.fstat(descriptor)
    if not stat.S_ISREG(initial.st_mode):
        raise RuntimeError(f"{label} is not a regular file")
    size = int(initial.st_size)
    if size < 0 or (size == 0 and not allow_empty):
        raise RuntimeError(f"{label} is empty")
    if size > maximum_bytes:
        raise RuntimeError(
            f"{label} exceeds the {maximum_bytes}-byte limit"
        )
    chunks: list[bytes] = []
    offset = 0
    while offset < size:
        chunk = os.pread(
            descriptor,
            min(1024 * 1024, size - offset),
            offset,
        )
        if not chunk:
            raise RuntimeError(f"{label} changed while it was read")
        chunks.append(chunk)
        offset += len(chunk)
    if os.pread(descriptor, 1, size):
        raise RuntimeError(f"{label} grew while it was read")
    final = os.fstat(descriptor)
    if (
        int(final.st_dev) != int(initial.st_dev)
        or int(final.st_ino) != int(initial.st_ino)
        or int(final.st_mode) != int(initial.st_mode)
        or int(final.st_size) != size
        or int(final.st_mtime_ns) != int(initial.st_mtime_ns)
    ):
        raise RuntimeError(f"{label} identity changed while it was read")
    return b"".join(chunks)


def _native_attestation_duplicate_guard(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise RuntimeError(
                f"duplicate key in native build attestation: {key!r}"
            )
        value[key] = item
    return value


def _native_attestation_integer(value: str) -> int:
    digits = value[1:] if value.startswith("-") else value
    if len(digits) > MAX_NATIVE_BUILD_ATTESTATION_INTEGER_DIGITS:
        raise RuntimeError(
            "integer in native build attestation exceeds the digit limit"
        )
    return int(value, 10)


def _native_attestation_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise RuntimeError(
            "non-finite number in native build attestation"
        )
    return parsed


def _native_attestation_constant(value: str) -> Any:
    raise RuntimeError(
        f"non-standard constant in native build attestation: {value}"
    )


def _native_attestation_canonical_bytes(value: Any) -> bytes:
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return (encoded + "\n").encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError) as error:
        raise RuntimeError(
            f"native build attestation cannot be canonically encoded: {error}"
        ) from error


def _parse_native_build_attestation(content: bytes) -> dict[str, Any]:
    """Parse the one canonical on-disk native attestation representation."""

    if not isinstance(content, bytes):
        raise TypeError("native build attestation content must be bytes")
    if not content:
        raise RuntimeError("native build attestation is empty")
    if len(content) > MAX_NATIVE_BUILD_ATTESTATION_BYTES:
        raise RuntimeError("native build attestation exceeds the size limit")
    try:
        decoded = content.decode("utf-8", errors="strict")
        value = json.loads(
            decoded,
            object_pairs_hook=_native_attestation_duplicate_guard,
            parse_constant=_native_attestation_constant,
            parse_float=_native_attestation_float,
            parse_int=_native_attestation_integer,
        )
    except (
        UnicodeError,
        json.JSONDecodeError,
        OverflowError,
        RuntimeError,
        ValueError,
    ) as error:
        raise RuntimeError(
            f"invalid native build attestation JSON: {error}"
        ) from error
    if not isinstance(value, dict):
        raise RuntimeError("native build attestation must be a JSON object")
    if content != _native_attestation_canonical_bytes(value):
        raise RuntimeError(
            "native build attestation is not canonical UTF-8 JSON"
        )
    if value.get("schema_version") != NATIVE_BUILD_ATTESTATION_SCHEMA_VERSION:
        raise RuntimeError("unsupported native build-attestation schema")
    if set(value) != NATIVE_BUILD_ATTESTATION_TOP_LEVEL_KEYS:
        raise RuntimeError("native build attestation top-level keys mismatch")
    return value


def _attestation_identity_matches(
    actual: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> bool:
    return all(
        actual.get(field) == expected.get(field)
        for field in _ATTESTATION_IDENTITY_FIELDS
    )


def _read_open_attestation(
    descriptor: int,
    identity: Mapping[str, Any],
) -> bytes:
    """Read a bounded descriptor snapshot and revalidate its hashed identity."""

    size = identity.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise RuntimeError("native build attestation has an invalid size")
    if size > MAX_NATIVE_BUILD_ATTESTATION_BYTES:
        raise RuntimeError("native build attestation exceeds the size limit")
    chunks: list[bytes] = []
    offset = 0
    while offset < size:
        chunk = os.pread(descriptor, min(1024 * 1024, size - offset), offset)
        if not chunk:
            raise RuntimeError(
                "native build attestation changed while it was read"
            )
        chunks.append(chunk)
        offset += len(chunk)
    if os.pread(descriptor, 1, size):
        raise RuntimeError(
            "native build attestation changed while it was read"
        )
    content = b"".join(chunks)
    if hashlib.sha256(content).hexdigest() != identity.get("sha256"):
        raise RuntimeError(
            "native build attestation changed after it was hashed"
        )
    final_status = os.fstat(descriptor)
    final_identity = {
        "path": identity.get("path"),
        "device": int(final_status.st_dev),
        "inode": int(final_status.st_ino),
        "mode": int(final_status.st_mode),
        "size": int(final_status.st_size),
        "mtime_ns": int(final_status.st_mtime_ns),
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    if not _attestation_identity_matches(final_identity, identity):
        raise RuntimeError(
            "native build attestation identity changed while it was read"
        )
    return content


def revalidate_launcher_fd_final(
    descriptor: int,
    expected_identity: Mapping[str, Any],
) -> dict[str, Any]:
    """Rehash the already-open launcher immediately before ``Popen``."""

    captured_at = _utc_now()
    try:
        before = os.fstat(descriptor)
        digest = _sha256_fd(descriptor)
        after = os.fstat(descriptor)
        observed = {
            "path": expected_identity.get("path"),
            "device": int(after.st_dev),
            "inode": int(after.st_ino),
            "mode": int(after.st_mode),
            "size": int(after.st_size),
            "mtime_ns": int(after.st_mtime_ns),
            "sha256": digest,
        }
        stable_during_check = (
            _status_identity(before) == _status_identity(after)
        )
        regular = stat.S_ISREG(after.st_mode)
        matches_initial = _attestation_identity_matches(
            observed,
            expected_identity,
        )
        checks = {
            "descriptor_is_regular": regular,
            "descriptor_stable_during_final_hash": stable_during_check,
            "identity_matches_initial_open": matches_initial,
        }
        return {
            "captured_at_utc": captured_at,
            "completed": True,
            "error": None,
            "identity": observed,
            "checks": checks,
            "passed": all(checks.values()),
            "same_uid_out_of_band_write_threat_boundary": (
                FORMAL_LAUNCHER_THREAT_BOUNDARIES[
                    "same_uid_out_of_band_launcher_write"
                ]
            ),
        }
    except Exception as error:
        return {
            "captured_at_utc": captured_at,
            "completed": False,
            "error": f"{type(error).__name__}: {error}",
            "identity": None,
            "checks": {
                "descriptor_is_regular": False,
                "descriptor_stable_during_final_hash": False,
                "identity_matches_initial_open": False,
            },
            "passed": False,
            "same_uid_out_of_band_write_threat_boundary": (
                FORMAL_LAUNCHER_THREAT_BOUNDARIES[
                    "same_uid_out_of_band_launcher_write"
                ]
            ),
        }


class _FlockLease:
    """A no-follow regular-file flock with evidence-friendly identity."""

    def __init__(
        self,
        path: Path,
        *,
        operation: int,
        kind: str,
    ) -> None:
        self.path = path
        self.operation = operation
        self.kind = kind
        self.descriptor: int | None = None
        self.record: dict[str, Any] | None = None

    def __enter__(self) -> "_FlockLease":
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        parent_status = self.path.parent.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(parent_status.st_mode)
            or parent_status.st_uid != os.geteuid()
            or stat.S_IMODE(parent_status.st_mode) & 0o077
        ):
            raise RuntimeError(
                f"{self.kind} lock directory is not owner-private"
            )
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.path, flags, 0o600)
        try:
            identity = os.fstat(descriptor)
            if not stat.S_ISREG(identity.st_mode):
                raise RuntimeError(f"{self.kind} lock is not a regular file")
            if (
                identity.st_uid != os.geteuid()
                or stat.S_IMODE(identity.st_mode) != 0o600
                or identity.st_nlink != 1
            ):
                raise RuntimeError(
                    f"{self.kind} lock is not an owner-private, "
                    "single-link 0600 file"
                )
            fcntl.flock(descriptor, self.operation | fcntl.LOCK_NB)
        except BaseException:
            os.close(descriptor)
            raise
        self.descriptor = descriptor
        self.record = {
            "kind": self.kind,
            "path": str(self.path),
            "device": int(identity.st_dev),
            "inode": int(identity.st_ino),
            "uid": int(identity.st_uid),
            "gid": int(identity.st_gid),
            "mode_octal": f"{stat.S_IMODE(identity.st_mode):04o}",
            "nlink": int(identity.st_nlink),
            "acquired_at_utc": _utc_now(),
            "nonblocking": True,
            "mode": (
                "shared"
                if self.operation == fcntl.LOCK_SH
                else "exclusive"
            ),
        }
        return self

    def close(self) -> None:
        if self.descriptor is None:
            return
        descriptor = self.descriptor
        self.descriptor = None
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def __exit__(self, _type: Any, _value: Any, _tb: Any) -> bool:
        self.close()
        return False


class _GpuLease(_FlockLease):
    """Exclusive UUID lease that fails persistent quarantine closed."""

    def __init__(self, root: Path, gpu_uuid: str) -> None:
        key = hashlib.sha256(gpu_uuid.encode("utf-8")).hexdigest()
        super().__init__(
            root / f"gpu-{key}.lock",
            operation=fcntl.LOCK_EX,
            kind="gpu_uuid",
        )
        self.gpu_uuid = gpu_uuid
        self.quarantine_path = root / f"gpu-{key}.quarantine.json"
        self.finalized = False
        self.fallback_quarantine_persisted = False
        self.masked_poison_armed = False
        self._masked_poison_payload: bytes | None = None

    def _quarantine_entry_exists(self) -> bool:
        """Treat every directory entry, including dangling links, as poison."""

        try:
            self.quarantine_path.lstat()
        except FileNotFoundError:
            return False
        return True

    def __enter__(self) -> "_GpuLease":
        super().__enter__()
        assert self.descriptor is not None
        fallback_size = os.fstat(self.descriptor).st_size
        if self._quarantine_entry_exists() or fallback_size != 0:
            self.close()
            raise RuntimeError(
                "GPU is persistently quarantined; manual investigation and "
                "explicit marker removal/lock truncation are required: "
                f"{self.quarantine_path}"
            )
        assert self.record is not None
        self.record.update(
            {
                "gpu_uuid": self.gpu_uuid,
                "quarantine_path": str(self.quarantine_path),
                "preexisting_quarantine": False,
            }
        )
        return self

    def _fsync_lock_directory(self) -> None:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(self.path.parent, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _replace_lock_payload(self, payload: bytes) -> None:
        if self.descriptor is None:
            raise RuntimeError("GPU lock payload requires a held UUID lock")
        if payload:
            # Never create an empty crash window when replacing an already
            # armed poison with a fallback quarantine. Write+sync new bytes
            # over the nonempty old record before truncating stale tail data.
            offset = 0
            while offset < len(payload):
                written = os.pwrite(
                    self.descriptor,
                    payload[offset:],
                    offset,
                )
                if written <= 0:
                    raise OSError("short write to GPU lock poison record")
                offset += written
            os.fsync(self.descriptor)
            os.ftruncate(self.descriptor, len(payload))
            os.fsync(self.descriptor)
        else:
            # Emptying is reserved for the explicitly authorized clean-clear
            # path after all GPU safety and terminal conditions are durable.
            os.ftruncate(self.descriptor, 0)
            os.fsync(self.descriptor)
        self._fsync_lock_directory()

    def arm_masked_poison(self, *, run_id: str) -> None:
        """Durably poison the UUID before a masked child can exist."""

        if (
            not isinstance(run_id, str)
            or not run_id
            or "\x00" in run_id
        ):
            raise ValueError("masked poison run_id must be non-empty")
        if self.masked_poison_armed:
            raise RuntimeError("masked GPU poison is already armed")
        if self.descriptor is None or os.fstat(self.descriptor).st_size != 0:
            raise RuntimeError("masked GPU poison requires an empty held lock")
        payload = (
            canonical_json(
                {
                    "schema_version": (
                        GPU_MASKED_ARMED_POISON_SCHEMA_VERSION
                    ),
                    "armed_at_utc": _utc_now(),
                    "gpu_uuid": self.gpu_uuid,
                    "run_id": run_id,
                    "clear_policy": (
                        "only_after_clean_xid_drain_post_health_"
                        "reservation_and_terminal_evidence"
                    ),
                    "auto_clear_on_process_exit": False,
                }
            ).encode("utf-8")
            + b"\n"
        )
        self._replace_lock_payload(payload)
        self.masked_poison_armed = True
        self._masked_poison_payload = payload
        if self.record is not None:
            self.record["masked_poison_armed"] = True
            self.record["masked_poison_schema"] = (
                GPU_MASKED_ARMED_POISON_SCHEMA_VERSION
            )

    def clear_masked_poison(self) -> None:
        """Clear only this instance's exact armed record while lock is held."""

        if (
            self.descriptor is None
            or not self.masked_poison_armed
            or self._masked_poison_payload is None
        ):
            raise RuntimeError("masked GPU poison is not armed")
        if (
            self._quarantine_entry_exists()
            or self.fallback_quarantine_persisted
        ):
            raise RuntimeError("refusing to clear a quarantined GPU poison")
        size = os.fstat(self.descriptor).st_size
        observed = os.pread(self.descriptor, size, 0)
        if observed != self._masked_poison_payload:
            raise RuntimeError(
                "masked GPU poison changed before clean clearance"
            )
        self._replace_lock_payload(b"")
        self.masked_poison_armed = False
        self._masked_poison_payload = None
        if self.record is not None:
            self.record["masked_poison_cleared"] = True
            self.record["masked_poison_cleared_at_utc"] = _utc_now()

    def _persist_quarantine_in_lock(
        self,
        reasons: Sequence[str],
        marker_error: BaseException,
    ) -> None:
        """Persist a fallback poison record before releasing the UUID lock."""

        if self.descriptor is None:
            raise RuntimeError(
                "cannot persist fallback quarantine without the held GPU lock"
            )
        payload = (
            canonical_json(
                {
                    "schema_version": (
                        "burstserve.gpu-quarantine-lock-fallback/v1"
                    ),
                    "created_at_utc": _utc_now(),
                    "gpu_uuid": self.gpu_uuid,
                    "reasons": list(reasons),
                    "marker_path": str(self.quarantine_path),
                    "marker_error": (
                        f"{type(marker_error).__name__}: {marker_error}"
                    ),
                    "auto_clear_permitted": False,
                }
            ).encode("utf-8")
            + b"\n"
        )
        self._replace_lock_payload(payload)
        self.fallback_quarantine_persisted = True
        if self.record is not None:
            self.record["fallback_quarantine_written"] = True

    def quarantine(self, reasons: Sequence[str], **evidence: Any) -> None:
        normalized = sorted(
            {
                str(reason)
                for reason in reasons
                if isinstance(reason, str) and reason
            }
        )
        if not normalized:
            raise ValueError("quarantine requires at least one reason")
        try:
            write_json_atomic(
                self.quarantine_path,
                {
                    "schema_version": GPU_QUARANTINE_SCHEMA_VERSION,
                    "created_at_utc": _utc_now(),
                    "gpu_uuid": self.gpu_uuid,
                    "reasons": normalized,
                    "evidence": evidence,
                    "auto_clear_permitted": False,
                },
            )
        except BaseException as marker_error:
            self._persist_quarantine_in_lock(normalized, marker_error)
            raise
        if self.record is not None:
            self.record["quarantine_written"] = True
            self.record["quarantine_reasons"] = normalized

    def mark_terminal(self) -> None:
        self.finalized = True
        if self.record is not None:
            self.record["terminal_artifact_written"] = True

    def __exit__(self, exc_type: Any, value: Any, tb: Any) -> bool:
        if not self.finalized and self.descriptor is not None:
            try:
                self.quarantine(
                    ["supervisor_exited_without_terminal_artifact"],
                    exception_type=(
                        exc_type.__name__ if exc_type is not None else None
                    ),
                    exception_message=str(value) if value is not None else None,
                )
            except BaseException as marker_error:
                if not self.fallback_quarantine_persisted:
                    # Neither the atomic marker nor its held-lock fallback is
                    # durable.  Retain the raw flock descriptor until process
                    # exit and surface this fail-safe condition instead of
                    # admitting another formal owner.
                    raise RuntimeError(
                        "GPU quarantine could not be persisted; UUID lock "
                        "intentionally remains held"
                    ) from marker_error
                # The fallback poison record is durable and future leases
                # reject it. Preserve an active exception; otherwise surface
                # the loss of the canonical quarantine marker.
                if exc_type is None:
                    super().__exit__(exc_type, value, tb)
                    raise
        return super().__exit__(exc_type, value, tb)


class _TerminalArtifactGuard:
    """Last-resort terminal evidence for any supervisor escape path."""

    def __init__(self, run_directory: Path, run_id: str) -> None:
        self.run_directory = run_directory
        self.run_id = run_id
        self.finalized = False

    def mark_terminal(self) -> None:
        self.finalized = True

    def __enter__(self) -> "_TerminalArtifactGuard":
        return self

    def __exit__(self, exc_type: Any, value: Any, tb: Any) -> bool:
        outcome_path = self.run_directory / "outcome.json"
        if not self.finalized:
            prior_value: dict[str, Any] | None = None
            prior_summary: dict[str, Any] = {
                "outcome_existed": outcome_path.exists(),
                "sha256": None,
                "parse_error": None,
            }
            if outcome_path.exists():
                try:
                    prior_bytes = outcome_path.read_bytes()
                    prior_summary["sha256"] = hashlib.sha256(
                        prior_bytes
                    ).hexdigest()
                    parsed = json.loads(prior_bytes.decode("utf-8"))
                    if isinstance(parsed, dict):
                        prior_value = parsed
                        prior_summary.update(
                            {
                                field: parsed.get(field)
                                for field in (
                                    "schema_version",
                                    "completed_at_utc",
                                    "exit_code",
                                    "process_exit_code",
                                    "local_probe_passed",
                                    "accepted",
                                    "quarantine_required",
                                    "quarantine_reasons",
                                )
                            }
                        )
                    else:
                        prior_summary["parse_error"] = (
                            "prior outcome was not a JSON object"
                        )
                except BaseException as error:
                    prior_summary["parse_error"] = (
                        f"{type(error).__name__}: {error}"
                    )
            stdout_path = self.run_directory / "stdout.log"
            stderr_path = self.run_directory / "stderr.log"
            if not stdout_path.exists():
                write_text_atomic(stdout_path, "")
            if not stderr_path.exists():
                write_text_atomic(
                    stderr_path,
                    "supervisor escaped before normal terminal evidence\n",
                )
            downgraded = dict(prior_value or {})
            existing_reasons = downgraded.get("quarantine_reasons")
            reasons = (
                [
                    str(reason)
                    for reason in existing_reasons
                    if isinstance(reason, str) and reason
                ]
                if isinstance(existing_reasons, list)
                else []
            )
            reasons.append("supervisor_exited_without_terminal_artifact")
            if isinstance(value, KeyboardInterrupt):
                exit_code = 130
            elif isinstance(value, _ChildWindowInterrupted):
                exit_code = 128 + value.signum
            elif isinstance(value, SystemExit):
                exit_code = _normalize_exit_code(value.code)
            else:
                exit_code = 1
            downgraded.update(
                {
                    "schema_version": OUTCOME_SCHEMA_VERSION,
                    "completed_at_utc": _utc_now(),
                    "exit_code": _normalize_exit_code(exit_code),
                    "process_exit_code": downgraded.get(
                        "process_exit_code"
                    ),
                    "timed_out": bool(downgraded.get("timed_out", False)),
                    "child_launch_error": (
                        f"{exc_type.__name__}: {value}"
                        if exc_type is not None
                        else "supervisor exited without terminal artifact"
                    ),
                    "quarantine_required": True,
                    "quarantine_reasons": sorted(set(reasons)),
                    "local_probe_passed": False,
                    "accepted": False,
                    "minimal_terminal_artifact": True,
                    "prior_terminal_summary": prior_summary,
                }
            )
            write_json_atomic(outcome_path, downgraded)
        if value is not None:
            try:
                if not isinstance(
                    getattr(value, "burstserve_run_directory", None),
                    Path,
                ):
                    setattr(
                        value,
                        "burstserve_run_directory",
                        self.run_directory,
                    )
                # The normal terminal path may already have downgraded a
                # locally successful run and attached its durable exit code.
                # This outer guard supplies only a last-resort default; it
                # must never overwrite the more precise terminal decision.
                if not hasattr(value, "burstserve_exit_code"):
                    setattr(value, "burstserve_exit_code", 1)
            except BaseException:
                pass
        return False


def _parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _normalize_exit_code(value: Any, *, default: int = 1) -> int:
    """Map child/supervisor status to a stable shell-compatible byte."""

    if isinstance(value, bool) or not isinstance(value, int):
        return max(0, min(255, default))
    if value < 0:
        signal_number = -value
        return min(255, 128 + signal_number)
    return min(255, value)


def validate_reservation_evidence(
    value: Any,
    *,
    physical_gpu: int,
    gpu_uuid: str,
    now: datetime | None = None,
    required_horizon_s: float = 0.0,
) -> tuple[dict[str, bool], bool]:
    evidence = value if isinstance(value, Mapping) else {}
    current = now or datetime.now(timezone.utc)
    horizon_valid = (
        isinstance(required_horizon_s, (int, float))
        and not isinstance(required_horizon_s, bool)
        and math.isfinite(float(required_horizon_s))
        and float(required_horizon_s) >= 0
    )
    required_until = (
        current + timedelta(seconds=float(required_horizon_s))
        if horizon_valid
        else None
    )
    valid_from = _parse_utc(evidence.get("valid_from_utc"))
    valid_until = _parse_utc(evidence.get("valid_until_utc"))
    checks = {
        "reservation_schema_exact": (
            evidence.get("schema_version")
            == GPU_RESERVATION_SCHEMA_VERSION
        ),
        "reservation_status_active": evidence.get("status") == "active",
        "reservation_gpu_uuid_exact": evidence.get("gpu_uuid") == gpu_uuid,
        "reservation_physical_gpu_exact": (
            isinstance(evidence.get("physical_gpu"), int)
            and not isinstance(evidence.get("physical_gpu"), bool)
            and evidence.get("physical_gpu") == physical_gpu
        ),
        "reservation_identity_recorded": (
            isinstance(evidence.get("reservation_id"), str)
            and bool(evidence.get("reservation_id"))
            and isinstance(evidence.get("owner"), str)
            and bool(evidence.get("owner"))
        ),
        "reservation_valid_from_parseable": valid_from is not None,
        "reservation_valid_until_parseable": valid_until is not None,
        "reservation_started": (
            valid_from is not None and valid_from <= current
        ),
        "reservation_not_expired": (
            valid_until is not None and current < valid_until
        ),
        "reservation_interval_ordered": (
            valid_from is not None
            and valid_until is not None
            and valid_from < valid_until
        ),
        "reservation_horizon_is_valid": horizon_valid,
        "reservation_covers_required_horizon": (
            valid_until is not None
            and required_until is not None
            and valid_until >= required_until
        ),
    }
    return checks, all(checks.values())


def _send_process_group_signal(
    process: subprocess.Popen[str],
    signum: int,
) -> str | None:
    if process.returncode is not None:
        return (
            "refused to signal a process group after its leader was reaped; "
            "PGID reuse cannot be excluded"
        )
    try:
        os.killpg(process.pid, signum)
        return None
    except ProcessLookupError:
        return None
    except OSError as error:
        if signum == signal.SIGKILL:
            try:
                process.kill()
                return None
            except BaseException as fallback:
                return (
                    f"group signal {signum} failed: {type(error).__name__}: "
                    f"{error}; direct kill failed: {type(fallback).__name__}: "
                    f"{fallback}"
                )
        return (
            f"group signal {signum} failed: {type(error).__name__}: {error}"
        )


def _process_group_exists(process_group_id: int) -> tuple[bool, str | None]:
    try:
        os.killpg(process_group_id, 0)
        return True, None
    except ProcessLookupError:
        return False, None
    except OSError as error:
        return True, (
            f"cannot verify process group {process_group_id}: "
            f"{type(error).__name__}: {error}"
        )


def _wait_for_process_group_exit(
    process_group_id: int,
    *,
    timeout_s: float,
) -> tuple[bool, str | None]:
    deadline = time.monotonic() + max(0.0, timeout_s)
    last_error: str | None = None
    while True:
        exists, error = _process_group_exists(process_group_id)
        if not exists:
            return True, last_error
        if error is not None:
            last_error = error
        if time.monotonic() >= deadline:
            return False, last_error
        time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))


def _terminate_and_reap_process_group(
    process: subprocess.Popen[str],
) -> dict[str, Any]:
    """Best-effort bounded cleanup without trusting inherited SIGTERM masks."""

    errors: list[str] = []
    pending_base_exception: BaseException | None = None
    stdout = ""
    stderr = ""
    term_error = _send_process_group_signal(process, signal.SIGTERM)
    if term_error:
        errors.append(term_error)
    # HUP/TERM are blocked around Popen so the process handle cannot be lost.
    # Until a dedicated exec launcher clears that inherited mask, SIGTERM is
    # advisory only and must be followed by SIGKILL without a grace wait.
    kill_error = _send_process_group_signal(process, signal.SIGKILL)
    if kill_error:
        errors.append(kill_error)
    try:
        stdout, stderr = process.communicate(
            timeout=FINAL_REAP_TIMEOUT_S
        )
    except subprocess.TimeoutExpired:
        errors.append(
            "child did not reap after SIGKILL within "
            f"{FINAL_REAP_TIMEOUT_S}s"
        )
    except BaseException as error:
        if not isinstance(error, Exception):
            pending_base_exception = error
        errors.append(
            f"communicate after SIGKILL failed: {type(error).__name__}: {error}"
        )

    child_reaped = process.returncode is not None
    # Never address the numeric PGID after wait/communicate reaped its leader:
    # the kernel may already have reused it for an unrelated process. A
    # timeout path is conservatively quarantined unless a later waitid-based
    # supervisor proved group quiescence while retaining the zombie leader.
    group_quiesced = False
    errors.append(
        "group quiescence was not proven before leader reap; no post-reap "
        "PGID signal was attempted"
    )
    process_group_reaped = child_reaped and group_quiesced
    if not process_group_reaped:
        errors.append("process group could not be fully reaped and quiesced")
    return {
        "stdout": stdout or "",
        "stderr": stderr or "",
        "child_reaped": child_reaped,
        "process_group_quiesced": group_quiesced,
        "process_group_reaped": process_group_reaped,
        "errors": errors,
        "pending_base_exception": pending_base_exception,
    }


def _verify_completed_process_group(
    process: subprocess.Popen[str],
) -> dict[str, Any]:
    """Ensure a nominally completed child left no process-group descendants."""

    child_reaped = process.returncode is not None
    # This compatibility path intentionally performs no PGID operation. New
    # formal execution uses a waitid(WNOWAIT) supervisor; fake-process tests
    # that take this path cannot prove descendants and therefore fail closed.
    group_quiesced = False
    process_group_reaped = False
    errors = [
        "legacy wait path cannot prove group quiescence before leader reap; "
        "post-reap PGID operations are forbidden"
    ]
    return {
        "stdout": "",
        "stderr": "",
        "child_reaped": child_reaped,
        "process_group_quiesced": group_quiesced,
        "process_group_reaped": process_group_reaped,
        "errors": errors,
        "pending_base_exception": None,
    }


def _read_proc_process_identity(pid: int) -> dict[str, int | str]:
    """Read a Linux process identity suitable for safe PGID operations."""

    content = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    close = content.rfind(")")
    if close < 0:
        raise RuntimeError(f"malformed /proc/{pid}/stat")
    prefix = content[: close + 1]
    open_index = prefix.find("(")
    fields = content[close + 2 :].split()
    if open_index < 0 or len(fields) < 20:
        raise RuntimeError(f"malformed /proc/{pid}/stat")
    # After comm, fields begin at documented field 3 (state).
    return {
        "pid": pid,
        "comm": prefix[open_index + 1 : -1],
        "state": fields[0],
        "ppid": int(fields[1]),
        "process_group_id": int(fields[2]),
        "session_id": int(fields[3]),
        "starttime_ticks": int(fields[19]),
    }


def _capture_process_identity(pid: int) -> dict[str, int | str]:
    identity = _read_proc_process_identity(pid)
    if (
        identity["process_group_id"] != pid
        or identity["session_id"] != pid
    ):
        raise RuntimeError(
            "start_new_session child did not become its own session/group "
            f"leader: {identity}"
        )
    return identity


def _identity_still_matches(expected: Mapping[str, Any]) -> bool:
    try:
        observed = _read_proc_process_identity(int(expected["pid"]))
    except (OSError, RuntimeError, KeyError, TypeError, ValueError):
        return False
    return all(
        observed.get(field) == expected.get(field)
        for field in (
            "pid",
            "process_group_id",
            "session_id",
            "starttime_ticks",
        )
    )


def _waitid_wnowait(pid: int, timeout_s: float) -> bool:
    """Return true once the leader is waitable, leaving it unreaped."""

    deadline = time.monotonic() + max(0.0, timeout_s)
    options = os.WEXITED | os.WNOHANG | os.WNOWAIT
    while True:
        result = os.waitid(os.P_PID, pid, options)
        if result is not None and int(result.si_pid) == pid:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))


def _scan_bound_process_group(
    expected: Mapping[str, Any],
) -> list[dict[str, int | str]]:
    """List descendants while the zombie leader pins the numeric PGID."""

    members: list[dict[str, int | str]] = []
    leader_pid = int(expected["pid"])
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        pid = int(entry.name)
        try:
            identity = _read_proc_process_identity(pid)
        except (OSError, RuntimeError, ValueError):
            continue
        if pid == leader_pid:
            if not all(
                identity.get(field) == expected.get(field)
                for field in (
                    "pid",
                    "process_group_id",
                    "session_id",
                    "starttime_ticks",
                )
            ):
                raise RuntimeError("leader identity changed before final reap")
            continue
        if (
            identity["process_group_id"] == leader_pid
            or identity["session_id"] == leader_pid
        ):
            members.append(identity)
    return members


def _signal_bound_process_group(
    process: subprocess.Popen[str],
    expected: Mapping[str, Any],
    signum: int,
) -> str | None:
    """Signal only while the captured leader identity still pins the PGID."""

    if process.returncode is not None:
        return "refused bound group signal after leader reap"
    if not _identity_still_matches(expected):
        return "refused bound group signal after leader identity mismatch"
    try:
        os.killpg(int(expected["process_group_id"]), signum)
    except ProcessLookupError:
        return None
    except OSError as error:
        return f"bound group signal failed: {type(error).__name__}: {error}"
    return None


def _supervise_process(
    process: subprocess.Popen[str],
    identity: Mapping[str, Any],
    *,
    timeout_s: float,
) -> dict[str, Any]:
    """Retain the zombie leader until its complete session is quiescent."""

    errors: list[str] = []
    pending: BaseException | None = None

    def remember(operation: str, error: BaseException) -> None:
        nonlocal pending
        errors.append(f"{operation} failed: {type(error).__name__}: {error}")
        if pending is None and not isinstance(error, Exception):
            pending = error

    def waitid_safely(timeout: float, operation: str) -> bool:
        try:
            return _waitid_wnowait(process.pid, timeout)
        except BaseException as error:
            remember(operation, error)
            return False

    def signal_safely(signum: int, operation: str) -> None:
        try:
            error = _signal_bound_process_group(process, identity, signum)
        except BaseException as signal_error:
            remember(operation, signal_error)
            return
        if error:
            errors.append(error)

    leader_waitable = waitid_safely(timeout_s, "initial waitid(WNOWAIT)")
    timed_out = not leader_waitable
    if timed_out:
        signal_safely(signal.SIGTERM, "bound group SIGTERM")
        # The launcher clears the inherited signal mask, but a bounded run
        # cannot rely on cooperative termination.
        signal_safely(signal.SIGKILL, "bound group SIGKILL")
        leader_waitable = waitid_safely(
            FINAL_REAP_TIMEOUT_S,
            "post-SIGKILL waitid(WNOWAIT)",
        )
        if not leader_waitable:
            errors.append("leader did not become waitable after SIGKILL")

    descendants: list[dict[str, int | str]] = []
    identity_matches = False
    if leader_waitable:
        try:
            identity_matches = _identity_still_matches(identity)
        except BaseException as error:
            remember("leader identity verification", error)
    if leader_waitable and identity_matches:
        try:
            descendants = _scan_bound_process_group(identity)
        except BaseException as error:
            remember("bound group scan", error)
        if descendants:
            # The zombie leader still exists, so this PGID cannot have been
            # reused. Kill the complete bound group before the final reap.
            try:
                os.killpg(int(identity["process_group_id"]), signal.SIGKILL)
            except ProcessLookupError:
                pass
            except BaseException as error:
                remember("descendant SIGKILL", error)
            deadline = time.monotonic() + FINAL_REAP_TIMEOUT_S
            while time.monotonic() < deadline:
                try:
                    remaining = _scan_bound_process_group(identity)
                except BaseException as error:
                    remember("bound group rescan", error)
                    break
                if not remaining:
                    descendants = []
                    break
                descendants = remaining
                try:
                    time.sleep(0.01)
                except BaseException as error:
                    remember("bound group reap wait", error)
                    # Continue cleanup after recording the first interruption.
            if descendants:
                errors.append("bound session descendants did not exit")
    elif leader_waitable:
        errors.append("leader identity changed before bound group scan")

    try:
        return_code = process.wait(timeout=FINAL_REAP_TIMEOUT_S)
    except BaseException as error:
        return_code = None
        remember("final reap", error)
    quiescent = return_code is not None and not descendants and not errors
    return {
        "timed_out": timed_out,
        "return_code": return_code,
        "child_reaped": return_code is not None,
        "process_group_quiesced": quiescent,
        "process_group_reaped": quiescent,
        "descendants_after_waitable": descendants,
        "errors": errors,
        "pending_base_exception": pending,
        "identity": dict(identity),
        "wait_strategy": "waitid(WNOWAIT)+bound-session-scan+final-wait",
    }


def _reap_spawn_without_identity(
    process: subprocess.Popen[str],
) -> dict[str, Any]:
    """Boundedly reap a spawned child when its session identity is unknown.

    This path is used only if the first post-``Popen`` evidence write or the
    ``/proc`` identity capture fails.  Addressing ``process.pid`` through the
    still-live ``Popen`` object is safe; addressing its numeric PGID is not,
    because we never proved that the child became the requested session
    leader.  Consequently this path always fails group-quiescence closed and
    forces persistent GPU quarantine even if the direct child is reaped.
    """

    errors = [
        "spawned child identity was not captured; numeric PGID operations "
        "are forbidden and descendant quiescence cannot be proven"
    ]
    pending: BaseException | None = None
    for attempt in range(2):
        try:
            if process.returncode is None:
                process.kill()
            break
        except ProcessLookupError:
            # The child may have exited between Popen and the direct kill. A
            # bounded wait below still establishes direct-child reap.
            break
        except BaseException as error:
            errors.append(
                "direct child SIGKILL "
                f"attempt {attempt + 1} failed: "
                f"{type(error).__name__}: {error}"
            )
            if pending is None and not isinstance(error, Exception):
                pending = error

    try:
        return_code = process.wait(timeout=FINAL_REAP_TIMEOUT_S)
    except BaseException as error:
        return_code = None
        errors.append(
            f"direct child final reap failed: {type(error).__name__}: {error}"
        )
        if pending is None and not isinstance(error, Exception):
            pending = error
        # A signal-handler interruption may have aborted wait before it could
        # reap. Retry once after a direct, PID-bound kill.
        try:
            if process.returncode is None:
                process.kill()
            return_code = process.wait(timeout=FINAL_REAP_TIMEOUT_S)
        except BaseException as retry_error:
            errors.append(
                "direct child retry reap failed: "
                f"{type(retry_error).__name__}: {retry_error}"
            )
            if pending is None and not isinstance(retry_error, Exception):
                pending = retry_error

    return {
        "timed_out": False,
        "return_code": return_code,
        "child_reaped": return_code is not None,
        "process_group_quiesced": False,
        "process_group_reaped": False,
        "descendants_after_waitable": [],
        "errors": errors,
        "pending_base_exception": pending,
        "identity": None,
        "wait_strategy": "direct-child-kill+wait;group-untrusted",
    }


def capture_formal_git_snapshot(
    repo_root: Path,
    *,
    expected_libsmctrl_commit: str,
) -> RepositorySnapshot:
    """Capture the raw main tree and registered libsmctrl gitlink safely."""

    return capture_repository(
        repo_root.resolve(),
        expected_gitlinks={
            DEFAULT_LIBSMCTRL_ROOT.as_posix(): expected_libsmctrl_commit,
        },
        allowed_untracked_roots=FORMAL_GIT_ALLOWED_UNTRACKED_ROOTS,
        allow_untracked_regular_files=False,
        git=TRUSTED_GIT_EXECUTABLE,
    )


def source_revision(
    repo_root: Path,
    libsmctrl_root: Path,
    *,
    expected_libsmctrl_commit: str,
) -> str:
    """Return an identity containing both BurstServe and libsmctrl revisions."""

    if not _valid_git_oid(expected_libsmctrl_commit):
        raise RuntimeError("expected libsmctrl commit is not a full Git OID")
    if libsmctrl_root.resolve() != (
        repo_root.resolve() / DEFAULT_LIBSMCTRL_ROOT
    ):
        return "burstserve-state-unavailable;libsmctrl-noncanonical"
    snapshot = capture_formal_git_snapshot(
        repo_root,
        expected_libsmctrl_commit=expected_libsmctrl_commit,
    )
    link = next(
        (
            item
            for item in snapshot.gitlinks
            if item.path == os.fsencode(DEFAULT_LIBSMCTRL_ROOT.as_posix())
        ),
        None,
    )
    main_head = snapshot.head_oid or "unavailable"
    main_state = (
        "raw-clean"
        if snapshot.clean
        else ("raw-nonclean" if snapshot.complete else "raw-incomplete")
    )
    lib_head = (
        link.snapshot.head_oid
        if link is not None and link.snapshot.head_oid is not None
        else "unavailable"
    )
    lib_state = (
        "raw-clean"
        if link is not None and link.clean
        else (
            "raw-nonclean"
            if link is not None and link.snapshot.complete
            else "raw-incomplete"
        )
    )
    return (
        f"burstserve-{main_head}+{main_state}-"
        f"{snapshot.identity_sha256[:16]};"
        f"libsmctrl-{lib_head}+{lib_state}"
    )


def _status_identity(status: os.stat_result) -> dict[str, int]:
    return {
        "device": int(status.st_dev),
        "inode": int(status.st_ino),
        "mode": int(status.st_mode),
        "uid": int(status.st_uid),
        "gid": int(status.st_gid),
        "nlink": int(status.st_nlink),
        "size": int(status.st_size),
        "mtime_ns": int(status.st_mtime_ns),
        "ctime_ns": int(status.st_ctime_ns),
    }


def _trusted_libcuda_directory_records() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for directory in (
        Path("/usr"),
        Path("/usr/lib"),
        TRUSTED_LIBCUDA_DIRECTORY,
    ):
        status = directory.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(status.st_mode)
            or status.st_uid != 0
            or stat.S_IMODE(status.st_mode) & 0o022
        ):
            raise RuntimeError(
                f"libcuda trusted directory is not root-owned and "
                f"non-writable: {directory}"
            )
        records.append(
            {
                "path": str(directory),
                **_status_identity(status),
            }
        )
    return records


def _open_verified_libcuda(
    link_path: Path = DEFAULT_LIBCUDA_LINK,
) -> tuple[int, dict[str, Any]]:
    """Open the fixed system libcuda target and bind link plus inode identity."""

    if link_path != DEFAULT_LIBCUDA_LINK or not link_path.is_absolute():
        raise RuntimeError(
            f"formal libcuda path must be {DEFAULT_LIBCUDA_LINK}"
        )
    directory_records = _trusted_libcuda_directory_records()
    link_before = os.lstat(link_path)
    if not stat.S_ISLNK(link_before.st_mode) or link_before.st_uid != 0:
        raise RuntimeError("formal libcuda link is not a root-owned symlink")
    link_target = os.readlink(link_path)
    if not link_target or "\x00" in link_target:
        raise RuntimeError("formal libcuda symlink target is invalid")
    resolved = link_path.resolve(strict=True)
    if resolved.parent != TRUSTED_LIBCUDA_DIRECTORY:
        raise RuntimeError(
            "formal libcuda target escaped the trusted library directory"
        )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(resolved, flags)
    try:
        target_before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(target_before.st_mode)
            or target_before.st_uid != 0
            or stat.S_IMODE(target_before.st_mode) & 0o022
            or target_before.st_nlink != 1
            or target_before.st_size <= 0
        ):
            raise RuntimeError(
                "formal libcuda target is not a root-owned, immutable-shape "
                "regular file"
            )
        digest = _sha256_fd(descriptor)
        target_after = os.fstat(descriptor)
        link_after = os.lstat(link_path)
        if (
            _status_identity(target_before) != _status_identity(target_after)
            or _status_identity(link_before) != _status_identity(link_after)
            or os.readlink(link_path) != link_target
        ):
            raise RuntimeError(
                "formal libcuda link or target changed during inspection"
            )
        identity = {
            "link_path": str(link_path),
            "link_target": link_target,
            "link_identity": _status_identity(link_before),
            "resolved_path": str(resolved),
            "target_identity": {
                **_status_identity(target_before),
                "sha256": digest,
            },
            "trusted_directories": directory_records,
        }
        return descriptor, identity
    except BaseException:
        os.close(descriptor)
        raise


def inspect_cuda_driver_library() -> dict[str, Any]:
    descriptor, identity = _open_verified_libcuda()
    os.close(descriptor)
    return identity


def query_cuda_driver() -> dict[str, Any]:
    """Read the driver version from one verified absolute libcuda inode."""

    descriptor, identity = _open_verified_libcuda()
    load_path = f"/proc/self/fd/{descriptor}"
    try:
        try:
            cuda = ctypes.CDLL(
                load_path,
                mode=(
                    int(getattr(os, "RTLD_NOW", 2))
                    | int(getattr(os, "RTLD_LOCAL", 0))
                ),
            )
        except OSError as exc:
            raise RuntimeError(
                f"cannot load verified libcuda FD {load_path}: {exc}"
            ) from exc
        version = ctypes.c_int()
        get_version = cuda.cuDriverGetVersion
        get_version.argtypes = [ctypes.POINTER(ctypes.c_int)]
        get_version.restype = ctypes.c_int
        status = int(get_version(ctypes.byref(version)))
        if status != 0:
            raise RuntimeError(
                f"cuDriverGetVersion failed with CUDA status {status}"
            )
        if version.value <= 0:
            raise RuntimeError(
                f"cuDriverGetVersion returned {version.value}"
            )
        return {
            "version": int(version.value),
            "library_identity": identity,
            "load_path": load_path,
            "load_binding": "verified /proc/self/fd descriptor",
            "creates_cuda_context": False,
            "python_pre_main_threat_boundary": (
                FORMAL_LAUNCHER_THREAT_BOUNDARIES[
                    "python_pre_main_injection"
                ]
            ),
        }
    finally:
        os.close(descriptor)


def query_cuda_driver_version() -> int:
    """Compatibility wrapper around the identity-bound driver query."""

    return int(query_cuda_driver()["version"])


def revalidate_cuda_driver_library(
    expected_identity: Mapping[str, Any],
) -> dict[str, Any]:
    """Re-open the fixed libcuda path and compare its complete identity."""

    try:
        observed = inspect_cuda_driver_library()
        matches = observed == dict(expected_identity)
        return {
            "completed": True,
            "error": None,
            "expected_identity": dict(expected_identity),
            "observed_identity": observed,
            "matches_initial": matches,
            "passed": matches,
        }
    except Exception as error:
        return {
            "completed": False,
            "error": f"{type(error).__name__}: {error}",
            "expected_identity": dict(expected_identity),
            "observed_identity": None,
            "matches_initial": False,
            "passed": False,
        }


def evaluate_libcuda_build_binding(
    library_identity: Mapping[str, Any],
    formal_source_binding: Mapping[str, Any],
) -> dict[str, bool]:
    """Bind the runtime driver inode to the attested native build stamp."""

    build_stamp = formal_source_binding.get("build_stamp")
    build_stamp = (
        build_stamp if isinstance(build_stamp, Mapping) else {}
    )
    fields = build_stamp.get("fields")
    fields = fields if isinstance(fields, Mapping) else {}
    target = library_identity.get("target_identity")
    target = target if isinstance(target, Mapping) else {}
    return {
        "runtime_libcuda_build_stamp_fields_present": bool(fields),
        "runtime_libcuda_resolved_path_matches_build_stamp": (
            library_identity.get("resolved_path")
            == fields.get("LIBCUDA_LINK_LIBRARY")
        ),
        "runtime_libcuda_sha256_matches_build_stamp": (
            _valid_sha256(target.get("sha256"))
            and target.get("sha256")
            == fields.get("LIBCUDA_LINK_LIBRARY_SHA256")
        ),
        "runtime_libcuda_link_path_is_fixed": (
            library_identity.get("link_path")
            == str(DEFAULT_LIBCUDA_LINK)
        ),
        "runtime_libcuda_target_is_root_owned_regular": (
            target.get("uid") == 0
            and isinstance(target.get("mode"), int)
            and not isinstance(target.get("mode"), bool)
            and stat.S_ISREG(int(target["mode"]))
            and not stat.S_IMODE(int(target["mode"])) & 0o022
        ),
    }


def query_gpu(index: int) -> dict[str, Any]:
    """Return one physical GPU state via the fixed trusted nvidia-smi."""

    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise ValueError("physical GPU index must be a non-negative integer")
    result = subprocess.run(
        [
            str(TRUSTED_NVIDIA_SMI_EXECUTABLE),
            f"--id={index}",
            "--query-gpu=index,name,uuid,pci.bus_id,memory.total,memory.used,"
            "utilization.gpu,driver_version",
            "--format=csv,noheader,nounits",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        env=dict(TRUSTED_TOOL_ENVIRONMENT),
    )
    if result.returncode != 0:
        raise RuntimeError(f"nvidia-smi failed: {result.stderr.strip()}")
    rows = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(rows) != 1:
        raise RuntimeError(f"expected one GPU row, got {rows!r}")
    fields = [field.strip() for field in rows[0].split(",")]
    if len(fields) != 8:
        raise RuntimeError(f"unexpected nvidia-smi row: {rows[0]!r}")
    return {
        "index": int(fields[0]),
        "name": fields[1],
        "uuid": fields[2],
        "pci_bus_id": fields[3],
        "memory_total_mib": int(fields[4]),
        "memory_used_mib": int(fields[5]),
        "utilization_gpu_percent": int(fields[6]),
        "driver_version": fields[7],
    }


def query_compute_processes(gpu_uuid: str) -> list[dict[str, Any]]:
    """Return compute processes attached to one GPU UUID."""

    result = subprocess.run(
        [
            str(TRUSTED_NVIDIA_SMI_EXECUTABLE),
            "--query-compute-apps=gpu_uuid,pid,used_gpu_memory,process_name",
            "--format=csv,noheader,nounits",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        env=dict(TRUSTED_TOOL_ENVIRONMENT),
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"nvidia-smi compute-process query failed: {result.stderr.strip()}"
        )
    processes: list[dict[str, Any]] = []
    for row in result.stdout.splitlines():
        if not row.strip():
            continue
        fields = [field.strip() for field in row.split(",", maxsplit=3)]
        if len(fields) != 4:
            raise RuntimeError(f"unexpected compute-process row: {row!r}")
        if fields[0] != gpu_uuid:
            continue
        processes.append(
            {
                "gpu_uuid": fields[0],
                "pid": int(fields[1]),
                "used_gpu_memory_mib": int(fields[2]),
                "process_name": fields[3],
            }
        )
    return processes


def query_mps_processes() -> list[dict[str, Any]]:
    """Return host NVIDIA MPS control/server processes.

    Host-wide daemon presence is provenance, not proof that the isolated child
    is attached. Formal safety comes from the child's explicit empty
    ``CUDA_MPS_PIPE_DIRECTORY`` bypass.
    """

    result = subprocess.run(
        [str(TRUSTED_PS_EXECUTABLE), "-eo", "pid=,comm=,args="],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        env=dict(TRUSTED_TOOL_ENVIRONMENT),
    )
    if result.returncode != 0:
        raise RuntimeError(f"host process query failed: {result.stderr.strip()}")

    names = frozenset(
        {
            "nvidia-cuda-mps-control",
            "nvidia-cuda-mps-server",
        }
    )
    processes: list[dict[str, Any]] = []
    for row in result.stdout.splitlines():
        fields = row.strip().split(maxsplit=2)
        if len(fields) < 2:
            continue
        pid_text, command = fields[:2]
        arguments = fields[2] if len(fields) == 3 else command
        executable = Path(arguments.split(maxsplit=1)[0]).name
        if (
            command not in names
            and executable not in names
            and not command.startswith("nvidia-cuda-mps")
        ):
            continue
        try:
            pid = int(pid_text)
        except ValueError as exc:
            raise RuntimeError(f"unexpected host process row: {row!r}") from exc
        processes.append(
            {
                "pid": pid,
                "command": command,
                "arguments": arguments,
            }
        )
    return processes


def capture_final_launch_preflight(
    *,
    physical_gpu: int,
    expected_gpu_uuid: str,
    expected_lease_uuid: str,
    reservation_evidence: Any,
    mode: str,
    timeout_s: float,
    maximum_used_mib: int,
    allow_busy_gpu: bool,
    mps_pipe_directory: str,
) -> dict[str, Any]:
    """Revalidate mutable GPU state immediately before formal ``Popen``."""

    captured_at = datetime.now(timezone.utc)
    masked = mode in MASKED_MODES
    required_horizon_s = (
        (
            FINAL_PREFLIGHT_QUERY_BUDGET_S
            + float(timeout_s)
            + PROCESS_SUPERVISION_CLEANUP_BUDGET_S
            + MASKED_XID_TOTAL_BUDGET_MS / 1000.0
            + POST_HEALTH_QUERY_BUDGET_S
            + RESERVATION_SAFETY_MARGIN_S
        )
        if masked
        else 0.0
    )
    gpu: dict[str, Any] | None = None
    compute_processes: list[dict[str, Any]] = []
    mps_processes: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        observed = query_gpu(physical_gpu)
        if not isinstance(observed, Mapping):
            raise RuntimeError("GPU query did not return an object")
        gpu = dict(observed)
        index = gpu.get("index")
        uuid = gpu.get("uuid")
        used = gpu.get("memory_used_mib")
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or index < 0
        ):
            errors.append("query_gpu: GPU index is malformed")
        if not isinstance(uuid, str) or not uuid:
            errors.append("query_gpu: GPU UUID is malformed")
        if (
            isinstance(used, bool)
            or not isinstance(used, int)
            or used < 0
        ):
            errors.append("query_gpu: GPU memory_used_mib is malformed")
    except Exception as error:
        errors.append(f"query_gpu: {type(error).__name__}: {error}")

    observed_uuid = gpu.get("uuid") if gpu is not None else None
    if isinstance(observed_uuid, str) and observed_uuid:
        try:
            observed_processes = query_compute_processes(observed_uuid)
            if not isinstance(observed_processes, list) or any(
                not isinstance(item, Mapping)
                for item in observed_processes
            ):
                raise RuntimeError(
                    "compute process query returned a malformed record"
                )
            compute_processes = [
                dict(item) for item in observed_processes
            ]
        except Exception as error:
            errors.append(
                "query_compute_processes: "
                f"{type(error).__name__}: {error}"
            )
    else:
        errors.append("query_compute_processes: final GPU UUID unavailable")
    try:
        observed_mps_processes = query_mps_processes()
        if not isinstance(observed_mps_processes, list) or any(
            not isinstance(item, Mapping)
            for item in observed_mps_processes
        ):
            raise RuntimeError("MPS process query returned a malformed record")
        mps_processes = [dict(item) for item in observed_mps_processes]
    except Exception as error:
        errors.append(
            f"query_mps_processes: {type(error).__name__}: {error}"
        )

    if masked:
        reservation_checks, reservation_valid = (
            validate_reservation_evidence(
                reservation_evidence,
                physical_gpu=physical_gpu,
                gpu_uuid=str(observed_uuid or ""),
                now=captured_at,
                required_horizon_s=required_horizon_s,
            )
        )
    else:
        reservation_checks = {
            "reservation_not_required_for_baseline": True,
        }
        reservation_valid = True
    effective_busy_override = allow_busy_gpu and mode == "baseline"
    used = gpu.get("memory_used_mib") if gpu is not None else None
    memory_safe = (
        isinstance(used, int)
        and not isinstance(used, bool)
        and used >= 0
        and (used <= maximum_used_mib or effective_busy_override)
    )
    checks = {
        "health_queries_completed": not errors,
        "gpu_accessible": gpu is not None,
        "gpu_ordinal_exact": (
            gpu is not None
            and isinstance(gpu.get("index"), int)
            and not isinstance(gpu.get("index"), bool)
            and gpu.get("index") == physical_gpu
        ),
        "gpu_uuid_stable": observed_uuid == expected_gpu_uuid,
        "gpu_uuid_matches_held_lease": (
            observed_uuid == expected_lease_uuid
        ),
        "memory_safe_or_explicit_busy_baseline": memory_safe,
        "compute_processes_absent_or_explicit_busy_baseline": (
            not compute_processes or effective_busy_override
        ),
        "empty_mps_pipe_bypass_exact": mps_pipe_directory == "",
        **reservation_checks,
        "reservation_valid_for_complete_run_horizon": reservation_valid,
    }
    required_until = captured_at + timedelta(seconds=required_horizon_s)
    return {
        "captured_at_utc": captured_at.isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z"),
        "required_horizon_s": required_horizon_s,
        "required_until_utc": required_until.isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z"),
        "gpu": gpu,
        "compute_processes": compute_processes,
        "mps_processes": mps_processes,
        "errors": errors,
        "checks": checks,
        "passed": all(checks.values()),
    }


def _require_exact_object_keys(
    value: Any,
    expected: frozenset[str],
    *,
    label: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimeError(f"{label} must be a JSON object")
    if set(value) != expected:
        raise RuntimeError(f"{label} keys do not match the v2 schema")
    return value


def _validate_gate_manifest_schema(content: Any) -> Mapping[str, Any]:
    manifest = _require_exact_object_keys(
        content,
        GATE_MANIFEST_TOP_LEVEL_KEYS,
        label="Gate-A manifest",
    )
    if manifest.get("schema_version") != GATE_MANIFEST_SCHEMA_VERSION:
        raise RuntimeError(
            "unsupported Gate-A manifest schema: "
            f"{manifest.get('schema_version')!r}"
        )
    _require_exact_object_keys(
        manifest.get("hardware"),
        GATE_MANIFEST_HARDWARE_KEYS,
        label="Gate-A manifest hardware",
    )
    source = _require_exact_object_keys(
        manifest.get("source"),
        GATE_MANIFEST_SOURCE_KEYS,
        label="Gate-A manifest source",
    )
    if not _valid_git_oid(source.get("libsmctrl_commit")):
        raise RuntimeError(
            "Gate-A manifest source.libsmctrl_commit must be a lowercase "
            "full Git object ID"
        )
    if source.get("libsmctrl_metadata") != DEFAULT_SOURCE_METADATA.as_posix():
        raise RuntimeError(
            "Gate-A manifest source.libsmctrl_metadata is not canonical"
        )
    for field in (
        "approved_launcher_sha256",
        "approved_real_probe_sha256",
        "approved_build_stamp_sha256",
        "approved_build_attestation_sha256",
    ):
        if not _valid_sha256(source.get(field)):
            raise RuntimeError(
                f"Gate-A manifest source.{field} must be SHA-256"
            )
    safety = _require_exact_object_keys(
        manifest.get("safety"),
        GATE_MANIFEST_SAFETY_KEYS,
        label="Gate-A manifest safety",
    )
    _require_exact_object_keys(
        manifest.get("baseline"),
        GATE_MANIFEST_BASELINE_KEYS,
        label="Gate-A manifest baseline",
    )
    _require_exact_object_keys(
        manifest.get("single_tpc_matrix_after_explicit_promotion"),
        GATE_MANIFEST_MATRIX_KEYS,
        label="Gate-A manifest single-TPC matrix",
    )
    _require_exact_object_keys(
        safety.get("xid_monitoring"),
        GATE_MANIFEST_XID_KEYS,
        label="Gate-A manifest Xid monitoring",
    )
    reservation = safety.get("exclusive_reservation_evidence")
    if reservation is not None:
        _require_exact_object_keys(
            reservation,
            GATE_MANIFEST_RESERVATION_KEYS,
            label="Gate-A manifest reservation evidence",
        )
    requirements = manifest.get("promotion_requirements")
    if (
        not isinstance(requirements, list)
        or not requirements
        or any(
            not isinstance(item, str) or not item
            for item in requirements
        )
    ):
        raise RuntimeError(
            "Gate-A manifest promotion_requirements must be non-empty strings"
        )
    return manifest


def load_gate_manifest_record(
    path: Path,
    *,
    repo_root: Path,
) -> dict[str, Any]:
    """Load a Gate-A manifest and bind its canonical content to the run ID."""

    resolved = path.resolve()
    manifest_root = (repo_root / "experiments" / "manifests").resolve()
    try:
        relative_to_manifest_root = resolved.relative_to(manifest_root)
        relative_to_repo = resolved.relative_to(repo_root.resolve())
    except ValueError as exc:
        raise RuntimeError(
            f"Gate-A manifest must be inside {manifest_root}: {resolved}"
        ) from exc
    if not relative_to_manifest_root.parts:
        raise RuntimeError("Gate-A manifest path must name a file")

    try:
        raw_content = _read_bounded_regular_bytes(
            resolved,
            label="Gate-A manifest",
            maximum_bytes=MAX_GATE_MANIFEST_BYTES,
        )
        parsed = _parse_strict_json_document(
            raw_content,
            label="Gate-A manifest",
            maximum_bytes=MAX_GATE_MANIFEST_BYTES,
            sort_keys=True,
            trailing_newline=True,
            require_canonical=False,
        )
        content = dict(_validate_gate_manifest_schema(parsed))
    except (OSError, RuntimeError) as exc:
        raise RuntimeError(f"invalid Gate-A manifest {path}: {exc}") from exc
    expected_commit = content["source"]["libsmctrl_commit"]
    register_libsmctrl = (
        repo_root.resolve() / DEFAULT_LIBSMCTRL_ROOT
    ).is_dir()
    snapshot = capture_repository(
        repo_root.resolve(),
        expected_gitlinks=(
            {
                DEFAULT_LIBSMCTRL_ROOT.as_posix(): (
                    expected_commit
                )
            }
            if register_libsmctrl
            else {}
        ),
        allowed_untracked_roots=FORMAL_GIT_ALLOWED_UNTRACKED_ROOTS,
        allow_untracked_regular_files=False,
        git=TRUSTED_GIT_EXECUTABLE,
    )
    if not snapshot.complete:
        raise RuntimeError(
            "could not verify Gate-A manifest with safe Git provenance: "
            + ";".join(snapshot.errors)
        )
    manifest_state = snapshot.path_state(relative_to_repo.as_posix())
    head_state = manifest_state.get("head")
    index_state = manifest_state.get("index")
    worktree_state = manifest_state.get("worktree")
    if (
        not isinstance(head_state, Mapping)
        or not isinstance(index_state, Mapping)
        or not isinstance(worktree_state, Mapping)
        or head_state.get("mode") not in {"100644", "100755"}
        or index_state.get("mode") != head_state.get("mode")
        or worktree_state.get("git_mode") != head_state.get("mode")
        or index_state.get("oid") != head_state.get("oid")
        or worktree_state.get("git_oid") != head_state.get("oid")
        or worktree_state.get("sha256")
        != hashlib.sha256(raw_content).hexdigest()
    ):
        raise RuntimeError(
            "Gate-A manifest raw worktree bytes must exactly match "
            f"HEAD and index: {relative_to_repo}"
        )
    git_blob = str(head_state["oid"])
    canonical = canonical_json(content)
    try:
        recorded_path = str(resolved.relative_to(repo_root.resolve()))
    except ValueError:  # pragma: no cover - constrained above.
        recorded_path = str(resolved)
    return {
        "path": recorded_path,
        "git_blob": git_blob,
        "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "content": content,
    }


def validate_gate_manifest_record(
    value: Any,
    *,
    repo_root: Path,
) -> Mapping[str, Any]:
    """Validate an embedded Gate-A manifest record and return its content."""

    if not isinstance(value, Mapping):
        raise RuntimeError("run config is missing its Gate-A manifest record")
    if set(value) != GATE_MANIFEST_RECORD_KEYS:
        raise RuntimeError("Gate-A manifest record keys do not match schema")
    content = value.get("content")
    digest = value.get("sha256")
    path_value = value.get("path")
    git_blob = value.get("git_blob")
    if (
        not isinstance(content, Mapping)
        or not isinstance(digest, str)
        or not isinstance(path_value, str)
        or not isinstance(git_blob, str)
    ):
        raise RuntimeError("Gate-A manifest record is malformed")
    content = _validate_gate_manifest_schema(content)
    git_blob_valid = (
        len(git_blob) in {40, 64}
        and git_blob == git_blob.lower()
        and all(character in "0123456789abcdef" for character in git_blob)
    )
    if not _valid_sha256(digest) or not git_blob_valid:
        raise RuntimeError("Gate-A manifest record digests are malformed")
    observed = hashlib.sha256(
        canonical_json(content).encode("utf-8")
    ).hexdigest()
    if observed != digest:
        raise RuntimeError("Gate-A manifest content does not match its SHA256")

    relative = Path(path_value)
    if relative.is_absolute():
        raise RuntimeError("Gate-A manifest record path must be repository-relative")
    resolved = (repo_root / relative).resolve()
    allowed_root = (repo_root / "experiments" / "manifests").resolve()
    try:
        resolved.relative_to(allowed_root)
    except ValueError as exc:
        raise RuntimeError(
            "Gate-A manifest record points outside experiments/manifests"
        ) from exc
    current_record = load_gate_manifest_record(resolved, repo_root=repo_root)
    if current_record["git_blob"] != git_blob:
        raise RuntimeError("Gate-A manifest Git blob does not match current HEAD")
    if current_record["sha256"] != digest:
        raise RuntimeError("Gate-A manifest file differs from embedded content")
    return content


def evaluate_gate_manifest_policy(
    manifest: Mapping[str, Any],
    *,
    mode: str,
    physical_gpu: int,
    gpu: Mapping[str, Any],
    driver_version: int,
    experimental_mask_off: int | None,
    timeout_s: float,
    maximum_used_mib: int,
    iterations: int,
    blocks: int,
    threads_per_block: int,
    trial: int,
    enabled_tpc: int,
    now: datetime | None = None,
) -> tuple[dict[str, bool], bool]:
    """Enforce the versioned Gate-A safety/promotion manifest."""

    hardware = manifest.get("hardware")
    safety = manifest.get("safety")
    baseline = manifest.get("baseline")
    if (
        not isinstance(hardware, Mapping)
        or not isinstance(safety, Mapping)
        or not isinstance(baseline, Mapping)
    ):
        raise RuntimeError(
            "Gate-A manifest hardware/safety/baseline sections are invalid"
        )

    masked = mode in MASKED_MODES
    matrix = manifest.get("single_tpc_matrix_after_explicit_promotion")
    matrix = matrix if isinstance(matrix, Mapping) else {}

    def unique_string_list(value: Any) -> tuple[list[str], bool]:
        if not isinstance(value, list) or not all(
            isinstance(item, str) and bool(item) for item in value
        ):
            return [], False
        return list(value), len(value) == len(set(value))

    def unique_integer_list(
        value: Any,
        *,
        minimum: int | None = None,
    ) -> tuple[list[int], bool]:
        if not isinstance(value, list) or not all(
            isinstance(item, int)
            and not isinstance(item, bool)
            and (minimum is None or item >= minimum)
            for item in value
        ):
            return [], False
        normalized = [int(item) for item in value]
        return normalized, len(normalized) == len(set(normalized))

    physical_gpu_indices, physical_gpu_indices_valid = unique_integer_list(
        hardware.get("physical_gpu_indices"),
        minimum=0,
    )
    approved_modes, approved_modes_valid = unique_string_list(
        safety.get("approved_mask_modes")
    )
    approved_modes_valid = approved_modes_valid and all(
        item in MASKED_MODES for item in approved_modes
    )
    reserved_uuids, reserved_uuids_valid = unique_string_list(
        safety.get("reserved_gpu_uuids")
    )
    stream_candidates, stream_candidates_valid = unique_integer_list(
        safety.get("stream_mask_off_candidates")
    )
    matrix_modes, matrix_modes_valid = unique_string_list(
        matrix.get("modes")
    )
    matrix_modes_valid = bool(matrix_modes) and matrix_modes_valid and all(
        item in MASKED_MODES for item in matrix_modes
    )
    matrix_tpc_bits, matrix_tpc_bits_valid = unique_integer_list(
        matrix.get("tpc_bits"),
        minimum=0,
    )
    matrix_tpc_bits_valid = bool(matrix_tpc_bits) and matrix_tpc_bits_valid
    expected_tpc_count = hardware.get("expected_tpc_count")
    expected_tpc_count_valid = (
        isinstance(expected_tpc_count, int)
        and not isinstance(expected_tpc_count, bool)
        and expected_tpc_count > 0
    )
    matrix_tpc_bits_in_range = (
        matrix_tpc_bits_valid
        and expected_tpc_count_valid
        and all(bit < expected_tpc_count for bit in matrix_tpc_bits)
    )
    matrix_allowed_sms, matrix_allowed_sms_valid = unique_integer_list(
        matrix.get("allowed_observed_sm_count"),
        minimum=1,
    )
    matrix_allowed_sms_valid = (
        bool(matrix_allowed_sms) and matrix_allowed_sms_valid
    )
    matrix_trials = matrix.get("trials_per_cell")
    matrix_trials_valid = (
        isinstance(matrix_trials, int)
        and not isinstance(matrix_trials, bool)
        and matrix_trials >= 3
    )
    baseline_trials = baseline.get("trials_per_gpu")
    baseline_trials_valid = (
        isinstance(baseline_trials, int)
        and not isinstance(baseline_trials, bool)
        and baseline_trials >= 3
    )
    minimum_coverage = baseline.get("minimum_sm_coverage_fraction")
    minimum_coverage_valid = (
        isinstance(minimum_coverage, (int, float))
        and not isinstance(minimum_coverage, bool)
        and float(minimum_coverage) >= BASELINE_MIN_SM_COVERAGE
        and float(minimum_coverage) <= 1.0
    )
    manifest_sm_count = hardware.get("sm_count")
    manifest_sm_count_valid = (
        isinstance(manifest_sm_count, int)
        and not isinstance(manifest_sm_count, bool)
        and manifest_sm_count > 0
    )
    canonical_tpc_bits = (
        [
            0,
            expected_tpc_count // 2 - 1,
            expected_tpc_count // 2,
            expected_tpc_count - 1,
        ]
        if expected_tpc_count_valid and expected_tpc_count >= 4
        else []
    )
    reservation_checks, reservation_valid = validate_reservation_evidence(
        safety.get("exclusive_reservation_evidence"),
        physical_gpu=physical_gpu,
        gpu_uuid=str(gpu.get("uuid", "")),
        now=now,
        required_horizon_s=(
            FINAL_PREFLIGHT_QUERY_BUDGET_S
            + float(timeout_s)
            + PROCESS_SUPERVISION_CLEANUP_BUDGET_S
            + MASKED_XID_TOTAL_BUDGET_MS / 1000.0
            + POST_HEALTH_QUERY_BUDGET_S
            + RESERVATION_SAFETY_MARGIN_S
        ),
    )
    xid_monitoring = safety.get("xid_monitoring")
    xid_monitoring = (
        xid_monitoring if isinstance(xid_monitoring, Mapping) else {}
    )
    source = manifest.get("source")
    source = source if isinstance(source, Mapping) else {}
    checks = {
        "manifest_schema_v2_exact": (
            manifest.get("schema_version") == GATE_MANIFEST_SCHEMA_VERSION
        ),
        "source_launcher_pin_valid": _valid_sha256(
            source.get("approved_launcher_sha256")
        ),
        "source_real_probe_pin_valid": _valid_sha256(
            source.get("approved_real_probe_sha256")
        ),
        "source_build_stamp_pin_valid": _valid_sha256(
            source.get("approved_build_stamp_sha256")
        ),
        "source_build_attestation_pin_valid": _valid_sha256(
            source.get("approved_build_attestation_sha256")
        ),
        "hardware_runtime_api_version_is_positive_integer": (
            isinstance(hardware.get("runtime_api_version"), int)
            and not isinstance(hardware.get("runtime_api_version"), bool)
            and hardware.get("runtime_api_version") > 0
        ),
        "baseline_threads_are_native_canonical": (
            baseline.get("threads_per_block") == PROBE_THREADS_PER_BLOCK
        ),
        "masked_threads_are_native_canonical": (
            matrix.get("threads_per_block") == PROBE_THREADS_PER_BLOCK
        ),
        "baseline_blocks_per_sm_is_positive_integer": (
            isinstance(baseline.get("blocks_per_sm"), int)
            and not isinstance(baseline.get("blocks_per_sm"), bool)
            and baseline.get("blocks_per_sm") > 0
        ),
        "masked_blocks_is_positive_integer": (
            isinstance(matrix.get("blocks"), int)
            and not isinstance(matrix.get("blocks"), bool)
            and matrix.get("blocks") > 0
        ),
        "masked_iterations_is_positive_integer": (
            isinstance(matrix.get("iterations"), int)
            and not isinstance(matrix.get("iterations"), bool)
            and matrix.get("iterations") > 0
        ),
        "physical_gpu_indices_are_valid_unique_integers": (
            physical_gpu_indices_valid
        ),
        "approved_mask_modes_are_valid_unique_strings": (
            approved_modes_valid
        ),
        "reserved_gpu_uuids_are_valid_unique_strings": (
            reserved_uuids_valid
        ),
        "stream_mask_off_candidates_are_valid_unique_integers": (
            stream_candidates_valid
        ),
        "single_tpc_matrix_modes_are_valid_unique_strings": (
            matrix_modes_valid
        ),
        "single_tpc_matrix_tpc_bits_are_valid_unique_integers": (
            matrix_tpc_bits_valid
        ),
        "single_tpc_matrix_tpc_bits_are_in_range": (
            matrix_tpc_bits_in_range
        ),
        "single_tpc_matrix_allowed_sm_counts_are_valid_unique_integers": (
            matrix_allowed_sms_valid
        ),
        "single_tpc_matrix_trials_per_cell_is_positive_integer": (
            matrix_trials_valid
        ),
        "baseline_trials_meet_minimum": baseline_trials_valid,
        "baseline_coverage_meets_minimum": minimum_coverage_valid,
        "single_tpc_matrix_modes_are_canonical": (
            matrix_modes == ["global", "next", "stream"]
        ),
        "single_tpc_matrix_tpc_bits_are_canonical": (
            matrix_tpc_bits == canonical_tpc_bits
        ),
        "single_tpc_matrix_allowed_sm_counts_are_exact": (
            matrix_allowed_sms == [1, 2]
        ),
        "manifest_forbids_mps": safety.get("mps_allowed") is False,
        "manifest_mps_bypass_is_exact": (
            safety.get("mps_bypass")
            == "CUDA_MPS_PIPE_DIRECTORY_empty"
        ),
        "physical_gpu_is_declared": (
            physical_gpu_indices_valid
            and physical_gpu in physical_gpu_indices
        ),
        "gpu_name_matches_manifest": gpu.get("name") == hardware.get("gpu_name"),
        "driver_api_matches_manifest": (
            driver_version == hardware.get("driver_api_version")
        ),
        "timeout_matches_manifest": timeout_s == safety.get("timeout_s"),
        "memory_limit_matches_manifest": (
            maximum_used_mib
            == safety.get("maximum_preexisting_gpu_memory_mib")
        ),
        "baseline_iterations_match_manifest": (
            mode != "baseline" or iterations == baseline.get("iterations")
        ),
        "baseline_blocks_match_manifest": (
            mode != "baseline"
            or (
                isinstance(baseline.get("blocks_per_sm"), int)
                and not isinstance(baseline.get("blocks_per_sm"), bool)
                and manifest_sm_count_valid
                and blocks
                == int(manifest_sm_count)
                * int(baseline.get("blocks_per_sm", -1))
            )
        ),
        "baseline_threads_match_manifest": (
            mode != "baseline"
            or threads_per_block == baseline.get("threads_per_block")
        ),
        "baseline_trial_is_registered": (
            mode != "baseline"
            or (
                isinstance(baseline.get("trials_per_gpu"), int)
                and not isinstance(baseline.get("trials_per_gpu"), bool)
                and 0 <= trial < int(baseline["trials_per_gpu"])
            )
        ),
        "masked_mode_is_registered_in_single_tpc_matrix": (
            not masked
            or (matrix_modes_valid and mode in matrix_modes)
        ),
        "masked_enabled_tpc_is_registered": (
            not masked
            or (
                matrix_tpc_bits_in_range
                and isinstance(enabled_tpc, int)
                and not isinstance(enabled_tpc, bool)
                and enabled_tpc in matrix_tpc_bits
            )
        ),
        "masked_trial_is_registered": (
            not masked
            or (
                matrix_trials_valid
                and isinstance(trial, int)
                and not isinstance(trial, bool)
                and 0 <= trial < matrix_trials
            )
        ),
        "masked_iterations_match_manifest": (
            not masked or iterations == matrix.get("iterations")
        ),
        "masked_blocks_match_manifest": (
            not masked or blocks == matrix.get("blocks")
        ),
        "masked_threads_match_manifest": (
            not masked
            or threads_per_block == matrix.get("threads_per_block")
        ),
        "masked_experiment_promoted": (
            not masked or safety.get("experimental_mask_enabled") is True
        ),
        "masked_mode_approved": not masked or mode in approved_modes,
        "masked_gpu_is_reserved": (
            not masked or gpu.get("uuid") in reserved_uuids
        ),
        "masked_gpu_has_current_reservation_evidence": (
            not masked or reservation_valid
        ),
        "masked_xid_monitoring_is_available": (
            not masked or xid_monitoring.get("available") is True
        ),
        "masked_xid_monitoring_method_is_exact": (
            not masked
            or xid_monitoring.get("method")
            == "nvmlEventSetWait_v2_exact_xid"
        ),
        "masked_xid_quiet_interval_is_exact": (
            not masked
            or xid_monitoring.get("quiet_ms")
            == MASKED_XID_DRAIN_TIMEOUT_MS
        ),
        "masked_xid_library_path_is_absolute": (
            not masked
            or (
                isinstance(xid_monitoring.get("library_path"), str)
                and Path(xid_monitoring["library_path"]).is_absolute()
            )
        ),
        "masked_xid_library_hash_is_pinned": (
            not masked
            or _valid_sha256(xid_monitoring.get("library_sha256"))
        ),
        "masked_xid_library_version_is_pinned": (
            not masked
            or (
                isinstance(xid_monitoring.get("library_version"), str)
                and bool(xid_monitoring.get("library_version"))
            )
        ),
        "runner_masked_health_monitor_is_implemented": (
            not masked or MASKED_HEALTH_MONITOR_IMPLEMENTED
        ),
        "stream_offset_search_promoted": (
            mode != "stream"
            or safety.get("stream_offset_search_enabled") is True
        ),
        "stream_prerequisites_accepted": (
            mode != "stream"
            or safety.get("global_next_matrix_accepted") is True
        ),
        "stream_offset_is_declared": (
            mode != "stream" or experimental_mask_off in stream_candidates
        ),
        "stream_offset_is_8byte_aligned": (
            mode != "stream"
            or (
                experimental_mask_off is not None
                and experimental_mask_off % 8 == 0
            )
        ),
    }
    checks.update(
        {
            f"masked_{name}": (not masked or passed)
            for name, passed in reservation_checks.items()
        }
    )
    return checks, all(checks.values())


def _load_libsmctrl_source_metadata(
    repo_root: Path,
    metadata_path: Path = DEFAULT_SOURCE_METADATA,
) -> dict[str, Any]:
    """Validate canonical metadata and every pinned libsmctrl source byte."""

    canonical_path = repo_root.resolve() / DEFAULT_SOURCE_METADATA
    path_is_canonical = (
        metadata_path == canonical_path
        if metadata_path.is_absolute()
        else metadata_path == DEFAULT_SOURCE_METADATA
    )
    if not path_is_canonical:
        raise RuntimeError(
            "libsmctrl source metadata path must be exactly "
            f"{canonical_path}"
        )
    path = canonical_path
    try:
        content = _read_bounded_regular_bytes(
            path,
            label="libsmctrl source metadata",
            maximum_bytes=MAX_SOURCE_METADATA_BYTES,
        )
        metadata = _parse_strict_json_document(
            content,
            label="libsmctrl source metadata",
            maximum_bytes=MAX_SOURCE_METADATA_BYTES,
            sort_keys=True,
            trailing_newline=True,
            require_canonical=False,
        )
        metadata = _require_exact_object_keys(
            metadata,
            SOURCE_METADATA_TOP_LEVEL_KEYS,
            label="libsmctrl source metadata",
        )
        if metadata.get("schema_version") != "burstserve.libsmctrl-source/v1":
            raise ValueError("unsupported source metadata schema")
        files = _require_exact_object_keys(
            metadata.get("files"),
            SOURCE_METADATA_FILE_KEYS,
            label="libsmctrl source metadata files",
        )
        compatibility = _require_exact_object_keys(
            metadata.get("compatibility"),
            SOURCE_METADATA_COMPATIBILITY_KEYS,
            label="libsmctrl source metadata compatibility",
        )
        if metadata.get("path") != DEFAULT_LIBSMCTRL_ROOT.as_posix():
            raise ValueError("metadata path is not canonical")
        if not _valid_git_oid(metadata.get("source_commit")):
            raise ValueError("metadata source_commit is not a full Git OID")
        for field in ("source_url", "retrieved_on", "policy"):
            if not isinstance(metadata.get(field), str) or not metadata[field]:
                raise ValueError(f"metadata {field} must be nonempty text")
        if not (
            metadata["source_url"].startswith("https://")
            or metadata["source_url"].startswith("http://")
        ):
            raise ValueError("metadata source_url must be HTTP(S)")
        try:
            datetime.strptime(metadata["retrieved_on"], "%Y-%m-%d")
        except ValueError as error:
            raise ValueError(
                "metadata retrieved_on must be YYYY-MM-DD"
            ) from error
        for field, digest in files.items():
            if not _valid_sha256(digest):
                raise ValueError(f"metadata files.{field} is not SHA-256")
        for field in (
            "upstream_readme_cuda_range",
            "target_stream_mask_status",
        ):
            if (
                not isinstance(compatibility.get(field), str)
                or not compatibility[field]
            ):
                raise ValueError(
                    f"metadata compatibility.{field} must be nonempty text"
                )
        for field in (
            "latest_x86_64_stream_case",
            "target_driver_api_version",
        ):
            value = compatibility.get(field)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value <= 0
            ):
                raise ValueError(
                    f"metadata compatibility.{field} must be positive integer"
                )
        if (
            compatibility["target_driver_api_version"]
            < compatibility["latest_x86_64_stream_case"]
        ):
            raise ValueError(
                "metadata target driver predates its latest supported case"
            )
        actual_files: dict[str, str] = {}
        for name in sorted(SOURCE_METADATA_FILE_KEYS):
            source_bytes = _read_bounded_regular_bytes(
                repo_root.resolve() / DEFAULT_LIBSMCTRL_ROOT / name,
                label=f"pinned libsmctrl source {name}",
                maximum_bytes=MAX_FORMAL_SOURCE_FILE_BYTES,
            )
            actual_files[name] = hashlib.sha256(source_bytes).hexdigest()
        if actual_files != dict(files):
            raise ValueError("pinned libsmctrl source file hashes mismatch")
        return {
            "path": str(canonical_path),
            "content": dict(metadata),
            "actual_file_sha256": actual_files,
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    except (OSError, KeyError, RuntimeError, TypeError, ValueError) as exc:
        raise RuntimeError(f"invalid libsmctrl source metadata {path}: {exc}") from exc


def latest_pinned_driver_version(
    repo_root: Path,
    metadata_path: Path = DEFAULT_SOURCE_METADATA,
) -> int:
    """Return the newest driver API case declared by pinned source metadata."""

    record = _load_libsmctrl_source_metadata(repo_root, metadata_path)
    return int(
        record["content"]["compatibility"]["latest_x86_64_stream_case"]
    )


def evaluate_driver_policy(
    *,
    mode: str,
    driver_version: int,
    latest_pinned_version: int,
    experimental_allow_unsupported_driver: bool,
    experimental_mask_off: int | None,
) -> tuple[dict[str, bool], bool]:
    """Evaluate the fail-closed policy before launching a masked probe."""

    if mode not in PROBE_MODES:
        raise ValueError(f"unsupported probe mode: {mode}")
    masked = mode in MASKED_MODES
    driver_is_pinned = (
        driver_version in PINNED_VALIDATED_DRIVER_VERSIONS
        and driver_version <= latest_pinned_version
        and (
            mode != "stream"
            or driver_version >= 9000
        )
    )
    unsupported = masked and not driver_is_pinned
    offset_is_explicit = experimental_mask_off is not None
    checks = {
        "driver_is_pinned_or_explicitly_allowed": (
            not unsupported or experimental_allow_unsupported_driver
        ),
        "stream_unknown_driver_has_explicit_mask_off": (
            not (unsupported and mode == "stream") or offset_is_explicit
        ),
        "mask_off_only_used_for_stream": (
            not offset_is_explicit or mode == "stream"
        ),
        "mask_off_requires_experimental_allow": (
            not offset_is_explicit or experimental_allow_unsupported_driver
        ),
    }
    return checks, all(checks.values())


def build_child_environment(
    *,
    selected_gpu_uuid: str,
    experimental_mask_off: int | None,
    parent_pid: int | None = None,
) -> dict[str, str]:
    """Construct an ``env -i``-equivalent native child allowlist."""

    if (
        not isinstance(selected_gpu_uuid, str)
        or not selected_gpu_uuid
        or "\x00" in selected_gpu_uuid
    ):
        raise ValueError("selected_gpu_uuid must be a non-empty safe string")
    environment = {
        "LANG": "C",
        "LC_ALL": "C",
        "TZ": "UTC",
        "CUDA_CACHE_DISABLE": "1",
        "CUDA_VISIBLE_DEVICES": selected_gpu_uuid,
    }
    # NVIDIA documents an empty/nonexistent pipe directory as the explicit
    # way to bypass MPS. Merely deleting the variable would select the default
    # /tmp/nvidia-mps daemon.
    environment["CUDA_MPS_PIPE_DIRECTORY"] = ""
    if experimental_mask_off is not None:
        if (
            isinstance(experimental_mask_off, bool)
            or not isinstance(experimental_mask_off, int)
        ):
            raise ValueError("experimental_mask_off must be integer or null")
        environment["MASK_OFF"] = str(experimental_mask_off)
    if parent_pid is not None:
        if (
            isinstance(parent_pid, bool)
            or not isinstance(parent_pid, int)
            or parent_pid <= 0
        ):
            raise ValueError("parent_pid must be a positive integer")
        environment["BURSTSERVE_PARENT_PID"] = str(parent_pid)
    return environment


def capture_probe_environment(
    *,
    repo_root: Path,
    selected_gpu_uuid: str,
    expected_libsmctrl_commit: str,
) -> dict[str, Any]:
    """Capture formal provenance without exposing the target GPU to helpers."""

    if (
        not isinstance(selected_gpu_uuid, str)
        or not selected_gpu_uuid
        or "\x00" in selected_gpu_uuid
    ):
        raise ValueError("selected GPU UUID must be a non-empty safe string")
    if not _valid_git_oid(expected_libsmctrl_commit):
        raise ValueError(
            "expected libsmctrl commit must be a lowercase full Git OID"
        )
    snapshot = capture_environment(
        repo_root=repo_root,
        model_root=None,
        command_environment=(
            FORMAL_ENVIRONMENT_CAPTURE_SUBPROCESS_ENVIRONMENT
        ),
        framework_gpu_probe=False,
        allow_nvcc_path_search=False,
        isolated_python=True,
        git_expected_gitlinks={
            DEFAULT_LIBSMCTRL_ROOT.as_posix(): expected_libsmctrl_commit,
        },
        git_allowed_untracked_roots=FORMAL_GIT_ALLOWED_UNTRACKED_ROOTS,
        git_allow_untracked_regular_files=False,
        require_asle_binding=True,
    )
    snapshot["formal_gpu_capture_policy"] = {
        "selected_gpu_uuid": selected_gpu_uuid,
        "framework_gpu_probe_enabled": False,
        "subprocess_cuda_visible_devices": "",
        "target_gpu_exposed_to_framework_subprocess": False,
        "parent_environment_mutated": False,
        "target_gpu_source": (
            f"{TRUSTED_NVIDIA_SMI_EXECUTABLE} exact-environment queries"
        ),
    }
    return snapshot


def build_probe_command(
    *,
    binary: Path,
    mode: str,
    enabled_tpc: int,
    iterations: int,
    blocks: int,
    experimental_allow_unsupported_driver: bool,
) -> list[str]:
    """Construct the native probe command without shell interpolation."""

    if mode not in PROBE_MODES:
        raise ValueError(f"unsupported probe mode: {mode}")
    if isinstance(enabled_tpc, bool) or enabled_tpc < 0:
        raise ValueError("enabled_tpc must be a non-negative bit index")
    if isinstance(iterations, bool) or iterations <= 0:
        raise ValueError("iterations must be positive")
    if isinstance(blocks, bool) or not isinstance(blocks, int) or blocks <= 0:
        raise ValueError("blocks must be positive")
    command = [str(binary), "--mode", mode]
    if mode in MASKED_MODES:
        command.extend(["--enabled-tpc", str(enabled_tpc)])
    command.extend(
        ["--iterations", str(iterations), "--blocks", str(blocks)]
    )
    if experimental_allow_unsupported_driver:
        command.append("--allow-unsupported-driver")
    return command


def parse_native_output(stdout: str) -> dict[str, Any]:
    """Parse the exact compact one-line representation emitted by the probe."""

    if not isinstance(stdout, str):
        raise NativeOutputError("native stdout must be text")
    try:
        raw = stdout.encode("utf-8", errors="strict")
        value = _parse_strict_json_document(
            raw,
            label="native stdout",
            maximum_bytes=MAX_NATIVE_STDOUT_BYTES,
            sort_keys=False,
            trailing_newline=True,
        )
    except (RuntimeError, UnicodeError) as exc:
        raise NativeOutputError(str(exc)) from exc
    if not isinstance(value, dict):
        raise NativeOutputError("native JSON must be an object")
    expected_order = NATIVE_STDOUT_TOP_LEVEL_ORDER
    if tuple(value) not in {expected_order, expected_order + ("error",)}:
        raise NativeOutputError(
            "native JSON top-level keys/order do not match the schema"
        )
    if value.get("schema_version") != NATIVE_SCHEMA_VERSION:
        raise NativeOutputError(
            f"unsupported native schema: {value.get('schema_version')!r}"
        )
    parent_guard = value.get("parent_guard")
    if (
        not isinstance(parent_guard, dict)
        or tuple(parent_guard) != NATIVE_STDOUT_PARENT_GUARD_ORDER
    ):
        raise NativeOutputError(
            "native parent_guard keys/order do not match the schema"
        )
    device = value.get("device")
    if (
        not isinstance(device, dict)
        or tuple(device) != NATIVE_STDOUT_DEVICE_ORDER
    ):
        raise NativeOutputError(
            "native device keys/order do not match the schema"
        )
    histogram = value.get("observed_histogram")
    error_present = "error" in value
    error_valid = (
        error_present
        and isinstance(value["error"], str)
        and bool(value["error"])
    )
    status_ok = value.get("status") == "ok"
    if (
        not isinstance(histogram, dict)
        or len(histogram) > 1024
        or (status_ok and not histogram)
        or (status_ok and error_present)
        or (not status_ok and not error_valid)
    ):
        raise NativeOutputError(
            "native histogram/error fields do not match status semantics"
        )
    numeric_keys: list[int] = []
    for key, count in histogram.items():
        if (
            not isinstance(key, str)
            or not key.isascii()
            or not 1 <= len(key) <= 10
            or not key.isdigit()
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count <= 0
        ):
            raise NativeOutputError(
                "native observed_histogram entries are malformed"
            )
        numeric_key = int(key, 10)
        if str(numeric_key) != key:
            raise NativeOutputError(
                "native observed_histogram key is not canonical decimal"
            )
        numeric_keys.append(numeric_key)
    if numeric_keys != sorted(numeric_keys) or len(set(numeric_keys)) != len(
        numeric_keys
    ):
        raise NativeOutputError(
            "native observed_histogram keys are not unique numeric order"
        )
    return value


def _integer(value: Any, *, field: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise NativeOutputError(
            f"{field} must be an integer greater than or equal to {minimum}"
        )
    return value


def normalize_histogram(value: Any) -> dict[int, int]:
    """Normalize native object, dense-array, or record-array histograms."""

    result: dict[int, int] = {}

    def add(sm_id_value: Any, count_value: Any) -> None:
        if isinstance(sm_id_value, str):
            try:
                sm_id_value = int(sm_id_value, 10)
            except ValueError as exc:
                raise NativeOutputError(
                    f"histogram SM key is not an integer: {sm_id_value!r}"
                ) from exc
        sm_id = _integer(sm_id_value, field="histogram sm_id")
        count = _integer(count_value, field=f"histogram[{sm_id}]")
        if sm_id in result:
            raise NativeOutputError(f"duplicate histogram SM id {sm_id}")
        result[sm_id] = count

    if isinstance(value, Mapping):
        for sm_id, count in value.items():
            add(sm_id, count)
    elif isinstance(value, list):
        if all(
            isinstance(item, int) and not isinstance(item, bool) for item in value
        ):
            for sm_id, count in enumerate(value):
                add(sm_id, count)
        else:
            for item in value:
                if isinstance(item, Mapping):
                    sm_id = item.get("sm_id", item.get("sm", item.get("id")))
                    count = item.get("count", item.get("hits"))
                    add(sm_id, count)
                elif isinstance(item, list) and len(item) == 2:
                    add(item[0], item[1])
                else:
                    raise NativeOutputError(
                        "histogram array must be dense counts, records, or pairs"
                    )
    else:
        raise NativeOutputError("observed_histogram must be an object or array")
    if not result:
        raise NativeOutputError("observed_histogram must not be empty")
    return result


def evaluate_probe(
    native: Mapping[str, Any],
    *,
    expected_mode: str,
    expected_enabled_tpc: int,
    expected_driver_version: int,
    expected_runtime_version: int,
    expected_iterations: int,
    process_exit_code: int,
    expected_device_uuid: str | None = None,
    expected_device_name: str | None = None,
    expected_sm_count: int | None = None,
    expected_compute_capability: Sequence[int] | None = None,
    expected_blocks: int | None = None,
    expected_threads_per_block: int = PROBE_THREADS_PER_BLOCK,
    expected_device_ordinal: int = 0,
    expected_tpc_count: int | None = None,
    expected_parent_pid: int | None = None,
    stderr: str = "",
    allowed_observed_sm_counts: Sequence[int] = (1, 2),
    minimum_sm_coverage: float = BASELINE_MIN_SM_COVERAGE,
) -> tuple[dict[str, bool], dict[str, Any], bool]:
    """Evaluate Gate-A baseline or single-TPC semantics."""

    device = native.get("device")
    device_ok = isinstance(device, Mapping)
    try:
        sm_count = _integer(
            device.get("sm_count") if device_ok else None,
            field="device.sm_count",
            minimum=1,
        )
    except NativeOutputError:
        sm_count = 0
        device_ok = False
    device_uuid = device.get("uuid") if device_ok else None
    device_name = device.get("name") if device_ok else None
    cc_major = device.get("cc_major") if device_ok else None
    cc_minor = device.get("cc_minor") if device_ok else None

    histogram_error: str | None = None
    try:
        histogram = normalize_histogram(native.get("observed_histogram"))
    except NativeOutputError as exc:
        histogram = {}
        histogram_error = str(exc)
    positive_ids = sorted(sm_id for sm_id, count in histogram.items() if count > 0)
    ids_in_range = bool(sm_count) and all(sm_id < sm_count for sm_id in histogram)
    coverage = len(positive_ids) / sm_count if sm_count else 0.0

    mode_matches = native.get("mode") == expected_mode
    status_ok = native.get("status") == "ok"
    try:
        native_driver = _integer(
            native.get("driver_version"),
            field="driver_version",
            minimum=1,
        )
    except NativeOutputError:
        native_driver = -1
    try:
        native_runtime = _integer(
            native.get("runtime_version"),
            field="runtime_version",
            minimum=1,
        )
    except NativeOutputError:
        native_runtime = -1

    try:
        native_iterations = _integer(
            native.get("iterations"),
            field="iterations",
            minimum=1,
        )
    except NativeOutputError:
        native_iterations = -1
    try:
        native_blocks = _integer(
            native.get("blocks"),
            field="blocks",
            minimum=1,
        )
    except NativeOutputError:
        native_blocks = -1
    try:
        native_threads = _integer(
            native.get("threads_per_block"),
            field="threads_per_block",
            minimum=1,
        )
    except NativeOutputError:
        native_threads = -1
    observed_blocks = sum(histogram.values())
    parent_guard = native.get("parent_guard")
    parent_guard_valid = isinstance(parent_guard, Mapping)
    if expected_mode in MASKED_MODES:
        parent_guard_checks = {
            "parent_guard_valid": parent_guard_valid,
            "parent_guard_mode_matches": (
                parent_guard_valid
                and parent_guard.get("mode") == "linux_pdeathsig_sigkill"
            ),
            "parent_guard_status_armed": (
                parent_guard_valid
                and parent_guard.get("status") == "armed"
            ),
            "parent_guard_expected_pid_matches": (
                parent_guard_valid
                and expected_parent_pid is not None
                and isinstance(
                    parent_guard.get("expected_parent_pid"), int
                )
                and not isinstance(
                    parent_guard.get("expected_parent_pid"), bool
                )
                and int(parent_guard["expected_parent_pid"])
                == expected_parent_pid
            ),
            "parent_guard_observed_pid_matches": (
                parent_guard_valid
                and expected_parent_pid is not None
                and isinstance(
                    parent_guard.get("observed_parent_pid"), int
                )
                and not isinstance(
                    parent_guard.get("observed_parent_pid"), bool
                )
                and int(parent_guard["observed_parent_pid"])
                == expected_parent_pid
            ),
            "parent_guard_pdeathsig_is_sigkill": (
                parent_guard_valid
                and isinstance(parent_guard.get("pdeath_signal"), int)
                and not isinstance(
                    parent_guard.get("pdeath_signal"), bool
                )
                and int(parent_guard["pdeath_signal"]) == signal.SIGKILL
            ),
            "parent_guard_inherited_pdeathsig_is_sigkill": (
                parent_guard_valid
                and isinstance(
                    parent_guard.get("inherited_pdeath_signal"), int
                )
                and not isinstance(
                    parent_guard.get("inherited_pdeath_signal"), bool
                )
                and int(parent_guard["inherited_pdeath_signal"])
                == signal.SIGKILL
            ),
        }
    else:
        parent_guard_checks = {
            "parent_guard_valid": parent_guard_valid,
            "parent_guard_mode_matches": (
                parent_guard_valid
                and parent_guard.get("mode") == "not_required"
            ),
            "parent_guard_status_armed": (
                parent_guard_valid
                and parent_guard.get("status") == "not_required"
            ),
            "parent_guard_expected_pid_matches": (
                parent_guard_valid
                and parent_guard.get("expected_parent_pid") is None
            ),
            "parent_guard_observed_pid_matches": (
                parent_guard_valid
                and isinstance(parent_guard.get("observed_parent_pid"), int)
                and not isinstance(
                    parent_guard.get("observed_parent_pid"), bool
                )
                and int(parent_guard["observed_parent_pid"]) > 0
            ),
            "parent_guard_pdeathsig_is_sigkill": (
                parent_guard_valid
                and parent_guard.get("pdeath_signal") is None
            ),
            "parent_guard_inherited_pdeathsig_is_sigkill": (
                parent_guard_valid
                and parent_guard.get("inherited_pdeath_signal") is None
            ),
        }

    baseline_broad = (
        expected_mode != "baseline"
        or (
            len(positive_ids) >= 2
            and coverage >= minimum_sm_coverage
        )
    )
    requested_matches = True
    single_tpc_scope = True
    if expected_mode in MASKED_MODES:
        try:
            requested = _integer(
                native.get("requested_enabled_tpc"),
                field="requested_enabled_tpc",
            )
        except NativeOutputError:
            requested = -1
        requested_matches = requested == expected_enabled_tpc
        valid_allowed_counts = (
            bool(allowed_observed_sm_counts)
            and all(
                isinstance(count, int)
                and not isinstance(count, bool)
                and count > 0
                for count in allowed_observed_sm_counts
            )
            and len(set(allowed_observed_sm_counts))
            == len(allowed_observed_sm_counts)
        )
        single_tpc_scope = (
            valid_allowed_counts
            and len(positive_ids) in allowed_observed_sm_counts
        )

    acceptance = {
        "process_exit_zero": process_exit_code == 0,
        "status_ok": status_ok,
        "mode_matches": mode_matches,
        "driver_version_matches_preflight": native_driver
        == expected_driver_version,
        "runtime_version_matches_manifest": (
            native_runtime == expected_runtime_version
        ),
        "iterations_match_request": native_iterations == expected_iterations,
        "threads_per_block_matches_manifest": (
            native_threads == expected_threads_per_block
        ),
        "successful_native_error_empty": (
            "error" not in native or native.get("error") == ""
        ),
        "successful_native_stderr_empty": stderr == "",
        "device_valid": device_ok,
        "device_ordinal_matches_isolated_visible_device": (
            device_ok
            and isinstance(device.get("ordinal"), int)
            and not isinstance(device.get("ordinal"), bool)
            and device.get("ordinal") == expected_device_ordinal
        ),
        "device_uuid_matches_preflight": (
            expected_device_uuid is None or device_uuid == expected_device_uuid
        ),
        "device_name_matches_manifest": (
            expected_device_name is None or device_name == expected_device_name
        ),
        "sm_count_matches_manifest": (
            expected_sm_count is None or sm_count == expected_sm_count
        ),
        "compute_capability_matches_manifest": (
            expected_compute_capability is None
            or [cc_major, cc_minor] == list(expected_compute_capability)
        ),
        "histogram_valid": histogram_error is None,
        "observed_sm_ids_in_range": ids_in_range,
        "samples_observed": bool(positive_ids),
        "histogram_count_matches_reported_blocks": (
            native_blocks > 0 and observed_blocks == native_blocks
        ),
        "reported_blocks_match_manifest": (
            expected_blocks is None or native_blocks == expected_blocks
        ),
        "tpc_count_matches_manifest": (
            native.get("tpc_count") == expected_tpc_count
        ),
        "baseline_broad_sm_coverage": baseline_broad,
        "requested_tpc_matches": requested_matches,
        "single_tpc_observed_one_or_two_sms": single_tpc_scope,
        **parent_guard_checks,
    }
    metrics = {
        "device_sm_count": sm_count,
        "device_uuid": device_uuid,
        "device_name": device_name,
        "compute_capability": [cc_major, cc_minor],
        "observed_sm_count": len(positive_ids),
        "observed_sm_ids": positive_ids,
        "sm_coverage_ratio": coverage,
        "reported_blocks": native_blocks,
        "observed_blocks": observed_blocks,
        "reported_iterations": native_iterations,
        "reported_runtime_version": native_runtime,
        "reported_threads_per_block": native_threads,
        "reported_tpc_count": native.get("tpc_count"),
        "histogram_error": histogram_error,
        "parent_guard": dict(parent_guard) if parent_guard_valid else None,
    }
    return acceptance, metrics, all(acceptance.values())


def _record_event(
    path: Path,
    *,
    run_id: str,
    sequence: int,
    event_type: str,
    payload: Mapping[str, Any] | None = None,
) -> None:
    append_jsonl_atomic(
        path,
        EventRecord.create(
            run_id=run_id,
            sequence=sequence,
            event_type=event_type,
            payload=payload,
        ),
    )


def _monitor_error_provenance(
    *,
    gpu_uuid: str,
    phase: str,
    error: BaseException,
) -> dict[str, Any]:
    """Return fail-closed provenance when a monitor cannot serialize itself."""

    return {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "physical_uuid": gpu_uuid,
        "library": None,
        "xid_event_bit": NVML_EVENT_TYPE_XID_CRITICAL_ERROR,
        "supported_event_bits": None,
        "registered_event_bits": 0,
        "setup": {
            "started_at_utc": None,
            "completed_at_utc": _utc_now(),
            "succeeded": False,
            "error": {
                "timestamp_utc": _utc_now(),
                "category": "runner",
                "operation": phase,
                "code": None,
                "return_name": None,
                "message": f"{type(error).__name__}: {error}",
            },
        },
        "drain": {
            "started_at_utc": None,
            "completed_at_utc": None,
            "timeout_ms": MASKED_XID_DRAIN_TIMEOUT_MS,
            "requested_quiet_ms": MASKED_XID_DRAIN_TIMEOUT_MS,
            "observed_quiet_ms": None,
            "wait_calls": 0,
            "succeeded": False,
            "error": None,
        },
        "cleanup": {
            "completed_at_utc": None,
            "errors": [],
        },
        "events": [],
        "xids_seen": 0,
        "safe_for_acceptance": False,
    }


def _masked_monitor_record(
    *,
    mode: str,
    status: str,
    provenance: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if mode not in MASKED_MODES:
        return None
    return {
        "status": status,
        "post_probe_drain_timeout_ms": MASKED_XID_DRAIN_TIMEOUT_MS,
        "provenance": dict(provenance) if provenance is not None else None,
    }


def evaluate_masked_health_monitor(
    provenance: Any,
    *,
    expected_gpu_uuid: str,
    expected_library_path: str,
    expected_library_sha256: str,
    expected_library_version: str,
) -> tuple[dict[str, bool], bool]:
    """Independently validate monitor provenance instead of trusting its verdict."""

    record = provenance if isinstance(provenance, Mapping) else {}
    setup = record.get("setup")
    setup = setup if isinstance(setup, Mapping) else {}
    drain = record.get("drain")
    drain = drain if isinstance(drain, Mapping) else {}
    cleanup = record.get("cleanup")
    cleanup = cleanup if isinstance(cleanup, Mapping) else {}
    library = record.get("library")
    library = library if isinstance(library, Mapping) else {}

    supported = record.get("supported_event_bits")
    registered = record.get("registered_event_bits")
    registered_device_handle = record.get("registered_device_handle")
    xids_seen = record.get("xids_seen")
    events = record.get("events")
    event_records_valid = isinstance(events, list) and all(
        isinstance(event, Mapping)
        and set(event)
        == {
            "timestamp_utc",
            "timestamp_monotonic_s",
            "device_handle",
            "event_type_bits",
            "xid_code",
            "gpu_instance_id",
            "compute_instance_id",
        }
        and isinstance(event.get("event_type_bits"), int)
        and not isinstance(event.get("event_type_bits"), bool)
        and int(event["event_type_bits"])
        == NVML_EVENT_TYPE_XID_CRITICAL_ERROR
        and isinstance(event.get("xid_code"), int)
        and not isinstance(event.get("xid_code"), bool)
        and 0 <= int(event["xid_code"]) <= 0xFFFFFFFFFFFFFFFF
        and isinstance(event.get("device_handle"), int)
        and not isinstance(event.get("device_handle"), bool)
        and int(event["device_handle"]) == registered_device_handle
        and isinstance(event.get("gpu_instance_id"), int)
        and not isinstance(event.get("gpu_instance_id"), bool)
        and 0 <= int(event["gpu_instance_id"]) <= 0xFFFFFFFF
        and isinstance(event.get("compute_instance_id"), int)
        and not isinstance(event.get("compute_instance_id"), bool)
        and 0 <= int(event["compute_instance_id"]) <= 0xFFFFFFFF
        and isinstance(event.get("timestamp_utc"), str)
        and bool(event.get("timestamp_utc"))
        and event["timestamp_utc"].endswith("Z")
        and _parse_utc(event["timestamp_utc"]) is not None
        and isinstance(event.get("timestamp_monotonic_s"), (int, float))
        and not isinstance(event.get("timestamp_monotonic_s"), bool)
        and math.isfinite(float(event["timestamp_monotonic_s"]))
        for event in (events if isinstance(events, list) else [])
    )
    events_contain_no_xid = event_records_valid and all(
        not (
            int(event["event_type_bits"])
            & NVML_EVENT_TYPE_XID_CRITICAL_ERROR
        )
        for event in events
    )
    cleanup_errors = cleanup.get("errors")
    requested_quiet = drain.get("requested_quiet_ms")
    observed_quiet = drain.get("observed_quiet_ms")
    required_symbols = {
        "init": "nvmlInit_v2",
        "shutdown": "nvmlShutdown",
        "system_get_nvml_version": "nvmlSystemGetNVMLVersion",
        "device_get_handle_by_uuid": "nvmlDeviceGetHandleByUUID_v2",
        "event_set_create": "nvmlEventSetCreate",
        "event_set_free": "nvmlEventSetFree",
        "device_get_supported_event_types": (
            "nvmlDeviceGetSupportedEventTypes"
        ),
        "device_register_events": "nvmlDeviceRegisterEvents",
        "event_set_wait_v2": "nvmlEventSetWait_v2",
    }
    symbols = library.get("symbols")
    library_identity = library.get("identity")
    sealed_snapshot = library.get("sealed_snapshot")
    load_path = library.get("load_path")

    def finite_number(value: Any) -> bool:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            and float(value) >= 0
        )

    def valid_utc(value: Any) -> bool:
        if not isinstance(value, str) or not value.endswith("Z"):
            return False
        try:
            parsed = datetime.fromisoformat(value[:-1] + "+00:00")
        except ValueError:
            return False
        return parsed.tzinfo is not None

    setup_start = setup.get("started_monotonic_s")
    setup_end = setup.get("completed_monotonic_s")
    drain_start = drain.get("started_monotonic_s")
    drain_end = drain.get("completed_monotonic_s")
    quiet_start = drain.get("quiet_started_monotonic_s")
    quiet_end = drain.get("quiet_completed_monotonic_s")
    checks = {
        "schema_version_exact": (
            record.get("schema_version") == PROVENANCE_SCHEMA_VERSION
        ),
        "monitor_method_exact": (
            record.get("method") == "nvmlEventSetWait_v2_exact_xid"
        ),
        "physical_gpu_uuid_exact": (
            record.get("physical_uuid") == expected_gpu_uuid
        ),
        "registered_device_handle_valid": (
            isinstance(registered_device_handle, int)
            and not isinstance(registered_device_handle, bool)
            and registered_device_handle > 0
        ),
        "xid_event_bit_exact": (
            record.get("xid_event_bit")
            == NVML_EVENT_TYPE_XID_CRITICAL_ERROR
        ),
        "xid_bit_supported": (
            isinstance(supported, int)
            and not isinstance(supported, bool)
            and bool(supported & NVML_EVENT_TYPE_XID_CRITICAL_ERROR)
        ),
        "xid_bit_registered_exact": (
            registered == NVML_EVENT_TYPE_XID_CRITICAL_ERROR
        ),
        "library_path_exact": (
            library.get("path") == expected_library_path
        ),
        "library_loaded_from_sealed_snapshot_fd": (
            isinstance(load_path, str)
            and load_path.startswith("/proc/self/fd/")
            and load_path.removeprefix("/proc/self/fd/").isdecimal()
        ),
        "library_identity_exact_regular": (
            isinstance(library_identity, Mapping)
            and set(library_identity)
            == {
                "device",
                "inode",
                "mode",
                "uid",
                "gid",
                "nlink",
                "size",
                "mtime_ns",
                "ctime_ns",
            }
            and all(
                isinstance(library_identity.get(name), int)
                and not isinstance(library_identity.get(name), bool)
                and int(library_identity[name]) >= 0
                for name in (
                    "device",
                    "inode",
                    "mode",
                    "uid",
                    "gid",
                    "nlink",
                    "size",
                    "mtime_ns",
                    "ctime_ns",
                )
            )
            and stat.S_ISREG(int(library_identity["mode"]))
            and int(library_identity["uid"]) in {0, os.geteuid()}
            and stat.S_IMODE(int(library_identity["mode"])) & 0o022 == 0
            and int(library_identity["nlink"]) == 1
            and 0
            < int(library_identity["size"])
            <= MAX_NVML_LIBRARY_BYTES
        ),
        "library_sealed_snapshot_exact": (
            isinstance(sealed_snapshot, Mapping)
            and isinstance(library_identity, Mapping)
            and set(sealed_snapshot)
            == {
                "device",
                "inode",
                "mode",
                "size",
                "sha256",
                "seals",
                "required_seals",
                "exec_seal",
                "exec_seal_applied",
                "mfd_exec_used",
                "copy_limit_bytes",
            }
            and all(
                isinstance(sealed_snapshot.get(name), int)
                and not isinstance(sealed_snapshot.get(name), bool)
                and int(sealed_snapshot[name]) >= 0
                for name in (
                    "device",
                    "inode",
                    "mode",
                    "size",
                    "seals",
                    "required_seals",
                    "exec_seal",
                    "copy_limit_bytes",
                )
            )
            and isinstance(
                sealed_snapshot.get("exec_seal_applied"),
                bool,
            )
            and isinstance(sealed_snapshot.get("mfd_exec_used"), bool)
            and stat.S_ISREG(int(sealed_snapshot["mode"]))
            and stat.S_IMODE(int(sealed_snapshot["mode"])) == 0o500
            and sealed_snapshot.get("sha256") == expected_library_sha256
            and int(sealed_snapshot["size"])
            == int(library_identity["size"])
            and int(sealed_snapshot["copy_limit_bytes"])
            == MAX_NVML_LIBRARY_BYTES
            and (
                int(sealed_snapshot["seals"])
                & int(sealed_snapshot["required_seals"])
                == int(sealed_snapshot["required_seals"])
            )
            and (
                sealed_snapshot["exec_seal_applied"] is False
                or (
                    int(sealed_snapshot["seals"])
                    & int(sealed_snapshot["exec_seal"])
                    == int(sealed_snapshot["exec_seal"])
                )
            )
        ),
        "library_sha256_exact": (
            library.get("sha256") == expected_library_sha256
            and library.get("expected_sha256")
            == expected_library_sha256
        ),
        "library_version_exact": (
            library.get("version") == expected_library_version
            and library.get("expected_version")
            == expected_library_version
        ),
        "required_symbols_exact": symbols == required_symbols,
        "setup_succeeded": setup.get("succeeded") is True,
        "setup_error_absent": setup.get("error") is None,
        "drain_succeeded": drain.get("succeeded") is True,
        "drain_error_absent": drain.get("error") is None,
        "drain_timeout_exact": (
            drain.get("timeout_ms") == MASKED_XID_DRAIN_TIMEOUT_MS
        ),
        "requested_quiet_interval_exact": (
            requested_quiet == MASKED_XID_DRAIN_TIMEOUT_MS
        ),
        "observed_quiet_interval_sufficient": (
            isinstance(observed_quiet, (int, float))
            and not isinstance(observed_quiet, bool)
            and math.isfinite(float(observed_quiet))
            and float(observed_quiet) + 1e-6
            >= MASKED_XID_DRAIN_TIMEOUT_MS
        ),
        "setup_timestamps_complete": (
            valid_utc(setup.get("started_at_utc"))
            and valid_utc(setup.get("completed_at_utc"))
            and finite_number(setup_start)
            and finite_number(setup_end)
            and float(setup_end) >= float(setup_start)
        ),
        "drain_timestamps_complete": (
            valid_utc(drain.get("started_at_utc"))
            and valid_utc(drain.get("completed_at_utc"))
            and finite_number(drain_start)
            and finite_number(drain_end)
            and float(drain_end) >= float(drain_start)
        ),
        "quiet_monotonic_interval_complete": (
            finite_number(quiet_start)
            and finite_number(quiet_end)
            and float(quiet_end) >= float(quiet_start)
        ),
        "cleanup_timestamp_complete": (
            valid_utc(cleanup.get("completed_at_utc"))
            and finite_number(cleanup.get("completed_monotonic_s"))
        ),
        "lifecycle_monotonic_ordered": (
            finite_number(setup_end)
            and finite_number(drain_start)
            and finite_number(drain_end)
            and finite_number(cleanup.get("completed_monotonic_s"))
            and float(drain_start) >= float(setup_end)
            and float(drain_end) >= float(drain_start)
            and float(cleanup["completed_monotonic_s"]) >= float(drain_end)
        ),
        "drain_wait_calls_positive_integer": (
            isinstance(drain.get("wait_calls"), int)
            and not isinstance(drain.get("wait_calls"), bool)
            and drain.get("wait_calls") > 0
        ),
        "cleanup_errors_absent": (
            isinstance(cleanup_errors, list) and not cleanup_errors
        ),
        "xids_seen_is_zero": (
            isinstance(xids_seen, int)
            and not isinstance(xids_seen, bool)
            and xids_seen == 0
        ),
        "xid_count_matches_events": (
            isinstance(xids_seen, int)
            and not isinstance(xids_seen, bool)
            and isinstance(events, list)
            and xids_seen == len(events)
        ),
        "event_records_valid": event_records_valid,
        "events_contain_no_xid": events_contain_no_xid,
        "monitor_safe_verdict_true": (
            record.get("safe_for_acceptance") is True
        ),
    }
    return checks, all(checks.values())


def _sha256_file(path: Path) -> str:
    descriptor, identity = _open_regular_nofollow(path)
    os.close(descriptor)
    return str(identity["sha256"])


def _path_identity(path: Path) -> dict[str, Any]:
    """Return a no-follow identity used to detect source replacement races."""

    try:
        stat_result = path.stat(follow_symlinks=False)
    except OSError as error:
        return {
            "path": str(path),
            "available": False,
            "error": f"{type(error).__name__}: {error}",
        }
    return {
        "path": str(path),
        "available": True,
        "device": int(stat_result.st_dev),
        "inode": int(stat_result.st_ino),
        "mode": int(stat_result.st_mode),
        "size": int(stat_result.st_size),
        "mtime_ns": int(stat_result.st_mtime_ns),
    }


def native_build_record(binary: Path) -> dict[str, Any]:
    """Return the generated native build configuration beside *binary*."""

    stamp = binary.parent / "build-config.stamp"
    try:
        content_bytes = _read_bounded_regular_bytes(
            stamp,
            label="native build stamp",
            maximum_bytes=MAX_BUILD_STAMP_BYTES,
        )
        content = content_bytes.decode("utf-8", errors="strict")
    except (OSError, RuntimeError, UnicodeError):
        return {
            "found": False,
            "path": str(stamp),
        }
    return {
        "found": True,
        "path": str(stamp),
        "sha256": hashlib.sha256(content_bytes).hexdigest(),
        "content": content,
    }


def _parse_build_stamp_strict(content: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in content.splitlines():
        key, separator, value = line.partition("=")
        if not separator or not key or key in fields:
            raise RuntimeError(f"malformed or duplicate build stamp line: {line!r}")
        fields[key] = value
    if tuple(fields) != BUILD_STAMP_FIELDS:
        raise RuntimeError("build stamp fields/order do not match the schema")
    return fields


def _tree_digest(root: Path) -> str:
    resolved = root.resolve(strict=True)
    if not resolved.is_dir():
        raise RuntimeError(f"identity tree is not a directory: {resolved}")
    files = sorted(
        (path for path in resolved.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(resolved).as_posix(),
    )
    if not files:
        raise RuntimeError(f"identity tree is empty: {resolved}")
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(resolved).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(_sha256_file(path).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _clean_tool_environment() -> dict[str, str]:
    return dict(TRUSTED_TOOL_ENVIRONMENT)


def _command_version_sha256(
    command: str,
    *,
    environment: Mapping[str, str] | None = None,
    version_arguments: Sequence[str] = ("--version",),
) -> str:
    tokens = shlex.split(command)
    if not tokens:
        raise RuntimeError("empty toolchain command")
    completed = subprocess.run(
        [*tokens, *version_arguments],
        env=(
            dict(environment)
            if environment is not None
            else _clean_tool_environment()
        ),
        check=False,
        capture_output=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"tool version query failed for {command!r}: "
            f"exit {completed.returncode}"
        )
    return hashlib.sha256(
        completed.stdout + b"\0" + completed.stderr
    ).hexdigest()


def _tool_version_arguments(command_field: str) -> tuple[str, ...]:
    if command_field == "PYTHON_EXECUTABLE":
        return ("-I", "-S", "-B", "--version")
    return ("--version",)


def _runtime_dependencies(path: Path) -> list[dict[str, str]]:
    completed = subprocess.run(
        ["/usr/bin/ldd", str(path)],
        env=dict(TRUSTED_TOOL_ENVIRONMENT),
        check=False,
        capture_output=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"ldd failed for {path}")
    dependencies: dict[str, Path] = {}
    for raw in completed.stdout.decode("utf-8", errors="strict").splitlines():
        line = raw.strip()
        if not line or line.startswith("linux-vdso"):
            continue
        if "not found" in line:
            raise RuntimeError(f"unresolved runtime dependency: {line}")
        if "=>" in line:
            name, remainder = line.split("=>", 1)
            candidate = remainder.strip().split(maxsplit=1)[0]
            if candidate.startswith("/"):
                dependencies[name.strip()] = Path(candidate).resolve(
                    strict=True
                )
        else:
            candidate = line.split(maxsplit=1)[0]
            if candidate.startswith("/"):
                resolved = Path(candidate).resolve(strict=True)
                dependencies[resolved.name] = resolved
    return [
        {
            "name": name,
            "path": str(dependency),
            "sha256": _sha256_file(dependency),
        }
        for name, dependency in sorted(dependencies.items())
    ]


def _verify_attestation_with_pinned_builder(
    *,
    repo_root: Path,
    stamp_fields: Mapping[str, str],
    stamp_path: Path,
    attestation_path: Path,
) -> dict[str, Any]:
    """Run the already-hashed verifier to cover its complete finite contract."""

    script_path = repo_root / "native/smctrl_probe/build_attestation.py"
    script_descriptor, script_identity = _open_regular_nofollow(script_path)
    try:
        environment = {
            "LANG": "C",
            "LC_ALL": "C",
            "TZ": "UTC",
            "SOURCE_DATE_EPOCH": "0",
            "CUDA_CACHE_DISABLE": "1",
        }
        environment.update(
            {
                "BS_SOURCE_DIR": str(
                    repo_root / "native/smctrl_probe"
                ),
                "BS_REPO_ROOT": str(repo_root),
                "BS_BUILD_DIR": str(stamp_path.parent),
                "BS_BUILD_TMPDIR": stamp_fields["BUILD_TMPDIR"],
                "BS_BUILD_LOCK_PATH": stamp_fields["BUILD_LOCK_PATH"],
                "BS_HERMETIC_PATH": stamp_fields["HERMETIC_PATH"],
                "BS_CUDA_HOME": stamp_fields["CUDA_HOME"],
                "BS_CUDA_ARCH": stamp_fields["CUDA_ARCH"],
                "BS_NVCC": stamp_fields["NVCC"],
                "BS_CC": stamp_fields["CC"],
                "BS_AR": stamp_fields["AR"],
                "BS_CPPFLAGS": stamp_fields["CPPFLAGS"],
                "BS_CFLAGS": stamp_fields["CFLAGS"],
                "BS_NVCCFLAGS": stamp_fields["NVCCFLAGS"],
                "BS_LDLIBS": stamp_fields["LDLIBS"],
                "BS_LAUNCHER_CFLAGS": stamp_fields[
                    "LAUNCHER_CFLAGS"
                ],
                "BS_LIBSMCTRL_DIR": stamp_fields["LIBSMCTRL_DIR"],
            }
        )
        command = [
            stamp_fields["PYTHON_EXECUTABLE"],
            "-I",
            "-S",
            "-B",
            f"/proc/self/fd/{script_descriptor}",
            "verify",
            "--stamp",
            str(stamp_path),
            "--attestation",
            str(attestation_path),
        ]
        completed = subprocess.run(
            command,
            cwd=repo_root,
            env=environment,
            check=False,
            capture_output=True,
            timeout=180,
            close_fds=True,
            pass_fds=(script_descriptor,),
        )
    finally:
        os.close(script_descriptor)
    return {
        "command": command,
        "script_fd_identity": script_identity,
        "return_code": completed.returncode,
        "stdout_sha256": hashlib.sha256(completed.stdout).hexdigest(),
        "stderr_sha256": hashlib.sha256(completed.stderr).hexdigest(),
        "stderr": completed.stderr.decode("utf-8", errors="replace"),
        "passed": completed.returncode == 0,
    }


def _artifact_record_matches(
    record: Any,
    expected_path: Path,
    *,
    expected_mode: int,
) -> tuple[dict[str, Any], dict[str, bool]]:
    declared = record if isinstance(record, Mapping) else {}
    descriptor, actual = _open_regular_nofollow(expected_path)
    os.close(descriptor)
    checks = {
        "record_is_object": isinstance(record, Mapping),
        "record_keys_exact": (
            isinstance(record, Mapping)
            and set(record)
            == {"path", "sha256", "size_bytes", "metadata", "xattrs"}
        ),
        "path_exact": declared.get("path") == str(expected_path),
        "sha256_exact": declared.get("sha256") == actual["sha256"],
        "size_exact": declared.get("size_bytes") == actual["size"],
    }
    status = expected_path.stat(follow_symlinks=False)
    xattrs = _xattr_records(expected_path)
    metadata_actual = {
        "file_type": "regular",
        "uid": status.st_uid,
        "gid": status.st_gid,
        "mode_octal": f"{stat.S_IMODE(status.st_mode):04o}",
        "nlink": status.st_nlink,
        "device": status.st_dev,
        "inode": status.st_ino,
        "xattrs": xattrs,
    }
    checks.update(
        {
            "metadata_exact": declared.get("metadata") == metadata_actual,
            "metadata_keys_exact": (
                isinstance(declared.get("metadata"), Mapping)
                and set(declared["metadata"])
                == {
                    "file_type",
                    "uid",
                    "gid",
                    "mode_octal",
                    "nlink",
                    "device",
                    "inode",
                    "xattrs",
                }
            ),
            "xattrs_exact": declared.get("xattrs") == xattrs,
            "owner_is_euid": status.st_uid == os.geteuid(),
            "mode_exact": stat.S_IMODE(status.st_mode) == expected_mode,
            "single_link": status.st_nlink == 1,
            "security_capability_absent": all(
                item["name"] != "security.capability" for item in xattrs
            ),
        }
    )
    actual["metadata"] = metadata_actual
    return actual, checks


def _formal_build_untracked_policy(
    *,
    repo_root: Path,
    snapshot: RepositorySnapshot,
    attestation: Mapping[str, Any],
    attestation_identity: Mapping[str, Any],
    asle_archive: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, bool]]:
    """Allow exactly the canonical attested build inventory plus pinned ASLE."""

    root = repo_root.resolve()
    build_relative = DEFAULT_BINARY.parent
    canonical_outputs = {
        "launcher": DEFAULT_BINARY,
        "real_probe": build_relative / "smid_probe.real",
        "parent_guard_test_helper": (
            build_relative / "parent_guard_test_helper"
        ),
        "real_probe_identity_header": (
            build_relative / "real_probe_identity.h"
        ),
        "guard_exec_test_launcher": (
            build_relative / "guard_exec_test_launcher"
        ),
        "guard_exec_test_fixture": (
            build_relative / "guard_exec_test_launcher.real"
        ),
        "guard_exec_test_identity_header": (
            build_relative / "guard_exec_test_identity.h"
        ),
    }
    canonical_modes = {
        "launcher": "0500",
        "real_probe": "0500",
        "parent_guard_test_helper": "0500",
        "real_probe_identity_header": "0400",
        "guard_exec_test_launcher": "0500",
        "guard_exec_test_fixture": "0500",
        "guard_exec_test_identity_header": "0400",
    }
    outputs = attestation.get("outputs")
    outputs = outputs if isinstance(outputs, Mapping) else {}
    output_keys_exact = set(outputs) == set(canonical_outputs)

    expected_files: dict[str, dict[str, Any]] = {}
    record_checks: dict[str, bool] = {}

    def add_canonical_record(
        *,
        label: str,
        relative: Path,
        record: Any,
        expected_mode: str,
        size_required: bool,
    ) -> None:
        declared = record if isinstance(record, Mapping) else {}
        metadata = declared.get("metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        raw_mode = declared.get("mode")
        declared_mode = metadata.get("mode_octal")
        if (
            declared_mode is None
            and isinstance(raw_mode, int)
            and not isinstance(raw_mode, bool)
        ):
            declared_mode = f"{stat.S_IMODE(raw_mode):04o}"
        size = declared.get("size_bytes", declared.get("size"))
        device = metadata.get("device", declared.get("device"))
        inode = metadata.get("inode", declared.get("inode"))
        canonical_path = root / relative
        valid = bool(
            declared.get("path") == str(canonical_path)
            and _valid_sha256(declared.get("sha256"))
            and declared_mode == expected_mode
            and isinstance(device, int)
            and not isinstance(device, bool)
            and isinstance(inode, int)
            and not isinstance(inode, bool)
            and (
                not size_required
                or (
                    isinstance(size, int)
                    and not isinstance(size, bool)
                    and size > 0
                )
            )
        )
        expected_files[relative.as_posix()] = {
            "label": label,
            "record_valid": valid,
            "sha256": declared.get("sha256"),
            "size": size,
            "mode_octal": expected_mode,
            "device": device,
            "inode": inode,
        }
        record_checks[label] = valid

    build_stamp = attestation.get("build_stamp")
    add_canonical_record(
        label="build_stamp",
        relative=build_relative / "build-config.stamp",
        record=build_stamp,
        expected_mode="0400",
        size_required=False,
    )
    for name, relative in canonical_outputs.items():
        add_canonical_record(
            label=f"output:{name}",
            relative=relative,
            record=outputs.get(name),
            expected_mode=canonical_modes[name],
            size_required=True,
        )
    add_canonical_record(
        label="build_attestation",
        relative=build_relative / "build-attestation.json",
        record=attestation_identity,
        expected_mode="0400",
        size_required=True,
    )

    asle_expected = asle_archive.get("expected")
    asle_record_valid = bool(
        asle_archive.get("passed") is True
        and asle_archive.get("path") == "ASLE.tar.gz"
        and isinstance(asle_expected, Mapping)
        and _valid_sha256(asle_expected.get("sha256"))
        and isinstance(asle_expected.get("size"), int)
        and not isinstance(asle_expected.get("size"), bool)
        and asle_expected.get("size", 0) > 0
        and asle_expected.get("mode_octal") == "0644"
    )
    expected_files["ASLE.tar.gz"] = {
        "label": "pinned_asle_archive",
        "record_valid": asle_record_valid,
        "sha256": (
            asle_expected.get("sha256")
            if isinstance(asle_expected, Mapping)
            else None
        ),
        "size": (
            asle_expected.get("size")
            if isinstance(asle_expected, Mapping)
            else None
        ),
        "mode_octal": "0644",
        "device": None,
        "inode": None,
    }
    record_checks["pinned_asle_archive"] = asle_record_valid

    expected_directories = {
        "build": "0700",
        build_relative.as_posix(): "0700",
        (build_relative / "tmp").as_posix(): "0700",
    }
    actual = {
        os.fsdecode(entry.path): entry
        for entry in snapshot.untracked_entries
    }
    expected_paths = set(expected_files) | set(expected_directories)
    path_set_exact = set(actual) == expected_paths

    file_checks: dict[str, bool] = {}
    for relative, expected in sorted(expected_files.items()):
        entry = actual.get(relative)
        file_checks[relative] = bool(
            expected["record_valid"]
            and entry is not None
            and entry.kind == "regular"
            and entry.sha256 == expected["sha256"]
            and (
                expected["size"] is None
                or entry.size == expected["size"]
            )
            and entry.mode_octal == expected["mode_octal"]
            and (
                expected["device"] is None
                or entry.device == expected["device"]
            )
            and (
                expected["inode"] is None
                or entry.inode == expected["inode"]
            )
        )

    directory_checks: dict[str, bool] = {}
    directory_metadata: dict[str, dict[str, Any]] = {}
    for relative, expected_mode in sorted(expected_directories.items()):
        entry = actual.get(relative)
        path = root / relative
        try:
            status = os.lstat(path)
            xattrs = _xattr_records(path)
            metadata = {
                "device": int(status.st_dev),
                "inode": int(status.st_ino),
                "uid": int(status.st_uid),
                "gid": int(status.st_gid),
                "mode_octal": f"{stat.S_IMODE(status.st_mode):04o}",
                "file_type": (
                    "directory"
                    if stat.S_ISDIR(status.st_mode)
                    else "other"
                ),
                "xattrs": xattrs,
            }
            passed = bool(
                entry is not None
                and entry.kind == "directory"
                and entry.device == status.st_dev
                and entry.inode == status.st_ino
                and stat.S_ISDIR(status.st_mode)
                and status.st_uid == os.geteuid()
                and stat.S_IMODE(status.st_mode) == int(expected_mode, 8)
                and not status.st_mode & (stat.S_ISUID | stat.S_ISGID)
                and not xattrs
            )
        except OSError as error:
            metadata = {
                "error": f"{type(error).__name__}: {error}",
            }
            passed = False
        directory_metadata[relative] = metadata
        directory_checks[relative] = passed

    checks = {
        "formal_git_build_output_keys_exact": output_keys_exact,
        "formal_git_build_records_canonical": (
            bool(record_checks) and all(record_checks.values())
        ),
        "formal_git_build_exception_paths_exact": path_set_exact,
        "formal_git_build_exception_files_attested": (
            bool(file_checks) and all(file_checks.values())
        ),
        "formal_git_build_exception_directories_private": (
            bool(directory_checks) and all(directory_checks.values())
        ),
    }
    record = {
        "policy": (
            "exact canonical nine-file build inventory, three private "
            "directories, and separately pinned ASLE archive"
        ),
        "expected_files": expected_files,
        "expected_directories": expected_directories,
        "actual_entries": {
            path: entry.to_dict() for path, entry in sorted(actual.items())
        },
        "record_checks": record_checks,
        "file_checks": file_checks,
        "directory_metadata": directory_metadata,
        "directory_checks": directory_checks,
        "checks": checks,
    }
    return record, checks


def _formal_git_source_policy(
    *,
    repo_root: Path,
    gate_manifest: Mapping[str, Any],
    attestation: Mapping[str, Any],
    attestation_identity: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, bool], RepositorySnapshot]:
    source = gate_manifest.get("source")
    source = source if isinstance(source, Mapping) else {}
    expected_commit = source.get("libsmctrl_commit")
    if not _valid_git_oid(expected_commit):
        raise RuntimeError(
            "formal source policy requires a full libsmctrl Git OID"
        )
    snapshot = capture_formal_git_snapshot(
        repo_root,
        expected_libsmctrl_commit=expected_commit,
    )
    gitlink = next(
        (
            item
            for item in snapshot.gitlinks
            if item.path == os.fsencode(DEFAULT_LIBSMCTRL_ROOT.as_posix())
        ),
        None,
    )
    asle_metadata = load_asle_source_metadata(repo_root)
    asle_archive = verify_asle_archive_snapshot(
        asle_metadata,
        snapshot,
    )
    libsmctrl_metadata = _load_libsmctrl_source_metadata(repo_root)
    build_record, build_checks = _formal_build_untracked_policy(
        repo_root=repo_root,
        snapshot=snapshot,
        attestation=attestation,
        attestation_identity=attestation_identity,
        asle_archive=asle_archive,
    )

    def tracked_path_exact(relative: str) -> bool:
        state = snapshot.path_state(relative)
        head = state.get("head")
        index = state.get("index")
        worktree = state.get("worktree")
        return bool(
            isinstance(head, Mapping)
            and isinstance(index, Mapping)
            and isinstance(worktree, Mapping)
            and head.get("mode") in {"100644", "100755"}
            and index.get("mode") == head.get("mode")
            and worktree.get("git_mode") == head.get("mode")
            and index.get("oid") == head.get("oid")
            and worktree.get("git_oid") == head.get("oid")
        )

    metadata_path = source.get("libsmctrl_metadata")
    checks = {
        "formal_git_snapshot_complete": snapshot.complete,
        "formal_git_head_available": snapshot.head_oid is not None,
        "formal_git_no_staged_changes": not snapshot.staged_changes,
        "formal_git_no_tracked_worktree_changes": (
            not snapshot.unstaged_changes
        ),
        "formal_git_libsmctrl_registered_clean": (
            gitlink is not None and gitlink.clean
        ),
        "formal_git_libsmctrl_commit_exact": (
            gitlink is not None
            and gitlink.snapshot.head_oid == expected_commit
        ),
        "formal_git_gate_manifest_raw_head_exact": tracked_path_exact(
            DEFAULT_GATE_MANIFEST.as_posix()
        ),
        "formal_git_source_metadata_raw_head_exact": (
            metadata_path == DEFAULT_SOURCE_METADATA.as_posix()
            and tracked_path_exact(metadata_path)
        ),
        "formal_git_source_metadata_commit_exact": (
            libsmctrl_metadata["content"].get("source_commit")
            == expected_commit
            and gitlink is not None
            and libsmctrl_metadata["content"].get("source_commit")
            == gitlink.snapshot.head_oid
        ),
        "formal_git_asle_metadata_raw_head_exact": tracked_path_exact(
            "vendor/ASLE_SOURCE.json"
        ),
        **{
            f"formal_git_asle_{name}": passed
            for name, passed in asle_archive["checks"].items()
        },
        **build_checks,
    }
    record = {
        "snapshot": snapshot.to_dict(),
        "snapshot_identity_sha256": snapshot.identity_sha256,
        "libsmctrl_gitlink": (
            gitlink.to_dict() if gitlink is not None else None
        ),
        "build_untracked_exception": build_record,
        "asle_archive": asle_archive,
        "libsmctrl_source_metadata": libsmctrl_metadata,
        "checks": checks,
    }
    return record, checks, snapshot


def formal_source_binding(
    *,
    repo_root: Path,
    binary: Path,
    libsmctrl_root: Path,
    gate_manifest: Mapping[str, Any],
    build_record: Mapping[str, Any],
    launcher_identity: Mapping[str, Any] | None = None,
    build_attestation: Mapping[str, Any] | None = None,
    build_lock_record: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, bool]]:
    """Independently verify every attested build input and output."""

    canonical_binary = repo_root.resolve() / DEFAULT_BINARY
    canonical_real = canonical_binary.with_name(
        f"{canonical_binary.name}.real"
    )
    canonical_build_dir = canonical_binary.parent
    canonical_outputs = {
        "launcher": canonical_binary,
        "real_probe": canonical_real,
        "parent_guard_test_helper": (
            canonical_build_dir / "parent_guard_test_helper"
        ),
        "real_probe_identity_header": (
            canonical_build_dir / "real_probe_identity.h"
        ),
        "guard_exec_test_launcher": (
            canonical_build_dir / "guard_exec_test_launcher"
        ),
        "guard_exec_test_fixture": (
            canonical_build_dir / "guard_exec_test_launcher.real"
        ),
        "guard_exec_test_identity_header": (
            canonical_build_dir / "guard_exec_test_identity.h"
        ),
    }
    stamp_path = canonical_build_dir / "build-config.stamp"
    attestation_path = canonical_build_dir / "build-attestation.json"
    checks: dict[str, bool] = {
        "binary_is_repo_canonical": binary == canonical_binary,
        "libsmctrl_is_repo_canonical": (
            libsmctrl_root == repo_root.resolve() / DEFAULT_LIBSMCTRL_ROOT
        ),
    }

    try:
        stamp_bytes = _read_bounded_regular_bytes(
            stamp_path,
            label="native build stamp",
            maximum_bytes=MAX_BUILD_STAMP_BYTES,
        )
        stamp_content = stamp_bytes.decode("utf-8", errors="strict")
        stamp_fields = _parse_build_stamp_strict(stamp_content)
        stamp_sha256 = hashlib.sha256(stamp_bytes).hexdigest()
    except (OSError, UnicodeError, RuntimeError):
        stamp_content = ""
        stamp_fields = {}
        stamp_sha256 = None
    checks["build_stamp_schema_exact"] = bool(stamp_fields)

    try:
        descriptor, attestation_identity = _open_regular_nofollow(
            attestation_path
        )
        try:
            attestation_bytes = _read_open_attestation(
                descriptor,
                attestation_identity,
            )
        finally:
            os.close(descriptor)
        parsed_attestation = _parse_native_build_attestation(
            attestation_bytes
        )
        if (
            build_attestation is not None
            and parsed_attestation != dict(build_attestation)
        ):
            raise RuntimeError(
                "provided build attestation does not match its on-disk bytes"
            )
        attestation_value = parsed_attestation
    except (OSError, RuntimeError, TypeError, ValueError):
        attestation_value = {}
        attestation_identity = {
            "path": str(attestation_path),
            "sha256": None,
        }
        attestation_bytes = b""

    checks["attestation_schema_exact"] = (
        attestation_value.get("schema_version")
        == NATIVE_BUILD_ATTESTATION_SCHEMA_VERSION
    )
    checks["attestation_top_level_keys_exact"] = (
        set(attestation_value) == NATIVE_BUILD_ATTESTATION_TOP_LEVEL_KEYS
    )
    checks["attestation_canonical_serialization"] = (
        attestation_bytes
        == _native_attestation_canonical_bytes(attestation_value)
    )
    attested_stamp = attestation_value.get("build_stamp")
    attested_stamp = (
        attested_stamp if isinstance(attested_stamp, Mapping) else {}
    )
    checks.update(
        {
            "attested_stamp_keys_exact": set(attested_stamp)
            == {"path", "sha256", "fields", "metadata"},
            "attested_stamp_path_exact": (
                attested_stamp.get("path") == str(stamp_path)
            ),
            "attested_stamp_hash_exact": (
                stamp_sha256 is not None
                and attested_stamp.get("sha256") == stamp_sha256
            ),
            "attested_stamp_fields_exact": (
                bool(stamp_fields)
                and attested_stamp.get("fields") == stamp_fields
            ),
        }
    )
    try:
        stamp_status = stamp_path.stat(follow_symlinks=False)
        stamp_metadata = {
            "file_type": "regular",
            "uid": stamp_status.st_uid,
            "gid": stamp_status.st_gid,
            "mode_octal": f"{stat.S_IMODE(stamp_status.st_mode):04o}",
            "nlink": stamp_status.st_nlink,
            "device": stamp_status.st_dev,
            "inode": stamp_status.st_ino,
            "xattrs": _xattr_records(stamp_path),
        }
    except OSError:
        stamp_metadata = {}
    checks["attested_stamp_metadata_exact"] = (
        attested_stamp.get("metadata") == stamp_metadata
        and stamp_metadata.get("uid") == os.geteuid()
        and stamp_metadata.get("mode_octal") == "0400"
        and stamp_metadata.get("nlink") == 1
    )

    source_hashes: dict[str, str | None] = {}
    for field, relative in BUILD_SOURCE_PATHS.items():
        path = repo_root / relative
        try:
            source_content = _read_bounded_regular_bytes(
                path,
                label=f"formal build source {relative}",
                maximum_bytes=MAX_FORMAL_SOURCE_FILE_BYTES,
            )
            digest = hashlib.sha256(source_content).hexdigest()
        except (OSError, RuntimeError):
            digest = None
        source_hashes[field] = digest
        checks[f"source_{field}_matches_stamp"] = (
            digest is not None and stamp_fields.get(field) == digest
        )
    attested_source_hashes = attestation_value.get("source_hashes")
    checks["attested_source_hashes_exact"] = (
        isinstance(attested_source_hashes, Mapping)
        and dict(attested_source_hashes)
        == {
            field: stamp_fields.get(field)
            for field in BUILD_SOURCE_PATHS
        }
        | {
            "LIBSMCTRL_GIT_STATUS_SHA256": stamp_fields.get(
                "LIBSMCTRL_GIT_STATUS_SHA256"
            ),
            "LIBSMCTRL_GIT_SNAPSHOT_SHA256": stamp_fields.get(
                "LIBSMCTRL_GIT_SNAPSHOT_SHA256"
            ),
        }
    )
    verifier_record: dict[str, Any]
    if (
        bool(stamp_fields)
        and all(
            checks.get(f"source_{field}_matches_stamp") is True
            for field in BUILD_SOURCE_PATHS
        )
    ):
        try:
            verifier_record = _verify_attestation_with_pinned_builder(
                repo_root=repo_root,
                stamp_fields=stamp_fields,
                stamp_path=stamp_path,
                attestation_path=attestation_path,
            )
        except (
            OSError,
            KeyError,
            RuntimeError,
            subprocess.TimeoutExpired,
        ) as error:
            verifier_record = {
                "passed": False,
                "error": f"{type(error).__name__}: {error}",
            }
    else:
        verifier_record = {
            "passed": False,
            "error": "source/stamp validation failed before verifier launch",
        }
    checks["pinned_full_attestation_verifier_passed"] = (
        verifier_record.get("passed") is True
    )

    path_hash_pairs = (
        ("CUDA_RUNTIME_LIBRARY", "CUDA_RUNTIME_LIBRARY_SHA256"),
        ("CUDA_VERSION_FILE", "CUDA_VERSION_FILE_SHA256"),
        ("LIBCUDA_LINK_LIBRARY", "LIBCUDA_LINK_LIBRARY_SHA256"),
        ("NVCC_EXECUTABLE", "NVCC_EXECUTABLE_SHA256"),
        ("CC_EXECUTABLE", "CC_EXECUTABLE_SHA256"),
        ("AR_EXECUTABLE", "AR_EXECUTABLE_SHA256"),
        ("PYTHON_EXECUTABLE", "PYTHON_EXECUTABLE_SHA256"),
    )
    for path_field, hash_field in path_hash_pairs:
        path_value = stamp_fields.get(path_field)
        try:
            path = Path(str(path_value))
            actual_hash = (
                _sha256_file(path)
                if path.is_absolute() and path.is_file()
                else None
            )
        except OSError:
            actual_hash = None
        checks[f"toolchain_{hash_field}_exact"] = (
            actual_hash is not None
            and stamp_fields.get(hash_field) == actual_hash
        )
    try:
        include_root = Path(stamp_fields["CUDA_INCLUDE_ROOT"])
        include_digest = _tree_digest(include_root)
    except (KeyError, OSError, RuntimeError):
        include_digest = None
    checks["cuda_include_tree_hash_exact"] = (
        include_digest is not None
        and stamp_fields.get("CUDA_INCLUDE_TREE_SHA256")
        == include_digest
    )
    try:
        libdevice_root = Path(stamp_fields["CUDA_LIBDEVICE_ROOT"])
        libdevice_digest = _tree_digest(libdevice_root)
    except (KeyError, OSError, RuntimeError):
        libdevice_digest = None
    checks["cuda_libdevice_tree_hash_exact"] = (
        libdevice_digest is not None
        and stamp_fields.get("CUDA_LIBDEVICE_TREE_SHA256")
        == libdevice_digest
    )
    for command_field, version_field in (
        ("NVCC_EXECUTABLE", "NVCC_VERSION_SHA256"),
        ("CC_EXECUTABLE", "CC_VERSION_SHA256"),
        ("AR_EXECUTABLE", "AR_VERSION_SHA256"),
        ("PYTHON_EXECUTABLE", "PYTHON_VERSION_SHA256"),
    ):
        try:
            hermetic_environment = {
                "LANG": "C",
                "LC_ALL": "C",
                "TZ": "UTC",
                "SOURCE_DATE_EPOCH": "0",
                "CUDA_CACHE_DISABLE": "1",
                "PATH": stamp_fields["HERMETIC_PATH"],
                "TMPDIR": stamp_fields["BUILD_TMPDIR"],
            }
            version_digest = _command_version_sha256(
                stamp_fields[command_field],
                environment=hermetic_environment,
                version_arguments=_tool_version_arguments(command_field),
            )
        except (KeyError, OSError, RuntimeError, subprocess.TimeoutExpired):
            version_digest = None
        checks[f"toolchain_{version_field}_exact"] = (
            version_digest is not None
            and stamp_fields.get(version_field) == version_digest
        )

    output_records = attestation_value.get("outputs")
    output_records = (
        output_records if isinstance(output_records, Mapping) else {}
    )
    checks["attestation_output_keys_exact"] = set(output_records) == set(
        canonical_outputs
    )
    actual_outputs: dict[str, dict[str, Any]] = {}
    for name, path in canonical_outputs.items():
        try:
            actual, output_checks = _artifact_record_matches(
                output_records.get(name),
                path,
                expected_mode=(
                    0o400 if name.endswith("identity_header") else 0o500
                ),
            )
        except (OSError, RuntimeError):
            actual = {"path": str(path), "sha256": None}
            output_checks = {"artifact_accessible": False}
        actual_outputs[name] = actual
        checks.update(
            {
                f"output_{name}_{key}": passed
                for key, passed in output_checks.items()
            }
        )

    launcher_actual = (
        dict(launcher_identity)
        if launcher_identity is not None
        else actual_outputs["launcher"]
    )
    launcher_attested = output_records.get("launcher")
    launcher_attested = (
        launcher_attested
        if isinstance(launcher_attested, Mapping)
        else {}
    )
    checks["opened_launcher_fd_matches_attested_output"] = (
        launcher_actual.get("sha256")
        == launcher_attested.get("sha256")
        and launcher_actual.get("device")
        == actual_outputs["launcher"].get("device")
        and launcher_actual.get("inode")
        == actual_outputs["launcher"].get("inode")
    )

    try:
        actual_dependencies = _runtime_dependencies(canonical_real)
    except (OSError, RuntimeError, UnicodeError, subprocess.TimeoutExpired):
        actual_dependencies = []
    checks["runtime_dependencies_exact"] = (
        attestation_value.get("real_probe_runtime_dependencies")
        == actual_dependencies
        and bool(actual_dependencies)
    )
    try:
        actual_guard_dependencies = _runtime_dependencies(
            canonical_outputs["guard_exec_test_fixture"]
        )
    except (OSError, RuntimeError, UnicodeError, subprocess.TimeoutExpired):
        actual_guard_dependencies = []
    checks["guard_fixture_runtime_dependencies_exact"] = (
        attestation_value.get("guard_fixture_runtime_dependencies")
        == actual_guard_dependencies
        and bool(actual_guard_dependencies)
    )

    formal_git_record, formal_git_checks, formal_git_snapshot = (
        _formal_git_source_policy(
            repo_root=repo_root,
            gate_manifest=gate_manifest,
            attestation=attestation_value,
            attestation_identity=attestation_identity,
        )
    )
    checks.update(formal_git_checks)
    formal_gitlink = next(
        (
            item
            for item in formal_git_snapshot.gitlinks
            if item.path == os.fsencode(DEFAULT_LIBSMCTRL_ROOT.as_posix())
        ),
        None,
    )
    git_commit = (
        formal_gitlink.snapshot.head_oid
        if formal_gitlink is not None
        else None
    )
    git_status = (
        ""
        if formal_gitlink is not None and formal_gitlink.clean
        else None
    )
    git_status_sha256 = (
        hashlib.sha256(git_status.encode("utf-8")).hexdigest()
        if git_status is not None
        else None
    )
    source_manifest = gate_manifest.get("source")
    source_manifest = (
        source_manifest if isinstance(source_manifest, Mapping) else {}
    )
    checks.update(
        {
            "libsmctrl_git_commit_exact": (
                git_commit
                == stamp_fields.get("LIBSMCTRL_GIT_COMMIT")
                == source_manifest.get("libsmctrl_commit")
            ),
            "libsmctrl_git_status_clean": git_status == "",
            "libsmctrl_git_status_hash_exact": (
                git_status_sha256
                == stamp_fields.get("LIBSMCTRL_GIT_STATUS_SHA256")
            ),
            "libsmctrl_dirty_stamp_clean": (
                stamp_fields.get("LIBSMCTRL_GIT_DIRTY") == "clean"
            ),
            "libsmctrl_git_snapshot_hash_exact": (
                formal_gitlink is not None
                and _valid_sha256(
                    stamp_fields.get("LIBSMCTRL_GIT_SNAPSHOT_SHA256")
                )
                and formal_gitlink.snapshot.identity_sha256
                == stamp_fields.get("LIBSMCTRL_GIT_SNAPSHOT_SHA256")
            ),
        }
    )

    attestation_sha256 = attestation_identity.get("sha256")
    pins = {
        "launcher": (
            source_manifest.get("approved_launcher_sha256"),
            launcher_actual.get("sha256"),
        ),
        "real_probe": (
            source_manifest.get("approved_real_probe_sha256"),
            actual_outputs["real_probe"].get("sha256"),
        ),
        "build_stamp": (
            source_manifest.get("approved_build_stamp_sha256"),
            stamp_sha256,
        ),
        "build_attestation": (
            source_manifest.get("approved_build_attestation_sha256"),
            attestation_sha256,
        ),
    }
    for name, (declared, actual) in pins.items():
        checks[f"manifest_{name}_pin_valid"] = _valid_sha256(declared)
        checks[f"manifest_{name}_pin_exact"] = (
            _valid_sha256(declared) and declared == actual
        )

    attested_checks = attestation_value.get("checks")
    checks["attestation_internal_checks_all_true"] = (
        isinstance(attested_checks, Mapping)
        and bool(attested_checks)
        and all(value is True for value in attested_checks.values())
    )
    checks["attestation_internal_check_keys_exact"] = (
        isinstance(attested_checks, Mapping)
        and set(attested_checks)
        == {
            "post_link_stamp_matches_current_inputs",
            "libsmctrl_worktree_clean",
            "launcher_is_static",
            "test_launcher_is_static",
            "test_helper_is_static",
            "launcher_identity_matches_real_probe",
            "test_launcher_identity_matches_fixture",
            "output_metadata_and_xattrs_accepted",
            "build_lock_metadata_accepted",
        }
    )
    launcher_contract = attestation_value.get("launcher_contract")
    launcher_contract = (
        launcher_contract
        if isinstance(launcher_contract, Mapping)
        else {}
    )
    snapshot_contract = launcher_contract.get("snapshot")
    snapshot_contract = (
        snapshot_contract
        if isinstance(snapshot_contract, Mapping)
        else {}
    )
    exec_contract = launcher_contract.get("exec")
    exec_contract = (
        exec_contract if isinstance(exec_contract, Mapping) else {}
    )
    signal_contract = launcher_contract.get("lifecycle_signals")
    signal_contract = (
        signal_contract if isinstance(signal_contract, Mapping) else {}
    )
    descriptor_contract = launcher_contract.get(
        "inherited_file_descriptors"
    )
    descriptor_contract = (
        descriptor_contract
        if isinstance(descriptor_contract, Mapping)
        else {}
    )
    launcher_identity_contract = launcher_contract.get("identity")
    launcher_identity_contract = (
        launcher_identity_contract
        if isinstance(launcher_identity_contract, Mapping)
        else {}
    )
    checks.update(
        {
            "launcher_contract_keys_exact": set(launcher_contract)
            == {
                "static_elf",
                "identity",
                "source_open_flags",
                "snapshot",
                "seals",
                "exec",
                "parent_guard",
                "lifecycle_signals",
                "environment",
                "inherited_file_descriptors",
                "production_fault_injection",
            },
            "launcher_contract_static": (
                launcher_contract.get("static_elf") is True
            ),
            "launcher_contract_real_identity_exact": (
                launcher_identity_contract.get("sha256")
                == actual_outputs["real_probe"].get("sha256")
                and launcher_identity_contract.get("size_bytes")
                == actual_outputs["real_probe"].get("size")
            ),
            "launcher_contract_open_flags_exact": (
                launcher_contract.get("source_open_flags")
                == [
                    "O_RDONLY",
                    "O_CLOEXEC",
                    "O_NOFOLLOW",
                    "O_NONBLOCK",
                ]
            ),
            "launcher_contract_sealed_memfd_exact": (
                snapshot_contract.get("mechanism") == "memfd_create"
                and snapshot_contract.get("flags")
                == ["MFD_CLOEXEC", "MFD_ALLOW_SEALING", "MFD_EXEC"]
                and snapshot_contract.get("copy_limit")
                == "expected_size_plus_one"
                and snapshot_contract.get("mode_octal") == "0500"
                and snapshot_contract.get(
                    "post_seal_fstat_hash_and_elf_validation"
                )
                is True
                and snapshot_contract.get("legacy_retry_without_mfd_exec")
                is False
            ),
            "launcher_contract_execveat_exact": (
                exec_contract.get("mechanism")
                == "syscall(SYS_execveat)"
                and exec_contract.get("flags") == ["AT_EMPTY_PATH"]
                and exec_contract.get("pathname_fallback") is False
            ),
            "launcher_contract_seals_exact": (
                isinstance(launcher_contract.get("seals"), Mapping)
                and launcher_contract["seals"]
                == {
                    "required": [
                        "F_SEAL_WRITE",
                        "F_SEAL_GROW",
                        "F_SEAL_SHRINK",
                        "F_SEAL_SEAL",
                    ],
                    "execution_bit_seal_when_supported": "F_SEAL_EXEC",
                    "verified_with": "F_GET_SEALS",
                }
            ),
            "launcher_contract_parent_guard_exact": (
                launcher_contract.get("parent_guard")
                == (
                    "PR_SET_PDEATHSIG=SIGKILL+immediate_getppid+"
                    "post_exec_rearm"
                )
            ),
            "launcher_contract_signal_reset_exact": (
                signal_contract.get("reset_to_default")
                == ["SIGHUP", "SIGINT", "SIGTERM"]
                and signal_contract.get("unblocked")
                == ["SIGHUP", "SIGINT", "SIGTERM"]
            ),
            "launcher_contract_fd_policy_exact": (
                descriptor_contract.get("preserved") == [0, 1, 2]
                and descriptor_contract.get("sealed_exec_fd") == 3
                and descriptor_contract.get("close_range_from") == 4
                and descriptor_contract.get("exec_fd_cloexec") is True
            ),
            "launcher_contract_environment_exact": (
                launcher_contract.get("environment")
                == {
                    "removed": [
                        "LD_*",
                        "GLIBC_TUNABLES",
                        "GCONV_PATH",
                        "LOCPATH",
                        "NLSPATH",
                        "CUDA_INJECTION*",
                    ],
                    "preserved_classes": [
                        "CUDA_VISIBLE_DEVICES",
                        "CUDA_MPS_PIPE_DIRECTORY",
                        "MASK_OFF",
                        "BURSTSERVE_PARENT_PID",
                    ],
                }
            ),
            "launcher_contract_fault_injection_absent": (
                launcher_contract.get("production_fault_injection") is False
            ),
            "attested_build_environment_present": isinstance(
                attestation_value.get("build_environment"),
                Mapping,
            ),
            "attested_toolchain_present": isinstance(
                attestation_value.get("toolchain"),
                Mapping,
            ),
            "attested_toolchain_keys_exact": (
                isinstance(attestation_value.get("toolchain"), Mapping)
                and set(attestation_value["toolchain"])
                == {
                    "cuda_subtools",
                    "host_components",
                    "host_compiler_fingerprints",
                    "nvcc_dryrun_sha256",
                    "versions",
                }
            ),
            "finite_build_contract_present": isinstance(
                attestation_value.get("finite_build_contract"),
                Mapping,
            ),
        }
    )
    test_fixture_contract = attestation_value.get(
        "test_fixture_contract"
    )
    test_fixture_contract = (
        test_fixture_contract
        if isinstance(test_fixture_contract, Mapping)
        else {}
    )
    fixture_identity = test_fixture_contract.get("identity")
    fixture_identity = (
        fixture_identity if isinstance(fixture_identity, Mapping) else {}
    )
    checks.update(
        {
            "test_fixture_contract_keys_exact": set(test_fixture_contract)
            == {
                "launcher_static_elf",
                "fixture_cuda_linked",
                "fault_injection_compiled_only_into_test_launcher",
                "identity",
            },
            "test_fixture_contract_exact": (
                test_fixture_contract.get("launcher_static_elf") is True
                and test_fixture_contract.get("fixture_cuda_linked")
                is False
                and test_fixture_contract.get(
                    "fault_injection_compiled_only_into_test_launcher"
                )
                is True
                and fixture_identity.get("sha256")
                == actual_outputs["guard_exec_test_fixture"].get("sha256")
                and fixture_identity.get("size_bytes")
                == actual_outputs["guard_exec_test_fixture"].get("size")
            ),
        }
    )
    build_lock = attestation_value.get("build_lock")
    checks["attested_build_lock_keys_exact"] = (
        isinstance(build_lock, Mapping)
        and set(build_lock)
        == {
            "path",
            "uid",
            "gid",
            "mode_octal",
            "nlink",
            "xattrs",
            "directory_path",
            "directory_uid",
            "directory_gid",
            "directory_mode_octal",
            "directory_xattrs",
            "inherited_lock_fd",
        }
    )
    checks["attested_build_lock_matches_held_lock"] = (
        isinstance(build_lock, Mapping)
        and isinstance(build_lock_record, Mapping)
        and build_lock.get("path") == build_lock_record.get("path")
    )
    if isinstance(build_lock, Mapping):
        try:
            lock_path = Path(str(build_lock["path"]))
            lock_status = lock_path.stat(follow_symlinks=False)
            lock_directory = lock_path.parent
            directory_status = lock_directory.stat(follow_symlinks=False)
            actual_lock_record = {
                "path": str(lock_path.resolve(strict=True)),
                "uid": lock_status.st_uid,
                "gid": lock_status.st_gid,
                "mode_octal": f"{stat.S_IMODE(lock_status.st_mode):04o}",
                "nlink": lock_status.st_nlink,
                "xattrs": _xattr_records(lock_path),
                "directory_path": str(lock_directory.resolve(strict=True)),
                "directory_uid": directory_status.st_uid,
                "directory_gid": directory_status.st_gid,
                "directory_mode_octal": (
                    f"{stat.S_IMODE(directory_status.st_mode):04o}"
                ),
                "directory_xattrs": _xattr_records(lock_directory),
                "inherited_lock_fd": 9,
            }
        except (OSError, KeyError):
            actual_lock_record = {}
    else:
        actual_lock_record = {}
    checks["attested_build_lock_metadata_exact"] = (
        dict(build_lock) == actual_lock_record
        and actual_lock_record.get("uid") == os.geteuid()
        and actual_lock_record.get("mode_octal") == "0600"
        and actual_lock_record.get("nlink") == 1
        and actual_lock_record.get("directory_uid") == os.geteuid()
        and actual_lock_record.get("directory_mode_octal") == "0700"
    )

    source_snapshot = {
        "attestation_sha256": attestation_sha256,
        "stamp_sha256": stamp_sha256,
        "source_hashes": source_hashes,
        "outputs": actual_outputs,
        "runtime_dependencies": actual_dependencies,
        "guard_fixture_runtime_dependencies": actual_guard_dependencies,
        "pinned_attestation_verifier": verifier_record,
        "launcher_fd": launcher_actual,
        "build_lock": dict(build_lock or {}),
        "formal_git": formal_git_record,
        "git_commit": git_commit,
        "git_status_sha256": git_status_sha256,
        "git_snapshot_sha256": (
            formal_gitlink.snapshot.identity_sha256
            if formal_gitlink is not None
            else None
        ),
    }
    record = {
        "canonical_binary": str(canonical_binary),
        "canonical_real_probe": str(canonical_real),
        "requested_binary": str(binary),
        "canonical_libsmctrl_root": str(
            repo_root.resolve() / DEFAULT_LIBSMCTRL_ROOT
        ),
        "requested_libsmctrl_root": str(libsmctrl_root),
        "build_stamp": {
            "path": str(stamp_path),
            "sha256": stamp_sha256,
            "fields": stamp_fields,
        },
        "build_attestation": {
            "path": str(attestation_path),
            "sha256": attestation_sha256,
            "content": dict(attestation_value),
        },
        "source_hashes": source_hashes,
        "outputs": actual_outputs,
        "runtime_dependencies": actual_dependencies,
        "guard_fixture_runtime_dependencies": actual_guard_dependencies,
        "pinned_attestation_verifier": verifier_record,
        "launcher_fd_identity": launcher_actual,
        "build_lock": dict(build_lock or {}),
        "held_build_lock": dict(build_lock_record or {}),
        "formal_git": formal_git_record,
        "checks": checks,
        "source_snapshot_sha256": hashlib.sha256(
            canonical_json(source_snapshot).encode("utf-8")
        ).hexdigest(),
    }
    return record, checks


def evaluate_formal_source_policy(
    checks: Mapping[str, bool],
    *,
    mode: str,
) -> tuple[dict[str, bool], bool, bool, bool]:
    """Return required checks, canonical selection, local and launch policy."""

    required = dict(checks)
    canonical = bool(
        checks.get("binary_is_repo_canonical")
        and checks.get("libsmctrl_is_repo_canonical")
    )
    local_eligible = canonical and all(required.values())
    # The GPU-backed formal execute path is canonical-only for every mode.
    # External binaries belong to a separate non-GPU exploration tool and can
    # never be accepted by this function.
    launch_permitted = local_eligible
    return required, canonical, local_eligible, launch_permitted


_FORMAL_GIT_TREE_REQUIRED_CHECKS = frozenset(
    {
        "formal_git_snapshot_complete",
        "formal_git_head_available",
        "formal_git_no_staged_changes",
        "formal_git_no_tracked_worktree_changes",
        "formal_git_libsmctrl_registered_clean",
        "formal_git_libsmctrl_commit_exact",
        "formal_git_gate_manifest_raw_head_exact",
        "formal_git_source_metadata_raw_head_exact",
        "formal_git_source_metadata_commit_exact",
        "formal_git_asle_metadata_raw_head_exact",
        "formal_git_asle_archive_entry_unique",
        "formal_git_asle_archive_is_regular",
        "formal_git_asle_archive_size_exact",
        "formal_git_asle_archive_sha256_exact",
        "formal_git_asle_archive_mode_read_only_data",
        "formal_git_build_output_keys_exact",
        "formal_git_build_records_canonical",
        "formal_git_build_exception_paths_exact",
        "formal_git_build_exception_files_attested",
        "formal_git_build_exception_directories_private",
    }
)


def _formal_source_tree_policy_clean(checks: Mapping[str, bool]) -> bool:
    """Interpret exact reviewed untracked exceptions as formal-clean."""

    formal_git_checks = {
        name: passed
        for name, passed in checks.items()
        if name.startswith("formal_git_")
    }
    return bool(
        _FORMAL_GIT_TREE_REQUIRED_CHECKS <= set(formal_git_checks)
        and all(formal_git_checks.values())
    )


def revalidate_formal_source_binding(
    *,
    phase: str,
    repo_root: Path,
    binary: Path,
    libsmctrl_root: Path,
    gate_manifest: Mapping[str, Any],
    mode: str,
    expected_snapshot_sha256: str,
    launcher_identity: Mapping[str, Any] | None = None,
    build_lock_record: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Rehash and restat every formal source artifact at a lifecycle boundary."""

    build_record = native_build_record(binary)
    binding, checks = formal_source_binding(
        repo_root=repo_root,
        binary=binary,
        libsmctrl_root=libsmctrl_root,
        gate_manifest=gate_manifest,
        build_record=build_record,
        launcher_identity=launcher_identity,
        build_lock_record=build_lock_record,
    )
    required, canonical, local_eligible, launch_permitted = (
        evaluate_formal_source_policy(checks, mode=mode)
    )
    observed_snapshot = binding.get("source_snapshot_sha256")
    snapshot_matches = (
        isinstance(observed_snapshot, str)
        and observed_snapshot == expected_snapshot_sha256
    )
    return {
        "phase": phase,
        "completed": True,
        "error": None,
        "build_record": build_record,
        "binding": binding,
        "checks": checks,
        "required_checks": required,
        "canonical_paths_selected": canonical,
        "source_eligible_for_local_pass": local_eligible,
        "formal_source_launch_permitted": launch_permitted,
        "expected_snapshot_sha256": expected_snapshot_sha256,
        "observed_snapshot_sha256": observed_snapshot,
        "snapshot_matches_initial": snapshot_matches,
        "passed_for_launch": launch_permitted and snapshot_matches,
        "passed_for_local_acceptance": local_eligible and snapshot_matches,
    }


def _load_attestation_bootstrap(
    binary: Path,
    *,
    expected_sha256: str,
    expected_identity: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read only enough attestation to safely acquire its build lock."""

    attestation_path = binary.parent / "build-attestation.json"
    descriptor, identity = _open_regular_nofollow(attestation_path)
    try:
        if (
            expected_identity is not None
            and not _attestation_identity_matches(
                identity,
                expected_identity,
            )
        ):
            raise RuntimeError(
                "build attestation identity changed after it was hashed"
            )
        content = _read_open_attestation(descriptor, identity)
    finally:
        os.close(descriptor)
    if identity["sha256"] != expected_sha256:
        raise RuntimeError(
            "build attestation does not match the Gate-A manifest pin"
        )
    record = _parse_native_build_attestation(content)
    build_lock = record.get("build_lock")
    if not isinstance(build_lock, Mapping):
        raise RuntimeError("build attestation is missing build_lock")
    path_value = build_lock.get("path")
    owner_uid = build_lock.get("uid")
    mode = build_lock.get("mode_octal")
    if not isinstance(path_value, str) or not Path(path_value).is_absolute():
        raise RuntimeError("attested build lock path must be absolute")
    if owner_uid != os.geteuid():
        raise RuntimeError("attested build lock owner does not match euid")
    if mode != "0600":
        raise RuntimeError("attested build lock mode is not owner-private")
    expected_path = (
        Path("/run/user")
        / str(os.geteuid())
        / "burstserve-smctrl-probe"
        / "build.lock"
    )
    if Path(path_value) != expected_path:
        raise RuntimeError(
            "attested build lock is not the deterministic owner-private path"
        )
    directory = expected_path.parent
    directory_status = directory.stat(follow_symlinks=False)
    lock_status = expected_path.stat(follow_symlinks=False)
    if (
        not stat.S_ISDIR(directory_status.st_mode)
        or directory_status.st_uid != os.geteuid()
        or stat.S_IMODE(directory_status.st_mode) != 0o700
        or build_lock.get("directory_path") != str(directory)
        or build_lock.get("directory_uid") != directory_status.st_uid
        or build_lock.get("directory_gid") != directory_status.st_gid
        or build_lock.get("directory_mode_octal") != "0700"
        or build_lock.get("directory_xattrs")
        != _xattr_records(directory)
        or not stat.S_ISREG(lock_status.st_mode)
        or lock_status.st_uid != os.geteuid()
        or lock_status.st_nlink != 1
        or stat.S_IMODE(lock_status.st_mode) != 0o600
        or build_lock.get("gid") != lock_status.st_gid
        or build_lock.get("nlink") != 1
        or build_lock.get("xattrs") != _xattr_records(expected_path)
        or build_lock.get("inherited_lock_fd") != 9
    ):
        raise RuntimeError("attested build lock metadata is invalid")
    identity["content_size"] = len(content)
    return record, identity


def execute(
    *,
    repo_root: Path,
    binary: Path,
    libsmctrl_root: Path,
    run_root: Path,
    config: Mapping[str, Any],
    timeout_s: float,
    maximum_used_mib: int,
    allow_busy_gpu: bool,
) -> tuple[int, Path]:
    """Run one formally attested probe while holding all lifecycle leases."""

    if binary != repo_root.resolve() / DEFAULT_BINARY:
        raise RuntimeError(
            "formal execute rejects external binaries; use the separate "
            "non-GPU exploration path"
        )
    if libsmctrl_root != repo_root.resolve() / DEFAULT_LIBSMCTRL_ROOT:
        raise RuntimeError(
            "formal execute requires the repository-canonical libsmctrl"
        )

    embedded = config.get("gate_manifest")
    content = embedded.get("content") if isinstance(embedded, Mapping) else None
    if (
        not isinstance(content, Mapping)
        or content.get("schema_version") != GATE_MANIFEST_SCHEMA_VERSION
    ):
        raise RuntimeError("formal execute requires Gate-A manifest schema v2")
    source = content.get("source") if isinstance(content, Mapping) else None
    expected_attestation_sha256 = (
        source.get("approved_build_attestation_sha256")
        if isinstance(source, Mapping)
        else None
    )
    if not _valid_sha256(expected_attestation_sha256):
        raise RuntimeError(
            "formal execute requires a Gate-A v2 build-attestation pin"
        )
    bootstrap, bootstrap_identity = _load_attestation_bootstrap(
        binary,
        expected_sha256=str(expected_attestation_sha256),
    )
    build_lock = bootstrap["build_lock"]
    with ExitStack() as resources:
        lock = resources.enter_context(
            _FlockLease(
                Path(str(build_lock["path"])),
                operation=fcntl.LOCK_SH,
                kind="native_build",
            )
        )
        # Re-open after acquiring the shared build lock. Any bootstrap race is
        # rejected before the launcher descriptor can be opened.
        locked_attestation, locked_identity = _load_attestation_bootstrap(
            binary,
            expected_sha256=str(expected_attestation_sha256),
            expected_identity=bootstrap_identity,
        )
        if (
            locked_identity != bootstrap_identity
            or locked_attestation.get("build_lock") != bootstrap.get("build_lock")
        ):
            raise RuntimeError(
                "build attestation changed while acquiring the build lock"
            )
        launcher_descriptor, launcher_identity = _open_regular_nofollow(binary)
        resources.callback(os.close, launcher_descriptor)
        return _execute_under_leases(
            repo_root=repo_root,
            binary=binary,
            libsmctrl_root=libsmctrl_root,
            run_root=run_root,
            config=config,
            timeout_s=timeout_s,
            maximum_used_mib=maximum_used_mib,
            allow_busy_gpu=allow_busy_gpu,
            resources=resources,
            build_lock_record=dict(lock.record or {}),
            launcher_descriptor=launcher_descriptor,
            launcher_identity=launcher_identity,
            build_attestation=locked_attestation,
            build_attestation_identity=locked_identity,
        )


def _execute_under_leases(
    *,
    repo_root: Path,
    binary: Path,
    libsmctrl_root: Path,
    run_root: Path,
    config: Mapping[str, Any],
    timeout_s: float,
    maximum_used_mib: int,
    allow_busy_gpu: bool,
    resources: ExitStack,
    build_lock_record: Mapping[str, Any],
    launcher_descriptor: int,
    launcher_identity: Mapping[str, Any],
    build_attestation: Mapping[str, Any],
    build_attestation_identity: Mapping[str, Any],
) -> tuple[int, Path]:
    """Run one probe and return ``(runner_exit_code, run_directory)``."""

    if timeout_s <= 0:
        raise ValueError("timeout_s must be positive")
    if not binary.is_file():
        raise FileNotFoundError(f"native SM-ID probe not found: {binary}")
    if config.get("schema_version") != CELL_SCHEMA_VERSION:
        raise ValueError(
            "unsupported SM-ID probe cell schema: "
            f"{config.get('schema_version')!r}"
        )

    def config_integer(name: str, *, minimum: int | None = None) -> int:
        value = config.get(name)
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or (minimum is not None and value < minimum)
        ):
            suffix = (
                " integer"
                if minimum is None
                else f" integer >= {minimum}"
            )
            raise ValueError(f"config {name!r} must be an{suffix}")
        return value

    physical_gpu = config_integer("physical_gpu", minimum=0)
    mode_value = config.get("mode")
    if not isinstance(mode_value, str) or mode_value not in PROBE_MODES:
        raise ValueError(f"config mode must be one of {PROBE_MODES}")
    mode = mode_value
    enabled_tpc = config_integer("enabled_tpc", minimum=0)
    iterations = config_integer("iterations", minimum=1)
    blocks = config_integer("blocks", minimum=1)
    threads_per_block = config_integer("threads_per_block", minimum=1)
    trial = config_integer("trial", minimum=0)
    experimental_allow_value = config.get(
        "experimental_allow_unsupported_driver"
    )
    if not isinstance(experimental_allow_value, bool):
        raise ValueError(
            "config 'experimental_allow_unsupported_driver' must be bool"
        )
    experimental_allow = experimental_allow_value
    mask_off_raw = config.get("experimental_mask_off")
    if mask_off_raw is not None and (
        isinstance(mask_off_raw, bool)
        or not isinstance(mask_off_raw, int)
    ):
        raise ValueError(
            "config 'experimental_mask_off' must be an integer or null"
        )
    experimental_mask_off = mask_off_raw
    gate_manifest = validate_gate_manifest_record(
        config.get("gate_manifest"),
        repo_root=repo_root,
    )
    hardware = gate_manifest.get("hardware")
    if not isinstance(hardware, Mapping):
        raise RuntimeError("Gate-A manifest hardware section is invalid")

    gpu_preflight = query_gpu(physical_gpu)
    gpu_lease = resources.enter_context(
        _GpuLease(
            Path("/run/user")
            / str(os.geteuid())
            / "burstserve-smctrl-probe"
            / GPU_LOCK_DIRECTORY_NAME,
            str(gpu_preflight["uuid"]),
        )
    )
    effective_allow_busy = allow_busy_gpu and mode == "baseline"
    compute_processes_preflight = query_compute_processes(
        str(gpu_preflight["uuid"])
    )
    mps_processes_preflight = query_mps_processes()
    driver_probe = query_cuda_driver()
    if (
        not isinstance(driver_probe, Mapping)
        or isinstance(driver_probe.get("version"), bool)
        or not isinstance(driver_probe.get("version"), int)
        or int(driver_probe["version"]) <= 0
        or not isinstance(driver_probe.get("library_identity"), Mapping)
    ):
        raise RuntimeError("CUDA driver query returned malformed evidence")
    driver_version = int(driver_probe["version"])
    libcuda_identity = dict(driver_probe["library_identity"])
    latest_supported = latest_pinned_driver_version(repo_root)
    driver_policy_checks, driver_policy_permitted = evaluate_driver_policy(
        mode=mode,
        driver_version=driver_version,
        latest_pinned_version=latest_supported,
        experimental_allow_unsupported_driver=experimental_allow,
        experimental_mask_off=experimental_mask_off,
    )
    source_manifest = gate_manifest.get("source")
    source_manifest = (
        source_manifest if isinstance(source_manifest, Mapping) else {}
    )
    expected_libsmctrl_commit = source_manifest.get("libsmctrl_commit")
    if not _valid_git_oid(expected_libsmctrl_commit):
        raise RuntimeError(
            "validated Gate manifest lost its libsmctrl commit binding"
        )
    revision = source_revision(
        repo_root,
        libsmctrl_root,
        expected_libsmctrl_commit=expected_libsmctrl_commit,
    )
    build_record = native_build_record(binary)
    source_binding, formal_source_checks = formal_source_binding(
        repo_root=repo_root,
        binary=binary,
        libsmctrl_root=libsmctrl_root,
        gate_manifest=gate_manifest,
        build_record=build_record,
        launcher_identity=launcher_identity,
        build_attestation=build_attestation,
        build_lock_record=build_lock_record,
    )
    (
        formal_required_checks,
        canonical_paths_selected,
        source_eligible_for_local_pass,
        formal_source_preflight_permitted,
    ) = evaluate_formal_source_policy(
        formal_source_checks,
        mode=mode,
    )
    source_binding["required_checks_for_mode"] = formal_required_checks
    source_binding["approval_pins_required_for_mode"] = (
        mode in MASKED_MODES
    )
    source_binding["canonical_paths_selected"] = canonical_paths_selected
    source_binding["eligible_for_local_pass"] = (
        source_eligible_for_local_pass
    )
    runtime_libcuda_binding_checks = evaluate_libcuda_build_binding(
        libcuda_identity,
        source_binding,
    )
    initial_source_snapshot_sha256 = str(
        source_binding["source_snapshot_sha256"]
    )
    environment = capture_probe_environment(
        repo_root=repo_root,
        selected_gpu_uuid=str(gpu_preflight["uuid"]),
        expected_libsmctrl_commit=expected_libsmctrl_commit,
    )

    # The environment snapshot is intentionally comprehensive and can take
    # seconds. Re-run the physical-device checks immediately before launch.
    gpu_launch = query_gpu(physical_gpu)
    compute_processes_launch = query_compute_processes(str(gpu_launch["uuid"]))
    mps_processes_launch = query_mps_processes()
    manifest_policy_checks, manifest_policy_permitted = (
        evaluate_gate_manifest_policy(
            gate_manifest,
            mode=mode,
            physical_gpu=physical_gpu,
            gpu=gpu_launch,
            driver_version=driver_version,
            experimental_mask_off=experimental_mask_off,
            timeout_s=timeout_s,
            maximum_used_mib=maximum_used_mib,
            iterations=iterations,
            blocks=blocks,
            threads_per_block=threads_per_block,
            trial=trial,
            enabled_tpc=enabled_tpc,
        )
    )
    safety_policy_checks = {
        "gpu_uuid_stable_during_preflight": (
            gpu_preflight.get("uuid") == gpu_launch.get("uuid")
        ),
        "host_mps_state_recorded_at_initial_preflight": isinstance(
            mps_processes_preflight,
            list,
        ),
        "host_mps_state_recorded_at_launch_preflight": isinstance(
            mps_processes_launch,
            list,
        ),
        "mps_explicitly_bypassed": True,
        "busy_override_only_for_baseline": (
            not allow_busy_gpu or mode == "baseline"
        ),
        "memory_safe_at_initial_preflight": (
            int(gpu_preflight["memory_used_mib"]) <= maximum_used_mib
            or effective_allow_busy
        ),
        "memory_safe_at_launch_preflight": (
            int(gpu_launch["memory_used_mib"]) <= maximum_used_mib
            or effective_allow_busy
        ),
        "compute_processes_absent_at_initial_preflight_or_baseline_override": (
            not compute_processes_preflight or effective_allow_busy
        ),
        "compute_processes_absent_at_launch_preflight_or_baseline_override": (
            not compute_processes_launch or effective_allow_busy
        ),
        "timeout_recorded_in_config": config.get("timeout_s") == timeout_s,
        "memory_limit_recorded_in_config": (
            config.get("maximum_used_mib") == maximum_used_mib
        ),
        "busy_override_recorded_in_config": (
            config.get("allow_busy_gpu") is allow_busy_gpu
        ),
        "source_tree_is_formal_policy_clean": (
            _formal_source_tree_policy_clean(formal_source_checks)
        ),
        "native_build_stamp_is_present": build_record.get("found") is True,
        **runtime_libcuda_binding_checks,
    }
    preflight_permitted = (
        driver_policy_permitted
        and manifest_policy_permitted
        and formal_source_preflight_permitted
        and all(safety_policy_checks.values())
    )

    environment.update(
        {
            "selected_gpu_initial_preflight": gpu_preflight,
            "selected_gpu_launch_preflight": gpu_launch,
            "selected_gpu_compute_processes_initial": (
                compute_processes_preflight
            ),
            "selected_gpu_compute_processes_launch": compute_processes_launch,
            "host_mps_processes_initial": mps_processes_preflight,
            "host_mps_processes_launch": mps_processes_launch,
            "mps_bypass": {
                "CUDA_MPS_PIPE_DIRECTORY": "",
                "basis": "NVIDIA-documented empty-pipe-directory bypass",
            },
            "cuda_driver_api_version_preflight": driver_version,
            "cuda_driver_probe": dict(driver_probe),
            "runtime_libcuda_build_binding_checks": (
                runtime_libcuda_binding_checks
            ),
            "libsmctrl_latest_pinned_driver_api_version": latest_supported,
            "native_binary": {
                "path": str(binary),
                "sha256": launcher_identity["sha256"],
                "opened_fd_identity": dict(launcher_identity),
            },
            "native_build": build_record,
            "native_build_lock": dict(build_lock_record),
            "native_build_attestation": {
                "identity": dict(build_attestation_identity),
                "content": dict(build_attestation),
            },
            "gpu_lease": dict(gpu_lease.record or {}),
            "formal_source_binding": source_binding,
            "formal_launcher_threat_boundaries": dict(
                FORMAL_LAUNCHER_THREAT_BOUNDARIES
            ),
        }
    )
    manifest = RunManifest.create(
        config=config,
        seed=int(config.get("seed", 0)),
        source_revision=revision,
        environment=environment,
        metadata={
            "purpose": "phase1-libsmctrl-gate-a",
            "runner": RUNNER_VERSION,
        },
    )

    run_directory = run_root / manifest.run_id
    run_directory.mkdir(parents=True, exist_ok=False)
    # Enter the signal-restoration stack before terminal guards so any outer
    # unwind writes fail-closed evidence while lifecycle signals are still
    # handled by our deferring handler. The stack is explicitly drained on
    # the normal finalization path.
    signal_restore_stack = resources.enter_context(ExitStack())
    terminal_guard = resources.enter_context(
        _TerminalArtifactGuard(run_directory, manifest.run_id)
    )

    def quarantine_gpu_before_signal_restore() -> None:
        if gpu_lease.finalized or gpu_lease.descriptor is None:
            return
        gpu_lease.quarantine(
            ["supervisor_exited_without_terminal_artifact"],
            run_id=manifest.run_id,
        )
        gpu_lease.mark_terminal()

    resources.callback(quarantine_gpu_before_signal_restore)
    events_path = run_directory / "events.jsonl"
    write_json_atomic(run_directory / "manifest.json", manifest.to_dict())
    _record_event(
        events_path,
        run_id=manifest.run_id,
        sequence=0,
        event_type="run.preflight",
        payload={
            "gpu_initial": gpu_preflight,
            "gpu_launch": gpu_launch,
            "compute_processes_initial": compute_processes_preflight,
            "compute_processes_launch": compute_processes_launch,
            "mps_processes_initial": mps_processes_preflight,
            "mps_processes_launch": mps_processes_launch,
            "driver_version": driver_version,
            "cuda_driver_probe": dict(driver_probe),
            "runtime_libcuda_build_binding_checks": (
                runtime_libcuda_binding_checks
            ),
            "latest_pinned_driver_version": latest_supported,
            "driver_policy": driver_policy_checks,
            "driver_policy_permitted": driver_policy_permitted,
            "manifest_policy": manifest_policy_checks,
            "manifest_policy_permitted": manifest_policy_permitted,
            "safety_policy": safety_policy_checks,
            "preflight_permitted": preflight_permitted,
            "formal_source_binding": source_binding,
            "formal_source_checks": formal_source_checks,
            "formal_source_required_checks": formal_required_checks,
            "formal_source_preflight_permitted": (
                formal_source_preflight_permitted
            ),
            "source_eligible_for_local_pass": (
                source_eligible_for_local_pass
            ),
            "source_prelaunch_revalidation": None,
            "source_preexec_revalidation": None,
            "source_postrun_revalidation": None,
            "libcuda_final_revalidation": None,
            "launcher_fd_final": None,
            "formal_launcher_threat_boundaries": dict(
                FORMAL_LAUNCHER_THREAT_BOUNDARIES
            ),
        },
    )

    command = build_probe_command(
        binary=binary,
        mode=mode,
        enabled_tpc=enabled_tpc,
        iterations=iterations,
        blocks=blocks,
        experimental_allow_unsupported_driver=experimental_allow,
    )
    expected_parent_pid = os.getpid() if mode in MASKED_MODES else None
    child_environment = build_child_environment(
        selected_gpu_uuid=str(gpu_launch["uuid"]),
        experimental_mask_off=experimental_mask_off,
        parent_pid=expected_parent_pid,
    )
    command_record = {
        "argv": command,
        "cwd": str(repo_root),
        "prepared_at_utc": _utc_now(),
        "environment_overrides": dict(child_environment),
        "environment_policy": {
            "mode": "env-i exact allowlist",
            "allowed_names": sorted(child_environment),
            "inherited_names": [],
        },
        "parent_death_protection": {
            "mechanism": (
                "native_linux_prctl_pdeathsig_sigkill"
                if mode in MASKED_MODES
                else "not_required"
            ),
            "expected_parent_pid": expected_parent_pid,
            "runner_signal_handlers": ["SIGINT", "SIGHUP", "SIGTERM"],
            "residual": (
                "runner handlers and bounded process-group reap supplement "
                "the native PR_SET_PDEATHSIG guard"
                if mode in MASKED_MODES
                else None
            ),
        },
        "dynamic_loader_policy": {
            "mode": "env-i exact allowlist for every mode",
            "inherited_environment": False,
            "loader_and_cuda_tuning_variables_absent": all(
                not (
                    name.startswith("LD_")
                    or name
                    in {
                        "GLIBC_TUNABLES",
                        "GCONV_PATH",
                        "LOCPATH",
                        "NLSPATH",
                    }
                    or name.startswith("CUDA_INJECTION")
                    or (
                        name.startswith("CUDA_")
                        and name
                        not in {
                            "CUDA_CACHE_DISABLE",
                            "CUDA_VISIBLE_DEVICES",
                            "CUDA_MPS_PIPE_DIRECTORY",
                        }
                    )
                )
                for name in child_environment
            ),
        },
        "signal_mask_policy": {
            "runner_blocks_during_popen": [
                "SIGINT",
                "SIGHUP",
                "SIGTERM",
            ],
            "child_mask_reset_before_exec": True,
            "cleanup_policy": (
                "identity-bound SIGTERM/SIGKILL while waitid WNOWAIT "
                "retains the leader"
            ),
            "residual": None,
        },
        "launcher_fd": {
            "fd": launcher_descriptor,
            **dict(launcher_identity),
            "execution_path": f"/proc/self/fd/{launcher_descriptor}",
            "passed_explicitly": True,
        },
        "launcher_fd_final": None,
        "cuda_driver_probe": dict(driver_probe),
        "libcuda_final_revalidation": None,
        "formal_launcher_threat_boundaries": dict(
            FORMAL_LAUNCHER_THREAT_BOUNDARIES
        ),
    }
    write_json_atomic(run_directory / "command.json", command_record)

    if not preflight_permitted:
        write_text_atomic(run_directory / "stdout.log", "")
        write_text_atomic(
            run_directory / "stderr.log",
            "probe rejected by fail-closed preflight policy\n",
        )
        outcome = {
            "schema_version": OUTCOME_SCHEMA_VERSION,
            "completed_at_utc": _utc_now(),
            "exit_code": 4,
            "process_exit_code": None,
            "timed_out": False,
            "child_launch_error": None,
            "child_interruption": None,
            "process_group_reaped": True,
            "process_group_health": {
                "child_reaped": True,
                "process_group_quiesced": True,
                "process_group_reaped": True,
                "errors": [],
                "reason": "child_not_launched_preflight_rejected",
            },
            "quarantine_required": False,
            "native_output_found": False,
            "native_output_error": "child not launched: preflight rejected",
            "native_status": None,
            "driver_policy": driver_policy_checks,
            "driver_policy_permitted": driver_policy_permitted,
            "manifest_policy": manifest_policy_checks,
            "manifest_policy_permitted": manifest_policy_permitted,
            "safety_policy": safety_policy_checks,
            "preflight_permitted": False,
            "formal_source_binding": source_binding,
            "formal_source_checks": formal_source_checks,
            "formal_source_required_checks": formal_required_checks,
            "formal_source_preflight_permitted": (
                formal_source_preflight_permitted
            ),
            "source_eligible_for_local_pass": (
                source_eligible_for_local_pass
            ),
            "semantic_acceptance": {},
            "semantic_metrics": {},
            "masked_health_monitor_status": (
                "not_applicable"
                if mode not in MASKED_MODES
                else "not_started_preflight_rejected"
            ),
            "masked_health_monitor": _masked_monitor_record(
                mode=mode,
                status="not_started_preflight_rejected",
                provenance=None,
            ),
            "masked_health_monitor_checks": {},
            "local_probe_passed": False,
            "requires_matrix_validation": mode in MASKED_MODES,
            "accepted": False,
        }
        write_json_atomic(run_directory / "outcome.json", outcome)
        _record_event(
            events_path,
            run_id=manifest.run_id,
            sequence=1,
            event_type="run.rejected",
            payload=outcome,
        )
        if not safety_policy_checks["gpu_uuid_stable_during_preflight"]:
            gpu_lease.quarantine(
                ["gpu_uuid_changed_during_preflight"],
                gpu_initial=gpu_preflight,
                gpu_launch=gpu_launch,
            )
        gpu_lease.mark_terminal()
        terminal_guard.mark_terminal()
        return 4, run_directory

    event_sequence = 1
    try:
        source_prelaunch_revalidation = revalidate_formal_source_binding(
            phase="prelaunch",
            repo_root=repo_root,
            binary=binary,
            libsmctrl_root=libsmctrl_root,
            gate_manifest=gate_manifest,
            mode=mode,
            expected_snapshot_sha256=initial_source_snapshot_sha256,
            launcher_identity=launcher_identity,
            build_lock_record=build_lock_record,
        )
    except Exception as exc:
        source_prelaunch_revalidation = {
            "phase": "prelaunch",
            "completed": False,
            "error": f"{type(exc).__name__}: {exc}",
            "expected_snapshot_sha256": (
                initial_source_snapshot_sha256
            ),
            "observed_snapshot_sha256": None,
            "snapshot_matches_initial": False,
            "passed_for_launch": False,
            "passed_for_local_acceptance": False,
        }
    _record_event(
        events_path,
        run_id=manifest.run_id,
        sequence=event_sequence,
        event_type="run.source_revalidated",
        payload=source_prelaunch_revalidation,
    )
    event_sequence += 1
    if not source_prelaunch_revalidation["passed_for_launch"]:
        write_text_atomic(run_directory / "stdout.log", "")
        write_text_atomic(
            run_directory / "stderr.log",
            "probe rejected because formal source artifacts changed after "
            "initial preflight or failed immediate prelaunch validation\n",
        )
        source_revalidation_outcome = {
            "schema_version": OUTCOME_SCHEMA_VERSION,
            "completed_at_utc": _utc_now(),
            "exit_code": 4,
            "process_exit_code": None,
            "timed_out": False,
            "child_launch_error": None,
            "child_interruption": None,
            "process_group_reaped": True,
            "process_group_health": {
                "child_reaped": True,
                "process_group_quiesced": True,
                "process_group_reaped": True,
                "errors": [],
                "reason": "child_not_launched_source_revalidation_failed",
            },
            "quarantine_required": False,
            "native_output_found": False,
            "native_output_error": (
                "child not launched: source revalidation failed"
            ),
            "native_status": None,
            "driver_policy": driver_policy_checks,
            "driver_policy_permitted": driver_policy_permitted,
            "manifest_policy": manifest_policy_checks,
            "manifest_policy_permitted": manifest_policy_permitted,
            "safety_policy": safety_policy_checks,
            "preflight_permitted": preflight_permitted,
            "formal_source_binding": source_binding,
            "formal_source_checks": formal_source_checks,
            "formal_source_required_checks": formal_required_checks,
            "formal_source_preflight_permitted": (
                formal_source_preflight_permitted
            ),
            "source_eligible_for_local_pass": (
                source_eligible_for_local_pass
            ),
            "source_prelaunch_revalidation": (
                source_prelaunch_revalidation
            ),
            "source_preexec_revalidation": None,
            "source_postrun_revalidation": None,
            "post_health": {
                "gpu": None,
                "compute_processes": [],
                "mps_processes": [],
                "error": "not run: child was not launched",
                "checks": {
                    "health_queries_completed": False,
                    "gpu_accessible_after_probe": False,
                    "gpu_uuid_stable_after_probe": False,
                    "memory_safe_after_probe": False,
                    "compute_processes_absent_after_probe_or_baseline_override": False,
                    "process_group_reaped": True,
                },
            },
            "semantic_acceptance": {},
            "semantic_metrics": {},
            "masked_health_monitor_status": (
                "not_applicable"
                if mode not in MASKED_MODES
                else "not_started_source_revalidation_rejected"
            ),
            "masked_health_monitor": _masked_monitor_record(
                mode=mode,
                status="not_started_source_revalidation_rejected",
                provenance=None,
            ),
            "masked_health_monitor_checks": {},
            "local_probe_passed": False,
            "requires_matrix_validation": mode in MASKED_MODES,
            "accepted": False,
        }
        write_json_atomic(
            run_directory / "outcome.json",
            source_revalidation_outcome,
        )
        _record_event(
            events_path,
            run_id=manifest.run_id,
            sequence=event_sequence,
            event_type="run.rejected",
            payload=source_revalidation_outcome,
        )
        gpu_lease.mark_terminal()
        terminal_guard.mark_terminal()
        return 4, run_directory

    masked_monitor: NvmlXidMonitor | None = None
    masked_monitor_provenance: dict[str, Any] | None = None
    masked_monitor_checks: dict[str, bool] = {}
    masked_monitor_passed = mode not in MASKED_MODES
    xids_observed = False
    masked_monitor_status = (
        "not_applicable" if mode not in MASKED_MODES else "setup_pending"
    )
    if mode in MASKED_MODES:
        gpu_uuid = str(gpu_launch["uuid"])
        monitor_manifest = gate_manifest["safety"]["xid_monitoring"]
        try:
            masked_monitor = NvmlXidMonitor(
                gpu_uuid,
                library_path=monitor_manifest["library_path"],
                expected_library_sha256=monitor_manifest[
                    "library_sha256"
                ],
                expected_library_version=monitor_manifest[
                    "library_version"
                ],
            )
            masked_monitor.__enter__()
            resources.callback(masked_monitor.close)
            masked_monitor_status = "registered"
            masked_monitor_provenance = masked_monitor.to_provenance()
        except BaseException as exc:
            setup_base_exception = (
                exc if not isinstance(exc, Exception) else None
            )
            if masked_monitor is not None:
                try:
                    masked_monitor.close()
                    masked_monitor_provenance = (
                        masked_monitor.to_provenance()
                    )
                except BaseException as serialization_exc:
                    if (
                        setup_base_exception is None
                        and not isinstance(serialization_exc, Exception)
                    ):
                        setup_base_exception = serialization_exc
                    cached = getattr(
                        masked_monitor,
                        "last_provenance",
                        None,
                    )
                    if isinstance(cached, Mapping):
                        masked_monitor_provenance = dict(cached)
                        masked_monitor_provenance[
                            "runner_serialization_error"
                        ] = {
                            "type": type(serialization_exc).__name__,
                            "message": str(serialization_exc),
                        }
                        masked_monitor_provenance[
                            "safe_for_acceptance"
                        ] = False
                    else:
                        masked_monitor_provenance = _monitor_error_provenance(
                            gpu_uuid=gpu_uuid,
                            phase="setup_and_serialize",
                            error=serialization_exc,
                        )
            else:
                masked_monitor_provenance = _monitor_error_provenance(
                    gpu_uuid=gpu_uuid,
                    phase="construct",
                    error=exc,
                )
            masked_monitor_status = "setup_failed"
            masked_monitor_checks, masked_monitor_passed = (
                evaluate_masked_health_monitor(
                    masked_monitor_provenance,
                    expected_gpu_uuid=gpu_uuid,
                    expected_library_path=monitor_manifest["library_path"],
                    expected_library_sha256=monitor_manifest[
                        "library_sha256"
                    ],
                    expected_library_version=monitor_manifest[
                        "library_version"
                    ],
                )
            )
            monitor_record = _masked_monitor_record(
                mode=mode,
                status=masked_monitor_status,
                provenance=masked_monitor_provenance,
            )
            _record_event(
                events_path,
                run_id=manifest.run_id,
                sequence=event_sequence,
                event_type="masked_health_monitor.setup_failed",
                payload=monitor_record,
            )
            event_sequence += 1
            write_text_atomic(run_directory / "stdout.log", "")
            write_text_atomic(
                run_directory / "stderr.log",
                "masked probe rejected because Xid monitor setup failed: "
                f"{type(exc).__name__}: {exc}\n",
            )
            if isinstance(setup_base_exception, KeyboardInterrupt):
                setup_exit_code = 130
            elif isinstance(setup_base_exception, SystemExit):
                setup_exit_code = (
                    int(setup_base_exception.code)
                    if isinstance(setup_base_exception.code, int)
                    else 1
                )
            else:
                setup_exit_code = 3
            outcome = {
                "schema_version": OUTCOME_SCHEMA_VERSION,
                "completed_at_utc": _utc_now(),
                "exit_code": setup_exit_code,
                "process_exit_code": None,
                "timed_out": False,
                "child_launch_error": None,
                "child_interruption": None,
                "process_group_reaped": True,
                "process_group_health": {
                    "child_reaped": True,
                    "process_group_quiesced": True,
                    "process_group_reaped": True,
                    "errors": [],
                    "reason": "child_not_launched_monitor_setup_failed",
                },
                "quarantine_required": True,
                "quarantine_reasons": ["untrusted_xid_monitor_setup"],
                "native_output_found": False,
                "native_output_error": (
                    "Xid monitor setup failed before child launch"
                ),
                "native_status": None,
                "driver_policy": driver_policy_checks,
                "driver_policy_permitted": driver_policy_permitted,
                "manifest_policy": manifest_policy_checks,
                "manifest_policy_permitted": manifest_policy_permitted,
                "safety_policy": safety_policy_checks,
                "preflight_permitted": preflight_permitted,
                "formal_source_binding": source_binding,
                "formal_source_checks": formal_source_checks,
                "formal_source_required_checks": formal_required_checks,
                "formal_source_preflight_permitted": (
                    formal_source_preflight_permitted
                ),
                "source_eligible_for_local_pass": (
                    source_eligible_for_local_pass
                ),
                "source_prelaunch_revalidation": (
                    source_prelaunch_revalidation
                ),
                "source_preexec_revalidation": None,
                "source_postrun_revalidation": None,
                "post_health": {
                    "gpu": None,
                    "compute_processes": [],
                    "mps_processes": [],
                    "error": "not run: child was not launched",
                    "checks": {
                        "health_queries_completed": False,
                        "gpu_accessible_after_probe": False,
                        "gpu_uuid_stable_after_probe": False,
                        "memory_safe_after_probe": False,
                        "compute_processes_absent_after_probe_or_baseline_override": False,
                    },
                },
                "semantic_acceptance": {},
                "semantic_metrics": {},
                "masked_health_monitor_status": masked_monitor_status,
                "masked_health_monitor": monitor_record,
                "masked_health_monitor_checks": masked_monitor_checks,
                "local_probe_passed": False,
                "requires_matrix_validation": True,
                "accepted": False,
            }
            write_json_atomic(run_directory / "outcome.json", outcome)
            _record_event(
                events_path,
                run_id=manifest.run_id,
                sequence=event_sequence,
                event_type="run.failed",
                payload=outcome,
            )
            gpu_lease.quarantine(
                ["untrusted_xid_monitor_setup"],
                monitor=monitor_record,
            )
            gpu_lease.mark_terminal()
            terminal_guard.mark_terminal()
            if setup_base_exception is not None:
                raise setup_base_exception
            return 3, run_directory

        _record_event(
            events_path,
            run_id=manifest.run_id,
            sequence=event_sequence,
            event_type="masked_health_monitor.registered",
            payload=_masked_monitor_record(
                mode=mode,
                status=masked_monitor_status,
                provenance=masked_monitor_provenance,
            ),
        )
        event_sequence += 1

    process: subprocess.Popen[str] | None = None
    process_identity: dict[str, Any] | None = None
    timed_out = False
    child_launch_error: str | None = None
    child_interruption: dict[str, Any] | None = None
    pending_base_exception: BaseException | None = None
    pending_traceback: Any = None
    stdout = ""
    stderr = ""
    process_group_health: dict[str, Any] = {
        "child_reaped": True,
        "process_group_quiesced": True,
        "process_group_reaped": True,
        "errors": [],
        "reason": "child_not_started",
    }
    previous_signal_handlers: _InstalledChildSignalHandlers | None = None
    source_preexec_revalidation: dict[str, Any] | None = None
    source_preexec_event_recorded = False
    libcuda_final_revalidation: dict[str, Any] | None = None
    launcher_fd_final: dict[str, Any] | None = None
    final_launch_preflight: dict[str, Any] = {
        "captured_at_utc": None,
        "required_horizon_s": None,
        "required_until_utc": None,
        "gpu": None,
        "compute_processes": [],
        "mps_processes": [],
        "errors": ["final launch preflight was not reached"],
        "checks": {},
        "passed": False,
    }
    launch_commit_reservation_revalidation: dict[str, Any] = {
        "captured_at_utc": None,
        "required_for_mode": mode in MASKED_MODES,
        "required_horizon_s": None,
        "required_until_utc": None,
        "checks": {},
        "passed": False,
        "error": "launch-commit reservation revalidation was not reached",
    }
    safety_manifest = gate_manifest.get("safety")
    reservation_evidence = (
        safety_manifest.get("exclusive_reservation_evidence")
        if isinstance(safety_manifest, Mapping)
        else None
    )
    blocked_signals = {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}
    stdout_path = run_directory / "stdout.log"
    stderr_path = run_directory / "stderr.log"
    # Keep read-capable descriptors open across the child lifetime so bounded
    # post-run capture reads the exact inodes passed to Popen, without a
    # same-path reopen or unbounded TextIO allocation.
    stdout_handle = stdout_path.open("w+", encoding="utf-8")
    stderr_handle = stderr_path.open("w+", encoding="utf-8")
    try:
        previous_signal_handlers = _install_child_signal_handlers()
        signal_restore_stack.callback(
            _restore_signal_handlers,
            previous_signal_handlers,
        )
        previous_signal_mask = signal.pthread_sigmask(
            signal.SIG_BLOCK,
            blocked_signals,
        )
        try:
            source_preexec_revalidation = revalidate_formal_source_binding(
                phase="preexec_after_monitor_and_signal_block",
                repo_root=repo_root,
                binary=binary,
                libsmctrl_root=libsmctrl_root,
                gate_manifest=gate_manifest,
                mode=mode,
                expected_snapshot_sha256=initial_source_snapshot_sha256,
                launcher_identity=launcher_identity,
                build_lock_record=build_lock_record,
            )
            _record_event(
                events_path,
                run_id=manifest.run_id,
                sequence=event_sequence,
                event_type="run.source_preexec_revalidated",
                payload=source_preexec_revalidation,
            )
            event_sequence += 1
            source_preexec_event_recorded = True
            if not source_preexec_revalidation["passed_for_launch"]:
                raise _SourceRevalidationRejected(
                    "formal source binding changed in the final "
                    "post-monitor pre-exec window"
                )
            executable_fd_path = f"/proc/self/fd/{launcher_descriptor}"
            exec_command = [executable_fd_path, *command[1:]]
            final_launch_preflight = capture_final_launch_preflight(
                physical_gpu=physical_gpu,
                expected_gpu_uuid=str(gpu_launch["uuid"]),
                expected_lease_uuid=gpu_lease.gpu_uuid,
                reservation_evidence=reservation_evidence,
                mode=mode,
                timeout_s=timeout_s,
                maximum_used_mib=maximum_used_mib,
                allow_busy_gpu=allow_busy_gpu,
                mps_pipe_directory=child_environment.get(
                    "CUDA_MPS_PIPE_DIRECTORY",
                    "<missing>",
                ),
            )
            if not final_launch_preflight["passed"]:
                _record_event(
                    events_path,
                    run_id=manifest.run_id,
                    sequence=event_sequence,
                    event_type="run.final_launch_preflight",
                    payload=final_launch_preflight,
                )
                event_sequence += 1
                raise RuntimeError(
                    "final launch preflight rejected the child"
                )
            libcuda_final_revalidation = (
                revalidate_cuda_driver_library(libcuda_identity)
            )
            if not libcuda_final_revalidation["passed"]:
                _record_event(
                    events_path,
                    run_id=manifest.run_id,
                    sequence=event_sequence,
                    event_type="run.final_launch_preflight",
                    payload=final_launch_preflight,
                )
                event_sequence += 1
                raise RuntimeError(
                    "final libcuda identity revalidation rejected the child"
                )
            # Rehash the exact descriptor that becomes argv[0] after mutable
            # GPU/libcuda checks. Reservation commit and durable poison are
            # the only remaining pre-Popen safety operations.
            launcher_fd_final = revalidate_launcher_fd_final(
                launcher_descriptor,
                launcher_identity,
            )
            if not launcher_fd_final["passed"]:
                _record_event(
                    events_path,
                    run_id=manifest.run_id,
                    sequence=event_sequence,
                    event_type="run.final_launch_preflight",
                    payload=final_launch_preflight,
                )
                event_sequence += 1
                raise RuntimeError(
                    "final launcher FD identity revalidation rejected the child"
                )
            _record_event(
                events_path,
                run_id=manifest.run_id,
                sequence=event_sequence,
                event_type="run.final_launch_preflight",
                payload=final_launch_preflight,
            )
            event_sequence += 1
            if mode in MASKED_MODES:
                # Persist poison before the literal launch-commit reservation
                # decision. SIGKILL/os._exit then leaves the UUID unusable.
                gpu_lease.arm_masked_poison(run_id=manifest.run_id)
            commit_captured_at = datetime.now(timezone.utc)
            commit_horizon_s = (
                float(timeout_s)
                + PROCESS_SUPERVISION_CLEANUP_BUDGET_S
                + MASKED_XID_TOTAL_BUDGET_MS / 1000.0
                + POST_HEALTH_QUERY_BUDGET_S
                + RESERVATION_SAFETY_MARGIN_S
            )
            if mode in MASKED_MODES:
                commit_checks, commit_passed = (
                    validate_reservation_evidence(
                        reservation_evidence,
                        physical_gpu=physical_gpu,
                        gpu_uuid=str(gpu_launch["uuid"]),
                        now=commit_captured_at,
                        required_horizon_s=commit_horizon_s,
                    )
                )
            else:
                commit_checks = {
                    "reservation_not_required_for_baseline": True,
                }
                commit_passed = True
                commit_horizon_s = 0.0
            commit_required_until = commit_captured_at + timedelta(
                seconds=commit_horizon_s
            )
            launch_commit_reservation_revalidation = {
                "captured_at_utc": commit_captured_at.isoformat(
                    timespec="microseconds"
                ).replace("+00:00", "Z"),
                "required_for_mode": mode in MASKED_MODES,
                "required_horizon_s": commit_horizon_s,
                "required_until_utc": (
                    commit_required_until.isoformat(
                        timespec="microseconds"
                    ).replace("+00:00", "Z")
                ),
                "checks": commit_checks,
                "passed": commit_passed,
                "error": None,
            }
            if not commit_passed:
                raise RuntimeError(
                    "launch-commit reservation revalidation rejected "
                    "the child"
                )
            # No fallible protocol operation belongs between this pure-memory
            # decision and Popen.
            process = subprocess.Popen(
                exec_command,
                cwd=repo_root,
                env=child_environment,
                stdout=stdout_handle,
                stderr=stderr_handle,
                text=True,
                start_new_session=True,
                close_fds=True,
                pass_fds=(launcher_descriptor,),
            )
            # This must be the first fallible operation after Popen. Once the
            # /proc start-time/session identity is captured, every later
            # failure (including the run.started fsync) can safely address the
            # still-bound process group without risking numeric PGID reuse.
            process_identity = _capture_process_identity(process.pid)
            _record_event(
                events_path,
                run_id=manifest.run_id,
                sequence=event_sequence,
                event_type="run.started",
                payload={
                    "argv": command,
                    "executed_argv0": executable_fd_path,
                    "pid": process.pid,
                    "started_at_utc": _utc_now(),
                    "launcher_fd_identity": dict(launcher_identity),
                    "launcher_fd_final": launcher_fd_final,
                    "libcuda_final_revalidation": (
                        libcuda_final_revalidation
                    ),
                    "final_launch_preflight": final_launch_preflight,
                    "launch_commit_reservation_revalidation": (
                        launch_commit_reservation_revalidation
                    ),
                },
            )
            event_sequence += 1
            command_record["launcher_fd_final"] = launcher_fd_final
            command_record["libcuda_final_revalidation"] = (
                libcuda_final_revalidation
            )
            write_json_atomic(
                run_directory / "command.json",
                command_record,
            )
        finally:
            signal.pthread_sigmask(
                signal.SIG_SETMASK,
                previous_signal_mask,
            )

        assert process is not None and process_identity is not None
        process_group_health = _supervise_process(
            process,
            process_identity,
            timeout_s=timeout_s,
        )
        timed_out = bool(process_group_health.pop("timed_out"))
        cleanup_interruption = process_group_health.pop(
            "pending_base_exception"
        )
        if cleanup_interruption is not None:
            pending_base_exception = cleanup_interruption
            pending_traceback = cleanup_interruption.__traceback__
    except BaseException as error:
        child_launch_error = f"{type(error).__name__}: {error}"
        if not isinstance(error, Exception):
            pending_base_exception = error
            pending_traceback = error.__traceback__
            child_interruption = {
                "type": type(error).__name__,
                "message": str(error),
                "signal": (
                    error.signum
                    if isinstance(error, _ChildWindowInterrupted)
                    else None
                ),
            }
        if process is not None:
            cleanup_mask = signal.pthread_sigmask(
                signal.SIG_BLOCK,
                blocked_signals,
            )
            if previous_signal_handlers is not None:
                previous_signal_handlers.defer_interrupts()
            try:
                if process_identity is None:
                    process_group_health = _reap_spawn_without_identity(
                        process
                    )
                elif process.returncode is None:
                    process_group_health = _supervise_process(
                        process,
                        process_identity,
                        timeout_s=0.0,
                    )
                else:
                    # An exceptional supervisor path may already have reaped
                    # the leader.  At that point the numeric PGID is unsafe
                    # to inspect or signal, so preserve only a fail-closed
                    # direct-child result.
                    legacy_health = _verify_completed_process_group(process)
                    process_group_health = {
                        "timed_out": False,
                        "return_code": process.returncode,
                        **legacy_health,
                        "identity": dict(process_identity),
                        "wait_strategy": (
                            "leader-already-reaped;group-untrusted"
                        ),
                    }
                timed_out = bool(process_group_health.pop("timed_out"))
                cleanup_interruption = process_group_health.pop(
                    "pending_base_exception"
                )
                if (
                    pending_base_exception is None
                    and cleanup_interruption is not None
                ):
                    pending_base_exception = cleanup_interruption
                    pending_traceback = cleanup_interruption.__traceback__
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, cleanup_mask)
        stderr_handle.write(
            "\nprobe child window setup/execution failed: "
            f"{child_launch_error}\n"
        )
    finally:
        if previous_signal_handlers is not None:
            previous_signal_handlers.defer_interrupts()
        stdout_handle.flush()
        stderr_handle.flush()
        log_capture_errors: list[str] = []
        try:
            stdout = _read_bounded_descriptor_bytes(
                stdout_handle.fileno(),
                label="native stdout",
                maximum_bytes=MAX_NATIVE_STDOUT_BYTES,
                allow_empty=True,
            ).decode("utf-8", errors="strict")
        except (OSError, RuntimeError, UnicodeError) as error:
            stdout = ""
            log_capture_errors.append(
                f"stdout:{type(error).__name__}:{error}"
            )
        try:
            stderr = _read_bounded_descriptor_bytes(
                stderr_handle.fileno(),
                label="native stderr",
                maximum_bytes=MAX_NATIVE_STDERR_BYTES,
                allow_empty=True,
            ).decode("utf-8", errors="strict")
        except (OSError, RuntimeError, UnicodeError) as error:
            stderr = ""
            log_capture_errors.append(
                f"stderr:{type(error).__name__}:{error}"
            )
        stdout_handle.close()
        stderr_handle.close()
        if log_capture_errors:
            log_error = (
                "post-child bounded log capture failed: "
                + ";".join(log_capture_errors)
            )
            child_launch_error = (
                f"{child_launch_error}; {log_error}"
                if child_launch_error is not None
                else log_error
            )
            stderr = f"{stderr}\n{log_error}\n"

    def capture_deferred_child_signal() -> None:
        nonlocal pending_base_exception, pending_traceback, child_interruption
        if (
            previous_signal_handlers is None
            or previous_signal_handlers.pending_signum is None
            or pending_base_exception is not None
        ):
            return
        interruption = _ChildWindowInterrupted(
            previous_signal_handlers.pending_signum
        )
        pending_base_exception = interruption
        pending_traceback = interruption.__traceback__
        child_interruption = {
            "type": type(interruption).__name__,
            "message": str(interruption),
            "signal": interruption.signum,
        }

    if source_preexec_revalidation is None:
        source_preexec_revalidation = {
            "phase": "preexec_after_monitor_and_signal_block",
            "completed": False,
            "error": "pre-exec revalidation was not reached",
            "expected_snapshot_sha256": initial_source_snapshot_sha256,
            "observed_snapshot_sha256": None,
            "snapshot_matches_initial": False,
            "passed_for_launch": False,
            "passed_for_local_acceptance": False,
        }
    if not source_preexec_event_recorded:
        _record_event(
            events_path,
            run_id=manifest.run_id,
            sequence=event_sequence,
            event_type="run.source_preexec_revalidated",
            payload=source_preexec_revalidation,
        )
        event_sequence += 1
    command_record["launcher_fd_final"] = launcher_fd_final
    command_record["libcuda_final_revalidation"] = (
        libcuda_final_revalidation
    )
    write_json_atomic(
        run_directory / "command.json",
        command_record,
    )
    stdout = stdout or ""
    stderr = stderr or ""
    process_group_reaped = bool(
        process_group_health.get("process_group_reaped")
    )
    if not process_group_reaped:
        stderr = (
            f"{stderr}\nQUARANTINE: native process group could not be "
            "fully reaped/quiesced\n"
        )
    write_text_atomic(run_directory / "stdout.log", stdout)
    write_text_atomic(run_directory / "stderr.log", stderr)
    raw_process_return_code = (
        None
        if process is None or process.returncode is None
        else int(process.returncode)
    )
    process_exit_code = (
        None
        if raw_process_return_code is None
        else (
            124
            if timed_out
            else _normalize_exit_code(raw_process_return_code)
        )
    )

    post_health_error: str | None = None
    post_gpu: dict[str, Any] | None = None
    post_compute_processes: list[dict[str, Any]] = []
    post_mps_processes: list[dict[str, Any]] = []
    post_gpu_index_valid = False
    post_gpu_uuid_valid = False
    post_gpu_memory_valid = False
    post_compute_processes_valid = False
    post_mps_processes_valid = False
    try:
        raw_post_gpu = query_gpu(physical_gpu)
        if not isinstance(raw_post_gpu, Mapping):
            raise RuntimeError("post-health GPU record is not an object")
        post_gpu = dict(raw_post_gpu)
        post_index = post_gpu.get("index")
        post_uuid = post_gpu.get("uuid")
        post_memory_used = post_gpu.get("memory_used_mib")
        post_gpu_index_valid = (
            isinstance(post_index, int)
            and not isinstance(post_index, bool)
            and post_index >= 0
        )
        post_gpu_uuid_valid = (
            isinstance(post_uuid, str) and bool(post_uuid)
        )
        post_gpu_memory_valid = (
            isinstance(post_memory_used, int)
            and not isinstance(post_memory_used, bool)
            and post_memory_used >= 0
        )
        malformed_fields = [
            field
            for field, valid in (
                ("index", post_gpu_index_valid),
                ("uuid", post_gpu_uuid_valid),
                ("memory_used_mib", post_gpu_memory_valid),
            )
            if not valid
        ]
        if malformed_fields:
            raise RuntimeError(
                "post-health GPU record has malformed fields: "
                + ",".join(malformed_fields)
            )
        raw_post_compute_processes = query_compute_processes(post_uuid)
        if not isinstance(raw_post_compute_processes, list) or any(
            not isinstance(item, Mapping)
            for item in raw_post_compute_processes
        ):
            raise RuntimeError(
                "post-health compute process record is malformed"
            )
        post_compute_processes = [
            dict(item) for item in raw_post_compute_processes
        ]
        post_compute_processes_valid = True
        raw_post_mps_processes = query_mps_processes()
        if not isinstance(raw_post_mps_processes, list) or any(
            not isinstance(item, Mapping) for item in raw_post_mps_processes
        ):
            raise RuntimeError("post-health MPS process record is malformed")
        post_mps_processes = [
            dict(item) for item in raw_post_mps_processes
        ]
        post_mps_processes_valid = True
    except BaseException as exc:
        post_health_error = f"{type(exc).__name__}: {exc}"
        if (
            pending_base_exception is None
            and not isinstance(exc, Exception)
        ):
            pending_base_exception = exc
            pending_traceback = exc.__traceback__
    post_health_checks = {
        "health_queries_completed": post_health_error is None,
        "gpu_accessible_after_probe": post_gpu is not None,
        "gpu_ordinal_exact_after_probe": (
            post_gpu_index_valid
            and post_gpu is not None
            and post_gpu.get("index") == physical_gpu
        ),
        "gpu_uuid_stable_after_probe": (
            post_gpu_uuid_valid
            and post_gpu is not None
            and post_gpu.get("uuid") == gpu_launch.get("uuid")
        ),
        "memory_safe_after_probe": (
            post_gpu_memory_valid
            and post_gpu is not None
            and (
                post_gpu["memory_used_mib"] <= maximum_used_mib
                or effective_allow_busy
            )
        ),
        "compute_processes_absent_after_probe_or_baseline_override": (
            post_compute_processes_valid
            and (
                not post_compute_processes or effective_allow_busy
            )
        ),
        "host_mps_state_recorded_after_probe": post_mps_processes_valid,
        "process_group_reaped": process_group_reaped,
    }

    if masked_monitor is not None:
        try:
            try:
                masked_monitor.drain(
                    timeout_ms=MASKED_XID_DRAIN_TIMEOUT_MS,
                    max_events=1,
                    maximum_total_ms=MASKED_XID_TOTAL_BUDGET_MS,
                    fail_fast_on_event=True,
                )
            except BaseException as exc:
                # The concrete monitor records a categorized drain failure.
                if (
                    pending_base_exception is None
                    and not isinstance(exc, Exception)
                ):
                    pending_base_exception = exc
                    pending_traceback = exc.__traceback__
        finally:
            try:
                masked_monitor.close()
                masked_monitor_provenance = (
                    masked_monitor.to_provenance()
                )
            except BaseException as exc:
                if (
                    pending_base_exception is None
                    and not isinstance(exc, Exception)
                ):
                    pending_base_exception = exc
                    pending_traceback = exc.__traceback__
                try:
                    masked_monitor_provenance = (
                        masked_monitor.to_provenance()
                    )
                    masked_monitor_provenance = dict(
                        masked_monitor_provenance
                    )
                    masked_monitor_provenance[
                        "safe_for_acceptance"
                    ] = False
                    masked_monitor_provenance[
                        "runner_close_interruption"
                    ] = {
                        "type": type(exc).__name__,
                        "message": str(exc),
                    }
                except BaseException as serialization_exc:
                    if (
                        pending_base_exception is None
                        and not isinstance(serialization_exc, Exception)
                    ):
                        pending_base_exception = serialization_exc
                        pending_traceback = (
                            serialization_exc.__traceback__
                        )
                    cached = getattr(
                        masked_monitor,
                        "last_provenance",
                        None,
                    )
                    if isinstance(cached, Mapping):
                        masked_monitor_provenance = dict(cached)
                        masked_monitor_provenance[
                            "runner_serialization_error"
                        ] = {
                            "type": type(serialization_exc).__name__,
                            "message": str(serialization_exc),
                        }
                        masked_monitor_provenance[
                            "safe_for_acceptance"
                        ] = False
                    else:
                        masked_monitor_provenance = _monitor_error_provenance(
                            gpu_uuid=str(gpu_launch["uuid"]),
                            phase="close_and_serialize",
                            error=serialization_exc,
                        )
        if not process_group_reaped:
            masked_monitor_provenance = dict(
                masked_monitor_provenance or {}
            )
            masked_monitor_provenance["safe_for_acceptance"] = False
            masked_monitor_provenance["runner_invalidations"] = [
                "process_group_not_reaped_or_quiesced"
            ]
        masked_monitor_checks, masked_monitor_passed = (
            evaluate_masked_health_monitor(
                masked_monitor_provenance,
                expected_gpu_uuid=str(gpu_launch["uuid"]),
                expected_library_path=monitor_manifest["library_path"],
                expected_library_sha256=monitor_manifest[
                    "library_sha256"
                ],
                expected_library_version=monitor_manifest[
                    "library_version"
                ],
            )
        )
        monitor_events = (
            masked_monitor_provenance.get("events", [])
            if masked_monitor_provenance is not None
            else []
        )
        direct_xids_observed = bool(
            masked_monitor is not None
            and getattr(masked_monitor, "xid_events", [])
        )
        xids_observed = direct_xids_observed or bool(
            masked_monitor_provenance is not None
            and (
                (
                    isinstance(
                        masked_monitor_provenance.get("xids_seen"),
                        int,
                    )
                    and not isinstance(
                        masked_monitor_provenance.get("xids_seen"),
                        bool,
                    )
                    and masked_monitor_provenance["xids_seen"] > 0
                )
                or (
                    isinstance(monitor_events, list)
                    and any(
                        isinstance(event, Mapping)
                        and isinstance(event.get("event_type_bits"), int)
                        and not isinstance(
                            event.get("event_type_bits"), bool
                        )
                        and bool(
                            event["event_type_bits"]
                            & NVML_EVENT_TYPE_XID_CRITICAL_ERROR
                        )
                        for event in monitor_events
                    )
                )
            )
        )
        # Xid is the highest-priority classification even when the child also
        # failed or timed out.
        if xids_observed:
            masked_monitor_status = "xid_observed"
        elif not process_group_reaped:
            masked_monitor_status = "quarantine_unreaped_process_group"
        elif child_launch_error is not None:
            masked_monitor_status = "child_launch_failed"
        elif masked_monitor_passed:
            masked_monitor_status = "clean"
        else:
            masked_monitor_status = "monitor_failed"
        _record_event(
            events_path,
            run_id=manifest.run_id,
            sequence=event_sequence,
            event_type="masked_health_monitor.drained",
            payload=_masked_monitor_record(
                mode=mode,
                status=masked_monitor_status,
                provenance=masked_monitor_provenance,
            ),
        )
        event_sequence += 1

    reservation_revalidated_at = datetime.now(timezone.utc)
    if mode in MASKED_MODES:
        post_reservation_checks, post_reservation_valid = (
            validate_reservation_evidence(
                reservation_evidence,
                physical_gpu=physical_gpu,
                gpu_uuid=str(gpu_launch["uuid"]),
                now=reservation_revalidated_at,
                required_horizon_s=0.0,
            )
        )
    else:
        post_reservation_checks = {
            "reservation_not_required_for_baseline": True,
        }
        post_reservation_valid = True
    post_reservation_revalidation = {
        "captured_at_utc": reservation_revalidated_at.isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z"),
        "required_for_mode": mode in MASKED_MODES,
        "checks": post_reservation_checks,
        "passed": post_reservation_valid,
    }
    post_health_checks[
        "reservation_valid_at_gpu_safety_end"
    ] = post_reservation_valid

    try:
        source_postrun_revalidation = revalidate_formal_source_binding(
            phase="postrun",
            repo_root=repo_root,
            binary=binary,
            libsmctrl_root=libsmctrl_root,
            gate_manifest=gate_manifest,
            mode=mode,
            expected_snapshot_sha256=initial_source_snapshot_sha256,
            launcher_identity=launcher_identity,
            build_lock_record=build_lock_record,
        )
    except BaseException as exc:
        if (
            pending_base_exception is None
            and not isinstance(exc, Exception)
        ):
            pending_base_exception = exc
            pending_traceback = exc.__traceback__
        source_postrun_revalidation = {
            "phase": "postrun",
            "completed": False,
            "error": f"{type(exc).__name__}: {exc}",
            "expected_snapshot_sha256": (
                initial_source_snapshot_sha256
            ),
            "observed_snapshot_sha256": None,
            "snapshot_matches_initial": False,
            "passed_for_launch": False,
            "passed_for_local_acceptance": False,
        }
    _record_event(
        events_path,
        run_id=manifest.run_id,
        sequence=event_sequence,
        event_type="run.source_postvalidated",
        payload=source_postrun_revalidation,
    )
    event_sequence += 1

    native: dict[str, Any] | None = None
    native_error: str | None = None
    acceptance: dict[str, bool] = {}
    metrics: dict[str, Any] = {}
    semantic_accepted = False
    try:
        native = parse_native_output(stdout)
        write_json_atomic(run_directory / "native.json", native)
        acceptance, metrics, semantic_accepted = evaluate_probe(
            native,
            expected_mode=mode,
            expected_enabled_tpc=enabled_tpc,
            expected_driver_version=driver_version,
            expected_runtime_version=int(
                hardware["runtime_api_version"]
            ),
            expected_iterations=iterations,
            process_exit_code=(
                process_exit_code
                if process_exit_code is not None
                else -1
            ),
            expected_device_uuid=str(gpu_launch["uuid"]),
            expected_device_name=str(hardware["gpu_name"]),
            expected_sm_count=int(hardware["sm_count"]),
            expected_compute_capability=hardware["compute_capability"],
            expected_blocks=blocks,
            expected_threads_per_block=threads_per_block,
            expected_device_ordinal=0,
            expected_tpc_count=(
                int(hardware["expected_tpc_count"])
                if mode in MASKED_MODES
                else None
            ),
            expected_parent_pid=expected_parent_pid,
            stderr=stderr,
            allowed_observed_sm_counts=tuple(
                gate_manifest[
                    "single_tpc_matrix_after_explicit_promotion"
                ]["allowed_observed_sm_count"]
            ),
            minimum_sm_coverage=float(
                gate_manifest["baseline"]["minimum_sm_coverage_fraction"]
            ),
        )
    except NativeOutputError as exc:
        native_error = str(exc)

    capture_deferred_child_signal()
    local_probe_passed = (
        preflight_permitted
        and source_eligible_for_local_pass
        and source_prelaunch_revalidation[
            "passed_for_local_acceptance"
        ]
        and source_preexec_revalidation[
            "passed_for_local_acceptance"
        ]
        and source_postrun_revalidation[
            "passed_for_local_acceptance"
        ]
        and final_launch_preflight["passed"] is True
        and launch_commit_reservation_revalidation["passed"] is True
        and isinstance(libcuda_final_revalidation, Mapping)
        and libcuda_final_revalidation.get("passed") is True
        and isinstance(launcher_fd_final, Mapping)
        and launcher_fd_final.get("passed") is True
        and not timed_out
        and child_launch_error is None
        and pending_base_exception is None
        and process_group_reaped
        and process_exit_code is not None
        and native is not None
        and semantic_accepted
        and all(post_health_checks.values())
        and masked_monitor_passed
    )
    # A single masked process can establish only local semantics. Cross-trial
    # stability, disjoint TPC maps, a follow-up baseline, and next-mask
    # non-leakage require a matrix validator before any masked cell is accepted.
    accepted = (
        local_probe_passed
        and mode == "baseline"
        and not allow_busy_gpu
    )
    if isinstance(pending_base_exception, KeyboardInterrupt):
        exit_code = 130
    elif isinstance(pending_base_exception, _ChildWindowInterrupted):
        exit_code = 128 + pending_base_exception.signum
    elif isinstance(pending_base_exception, SystemExit):
        exit_code = _normalize_exit_code(
            pending_base_exception.code
            if isinstance(pending_base_exception.code, int)
            else 1,
        )
    else:
        exit_code = (
            process_exit_code
            if process_exit_code not in (None, 0)
            else (0 if local_probe_passed else 3)
        )
    process_group_health.pop("pending_base_exception", None)
    quarantine_reasons: list[str] = []
    if xids_observed:
        quarantine_reasons.append("xid_observed")
    if not process_group_reaped:
        quarantine_reasons.append("process_group_not_reaped")
    final_checks = final_launch_preflight.get("checks")
    final_checks = (
        final_checks if isinstance(final_checks, Mapping) else {}
    )
    final_preflight_reached = (
        final_launch_preflight.get("captured_at_utc") is not None
    )
    if final_preflight_reached and final_launch_preflight.get("errors"):
        quarantine_reasons.append("final_launch_health_query_failed")
    if final_preflight_reached and final_checks.get("gpu_accessible") is False:
        quarantine_reasons.append(
            "gpu_unavailable_at_final_launch_preflight"
        )
    if final_preflight_reached and final_checks.get("gpu_ordinal_exact") is False:
        quarantine_reasons.append(
            "gpu_ordinal_changed_at_final_launch_preflight"
        )
    if final_preflight_reached and final_checks.get("gpu_uuid_stable") is False:
        quarantine_reasons.append(
            "gpu_uuid_changed_at_final_launch_preflight"
        )
    if (
        final_checks.get(
            "compute_processes_absent_or_explicit_busy_baseline"
        )
        is False
        and final_preflight_reached
        and not effective_allow_busy
    ):
        quarantine_reasons.append(
            "unexpected_compute_process_at_final_launch_preflight"
        )
    if (
        final_checks.get("memory_safe_or_explicit_busy_baseline") is False
        and final_preflight_reached
        and not effective_allow_busy
    ):
        quarantine_reasons.append(
            "gpu_memory_above_limit_at_final_launch_preflight"
        )
    if (
        mode in MASKED_MODES
        and launch_commit_reservation_revalidation["captured_at_utc"]
        is not None
        and launch_commit_reservation_revalidation["passed"] is not True
    ):
        quarantine_reasons.append(
            "reservation_invalid_at_popen_commit"
        )
    if post_health_error is not None:
        quarantine_reasons.append("post_health_query_failed")
    if post_gpu is None:
        quarantine_reasons.append("gpu_unavailable_after_probe")
    else:
        if not post_gpu_index_valid:
            quarantine_reasons.append(
                "gpu_ordinal_record_invalid_after_probe"
            )
        elif post_gpu.get("index") != physical_gpu:
            quarantine_reasons.append("gpu_ordinal_changed_after_probe")
        if not post_gpu_uuid_valid:
            quarantine_reasons.append("gpu_uuid_record_invalid_after_probe")
        elif post_gpu.get("uuid") != gpu_launch.get("uuid"):
            quarantine_reasons.append("gpu_uuid_changed_after_probe")
        if not post_gpu_memory_valid:
            quarantine_reasons.append(
                "gpu_memory_record_invalid_after_probe"
            )
    if (
        post_gpu_memory_valid
        and post_gpu is not None
        and post_gpu["memory_used_mib"] > maximum_used_mib
        and not effective_allow_busy
    ):
        quarantine_reasons.append("gpu_memory_above_limit_after_probe")
    if not post_compute_processes_valid:
        quarantine_reasons.append(
            "compute_process_record_invalid_after_probe"
        )
    elif post_compute_processes and not effective_allow_busy:
        quarantine_reasons.append("unexpected_compute_process_after_probe")
    if not post_mps_processes_valid:
        quarantine_reasons.append("mps_process_record_invalid_after_probe")
    if mode in MASKED_MODES and not masked_monitor_passed:
        quarantine_reasons.append("untrusted_xid_monitor")
    if mode in MASKED_MODES and not post_reservation_valid:
        quarantine_reasons.append("reservation_invalid_at_gpu_safety_end")
    if mode in MASKED_MODES and not local_probe_passed:
        quarantine_reasons.append("masked_run_not_clean")
    if quarantine_reasons:
        gpu_lease.quarantine(
            quarantine_reasons,
            post_gpu=post_gpu,
            post_health_error=post_health_error,
            monitor_status=masked_monitor_status,
            process_group_health=process_group_health,
        )
    outcome = {
        "schema_version": OUTCOME_SCHEMA_VERSION,
        "completed_at_utc": _utc_now(),
        "exit_code": exit_code,
        "process_exit_code": process_exit_code,
        "raw_process_return_code": raw_process_return_code,
        "timed_out": timed_out,
        "child_launch_error": child_launch_error,
        "child_interruption": child_interruption,
        "process_group_reaped": process_group_reaped,
        "process_group_health": process_group_health,
        "quarantine_required": bool(quarantine_reasons),
        "quarantine_reasons": sorted(set(quarantine_reasons)),
        "gpu_lease": dict(gpu_lease.record or {}),
        "native_output_found": native is not None,
        "native_output_error": native_error,
        "native_status": native.get("status") if native else None,
        "driver_policy": driver_policy_checks,
        "driver_policy_permitted": driver_policy_permitted,
        "manifest_policy": manifest_policy_checks,
        "manifest_policy_permitted": manifest_policy_permitted,
        "safety_policy": safety_policy_checks,
        "preflight_permitted": preflight_permitted,
        "formal_source_binding": source_binding,
        "formal_source_checks": formal_source_checks,
        "formal_source_required_checks": formal_required_checks,
        "formal_source_preflight_permitted": (
            formal_source_preflight_permitted
        ),
        "source_eligible_for_local_pass": source_eligible_for_local_pass,
        "source_prelaunch_revalidation": source_prelaunch_revalidation,
        "source_preexec_revalidation": source_preexec_revalidation,
        "source_postrun_revalidation": source_postrun_revalidation,
        "final_launch_preflight": final_launch_preflight,
        "launch_commit_reservation_revalidation": (
            launch_commit_reservation_revalidation
        ),
        "libcuda_final_revalidation": libcuda_final_revalidation,
        "launcher_fd_final": launcher_fd_final,
        "formal_launcher_threat_boundaries": dict(
            FORMAL_LAUNCHER_THREAT_BOUNDARIES
        ),
        "post_health": {
            "gpu": post_gpu,
            "compute_processes": post_compute_processes,
            "mps_processes": post_mps_processes,
            "error": post_health_error,
            "checks": post_health_checks,
            "reservation_revalidation": post_reservation_revalidation,
        },
        "semantic_acceptance": acceptance,
        "semantic_metrics": metrics,
        "masked_health_monitor_status": masked_monitor_status,
        "masked_health_monitor": _masked_monitor_record(
            mode=mode,
            status=masked_monitor_status,
            provenance=masked_monitor_provenance,
        ),
        "masked_health_monitor_checks": masked_monitor_checks,
        "local_probe_passed": local_probe_passed,
        "requires_matrix_validation": mode in MASKED_MODES,
        "accepted": accepted,
    }
    # Close the final deferred-signal window. Signals arriving after this
    # point remain kernel-pending until terminal evidence and lease state are
    # durable, then are delivered under the caller's original handlers.
    final_previous_signal_mask = signal.pthread_sigmask(
        signal.SIG_BLOCK,
        blocked_signals,
    )
    capture_deferred_child_signal()
    if child_interruption != outcome["child_interruption"]:
        outcome["completed_at_utc"] = _utc_now()
        outcome["child_interruption"] = child_interruption
        outcome["local_probe_passed"] = False
        outcome["accepted"] = False
        if isinstance(pending_base_exception, KeyboardInterrupt):
            outcome["exit_code"] = 130
        elif isinstance(pending_base_exception, _ChildWindowInterrupted):
            outcome["exit_code"] = 128 + pending_base_exception.signum
        elif isinstance(pending_base_exception, SystemExit):
            outcome["exit_code"] = _normalize_exit_code(
                pending_base_exception.code
            )

    terminal_error: BaseException | None = None
    downgrade_errors: list[BaseException] = []
    terminal_artifact_durable = False

    def terminal_failure_exit_code(error: BaseException) -> int:
        if isinstance(error, KeyboardInterrupt):
            return 130
        if isinstance(error, _ChildWindowInterrupted):
            return 128 + error.signum
        if isinstance(error, SystemExit):
            code = _normalize_exit_code(
                error.code if isinstance(error.code, int) else 1
            )
            return code if code != 0 else 3
        return 3

    def downgrade_terminal_in_memory(
        *,
        reason: str,
        error: BaseException,
    ) -> None:
        reasons = outcome.get("quarantine_reasons")
        normalized_reasons = (
            [
                str(item)
                for item in reasons
                if isinstance(item, str) and item
            ]
            if isinstance(reasons, list)
            else []
        )
        normalized_reasons.append(reason)
        outcome.update(
            {
                "completed_at_utc": _utc_now(),
                "exit_code": terminal_failure_exit_code(error),
                "local_probe_passed": False,
                "accepted": False,
                "quarantine_required": True,
                "quarantine_reasons": sorted(
                    set(normalized_reasons)
                ),
            }
        )
        health = outcome.get("process_group_health")
        health_record = dict(health) if isinstance(health, Mapping) else {}
        health_errors = health_record.get("errors")
        normalized_errors = (
            [
                str(item)
                for item in health_errors
                if isinstance(item, str)
            ]
            if isinstance(health_errors, list)
            else []
        )
        normalized_errors.append(
            f"terminal:{reason}:{type(error).__name__}:{error}"
        )
        health_record["errors"] = normalized_errors
        outcome["process_group_health"] = health_record

    def persist_downgraded_terminal() -> None:
        write_json_atomic(run_directory / "outcome.json", outcome)
        records: list[dict[str, Any]] = []
        if events_path.exists():
            for line in events_path.read_text(
                encoding="utf-8",
            ).splitlines():
                if line:
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise RuntimeError(
                            "terminal event history is not object JSON"
                        )
                    records.append(value)
        replacement = EventRecord.create(
            run_id=manifest.run_id,
            sequence=event_sequence,
            event_type="run.failed",
            payload=outcome,
        ).to_dict()
        if (
            records
            and records[-1].get("sequence") == event_sequence
            and records[-1].get("event_type")
            in {"run.completed", "run.failed"}
        ):
            records[-1] = replacement
        else:
            records.append(replacement)
        write_text_atomic(
            events_path,
            "".join(canonical_json(record) + "\n" for record in records),
        )

    def quarantine_terminal_failure(
        reason: str,
        error: BaseException,
    ) -> None:
        try:
            gpu_lease.quarantine(
                [reason],
                run_id=manifest.run_id,
                error=f"{type(error).__name__}: {error}",
            )
        except BaseException as quarantine_error:
            downgrade_errors.append(quarantine_error)

    def persist_or_guard_downgrade(
        reason: str,
        error: BaseException,
    ) -> None:
        nonlocal terminal_artifact_durable
        terminal_artifact_durable = False
        downgrade_terminal_in_memory(reason=reason, error=error)
        quarantine_terminal_failure(reason, error)
        try:
            persist_downgraded_terminal()
            terminal_artifact_durable = True
        except BaseException as persistence_error:
            downgrade_errors.append(persistence_error)
            try:
                terminal_guard.__exit__(
                    type(error),
                    error,
                    error.__traceback__,
                )
                terminal_artifact_durable = True
            except BaseException as guard_error:
                downgrade_errors.append(guard_error)

    try:
        write_json_atomic(run_directory / "outcome.json", outcome)
        _record_event(
            events_path,
            run_id=manifest.run_id,
            sequence=event_sequence,
            event_type=(
                "run.completed"
                if outcome["local_probe_passed"]
                else "run.failed"
            ),
            payload=outcome,
        )
        terminal_artifact_durable = True
    except BaseException as error:
        terminal_error = error
        persist_or_guard_downgrade(
            "terminal_artifact_write_failed",
            error,
        )

    restore_error: BaseException | None = None
    delivery_error: BaseException | None = None
    pending_signal_error: BaseException | None = None
    capture_deferred_child_signal()
    try:
        pending_signals = set(signal.sigpending())
    except BaseException as error:
        pending_signals = set()
        pending_signal_error = error
        persist_or_guard_downgrade(
            "terminal_pending_signal_query_failed",
            error,
        )
    pending_terminal_signals = sorted(
        int(signum)
        for signum in pending_signals
        if signum in blocked_signals
    )
    if pending_terminal_signals:
        interruption = _ChildWindowInterrupted(
            pending_terminal_signals[0]
        )
        if pending_base_exception is None:
            pending_base_exception = interruption
            pending_traceback = interruption.__traceback__
        child_interruption = {
            "type": type(interruption).__name__,
            "message": str(interruption),
            "signal": interruption.signum,
        }
        outcome["child_interruption"] = child_interruption
        persist_or_guard_downgrade(
            "terminal_signal_pending_before_restore",
            interruption,
        )

    def capture_and_persist_deferred_signal(reason: str) -> None:
        nonlocal pending_base_exception, pending_traceback
        nonlocal child_interruption
        if (
            previous_signal_handlers is None
            or previous_signal_handlers.pending_signum is None
        ):
            return
        signum = previous_signal_handlers.pending_signum
        existing_signal = (
            outcome.get("child_interruption", {}).get("signal")
            if isinstance(outcome.get("child_interruption"), Mapping)
            else None
        )
        if existing_signal == signum:
            return
        interruption = _ChildWindowInterrupted(signum)
        if pending_base_exception is None:
            pending_base_exception = interruption
            pending_traceback = interruption.__traceback__
        child_interruption = {
            "type": type(interruption).__name__,
            "message": str(interruption),
            "signal": interruption.signum,
        }
        outcome["child_interruption"] = child_interruption
        persist_or_guard_downgrade(reason, interruption)

    try:
        # Unmask while the runner's handlers are still installed in deferred
        # mode. Pending/racing lifecycle signals become evidence instead of
        # invoking a caller default handler before the terminal decision.
        signal.pthread_sigmask(
            signal.SIG_SETMASK,
            final_previous_signal_mask,
        )
    except BaseException as error:
        delivery_error = error
        persist_or_guard_downgrade(
            "terminal_signal_mask_restore_failed",
            error,
        )
        try:
            signal.pthread_sigmask(
                signal.SIG_SETMASK,
                final_previous_signal_mask,
            )
        except BaseException as retry_error:
            downgrade_errors.append(retry_error)
    capture_and_persist_deferred_signal(
        "terminal_signal_delivered_before_handler_restore"
    )
    try:
        # This restore is the explicit post-completion cancellation boundary.
        # Signals handled before it are captured above/below; signals arriving
        # after their caller handler is restored belong to caller execution.
        signal_restore_stack.close()
    except BaseException as error:
        restore_error = error
        persist_or_guard_downgrade(
            "terminal_signal_handler_restore_failed",
            error,
        )
    capture_and_persist_deferred_signal(
        "terminal_signal_delivered_during_handler_restore"
    )

    terminal_failures = (
        terminal_error,
        pending_signal_error,
        restore_error,
        delivery_error,
    )
    if (
        not any(error is not None for error in terminal_failures)
        and not pending_terminal_signals
        and pending_base_exception is None
        and mode in MASKED_MODES
        and outcome["local_probe_passed"] is True
        and not outcome["quarantine_required"]
    ):
        try:
            # This is the post-completion cancellation boundary: terminal
            # evidence is durable, pending lifecycle signals were sampled,
            # original handlers/mask are restored, and GPU safety is clean.
            gpu_lease.clear_masked_poison()
        except BaseException as error:
            terminal_error = error
            persist_or_guard_downgrade(
                "masked_poison_clear_failed",
                error,
            )

    if terminal_artifact_durable:
        gpu_lease.mark_terminal()
        terminal_guard.mark_terminal()

    chosen_error: BaseException | None = pending_base_exception
    for candidate in (
        terminal_error,
        *downgrade_errors,
        pending_signal_error,
        restore_error,
        delivery_error,
    ):
        if chosen_error is None and candidate is not None:
            chosen_error = candidate
        elif (
            chosen_error is not None
            and isinstance(chosen_error, Exception)
            and candidate is not None
            and not isinstance(candidate, Exception)
        ):
            # A non-Exception signal/control-flow interruption must never be
            # hidden behind an ordinary cleanup error.
            chosen_error = candidate

    if chosen_error is not None:
        try:
            setattr(
                chosen_error,
                "burstserve_run_directory",
                run_directory,
            )
            setattr(
                chosen_error,
                "burstserve_exit_code",
                _normalize_exit_code(outcome["exit_code"]),
            )
        except BaseException:
            pass
        if chosen_error is pending_base_exception:
            raise chosen_error.with_traceback(pending_traceback)
        raise chosen_error
    return _normalize_exit_code(outcome["exit_code"]), run_directory


def build_native(
    *,
    repo_root: Path,
    source_directory: Path,
    jobs: int | None,
) -> int:
    """Build and independently re-parse the formal native gate artifact."""

    canonical_source = repo_root.resolve() / DEFAULT_BUILD_SOURCE
    if source_directory != canonical_source:
        raise RuntimeError(
            "formal native build requires the repository-canonical source "
            f"directory: {canonical_source}"
        )
    makefile = source_directory / "Makefile"
    command = [
        "/usr/bin/make",
        "--no-print-directory",
        "-C",
        str(source_directory),
        "-f",
        str(makefile),
    ]
    if jobs is not None:
        if isinstance(jobs, bool) or not isinstance(jobs, int) or jobs <= 0:
            raise ValueError("jobs must be positive")
        command.extend(["-j", str(jobs)])
    command.append("gate-required-check")
    completed = subprocess.run(
        command,
        cwd=repo_root,
        env=dict(FORMAL_BUILD_ENVIRONMENT),
        check=False,
        capture_output=True,
    )
    if completed.stdout:
        sys.stdout.buffer.write(completed.stdout)
        sys.stdout.buffer.flush()
    if completed.stderr:
        sys.stderr.buffer.write(completed.stderr)
        sys.stderr.buffer.flush()
    if completed.returncode != 0:
        return _normalize_exit_code(completed.returncode)
    try:
        stdout = completed.stdout.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return 2
    sentinel_lines = [
        line
        for line in stdout.splitlines()
        if line == NATIVE_GATE_COMPLETION_SENTINEL
    ]
    nonempty_lines = [line for line in stdout.splitlines() if line]
    if (
        len(sentinel_lines) != 1
        or not nonempty_lines
        or nonempty_lines[-1] != NATIVE_GATE_COMPLETION_SENTINEL
    ):
        return 2
    try:
        _validate_postbuild_attestation(repo_root)
    except (OSError, RuntimeError, TypeError, ValueError):
        return 2
    return 0


def _validate_postbuild_attestation(repo_root: Path) -> None:
    """Strictly parse the just-built attestation after the make gate exits."""

    binary = repo_root.resolve() / DEFAULT_BINARY
    attestation_path = binary.parent / "build-attestation.json"
    descriptor, identity = _open_regular_nofollow(attestation_path)
    os.close(descriptor)
    record, parsed_identity = _load_attestation_bootstrap(
        binary,
        expected_sha256=str(identity["sha256"]),
        expected_identity=identity,
    )
    checks = record.get("checks")
    if (
        not _attestation_identity_matches(parsed_identity, identity)
        or not isinstance(checks, Mapping)
        or not checks
        or any(value is not True for value in checks.values())
    ):
        raise RuntimeError(
            "post-build attestation parser did not verify every check"
        )


def _resolve(repo_root: Path, path: Path) -> Path:
    return path.resolve() if path.is_absolute() else (repo_root / path).resolve()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="build the native SM-ID probe")
    build.add_argument("--repo-root", type=Path, default=Path.cwd())
    build.add_argument("--source-dir", type=Path, default=DEFAULT_BUILD_SOURCE)
    build.add_argument("--jobs", type=int)

    run = subparsers.add_parser("run", help="run one provenance-complete probe")
    run.add_argument("--repo-root", type=Path, default=Path.cwd())
    run.add_argument("--binary", type=Path, default=DEFAULT_BINARY)
    run.add_argument("--libsmctrl-root", type=Path, default=DEFAULT_LIBSMCTRL_ROOT)
    run.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    run.add_argument(
        "--gate-manifest",
        type=Path,
        default=DEFAULT_GATE_MANIFEST,
        help="versioned safety/promotion manifest embedded into the run ID",
    )
    run.add_argument("--physical-gpu", type=int, required=True)
    run.add_argument("--mode", choices=PROBE_MODES, required=True)
    run.add_argument("--enabled-tpc", type=int, default=0)
    run.add_argument("--iterations", type=int, default=4096)
    run.add_argument("--trial", type=int, default=0)
    run.add_argument("--seed", type=int, default=0)
    run.add_argument(
        "--timeout-s",
        type=float,
        help="must match the Gate-A manifest (default: manifest value)",
    )
    run.add_argument(
        "--maximum-used-mib",
        type=int,
        help="must match the Gate-A manifest (default: manifest value)",
    )
    run.add_argument(
        "--allow-busy-gpu",
        action="store_true",
        help="baseline-only exploratory override; forbidden for masked probes",
    )
    run.add_argument(
        "--experimental-allow-unsupported-driver",
        action="store_true",
        help="explicitly permit a masked probe on a newer unpinned driver",
    )
    run.add_argument(
        "--experimental-mask-off",
        type=int,
        help="stream-mask offset passed as MASK_OFF; requires experimental allow",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    if args.command == "build":
        try:
            return build_native(
                repo_root=repo_root,
                source_directory=_resolve(repo_root, args.source_dir),
                jobs=args.jobs,
            )
        except BaseException as error:
            print(
                f"formal native build rejected: "
                f"{type(error).__name__}: {error}",
                file=sys.stderr,
            )
            if isinstance(error, KeyboardInterrupt):
                return 130
            if isinstance(error, SystemExit):
                return _normalize_exit_code(error.code)
            return 2

    try:
        binary = _resolve(repo_root, args.binary)
        libsmctrl_root = _resolve(repo_root, args.libsmctrl_root)
        run_root = _resolve(repo_root, args.run_root)
        gate_manifest_record = load_gate_manifest_record(
            _resolve(repo_root, args.gate_manifest),
            repo_root=repo_root,
        )
        gate_content = gate_manifest_record.get("content")
        if not isinstance(gate_content, Mapping):
            raise RuntimeError("Gate-A manifest content is not an object")
        gate_safety = gate_content.get("safety")
        hardware = gate_content.get("hardware")
        baseline = gate_content.get("baseline")
        matrix = gate_content.get(
            "single_tpc_matrix_after_explicit_promotion"
        )
        if not all(
            isinstance(value, Mapping)
            for value in (gate_safety, hardware, baseline, matrix)
        ):
            raise RuntimeError(
                "Gate-A v2 manifest is missing a required object section"
            )

        def manifest_integer(
            section: Mapping[str, Any],
            field: str,
            *,
            minimum: int,
        ) -> int:
            value = section.get(field)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < minimum
            ):
                raise RuntimeError(
                    f"Gate-A manifest {field!r} must be an integer "
                    f">= {minimum}"
                )
            return value

        manifest_timeout = gate_safety.get("timeout_s")
        if (
            isinstance(manifest_timeout, bool)
            or not isinstance(manifest_timeout, (int, float))
            or not math.isfinite(float(manifest_timeout))
            or float(manifest_timeout) <= 0
        ):
            raise RuntimeError(
                "Gate-A manifest 'timeout_s' must be finite and positive"
            )
        timeout_s = (
            float(args.timeout_s)
            if args.timeout_s is not None
            else float(manifest_timeout)
        )
        maximum_used_mib = (
            int(args.maximum_used_mib)
            if args.maximum_used_mib is not None
            else manifest_integer(
                gate_safety,
                "maximum_preexisting_gpu_memory_mib",
                minimum=0,
            )
        )
        if args.mode == "baseline":
            block_count = manifest_integer(
                hardware,
                "sm_count",
                minimum=1,
            ) * manifest_integer(
                baseline,
                "blocks_per_sm",
                minimum=1,
            )
            threads_per_block = manifest_integer(
                baseline,
                "threads_per_block",
                minimum=1,
            )
        else:
            block_count = manifest_integer(
                matrix,
                "blocks",
                minimum=1,
            )
            threads_per_block = manifest_integer(
                matrix,
                "threads_per_block",
                minimum=1,
            )
        config = {
            "schema_version": CELL_SCHEMA_VERSION,
            "physical_gpu": args.physical_gpu,
            "mode": args.mode,
            "enabled_tpc": args.enabled_tpc,
            "iterations": args.iterations,
            "blocks": block_count,
            "threads_per_block": threads_per_block,
            "trial": args.trial,
            "seed": args.seed,
            "timeout_s": timeout_s,
            "maximum_used_mib": maximum_used_mib,
            "allow_busy_gpu": args.allow_busy_gpu,
            "experimental_allow_unsupported_driver": (
                args.experimental_allow_unsupported_driver
            ),
            "experimental_mask_off": args.experimental_mask_off,
            "gate_manifest": gate_manifest_record,
        }
        code, run_directory = execute(
            repo_root=repo_root,
            binary=binary,
            libsmctrl_root=libsmctrl_root,
            run_root=run_root,
            config=config,
            timeout_s=timeout_s,
            maximum_used_mib=maximum_used_mib,
            allow_busy_gpu=args.allow_busy_gpu,
        )
    except BaseException as error:
        run_directory = getattr(
            error,
            "burstserve_run_directory",
            None,
        )
        print(
            str(run_directory)
            if isinstance(run_directory, Path)
            else "<no-run-directory>"
        )
        return _normalize_exit_code(
            getattr(error, "burstserve_exit_code", 1)
        )
    print(run_directory)
    return _normalize_exit_code(code)


if __name__ == "__main__":
    raise SystemExit(main())
