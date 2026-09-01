"""Direct failure-mode tests for atomic evidence replacement."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

from core import evidence  # noqa: E402


class _FailingFile:
    def __init__(self, wrapped, *, operation: str, error: BaseException) -> None:
        self._wrapped = wrapped
        self._operation = operation
        self._error = error

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._wrapped.close()
        return False

    def write(self, content: str):
        if self._operation == "write":
            self._wrapped.write(content[:7])
            self._wrapped.flush()
            raise self._error
        return self._wrapped.write(content)

    def flush(self) -> None:
        if self._operation == "flush":
            raise self._error
        self._wrapped.flush()

    def fileno(self) -> int:
        return self._wrapped.fileno()


class EvidenceAtomicWriteTests(unittest.TestCase):
    original_bytes = b"# Existing evidence\n\noriginal entry\n"

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        self.target = self.workspace / "memory" / "evidence" / "topics" / "example" / "2026-09.md"
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(self.original_bytes)
        self.spec = {
            "file": self.target.relative_to(self.workspace).as_posix(),
            "entry": "- message_id: new@example.test\n  summary: new evidence",
        }

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def assert_original_preserved_without_temporary_files(self) -> None:
        self.assertEqual(self.target.read_bytes(), self.original_bytes)
        self.assertEqual(list(self.target.parent.glob(f".{self.target.name}.*.tmp")), [])

    def failing_fdopen(self, operation: str, error: BaseException):
        original_fdopen = os.fdopen

        def open_failing_file(file_descriptor, *args, **kwargs):
            wrapped = original_fdopen(file_descriptor, *args, **kwargs)
            return _FailingFile(wrapped, operation=operation, error=error)

        return open_failing_file

    def test_partial_write_error_preserves_existing_file(self) -> None:
        with patch.object(
            evidence.os,
            "fdopen",
            side_effect=self.failing_fdopen("write", OSError("simulated write failure")),
        ):
            with self.assertRaises(OSError):
                evidence.update_evidence_file(self.spec, "new@example.test", self.workspace)

        self.assert_original_preserved_without_temporary_files()

    def test_flush_error_preserves_existing_file(self) -> None:
        with patch.object(
            evidence.os,
            "fdopen",
            side_effect=self.failing_fdopen("flush", OSError("simulated flush failure")),
        ):
            with self.assertRaises(OSError):
                evidence.update_evidence_file(self.spec, "new@example.test", self.workspace)

        self.assert_original_preserved_without_temporary_files()

    def test_replace_error_preserves_existing_file(self) -> None:
        with patch.object(evidence.os, "replace", side_effect=OSError("simulated replace failure")):
            with self.assertRaises(OSError):
                evidence.update_evidence_file(self.spec, "new@example.test", self.workspace)

        self.assert_original_preserved_without_temporary_files()

    def test_cleanup_error_does_not_mask_primary_replace_error(self) -> None:
        primary_error = OSError("simulated replace failure")
        cleanup_error = OSError("simulated unlink failure")

        with (
            patch.object(evidence.os, "replace", side_effect=primary_error),
            patch.object(evidence.Path, "unlink", side_effect=cleanup_error) as unlink,
        ):
            with self.assertRaises(OSError) as raised:
                evidence.update_evidence_file(self.spec, "new@example.test", self.workspace)

        self.assertIs(raised.exception, primary_error)
        unlink.assert_called_once_with()
        self.assertEqual(self.target.read_bytes(), self.original_bytes)

    def test_fsync_error_preserves_existing_file(self) -> None:
        with patch.object(evidence.os, "fsync", side_effect=OSError("simulated fsync failure")):
            with self.assertRaises(OSError):
                evidence.update_evidence_file(self.spec, "new@example.test", self.workspace)

        self.assert_original_preserved_without_temporary_files()

    def test_keyboard_interrupt_after_partial_write_preserves_existing_file(self) -> None:
        with patch.object(
            evidence.os,
            "fdopen",
            side_effect=self.failing_fdopen("write", KeyboardInterrupt()),
        ):
            with self.assertRaises(KeyboardInterrupt):
                evidence.update_evidence_file(self.spec, "new@example.test", self.workspace)

        self.assert_original_preserved_without_temporary_files()

    def test_batch_replace_failure_is_reported_and_preserves_existing_file(self) -> None:
        pending = [{"message_id": "new@example.test", "evidence": self.spec}]

        with patch.object(evidence.os, "replace", side_effect=OSError("simulated replace failure")):
            result = evidence.flush_batch_evidence(pending, self.workspace)

        self.assertEqual(result, {str(self.target.resolve()): False})
        self.assert_original_preserved_without_temporary_files()

    def test_successful_update_replaces_complete_content(self) -> None:
        self.assertTrue(evidence.update_evidence_file(self.spec, "new@example.test", self.workspace))

        self.assertEqual(
            self.target.read_text(encoding="utf-8"),
            "# Existing evidence\n\noriginal entry\n\n"
            "- message_id: new@example.test\n  summary: new evidence\n",
        )
        self.assertEqual(list(self.target.parent.glob(f".{self.target.name}.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
