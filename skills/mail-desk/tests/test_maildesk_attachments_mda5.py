"""Hermetic tests for FR-08 MD-A5: Catalog- and filemap-backed attachment filing proposal.

Harden MD-A5 against real MD-A2, MD-A4, Classifier, Catalog, and Cloud-Atlas contracts:
1. Composite MD-A2 contract (manifest operation, fetch result, MD-A1 candidate, quarantine evidence)
2. Free attachment dictionaries fail closed (never produce 'proposed')
3. Missing or manipulated quarantine evidence fails closed (no self-attestation)
4. Review-hash and approval-receipt drift detection (fail closed)
5. Operation, candidate, and result drift detection (fail closed)
6. Canonical Cloud-Atlas filemap schema validation via gen_filemap.validate_filemap
   ($schema, project_title, scan_dir containment, entry required keys, 64-hex SHA-256)
7. MD-A4 handoff validation and hash binding (detects drift, requires canonical parts)
8. Real decision schema: kind: "topic" / "project" with subtopic and event scalars
9. Event cloud_storage selector and inheritance rules
10. Elimination of decision.target_dir bypass
11. Rejection of generic 'filemap' fallback key
12. Strict workspace containment (absolute paths and traversal in output_json rejected)
13. Filemap freshness & staleness checks
14. Deduplication (already_present on identical SHA-256)
15. Collision detection (collision_detected on identical name but different SHA-256)
16. Multiple storages and archive/read-only storages (storage_review_required)
17. Deterministic candidate hash binding
18. Strict read-only invariant (zero file mutations)
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, mock_open, patch

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

from core.common import normalize_message_id  # noqa: E402
from core.attachment_policy import sanitize_attachment_filename  # noqa: E402
from core.attachments import PROVENANCE_RFC822  # noqa: E402
from core.attachment_filing import (  # noqa: E402
    FILEMAP_SCHEMA_URI,
    InvalidMDA2FetchError,
    PROMOTION_STATUS_PENDING_HUMAN_REVIEW,
    STATUS_ALREADY_PRESENT,
    STATUS_COLLISION_DETECTED,
    STATUS_DIRECTORY_REVIEW_REQUIRED,
    STATUS_NOT_CONFIGURED,
    STATUS_PROPOSED,
    STATUS_STORAGE_REVIEW_REQUIRED,
    compute_candidate_hash,
    propose_attachment_filing,
    resolve_catalog_storage,
    validate_cloud_atlas_filemap,
    validate_filemap_freshness,
    validate_mda2_attachment,
)
from core.attachment_handoff import HandoffDriftError, build_attachment_analysis_handoff  # noqa: E402
from core.attachment_fetch import compute_review_hash  # noqa: E402

def build_test_mda2_composite(
    *,
    account: str = "primary",
    message_id: str = "<msg-501@example.org>",
    folder: str = "INBOX",
    envelope_id: str = "501",
    part_locator: str = "2",
    filename: str = "minutes_2026.pdf",
    sha256: str = "a" * 64,
    size_bytes: int = 1024,
    mime_type: str = "application/pdf",
    run_id: str = "run_20260914_test",
    status: str = "fetched",
    receipt_id: str = "rec-501",
    approved_by: str = "martin",
    approved_at: str = "2026-09-14T09:00:00Z",
) -> dict[str, Any]:
    """Test helper constructing a valid composite MD-A2 contract (operation, result, candidate)."""
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
        "provenance": PROVENANCE_RFC822,
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
        "receipt_id": receipt_id,
        "request_hash": rev_hash,
        "approved_at": approved_at,
        "approved_by": approved_by,
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


def make_filemap(
    *,
    scope: str = "project",
    storage_id: str = "primary",
    project: str = "pilot-proj",
    project_title: str = "Pilot Project",
    scan_dir: str = "data/cloud/PILOT",
    output_dir: str = "memory/cloud/projects/pilot-proj",
    updated_at: str = "2026-09-14 09:30:00",
    files: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Helper to build a valid Cloud-Atlas compliant filemap dictionary."""
    if files is None:
        files = {
            f"{scan_dir}/01_Admin/Correspondence/existing_doc.pdf": {
                "version": "1",
                "mtime": "2026-09-14 08:00:00",
                "size": "100 KB",
                "sha256": "f" * 64,
                "description": "Existing correspondence",
            }
        }
    return {
        "$schema": FILEMAP_SCHEMA_URI,
        "schema_version": 1,
        "kind": "cloud-filemap",
        "scope": scope,
        "storage_id": storage_id,
        "project": project,
        "project_title": project_title,
        "scan_dir": scan_dir,
        "output_dir": output_dir,
        "updated_at": updated_at,
        "files": files,
    }


