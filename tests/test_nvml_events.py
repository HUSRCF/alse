from __future__ import annotations

import ctypes
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import burstserve.nvml_events as nvml_events
from burstserve.nvml_events import (
    NVML_ERROR_NO_PERMISSION,
    NVML_ERROR_TIMEOUT,
    NVML_EVENT_TYPE_XID_CRITICAL_ERROR,
    NVML_INSTANCE_ID_NONE,
    NvmlCallError,
    NvmlEventData,
    NvmlLibraryError,
    NvmlPermissionError,
    NvmlProtocolError,
    NvmlUnsupportedError,
    NvmlXidMonitor,
)


class FakeFunction:
    def __init__(self, callback):
        self.callback = callback
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.callback(*args)


class FakeNvml:
    _name = "fake-libnvidia-ml.so.1"

    def __init__(
        self,
        *,
        supported_bits=NVML_EVENT_TYPE_XID_CRITICAL_ERROR,
        wait_results=None,
        supported_return=0,
        free_return=0,
        shutdown_return=0,
        version="610.43.02",
    ):
        self.calls = []
        self.supported_bits = supported_bits
        self.wait_results = list(
            wait_results if wait_results is not None else [NVML_ERROR_TIMEOUT]
        )

        self.nvmlInit_v2 = FakeFunction(lambda: self._return("init", 0))
        self.nvmlShutdown = FakeFunction(
            lambda: self._return("shutdown", shutdown_return)
        )
        self.version = version
        self.nvmlSystemGetNVMLVersion = FakeFunction(self._version)
        self.nvmlDeviceGetHandleByUUID_v2 = FakeFunction(
            self._device_by_uuid
        )
        self.nvmlEventSetCreate = FakeFunction(self._event_create)
        self.nvmlEventSetFree = FakeFunction(
            lambda _event_set: self._return("free", free_return)
        )
        self.nvmlDeviceGetSupportedEventTypes = FakeFunction(
            lambda _device, output: self._supported(output, supported_return)
        )
        self.nvmlDeviceRegisterEvents = FakeFunction(self._register)
        self.nvmlEventSetWait_v2 = FakeFunction(self._wait)

    def _return(self, name, value):
        self.calls.append(name)
        return value

    def _version(self, output, size):
        self.calls.append("version")
        encoded = self.version.encode("ascii") + b"\0"
        ctypes.memmove(output, encoded, min(len(encoded), size.value))
        return 0

    def _device_by_uuid(self, uuid, output):
        self.calls.append(("uuid", uuid))
        output._obj.value = 0xD00D
        return 0

    def _supported(self, output, return_code):
        self.calls.append("supported")
        output._obj.value = self.supported_bits
        return return_code

    def _event_create(self, output):
        self.calls.append("create")
        output._obj.value = 0xE771
        return 0

    def _register(self, device, bits, event_set):
        self.calls.append(("register", device.value, bits.value, event_set.value))
        return 0

    def _wait(self, _event_set, output, timeout):
        self.calls.append(("wait", timeout.value))
        result = (
            self.wait_results.pop(0)
            if self.wait_results
            else NVML_ERROR_TIMEOUT
        )
        if isinstance(result, dict):
            output._obj.device = 0xD00D
            output._obj.eventType = result.get(
                "event_type", NVML_EVENT_TYPE_XID_CRITICAL_ERROR
            )
            output._obj.eventData = result.get("xid", 0)
            output._obj.gpuInstanceId = result.get(
                "gpu_instance_id", NVML_INSTANCE_ID_NONE
            )
            output._obj.computeInstanceId = result.get(
                "compute_instance_id", NVML_INSTANCE_ID_NONE
            )
            return 0
        return result


def fixed_now():
    return "2026-07-30T12:00:00.000000Z"


class LayoutTest(unittest.TestCase):
    def test_event_data_matches_installed_nvml_header_layout(self):
        self.assertEqual(
            NvmlEventData._fields_,
            [
                ("device", ctypes.c_void_p),
                ("eventType", ctypes.c_ulonglong),
                ("eventData", ctypes.c_ulonglong),
                ("gpuInstanceId", ctypes.c_uint),
                ("computeInstanceId", ctypes.c_uint),
            ],
        )
        self.assertEqual(ctypes.sizeof(NvmlEventData), 32)
        self.assertEqual(NVML_EVENT_TYPE_XID_CRITICAL_ERROR, 8)


