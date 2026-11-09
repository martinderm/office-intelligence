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
    SentHandoffPublishError,
    SentIndexAppendError,
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
        self.fail_pages: dict[str, set[int]] = {}
        self.repeat_pages = False
        self.out_of_bound_pages = False
        self.transport_error_pages = False
        self.calls = 0

    def run(self, args, account=None, timeout=30, max_retries=5):
        self.calls += 1
        date = next(a[len("date "):] for a in args if a.startswith("date "))
        page = int(args[args.index("--page") + 1]) if "--page" in args else 1
        size = int(args[args.index("-s") + 1])
        if date in self.fail_dates or page in self.fail_pages.get(date, set()):
            raise RuntimeError("himalaya timeout")
        if self.transport_error_pages and page > 1:
            raise himalaya.HimalayaInvocationError(
                "himalaya_command_failed",
                "Himalaya failed: Error: transport error: buffer index out of bounds",
            )
        if self.out_of_bound_pages and page > 1:
            raise himalaya.HimalayaInvocationError(
                "himalaya_command_failed", "Himalaya failed: Error: envelope page out of bound"
            )
        # A detail may carry ``_day`` to live on another day than DAY.
        items = [
            {"id": eid, "subject": d["subject"]}
            for eid, d in self.details.items() if d.get("_day", DAY) == date
        ]
        start = (page - 1) * size
        if self.repeat_pages:
            start = 0  # unstable adapter: every page repeats page 1
        return json.dumps(items[start:start + size])

    def read(self, eid, *args, **kwargs):
        detail = self.details[str(eid)]
        if detail.get("_raise"):
            raise RuntimeError(str(detail["_raise"]))
        return {k: v for k, v in detail.items() if not k.startswith("_")}

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
            with self.assertRaises(SentHandoffPublishError) as caught:
                self.sync(mailbox)
        self.assertIsInstance(caught.exception.__cause__, OSError)
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
        self.assertEqual(1, released["handoff"]["consumed_ignored"])

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
        # The unparsable fragment is preserved in the sidecar, never glued or destroyed.
        self.assertEqual(["<m1@example.test>"], [json.loads(l)["message_id"] for l in lines])
        self.assertEqual(
            '{"message_id": "<torn@example.test>"\n',
            (self.dd / "sent-index.jsonl.torn").read_text("utf-8"),
        )


class BoundTests(_Base):
    def test_over_bound_batches_the_oldest_and_defers_the_rest(self) -> None:
        mailbox = _Mailbox(*(_details(n) for n in range(1, 6)))
        with patch.object(sent_indexer, "MAX_HANDOFF_ITEMS", 3), patch.object(sent_handoff, "MAX_HANDOFF_ITEMS", 3):
            first = self.sync(mailbox)
        self.assertEqual(3, first["new_count"])
        self.assertTrue(first["truncated"])
        self.assertEqual(2, first["handoff"]["deferred_count"])
        self.assertEqual(["m1@example.test", "m2@example.test", "m3@example.test"], self.index_ids())

        self.consume("m1@example.test", "m2@example.test", "m3@example.test")
        second = self.sync(mailbox)
        self.assertEqual(2, second["new_count"])
        self.assertFalse(second["truncated"])
        self.assertEqual(["m4@example.test", "m5@example.test"], self.handoff_ids())
        self.assertEqual(["m1@example.test", "m2@example.test", "m3@example.test",
                          "m4@example.test", "m5@example.test"], self.index_ids())

    def test_item_fields_are_bounded_and_marked(self) -> None:
        refs = " ".join(f"<r{i}@example.test>" for i in range(80))
        result = self.sync(_Mailbox(_details(1, references=refs, subject="s" * 1500)))
        item = result["new_items"][0]
        self.assertEqual(sent_handoff.MAX_REFERENCES, len(item["references"]))
        self.assertEqual("<r0@example.test>", item["references"][0])
        self.assertEqual("<r79@example.test>", item["references"][-1])
        self.assertEqual(sent_handoff.MAX_SUBJECT_CHARS, len(item["subject"]))
        self.assertEqual(["references", "subject"], item["truncated_fields"])
        # A producer-bounded item is exactly at the documented limits and passes the
        # read-side validation (the same limits are enforced when consuming).
        self.assertEqual(
            [{k: v for k, v in item.items() if k != "already_synthesized"}],
            sent_handoff.read_handoff(self.dd),
        )


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

    def test_first_duplicate_ledger_line_is_authoritative_for_every_index(self) -> None:
        def line(message_id: str, thread: str, ref: str) -> str:
            return json.dumps(_ledger_line(
                message_id=message_id, thread_key=thread, vorgang_ref=ref,
            ))

        (self.dd / LEDGER_FILENAME).write_text(
            line("a@example.test", "root-1@example.test", "V-1")
            + "\n"
            + line("a@example.test", "root-2@example.test", "V-2")
            + "\n",
            encoding="utf-8",
        )
        ledger = sent_handoff.load_ledger(self.dd)
        self.assertEqual("V-1", ledger["by_message_id"]["a@example.test"]["vorgang_ref"])
        reply = {
            "norm_message_id": "b@example.test",
            "in_reply_to": "",
            "references": [],
            "thread_key": "root-1@example.test",
            "anchor_candidates": [],
        }
        anchor = sent_handoff.resolve_thread_anchor(reply, ledger)
        self.assertEqual(("V-1", "a@example.test", "thread_key"), (
            anchor["vorgang_ref"], anchor["matched_message_id"], anchor["match_type"],
        ))
        # The conflicting duplicate must not introduce the second thread anchor.
        conflicting = {**reply, "norm_message_id": "c@example.test", "thread_key": "root-2@example.test"}
        self.assertIsNone(sent_handoff.resolve_thread_anchor(conflicting, ledger))

        sent_handoff.record_synthesized(self.dd, [conflicting])
        recorded = sent_handoff.load_ledger(self.dd)["by_message_id"]["c@example.test"]
        self.assertEqual(("primary", None), (recorded["role"], recorded["vorgang_ref"]))

    def test_thread_index_keeps_a_later_vorgang_of_the_same_thread(self) -> None:
        def line(message_id: str, thread: str, ref: str | None) -> str:
            return json.dumps(_ledger_line(
                message_id=message_id, thread_key=thread, vorgang_ref=ref,
            ))

        (self.dd / LEDGER_FILENAME).write_text(
            line("a@example.test", "root@example.test", None)
            + "\n"
            + line("b@example.test", "root@example.test", "V-7")
            + "\n",
            encoding="utf-8",
        )
        ledger = sent_handoff.load_ledger(self.dd)
        follow_up = {
            "norm_message_id": "c@example.test",
            "in_reply_to": "",
            "references": [],
            "thread_key": "root@example.test",
            "anchor_candidates": [],
        }
        anchor = sent_handoff.resolve_thread_anchor(follow_up, ledger)
        self.assertEqual(("V-7", "b@example.test", "thread_key"), (
            anchor["vorgang_ref"], anchor["matched_message_id"], anchor["match_type"],
        ))
        sent_handoff.record_synthesized(self.dd, [follow_up])
        record = sent_handoff.load_ledger(self.dd)["by_message_id"]["c@example.test"]
        self.assertEqual(("follow_up", "V-7", "b@example.test"), (
            record["role"], record["vorgang_ref"], record["anchored_to"],
        ))

    def test_record_synthesized_is_idempotent_and_repairs_torn_ledger(self) -> None:
        item = {"message_id": "<x@example.test>"}
        self.assertEqual(1, sent_handoff.record_synthesized(self.dd, [item, item]))
        self.assertEqual(0, sent_handoff.record_synthesized(self.dd, [item]))
        path = self.dd / LEDGER_FILENAME
        path.write_text(path.read_text("utf-8") + '{"schema_ver', encoding="utf-8")  # torn tail
        self.assertTrue(sent_handoff.load_ledger(self.dd)["torn_tail"])
        self.assertEqual(1, sent_handoff.record_synthesized(self.dd, [{"message_id": "<y@example.test>"}]))
        ledger = sent_handoff.load_ledger(self.dd)
        self.assertEqual({"x@example.test", "y@example.test"}, set(ledger["by_message_id"]))
        self.assertFalse(ledger["torn_tail"])
        self.assertEqual('{"schema_ver\n', (self.dd / (LEDGER_FILENAME + ".torn")).read_text("utf-8"))
        with self.assertRaises(ValueError):
            sent_handoff.record_synthesized(self.dd, [{"subject": "no id"}])


def _ledger_line(**over) -> dict:
    base = {
        "schema_version": 1, "message_id": "x@example.test", "thread_key": "x@example.test",
        "vorgang_ref": "V-1", "role": "primary", "anchored_to": None, "recorded_at": "2026-11-09T09:30:00Z",
    }
    base.update(over)
    return base


