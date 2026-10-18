"""First-class draft API (save / delete / replace) behavior tests.

Evidence mode ``tdd``, risk tier ``medium``.  This module is written **before**
the production change and is Red against the missing ``core.drafts`` contract
and the missing ``mail_desk_himalaya_client`` draft operations.

Pinned contract (GitHub issue #3, confirmed design):

* **No approval gate.**  Drafts stay unsent; sending/SMTP is out of scope and no
  ``message send`` command may ever be issued.  The batch-runner is untouched.
* **Dynamic Drafts folder resolution.**  Resolved from the live
  ``folder list -o json`` catalog with a documented deterministic fallback chain
  (``\\Drafts`` special-use attribute -> conventional ``Drafts`` -> GroupWise
  ``Work In Progress``); an unresolvable mailbox fails loudly with a bounded
  ``drafts_folder_unresolved`` error instead of silently hard-coding a folder.
* **UTF-8 APPEND.**  ``message save`` receives the raw RFC-822 message as one
  UTF-8 (SMTPUTF8) literal, so subject/recipient/body bytes survive a round-trip
  without a 7-bit downgrade.  The returned envelope carries
  ``{envelope_id, folder, subject, to, date}`` analogous to
  ``get_single_email_details``.
* **Delete without UID MOVE.**  Himalaya 1.2.0's ``message delete`` *moves* a
  non-trash message to the trash folder, so deletion marks the draft ``\\Deleted``
  in place (``flag add deleted``) and then expunges the folder
  (``folder expunge``); no move/copy is ever issued.
* **Replace = new-then-delete.**  The replacement draft is appended first, the
  old one is deleted afterwards; the documented crash window is recorded in the
  draft journal together with both envelope ids.
* **Standalone contract surface.**  The draft API is re-exported from the
  ``core`` package, and the operative documentation
  (``SKILL.md``, ``references/cli-operations.md``,
  ``references/backends/himalaya.md``) plus the L2 system map describe the three
  operations, the dynamic Drafts-folder resolution and the separate draft journal.

Everything here is hermetic: the Himalaya command invoker is faked, no
subprocess or network is used.
"""

from __future__ import annotations

import email
import email.policy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from typing import Any
from unittest.mock import patch

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mail_desk_himalaya_client as client  # noqa: E402
from core import drafts  # noqa: E402
from core import himalaya  # noqa: E402
from core.common import get_iso_week_folder  # noqa: E402
from himalaya_fixtures import DraftRunner, drafts_case  # noqa: E402

#: A UTF-8 torture subject: umlaut, en-dash and emoji.
UTF8_SUBJECT = "Prüfung – Kärnten \U0001F3AF"
UTF8_TO = "Empfänger Öztürk <a@example.org>"
UTF8_BODY = "Grüße aus Wien – 100 % – \U0001F3AF"


