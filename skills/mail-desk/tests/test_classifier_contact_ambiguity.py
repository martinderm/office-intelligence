"""Issue #63: multi-project contact ambiguity under a generic subject.

The real incident (env 9635) was a ``QUESTIONNAIRE INFORMATION`` mail from
Prof. Kassilly.  Her address is a contact of both ``WEEK`` and the planned
``ATAEL`` resubmission; the subject carried neither project code.  The classifier
took the pure contact hit of the higher-priority, earlier catalog entry as
``confidence: high`` and moved the mail to ATAEL, although the body clearly said
``Africa-UniNet`` / ``TUC`` / ``Dr. Susan Ouko``.

These tests pin the new split:

* generic subject + contact claimed by two projects + neutral body
  -> ``unknown``/``unclassified`` in INBOX, ``confidence: low``,
  ``review_required: true`` and no guessed move;
* unique catalog-backed body signals (keyword, other contact, domain, project
  name) -> the correct project;
* a tie or only the shared person's own signature -> Review;
* a project code in the subject still wins, and a single-affiliation contact
  keeps its existing ``high`` behavior.

Fixtures are hermetic in-memory catalogs; no mailbox, network, workspace or
consumer ``projects.json`` is touched.
"""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

from core import classifier  # noqa: E402
from core.matching import project_matching  # noqa: E402

KASSILLY_EMAIL = "nkasili@yahoo.com"
WEEK_FOLDER = "Projekte/WEEK"
ATAEL_FOLDER = "Projekte/In Ausarbeitung/ATAEL"
GENERIC_SUBJECT = "QUESTIONNAIRE INFORMATION"


def project_week(**overrides: object) -> dict[str, object]:
    """Return the Africa-UniNet / TUC project analog."""
    value: dict[str, object] = {
        "id": "week",
        "kuerzel": "WEEK",
        "title": "WEEK - Africa-UniNet",
        "mailbox_folder": WEEK_FOLDER,
        "aliases": ["WEEK"],
        "keywords": ["Africa-UniNet", "Turkana"],
        "typical_subject_patterns": ["P147"],
        "domains": ["tuc.ac.ke"],
        "contacts": [
            {"name": "Nancy Kassilly", "email": KASSILLY_EMAIL},
            {"name": "Dr. Susan Ouko", "email": "susan.ouko@tuc.ac.ke"},
        ],
        "routing_priority": 50,
    }
    value.update(overrides)
    return value


def project_atael(**overrides: object) -> dict[str, object]:
    """Return the ATAEL resubmission analog (earlier entry, higher priority)."""
    value: dict[str, object] = {
        "id": "atael",
        "kuerzel": "ATAEL",
        "title": "ATAEL",
        "mailbox_folder": ATAEL_FOLDER,
        "aliases": ["ATAEL"],
        "keywords": ["GAP-101323118"],
        "typical_subject_patterns": [],
        "domains": ["ec.europa.eu"],
        "contacts": [{"name": "Nancy Kassilly", "email": KASSILLY_EMAIL}],
        "routing_priority": 65,
    }
    value.update(overrides)
    return value


def ambiguous_catalog() -> list[dict[str, object]]:
    """Return ATAEL before WEEK, so a pure contact tie would reproduce the incident."""
    return [project_atael(), project_week()]


def _email(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "envelope_id": "9635",
        "folder": "INBOX",
        "message_id": "<9635@example.test>",
        "subject": GENERIC_SUBJECT,
        "from": "Prof. Nancy Kassilly <nkasili@yahoo.com>",
        "to": "Martin <martin@boku.ac.at>",
        "date": "2026-11-02",
        "preview": "",
        "attachments": [],
    }
    value.update(overrides)
    return value


def _classify(
    projects: list[dict[str, object]],
    *,
    topics: list[dict[str, object]] | None = None,
    **overrides: object,
) -> dict:
    return classifier.classify_email(
        _email(**overrides),
        projects=projects,
        topics=list(topics or []),
        sent_lookup={},
        final_index={"items": {}},
    )


