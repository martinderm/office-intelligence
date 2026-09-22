"""FR-17 / MD-R1 behavior tests for shared ``do_not_route_if`` semantics.

Evidence mode ``tdd``, risk tier ``high``.  The ``Target...`` classes define the
MD-R1 target semantics and are Red before the production change in
``core.matching.project_matching`` (shared DNR predicate owner) and
``core.matching.topic_matching`` (topic DNR) plus the classifier surfacing; the
``...Characterization`` classes pin behavior that must stay green before and
after.

Target semantics under test:

1. ``do_not_route_if`` applies to projects and topics with one shared predicate;
   a suppressed entry never routes, and topic DNR applies before stage 2a and
   before the unique-subtopic fallback loop.
2. The ``newsletter`` predicate matches only subject/header signals (subject,
   from, to, cc, reply-to, list headers); a body-only occurrence of the word
   does not suppress.
3. When every candidate is DNR-suppressed the decision is not a silent
   ``unknown``: it carries a bounded review reason and
   ``decision.suppressed_candidates[]`` rows built from catalog data only
   (``kind``, ``id``, ``suppression_reason``).
4. Newsletter-suppressed mail maps deterministically to the existing
   ``Newsletter`` path (``copy_as_move``, no review); any other suppression stays
   in ``INBOX`` with ``keep_in_folder`` and review.

B-2 reproduction: topic ``netzwerke`` declares
``do_not_route_if: ['newsletter', 'no-reply']`` and the subject carries
``Newsletter``; today the mail routes to ``Themen/Netzwerke`` with a preselected
subtopic.  The target suppresses it.
"""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import routing_fixtures  # noqa: E402
from core import classifier  # noqa: E402


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


def _select_topic(
    topics: list[dict],
    subject: str,
    *,
    from_str: str = "sender@other.test",
    to_str: str = "desk@other.test",
    cc_str: str = "",
    preview: str = "",
) -> dict | None:
    """Call the canonical topic root-match owner with the classifier's derived inputs."""
    full_text = f"{subject}\n{from_str}\n{to_str}\n{cc_str}\n{preview}"
    return classifier.select_topic_match(
        topics,
        subject=subject,
        full_text=full_text,
        full_text_lower=full_text.lower(),
        from_str=from_str,
        to_str=to_str,
        parties=f"{from_str} {to_str} {cc_str}".lower(),
    )


def _classify(
    overrides: dict,
    *,
    projects: list[dict] | None = None,
    topics: list[dict] | None = None,
    final_index: dict | None = None,
) -> dict:
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
        topics=list(topics or []),
        sent_lookup={},
        final_index=final_index or {"items": {}},
    )


class TargetTopicDoNotRouteTests(unittest.TestCase):
    """Target (Red now): topic DNR applies before 2a and before the subtopic fallback."""

    def test_b2_newsletter_subject_suppresses_the_only_matching_topic(self) -> None:
        result = _select_topic(
            [routing_fixtures.topic_netzwerke()], routing_fixtures.NEWSLETTER_SUBJECT
        )

        self.assertIsNone(result)

    def test_b2_suppression_applies_before_the_unique_subtopic_fallback_loop(self) -> None:
        topic = routing_fixtures.topic_netzwerke(
            keywords=[], typical_subject_patterns=[]
        )

        result = _select_topic([topic], routing_fixtures.NEWSLETTER_SUBJECT)

        self.assertIsNone(result)


class TargetNewsletterDeterministicMappingTests(unittest.TestCase):
    """Target (Red now): newsletter suppression maps deterministically to Newsletter."""

    def test_b2_newsletter_suppressed_topic_maps_to_the_newsletter_path(self) -> None:
        item = _classify(
            {"subject": routing_fixtures.NEWSLETTER_SUBJECT},
            topics=[routing_fixtures.topic_netzwerke()],
        )

        self.assertEqual(routing_fixtures.NEWSLETTER_FOLDER, item["action"]["target_folder"])
        self.assertEqual("copy_as_move", item["action"]["type"])
        self.assertIs(False, item["decision"].get("review_required"))
        rows = item["decision"].get("suppressed_candidates") or []
        self.assertIn(
            {"kind": "topic", "id": "netzwerke", "suppression_reason": "newsletter"},
            rows,
        )
        self.assertIsInstance(item["notes"], str)
        self.assertNotIn("Netzwerke", item["notes"])

    def test_strong_non_suppressed_candidate_beats_the_newsletter_mapping(self) -> None:
        item = _classify(
            {"subject": routing_fixtures.NEWSLETTER_SUBJECT},
            topics=[
                routing_fixtures.topic_netzwerke(),
                routing_fixtures.topic_hochschule_international(),
            ],
        )

        self.assertEqual("topic", item["decision"]["kind"])
        self.assertEqual("hochschule-international", item["decision"]["id"])
        self.assertEqual(
            "Themen/Hochschule International", item["action"]["target_folder"]
        )
        rows = item["decision"].get("suppressed_candidates") or []
        self.assertIn(
            {"kind": "topic", "id": "netzwerke", "suppression_reason": "newsletter"},
            rows,
        )


