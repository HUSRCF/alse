"""Fail-closed Git provenance without repository-controlled Git behavior.

The repository is treated as untrusted data.  Git is only allowed to parse a
private synthetic Git directory, a copied index, and the original object
database.  It is never given the original working tree, local configuration,
or repository attributes.  Working-tree equality is computed from raw bytes
in Python, so clean/smudge filters, text conversion, fsmonitor state, and
assume-unchanged bits cannot make a changed file appear clean.
Required HEAD/index commits, trees, and blobs are streamed through a bounded
temporary sink and rehashed, so a valid object payload stored under the wrong
object name cannot be accepted from Git's type/size metadata alone.

This module is intentionally standard-library-only.  It is suitable both for
import by the runtime and for execution from an isolated Python interpreter by
the native build attestation.

The A/B metadata and filesystem scans detect ordinary concurrent mutation.
They cannot exclude a same-privilege adversary performing an exact ABA
replacement between observations.  A formal launch must therefore also hold
the source on a read-only snapshot, or copy/seal it in a private namespace.
The trusted computing base includes the kernel/VFS, this scanner and Python,
and the selected Git binary plus its object/index parsers; repository config,
attributes, hooks, filters, pagers, and helper programs are outside it.

An allowed untracked root is an intentional blind spot: arbitrary files,
symlinks, mounts, and code may exist below an allowed directory without being
enumerated.  Formal callers must never allow import/loader/plugin paths,
compiler or build inputs, or paths containing artifacts that will execute.
Regular-file exclusions are rejected by default.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, field
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import signal
import stat
import subprocess
import tempfile
import time
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_VERSION = "burstserve.git-provenance/v1"
DEFAULT_GIT = Path("/usr/bin/git")
BURSTSERVE_FORMAL_PROTECTED_ROOTS = (
    "build",
    "environments",
    "experiments/manifests",
    "native",
    "pyproject.toml",
    "scripts",
    "src",
    "tests",
    "toy_bench",
    "vendor/libsmctrl",
)

_ALLOWED_OBJECT_FORMATS = frozenset({"sha1", "sha256"})
_OID_LENGTH = {"sha1": 40, "sha256": 64}
_TRACKED_MODES = frozenset({"100644", "100755", "120000", "160000"})
_SHARED_INDEX_RE = re.compile(r"^sharedindex\.([0-9a-f]+)$")


class GitProvenanceError(RuntimeError):
    """Raised internally when a repository state cannot be proven complete."""


class UnsupportedRepository(GitProvenanceError):
    """Raised for a repository layout deliberately outside the finite contract."""


class UnstableRepository(GitProvenanceError):
    """Raised when the repository changes while it is being inspected."""


@dataclass(frozen=True, slots=True)
class SnapshotLimits:
    """Finite resource contract for one repository snapshot."""

    max_config_bytes: int = 2 * 1024 * 1024
    max_index_bytes: int = 256 * 1024 * 1024
    max_metadata_bytes: int = 64 * 1024 * 1024
    max_git_output_bytes: int = 256 * 1024 * 1024
    max_git_stderr_bytes: int = 1024 * 1024
    max_entries: int = 1_000_000
    max_object_checks: int = 1_000_000
    max_single_file_bytes: int = 16 * 1024 * 1024 * 1024
    max_total_file_bytes: int = 64 * 1024 * 1024 * 1024
    max_shared_indexes: int = 64
    max_path_bytes: int = 4096
    max_path_component_bytes: int = 255
    max_path_components: int = 256
    max_aggregate_path_bytes: int = 256 * 1024 * 1024
    max_derived_prefixes: int = 1_000_000
    max_derived_prefix_bytes: int = 256 * 1024 * 1024
    max_policy_entries: int = 4096
    max_policy_path_bytes: int = 4 * 1024 * 1024
    git_timeout_seconds: float = 30.0
    max_attempts: int = 2

    def __post_init__(self) -> None:
        for name in (
            "max_config_bytes",
            "max_index_bytes",
            "max_metadata_bytes",
            "max_git_output_bytes",
            "max_git_stderr_bytes",
            "max_entries",
            "max_object_checks",
            "max_single_file_bytes",
            "max_total_file_bytes",
            "max_shared_indexes",
            "max_path_bytes",
            "max_path_component_bytes",
            "max_path_components",
            "max_aggregate_path_bytes",
            "max_derived_prefixes",
            "max_derived_prefix_bytes",
            "max_policy_entries",
            "max_policy_path_bytes",
            "max_attempts",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.git_timeout_seconds <= 0:
            raise ValueError("git_timeout_seconds must be positive")


def _path_fields(path: bytes) -> dict[str, str]:
    return {
        "path": os.fsdecode(path),
        "path_bytes_b64": base64.b64encode(path).decode("ascii"),
    }


def _target_fields(target: bytes | None) -> dict[str, str] | None:
    if target is None:
        return None
    return {
        "display": os.fsdecode(target),
        "bytes_b64": base64.b64encode(target).decode("ascii"),
    }


@dataclass(frozen=True, slots=True)
class GitEntry:
    path: bytes
    mode: str
    oid: str
    stage: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            **_path_fields(self.path),
            "mode": self.mode,
            "oid": self.oid,
            "stage": self.stage,
        }


@dataclass(frozen=True, slots=True)
class FilesystemEntry:
    path: bytes
    kind: str
    mode_octal: str
    size: int
    git_mode: str | None = None
    git_oid: str | None = None
    sha256: str | None = None
    symlink_target: bytes | None = None
    device: int | None = None
    inode: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            **_path_fields(self.path),
            "kind": self.kind,
            "mode_octal": self.mode_octal,
            "size": self.size,
            "git_mode": self.git_mode,
            "git_oid": self.git_oid,
            "sha256": self.sha256,
            "symlink_target": _target_fields(self.symlink_target),
            "device": self.device,
            "inode": self.inode,
        }


@dataclass(frozen=True, slots=True)
class GitChange:
    path: bytes
    kind: str
    head: GitEntry | None = None
    index: GitEntry | None = None
    worktree: FilesystemEntry | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            **_path_fields(self.path),
            "kind": self.kind,
            "head": self.head.to_dict() if self.head is not None else None,
            "index": self.index.to_dict() if self.index is not None else None,
            "worktree": (
                self.worktree.to_dict() if self.worktree is not None else None
            ),
        }


@dataclass(frozen=True, slots=True)
class GitlinkState:
    path: bytes
    recorded_oid: str
    required_oid: str | None
    snapshot: "RepositorySnapshot"
    object_format_matches: bool

    @property
    def clean(self) -> bool:
        required_matches = (
            self.required_oid is None
            or self.snapshot.head_oid == self.required_oid
        )
        return (
            self.object_format_matches
            and self.snapshot.complete
            and self.snapshot.clean
            and self.snapshot.head_oid == self.recorded_oid
            and required_matches
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            **_path_fields(self.path),
            "recorded_oid": self.recorded_oid,
            "required_oid": self.required_oid,
            "object_format_matches": self.object_format_matches,
            "clean": self.clean,
            "snapshot": self.snapshot.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class RepositorySnapshot:
    """A complete or explicitly incomplete raw repository snapshot."""

    worktree: str
    git_dir: str | None
    common_dir: str | None
    object_format: str | None
    head_oid: str | None
    index_sha256: str | None
    head_entries: tuple[GitEntry, ...] = ()
    index_entries: tuple[GitEntry, ...] = ()
    worktree_entries: tuple[FilesystemEntry, ...] = ()
    staged_changes: tuple[GitChange, ...] = ()
    unstaged_changes: tuple[GitChange, ...] = ()
    untracked_entries: tuple[FilesystemEntry, ...] = ()
    allowed_untracked_roots: tuple[bytes, ...] = ()
    protected_roots: tuple[bytes, ...] = ()
    allow_untracked_regular_files: bool = False
    gitlinks: tuple[GitlinkState, ...] = ()
    complete: bool = False
    attempts: int = 1
    errors: tuple[str, ...] = ()
    schema_version: str = SCHEMA_VERSION
    policy: Mapping[str, Any] = field(
        default_factory=lambda: {
            "tracked_content": "raw bytes; no Git conversion",
            "regular_file_mode": "owner execute bit selects 100755 vs 100644",
            "untracked": (
                "all entries including ignored, except explicit reviewed roots; "
                "root .git excluded"
            ),
            "attributes": "not consulted",
            "git_objects": "exact commit/tree/blob payloads rehashed twice",
            "repository_config": "only bounded format fields parsed with --no-includes",
        }
    )

    @property
    def clean(self) -> bool:
        return (
            self.complete
            and not self.staged_changes
            and not self.unstaged_changes
            and not self.untracked_entries
            and all(link.clean for link in self.gitlinks)
        )

    def path_state(self, path: str | bytes | PurePosixPath) -> dict[str, Any]:
        """Return HEAD, index, and raw worktree state for one relative path."""

        normalized = _normalize_relative_path(path, SnapshotLimits())
        head = next(
            (item for item in self.head_entries if item.path == normalized),
            None,
        )
        index = next(
            (
                item
                for item in self.index_entries
                if item.path == normalized and item.stage == 0
            ),
            None,
        )
        worktree = next(
            (item for item in self.worktree_entries if item.path == normalized),
            None,
        )
        return {
            **_path_fields(normalized),
            "head": head.to_dict() if head is not None else None,
            "index": index.to_dict() if index is not None else None,
            "worktree": worktree.to_dict() if worktree is not None else None,
        }

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "object_format": self.object_format,
            "head_oid": self.head_oid,
            "index_sha256": self.index_sha256,
            "head_entries": [entry.to_dict() for entry in self.head_entries],
            "index_entries": [entry.to_dict() for entry in self.index_entries],
            "worktree_entries": [
                entry.to_dict() for entry in self.worktree_entries
            ],
            "staged_changes": [
                change.to_dict() for change in self.staged_changes
            ],
            "unstaged_changes": [
                change.to_dict() for change in self.unstaged_changes
            ],
            "untracked_entries": [
                entry.to_dict() for entry in self.untracked_entries
            ],
            "allowed_untracked_roots": [
                _path_fields(path) for path in self.allowed_untracked_roots
            ],
            "protected_roots": [
                _path_fields(path) for path in self.protected_roots
            ],
            "allow_untracked_regular_files": (
                self.allow_untracked_regular_files
            ),
            "gitlinks": [link.to_dict() for link in self.gitlinks],
            "complete": self.complete,
            "errors": list(self.errors),
            "policy": dict(self.policy),
        }

    @property
    def identity_sha256(self) -> str:
        encoded = json.dumps(
            self._identity_payload(),
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        return hashlib.sha256(encoded).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "worktree": self.worktree,
            "git_dir": self.git_dir,
            "common_dir": self.common_dir,
            "object_format": self.object_format,
            "head_oid": self.head_oid,
            "index_sha256": self.index_sha256,
            "head_entries": [entry.to_dict() for entry in self.head_entries],
            "index_entries": [entry.to_dict() for entry in self.index_entries],
            "worktree_entries": [
                entry.to_dict() for entry in self.worktree_entries
            ],
            "staged_changes": [
                change.to_dict() for change in self.staged_changes
            ],
            "unstaged_changes": [
                change.to_dict() for change in self.unstaged_changes
            ],
            "untracked_entries": [
                entry.to_dict() for entry in self.untracked_entries
            ],
            "allowed_untracked_roots": [
                _path_fields(path) for path in self.allowed_untracked_roots
            ],
            "protected_roots": [
                _path_fields(path) for path in self.protected_roots
            ],
            "allow_untracked_regular_files": (
                self.allow_untracked_regular_files
            ),
            "gitlinks": [link.to_dict() for link in self.gitlinks],
            "complete": self.complete,
            "clean": self.clean,
            "attempts": self.attempts,
            "errors": list(self.errors),
            "policy": dict(self.policy),
            "identity_sha256": self.identity_sha256,
        }


@dataclass(frozen=True, slots=True)
class _Layout:
    worktree: Path
    git_dir: Path
    common_dir: Path
    objects_dir: Path
    index_file: Path
    config_file: Path


@dataclass(frozen=True, slots=True)
class _FileImage:
    content: bytes
    sha256: str
    stat_key: tuple[int, int, int, int, int, int, int]


@dataclass(frozen=True, slots=True)
class _MetadataToken:
    layout: tuple[str, str, str]
    config_sha256: str
    head_oid: str
    index_sha256: str
    shared_indexes: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class _ObjectCheck:
    oid: str
    object_type: str
    size: int


@dataclass(frozen=True, slots=True)
class _TreeListing:
    entries: tuple[GitEntry, ...]
    tree_oids: tuple[str, ...]


@dataclass(slots=True)
class _ByteBudget:
    remaining: int

    def consume(self, amount: int) -> None:
        if amount < 0 or amount > self.remaining:
            raise UnsupportedRepository(
                "working-tree content exceeds max_total_file_bytes"
            )
        self.remaining -= amount


@dataclass(frozen=True, slots=True)
class _FilesystemScan:
    tracked: tuple[FilesystemEntry, ...]
    untracked: tuple[FilesystemEntry, ...]


def _mode_octal(mode: int) -> str:
    return f"{stat.S_IMODE(mode):04o}"


def _stat_key(value: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_mode),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
        int(value.st_nlink),
    )


def _read_regular_file(path: Path, limit: int, description: str) -> _FileImage:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise GitProvenanceError(f"cannot open {description} {path}: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise UnsupportedRepository(f"{description} is not a regular file: {path}")
        if before.st_size < 0 or before.st_size > limit:
            raise UnsupportedRepository(
                f"{description} exceeds byte limit ({before.st_size} > {limit})"
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, limit - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise UnsupportedRepository(f"{description} exceeds byte limit")
            chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if _stat_key(before) != _stat_key(after) or total != before.st_size:
        raise UnstableRepository(f"{description} changed while being read: {path}")
    content = b"".join(chunks)
    return _FileImage(
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
        stat_key=_stat_key(before),
    )


def _write_private(path: Path, content: bytes, mode: int = 0o600) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor = os.open(path, flags, mode)
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _copy_image(path: Path, image: _FileImage) -> None:
    _write_private(path, image.content)


def _validate_git_executable(git: Path) -> Path:
    if not git.is_absolute():
        raise ValueError("git executable must be absolute")
    try:
        resolved = git.resolve(strict=True)
        metadata = resolved.stat()
    except OSError as exc:
        raise GitProvenanceError(f"cannot resolve Git executable {git}: {exc}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise GitProvenanceError(f"Git executable is not regular: {resolved}")
    if metadata.st_mode & 0o022:
        raise GitProvenanceError(
            f"Git executable is group/world writable: {resolved}"
        )
    return resolved


def _base_git_environment() -> dict[str, str]:
    return {
        "PATH": "/nonexistent",
        "LANG": "C",
        "LC_ALL": "C",
        "TZ": "UTC",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_COUNT": "0",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_ATTR_SYSTEM": "/dev/null",
        "GIT_ATTR_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_LITERAL_PATHSPECS": "1",
    }


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        try:
            process.kill()
        except OSError:
            pass


def _run_bounded(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    limits: SnapshotLimits,
    stdin_data: bytes | None = None,
    stdout_sink: Any | None = None,
) -> tuple[bytes, bytes]:
    """Run a subprocess while bounding time and both output streams."""

    input_stream = None
    if stdin_data is not None:
        if len(stdin_data) > limits.max_metadata_bytes:
            raise UnsupportedRepository(
                "command input exceeds finite metadata limit"
            )
        input_stream = tempfile.TemporaryFile(mode="w+b")
        try:
            input_stream.write(stdin_data)
            input_stream.flush()
            input_stream.seek(0)
        except BaseException:
            input_stream.close()
            raise
    try:
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            env=dict(environment),
            stdin=(
                input_stream
                if input_stream is not None
                else subprocess.DEVNULL
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        if input_stream is not None:
            input_stream.close()
        raise GitProvenanceError(f"cannot execute {command!r}: {exc}") from exc
    assert process.stdout is not None
    assert process.stderr is not None
    selector = selectors.DefaultSelector()
    stdout = bytearray()
    stderr = bytearray()
    stdout_size = 0
    streams = {
        process.stdout.fileno(): (
            process.stdout,
            "stdout",
            limits.max_git_output_bytes,
        ),
        process.stderr.fileno(): (
            process.stderr,
            "stderr",
            limits.max_git_stderr_bytes,
        ),
    }
    for descriptor, (stream, _, _) in streams.items():
        os.set_blocking(descriptor, False)
        selector.register(stream, selectors.EVENT_READ, descriptor)
    deadline = time.monotonic() + limits.git_timeout_seconds
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _kill_process_group(process)
                raise GitProvenanceError(f"command timed out: {command!r}")
            events = selector.select(min(remaining, 0.25))
            if not events and process.poll() is not None:
                # Pipes can still contain buffered bytes after process exit.
                events = [
                    (key, selectors.EVENT_READ)
                    for key in list(selector.get_map().values())
                ]
            for key, _ in events:
                descriptor = int(key.data)
                stream, stream_name, cap = streams[descriptor]
                try:
                    chunk = os.read(descriptor, 64 * 1024)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(stream)
                    continue
                current_size = (
                    stdout_size
                    if stream_name == "stdout"
                    else len(stderr)
                )
                if current_size + len(chunk) > cap:
                    _kill_process_group(process)
                    raise UnsupportedRepository(
                        f"command output exceeds finite limit: {command!r}"
                    )
                if stream_name == "stderr":
                    stderr.extend(chunk)
                elif stdout_sink is None:
                    stdout.extend(chunk)
                    stdout_size += len(chunk)
                else:
                    view = memoryview(chunk)
                    while view:
                        written = stdout_sink.write(view)
                        if written is None or written <= 0:
                            raise OSError("short temporary output write")
                        view = view[written:]
                    stdout_size += len(chunk)
        remaining = max(0.0, deadline - time.monotonic())
        try:
            return_code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            _kill_process_group(process)
            raise GitProvenanceError(f"command timed out: {command!r}") from exc
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()
        if process.poll() is None:
            _kill_process_group(process)
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        if input_stream is not None:
            input_stream.close()
    if return_code != 0:
        raise GitProvenanceError(
            f"command failed exit={return_code}: {command!r}: "
            f"{bytes(stderr).decode('utf-8', errors='replace')!r}"
        )
    return bytes(stdout), bytes(stderr)


def _run_git(
    git: Path,
    arguments: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    limits: SnapshotLimits,
    require_empty_stderr: bool = True,
    stdin_data: bytes | None = None,
    stdout_sink: Any | None = None,
) -> bytes:
    stdout, stderr = _run_bounded(
        [
            str(git),
            "--no-pager",
            "--no-replace-objects",
            "--no-lazy-fetch",
            "--no-optional-locks",
            *arguments,
        ],
        cwd=cwd,
        environment=environment,
        limits=limits,
        stdin_data=stdin_data,
        stdout_sink=stdout_sink,
    )
    if require_empty_stderr and stderr:
        raise GitProvenanceError(
            "Git produced unexpected stderr: "
            + stderr.decode("utf-8", errors="replace")
        )
    return stdout


def _discover_layout(worktree: Path, limits: SnapshotLimits) -> _Layout:
    try:
        root = worktree.resolve(strict=True)
        root_stat = root.stat()
    except OSError as exc:
        raise GitProvenanceError(f"cannot resolve worktree {worktree}: {exc}") from exc
    if not stat.S_ISDIR(root_stat.st_mode):
        raise UnsupportedRepository(f"worktree is not a directory: {root}")

    marker = root / ".git"
    try:
        marker_stat = marker.lstat()
    except OSError as exc:
        raise GitProvenanceError(f"cannot inspect Git marker {marker}: {exc}") from exc
    if stat.S_ISDIR(marker_stat.st_mode):
        git_dir = marker.resolve(strict=True)
    elif stat.S_ISREG(marker_stat.st_mode):
        image = _read_regular_file(marker, 4096, "Git marker")
        if image.content.count(b"\n") > 1:
            raise UnsupportedRepository("Git marker must contain exactly one line")
        line = image.content.rstrip(b"\n")
        if not line.startswith(b"gitdir: ") or not line[8:]:
            raise UnsupportedRepository(f"invalid Git marker: {marker}")
        target = Path(os.fsdecode(line[8:]))
        if not target.is_absolute():
            target = root / target
        try:
            git_dir = target.resolve(strict=True)
        except OSError as exc:
            raise GitProvenanceError(
                f"cannot resolve Git directory from {marker}: {exc}"
            ) from exc
    else:
        raise UnsupportedRepository(f"unsupported .git marker type: {marker}")
    if not git_dir.is_dir():
        raise UnsupportedRepository(f"Git directory is not a directory: {git_dir}")

    commondir_file = git_dir / "commondir"
    try:
        commondir_file.lstat()
    except FileNotFoundError:
        common_dir = git_dir
    except OSError as exc:
        raise GitProvenanceError(f"cannot inspect commondir: {exc}") from exc
    else:
        image = _read_regular_file(commondir_file, 4096, "commondir")
        if image.content.count(b"\n") > 1:
            raise UnsupportedRepository("commondir must contain exactly one line")
        value = image.content.rstrip(b"\n")
        if not value:
            raise UnsupportedRepository("commondir is empty")
        common_path = Path(os.fsdecode(value))
        if not common_path.is_absolute():
            common_path = git_dir / common_path
        try:
            common_dir = common_path.resolve(strict=True)
        except OSError as exc:
            raise GitProvenanceError(f"cannot resolve commondir: {exc}") from exc
    if not common_dir.is_dir():
        raise UnsupportedRepository(f"common Git directory is invalid: {common_dir}")

    objects_dir = common_dir / "objects"
    try:
        objects_stat = objects_dir.lstat()
    except OSError as exc:
        raise GitProvenanceError(f"cannot inspect object directory: {exc}") from exc
    if not stat.S_ISDIR(objects_stat.st_mode):
        raise UnsupportedRepository(
            f"object directory is not a directory: {objects_dir}"
        )
    for name in ("alternates", "http-alternates"):
        alternate = objects_dir / "info" / name
        try:
            alternate.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise GitProvenanceError(f"cannot inspect {alternate}: {exc}") from exc
        raise UnsupportedRepository(
            f"object alternates are outside the finite provenance contract: {alternate}"
        )

    return _Layout(
        worktree=root,
        git_dir=git_dir,
        common_dir=common_dir,
        objects_dir=objects_dir,
        index_file=git_dir / "index",
        config_file=common_dir / "config",
    )


def _parse_config_output(output: bytes) -> dict[str, list[bytes]]:
    values: dict[str, list[bytes]] = {}
    for record in output.split(b"\0"):
        if not record:
            continue
        if b"\n" in record:
            raw_key, value = record.split(b"\n", 1)
        else:
            raw_key, value = record, b""
        try:
            key = raw_key.decode("ascii").lower()
        except UnicodeDecodeError as exc:
            raise UnsupportedRepository("non-ASCII Git config key") from exc
        values.setdefault(key, []).append(value)
    return values


def _single_ascii_config(
    values: Mapping[str, list[bytes]],
    key: str,
    *,
    default: str | None = None,
) -> str | None:
    found = values.get(key, [])
    if not found:
        return default
    if len(found) != 1:
        raise UnsupportedRepository(f"duplicate repository format key: {key}")
    try:
        return found[0].decode("ascii").lower()
    except UnicodeDecodeError as exc:
        raise UnsupportedRepository(f"non-ASCII value for {key}") from exc


def _read_repository_format(
    git: Path,
    copied_config: Path,
    *,
    cwd: Path,
    limits: SnapshotLimits,
) -> str:
    output = _run_git(
        git,
        [
            "config",
            "--file",
            str(copied_config),
            "--no-includes",
            "--null",
            "--list",
        ],
        cwd=cwd,
        environment=_base_git_environment(),
        limits=limits,
    )
    values = _parse_config_output(output)
    for key in values:
        if key == "include.path" or (
            key.startswith("includeif.") and key.endswith(".path")
        ):
            raise UnsupportedRepository(
                "repository config includes are outside the finite contract"
            )

    version = _single_ascii_config(
        values, "core.repositoryformatversion", default="0"
    )
    if version not in {"0", "1"}:
        raise UnsupportedRepository(f"unsupported repository format version: {version}")
    extension_keys = {
        key: value for key, value in values.items() if key.startswith("extensions.")
    }
    allowed_extensions = {
        "extensions.objectformat",
        "extensions.refstorage",
        "extensions.worktreeconfig",
        "extensions.preciousobjects",
        "extensions.noop",
    }
    unknown = sorted(set(extension_keys) - allowed_extensions)
    if unknown:
        raise UnsupportedRepository(
            "unsupported repository extensions: " + ",".join(unknown)
        )
    ref_storage = _single_ascii_config(
        values, "extensions.refstorage", default="files"
    )
    if ref_storage != "files":
        raise UnsupportedRepository(
            f"unsupported reference storage: {ref_storage}"
        )
    object_format = _single_ascii_config(
        values, "extensions.objectformat", default="sha1"
    )
    if object_format not in _ALLOWED_OBJECT_FORMATS:
        raise UnsupportedRepository(f"unsupported object format: {object_format}")
    if object_format == "sha256" and version != "1":
        raise UnsupportedRepository("SHA-256 requires repository format v1")
    return object_format


def _valid_refname(refname: bytes, limits: SnapshotLimits) -> bool:
    if not refname.startswith(b"refs/") or refname.endswith((b".", b"/")):
        return False
    if len(refname) > limits.max_path_bytes:
        return False
    if any(value < 0x20 or value == 0x7F for value in refname):
        return False
    if any(token in refname for token in (b"..", b"//", b"@{")):
        return False
    if any(value in refname for value in b" ~^:?*[\\"):
        return False
    parts = refname.split(b"/")
    if len(parts) > limits.max_path_components:
        return False
    return all(
        part
        and len(part) <= limits.max_path_component_bytes
        and not part.startswith(b".")
        and not part.endswith(b".")
        and part not in {b".", b".."}
        and not part.endswith(b".lock")
        for part in parts
    )


def _is_per_worktree_ref(refname: bytes) -> bool:
    return refname.startswith(
        (b"refs/bisect/", b"refs/worktree/", b"refs/rewritten/")
    )


def _validate_oid(value: bytes, object_format: str, description: str) -> str:
    expected = _OID_LENGTH[object_format]
    if len(value) != expected or any(
        byte not in b"0123456789abcdef" for byte in value
    ):
        raise UnsupportedRepository(
            f"invalid {object_format} object name for {description}"
        )
    return value.decode("ascii")


def _packed_refs(
    layout: _Layout,
    object_format: str,
    limits: SnapshotLimits,
) -> dict[bytes, str]:
    path = layout.common_dir / "packed-refs"
    try:
        path.lstat()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise GitProvenanceError(f"cannot inspect packed-refs: {exc}") from exc
    image = _read_regular_file(path, limits.max_metadata_bytes, "packed-refs")
    result: dict[bytes, str] = {}
    aggregate_refname_bytes = 0
    for line in image.content.splitlines():
        if not line or line.startswith(b"#"):
            continue
        if line.startswith(b"^"):
            raise UnsupportedRepository(
                "peeled packed-refs entries are unsupported"
            )
        if b" " not in line:
            raise UnsupportedRepository("malformed packed-refs entry")
        raw_oid, refname = line.split(b" ", 1)
        if not _valid_refname(refname, limits):
            raise UnsupportedRepository("invalid refname in packed-refs")
        oid = _validate_oid(raw_oid, object_format, "packed ref")
        if refname in result:
            raise UnsupportedRepository("duplicate refname in packed-refs")
        result[refname] = oid
        aggregate_refname_bytes += len(refname)
        if len(result) > limits.max_entries:
            raise UnsupportedRepository("packed-refs entry limit exceeded")
        if aggregate_refname_bytes > limits.max_aggregate_path_bytes:
            raise UnsupportedRepository(
                "packed-refs aggregate refname bytes exceeded"
            )
    return result


def _loose_ref_path(layout: _Layout, refname: bytes) -> Path:
    base = layout.git_dir if _is_per_worktree_ref(refname) else layout.common_dir
    return base.joinpath(*(os.fsdecode(part) for part in refname.split(b"/")))


def _resolve_head(
    layout: _Layout,
    object_format: str,
    limits: SnapshotLimits,
) -> str:
    packed = _packed_refs(layout, object_format, limits)
    current_path = layout.git_dir / "HEAD"
    seen: set[bytes] = set()
    for _ in range(16):
        image = _read_regular_file(current_path, 4096, "reference")
        if image.content.count(b"\n") > 1:
            raise UnsupportedRepository("reference must contain exactly one line")
        value = image.content.rstrip(b"\n")
        if value.startswith(b"ref: "):
            refname = value[5:]
            if not _valid_refname(refname, limits):
                raise UnsupportedRepository("HEAD contains an invalid symbolic ref")
            if refname in seen:
                raise UnsupportedRepository("symbolic ref cycle")
            seen.add(refname)
            path = _loose_ref_path(layout, refname)
            try:
                path.lstat()
            except FileNotFoundError:
                if refname in packed:
                    return packed[refname]
                raise GitProvenanceError(f"unresolved HEAD ref: {os.fsdecode(refname)}")
            except OSError as exc:
                raise GitProvenanceError(f"cannot inspect HEAD ref: {exc}") from exc
            current_path = path
            continue
        return _validate_oid(value, object_format, "HEAD")
    raise UnsupportedRepository("symbolic ref depth exceeds limit")


def _shared_index_images(
    layout: _Layout,
    object_format: str,
    limits: SnapshotLimits,
) -> tuple[tuple[str, _FileImage], ...]:
    expected_length = _OID_LENGTH[object_format]
    try:
        with os.scandir(layout.git_dir) as iterator:
            names: list[str] = []
            for entry_number, entry in enumerate(iterator, start=1):
                if entry_number > limits.max_entries:
                    raise UnsupportedRepository(
                        "Git directory entry limit exceeded"
                    )
                names.append(entry.name)
        names.sort()
    except OSError as exc:
        raise GitProvenanceError(f"cannot list Git directory: {exc}") from exc
    result: list[tuple[str, _FileImage]] = []
    total_bytes = 0
    for name in names:
        match = _SHARED_INDEX_RE.fullmatch(name)
        if match is None:
            continue
        suffix = match.group(1)
        if len(suffix) != expected_length:
            raise UnsupportedRepository(f"malformed shared index name: {name}")
        if len(result) >= limits.max_shared_indexes:
            raise UnsupportedRepository("too many shared index files")
        image = _read_regular_file(
            layout.git_dir / name,
            limits.max_index_bytes,
            "shared index",
        )
        total_bytes += len(image.content)
        if total_bytes > limits.max_index_bytes:
            raise UnsupportedRepository(
                "shared indexes exceed aggregate index byte limit"
            )
        result.append((name, image))
    return tuple(result)


def _metadata_token(
    layout: _Layout,
    object_format: str,
    limits: SnapshotLimits,
) -> _MetadataToken:
    config = _read_regular_file(
        layout.config_file, limits.max_config_bytes, "repository config"
    )
    index = _read_regular_file(
        layout.index_file, limits.max_index_bytes, "Git index"
    )
    head_oid = _resolve_head(layout, object_format, limits)
    shared = _shared_index_images(layout, object_format, limits)
    return _MetadataToken(
        layout=(
            str(layout.git_dir),
            str(layout.common_dir),
            str(layout.objects_dir),
        ),
        config_sha256=config.sha256,
        head_oid=head_oid,
        index_sha256=index.sha256,
        shared_indexes=tuple((name, image.sha256) for name, image in shared),
    )


def _synthetic_config(object_format: str) -> bytes:
    if object_format == "sha1":
        return (
            b"[core]\n"
            b"\trepositoryformatversion = 0\n"
            b"\tbare = false\n"
            b"\tfilemode = true\n"
            b"\tattributesfile = /dev/null\n"
            b"\texcludesfile = /dev/null\n"
            b"\tfsmonitor = false\n"
        )
    return (
        b"[core]\n"
        b"\trepositoryformatversion = 1\n"
        b"\tbare = false\n"
        b"\tfilemode = true\n"
        b"\tattributesfile = /dev/null\n"
        b"\texcludesfile = /dev/null\n"
        b"\tfsmonitor = false\n"
        b"[extensions]\n"
        b"\tobjectformat = sha256\n"
    )


def _plumbing_environment(
    synthetic_git: Path,
    empty_worktree: Path,
    objects_dir: Path,
    index_file: Path,
) -> dict[str, str]:
    return {
        **_base_git_environment(),
        "GIT_DIR": str(synthetic_git),
        "GIT_COMMON_DIR": str(synthetic_git),
        "GIT_INDEX_FILE": str(index_file),
        "GIT_WORK_TREE": str(empty_worktree),
        "GIT_OBJECT_DIRECTORY": str(objects_dir),
    }


def _commit_root_tree(
    git: Path,
    *,
    cwd: Path,
    environment: Mapping[str, str],
    head_oid: str,
    object_format: str,
    limits: SnapshotLimits,
) -> str:
    content = _run_git(
        git,
        ["cat-file", "commit", head_oid],
        cwd=cwd,
        environment=environment,
        limits=limits,
    )
    if len(content) > limits.max_metadata_bytes:
        raise UnsupportedRepository("HEAD commit exceeds metadata byte limit")
    first_line, separator, _ = content.partition(b"\n")
    if not separator or not first_line.startswith(b"tree "):
        raise GitProvenanceError("HEAD commit lacks a canonical tree header")
    fields = first_line.split(b" ")
    if len(fields) != 2:
        raise GitProvenanceError("HEAD commit has a malformed tree header")
    return _validate_oid(fields[1], object_format, "HEAD root tree")


def _parse_ls_tree(
    output: bytes,
    object_format: str,
    limits: SnapshotLimits,
) -> _TreeListing:
    entries: list[GitEntry] = []
    tree_oids: list[str] = []
    seen: set[bytes] = set()
    aggregate_path_bytes = 0
    record_count = 0
    for record in output.split(b"\0"):
        if not record:
            continue
        if b"\t" not in record:
            raise GitProvenanceError("malformed ls-tree record")
        prefix, path = record.split(b"\t", 1)
        fields = prefix.split(b" ")
        if len(fields) != 3:
            raise GitProvenanceError("malformed ls-tree prefix")
        raw_mode, raw_type, raw_oid = fields
        try:
            mode = raw_mode.decode("ascii")
        except UnicodeDecodeError as exc:
            raise GitProvenanceError("non-ASCII tree mode") from exc
        if mode != "040000" and mode not in _TRACKED_MODES:
            raise UnsupportedRepository(f"unsupported tree entry mode: {mode}")
        expected_type = (
            b"tree"
            if mode == "040000"
            else b"commit"
            if mode == "160000"
            else b"blob"
        )
        if raw_type != expected_type:
            raise GitProvenanceError("tree entry type does not match mode")
        _validate_relative_bytes(path, limits)
        aggregate_path_bytes += len(path)
        if aggregate_path_bytes > limits.max_aggregate_path_bytes:
            raise UnsupportedRepository(
                "HEAD tree aggregate path byte limit exceeded"
            )
        if path in seen:
            raise GitProvenanceError("duplicate path in HEAD tree")
        seen.add(path)
        oid = _validate_oid(raw_oid, object_format, "tree entry")
        if mode == "040000":
            tree_oids.append(oid)
        else:
            entries.append(
                GitEntry(
                    path=path,
                    mode=mode,
                    oid=oid,
                )
            )
        record_count += 1
        if record_count > limits.max_entries:
            raise UnsupportedRepository("HEAD tree entry limit exceeded")
    entries.sort(key=lambda item: item.path)
    tree_oids.sort()
    return _TreeListing(
        entries=tuple(entries),
        tree_oids=tuple(tree_oids),
    )


def _parse_ls_files(
    output: bytes,
    object_format: str,
    limits: SnapshotLimits,
) -> tuple[GitEntry, ...]:
    entries: list[GitEntry] = []
    seen: set[tuple[bytes, int]] = set()
    aggregate_path_bytes = 0
    for record in output.split(b"\0"):
        if not record:
            continue
        if b"\t" not in record:
            raise GitProvenanceError("malformed ls-files record")
        prefix, path = record.split(b"\t", 1)
        fields = prefix.split(b" ")
        if len(fields) != 3:
            raise GitProvenanceError("malformed ls-files prefix")
        raw_mode, raw_oid, raw_stage = fields
        try:
            mode = raw_mode.decode("ascii")
            stage = int(raw_stage.decode("ascii"), 10)
        except (UnicodeDecodeError, ValueError) as exc:
            raise GitProvenanceError("invalid index entry prefix") from exc
        if mode == "040000":
            raise UnsupportedRepository("sparse index is unsupported")
        if mode not in _TRACKED_MODES:
            raise UnsupportedRepository(f"unsupported index entry mode: {mode}")
        if stage not in {0, 1, 2, 3}:
            raise GitProvenanceError("invalid index stage")
        _validate_relative_bytes(path, limits)
        aggregate_path_bytes += len(path)
        if aggregate_path_bytes > limits.max_aggregate_path_bytes:
            raise UnsupportedRepository(
                "index aggregate path byte limit exceeded"
            )
        key = (path, stage)
        if key in seen:
            raise GitProvenanceError("duplicate path/stage in index")
        seen.add(key)
        entries.append(
            GitEntry(
                path=path,
                mode=mode,
                oid=_validate_oid(raw_oid, object_format, "index entry"),
                stage=stage,
            )
        )
        if len(entries) > limits.max_entries:
            raise UnsupportedRepository("index entry limit exceeded")
    entries.sort(key=lambda item: (item.path, item.stage))
    return tuple(entries)


def _verify_required_objects(
    git: Path,
    *,
    cwd: Path,
    environment: Mapping[str, str],
    head_oid: str,
    root_tree_oid: str,
    tree_oids: Sequence[str],
    head_entries: Sequence[GitEntry],
    index_entries: Sequence[GitEntry],
    object_format: str,
    limits: SnapshotLimits,
) -> tuple[_ObjectCheck, ...]:
    requirements: dict[str, str] = {head_oid: "commit"}

    def require(oid: str, object_type: str) -> None:
        previous = requirements.setdefault(oid, object_type)
        if previous != object_type:
            raise GitProvenanceError(
                f"object required with conflicting exact types: {oid}"
            )

    require(root_tree_oid, "tree")
    for oid in tree_oids:
        require(oid, "tree")
    for entry in head_entries:
        if entry.mode != "160000":
            require(entry.oid, "blob")
    for entry in index_entries:
        if entry.mode != "160000":
            require(entry.oid, "blob")
    if len(requirements) > limits.max_object_checks:
        raise UnsupportedRepository("required Git object check count exceeded")

    ordered = tuple(sorted(requirements))
    input_size = sum(len(oid) + 1 for oid in ordered)
    if input_size > limits.max_metadata_bytes:
        raise UnsupportedRepository(
            "Git object check input exceeds finite metadata limit"
        )
    input_data = b"".join(
        oid.encode("ascii") + b"\n" for oid in ordered
    )
    output = _run_git(
        git,
        [
            "cat-file",
            "--batch-check=%(objectname) %(objecttype) %(objectsize)",
        ],
        cwd=cwd,
        environment=environment,
        limits=limits,
        stdin_data=input_data,
    )
    if not output.endswith(b"\n"):
        raise GitProvenanceError("cat-file batch-check output is unterminated")
    lines = output[:-1].split(b"\n")
    if len(lines) != len(ordered):
        raise GitProvenanceError(
            "cat-file batch-check response count mismatch"
        )

    result: list[_ObjectCheck] = []
    for expected_oid, line in zip(ordered, lines, strict=True):
        fields = line.split(b" ")
        if len(fields) != 3:
            raise GitProvenanceError(
                f"missing or malformed Git object: {expected_oid}"
            )
        raw_oid, raw_type, raw_size = fields
        if raw_oid != expected_oid.encode("ascii"):
            raise GitProvenanceError(
                "cat-file batch-check returned an unexpected object name"
            )
        expected_type = requirements[expected_oid].encode("ascii")
        if raw_type != expected_type:
            raise GitProvenanceError(
                f"Git object {expected_oid} has type "
                f"{raw_type.decode('ascii', errors='replace')!r}, "
                f"expected {expected_type.decode('ascii')!r}"
            )
        if (
            not raw_size
            or len(raw_size) > 20
            or any(value not in b"0123456789" for value in raw_size)
            or (len(raw_size) > 1 and raw_size.startswith(b"0"))
        ):
            raise GitProvenanceError(
                f"Git object {expected_oid} has an invalid size"
            )
        size = int(raw_size, 10)
        size_limit = (
            limits.max_metadata_bytes
            if expected_type in {b"commit", b"tree"}
            else limits.max_single_file_bytes
        )
        if size > size_limit:
            raise UnsupportedRepository(
                f"Git {expected_type.decode('ascii')} object exceeds byte limit"
            )
        result.append(
            _ObjectCheck(
                oid=expected_oid,
                object_type=expected_type.decode("ascii"),
                size=size,
            )
        )
    checks = tuple(result)
    _verify_exact_object_contents(
        git,
        cwd=cwd,
        environment=environment,
        checks=checks,
        object_format=object_format,
        limits=limits,
    )
    return checks


def _verify_exact_object_contents(
    git: Path,
    *,
    cwd: Path,
    environment: Mapping[str, str],
    checks: Sequence[_ObjectCheck],
    object_format: str,
    limits: SnapshotLimits,
) -> None:
    input_size = sum(len(check.oid) + 1 for check in checks)
    if input_size > limits.max_metadata_bytes:
        raise UnsupportedRepository(
            "Git object content input exceeds finite metadata limit"
        )
    estimated_output = sum(
        len(check.oid)
        + len(check.object_type)
        + len(str(check.size))
        + check.size
        + 4
        for check in checks
    )
    if estimated_output > limits.max_git_output_bytes:
        raise UnsupportedRepository(
            "required Git object contents exceed output byte limit"
        )
    input_data = b"".join(
        check.oid.encode("ascii") + b"\n" for check in checks
    )

    with tempfile.TemporaryFile(mode="w+b") as output:
        _run_git(
            git,
            ["cat-file", "--batch"],
            cwd=cwd,
            environment=environment,
            limits=limits,
            stdin_data=input_data,
            stdout_sink=output,
        )
        output.flush()
        output.seek(0)
        for check in checks:
            header = output.readline(257)
            if not header.endswith(b"\n") or len(header) > 256:
                raise GitProvenanceError(
                    f"malformed content header for Git object {check.oid}"
                )
            fields = header[:-1].split(b" ")
            if len(fields) != 3:
                raise GitProvenanceError(
                    f"missing content for Git object {check.oid}"
                )
            raw_oid, raw_type, raw_size = fields
            if raw_oid != check.oid.encode("ascii"):
                raise GitProvenanceError(
                    "Git object content response name mismatch"
                )
            if raw_type != check.object_type.encode("ascii"):
                raise GitProvenanceError(
                    f"Git object {check.oid} content type mismatch"
                )
            expected_size = str(check.size).encode("ascii")
            if raw_size != expected_size:
                raise GitProvenanceError(
                    f"Git object {check.oid} content size mismatch"
                )

            digest = hashlib.new(object_format)
            digest.update(
                f"{check.object_type} {check.size}\0".encode("ascii")
            )
            remaining = check.size
            while remaining:
                chunk = output.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise GitProvenanceError(
                        f"truncated content for Git object {check.oid}"
                    )
                digest.update(chunk)
                remaining -= len(chunk)
            if output.read(1) != b"\n":
                raise GitProvenanceError(
                    f"missing content separator for Git object {check.oid}"
                )
            if digest.hexdigest() != check.oid:
                raise GitProvenanceError(
                    f"Git object content identity mismatch: {check.oid}"
                )
        if output.read(1):
            raise GitProvenanceError(
                "unexpected trailing Git object content output"
            )


def _validate_relative_bytes(path: bytes, limits: SnapshotLimits) -> None:
    if not path or path.startswith(b"/") or b"\0" in path:
        raise UnsupportedRepository("invalid absolute/empty repository path")
    if len(path) > limits.max_path_bytes:
        raise UnsupportedRepository("repository path byte limit exceeded")
    parts = path.split(b"/")
    if any(part in {b"", b".", b".."} for part in parts):
        raise UnsupportedRepository("repository path contains unsafe component")
    if len(parts) > limits.max_path_components:
        raise UnsupportedRepository("repository path component count exceeded")
    if any(len(part) > limits.max_path_component_bytes for part in parts):
        raise UnsupportedRepository("repository path component byte limit exceeded")
    if parts[0] == b".git":
        raise UnsupportedRepository("index/tree contains administrative .git path")


def _normalize_relative_path(
    path: str | bytes | PurePosixPath,
    limits: SnapshotLimits,
) -> bytes:
    if isinstance(path, bytes):
        result = path
    else:
        result = os.fsencode(str(path))
    _validate_relative_bytes(result, limits)
    return result


def _stage_zero_map(entries: Iterable[GitEntry]) -> dict[bytes, GitEntry]:
    return {entry.path: entry for entry in entries if entry.stage == 0}


def _entry_change_kind(before: GitEntry, after: GitEntry) -> str | None:
    changes: list[str] = []
    if before.mode != after.mode:
        before_type = before.mode[:2]
        after_type = after.mode[:2]
        changes.append("type" if before_type != after_type else "mode")
    if before.oid != after.oid:
        changes.append("content")
    return "+".join(changes) if changes else None


def _staged_changes(
    head: Mapping[bytes, GitEntry],
    index_entries: Sequence[GitEntry],
) -> tuple[GitChange, ...]:
    result: list[GitChange] = []
    index = _stage_zero_map(index_entries)
    unmerged_paths = sorted(
        {entry.path for entry in index_entries if entry.stage != 0}
    )
    for path in unmerged_paths:
        result.append(
            GitChange(path=path, kind="unmerged", head=head.get(path))
        )
    for path in sorted(set(head) | set(index)):
        before = head.get(path)
        after = index.get(path)
        if before is None:
            result.append(GitChange(path=path, kind="added", index=after))
        elif after is None:
            result.append(GitChange(path=path, kind="deleted", head=before))
        else:
            kind = _entry_change_kind(before, after)
            if kind is not None:
                result.append(
                    GitChange(path=path, kind=kind, head=before, index=after)
                )
    return tuple(result)


def _filesystem_kind(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "regular"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISCHR(mode):
        return "character-device"
    if stat.S_ISBLK(mode):
        return "block-device"
    return "other"


def _hash_regular(
    path: bytes,
    relative: bytes,
    initial: os.stat_result,
    object_format: str,
    limits: SnapshotLimits,
    budget: _ByteBudget,
) -> FilesystemEntry:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise GitProvenanceError(
            f"cannot open tracked/worktree file {os.fsdecode(relative)}: {exc}"
        ) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise UnstableRepository(
                f"file type changed while opening {os.fsdecode(relative)}"
            )
        if _stat_key(initial) != _stat_key(before):
            raise UnstableRepository(
                f"file changed before hashing {os.fsdecode(relative)}"
            )
        if before.st_size < 0 or before.st_size > limits.max_single_file_bytes:
            raise UnsupportedRepository(
                f"file exceeds max_single_file_bytes: {os.fsdecode(relative)}"
            )
        budget.consume(int(before.st_size))
        git_hash = hashlib.new(object_format)
        git_hash.update(f"blob {before.st_size}\0".encode("ascii"))
        content_hash = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > before.st_size:
                raise UnstableRepository(
                    f"file grew while hashing {os.fsdecode(relative)}"
                )
            git_hash.update(chunk)
            content_hash.update(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        _stat_key(before) != _stat_key(after)
        or total != before.st_size
    ):
        raise UnstableRepository(
            f"file changed while hashing {os.fsdecode(relative)}"
        )
    git_mode = "100755" if before.st_mode & stat.S_IXUSR else "100644"
    return FilesystemEntry(
        path=relative,
        kind="regular",
        mode_octal=_mode_octal(before.st_mode),
        size=int(before.st_size),
        git_mode=git_mode,
        git_oid=git_hash.hexdigest(),
        sha256=content_hash.hexdigest(),
        device=int(before.st_dev),
        inode=int(before.st_ino),
    )


def _inspect_filesystem_entry(
    absolute: bytes,
    relative: bytes,
    metadata: os.stat_result,
    object_format: str,
    limits: SnapshotLimits,
    budget: _ByteBudget,
) -> FilesystemEntry:
    kind = _filesystem_kind(metadata.st_mode)
    if kind == "regular":
        return _hash_regular(
            absolute, relative, metadata, object_format, limits, budget
        )
    if kind == "symlink":
        try:
            target = os.readlink(absolute)
            after = os.lstat(absolute)
        except OSError as exc:
            raise GitProvenanceError(
                f"cannot read symlink {os.fsdecode(relative)}: {exc}"
            ) from exc
        if _stat_key(metadata) != _stat_key(after):
            raise UnstableRepository(
                f"symlink changed while reading {os.fsdecode(relative)}"
            )
        if isinstance(target, str):
            target_bytes = os.fsencode(target)
        else:
            target_bytes = target
        if len(target_bytes) > limits.max_single_file_bytes:
            raise UnsupportedRepository("symlink target exceeds byte limit")
        budget.consume(len(target_bytes))
        git_hash = hashlib.new(object_format)
        git_hash.update(f"blob {len(target_bytes)}\0".encode("ascii"))
        git_hash.update(target_bytes)
        return FilesystemEntry(
            path=relative,
            kind="symlink",
            mode_octal=_mode_octal(metadata.st_mode),
            size=len(target_bytes),
            git_mode="120000",
            git_oid=git_hash.hexdigest(),
            sha256=hashlib.sha256(target_bytes).hexdigest(),
            symlink_target=target_bytes,
            device=int(metadata.st_dev),
            inode=int(metadata.st_ino),
        )
    return FilesystemEntry(
        path=relative,
        kind=kind,
        mode_octal=_mode_octal(metadata.st_mode),
        size=int(metadata.st_size),
        git_mode="160000" if kind == "directory" else None,
        device=int(metadata.st_dev),
        inode=int(metadata.st_ino),
    )


def _scan_worktree(
    worktree: Path,
    index: Mapping[bytes, GitEntry],
    object_format: str,
    limits: SnapshotLimits,
    allowed_untracked_roots: frozenset[bytes],
    allow_untracked_regular_files: bool,
) -> _FilesystemScan:
    root = os.fsencode(worktree)
    tracked: dict[bytes, FilesystemEntry] = {}
    untracked: dict[bytes, FilesystemEntry] = {}
    prefixes: set[bytes] = set()
    prefix_bytes = 0
    for path in index:
        parts = path.split(b"/")
        prefix = b""
        for part in parts[:-1]:
            prefix = part if not prefix else prefix + b"/" + part
            if prefix in prefixes:
                continue
            prefixes.add(prefix)
            prefix_bytes += len(prefix)
            if len(prefixes) > limits.max_derived_prefixes:
                raise UnsupportedRepository(
                    "derived tracked-directory prefix count exceeded"
                )
            if prefix_bytes > limits.max_derived_prefix_bytes:
                raise UnsupportedRepository(
                    "derived tracked-directory prefix bytes exceeded"
                )
    budget = _ByteBudget(limits.max_total_file_bytes)
    entry_count = 0
    aggregate_entry_path_bytes = 0

    def add_untracked(entry: FilesystemEntry) -> None:
        nonlocal aggregate_entry_path_bytes, entry_count
        entry_count += 1
        aggregate_entry_path_bytes += len(entry.path)
        if entry_count > limits.max_entries:
            raise UnsupportedRepository("working-tree entry limit exceeded")
        if aggregate_entry_path_bytes > limits.max_aggregate_path_bytes:
            raise UnsupportedRepository(
                "working-tree aggregate path byte limit exceeded"
            )
        untracked[entry.path] = entry

    def add_tracked(entry: FilesystemEntry) -> None:
        nonlocal aggregate_entry_path_bytes, entry_count
        entry_count += 1
        aggregate_entry_path_bytes += len(entry.path)
        if entry_count > limits.max_entries:
            raise UnsupportedRepository("working-tree entry limit exceeded")
        if aggregate_entry_path_bytes > limits.max_aggregate_path_bytes:
            raise UnsupportedRepository(
                "working-tree aggregate path byte limit exceeded"
            )
        tracked[entry.path] = entry

    def walk(directory: bytes, relative_dir: bytes) -> None:
        try:
            before = os.stat(directory, follow_symlinks=False)
            with os.scandir(directory) as iterator:
                children = []
                for child_number, child in enumerate(iterator, start=1):
                    if child_number > limits.max_entries:
                        raise UnsupportedRepository(
                            "directory entry limit exceeded"
                        )
                    children.append(child)
                children.sort(key=lambda item: item.name)
        except OSError as exc:
            raise GitProvenanceError(
                f"cannot enumerate {os.fsdecode(relative_dir or b'.')}: {exc}"
            ) from exc
        if not stat.S_ISDIR(before.st_mode):
            raise UnstableRepository("directory changed type during scan")
        for child in children:
            name = child.name
            if isinstance(name, str):
                name_bytes = os.fsencode(name)
            else:
                name_bytes = name
            if not relative_dir and name_bytes == b".git":
                continue
            relative = (
                name_bytes
                if not relative_dir
                else relative_dir + b"/" + name_bytes
            )
            _validate_relative_bytes(relative, limits)
            absolute = os.fsencode(child.path)
            try:
                metadata = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise GitProvenanceError(
                    f"cannot inspect {os.fsdecode(relative)}: {exc}"
                ) from exc
            expected = index.get(relative)
            kind = _filesystem_kind(metadata.st_mode)
            if (
                expected is None
                and relative in allowed_untracked_roots
            ):
                # The caller has reviewed this exact untracked root.  Its
                # descendants are outside this snapshot's source-provenance
                # domain and are not enumerated.  Tracked-path overlap is
                # rejected before scanning.
                if kind == "regular" and not allow_untracked_regular_files:
                    raise UnsupportedRepository(
                        "allowed untracked regular files require explicit opt-in: "
                        f"{os.fsdecode(relative)}"
                    )
                if kind not in {"regular", "directory"}:
                    raise UnsupportedRepository(
                        "allowed untracked root has unsupported type: "
                        f"{os.fsdecode(relative)} ({kind})"
                    )
                continue
            if expected is not None and expected.mode == "160000":
                add_tracked(
                    _inspect_filesystem_entry(
                        absolute,
                        relative,
                        metadata,
                        object_format,
                        limits,
                        budget,
                    )
                )
                continue
            if kind == "directory":
                if expected is not None:
                    add_tracked(
                        _inspect_filesystem_entry(
                            absolute,
                            relative,
                            metadata,
                            object_format,
                            limits,
                            budget,
                        )
                    )
                elif relative not in prefixes:
                    add_untracked(
                        _inspect_filesystem_entry(
                            absolute,
                            relative,
                            metadata,
                            object_format,
                            limits,
                            budget,
                        )
                    )
                walk(absolute, relative)
                continue
            inspected = _inspect_filesystem_entry(
                absolute,
                relative,
                metadata,
                object_format,
                limits,
                budget,
            )
            if expected is not None:
                add_tracked(inspected)
            else:
                add_untracked(inspected)
        try:
            after = os.stat(directory, follow_symlinks=False)
        except OSError as exc:
            raise UnstableRepository("directory disappeared during scan") from exc
        if _stat_key(before) != _stat_key(after):
            raise UnstableRepository(
                f"directory changed while scanning {os.fsdecode(relative_dir or b'.')}"
            )

    walk(root, b"")
    return _FilesystemScan(
        tracked=tuple(tracked[path] for path in sorted(tracked)),
        untracked=tuple(untracked[path] for path in sorted(untracked)),
    )


def _unstaged_changes(
    index: Mapping[bytes, GitEntry],
    worktree: Mapping[bytes, FilesystemEntry],
) -> tuple[GitChange, ...]:
    result: list[GitChange] = []
    for path in sorted(index):
        expected = index[path]
        actual = worktree.get(path)
        if actual is None:
            result.append(
                GitChange(path=path, kind="deleted", index=expected)
            )
            continue
        if expected.mode == "160000":
            if actual.kind != "directory":
                result.append(
                    GitChange(
                        path=path,
                        kind="type",
                        index=expected,
                        worktree=actual,
                    )
                )
            continue
        kinds: list[str] = []
        if actual.git_mode != expected.mode:
            expected_type = "symlink" if expected.mode == "120000" else "regular"
            if actual.kind != expected_type:
                kinds.append("type")
            else:
                kinds.append("mode")
        if actual.git_oid != expected.oid:
            kinds.append("content")
        if kinds:
            result.append(
                GitChange(
                    path=path,
                    kind="+".join(dict.fromkeys(kinds)),
                    index=expected,
                    worktree=actual,
                )
            )
    return tuple(result)


def _normalize_expected_gitlinks(
    expected: Mapping[str | bytes | PurePosixPath, str | None] | None,
    limits: SnapshotLimits,
) -> dict[bytes, str | None]:
    if expected is None:
        return {}
    if len(expected) > limits.max_policy_entries:
        raise UnsupportedRepository("expected gitlink count limit exceeded")
    result: dict[bytes, str | None] = {}
    aggregate_path_bytes = 0
    for position, (path, oid) in enumerate(expected.items(), start=1):
        if position > limits.max_policy_entries:
            raise UnsupportedRepository(
                "expected gitlink iteration count limit exceeded"
            )
        normalized = _normalize_relative_path(path, limits)
        aggregate_path_bytes += len(normalized)
        if aggregate_path_bytes > limits.max_policy_path_bytes:
            raise UnsupportedRepository(
                "expected gitlink aggregate path bytes exceeded"
            )
        if normalized in result:
            raise ValueError(f"duplicate expected gitlink: {os.fsdecode(normalized)}")
        if oid is not None:
            if not isinstance(oid, str):
                raise ValueError("expected gitlink object name must be text")
            oid = oid.lower()
            if len(oid) not in set(_OID_LENGTH.values()) or any(
                character not in "0123456789abcdef" for character in oid
            ):
                raise ValueError(
                    "expected gitlink object name must be a full SHA-1/SHA-256 hex ID"
                )
        result[normalized] = oid
    return result


def _normalize_policy_roots(
    paths: Iterable[str | bytes | PurePosixPath] | None,
    *,
    description: str,
    limits: SnapshotLimits,
) -> tuple[bytes, ...]:
    if paths is None:
        return ()
    if isinstance(paths, (str, bytes, PurePosixPath)):
        raise ValueError(f"{description} must be a collection of paths")
    result: set[bytes] = set()
    aggregate_path_bytes = 0
    for position, path in enumerate(paths, start=1):
        if position > limits.max_policy_entries:
            raise UnsupportedRepository(f"{description} count limit exceeded")
        normalized = _normalize_relative_path(path, limits)
        aggregate_path_bytes += len(normalized)
        if aggregate_path_bytes > limits.max_policy_path_bytes:
            raise UnsupportedRepository(
                f"{description} aggregate path bytes exceeded"
            )
        result.add(normalized)
    return tuple(sorted(result))


def _path_overlap(
    root: bytes,
    *,
    candidates: Sequence[bytes],
    candidate_set: frozenset[bytes],
) -> bytes | None:
    if root in candidate_set:
        return root
    descendant_prefix = root + b"/"
    position = bisect_left(candidates, descendant_prefix)
    if position < len(candidates):
        candidate = candidates[position]
        if candidate.startswith(descendant_prefix):
            return candidate
    prefix = b""
    for part in root.split(b"/")[:-1]:
        prefix = part if not prefix else prefix + b"/" + part
        if prefix in candidate_set:
            return prefix
    return None


def _validate_allowed_untracked_roots(
    allowed: Sequence[bytes],
    protected_paths: Iterable[bytes],
    protected_roots: Sequence[bytes],
) -> None:
    allowed_set = frozenset(allowed)
    for root in allowed:
        prefix = b""
        for part in root.split(b"/")[:-1]:
            prefix = part if not prefix else prefix + b"/" + part
            if prefix in allowed_set:
                raise UnsupportedRepository(
                    "allowed untracked roots overlap each other: "
                    f"{os.fsdecode(prefix)} vs {os.fsdecode(root)}"
                )

    tracked_set = frozenset(protected_paths)
    tracked = tuple(sorted(tracked_set))
    policy_set = frozenset(protected_roots)
    policy = tuple(sorted(policy_set))
    for root in allowed:
        overlap = _path_overlap(
            root,
            candidates=tracked,
            candidate_set=tracked_set,
        )
        if overlap is not None:
            raise UnsupportedRepository(
                "allowed untracked root overlaps a HEAD/index path: "
                f"{os.fsdecode(root)} vs {os.fsdecode(overlap)}"
            )
        overlap = _path_overlap(
            root,
            candidates=policy,
            candidate_set=policy_set,
        )
        if overlap is not None:
            raise UnsupportedRepository(
                "allowed untracked root overlaps a protected root: "
                f"{os.fsdecode(root)} vs {os.fsdecode(overlap)}"
            )


def _incomplete_snapshot(
    worktree: Path,
    *,
    attempt: int,
    error: BaseException,
    layout: _Layout | None = None,
) -> RepositorySnapshot:
    return RepositorySnapshot(
        worktree=str(worktree),
        git_dir=str(layout.git_dir) if layout is not None else None,
        common_dir=str(layout.common_dir) if layout is not None else None,
        object_format=None,
        head_oid=None,
        index_sha256=None,
        complete=False,
        attempts=attempt,
        errors=(f"{type(error).__name__}: {error}",),
    )


def _capture_attempt(
    worktree: Path,
    *,
    git: Path,
    expected_gitlinks: Mapping[bytes, str | None],
    allowed_untracked_roots: tuple[bytes, ...],
    protected_roots: tuple[bytes, ...],
    allow_untracked_regular_files: bool,
    limits: SnapshotLimits,
    attempt: int,
    seen_worktrees: frozenset[Path],
) -> RepositorySnapshot:
    layout = _discover_layout(worktree, limits)
    if layout.worktree in seen_worktrees:
        raise UnsupportedRepository("recursive worktree/gitlink cycle")
    with tempfile.TemporaryDirectory(prefix="burstserve-git-provenance-") as temporary:
        temporary_root = Path(temporary)
        os.chmod(temporary_root, 0o700)
        source_config = temporary_root / "source.config"
        config_image = _read_regular_file(
            layout.config_file, limits.max_config_bytes, "repository config"
        )
        _copy_image(source_config, config_image)
        object_format = _read_repository_format(
            git, source_config, cwd=temporary_root, limits=limits
        )
        head_oid = _resolve_head(layout, object_format, limits)
        index_image = _read_regular_file(
            layout.index_file, limits.max_index_bytes, "Git index"
        )
        shared_images = _shared_index_images(layout, object_format, limits)
        token_before = _MetadataToken(
            layout=(
                str(layout.git_dir),
                str(layout.common_dir),
                str(layout.objects_dir),
            ),
            config_sha256=config_image.sha256,
            head_oid=head_oid,
            index_sha256=index_image.sha256,
            shared_indexes=tuple(
                (name, image.sha256) for name, image in shared_images
            ),
        )

        synthetic_git = temporary_root / "git"
        empty_worktree = temporary_root / "empty-worktree"
        synthetic_git.mkdir(mode=0o700)
        empty_worktree.mkdir(mode=0o700)
        (synthetic_git / "objects").mkdir(mode=0o700)
        (synthetic_git / "refs").mkdir(mode=0o700)
        _write_private(synthetic_git / "config", _synthetic_config(object_format))
        _write_private(synthetic_git / "HEAD", head_oid.encode("ascii") + b"\n")
        synthetic_index = synthetic_git / "index"
        _copy_image(synthetic_index, index_image)
        for name, image in shared_images:
            _copy_image(synthetic_git / name, image)

        environment = _plumbing_environment(
            synthetic_git,
            empty_worktree,
            layout.objects_dir,
            synthetic_index,
        )
        root_tree_oid = _commit_root_tree(
            git,
            cwd=empty_worktree,
            environment=environment,
            head_oid=head_oid,
            object_format=object_format,
            limits=limits,
        )
        tree_output = _run_git(
            git,
            [
                "ls-tree",
                "-r",
                "-t",
                "-z",
                "--full-tree",
                root_tree_oid,
            ],
            cwd=empty_worktree,
            environment=environment,
            limits=limits,
        )
        index_output = _run_git(
            git,
            ["ls-files", "--stage", "--sparse", "-z"],
            cwd=empty_worktree,
            environment=environment,
            limits=limits,
        )
        tree_listing = _parse_ls_tree(
            tree_output,
            object_format,
            limits,
        )
        head_entries = tree_listing.entries
        index_entries = _parse_ls_files(index_output, object_format, limits)
        object_check_before = _verify_required_objects(
            git,
            cwd=empty_worktree,
            environment=environment,
            head_oid=head_oid,
            root_tree_oid=root_tree_oid,
            tree_oids=tree_listing.tree_oids,
            head_entries=head_entries,
            index_entries=index_entries,
            object_format=object_format,
            limits=limits,
        )
        head_map = {entry.path: entry for entry in head_entries}
        index_map = _stage_zero_map(index_entries)

        registered_gitlinks = set(expected_gitlinks)
        actual_gitlinks = {
            entry.path for entry in index_entries if entry.mode == "160000"
        } | {entry.path for entry in head_entries if entry.mode == "160000"}
        unregistered = sorted(actual_gitlinks - registered_gitlinks)
        if unregistered:
            raise UnsupportedRepository(
                "unregistered gitlinks: "
                + ",".join(os.fsdecode(path) for path in unregistered)
            )
        missing_registered = sorted(registered_gitlinks - actual_gitlinks)
        if missing_registered:
            raise UnsupportedRepository(
                "expected gitlinks absent: "
                + ",".join(os.fsdecode(path) for path in missing_registered)
            )
        _validate_allowed_untracked_roots(
            allowed_untracked_roots,
            set(head_map) | set(index_map),
            protected_roots,
        )

        first_scan = _scan_worktree(
            layout.worktree,
            index_map,
            object_format,
            limits,
            frozenset(allowed_untracked_roots),
            allow_untracked_regular_files,
        )
        second_scan = _scan_worktree(
            layout.worktree,
            index_map,
            object_format,
            limits,
            frozenset(allowed_untracked_roots),
            allow_untracked_regular_files,
        )
        if first_scan != second_scan:
            raise UnstableRepository("working tree changed between A/B scans")
        root_tree_after = _commit_root_tree(
            git,
            cwd=empty_worktree,
            environment=environment,
            head_oid=head_oid,
            object_format=object_format,
            limits=limits,
        )
        tree_output_after = _run_git(
            git,
            [
                "ls-tree",
                "-r",
                "-t",
                "-z",
                "--full-tree",
                root_tree_after,
            ],
            cwd=empty_worktree,
            environment=environment,
            limits=limits,
        )
        tree_listing_after = _parse_ls_tree(
            tree_output_after,
            object_format,
            limits,
        )
        if (
            root_tree_after != root_tree_oid
            or tree_listing_after != tree_listing
        ):
            raise UnstableRepository(
                "HEAD commit/tree closure changed during worktree scan"
            )
        object_check_after = _verify_required_objects(
            git,
            cwd=empty_worktree,
            environment=environment,
            head_oid=head_oid,
            root_tree_oid=root_tree_after,
            tree_oids=tree_listing_after.tree_oids,
            head_entries=tree_listing_after.entries,
            index_entries=index_entries,
            object_format=object_format,
            limits=limits,
        )
        if object_check_after != object_check_before:
            raise UnstableRepository(
                "required Git object state changed during worktree scan"
            )
        tracked_map = {entry.path: entry for entry in first_scan.tracked}
        staged = _staged_changes(head_map, index_entries)
        unstaged = list(_unstaged_changes(index_map, tracked_map))

        gitlinks: list[GitlinkState] = []
        child_complete = True
        next_seen = seen_worktrees | {layout.worktree}
        for path in sorted(registered_gitlinks):
            head_entry = head_map.get(path)
            index_entry = index_map.get(path)
            if (
                head_entry is None
                or index_entry is None
                or head_entry.mode != "160000"
                or index_entry.mode != "160000"
            ):
                raise UnsupportedRepository(
                    f"expected path is not a HEAD/index gitlink: {os.fsdecode(path)}"
                )
            required_oid = expected_gitlinks[path]
            if required_oid is not None:
                _validate_oid(
                    required_oid.encode("ascii"),
                    object_format,
                    f"required gitlink {os.fsdecode(path)}",
                )
            child_root = layout.worktree.joinpath(
                *(os.fsdecode(part) for part in path.split(b"/"))
            )
            child = _capture_repository(
                child_root,
                git=git,
                expected_gitlinks={},
                allowed_untracked_roots=(),
                protected_roots=(),
                allow_untracked_regular_files=False,
                limits=limits,
                seen_worktrees=next_seen,
            )
            formats_match = child.object_format == object_format
            link = GitlinkState(
                path=path,
                recorded_oid=index_entry.oid,
                required_oid=required_oid,
                snapshot=child,
                object_format_matches=formats_match,
            )
            gitlinks.append(link)
            if not child.complete or not formats_match:
                child_complete = False
            actual = tracked_map.get(path)
            if (
                actual is not None
                and actual.kind == "directory"
                and child.head_oid is not None
                and child.head_oid != index_entry.oid
            ):
                unstaged.append(
                    GitChange(
                        path=path,
                        kind="gitlink-commit",
                        index=index_entry,
                        worktree=actual,
                    )
                )

        layout_after = _discover_layout(layout.worktree, limits)
        if layout_after != layout:
            raise UnstableRepository("Git administrative layout changed")
        token_after = _metadata_token(layout_after, object_format, limits)
        if token_after != token_before:
            raise UnstableRepository("Git HEAD/index/config changed during snapshot")

        errors: tuple[str, ...] = ()
        complete = child_complete
        if not child_complete:
            errors = ("one or more gitlink snapshots are incomplete",)
        return RepositorySnapshot(
            worktree=str(layout.worktree),
            git_dir=str(layout.git_dir),
            common_dir=str(layout.common_dir),
            object_format=object_format,
            head_oid=head_oid,
            index_sha256=index_image.sha256,
            head_entries=head_entries,
            index_entries=index_entries,
            worktree_entries=first_scan.tracked,
            staged_changes=staged,
            unstaged_changes=tuple(sorted(unstaged, key=lambda item: item.path)),
            untracked_entries=first_scan.untracked,
            allowed_untracked_roots=allowed_untracked_roots,
            protected_roots=protected_roots,
            allow_untracked_regular_files=(
                allow_untracked_regular_files
            ),
            gitlinks=tuple(gitlinks),
            complete=complete,
            attempts=attempt,
            errors=errors,
        )


def _capture_repository(
    worktree: Path,
    *,
    git: Path,
    expected_gitlinks: Mapping[bytes, str | None],
    allowed_untracked_roots: tuple[bytes, ...],
    protected_roots: tuple[bytes, ...],
    allow_untracked_regular_files: bool,
    limits: SnapshotLimits,
    seen_worktrees: frozenset[Path],
) -> RepositorySnapshot:
    last_error: BaseException | None = None
    last_layout: _Layout | None = None
    for attempt in range(1, limits.max_attempts + 1):
        try:
            last_layout = _discover_layout(worktree, limits)
            return _capture_attempt(
                worktree,
                git=git,
                expected_gitlinks=expected_gitlinks,
                allowed_untracked_roots=allowed_untracked_roots,
                protected_roots=protected_roots,
                allow_untracked_regular_files=(
                    allow_untracked_regular_files
                ),
                limits=limits,
                attempt=attempt,
                seen_worktrees=seen_worktrees,
            )
        except UnstableRepository as exc:
            last_error = exc
            continue
        except (
            GitProvenanceError,
            OSError,
            ValueError,
            RecursionError,
        ) as exc:
            return _incomplete_snapshot(
                worktree, attempt=attempt, error=exc, layout=last_layout
            )
    assert last_error is not None
    return _incomplete_snapshot(
        worktree,
        attempt=limits.max_attempts,
        error=last_error,
        layout=last_layout,
    )


def capture_repository(
    worktree: Path,
    *,
    expected_gitlinks: Mapping[
        str | bytes | PurePosixPath, str | None
    ] | None = None,
    allowed_untracked_roots: Iterable[
        str | bytes | PurePosixPath
    ] | None = None,
    protected_roots: Iterable[
        str | bytes | PurePosixPath
    ] | None = BURSTSERVE_FORMAL_PROTECTED_ROOTS,
    allow_untracked_regular_files: bool = False,
    git: Path = DEFAULT_GIT,
    limits: SnapshotLimits | None = None,
) -> RepositorySnapshot:
    """Capture HEAD, index, raw worktree, untracked, and fixed gitlink state.

    ``expected_gitlinks`` is deliberately explicit.  Every gitlink found in
    HEAD or the index must be registered, and each registered submodule is
    recursively inspected with the same isolation.  A mapping value pins the
    required submodule commit; ``None`` accepts the superproject's gitlink.

    ``allowed_untracked_roots`` deliberately hides every descendant from the
    snapshot.  It is only suitable for inert, independently controlled data
    or output.  Never allow a root reachable by Python/module import, dynamic
    loading, plugin/config discovery, compiler include/build inputs, or an
    executed artifact.  Allowed regular files are rejected by default and
    require the conspicuous ``allow_untracked_regular_files=True`` opt-in.

    Every allowed root must be disjoint from HEAD/index paths and from
    ``protected_roots``.  The default protected policy covers BurstServe code,
    native/build, manifest, script, test, and vendored-libsmctrl roots.  A
    formal caller must retain it or supply a reviewed superset covering every
    path reachable by its exact launch.  Exclusion/protection policy applies
    only to this repository; registered gitlinks are recursively strict.

    Any unsupported repository feature, resource-limit breach, command error,
    or unstable A/B observation produces ``complete == False`` and therefore
    can never produce ``clean == True``.
    """

    selected_limits = limits or SnapshotLimits()
    try:
        if not isinstance(allow_untracked_regular_files, bool):
            raise ValueError("allow_untracked_regular_files must be boolean")
        normalized_gitlinks = _normalize_expected_gitlinks(
            expected_gitlinks,
            selected_limits,
        )
        normalized_allowed = _normalize_policy_roots(
            allowed_untracked_roots,
            description="allowed untracked roots",
            limits=selected_limits,
        )
        normalized_protected = _normalize_policy_roots(
            protected_roots,
            description="protected roots",
            limits=selected_limits,
        )
        if normalized_allowed and not normalized_protected:
            raise UnsupportedRepository(
                "allowed untracked roots require a nonempty protected-root policy"
            )
        trusted_git = _validate_git_executable(git)
    except (
        GitProvenanceError,
        OSError,
        ValueError,
        RecursionError,
    ) as exc:
        return _incomplete_snapshot(worktree, attempt=1, error=exc)
    return _capture_repository(
        worktree,
        git=trusted_git,
        expected_gitlinks=normalized_gitlinks,
        allowed_untracked_roots=normalized_allowed,
        protected_roots=normalized_protected,
        allow_untracked_regular_files=allow_untracked_regular_files,
        limits=selected_limits,
        seen_worktrees=frozenset(),
    )


__all__ = [
    "BURSTSERVE_FORMAL_PROTECTED_ROOTS",
    "DEFAULT_GIT",
    "SCHEMA_VERSION",
    "FilesystemEntry",
    "GitChange",
    "GitEntry",
    "GitProvenanceError",
    "GitlinkState",
    "RepositorySnapshot",
    "SnapshotLimits",
    "UnsupportedRepository",
    "UnstableRepository",
    "capture_repository",
]
