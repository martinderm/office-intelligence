"""Issue #96 regression test: auto_resolve false positive on subject_clean.

Validates that check_if_replied and auto_resolve_replies_from_sent:
1. Never match a sent item via subject_clean if that sent item is the incoming
   email's In-Reply-To parent or listed in its References (initiator loop).
2. Enforce temporal order (sent_dt >= incoming_dt - clock_skew) so an earlier
   sent email cannot be mistaken for a reply to a later incoming email even
   when thread anchors are missing.
3. Correctly preserve date, in_reply_to, and references in replies-needed.jsonl
   when execute mode appends a reply case.
4. Continue to recognize genuine True Positive replies sent after the incoming email.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

from core import sent_indexer  # noqa: E402
from core.modes import execute as execute_mode  # noqa: E402
from review_fixtures import approved_execute  # noqa: E402


class _Workspace:
    """Hermetic temp workspace for mail-desk data."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()

    def __enter__(self) -> Path:
        return Path(self._tmp.name)

    def __exit__(self, *exc: object) -> None:
        self._tmp.cleanup()


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line.strip()) for line in handle if line.strip()]


class Issue96AutoResolveFalsePositiveTests(unittest.TestCase):
    """Regression tests for Issue #96 auto-resolve false positive."""

    def test_repro_initiator_sent_before_reply_not_resolved_by_subject_clean(self) -> None:
        """Repro Issue #96: An outgoing initiator from 2 days ago must not auto-resolve

        an incoming reply whose In-Reply-To points to that initiator.
        """
        initiator_sent = {
            "message_id": "<initiator-001@myorg.test>",
            "subject": "Quarterly Budget Review",
            "from": "me@myorg.test",
            "to": ["partner@example.test"],
            "at": "2026-11-10T10:00:00Z",
            "sent_envelope_id": "101",
        }

        incoming_reply = {
            "message_id": "<reply-002@example.test>",
            "subject": "Re: Quarterly Budget Review",
            "from": "Partner <partner@example.test>",
            "to": "me@myorg.test",
            "date": "2026-11-12T10:00:00Z",
            "in_reply_to": "<initiator-001@myorg.test>",
            "references": ["<initiator-001@myorg.test>"],
            "reply_status": "needed",
            "reply_note": "Partner asks for clarification on row 4",
        }

        with _Workspace() as ws:
            dd = ws / "data" / "mail-desk"
            _write_jsonl(dd / "sent-index.jsonl", [initiator_sent])
            _write_jsonl(dd / "replies-needed.jsonl", [incoming_reply])

            lookup = sent_indexer.load_sent_index(dd)

            # Direct check_if_replied: must NOT match the initiator email
            check_result = sent_indexer.check_if_replied(incoming_reply, lookup)
            self.assertTrue(
                check_result is None or not check_result.get("replied"),
                f"check_if_replied falsely matched initiator as reply: {check_result}",
            )

            # auto_resolve_replies_from_sent: must not resolve the open case
            resolve_result = sent_indexer.auto_resolve_replies_from_sent(data_dir=dd)
            self.assertEqual(
                0,
                resolve_result["resolved_count"],
                f"auto_resolve_replies_from_sent resolved {resolve_result['resolved_count']} items unexpectedly",
            )
            self.assertEqual(1, len(resolve_result["remaining_open"]))
            self.assertEqual(
                "<reply-002@example.test>",
                resolve_result["remaining_open"][0]["message_id"],
            )

            # Verify replies-needed.jsonl still holds the open case
            remaining_entries = _read_jsonl(dd / "replies-needed.jsonl")
            self.assertEqual(1, len(remaining_entries))
            self.assertEqual("<reply-002@example.test>", remaining_entries[0]["message_id"])
            self.assertEqual("needed", remaining_entries[0]["reply_status"])

    def test_temporal_order_prevents_false_positive_when_thread_anchors_missing(self) -> None:
        """When In-Reply-To/References are missing, temporal check prevents matching

        a sent email that occurred BEFORE the incoming email.
        """
        earlier_sent = {
            "message_id": "<earlier-001@myorg.test>",
            "subject": "Server Migration",
            "from": "me@myorg.test",
            "to": ["partner@example.test"],
            "at": "2026-11-10T10:00:00Z",
            "sent_envelope_id": "102",
        }

        later_incoming = {
            "message_id": "<incoming-003@example.test>",
            "subject": "Re: Server Migration",
            "from": "Partner <partner@example.test>",
            "date": "2026-11-12T10:00:00Z",
            # No in_reply_to or references
            "reply_status": "needed",
        }

        with _Workspace() as ws:
            dd = ws / "data" / "mail-desk"
            _write_jsonl(dd / "sent-index.jsonl", [earlier_sent])
            _write_jsonl(dd / "replies-needed.jsonl", [later_incoming])

            lookup = sent_indexer.load_sent_index(dd)
            check_result = sent_indexer.check_if_replied(later_incoming, lookup)
            self.assertTrue(
                check_result is None or not check_result.get("replied"),
                f"check_if_replied matched an earlier sent email without thread anchors: {check_result}",
            )

            resolve_result = sent_indexer.auto_resolve_replies_from_sent(data_dir=dd)
            self.assertEqual(0, resolve_result["resolved_count"])

    def test_execute_mode_persists_date_in_reply_to_and_references(self) -> None:
        """execute mode must write date, in_reply_to, references to replies-needed.jsonl."""
        with _Workspace() as ws:
            dd = ws / "data" / "mail-desk"
            dd.mkdir(parents=True, exist_ok=True)
            idx_file = dd / "index.json"

            item = {
                "envelope_id": "42",
                "message_id": "<item-99@example.test>",
                "subject": "Need Urgent Reply",
                "from": "Sender <sender@example.test>",
                "date": "2026-11-12T11:22:33Z",
                "in_reply_to": "<parent-00@myorg.test>",
                "references": ["<grandparent-00@myorg.test>", "<parent-00@myorg.test>"],
                "action": {"type": "copy_as_move", "target_folder": "INBOX"},
                "decision": {"needs_reply": True},
                "notes": "Reply needed regarding contract",
            }

            request = approved_execute({
                "mode": "execute",
                "items": [item],
            }, account="primary")

            execute_mode.run_execute_mode(
                request,
                account="primary",
                data_dir=dd,
                index_path=idx_file,
                dependencies={
                    "BatchProgressTracker": Mock(),
                    "verify_in_target_folder": Mock(return_value="42"),
                    "run_himalaya": Mock(),
                    "load_final_index": Mock(return_value={"items": {}}),
                    "save_final_index_atomic": Mock(),
                    "flush_batch_evidence": Mock(),
                    "auto_resolve_replies_from_sent": Mock(),
                    "sleep": Mock(),
                },
            )

            replies = _read_jsonl(dd / "replies-needed.jsonl")
            self.assertEqual(1, len(replies))
            entry = replies[0]

            self.assertEqual("item-99@example.test", entry.get("message_id"))
            self.assertEqual("2026-11-12T11:22:33Z", entry.get("date"), "date must be persisted in replies-needed.jsonl")
            self.assertEqual("<parent-00@myorg.test>", entry.get("in_reply_to"), "in_reply_to must be persisted")
            self.assertEqual(
                ["<grandparent-00@myorg.test>", "<parent-00@myorg.test>"],
                entry.get("references"),
                "references must be persisted",
            )

    def test_true_positive_later_reply_resolves_clean_subject(self) -> None:
        """A genuine subsequent sent reply with matching clean subject MUST resolve."""
        initiator_sent = {
            "message_id": "<initiator-001@myorg.test>",
            "subject": "Quarterly Budget Review",
            "from": "me@myorg.test",
            "to": ["partner@example.test"],
            "at": "2026-11-10T10:00:00Z",
            "sent_envelope_id": "101",
        }

        incoming_reply = {
            "message_id": "<reply-002@example.test>",
            "subject": "Re: Quarterly Budget Review",
            "from": "Partner <partner@example.test>",
            "to": "me@myorg.test",
            "date": "2026-11-12T10:00:00Z",
            "in_reply_to": "<initiator-001@myorg.test>",
            "references": ["<initiator-001@myorg.test>"],
            "reply_status": "needed",
            "reply_note": "Partner asks for clarification",
        }

        actual_reply_sent = {
            "message_id": "<final-reply-003@myorg.test>",
            "subject": "Re: Quarterly Budget Review",
            "from": "me@myorg.test",
            "to": ["partner@example.test"],
            "at": "2026-11-12T14:30:00Z",  # 4.5 hours after incoming reply
            "sent_envelope_id": "103",
        }

        with _Workspace() as ws:
            dd = ws / "data" / "mail-desk"
            _write_jsonl(dd / "sent-index.jsonl", [initiator_sent, actual_reply_sent])
            _write_jsonl(dd / "replies-needed.jsonl", [incoming_reply])

            lookup = sent_indexer.load_sent_index(dd)
            check_result = sent_indexer.check_if_replied(incoming_reply, lookup)
            self.assertIsNotNone(check_result)
            self.assertTrue(check_result.get("replied"))
            self.assertEqual("<final-reply-003@myorg.test>", check_result.get("sent_message_id"))

            resolve_result = sent_indexer.auto_resolve_replies_from_sent(data_dir=dd)
            self.assertEqual(1, resolve_result["resolved_count"])
            self.assertEqual(0, len(resolve_result["remaining_open"]))


    def test_clock_skew_boundary(self) -> None:
        """A sent reply within 5 minutes before incoming date is accepted (clock skew),

        but a sent email > 5 minutes before incoming date is rejected.
        """
        sent_within_skew = {
            "message_id": "<skew-ok@myorg.test>",
            "subject": "System Status Update",
            "from": "me@myorg.test",
            "to": ["partner@example.test"],
            "at": "2026-11-12T10:00:00Z",  # 2 minutes before incoming
            "sent_envelope_id": "201",
        }
        sent_too_early = {
            "message_id": "<too-early@myorg.test>",
            "subject": "System Status Update",
            "from": "me@myorg.test",
            "to": ["partner@example.test"],
            "at": "2026-11-12T09:50:00Z",  # 12 minutes before incoming (> 5 min)
            "sent_envelope_id": "202",
        }

        incoming_email = {
            "message_id": "<incoming-status@example.test>",
            "subject": "Re: System Status Update",
            "from": "Partner <partner@example.test>",
            "date": "2026-11-12T10:02:00Z",
            "reply_status": "needed",
        }

        # Case 1: Only sent_within_skew in sent index -> should match
        with _Workspace() as ws:
            dd = ws / "data" / "mail-desk"
            _write_jsonl(dd / "sent-index.jsonl", [sent_within_skew])
            lookup = sent_indexer.load_sent_index(dd)
            check_result = sent_indexer.check_if_replied(incoming_email, lookup)
            self.assertIsNotNone(check_result)
            self.assertTrue(check_result.get("replied"))
            self.assertEqual("<skew-ok@myorg.test>", check_result.get("sent_message_id"))

        # Case 2: Only sent_too_early in sent index -> should NOT match
        with _Workspace() as ws:
            dd = ws / "data" / "mail-desk"
            _write_jsonl(dd / "sent-index.jsonl", [sent_too_early])
            lookup = sent_indexer.load_sent_index(dd)
            check_result = sent_indexer.check_if_replied(incoming_email, lookup)
            self.assertTrue(
                check_result is None or not check_result.get("replied"),
                f"check_if_replied matched an email sent 12 minutes prior: {check_result}",
            )

    def test_references_ancestor_prevents_false_positive_even_without_in_reply_to(self) -> None:
        """If In-Reply-To is missing but References names the sent initiator,

        it must not match via subject_clean.
        """
        initiator = {
            "message_id": "<initiator-ref-only@myorg.test>",
            "subject": "Architecture Discussion",
            "from": "me@myorg.test",
            "to": ["partner@example.test"],
            "at": "2026-11-10T10:00:00Z",
            "sent_envelope_id": "301",
        }

        incoming = {
            "message_id": "<incoming-ref-only@example.test>",
            "subject": "Re: Architecture Discussion",
            "from": "Partner <partner@example.test>",
            "in_reply_to": "",  # Missing In-Reply-To
            "references": ["<initiator-ref-only@myorg.test>"],
            "date": "2026-11-12T10:00:00Z",
            "reply_status": "needed",
        }

        with _Workspace() as ws:
            dd = ws / "data" / "mail-desk"
            _write_jsonl(dd / "sent-index.jsonl", [initiator])
            _write_jsonl(dd / "replies-needed.jsonl", [incoming])

            lookup = sent_indexer.load_sent_index(dd)
            check_result = sent_indexer.check_if_replied(incoming, lookup)
            self.assertTrue(
                check_result is None or not check_result.get("replied"),
                f"check_if_replied matched an ancestor from references: {check_result}",
            )

            resolve_result = sent_indexer.auto_resolve_replies_from_sent(data_dir=dd)
            self.assertEqual(0, resolve_result["resolved_count"])


    def test_true_positive_later_reply_without_thread_anchors_resolves(self) -> None:
        """When thread anchors are missing entirely, a genuine subsequent reply

        (sent_dt >= incoming_dt) must still match via subject_clean (True Positive).
        """
        incoming_without_anchors = {
            "message_id": "<incoming-no-anchors@example.test>",
            "subject": "Re: Roadmap Discussion",
            "from": "Partner <partner@example.test>",
            "date": "2026-11-12T10:00:00Z",
            # No in_reply_to, no references
            "reply_status": "needed",
        }

        later_sent_reply = {
            "message_id": "<sent-later-reply@myorg.test>",
            "subject": "Re: Roadmap Discussion",
            "from": "me@myorg.test",
            "to": ["partner@example.test"],
            "at": "2026-11-12T12:00:00Z",  # 2 hours later
            "sent_envelope_id": "401",
        }

        with _Workspace() as ws:
            dd = ws / "data" / "mail-desk"
            _write_jsonl(dd / "sent-index.jsonl", [later_sent_reply])
            _write_jsonl(dd / "replies-needed.jsonl", [incoming_without_anchors])

            lookup = sent_indexer.load_sent_index(dd)
            check_result = sent_indexer.check_if_replied(incoming_without_anchors, lookup)
            self.assertIsNotNone(check_result)
            self.assertTrue(check_result.get("replied"))
            self.assertEqual("subject_clean", check_result.get("match_type"))
            self.assertEqual("<sent-later-reply@myorg.test>", check_result.get("sent_message_id"))

            resolve_result = sent_indexer.auto_resolve_replies_from_sent(data_dir=dd)
            self.assertEqual(1, resolve_result["resolved_count"])
            self.assertEqual(0, len(resolve_result["remaining_open"]))


    def test_multi_id_references_string_prevents_false_positive_in_check_if_replied(self) -> None:
        """A whitespace-separated multi-ID references header string (e.g. from himalaya)

        must be split so each ancestor is excluded from subject_clean matching.
        """
        initiator = {
            "message_id": "<initiator-multi@myorg.test>",
            "subject": "Project Architecture Alignment",
            "from": "me@myorg.test",
            "to": ["partner@example.test"],
            "at": "2026-11-10T10:00:00Z",
            "sent_envelope_id": "501",
        }

        incoming_reply = {
            "message_id": "<reply-multi@example.test>",
            "subject": "Re: Project Architecture Alignment",
            "from": "Partner <partner@example.test>",
            "date": "2026-11-12T10:00:00Z",
            "in_reply_to": "<root@other.test>",
            # Header string with multiple space-separated IDs
            "references": "<root@other.test> <initiator-multi@myorg.test>",
            "reply_status": "needed",
        }

        with _Workspace() as ws:
            dd = ws / "data" / "mail-desk"
            _write_jsonl(dd / "sent-index.jsonl", [initiator])
            _write_jsonl(dd / "replies-needed.jsonl", [incoming_reply])

            lookup = sent_indexer.load_sent_index(dd)
            check_result = sent_indexer.check_if_replied(incoming_reply, lookup)
            self.assertTrue(
                check_result is None or not check_result.get("replied"),
                f"check_if_replied falsely matched ancestor from multi-ID references string: {check_result}",
            )

            resolve_result = sent_indexer.auto_resolve_replies_from_sent(data_dir=dd)
            self.assertEqual(0, resolve_result["resolved_count"])
            self.assertEqual(1, len(resolve_result["remaining_open"]))

    def test_multi_id_references_execute_and_auto_resolve_roundtrip(self) -> None:
        """execute mode splits multi-ID references string into clean list,

        and auto_resolve correctly avoids matching ancestors across roundtrip.
        """
        initiator_sent = {
            "message_id": "<initiator-rt@myorg.test>",
            "subject": "Contract Renewal Discussion",
            "from": "me@myorg.test",
            "to": ["partner@example.test"],
            "at": "2026-11-10T09:00:00Z",
            "sent_envelope_id": "601",
        }

        with _Workspace() as ws:
            dd = ws / "data" / "mail-desk"
            dd.mkdir(parents=True, exist_ok=True)
            idx_file = dd / "index.json"
            _write_jsonl(dd / "sent-index.jsonl", [initiator_sent])

            item = {
                "envelope_id": "88",
                "message_id": "<incoming-rt@example.test>",
                "subject": "Re: Contract Renewal Discussion",
                "from": "Partner <partner@example.test>",
                "date": "2026-11-12T14:00:00Z",
                "in_reply_to": "<root-rt@other.test>",
                # Raw multi-ID references string from himalaya/classifier
                "references": "<root-rt@other.test> <initiator-rt@myorg.test>",
                "action": {"type": "copy_as_move", "target_folder": "INBOX"},
                "decision": {"needs_reply": True},
                "notes": "Partner asks for renewal conditions",
            }

            request = approved_execute({
                "mode": "execute",
                "items": [item],
            }, account="primary")

            execute_mode.run_execute_mode(
                request,
                account="primary",
                data_dir=dd,
                index_path=idx_file,
                dependencies={
                    "BatchProgressTracker": Mock(),
                    "verify_in_target_folder": Mock(return_value="88"),
                    "run_himalaya": Mock(),
                    "load_final_index": Mock(return_value={"items": {}}),
                    "save_final_index_atomic": Mock(),
                    "flush_batch_evidence": Mock(),
                    "auto_resolve_replies_from_sent": Mock(),
                    "sleep": Mock(),
                },
            )

            replies = _read_jsonl(dd / "replies-needed.jsonl")
            self.assertEqual(1, len(replies))
            entry = replies[0]

            # 1. Verify execute persisted references as a clean list of individual IDs
            self.assertEqual(
                ["<root-rt@other.test>", "<initiator-rt@myorg.test>"],
                entry.get("references"),
                "execute must split multi-ID references string into clean list of IDs",
            )
            self.assertEqual("incoming-rt@example.test", entry.get("message_id"))

            # 2. Verify auto_resolve does NOT resolve the open case against the initiator
            resolve_result = sent_indexer.auto_resolve_replies_from_sent(data_dir=dd)
            self.assertEqual(0, resolve_result["resolved_count"])
            self.assertEqual(1, len(resolve_result["remaining_open"]))

            # 3. Add genuine subsequent reply sent after incoming email -> must resolve
            actual_reply = {
                "message_id": "<actual-reply-rt@myorg.test>",
                "subject": "Re: Contract Renewal Discussion",
                "from": "me@myorg.test",
                "to": ["partner@example.test"],
                "at": "2026-11-12T16:00:00Z",  # 2 hours after incoming
                "sent_envelope_id": "602",
            }
            _write_jsonl(dd / "sent-index.jsonl", [initiator_sent, actual_reply])

            resolve_result_2 = sent_indexer.auto_resolve_replies_from_sent(data_dir=dd)
            self.assertEqual(1, resolve_result_2["resolved_count"])
            self.assertEqual(0, len(resolve_result_2["remaining_open"]))


if __name__ == "__main__":
    unittest.main()
