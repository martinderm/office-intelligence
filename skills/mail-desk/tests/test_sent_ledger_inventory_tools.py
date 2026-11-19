"""Running tests for the declared read-only inventory tools (Issue #88/F21).

The declared tools are ``mail_desk_sent_ledger.py inventory-legacy`` and
``inventory-promotion``.  This module drives the installed CLI end to end (in
process, canonical envelope) and asserts the produced reports, not just the
declared surface:

* valid, duplicate and shared inputs are classified/counted,
* schema violations stop fail-closed with exit ``2``,
* no raw Message-ID/Vorgang/EVID/reason/thread value reaches the report,
* the input file hash is reported and every workspace file stays byte-equal,
* a foreign ``--data-dir`` is refused, and
* both tools never ask for a workspace lease.

Synthetic data only: ``example.org`` identifiers and temporary directories.
No mailbox, network or real message is touched.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))
sys.path.insert(0, str(TESTS_ROOT))

from core import sent_ledger  # noqa: E402
from sent_ledger_adapter_fixtures import make_workspace, snapshot  # noqa: E402
from sent_ledger_consumer_harness import run_cli  # noqa: E402

RECORDED_AT = "2026-11-01T09:00:00Z"
RAW_PROMO_MID = "raw-promo-alpha@example.org"
RAW_PROMO_VORGANG = "V-RAW-PROMO-ONE"
RAW_PROMO_EVID = "EVID-RAW-PROMO-ONE"
RAW_PROMO_THREAD = "thread-raw-promo@example.org"
RAW_LEGACY_MID = "raw-legacy-alpha@example.org"
RAW_LEGACY_VORGANG = "V-RAW-LEGACY-ONE"
RAW_LEGACY_EVID = "EVID-RAW-LEGACY-ONE"
RAW_LEGACY_THREAD = "thread-raw-legacy@example.org"


def legacy_record(
    mid: str,
    *,
    status: str = "synthesized",
    vorgang_ref: str | None = "V-1",
    evid_id: str | None = "EVID-1",
    reason_code: str | None = None,
    thread_key: str | None = None,
    recorded_at: str = RECORDED_AT,
) -> dict:
    """One schema-shaped legacy inventory record (optional fields only if set)."""
    record: dict = {
        "schema_version": 1,
        "message_id": mid,
        "status": status,
        "recorded_at": recorded_at,
    }
    if vorgang_ref is not None:
        record["vorgang_ref"] = vorgang_ref
    if evid_id is not None:
        record["evid_id"] = evid_id
    if reason_code is not None:
        record["reason_code"] = reason_code
    if thread_key is not None:
        record["thread_key"] = thread_key
    return record


def promotion_record(
    mid: str,
    *,
    vorgang_ref: str = "V-1",
    evid_id: str = "EVID-1",
    in_reply_to: str | None = None,
    references: list[str] | None = None,
    thread_key: str | None = None,
) -> dict:
    """One schema-shaped durable promotion record (raw values stay in memory)."""
    record: dict = {
        "message_id": mid,
        "vorgang_ref": vorgang_ref,
        "evid_id": evid_id,
    }
    if in_reply_to is not None:
        record["in_reply_to"] = in_reply_to
    if references is not None:
        record["references"] = list(references)
    if thread_key is not None:
        record["thread_key"] = thread_key
    return record


class InventoryToolsTestCase(unittest.TestCase):
    """Isolated workspace per test; every input file lives inside the temp root."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def workspace(self, name: str = "synth-a") -> tuple[Path, Path]:
        return make_workspace(self.root / name)

    def write_inventory(self, path: Path, records: list[dict]) -> Path:
        path.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )
        return path

    def write_evidence(self, path: Path, records: list[dict]) -> Path:
        path.write_text(json.dumps(records, ensure_ascii=False) + "\n", encoding="utf-8")
        return path

    def inventory_promotion(self, workspace: Path, evidence: Path) -> "CliResult":
        return run_cli(
            "inventory-promotion", "--workspace", str(workspace),
            "--data-dir", "data/mail-desk", "--evidence-file", str(evidence), "--json",
        )

    def inventory_legacy(self, workspace: Path, inventory: Path) -> "CliResult":
        return run_cli(
            "inventory-legacy", "--workspace", str(workspace),
            "--data-dir", "data/mail-desk", "--inventory", str(inventory), "--json",
        )

    def assert_no_lease_was_taken(self, workspace: Path, call) -> "CliResult":
        """Run ``call`` with a verifier that fails if any lease is requested."""
        with mock.patch.object(
            sent_ledger, "verify_workspace_lock",
            side_effect=AssertionError("a read-only inventory tool requested a lease"),
        ):
            result = call()
        self.assertFalse(
            (workspace / ".agents" / "session.lock").exists(),
            msg="a read-only inventory tool created a lease artifact",
        )
        return result

    def assert_ref(self, value: str) -> None:
        self.assertRegex(value, r"^sha256:[0-9a-f]{16}$")

    def assert_hash_of(self, value: str, path: Path) -> None:
        self.assertEqual(value, hashlib.sha256(path.read_bytes()).hexdigest())


