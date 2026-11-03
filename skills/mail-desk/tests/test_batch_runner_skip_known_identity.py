"""Issue #66: ``skip_known`` skips only an envelope's own Message-ID.

The subject/from signature index is a non-authoritative routing hint.  A
physically present thread reply or same-subject mail with a new Message-ID must
be read and collected even when the signature matches a known record.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

from core.index import build_signature_index  # noqa: E402
import mail_desk_batch_runner as runner  # noqa: E402


KNOWN_MID = "known-alpha@example.test"
REPLY_MID = "reply-2@example.test"
OTHER_SENDER_MID = "other-sender-3@example.test"
SUBJECT = "Project Alpha Update"


def _envelopes() -> list[dict]:
    return [
        {"id": "1", "date": "Mon, 5 Jan 2026 09:00:00 +0000", "subject": SUBJECT,
         "from": {"addr": "alice@example.test"}},
        # Thread reply: same normalized subject and sender, new Message-ID.
        {"id": "2", "date": "Tue, 6 Jan 2026 09:00:00 +0000", "subject": SUBJECT,
         "from": {"addr": "alice@example.test"}},
        # Same normalized subject, different sender -> (subject, "") hint key.
        {"id": "3", "date": "Wed, 7 Jan 2026 09:00:00 +0000", "subject": SUBJECT,
         "from": {"addr": "bob@example.test"}},
    ]


def _details(envelope_id, *_args, **_kwargs) -> dict:
    return {
        "envelope_id": str(envelope_id),
        "message_id": {"1": KNOWN_MID, "2": REPLY_MID, "3": OTHER_SENDER_MID}[str(envelope_id)],
        "subject": SUBJECT,
        "from": f"{envelope_id}@example.test",
    }


def _known_items() -> dict:
    return {KNOWN_MID: {"message_id": KNOWN_MID, "final_folder": "Archive/Known"}}


def _signatures() -> dict:
    # Both hint keys point at the known message; a hint must never skip.
    hint = {"message_id": KNOWN_MID, "final_folder": "Hinted/Folder"}
    return {
        (SUBJECT.lower(), "alice@example.test"): hint,
        (SUBJECT.lower(), ""): hint,
    }


class QueryPathSkipKnownTests(unittest.TestCase):
    def _scan(self, *, skip_known: bool = True):
        with patch.object(runner, "load_final_index", return_value={"items": _known_items()}), \
                patch.object(runner, "build_signature_index", return_value=_signatures()), \
                patch.object(runner, "run_himalaya", return_value=json.dumps(_envelopes())), \
                patch.object(runner, "get_single_email_details", side_effect=_details) as read, \
                patch.object(runner.time, "sleep"):
            collected, known_count = runner.get_unprocessed_emails(
                "INBOX", 3, order="oldest", query="subject project", data_dir=Path("data/mail-desk"),
                skip_known=skip_known,
            )
        return collected, known_count, read

    def test_only_the_own_message_id_is_skipped(self) -> None:
        collected, known_count, read = self._scan()

        self.assertEqual([REPLY_MID, OTHER_SENDER_MID], [item["message_id"] for item in collected])
        self.assertEqual(1, known_count, "exactly the known Message-ID counts once")
        # The new mail is fully read; the known envelope is inspected too so its
        # Message-ID can be compared before the skip decision.
        self.assertEqual(["1", "2", "3"], [call.args[0] for call in read.call_args_list])

        reply, other_sender = collected
        self.assertFalse(reply["is_known"])
        self.assertIsNone(reply["known_location"])
        self.assertEqual("Hinted/Folder", reply["signature_hint"]["final_folder"])
        self.assertFalse(other_sender["is_known"])
        self.assertIsNone(other_sender["known_location"])
        self.assertIn("signature_hint", other_sender, "different-sender fallback stays a hint only")

    def test_known_location_never_comes_from_a_signature_match(self) -> None:
        collected, known_count, _ = self._scan(skip_known=False)

        self.assertEqual([KNOWN_MID, REPLY_MID, OTHER_SENDER_MID], [item["message_id"] for item in collected])
        self.assertEqual(1, known_count)
        known = collected[0]
        self.assertTrue(known["is_known"])
        self.assertEqual("Archive/Known", known["known_location"])
        self.assertNotIn("signature_hint", known)


class PagedPathSkipKnownTests(unittest.TestCase):
    def test_paged_path_uses_the_same_message_id_identity(self) -> None:
        with patch.object(runner, "load_final_index", return_value={"items": _known_items()}), \
                patch.object(runner, "build_signature_index", return_value=_signatures()), \
                patch.object(runner, "get_oldest_envelopes", return_value=_envelopes()), \
                patch.object(runner, "get_single_email_details", side_effect=_details) as read, \
                patch.object(runner.time, "sleep"):
            collected, known_count = runner.get_unprocessed_emails(
                "INBOX", 3, data_dir=Path("data/mail-desk"),
            )

        self.assertEqual([REPLY_MID, OTHER_SENDER_MID], [item["message_id"] for item in collected])
        self.assertEqual(1, known_count)
        self.assertEqual(["1", "2", "3"], [call.args[0] for call in read.call_args_list])


class SignatureIndexAmbiguityTests(unittest.TestCase):
    def _build(self, items: dict) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            (data_dir / "final-location-index.json").write_text(
                json.dumps({"schema_version": 1, "items": items}), encoding="utf-8",
            )
            return build_signature_index(data_dir)

    def test_ambiguous_subject_gets_no_senderless_fallback(self) -> None:
        signatures = self._build({
            "a@example.test": {"message_id": "A@Example.test", "subject": "Weekly Report",
                               "from": "Alice <alice@example.test>", "final_folder": "Archive/A"},
            "b@example.test": {"message_id": "b@example.test", "subject": "Weekly Report",
                               "from": "Alice <alice@example.test>", "final_folder": "Archive/B"},
        })

        self.assertNotIn(("weekly report", ""), signatures)
        self.assertIn(("weekly report", "alice@example.test"), signatures)

    def test_ambiguous_empty_sender_subject_gets_no_fallback(self) -> None:
        signatures = self._build({
            "a@example.test": {"message_id": "a@example.test", "subject": "Generic Subject",
                               "from": "", "final_folder": "Archive/A"},
            "b@example.test": {"message_id": "b@example.test", "subject": "Generic Subject",
                               "from": "", "final_folder": "Archive/B"},
        })

        self.assertNotIn(("generic subject", ""), signatures)

    def test_unique_subject_keeps_the_senderless_fallback(self) -> None:
        signatures = self._build({
            "solo@example.test": {"message_id": "solo@example.test", "subject": "Unique Subject",
                                  "from": "solo@example.test", "final_folder": "Archive/Solo"},
        })

        self.assertEqual("Archive/Solo", signatures[("unique subject", "")]["final_folder"])
        self.assertIn(("unique subject", "solo@example.test"), signatures)


if __name__ == "__main__":
    unittest.main()