class TargetDraftsFolderResolutionTests(unittest.TestCase):
    """Red now: the Drafts folder must be resolved dynamically and fail loudly."""

    def test_special_use_attribute_wins_over_folder_name(self) -> None:
        """A folder advertising the ``\\Drafts`` attribute resolves, whatever its name."""
        runner = drafts_case(
            folders=[
                {"name": "INBOX"},
                {"name": "Entwürfe", "attributes": ["\\Drafts"]},
                {"name": "Archiv"},
            ]
        )
        with patch.object(himalaya, "run_himalaya", runner):
            resolved = drafts.resolve_drafts_folder(account="demo")
        self.assertEqual("Entwürfe", resolved)

    def test_groupwise_work_in_progress_fallback(self) -> None:
        """Without the attribute, the GroupWise convention resolves the folder."""
        runner = drafts_case(folders=["INBOX", "Sent", "Work In Progress"])
        with patch.object(himalaya, "run_himalaya", runner):
            resolved = drafts.resolve_drafts_folder(account="demo")
        self.assertEqual("Work In Progress", resolved)

    def test_conventional_drafts_name_precedes_groupwise(self) -> None:
        """The conventional ``Drafts`` name is preferred to the GroupWise fallback."""
        runner = drafts_case(folders=["INBOX", "Work In Progress", "Drafts"])
        with patch.object(himalaya, "run_himalaya", runner):
            resolved = drafts.resolve_drafts_folder(account="demo")
        self.assertEqual("Drafts", resolved)

    def test_unresolvable_mailbox_fails_loud(self) -> None:
        """No attribute and no known name: a bounded fail-loud error, never a hard-coded folder."""
        runner = drafts_case(folders=["INBOX", "Archive", "Junk"])
        with patch.object(himalaya, "run_himalaya", runner):
            with self.assertRaises(drafts.DraftsFolderUnresolvedError) as raised:
                drafts.resolve_drafts_folder(account="demo")
        error = raised.exception
        self.assertEqual("drafts_folder_unresolved", error.reason_code)
        self.assertLessEqual(len(str(error)), 1000)
        # Fail loud before any write and without inventing a folder name.
        self.assertEqual([], runner.saved_raw)

    def test_folder_listing_failure_fails_loud(self) -> None:
        """A failing folder listing is reported as an unresolved Drafts folder."""

        def boom(args: list[str], account: str | None = None, **kwargs: object) -> str:
            raise himalaya.HimalayaInvocationError("himalaya_command_failed", "no mailbox")

        with patch.object(himalaya, "run_himalaya", boom):
            with self.assertRaises(drafts.DraftsFolderUnresolvedError) as raised:
                drafts.resolve_drafts_folder(account="demo")
        self.assertEqual("drafts_folder_unresolved", raised.exception.reason_code)


