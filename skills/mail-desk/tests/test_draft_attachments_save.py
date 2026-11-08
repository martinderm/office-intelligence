"""Draft attachments (#18) — ``save_draft`` with attachments through the real transport seam.

Evidence mode ``tdd``.  Matrix rows L1, L4, L7, P6, K1-K4, S1-S6, V1-V4, J1-J4, C3.

Seam note: ``core.drafts.save_draft`` runs against the real ``core.himalaya.run_himalaya`` /
``fetch_raw_message_eml`` / ``find_envelopes_by_message_id`` code; only the ``subprocess.run`` system
boundary is replaced by ``MailboxBoundary`` (a small synthetic Himalaya 1.2.0 model).  The workspace lease
is the REAL shared ``workspace-lock`` guard in a temporary consumer workspace (set
``OI_TEST_WORKSPACE_LOCK_ROOT``; a missing installation is a test failure, never a silent skip).
Expected values (bytes of the files read straight from disk, SHA-256 worked values, command tokens) are
independent of the production code.  This is an offline model, not a live mailbox proof.
"""

from __future__ import annotations

import base64
import email
import email.policy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import traceback
import unittest
from pathlib import Path
from unittest.mock import patch

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import diagnostics as diag  # noqa: E402
from core import draft_attachments as da  # noqa: E402
from core import drafts  # noqa: E402
from core import himalaya  # noqa: E402
from draft_attachment_fixtures import (  # noqa: E402
    MailboxBoundary,
    exact_size_body,
    make_workspace,
    synthetic_old_draft,
)

TEN_MIB = 10485760
SECRET = "SECRET-ATTACHMENT-MARKER-0451"
BINARY = bytes(range(256)) * 20  # 5120 bytes, every byte value


def guard_root() -> Path:
    """The trusted workspace-lock installation (never a silent skip)."""
    for name in ("OI_TEST_WORKSPACE_LOCK_ROOT", "WORKSPACE_LOCK_ROOT"):
        value = os.environ.get(name, "").strip()
        if value and (Path(value) / "scripts" / "workspace_lock_guard.py").is_file():
            return Path(value)
    sibling = MAIL_DESK_ROOT.parents[2] / "workspace-lock"
    if (sibling / "scripts" / "workspace_lock_guard.py").is_file():
        return sibling
    raise AssertionError(
        "workspace-lock is not installed: set OI_TEST_WORKSPACE_LOCK_ROOT to the trusted installation "
        "(see README.md#workspace-lock-voraussetzung); this suite never skips the lease checks silently"
    )


def probe(exc: BaseException) -> str:
    """Everything a log or traceback would show for ``exc`` (message, chain, context)."""
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


class SaveHarness(unittest.TestCase):
    LEASE = "draft-att-lease-1"

    def setUp(self) -> None:
        self.guard = guard_root()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = make_workspace(Path(self._tmp.name))
        guard = diag._load_workspace_lock_guard(self.guard)
        self.assertEqual("Acquired", guard.acquire_workspace_lock(self.root, harness="test", lease_id=self.LEASE).state)
        self.journal = drafts.DraftJournal(self.root / "data" / "mail-desk", "run-att")
        self.box = MailboxBoundary()
        self.files = {
            "data/report.pdf": BINARY,
            "memory/cloud/Notiz Ärger.txt": "Grüße – äöü 🎯\r\nzweite Zeile\n".encode("utf-8"),
        }
        for rel, data in self.files.items():
            target = self.root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

    # -- helpers -----------------------------------------------------------
    def save(self, **overrides):
        params = dict(
            subject="Angebot – Prüfung",
            to="Empfänger <a@example.org>",
            body="Anbei die Unterlagen.",
            account="demo",
            from_addr="me@example.org",
            date="Sat, 07 Nov 2026 12:00:00 +0100",
            message_id="<att1@example.org>",
            attachments=["data/report.pdf", "memory/cloud/Notiz Ärger.txt"],
            workspace_root=self.root,
            journal=self.journal,
            lease_id=self.LEASE,
            lock_root=self.guard,
        )
        params.update(overrides)
        subject, to, body = params.pop("subject"), params.pop("to"), params.pop("body")
        with patch.object(himalaya, "resolve_himalaya_invocation", return_value=("himalaya", Path("C:/fake/config.toml"))), \
                patch.object(himalaya.subprocess, "run", self.box):
            return drafts.save_draft(subject, to, body, **params)

    def journal_data(self) -> dict:
        return json.loads((self.root / "data" / "mail-desk" / drafts.DRAFT_JOURNAL_FILENAME).read_text(encoding="utf-8"))

    def attempt(self, message_id: str = "att1@example.org") -> dict:
        return self.journal_data()["runs"]["run-att"]["attempts"][message_id]

    def assertNoMailboxContact(self) -> None:  # noqa: N802
        self.assertEqual([], self.box.calls, "no mailbox command may run")

    def assertContentFree(self, exc: BaseException) -> None:  # noqa: N802
        text = probe(exc)
        for needle in (SECRET, BINARY[:40].hex(), base64.b64encode(BINARY[:60]).decode("ascii"), str(self.root)):
            self.assertNotIn(needle, text)