class MultiDateAndOverflowTests(_Base):
    def test_sync_by_dates_commits_once_across_days(self) -> None:
        mailbox = _Mailbox(_details(1), _details(2, _day="2026-09-25"))
        run, read = mailbox.patches()
        with run, read:
            added = sent_indexer.sync_sent_items_by_dates([DAY], account="ACC", data_dir=self.dd)
            again = sent_indexer.sync_sent_items_by_dates([DAY], account="ACC", data_dir=self.dd)
        self.assertEqual((2, 0), (added, again))
        self.assertEqual(["m1@example.test", "m2@example.test"], self.handoff_ids())
        self.assertEqual(["m1@example.test", "m2@example.test"], self.index_ids())

    def test_failing_window_aborts_before_any_write_and_rerun_recovers(self) -> None:
        # DAY: A reads fine, B's detail read fails; a later window serves A again.
        # Fail-closed: no partial commit, no ID leak; the rerun sees both mails.
        mailbox = _Mailbox(_details(1), _details(2))
        mailbox.details["102"]["_raise"] = "detail read failed"
        mailbox.details["201"] = _details(
            9, _day="2026-09-25", message_id="m1@example.test", raw_message_id="<m1@example.test>",
        )
        run, read = mailbox.patches()
        with run, read, self.assertRaises(SentSyncPartialFailure) as caught:
            sent_indexer.sync_sent_items(dates=[DAY], account="ACC", data_dir=self.dd)

        self.assertEqual([DAY], caught.exception.failed_dates)
        self.assertEqual(0, caught.exception.result["new_count"])
        self.assertEqual([], caught.exception.result["new_items"])
        self.assertFalse((self.dd / HANDOFF_FILENAME).exists())
        self.assertEqual([], self.index_ids())

        mailbox.details["102"].pop("_raise")  # window fixed
        rerun = self.sync(mailbox)
        self.assertEqual(2, rerun["new_count"])
        self.assertEqual(["m1@example.test", "m2@example.test"], self.index_ids())
        self.assertEqual(["m1@example.test", "m2@example.test"], self.handoff_ids())

    def test_sync_by_dates_failing_day_writes_nothing(self) -> None:
        mailbox = _Mailbox(_details(1))
        mailbox.fail_dates = {"2026-09-25"}
        run, read = mailbox.patches()
        with run, read, self.assertRaises(RuntimeError):
            sent_indexer.sync_sent_items_by_dates([DAY], account="ACC", data_dir=self.dd)
        self.assertEqual([], self.index_ids())
        self.assertFalse((self.dd / HANDOFF_FILENAME).exists())

    def test_window_failure_recovery_reconvers_the_full_original_window(self) -> None:
        mailbox = _Mailbox(_details(1, _day="2026-09-24"), _details(2, _day="2026-09-25"))
        mailbox.fail_dates = {"2026-09-25"}
        run, read = mailbox.patches()
        with run, read, patch.object(sent_indexer, "datetime", _frozen_datetime((2026, 9, 26))):
            with self.assertRaises(SentSyncPartialFailure) as caught:
                sent_indexer.sync_sent_items(account="ACC", data_dir=self.dd)
            self.assertIn("start=2026-07-29 end=2026-09-26", str(caught.exception))
            self.assertIn("sync_sent_items(since='2026-07-29')", str(caught.exception))
            self.assertEqual([], self.index_ids())

            mailbox.fail_dates = set()  # fixed: the default run re-covers the full window
            retry = sent_indexer.sync_sent_items(account="ACC", data_dir=self.dd)
        self.assertEqual(["m1@example.test", "m2@example.test"], [i["norm_message_id"] for i in retry["new_items"]])
        self.assertEqual(["m1@example.test", "m2@example.test"], self.index_ids())

    def test_empty_index_first_day_failure_is_retried_on_the_next_day(self) -> None:
        mailbox = _Mailbox(_details(1))
        mailbox.fail_dates = {DAY}
        run, read = mailbox.patches()
        with run, read, patch.object(sent_indexer, "datetime", _frozen_datetime((2026, 9, 24))):
            with self.assertRaises(SentSyncPartialFailure) as caught:
                sent_indexer.sync_sent_items(account="ACC", data_dir=self.dd)
        self.assertEqual([DAY], caught.exception.failed_dates)
        self.assertEqual([], self.index_ids())
        self.assertFalse((self.dd / HANDOFF_FILENAME).exists())

        mailbox.fail_dates = set()  # day D fixed; retry on D+1 must still include D
        run, read = mailbox.patches()
        with run, read, patch.object(sent_indexer, "datetime", _frozen_datetime((2026, 9, 25))):
            retry = sent_indexer.sync_sent_items(account="ACC", data_dir=self.dd)
        self.assertEqual(["m1@example.test"], [i["norm_message_id"] for i in retry["new_items"]])
        self.assertEqual(["m1@example.test"], self.index_ids())

    def test_item_budget_stops_at_a_day_boundary_and_names_recovery(self) -> None:
        seed = {"schema_version": 1, "at": "2026-09-21", "message_id": "<seed@example.test>",
                "sent_envelope_id": "900", "subject": "seed", "to": [], "references": []}
        (self.dd / "sent-index.jsonl").write_text(json.dumps(seed) + "\n", encoding="utf-8")
        mailbox = _Mailbox(
            _details(1, _day="2026-09-22"),
            _details(2, _day="2026-09-23"),
            _details(3, _day="2026-09-24"),
        )
        run, read = mailbox.patches()
        with run, read, patch.object(sent_indexer, "datetime", _frozen_datetime((2026, 9, 24))):
            result = sent_indexer.sync_sent_items(count=1, account="ACC", data_dir=self.dd)
            self.assertTrue(result["truncated"])
            self.assertEqual(("2026-09-22", "2026-09-23"), (
                result["coverage_end"], result["next_start"],
            ))
            self.assertIn("sync_sent_items_by_dates(dates=['2026-09-23'], include_next_day=False)",
                          result["follow_up_hint"])
            self.assertEqual(["seed@example.test", "m1@example.test"], self.index_ids())
            self.assertEqual(["m1@example.test"], self.handoff_ids())

            # The named recovery call actually covers the blocked day after consuming.
            self.consume("m1@example.test")
            sent_indexer.sync_sent_items_by_dates(
                ["2026-09-23"], account="ACC", data_dir=self.dd, include_next_day=False,
            )
        self.assertEqual(["seed@example.test", "m1@example.test", "m2@example.test"], self.index_ids())
        self.assertEqual(["m2@example.test"], self.handoff_ids())

    def test_item_budget_stops_before_a_day_that_exceeds_it_with_reachable_recovery(self) -> None:
        seed = {"schema_version": 1, "at": "2026-09-21", "message_id": "<seed@example.test>",
                "sent_envelope_id": "900", "subject": "seed", "to": [], "references": []}
        (self.dd / "sent-index.jsonl").write_text(json.dumps(seed) + "\n", encoding="utf-8")
        mailbox = _Mailbox(_details(1, _day="2026-09-22"), _details(2, _day="2026-09-22"))
        run, read = mailbox.patches()
        with run, read, patch.object(sent_indexer, "datetime", _frozen_datetime((2026, 9, 24))):
            result = sent_indexer.sync_sent_items(count=1, account="ACC", data_dir=self.dd)
            self.assertTrue(result["truncated"])
            self.assertEqual(("2026-09-21", "2026-09-22"), (
                result["coverage_end"], result["next_start"],
            ))
            self.assertEqual(0, result["new_count"])
            self.assertIn("sync_sent_items_by_dates(dates=['2026-09-22'], include_next_day=False)",
                          result["follow_up_hint"])
            self.assertEqual(["seed@example.test"], self.index_ids())
            self.assertFalse((self.dd / HANDOFF_FILENAME).exists())

            report = sent_indexer.sync_sent_items_by_dates_report(
                ["2026-09-22"], account="ACC", data_dir=self.dd, include_next_day=False,
            )
        self.assertEqual(2, report["new_count"])
        self.assertFalse(report["truncated"])
        self.assertEqual(["seed@example.test", "m1@example.test", "m2@example.test"], self.index_ids())

    def test_empty_days_do_not_consume_the_item_budget(self) -> None:
        seed = {"schema_version": 1, "at": "2026-09-21", "message_id": "<seed@example.test>",
                "sent_envelope_id": "900", "subject": "seed", "to": [], "references": []}
        (self.dd / "sent-index.jsonl").write_text(json.dumps(seed) + "\n", encoding="utf-8")
        mailbox = _Mailbox(_details(1, _day="2026-09-24"))
        run, read = mailbox.patches()
        with run, read, patch.object(sent_indexer, "datetime", _frozen_datetime((2026, 9, 24))):
            result = sent_indexer.sync_sent_items(count=1, account="ACC", data_dir=self.dd)
        self.assertFalse(result["truncated"])
        self.assertTrue(result["coverage_complete"])
        self.assertEqual(1, result["new_count"])
        self.assertEqual(["seed@example.test", "m1@example.test"], self.index_ids())

    def test_old_watermark_is_allowed_and_covers_every_day(self) -> None:
        # 61 days old: the guard checks coverage, not age.  Empty days count and the
        # run completes instead of blocking permanently.
        seed = {"schema_version": 1, "at": "2026-07-25", "message_id": "<seed@example.test>",
                "sent_envelope_id": "900", "subject": "seed", "to": [], "references": []}
        (self.dd / "sent-index.jsonl").write_text(json.dumps(seed) + "\n", encoding="utf-8")
        mailbox = _Mailbox()  # every day is empty
        run, read = mailbox.patches()
        with run, read, patch.object(sent_indexer, "datetime", _frozen_datetime((2026, 9, 24))):
            result = sent_indexer.sync_sent_items(account="ACC", data_dir=self.dd)

        self.assertEqual(0, result["new_count"])
        self.assertTrue(result["coverage_complete"])
        self.assertFalse(result["truncated"])
        self.assertEqual(("2026-07-25", "2026-09-24"), (
            result["coverage_start"], result["coverage_end"],
        ))
        self.assertEqual(62, result["covered_days"])
        self.assertEqual(62, mailbox.calls)  # one page per covered day, all empty

    def test_initial_marker_keeps_the_oldest_day_across_a_retry_day(self) -> None:
        oldest = "2026-07-27"  # D-59 for frozen D=2026-09-24
        mailbox = _Mailbox(_details(1, _day=oldest))
        mailbox.fail_dates = {DAY}
        run, read = mailbox.patches()
        with run, read, patch.object(sent_indexer, "datetime", _frozen_datetime((2026, 9, 24))):
            with self.assertRaises(SentSyncPartialFailure):
                sent_indexer.sync_sent_items(account="ACC", data_dir=self.dd)
        self.assertTrue((self.dd / sent_indexer.INITIAL_START_FILENAME).exists())
        self.assertEqual([], self.index_ids())

        mailbox.fail_dates = set()  # retry on the next day keeps the persisted start
        run, read = mailbox.patches()
        with run, read, patch.object(sent_indexer, "datetime", _frozen_datetime((2026, 9, 25))):
            retry = sent_indexer.sync_sent_items(account="ACC", data_dir=self.dd)
        self.assertEqual(["m1@example.test"], [i["norm_message_id"] for i in retry["new_items"]])
        self.assertEqual(["m1@example.test"], self.index_ids())
        state = sent_indexer._read_sync_state(self.dd)
        self.assertEqual("2026-09-24", state["covered_through"])  # yesterday, today re-read

    def test_since_cannot_skip_forward_over_an_existing_watermark(self) -> None:
        seed = {"schema_version": 1, "at": DAY, "message_id": "<seed@example.test>",
                "sent_envelope_id": "900", "subject": "seed", "to": [], "references": []}
        (self.dd / "sent-index.jsonl").write_text(json.dumps(seed) + "\n", encoding="utf-8")
        mailbox = _Mailbox(_details(1))  # on the watermark day
        run, read = mailbox.patches()
        with run, read, patch.object(sent_indexer, "datetime", _frozen_datetime((2026, 9, 24))):
            result = sent_indexer.sync_sent_items(
                since="2026-09-30", account="ACC", data_dir=self.dd,
            )
        self.assertEqual(("2026-09-24", "2026-09-24"), (result["coverage_start"], result["coverage_end"]))
        self.assertEqual(1, result["new_count"])
        self.assertEqual(["seed@example.test", "m1@example.test"], self.index_ids())

    def test_single_day_over_200_is_batched_and_completes_after_consume(self) -> None:
        mailbox = _Mailbox(*(_details(n, date="Thu, 24 Sep 2026 10:00:00 +0200") for n in range(1, 202)))  # 201 mails
        run, read = mailbox.patches()
        with run, read:
            first = sent_indexer.sync_sent_items(dates=[DAY], count=1000, account="ACC", data_dir=self.dd)
        self.assertEqual(200, first["new_count"])
        self.assertTrue(first["truncated"])
        self.assertEqual(DAY, first["next_start"])
        self.assertEqual(200, first["handoff"]["written_count"])
        self.assertEqual(1, first["handoff"]["deferred_count"])
        self.assertEqual(200, len(self.index_ids()))

        self.consume(*self.handoff_ids())  # consumer releases the first batch
        run, read = mailbox.patches()
        with run, read:
            second = sent_indexer.sync_sent_items(dates=[DAY], account="ACC", data_dir=self.dd)
        self.assertEqual(1, second["new_count"])
        self.assertFalse(second["truncated"])
        self.assertEqual(201, len(self.index_ids()))
        self.assertEqual(201, len(set(self.index_ids())))
        self.assertEqual(["m201@example.test"], self.handoff_ids())

    def test_earlier_deferred_items_survive_a_later_consumed_item(self) -> None:
        # Day1 has 5 new, day2 one already-consumed but unindexed item.  The batch
        # must stop at day1's first deferral and never index day2 ahead of it.
        day1, day2 = "2026-09-23", "2026-09-24"
        mailbox = _Mailbox(
            *(_details(n, _day=day1, date="Wed, 23 Sep 2026 10:00:00 +0200") for n in range(1, 6)),
            _details(9, _day=day2, date="Thu, 24 Sep 2026 10:00:00 +0200"),
        )
        sent_handoff.record_synthesized(self.dd, [{"message_id": "<m9@example.test>"}], vorgang_ref="V-9")
        run, read = mailbox.patches()
        with run, read, patch.object(sent_indexer, "MAX_HANDOFF_ITEMS", 3), \
                patch.object(sent_handoff, "MAX_HANDOFF_ITEMS", 3), \
                patch.object(sent_indexer, "datetime", _frozen_datetime((2026, 9, 25))):
            first = sent_indexer.sync_sent_items(account="ACC", data_dir=self.dd)
            self.assertEqual(3, first["new_count"])
            self.assertTrue(first["truncated"])
            self.assertEqual(day1, first["next_start"])
            self.assertNotIn("m9@example.test", self.index_ids())  # later item not indexed ahead
            self.consume("m1@example.test", "m2@example.test", "m3@example.test")

            second = sent_indexer.sync_sent_items(account="ACC", data_dir=self.dd)
        self.assertFalse(second["truncated"])
        expected = {f"m{n}@example.test" for n in range(1, 6)} | {"m9@example.test"}
        self.assertEqual(expected, set(self.index_ids()))
        self.assertEqual(len(expected), len(self.index_ids()))

    def test_old_explicit_backfill_rewinds_barrier_before_a_younger_watermark(self) -> None:
        old_day, young_day = "2026-09-20", "2026-09-23"
        seed = {"schema_version": 1, "at": young_day, "message_id": "<seed@example.test>",
                "sent_envelope_id": "900", "subject": "seed", "to": [], "references": []}
        (self.dd / "sent-index.jsonl").write_text(json.dumps(seed) + "\n", encoding="utf-8")
        mailbox = _Mailbox(
            _details(1, _day=old_day, date="Sun, 20 Sep 2026 10:00:00 +0200"),
            _details(2, _day="2026-09-21", date="Mon, 21 Sep 2026 10:00:00 +0200"),
        )
        run, read = mailbox.patches()
        with run, read, patch.object(sent_indexer, "datetime", _frozen_datetime((2026, 9, 23))):
            report = sent_indexer.sync_sent_items_by_dates_report(
                [old_day], account="ACC", data_dir=self.dd, include_next_day=False,
            )
            self.assertEqual(1, report["new_count"])
            self.consume("m1@example.test")
            default = sent_indexer.sync_sent_items(account="ACC", data_dir=self.dd)
        self.assertEqual(1, default["new_count"])
        self.assertEqual({"seed@example.test", "m1@example.test", "m2@example.test"}, set(self.index_ids()))

    def test_batch_crash_before_index_blocks_until_consume_then_completes(self) -> None:
        mailbox = _Mailbox(*(_details(n) for n in range(1, 6)))
        run, read = mailbox.patches()
        with run, read, patch.object(sent_indexer, "MAX_HANDOFF_ITEMS", 3), \
                patch.object(sent_handoff, "MAX_HANDOFF_ITEMS", 3), \
                patch.object(sent_indexer, "datetime", _frozen_datetime()), \
                patch.object(sent_indexer, "append_sent_index_entries", side_effect=OSError("disk gone")):
            with self.assertRaises(RuntimeError):
                sent_indexer.sync_sent_items(dates=[DAY], count=1000, account="ACC", data_dir=self.dd)
        self.assertEqual(3, len(self.handoff_ids()))
        self.assertEqual([], self.index_ids())

        run, read = mailbox.patches()
        with run, read, patch.object(sent_indexer, "MAX_HANDOFF_ITEMS", 3), \
                patch.object(sent_handoff, "MAX_HANDOFF_ITEMS", 3), \
                patch.object(sent_indexer, "datetime", _frozen_datetime()):
            with self.assertRaises(SentHandoffError) as blocked:
                sent_indexer.sync_sent_items(dates=[DAY], count=1000, account="ACC", data_dir=self.dd)
            self.assertEqual("handoff_pending", blocked.exception.reason_code)
            self.consume(*self.handoff_ids())
            done = sent_indexer.sync_sent_items(dates=[DAY], count=1000, account="ACC", data_dir=self.dd)
        self.assertEqual(5, done["new_count"])
        self.assertEqual(5, len(self.index_ids()))
        self.assertEqual(5, len(set(self.index_ids())))

    def test_by_dates_report_surfaces_deferrals(self) -> None:
        mailbox = _Mailbox(*(_details(n, date="Thu, 24 Sep 2026 10:00:00 +0200") for n in range(1, 202)))
        run, read = mailbox.patches()
        with run, read:
            report = sent_indexer.sync_sent_items_by_dates_report(
                [DAY], account="ACC", data_dir=self.dd, include_next_day=False,
            )
        self.assertTrue(report["truncated"])
        self.assertEqual(1, report["deferred_count"])
        self.assertEqual(DAY, report["next_start"])
        self.assertIn("sync_sent_items_by_dates_report", report["recovery"])

    def test_by_dates_int_api_fails_closed_on_a_deferred_batch(self) -> None:
        mailbox = _Mailbox(*(_details(n, date="Thu, 24 Sep 2026 10:00:00 +0200") for n in range(1, 202)))
        run, read = mailbox.patches()
        with run, read:
            with self.assertRaises(SentHandoffError) as caught:
                sent_indexer.sync_sent_items_by_dates(
                    [DAY], account="ACC", data_dir=self.dd, include_next_day=False,
                )
        self.assertEqual("handoff_deferred", caught.exception.reason_code)
        self.assertEqual(200, caught.exception.details["committed_count"])
        self.assertEqual(1, caught.exception.details["deferred_count"])
        self.assertEqual(200, len(self.index_ids()))

    def test_single_day_250_is_two_batches_and_all_ids_eventual(self) -> None:
        mailbox = _Mailbox(*(_details(n, date="Thu, 24 Sep 2026 10:00:00 +0200") for n in range(1, 251)))
        run, read = mailbox.patches()
        with run, read:
            first = sent_indexer.sync_sent_items_by_dates_report(
                [DAY], account="ACC", data_dir=self.dd, include_next_day=False,
            )
        self.assertEqual(200, first["new_count"])
        self.assertTrue(first["truncated"])
        self.consume(*self.handoff_ids())
        run, read = mailbox.patches()
        with run, read:
            second = sent_indexer.sync_sent_items_by_dates_report(
                [DAY], account="ACC", data_dir=self.dd, include_next_day=False,
            )
        self.assertEqual(50, second["new_count"])
        self.assertFalse(second["truncated"])
        self.assertEqual(250, len(self.index_ids()))
        self.assertEqual(250, len(set(self.index_ids())))

    def test_since_never_jumps_over_the_persisted_initial_start(self) -> None:
        mailbox = _Mailbox(_details(1, _day="2026-09-20"))
        mailbox.fail_dates = {DAY}
        run, read = mailbox.patches()
        with run, read, patch.object(sent_indexer, "datetime", _frozen_datetime((2026, 9, 24))):
            with self.assertRaises(SentSyncPartialFailure):
                sent_indexer.sync_sent_items(since="2026-09-20", account="ACC", data_dir=self.dd)
            state = sent_indexer._read_sync_state(self.dd)
            self.assertEqual("2026-09-20", state["initial_start"])
            self.assertIsNone(state["covered_through"])

            mailbox.fail_dates = set()
            retry = sent_indexer.sync_sent_items(since="2026-09-24", account="ACC", data_dir=self.dd)
        self.assertEqual(["m1@example.test"], [i["norm_message_id"] for i in retry["new_items"]])
        self.assertEqual(["m1@example.test"], self.index_ids())

    def test_future_and_malformed_coverage_state_fail_closed(self) -> None:
        state_path = self.dd / sent_indexer.SYNC_STATE_FILENAME
        state_path.write_text(json.dumps({"schema_version": 1, "initial_start": None,
                                          "covered_through": "2026-12-31"}), encoding="utf-8")
        mailbox = _Mailbox(_details(1))
        run, read = mailbox.patches()
        with run, read, patch.object(sent_indexer, "datetime", _frozen_datetime((2026, 9, 24))):
            with self.assertRaises(SentHandoffError) as caught:
                sent_indexer.sync_sent_items(account="ACC", data_dir=self.dd)
        self.assertEqual("sent_initial_state_invalid", caught.exception.reason_code)
        self.assertEqual(0, mailbox.calls)
        self.assertEqual([], self.index_ids())

        state_path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(SentHandoffError) as malformed:
            sent_indexer.sync_sent_items(account="ACC", data_dir=self.dd)
        self.assertEqual("sent_initial_state_invalid", malformed.exception.reason_code)

    def test_initial_marker_older_than_watermark_is_not_bypassed(self) -> None:
        state_path = self.dd / sent_indexer.SYNC_STATE_FILENAME
        state_path.write_text(json.dumps({"schema_version": 1, "initial_start": "2026-09-20",
                                          "covered_through": None}), encoding="utf-8")
        seed = {"schema_version": 1, "at": "2026-09-24", "message_id": "<seed@example.test>",
                "sent_envelope_id": "900", "subject": "seed", "to": [], "references": []}
        (self.dd / "sent-index.jsonl").write_text(json.dumps(seed) + "\n", encoding="utf-8")
        mailbox = _Mailbox(_details(1, _day="2026-09-20", date="Sun, 20 Sep 2026 10:00:00 +0200"))
        run, read = mailbox.patches()
        with run, read, patch.object(sent_indexer, "datetime", _frozen_datetime((2026, 9, 24))):
            result = sent_indexer.sync_sent_items(account="ACC", data_dir=self.dd)
        self.assertEqual("2026-09-20", result["coverage_start"])
        self.assertEqual(["m1@example.test"], [i["norm_message_id"] for i in result["new_items"]])
        self.assertEqual({"seed@example.test", "m1@example.test"}, set(self.index_ids()))

    def test_empty_explicit_day_rewinds_the_barrier_so_later_mail_is_seen(self) -> None:
        day = "2026-09-22"
        empty = _Mailbox()
        run, read = empty.patches()
        with run, read:
            report = sent_indexer.sync_sent_items_by_dates_report(
                [day], account="ACC", data_dir=self.dd, include_next_day=False,
            )
        self.assertEqual(0, report["new_count"])
        state = sent_indexer._read_sync_state(self.dd)
        self.assertEqual("2026-09-21", state["covered_through"])

        arriving = _Mailbox(_details(1, _day=day, date="Tue, 22 Sep 2026 10:00:00 +0200"))
        run, read = arriving.patches()
        with run, read, patch.object(sent_indexer, "datetime", _frozen_datetime((2026, 9, 24))):
            result = sent_indexer.sync_sent_items(account="ACC", data_dir=self.dd)
        self.assertEqual(1, result["new_count"])
        self.assertEqual(["m1@example.test"], self.index_ids())

    def test_barrier_write_failure_after_index_rerun_dedupes(self) -> None:
        mailbox = _Mailbox(_details(1), _details(2))
        run, read = mailbox.patches()
        with run, read, patch.object(sent_indexer, "_write_sync_state", side_effect=OSError("disk gone")):
            with self.assertRaises(SentIndexAppendError):
                sent_indexer.sync_sent_items(dates=[DAY], account="ACC", data_dir=self.dd)
        self.assertEqual(2, len(self.index_ids()))
        self.assertFalse((self.dd / sent_indexer.SYNC_STATE_FILENAME).exists())

        run, read = mailbox.patches()
        with run, read:
            rerun = sent_indexer.sync_sent_items(dates=[DAY], account="ACC", data_dir=self.dd)
        self.assertEqual(0, rerun["new_count"])
        self.assertEqual(2, len(self.index_ids()))
        self.assertEqual(2, len(set(self.index_ids())))
        state = sent_indexer._read_sync_state(self.dd)
        self.assertEqual("2026-09-23", state["covered_through"])

    def test_bounded_batch_still_commits_the_oldest_and_defers_the_rest(self) -> None:
        seed = {"schema_version": 1, "at": "2026-09-30", "message_id": "<seed@example.test>",
                "sent_envelope_id": "900", "subject": "seed", "to": [], "references": []}
        (self.dd / "sent-index.jsonl").write_text(json.dumps(seed) + "\n", encoding="utf-8")
        mailbox = _Mailbox(*(_details(n) for n in range(1, 6)))
        with patch.object(sent_indexer, "MAX_HANDOFF_ITEMS", 3), patch.object(sent_handoff, "MAX_HANDOFF_ITEMS", 3), \
                patch.object(sent_indexer, "datetime", _frozen_datetime()):
            first = self.sync(mailbox)
        self.assertEqual(3, first["new_count"])
        self.assertTrue(first["truncated"])
        self.assertEqual(2, first["handoff"]["deferred_count"])
        self.assertEqual(["seed@example.test", "m1@example.test", "m2@example.test", "m3@example.test"],
                         self.index_ids())


