from __future__ import annotations

import ctypes
import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import select
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_SOURCE = REPO_ROOT / "tests" / "test_native_parent_guard.py"
NATIVE_ROOT = REPO_ROOT / "native" / "smctrl_probe"
SOURCE = NATIVE_ROOT / "smid_probe.cu"
LAUNCHER_SOURCE = NATIVE_ROOT / "guard_launcher.c"
PARENT_GUARD_SOURCE = NATIVE_ROOT / "parent_guard.c"
SEALED_EXEC_SOURCE = NATIVE_ROOT / "sealed_exec.c"
MAKEFILE = NATIVE_ROOT / "Makefile"
ATTESTATION_SCRIPT = NATIVE_ROOT / "build_attestation.py"
BINARY = REPO_ROOT / "build" / "smctrl_probe" / "smid_probe"
REAL_BINARY = REPO_ROOT / "build" / "smctrl_probe" / "smid_probe.real"
HELPER = REPO_ROOT / "build" / "smctrl_probe" / "parent_guard_test_helper"
EXEC_TEST_LAUNCHER = (
    REPO_ROOT / "build" / "smctrl_probe" / "guard_exec_test_launcher"
)
EXEC_TEST_REAL = Path(f"{EXEC_TEST_LAUNCHER}.real")
IDENTITY_HEADER = (
    REPO_ROOT / "build" / "smctrl_probe" / "real_probe_identity.h"
)
EXEC_TEST_IDENTITY_HEADER = (
    REPO_ROOT / "build" / "smctrl_probe" / "guard_exec_test_identity.h"
)
STAMP = REPO_ROOT / "build" / "smctrl_probe" / "build-config.stamp"
ATTESTATION = (
    REPO_ROOT / "build" / "smctrl_probe" / "build-attestation.json"
)
LIBSMCTRL = REPO_ROOT / "vendor" / "libsmctrl"
BUILD_LOCK = (
    Path("/run/user")
    / str(os.geteuid())
    / "burstserve-smctrl-probe"
    / "build.lock"
)
GATE_COMPLETION_SENTINEL = (
    "burstserve-native-gate-required-check: verified"
)
GIT_PROVENANCE_SOURCE = (
    REPO_ROOT / "src" / "burstserve" / "git_provenance.py"
)
PINNED_PYTHON = "/usr/bin/python3"
PYTHON_ISOLATION_FLAGS = ("-I", "-S", "-B")
PR_SET_CHILD_SUBREAPER = 36
PR_GET_CHILD_SUBREAPER = 37
INJECTION_ENVIRONMENT = {
    "LD_PRELOAD": "/definitely/not/a/library.so",
    "LD_AUDIT": "/definitely/not/an/auditor.so",
    "LD_LIBRARY_PATH": "/definitely/not/a/library/path",
    "LD_FAKE_FUTURE_INJECTION": "must-be-cleared",
    "GLIBC_TUNABLES": "glibc.malloc.check=3",
    "GCONV_PATH": "/definitely/not/a/gconv/path",
    "LOCPATH": "/definitely/not/a/locale/path",
    "NLSPATH": "/definitely/not/a/message/path",
    "CUDA_INJECTION_PATH": "/definitely/not/an/injector.so",
    "CUDA_INJECTION32_PATH": "/definitely/not/an/injector32.so",
    "CUDA_INJECTION64_PATH": "/definitely/not/an/injector64.so",
    "CUDA_INJECTION_FUTURE_PATH": "/definitely/not/a/future-injector.so",
}
_ATTESTATION_MODULE = None


def _safe_environment() -> dict[str, str]:
    exact_injection_names = {
        "GLIBC_TUNABLES",
        "GCONV_PATH",
        "LOCPATH",
        "NLSPATH",
        "CPATH",
        "CPLUS_INCLUDE_PATH",
        "C_INCLUDE_PATH",
        "OBJC_INCLUDE_PATH",
        "LIBRARY_PATH",
        "COMPILER_PATH",
        "GCC_EXEC_PREFIX",
        "CUDAFE_FLAGS",
        "PTXAS_FLAGS",
        "CFLAGS",
        "CPPFLAGS",
        "LDFLAGS",
        "BASHOPTS",
        "BASH_ENV",
        "ENV",
        "MAKEFLAGS",
        "MFLAGS",
        "GNUMAKEFLAGS",
        "MAKEOVERRIDES",
        "MAKEFILE_LIST",
        "SHELLOPTS",
    }
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("LD_")
        and not name.startswith("BASH_FUNC_")
        and not name.startswith("CUDA_INJECTION")
        and not name.startswith("NVCC_")
        and not name.startswith("PYTHON")
        and name not in exact_injection_names
    }
    environment["CUDA_VISIBLE_DEVICES"] = ""
    environment["CUDA_MPS_PIPE_DIRECTORY"] = ""
    environment.pop("MASK_OFF", None)
    return environment


def _create_anonymous_memfd(name: str) -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    create = libc.memfd_create
    create.argtypes = (ctypes.c_char_p, ctypes.c_uint)
    create.restype = ctypes.c_int
    descriptor = create(name.encode("utf-8"), 0x0001)
    if descriptor < 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))
    return descriptor


def _program_types(path: Path) -> set[int]:
    with path.open("rb") as source:
        header = source.read(64)
        if len(header) != 64 or header[:6] != b"\x7fELF\x02\x01":
            raise AssertionError(f"not an ELF64 little-endian binary: {path}")
        unpacked = struct.unpack("<16sHHIQQQIHHHHHH", header)
        program_offset = unpacked[5]
        program_entry_size = unpacked[9]
        program_count = unpacked[10]
        result: set[int] = set()
        for index in range(program_count):
            source.seek(program_offset + index * program_entry_size)
            entry = source.read(program_entry_size)
            if len(entry) != program_entry_size:
                raise AssertionError(f"truncated ELF program headers: {path}")
            result.add(struct.unpack_from("<I", entry)[0])
        return result


def _parse_stamp(path: Path = STAMP) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if not separator or not key or key in fields:
            raise AssertionError(f"malformed or duplicate stamp field: {line}")
        fields[key] = value
    return fields


def _proc_stat_fields(pid: int) -> list[str] | None:
    # The kernel prints comm as raw bytes, so only the trailer is decoded.
    try:
        content = Path(f"/proc/{pid}/stat").read_bytes()
    except (FileNotFoundError, ProcessLookupError):
        return None
    closing = content.rfind(b")")
    if closing < 0:
        raise AssertionError(f"malformed /proc stat for pid {pid}")
    return content[closing + 2 :].decode("ascii", errors="strict").split()


def _process_start_identity(pid: int) -> str | None:
    fields_after_comm = _proc_stat_fields(pid)
    if fields_after_comm is None:
        return None
    # Field 3 is index 0 here; process start time is field 22.
    if len(fields_after_comm) <= 19:
        raise AssertionError(f"truncated /proc stat for pid {pid}")
    return fields_after_comm[19]


def _process_state(pid: int) -> str | None:
    fields_after_comm = _proc_stat_fields(pid)
    if fields_after_comm is None:
        return None
    if not fields_after_comm:
        raise AssertionError(f"truncated /proc stat for pid {pid}")
    return fields_after_comm[0]


def _live_thread_states(pid: int) -> list[str]:
    """States of every non-zombie thread of a thread group."""

    try:
        threads = os.listdir(f"/proc/{pid}/task")
    except (FileNotFoundError, ProcessLookupError, NotADirectoryError):
        return []
    states: list[str] = []
    for thread in threads:
        if not thread.isdigit():
            continue
        try:
            content = Path(f"/proc/{pid}/task/{thread}/stat").read_bytes()
        except (FileNotFoundError, ProcessLookupError):
            continue
        closing = content.rfind(b")")
        if closing < 0:
            continue
        fields = content[closing + 2 :].decode("ascii", errors="strict").split()
        if fields and fields[0] != "Z":
            states.append(fields[0])
    return states


def _kill_if_same_process(pid: int, start_identity: str) -> None:
    if _process_start_identity(pid) != start_identity:
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def _attestation_environment(
    *,
    libsmctrl_dir: Path,
    python: str,
) -> dict[str, str]:
    fields = _parse_stamp()
    return {
        "PATH": fields["HERMETIC_PATH"],
        "LANG": "C",
        "LC_ALL": "C",
        "TZ": "UTC",
        "SOURCE_DATE_EPOCH": "0",
        "CUDA_CACHE_DISABLE": "1",
        "TMPDIR": fields["BUILD_TMPDIR"],
        "BS_SOURCE_DIR": str(NATIVE_ROOT),
        "BS_REPO_ROOT": str(REPO_ROOT),
        "BS_BUILD_DIR": str(BINARY.parent),
        "BS_BUILD_TMPDIR": fields["BUILD_TMPDIR"],
        "BS_BUILD_LOCK_PATH": str(BUILD_LOCK),
        "BS_HERMETIC_PATH": fields["HERMETIC_PATH"],
        "BS_CUDA_HOME": fields["CUDA_HOME"],
        "BS_CUDA_ARCH": fields["CUDA_ARCH"],
        "BS_NVCC": fields["NVCC"],
        "BS_CC": fields["CC"],
        "BS_AR": fields["AR"],
        "BS_OBJCOPY": fields["OBJCOPY"],
        "BS_CPPFLAGS": fields["CPPFLAGS"],
        "BS_CFLAGS": fields["CFLAGS"],
        "BS_NVCCFLAGS": fields["NVCCFLAGS"],
        "BS_LDLIBS": fields["LDLIBS"],
        "BS_LAUNCHER_CFLAGS": fields["LAUNCHER_CFLAGS"],
        "BS_LIBSMCTRL_DIR": str(libsmctrl_dir),
    }


def _isolated_python_command(python: str) -> list[str]:
    return [python, *PYTHON_ISOLATION_FLAGS]


def _load_attestation_module():
    global _ATTESTATION_MODULE
    if _ATTESTATION_MODULE is not None:
        return _ATTESTATION_MODULE
    specification = importlib.util.spec_from_file_location(
        "burstserve_test_native_build_attestation_io",
        ATTESTATION_SCRIPT,
    )
    if specification is None or specification.loader is None:
        raise AssertionError("cannot load native build attestation module")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    _ATTESTATION_MODULE = module
    return module


class NativeParentGuardSourceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SOURCE.read_text(encoding="utf-8")
        cls.launcher_source = LAUNCHER_SOURCE.read_text(encoding="utf-8")
        cls.parent_guard_source = PARENT_GUARD_SOURCE.read_text(encoding="utf-8")
        cls.sealed_exec_source = SEALED_EXEC_SOURCE.read_text(encoding="utf-8")
        cls.makefile = MAKEFILE.read_text(encoding="utf-8")
        cls.attestation_source = ATTESTATION_SCRIPT.read_text(encoding="utf-8")

    def test_native_schema_v2_and_guard_is_before_first_cuda_call(self) -> None:
        self.assertIn('"burstserve.smid-probe-native/v2"', self.source)
        main = self.source[self.source.index("int main(") :]
        guard = main.index("arm_masked_parent_guard(")
        first_cuda = min(
            main.index("cuDriverGetVersion("),
            main.index("cudaRuntimeGetVersion("),
            main.index("cudaSetDevice("),
        )
        self.assertLess(guard, first_cuda)

    def test_probe_requires_guard_inherited_across_exec(self) -> None:
        guard = self.source[
            self.source.index("bool arm_masked_parent_guard(") :
            self.source.index("bool driver_is_validated_by_upstream(")
        ]
        self.assertLess(
            guard.index("PR_GET_PDEATHSIG"),
            guard.index("PR_SET_PDEATHSIG"),
        )
        self.assertIn("launcher_guard_missing", guard)
        self.assertIn("inherited_pdeath_signal", self.source)

    def test_prctl_is_before_immediate_parent_identity_check(self) -> None:
        for source in (self.source, self.parent_guard_source):
            match = re.search(
                r"prctl\(PR_SET_PDEATHSIG, SIGKILL\).*?"
                r"(?:const pid_t observed_parent = |observed_parent = )"
                r"getppid\(\);",
                source,
                flags=re.DOTALL,
            )
            self.assertIsNotNone(match)
            between = match.group(0) if match is not None else ""
            self.assertNotIn("cuDriver", between)
            self.assertNotIn("cudaRuntime", between)
            self.assertNotIn("libsmctrl_", between)

    def test_launcher_executes_only_a_sealed_memfd_snapshot(self) -> None:
        combined = self.launcher_source + self.sealed_exec_source
        for token in (
            "O_NOFOLLOW",
            "O_NONBLOCK",
            "O_CLOEXEC",
            "BURSTSERVE_REAL_PROBE_SHA256",
            "BURSTSERVE_REAL_PROBE_SIZE",
            "MFD_CLOEXEC",
            "MFD_ALLOW_SEALING",
            "MFD_EXEC",
            "F_SEAL_WRITE",
            "F_SEAL_GROW",
            "F_SEAL_SHRINK",
            "F_SEAL_EXEC",
            "F_SEAL_SEAL",
            "F_GET_SEALS",
            "SYS_execveat",
            "AT_EMPTY_PATH",
        ):
            self.assertIn(token, combined)
        self.assertNotIn("fexecve(", self.launcher_source)
        self.assertNotIn("execve(", self.launcher_source)
        self.assertIn("pathname execution fallback", self.sealed_exec_source)
        self.assertLess(
            self.launcher_source.index("burstserve_arm_parent_guard("),
            self.launcher_source.index(
                "burstserve_restore_lifecycle_signals()"
            ),
        )
        self.assertLess(
            self.launcher_source.index(
                "burstserve_restore_lifecycle_signals()"
            ),
            self.launcher_source.index(
                "burstserve_sanitize_loader_environment()"
            ),
        )
        self.assertLess(
            self.launcher_source.index(
                "burstserve_sanitize_loader_environment()"
            ),
            self.launcher_source.index("open(real_path"),
        )

    def test_runtime_sanitizer_and_signal_contract_are_explicit(self) -> None:
        for token in (
            '"LD_"',
            '"GLIBC_TUNABLES"',
            '"GCONV_PATH"',
            '"CUDA_INJECTION"',
            '"LOCPATH"',
            '"NLSPATH"',
            "SIG_UNBLOCK",
            "SIGHUP",
            "SIGINT",
            "SIGTERM",
        ):
            self.assertIn(token, self.parent_guard_source)

    def test_build_contract_uses_private_inherited_lock_and_atomic_outputs(
        self,
    ) -> None:
        for token in (
            "/run/user/",
            "burstserve-smctrl-probe",
            "build.lock",
            "BURSTSERVE_BUILD_LOCK_FD",
            "/proc/self/fd/",
            ".DELETE_ON_ERROR",
            "unexport $(.VARIABLES)",
            "override .SHELLFLAGS := -p -eu -o pipefail -c",
            "MAKE_SHELL_MODE_DIAGNOSTIC",
            "$(ENV) -i",
            "-ccbin=",
            "gate-required-check",
            GATE_COMPLETION_SENTINEL,
            "identity-header",
            "$(CHMOD) 0500",
            "$(MV) -f",
        ):
            self.assertIn(token, self.makefile)
        self.assertNotIn("$(RM) -r", self.makefile)
        self.assertNotIn("rm -rf", self.makefile)
        substantive_lines = [
            line
            for line in self.makefile.splitlines()
            if line and not line.startswith("#")
        ]
        self.assertEqual(substantive_lines[0], "unexport $(.VARIABLES)")
        self.assertLess(
            substantive_lines.index("unexport $(.VARIABLES)"),
            next(
                index
                for index, line in enumerate(substantive_lines)
                if "$(shell" in line
            ),
        )

    def test_make_trust_roots_are_not_command_line_overridable(self) -> None:
        assignment = re.compile(
            r"^[A-Za-z_.][A-Za-z0-9_.-]*\s*(?::|\+|\?)?="
        )
        inside_define = False
        unprotected: list[str] = []
        for line in self.makefile.splitlines():
            if line.startswith("override define "):
                inside_define = True
                continue
            if inside_define and line == "endef":
                inside_define = False
                continue
            if not inside_define and (
                line.startswith("define ") or assignment.match(line)
            ):
                if not line.startswith("override "):
                    unprotected.append(line)
        self.assertEqual(
            unprotected,
            [],
            "every named Make trust root must use the override directive",
        )
        for token in (
            "override MAKE := /usr/bin/make",
            "override SOURCE_DIR :=",
            "override REPO_ROOT :=",
            "override CUDA_HOME :=",
            "override BUILD_DIR :=",
            "override LIBSMCTRL_DIR :=",
            "override PYTHON := /usr/bin/python3",
            "override FLOCK := /usr/bin/flock",
            "override BUILD_LOCK :=",
            "override ATTESTATION :=",
            "override HERMETIC_PATH :=",
            "override CPPFLAGS :=",
            "override CFLAGS :=",
            "override NVCCFLAGS :=",
            "override LDLIBS :=",
            "override define ATTEST_ENV",
            "override define acquire_build_lock",
            "trust-root-self-check",
            "$(origin MAKEFLAGS)",
            "forbidden GNU make control mode",
            "command-line or override MAKEFLAGS assignments are forbidden",
            "PYTHONNOUSERSITE=1",
            "PYTHONDONTWRITEBYTECODE=1",
            "$(TEST_ENV) /usr/bin/python3 -I -S -B",
            "'$(REPO_ROOT)/tests/test_native_parent_guard.py' -v",
            "make-parse-guard",
            "/usr/bin/python3 -I -S -B",
            "$(words $(MAKEFILE_LIST))",
            "$(realpath",
            "$(.SHELLSTATUS)",
            "MAKE_PARSE_GUARD_DIAGNOSTIC",
            "MAKE_SHELL_MODE_DIAGNOSTIC",
        ):
            self.assertIn(token, self.makefile)
        lock_macro = self.makefile[
            self.makefile.index("override define acquire_build_lock") :
            self.makefile.index(
                "endef",
                self.makefile.index("override define acquire_build_lock"),
            )
        ]
        self.assertIn("/usr/bin/make", lock_macro)
        self.assertNotIn("$(MAKE)", lock_macro)
        self.assertNotIn(
            "-m unittest tests.test_native_parent_guard",
            self.makefile,
        )
        self.assertEqual(
            self.makefile.count(
                "$(ATTEST_ENV) $(PYTHON) -I -S -B "
                "'$(ATTESTATION_SCRIPT)'"
            ),
            7,
        )

    def test_attestation_has_finite_not_hermetic_contract(self) -> None:
        for token in (
            "burstserve.native-build-attestation/v2",
            "reject_dirty_libsmctrl",
            "security.capability",
            "HOST_TOOLCHAIN_COMPONENTS_SHA256",
            "CUDA_SUBTOOLS_SHA256",
            "NVCC_DRYRUN_SHA256",
            "finite_build_contract",
            "unlisted system headers",
            "parse_canonical_json",
            "duplicate key in build attestation JSON",
            "canonical encoding",
            "TEST_NATIVE_PARENT_GUARD_PY_SHA256",
            "validate_formal_make_invocation",
            "FORMAL_MAKE_EXECUTABLE",
            "FORBIDDEN_MAKE_ENVIRONMENT",
            "FORBIDDEN_MAKE_ENVIRONMENT_PREFIXES",
            'b"BASH_ENV"',
            'b"BASH_FUNC_"',
            'b"LD_"',
            'b"GLIBC_TUNABLES"',
            "external GNU make --eval/-E is forbidden",
            "multiple GNU make -f inputs are forbidden",
            "PYTHON_ISOLATION_FLAGS",
            "invocation_prefix=PYTHON_ISOLATION_FLAGS",
            "exact_json_equal",
            "_quiesce_process_group",
            "_live_process_group_members",
            "PROCESS_GROUP_QUIESCE_SECONDS",
            "has too many nodes",
        ):
            self.assertIn(token, self.attestation_source)
        readme = (NATIVE_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(
            "Only the formal runner originating the fixed argv makes that "
            "impossible",
            readme,
        )
        self.assertIn("not re-read for A/B", readme)
        self.assertNotIn("changing records", readme)

    def test_attestation_regular_reads_reject_fifo_and_symlink(self) -> None:
        module = _load_attestation_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            regular = root / "regular"
            regular.write_bytes(b"payload")
            linked = root / "linked"
            linked.symlink_to(regular.name)
            fifo = root / "fifo"
            os.mkfifo(fifo)

            with self.assertRaisesRegex(
                module.AttestationError,
                "without symlink traversal|open regular file safely",
            ):
                module.snapshot_regular_file(
                    linked,
                    description="linked input",
                    max_bytes=1024,
                )
            started = time.monotonic()
            with self.assertRaisesRegex(
                module.AttestationError,
                "not a regular file",
            ):
                module.snapshot_regular_file(
                    fifo,
                    description="FIFO input",
                    max_bytes=1024,
                )
            self.assertLess(time.monotonic() - started, 1.0)

    def test_attestation_executes_the_same_git_scanner_bytes_it_hashed(
        self,
    ) -> None:
        module = _load_attestation_module()
        with tempfile.TemporaryDirectory() as temporary:
            repo_root = Path(temporary)
            scanner = (
                repo_root / "src" / "burstserve" / "git_provenance.py"
            )
            scanner.parent.mkdir(parents=True)
            scanner.write_text(
                "class Snapshot:\n"
                "    complete = True\n"
                "    head_oid = 'a' * 40\n"
                "    errors = ()\n"
                "def capture_repository(*args, **kwargs):\n"
                "    return Snapshot()\n",
                encoding="utf-8",
            )
            snapshot = module.snapshot_regular_file(
                scanner,
                description="test Git scanner",
                max_bytes=module.MAX_SOURCE_FILE_BYTES,
                include_content=True,
            )
            scanner.write_text(
                "raise RuntimeError('reopened changed scanner bytes')\n",
                encoding="utf-8",
            )
            result = module.capture_libsmctrl_git_snapshot(
                repo_root,
                repo_root,
                module_snapshot=snapshot,
            )
            self.assertTrue(result.complete)
            self.assertEqual(result.head_oid, "a" * 40)

    def test_attestation_rejects_symlink_ancestor_for_reads_and_writes(
        self,
    ) -> None:
        module = _load_attestation_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            real_parent = root / "real"
            real_parent.mkdir(mode=0o700)
            (real_parent / "input").write_bytes(b"payload")
            alias = root / "alias"
            alias.symlink_to(real_parent.name, target_is_directory=True)

            with self.assertRaisesRegex(
                module.AttestationError,
                "without symlink traversal",
            ):
                module.snapshot_regular_file(
                    alias / "input",
                    description="aliased input",
                    max_bytes=1024,
                )
            with self.assertRaisesRegex(
                module.AttestationError,
                "without symlink traversal",
            ):
                module.atomic_write(alias / "output", b"output")
            self.assertFalse((real_parent / "output").exists())
            direct_output = real_parent / "direct-output"
            module.atomic_write(direct_output, b"output")
            self.assertEqual(direct_output.read_bytes(), b"output")
            self.assertEqual(
                stat.S_IMODE(direct_output.stat().st_mode),
                0o400,
            )

    def test_attestation_file_and_tree_resource_limits_are_enforced(
        self,
    ) -> None:
        module = _load_attestation_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            oversized = root / "oversized"
            oversized.write_bytes(b"12345")
            with self.assertRaisesRegex(
                module.AttestationError,
                "byte limit",
            ):
                module.snapshot_regular_file(
                    oversized,
                    description="oversized input",
                    max_bytes=4,
                )

            tree = root / "tree"
            tree.mkdir()
            (tree / "a").write_bytes(b"a")
            (tree / "b").write_bytes(b"b")
            with mock.patch.object(module, "MAX_TREE_ENTRIES", 1):
                with self.assertRaisesRegex(
                    module.AttestationError,
                    "entry limit",
                ):
                    module.tree_digest(tree)
            (tree / "link").symlink_to("a")
            with self.assertRaisesRegex(
                module.AttestationError,
                "unsupported file type",
            ):
                module.tree_digest(tree)

            descriptor = os.open(oversized, os.O_RDONLY)
            try:
                too_many = [
                    f"user.test-{index}"
                    for index in range(module.MAX_XATTR_COUNT + 1)
                ]
                with mock.patch.object(
                    module.os,
                    "listxattr",
                    return_value=too_many,
                ):
                    with self.assertRaisesRegex(
                        module.AttestationError,
                        "too many xattrs",
                    ):
                        module._bounded_xattrs_from_fd(
                            descriptor,
                            oversized,
                        )
            finally:
                os.close(descriptor)

    def test_attestation_stamp_header_and_json_parsers_are_strict_and_bounded(
        self,
    ) -> None:
        module = _load_attestation_module()
        fields = {field: "value" for field in module.STAMP_FIELD_ORDER}
        stamp = module.serialize_stamp(fields)
        self.assertEqual(module.parse_stamp(stamp), fields)
        for malformed in (
            stamp[:-1],
            stamp.replace(b"\n", b"\r\n", 1),
            stamp + b"\0",
        ):
            with self.subTest(malformed=malformed[-16:]):
                with self.assertRaises(module.AttestationError):
                    module.parse_stamp(malformed)

        header = (
            "#ifndef BURSTSERVE_REAL_PROBE_IDENTITY_H_\n"
            "#define BURSTSERVE_REAL_PROBE_IDENTITY_H_\n"
            f'#define BURSTSERVE_REAL_PROBE_SHA256 "{"a" * 64}"\n'
            "#define BURSTSERVE_REAL_PROBE_SIZE 1ULL\n"
            "#endif\n"
        ).encode("ascii")
        self.assertEqual(module.parse_identity_header(header), ("a" * 64, 1))
        with self.assertRaisesRegex(
            module.AttestationError,
            "malformed",
        ):
            module.parse_identity_header(header + b"#define INJECTED 1\n")

        with mock.patch.object(module, "MAX_ATTESTATION_BYTES", 8):
            with self.assertRaisesRegex(
                module.AttestationError,
                "byte limit",
            ):
                module.parse_canonical_json(b'{"long":1}\n')
        with self.assertRaisesRegex(
            module.AttestationError,
            "duplicate key",
        ):
            module.parse_canonical_json(b'{"a":1,"a":1}\n')
        nested = b"[" * (module.MAX_JSON_DEPTH + 1)
        with self.assertRaisesRegex(
            module.AttestationError,
            "nesting limit",
        ):
            module.parse_canonical_json(nested)

    def test_attestation_subprocess_output_and_descendants_are_bounded(
        self,
    ) -> None:
        module = _load_attestation_module()
        configuration = {
            "hermetic_path": "/usr/bin:/bin",
            "build_tmpdir": "/tmp",
        }
        with mock.patch.object(module, "MAX_SUBPROCESS_STREAM_BYTES", 64):
            with self.assertRaisesRegex(
                module.AttestationError,
                "stdout exceeds",
            ):
                module.run_command(
                    [
                        PINNED_PYTHON,
                        "-I",
                        "-S",
                        "-B",
                        "-c",
                        "import sys;sys.stdout.write('x'*1024)",
                    ],
                    configuration,
                )

        child_script = (
            "import subprocess;"
            "subprocess.Popen(['/usr/bin/python3','-I','-S','-B','-c',"
            "'import time;time.sleep(30)']);"
        )
        started = time.monotonic()
        with mock.patch.object(module, "SUBPROCESS_TIMEOUT_SECONDS", 0.2):
            with self.assertRaisesRegex(
                module.AttestationError,
                "timed out",
            ):
                module.run_command(
                    [
                        PINNED_PYTHON,
                        "-I",
                        "-S",
                        "-B",
                        "-c",
                        child_script,
                    ],
                    configuration,
                )
        self.assertLess(time.monotonic() - started, 2.0)

    DESCENDANT_SCRIPT = (
        "import subprocess, sys\n"
        "child = subprocess.Popen(\n"
        "    [sys.executable, '-I', '-S', '-B', '-c',\n"
        "     'import time\\ntime.sleep(60)'],\n"
        "    stdin=subprocess.DEVNULL,\n"
        "    stdout=subprocess.DEVNULL,\n"
        "    stderr=subprocess.DEVNULL,\n"
        ")\n"
        "open(sys.argv[1], 'w', encoding='ascii').write(str(child.pid))\n"
    )

    def _reap_descendant(self, record: Path) -> None:
        try:
            pid = int(record.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            return
        identity = _process_start_identity(pid)
        if identity is not None:
            _kill_if_same_process(pid, identity)

    def test_attestation_quiesces_descendants_that_outlive_normal_exit(
        self,
    ) -> None:
        module = _load_attestation_module()
        configuration = {
            "hermetic_path": "/usr/bin:/bin",
            "build_tmpdir": "/tmp",
        }
        with tempfile.TemporaryDirectory() as temporary:
            record = Path(temporary) / "descendant.pid"
            try:
                started = time.monotonic()
                returncode, _, _ = module.run_command(
                    [
                        PINNED_PYTHON,
                        "-I",
                        "-S",
                        "-B",
                        "-c",
                        self.DESCENDANT_SCRIPT,
                        str(record),
                    ],
                    configuration,
                )
                elapsed = time.monotonic() - started
                self.assertEqual(returncode, 0)
                self.assertLess(elapsed, module.SUBPROCESS_TIMEOUT_SECONDS)
                descendant = int(record.read_text(encoding="ascii").strip())
                # The direct child exited normally and closed both pipes, so
                # only an explicit process-group sweep can reach this
                # descendant, which holds no inherited pipe of its own.  A
                # bare "Z" is not proof of death once a leader thread can exit
                # ahead of its siblings, so require a dead thread group too.
                state = _process_state(descendant)
                self.assertIn(state, (None, "Z"))
                if state == "Z":
                    self.assertEqual(_live_thread_states(descendant), [])
            finally:
                self._reap_descendant(record)

    NON_ASCII_DESCENDANT_SCRIPT = (
        "import ctypes, subprocess, sys\n"
        "child = subprocess.Popen(\n"
        "    [sys.executable, '-I', '-S', '-B', '-c',\n"
        "     'import ctypes, time\\n"
        "libc = ctypes.CDLL(None, use_errno=True)\\n"
        "libc.prctl(15, ctypes.c_char_p(b\\\"\\\\xff\\\\xfehid\\\"), 0, 0, 0)\\n"
        "time.sleep(60)'],\n"
        "    stdin=subprocess.DEVNULL,\n"
        "    stdout=subprocess.DEVNULL,\n"
        "    stderr=subprocess.DEVNULL,\n"
        ")\n"
        "open(sys.argv[1], 'w', encoding='ascii').write(str(child.pid))\n"
    )

    # A thread group whose leader thread exits first reports state "Z" in
    # /proc/<tgid>/stat while its remaining threads keep running.
    ZOMBIE_LEADER_DESCENDANT_SCRIPT = (
        "import subprocess, sys\n"
        "child = subprocess.Popen(\n"
        "    [sys.executable, '-I', '-S', '-B', '-c',\n"
        "     'import ctypes, threading, time\\n"
        "threading.Thread(target=lambda: time.sleep(60)).start()\\n"
        "time.sleep(0.2)\\n"
        "ctypes.CDLL(None).syscall(60, 0)'],\n"
        "    stdin=subprocess.DEVNULL,\n"
        "    stdout=subprocess.DEVNULL,\n"
        "    stderr=subprocess.DEVNULL,\n"
        ")\n"
        "open(sys.argv[1], 'w', encoding='ascii').write(str(child.pid))\n"
    )

    def _refuse_to_kill(self):
        real_killpg = os.killpg

        def refuse_to_kill(process_group: int, number: int) -> None:
            if number == 0:
                real_killpg(process_group, number)
                return
            return None

        return refuse_to_kill

    def _assert_scan_reports_descendant(
        self,
        script: str,
        *,
        settle: float,
    ) -> int:
        """With SIGKILL neutered, the scan must still report this descendant.

        Neutering the signal isolates the scan from the ordering guarantee it
        normally relies on, so the descendant can only be reported if the
        ``/proc`` classification itself sees it.  Returns its pid.
        """

        module = _load_attestation_module()
        configuration = {
            "hermetic_path": "/usr/bin:/bin",
            "build_tmpdir": "/tmp",
        }
        with tempfile.TemporaryDirectory() as temporary:
            record = Path(temporary) / "descendant.pid"
            try:
                with mock.patch.object(
                    module, "PROCESS_GROUP_QUIESCE_SECONDS", settle
                ), mock.patch.object(
                    module.os, "killpg", self._refuse_to_kill()
                ):
                    with self.assertRaises(module.AttestationError) as raised:
                        module.run_command(
                            [
                                PINNED_PYTHON,
                                "-I",
                                "-S",
                                "-B",
                                "-c",
                                script,
                                str(record),
                            ],
                            configuration,
                        )
                message = str(raised.exception)
                self.assertIn("did not terminate", message)
                descendant = int(record.read_text(encoding="ascii").strip())
                self.assertIn(str(descendant), message)
                return descendant
            finally:
                self._reap_descendant(record)

    def test_attestation_scan_cannot_be_hidden_by_a_process_name(
        self,
    ) -> None:
        # The kernel writes comm as raw bytes, so a non-ASCII process name
        # must not make a live group member invisible to the scan itself.
        # This pins "not omitted"; that such a record is positively classified
        # rather than merely counted as unclassifiable is pinned by
        # test_attestation_unclassifiable_proc_records_count_as_live.
        self._assert_scan_reports_descendant(
            self.NON_ASCII_DESCENDANT_SCRIPT,
            settle=0.3,
        )

    def test_attestation_scan_cannot_be_hidden_by_a_zombie_leader(
        self,
    ) -> None:
        # A "Z" leader is only proof of death once every sibling thread is
        # gone; otherwise the group is still executing code.
        self._assert_scan_reports_descendant(
            self.ZOMBIE_LEADER_DESCENDANT_SCRIPT,
            settle=1.0,
        )

    def test_attestation_group_scan_counts_unclassifiable_pids_as_live(
        self,
    ) -> None:
        module = _load_attestation_module()
        real_record = module._proc_state_and_group
        target = os.getpid()
        group = os.getpgid(target)

        def unreadable(pid_text: str):
            if int(pid_text, 10) == target:
                return module._PROC_RECORD_UNREADABLE
            return real_record(pid_text)

        with mock.patch.object(
            module, "_proc_state_and_group", unreadable
        ):
            self.assertIn(target, module._live_process_group_members(group))

    def test_attestation_unclassifiable_proc_records_count_as_live(
        self,
    ) -> None:
        module = _load_attestation_module()
        self.assertIsNone(module._proc_state_and_group("0"))
        raw = (
            b"4242 (\xff\xfe-evil) S 4241 4242 4200 0 -1 4194304 "
            b"1 0 0 0 0 0 0 0 20 0 1 0 100 0 0\n"
        )
        with mock.patch.object(
            module,
            "_bounded_proc_read",
            return_value=raw,
        ):
            self.assertEqual(
                module._proc_state_and_group("4242"),
                ("S", 4242),
            )
        for content in (b"", b"4242 (evil S 4241\n", b"4242 (evil) S\n"):
            with self.subTest(content=content):
                with mock.patch.object(
                    module,
                    "_bounded_proc_read",
                    return_value=content,
                ):
                    self.assertIsInstance(
                        module._proc_state_and_group("4242"),
                        module._UnreadableProcRecord,
                    )
        for number, expected_absent in (
            (errno.ENOENT, True),
            (errno.ESRCH, True),
            (errno.EACCES, False),
            (errno.EIO, False),
        ):
            with self.subTest(errno=number):
                failure = module.AttestationError("cannot inspect")
                failure.__cause__ = OSError(number, os.strerror(number))
                with mock.patch.object(
                    module,
                    "_bounded_proc_read",
                    side_effect=failure,
                ):
                    record = module._proc_state_and_group("4242")
                if expected_absent:
                    self.assertIsNone(record)
                else:
                    self.assertIsInstance(
                        record,
                        module._UnreadableProcRecord,
                    )

    def test_attestation_process_group_quiesce_is_bounded_and_fails_closed(
        self,
    ) -> None:
        module = _load_attestation_module()
        configuration = {
            "hermetic_path": "/usr/bin:/bin",
            "build_tmpdir": "/tmp",
        }
        real_killpg = os.killpg

        def refuse_to_kill(process_group: int, number: int) -> None:
            if number == 0:
                real_killpg(process_group, number)
                return
            return None

        with tempfile.TemporaryDirectory() as temporary:
            record = Path(temporary) / "descendant.pid"
            try:
                with mock.patch.object(
                    module, "PROCESS_GROUP_QUIESCE_SECONDS", 0.3
                ), mock.patch.object(module.os, "killpg", refuse_to_kill):
                    started = time.monotonic()
                    with self.assertRaisesRegex(
                        module.AttestationError,
                        "did not terminate",
                    ):
                        module.run_command(
                            [
                                PINNED_PYTHON,
                                "-I",
                                "-S",
                                "-B",
                                "-c",
                                self.DESCENDANT_SCRIPT,
                                str(record),
                            ],
                            configuration,
                        )
                    self.assertLess(time.monotonic() - started, 10.0)
            finally:
                self._reap_descendant(record)

    def test_attested_build_lock_excludes_ambient_directory_link_count(
        self,
    ) -> None:
        """A sibling directory beside the lock must not void the build.

        The runner creates its GPU lease directory inside the same private
        runtime directory as the build lock, which raises that directory's
        link count.  Attesting the count made the first formal run invalidate
        its own attestation permanently, so it is deliberately not attested.
        """

        module = _load_attestation_module()
        self.assertNotIn(
            "directory_nlink",
            module.build_lock_record.__code__.co_consts,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock_directory = root / "burstserve-smctrl-probe"
            lock_directory.mkdir(mode=0o700)
            lock_path = lock_directory / "build.lock"
            lock_path.touch(mode=0o600)
            configuration = {"build_lock_path": str(lock_path)}

            before = module.build_lock_record(configuration)
            (lock_directory / ".burstserve-gpu-locks").mkdir(mode=0o700)
            after = module.build_lock_record(configuration)

            self.assertEqual(before, after)
            self.assertNotIn("directory_nlink", before)
            # The lock file's own hardlink defence is still pinned.
            self.assertEqual(before["nlink"], 1)
            self.assertEqual(before["mode_octal"], "0600")
            self.assertEqual(before["directory_mode_octal"], "0700")

    def test_attestation_json_node_budget_is_enforced_before_parsing(
        self,
    ) -> None:
        module = _load_attestation_module()
        flat = b"[" + b"1," * (module.MAX_JSON_NODES + 1) + b"1]\n"
        with mock.patch.object(
            module.json,
            "loads",
            side_effect=AssertionError("graph was built before the bound"),
        ):
            with self.assertRaisesRegex(
                module.AttestationError,
                "too many nodes",
            ):
                module.parse_canonical_json(flat)

        # Structural bytes inside a string never count as nodes or nesting.
        module._reject_excessive_json_nesting(
            b'{"a":"' + b"[{" * (module.MAX_JSON_DEPTH + 8) + b'","b":"\\""}'
        )
        # Object keys are not nodes, so the lexical budget matches the
        # post-parse accounting instead of rejecting accepted documents.
        with mock.patch.object(module, "MAX_JSON_NODES", 3):
            module._reject_excessive_json_nesting(b'{"a":1,"b":2}')
            with self.assertRaisesRegex(
                module.AttestationError,
                "too many nodes",
            ):
                module._reject_excessive_json_nesting(b'{"a":1,"b":2,"c":3}')

    def test_attestation_comparison_rejects_bool_int_float_substitution(
        self,
    ) -> None:
        module = _load_attestation_module()
        self.assertTrue(
            module.exact_json_equal(
                {"a": [1, True, 1.5, None, "x"]},
                {"a": [1, True, 1.5, None, "x"]},
            )
        )
        # Python equality alone would accept every one of these.
        self.assertEqual({"a": True, "b": 1}, {"a": 1, "b": 1.0})
        for left, right in (
            (True, 1),
            (1, True),
            (1, 1.0),
            (1.0, 1),
            (False, 0),
            ({"a": True}, {"a": 1}),
            ([0], [False]),
            ({"a": 1}, {"a": 1, "b": 2}),
            ([1], [1, 1]),
            # Dict lookup compares keys with ``==``; a JSON object key is
            # always a string, so a 1/True key pair must not match either.
            ({1: "a"}, {True: "a"}),
            ({True: "a"}, {1: "a"}),
        ):
            with self.subTest(left=left, right=right):
                self.assertFalse(module.exact_json_equal(left, right))

    def test_native_test_source_hash_is_attested(self) -> None:
        self.assertIn(
            "$(REPO_ROOT)/tests/test_native_parent_guard.py",
            self.makefile,
        )
        self.assertIn(
            "TEST_NATIVE_PARENT_GUARD_PY_SHA256",
            self.attestation_source,
        )

    def test_build_git_capture_ignores_clean_filter_and_writes_no_bytecode(
        self,
    ) -> None:
        specification = importlib.util.spec_from_file_location(
            "burstserve_test_build_attestation_safe_git",
            ATTESTATION_SCRIPT,
        )
        self.assertIsNotNone(specification)
        self.assertIsNotNone(specification.loader)
        module = importlib.util.module_from_spec(specification)
        sys.modules[specification.name] = module
        try:
            specification.loader.exec_module(module)
            with tempfile.TemporaryDirectory() as temporary:
                temporary_root = Path(temporary)
                repository = temporary_root / "libsmctrl"
                subprocess.run(
                    ["/usr/bin/git", "init", "-q", str(repository)],
                    check=True,
                )
                (repository / ".gitattributes").write_text(
                    "payload.txt filter=evil\n",
                    encoding="utf-8",
                )
                (repository / "payload.txt").write_text(
                    "payload\n",
                    encoding="utf-8",
                )
                subprocess.run(
                    [
                        "/usr/bin/git",
                        "-C",
                        str(repository),
                        "add",
                        ".gitattributes",
                        "payload.txt",
                    ],
                    check=True,
                )
                subprocess.run(
                    [
                        "/usr/bin/git",
                        "-C",
                        str(repository),
                        "-c",
                        "user.name=Test",
                        "-c",
                        "user.email=test@example.invalid",
                        "commit",
                        "-q",
                        "-m",
                        "initial",
                    ],
                    check=True,
                )
                marker = temporary_root / "filter.marker"
                filter_script = temporary_root / "evil-filter.sh"
                filter_script.write_text(
                    "#!/bin/sh\n"
                    f"/usr/bin/touch {marker}\n"
                    "/bin/cat\n",
                    encoding="utf-8",
                )
                filter_script.chmod(0o700)
                subprocess.run(
                    [
                        "/usr/bin/git",
                        "-C",
                        str(repository),
                        "config",
                        "filter.evil.clean",
                        str(filter_script),
                    ],
                    check=True,
                )
                subprocess.run(
                    [
                        "/usr/bin/git",
                        "-C",
                        str(repository),
                        "config",
                        "filter.evil.required",
                        "true",
                    ],
                    check=True,
                )
                marker.unlink(missing_ok=True)
                pycache = GIT_PROVENANCE_SOURCE.parent / "__pycache__"
                before = (
                    set(pycache.iterdir()) if pycache.is_dir() else set()
                )

                original = sys.dont_write_bytecode
                sys.dont_write_bytecode = True
                try:
                    snapshot = module.capture_libsmctrl_git_snapshot(
                        REPO_ROOT,
                        repository,
                    )
                finally:
                    sys.dont_write_bytecode = original
                after = (
                    set(pycache.iterdir()) if pycache.is_dir() else set()
                )

                self.assertTrue(snapshot.clean, snapshot.errors)
                self.assertFalse(marker.exists())
                self.assertEqual(after, before)
        finally:
            sys.modules.pop(specification.name, None)

    def test_isolated_python_startup_blocks_sitecustomize(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            injected = root / "injected"
            injected.mkdir()
            marker = root / "pre-main-marker"
            (injected / "sitecustomize.py").write_text(
                "import os\n"
                "from pathlib import Path\n"
                "Path(os.environ['BURSTSERVE_SITE_MARKER']).write_text("
                "'executed', encoding='utf-8')\n",
                encoding="utf-8",
            )
            environment = _safe_environment()
            environment["PYTHONPATH"] = str(injected)
            environment["BURSTSERVE_SITE_MARKER"] = str(marker)

            fixture_control = subprocess.run(
                [
                    *_isolated_python_command(PINNED_PYTHON),
                    "-c",
                    (
                        "import sys;"
                        "sys.path.insert(0, sys.argv[1]);"
                        "import sitecustomize"
                    ),
                    str(injected),
                ],
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(
                fixture_control.returncode,
                0,
                fixture_control.stderr,
            )
            self.assertEqual(marker.read_text(encoding="utf-8"), "executed")
            marker.unlink()

            isolated = subprocess.run(
                [
                    *_isolated_python_command(PINNED_PYTHON),
                    "-c",
                    (
                        "import json,sys;"
                        "print(json.dumps({'site': 'site' in sys.modules,"
                        "'path': sys.path}))"
                    ),
                ],
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(isolated.returncode, 0, isolated.stderr)
            result = json.loads(isolated.stdout)
            self.assertFalse(result["site"])
            self.assertNotIn(str(injected), result["path"])
            self.assertFalse(
                any(
                    "site-packages" in entry or "dist-packages" in entry
                    for entry in result["path"]
                ),
                result["path"],
            )
            self.assertFalse(marker.exists())

    def test_absolute_test_entrypoint_cannot_be_package_shadowed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shadow = root / "tests"
            shadow.mkdir()
            marker = root / "shadow-imported"
            (shadow / "__init__.py").write_text("", encoding="utf-8")
            (shadow / "test_native_parent_guard.py").write_text(
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('shadowed')\n",
                encoding="utf-8",
            )
            environment = _safe_environment()
            environment["PYTHONPATH"] = str(root)
            test_name = (
                "NativeParentGuardSourceTest."
                "test_native_test_source_hash_is_attested"
            )
            completed = subprocess.run(
                [
                    *_isolated_python_command(PINNED_PYTHON),
                    str(TEST_SOURCE),
                    test_name,
                    "-v",
                ],
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn(test_name, completed.stderr)
            self.assertIn("OK", completed.stderr)
            self.assertFalse(marker.exists())


class NativeParentGuardArtifactTest(unittest.TestCase):
    @classmethod
    def required_artifacts(cls) -> tuple[Path, ...]:
        return (
            BINARY,
            REAL_BINARY,
            HELPER,
            EXEC_TEST_LAUNCHER,
            EXEC_TEST_REAL,
            IDENTITY_HEADER,
            EXEC_TEST_IDENTITY_HEADER,
            STAMP,
            ATTESTATION,
        )

    def require_artifacts(self) -> None:
        missing = [
            path for path in self.required_artifacts() if not path.is_file()
        ]
        if missing:
            self.skipTest(f"native artifacts have not been built: {missing}")

    def test_generated_stamp_matches_current_native_sources(self) -> None:
        self.require_artifacts()
        fields = _parse_stamp()
        source_paths = {
            "MAKEFILE_SHA256": MAKEFILE,
            "README_SHA256": NATIVE_ROOT / "README.md",
            "PROBE_CU_SHA256": SOURCE,
            "GUARD_LAUNCHER_C_SHA256": LAUNCHER_SOURCE,
            "GUARD_EXEC_TEST_FIXTURE_C_SHA256": (
                NATIVE_ROOT / "guard_exec_test_fixture.c"
            ),
            "PARENT_GUARD_C_SHA256": PARENT_GUARD_SOURCE,
            "PARENT_GUARD_H_SHA256": NATIVE_ROOT / "parent_guard.h",
            "PARENT_GUARD_TEST_HELPER_C_SHA256": (
                NATIVE_ROOT / "parent_guard_test_helper.c"
            ),
            "SEALED_EXEC_C_SHA256": SEALED_EXEC_SOURCE,
            "SEALED_EXEC_H_SHA256": NATIVE_ROOT / "sealed_exec.h",
            "SHA256_C_SHA256": NATIVE_ROOT / "sha256.c",
            "SHA256_H_SHA256": NATIVE_ROOT / "sha256.h",
            "BUILD_ATTESTATION_PY_SHA256": ATTESTATION_SCRIPT,
            "GIT_PROVENANCE_PY_SHA256": GIT_PROVENANCE_SOURCE,
            "TEST_NATIVE_PARENT_GUARD_PY_SHA256": TEST_SOURCE,
            "LIBSMCTRL_C_SHA256": LIBSMCTRL / "libsmctrl.c",
            "LIBSMCTRL_H_SHA256": LIBSMCTRL / "libsmctrl.h",
        }
        for field, path in source_paths.items():
            self.assertEqual(
                fields[field], hashlib.sha256(path.read_bytes()).hexdigest()
            )
        self.assertEqual(fields["BUILD_LOCK_PATH"], str(BUILD_LOCK))
        self.assertEqual(fields["LIBSMCTRL_GIT_DIRTY"], "clean")
        self.assertIn("-ccbin=/usr/bin/cc", fields["NVCCFLAGS"])

    def test_machine_attestation_binds_outputs_and_security_contract(
        self,
    ) -> None:
        self.require_artifacts()
        attestation = json.loads(ATTESTATION.read_text(encoding="utf-8"))
        self.assertEqual(
            attestation["schema_version"],
            "burstserve.native-build-attestation/v2",
        )
        self.assertTrue(all(attestation["checks"].values()))
        self.assertEqual(attestation["build_lock"]["path"], str(BUILD_LOCK))
        self.assertEqual(attestation["build_lock"]["mode_octal"], "0600")
        self.assertEqual(
            attestation["launcher_contract"]["exec"]["mechanism"],
            "syscall(SYS_execveat)",
        )
        self.assertFalse(
            attestation["launcher_contract"]["exec"]["pathname_fallback"]
        )
        self.assertFalse(
            attestation["launcher_contract"]["production_fault_injection"]
        )
        expected_paths = {
            "launcher": BINARY,
            "real_probe": REAL_BINARY,
            "parent_guard_test_helper": HELPER,
            "real_probe_identity_header": IDENTITY_HEADER,
            "guard_exec_test_launcher": EXEC_TEST_LAUNCHER,
            "guard_exec_test_fixture": EXEC_TEST_REAL,
            "guard_exec_test_identity_header": EXEC_TEST_IDENTITY_HEADER,
        }
        for name, path in expected_paths.items():
            record = attestation["outputs"][name]
            self.assertEqual(record["path"], str(path))
            self.assertEqual(
                record["sha256"], hashlib.sha256(path.read_bytes()).hexdigest()
            )
            self.assertEqual(record["size_bytes"], path.stat().st_size)
            self.assertEqual(record["metadata"]["uid"], os.geteuid())
            self.assertEqual(record["metadata"]["nlink"], 1)
            self.assertNotIn(
                "security.capability",
                [entry["name"] for entry in record["xattrs"]],
            )
        self.assertEqual(
            attestation["source_hashes"][
                "TEST_NATIVE_PARENT_GUARD_PY_SHA256"
            ],
            hashlib.sha256(TEST_SOURCE.read_bytes()).hexdigest(),
        )

    def test_identity_headers_bind_sha_and_size(self) -> None:
        self.require_artifacts()
        for header, binary in (
            (IDENTITY_HEADER, REAL_BINARY),
            (EXEC_TEST_IDENTITY_HEADER, EXEC_TEST_REAL),
        ):
            content = header.read_text(encoding="ascii")
            self.assertIn(hashlib.sha256(binary.read_bytes()).hexdigest(), content)
            self.assertIn(
                f"BURSTSERVE_REAL_PROBE_SIZE {binary.stat().st_size}ULL",
                content,
            )

    def test_artifacts_have_exact_permissions_links_and_no_capability(
        self,
    ) -> None:
        self.require_artifacts()
        modes = {
            BINARY: 0o500,
            REAL_BINARY: 0o500,
            HELPER: 0o500,
            EXEC_TEST_LAUNCHER: 0o500,
            EXEC_TEST_REAL: 0o500,
            IDENTITY_HEADER: 0o400,
            EXEC_TEST_IDENTITY_HEADER: 0o400,
            STAMP: 0o400,
            ATTESTATION: 0o400,
            BINARY.parent: 0o700,
        }
        self._assert_exact_permissions(modes)

    def test_build_lock_has_exact_permissions_links_and_no_capability(
        self,
    ) -> None:
        """The build lock is checked separately because it outlives less.

        ``build/`` is persistent, but the lock lives under ``/run/user/$UID``,
        which is tmpfs and is cleared when the login session ends. Asserting
        it alongside the persistent artifacts made the suite raise
        FileNotFoundError whenever the binaries had survived a reboot and the
        lock had not -- an error, not a skip, from a condition
        ``require_artifacts`` does not cover. Skipping here is honest: a
        missing lock is not a permissions failure, and it must not read as a
        passing check either.
        """

        self.require_artifacts()
        if not BUILD_LOCK.exists():
            self.skipTest(
                f"the build lock is not present in this session: {BUILD_LOCK}"
            )
        self._assert_exact_permissions(
            {BUILD_LOCK: 0o600, BUILD_LOCK.parent: 0o700}
        )

    def _assert_exact_permissions(self, modes: dict[Path, int]) -> None:
        for path, expected_mode in modes.items():
            with self.subTest(path=path):
                status = path.stat(follow_symlinks=False)
                self.assertEqual(status.st_uid, os.geteuid())
                self.assertEqual(stat.S_IMODE(status.st_mode), expected_mode)
                if path.is_file():
                    self.assertEqual(status.st_nlink, 1)
                self.assertNotIn(
                    "security.capability",
                    os.listxattr(path, follow_symlinks=False),
                )

    def test_launchers_and_helper_are_static_but_fixture_has_no_cuda(
        self,
    ) -> None:
        self.require_artifacts()
        for path in (BINARY, EXEC_TEST_LAUNCHER, HELPER):
            self.assertTrue({2, 3}.isdisjoint(_program_types(path)), path)
        completed = subprocess.run(
            ["/usr/bin/ldd", str(EXEC_TEST_REAL)],
            env=_safe_environment(),
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        dependencies = completed.stdout.lower()
        self.assertNotIn("cuda", dependencies)
        self.assertNotIn("nvidia", dependencies)

    def test_static_helper_sha256_matches_python(self) -> None:
        self.require_artifacts()
        sizes = (0, 1, 55, 56, 63, 64, 65, 16_383, 16_384, 16_385)
        with tempfile.TemporaryDirectory() as temporary:
            sample = Path(temporary) / "sample"
            for size in sizes:
                with self.subTest(size=size):
                    content = bytes(index % 251 for index in range(size))
                    sample.write_bytes(content)
                    completed = subprocess.run(
                        [str(HELPER), "--sha256", str(sample)],
                        env=_safe_environment(),
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    self.assertEqual(
                        completed.returncode, 0, completed.stderr
                    )
                    self.assertEqual(
                        completed.stdout.strip(),
                        hashlib.sha256(content).hexdigest(),
                    )

    def test_seals_reject_write_and_truncate(self) -> None:
        self.require_artifacts()
        completed = subprocess.run(
            [str(HELPER), "--sealed-memfd-self-test", str(EXEC_TEST_REAL)],
            env=_safe_environment(),
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("write_errno=1", completed.stdout)
        self.assertIn("truncate_errno=1", completed.stdout)
        self.assertIn("exec_seal=1", completed.stdout)

    def test_launcher_missing_parent_env_rejects_without_exec(self) -> None:
        self.require_artifacts()
        environment = _safe_environment()
        environment.pop("BURSTSERVE_PARENT_PID", None)
        completed = subprocess.run(
            [str(BINARY), "--mode", "global", "--enabled-tpc", "0"],
            cwd=REPO_ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 4)
        self.assertEqual(completed.stdout, "")
        self.assertIn("parent guard missing_env", completed.stderr)
        self.assertNotIn("CUDA", completed.stderr)

    def test_launcher_rejects_tampered_or_symlinked_source_before_exec(
        self,
    ) -> None:
        self.require_artifacts()
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            launcher = temporary_root / "smid_probe"
            real_probe = temporary_root / "smid_probe.real"
            shutil.copy2(BINARY, launcher)
            shutil.copy2(REAL_BINARY, real_probe)
            real_probe.chmod(0o700)
            tampered = bytearray(real_probe.read_bytes())
            tampered[-1] ^= 0x01
            real_probe.write_bytes(tampered)
            real_probe.chmod(0o500)
            completed = subprocess.run(
                [str(launcher), "--mode", "baseline"],
                env=_safe_environment(),
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(completed.returncode, 4)
            self.assertEqual(completed.stdout, "")
            self.assertIn("sealed executable snapshot rejected", completed.stderr)

            real_probe.chmod(0o700)
            real_probe.unlink()
            real_probe.symlink_to(REAL_BINARY)
            completed = subprocess.run(
                [str(launcher), "--mode", "baseline"],
                env=_safe_environment(),
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(completed.returncode, 4)
            self.assertEqual(completed.stdout, "")
            self.assertIn("cannot open real probe", completed.stderr)

    def test_real_probe_rejects_direct_masked_exec_before_cuda(self) -> None:
        self.require_artifacts()
        environment = _safe_environment()
        environment["BURSTSERVE_PARENT_PID"] = str(os.getpid())
        completed = subprocess.run(
            [str(REAL_BINARY), "--mode", "global", "--enabled-tpc", "0"],
            cwd=REPO_ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 4)
        lines = [line for line in completed.stdout.splitlines() if line]
        self.assertEqual(len(lines), 1)
        report = json.loads(lines[0])
        self.assertEqual(
            report["schema_version"], "burstserve.smid-probe-native/v2"
        )
        self.assertEqual(
            report["parent_guard"]["status"], "launcher_guard_missing"
        )
        self.assertIsNone(report["driver_version"])
        self.assertIsNone(report["runtime_version"])
        self.assertIsNone(report["device"]["name"])
        self.assertEqual(report["observed_histogram"], {})

    def test_production_launcher_does_not_contain_fault_control(self) -> None:
        self.require_artifacts()
        self.assertNotIn(
            b"BURSTSERVE_TEST_SEALED_EXEC_FAULT",
            BINARY.read_bytes(),
        )
        self.assertIn(
            b"BURSTSERVE_TEST_SEALED_EXEC_FAULT",
            EXEC_TEST_LAUNCHER.read_bytes(),
        )

    def test_internal_make_target_rejects_missing_inherited_lock(self) -> None:
        environment = _safe_environment()
        for name in (
            "MAKEFLAGS",
            "MFLAGS",
            "BURSTSERVE_BUILD_LOCK_PATH",
            "BURSTSERVE_BUILD_LOCK_FD",
        ):
            environment.pop(name, None)
        completed = subprocess.run(
            [
                "/usr/bin/make",
                "-C",
                str(NATIVE_ROOT),
                "--no-print-directory",
                "_locked-all",
            ],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertNotEqual(completed.returncode, 0)

    def test_make_control_modes_fail_during_parse_without_writes(self) -> None:
        self.require_artifacts()
        observed_paths = (*self.required_artifacts(), BUILD_LOCK)

        def snapshot(path: Path) -> tuple[int | str, ...]:
            status = path.stat(follow_symlinks=False)
            return (
                status.st_dev,
                status.st_ino,
                status.st_mode,
                status.st_uid,
                status.st_gid,
                status.st_nlink,
                status.st_size,
                status.st_mtime_ns,
                status.st_ctime_ns,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )

        before = {path: snapshot(path) for path in observed_paths}
        base_environment = _safe_environment()
        argv_cases = (
            ("dry-run-short", ("-n",), "gate-required-check"),
            ("just-print", ("--just-print",), "gate-required-check"),
            ("dry-run", ("--dry-run",), "gate-required-check"),
            ("recon", ("--recon",), "gate-required-check"),
            ("touch-short", ("-t",), "verify-attestation"),
            ("touch-long", ("--touch",), "verify-attestation"),
            (
                "ignore-errors-short",
                ("-i",),
                "_locked-verify-attestation",
            ),
            (
                "ignore-errors-long",
                ("--ignore-errors",),
                "_locked-verify-attestation",
            ),
            ("question-short", ("-q",), "gate-required-check"),
            ("question-long", ("--question",), "gate-required-check"),
            (
                "masked-makeflags",
                ("-n", "MAKEFLAGS="),
                "gate-required-check",
            ),
        )
        for name, options, goal in argv_cases:
            with self.subTest(source="argv", name=name):
                completed = subprocess.run(
                    [
                        "/usr/bin/make",
                        *options,
                        "-C",
                        str(NATIVE_ROOT),
                        "--no-print-directory",
                        goal,
                    ],
                    env=base_environment,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertNotEqual(completed.returncode, 0)
                self.assertTrue(
                    "forbidden GNU make control mode" in completed.stderr
                    or "MAKEFLAGS assignments are forbidden"
                    in completed.stderr,
                    completed.stderr,
                )

        environment_cases = (
            ("n", "gate-required-check"),
            ("t", "verify-attestation"),
            ("i", "_locked-verify-attestation"),
            ("q", "gate-required-check"),
        )
        for makeflags, goal in environment_cases:
            with self.subTest(source="environment", makeflags=makeflags):
                environment = base_environment.copy()
                environment["MAKEFLAGS"] = makeflags
                completed = subprocess.run(
                    [
                        "/usr/bin/make",
                        "-C",
                        str(NATIVE_ROOT),
                        "--no-print-directory",
                        goal,
                    ],
                    env=environment,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn(
                    "forbidden GNU make control mode",
                    completed.stderr,
                )

        after = {path: snapshot(path) for path in observed_paths}
        self.assertEqual(after, before)

    def test_shell_startup_and_dynamic_environment_injection_fail_closed(
        self,
    ) -> None:
        self.require_artifacts()
        observed_paths = (*self.required_artifacts(), BUILD_LOCK)

        def snapshot(path: Path) -> tuple[int | str, ...]:
            status = path.stat(follow_symlinks=False)
            return (
                status.st_dev,
                status.st_ino,
                status.st_mode,
                status.st_uid,
                status.st_gid,
                status.st_nlink,
                status.st_size,
                status.st_mtime_ns,
                status.st_ctime_ns,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )

        def invoke(
            name: str,
            injected_environment: dict[str, str],
            marker: Path,
            *,
            pass_fds: tuple[int, ...] = (),
        ) -> None:
            environment = _safe_environment()
            environment.update(injected_environment)
            completed = subprocess.run(
                [
                    "/usr/bin/make",
                    "-C",
                    str(NATIVE_ROOT),
                    "--no-print-directory",
                    "gate-required-check",
                ],
                env=environment,
                pass_fds=pass_fds,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertNotEqual(
                completed.returncode,
                0,
                f"{name} produced a false-success Gate:\n"
                f"stdout={completed.stdout}\nstderr={completed.stderr}",
            )
            self.assertNotIn(GATE_COMPLETION_SENTINEL, completed.stdout)
            self.assertIn(
                "forbidden make/shell/dynamic-process environment",
                completed.stderr,
            )
            self.assertFalse(marker.exists(), name)

        before = {path: snapshot(path) for path in observed_paths}
        attestation_before = ATTESTATION.read_bytes()
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)

            startup_scripts = {
                "bash-env-anonymous-memfd-exit-zero": b"exit 0\n",
                "bash-env-forged-sentinel": (
                    b"/usr/bin/printf '%s\\n' '"
                    + GATE_COMPLETION_SENTINEL.encode("ascii")
                    + b"'\nexit 0\n"
                ),
                "bash-env-malicious-exit-trap": (
                    b"trap '/usr/bin/touch "
                    + str(temporary_root / "bash-trap").encode()
                    + b"' EXIT\nexit 0\n"
                ),
                "bash-env-malicious-exec": (
                    b"exec /usr/bin/touch "
                    + str(temporary_root / "bash-exec").encode()
                    + b"\n"
                ),
            }
            for name, content in startup_scripts.items():
                with self.subTest(source="BASH_ENV", name=name):
                    marker_name = (
                        "bash-trap"
                        if name.endswith("trap")
                        else "bash-exec"
                        if name.endswith("exec")
                        else name
                    )
                    marker = temporary_root / marker_name
                    descriptor = _create_anonymous_memfd(
                        f"burstserve-{name}"
                    )
                    try:
                        os.write(descriptor, content)
                        os.lseek(descriptor, 0, os.SEEK_SET)
                        invoke(
                            name,
                            {"BASH_ENV": f"/proc/self/fd/{descriptor}"},
                            marker,
                            pass_fds=(descriptor,),
                        )
                    finally:
                        os.close(descriptor)

            env_marker = temporary_root / "env-startup"
            env_descriptor = _create_anonymous_memfd(
                "burstserve-env-startup"
            )
            try:
                os.write(
                    env_descriptor,
                    (
                        f"/usr/bin/touch {env_marker}\nexit 0\n"
                    ).encode("utf-8"),
                )
                os.lseek(env_descriptor, 0, os.SEEK_SET)
                with self.subTest(source="ENV"):
                    invoke(
                        "env-anonymous-memfd",
                        {"ENV": f"/proc/self/fd/{env_descriptor}"},
                        env_marker,
                        pass_fds=(env_descriptor,),
                    )
            finally:
                os.close(env_descriptor)

            function_marker = temporary_root / "exported-function"
            function_cases = {
                "BASH_FUNC_exec%%": (
                    f"() {{ /usr/bin/touch {function_marker}; "
                    'builtin exec "$@"; }}'
                ),
                "BASH_FUNC_test%%": (
                    f"() {{ /usr/bin/touch {function_marker}; "
                    'builtin test "$@"; }}'
                ),
                "BASH_FUNC_trap%%": (
                    f"() {{ /usr/bin/touch {function_marker}; "
                    'builtin trap "$@"; }}'
                ),
            }
            for variable, value in function_cases.items():
                with self.subTest(source="exported-function", name=variable):
                    invoke(
                        variable,
                        {variable: value},
                        function_marker,
                    )

            passive_marker = temporary_root / "passive-environment"
            passive_cases = {
                "SHELLOPTS": "xtrace",
                "BASHOPTS": "extdebug",
                "LD_FAKE_FUTURE_INJECTION": "rejected-by-prefix",
                "LD_PRELOAD": "/definitely/not/a/library.so",
                "LD_AUDIT": "/definitely/not/an/auditor.so",
                "LD_LIBRARY_PATH": "/definitely/not/a/library/path",
                "GLIBC_TUNABLES": "glibc.malloc.check=1",
                "GCONV_PATH": str(temporary_root / "gconv"),
                "LOCPATH": str(temporary_root / "locale"),
                "NLSPATH": str(temporary_root / "messages"),
            }
            for variable, value in passive_cases.items():
                with self.subTest(source="dynamic-or-shell", name=variable):
                    invoke(
                        variable,
                        {variable: value},
                        passive_marker,
                    )

        after = {path: snapshot(path) for path in observed_paths}
        self.assertEqual(after, before)
        self.assertEqual(ATTESTATION.read_bytes(), attestation_before)
        attestation = json.loads(attestation_before)
        self.assertTrue(all(attestation["checks"].values()))

    def test_external_make_code_injection_fails_parse_without_writes(
        self,
    ) -> None:
        self.require_artifacts()
        observed_paths = (*self.required_artifacts(), BUILD_LOCK)

        def snapshot(path: Path) -> tuple[int | str, ...]:
            status = path.stat(follow_symlinks=False)
            return (
                status.st_dev,
                status.st_ino,
                status.st_mode,
                status.st_uid,
                status.st_gid,
                status.st_nlink,
                status.st_size,
                status.st_mtime_ns,
                status.st_ctime_ns,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )

        before = {path: snapshot(path) for path in observed_paths}
        base_environment = _safe_environment()
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            marker = temporary_root / "recipe-executed"
            external_makefile = temporary_root / "injected.mk"
            external_makefile.write_text(
                ".IGNORE:\n"
                ".PHONY: burstserve-injected-marker\n"
                "_locked-verify-attestation: burstserve-injected-marker\n"
                "burstserve-injected-marker:\n"
                f"\t/usr/bin/touch {marker}\n",
                encoding="utf-8",
            )
            eval_payload = (
                ".IGNORE:\n"
                ".PHONY: burstserve-eval-marker\n"
                "_locked-verify-attestation: burstserve-eval-marker\n"
                "burstserve-eval-marker:\n"
                f"\t/usr/bin/touch {marker}\n"
            )
            argv_cases = (
                ("eval-long", (f"--eval={eval_payload}",)),
                ("eval-short", ("-E", eval_payload)),
                (
                    "extra-short-makefile-before-official",
                    (
                        "-f",
                        str(external_makefile),
                        "-f",
                        str(MAKEFILE),
                    ),
                ),
                (
                    "extra-short-makefile-after-official",
                    (
                        "-f",
                        str(MAKEFILE),
                        "-f",
                        str(external_makefile),
                    ),
                ),
                (
                    "extra-long-file-abbreviation",
                    (
                        f"--fi={external_makefile}",
                        f"--fi={MAKEFILE}",
                    ),
                ),
                (
                    "extra-long-makefile-abbreviation",
                    (
                        f"--makef={MAKEFILE}",
                        f"--makef={external_makefile}",
                    ),
                ),
                ("makefiles-assignment", (f"MAKEFILES={external_makefile}",)),
                ("makefile-list-assignment", ("MAKEFILE_LIST=",)),
                (
                    "gnumakeflags-assignment",
                    ("GNUMAKEFLAGS=--eval=.IGNORE:",),
                ),
                ("makeflags-assignment", ("MAKEFLAGS=",)),
                ("makeoverrides-assignment", ("MAKEOVERRIDES=injected",)),
            )
            for name, options in argv_cases:
                with self.subTest(source="argv", name=name):
                    completed = subprocess.run(
                        [
                            "/usr/bin/make",
                            "-C",
                            str(NATIVE_ROOT),
                            "--no-print-directory",
                            *options,
                            "_locked-verify-attestation",
                        ],
                        env=base_environment,
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    self.assertNotEqual(completed.returncode, 0)
                    self.assertTrue(
                        "formal GNU make invocation" in completed.stderr
                        or "MAKEFLAGS assignments are forbidden"
                        in completed.stderr,
                        completed.stderr,
                    )
                    self.assertNotIn(
                        "native-attestation-verification: ok",
                        completed.stdout,
                    )
                    self.assertFalse(marker.exists())

            environment_cases = {
                "MAKEFILES": str(external_makefile),
                "MAKEFILE_LIST": "hidden",
                "GNUMAKEFLAGS": "--eval=.IGNORE:",
                "MAKEFLAGS": "--eval=.IGNORE:",
                "MAKEOVERRIDES": "injected",
                "MFLAGS": "-i",
            }
            for variable, value in environment_cases.items():
                with self.subTest(source="environment", variable=variable):
                    environment = base_environment.copy()
                    environment[variable] = value
                    completed = subprocess.run(
                        [
                            "/usr/bin/make",
                            "-C",
                            str(NATIVE_ROOT),
                            "--no-print-directory",
                            "_locked-verify-attestation",
                        ],
                        env=environment,
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    self.assertNotEqual(completed.returncode, 0)
                    self.assertTrue(
                        "formal GNU make invocation" in completed.stderr
                        or "forbidden make-control environment"
                        in completed.stderr,
                        completed.stderr,
                    )
                    self.assertNotIn(
                        "native-attestation-verification: ok",
                        completed.stdout,
                    )
                    self.assertFalse(marker.exists())

        after = {path: snapshot(path) for path in observed_paths}
        self.assertEqual(after, before)

    def test_command_line_make_overrides_cannot_bypass_real_verify(
        self,
    ) -> None:
        self.require_artifacts()
        environment = _safe_environment()
        for name in (
            "MAKEFLAGS",
            "MFLAGS",
            "MAKEOVERRIDES",
            "BURSTSERVE_BUILD_LOCK_PATH",
            "BURSTSERVE_BUILD_LOCK_FD",
        ):
            environment.pop(name, None)

        inherited_lock = False
        try:
            inherited_lock = (
                Path("/proc/self/fd/9").resolve(strict=True)
                == BUILD_LOCK.resolve(strict=True)
            )
        except OSError:
            pass
        target = (
            "_locked-verify-attestation"
            if inherited_lock
            else "verify-attestation"
        )
        command = [
            "/usr/bin/make",
            "-C",
            str(NATIVE_ROOT),
            "--no-print-directory",
            target,
            "SHELL=/usr/bin/true",
            "MAKE=/usr/bin/true",
            "PYTHON=/usr/bin/true",
            "ENV=/usr/bin/true",
            "FLOCK=/usr/bin/true",
            "INSTALL=/usr/bin/true",
            "STAT=/usr/bin/true",
            "READLINK=/usr/bin/true",
            "REALPATH=/usr/bin/true",
            "RM=/usr/bin/true",
            "RMDIR=/usr/bin/true",
            "MV=/usr/bin/true",
            "CHMOD=/usr/bin/true",
            "SOURCE_DIR=/definitely/not/native-source",
            "REPO_ROOT=/definitely/not/repository",
            "CUDA_HOME=/definitely/not/cuda",
            "BUILD_DIR=/definitely/not/build",
            "LIBSMCTRL_DIR=/definitely/not/libsmctrl",
            "BUILD_EUID=0",
            "RUNTIME_USER_DIR=/definitely/not/runtime",
            "BUILD_LOCK_DIR=/definitely/not/lock-dir",
            "BUILD_LOCK=/definitely/not/build.lock",
            "LOCK_FD=1",
            "BURSTSERVE_BUILD_LOCK_PATH=/definitely/not/build.lock",
            "BURSTSERVE_BUILD_LOCK_FD=1",
            "TARGET=/definitely/not/launcher",
            "REAL_TARGET=/definitely/not/real",
            "CONFIG_STAMP=/dev/null",
            "ATTESTATION=/dev/null",
            "ATTESTATION_SCRIPT=/usr/bin/true",
            "TMP_DIR=/definitely/not/tmp",
            "NVCC=/usr/bin/true",
            "CC=/usr/bin/true",
            "AR=/usr/bin/true",
            "CPPFLAGS=-DBURSTSERVE_OVERRIDE_BYPASS",
            "CFLAGS=-DBURSTSERVE_OVERRIDE_BYPASS",
            "LAUNCHER_CFLAGS=-DBURSTSERVE_OVERRIDE_BYPASS",
            "NVCCFLAGS=-DBURSTSERVE_OVERRIDE_BYPASS",
            "LDLIBS=-lburstserve_override_bypass",
            "HERMETIC_PATH=/definitely/not/path",
            "BUILD_ENV=/usr/bin/true",
            "RECURSIVE_MAKE_ENV=/usr/bin/true",
            "ATTEST_ENV=/usr/bin/true",
            "NATIVE_SOURCES=",
            "ALL_GENERATED_FILES=",
            "acquire_build_lock=/usr/bin/true",
        ]
        completed = subprocess.run(
            command,
            env=environment,
            pass_fds=((9,) if inherited_lock else ()),
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn(
            "native-trust-root-self-check: ok",
            completed.stdout,
        )
        self.assertIn(
            "native-attestation-verification: ok",
            completed.stdout,
        )

    def test_verify_rejects_noncanonical_attestation_bytes(self) -> None:
        self.require_artifacts()
        pinned_python = PINNED_PYTHON
        canonical = ATTESTATION.read_bytes()
        parsed = json.loads(canonical)
        self.assertEqual(
            canonical,
            (
                json.dumps(
                    parsed,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
                + "\n"
            ).encode("utf-8"),
        )
        duplicate = (
            b'{"schema_version":"burstserve.native-build-attestation/v2",'
            + canonical[1:]
        )
        escaped_slash = canonical.replace(b"/", b"\\/", 1)
        substituted = json.loads(canonical)
        self.assertIs(substituted["checks"]["launcher_is_static"], True)
        substituted["checks"]["launcher_is_static"] = 1
        bool_to_int = (
            json.dumps(
                substituted,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            + "\n"
        ).encode("utf-8")
        self.assertEqual(json.loads(bool_to_int), json.loads(canonical))
        variants = {
            "leading-whitespace": (b" " + canonical, "canonical encoding"),
            "duplicate-key": (duplicate, "duplicate key"),
            "alternate-escape": (escaped_slash, "canonical encoding"),
            # Canonical-byte equality already distinguishes true from 1, so
            # this pins the end-to-end rejection; exact_json_equal is defence
            # in depth behind it and is unit-tested separately.
            "bool-to-int-substitution": (
                bool_to_int,
                "stale or does not match outputs",
            ),
        }
        environment = _attestation_environment(
            libsmctrl_dir=LIBSMCTRL,
            python=pinned_python,
        )
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            positive = temporary_root / "canonical.json"
            positive.write_bytes(canonical)
            positive.chmod(0o400)
            accepted = subprocess.run(
                [
                    *_isolated_python_command(pinned_python),
                    str(ATTESTATION_SCRIPT),
                    "verify",
                    "--stamp",
                    str(STAMP),
                    "--attestation",
                    str(positive),
                ],
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(accepted.returncode, 0, accepted.stderr)

            for name, (content, expected_error) in variants.items():
                with self.subTest(name=name):
                    candidate = temporary_root / f"{name}.json"
                    candidate.write_bytes(content)
                    candidate.chmod(0o400)
                    rejected = subprocess.run(
                        [
                            *_isolated_python_command(pinned_python),
                            str(ATTESTATION_SCRIPT),
                            "verify",
                            "--stamp",
                            str(STAMP),
                            "--attestation",
                            str(candidate),
                        ],
                        env=environment,
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=60,
                    )
                    self.assertEqual(rejected.returncode, 2)
                    self.assertIn(expected_error, rejected.stderr)

    def test_dirty_libsmctrl_finalize_and_verify_reject_directly(self) -> None:
        self.require_artifacts()
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            clone = temporary_root / "libsmctrl"
            subprocess.run(
                [
                    "/usr/bin/git",
                    "clone",
                    "--quiet",
                    "--no-hardlinks",
                    str(LIBSMCTRL),
                    str(clone),
                ],
                env=_safe_environment(),
                check=True,
                capture_output=True,
                timeout=30,
            )
            (clone / "untracked-dirty-marker").write_text(
                "dirty\n", encoding="utf-8"
            )
            environment = _attestation_environment(
                libsmctrl_dir=clone,
                python=sys.executable,
            )
            dirty_stamp = temporary_root / "dirty.stamp"
            stamp_result = subprocess.run(
                [
                    *_isolated_python_command(sys.executable),
                    str(ATTESTATION_SCRIPT),
                    "stamp",
                    "--output",
                    str(dirty_stamp),
                ],
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(stamp_result.returncode, 0, stamp_result.stderr)
            for command in ("finalize", "verify"):
                arguments = [
                    *_isolated_python_command(sys.executable),
                    str(ATTESTATION_SCRIPT),
                    command,
                    "--stamp",
                    str(dirty_stamp),
                ]
                if command == "finalize":
                    arguments += ["--output", str(temporary_root / "dirty.json")]
                else:
                    arguments += ["--attestation", str(ATTESTATION)]
                completed = subprocess.run(
                    arguments,
                    env=environment,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(completed.returncode, 2)
                self.assertIn("dirty libsmctrl is forbidden", completed.stderr)


class NativeSealedExecBehaviorTest(unittest.TestCase):
    def require_fixture(self) -> None:
        missing = [
            path
            for path in (EXEC_TEST_LAUNCHER, EXEC_TEST_REAL)
            if not path.is_file()
        ]
        if missing:
            self.skipTest(f"sealed-exec fixture has not been built: {missing}")

    def _fixture_environment(self, closed_fd: int) -> dict[str, str]:
        environment = _safe_environment()
        environment.update(INJECTION_ENVIRONMENT)
        environment.update(
            {
                "BURSTSERVE_PARENT_PID": str(os.getpid()),
                "CUDA_VISIBLE_DEVICES": "",
                "CUDA_MPS_PIPE_DIRECTORY": "",
                "MASK_OFF": "17",
                "BURSTSERVE_TEST_EXPECT_CLOSED_FD": str(closed_fd),
            }
        )
        return environment

    def test_dynamic_fixture_observes_guard_env_signals_fds_and_seals(
        self,
    ) -> None:
        self.require_fixture()
        original_fd = os.open("/dev/null", os.O_RDONLY | os.O_CLOEXEC)
        inherited_fd = os.dup2(original_fd, 100, inheritable=True)
        os.close(original_fd)
        old_mask = signal.pthread_sigmask(
            signal.SIG_BLOCK,
            {signal.SIGHUP, signal.SIGINT, signal.SIGTERM},
        )
        try:
            completed = subprocess.run(
                [str(EXEC_TEST_LAUNCHER), "--mode", "global"],
                env=self._fixture_environment(inherited_fd),
                pass_fds=(inherited_fd,),
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
            os.close(inherited_fd)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["parent_guard"], signal.SIGKILL)
        self.assertTrue(report["signals_unblocked"])
        self.assertTrue(report["environment_sanitized"])
        self.assertTrue(report["business_environment_preserved"])
        self.assertTrue(report["inherited_fd_closed"])
        self.assertTrue(report["exec_seal"])

    def test_source_mutation_after_snapshot_executes_only_sealed_bytes(
        self,
    ) -> None:
        self.require_fixture()
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            launcher = temporary_root / EXEC_TEST_LAUNCHER.name
            real = Path(f"{launcher}.real")
            shutil.copy2(EXEC_TEST_LAUNCHER, launcher)
            shutil.copy2(EXEC_TEST_REAL, real)
            ready_read, ready_write = os.pipe()
            continue_read, continue_write = os.pipe()
            leak_source = os.open("/dev/null", os.O_RDONLY | os.O_CLOEXEC)
            leak_fd = os.dup2(leak_source, 100, inheritable=True)
            os.close(leak_source)
            environment = self._fixture_environment(leak_fd)
            environment["BURSTSERVE_TEST_READY_FD"] = str(ready_write)
            environment["BURSTSERVE_TEST_CONTINUE_FD"] = str(continue_read)
            process: subprocess.Popen[str] | None = None
            try:
                process = subprocess.Popen(
                    [str(launcher), "--mode", "global"],
                    env=environment,
                    pass_fds=(ready_write, continue_read, leak_fd),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                os.close(ready_write)
                ready_write = -1
                os.close(continue_read)
                continue_read = -1
                readable, _, _ = select.select([ready_read], [], [], 10)
                self.assertTrue(readable, "launcher did not seal its snapshot")
                self.assertEqual(os.read(ready_read, 1), b"R")

                real.chmod(0o700)
                real.write_bytes(b"mutated pathname source\n")
                real.chmod(0o500)
                os.write(continue_write, b"C")
                stdout, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, stderr)
                report = json.loads(stdout)
                self.assertEqual(report["status"], "ok")
                self.assertTrue(report["exec_seal"])
            finally:
                for descriptor in (
                    ready_read,
                    ready_write,
                    continue_read,
                    continue_write,
                    leak_fd,
                ):
                    if descriptor >= 0:
                        try:
                            os.close(descriptor)
                        except OSError:
                            pass
                if process is not None and process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)
                if process is not None:
                    for pipe in (process.stdout, process.stderr):
                        if pipe is not None:
                            pipe.close()

    def test_memfd_seal_and_execveat_faults_fail_closed(self) -> None:
        self.require_fixture()
        for fault in ("memfd", "seal", "execveat"):
            with self.subTest(fault=fault):
                environment = _safe_environment()
                environment["BURSTSERVE_TEST_SEALED_EXEC_FAULT"] = fault
                completed = subprocess.run(
                    [str(EXEC_TEST_LAUNCHER), "--mode", "baseline"],
                    env=environment,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(completed.returncode, 4)
                self.assertEqual(completed.stdout, "")
                self.assertTrue(
                    "sealed executable snapshot rejected" in completed.stderr
                    or "execveat failed closed" in completed.stderr,
                    completed.stderr,
                )


class NativeParentGuardCpuBehaviorTest(unittest.TestCase):
    def require_helper(self) -> None:
        if not HELPER.is_file():
            self.skipTest("CPU-only native parent-guard helper has not been built")

    def run_helper(
        self,
        argument: str,
        *,
        expected_parent: str | None,
        extra_environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        environment = _safe_environment()
        if expected_parent is None:
            environment.pop("BURSTSERVE_PARENT_PID", None)
        else:
            environment["BURSTSERVE_PARENT_PID"] = expected_parent
        if extra_environment:
            environment.update(extra_environment)
        return subprocess.run(
            [str(HELPER), argument],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_valid_parent_arms_with_no_gpu_visibility(self) -> None:
        self.require_helper()
        completed = self.run_helper(
            "--arm-and-exit",
            expected_parent=str(os.getpid()),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn(f"parent={os.getpid()}", completed.stdout)
        self.assertIn(f"signal={signal.SIGKILL}", completed.stdout)
        self.assertTrue(completed.stdout.endswith(" cuda=\n"), completed.stdout)

    def test_invalid_overflow_and_missing_parent_pid_fail_closed(self) -> None:
        self.require_helper()
        invalid_values: list[str | None] = [
            None,
            "",
            "0",
            "-1",
            "+1",
            " 1",
            "1 ",
            str(1 << 31),
            "9" * 200,
        ]
        for value in invalid_values:
            with self.subTest(value=value):
                completed = self.run_helper(
                    "--arm-and-exit",
                    expected_parent=value,
                )
                self.assertEqual(completed.returncode, 4)
                expected_status = (
                    "missing_env" if value in (None, "") else "invalid_env"
                )
                self.assertIn(expected_status, completed.stderr)
                self.assertNotIn("armed pid=", completed.stdout)

    def test_mismatched_parent_pid_fails_closed(self) -> None:
        self.require_helper()
        completed = self.run_helper(
            "--arm-and-exit",
            expected_parent=str(os.getpid() + 1),
        )
        self.assertEqual(completed.returncode, 4)
        self.assertIn("parent_mismatch", completed.stderr)
        self.assertNotIn("armed pid=", completed.stdout)

    def test_static_helper_sanitizes_all_injection_environment(self) -> None:
        self.require_helper()
        completed = self.run_helper(
            "--sanitize-environment",
            expected_parent=None,
            extra_environment=INJECTION_ENVIRONMENT,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            completed.stdout.splitlines(),
            [
                "LD_PRELOAD=absent",
                "LD_AUDIT=absent",
                "LD_LIBRARY_PATH=absent",
                "GLIBC_TUNABLES=absent",
                "GCONV_PATH=absent",
                "LOCPATH=absent",
                "NLSPATH=absent",
                "CUDA_INJECTION32_PATH=absent",
                "CUDA_INJECTION64_PATH=absent",
                "CUDA_VISIBLE_DEVICES=",
            ],
        )

    def test_armed_child_is_sigkilled_when_supervisor_dies(self) -> None:
        self.require_helper()
        libc = ctypes.CDLL(None, use_errno=True)
        prctl = libc.prctl
        prctl.argtypes = [
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
        ]
        prctl.restype = ctypes.c_int
        previous = ctypes.c_int()
        self.assertEqual(
            prctl(
                PR_GET_CHILD_SUBREAPER,
                ctypes.addressof(previous),
                0,
                0,
                0,
            ),
            0,
        )
        self.assertEqual(prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0), 0)

        supervisor_code = (
            "import os, subprocess, sys, time\n"
            "environment = os.environ.copy()\n"
            "environment['BURSTSERVE_PARENT_PID'] = str(os.getpid())\n"
            "environment['CUDA_VISIBLE_DEVICES'] = ''\n"
            "environment['CUDA_MPS_PIPE_DIRECTORY'] = ''\n"
            "child = subprocess.Popen(\n"
            "    [sys.argv[1], '--arm-and-wait'],\n"
            "    env=environment,\n"
            "    stdout=subprocess.PIPE,\n"
            "    stderr=subprocess.PIPE,\n"
            "    text=True,\n"
            ")\n"
            "ready = child.stdout.readline().strip()\n"
            "print(f'{child.pid}|{ready}', flush=True)\n"
            "while True:\n"
            "    time.sleep(60)\n"
        )
        supervisor: subprocess.Popen[str] | None = None
        child_pid: int | None = None
        child_identity: str | None = None
        child_reaped = False
        try:
            supervisor = subprocess.Popen(
                [
                    *_isolated_python_command(sys.executable),
                    "-c",
                    supervisor_code,
                    str(HELPER),
                ],
                env=_safe_environment(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            self.assertIsNotNone(supervisor.stdout)
            readable, _, _ = select.select([supervisor.stdout], [], [], 10)
            self.assertTrue(readable, "supervisor did not report an armed child")
            line = supervisor.stdout.readline().strip()
            pid_text, separator, ready = line.partition("|")
            self.assertEqual(separator, "|", line)
            child_pid = int(pid_text)
            child_identity = _process_start_identity(child_pid)
            self.assertIsNotNone(child_identity)
            self.assertIn("armed pid=", ready)
            self.assertTrue(ready.endswith(" cuda="), ready)

            supervisor.kill()
            supervisor.wait(timeout=10)
            self.assertEqual(supervisor.returncode, -signal.SIGKILL)

            deadline = time.monotonic() + 10
            child_status: int | None = None
            while time.monotonic() < deadline:
                try:
                    waited_pid, status = os.waitpid(child_pid, os.WNOHANG)
                except ChildProcessError:
                    time.sleep(0.01)
                    continue
                if waited_pid == child_pid:
                    child_status = status
                    child_reaped = True
                    break
                time.sleep(0.01)
            self.assertIsNotNone(child_status, "guarded child survived parent death")
            assert child_status is not None
            self.assertTrue(os.WIFSIGNALED(child_status))
            self.assertEqual(os.WTERMSIG(child_status), signal.SIGKILL)
        finally:
            if supervisor is not None and supervisor.poll() is None:
                supervisor.kill()
                supervisor.wait(timeout=10)
            if (
                child_pid is not None
                and child_identity is not None
                and not child_reaped
            ):
                _kill_if_same_process(child_pid, child_identity)
                try:
                    os.waitpid(child_pid, 0)
                except ChildProcessError:
                    pass
            if supervisor is not None:
                for pipe in (supervisor.stdout, supervisor.stderr):
                    if pipe is not None:
                        pipe.close()
            self.assertEqual(
                prctl(PR_SET_CHILD_SUBREAPER, previous.value, 0, 0, 0),
                0,
            )


if __name__ == "__main__":
    unittest.main()
