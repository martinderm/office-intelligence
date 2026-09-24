"""FR-24/MD-R9 skill-routing contract tests.

Hermetic documentation contract. This module reads the real documentation files
shipped in the bundle and asserts that MD-R9's routing obligations are met; it
imports no production code and touches no mailbox, network or catalog.

Red-Gate history: at the MD-R9-001 dispatch the SKILL.md description still said
"fuehrt keine Massenpipeline aus" without anchoring batch pipelines
(draft-execute-verify) as routing *through* this skill, and the pre-archive
record ``docs/features/FR-24.md`` (now ``docs/features/_archive.md`` per the
archival rule) did not yet carry the canonical mandatory load block
(Pflicht-Ladeblock) nor the consumer-migration hint (Phase 0 in
``pipelines/mail-desk-batch.md``). All five tests failed then and pin the
resolved state since.
"""

from __future__ import annotations

from pathlib import Path
import re
import unittest


BUNDLE_ROOT = Path(__file__).resolve().parents[3]
SKILL_DOC = BUNDLE_ROOT / "skills" / "mail-desk" / "SKILL.md"
#: FR-24 lebt nach Abschluss im Langzeit-Archiv (Archivierungsregel:
#: Record-Files existieren nur für aktive FRs).
FR24_ARCHIVE = BUNDLE_ROOT / "docs" / "features" / "_archive.md"

#: The old self-excluding phrase must no longer stand uncorrected in the
#: description: batch pipelines must be anchored through this skill instead.
NEGATIVE_DESCRIPTION_PHRASE = "führt keine Massenpipeline aus"

#: Routing anchor phrases the description must carry (case-insensitive match).
DESCRIPTION_ANCHOR_PATTERNS = (
    r"(?i)batch[- ]?pipelines?|massenverarbeitung|stapelverarbeitung|batch-läufe|batchläufe",
    r"(?i)draft.{0,12}execute.{0,12}verify",
)

#: Canonical mandatory load block artifacts (Pflicht-Ladeblock).
LOAD_BLOCK_ARTIFACTS = (
    "Pflicht-Ladeblock",
    "skills/mail-desk/SKILL.md",
    "references/cli-operations.md",
    "references/backends/himalaya.md",
)

#: Consumer migration obligations the record must document.
MIGRATION_ARTIFACTS = (
    "pipelines/mail-desk-batch.md",
    "Phase 0",
    "Pipeline und Fachvertrag",
)


class _RoutingContractTestCase(unittest.TestCase):
    """Shared read-only helpers; declares no test methods of its own."""

    def read_doc(self, path: Path) -> str:
        self.assertTrue(path.is_file(), f"documentation file is missing: {path}")
        return path.read_text(encoding="utf-8")


class SkillDescriptionRoutingTests(_RoutingContractTestCase):
    """The SKILL.md description routes batch work *through* mail-desk."""

    def test_description_no_longer_self_excludes_for_batch_work(self) -> None:
        text = self.read_doc(SKILL_DOC)
        self.assertNotIn(
            NEGATIVE_DESCRIPTION_PHRASE,
            text,
            "SKILL.md description must no longer contain the self-excluding "
            "phrase 'führt keine Massenpipeline aus' (FR-24/MD-R9)",
        )

    def test_description_anchors_batch_pipelines_through_this_skill(self) -> None:
        text = self.read_doc(SKILL_DOC)
        first_line_block = text.split("# mail-desk", 1)[0]
        for pattern in DESCRIPTION_ANCHOR_PATTERNS:
            with self.subTest(pattern=pattern):
                self.assertRegex(first_line_block, pattern)


class FR24ArchiveLoadBlockTests(_RoutingContractTestCase):
    """The FR-24 archive section carries the canonical mandatory load block."""

    def test_record_contains_canonical_load_block(self) -> None:
        text = self.read_doc(FR24_ARCHIVE)
        for artifact in LOAD_BLOCK_ARTIFACTS:
            with self.subTest(artifact=artifact):
                self.assertIn(artifact, text)

    def test_record_documents_consumer_migration_hint(self) -> None:
        text = self.read_doc(FR24_ARCHIVE)
        for artifact in MIGRATION_ARTIFACTS:
            with self.subTest(artifact=artifact):
                self.assertIn(artifact, text)

    def test_record_migration_hint_is_copyable_block(self) -> None:
        text = self.read_doc(FR24_ARCHIVE)
        # The archive section pins the binding load ORDER (1./2./3.) as
        # prose; the copyable fenced block lives in the Git history of the
        # pre-archive record file.
        self.assertIn("Reihenfolge\n  bindend", text)
        self.assertRegex(text, r"1\. `skills/mail-desk/SKILL\.md`")
        self.assertRegex(text, r"2\. gew\u00e4hlte[\s\S]{0,80}Adapter-Referenz")
        self.assertRegex(text, r"3\. `references/cli-operations\.md`")


if __name__ == "__main__":
    unittest.main()