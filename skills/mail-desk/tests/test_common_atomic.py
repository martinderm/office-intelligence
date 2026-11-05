"""Failure-mode tests for shared atomic mail-desk writers."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

from core import attachment_disposition_log, attachment_promotion, common  # noqa: E402
from core.recovery import JOURNAL_FILENAME, BatchRecoveryJournal  # noqa: E402


def sharing_error(winerror: int = 5) -> OSError:
    """Build the OSError ``os.replace`` raises while another handle blocks it."""
    if os.name == "nt":
        return OSError(13, "Access is denied", None, winerror)
    error = PermissionError(13, "Access is denied")
    error.winerror = winerror  # type: ignore[attr-defined]
    return error


class ReplaceScript:
    """``os.replace`` stand-in: fails with scripted errors, then really replaces.

    The final real replace is settled inside the stand-in, so a genuine
    environment sharing error (the very flake of issue #70, e.g. a scanner on
    the fresh temp file) can neither inflate ``calls`` nor reach the code under
    test: call counts and backoffs stay exactly the scripted ones.
    """

    def __init__(self, failures: list[BaseException]) -> None:
        self.failures = list(failures)
        self.calls = 0
        self._real = os.replace
        self._sleep = time.sleep  # captured before tests patch ``time.sleep``

    def __call__(self, source, target):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        for _ in range(100):
            try:
                return self._real(source, target)
            except PermissionError as error:
                if getattr(error, "winerror", None) not in common.TRANSIENT_REPLACE_WINERRORS:
                    raise
                self._sleep(0.02)
        return self._real(source, target)


class CommonAtomicWriteTests(unittest.TestCase):
    def test_replace_failure_preserves_existing_json_and_cleans_staging_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "state.json"
            target.write_text('{"before": true}\n', encoding="utf-8")

            with patch.object(common.os, "replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    common.atomic_write_json(target, {"after": True})

            self.assertEqual('{"before": true}\n', target.read_text(encoding="utf-8"))
            self.assertEqual([target], list(Path(temp_dir).iterdir()))

    def test_json_and_jsonl_writers_use_stable_utf8_documents(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            json_target = Path(temp_dir) / "state.json"
            jsonl_target = Path(temp_dir) / "state.jsonl"

            common.atomic_write_json(json_target, {"name": "Grüße"})
            common.atomic_rewrite_jsonl(jsonl_target, ({"id": 1}, {"id": 2}))

            self.assertEqual({"name": "Grüße"}, json.loads(json_target.read_text(encoding="utf-8")))
            self.assertTrue(json_target.read_bytes().endswith(b"\n"))
            self.assertEqual(
                [{"id": 1}, {"id": 2}],
                [json.loads(line) for line in jsonl_target.read_text(encoding="utf-8").splitlines()],
            )


class AtomicReplaceRetryTests(unittest.TestCase):
    """Issue #70: bounded, replace-only retry for transient Windows sharing errors."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.target = self.dir / "batch-recovery-journal.json"
        self.target.write_text('{"before": true}\n', encoding="utf-8")

    def _assert_no_staging_files(self):
        self.assertEqual([self.target], list(self.dir.iterdir()))

    def test_transient_sharing_error_then_success_replaces_target(self):
        script = ReplaceScript([sharing_error(5), sharing_error(32)])
        with patch.object(common.os, "replace", side_effect=script), \
                patch.object(common.time, "sleep") as sleep:
            common.atomic_write_json(self.target, {"after": True})

        self.assertEqual(3, script.calls)
        self.assertEqual({"after": True}, json.loads(self.target.read_text(encoding="utf-8")))
        self.assertEqual(
            list(common.REPLACE_RETRY_DELAYS_SECONDS[:2]),
            [call.args[0] for call in sleep.call_args_list],
        )
        self._assert_no_staging_files()

    def test_persistent_sharing_error_is_classified_after_bounded_attempts(self):
        script = ReplaceScript([sharing_error(5)] * 100)
        with patch.object(common.os, "replace", side_effect=script), \
                patch.object(common.time, "sleep") as sleep:
            with self.assertRaises(common.AtomicReplaceError) as ctx:
                common.atomic_write_json(self.target, {"after": True})

        attempts = len(common.REPLACE_RETRY_DELAYS_SECONDS) + 1
        self.assertEqual(attempts, script.calls, "no endless loop: attempts are bounded")
        self.assertEqual(attempts - 1, sleep.call_count)
        error = ctx.exception
        self.assertIsInstance(error, OSError, "stays an OSError for existing handlers")
        self.assertEqual("atomic_replace_failed", error.reason_code)
        self.assertEqual(attempts, error.attempts)
        self.assertEqual(5, error.cause_winerror)
        self.assertIn(self.target.name, str(error))
        self.assertNotIn(str(self.dir), str(error), "no absolute path echo")
        self.assertIsInstance(error.__cause__, OSError)
        self.assertEqual('{"before": true}\n', self.target.read_text(encoding="utf-8"))
        self._assert_no_staging_files()

    def test_retry_budget_is_small_and_bounded(self):
        delays = common.REPLACE_RETRY_DELAYS_SECONDS
        self.assertGreaterEqual(len(delays), 1)
        self.assertLessEqual(len(delays), 6)
        self.assertLessEqual(sum(delays), 0.5, "documented budget is about 0.4 s")
        self.assertTrue(all(delay > 0 for delay in delays))

    def test_non_transient_errors_are_not_retried(self):
        permanent_errors = [
            OSError("replace failed"),
            FileNotFoundError(2, "No such file"),
            PermissionError(13, "Permission denied"),  # no winerror: not a sharing violation
            sharing_error(1314),  # a Windows error code outside the sharing/denied set
        ]
        for error in permanent_errors:
            with self.subTest(error=repr(error)):
                script = ReplaceScript([error])
                with patch.object(common.os, "replace", side_effect=script), \
                        patch.object(common.time, "sleep") as sleep:
                    with self.assertRaises(OSError) as ctx:
                        common.atomic_write_json(self.target, {"after": True})
                self.assertIs(error, ctx.exception, "original error surfaces untouched")
                self.assertEqual(1, script.calls)
                sleep.assert_not_called()
                self._assert_no_staging_files()

    def test_retry_applies_only_to_replace_not_to_staging_write(self):
        with patch.object(common.os, "fsync", side_effect=sharing_error(5)) as fsync, \
                patch.object(common.time, "sleep") as sleep:
            with self.assertRaises(OSError) as ctx:
                common.atomic_write_json(self.target, {"after": True})
        self.assertNotIsInstance(ctx.exception, common.AtomicReplaceError)
        self.assertEqual(1, fsync.call_count)
        sleep.assert_not_called()
        self.assertEqual('{"before": true}\n', self.target.read_text(encoding="utf-8"))

    @unittest.skipUnless(os.name == "nt", "open handles block os.replace only on Windows")
    def test_real_open_handle_is_the_issue_failure_mode_and_recovers_when_released(self):
        # Python's open() shares read/write but not delete, so a lingering reader
        # makes os.replace fail with WinError 5 exactly like the observed flake.
        handle = open(self.target, "rb")
        self.addCleanup(handle.close)

        def release_handle(_seconds):
            handle.close()

        with patch.object(common.time, "sleep", side_effect=release_handle) as sleep:
            common.atomic_write_json(self.target, {"after": True})

        self.assertGreaterEqual(sleep.call_count, 1, "released during a backoff")
        self.assertEqual({"after": True}, json.loads(self.target.read_text(encoding="utf-8")))
        self._assert_no_staging_files()

    @unittest.skipUnless(os.name == "nt", "open handles block os.replace only on Windows")
    def test_real_open_handle_held_forever_ends_in_classified_error(self):
        handle = open(self.target, "rb")
        self.addCleanup(handle.close)

        with patch.object(common.time, "sleep") as sleep:
            with self.assertRaises(common.AtomicReplaceError) as ctx:
                common.atomic_write_json(self.target, {"after": True})

        self.assertEqual(len(common.REPLACE_RETRY_DELAYS_SECONDS), sleep.call_count)
        self.assertEqual("atomic_replace_failed", ctx.exception.reason_code)
        handle.close()
        self.assertEqual('{"before": true}\n', self.target.read_text(encoding="utf-8"))
        self._assert_no_staging_files()


