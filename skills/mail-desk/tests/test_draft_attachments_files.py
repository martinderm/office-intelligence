"""Draft attachments (#18) — validation, zone confinement and bounded safe reading.

Evidence mode ``tdd``.  Matrix rows L1-L3, P1-P6, N1-N3 (see the external run's ACCEPTANCE-MATRIX).

Seam note: ``core.draft_attachments.load_attachments`` / ``normalize_attachment_specs`` are the public
callables.  They catch path, link, name, MIME-type and size mistakes before any mailbox command; they do
not cover mailbox behaviour (see the save/replace suites).  Only the operating-system boundary is
exercised for the race tests (``os.open``/``os.read`` are wrapped, never replaced by stubs).
Expected values (3 MiB, 5 files, device names) are literals, not recomputed from the code.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import draft_attachments as da  # noqa: E402
from core import drafts  # noqa: E402
from draft_attachment_fixtures import (  # noqa: E402
    make_dir_link,
    make_file_symlink,
    make_workspace,
    remove_dir_link,
)

THREE_MIB = 3145728
SECRET = b"OUTSIDE-SECRET-CONTENT-MARKER"


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = make_workspace(Path(self._tmp.name))
        self.outside = Path(self._tmp.name) / "outside"
        self.outside.mkdir()
        (self.outside / "secret.txt").write_bytes(SECRET)

    def put(self, rel: str, data: bytes = b"payload") -> Path:
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    def assertReason(self, reason: str, specs, **kw) -> drafts.DraftError:  # noqa: N802
        with self.assertRaises(da.DraftAttachmentError) as raised:
            da.load_attachments(self.root, specs, **kw)
        self.assertEqual(reason, raised.exception.reason_code)
        self.assertIsInstance(raised.exception, drafts.DraftError)
        text = str(raised.exception)
        self.assertNotIn(SECRET.decode(), text)
        self.assertNotIn(str(self.root), text)
        self.assertNotIn(str(self.outside), text)
        return raised.exception


class LimitTests(_Base):
    def test_exactly_five_attachments_are_accepted_and_six_rejected(self) -> None:  # L1
        specs = []
        for index in range(5):
            self.put(f"data/f{index}.txt", b"x")
            specs.append(f"data/f{index}.txt")
        loaded = da.load_attachments(self.root, specs)
        self.assertEqual(5, len(loaded))
        self.put("data/f5.txt", b"x")
        reads: list[int] = []
        real_open = os.open

        def counting_open(*args, **kwargs):
            reads.append(1)
            return real_open(*args, **kwargs)

        with patch.object(os, "open", counting_open):
            self.assertReason("attachment_count_exceeded", specs + ["data/f5.txt"])
        self.assertEqual([], reads, "the count limit must fire before any file is opened")

    def test_file_of_exactly_three_mib_is_accepted(self) -> None:  # L2
        self.put("data/max.bin", b"\x01" * THREE_MIB)
        (att,) = da.load_attachments(self.root, ["data/max.bin"])
        self.assertEqual(3145728, att.size)
        self.assertEqual(3145728, len(att.data))

    def test_file_one_byte_over_three_mib_is_rejected_without_reading(self) -> None:  # L2
        self.put("data/over.bin", b"\x01" * (THREE_MIB + 1))
        read_calls: list[int] = []
        real_read = os.read

        def counting_read(fd, n):
            read_calls.append(n)
            return real_read(fd, n)

        with patch.object(os, "read", counting_read):
            self.assertReason("attachment_too_large", ["data/over.bin"])
        self.assertEqual([], read_calls, "an oversized file is rejected from fstat, before reading")

    def test_file_growing_during_read_is_rejected_and_bounded(self) -> None:  # L3
        path = self.put("data/grow.bin", b"\x02" * THREE_MIB)
        real_read = os.read
        state = {"grown": False, "total": 0}

        def growing_read(fd, n):
            data = real_read(fd, n)
            state["total"] += len(data)
            if not state["grown"]:
                state["grown"] = True
                with open(path, "ab") as handle:
                    handle.write(b"\x03" * 4096)
            return data

        with patch.object(os, "read", growing_read):
            self.assertReason("attachment_changed", ["data/grow.bin"])
        self.assertLessEqual(state["total"], THREE_MIB + 1, "never more than limit+1 bytes are read")

    def test_empty_file_is_rejected(self) -> None:  # P4
        self.put("data/empty.txt", b"")
        self.assertReason("attachment_empty", ["data/empty.txt"])


class PathTests(_Base):
    def test_all_four_zones_are_allowed(self) -> None:  # P1
        for zone in ("memory/references", "memory/evidence", "memory/cloud", "data"):
            with self.subTest(zone=zone):
                self.put(f"{zone}/sub/a.txt", b"zone")
                (att,) = da.load_attachments(self.root, [f"{zone}/sub/a.txt"])
                self.assertEqual("a.txt", att.filename)

    def test_invalid_or_escaping_paths_are_rejected(self) -> None:  # P1
        self.put("data/ok.txt")
        bad = {
            "outside zone": "notes/a.txt",
            "zone root itself": "data",
            "memory root": "memory/a.txt",
            "dotdot": "data/../.agents/mail-desk-backend.json",
            "dotdot2": "data/sub/../../x",
            "dot segment": "data/./ok.txt",
            "empty segment": "data//ok.txt",
            "absolute posix": "/etc/passwd",
            "absolute windows": "C:/Windows/win.ini",
            "unc": "//server/share/a.txt",
            "backslash": "data\\ok.txt",
            "colon (ADS)": "data/ok.txt:stream",
            "nul": "data/ok\x00.txt",
            "control": "data/ok\n.txt",
            "wrong case zone": "Data/ok.txt",
            "device name": "data/NUL",
            "device name ext": "data/con.txt",
            "trailing dot": "data/ok.txt.",
            "trailing space": "data/ok.txt ",
            "wildcard": "data/*.txt",
            "empty": "",
        }
        for label, value in bad.items():
            with self.subTest(label=label):
                with self.assertRaises(da.DraftAttachmentError) as raised:
                    da.load_attachments(self.root, [value])
                self.assertIn(raised.exception.reason_code, {"attachment_path_invalid", "attachment_path_forbidden"})

    def test_mail_desk_runtime_state_is_excluded_even_by_case_or_short_name(self) -> None:  # P2
        self.put("data/mail-desk/draft-journal.json", b"{}")
        for value in ("data/mail-desk/draft-journal.json", "data/Mail-Desk/draft-journal.json", "data/MAIL-DESK/x"):
            with self.subTest(value=value):
                with self.assertRaises(da.DraftAttachmentError) as raised:
                    da.load_attachments(self.root, [value])
                self.assertEqual("attachment_path_forbidden", raised.exception.reason_code)

    def test_missing_file_and_directory_are_rejected(self) -> None:  # P4
        (self.root / "data" / "dir").mkdir()
        self.assertReason("attachment_unreadable", ["data/missing.txt"])
        self.assertReason("attachment_not_regular_file", ["data/dir"])

    def test_symlinked_zone_root_is_refused(self) -> None:  # P3
        (self.root / "memory" / "cloud").rmdir()
        make_dir_link(self.root / "memory" / "cloud", self.outside)
        self.addCleanup(remove_dir_link, self.root / "memory" / "cloud")
        self.assertReason("attachment_link_refused", ["memory/cloud/secret.txt"])

    def test_symlinked_intermediate_directory_is_refused(self) -> None:  # P3
        make_dir_link(self.root / "data" / "linked", self.outside)
        self.addCleanup(remove_dir_link, self.root / "data" / "linked")
        self.assertReason("attachment_link_refused", ["data/linked/secret.txt"])

    def test_symlinked_file_is_refused(self) -> None:  # P3
        if not make_file_symlink(self.root / "data" / "ln.txt", self.outside / "secret.txt"):
            self.skipTest("host forbids file symlinks (capability, not a missing dependency)")
        self.assertReason("attachment_link_refused", ["data/ln.txt"])

    def test_workspace_root_may_itself_sit_behind_a_link(self) -> None:  # P3 (root = trusted anchor)
        self.put("data/a.txt", b"data")
        anchor = Path(self._tmp.name) / "anchor"
        make_dir_link(anchor, self.root)
        self.addCleanup(remove_dir_link, anchor)
        (att,) = da.load_attachments(anchor, ["data/a.txt"])
        self.assertEqual(b"data", att.data)


class RaceTests(_Base):
    """The opened handle is proven to be the in-zone file BEFORE a single byte is read."""

    def test_directory_swapped_for_link_between_check_and_open_never_reads_outside(self) -> None:  # P5
        self.put("data/sub/a.txt", b"inside")
        (self.outside / "a.txt").write_bytes(SECRET)
        real_open = os.open
        swapped = {"done": False}

        def swapping_open(path, flags, *args, **kwargs):
            if not swapped["done"] and str(path).replace("\\", "/").endswith("data/sub/a.txt"):
                swapped["done"] = True
                (self.root / "data" / "sub" / "a.txt").unlink()
                (self.root / "data" / "sub").rmdir()
                make_dir_link(self.root / "data" / "sub", self.outside)
            return real_open(path, flags, *args, **kwargs)

        reads: list[int] = []
        real_read = os.read

        def spying_read(fd, n):
            reads.append(n)
            return real_read(fd, n)

        try:
            with patch.object(os, "open", swapping_open), patch.object(os, "read", spying_read):
                self.assertReason("attachment_changed", ["data/sub/a.txt"])
        finally:
            if (self.root / "data" / "sub").is_symlink() or os.path.isjunction(self.root / "data" / "sub"):
                remove_dir_link(self.root / "data" / "sub")
        self.assertTrue(swapped["done"])
        self.assertEqual([], reads, "no byte may be read once the handle identity differs")

    def test_regular_file_replaced_between_check_and_open_is_refused(self) -> None:  # P5
        target = self.put("data/a.txt", b"original")
        real_open = os.open
        swapped = {"done": False}

        def swapping_open(path, flags, *args, **kwargs):
            if not swapped["done"] and str(path).replace("\\", "/").endswith("data/a.txt"):
                swapped["done"] = True
                replacement = target.with_name("replacement.txt")
                replacement.write_bytes(b"different file")
                os.replace(replacement, target)
            return real_open(path, flags, *args, **kwargs)

        reads: list[int] = []
        real_read = os.read
        with patch.object(os, "open", swapping_open), patch.object(
            os, "read", lambda fd, n: (reads.append(n), real_read(fd, n))[1]
        ):
            self.assertReason("attachment_changed", ["data/a.txt"])
        self.assertEqual([], reads)


class NameAndTypeTests(_Base):
    def test_default_name_is_basename_and_unicode_is_kept(self) -> None:  # N1
        self.put("memory/references/Prüfung Ärger – 🎯.pdf", b"%PDF")
        (att,) = da.load_attachments(self.root, ["memory/references/Prüfung Ärger – 🎯.pdf"])
        self.assertEqual("Prüfung Ärger – 🎯.pdf", att.filename)
        self.assertEqual("application/pdf", att.content_type)

    def test_explicit_filename_override_and_validation(self) -> None:  # N1
        self.put("data/a.bin", b"x")
        (att,) = da.load_attachments(self.root, [{"path": "data/a.bin", "filename": "Angebot 2026.bin"}])
        self.assertEqual("Angebot 2026.bin", att.filename)
        bad = [
            "", " lead.txt", "trail.txt ", "dot.", "..", ".", "a/b.txt", "a\\b.txt", "a:b.txt", "a*b", 'a"b', "a<b", "a>b",
            "a|b", "a?b", "line\nbreak.txt", "tab\t.txt", "nul\x00.txt", "bidi\u202e.txt", "sep\u2028.txt", "isol\u2066.txt",
            "NUL.txt", "com1", "x" * 129, "ä" * 128 + "ä", "lone\ud800.txt",
        ]
        for value in bad:
            with self.subTest(value=ascii(value)):
                with self.assertRaises(da.DraftAttachmentError) as raised:
                    da.load_attachments(self.root, [{"path": "data/a.bin", "filename": value}])
                self.assertEqual("attachment_filename_invalid", raised.exception.reason_code)

    def test_filename_length_limits_are_inclusive(self) -> None:  # N1
        self.put("data/a.bin", b"x")
        (att,) = da.load_attachments(self.root, [{"path": "data/a.bin", "filename": "a" * 128}])
        self.assertEqual(128, len(att.filename))
        # 85 three-byte characters are 255 UTF-8 bytes: allowed; 86 are 258 bytes: rejected.
        (att,) = da.load_attachments(self.root, [{"path": "data/a.bin", "filename": "€" * 85}])
        self.assertEqual(255, len(att.filename.encode("utf-8")))
        with self.assertRaises(da.DraftAttachmentError):
            da.load_attachments(self.root, [{"path": "data/a.bin", "filename": "€" * 86}])

    def test_duplicate_paths_and_filenames_are_rejected(self) -> None:  # N1
        self.put("data/a.txt", b"x")
        self.put("memory/cloud/A.TXT", b"y")
        self.assertReason("attachment_duplicate", ["data/a.txt", "data/a.txt"])
        self.assertReason("attachment_duplicate", ["data/a.txt", "memory/cloud/A.TXT"])

    def test_content_type_default_table_and_fallback(self) -> None:  # N2
        expected = {
            "a.pdf": "application/pdf", "a.PNG": "image/png", "a.jpg": "image/jpeg", "a.jpeg": "image/jpeg",
            "a.txt": "text/plain", "a.csv": "text/csv", "a.md": "text/markdown", "a.json": "application/json",
            "a.docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "a.xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "a.zip": "application/zip", "a.unknownext": "application/octet-stream", "noext": "application/octet-stream",
        }
        for name, content_type in expected.items():
            with self.subTest(name=name):
                self.put(f"data/{name}", b"x")
                (att,) = da.load_attachments(self.root, [f"data/{name}"])
                self.assertEqual(content_type, att.content_type)

    def test_explicit_content_type_validation(self) -> None:  # N2
        self.put("data/a.bin", b"x")
        (att,) = da.load_attachments(self.root, [{"path": "data/a.bin", "content_type": "application/x-custom+json"}])
        self.assertEqual("application/x-custom+json", att.content_type)
        for bad in ("Application/PDF", "application/pdf; charset=utf-8", "application", "multipart/mixed",
                    "message/rfc822", "text/", "/pdf", "a b/c", "text/pl\nain", "", "x" * 130 + "/y"):
            with self.subTest(value=ascii(bad)):
                with self.assertRaises(da.DraftAttachmentError) as raised:
                    da.load_attachments(self.root, [{"path": "data/a.bin", "content_type": bad}])
                self.assertEqual("attachment_content_type_invalid", raised.exception.reason_code)

    def test_spec_shape_is_closed_and_typed(self) -> None:  # N3
        self.put("data/a.txt", b"x")
        for bad in (
            [{"path": "data/a.txt", "bonus": 1}], [{"filename": "x.txt"}], [42], [None], [["data/a.txt"]],
            [{"path": 5}], [{"path": "data/a.txt", "filename": 5}], "data/a.txt", {"path": "data/a.txt"}, 7,
        ):
            with self.subTest(value=repr(bad)):
                with self.assertRaises(da.DraftAttachmentError) as raised:
                    da.load_attachments(self.root, bad)
                self.assertEqual("attachment_spec_invalid", raised.exception.reason_code)

    def test_loaded_attachment_carries_independent_digest(self) -> None:  # B2 support
        self.put("data/a.bin", b"abc")
        (att,) = da.load_attachments(self.root, ["data/a.bin"])
        # sha256("abc") worked example from FIPS 180-2.
        self.assertEqual("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad", att.sha256)
        self.assertEqual(3, att.size)
        self.assertEqual(b"abc", att.data)


if __name__ == "__main__":
    unittest.main()
