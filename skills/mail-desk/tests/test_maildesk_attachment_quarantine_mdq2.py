"""Hermetic TDD unit tests for FR-11 / MD-Q2 versioned quarantine index.

Tests:
- atomic writer and lock enforcement;
- deterministic ID and idempotency;
- path containment;
- symlink/reparse point protection;
- manipulated inventory;
- hash and size drift;
- missing file;
- unknown fields and status values;
- rejection of absolute paths and forbidden content;
- reconcile without mutation;
- two successfully analyzed PDFs from separate runs;
- byte-identical Final Location Index;
- CLI interface operations.
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
from unittest.mock import MagicMock, patch
from typing import Any

# Ensure core and scripts are on sys.path
_scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
if str(_scripts_dir) not in sys.path:
    sys.path.insert(0, str(_scripts_dir))

from core.common import normalize_message_id, utc_now_iso
from core.attachment_fetch import (
    INVENTORY_FILENAME,
    _QuarantineInventoryLock,
    check_quarantine_path_security,
    SymlinkEscapeError,
    QuarantineInventoryError,
)
from core.attachment_quarantine_index import (
    INDEX_FILENAME,
    SCHEMA_VERSION,
    LIFECYCLE_STATE_QUARANTINED,
    ANALYSIS_STATUS_COMPLETED,
    QuarantineIndexError,
    WorkspaceLockRequiredError,
    AttachmentIndexDriftError,
    AttachmentIndexSchemaError,
    ForbiddenContentError,
    PhysicalVerificationError,
    compute_attachment_id,
    resolve_quarantine_index_path,
    load_quarantine_index,
    save_quarantine_index_atomic,
    validate_quarantine_index_entry,
    record_quarantine_entry,
    reconcile_quarantine_index,
    lookup_quarantine_entry,
    get_quarantine_index_stats,
)


class TestMailDeskAttachmentQuarantineMDQ2(unittest.TestCase):
    """Hermetic test suite for MD-Q2 versioned quarantine index."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="test_mdq2_")
        self.ws_root = Path(self.temp_dir).resolve()
        self.data_dir = self.ws_root / "data" / "mail-desk"
        self.attachments_root = self.data_dir / "attachments"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.attachments_root.mkdir(parents=True, exist_ok=True)

        self.lease_id = "test_lease_mdq2_001"
        self.conv_id = "test_conv_mdq2_001"

        # Mock / set up workspace lock in .agents/session.lock
        self.agents_dir = self.ws_root / ".agents"
        self.agents_dir.mkdir(parents=True, exist_ok=True)
        self.lock_file = self.agents_dir / "session.lock"
        self._write_lock_file(lease_id=self.lease_id, conv_id=self.conv_id)

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

        inv_data = {
            "schema_version": 1,
            "messages": {
                norm_mid: {
                    "count": 1,
                    "total_bytes": file_size,
                    "files": {
                        filename: {
                            "sha256": file_sha,
                            "size_bytes": file_size,
                        }
                    },
                }
            },
        }
        inv_file = run_dir / INVENTORY_FILENAME
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
            "contract_version": "1",
            "lifecycle_state": "quarantined",
            "disposition_ref": None,
        }

    # --------------------------------------------------------------------------
    # 1. Atomic Writer & Lock Enforcement
    # --------------------------------------------------------------------------
    def test_atomic_writer_and_lock_enforcement(self) -> None:
        """Writer must enforce valid workspace lock and write atomically."""
        entry = self._create_sample_quarantine_run(
            "run_lock_01", "<msg_lock_01@example.org>", "doc1.pdf", b"%PDF-1.4 test lock"
        )
        index_path = self.data_dir / INDEX_FILENAME

        # 1. Fails without valid lock (wrong lease and wrong conv)
        with self.assertRaises(WorkspaceLockRequiredError):
            record_quarantine_entry(
                index_path,
                payload=entry,
                workspace_root=self.ws_root,
                lease_id="wrong_lease",
                conversation_id="wrong_conv",
            )

        # 2. Succeeds with valid lock
        res = record_quarantine_entry(
            index_path,
            payload=entry,
            workspace_root=self.ws_root,
            lease_id=self.lease_id,
            conversation_id=self.conv_id,
        )
        self.assertEqual(res["status"], "created")
        self.assertTrue(index_path.exists())

        # Verify no stray temporary files in data_dir
        tmp_files = list(self.data_dir.glob(f"{INDEX_FILENAME}.*.tmp"))
        self.assertEqual(len(tmp_files), 0)

        # Verify content
        index_data = load_quarantine_index(index_path)
        self.assertEqual(index_data["schema_version"], 1)
        self.assertIn(entry["attachment_id"], index_data["items"])

    # --------------------------------------------------------------------------
    # 2. Deterministic ID & Idempotency
    # --------------------------------------------------------------------------
    def test_deterministic_id_and_idempotency(self) -> None:
        """attachment_id must bind normalized mid, part_locator, and sha256; repetition is no-op."""
        mid = "  <MSG-UPPER@EXAMPLE.ORG>  "
        loc = "1.2"
        sha = "E3B0C44298FC1C149AFBF4C8996FB92427AE41E4649B934CA495991B7852B855"

        id1 = compute_attachment_id(mid, loc, sha)
        id2 = compute_attachment_id("<msg-upper@example.org>", "1.2", sha.lower())
        self.assertEqual(id1, id2)
        self.assertEqual(len(id1), 64)

        entry = self._create_sample_quarantine_run(
            "run_idem_01", mid, "doc_idem.pdf", b"%PDF-1.4 test idempotency", part_locator="1.2"
        )
        index_path = self.data_dir / INDEX_FILENAME

        # First recording
        res1 = record_quarantine_entry(
            index_path,
            payload=entry,
            workspace_root=self.ws_root,
            lease_id=self.lease_id,
            conversation_id=self.conv_id,
        )
        self.assertEqual(res1["status"], "created")

        # Second recording with identical data -> no-op
        res2 = record_quarantine_entry(
            index_path,
            payload=entry,
            workspace_root=self.ws_root,
            lease_id=self.lease_id,
            conversation_id=self.conv_id,
        )
        self.assertEqual(res2["status"], "unchanged")

        # Verify index has exactly 1 entry
        data = load_quarantine_index(index_path)
        self.assertEqual(len(data["items"]), 1)

    # --------------------------------------------------------------------------
    # 3. Path Containment
    # --------------------------------------------------------------------------
    def test_path_containment_enforcement(self) -> None:
        """quarantine_path must reside within data/mail-desk/attachments/<run_id>/."""
        entry = self._create_sample_quarantine_run(
            "run_contain_01", "<msg_contain@example.org>", "escaped.pdf", b"%PDF-1.4 containment"
        )
        index_path = self.data_dir / INDEX_FILENAME

        # Path escaping via ..
        bad_entry = dict(entry)
        bad_entry["quarantine_path"] = "data/mail-desk/attachments/../../escaped.pdf"
        with self.assertRaises(QuarantineIndexError):
            record_quarantine_entry(
                index_path,
                payload=bad_entry,
                workspace_root=self.ws_root,
                lease_id=self.lease_id,
                conversation_id=self.conv_id,
            )

        # Path in different directory
        bad_entry2 = dict(entry)
        bad_entry2["quarantine_path"] = "memory/evidence/escaped.pdf"
        with self.assertRaises(QuarantineIndexError):
            record_quarantine_entry(
                index_path,
                payload=bad_entry2,
                workspace_root=self.ws_root,
                lease_id=self.lease_id,
                conversation_id=self.conv_id,
            )

    # --------------------------------------------------------------------------
    # 4. Symlink / Reparse Point Protection
    # --------------------------------------------------------------------------
    def test_symlink_and_reparse_point_protection(self) -> None:
        """Symlinks and Windows Reparse Points in quarantine path or target file must fail closed."""
        entry = self._create_sample_quarantine_run(
            "run_sym_01", "<msg_sym@example.org>", "sym.pdf", b"%PDF-1.4 symlink test"
        )
        index_path = self.data_dir / INDEX_FILENAME

        # 1. Symlink detection via is_symlink
        with patch("pathlib.Path.is_symlink", return_value=True):
            with self.assertRaises((SymlinkEscapeError, PhysicalVerificationError)):
                record_quarantine_entry(
                    index_path,
                    payload=entry,
                    workspace_root=self.ws_root,
                    lease_id=self.lease_id,
                    conversation_id=self.conv_id,
                )

        # 2. Windows reparse point attribute (0x400)
        mock_stat = MagicMock()
        mock_stat.st_file_attributes = 0x400
        with patch("os.name", "nt"), patch("os.lstat", return_value=mock_stat):
            with self.assertRaises((SymlinkEscapeError, PhysicalVerificationError)):
                record_quarantine_entry(
                    index_path,
                    payload=entry,
                    workspace_root=self.ws_root,
                    lease_id=self.lease_id,
                    conversation_id=self.conv_id,
                )

    # --------------------------------------------------------------------------
    # 5. Manipulated Inventory
    # --------------------------------------------------------------------------
    def test_manipulated_inventory_fails_closed(self) -> None:
        """Manipulated or missing .quarantine-inventory.json must fail closed."""
        entry = self._create_sample_quarantine_run(
            "run_manip_01", "<msg_manip@example.org>", "tampered.pdf", b"%PDF-1.4 tampered"
        )
        index_path = self.data_dir / INDEX_FILENAME
        inv_file = self.attachments_root / "run_manip_01" / INVENTORY_FILENAME

        # 1. Missing inventory file
        inv_file.unlink()
        with self.assertRaises(QuarantineInventoryError):
            record_quarantine_entry(
                index_path,
                payload=entry,
                workspace_root=self.ws_root,
                lease_id=self.lease_id,
                conversation_id=self.conv_id,
            )

        # 2. Corrupted / mismatched SHA in inventory
        bad_inv = {
            "schema_version": 1,
            "messages": {
                entry["message_id"]: {
                    "count": 1,
                    "total_bytes": entry["size_bytes"],
                    "files": {
                        "tampered.pdf": {
                            "sha256": "0" * 64,  # mismatched SHA
                            "size_bytes": entry["size_bytes"],
                        }
                    },
                }
            },
        }
        inv_file.write_text(json.dumps(bad_inv), encoding="utf-8")
        with self.assertRaises(QuarantineInventoryError):
            record_quarantine_entry(
                index_path,
                payload=entry,
                workspace_root=self.ws_root,
                lease_id=self.lease_id,
                conversation_id=self.conv_id,
            )

    # --------------------------------------------------------------------------
    # 6. Hash & Size Drift
    # --------------------------------------------------------------------------
    def test_hash_and_size_drift_detection(self) -> None:
        """Drift in file size or hash between payload, inventory, and disk must fail closed."""
        entry = self._create_sample_quarantine_run(
            "run_drift_01", "<msg_drift@example.org>", "drift.pdf", b"%PDF-1.4 file content"
        )
        index_path = self.data_dir / INDEX_FILENAME

        # 1. Payload size mismatch against disk file
        bad_entry_size = dict(entry)
        bad_entry_size["size_bytes"] = entry["size_bytes"] + 100
        with self.assertRaises((AttachmentIndexDriftError, PhysicalVerificationError)):
            record_quarantine_entry(
                index_path,
                payload=bad_entry_size,
                workspace_root=self.ws_root,
                lease_id=self.lease_id,
                conversation_id=self.conv_id,
            )

        # 2. Payload SHA mismatch against disk file
        bad_entry_sha = dict(entry)
        bad_entry_sha["sha256"] = "f" * 64
        with self.assertRaises((AttachmentIndexDriftError, PhysicalVerificationError)):
            record_quarantine_entry(
                index_path,
                payload=bad_entry_sha,
                workspace_root=self.ws_root,
                lease_id=self.lease_id,
                conversation_id=self.conv_id,
            )

        # 3. Successful first recording
        record_quarantine_entry(
            index_path,
            payload=entry,
            workspace_root=self.ws_root,
            lease_id=self.lease_id,
            conversation_id=self.conv_id,
        )

        # 4. Attempt to update existing entry with different hash or path -> Drift error!
        drifted_existing = dict(entry)
        drifted_existing["sha256"] = "1" * 64
        with self.assertRaises(AttachmentIndexDriftError):
            record_quarantine_entry(
                index_path,
                payload=drifted_existing,
                workspace_root=self.ws_root,
                lease_id=self.lease_id,
                conversation_id=self.conv_id,
            )

    # --------------------------------------------------------------------------
    # 7. Missing File
    # --------------------------------------------------------------------------
    def test_missing_file_fails_closed(self) -> None:
        """If quarantine file does not exist on disk, fail closed."""
        entry = self._create_sample_quarantine_run(
            "run_missing_01", "<msg_miss@example.org>", "missing.pdf", b"%PDF-1.4 missing"
        )
        index_path = self.data_dir / INDEX_FILENAME

        # Delete the file
        target_file = self.ws_root / entry["quarantine_path"]
        target_file.unlink()

        with self.assertRaises(FileNotFoundError):
            record_quarantine_entry(
                index_path,
                payload=entry,
                workspace_root=self.ws_root,
                lease_id=self.lease_id,
                conversation_id=self.conv_id,
            )

    # --------------------------------------------------------------------------
    # 8. Unknown Fields & Status Values
    # --------------------------------------------------------------------------
    def test_unknown_fields_and_invalid_status_rejected(self) -> None:
        """Only allowed fields and valid status values are permitted."""
        entry = self._create_sample_quarantine_run(
            "run_fields_01", "<msg_fields@example.org>", "valid.pdf", b"%PDF-1.4 fields"
        )
        index_path = self.data_dir / INDEX_FILENAME

        # 1. Unknown field
        bad_fields = dict(entry)
        bad_fields["unknown_custom_key"] = "forbidden"
        with self.assertRaises(AttachmentIndexSchemaError):
            record_quarantine_entry(
                index_path,
                payload=bad_fields,
                workspace_root=self.ws_root,
                lease_id=self.lease_id,
                conversation_id=self.conv_id,
            )

        # 2. Invalid analysis_status (must be "completed")
        bad_status = dict(entry)
        bad_status["analysis_status"] = "in_progress"
        with self.assertRaises(AttachmentIndexSchemaError):
            record_quarantine_entry(
                index_path,
                payload=bad_status,
                workspace_root=self.ws_root,
                lease_id=self.lease_id,
                conversation_id=self.conv_id,
            )

        # 3. Invalid lifecycle_state (must be "quarantined")
        bad_lifecycle = dict(entry)
        bad_lifecycle["lifecycle_state"] = "promoted"
        with self.assertRaises(AttachmentIndexSchemaError):
            record_quarantine_entry(
                index_path,
                payload=bad_lifecycle,
                workspace_root=self.ws_root,
                lease_id=self.lease_id,
                conversation_id=self.conv_id,
            )

    # --------------------------------------------------------------------------
    # 9. Rejection of Absolute Paths & Forbidden Content
    # --------------------------------------------------------------------------
    def test_rejection_of_absolute_paths_and_forbidden_content(self) -> None:
        """Absolute paths, mail bodies, extracted texts, and credentials must be rejected."""
        entry = self._create_sample_quarantine_run(
            "run_forbid_01", "<msg_forbid@example.org>", "doc.pdf", b"%PDF-1.4 forbid"
        )
        index_path = self.data_dir / INDEX_FILENAME

        # 1. Absolute quarantine path rejected
        bad_abs = dict(entry)
        bad_abs["quarantine_path"] = str((self.ws_root / entry["quarantine_path"]).resolve())
        with self.assertRaises(QuarantineIndexError):
            record_quarantine_entry(
                index_path,
                payload=bad_abs,
                workspace_root=self.ws_root,
                lease_id=self.lease_id,
                conversation_id=self.conv_id,
            )

        # 2. Forbidden fields: extracted text, prompts, credentials, envelope_id
        for forbidden_key in ["text", "extracted_text", "content", "body", "prompt", "credentials", "envelope_id"]:
            bad_content = dict(entry)
            bad_content[forbidden_key] = "secret or text content"
            with self.assertRaises(ForbiddenContentError):
                record_quarantine_entry(
                    index_path,
                    payload=bad_content,
                    workspace_root=self.ws_root,
                    lease_id=self.lease_id,
                    conversation_id=self.conv_id,
                )

    # --------------------------------------------------------------------------
    # 10. Reconcile Without Mutation
    # --------------------------------------------------------------------------
    def test_reconcile_without_mutation(self) -> None:
        """Reconcile must report consistent, missing_review, drift without mutating index or disk."""
        index_path = self.data_dir / INDEX_FILENAME

        # Entry 1: consistent
        e1 = self._create_sample_quarantine_run("run_rec_1", "<m1@test.org>", "f1.pdf", b"%PDF-1.4 file 1")
        record_quarantine_entry(index_path, payload=e1, workspace_root=self.ws_root, lease_id=self.lease_id, conversation_id=self.conv_id)

        # Entry 2: will become missing_review
        e2 = self._create_sample_quarantine_run("run_rec_2", "<m2@test.org>", "f2.pdf", b"%PDF-1.4 file 2")
        record_quarantine_entry(index_path, payload=e2, workspace_root=self.ws_root, lease_id=self.lease_id, conversation_id=self.conv_id)
        # Delete file 2 from disk
        (self.ws_root / e2["quarantine_path"]).unlink()

        # Entry 3: will become drift (tamper disk file)
        e3 = self._create_sample_quarantine_run("run_rec_3", "<m3@test.org>", "f3.pdf", b"%PDF-1.4 file 3")
        record_quarantine_entry(index_path, payload=e3, workspace_root=self.ws_root, lease_id=self.lease_id, conversation_id=self.conv_id)
        # Modify file 3 on disk
        (self.ws_root / e3["quarantine_path"]).write_bytes(b"%PDF-1.4 tampered bytes")

        # Snapshot index before reconcile
        index_bytes_before = index_path.read_bytes()

        # Run reconcile
        recon = reconcile_quarantine_index(index_path, workspace_root=self.ws_root)

        # Verify findings
        self.assertEqual(recon["total_entries"], 3)
        self.assertEqual(recon["consistent_count"], 1)
        self.assertEqual(recon["missing_review_count"], 1)
        self.assertEqual(recon["drift_count"], 1)

        findings_map = {r["attachment_id"]: r["status"] for r in recon["results"]}
        self.assertEqual(findings_map[e1["attachment_id"]], "consistent")
        self.assertEqual(findings_map[e2["attachment_id"]], "missing_review")
        self.assertEqual(findings_map[e3["attachment_id"]], "drift")

        # Verify index was not mutated
        index_bytes_after = index_path.read_bytes()
        self.assertEqual(index_bytes_before, index_bytes_after)

    # --------------------------------------------------------------------------
    # 11. Two Analyzed PDFs from Separate Runs
    # --------------------------------------------------------------------------
    def test_two_analyzed_pdfs_from_separate_runs(self) -> None:
        """Two analyzed PDFs from separate runs must be indexed cleanly and reported consistent."""
        index_path = self.data_dir / INDEX_FILENAME

        pdf_alpha = b"%PDF-1.4 Alpha Report Header\nSample Alpha Content"
        pdf_beta = b"%PDF-1.4 Beta Invoice Header\nSample Beta Content"

        entry_alpha = self._create_sample_quarantine_run(
            "run_alpha_20260916", "<alpha@example.org>", "report_alpha.pdf", pdf_alpha, part_locator="1"
        )
        entry_beta = self._create_sample_quarantine_run(
            "run_beta_20260916", "<beta@example.org>", "invoice_beta.pdf", pdf_beta, part_locator="2"
        )

        res_a = record_quarantine_entry(
            index_path, payload=entry_alpha, workspace_root=self.ws_root, lease_id=self.lease_id, conversation_id=self.conv_id
        )
        res_b = record_quarantine_entry(
            index_path, payload=entry_beta, workspace_root=self.ws_root, lease_id=self.lease_id, conversation_id=self.conv_id
        )

        self.assertEqual(res_a["status"], "created")
        self.assertEqual(res_b["status"], "created")

        index_data = load_quarantine_index(index_path)
        self.assertEqual(len(index_data["items"]), 2)
        self.assertIn(entry_alpha["attachment_id"], index_data["items"])
        self.assertIn(entry_beta["attachment_id"], index_data["items"])

        recon = reconcile_quarantine_index(index_path, workspace_root=self.ws_root)
        self.assertEqual(recon["consistent_count"], 2)
        self.assertEqual(recon["missing_review_count"], 0)
        self.assertEqual(recon["drift_count"], 0)

    # --------------------------------------------------------------------------
    # 12. Byte-Identical Final Location Index
    # --------------------------------------------------------------------------
    def test_final_location_index_remains_byte_identical(self) -> None:
        """final-location-index.json must not be touched or mutated by quarantine index operations."""
        final_index_path = self.data_dir / "final-location-index.json"
        initial_content = json.dumps(
            {
                "schema_version": 1,
                "updated_at": "2026-09-16T10:00:00Z",
                "items": {
                    "<msg_final_01@example.org>": {
                        "message_id": "<msg_final_01@example.org>",
                        "final_folder": "Projekte/XYZ",
                        "envelope_id": "42",
                    }
                },
            },
            indent=2,
        ).encode("utf-8")
        final_index_path.write_bytes(initial_content)
        initial_hash = hashlib.sha256(initial_content).hexdigest()

        # Perform quarantine operations
        entry = self._create_sample_quarantine_run(
            "run_iso_01", "<msg_iso@example.org>", "iso.pdf", b"%PDF-1.4 byte identical test"
        )
        record_quarantine_entry(
            self.data_dir / INDEX_FILENAME,
            payload=entry,
            workspace_root=self.ws_root,
            lease_id=self.lease_id,
            conversation_id=self.conv_id,
        )
        reconcile_quarantine_index(self.data_dir / INDEX_FILENAME, workspace_root=self.ws_root)
        get_quarantine_index_stats(self.data_dir / INDEX_FILENAME)

        # Verify final-location-index.json is byte-identical
        after_content = final_index_path.read_bytes()
        after_hash = hashlib.sha256(after_content).hexdigest()
        self.assertEqual(initial_hash, after_hash)
        self.assertEqual(initial_content, after_content)

    # --------------------------------------------------------------------------
    # 13. CLI Script Invocations
    # --------------------------------------------------------------------------
    def test_cli_script_invocations(self) -> None:
        """Verify CLI script stats, lookup, reconcile, and record commands."""
        script_path = _scripts_dir / "mail_desk_attachment_quarantine_index.py"
        self.assertTrue(script_path.exists())

        # 1. stats on empty
        res = subprocess.run(
            [sys.executable, str(script_path), "stats", "--data-dir", str(self.data_dir), "--json"],
            capture_output=True,
            text=True,
            check=True,
        )
        out_json = json.loads(res.stdout)
        self.assertTrue(out_json["success"])
        self.assertEqual(out_json["data"]["stats"]["total_indexed_items"], 0)

        # 2. record via CLI
        entry = self._create_sample_quarantine_run(
            "run_cli_01", "<cli_msg@example.org>", "cli_doc.pdf", b"%PDF-1.4 CLI test"
        )
        manifest_file = self.data_dir / "cli_input.json"
        manifest_file.write_text(json.dumps(entry), encoding="utf-8")

        env = dict(os.environ)
        env["WORKSPACE_ROOT"] = str(self.ws_root)
        env["WORKSPACE_LOCK_LEASE_ID"] = self.lease_id
        env["WORKSPACE_LOCK_CONVERSATION_ID"] = self.conv_id

        res_rec = subprocess.run(
            [sys.executable, str(script_path), "record", "--input", str(manifest_file), "--data-dir", str(self.data_dir), "--json"],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        rec_json = json.loads(res_rec.stdout)
        self.assertTrue(rec_json["success"])

        # 3. lookup via CLI
        res_lookup = subprocess.run(
            [sys.executable, str(script_path), "lookup", "--attachment-id", entry["attachment_id"], "--data-dir", str(self.data_dir), "--json"],
            capture_output=True,
            text=True,
            check=True,
        )
        lookup_json = json.loads(res_lookup.stdout)
        self.assertTrue(lookup_json["success"])
        self.assertEqual(lookup_json["data"]["item"]["attachment_id"], entry["attachment_id"])

        # 4. reconcile via CLI
        res_recon = subprocess.run(
            [sys.executable, str(script_path), "reconcile", "--data-dir", str(self.data_dir), "--workspace-root", str(self.ws_root), "--json"],
            capture_output=True,
            text=True,
            check=True,
        )
        recon_json = json.loads(res_recon.stdout)
        self.assertTrue(recon_json["success"])
        self.assertEqual(recon_json["data"]["reconcile"]["consistent_count"], 1)


if __name__ == "__main__":
    unittest.main()