class WindowTruncationTests(_Base):
    def test_exactly_full_page_with_empty_next_page_is_complete(self) -> None:
        mailbox = _Mailbox(*(_details(n) for n in range(1, 101)))  # 100 mails
        run, read = mailbox.patches()
        with run, read:
            added = sent_indexer.sync_sent_items_by_dates(
                [DAY], account="ACC", data_dir=self.dd, include_next_day=False,
            )
        self.assertEqual(100, added)
        self.assertEqual(100, len(self.index_ids()))
        self.assertEqual(2, mailbox.calls)  # page 1 full, page 2 empty

    def test_more_than_one_page_is_fully_paginated(self) -> None:
        mailbox = _Mailbox(*(_details(n) for n in range(1, 102)))  # 101 mails
        run, read = mailbox.patches()
        with run, read:
            added = sent_indexer.sync_sent_items_by_dates(
                [DAY], account="ACC", data_dir=self.dd, include_next_day=False,
            )
        self.assertEqual(101, added)
        self.assertEqual(101, len(self.index_ids()))
        self.assertEqual(2, mailbox.calls)  # 100 + 1

    def test_multiple_pages_are_fully_paginated(self) -> None:
        mailbox = _Mailbox(*(_details(n) for n in range(1, 151)))  # 150 mails
        run, read = mailbox.patches()
        with run, read:
            added = sent_indexer.sync_sent_items_by_dates(
                [DAY], account="ACC", data_dir=self.dd, include_next_day=False,
            )
        self.assertEqual(150, added)
        self.assertEqual(150, len(self.index_ids()))
        self.assertEqual(2, mailbox.calls)  # 100 + 50

    def test_page_limit_without_proof_of_completeness_is_fail_closed(self) -> None:
        mailbox = _Mailbox(*(_details(n) for n in range(1, 101)))  # 100 mails
        run, read = mailbox.patches()
        with run, read, self.assertRaises(SentHandoffError) as caught:
            sent_indexer.sync_sent_items_by_dates(
                [DAY], account="ACC", data_dir=self.dd, include_next_day=False, max_pages=1,
            )
        self.assertEqual("sent_window_truncated", caught.exception.reason_code)
        self.assertIn("max_pages=2", str(caught.exception))
        self.assertEqual(1, mailbox.calls)
        self.assertEqual([], self.index_ids())
        self.assertFalse((self.dd / HANDOFF_FILENAME).exists())

    def test_error_on_a_later_page_is_fail_closed_before_any_write(self) -> None:
        mailbox = _Mailbox(*(_details(n) for n in range(1, 151)))  # 150 mails
        mailbox.fail_pages = {DAY: {2}}
        run, read = mailbox.patches()
        with run, read, self.assertRaises(RuntimeError) as caught:
            sent_indexer.sync_sent_items_by_dates(
                [DAY], account="ACC", data_dir=self.dd, include_next_day=False,
            )
        self.assertIn("sync_sent_items_by_dates(dates=['2026-09-24']", str(caught.exception))
        self.assertEqual([], self.index_ids())
        self.assertFalse((self.dd / HANDOFF_FILENAME).exists())

    def test_repeated_page_ids_are_deduplicated_and_fail_closed(self) -> None:
        mailbox = _Mailbox(*(_details(n) for n in range(1, 101)))
        mailbox.repeat_pages = True
        run, read = mailbox.patches()
        with run, read, self.assertRaises(SentHandoffError) as caught:
            sent_indexer.sync_sent_items_by_dates(
                [DAY], account="ACC", data_dir=self.dd, include_next_day=False,
            )
        self.assertEqual("sent_window_truncated", caught.exception.reason_code)
        self.assertEqual(100, caught.exception.details["envelopes_examined"])  # deduped
        self.assertEqual([], self.index_ids())
        self.assertFalse((self.dd / HANDOFF_FILENAME).exists())

    def test_verified_out_of_bound_on_extra_page_is_treated_as_end(self) -> None:
        mailbox = _Mailbox(*(_details(n) for n in range(1, 101)))  # exact 100 multiple
        mailbox.out_of_bound_pages = True
        run, read = mailbox.patches()
        with run, read:
            added = sent_indexer.sync_sent_items_by_dates(
                [DAY], account="ACC", data_dir=self.dd, include_next_day=False,
            )
        self.assertEqual(100, added)
        self.assertEqual(100, len(self.index_ids()))
        self.assertEqual(2, mailbox.calls)

    def test_transport_out_of_bounds_error_is_not_mistaken_for_page_end(self) -> None:
        mailbox = _Mailbox(*(_details(n) for n in range(1, 101)))
        mailbox.transport_error_pages = True
        run, read = mailbox.patches()
        with run, read, self.assertRaises(RuntimeError) as caught:
            sent_indexer.sync_sent_items_by_dates(
                [DAY], account="ACC", data_dir=self.dd, include_next_day=False,
            )
        self.assertIn("buffer index out of bounds", str(caught.exception))
        self.assertEqual([], self.index_ids())
        self.assertFalse((self.dd / HANDOFF_FILENAME).exists())

    def test_empty_first_page_is_complete_with_zero(self) -> None:
        mailbox = _Mailbox()
        result = self.sync(mailbox)
        self.assertEqual(0, result["new_count"])
        self.assertFalse(result["truncated"])


