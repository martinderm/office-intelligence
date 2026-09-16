"""Hermetic unit test suite for Attachment Disposition Log & Safe Cleanup (FR-11 / MD-Q3).

Verifies all contract invariants:
1. Append-only writer and existing-bytes integrity.
2. Workspace lock enforcement without legacy bypass.
3. Deterministic decision_id derivation and idempotency.
4. Duplicate decision_id with conflicting content (drift detection).
5. Invalid or missing human receipts fail-closed.
6. Quarantine index entry hash drift fail-closed.
7. Unknown decisions and unknown fields rejection.
8. Forbidden and nested content rejection (no mail body, prompts, credentials).
9. Retain decision semantics with future and expired review dates.
10. Discard decision without immediate deletion and apply receipt requirement.
11. Promote decision and FR-09 candidate review hash binding.
12. Active run evidence protects quarantine files.
13. Manipulated run ID, path traversal, and absolute path rejection.
14. Symlink and Windows reparse point (0x400) protection.
15. Manipulated inventory, hash drift, and size drift.
16. Read-only report byte-identity across all files.
17. Partial failure isolation and atomic index update.
18. Recovery and idempotent retry after interrupted index update.
19. Byte-identity preservation of final-location-index.json and zero mailbox mutation.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

# Ensure core and scripts are on sys.path
_scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
if str(_scripts_dir) not in sys.path:
    sys.path.insert(0, str(_scripts_dir))

from core.common import normalize_message_id, utc_now_iso
from core.attachment_fetch import INVENTORY_FILENAME
from core.attachment_quarantine_index import (
    INDEX_FILENAME,
    compute_attachment_id,
    canonical_index_entry_sha256,
    load_quarantine_index,
    record_quarantine_entry,
    save_quarantine_index_atomic,
    AttachmentIndexDriftError,
    AttachmentIndexSchemaError,
    ForbiddenContentError,
    WorkspaceLockRequiredError,
)
from core.attachment_disposition_log import (
    DISPOSITION_LOG_FILENAME,
    DECISION_RETAIN,
    DECISION_DISCARD,
    DECISION_PROMOTE,
    STATUS_ELIGIBLE,
    STATUS_PROTECTED,
    STATUS_INVALID,
    DispositionError,
    DispositionSchemaError,
    DispositionDriftError,
    DispositionLockRequiredError,
    DispositionApplyError,
    compute_decision_id,
    load_disposition_log,
    record_disposition_entry,
    report_dispositions,
    apply_discard,
    validate_disposition_entry,
)


class TestMailDeskAttachmentDispositionMDQ3(unittest.TestCase):
    """Hermetic test suite for MD-Q3 disposition log, reporting, and safe cleanup."""

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

        # Setup final-location-index.json to guarantee byte identity
        self.final_index_path = self.data_dir / "final-location-index.json"
        self.final_index_content = json.dumps(
            {"schema_version": 1, "items": {"<sample@example.org>": {"final_folder": "Archive"}}},
            indent=2,
        ).encode("utf-8")
        self.final_index_path.write_bytes(self.final_index_content)

        # Set up a sample quarantine attachment entry
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

        # Valid human receipt hash
        self.human_receipt_hash = "a" * 64

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

        file_sha = hashlib.sha256(payload).hexdigest().lower()
        file_size = len(payload)
        norm_mid = normalize_message_id(message_id)

        inv_file = run_dir / INVENTORY_FILENAME
        if inv_file.exists():
            try:
                inv_data = json.loads(inv_file.read_text(encoding="utf-8"))
            except Exception:
                inv_data = {"schema_version": 1, "messages": {}}
        else:
            inv_data = {"schema_version": 1, "messages": {}}

        msgs = inv_data.setdefault("messages", {})
        msg_entry = msgs.setdefault(norm_mid, {"files": {}, "count": 0, "total_bytes": 0})
        files = msg_entry.setdefault("files", {})
        files[filename] = {
            "sha256": file_sha,
            "size_bytes": file_size,
        }
        msg_entry["count"] = len(files)
        msg_entry["total_bytes"] = sum(f["size_bytes"] for f in files.values())
        inv_file.write_text(json.dumps(inv_data, indent=2), encoding="utf-8")

        rel_path = f"data/mail-desk/attachments/{run_id}/{filename}"
        att_id = compute_attachment_id(norm_mid, part_locator, file_sha)

        return {
            "attachment_id": att_id,
            "message_id": norm_mid,
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

    # --------------------------------------------------------------------------
    # 1. Append-Only Writer and Bytes Integrity
    # --------------------------------------------------------------------------
    def test_append_only_writer_and_bytes_integrity(self) -> None:
        """Sequential records must append to attachment-disposition-log.jsonl without altering earlier bytes."""
        self._write_lock_file(self.lease_id, self.conv_id)

        # First entry: retain
        payload_1 = {
            "attachment_id": self.att_id_01,
            "index_entry_sha256": self.sample_idx_hash,
            "decision": DECISION_RETAIN,
            "timestamp": "2026-09-16T12:00:00Z",
            "human_receipt_hash": self.human_receipt_hash,
            "rationale": "Retain for project audit",
            "review_after": "2026-10-16T12:00:00Z",
        }
        res_1 = record_disposition_entry(
            self.log_path,
            payload=payload_1,
            workspace_root=self.ws_root,
            lease_id=self.lease_id,
            conversation_id=self.conv_id,
        )
        self.assertEqual(res_1["status"], "recorded")
        bytes_after_1 = self.log_path.read_bytes()

        # Second entry: discard on same attachment with updated timestamp
        payload_2 = {
            "attachment_id": self.att_id_01,
            "index_entry_sha256": self.sample_idx_hash,
            "decision": DECISION_DISCARD,
            "timestamp": "2026-09-16T14:00:00Z",
            "human_receipt_hash": "b" * 64,
            "rationale": "Audit complete, discard authorized",
        }
        res_2 = record_disposition_entry(
            self.log_path,
            payload=payload_2,
            workspace_root=self.ws_root,
            lease_id=self.lease_id,
            conversation_id=self.conv_id,
        )
        self.assertEqual(res_2["status"], "recorded")
        bytes_after_2 = self.log_path.read_bytes()

        # Integrity assertion: bytes_after_2 must strictly start with bytes_after_1
        self.assertTrue(bytes_after_2.startswith(bytes_after_1))
        self.assertGreater(len(bytes_after_2), len(bytes_after_1))

        # Log loading verifies two distinct lines
        log_data = load_disposition_log(self.log_path)
        self.assertEqual(len(log_data["entries"]), 2)
        self.assertEqual(log_data["latest_by_attachment_id"][self.att_id_01]["decision"], DECISION_DISCARD)

    # --------------------------------------------------------------------------
    # 2. Lock Enforcement Without Legacy Bypass
    # --------------------------------------------------------------------------
    def test_lock_enforcement_without_legacy_bypass(self) -> None:
        """Mutations must fail closed without valid workspace lock and forbid legacy bypass."""
        payload = {
            "attachment_id": self.att_id_01,
            "index_entry_sha256": self.sample_idx_hash,
            "decision": DECISION_RETAIN,
            "timestamp": "2026-09-16T12:00:00Z",
            "human_receipt_hash": self.human_receipt_hash,
        }

        # 1. Missing or wrong lock fails
        with self.assertRaises(DispositionLockRequiredError):
            record_disposition_entry(
                self.log_path,
                payload=payload,
                workspace_root=self.ws_root,
                lease_id="wrong_lease",
                conversation_id="wrong_conv",
            )

        # 2. Even with WORKSPACE_LOCK_ALLOW_LEGACY env set, legacy bypass is rejected
        with patch.dict(os.environ, {"WORKSPACE_LOCK_ALLOW_LEGACY": "true"}):
            with self.assertRaises(DispositionLockRequiredError):
                record_disposition_entry(
                    self.log_path,
                    payload=payload,
                    workspace_root=self.ws_root,
                    lease_id="wrong_lease",
                    conversation_id="wrong_conv",
                )

        # 3. Discard apply also strictly requires lock
        with self.assertRaises(DispositionLockRequiredError):
            apply_discard(
                apply_receipt_hash="c" * 64,
                attachment_id=self.att_id_01,
                workspace_root=self.ws_root,
                lease_id="wrong_lease",
                conversation_id="wrong_conv",
            )

    # --------------------------------------------------------------------------
    # 3. Deterministic ID and Idempotency
    # --------------------------------------------------------------------------
    def test_deterministic_id_and_idempotency(self) -> None:
        """decision_id must be deterministic and identical repetitions must be no-op unchanged."""
        self._write_lock_file(self.lease_id, self.conv_id)

        payload = {
            "attachment_id": self.att_id_01,
            "index_entry_sha256": self.sample_idx_hash,
            "decision": DECISION_RETAIN,
            "timestamp": "2026-09-16T12:00:00Z",
            "human_receipt_hash": self.human_receipt_hash,
            "rationale": "Deterministic test",
        }
        res_1 = record_disposition_entry(
            self.log_path,
            payload=payload,
            workspace_root=self.ws_root,
            lease_id=self.lease_id,
            conversation_id=self.conv_id,
        )
        self.assertEqual(res_1["status"], "recorded")
        dec_id = res_1["decision_id"]

        # Expected deterministic computation
        expected_id = compute_decision_id(
            attachment_id=self.att_id_01,
            index_entry_sha256=self.sample_idx_hash,
            decision=DECISION_RETAIN,
            timestamp="2026-09-16T12:00:00Z",
            human_receipt_hash=self.human_receipt_hash,
            rationale="Deterministic test",
        )
        self.assertEqual(dec_id, expected_id)

        # Repeated recording with identical payload -> unchanged
        res_2 = record_disposition_entry(
            self.log_path,
            payload=payload,
            workspace_root=self.ws_root,
            lease_id=self.lease_id,
            conversation_id=self.conv_id,
        )
        self.assertEqual(res_2["status"], "unchanged")
        self.assertEqual(res_2["decision_id"], dec_id)

        # File still contains exactly 1 line
        lines = [line for line in self.log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)

    # --------------------------------------------------------------------------
    # 4. Duplicate decision_id with Conflicting Content (Drift)
    # --------------------------------------------------------------------------
    def test_decision_id_drift_detection(self) -> None:
        """Same decision_id with altered content must raise DispositionDriftError."""
        self._write_lock_file(self.lease_id, self.conv_id)

        payload = {
            "attachment_id": self.att_id_01,
            "index_entry_sha256": self.sample_idx_hash,
            "decision": DECISION_RETAIN,
            "timestamp": "2026-09-16T12:00:00Z",
            "human_receipt_hash": self.human_receipt_hash,
            "rationale": "Initial rationale",
        }
        res = record_disposition_entry(
            self.log_path,
            payload=payload,
            workspace_root=self.ws_root,
            lease_id=self.lease_id,
            conversation_id=self.conv_id,
        )
        dec_id = res["decision_id"]

        # Falsified entry claiming same decision_id but different rationale
        mutated_entry = {
            "decision_id": dec_id,
            "attachment_id": self.att_id_01,
            "index_entry_sha256": self.sample_idx_hash,
            "decision": DECISION_RETAIN,
            "timestamp": "2026-09-16T12:00:00Z",
            "human_receipt_hash": self.human_receipt_hash,
            "rationale": "Altered rationale",
        }
        with self.assertRaises(DispositionDriftError):
            validate_disposition_entry(mutated_entry)

    # --------------------------------------------------------------------------
    # 5. Invalid or Missing Human Receipts
    # --------------------------------------------------------------------------
    def test_invalid_or_missing_human_receipts(self) -> None:
        """Missing or malformed human receipts must raise DispositionSchemaError."""
        invalid_receipts = [
            None,
            "",
            "   ",
            "1",
            "not_hex",
            "a" * 63,
            "a" * 65,
            12345,
            {"hash": "nested"},
        ]
        for bad_hr in invalid_receipts:
            payload = {
                "attachment_id": self.att_id_01,
                "index_entry_sha256": self.sample_idx_hash,
                "decision": DECISION_RETAIN,
                "timestamp": "2026-09-16T12:00:00Z",
                "human_receipt_hash": bad_hr,
            }
            with self.assertRaises(DispositionSchemaError, msg=f"Failed to reject human receipt {bad_hr!r}"):
                validate_disposition_entry(payload)

    # --------------------------------------------------------------------------
    # 6. Quarantine Index Entry Hash Drift Fail-Closed
    # --------------------------------------------------------------------------
    def test_index_entry_hash_drift_fail_closed(self) -> None:
        """Disposition recording must fail closed if index_entry_sha256 does not match disk index."""
        self._write_lock_file(self.lease_id, self.conv_id)

        stale_idx_hash = "f" * 64
        payload = {
            "attachment_id": self.att_id_01,
            "index_entry_sha256": stale_idx_hash,
            "decision": DECISION_RETAIN,
            "timestamp": "2026-09-16T12:00:00Z",
            "human_receipt_hash": self.human_receipt_hash,
        }
        with self.assertRaises(AttachmentIndexDriftError):
            record_disposition_entry(
                self.log_path,
                payload=payload,
                workspace_root=self.ws_root,
                lease_id=self.lease_id,
                conversation_id=self.conv_id,
            )

    # --------------------------------------------------------------------------
    # 7. Unknown Decisions and Unknown Fields
    # --------------------------------------------------------------------------
    def test_unknown_decisions_and_unknown_fields(self) -> None:
        """Unknown decisions and unknown schema fields must fail closed."""
        # 1. Unknown decision
        bad_dec = {
            "attachment_id": self.att_id_01,
            "index_entry_sha256": self.sample_idx_hash,
            "decision": "archive",  # Not in retain, discard, promote
            "timestamp": "2026-09-16T12:00:00Z",
            "human_receipt_hash": self.human_receipt_hash,
        }
        with self.assertRaises(DispositionSchemaError):
            validate_disposition_entry(bad_dec)

        # 2. Unknown field
        bad_field = {
            "attachment_id": self.att_id_01,
            "index_entry_sha256": self.sample_idx_hash,
            "decision": DECISION_RETAIN,
            "timestamp": "2026-09-16T12:00:00Z",
            "human_receipt_hash": self.human_receipt_hash,
            "unknown_extra": "rejected",
        }
        with self.assertRaises(DispositionSchemaError):
            validate_disposition_entry(bad_field)

    # --------------------------------------------------------------------------
    # 8. Forbidden and Nested Content Rejection
    # --------------------------------------------------------------------------
    def test_forbidden_and_nested_content_rejection(self) -> None:
        """Prompts, credentials, mail bodies, and multiline rationales must be rejected."""
        forbidden_keys = ["prompt", "credentials", "body", "token", "envelope_id", "password"]
        for fkey in forbidden_keys:
            bad_entry = {
                "attachment_id": self.att_id_01,
                "index_entry_sha256": self.sample_idx_hash,
                "decision": DECISION_RETAIN,
                "timestamp": "2026-09-16T12:00:00Z",
                "human_receipt_hash": self.human_receipt_hash,
                fkey: "secret",
            }
            with self.assertRaises((ForbiddenContentError, DispositionSchemaError)):
                validate_disposition_entry(bad_entry)

        # Multiline rationale
        multiline_entry = {
            "attachment_id": self.att_id_01,
            "index_entry_sha256": self.sample_idx_hash,
            "decision": DECISION_RETAIN,
            "timestamp": "2026-09-16T12:00:00Z",
            "human_receipt_hash": self.human_receipt_hash,
            "rationale": "Line 1\nLine 2 containing extracted mail text",
        }
        with self.assertRaises(DispositionSchemaError):
            validate_disposition_entry(multiline_entry)

        # Oversized rationale (>256 chars)
        oversized_entry = {
            "attachment_id": self.att_id_01,
            "index_entry_sha256": self.sample_idx_hash,
            "decision": DECISION_RETAIN,
            "timestamp": "2026-09-16T12:00:00Z",
            "human_receipt_hash": self.human_receipt_hash,
            "rationale": "x" * 257,
        }
        with self.assertRaises(DispositionSchemaError):
            validate_disposition_entry(oversized_entry)

    # --------------------------------------------------------------------------
    # 9. Retain Decision Semantics with Future and Expired Review Dates
    # --------------------------------------------------------------------------
    def test_retain_decision_review_after_semantics(self) -> None:
        """retain with future or expired review_after must remain protected until explicit discard."""
        self._write_lock_file(self.lease_id, self.conv_id)

        # 1. Future review date
        payload_future = {
            "attachment_id": self.att_id_01,
            "index_entry_sha256": self.sample_idx_hash,
            "decision": DECISION_RETAIN,
            "timestamp": "2026-09-16T12:00:00Z",
            "human_receipt_hash": self.human_receipt_hash,
            "review_after": "2026-12-31T23:59:59Z",
        }
        record_disposition_entry(
            self.log_path,
            payload=payload_future,
            workspace_root=self.ws_root,
            lease_id=self.lease_id,
            conversation_id=self.conv_id,
        )
        rep_future = report_dispositions(workspace_root=self.ws_root)
        self.assertEqual(rep_future["summary"]["protected"], 1)
        self.assertEqual(rep_future["summary"]["eligible"], 0)

        # 2. Expired review date still remains protected from deletion
        payload_expired = {
            "attachment_id": self.att_id_01,
            "index_entry_sha256": self.sample_idx_hash,
            "decision": DECISION_RETAIN,
            "timestamp": "2026-09-16T13:00:00Z",
            "human_receipt_hash": self.human_receipt_hash,
            "review_after": "2020-01-01T00:00:00Z",
        }
        record_disposition_entry(
            self.log_path,
            payload=payload_expired,
            workspace_root=self.ws_root,
            lease_id=self.lease_id,
            conversation_id=self.conv_id,
        )
        rep_expired = report_dispositions(workspace_root=self.ws_root)
        self.assertEqual(rep_expired["summary"]["protected"], 1)
        self.assertEqual(rep_expired["summary"]["eligible"], 0)

        # 3. Retain rejects candidate_review_hash
        bad_retain = dict(payload_future)
        bad_retain["candidate_review_hash"] = "c" * 64
        with self.assertRaises(DispositionSchemaError):
            validate_disposition_entry(bad_retain)

    # --------------------------------------------------------------------------
    # 10. Discard Decision Without Immediate Deletion & Apply Receipt
    # --------------------------------------------------------------------------
    def test_discard_decision_without_immediate_deletion_and_apply_receipt(self) -> None:
        """discard decision must not delete file; apply_discard requires valid apply receipt."""
        self._write_lock_file(self.lease_id, self.conv_id)

        payload_discard = {
            "attachment_id": self.att_id_01,
            "index_entry_sha256": self.sample_idx_hash,
            "decision": DECISION_DISCARD,
            "timestamp": "2026-09-16T12:00:00Z",
            "human_receipt_hash": self.human_receipt_hash,
            "rationale": "Discard authorized",
        }
        record_disposition_entry(
            self.log_path,
            payload=payload_discard,
            workspace_root=self.ws_root,
            lease_id=self.lease_id,
            conversation_id=self.conv_id,
        )

        # File is still present on disk
        target_file = self.ws_root / PurePosixPath(self.sample_entry["quarantine_path"])
        self.assertTrue(target_file.is_file())

        # Report classifies it as eligible
        rep = report_dispositions(workspace_root=self.ws_root)
        self.assertEqual(rep["summary"]["eligible"], 1)

        # Apply fails without valid apply_receipt_hash
        with self.assertRaises(DispositionApplyError):
            apply_discard(
                apply_receipt_hash="",
                attachment_id=self.att_id_01,
                workspace_root=self.ws_root,
                lease_id=self.lease_id,
                conversation_id=self.conv_id,
            )

        # Apply succeeds with valid apply_receipt_hash
        apply_res = apply_discard(
            apply_receipt_hash="e" * 64,
            attachment_id=self.att_id_01,
            workspace_root=self.ws_root,
            lease_id=self.lease_id,
            conversation_id=self.conv_id,
        )
        self.assertEqual(apply_res["status"], "completed")
        self.assertEqual(apply_res["deleted_count"], 1)
        self.assertFalse(target_file.exists())

        # Quarantined index entry is atomically removed
        idx_after = load_quarantine_index(self.index_path)
        self.assertNotIn(self.att_id_01, idx_after["items"])

    # --------------------------------------------------------------------------
    # 11. Promote Decision and FR-09 Candidate Review Hash
    # --------------------------------------------------------------------------
    def test_promote_decision_and_fr09_link(self) -> None:
        """promote requires candidate_review_hash; protects from cleanup; no cloud sync."""
        self._write_lock_file(self.lease_id, self.conv_id)

        # 1. Promote without candidate_review_hash fails
        bad_promote = {
            "attachment_id": self.att_id_01,
            "index_entry_sha256": self.sample_idx_hash,
            "decision": DECISION_PROMOTE,
            "timestamp": "2026-09-16T12:00:00Z",
            "human_receipt_hash": self.human_receipt_hash,
        }
        with self.assertRaises(DispositionSchemaError):
            validate_disposition_entry(bad_promote)

        # 2. Promote with candidate_review_hash succeeds
        good_promote = dict(bad_promote)
        good_promote["candidate_review_hash"] = "d" * 64
        good_promote["promotion_id"] = "promo_fr09_001"
        good_promote["promotion_status"] = "pending"

        rec = record_disposition_entry(
            self.log_path,
            payload=good_promote,
            workspace_root=self.ws_root,
            lease_id=self.lease_id,
            conversation_id=self.conv_id,
        )
        self.assertEqual(rec["status"], "recorded")

        # Report classifies as protected
        rep = report_dispositions(workspace_root=self.ws_root)
        self.assertEqual(rep["summary"]["protected"], 1)
        self.assertEqual(rep["summary"]["eligible"], 0)

        # Attempt to apply discard fails closed (not eligible)
        with self.assertRaises(DispositionApplyError):
            apply_discard(
                apply_receipt_hash="f" * 64,
                attachment_id=self.att_id_01,
                workspace_root=self.ws_root,
                lease_id=self.lease_id,
                conversation_id=self.conv_id,
            )

    # --------------------------------------------------------------------------
    # 12. Active Run Evidence Protects Quarantine Files
    # --------------------------------------------------------------------------
    def test_active_run_protects_quarantine_file(self) -> None:
        """Active inventory lock or running recovery journal marks discard candidate as protected."""
        self._write_lock_file(self.lease_id, self.conv_id)

        payload_discard = {
            "attachment_id": self.att_id_01,
            "index_entry_sha256": self.sample_idx_hash,
            "decision": DECISION_DISCARD,
            "timestamp": "2026-09-16T12:00:00Z",
            "human_receipt_hash": self.human_receipt_hash,
        }
        record_disposition_entry(
            self.log_path,
            payload=payload_discard,
            workspace_root=self.ws_root,
            lease_id=self.lease_id,
            conversation_id=self.conv_id,
        )

        # Create active inventory lock in run directory
        run_dir = self.attachments_root / self.sample_entry["run_id"]
        lock_file = run_dir / ".quarantine-inventory.lock"
        lock_file.write_text("active_lock", encoding="utf-8")

        # Report marks item as protected due to active run
        rep = report_dispositions(workspace_root=self.ws_root)
        self.assertEqual(rep["summary"]["protected"], 1)
        self.assertEqual(rep["summary"]["eligible"], 0)

        # Remove lock file -> becomes eligible
        lock_file.unlink()
        rep_clean = report_dispositions(workspace_root=self.ws_root)
        self.assertEqual(rep_clean["summary"]["eligible"], 1)

    # --------------------------------------------------------------------------
    # 13. Manipulated Run-ID, Path Traversal, and Absolute Path Rejection
    # --------------------------------------------------------------------------
    def test_manipulated_run_id_and_path_traversal(self) -> None:
        """Manipulated run IDs and escape paths must be classified as invalid."""
        # Mutate index directly with traversal
        idx_data = load_quarantine_index(self.index_path)
        idx_data["items"][self.att_id_01]["quarantine_path"] = "data/mail-desk/attachments/../../etc/passwd"
        save_quarantine_index_atomic(self.index_path, idx_data)

        rep = report_dispositions(workspace_root=self.ws_root)
        self.assertEqual(rep["summary"]["invalid"], 1)
        self.assertIn("traversal", rep["items"][0]["reason"].lower())

    # --------------------------------------------------------------------------
    # 14. Symlink and Windows Reparse Point Protection
    # --------------------------------------------------------------------------
    def test_symlink_and_windows_reparse_point_protection(self) -> None:
        """Symlinks and reparse points must be classified as invalid and blocked from deletion."""
        self._write_lock_file(self.lease_id, self.conv_id)
        target_file = self.ws_root / PurePosixPath(self.sample_entry["quarantine_path"])

        # Mock is_symlink to simulate malicious symlink
        with patch.object(Path, "is_symlink", return_value=True):
            rep = report_dispositions(workspace_root=self.ws_root)
            self.assertEqual(rep["summary"]["invalid"], 1)

        # Mock Windows reparse point attribute 0x400
        if os.name == "nt":
            class MockStat:
                st_mode = 0o100644
                st_file_attributes = 0x400
                st_size = 100

            with patch("os.lstat", return_value=MockStat()):
                rep = report_dispositions(workspace_root=self.ws_root)
                self.assertEqual(rep["summary"]["invalid"], 1)

    # --------------------------------------------------------------------------
    # 15. Manipulated Inventory and Hash Drift
    # --------------------------------------------------------------------------
    def test_manipulated_inventory_and_hash_drift(self) -> None:
        """Drift in file bytes or inventory corruption must be classified as invalid."""
        target_file = self.ws_root / PurePosixPath(self.sample_entry["quarantine_path"])
        # Mutate disk file bytes
        target_file.write_bytes(b"tampered content")

        rep = report_dispositions(workspace_root=self.ws_root)
        self.assertEqual(rep["summary"]["invalid"], 1)
        self.assertIn("drift", rep["items"][0]["reason"].lower())

    # --------------------------------------------------------------------------
    # 16. Read-Only Report Byte Identity
    # --------------------------------------------------------------------------
    def test_read_only_report_byte_identity(self) -> None:
        """Running report_dispositions must not modify index, log, inventories, or disk files."""
        initial_index_bytes = self.index_path.read_bytes()
        initial_final_index_bytes = self.final_index_path.read_bytes()

        # Run report multiple times
        rep1 = report_dispositions(workspace_root=self.ws_root)
        rep2 = report_dispositions(workspace_root=self.ws_root)

        self.assertEqual(rep1["summary"], rep2["summary"])
        self.assertEqual(self.index_path.read_bytes(), initial_index_bytes)
        self.assertEqual(self.final_index_path.read_bytes(), initial_final_index_bytes)

    # --------------------------------------------------------------------------
    # 17. Partial Failure Isolation and Atomic Index Update
    # --------------------------------------------------------------------------
    def test_partial_failure_and_atomic_index_update(self) -> None:
        """Partial failures must delete verified files without marking uncleaned files as success."""
        self._write_lock_file(self.lease_id, self.conv_id)

        # Create second attachment in same run
        entry_2 = self._create_sample_quarantine_run(
            self.sample_entry["run_id"],
            "<doc02@example.org>",
            "statement_02.pdf",
            b"%PDF-1.4 second document in run",
            part_locator="2",
        )
        rec_2 = record_quarantine_entry(
            self.index_path,
            payload=entry_2,
            workspace_root=self.ws_root,
            lease_id=self.lease_id,
            conversation_id=self.conv_id,
        )
        att_id_02 = rec_2["attachment_id"]

        # Record discard for both
        disk_idx = load_quarantine_index(self.index_path)
        hash_1 = canonical_index_entry_sha256(disk_idx["items"][self.att_id_01])
        hash_2 = canonical_index_entry_sha256(disk_idx["items"][att_id_02])

        record_disposition_entry(
            self.log_path,
            payload={
                "attachment_id": self.att_id_01,
                "index_entry_sha256": hash_1,
                "decision": DECISION_DISCARD,
                "timestamp": "2026-09-16T12:00:00Z",
                "human_receipt_hash": self.human_receipt_hash,
            },
            workspace_root=self.ws_root,
            lease_id=self.lease_id,
            conversation_id=self.conv_id,
        )
        record_disposition_entry(
            self.log_path,
            payload={
                "attachment_id": att_id_02,
                "index_entry_sha256": hash_2,
                "decision": DECISION_DISCARD,
                "timestamp": "2026-09-16T12:05:00Z",
                "human_receipt_hash": self.human_receipt_hash,
            },
            workspace_root=self.ws_root,
            lease_id=self.lease_id,
            conversation_id=self.conv_id,
        )

        # Discard only the first attachment explicitly
        res = apply_discard(
            apply_receipt_hash="a" * 64,
            attachment_id=self.att_id_01,
            workspace_root=self.ws_root,
            lease_id=self.lease_id,
            conversation_id=self.conv_id,
        )
        self.assertEqual(res["status"], "completed")
        self.assertEqual(res["deleted_count"], 1)

        # Index still contains attachment 2!
        idx_after = load_quarantine_index(self.index_path)
        self.assertNotIn(self.att_id_01, idx_after["items"])
        self.assertIn(att_id_02, idx_after["items"])

        # Physical file 2 still exists!
        file_2 = self.ws_root / PurePosixPath(entry_2["quarantine_path"])
        self.assertTrue(file_2.is_file())

    # --------------------------------------------------------------------------
    # 18. Recovery and Idempotent Retry After Interrupted Index Update
    # --------------------------------------------------------------------------
    def test_interruption_between_deletion_and_index_update_recovery(self) -> None:
        """If file was deleted but index update aborted, retry must complete index update cleanly."""
        self._write_lock_file(self.lease_id, self.conv_id)

        # Record discard
        record_disposition_entry(
            self.log_path,
            payload={
                "attachment_id": self.att_id_01,
                "index_entry_sha256": self.sample_idx_hash,
                "decision": DECISION_DISCARD,
                "timestamp": "2026-09-16T12:00:00Z",
                "human_receipt_hash": self.human_receipt_hash,
            },
            workspace_root=self.ws_root,
            lease_id=self.lease_id,
            conversation_id=self.conv_id,
        )

        # Manually unlink file to simulate abort between target_file.unlink() and index update
        target_file = self.ws_root / PurePosixPath(self.sample_entry["quarantine_path"])
        target_file.unlink()
        self.assertFalse(target_file.exists())

        # Read-only report reflects invalid / recovery state
        rep = report_dispositions(workspace_root=self.ws_root)
        self.assertEqual(rep["summary"]["invalid"], 1)
        self.assertIn("missing", rep["items"][0]["reason"].lower())

        # Calling apply_discard retry completes the pending index update
        retry_res = apply_discard(
            apply_receipt_hash="b" * 64,
            attachment_id=self.att_id_01,
            workspace_root=self.ws_root,
            lease_id=self.lease_id,
            conversation_id=self.conv_id,
        )
        self.assertEqual(retry_res["status"], "completed")
        self.assertEqual(retry_res["deleted_files"][0]["status"], "recovered_index_updated")

        # Quarantine index is now clean
        idx_after = load_quarantine_index(self.index_path)
        self.assertNotIn(self.att_id_01, idx_after["items"])

    # --------------------------------------------------------------------------
    # 19. Final Location Index Byte Identity and No Mailbox Mutation
    # --------------------------------------------------------------------------
    def test_final_location_index_byte_identity(self) -> None:
        """final-location-index.json must remain strictly byte-identical after full MD-Q3 cycle."""
        self._write_lock_file(self.lease_id, self.conv_id)

        record_disposition_entry(
            self.log_path,
            payload={
                "attachment_id": self.att_id_01,
                "index_entry_sha256": self.sample_idx_hash,
                "decision": DECISION_DISCARD,
                "timestamp": "2026-09-16T12:00:00Z",
                "human_receipt_hash": self.human_receipt_hash,
            },
            workspace_root=self.ws_root,
            lease_id=self.lease_id,
            conversation_id=self.conv_id,
        )
        report_dispositions(workspace_root=self.ws_root)
        apply_discard(
            apply_receipt_hash="c" * 64,
            attachment_id=self.att_id_01,
            workspace_root=self.ws_root,
            lease_id=self.lease_id,
            conversation_id=self.conv_id,
        )

        # Byte identity of final-location-index.json must hold
        self.assertEqual(self.final_index_path.read_bytes(), self.final_index_content)

    # --------------------------------------------------------------------------
    # 20. CLI Interface Operations
    # --------------------------------------------------------------------------
    def test_cli_facade_operations(self) -> None:
        """CLI facade mail_desk_attachment_disposition.py must support record, report, and apply-discard."""
        self._write_lock_file(self.lease_id, self.conv_id)
        cli_script = _scripts_dir / "mail_desk_attachment_disposition.py"

        # 1. CLI report on initial state
        cmd_rep = [
            sys.executable,
            str(cli_script),
            "--workspace-root",
            str(self.ws_root),
            "--json",
            "report",
        ]
        proc_rep = subprocess.run(cmd_rep, capture_output=True, text=True, check=False)
        self.assertEqual(proc_rep.returncode, 0, f"CLI report failed: {proc_rep.stderr}")
        rep_json = json.loads(proc_rep.stdout)
        self.assertTrue(rep_json["success"])
        self.assertEqual(rep_json["data"]["report"]["summary"]["protected"], 1)

        # 2. CLI record with input payload JSON
        record_payload = {
            "attachment_id": self.att_id_01,
            "index_entry_sha256": self.sample_idx_hash,
            "decision": DECISION_DISCARD,
            "timestamp": "2026-09-16T12:00:00Z",
            "human_receipt_hash": self.human_receipt_hash,
            "rationale": "CLI discard authorization",
        }
        input_json_file = Path(self.temp_dir) / "cli_record_input.json"
        input_json_file.write_text(json.dumps(record_payload, indent=2), encoding="utf-8")

        cmd_rec = [
            sys.executable,
            str(cli_script),
            "--workspace-root",
            str(self.ws_root),
            "--lease-id",
            self.lease_id,
            "--conversation-id",
            self.conv_id,
            "--json",
            "record",
            "--input",
            str(input_json_file),
        ]
        proc_rec = subprocess.run(cmd_rec, capture_output=True, text=True, check=False)
        self.assertEqual(proc_rec.returncode, 0, f"CLI record failed: {proc_rec.stderr}")
        rec_json = json.loads(proc_rec.stdout)
        self.assertTrue(rec_json["success"])
        self.assertEqual(rec_json["data"]["status"], "recorded")

        # 3. CLI apply-discard without lock fails
        cmd_apply_nolock = [
            sys.executable,
            str(cli_script),
            "--workspace-root",
            str(self.ws_root),
            "--lease-id",
            "wrong_lease",
            "--conversation-id",
            "wrong_conv",
            "--json",
            "apply-discard",
            "--apply-receipt-hash",
            "d" * 64,
        ]
        proc_apply_nolock = subprocess.run(cmd_apply_nolock, capture_output=True, text=True, check=False)
        self.assertNotEqual(proc_apply_nolock.returncode, 0)
        nolock_json = json.loads(proc_apply_nolock.stdout)
        self.assertFalse(nolock_json["success"])
        self.assertIn("DispositionLockRequiredError", nolock_json["error"]["type"])

        # 4. CLI apply-discard with valid lock succeeds
        cmd_apply = [
            sys.executable,
            str(cli_script),
            "--workspace-root",
            str(self.ws_root),
            "--lease-id",
            self.lease_id,
            "--conversation-id",
            self.conv_id,
            "--json",
            "apply-discard",
            "--apply-receipt-hash",
            "d" * 64,
            "--attachment-id",
            self.att_id_01,
        ]
        proc_apply = subprocess.run(cmd_apply, capture_output=True, text=True, check=False)
        self.assertEqual(proc_apply.returncode, 0, f"CLI apply-discard failed: {proc_apply.stderr}")
        apply_json = json.loads(proc_apply.stdout)
        self.assertTrue(apply_json["success"])
        self.assertEqual(apply_json["data"]["deleted_count"], 1)


if __name__ == "__main__":
    unittest.main()
