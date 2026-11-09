"""#88 Sent-Items synthesis handoff contracts (hermetic, offline).

Covers the deterministic bundle part: new-item detection with Cc, the atomic,
bounded, idempotent ``new-sent-items.json`` handoff written BEFORE the sent-index
grows, the consumption ledger, thread anchors, fail-closed refusals and the three
crash windows (handoff written/index not, index written/handoff unconsumed, rerun).
No mailbox, network or consumer access.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

from core import himalaya, sent_handoff, sent_indexer  # noqa: E402
from core.modes import sync_sent as sync_sent_mode  # noqa: E402
from core.sent_handoff import (  # noqa: E402
    HANDOFF_FILENAME,
    LEDGER_FILENAME,
    SentHandoffError,
    SentSyncPartialFailure,
)

DAY = "2026-09-24"
HEAD_ENTRY_KEYS = {
    "schema_version", "at", "updated_at", "mailbox", "message_id", "in_reply_to", "references",
    "subject", "from", "to", "folder", "sent_envelope_id", "backend_locator", "note",
}


def _details(n: int, **over) -> dict:
    base = {
        "message_id": f"m{n}@example.test",
        "raw_message_id": f"<m{n}@example.test>",
        "in_reply_to": "",
        "references": "",
        "subject": f"Subject {n}",
        "from": "Desk Owner <owner@example.test>",
        "to": "partner@example.test",
        "cc": "",
        "date": f"Thu, 24 Sep 2026 {10 + n:02d}:00:00 +0200",
    }
    base.update(over)
    return base


class _Mailbox:
    """Fake Sent folder: ``envelopes`` of DAY, ``details`` by envelope id."""

    def __init__(self, *details: dict) -> None:
        self.details = {str(100 + i): d for i, d in enumerate(details, start=1)}
        self.fail_dates: set[str] = set()

    def run(self, args, account=None, timeout=30, max_retries=5):
        date = next(a[len("date "):] for a in args if a.startswith("date "))
        if date in self.fail_dates:
            raise RuntimeError("himalaya timeout")
        if date != DAY:
            return "[]"
        return json.dumps([{"id": eid, "subject": d["subject"]} for eid, d in self.details.items()])

    def read(self, eid, *args, **kwargs):
        return dict(self.details[str(eid)])

    def patches(self):
        return (
            patch.object(sent_indexer, "run_himalaya", side_effect=self.run),
            patch.object(sent_indexer, "get_single_email_details", side_effect=self.read),
        )


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dd = Path(tmp.name) / "data" / "mail-desk"
        self.dd.mkdir(parents=True)

    def sync(self, mailbox: _Mailbox) -> dict:
        run, read = mailbox.patches()
        with run, read:
            return sent_indexer.sync_sent_items(dates=[DAY], account="ACC", data_dir=self.dd)

    def handoff_ids(self) -> list[str]:
        return [i["norm_message_id"] for i in sent_handoff.read_handoff(self.dd)]

    def index_ids(self) -> list[str]:
        path = self.dd / "sent-index.jsonl"
        if not path.exists():
            return []
        return [sent_handoff.norm_id(json.loads(l)["message_id"]) for l in path.read_text("utf-8").splitlines() if l.strip()]

    def consume(self, *ids: str, vorgang_ref: str | None = None) -> None:
        items = [i for i in sent_handoff.read_handoff(self.dd) if i["norm_message_id"] in ids]
        sent_handoff.record_synthesized(self.dd, items, vorgang_ref=vorgang_ref)


class NewItemDetectionTests(_Base):
    def test_zero_new_items_writes_no_handoff(self) -> None:
        result = self.sync(_Mailbox())
        self.assertEqual((0, []), (result["new_count"], result["new_items"]))
        self.assertEqual("none", result["handoff"]["state"])
        self.assertFalse((self.dd / HANDOFF_FILENAME).exists())

    def test_one_new_item_carries_all_fields_and_correct_cc(self) -> None:
        mailbox = _Mailbox(_details(
            1,
            to="Partner <partner@example.test>",
            cc='"Doe, John" <john@example.test>, second@example.test',
            in_reply_to="<mid@example.test>",
            references="<root@example.test> <mid@example.test>",
        ))
        result = self.sync(mailbox)

        self.assertEqual(1, result["new_count"])
        self.assertEqual(result["new_count"], result["new_entries_indexed"])
        item = result["new_items"][0]
        self.assertEqual("<m1@example.test>", item["message_id"])
        self.assertEqual("Subject 1", item["subject"])
        self.assertEqual(["Partner <partner@example.test>"], item["to"])
        self.assertEqual(["Doe, John <john@example.test>", "second@example.test"], item["cc"])
        self.assertEqual("<mid@example.test>", item["in_reply_to"])
        self.assertEqual(["<root@example.test>", "<mid@example.test>"], item["references"])
        self.assertEqual("101", item["envelope_id"])
        self.assertTrue(item["date"])
        self.assertEqual("root@example.test", item["thread_key"])
        self.assertEqual(["mid@example.test", "root@example.test"], item["anchor_candidates"])
        # Handoff file equals the (unflagged) envelope items; index shape is unchanged.
        stored = sent_handoff.read_handoff(self.dd)
        self.assertEqual([{k: v for k, v in item.items() if k != "already_synthesized"}], stored)
        entry = json.loads((self.dd / "sent-index.jsonl").read_text("utf-8"))
        self.assertEqual(HEAD_ENTRY_KEYS, set(entry))

    def test_n_new_items_with_existing_entries_have_no_false_positives(self) -> None:
        mailbox = _Mailbox(_details(1), _details(2), _details(3))
        # Message 1 is already indexed (same envelope id and Message-ID).
        seed = {"schema_version": 1, "at": "2026-09-24", "message_id": "<m1@example.test>",
                "sent_envelope_id": "101", "subject": "Subject 1", "to": [], "references": []}
        (self.dd / "sent-index.jsonl").write_text(json.dumps(seed) + "\n", encoding="utf-8")

        result = self.sync(mailbox)

        self.assertEqual(2, result["new_count"])
        self.assertEqual(["m2@example.test", "m3@example.test"], [i["norm_message_id"] for i in result["new_items"]])
        self.assertEqual(["m2@example.test", "m3@example.test"], self.handoff_ids())

    def test_incomplete_header_read_is_flagged_not_hidden(self) -> None:
        result = self.sync(_Mailbox(_details(1, headers_complete=False)))
        self.assertFalse(result["new_items"][0]["headers_complete"])
        self.assertEqual(1, result["handoff"]["headers_incomplete_count"])


class IdempotenceAndCrashWindowTests(_Base):
    def test_repeated_run_creates_no_duplicates(self) -> None:
        mailbox = _Mailbox(_details(1), _details(2))
        self.sync(mailbox)
        before = (self.dd / HANDOFF_FILENAME).read_bytes()

        again = self.sync(mailbox)

        self.assertEqual(0, again["new_count"])
        self.assertEqual("pending", again["handoff"]["state"])
        self.assertEqual(2, again["handoff"]["pending_count"])
        self.assertEqual(before, (self.dd / HANDOFF_FILENAME).read_bytes())
        self.assertEqual(["m1@example.test", "m2@example.test"], self.index_ids())

    def test_crash_between_handoff_and_index_recovers_idempotently(self) -> None:
        mailbox = _Mailbox(_details(1), _details(2))
        with patch.object(sent_indexer, "append_sent_index_entries", side_effect=OSError("disk gone")):
            with self.assertRaises(RuntimeError) as caught:
                self.sync(mailbox)
        self.assertIn("handoff is retained", str(caught.exception))
        # Handoff is durable, the index did not grow.
        self.assertEqual(["m1@example.test", "m2@example.test"], self.handoff_ids())
        self.assertEqual([], self.index_ids())

        rerun = self.sync(mailbox)

        self.assertEqual(2, rerun["new_count"])
        self.assertEqual(0, rerun["handoff"]["written_count"])  # merged by Message-ID, no rewrite
        self.assertEqual(["m1@example.test", "m2@example.test"], self.handoff_ids())
        self.assertEqual(["m1@example.test", "m2@example.test"], self.index_ids())

    def test_crash_with_partially_appended_index_recovers_idempotently(self) -> None:
        mailbox = _Mailbox(_details(1), _details(2))
        self.sync(mailbox)
        # Simulate a torn append: only the first index line survived.
        path = self.dd / "sent-index.jsonl"
        first = path.read_text("utf-8").splitlines()[0]
        path.write_text(first + "\n", encoding="utf-8")

        rerun = self.sync(mailbox)

        self.assertEqual(["m2@example.test"], [i["norm_message_id"] for i in rerun["new_items"]])
        self.assertEqual(["m1@example.test", "m2@example.test"], self.handoff_ids())
        self.assertEqual(["m1@example.test", "m2@example.test"], self.index_ids())

    def test_handoff_write_failure_leaves_index_untouched(self) -> None:
        mailbox = _Mailbox(_details(1))
        with patch.object(sent_indexer, "write_handoff", side_effect=OSError("no space")):
            with self.assertRaises(OSError):
                self.sync(mailbox)
        self.assertEqual([], self.index_ids())
        self.assertFalse((self.dd / HANDOFF_FILENAME).exists())
        self.assertEqual(1, self.sync(mailbox)["new_count"])

    def test_crash_after_index_before_consumption_blocks_new_items_fail_closed(self) -> None:
        self.sync(_Mailbox(_details(1)))
        before = (self.dd / HANDOFF_FILENAME).read_bytes()
        index_before = (self.dd / "sent-index.jsonl").read_bytes()
        newer = _Mailbox(_details(1), _details(2))

        run, read = newer.patches()
        with run, read, self.assertRaises(SentHandoffError) as caught:
            sent_indexer.sync_sent_items(dates=[DAY], account="ACC", data_dir=self.dd)

        self.assertEqual("handoff_pending", caught.exception.reason_code)
        self.assertEqual(before, (self.dd / HANDOFF_FILENAME).read_bytes())
        self.assertEqual(index_before, (self.dd / "sent-index.jsonl").read_bytes())

        # Consumption (ledger) releases the block; the new item is then handed off.
        self.consume("m1@example.test")
        released = self.sync(newer)
        self.assertEqual(["m2@example.test"], [i["norm_message_id"] for i in released["new_items"]])
        self.assertEqual(["m2@example.test"], self.handoff_ids())
        self.assertEqual(1, released["handoff"]["consumed_pruned"])

    def test_invalid_handoff_is_never_overwritten(self) -> None:
        (self.dd / HANDOFF_FILENAME).write_text("{not json", encoding="utf-8")
        with self.assertRaises(SentHandoffError) as caught:
            self.sync(_Mailbox(_details(1)))
        self.assertEqual("handoff_invalid", caught.exception.reason_code)
        self.assertEqual("{not json", (self.dd / HANDOFF_FILENAME).read_text("utf-8"))
        self.assertEqual([], self.index_ids())

    def test_atomic_write_keeps_previous_handoff_when_replace_fails(self) -> None:
        self.sync(_Mailbox(_details(1)))
        before = (self.dd / HANDOFF_FILENAME).read_bytes()
        with patch("core.common.replace_with_retry", side_effect=OSError("blocked")):
            with self.assertRaises(OSError):
                sent_handoff.write_handoff(self.dd, [])
        self.assertEqual(before, (self.dd / HANDOFF_FILENAME).read_bytes())
        self.assertEqual([], [p.name for p in self.dd.iterdir() if p.name.endswith(".tmp")])

    def test_torn_index_line_is_repaired_before_append(self) -> None:
        path = self.dd / "sent-index.jsonl"
        path.write_text('{"message_id": "<torn@example.test>"', encoding="utf-8")  # no newline
        self.sync(_Mailbox(_details(1)))
        lines = [l for l in path.read_text("utf-8").splitlines() if l.strip()]
        self.assertEqual(2, len(lines))
        self.assertEqual("<m1@example.test>", json.loads(lines[1])["message_id"])


class BoundTests(_Base):
    def test_overflow_defers_surplus_without_loss(self) -> None:
        mailbox = _Mailbox(*(_details(n) for n in range(1, 6)))
        with patch.object(sent_indexer, "MAX_HANDOFF_ITEMS", 3), patch.object(sent_handoff, "MAX_HANDOFF_ITEMS", 3):
            first = self.sync(mailbox)
            self.assertEqual(3, first["new_count"])
            self.assertEqual(2, first["handoff"]["deferred_count"])
            self.assertIn("deferred", first["follow_up_hint"])
            self.assertEqual(["m1@example.test", "m2@example.test", "m3@example.test"], self.index_ids())
            # Pending handoff blocks the surplus (fail-closed) until consumed.
            with self.assertRaises(SentHandoffError):
                self.sync(mailbox)
            self.consume("m1@example.test", "m2@example.test", "m3@example.test")
            second = self.sync(mailbox)
        self.assertEqual(["m4@example.test", "m5@example.test"], [i["norm_message_id"] for i in second["new_items"]])
        self.assertEqual(["m4@example.test", "m5@example.test"], self.handoff_ids())
        self.assertEqual(0, second["handoff"]["deferred_count"])

    def test_item_fields_are_bounded_and_marked(self) -> None:
        refs = " ".join(f"<r{i}@example.test>" for i in range(80))
        result = self.sync(_Mailbox(_details(1, references=refs, subject="s" * 1500)))
        item = result["new_items"][0]
        self.assertEqual(sent_handoff.MAX_REFERENCES, len(item["references"]))
        self.assertEqual("<r0@example.test>", item["references"][0])
        self.assertEqual("<r79@example.test>", item["references"][-1])
        self.assertEqual(sent_handoff.MAX_SUBJECT_CHARS, len(item["subject"]))
        self.assertEqual(["references", "subject"], item["truncated_fields"])


class LedgerAndThreadTests(_Base):
    def test_synthesized_message_is_indexed_but_not_handed_off_again(self) -> None:
        sent_handoff.record_synthesized(self.dd, [{"message_id": "<m1@example.test>"}], vorgang_ref="V-1")
        result = self.sync(_Mailbox(_details(1), _details(2)))
        flags = {i["norm_message_id"]: i["already_synthesized"] for i in result["new_items"]}
        self.assertEqual({"m1@example.test": True, "m2@example.test": False}, flags)
        self.assertEqual(["m2@example.test"], self.handoff_ids())
        self.assertEqual(1, result["handoff"]["already_synthesized_skipped"])
        self.assertEqual(["m1@example.test", "m2@example.test"], self.index_ids())

    def test_reply_in_same_thread_is_anchored_to_existing_vorgang(self) -> None:
        sent_handoff.record_synthesized(self.dd, [{"message_id": "<root@example.test>"}], vorgang_ref="V-7")
        mailbox = _Mailbox(
            _details(1, in_reply_to="<root@example.test>", references="<root@example.test>"),
            _details(2, in_reply_to="<other@example.test>", references="<root@example.test> <other@example.test>"),
            _details(3),
        )
        items = {i["norm_message_id"]: i for i in self.sync(mailbox)["new_items"]}
        self.assertEqual(
            {"vorgang_ref": "V-7", "matched_message_id": "root@example.test", "match_type": "in_reply_to"},
            items["m1@example.test"]["anchored_to"],
        )
        self.assertEqual("references", items["m2@example.test"]["anchored_to"]["match_type"])
        self.assertIsNone(items["m3@example.test"]["anchored_to"])

    def test_thread_key_anchor_and_follow_up_role(self) -> None:
        sent_handoff.record_synthesized(
            self.dd, [{"message_id": "<a@example.test>", "thread_key": "root@example.test"}], vorgang_ref="V-9"
        )
        item = {"norm_message_id": "b@example.test", "thread_key": "root@example.test", "anchor_candidates": []}
        anchor = sent_handoff.resolve_thread_anchor(item, sent_handoff.load_ledger(self.dd))
        self.assertEqual(("V-9", "thread_key"), (anchor["vorgang_ref"], anchor["match_type"]))
        sent_handoff.record_synthesized(self.dd, [{**item, "anchored_to": anchor}])
        record = sent_handoff.load_ledger(self.dd)["by_message_id"]["b@example.test"]
        self.assertEqual(("follow_up", "V-9", "a@example.test"), (record["role"], record["vorgang_ref"], record["anchored_to"]))

    def test_record_synthesized_is_idempotent_and_repairs_torn_ledger(self) -> None:
        item = {"message_id": "<x@example.test>"}
        self.assertEqual(1, sent_handoff.record_synthesized(self.dd, [item, item]))
        self.assertEqual(0, sent_handoff.record_synthesized(self.dd, [item]))
        path = self.dd / LEDGER_FILENAME
        path.write_text(path.read_text("utf-8") + '{"schema_ver', encoding="utf-8")  # torn tail
        self.assertEqual(1, sent_handoff.record_synthesized(self.dd, [{"message_id": "<y@example.test>"}]))
        ledger = sent_handoff.load_ledger(self.dd)
        self.assertEqual({"x@example.test", "y@example.test"}, set(ledger["by_message_id"]))
        self.assertEqual(1, ledger["invalid_lines"])
        with self.assertRaises(ValueError):
            sent_handoff.record_synthesized(self.dd, [{"subject": "no id"}])


class ModeEnvelopeTests(_Base):
    def run_mode(self, mailbox: _Mailbox) -> dict:
        run, read = mailbox.patches()
        with run, read, patch.object(sent_indexer, "datetime", _frozen_datetime()):
            return sync_sent_mode.run_sync_sent_mode(
                {"mode": "sync_sent", "count": 3, "folder": "Sent Items"}, account="ACC", data_dir=self.dd
            )

    def test_success_envelope_has_new_count_items_and_handoff(self) -> None:
        envelope = self.run_mode(_Mailbox(_details(1, cc="c@example.test")))
        self.assertTrue(envelope["ok"])
        self.assertEqual(1, envelope["new_count"])
        self.assertEqual(["c@example.test"], envelope["new_items"][0]["cc"])
        self.assertEqual("written", envelope["handoff"]["state"])
        self.assertEqual(str(self.dd / HANDOFF_FILENAME), envelope["handoff"]["file"])
        json.dumps(envelope)

    def test_pending_handoff_is_a_failed_envelope_with_reason_code(self) -> None:
        self.run_mode(_Mailbox(_details(1)))
        envelope = self.run_mode(_Mailbox(_details(1), _details(2)))
        self.assertFalse(envelope["ok"])
        self.assertEqual("handoff_pending", envelope["reason_code"])
        self.assertEqual((0, []), (envelope["new_count"], envelope["new_items"]))
        self.assertEqual(1, envelope["handoff"]["pending_count"])
        self.assertEqual(["m1@example.test"], self.index_ids())

    def test_partial_date_failure_commits_successes_and_fails(self) -> None:
        mailbox = _Mailbox(_details(1))
        mailbox.fail_dates = {"2026-09-25"}
        envelope = self.run_mode(mailbox)
        self.assertFalse(envelope["ok"])
        self.assertEqual("sync_partial_failure", envelope["reason_code"])
        self.assertEqual(["2026-09-25"], envelope["failed_dates"])
        self.assertEqual(1, envelope["new_count"])
        self.assertEqual(["m1@example.test"], self.handoff_ids())

        run, read = mailbox.patches()
        with run, read, self.assertRaises(SentSyncPartialFailure):
            sent_indexer.sync_sent_items(dates=[DAY], account="ACC", data_dir=self.dd)

    def test_runner_envelope_exit_semantics(self) -> None:
        import mail_desk_batch_runner as runner

        ok = runner._result_envelope(self.run_mode(_Mailbox(_details(1))), "sync_sent")
        self.assertTrue(ok["success"])
        self.assertEqual(1, ok["data"]["new_count"])
        refused = runner._result_envelope(self.run_mode(_Mailbox(_details(1), _details(2))), "sync_sent")
        self.assertFalse(refused["success"])
        self.assertEqual("handoff_pending", refused["data"]["reason_code"])


class CliSummaryTests(unittest.TestCase):
    def test_summary_surfaces_counts_not_items(self) -> None:
        import argparse
        import subprocess

        import mail_desk_batch_cli as cli

        with tempfile.TemporaryDirectory() as tmp:
            envelope_path = Path(tmp) / "e.json"
            envelope_path.write_text(json.dumps({
                "success": False, "state": "Failed", "message": "pending",
                "data": {
                    "new_count": 0, "reason_code": "handoff_pending", "new_items": [{"subject": "x"}],
                    "handoff": {"state": "handoff_pending", "pending_count": 2, "file": "f", "ignored": 1},
                },
            }), encoding="utf-8")
            summary = cli._summary(
                argparse.Namespace(mode="sync-sent"), envelope_path, Path(tmp) / "p.log",
                subprocess.CompletedProcess([], 1),
            )
        self.assertEqual("handoff_pending", summary["reason_code"])
        self.assertEqual({"state": "handoff_pending", "pending_count": 2, "file": "f"}, summary["handoff"])
        self.assertNotIn("new_items", summary)


class HimalayaCcTests(unittest.TestCase):
    def test_cc_is_returned_from_header_read(self) -> None:
        stdout = (
            "Message-Id: <a@example.test>\nIn-Reply-To: <b@example.test>\nReferences: <b@example.test>\n"
            "From: me@example.test\nTo: you@example.test\nCc: \"Doe, John\" <j@example.test>,\n"
            " k@example.test\nDate: Thu, 24 Sep 2026 10:00:00 +0200\nSubject: Hi\n\nbody\n"
        )
        with patch.object(himalaya, "run_himalaya", return_value=stdout):
            details = himalaya.get_single_email_details(
                "1", "Sent Items", account="ACC", inspect_attachments=False
            )
        self.assertEqual('"Doe, John" <j@example.test>, k@example.test', details["cc"])
        self.assertTrue(details["headers_complete"])
        self.assertEqual(["Doe, John <j@example.test>", "k@example.test"],
                         sent_handoff.parse_address_list(details["cc"]))

    def test_unreadable_message_reports_incomplete_headers(self) -> None:
        with patch.object(himalaya, "run_himalaya", side_effect=RuntimeError("boom")):
            details = himalaya.get_single_email_details(
                "1", "Sent Items", account="ACC", inspect_attachments=False
            )
        self.assertEqual(("", False), (details["cc"], details["headers_complete"]))


def _frozen_datetime():
    from datetime import datetime as real

    class _Frozen(real):
        @classmethod
        def now(cls):
            return real(2026, 9, 24, 12, 0, 0)

    return _Frozen


if __name__ == "__main__":
    unittest.main()
