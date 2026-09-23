"""FR-21/MD-S4 docs-contract tests: desk-signals catalog + subject-pattern semantics.

Hermetic documentation contract. This module reads the real documentation files
shipped in the bundle and asserts that MD-S4's documentation obligations are
met; it imports no production code and touches no mailbox, network or catalog.

At HEAD the MD-S4 sections do not exist yet, so this module is a genuine Red:

* ``skills/mail-desk/SKILL.md`` never mentions the workspace desk-signals
  catalog ``memory/references/mail-desk/mail-desk.json``.
* ``skills/mail-desk/references/batch-runner.md`` never references the catalog
  maintenance contract.
* ``skills/topic-catalog-entry/SKILL.md`` documents the
  ``typical_subject_patterns`` field but not its literal, non-regex matching
  semantics nor the root-topic distinction.

MD-S5 (the catalog validator) is explicitly out of scope here.
"""

from __future__ import annotations

from pathlib import Path
import re
import unittest


MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
SKILL_DOC = MAIL_DESK_ROOT / "SKILL.md"
BATCH_RUNNER_DOC = MAIL_DESK_ROOT / "references" / "batch-runner.md"
TOPIC_CATALOG_SKILL_DOC = MAIL_DESK_ROOT.parent / "topic-catalog-entry" / "SKILL.md"

#: Canonical workspace path of the desk-signals catalog (schema 1).
CATALOG_RELATIVE_PATH = "memory/references/mail-desk/mail-desk.json"


class _DocsContractTestCase(unittest.TestCase):
    """Shared read-only helpers; declares no test methods of its own."""

    def read_doc(self, path: Path) -> str:
        self.assertTrue(path.is_file(), f"documentation file is missing: {path}")
        return path.read_text(encoding="utf-8")

    def assert_any_phrase(self, text: str, phrases: tuple[str, ...], message: str) -> None:
        haystack = text.casefold()
        if any(phrase.casefold() in haystack for phrase in phrases):
            return
        self.fail(f"{message}; none of {list(phrases)!r} present")

    def assert_any_pattern(self, text: str, patterns: tuple[str, ...], message: str) -> None:
        if any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns):
            return
        self.fail(f"{message}; none of {list(patterns)!r} matched")


class DeskSignalsCatalogSkillDocTests(_DocsContractTestCase):
    """``skills/mail-desk/SKILL.md`` documents the workspace desk-signals catalog."""

    def setUp(self) -> None:
        self.text = self.read_doc(SKILL_DOC)

    def test_skill_doc_names_the_workspace_catalog_and_its_path(self) -> None:
        self.assertIn(
            "mail-desk.json",
            self.text,
            "SKILL.md must name the desk-signals catalog file 'mail-desk.json'",
        )
        self.assertIn(
            CATALOG_RELATIVE_PATH,
            self.text,
            f"SKILL.md must document the canonical catalog path '{CATALOG_RELATIVE_PATH}'",
        )

    def test_skill_doc_scopes_catalog_maintenance_to_the_workspace(self) -> None:
        self.assertIn(
            CATALOG_RELATIVE_PATH,
            self.text,
            "the maintenance contract must point at the workspace catalog path",
        )
        self.assert_any_phrase(
            self.text,
            ("Workspace", "workspace"),
            "maintenance must be scoped to the consuming workspace",
        )
        self.assert_any_phrase(
            self.text,
            ("ausschließlich", "nur "),
            "SKILL.md must state reply triggers are maintained only in the workspace catalog",
        )
        self.assert_any_phrase(
            self.text,
            ("Bundle", "bundle"),
            "SKILL.md must name the bundle as the non-editable side",
        )

    def test_skill_doc_marks_bundle_default_as_compatibility_fallback(self) -> None:
        self.assertIn(
            "mail-desk.json",
            self.text,
            "the fallback statement must be tied to the desk-signals catalog",
        )
        self.assert_any_phrase(
            self.text,
            ("Fallback", "fallback"),
            "the bundle default must be documented as a fallback",
        )
        self.assert_any_phrase(
            self.text,
            ("Kompatibilität", "compatibility"),
            "the bundle default must be documented as compatibility-only",
        )
        self.assert_any_phrase(
            self.text,
            ("Bundle", "bundle"),
            "the fallback must be attributed to the bundle, not the workspace catalog",
        )

    def test_skill_doc_documents_catalog_schema_fields(self) -> None:
        self.assertIn(
            "reply_triggers",
            self.text,
            "SKILL.md must document the required reply_heuristics.reply_triggers field",
        )
        self.assertIn(
            "no_reply_sender_tokens",
            self.text,
            "SKILL.md must document the optional no_reply_sender_tokens field",
        )
        self.assertIn(
            "schema_version",
            self.text,
            "SKILL.md must document the catalog schema_version (schema 1)",
        )

    def test_skill_doc_explains_owner_address_effect(self) -> None:
        self.assertIn(
            "owner_address",
            self.text,
            "SKILL.md must document the owner_address catalog field",
        )
        self.assert_any_phrase(
            self.text,
            ("Sent", "sent"),
            "owner_address semantics must reference the sent-reply check",
        )
        self.assert_any_phrase(
            self.text,
            ("to/cc", "to`/`cc", "Empfänger", "recipient"),
            "owner_address semantics must reference the to/cc identity check",
        )
        self.assert_any_phrase(
            self.text,
            ("null", "None"),
            "SKILL.md must document that owner_address null keeps the keyword heuristic",
        )

    def test_skill_doc_documents_fail_loud_schema_drift(self) -> None:
        self.assertIn(
            "ValueError",
            self.text,
            "SKILL.md must name the fail-loud error type (ValueError) on catalog drift",
        )
        self.assert_any_phrase(
            self.text,
            ("Drift", "drift"),
            "SKILL.md must document schema drift handling",
        )
        self.assert_any_phrase(
            self.text,
            ("fail-loud", "fail loud", "fail-closed"),
            "SKILL.md must document fail-loud behavior instead of a silent fallback",
        )


