"""Documentation contract for the post-batch synthesis / context consultation (Issue #64).

Hermetic: reads only the shipped documentation files and imports no production
code, touches no mailbox, network or catalog.
"""

from __future__ import annotations

from pathlib import Path
import re
import unittest


BUNDLE_ROOT = Path(__file__).resolve().parents[3]
MAIL_DESK = BUNDLE_ROOT / "skills" / "mail-desk"
SYNTHESIS = MAIL_DESK / "references" / "synthesis.md"
BATCH_RUNNER = MAIL_DESK / "references" / "batch-runner.md"
SKILL = MAIL_DESK / "SKILL.md"
TASK_DESK_DOCS = (
    BUNDLE_ROOT / "skills" / "task-desk" / "SKILL.md",
    BUNDLE_ROOT / "skills" / "task-desk" / "references" / "task-schemas.md",
)
NO_LABELS_TEST = MAIL_DESK / "tests" / "test_docs_no_ticket_labels.py"

#: Sections that moved out of batch-runner.md and must now live in synthesis.md.
MOVED_SECTIONS = (
    "## Post-Batch-Telemetrie",
    "## Syntheseziele",
    "## Synthese-Handoff",
    "## Completion-Gate und Luna-Betriebsprofil",
)

#: Schema/vocabulary of the moved sections that must survive unchanged.
MOVED_VOCABULARY = (
    "affected_projects",
    "affected_topics",
    "synthesis_required",
    "synthesis_targets",
    "recommended_action",
    "task_anchor",
    "synthesis_candidate",
    "synthesis_handoff",
    "completion_report",
    "target_selection_required",
    "not_required",
    "verification_required",
    "recovery_required",
    "dossier_apply",
    "reconcile",
    "schema_version",
    "Luna",
)


def _read(path: Path) -> str:
    assert path.is_file(), f"missing: {path}"
    return path.read_text(encoding="utf-8")


class SynthesisContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = _read(SYNTHESIS)

    def test_three_search_paths_in_fixed_order(self) -> None:
        positions = [
            self.text.index("Event-Dossier."),
            self.text.index("Subtopic-Spezialnotiz und Steuerdateien."),
            self.text.index("Cloud-Atlas."),
        ]
        self.assertEqual(sorted(positions), positions)
        for needle in (
            "memory/references/projects/<project>/events/<event>/index.md",
            "memory/references/topics/<topic>/subtopics/<subtopic>/events/<event>/index.md",
            "subtopics[].reference_md",
            "signals.md",
            "contacts.md",
            "statusampel-*.md",
            "workpackages/*.md",
            "cloud_sync",
            "cloud_atlas_preflight",
            "projects.json",
            "topics.json",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, self.text)

    def test_single_home_and_evidence_role(self) -> None:
        for needle in (
            "Single Home",
            "Evidenzlog",
            "chronologische",
            "`Message-ID`-Anker",
            "verweisen nur",
            "Event-Dossier > Subtopic-Spezialnotiz > Operations-/Projekt-/Topic-Steuerdatei",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, self.text)

    def test_report_contract_and_review_duty(self) -> None:
        for needle in (
            '"context"',
            '"canonical_file"',
            '"message_ids"',
            '"outcome"',
            '"reason"',
            "`updated`",
            "`linked`",
            "`noop`",
            "`review_required`",
            "unvollständig",
            "event-documentation",
            "untrusted",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, self.text)

    def test_moved_sections_and_vocabulary_live_in_synthesis(self) -> None:
        for heading in MOVED_SECTIONS:
            with self.subTest(heading=heading):
                self.assertIn(heading, self.text)
        for term in MOVED_VOCABULARY:
            with self.subTest(term=term):
                self.assertIn(term, self.text)

    def test_stop_conditions_documented(self) -> None:
        self.assertIn("## Stopbedingungen", self.text)
        for needle in ("fehlenden Projekt-/Topic-Katalogen", "doppelten Event-IDs", "nicht deklarierter Cloud-Ressource"):
            with self.subTest(needle=needle):
                self.assertIn(needle, self.text)

    def test_single_provenance_line_without_ticket_labels(self) -> None:
        lines = [line for line in self.text.splitlines() if "Vertragshistorie" in line]
        self.assertEqual(1, len(lines))
        self.assertIn("docs/system-map", self.text)
        self.assertIn("docs/features/_archive.md", self.text)
        self.assertEqual([], re.findall(r"FR-\d+[a-z0-9]*|MD-[A-Z]{1,3}\d*(?:[-–][A-Z0-9]+)?", self.text))


class CrossReferenceTests(unittest.TestCase):
    def test_batch_runner_points_to_synthesis_and_no_longer_hosts_moved_sections(self) -> None:
        text = _read(BATCH_RUNNER)
        self.assertIn("[`synthesis.md`](synthesis.md)", text)
        self.assertIn("## Post-Batch-Kontext-Konsultation", text)
        self.assertIn("synthesis_targets: []", text)
        for heading in MOVED_SECTIONS:
            with self.subTest(heading=heading):
                self.assertNotIn(heading, text)

    def test_skill_points_to_synthesis(self) -> None:
        text = _read(SKILL)
        self.assertIn("references/synthesis.md", text)
        self.assertIn("Pflicht-Kontext-Konsultation", text)

    def test_task_desk_cross_references_target_synthesis(self) -> None:
        for path in TASK_DESK_DOCS:
            text = _read(path)
            with self.subTest(doc=path.name):
                self.assertIn("synthesis.md", text)
                self.assertNotIn("batch-runner.md", text)

    def test_synthesis_is_in_operative_docs_label_gate(self) -> None:
        self.assertIn("skills/mail-desk/references/synthesis.md", _read(NO_LABELS_TEST))


if __name__ == "__main__":
    unittest.main()
