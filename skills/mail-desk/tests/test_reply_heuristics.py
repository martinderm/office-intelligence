"""FR-17/MD-R8 reply heuristic contracts: closing/thank-you mails never require a reply.

``needs_reply`` is bound to a concrete request (question, ask, deadline, decision,
approval, contribution).  A closing/thank-you mail -- in the preview or only in the
full body of a previously escalated item -- must downgrade to ``needs_reply: false``
instead of silently asserting ``true``.  Review semantics for remaining ambiguity
are unchanged.
"""

from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import Mock


MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

from core import classifier  # noqa: E402
from core.matching import reply_heuristics  # noqa: E402


class ReplyHeuristicOwnerTests(unittest.TestCase):
    """Unit contracts for the canonical reply-heuristics owner."""

    def test_pure_closing_thanks_is_detected_and_not_reply_worthy(self) -> None:
        # `is_closing_or_thanks` is the downgrade *condition*: a pure closing mail
        # is detected (True) and therefore must never stay reply-worthy.
        text = (
            "Hallo Martin,\n\nvielen Dank für das Zusammenstellen der Unterlagen. "
            "Passt für mich. Liebe Grüße Claus"
        )
        self.assertTrue(reply_heuristics.is_closing_or_thanks(text))
        self.assertFalse(reply_heuristics.needs_reply_review(text))

    def test_thanks_with_follow_up_announcement_stays_ambiguous(self) -> None:
        # "Danke" plus a concrete follow-up request keeps the review semantics.
        text = (
            "Vielen Dank für die Unterlagen. Bitte ergänze noch den Link "
            "bis Ende nächster Woche."
        )
        self.assertTrue(reply_heuristics.needs_reply_review(text))

    def test_explicit_request_is_never_downgraded(self) -> None:
        text = "Vielen Dank für die Rückmeldung. Könntest du mir das Protokoll bitte senden?"
        self.assertFalse(reply_heuristics.is_closing_or_thanks(text))
        self.assertTrue(reply_heuristics.needs_reply_review(text))

    def test_empty_text_is_not_closing(self) -> None:
        self.assertFalse(reply_heuristics.is_closing_or_thanks(""))
        self.assertFalse(reply_heuristics.needs_reply_review(""))

    def test_quoted_predecessor_request_does_not_block_closing(self) -> None:
        # Live case B-11 (Env 9412): the thanks is the new content; the quoted
        # predecessor announces a delivery ("wird ... ergänzen") and must not
        # keep the reply requirement alive.
        text = (
            "Liebe Christina, lieber Martin,\n\n"
            "vielen Dank für das Zusammenstellen der Unterlagen.\n\n"
            "Liebe Grüße\nClaus\n"
            ">>> Martin Mayr <martin.mayr@boku.ac.at> 30.07.2026 14:26 >>>\n\n"
            "Lieber Claus,\n\nunter dem Link findest du ausgewählte Aktivitäten:\n"
            "Christina wird morgen noch eine Präsentation ergänzen.\n"
        )
        self.assertTrue(reply_heuristics.is_closing_or_thanks(text))
        self.assertFalse(reply_heuristics.needs_reply_review(text))

    def test_quote_boundary_variants_are_stripped(self) -> None:
        for separator in (
            "Am 30.07.2026 schrieb Martin Mayr <martin.mayr@boku.ac.at>:",
            "-----Ursprüngliche Nachricht-----",
            "________________________________",
            "> Bitte ergänze noch den Link.",
        ):
            with self.subTest(separator=separator):
                text = f"Vielen Dank für die Unterlagen.\n{separator}\nBitte ergänze den Link bis Freitag.\n"
                self.assertTrue(reply_heuristics.is_closing_or_thanks(text))

    def test_request_in_new_part_blocks_closing_with_quoted_farewell(self) -> None:
        text = (
            "Hallo Martin, bitte sende mir noch das Protokoll.\n\n"
            ">>> Martin Mayr <martin.mayr@boku.ac.at> 30.07.2026 14:26 >>>\n\n"
            "Vielen Dank und liebe Grüße\nClaus\n"
        )
        self.assertFalse(reply_heuristics.is_closing_or_thanks(text))
        self.assertTrue(reply_heuristics.needs_reply_review(text))


