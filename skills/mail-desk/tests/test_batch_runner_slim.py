"""Batch-runner slimming contract (DOC-S1).

Hermetic documentation contract for ``references/batch-runner.md``. Pins the
size gate and a no-loss floor of contract vocabulary; imports no production
code.

Red-Gate history: at the DOC-S1-001 dispatch the file carried 8754 words
(1643 lines, 40% JSON examples) and failed the size gate below; the no-loss
floors (sections, contract vocabulary, schema fields) passed then and pin the
slimmed, contract-complete state since.
"""

from __future__ import annotations

from pathlib import Path
import re
import unittest


BUNDLE_ROOT = Path(__file__).resolve().parents[3]
DOC = BUNDLE_ROOT / "skills" / "mail-desk" / "references" / "batch-runner.md"

#: Size gate: 20-40% reduction from the 8754-word baseline.
MIN_WORDS = 5300
MAX_WORDS = 7100

#: All mode/section headings must survive (no section removal).
REQUIRED_SECTIONS = (
    "## Zweck & Architektur",
    "## Einheitliche Standard-Dateinamen",
    "## CLI-Aufrufe & Parameter",
    "## Modus: `dossier`",
    "## Modus: `dossier_apply`",
    "## Modus: `dossier_synthesis`",
    "## Modus: `dossier_handoff`",
    "## Modus 1: `inspect`",
    "## Modus 2: `execute`",
    "## Modus 3: `verify`",
    "## Modus 4: `search`",
    "## Modus 5: `resolve`",
    "## Modus: `reconcile`",
    "## Desk-Signals-Katalog",
    "## Live-Fortschritts-Monitoring",
    "## Final-Index- und Batch-Importregeln",
    "## Fehlerbehandlung & Sicherheit",
    "## Materialitäts-Gate und LLM-Handoff",
    "## Katalog- und Filemap-gestützter Ablagevorschlag",
    "## Policygebundener Anhang-Evaluierungs-Orchestrator",
    "## Draft-Integration",
)

#: Contract vocabulary floor: status/reason values and contract fields that
#: must remain documented in batch-runner.md (terms documented in sibling
#: operative docs - SKILL.md/cli-operations.md - are not required here).
CONTRACT_VOCABULARY = (
    "lock_unavailable",
    "no_attachments",
    "no_allowed_attachments",
    "handoff_ready",
    "still_ambiguous",
    "policy_blocked",
    "quota_exceeded",
    "fetch_failed",
    "extraction_failed",
    "handoff_invalid",
    "classification_clear",
    "skipped_count_limit",
    "skipped_inline_limit",
    "allowed",
    "max_attachments_per_message",
    "max_inline_per_message",
    "expected_count",
    "allow_fewer",
    "review_hash",
    "approval_receipt",
    "batch-manifest.json",
    "batch-draft.json",
    "final-location-index.json",
    "runner-progress.json",
    "workspace-lease-id",
    "workspace-conversation-id",
    "--draft",
    "--pipeline",
    "--reconcile",
    "envelope_id",
    "message_id",
    "run_id",
    "policy_status",
)

#: Provenance line must stay exactly once.
PROVENANCE_PHRASE = "Vertragshistorie"


class BatchRunnerSlimTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(DOC.is_file(), f"missing: {DOC}")
        self.text = DOC.read_text(encoding="utf-8")

    def test_size_gate(self) -> None:
        words = len(self.text.split())
        self.assertGreaterEqual(
            words,
            MIN_WORDS,
            f"batch-runner.md must stay informative (>= {MIN_WORDS} words), got {words}",
        )
        self.assertLessEqual(
            words,
            MAX_WORDS,
            f"batch-runner.md must stay slim (<= {MAX_WORDS} words), got {words}",
        )

    def test_all_sections_survive(self) -> None:
        for heading in REQUIRED_SECTIONS:
            with self.subTest(section=heading):
                self.assertIn(heading, self.text)

    def test_contract_vocabulary_floor(self) -> None:
        for term in CONTRACT_VOCABULARY:
            with self.subTest(term=term):
                self.assertIn(term, self.text)

    def test_schemas_keep_field_definitions(self) -> None:
        # JSON-Schemata sind Vertragsdaten: jede property-Definition bleibt.
        fence = chr(96) * 3
        schema_blocks = re.findall(fence + r"json\n(.*?)" + fence, self.text, re.DOTALL)
        schema_text = "\n".join(schema_blocks)
        for field in (
            '"mode"', '"account"', '"folder"', '"items"', '"decision"',
            '"attachment_evaluation"', '"review"',
        ):
            with self.subTest(field=field):
                self.assertIn(field, schema_text)

    def test_provenance_line_still_single(self) -> None:
        lines = [line for line in self.text.splitlines() if PROVENANCE_PHRASE in line]
        self.assertEqual(1, len(lines))


if __name__ == "__main__":
    unittest.main()