class MonitorTest(unittest.TestCase):
    def make_monitor(self, uuid, library, **kwargs):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "libnvidia-ml.so.1"
        content = b"fake nvml identity"
        path.write_bytes(content)
        path.chmod(0o644)
        return NvmlXidMonitor(
            uuid,
            library_path=path,
            expected_library_sha256=hashlib.sha256(content).hexdigest(),
            expected_library_version=library.version,
            library=library,
            now=fixed_now,
            **kwargs,
        )

    def test_timeout_without_event_is_safe_after_cleanup(self):
        library = FakeNvml()
        monitor = self.make_monitor("GPU-test", library)

        with monitor:
            self.assertFalse(monitor.safe_for_acceptance)
            self.assertLess(
                library.calls.index(("register", 0xD00D, 8, 0xE771)),
                len(library.calls),
            )
            self.assertEqual(monitor.drain(25), [])
            self.assertFalse(monitor.safe_for_acceptance)

        self.assertTrue(monitor.safe_for_acceptance)
        provenance = monitor.to_provenance()
        self.assertTrue(provenance["safe_for_acceptance"])
        self.assertEqual(provenance["supported_event_bits"], 8)
        self.assertEqual(provenance["registered_event_bits"], 8)
        self.assertEqual(provenance["drain"]["requested_quiet_ms"], 25)
        self.assertGreaterEqual(
            provenance["drain"]["observed_quiet_ms"],
            25,
        )
        self.assertEqual(
            provenance["library"]["symbols"]["device_get_handle_by_uuid"],
            "nvmlDeviceGetHandleByUUID_v2",
        )
        self.assertEqual(library.calls[-2:], ["free", "shutdown"])

    def test_xid_is_serialized_and_rejects_acceptance(self):
        library = FakeNvml(
            wait_results=[
                {
                    "xid": 79,
                    "gpu_instance_id": 3,
                    "compute_instance_id": 5,
                },
                NVML_ERROR_TIMEOUT,
            ]
        )
        monitor = self.make_monitor("GPU-xid", library)

        with monitor:
            events = monitor.drain(10)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["device_handle"], 0xD00D)
        self.assertEqual(events[0]["event_type_bits"], 8)
        self.assertEqual(events[0]["xid_code"], 79)
        self.assertEqual(events[0]["gpu_instance_id"], 3)
        self.assertEqual(events[0]["compute_instance_id"], 5)
        self.assertFalse(monitor.acceptance_safe)
        self.assertEqual(monitor.to_provenance()["xids_seen"], 1)

    def test_unsupported_xid_bit_fails_setup_closed(self):
        library = FakeNvml(supported_bits=0)
        monitor = self.make_monitor("GPU-no-xid", library)

        with self.assertRaises(NvmlUnsupportedError):
            with monitor:
                self.fail("child launch would be unsafe")

        provenance = monitor.to_provenance()
        self.assertEqual(provenance["setup"]["error"]["category"], "unsupported")
        self.assertFalse(provenance["safe_for_acceptance"])
        self.assertNotIn("create", library.calls)
        self.assertEqual(library.calls[-1], "shutdown")

    def test_permission_error_is_distinct_setup_failure(self):
        library = FakeNvml(supported_return=NVML_ERROR_NO_PERMISSION)
        monitor = self.make_monitor("GPU-permission", library)

        with self.assertRaises(NvmlPermissionError):
            monitor.open()

        self.assertEqual(monitor.setup_error["category"], "permission")
        self.assertEqual(monitor.setup_error["code"], NVML_ERROR_NO_PERMISSION)
        self.assertEqual(library.calls[-1], "shutdown")

    def test_missing_required_symbol_is_library_failure(self):
        library = FakeNvml()
        del library.nvmlEventSetWait_v2
        monitor = self.make_monitor("GPU-old-library", library)

        with self.assertRaises(NvmlLibraryError):
            monitor.open()

        self.assertEqual(monitor.setup_error["category"], "library")
        self.assertFalse(monitor.safe_for_acceptance)
        self.assertNotIn("init", library.calls)

    def test_legacy_uuid_symbol_is_not_an_accepted_fallback(self):
        library = FakeNvml()
        library.nvmlDeviceGetHandleByUUID = (
            library.nvmlDeviceGetHandleByUUID_v2
        )
        del library.nvmlDeviceGetHandleByUUID_v2
        monitor = self.make_monitor("GPU-legacy-symbol", library)
        with self.assertRaises(NvmlLibraryError):
            monitor.open()

    def test_library_hash_and_version_are_both_pinned(self):
        library = FakeNvml(version="wrong")
        monitor = self.make_monitor("GPU-version", library)
        monitor.expected_library_version = "expected"
        with self.assertRaises(NvmlLibraryError):
            monitor.open()
        self.assertEqual(
            monitor.setup_error["operation"],
            "verify_library_version",
        )

    def test_source_inode_mutation_cannot_change_sealed_loader_bytes(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        content = b"fd-bound fake nvml"
        path = Path(directory.name) / "libnvidia-ml.so.1"
        path.write_bytes(content)
        path.chmod(0o644)
        library = FakeNvml()
        observed = {}

        def loader(load_path):
            # Mutate the already-opened source inode in place after BurstServe
            # captured/hash-checked it. The loader must still see only the
            # immutable sealed snapshot or fail closed.
            path.write_bytes(b"attacker replaced source inode contents")
            observed["path"] = load_path
            observed["content"] = Path(load_path).read_bytes()
            return library

        monitor = NvmlXidMonitor(
            "GPU-fd-bound",
            library_path=path,
            expected_library_sha256=hashlib.sha256(content).hexdigest(),
            expected_library_version=library.version,
            library_loader=loader,
            now=fixed_now,
        )
        with monitor:
            monitor.drain(1)

        self.assertRegex(observed["path"], r"^/proc/self/fd/[0-9]+$")
        self.assertEqual(observed["content"], content)
        self.assertNotEqual(path.read_bytes(), content)
        descriptor = int(observed["path"].rsplit("/", 1)[1])
        with self.assertRaises(OSError):
            os.fstat(descriptor)
        self.assertEqual(
            monitor.to_provenance()["library"]["load_path"],
            observed["path"],
        )
        snapshot = monitor.to_provenance()["library"]["sealed_snapshot"]
        self.assertEqual(
            snapshot["sha256"],
            hashlib.sha256(content).hexdigest(),
        )
        self.assertEqual(
            snapshot["seals"] & snapshot["required_seals"],
            snapshot["required_seals"],
        )

    def test_memfd_creation_oserror_is_a_categorized_library_failure(self):
        monitor = self.make_monitor("GPU-memfd-failure", FakeNvml())
        with mock.patch(
            "burstserve.nvml_events._memfd_create",
            side_effect=OSError(12, "synthetic memfd failure"),
        ):
            with self.assertRaises(NvmlLibraryError) as raised:
                monitor.open()

        self.assertEqual(raised.exception.operation, "create_snapshot")
        self.assertEqual(monitor.setup_error["category"], "library")
        self.assertEqual(
            monitor.setup_error["operation"],
            "create_snapshot",
        )

    def test_libc_memfd_fallback_when_python_omits_os_wrapper(self):
        with mock.patch.object(
            nvml_events.os,
            "memfd_create",
            None,
            create=True,
        ):
            descriptor = nvml_events._memfd_create(
                "burstserve-nvml-fallback-test",
                int(getattr(os, "MFD_CLOEXEC", 0x0001))
                | int(getattr(os, "MFD_ALLOW_SEALING", 0x0002)),
            )
        try:
            self.assertGreaterEqual(descriptor, 0)
            self.assertEqual(os.fstat(descriptor).st_size, 0)
        finally:
            os.close(descriptor)

    def test_snapshot_permission_oserror_closes_created_memfd(self):
        monitor = self.make_monitor("GPU-fchmod-failure", FakeNvml())
        real_memfd_create = nvml_events._memfd_create
        created_descriptors = []

        def record_memfd(*args, **kwargs):
            descriptor = real_memfd_create(*args, **kwargs)
            created_descriptors.append(descriptor)
            return descriptor

        with (
            mock.patch(
                "burstserve.nvml_events._memfd_create",
                side_effect=record_memfd,
            ),
            mock.patch(
                "burstserve.nvml_events.os.fchmod",
                side_effect=OSError(5, "synthetic fchmod failure"),
            ),
        ):
            with self.assertRaises(NvmlLibraryError) as raised:
                monitor.open()

        self.assertEqual(
            raised.exception.operation,
            "prepare_snapshot_permissions",
        )
        self.assertEqual(monitor.setup_error["category"], "library")
        self.assertTrue(created_descriptors)
        for descriptor in created_descriptors:
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_monotonic_clock_regression_is_a_protocol_error(self):
        values = iter([2.0, 1.0])
        monitor = self.make_monitor(
            "GPU-clock-regression",
            FakeNvml(),
            monotonic=lambda: next(values),
        )
        self.assertEqual(monitor._monotonic_now("first"), 2.0)
        with self.assertRaisesRegex(NvmlProtocolError, "regressed"):
            monitor._monotonic_now("second")

    def test_event_from_a_different_device_handle_fails_closed(self):
        library = FakeNvml(
            wait_results=[{"xid": 79}, NVML_ERROR_TIMEOUT]
        )

        def wrong_device_wait(event_set, output, timeout):
            result = library._wait(event_set, output, timeout)
            if result == 0:
                output._obj.device = 0xBAD
            return result

        library.nvmlEventSetWait_v2 = FakeFunction(wrong_device_wait)
        monitor = self.make_monitor("GPU-wrong-device", library)
        with monitor:
            with self.assertRaisesRegex(
                Exception,
                "device handle does not match",
            ):
                monitor.drain(10)
        self.assertEqual(monitor.to_provenance()["xids_seen"], 1)
        self.assertFalse(monitor.safe_for_acceptance)

    def test_zero_timeout_is_rejected(self):
        library = FakeNvml()
        monitor = self.make_monitor("GPU-zero-timeout", library)
        with monitor:
            with self.assertRaises(ValueError):
                monitor.drain(0)

    def test_drain_failure_is_recorded_and_cleanup_still_runs(self):
        library = FakeNvml(wait_results=[15])
        monitor = self.make_monitor("GPU-lost", library)

        with monitor:
            with self.assertRaises(NvmlCallError):
                monitor.drain(50)

        provenance = monitor.to_provenance()
        self.assertFalse(provenance["drain"]["succeeded"])
        self.assertEqual(provenance["drain"]["error"]["code"], 15)
        self.assertFalse(provenance["safe_for_acceptance"])
        self.assertEqual(library.calls[-2:], ["free", "shutdown"])

    def test_child_exception_does_not_skip_cleanup(self):
        library = FakeNvml()
        monitor = self.make_monitor("GPU-child-error", library)

        with self.assertRaisesRegex(RuntimeError, "child failed"):
            with monitor:
                raise RuntimeError("child failed")

        self.assertEqual(library.calls[-2:], ["free", "shutdown"])
        self.assertFalse(monitor.safe_for_acceptance)

    def test_cleanup_failure_rejects_otherwise_clean_run(self):
        library = FakeNvml(free_return=15)
        monitor = self.make_monitor("GPU-cleanup-error", library)

        with monitor:
            monitor.drain(1)

        self.assertEqual(monitor.cleanup_errors[0]["operation"], "nvmlEventSetFree")
        self.assertFalse(monitor.safe_for_acceptance)

    def test_early_timeout_retries_until_monotonic_quiet_deadline(self):
        monotonic_values = iter(
            [0.0, 0.0, 0.0, 0.0, 0.0, 0.2, 0.4, 1.0, 1.0]
        )
        library = FakeNvml(
            wait_results=[
                NVML_ERROR_TIMEOUT,
                NVML_ERROR_TIMEOUT,
                NVML_ERROR_TIMEOUT,
            ]
        )
        monitor = self.make_monitor(
            "GPU-early-timeout",
            library,
            monotonic=lambda: next(monotonic_values),
        )

        with monitor:
            monitor.drain(1000)

        waits = [call for call in library.calls if isinstance(call, tuple) and call[0] == "wait"]
        self.assertEqual(
            waits,
            [
                ("wait", 1000),
                ("wait", 800),
                ("wait", 600),
                ("wait", 0),
            ],
        )
        provenance = monitor.to_provenance()
        self.assertEqual(provenance["drain"]["wait_calls"], 4)
        self.assertEqual(provenance["drain"]["final_poll_calls"], 1)
        self.assertEqual(provenance["drain"]["requested_quiet_ms"], 1000)
        self.assertEqual(provenance["drain"]["observed_quiet_ms"], 1000)
        self.assertTrue(provenance["safe_for_acceptance"])

    def test_final_zero_poll_catches_xid_after_scheduler_pause(self):
        monotonic_values = iter(
            [0.0, 0.0, 0.0, 0.0, 0.0, 2.0, 2.0, 2.0, 2.0, 2.0]
        )
        library = FakeNvml(
            wait_results=[
                NVML_ERROR_TIMEOUT,
                {"xid": 79},
            ]
        )
        monitor = self.make_monitor(
            "GPU-final-poll-xid",
            library,
            monotonic=lambda: next(monotonic_values),
        )

        with monitor:
            with self.assertRaisesRegex(
                NvmlProtocolError,
                "fail-fast drain aborted",
            ):
                monitor.drain(
                    1000,
                    maximum_total_ms=2000,
                    fail_fast_on_event=True,
                )

        waits = [
            call
            for call in library.calls
            if isinstance(call, tuple) and call[0] == "wait"
        ]
        self.assertEqual(waits, [("wait", 1000), ("wait", 0)])
        self.assertEqual(len(monitor.xid_events), 1)
        self.assertEqual(
            monitor.to_provenance()["drain"]["final_poll_calls"],
            1,
        )
        self.assertFalse(monitor.safe_for_acceptance)

    def test_total_deadline_bounds_repeated_early_timeouts(self):
        monotonic_values = iter(
            [0.0, 0.0, 0.0, 0.0, 0.0, 0.5, 0.5, 0.5, 0.5]
        )
        library = FakeNvml(
            wait_results=[NVML_ERROR_TIMEOUT, NVML_ERROR_TIMEOUT]
        )
        monitor = self.make_monitor(
            "GPU-total-deadline",
            library,
            monotonic=lambda: next(monotonic_values),
        )

        with monitor:
            with self.assertRaisesRegex(
                NvmlProtocolError,
                "maximum total drain deadline expired",
            ):
                monitor.drain(1000, maximum_total_ms=500)

        waits = [
            call
            for call in library.calls
            if isinstance(call, tuple) and call[0] == "wait"
        ]
        self.assertEqual(waits, [("wait", 500), ("wait", 0)])
        provenance = monitor.to_provenance()
        self.assertEqual(provenance["drain"]["maximum_total_ms"], 500)
        self.assertFalse(provenance["drain"]["succeeded"])

    def test_keyboard_interrupt_during_drain_is_recorded_and_preserved(self):
        library = FakeNvml()

        def interrupt_wait(*_args):
            raise KeyboardInterrupt()

        library.nvmlEventSetWait_v2 = FakeFunction(interrupt_wait)
        monitor = self.make_monitor("GPU-interrupt-drain", library)
        monitor.open()
        try:
            with self.assertRaises(KeyboardInterrupt):
                monitor.drain(1000)
        finally:
            monitor.close()

        provenance = monitor.to_provenance()
        self.assertIn(
            "KeyboardInterrupt",
            provenance["drain"]["error"]["message"],
        )
        self.assertFalse(provenance["safe_for_acceptance"])

    def test_keyboard_interrupt_during_setup_is_recorded_and_preserved(self):
        library = FakeNvml()

        def interrupt_init():
            library.calls.append("init")
            raise KeyboardInterrupt()

        library.nvmlInit_v2 = FakeFunction(interrupt_init)
        monitor = self.make_monitor("GPU-interrupt-setup", library)

        with self.assertRaises(KeyboardInterrupt):
            monitor.open()

        provenance = monitor.to_provenance()
        self.assertIn(
            "KeyboardInterrupt",
            provenance["setup"]["error"]["message"],
        )
        self.assertFalse(provenance["safe_for_acceptance"])

    def test_keyboard_interrupt_during_cleanup_finishes_shutdown_and_reraises(
        self,
    ):
        library = FakeNvml()

        def interrupt_free(_event_set):
            library.calls.append("free")
            raise KeyboardInterrupt()

        library.nvmlEventSetFree = FakeFunction(interrupt_free)
        monitor = self.make_monitor("GPU-interrupt-cleanup", library)
        monitor.open()
        monitor.drain(1)

        with self.assertRaises(KeyboardInterrupt):
            monitor.close()

        self.assertEqual(library.calls[-2:], ["free", "shutdown"])
        self.assertTrue(monitor.to_provenance()["cleanup"]["errors"])
        self.assertFalse(monitor.safe_for_acceptance)


if __name__ == "__main__":
    unittest.main()