def _select_project(
    projects: list[dict[str, object]],
    subject: str,
    *,
    preview: str = "",
    from_str: str = "Prof. Nancy Kassilly <nkasili@yahoo.com>",
    to_str: str = "Martin <martin@boku.ac.at>",
    cc_str: str = "",
    suppressed: list[dict] | None = None,
) -> dict | None:
    full_text = f"{subject}\n{from_str}\n{to_str}\n{cc_str}\n{preview}"
    return project_matching.select_project_match(
        projects,
        subject=subject,
        full_text=full_text,
        full_text_lower=full_text.lower(),
        from_str=from_str,
        to_str=to_str,
        parties=f"{from_str} {to_str} {cc_str}".lower(),
        cc_str=cc_str,
        body_text=preview,
        suppressed_candidates=suppressed,
    )


class NeutralBodyReviewTests(unittest.TestCase):
    """Shared contact + generic subject + neutral body never guesses a project."""

    def test_shared_contact_generic_subject_and_empty_body_stays_in_inbox(self) -> None:
        item = _classify(ambiguous_catalog())

        self.assertEqual("unknown", item["decision"]["kind"])
        self.assertEqual("unclassified", item["decision"]["id"])
        self.assertEqual("low", item["decision"]["confidence"])
        self.assertIs(True, item["decision"]["review_required"])
        self.assertEqual(
            "ambiguous_contact_generic_subject",
            item["decision"]["review_reason"],
        )
        self.assertEqual(
            {"type": "keep_in_folder", "target_folder": "INBOX"}, item["action"]
        )
        self.assertIsNone(item["evidence"])
        self.assertIn("ATAEL", item["notes"])
        self.assertIn("WEEK", item["notes"])

    def test_shared_persons_own_signature_is_not_a_discriminator(self) -> None:
        item = _classify(
            ambiguous_catalog(),
            preview=(
                "Dear colleagues,\n"
                "best regards\n"
                "Prof. Nancy Kassilly\n"
                "nkasili@yahoo.com\n"
            ),
        )

        self.assertEqual("unknown", item["decision"]["kind"])
        self.assertIs(True, item["decision"]["review_required"])

    def test_institutional_shared_signature_does_not_leak_domain_or_keyword(self) -> None:
        # The shared person's institutional address embeds WEEK's domain and TUC
        # keyword. Masking the ambiguous address must keep the body neutral.
        institutional = "n.kassilly@tuc.ac.ke"
        catalog = [
            project_atael(
                contacts=[{"name": "Nancy Kassilly", "email": institutional}]
            ),
            project_week(
                keywords=["TUC"],
                domains=["tuc.ac.ke"],
                contacts=[
                    {"name": "Nancy Kassilly", "email": institutional},
                    {"name": "Dr. Susan Ouko", "email": "susan.ouko@tuc.ac.ke"},
                ],
            ),
        ]
        item = _classify(
            catalog,
            **{
                "from": f"Nancy Kassilly <{institutional}>",
                "preview": f"Best regards\nNancy Kassilly\n{institutional}\n",
            },
        )

        self.assertEqual("unknown", item["decision"]["kind"])
        self.assertEqual("INBOX", item["action"]["target_folder"])
        self.assertIs(True, item["decision"]["review_required"])

    def test_unique_contact_substring_inside_another_address_is_not_a_signal(self) -> None:
        third = project_atael(
            id="third",
            kuerzel="THIRD",
            mailbox_folder="Projekte/THIRD",
            contacts=[{"email": "ann@example.test"}],
            routing_priority=10,
        )

        item = _classify(
            [project_atael(), project_week(), third],
            cc="Joann <joann@example.test>",
        )

        self.assertEqual("unknown", item["decision"]["kind"])
        self.assertEqual("INBOX", item["action"]["target_folder"])
        self.assertIs(True, item["decision"]["review_required"])

    def test_body_with_only_shared_keyword_ties_into_review(self) -> None:
        catalog = [
            project_atael(keywords=["questionnaire"]),
            project_week(keywords=["questionnaire"]),
        ]

        item = _classify(catalog, preview="Please find the questionnaire data attached.")

        self.assertEqual("unknown", item["decision"]["kind"])
        self.assertIs(True, item["decision"]["review_required"])

    def test_body_naming_both_projects_ties_into_review(self) -> None:
        item = _classify(
            ambiguous_catalog(),
            preview="WEEK and ATAEL both receive the Africa-UniNet questionnaire.",
        )

        self.assertEqual("unknown", item["decision"]["kind"])
        self.assertIs(True, item["decision"]["review_required"])


