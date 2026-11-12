"""Issue #107: idempotent legacy import with dry run, conflicts and approval.

Hermetic and offline.  No real legacy data, mailbox or consumer access; all
inventories and ledgers are synthetic temporary fixtures.
"""

from __future__ import annotations

import json
import os
import subprocess
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


def _make_dir_link(link: Path, target: Path) -> bool:
    """Create a directory symlink or Windows junction for binding tests."""
    try:
        link.symlink_to(target, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        if os.name == "nt":
            try:
                subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                    check=True, capture_output=True,
                )
                return True
            except (OSError, subprocess.CalledProcessError):
                return False
        return False


def _inventory_record(mid: str, **over) -> dict:
    base = {
        "schema_version": 1,
        "message_id": f"<{mid}>",
        "status": "synthesized",
        "vorgang_ref": "V-1",
        "evid_id": "E-1",
        "reason_code": None,
        "recorded_at": "2026-10-01T10:00:00Z",
    }
    base.update(over)
    return base


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


def _handoff_item(mid: str) -> dict:
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
    return sent_handoff.build_handoff_item(entry, to=entry["to"], cc="", headers_complete=True)


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws = Path(tmp.name)
        self.dd = self.ws / "data" / "mail-desk"
        self.dd.mkdir(parents=True)
        self.inventory = self.ws / "legacy-inventory.jsonl"

    def write_inventory(self, *records: dict) -> None:
        self.inventory.write_text(
            "\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8",
        )

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

    def apply(self, token: str | None, *, verifier=None):
        check = verifier or (lambda *args, **kwargs: object())
        with patch.object(sent_ledger, "verify_workspace_lock", side_effect=check):
            return sent_ledger.apply_legacy_import(
                self.dd, self.inventory, approval=token, workspace_root=self.ws,
                lease_id="LEASE-1", conversation_id="CONV-1",
            )