class SuccessTests(SaveHarness):
    def test_complete_draft_is_appended_once_and_verified_semantically(self) -> None:  # S1, S2
        result = self.save()
        self.assertEqual(1, len(self.box.save_calls), "exactly one APPEND")
        tokens = self.box.save_calls[0]
        self.assertEqual("plain", tokens[tokens.index("-o") + 1])
        self.assertEqual("Drafts", tokens[tokens.index("-f") + 1])
        self.assertEqual("demo", tokens[tokens.index("-a") + 1])
        payload = self.box.saved_payloads[0]
        self.assertTrue(payload.endswith(b"\r\n"), "canonical raw + one transport CRLF")
        stored = email.message_from_bytes(payload, policy=email.policy.SMTPUTF8)
        decoded = [p.get_payload(decode=True) for p in stored.iter_attachments()]
        self.assertEqual(
            [Path(self.root / r).read_bytes() for r in self.files], decoded, "decoded binary identity from disk"
        )
        self.assertEqual(["report.pdf", "Notiz Ärger.txt"], [p.get_filename() for p in stored.iter_attachments()])
        self.assertEqual(["application/pdf", "text/plain"], [p.get_content_type() for p in stored.iter_attachments()])
        self.assertEqual("42", result["envelope_id"])
        self.assertEqual("Drafts", result["folder"])
        self.assertEqual("att1@example.org", result["message_id"])
        self.assertEqual(2, result["attachment_count"])
        self.assertEqual("content", result["verified"])
        self.assertEqual(hashlib.sha256(BINARY).hexdigest(), result["attachments"][0]["sha256"])
        self.assertEqual(len(payload) - 2, result["size_bytes"])
        # Read-back ran after the APPEND, through export (not only the envelope header).
        order = [c for c, _ in self.box.calls]
        save_index = next(i for i, c in enumerate(order) if "save" in c)
        export_index = next(i for i, c in enumerate(order) if "export" in c)
        self.assertGreater(export_index, save_index)

    def test_target_folder_is_resolved_by_special_use_or_given_explicitly(self) -> None:  # S6
        self.box.folders = ["INBOX", {"name": "Entwürfe", "attributes": ["\\Drafts"]}]
        result = self.save()
        self.assertEqual("Entwürfe", result["folder"])
        self.assertEqual(1, len(self.box.stored("Entwürfe")))
        box2_result = self.save(folder="Drafts", message_id="<att2@example.org>")
        self.assertEqual("Drafts", box2_result["folder"])

    def test_text_only_save_keeps_the_old_path_without_binding_or_read_back(self) -> None:  # compat
        with patch.object(himalaya, "resolve_himalaya_invocation", return_value=("himalaya", Path("C:/fake/config.toml"))), \
                patch.object(himalaya.subprocess, "run", self.box):
            result = drafts.save_draft("S", "a@example.org", "T", account="demo", message_id="<plain@example.org>")
        self.assertNotIn("attachment_count", result)
        self.assertEqual([], self.box.commands("export"))

    def test_lease_credentials_may_come_from_the_environment(self) -> None:  # C3
        with patch.dict(os.environ, {"WORKSPACE_LOCK_LEASE_ID": self.LEASE, "WORKSPACE_LOCK_ROOT": str(self.guard)}):
            result = self.save(lease_id=None, lock_root=None)
        self.assertEqual("42", result["envelope_id"])

    def test_journal_holds_metadata_but_never_content(self) -> None:  # J2, J4
        self.save()
        text = (self.root / "data" / "mail-desk" / drafts.DRAFT_JOURNAL_FILENAME).read_text(encoding="utf-8")
        attempt = self.attempt()
        self.assertEqual("saved_verified", attempt["status"])
        self.assertEqual("Drafts", attempt["folder"])
        self.assertEqual(
            [
                {"filename": "report.pdf", "content_type": "application/pdf", "size": 5120,
                 "sha256": hashlib.sha256(BINARY).hexdigest()},
                {"filename": "Notiz Ärger.txt", "content_type": "text/plain", "size": len(self.files["memory/cloud/Notiz Ärger.txt"]),
                 "sha256": hashlib.sha256(self.files["memory/cloud/Notiz Ärger.txt"]).hexdigest()},
            ],
            attempt["attachments"],
        )
        self.assertNotIn(base64.b64encode(BINARY[:60]).decode("ascii"), text)
        self.assertEqual("save_draft", self.journal_data()["runs"]["run-att"]["items"]["42"]["action"])

    def test_attempt_is_journaled_before_the_append_starts(self) -> None:  # J1
        seen: list[str] = []
        self.box.on_save = lambda: seen.append(self.attempt()["status"])
        self.save()
        self.assertEqual(["append_started"], seen)