class PendingWriteContractTests(_Base):
    def test_consumed_items_do_not_trigger_writes_and_pending_is_never_rewritten(self) -> None:
        both = _Mailbox(_details(1), _details(2))
        self.sync(both)
        self.consume("m1@example.test")
        before = (self.dd / HANDOFF_FILENAME).read_bytes()

        zero_new = self.sync(both)  # nothing new: no write, consumed item merely ignored
        self.assertEqual(("pending", 1, 1), (
            zero_new["handoff"]["state"], zero_new["handoff"]["pending_count"], zero_new["handoff"]["consumed_ignored"]
        ))
        self.assertEqual(before, (self.dd / HANDOFF_FILENAME).read_bytes())

        with self.assertRaises(SentHandoffError) as caught:  # pending m2 + new m3: refused, unchanged
            self.sync(_Mailbox(_details(1), _details(2), _details(3)))
        self.assertEqual(("handoff_pending", "pending"), (caught.exception.reason_code, caught.exception.details["state"]))
        self.assertEqual(before, (self.dd / HANDOFF_FILENAME).read_bytes())
        self.assertEqual(["m1@example.test", "m2@example.test"], self.index_ids())

    def test_all_consumed_handoff_is_replaced_by_the_next_new_items(self) -> None:
        self.sync(_Mailbox(_details(1)))
        self.consume("m1@example.test")
        self.sync(_Mailbox(_details(1), _details(2)))
        self.assertEqual(["m2@example.test"], self.handoff_ids())

    def test_pending_items_of_another_mailbox_are_refused(self) -> None:
        self.sync(_Mailbox(_details(1)))
        path = self.dd / HANDOFF_FILENAME
        document = json.loads(path.read_text("utf-8"))
        document["items"][0]["mailbox"] = "OTHER"
        path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(SentHandoffError) as caught:
            self.sync(_Mailbox(_details(1), _details(2)))
        self.assertEqual(("handoff_invalid", "mailbox_mismatch"), (caught.exception.reason_code, caught.exception.details["reason"]))
        self.assertEqual(["m1@example.test"], self.index_ids())

    def test_directory_fsync_is_best_effort_and_portable(self) -> None:
        with patch.object(sent_handoff, "_fsync_directory") as fsync_dir:
            sent_handoff.write_handoff(self.dd, [])
        fsync_dir.assert_called_once_with(self.dd)
        with patch.object(sent_handoff.os, "open", side_effect=OSError("directories cannot be opened")):
            sent_handoff._fsync_directory(self.dd)  # must not raise (Windows)


