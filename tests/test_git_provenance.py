from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
import zlib
from unittest import mock

import burstserve.git_provenance as provenance
from burstserve.git_provenance import SnapshotLimits, capture_repository


GIT = Path("/usr/bin/git")


def _run_git(
    repository: Path,
    *arguments: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C",
    }
    return subprocess.run(
        [str(GIT), "-C", str(repository), *arguments],
        check=check,
        capture_output=True,
        text=True,
        env=environment,
    )


def _init_repository(path: Path, *, object_format: str = "sha1") -> None:
    path.mkdir(parents=True)
    arguments = ["init", "--quiet"]
    if object_format != "sha1":
        arguments.append(f"--object-format={object_format}")
    _run_git(path, *arguments)
    _run_git(path, "config", "user.name", "Provenance Test")
    _run_git(path, "config", "user.email", "provenance@example.invalid")


def _commit_all(repository: Path, message: str = "fixture") -> str:
    _run_git(repository, "add", "--all")
    _run_git(repository, "commit", "--quiet", "-m", message)
    return _run_git(repository, "rev-parse", "HEAD").stdout.strip()


def _make_basic_repository(
    path: Path,
    *,
    object_format: str = "sha1",
) -> str:
    _init_repository(path, object_format=object_format)
    (path / "tracked.txt").write_bytes(b"base\n")
    return _commit_all(path)


def _replace_loose_object_with_valid_payload(
    repository: Path,
    oid: str,
    object_type: str,
    payload: bytes,
) -> None:
    object_path = (
        repository / ".git" / "objects" / oid[:2] / oid[2:]
    )
    object_path.chmod(0o600)
    header = f"{object_type} {len(payload)}\0".encode("ascii")
    object_path.write_bytes(zlib.compress(header + payload))


class GitProvenanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_clean_sha1_and_sha256_repositories(self) -> None:
        for object_format, oid_length in (("sha1", 40), ("sha256", 64)):
            with self.subTest(object_format=object_format):
                repository = self.root / object_format
                expected_head = _make_basic_repository(
                    repository,
                    object_format=object_format,
                )

                snapshot = capture_repository(repository)

                self.assertTrue(snapshot.complete, snapshot.errors)
                self.assertTrue(snapshot.clean, snapshot.to_dict())
                self.assertEqual(snapshot.object_format, object_format)
                self.assertEqual(snapshot.head_oid, expected_head)
                self.assertEqual(len(snapshot.head_oid or ""), oid_length)
                state = snapshot.path_state("tracked.txt")
                self.assertEqual(state["head"]["oid"], state["worktree"]["git_oid"])
                self.assertEqual(
                    snapshot.identity_sha256,
                    snapshot.to_dict()["identity_sha256"],
                )

    def test_staged_unstaged_mode_symlink_and_untracked_are_raw(self) -> None:
        repository = self.root / "repo"
        _init_repository(repository)
        (repository / "tracked.txt").write_bytes(b"base\n")
        (repository / "link").symlink_to("first")
        _commit_all(repository)

        (repository / "tracked.txt").write_bytes(b"index\n")
        (repository / "link").unlink()
        (repository / "link").symlink_to("second")
        _run_git(repository, "add", "tracked.txt", "link")
        (repository / "tracked.txt").write_bytes(b"worktree\n")
        os.chmod(repository / "tracked.txt", 0o754)
        (repository / "ignored.bin").write_bytes(b"ignored but observable")
        (repository / ".gitignore").write_text("ignored.bin\n")

        snapshot = capture_repository(repository)

        self.assertTrue(snapshot.complete, snapshot.errors)
        self.assertFalse(snapshot.clean)
        self.assertIn(
            b"tracked.txt",
            {change.path for change in snapshot.staged_changes},
        )
        unstaged = {
            change.path: change.kind for change in snapshot.unstaged_changes
        }
        self.assertIn(b"tracked.txt", unstaged)
        self.assertIn("content", unstaged[b"tracked.txt"])
        self.assertIn("mode", unstaged[b"tracked.txt"])
        self.assertIn(
            b".gitignore",
            {entry.path for entry in snapshot.untracked_entries},
        )
        self.assertIn(
            b"ignored.bin",
            {entry.path for entry in snapshot.untracked_entries},
        )
        link_state = snapshot.path_state("link")
        self.assertEqual(link_state["index"]["mode"], "120000")
        self.assertEqual(link_state["worktree"]["symlink_target"]["display"], "second")

    def test_allowed_untracked_roots_are_explicit_and_cannot_hide_source(self) -> None:
        repository = self.root / "repo"
        _init_repository(repository)
        (repository / "src").mkdir()
        (repository / "src" / "app.py").write_text("VALUE = 1\n")
        (repository / ".gitignore").write_text(
            "/artifacts/\n/build/\n**/__pycache__/\n*.tar\n"
        )
        _commit_all(repository)
        (repository / "artifacts").mkdir()
        (repository / "artifacts" / "result.bin").write_bytes(b"inert output")

        strict = capture_repository(repository)
        allowed = capture_repository(
            repository,
            allowed_untracked_roots=("artifacts",),
        )

        self.assertTrue(strict.complete, strict.errors)
        self.assertFalse(strict.clean)
        self.assertTrue(allowed.complete, allowed.errors)
        self.assertTrue(allowed.clean, allowed.to_dict())
        self.assertEqual(
            allowed.allowed_untracked_roots,
            (b"artifacts",),
        )
        self.assertIn(b"src", allowed.protected_roots)

        (repository / "src" / "injected.py").write_text("raise SystemExit\n")
        injected = capture_repository(
            repository,
            allowed_untracked_roots=("artifacts",),
        )
        self.assertFalse(injected.clean)
        self.assertIn(
            b"src/injected.py",
            {entry.path for entry in injected.untracked_entries},
        )
        (repository / "src" / "injected.py").unlink()

        no_protection = capture_repository(
            repository,
            allowed_untracked_roots=("artifacts",),
            protected_roots=(),
        )
        self.assertFalse(no_protection.complete)
        self.assertIn("protected-root policy", " ".join(no_protection.errors))

        tracked_overlap = capture_repository(
            repository,
            allowed_untracked_roots=("artifacts", "src"),
            protected_roots=("native",),
        )
        self.assertFalse(tracked_overlap.complete)
        self.assertIn("HEAD/index path", " ".join(tracked_overlap.errors))

        (repository / "build").mkdir()
        (repository / "build" / "artifact.bin").write_bytes(b"executable output")
        build = capture_repository(
            repository,
            allowed_untracked_roots=("artifacts", "build"),
        )
        self.assertFalse(build.complete)
        self.assertIn("protected root", " ".join(build.errors))
        (repository / "build" / "artifact.bin").unlink()
        (repository / "build").rmdir()

        (repository / "src" / "__pycache__").mkdir()
        (repository / "src" / "__pycache__" / "app.pyc").write_bytes(b"cache")
        import_cache = capture_repository(
            repository,
            allowed_untracked_roots=("artifacts", "src/__pycache__"),
        )
        self.assertFalse(import_cache.complete)
        self.assertIn("protected root", " ".join(import_cache.errors))
        (repository / "src" / "__pycache__" / "app.pyc").unlink()
        (repository / "src" / "__pycache__").rmdir()

        outside = self.root / "outside"
        outside.mkdir()
        (repository / "cache-link").symlink_to(outside, target_is_directory=True)
        symlink_root = capture_repository(
            repository,
            allowed_untracked_roots=(
                "artifacts",
                "cache-link",
            ),
        )
        self.assertFalse(symlink_root.complete)
        self.assertIn("unsupported type", " ".join(symlink_root.errors))
        (repository / "cache-link").unlink()

        (repository / "archive.tar").write_bytes(b"separately attested archive")
        regular_default = capture_repository(
            repository,
            allowed_untracked_roots=("archive.tar", "artifacts"),
        )
        self.assertFalse(regular_default.complete)
        self.assertIn("explicit opt-in", " ".join(regular_default.errors))
        regular_opt_in = capture_repository(
            repository,
            allowed_untracked_roots=("archive.tar", "artifacts"),
            allow_untracked_regular_files=True,
        )
        self.assertTrue(regular_opt_in.complete, regular_opt_in.errors)
        self.assertTrue(regular_opt_in.clean, regular_opt_in.to_dict())

    def _install_hiding_filter(
        self,
        repository: Path,
        *,
        attributes_path: Path,
    ) -> Path:
        marker = self.root / f"filter-{repository.name}.marker"
        script = self.root / f"filter-{repository.name}.sh"
        script.write_text(
            "#!/bin/sh\n"
            f"printf invoked >> {marker}\n"
            "printf 'base\\n'\n"
        )
        script.chmod(0o700)
        attributes_path.parent.mkdir(parents=True, exist_ok=True)
        attributes_path.write_text("tracked.txt filter=evil\n")
        _run_git(repository, "config", "filter.evil.clean", str(script))
        _run_git(repository, "config", "filter.evil.required", "true")
        return marker

    def test_head_attributes_and_local_clean_filter_cannot_hide_change(self) -> None:
        repository = self.root / "repo"
        _init_repository(repository)
        (repository / "tracked.txt").write_bytes(b"base\n")
        (repository / ".gitattributes").write_text("tracked.txt filter=evil\n")
        _commit_all(repository)
        marker = self._install_hiding_filter(
            repository,
            attributes_path=repository / ".gitattributes",
        )
        (repository / "tracked.txt").write_bytes(b"evil\n")

        legacy = _run_git(
            repository,
            "status",
            "--porcelain",
            "--untracked-files=no",
        )
        self.assertEqual(legacy.stdout, "")
        self.assertTrue(marker.exists(), "attack fixture did not execute")
        marker.unlink()

        snapshot = capture_repository(repository)

        self.assertFalse(marker.exists(), "repository clean filter executed")
        self.assertTrue(snapshot.complete, snapshot.errors)
        self.assertFalse(snapshot.clean)
        self.assertEqual(
            {change.path for change in snapshot.unstaged_changes},
            {b"tracked.txt"},
        )

    def test_info_attributes_and_local_clean_filter_cannot_hide_change(self) -> None:
        repository = self.root / "repo"
        _make_basic_repository(repository)
        info_attributes = Path(
            _run_git(
                repository,
                "rev-parse",
                "--path-format=absolute",
                "--git-path",
                "info/attributes",
            ).stdout.strip()
        )
        marker = self._install_hiding_filter(
            repository,
            attributes_path=info_attributes,
        )
        (repository / "tracked.txt").write_bytes(b"evil\n")

        legacy = _run_git(
            repository,
            "status",
            "--porcelain",
            "--untracked-files=no",
        )
        self.assertEqual(legacy.stdout, "")
        self.assertTrue(marker.exists(), "attack fixture did not execute")
        marker.unlink()

        snapshot = capture_repository(repository)

        self.assertFalse(marker.exists(), "info/attributes clean filter executed")
        self.assertTrue(snapshot.complete, snapshot.errors)
        self.assertFalse(snapshot.clean)
        self.assertEqual(
            {change.path for change in snapshot.unstaged_changes},
            {b"tracked.txt"},
        )

    def test_local_fsmonitor_command_is_not_executed(self) -> None:
        repository = self.root / "repo"
        _make_basic_repository(repository)
        marker = self.root / "fsmonitor.marker"
        script = self.root / "fsmonitor.sh"
        script.write_text(
            "#!/bin/sh\n"
            f"printf invoked >> {marker}\n"
            "printf '\\n'\n"
        )
        script.chmod(0o700)
        _run_git(repository, "config", "core.fsmonitor", str(script))

        _run_git(repository, "status", "--porcelain", check=False)
        self.assertTrue(marker.exists(), "fsmonitor attack fixture did not execute")
        marker.unlink()
        snapshot = capture_repository(repository)

        self.assertTrue(snapshot.complete, snapshot.errors)
        self.assertTrue(snapshot.clean, snapshot.to_dict())
        self.assertFalse(marker.exists(), "repository fsmonitor command executed")

    def test_registered_submodule_is_recursively_scanned_without_filters(self) -> None:
        child_source = self.root / "child-source"
        child_head = _make_basic_repository(child_source)
        repository = self.root / "super"
        _init_repository(repository)
        _run_git(
            repository,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "--quiet",
            str(child_source),
            "sub",
        )
        _commit_all(repository)
        child = repository / "sub"
        info_attributes = Path(
            _run_git(
                child,
                "rev-parse",
                "--path-format=absolute",
                "--git-path",
                "info/attributes",
            ).stdout.strip()
        )
        marker = self._install_hiding_filter(
            child,
            attributes_path=info_attributes,
        )
        (child / "tracked.txt").write_bytes(b"evil\n")

        legacy = _run_git(
            repository,
            "status",
            "--porcelain",
            "--ignore-submodules=none",
            "--untracked-files=no",
        )
        self.assertEqual(legacy.stdout, "")
        self.assertTrue(marker.exists(), "submodule attack fixture did not execute")
        marker.unlink()

        unregistered = capture_repository(repository)
        snapshot = capture_repository(
            repository,
            expected_gitlinks={"sub": child_head},
        )

        self.assertFalse(unregistered.complete)
        self.assertFalse(marker.exists(), "submodule clean filter executed")
        self.assertTrue(snapshot.complete, snapshot.errors)
        self.assertFalse(snapshot.clean)
        self.assertEqual(len(snapshot.gitlinks), 1)
        self.assertFalse(snapshot.gitlinks[0].clean)
        self.assertFalse(snapshot.gitlinks[0].snapshot.clean)

    def test_linked_worktree_and_packed_refs(self) -> None:
        repository = self.root / "repo"
        _make_basic_repository(repository)
        _run_git(repository, "pack-refs", "--all", "--prune")
        packed = capture_repository(repository)
        self.assertTrue(packed.complete, packed.errors)
        self.assertTrue(packed.clean, packed.to_dict())

        linked = self.root / "linked"
        _run_git(
            repository,
            "worktree",
            "add",
            "--quiet",
            "--detach",
            str(linked),
            "HEAD",
        )
        linked_snapshot = capture_repository(linked)
        self.assertTrue(linked_snapshot.complete, linked_snapshot.errors)
        self.assertTrue(linked_snapshot.clean, linked_snapshot.to_dict())
        self.assertNotEqual(linked_snapshot.git_dir, linked_snapshot.common_dir)

    def test_split_index_is_copied_into_private_git_directory(self) -> None:
        for object_format in ("sha1", "sha256"):
            with self.subTest(object_format=object_format):
                repository = self.root / f"repo-{object_format}"
                _make_basic_repository(
                    repository,
                    object_format=object_format,
                )
                _run_git(repository, "update-index", "--split-index")
                self.assertTrue(
                    list((repository / ".git").glob("sharedindex.*"))
                )

                snapshot = capture_repository(repository)

                self.assertTrue(snapshot.complete, snapshot.errors)
                self.assertTrue(snapshot.clean, snapshot.to_dict())

    def test_fifo_metadata_fails_promptly_and_loose_ref_never_falls_back(
        self,
    ) -> None:
        repository = self.root / "repo"
        _make_basic_repository(repository)

        for relative in (".git/config", ".git/index"):
            with self.subTest(relative=relative):
                path = repository / relative
                content = path.read_bytes()
                path.unlink()
                os.mkfifo(path)
                started = time.monotonic()
                snapshot = capture_repository(repository)
                elapsed = time.monotonic() - started
                self.assertFalse(snapshot.complete)
                self.assertLess(elapsed, 2.0)
                self.assertIn("not a regular file", " ".join(snapshot.errors))
                path.unlink()
                path.write_bytes(content)

        branch = _run_git(
            repository,
            "symbolic-ref",
            "--short",
            "HEAD",
        ).stdout.strip()
        _run_git(repository, "pack-refs", "--all", "--prune")
        loose = repository / ".git" / "refs" / "heads" / branch
        loose.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(loose)

        started = time.monotonic()
        malicious_loose = capture_repository(repository)
        elapsed = time.monotonic() - started

        self.assertFalse(malicious_loose.complete)
        self.assertLess(elapsed, 2.0)
        self.assertIn("not a regular file", " ".join(malicious_loose.errors))
        loose.unlink()
        packed_only = capture_repository(repository)
        self.assertTrue(packed_only.complete, packed_only.errors)
        self.assertTrue(packed_only.clean, packed_only.to_dict())

    def test_head_must_be_exact_commit_and_refs_use_strict_grammar(self) -> None:
        tagged = self.root / "tagged"
        _make_basic_repository(tagged)
        _run_git(tagged, "tag", "-a", "review-tag", "-m", "annotated")
        tag_oid = _run_git(
            tagged,
            "rev-parse",
            "refs/tags/review-tag",
        ).stdout.strip()
        (tagged / ".git" / "HEAD").write_text(tag_oid + "\n")

        tag_snapshot = capture_repository(tagged)

        self.assertFalse(tag_snapshot.complete)
        self.assertIn("expected 'commit'", " ".join(tag_snapshot.errors))

        dot_ref = self.root / "dot-ref"
        head = _make_basic_repository(dot_ref)
        hidden_ref = dot_ref / ".git" / "refs" / "heads" / ".hidden"
        hidden_ref.parent.mkdir(parents=True, exist_ok=True)
        hidden_ref.write_text(head + "\n")
        (dot_ref / ".git" / "HEAD").write_text(
            "ref: refs/heads/.hidden\n"
        )
        dot_snapshot = capture_repository(dot_ref)
        self.assertFalse(dot_snapshot.complete)
        self.assertIn("invalid symbolic ref", " ".join(dot_snapshot.errors))

        peeled = self.root / "peeled"
        _make_basic_repository(peeled)
        _run_git(peeled, "tag", "-a", "packed-tag", "-m", "annotated")
        _run_git(peeled, "pack-refs", "--all", "--prune")
        packed_refs = (peeled / ".git" / "packed-refs").read_bytes()
        self.assertIn(b"\n^", packed_refs)
        peeled_snapshot = capture_repository(peeled)
        self.assertFalse(peeled_snapshot.complete)
        self.assertIn("peeled packed-refs", " ".join(peeled_snapshot.errors))

    def test_batch_object_checks_reject_missing_corrupt_and_wrong_type(
        self,
    ) -> None:
        wrong_type = self.root / "wrong-type"
        _make_basic_repository(wrong_type)
        _run_git(
            wrong_type,
            "tag",
            "-a",
            "wrong-object",
            "-m",
            "wrong type",
        )
        tag_oid = _run_git(
            wrong_type,
            "rev-parse",
            "refs/tags/wrong-object",
        ).stdout.strip()
        _run_git(
            wrong_type,
            "update-index",
            "--add",
            "--cacheinfo",
            f"100644,{tag_oid},wrong.bin",
        )
        wrong_snapshot = capture_repository(wrong_type)
        self.assertFalse(wrong_snapshot.complete)
        self.assertIn("expected 'blob'", " ".join(wrong_snapshot.errors))

        missing = self.root / "missing"
        _make_basic_repository(missing)
        missing_path = missing / "missing.bin"
        missing_path.write_bytes(b"unique missing object\n")
        missing_oid = _run_git(
            missing,
            "hash-object",
            "-w",
            "missing.bin",
        ).stdout.strip()
        _run_git(
            missing,
            "update-index",
            "--add",
            "--cacheinfo",
            f"100644,{missing_oid},missing.bin",
        )
        missing_object = (
            missing / ".git" / "objects" / missing_oid[:2] / missing_oid[2:]
        )
        missing_object.unlink()
        missing_snapshot = capture_repository(missing)
        self.assertFalse(missing_snapshot.complete)
        self.assertIn(
            "missing or malformed Git object",
            " ".join(missing_snapshot.errors),
        )

        corrupt = self.root / "corrupt"
        _make_basic_repository(corrupt)
        corrupt_oid = _run_git(
            corrupt,
            "rev-parse",
            "HEAD:tracked.txt",
        ).stdout.strip()
        corrupt_object = (
            corrupt / ".git" / "objects" / corrupt_oid[:2] / corrupt_oid[2:]
        )
        corrupt_object.chmod(0o600)
        corrupt_object.write_bytes(b"not a zlib Git object")
        corrupt_snapshot = capture_repository(corrupt)
        self.assertFalse(corrupt_snapshot.complete)
        self.assertIn("unable to unpack", " ".join(corrupt_snapshot.errors))

    def test_batch_object_result_must_be_stable_across_raw_scans(self) -> None:
        repository = self.root / "repo"
        _make_basic_repository(repository)
        original_verify = provenance._verify_required_objects
        call_count = 0

        def changing_check(*args: object, **kwargs: object) -> object:
            nonlocal call_count
            result = original_verify(*args, **kwargs)
            call_count += 1
            if call_count == 2:
                first = result[0]
                return (
                    provenance._ObjectCheck(
                        oid=first.oid,
                        object_type=first.object_type,
                        size=first.size + 1,
                    ),
                    *result[1:],
                )
            return result

        with mock.patch.object(
            provenance,
            "_verify_required_objects",
            side_effect=changing_check,
        ):
            snapshot = capture_repository(
                repository,
                limits=SnapshotLimits(max_attempts=1),
            )

        self.assertFalse(snapshot.complete)
        self.assertIn("object state changed", " ".join(snapshot.errors))

    def test_exact_hash_rejects_valid_wrong_blob_and_commit_payloads(
        self,
    ) -> None:
        wrong_blob = self.root / "wrong-blob-payload"
        _make_basic_repository(wrong_blob)
        blob_oid = _run_git(
            wrong_blob,
            "rev-parse",
            "HEAD:tracked.txt",
        ).stdout.strip()
        _replace_loose_object_with_valid_payload(
            wrong_blob,
            blob_oid,
            "blob",
            b"evil\n",
        )
        fsck_blob = _run_git(
            wrong_blob,
            "fsck",
            "--no-dangling",
            check=False,
        )
        self.assertNotEqual(fsck_blob.returncode, 0)

        blob_snapshot = capture_repository(wrong_blob)

        self.assertFalse(blob_snapshot.complete)
        self.assertFalse(blob_snapshot.clean)
        self.assertIn(
            "content identity mismatch",
            " ".join(blob_snapshot.errors),
        )

        wrong_commit = self.root / "wrong-commit-payload"
        head_oid = _make_basic_repository(wrong_commit)
        commit_payload = _run_git(
            wrong_commit,
            "cat-file",
            "commit",
            head_oid,
        ).stdout.encode("utf-8")
        self.assertIn(b"fixture", commit_payload)
        wrong_commit_payload = commit_payload.replace(
            b"fixture",
            b"fixturE",
            1,
        )
        self.assertEqual(len(wrong_commit_payload), len(commit_payload))
        _replace_loose_object_with_valid_payload(
            wrong_commit,
            head_oid,
            "commit",
            wrong_commit_payload,
        )
        fsck_commit = _run_git(
            wrong_commit,
            "fsck",
            "--no-dangling",
            check=False,
        )
        self.assertNotEqual(fsck_commit.returncode, 0)

        commit_snapshot = capture_repository(wrong_commit)

        self.assertFalse(commit_snapshot.complete)
        self.assertFalse(commit_snapshot.clean)
        self.assertIn(
            "content identity mismatch",
            " ".join(commit_snapshot.errors),
        )

    def test_path_prefix_and_policy_resources_are_bounded(self) -> None:
        repository = self.root / "nested"
        _init_repository(repository)
        nested = repository / "a" / "b" / "c" / "d"
        nested.mkdir(parents=True)
        (nested / "f").write_text("content\n")
        _commit_all(repository)
        _run_git(repository, "checkout", "--quiet", "--detach", "HEAD")

        path_components = capture_repository(
            repository,
            protected_roots=(),
            limits=SnapshotLimits(max_path_components=3),
        )
        self.assertFalse(path_components.complete)
        self.assertIn("component count", " ".join(path_components.errors))

        path_bytes = capture_repository(
            repository,
            protected_roots=(),
            limits=SnapshotLimits(max_path_bytes=5),
        )
        self.assertFalse(path_bytes.complete)
        self.assertIn("path byte limit", " ".join(path_bytes.errors))

        prefix_count = capture_repository(
            repository,
            protected_roots=(),
            limits=SnapshotLimits(max_derived_prefixes=2),
        )
        self.assertFalse(prefix_count.complete)
        self.assertIn("prefix count", " ".join(prefix_count.errors))

        prefix_bytes = capture_repository(
            repository,
            protected_roots=(),
            limits=SnapshotLimits(max_derived_prefix_bytes=12),
        )
        self.assertFalse(prefix_bytes.complete)
        self.assertIn("prefix bytes", " ".join(prefix_bytes.errors))

        policy_count = capture_repository(
            repository,
            allowed_untracked_roots=("one", "two"),
            protected_roots=("protected",),
            limits=SnapshotLimits(max_policy_entries=1),
        )
        self.assertFalse(policy_count.complete)
        self.assertIn("count limit", " ".join(policy_count.errors))

        policy_bytes = capture_repository(
            repository,
            allowed_untracked_roots=("long-output",),
            protected_roots=("p",),
            limits=SnapshotLimits(max_policy_path_bytes=5),
        )
        self.assertFalse(policy_bytes.complete)
        self.assertIn("aggregate path bytes", " ".join(policy_bytes.errors))

        expected_count = capture_repository(
            repository,
            expected_gitlinks={"one": None, "two": None},
            protected_roots=(),
            limits=SnapshotLimits(max_policy_entries=1),
        )
        self.assertFalse(expected_count.complete)
        self.assertIn("gitlink count", " ".join(expected_count.errors))

        class LyingMapping(dict[str, None]):
            def __len__(self) -> int:
                return 0

        lying_expected = LyingMapping({"one": None, "two": None})
        lying_count = capture_repository(
            repository,
            expected_gitlinks=lying_expected,
            protected_roots=(),
            limits=SnapshotLimits(max_policy_entries=1),
        )
        self.assertFalse(lying_count.complete)
        self.assertIn("iteration count", " ".join(lying_count.errors))

        object_count = capture_repository(
            repository,
            protected_roots=(),
            limits=SnapshotLimits(max_object_checks=1),
        )
        self.assertFalse(object_count.complete)
        self.assertIn("object check count", " ".join(object_count.errors))

    def test_sparse_index_reftable_config_include_and_alternates_fail_closed(
        self,
    ) -> None:
        sparse = self.root / "sparse"
        _init_repository(sparse)
        (sparse / "a").mkdir()
        (sparse / "b").mkdir()
        (sparse / "a" / "one.txt").write_text("one\n")
        (sparse / "b" / "two.txt").write_text("two\n")
        _commit_all(sparse)
        _run_git(sparse, "sparse-checkout", "init", "--cone", "--sparse-index")
        _run_git(sparse, "sparse-checkout", "set", "a")
        sparse_snapshot = capture_repository(sparse)
        self.assertFalse(sparse_snapshot.complete)
        self.assertTrue(
            any(
                marker in " ".join(sparse_snapshot.errors)
                for marker in ("sparse index", "repository format")
            ),
            sparse_snapshot.errors,
        )

        reftable = self.root / "reftable"
        reftable.mkdir()
        initialized = _run_git(
            reftable,
            "init",
            "--quiet",
            "--ref-format=reftable",
            check=False,
        )
        if initialized.returncode == 0:
            _run_git(reftable, "config", "user.name", "Provenance Test")
            _run_git(
                reftable,
                "config",
                "user.email",
                "provenance@example.invalid",
            )
            (reftable / "tracked.txt").write_text("base\n")
            _commit_all(reftable)
            reftable_snapshot = capture_repository(reftable)
            self.assertFalse(reftable_snapshot.complete)
            self.assertIn("reference storage", " ".join(reftable_snapshot.errors))

        included = self.root / "included"
        _make_basic_repository(included)
        _run_git(included, "config", "include.path", str(self.root / "unused"))
        included_snapshot = capture_repository(included)
        self.assertFalse(included_snapshot.complete)
        self.assertIn("includes", " ".join(included_snapshot.errors))

        alternate = self.root / "alternate"
        _make_basic_repository(alternate)
        (alternate / ".git" / "objects" / "info" / "alternates").write_text(
            str(included / ".git" / "objects") + "\n"
        )
        alternate_snapshot = capture_repository(alternate)
        self.assertFalse(alternate_snapshot.complete)
        self.assertIn("alternates", " ".join(alternate_snapshot.errors))

    def test_resource_limits_and_persistent_instability_fail_closed(self) -> None:
        repository = self.root / "repo"
        _make_basic_repository(repository)

        limited = capture_repository(
            repository,
            limits=SnapshotLimits(max_git_output_bytes=1),
        )
        self.assertFalse(limited.complete)
        self.assertFalse(limited.clean)
        self.assertIn("limit", " ".join(limited.errors))

        original_scan = provenance._scan_worktree
        tracked = repository / "tracked.txt"

        def scan_then_toggle(*args: object, **kwargs: object) -> object:
            result = original_scan(*args, **kwargs)
            current = tracked.read_bytes()
            tracked.write_bytes(b"next\n" if current == b"base\n" else b"base\n")
            return result

        with mock.patch.object(
            provenance,
            "_scan_worktree",
            side_effect=scan_then_toggle,
        ):
            unstable = capture_repository(
                repository,
                limits=SnapshotLimits(max_attempts=2),
            )
        self.assertFalse(unstable.complete)
        self.assertFalse(unstable.clean)
        self.assertEqual(unstable.attempts, 2)
        self.assertIn("changed between A/B", " ".join(unstable.errors))

    def test_cross_object_format_submodule_fails_closed(self) -> None:
        child_source = self.root / "sha256-child"
        _make_basic_repository(
            child_source,
            object_format="sha256",
        )
        repository = self.root / "sha1-super"
        _init_repository(repository)
        added = _run_git(
            repository,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "--quiet",
            str(child_source),
            "sub",
            check=False,
        )
        if added.returncode != 0:
            self.skipTest("installed Git refuses cross-format submodule fixtures")
        _commit_all(repository)

        snapshot = capture_repository(
            repository,
            expected_gitlinks={"sub": None},
        )

        self.assertFalse(snapshot.complete)
        self.assertFalse(snapshot.clean)
        self.assertEqual(len(snapshot.gitlinks), 1)
        self.assertFalse(snapshot.gitlinks[0].object_format_matches)


if __name__ == "__main__":
    unittest.main()