class TargetNonNewsletterSuppressionTests(unittest.TestCase):
    """Target (Red now): a no-reply suppression stays in INBOX review with the candidate."""

    def test_b1_9387_analog_reports_review_reason_and_suppressed_candidate(self) -> None:
        project = routing_fixtures.project_atael(
            do_not_route_if=["newsletter", "no-reply"]
        )

        item = _classify(
            {
                "subject": routing_fixtures.ATAEL_INFO_SUBJECT,
                "from": routing_fixtures.NO_REPLY_SENDER,
            },
            projects=[project],
        )

        self.assertIs(True, item["decision"].get("review_required"))
        review_reason = item["decision"].get("review_reason")
        self.assertTrue(isinstance(review_reason, str) and review_reason.strip())
        rows = item["decision"].get("suppressed_candidates") or []
        self.assertIn(
            {"kind": "project", "id": "atael", "suppression_reason": "no-reply"},
            rows,
        )


class TargetNewsletterBodyScopedPredicateTests(unittest.TestCase):
    """Target (Red now): the newsletter predicate ignores body-only occurrences."""

    def test_body_only_newsletter_word_does_not_suppress_project_routing(self) -> None:
        project = routing_fixtures.project_atael(do_not_route_if=["newsletter"])

        result = _select_project(
            [project],
            routing_fixtures.ATAEL_SUBJECT,
            preview="Read our Newsletter for the September update.",
        )

        self.assertIsNotNone(result)
        self.assertEqual("atael", result["id"])


class TopicDoNotRouteCharacterizationTests(unittest.TestCase):
    """Characterization (green now and after): topic routing without the DNR signal."""

    def test_oead_subject_without_newsletter_still_routes_to_netzwerke(self) -> None:
        result = _select_topic(
            [routing_fixtures.topic_netzwerke()],
            "OeAD / Hochschule International 7/2026",
        )

        self.assertIsNotNone(result)
        self.assertEqual("netzwerke", result["id"])
        self.assertEqual("high", result["confidence"])
        self.assertEqual("Themen/Netzwerke", result["folder"])
        self.assertEqual(
            "eu-projekte-und-oead", result["preselected_subtopic"]["subtopic"]
        )


class NewsletterPredicateCharacterizationTests(unittest.TestCase):
    """Characterization (green now and after): header-scoped newsletter semantics."""

    def test_newsletter_subject_token_suppresses_a_project(self) -> None:
        project = routing_fixtures.project_atael(do_not_route_if=["newsletter"])

        result = _select_project([project], "ATAEL Newsletter update")

        self.assertIsNone(result)

    def test_body_only_newsletter_word_does_not_suppress_topic_routing(self) -> None:
        result = _select_topic(
            [routing_fixtures.topic_newsletter_only()],
            "OeAD International 7/2026",
            preview="Read our Newsletter for the September update.",
        )

        self.assertIsNotNone(result)
        self.assertEqual("oead-international", result["id"])


class ExistingHeaderPredicateCharacterizationTests(unittest.TestCase):
    """Characterization (green now and after): the existing header predicates hold."""

    def test_mailing_list_header_recipient_suppresses_a_project(self) -> None:
        project = routing_fixtures.project_atael(do_not_route_if=["mailing list"])

        result = _select_project(
            [project],
            "ATAEL meeting information",
            to_str="Verteilerliste Wissenschaftliche Mitarbeiter:ls@tum.de",
        )

        self.assertIsNone(result)

    def test_no_reply_sender_suppresses_a_project(self) -> None:
        project = routing_fixtures.project_atael(do_not_route_if=["no-reply"])

        result = _select_project(
            [project],
            routing_fixtures.ATAEL_INFO_SUBJECT,
            from_str=routing_fixtures.NO_REPLY_SENDER,
        )

        self.assertIsNone(result)

    def test_automatic_reply_subject_suppresses_a_project(self) -> None:
        project = routing_fixtures.project_atael(do_not_route_if=["automatic reply"])

        result = _select_project([project], "Abwesenheitsnotiz: ATAEL Vertretung")

        self.assertIsNone(result)


class NonNewsletterOutcomeCharacterizationTests(unittest.TestCase):
    """Characterization (green now and after): a no-reply suppression keeps the mail in INBOX."""

    def test_no_reply_suppression_targets_inbox_with_keep_in_folder(self) -> None:
        project = routing_fixtures.project_atael(
            do_not_route_if=["newsletter", "no-reply"]
        )

        item = _classify(
            {
                "subject": routing_fixtures.ATAEL_INFO_SUBJECT,
                "from": routing_fixtures.NO_REPLY_SENDER,
            },
            projects=[project],
        )

        self.assertEqual("INBOX", item["action"]["target_folder"])
        self.assertEqual("keep_in_folder", item["action"]["type"])


class ThreadInheritanceCharacterizationTests(unittest.TestCase):
    """Characterization (green now and after): thread inheritance never consults DNR."""

    def test_parent_folder_inheritance_routes_while_dnr_stays_out_of_inheritance(self) -> None:
        project = routing_fixtures.project_atael(
            do_not_route_if=["newsletter", "no-reply"]
        )
        parent_mid = "parent.9387@example.test"
        final_index = {
            "items": {
                parent_mid: {"final_folder": "Projekte/In Ausarbeitung/ATAEL"},
            }
        }

        item = _classify(
            {
                "subject": "Newsletter: For Information - ATAEL",
                "from": routing_fixtures.NO_REPLY_SENDER,
                "in_reply_to": f"<{parent_mid}>",
            },
            projects=[project],
            final_index=final_index,
        )

        self.assertEqual("project", item["decision"]["kind"])
        self.assertEqual("atael", item["decision"]["id"])
        self.assertEqual("Projekte/In Ausarbeitung/ATAEL", item["action"]["target_folder"])


if __name__ == "__main__":
    unittest.main()
