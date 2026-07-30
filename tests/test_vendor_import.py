from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from burstserve.vendor_import import (
    TREE_HASH_ALGORITHM,
    VendorImportError,
    import_baseline,
    main,
    tree_digest,
    verify,
)


def _expected_tree_digest(files: dict[str, bytes]) -> str:
    outer = hashlib.sha256()
    for relative, content in sorted(
        files.items(), key=lambda item: f"vendor/asle/{item[0]}".encode()
    ):
        inner = hashlib.sha256(content).hexdigest()
        outer.update(f"{inner}  vendor/asle/{relative}\n".encode())
    return outer.hexdigest()


class TemporaryVendorRepository:
    def __init__(
        self,
        root: Path,
        *,
        files: dict[str, bytes] | None = None,
        extra_members: list[tuple[tarfile.TarInfo, bytes | None]] | None = None,
    ) -> None:
        self.root = root
        self.files = files or {
            "README.md": b"immutable baseline\n",
            "nested/model.py": b"print('asle')\n",
        }
        self.archive_path = root / "ASLE.tar.gz"
        (root / "vendor").mkdir()
        with tarfile.open(self.archive_path, mode="w:gz") as archive:
            top = tarfile.TarInfo("ASLE/")
            top.type = tarfile.DIRTYPE
            top.mode = 0o755
            archive.addfile(top)
            for relative, content in self.files.items():
                member = tarfile.TarInfo(f"ASLE/{relative}")
                member.size = len(content)
                member.mode = 0o644
                archive.addfile(member, io.BytesIO(content))
            for member, content in extra_members or []:
                archive.addfile(
                    member,
                    None if content is None else io.BytesIO(content),
                )
        self.write_manifest()

    def write_manifest(
        self,
        *,
        archive_sha256: str | None = None,
        imported_path: str = "vendor/asle",
    ) -> None:
        archive_bytes = self.archive_path.read_bytes()
        manifest = {
            "schema_version": 1,
            "source_archive": "ASLE.tar.gz",
            "archive_sha256": archive_sha256
            or hashlib.sha256(archive_bytes).hexdigest(),
            "archive_size_bytes": len(archive_bytes),
            "archive_top_level": "ASLE",
            "imported_path": imported_path,
            "imported_file_count": len(self.files),
            "imported_tree_sha256": _expected_tree_digest(self.files),
        }
        (self.root / "vendor" / "ASLE_SOURCE.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )


class VendorImportTest(unittest.TestCase):
    def test_import_then_verify_and_never_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = TemporaryVendorRepository(root)

            imported = import_baseline(root)

            self.assertEqual(imported.file_count, len(fixture.files))
            self.assertEqual(
                imported.tree_sha256, _expected_tree_digest(fixture.files)
            )
            for relative, expected in fixture.files.items():
                self.assertEqual(
                    (root / "vendor" / "asle" / relative).read_bytes(), expected
                )
            self.assertEqual(verify(root), imported)

            sentinel = root / "vendor" / "asle" / "README.md"
            sentinel.write_bytes(b"user-owned change\n")
            with self.assertRaisesRegex(VendorImportError, "refusing to overwrite"):
                import_baseline(root)
            self.assertEqual(sentinel.read_bytes(), b"user-owned change\n")

    def test_tree_digest_matches_documented_sha256sum_lines(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = {"z.txt": b"last", "a/a.txt": b"first"}
            destination = root / "vendor" / "asle"
            (destination / "a").mkdir(parents=True)
            (destination / "z.txt").write_bytes(files["z.txt"])
            (destination / "a" / "a.txt").write_bytes(files["a/a.txt"])

            observed, count = tree_digest(root, Path("vendor/asle"))

            self.assertEqual(TREE_HASH_ALGORITHM, "sha256sum-lines-vendor-asle-v1")
            self.assertEqual(count, 2)
            self.assertEqual(observed, _expected_tree_digest(files))

    def test_verify_only_requires_existing_import(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            TemporaryVendorRepository(root)

            with self.assertRaisesRegex(VendorImportError, "tree not found"):
                verify(root)
            self.assertEqual(main(["--repo-root", str(root), "--verify-only"]), 2)
            self.assertFalse((root / "vendor" / "asle").exists())

    def test_archive_digest_is_checked_before_import(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = TemporaryVendorRepository(root)
            fixture.write_manifest(archive_sha256="0" * 64)

            with self.assertRaisesRegex(VendorImportError, "SHA-256 mismatch"):
                import_baseline(root)
            self.assertFalse((root / "vendor" / "asle").exists())

    def test_path_traversal_is_rejected_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            traversal = tarfile.TarInfo("ASLE/../../escape.txt")
            traversal.size = len(b"escape")
            TemporaryVendorRepository(
                root, extra_members=[(traversal, b"escape")]
            )

            with self.assertRaisesRegex(VendorImportError, "unsafe archive member"):
                import_baseline(root)
            self.assertFalse((root / "escape.txt").exists())
            self.assertFalse((root / "vendor" / "asle").exists())

    def test_absolute_path_is_rejected_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            absolute = tarfile.TarInfo("/outside.txt")
            absolute.size = len(b"outside")
            TemporaryVendorRepository(
                root, extra_members=[(absolute, b"outside")]
            )

            with self.assertRaisesRegex(VendorImportError, "unsafe archive member"):
                import_baseline(root)
            self.assertFalse((root / "vendor" / "asle").exists())

    def test_symlink_and_hardlink_are_rejected(self) -> None:
        for link_type in (tarfile.SYMTYPE, tarfile.LNKTYPE):
            with self.subTest(link_type=link_type):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    link = tarfile.TarInfo("ASLE/escape-link")
                    link.type = link_type
                    link.linkname = "../../outside"
                    TemporaryVendorRepository(root, extra_members=[(link, None)])

                    with self.assertRaisesRegex(
                        VendorImportError, "links are not permitted"
                    ):
                        import_baseline(root)
                    self.assertFalse((root / "vendor" / "asle").exists())

    def test_manifest_cannot_redirect_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = TemporaryVendorRepository(root)
            fixture.write_manifest(imported_path="other/place")

            with self.assertRaisesRegex(VendorImportError, "stable destination"):
                import_baseline(root)
            self.assertFalse((root / "other").exists())

    def test_verify_rejects_modified_imported_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            TemporaryVendorRepository(root)
            import_baseline(root)
            (root / "vendor" / "asle" / "README.md").write_bytes(b"modified\n")

            with self.assertRaisesRegex(VendorImportError, "tree SHA-256 mismatch"):
                verify(root)


if __name__ == "__main__":
    unittest.main()
