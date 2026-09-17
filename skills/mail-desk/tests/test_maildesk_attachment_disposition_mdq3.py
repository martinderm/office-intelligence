"""Hermetic unit and adversarial tests for MD-Q3 disposition log, reporting, verifiable receipts, and recovery journal."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

_test_dir = Path(__file__).resolve().parent
_skill_dir = _test_dir.parent
_scripts_dir = _skill_dir / "scripts"

if str(_scripts_dir) not in sys.path:
    sys.path.insert(0, str(_scripts_dir))

from core.common import normalize_message_id

from core import (
    DISPOSITION_LOG_FILENAME,
    DISCARD_JOURNAL_FILENAME,
    DECISION_RETAIN,
    DECISION_DISCARD,
    DECISION_PROMOTE,
    STATUS_ELIGIBLE,
    STATUS_PROTECTED,
    STATUS_INVALID,
    JOURNAL_STATE_PREPARED,
    JOURNAL_STATE_FILE_DELETED,
    JOURNAL_STATE_INVENTORY_UPDATED,
    JOURNAL_STATE_INDEX_UPDATED,
    JOURNAL_STATE_COMPLETED,
    JOURNAL_STATE_FAILED,
    AttachmentIndexSchemaError,
    DispositionError,
    DispositionSchemaError,
    DispositionDriftError,
    DispositionLockRequiredError,
    DispositionApplyError,
    ReceiptError,
    ReceiptMissingError,
    ReceiptMalformedError,
    ReceiptDriftError,
    RecoveryEvidenceMissingError,
    RecoveryJournalCorruptedError,
    InventoryUpdateError,
    PhysicalVerificationError,
    ForbiddenContentError,
    compute_decision_id,
    resolve_disposition_log_path,
    resolve_discard_journal_path,
    validate_disposition_entry,
    load_disposition_log,
    load_discard_journal,
    record_disposition_entry,
    report_dispositions,
    apply_discard,
    canonical_receipt_sha256,
    build_disposition_request,
    canonical_disposition_request_sha256,
    build_disposition_receipt,
    verify_approval_receipt,
    build_apply_request,
    canonical_apply_request_sha256,
    build_apply_receipt,
    verify_apply_receipt,
    record_journal_state,
    record_journal_failure,
    update_quarantine_inventory_atomic,
    load_quarantine_index,
    record_quarantine_entry,
    canonical_index_entry_sha256,
    utc_now_iso,
    INDEX_FILENAME,
)


class TestMailDeskAttachmentDispositionMDQ3(unittest.TestCase):
    """Hermetic test suite for MD-Q3 verifiable receipts, audit log, and journal recovery."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="test_mdq3_")
        self.ws_root = Path(self.temp_dir).resolve()
        self.data_dir = self.ws_root / "data" / "mail-desk"
        self.attachments_root = self.data_dir / "attachments"
        self.agents_dir = self.ws_root / ".agents"

        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.attachments_root.mkdir(parents=True, exist_ok=True)
        self.agents_dir.mkdir(parents=True, exist_ok=True)

        self.lease_id = "test_lease_mdq3_001"
        self.conv_id = "test_conv_mdq3_001"
        self.lock_file = self.agents_dir / "session.lock"

        self.index_path = self.data_dir / INDEX_FILENAME
        self.log_path = self.data_dir / DISPOSITION_LOG_FILENAME
        self.journal_path = self.data_dir / DISCARD_JOURNAL_FILENAME

        # Setup final-location-index.json to guarantee byte identity
        self.final_index_path = self.data_dir / "final-location-index.json"
        self.final_index_content = json.dumps(
            {"schema_version": 1, "items": {"<sample@example.org>": {"final_folder": "Archive"}}},
            indent=2,
        ).encode("utf-8")
        self.final_index_path.write_bytes(self.final_index_content)

        # Set up sample quarantine attachment entries
        self._write_lock_file(self.lease_id, self.conv_id)
        self.sample_entry = self._create_sample_quarantine_run(
            "run_mdq3_01",
            "<doc01@example.org>",
            "invoice_01.pdf",
            b"%PDF-1.4 sample invoice payload for MD-Q3",
        )
        record_res = record_quarantine_entry(
            self.index_path,
            payload=self.sample_entry,
            workspace_root=self.ws_root,
            lease_id=self.lease_id,
            conversation_id=self.conv_id,
        )
        self.assertEqual(record_res["status"], "created")
        self.att_id_01 = record_res["attachment_id"]

        # Current canonical index entry hash
        disk_idx = load_quarantine_index(self.index_path)
        self.sample_idx_entry = disk_idx["items"][self.att_id_01]
        self.sample_idx_hash = canonical_index_entry_sha256(self.sample_idx_entry)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _write_lock_file(self, lease_id: str, conv_id: str, harness: str = "antigravity") -> None:
        now_iso = utc_now_iso()
        lock_data = {
            "harness": harness,
            "pid": os.getpid(),
            "user": "test_user",
            "started": now_iso,
            "heartbeat": now_iso,
            "leaseId": lease_id,
            "conversationId": conv_id,
        }
        self.lock_file.write_text(json.dumps(lock_data, indent=2), encoding="utf-8")

    def _create_sample_quarantine_run(
        self,
        run_id: str,
        message_id: str,
        filename: str,
        payload: bytes,
        part_locator: str = "1",
        account: str = "default_acc",
        folder: str = "INBOX",
        mime_type: str = "application/pdf",
    ) -> dict[str, Any]:
        """Helper to create physical file and valid .quarantine-inventory.json in a run dir."""
        run_dir = self.attachments_root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        target_file = run_dir / filename
        target_file.write_bytes(payload)

        file_sha = hashlib.sha256(payload).hexdigest()
        file_size = len(payload)

        inv_path = run_dir / ".quarantine-inventory.json"
        if inv_path.exists():
            inv = json.loads(inv_path.read_text(encoding="utf-8"))
        else:
            inv = {
                "schema_version": 1,
                "run_id": run_id,
                "created_at": utc_now_iso(),
                "messages": {},
            }

        norm_mid = normalize_message_id(message_id) or message_id.strip()
        msg_entry = inv["messages"].setdefault(norm_mid, {
            "account": account,
            "folder": folder,
            "count": 0,
            "total_bytes": 0,
            "files": {},
        })
        msg_entry["files"][filename] = {
            "part_locator": part_locator,
            "mime_type": mime_type,
            "sha256": file_sha,
            "size_bytes": file_size,
        }
        msg_entry["count"] = len(msg_entry["files"])
        msg_entry["total_bytes"] = sum(f["size_bytes"] for f in msg_entry["files"].values())
        inv_path.write_text(json.dumps(inv, indent=2, sort_keys=True), encoding="utf-8")

        rel_path = f"data/mail-desk/attachments/{run_id}/{filename}"
        return {
            "message_id": message_id,
            "account": account,
            "folder": folder,
            "part_locator": part_locator,
            "clean_filename": filename,
            "mime_type": mime_type,
            "sha256": file_sha,
            "size_bytes": file_size,
            "run_id": run_id,
            "quarantine_path": rel_path,
            "analysis_status": "completed",
            "analyzed_at": utc_now_iso(),
            "contract_version": "1.0",
            "lifecycle_state": "quarantined",
            "disposition_ref": None,
        }

    def _make_disposition_receipt(
        self,
        request_payload: dict[str, Any],
        receipt_id: str = "rcpt_disp_001",
        approved_by: str = "human_reviewer",
    ) -> dict[str, Any]:
        req_hash = canonical_disposition_request_sha256(request_payload)
        return {
            "receipt_id": receipt_id,
            "request_hash": req_hash,
            "approved_at": utc_now_iso(),
            "approved_by": approved_by,
        }

    def _make_apply_receipt(
        self,
        apply_req: dict[str, Any],
        receipt_id: str = "rcpt_apply_001",
        approved_by: str = "human_operator",
    ) -> dict[str, Any]:
        req_hash = canonical_apply_request_sha256(apply_req)
        return {
            "receipt_id": receipt_id,
            "request_hash": req_hash,
            "approved_at": utc_now_iso(),
            "approved_by": approved_by,
        }

    # --------------------------------------------------------------------------
    # 1. Receipt Structure & Random Hash Rejection (P1 Finding 1)
    # --------------------------------------------------------------------------
    def test_random_64_hex_hash_rejected_as_authorization(self) -> None:
        """Raw 64-hex string does not constitute authorization for disposition or apply."""
        self._write_lock_file(self.lease_id, self.conv_id)
        raw_hash = "a" * 64

        # Record with raw hash and no receipt fails closed
        payload = {
            "attachment_id": self.att_id_01,
            "decision": DECISION_DISCARD,
            "human_receipt_hash": raw_hash,
        }
        with self.assertRaises(ReceiptMalformedError):
            record_disposition_entry(
                self.log_path,
                payload=payload,
                workspace_root=self.ws_root,
                lease_id=self.lease_id,
                conversation_id=self.conv_id,
            )

        # Apply with raw string receipt fails closed
        with self.assertRaises(ReceiptMalformedError):
            apply_discard(
                attachment_id=self.att_id_01,
                apply_receipt=raw_hash,  # type: ignore
                workspace_root=self.ws_root,
                lease_id=self.lease_id,
                conversation_id=self.conv_id,
            )

    def test_receipt_request_hash_drift_rejected(self) -> None:
        """Receipt with mismatched request_hash must fail closed as ReceiptDriftError."""
        self._write_lock_file(self.lease_id, self.conv_id)
        disp_req = build_disposition_request(
            attachment_id=self.att_id_01,
            index_entry_sha256=self.sample_idx_hash,
            decision=DECISION_DISCARD,
        )
        drifted_receipt = {
            "receipt_id": "rcpt_tampered_001",
            "request_hash": "f" * 64,  # tampered hash
            "approved_at": utc_now_iso(),
            "approved_by": "attacker",
        }
        payload = {
            "attachment_id": self.att_id_01,
            "decision": DECISION_DISCARD,
            "approval_receipt": drifted_receipt,
        }
        with self.assertRaises(ReceiptDriftError):
            record_disposition_entry(
                self.log_path,
                payload=payload,
                workspace_root=self.ws_root,
                lease_id=self.lease_id,
                conversation_id=self.conv_id,
            )

    # --------------------------------------------------------------------------
    # 2. Scope Binding: Receipt for A cannot delete B, No Bulk Apply (P1 Finding 2)
    # --------------------------------------------------------------------------
    def test_receipt_for_attachment_a_cannot_delete_b(self) -> None:
        """An apply receipt strictly bound to attachment A cannot be used to delete attachment B."""
        self._write_lock_file(self.lease_id, self.conv_id)
        # Create second attachment B
        entry_b = self._create_sample_quarantine_run(
            "run_mdq3_01",
            "<doc02@example.org>",
            "invoice_02.pdf",
            b"%PDF-1.4 sample invoice payload B",
        )
        record_res_b = record_quarantine_entry(
            self.index_path,
            payload=entry_b,
            workspace_root=self.ws_root,
            lease_id=self.lease_id,
            conversation_id=self.conv_id,
        )
        att_id_b = record_res_b["attachment_id"]

        disk_idx = load_quarantine_index(self.index_path)
        hash_a = canonical_index_entry_sha256(disk_idx["items"][self.att_id_01])
        hash_b = canonical_index_entry_sha256(disk_idx["items"][att_id_b])

        # Record discard for both A and B
        req_a = build_disposition_request(attachment_id=self.att_id_01, index_entry_sha256=hash_a, decision=DECISION_DISCARD)
        rcpt_a = self._make_disposition_receipt(req_a)
        record_disposition_entry(self.log_path, payload={"attachment_id": self.att_id_01, "decision": DECISION_DISCARD, "approval_receipt": rcpt_a}, workspace_root=self.ws_root, lease_id=self.lease_id, conversation_id=self.conv_id)

        req_b = build_disposition_request(attachment_id=att_id_b, index_entry_sha256=hash_b, decision=DECISION_DISCARD)
        rcpt_b = self._make_disposition_receipt(req_b)
        record_disposition_entry(self.log_path, payload={"attachment_id": att_id_b, "decision": DECISION_DISCARD, "approval_receipt": rcpt_b}, workspace_root=self.ws_root, lease_id=self.lease_id, conversation_id=self.conv_id)

        log_data = load_disposition_log(self.log_path)
        dec_a_id = log_data["latest_by_attachment_id"][self.att_id_01]["decision_id"]

        # Build apply receipt bound strictly to A
        apply_req_a = build_apply_request(
            attachment_id=self.att_id_01,
            decision_id=dec_a_id,
            index_entry_sha256=hash_a,
            quarantine_path=disk_idx["items"][self.att_id_01]["quarantine_path"],
            sha256=disk_idx["items"][self.att_id_01]["sha256"],
            size_bytes=disk_idx["items"][self.att_id_01]["size_bytes"],
            run_id="run_mdq3_01",
        )
        apply_rcpt_a = self._make_apply_receipt(apply_req_a)

        # Attempt to apply using receipt for A against attachment B: must fail closed!
        with self.assertRaises(ReceiptDriftError):
            apply_discard(
                attachment_id=att_id_b,
                apply_receipt=apply_rcpt_a,
                workspace_root=self.ws_root,
                lease_id=self.lease_id,
                conversation_id=self.conv_id,
            )

    def test_no_bulk_apply_without_specific_attachment(self) -> None:
        """Apply without attachment_id must fail closed: bulk apply is forbidden."""
        self._write_lock_file(self.lease_id, self.conv_id)
        dummy_receipt = {
            "receipt_id": "rcpt_bulk_001",
            "request_hash": "a" * 64,
            "approved_at": utc_now_iso(),
            "approved_by": "operator",
        }
        with self.assertRaises(DispositionApplyError) as ctx:
            apply_discard(
                attachment_id="",  # empty
                apply_receipt=dummy_receipt,
                workspace_root=self.ws_root,
                lease_id=self.lease_id,
                conversation_id=self.conv_id,
            )
        self.assertIn("mandatory", str(ctx.exception).lower())

    # --------------------------------------------------------------------------
    # 3. Persisted Apply-/Recovery-Journal & Missing File Proof (P1 Finding 3 & 4)
    # --------------------------------------------------------------------------
    def test_missing_file_without_recovery_journal_rejected(self) -> None:
        """A missing target file without matching file_deleted recovery journal fails closed."""
        self._write_lock_file(self.lease_id, self.conv_id)
        # Record discard
        disp_req = build_disposition_request(attachment_id=self.att_id_01, index_entry_sha256=self.sample_idx_hash, decision=DECISION_DISCARD)
        disp_rcpt = self._make_disposition_receipt(disp_req)
        record_disposition_entry(self.log_path, payload={"attachment_id": self.att_id_01, "decision": DECISION_DISCARD, "approval_receipt": disp_rcpt}, workspace_root=self.ws_root, lease_id=self.lease_id, conversation_id=self.conv_id)

        # Delete physical file out-of-band without journal
        target_file = self.ws_root / PurePosixPath(self.sample_entry["quarantine_path"])
        target_file.unlink()
        self.assertFalse(target_file.exists())

        log_data = load_disposition_log(self.log_path)
        dec_id = log_data["latest_by_attachment_id"][self.att_id_01]["decision_id"]

        apply_req = build_apply_request(
            attachment_id=self.att_id_01,
            decision_id=dec_id,
            index_entry_sha256=self.sample_idx_hash,
            quarantine_path=self.sample_entry["quarantine_path"],
            sha256=self.sample_entry["sha256"],
            size_bytes=self.sample_entry["size_bytes"],
            run_id=self.sample_entry["run_id"],
        )
        apply_rcpt = self._make_apply_receipt(apply_req)

        # Must fail closed with RecoveryEvidenceMissingError; index remains untouched
        with self.assertRaises(RecoveryEvidenceMissingError):
            apply_discard(
                attachment_id=self.att_id_01,
                apply_receipt=apply_rcpt,
                workspace_root=self.ws_root,
                lease_id=self.lease_id,
                conversation_id=self.conv_id,
            )

        disk_idx = load_quarantine_index(self.index_path)
        self.assertIn(self.att_id_01, disk_idx["items"])

    def test_inventory_write_failure_leaves_index_untouched(self) -> None:
        """If inventory write fails after unlink, journal records failure and index is left untouched."""
        self._write_lock_file(self.lease_id, self.conv_id)
        disp_req = build_disposition_request(attachment_id=self.att_id_01, index_entry_sha256=self.sample_idx_hash, decision=DECISION_DISCARD)
        disp_rcpt = self._make_disposition_receipt(disp_req)
        record_disposition_entry(self.log_path, payload={"attachment_id": self.att_id_01, "decision": DECISION_DISCARD, "approval_receipt": disp_rcpt}, workspace_root=self.ws_root, lease_id=self.lease_id, conversation_id=self.conv_id)

        log_data = load_disposition_log(self.log_path)
        dec_id = log_data["latest_by_attachment_id"][self.att_id_01]["decision_id"]

        apply_req = build_apply_request(
            attachment_id=self.att_id_01,
            decision_id=dec_id,
            index_entry_sha256=self.sample_idx_hash,
            quarantine_path=self.sample_entry["quarantine_path"],
            sha256=self.sample_entry["sha256"],
            size_bytes=self.sample_entry["size_bytes"],
            run_id=self.sample_entry["run_id"],
        )
        apply_rcpt = self._make_apply_receipt(apply_req)

        # Simulate atomic inventory update failure after file deletion
        with patch(
            "core.attachment_disposition_log.update_quarantine_inventory_atomic",
            side_effect=InventoryUpdateError("simulated atomic inventory failure"),
        ):
            with self.assertRaises(DispositionApplyError):
                apply_discard(
                    attachment_id=self.att_id_01,
                    apply_receipt=apply_rcpt,
                    workspace_root=self.ws_root,
                    lease_id=self.lease_id,
                    conversation_id=self.conv_id,
                )

        # Quarantine index must NOT have been modified
        disk_idx = load_quarantine_index(self.index_path)
        self.assertIn(self.att_id_01, disk_idx["items"])

        # Journal must record failure
        journal = load_discard_journal(self.journal_path)
        entries = list(journal["entries"].values())
        self.assertTrue(len(entries) > 0)
        self.assertEqual(entries[0]["status"], "failed")
        self.assertEqual(entries[0]["last_successful_state"], JOURNAL_STATE_FILE_DELETED)
        self.assertEqual(entries[0]["failure_stage"], "inventory_update")

    def test_retry_after_file_deleted_recovers_cleanly(self) -> None:
        """When an operation was interrupted after file_deleted, a retry with valid receipt recovers."""
        self._write_lock_file(self.lease_id, self.conv_id)
        disp_req = build_disposition_request(attachment_id=self.att_id_01, index_entry_sha256=self.sample_idx_hash, decision=DECISION_DISCARD)
        disp_rcpt = self._make_disposition_receipt(disp_req)
        record_disposition_entry(self.log_path, payload={"attachment_id": self.att_id_01, "decision": DECISION_DISCARD, "approval_receipt": disp_rcpt}, workspace_root=self.ws_root, lease_id=self.lease_id, conversation_id=self.conv_id)

        log_data = load_disposition_log(self.log_path)
        dec_id = log_data["latest_by_attachment_id"][self.att_id_01]["decision_id"]

        apply_req = build_apply_request(
            attachment_id=self.att_id_01,
            decision_id=dec_id,
            index_entry_sha256=self.sample_idx_hash,
            quarantine_path=self.sample_entry["quarantine_path"],
            sha256=self.sample_entry["sha256"],
            size_bytes=self.sample_entry["size_bytes"],
            run_id=self.sample_entry["run_id"],
        )
        apply_rcpt = self._make_apply_receipt(apply_req)
        apply_rcpt_hash = canonical_receipt_sha256(apply_rcpt)

        # Unlink target file and simulate a journal entry in file_deleted state
        target_file = self.ws_root / PurePosixPath(self.sample_entry["quarantine_path"])
        target_file.unlink()

        from core.attachment_disposition_log import record_journal_state
        record_journal_state(
            self.journal_path,
            attachment_id=self.att_id_01,
            decision_id=dec_id,
            apply_receipt_hash=apply_rcpt_hash,
            apply_request_hash=canonical_apply_request_sha256(apply_req),
            previous_index_entry_sha256=self.sample_idx_hash,
            quarantine_path=self.sample_entry["quarantine_path"],
            sha256=self.sample_entry["sha256"],
            size_bytes=self.sample_entry["size_bytes"],
            run_id=self.sample_entry["run_id"],
            state=JOURNAL_STATE_PREPARED,
        )
        record_journal_state(
            self.journal_path,
            attachment_id=self.att_id_01,
            decision_id=dec_id,
            apply_receipt_hash=apply_rcpt_hash,
            apply_request_hash=canonical_apply_request_sha256(apply_req),
            previous_index_entry_sha256=self.sample_idx_hash,
            quarantine_path=self.sample_entry["quarantine_path"],
            sha256=self.sample_entry["sha256"],
            size_bytes=self.sample_entry["size_bytes"],
            run_id=self.sample_entry["run_id"],
            state=JOURNAL_STATE_FILE_DELETED,
        )

        # Retry apply_discard with the same receipt -> should recover, update inventory and index
        res = apply_discard(
            attachment_id=self.att_id_01,
            apply_receipt=apply_rcpt,
            workspace_root=self.ws_root,
            lease_id=self.lease_id,
            conversation_id=self.conv_id,
        )
        self.assertEqual(res["status"], "completed")
        self.assertTrue(res.get("recovered"))

        # Quarantine index item now cleanly removed
        disk_idx = load_quarantine_index(self.index_path)
        self.assertNotIn(self.att_id_01, disk_idx["items"])

        # Journal now in completed state
        journal = load_discard_journal(self.journal_path)
        entries = list(journal["entries"].values())
        self.assertEqual(entries[0]["state"], JOURNAL_STATE_COMPLETED)

    def test_retry_after_inventory_updated_recovers_cleanly(self) -> None:
        """When an operation was interrupted after inventory_updated, a retry removes index entry cleanly."""
        self._write_lock_file(self.lease_id, self.conv_id)
        disp_req = build_disposition_request(attachment_id=self.att_id_01, index_entry_sha256=self.sample_idx_hash, decision=DECISION_DISCARD)
        disp_rcpt = self._make_disposition_receipt(disp_req)
        record_disposition_entry(self.log_path, payload={"attachment_id": self.att_id_01, "decision": DECISION_DISCARD, "approval_receipt": disp_rcpt}, workspace_root=self.ws_root, lease_id=self.lease_id, conversation_id=self.conv_id)

        log_data = load_disposition_log(self.log_path)
        dec_id = log_data["latest_by_attachment_id"][self.att_id_01]["decision_id"]

        apply_req = build_apply_request(
            attachment_id=self.att_id_01,
            decision_id=dec_id,
            index_entry_sha256=self.sample_idx_hash,
            quarantine_path=self.sample_entry["quarantine_path"],
            sha256=self.sample_entry["sha256"],
            size_bytes=self.sample_entry["size_bytes"],
            run_id=self.sample_entry["run_id"],
        )
        apply_rcpt = self._make_apply_receipt(apply_req)
        apply_rcpt_hash = canonical_receipt_sha256(apply_rcpt)

        # Unlink target file and update inventory manually
        target_file = self.ws_root / PurePosixPath(self.sample_entry["quarantine_path"])
        target_file.unlink()
        run_dir = self.attachments_root / self.sample_entry["run_id"]
        update_quarantine_inventory_atomic(run_dir, self.sample_entry["message_id"], self.sample_entry["clean_filename"])

        from core.attachment_disposition_log import record_journal_state
        record_journal_state(
            self.journal_path,
            attachment_id=self.att_id_01,
            decision_id=dec_id,
            apply_receipt_hash=apply_rcpt_hash,
            apply_request_hash=canonical_apply_request_sha256(apply_req),
            previous_index_entry_sha256=self.sample_idx_hash,
            quarantine_path=self.sample_entry["quarantine_path"],
            sha256=self.sample_entry["sha256"],
            size_bytes=self.sample_entry["size_bytes"],
            run_id=self.sample_entry["run_id"],
            state=JOURNAL_STATE_PREPARED,
        )
        record_journal_state(
            self.journal_path,
            attachment_id=self.att_id_01,
            decision_id=dec_id,
            apply_receipt_hash=apply_rcpt_hash,
            apply_request_hash=canonical_apply_request_sha256(apply_req),
            previous_index_entry_sha256=self.sample_idx_hash,
            quarantine_path=self.sample_entry["quarantine_path"],
            sha256=self.sample_entry["sha256"],
            size_bytes=self.sample_entry["size_bytes"],
            run_id=self.sample_entry["run_id"],
            state=JOURNAL_STATE_FILE_DELETED,
        )
        record_journal_state(
            self.journal_path,
            attachment_id=self.att_id_01,
            decision_id=dec_id,
            apply_receipt_hash=apply_rcpt_hash,
            apply_request_hash=canonical_apply_request_sha256(apply_req),
            previous_index_entry_sha256=self.sample_idx_hash,
            quarantine_path=self.sample_entry["quarantine_path"],
            sha256=self.sample_entry["sha256"],
            size_bytes=self.sample_entry["size_bytes"],
            run_id=self.sample_entry["run_id"],
            state=JOURNAL_STATE_INVENTORY_UPDATED,
        )

        res = apply_discard(
            attachment_id=self.att_id_01,
            apply_receipt=apply_rcpt,
            workspace_root=self.ws_root,
            lease_id=self.lease_id,
            conversation_id=self.conv_id,
        )
        self.assertEqual(res["status"], "completed")

        # Quarantine index item now cleanly removed
        disk_idx = load_quarantine_index(self.index_path)
        self.assertNotIn(self.att_id_01, disk_idx["items"])

        # Journal now in completed state
        journal = load_discard_journal(self.journal_path)
        entries = list(journal["entries"].values())
        self.assertEqual(entries[0]["state"], JOURNAL_STATE_COMPLETED)

    def test_forbidden_content_in_receipt_fails(self) -> None:
        """Passing credentials, secrets, or prompts inside a receipt fails closed."""
        self._write_lock_file(self.lease_id, self.conv_id)
        disp_req = build_disposition_request(attachment_id=self.att_id_01, index_entry_sha256=self.sample_idx_hash, decision=DECISION_DISCARD)
        bad_rcpt = self._make_disposition_receipt(disp_req)
        bad_rcpt["credentials"] = "sk-secret-token"

        with self.assertRaises(ForbiddenContentError):
            record_disposition_entry(
                self.log_path,
                payload={
                    "attachment_id": self.att_id_01,
                    "decision": DECISION_DISCARD,
                    "approval_receipt": bad_rcpt,
                },
                workspace_root=self.ws_root,
                lease_id=self.lease_id,
                conversation_id=self.conv_id,
            )

    def test_tampered_or_foreign_recovery_journal_rejected(self) -> None:
        """A recovery journal entry with mismatched prior index hash or receipt is rejected."""
        self._write_lock_file(self.lease_id, self.conv_id)
        disp_req = build_disposition_request(attachment_id=self.att_id_01, index_entry_sha256=self.sample_idx_hash, decision=DECISION_DISCARD)
        disp_rcpt = self._make_disposition_receipt(disp_req)
        record_disposition_entry(self.log_path, payload={"attachment_id": self.att_id_01, "decision": DECISION_DISCARD, "approval_receipt": disp_rcpt}, workspace_root=self.ws_root, lease_id=self.lease_id, conversation_id=self.conv_id)

        target_file = self.ws_root / PurePosixPath(self.sample_entry["quarantine_path"])
        target_file.unlink()

        log_data = load_disposition_log(self.log_path)
        dec_id = log_data["latest_by_attachment_id"][self.att_id_01]["decision_id"]

        apply_req = build_apply_request(
            attachment_id=self.att_id_01,
            decision_id=dec_id,
            index_entry_sha256=self.sample_idx_hash,
            quarantine_path=self.sample_entry["quarantine_path"],
            sha256=self.sample_entry["sha256"],
            size_bytes=self.sample_entry["size_bytes"],
            run_id=self.sample_entry["run_id"],
        )
        apply_rcpt = self._make_apply_receipt(apply_req)

        # Plant foreign journal entry with wrong prior index hash and matching valid request hash
        from core.attachment_disposition_log import record_journal_state
        wrong_idx_hash = "0" * 64
        wrong_apply_req = build_apply_request(
            attachment_id=self.att_id_01,
            decision_id=dec_id,
            index_entry_sha256=wrong_idx_hash,
            quarantine_path=self.sample_entry["quarantine_path"],
            sha256=self.sample_entry["sha256"],
            size_bytes=self.sample_entry["size_bytes"],
            run_id=self.sample_entry["run_id"],
        )
        wrong_rcpt = self._make_apply_receipt(wrong_apply_req)
        wrong_rcpt_hash = canonical_receipt_sha256(wrong_rcpt)
        wrong_req_hash = canonical_apply_request_sha256(wrong_apply_req)

        record_journal_state(
            self.journal_path,
            attachment_id=self.att_id_01,
            decision_id=dec_id,
            apply_receipt_hash=wrong_rcpt_hash,
            apply_request_hash=wrong_req_hash,
            previous_index_entry_sha256=wrong_idx_hash,
            quarantine_path=self.sample_entry["quarantine_path"],
            sha256=self.sample_entry["sha256"],
            size_bytes=self.sample_entry["size_bytes"],
            run_id=self.sample_entry["run_id"],
            state=JOURNAL_STATE_PREPARED,
        )
        record_journal_state(
            self.journal_path,
            attachment_id=self.att_id_01,
            decision_id=dec_id,
            apply_receipt_hash=wrong_rcpt_hash,
            apply_request_hash=wrong_req_hash,
            previous_index_entry_sha256=wrong_idx_hash,
            quarantine_path=self.sample_entry["quarantine_path"],
            sha256=self.sample_entry["sha256"],
            size_bytes=self.sample_entry["size_bytes"],
            run_id=self.sample_entry["run_id"],
            state=JOURNAL_STATE_FILE_DELETED,
        )

        with self.assertRaises(RecoveryEvidenceMissingError):
            apply_discard(
                attachment_id=self.att_id_01,
                apply_receipt=apply_rcpt,
                workspace_root=self.ws_root,
                lease_id=self.lease_id,
                conversation_id=self.conv_id,
            )

    # --------------------------------------------------------------------------
    # 4. Standard Lifecycle Operations: Retain, Promote, Discard (End-to-End)
    # --------------------------------------------------------------------------
    def test_record_retain_with_review_after(self) -> None:
        """Recording retain decision with review_after succeeds and report marks protected."""
        self._write_lock_file(self.lease_id, self.conv_id)
        disp_req = build_disposition_request(
            attachment_id=self.att_id_01,
            index_entry_sha256=self.sample_idx_hash,
            decision=DECISION_RETAIN,
            review_after="2026-10-01T00:00:00Z",
            rationale="Auditing pending for Q3",
        )
        disp_rcpt = self._make_disposition_receipt(disp_req)
        res = record_disposition_entry(
            self.log_path,
            payload={
                "attachment_id": self.att_id_01,
                "decision": DECISION_RETAIN,
                "review_after": "2026-10-01T00:00:00Z",
                "rationale": "Auditing pending for Q3",
                "approval_receipt": disp_rcpt,
            },
            workspace_root=self.ws_root,
            lease_id=self.lease_id,
            conversation_id=self.conv_id,
        )
        self.assertEqual(res["status"], "recorded")

        # Report marks item as protected
        rep = report_dispositions(workspace_root=self.ws_root)
        self.assertEqual(rep["summary"]["protected"], 1)
        self.assertEqual(rep["summary"]["eligible"], 0)

    def test_record_promote_with_candidate_review_hash(self) -> None:
        """Recording promote decision binds candidate_review_hash and report marks protected."""
        self._write_lock_file(self.lease_id, self.conv_id)
        cand_hash = "c" * 64
        disp_req = build_disposition_request(
            attachment_id=self.att_id_01,
            index_entry_sha256=self.sample_idx_hash,
            decision=DECISION_PROMOTE,
            candidate_review_hash=cand_hash,
            promotion_status="pending",
        )
        disp_rcpt = self._make_disposition_receipt(disp_req)
        res = record_disposition_entry(
            self.log_path,
            payload={
                "attachment_id": self.att_id_01,
                "decision": DECISION_PROMOTE,
                "candidate_review_hash": cand_hash,
                "promotion_status": "pending",
                "approval_receipt": disp_rcpt,
            },
            workspace_root=self.ws_root,
            lease_id=self.lease_id,
            conversation_id=self.conv_id,
        )
        self.assertEqual(res["status"], "recorded")

        rep = report_dispositions(workspace_root=self.ws_root)
        self.assertEqual(rep["summary"]["protected"], 1)
        self.assertEqual(rep["summary"]["eligible"], 0)

    def test_apply_discard_happy_path_end_to_end(self) -> None:
        """Full end-to-end authorized discard: report eligible -> apply -> file deleted, inventory & index updated."""
        self._write_lock_file(self.lease_id, self.conv_id)
        disp_req = build_disposition_request(
            attachment_id=self.att_id_01,
            index_entry_sha256=self.sample_idx_hash,
            decision=DECISION_DISCARD,
            rationale="Invoice processed and verified",
        )
        disp_rcpt = self._make_disposition_receipt(disp_req)
        record_res = record_disposition_entry(
            self.log_path,
            payload={
                "attachment_id": self.att_id_01,
                "decision": DECISION_DISCARD,
                "rationale": "Invoice processed and verified",
                "approval_receipt": disp_rcpt,
            },
            workspace_root=self.ws_root,
            lease_id=self.lease_id,
            conversation_id=self.conv_id,
        )
        self.assertEqual(record_res["status"], "recorded")

        # 1. Check report: eligible == 1
        rep = report_dispositions(workspace_root=self.ws_root)
        self.assertEqual(rep["summary"]["eligible"], 1)
        self.assertEqual(rep["summary"]["protected"], 0)
        self.assertEqual(rep["summary"]["invalid"], 0)

        # 2. Build apply receipt
        log_data = load_disposition_log(self.log_path)
        dec_id = log_data["latest_by_attachment_id"][self.att_id_01]["decision_id"]

        apply_req = build_apply_request(
            attachment_id=self.att_id_01,
            decision_id=dec_id,
            index_entry_sha256=self.sample_idx_hash,
            quarantine_path=self.sample_entry["quarantine_path"],
            sha256=self.sample_entry["sha256"],
            size_bytes=self.sample_entry["size_bytes"],
            run_id=self.sample_entry["run_id"],
        )
        apply_rcpt = self._make_apply_receipt(apply_req)

        # 3. Execute apply_discard
        apply_res = apply_discard(
            attachment_id=self.att_id_01,
            apply_receipt=apply_rcpt,
            workspace_root=self.ws_root,
            lease_id=self.lease_id,
            conversation_id=self.conv_id,
        )
        self.assertEqual(apply_res["status"], "completed")
        self.assertEqual(apply_res["deleted_count"], 1)

        # 4. Verify physical file is gone
        target_file = self.ws_root / PurePosixPath(self.sample_entry["quarantine_path"])
        self.assertFalse(target_file.exists())

        # 5. Verify index no longer has item
        disk_idx = load_quarantine_index(self.index_path)
        self.assertNotIn(self.att_id_01, disk_idx["items"])

        # 6. Verify inventory was atomically updated
        inv_file = self.attachments_root / self.sample_entry["run_id"] / ".quarantine-inventory.json"
        inv = json.loads(inv_file.read_text(encoding="utf-8"))
        msg_files = inv.get("messages", {}).get(self.sample_entry["message_id"], {}).get("files", {})
        self.assertNotIn(self.sample_entry["clean_filename"], msg_files)

        # 7. Verify journal is in completed state
        journal = load_discard_journal(self.journal_path)
        entries = list(journal["entries"].values())
        self.assertEqual(entries[0]["state"], JOURNAL_STATE_COMPLETED)

        # 8. Verify final-location-index.json is byte-identical
        self.assertEqual(self.final_index_path.read_bytes(), self.final_index_content)

    # --------------------------------------------------------------------------
    # 5. Pre-Unlink Gate Checks: Symlinks, Reparse Points, Active Run
    # --------------------------------------------------------------------------
    def test_active_run_evidence_prevents_deletion(self) -> None:
        """An active run lock file in run directory prevents discard apply."""
        self._write_lock_file(self.lease_id, self.conv_id)
        disp_req = build_disposition_request(attachment_id=self.att_id_01, index_entry_sha256=self.sample_idx_hash, decision=DECISION_DISCARD)
        disp_rcpt = self._make_disposition_receipt(disp_req)
        record_disposition_entry(self.log_path, payload={"attachment_id": self.att_id_01, "decision": DECISION_DISCARD, "approval_receipt": disp_rcpt}, workspace_root=self.ws_root, lease_id=self.lease_id, conversation_id=self.conv_id)

        # Place active inventory lock in run dir
        run_dir = self.attachments_root / self.sample_entry["run_id"]
        inv_lock = run_dir / ".quarantine-inventory.lock"
        inv_lock.write_text("locked", encoding="utf-8")

        log_data = load_disposition_log(self.log_path)
        dec_id = log_data["latest_by_attachment_id"][self.att_id_01]["decision_id"]

        apply_req = build_apply_request(
            attachment_id=self.att_id_01,
            decision_id=dec_id,
            index_entry_sha256=self.sample_idx_hash,
            quarantine_path=self.sample_entry["quarantine_path"],
            sha256=self.sample_entry["sha256"],
            size_bytes=self.sample_entry["size_bytes"],
            run_id=self.sample_entry["run_id"],
        )
        apply_rcpt = self._make_apply_receipt(apply_req)

        with self.assertRaises(DispositionApplyError):
            apply_discard(
                attachment_id=self.att_id_01,
                apply_receipt=apply_rcpt,
                workspace_root=self.ws_root,
                lease_id=self.lease_id,
                conversation_id=self.conv_id,
            )

        # File and index remain intact
        target_file = self.ws_root / PurePosixPath(self.sample_entry["quarantine_path"])
        self.assertTrue(target_file.exists())
        disk_idx = load_quarantine_index(self.index_path)
        self.assertIn(self.att_id_01, disk_idx["items"])

    def test_byte_drift_immediately_before_unlink_fails(self) -> None:
        """Tampering with file bytes between record and apply aborts deletion before unlink."""
        self._write_lock_file(self.lease_id, self.conv_id)
        disp_req = build_disposition_request(attachment_id=self.att_id_01, index_entry_sha256=self.sample_idx_hash, decision=DECISION_DISCARD)
        disp_rcpt = self._make_disposition_receipt(disp_req)
        record_disposition_entry(self.log_path, payload={"attachment_id": self.att_id_01, "decision": DECISION_DISCARD, "approval_receipt": disp_rcpt}, workspace_root=self.ws_root, lease_id=self.lease_id, conversation_id=self.conv_id)

        # Corrupt file bytes on disk
        target_file = self.ws_root / PurePosixPath(self.sample_entry["quarantine_path"])
        target_file.write_bytes(b"tampered file content")

        log_data = load_disposition_log(self.log_path)
        dec_id = log_data["latest_by_attachment_id"][self.att_id_01]["decision_id"]

        apply_req = build_apply_request(
            attachment_id=self.att_id_01,
            decision_id=dec_id,
            index_entry_sha256=self.sample_idx_hash,
            quarantine_path=self.sample_entry["quarantine_path"],
            sha256=self.sample_entry["sha256"],
            size_bytes=self.sample_entry["size_bytes"],
            run_id=self.sample_entry["run_id"],
        )
        apply_rcpt = self._make_apply_receipt(apply_req)

        with self.assertRaises(PhysicalVerificationError):
            apply_discard(
                attachment_id=self.att_id_01,
                apply_receipt=apply_rcpt,
                workspace_root=self.ws_root,
                lease_id=self.lease_id,
                conversation_id=self.conv_id,
            )

        disk_idx = load_quarantine_index(self.index_path)
        self.assertIn(self.att_id_01, disk_idx["items"])

    # --------------------------------------------------------------------------
    # 6. CLI Interface with Verifiable Receipts
    # --------------------------------------------------------------------------
    def test_cli_facade_operations(self) -> None:
        """CLI facade mail_desk_attachment_disposition.py supports record, report, and apply-discard with receipts."""
        self._write_lock_file(self.lease_id, self.conv_id)
        cli_script = _scripts_dir / "mail_desk_attachment_disposition.py"

        # 1. Report
        proc_rep = subprocess.run(
            [sys.executable, str(cli_script), "--workspace-root", str(self.ws_root), "--json", "report"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(proc_rep.returncode, 0)
        rep_json = json.loads(proc_rep.stdout)
        self.assertTrue(rep_json["success"])
        self.assertEqual(rep_json["data"]["report"]["summary"]["protected"], 1)

        # 2. Record with receipt file
        disp_req = build_disposition_request(
            attachment_id=self.att_id_01,
            index_entry_sha256=self.sample_idx_hash,
            decision=DECISION_DISCARD,
            rationale="CLI test discard",
        )
        disp_rcpt = self._make_disposition_receipt(disp_req)
        rcpt_file = Path(self.temp_dir) / "disp_receipt.json"
        rcpt_file.write_text(json.dumps(disp_rcpt, indent=2), encoding="utf-8")

        proc_rec = subprocess.run(
            [
                sys.executable, str(cli_script),
                "--workspace-root", str(self.ws_root),
                "--lease-id", self.lease_id,
                "--conversation-id", self.conv_id,
                "--json", "record",
                "--attachment-id", self.att_id_01,
                "--index-entry-sha256", self.sample_idx_hash,
                "--decision", DECISION_DISCARD,
                "--rationale", "CLI test discard",
                "--receipt", str(rcpt_file),
            ],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(proc_rec.returncode, 0, f"CLI record failed: {proc_rec.stderr}")
        rec_json = json.loads(proc_rec.stdout)
        self.assertTrue(rec_json["success"])

        # 3. Apply without receipt fails
        proc_apply_noreceipt = subprocess.run(
            [
                sys.executable, str(cli_script),
                "--workspace-root", str(self.ws_root),
                "--lease-id", self.lease_id,
                "--conversation-id", self.conv_id,
                "--json", "apply-discard",
                "--attachment-id", self.att_id_01,
            ],
            capture_output=True, text=True, check=False,
        )
        self.assertNotEqual(proc_apply_noreceipt.returncode, 0)

        # 4. Apply with valid receipt succeeds
        log_data = load_disposition_log(self.log_path)
        dec_id = log_data["latest_by_attachment_id"][self.att_id_01]["decision_id"]
        apply_req = build_apply_request(
            attachment_id=self.att_id_01,
            decision_id=dec_id,
            index_entry_sha256=self.sample_idx_hash,
            quarantine_path=self.sample_entry["quarantine_path"],
            sha256=self.sample_entry["sha256"],
            size_bytes=self.sample_entry["size_bytes"],
            run_id=self.sample_entry["run_id"],
        )
        apply_rcpt = self._make_apply_receipt(apply_req)
        apply_rcpt_file = Path(self.temp_dir) / "apply_receipt.json"
        apply_rcpt_file.write_text(json.dumps(apply_rcpt, indent=2), encoding="utf-8")

        proc_apply = subprocess.run(
            [
                sys.executable, str(cli_script),
                "--workspace-root", str(self.ws_root),
                "--lease-id", self.lease_id,
                "--conversation-id", self.conv_id,
                "--json", "apply-discard",
                "--attachment-id", self.att_id_01,
                "--receipt", str(apply_rcpt_file),
            ],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(proc_apply.returncode, 0, f"CLI apply failed: {proc_apply.stderr}")
        apply_json = json.loads(proc_apply.stdout)
        self.assertTrue(apply_json["success"])
        self.assertEqual(apply_json["data"]["deleted_count"], 1)

    # --------------------------------------------------------------------------
    # 7. Recovery Journal Hardening & Adversarial Tests
    # --------------------------------------------------------------------------
    def test_real_inventory_update_error_e2e_and_retry(self) -> None:
        """Partial failure at inventory update leaves file deleted, records failure in journal, and subsequent retry succeeds."""
        self._write_lock_file(self.lease_id, self.conv_id)
        disp_req = build_disposition_request(
            attachment_id=self.att_id_01,
            index_entry_sha256=self.sample_idx_hash,
            decision=DECISION_DISCARD,
            rationale="Test partial inventory failure and recovery",
        )
        disp_rcpt = self._make_disposition_receipt(disp_req)
        record_disposition_entry(
            self.log_path,
            payload={
                "attachment_id": self.att_id_01,
                "decision": DECISION_DISCARD,
                "rationale": "Test partial inventory failure and recovery",
                "approval_receipt": disp_rcpt,
            },
            workspace_root=self.ws_root,
            lease_id=self.lease_id,
            conversation_id=self.conv_id,
        )

        log_data = load_disposition_log(self.log_path)
        dec_id = log_data["latest_by_attachment_id"][self.att_id_01]["decision_id"]

        apply_req = build_apply_request(
            attachment_id=self.att_id_01,
            decision_id=dec_id,
            index_entry_sha256=self.sample_idx_hash,
            quarantine_path=self.sample_entry["quarantine_path"],
            sha256=self.sample_entry["sha256"],
            size_bytes=self.sample_entry["size_bytes"],
            run_id=self.sample_entry["run_id"],
        )
        apply_rcpt = self._make_apply_receipt(apply_req)

        # 1. Trigger failure during atomic inventory update
        with patch(
            "core.attachment_disposition_log.update_quarantine_inventory_atomic",
            side_effect=InventoryUpdateError("simulated inventory disk error"),
        ):
            with self.assertRaises(DispositionApplyError):
                apply_discard(
                    attachment_id=self.att_id_01,
                    apply_receipt=apply_rcpt,
                    workspace_root=self.ws_root,
                    lease_id=self.lease_id,
                    conversation_id=self.conv_id,
                )

        # Verify physical file was unlinked
        target_file = self.ws_root / PurePosixPath(self.sample_entry["quarantine_path"])
        self.assertFalse(target_file.exists())

        # Verify index was not touched
        disk_idx = load_quarantine_index(self.index_path)
        self.assertIn(self.att_id_01, disk_idx["items"])

        # Verify journal status is failed, last_successful_state is file_deleted
        jdata = load_discard_journal(self.journal_path)
        entry = list(jdata["entries"].values())[0]
        self.assertEqual(entry["status"], "failed")
        self.assertEqual(entry["last_successful_state"], JOURNAL_STATE_FILE_DELETED)
        self.assertEqual(entry["state"], JOURNAL_STATE_FILE_DELETED)
        self.assertEqual(entry["failure_stage"], "inventory_update")

        # 2. Retry apply_discard with the SAME receipt without error -> must resume and complete
        retry_res = apply_discard(
            attachment_id=self.att_id_01,
            apply_receipt=apply_rcpt,
            workspace_root=self.ws_root,
            lease_id=self.lease_id,
            conversation_id=self.conv_id,
        )
        self.assertEqual(retry_res["status"], "completed")
        self.assertTrue(retry_res.get("recovered"))

        # Target file still does not exist
        self.assertFalse(target_file.exists())

        # Index item removed
        disk_idx = load_quarantine_index(self.index_path)
        self.assertNotIn(self.att_id_01, disk_idx["items"])

        # Inventory updated
        inv_file = self.attachments_root / self.sample_entry["run_id"] / ".quarantine-inventory.json"
        inv = json.loads(inv_file.read_text(encoding="utf-8"))
        msg_files = inv.get("messages", {}).get(self.sample_entry["message_id"], {}).get("files", {})
        self.assertNotIn(self.sample_entry["clean_filename"], msg_files)

        # Journal completed
        jdata_final = load_discard_journal(self.journal_path)
        entry_final = list(jdata_final["entries"].values())[0]
        self.assertEqual(entry_final["status"], "completed")
        self.assertEqual(entry_final["state"], JOURNAL_STATE_COMPLETED)
        self.assertEqual(entry_final["last_successful_state"], JOURNAL_STATE_COMPLETED)
        self.assertIsNone(entry_final["failure_stage"])

    def test_tampered_state_with_matching_key_rejected(self) -> None:
        """A journal entry with state tampered to file_deleted without matching monotonic history fails validation."""
        self._write_lock_file(self.lease_id, self.conv_id)
        req = build_apply_request(
            attachment_id=self.att_id_01,
            decision_id="d" * 64,
            index_entry_sha256=self.sample_idx_hash,
            quarantine_path=self.sample_entry["quarantine_path"],
            sha256=self.sample_entry["sha256"],
            size_bytes=self.sample_entry["size_bytes"],
            run_id=self.sample_entry["run_id"],
        )
        rcpt = self._make_apply_receipt(req)
        rcpt_hash = canonical_receipt_sha256(rcpt)
        req_hash = canonical_apply_request_sha256(req)

        from core.attachment_disposition_log import record_journal_state
        record_journal_state(
            self.journal_path,
            attachment_id=self.att_id_01,
            decision_id="d" * 64,
            apply_receipt_hash=rcpt_hash,
            apply_request_hash=req_hash,
            previous_index_entry_sha256=self.sample_idx_hash,
            quarantine_path=self.sample_entry["quarantine_path"],
            sha256=self.sample_entry["sha256"],
            size_bytes=self.sample_entry["size_bytes"],
            run_id=self.sample_entry["run_id"],
            state=JOURNAL_STATE_PREPARED,
        )

        # Tamper directly on disk: alter state to 'file_deleted' without corresponding history
        raw_j = json.loads(self.journal_path.read_text(encoding="utf-8"))
        j_key = list(raw_j["entries"].keys())[0]
        raw_j["entries"][j_key]["state"] = JOURNAL_STATE_FILE_DELETED
        raw_j["entries"][j_key]["last_successful_state"] = JOURNAL_STATE_FILE_DELETED
        self.journal_path.write_text(json.dumps(raw_j), encoding="utf-8")

        with self.assertRaises(RecoveryJournalCorruptedError) as ctx:
            load_discard_journal(self.journal_path)
        self.assertIn("does not match history-derived", str(ctx.exception))

    def test_tampered_scope_fields_rejected(self) -> None:
        """Tampering with immutable scope fields (quarantine_path, sha256, size_bytes, run_id) breaks apply_request_hash binding."""
        self._write_lock_file(self.lease_id, self.conv_id)
        req = build_apply_request(
            attachment_id=self.att_id_01,
            decision_id="d" * 64,
            index_entry_sha256=self.sample_idx_hash,
            quarantine_path=self.sample_entry["quarantine_path"],
            sha256=self.sample_entry["sha256"],
            size_bytes=self.sample_entry["size_bytes"],
            run_id=self.sample_entry["run_id"],
        )
        rcpt = self._make_apply_receipt(req)
        rcpt_hash = canonical_receipt_sha256(rcpt)
        req_hash = canonical_apply_request_sha256(req)

        from core.attachment_disposition_log import record_journal_state
        record_journal_state(
            self.journal_path,
            attachment_id=self.att_id_01,
            decision_id="d" * 64,
            apply_receipt_hash=rcpt_hash,
            apply_request_hash=req_hash,
            previous_index_entry_sha256=self.sample_idx_hash,
            quarantine_path=self.sample_entry["quarantine_path"],
            sha256=self.sample_entry["sha256"],
            size_bytes=self.sample_entry["size_bytes"],
            run_id=self.sample_entry["run_id"],
            state=JOURNAL_STATE_PREPARED,
        )

        raw_j = json.loads(self.journal_path.read_text(encoding="utf-8"))
        j_key = list(raw_j["entries"].keys())[0]

        # Case 1: Tamper size_bytes
        raw_j_tampered = json.loads(json.dumps(raw_j))
        raw_j_tampered["entries"][j_key]["size_bytes"] = 999999
        self.journal_path.write_text(json.dumps(raw_j_tampered), encoding="utf-8")
        with self.assertRaises(RecoveryJournalCorruptedError) as ctx:
            load_discard_journal(self.journal_path)
        self.assertIn("apply_request_hash drift", str(ctx.exception))

        # Case 2: Tamper quarantine_path
        raw_j_tampered = json.loads(json.dumps(raw_j))
        raw_j_tampered["entries"][j_key]["quarantine_path"] = "data/mail-desk/attachments/run_mdq3_01/evil.pdf"
        self.journal_path.write_text(json.dumps(raw_j_tampered), encoding="utf-8")
        with self.assertRaises(RecoveryJournalCorruptedError) as ctx:
            load_discard_journal(self.journal_path)
        self.assertIn("apply_request_hash drift", str(ctx.exception))

        # Case 3: Tamper sha256
        raw_j_tampered = json.loads(json.dumps(raw_j))
        raw_j_tampered["entries"][j_key]["sha256"] = "f" * 64
        self.journal_path.write_text(json.dumps(raw_j_tampered), encoding="utf-8")
        with self.assertRaises(RecoveryJournalCorruptedError) as ctx:
            load_discard_journal(self.journal_path)
        self.assertIn("apply_request_hash drift", str(ctx.exception))

    def test_unknown_journal_fields_rejected(self) -> None:
        """Root or entry unknown fields in discard journal are rejected fail-closed."""
        self._write_lock_file(self.lease_id, self.conv_id)
        req = build_apply_request(
            attachment_id=self.att_id_01,
            decision_id="d" * 64,
            index_entry_sha256=self.sample_idx_hash,
            quarantine_path=self.sample_entry["quarantine_path"],
            sha256=self.sample_entry["sha256"],
            size_bytes=self.sample_entry["size_bytes"],
            run_id=self.sample_entry["run_id"],
        )
        rcpt = self._make_apply_receipt(req)
        rcpt_hash = canonical_receipt_sha256(rcpt)
        req_hash = canonical_apply_request_sha256(req)

        from core.attachment_disposition_log import record_journal_state
        record_journal_state(
            self.journal_path,
            attachment_id=self.att_id_01,
            decision_id="d" * 64,
            apply_receipt_hash=rcpt_hash,
            apply_request_hash=req_hash,
            previous_index_entry_sha256=self.sample_idx_hash,
            quarantine_path=self.sample_entry["quarantine_path"],
            sha256=self.sample_entry["sha256"],
            size_bytes=self.sample_entry["size_bytes"],
            run_id=self.sample_entry["run_id"],
            state=JOURNAL_STATE_PREPARED,
        )

        raw_j = json.loads(self.journal_path.read_text(encoding="utf-8"))
        j_key = list(raw_j["entries"].keys())[0]

        # Case 1: Unknown root field
        raw_j_bad_root = json.loads(json.dumps(raw_j))
        raw_j_bad_root["extra_root_prop"] = True
        self.journal_path.write_text(json.dumps(raw_j_bad_root), encoding="utf-8")
        with self.assertRaises(RecoveryJournalCorruptedError) as ctx:
            load_discard_journal(self.journal_path)
        self.assertIn("Unknown root field", str(ctx.exception))

        # Case 2: Unknown entry field
        raw_j_bad_entry = json.loads(json.dumps(raw_j))
        raw_j_bad_entry["entries"][j_key]["malicious_injected_key"] = "payload"
        self.journal_path.write_text(json.dumps(raw_j_bad_entry), encoding="utf-8")
        with self.assertRaises(RecoveryJournalCorruptedError) as ctx:
            load_discard_journal(self.journal_path)
        self.assertIn("Unknown field(s) in journal entry", str(ctx.exception))

    def test_invalid_and_backward_transitions_rejected(self) -> None:
        """Backward transitions (e.g. completed -> prepared or file_deleted) are rejected."""
        self._write_lock_file(self.lease_id, self.conv_id)
        req = build_apply_request(
            attachment_id=self.att_id_01,
            decision_id="d" * 64,
            index_entry_sha256=self.sample_idx_hash,
            quarantine_path=self.sample_entry["quarantine_path"],
            sha256=self.sample_entry["sha256"],
            size_bytes=self.sample_entry["size_bytes"],
            run_id=self.sample_entry["run_id"],
        )
        rcpt = self._make_apply_receipt(req)
        rcpt_hash = canonical_receipt_sha256(rcpt)
        req_hash = canonical_apply_request_sha256(req)

        from core.attachment_disposition_log import record_journal_state
        record_journal_state(
            self.journal_path,
            attachment_id=self.att_id_01,
            decision_id="d" * 64,
            apply_receipt_hash=rcpt_hash,
            apply_request_hash=req_hash,
            previous_index_entry_sha256=self.sample_idx_hash,
            quarantine_path=self.sample_entry["quarantine_path"],
            sha256=self.sample_entry["sha256"],
            size_bytes=self.sample_entry["size_bytes"],
            run_id=self.sample_entry["run_id"],
            state=JOURNAL_STATE_PREPARED,
        )
        record_journal_state(
            self.journal_path,
            attachment_id=self.att_id_01,
            decision_id="d" * 64,
            apply_receipt_hash=rcpt_hash,
            apply_request_hash=req_hash,
            previous_index_entry_sha256=self.sample_idx_hash,
            quarantine_path=self.sample_entry["quarantine_path"],
            sha256=self.sample_entry["sha256"],
            size_bytes=self.sample_entry["size_bytes"],
            run_id=self.sample_entry["run_id"],
            state=JOURNAL_STATE_FILE_DELETED,
        )

        # Attempt backward transition file_deleted -> prepared
        with self.assertRaises(RecoveryJournalCorruptedError) as ctx:
            record_journal_state(
                self.journal_path,
                attachment_id=self.att_id_01,
                decision_id="d" * 64,
                apply_receipt_hash=rcpt_hash,
                apply_request_hash=req_hash,
                previous_index_entry_sha256=self.sample_idx_hash,
                quarantine_path=self.sample_entry["quarantine_path"],
                sha256=self.sample_entry["sha256"],
                size_bytes=self.sample_entry["size_bytes"],
                run_id=self.sample_entry["run_id"],
                state=JOURNAL_STATE_PREPARED,
            )
        self.assertIn("Invalid backward transition", str(ctx.exception))

    def test_skipped_transitions_rejected(self) -> None:
        """Skipped transitions (e.g. prepared -> completed or prepared -> inventory_updated) are rejected."""
        self._write_lock_file(self.lease_id, self.conv_id)
        req = build_apply_request(
            attachment_id=self.att_id_01,
            decision_id="d" * 64,
            index_entry_sha256=self.sample_idx_hash,
            quarantine_path=self.sample_entry["quarantine_path"],
            sha256=self.sample_entry["sha256"],
            size_bytes=self.sample_entry["size_bytes"],
            run_id=self.sample_entry["run_id"],
        )
        rcpt = self._make_apply_receipt(req)
        rcpt_hash = canonical_receipt_sha256(rcpt)
        req_hash = canonical_apply_request_sha256(req)

        from core.attachment_disposition_log import record_journal_state
        record_journal_state(
            self.journal_path,
            attachment_id=self.att_id_01,
            decision_id="d" * 64,
            apply_receipt_hash=rcpt_hash,
            apply_request_hash=req_hash,
            previous_index_entry_sha256=self.sample_idx_hash,
            quarantine_path=self.sample_entry["quarantine_path"],
            sha256=self.sample_entry["sha256"],
            size_bytes=self.sample_entry["size_bytes"],
            run_id=self.sample_entry["run_id"],
            state=JOURNAL_STATE_PREPARED,
        )

        # Attempt skipped transition prepared -> completed (skipping file_deleted, inventory_updated, index_updated)
        with self.assertRaises(RecoveryJournalCorruptedError) as ctx:
            record_journal_state(
                self.journal_path,
                attachment_id=self.att_id_01,
                decision_id="d" * 64,
                apply_receipt_hash=rcpt_hash,
                apply_request_hash=req_hash,
                previous_index_entry_sha256=self.sample_idx_hash,
                quarantine_path=self.sample_entry["quarantine_path"],
                sha256=self.sample_entry["sha256"],
                size_bytes=self.sample_entry["size_bytes"],
                run_id=self.sample_entry["run_id"],
                state=JOURNAL_STATE_COMPLETED,
            )
        self.assertIn("Invalid skipped transition", str(ctx.exception))

    def test_already_missing_index_with_wrong_receipt_rejected(self) -> None:
        """When quarantine index entry is already missing, passing a wrong/drifted receipt is rejected."""
        self._write_lock_file(self.lease_id, self.conv_id)
        disp_req = build_disposition_request(
            attachment_id=self.att_id_01,
            index_entry_sha256=self.sample_idx_hash,
            decision=DECISION_DISCARD,
            rationale="Test already missing index with wrong receipt",
        )
        disp_rcpt = self._make_disposition_receipt(disp_req)
        record_disposition_entry(
            self.log_path,
            payload={
                "attachment_id": self.att_id_01,
                "decision": DECISION_DISCARD,
                "rationale": "Test already missing index with wrong receipt",
                "approval_receipt": disp_rcpt,
            },
            workspace_root=self.ws_root,
            lease_id=self.lease_id,
            conversation_id=self.conv_id,
        )

        log_data = load_disposition_log(self.log_path)
        dec_id = log_data["latest_by_attachment_id"][self.att_id_01]["decision_id"]

        apply_req = build_apply_request(
            attachment_id=self.att_id_01,
            decision_id=dec_id,
            index_entry_sha256=self.sample_idx_hash,
            quarantine_path=self.sample_entry["quarantine_path"],
            sha256=self.sample_entry["sha256"],
            size_bytes=self.sample_entry["size_bytes"],
            run_id=self.sample_entry["run_id"],
        )
        apply_rcpt = self._make_apply_receipt(apply_req)

        # Complete discard first so file and index item are gone and journal is completed
        res1 = apply_discard(
            attachment_id=self.att_id_01,
            apply_receipt=apply_rcpt,
            workspace_root=self.ws_root,
            lease_id=self.lease_id,
            conversation_id=self.conv_id,
        )
        self.assertEqual(res1["status"], "completed")

        # Now pass a forged/wrong receipt for the already-missing item
        forged_rcpt = dict(apply_rcpt)
        forged_rcpt["request_hash"] = "0" * 64

        with self.assertRaises((ReceiptDriftError, DispositionApplyError)):
            apply_discard(
                attachment_id=self.att_id_01,
                apply_receipt=forged_rcpt,
                workspace_root=self.ws_root,
                lease_id=self.lease_id,
                conversation_id=self.conv_id,
            )

    def test_idempotent_retry_after_index_updated_or_completed(self) -> None:
        """Calling apply_discard after index_updated or completed returns completed idempotently."""
        self._write_lock_file(self.lease_id, self.conv_id)
        disp_req = build_disposition_request(
            attachment_id=self.att_id_01,
            index_entry_sha256=self.sample_idx_hash,
            decision=DECISION_DISCARD,
            rationale="Test idempotency after completion",
        )
        disp_rcpt = self._make_disposition_receipt(disp_req)
        record_disposition_entry(
            self.log_path,
            payload={
                "attachment_id": self.att_id_01,
                "decision": DECISION_DISCARD,
                "rationale": "Test idempotency after completion",
                "approval_receipt": disp_rcpt,
            },
            workspace_root=self.ws_root,
            lease_id=self.lease_id,
            conversation_id=self.conv_id,
        )

        log_data = load_disposition_log(self.log_path)
        dec_id = log_data["latest_by_attachment_id"][self.att_id_01]["decision_id"]

        apply_req = build_apply_request(
            attachment_id=self.att_id_01,
            decision_id=dec_id,
            index_entry_sha256=self.sample_idx_hash,
            quarantine_path=self.sample_entry["quarantine_path"],
            sha256=self.sample_entry["sha256"],
            size_bytes=self.sample_entry["size_bytes"],
            run_id=self.sample_entry["run_id"],
        )
        apply_rcpt = self._make_apply_receipt(apply_req)

        # 1. Complete discard
        res1 = apply_discard(
            attachment_id=self.att_id_01,
            apply_receipt=apply_rcpt,
            workspace_root=self.ws_root,
            lease_id=self.lease_id,
            conversation_id=self.conv_id,
        )
        self.assertEqual(res1["status"], "completed")

        # 2. Retry while completed -> returns completed idempotently
        res2 = apply_discard(
            attachment_id=self.att_id_01,
            apply_receipt=apply_rcpt,
            workspace_root=self.ws_root,
            lease_id=self.lease_id,
            conversation_id=self.conv_id,
        )
        self.assertEqual(res2["status"], "completed")
        self.assertEqual(res2.get("deleted_count"), 0)

    def test_truncated_history_only_completed_rejected(self) -> None:
        """P1: A discard journal whose history starts with completed (truncated) must be rejected."""
        self._write_lock_file(self.lease_id, self.conv_id)
        disp_req = build_disposition_request(
            attachment_id=self.att_id_01,
            index_entry_sha256=self.sample_idx_hash,
            decision=DECISION_DISCARD,
            rationale="Test truncated history",
        )
        disp_rcpt = self._make_disposition_receipt(disp_req)
        record_disposition_entry(
            self.log_path,
            payload={
                "attachment_id": self.att_id_01,
                "decision": DECISION_DISCARD,
                "rationale": "Test truncated history",
                "approval_receipt": disp_rcpt,
            },
            workspace_root=self.ws_root,
            lease_id=self.lease_id,
            conversation_id=self.conv_id,
        )

        log_data = load_disposition_log(self.log_path)
        dec_id = log_data["latest_by_attachment_id"][self.att_id_01]["decision_id"]

        apply_req = build_apply_request(
            attachment_id=self.att_id_01,
            decision_id=dec_id,
            index_entry_sha256=self.sample_idx_hash,
            quarantine_path=self.sample_entry["quarantine_path"],
            sha256=self.sample_entry["sha256"],
            size_bytes=self.sample_entry["size_bytes"],
            run_id=self.sample_entry["run_id"],
        )
        apply_rcpt = self._make_apply_receipt(apply_req)

        res = apply_discard(
            attachment_id=self.att_id_01,
            apply_receipt=apply_rcpt,
            workspace_root=self.ws_root,
            lease_id=self.lease_id,
            conversation_id=self.conv_id,
        )
        self.assertEqual(res["status"], "completed")

        # Corrupt journal history: truncate to only the last completed step
        journal = load_discard_journal(self.journal_path)
        for entry in journal["entries"].values():
            entry["history"] = [
                {"state": JOURNAL_STATE_COMPLETED, "status": "success", "timestamp": utc_now_iso()}
            ]
        self.journal_path.write_text(json.dumps(journal, indent=2), encoding="utf-8")

        with self.assertRaises(RecoveryJournalCorruptedError) as ctx:
            load_discard_journal(self.journal_path)
        self.assertIn("first history state must be 'prepared'", str(ctx.exception))

    def test_completed_state_with_failed_status_rejected(self) -> None:
        """P1: A journal entry with state=completed and status=failed must fail closed on load and candidate match."""
        self._write_lock_file(self.lease_id, self.conv_id)
        disp_req = build_disposition_request(
            attachment_id=self.att_id_01,
            index_entry_sha256=self.sample_idx_hash,
            decision=DECISION_DISCARD,
            rationale="Test contradictory state and status",
        )
        disp_rcpt = self._make_disposition_receipt(disp_req)
        record_disposition_entry(
            self.log_path,
            payload={
                "attachment_id": self.att_id_01,
                "decision": DECISION_DISCARD,
                "rationale": "Test contradictory state and status",
                "approval_receipt": disp_rcpt,
            },
            workspace_root=self.ws_root,
            lease_id=self.lease_id,
            conversation_id=self.conv_id,
        )

        log_data = load_disposition_log(self.log_path)
        dec_id = log_data["latest_by_attachment_id"][self.att_id_01]["decision_id"]

        apply_req = build_apply_request(
            attachment_id=self.att_id_01,
            decision_id=dec_id,
            index_entry_sha256=self.sample_idx_hash,
            quarantine_path=self.sample_entry["quarantine_path"],
            sha256=self.sample_entry["sha256"],
            size_bytes=self.sample_entry["size_bytes"],
            run_id=self.sample_entry["run_id"],
        )
        apply_rcpt = self._make_apply_receipt(apply_req)

        res = apply_discard(
            attachment_id=self.att_id_01,
            apply_receipt=apply_rcpt,
            workspace_root=self.ws_root,
            lease_id=self.lease_id,
            conversation_id=self.conv_id,
        )
        self.assertEqual(res["status"], "completed")

        # Modify journal on disk to have state=completed but status=failed
        journal = load_discard_journal(self.journal_path)
        for entry in journal["entries"].values():
            entry["status"] = "failed"
            entry["failure_stage"] = "completion"
            entry["error"] = {"stage": "completion", "error": "simulated contradictory failure"}
        self.journal_path.write_text(json.dumps(journal, indent=2), encoding="utf-8")

        # 1. load_discard_journal fails closed
        with self.assertRaises(RecoveryJournalCorruptedError) as ctx:
            load_discard_journal(self.journal_path)
        self.assertIn("cannot have state 'completed'", str(ctx.exception))

        # 2. apply_discard rejects it and does not treat it as idempotent completed
        with self.assertRaises((RecoveryJournalCorruptedError, DispositionApplyError)):
            apply_discard(
                attachment_id=self.att_id_01,
                apply_receipt=apply_rcpt,
                workspace_root=self.ws_root,
                lease_id=self.lease_id,
                conversation_id=self.conv_id,
            )

    def test_zero_or_negative_size_bytes_rejected(self) -> None:
        """P2: size_bytes must be strictly positive integer > 0 across requests, journal, and index."""
        # 1. build_apply_request rejects 0, -1, bool
        for bad_size in (0, -1, True, False):
            with self.assertRaises(DispositionApplyError):
                build_apply_request(
                    attachment_id=self.att_id_01,
                    decision_id="a" * 64,
                    index_entry_sha256=self.sample_idx_hash,
                    quarantine_path=self.sample_entry["quarantine_path"],
                    sha256=self.sample_entry["sha256"],
                    size_bytes=bad_size,
                    run_id=self.sample_entry["run_id"],
                )

        # 2. record_journal_state rejects 0 and negative
        req_hash = "b" * 64
        rcpt_hash = "c" * 64
        with self.assertRaises(ValueError):
            record_journal_state(
                self.journal_path,
                attachment_id=self.att_id_01,
                decision_id="a" * 64,
                apply_receipt_hash=rcpt_hash,
                apply_request_hash=req_hash,
                previous_index_entry_sha256=self.sample_idx_hash,
                quarantine_path=self.sample_entry["quarantine_path"],
                sha256=self.sample_entry["sha256"],
                size_bytes=0,
                run_id=self.sample_entry["run_id"],
                state=JOURNAL_STATE_PREPARED,
            )

        # 3. record_journal_failure rejects 0 and negative
        with self.assertRaises(ValueError):
            record_journal_failure(
                self.journal_path,
                attachment_id=self.att_id_01,
                decision_id="a" * 64,
                apply_receipt_hash=rcpt_hash,
                apply_request_hash=req_hash,
                previous_index_entry_sha256=self.sample_idx_hash,
                quarantine_path=self.sample_entry["quarantine_path"],
                sha256=self.sample_entry["sha256"],
                size_bytes=0,
                run_id=self.sample_entry["run_id"],
                stage="preparation",
                error={"stage": "preparation", "error": "test"},
            )

        # 4. load_discard_journal rejects size_bytes <= 0
        jid = hashlib.sha256(
            f"{self.att_id_01}:{'a' * 64}:{rcpt_hash}:{self.sample_idx_hash}".encode("utf-8")
        ).hexdigest()
        valid_entry = {
            "journal_entry_id": jid,
            "attachment_id": self.att_id_01,
            "decision_id": "a" * 64,
            "apply_receipt_hash": rcpt_hash,
            "apply_request_hash": req_hash,
            "previous_index_entry_sha256": self.sample_idx_hash,
            "quarantine_path": self.sample_entry["quarantine_path"],
            "sha256": self.sample_entry["sha256"],
            "size_bytes": 0,
            "run_id": self.sample_entry["run_id"],
            "state": JOURNAL_STATE_PREPARED,
            "last_successful_state": JOURNAL_STATE_PREPARED,
            "status": "in_progress",
            "created_at": utc_now_iso(),
            "updated_at": utc_now_iso(),
            "history": [{"state": JOURNAL_STATE_PREPARED, "status": "success", "timestamp": utc_now_iso()}],
            "failure_stage": None,
            "error": None,
        }
        test_j_data = {
            "schema_version": 1,
            "updated_at": utc_now_iso(),
            "entries": {jid: valid_entry},
        }
        self.journal_path.write_text(json.dumps(test_j_data, indent=2), encoding="utf-8")
        with self.assertRaises(RecoveryJournalCorruptedError) as ctx:
            load_discard_journal(self.journal_path)
        self.assertIn("invalid size_bytes", str(ctx.exception))

        # 5. apply_discard rejects size_bytes <= 0 in quarantine index
        self._write_lock_file(self.lease_id, self.conv_id)
        bad_idx = load_quarantine_index(self.index_path)
        bad_idx["items"][self.att_id_01]["size_bytes"] = 0
        self.index_path.write_text(json.dumps(bad_idx, indent=2), encoding="utf-8")
        dummy_req = build_apply_request(
            attachment_id=self.att_id_01,
            decision_id="a" * 64,
            index_entry_sha256=self.sample_idx_hash,
            quarantine_path=self.sample_entry["quarantine_path"],
            sha256=self.sample_entry["sha256"],
            size_bytes=100,
            run_id=self.sample_entry["run_id"],
        )
        dummy_rcpt = self._make_apply_receipt(dummy_req)
        with self.assertRaises((DispositionSchemaError, DispositionApplyError, AttachmentIndexSchemaError)):
            apply_discard(
                attachment_id=self.att_id_01,
                apply_receipt=dummy_rcpt,
                workspace_root=self.ws_root,
                lease_id=self.lease_id,
                conversation_id=self.conv_id,
            )


if __name__ == "__main__":
    unittest.main()