class DryRunTests(_Base):
    def test_dry_run_reports_imports_already_present_and_conflicts_without_writes(self) -> None:
        self.write_ledger(
            _ledger_record("already@example.test"),
            _ledger_record("conflict@example.test", vorgang_ref="V-9", evid_id="E-9"),
        )
        self.write_inventory(
            _inventory_record("new@example.test", evid_id="E-NEW"),
            _inventory_record("already@example.test"),
            _inventory_record("conflict@example.test", evid_id="E-CONFLICT"),
            _inventory_record(
                "noev@example.test", status="no_evidence", vorgang_ref=None,
                evid_id=None, reason_code="no_relevant_evidence",
            ),
            _inventory_record("missing-evid@example.test", evid_id=None),
        )
        before = self.ledger_bytes()

        plan = sent_ledger.build_legacy_import_plan(self.dd, self.inventory)

        self.assertTrue(plan["dry_run"])
        self.assertTrue(plan["blocked"])
        self.assertEqual(before, self.ledger_bytes())
        actions = {entry["message_id"]: entry["action"] for entry in plan["entries"]}
        self.assertEqual("import", actions["new@example.test"])
        self.assertEqual("import", actions["noev@example.test"])
        self.assertEqual("already_present", actions["already@example.test"])
        noev = next(e for e in plan["entries"] if e["message_id"] == "noev@example.test")
        self.assertEqual(("no_evidence", "no_relevant_evidence", None, None), (
            noev["outcome"], noev["reason_code"], noev["vorgang_ref"], noev["evid_id"],
        ))
        codes = {(c["message_id"], c["code"]) for c in plan["conflicts"]}
        self.assertIn(("conflict@example.test", "ledger_conflict"), codes)
        self.assertIn(("missing-evid@example.test", "missing_evid_id"), codes)
        self.assertIsNone(plan["approval"]["token"])
        # No report special file: the report is the returned value only.
        self.assertEqual({LEDGER_FILENAME}, {path.name for path in self.dd.iterdir()})

    def test_plan_id_is_stable_and_binds_inventory_ledger_and_handoff(self) -> None:
        self.write_inventory(_inventory_record("a@example.test"))
        first = sent_ledger.build_legacy_import_plan(self.dd, self.inventory)
        self.assertEqual(first["plan_id"], sent_ledger.build_legacy_import_plan(self.dd, self.inventory)["plan_id"])

        self.write_inventory(
            _inventory_record("a@example.test"), _inventory_record("b@example.test", evid_id="E-B"),
        )
        added = sent_ledger.build_legacy_import_plan(self.dd, self.inventory)
        self.assertNotEqual(first["plan_id"], added["plan_id"])

        self.write_ledger(_ledger_record("unrelated@example.test", evid_id="E-LEDGER"))
        ledger_changed = sent_ledger.build_legacy_import_plan(self.dd, self.inventory)
        self.assertNotEqual(added["plan_id"], ledger_changed["plan_id"])

        sent_handoff.write_handoff(self.dd, [])
        handoff_changed = sent_ledger.build_legacy_import_plan(self.dd, self.inventory)
        self.assertNotEqual(ledger_changed["plan_id"], handoff_changed["plan_id"])

    def test_dry_run_does_not_call_the_lease_verifier(self) -> None:
        self.write_inventory(_inventory_record("a@example.test"))
        with patch.object(sent_ledger, "verify_workspace_lock") as verify:
            sent_ledger.build_legacy_import_plan(self.dd, self.inventory)
        verify.assert_not_called()

    def test_invalid_inventory_fails_closed_without_a_plan(self) -> None:
        cases = {
            "unknown_field": _inventory_record("a@example.test", extra=1),
            "missing_message_id": {k: v for k, v in _inventory_record("a@example.test").items()
                                   if k != "message_id"},
            "bad_status": _inventory_record("a@example.test", status="open"),
        }
        for name, record in cases.items():
            with self.subTest(name):
                self.write_inventory(record)
                with self.assertRaises(SentLedgerError) as caught:
                    sent_ledger.build_legacy_import_plan(self.dd, self.inventory)
                self.assertEqual("legacy_inventory_invalid", caught.exception.reason_code)
                self.assertIsNone(self.ledger_bytes())

    def test_invalid_json_line_fails_closed(self) -> None:
        self.inventory.write_text("{not json\n", encoding="utf-8")
        with self.assertRaises(SentLedgerError) as caught:
            sent_ledger.build_legacy_import_plan(self.dd, self.inventory)
        self.assertEqual("legacy_inventory_invalid", caught.exception.reason_code)

    def test_conflicting_duplicate_inventory_ids_block(self) -> None:
        self.write_inventory(
            _inventory_record("a@example.test", vorgang_ref="V-1"),
            _inventory_record("a@example.test", vorgang_ref="V-2"),
        )
        plan = sent_ledger.build_legacy_import_plan(self.dd, self.inventory)
        self.assertTrue(plan["blocked"])
        self.assertIn(
            ("a@example.test", "duplicate_inventory_conflict"),
            {(c["message_id"], c["code"]) for c in plan["conflicts"]},
        )

    def test_conflicting_duplicate_ledger_lines_block_even_with_first_wins(self) -> None:
        self.write_ledger(
            _ledger_record("a@example.test", vorgang_ref="V-1"),
            _ledger_record("a@example.test", vorgang_ref="V-2"),
        )
        self.write_inventory(_inventory_record("a@example.test", vorgang_ref="V-1"))
        plan = sent_ledger.build_legacy_import_plan(self.dd, self.inventory)
        self.assertTrue(plan["blocked"])
        self.assertIn(
            ("a@example.test", "existing_ledger_conflict"),
            {(c["message_id"], c["code"]) for c in plan["conflicts"]},
        )

    def test_legacy_line_without_evid_is_valid_but_conflicts_with_imported_evidence(self) -> None:
        # A schema-1 line without outcome/source/evid stays readable ...
        old = {
            "schema_version": 1, "message_id": "a@example.test", "vorgang_ref": "V-1",
            "role": "primary", "anchored_to": None, "recorded_at": "2026-10-01T10:00:00Z",
        }
        self.write_ledger(old)
        self.assertEqual(
            "synthesized", sent_handoff.ledger_outcome(
                sent_handoff.load_ledger(self.dd)["by_message_id"]["a@example.test"]
            ),
        )
        # ... but the import must not claim an EVID anchor the line does not carry.
        self.write_inventory(_inventory_record("a@example.test", vorgang_ref="V-1", evid_id="E-1"))
        plan = sent_ledger.build_legacy_import_plan(self.dd, self.inventory)
        self.assertTrue(plan["blocked"])
        self.assertIn(
            ("a@example.test", "ledger_conflict"),
            {(c["message_id"], c["code"]) for c in plan["conflicts"]},
        )

    def test_inventory_evid_shared_by_two_message_ids_blocks(self) -> None:
        self.write_inventory(
            _inventory_record("a@example.test", evid_id="E-SHARED"),
            _inventory_record("b@example.test", evid_id="E-SHARED"),
        )
        plan = sent_ledger.build_legacy_import_plan(self.dd, self.inventory)
        self.assertTrue(plan["blocked"])
        self.assertEqual(
            {("a@example.test", "evid_shared_conflict"),
             ("b@example.test", "evid_shared_conflict")},
            {(c["message_id"], c["code"]) for c in plan["conflicts"]},
        )
        self.assertIsNone(self.ledger_bytes())

    def test_inventory_evid_owned_by_another_ledger_message_blocks(self) -> None:
        self.write_ledger(_ledger_record("a@example.test", evid_id="E-SHARED"))
        self.write_inventory(_inventory_record("b@example.test", evid_id="E-SHARED"))
        before = self.ledger_bytes()
        plan = sent_ledger.build_legacy_import_plan(self.dd, self.inventory)
        self.assertTrue(plan["blocked"])
        self.assertIn(
            ("b@example.test", "evid_shared_conflict"),
            {(c["message_id"], c["code"]) for c in plan["conflicts"]},
        )
        self.assertEqual(before, self.ledger_bytes())

    def test_plan_id_binds_target_workspace_even_with_identical_state(self) -> None:
        ws_a = self.ws / "ws-a"
        ws_b = self.ws / "ws-b"
        dd_a = ws_a / "data" / "mail-desk"
        dd_b = ws_b / "data" / "mail-desk"
        dd_a.mkdir(parents=True)
        dd_b.mkdir(parents=True)
        self.write_inventory(_inventory_record("a@example.test"))

        plan_a = sent_ledger.build_legacy_import_plan(dd_a, self.inventory)
        plan_b = sent_ledger.build_legacy_import_plan(dd_b, self.inventory)
        self.assertNotEqual(plan_a["target_workspace_id"], plan_b["target_workspace_id"])
        self.assertNotEqual(plan_a["plan_id"], plan_b["plan_id"])
        self.assertNotIn(str(dd_a), json.dumps(plan_a))
        self.assertNotIn(str(dd_b), json.dumps(plan_b))

        with patch.object(sent_ledger, "verify_workspace_lock", return_value=object()):
            with self.assertRaises(SentLedgerError) as caught:
                sent_ledger.apply_legacy_import(
                    dd_b, self.inventory, approval=plan_a["plan_id"],
                    workspace_root=ws_b, lease_id="LEASE-B", conversation_id="CONV-B",
                )
        self.assertEqual("legacy_import_approval_stale", caught.exception.reason_code)
        self.assertFalse((dd_b / LEDGER_FILENAME).exists())
        self.assertFalse((dd_a / LEDGER_FILENAME).exists())

    def test_hardlinked_ledger_alias_blocks_before_any_read_or_write(self) -> None:
        external = self.ws / "external-ledger.jsonl"
        external.write_text("", encoding="utf-8")
        before = external.read_bytes()
        try:
            os.link(external, self.dd / LEDGER_FILENAME)
        except (OSError, NotImplementedError):
            self.skipTest("hardlinks are unavailable")
        self.write_inventory(_inventory_record("a@example.test"))
        with self.assertRaises(sent_handoff.SentHandoffError) as caught:
            sent_ledger.build_legacy_import_plan(self.dd, self.inventory)
        self.assertEqual("ledger_alias_invalid", caught.exception.reason_code)
        self.assertEqual(before, external.read_bytes())


