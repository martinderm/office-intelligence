"""FR-02c contracts for neutral, catalog-backed project evidence."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock


MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

from core import classifier, evidence  # noqa: E402


class ProjectEvidenceContextTests(unittest.TestCase):
    @staticmethod
    def project() -> dict:
        return {
            "id": "meshe",
            "kuerzel": "MESHE",
            "title": "MESHE",
            "mailbox_folder": "Projects/MESHE",
            "schema_version": 3,
            "workpackages": [{
                "id": "wp1",
                "title": "Quality and Evaluation",
                "status": "active",
                "tasks": [{"id": "T1.7", "title": "Quality Plan"}],
                "deliverables": [{"id": "D1.2", "title": "Quality Handbook"}],
            }],
            "milestones": [{"id": "MS5", "title": "Evaluation completed"}],
        }

    @staticmethod
    def email(subject: str, **extra: object) -> dict:
        return {
            "envelope_id": "42",
            "folder": "INBOX",
            "message_id": "<evidence@example.test>",
            "subject": subject,
            "from": "Coordinator <coord@example.test>",
            "to": "Martin <martin@example.test>",
            "date": "2026-05-18",
            **extra,
        }

    def classify_project(self, subject: str, **extra: object) -> dict:
        return classifier.classify_email(
            self.email(subject, **extra),
            projects=[self.project()], topics=[], sent_lookup={}, final_index={"items": {}},
        )

    def test_complete_wp_task_deliverable_context_is_catalog_backed_and_neutral(self) -> None:
        item = self.classify_project("MESHE WP1 T1.7 D1.2")
        spec = item["evidence"]

        self.assertEqual("memory/evidence/projects/meshe/2026-05.md", spec["file"])
        self.assertIn("[MESHE | WP1 (Quality and Evaluation) / T1.7 (Quality Plan) / D1.2 (Quality Handbook)]", spec["entry"])
        self.assertIn("Message-ID: `evidence@example.test`", spec["entry"])
        self.assertIn("Beteiligte: Coordinator <coord@example.test>; An: Martin <martin@example.test>", spec["entry"])
        self.assertIn("Mailgegenstand: MESHE WP1 T1.7 D1.2", spec["entry"])

    def test_project_milestone_is_rendered_from_catalog_data(self) -> None:
        spec = self.classify_project("MESHE MS5")["evidence"]

        self.assertIn("[MESHE | MS5 (Evaluation completed)]", spec["entry"])

    def test_ambiguous_candidates_do_not_create_a_false_scalar_context(self) -> None:
        project = self.project()
        project["workpackages"][0]["tasks"].append({"id": "T1.8", "title": "Shared Review"})
        project["workpackages"].append({
            "id": "wp2", "title": "Engagement", "status": "active",
            "tasks": [{"id": "T2.1", "title": "Shared Review"}], "deliverables": [],
        })
        item = classifier.classify_email(
            self.email("MESHE Shared Review"),
            projects=[project], topics=[], sent_lookup={}, final_index={"items": {}},
        )

        self.assertNotIn("task", item["decision"])
        self.assertIn("artifact_candidates", item["decision"])
        self.assertIn("[MESHE]", item["evidence"]["entry"])
        self.assertNotIn("T1.8", item["evidence"]["entry"])
        self.assertNotIn("T2.1", item["evidence"]["entry"])

    def test_thread_inherited_project_evidence_uses_writer_file_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            item = classifier.classify_email(
                self.email("Re: MESHE", in_reply_to="<parent@example.test>"),
                workspace_root=workspace,
                projects=[self.project()], topics=[], sent_lookup={},
                final_index={"items": {"parent@example.test": {"final_folder": "Projects/MESHE"}}},
            )
            spec = item["evidence"]
            written = evidence.flush_batch_evidence(
                [{"message_id": item["message_id"], "evidence": spec}], workspace
            )
            target = workspace / spec["file"]

            self.assertEqual({str(target.resolve()): True}, written)
            self.assertNotIn("target_file", spec)
            self.assertTrue(target.exists())
            self.assertIn("evidence@example.test", target.read_text(encoding="utf-8"))

    def test_full_read_status_is_machine_readable_without_persisting_body(self) -> None:
        full_reader = Mock(return_value={
            **self.email("MESHE Audit"),
            "preview": "SECRET FULL BODY MUST NOT BE PERSISTED",
            "error": None,
        })
        item = classifier.classify_email_two_pass(
            self.email("MESHE Audit"),
            projects=[self.project()], topics=[], sent_lookup={}, final_index={"items": {}},
            full_reader=full_reader,
        )

        spec = item["evidence"]
        self.assertEqual("full_body", spec["read_escalation"]["level"])
        self.assertEqual("completed", spec["read_escalation"]["status"])
        self.assertNotIn("SECRET FULL BODY", json.dumps(spec))

    def test_legacy_project_and_thread_topic_remain_writer_compatible(self) -> None:
        legacy = {
            "id": "legacy", "kuerzel": "LEGACY", "title": "Legacy",
            "mailbox_folder": "Projects/Legacy", "schema_version": 2,
        }
        legacy_item = classifier.classify_email(
            self.email("LEGACY update"), projects=[legacy], topics=[], sent_lookup={}, final_index={"items": {}},
        )
        self.assertIn("[LEGACY]", legacy_item["evidence"]["entry"])

        topic = {"id": "topic", "title": "Topic", "mailbox_folder": "Topics/Topic"}
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            topic_item = classifier.classify_email(
                self.email("Re: Topic", in_reply_to="<topic-parent@example.test>"),
                workspace_root=workspace,
                projects=[], topics=[topic], sent_lookup={},
                final_index={"items": {"topic-parent@example.test": {"final_folder": "Topics/Topic"}}},
            )
            self.assertTrue(evidence.update_evidence_file(topic_item["evidence"], topic_item["message_id"], workspace))


if __name__ == "__main__":
    unittest.main()
