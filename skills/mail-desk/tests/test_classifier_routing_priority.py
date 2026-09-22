"""FR-17 / MD-R1 behavior tests for project routing priority and signal quality.

Evidence mode ``tdd``, risk tier ``high``.  The ``Target...`` classes define the
MD-R1 target semantics and are Red before the production change in
``core.matching.project_matching.select_project_match``; the
``...Characterization`` classes pin behavior that must stay green before and
after.

Documented target ordering for equally plausible catalog candidates: an exact
ID/kuerzel/alias/``typical_subject_patterns`` subject match outranks a pure
contact/domain/body-context match of another project (signal quality before
catalog order, MD-R1 acceptance criterion); ``routing_priority`` then orders
equally strong candidates (numerically higher first, missing or invalid treated as
neutral-lowest); stable catalog order is the final tie-breaker.  Invalid priority
types are never coerced: Python's ``bool`` is an ``int`` subclass, so booleans and
strings are both treated as missing/neutral instead of being cast to a rank.

B-1 reproduction: ``usage-ng`` precedes ``atael`` in catalog order and wins today
through a pure TUM contact match, although the subject carries the exact ``ATAEL``
code.  The target selects ``atael``.

The pre-existing expectation in ``test_classifier_project_matching_module.py``
(an earlier catalog contact entry beats a later exact subject code) still
describes today's production behavior and is intentionally left unchanged; it
will be updated in the MD-R1 production dispatch.
``TargetCatalogOrderVsLaterExactCodeTests`` carries the target-semantics
counterpart here.
"""

from __future__ import annotations

import importlib
from pathlib import Path
import sys
import unittest

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import routing_fixtures  # noqa: E402
from core import classifier  # noqa: E402

_NEUTRAL_SUBJECT = "Neutral routing subject without a catalog code"


def _select_project(
    projects: list[dict],
    subject: str,
    *,
    from_str: str = "sender@other.test",
    to_str: str = "desk@other.test",
    cc_str: str = "",
    preview: str = "",
) -> dict | None:
    """Call the canonical project root-match owner with the classifier's derived inputs."""
    full_text = f"{subject}\n{from_str}\n{to_str}\n{cc_str}\n{preview}"
    return classifier.select_project_match(
        projects,
        subject=subject,
        full_text=full_text,
        full_text_lower=full_text.lower(),
        from_str=from_str,
        to_str=to_str,
        parties=f"{from_str} {to_str} {cc_str}".lower(),
    )


def _classify(overrides: dict, *, projects: list[dict] | None = None) -> dict:
    """Classify one hermetic email through the facade with the fixture catalogs."""
    email = {
        "envelope_id": "fr17-r1",
        "folder": "INBOX",
        "message_id": "<fr17-r1@example.test>",
        "subject": "",
        "from": "Coordinator <coord@other.test>",
        "to": "Martin <martin@other.test>",
        "date": "2026-09-22",
        "preview": "",
    }
    email.update(overrides)
    return classifier.classify_email(
        email,
        projects=list(projects or []),
        topics=[],
        sent_lookup={},
        final_index={"items": {}},
    )


def _shared_contact_party() -> str:
    return f"Sender <{routing_fixtures.SHARED_CONTACT_EMAIL}>"


class TargetExactSubjectCodeTests(unittest.TestCase):
    """Target (Red now): an exact subject code beats a pure contact match of another project."""

    def test_b1_atael_subject_code_beats_earlier_usage_ng_contact_match(self) -> None:
        result = _select_project(
            routing_fixtures.boku_analog_projects(),
            routing_fixtures.ATAEL_SUBJECT,
            from_str=f"Michaelis, Sybille <{routing_fixtures.TUM_CONTACT_EMAIL}>",
        )

        self.assertIsNotNone(result)
        self.assertEqual("atael", result["id"])
        self.assertEqual("high", result["confidence"])
        self.assertEqual("Projekte/In Ausarbeitung/ATAEL", result["folder"])

    def test_b1_classification_selects_the_exact_code_project(self) -> None:
        item = _classify(
            {
                "subject": routing_fixtures.ATAEL_SUBJECT,
                "from": f"Michaelis, Sybille <{routing_fixtures.TUM_CONTACT_EMAIL}>",
            },
            projects=routing_fixtures.boku_analog_projects(),
        )

        self.assertEqual("project", item["decision"]["kind"])
        self.assertEqual("atael", item["decision"]["id"])
        self.assertEqual("Projekte/In Ausarbeitung/ATAEL", item["action"]["target_folder"])

    def test_exact_subject_code_beats_higher_priority_contact_entry(self) -> None:
        exact = routing_fixtures.project_atael(routing_priority=10)
        contact = routing_fixtures.project_usage_ng(routing_priority=100)

        result = _select_project(
            [contact, exact],
            routing_fixtures.ATAEL_SUBJECT,
            from_str=f"Michaelis, Sybille <{routing_fixtures.TUM_CONTACT_EMAIL}>",
        )

        self.assertIsNotNone(result)
        self.assertEqual("atael", result["id"])
        self.assertEqual("high", result["confidence"])


