"""FR-13 / MD-M1-T01 structural Red tests for the ``core.matching`` foundation.

These tests define the missing matching boundary for MDM1-T01 before any production
exists (evidence mode ``tdd``, risk tier ``high``):

1. ``core.matching.date_parser`` owns ``parse_date_to_year_month`` and
   ``core.matching.ambiguity`` owns the reusable ambiguity policy module; both import
   as part of the existing Mail Desk package layout.
2. The ``core.classifier`` compatibility facade re-exports the moved callable by
   object identity.
3. Baseline date parsing and representative ambiguity outcomes are unchanged.
4. The T01 ambiguity owner exposes a minimal, concrete callable API for reusable
   ranking/unique-choice, cross-kind conflict and fallback ambiguity, and the facade
   routes a representative classification through that canonical owner.
5. ``classifier_rules_fingerprint`` binds the deterministic, ordered T01 active
   source set, moves for a semantic AST change in any bound module, ignores
   comments/formatting/host paths and fails closed for missing or invalid sources.

T01 owns only ``classifier.py``, ``ambiguity.py`` and ``date_parser.py``; the project
and topic matching modules arrive with their own tickets and are deliberately not
required here.  The fingerprint exposes its ordered source set as
``attachment_reclassification._CLASSIFIER_MODULE_PATHS`` (the approved MD-M1
source-set seam).  No private matcher helper, digest literal or ranking detail beyond
the owner's observable contract is asserted.
"""

from __future__ import annotations

from contextlib import contextmanager
import importlib
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

from core import classifier  # noqa: E402
from core import attachment_reclassification as reclass  # noqa: E402

# The active T01 ordered source set from the spec ("Classifier revision"), relative to
# the mail-desk root.  T02 (project_matching) and T03 (topic_matching) extend it later.
_EXPECTED_RULE_SOURCES = (
    "scripts/core/classifier.py",
    "scripts/core/matching/ambiguity.py",
    "scripts/core/matching/date_parser.py",
)

# The canonical T01 ambiguity owner callable seams.
_AMBIGUITY_CALLABLES = (
    "resolve_scored_candidates",
    "select_unique_fallback",
    "cross_kind_conflict",
    "mark_cross_kind_conflict",
)

_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")


def _import_matching(dotted: str):
    """Import a matching module lazily so the Red failure is the missing production
    boundary rather than a file-level collection error."""
    return importlib.import_module(dotted)


def _ambiguity():
    return _import_matching("core.matching.ambiguity")


@contextmanager
def _spy_owner_callable(name: str):
    """Wrap the canonical ambiguity owner callable in a spy that preserves behavior.

    The same spy is installed on the owner module and, when the facade holds a bound
    reference, on ``core.classifier`` as well, so routing is observed regardless of
    whether ``classifier.py`` calls through the package or a direct import.
    """
    owner = _ambiguity()
    spy = Mock(wraps=getattr(owner, name))
    patchers = [patch.object(owner, name, spy)]
    if hasattr(classifier, name):
        patchers.append(patch.object(classifier, name, spy))
    for patcher in patchers:
        patcher.start()
    try:
        yield spy
    finally:
        for patcher in reversed(patchers):
            patcher.stop()


def _subtopic(identifier: str, pattern: str) -> dict[str, object]:
    return {
        "id": identifier,
        "title": f"Sub {identifier}",
        "typical_subject_patterns": [pattern],
        "keywords": [],
        "contacts": [],
    }


def _topic(subtopics: list[dict[str, object]]) -> dict[str, object]:
    return {
        "id": "t1",
        "title": "Topic One",
        "mailbox_folder": "Themen/T1",
        "typical_subject_patterns": [],
        "keywords": [],
        "domains": [],
        "contacts": [],
        "subtopics": subtopics,
    }


def _ambiguous_email() -> dict[str, object]:
    return {
        "envelope_id": "1",
        "folder": "INBOX",
        "subject": "Orion update",
        "from": "sender@example.test",
        "to": "desk@example.test",
        "preview": "orion",
        "date": "2026-01-15",
    }


class MatchingOwnerModuleTests(unittest.TestCase):
    """The matching package owns its assigned callables and the facade re-exports them."""

    def test_date_parser_module_is_importable_and_owns_parse_date(self) -> None:
        module = _import_matching("core.matching.date_parser")
        self.assertTrue(callable(module.parse_date_to_year_month))

    def test_ambiguity_module_owns_the_policy_callable_seams(self) -> None:
        module = _ambiguity()
        for name in _AMBIGUITY_CALLABLES:
            with self.subTest(callable=name):
                self.assertIn(name, vars(module))
                value = getattr(module, name)
                self.assertTrue(callable(value))
                self.assertEqual(module.__name__, getattr(value, "__module__", None))

    def test_facade_reexports_owner_callable_by_identity(self) -> None:
        module = _import_matching("core.matching.date_parser")
        self.assertIs(
            classifier.parse_date_to_year_month,
            module.parse_date_to_year_month,
        )