class BodyArbitrationTests(unittest.TestCase):
    """Exactly one catalog-backed body signal resolves the contact tie."""

    def test_unique_project_keyword_routes_to_week(self) -> None:
        item = _classify(
            ambiguous_catalog(),
            preview=(
                "We are preparing the submission to Africa-UniNet and the Turkana "
                "University College final report."
            ),
        )

        self.assertEqual("project", item["decision"]["kind"])
        self.assertEqual("week", item["decision"]["id"])
        self.assertEqual("high", item["decision"]["confidence"])
        self.assertEqual(WEEK_FOLDER, item["action"]["target_folder"])

    def test_unique_consortium_contact_name_routes_to_week(self) -> None:
        item = _classify(
            ambiguous_catalog(),
            preview="Please coordinate the questionnaire with Dr. Susan Ouko.",
        )

        self.assertEqual("project", item["decision"]["kind"])
        self.assertEqual("week", item["decision"]["id"])
        self.assertEqual(WEEK_FOLDER, item["action"]["target_folder"])

    def test_unique_week_contact_in_cc_breaks_the_tie_without_body_text(self) -> None:
        item = _classify(
            ambiguous_catalog(),
            cc="Susan Ouko <susan.ouko@tuc.ac.ke>",
        )

        self.assertEqual("project", item["decision"]["kind"])
        self.assertEqual("week", item["decision"]["id"])
        self.assertEqual(WEEK_FOLDER, item["action"]["target_folder"])

    def test_unique_project_domain_routes_to_week(self) -> None:
        item = _classify(
            ambiguous_catalog(),
            preview="The results are published at https://tuc.ac.ke/results today.",
        )

        self.assertEqual("project", item["decision"]["kind"])
        self.assertEqual("week", item["decision"]["id"])
        self.assertEqual(WEEK_FOLDER, item["action"]["target_folder"])

    def test_unique_project_name_in_body_routes_to_week(self) -> None:
        item = _classify(
            ambiguous_catalog(),
            preview="This is the WEEK final report for the questionnaire.",
        )

        self.assertEqual("project", item["decision"]["kind"])
        self.assertEqual("week", item["decision"]["id"])
        self.assertEqual(WEEK_FOLDER, item["action"]["target_folder"])

    def test_project_code_in_subject_keeps_its_precedence(self) -> None:
        item = _classify(
            ambiguous_catalog(),
            subject="WEEK questionnaire information",
        )

        self.assertEqual("project", item["decision"]["kind"])
        self.assertEqual("week", item["decision"]["id"])
        self.assertEqual("high", item["decision"]["confidence"])

    def test_single_affiliation_contact_stays_high_confidence(self) -> None:
        item = _classify([project_atael()])

        self.assertEqual("project", item["decision"]["kind"])
        self.assertEqual("atael", item["decision"]["id"])
        self.assertEqual("high", item["decision"]["confidence"])
        self.assertEqual(ATAEL_FOLDER, item["action"]["target_folder"])

    def test_do_not_route_peer_is_not_part_of_the_ambiguity(self) -> None:
        suppressed_atael = project_atael(do_not_route_if=["newsletter"])
        suppressed: list[dict] = []
        result = _select_project(
            [suppressed_atael, project_week()],
            "Newsletter: QUESTIONNAIRE INFORMATION",
            suppressed=suppressed,
        )

        self.assertEqual(["atael"], [row["id"] for row in suppressed])
        self.assertIsNotNone(result)
        self.assertEqual("week", result["id"])
        self.assertEqual("high", result["confidence"])