class HandoffValidationTests(_Base):
    MUTATIONS = {
        "identity": lambda d: d["items"][0].__setitem__("norm_message_id", "other@example.test"),
        "thread_key": lambda d: d["items"][0].__setitem__("thread_key", "evil@example.test"),
        "anchor_candidates": lambda d: d["items"][0].__setitem__("anchor_candidates", ["a@example.test"]),
        "count": lambda d: d.__setitem__("count", 5),
        "bound": lambda d: d.__setitem__("max_items", 0),
        "unknown_field": lambda d: d["items"][0].__setitem__("extra", 1),
        "to_type": lambda d: d["items"][0].__setitem__("to", "a@example.test"),
        "anchored_to": lambda d: d["items"][0].__setitem__("anchored_to", {"x": 1}),
        "kind": lambda d: d.__setitem__("kind", "other"),
        "mailbox_blank": lambda d: d["items"][0].__setitem__("mailbox", " "),
        "missing_field": lambda d: d["items"][0].pop("cc"),
        "headers_flag": lambda d: d["items"][0].__setitem__("headers_complete", "yes"),
        "truncated_field_objects": lambda d: d["items"][0].__setitem__("truncated_fields", [{}]),
        "truncated_field_types": lambda d: d["items"][0].__setitem__("truncated_fields", ["subject", 1]),
        "anchored_to_match_type_type": lambda d: d["items"][0].__setitem__(
            "anchored_to", {"vorgang_ref": "V-1", "matched_message_id": "m@example.test", "match_type": []}
        ),
        "duplicate": lambda d: (d["items"].append(dict(d["items"][0])), d.__setitem__("count", 2)),
        "schema_version_float": lambda d: d.__setitem__("schema_version", 1.0),
        "schema_version_bool": lambda d: d.__setitem__("schema_version", True),
        "count_float": lambda d: d.__setitem__("count", float(d["count"])),
        "count_bool": lambda d: d.__setitem__("count", True),
        "generated_at": lambda d: d.__setitem__("generated_at", "yesterday"),
    }

    def test_canonical_item_bound_applies_regardless_of_the_file_max_items(self) -> None:
        def item(index: int) -> dict:
            return sent_handoff.build_handoff_item(
                {
                    "message_id": f"<m{index}@example.test>",
                    "at": f"Thu, 24 Sep 2026 10:{index % 60:02d}:00 +0200",
                    "subject": f"Subject {index}",
                    "references": [],
                    "sent_envelope_id": str(1000 + index),
                    "folder": "Sent Items",
                    "mailbox": "ACC",
                },
                to="", cc="", headers_complete=True,
            )

        path = self.dd / HANDOFF_FILENAME
        accepted = {
            "schema_version": 1, "kind": "mail-desk.new-sent-items",
            "generated_at": "2026-11-09T09:00:00Z", "max_items": 200, "count": 200,
            "items": [item(i) for i in range(200)],
        }
        path.write_text(json.dumps(accepted), encoding="utf-8")
        self.assertEqual(200, len(sent_handoff.read_handoff(self.dd)))  # canonical boundary passes

        oversized = {**accepted, "max_items": 1000, "count": 201, "items": [item(i) for i in range(201)]}
        path.write_text(json.dumps(oversized), encoding="utf-8")
        before = path.read_bytes()
        with self.assertRaises(SentHandoffError) as caught:
            sent_handoff.read_handoff(self.dd)
        self.assertEqual(("handoff_invalid", "max_items_malformed"), (
            caught.exception.reason_code, caught.exception.details["reason"],
        ))
        self.assertEqual(before, path.read_bytes())
        with self.assertRaises(SentHandoffError):
            sent_handoff.load_pending_handoff(self.dd)

        # Even with a canonical header the count bound is the canonical one (200).
        over_count = {**accepted, "count": 201, "items": [item(i) for i in range(201)]}
        path.write_text(json.dumps(over_count), encoding="utf-8")
        with self.assertRaises(SentHandoffError) as counted:
            sent_handoff.read_handoff(self.dd)
        self.assertEqual("count_or_bound_mismatch", counted.exception.details["reason"])

        widened_header = {**accepted, "max_items": 1000}
        path.write_text(json.dumps(widened_header), encoding="utf-8")
        with self.assertRaises(SentHandoffError) as header:
            sent_handoff.read_handoff(self.dd)
        self.assertEqual("max_items_malformed", header.exception.details["reason"])

    def test_documented_field_lengths_are_enforced_on_read(self) -> None:
        self.sync(_Mailbox(_details(1)))
        path = self.dd / HANDOFF_FILENAME
        valid = json.loads(path.read_text("utf-8"))

        cases = {
            "subject": lambda d: d["items"][0].__setitem__("subject", "s" * (sent_handoff.MAX_SUBJECT_CHARS + 1)),
            "to": lambda d: d["items"][0].__setitem__(
                "to", [f"p{i}@example.test" for i in range(sent_handoff.MAX_RECIPIENTS + 1)]
            ),
            "cc": lambda d: d["items"][0].__setitem__(
                "cc", [f"c{i}@example.test" for i in range(sent_handoff.MAX_RECIPIENTS + 1)]
            ),
        }

        def overlong_references(document: dict) -> None:
            item = document["items"][0]
            references = [f"<r{i}@example.test>" for i in range(sent_handoff.MAX_REFERENCES + 1)]
            item["references"] = references
            item["thread_key"] = sent_handoff.thread_key(item["in_reply_to"], references, item["message_id"])
            item["anchor_candidates"] = sent_handoff.thread_anchor_ids(
                item["in_reply_to"], references, item["message_id"]
            )

        cases["references"] = overlong_references
        expected = {
            "subject": "subject_too_long",
            "to": "to_too_many",
            "cc": "cc_too_many",
            "references": "references_too_long",
        }
        for name, mutate in cases.items():
            with self.subTest(name):
                document = json.loads(json.dumps(valid))
                mutate(document)
                path.write_text(json.dumps(document), encoding="utf-8")
                before = path.read_bytes()
                with self.assertRaises(SentHandoffError) as caught:
                    sent_handoff.read_handoff(self.dd)
                self.assertEqual("handoff_invalid", caught.exception.reason_code)
                self.assertEqual(f"item[0]: {expected[name]}", caught.exception.details["reason"])
                self.assertEqual(before, path.read_bytes())
                with self.assertRaises(SentHandoffError):
                    sent_handoff.load_pending_handoff(self.dd)

    def test_deeply_nested_json_is_handoff_invalid_not_recursion_error(self) -> None:
        deep = "[" * 10000 + "]" * 10000
        path = self.dd / HANDOFF_FILENAME
        path.write_text(
            '{"schema_version":1,"kind":"mail-desk.new-sent-items",'
            '"generated_at":"2026-11-09T09:00:00Z","max_items":200,"count":0,"items":[],"deep":'
            + deep + "}",
            encoding="utf-8",
        )
        before = path.read_bytes()
        with self.assertRaises(SentHandoffError) as caught:
            sent_handoff.read_handoff(self.dd)
        self.assertEqual(("handoff_invalid", "json_too_deep"), (
            caught.exception.reason_code, caught.exception.details["reason"],
        ))
        self.assertEqual(before, path.read_bytes())

    def test_every_inconsistency_is_refused_untouched_and_nothing_is_indexed(self) -> None:
        self.sync(_Mailbox(_details(1)))
        path = self.dd / HANDOFF_FILENAME
        valid = json.loads(path.read_text("utf-8"))
        self.assertEqual(1, len(sent_handoff.read_handoff(self.dd)))  # control: the valid document passes
        for name, mutate in self.MUTATIONS.items():
            with self.subTest(name):
                document = json.loads(json.dumps(valid))
                mutate(document)
                path.write_text(json.dumps(document), encoding="utf-8")
                before = path.read_bytes()
                with self.assertRaises(SentHandoffError) as caught:
                    self.sync(_Mailbox(_details(1), _details(2)))
                self.assertEqual(("handoff_invalid", "invalid"), (caught.exception.reason_code, caught.exception.details["state"]))
                self.assertEqual(before, path.read_bytes())
                self.assertEqual(["m1@example.test"], self.index_ids())
                with self.assertRaises(SentHandoffError):
                    sent_handoff.load_pending_handoff(self.dd)


