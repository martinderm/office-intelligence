"""FR-03a contracts for conservative, catalog-backed subtopic resolution."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest


MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

from core import classifier, evidence  # noqa: E402


class SubtopicClassifierTests(unittest.TestCase):
    @staticmethod
    def topic(*, subtopics: list[dict] | None = None) -> dict:
        return {
            "id": "operations",
            "title": "Operations",
            "mailbox_folder": "Topics/Operations",
            "aliases": ["Operations"],
            "keywords": ["operational"],
            "subtopics": subtopics if subtopics is not None else [{
                "id": "course-design",
                "title": "Course Design",
                "aliases": ["Course Redesign"],
                "keywords": ["micro credential"],
                "typical_subject_patterns": ["[RPL-2026]"],
                "contacts": [{"email": "course@example.test"}],
                "status": "active",
            }],
        }

    @staticmethod
    def cagliari_topics() -> list[dict]:
        """Focused fixture copied from the BOKU Dienstreisen/Netzwerke contract."""
        return [
            {
                "id": "dienstreisen",
                "title": "Dienstreisen",
                "mailbox_folder": "Administratives/Dienstreisen",
                "aliases": [],
                "keywords": ["Dienstreise", "Reise"],
                "typical_subject_patterns": ["A1-Formular zu DR-Auftrag"],
                "subtopics": [{
                    "id": "2026-06-cagliari-eucen-conference",
                    "title": "Cagliari 2026-06 — IACEE Symposium & EUCEN Conference",
                    "aliases": ["Cagliari 2026-06", "IACEE Symposium 2026"],
                    "keywords": ["Cagliari", "IACEE"],
                    "typical_subject_patterns": [
                        "Cagliari",
                        "IACEE Symposium 2026",
                        "Join Us in Cagliari",
                        "Response submission for Registration Form 56th EUCEN Annual Conference",
                    ],
                    "contacts": [
                        {"email": "spol@unica.it"},
                        {"email": "alice.sgualdini@unica.it"},
                    ],
                    "status": "active",
                }],
            },
            {
                "id": "netzwerke",
                "title": "Netzwerke",
                "mailbox_folder": "Themen/Netzwerke",
                "aliases": ["Networks"],
                "keywords": ["EUCEN"],
                "typical_subject_patterns": ["We are EUCEN", "EUCEN"],
                "subtopics": [{
                    "id": "eucen",
                    "title": "EUCEN",
                    "aliases": ["European University Continuing Education Network", "We are EUCEN"],
                    "keywords": ["EUCEN", "eucen annual conference"],
                    "contacts": [],
                    "status": "active",
                }],
            },
        ]

    @staticmethod
    def email(subject: str, **extra: object) -> dict:
        return {
            "envelope_id": "subtopic-1",
            "folder": "INBOX",
            "message_id": "<subtopic-1@example.test>",
            "subject": subject,
            "from": "Coordinator <general@example.test>",
            "to": "Martin <martin@example.test>",
            "date": "2026-06-10",
            **extra,
        }

    def classify(self, subject: str, *, topic: dict | None = None, **extra: object) -> dict:
        return classifier.classify_email(
            self.email(subject, **extra),
            projects=[], topics=[topic or self.topic()], sent_lookup={}, final_index={"items": {}},
        )

    def test_alias_and_subtopic_only_alias_preserve_parent_routing(self) -> None:
        direct = self.classify("Operations: Course Redesign")
        fallback = self.classify("Course Redesign timetable")

        for item in (direct, fallback):
            self.assertEqual("Topics/Operations", item["action"]["target_folder"])
            self.assertEqual("operations", item["decision"]["id"])
            self.assertEqual("course-design", item["decision"]["subtopic"])
            self.assertIn("subject_alias:Course Redesign", item["decision"]["subtopic_match_reasons"])

    def test_keyword_and_subject_pattern_are_resolved_after_parent_selection(self) -> None:
        keyword = self.classify("Operations update", preview="New micro credential material")
        pattern = self.classify("[RPL-2026] deadline")
        subject_keyword = self.classify("Micro credential timetable")

        self.assertEqual("course-design", keyword["decision"]["subtopic"])
        self.assertIn("keyword:micro credential", keyword["decision"]["subtopic_match_reasons"])
        self.assertEqual("course-design", pattern["decision"]["subtopic"])
        self.assertIn("subject_pattern:[RPL-2026]", pattern["decision"]["subtopic_match_reasons"])
        self.assertEqual("operations", subject_keyword["decision"]["id"])
        self.assertEqual("course-design", subject_keyword["decision"]["subtopic"])

    def test_preview_keyword_alone_does_not_select_a_parent_topic(self) -> None:
        item = self.classify("Weekly update", preview="New micro credential material")

        self.assertEqual("unknown", item["decision"]["kind"])
        self.assertEqual("INBOX", item["action"]["target_folder"])

    def test_contact_requires_independent_parent_subject_signal(self) -> None:
        identified = classifier.classify_email(
            self.email("Operations weekly update", **{"from": "Lead <course@example.test>"}),
            projects=[], topics=[self.topic()], sent_lookup={}, final_index={"items": {}},
        )
        contact_only = classifier.classify_email(
            self.email("Weekly update", **{"from": "Lead <course@example.test>"}),
            projects=[], topics=[self.topic()], sent_lookup={}, final_index={"items": {}},
        )

        self.assertEqual("course-design", identified["decision"]["subtopic"])
        self.assertIn("unique_contact:course@example.test", identified["decision"]["subtopic_match_reasons"])
        self.assertEqual("unknown", contact_only["decision"]["kind"])

    def test_subtopic_only_signal_ambiguous_across_topics_fails_closed(self) -> None:
        other_topic = {
            "id": "other", "title": "Other", "mailbox_folder": "Topics/Other",
            "subtopics": [{"id": "other-sub", "title": "Other sub", "aliases": ["Shared signal"], "status": "active"}],
        }
        item = classifier.classify_email(
            self.email("Shared signal"), projects=[],
            topics=[self.topic(subtopics=[{"id": "ops-sub", "title": "Ops sub", "aliases": ["Shared signal"], "status": "active"}]), other_topic],
            sent_lookup={}, final_index={"items": {}},
        )

        self.assertEqual("unknown", item["decision"]["kind"])
        self.assertEqual("INBOX", item["action"]["target_folder"])

    def test_cagliari_subject_patterns_beat_generic_eucen_but_keep_generic_network_routing(self) -> None:
        expected_cagliari = "2026-06-cagliari-eucen-conference"
        cagliari_subjects = [
            "Response submission for Registration Form 56th EUCEN Annual Conference",
            "Join Us in Cagliari | IACEE Symposium 2026 | Register Today",
            "IACEE/Cagliari",
        ]
        for subject in cagliari_subjects:
            with self.subTest(subject=subject):
                item = classifier.classify_email(
                    self.email(subject), projects=[], topics=self.cagliari_topics(),
                    sent_lookup={}, final_index={"items": {}},
                )
                self.assertEqual("dienstreisen", item["decision"]["id"])
                self.assertEqual(expected_cagliari, item["decision"]["subtopic"])
                self.assertEqual("Administratives/Dienstreisen", item["action"]["target_folder"])

        for subject in ("EUCEN Annual Conference", "We are EUCEN"):
            with self.subTest(subject=subject):
                item = classifier.classify_email(
                    self.email(subject), projects=[], topics=self.cagliari_topics(),
                    sent_lookup={}, final_index={"items": {}},
                )
                self.assertEqual("netzwerke", item["decision"]["id"])
                self.assertEqual("eucen", item["decision"]["subtopic"])
                self.assertEqual("Themen/Netzwerke", item["action"]["target_folder"])

        contact_only = classifier.classify_email(
            self.email("Weekly update", **{"from": "Contact <spol@unica.it>"}),
            projects=[], topics=self.cagliari_topics(), sent_lookup={}, final_index={"items": {}},
        )
        self.assertEqual("unknown", contact_only["decision"]["kind"])
        self.assertEqual("INBOX", contact_only["action"]["target_folder"])

    def test_ambiguous_and_inactive_subtopics_do_not_create_scalar(self) -> None:
        ambiguous_topic = self.topic(subtopics=[
            {"id": "one", "title": "One", "aliases": ["Shared signal"], "status": "active"},
            {"id": "two", "title": "Two", "aliases": ["Shared signal"], "status": "active"},
        ])
        ambiguous = self.classify("Operations — Shared signal", topic=ambiguous_topic)
        inactive_topic = self.topic(subtopics=[
            {"id": "archived", "title": "Archived", "aliases": ["Old record"], "status": "inactive"},
        ])
        inactive = self.classify("Operations — Old record", topic=inactive_topic)

        self.assertNotIn("subtopic", ambiguous["decision"])
        self.assertEqual(["one", "two"], [row["id"] for row in ambiguous["decision"]["subtopic_candidates"]])
        self.assertNotIn("subtopic", inactive["decision"])
        self.assertEqual("Topics/Operations", inactive["action"]["target_folder"])

    def test_legacy_subtopic_without_status_is_active_and_root_remains_compatible(self) -> None:
        legacy_subtopic = self.topic(subtopics=[
            {"id": "legacy", "title": "Legacy", "aliases": ["Legacy subtopic"]},
        ])
        legacy = self.classify("Legacy subtopic notice", topic=legacy_subtopic)
        root_only = self.classify("Operations notice", topic=self.topic(subtopics=[]))

        self.assertEqual("legacy", legacy["decision"]["subtopic"])
        self.assertEqual("Topics/Operations", root_only["action"]["target_folder"])
        self.assertNotIn("subtopic", root_only["decision"])
        self.assertIsNone(root_only["evidence"])
        self.assertEqual([], root_only["synthesis_targets"])

    def test_subtopic_evidence_and_optional_safe_target_are_catalog_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            reference = "memory/references/topics/operations/subtopics/course-design.md"
            target = workspace / reference
            target.parent.mkdir(parents=True)
            target.write_text("# Course Design\n", encoding="utf-8")
            topic = self.topic()
            topic["subtopics"][0]["reference_md"] = reference
            item = classifier.classify_email(
                self.email("Operations — Course Redesign", synthesis_targets=[{"file": "untrusted.md"}]),
                workspace_root=workspace,
                projects=[], topics=[topic], sent_lookup={}, final_index={"items": {}},
            )

        self.assertEqual("memory/evidence/topics/operations/2026-06.md", item["evidence"]["file"])
        self.assertIn("Message-ID: `subtopic-1@example.test`", item["evidence"]["entry"])
        self.assertIn("[OPERATIONS | course-design (Course Design)]", item["evidence"]["entry"])
        self.assertEqual([{"file": reference, "type": "subtopic_reference"}], item["synthesis_targets"])

    def test_thread_inherited_topic_resolves_current_subtopic_and_writes_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            item = classifier.classify_email(
                self.email("Re: Course Redesign", in_reply_to="<parent@example.test>"),
                workspace_root=workspace,
                projects=[], topics=[self.topic()], sent_lookup={},
                final_index={"items": {"parent@example.test": {"final_folder": "Topics/Operations"}}},
            )
            written = evidence.flush_batch_evidence(
                [{"message_id": item["message_id"], "evidence": item["evidence"]}], workspace
            )

            self.assertEqual("course-design", item["decision"]["subtopic"])
            self.assertTrue(written)
            self.assertTrue((workspace / item["evidence"]["file"]).is_file())


if __name__ == "__main__":
    unittest.main()