class PromotionInventoryTest(InventoryToolsTestCase):
    """``inventory-promotion``: valid, duplicate, shared and fail-closed paths."""

    def test_unique_evidence_is_conflict_free_and_points_to_the_recovery_tools(self) -> None:
        workspace, data_dir = self.workspace()
        evidence = self.write_evidence(data_dir / "durable-promotions.json", [
            promotion_record(
                RAW_PROMO_MID,
                vorgang_ref=RAW_PROMO_VORGANG,
                evid_id=RAW_PROMO_EVID,
                in_reply_to="root@example.org",
                thread_key=RAW_PROMO_THREAD,
            ),
        ])
        before = snapshot(workspace)
        result = self.assert_no_lease_was_taken(
            workspace, lambda: self.inventory_promotion(workspace, evidence),
        )
        self.assertEqual(result.exit_code, 0, msg=json.dumps(result.envelope))
        data = result.data
        self.assertEqual(data["kind"], "mail-desk.sent-ledger.promotion-evidence-inventory")
        self.assertEqual(data["view_version"], 1)
        self.assertTrue(data["read_only"])
        self.assertEqual(data["entry_count"], 1)
        self.assertTrue(data["conflict_free"])
        self.assertEqual(data["classification_counts"],
                         {"unique": 1, "duplicate_message_id": 0, "shared_evid": 0})
        self.assertEqual(data["issue_counts"], {"duplicate_message_id": 0, "shared_evid": 0})
        self.assertEqual(data["conflicts"],
                         {"duplicate_message_ids": 0, "shared_evid_ids": 0})
        self.assertEqual(data["thread_metadata"]["with_metadata"], 1)
        self.assertEqual(data["thread_metadata"]["follow_ups"], 1)
        self.assertIn("crash-check", data["next"])
        self.assertIn("recover-promotion", data["next"])
        self.assert_hash_of(data["evidence_sha256"], evidence)
        self.assertEqual(snapshot(workspace), before, msg="a read-only inventory wrote a file")
        self.assert_ref(data["records"][0]["item_ref"])
        self.assert_ref(data["records"][0]["vorgang_ref"])
        self.assert_ref(data["records"][0]["evid_ref"])
        self.assert_ref(data["records"][0]["thread_ref"])

    def test_duplicate_and_shared_evidence_is_counted_with_documented_precedence(self) -> None:
        workspace, data_dir = self.workspace()
        evidence = self.write_evidence(data_dir / "durable-promotions.json", [
            # unique
            promotion_record("unique@example.org", evid_id="EVID-UNIQUE"),
            # duplicate Message-ID, distinct EVID owner
            promotion_record("dup@example.org", vorgang_ref="V-DUP", evid_id="EVID-DUP"),
            promotion_record("dup@example.org", vorgang_ref="V-DUP", evid_id="EVID-DUP"),
            # shared EVID across two different Message-IDs
            promotion_record("shared-a@example.org", evid_id="EVID-SHARED"),
            promotion_record("shared-b@example.org", evid_id="EVID-SHARED"),
            # duplicate Message-ID *and* shared EVID: precedence is duplicate first
            promotion_record("both@example.org", evid_id="EVID-BOTH"),
            promotion_record("both@example.org", evid_id="EVID-BOTH"),
            promotion_record("other@example.org", evid_id="EVID-BOTH"),
        ])
        result = self.inventory_promotion(workspace, evidence)
        self.assertEqual(result.exit_code, 0, msg=json.dumps(result.envelope))
        data = result.data
        self.assertEqual(data["entry_count"], 8)
        self.assertFalse(data["conflict_free"])
        self.assertEqual(
            data["classification_counts"],
            {"unique": 1, "duplicate_message_id": 4, "shared_evid": 3},
        )
        self.assertEqual(
            data["issue_counts"], {"duplicate_message_id": 4, "shared_evid": 5},
        )
        self.assertEqual(
            data["conflicts"], {"duplicate_message_ids": 2, "shared_evid_ids": 2},
        )
        both = [
            entry for entry in data["records"]
            if entry["classification"] == "duplicate_message_id"
            and entry["issues"] == ["duplicate_message_id", "shared_evid"]
        ]
        self.assertEqual(len(both), 2, msg="classification precedence is not duplicate-first")
        # Conflicting evidence must not offer a promote/recover shortcut.
        self.assertIn("Resolve", data["next"])
        self.assertIn("duplicate Message-IDs", data["next"])
        self.assertIn("shared EVID-IDs", data["next"])

    def test_missing_required_field_is_promotion_inventory_invalid_exit_two(self) -> None:
        for name, record, reason in (
            (
                "missing-evid",
                {"message_id": "alpha@example.org", "vorgang_ref": "V-1"},
                "missing_field:evid_id",
            ),
            (
                "missing-vorgang",
                {"message_id": "alpha@example.org", "evid_id": "EVID-1"},
                "missing_field:vorgang_ref",
            ),
            (
                "bad-references",
                {
                    "message_id": "alpha@example.org", "vorgang_ref": "V-1",
                    "evid_id": "EVID-1", "references": "root@example.org",
                },
                "references_invalid",
            ),
            (
                "bad-in-reply-to",
                {
                    "message_id": "alpha@example.org", "vorgang_ref": "V-1",
                    "evid_id": "EVID-1", "in_reply_to": 7,
                },
                "in_reply_to_invalid",
            ),
        ):
            with self.subTest(case=name):
                workspace, data_dir = self.workspace(f"synth-{name}")
                evidence = self.write_evidence(
                    data_dir / "durable-promotions.json", [record],
                )
                before = snapshot(workspace)
                result = self.inventory_promotion(workspace, evidence)
                self.assertEqual(result.exit_code, 2, msg=json.dumps(result.envelope))
                self.assertFalse(result.success)
                self.assertEqual(result.reason_code, "promotion_inventory_invalid")
                self.assertEqual(result.data.get("state"), "invalid")
                self.assertEqual(result.data.get("invalid_entry"), 1)
                self.assertEqual(result.data.get("reason"), reason)
                self.assertEqual(snapshot(workspace), before)

    def test_unreadable_or_non_list_evidence_is_promotion_recovery_incomplete(self) -> None:
        for name, payload in (("not-a-list", {"records": []}), ("not-json", None)):
            with self.subTest(case=name):
                workspace, data_dir = self.workspace(f"synth-{name}")
                evidence = data_dir / "durable-promotions.json"
                if payload is None:
                    evidence.write_text("{not json", encoding="utf-8")
                else:
                    evidence.write_text(json.dumps(payload), encoding="utf-8")
                result = self.inventory_promotion(workspace, evidence)
                self.assertEqual(result.exit_code, 2, msg=json.dumps(result.envelope))
                self.assertEqual(result.reason_code, "promotion_recovery_incomplete")

    def test_report_contains_no_raw_identifier_or_thread_value(self) -> None:
        workspace, data_dir = self.workspace()
        evidence = self.write_evidence(data_dir / "durable-promotions.json", [
            promotion_record(
                RAW_PROMO_MID,
                vorgang_ref=RAW_PROMO_VORGANG,
                evid_id=RAW_PROMO_EVID,
                in_reply_to="ancestor-raw@example.org",
                references=["ancestor-raw@example.org"],
                thread_key=RAW_PROMO_THREAD,
            ),
            promotion_record("raw-promo-duplicate@example.org",
                            evid_id="EVID-RAW-PROMO-DUPLICATE"),
            promotion_record("raw-promo-duplicate@example.org",
                            evid_id="EVID-RAW-PROMO-DUPLICATE"),
            promotion_record("raw-promo-shared-a@example.org", evid_id="EVID-RAW-SHARED"),
            promotion_record("raw-promo-shared-b@example.org", evid_id="EVID-RAW-SHARED"),
        ])
        result = self.inventory_promotion(workspace, evidence)
        self.assertEqual(result.exit_code, 0, msg=json.dumps(result.envelope))
        text = json.dumps(result.envelope, ensure_ascii=False)
        for raw in (
            RAW_PROMO_MID, RAW_PROMO_VORGANG, RAW_PROMO_EVID, RAW_PROMO_THREAD,
            "ancestor-raw@example.org", "raw-promo-duplicate@example.org",
            "EVID-RAW-PROMO-DUPLICATE", "raw-promo-shared-a@example.org",
            "raw-promo-shared-b@example.org", "EVID-RAW-SHARED",
        ):
            self.assertNotIn(raw, text, msg=f"raw value {raw!r} leaked into the report")
        for entry in result.data["records"]:
            self.assert_ref(entry["item_ref"])
            self.assert_ref(entry["vorgang_ref"])
            self.assert_ref(entry["evid_ref"])
        self.assertNotIn("references", json.dumps(result.data["records"]))

    def test_foreign_data_dir_is_rejected_before_reading_or_writing(self) -> None:
        workspace_a, data_a = self.workspace("synth-a")
        workspace_b, data_b = self.workspace("synth-b")
        evidence = self.write_evidence(data_a / "durable-promotions.json", [
            promotion_record("alpha@example.org"),
        ])
        before_a, before_b = snapshot(workspace_a), snapshot(workspace_b)
        result = run_cli(
            "inventory-promotion", "--workspace", str(workspace_a),
            "--data-dir", str(data_b), "--evidence-file", str(evidence), "--json",
        )
        self.assertEqual(result.exit_code, 2, msg=json.dumps(result.envelope))
        self.assertEqual(result.reason_code, "workspace_binding_invalid")
        self.assertEqual(snapshot(workspace_a), before_a)
        self.assertEqual(snapshot(workspace_b), before_b)


