"""Safely import and verify the immutable ASLE baseline.

The tree digest intentionally matches the command originally used to create
``vendor/ASLE_SOURCE.json``::

    find vendor/asle -type f -print0 | sort -z \
        | xargs -0 sha256sum | sha256sum

In other words, regular files are sorted by their repository-relative POSIX
path, each inner line is ``<file-sha256>  <path>\\n``, and the concatenation of
those lines is hashed once more.  The stable ``vendor/asle`` destination is
part of this representation.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
import tarfile
import tempfile
from typing import Any, BinaryIO, Mapping, Sequence


MANIFEST_RELATIVE_PATH = Path("vendor/ASLE_SOURCE.json")
IMPORTED_RELATIVE_PATH = PurePosixPath("vendor/asle")
TREE_HASH_ALGORITHM = "sha256sum-lines-vendor-asle-v1"
_COPY_CHUNK_BYTES = 1024 * 1024


class VendorImportError(RuntimeError):
    """Raised when the manifest, archive, or imported tree is invalid."""


@dataclass(frozen=True)
class SourceManifest:
    """Validated fields from ``vendor/ASLE_SOURCE.json``."""

    source_archive: PurePosixPath
    archive_sha256: str
    archive_size_bytes: int
    archive_top_level: str
    imported_path: PurePosixPath
    imported_file_count: int
    imported_tree_sha256: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SourceManifest":
        if value.get("schema_version") != 1:
            raise VendorImportError("manifest schema_version must be 1")

        source_archive = _safe_relative_path(
            value.get("source_archive"), "source_archive"
        )
        imported_path = _safe_relative_path(
            value.get("imported_path"), "imported_path"
        )
        if imported_path != IMPORTED_RELATIVE_PATH:
            raise VendorImportError(
                "imported_path must be the stable destination 'vendor/asle'"
            )

        archive_top_level = value.get("archive_top_level")
        if (
            not isinstance(archive_top_level, str)
            or not archive_top_level
            or PurePosixPath(archive_top_level).parts != (archive_top_level,)
            or archive_top_level in {".", ".."}
        ):
            raise VendorImportError(
                "archive_top_level must be one safe path component"
            )

        archive_sha256 = _validated_digest(
            value.get("archive_sha256"), "archive_sha256"
        )
        tree_sha256 = _validated_digest(
            value.get("imported_tree_sha256"), "imported_tree_sha256"
        )
        archive_size = _nonnegative_integer(
            value.get("archive_size_bytes"), "archive_size_bytes"
        )
        file_count = _nonnegative_integer(
            value.get("imported_file_count"), "imported_file_count"
        )

        return cls(
            source_archive=source_archive,
            archive_sha256=archive_sha256,
            archive_size_bytes=archive_size,
            archive_top_level=archive_top_level,
            imported_path=imported_path,
            imported_file_count=file_count,
            imported_tree_sha256=tree_sha256,
        )


@dataclass(frozen=True)
class VerificationResult:
    """Hashes and counts established by an import or verification."""

    archive_sha256: str
    archive_size_bytes: int
    tree_sha256: str
    file_count: int

    def to_dict(self, *, mode: str) -> dict[str, Any]:
        return {
            "archive_sha256": self.archive_sha256,
            "archive_size_bytes": self.archive_size_bytes,
            "file_count": self.file_count,
            "mode": mode,
            "tree_hash_algorithm": TREE_HASH_ALGORITHM,
            "tree_sha256": self.tree_sha256,
        }


def _safe_relative_path(value: Any, field: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise VendorImportError(f"{field} must be a non-empty string")
    if "\\" in value:
        raise VendorImportError(f"{field} must use POSIX path separators")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise VendorImportError(f"{field} must be a safe relative path")
    return path


def _validated_digest(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdefABCDEF" for character in value)
    ):
        raise VendorImportError(f"{field} must be a 64-character SHA-256 digest")
    return value.lower()


def _nonnegative_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise VendorImportError(f"{field} must be a non-negative integer")
    return value


def load_manifest(repo_root: Path) -> SourceManifest:
    """Read and validate the fixed ASLE source manifest."""

    manifest_path = repo_root / MANIFEST_RELATIVE_PATH
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise VendorImportError(f"manifest not found: {manifest_path}") from error
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise VendorImportError(f"cannot read manifest {manifest_path}: {error}") from error
    if not isinstance(value, dict):
        raise VendorImportError("manifest root must be a JSON object")
    return SourceManifest.from_mapping(value)


def _hash_stream(stream: BinaryIO) -> str:
    digest = hashlib.sha256()
    while chunk := stream.read(_COPY_CHUNK_BYTES):
        digest.update(chunk)
    return digest.hexdigest()


def hash_file(path: Path) -> str:
    """Return a streaming SHA-256 digest for a regular file."""

    try:
        with path.open("rb") as stream:
            return _hash_stream(stream)
    except OSError as error:
        raise VendorImportError(f"cannot hash {path}: {error}") from error


def _path_sort_key(path: PurePosixPath) -> bytes:
    return path.as_posix().encode("utf-8")


def tree_digest(repo_root: Path, imported_path: PurePosixPath) -> tuple[str, int]:
    """Compute the manifest-compatible digest of an imported regular-file tree."""

    tree_root = repo_root.joinpath(*imported_path.parts)
    try:
        root_status = tree_root.lstat()
    except FileNotFoundError as error:
        raise VendorImportError(f"imported tree not found: {tree_root}") from error
    except OSError as error:
        raise VendorImportError(f"cannot inspect imported tree {tree_root}: {error}") from error
    if not stat.S_ISDIR(root_status.st_mode):
        raise VendorImportError(f"imported tree is not a directory: {tree_root}")

    regular_files: list[tuple[PurePosixPath, Path]] = []
    for directory, directory_names, file_names in os.walk(
        tree_root, topdown=True, followlinks=False
    ):
        directory_path = Path(directory)
        for name in tuple(directory_names):
            child = directory_path / name
            child_status = child.lstat()
            if stat.S_ISLNK(child_status.st_mode):
                raise VendorImportError(f"symlink in imported tree: {child}")
            if not stat.S_ISDIR(child_status.st_mode):
                raise VendorImportError(f"special entry in imported tree: {child}")
        for name in file_names:
            child = directory_path / name
            child_status = child.lstat()
            if not stat.S_ISREG(child_status.st_mode):
                kind = "symlink" if stat.S_ISLNK(child_status.st_mode) else "special entry"
                raise VendorImportError(f"{kind} in imported tree: {child}")
            repository_relative = PurePosixPath(
                child.relative_to(repo_root).as_posix()
            )
            regular_files.append((repository_relative, child))

    outer_digest = hashlib.sha256()
    for repository_relative, path in sorted(
        regular_files, key=lambda item: _path_sort_key(item[0])
    ):
        inner_digest = hash_file(path)
        outer_digest.update(
            f"{inner_digest}  {repository_relative.as_posix()}\n".encode("utf-8")
        )
    return outer_digest.hexdigest(), len(regular_files)


def _resolve_source_archive(repo_root: Path, relative: PurePosixPath) -> Path:
    archive = repo_root.joinpath(*relative.parts)
    try:
        resolved_root = repo_root.resolve(strict=True)
        resolved_archive = archive.resolve(strict=True)
    except OSError as error:
        raise VendorImportError(f"cannot resolve source archive {archive}: {error}") from error
    if not resolved_archive.is_relative_to(resolved_root):
        raise VendorImportError("source archive resolves outside the repository")
    if not resolved_archive.is_file():
        raise VendorImportError(f"source archive is not a regular file: {archive}")
    return archive


def verify_archive(repo_root: Path, manifest: SourceManifest) -> Path:
    """Verify archive size and digest, returning its repository path."""

    archive = _resolve_source_archive(repo_root, manifest.source_archive)
    try:
        observed_size = archive.stat().st_size
    except OSError as error:
        raise VendorImportError(f"cannot stat source archive {archive}: {error}") from error
    if observed_size != manifest.archive_size_bytes:
        raise VendorImportError(
            "archive size mismatch: "
            f"expected {manifest.archive_size_bytes}, observed {observed_size}"
        )
    observed_digest = hash_file(archive)
    if observed_digest != manifest.archive_sha256:
        raise VendorImportError(
            "archive SHA-256 mismatch: "
            f"expected {manifest.archive_sha256}, observed {observed_digest}"
        )
    return archive


def _validated_member_path(
    member: tarfile.TarInfo, top_level: str
) -> PurePosixPath:
    raw_name = member.name.rstrip("/")
    if not raw_name or raw_name.startswith("/") or "\\" in raw_name:
        raise VendorImportError(f"unsafe archive member path: {member.name!r}")
    raw_parts = raw_name.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise VendorImportError(f"unsafe archive member path: {member.name!r}")
    path = PurePosixPath(*raw_parts)
    if path.parts[0] != top_level:
        raise VendorImportError(
            f"archive member is outside top-level {top_level!r}: {member.name!r}"
        )
    if member.issym() or member.islnk():
        raise VendorImportError(
            f"archive links are not permitted: {member.name!r} -> {member.linkname!r}"
        )
    if not (member.isdir() or member.isreg()):
        raise VendorImportError(
            f"archive special entry is not permitted: {member.name!r}"
        )
    return path


def validated_members(
    archive: tarfile.TarFile, top_level: str
) -> list[tuple[tarfile.TarInfo, PurePosixPath]]:
    """Validate every member before any archive content is written."""

    result: list[tuple[tarfile.TarInfo, PurePosixPath]] = []
    seen: set[PurePosixPath] = set()
    for member in archive.getmembers():
        path = _validated_member_path(member, top_level)
        if path in seen:
            raise VendorImportError(f"duplicate archive member: {member.name!r}")
        seen.add(path)
        result.append((member, path))
    if not result:
        raise VendorImportError("source archive is empty")
    return result


def inspect_archive(archive_path: Path, top_level: str) -> None:
    """Open and fully validate archive metadata without extracting it."""

    try:
        with tarfile.open(archive_path, mode="r:*") as archive:
            validated_members(archive, top_level)
    except (tarfile.TarError, OSError) as error:
        raise VendorImportError(f"cannot inspect source archive: {error}") from error


def _extract_validated_archive(
    archive_path: Path, staging_root: Path, top_level: str
) -> Path:
    try:
        with tarfile.open(archive_path, mode="r:*") as archive:
            members = validated_members(archive, top_level)
            for member, member_path in members:
                destination = staging_root.joinpath(*member_path.parts)
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue

                destination.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise VendorImportError(
                        f"cannot read regular archive member: {member.name!r}"
                    )
                with source, destination.open("xb") as output:
                    shutil.copyfileobj(source, output, length=_COPY_CHUNK_BYTES)
                os.chmod(destination, member.mode & 0o777)
    except VendorImportError:
        raise
    except (tarfile.TarError, OSError) as error:
        raise VendorImportError(f"cannot extract source archive: {error}") from error

    extracted_top = staging_root / top_level
    if not extracted_top.is_dir():
        raise VendorImportError(
            f"archive does not contain top-level directory {top_level!r}"
        )
    return extracted_top


def _assert_expected_tree(
    result: tuple[str, int], manifest: SourceManifest
) -> VerificationResult:
    tree_sha256, file_count = result
    if file_count != manifest.imported_file_count:
        raise VendorImportError(
            "imported file count mismatch: "
            f"expected {manifest.imported_file_count}, observed {file_count}"
        )
    if tree_sha256 != manifest.imported_tree_sha256:
        raise VendorImportError(
            "imported tree SHA-256 mismatch: "
            f"expected {manifest.imported_tree_sha256}, observed {tree_sha256}"
        )
    return VerificationResult(
        archive_sha256=manifest.archive_sha256,
        archive_size_bytes=manifest.archive_size_bytes,
        tree_sha256=tree_sha256,
        file_count=file_count,
    )


def verify(repo_root: Path) -> VerificationResult:
    """Verify the source archive, archive safety, and immutable imported tree."""

    repo_root = repo_root.resolve(strict=True)
    manifest = load_manifest(repo_root)
    archive_path = verify_archive(repo_root, manifest)
    inspect_archive(archive_path, manifest.archive_top_level)
    return _assert_expected_tree(
        tree_digest(repo_root, manifest.imported_path), manifest
    )


def _move_staged_tree_without_overwrite(staged: Path, destination: Path) -> None:
    """Publish a staged tree while refusing any pre-existing destination."""

    try:
        destination.mkdir(parents=False, exist_ok=False)
    except FileExistsError as error:
        raise VendorImportError(
            f"destination already exists; refusing to overwrite: {destination}"
        ) from error
    except OSError as error:
        raise VendorImportError(f"cannot create destination {destination}: {error}") from error

    try:
        for child in staged.iterdir():
            shutil.move(str(child), str(destination / child.name))
    except BaseException:
        shutil.rmtree(destination)
        raise


def import_baseline(repo_root: Path) -> VerificationResult:
    """Safely import ASLE when and only when ``vendor/asle`` is absent."""

    repo_root = repo_root.resolve(strict=True)
    manifest = load_manifest(repo_root)
    destination = repo_root.joinpath(*manifest.imported_path.parts)
    if destination.exists() or destination.is_symlink():
        raise VendorImportError(
            f"destination already exists; refusing to overwrite: {destination}"
        )

    archive_path = verify_archive(repo_root, manifest)
    destination_parent = destination.parent
    try:
        parent_status = destination_parent.lstat()
    except OSError as error:
        raise VendorImportError(
            f"cannot inspect destination parent {destination_parent}: {error}"
        ) from error
    if not stat.S_ISDIR(parent_status.st_mode):
        raise VendorImportError(
            f"destination parent is not a directory: {destination_parent}"
        )

    with tempfile.TemporaryDirectory(
        prefix=".asle-import-", dir=destination_parent
    ) as temporary:
        staging_root = Path(temporary)
        staged_top = _extract_validated_archive(
            archive_path, staging_root, manifest.archive_top_level
        )

        staged_repo = staging_root / "repository"
        staged_destination = staged_repo.joinpath(*manifest.imported_path.parts)
        staged_destination.parent.mkdir(parents=True)
        staged_top.rename(staged_destination)
        result = _assert_expected_tree(
            tree_digest(staged_repo, manifest.imported_path), manifest
        )
        _move_staged_tree_without_overwrite(staged_destination, destination)

    published = _assert_expected_tree(
        tree_digest(repo_root, manifest.imported_path), manifest
    )
    if published != result:
        raise VendorImportError("published tree differs from the validated staging tree")
    return published


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Safely import or verify the immutable ASLE baseline."
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="repository containing vendor/ASLE_SOURCE.json (default: cwd)",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="verify the archive and existing tree without writing anything",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point."""

    arguments = _parser().parse_args(argv)
    try:
        if arguments.verify_only:
            result = verify(arguments.repo_root)
            mode = "verified"
        else:
            result = import_baseline(arguments.repo_root)
            mode = "imported"
    except (VendorImportError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    print(json.dumps(result.to_dict(mode=mode), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
