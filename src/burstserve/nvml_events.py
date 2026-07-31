"""Fail-closed NVML Xid event monitoring for risky GPU experiments.

The monitor intentionally uses only :mod:`ctypes`.  Registration happens in
``__enter__`` so a caller can establish monitoring before launching a child:

    monitor = NvmlXidMonitor("GPU-...")
    with monitor:
        launch_and_wait_for_child()
        monitor.drain(timeout_ms=1000)
    assert monitor.safe_for_acceptance

``safe_for_acceptance`` is deliberately false until the context has closed.
This prevents a masked run from being accepted when setup, draining, or
cleanup was skipped or failed.
"""

from __future__ import annotations

import ctypes
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import math
import os
from pathlib import Path
import stat
import time
from typing import Any, Callable, Mapping, Sequence


NVML_SUCCESS = 0
NVML_ERROR_NOT_SUPPORTED = 3
NVML_ERROR_NO_PERMISSION = 4
NVML_ERROR_TIMEOUT = 10
NVML_ERROR_LIBRARY_NOT_FOUND = 12
NVML_ERROR_FUNCTION_NOT_FOUND = 13

NVML_EVENT_TYPE_XID_CRITICAL_ERROR = 0x0000000000000008
NVML_INSTANCE_ID_NONE = 0xFFFFFFFF
NVML_SYSTEM_NVML_VERSION_BUFFER_SIZE = 80
PROVENANCE_SCHEMA_VERSION = "burstserve.nvml-xid-monitor/v2"
MAX_NVML_LIBRARY_BYTES = 256 * 1024 * 1024
_F_ADD_SEALS = int(getattr(fcntl, "F_ADD_SEALS", 1033))
_F_GET_SEALS = int(getattr(fcntl, "F_GET_SEALS", 1034))

_REQUIRED_SYMBOLS = {
    "init": "nvmlInit_v2",
    "shutdown": "nvmlShutdown",
    "system_get_nvml_version": "nvmlSystemGetNVMLVersion",
    "device_get_handle_by_uuid": "nvmlDeviceGetHandleByUUID_v2",
    "event_set_create": "nvmlEventSetCreate",
    "event_set_free": "nvmlEventSetFree",
    "device_get_supported_event_types": "nvmlDeviceGetSupportedEventTypes",
    "device_register_events": "nvmlDeviceRegisterEvents",
    "event_set_wait_v2": "nvmlEventSetWait_v2",
}

_RETURN_NAMES = {
    NVML_SUCCESS: "NVML_SUCCESS",
    1: "NVML_ERROR_UNINITIALIZED",
    2: "NVML_ERROR_INVALID_ARGUMENT",
    NVML_ERROR_NOT_SUPPORTED: "NVML_ERROR_NOT_SUPPORTED",
    NVML_ERROR_NO_PERMISSION: "NVML_ERROR_NO_PERMISSION",
    5: "NVML_ERROR_ALREADY_INITIALIZED",
    6: "NVML_ERROR_NOT_FOUND",
    7: "NVML_ERROR_INSUFFICIENT_SIZE",
    8: "NVML_ERROR_INSUFFICIENT_POWER",
    9: "NVML_ERROR_DRIVER_NOT_LOADED",
    NVML_ERROR_TIMEOUT: "NVML_ERROR_TIMEOUT",
    11: "NVML_ERROR_IRQ_ISSUE",
    NVML_ERROR_LIBRARY_NOT_FOUND: "NVML_ERROR_LIBRARY_NOT_FOUND",
    NVML_ERROR_FUNCTION_NOT_FOUND: "NVML_ERROR_FUNCTION_NOT_FOUND",
    14: "NVML_ERROR_CORRUPTED_INFOROM",
    15: "NVML_ERROR_GPU_IS_LOST",
    16: "NVML_ERROR_RESET_REQUIRED",
    17: "NVML_ERROR_OPERATING_SYSTEM",
    18: "NVML_ERROR_LIB_RM_VERSION_MISMATCH",
    19: "NVML_ERROR_IN_USE",
    20: "NVML_ERROR_MEMORY",
    21: "NVML_ERROR_NO_DATA",
    23: "NVML_ERROR_INSUFFICIENT_RESOURCES",
    29: "NVML_ERROR_INVALID_STATE",
    999: "NVML_ERROR_UNKNOWN",
}


class NvmlEventData(ctypes.Structure):
    """Exact ``nvmlEventData_t`` layout from the installed CUDA 13.3 nvml.h."""

    _fields_ = [
        ("device", ctypes.c_void_p),
        ("eventType", ctypes.c_ulonglong),
        ("eventData", ctypes.c_ulonglong),
        ("gpuInstanceId", ctypes.c_uint),
        ("computeInstanceId", ctypes.c_uint),
    ]


class NvmlMonitorError(RuntimeError):
    """Base class for categorized, serializable monitor failures."""

    category = "nvml"

    def __init__(
        self,
        operation: str,
        message: str,
        *,
        code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.operation = operation
        self.code = code

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "operation": self.operation,
            "code": self.code,
            "return_name": _RETURN_NAMES.get(self.code) if self.code is not None else None,
            "message": str(self),
        }


class NvmlUnsupportedError(NvmlMonitorError):
    category = "unsupported"


class NvmlPermissionError(NvmlMonitorError):
    category = "permission"


