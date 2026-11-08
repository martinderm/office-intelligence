"""Raw-EML import (#18) — ``save_raw_draft`` through the real transport seam (offline, synthetic).

Evidence mode ``tdd``.  Matrix rows S1-S9 (one verified APPEND, no retry, journal, binding order),
F1-F8 (zone file boundary) and U1-U6 (mailbox failure modes).

Seam note: ``core.drafts.save_raw_draft`` runs against the real ``core.himalaya.run_himalaya`` /
``fetch_raw_message_eml`` / ``find_envelopes_by_message_id`` code; only the ``subprocess.run`` system
boundary is replaced by ``MailboxBoundary`` (synthetic Himalaya 1.2.0 model including its documented stdin
normalization).  The workspace lease is the REAL shared ``workspace-lock`` guard in a temporary consumer
workspace.  Expected bytes are built with the standard library, independent of the code under test.  This
is an offline model, not a live mailbox proof.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import draft_raw_eml as dre  # noqa: E402
from core import drafts  # noqa: E402
from core import himalaya  # noqa: E402
from draft_attachment_fixtures import make_dir_link, make_file_symlink, remove_dir_link  # noqa: E402
from raw_eml_fixtures import make_eml, plain  # noqa: E402
from test_draft_attachments_save import BINARY, SaveHarness, probe  # noqa: E402

SECRET = "SECRET-RAW-EML-MARKER-0451"
TEN_MIB = 10 * 1048576
EML_PATH = "data/inbound/draft.eml"
MESSAGE_ID = "raw1@example.org"


class RawHarness(SaveHarness):
    def setUp(self) -> None:
        super().setUp()
        self.eml = make_eml(attachments=[("report.pdf", "application/pdf", BINARY)])
        self.put(EML_PATH, self.eml)

    def put(self, rel: str, data: bytes) -> Path:
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    def save_raw(self, path: str = EML_PATH, **overrides):
        params = dict(
            account="demo",
            workspace_root=self.root,
            journal=self.journal,
            lease_id=self.LEASE,
            lock_root=self.guard,
        )
        params.update(overrides)
        with patch.object(himalaya, "resolve_himalaya_invocation", return_value=("himalaya", Path("C:/fake/config.toml"))), \
                patch.object(himalaya.subprocess, "run", self.box):
            return drafts.save_raw_draft(path, **params)

    def attempts(self) -> dict:
        return self.journal_data()["runs"]["run-att"].get("attempts", {})

    def assertRefusedBeforeMailbox(self, reason: str, path: str = EML_PATH, **overrides) -> BaseException:  # noqa: N802
        with self.assertRaises(drafts.DraftError) as raised:
            self.save_raw(path, **overrides)
        self.assertEqual(reason, raised.exception.reason_code, str(raised.exception))
        self.assertNoMailboxContact()
        self.assertEqual({}, self.attempts(), "no journal attempt before validation succeeded")
        text = probe(raised.exception)
        for needle in (SECRET, str(self.root), BINARY[:40].hex()):
            self.assertNotIn(needle, text)
        return raised.exception


class SuccessTests(RawHarness):
    def test_the_prepared_message_is_appended_once_and_verified_semantically(self) -> None:  # S1, S2
        result = self.save_raw()
        self.assertEqual(1, len(self.box.save_calls), "exactly one APPEND")
        tokens = self.box.save_calls[0]
        self.assertEqual(["message", "save"], tokens[tokens.index("message"):tokens.index("message") + 2])
        self.assertEqual("plain", tokens[tokens.index("-o") + 1])
        self.assertEqual("Drafts", tokens[tokens.index("-f") + 1])
        self.assertEqual("demo", tokens[tokens.index("-a") + 1])
        self.assertEqual(self.eml + b"\r\n", self.box.saved_payloads[0], "canonical CRLF message + one transport CRLF")
        self.assertEqual([self.eml.decode("utf-8")], self.box.stored(), "stored equals the prepared message")
        self.assertEqual("42", result["envelope_id"])
        self.assertEqual("Drafts", result["folder"])
        self.assertEqual(MESSAGE_ID, result["message_id"])
        self.assertEqual("Angebot – Prüfung", result["subject"])
        self.assertEqual("Empfänger <a@example.org>", result["to"])
        self.assertEqual("Sat, 07 Nov 2026 12:00:00 +0100", result["date"])
        self.assertEqual(1, result["attachment_count"])
        self.assertEqual(hashlib.sha256(BINARY).hexdigest(), result["attachments"][0]["sha256"])
        self.assertEqual(len(self.eml), result["size_bytes"])
        self.assertEqual("content", result["verified"])
        self.assertEqual(
            {"path": EML_PATH, "line_endings": "crlf", "normalized": False,
             "source_sha256": hashlib.sha256(self.eml).hexdigest(),
             "normalized_sha256": hashlib.sha256(self.eml).hexdigest()},
            result["source"],
        )
        order = [c for c, _ in self.box.calls]
        save_index = next(i for i, c in enumerate(order) if "save" in c)
        self.assertGreater(next(i for i, c in enumerate(order) if "export" in c), save_index, "read-back after the APPEND")

    def test_the_only_writer_is_the_existing_stdin_append(self) -> None:  # S3 (no second writer)
        with patch.object(himalaya, "run_himalaya", wraps=himalaya.run_himalaya) as spy:
            self.save_raw()
        saves = [c for c in spy.call_args_list if c.args and c.args[0][:2] == ["message", "save"]]
        self.assertEqual(1, len(saves))
        kwargs = saves[0].kwargs
        self.assertEqual(self.eml + b"\r\n", kwargs["stdin_bytes"])
        self.assertEqual(1, kwargs["max_retries"])
        self.assertEqual(["-o", "plain"], saves[0].args[0][2:4])
        mutating = [c for c, _ in self.box.calls if any(w in c for w in ("save", "flag", "expunge", "move", "copy", "delete"))]
        self.assertEqual(1, len(mutating), "nothing but the one APPEND mutates the mailbox")

    def test_an_lf_source_is_normalized_and_stored_with_crlf(self) -> None:  # S4
        crlf = self.eml
        lf = crlf.replace(b"\r\n", b"\n")
        self.assertNotEqual(crlf, lf)
        self.put("data/inbound/lf.eml", lf)
        result = self.save_raw("data/inbound/lf.eml")
        self.assertEqual(crlf + b"\r\n", self.box.saved_payloads[0])
        self.assertEqual([crlf.decode("utf-8")], self.box.stored())
        self.assertEqual("lf", result["source"]["line_endings"])
        self.assertTrue(result["source"]["normalized"])
        self.assertEqual(hashlib.sha256(lf).hexdigest(), result["source"]["source_sha256"])
        self.assertEqual(hashlib.sha256(crlf).hexdigest(), result["source"]["normalized_sha256"])

    def test_trailing_blank_lines_survive_the_himalaya_normalization(self) -> None:  # S4
        data = make_eml(body="Text\n\n\n")
        self.put("data/inbound/blank.eml", data)
        self.save_raw("data/inbound/blank.eml")
        self.assertEqual([data.decode("utf-8")], self.box.stored())

    def test_an_explicit_target_folder_is_used(self) -> None:  # S5
        result = self.save_raw(folder="Sent")
        self.assertEqual("Sent", result["folder"])
        self.assertEqual(1, len(self.box.stored("Sent")))

    def test_the_journal_holds_intent_outcome_and_metadata_but_no_content(self) -> None:  # S6
        self.save_raw()
        data = self.journal_data()["runs"]["run-att"]
        attempt = data["attempts"][MESSAGE_ID]
        self.assertEqual("saved_verified", attempt["status"])
        self.assertEqual("save_raw_draft", attempt["action"])
        self.assertEqual("42", attempt["envelope_id"])
        self.assertEqual(1, attempt["attachment_count"])
        self.assertEqual(EML_PATH, attempt["source_path"])
        self.assertEqual(hashlib.sha256(self.eml).hexdigest(), attempt["source_sha256"])
        self.assertEqual(hashlib.sha256(self.eml).hexdigest(), attempt["normalized_sha256"])
        self.assertEqual("save_raw_draft", data["items"]["42"]["action"])
        text = (self.root / "data" / "mail-desk" / drafts.DRAFT_JOURNAL_FILENAME).read_text(encoding="utf-8")
        self.assertNotIn("Anbei", text)
        self.assertNotIn(BINARY[:40].hex(), text)

    def test_the_intent_is_journaled_before_the_append_starts(self) -> None:  # S7
        seen: list[str] = []
        self.box.on_save = lambda: seen.append(self.attempts()[MESSAGE_ID]["status"])
        self.save_raw()
        self.assertEqual(["append_started"], seen)

    def test_a_journal_that_cannot_be_written_stops_before_the_append(self) -> None:  # S7
        with patch.object(self.journal, "record_attempt", side_effect=OSError("disk full")):
            with self.assertRaises(drafts.DraftError) as raised:
                self.save_raw()
        self.assertEqual(drafts.DRAFT_JOURNAL_WRITE_FAILED_REASON, raised.exception.reason_code)
        self.assertEqual([], self.box.save_calls)

    def test_a_text_only_message_without_attachments_is_supported(self) -> None:  # S8
        data = make_eml()
        self.put("data/inbound/text.eml", data)
        result = self.save_raw("data/inbound/text.eml")
        self.assertEqual(0, result["attachment_count"])
        self.assertEqual([data.decode("utf-8")], self.box.stored())


class BindingTests(RawHarness):
    def test_a_missing_lease_stops_before_the_file_is_read(self) -> None:  # S9
        with patch.dict(os.environ):
            os.environ.pop("WORKSPACE_LOCK_LEASE_ID", None)
            # The file does not exist: the lock reason (not "unreadable") proves binding precedes the read.
            self.assertRefusedBeforeMailbox("draft_lock_missing", "data/inbound/missing.eml", lease_id=None)

    def test_a_foreign_lease_is_refused(self) -> None:  # S9
        self.assertRefusedBeforeMailbox("draft_lock_missing", lease_id="somebody-else")

    def test_workspace_root_and_journal_are_mandatory(self) -> None:  # S9
        self.assertRefusedBeforeMailbox("draft_binding_failed", workspace_root=None)
        self.assertRefusedBeforeMailbox("draft_binding_failed", journal=None)

    def test_the_account_must_be_the_workspace_bound_one(self) -> None:  # S9
        self.assertRefusedBeforeMailbox("draft_binding_failed", account="someone-else")

    def test_a_journal_outside_the_workspace_is_refused(self) -> None:  # S9
        elsewhere = drafts.DraftJournal(Path(self._tmp.name) / "other-journal", "run-x")
        with self.assertRaises(drafts.DraftError) as raised:
            self.save_raw(journal=elsewhere)
        self.assertEqual("draft_binding_failed", raised.exception.reason_code)
        self.assertNoMailboxContact()


class FileBoundaryTests(RawHarness):
    def test_only_regular_eml_files_in_the_four_zones_are_accepted(self) -> None:  # F1
        for zone in ("memory/references", "memory/evidence", "memory/cloud", "data"):
            with self.subTest(zone=zone):
                message_id = f"<{zone.replace('/', '-')}@example.org>"
                self.put(f"{zone}/in/z.eml", make_eml(message_id=message_id))
                self.assertEqual("content", self.save_raw(f"{zone}/in/z.eml")["verified"])

    def test_other_extensions_and_odd_names_are_refused(self) -> None:  # F2
        for rel in ("data/inbound/draft.txt", "data/inbound/draft", "data/inbound/draft.eml.pdf", "data/inbound/.eml"):
            with self.subTest(rel):
                self.put(rel, self.eml)
                self.assertRefusedBeforeMailbox("raw_eml_extension_invalid", rel)
        self.put("data/inbound/UPPER.EML", make_eml(message_id="<upper@example.org>"))
        self.assertEqual("content", self.save_raw("data/inbound/UPPER.EML")["verified"])

    def test_paths_outside_the_zones_or_into_runtime_state_are_refused(self) -> None:  # F3
        self.put("notes/x.eml", self.eml)
        self.put("data/mail-desk/x.eml", self.eml)
        self.assertRefusedBeforeMailbox("raw_eml_path_invalid", "notes/x.eml")
        self.assertRefusedBeforeMailbox("raw_eml_path_forbidden", "data/mail-desk/x.eml")
        self.assertRefusedBeforeMailbox("raw_eml_path_forbidden", "data/MAIL-DESK/x.eml")
        for bad in ("../x.eml", "data/../x.eml", "data//x.eml", "/data/x.eml", "data\\x.eml", "C:/x.eml", "data", "", None, 5):
            with self.subTest(bad):
                self.assertRefusedBeforeMailbox("raw_eml_path_invalid", bad)

    def test_a_missing_file_a_directory_and_an_empty_file_are_refused(self) -> None:  # F4
        self.assertRefusedBeforeMailbox("raw_eml_unreadable", "data/inbound/absent.eml")
        (self.root / "data" / "inbound" / "dir.eml").mkdir()
        self.assertRefusedBeforeMailbox("raw_eml_not_regular_file", "data/inbound/dir.eml")
        self.put("data/inbound/empty.eml", b"")
        self.assertRefusedBeforeMailbox("raw_eml_empty", "data/inbound/empty.eml")

    def test_a_directory_link_or_junction_in_the_path_is_refused(self) -> None:  # F5
        outside = Path(self._tmp.name) / "outside"
        outside.mkdir()
        (outside / "x.eml").write_bytes(make_eml(message_id="<outside@example.org>"))
        link = self.root / "data" / "linkdir"
        make_dir_link(link, outside)
        try:
            self.assertRefusedBeforeMailbox("raw_eml_link_refused", "data/linkdir/x.eml")
        finally:
            remove_dir_link(link)

    def test_a_file_symlink_is_refused(self) -> None:  # F5
        outside = Path(self._tmp.name) / "outside2"
        outside.mkdir()
        target = outside / "x.eml"
        target.write_bytes(make_eml(message_id="<outside2@example.org>"))
        if not make_file_symlink(self.root / "data" / "inbound" / "sym.eml", target):
            self.skipTest("the host forbids file symlinks (Windows without privilege)")
        self.assertRefusedBeforeMailbox("raw_eml_link_refused", "data/inbound/sym.eml")

    def test_a_file_over_ten_mib_is_refused_without_reading_it(self) -> None:  # F6
        self.put("data/inbound/big.eml", b"x" * (TEN_MIB + 1))
        reads: list[int] = []
        real_read = os.read
        with patch.object(os, "read", lambda fd, n: (reads.append(n), real_read(fd, n))[1]):
            self.assertRefusedBeforeMailbox("raw_eml_too_large", "data/inbound/big.eml")
        self.assertEqual([], reads, "rejected from fstat before reading")

    def test_a_file_growing_while_it_is_read_is_refused_and_the_read_is_bounded(self) -> None:  # F7
        path = self.put("data/inbound/grow.eml", self.eml)
        real_read = os.read
        state = {"grown": False, "total": 0}

        def growing_read(fd, n):
            data = real_read(fd, n)
            state["total"] += len(data)
            if not state["grown"]:
                state["grown"] = True
                with open(path, "ab") as handle:
                    handle.write(b"X-Late: 1\r\n")
            return data

        with patch.object(os, "read", growing_read):
            self.assertRefusedBeforeMailbox("raw_eml_changed", "data/inbound/grow.eml")
        self.assertLessEqual(state["total"], len(self.eml) + 1)

    def test_an_invalid_message_is_refused_before_any_mailbox_contact_or_journal_entry(self) -> None:  # F8
        self.put("data/inbound/dup.eml", plain(extra_headers=["Message-ID: <other@example.org>"]))
        self.assertRefusedBeforeMailbox("raw_eml_header_duplicate", "data/inbound/dup.eml")
        self.put("data/inbound/nomid.eml", plain(message_id=None, body=[SECRET]))
        self.assertRefusedBeforeMailbox("raw_eml_header_missing", "data/inbound/nomid.eml")
        self.put("data/inbound/lat.eml", b"\xff" + self.eml)
        self.assertRefusedBeforeMailbox("raw_eml_not_utf8", "data/inbound/lat.eml")


class MailboxFailureTests(RawHarness):
    def test_a_message_id_already_present_in_the_folder_fails_closed(self) -> None:  # U1
        self.box.seed("Drafts", self.eml.decode("utf-8"))
        with self.assertRaises(drafts.DraftError) as raised:
            self.save_raw()
        self.assertEqual(drafts.DRAFT_MESSAGE_ID_NOT_UNIQUE_REASON, raised.exception.reason_code)
        self.assertEqual([], self.box.save_calls)
        self.assertEqual({}, self.attempts())

    def test_a_folder_too_large_to_prove_absence_fails_closed(self) -> None:  # U1
        for index in range(himalaya.UNIQUE_LOOKUP_MAX_HEADER_READS):
            self.box.seed("Drafts", make_eml(message_id=f"<f{index}@example.org>").decode("utf-8"))
        with self.assertRaises(drafts.DraftError) as raised:
            self.save_raw()
        self.assertEqual(drafts.DRAFT_MESSAGE_ID_NOT_UNIQUE_REASON, raised.exception.reason_code)
        self.assertEqual([], self.box.save_calls)

    def test_a_failed_append_is_reported_not_retried_and_journaled(self) -> None:  # U2
        self.box.save_exit = 1
        self.box.save_stderr = "authentication failed"
        with self.assertRaises(drafts.DraftError) as raised:
            self.save_raw()
        self.assertEqual(drafts.DRAFT_SAVE_FAILED_REASON, raised.exception.reason_code)
        self.assertEqual(1, len(self.box.save_calls))
        self.assertEqual("append_failed", self.attempts()[MESSAGE_ID]["status"])
        self.assertNotIn("authentication", str(raised.exception))

    def test_a_transient_looking_failure_is_still_not_retried(self) -> None:  # U2
        self.box.save_exit = 1
        self.box.save_stderr = "connection reset by peer"
        with patch.object(himalaya.time, "sleep") as sleep:
            with self.assertRaises(drafts.DraftError):
                self.save_raw()
        self.assertEqual(1, len(self.box.save_calls))
        sleep.assert_not_called()

    def test_a_timeout_leaves_an_unknown_outcome_that_is_journaled_and_never_retried(self) -> None:  # U3
        self.box.save_raises = subprocess.TimeoutExpired(cmd="himalaya", timeout=35)
        with self.assertRaises(drafts.DraftError) as raised:
            self.save_raw()
        self.assertEqual(drafts.DRAFT_APPEND_OUTCOME_UNKNOWN_REASON, raised.exception.reason_code)
        self.assertIn(MESSAGE_ID, str(raised.exception))
        self.assertEqual(1, len(self.box.save_calls))
        self.assertEqual("append_outcome_unknown", self.attempts()[MESSAGE_ID]["status"])
        self.assertEqual({}, self.journal_data()["runs"]["run-att"]["items"], "no success item")

    def test_a_launch_failure_is_an_unknown_outcome(self) -> None:  # U3
        self.box.save_raises = OSError("pipe broke")
        with self.assertRaises(drafts.DraftError) as raised:
            self.save_raw()
        self.assertEqual(drafts.DRAFT_APPEND_OUTCOME_UNKNOWN_REASON, raised.exception.reason_code)
        self.assertEqual(1, len(self.box.save_calls))

    def test_a_lost_append_is_never_reported_as_success(self) -> None:  # U4
        self.box.save_stores = False
        with self.assertRaises(drafts.DraftError) as raised:
            self.save_raw()
        self.assertEqual(drafts.DRAFT_VERIFICATION_FAILED_REASON, raised.exception.reason_code)
        self.assertEqual(1, len(self.box.save_calls))
        self.assertEqual("appended_unverified", self.attempts()[MESSAGE_ID]["status"])

    def test_a_stored_copy_that_differs_semantically_is_never_reported_as_success(self) -> None:  # U5
        mutations = {
            "body": lambda raw: raw.replace("Anbei die Unterlagen.", "Anbei die Unterlagen!"),
            "subject": lambda raw: raw.replace("Subject: Angebot", "Subject: Anderes"),
            "extra header": lambda raw: raw.replace("Date:", "X-Server: added\r\nDate:", 1),
            "attachment bytes": lambda raw: raw.replace("AAEC", "AAED", 1),
            "attachment dropped": lambda raw: raw[: raw.index("--===============")] + "--===============",
        }
        for label, mutate in mutations.items():
            with self.subTest(label):
                self.setUp()  # fresh workspace, journal and mailbox for every case
                ascii_body = make_eml(body="Anbei die Unterlagen.\n", attachments=[("report.pdf", "application/pdf", BINARY)])
                self.put(EML_PATH, ascii_body)
                original = ascii_body.decode("utf-8")
                self.assertTrue(original != mutate(original), "the mutation must change the message")
                self.box.mutate_stored = mutate
                with self.assertRaises(drafts.DraftError) as raised:
                    self.save_raw()
                self.assertEqual(drafts.DRAFT_VERIFICATION_FAILED_REASON, raised.exception.reason_code)
                self.assertEqual(1, len(self.box.save_calls))
                self.assertEqual("appended_unverified", self.attempts()[MESSAGE_ID]["status"])

    def test_a_stored_copy_with_changed_trailing_newlines_is_never_reported_as_success(self) -> None:  # U5 (finding 3)
        data = make_eml(body="Text\n", message_id="<tail@example.org>")
        self.put("data/inbound/tail.eml", data)
        self.box.mutate_stored = lambda raw: raw + "\r\n\r\n"
        with self.assertRaises(drafts.DraftError) as raised:
            self.save_raw("data/inbound/tail.eml")
        self.assertEqual(drafts.DRAFT_VERIFICATION_FAILED_REASON, raised.exception.reason_code)
        self.assertIn("body:text", str(raised.exception))
        self.assertEqual(1, len(self.box.save_calls))
        self.assertEqual("appended_unverified", self.attempts()["tail@example.org"]["status"])

    def test_two_stored_copies_are_never_reported_as_success(self) -> None:  # U6
        self.box.duplicate_on_save = True
        with self.assertRaises(drafts.DraftError) as raised:
            self.save_raw()
        self.assertEqual(drafts.DRAFT_VERIFICATION_FAILED_REASON, raised.exception.reason_code)
        self.assertEqual(1, len(self.box.save_calls))

    def test_failure_texts_never_contain_message_content(self) -> None:  # U6
        self.put("data/inbound/secret.eml", make_eml(body=SECRET + "\n", message_id="<secret@example.org>"))
        self.box.mutate_stored = lambda raw: raw.replace(SECRET, "changed")
        with self.assertRaises(drafts.DraftError) as raised:
            self.save_raw("data/inbound/secret.eml")
        self.assertNotIn(SECRET, probe(raised.exception))
        text = (self.root / "data" / "mail-desk" / drafts.DRAFT_JOURNAL_FILENAME).read_text(encoding="utf-8")
        self.assertNotIn(SECRET, text)


if __name__ == "__main__":
    unittest.main()
