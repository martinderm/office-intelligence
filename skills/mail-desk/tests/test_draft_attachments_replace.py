"""Draft attachments (#18) — ``replace_draft``: explicit full list, omission protection, old-draft safety.

Evidence mode ``tdd``.  Matrix rows R1-R9 (plus K/J rows on the replace path).

Seam note: ``core.drafts.replace_draft`` against the real ``run_himalaya`` code and the synthetic
``MailboxBoundary`` (subprocess boundary only), real workspace lease.  It catches data-loss paths of the
new-then-delete order: unverified new mail, unknown APPEND outcome, shifted / vanished / duplicated old
drafts, omitted attachment lists on drafts that already carry attachments.  Beyond its boundary: a real
mailbox (offline model only).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import drafts  # noqa: E402
from core import himalaya  # noqa: E402
from draft_attachment_fixtures import synthetic_old_draft  # noqa: E402
from test_draft_attachments_save import SECRET, SaveHarness, probe  # noqa: E402,F401

from unittest.mock import patch  # noqa: E402

OLD_ID = "<old@example.org>"


class ReplaceHarness(SaveHarness):
    def setUp(self) -> None:
        super().setUp()
        self.old_raw = synthetic_old_draft(OLD_ID, subject="Alt", attachment=b"OLD-ATTACHMENT-BYTES")
        self.old_env = self.box.seed("Drafts", self.old_raw)  # "42"; the new draft will be "43"

    def replace(self, **overrides):
        params = dict(
            subject="Angebot – Prüfung",
            to="Empfänger <a@example.org>",
            body="Anbei die Unterlagen.",
            folder="Drafts",
            account="demo",
            from_addr="me@example.org",
            date="Sat, 07 Nov 2026 12:00:00 +0100",
            message_id="<att1@example.org>",
            attachments=["data/report.pdf", "memory/cloud/Notiz Ärger.txt"],
            old_message_id=OLD_ID,
            workspace_root=self.root,
            journal=self.journal,
            lease_id=self.LEASE,
            lock_root=self.guard,
        )
        params.update(overrides)
        for key in [k for k, v in params.items() if v is drafts.ATTACHMENTS_UNSPECIFIED]:
            del params[key]
        subject, to, body = params.pop("subject"), params.pop("to"), params.pop("body")
        old_id = params.pop("old_envelope_id", self.old_env)
        with patch.object(himalaya, "resolve_himalaya_invocation", return_value=("himalaya", Path("C:/fake/config.toml"))), \
                patch.object(himalaya.subprocess, "run", self.box):
            return drafts.replace_draft(old_id, subject, to, body, **params)

    def deletes(self) -> list[list[str]]:
        return self.box.commands("flag", "add") + self.box.commands("expunge") + self.box.commands("message", "delete")

    def old_still_stored(self) -> bool:
        return self.old_raw in self.box.stored("Drafts")


class ExplicitReplaceTests(ReplaceHarness):
    def test_full_list_replaces_and_old_is_deleted_only_after_verified_new_mail(self) -> None:  # R1
        result = self.replace()
        self.assertEqual("43", result["envelope_id"])
        self.assertEqual(self.old_env, result["old_envelope_id"])
        self.assertTrue(result["replaced"])
        self.assertTrue(result["old_deleted"])
        self.assertEqual(2, result["attachment_count"])
        self.assertEqual("content", result["verified"])
        self.assertFalse(self.old_still_stored())
        self.assertEqual(1, len(self.box.stored("Drafts")))
        order = [c for c, _ in self.box.calls]
        save_index = next(i for i, c in enumerate(order) if "save" in c)
        new_export = max(i for i, c in enumerate(order) if "export" in c and "43" in c)
        first_delete = next(i for i, c in enumerate(order) if "flag" in c and "add" in c)
        self.assertLess(save_index, new_export)
        self.assertLess(new_export, first_delete, "the old draft is deleted only after the new mail was read back")
        flag = self.box.commands("flag", "add")[0]
        self.assertIn(self.old_env, flag)
        self.assertEqual(1, len(self.box.save_calls))
        run = self.journal_data()["runs"]["run-att"]
        self.assertEqual(self.old_env, run["items"]["43"]["old_envelope_id"])
        self.assertEqual("replaced", run["attempts"]["att1@example.org"]["status"])

    def test_explicit_empty_list_removes_all_attachments_knowingly(self) -> None:  # R4
        result = self.replace(attachments=[])
        self.assertEqual(0, result["attachment_count"])
        self.assertEqual("content", result["verified"])
        self.assertTrue(result["old_deleted"])
        self.assertFalse(self.old_still_stored())

    def test_unexpected_mime_structure_fails_the_verification_and_keeps_the_old_draft(self) -> None:  # review R1 / 2
        import email

        def wrap_in_multipart(raw: str) -> str:
            msg = email.message_from_string(raw, policy=drafts.SMTPUTF8_CRLF)
            msg.make_mixed()
            return msg.as_string()

        self.box.mutate_stored = wrap_in_multipart
        with self.assertRaises(drafts.DraftError) as raised:
            self.replace(attachments=[])
        self.assertEqual(drafts.DRAFT_VERIFICATION_FAILED_REASON, raised.exception.reason_code)
        self.assertIn("structure", str(raised.exception))
        self.assertEqual([], self.deletes())
        self.assertTrue(self.old_still_stored())

    def test_explicit_mode_needs_the_real_lease_and_the_old_message_id(self) -> None:  # K, R7
        with self.assertRaises(drafts.DraftError) as raised:
            self.replace(lease_id="foreign")
        self.assertEqual("draft_lock_missing", raised.exception.reason_code)
        with self.assertRaises(drafts.DraftError) as raised:
            self.replace(old_message_id=None)
        self.assertEqual(drafts.DRAFT_OLD_IDENTITY_MISMATCH_REASON, raised.exception.reason_code)
        self.assertEqual([], self.box.save_calls)
        self.assertTrue(self.old_still_stored())

    def test_old_draft_with_a_different_message_id_is_neither_replaced_nor_deleted(self) -> None:  # R7
        with self.assertRaises(drafts.DraftError) as raised:
            self.replace(old_message_id="<someone-else@example.org>")
        self.assertEqual(drafts.DRAFT_OLD_IDENTITY_MISMATCH_REASON, raised.exception.reason_code)
        self.assertEqual([], self.box.save_calls)
        self.assertEqual([], self.deletes())

    def test_unreadable_old_draft_stops_before_the_append(self) -> None:  # R3
        self.box.export_exit = 1
        self.box.export_stderr = f"nope {SECRET}"
        with self.assertRaises(drafts.DraftError) as raised:
            self.replace()
        self.assertEqual(drafts.DRAFT_OLD_UNREADABLE_REASON, raised.exception.reason_code)
        self.assertEqual([], self.box.save_calls)
        self.assertNotIn(SECRET, probe(raised.exception))

    def test_new_message_id_equal_to_the_old_one_is_refused(self) -> None:  # R7
        with self.assertRaises(drafts.DraftError) as raised:
            self.replace(message_id=OLD_ID)
        self.assertEqual(drafts.DRAFT_MESSAGE_ID_NOT_UNIQUE_REASON, raised.exception.reason_code)
        self.assertEqual([], self.box.save_calls)
        self.assertTrue(self.old_still_stored())


class ReplaceSizeBoundTests(ReplaceHarness):
    def test_every_replace_mode_obeys_the_ten_mib_bound_before_the_append(self) -> None:  # review R1 / 4
        from draft_attachment_fixtures import exact_size_body

        over = exact_size_body(10485760 + 1, (), message_id="<att1@example.org>", date="Sat, 07 Nov 2026 12:00:00 +0100",
                               )
        common = dict(subject="Betreff", to="a@example.org", from_addr=None, body=over)
        for label, extra in (
            ("explicit empty list", dict(attachments=[])),
            ("omitted", dict(attachments=drafts.ATTACHMENTS_UNSPECIFIED, workspace_root=None, lease_id=None, lock_root=None)),
        ):
            with self.subTest(mode=label):
                with self.assertRaises(drafts.DraftError) as raised:
                    self.replace(old_envelope_id=self.old_env, **common, **extra)
                self.assertEqual("message_too_large", raised.exception.reason_code)
                self.assertEqual([], self.box.save_calls)
                self.assertEqual([], self.deletes())
                self.assertTrue(self.old_still_stored())


class OldDraftProtectionTests(ReplaceHarness):
    def test_verification_failure_keeps_the_old_draft_and_issues_no_delete(self) -> None:  # R5
        self.box.mutate_stored = lambda raw: raw.replace("Anbei die Unterlagen.", "Anbei die Unterlagen!")
        with self.assertRaises(drafts.DraftError) as raised:
            self.replace()
        self.assertEqual(drafts.DRAFT_VERIFICATION_FAILED_REASON, raised.exception.reason_code)
        self.assertEqual([], self.deletes())
        self.assertTrue(self.old_still_stored())

    def test_unknown_append_outcome_keeps_the_old_draft(self) -> None:  # R6
        self.box.save_raises = OSError(206, f"launch {SECRET}")
        with self.assertRaises(drafts.DraftError) as raised:
            self.replace()
        self.assertEqual("draft_append_outcome_unknown", raised.exception.reason_code)
        self.assertEqual(1, len(self.box.save_calls))
        self.assertEqual([], self.deletes())
        self.assertTrue(self.old_still_stored())
        self.assertNotIn(SECRET, probe(raised.exception))

    def test_definite_append_failure_keeps_the_old_draft(self) -> None:  # R6
        self.box.save_exit = 1
        with self.assertRaises(drafts.DraftError):
            self.replace()
        self.assertEqual([], self.deletes())
        self.assertTrue(self.old_still_stored())

    def test_old_draft_that_was_renumbered_is_resolved_again_by_message_id(self) -> None:  # R8
        def renumber() -> None:
            for message in self.box.messages["Drafts"]:
                if message["id"] == self.old_env:
                    message["id"] = "7"

        self.box.after_save = renumber
        result = self.replace()
        self.assertTrue(result["old_deleted"])
        flag = self.box.commands("flag", "add")[0]
        self.assertIn("7", flag)
        self.assertNotIn(self.old_env, flag, "the stale transient id must not be used for the delete")
        self.assertFalse(self.old_still_stored())

    def test_old_draft_that_vanished_is_reported_not_deleted_again(self) -> None:  # R8
        self.box.after_save = lambda: self.box.messages["Drafts"].__delitem__(0)
        result = self.replace()
        self.assertFalse(result["old_deleted"])
        self.assertEqual("old_draft_not_found", result["old_status"])
        self.assertEqual([], self.box.commands("flag", "add"))

    def test_ambiguous_old_message_id_is_not_deleted(self) -> None:  # R8
        self.box.seed("Drafts", self.old_raw)  # a second draft with the same Message-ID
        with self.assertRaises(drafts.DraftPartialError) as raised:
            self.replace()
        self.assertEqual(drafts.DRAFT_REPLACE_OLD_NOT_DELETED_REASON, raised.exception.reason_code)
        self.assertEqual("44", raised.exception.new_envelope["envelope_id"])
        self.assertEqual([], self.box.commands("flag", "add"))

    def test_delete_failure_after_success_is_a_partial_outcome_with_the_new_envelope(self) -> None:  # R9
        self.box.flag_exit = 1
        self.box.flag_stderr = f"imap said no {SECRET}"
        with self.assertRaises(drafts.DraftPartialError) as raised:
            self.replace()
        error = raised.exception
        self.assertEqual(drafts.DRAFT_REPLACE_OLD_NOT_DELETED_REASON, error.reason_code)
        self.assertEqual("43", error.new_envelope["envelope_id"])
        self.assertNotIn(SECRET, probe(error))
        self.assertEqual("old_delete_failed", self.attempt()["status"])
        self.assertTrue(self.old_still_stored())
        self.assertEqual(2, len(self.box.stored("Drafts")), "both drafts stay observable and are reported")


class OldDraftParsingTests(ReplaceHarness):
    """Attachment freeness is never derived from a defective structure or an ambiguous identity (review R1 / 2)."""

    NO_BOUNDARY = (
        "Message-ID: <broken@example.org>\r\nSubject: Alt\r\nTo: old@example.org\r\nMIME-Version: 1.0\r\n"
        "Content-Type: multipart/mixed\r\n\r\nLooks like plain text but hides an attachment part.\r\n"
    )
    DUPLICATE_ID = (
        "Message-ID: <dupid@example.org>\r\nMessage-ID: <other@example.org>\r\nSubject: Alt\r\nTo: old@example.org\r\n\r\nText\r\n"
    )

    def legacy(self, old_id: str, **overrides):
        params = dict(old_envelope_id=old_id, old_message_id=None, attachments=drafts.ATTACHMENTS_UNSPECIFIED,
                      workspace_root=None, lease_id=None, lock_root=None, message_id=None, from_addr=None)
        params.update(overrides)
        return self.replace(**params)

    def test_defective_old_structure_is_not_treated_as_attachment_free(self) -> None:
        old = self.box.seed("Drafts", self.NO_BOUNDARY)
        with self.assertRaises(drafts.DraftError) as raised:
            self.legacy(old)
        self.assertEqual(drafts.DRAFT_OLD_UNREADABLE_REASON, raised.exception.reason_code)
        self.assertEqual([], self.box.save_calls)
        self.assertEqual([], self.deletes())

    def test_duplicate_message_id_headers_of_the_old_draft_are_ambiguous_in_every_mode(self) -> None:
        old = self.box.seed("Drafts", self.DUPLICATE_ID)
        with self.assertRaises(drafts.DraftError) as omitted:
            self.legacy(old)
        self.assertEqual(drafts.DRAFT_OLD_UNREADABLE_REASON, omitted.exception.reason_code)
        with self.assertRaises(drafts.DraftError) as explicit:
            self.replace(old_envelope_id=old, old_message_id="<dupid@example.org>")
        self.assertEqual(drafts.DRAFT_OLD_UNREADABLE_REASON, explicit.exception.reason_code)
        self.assertEqual([], self.box.save_calls)
        self.assertEqual([], self.deletes())

    def test_explicit_list_tolerates_a_mime_defect_of_the_old_draft_but_not_its_identity(self) -> None:
        old = self.box.seed("Drafts", self.NO_BOUNDARY)
        result = self.replace(old_envelope_id=old, old_message_id="<broken@example.org>")
        self.assertTrue(result["old_deleted"])


class OmittedVerifiedReplaceTests(ReplaceHarness):
    """Omitted attachments (text-only old draft) still get the full verified replace (review R1 / 3).

    API rationale: omission cannot name a workspace, so no lease / file access is involved, but the new
    mail, the Message-ID precheck, the journal and the re-resolution of the old draft are the very same
    steps as for an explicit list.  The old draft's own Message-ID is read from the old draft; a given
    ``old_message_id`` must match.  Without a journal there is no reconcile trail, so it is refused.
    """

    def setUp(self) -> None:
        super().setUp()
        self.plain_raw = synthetic_old_draft("<plain@example.org>", subject="Plain")
        self.plain_env = self.box.seed("Drafts", self.plain_raw)  # "43"; the new draft becomes "44"

    def omitted(self, **overrides):
        params = dict(old_envelope_id=self.plain_env, old_message_id=None, attachments=drafts.ATTACHMENTS_UNSPECIFIED,
                      workspace_root=None, lease_id=None, lock_root=None, message_id=None)
        params.update(overrides)
        return self.replace(**params)

    def test_success_uses_the_shared_verification_journal_and_re_resolution(self) -> None:
        result = self.omitted()
        self.assertEqual("44", result["envelope_id"])
        self.assertEqual("content", result["verified"])
        self.assertEqual(0, result["attachment_count"])
        self.assertTrue(result["old_deleted"])
        self.assertEqual(1, len(self.box.save_calls))
        order = [c for c, _ in self.box.calls]
        new_export = max(i for i, c in enumerate(order) if "export" in c and "44" in c)
        first_delete = next(i for i, c in enumerate(order) if "flag" in c and "add" in c)
        self.assertLess(new_export, first_delete)
        run = self.journal_data()["runs"]["run-att"]
        self.assertEqual(self.plain_env, run["items"]["44"]["old_envelope_id"])
        (attempt,) = [a for a in run["attempts"].values() if a.get("envelope_id") == "44"]
        self.assertEqual("replaced", attempt["status"])
        self.assertEqual("plain@example.org", attempt["old_message_id"])

    def test_body_mutation_of_the_stored_new_mail_keeps_the_old_draft(self) -> None:
        self.box.mutate_stored = lambda raw: raw.replace("Anbei die Unterlagen.", "Anbei die Unterlagen!")
        with self.assertRaises(drafts.DraftError) as raised:
            self.omitted()
        self.assertEqual(drafts.DRAFT_VERIFICATION_FAILED_REASON, raised.exception.reason_code)
        self.assertEqual([], self.deletes())
        self.assertIn(self.plain_raw, self.box.stored("Drafts"))

    def test_message_id_collisions_are_refused_before_the_append(self) -> None:
        for taken in ("<plain@example.org>", "<att1@example.org>"):
            with self.subTest(taken=taken):
                self.setUp()
                if taken == "<att1@example.org>":
                    self.box.seed("Drafts", synthetic_old_draft(taken, subject="Anderer Betreff"))
                with self.assertRaises(drafts.DraftError) as raised:
                    self.omitted(message_id=taken)
                self.assertEqual(drafts.DRAFT_MESSAGE_ID_NOT_UNIQUE_REASON, raised.exception.reason_code)
                self.assertEqual([], self.box.save_calls)
                self.assertEqual([], self.deletes())

    def test_renumbered_old_draft_is_deleted_by_its_current_id_only(self) -> None:
        def renumber() -> None:
            for message in self.box.messages["Drafts"]:
                if message["id"] == self.plain_env:
                    message["id"] = "9"

        self.box.after_save = renumber
        result = self.omitted()
        self.assertTrue(result["old_deleted"])
        flag = self.box.commands("flag", "add")[0]
        self.assertIn("9", flag)
        self.assertNotIn(self.plain_env, flag)

    def test_vanished_old_draft_is_reported_not_deleted(self) -> None:
        self.box.after_save = lambda: self.box.messages["Drafts"].__delitem__(1)
        result = self.omitted()
        self.assertFalse(result["old_deleted"])
        self.assertEqual("old_draft_not_found", result["old_status"])
        self.assertEqual([], self.box.commands("flag", "add"))

    def test_unknown_append_outcome_and_delete_failure_are_honest(self) -> None:
        self.box.save_raises = OSError(206, f"x {SECRET}")
        with self.assertRaises(drafts.DraftError) as unknown:
            self.omitted()
        self.assertEqual("draft_append_outcome_unknown", unknown.exception.reason_code)
        self.assertEqual([], self.deletes())
        self.setUp()
        self.box.flag_exit = 1
        self.box.flag_stderr = f"no {SECRET}"
        with self.assertRaises(drafts.DraftPartialError) as partial:
            self.omitted()
        self.assertEqual(drafts.DRAFT_REPLACE_OLD_NOT_DELETED_REASON, partial.exception.reason_code)
        self.assertEqual("44", partial.exception.new_envelope["envelope_id"])
        self.assertNotIn(SECRET, probe(partial.exception))

    def test_a_missing_journal_is_refused_before_any_mailbox_write(self) -> None:
        with self.assertRaises(drafts.DraftError) as raised:
            self.omitted(journal=None)
        self.assertEqual("draft_binding_failed", raised.exception.reason_code)
        self.assertEqual([], self.box.save_calls)
        self.assertEqual([], self.deletes())

    def test_given_old_message_id_must_match_the_old_draft(self) -> None:
        with self.assertRaises(drafts.DraftError) as raised:
            self.omitted(old_message_id="<someone-else@example.org>")
        self.assertEqual(drafts.DRAFT_OLD_IDENTITY_MISMATCH_REASON, raised.exception.reason_code)
        self.assertEqual([], self.box.save_calls)
        result = self.omitted(old_message_id="<plain@example.org>")
        self.assertTrue(result["old_deleted"])


class OmittedAttachmentListTests(ReplaceHarness):
    """Omitting ``attachments`` must never silently drop what an existing draft carries."""

    def legacy_replace(self, **overrides):
        params = dict(old_message_id=None, attachments=drafts.ATTACHMENTS_UNSPECIFIED, workspace_root=None,
                      lease_id=None, lock_root=None, message_id=None, from_addr=None)
        params.update(overrides)
        return self.replace(**params)

    def test_omitted_list_with_an_old_draft_that_has_attachments_is_refused(self) -> None:  # R2
        with self.assertRaises(drafts.DraftError) as raised:
            self.legacy_replace()
        self.assertEqual(drafts.DRAFT_REPLACE_WOULD_DROP_ATTACHMENTS_REASON, raised.exception.reason_code)
        self.assertEqual([], self.box.save_calls)
        self.assertEqual([], self.deletes())
        self.assertTrue(self.old_still_stored())
        self.assertNotIn("OLD-ATTACHMENT-BYTES", probe(raised.exception))

    def test_omitted_list_with_a_text_only_old_draft_keeps_the_former_behaviour(self) -> None:  # R3
        plain_id = self.box.seed("Drafts", synthetic_old_draft("<plain@example.org>", subject="Plain"))
        result = self.legacy_replace(old_envelope_id=plain_id)
        self.assertTrue(result["replaced"])
        self.assertEqual(0, result["attachment_count"])
        self.assertEqual("content", result["verified"], "omitted replace shares the semantic new-mail verification")
        remaining = self.box.stored("Drafts")
        self.assertEqual(2, len(remaining))  # attachment-bearing old draft + the new one; plain old is gone

    def test_omitted_list_with_unreadable_old_draft_fails_closed(self) -> None:  # R3
        self.box.export_exit = 1
        with self.assertRaises(drafts.DraftError) as raised:
            self.legacy_replace()
        self.assertEqual(drafts.DRAFT_OLD_UNREADABLE_REASON, raised.exception.reason_code)
        self.assertEqual([], self.box.save_calls)

    def test_omitted_list_with_expected_old_message_id_checks_the_identity(self) -> None:  # R3
        plain_id = self.box.seed("Drafts", synthetic_old_draft("<plain@example.org>", subject="Plain"))
        with self.assertRaises(drafts.DraftError) as raised:
            self.legacy_replace(old_envelope_id=plain_id, old_message_id="<other@example.org>")
        self.assertEqual(drafts.DRAFT_OLD_IDENTITY_MISMATCH_REASON, raised.exception.reason_code)
        self.assertEqual([], self.box.save_calls)


if __name__ == "__main__":
    unittest.main()
