"""FR-03b2b contracts for catalog-backed subtopic operations."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest


MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

from core import classifier, evidence  # noqa: E402


class OperationClassifierTests(unittest.TestCase):
    @staticmethod
    def operation(**changes: object) -> dict:
        value = {
            "id": "coordination",
            "title": "Coordination cycle",
            "aliases": ["Coordination Call"],
            "keywords": ["agenda"],
            "typical_subject_patterns": ["[OPS-CALL]"],
            "status": "active",
        }
        value.update(changes)
        return value

    @classmethod
    def topic(cls, *, operations: list[dict] | None = None) -> dict:
        return {
            "id": "operations",
            "title": "Operations",
            "mailbox_folder": "Topics/Operations",
            "aliases": ["Operations"],
            "subtopics": [{
                "id": "course-design",
                "title": "Course Design",
                "aliases": ["Course Redesign"],
                "status": "active",
                "operations": operations if operations is not None else [cls.operation()],
            }],
        }

    @staticmethod
    def email(subject: str, **extra: object) -> dict:
        return {
            "envelope_id": "operation-1",
            "folder": "INBOX",
            "message_id": "<operation-1@example.test>",
            "subject": subject,
            "from": "Coordinator <coord@example.test>",
            "to": "Martin <martin@example.test>",
            "date": "2026-07-08",
            **extra,
        }

    def classify(self, subject: str, *, topic: dict | None = None, workspace: Path | None = None, **extra: object) -> dict:
        return classifier.classify_email(
            self.email(subject, **extra), workspace_root=workspace,
            projects=[], topics=[topic or self.topic()], sent_lookup={}, final_index={"items": {}},
        )

    def test_pattern_resolves_operation_with_scoped_evidence_and_safe_index_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            reference = "memory/references/topics/operations/subtopics/course-design/operations/coordination/index.md"
            target = workspace / reference
            target.parent.mkdir(parents=True)
            target.write_text("# Coordination\n", encoding="utf-8")
            topic = self.topic()
            topic["subtopics"][0]["operations"][0]["reference_md"] = reference
            item = self.classify(
                "Operations — Course Redesign [OPS-CALL]", topic=topic, workspace=workspace,
                synthesis_targets=[{"file": "mail-supplied.md", "type": "untrusted"}],
            )

        self.assertEqual("Topics/Operations", item["action"]["target_folder"])
        self.assertEqual("course-design", item["decision"]["subtopic"])
        self.assertEqual("coordination", item["decision"]["operation"])
        self.assertIn("subject_pattern:[OPS-CALL]", item["decision"]["operation_match_reasons"])
        self.assertEqual(
            "memory/evidence/topics/operations/subtopics/course-design/operations/coordination/2026-07.md",
            item["evidence"]["file"],
        )
        self.assertIn("[OPERATIONS | course-design (Course Design) / coordination (Coordination cycle)]", item["evidence"]["entry"])
        self.assertEqual([{"file": reference, "type": "operation_reference"}], item["synthesis_targets"])

    def test_alias_subject_keyword_and_preview_keyword_score_deterministically(self) -> None:
        alias = self.classify("Operations — Course Redesign — Coordination Call")
        keyword = self.classify("Operations — Course Redesign agenda")
        preview = self.classify("Operations — Course Redesign update", preview="agenda attached")

        self.assertIn("subject_alias:Coordination Call", alias["decision"]["operation_match_reasons"])
        self.assertIn("subject_keyword:agenda", keyword["decision"]["operation_match_reasons"])
        self.assertIn("keyword:agenda", preview["decision"]["operation_match_reasons"])

    def test_operation_needs_current_parent_and_subtopic_evidence(self) -> None:
        root_only = self.classify("Operations — Coordination Call")
        self.assertEqual("operations", root_only["decision"]["id"])
        self.assertNotIn("operation", root_only["decision"])

        unrelated = self.classify("Course Redesign — Coordination Call", topic={
            "id": "operations", "title": "Operations", "mailbox_folder": "Topics/Operations",
            "subtopics": self.topic()["subtopics"],
        })
        self.assertEqual("operations", unrelated["decision"]["id"])
        self.assertEqual("course-design", unrelated["decision"]["subtopic"])
        self.assertEqual("coordination", unrelated["decision"]["operation"])

    def test_ambiguous_duplicate_and_inactive_operations_do_not_create_scalar(self) -> None:
        ambiguous = self.classify("Operations — Course Redesign — Shared", topic=self.topic(operations=[
            self.operation(id="one", title="One", aliases=["Shared"]),
            self.operation(id="two", title="Two", aliases=["Shared"]),
        ]))
        duplicate = self.classify("Operations — Course Redesign — Coordination Call", topic=self.topic(operations=[
            self.operation(), self.operation(title="Duplicate record", aliases=[]),
        ]))
        inactive = self.classify("Operations — Course Redesign — Old process", topic=self.topic(operations=[
            self.operation(id="old-process", title="Old process", aliases=["Old process"], status="inactive"),
        ]))

        self.assertNotIn("operation", ambiguous["decision"])
        self.assertEqual(["one", "two"], [row["id"] for row in ambiguous["decision"]["operation_candidates"]])
        self.assertNotIn("operation", duplicate["decision"])
        self.assertIn("operation_candidates", duplicate["decision"])
        self.assertNotIn("operation", inactive["decision"])

    def test_legacy_operation_is_active_but_noncanonical_reference_has_no_target(self) -> None:
        operation = self.operation(reference_md="memory/references/topics/operations/unsafe.md")
        operation.pop("status")
        legacy = self.classify("Operations — Course Redesign — Coordination Call", topic=self.topic(operations=[operation]))

        self.assertNotIn("operation", legacy["decision"])
        self.assertEqual("noncanonical_reference_md", legacy["decision"]["operation_candidates"][0]["reasons"][-1])
        self.assertEqual([], legacy["synthesis_targets"])

    def test_topic_thread_does_not_inherit_operation_but_current_mail_can_prove_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            item = classifier.classify_email(
                self.email("Re: Course Redesign — Coordination Call", in_reply_to="<parent@example.test>"),
                workspace_root=workspace, projects=[], topics=[self.topic()], sent_lookup={},
                final_index={"items": {"parent@example.test": {"final_folder": "Topics/Operations"}}},
            )
            written = evidence.flush_batch_evidence(
                [{"message_id": item["message_id"], "evidence": item["evidence"]}], workspace
            )

        self.assertEqual("course-design", item["decision"]["subtopic"])
        self.assertEqual("coordination", item["decision"]["operation"])
        self.assertTrue(written)
        self.assertEqual("operation_evidence", item["evidence"]["type"])


if __name__ == "__main__":
    unittest.main()
