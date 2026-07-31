#!/usr/bin/env python3
"""Build, identity, and verify the native SM probe's finite trust contract."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import errno
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import selectors
import shlex
import shutil
import signal
import stat
import struct
import subprocess
import sys
import time
import types
from typing import Any, Iterator, Mapping, Sequence


SCHEMA_VERSION = "burstserve.native-build-attestation/v2"
GIT = Path("/usr/bin/git")
LDD = Path("/usr/bin/ldd")
DEV_NULL = Path("/dev/null")
PYTHON_ISOLATION_FLAGS = ("-I", "-S", "-B")
READ_CHUNK_BYTES = 1024 * 1024
MAX_PATH_BYTES = 4096
MAX_PATH_COMPONENTS = 256
MAX_PATH_COMPONENT_BYTES = 255
MAX_SOURCE_FILE_BYTES = 64 * 1024 * 1024
MAX_IDENTITY_FILE_BYTES = 512 * 1024 * 1024
MAX_OUTPUT_FILE_BYTES = 512 * 1024 * 1024
MAX_OUTPUT_AGGREGATE_BYTES = 1024 * 1024 * 1024
MAX_STAMP_BYTES = 256 * 1024
MAX_ATTESTATION_BYTES = 8 * 1024 * 1024
MAX_JSON_DEPTH = 64
MAX_JSON_NODES = 100_000
MAX_JSON_STRING_BYTES = 1024 * 1024
MAX_JSON_INTEGER_DIGITS = 128
MAX_IDENTITY_HEADER_BYTES = 16 * 1024
MAX_TREE_ENTRIES = 100_000
MAX_TREE_FILES = 100_000
MAX_TREE_DEPTH = 64
MAX_TREE_RELATIVE_PATH_BYTES = 4096
MAX_TREE_AGGREGATE_PATH_BYTES = 64 * 1024 * 1024
MAX_TREE_FILE_BYTES = 512 * 1024 * 1024
MAX_TREE_AGGREGATE_BYTES = 4 * 1024 * 1024 * 1024
MAX_XATTR_COUNT = 128
MAX_XATTR_NAME_BYTES = 1024
MAX_XATTR_VALUE_BYTES = 64 * 1024
MAX_XATTR_AGGREGATE_BYTES = 1024 * 1024
MAX_SUBPROCESS_STREAM_BYTES = 8 * 1024 * 1024
SUBPROCESS_TIMEOUT_SECONDS = 30.0
SUBPROCESS_REAP_SECONDS = 5.0
PROCESS_GROUP_QUIESCE_SECONDS = 5.0
PROCESS_GROUP_POLL_SECONDS = 0.01
MAX_RUNTIME_DEPENDENCIES = 512
MAX_PROC_STAT_BYTES = 64 * 1024
MAX_PROC_VECTOR_BYTES = 8 * 1024 * 1024
MAX_PROC_RECORDS = 65_536
STAMP_FIELD_ORDER = (
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
CONFIG_ENVIRONMENT = {
    "source_dir": "BS_SOURCE_DIR",
    "repo_root": "BS_REPO_ROOT",
    "build_dir": "BS_BUILD_DIR",
    "build_tmpdir": "BS_BUILD_TMPDIR",
    "build_lock_path": "BS_BUILD_LOCK_PATH",
    "hermetic_path": "BS_HERMETIC_PATH",
    "cuda_home": "BS_CUDA_HOME",
    "cuda_arch": "BS_CUDA_ARCH",
    "nvcc": "BS_NVCC",
    "cc": "BS_CC",
    "ar": "BS_AR",
    "cppflags": "BS_CPPFLAGS",
    "cflags": "BS_CFLAGS",
    "nvccflags": "BS_NVCCFLAGS",
    "ldlibs": "BS_LDLIBS",
    "launcher_cflags": "BS_LAUNCHER_CFLAGS",
    "libsmctrl_dir": "BS_LIBSMCTRL_DIR",
}
BASE_BUILD_ENVIRONMENT = {
    "LANG": "C",
    "LC_ALL": "C",
    "TZ": "UTC",
    "SOURCE_DATE_EPOCH": "0",
    "CUDA_CACHE_DISABLE": "1",
}
FORBIDDEN_IMPLICIT_BUILD_ENVIRONMENT = (
    "CPATH",
    "CPLUS_INCLUDE_PATH",
    "C_INCLUDE_PATH",
    "OBJC_INCLUDE_PATH",
    "LIBRARY_PATH",
    "COMPILER_PATH",
    "GCC_EXEC_PREFIX",
    "NVCC_PREPEND_FLAGS",
    "NVCC_APPEND_FLAGS",
    "CUDAFE_FLAGS",
    "PTXAS_FLAGS",
    "PYTHONPATH",
    "PYTHONHOME",
    "LD_PRELOAD",
    "LD_AUDIT",
    "LD_LIBRARY_PATH",
)
FORMAL_MAKE_EXECUTABLE = Path("/usr/bin/make")
FORBIDDEN_MAKE_ENVIRONMENT = frozenset(
    {
        b"BASHOPTS",
        b"BASH_ENV",
        b"ENV",
        b"GCONV_PATH",
        b"GLIBC_TUNABLES",
        b"LOCPATH",
        b"MAKEFILES",
        b"MAKEFILE_LIST",
        b"GNUMAKEFLAGS",
        b"MAKEFLAGS",
        b"MAKEOVERRIDES",
        b"MFLAGS",
        b"NLSPATH",
        b"SHELLOPTS",
    }
)
FORBIDDEN_MAKE_ENVIRONMENT_PREFIXES = (
    b"BASH_FUNC_",
    b"LD_",
)


class AttestationError(RuntimeError):
    """Raised when a formal native build cannot be attested."""


@dataclass(frozen=True)
class RegularFileSnapshot:
    """One descriptor-bound observation of a regular file."""

    path: Path
    status: os.stat_result
    sha256: str
    content: bytes | None
    xattrs: tuple[tuple[str, bytes], ...]


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _bounded_absolute_path(path: Path) -> Path:
    raw = os.fspath(path)
    if "\0" in raw:
        raise AttestationError("path contains NUL")
    absolute = Path(os.path.abspath(raw))
    try:
        encoded = str(absolute).encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise AttestationError(f"path is not strict UTF-8: {absolute!r}") from exc
    if len(encoded) > MAX_PATH_BYTES:
        raise AttestationError(f"path exceeds byte limit: {absolute}")
    if len(absolute.parts) > MAX_PATH_COMPONENTS:
        raise AttestationError(f"path has too many components: {absolute}")
    for component in absolute.parts[1:]:
        if (
            len(component.encode("utf-8", errors="strict"))
            > MAX_PATH_COMPONENT_BYTES
        ):
            raise AttestationError(
                f"path component exceeds byte limit: {absolute}"
            )
    return absolute


@contextmanager
def _open_directory_nofollow(path: Path) -> Iterator[tuple[int, Path]]:
    absolute = _bounded_absolute_path(path)
    flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_CLOEXEC
        | os.O_NOFOLLOW
        | os.O_NONBLOCK
    )
    descriptor = -1
    try:
        descriptor = os.open("/", flags)
        for component in absolute.parts[1:]:
            next_descriptor = os.open(
                component,
                flags,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        yield descriptor, absolute
    except OSError as exc:
        raise AttestationError(
            f"cannot open directory without symlink traversal: {absolute}: {exc}"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


@contextmanager
def _open_regular_nofollow(path: Path) -> Iterator[tuple[int, Path]]:
    absolute = _bounded_absolute_path(path)
    if absolute.name in ("", ".", ".."):
        raise AttestationError(f"invalid regular-file path: {absolute}")
    with _open_directory_nofollow(absolute.parent) as (
        parent_descriptor,
        _,
    ):
        descriptor = -1
        try:
            descriptor = os.open(
                absolute.name,
                os.O_RDONLY
                | os.O_CLOEXEC
                | os.O_NOFOLLOW
                | os.O_NONBLOCK,
                dir_fd=parent_descriptor,
            )
            yield descriptor, absolute
        except OSError as exc:
            raise AttestationError(
                f"cannot open regular file safely: {absolute}: {exc}"
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)


def _stable_status_fields(status: os.stat_result) -> tuple[int, ...]:
    return (
        status.st_dev,
        status.st_ino,
        status.st_mode,
        status.st_nlink,
        status.st_uid,
        status.st_gid,
        status.st_size,
        status.st_mtime_ns,
        status.st_ctime_ns,
    )


def _validate_regular_status(
    status: os.stat_result,
    path: Path,
    *,
    max_bytes: int,
) -> None:
    if not stat.S_ISREG(status.st_mode):
        raise AttestationError(f"input is not a regular file: {path}")
    if status.st_size < 0 or status.st_size > max_bytes:
        raise AttestationError(
            f"regular file exceeds {max_bytes} byte limit: {path}"
        )


def _bounded_xattrs_from_fd(
    descriptor: int,
    path: Path,
) -> tuple[tuple[str, bytes], ...]:
    try:
        names = sorted(os.listxattr(descriptor))
    except OSError as exc:
        raise AttestationError(f"cannot list xattrs for {path}: {exc}") from exc
    if len(names) > MAX_XATTR_COUNT:
        raise AttestationError(f"too many xattrs for {path}")
    aggregate_bytes = 0
    records: list[tuple[str, bytes]] = []
    for name in names:
        try:
            encoded_name = name.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise AttestationError(
                f"xattr name is not strict UTF-8 for {path}"
            ) from exc
        if len(encoded_name) > MAX_XATTR_NAME_BYTES:
            raise AttestationError(f"xattr name is too long for {path}")
        try:
            value = os.getxattr(descriptor, name)
        except OSError as exc:
            raise AttestationError(
                f"cannot read xattr {name!r} for {path}: {exc}"
            ) from exc
        if len(value) > MAX_XATTR_VALUE_BYTES:
            raise AttestationError(f"xattr value is too large for {path}")
        aggregate_bytes += len(encoded_name) + len(value)
        if aggregate_bytes > MAX_XATTR_AGGREGATE_BYTES:
            raise AttestationError(f"xattrs exceed aggregate limit for {path}")
        records.append((name, value))
    if any(name == "security.capability" for name, _ in records):
        raise AttestationError(f"security.capability is forbidden: {path}")
    return tuple(records)


def _snapshot_open_regular(
    descriptor: int,
    path: Path,
    *,
    max_bytes: int,
    include_content: bool = False,
    include_xattrs: bool = False,
) -> RegularFileSnapshot:
    digest = hashlib.sha256()
    chunks: list[bytes] | None = [] if include_content else None
    before = os.fstat(descriptor)
    _validate_regular_status(before, path, max_bytes=max_bytes)
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
    except OSError as exc:
        raise AttestationError(f"cannot seek regular file {path}: {exc}") from exc
    total = 0
    while True:
        remaining = max_bytes - total
        chunk = os.read(descriptor, min(READ_CHUNK_BYTES, remaining + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise AttestationError(
                f"regular file grew beyond {max_bytes} byte limit: {path}"
            )
        digest.update(chunk)
        if chunks is not None:
            chunks.append(chunk)
    if total != before.st_size:
        raise AttestationError(
            f"regular file size changed while reading: {path}"
        )
    xattrs = (
        _bounded_xattrs_from_fd(descriptor, path)
        if include_xattrs
        else ()
    )
    after = os.fstat(descriptor)
    if _stable_status_fields(before) != _stable_status_fields(after):
        raise AttestationError(
            f"regular file metadata changed while reading: {path}"
        )
    return RegularFileSnapshot(
        path=path,
        status=after,
        sha256=digest.hexdigest(),
        content=(b"".join(chunks) if chunks is not None else None),
        xattrs=xattrs,
    )


def snapshot_regular_file(
    path: Path,
    *,
    description: str,
    max_bytes: int,
    include_content: bool = False,
    include_xattrs: bool = False,
) -> RegularFileSnapshot:
    try:
        with _open_regular_nofollow(path) as (descriptor, absolute):
            return _snapshot_open_regular(
                descriptor,
                absolute,
                max_bytes=max_bytes,
                include_content=include_content,
                include_xattrs=include_xattrs,
            )
    except AttestationError as exc:
        raise AttestationError(f"{description}: {exc}") from exc


def sha256_file(
    path: Path,
    *,
    description: str = "identity input",
    max_bytes: int = MAX_IDENTITY_FILE_BYTES,
) -> str:
    return snapshot_regular_file(
        path,
        description=description,
        max_bytes=max_bytes,
    ).sha256


def capture_libsmctrl_git_snapshot(
    repo_root: Path,
    libsmctrl_dir: Path,
    *,
    module_snapshot: RegularFileSnapshot | None = None,
) -> Any:
    """Load the pinned raw-byte scanner and capture libsmctrl without filters."""

    module_path = _bounded_absolute_path(
        repo_root / "src" / "burstserve" / "git_provenance.py"
    )
    if module_snapshot is None:
        module_snapshot = snapshot_regular_file(
            module_path,
            description="safe Git provenance implementation",
            max_bytes=MAX_SOURCE_FILE_BYTES,
            include_content=True,
        )
    if module_snapshot.content is None:
        raise AttestationError("safe Git provenance source bytes are unavailable")
    if module_snapshot.path != module_path:
        raise AttestationError("safe Git provenance source path changed")
    module_path = module_snapshot.path
    module_name = "_burstserve_native_build_git_provenance"
    specification = importlib.util.spec_from_loader(
        module_name,
        loader=None,
        origin=str(module_path),
    )
    if specification is None:
        raise AttestationError(
            f"cannot load safe Git provenance implementation: {module_path}"
        )
    module = types.ModuleType(module_name)
    module.__file__ = str(module_path)
    module.__loader__ = None
    module.__package__ = ""
    module.__spec__ = specification
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = module
    previous_dont_write_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        code = compile(
            module_snapshot.content,
            str(module_path),
            "exec",
            dont_inherit=True,
        )
        exec(code, module.__dict__)
    except BaseException:
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous
        raise
    finally:
        sys.dont_write_bytecode = previous_dont_write_bytecode
    snapshot = module.capture_repository(
        libsmctrl_dir,
        expected_gitlinks={},
        allowed_untracked_roots=(),
        protected_roots=(),
        allow_untracked_regular_files=False,
        git=GIT,
    )
    if not snapshot.complete or snapshot.head_oid is None:
        detail = "; ".join(snapshot.errors) or "incomplete safe Git snapshot"
        raise AttestationError(
            f"cannot capture libsmctrl source identity safely: {detail}"
        )
    return snapshot


def canonical_json(
    value: object,
    *,
    max_bytes: int | None = None,
) -> bytes:
    encoder = json.JSONEncoder(
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    chunks: list[bytes] = []
    total = 0
    for fragment in encoder.iterencode(value):
        encoded = fragment.encode("utf-8", errors="strict")
        total += len(encoded)
        if max_bytes is not None and total + 1 > max_bytes:
            raise AttestationError(
                f"canonical JSON exceeds {max_bytes} byte limit"
            )
        chunks.append(encoded)
    chunks.append(b"\n")
    return b"".join(chunks)


def reject_duplicate_json_object(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise AttestationError(
                f"duplicate key in build attestation JSON: {key!r}"
            )
        result[key] = value
    return result


def reject_nonstandard_json_constant(value: str) -> object:
    raise AttestationError(
        f"non-standard constant in build attestation JSON: {value}"
    )


def _parse_bounded_json_integer(value: str) -> int:
    digits = value[1:] if value.startswith("-") else value
    if len(digits) > MAX_JSON_INTEGER_DIGITS:
        raise AttestationError("integer in build attestation JSON is too large")
    return int(value, 10)


def _parse_bounded_json_float(value: str) -> float:
    if len(value) > MAX_JSON_INTEGER_DIGITS:
        raise AttestationError("float in build attestation JSON is too large")
    result = float(value)
    if not math.isfinite(result):
        raise AttestationError("non-finite float in build attestation JSON")
    return result


JSON_WHITESPACE_BYTES = frozenset(b" \t\n\r")


def _reject_excessive_json_nesting(content: bytes) -> None:
    """Bound depth and node count lexically, before any graph is allocated.

    ``json.loads`` materializes the entire object graph before any post-parse
    validator can see it, so a flat array of millions of elements would already
    have been allocated by the time :func:`_validate_json_resources` runs.  This
    scanner is string- and escape-aware: structural bytes inside a JSON string
    are ignored, object keys are not counted as nodes, and each maximal run of
    bare literal bytes (number, ``true``, ``false``, ``null``) counts once, so
    the budget matches the post-parse node accounting for well-formed input and
    never rejects a document that the post-parse bound would accept.
    """

    depth = 0
    nodes = 0
    # False marks an array context, True an object context.
    containers: list[bool] = []
    expect_key = False
    in_string = False
    escaped = False
    in_literal = False

    def count_node() -> None:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_JSON_NODES:
            raise AttestationError("build attestation JSON has too many nodes")

    for byte in content:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:
                escaped = True
            elif byte == 0x22:
                in_string = False
                if expect_key:
                    expect_key = False
                else:
                    count_node()
            continue
        if byte == 0x22:
            in_string = True
            in_literal = False
            continue
        in_literal_byte = False
        if byte in (0x5B, 0x7B):
            count_node()
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise AttestationError(
                    "build attestation JSON exceeds nesting limit"
                )
            containers.append(byte == 0x7B)
            expect_key = byte == 0x7B
        elif byte in (0x5D, 0x7D):
            depth -= 1
            if containers:
                containers.pop()
            expect_key = False
            if depth < 0:
                break
        elif byte == 0x2C:
            expect_key = bool(containers) and containers[-1]
        elif byte == 0x3A:
            expect_key = False
        elif byte in JSON_WHITESPACE_BYTES:
            pass
        else:
            in_literal_byte = True
            if not in_literal:
                count_node()
        in_literal = in_literal_byte


def _validate_json_resources(value: object) -> None:
    stack: list[tuple[object, int]] = [(value, 0)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES:
            raise AttestationError("build attestation JSON has too many nodes")
        if depth > MAX_JSON_DEPTH:
            raise AttestationError(
                "build attestation JSON exceeds nesting limit"
            )
        if isinstance(current, dict):
            for key, child in current.items():
                try:
                    encoded_key = key.encode("utf-8", errors="strict")
                except UnicodeEncodeError as exc:
                    raise AttestationError(
                        "build attestation JSON key is not valid UTF-8"
                    ) from exc
                if len(encoded_key) > MAX_JSON_STRING_BYTES:
                    raise AttestationError(
                        "build attestation JSON key exceeds byte limit"
                    )
                stack.append((child, depth + 1))
        elif isinstance(current, list):
            stack.extend((child, depth + 1) for child in current)
        elif isinstance(current, str):
            try:
                encoded_value = current.encode("utf-8", errors="strict")
            except UnicodeEncodeError as exc:
                raise AttestationError(
                    "build attestation JSON string is not valid UTF-8"
                ) from exc
            if len(encoded_value) > MAX_JSON_STRING_BYTES:
                raise AttestationError(
                    "build attestation JSON string exceeds byte limit"
                )
        elif isinstance(current, float) and not math.isfinite(current):
            raise AttestationError("non-finite float in build attestation JSON")


def exact_json_equal(left: object, right: object, *, depth: int = 0) -> bool:
    """Compare two decoded JSON values by exact type and structure.

    Plain ``==`` treats ``True``, ``1`` and ``1.0`` as the same value, so a
    substituted boolean/integer/float would compare equal to the expected
    attestation.  Every node must therefore match its exact Python type.
    """

    if depth > MAX_JSON_DEPTH:
        raise AttestationError("build attestation JSON exceeds nesting limit")
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        assert isinstance(right, dict)
        if len(left) != len(right):
            return False
        for key, value in left.items():
            # A JSON object key is always a string.  Requiring that here keeps
            # dict lookup, which compares keys with ``==``, from matching a
            # ``True``/``1`` key pair the way it would for values.
            if not isinstance(key, str):
                return False
            if key not in right:
                return False
            if not exact_json_equal(value, right[key], depth=depth + 1):
                return False
        return all(isinstance(key, str) for key in right)
    if isinstance(left, list):
        assert isinstance(right, list)
        if len(left) != len(right):
            return False
        return all(
            exact_json_equal(item, other, depth=depth + 1)
            for item, other in zip(left, right)
        )
    return left == right


def parse_canonical_json(content: bytes) -> object:
    if len(content) > MAX_ATTESTATION_BYTES:
        raise AttestationError("build attestation JSON exceeds byte limit")
    _reject_excessive_json_nesting(content)
    try:
        decoded = content.decode("utf-8", errors="strict")
        value = json.loads(
            decoded,
            object_pairs_hook=reject_duplicate_json_object,
            parse_constant=reject_nonstandard_json_constant,
            parse_int=_parse_bounded_json_integer,
            parse_float=_parse_bounded_json_float,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise AttestationError(
            f"cannot decode build attestation JSON: {exc}"
        ) from exc
    _validate_json_resources(value)
    if content != canonical_json(value, max_bytes=MAX_ATTESTATION_BYTES):
        raise AttestationError(
            "build attestation JSON bytes are not the canonical encoding"
        )
    return value


def _bounded_proc_read(path: str, *, max_bytes: int) -> bytes:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | os.O_CLOEXEC
            | os.O_NOFOLLOW
            | os.O_NONBLOCK,
        )
        chunks: list[bytes] = []
        total = 0
        while True:
            remaining = max_bytes - total
            chunk = os.read(descriptor, min(64 * 1024, remaining + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise AttestationError(
                    f"proc record exceeds {max_bytes} byte limit: {path}"
                )
            chunks.append(chunk)
        return b"".join(chunks)
    except OSError as exc:
        raise AttestationError(f"cannot inspect {path}: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def proc_parent_pid(pid: int) -> int:
    try:
        content = _bounded_proc_read(
            f"/proc/{pid}/stat",
            max_bytes=MAX_PROC_STAT_BYTES,
        ).decode("ascii", errors="strict")
    except UnicodeDecodeError as exc:
        raise AttestationError(
            f"/proc/{pid}/stat is not strict ASCII"
        ) from exc
    closing = content.rfind(")")
    if closing < 0:
        raise AttestationError(f"malformed /proc/{pid}/stat")
    fields = content[closing + 2 :].split()
    if len(fields) < 2:
        raise AttestationError(f"truncated /proc/{pid}/stat")
    try:
        return int(fields[1], 10)
    except ValueError as exc:
        raise AttestationError(f"invalid parent pid in /proc/{pid}/stat") from exc


def find_formal_make_ancestor() -> int:
    expected_executable = FORMAL_MAKE_EXECUTABLE.resolve(strict=True)
    pid = os.getppid()
    for _ in range(16):
        if pid <= 1:
            break
        try:
            executable = Path(os.readlink(f"/proc/{pid}/exe")).resolve(
                strict=True
            )
        except OSError as exc:
            raise AttestationError(
                f"cannot inspect executable for ancestor pid {pid}: {exc}"
            ) from exc
        if executable == expected_executable:
            return pid
        pid = proc_parent_pid(pid)
    raise AttestationError(
        "parse guard is not descended from the pinned /usr/bin/make"
    )


def proc_nul_records(pid: int, name: str) -> list[bytes]:
    content = _bounded_proc_read(
        f"/proc/{pid}/{name}",
        max_bytes=MAX_PROC_VECTOR_BYTES,
    )
    if content and not content.endswith(b"\0"):
        raise AttestationError(f"/proc/{pid}/{name} is not NUL terminated")
    records = [record for record in content.split(b"\0") if record]
    if len(records) > MAX_PROC_RECORDS:
        raise AttestationError(f"/proc/{pid}/{name} has too many records")
    return records


def is_forbidden_make_environment_name(name: bytes) -> bool:
    return (
        name in FORBIDDEN_MAKE_ENVIRONMENT
        or name.startswith(FORBIDDEN_MAKE_ENVIRONMENT_PREFIXES)
    )


def is_special_make_assignment(argument: str) -> bool:
    name, separator, _ = argument.partition("=")
    if not separator:
        return False
    normalized = name.rstrip(":+?!")
    return normalized in {
        "MAKEFILES",
        "MAKEFILE_LIST",
        "GNUMAKEFLAGS",
        "MAKEFLAGS",
        "MAKEOVERRIDES",
        "MFLAGS",
    }


def is_eval_option(argument: str) -> bool:
    if argument == "-E" or argument.startswith("-E"):
        return True
    if argument.startswith("-") and not argument.startswith("--"):
        return "E" in argument[1:]
    if argument.startswith("--"):
        option = argument.split("=", 1)[0]
        return len(option) >= 3 and "--eval".startswith(option)
    return False


def validate_formal_make_invocation(
    source_dir: Path,
    expected_makefile: Path,
) -> None:
    source = source_dir.resolve(strict=True)
    makefile = expected_makefile.resolve(strict=True)
    if makefile != source / "Makefile":
        raise AttestationError("formal Makefile is not the source-root Makefile")

    make_pid = find_formal_make_ancestor()
    try:
        make_cwd = Path(os.readlink(f"/proc/{make_pid}/cwd")).resolve(
            strict=True
        )
    except OSError as exc:
        raise AttestationError(
            f"cannot inspect working directory for make pid {make_pid}: {exc}"
        ) from exc
    if make_cwd != source:
        raise AttestationError(
            f"formal make cwd mismatch: expected {source}, observed {make_cwd}"
        )

    environment_names = {
        record.partition(b"=")[0]
        for record in proc_nul_records(make_pid, "environ")
    }
    injected_environment_bytes = sorted(
        name
        for name in environment_names
        if is_forbidden_make_environment_name(name)
    )
    injected_environment = [
        name.decode("ascii", errors="backslashreplace")
        for name in injected_environment_bytes
    ]
    if injected_environment:
        raise AttestationError(
            "forbidden make/shell/dynamic-process environment present: "
            + ",".join(injected_environment)
        )

    try:
        arguments = [
            record.decode("utf-8", errors="strict")
            for record in proc_nul_records(make_pid, "cmdline")
        ]
    except UnicodeDecodeError as exc:
        raise AttestationError("make argv is not strict UTF-8") from exc
    if not arguments:
        raise AttestationError("make argv is empty")

    file_arguments: list[str] = []
    index = 1
    while index < len(arguments):
        argument = arguments[index]
        if is_eval_option(argument):
            raise AttestationError(
                f"external GNU make --eval/-E is forbidden: {argument!r}"
            )
        if is_special_make_assignment(argument):
            raise AttestationError(
                f"make-control variable assignment is forbidden: {argument!r}"
            )
        if argument == "-f":
            index += 1
            if index >= len(arguments):
                raise AttestationError("GNU make -f is missing its argument")
            file_arguments.append(arguments[index])
        elif argument.startswith("-f"):
            raise AttestationError(
                "attached or clustered GNU make -f is forbidden"
            )
        elif argument.startswith("--"):
            option = argument.split("=", 1)[0]
            if (
                len(option) >= 3
                and (
                    "--file".startswith(option)
                    or "--makefile".startswith(option)
                )
            ):
                raise AttestationError(
                    f"external GNU makefile option is forbidden: {argument!r}"
                )
        index += 1

    if len(file_arguments) > 1:
        raise AttestationError("multiple GNU make -f inputs are forbidden")
    if file_arguments:
        supplied_makefile = Path(file_arguments[0])
        if not supplied_makefile.is_absolute():
            supplied_makefile = make_cwd / supplied_makefile
        try:
            supplied_makefile = supplied_makefile.resolve(strict=True)
        except OSError as exc:
            raise AttestationError(
                f"cannot resolve supplied GNU makefile: {exc}"
            ) from exc
        if supplied_makefile != makefile:
            raise AttestationError(
                f"external GNU makefile is forbidden: {supplied_makefile}"
            )


def build_subprocess_environment(
    configuration: Mapping[str, str],
    *,
    git: bool = False,
) -> dict[str, str]:
    environment = {
        **BASE_BUILD_ENVIRONMENT,
        "PATH": configuration["hermetic_path"],
        "TMPDIR": configuration["build_tmpdir"],
    }
    if git:
        environment.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_TERMINAL_PROMPT": "0",
            }
        )
    return environment


class _UnreadableProcRecord:
    """Sentinel for a ``/proc`` record that could not be classified."""


_PROC_RECORD_UNREADABLE = _UnreadableProcRecord()


def _proc_stat_record(
    path: str,
) -> tuple[str, int] | None | _UnreadableProcRecord:
    """Classify one ``stat`` path as ``(state, pgid)``, gone, or unreadable.

    The kernel prints ``comm`` as raw bytes, so a live process whose name
    contains any byte outside ASCII would make a whole-record strict decode
    fail.  The record is therefore parsed as bytes and only the fields after
    the final ``)`` — which the kernel always writes as ASCII — are decoded.

    Only a vanished pid may be reported as absent.  This function is the proof
    that a build subprocess group is empty, so every other read or parse
    failure is reported as unreadable and is treated by the caller as a
    possibly live member rather than being silently skipped.
    """

    try:
        content = _bounded_proc_read(
            path,
            max_bytes=MAX_PROC_STAT_BYTES,
        )
    except AttestationError as exc:
        cause = exc.__cause__
        if isinstance(cause, OSError) and cause.errno in (
            errno.ENOENT,
            errno.ESRCH,
        ):
            # The process exited between the directory listing and this read.
            return None
        return _PROC_RECORD_UNREADABLE
    closing = content.rfind(b")")
    if closing < 0:
        return _PROC_RECORD_UNREADABLE
    try:
        trailer = content[closing + 2 :].decode("ascii", errors="strict")
    except UnicodeDecodeError:
        return _PROC_RECORD_UNREADABLE
    fields = trailer.split()
    if len(fields) < 3:
        return _PROC_RECORD_UNREADABLE
    try:
        return fields[0], int(fields[2], 10)
    except ValueError:
        return _PROC_RECORD_UNREADABLE


def _proc_state_and_group(
    pid_text: str,
) -> tuple[str, int] | None | _UnreadableProcRecord:
    return _proc_stat_record(f"/proc/{pid_text}/stat")


def _thread_group_snapshot_has_live_thread(pid_text: str) -> bool:
    """Report whether one ``/proc/<tgid>/task`` snapshot holds a live thread."""

    try:
        threads = os.listdir(f"/proc/{pid_text}/task")
    except (FileNotFoundError, ProcessLookupError, NotADirectoryError):
        return False
    except OSError:
        return True
    if len(threads) > MAX_PROC_RECORDS:
        raise AttestationError(
            f"thread group {pid_text} has too many threads to verify safely"
        )
    for thread in threads:
        if not thread.isdigit():
            continue
        record = _proc_stat_record(f"/proc/{pid_text}/task/{thread}/stat")
        if record is None:
            continue
        if isinstance(record, _UnreadableProcRecord):
            return True
        state, _ = record
        if state != "Z":
            return True
    return False


def _thread_group_has_live_thread(pid_text: str) -> bool:
    """Report whether a ``Z`` thread group still holds a runnable thread.

    ``/proc/<tgid>/stat`` reports state ``Z`` as soon as the leader thread
    exits, even while the group's other threads keep running, so a zombie
    leader is only evidence of death when no sibling thread is alive.  An
    unreadable sibling counts as alive.

    Listing the task directory is not atomic with reading each thread's state,
    so a thread cloned after a listing would not be examined.  Two consecutive
    all-dead snapshots are therefore required, which narrows that window but
    does not close it on its own.  The caller's actual guarantee is ordering:
    the whole process group has already been sent ``SIGKILL``, after which the
    kernel refuses to complete a fork or clone into it.
    """

    return any(
        _thread_group_snapshot_has_live_thread(pid_text) for _ in range(2)
    )


def _live_process_group_members(process_group: int) -> list[int]:
    """List thread groups still observed in the group after it was signalled.

    A pid whose record cannot be classified is reported as a member even when
    its real group is unknown.  That can only delay the caller into a bounded
    fail-closed error, whereas skipping it could report a live group as empty.
    A ``Z`` leader is only skipped once its thread group is observed dead.

    This scan is not a standalone proof that no member can execute: it is only
    sound because :func:`_quiesce_process_group` has already ``SIGKILL``ed the
    whole group, which stops any new task from entering it.
    """

    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return []
    except OSError as exc:
        raise AttestationError(
            f"cannot inspect build subprocess group {process_group}: {exc}"
        ) from exc
    try:
        entries = os.listdir("/proc")
    except OSError as exc:
        raise AttestationError(
            f"cannot enumerate /proc for build subprocess group "
            f"{process_group}: {exc}"
        ) from exc
    if len(entries) > MAX_PROC_RECORDS:
        raise AttestationError("/proc has too many records to verify safely")
    members: list[int] = []
    for entry in entries:
        if not entry.isdigit():
            continue
        record = _proc_state_and_group(entry)
        if record is None:
            continue
        if isinstance(record, _UnreadableProcRecord):
            members.append(int(entry, 10))
            continue
        state, group = record
        if group != process_group:
            continue
        if state != "Z" or _thread_group_has_live_thread(entry):
            members.append(int(entry, 10))
    return members


def _quiesce_process_group(process: subprocess.Popen[bytes]) -> None:
    """Kill the whole session/group and boundedly verify that it is empty.

    ``start_new_session`` makes the direct child both session and process-group
    leader, so every descendant it did not deliberately detach shares its
    process-group id.  A descendant that closes the inherited pipes and stays
    alive would otherwise survive a normal exit of the direct child, so the
    group is signalled and then verified on every path, not only on error.
    """

    process_group = process.pid
    kill_error: OSError | None = None
    try:
        os.killpg(process_group, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError as exc:
        kill_error = exc
    # The leader is reaped on every path, including a failed signal, so a
    # signalling error can never additionally leak an unreaped child.  Reusing
    # the pid as a group id is safe because Linux keeps a ``struct pid``
    # allocated while any task still references it under any type, including
    # PIDTYPE_PGID, so the number cannot be recycled while the group is
    # non-empty.
    try:
        process.wait(timeout=SUBPROCESS_REAP_SECONDS)
    except (OSError, subprocess.SubprocessError) as exc:
        raise AttestationError(
            f"cannot reap build subprocess {process_group}: {exc}"
        ) from exc
    if kill_error is not None:
        raise AttestationError(
            f"cannot signal build subprocess group {process_group}: "
            f"{kill_error}"
        ) from kill_error
    deadline = time.monotonic() + PROCESS_GROUP_QUIESCE_SECONDS
    while True:
        members = _live_process_group_members(process_group)
        if not members:
            return
        if time.monotonic() >= deadline:
            raise AttestationError(
                "build subprocess group did not terminate within "
                f"{PROCESS_GROUP_QUIESCE_SECONDS} seconds: "
                f"{process_group}: {sorted(members)}"
            )
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            return
        except OSError as exc:
            raise AttestationError(
                f"cannot signal build subprocess group {process_group}: {exc}"
            ) from exc
        time.sleep(PROCESS_GROUP_POLL_SECONDS)


def run_command(
    arguments: Sequence[str],
    configuration: Mapping[str, str],
    *,
    cwd: Path | None = None,
    require_success: bool = True,
    git: bool = False,
    pass_fds: Sequence[int] = (),
) -> tuple[int, bytes, bytes]:
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            list(arguments),
            cwd=cwd,
            env=build_subprocess_environment(configuration, git=git),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            close_fds=True,
            pass_fds=tuple(pass_fds),
        )
        if process.stdout is None or process.stderr is None:
            raise AttestationError("subprocess pipes were not created")
        selector = selectors.DefaultSelector()
        try:
            streams = {
                process.stdout.fileno(): (
                    "stdout",
                    bytearray(),
                    process.stdout,
                ),
                process.stderr.fileno(): (
                    "stderr",
                    bytearray(),
                    process.stderr,
                ),
            }
            for descriptor in streams:
                os.set_blocking(descriptor, False)
                selector.register(descriptor, selectors.EVENT_READ)
            deadline = time.monotonic() + SUBPROCESS_TIMEOUT_SECONDS
            while selector.get_map() or process.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AttestationError(
                        "command timed out while building attestation: "
                        f"{arguments!r}"
                    )
                events = selector.select(min(remaining, 0.25))
                for key, _ in events:
                    name, output, _pipe = streams[key.fd]
                    try:
                        chunk = os.read(key.fd, 64 * 1024)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fd)
                        continue
                    output.extend(chunk)
                    if len(output) > MAX_SUBPROCESS_STREAM_BYTES:
                        raise AttestationError(
                            f"command {name} exceeds "
                            f"{MAX_SUBPROCESS_STREAM_BYTES} byte limit: "
                            f"{arguments!r}"
                        )
            remaining = max(0.0, deadline - time.monotonic())
            returncode = process.wait(timeout=remaining)
            stdout = bytes(streams[process.stdout.fileno()][1])
            stderr = bytes(streams[process.stderr.fileno()][1])
        finally:
            selector.close()
            process.stdout.close()
            process.stderr.close()
    except (OSError, subprocess.SubprocessError) as exc:
        raise AttestationError(
            f"command failed while building attestation: {arguments!r}: {exc}"
        ) from exc
    finally:
        if process is not None:
            _quiesce_process_group(process)
    if require_success and returncode != 0:
        raise AttestationError(
            f"command failed while building attestation: {arguments!r}: "
            f"exit={returncode}, "
            f"stderr={stderr.decode(errors='replace')!r}"
        )
    return returncode, stdout, stderr


def command_tokens(command: str) -> list[str]:
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        raise AttestationError(f"invalid tool command {command!r}: {exc}") from exc
    if len(tokens) != 1:
        raise AttestationError(
            f"tool command must be one absolute executable path: {command!r}"
        )
    if not Path(tokens[0]).is_absolute():
        raise AttestationError(f"tool path must be absolute: {tokens[0]!r}")
    return tokens


def required_file(path: Path, description: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise AttestationError(f"{description} is unavailable: {path}: {exc}") from exc
    try:
        with _open_regular_nofollow(resolved) as (descriptor, absolute):
            status = os.fstat(descriptor)
            if not stat.S_ISREG(status.st_mode):
                raise AttestationError(
                    f"{description} is not a regular file: {absolute}"
                )
    except AttestationError as exc:
        raise AttestationError(f"{description} is unavailable: {exc}") from exc
    return resolved


def resolve_tool(command: str, description: str) -> Path:
    return required_file(Path(command_tokens(command)[0]), description)


def version_record(
    executable: Path,
    configuration: Mapping[str, str],
    *,
    require_success: bool = True,
    invocation_prefix: Sequence[str] = (),
) -> dict[str, Any]:
    returncode, stdout, stderr = run_command(
        [str(executable), *invocation_prefix, "--version"],
        configuration,
        require_success=require_success,
    )
    return {
        "returncode": returncode,
        "stdout_stderr_sha256": sha256_bytes(stdout + b"\0" + stderr),
    }


def tree_digest(root: Path) -> str:
    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise AttestationError(f"identity tree is unavailable: {root}: {exc}") from exc

    records: list[tuple[bytes, str]] = []
    entry_count = 0
    file_count = 0
    aggregate_path_bytes = 0
    aggregate_file_bytes = 0
    directory_flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_CLOEXEC
        | os.O_NOFOLLOW
        | os.O_NONBLOCK
    )

    def visit(
        directory_descriptor: int,
        relative_parts: tuple[str, ...],
        depth: int,
    ) -> None:
        nonlocal entry_count
        nonlocal file_count
        nonlocal aggregate_path_bytes
        nonlocal aggregate_file_bytes
        if depth > MAX_TREE_DEPTH:
            raise AttestationError(f"identity tree exceeds depth limit: {resolved}")
        before_directory = os.fstat(directory_descriptor)
        if not stat.S_ISDIR(before_directory.st_mode):
            raise AttestationError(
                f"identity tree entry is not a directory: {resolved}"
            )
        try:
            iterator = os.scandir(directory_descriptor)
        except OSError as exc:
            raise AttestationError(
                f"cannot enumerate identity tree {resolved}: {exc}"
            ) from exc
        with iterator:
            for entry in iterator:
                entry_count += 1
                if entry_count > MAX_TREE_ENTRIES:
                    raise AttestationError(
                        f"identity tree exceeds entry limit: {resolved}"
                    )
                try:
                    encoded_name = entry.name.encode(
                        "utf-8",
                        errors="strict",
                    )
                except UnicodeEncodeError as exc:
                    raise AttestationError(
                        f"identity tree path is not strict UTF-8: {entry.name!r}"
                    ) from exc
                if len(encoded_name) > MAX_PATH_COMPONENT_BYTES:
                    raise AttestationError(
                        f"identity tree component exceeds byte limit: "
                        f"{entry.name!r}"
                    )
                parts = (*relative_parts, entry.name)
                relative_text = "/".join(parts)
                relative = relative_text.encode("utf-8")
                if len(relative) > MAX_TREE_RELATIVE_PATH_BYTES:
                    raise AttestationError(
                        f"identity tree path exceeds byte limit: {relative_text}"
                    )
                aggregate_path_bytes += len(relative)
                if aggregate_path_bytes > MAX_TREE_AGGREGATE_PATH_BYTES:
                    raise AttestationError(
                        f"identity tree paths exceed aggregate limit: {resolved}"
                    )
                try:
                    observed = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    raise AttestationError(
                        f"cannot stat identity tree entry {relative_text}: {exc}"
                    ) from exc
                if stat.S_ISDIR(observed.st_mode):
                    child_descriptor = -1
                    try:
                        child_descriptor = os.open(
                            entry.name,
                            directory_flags,
                            dir_fd=directory_descriptor,
                        )
                        opened = os.fstat(child_descriptor)
                        if (
                            opened.st_dev != observed.st_dev
                            or opened.st_ino != observed.st_ino
                            or not stat.S_ISDIR(opened.st_mode)
                        ):
                            raise AttestationError(
                                "identity tree directory changed while opening: "
                                f"{relative_text}"
                            )
                        visit(child_descriptor, parts, depth + 1)
                    except OSError as exc:
                        raise AttestationError(
                            f"cannot open identity tree directory "
                            f"{relative_text}: {exc}"
                        ) from exc
                    finally:
                        if child_descriptor >= 0:
                            os.close(child_descriptor)
                elif stat.S_ISREG(observed.st_mode):
                    file_count += 1
                    if file_count > MAX_TREE_FILES:
                        raise AttestationError(
                            f"identity tree exceeds file limit: {resolved}"
                        )
                    file_descriptor = -1
                    try:
                        file_descriptor = os.open(
                            entry.name,
                            os.O_RDONLY
                            | os.O_CLOEXEC
                            | os.O_NOFOLLOW
                            | os.O_NONBLOCK,
                            dir_fd=directory_descriptor,
                        )
                        opened = os.fstat(file_descriptor)
                        if (
                            opened.st_dev != observed.st_dev
                            or opened.st_ino != observed.st_ino
                        ):
                            raise AttestationError(
                                "identity tree file changed while opening: "
                                f"{relative_text}"
                            )
                        snapshot = _snapshot_open_regular(
                            file_descriptor,
                            resolved / relative_text,
                            max_bytes=MAX_TREE_FILE_BYTES,
                        )
                    except OSError as exc:
                        raise AttestationError(
                            f"cannot open identity tree file "
                            f"{relative_text}: {exc}"
                        ) from exc
                    finally:
                        if file_descriptor >= 0:
                            os.close(file_descriptor)
                    aggregate_file_bytes += snapshot.status.st_size
                    if aggregate_file_bytes > MAX_TREE_AGGREGATE_BYTES:
                        raise AttestationError(
                            f"identity tree exceeds aggregate byte limit: "
                            f"{resolved}"
                        )
                    records.append((relative, snapshot.sha256))
                else:
                    raise AttestationError(
                        "identity tree contains unsupported file type: "
                        f"{relative_text}"
                    )
        after_directory = os.fstat(directory_descriptor)
        if _stable_status_fields(before_directory) != _stable_status_fields(
            after_directory
        ):
            raise AttestationError(
                f"identity tree directory changed while reading: {resolved}"
            )

    with _open_directory_nofollow(resolved) as (root_descriptor, absolute):
        root_status = os.fstat(root_descriptor)
        if not stat.S_ISDIR(root_status.st_mode):
            raise AttestationError(f"tree root is not a directory: {absolute}")
        visit(root_descriptor, (), 0)
    if not records:
        raise AttestationError(f"identity tree is empty: {resolved}")
    digest = hashlib.sha256()
    for relative, file_digest in sorted(records):
        digest.update(relative)
        digest.update(b"\0")
        digest.update(file_digest.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def cuda_version_file(cuda_home: Path) -> Path:
    for name in ("version.json", "version.txt"):
        candidate = cuda_home / name
        try:
            return required_file(candidate, f"CUDA version metadata {name}")
        except AttestationError:
            continue
    raise AttestationError(f"CUDA version metadata is missing under {cuda_home}")


def load_configuration() -> dict[str, str]:
    configuration: dict[str, str] = {}
    for key, environment_name in CONFIG_ENVIRONMENT.items():
        value = os.environ.get(environment_name)
        if value is None or value == "":
            raise AttestationError(
                f"required build environment {environment_name} is missing"
            )
        configuration[key] = value
    for name in FORBIDDEN_IMPLICIT_BUILD_ENVIRONMENT:
        if name in os.environ:
            raise AttestationError(
                f"implicit build injection variable survived env allowlist: {name}"
            )
    for tool in ("nvcc", "cc", "ar"):
        command_tokens(configuration[tool])
    build_dir = _bounded_absolute_path(Path(configuration["build_dir"]))
    build_tmpdir = _bounded_absolute_path(
        Path(configuration["build_tmpdir"])
    )
    build_lock_path = _bounded_absolute_path(
        Path(configuration["build_lock_path"])
    )
    strict_metadata(build_dir, expected_mode=0o700, kind="directory")
    strict_metadata(build_tmpdir, expected_mode=0o700, kind="directory")
    strict_metadata(
        build_lock_path.parent,
        expected_mode=0o700,
        kind="directory",
    )
    strict_metadata(build_lock_path, expected_mode=0o600)
    configuration["build_dir"] = str(build_dir)
    configuration["build_tmpdir"] = str(build_tmpdir)
    configuration["build_lock_path"] = str(build_lock_path)
    return configuration


def build_environment_contract(
    configuration: Mapping[str, str],
) -> dict[str, Any]:
    return {
        "allowlist": build_subprocess_environment(configuration),
        "explicit_make_to_attestation_variables": sorted(
            CONFIG_ENVIRONMENT.values()
        ),
        "forbidden_implicit_variables": list(
            FORBIDDEN_IMPLICIT_BUILD_ENVIRONMENT
        ),
        "compiler_invocations_use_env_i": True,
        "nvcc_explicit_ccbin": configuration["cc"] in configuration["nvccflags"],
    }


def normalized_fingerprint(data: bytes) -> str:
    normalized = re.sub(
        rb"tmpxft_[0-9A-Fa-f]+_",
        b"tmpxft_<PROCESS>_",
        data,
    )
    normalized = re.sub(
        rb"/[^ \t\r\n\"']*/cc[0-9A-Za-z]+\.(?:s|o|res)",
        b"<CC_TEMP>",
        normalized,
    )
    return sha256_bytes(normalized)


def cuda_subtool_records(
    cuda_home: Path,
    configuration: Mapping[str, str],
) -> list[dict[str, Any]]:
    candidates = (
        (cuda_home / "nvvm" / "bin" / "cicc", True),
        (cuda_home / "bin" / "cudafe++", True),
        (cuda_home / "bin" / "ptxas", True),
        (cuda_home / "bin" / "fatbinary", True),
        (cuda_home / "bin" / "nvlink", True),
        (cuda_home / "bin" / "crt" / "link.stub", False),
        (cuda_home / "bin" / "crt" / "prelink.stub", False),
    )
    records: list[dict[str, Any]] = []
    for candidate, executable_component in candidates:
        executable = required_file(candidate, f"CUDA subtool {candidate.name}")
        records.append(
            {
                "name": candidate.name,
                "path": str(executable),
                "sha256": sha256_file(executable),
                "version": (
                    version_record(
                        executable,
                        configuration,
                        require_success=False,
                    )
                    if executable_component
                    else None
                ),
            }
        )
    return records


def compiler_reported_file(
    cc: Path,
    query: str,
    configuration: Mapping[str, str],
    description: str,
) -> Path:
    _, stdout, _ = run_command(
        [str(cc), f"-print-file-name={query}"],
        configuration,
    )
    value = stdout.decode("utf-8", errors="strict").strip()
    if not value or value == query:
        raise AttestationError(f"host compiler could not resolve {description}")
    return required_file(Path(value), description)


def compiler_program(
    cc: Path,
    name: str,
    configuration: Mapping[str, str],
) -> Path:
    _, stdout, _ = run_command(
        [str(cc), f"-print-prog-name={name}"],
        configuration,
    )
    value = stdout.decode("utf-8", errors="strict").strip()
    if not value:
        raise AttestationError(f"host compiler did not report {name}")
    candidate = Path(value)
    if candidate.is_absolute() or "/" in value:
        return required_file(candidate, f"host compiler component {name}")
    located = shutil.which(
        value,
        path=build_subprocess_environment(configuration)["PATH"],
    )
    if located is None:
        raise AttestationError(f"cannot resolve host compiler component {name}")
    return required_file(Path(located), f"host compiler component {name}")


def host_toolchain_component_records(
    cc: Path,
    configuration: Mapping[str, str],
) -> list[dict[str, str]]:
    paths = {
        "cc1": compiler_program(cc, "cc1", configuration),
        "as": compiler_program(cc, "as", configuration),
        "ld": compiler_program(cc, "ld", configuration),
        "collect2": compiler_program(cc, "collect2", configuration),
        "lto-wrapper": compiler_program(cc, "lto-wrapper", configuration),
        "liblto_plugin.so": compiler_reported_file(
            cc,
            "liblto_plugin.so",
            configuration,
            "liblto_plugin.so",
        ),
        "crt1.o": compiler_reported_file(
            cc, "crt1.o", configuration, "crt1.o"
        ),
        "Scrt1.o": compiler_reported_file(
            cc, "Scrt1.o", configuration, "Scrt1.o"
        ),
        "crti.o": compiler_reported_file(
            cc, "crti.o", configuration, "crti.o"
        ),
        "crtbegin.o": compiler_reported_file(
            cc, "crtbegin.o", configuration, "crtbegin.o"
        ),
        "crtbeginS.o": compiler_reported_file(
            cc, "crtbeginS.o", configuration, "crtbeginS.o"
        ),
        "crtbeginT.o": compiler_reported_file(
            cc, "crtbeginT.o", configuration, "crtbeginT.o"
        ),
        "crtend.o": compiler_reported_file(
            cc, "crtend.o", configuration, "crtend.o"
        ),
        "crtendS.o": compiler_reported_file(
            cc, "crtendS.o", configuration, "crtendS.o"
        ),
        "crtn.o": compiler_reported_file(
            cc, "crtn.o", configuration, "crtn.o"
        ),
    }
    return [
        {
            "name": name,
            "path": str(path),
            "sha256": sha256_file(path),
        }
        for name, path in sorted(paths.items())
    ]


def compiler_fingerprints(
    cc: Path,
    configuration: Mapping[str, str],
) -> dict[str, str]:
    _, search_stdout, search_stderr = run_command(
        [str(cc), "-print-search-dirs"],
        configuration,
    )
    _, include_stdout, include_stderr = run_command(
        [str(cc), "-E", "-x", "c", "-v", str(DEV_NULL)],
        configuration,
    )
    _, dry_stdout, dry_stderr = run_command(
        [
            str(cc),
            "-###",
            "-x",
            "c",
            str(DEV_NULL),
            "-o",
            "/tmp/burstserve-smctrl-probe-cc-dryrun",
        ],
        configuration,
    )
    _, specs_stdout, specs_stderr = run_command(
        [str(cc), "-dumpspecs"],
        configuration,
    )
    return {
        "search_paths_sha256": sha256_bytes(
            search_stdout + b"\0" + search_stderr
        ),
        "include_search_sha256": sha256_bytes(
            include_stdout + b"\0" + include_stderr
        ),
        "dryrun_sha256": normalized_fingerprint(
            dry_stdout + b"\0" + dry_stderr
        ),
        "specs_sha256": sha256_bytes(specs_stdout + b"\0" + specs_stderr),
    }


def nvcc_dryrun_fingerprint(
    nvcc: Path,
    configuration: Mapping[str, str],
) -> str:
    arguments = [
        str(nvcc),
        *shlex.split(configuration["cppflags"]),
        *shlex.split(configuration["nvccflags"]),
        "--dryrun",
        "-c",
        str(Path(configuration["source_dir"]) / "smid_probe.cu"),
        "-o",
        "/tmp/burstserve-smctrl-probe-nvcc-dryrun.o",
    ]
    _, stdout, stderr = run_command(arguments, configuration)
    return normalized_fingerprint(stdout + b"\0" + stderr)


def libcuda_link_library(
    cc: Path,
    configuration: Mapping[str, str],
) -> Path:
    return compiler_reported_file(
        cc,
        "libcuda.so",
        configuration,
        "link-time libcuda",
    )


def collect_identity(
    configuration: Mapping[str, str],
) -> tuple[dict[str, str], dict[str, Any]]:
    source_dir = Path(configuration["source_dir"]).resolve(strict=True)
    repo_root = Path(configuration["repo_root"]).resolve(strict=True)
    cuda_home = Path(configuration["cuda_home"]).resolve(strict=True)
    libsmctrl_dir = Path(configuration["libsmctrl_dir"]).resolve(strict=True)
    build_lock_path = Path(configuration["build_lock_path"])

    source_paths = {
        "MAKEFILE_SHA256": source_dir / "Makefile",
        "README_SHA256": source_dir / "README.md",
        "PROBE_CU_SHA256": source_dir / "smid_probe.cu",
        "GUARD_LAUNCHER_C_SHA256": source_dir / "guard_launcher.c",
        "GUARD_EXEC_TEST_FIXTURE_C_SHA256": (
            source_dir / "guard_exec_test_fixture.c"
        ),
        "PARENT_GUARD_C_SHA256": source_dir / "parent_guard.c",
        "PARENT_GUARD_H_SHA256": source_dir / "parent_guard.h",
        "PARENT_GUARD_TEST_HELPER_C_SHA256": (
            source_dir / "parent_guard_test_helper.c"
        ),
        "SEALED_EXEC_C_SHA256": source_dir / "sealed_exec.c",
        "SEALED_EXEC_H_SHA256": source_dir / "sealed_exec.h",
        "SHA256_C_SHA256": source_dir / "sha256.c",
        "SHA256_H_SHA256": source_dir / "sha256.h",
        "BUILD_ATTESTATION_PY_SHA256": source_dir / "build_attestation.py",
        "GIT_PROVENANCE_PY_SHA256": (
            repo_root / "src" / "burstserve" / "git_provenance.py"
        ),
        "TEST_NATIVE_PARENT_GUARD_PY_SHA256": (
            repo_root / "tests" / "test_native_parent_guard.py"
        ),
        "LIBSMCTRL_C_SHA256": libsmctrl_dir / "libsmctrl.c",
        "LIBSMCTRL_H_SHA256": libsmctrl_dir / "libsmctrl.h",
    }
    source_snapshots: dict[str, RegularFileSnapshot] = {}
    for field, path in source_paths.items():
        source_snapshots[field] = snapshot_regular_file(
            path,
            description=field,
            max_bytes=MAX_SOURCE_FILE_BYTES,
            include_content=(field == "GIT_PROVENANCE_PY_SHA256"),
        )
    source_hashes = {
        field: snapshot.sha256
        for field, snapshot in source_snapshots.items()
    }

    libsmctrl_snapshot = capture_libsmctrl_git_snapshot(
        repo_root,
        libsmctrl_dir,
        module_snapshot=source_snapshots["GIT_PROVENANCE_PY_SHA256"],
    )
    commit = libsmctrl_snapshot.head_oid
    libsmctrl_clean = libsmctrl_snapshot.clean
    libsmctrl_status_sha256 = (
        sha256_bytes(b"")
        if libsmctrl_clean
        else libsmctrl_snapshot.identity_sha256
    )

    include_root = (cuda_home / "include").resolve(strict=True)
    libdevice_root = (cuda_home / "nvvm" / "libdevice").resolve(strict=True)
    runtime_library = required_file(
        cuda_home / "lib64" / "libcudart.so",
        "CUDA runtime link library",
    )
    version_file = cuda_version_file(cuda_home)
    nvcc = resolve_tool(configuration["nvcc"], "nvcc")
    cc = resolve_tool(configuration["cc"], "host C compiler")
    ar = resolve_tool(configuration["ar"], "archiver")
    python = required_file(Path(sys.executable), "Python interpreter")
    cuda_driver_link = libcuda_link_library(cc, configuration)

    nvcc_version = version_record(nvcc, configuration)
    cc_version = version_record(cc, configuration)
    ar_version = version_record(ar, configuration)
    python_version = version_record(
        python,
        configuration,
        invocation_prefix=PYTHON_ISOLATION_FLAGS,
    )
    cuda_subtools = cuda_subtool_records(cuda_home, configuration)
    host_components = host_toolchain_component_records(cc, configuration)
    cc_fingerprints = compiler_fingerprints(cc, configuration)
    nvcc_dryrun = nvcc_dryrun_fingerprint(nvcc, configuration)
    environment_contract = build_environment_contract(configuration)

    fields = {
        "CUDA_HOME": str(cuda_home),
        "CUDA_ARCH": configuration["cuda_arch"],
        "NVCC": configuration["nvcc"],
        "CC": configuration["cc"],
        "AR": configuration["ar"],
        "CPPFLAGS": configuration["cppflags"],
        "CFLAGS": configuration["cflags"],
        "NVCCFLAGS": configuration["nvccflags"],
        "LDLIBS": configuration["ldlibs"],
        "LAUNCHER_CFLAGS": configuration["launcher_cflags"],
        "LIBSMCTRL_DIR": str(libsmctrl_dir),
        "BUILD_TMPDIR": str(
            Path(configuration["build_tmpdir"]).resolve(strict=True)
        ),
        "BUILD_LOCK_PATH": str(_bounded_absolute_path(build_lock_path)),
        "HERMETIC_PATH": configuration["hermetic_path"],
        "BUILD_ENVIRONMENT_SHA256": sha256_bytes(
            canonical_json(environment_contract)
        ),
        **source_hashes,
        "LIBSMCTRL_GIT_COMMIT": commit,
        "LIBSMCTRL_GIT_DIRTY": "clean" if libsmctrl_clean else "dirty",
        "LIBSMCTRL_GIT_STATUS_SHA256": libsmctrl_status_sha256,
        "LIBSMCTRL_GIT_SNAPSHOT_SHA256": (
            libsmctrl_snapshot.identity_sha256
        ),
        "CUDA_INCLUDE_ROOT": str(include_root),
        "CUDA_INCLUDE_TREE_SHA256": tree_digest(include_root),
        "CUDA_LIBDEVICE_ROOT": str(libdevice_root),
        "CUDA_LIBDEVICE_TREE_SHA256": tree_digest(libdevice_root),
        "CUDA_RUNTIME_LIBRARY": str(runtime_library),
        "CUDA_RUNTIME_LIBRARY_SHA256": sha256_file(runtime_library),
        "CUDA_VERSION_FILE": str(version_file),
        "CUDA_VERSION_FILE_SHA256": sha256_file(version_file),
        "LIBCUDA_LINK_LIBRARY": str(cuda_driver_link),
        "LIBCUDA_LINK_LIBRARY_SHA256": sha256_file(cuda_driver_link),
        "NVCC_EXECUTABLE": str(nvcc),
        "NVCC_EXECUTABLE_SHA256": sha256_file(nvcc),
        "NVCC_VERSION_SHA256": nvcc_version["stdout_stderr_sha256"],
        "NVCC_DRYRUN_SHA256": nvcc_dryrun,
        "CUDA_SUBTOOLS_SHA256": sha256_bytes(canonical_json(cuda_subtools)),
        "CC_EXECUTABLE": str(cc),
        "CC_EXECUTABLE_SHA256": sha256_file(cc),
        "CC_VERSION_SHA256": cc_version["stdout_stderr_sha256"],
        "CC_SEARCH_PATHS_SHA256": cc_fingerprints["search_paths_sha256"],
        "CC_INCLUDE_SEARCH_SHA256": (
            cc_fingerprints["include_search_sha256"]
        ),
        "CC_DRYRUN_SHA256": cc_fingerprints["dryrun_sha256"],
        "CC_SPECS_SHA256": cc_fingerprints["specs_sha256"],
        "HOST_TOOLCHAIN_COMPONENTS_SHA256": sha256_bytes(
            canonical_json(host_components)
        ),
        "AR_EXECUTABLE": str(ar),
        "AR_EXECUTABLE_SHA256": sha256_file(ar),
        "AR_VERSION_SHA256": ar_version["stdout_stderr_sha256"],
        "PYTHON_EXECUTABLE": str(python),
        "PYTHON_EXECUTABLE_SHA256": sha256_file(python),
        "PYTHON_VERSION_SHA256": python_version["stdout_stderr_sha256"],
    }
    missing = set(STAMP_FIELD_ORDER) - set(fields)
    extra = set(fields) - set(STAMP_FIELD_ORDER)
    if missing or extra:
        raise AttestationError(
            f"internal stamp field mismatch: missing={missing}, extra={extra}"
        )
    detail = {
        "build_environment": environment_contract,
        "cuda_subtools": cuda_subtools,
        "host_toolchain_components": host_components,
        "host_compiler_fingerprints": cc_fingerprints,
        "nvcc_dryrun_sha256": nvcc_dryrun,
        "versions": {
            "nvcc": nvcc_version,
            "cc": cc_version,
            "ar": ar_version,
            "python": python_version,
        },
        "libsmctrl_git_snapshot": libsmctrl_snapshot.to_dict(),
    }
    return fields, detail


def serialize_stamp(fields: Mapping[str, str]) -> bytes:
    lines: list[str] = []
    for field in STAMP_FIELD_ORDER:
        value = fields[field]
        if "\n" in value or "\r" in value:
            raise AttestationError(f"stamp value contains a newline: {field}")
        lines.append(f"{field}={value}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def parse_stamp(content: bytes) -> dict[str, str]:
    if len(content) > MAX_STAMP_BYTES:
        raise AttestationError("build stamp exceeds byte limit")
    if not content.endswith(b"\n") or b"\r" in content or b"\0" in content:
        raise AttestationError("build stamp bytes are not canonical")
    try:
        decoded = content.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise AttestationError("build stamp is not strict UTF-8") from exc
    fields: dict[str, str] = {}
    for line in decoded[:-1].split("\n"):
        key, separator, value = line.partition("=")
        if not separator or not key:
            raise AttestationError(f"malformed build stamp line: {line!r}")
        if key in fields:
            raise AttestationError(f"duplicate build stamp field: {key}")
        fields[key] = value
    if tuple(fields) != STAMP_FIELD_ORDER:
        raise AttestationError("build stamp fields/order do not match schema")
    if serialize_stamp(fields) != content:
        raise AttestationError("build stamp bytes are not canonical")
    return fields


def atomic_write(
    path: Path,
    content: bytes,
    *,
    mode: int = 0o400,
    max_bytes: int = MAX_ATTESTATION_BYTES,
) -> None:
    if len(content) > max_bytes:
        raise AttestationError(
            f"generated content exceeds {max_bytes} byte limit: {path}"
        )
    absolute = _bounded_absolute_path(path)
    if absolute.name in ("", ".", ".."):
        raise AttestationError(f"invalid generated-file path: {absolute}")
    temporary_name = f".{absolute.name}.tmp.{os.getpid()}"
    with _open_directory_nofollow(absolute.parent) as (
        directory_descriptor,
        parent,
    ):
        parent_status = os.fstat(directory_descriptor)
        if (
            not stat.S_ISDIR(parent_status.st_mode)
            or parent_status.st_uid != os.geteuid()
            or stat.S_IMODE(parent_status.st_mode) != 0o700
            or parent_status.st_mode & (stat.S_ISUID | stat.S_ISGID)
        ):
            raise AttestationError(
                "generated-file parent must be euid-owned mode 0700: "
                f"{parent}"
            )
        descriptor = -1
        try:
            descriptor = os.open(
                temporary_name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_NOFOLLOW
                | os.O_NONBLOCK
                | os.O_CLOEXEC,
                0o600,
                dir_fd=directory_descriptor,
            )
            view = memoryview(content)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise AttestationError(
                        f"short write for {parent / temporary_name}"
                    )
                view = view[written:]
            os.fchmod(descriptor, mode)
            os.fsync(descriptor)
            generated_status = os.fstat(descriptor)
            if (
                not stat.S_ISREG(generated_status.st_mode)
                or generated_status.st_uid != os.geteuid()
                or generated_status.st_nlink != 1
                or generated_status.st_size != len(content)
                or stat.S_IMODE(generated_status.st_mode) != mode
            ):
                raise AttestationError(
                    f"generated-file metadata is invalid: "
                    f"{parent / temporary_name}"
                )
            os.close(descriptor)
            descriptor = -1
            os.replace(
                temporary_name,
                absolute.name,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
            )
            os.fsync(directory_descriptor)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary_name, dir_fd=directory_descriptor)
            except FileNotFoundError:
                pass


def _xattr_record_dicts(
    xattrs: Sequence[tuple[str, bytes]],
) -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "size_bytes": len(value),
            "sha256": sha256_bytes(value),
        }
        for name, value in xattrs
    ]


def _metadata_record(
    status: os.stat_result,
    xattrs: Sequence[tuple[str, bytes]],
    path: Path,
    *,
    expected_mode: int,
    kind: str,
) -> dict[str, Any]:
    expected_type = stat.S_ISDIR if kind == "directory" else stat.S_ISREG
    if not expected_type(status.st_mode):
        raise AttestationError(f"unexpected file type for {path}")
    if status.st_uid != os.geteuid():
        raise AttestationError(f"artifact is not owned by euid: {path}")
    if kind != "directory" and status.st_nlink != 1:
        raise AttestationError(f"artifact link count must be one: {path}")
    actual_mode = stat.S_IMODE(status.st_mode)
    if actual_mode != expected_mode or status.st_mode & (
        stat.S_ISUID | stat.S_ISGID
    ):
        raise AttestationError(
            f"artifact mode mismatch for {path}: {actual_mode:04o}"
        )
    return {
        "file_type": "directory" if kind == "directory" else "regular",
        "uid": status.st_uid,
        "gid": status.st_gid,
        "mode_octal": f"{actual_mode:04o}",
        "nlink": status.st_nlink,
        "device": status.st_dev,
        "inode": status.st_ino,
        "xattrs": _xattr_record_dicts(xattrs),
    }


def strict_metadata(
    path: Path,
    *,
    expected_mode: int,
    kind: str = "file",
) -> dict[str, Any]:
    if kind == "directory":
        with _open_directory_nofollow(path) as (descriptor, absolute):
            before = os.fstat(descriptor)
            xattrs = _bounded_xattrs_from_fd(descriptor, absolute)
            after = os.fstat(descriptor)
            if _stable_status_fields(before) != _stable_status_fields(after):
                raise AttestationError(
                    f"directory metadata changed while reading: {absolute}"
                )
            return _metadata_record(
                after,
                xattrs,
                absolute,
                expected_mode=expected_mode,
                kind=kind,
            )
    if kind != "file":
        raise AttestationError(f"unsupported metadata kind: {kind}")
    with _open_regular_nofollow(path) as (descriptor, absolute):
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise AttestationError(f"unexpected file type for {absolute}")
        xattrs = _bounded_xattrs_from_fd(descriptor, absolute)
        after = os.fstat(descriptor)
        if _stable_status_fields(before) != _stable_status_fields(after):
            raise AttestationError(
                f"file metadata changed while reading: {absolute}"
            )
        return _metadata_record(
            after,
            xattrs,
            absolute,
            expected_mode=expected_mode,
            kind=kind,
        )


def elf_program_types(content: bytes, description: str) -> set[int]:
    if len(content) < 64 or content[:4] != b"\x7fELF":
        raise AttestationError(f"not an ELF binary: {description}")
    if content[4] != 2 or content[5] != 1:
        raise AttestationError(
            f"expected ELF64 little-endian binary: {description}"
        )
    unpacked = struct.unpack("<16sHHIQQQIHHHHHH", content[:64])
    program_offset = unpacked[5]
    program_entry_size = unpacked[9]
    program_count = unpacked[10]
    if not 56 <= program_entry_size <= 4096:
        raise AttestationError(
            f"invalid ELF program header size: {description}"
        )
    if not 1 <= program_count <= 4096:
        raise AttestationError(f"invalid ELF program header count: {description}")
    program_bytes = program_entry_size * program_count
    if (
        program_offset < 64
        or program_offset > len(content)
        or program_bytes > len(content) - program_offset
    ):
        raise AttestationError(f"truncated ELF headers: {description}")
    types: set[int] = set()
    for index in range(program_count):
        offset = program_offset + index * program_entry_size
        types.add(struct.unpack_from("<I", content, offset)[0])
    return types


def is_static_elf(snapshot: RegularFileSnapshot) -> bool:
    if snapshot.content is None:
        raise AttestationError(f"ELF bytes are unavailable: {snapshot.path}")
    program_types = elf_program_types(snapshot.content, str(snapshot.path))
    return 2 not in program_types and 3 not in program_types


def runtime_dependencies(
    path: Path,
    configuration: Mapping[str, str],
    *,
    expected_sha256: str,
    expected_status: os.stat_result,
) -> list[dict[str, str]]:
    with _open_regular_nofollow(path) as (descriptor, absolute):
        before = _snapshot_open_regular(
            descriptor,
            absolute,
            max_bytes=MAX_OUTPUT_FILE_BYTES,
        )
        if (
            before.sha256 != expected_sha256
            or _stable_status_fields(before.status)
            != _stable_status_fields(expected_status)
        ):
            raise AttestationError(
                f"runtime-dependency input changed before ldd: {absolute}"
            )
        _, stdout, _ = run_command(
            [str(LDD), f"/proc/self/fd/{descriptor}"],
            configuration,
            pass_fds=(descriptor,),
        )
        after = _snapshot_open_regular(
            descriptor,
            absolute,
            max_bytes=MAX_OUTPUT_FILE_BYTES,
        )
        if (
            after.sha256 != expected_sha256
            or _stable_status_fields(before.status)
            != _stable_status_fields(after.status)
        ):
            raise AttestationError(
                f"runtime-dependency input changed during ldd: {absolute}"
            )
    dependencies: dict[str, Path] = {}
    for raw_line in stdout.decode("utf-8", errors="strict").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("linux-vdso"):
            continue
        if "not found" in line:
            raise AttestationError(f"unresolved runtime dependency: {line}")
        if "=>" in line:
            name, remainder = line.split("=>", 1)
            path_text = remainder.strip().split(maxsplit=1)[0]
            if path_text.startswith("/"):
                dependencies[name.strip()] = Path(path_text).resolve(strict=True)
        else:
            path_text = line.split(maxsplit=1)[0]
            if path_text.startswith("/"):
                resolved = Path(path_text).resolve(strict=True)
                dependencies[resolved.name] = resolved
        if len(dependencies) > MAX_RUNTIME_DEPENDENCIES:
            raise AttestationError("ldd reported too many runtime dependencies")
    return [
        {
            "name": name,
            "path": str(dependency),
            "sha256": sha256_file(
                dependency,
                description=f"runtime dependency {name}",
            ),
        }
        for name, dependency in sorted(dependencies.items())
    ]


IDENTITY_HEADER_PATTERN = re.compile(
    rb"#ifndef BURSTSERVE_REAL_PROBE_IDENTITY_H_\n"
    rb"#define BURSTSERVE_REAL_PROBE_IDENTITY_H_\n"
    rb'#define BURSTSERVE_REAL_PROBE_SHA256 "([0-9a-f]{64})"\n'
    rb"#define BURSTSERVE_REAL_PROBE_SIZE (0|[1-9][0-9]{0,19})ULL\n"
    rb"#endif\n"
)


def parse_identity_header(content: bytes) -> tuple[str, int]:
    if len(content) > MAX_IDENTITY_HEADER_BYTES:
        raise AttestationError("real-probe identity header exceeds byte limit")
    match = IDENTITY_HEADER_PATTERN.fullmatch(content)
    if match is None:
        raise AttestationError("real-probe identity header is malformed")
    return match.group(1).decode("ascii"), int(match.group(2), 10)


def output_paths(configuration: Mapping[str, str]) -> dict[str, tuple[Path, int]]:
    build_dir = _bounded_absolute_path(Path(configuration["build_dir"]))
    return {
        "launcher": (build_dir / "smid_probe", 0o500),
        "real_probe": (build_dir / "smid_probe.real", 0o500),
        "parent_guard_test_helper": (
            build_dir / "parent_guard_test_helper",
            0o500,
        ),
        "real_probe_identity_header": (
            build_dir / "real_probe_identity.h",
            0o400,
        ),
        "guard_exec_test_launcher": (
            build_dir / "guard_exec_test_launcher",
            0o500,
        ),
        "guard_exec_test_fixture": (
            build_dir / "guard_exec_test_launcher.real",
            0o500,
        ),
        "guard_exec_test_identity_header": (
            build_dir / "guard_exec_test_identity.h",
            0o400,
        ),
    }


def reject_dirty_libsmctrl(fields: Mapping[str, str]) -> None:
    if fields.get("LIBSMCTRL_GIT_DIRTY") != "clean":
        raise AttestationError(
            "dirty libsmctrl is forbidden for finalize and verify"
        )


def build_lock_record(configuration: Mapping[str, str]) -> dict[str, Any]:
    lock_path = Path(configuration["build_lock_path"])
    lock_metadata = strict_metadata(lock_path, expected_mode=0o600)
    directory_metadata = strict_metadata(
        lock_path.parent,
        expected_mode=0o700,
        kind="directory",
    )
    return {
        "path": str(_bounded_absolute_path(lock_path)),
        "uid": lock_metadata["uid"],
        "gid": lock_metadata["gid"],
        "mode_octal": lock_metadata["mode_octal"],
        "nlink": lock_metadata["nlink"],
        "xattrs": lock_metadata["xattrs"],
        "directory_path": str(_bounded_absolute_path(lock_path.parent)),
        "directory_uid": directory_metadata["uid"],
        "directory_gid": directory_metadata["gid"],
        "directory_mode_octal": directory_metadata["mode_octal"],
        # The lock directory's link count is ambient runtime state, not build
        # identity: any subdirectory the runner legitimately creates beside
        # the lock changes it, which would make the first formal run
        # permanently invalidate its own attestation.  The lock file's own
        # nlink is still pinned to 1, which is the hardlink defence that
        # matters, and the directory stays euid-owned mode 0700.
        "directory_xattrs": directory_metadata["xattrs"],
        "inherited_lock_fd": 9,
    }


def compute_attestation(
    configuration: Mapping[str, str],
    stamp_path: Path,
) -> dict[str, object]:
    expected_fields, identity_detail = collect_identity(configuration)
    reject_dirty_libsmctrl(expected_fields)
    expected_stamp = serialize_stamp(expected_fields)
    stamp_snapshot = snapshot_regular_file(
        stamp_path,
        description="build stamp",
        max_bytes=MAX_STAMP_BYTES,
        include_content=True,
        include_xattrs=True,
    )
    if stamp_snapshot.content is None:
        raise AttestationError("build stamp bytes are unavailable")
    actual_stamp = stamp_snapshot.content
    stamp_metadata = _metadata_record(
        stamp_snapshot.status,
        stamp_snapshot.xattrs,
        stamp_snapshot.path,
        expected_mode=0o400,
        kind="file",
    )
    if actual_stamp != expected_stamp:
        actual_fields = parse_stamp(actual_stamp)
        changed = [
            key
            for key in STAMP_FIELD_ORDER
            if actual_fields[key] != expected_fields[key]
        ]
        raise AttestationError(
            "source/toolchain identity changed between stamp and post-link "
            f"check: {changed}"
        )
    parse_stamp(actual_stamp)

    paths = output_paths(configuration)
    output_records: dict[str, dict[str, Any]] = {}
    output_snapshots: dict[str, RegularFileSnapshot] = {}
    output_aggregate_bytes = 0
    for name, (path, expected_mode) in paths.items():
        file_limit = (
            MAX_IDENTITY_HEADER_BYTES
            if name.endswith("_identity_header")
            else MAX_OUTPUT_FILE_BYTES
        )
        snapshot = snapshot_regular_file(
            path,
            description=f"native output {name}",
            max_bytes=file_limit,
            include_content=True,
            include_xattrs=True,
        )
        output_aggregate_bytes += snapshot.status.st_size
        if output_aggregate_bytes > MAX_OUTPUT_AGGREGATE_BYTES:
            raise AttestationError(
                "native outputs exceed aggregate byte limit"
            )
        metadata = _metadata_record(
            snapshot.status,
            snapshot.xattrs,
            snapshot.path,
            expected_mode=expected_mode,
            kind="file",
        )
        output_snapshots[name] = snapshot
        output_records[name] = {
            "path": str(snapshot.path),
            "sha256": snapshot.sha256,
            "size_bytes": snapshot.status.st_size,
            "metadata": metadata,
            "xattrs": metadata["xattrs"],
        }

    launcher_static = is_static_elf(output_snapshots["launcher"])
    helper_static = is_static_elf(
        output_snapshots["parent_guard_test_helper"]
    )
    test_launcher_static = is_static_elf(
        output_snapshots["guard_exec_test_launcher"]
    )
    if not launcher_static or not helper_static or not test_launcher_static:
        raise AttestationError("pre-exec launchers and CPU helper must be static")

    real_header = output_snapshots["real_probe_identity_header"].content
    fixture_header = output_snapshots[
        "guard_exec_test_identity_header"
    ].content
    if real_header is None or fixture_header is None:
        raise AttestationError("identity header bytes are unavailable")
    real_identity = parse_identity_header(real_header)
    fixture_identity = parse_identity_header(fixture_header)
    real_output = output_records["real_probe"]
    fixture_output = output_records["guard_exec_test_fixture"]
    if real_identity != (
        real_output["sha256"],
        real_output["size_bytes"],
    ):
        raise AttestationError("launcher identity does not match real probe")
    if fixture_identity != (
        fixture_output["sha256"],
        fixture_output["size_bytes"],
    ):
        raise AttestationError("test launcher identity does not match fixture")

    source_fields = {
        key: value
        for key, value in expected_fields.items()
        if key.endswith("_SHA256")
        and (
            key.startswith("MAKEFILE")
            or key.startswith("README")
            or key.startswith("PROBE_")
            or key.startswith("GUARD_")
            or key.startswith("PARENT_")
            or key.startswith("SEALED_")
            or key.startswith("SHA256_")
            or key.startswith("BUILD_ATTESTATION")
            or key.startswith("GIT_PROVENANCE")
            or key.startswith("TEST_NATIVE_PARENT_GUARD")
            or key.startswith("LIBSMCTRL_")
        )
    }
    forbidden_runtime_environment = [
        "LD_*",
        "GLIBC_TUNABLES",
        "GCONV_PATH",
        "LOCPATH",
        "NLSPATH",
        "CUDA_INJECTION*",
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "build_lock": build_lock_record(configuration),
        "build_stamp": {
            "path": str(stamp_snapshot.path),
            "sha256": stamp_snapshot.sha256,
            "fields": expected_fields,
            "metadata": stamp_metadata,
        },
        "build_environment": identity_detail["build_environment"],
        "toolchain": {
            "cuda_subtools": identity_detail["cuda_subtools"],
            "host_components": identity_detail[
                "host_toolchain_components"
            ],
            "host_compiler_fingerprints": identity_detail[
                "host_compiler_fingerprints"
            ],
            "nvcc_dryrun_sha256": identity_detail[
                "nvcc_dryrun_sha256"
            ],
            "versions": identity_detail["versions"],
        },
        "finite_build_contract": {
            "bound": [
                "listed native and libsmctrl source bytes",
                "complete CUDA include and libdevice trees",
                "listed compiler/subtool/CRT bytes and fingerprints",
                "link-time libcudart/libcuda and post-link ldd dependencies",
                "explicit flags and env-i allowlist",
                "post-link ELF bytes, size, ownership, mode, link count, xattrs",
            ],
            "not_claimed": [
                "unlisted system headers selected by compiler search paths",
                "compiler or CUDA subtool dlopen dependencies",
                "build-orchestration utility bytes such as make, env, git, "
                "flock, install, chmod, mv, and ldd",
                "individual libc/libgcc static archive inputs already folded "
                "into the final statically linked output hash",
                "kernel, firmware, or live NVIDIA driver state after Gate",
                "protection from same-euid mutation outside the advisory lock",
                "GNU make --eval/-E text, which GNU make expands before the "
                "parse guard can run and which the guard can therefore only "
                "reject after the fact",
                "A/B stability of the make ancestor's /proc records, which are "
                "read once under bounded, strictly parsed single snapshots",
            ],
            "libsmctrl_git_snapshot": identity_detail[
                "libsmctrl_git_snapshot"
            ],
        },
        "source_hashes": source_fields,
        "outputs": output_records,
        "launcher_contract": {
            "static_elf": launcher_static,
            "identity": {
                "sha256": real_identity[0],
                "size_bytes": real_identity[1],
            },
            "source_open_flags": [
                "O_RDONLY",
                "O_CLOEXEC",
                "O_NOFOLLOW",
                "O_NONBLOCK",
            ],
            "snapshot": {
                "mechanism": "memfd_create",
                "flags": [
                    "MFD_CLOEXEC",
                    "MFD_ALLOW_SEALING",
                    "MFD_EXEC",
                ],
                "copy_limit": "expected_size_plus_one",
                "mode_octal": "0500",
                "post_seal_fstat_hash_and_elf_validation": True,
                "legacy_retry_without_mfd_exec": False,
            },
            "seals": {
                "required": [
                    "F_SEAL_WRITE",
                    "F_SEAL_GROW",
                    "F_SEAL_SHRINK",
                    "F_SEAL_SEAL",
                ],
                "execution_bit_seal_when_supported": "F_SEAL_EXEC",
                "verified_with": "F_GET_SEALS",
            },
            "exec": {
                "mechanism": "syscall(SYS_execveat)",
                "flags": ["AT_EMPTY_PATH"],
                "pathname_fallback": False,
            },
            "parent_guard": (
                "PR_SET_PDEATHSIG=SIGKILL+immediate_getppid+"
                "post_exec_rearm"
            ),
            "lifecycle_signals": {
                "reset_to_default": ["SIGHUP", "SIGINT", "SIGTERM"],
                "unblocked": ["SIGHUP", "SIGINT", "SIGTERM"],
            },
            "environment": {
                "removed": forbidden_runtime_environment,
                "preserved_classes": [
                    "CUDA_VISIBLE_DEVICES",
                    "CUDA_MPS_PIPE_DIRECTORY",
                    "MASK_OFF",
                    "BURSTSERVE_PARENT_PID",
                ],
            },
            "inherited_file_descriptors": {
                "preserved": [0, 1, 2],
                "sealed_exec_fd": 3,
                "close_range_from": 4,
                "exec_fd_cloexec": True,
            },
            "production_fault_injection": False,
        },
        "test_fixture_contract": {
            "launcher_static_elf": test_launcher_static,
            "fixture_cuda_linked": False,
            "fault_injection_compiled_only_into_test_launcher": True,
            "identity": {
                "sha256": fixture_identity[0],
                "size_bytes": fixture_identity[1],
            },
        },
        "real_probe_runtime_dependencies": runtime_dependencies(
            paths["real_probe"][0],
            configuration,
            expected_sha256=output_records["real_probe"]["sha256"],
            expected_status=output_snapshots["real_probe"].status,
        ),
        "guard_fixture_runtime_dependencies": runtime_dependencies(
            paths["guard_exec_test_fixture"][0],
            configuration,
            expected_sha256=output_records[
                "guard_exec_test_fixture"
            ]["sha256"],
            expected_status=output_snapshots[
                "guard_exec_test_fixture"
            ].status,
        ),
        "checks": {
            "post_link_stamp_matches_current_inputs": True,
            "libsmctrl_worktree_clean": True,
            "launcher_is_static": launcher_static,
            "test_launcher_is_static": test_launcher_static,
            "test_helper_is_static": helper_static,
            "launcher_identity_matches_real_probe": True,
            "test_launcher_identity_matches_fixture": True,
            "output_metadata_and_xattrs_accepted": True,
            "build_lock_metadata_accepted": True,
        },
    }


def identity_header_content(snapshot: RegularFileSnapshot) -> bytes:
    return (
        "#ifndef BURSTSERVE_REAL_PROBE_IDENTITY_H_\n"
        "#define BURSTSERVE_REAL_PROBE_IDENTITY_H_\n"
        f'#define BURSTSERVE_REAL_PROBE_SHA256 "{snapshot.sha256}"\n'
        f"#define BURSTSERVE_REAL_PROBE_SIZE {snapshot.status.st_size}ULL\n"
        "#endif\n"
    ).encode("ascii")


def command_stamp(arguments: argparse.Namespace) -> None:
    fields, _ = collect_identity(load_configuration())
    atomic_write(
        arguments.output,
        serialize_stamp(fields),
        max_bytes=MAX_STAMP_BYTES,
    )


def command_identity_header(arguments: argparse.Namespace) -> None:
    snapshot = snapshot_regular_file(
        arguments.input,
        description="identity-header input",
        max_bytes=MAX_OUTPUT_FILE_BYTES,
        include_xattrs=True,
    )
    _metadata_record(
        snapshot.status,
        snapshot.xattrs,
        snapshot.path,
        expected_mode=0o500,
        kind="file",
    )
    atomic_write(
        arguments.output,
        identity_header_content(snapshot),
        max_bytes=MAX_IDENTITY_HEADER_BYTES,
    )


def command_finalize(arguments: argparse.Namespace) -> None:
    attestation = compute_attestation(
        load_configuration(),
        arguments.stamp,
    )
    atomic_write(
        arguments.output,
        canonical_json(attestation, max_bytes=MAX_ATTESTATION_BYTES),
        max_bytes=MAX_ATTESTATION_BYTES,
    )


def command_verify(arguments: argparse.Namespace) -> None:
    expected = compute_attestation(
        load_configuration(),
        arguments.stamp,
    )
    attestation_snapshot = snapshot_regular_file(
        arguments.attestation,
        description="build attestation",
        max_bytes=MAX_ATTESTATION_BYTES,
        include_content=True,
        include_xattrs=True,
    )
    _metadata_record(
        attestation_snapshot.status,
        attestation_snapshot.xattrs,
        attestation_snapshot.path,
        expected_mode=0o400,
        kind="file",
    )
    if attestation_snapshot.content is None:
        raise AttestationError("build attestation bytes are unavailable")
    actual = parse_canonical_json(attestation_snapshot.content)
    expected_content = canonical_json(expected, max_bytes=MAX_ATTESTATION_BYTES)
    # Canonical-byte equality runs first because it is what a stale or
    # tampered attestation actually trips, and its message says so.  The
    # exact-type comparison then runs as defence in depth: it rejects any
    # value or type difference that byte equality could ever miss, and it
    # never accepts a document byte equality rejected.
    if attestation_snapshot.content != expected_content:
        raise AttestationError("build attestation is stale or does not match outputs")
    if not exact_json_equal(actual, expected):
        raise AttestationError(
            "build attestation does not match outputs by exact JSON type"
        )


def command_make_parse_guard(arguments: argparse.Namespace) -> None:
    validate_formal_make_invocation(
        arguments.source_dir,
        arguments.makefile,
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    subparsers = result.add_subparsers(dest="command", required=True)

    stamp = subparsers.add_parser("stamp")
    stamp.add_argument("--output", type=Path, required=True)
    stamp.set_defaults(handler=command_stamp)

    identity = subparsers.add_parser("identity-header")
    identity.add_argument("--input", type=Path, required=True)
    identity.add_argument("--output", type=Path, required=True)
    identity.set_defaults(handler=command_identity_header)

    finalize = subparsers.add_parser("finalize")
    finalize.add_argument("--stamp", type=Path, required=True)
    finalize.add_argument("--output", type=Path, required=True)
    finalize.set_defaults(handler=command_finalize)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--stamp", type=Path, required=True)
    verify.add_argument("--attestation", type=Path, required=True)
    verify.set_defaults(handler=command_verify)

    make_guard = subparsers.add_parser("make-parse-guard")
    make_guard.add_argument("--source-dir", type=Path, required=True)
    make_guard.add_argument("--makefile", type=Path, required=True)
    make_guard.set_defaults(handler=command_make_parse_guard)
    return result


def main() -> int:
    arguments = parser().parse_args()
    try:
        arguments.handler(arguments)
    except (AttestationError, OSError, UnicodeError, ValueError) as exc:
        print(f"native build attestation: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