class RecoveryJournalReplaceTests(unittest.TestCase):
    """Issue #70: the batch recovery journal rides on the bounded replace retry."""

    ITEM = {
        "message_id": "<m1@example.test>",
        "envelope_id": "7",
        "source_folder": "INBOX",
        "action": {"type": "copy_as_move", "target_folder": "Archive"},
    }

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)
        self.journal_path = self.data_dir / JOURNAL_FILENAME

    def _phase(self) -> str:
        persisted = json.loads(self.journal_path.read_text(encoding="utf-8"))
        return persisted["runs"]["run-70"]["items"]["m1@example.test"]["phase"]

    def test_journal_transition_survives_transient_replace_denial(self):
        journal = BatchRecoveryJournal(self.data_dir, "run-70")
        record = journal.ensure_item(self.ITEM)

        script = ReplaceScript([sharing_error(5), sharing_error(5)])
        with patch.object(common.os, "replace", side_effect=script), \
                patch.object(common.time, "sleep"):
            journal.transition(record, "copied", target_uid="42")

        self.assertEqual(3, script.calls)
        self.assertEqual("copied", self._phase())
        self.assertEqual([self.journal_path], list(self.data_dir.iterdir()))

    def test_exhausted_replace_raises_classified_error_and_keeps_last_good_journal(self):
        journal = BatchRecoveryJournal(self.data_dir, "run-70")
        record = journal.ensure_item(self.ITEM)
        last_good = self.journal_path.read_bytes()

        script = ReplaceScript([sharing_error(5)] * 100)
        with patch.object(common.os, "replace", side_effect=script), \
                patch.object(common.time, "sleep"):
            with self.assertRaises(common.AtomicReplaceError) as ctx:
                journal.transition(record, "copied", target_uid="42")

        self.assertEqual("atomic_replace_failed", ctx.exception.reason_code)
        self.assertEqual(len(common.REPLACE_RETRY_DELAYS_SECONDS) + 1, script.calls)
        self.assertEqual(last_good, self.journal_path.read_bytes())
        self.assertEqual([self.journal_path], list(self.data_dir.iterdir()))

    @unittest.skipUnless(os.name == "nt", "open handles block os.replace only on Windows")
    def test_journal_write_recovers_when_a_lingering_reader_releases_the_file(self):
        journal = BatchRecoveryJournal(self.data_dir, "run-70")
        record = journal.ensure_item(self.ITEM)

        handle = open(self.journal_path, "rb")  # models an indexer/AV scan of the journal
        self.addCleanup(handle.close)
        with patch.object(common.time, "sleep", side_effect=lambda _s: handle.close()):
            journal.transition(record, "copied", target_uid="42")

        self.assertEqual("copied", self._phase())