class LedgerValidationTests(_Base):
    BAD = {
        "unknown_field": _ledger_line(extra=1),
        "invalid_role": _ledger_line(role="other"),
        "role_list": _ledger_line(role=[]),
        "role_object": _ledger_line(role={"name": "primary"}),
        "not_normalized": _ledger_line(message_id="<X@example.test>"),
        "follow_up_without_anchor": _ledger_line(role="follow_up"),
        "primary_with_anchor": _ledger_line(anchored_to="a@example.test"),
        "bad_timestamp": _ledger_line(recorded_at="soon"),
        "bad_schema": _ledger_line(schema_version=2),
        "schema_float": _ledger_line(schema_version=1.0),
        "schema_bool": _ledger_line(schema_version=True),
        "vorgang_ref_type": _ledger_line(vorgang_ref=7),
        "missing_required": {k: v for k, v in _ledger_line().items() if k != "recorded_at"},
    }

    def write_ledger(self, *lines: str) -> None:
        (self.dd / LEDGER_FILENAME).write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_schema_violations_fail_closed_before_any_write(self) -> None:
        for name, bad in self.BAD.items():
            with self.subTest(name):
                self.write_ledger(json.dumps(_ledger_line(message_id="ok@example.test")), json.dumps(bad))
                with self.assertRaises(SentHandoffError) as caught:
                    sent_handoff.load_ledger(self.dd)
                self.assertEqual(("ledger_invalid", [2]), (caught.exception.reason_code, caught.exception.details["invalid_lines"]))
                with self.assertRaises(SentHandoffError):
                    self.sync(_Mailbox(_details(1)))
                with self.assertRaises(SentHandoffError):
                    sent_handoff.record_synthesized(self.dd, [{"message_id": "<n@example.test>"}])
                self.assertFalse((self.dd / HANDOFF_FILENAME).exists())
                self.assertEqual([], self.index_ids())

    def test_deeply_nested_ledger_line_is_ledger_invalid_not_recursion_error(self) -> None:
        deep = '{"deep":' + "[" * 10000 + "]" * 10000 + "}"
        self.write_ledger(deep)
        with self.assertRaises(SentHandoffError) as caught:
            sent_handoff.load_ledger(self.dd)
        self.assertEqual(("ledger_invalid", [1]), (
            caught.exception.reason_code, caught.exception.details["invalid_lines"],
        ))

    def test_garbage_middle_line_fails_closed_and_minimal_valid_line_passes(self) -> None:
        valid = json.dumps(_ledger_line())
        self.write_ledger(valid, "garbage", valid)
        with self.assertRaises(SentHandoffError) as caught:
            sent_handoff.load_ledger(self.dd)
        self.assertEqual([2], caught.exception.details["invalid_lines"])
        minimal = {k: _ledger_line()[k] for k in ("schema_version", "message_id", "role", "recorded_at")}
        self.write_ledger(json.dumps(minimal))
        self.assertEqual({"x@example.test"}, set(sent_handoff.load_ledger(self.dd)["by_message_id"]))