class TargetSaveDraftTests(unittest.TestCase):
    """Red now: ``save_draft`` appends a UTF-8 literal into the resolved folder."""

    def test_save_draft_appends_utf8_literal_and_returns_envelope(self) -> None:
        runner = drafts_case(folders=["INBOX", "Sent", "Drafts"])
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(himalaya, "run_himalaya", runner):
                envelope = client.op_save_draft(
                    to=UTF8_TO,
                    subject=UTF8_SUBJECT,
                    body=UTF8_BODY,
                    account="demo",
                    data_dir=Path(tmp),
                )

        save_calls = runner.actions("message", "save")
        self.assertEqual(1, len(save_calls))
        save_call = save_calls[0]
        folder_index = save_call.index("-f")
        self.assertEqual("Drafts", save_call[folder_index + 1], "saved into the resolved Drafts folder")
        self.assertIn("-o", save_call, "an explicit plain output format must keep stdin in charge")
        self.assertEqual("plain", save_call[save_call.index("-o") + 1])

        # The raw RFC-822 message travels only through the binary stdin pipe.
        self.assertEqual(1, len(runner.saved_stdin))
        raw = runner.saved_raw[0]
        for token in save_call:
            self.assertNotIn(raw, token)
            self.assertNotIn(UTF8_BODY, token)
        parsed = email.message_from_string(raw, policy=email.policy.SMTPUTF8)
        self.assertEqual(UTF8_SUBJECT, parsed["Subject"])
        self.assertEqual(UTF8_TO, parsed["To"])
        body_content = parsed.get_body(preferencelist=("plain",)).get_content()
        self.assertEqual(UTF8_BODY, body_content.strip())
        # No 7-bit downgrade: headers are raw UTF-8, not RFC 2047 encoded words.
        self.assertNotIn("=?", raw)

        # Envelope contract analogous to ``get_single_email_details``.
        for key in ("envelope_id", "folder", "subject", "to", "date"):
            self.assertIn(key, envelope)
        self.assertEqual("Drafts", envelope["folder"])
        self.assertEqual(UTF8_SUBJECT, envelope["subject"])
        self.assertEqual(UTF8_TO, envelope["to"])
        self.assertTrue(str(envelope["envelope_id"]).strip())
        self.assertTrue(str(envelope["date"]).strip())

    def test_save_draft_explicit_folder_skips_resolution(self) -> None:
        """An explicit folder is used verbatim and never triggers a folder listing."""
        runner = drafts_case(folders=["INBOX", "Drafts"])
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(himalaya, "run_himalaya", runner):
                client.op_save_draft(
                    to="a@example.org",
                    subject="Subject",
                    body="Body",
                    folder="Drafts",
                    account="demo",
                    data_dir=Path(tmp),
                )
        self.assertEqual([], runner.actions("folder", "list"))
        save_calls = runner.actions("message", "save")
        self.assertEqual("Drafts", save_calls[0][save_calls[0].index("-f") + 1])

    def test_save_draft_records_envelope_in_the_draft_journal(self) -> None:
        runner = drafts_case(folders=["INBOX", "Drafts"])
        with tempfile.TemporaryDirectory() as tmp:
            journal = drafts.DraftJournal(Path(tmp), "run-save")
            with patch.object(himalaya, "run_himalaya", runner):
                envelope = client.op_save_draft(
                    to=UTF8_TO,
                    subject=UTF8_SUBJECT,
                    body=UTF8_BODY,
                    account="demo",
                    journal=journal,
                )
            payload = json.loads((Path(tmp) / drafts.DRAFT_JOURNAL_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(1, payload["schema_version"])
        entries = list(payload["runs"]["run-save"]["items"].values())
        self.assertEqual(1, len(entries))
        entry = entries[0]
        self.assertEqual("save_draft", entry["action"])
        self.assertEqual(envelope["envelope_id"], entry["envelope_id"])
        self.assertEqual("Drafts", entry["folder"])
        self.assertEqual(UTF8_SUBJECT, entry["subject"])
        self.assertEqual(UTF8_TO, entry["to"])
        self.assertTrue(str(entry["date"]).strip())


class TargetDeleteDraftTests(unittest.TestCase):
    """Red now: deletion must be ``\\Deleted`` + EXPUNGE, never a UID MOVE."""

    def test_delete_draft_marks_deleted_then_expunges_without_move(self) -> None:
        """Rotation (DRAFT-P1, 2026-10-16): the pin moves from ``message delete``
        to ``flag add deleted``.

        Rationale: Himalaya 1.2.0's own ``message delete --help`` states that the
        command "does not really delete the message: if the given folder points to
        the trash folder, it adds the 'deleted' flag to its envelope, otherwise it
        moves it to the trash folder".  On the Drafts folder that is a UID MOVE,
        which the confirmed design forbids.  ``flag add deleted`` sets the
        ``\\Deleted`` flag in place; the following ``folder expunge`` removes the
        marked message for real.
        """
        runner = drafts_case(folders=["INBOX", "Drafts"])
        runner.add_envelope("Drafts", id="41", subject="Alt", message_id="old@example.org")
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(himalaya, "run_himalaya", runner):
                result = client.op_delete_draft(
                    envelope_id="41",
                    folder="Drafts",
                    account="demo",
                    data_dir=Path(tmp),
                )

        flag_calls = runner.actions("flag", "add")
        expunge_calls = runner.actions("folder", "expunge")
        self.assertEqual([["flag", "add", "deleted", "41", "-f", "Drafts"]], flag_calls)
        self.assertEqual([["folder", "expunge", "Drafts"]], expunge_calls)
        self.assertLess(
            runner.index_of_first("flag", "add"),
            runner.index_of_first("folder", "expunge"),
        )
        for command in runner.commands():
            self.assertNotIn("move", command)
            self.assertNotIn("copy", command)
            self.assertNotEqual(["message", "send"], command[:2])
            # ``message delete`` moves a non-trash message to the trash folder.
            self.assertNotEqual(["message", "delete"], command[:2])

        self.assertEqual("41", result["envelope_id"])
        self.assertEqual("Drafts", result["folder"])
        self.assertTrue(result["deleted"])
        self.assertTrue(result["expunged"])
        # The expunge physically removed the marked message.
        self.assertEqual([], runner.envelope_ids("Drafts"))


class _DeleteStepJournalProbe(DraftRunner):
    """A ``DraftRunner`` that snapshots the draft journal when the delete step starts.

    The delete step's first command is ``flag add deleted <id> -f <folder>``; the
    probe reads the journal file at that instant (before delegating to the modeled
    binary), so a test can pin that the replacement entry is already persisted by
    then.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.journal_path: Path | None = None
        self.delete_step_journal_text: str | None = None

    def __call__(
        self,
        args: list[str],
        account: str | None = None,
        timeout: int = 35,
        max_retries: int = 5,
        *,
        stdin_bytes: bytes | None = None,
        **kwargs: Any,
    ) -> str:
        if (
            self.delete_step_journal_text is None
            and list(args)[:2] == ["flag", "add"]
            and self.journal_path is not None
        ):
            self.delete_step_journal_text = (
                self.journal_path.read_text(encoding="utf-8") if self.journal_path.exists() else ""
            )
        return super().__call__(
            args,
            account=account,
            timeout=timeout,
            max_retries=max_retries,
            stdin_bytes=stdin_bytes,
            **kwargs,
        )


class TargetReplaceDraftTests(unittest.TestCase):
    """Red now: replace must append the new draft before deleting the old one."""

    def test_replace_creates_new_draft_before_deleting_old(self) -> None:
        """Rotation (DRAFT-P1, 2026-10-16): the delete step's argv pin moved from
        ``message delete`` to ``flag add deleted`` (rationale:
        ``TargetDeleteDraftTests``); the new-then-delete order is unchanged.
        """
        runner = drafts_case(folders=["INBOX", "Drafts"])
        runner.add_envelope("Drafts", id="41", subject="Alt", message_id="old@example.org")
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(himalaya, "run_himalaya", runner):
                result = client.op_replace_draft(
                    old_envelope_id="41",
                    to="a@example.org",
                    subject="Neu",
                    body="Body",
                    folder="Drafts",
                    account="demo",
                    data_dir=Path(tmp),
                )

        save_index = runner.index_of_first("message", "save")
        flag_index = runner.index_of_first("flag", "add")
        expunge_index = runner.index_of_first("folder", "expunge")
        self.assertGreaterEqual(save_index, 0)
        self.assertLess(save_index, flag_index, "the new draft is appended before the old one is marked deleted")
        self.assertLess(flag_index, expunge_index)
        self.assertEqual([["flag", "add", "deleted", "41", "-f", "Drafts"]], runner.actions("flag", "add"))
        for command in runner.commands():
            self.assertNotIn("move", command)
            self.assertNotIn("copy", command)
            self.assertNotEqual(["message", "send"], command[:2])
            self.assertNotEqual(["message", "delete"], command[:2])

        self.assertEqual("41", result["old_envelope_id"])
        self.assertNotEqual("41", result["envelope_id"])
        self.assertTrue(result["replaced"])

    def test_replace_journal_records_both_uids_and_crash_window(self) -> None:
        runner = drafts_case(folders=["INBOX", "Drafts"])
        runner.add_envelope("Drafts", id="41", subject="Alt", message_id="old@example.org")
        with tempfile.TemporaryDirectory() as tmp:
            journal = drafts.DraftJournal(Path(tmp), "run-replace")
            with patch.object(himalaya, "run_himalaya", runner):
                result = client.op_replace_draft(
                    old_envelope_id="41",
                    to=UTF8_TO,
                    subject=UTF8_SUBJECT,
                    body=UTF8_BODY,
                    folder="Drafts",
                    account="demo",
                    journal=journal,
                )
            payload = json.loads((Path(tmp) / drafts.DRAFT_JOURNAL_FILENAME).read_text(encoding="utf-8"))
            # The batch recovery contract is a different file and stays untouched.
            self.assertFalse((Path(tmp) / "batch-recovery-journal.json").exists())

        entries = list(payload["runs"]["run-replace"]["items"].values())
        self.assertEqual(1, len(entries))
        entry = entries[0]
        self.assertEqual("replace_draft", entry["action"])
        self.assertEqual(result["envelope_id"], entry["envelope_id"])
        self.assertEqual("41", entry["old_envelope_id"])
        self.assertEqual(UTF8_SUBJECT, entry["subject"])
        self.assertEqual(UTF8_TO, entry["to"])
        self.assertTrue(str(entry["crash_window"]).strip(), "the new-then-delete crash window is documented")

    def test_replace_journal_entry_exists_before_the_delete_step(self) -> None:
        """F1 (DRAFT-P1-002, 2026-10-16): the replacement entry is durably recorded
        before the delete step starts, so a crash during ``\\Deleted``/EXPUNGE stays
        reconcile-visible instead of falling into a journal-blind window.
        """
        runner = _DeleteStepJournalProbe(folders=["INBOX", "Drafts"])
        runner.add_envelope("Drafts", id="41", subject="Alt", message_id="old@example.org")
        with tempfile.TemporaryDirectory() as tmp:
            journal = drafts.DraftJournal(Path(tmp), "run-replace-order")
            runner.journal_path = Path(tmp) / drafts.DRAFT_JOURNAL_FILENAME
            with patch.object(himalaya, "run_himalaya", runner):
                client.op_replace_draft(
                    old_envelope_id="41",
                    to="a@example.org",
                    subject="Neu",
                    body="Body",
                    folder="Drafts",
                    account="demo",
                    journal=journal,
                )
            snapshot_text = runner.delete_step_journal_text

        self.assertIsNotNone(snapshot_text, "the delete step must have run")
        snapshot = json.loads(snapshot_text)  # type: ignore[arg-type]
        items = snapshot["runs"]["run-replace-order"]["items"]
        self.assertEqual(
            1,
            len(items),
            "the replacement journal entry must already exist when the delete step starts",
        )
        (entry,) = items.values()
        self.assertEqual("replace_draft", entry["action"])
        self.assertEqual("41", entry["old_envelope_id"])
        self.assertEqual(runner.envelope_ids("Drafts"), ["42"], "the new draft is the surviving one")


class CharacterizationBoundaryTests(unittest.TestCase):
    """Green now and after: the standalone feature stays out of the batch contract."""

    def test_draft_journal_filename_is_distinct_from_batch_recovery_journal(self) -> None:
        self.assertEqual("draft-journal.json", drafts.DRAFT_JOURNAL_FILENAME)
        self.assertNotEqual("batch-recovery-journal.json", drafts.DRAFT_JOURNAL_FILENAME)

    def test_batch_runner_declares_no_draft_api_modes(self) -> None:
        runner_source = (MAIL_DESK_ROOT / "scripts" / "mail_desk_batch_runner.py").read_text(encoding="utf-8")
        for token in ("save_draft", "save-draft", "delete_draft", "delete-draft", "replace_draft", "replace-draft"):
            self.assertNotIn(token, runner_source)

    def test_drafts_module_exposes_no_send_path(self) -> None:
        for attribute in dir(drafts):
            self.assertNotIn("send", attribute.lower())


class CorePackageExportTests(unittest.TestCase):
    """Red now: the draft API is re-exported from ``core`` like every other core module."""

    #: Symbols the package boundary must expose for in-bundle consumers.
    EXPORTED_SYMBOLS = (
        "DraftError",
        "DraftsFolderUnresolvedError",
        "DraftJournalCorruptedError",
        "DraftJournal",
        "build_draft_message",
        "resolve_drafts_folder",
        "save_draft",
        "delete_draft",
        "replace_draft",
        "DRAFT_JOURNAL_FILENAME",
        "REPLACE_CRASH_WINDOW",
    )

    def test_core_package_reexports_the_draft_api_by_object_identity(self) -> None:
        import core as core_package  # noqa: PLC0415 - import-time identity check

        for symbol in self.EXPORTED_SYMBOLS:
            with self.subTest(symbol=symbol):
                self.assertTrue(
                    hasattr(core_package, symbol),
                    f"core must re-export the draft symbol {symbol!r}",
                )
                self.assertIs(
                    getattr(core_package, symbol),
                    getattr(drafts, symbol),
                    f"core.{symbol} must be the same object as core.drafts.{symbol}",
                )


class DraftCliSurfaceTests(unittest.TestCase):
    """Characterization: the three draft ops are standalone CLI subcommands."""

    def test_subcommands_parse_with_their_flags(self) -> None:
        save = client.parse_args(["save-draft", "-t", "a@example.org", "-s", "Betreff", "-b", "Text"])
        self.assertEqual("save-draft", save.subcommand)
        self.assertEqual("a@example.org", save.to)
        self.assertEqual("Betreff", save.subject)
        self.assertEqual("Text", save.body)
        self.assertIsNone(save.folder)

        delete = client.parse_args(["delete-draft", "41", "-f", "Drafts"])
        self.assertEqual("delete-draft", delete.subcommand)
        self.assertEqual("41", delete.envelope_id)

        replace = client.parse_args(["replace-draft", "41", "-t", "a@example.org", "-s", "Betreff"])
        self.assertEqual("replace-draft", replace.subcommand)
        self.assertEqual("41", replace.envelope_id)

    def test_manifest_executes_the_draft_actions(self) -> None:
        """The documented manifest actions run through the same standalone op layer."""
        runner = drafts_case(folders=["INBOX", "Drafts"])
        runner.add_envelope("Drafts", id="41", subject="Alt", message_id="old@example.org")
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "himalaya-op.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "account": "demo",
                        "operations": [
                            {"action": "save_draft", "to": "a@example.org", "subject": "Neu", "body": "Text"},
                            {"action": "delete_draft", "envelope_id": "41", "folder": "Drafts"},
                        ],
                        "delete_input_on_success": False,
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(himalaya, "run_himalaya", runner), patch.object(
                client, "resolve_data_dir", lambda *args, **kwargs: Path(tmp)
            ):
                result = client.execute_manifest(manifest_path, account="demo")
            journal = json.loads(
                (Path(tmp) / drafts.DRAFT_JOURNAL_FILENAME).read_text(encoding="utf-8")
            )

        self.assertTrue(result["all_succeeded"], result["results"])
        self.assertEqual(["save_draft", "delete_draft"], [item["action"] for item in result["results"]])
        self.assertEqual(1, len(runner.actions("message", "save")))
        self.assertEqual([["folder", "expunge", "Drafts"]], runner.actions("folder", "expunge"))
        (run_id,) = list(journal["runs"])
        self.assertEqual("drafts-" + get_iso_week_folder(), run_id)
        self.assertEqual(2, len(journal["runs"][run_id]["items"]))


class OperativeDocsContractTests(unittest.TestCase):
    """Red now: the operative docs and the L2 system map describe the draft API."""

    def _read(self, relative: str) -> str:
        path = MAIL_DESK_ROOT / relative
        self.assertTrue(path.is_file(), f"documentation file is missing: {path}")
        return path.read_text(encoding="utf-8")

    def test_skill_md_separates_save_draft_from_draft_manifest(self) -> None:
        text = self._read("SKILL.md")
        self.assertIn("save_draft", text)
        self.assertIn("DraftManifest", text)
        self.assertIn(
            "Nicht zu verwechseln",
            text,
            "SKILL.md must split the two 'draft' terms explicitly, not implicitly",
        )

    def test_skill_md_no_longer_claims_quarantine_packaging_is_open(self) -> None:
        """Drift fix: the quarantine packaging is finished, not open."""
        text = self._read("SKILL.md")
        self.assertNotIn("Quarantäne-Paketierung unter `core/quarantine/` bleibt offen", text)
        self.assertIn("Quarantäne-Paketierung unter `core/quarantine/` ist abgeschlossen", text)

    def test_cli_operations_documents_the_draft_operations(self) -> None:
        text = self._read("references/cli-operations.md")
        for token in (
            "save-draft",
            "delete-draft",
            "replace-draft",
            '"action": "save_draft"',
            "draft-journal.json",
            "Work In Progress",
            "\\Drafts",
        ):
            with self.subTest(token=token):
                self.assertIn(token, text)

    def test_backend_reference_documents_the_draft_operations(self) -> None:
        text = self._read("references/backends/himalaya.md")
        for token in (
            "save-draft",
            "delete-draft",
            "replace-draft",
            "message save",
            "flag add deleted",
            "folder expunge",
            "\\Drafts",
        ):
            with self.subTest(token=token):
                self.assertIn(token, text)

    def test_system_map_documents_the_draft_api_and_its_journal(self) -> None:
        readme = self._read("docs/system-map/README.md")
        objects = self._read("docs/system-map/objects.md")
        processes = self._read("docs/system-map/processes.md")
        self.assertIn("scripts/core/drafts.py", readme)
        self.assertIn("draft-journal.json", objects)
        self.assertIn("resolve_drafts_folder", processes)
        self.assertIn("folder expunge", processes)


if __name__ == "__main__":
    unittest.main()