class NvmlLibraryError(NvmlMonitorError):
    category = "library"


class NvmlCallError(NvmlMonitorError):
    category = "nvml"


class NvmlProtocolError(NvmlMonitorError):
    category = "protocol"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _return_error(operation: str, code: int) -> NvmlMonitorError:
    name = _RETURN_NAMES.get(code, f"NVML_RETURN_{code}")
    message = f"{operation} failed with {name} ({code})"
    if code == NVML_ERROR_NOT_SUPPORTED:
        return NvmlUnsupportedError(operation, message, code=code)
    if code == NVML_ERROR_NO_PERMISSION:
        return NvmlPermissionError(operation, message, code=code)
    if code in (NVML_ERROR_LIBRARY_NOT_FOUND, NVML_ERROR_FUNCTION_NOT_FOUND):
        return NvmlLibraryError(operation, message, code=code)
    return NvmlCallError(operation, message, code=code)


def _check_return(operation: str, value: Any) -> None:
    code = int(value)
    if code != NVML_SUCCESS:
        raise _return_error(operation, code)


def _sha256_fd(descriptor: int, size: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while offset < size:
        chunk = os.pread(
            descriptor,
            min(1024 * 1024, size - offset),
            offset,
        )
        if not chunk:
            raise NvmlLibraryError(
                "hash_snapshot",
                "sealed NVML snapshot ended before its recorded size",
            )
        digest.update(chunk)
        offset += len(chunk)
    if os.pread(descriptor, 1, size):
        raise NvmlLibraryError(
            "hash_snapshot",
            "sealed NVML snapshot exceeds its recorded size",
        )
    return digest.hexdigest()


def _memfd_create(name: str, flags: int) -> int:
    """Create a memfd even when the Python build omits ``os.memfd_create``."""

    python_memfd_create = getattr(os, "memfd_create", None)
    if python_memfd_create is not None:
        return int(python_memfd_create(name, flags))
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc_memfd_create = libc.memfd_create
    except (AttributeError, OSError) as error:
        raise OSError(
            errno.ENOSYS,
            "memfd_create is unavailable in Python and the process libc",
        ) from error
    libc_memfd_create.argtypes = [ctypes.c_char_p, ctypes.c_uint]
    libc_memfd_create.restype = ctypes.c_int
    encoded_name = name.encode("ascii", errors="strict")
    descriptor = int(libc_memfd_create(encoded_name, int(flags)))
    if descriptor < 0:
        error_number = ctypes.get_errno() or errno.EIO
        raise OSError(
            error_number,
            os.strerror(error_number),
        )
    return descriptor


def _sealed_library_snapshot(
    path: Path,
    *,
    expected_sha256: str,
) -> tuple[int, str, dict[str, int], dict[str, Any]]:
    """Copy a trusted source inode into a rehashed, sealed executable memfd."""

    if not path.is_absolute():
        raise ValueError("NVML library path must be absolute")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise NvmlLibraryError(
            "open_library",
            f"could not securely open {path}: {type(error).__name__}: {error}",
        ) from error
    snapshot_descriptor: int | None = None
    result: tuple[int, str, dict[str, int], dict[str, Any]] | None = None
    failure: BaseException | None = None
    failure_operation = "verify_library_metadata"
    operation = failure_operation
    try:
        operation = "verify_library_metadata"
        source_before = os.fstat(descriptor)
        if not stat.S_ISREG(source_before.st_mode):
            raise NvmlLibraryError(
                "open_library",
                f"NVML library is not a regular file: {path}",
            )
        if source_before.st_uid not in {0, os.geteuid()}:
            raise NvmlLibraryError(
                "verify_library_metadata",
                "NVML library owner is neither root nor the effective user",
            )
        if stat.S_IMODE(source_before.st_mode) & 0o022:
            raise NvmlLibraryError(
                "verify_library_metadata",
                "NVML library is group/world writable",
            )
        if source_before.st_nlink != 1:
            raise NvmlLibraryError(
                "verify_library_metadata",
                "NVML library must have exactly one hard link",
            )
        if not 0 < source_before.st_size <= MAX_NVML_LIBRARY_BYTES:
            raise NvmlLibraryError(
                "verify_library_metadata",
                "NVML library size is zero or exceeds the snapshot limit",
            )

        base_flags = int(getattr(os, "MFD_CLOEXEC", 0x0001)) | int(
            getattr(os, "MFD_ALLOW_SEALING", 0x0002)
        )
        exec_flag = int(getattr(os, "MFD_EXEC", 0x0010))
        mfd_exec_used = True
        operation = "create_snapshot"
        try:
            snapshot_descriptor = _memfd_create(
                "burstserve-nvml-snapshot",
                base_flags | exec_flag,
            )
        except OSError as error:
            if error.errno != errno.EINVAL:
                raise
            snapshot_descriptor = _memfd_create(
                "burstserve-nvml-snapshot",
                base_flags,
            )
            mfd_exec_used = False

        digest = hashlib.sha256()
        offset = 0
        operation = "copy_snapshot"
        while offset < source_before.st_size:
            chunk = os.pread(
                descriptor,
                min(1024 * 1024, source_before.st_size - offset),
                offset,
            )
            if not chunk:
                raise NvmlLibraryError(
                    "copy_snapshot",
                    "NVML source ended during immutable snapshot copy",
                )
            digest.update(chunk)
            written_offset = 0
            while written_offset < len(chunk):
                written = os.write(
                    snapshot_descriptor,
                    chunk[written_offset:],
                )
                if written <= 0:
                    raise NvmlLibraryError(
                        "copy_snapshot",
                        "short write to NVML immutable snapshot",
                    )
                written_offset += written
            offset += len(chunk)
        source_digest = digest.hexdigest()
        if source_digest != expected_sha256:
            raise NvmlLibraryError(
                "verify_library_hash",
                "NVML library SHA-256 does not match the manifest pin",
            )
        operation = "verify_library_stability"
        source_after = os.fstat(descriptor)
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_gid",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if any(
            getattr(source_before, field) != getattr(source_after, field)
            for field in stable_fields
        ):
            raise NvmlLibraryError(
                "verify_library_stability",
                "NVML source inode changed during snapshot capture",
            )

        operation = "prepare_snapshot_permissions"
        os.fchmod(snapshot_descriptor, 0o500)
        required_seals = (
            int(getattr(fcntl, "F_SEAL_WRITE", 0x0008))
            | int(getattr(fcntl, "F_SEAL_GROW", 0x0004))
            | int(getattr(fcntl, "F_SEAL_SHRINK", 0x0002))
            | int(getattr(fcntl, "F_SEAL_SEAL", 0x0001))
        )
        exec_seal = int(getattr(fcntl, "F_SEAL_EXEC", 0x0020))
        exec_seal_applied = True
        operation = "seal_snapshot"
        try:
            fcntl.fcntl(
                snapshot_descriptor,
                _F_ADD_SEALS,
                required_seals | exec_seal,
            )
        except OSError as error:
            if error.errno != errno.EINVAL:
                raise
            exec_seal_applied = False
            fcntl.fcntl(
                snapshot_descriptor,
                _F_ADD_SEALS,
                required_seals,
            )
        operation = "verify_sealed_snapshot"
        observed_seals = int(
            fcntl.fcntl(snapshot_descriptor, _F_GET_SEALS)
        )
        snapshot_stat = os.fstat(snapshot_descriptor)
        snapshot_digest = _sha256_fd(
            snapshot_descriptor,
            int(source_before.st_size),
        )
        if (
            snapshot_digest != expected_sha256
            or snapshot_stat.st_size != source_before.st_size
            or stat.S_IMODE(snapshot_stat.st_mode) != 0o500
            or observed_seals & required_seals != required_seals
            or (
                exec_seal_applied
                and observed_seals & exec_seal != exec_seal
            )
        ):
            raise NvmlLibraryError(
                "verify_sealed_snapshot",
                "sealed NVML snapshot failed post-seal verification",
            )

        source_record = {
            "device": int(source_before.st_dev),
            "inode": int(source_before.st_ino),
            "mode": int(source_before.st_mode),
            "uid": int(source_before.st_uid),
            "gid": int(source_before.st_gid),
            "nlink": int(source_before.st_nlink),
            "size": int(source_before.st_size),
            "mtime_ns": int(source_before.st_mtime_ns),
            "ctime_ns": int(source_before.st_ctime_ns),
        }
        snapshot_record = {
            "device": int(snapshot_stat.st_dev),
            "inode": int(snapshot_stat.st_ino),
            "mode": int(snapshot_stat.st_mode),
            "size": int(snapshot_stat.st_size),
            "sha256": snapshot_digest,
            "seals": observed_seals,
            "required_seals": required_seals,
            "exec_seal": exec_seal,
            "exec_seal_applied": exec_seal_applied,
            "mfd_exec_used": mfd_exec_used,
            "copy_limit_bytes": MAX_NVML_LIBRARY_BYTES,
        }
        result = (
            snapshot_descriptor,
            snapshot_digest,
            source_record,
            snapshot_record,
        )
    except BaseException as error:
        failure = error
        failure_operation = operation

    try:
        os.close(descriptor)
    except BaseException as error:
        if failure is None or (
            isinstance(failure, Exception)
            and not isinstance(error, Exception)
        ):
            failure = error
            failure_operation = "close_library"

    if result is None or failure is not None:
        if snapshot_descriptor is not None:
            try:
                os.close(snapshot_descriptor)
            except BaseException as error:
                if failure is None or (
                    isinstance(failure, Exception)
                    and not isinstance(error, Exception)
                ):
                    failure = error
                    failure_operation = "close_snapshot"

    if failure is not None:
        if isinstance(failure, OSError):
            wrapped = NvmlLibraryError(
                failure_operation,
                "immutable NVML snapshot operation failed: "
                f"{type(failure).__name__}: {failure}",
            )
            raise wrapped from failure
        raise failure.with_traceback(failure.__traceback__)

    if result is None:  # pragma: no cover - defensive invariant.
        raise NvmlLibraryError(
            "create_snapshot",
            "immutable NVML snapshot completed without a result",
        )
    return result


