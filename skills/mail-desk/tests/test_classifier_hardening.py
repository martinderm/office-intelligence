"""Tests for classifier hardening: acronym safe matching, do_not_route_if, forwarded senders, notifications, spam."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

from core import classifier  # noqa: E402


class ClassifierHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.week_project = {
            "id": "week",
            "kuerzel": "WEEK",
            "title": "WEEK - Africa-UniNet",
            "mailbox_folder": "Projekte/WEEK",
            "aliases": ["WEEK"],
            "keywords": ["turkana"],
            "typical_subject_patterns": ["WEEK", "P147"],
            "do_not_route_if": ["newsletter", "mailing list"],
            "contacts": [{"email": "week.coordinator@boku.ac.at"}],
        }
        self.meshe_project = {
            "id": "meshe",
            "kuerzel": "MESHE",
            "title": "MESHE",
            "mailbox_folder": "Projekte/MESHE",
            "aliases": ["MESHE"],
            "contacts": [{"name": "Carme Royo", "email": "carme.royo@eucen.eu"}],
            "domains": ["eucen.eu", "meshe.eucen.eu"],
        }

    def test_short_acronym_prose_does_not_falsely_match(self) -> None:
        email = {
            "envelope_id": "9093",
            "folder": "INBOX",
            "subject": "The Mid-Level Academic Staff Meeting has been cancelled this week",
            "from": "mittelbau@ls.tum.de",
            "to": "ls_wiss_ma@ls.tum.de",
            "preview": "Meeting cancelled for this week.",
        }
        result = classifier.classify_email(email, projects=[self.week_project], topics=[])
        self.assertEqual(result["decision"]["kind"], "unknown")
        self.assertNotEqual(result["action"]["target_folder"], "Projekte/WEEK")

    def test_short_acronym_exact_case_matches_project(self) -> None:
        email = {
            "envelope_id": "9079",
            "folder": "INBOX",
            "subject": "Wtrlt: AW: Projekt 147 – WEEK: Bericht und Terminabstimmung",
            "from": "christina.paulus@boku.ac.at",
            "to": "martin.mayr@boku.ac.at",
            "preview": "Hier der Bericht fuer WEEK.",
        }
        result = classifier.classify_email(email, projects=[self.week_project], topics=[])
        self.assertEqual(result["decision"]["kind"], "project")
        self.assertEqual(result["decision"]["id"], "week")
        self.assertEqual(result["action"]["target_folder"], "Projekte/WEEK")

    def test_do_not_route_if_mailing_list_blocks_routing(self) -> None:
        email = {
            "envelope_id": "1234",
            "folder": "INBOX",
            "subject": "WEEK Meeting Information",
            "from": "newsletter@tum.de",
            "to": "Verteilerliste Wissenschaftliche Mitarbeiter:ls@tum.de",
            "preview": "Information sent to mailing list.",
        }
        result = classifier.classify_email(email, projects=[self.week_project], topics=[])
        self.assertNotEqual(result["action"]["target_folder"], "Projekte/WEEK")

    def test_forwarded_sender_extracts_project_contact(self) -> None:
        email = {
            "envelope_id": "9090",
            "folder": "INBOX",
            "subject": "Wtrlt: Re: back from hospital and two questions",
            "from": "Christina Paulus <christina.paulus@boku.ac.at>",
            "to": "Mayr Martin <martin.mayr@boku.ac.at>",
            "preview": ">>> Carme ROYO <carme.royo@eucen.eu> 25.05.2026 18:23 >>>\nDear Christina,\nAttached the abstract...",
        }
        result = classifier.classify_email(email, projects=[self.meshe_project], topics=[])
        self.assertEqual(result["decision"]["kind"], "project")
        self.assertEqual(result["decision"]["id"], "meshe")
        self.assertEqual(result["action"]["target_folder"], "Projekte/MESHE")

    def test_zoom_join_ping_routed_to_trash(self) -> None:
        email = {
            "envelope_id": "9087",
            "folder": "INBOX",
            "subject": "Susanne Hinterberger ist dem Meeting beigetreten - Roadmap",
            "from": "Zoom <no-reply@zoom.us>",
            "to": "martin.mayr@boku.ac.at",
            "preview": "Susanne Hinterberger ist dem Meeting beigetreten.",
        }
        result = classifier.classify_email(email, projects=[self.week_project], topics=[])
        self.assertEqual(result["action"]["target_folder"], "Trash")
        self.assertEqual(result["decision"]["kind"], "notification")

    def test_junk_freemailer_spam_routed_to_junk(self) -> None:
        email = {
            "envelope_id": "9094",
            "folder": "INBOX",
            "subject": "Fw: Urgent Inquiry and Request for Clarification",
            "from": "qcpd qcpd <qcpd2055@yahoo.com>",
            "to": "martin.mayr@boku.ac.at",
            "preview": "Urgent inquiry regarding proposal...",
        }
        result = classifier.classify_email(email, projects=[self.week_project], topics=[])
        self.assertEqual(result["action"]["target_folder"], "Junk")
        self.assertEqual(result["decision"]["kind"], "spam")

    def test_common_word_project_prose_does_not_match(self) -> None:
        latest_project = {
            "id": "latest",
            "kuerzel": "LATEST",
            "mailbox_folder": "Projekte/LATEST",
            "aliases": ["LATEST"],
            "keywords": ["microcredentials", "benchmarking"],
        }
        email = {
            "envelope_id": "9103",
            "folder": "INBOX",
            "subject": "SAVE THE DATE – Cedefop virtual get-together 'Upskilling Europe: Microcredentials in action'",
            "from": "SANTOS, Maria Teresa <maite.santos@cedefop.europa.eu>",
            "to": "martin.mayr@boku.ac.at",
            "preview": "Participants will get latest insights on two labour market sectors...",
        }
        result = classifier.classify_email(email, projects=[latest_project], topics=[])
        self.assertNotEqual(result["action"]["target_folder"], "Projekte/LATEST")

    def test_subtopic_id_overrides_generic_root_keyword(self) -> None:
        aixlll_topic = {
            "id": "aixlll",
            "title": "AIxLLL",
            "mailbox_folder": "Themen/AIxLLL",
            "keywords": ["ai", "upskilling", "reskilling"],
            "subtopics": [],
        }
        netzwerke_topic = {
            "id": "netzwerke",
            "title": "Netzwerke",
            "mailbox_folder": "Themen/Netzwerke",
            "domains": ["cedefop.europa.eu"],
            "keywords": ["CEDEFOP"],
            "subtopics": [
                {
                    "id": "cedefop",
                    "title": "CEDEFOP",
                    "keywords": ["CEDEFOP", "cedefop.europa.eu"],
                    "status": "active",
                }
            ],
        }
        email = {
            "envelope_id": "9103",
            "folder": "INBOX",
            "subject": "SAVE THE DATE – Cedefop virtual get-together 'Upskilling Europe: Microcredentials in action'",
            "from": "SANTOS, Maria Teresa <maite.santos@cedefop.europa.eu>",
            "to": "martin.mayr@boku.ac.at",
            "preview": "Cedefop virtual get-together on upskilling Europe...",
        }
        result = classifier.classify_email(email, projects=[], topics=[aixlll_topic, netzwerke_topic])
        self.assertEqual(result["action"]["target_folder"], "Themen/Netzwerke")
        self.assertEqual(result["decision"]["subtopic"], "cedefop")


if __name__ == "__main__":
    unittest.main()