class TargetCatalogOrderVsLaterExactCodeTests(unittest.TestCase):
    """Target (Red now): a later exact subject code beats an earlier catalog contact entry."""

    def test_later_orion_subject_code_beats_earlier_contact_entry(self) -> None:
        earlier = routing_fixtures.project_contact("alpha", routing_priority=50)
        later = routing_fixtures.project_orion()

        result = _select_project(
            [earlier, later], "ORION update", from_str=_shared_contact_party()
        )

        self.assertIsNotNone(result)
        self.assertEqual("orion", result["id"])
        self.assertEqual("high", result["confidence"])


class TargetRoutingPriorityOrderingTests(unittest.TestCase):
    """Target (Red now): routing_priority orders equally strong catalog candidates."""

    def test_higher_routing_priority_wins_on_equal_contact_strength(self) -> None:
        low = routing_fixtures.project_contact("low", routing_priority=50)
        high = routing_fixtures.project_contact("high", routing_priority=65)

        result = _select_project(
            [low, high], _NEUTRAL_SUBJECT, from_str=_shared_contact_party()
        )

        self.assertIsNotNone(result)
        self.assertEqual("high", result["id"])

    def test_missing_routing_priority_ranks_below_an_explicit_priority(self) -> None:
        silent = routing_fixtures.project_contact_without_priority("silent")
        explicit = routing_fixtures.project_contact("explicit", routing_priority=50)

        result = _select_project(
            [silent, explicit], _NEUTRAL_SUBJECT, from_str=_shared_contact_party()
        )

        self.assertIsNotNone(result)
        self.assertEqual("explicit", result["id"])

    def test_string_priority_is_treated_as_missing_and_does_not_crash(self) -> None:
        invalid = routing_fixtures.project_contact("invalid", routing_priority="80")
        explicit = routing_fixtures.project_contact("explicit", routing_priority=50)

        result = _select_project(
            [invalid, explicit], _NEUTRAL_SUBJECT, from_str=_shared_contact_party()
        )

        self.assertIsNotNone(result)
        self.assertEqual("explicit", result["id"])

    def test_boolean_priority_is_treated_as_missing_and_does_not_crash(self) -> None:
        invalid = routing_fixtures.project_contact("invalid", routing_priority=True)
        explicit = routing_fixtures.project_contact("explicit", routing_priority=50)

        result = _select_project(
            [invalid, explicit], _NEUTRAL_SUBJECT, from_str=_shared_contact_party()
        )

        self.assertIsNotNone(result)
        self.assertEqual("explicit", result["id"])


class CanonicalRouteCharacterizationTests(unittest.TestCase):
    """Characterization (green now and after): exact subject codes keep routing alone."""

    def test_atael_subject_code_without_any_contact_match_routes_to_atael(self) -> None:
        result = _select_project(
            routing_fixtures.boku_analog_projects(), routing_fixtures.ATAEL_SUBJECT
        )

        self.assertIsNotNone(result)
        self.assertEqual("atael", result["id"])
        self.assertEqual("high", result["confidence"])
        self.assertEqual("Projekte/In Ausarbeitung/ATAEL", result["folder"])