class NvmlXidMonitor:
    """Register and drain Xid events for one physical GPU UUID.

    A setup failure raises a categorized :class:`NvmlMonitorError`, ensuring a
    caller does not accidentally launch a risky child without monitoring.  A
    drain failure also raises and permanently makes the run unacceptable.
    Cleanup errors are recorded instead of masking an exception from the child.
    """

    def __init__(
        self,
        physical_uuid: str,
        *,
        library_path: str | Path,
        expected_library_sha256: str,
        expected_library_version: str,
        library: Any | None = None,
        library_loader: Callable[[str], Any] | None = None,
        now: Callable[[], str] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(physical_uuid, str) or not physical_uuid.startswith("GPU-"):
            raise ValueError("physical_uuid must be an NVML physical UUID starting with 'GPU-'")
        if "\x00" in physical_uuid:
            raise ValueError("physical_uuid must not contain NUL")
        try:
            physical_uuid.encode("ascii")
        except UnicodeEncodeError as error:
            raise ValueError("physical_uuid must be ASCII") from error
        path = Path(library_path)
        if not path.is_absolute():
            raise ValueError("library_path must be absolute")
        if (
            not isinstance(expected_library_sha256, str)
            or len(expected_library_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in expected_library_sha256
            )
        ):
            raise ValueError(
                "expected_library_sha256 must be a lowercase SHA-256 digest"
            )
        if (
            not isinstance(expected_library_version, str)
            or not expected_library_version
            or "\x00" in expected_library_version
        ):
            raise ValueError("expected_library_version must be non-empty")

        self.physical_uuid = physical_uuid
        self.library_path = path
        self.expected_library_sha256 = expected_library_sha256
        self.expected_library_version = expected_library_version
        self._library = library
        self._library_loader = library_loader
        self._now = now
        self._monotonic = monotonic

        self._functions: dict[str, Any] = {}
        self._symbols: dict[str, str] = {}
        self._device = ctypes.c_void_p()
        self._event_set = ctypes.c_void_p()
        self._snapshot_descriptor: int | None = None
        self._library_load_path: str | None = None
        self._initialized = False
        self._entered = False
        self._closed = False
        self._last_provenance: dict[str, Any] | None = None

        self.setup_started_at_utc: str | None = None
        self.setup_completed_at_utc: str | None = None
        self.setup_started_monotonic_s: float | None = None
        self.setup_completed_monotonic_s: float | None = None
        self.setup_succeeded = False
        self.setup_error: dict[str, Any] | None = None
        self.drain_started_at_utc: str | None = None
        self.drain_completed_at_utc: str | None = None
        self.drain_started_monotonic_s: float | None = None
        self.drain_completed_monotonic_s: float | None = None
        self.quiet_started_monotonic_s: float | None = None
        self.quiet_completed_monotonic_s: float | None = None
        self.drain_timeout_ms: int | None = None
        self.requested_quiet_ms: int | None = None
        self.observed_quiet_ms: float | None = None
        self.drain_wait_calls = 0
        self.drain_final_poll_calls = 0
        self.drain_maximum_total_ms: int | None = None
        self.drain_fail_fast_on_event = False
        self.drain_succeeded = False
        self.drain_error: dict[str, Any] | None = None
        self.cleanup_completed_at_utc: str | None = None
        self.cleanup_completed_monotonic_s: float | None = None
        self.cleanup_errors: list[dict[str, Any]] = []
        self.library_sha256: str | None = None
        self.library_identity: dict[str, int] | None = None
        self.library_snapshot: dict[str, Any] | None = None
        self.library_version: str | None = None
        self.supported_event_bits: int | None = None
        self.registered_event_bits = 0
        self.events: list[dict[str, Any]] = []
        self._last_monotonic_s: float | None = None

    def _load_library(self) -> Any:
        if self._library is not None:
            return self._library
        if self._snapshot_descriptor is None:
            raise NvmlLibraryError(
                "load_library",
                "sealed NVML snapshot descriptor is unavailable",
            )
        load_path = f"/proc/self/fd/{self._snapshot_descriptor}"
        self._library_load_path = load_path
        loader = self._library_loader
        try:
            if loader is None:
                self._library = ctypes.CDLL(
                    load_path,
                    use_errno=True,
                )
            else:
                self._library = loader(load_path)
        except (OSError, AttributeError) as error:
            raise NvmlLibraryError(
                "load_library",
                f"could not load {self.library_path}: "
                f"{type(error).__name__}: {error}",
            ) from error
        return self._library

    def _monotonic_now(self, operation: str) -> float:
        value = float(self._monotonic())
        if not math.isfinite(value) or value < 0:
            raise NvmlProtocolError(
                operation,
                f"monotonic clock returned invalid value {value!r}",
            )
        if (
            self._last_monotonic_s is not None
            and value < self._last_monotonic_s
        ):
            raise NvmlProtocolError(
                operation,
                "monotonic clock regressed from "
                f"{self._last_monotonic_s!r} to {value!r}",
            )
        self._last_monotonic_s = value
        return value

    def _bind(
        self,
        logical_name: str,
        symbol: str,
        argtypes: Sequence[Any],
    ) -> None:
        library = self._load_library()
        try:
            function = getattr(library, symbol)
        except AttributeError:
            raise NvmlLibraryError(
                "bind_symbol",
                f"NVML is missing exact required symbol {symbol}",
                code=NVML_ERROR_FUNCTION_NOT_FOUND,
            )
        try:
            function.argtypes = list(argtypes)
            function.restype = ctypes.c_int
        except (AttributeError, TypeError) as error:
            raise NvmlLibraryError(
                "bind_symbol",
                f"could not declare ABI for {symbol}: "
                f"{type(error).__name__}: {error}",
            ) from error
        self._functions[logical_name] = function
        self._symbols[logical_name] = symbol

    def _bind_required_symbols(self) -> None:
        device_pointer = ctypes.POINTER(ctypes.c_void_p)
        event_set_pointer = ctypes.POINTER(ctypes.c_void_p)
        event_bits_pointer = ctypes.POINTER(ctypes.c_ulonglong)
        event_data_pointer = ctypes.POINTER(NvmlEventData)

        self._bind("init", _REQUIRED_SYMBOLS["init"], ())
        self._bind("shutdown", _REQUIRED_SYMBOLS["shutdown"], ())
        self._bind(
            "system_get_nvml_version",
            _REQUIRED_SYMBOLS["system_get_nvml_version"],
            (ctypes.c_char_p, ctypes.c_uint),
        )
        self._bind(
            "device_get_handle_by_uuid",
            _REQUIRED_SYMBOLS["device_get_handle_by_uuid"],
            (ctypes.c_char_p, device_pointer),
        )
        self._bind(
            "event_set_create",
            _REQUIRED_SYMBOLS["event_set_create"],
            (event_set_pointer,),
        )
        self._bind(
            "event_set_free",
            _REQUIRED_SYMBOLS["event_set_free"],
            (ctypes.c_void_p,),
        )
        self._bind(
            "device_get_supported_event_types",
            _REQUIRED_SYMBOLS["device_get_supported_event_types"],
            (ctypes.c_void_p, event_bits_pointer),
        )
        self._bind(
            "device_register_events",
            _REQUIRED_SYMBOLS["device_register_events"],
            (ctypes.c_void_p, ctypes.c_ulonglong, ctypes.c_void_p),
        )
        self._bind(
            "event_set_wait_v2",
            _REQUIRED_SYMBOLS["event_set_wait_v2"],
            (ctypes.c_void_p, event_data_pointer, ctypes.c_uint),
        )

    def open(self) -> "NvmlXidMonitor":
        if self._entered:
            raise RuntimeError("NvmlXidMonitor instances are single-use")
        self._entered = True
        self.setup_started_at_utc = self._now()
        self.setup_started_monotonic_s = self._monotonic_now("setup_start")
        try:
            (
                self._snapshot_descriptor,
                library_sha256,
                source_identity,
                snapshot_identity,
            ) = _sealed_library_snapshot(
                self.library_path,
                expected_sha256=self.expected_library_sha256,
            )
            self.library_sha256 = library_sha256
            self.library_identity = source_identity
            self.library_snapshot = snapshot_identity
            self._library_load_path = (
                f"/proc/self/fd/{self._snapshot_descriptor}"
            )
            self._bind_required_symbols()
            _check_return("nvmlInit_v2", self._functions["init"]())
            self._initialized = True

            version_buffer = ctypes.create_string_buffer(
                NVML_SYSTEM_NVML_VERSION_BUFFER_SIZE
            )
            _check_return(
                _REQUIRED_SYMBOLS["system_get_nvml_version"],
                self._functions["system_get_nvml_version"](
                    version_buffer,
                    ctypes.c_uint(len(version_buffer)),
                ),
            )
            try:
                self.library_version = version_buffer.value.decode(
                    "ascii",
                    errors="strict",
                )
            except UnicodeDecodeError as error:
                raise NvmlProtocolError(
                    _REQUIRED_SYMBOLS["system_get_nvml_version"],
                    "NVML version is not ASCII",
                ) from error
            if self.library_version != self.expected_library_version:
                raise NvmlLibraryError(
                    "verify_library_version",
                    "NVML runtime version does not match the manifest pin: "
                    f"{self.library_version!r} != "
                    f"{self.expected_library_version!r}",
                )

            _check_return(
                self._symbols["device_get_handle_by_uuid"],
                self._functions["device_get_handle_by_uuid"](
                    self.physical_uuid.encode("ascii"),
                    ctypes.byref(self._device),
                ),
            )
            if not self._device.value:
                raise NvmlProtocolError(
                    self._symbols["device_get_handle_by_uuid"],
                    "NVML returned a null device handle",
                )

            supported = ctypes.c_ulonglong()
            _check_return(
                "nvmlDeviceGetSupportedEventTypes",
                self._functions["device_get_supported_event_types"](
                    self._device,
                    ctypes.byref(supported),
                ),
            )
            self.supported_event_bits = int(supported.value)
            if not (self.supported_event_bits & NVML_EVENT_TYPE_XID_CRITICAL_ERROR):
                raise NvmlUnsupportedError(
                    "xid_support",
                    "physical GPU does not advertise nvmlEventTypeXidCriticalError",
                    code=NVML_ERROR_NOT_SUPPORTED,
                )

            _check_return(
                "nvmlEventSetCreate",
                self._functions["event_set_create"](ctypes.byref(self._event_set)),
            )
            if not self._event_set.value:
                raise NvmlProtocolError(
                    "nvmlEventSetCreate", "NVML returned a null event-set handle"
                )
            _check_return(
                "nvmlDeviceRegisterEvents",
                self._functions["device_register_events"](
                    self._device,
                    ctypes.c_ulonglong(NVML_EVENT_TYPE_XID_CRITICAL_ERROR),
                    self._event_set,
                ),
            )
            self.registered_event_bits = NVML_EVENT_TYPE_XID_CRITICAL_ERROR
            self.setup_succeeded = True
            self.setup_completed_at_utc = self._now()
            self.setup_completed_monotonic_s = self._monotonic_now(
                "setup_complete"
            )
            self._cache_provenance()
            return self
        except NvmlMonitorError as error:
            self.setup_error = self._error_record(error)
            self.setup_completed_at_utc = self._now()
            self.setup_completed_monotonic_s = self._monotonic_now(
                "setup_error"
            )
            try:
                self.close()
            except BaseException as cleanup_error:
                if not isinstance(cleanup_error, Exception):
                    raise
            raise
        except BaseException as error:
            wrapped = NvmlProtocolError(
                "setup",
                f"unexpected setup failure: {type(error).__name__}: {error}",
            )
            self.setup_error = self._error_record(wrapped)
            self.setup_completed_at_utc = self._now()
            self.setup_completed_monotonic_s = self._monotonic_now(
                "setup_error"
            )
            try:
                self.close()
            except BaseException as cleanup_error:
                if (
                    isinstance(error, Exception)
                    and not isinstance(cleanup_error, Exception)
                ):
                    raise
            if not isinstance(error, Exception):
                raise
            raise wrapped from error

    def __enter__(self) -> "NvmlXidMonitor":
        return self.open()

    def _error_record(self, error: NvmlMonitorError) -> dict[str, Any]:
        return {"timestamp_utc": self._now(), **error.to_dict()}

    def drain(
        self,
        timeout_ms: int,
        *,
        max_events: int = 1024,
        maximum_total_ms: int | None = None,
        fail_fast_on_event: bool = False,
    ) -> list[dict[str, Any]]:
        """Drain Xids through a quiet interval and a final zero-time poll."""

        if not self.setup_succeeded or self._closed:
            raise RuntimeError("monitor must be open with successful setup before drain")
        if self.drain_started_at_utc is not None:
            raise RuntimeError("drain may only be called once")
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int):
            raise TypeError("timeout_ms must be an integer")
        if not 0 < timeout_ms <= 0xFFFFFFFF:
            raise ValueError("timeout_ms must be positive and fit unsigned int")
        if isinstance(max_events, bool) or not isinstance(max_events, int):
            raise TypeError("max_events must be an integer")
        if max_events <= 0:
            raise ValueError("max_events must be positive")
        if (
            maximum_total_ms is not None
            and (
                isinstance(maximum_total_ms, bool)
                or not isinstance(maximum_total_ms, int)
            )
        ):
            raise TypeError("maximum_total_ms must be an integer or None")
        if (
            maximum_total_ms is not None
            and not 0 < maximum_total_ms <= 0xFFFFFFFF
        ):
            raise ValueError(
                "maximum_total_ms must be positive and fit unsigned int"
            )
        if not isinstance(fail_fast_on_event, bool):
            raise TypeError("fail_fast_on_event must be a boolean")

        self.drain_started_at_utc = self._now()
        self.drain_started_monotonic_s = self._monotonic_now("drain_start")
        self.drain_timeout_ms = timeout_ms
        self.requested_quiet_ms = timeout_ms
        self.drain_maximum_total_ms = maximum_total_ms
        self.drain_fail_fast_on_event = fail_fast_on_event
        quiet_seconds = timeout_ms / 1000.0
        quiet_started = self._monotonic_now("quiet_start")
        self.quiet_started_monotonic_s = quiet_started
        quiet_deadline = quiet_started + quiet_seconds
        total_deadline = (
            self.drain_started_monotonic_s
            + maximum_total_ms / 1000.0
            if maximum_total_ms is not None
            else None
        )

        def wait_once(wait_ms: int) -> tuple[int, NvmlEventData]:
            event_data = NvmlEventData()
            self.drain_wait_calls += 1
            return_code = int(
                self._functions["event_set_wait_v2"](
                    self._event_set,
                    ctypes.byref(event_data),
                    ctypes.c_uint(wait_ms),
                )
            )
            return return_code, event_data

        def consume_event(event_data: NvmlEventData) -> float:
            nonlocal quiet_started, quiet_deadline
            event_type = int(event_data.eventType)
            event_device = int(event_data.device or 0)
            event_monotonic = self._monotonic_now("event_timestamp")
            record = {
                "timestamp_utc": self._now(),
                "timestamp_monotonic_s": event_monotonic,
                "device_handle": event_device,
                "event_type_bits": event_type,
                "xid_code": (
                    int(event_data.eventData)
                    if event_type & NVML_EVENT_TYPE_XID_CRITICAL_ERROR
                    else None
                ),
                "gpu_instance_id": int(event_data.gpuInstanceId),
                "compute_instance_id": int(event_data.computeInstanceId),
            }
            self.events.append(record)
            self._cache_provenance()
            if event_device != int(self._device.value or 0):
                raise NvmlProtocolError(
                    "nvmlEventSetWait_v2",
                    "event device handle does not match the registered "
                    "physical GPU handle",
                )
            if event_type != NVML_EVENT_TYPE_XID_CRITICAL_ERROR:
                raise NvmlProtocolError(
                    "nvmlEventSetWait_v2",
                    "received event bits other than the exact registered "
                    f"Xid bit: 0x{event_type:x}",
                )
            if fail_fast_on_event:
                raise NvmlProtocolError(
                    "nvmlEventSetWait_v2",
                    "registered Xid observed; fail-fast drain aborted",
                )
            if len(self.events) >= max_events:
                raise NvmlProtocolError(
                    "nvmlEventSetWait_v2",
                    f"event drain exceeded safety limit of {max_events}",
                )
            quiet_started = self._monotonic_now("quiet_restart")
            self.quiet_started_monotonic_s = quiet_started
            quiet_deadline = quiet_started + quiet_seconds
            return event_monotonic

        try:
            while True:
                now_monotonic = self._monotonic_now("drain_wait")
                quiet_remaining = quiet_deadline - now_monotonic
                total_remaining = (
                    total_deadline - now_monotonic
                    if total_deadline is not None
                    else None
                )
                boundary_reached = (
                    quiet_remaining <= 0
                    or (
                        total_remaining is not None
                        and total_remaining <= 0
                    )
                )
                if boundary_reached:
                    # A userspace scheduling pause can carry execution beyond
                    # the deadline after an early NVML timeout. Poll the queue
                    # once at zero timeout before declaring the interval quiet.
                    self.drain_final_poll_calls += 1
                    return_code, event_data = wait_once(0)
                    if return_code != NVML_ERROR_TIMEOUT:
                        _check_return(
                            "nvmlEventSetWait_v2",
                            return_code,
                        )
                        event_monotonic = consume_event(event_data)
                        if (
                            total_deadline is not None
                            and event_monotonic >= total_deadline
                        ):
                            raise NvmlProtocolError(
                                "drain",
                                "maximum total drain deadline reached with "
                                "a queued event",
                            )
                        continue
                    if quiet_remaining > 0:
                        raise NvmlProtocolError(
                            "drain",
                            "maximum total drain deadline expired before "
                            "the requested quiet interval completed",
                        )
                    self.observed_quiet_ms = max(
                        0.0,
                        (now_monotonic - quiet_started) * 1000.0,
                    )
                    if not math.isfinite(self.observed_quiet_ms):
                        raise NvmlProtocolError(
                            "drain",
                            "observed quiet interval is non-finite",
                        )
                    self.quiet_completed_monotonic_s = now_monotonic
                    self.drain_completed_monotonic_s = now_monotonic
                    self.drain_succeeded = True
                    self.drain_completed_at_utc = self._now()
                    self._cache_provenance()
                    return list(self.events)
                wait_seconds = quiet_remaining
                if total_remaining is not None:
                    wait_seconds = min(wait_seconds, total_remaining)
                wait_ms = max(1, math.ceil(wait_seconds * 1000.0))
                return_code, event_data = wait_once(wait_ms)
                if return_code == NVML_ERROR_TIMEOUT:
                    # NVML documents that an interrupt may produce an early
                    # timeout. Only the monotonic deadline proves a full quiet
                    # interval, so an early return must wait again.
                    continue
                _check_return("nvmlEventSetWait_v2", return_code)
                event_monotonic = consume_event(event_data)
                if (
                    total_deadline is not None
                    and event_monotonic >= total_deadline
                ):
                    raise NvmlProtocolError(
                        "drain",
                        "maximum total drain deadline reached with an event",
                    )
        except NvmlMonitorError as error:
            self.observed_quiet_ms = max(
                0.0,
                (self._monotonic_now("drain_error") - quiet_started) * 1000.0,
            )
            self.drain_error = self._error_record(error)
            self.drain_completed_at_utc = self._now()
            self.drain_completed_monotonic_s = self._monotonic_now(
                "drain_error_complete"
            )
            self._cache_provenance()
            raise
        except BaseException as error:
            self.observed_quiet_ms = max(
                0.0,
                (
                    self._monotonic_now("drain_error")
                    - quiet_started
                )
                * 1000.0,
            )
            wrapped = NvmlProtocolError(
                "drain",
                f"unexpected drain failure: {type(error).__name__}: {error}",
            )
            self.drain_error = self._error_record(wrapped)
            self.drain_completed_at_utc = self._now()
            self.drain_completed_monotonic_s = self._monotonic_now(
                "drain_error_complete"
            )
            self._cache_provenance()
            if not isinstance(error, Exception):
                raise
            raise wrapped from error

    def _cleanup_call(
        self,
        operation: str,
        function: Any,
        *args: Any,
    ) -> BaseException | None:
        try:
            _check_return(operation, function(*args))
        except NvmlMonitorError as error:
            self.cleanup_errors.append(self._error_record(error))
        except BaseException as error:
            wrapped = NvmlProtocolError(
                operation,
                f"unexpected cleanup failure: {type(error).__name__}: {error}",
            )
            self.cleanup_errors.append(self._error_record(wrapped))
            if not isinstance(error, Exception):
                return error
        return None

    def close(self) -> None:
        if self._closed:
            return
        pending_base_exception: BaseException | None = None
        if self._event_set.value and "event_set_free" in self._functions:
            pending_base_exception = self._cleanup_call(
                "nvmlEventSetFree",
                self._functions["event_set_free"],
                self._event_set,
            )
            self._event_set = ctypes.c_void_p()
        if self._initialized and "shutdown" in self._functions:
            shutdown_interruption = self._cleanup_call(
                "nvmlShutdown",
                self._functions["shutdown"],
            )
            if pending_base_exception is None:
                pending_base_exception = shutdown_interruption
            self._initialized = False
        if self._snapshot_descriptor is not None:
            descriptor = self._snapshot_descriptor
            self._snapshot_descriptor = None
            try:
                os.close(descriptor)
            except BaseException as error:
                wrapped = NvmlProtocolError(
                    "close_library_descriptor",
                    "could not close the sealed NVML snapshot descriptor: "
                    f"{type(error).__name__}: {error}",
                )
                self.cleanup_errors.append(self._error_record(wrapped))
                if (
                    pending_base_exception is None
                    and not isinstance(error, Exception)
                ):
                    pending_base_exception = error
        self._closed = True
        self.cleanup_completed_at_utc = self._now()
        self.cleanup_completed_monotonic_s = self._monotonic_now(
            "cleanup_complete"
        )
        self._cache_provenance()
        if pending_base_exception is not None:
            raise pending_base_exception

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> bool:
        self.close()
        return False

    @property
    def xid_events(self) -> list[Mapping[str, Any]]:
        return [
            event
            for event in self.events
            if event["event_type_bits"] & NVML_EVENT_TYPE_XID_CRITICAL_ERROR
        ]

    @property
    def safe_for_acceptance(self) -> bool:
        """True only after complete, error-free monitoring with no observed Xid."""

        return bool(
            self.setup_succeeded
            and self.drain_succeeded
            and self._closed
            and not self.setup_error
            and not self.drain_error
            and not self.cleanup_errors
            and not self.xid_events
        )

    @property
    def acceptance_safe(self) -> bool:
        """Alias retained for callers that phrase the result as a predicate."""

        return self.safe_for_acceptance

    def to_provenance(self) -> dict[str, Any]:
        record = {
            "schema_version": PROVENANCE_SCHEMA_VERSION,
            "method": "nvmlEventSetWait_v2_exact_xid",
            "physical_uuid": self.physical_uuid,
            "registered_device_handle": int(self._device.value or 0),
            "library": {
                "path": str(self.library_path),
                "sha256": self.library_sha256,
                "expected_sha256": self.expected_library_sha256,
                "version": self.library_version,
                "expected_version": self.expected_library_version,
                "identity": self.library_identity,
                "sealed_snapshot": self.library_snapshot,
                "load_path": self._library_load_path,
                "symbols": dict(sorted(self._symbols.items())),
            },
            "xid_event_bit": NVML_EVENT_TYPE_XID_CRITICAL_ERROR,
            "supported_event_bits": self.supported_event_bits,
            "registered_event_bits": self.registered_event_bits,
            "setup": {
                "started_at_utc": self.setup_started_at_utc,
                "completed_at_utc": self.setup_completed_at_utc,
                "started_monotonic_s": self.setup_started_monotonic_s,
                "completed_monotonic_s": self.setup_completed_monotonic_s,
                "succeeded": self.setup_succeeded,
                "error": self.setup_error,
            },
            "drain": {
                "started_at_utc": self.drain_started_at_utc,
                "completed_at_utc": self.drain_completed_at_utc,
                "started_monotonic_s": self.drain_started_monotonic_s,
                "completed_monotonic_s": self.drain_completed_monotonic_s,
                "quiet_started_monotonic_s": self.quiet_started_monotonic_s,
                "quiet_completed_monotonic_s": (
                    self.quiet_completed_monotonic_s
                ),
                "timeout_ms": self.drain_timeout_ms,
                "requested_quiet_ms": self.requested_quiet_ms,
                "maximum_total_ms": self.drain_maximum_total_ms,
                "fail_fast_on_event": self.drain_fail_fast_on_event,
                "observed_quiet_ms": self.observed_quiet_ms,
                "wait_calls": self.drain_wait_calls,
                "final_poll_calls": self.drain_final_poll_calls,
                "succeeded": self.drain_succeeded,
                "error": self.drain_error,
            },
            "cleanup": {
                "completed_at_utc": self.cleanup_completed_at_utc,
                "completed_monotonic_s": self.cleanup_completed_monotonic_s,
                "errors": list(self.cleanup_errors),
            },
            "events": list(self.events),
            "xids_seen": len(self.xid_events),
            "safe_for_acceptance": self.safe_for_acceptance,
        }
        self._last_provenance = record
        return record

    def _cache_provenance(self) -> None:
        """Retain the newest primitive-only snapshot, including every Xid."""

        try:
            self._last_provenance = self.to_provenance()
        except BaseException:
            # Evidence serialization must never erase an earlier Xid-bearing
            # snapshot. The caller can retrieve it with ``last_provenance``.
            pass

    @property
    def last_provenance(self) -> dict[str, Any] | None:
        return self._last_provenance


__all__ = [
    "MAX_NVML_LIBRARY_BYTES",
    "NVML_ERROR_NO_PERMISSION",
    "NVML_ERROR_NOT_SUPPORTED",
    "NVML_ERROR_TIMEOUT",
    "NVML_EVENT_TYPE_XID_CRITICAL_ERROR",
    "NVML_INSTANCE_ID_NONE",
    "NvmlCallError",
    "NvmlEventData",
    "NvmlLibraryError",
    "NvmlMonitorError",
    "NvmlPermissionError",
    "NvmlProtocolError",
    "NvmlUnsupportedError",
    "NvmlXidMonitor",
    "PROVENANCE_SCHEMA_VERSION",
]