class TwoPassFullReadTests(unittest.TestCase):
    """The preview Review outcome escalates once and the full body can resolve it."""

    def test_full_body_signal_resolves_the_preview_ambiguity(self) -> None:
        full_body = (
            "Dear Martin,\n"
            "we submit the questionnaire to Africa-UniNet. "
            "Dr. Susan Ouko from Turkana University College coordinated the data.\n"
        )
        calls: list[tuple] = []

        def full_reader(envelope_id, folder, account, **kwargs):
            calls.append((envelope_id, folder, account, kwargs.get("full_body")))
            return {"preview": full_body, "subject": GENERIC_SUBJECT}

        item = classifier.classify_email_two_pass(
            _email(),
            projects=ambiguous_catalog(),
            topics=[],
            sent_lookup={},
            final_index={"items": {}},
            full_reader=full_reader,
        )

        self.assertEqual(1, len(calls))
        self.assertEqual("project", item["decision"]["kind"])
        self.assertEqual("week", item["decision"]["id"])
        self.assertEqual(WEEK_FOLDER, item["action"]["target_folder"])
        self.assertEqual("completed", item["decision"]["read_escalation"]["status"])

    def test_full_body_staying_neutral_keeps_the_review_outcome(self) -> None:
        def full_reader(envelope_id, folder, account, **kwargs):
            return {"preview": "Dear colleagues,\nplease find the questionnaire attached.\n"}

        item = classifier.classify_email_two_pass(
            _email(),
            projects=ambiguous_catalog(),
            topics=[],
            sent_lookup={},
            final_index={"items": {}},
            full_reader=full_reader,
        )

        self.assertEqual("unknown", item["decision"]["kind"])
        self.assertEqual("INBOX", item["action"]["target_folder"])
        self.assertIs(True, item["decision"]["review_required"])
        self.assertEqual("completed", item["decision"]["read_escalation"]["status"])


class OwnerContractTests(unittest.TestCase):
    """The canonical root-match owner exposes the documented ambiguous result."""

    def test_ambiguous_result_shape_and_sorted_candidates(self) -> None:
        result = _select_project(ambiguous_catalog(), GENERIC_SUBJECT)

        self.assertEqual(
            {
                "id": "unclassified",
                "folder": "INBOX",
                "name": "",
                "confidence": "low",
                "catalog": None,
                "review_required": True,
                "review_reason": "ambiguous_contact_generic_subject",
                "candidates": ["ATAEL", "WEEK"],
            },
            result,
        )

    def test_normal_match_keeps_the_unchanged_result_shape(self) -> None:
        result = _select_project([project_week()], GENERIC_SUBJECT)

        self.assertIsNotNone(result)
        self.assertEqual(
            {"id", "folder", "name", "confidence", "catalog"}, set(result)
        )
        self.assertEqual("week", result["id"])
        self.assertEqual("high", result["confidence"])

    def test_body_scope_excludes_subject_and_headers(self) -> None:
        body = project_matching._body_scope_text(
            "QUESTIONNAIRE INFORMATION\nsender@example.test\ndesk@example.test\n"
            "cc@example.test\nPlease find the questionnaire attached.",
            "QUESTIONNAIRE INFORMATION",
            "sender@example.test",
            "desk@example.test",
            "cc@example.test",
        )
        self.assertEqual("Please find the questionnaire attached.", body)

    def test_short_acronym_in_prose_is_not_a_body_name_signal(self) -> None:
        self.assertFalse(
            project_matching._body_name_matches("WEEK", "We will reply this week.")
        )
        self.assertTrue(
            project_matching._body_name_matches("WEEK", "The WEEK final report.")
        )

    def test_address_boundary_rejects_local_part_substrings(self) -> None:
        self.assertTrue(
            project_matching._address_in_text(
                "ann@example.test", "Mail ann@example.test today."
            )
        )
        self.assertFalse(
            project_matching._address_in_text(
                "ann@example.test", "Mail joann@example.test today."
            )
        )

    def test_masking_removes_the_shared_address_and_embedded_domain(self) -> None:
        masked = project_matching._mask_addresses(
            "Signature n.kassilly@tuc.ac.ke",
            frozenset({"n.kassilly@tuc.ac.ke"}),
        )
        self.assertNotIn("tuc.ac.ke", masked)

    def test_academic_suffixes_do_not_break_the_name_signature(self) -> None:
        signature = project_matching._contact_name_signature(
            "Dr. Susan Ouko, PhD"
        )
        self.assertEqual("susan ouko", signature)
        self.assertTrue(
            project_matching._contact_name_in_text(
                signature, "Please coordinate with Dr. Susan Ouko."
            )
        )

    def test_shared_person_email_is_never_a_signal(self) -> None:
        result = _select_project(
            ambiguous_catalog(),
            GENERIC_SUBJECT,
            preview="Please write to nkasili@yahoo.com about the questionnaire.",
        )
        self.assertEqual("unclassified", result["id"])
        self.assertIs(True, result["review_required"])