class RoutingPriorityCharacterizationTests(unittest.TestCase):
    """Characterization (green now and after): stable tie-breakers stay deterministic."""

    def test_equal_priorities_fall_back_to_catalog_order(self) -> None:
        first = routing_fixtures.project_contact("first", routing_priority=50)
        second = routing_fixtures.project_contact("second", routing_priority=50)

        result = _select_project(
            [first, second], _NEUTRAL_SUBJECT, from_str=_shared_contact_party()
        )

        self.assertIsNotNone(result)
        self.assertEqual("first", result["id"])

    def test_two_catalog_entries_without_priority_fall_back_to_catalog_order(self) -> None:
        first = routing_fixtures.project_contact_without_priority("first")
        second = routing_fixtures.project_contact_without_priority("second")

        result = _select_project(
            [first, second], _NEUTRAL_SUBJECT, from_str=_shared_contact_party()
        )

        self.assertIsNotNone(result)
        self.assertEqual("first", result["id"])

    def test_invalid_priorities_are_not_coerced_into_a_rank(self) -> None:
        neutral = routing_fixtures.project_contact_without_priority("neutral")
        boolean = routing_fixtures.project_contact("boolean", routing_priority=True)
        text = routing_fixtures.project_contact("text", routing_priority="80")

        for label, candidate in (("bool", boolean), ("str", text)):
            with self.subTest(invalid_priority=label):
                result = _select_project(
                    [neutral, candidate], _NEUTRAL_SUBJECT, from_str=_shared_contact_party()
                )
                self.assertIsNotNone(result)
                self.assertEqual("neutral", result["id"])

    def test_within_entry_exact_subject_code_beats_its_own_contact_match(self) -> None:
        project = routing_fixtures.project_contact("meshe", routing_priority=50)

        result = _select_project(
            [project], "MESHE weekly sync", from_str=_shared_contact_party()
        )

        self.assertIsNotNone(result)
        self.assertEqual("meshe", result["id"])
        self.assertEqual("high", result["confidence"])


class ShortAcronymGuardCharacterizationTests(unittest.TestCase):
    """Characterization (green now and after): short/common acronym guards stay intact."""

    def test_all_caps_code_requires_a_word_boundary(self) -> None:
        result = _select_project(
            [routing_fixtures.project_atael()], "MATAELX evaluation"
        )

        self.assertIsNone(result)

    def test_common_word_acronym_prose_stays_protected(self) -> None:
        project = routing_fixtures.project_atael(id="usage-word", kuerzel="USAGE")

        result = _select_project([project], "Please review the usage report")

        self.assertIsNone(result)


class AmbiguityPolicyCharacterizationTests(unittest.TestCase):
    """Characterization (green now and after): the ambiguity callables are untouched."""

    def test_scored_candidate_ranking_is_deterministic(self) -> None:
        module = importlib.import_module("core.matching.ambiguity")
        alpha = {"id": "alpha", "title": "Alpha", "reasons": []}
        beta = {"id": "beta", "title": "Beta", "reasons": []}
        zeta = {"id": "zeta", "title": "Zeta", "reasons": []}

        resolved = module.resolve_scored_candidates([(100, zeta), (300, beta), (300, alpha)])

        self.assertEqual(
            ["alpha", "beta"], [candidate["id"] for candidate in resolved["candidates"]]
        )
        self.assertEqual(
            resolved,
            module.resolve_scored_candidates([(300, alpha), (300, beta), (100, zeta)]),
        )

    def test_unique_fallback_and_cross_kind_conflict_semantics_are_unchanged(self) -> None:
        module = importlib.import_module("core.matching.ambiguity")
        top = {"id": "alpha", "title": "Alpha"}
        other = {"id": "beta", "title": "Beta"}

        self.assertEqual(top, module.select_unique_fallback([(300, top), (100, other)]))
        self.assertIsNone(module.select_unique_fallback([(300, top), (300, other)]))
        self.assertTrue(module.cross_kind_conflict({"operation": "op", "event": "ev"}))
        self.assertFalse(module.cross_kind_conflict({"operation": "op"}))

    def test_unique_subtopic_fallback_output_is_unchanged(self) -> None:
        module = importlib.import_module("core.matching.topic_matching")
        topic = {
            "id": "operations",
            "title": "Operations",
            "mailbox_folder": "Themen/Operations",
            "aliases": [],
            "keywords": [],
            "typical_subject_patterns": [],
            "domains": [],
            "contacts": [],
            "do_not_route_if": [],
            "subtopics": [
                {
                    "id": "course-design",
                    "title": "Course Design",
                    "aliases": ["Course Redesign"],
                    "keywords": [],
                    "typical_subject_patterns": [],
                    "contacts": [],
                    "status": "active",
                }
            ],
        }

        result = module.select_topic_match(
            [topic],
            subject="Course Redesign timetable",
            full_text="Course Redesign timetable",
            full_text_lower="course redesign timetable",
            from_str="sender@other.test",
            to_str="desk@other.test",
            parties="sender@other.test desk@other.test",
        )

        self.assertIsNotNone(result)
        self.assertEqual("operations", result["id"])
        self.assertEqual("course-design", result["preselected_subtopic"]["subtopic"])


if __name__ == "__main__":
    unittest.main()