class TornUtf8Tests(_Base):
    PARTIAL = b'{"schema_version":1,"message_id":"caf\xc3'  # record cut inside a multi-byte character

    def test_torn_utf8_ledger_tail_keeps_valid_evidence(self) -> None:
        sent_handoff.record_synthesized(self.dd, [{"message_id": "<x@example.test>"}], vorgang_ref="V-1")
        path = self.dd / LEDGER_FILENAME
        path.write_bytes(path.read_bytes() + self.PARTIAL)

        ledger = sent_handoff.load_ledger(self.dd)  # must not raise UnicodeDecodeError
        self.assertTrue(ledger["torn_tail"])
        self.assertEqual({"x@example.test"}, set(ledger["by_message_id"]))

        sent_handoff.record_synthesized(self.dd, [{"message_id": "<y@example.test>"}])
        self.assertEqual({"x@example.test", "y@example.test"}, set(sent_handoff.load_ledger(self.dd)["by_message_id"]))
        self.assertEqual(self.PARTIAL + b"\n", (self.dd / (LEDGER_FILENAME + ".torn")).read_bytes())

    def test_torn_utf8_index_tail_neither_breaks_reads_nor_loses_entries(self) -> None:
        self.sync(_Mailbox(_details(1)))
        path = self.dd / "sent-index.jsonl"
        path.write_bytes(path.read_bytes() + self.PARTIAL)

        self.assertEqual(["m1@example.test"], list(sent_indexer.load_sent_index(self.dd)["by_message_id"]))
        self.consume("m1@example.test")  # release the handoff so m2 may be handed off
        self.sync(_Mailbox(_details(1), _details(2)))

        self.assertEqual(["m1@example.test", "m2@example.test"], self.index_ids())
        self.assertEqual(self.PARTIAL + b"\n", (self.dd / "sent-index.jsonl.torn").read_bytes())

    def test_complete_record_without_trailing_newline_is_kept_not_quarantined(self) -> None:
        path = self.dd / LEDGER_FILENAME
        path.write_text(json.dumps(_ledger_line()), encoding="utf-8")  # valid, no newline
        self.assertEqual({"x@example.test"}, set(sent_handoff.load_ledger(self.dd)["by_message_id"]))
        sent_handoff.record_synthesized(self.dd, [{"message_id": "<y@example.test>"}])
        self.assertEqual({"x@example.test", "y@example.test"}, set(sent_handoff.load_ledger(self.dd)["by_message_id"]))
        self.assertFalse((self.dd / (LEDGER_FILENAME + ".torn")).exists())


class ReplyReresolutionTests(_Base):
    def two_item_handoff(self) -> None:
        self.sync(_Mailbox(
            _details(1),
            _details(2, in_reply_to="<m1@example.test>", references="<m1@example.test>"),
        ))

    def test_pending_view_orders_ancestors_first_and_reresolves_after_each_record(self) -> None:
        self.two_item_handoff()
        view = sent_handoff.load_pending_handoff(self.dd)
        self.assertEqual(["m1@example.test", "m2@example.test"], [i["norm_message_id"] for i in view["items"]])
        self.assertEqual([[], ["m1@example.test"]], [i["waits_for"] for i in view["items"]])
        self.assertEqual([None, None], [i["anchored_to"] for i in view["items"]])

        sent_handoff.record_synthesized(self.dd, view["items"][:1], vorgang_ref="V-7")
        after = sent_handoff.load_pending_handoff(self.dd)
        self.assertEqual(1, after["pending_count"])
        reply = after["items"][0]
        self.assertEqual(
            {"vorgang_ref": "V-7", "matched_message_id": "m1@example.test", "match_type": "in_reply_to"},
            reply["anchored_to"],
        )
        # The stored snapshot is stale (None); recording re-resolves against the current ledger.
        self.assertIsNone(sent_handoff.read_handoff(self.dd)[1]["anchored_to"])
        sent_handoff.record_synthesized(self.dd, [sent_handoff.read_handoff(self.dd)[1]])
        record = sent_handoff.load_ledger(self.dd)["by_message_id"]["m2@example.test"]
        self.assertEqual(("follow_up", "V-7", "m1@example.test"), (record["role"], record["vorgang_ref"], record["anchored_to"]))
        self.assertEqual(0, sent_handoff.load_pending_handoff(self.dd)["pending_count"])

    def test_one_batch_call_resolves_replies_against_earlier_items_of_the_same_call(self) -> None:
        self.two_item_handoff()
        items = sent_handoff.load_pending_handoff(self.dd)["items"]
        sent_handoff.record_synthesized(self.dd, [{**items[0], "vorgang_ref": "V-9"}, items[1]])
        ledger = sent_handoff.load_ledger(self.dd)["by_message_id"]
        self.assertEqual(("primary", "follow_up"), (ledger["m1@example.test"]["role"], ledger["m2@example.test"]["role"]))
        self.assertEqual("V-9", ledger["m2@example.test"]["vorgang_ref"])

    def test_ancestry_cycle_in_untrusted_headers_does_not_hang_or_drop_items(self) -> None:
        self.sync(_Mailbox(
            _details(1, in_reply_to="<m2@example.test>", references="<m2@example.test>"),
            _details(2, in_reply_to="<m1@example.test>", references="<m1@example.test>"),
        ))
        view = sent_handoff.load_pending_handoff(self.dd)
        self.assertEqual({"m1@example.test", "m2@example.test"}, {i["norm_message_id"] for i in view["items"]})


