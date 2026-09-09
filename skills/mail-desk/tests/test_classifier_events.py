"""FR-03b2c contracts for catalog-backed, finite subtopic events."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest


MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

from core import classifier, evidence  # noqa: E402


class EventClassifierTests(unittest.TestCase):
    @staticmethod
    def event(**changes: object) -> dict:
        value = {
            "id": "spring-forum-2026",
            "title": "Spring Forum 2026",
            "starts_on": "2026-05-04",
            "ends_on": "2026-05-06",
            "aliases": ["Spring Forum"],
            "keywords": ["registration"],
            "typical_subject_patterns": ["[SPRING-FORUM]"],
            "reference_md": "memory/references/topics/operations/subtopics/course-design/events/spring-forum-2026/index.md",
            "cloud_storage": {"scope": "topic", "storage_id": "topic-store"},
            "status": "active",
            "phase": "planned",
        }
        value.update(changes)
        return value

    @classmethod
    def topic(cls, *, events: list[dict] | None = None, operations: list[dict] | None = None) -> dict:
        return {
            "id": "operations",
            "title": "Operations",
            "mailbox_folder": "Topics/Operations",
            "aliases": ["Operations"],
            "cloud_sync": {"topic-store": {"scan_dir": "data/cloud/TOPIC"}},
            "subtopics": [{
                "id": "course-design",
                "title": "Course Design",
                "aliases": ["Course Redesign"],
                "cloud_sync": {"sub-store": {"scan_dir": "data/cloud/SUB"}},
                "status": "active",
                "events": events if events is not None else [cls.event()],
                "operations": operations if operations is not None else [],
            }],
        }

    @staticmethod
    def email(subject: str, **extra: object) -> dict:
        return {
            "envelope_id": "event-1",
            "folder": "INBOX",
            "message_id": "<event-1@example.test>",
            "subject": subject,
            "from": "Events <events@example.test>",
            "to": "Martin <martin@example.test>",
            "date": "2026-04-20",
            **extra,
        }

    @staticmethod
    def add_dossier(workspace: Path, event: dict) -> None:
        path = workspace / event["reference_md"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Event dossier\n", encoding="utf-8")

    def test_event_pattern_has_scoped_evidence_and_dossier_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            event = self.event()
            self.add_dossier(workspace, event)
            item = classifier.classify_email(
                self.email("Operations — Course Redesign [SPRING-FORUM]", synthesis_targets=[{"file": "mail.md"}]),
                workspace_root=workspace, projects=[], topics=[self.topic(events=[event])],
                sent_lookup={}, final_index={"items": {}},
            )

        self.assertEqual("spring-forum-2026", item["decision"]["event"])
        self.assertIn("subject_pattern:[SPRING-FORUM]", item["decision"]["event_match_reasons"])
        self.assertEqual("memory/evidence/topics/operations/events/spring-forum-2026/2026-04.md", item["evidence"]["file"])
        self.assertEqual("event_evidence", item["evidence"]["type"])
        self.assertEqual([{"file": event["reference_md"], "type": "event_dossier"}], item["synthesis_targets"])
        self.assertEqual("Topics/Operations", item["action"]["target_folder"])

    def test_subtopic_storage_scope_is_validated_without_parent_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            scoped = self.event(cloud_storage={"scope": "subtopic", "storage_id": "sub-store"})
            self.add_dossier(workspace, scoped)
            valid = classifier.classify_email(self.email("Operations — Course Redesign Spring Forum"), workspace_root=workspace, projects=[], topics=[self.topic(events=[scoped])], sent_lookup={}, final_index={"items": {}})
            invalid = self.event(cloud_storage={"scope": "subtopic", "storage_id": "topic-store"})
            self.add_dossier(workspace, invalid)
            rejected = classifier.classify_email(self.email("Operations — Course Redesign Spring Forum"), workspace_root=workspace, projects=[], topics=[self.topic(events=[invalid])], sent_lookup={}, final_index={"items": {}})

        self.assertEqual("spring-forum-2026", valid["decision"]["event"])
        self.assertNotIn("event", rejected["decision"])
        self.assertIn("invalid_cloud_storage", rejected["decision"]["event_candidates"][0]["reasons"])

    def test_invalid_dates_missing_dossier_duplicate_and_inactive_events_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            invalid_date = self.event(starts_on="04-05-2026")
            self.add_dossier(workspace, invalid_date)
            date_item = classifier.classify_email(self.email("Operations — Course Redesign Spring Forum"), workspace_root=workspace, projects=[], topics=[self.topic(events=[invalid_date])], sent_lookup={}, final_index={"items": {}})
            basic_iso_date = self.event(starts_on="20260504")
            self.add_dossier(workspace, basic_iso_date)
            basic_date_item = classifier.classify_email(self.email("Operations — Course Redesign Spring Forum"), workspace_root=workspace, projects=[], topics=[self.topic(events=[basic_iso_date])], sent_lookup={}, final_index={"items": {}})
            invalid_range = self.event(ends_on="2026-05-01")
            self.add_dossier(workspace, invalid_range)
            range_item = classifier.classify_email(self.email("Operations — Course Redesign Spring Forum"), workspace_root=workspace, projects=[], topics=[self.topic(events=[invalid_range])], sent_lookup={}, final_index={"items": {}})
            duplicate_one = self.event()
            duplicate_two = self.event(title="Duplicate", aliases=[])
            self.add_dossier(workspace, duplicate_one)
            duplicate_item = classifier.classify_email(self.email("Operations — Course Redesign Spring Forum"), workspace_root=workspace, projects=[], topics=[self.topic(events=[duplicate_one, duplicate_two])], sent_lookup={}, final_index={"items": {}})
            missing_item = classifier.classify_email(self.email("Operations — Course Redesign Missing dossier"), workspace_root=workspace, projects=[], topics=[self.topic(events=[self.event(id="missing-dossier", title="Missing dossier", aliases=["Missing dossier"], reference_md="memory/references/topics/operations/subtopics/course-design/events/missing-dossier/index.md")])], sent_lookup={}, final_index={"items": {}})
            inactive = self.event(status="inactive")
            self.add_dossier(workspace, inactive)
            inactive_item = classifier.classify_email(self.email("Operations — Course Redesign Spring Forum"), workspace_root=workspace, projects=[], topics=[self.topic(events=[inactive])], sent_lookup={}, final_index={"items": {}})
            invalid_status = self.event(status="paused")
            self.add_dossier(workspace, invalid_status)
            status_item = classifier.classify_email(self.email("Operations — Course Redesign Spring Forum"), workspace_root=workspace, projects=[], topics=[self.topic(events=[invalid_status])], sent_lookup={}, final_index={"items": {}})
            noncanonical = self.event(reference_md="memory/references/topics/operations/unsafe.md")
            noncanonical_item = classifier.classify_email(self.email("Operations — Course Redesign Spring Forum"), workspace_root=workspace, projects=[], topics=[self.topic(events=[noncanonical])], sent_lookup={}, final_index={"items": {}})

        self.assertIn("invalid_starts_on", date_item["decision"]["event_candidates"][0]["reasons"])
        self.assertIn("invalid_starts_on", basic_date_item["decision"]["event_candidates"][0]["reasons"])
        self.assertIn("invalid_ends_on", range_item["decision"]["event_candidates"][0]["reasons"])
        self.assertNotIn("event", duplicate_item["decision"])
        self.assertIn("event_candidates", duplicate_item["decision"])
        self.assertIn("missing_event_dossier", missing_item["decision"]["event_candidates"][0]["reasons"])
        self.assertNotIn("event", inactive_item["decision"])
        self.assertIn("invalid_event_status", status_item["decision"]["event_candidates"][0]["reasons"])
        self.assertIn("noncanonical_event_reference_md", noncanonical_item["decision"]["event_candidates"][0]["reasons"])

    def test_statusless_event_remains_legacy_active(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            legacy = self.event()
            legacy.pop("status")
            self.add_dossier(workspace, legacy)
            item = classifier.classify_email(self.email("Operations — Course Redesign Spring Forum"), workspace_root=workspace, projects=[], topics=[self.topic(events=[legacy])], sent_lookup={}, final_index={"items": {}})

        self.assertEqual("spring-forum-2026", item["decision"]["event"])

    def test_thread_topic_needs_current_event_signal_and_writes_event_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            event = self.event()
            self.add_dossier(workspace, event)
            item = classifier.classify_email(
                self.email("Re: Course Redesign — Spring Forum", in_reply_to="<parent@example.test>"),
                workspace_root=workspace, projects=[], topics=[self.topic(events=[event])], sent_lookup={},
                final_index={"items": {"parent@example.test": {"final_folder": "Topics/Operations"}}},
            )
            written = evidence.flush_batch_evidence([{"message_id": item["message_id"], "evidence": item["evidence"]}], workspace)

        self.assertEqual("spring-forum-2026", item["decision"]["event"])
        self.assertTrue(written)
        self.assertEqual("event_evidence", item["evidence"]["type"])

    def test_cross_kind_operation_event_conflict_has_no_scalar_or_event_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            event = self.event(typical_subject_patterns=["[SHARED]"])
            self.add_dossier(workspace, event)
            operation = {
                "id": "coordination", "title": "Coordination", "aliases": [],
                "keywords": [], "typical_subject_patterns": ["[SHARED]"], "status": "active",
            }
            item = classifier.classify_email(
                self.email("Operations — Course Redesign [SHARED]"), workspace_root=workspace,
                projects=[], topics=[self.topic(events=[event], operations=[operation])],
                sent_lookup={}, final_index={"items": {}},
            )

        self.assertNotIn("operation", item["decision"])
        self.assertNotIn("event", item["decision"])
        self.assertEqual("cross_kind_conflict", item["decision"]["operation_candidates"][0]["reasons"][-1])
        self.assertEqual("cross_kind_conflict", item["decision"]["event_candidates"][0]["reasons"][-1])
        self.assertEqual("topic_evidence", item["evidence"]["type"])

    def test_cross_kind_conflict_is_fail_closed_for_asymmetric_candidate_sets(self) -> None:
        operation = {
            "id": "coordination", "title": "Coordination", "aliases": [],
            "keywords": [], "typical_subject_patterns": ["[SHARED]"], "status": "active",
        }
        operation_two = dict(operation, id="coordination-two", title="Coordination two")
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            unique_event = self.event(typical_subject_patterns=["[SHARED]"])
            self.add_dossier(workspace, unique_event)
            event_scalar = classifier.classify_email(
                self.email("Operations — Course Redesign [SHARED]"), workspace_root=workspace,
                projects=[], topics=[self.topic(events=[unique_event], operations=[operation, operation_two])],
                sent_lookup={}, final_index={"items": {}},
            )

            event_one = self.event(id="forum-one", title="Forum one", aliases=[],
                                   typical_subject_patterns=["[SHARED]"],
                                   reference_md="memory/references/topics/operations/subtopics/course-design/events/forum-one/index.md")
            event_two = self.event(id="forum-two", title="Forum two", aliases=[],
                                   typical_subject_patterns=["[SHARED]"],
                                   reference_md="memory/references/topics/operations/subtopics/course-design/events/forum-two/index.md")
            self.add_dossier(workspace, event_one)
            self.add_dossier(workspace, event_two)
            operation_scalar = classifier.classify_email(
                self.email("Operations — Course Redesign [SHARED]"), workspace_root=workspace,
                projects=[], topics=[self.topic(events=[event_one, event_two], operations=[operation])],
                sent_lookup={}, final_index={"items": {}},
            )

        for item in (event_scalar, operation_scalar):
            self.assertNotIn("operation", item["decision"])
            self.assertNotIn("event", item["decision"])
            self.assertTrue(item["decision"]["operation_candidates"])
            self.assertTrue(item["decision"]["event_candidates"])
            self.assertTrue(all("cross_kind_conflict" in row["reasons"] for row in item["decision"]["operation_candidates"]))
            self.assertTrue(all("cross_kind_conflict" in row["reasons"] for row in item["decision"]["event_candidates"]))
            self.assertEqual("topic_evidence", item["evidence"]["type"])
            self.assertEqual([], item["synthesis_targets"])


if __name__ == "__main__":
    unittest.main()