class LegacyInventoryTest(InventoryToolsTestCase):
    """``inventory-legacy``: valid, conflicting, invalid and read-only paths."""

    def test_valid_inventory_is_summarised_read_only_without_raw_identifiers(self) -> None:
        workspace, data_dir = self.workspace()
        inventory = self.write_inventory(data_dir / "legacy-inventory.jsonl", [
            legacy_record(RAW_LEGACY_MID, vorgang_ref=RAW_LEGACY_VORGANG,
                          evid_id=RAW_LEGACY_EVID, thread_key=RAW_LEGACY_THREAD),
            legacy_record("legacy-no-evidence@example.org", status="no_evidence",
                          vorgang_ref=None, evid_id=None,
                          reason_code="no_relevant_evidence"),
        ])
        before = snapshot(workspace)
        result = self.assert_no_lease_was_taken(
            workspace, lambda: self.inventory_legacy(workspace, inventory),
        )
        self.assertEqual(result.exit_code, 0, msg=json.dumps(result.envelope))
        data = result.data
        self.assertEqual(data["kind"], "mail-desk.sent-ledger.legacy-inventory-report")
        self.assertEqual(data["view_version"], 1)
        self.assertTrue(data["read_only"])
        self.assertEqual(data["record_count"], 2)
        self.assertEqual(data["status_counts"], {"synthesized": 1, "no_evidence": 1})
        self.assertEqual(data["conflicts"]["problem_records"], 0)
        self.assertEqual(data["conflicts"]["duplicate_message_ids"], 0)
        self.assertEqual(data["conflicts"]["shared_evid_ids"], 0)
        self.assert_hash_of(data["inventory_sha256"], inventory)
        self.assertEqual(data["schema"]["kind"], "mail-desk.sent-ledger.legacy-inventory")
        self.assertEqual(snapshot(workspace), before, msg="a read-only inventory wrote a file")
        records = data["records"]
        self.assertEqual(len(records), 2)
        for entry in records:
            self.assert_ref(entry["item_ref"])
        self.assertTrue(any("thread_ref" in entry for entry in records))
        text = json.dumps(result.envelope, ensure_ascii=False)
        for raw in (
            RAW_LEGACY_MID, RAW_LEGACY_VORGANG, RAW_LEGACY_EVID, RAW_LEGACY_THREAD,
            "legacy-no-evidence@example.org", "no_relevant_evidence",
        ):
            self.assertNotIn(raw, text, msg=f"raw value {raw!r} leaked into the report")

    def test_conflicts_duplicates_and_shared_evid_are_counted(self) -> None:
        workspace, data_dir = self.workspace()
        inventory = self.write_inventory(data_dir / "legacy-inventory.jsonl", [
            # synthesized triad incomplete -> reported problem, not a schema error
            legacy_record("missing-evid@example.org", evid_id=None),
            # no_evidence with an EVID anchor -> reported problem
            legacy_record("no-evidence-with-anchor@example.org", status="no_evidence",
                          vorgang_ref=None, evid_id="EVID-FORBIDDEN",
                          reason_code="no_relevant_evidence"),
            # shared EVID across two Message-IDs
            legacy_record("shared-a@example.org", evid_id="EVID-SHARED-LEGACY"),
            legacy_record("shared-b@example.org", evid_id="EVID-SHARED-LEGACY"),
            # identical duplicate inventory line
            legacy_record("duplicate@example.org", evid_id="EVID-DUPLICATE"),
            legacy_record("duplicate@example.org", evid_id="EVID-DUPLICATE"),
        ])
        before = snapshot(workspace)
        result = self.inventory_legacy(workspace, inventory)
        self.assertEqual(result.exit_code, 0, msg=json.dumps(result.envelope))
        data = result.data
        self.assertEqual(data["record_count"], 6)
        conflicts = data["conflicts"]
        self.assertEqual(conflicts["problem_records"], 2)
        self.assertEqual(
            conflicts["problem_counts"],
            {"missing_evid_id": 1, "no_evidence_with_evid_id": 1},
        )
        self.assertEqual(conflicts["duplicate_message_ids"], 1)
        self.assertEqual(conflicts["shared_evid_ids"], 1)
        problems = sorted(
            entry["problem"] for entry in data["records"] if "problem" in entry
        )
        self.assertEqual(problems, ["missing_evid_id", "no_evidence_with_evid_id"])
        self.assertEqual(snapshot(workspace), before)

    def test_missing_required_field_is_legacy_inventory_invalid_exit_two(self) -> None:
        for name, record, reason in (
            (
                "missing-recorded-at",
                {"schema_version": 1, "message_id": "alpha@example.org",
                 "status": "synthesized", "vorgang_ref": "V-1", "evid_id": "EVID-1"},
                "missing_field:recorded_at",
            ),
            (
                "unknown-field",
                {**legacy_record("alpha@example.org"), "note": "synthetic"},
                "unknown_field:note",
            ),
            (
                "bad-status",
                {**legacy_record("alpha@example.org"), "status": "promoted"},
                "invalid_status",
            ),
            (
                "bad-schema-version",
                {**legacy_record("alpha@example.org"), "schema_version": 2},
                "unsupported_schema_version",
            ),
        ):
            with self.subTest(case=name):
                workspace, data_dir = self.workspace(f"synth-{name}")
                inventory = self.write_inventory(
                    data_dir / "legacy-inventory.jsonl", [record],
                )
                before = snapshot(workspace)
                result = self.inventory_legacy(workspace, inventory)
                self.assertEqual(result.exit_code, 2, msg=json.dumps(result.envelope))
                self.assertFalse(result.success)
                self.assertEqual(result.reason_code, "legacy_inventory_invalid")
                self.assertEqual(result.data.get("invalid_line"), 1)
                self.assertEqual(result.data.get("reason"), reason)
                self.assertEqual(snapshot(workspace), before)

    def test_empty_and_unreadable_inventory_stop_with_exit_two(self) -> None:
        with self.subTest(case="empty"):
            workspace, data_dir = self.workspace("synth-empty")
            inventory = data_dir / "legacy-inventory.jsonl"
            inventory.write_text("\n\n", encoding="utf-8")
            result = self.inventory_legacy(workspace, inventory)
            self.assertEqual(result.exit_code, 2, msg=json.dumps(result.envelope))
            self.assertEqual(result.reason_code, "legacy_inventory_invalid")
            self.assertEqual(result.data.get("reason"), "empty")

        with self.subTest(case="unreadable"):
            workspace, data_dir = self.workspace("synth-unreadable")
            result = self.inventory_legacy(workspace, data_dir)
            self.assertEqual(result.exit_code, 2, msg=json.dumps(result.envelope))
            self.assertEqual(result.reason_code, "legacy_inventory_invalid")
            self.assertEqual(result.data.get("reason"), "unreadable")

    def test_foreign_data_dir_is_rejected_before_reading_or_writing(self) -> None:
        workspace_a, data_a = self.workspace("synth-a")
        workspace_b, data_b = self.workspace("synth-b")
        inventory = self.write_inventory(data_a / "legacy-inventory.jsonl", [
            legacy_record("alpha@example.org"),
        ])
        before_a, before_b = snapshot(workspace_a), snapshot(workspace_b)
        result = run_cli(
            "inventory-legacy", "--workspace", str(workspace_a),
            "--data-dir", str(data_b), "--inventory", str(inventory), "--json",
        )
        self.assertEqual(result.exit_code, 2, msg=json.dumps(result.envelope))
        self.assertEqual(result.reason_code, "workspace_binding_invalid")
        self.assertEqual(snapshot(workspace_a), before_a)
        self.assertEqual(snapshot(workspace_b), before_b)


if __name__ == "__main__":  # pragma: no cover - manual single-module run
    unittest.main()