class LimitTests(SaveHarness):
    def test_more_than_five_attachments_never_touch_files_or_mailbox(self) -> None:  # L1, L7
        specs = []
        for index in range(6):
            (self.root / "data" / f"f{index}.txt").write_bytes(b"x")
            specs.append(f"data/f{index}.txt")
        opened: list[str] = []
        real_open = os.open
        with patch.object(os, "open", lambda p, *a, **k: (opened.append(str(p)), real_open(p, *a, **k))[1]):
            with self.assertRaises(da.DraftAttachmentError) as raised:
                self.save(attachments=specs)
        self.assertEqual("attachment_count_exceeded", raised.exception.reason_code)
        self.assertEqual([], [p for p in opened if "f0.txt" in p])
        self.assertNoMailboxContact()
        self.assertNotIn("attempts", self.journal_data()["runs"]["run-att"])

    def test_five_attachments_are_accepted(self) -> None:  # L1
        specs = []
        for index in range(5):
            (self.root / "data" / f"f{index}.txt").write_bytes(b"x%d" % index)
            specs.append(f"data/f{index}.txt")
        result = self.save(attachments=specs)
        self.assertEqual(5, result["attachment_count"])

    def test_message_of_exactly_ten_mib_is_appended_and_one_byte_more_is_not(self) -> None:  # L4, L7
        big = bytes(range(256)) * 12288
        (self.root / "data" / "a.bin").write_bytes(big)
        (self.root / "data" / "b.bin").write_bytes(big)
        specs = ["data/a.bin", "data/b.bin"]
        prepared = da.load_attachments(self.root, specs)
        common = dict(subject="Betreff", to="a@example.org", message_id="<size@example.org>", from_addr=None,
                      attachments=specs)
        body = exact_size_body(TEN_MIB, prepared, message_id="<size@example.org>", date="Sat, 07 Nov 2026 12:00:00 +0100")
        result = self.save(body=body, **common)
        self.assertEqual(TEN_MIB, result["size_bytes"])
        self.assertEqual(TEN_MIB + 2, len(self.box.saved_payloads[0]), "transport CRLF is not mail content")
        before = len(self.box.calls)
        over = exact_size_body(TEN_MIB + 1, prepared, message_id="<size2@example.org>", date="Sat, 07 Nov 2026 12:00:00 +0100")
        with self.assertRaises(da.DraftAttachmentError) as raised:
            self.save(body=over, **{**common, "message_id": "<size2@example.org>"})
        self.assertEqual("message_too_large", raised.exception.reason_code)
        self.assertEqual(before, len(self.box.calls), "rejected before any mailbox command")
        self.assertEqual(1, len(self.box.save_calls))

    def test_oversized_total_is_rejected_before_append_without_journal_attempt(self) -> None:  # L7
        big = bytes(range(256)) * 12288
        specs = []
        for index in range(3):
            (self.root / "data" / f"big{index}.bin").write_bytes(big)
            specs.append(f"data/big{index}.bin")
        with self.assertRaises(da.DraftAttachmentError) as raised:
            self.save(attachments=specs)
        self.assertEqual("message_too_large", raised.exception.reason_code)
        self.assertNoMailboxContact()
        self.assertNotIn("attempts", self.journal_data()["runs"]["run-att"])