class DeskSignalsCatalogBatchRunnerDocTests(_DocsContractTestCase):
    """``skills/mail-desk/references/batch-runner.md`` references the catalog contract."""

    def setUp(self) -> None:
        self.text = self.read_doc(BATCH_RUNNER_DOC)

    def test_batch_runner_doc_references_the_workspace_catalog(self) -> None:
        self.assertIn(
            "mail-desk.json",
            self.text,
            "batch-runner.md must mention the desk-signals catalog 'mail-desk.json'",
        )
        self.assertIn(
            CATALOG_RELATIVE_PATH,
            self.text,
            f"batch-runner.md must reference the canonical path '{CATALOG_RELATIVE_PATH}'",
        )

    def test_batch_runner_doc_references_the_maintenance_contract(self) -> None:
        self.assertIn(
            "mail-desk.json",
            self.text,
            "the maintenance contract must name the desk-signals catalog",
        )
        self.assert_any_phrase(
            self.text,
            ("Desk-Signals", "desk-signals", "reply_heuristics", "Workspace-Katalog"),
            "batch-runner.md must name the desk-signals catalog concept it references",
        )
        self.assert_any_phrase(
            self.text,
            ("Fallback", "fallback", "Kompatibilität", "compatibility"),
            "batch-runner.md must mark the bundle default as a compatibility fallback",
        )


class SubjectPatternSemanticsDocTests(_DocsContractTestCase):
    """``skills/topic-catalog-entry/SKILL.md`` documents the pattern match semantics."""

    def setUp(self) -> None:
        self.text = self.read_doc(TOPIC_CATALOG_SKILL_DOC)

    def test_documents_literal_word_boundary_matching(self) -> None:
        self.assertIn(
            "typical_subject_patterns",
            self.text,
            "the field must stay documented as typical_subject_patterns",
        )
        self.assert_any_phrase(
            self.text,
            ("Literal", "literal"),
            "subject patterns must be documented as literal signals",
        )
        self.assert_any_pattern(
            self.text,
            (r"\(\?<!\\w\)", r"lookaround", r"look-around", r"Wortgrenze"),
            "subject patterns must be documented with word-boundary/lookaround matching",
        )

    def test_documents_minimum_length_and_separator_normalization(self) -> None:
        self.assert_any_pattern(
            self.text,
            (r"mindestens\s*3", r"3\s*Zeichen", r">=\s*3", r"≥\s*3"),
            "subject patterns must document the minimum length of 3 characters",
        )
        self.assertIn(
            "[-_]",
            self.text,
            "subject patterns must document the [-_] separator normalization",
        )
        self.assert_any_phrase(
            self.text,
            ("Normalisierung", "normalisier", "normalization"),
            "subject patterns must document separator normalization",
        )

    def test_documents_case_insensitive_and_not_regex(self) -> None:
        self.assert_any_phrase(
            self.text,
            ("case-insensitive", "case insensitive", "Groß-/Kleinschreibung", "IGNORECASE", "casefold"),
            "subject patterns must be documented as case-insensitive",
        )
        self.assert_any_phrase(
            self.text,
            (
                "kein Regex",
                "keine Regex",
                "keinen Regex",
                "not regex",
                "not a regex",
                "not a regular expression",
                "keine reguläre",
                "keine regulären",
                "nicht als Regex",
            ),
            "subject patterns must be documented as NOT regex",
        )

    def test_documents_regex_style_counterexample_never_matches(self) -> None:
        self.assertIn(
            ".*",
            self.text,
            "the counterexample must show a regex-style '.*' pattern",
        )
        self.assert_any_phrase(
            self.text,
            ("Literal", "literal", "wörtlich"),
            "the counterexample must explain '.*' is searched literally",
        )
        self.assert_any_phrase(
            self.text,
            ("matcht nie", "matcht niemals", "niemals matchen", "never match", "matcht nicht", "kein Treffer"),
            "the counterexample must state such a pattern never matches",
        )

    def test_documents_root_topic_pattern_distinction(self) -> None:
        self.assert_any_pattern(
            self.text,
            (r"\broot\b",),
            "the root-topic pattern semantics must be documented",
        )
        self.assert_any_phrase(
            self.text,
            ("substring", "teilstring", "teilzeichenkette"),
            "root patterns must be documented as plain substring matching",
        )
        self.assert_any_pattern(
            self.text,
            (r"\\b", r"Wortgrenze"),
            "root patterns must be documented as word-bounded where applicable",
        )
        self.assert_any_phrase(
            self.text,
            ("Unterschied", "anders", "im Gegensatz", "distinction", "different"),
            "the root-vs-subtopic pattern distinction must be stated explicitly",
        )


if __name__ == "__main__":
    unittest.main()