class ApplyTests(_Base):
    def test_apply_requires_the_exact_dry_run_token_and_a_verified_lease(self) -> None:
        self.write_inventory(_inventory_record("a@example.test"))
        plan = sent_ledger.build_legacy_import_plan(self.dd, self.inventory)
        self.assertFalse(plan["blocked"])

        with self.assertRaises(SentLedgerError) as missing:
            self.apply(None)
        self.assertEqual("legacy_import_approval_required", missing.exception.reason_code)
        self.assertIsNone(self.ledger_bytes())

        with patch.object(sent_ledger, "verify_workspace_lock") as verify:
            with self.assertRaises(SentLedgerError) as stale:
                sent_ledger.apply_legacy_import(
                    self.dd, self.inventory, approval="sha256:not-the-plan",
                    workspace_root=self.ws, lease_id="LEASE-1", conversation_id="CONV-1",
                )
        self.assertEqual("legacy_import_approval_stale", stale.exception.reason_code)
        verify.assert_called_once()
        self.assertIsNone(self.ledger_bytes())

    def test_missing_lease_denies_before_any_write(self) -> None:
        self.write_inventory(_inventory_record("a@example.test"))
        plan = sent_ledger.build_legacy_import_plan(self.dd, self.inventory)
        with patch.object(sent_ledger, "verify_workspace_lock", side_effect=RuntimeError("no lock")):
            with self.assertRaises(SentLedgerError) as caught:
                sent_ledger.apply_legacy_import(
                    self.dd, self.inventory, approval=plan["plan_id"],
                    workspace_root=self.ws, lease_id=None, conversation_id=None,
                )
        self.assertEqual("workspace_lease_required", caught.exception.reason_code)
        self.assertIsNone(self.ledger_bytes())

    def test_apply_rejects_a_lease_for_workspace_a_with_data_dir_in_b(self) -> None:
        other_ws = self.ws / "other-workspace"
        other_dd = other_ws / "data" / "mail-desk"
        other_dd.mkdir(parents=True)
        self.write_inventory(_inventory_record("a@example.test"))
        plan = sent_ledger.build_legacy_import_plan(self.dd, self.inventory)
        with patch.object(sent_ledger, "verify_workspace_lock") as verify:
            with self.assertRaises(SentLedgerError) as caught:
                sent_ledger.apply_legacy_import(
                    other_dd, self.inventory, approval=plan["plan_id"],
                    workspace_root=self.ws, lease_id="LEASE-1", conversation_id="CONV-1",
                )
        self.assertEqual("workspace_binding_invalid", caught.exception.reason_code)
        verify.assert_not_called()
        self.assertFalse((other_dd / LEDGER_FILENAME).exists())
        self.assertIsNone(self.ledger_bytes())

    def test_apply_rejects_traversal_outside_the_data_zone(self) -> None:
        escaped = self.ws / "other" / "mail-desk"
        escaped.mkdir(parents=True)
        traversal = self.ws / "data" / "mail-desk" / ".." / ".." / "other" / "mail-desk"
        self.write_inventory(_inventory_record("a@example.test"))
        plan = sent_ledger.build_legacy_import_plan(self.dd, self.inventory)
        with patch.object(sent_ledger, "verify_workspace_lock") as verify:
            with self.assertRaises(SentLedgerError) as caught:
                sent_ledger.apply_legacy_import(
                    traversal, self.inventory, approval=plan["plan_id"],
                    workspace_root=self.ws, lease_id="LEASE-1", conversation_id="CONV-1",
                )
        self.assertEqual("workspace_binding_invalid", caught.exception.reason_code)
        verify.assert_not_called()
        self.assertFalse((escaped / LEDGER_FILENAME).exists())

    def test_apply_rejects_a_symlinked_or_junction_data_zone(self) -> None:
        linked_ws = self.ws / "linked-workspace"
        (linked_ws / "data").mkdir(parents=True)
        external = self.ws / "external-data-mail-desk"
        external.mkdir()
        link = linked_ws / "data" / "mail-desk"
        if not _make_dir_link(link, external):
            self.skipTest("directory symlinks/junctions are unavailable")
        self.write_inventory(_inventory_record("a@example.test"))
        plan = sent_ledger.build_legacy_import_plan(self.dd, self.inventory)
        with patch.object(sent_ledger, "verify_workspace_lock") as verify:
            with self.assertRaises(SentLedgerError) as caught:
                sent_ledger.apply_legacy_import(
                    link, self.inventory, approval=plan["plan_id"],
                    workspace_root=linked_ws, lease_id="LEASE-1", conversation_id="CONV-1",
                )
        self.assertEqual("workspace_binding_invalid", caught.exception.reason_code)
        self.assertEqual("linked_data_zone", caught.exception.details["reason"])
        verify.assert_not_called()
        self.assertFalse((external / LEDGER_FILENAME).exists())

    def test_apply_appends_legacy_provenance_and_writes_no_report_file(self) -> None:
        self.write_inventory(
            _inventory_record("s@example.test", thread_key="root@example.test"),
            _inventory_record(
                "n@example.test", status="no_evidence", vorgang_ref=None, evid_id=None,
                reason_code="legacy_status_no_evidence",
            ),
        )
        plan = sent_ledger.build_legacy_import_plan(self.dd, self.inventory)
        result = self.apply(plan["plan_id"])

        self.assertEqual(("applied", 2), (result["status"], result["applied"]))
        records = {record["message_id"]: record for record in self.ledger_records()}
        synthesized = records["s@example.test"]
        self.assertEqual(("legacy_import", "synthesized", "V-1", "E-1"), (
            synthesized["source"], synthesized["outcome"],
            synthesized["vorgang_ref"], synthesized["evid_id"],
        ))
        no_evidence = records["n@example.test"]
        self.assertEqual(("legacy_import", "no_evidence", "legacy_status_no_evidence"), (
            no_evidence["source"], no_evidence["outcome"], no_evidence["reason_code"],
        ))
        self.assertIsNone(no_evidence["vorgang_ref"])
        self.assertNotIn("evid_id", no_evidence)
        self.assertEqual({LEDGER_FILENAME}, {path.name for path in self.dd.iterdir()})

    def test_apply_consumes_pending_handoff_but_leaves_its_bytes_untouched(self) -> None:
        sent_handoff.write_handoff(self.dd, [_handoff_item("m1@example.test")])
        handoff_before = (self.dd / sent_handoff.HANDOFF_FILENAME).read_bytes()
        self.write_inventory(_inventory_record("m1@example.test"))
        plan = sent_ledger.build_legacy_import_plan(self.dd, self.inventory)
        self.assertEqual(1, plan["summary"]["pending_handoff"])

        self.apply(plan["plan_id"])

        self.assertEqual(handoff_before, (self.dd / sent_handoff.HANDOFF_FILENAME).read_bytes())
        self.assertEqual(0, sent_handoff.load_pending_handoff(self.dd)["pending_count"])

    def test_conflicts_block_apply_and_leave_the_ledger_untouched(self) -> None:
        self.write_ledger(_ledger_record("a@example.test", vorgang_ref="V-9", evid_id="E-9"))
        self.write_inventory(_inventory_record("a@example.test", vorgang_ref="V-1", evid_id="E-1"))
        plan = sent_ledger.build_legacy_import_plan(self.dd, self.inventory)
        before = self.ledger_bytes()
        with self.assertRaises(SentLedgerError) as caught:
            self.apply(plan["plan_id"])
        self.assertEqual("legacy_import_conflict", caught.exception.reason_code)
        self.assertEqual(before, self.ledger_bytes())

    def test_partial_import_repeats_idempotently_with_a_fresh_token(self) -> None:
        self.write_inventory(
            _inventory_record("a@example.test", evid_id="E-A"),
            _inventory_record("b@example.test", evid_id="E-B"),
        )
        first = sent_ledger.build_legacy_import_plan(self.dd, self.inventory)
        # Simulate a crash after the first line: only A is durable.
        sent_handoff.append_ledger_records(self.dd, [{
            "schema_version": 1, "message_id": "a@example.test", "vorgang_ref": "V-1",
            "role": "primary", "anchored_to": None,
            "recorded_at": "2026-10-01T10:00:00Z", "outcome": "synthesized",
            "source": "legacy_import", "evid_id": "E-A",
        }])
        before = self.ledger_bytes()

        second = sent_ledger.build_legacy_import_plan(self.dd, self.inventory)
        self.assertNotEqual(first["plan_id"], second["plan_id"])
        actions = {entry["message_id"]: entry["action"] for entry in second["entries"]}
        self.assertEqual({"already_present", "import"}, set(actions.values()))

        with self.assertRaises(SentLedgerError) as stale:
            self.apply(first["plan_id"])
        self.assertEqual("legacy_import_approval_stale", stale.exception.reason_code)
        self.assertEqual(before, self.ledger_bytes())

        result = self.apply(second["plan_id"])
        self.assertEqual(("applied", 1), (result["status"], result["applied"]))
        self.assertEqual(
            {"a@example.test", "b@example.test"},
            {record["message_id"] for record in self.ledger_records()},
        )
        self.assertEqual(2, len(self.ledger_records()))

    def test_repeat_after_complete_import_is_a_safe_noop(self) -> None:
        self.write_inventory(_inventory_record("a@example.test"))
        plan = sent_ledger.build_legacy_import_plan(self.dd, self.inventory)
        self.apply(plan["plan_id"])
        before = self.ledger_bytes()

        repeat = sent_ledger.build_legacy_import_plan(self.dd, self.inventory)
        self.assertEqual(0, repeat["summary"]["import"])
        result = self.apply(plan["plan_id"])  # old token is irrelevant without imports
        self.assertEqual(("already_complete", 0), (result["status"], result["applied"]))
        self.assertEqual(before, self.ledger_bytes())

    def test_torn_ledger_tail_is_repaired_and_evidence_preserved(self) -> None:
        path = self.dd / LEDGER_FILENAME
        path.write_text(
            json.dumps(_ledger_record("old@example.test", evid_id="E-OLD")) + "\n{\"schema_ver", encoding="utf-8",
        )
        self.write_inventory(_inventory_record("new@example.test"))
        plan = sent_ledger.build_legacy_import_plan(self.dd, self.inventory)
        self.apply(plan["plan_id"])
        self.assertEqual(
            {"old@example.test", "new@example.test"},
            {record["message_id"] for record in self.ledger_records()},
        )
        self.assertEqual('{"schema_ver\n', (self.dd / (LEDGER_FILENAME + ".torn")).read_text("utf-8"))

    def test_apply_write_failure_is_reported_as_unknown_then_rerun_is_idempotent(self) -> None:
        self.write_inventory(_inventory_record("a@example.test"))
        plan = sent_ledger.build_legacy_import_plan(self.dd, self.inventory)
        with patch.object(sent_ledger, "append_ledger_records", side_effect=OSError("disk gone")):
            with self.assertRaises(SentLedgerError) as caught:
                self.apply(plan["plan_id"])
        self.assertEqual(("legacy_import_write_failed", "unknown"), (
            caught.exception.reason_code, caught.exception.details["state"],
        ))
        self.assertIsNone(self.ledger_bytes())

        # A fresh dry run + token is required; the rerun appends exactly once.
        retry_plan = sent_ledger.build_legacy_import_plan(self.dd, self.inventory)
        self.assertEqual(plan["plan_id"], retry_plan["plan_id"])
        result = self.apply(retry_plan["plan_id"])
        self.assertEqual(1, result["applied"])
        self.assertEqual(1, len(self.ledger_records()))


if __name__ == "__main__":
    unittest.main()