class BindingTests(SaveHarness):
    def assertBinding(self, reason: str, **overrides) -> None:  # noqa: N802
        with self.assertRaises(drafts.DraftError) as raised:
            self.save(**overrides)
        self.assertEqual(reason, raised.exception.reason_code)
        self.assertNoMailboxContact()
        self.assertContentFree(raised.exception)

    def test_workspace_root_and_journal_are_required(self) -> None:  # K1
        self.assertBinding("draft_binding_failed", workspace_root=None)
        self.assertBinding("draft_binding_failed", journal=None)

    def test_missing_or_foreign_lease_is_refused_before_any_file_is_read(self) -> None:  # K2
        opened: list[str] = []
        real_open = os.open
        with patch.object(os, "open", lambda p, *a, **k: (opened.append(str(p)), real_open(p, *a, **k))[1]):
            self.assertBinding("draft_lock_missing", lease_id="foreign-lease")
            self.assertBinding("draft_lock_missing", lease_id=None)
        self.assertEqual([], [p for p in opened if "report.pdf" in p])

    def test_account_must_be_bound_and_match(self) -> None:  # K3
        self.assertBinding("draft_binding_failed", account="other")
        result = self.save(account=None)  # falls back to the configured account
        self.assertEqual("demo", self.box.save_calls[0][self.box.save_calls[0].index("-a") + 1])
        self.assertEqual("42", result["envelope_id"])

    def test_null_account_configuration_is_refused(self) -> None:  # K3
        (self.root / ".agents" / "mail-desk-backend.json").write_text(
            json.dumps({"schema_version": 1, "backend": "himalaya", "account": None}), encoding="utf-8"
        )
        self.assertBinding("draft_binding_failed", account=None)

    def test_journal_must_live_in_the_workspace_data_directory(self) -> None:  # K4
        other = drafts.DraftJournal(Path(self._tmp.name) / "elsewhere", "run-x")
        self.assertBinding("draft_binding_failed", journal=other)


