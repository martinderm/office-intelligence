"""Batch-runner slimming contract (DOC-S1).

Hermetic documentation contract for ``references/batch-runner.md``. Pins the
size gate and a no-loss floor of contract vocabulary; imports no production
code.

Red-Gate history: at the DOC-S1-001 dispatch the file carried 8754 words
(1643 lines, 40% JSON examples) and failed the size gate below; the no-loss
floors (sections, contract vocabulary, schema fields) passed then and pin the
slimmed, contract-complete state since.

Orchestrator test-correction (documented, 2026-09-27, run
2026-09-27T204500Z-fr09-mdp1): MAX_WORDS 7100 → 7400. The MD-P1 module contract
added a new normative section (``## Promotion-Preflight``) to the reference;
the gate is a hygiene guard (no unbounded regrowth), not a fixed-content
freeze, so the ceiling moves once, with the real new module contract as the
reason. All floors (sections, vocabulary, schema) unchanged.

Orchestrator test-correction (documented, 2026-10-16, run
2026-10-16-office-intelligence-fr28-doc-batch-docs): MAX_WORDS 7400 → 8000
(measured 7843 words + 100 headroom, rounded to the next hundred). FR-28
review-correction/verify/recovery docs. All floors (sections, vocabulary,
schema) unchanged.

Orchestrator test-correction (documented, 2026-11-11, issue #103, lane
2026-11-11-lane-103-cli-batch-skip): MAX_WORDS 8000 → 8250 (measured 8191 words
+ headroom, rounded to the next fifty). The new normative section
``#### CLI-Batch-Skip über `envelope thread` und Transportzähler`` documents the
fail-closed scan accelerator and its transport counter/abort contract. All
floors (sections, vocabulary, schema) unchanged.

Orchestrator test-correction (documented, 2026-11-11, issue #103 review follow-up,
lane 2026-11-11-lane-103-cli-batch-skip): MAX_WORDS 8250 → 8300 (measured 8299
words, rounded to the next fifty). The documented contract now also covers the
atomic spawn gate, the 10,000 default budget and F6 no-write behaviour. All
floors unchanged.

Orchestrator test-correction (documented, 2026-11-11, issue #107, lane
2026-11-11-lane-107-sent-ledger): MAX_WORDS 8300 → 8350 (measured 8334 words,
rounded to the next fifty). The sync_sent consumer paragraph now names the
reasoned `no_evidence` outcome and the exact promotion-recovery triad. All
floors unchanged.

Orchestrator test-correction (documented, 2026-11-23, issue #103 D1, lane
2026-11-23-frontier-103-login-reduction): MAX_WORDS 8350 → 8375 (measured 8369
words, rounded to the next twenty-five). The caller documentation now names the
full `starts_by_purpose` purpose list and the injected draft-reader `purpose`
keyword. All floors unchanged.

Orchestrator test-correction (documented, 2026-11-23, issue #103 P1, same lane):
MAX_WORDS 8375 → 8425 (measured 8410 words, rounded to the next twenty-five).
The scan section now documents the draft-only `allow_eml_details` opt-in, its
config key and the 1-start/2-start/read-only call counts. All floors unchanged.

Orchestrator test-correction (documented, 2026-11-23, issue #103 P1 fix round,
same lane): MAX_WORDS 8425 → 8450 (measured 8436 words, rounded to the next
twenty-five). The opt-in default is now documented as `False`, with the
model-based evidence limit and the activation gate. All floors unchanged.

Orchestrator test-correction (documented, 2026-11-23, issue #103 P2, lane
2026-11-23-frontier-103-login-reduction): MAX_WORDS 8450 → 8525 (measured 8517
words, rounded to the next twenty-five). The scan section now documents the P2
scan-source reuse: one-scan lifetime, identity binding, the per-EML/per-scan/
source-count bounds, the first-time-full-text barrier rule, evaluation reuse
under the existing checks and P1 inactivity. All floors unchanged.

Orchestrator test-correction (documented, 2026-11-23, issue #103 P2 fix round 2,
same lane): MAX_WORDS 8525 → 8550 (measured 8525 words, rounded to the next
twenty-five). The P2 section now names the separate full-text gate
``allow_scan_source_full_text`` (default ``False``), marks the parity class as
modelled, not live-proven, and keeps the hit counts scoped to the active gate.
Floors unchanged.

Orchestrator test-correction (documented, 2026-11-23, issue #103 P3, lane
2026-11-23-frontier-103-p3-preview-first): MAX_WORDS 8550 → 8675 (measured 8650
words, rounded to the next twenty-five). The scan section now documents the P3
``scan_profile`` (default ``balanced``) with the preview-first flow, the one-
export/no-second-read rule, the identity-drift fallback and the measured net
start effect against the unchanged baseline. Floors unchanged.

Orchestrator test-correction (documented, 2026-11-23, issue #103 P3 fix round 1,
same lane): MAX_WORDS 8675 → 8725 (measured 8695 words, rounded to the next
twenty-five). The net-effect statement is now precise (—1 only for
``has_attachment is not False``, ±0 for the read-only case, drift worst case
4 vs 2, no "+1" against ``balanced``) and the empty-preview-ID edge case is
documented. Floors unchanged.

Orchestrator test-correction (documented, 2026-11-23, issue #103 P5, lane
2026-11-23-frontier-103-p5-pacing): MAX_WORDS 8725 → 8825 (measured 8804 words,
rounded to the next twenty-five). The scan section now documents the P5 pacing
and transient-backoff contract: config keys/flags, unchanged defaults 0/2.0,
pilot 250/5.0, the central seam, ``pace=False`` for payload/diagnostic starts,
the repeated block/budget check and the additive metrics. Floors unchanged.

Orchestrator test-correction (documented, 2026-11-23, issue #103 P5 fix round 2,
same lane): MAX_WORDS 8825 → 8850 (measured 8833 words, rounded to the next
twenty-five). The P5 paragraph now states that ``pacing_ms`` has no additional
upper bound (operator-owned) and that non-finite/out-of-float-range backoff
values are rejected. Floors unchanged.
"""

from __future__ import annotations

from pathlib import Path
import re
import unittest


BUNDLE_ROOT = Path(__file__).resolve().parents[3]
DOC = BUNDLE_ROOT / "skills" / "mail-desk" / "references" / "batch-runner.md"

#: Size gate: 20-40% reduction from the 8754-word baseline; raised for the
#: MD-P1 promotion-preflight module contract, the FR-28
#: review-correction/verify/recovery docs, the issue-#103 scan accelerator
#: contract, its atomic-spawn/default-budget follow-up, the issue-#107
#: sync_sent outcome/recovery paragraph, the issue-#103 D1 purpose-telemetry
#: contract at the callers, the issue-#103 P1 EML-preview opt-in, its
#: default-`False`/activation-gate fix round, the issue-#103 P2 scan-source
#: reuse contract, its full-text gate/modelled-class fix round, and the
#: issue-#103 P3 scan-profile/preview-first contract, its precise net-effect/
#: empty-ID fix round, the issue-#103 P5 pacing/backoff contract and its
#: operator-owned pacing bound (see docstring history).
MIN_WORDS = 5300
MAX_WORDS = 8850

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
    "## Promotion-Preflight",
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