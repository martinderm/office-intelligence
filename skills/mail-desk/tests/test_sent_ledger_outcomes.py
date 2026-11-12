"""Issue #107: explicit sent-ledger outcomes and compatibility contracts.

Hermetic and offline: no mailbox, network, consumer workspace or real data.  The
tests pin the additive schema-1 extension (legacy lines stay ``synthesized``), the
reasoned ``no_evidence`` consumption, conflict-aware appends and the no-false-anchor
rule.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

from core import sent_handoff  # noqa: E402
from core.sent_handoff import (  # noqa: E402
    LEDGER_FILENAME,
    SentHandoffError,
    SentLedgerConflict,
)

DAY = "2026-09-24"


def _line(**over) -> dict:
    base = {
        "schema_version": 1,
        "message_id": "x@example.test",
        "thread_key": "x@example.test",
        "vorgang_ref": "V-1",
        "role": "primary",
        "anchored_to": None,
        "recorded_at": "2026-11-09T09:30:00Z",
    }
    base.update(over)
    return base


def _handoff_item(mid: str, **over) -> dict:
    entry = {
        "schema_version": 1,
        "at": "2026-09-24T10:00:00Z",
        "mailbox": "ACC",
        "message_id": f"<{mid}>",
        "in_reply_to": "",
        "references": [],
        "subject": f"Subject {mid}",
        "from": "Owner <owner@example.test>",
        "to": ["partner@example.test"],
        "folder": "Sent Items",
        "sent_envelope_id": "101",
    }
    entry.update(over)
    return sent_handoff.build_handoff_item(
        entry, to=entry["to"], cc="", headers_complete=True,
    )


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dd = Path(tmp.name) / "data" / "mail-desk"
        self.dd.mkdir(parents=True)

    def write_ledger(self, *records: dict) -> bytes:
        path = self.dd / LEDGER_FILENAME
        path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
        return path.read_bytes()

    def ledger_records(self) -> list[dict]:
        path = self.dd / LEDGER_FILENAME
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]

    def record(self, mid: str) -> dict:
        return sent_handoff.load_ledger(self.dd)["by_message_id"][mid]


class LegacySchemaCompatibilityTests(_Base):
    def test_old_line_without_outcome_is_synthesized_and_still_anchors(self) -> None:
        self.write_ledger(_line(message_id="old@example.test", thread_key="root@example.test"))
        ledger = sent_handoff.load_ledger(self.dd)
        record = ledger["by_message_id"]["old@example.test"]
        self.assertEqual("synthesized", sent_handoff.ledger_outcome(record))
        self.assertEqual("synthesis", sent_handoff.ledger_source(record))
        self.assertEqual(["old@example.test"], list(ledger["all_by_message_id"]))
        anchor = sent_handoff.resolve_thread_anchor(
            {"norm_message_id": "new@example.test", "in_reply_to": "old@example.test",
             "references": [], "thread_key": "root@example.test", "anchor_candidates": []},
            ledger,
        )
        self.assertEqual(("V-1", "old@example.test", "in_reply_to"), (
            anchor["vorgang_ref"], anchor["matched_message_id"], anchor["match_type"],
        ))

    def test_new_no_evidence_line_is_valid_next_to_old_lines(self) -> None:
        self.write_ledger(_line(message_id="old@example.test"))
        sent_handoff.record_no_evidence(
            self.dd, [{"message_id": "<no@example.test>"}], reason_code="no_relevant_evidence",
        )
        ledger = sent_handoff.load_ledger(self.dd)
        self.assertEqual({"old@example.test", "no@example.test"}, set(ledger["by_message_id"]))
        no = ledger["by_message_id"]["no@example.test"]
        self.assertEqual(("no_evidence", "synthesis", "no_relevant_evidence"), (
            sent_handoff.ledger_outcome(no), sent_handoff.ledger_source(no), no["reason_code"],
        ))

    def test_minimal_old_line_remains_valid(self) -> None:
        minimal = {
            "schema_version": 1, "message_id": "m@example.test", "role": "primary",
            "recorded_at": "2026-11-09T09:30:00Z",
        }
        self.write_ledger(minimal)
        self.assertEqual({"m@example.test"}, set(sent_handoff.load_ledger(self.dd)["by_message_id"]))


class NoEvidenceTests(_Base):
    def test_no_evidence_consumes_handoff_without_evidence_or_anchor(self) -> None:
        sent_handoff.write_handoff(self.dd, [_handoff_item("m1@example.test")])
        self.assertEqual(1, sent_handoff.load_pending_handoff(self.dd)["pending_count"])

        added = sent_handoff.record_no_evidence(
            self.dd, [{"message_id": "<m1@example.test>"}], reason_code="no_matching_vorgang",
        )

        self.assertEqual(1, added)
        record = self.record("m1@example.test")
        self.assertEqual(("no_evidence", "primary", None, None, None), (
            sent_handoff.ledger_outcome(record), record["role"],
            record["vorgang_ref"], record["anchored_to"], record.get("evid_id"),
        ))
        self.assertEqual("no_matching_vorgang", record["reason_code"])
        self.assertEqual(0, sent_handoff.load_pending_handoff(self.dd)["pending_count"])
        self.assertEqual([], sent_handoff.load_pending_handoff(self.dd)["items"])

    def test_no_evidence_never_becomes_a_thread_anchor(self) -> None:
        sent_handoff.record_no_evidence(
            self.dd,
            [{"message_id": "<no@example.test>", "thread_key": "root@example.test"}],
            reason_code="thread_unresolved",
        )
        ledger = sent_handoff.load_ledger(self.dd)
        self.assertNotIn("root@example.test", ledger["by_thread_key"])
        anchor = sent_handoff.resolve_thread_anchor(
            {"norm_message_id": "reply@example.test", "in_reply_to": "no@example.test",
             "references": [], "thread_key": "root@example.test", "anchor_candidates": []},
            ledger,
        )
        self.assertIsNone(anchor)

        sent_handoff.record_synthesized(
            self.dd, [{"message_id": "<reply@example.test>", "in_reply_to": "<no@example.test>",
                       "references": ["<no@example.test>"]}],
        )
        reply = self.record("reply@example.test")
        self.assertEqual(("primary", None, None), (
            reply["role"], reply["vorgang_ref"], reply["anchored_to"],
        ))

    def test_no_evidence_does_not_hide_a_later_synthesized_thread_anchor(self) -> None:
        sent_handoff.record_synthesized(
            self.dd, [{"message_id": "<s@example.test>", "thread_key": "root@example.test"}],
            vorgang_ref="V-7",
        )
        sent_handoff.record_no_evidence(
            self.dd,
            [{"message_id": "<n@example.test>", "thread_key": "root@example.test"}],
            reason_code="no_relevant_evidence",
        )
        ledger = sent_handoff.load_ledger(self.dd)
        self.assertEqual("s@example.test", ledger["by_thread_key"]["root@example.test"]["message_id"])
        anchor = sent_handoff.resolve_thread_anchor(
            {"norm_message_id": "later@example.test", "in_reply_to": "",
             "references": [], "thread_key": "root@example.test", "anchor_candidates": []},
            ledger,
        )
        self.assertEqual(("V-7", "s@example.test"), (anchor["vorgang_ref"], anchor["matched_message_id"]))

    def test_no_evidence_is_idempotent(self) -> None:
        item = {"message_id": "<m@example.test>"}
        self.assertEqual(1, sent_handoff.record_no_evidence(
            self.dd, [item], reason_code="no_relevant_evidence",
        ))
        before = (self.dd / LEDGER_FILENAME).read_bytes()
        self.assertEqual(0, sent_handoff.record_no_evidence(
            self.dd, [item], reason_code="no_relevant_evidence",
        ))
        self.assertEqual(before, (self.dd / LEDGER_FILENAME).read_bytes())


class SynthesizedOutcomeTests(_Base):
    def test_synthesized_line_carries_outcome_source_and_evid(self) -> None:
        sent_handoff.record_synthesized(
            self.dd, [{"message_id": "<m@example.test>"}],
            vorgang_ref="V-7", evid_id="EVID-2026-0042",
        )
        record = self.record("m@example.test")
        self.assertEqual(("synthesized", "synthesis", "V-7", "EVID-2026-0042"), (
            sent_handoff.ledger_outcome(record), sent_handoff.ledger_source(record),
            record["vorgang_ref"], record["evid_id"],
        ))
        self.assertNotIn("reason_code", record)

    def test_old_call_shape_without_evid_remains_compatible(self) -> None:
        sent_handoff.record_synthesized(self.dd, [{"message_id": "<m@example.test>"}], vorgang_ref="V-1")
        record = self.record("m@example.test")
        self.assertEqual(("synthesized", "V-1", None), (
            sent_handoff.ledger_outcome(record), record["vorgang_ref"], record.get("evid_id"),
        ))

    def test_follow_up_with_evid_stays_anchored(self) -> None:
        sent_handoff.record_synthesized(
            self.dd, [{"message_id": "<root@example.test>"}], vorgang_ref="V-7", evid_id="E-1",
        )
        sent_handoff.record_synthesized(
            self.dd,
            [{"message_id": "<reply@example.test>", "in_reply_to": "<root@example.test>",
              "references": ["<root@example.test>"]}],
            evid_id="E-2",
        )
        reply = self.record("reply@example.test")
        self.assertEqual(("follow_up", "V-7", "root@example.test", "E-2"), (
            reply["role"], reply["vorgang_ref"], reply["anchored_to"], reply["evid_id"],
        ))


class ConflictTests(_Base):
    def test_synthesized_then_no_evidence_conflicts_without_first_wins(self) -> None:
        sent_handoff.record_synthesized(
            self.dd, [{"message_id": "<m@example.test>"}], vorgang_ref="V-1", evid_id="E-1",
        )
        before = (self.dd / LEDGER_FILENAME).read_bytes()
        with self.assertRaises(SentLedgerConflict) as caught:
            sent_handoff.record_no_evidence(
                self.dd, [{"message_id": "<m@example.test>"}], reason_code="no_relevant_evidence",
            )
        self.assertEqual("ledger_conflict", caught.exception.reason_code)
        self.assertEqual(before, (self.dd / LEDGER_FILENAME).read_bytes())

    def test_no_evidence_then_synthesized_conflicts(self) -> None:
        sent_handoff.record_no_evidence(
            self.dd, [{"message_id": "<m@example.test>"}], reason_code="no_relevant_evidence",
        )
        with self.assertRaises(SentLedgerConflict):
            sent_handoff.record_synthesized(self.dd, [{"message_id": "<m@example.test>"}], vorgang_ref="V-1")
        self.assertEqual(1, len(self.ledger_records()))

    def test_conflicting_duplicate_lines_block_a_new_operation(self) -> None:
        self.write_ledger(
            _line(message_id="m@example.test", vorgang_ref="V-1"),
            _line(message_id="m@example.test", vorgang_ref="V-2"),
        )
        # The documented read view still keeps the first line.
        self.assertEqual("V-1", self.record("m@example.test")["vorgang_ref"])
        with self.assertRaises(SentLedgerConflict):
            sent_handoff.record_synthesized(
                self.dd, [{"message_id": "<m@example.test>"}], vorgang_ref="V-1",
            )

    def test_identical_duplicate_lines_are_an_idempotent_skip(self) -> None:
        line = _line(message_id="m@example.test", vorgang_ref="V-1")
        self.write_ledger(line, line)
        before = (self.dd / LEDGER_FILENAME).read_bytes()
        self.assertEqual(0, sent_handoff.record_synthesized(
            self.dd, [{"message_id": "<m@example.test>"}], vorgang_ref="V-1",
        ))
        self.assertEqual(before, (self.dd / LEDGER_FILENAME).read_bytes())

    def test_old_repeat_without_optional_args_stays_idempotent(self) -> None:
        sent_handoff.record_synthesized(
            self.dd, [{"message_id": "<m@example.test>"}],
            vorgang_ref="V-1", evid_id="E-1",
        )
        before = (self.dd / LEDGER_FILENAME).read_bytes()
        self.assertEqual(0, sent_handoff.record_synthesized(
            self.dd, [{"message_id": "<m@example.test>"}],
        ))
        self.assertEqual(before, (self.dd / LEDGER_FILENAME).read_bytes())

    def test_anchor_resolved_vorgang_is_idempotent_without_optional_args(self) -> None:
        sent_handoff.record_synthesized(
            self.dd, [{"message_id": "<root@example.test>"}],
            vorgang_ref="V-1", evid_id="E-1",
        )
        reply = {
            "message_id": "<reply@example.test>", "in_reply_to": "<root@example.test>",
            "references": ["<root@example.test>"],
        }
        self.assertEqual(1, sent_handoff.record_synthesized(
            self.dd, [reply], evid_id="E-2",
        ))
        before = (self.dd / LEDGER_FILENAME).read_bytes()
        # The old call form repeats the same item without optional arguments.
        self.assertEqual(0, sent_handoff.record_synthesized(self.dd, [reply]))
        self.assertEqual(before, (self.dd / LEDGER_FILENAME).read_bytes())

    def test_later_derived_vorgang_does_not_break_an_old_noop_repeat(self) -> None:
        first = {"message_id": "<first@example.test>", "thread_key": "root@example.test"}
        self.assertEqual(1, sent_handoff.record_synthesized(self.dd, [first]))
        # A later message of the same thread introduces the Vorgang and refines
        # the thread index; the original first line still has no Vorgang.
        self.assertEqual(1, sent_handoff.record_synthesized(
            self.dd,
            [{"message_id": "<second@example.test>", "thread_key": "root@example.test"}],
            vorgang_ref="V-1",
        ))
        before = (self.dd / LEDGER_FILENAME).read_bytes()
        # Repeating the original call derives V-1 from the later anchor now;
        # that derived value must not turn the old no-op into a conflict.
        self.assertEqual(0, sent_handoff.record_synthesized(self.dd, [first]))
        self.assertEqual(before, (self.dd / LEDGER_FILENAME).read_bytes())
        self.assertEqual(
            "follow_up",
            sent_handoff.load_ledger(self.dd)["by_message_id"]["second@example.test"]["role"],
        )

    def test_explicit_conflicting_vorgang_or_evid_stays_a_conflict(self) -> None:
        sent_handoff.record_synthesized(
            self.dd, [{"message_id": "<m@example.test>"}],
            vorgang_ref="V-1", evid_id="E-1",
        )
        for overrides in ({"vorgang_ref": "V-2"}, {"evid_id": "E-2"}):
            with self.subTest(overrides=overrides):
                with self.assertRaises(SentLedgerConflict):
                    sent_handoff.record_synthesized(
                        self.dd, [{"message_id": "<m@example.test>"}], **overrides,
                    )

    def test_explicit_value_against_missing_existing_value_conflicts(self) -> None:
        sent_handoff.record_synthesized(self.dd, [{"message_id": "<m@example.test>"}])
        for overrides in ({"vorgang_ref": "V-1"}, {"evid_id": "E-1"}):
            with self.subTest(overrides=overrides):
                with self.assertRaises(SentLedgerConflict):
                    sent_handoff.record_synthesized(
                        self.dd, [{"message_id": "<m@example.test>"}], **overrides,
                    )

    def test_repeated_no_evidence_with_same_reason_stays_idempotent(self) -> None:
        item = {"message_id": "<n@example.test>"}
        self.assertEqual(1, sent_handoff.record_no_evidence(
            self.dd, [item], reason_code="no_relevant_evidence",
        ))
        before = (self.dd / LEDGER_FILENAME).read_bytes()
        # The reason is required for the first append; a repeat is satisfied by
        # the durable line's identity only when the same reason is supplied.
        self.assertEqual(0, sent_handoff.record_no_evidence(
            self.dd, [item], reason_code="no_relevant_evidence",
        ))
        self.assertEqual(before, (self.dd / LEDGER_FILENAME).read_bytes())



class ValidationTests(_Base):
    BAD = {
        "invalid_outcome": _line(outcome="maybe"),
        "invalid_source": _line(source="manual"),
        "reason_on_synthesized": _line(reason_code="no_relevant_evidence"),
        "no_evidence_without_reason": _line(outcome="no_evidence", vorgang_ref=None),
        "no_evidence_with_evid": _line(outcome="no_evidence", vorgang_ref=None, evid_id="E-1",
                                        reason_code="no_relevant_evidence"),
        "no_evidence_with_vorgang": _line(outcome="no_evidence", vorgang_ref="V-1",
                                          reason_code="no_relevant_evidence"),
        "no_evidence_with_anchor": _line(outcome="no_evidence", vorgang_ref=None,
                                         anchored_to="a@example.test",
                                         reason_code="no_relevant_evidence"),
        "no_evidence_follow_up": _line(outcome="no_evidence", vorgang_ref=None, role="follow_up",
                                       anchored_to="a@example.test",
                                       reason_code="no_relevant_evidence"),
        "legacy_synth_missing_vorgang": _line(source="legacy_import", vorgang_ref=None, evid_id="E-1"),
        "legacy_synth_missing_evid": _line(source="legacy_import", evid_id=None),
        "legacy_no_evidence_with_evid": _line(source="legacy_import", outcome="no_evidence",
                                              vorgang_ref=None, evid_id="E-1",
                                              reason_code="no_relevant_evidence"),
        "bad_reason_token": _line(outcome="no_evidence", vorgang_ref=None,
                                  reason_code="Not A Token"),
        "bad_evid_token": _line(evid_id=" E-1"),
    }

    def test_every_schema_violation_is_ledger_invalid(self) -> None:
        good = json.dumps(_line(message_id="good@example.test"))
        for name, bad in self.BAD.items():
            with self.subTest(name):
                (self.dd / LEDGER_FILENAME).write_text(
                    good + "\n" + json.dumps(bad) + "\n", encoding="utf-8",
                )
                with self.assertRaises(SentHandoffError) as caught:
                    sent_handoff.load_ledger(self.dd)
                self.assertEqual("ledger_invalid", caught.exception.reason_code)
                self.assertEqual([2], caught.exception.details["invalid_lines"])

    def test_record_no_evidence_rejects_missing_reason_before_write(self) -> None:
        with self.assertRaises(ValueError):
            sent_handoff.record_no_evidence(
                self.dd, [{"message_id": "<m@example.test>"}], reason_code="",
            )
        self.assertFalse((self.dd / LEDGER_FILENAME).exists())

    def test_record_synthesized_rejects_reason_and_no_evidence_evid(self) -> None:
        with self.assertRaises(ValueError):
            sent_handoff.record_outcome(
                self.dd, [{"message_id": "<m@example.test>"}],
                outcome="synthesized", reason_code="no_relevant_evidence",
            )
        with self.assertRaises(ValueError):
            sent_handoff.record_outcome(
                self.dd, [{"message_id": "<m@example.test>"}],
                outcome="no_evidence", reason_code="no_relevant_evidence", evid_id="E-1",
            )


class AppendRecordsTests(_Base):
    def test_legacy_import_records_append_and_validate(self) -> None:
        synthesized = {
            "schema_version": 1, "message_id": "s@example.test", "thread_key": "root@example.test",
            "vorgang_ref": "V-1", "role": "primary", "anchored_to": None,
            "recorded_at": "2026-10-01T10:00:00Z", "outcome": "synthesized",
            "source": "legacy_import", "evid_id": "E-1",
        }
        no_evidence = {
            "schema_version": 1, "message_id": "n@example.test", "thread_key": "n@example.test",
            "vorgang_ref": None, "role": "primary", "anchored_to": None,
            "recorded_at": "2026-10-01T10:00:00Z", "outcome": "no_evidence",
            "source": "legacy_import", "reason_code": "legacy_status_no_evidence",
        }
        self.assertEqual(2, sent_handoff.append_ledger_records(self.dd, [synthesized, no_evidence]))
        ledger = sent_handoff.load_ledger(self.dd)
        self.assertEqual("legacy_import", sent_handoff.ledger_source(ledger["by_message_id"]["s@example.test"]))
        self.assertEqual("s@example.test", ledger["by_thread_key"]["root@example.test"]["message_id"])
        self.assertNotIn("n@example.test", ledger["by_thread_key"])

    def test_append_records_rejects_invalid_before_write(self) -> None:
        bad = {
            "schema_version": 1, "message_id": "m@example.test", "role": "primary",
            "recorded_at": "2026-10-01T10:00:00Z", "source": "legacy_import",
        }
        with self.assertRaises(ValueError):
            sent_handoff.append_ledger_records(self.dd, [bad])
        self.assertFalse((self.dd / LEDGER_FILENAME).exists())

    def test_append_records_is_idempotent_and_conflict_aware(self) -> None:
        record = {
            "schema_version": 1, "message_id": "m@example.test", "vorgang_ref": "V-1",
            "role": "primary", "anchored_to": None, "recorded_at": "2026-10-01T10:00:00Z",
            "outcome": "synthesized", "source": "legacy_import", "evid_id": "E-1",
        }
        self.assertEqual(1, sent_handoff.append_ledger_records(self.dd, [record]))
        self.assertEqual(0, sent_handoff.append_ledger_records(self.dd, [record]))
        with self.assertRaises(SentLedgerConflict):
            sent_handoff.append_ledger_records(self.dd, [{**record, "evid_id": "E-2"}])

    def test_torn_tail_is_repaired_before_no_evidence_append(self) -> None:
        path = self.dd / LEDGER_FILENAME
        path.write_text(json.dumps(_line(message_id="old@example.test")) + "\n{\"schema_ver", encoding="utf-8")
        self.assertTrue(sent_handoff.load_ledger(self.dd)["torn_tail"])
        sent_handoff.record_no_evidence(
            self.dd, [{"message_id": "<n@example.test>"}], reason_code="no_relevant_evidence",
        )
        self.assertEqual(
            {"old@example.test", "n@example.test"},
            {record["message_id"] for record in self.ledger_records()},
        )
        self.assertEqual('{"schema_ver\n', (self.dd / (LEDGER_FILENAME + ".torn")).read_text("utf-8"))

    def test_hardlinked_ledger_alias_is_refused_for_no_evidence(self) -> None:
        external = self.dd.parent / "external-ledger.jsonl"
        external.write_text("", encoding="utf-8")
        before = external.read_bytes()
        try:
            os.link(external, self.dd / LEDGER_FILENAME)
        except (OSError, NotImplementedError):
            self.skipTest("hardlinks are unavailable")
        with self.assertRaises(SentHandoffError) as caught:
            sent_handoff.record_no_evidence(
                self.dd, [{"message_id": "<m@example.test>"}], reason_code="no_relevant_evidence",
            )
        self.assertEqual("ledger_alias_invalid", caught.exception.reason_code)
        self.assertEqual(before, external.read_bytes())


if __name__ == "__main__":
    unittest.main()
