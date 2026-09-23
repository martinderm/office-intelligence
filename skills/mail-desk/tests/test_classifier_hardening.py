"""Tests for classifier hardening: acronym safe matching, do_not_route_if, forwarded senders, notifications, spam.

FR-22/MD-ID4 additionally hardens the junk-freemailer branch (``classifier.py``
2-sys): the four identity-bearing BOKU content counter-indicators are removed so
a phishing-pattern subject from a freemail domain is junked unconditionally, and a
schema-2 ``spam_sender_allowlist`` catalog entry (exact address OR domain,
case-insensitive) exempts a trusted sender from that branch.  Those contracts are
pinned by :class:`JunkFreemailerHardeningTests` with hermetic temp-workspace
catalogs; no real catalog, mailbox or network is touched.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

from core import classifier  # noqa: E402


#: Desk-signals catalog location, relative to a hermetic temp workspace root.
CATALOG_RELATIVE_PATH = Path("memory") / "references" / "mail-desk" / "mail-desk.json"


class _CatalogWorkspace:
    """Hermetic temp workspace that never touches a real catalog or mailbox."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()

    def __enter__(self) -> Path:
        return Path(self._tmp.name)

    def __exit__(self, *exc: object) -> None:
        self._tmp.cleanup()


def write_catalog(workspace_root: Path, payload: object) -> Path:
    """Write the desk-signals catalog into a hermetic workspace root."""
    path = workspace_root / CATALOG_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _schema2_catalog(*, spam_sender_allowlist: tuple[str, ...] = ()) -> dict:
    """Minimal schema-2 desk-signals catalog carrying the MD-ID4 allowlist."""
    return {
        "schema_version": 2,
        "reply_heuristics": {
            "owner_address": "desk@example.org",
            "spam_sender_allowlist": list(spam_sender_allowlist),
        },
    }


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


class JunkFreemailerHardeningTests(unittest.TestCase):
    """FR-22/MD-ID4 junk-freemailer contracts (counter-indicator removal + allowlist).

    The junk-freemailer branch requires a phishing-pattern subject AND a freemail
    sender domain.  MD-ID4 (a) removes the BOKU content counter-indicators and
    (b) adds a schema-2 ``spam_sender_allowlist`` exemption (exact address or
    domain, case-insensitive).  A sender that is not exempted still lands in Junk.
    """

    #: Phishing subject matching the branch regex ``\burgent inquiry\b``.
    PHISHING_SUBJECT = "Fw: Urgent Inquiry and Request for Clarification"
    #: Body text free of any former counter-indicator keyword.
    PLAIN_PREVIEW = "Urgent inquiry regarding proposal..."

    def _email(self, *, from_: str, preview: str = PLAIN_PREVIEW) -> dict:
        return {
            "envelope_id": "md-id4",
            "folder": "INBOX",
            "subject": self.PHISHING_SUBJECT,
            "from": from_,
            "to": "martin.mayr@boku.ac.at",
            "preview": preview,
        }

    def _classify(self, workspace_root: Path, email: dict) -> dict:
        return classifier.classify_email(
            email,
            workspace_root=workspace_root,
            projects=[],
            topics=[],
        )

    def test_freemailer_with_counter_indicator_still_junk(self) -> None:
        # MD-ID4: the four BOKU content counter-indicators are gone, so a former
        # rescue keyword in the body no longer suppresses the freemailer junk.
        with _CatalogWorkspace() as ws:
            item = self._classify(
                ws,
                self._email(
                    from_="qcpd2055@yahoo.com",
                    preview="weiterbildung programm fuer interessierte",
                ),
            )

        self.assertEqual(item["action"]["target_folder"], "Junk")
        self.assertEqual(item["decision"]["kind"], "spam")

    def test_allowlisted_sender_not_junked(self) -> None:
        # An exact-address allowlist entry exempts a freemail phishing sender.
        with _CatalogWorkspace() as ws:
            write_catalog(
                ws,
                _schema2_catalog(spam_sender_allowlist=("trusted@yahoo.com",)),
            )
            item = self._classify(ws, self._email(from_="trusted@yahoo.com"))

        self.assertNotEqual(item["action"]["target_folder"], "Junk")
        self.assertNotEqual(item["decision"]["kind"], "spam")

    def test_allowlisted_domain_not_junked(self) -> None:
        # A domain allowlist entry (case-insensitive) exempts any sender on it.
        with _CatalogWorkspace() as ws:
            write_catalog(
                ws,
                _schema2_catalog(spam_sender_allowlist=("YAHOO.COM",)),
            )
            item = self._classify(ws, self._email(from_="person@yahoo.com"))

        self.assertNotEqual(item["action"]["target_folder"], "Junk")
        self.assertNotEqual(item["decision"]["kind"], "spam")

    def test_non_allowlisted_freemailer_still_junked(self) -> None:
        # Control: a non-matching allowlist must not exempt an unrelated freemailer.
        with _CatalogWorkspace() as ws:
            write_catalog(
                ws,
                _schema2_catalog(spam_sender_allowlist=("example.org",)),
            )
            item = self._classify(ws, self._email(from_="attacker@hotmail.com"))

        self.assertEqual(item["action"]["target_folder"], "Junk")
        self.assertEqual(item["decision"]["kind"], "spam")


if __name__ == "__main__":
    unittest.main()