class AmbiguityPolicyContractTests(unittest.TestCase):
    """Concrete behavior of the canonical T01 ambiguity owner callables."""

    def test_scored_candidates_rank_deterministically_and_resolve_ties(self) -> None:
        low = {"id": "zeta", "title": "Zeta", "reasons": []}
        alpha = {"id": "alpha", "title": "Alpha", "reasons": []}
        beta = {"id": "beta", "title": "Beta", "reasons": []}
        scored = [(100, low), (300, beta), (300, alpha)]

        resolved = _ambiguity().resolve_scored_candidates(scored)
        self.assertEqual(
            ["alpha", "beta"],
            [candidate["id"] for candidate in resolved["candidates"]],
        )
        # Input order must never influence the resolution.
        self.assertEqual(
            resolved, _ambiguity().resolve_scored_candidates(list(reversed(scored)))
        )

    def test_unique_choice_returns_the_single_top_candidate(self) -> None:
        top = {"id": "alpha", "title": "Alpha", "reasons": ["subject_pattern:orion"]}
        low = {"id": "beta", "title": "Beta", "reasons": []}
        resolved = _ambiguity().resolve_scored_candidates([(300, top), (100, low)])
        self.assertEqual({"unique": top}, resolved)

    def test_no_candidates_resolves_to_empty(self) -> None:
        self.assertEqual({}, _ambiguity().resolve_scored_candidates([]))

    def test_fallback_selects_only_a_unique_unambiguous_choice(self) -> None:
        top = {"id": "alpha", "title": "Alpha"}
        other = {"id": "beta", "title": "Beta"}
        module = _ambiguity()
        self.assertEqual(top, module.select_unique_fallback([(300, top), (100, other)]))
        self.assertIsNone(
            module.select_unique_fallback([(300, top), (100, other)], ambiguous=True)
        )
        self.assertIsNone(module.select_unique_fallback([(300, top), (300, other)]))
        self.assertIsNone(module.select_unique_fallback([]))

    def test_cross_kind_conflict_detects_both_activity_kinds(self) -> None:
        module = _ambiguity()
        self.assertTrue(module.cross_kind_conflict({"operation": "op", "event": "ev"}))
        self.assertTrue(
            module.cross_kind_conflict(
                {"operation_candidates": [{"id": "o"}], "event_candidates": [{"id": "e"}]}
            )
        )
        self.assertTrue(
            module.cross_kind_conflict({"operation": "op", "event_candidates": [{"id": "e"}]})
        )
        self.assertFalse(module.cross_kind_conflict({"operation": "op"}))
        self.assertFalse(module.cross_kind_conflict({"event": "ev"}))
        self.assertFalse(module.cross_kind_conflict({}))

    def test_cross_kind_conflict_marking_is_idempotent(self) -> None:
        module = _ambiguity()
        candidate = {"id": "o", "title": "Op", "reasons": ["subject_pattern:x"]}
        marked = module.mark_cross_kind_conflict([candidate])
        self.assertEqual(
            ["subject_pattern:x", "cross_kind_conflict"], marked[0]["reasons"]
        )
        again = module.mark_cross_kind_conflict(marked)
        self.assertEqual(
            ["subject_pattern:x", "cross_kind_conflict"], again[0]["reasons"]
        )


class BaselineBehaviorParityTests(unittest.TestCase):
    """Date parsing and representative ambiguity outcomes stay unchanged."""

    def test_date_parsing_outcomes_unchanged(self) -> None:
        cases = {
            "": ("2026-01", "2026-01-01"),
            "2026-01-15": ("2026-01", "2026-01-15"),
            "Thu, 15 Jan 2026 13:11:40 +0000": ("2026-01", "2026-01-15"),
            "garbage": ("2026-01", "2026-01-01"),
        }
        for raw, expected in cases.items():
            with self.subTest(date=raw):
                self.assertEqual(expected, classifier.parse_date_to_year_month(raw))

    def test_unique_topic_selection_is_unchanged(self) -> None:
        topic = _topic([_subtopic("s1", "orion")])
        resolution = classifier._select_topic_subtopic(
            topic, subject="Orion update", full_text="orion", from_str="sender@example.test"
        )
        self.assertEqual("s1", resolution["subtopic"])
        self.assertEqual(
            "topic",
            classifier.classify_email(_ambiguous_email(), projects=[], topics=[topic])[
                "decision"
            ]["kind"],
        )

    def test_tied_topic_candidates_stay_ambiguous(self) -> None:
        topic = _topic([_subtopic("s1", "orion"), _subtopic("s2", "orion")])
        resolution = classifier._select_topic_subtopic(
            topic, subject="Orion update", full_text="orion", from_str="sender@example.test"
        )
        self.assertEqual(["s1", "s2"], [candidate["id"] for candidate in resolution["candidates"]])
        self.assertEqual(
            "unknown",
            classifier.classify_email(_ambiguous_email(), projects=[], topics=[topic])[
                "decision"
            ]["kind"],
        )


