"""Dedocify docs contract: operative Fachverträge ohne Ticket-Labels.

Hermetic documentation contract. Reads the real operative documentation files
shipped in the bundle and asserts that they carry FR-/MD-ticket labels
nowhere: the operative contracts describe function, files and contract
behavior; ticket provenance lives in the System Map (L2), the feature-request
archive and Git history. Imports no production code and touches no mailbox,
network or catalog.

Red-Gate history: at the DOC-R1-001 dispatch the operative docs still carried
204 FR/MD labels (SKILL.md 55, batch-runner.md 91, cli-operations.md 32,
himalaya.md 19, refactor-map.md 2, project-catalog-entry SKILL.md 5); all
tests below failed then and pin the resolved label-free state since.
"""

from __future__ import annotations

from pathlib import Path
import re
import unittest


BUNDLE_ROOT = Path(__file__).resolve().parents[3]

#: Operative Fachverträge: sie werden geladen, um Arbeit auszuführen; sie
#: dürfen keine Ticket-Labels tragen.
OPERATIVE_DOCS = (
    ("skills/mail-desk/SKILL.md",),
    ("skills/mail-desk/references/batch-runner.md",),
    ("skills/mail-desk/references/cli-operations.md",),
    ("skills/mail-desk/references/backends/himalaya.md",),
    ("skills/mail-desk/references/refactor-map.md",),
    ("skills/project-catalog-entry/SKILL.md",),
)

#: Ticket-Label-Muster (FR-04b, FR-15, MD-E1-T01, MD-R9-001, ...).
TICKET_LABEL_PATTERN = re.compile(r"FR-\d+[a-z0-9]*|MD-[A-Z]{1,3}\d*(?:[-–][A-Z0-9]+)?")

#: Je operativem File genau eine Herkunftszeile.
PROVENANCE_LINE_PHRASES = (
    "Vertragshistorie",
    "System Map",
    "docs/features/_archive.md",
)


class _OperativeDocsTestCase(unittest.TestCase):
    def read_doc(self, rel: str) -> str:
        path = BUNDLE_ROOT / rel
        self.assertTrue(path.is_file(), f"documentation file is missing: {path}")
        return path.read_text(encoding="utf-8")


class NoTicketLabelsTests(_OperativeDocsTestCase):
    """Operative Fachverträge tragen keine FR-/MD-Ticket-Labels."""

    def test_operative_docs_are_label_free(self) -> None:
        for (rel,) in OPERATIVE_DOCS:
            text = self.read_doc(rel)
            hits = TICKET_LABEL_PATTERN.findall(text)
            with self.subTest(doc=rel):
                self.assertEqual(
                    [],
                    hits,
                    f"{rel} must be free of FR-/MD-ticket labels "
                    f"(provenance lives in System Map/archive/Git); found: {hits[:10]}",
                )


class ProvenanceLineTests(_OperativeDocsTestCase):
    """Je operativem File genau eine Herkunftszeile zur Vertragshistorie."""

    def test_each_operative_doc_has_exactly_one_provenance_line(self) -> None:
        for (rel,) in OPERATIVE_DOCS:
            text = self.read_doc(rel)
            with self.subTest(doc=rel):
                for phrase in PROVENANCE_LINE_PHRASES:
                    self.assertIn(
                        phrase,
                        text,
                        f"{rel} must carry the single provenance line pointing to "
                        f"System Map + docs/features/_archive.md ({phrase!r} missing)",
                    )
                # Genau eine Zeile mit 'Vertragshistorie'
                lines = [line for line in text.splitlines() if "Vertragshistorie" in line]
                self.assertEqual(
                    1,
                    len(lines),
                    f"{rel} must carry exactly one provenance line, got {len(lines)}",
                )


class SystemMapUnchangedTests(unittest.TestCase):
    """Die L2-System-Map bleibt Historie-Heimat (Label-Freiheit gilt nicht dort)."""

    def test_l2_map_still_carries_ticket_labels(self) -> None:
        path = BUNDLE_ROOT / "skills" / "mail-desk" / "docs" / "system-map" / "README.md"
        self.assertTrue(path.is_file())
        text = path.read_text(encoding="utf-8")
        self.assertGreaterEqual(
            len(TICKET_LABEL_PATTERN.findall(text)),
            50,
            "L2 system map keeps FR/MD labels as canonical provenance layer",
        )


if __name__ == "__main__":
    unittest.main()