"""Issue #107: crash-idempotent promotion recovery with the exact triad.

Hermetic and offline.  ``promotion_evidence`` is a synthetic durable-promotion
inventory; the tests never read a consumer workspace or real evidence.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

from core import sent_handoff, sent_ledger  # noqa: E402
from core.sent_handoff import LEDGER_FILENAME  # noqa: E402
from core.sent_ledger import SentLedgerError  # noqa: E402


def _ledger_record(mid: str, **over) -> dict:
    base = {
        "schema_version": 1,
        "message_id": mid,
        "vorgang_ref": "V-1",
        "role": "primary",
        "anchored_to": None,
        "recorded_at": "2026-10-01T10:00:00Z",
        "outcome": "synthesized",
        "source": "synthesis",
        "evid_id": "E-1",
    }
    base.update(over)
    return base


def _evidence(mid: str, **over) -> dict:
    base = {"message_id": f"<{mid}>", "vorgang_ref": "V-1", "evid_id": "E-1"}
    base.update(over)
    return base


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws = Path(tmp.name)
        self.dd = self.ws / "data" / "mail-desk"
        self.dd.mkdir(parents=True)

    def write_ledger(self, *records: dict) -> bytes:
        path = self.dd / LEDGER_FILENAME
        path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
        return path.read_bytes()

    def ledger_bytes(self) -> bytes | None:
        path = self.dd / LEDGER_FILENAME
        return path.read_bytes() if path.exists() else None

    def ledger_records(self) -> list[dict]:
        path = self.dd / LEDGER_FILENAME
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]

    def recover(self, *, mid="m@example.test", ref="V-1", evid="E-1", evidence=None, verifier=None):
        records = evidence if evidence is not None else [_evidence(mid, vorgang_ref=ref, evid_id=evid)]
        check = verifier or (lambda *args, **kwargs: object())
        with patch.object(sent_ledger, "verify_workspace_lock", side_effect=check):
            return sent_ledger.recover_promotion(
                self.dd, message_id=f"<{mid}>", vorgang_ref=ref, evid_id=evid,
                promotion_evidence=records, workspace_root=self.ws,
                lease_id="LEASE-1", conversation_id="CONV-1",
            )


class RecoveryTests(_Base):
    def test_unique_triad_appends_exactly_one_ledger_line(self) -> None:
        result = self.recover()
        self.assertEqual(("recovered", 1), (result["status"], result["appended"]))
        self.assertEqual(1, len(self.ledger_records()))
        record = self.ledger_records()[0]
        self.assertEqual(("synthesized", "synthesis", "V-1", "E-1", "primary", None), (
            record["outcome"], record["source"], record["vorgang_ref"],
            record["evid_id"], record["role"], record["anchored_to"],
        ))
        self.assertNotIn("reason_code", record)

    def test_repeat_after_recovery_is_already_consumed_without_append(self) -> None:
        self.recover()
        before = self.ledger_bytes()
        repeat = self.recover()
        self.assertEqual(("already_consumed", 0), (repeat["status"], repeat["appended"]))
        self.assertEqual(before, self.ledger_bytes())

    def test_recovery_anchors_a_reply_to_its_recorded_ancestor(self) -> None:
        sent_handoff.record_synthesized(
            self.dd, [{"message_id": "<root@example.test>"}], vorgang_ref="V-1", evid_id="E-1",
        )
        result = self.recover(
            mid="reply@example.test", ref="V-1", evid="E-2",
            evidence=[_evidence(
                "reply@example.test", vorgang_ref="V-1", evid_id="E-2",
                in_reply_to="<root@example.test>",
            )],
        )
        self.assertEqual(1, result["appended"])
        reply = sent_handoff.load_ledger(self.dd)["by_message_id"]["reply@example.test"]
        self.assertEqual(("follow_up", "V-1", "root@example.test"), (
            reply["role"], reply["vorgang_ref"], reply["anchored_to"],
        ))

    def test_recovery_stops_when_reply_triad_differs_from_ancestor_vorgang(self) -> None:
        sent_handoff.record_synthesized(
            self.dd, [{"message_id": "<root@example.test>"}], vorgang_ref="V-OLD", evid_id="E-1",
        )
        before = self.ledger_bytes()
        with self.assertRaises(SentLedgerError) as caught:
            self.recover(
                mid="reply@example.test", ref="V-NEW", evid="E-2",
                evidence=[_evidence(
                    "reply@example.test", vorgang_ref="V-NEW", evid_id="E-2",
                    in_reply_to="<root@example.test>",
                )],
            )
        self.assertEqual("promotion_anchor_conflict", caught.exception.reason_code)
        self.assertEqual(before, self.ledger_bytes())

    def test_recovery_stops_on_a_thread_key_anchor_with_another_vorgang(self) -> None:
        sent_handoff.record_synthesized(
            self.dd,
            [{"message_id": "<root@example.test>", "thread_key": "root@example.test"}],
            vorgang_ref="V-OLD", evid_id="E-1",
        )
        before = self.ledger_bytes()
        with self.assertRaises(SentLedgerError) as caught:
            self.recover(
                mid="reply@example.test", ref="V-NEW", evid="E-2",
                evidence=[_evidence(
                    "reply@example.test", vorgang_ref="V-NEW", evid_id="E-2",
                    thread_key="root@example.test",
                )],
            )
        self.assertEqual("promotion_anchor_conflict", caught.exception.reason_code)
        self.assertEqual(before, self.ledger_bytes())

    def test_no_matching_evidence_stops(self) -> None:
        with self.assertRaises(SentLedgerError) as caught:
            self.recover(evidence=[_evidence("other@example.test", vorgang_ref="V-1", evid_id="E-1")])
        self.assertEqual("promotion_not_found", caught.exception.reason_code)
        self.assertIsNone(self.ledger_bytes())

    def test_multiple_evidence_records_for_the_message_are_ambiguous(self) -> None:
        with self.assertRaises(SentLedgerError) as caught:
            self.recover(evidence=[
                _evidence("m@example.test"), _evidence("m@example.test"),
            ])
        self.assertEqual("promotion_ambiguous", caught.exception.reason_code)
        self.assertIsNone(self.ledger_bytes())

    def test_partial_triad_match_is_a_conflict(self) -> None:
        for evidence in (
            [_evidence("m@example.test", vorgang_ref="V-OTHER")],
            [_evidence("m@example.test", evid_id="E-OTHER")],
            [{"message_id": "<m@example.test>"}],
        ):
            with self.subTest(evidence=evidence):
                with self.assertRaises(SentLedgerError) as caught:
                    self.recover(evidence=evidence)
                self.assertEqual("promotion_conflict", caught.exception.reason_code)
                self.assertIsNone(self.ledger_bytes())

    def test_incomplete_triad_is_refused_before_lease_or_write(self) -> None:
        with patch.object(sent_ledger, "verify_workspace_lock") as verify:
            with self.assertRaises(SentLedgerError) as caught:
                sent_ledger.recover_promotion(
                    self.dd, message_id="", vorgang_ref="V-1", evid_id="E-1",
                    promotion_evidence=[_evidence("m@example.test")],
                )
        self.assertEqual("promotion_recovery_incomplete", caught.exception.reason_code)
        verify.assert_not_called()
        self.assertIsNone(self.ledger_bytes())

    def test_missing_lease_denies_before_write(self) -> None:
        with patch.object(sent_ledger, "verify_workspace_lock", side_effect=RuntimeError("no lock")):
            with self.assertRaises(SentLedgerError) as caught:
                sent_ledger.recover_promotion(
                    self.dd, message_id="m@example.test", vorgang_ref="V-1",
                    evid_id="E-1", promotion_evidence=[_evidence("m@example.test")],
                    workspace_root=self.ws,
                )
        self.assertEqual("workspace_lease_required", caught.exception.reason_code)
        self.assertIsNone(self.ledger_bytes())

    def test_recovery_rejects_a_lease_for_workspace_a_with_data_dir_in_b(self) -> None:
        other_ws = self.ws / "other-recovery-workspace"
        other_dd = other_ws / "data" / "mail-desk"
        other_dd.mkdir(parents=True)
        with patch.object(sent_ledger, "verify_workspace_lock") as verify:
            with self.assertRaises(SentLedgerError) as caught:
                sent_ledger.recover_promotion(
                    other_dd, message_id="m@example.test", vorgang_ref="V-1",
                    evid_id="E-1", promotion_evidence=[_evidence("m@example.test")],
                    workspace_root=self.ws, lease_id="LEASE-1", conversation_id="CONV-1",
                )
        self.assertEqual("workspace_binding_invalid", caught.exception.reason_code)
        verify.assert_not_called()
        self.assertFalse((other_dd / LEDGER_FILENAME).exists())

    def test_conflicting_ledger_line_stops_without_first_wins(self) -> None:
        self.write_ledger(_ledger_record("m@example.test", evid_id="E-9"))
        before = self.ledger_bytes()
        with self.assertRaises(SentLedgerError) as caught:
            self.recover()
        self.assertEqual("ledger_conflict", caught.exception.reason_code)
        self.assertEqual(before, self.ledger_bytes())

    def test_no_evidence_ledger_line_is_not_a_synthesis_match(self) -> None:
        self.write_ledger(_ledger_record(
            "m@example.test", outcome="no_evidence", vorgang_ref=None,
            evid_id=None, reason_code="no_relevant_evidence",
        ))
        with self.assertRaises(SentLedgerError) as caught:
            self.recover()
        self.assertEqual("ledger_conflict", caught.exception.reason_code)
        self.assertEqual(1, len(self.ledger_records()))

    def test_duplicate_ledger_lines_are_ambiguous(self) -> None:
        self.write_ledger(
            _ledger_record("m@example.test", evid_id="E-1"),
            _ledger_record("m@example.test", evid_id="E-1"),
        )
        with self.assertRaises(SentLedgerError) as caught:
            self.recover()
        self.assertEqual("ledger_ambiguous", caught.exception.reason_code)

    def test_evid_shared_with_another_message_conflicts(self) -> None:
        self.write_ledger(_ledger_record("other@example.test", evid_id="E-1"))
        with self.assertRaises(SentLedgerError) as caught:
            self.recover()
        self.assertEqual("evid_shared_conflict", caught.exception.reason_code)
        self.assertEqual(1, len(self.ledger_records()))

    def test_evid_shared_with_other_evidence_conflicts_before_ledger_write(self) -> None:
        with self.assertRaises(SentLedgerError) as caught:
            self.recover(evidence=[
                _evidence("m@example.test", vorgang_ref="V-1", evid_id="E-1"),
                _evidence("other@example.test", vorgang_ref="V-2", evid_id="E-1"),
            ])
        self.assertEqual("evid_shared_conflict", caught.exception.reason_code)
        self.assertIsNone(self.ledger_bytes())

    def test_competing_evidence_with_same_evid_but_no_message_id_stops(self) -> None:
        with self.assertRaises(SentLedgerError) as caught:
            self.recover(evidence=[
                _evidence("m@example.test", vorgang_ref="V-1", evid_id="E-1"),
                {"vorgang_ref": "other", "evid_id": "E-1"},
            ])
        self.assertEqual("promotion_recovery_incomplete", caught.exception.reason_code)
        self.assertEqual(
            "competing_evidence_missing_message_id", caught.exception.details["reason"],
        )
        self.assertIsNone(self.ledger_bytes())

    def test_already_consumed_does_not_hide_a_foreign_ledger_evid_owner(self) -> None:
        self.write_ledger(
            _ledger_record("m@example.test", vorgang_ref="V-1", evid_id="E-1"),
            _ledger_record("other@example.test", vorgang_ref="V-2", evid_id="E-1"),
        )
        before = self.ledger_bytes()
        with self.assertRaises(SentLedgerError) as caught:
            self.recover()
        self.assertEqual("evid_shared_conflict", caught.exception.reason_code)
        self.assertEqual(before, self.ledger_bytes())

    def test_exact_token_matching_is_case_sensitive_but_message_id_is_normalized(self) -> None:
        with self.assertRaises(SentLedgerError) as caught:
            self.recover(
                mid="M@EXAMPLE.TEST", ref="v-1", evid="E-1",
                evidence=[_evidence("m@example.test", vorgang_ref="V-1", evid_id="E-1")],
            )
        # Message-ID normalizes (match); the Vorgang token does not case-fold.
        self.assertEqual("promotion_conflict", caught.exception.reason_code)

        result = self.recover(
            mid="M@EXAMPLE.TEST", ref="V-1", evid="E-1",
            evidence=[_evidence("m@example.test", vorgang_ref="V-1", evid_id="E-1")],
        )
        self.assertEqual(("recovered", 1), (result["status"], result["appended"]))
        self.assertIn("m@example.test", sent_handoff.load_ledger(self.dd)["by_message_id"])

    def test_torn_ledger_tail_is_repaired_and_preserved(self) -> None:
        path = self.dd / LEDGER_FILENAME
        path.write_text(
            json.dumps(_ledger_record("old@example.test", evid_id="E-OLD")) + "\n{\"schema_ver",
            encoding="utf-8",
        )
        self.recover()
        self.assertEqual(
            {"old@example.test", "m@example.test"},
            {record["message_id"] for record in self.ledger_records()},
        )
        self.assertEqual('{"schema_ver\n', (self.dd / (LEDGER_FILENAME + ".torn")).read_text("utf-8"))

    def test_hardlinked_ledger_alias_is_refused_before_recovery(self) -> None:
        external = self.ws / "external-ledger.jsonl"
        external.write_text("", encoding="utf-8")
        before = external.read_bytes()
        try:
            os.link(external, self.dd / LEDGER_FILENAME)
        except (OSError, NotImplementedError):
            self.skipTest("hardlinks are unavailable")
        with self.assertRaises(sent_handoff.SentHandoffError) as caught:
            self.recover()
        self.assertEqual("ledger_alias_invalid", caught.exception.reason_code)
        self.assertEqual(before, external.read_bytes())


if __name__ == "__main__":
    unittest.main()
