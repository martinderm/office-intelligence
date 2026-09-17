"""Focused unit and integration tests for FR-14 / MD-C1: Additive Coverage Fields in Schema 1.

Verifies the 8 focused MD-C1 requirements:
1. Vollständige PDF -> analysis_completeness: full
2. Zeichenlimitierte PDF -> analysis_completeness: truncated mit Grund und Budget
3. Bestehender Schema-1-Eintrag ohne Coverage -> Anzeige unknown, Datei byte-identisch
4. Neuer Schema-1-Eintrag mit Coverage (und idempotente Wiederholung)
5. 'full' plus Truncation wird abgewiesen (fail-closed in Handoff & Quarantäneindex)
6. Coverage-Änderung verändert Handoff- und Candidate-Hash (und Drift stoppt fail-closed)
7. MD-A3-A5 und MD-Q2/Q3 Regressionen
8. final-location-index.json bleibt byte-identisch
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sys
import tempfile
import unittest
from unittest.mock import patch

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

from core.attachment_handoff import (
    ALLOWED_ANALYSIS_COMPLETENESS,
    ALLOWED_TRUNCATION_REASONS,
    ALLOWED_TRUNCATION_STAGES,
    ANALYSIS_COMPLETENESS_FULL,
    ANALYSIS_COMPLETENESS_PARTIAL,
    ANALYSIS_COMPLETENESS_TRUNCATED,
    ANALYSIS_COMPLETENESS_UNAVAILABLE,
    ANALYSIS_COMPLETENESS_UNKNOWN,
    MAX_CHARS_PER_ATTACHMENT,
    MAX_CHARS_PER_MAIL,
    MATERIALITY_REQUIRED_FOR_DECISION,
    MATERIALITY_SUPPLEMENTARY,
    TRUNCATION_STAGE_EXTRACTION,
    TRUNCATION_STAGE_HANDOFF_CUMULATIVE_MAIL,
    TRUNCATION_STAGE_HANDOFF_PER_ATTACHMENT,
    TRUNCATION_STAGE_NONE,
    AttachmentHandoffError,
    HandoffDriftError,
    build_attachment_analysis_handoff,
    compute_handoff_hash,
    validate_attachment_handoff,
)
from core.attachment_filing import (
    compute_candidate_hash,
    propose_attachment_filing,
)
from core.attachment_fetch import compute_review_hash
from core.attachment_policy import sanitize_attachment_filename
from core.attachment_quarantine_index import (
    INDEX_FILENAME,
    SCHEMA_VERSION,
    AttachmentIndexDriftError,
    AttachmentIndexSchemaError,
    ForbiddenContentError,
    WorkspaceLockRequiredError,
    compute_attachment_id,
    get_quarantine_index_stats,
    load_quarantine_index,
    lookup_quarantine_entry,
    record_quarantine_entry,
    save_quarantine_index_atomic,
    validate_quarantine_index_entry,
)
from core.common import normalize_message_id, utc_now_iso


class TestMailDeskAttachmentCoverageMDC1(unittest.TestCase):
    """Focused test suite for FR-14 / MD-C1 additive coverage fields in Schema 1."""

    def setUp(self) -> None:
        self.tmp_dir_obj = tempfile.TemporaryDirectory()
        self.ws_root = Path(self.tmp_dir_obj.name).resolve()
        self.data_dir = self.ws_root / "data" / "mail-desk"
        self.attachments_dir = self.data_dir / "attachments"
        self.attachments_dir.mkdir(parents=True, exist_ok=True)

        self.lease_id = "test_lease_mdc1_001"
        self.conv_id = "test_conv_mdc1_001"

        # Mock workspace lock
        self._lock_patch = patch(
            "core.attachment_quarantine_index.verify_quarantine_workspace_lock",
            return_value=True,
        )
        self._lock_patch.start()

    def tearDown(self) -> None:
        self._lock_patch.stop()
        self.tmp_dir_obj.cleanup()

    def _setup_quarantine_file(
        self,
        run_id: str,
        filename: str,
        content: bytes,
        message_id: str = "mid-test-1@example.com",
    ) -> tuple[str, str, int]:
        run_dir = self.attachments_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        target_file = run_dir / filename
        target_file.write_bytes(content)

        sha = hashlib.sha256(content).hexdigest().lower()
        size = len(content)
        rel_path = f"data/mail-desk/attachments/{run_id}/{filename}"

        norm_mid = normalize_message_id(message_id)
        inventory = {
            "schema_version": 1,
            "messages": {
                norm_mid: {
                    "count": 1,
                    "total_bytes": size,
                    "files": {
                        filename: {
                            "sha256": sha,
                            "size_bytes": size,
                        }
                    },
                }
            },
        }
        (run_dir / ".quarantine-inventory.json").write_text(
            json.dumps(inventory, indent=2), encoding="utf-8"
        )
        return rel_path, sha, size

    def _build_sample_attachment(
        self,
        *,
        account: str = "primary",
        message_id: str = "<msg-full-001@example.com>",
        folder: str = "INBOX",
        envelope_id: str = "env-001",
        part_locator: str = "1",
        filename: str = "document.pdf",
        sha256: str,
        size_bytes: int = 100,
        run_id: str = "run_mdc1_test",
        mime_type: str = "application/pdf",
        status: str = "fetched",
    ) -> dict[str, Any]:
        cand = {
            "account": account,
            "message_id": message_id,
            "folder": folder,
            "envelope_id": envelope_id,
            "part_locator": part_locator,
            "filename": filename,
            "sha256": sha256,
            "size_bytes": size_bytes,
            "mime_type": mime_type,
            "fetch_status": "available",
            "provenance": "rfc822_mime_inspection",
        }
        norm_mid = normalize_message_id(message_id) or ""
        rev_hash = compute_review_hash(
            account=account,
            message_id=norm_mid,
            folder=folder,
            envelope_id=envelope_id,
            part_locator=part_locator,
            inventory_sha256=sha256,
        )
        receipt = {
            "receipt_id": "rcpt-001",
            "request_hash": rev_hash,
            "approved_at": "2026-09-17T10:00:00Z",
            "approved_by": "human_reviewer",
        }
        op = {
            "action": "attachment_fetch",
            "account": account,
            "message_id": message_id,
            "folder": folder,
            "envelope_id": envelope_id,
            "part_locator": part_locator,
            "inventory_sha256": sha256,
            "review_hash": rev_hash,
            "approval_receipt": receipt,
            "run_id": run_id,
            "candidate": cand,
        }
        clean_fn = sanitize_attachment_filename(filename)
        rel_path = f"data/mail-desk/attachments/{run_id}/{clean_fn}"
        res = {
            "status": status,
            "run_id": run_id,
            "filename": clean_fn,
            "inventory_sha256": sha256,
            "fetch_sha256": sha256,
            "effective_mime_type": mime_type,
            "size_bytes": size_bytes,
            "relative_path": rel_path,
            "error": None,
        }
        return {
            "operation": op,
            "result": res,
            "candidate": cand,
        }

    # --------------------------------------------------------------------------
    # 1. Vollständige PDF -> analysis_completeness: full
    # --------------------------------------------------------------------------
    def test_01_full_pdf_extraction_to_handoff_and_candidate(self) -> None:
        """Complete PDF extraction results in analysis_completeness='full' without truncation."""
        text = "This is the complete text of an unconstrained document."
        sha = hashlib.sha256(b"full_doc").hexdigest()

        canonical_parts = [{
            "part_locator": "1",
            "filename": "document.pdf",
            "sha256": sha,
            "source_sha256": sha,
            "mime_type": "application/pdf",
            "provenance": "rfc822_mime_inspection",
        }]

        extraction = {
            "part_locator": "1",
            "filename": "document.pdf",
            "source_sha256": sha,
            "mime_type": "application/pdf",
            "materiality": MATERIALITY_REQUIRED_FOR_DECISION,
            "status": "extracted",
            "quality": "high",
            "character_count": len(text),
            "source_character_count": len(text),
            "truncated": False,
            "truncation_reason": None,
            "raw_text": text,
        }

        mail_id = {
            "account": "primary",
            "message_id": "<msg-full-001@example.com>",
            "folder": "INBOX",
            "envelope_id": "env-001",
        }
        decision = {"kind": "project", "id": "proj-alpha", "confidence": "high"}

        handoff = build_attachment_analysis_handoff(
            mail_identity=mail_id,
            attachments=[extraction],
            decision=decision,
            canonical_parts=canonical_parts,
        )

        item = handoff["items"][0]
        self.assertEqual(item["analysis_completeness"], ANALYSIS_COMPLETENESS_FULL)
        self.assertIsNone(item["truncation_reason"])
        self.assertEqual(item["truncation_stage"], TRUNCATION_STAGE_NONE)
        self.assertEqual(item["handoff_character_count"], len(text))
        self.assertEqual(item["source_character_count"], len(text))
        self.assertEqual(item["analysis_character_budget"], MAX_CHARS_PER_ATTACHMENT)

        # Propose filing candidate
        mock_catalogs = {
            "projects": [
                {
                    "id": "proj-alpha",
                    "title": "Project Alpha",
                    "cloud_sync": {
                        "primary": {
                            "scan_dir": "data/cloud/ALPHA",
                            "target_dir": "01_Admin/Correspondence",
                            "output_json": "memory/cloud/projects/proj-alpha/filemap.json",
                            "output_dir": "memory/cloud/projects/proj-alpha",
                        }
                    },
                },
            ]
        }
        filemap_alpha = {
            "$schema": "https://raw.githubusercontent.com/martinderm/office-intelligence/main/skills/cloud-atlas/references/filemap.schema.json",
            "schema_version": 1,
            "kind": "cloud-filemap",
            "scope": "project",
            "storage_id": "primary",
            "project": "proj-alpha",
            "project_title": "Project Alpha",
            "scan_dir": "data/cloud/ALPHA",
            "output_dir": "memory/cloud/projects/proj-alpha",
            "updated_at": "2026-09-17 10:00:00",
            "files": {
                "data/cloud/ALPHA/01_Admin/Correspondence/existing_doc.pdf": {
                    "version": "1",
                    "mtime": "2026-09-17 08:00:00",
                    "size": "100 KB",
                    "sha256": "f" * 64,
                    "description": "Existing correspondence",
                }
            },
        }
        sample_att = self._build_sample_attachment(
            sha256=sha,
            filename="document.pdf",
            part_locator="1",
            message_id="<msg-full-001@example.com>",
            run_id="run_mdc1_test",
        )
        candidate = propose_attachment_filing(
            sample_att,
            decision,
            mock_catalogs,
            manifest_account="primary",
            filemaps={"primary": filemap_alpha},
            handoff=handoff,
            canonical_parts=canonical_parts,
            current_time=datetime(2026, 9, 17, 10, 30, 0, tzinfo=timezone.utc),
        )

        cov = candidate["coverage_evidence"]
        self.assertEqual(cov["analysis_completeness"], ANALYSIS_COMPLETENESS_FULL)
        self.assertIsNone(cov["truncation_reason"])
        self.assertEqual(cov["truncation_stage"], TRUNCATION_STAGE_NONE)
        self.assertEqual(cov["handoff_character_count"], len(text))
        self.assertEqual(cov["analysis_character_budget"], MAX_CHARS_PER_ATTACHMENT)
        self.assertEqual(cov["source_character_count"], len(text))
        # Reason should NOT contain limited analysis marker
        self.assertNotIn("[Coverage:", candidate["reason"])

    # --------------------------------------------------------------------------
    # 2. Zeichenlimitierte PDF -> analysis_completeness: truncated mit Grund und Budget
    # --------------------------------------------------------------------------
    def test_02_truncated_pdf_extraction_to_handoff_and_candidate(self) -> None:
        """Character-limited PDF extraction results in 'truncated' with reason and budget."""
        char_limit = 100
        long_text = "A" * 500
        sha = hashlib.sha256(b"truncated_doc").hexdigest()

        canonical_parts = [{
            "part_locator": "1",
            "filename": "large.pdf",
            "sha256": sha,
            "source_sha256": sha,
            "mime_type": "application/pdf",
            "provenance": "rfc822_mime_inspection",
        }]

        extraction = {
            "part_locator": "1",
            "filename": "large.pdf",
            "source_sha256": sha,
            "mime_type": "application/pdf",
            "materiality": MATERIALITY_REQUIRED_FOR_DECISION,
            "status": "extracted",
            "quality": "high",
            "character_count": len(long_text),
            "source_character_count": len(long_text),
            "truncated": False,
            "truncation_reason": None,
            "raw_text": long_text,
        }

        mail_id = {
            "account": "primary",
            "message_id": "<msg-trunc-002@example.com>",
            "folder": "INBOX",
            "envelope_id": "env-002",
        }
        decision = {"kind": "project", "id": "proj-beta", "confidence": "high"}

        handoff = build_attachment_analysis_handoff(
            mail_identity=mail_id,
            attachments=[extraction],
            decision=decision,
            canonical_parts=canonical_parts,
            max_chars_per_attachment=char_limit,
        )

        item = handoff["items"][0]
        self.assertEqual(item["analysis_completeness"], ANALYSIS_COMPLETENESS_TRUNCATED)
        self.assertEqual(item["truncation_reason"], "max_chars_exceeded")
        self.assertEqual(item["truncation_stage"], TRUNCATION_STAGE_HANDOFF_PER_ATTACHMENT)
        self.assertEqual(item["handoff_character_count"], char_limit)
        self.assertEqual(item["analysis_character_budget"], char_limit)
        self.assertEqual(item["source_character_count"], len(long_text))

        # Propose filing candidate
        mock_catalogs = {
            "projects": [
                {
                    "id": "proj-beta",
                    "title": "Project Beta",
                    "cloud_sync": {
                        "primary": {
                            "scan_dir": "data/cloud/BETA",
                            "target_dir": "01_Admin/Correspondence",
                            "output_json": "memory/cloud/projects/proj-beta/filemap.json",
                            "output_dir": "memory/cloud/projects/proj-beta",
                        }
                    },
                },
            ]
        }
        filemap_beta = {
            "$schema": "https://raw.githubusercontent.com/martinderm/office-intelligence/main/skills/cloud-atlas/references/filemap.schema.json",
            "schema_version": 1,
            "kind": "cloud-filemap",
            "scope": "project",
            "storage_id": "primary",
            "project": "proj-beta",
            "project_title": "Project Beta",
            "scan_dir": "data/cloud/BETA",
            "output_dir": "memory/cloud/projects/proj-beta",
            "updated_at": "2026-09-17 10:00:00",
            "files": {
                "data/cloud/BETA/01_Admin/Correspondence/existing_doc.pdf": {
                    "version": "1",
                    "mtime": "2026-09-17 08:00:00",
                    "size": "100 KB",
                    "sha256": "f" * 64,
                    "description": "Existing correspondence",
                }
            },
        }
        sample_att = self._build_sample_attachment(
            sha256=sha,
            filename="large.pdf",
            part_locator="1",
            message_id="<msg-trunc-002@example.com>",
            envelope_id="env-002",
            run_id="run_mdc1_test",
        )
        candidate = propose_attachment_filing(
            sample_att,
            decision,
            mock_catalogs,
            manifest_account="primary",
            filemaps={"primary": filemap_beta},
            handoff=handoff,
            canonical_parts=canonical_parts,
            current_time=datetime(2026, 9, 17, 10, 30, 0, tzinfo=timezone.utc),
        )

        cov = candidate["coverage_evidence"]
        self.assertEqual(cov["analysis_completeness"], ANALYSIS_COMPLETENESS_TRUNCATED)
        self.assertEqual(cov["truncation_reason"], "max_chars_exceeded")
        self.assertEqual(cov["truncation_stage"], TRUNCATION_STAGE_HANDOFF_PER_ATTACHMENT)
        self.assertEqual(cov["handoff_character_count"], char_limit)
        self.assertEqual(cov["analysis_character_budget"], char_limit)
        self.assertEqual(cov["source_character_count"], len(long_text))
        # Visible note in filing candidate reason
        self.assertIn("[Coverage: truncated (max_chars_exceeded)]", candidate["reason"])

    # --------------------------------------------------------------------------
    # 3. Bestehender Schema-1-Eintrag ohne Coverage -> Anzeige unknown, Datei byte-identisch
    # --------------------------------------------------------------------------
    def test_03_existing_schema1_entry_without_coverage_reports_unknown_byte_identical(self) -> None:
        """Existing Schema 1 entry without coverage fields reports 'unknown' on read; file stays byte-identical."""
        index_path = self.data_dir / INDEX_FILENAME
        mid = "msg-legacy-001@example.com"
        sha = "b" * 64
        entry_id = compute_attachment_id(mid, "1", sha)
        entry_raw = {
            "attachment_id": entry_id,
            "message_id": mid,
            "account": "primary",
            "folder": "INBOX",
            "original_folder": "INBOX",
            "part_locator": "1",
            "clean_filename": "legacy.pdf",
            "filename": "legacy.pdf",
            "mime_type": "application/pdf",
            "effective_mime_type": "application/pdf",
            "sha256": sha,
            "size_bytes": 1024,
            "run_id": "run-2026-09-17-001",
            "quarantine_path": "data/mail-desk/attachments/run-2026-09-17-001/legacy.pdf",
            "analysis_status": "completed",
            "analyzed_at": "2026-09-17T10:00:00Z",
            "contract_version": "v1.0",
            "lifecycle_state": "quarantined",
            "disposition_ref": None,
        }

        index_doc = {
            "schema_version": 1,
            "updated_at": "2026-09-17T10:00:00Z",
            "items": {
                entry_id: entry_raw,
            },
        }
        original_bytes = json.dumps(index_doc, indent=2).encode("utf-8") + b"\n"
        index_path.write_bytes(original_bytes)

        # Lookup by attachment_id
        entry = lookup_quarantine_entry(index_path, attachment_id=entry_id)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["analysis_completeness"], ANALYSIS_COMPLETENESS_UNKNOWN)

        # File on disk must remain strictly byte-identical
        after_bytes = index_path.read_bytes()
        self.assertEqual(after_bytes, original_bytes)

        # load_quarantine_index should also not rewrite file
        loaded = load_quarantine_index(index_path)
        self.assertEqual(loaded["schema_version"], 1)
        self.assertEqual(index_path.read_bytes(), original_bytes)

    # --------------------------------------------------------------------------
    # 4. Neuer Schema-1-Eintrag mit Coverage
    # --------------------------------------------------------------------------
    def test_04_new_schema1_entry_with_coverage_and_idempotency(self) -> None:
        """New Schema 1 entry persists coverage fields and supports pure idempotent repetition."""
        index_path = self.data_dir / INDEX_FILENAME
        run_id = "run-2026-09-17-002"
        rel_path, sha, size = self._setup_quarantine_file(
            run_id=run_id,
            filename="new_covered.pdf",
            content=b"%PDF-1.4 new covered document",
            message_id="msg-new-004@example.com",
        )

        mid = "msg-new-004@example.com"
        att_id = compute_attachment_id(mid, "1", sha)

        payload = {
            "attachment_id": att_id,
            "message_id": mid,
            "account": "primary",
            "folder": "INBOX",
            "part_locator": "1",
            "clean_filename": "new_covered.pdf",
            "mime_type": "application/pdf",
            "sha256": sha,
            "size_bytes": size,
            "run_id": run_id,
            "quarantine_path": rel_path,
            "analysis_status": "completed",
            "analyzed_at": utc_now_iso(),
            "contract_version": "v1.0",
            "lifecycle_state": "quarantined",
            "disposition_ref": None,
            "analysis_completeness": ANALYSIS_COMPLETENESS_TRUNCATED,
            "truncation_reason": "max_chars_exceeded",
            "truncation_stage": TRUNCATION_STAGE_HANDOFF_PER_ATTACHMENT,
            "handoff_character_count": 15000,
            "analysis_character_budget": 15000,
            "source_character_count": 30000,
        }

        # 1. First record: created
        res1 = record_quarantine_entry(
            index_path,
            payload=payload,
            workspace_root=self.ws_root,
        )
        self.assertEqual(res1["status"], "created")
        self.assertEqual(res1["attachment_id"], att_id)

        # Verify on disk schema_version is 1 and coverage fields are present
        saved = json.loads(index_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["schema_version"], 1)
        item_on_disk = saved["items"][att_id]
        self.assertEqual(item_on_disk["analysis_completeness"], ANALYSIS_COMPLETENESS_TRUNCATED)
        self.assertEqual(item_on_disk["truncation_reason"], "max_chars_exceeded")
        self.assertEqual(item_on_disk["handoff_character_count"], 15000)

        # 2. Second record (identical): unchanged (idempotent no-op)
        res2 = record_quarantine_entry(
            index_path,
            payload=payload,
            workspace_root=self.ws_root,
        )
        self.assertEqual(res2["status"], "unchanged")

    # --------------------------------------------------------------------------
    # 5. 'full' plus Truncation wird abgewiesen
    # --------------------------------------------------------------------------
    def test_05_full_plus_truncation_rejected(self) -> None:
        """analysis_completeness='full' is strictly rejected when truncation is present."""
        # 1. Handoff rejection
        mail_id = {
            "account": "primary",
            "message_id": "<msg-contradict@example.com>",
            "folder": "INBOX",
            "envelope_id": "env-contradict",
        }
        contradictory_handoff = {
            "schema_version": 1,
            "status": "ready",
            "blocked_reason": None,
            "mail_identity": mail_id,
            "decision_snapshot": {"kind": "project", "id": "p1"},
            "canonical_parts": [{
                "part_locator": "1",
                "filename": "doc.pdf",
                "sha256": "c" * 64,
                "mime_type": "application/pdf",
                "provenance": "rfc822_mime_inspection",
            }],
            "total_attachments": 1,
            "total_chars": 10,
            "cumulative_chars_budget": 30000,
            "is_cumulative_truncated": False,
            "blocked_required_attachments": [],
            "items": [{
                "part_locator": "1",
                "filename": "doc.pdf",
                "source_sha256": "c" * 64,
                "mime_type": "application/pdf",
                "materiality": MATERIALITY_SUPPLEMENTARY,
                "status": "extracted",
                "char_count": 10,
                "truncated": True,  # Contradiction: truncated=True with 'full'
                "truncation_reason": "max_chars_exceeded",
                "content_hash": hashlib.sha256(b"hello").hexdigest(),
                "xml_block": '<untrusted_attachment_content part_locator="1" filename="doc.pdf" source_sha256="' + "c"*64 + '" sha256="' + "c"*64 + '" mime_type="application/pdf" materiality="supplementary" status="extracted" char_count="10" truncated="true">\nhello\n</untrusted_attachment_content>',
                "analysis_completeness": ANALYSIS_COMPLETENESS_FULL,
                "truncation_stage": TRUNCATION_STAGE_EXTRACTION,
            }],
            "prompt_content": "dummy",
        }
        with self.assertRaises(AttachmentHandoffError):
            validate_attachment_handoff(
                contradictory_handoff,
                mail_identity=mail_id,
                canonical_parts=contradictory_handoff["canonical_parts"],
            )

        # 2. Quarantine Index rejection
        mid = "msg-invalid@example.com"
        sha = "e" * 64
        att_id = compute_attachment_id(mid, "1", sha)
        invalid_index_entry = {
            "attachment_id": att_id,
            "message_id": mid,
            "account": "primary",
            "folder": "INBOX",
            "part_locator": "1",
            "clean_filename": "doc.pdf",
            "mime_type": "application/pdf",
            "sha256": sha,
            "size_bytes": 100,
            "run_id": "run-2026-09-17-003",
            "quarantine_path": "data/mail-desk/attachments/run-2026-09-17-003/doc.pdf",
            "analysis_status": "completed",
            "analyzed_at": utc_now_iso(),
            "contract_version": "v1.0",
            "lifecycle_state": "quarantined",
            "analysis_completeness": ANALYSIS_COMPLETENESS_FULL,
            "truncation_reason": "max_chars_exceeded",  # Forbidden with 'full'
            "truncation_stage": TRUNCATION_STAGE_NONE,
        }
        with self.assertRaises(AttachmentIndexSchemaError):
            validate_quarantine_index_entry(invalid_index_entry)

    # --------------------------------------------------------------------------
    # 6. Coverage-Änderung verändert Handoff- und Candidate-Hash
    # --------------------------------------------------------------------------
    def test_06_coverage_change_alters_handoff_and_candidate_hash(self) -> None:
        """Mutating coverage fields changes the deterministic handoff_hash and candidate_hash."""
        mail_id = {
            "account": "primary",
            "message_id": "<msg-hash-001@example.com>",
            "folder": "INBOX",
            "envelope_id": "env-hash-001",
        }
        base_item = {
            "part_locator": "1",
            "filename": "doc.pdf",
            "source_sha256": "f" * 64,
            "mime_type": "application/pdf",
            "materiality": "supplementary",
            "status": "extracted",
            "char_count": 100,
            "truncated": False,
            "truncation_reason": None,
            "analysis_completeness": ANALYSIS_COMPLETENESS_FULL,
            "truncation_stage": TRUNCATION_STAGE_NONE,
            "handoff_character_count": 100,
            "analysis_character_budget": 15000,
            "source_character_count": 100,
        }

        hash_full = compute_handoff_hash(mail_id, [base_item])

        # Mutate to truncated
        mutated_item = dict(base_item)
        mutated_item["analysis_completeness"] = ANALYSIS_COMPLETENESS_TRUNCATED
        mutated_item["truncated"] = True
        mutated_item["truncation_reason"] = "max_chars_exceeded"
        mutated_item["truncation_stage"] = TRUNCATION_STAGE_HANDOFF_PER_ATTACHMENT
        hash_trunc = compute_handoff_hash(mail_id, [mutated_item])

        self.assertNotEqual(hash_full, hash_trunc)

        # Candidate hash test
        candidate_full_payload = {
            "schema_version": 1,
            "candidate_type": "attachment_filing_candidate",
            "storage_id": "proj-alpha",
            "part_locator": "1",
            "filename": "doc.pdf",
            "source_sha256": "f" * 64,
            "target_relative_path": "projects/alpha/doc.pdf",
            "coverage_evidence": {
                "analysis_completeness": ANALYSIS_COMPLETENESS_FULL,
                "truncation_reason": None,
                "truncation_stage": TRUNCATION_STAGE_NONE,
                "handoff_character_count": 100,
                "analysis_character_budget": 15000,
                "source_character_count": 100,
            },
        }
        cand_hash_1 = compute_candidate_hash(candidate_full_payload)

        candidate_trunc_payload = copy.deepcopy(candidate_full_payload)
        candidate_trunc_payload["coverage_evidence"]["analysis_completeness"] = ANALYSIS_COMPLETENESS_TRUNCATED
        candidate_trunc_payload["coverage_evidence"]["truncation_reason"] = "max_chars_exceeded"
        cand_hash_2 = compute_candidate_hash(candidate_trunc_payload)

        self.assertNotEqual(cand_hash_1, cand_hash_2)

        # Drift validation fails closed: recomputed candidate_hash drifts
        candidate_with_drift = copy.deepcopy(candidate_full_payload)
        candidate_with_drift["candidate_hash"] = cand_hash_1
        candidate_with_drift["coverage_evidence"]["analysis_completeness"] = ANALYSIS_COMPLETENESS_TRUNCATED
        recomputed = compute_candidate_hash(candidate_with_drift)
        self.assertNotEqual(recomputed, candidate_with_drift["candidate_hash"])

    # --------------------------------------------------------------------------
    # 7. MD-A3–A5 und MD-Q2/Q3 bleiben grün (verifies compatibility invariants)
    # --------------------------------------------------------------------------
    def test_07_quarantine_index_reconcile_and_drift_compatibility(self) -> None:
        """Verify that Schema 1 quarantine index entries drift fail-closed on coverage mismatch."""
        index_path = self.data_dir / INDEX_FILENAME
        run_id = "run-2026-09-17-005"
        rel_path, sha, size = self._setup_quarantine_file(
            run_id=run_id,
            filename="drift_test.pdf",
            content=b"%PDF-1.4 drift test",
            message_id="msg-drift@example.com",
        )

        mid = "msg-drift@example.com"
        att_id = compute_attachment_id(mid, "1", sha)

        payload1 = {
            "attachment_id": att_id,
            "message_id": mid,
            "account": "primary",
            "folder": "INBOX",
            "part_locator": "1",
            "clean_filename": "drift_test.pdf",
            "mime_type": "application/pdf",
            "sha256": sha,
            "size_bytes": size,
            "run_id": run_id,
            "quarantine_path": rel_path,
            "analysis_status": "completed",
            "analyzed_at": utc_now_iso(),
            "contract_version": "v1.0",
            "lifecycle_state": "quarantined",
            "disposition_ref": None,
            "analysis_completeness": ANALYSIS_COMPLETENESS_FULL,
            "truncation_reason": None,
            "truncation_stage": TRUNCATION_STAGE_NONE,
            "handoff_character_count": 500,
            "analysis_character_budget": 15000,
            "source_character_count": 500,
        }

        record_quarantine_entry(index_path, payload=payload1, workspace_root=self.ws_root)

        # Attempt to re-record same attachment_id with conflicting completeness -> must raise AttachmentIndexDriftError
        payload_drift = copy.deepcopy(payload1)
        payload_drift["analysis_completeness"] = ANALYSIS_COMPLETENESS_TRUNCATED
        payload_drift["truncation_reason"] = "max_chars_exceeded"
        payload_drift["truncation_stage"] = TRUNCATION_STAGE_HANDOFF_PER_ATTACHMENT

        with self.assertRaises(AttachmentIndexDriftError):
            record_quarantine_entry(index_path, payload=payload_drift, workspace_root=self.ws_root)

    # --------------------------------------------------------------------------
    # 8. final-location-index.json bleibt byte-identisch
    # --------------------------------------------------------------------------
    def test_08_final_location_index_remains_byte_identical(self) -> None:
        """Ensure that final-location-index.json is never touched or mutated by coverage operations."""
        final_index_path = self.data_dir / "final-location-index.json"
        initial_bytes = b'{\n  "schema_version": 1,\n  "messages": {\n    "msg-001": "done"\n  }\n}\n'
        final_index_path.write_bytes(initial_bytes)

        # Perform index operations
        run_id = "run-2026-09-17-008"
        rel_path, sha, size = self._setup_quarantine_file(
            run_id=run_id,
            filename="iso.pdf",
            content=b"%PDF-1.4 test isolation",
            message_id="msg-iso@example.com",
        )
        mid = "msg-iso@example.com"
        att_id = compute_attachment_id(mid, "1", sha)

        record_quarantine_entry(
            self.data_dir / INDEX_FILENAME,
            payload={
                "attachment_id": att_id,
                "message_id": mid,
                "account": "primary",
                "folder": "INBOX",
                "part_locator": "1",
                "clean_filename": "iso.pdf",
                "mime_type": "application/pdf",
                "sha256": sha,
                "size_bytes": size,
                "run_id": run_id,
                "quarantine_path": rel_path,
                "analysis_status": "completed",
                "analyzed_at": utc_now_iso(),
                "contract_version": "v1.0",
                "lifecycle_state": "quarantined",
                "disposition_ref": None,
                "analysis_completeness": ANALYSIS_COMPLETENESS_FULL,
                "truncation_reason": None,
                "truncation_stage": TRUNCATION_STAGE_NONE,
                "handoff_character_count": size,
                "analysis_character_budget": 15000,
                "source_character_count": size,
            },
            workspace_root=self.ws_root,
        )

        lookup_quarantine_entry(self.data_dir / INDEX_FILENAME, attachment_id=att_id)

        # Verify byte identity
        after_bytes = final_index_path.read_bytes()
        self.assertEqual(after_bytes, initial_bytes)

    # --------------------------------------------------------------------------
    # 9. P1 / P2 Contract Tests
    # --------------------------------------------------------------------------
    def test_09_persist_unknown_rejected(self) -> None:
        """'unknown' completeness is strictly rejected for new or persisted entries."""
        mid = "msg-unknown@example.com"
        sha = "d" * 64
        att_id = compute_attachment_id(mid, "1", sha)
        entry_with_unknown = {
            "attachment_id": att_id,
            "message_id": mid,
            "account": "primary",
            "folder": "INBOX",
            "part_locator": "1",
            "clean_filename": "doc.pdf",
            "mime_type": "application/pdf",
            "sha256": sha,
            "size_bytes": 100,
            "run_id": "run-2026-09-17-009",
            "quarantine_path": "data/mail-desk/attachments/run-2026-09-17-009/doc.pdf",
            "analysis_status": "completed",
            "analyzed_at": utc_now_iso(),
            "contract_version": "v1.0",
            "lifecycle_state": "quarantined",
            "disposition_ref": None,
            "analysis_completeness": "unknown",
            "truncation_reason": None,
            "truncation_stage": "none",
            "handoff_character_count": 100,
            "analysis_character_budget": 15000,
            "source_character_count": 100,
        }
        with self.assertRaises(AttachmentIndexSchemaError):
            validate_quarantine_index_entry(entry_with_unknown)

        with self.assertRaises(AttachmentIndexSchemaError):
            record_quarantine_entry(
                self.data_dir / INDEX_FILENAME,
                payload=entry_with_unknown,
                workspace_root=self.ws_root,
            )

    def test_10_handoff_unknown_rejected(self) -> None:
        """'unknown' completeness is strictly rejected in handoff items."""
        mail_id = {
            "account": "primary",
            "message_id": "<msg-handoff-unk@example.com>",
            "folder": "INBOX",
            "envelope_id": "env-unk",
        }
        handoff_with_unknown = {
            "schema_version": 1,
            "status": "ready",
            "blocked_reason": None,
            "mail_identity": mail_id,
            "decision_snapshot": {"kind": "project", "id": "p1"},
            "canonical_parts": [{
                "part_locator": "1",
                "filename": "doc.pdf",
                "sha256": "c" * 64,
                "mime_type": "application/pdf",
                "provenance": "rfc822_mime_inspection",
            }],
            "total_attachments": 1,
            "total_chars": 10,
            "cumulative_chars_budget": 30000,
            "is_cumulative_truncated": False,
            "blocked_required_attachments": [],
            "items": [{
                "part_locator": "1",
                "filename": "doc.pdf",
                "source_sha256": "c" * 64,
                "mime_type": "application/pdf",
                "materiality": MATERIALITY_SUPPLEMENTARY,
                "status": "extracted",
                "char_count": 10,
                "truncated": False,
                "truncation_reason": None,
                "content_hash": hashlib.sha256(b"hello").hexdigest(),
                "xml_block": '<untrusted_attachment_content part_locator="1" filename="doc.pdf" source_sha256="' + "c"*64 + '" sha256="' + "c"*64 + '" mime_type="application/pdf" materiality="supplementary" status="extracted" char_count="10" truncated="false">\nhello\n</untrusted_attachment_content>',
                "analysis_completeness": "unknown",
                "truncation_stage": "none",
                "handoff_character_count": 10,
                "analysis_character_budget": 15000,
                "source_character_count": 10,
            }],
            "prompt_content": '<untrusted_attachment_content part_locator="1" filename="doc.pdf" source_sha256="' + "c"*64 + '" sha256="' + "c"*64 + '" mime_type="application/pdf" materiality="supplementary" status="extracted" char_count="10" truncated="false">\nhello\n</untrusted_attachment_content>',
        }
        with self.assertRaises(AttachmentHandoffError):
            validate_attachment_handoff(
                handoff_with_unknown,
                mail_identity=mail_id,
                canonical_parts=handoff_with_unknown["canonical_parts"],
            )

    def test_11_orphan_coverage_fields_rejected(self) -> None:
        """Incomplete coverage blocks (orphan or missing coverage fields) are rejected fail-closed."""
        mid = "msg-orphan@example.com"
        sha = "a" * 64
        att_id = compute_attachment_id(mid, "1", sha)
        base_entry = {
            "attachment_id": att_id,
            "message_id": mid,
            "account": "primary",
            "folder": "INBOX",
            "part_locator": "1",
            "clean_filename": "orphan.pdf",
            "mime_type": "application/pdf",
            "sha256": sha,
            "size_bytes": 100,
            "run_id": "run-2026-09-17-011",
            "quarantine_path": "data/mail-desk/attachments/run-2026-09-17-011/orphan.pdf",
            "analysis_status": "completed",
            "analyzed_at": utc_now_iso(),
            "contract_version": "v1.0",
            "lifecycle_state": "quarantined",
            "disposition_ref": None,
        }

        # 1. Only truncation_reason provided (no analysis_completeness or other coverage fields)
        entry_orphan1 = dict(base_entry)
        entry_orphan1["truncation_reason"] = "max_chars_exceeded"
        with self.assertRaises(AttachmentIndexSchemaError):
            validate_quarantine_index_entry(entry_orphan1)

        # 2. Only analysis_completeness provided (missing 5 other fields)
        entry_orphan2 = dict(base_entry)
        entry_orphan2["analysis_completeness"] = "truncated"
        with self.assertRaises(AttachmentIndexSchemaError):
            validate_quarantine_index_entry(entry_orphan2)

        # 3. Missing core coverage field (e.g. handoff_character_count missing)
        entry_orphan3 = dict(base_entry)
        entry_orphan3["analysis_completeness"] = "truncated"
        entry_orphan3["truncation_reason"] = "max_chars_exceeded"
        entry_orphan3["truncation_stage"] = "extraction"
        entry_orphan3["analysis_character_budget"] = 100
        # handoff_character_count omitted
        with self.assertRaises(AttachmentIndexSchemaError):
            validate_quarantine_index_entry(entry_orphan3)

    def test_12_writer_rejects_unknown_fields(self) -> None:
        """save_quarantine_index_atomic rejects unknown fields fail-closed via validate_quarantine_index_entry."""
        index_path = self.data_dir / INDEX_FILENAME
        mid = "msg-writer-unknown@example.com"
        sha = "9" * 64
        att_id = compute_attachment_id(mid, "1", sha)
        valid_entry = {
            "attachment_id": att_id,
            "message_id": mid,
            "account": "primary",
            "folder": "INBOX",
            "part_locator": "1",
            "clean_filename": "test.pdf",
            "mime_type": "application/pdf",
            "sha256": sha,
            "size_bytes": 100,
            "run_id": "run-2026-09-17-012",
            "quarantine_path": "data/mail-desk/attachments/run-2026-09-17-012/test.pdf",
            "analysis_status": "completed",
            "analyzed_at": utc_now_iso(),
            "contract_version": "v1.0",
            "lifecycle_state": "quarantined",
            "disposition_ref": None,
        }

        entry_with_unknown_field = dict(valid_entry)
        entry_with_unknown_field["unexpected_bogus_field"] = "not_allowed"

        index_doc = {
            "schema_version": 1,
            "updated_at": utc_now_iso(),
            "items": {
                att_id: entry_with_unknown_field,
            },
        }

        with self.assertRaises(AttachmentIndexSchemaError):
            save_quarantine_index_atomic(index_path, index_doc)

    def test_13_historical_entries_remain_byte_identical_across_operations(self) -> None:
        """Historical Schema 1 entries without coverage fields remain strictly byte-identical on disk."""
        index_path = self.data_dir / INDEX_FILENAME
        mid = "msg-historical@example.com"
        sha = "8" * 64
        att_id = compute_attachment_id(mid, "1", sha)
        historical_entry = {
            "attachment_id": att_id,
            "message_id": mid,
            "account": "primary",
            "folder": "INBOX",
            "original_folder": "INBOX",
            "part_locator": "1",
            "clean_filename": "historical.pdf",
            "filename": "historical.pdf",
            "mime_type": "application/pdf",
            "effective_mime_type": "application/pdf",
            "sha256": sha,
            "size_bytes": 500,
            "run_id": "run-historical-001",
            "quarantine_path": "data/mail-desk/attachments/run-historical-001/historical.pdf",
            "analysis_status": "completed",
            "analyzed_at": "2026-09-10T12:00:00Z",
            "contract_version": "v1.0",
            "lifecycle_state": "quarantined",
            "disposition_ref": None,
        }

        index_doc = {
            "schema_version": 1,
            "updated_at": "2026-09-10T12:00:00Z",
            "items": {
                att_id: historical_entry,
            },
        }
        exact_bytes = json.dumps(index_doc, indent=2).encode("utf-8") + b"\n"
        index_path.write_bytes(exact_bytes)

        # 1. Lookup
        found = lookup_quarantine_entry(index_path, attachment_id=att_id)
        self.assertIsNotNone(found)
        self.assertEqual(found["analysis_completeness"], "unknown")
        self.assertEqual(index_path.read_bytes(), exact_bytes)

        # 2. Stats
        stats = get_quarantine_index_stats(index_path)
        self.assertEqual(stats["total_indexed_items"], 1)
        self.assertEqual(index_path.read_bytes(), exact_bytes)

        # 3. Load
        loaded = load_quarantine_index(index_path)
        self.assertEqual(loaded["schema_version"], 1)
        self.assertEqual(index_path.read_bytes(), exact_bytes)

    def test_14_coverage_without_source_character_count_accepted(self) -> None:
        """Coverage with 5 core fields is accepted when source_character_count is omitted or null."""
        mid = "msg-no-src-chars@example.com"
        sha = "7" * 64
        att_id = compute_attachment_id(mid, "1", sha)
        entry_omitted = {
            "attachment_id": att_id,
            "message_id": mid,
            "account": "primary",
            "folder": "INBOX",
            "part_locator": "1",
            "clean_filename": "doc.pdf",
            "mime_type": "application/pdf",
            "sha256": sha,
            "size_bytes": 100,
            "run_id": "run-2026-09-17-014",
            "quarantine_path": "data/mail-desk/attachments/run-2026-09-17-014/doc.pdf",
            "analysis_status": "completed",
            "analyzed_at": utc_now_iso(),
            "contract_version": "v1.0",
            "lifecycle_state": "quarantined",
            "disposition_ref": None,
            "analysis_completeness": "full",
            "truncation_reason": None,
            "truncation_stage": "none",
            "handoff_character_count": 100,
            "analysis_character_budget": 15000,
            # source_character_count is omitted
        }
        res_omitted = validate_quarantine_index_entry(entry_omitted)
        self.assertIsNone(res_omitted["source_character_count"])
        self.assertEqual(res_omitted["analysis_completeness"], "full")

        entry_null = dict(entry_omitted)
        entry_null["source_character_count"] = None
        res_null = validate_quarantine_index_entry(entry_null)
        self.assertIsNone(res_null["source_character_count"])

        entry_with_val = dict(entry_omitted)
        entry_with_val["source_character_count"] = 500
        res_val = validate_quarantine_index_entry(entry_with_val)
        self.assertEqual(res_val["source_character_count"], 500)

    def test_15_handoff_explicit_signature_regression(self) -> None:
        """build_attachment_analysis_handoff enforces typed signature and rejects unknown kwargs/args."""
        mail_id = {
            "account": "primary",
            "message_id": "<msg-sig@example.com>",
            "folder": "INBOX",
            "envelope_id": "env-sig",
        }
        # 1. Calling with proper signature succeeds
        res = build_attachment_analysis_handoff(mail_id, [])
        self.assertEqual(res["total_attachments"], 0)

        # 2. Unknown keyword arguments raise TypeError (no **kwargs masking caller bugs)
        with self.assertRaises(TypeError):
            build_attachment_analysis_handoff(mail_id, [], unexpected_keyword_arg=True)  # type: ignore

        # 3. Non-mapping mail_identity raises AttachmentHandoffError
        with self.assertRaises(AttachmentHandoffError):
            build_attachment_analysis_handoff("not_a_mapping", [])  # type: ignore


if __name__ == "__main__":
    unittest.main()