class SiblingJournalReplaceTests(unittest.TestCase):
    """Issue #70: the promotion and discard journals share the same bounded retry.

    The full-suite flake was also observed in ``_write_journal_atomic`` of the
    promotion journal, which had its own bare ``os.replace``.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_promotion_journal_survives_transient_replace_denial(self):
        path = self.dir / "promotion-journal.json"
        script = ReplaceScript([sharing_error(5)])
        with patch.object(common.os, "replace", side_effect=script), \
                patch.object(common.time, "sleep"):
            attachment_promotion._write_journal_atomic(path, {"schema_version": 1})

        self.assertEqual(2, script.calls)
        self.assertEqual(1, json.loads(path.read_text(encoding="utf-8"))["schema_version"])
        self.assertEqual([path], list(self.dir.iterdir()))

    def test_promotion_journal_exhausted_replace_is_classified_and_cleans_up(self):
        path = self.dir / "promotion-journal.json"
        path.write_text('{"before": true}\n', encoding="utf-8")
        script = ReplaceScript([sharing_error(5)] * 100)
        with patch.object(common.os, "replace", side_effect=script), \
                patch.object(common.time, "sleep"):
            with self.assertRaises(common.AtomicReplaceError):
                attachment_promotion._write_journal_atomic(path, {"schema_version": 1})

        self.assertEqual(len(common.REPLACE_RETRY_DELAYS_SECONDS) + 1, script.calls)
        self.assertEqual('{"before": true}\n', path.read_text(encoding="utf-8"))
        self.assertEqual([path], list(self.dir.iterdir()))

    def test_discard_journal_survives_transient_replace_denial(self):
        path = self.dir / "attachment-discard-journal.json"
        script = ReplaceScript([sharing_error(5), sharing_error(32)])
        with patch.object(common.os, "replace", side_effect=script), \
                patch.object(common.time, "sleep"):
            attachment_disposition_log._save_discard_journal_atomic(path, {"entries": {}})

        self.assertEqual(3, script.calls)
        self.assertEqual({}, json.loads(path.read_text(encoding="utf-8"))["entries"])
        self.assertEqual([path], list(self.dir.iterdir()))

    def test_discard_journal_exhausted_replace_keeps_its_corruption_contract(self):
        path = self.dir / "attachment-discard-journal.json"
        path.write_text('{"before": true}\n', encoding="utf-8")
        script = ReplaceScript([sharing_error(5)] * 100)
        with patch.object(common.os, "replace", side_effect=script), \
                patch.object(common.time, "sleep"):
            with self.assertRaises(attachment_disposition_log.RecoveryJournalCorruptedError) as ctx:
                attachment_disposition_log._save_discard_journal_atomic(path, {"entries": {}})

        self.assertIsInstance(ctx.exception.__cause__, common.AtomicReplaceError)
        self.assertEqual(len(common.REPLACE_RETRY_DELAYS_SECONDS) + 1, script.calls)
        self.assertEqual('{"before": true}\n', path.read_text(encoding="utf-8"))
        self.assertEqual([path], list(self.dir.iterdir()))


if __name__ == "__main__":
    unittest.main()