class ModeEnvelopeTests(_Base):
    def run_mode(self, mailbox: _Mailbox) -> dict:
        run, read = mailbox.patches()
        with run, read, patch.object(sent_indexer, "datetime", _frozen_datetime()):
            return sync_sent_mode.run_sync_sent_mode(
                {"mode": "sync_sent", "count": 150, "folder": "Sent Items"}, account="ACC", data_dir=self.dd
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
        self.assertEqual("pending", envelope["handoff"]["state"])
        self.assertEqual(["m1@example.test"], self.index_ids())

    def assert_failed_shape(
        self, envelope: dict, reason: str, state: str, *,
        write_state: str = "none", new_count: int | None = 0,
    ) -> None:
        self.assertFalse(envelope["ok"])
        self.assertEqual(reason, envelope["reason_code"])
        self.assertEqual((new_count, []), (envelope["new_count"], envelope["new_items"]))
        self.assertEqual(write_state, envelope["write_state"])
        self.assertIn(envelope["handoff"]["state"], sent_handoff.HANDOFF_STATES)
        self.assertEqual(state, envelope["handoff"]["state"])
        self.assertEqual(str(self.dd / HANDOFF_FILENAME), envelope["handoff"]["file"])
        for key in ("written_count", "pending_count", "consumed_ignored", "deferred_count",
                    "headers_incomplete_count", "max_items"):
            self.assertIn(key, envelope["handoff"])
        json.dumps(envelope)

    def test_every_failure_envelope_keeps_shape_and_documented_state(self) -> None:
        path = self.dd / HANDOFF_FILENAME
        path.write_text("{broken", encoding="utf-8")
        self.assert_failed_shape(self.run_mode(_Mailbox(_details(1))), "handoff_invalid", "invalid", write_state="state")
        path.unlink()

        (self.dd / LEDGER_FILENAME).write_text("garbage\n", encoding="utf-8")
        self.assert_failed_shape(self.run_mode(_Mailbox(_details(1))), "ledger_invalid", "invalid")
        (self.dd / LEDGER_FILENAME).unlink()

        with patch.object(sent_indexer, "append_sent_index_entries", side_effect=OSError("disk gone")):
            envelope = self.run_mode(_Mailbox(_details(1)))  # append failure after the handoff was written
        # Unknown progress: the handoff is persisted, the index may be partially grown.
        self.assert_failed_shape(envelope, "sync_failed", "pending", write_state="unknown", new_count=None)
        self.assertEqual("OSError", envelope["handoff"]["error_type"])
        self.assertIs(True, envelope["handoff"]["append_failed"])
        self.assertEqual(1, envelope["handoff"]["pending_count"])
        self.assertIs(True, envelope["new_items_unavailable"])
        self.assertEqual(["m1@example.test"], self.handoff_ids())

        # Overflow refusal reports the state of the untouched handoff (here: pending, m1 is unconsumed).
        def deferred(**_kwargs):
            raise SentHandoffError("handoff_deferred", "batch deferred", {"state": "none"})

        refused = sync_sent_mode.run_sync_sent_mode(
            {"count": 3}, account="ACC", data_dir=self.dd, dependencies={"sync_sent_items": deferred}
        )
        self.assert_failed_shape(refused, "handoff_deferred", "none")

    def test_unreadable_index_is_a_shaped_failure_not_a_success(self) -> None:
        def boom(**_kwargs):
            raise PermissionError("sent-index locked")

        envelope = sync_sent_mode.run_sync_sent_mode(
            {"count": 3}, account="ACC", data_dir=self.dd, dependencies={"sync_sent_items": boom}
        )
        self.assert_failed_shape(envelope, "sync_failed", "none", write_state="none", new_count=0)
        self.assertIn("PermissionError", envelope["message"])

    def test_invalid_manifest_count_yields_the_shaped_refusal_and_result_envelope(self) -> None:
        def never(**_kwargs):  # a manifest refusal must never reach the mailbox
            raise AssertionError("sync must not run")

        for bad in ("not-a-number", None, 0, -3, True, 2.5):
            with self.subTest(bad=bad):
                envelope = sync_sent_mode.run_sync_sent_mode(
                    {"count": bad}, account="ACC", data_dir=self.dd,
                    dependencies={"sync_sent_items": never},
                )
                self.assert_failed_shape(envelope, "sync_invalid_count", "none")
                self.assertEqual("count", envelope["handoff"]["invalid_field"])
                self.assertIn("positive integer", envelope["message"])

        import mail_desk_batch_runner as runner

        result_envelope = runner._result_envelope(envelope, "sync_sent")
        self.assertFalse(result_envelope["success"])
        self.assertEqual((0, []), (
            result_envelope["data"]["new_count"], result_envelope["data"]["new_items"]
        ))
        self.assertEqual("sync_invalid_count", result_envelope["data"]["reason_code"])
        self.assertEqual("none", result_envelope["data"]["handoff"]["state"])

    def test_legacy_tuple_contract_also_carries_new_count_and_items(self) -> None:
        envelope = sync_sent_mode.run_sync_sent_mode(
            {"count": 3}, account="ACC", data_dir=self.dd, dependencies={"sync_sent_items": lambda **_: (7, 2)}
        )
        self.assertTrue(envelope["ok"])
        self.assertEqual((2, [], True), (envelope["new_count"], envelope["new_items"], envelope["new_items_unavailable"]))
        self.assertEqual("none", envelope["handoff"]["state"])
        self.assertEqual(str(self.dd / HANDOFF_FILENAME), envelope["handoff"]["file"])
        for key in ("written_count", "pending_count", "consumed_ignored", "deferred_count",
                    "headers_incomplete_count", "max_items"):
            self.assertIn(key, envelope["handoff"])

    def test_runner_binding_refusal_keeps_the_sync_result_shape(self) -> None:
        import mail_desk_batch_runner as runner

        result = runner.run_sync_sent_mode({"count": 3}, account="primary", data_dir=self.dd)  # no backend binding file
        self.assertFalse(result["ok"])
        self.assertEqual("backend_binding", result["reason_code"])
        self.assertEqual((0, []), (result["new_count"], result["new_items"]))
        self.assertEqual("none", result["write_state"])
        self.assertEqual(str(self.dd / HANDOFF_FILENAME), result["handoff"]["file"])
        self.assertIn(result["handoff"]["state"], sent_handoff.HANDOFF_STATES)
        self.assertIn("backend_binding", result)
        json.dumps(result)

        envelope = runner._result_envelope(result, "sync_sent")
        self.assertFalse(envelope["success"])
        self.assertEqual("backend_binding", envelope["data"]["reason_code"])
        self.assertEqual((0, []), (envelope["data"]["new_count"], envelope["data"]["new_items"]))
        self.assertEqual("none", envelope["data"]["handoff"]["state"])
        self.assertIn("backend_binding", envelope["data"])

    def test_invalid_since_and_float_max_pages_are_shaped_refusals(self) -> None:
        for config, reason in (
            ({"count": 150, "since": "30.09.2026"}, "sync_invalid_since"),
            ({"count": 150, "max_pages": 2.5}, "sync_invalid_max_pages"),
        ):
            with self.subTest(reason=reason):
                envelope = sync_sent_mode.run_sync_sent_mode(config, account="ACC", data_dir=self.dd)
                self.assertFalse(envelope["ok"])
                self.assertEqual(reason, envelope["reason_code"])
                self.assertEqual("none", envelope["write_state"])

    def test_truncated_window_reports_the_real_pending_handoff_state(self) -> None:
        self.run_mode(_Mailbox(_details(1), _details(2)))  # leaves two unconsumed handoff items
        full = _Mailbox(*(_details(n) for n in range(1, 101)))  # full page on the watermark day
        run, read = full.patches()
        with run, read, patch.object(sent_indexer, "datetime", _frozen_datetime()):
            envelope = sync_sent_mode.run_sync_sent_mode(
                {"count": 150, "max_pages": 1}, account="ACC", data_dir=self.dd,
            )

        self.assertFalse(envelope["ok"])
        self.assertEqual("sent_window_truncated", envelope["reason_code"])
        self.assertEqual("pending", envelope["handoff"]["state"])
        self.assertEqual(2, envelope["handoff"]["pending_count"])
        self.assertEqual(DAY, envelope["handoff"]["date"])
        self.assertEqual("none", envelope["write_state"])
        self.assertEqual(2, len(self.handoff_ids()))

    def test_failed_date_window_is_fail_closed_and_reports_failed_dates(self) -> None:
        mailbox = _Mailbox(_details(1))
        mailbox.fail_dates = {DAY}
        envelope = self.run_mode(mailbox)
        self.assertFalse(envelope["ok"])
        self.assertEqual("sync_window_failed", envelope["reason_code"])
        self.assertEqual("state", envelope["write_state"])
        self.assertEqual([DAY], envelope["failed_dates"])
        self.assertEqual((0, []), (envelope["new_count"], envelope["new_items"]))
        self.assertEqual("none", envelope["handoff"]["state"])
        self.assertFalse((self.dd / HANDOFF_FILENAME).exists())
        self.assertEqual([], self.index_ids())

        mailbox.fail_dates = set()  # window fixed: retry writes both days once
        retry = self.run_mode(mailbox)
        self.assertTrue(retry["ok"])
        self.assertEqual(1, retry["new_count"])
        self.assertEqual(["m1@example.test"], self.index_ids())

    def test_all_dates_failing_reports_no_writes(self) -> None:
        mailbox = _Mailbox(_details(1))
        mailbox.fail_dates = {DAY, "2026-09-25"}
        envelope = self.run_mode(mailbox)
        self.assertFalse(envelope["ok"])
        self.assertEqual(("sync_window_failed", "state", 0), (
            envelope["reason_code"], envelope["write_state"], envelope["new_count"],
        ))
        self.assertFalse((self.dd / HANDOFF_FILENAME).exists())
        self.assertEqual([], self.index_ids())

    def test_publish_failure_after_replace_reports_unknown_and_rerun_is_idempotent(self) -> None:
        mailbox = _Mailbox(_details(1))
        # The handoff file is already atomically replaced when the directory sync
        # fails; the envelope must not claim that nothing was written.
        with patch.object(sent_handoff, "_fsync_directory", side_effect=OSError("close failed")):
            failed = self.run_mode(mailbox)

        self.assert_failed_shape(failed, "sync_failed", "pending", write_state="unknown", new_count=None)
        self.assertEqual("OSError", failed["handoff"]["error_type"])
        self.assertIs(True, failed["handoff"]["handoff_publish_failed"])
        self.assertEqual(1, failed["handoff"]["pending_count"])
        self.assertEqual(["m1@example.test"], self.handoff_ids())
        self.assertEqual([], self.index_ids())

        rerun = self.run_mode(mailbox)
        self.assertTrue(rerun["ok"])
        self.assertEqual(1, rerun["new_count"])
        self.assertEqual(0, rerun["handoff"]["written_count"])
        self.assertEqual(["m1@example.test"], self.index_ids())

    def test_append_failure_reports_unknown_progress_and_rerun_is_idempotent(self) -> None:
        mailbox = _Mailbox(_details(1))
        with patch.object(sent_indexer, "append_sent_index_entries", side_effect=OSError("disk gone")):
            failed = self.run_mode(mailbox)
        self.assert_failed_shape(failed, "sync_failed", "pending", write_state="unknown", new_count=None)
        self.assertEqual(["m1@example.test"], self.handoff_ids())
        self.assertEqual([], self.index_ids())

        rerun = self.run_mode(mailbox)
        self.assertTrue(rerun["ok"])
        self.assertEqual(1, rerun["new_count"])
        self.assertEqual(0, rerun["handoff"]["written_count"])
        self.assertEqual(["m1@example.test"], self.index_ids())

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
                    "write_state": "none",
                    "handoff": {"state": "handoff_pending", "pending_count": 2, "file": "f", "ignored": 1},
                },
            }), encoding="utf-8")
            summary = cli._summary(
                argparse.Namespace(mode="sync-sent"), envelope_path, Path(tmp) / "p.log",
                subprocess.CompletedProcess([], 1),
            )
        self.assertEqual("handoff_pending", summary["reason_code"])
        self.assertEqual("none", summary["write_state"])
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


def _frozen_datetime(day=(2026, 9, 24)):
    from datetime import datetime as real

    class _Frozen(real):
        @classmethod
        def now(cls):
            return real(*day, 12, 0, 0)

    return _Frozen


if __name__ == "__main__":
    unittest.main()
