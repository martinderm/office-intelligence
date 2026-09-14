"""Tests for FR-08 MD-A5: Catalog- and filemap-backed attachment filing candidate proposal.

Hermetic tests verifying:
1. Project filing proposal
2. Topic filing proposal
3. Subtopic filing proposal (explicit & inherited)
4. Event storage inheritance (only inherits from explicitly cataloged parent/subtopic)
5. EUCEN filing resolution
6. Multiple storages and archive/read-only storages (storage_review_required)
7. Missing and stale filemap handling (storage_review_required)
8. Unoccupied target directory handling (directory_review_required)
9. Path traversal & filename injection protection
10. Deduplication (already_present on identical SHA-256)
11. Collision detection (collision_detected on identical name but different SHA-256)
12. Deterministic candidate hash binding
13. Strict read-only invariant (zero writes/mutations to filemap, catalogs, or cloud)
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, mock_open, patch

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

from core.attachment_filing import (  # noqa: E402
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
    validate_filemap_freshness,
)


class TestMailDeskAttachmentsMDA5(unittest.TestCase):
    """Hermetic test suite for MD-A5 attachment filing proposals."""

    def setUp(self) -> None:
        self.now = datetime(2026, 9, 14, 10, 0, 0, tzinfo=timezone.utc)
        self.fresh_timestamp = "2026-09-14 09:30:00"

        self.mock_catalogs = {
            "projects": [
                {
                    "id": "pilot-proj",
                    "title": "Pilot Project",
                    "cloud_sync": {
                        "primary": {
                            "type": "nextcloud",
                            "target_dir": "01_Admin/Correspondence",
                            "output_json": "memory/cloud/projects/pilot-proj/filemap.json",
                        }
                    },
                },
                {
                    "id": "multi-storage-proj",
                    "title": "Multi Storage Project",
                    "cloud_sync": {
                        "drive": {"target_dir": "docs"},
                        "nextcloud": {"target_dir": "docs"},
                    },
                },
                {
                    "id": "archive-storage-proj",
                    "title": "Archive Storage Project",
                    "cloud_sync": {
                        "archive_main": {
                            "archive": True,
                            "target_dir": "archive_docs",
                        }
                    },
                },
                {
                    "id": "no-cloud-proj",
                    "title": "Project Without Cloud",
                },
                {
                    "id": "eucen",
                    "title": "EUCEN European University Continuing Education Network",
                    "cloud_sync": {
                        "eucen_cloud": {
                            "target_dir": "General/Correspondence",
                            "output_json": "memory/cloud/projects/eucen/filemap.json",
                        }
                    },
                },
            ],
            "topics": [
                {
                    "id": "hr-topic",
                    "title": "Human Resources",
                    "cloud_sync": {
                        "hr_cloud": {
                            "target_dir": "Recruiting/Applications",
                            "output_json": "memory/cloud/topics/hr-topic/filemap.json",
                        }
                    },
                    "subtopics": [
                        {
                            "id": "recruiting",
                            "title": "Recruiting",
                            # Inherits from parent topic hr_cloud
                        },
                        {
                            "id": "payroll",
                            "title": "Payroll",
                            "cloud_sync": {
                                "payroll_cloud": {
                                    "target_dir": "Monthly/Reports",
                                    "output_json": "memory/cloud/topics/hr-topic/payroll/filemap.json",
                                }
                            },
                        },
                    ],
                },
                {
                    "id": "topic-no-cloud",
                    "title": "No Cloud Topic",
                    "subtopics": [
                        {"id": "general", "title": "General"}
                    ],
                },
            ],
        }

        self.sample_attachment = {
            "account": "primary",
            "message_id": "<msg-501@example.org>",
            "folder": "INBOX",
            "envelope_id": "501",
            "part_locator": "2",
            "filename": "minutes_2026.pdf",
            "sha256": "a" * 64,
            "quarantine_path": "data/mail-desk/attachments/run-1/minutes_2026.pdf",
        }

        self.mock_filemap = {
            "schema_version": 1,
            "updated_at": self.fresh_timestamp,
            "files": {
                "01_Admin/Correspondence/existing_doc.pdf": {
                    "version": "1",
                    "mtime": "2026-09-14 08:00:00",
                    "size": "100 KB",
                    "sha256": "f" * 64,
                    "description": "Existing correspondence",
                }
            },
        }

    def test_1_project_filing_proposal(self) -> None:
        """Project with unambiguous cloud storage and occupied directory produces 'proposed' candidate."""
        decision = {"kind": "project", "id": "pilot-proj"}
        candidate = propose_attachment_filing(
            self.sample_attachment,
            decision,
            self.mock_catalogs,
            filemaps={"primary": self.mock_filemap},
            current_time=self.now,
        )
        self.assertEqual(candidate["status"], STATUS_PROPOSED)
        self.assertEqual(candidate["promotion_status"], PROMOTION_STATUS_PENDING_HUMAN_REVIEW)
        self.assertEqual(candidate["destination"]["storage_id"], "primary")
        self.assertEqual(candidate["destination"]["target_relative_path"], "01_Admin/Correspondence/minutes_2026.pdf")
        self.assertFalse(candidate["dedupe"]["already_present"])
        self.assertFalse(candidate["dedupe"]["collision_detected"])
        self.assertEqual(len(candidate["candidate_hash"]), 64)

    def test_2_topic_filing_proposal(self) -> None:
        """Topic with cloud storage produces 'proposed' candidate."""
        decision = {"kind": "topic", "id": "hr-topic"}
        filemap = {
            "schema_version": 1,
            "updated_at": self.fresh_timestamp,
            "files": {
                "Recruiting/Applications/resume.pdf": {
                    "sha256": "e" * 64,
                    "mtime": "2026-09-14 08:00:00",
                    "size": "50 KB",
                    "version": "1",
                    "description": "Resume",
                }
            },
        }
        candidate = propose_attachment_filing(
            self.sample_attachment,
            decision,
            self.mock_catalogs,
            filemaps={"hr_cloud": filemap},
            current_time=self.now,
        )
        self.assertEqual(candidate["status"], STATUS_PROPOSED)
        self.assertEqual(candidate["destination"]["storage_id"], "hr_cloud")
        self.assertEqual(candidate["destination"]["target_relative_path"], "Recruiting/Applications/minutes_2026.pdf")

    def test_3_subtopic_filing_proposal(self) -> None:
        """Subtopic inherits parent topic storage when not explicitly specified, or uses explicit."""
        # 1. Inherited
        decision_inherited = {"kind": "subtopic", "id": "recruiting", "topic": "hr-topic"}
        filemap_hr = {
            "schema_version": 1,
            "updated_at": self.fresh_timestamp,
            "files": {"Recruiting/Applications/app.pdf": {"sha256": "1" * 64}},
        }
        c1 = propose_attachment_filing(
            self.sample_attachment,
            decision_inherited,
            self.mock_catalogs,
            filemaps={"hr_cloud": filemap_hr},
            current_time=self.now,
        )
        self.assertEqual(c1["status"], STATUS_PROPOSED)
        self.assertEqual(c1["destination"]["storage_id"], "hr_cloud")

        # 2. Explicit
        decision_explicit = {"kind": "subtopic", "id": "payroll", "topic": "hr-topic"}
        filemap_payroll = {
            "schema_version": 1,
            "updated_at": self.fresh_timestamp,
            "files": {"Monthly/Reports/sep.pdf": {"sha256": "2" * 64}},
        }
        c2 = propose_attachment_filing(
            self.sample_attachment,
            decision_explicit,
            self.mock_catalogs,
            filemaps={"payroll_cloud": filemap_payroll},
            current_time=self.now,
        )
        self.assertEqual(c2["status"], STATUS_PROPOSED)
        self.assertEqual(c2["destination"]["storage_id"], "payroll_cloud")

    def test_4_event_inheritance_rules(self) -> None:
        """Events only inherit explicitly cataloged parent or subtopic storages; otherwise not_configured."""
        # Event with parent that has cloud_sync inherits properly
        decision_with_cloud = {
            "kind": "event",
            "event": "kickoff_2026",
            "subtopic": "recruiting",
            "id": "hr-topic",
        }
        filemap_hr = {
            "schema_version": 1,
            "updated_at": self.fresh_timestamp,
            "files": {"Recruiting/Applications/kickoff.pdf": {"sha256": "3" * 64}},
        }
        cand_ok = propose_attachment_filing(
            self.sample_attachment,
            decision_with_cloud,
            self.mock_catalogs,
            filemaps={"hr_cloud": filemap_hr},
            current_time=self.now,
        )
        self.assertEqual(cand_ok["status"], STATUS_PROPOSED)
        self.assertEqual(cand_ok["destination"]["storage_id"], "hr_cloud")

        # Event with parent having NO cloud_sync -> not_configured
        decision_no_cloud = {
            "kind": "event",
            "event": "workshop_2026",
            "subtopic": "general",
            "id": "topic-no-cloud",
        }
        cand_fail = propose_attachment_filing(
            self.sample_attachment,
            decision_no_cloud,
            self.mock_catalogs,
            current_time=self.now,
        )
        self.assertEqual(cand_fail["status"], STATUS_NOT_CONFIGURED)
        self.assertIn("explicitly cataloged parent or subtopic", cand_fail["reason"])

    def test_5_eucen_filing_resolution(self) -> None:
        """EUCEN catalog entity resolves storage and proposes filing."""
        decision = {"kind": "project", "id": "eucen"}
        filemap_eucen = {
            "schema_version": 1,
            "updated_at": self.fresh_timestamp,
            "files": {"General/Correspondence/letter.pdf": {"sha256": "4" * 64}},
        }
        cand = propose_attachment_filing(
            self.sample_attachment,
            decision,
            self.mock_catalogs,
            filemaps={"eucen_cloud": filemap_eucen},
            current_time=self.now,
        )
        self.assertEqual(cand["status"], STATUS_PROPOSED)
        self.assertEqual(cand["destination"]["storage_id"], "eucen_cloud")
        self.assertEqual(cand["destination"]["target_relative_path"], "General/Correspondence/minutes_2026.pdf")

    def test_6_multiple_and_archive_storages_require_review(self) -> None:
        """Multiple storages or archive/read-only storages must yield storage_review_required."""
        # 1. Multiple storages
        dec_multi = {"kind": "project", "id": "multi-storage-proj"}
        c_multi = propose_attachment_filing(self.sample_attachment, dec_multi, self.mock_catalogs)
        self.assertEqual(c_multi["status"], STATUS_STORAGE_REVIEW_REQUIRED)
        self.assertIn("Multiple cloud storages", c_multi["reason"])

        # 2. Archive storage
        dec_archive = {"kind": "project", "id": "archive-storage-proj"}
        c_archive = propose_attachment_filing(self.sample_attachment, dec_archive, self.mock_catalogs)
        self.assertEqual(c_archive["status"], STATUS_STORAGE_REVIEW_REQUIRED)
        self.assertIn("archive or read-only", c_archive["reason"])

    def test_7_missing_and_stale_filemap(self) -> None:
        """Missing or stale filemap yields storage_review_required."""
        decision = {"kind": "project", "id": "pilot-proj"}

        # 1. Missing filemap (no filemaps dict, no file on disk)
        c_missing = propose_attachment_filing(
            self.sample_attachment,
            decision,
            self.mock_catalogs,
            filemaps={},
            workspace_root=Path("/nonexistent_workspace"),
        )
        self.assertEqual(c_missing["status"], STATUS_STORAGE_REVIEW_REQUIRED)
        self.assertIn("Filemap not found", c_missing["reason"])

        # 2. Stale filemap (timestamp 48 hours ago)
        stale_filemap = {
            "schema_version": 1,
            "updated_at": "2026-09-12 10:00:00",
            "files": {"01_Admin/Correspondence/doc.pdf": {"sha256": "5" * 64}},
        }
        c_stale = propose_attachment_filing(
            self.sample_attachment,
            decision,
            self.mock_catalogs,
            filemaps={"primary": stale_filemap},
            current_time=self.now,
        )
        self.assertEqual(c_stale["status"], STATUS_STORAGE_REVIEW_REQUIRED)
        self.assertTrue(c_stale["filemap_evidence"]["is_stale"])
        self.assertIn("stale", c_stale["reason"].lower())

    def test_8_unoccupied_target_directory(self) -> None:
        """If candidate directory has no files in filemap, directory_review_required must be returned."""
        decision = {"kind": "project", "id": "pilot-proj"}
        filemap_different_dir = {
            "schema_version": 1,
            "updated_at": self.fresh_timestamp,
            "files": {
                "99_Unrelated/OtherFolder/doc.pdf": {"sha256": "6" * 64}
            },
        }
        cand = propose_attachment_filing(
            self.sample_attachment,
            decision,
            self.mock_catalogs,
            filemaps={"primary": filemap_different_dir},
            current_time=self.now,
        )
        self.assertEqual(cand["status"], STATUS_DIRECTORY_REVIEW_REQUIRED)
        self.assertIn("not established/occupied", cand["reason"])

    def test_9_path_traversal_and_injection_protection(self) -> None:
        """Path traversal in attachment filename or directory must be neutralized or rejected."""
        decision = {"kind": "project", "id": "pilot-proj"}
        malicious_att = dict(self.sample_attachment)
        malicious_att["filename"] = "../../../../../etc/passwd"

        cand = propose_attachment_filing(
            malicious_att,
            decision,
            self.mock_catalogs,
            filemaps={"primary": self.mock_filemap},
            current_time=self.now,
        )
        # Traversal in filename is sanitized to 'passwd'
        self.assertEqual(cand["destination"]["target_filename"], "passwd")
        self.assertEqual(cand["destination"]["target_relative_path"], "01_Admin/Correspondence/passwd")
        self.assertNotIn("..", cand["destination"]["target_relative_path"])

        # Traversal in target_dir in decision must trigger directory_review_required
        decision_bad_dir = {"kind": "project", "id": "pilot-proj", "target_dir": "../escape"}
        cand_bad_dir = propose_attachment_filing(
            self.sample_attachment,
            decision_bad_dir,
            self.mock_catalogs,
            filemaps={"primary": self.mock_filemap},
            current_time=self.now,
        )
        self.assertEqual(cand_bad_dir["status"], STATUS_DIRECTORY_REVIEW_REQUIRED)
        self.assertIn("traversal", cand_bad_dir["reason"].lower())

    def test_10_deduplication_already_present(self) -> None:
        """File with identical SHA-256 already cataloged must return already_present."""
        decision = {"kind": "project", "id": "pilot-proj"}
        dup_filemap = {
            "schema_version": 1,
            "updated_at": self.fresh_timestamp,
            "files": {
                "01_Admin/Correspondence/existing_doc.pdf": {
                    "sha256": "a" * 64,  # Exact same sha256 as sample_attachment
                    "version": "1",
                }
            },
        }
        cand = propose_attachment_filing(
            self.sample_attachment,
            decision,
            self.mock_catalogs,
            filemaps={"primary": dup_filemap},
            current_time=self.now,
        )
        self.assertEqual(cand["status"], STATUS_ALREADY_PRESENT)
        self.assertTrue(cand["dedupe"]["already_present"])
        self.assertFalse(cand["dedupe"]["collision_detected"])
        self.assertEqual(cand["dedupe"]["existing_path"], "01_Admin/Correspondence/existing_doc.pdf")
        self.assertEqual(cand["dedupe"]["existing_sha256"], "a" * 64)

    def test_11_collision_detection(self) -> None:
        """File with same name but different SHA-256 must return collision_detected."""
        decision = {"kind": "project", "id": "pilot-proj"}
        collision_filemap = {
            "schema_version": 1,
            "updated_at": self.fresh_timestamp,
            "files": {
                "01_Admin/Correspondence/minutes_2026.pdf": {  # Same filename
                    "sha256": "b" * 64,  # Different SHA-256!
                    "version": "1",
                }
            },
        }
        cand = propose_attachment_filing(
            self.sample_attachment,
            decision,
            self.mock_catalogs,
            filemaps={"primary": collision_filemap},
            current_time=self.now,
        )
        self.assertEqual(cand["status"], STATUS_COLLISION_DETECTED)
        self.assertFalse(cand["dedupe"]["already_present"])
        self.assertTrue(cand["dedupe"]["collision_detected"])
        self.assertEqual(cand["dedupe"]["existing_path"], "01_Admin/Correspondence/minutes_2026.pdf")
        self.assertEqual(cand["dedupe"]["existing_sha256"], "b" * 64)

    def test_12_deterministic_hash_binding(self) -> None:
        """compute_candidate_hash produces consistent, deterministic 64-char hex SHA-256."""
        c1 = {"a": 1, "b": "hello", "nested": {"k": "v"}}
        c2 = {"b": "hello", "nested": {"k": "v"}, "a": 1}
        self.assertEqual(compute_candidate_hash(c1), compute_candidate_hash(c2))
        self.assertEqual(len(compute_candidate_hash(c1)), 64)

    def test_13_strict_read_only_invariant(self) -> None:
        """propose_attachment_filing must never write to disk or mutate files/directories."""
        with patch("builtins.open", side_effect=AssertionError("open() was called in write/modify mode!")) as mock_file_open:
            # Re-allow read mode if necessary, but block all write modes
            def safe_open(file, mode="r", *args, **kwargs):
                if any(m in mode for m in ("w", "a", "+", "x")):
                    raise AssertionError(f"Write operation attempted on {file} with mode {mode!r}!")
                return mock_open(read_data=json.dumps(self.mock_filemap))(file, mode, *args, **kwargs)

            mock_file_open.side_effect = safe_open

            with patch("os.mkdir", side_effect=AssertionError("os.mkdir was called!")):
                with patch("os.makedirs", side_effect=AssertionError("os.makedirs was called!")):
                    with patch("pathlib.Path.mkdir", side_effect=AssertionError("Path.mkdir was called!")):
                        decision = {"kind": "project", "id": "pilot-proj"}
                        cand = propose_attachment_filing(
                            self.sample_attachment,
                            decision,
                            self.mock_catalogs,
                            filemaps={"primary": self.mock_filemap},
                            current_time=self.now,
                        )
                        self.assertEqual(cand["status"], STATUS_PROPOSED)


if __name__ == "__main__":
    unittest.main()