class AmbiguityRoutingTests(unittest.TestCase):
    """A representative classification routes through the canonical ambiguity owner."""

    def test_topic_resolution_routes_through_owner_unique_choice(self) -> None:
        topic = _topic([_subtopic("s1", "orion")])
        with _spy_owner_callable("resolve_scored_candidates") as spy:
            resolution = classifier._select_topic_subtopic(
                topic, subject="Orion update", full_text="orion", from_str="sender@example.test"
            )
        self.assertTrue(spy.called, "topic resolution must use the canonical owner")
        self.assertEqual("s1", resolution["subtopic"])

    def test_classification_routes_through_owner_policy(self) -> None:
        topic = _topic([_subtopic("s1", "orion")])
        with _spy_owner_callable("resolve_scored_candidates") as unique_spy, \
                _spy_owner_callable("select_unique_fallback") as fallback_spy:
            decision = classifier.classify_email(
                _ambiguous_email(), projects=[], topics=[topic]
            )["decision"]
        self.assertTrue(unique_spy.called, "classification must use the canonical owner")
        self.assertTrue(fallback_spy.called, "fallback selection must use the canonical owner")
        self.assertEqual("topic", decision["kind"])
        self.assertEqual("s1", decision["subtopic"])


class ClassifierRuleSourceSetTests(unittest.TestCase):
    """The revision fingerprint binds the ordered active source set and fails closed."""

    def _sources(self) -> tuple[Path, ...]:
        return tuple(reclass._CLASSIFIER_MODULE_PATHS)

    def test_ordered_source_set_matches_spec(self) -> None:
        sources = self._sources()
        self.assertEqual(len(_EXPECTED_RULE_SOURCES), len(sources))
        relative = tuple(
            Path(path).resolve().relative_to(MAIL_DESK_ROOT).as_posix() for path in sources
        )
        self.assertEqual(_EXPECTED_RULE_SOURCES, relative)
        for path in sources:
            self.assertTrue(Path(path).is_file(), str(path))

    def test_fingerprint_is_deterministic_64_hex(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            first = reclass.classifier_rules_fingerprint(workspace)
            second = reclass.classifier_rules_fingerprint(workspace)
        self.assertRegex(first, _FINGERPRINT_RE)
        self.assertEqual(first, second)

    def test_each_bound_module_moves_fingerprint_only_for_semantic_changes(self) -> None:
        sources = self._sources()
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            baseline = reclass.classifier_rules_fingerprint(workspace)
            for index, source in enumerate(sources):
                text = Path(source).read_text(encoding="utf-8")
                variants = {
                    "copy": text,
                    "comment": text + "\n# trailing comment only\n",
                    "format": text + "\n\n\n",
                    "semantic": text + "\n_MDM1_SENTINEL = 1\n",
                }
                results: dict[str, str] = {}
                for label, variant in variants.items():
                    candidate = workspace / f"{index}-{label}.py"
                    candidate.write_text(variant, encoding="utf-8")
                    patched = tuple(
                        candidate if position == index else path
                        for position, path in enumerate(sources)
                    )
                    with patch.object(reclass, "_CLASSIFIER_MODULE_PATHS", patched):
                        results[label] = reclass.classifier_rules_fingerprint(workspace)
                with self.subTest(module=Path(source).name):
                    # Identical content at a different host path must not move the digest.
                    self.assertEqual(baseline, results["copy"])
                    # Comments and formatting must not move it; a semantic AST change must.
                    self.assertEqual(baseline, results["comment"])
                    self.assertEqual(baseline, results["format"])
                    self.assertNotEqual(baseline, results["semantic"])

    def test_missing_bound_source_fails_closed(self) -> None:
        sources = self._sources()
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            patched = (workspace / "missing.py",) + tuple(sources[1:])
            with patch.object(reclass, "_CLASSIFIER_MODULE_PATHS", patched):
                with self.assertRaises(reclass.AttachmentReclassificationContractError):
                    reclass.classifier_rules_fingerprint(workspace)

    def test_invalid_bound_source_fails_closed(self) -> None:
        sources = self._sources()
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            broken = workspace / "broken.py"
            broken.write_text("def broken(:\n", encoding="utf-8")
            patched = (broken,) + tuple(sources[1:])
            with patch.object(reclass, "_CLASSIFIER_MODULE_PATHS", patched):
                with self.assertRaises(reclass.AttachmentReclassificationContractError):
                    reclass.classifier_rules_fingerprint(workspace)


if __name__ == "__main__":
    unittest.main()