class OutcomeTests(SaveHarness):
    def test_definite_append_failure_is_classified_and_journaled(self) -> None:  # S3, S5, J2
        self.box.save_exit = 1
        self.box.save_stderr = f"server said NO {SECRET}"
        with self.assertRaises(drafts.DraftError) as raised:
            self.save()
        self.assertEqual(drafts.DRAFT_SAVE_FAILED_REASON, raised.exception.reason_code)
        self.assertEqual(1, len(self.box.save_calls))
        self.assertEqual([], self.box.commands("export"), "no verification after a failed APPEND")
        self.assertEqual("append_failed", self.attempt()["status"])
        self.assertContentFree(raised.exception)
        self.assertIsNone(raised.exception.__cause__)

    def test_unknown_outcomes_are_never_retried_and_are_journaled_honestly(self) -> None:  # S4, S5, J2
        cases = {
            "timeout": subprocess.TimeoutExpired(["himalaya"], 35),
            "launch": OSError(206, f"too long {SECRET}"),
            "pipe": BrokenPipeError(),
            "other backend exception": RuntimeError(f"boom {SECRET} {BINARY[:40].hex()}"),
        }
        for label, failure in cases.items():
            with self.subTest(label=label):
                self.setUp()
                self.box.save_raises = failure
                with self.assertRaises(drafts.DraftError) as raised:
                    self.save()
                self.assertEqual("draft_append_outcome_unknown", raised.exception.reason_code)
                self.assertEqual(1, len(self.box.save_calls), "unknown outcome: exactly one APPEND, no retry")
                self.assertEqual([], self.box.commands("export"))
                self.assertEqual("append_outcome_unknown", self.attempt()["status"])
                self.assertIn("att1@example.org", str(raised.exception))
                self.assertContentFree(raised.exception)

    def test_transient_error_after_the_payload_was_sent_counts_as_unknown(self) -> None:  # S4
        self.box.save_exit = 1
        self.box.save_stderr = f"TLS stream reset {SECRET}"
        with self.assertRaises(drafts.DraftError) as raised:
            self.save()
        self.assertEqual("draft_append_outcome_unknown", raised.exception.reason_code)
        self.assertEqual(1, len(self.box.save_calls))
        self.assertContentFree(raised.exception)

    def test_message_id_already_in_the_folder_blocks_the_append(self) -> None:  # V-dup (pre)
        self.box.seed("Drafts", synthetic_old_draft("<att1@example.org>", subject="Angebot – Prüfung"))
        with self.assertRaises(drafts.DraftError) as raised:
            self.save()
        self.assertEqual("draft_message_id_not_unique", raised.exception.reason_code)
        self.assertEqual([], self.box.save_calls)

    def test_journal_failure_before_the_append_aborts_without_append(self) -> None:  # J3
        with patch("os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(drafts.DraftError) as raised:
                self.save()
        self.assertEqual("draft_journal_write_failed", raised.exception.reason_code)
        self.assertEqual([], self.box.save_calls)

    def test_journal_failure_after_the_append_is_reported_honestly(self) -> None:  # J3
        real_replace = os.replace
        calls = {"n": 0}

        def failing_replace(src, dst):
            calls["n"] += 1
            if calls["n"] >= 2:  # 1st = append_started; everything after the APPEND fails
                raise OSError("disk full")
            return real_replace(src, dst)

        with patch("os.replace", failing_replace):
            with self.assertRaises(drafts.DraftError) as raised:
                self.save()
        self.assertEqual("draft_journal_write_failed", raised.exception.reason_code)
        self.assertEqual(1, len(self.box.save_calls))
        self.assertIn("appended", str(raised.exception))


class VerificationTests(SaveHarness):
    def assertUnverified(self, mutate=None, **box_flags) -> drafts.DraftError:  # noqa: N802
        self.box.mutate_stored = mutate
        for name, value in box_flags.items():
            setattr(self.box, name, value)
        with self.assertRaises(drafts.DraftError) as raised:
            self.save()
        self.assertEqual(drafts.DRAFT_VERIFICATION_FAILED_REASON, raised.exception.reason_code)
        self.assertEqual("appended_unverified", self.attempt()["status"])
        self.assertEqual(1, len(self.box.save_calls))
        self.assertContentFree(raised.exception)
        return raised.exception

    def test_each_semantic_deviation_fails_the_verification(self) -> None:  # V1, V2, V4
        first_line = base64.b64encode(BINARY).decode("ascii")[:76]

        def drop_last_part(raw: str) -> str:
            msg = email.message_from_string(raw, policy=drafts.SMTPUTF8_CRLF)
            msg.get_payload().pop()
            return msg.as_string()

        def extra_part(raw: str) -> str:
            msg = email.message_from_string(raw, policy=drafts.SMTPUTF8_CRLF)
            msg.add_attachment(b"x", maintype="application", subtype="pdf", filename="extra.pdf")
            return msg.as_string()

        mutations = {
            "bytes": lambda raw: raw.replace(first_line, first_line[:-4] + ("AAAA" if not first_line.endswith("AAAA") else "BBBB"), 1),
            "filename": lambda raw: raw.replace('filename="report.pdf"', 'filename="other.pdf"'),
            "mime": lambda raw: raw.replace("Content-Type: application/pdf", "Content-Type: application/zip"),
            "disposition": lambda raw: raw.replace("Content-Disposition: attachment; filename=\"report.pdf\"",
                                                   "Content-Disposition: inline; filename=\"report.pdf\""),
            "missing attachment": drop_last_part,
            "extra attachment": extra_part,
            "subject": lambda raw: raw.replace("Subject: Angebot", "Subject: Anderes", 1),
            "to": lambda raw: raw.replace("a@example.org", "evil@example.org", 1),
            "body": lambda raw: raw.replace("Anbei die Unterlagen.", "Anbei die Unterlagen!"),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                self.setUp()
                self.assertUnverified(mutate)

    def test_a_different_message_id_means_not_found(self) -> None:  # V3
        self.assertUnverified(lambda raw: raw.replace("<att1@example.org>", "<other@example.org>"))

    def test_a_lost_append_is_not_reported_as_success(self) -> None:  # V3
        self.assertUnverified(save_stores=False)

    def test_failing_export_is_not_success_and_does_not_leak_stderr(self) -> None:  # V3
        error = self.assertUnverified(export_exit=1, export_stderr=f"broken {SECRET}")
        self.assertNotIn(SECRET, probe(error))

    def test_stored_message_beyond_the_readback_bound_is_not_loaded(self) -> None:  # V3
        pad = "X-Pad: " + "a" * 70 + "\r\n"
        self.assertUnverified(lambda raw: pad * (13 * 1048576 // len(pad)) + raw)

    def test_duplicate_stored_messages_are_ambiguous_not_first_match(self) -> None:  # V3 (duplicates)
        error = self.assertUnverified(duplicate_on_save=True)
        self.assertIn("2", str(error))


if __name__ == "__main__":
    unittest.main()
