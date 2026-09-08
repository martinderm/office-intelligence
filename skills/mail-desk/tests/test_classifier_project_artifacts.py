"""FR-02a contracts for catalog-backed project artifact matching."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest


MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

from core import classifier  # noqa: E402


class ProjectArtifactClassifierTests(unittest.TestCase):
    @staticmethod
    def meshe() -> dict:
        return {
            "id": "meshe",
            "kuerzel": "MESHE",
            "title": "Microcredentials Exchange System",
            "mailbox_folder": "Projects/MESHE",
            "schema_version": 3,
            "workpackages": [
                {
                    "id": "wp1",
                    "number": 1,
                    "title": "Quality and Evaluation",
                    "status": "active",
                    "tasks": [{"id": "T1.7", "title": "Quality Plan"}],
                    "deliverables": [{"id": "D1.2", "title": "Quality Handbook"}],
                },
                {
                    "id": "wp2",
                    "number": 2,
                    "title": "Advocacy and Motivation Pack on Micro-Credentials for Learners",
                    "status": "active",
                    "keywords": ["focus groups"],
                    "tasks": [{"id": "T2.2", "title": "FDGs"}],
                    "deliverables": [],
                },
            ],
            "milestones": [{"id": "MS5", "title": "Evaluation completed"}],
        }

    def classify(self, subject: str, *, project: dict | None = None, **email: object) -> dict:
        final_index = email.pop("final_index", {"items": {}})
        return classifier.classify_email(
            {"subject": subject, "message_id": "fr02a@example.test", **email},
            projects=[project or self.meshe()],
            topics=[],
            sent_lookup={},
            final_index=final_index,
        )

    def test_meshe_wp2_and_t22_codes_are_selected_with_reasons(self) -> None:
        item = self.classify("MESHE WP2 / T2.2 Focus Group")
        decision = item["decision"]

        self.assertEqual("meshe", decision["id"])
        self.assertEqual("wp2", decision["workpackage"])
        self.assertEqual("T2.2", decision["task"])
        self.assertIn("exact_code:wp2", decision["artifact_match_reasons"]["workpackage"])
        self.assertIn("exact_code:T2.2", decision["artifact_match_reasons"]["task"])

    def test_task_and_deliverable_codes_infer_the_common_workpackage(self) -> None:
        decision = self.classify("MESHE T1.7 and D1.2 draft")["decision"]

        self.assertEqual("wp1", decision["workpackage"])
        self.assertEqual("T1.7", decision["task"])
        self.assertEqual("D1.2", decision["deliverable"])
        self.assertEqual(["inferred_from_nested_artifact"], decision["artifact_match_reasons"]["workpackage"])

    def test_project_level_milestone_is_selected(self) -> None:
        decision = self.classify("MESHE MS5 update")["decision"]

        self.assertEqual("MS5", decision["milestone"])
        self.assertIn("exact_code:MS5", decision["artifact_match_reasons"]["milestone"])

    def test_unique_descriptive_term_matches_without_a_code(self) -> None:
        decision = self.classify("MESHE: FDGs invitation")["decision"]

        self.assertEqual("wp2", decision["workpackage"])
        self.assertEqual("T2.2", decision["task"])
        self.assertIn("title:FDGs", decision["artifact_match_reasons"]["task"])

    def test_wp2_focus_group_does_not_invent_an_unproven_task(self) -> None:
        decision = self.classify("MESHE WP2 BOKU Focus Group")["decision"]

        self.assertEqual("wp2", decision["workpackage"])
        self.assertNotIn("task", decision)

    def test_descriptive_term_does_not_match_inside_a_larger_word(self) -> None:
        project = self.meshe()
        project["workpackages"][0]["tasks"] = [{"id": "T1.8", "title": "Audit"}]

        decision = self.classify("MESHE preauditing note", project=project)["decision"]

        self.assertNotIn("task", decision)
        self.assertNotIn("artifact_candidates", decision)

    def test_ambiguous_descriptive_term_exposes_candidates_without_scalar(self) -> None:
        project = self.meshe()
        project["workpackages"][0]["tasks"].append({"id": "T1.8", "title": "Shared Workshop"})
        project["workpackages"][1]["tasks"].append({"id": "T2.3", "title": "Shared Workshop"})

        decision = self.classify("MESHE Shared Workshop", project=project)["decision"]

        self.assertNotIn("task", decision)
        self.assertEqual(["T1.8", "T2.3"], [row["id"] for row in decision["artifact_candidates"]["task"]])

    def test_legacy_project_without_v3_structures_remains_root_compatible(self) -> None:
        legacy = {
            "id": "meshe",
            "kuerzel": "MESHE",
            "title": "Legacy MESHE",
            "mailbox_folder": "Projects/MESHE",
            "schema_version": 2,
        }
        decision = self.classify("MESHE T1.7", project=legacy)["decision"]

        self.assertEqual("meshe", decision["id"])
        self.assertFalse({"workpackage", "task", "deliverable", "milestone"} & set(decision))
        self.assertNotIn("artifact_candidates", decision)

    def test_thread_inherits_project_but_resolves_current_mail_artifact(self) -> None:
        item = self.classify(
            "Re: unrelated subject",
            preview="Please review T1.7 today.",
            in_reply_to="<parent@example.test>",
            final_index={"items": {"parent@example.test": {"final_folder": "Projects/MESHE"}}},
        )
        decision = item["decision"]

        self.assertEqual("meshe", decision["id"])
        self.assertEqual("T1.7", decision["task"])
        self.assertEqual("wp1", decision["workpackage"])


if __name__ == "__main__":
    unittest.main()