class FallbackIsolationTests(unittest.TestCase):
    """The ambiguity Review outcome is not overridden by topic or DNR fallbacks."""

    def test_topic_match_does_not_override_the_project_ambiguity(self) -> None:
        topic = {
            "id": "questionnaires",
            "title": "Questionnaires",
            "mailbox_folder": "Themen/Questionnaires",
            "aliases": [],
            "keywords": ["questionnaire"],
            "typical_subject_patterns": [],
            "domains": [],
            "contacts": [],
            "subtopics": [],
        }

        item = _classify(
            ambiguous_catalog(),
            topics=[topic],
            preview="Please find the questionnaire data attached.",
        )

        self.assertEqual("unknown", item["decision"]["kind"])
        self.assertEqual("INBOX", item["action"]["target_folder"])
        self.assertIs(True, item["decision"]["review_required"])

    def test_zoom_notification_does_not_override_the_project_ambiguity(self) -> None:
        item = _classify(
            ambiguous_catalog(),
            subject="Nancy ist dem Meeting beigetreten",
            **{
                "from": "Zoom <no-reply@zoom.us>",
                "cc": "Nancy Kassilly <nkasili@yahoo.com>",
            },
        )

        self.assertEqual("unknown", item["decision"]["kind"])
        self.assertEqual("INBOX", item["action"]["target_folder"])
        self.assertIs(True, item["decision"]["review_required"])

    def test_newsletter_suppression_does_not_override_the_project_ambiguity(self) -> None:
        topic = {
            "id": "newsletter-archive",
            "title": "Newsletter Archive",
            "mailbox_folder": "Newsletter",
            "aliases": [],
            "keywords": [],
            "typical_subject_patterns": [],
            "domains": [],
            "contacts": [],
            "do_not_route_if": ["newsletter"],
            "subtopics": [],
        }
        third = project_atael(
            id="third",
            kuerzel="THIRD",
            mailbox_folder="Projekte/THIRD",
            contacts=[],
            do_not_route_if=["newsletter"],
        )

        item = _classify(
            [project_atael(), project_week(), third],
            topics=[topic],
            subject="Newsletter: QUESTIONNAIRE INFORMATION",
        )

        self.assertEqual("unknown", item["decision"]["kind"])
        self.assertEqual("INBOX", item["action"]["target_folder"])
        self.assertIs(True, item["decision"]["review_required"])
        self.assertEqual("third", item["decision"]["suppressed_candidates"][0]["id"])


if __name__ == "__main__":
    unittest.main()
