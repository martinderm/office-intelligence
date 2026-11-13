"""Issue #88 consumer adapter: pending, record and crash-check (offline, synthetic).

Covers the reviewed bundle gaps: read-only pending view without raw mail content,
outcome/provenance-aware consumption counters, missing vs. empty handoff, the
account/mailbox gate, lease + workspace binding before any append, fresh anchor
consistency, the open-ancestor gate shared by ``record``/``recover-promotion``/
``crash-check``, strictly typed evidence thread metadata, idempotent consumption,
conflicting identity, exact-triad crash classification and strict read-only
behavior.  Lease verification is injected (mocked) in the positive paths; an
actual workspace lease is **not** proven here.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))
sys.path.insert(0, str(TESTS_ROOT))

import mail_desk_sent_ledger as cli  # noqa: E402
from core import sent_ledger  # noqa: E402
from core.sent_handoff import HANDOFF_FILENAME, LEDGER_FILENAME  # noqa: E402
from sent_ledger_adapter_fixtures import (  # noqa: E402
    ACCOUNT_A,
    ACCOUNT_B,
    handoff_item,
    ledger_line,
    make_workspace,
    read_ledger_lines,
    snapshot,
    write_backend_config,
    write_evaluations,
    write_evidence,
    write_handoff,
    write_ledger,
)


class AdapterTestBase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.workspace = self.root / "synth-workspace"
        self.data_dir = self.workspace / "data" / "mail-desk"
        self.data_dir.mkdir(parents=True)

    def run_cli(self, *args: str) -> tuple[int, dict]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(list(args))
        return code, json.loads(out.getvalue())

    def pending(self, *extra: str) -> tuple[int, dict]:
        return self.run_cli(
            "pending", "--workspace", str(self.workspace),
            "--data-dir", "data/mail-desk", *extra,
        )

    def record(self, *extra: str, message_id: str | None = None,
               item_ref: str | None = None) -> tuple[int, dict]:
        selector: list[str] = []
        if message_id is not None:
            selector = ["--message-id", message_id]
        elif item_ref is not None:
            selector = ["--item-ref", item_ref]
        return self.run_cli(
            "record", "--workspace", str(self.workspace),
            "--data-dir", "data/mail-desk", *selector,
            "--workspace-lease-id", "LEASE-1", *extra,
        )

    def crash_check(self, message_id: str, vorgang_ref: str, evid_id: str,
                    *extra: str) -> tuple[int, dict]:
        return self.run_cli(
            "crash-check", "--workspace", str(self.workspace),
            "--data-dir", "data/mail-desk",
            "--message-id", message_id, "--vorgang-ref", vorgang_ref,
            "--evid-id", evid_id, *extra,
        )

    def assert_reason(self, envelope: dict, reason_code: str) -> None:
        self.assertFalse(envelope["success"])
        self.assertEqual(reason_code, envelope["data"].get("reason_code"))


class PendingViewTests(AdapterTestBase):
    def test_missing_handoff_is_an_empty_read_only_view(self) -> None:
        before = snapshot(self.root)
        code, envelope = self.pending()
        self.assertEqual(0, code)
        self.assertTrue(envelope["success"])
        data = envelope["data"]
        self.assertEqual("empty", data["status"])
        self.assertEqual("none", data["handoff_state"])
        self.assertFalse(data["handoff_present"])
        self.assertEqual(0, data["handoff_item_count"])
        self.assertEqual(0, data["pending_count"])
        self.assertEqual([], data["items"])
        self.assertEqual("sent_ledger_pending", data["mode"])
        self.assertTrue(data["read_only"])
        self.assertEqual(1, data["view_version"])
        self.assertIn("handoff file is missing", data["next"])
        self.assertEqual(before, snapshot(self.root))

    def test_empty_handoff_file_differs_from_missing(self) -> None:
        write_handoff(self.data_dir, [])
        code, envelope = self.pending()
        self.assertEqual(0, code)
        data = envelope["data"]
        self.assertEqual("none", data["handoff_state"])
        self.assertTrue(data["handoff_present"])
        self.assertEqual(0, data["handoff_item_count"])
        self.assertEqual(0, data["pending_count"])
        self.assertIn("present and known empty", data["next"])

    def test_pending_view_has_no_raw_mail_content_or_identifiers(self) -> None:
        items = [
            handoff_item("first@example.org", subject="Very distinctive subject token"),
            handoff_item("second@example.org", subject="Another distinctive subject token"),
        ]
        write_handoff(self.data_dir, items)
        before = snapshot(self.root)
        code, envelope = self.pending()
        self.assertEqual(0, code)
        data = envelope["data"]
        self.assertEqual(2, data["pending_count"])
        self.assertEqual(2, data["handoff_item_count"])
        self.assertEqual(0, data["consumed_ignored"])
        self.assertEqual(2, len(data["items"]))
        self.assertEqual(0, data["counts"]["with_anchor"])
        serialized = json.dumps(envelope, ensure_ascii=False)
        for raw in (
            "first@example.org", "second@example.org",
            "Very distinctive subject token", "Another distinctive subject token",
            "sender@example.org", "recipient@example.org", "9001",
            "Mon, 02 Nov 2026 08:00:00 +0100",
        ):
            self.assertNotIn(raw, serialized)
        for item in data["items"]:
            self.assertRegex(item["item_ref"], r"^sha256:[0-9a-f]{16}$")
            self.assertIsNone(item["thread_ref"])
        self.assertEqual(before, snapshot(self.root))

    def test_pending_view_is_stable_across_runs(self) -> None:
        write_handoff(self.data_dir, [handoff_item("stable@example.org")])
        first_code, first = self.pending()
        second_code, second = self.pending()
        self.assertEqual((0, 0), (first_code, second_code))
        self.assertEqual(first["data"], second["data"])

    def test_no_evidence_consumes_without_becoming_an_anchor(self) -> None:
        write_handoff(self.data_dir, [
            handoff_item("root@example.org"),
            handoff_item("reply@example.org", in_reply_to="root@example.org",
                         references=["root@example.org"]),
        ])
        write_ledger(self.data_dir, [
            ledger_line("root@example.org", outcome="no_evidence",
                        reason_code="no_relevant_evidence"),
        ])
        code, envelope = self.pending()
        self.assertEqual(0, code)
        data = envelope["data"]
        self.assertEqual(1, data["pending_count"])
        self.assertEqual(1, data["consumed_ignored"])
        self.assertEqual({"synthesized": 0, "no_evidence": 1}, data["consumed_outcomes"])
        self.assertEqual({"synthesis": 1, "legacy_import": 0}, data["consumed_sources"])
        self.assertEqual({"synthesized": 0, "no_evidence": 1}, data["ledger"]["outcomes"])
        self.assertFalse(data["items"][0]["anchored"])
        self.assertIsNone(data["items"][0]["anchor"])

    def test_legacy_import_synthesized_consumes_and_anchors(self) -> None:
        write_handoff(self.data_dir, [
            handoff_item("root@example.org"),
            handoff_item("reply@example.org", in_reply_to="root@example.org"),
        ])
        write_ledger(self.data_dir, [
            ledger_line("root@example.org", source="legacy_import",
                        vorgang_ref="V-9", evid_id="E-9"),
        ])
        code, envelope = self.pending()
        self.assertEqual(0, code)
        data = envelope["data"]
        self.assertEqual(1, data["pending_count"])
        self.assertEqual({"synthesized": 1, "no_evidence": 0}, data["consumed_outcomes"])
        self.assertEqual({"synthesis": 0, "legacy_import": 1}, data["consumed_sources"])
        self.assertTrue(data["items"][0]["anchored"])
        self.assertEqual("V-9", data["items"][0]["anchor"]["vorgang_ref"])
        self.assertEqual("in_reply_to", data["items"][0]["anchor"]["match_type"])

    def test_legacy_import_no_evidence_consumes_without_anchor(self) -> None:
        write_handoff(self.data_dir, [
            handoff_item("root@example.org"),
            handoff_item("reply@example.org", in_reply_to="root@example.org"),
        ])
        write_ledger(self.data_dir, [
            ledger_line("root@example.org", outcome="no_evidence",
                        source="legacy_import", reason_code="legacy_status_no_evidence"),
        ])
        code, envelope = self.pending()
        self.assertEqual(0, code)
        data = envelope["data"]
        self.assertEqual(1, data["pending_count"])
        self.assertEqual({"synthesized": 0, "no_evidence": 1}, data["consumed_outcomes"])
        self.assertEqual({"synthesis": 0, "legacy_import": 1}, data["consumed_sources"])
        self.assertFalse(data["items"][0]["anchored"])

    def test_ancestor_is_listed_first_and_blocks_its_reply(self) -> None:
        write_handoff(self.data_dir, [
            handoff_item("reply@example.org", in_reply_to="root@example.org"),
            handoff_item("root@example.org"),
        ])
        code, envelope = self.pending()
        self.assertEqual(0, code)
        items = envelope["data"]["items"]
        self.assertEqual(2, len(items))
        self.assertEqual(0, items[0]["waits_for_count"])
        self.assertEqual(1, items[1]["waits_for_count"])
        self.assertEqual([items[0]["item_ref"]], items[1]["waits_for"])
        self.assertEqual(1, envelope["data"]["counts"]["with_pending_ancestors"])

    def test_counts_headers_truncation_and_duplicate_ledger_ids(self) -> None:
        write_handoff(self.data_dir, [
            handoff_item("a@example.org", headers_complete=False,
                         truncated_fields=["subject"]),
        ])
        write_ledger(self.data_dir, [
            ledger_line("consumed@example.org"),
            ledger_line("consumed@example.org"),
        ])
        code, envelope = self.pending()
        self.assertEqual(0, code)
        data = envelope["data"]
        self.assertEqual(1, data["counts"]["headers_incomplete"])
        self.assertEqual(1, data["counts"]["truncated"])
        self.assertEqual(1, data["ledger"]["duplicate_message_ids"])
        self.assertEqual(2, data["ledger"]["lines"])

    def test_corrupt_handoff_fails_closed_without_writing(self) -> None:
        (self.data_dir / HANDOFF_FILENAME).write_text("{ not json", encoding="utf-8")
        before = snapshot(self.root)
        code, envelope = self.pending()
        self.assertEqual(2, code)
        self.assert_reason(envelope, "handoff_invalid")
        self.assertEqual(before, snapshot(self.root))

    def test_corrupt_ledger_fails_closed_without_writing(self) -> None:
        write_handoff(self.data_dir, [handoff_item("a@example.org")])
        (self.data_dir / LEDGER_FILENAME).write_text(
            json.dumps({"schema_version": 1, "message_id": "a@example.org"}) + "\n",
            encoding="utf-8",
        )
        before = snapshot(self.root)
        code, envelope = self.pending()
        self.assertEqual(2, code)
        self.assert_reason(envelope, "ledger_invalid")
        self.assertEqual(before, snapshot(self.root))

    def test_foreign_data_dir_is_rejected_read_only(self) -> None:
        other, other_dd = make_workspace(self.root / "other-workspace")
        write_handoff(other_dd, [handoff_item("other@example.org")])
        before = snapshot(other)
        code, envelope = self.run_cli(
            "pending", "--workspace", str(self.workspace),
            "--data-dir", str(other_dd),
        )
        self.assertEqual(2, code)
        self.assert_reason(envelope, "workspace_binding_invalid")
        self.assertEqual(before, snapshot(other))

    def test_pending_does_not_require_a_lease(self) -> None:
        write_handoff(self.data_dir, [handoff_item("a@example.org")])
        with patch.object(sent_ledger, "verify_workspace_lock",
                          side_effect=AssertionError("read-only must not verify a lease")):
            code, envelope = self.pending()
        self.assertEqual(0, code)
        self.assertTrue(envelope["success"])

    def test_account_gate_stops_the_whole_round_without_raw_ids(self) -> None:
        write_backend_config(self.workspace, account=ACCOUNT_A)
        write_handoff(self.data_dir, [
            handoff_item("bound@example.org", mailbox=ACCOUNT_A),
            handoff_item("foreign@example.org", mailbox=ACCOUNT_B),
        ])
        before = snapshot(self.root)
        code, envelope = self.pending("--account", ACCOUNT_A)
        self.assertEqual(2, code)
        self.assert_reason(envelope, "mailbox_mismatch")
        data = envelope["data"]
        self.assertEqual(1, data["mismatch_count"])
        self.assertEqual(ACCOUNT_A, data["verified_account"])
        self.assertEqual(1, len(data["mismatch_refs"]))
        serialized = json.dumps(envelope, ensure_ascii=False)
        self.assertNotIn("foreign@example.org", serialized)
        self.assertNotIn("bound@example.org", serialized)
        self.assertEqual(before, snapshot(self.root))

    def test_requested_account_mismatch_is_fail_closed(self) -> None:
        write_backend_config(self.workspace, account=ACCOUNT_A)
        write_handoff(self.data_dir, [handoff_item("a@example.org", mailbox=ACCOUNT_A)])
        code, envelope = self.pending("--account", ACCOUNT_B)
        self.assertEqual(2, code)
        self.assert_reason(envelope, "account_mismatch")

    def test_missing_backend_config_with_account_fails_closed(self) -> None:
        write_handoff(self.data_dir, [handoff_item("a@example.org")])
        code, envelope = self.pending("--account", ACCOUNT_A)
        self.assertEqual(2, code)
        self.assert_reason(envelope, "workspace_config_invalid")

    def test_workspace_without_bound_account_is_rejected(self) -> None:
        write_backend_config(self.workspace, account=None)
        write_handoff(self.data_dir, [handoff_item("a@example.org")])
        code, envelope = self.pending("--account", ACCOUNT_A)
        self.assertEqual(2, code)
        # An explicit request against the configured null default is an account
        # mismatch; either way the round is fail-closed before any read/append.
        self.assert_reason(envelope, "account_mismatch")

    def test_account_gate_accepts_fully_bound_items(self) -> None:
        write_backend_config(self.workspace, account=ACCOUNT_A)
        write_handoff(self.data_dir, [handoff_item("a@example.org", mailbox=ACCOUNT_A)])
        code, envelope = self.pending("--account", ACCOUNT_A)
        self.assertEqual(0, code)
        self.assertEqual(ACCOUNT_A, envelope["data"]["verified_account"])


class RecordTests(AdapterTestBase):
    def write_single_item(self, mid: str = "a@example.org", **overrides) -> None:
        write_handoff(self.data_dir, [handoff_item(mid, **overrides)])

    def patch_lease(self):
        return patch.object(sent_ledger, "verify_workspace_lock", return_value=object())

    def test_synthesized_appends_exactly_one_validated_line(self) -> None:
        self.write_single_item()
        handoff_before = (self.data_dir / HANDOFF_FILENAME).read_bytes()
        with self.patch_lease():
            code, envelope = self.record(
                "--outcome", "synthesized", "--vorgang-ref", "V-7",
                "--evid-id", "EVID-2026-0042", message_id="<a@example.org>",
            )
        self.assertEqual(0, code)
        data = envelope["data"]
        self.assertEqual(("appended", 1), (data["status"], data["appended"]))
        self.assertEqual("synthesized", data["outcome"])
        self.assertEqual("a@example.org", data["norm_message_id"])
        self.assertEqual(1, data["pending_before"])
        self.assertEqual(0, data["pending_after"])
        self.assertTrue(data["handoff_unchanged"])
        self.assertEqual(handoff_before, (self.data_dir / HANDOFF_FILENAME).read_bytes())
        lines = read_ledger_lines(self.data_dir)
        self.assertEqual(1, len(lines))
        line = lines[0]
        self.assertEqual(("a@example.org", "synthesized", "synthesis", "V-7",
                          "EVID-2026-0042", "primary"), (
            line["message_id"], line["outcome"], line["source"],
            line["vorgang_ref"], line["evid_id"], line["role"],
        ))
        self.assertNotIn("Synthetic subject", json.dumps(envelope, ensure_ascii=False))

    def test_matching_consumed_id_is_an_idempotent_no_op(self) -> None:
        self.write_single_item()
        with self.patch_lease():
            first_code, _ = self.record(
                "--outcome", "synthesized", "--vorgang-ref", "V-7",
                "--evid-id", "E-7", message_id="a@example.org",
            )
            ledger_before = (self.data_dir / LEDGER_FILENAME).read_bytes()
            code, envelope = self.record(
                "--outcome", "synthesized", "--vorgang-ref", "V-7",
                "--evid-id", "E-7", message_id="a@example.org",
            )
        self.assertEqual((0, 0), (first_code, code))
        data = envelope["data"]
        self.assertEqual(("already_consumed", 0), (data["status"], data["appended"]))
        self.assertEqual(ledger_before, (self.data_dir / LEDGER_FILENAME).read_bytes())

    def test_repeated_no_evidence_is_an_idempotent_no_op(self) -> None:
        self.write_single_item()
        with self.patch_lease():
            self.record("--outcome", "no_evidence",
                        "--reason-code", "no_relevant_evidence", message_id="a@example.org")
            before = (self.data_dir / LEDGER_FILENAME).read_bytes()
            code, envelope = self.record(
                "--outcome", "no_evidence", "--reason-code", "no_relevant_evidence",
                message_id="a@example.org",
            )
        self.assertEqual(0, code)
        self.assertEqual("already_consumed", envelope["data"]["status"])
        self.assertEqual(before, (self.data_dir / LEDGER_FILENAME).read_bytes())

    def test_differing_identity_is_a_conflict_and_writes_nothing(self) -> None:
        self.write_single_item()
        with self.patch_lease():
            self.record("--outcome", "synthesized", "--vorgang-ref", "V-1",
                        "--evid-id", "E-1", message_id="a@example.org")
            before = (self.data_dir / LEDGER_FILENAME).read_bytes()
            code, envelope = self.record(
                "--outcome", "no_evidence", "--reason-code", "no_relevant_evidence",
                message_id="a@example.org",
            )
        self.assertEqual(2, code)
        self.assert_reason(envelope, "ledger_conflict")
        self.assertEqual(before, (self.data_dir / LEDGER_FILENAME).read_bytes())

    def test_no_evidence_requires_a_reason_and_forbids_anchors(self) -> None:
        self.write_single_item()
        with self.patch_lease():
            code, envelope = self.record(
                "--outcome", "no_evidence", message_id="a@example.org",
            )
            self.assertEqual((2, "missing_reason_code"),
                             (code, envelope["data"]["reason_code"]))
            code, envelope = self.record(
                "--outcome", "no_evidence", "--reason-code", "no_relevant_evidence",
                "--vorgang-ref", "V-1", message_id="a@example.org",
            )
            self.assertEqual((2, "no_evidence_with_anchor"),
                             (code, envelope["data"]["reason_code"]))
            code, envelope = self.record(
                "--outcome", "no_evidence", "--reason-code", "Not A Token!",
                message_id="a@example.org",
            )
            self.assertEqual((2, "reason_code_invalid"),
                             (code, envelope["data"]["reason_code"]))
        self.assertEqual([], read_ledger_lines(self.data_dir))

    def test_synthesized_requires_vorgang_and_rejects_reason(self) -> None:
        self.write_single_item()
        with self.patch_lease():
            code, envelope = self.record(
                "--outcome", "synthesized", message_id="a@example.org",
            )
            self.assertEqual((2, "missing_vorgang_ref"),
                             (code, envelope["data"]["reason_code"]))
            code, envelope = self.record(
                "--outcome", "synthesized", "--vorgang-ref", "V-1",
                "--reason-code", "no_relevant_evidence", message_id="a@example.org",
            )
            self.assertEqual((2, "reason_code_not_allowed"),
                             (code, envelope["data"]["reason_code"]))
        self.assertEqual([], read_ledger_lines(self.data_dir))

    def test_no_evidence_body_and_outcome_are_recorded_without_anchor(self) -> None:
        self.write_single_item()
        with self.patch_lease():
            code, envelope = self.record(
                "--outcome", "no_evidence", "--reason-code", "headers_incomplete",
                message_id="a@example.org",
            )
        self.assertEqual(0, code)
        line = read_ledger_lines(self.data_dir)[0]
        self.assertEqual("no_evidence", line["outcome"])
        self.assertEqual("headers_incomplete", line["reason_code"])
        self.assertIsNone(line["vorgang_ref"])
        self.assertNotIn("evid_id", line)
        self.assertIsNone(line["anchored_to"])

    def test_foreign_evid_owner_stops_before_append(self) -> None:
        self.write_single_item()
        write_ledger(self.data_dir, [
            ledger_line("other@example.org", vorgang_ref="V-1", evid_id="E-1"),
        ])
        with self.patch_lease():
            code, envelope = self.record(
                "--outcome", "synthesized", "--vorgang-ref", "V-1",
                "--evid-id", "E-1", message_id="a@example.org",
            )
        self.assertEqual(2, code)
        self.assert_reason(envelope, "evid_shared_conflict")
        self.assertEqual(1, len(read_ledger_lines(self.data_dir)))

    def test_anchor_conflict_stops_before_append(self) -> None:
        write_handoff(self.data_dir, [
            handoff_item("root@example.org"),
            handoff_item("reply@example.org", in_reply_to="root@example.org"),
        ])
        write_ledger(self.data_dir, [
            ledger_line("root@example.org", vorgang_ref="V-1", evid_id=None),
        ])
        with self.patch_lease():
            code, envelope = self.record(
                "--outcome", "synthesized", "--vorgang-ref", "V-2",
                "--evid-id", "E-2", message_id="reply@example.org",
            )
        self.assertEqual(2, code)
        self.assert_reason(envelope, "anchor_conflict")
        self.assertEqual(1, len(read_ledger_lines(self.data_dir)))

    def test_reference_less_anchor_is_not_derived_into_a_vorgang(self) -> None:
        write_handoff(self.data_dir, [
            handoff_item("root@example.org"),
            handoff_item("reply@example.org", in_reply_to="root@example.org"),
        ])
        write_ledger(self.data_dir, [
            ledger_line("root@example.org", vorgang_ref=None, evid_id=None),
        ])
        with self.patch_lease():
            code, envelope = self.record(
                "--outcome", "synthesized", "--vorgang-ref", "V-9",
                message_id="reply@example.org",
            )
        self.assertEqual(2, code)
        self.assert_reason(envelope, "reference_anchor_unresolved")
        self.assertEqual(1, len(read_ledger_lines(self.data_dir)))

    def test_reply_becomes_a_follow_up_of_the_fresh_anchor(self) -> None:
        write_handoff(self.data_dir, [
            handoff_item("root@example.org"),
            handoff_item("reply@example.org", in_reply_to="root@example.org"),
        ])
        write_ledger(self.data_dir, [
            ledger_line("root@example.org", vorgang_ref="V-1", evid_id=None),
        ])
        with self.patch_lease():
            code, envelope = self.record(
                "--outcome", "synthesized", "--vorgang-ref", "V-1",
                "--evid-id", "E-2", message_id="reply@example.org",
            )
        self.assertEqual(0, code)
        line = read_ledger_lines(self.data_dir)[-1]
        self.assertEqual(("follow_up", "root@example.org", "V-1"),
                         (line["role"], line["anchored_to"], line["vorgang_ref"]))

    def test_pending_ancestor_must_be_consumed_first(self) -> None:
        write_handoff(self.data_dir, [
            handoff_item("root@example.org"),
            handoff_item("reply@example.org", in_reply_to="root@example.org"),
        ])
        with self.patch_lease():
            code, envelope = self.record(
                "--outcome", "synthesized", "--vorgang-ref", "V-1",
                message_id="reply@example.org",
            )
        self.assertEqual(2, code)
        self.assert_reason(envelope, "ancestor_pending")
        self.assertEqual([], read_ledger_lines(self.data_dir))

    def test_unknown_or_non_pending_id_is_refused(self) -> None:
        with self.patch_lease():
            code, envelope = self.record(
                "--outcome", "synthesized", "--vorgang-ref", "V-1",
                message_id="unknown@example.org",
            )
        self.assertEqual(2, code)
        self.assert_reason(envelope, "not_pending")
        self.assertEqual([], read_ledger_lines(self.data_dir))

    def test_item_ref_selector_keeps_the_raw_id_out_of_the_report(self) -> None:
        self.write_single_item("secret@example.org")
        _, pending = self.pending()
        item_ref = pending["data"]["items"][0]["item_ref"]
        with self.patch_lease():
            code, envelope = self.record(
                "--outcome", "no_evidence", "--reason-code", "no_relevant_evidence",
                item_ref=item_ref,
            )
        self.assertEqual(0, code)
        self.assertIsNone(envelope["data"]["norm_message_id"])
        self.assertNotIn("secret@example.org", json.dumps(envelope, ensure_ascii=False))
        self.assertEqual(1, len(read_ledger_lines(self.data_dir)))

    def test_missing_lease_writes_nothing(self) -> None:
        self.write_single_item()
        with patch.object(sent_ledger, "verify_workspace_lock",
                          side_effect=RuntimeError("no lease")):
            code, envelope = self.record(
                "--outcome", "synthesized", "--vorgang-ref", "V-1",
                message_id="a@example.org",
            )
        self.assertEqual(2, code)
        self.assert_reason(envelope, "workspace_lease_required")
        self.assertEqual([], read_ledger_lines(self.data_dir))

    def test_foreign_data_dir_is_rejected_before_the_verifier(self) -> None:
        self.write_single_item()
        other, other_dd = make_workspace(self.root / "other-workspace")
        write_handoff(other_dd, [handoff_item("other@example.org")])
        before = snapshot(other)
        with patch.object(sent_ledger, "verify_workspace_lock") as verify:
            code, envelope = self.run_cli(
                "record", "--workspace", str(self.workspace),
                "--data-dir", str(other_dd), "--message-id", "other@example.org",
                "--outcome", "synthesized", "--vorgang-ref", "V-1",
                "--workspace-lease-id", "LEASE-1",
            )
        self.assertEqual(2, code)
        self.assert_reason(envelope, "workspace_binding_invalid")
        verify.assert_not_called()
        self.assertEqual(before, snapshot(other))

    def test_account_gate_blocks_record_without_writing(self) -> None:
        write_backend_config(self.workspace, account=ACCOUNT_A)
        self.write_single_item(mailbox=ACCOUNT_B)
        with self.patch_lease():
            code, envelope = self.record(
                "--outcome", "synthesized", "--vorgang-ref", "V-1",
                "--account", ACCOUNT_A, message_id="a@example.org",
            )
        self.assertEqual(2, code)
        self.assert_reason(envelope, "mailbox_mismatch")
        self.assertEqual([], read_ledger_lines(self.data_dir))

    def test_corrupt_handoff_blocks_record(self) -> None:
        (self.data_dir / HANDOFF_FILENAME).write_text("{ broken", encoding="utf-8")
        with self.patch_lease():
            code, envelope = self.record(
                "--outcome", "synthesized", "--vorgang-ref", "V-1",
                message_id="a@example.org",
            )
        self.assertEqual(2, code)
        self.assert_reason(envelope, "handoff_invalid")
        self.assertEqual([], read_ledger_lines(self.data_dir))


class ConsumeTests(AdapterTestBase):
    """Batch consumer: only already evaluated items are appended; all else stays open."""

    def evaluations(self, entries: list[dict], name: str = "evaluations.json") -> Path:
        return write_evaluations(self.root / name, entries)

    def consume(self, path: Path, *extra: str, lease: bool = True) -> tuple[int, dict]:
        lease_args = ["--workspace-lease-id", "LEASE-1"] if lease else []
        return self.run_cli(
            "consume", "--workspace", str(self.workspace),
            "--data-dir", "data/mail-desk", "--evaluations", str(path), *lease_args,
            *extra,
        )

    def patch_lease(self):
        return patch.object(sent_ledger, "verify_workspace_lock", return_value=object())

    def test_only_evaluated_items_are_consumed_and_the_rest_stays_open(self) -> None:
        write_handoff(self.data_dir, [
            handoff_item("a@example.org"),
            handoff_item("b@example.org"),
            handoff_item("c@example.org"),
        ])
        path = self.evaluations([
            {"message_id": "a@example.org", "outcome": "synthesized",
             "vorgang_ref": "V-7", "evid_id": "E-7"},
            {"message_id": "b@example.org", "outcome": "no_evidence",
             "reason_code": "legacy_status_no_evidence"},
        ])
        handoff_before = (self.data_dir / HANDOFF_FILENAME).read_bytes()
        with self.patch_lease():
            code, envelope = self.consume(path)
        self.assertEqual(0, code)
        data = envelope["data"]
        self.assertEqual(("partial", 3, 1), (
            data["status"], data["pending_before"], data["pending_after"],
        ))
        self.assertEqual({"synthesized": 1, "no_evidence": 1}, data["consumed"])
        self.assertEqual(2, data["appended"])
        self.assertEqual(1, data["not_evaluated_count"])
        self.assertEqual(0, data["blocked_count"])
        self.assertTrue(data["handoff_unchanged"])
        self.assertEqual(handoff_before, (self.data_dir / HANDOFF_FILENAME).read_bytes())
        self.assertEqual(2, len(read_ledger_lines(self.data_dir)))

    def test_second_run_is_idempotent_and_writes_nothing(self) -> None:
        write_handoff(self.data_dir, [handoff_item("a@example.org")])
        path = self.evaluations([
            {"message_id": "a@example.org", "outcome": "synthesized",
             "vorgang_ref": "V-7", "evid_id": "E-7"},
        ])
        with self.patch_lease():
            self.assertEqual(0, self.consume(path)[0])
            ledger_before = (self.data_dir / LEDGER_FILENAME).read_bytes()
            code, envelope = self.consume(path)
        self.assertEqual(0, code)
        data = envelope["data"]
        self.assertEqual(("empty", 0, 0), (
            data["status"], data["pending_before"], data["pending_after"],
        ))
        self.assertEqual(0, data["appended"])
        self.assertEqual(1, data["already_consumed_count"])
        self.assertEqual(ledger_before, (self.data_dir / LEDGER_FILENAME).read_bytes())

    def test_dry_run_needs_no_lease_and_writes_nothing(self) -> None:
        write_handoff(self.data_dir, [
            handoff_item("a@example.org"),
            handoff_item("b@example.org"),
            handoff_item("c@example.org"),
        ])
        path = self.evaluations([
            {"message_id": "a@example.org", "outcome": "synthesized", "vorgang_ref": "V-7"},
            {"message_id": "b@example.org", "outcome": "no_evidence",
             "reason_code": "legacy_status_no_evidence"},
        ])
        before = snapshot(self.root)
        with patch.object(sent_ledger, "verify_workspace_lock",
                          side_effect=AssertionError("dry-run must not take a lease")):
            code, envelope = self.consume(path, "--dry-run", lease=False)
        self.assertEqual(0, code)
        data = envelope["data"]
        self.assertTrue(data["dry_run"])
        self.assertEqual(0, data["appended"])
        self.assertEqual(2, data["would_consume"])
        self.assertEqual({"synthesized": 1, "no_evidence": 1},
                         data["would_consume_outcomes"])
        self.assertEqual(3, data["pending_before"])
        self.assertEqual(3, data["pending_after"])
        self.assertEqual(1, data["projected_pending_after"])
        self.assertEqual(before, snapshot(self.root))

    def test_missing_lease_writes_nothing(self) -> None:
        write_handoff(self.data_dir, [handoff_item("a@example.org")])
        path = self.evaluations([
            {"message_id": "a@example.org", "outcome": "synthesized", "vorgang_ref": "V-7"},
        ])
        with patch.object(sent_ledger, "verify_workspace_lock",
                          side_effect=RuntimeError("no lease")):
            code, envelope = self.consume(path)
        self.assertEqual(2, code)
        self.assert_reason(envelope, "workspace_lease_required")
        self.assertFalse((self.data_dir / LEDGER_FILENAME).exists())

    def test_malformed_or_duplicate_evaluations_write_nothing(self) -> None:
        write_handoff(self.data_dir, [handoff_item("a@example.org")])
        cases = {
            "no_vorgang": [{"message_id": "a@example.org", "outcome": "synthesized"}],
            "duplicate": [
                {"message_id": "a@example.org", "outcome": "no_evidence",
                 "reason_code": "no_relevant_evidence"},
                {"message_id": "<a@example.org>", "outcome": "no_evidence",
                 "reason_code": "no_relevant_evidence"},
            ],
            "unknown_field": [{"message_id": "a@example.org", "outcome": "not_evaluated",
                               "note": "x"}],
        }
        for name, entries in cases.items():
            with self.subTest(name=name):
                path = self.evaluations(entries, name=f"{name}.json")
                with self.patch_lease():
                    code, envelope = self.consume(path)
                self.assertEqual(2, code)
                self.assert_reason(envelope, "evaluation_invalid")
                self.assertFalse((self.data_dir / LEDGER_FILENAME).exists())
        broken = self.root / "broken.json"
        broken.write_text("{ broken", encoding="utf-8")
        with self.patch_lease():
            code, envelope = self.consume(broken)
        self.assertEqual(2, code)
        self.assert_reason(envelope, "evaluation_invalid")

    def test_ancestor_is_consumed_before_its_reply(self) -> None:
        write_handoff(self.data_dir, [
            handoff_item("reply@example.org", in_reply_to="root@example.org"),
            handoff_item("root@example.org"),
        ])
        path = self.evaluations([
            {"message_id": "reply@example.org", "outcome": "synthesized",
             "vorgang_ref": "V-7", "evid_id": "E-8"},
            {"message_id": "root@example.org", "outcome": "synthesized",
             "vorgang_ref": "V-7", "evid_id": "E-7"},
        ])
        with self.patch_lease():
            code, envelope = self.consume(path)
        self.assertEqual(0, code)
        self.assertEqual(2, envelope["data"]["appended"])
        lines = {line["message_id"]: line for line in read_ledger_lines(self.data_dir)}
        self.assertEqual(("primary", None), (lines["root@example.org"]["role"],
                                             lines["root@example.org"]["anchored_to"]))
        self.assertEqual(("follow_up", "root@example.org"), (
            lines["reply@example.org"]["role"],
            lines["reply@example.org"]["anchored_to"],
        ))

    def test_reply_with_an_unevaluated_ancestor_is_deferred_not_consumed(self) -> None:
        write_handoff(self.data_dir, [
            handoff_item("reply@example.org", in_reply_to="root@example.org"),
            handoff_item("root@example.org"),
        ])
        path = self.evaluations([
            {"message_id": "reply@example.org", "outcome": "synthesized", "vorgang_ref": "V-7"},
        ])
        with self.patch_lease():
            code, envelope = self.consume(path)
        self.assertEqual(0, code)
        data = envelope["data"]
        self.assertEqual(("partial", 0, 2), (
            data["status"], data["appended"], data["pending_after"],
        ))
        self.assertEqual(1, data["not_evaluated_count"])
        self.assertEqual(1, data["deferred_count"])
        self.assertEqual("ancestor_pending", data["deferred"][0]["reason_code"])
        self.assertFalse((self.data_dir / LEDGER_FILENAME).exists())

    def test_per_item_anchor_conflict_is_reported_and_keeps_the_item_open(self) -> None:
        write_handoff(self.data_dir, [
            handoff_item("reply@example.org", in_reply_to="root@example.org"),
        ])
        write_ledger(self.data_dir, [ledger_line("root@example.org", vorgang_ref="V-1")])
        path = self.evaluations([
            {"message_id": "reply@example.org", "outcome": "synthesized",
             "vorgang_ref": "V-2", "evid_id": "E-8"},
        ])
        ledger_before = (self.data_dir / LEDGER_FILENAME).read_bytes()
        with self.patch_lease():
            code, envelope = self.consume(path)
        self.assertEqual(2, code)
        self.assert_reason(envelope, "consume_blocked")
        data = envelope["data"]
        self.assertEqual((1, 1), (data["pending_before"], data["pending_after"]))
        self.assertEqual("anchor_conflict", data["blocked"][0]["reason_code"])
        self.assertEqual(ledger_before, (self.data_dir / LEDGER_FILENAME).read_bytes())

    def test_durable_identity_conflict_stops_before_writing(self) -> None:
        write_handoff(self.data_dir, [])
        write_ledger(self.data_dir, [ledger_line("a@example.org", vorgang_ref="V-1")])
        path = self.evaluations([
            {"message_id": "a@example.org", "outcome": "no_evidence",
             "reason_code": "no_relevant_evidence"},
        ])
        ledger_before = (self.data_dir / LEDGER_FILENAME).read_bytes()
        with self.patch_lease():
            code, envelope = self.consume(path)
        self.assertEqual(2, code)
        self.assert_reason(envelope, "ledger_conflict")
        self.assertEqual(ledger_before, (self.data_dir / LEDGER_FILENAME).read_bytes())

    def test_shared_evid_across_two_entries_stops_before_writing(self) -> None:
        write_handoff(self.data_dir, [
            handoff_item("a@example.org"),
            handoff_item("b@example.org"),
        ])
        path = self.evaluations([
            {"message_id": "a@example.org", "outcome": "synthesized",
             "vorgang_ref": "V-7", "evid_id": "E-7"},
            {"message_id": "b@example.org", "outcome": "synthesized",
             "vorgang_ref": "V-7", "evid_id": "E-7"},
        ])
        with self.patch_lease():
            code, envelope = self.consume(path)
        self.assertEqual(2, code)
        self.assert_reason(envelope, "evid_shared_conflict")
        self.assertFalse((self.data_dir / LEDGER_FILENAME).exists())

    def test_reports_do_not_echo_raw_message_ids(self) -> None:
        write_handoff(self.data_dir, [
            handoff_item("secret-a@example.org"),
            handoff_item("secret-b@example.org"),
        ])
        path = self.evaluations([
            {"message_id": "secret-a@example.org", "outcome": "no_evidence",
             "reason_code": "no_relevant_evidence"},
        ])
        with self.patch_lease():
            code, envelope = self.consume(path)
        self.assertEqual(0, code)
        serialized = json.dumps(envelope, ensure_ascii=False)
        self.assertNotIn("secret-a@example.org", serialized)
        self.assertNotIn("secret-b@example.org", serialized)
        self.assertEqual(1, envelope["data"]["not_evaluated_count"])

    def test_account_gate_stops_the_whole_round(self) -> None:
        write_backend_config(self.workspace, account=ACCOUNT_A)
        write_handoff(self.data_dir, [handoff_item("a@example.org", mailbox=ACCOUNT_B)])
        path = self.evaluations([
            {"message_id": "a@example.org", "outcome": "synthesized", "vorgang_ref": "V-7"},
        ])
        with self.patch_lease():
            code, envelope = self.consume(path, "--account", ACCOUNT_A)
        self.assertEqual(2, code)
        self.assert_reason(envelope, "mailbox_mismatch")
        self.assertFalse((self.data_dir / LEDGER_FILENAME).exists())

    def test_empty_handoff_is_a_clean_noop(self) -> None:
        path = self.evaluations([
            {"message_id": "a@example.org", "outcome": "synthesized", "vorgang_ref": "V-7"},
        ])
        with self.patch_lease():
            code, envelope = self.consume(path)
        self.assertEqual(0, code)
        data = envelope["data"]
        self.assertEqual(("empty", 0, 0), (
            data["status"], data["pending_before"], data["pending_after"],
        ))
        self.assertEqual(1, data["unmatched_evaluations"])
        self.assertFalse((self.data_dir / LEDGER_FILENAME).exists())

    def test_explicit_not_evaluated_entry_stays_open(self) -> None:
        write_handoff(self.data_dir, [handoff_item("a@example.org")])
        path = self.evaluations([
            {"message_id": "a@example.org", "outcome": "not_evaluated"},
        ])
        with self.patch_lease():
            code, envelope = self.consume(path)
        self.assertEqual(0, code)
        data = envelope["data"]
        self.assertEqual((1, 0, 1), (
            data["pending_before"], data["appended"], data["pending_after"],
        ))
        self.assertEqual(1, data["not_evaluated_count"])


class CrashCheckTests(AdapterTestBase):
    def test_absent_triad_is_a_read_only_diagnosis(self) -> None:
        before = snapshot(self.root)
        code, envelope = self.crash_check("a@example.org", "V-7", "E-1")
        self.assertEqual(0, code)
        data = envelope["data"]
        self.assertEqual(("absent", "absent", "not_provided"),
                         (data["status"], data["ledger_state"], data["evidence_state"]))
        self.assertFalse(data["safe_to_append"])
        self.assertTrue(data["read_only"])
        self.assertEqual(before, snapshot(self.root))

    def test_unique_exact_triad(self) -> None:
        write_ledger(self.data_dir, [
            ledger_line("a@example.org", vorgang_ref="V-7", evid_id="E-1"),
        ])
        before = snapshot(self.root)
        code, envelope = self.crash_check("a@example.org", "V-7", "E-1")
        self.assertEqual(0, code)
        data = envelope["data"]
        self.assertEqual(("unique", "unique"), (data["status"], data["ledger_state"]))
        self.assertEqual(1, data["ledger_lines"])
        self.assertFalse(data["append_required"])
        self.assertEqual(before, snapshot(self.root))

    def test_different_triad_is_a_conflict(self) -> None:
        write_ledger(self.data_dir, [
            ledger_line("a@example.org", vorgang_ref="V-9", evid_id="E-9"),
        ])
        code, envelope = self.crash_check("a@example.org", "V-7", "E-1")
        self.assertEqual(2, code)
        self.assert_reason(envelope, "ledger_conflict")
        self.assertEqual("conflict", envelope["data"]["status"])

    def test_duplicate_ledger_lines_are_a_conflict(self) -> None:
        write_ledger(self.data_dir, [
            ledger_line("a@example.org", vorgang_ref="V-7", evid_id="E-1"),
            ledger_line("a@example.org", vorgang_ref="V-7", evid_id="E-1"),
        ])
        code, envelope = self.crash_check("a@example.org", "V-7", "E-1")
        self.assertEqual(2, code)
        self.assert_reason(envelope, "ledger_ambiguous")
        self.assertEqual(2, envelope["data"]["ledger_lines"])

    def test_foreign_evid_owner_is_a_conflict(self) -> None:
        write_ledger(self.data_dir, [
            ledger_line("other@example.org", vorgang_ref="V-1", evid_id="E-1"),
        ])
        code, envelope = self.crash_check("a@example.org", "V-7", "E-1")
        self.assertEqual(2, code)
        self.assert_reason(envelope, "evid_shared_conflict")

    def test_tokens_match_exactly_and_never_as_substrings(self) -> None:
        write_ledger(self.data_dir, [
            ledger_line("a@example.org", vorgang_ref="V-70", evid_id="E-10"),
        ])
        code, envelope = self.crash_check("a@example.org", "V-7", "E-10")
        self.assertEqual(2, code)
        self.assertEqual("conflict", envelope["data"]["status"])
        code, envelope = self.crash_check("a@example.org", "V-70", "E-1")
        self.assertEqual(2, code)
        self.assertEqual("conflict", envelope["data"]["status"])

    def test_non_exact_token_is_rejected_before_reading(self) -> None:
        before = snapshot(self.root)
        code, envelope = self.crash_check("a@example.org", " V-7", "E-1")
        self.assertEqual(2, code)
        self.assert_reason(envelope, "promotion_recovery_incomplete")
        self.assertEqual(before, snapshot(self.root))

    def test_external_evidence_without_a_ledger_line_is_safe_to_append(self) -> None:
        evidence = write_evidence(self.root / "promotions.json", [
            {"message_id": "<a@example.org>", "vorgang_ref": "V-7", "evid_id": "E-1"},
        ])
        code, envelope = self.crash_check(
            "a@example.org", "V-7", "E-1", "--evidence-file", str(evidence),
        )
        self.assertEqual(0, code)
        data = envelope["data"]
        self.assertEqual(("unique", "unique", "absent"),
                         (data["status"], data["evidence_state"], data["ledger_state"]))
        self.assertTrue(data["safe_to_append"])
        self.assertFalse((self.data_dir / LEDGER_FILENAME).exists())

    def test_absent_evidence_is_absent(self) -> None:
        evidence = write_evidence(self.root / "promotions.json", [])
        code, envelope = self.crash_check(
            "a@example.org", "V-7", "E-1", "--evidence-file", str(evidence),
        )
        self.assertEqual(0, code)
        self.assertEqual(("absent", "absent", "absent"),
                         (envelope["data"]["status"], envelope["data"]["evidence_state"],
                          envelope["data"]["ledger_state"]))
        self.assertFalse(envelope["data"]["safe_to_append"])

    def test_ambiguous_and_partial_evidence_are_conflicts(self) -> None:
        ambiguous = write_evidence(self.root / "ambiguous.json", [
            {"message_id": "<a@example.org>", "vorgang_ref": "V-7", "evid_id": "E-1"},
            {"message_id": "<a@example.org>", "vorgang_ref": "V-7", "evid_id": "E-1"},
        ])
        code, envelope = self.crash_check(
            "a@example.org", "V-7", "E-1", "--evidence-file", str(ambiguous),
        )
        self.assertEqual(2, code)
        self.assert_reason(envelope, "promotion_ambiguous")
        partial = write_evidence(self.root / "partial.json", [
            {"message_id": "<a@example.org>", "vorgang_ref": "V-7", "evid_id": "E-9"},
        ])
        code, envelope = self.crash_check(
            "a@example.org", "V-7", "E-1", "--evidence-file", str(partial),
        )
        self.assertEqual(2, code)
        self.assert_reason(envelope, "promotion_conflict")

    def test_competing_evid_owner_in_evidence_is_a_conflict(self) -> None:
        evidence = write_evidence(self.root / "competing.json", [
            {"message_id": "<a@example.org>", "vorgang_ref": "V-7", "evid_id": "E-1"},
            {"message_id": "<other@example.org>", "vorgang_ref": "V-1", "evid_id": "E-1"},
        ])
        code, envelope = self.crash_check(
            "a@example.org", "V-7", "E-1", "--evidence-file", str(evidence),
        )
        self.assertEqual(2, code)
        self.assert_reason(envelope, "evid_shared_conflict")

    def test_consumed_ledger_line_without_evidence_is_a_conflict(self) -> None:
        write_ledger(self.data_dir, [
            ledger_line("a@example.org", vorgang_ref="V-7", evid_id="E-1"),
        ])
        evidence = write_evidence(self.root / "empty.json", [])
        code, envelope = self.crash_check(
            "a@example.org", "V-7", "E-1", "--evidence-file", str(evidence),
        )
        self.assertEqual(2, code)
        self.assert_reason(envelope, "evidence_incomplete")

    def test_foreign_thread_anchor_stops_the_recovery_candidate(self) -> None:
        write_ledger(self.data_dir, [
            ledger_line("root@example.org", vorgang_ref="V-1", evid_id="E-1"),
        ])
        evidence = write_evidence(self.root / "anchor.json", [{
            "message_id": "<reply@example.org>", "vorgang_ref": "V-2", "evid_id": "E-2",
            "in_reply_to": "<root@example.org>",
        }])
        code, envelope = self.crash_check(
            "reply@example.org", "V-2", "E-2", "--evidence-file", str(evidence),
        )
        self.assertEqual(2, code)
        self.assert_reason(envelope, "promotion_anchor_conflict")
        self.assertEqual(1, len(read_ledger_lines(self.data_dir)))

    def test_malformed_evidence_fails_closed_without_writing(self) -> None:
        evidence = self.root / "broken.json"
        evidence.write_text("{ broken", encoding="utf-8")
        before = snapshot(self.root)
        code, envelope = self.crash_check(
            "a@example.org", "V-7", "E-1", "--evidence-file", str(evidence),
        )
        self.assertEqual(2, code)
        self.assert_reason(envelope, "promotion_recovery_incomplete")
        self.assertEqual(before, snapshot(self.root))


class AnchorConsistencyTests(AdapterTestBase):
    """Conservative anchor checks shared by record, crash-check and recovery."""

    def patch_lease(self):
        return patch.object(sent_ledger, "verify_workspace_lock", return_value=object())

    def recover(self, message_id: str, vorgang_ref: str, evid_id: str,
                evidence: list[dict], *extra: str) -> tuple[int, dict]:
        path = write_evidence(self.root / "promotions.json", evidence)
        return self.run_cli(
            "recover-promotion", "--workspace", str(self.workspace),
            "--data-dir", "data/mail-desk",
            "--message-id", message_id, "--vorgang-ref", vorgang_ref,
            "--evid-id", evid_id, "--evidence-file", str(path),
            "--workspace-lease-id", "LEASE-1", *extra,
        )

    def _reply_fixture(self, *, headers_complete: bool = True,
                       references: list[str] | None = None) -> None:
        write_handoff(self.data_dir, [
            handoff_item("near@example.org"),
            handoff_item("far@example.org"),
            handoff_item(
                "reply@example.org", in_reply_to="near@example.org",
                references=references if references is not None else ["far@example.org"],
                headers_complete=headers_complete,
            ),
        ])

    def test_record_stops_on_divergent_duplicate_ancestor_lines(self) -> None:
        self._reply_fixture()
        write_ledger(self.data_dir, [
            ledger_line("near@example.org", vorgang_ref="V-1", evid_id=None),
            ledger_line("near@example.org", vorgang_ref="V-9", evid_id=None),
            ledger_line("far@example.org", vorgang_ref="V-3", evid_id=None),
        ])
        before = (self.data_dir / LEDGER_FILENAME).read_bytes()
        with self.patch_lease():
            code, envelope = self.record(
                "--outcome", "synthesized", "--vorgang-ref", "V-1",
                message_id="reply@example.org",
            )
        self.assertEqual(2, code)
        self.assert_reason(envelope, "anchor_ambiguous")
        self.assertEqual(before, (self.data_dir / LEDGER_FILENAME).read_bytes())

    def test_record_stops_on_missing_handoff_headers(self) -> None:
        self._reply_fixture(headers_complete=False)
        write_ledger(self.data_dir, [
            ledger_line("near@example.org", vorgang_ref="V-1", evid_id=None),
            ledger_line("far@example.org", vorgang_ref="V-3", evid_id=None),
        ])
        before = (self.data_dir / LEDGER_FILENAME).read_bytes()
        with self.patch_lease():
            code, envelope = self.record(
                "--outcome", "synthesized", "--vorgang-ref", "V-1",
                message_id="reply@example.org",
            )
        self.assertEqual(2, code)
        self.assert_reason(envelope, "handoff_metadata_incomplete")
        self.assertEqual(before, (self.data_dir / LEDGER_FILENAME).read_bytes())

    def test_crash_check_stops_on_divergent_duplicate_ancestor_lines(self) -> None:
        self._reply_fixture()
        write_ledger(self.data_dir, [
            ledger_line("near@example.org", vorgang_ref="V-1", evid_id=None),
            ledger_line("near@example.org", vorgang_ref="V-9", evid_id=None),
        ])
        code, envelope = self.crash_check("reply@example.org", "V-1", "E-2")
        self.assertEqual(2, code)
        self.assert_reason(envelope, "promotion_anchor_ambiguous")

    def test_crash_check_stops_on_reference_less_anchor(self) -> None:
        self._reply_fixture()
        write_ledger(self.data_dir, [
            ledger_line("near@example.org", vorgang_ref=None, evid_id=None),
        ])
        code, envelope = self.crash_check("reply@example.org", "V-1", "E-2")
        self.assertEqual(2, code)
        self.assert_reason(envelope, "promotion_anchor_unresolved")

    def test_crash_check_stops_on_missing_handoff_headers(self) -> None:
        self._reply_fixture(headers_complete=False)
        code, envelope = self.crash_check("reply@example.org", "V-1", "E-2")
        self.assertEqual(2, code)
        self.assert_reason(envelope, "handoff_metadata_incomplete")

    def test_crash_check_checks_all_other_thread_anchors(self) -> None:
        self._reply_fixture()
        write_ledger(self.data_dir, [
            ledger_line("near@example.org", vorgang_ref="V-1", evid_id=None),
            ledger_line("far@example.org", vorgang_ref="V-2", evid_id=None),
        ])
        code, envelope = self.crash_check("reply@example.org", "V-1", "E-2")
        self.assertEqual(2, code)
        self.assert_reason(envelope, "promotion_anchor_conflict")
        self.assertIn("V-2", envelope["data"]["conflicting_refs"])

    def test_recovery_stops_on_other_thread_anchor(self) -> None:
        self._reply_fixture()
        write_ledger(self.data_dir, [
            ledger_line("near@example.org", vorgang_ref="V-1", evid_id=None),
            ledger_line("far@example.org", vorgang_ref="V-2", evid_id=None),
        ])
        with self.patch_lease():
            code, envelope = self.recover(
                "reply@example.org", "V-1", "E-2",
                [{"message_id": "<reply@example.org>", "vorgang_ref": "V-1",
                  "evid_id": "E-2"}],
            )
        self.assertEqual(2, code)
        self.assert_reason(envelope, "promotion_anchor_conflict")
        self.assertIn("V-2", envelope["data"]["conflicting_refs"])

    def test_recovery_stops_on_reference_less_anchor(self) -> None:
        self._reply_fixture()
        write_ledger(self.data_dir, [
            ledger_line("near@example.org", vorgang_ref=None, evid_id=None),
        ])
        with self.patch_lease():
            code, envelope = self.recover(
                "reply@example.org", "V-1", "E-2",
                [{"message_id": "<reply@example.org>", "vorgang_ref": "V-1",
                  "evid_id": "E-2"}],
            )
        self.assertEqual(2, code)
        self.assert_reason(envelope, "promotion_anchor_unresolved")

    def test_recovery_stops_on_missing_handoff_headers(self) -> None:
        self._reply_fixture(headers_complete=False)
        with self.patch_lease():
            code, envelope = self.recover(
                "reply@example.org", "V-1", "E-2",
                [{"message_id": "<reply@example.org>", "vorgang_ref": "V-1",
                  "evid_id": "E-2"}],
            )
        self.assertEqual(2, code)
        self.assert_reason(envelope, "handoff_metadata_incomplete")

    def test_recovery_stops_on_contradicting_metadata(self) -> None:
        self._reply_fixture()
        with self.patch_lease():
            code, envelope = self.recover(
                "reply@example.org", "V-1", "E-2",
                [{"message_id": "<reply@example.org>", "vorgang_ref": "V-1",
                  "evid_id": "E-2", "in_reply_to": "<far@example.org>"}],
            )
        self.assertEqual(2, code)
        self.assert_reason(envelope, "promotion_metadata_conflict")

    def test_foreign_evid_without_target_record_is_a_conflict_not_absent(self) -> None:
        evidence = write_evidence(self.root / "foreign.json", [
            {"message_id": "", "vorgang_ref": "V-1", "evid_id": "E-1"},
        ])
        code, envelope = self.crash_check(
            "a@example.org", "V-7", "E-1", "--evidence-file", str(evidence),
        )
        self.assertEqual(2, code)
        self.assert_reason(envelope, "competing_evidence_missing_message_id")

        evidence = write_evidence(self.root / "foreign-other.json", [
            {"message_id": "<other@example.org>", "vorgang_ref": "V-1", "evid_id": "E-1"},
        ])
        code, envelope = self.crash_check(
            "a@example.org", "V-7", "E-1", "--evidence-file", str(evidence),
        )
        self.assertEqual(2, code)
        self.assert_reason(envelope, "evid_shared_conflict")
        self.assertFalse((self.data_dir / LEDGER_FILENAME).exists())


class AncestorPendingGateTests(AdapterTestBase):
    """An open ancestor blocks crash-check and recovery exactly like record."""

    def patch_lease(self):
        return patch.object(sent_ledger, "verify_workspace_lock", return_value=object())

    def recover(self, message_id: str, vorgang_ref: str, evid_id: str,
                evidence: list[dict], *extra: str) -> tuple[int, dict]:
        path = write_evidence(self.root / "promotions.json", evidence)
        return self.run_cli(
            "recover-promotion", "--workspace", str(self.workspace),
            "--data-dir", "data/mail-desk",
            "--message-id", message_id, "--vorgang-ref", vorgang_ref,
            "--evid-id", evid_id, "--evidence-file", str(path),
            "--workspace-lease-id", "LEASE-1", *extra,
        )

    def write_reply_with_open_ancestor(self) -> None:
        write_handoff(self.data_dir, [
            handoff_item("root@example.org"),
            handoff_item("reply@example.org", in_reply_to="root@example.org"),
        ])

    def test_recovery_stops_on_pending_ancestor_without_writing(self) -> None:
        self.write_reply_with_open_ancestor()
        before = snapshot(self.data_dir)
        with self.patch_lease():
            code, envelope = self.recover(
                "reply@example.org", "V-1", "E-2",
                [{"message_id": "<reply@example.org>", "vorgang_ref": "V-1",
                  "evid_id": "E-2"}],
            )
        self.assertEqual(2, code)
        self.assert_reason(envelope, "ancestor_pending")
        self.assertEqual(1, len(envelope["data"]["waits_for"]))
        self.assertEqual(before, snapshot(self.data_dir))
        self.assertFalse((self.data_dir / LEDGER_FILENAME).exists())

    def test_crash_check_never_recommends_append_with_pending_ancestor(self) -> None:
        self.write_reply_with_open_ancestor()
        evidence = write_evidence(self.root / "promotions.json", [
            {"message_id": "<reply@example.org>", "vorgang_ref": "V-1", "evid_id": "E-2"},
        ])
        before = snapshot(self.data_dir)
        code, envelope = self.crash_check(
            "reply@example.org", "V-1", "E-2", "--evidence-file", str(evidence),
        )
        self.assertEqual(2, code)
        self.assert_reason(envelope, "ancestor_pending")
        data = envelope["data"]
        self.assertFalse(data["safe_to_append"])
        self.assertFalse(data["append_required"])
        self.assertEqual("conflict", data["status"])
        self.assertEqual(before, snapshot(self.data_dir))
        self.assertFalse((self.data_dir / LEDGER_FILENAME).exists())

    def test_evidence_only_reply_stops_for_a_pending_handoff_ancestor(self) -> None:
        write_handoff(self.data_dir, [handoff_item("root@example.org")])
        evidence = write_evidence(self.root / "promotions.json", [{
            "message_id": "<reply@example.org>", "vorgang_ref": "V-1", "evid_id": "E-2",
            "in_reply_to": "<root@example.org>",
        }])
        before = snapshot(self.data_dir)
        code, envelope = self.crash_check(
            "reply@example.org", "V-1", "E-2", "--evidence-file", str(evidence),
        )
        self.assertEqual(2, code)
        self.assert_reason(envelope, "ancestor_pending")
        self.assertFalse(envelope["data"]["safe_to_append"])
        with self.patch_lease():
            code, envelope = self.recover(
                "reply@example.org", "V-1", "E-2",
                [{"message_id": "<reply@example.org>", "vorgang_ref": "V-1",
                  "evid_id": "E-2", "in_reply_to": "<root@example.org>"}],
            )
        self.assertEqual(2, code)
        self.assert_reason(envelope, "ancestor_pending")
        self.assertEqual(before, snapshot(self.data_dir))
        self.assertFalse((self.data_dir / LEDGER_FILENAME).exists())

    def test_consumed_ancestor_allows_the_reply(self) -> None:
        self.write_reply_with_open_ancestor()
        write_ledger(self.data_dir, [
            ledger_line("root@example.org", vorgang_ref="V-1", evid_id="E-1"),
        ])
        with self.patch_lease():
            code, envelope = self.recover(
                "reply@example.org", "V-1", "E-2",
                [{"message_id": "<reply@example.org>", "vorgang_ref": "V-1",
                  "evid_id": "E-2"}],
            )
        self.assertEqual(0, code)
        self.assertEqual("recovered", envelope["data"]["status"])
        self.assertEqual(1, envelope["data"]["appended"])
        self.assertEqual(2, len(read_ledger_lines(self.data_dir)))


class StrictEvidenceMetadataTests(AdapterTestBase):
    """Malformed evidence thread metadata fails closed instead of being coerced."""

    def patch_lease(self):
        return patch.object(sent_ledger, "verify_workspace_lock", return_value=object())

    def recover(self, evidence: list[dict]) -> tuple[int, dict]:
        path = write_evidence(self.root / "promotions.json", evidence)
        return self.run_cli(
            "recover-promotion", "--workspace", str(self.workspace),
            "--data-dir", "data/mail-desk",
            "--message-id", "reply@example.org", "--vorgang-ref", "V-1",
            "--evid-id", "E-2", "--evidence-file", str(path),
            "--workspace-lease-id", "LEASE-1",
        )

    def test_recovery_rejects_string_references_without_handoff(self) -> None:
        before = snapshot(self.data_dir)
        with self.patch_lease():
            code, envelope = self.recover([{
                "message_id": "<reply@example.org>", "vorgang_ref": "V-1",
                "evid_id": "E-2", "references": "<root@example.org>",
            }])
        self.assertEqual(2, code)
        self.assert_reason(envelope, "promotion_metadata_invalid")
        self.assertEqual(before, snapshot(self.data_dir))
        self.assertFalse((self.data_dir / LEDGER_FILENAME).exists())

    def test_recovery_rejects_non_string_in_reply_to_without_handoff(self) -> None:
        before = snapshot(self.data_dir)
        with self.patch_lease():
            code, envelope = self.recover([{
                "message_id": "<reply@example.org>", "vorgang_ref": "V-1",
                "evid_id": "E-2", "in_reply_to": 42,
            }])
        self.assertEqual(2, code)
        self.assert_reason(envelope, "promotion_metadata_invalid")
        self.assertEqual(before, snapshot(self.data_dir))

    def test_recovery_rejects_non_list_references_without_handoff(self) -> None:
        with self.patch_lease():
            code, envelope = self.recover([{
                "message_id": "<reply@example.org>", "vorgang_ref": "V-1",
                "evid_id": "E-2", "references": {"root": "<root@example.org>"},
            }])
        self.assertEqual(2, code)
        self.assert_reason(envelope, "promotion_metadata_invalid")
        self.assertFalse((self.data_dir / LEDGER_FILENAME).exists())

    def test_recovery_rejects_whitespace_only_in_reply_to_without_handoff(self) -> None:
        with self.patch_lease():
            code, envelope = self.recover([{
                "message_id": "<reply@example.org>", "vorgang_ref": "V-1",
                "evid_id": "E-2", "in_reply_to": "   ",
            }])
        self.assertEqual(2, code)
        self.assert_reason(envelope, "promotion_metadata_invalid")
        self.assertFalse((self.data_dir / LEDGER_FILENAME).exists())

    def test_crash_check_rejects_non_string_reference_entry_without_handoff(self) -> None:
        evidence = write_evidence(self.root / "promotions.json", [{
            "message_id": "<reply@example.org>", "vorgang_ref": "V-1",
            "evid_id": "E-2", "references": ["<root@example.org>", 7],
        }])
        before = snapshot(self.data_dir)
        code, envelope = self.crash_check(
            "reply@example.org", "V-1", "E-2", "--evidence-file", str(evidence),
        )
        self.assertEqual(2, code)
        self.assert_reason(envelope, "promotion_metadata_invalid")
        self.assertFalse(envelope["data"]["safe_to_append"])
        self.assertEqual(before, snapshot(self.data_dir))

    def test_crash_check_rejects_non_string_thread_key_without_handoff(self) -> None:
        evidence = write_evidence(self.root / "promotions.json", [{
            "message_id": "<reply@example.org>", "vorgang_ref": "V-1",
            "evid_id": "E-2", "thread_key": 7,
        }])
        code, envelope = self.crash_check(
            "reply@example.org", "V-1", "E-2", "--evidence-file", str(evidence),
        )
        self.assertEqual(2, code)
        self.assert_reason(envelope, "promotion_metadata_invalid")
        self.assertFalse((self.data_dir / LEDGER_FILENAME).exists())

    def test_valid_thread_metadata_without_handoff_stays_appendable(self) -> None:
        evidence = write_evidence(self.root / "promotions.json", [{
            "message_id": "<reply@example.org>", "vorgang_ref": "V-1",
            "evid_id": "E-2", "in_reply_to": "<root@example.org>",
            "references": ["<root@example.org>"],
        }])
        before = snapshot(self.data_dir)
        code, envelope = self.crash_check(
            "reply@example.org", "V-1", "E-2", "--evidence-file", str(evidence),
        )
        self.assertEqual(0, code)
        data = envelope["data"]
        self.assertEqual("unique", data["status"])
        self.assertTrue(data["safe_to_append"])
        self.assertEqual(before, snapshot(self.data_dir))


class EnvelopeContractTests(AdapterTestBase):
    def test_json_flag_is_accepted_and_changes_nothing(self) -> None:
        write_handoff(self.data_dir, [handoff_item("a@example.org")])
        _, without = self.pending()
        _, with_flag = self.pending("--json")
        self.assertEqual(without["data"], with_flag["data"])

    def test_json_flag_is_accepted_for_the_other_commands(self) -> None:
        write_ledger(self.data_dir, [
            ledger_line("a@example.org", vorgang_ref="V-7", evid_id="E-1"),
        ])
        code, envelope = self.crash_check("a@example.org", "V-7", "E-1", "--json")
        self.assertEqual(0, code)
        self.assertTrue(envelope["success"])

    def test_error_envelopes_carry_a_reason_code_and_compact_message(self) -> None:
        write_handoff(self.data_dir, [handoff_item("a@example.org")])
        code, envelope = self.pending("--account", ACCOUNT_A)
        self.assertEqual(2, code)
        self.assertIn("reason_code", envelope["data"])
        self.assertLessEqual(len(envelope["error"]["message"]), 1000)


if __name__ == "__main__":
    unittest.main()