class TestMailDeskAttachmentsMDA5(unittest.TestCase):
    """Hermetic test suite for hardened MD-A5 attachment filing proposals."""

    def setUp(self) -> None:
        self.now = datetime(2026, 9, 14, 10, 0, 0, tzinfo=timezone.utc)
        self.fresh_timestamp = "2026-09-14 09:30:00"

        # Canonical Catalog Fixtures
        self.mock_catalogs = {
            "projects": [
                {
                    "id": "pilot-proj",
                    "title": "Pilot Project",
                    "cloud_sync": {
                        "primary": {
                            "scan_dir": "data/cloud/PILOT",
                            "target_dir": "01_Admin/Correspondence",
                            "output_json": "memory/cloud/projects/pilot-proj/filemap.json",
                            "output_dir": "memory/cloud/projects/pilot-proj",
                        }
                    },
                },
                {
                    "id": "multi-storage-proj",
                    "title": "Multi Storage Project",
                    "cloud_sync": {
                        "drive": {
                            "scan_dir": "data/cloud/DRIVE",
                            "target_dir": "docs",
                            "output_json": "memory/cloud/projects/multi/filemap-drive.json",
                            "output_dir": "memory/cloud/projects/multi",
                        },
                        "nextcloud": {
                            "scan_dir": "data/cloud/NC",
                            "target_dir": "docs",
                            "output_json": "memory/cloud/projects/multi/filemap-nc.json",
                            "output_dir": "memory/cloud/projects/multi",
                        },
                    },
                },
                {
                    "id": "archive-storage-proj",
                    "title": "Archive Storage Project",
                    "cloud_sync": {
                        "archive_main": {
                            "archive": True,
                            "scan_dir": "data/cloud/ARCHIVE",
                            "target_dir": "archive_docs",
                            "output_json": "memory/cloud/projects/archive/filemap.json",
                            "output_dir": "memory/cloud/projects/archive",
                        }
                    },
                },
                {
                    "id": "no-cloud-proj",
                    "title": "Project Without Cloud",
                },
            ],
            "topics": [
                {
                    "id": "hr-topic",
                    "title": "Human Resources",
                    "cloud_sync": {
                        "hr_cloud": {
                            "scan_dir": "data/cloud/HR",
                            "target_dir": "Recruiting/Applications",
                            "output_json": "memory/cloud/topics/hr-topic/filemap.json",
                            "output_dir": "memory/cloud/topics/hr-topic",
                        }
                    },
                    "subtopics": [
                        {
                            "id": "recruiting",
                            "title": "Recruiting",
                        },
                        {
                            "id": "payroll",
                            "title": "Payroll",
                            "cloud_sync": {
                                "payroll_cloud": {
                                    "scan_dir": "data/cloud/PAYROLL",
                                    "target_dir": "Monthly/Reports",
                                    "output_json": "memory/cloud/topics/hr-topic/payroll/filemap.json",
                                    "output_dir": "memory/cloud/topics/hr-topic/payroll",
                                }
                            },
                        },
                        {
                            "id": "events_sub",
                            "title": "Events Subtopic",
                            "cloud_sync": {
                                "payroll_cloud": {
                                    "scan_dir": "data/cloud/PAYROLL",
                                    "target_dir": "Monthly/Reports",
                                    "output_json": "memory/cloud/topics/hr-topic/payroll/filemap.json",
                                    "output_dir": "memory/cloud/topics/hr-topic/payroll",
                                }
                            },
                            "events": [
                                {
                                    "id": "conf_subtopic_selector",
                                    "title": "Annual Conference (Subtopic Storage)",
                                    "cloud_storage": {
                                        "scope": "subtopic",
                                        "storage_id": "payroll_cloud",
                                    },
                                },
                                {
                                    "id": "conf_topic_selector",
                                    "title": "Annual Conference (Topic Storage)",
                                    "cloud_storage": {
                                        "scope": "topic",
                                        "storage_id": "hr_cloud",
                                    },
                                },
                                {
                                    "id": "conf_invalid_selector",
                                    "title": "Annual Conference (Invalid Selector)",
                                    "cloud_storage": {
                                        "scope": "subtopic",
                                        "storage_id": "nonexistent_storage",
                                    },
                                },
                                {
                                    "id": "conf_inherited",
                                    "title": "Annual Conference (Inherited Storage)",
                                },
                            ],
                        },
                    ],
                },
                {
                    "id": "topic-no-cloud",
                    "title": "No Cloud Topic",
                    "subtopics": [
                        {
                            "id": "general",
                            "title": "General",
                            "events": [
                                {
                                    "id": "workshop_2026",
                                    "title": "Workshop 2026",
                                }
                            ],
                        }
                    ],
                },
            ],
        }

        # Valid MD-A2 Composite Attachment Input
        self.sample_mda2_attachment = build_test_mda2_composite(
            account="primary",
            message_id="<msg-501@example.org>",
            folder="INBOX",
            envelope_id="501",
            part_locator="2",
            filename="minutes_2026.pdf",
            sha256="a" * 64,
            run_id="run_20260914_test",
        )

        self.manifest_account = "primary"

        # Canonical Cloud-Atlas Filemap Fixture
        self.mock_filemap = make_filemap(
            scope="project",
            storage_id="primary",
            project="pilot-proj",
            project_title="Pilot Project",
            scan_dir="data/cloud/PILOT",
            output_dir="memory/cloud/projects/pilot-proj",
            updated_at=self.fresh_timestamp,
        )

    # ==========================================================================
    # 1. Project Filing Proposal & MD-A2 Contract
    # ==========================================================================

    def test_1_project_filing_proposal(self) -> None:
        """Project with unambiguous cloud storage and occupied directory produces 'proposed' candidate."""
        decision = {"kind": "project", "id": "pilot-proj"}
        candidate = propose_attachment_filing(
            self.sample_mda2_attachment,
            decision,
            self.mock_catalogs,
            manifest_account=self.manifest_account,
            filemaps={"primary": self.mock_filemap},
            current_time=self.now,
        )
        self.assertEqual(candidate["status"], STATUS_PROPOSED)
        self.assertEqual(candidate["promotion_status"], PROMOTION_STATUS_PENDING_HUMAN_REVIEW)
        self.assertEqual(candidate["destination"]["storage_id"], "primary")
        self.assertEqual(candidate["destination"]["target_relative_path"], "01_Admin/Correspondence/minutes_2026.pdf")
        self.assertEqual(candidate["source"]["original_filename"], "minutes_2026.pdf")
        self.assertEqual(candidate["destination"]["target_filename"], "minutes_2026.pdf")
        self.assertFalse(candidate["dedupe"]["already_present"])
        self.assertFalse(candidate["dedupe"]["collision_detected"])
        self.assertEqual(len(candidate["candidate_hash"]), 64)

    # ==========================================================================
    # 2. Real Decision Schema: Topic, Subtopic, Event
    # ==========================================================================

    def test_2_topic_filing_proposal(self) -> None:
        """Topic with cloud storage produces 'proposed' candidate."""
        decision = {"kind": "topic", "id": "hr-topic"}
        filemap = make_filemap(
            scope="topic",
            storage_id="hr_cloud",
            project="hr-topic",
            project_title="Human Resources",
            scan_dir="data/cloud/HR",
            output_dir="memory/cloud/topics/hr-topic",
            files={
                "data/cloud/HR/Recruiting/Applications/resume.pdf": {
                    "sha256": "e" * 64,
                    "mtime": "2026-09-14 08:00:00",
                    "size": "50 KB",
                    "version": "1",
                    "description": "Resume",
                }
            },
        )
        candidate = propose_attachment_filing(
            self.sample_mda2_attachment,
            decision,
            self.mock_catalogs,
            manifest_account=self.manifest_account,
            filemaps={"hr_cloud": filemap},
            current_time=self.now,
        )
        self.assertEqual(candidate["status"], STATUS_PROPOSED)
        self.assertEqual(candidate["destination"]["storage_id"], "hr_cloud")
        self.assertEqual(candidate["destination"]["target_relative_path"], "Recruiting/Applications/minutes_2026.pdf")

    def test_3_subtopic_filing_proposal_inherited_and_explicit(self) -> None:
        """Subtopic inherits parent topic storage when not explicitly specified, or uses explicit."""
        # 1. Inherited storage (subtopic='recruiting' on topic='hr-topic')
        decision_inherited = {"kind": "topic", "id": "hr-topic", "subtopic": "recruiting"}
        filemap_hr = make_filemap(
            scope="topic",
            storage_id="hr_cloud",
            project="hr-topic",
            project_title="Human Resources",
            scan_dir="data/cloud/HR",
            output_dir="memory/cloud/topics/hr-topic",
            files={
                "data/cloud/HR/Recruiting/Applications/app.pdf": {
                    "version": "1",
                    "mtime": "2026-09-14 08:00:00",
                    "size": "50 KB",
                    "sha256": "1" * 64,
                    "description": "Application",
                }
            },
        )
        c1 = propose_attachment_filing(
            self.sample_mda2_attachment,
            decision_inherited,
            self.mock_catalogs,
            manifest_account=self.manifest_account,
            filemaps={"hr_cloud": filemap_hr},
            current_time=self.now,
        )
        self.assertEqual(c1["status"], STATUS_PROPOSED)
        self.assertEqual(c1["destination"]["storage_id"], "hr_cloud")

        # 2. Explicit subtopic storage (subtopic='payroll' on topic='hr-topic')
        decision_explicit = {"kind": "topic", "id": "hr-topic", "subtopic": "payroll"}
        filemap_payroll = make_filemap(
            scope="topic",
            storage_id="payroll_cloud",
            project="hr-topic",
            project_title="Payroll",
            scan_dir="data/cloud/PAYROLL",
            output_dir="memory/cloud/topics/hr-topic/payroll",
            files={
                "data/cloud/PAYROLL/Monthly/Reports/sep.pdf": {
                    "version": "1",
                    "mtime": "2026-09-14 08:00:00",
                    "size": "60 KB",
                    "sha256": "2" * 64,
                    "description": "September Report",
                }
            },
        )
        c2 = propose_attachment_filing(
            self.sample_mda2_attachment,
            decision_explicit,
            self.mock_catalogs,
            manifest_account=self.manifest_account,
            filemaps={"payroll_cloud": filemap_payroll},
            current_time=self.now,
        )
        self.assertEqual(c2["status"], STATUS_PROPOSED)
        self.assertEqual(c2["destination"]["storage_id"], "payroll_cloud")
        self.assertEqual(c2["destination"]["target_relative_path"], "Monthly/Reports/minutes_2026.pdf")

    def test_4_event_resolution_and_cloud_storage_selectors(self) -> None:
        """Events resolve cloud storage via explicit cloud_storage selector or inheritance."""
        # 1. Event with explicit subtopic selector
        d1 = {"kind": "topic", "id": "hr-topic", "subtopic": "events_sub", "event": "conf_subtopic_selector"}
        sync1, err1, st1 = resolve_catalog_storage(d1, self.mock_catalogs)
        self.assertIsNotNone(sync1)
        self.assertIn("payroll_cloud", sync1)

        # 2. Event with explicit topic selector
        d2 = {"kind": "topic", "id": "hr-topic", "subtopic": "events_sub", "event": "conf_topic_selector"}
        sync2, err2, st2 = resolve_catalog_storage(d2, self.mock_catalogs)
        self.assertIsNotNone(sync2)
        self.assertIn("hr_cloud", sync2)

        # 3. Event with invalid selector (storage nonexistent in declared scope)
        d3 = {"kind": "topic", "id": "hr-topic", "subtopic": "events_sub", "event": "conf_invalid_selector"}
        sync3, err3, st3 = resolve_catalog_storage(d3, self.mock_catalogs)
        self.assertIsNone(sync3)
        self.assertEqual(st3, STATUS_STORAGE_REVIEW_REQUIRED)

        # 4. Event without explicit selector (inherits from subtopic)
        d4 = {"kind": "topic", "id": "hr-topic", "subtopic": "events_sub", "event": "conf_inherited"}
        sync4, err4, st4 = resolve_catalog_storage(d4, self.mock_catalogs)
        self.assertIsNotNone(sync4)
        self.assertIn("payroll_cloud", sync4)

    # ==========================================================================
    # 3. Adversarial MD-A2 Composite Contract & Fail-Closed Tests
    # ==========================================================================

    def test_5_free_attachment_dictionary_fails_closed(self) -> None:
        """Free flat dictionary without verified composite contract is rejected fail-closed."""
        free_dict = {
            "account": "primary",
            "message_id": "<msg-501@example.org>",
            "folder": "INBOX",
            "envelope_id": "501",
            "part_locator": "2",
            "filename": "minutes_2026.pdf",
            "sha256": "a" * 64,
            "status": "fetched",
            "quarantine_path": "data/mail-desk/attachments/run/minutes_2026.pdf",
        }
        with self.assertRaises(InvalidMDA2FetchError):
            validate_mda2_attachment(free_dict, manifest_account=self.manifest_account)

        with self.assertRaises(InvalidMDA2FetchError):
            propose_attachment_filing(free_dict, {"kind": "project", "id": "pilot-proj"}, self.mock_catalogs, manifest_account=self.manifest_account)

    def test_6_operation_action_must_be_attachment_fetch(self) -> None:
        """MD-A2 operation action must be exactly 'attachment_fetch'."""
        base = build_test_mda2_composite()

        # 1. Action is 'other_action'
        bad_action1 = dict(base, operation=dict(base["operation"], action="other_action"))
        with self.assertRaises(InvalidMDA2FetchError):
            validate_mda2_attachment(bad_action1, manifest_account=self.manifest_account)

        # 2. Action is missing or None
        bad_action2 = dict(base, operation=dict(base["operation"], action=None))
        with self.assertRaises(InvalidMDA2FetchError):
            validate_mda2_attachment(bad_action2, manifest_account=self.manifest_account)

        # 3. Action is empty string
        bad_action3 = dict(base, operation=dict(base["operation"], action=""))
        with self.assertRaises(InvalidMDA2FetchError):
            validate_mda2_attachment(bad_action3, manifest_account=self.manifest_account)

    def test_7_exact_quarantine_relative_path_enforced(self) -> None:
        """MD-A2 relative_path must be exactly data/mail-desk/attachments/<run_id>/<sanitized_filename>."""
        base = build_test_mda2_composite()
        run_id = base["operation"]["run_id"]
        clean_fn = sanitize_attachment_filename(base["candidate"]["filename"])

        # 1. Non-quarantine directory prefix
        bad_path1 = dict(base, result=dict(base["result"], relative_path="data/other/path.pdf"))
        with self.assertRaises(InvalidMDA2FetchError):
            validate_mda2_attachment(bad_path1, manifest_account=self.manifest_account)

        # 2. Wrong filename in relative path
        bad_path2 = dict(base, result=dict(base["result"], relative_path=f"data/mail-desk/attachments/{run_id}/wrong_name.pdf"))
        with self.assertRaises(InvalidMDA2FetchError):
            validate_mda2_attachment(bad_path2, manifest_account=self.manifest_account)

        # 3. Wrong run_id in relative path
        bad_path3 = dict(base, result=dict(base["result"], relative_path=f"data/mail-desk/attachments/other_run/{clean_fn}"))
        with self.assertRaises(InvalidMDA2FetchError):
            validate_mda2_attachment(bad_path3, manifest_account=self.manifest_account)

        # 4. Traversal attempt
        bad_path4 = dict(base, result=dict(base["result"], relative_path=f"data/mail-desk/attachments/{run_id}/../escape.pdf"))
        with self.assertRaises(InvalidMDA2FetchError):
            validate_mda2_attachment(bad_path4, manifest_account=self.manifest_account)

    def test_8_receipt_and_review_hash_drift_fails_closed(self) -> None:
        """Operation review_hash drift or missing/drifted approval_receipt fails closed."""
        base = build_test_mda2_composite()

        # 1. Tampered review_hash in operation
        bad_op_hash = dict(base, operation=dict(base["operation"], review_hash="0" * 64))
        with self.assertRaises(InvalidMDA2FetchError):
            validate_mda2_attachment(bad_op_hash, manifest_account=self.manifest_account)

        # 2. Missing approval receipt
        no_receipt = dict(base, operation=dict(base["operation"], approval_receipt=None))
        with self.assertRaises(InvalidMDA2FetchError):
            validate_mda2_attachment(no_receipt, manifest_account=self.manifest_account)

        # 3. Drifted approval receipt request_hash
        bad_receipt = dict(base, operation=dict(
            base["operation"],
            approval_receipt=dict(base["operation"]["approval_receipt"], request_hash="1" * 64),
        ))
        with self.assertRaises(InvalidMDA2FetchError):
            validate_mda2_attachment(bad_receipt, manifest_account=self.manifest_account)

        # 4. Consistent forged review_hash and receipt (matching each other but drifting from candidate)
        forged_hash = "9" * 64
        bad_forged = dict(base, operation=dict(
            base["operation"],
            review_hash=forged_hash,
            approval_receipt=dict(base["operation"]["approval_receipt"], request_hash=forged_hash),
        ))
        with self.assertRaises(InvalidMDA2FetchError):
            validate_mda2_attachment(bad_forged, manifest_account=self.manifest_account)

        # 5. Empty review_hash in operation
        empty_rev = dict(base, operation=dict(base["operation"], review_hash=""))
        with self.assertRaises(InvalidMDA2FetchError):
            validate_mda2_attachment(empty_rev, manifest_account=self.manifest_account)

    def test_9_operation_candidate_result_drift_fails_closed(self) -> None:
        """Drifts between operation, candidate, and result fail closed."""
        base = build_test_mda2_composite()

        # 1. Account drift
        drift_acc = dict(base, operation=dict(base["operation"], account="other_acc"))
        with self.assertRaises(InvalidMDA2FetchError):
            validate_mda2_attachment(drift_acc, manifest_account=self.manifest_account)

        # 2. Message-ID drift
        drift_mid = dict(base, operation=dict(base["operation"], message_id="<other@example.org>"))
        with self.assertRaises(InvalidMDA2FetchError):
            validate_mda2_attachment(drift_mid, manifest_account=self.manifest_account)

        # 3. Folder drift
        drift_fld = dict(base, operation=dict(base["operation"], folder="SPAM"))
        with self.assertRaises(InvalidMDA2FetchError):
            validate_mda2_attachment(drift_fld, manifest_account=self.manifest_account)

        # 4. Envelope-ID drift
        drift_eid = dict(base, operation=dict(base["operation"], envelope_id="999"))
        with self.assertRaises(InvalidMDA2FetchError):
            validate_mda2_attachment(drift_eid, manifest_account=self.manifest_account)

        # 5. Part-Locator drift
        drift_loc = dict(base, operation=dict(base["operation"], part_locator="3"))
        with self.assertRaises(InvalidMDA2FetchError):
            validate_mda2_attachment(drift_loc, manifest_account=self.manifest_account)

        # 6. Filename drift in result
        drift_fn = dict(base, result=dict(base["result"], filename="wrong_name.pdf"))
        with self.assertRaises(InvalidMDA2FetchError):
            validate_mda2_attachment(drift_fn, manifest_account=self.manifest_account)

        # 7. Hash drift in result
        drift_sha = dict(base, result=dict(base["result"], fetch_sha256="c" * 64))
        with self.assertRaises(InvalidMDA2FetchError):
            validate_mda2_attachment(drift_sha, manifest_account=self.manifest_account)

        # 8. Run-ID drift
        drift_run = dict(base, result=dict(base["result"], run_id="run_different"))
        with self.assertRaises(InvalidMDA2FetchError):
            validate_mda2_attachment(drift_run, manifest_account=self.manifest_account)

        # 9. Result with error
        err_res = dict(base, result=dict(base["result"], error="fetch failed"))
        with self.assertRaises(InvalidMDA2FetchError):
            validate_mda2_attachment(err_res, manifest_account=self.manifest_account)

        # 10. Result with unverified status
        bad_status = dict(base, result=dict(base["result"], status="available"))
        with self.assertRaises(InvalidMDA2FetchError):
            validate_mda2_attachment(bad_status, manifest_account=self.manifest_account)

    # ==========================================================================
    # 4. MD-A4 Handoff Validation & Binding
    # ==========================================================================

    def test_10_mda4_handoff_validation_and_binding(self) -> None:
        """Valid MD-A4 handoff is re-validated and binds handoff_hash into candidate."""
        decision = {"kind": "project", "id": "pilot-proj"}
        canonical_part = {
            "part_locator": "2",
            "filename": "minutes_2026.pdf",
            "sha256": "a" * 64,
            "mime_type": "application/pdf",
            "provenance": "rfc822_mime_inspection",
        }
        item = {
            "part_locator": "2",
            "filename": "minutes_2026.pdf",
            "source_sha256": "a" * 64,
            "mime_type": "application/pdf",
            "materiality": "required_for_decision",
            "status": "extracted",
            "quality": "high",
            "text": "Minutes text",
        }
        mail_identity = {
            "account": "primary",
            "message_id": "<msg-501@example.org>",
            "folder": "INBOX",
            "envelope_id": "501",
        }
        handoff = build_attachment_analysis_handoff(
            mail_identity,
            [item],
            decision=decision,
            canonical_parts=[canonical_part],
        )

        candidate = propose_attachment_filing(
            self.sample_mda2_attachment,
            decision,
            self.mock_catalogs,
            manifest_account=self.manifest_account,
            filemaps={"primary": self.mock_filemap},
            handoff=handoff,
            canonical_parts=[canonical_part],
            current_time=self.now,
        )
        self.assertEqual(candidate["status"], STATUS_PROPOSED)
        self.assertEqual(candidate["handoff_hash"], handoff["handoff_hash"])
        self.assertIsNotNone(candidate["handoff_evidence"])
        self.assertEqual(candidate["handoff_evidence"]["items_count"], 1)

    def test_11_mda4_handoff_drift_fails_closed(self) -> None:
        """Manipulated handoff or mail identity drift causes fail closed."""
        decision = {"kind": "project", "id": "pilot-proj"}
        canonical_part = {
            "part_locator": "2",
            "filename": "minutes_2026.pdf",
            "sha256": "a" * 64,
            "mime_type": "application/pdf",
            "provenance": "rfc822_mime_inspection",
        }
        item = {
            "part_locator": "2",
            "filename": "minutes_2026.pdf",
            "source_sha256": "a" * 64,
            "mime_type": "application/pdf",
            "materiality": "required_for_decision",
            "status": "extracted",
            "quality": "high",
            "text": "Minutes text",
        }
        mail_identity = {
            "account": "primary",
            "message_id": "<msg-501@example.org>",
            "folder": "INBOX",
            "envelope_id": "501",
        }
        handoff = build_attachment_analysis_handoff(
            mail_identity,
            [item],
            decision=decision,
            canonical_parts=[canonical_part],
        )

        # Tamper with handoff hash
        tampered_handoff = dict(handoff, handoff_hash="b" * 64)
        with self.assertRaises(HandoffDriftError):
            propose_attachment_filing(
                self.sample_mda2_attachment,
                decision,
                self.mock_catalogs,
                manifest_account=self.manifest_account,
                filemaps={"primary": self.mock_filemap},
                handoff=tampered_handoff,
                canonical_parts=[canonical_part],
                current_time=self.now,
            )

    def test_12_handoff_without_canonical_parts_fails_closed(self) -> None:
        """Handoff with items provided without canonical_parts fails closed."""
        decision = {"kind": "project", "id": "pilot-proj"}
        canonical_part = {
            "part_locator": "2",
            "filename": "minutes_2026.pdf",
            "sha256": "a" * 64,
            "mime_type": "application/pdf",
            "provenance": "rfc822_mime_inspection",
        }
        item = {
            "part_locator": "2",
            "filename": "minutes_2026.pdf",
            "source_sha256": "a" * 64,
            "mime_type": "application/pdf",
            "materiality": "required_for_decision",
            "status": "extracted",
            "quality": "high",
            "text": "Minutes text",
        }
        mail_identity = {
            "account": "primary",
            "message_id": "<msg-501@example.org>",
            "folder": "INBOX",
            "envelope_id": "501",
        }
        handoff = build_attachment_analysis_handoff(
            mail_identity,
            [item],
            decision=decision,
            canonical_parts=[canonical_part],
        )

        with self.assertRaises(HandoffDriftError):
            propose_attachment_filing(
                self.sample_mda2_attachment,
                decision,
                self.mock_catalogs,
                manifest_account=self.manifest_account,
                filemaps={"primary": self.mock_filemap},
                handoff=handoff,
                canonical_parts=None,
                current_time=self.now,
            )

    # ==========================================================================
    # 5. Elimination of decision.target_dir
    # ==========================================================================

    def test_13_decision_target_dir_strictly_ignored(self) -> None:
        """Untrusted decision.target_dir cannot override catalog configuration."""
        decision = {
            "kind": "project",
            "id": "pilot-proj",
            "target_dir": "ATTACKER/OVERRIDE",
        }
        candidate = propose_attachment_filing(
            self.sample_mda2_attachment,
            decision,
            self.mock_catalogs,
            manifest_account=self.manifest_account,
            filemaps={"primary": self.mock_filemap},
            current_time=self.now,
        )
        self.assertEqual(candidate["status"], STATUS_PROPOSED)
        self.assertNotIn("ATTACKER", candidate["destination"]["target_dir"])
        self.assertEqual(candidate["destination"]["target_dir"], "01_Admin/Correspondence")

    # ==========================================================================
    # 6. Canonical Cloud-Atlas Filemap Schema Validation
    # ==========================================================================

    def test_14_cloud_atlas_filemap_canonical_schema_validation(self) -> None:
        """Cloud-Atlas filemap validation enforces canonical schema and rejects tampering."""
        storage_cfg = {"scan_dir": "data/cloud/PILOT", "output_dir": "memory/cloud/projects/pilot-proj"}

        # 1. Missing $schema
        bad_fm1 = make_filemap()
        del bad_fm1["$schema"]
        is_val, _, err = validate_cloud_atlas_filemap(bad_fm1, "primary", "project", "pilot-proj", storage_cfg)
        self.assertFalse(is_val)
        self.assertIn("missing required keys", err)

        # 2. Invalid $schema URI
        bad_fm2 = make_filemap()
        bad_fm2["$schema"] = "https://evil.com/fake.json"
        is_val, _, err = validate_cloud_atlas_filemap(bad_fm2, "primary", "project", "pilot-proj", storage_cfg)
        self.assertFalse(is_val)
        self.assertIn("schema", err.lower())

        # 3. Missing project_title
        bad_fm3 = make_filemap()
        del bad_fm3["project_title"]
        is_val, _, err = validate_cloud_atlas_filemap(bad_fm3, "primary", "project", "pilot-proj", storage_cfg)
        self.assertFalse(is_val)

        # 4. File key outside scan_dir
        bad_fm4 = make_filemap(files={
            "data/cloud/OTHER/escape.pdf": {
                "version": "1",
                "mtime": "2026-09-14 08:00:00",
                "size": "10 KB",
                "sha256": "c" * 64,
                "description": "Escape",
            }
        })
        is_val, _, err = validate_cloud_atlas_filemap(bad_fm4, "primary", "project", "pilot-proj", storage_cfg)
        self.assertFalse(is_val)
        self.assertIn("scan_dir", err)

        # 5. File key with traversal
        bad_fm5 = make_filemap(files={
            "data/cloud/PILOT/../escape.pdf": {
                "version": "1",
                "mtime": "2026-09-14 08:00:00",
                "size": "10 KB",
                "sha256": "c" * 64,
                "description": "Escape",
            }
        })
        is_val, _, err = validate_cloud_atlas_filemap(bad_fm5, "primary", "project", "pilot-proj", storage_cfg)
        self.assertFalse(is_val)

        # 6. File entry missing sha256
        bad_fm6 = make_filemap(files={
            "data/cloud/PILOT/doc.pdf": {
                "version": "1",
                "mtime": "2026-09-14 08:00:00",
                "size": "10 KB",
                "description": "No hash",
            }
        })
        is_val, _, err = validate_cloud_atlas_filemap(bad_fm6, "primary", "project", "pilot-proj", storage_cfg)
        self.assertFalse(is_val)

        # 7. File entry with non-64-hex SHA-256
        bad_fm7 = make_filemap(files={
            "data/cloud/PILOT/doc.pdf": {
                "version": "1",
                "mtime": "2026-09-14 08:00:00",
                "size": "10 KB",
                "sha256": "not-a-valid-hex-hash",
                "description": "Invalid hash",
            }
        })
        is_val, _, err = validate_cloud_atlas_filemap(bad_fm7, "primary", "project", "pilot-proj", storage_cfg)
        self.assertFalse(is_val)

        # 8. Unknown container key rejected
        bad_fm8 = make_filemap()
        bad_fm8["unknown_forbidden_key"] = "evil"
        is_val, _, err = validate_cloud_atlas_filemap(bad_fm8, "primary", "project", "pilot-proj", storage_cfg)
        self.assertFalse(is_val)
        self.assertIn("unknown container keys", err)

        # 9. Invalid timestamp in updated_at
        bad_fm9 = make_filemap(updated_at="not-a-timestamp")
        is_val, _, err = validate_cloud_atlas_filemap(bad_fm9, "primary", "project", "pilot-proj", storage_cfg)
        self.assertFalse(is_val)
        self.assertIn("invalid timestamp", err)

        # 10. File entry missing required version key
        bad_fm10 = make_filemap(files={
            "data/cloud/PILOT/doc.pdf": {
                "mtime": "2026-09-14 08:00:00",
                "size": "10 KB",
                "sha256": "d" * 64,
                "description": "Missing version",
            }
        })
        is_val, _, err = validate_cloud_atlas_filemap(bad_fm10, "primary", "project", "pilot-proj", storage_cfg)
        self.assertFalse(is_val)
        self.assertIn("missing keys", err)

        # 11. File entry with invalid size format
        bad_fm11 = make_filemap(files={
            "data/cloud/PILOT/doc.pdf": {
                "version": "1",
                "mtime": "2026-09-14 08:00:00",
                "size": "10000",
                "sha256": "d" * 64,
                "description": "Bad size format",
            }
        })
        is_val, _, err = validate_cloud_atlas_filemap(bad_fm11, "primary", "project", "pilot-proj", storage_cfg)
        self.assertFalse(is_val)
        self.assertIn("invalid format", err)

    def test_15_filemaps_rejects_generic_filemap_key(self) -> None:
        """In-memory filemaps dict requires exact storage_id key; generic 'filemap' key is rejected."""
        decision = {"kind": "project", "id": "pilot-proj"}
        c = propose_attachment_filing(
            self.sample_mda2_attachment,
            decision,
            self.mock_catalogs,
            manifest_account=self.manifest_account,
            filemaps={"filemap": self.mock_filemap},
            current_time=self.now,
        )
        self.assertEqual(c["status"], STATUS_STORAGE_REVIEW_REQUIRED)
        self.assertIn("generic 'filemap' fallback prohibited", c["reason"])

    # ==========================================================================
    # 7. Workspace Containment
    # ==========================================================================

    def test_16_workspace_containment_absolute_and_traversal_rejected(self) -> None:
        """Catalog storage configuration with absolute path or traversal in output_json is rejected."""
        catalogs_bad = {
            "projects": [
                {
                    "id": "bad-proj",
                    "cloud_sync": {
                        "s1": {
                            "scan_dir": "data/cloud/BAD",
                            "output_json": "/etc/passwd",
                        }
                    },
                }
            ]
        }
        d = {"kind": "project", "id": "bad-proj"}
        c = propose_attachment_filing(
            self.sample_mda2_attachment,
            d,
            catalogs_bad,
            manifest_account=self.manifest_account,
            workspace_root=Path("D:/workspace"),
        )
        self.assertEqual(c["status"], STATUS_STORAGE_REVIEW_REQUIRED)
        self.assertIn("absolute path forbidden", c["reason"])

    # ==========================================================================
    # 8. Target Directory Occupancy & Deduplication
    # ==========================================================================

    def test_17_unoccupied_target_directory(self) -> None:
        """Configured target directory not occupied in filemap returns 'directory_review_required'."""
        decision = {"kind": "project", "id": "pilot-proj"}
        empty_files_filemap = make_filemap(files={})
        c = propose_attachment_filing(
            self.sample_mda2_attachment,
            decision,
            self.mock_catalogs,
            manifest_account=self.manifest_account,
            filemaps={"primary": empty_files_filemap},
            current_time=self.now,
        )
        self.assertEqual(c["status"], STATUS_DIRECTORY_REVIEW_REQUIRED)
        self.assertIn("not established/occupied", c["reason"])

    def test_18_deduplication_already_present(self) -> None:
        """File with identical SHA-256 already present in filemap returns 'already_present'."""
        decision = {"kind": "project", "id": "pilot-proj"}
        filemap_with_same_sha = make_filemap(
            files={
                "data/cloud/PILOT/01_Admin/Correspondence/existing_doc.pdf": {
                    "sha256": "a" * 64,
                    "version": "1",
                    "mtime": "2026-09-14 08:00:00",
                    "size": "100 KB",
                    "description": "Existing minutes",
                }
            }
        )
        c = propose_attachment_filing(
            self.sample_mda2_attachment,
            decision,
            self.mock_catalogs,
            manifest_account=self.manifest_account,
            filemaps={"primary": filemap_with_same_sha},
            current_time=self.now,
        )
        self.assertEqual(c["status"], STATUS_ALREADY_PRESENT)
        self.assertTrue(c["dedupe"]["already_present"])
        self.assertFalse(c["dedupe"]["collision_detected"])

    def test_19_collision_detection(self) -> None:
        """File with same name but different SHA-256 returns 'collision_detected'."""
        decision = {"kind": "project", "id": "pilot-proj"}
        filemap_with_diff_sha = make_filemap(
            files={
                "data/cloud/PILOT/01_Admin/Correspondence/minutes_2026.pdf": {
                    "sha256": "b" * 64,
                    "version": "1",
                    "mtime": "2026-09-14 08:00:00",
                    "size": "100 KB",
                    "description": "Colliding minutes",
                }
            }
        )
        c = propose_attachment_filing(
            self.sample_mda2_attachment,
            decision,
            self.mock_catalogs,
            manifest_account=self.manifest_account,
            filemaps={"primary": filemap_with_diff_sha},
            current_time=self.now,
        )
        self.assertEqual(c["status"], STATUS_COLLISION_DETECTED)
        self.assertFalse(c["dedupe"]["already_present"])
        self.assertTrue(c["dedupe"]["collision_detected"])

    # ==========================================================================
    # 9. Storage Ambiguity & Read-Only Invariant
    # ==========================================================================

    def test_20_multiple_and_archive_storages_require_review(self) -> None:
        """Multiple storages or archive storages require human selection."""
        # 1. Multiple storages
        d_multi = {"kind": "project", "id": "multi-storage-proj"}
        c_multi = propose_attachment_filing(
            self.sample_mda2_attachment,
            d_multi,
            self.mock_catalogs,
            manifest_account=self.manifest_account,
        )
        self.assertEqual(c_multi["status"], STATUS_STORAGE_REVIEW_REQUIRED)

        # 2. Archive storage
        d_archive = {"kind": "project", "id": "archive-storage-proj"}
        c_archive = propose_attachment_filing(
            self.sample_mda2_attachment,
            d_archive,
            self.mock_catalogs,
            manifest_account=self.manifest_account,
        )
        self.assertEqual(c_archive["status"], STATUS_STORAGE_REVIEW_REQUIRED)
        self.assertIn("archive or read-only", c_archive["reason"])

    def test_21_missing_and_stale_filemap(self) -> None:
        """Missing or stale filemap triggers review requirement."""
        decision = {"kind": "project", "id": "pilot-proj"}
        stale_filemap = make_filemap(updated_at="2026-09-10 09:00:00")
        c = propose_attachment_filing(
            self.sample_mda2_attachment,
            decision,
            self.mock_catalogs,
            manifest_account=self.manifest_account,
            filemaps={"primary": stale_filemap},
            current_time=self.now,
        )
        self.assertEqual(c["status"], STATUS_STORAGE_REVIEW_REQUIRED)
        self.assertTrue(c["filemap_evidence"]["is_stale"])

    def test_22_deterministic_candidate_hash_binding(self) -> None:
        """Candidate hash is deterministic and binds all evidence."""
        decision = {"kind": "project", "id": "pilot-proj"}
        c1 = propose_attachment_filing(
            self.sample_mda2_attachment,
            decision,
            self.mock_catalogs,
            manifest_account=self.manifest_account,
            filemaps={"primary": self.mock_filemap},
            current_time=self.now,
        )
        c2 = propose_attachment_filing(
            self.sample_mda2_attachment,
            decision,
            self.mock_catalogs,
            manifest_account=self.manifest_account,
            filemaps={"primary": self.mock_filemap},
            current_time=self.now,
        )
        self.assertEqual(c1["candidate_hash"], c2["candidate_hash"])

    def test_23_strict_read_only_invariant(self) -> None:
        """Module must never modify filemaps or write files to disk."""
        decision = {"kind": "project", "id": "pilot-proj"}
        original_json = json.dumps(self.mock_filemap, sort_keys=True)
        with patch("pathlib.Path.open", side_effect=AssertionError("Disk write prohibited")):
            with patch("builtins.open", side_effect=AssertionError("Builtin open write prohibited")):
                c = propose_attachment_filing(
                    self.sample_mda2_attachment,
                    decision,
                    self.mock_catalogs,
                    manifest_account=self.manifest_account,
                    filemaps={"primary": self.mock_filemap},
                    current_time=self.now,
                )
        self.assertEqual(c["status"], STATUS_PROPOSED)
        self.assertEqual(original_json, json.dumps(self.mock_filemap, sort_keys=True))

    def test_24_target_dir_traversal_in_catalog_rejected(self) -> None:
        """Catalog target_dir with path traversal is rejected."""
        catalogs = {
            "projects": [
                {
                    "id": "traversal-proj",
                    "cloud_sync": {
                        "primary": {
                            "scan_dir": "data/cloud/TRAV",
                            "target_dir": "../escape_root",
                            "output_json": "memory/cloud/projects/traversal-proj/filemap.json",
                            "output_dir": "memory/cloud/projects/traversal-proj",
                        }
                    },
                }
            ]
        }
        fm = make_filemap(
            project="traversal-proj",
            scan_dir="data/cloud/TRAV",
            output_dir="memory/cloud/projects/traversal-proj",
        )
        d = {"kind": "project", "id": "traversal-proj"}
        c = propose_attachment_filing(
            self.sample_mda2_attachment,
            d,
            catalogs,
            manifest_account=self.manifest_account,
            filemaps={"primary": fm},
            current_time=self.now,
        )
        self.assertEqual(c["status"], STATUS_DIRECTORY_REVIEW_REQUIRED)
        self.assertIn("traversal", c["reason"].lower())

    def test_25_unknown_decision_subtopic_or_event_not_configured(self) -> None:
        """Unknown subtopic or event in decision returns not_configured."""
        d1 = {"kind": "topic", "id": "hr-topic", "subtopic": "nonexistent_sub"}
        c1 = propose_attachment_filing(self.sample_mda2_attachment, d1, self.mock_catalogs, manifest_account=self.manifest_account)
        self.assertEqual(c1["status"], STATUS_NOT_CONFIGURED)

        d2 = {"kind": "topic", "id": "hr-topic", "subtopic": "recruiting", "event": "nonexistent_ev"}
        c2 = propose_attachment_filing(self.sample_mda2_attachment, d2, self.mock_catalogs, manifest_account=self.manifest_account)
        self.assertEqual(c2["status"], STATUS_NOT_CONFIGURED)

    def test_26_filemap_scan_dir_or_output_dir_drift_rejected(self) -> None:
        """Drift between catalog scan_dir/output_dir and filemap scan_dir/output_dir fails closed."""
        decision = {"kind": "project", "id": "pilot-proj"}
        drifted_fm = make_filemap(scan_dir="data/cloud/OTHER_SCAN")
        c = propose_attachment_filing(
            self.sample_mda2_attachment,
            decision,
            self.mock_catalogs,
            manifest_account=self.manifest_account,
            filemaps={"primary": drifted_fm},
            current_time=self.now,
        )
        self.assertEqual(c["status"], STATUS_STORAGE_REVIEW_REQUIRED)
        self.assertIn("does not match", c["reason"])


    # ==========================================================================
    # 8. Physical Quarantine Evidence Verification (Read-Only)
    # ==========================================================================

    def test_27_physical_quarantine_verification_success(self) -> None:
        """Read-only verification of real .quarantine-inventory.json and disk file hash succeeds."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp_dir:
            ws_root = Path(tmp_dir)
            file_bytes = b"%PDF-1.4 test minutes content"
            file_sha = hashlib.sha256(file_bytes).hexdigest().lower()
            run_id = "run_20260914_phys"
            clean_fn = "minutes_phys.pdf"
            mid = "<msg-phys@example.org>"
            norm_mid = normalize_message_id(mid)

            # Create physical quarantine directory and file
            q_dir = ws_root / "data" / "mail-desk" / "attachments" / run_id
            q_dir.mkdir(parents=True, exist_ok=True)
            target_file = q_dir / clean_fn
            target_file.write_bytes(file_bytes)

            # Create .quarantine-inventory.json
            inv_file = q_dir / ".quarantine-inventory.json"
            inv_data = {
                "schema_version": 1,
                "messages": {
                    norm_mid: {
                        "count": 1,
                        "total_bytes": len(file_bytes),
                        "files": {
                            clean_fn: {
                                "sha256": file_sha,
                                "size_bytes": len(file_bytes),
                            }
                        },
                    }
                },
            }
            inv_file.write_text(json.dumps(inv_data), encoding="utf-8")

            comp = build_test_mda2_composite(
                message_id=mid,
                filename=clean_fn,
                sha256=file_sha,
                size_bytes=len(file_bytes),
                run_id=run_id,
            )

            # Validate with physical verification enabled
            norm_att = validate_mda2_attachment(
                comp,
                manifest_account=self.manifest_account,
                workspace_root=ws_root,
                verify_physical_evidence=True,
            )
            self.assertTrue(norm_att["quarantine_evidence"]["physical_verified"])
            self.assertEqual(norm_att["quarantine_evidence"]["sha256"], file_sha)

            # Verify through propose_attachment_filing
            decision = {"kind": "project", "id": "pilot-proj"}
            c = propose_attachment_filing(
                comp,
                decision,
                self.mock_catalogs,
                manifest_account=self.manifest_account,
                filemaps={"primary": self.mock_filemap},
                workspace_root=ws_root,
                verify_physical_evidence=True,
                current_time=self.now,
            )
            self.assertEqual(c["status"], STATUS_PROPOSED)
            self.assertTrue(c["quarantine_evidence"]["physical_verified"])

    def test_28_physical_quarantine_missing_inventory_fails_closed(self) -> None:
        """Physical verification fails closed when .quarantine-inventory.json is missing."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp_dir:
            ws_root = Path(tmp_dir)
            comp = build_test_mda2_composite(run_id="run_missing_inv")

            with self.assertRaises(InvalidMDA2FetchError) as ctx:
                validate_mda2_attachment(
                    comp,
                    manifest_account=self.manifest_account,
                    workspace_root=ws_root,
                    verify_physical_evidence=True,
                )
            self.assertIn("quarantine inventory file not found", str(ctx.exception).lower())

    def test_29_physical_quarantine_disk_hash_tampering_fails_closed(self) -> None:
        """Physical verification fails closed when file on disk does not match candidate SHA-256."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp_dir:
            ws_root = Path(tmp_dir)
            file_bytes = b"real content"
            file_sha = hashlib.sha256(file_bytes).hexdigest().lower()
            run_id = "run_disk_tamper"
            clean_fn = "tampered.pdf"
            mid = "<msg-tamper@example.org>"
            norm_mid = normalize_message_id(mid)

            q_dir = ws_root / "data" / "mail-desk" / "attachments" / run_id
            q_dir.mkdir(parents=True, exist_ok=True)
            # Write altered bytes to disk
            (q_dir / clean_fn).write_bytes(b"altered disk content")

            inv_file = q_dir / ".quarantine-inventory.json"
            inv_data = {
                "schema_version": 1,
                "messages": {
                    norm_mid: {
                        "count": 1,
                        "total_bytes": len(file_bytes),
                        "files": {
                            clean_fn: {
                                "sha256": file_sha,
                                "size_bytes": len(file_bytes),
                            }
                        },
                    }
                },
            }
            inv_file.write_text(json.dumps(inv_data), encoding="utf-8")

            comp = build_test_mda2_composite(
                message_id=mid,
                filename=clean_fn,
                sha256=file_sha,
                size_bytes=len(file_bytes),
                run_id=run_id,
            )

            with self.assertRaises(InvalidMDA2FetchError) as ctx:
                validate_mda2_attachment(
                    comp,
                    manifest_account=self.manifest_account,
                    workspace_root=ws_root,
                    verify_physical_evidence=True,
                )
            self.assertIn("sha-256 hash drift", str(ctx.exception).lower())

    def test_30_physical_quarantine_inventory_hash_drift_fails_closed(self) -> None:
        """Physical verification fails closed when .quarantine-inventory.json hash does not match candidate."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp_dir:
            ws_root = Path(tmp_dir)
            file_bytes = b"real content"
            file_sha = hashlib.sha256(file_bytes).hexdigest().lower()
            run_id = "run_inv_drift"
            clean_fn = "drift.pdf"
            mid = "<msg-drift@example.org>"
            norm_mid = normalize_message_id(mid)

            q_dir = ws_root / "data" / "mail-desk" / "attachments" / run_id
            q_dir.mkdir(parents=True, exist_ok=True)
            (q_dir / clean_fn).write_bytes(file_bytes)

            inv_file = q_dir / ".quarantine-inventory.json"
            inv_data = {
                "schema_version": 1,
                "messages": {
                    norm_mid: {
                        "count": 1,
                        "total_bytes": len(file_bytes),
                        "files": {
                            clean_fn: {
                                "sha256": "0" * 64,  # Drifted hash in inventory
                                "size_bytes": len(file_bytes),
                            }
                        },
                    }
                },
            }
            inv_file.write_text(json.dumps(inv_data), encoding="utf-8")

            comp = build_test_mda2_composite(
                message_id=mid,
                filename=clean_fn,
                sha256=file_sha,
                size_bytes=len(file_bytes),
                run_id=run_id,
            )

            with self.assertRaises(InvalidMDA2FetchError) as ctx:
                validate_mda2_attachment(
                    comp,
                    manifest_account=self.manifest_account,
                    workspace_root=ws_root,
                    verify_physical_evidence=True,
                )
            self.assertIn("inventory sha-256 drift", str(ctx.exception).lower())


    # ==========================================================================
    # 9. Real Manifest Account Boundary Tests
    # ==========================================================================

    def test_31_embedded_account_only_fails_closed(self) -> None:
        """Attachment composite containing embedded manifest_account or bound_account is rejected when keyword param is omitted."""
        # 1. Composite with embedded manifest_account
        comp_man = build_test_mda2_composite(account="primary")
        comp_man["manifest_account"] = "primary"
        with self.assertRaises(InvalidMDA2FetchError) as ctx1:
            validate_mda2_attachment(comp_man)
        self.assertIn("missing required bound manifest account", str(ctx1.exception).lower())

        # 2. Composite with embedded bound_account
        comp_bound = build_test_mda2_composite(account="primary")
        comp_bound["bound_account"] = "primary"
        with self.assertRaises(InvalidMDA2FetchError) as ctx2:
            validate_mda2_attachment(comp_bound)
        self.assertIn("missing required bound manifest account", str(ctx2.exception).lower())

        # 3. Via propose_attachment_filing without explicit account kwarg
        decision = {"kind": "project", "id": "pilot-proj"}
        with self.assertRaises(InvalidMDA2FetchError) as ctx3:
            propose_attachment_filing(
                comp_man,
                decision,
                self.mock_catalogs,
                filemaps={"primary": self.mock_filemap},
                current_time=self.now,
            )
        self.assertIn("missing required bound manifest account", str(ctx3.exception).lower())

    def test_32_explicit_account_succeeds(self) -> None:
        """Passing explicit manifest_account or bound_account keyword parameter succeeds."""
        comp = build_test_mda2_composite(account="primary")
        decision = {"kind": "project", "id": "pilot-proj"}

        # 1. manifest_account keyword parameter
        att1 = validate_mda2_attachment(comp, manifest_account="primary")
        self.assertEqual(att1["account"], "primary")
        c1 = propose_attachment_filing(
            comp,
            decision,
            self.mock_catalogs,
            manifest_account="primary",
            filemaps={"primary": self.mock_filemap},
            current_time=self.now,
        )
        self.assertEqual(c1["status"], STATUS_PROPOSED)
        self.assertEqual(c1["source"]["account"], "primary")

        # 2. bound_account keyword parameter
        att2 = validate_mda2_attachment(comp, bound_account="primary")
        self.assertEqual(att2["account"], "primary")
        c2 = propose_attachment_filing(
            comp,
            decision,
            self.mock_catalogs,
            bound_account="primary",
            filemaps={"primary": self.mock_filemap},
            current_time=self.now,
        )
        self.assertEqual(c2["status"], STATUS_PROPOSED)
        self.assertEqual(c2["source"]["account"], "primary")

    def test_33_two_matching_account_params_succeed(self) -> None:
        """Passing both manifest_account and bound_account with matching values (with whitespace trimming) succeeds."""
        comp = build_test_mda2_composite(account="primary")
        decision = {"kind": "project", "id": "pilot-proj"}

        att = validate_mda2_attachment(comp, manifest_account="primary", bound_account="  primary  ")
        self.assertEqual(att["account"], "primary")

        c = propose_attachment_filing(
            comp,
            decision,
            self.mock_catalogs,
            manifest_account="  primary",
            bound_account="primary  ",
            filemaps={"primary": self.mock_filemap},
            current_time=self.now,
        )
        self.assertEqual(c["status"], STATUS_PROPOSED)
        self.assertEqual(c["source"]["account"], "primary")

    def test_34_two_differing_account_params_fail_closed(self) -> None:
        """Passing differing manifest_account and bound_account parameters fails closed immediately."""
        comp = build_test_mda2_composite(account="primary")
        decision = {"kind": "project", "id": "pilot-proj"}

        with self.assertRaises(InvalidMDA2FetchError) as ctx1:
            validate_mda2_attachment(comp, manifest_account="primary", bound_account="secondary")
        self.assertIn("account drift between explicit keyword parameters", str(ctx1.exception).lower())

        with self.assertRaises(InvalidMDA2FetchError) as ctx2:
            propose_attachment_filing(
                comp,
                decision,
                self.mock_catalogs,
                manifest_account="primary",
                bound_account="secondary",
                filemaps={"primary": self.mock_filemap},
                current_time=self.now,
            )
        self.assertIn("account drift between explicit keyword parameters", str(ctx2.exception).lower())

    def test_35_operation_account_missing_with_valid_manifest_account_succeeds(self) -> None:
        """operation.account is optional: omitting it when bound manifest account is valid succeeds."""
        comp = build_test_mda2_composite(account="work@example.com")
        comp["operation"].pop("account", None)
        norm_att = validate_mda2_attachment(comp, manifest_account="work@example.com")
        self.assertEqual(norm_att["account"], "work@example.com")

        decision = {"kind": "project", "id": "pilot-proj"}
        c = propose_attachment_filing(
            comp,
            decision,
            self.mock_catalogs,
            manifest_account="work@example.com",
            filemaps={"primary": self.mock_filemap},
            current_time=self.now,
        )
        self.assertEqual(c["status"], STATUS_PROPOSED)
        self.assertEqual(c["source"]["account"], "work@example.com")

    def test_36_matching_optional_operation_account_succeeds(self) -> None:
        """When operation.account is present and matches bound manifest account, validation succeeds."""
        comp = build_test_mda2_composite(account="work@example.com")
        comp["operation"]["account"] = "work@example.com"
        norm_att = validate_mda2_attachment(comp, manifest_account="work@example.com")
        self.assertEqual(norm_att["account"], "work@example.com")

    def test_37_drifted_optional_operation_account_fails_closed(self) -> None:
        """When operation.account is present but drifts from bound manifest account, fails closed."""
        comp = build_test_mda2_composite(account="work@example.com")
        comp["operation"]["account"] = "attacker@example.com"
        with self.assertRaises(InvalidMDA2FetchError) as ctx:
            validate_mda2_attachment(comp, manifest_account="work@example.com")
        self.assertIn("account drift between operation", str(ctx.exception).lower())

    def test_38_candidate_account_drift_against_manifest_account_fails_closed(self) -> None:
        """When candidate account drifts from bound manifest account, fails closed."""
        comp = build_test_mda2_composite(account="work@example.com")
        comp["candidate"]["account"] = "other@example.com"
        with self.assertRaises(InvalidMDA2FetchError) as ctx:
            validate_mda2_attachment(comp, manifest_account="work@example.com")
        self.assertIn("account drift between candidate", str(ctx.exception).lower())

    # ==========================================================================
    # 10. Canonical Quarantine Security & Invariant Tests
    # ==========================================================================

    def test_39_physical_quarantine_inconsistent_count_or_total_bytes_fails_closed(self) -> None:
        """Physical verification fails closed when inventory count or total_bytes is inconsistent."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp_dir:
            ws_root = Path(tmp_dir)
            file_bytes = b"real content"
            file_sha = hashlib.sha256(file_bytes).hexdigest().lower()
            run_id = "run_inconsistent_count"
            clean_fn = "inconsistent.pdf"
            mid = "<msg-inconsistent@example.org>"
            norm_mid = normalize_message_id(mid)

            q_dir = ws_root / "data" / "mail-desk" / "attachments" / run_id
            q_dir.mkdir(parents=True, exist_ok=True)
            (q_dir / clean_fn).write_bytes(file_bytes)

            inv_file = q_dir / ".quarantine-inventory.json"
            inv_data = {
                "schema_version": 1,
                "messages": {
                    norm_mid: {
                        "count": 2,  # Inconsistent: files dict only has 1
                        "total_bytes": len(file_bytes),
                        "files": {
                            clean_fn: {
                                "sha256": file_sha,
                                "size_bytes": len(file_bytes),
                            }
                        },
                    }
                },
            }
            inv_file.write_text(json.dumps(inv_data), encoding="utf-8")

            comp = build_test_mda2_composite(
                message_id=mid,
                filename=clean_fn,
                sha256=file_sha,
                size_bytes=len(file_bytes),
                run_id=run_id,
            )

            with self.assertRaises(InvalidMDA2FetchError) as ctx:
                validate_mda2_attachment(
                    comp,
                    manifest_account=self.manifest_account,
                    workspace_root=ws_root,
                    verify_physical_evidence=True,
                )
            self.assertIn("does not match number of files", str(ctx.exception).lower())

            # Case B: total_bytes is corrupted/inconsistent
            inv_data["messages"][norm_mid]["count"] = 1
            inv_data["messages"][norm_mid]["total_bytes"] = 999999
            inv_file.write_text(json.dumps(inv_data), encoding="utf-8")

            with self.assertRaises(InvalidMDA2FetchError) as ctx:
                validate_mda2_attachment(
                    comp,
                    manifest_account=self.manifest_account,
                    workspace_root=ws_root,
                    verify_physical_evidence=True,
                )
            self.assertIn("does not match sum of file sizes", str(ctx.exception).lower())

    def test_40_physical_quarantine_corrupted_foreign_entry_fails_closed(self) -> None:
        """Physical verification fails closed when another foreign message in inventory is corrupted."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp_dir:
            ws_root = Path(tmp_dir)
            file_bytes = b"real content"
            file_sha = hashlib.sha256(file_bytes).hexdigest().lower()
            run_id = "run_corrupt_foreign"
            clean_fn = "valid.pdf"
            mid = "<msg-valid@example.org>"
            norm_mid = normalize_message_id(mid)

            q_dir = ws_root / "data" / "mail-desk" / "attachments" / run_id
            q_dir.mkdir(parents=True, exist_ok=True)
            (q_dir / clean_fn).write_bytes(file_bytes)

            inv_file = q_dir / ".quarantine-inventory.json"
            inv_data = {
                "schema_version": 1,
                "messages": {
                    norm_mid: {
                        "count": 1,
                        "total_bytes": len(file_bytes),
                        "files": {
                            clean_fn: {
                                "sha256": file_sha,
                                "size_bytes": len(file_bytes),
                            }
                        },
                    },
                    "foreign-msg@example.com": {
                        "count": 1,
                        "total_bytes": 100,
                        "files": "not-a-dict",  # Corrupted schema
                    },
                },
            }
            inv_file.write_text(json.dumps(inv_data), encoding="utf-8")

            comp = build_test_mda2_composite(
                message_id=mid,
                filename=clean_fn,
                sha256=file_sha,
                size_bytes=len(file_bytes),
                run_id=run_id,
            )

            with self.assertRaises(InvalidMDA2FetchError) as ctx:
                validate_mda2_attachment(
                    comp,
                    manifest_account=self.manifest_account,
                    workspace_root=ws_root,
                    verify_physical_evidence=True,
                )
            self.assertIn("corrupted quarantine inventory", str(ctx.exception).lower())

    def test_41_physical_quarantine_symlink_or_reparse_escape_fails_closed(self) -> None:
        """Physical verification fails closed when target file is a symlink or Windows reparse point."""
        import tempfile
        from unittest.mock import patch, MagicMock
        with tempfile.TemporaryDirectory() as tmp_dir:
            ws_root = Path(tmp_dir)
            file_bytes = b"real content"
            file_sha = hashlib.sha256(file_bytes).hexdigest().lower()
            run_id = "run_symlink"
            clean_fn = "symlink.pdf"
            mid = "<msg-symlink@example.org>"
            norm_mid = normalize_message_id(mid)

            q_dir = ws_root / "data" / "mail-desk" / "attachments" / run_id
            q_dir.mkdir(parents=True, exist_ok=True)
            (q_dir / clean_fn).write_bytes(file_bytes)

            inv_file = q_dir / ".quarantine-inventory.json"
            inv_data = {
                "schema_version": 1,
                "messages": {
                    norm_mid: {
                        "count": 1,
                        "total_bytes": len(file_bytes),
                        "files": {
                            clean_fn: {
                                "sha256": file_sha,
                                "size_bytes": len(file_bytes),
                            }
                        },
                    }
                },
            }
            inv_file.write_text(json.dumps(inv_data), encoding="utf-8")

            comp = build_test_mda2_composite(
                message_id=mid,
                filename=clean_fn,
                sha256=file_sha,
                size_bytes=len(file_bytes),
                run_id=run_id,
            )

            # Test symlink detection
            with patch("pathlib.Path.is_symlink", return_value=True):
                with self.assertRaises(InvalidMDA2FetchError) as ctx:
                    validate_mda2_attachment(
                        comp,
                        manifest_account=self.manifest_account,
                        workspace_root=ws_root,
                        verify_physical_evidence=True,
                    )
                self.assertIn("symlink detected", str(ctx.exception).lower())

            # Test Windows reparse point (0x400)
            mock_stat = MagicMock()
            mock_stat.st_file_attributes = 0x400
            with patch("os.name", "nt"), patch("os.lstat", return_value=mock_stat):
                with self.assertRaises(InvalidMDA2FetchError) as ctx:
                    validate_mda2_attachment(
                        comp,
                        manifest_account=self.manifest_account,
                        workspace_root=ws_root,
                        verify_physical_evidence=True,
                    )
                self.assertIn("reparse point detected", str(ctx.exception).lower())


if __name__ == "__main__":
    unittest.main()
