"""Versioned experiment provenance and crash-safe JSON output.

This module deliberately depends only on the Python standard library so that
provenance can be captured before importing CUDA or machine-learning runtimes.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import socket
import sys
import tempfile
from typing import Any, Iterable, Iterator, Mapping

try:
    import fcntl
except ImportError:  # pragma: no cover - BurstServe currently targets Linux.
    fcntl = None  # type: ignore[assignment]


RUN_MANIFEST_SCHEMA_VERSION = "burstserve.run-manifest/v1"
EVENT_RECORD_SCHEMA_VERSION = "burstserve.event-record/v1"

_RUN_ID_NAMESPACE = "burstserve.run-id/v1"
_CUDA_ENVIRONMENT_FIELDS = (
    "CUDA_VISIBLE_DEVICES",
    "NVIDIA_VISIBLE_DEVICES",
    "CUDA_DEVICE_ORDER",
    "CUDA_HOME",
    "CUDA_PATH",
)
_JSON_SCALAR = str | int | float | bool | None
JsonValue = _JSON_SCALAR | list["JsonValue"] | dict[str, "JsonValue"]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _to_plain_json(value: Any) -> JsonValue:
    """Convert supported values to a detached, JSON-compatible value.

    Mapping keys must be strings. NaN and infinity are rejected by the final
    canonical encoder because they are not valid JSON and are not portable
    experiment parameters.
    """

    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        result: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"JSON object keys must be strings, got {type(key)!r}")
            result[key] = _to_plain_json(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_to_plain_json(item) for item in value]
    raise TypeError(f"value of type {type(value)!r} is not JSON serializable")


def canonical_json(value: Any) -> str:
    """Return the stable JSON representation used for IDs and persisted data."""

    return json.dumps(
        _to_plain_json(value),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _detached_json(value: Any) -> JsonValue:
    """Normalize and detach mutable input using the canonical representation."""

    return json.loads(canonical_json(value))


def derive_run_id(
    config: Mapping[str, Any],
    seed: int,
    source_revision: str,
) -> str:
    """Derive a stable run ID from semantic inputs.

    Host details and creation timestamps are intentionally excluded so that the
    same source, seed, and canonical configuration map to the same run across
    machines. The complete SHA-256 digest is retained to make the identifier
    suitable for experiment joins and artifact provenance.
    """

    if isinstance(seed, bool) or not isinstance(seed, int):
        raise TypeError("seed must be an integer")
    if not isinstance(source_revision, str) or not source_revision.strip():
        raise ValueError("source_revision must be a non-empty string")
    payload = {
        "namespace": _RUN_ID_NAMESPACE,
        "config": config,
        "seed": seed,
        "source_revision": source_revision,
    }
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return f"bs1-{digest}"


def collect_environment(
    extra_cuda_environment_fields: Iterable[str] = (),
) -> dict[str, JsonValue]:
    """Collect host/runtime context without importing Torch or probing a GPU."""

    names = list(_CUDA_ENVIRONMENT_FIELDS)
    for name in extra_cuda_environment_fields:
        if not isinstance(name, str) or not name:
            raise ValueError("extra CUDA environment field names must be non-empty")
        if name not in names:
            names.append(name)

    return {
        "host": {
            "hostname": socket.gethostname(),
        },
        "python": {
            "executable": sys.executable,
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
        },
        "platform": {
            "machine": platform.machine(),
            "platform": platform.platform(),
            "processor": platform.processor(),
            "release": platform.release(),
            "system": platform.system(),
        },
        "cuda_environment": {name: os.environ.get(name) for name in names},
    }


@dataclass(frozen=True, slots=True)
class RunManifest:
    """Immutable, versioned description of one semantic experiment run."""

    run_id: str
    created_at_utc: str
    seed: int
    source_revision: str
    config: dict[str, JsonValue]
    environment: dict[str, JsonValue]
    metadata: dict[str, JsonValue]
    schema_version: str = RUN_MANIFEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != RUN_MANIFEST_SCHEMA_VERSION:
            raise ValueError(f"unsupported run manifest schema: {self.schema_version}")
        normalized_config = _detached_json(self.config)
        normalized_environment = _detached_json(self.environment)
        normalized_metadata = _detached_json(self.metadata)
        if not isinstance(normalized_config, dict):
            raise TypeError("config must be a JSON object")
        if not isinstance(normalized_environment, dict):
            raise TypeError("environment must be a JSON object")
        if not isinstance(normalized_metadata, dict):
            raise TypeError("metadata must be a JSON object")
        object.__setattr__(self, "config", normalized_config)
        object.__setattr__(self, "environment", normalized_environment)
        object.__setattr__(self, "metadata", normalized_metadata)

        expected = derive_run_id(self.config, self.seed, self.source_revision)
        if self.run_id != expected:
            raise ValueError(f"run_id does not match manifest inputs: expected {expected}")
        if not isinstance(self.created_at_utc, str) or not self.created_at_utc:
            raise ValueError("created_at_utc must be a non-empty string")

    @classmethod
    def create(
        cls,
        *,
        config: Mapping[str, Any],
        seed: int,
        source_revision: str,
        environment: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        created_at_utc: str | None = None,
    ) -> "RunManifest":
        normalized_config = _detached_json(config)
        if not isinstance(normalized_config, dict):
            raise TypeError("config must be a JSON object")
        return cls(
            run_id=derive_run_id(normalized_config, seed, source_revision),
            created_at_utc=created_at_utc or _utc_now(),
            seed=seed,
            source_revision=source_revision,
            config=normalized_config,
            environment=dict(environment) if environment is not None else collect_environment(),
            metadata=dict(metadata) if metadata is not None else {},
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return _to_plain_json(asdict(self))  # type: ignore[return-value]

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RunManifest":
        return cls(**dict(value))


@dataclass(frozen=True, slots=True)
class EventRecord:
    """One ordered, versioned event associated with a run manifest."""

    run_id: str
    sequence: int
    timestamp_utc: str
    event_type: str
    payload: dict[str, JsonValue]
    schema_version: str = EVENT_RECORD_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != EVENT_RECORD_SCHEMA_VERSION:
            raise ValueError(f"unsupported event record schema: {self.schema_version}")
        if not isinstance(self.run_id, str) or not self.run_id:
            raise ValueError("run_id must be a non-empty string")
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int):
            raise TypeError("sequence must be an integer")
        if self.sequence < 0:
            raise ValueError("sequence must be non-negative")
        if not isinstance(self.timestamp_utc, str) or not self.timestamp_utc:
            raise ValueError("timestamp_utc must be a non-empty string")
        if not isinstance(self.event_type, str) or not self.event_type:
            raise ValueError("event_type must be a non-empty string")
        normalized_payload = _detached_json(self.payload)
        if not isinstance(normalized_payload, dict):
            raise TypeError("payload must be a JSON object")
        object.__setattr__(self, "payload", normalized_payload)

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        sequence: int,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        timestamp_utc: str | None = None,
    ) -> "EventRecord":
        return cls(
            run_id=run_id,
            sequence=sequence,
            timestamp_utc=timestamp_utc or _utc_now(),
            event_type=event_type,
            payload=dict(payload) if payload is not None else {},
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return _to_plain_json(asdict(self))  # type: ignore[return-value]

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EventRecord":
        return cls(**dict(value))


def _json_payload(value: Any) -> bytes:
    return (canonical_json(value) + "\n").encode("utf-8")


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    # Directory durability is part of the atomic-write contract.  A rename
    # whose containing directory could not be opened/fsynced must never be
    # reported as durable to callers that may release safety poison.
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_bytes_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
        _fsync_directory(path.parent)
    except BaseException:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise


def write_json_atomic(path: str | os.PathLike[str], value: Any) -> None:
    """Atomically replace *path* with one canonical JSON document."""

    _write_bytes_atomic(Path(path), _json_payload(value))


def write_text_atomic(path: str | os.PathLike[str], value: str) -> None:
    """Atomically replace *path* with UTF-8 text.

    Callers must provide their desired trailing newline explicitly.  This is
    useful for machine-generated lock and configuration files that are not
    JSON, while retaining the same crash-safety guarantees as experiment
    provenance.
    """

    if not isinstance(value, str):
        raise TypeError("value must be a string")
    _write_bytes_atomic(Path(path), value.encode("utf-8"))


def write_jsonl_atomic(
    path: str | os.PathLike[str],
    records: Iterable[Any],
) -> None:
    """Atomically replace *path* with canonical, newline-terminated JSONL."""

    payload = b"".join(_json_payload(record) for record in records)
    _write_bytes_atomic(Path(path), payload)


@contextmanager
def _sidecar_lock(path: Path) -> Iterator[None]:
    if fcntl is None:
        raise RuntimeError("atomic JSONL append requires POSIX fcntl")
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    with lock_path.open("a+b") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def append_jsonl_atomic(path: str | os.PathLike[str], record: Any) -> None:
    """Crash-safely append one record using a locked atomic file replacement.

    This intentionally rewrites the file. It is suitable for low-rate
    provenance events; high-frequency scheduler telemetry should be buffered
    and committed with :func:`write_jsonl_atomic` in bounded chunks.
    """

    destination = Path(path)
    line = _json_payload(record)
    with _sidecar_lock(destination):
        existing = destination.read_bytes() if destination.exists() else b""
        if existing and not existing.endswith(b"\n"):
            raise ValueError(f"existing JSONL file is not newline terminated: {destination}")
        _write_bytes_atomic(destination, existing + line)


def read_json(path: str | os.PathLike[str]) -> JsonValue:
    with Path(path).open("r", encoding="utf-8") as source:
        return json.load(source)


def read_jsonl(path: str | os.PathLike[str]) -> list[JsonValue]:
    records: list[JsonValue] = []
    with Path(path).open("r", encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                raise ValueError(f"blank line at {path}:{line_number}")
            records.append(json.loads(line))
    return records