class ClosingDowngradeTests(unittest.TestCase):
    """Facade contracts: the closing/thanks downgrade applies in both passes."""

    @staticmethod
    def project() -> dict:
        return {
            "id": "atael",
            "kuerzel": "ATAEL",
            "title": "ATAEL",
            "mailbox_folder": "Projects/ATAEL",
            "schema_version": 3,
            "workpackages": [],
            "milestones": [],
        }

    def email(self, subject: str, preview: str = "", body: str | None = None) -> dict:
        mail = {
            "envelope_id": "9912",
            "folder": "INBOX",
            "message_id": "closing@example.test",
            "subject": subject,
            "preview": preview,
        }
        if body is not None:
            mail["preview"] = body
        return mail

    def two_pass(self, email: dict, reader) -> dict:
        return classifier.classify_email_two_pass(
            email,
            projects=[self.project()],
            topics=[],
            sent_lookup={},
            final_index={"items": {}},
            full_reader=reader,
            account="test-account",
        )

    def test_closing_thanks_thread_reply_is_downgraded(self) -> None:
        # Thread-Vererbung to a project folder must not assert needs_reply for a
        # pure closing mail.
        reader = Mock()
        email = self.email("AW: ATAEL Lieferung", body="Vielen Dank für das Zusammenstellen. Liebe Grüße")
        email["in_reply_to"] = "<parent@example.test>"
        item = self.two_pass(
            email,
            lambda *args, **kwargs: dict(
                self.email("AW: ATAEL Lieferung", body="Vielen Dank für das Zusammenstellen. Liebe Grüße")
            ),
        )
        self.assertFalse(item["decision"]["needs_reply"])

    def test_full_read_closing_body_downgrades_preview_needs_reply(self) -> None:
        # The preview only contains a trigger ("bitte"); the full body reveals a
        # pure closing/thank-you message, so the reply decision downgrades.
        email = self.email(
            "AW: ATAEL Lieferung",
            preview="Bitte um kurze Rückmeldung zur Lieferung.",
            body=None,
        )
        full = dict(email)
        full["preview"] = "Vielen Dank für das Zusammenstellen der Unterlagen. Liebe Grüße Claus"
        reader = Mock(return_value=full)
        item = self.two_pass(email, reader)
        self.assertTrue(reader.called)
        self.assertFalse(item["decision"]["needs_reply"])

    def test_closing_with_quoted_predecessor_downgrades(self) -> None:
        # Live case B-11: closing thanks plus the quoted delivery predecessor.
        body = (
            "Liebe Christina, lieber Martin,\n\n"
            "vielen Dank für das Zusammenstellen der Unterlagen.\n\n"
            "Liebe Grüße\nClaus\n"
            ">>> Martin Mayr <martin.mayr@boku.ac.at> 30.07.2026 14:26 >>>\n\n"
            "Lieber Claus,\n\nunter dem Link findest du ausgewählte Aktivitäten:\n"
            "Christina wird morgen noch eine Präsentation ergänzen.\n"
        )
        email = self.email("AW: ATAEL Lieferung", body=body)
        email["in_reply_to"] = "<parent@example.test>"
        item = self.two_pass(email, lambda *args, **kwargs: dict(email))
        decision = item["decision"]
        self.assertFalse(decision["needs_reply"])
        self.assertEqual(decision["reply_downgrade"]["rule_revision"], "md-r8")
        self.assertEqual([key for key in decision if key.startswith("_")], [])

    def test_facade_decision_has_no_closing_check_debug_keys(self) -> None:
        email = self.email(
            "AW: ATAEL Lieferung",
            preview="Hallo Martin, kannst du mir das Protokoll bitte senden?",
            body=None,
        )
        full = dict(email)
        full["preview"] = (
            "Hallo Martin, kannst du mir das Protokoll bitte senden? "
            "Vielen Dank im Voraus."
        )
        item = self.two_pass(email, Mock(return_value=full))
        decision = item["decision"]
        self.assertTrue(decision["needs_reply"])
        self.assertEqual([key for key in decision if key.startswith("_")], [])

    def test_real_request_body_is_never_downgraded(self) -> None:
        email = self.email(
            "ATAEL Protokoll",
            preview="Hallo Martin, kannst du mir das Protokoll bitte senden?",
            body=None,
        )
        full = dict(email)
        full["preview"] = (
            "Hallo Martin, kannst du mir das Protokoll bitte senden? "
            "Vielen Dank im Voraus."
        )
        reader = Mock(return_value=full)
        item = self.two_pass(email, reader)
        self.assertTrue(item["decision"]["needs_reply"])


if __name__ == "__main__":
    unittest.main()