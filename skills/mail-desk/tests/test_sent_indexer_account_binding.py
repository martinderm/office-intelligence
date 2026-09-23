"""FR-18/MD-S2 sent-index account-binding contracts (tests-only Red phase).

The sent-items indexer must tag every entry with the verified batch ``account``
that already flows through ``sync_sent_items``/the batch run, replacing the
hardcoded ``"mailbox": "BOKU-MARTIN"`` literal in ``core/sent_indexer.py``.  A
sync without a bound account must fail loud with a structured ``ValueError``
before any entry is written, and the entry structure for a given account stays
identical to the pre-MD-S2 shape apart from the ``mailbox`` value.

MD-S2 contract pinned by this suite (the implementation must satisfy it):

* ``sync_sent_items_by_date(date, folder=..., account="OTHER-ACCOUNT", data_dir=...)``
  writes an entry whose ``mailbox`` equals the passed account.
* ``account=None`` raises a ``ValueError`` (or subclass) whose message names the
  account binding, before ``sent-index.jsonl`` is created.
* With ``account="BOKU-MARTIN"`` the entry structure is byte-for-byte identical
  to HEAD apart from the (now account-derived) ``mailbox`` value.
* The ``BOKU-MARTIN`` literal disappears from ``sent_indexer.py``.

All fixtures are hermetic: ``core.sent_indexer.run_himalaya`` and
``core.sent_indexer.get_single_email_details`` are patched in the module
namespace (the same seam the production code imports), so no mailbox, network or
real data directory is ever touched.
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

from core import sent_indexer  # noqa: E402


DATE = "2026-09-23"
FOLDER = "Sent Items"
ENVELOPE_ID = "4242"
RAW_MESSAGE_ID = "sent-abc123@example.test"
SENT_INDEX_NAME = "sent-index.jsonl"

#: The exact HEAD entry key set produced by ``sync_sent_items_by_date``.
HEAD_ENTRY_KEYS = (
    "schema_version",
    "at",
    "updated_at",
    "mailbox",
    "message_id",
    "in_reply_to",
    "references",
    "subject",
    "from",
    "to",
    "folder",
    "sent_envelope_id",
    "backend_locator",
    "note",
)

#: Deterministic single-message read result returned for the one envelope.
SENT_DETAILS = {
    "message_id": RAW_MESSAGE_ID,
    "raw_message_id": RAW_MESSAGE_ID,
    "in_reply_to": "root-1@example.test",
    "references": ["root-1@example.test", "mid-2@example.test"],
    "subject": "Re: Projekt Update",
    "from": "Desk Owner <owner@example.test>",
    "to": "partner@example.test, second@example.test",
    "date": "2026-09-23T10:00:00Z",
}


def _envelope_list_payload() -> str:
    """Hermetic ``himalaya envelope list -o json`` payload with one envelope."""
    return json.dumps([{"id": ENVELOPE_ID, "subject": SENT_DETAILS["subject"]}])


def _expected_entry(account: str, updated_at: str) -> dict:
    """The HEAD entry shape for the fixture, with ``mailbox`` bound to account."""
    return {
        "schema_version": 1,
        "at": SENT_DETAILS["date"],
        "updated_at": updated_at,
        "mailbox": account,
        "message_id": f"<{RAW_MESSAGE_ID}>",
        "in_reply_to": "<root-1@example.test>",
        "references": ["<root-1@example.test>", "<mid-2@example.test>"],
        "subject": "Re: Projekt Update",
        "from": "Desk Owner <owner@example.test>",
        "to": ["partner@example.test", "second@example.test"],
        "folder": FOLDER,
        "sent_envelope_id": ENVELOPE_ID,
        "backend_locator": f"{ENVELOPE_ID}|{RAW_MESSAGE_ID}",
        "note": "Gesendet: Re: Projekt Update",
    }


class _SentIndexerFixture(unittest.TestCase):
    """Shared hermetic temp data directory and mocked himalaya seam."""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.data_dir = Path(self._temporary.name) / "data" / "mail-desk"
        self.data_dir.mkdir(parents=True)

    def _patched_himalaya(self):
        return patch.object(
            sent_indexer, "run_himalaya", return_value=_envelope_list_payload()
        ), patch.object(
            sent_indexer,
            "get_single_email_details",
            return_value=dict(SENT_DETAILS),
        )

    def _sync(self, account: str | None) -> int:
        run_patch, details_patch = self._patched_himalaya()
        with run_patch, details_patch:
            return sent_indexer.sync_sent_items_by_date(
                DATE, folder=FOLDER, account=account, data_dir=self.data_dir
            )

    def _read_single_entry(self) -> dict:
        sent_path = self.data_dir / SENT_INDEX_NAME
        self.assertTrue(
            sent_path.exists(), f"{SENT_INDEX_NAME} must exist after a bound sync"
        )
        lines = [
            line
            for line in sent_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(1, len(lines), "exactly one entry must be written")
        return json.loads(lines[0])


class SentIndexAccountBindingTests(_SentIndexerFixture):
    """MD-S2 acceptance: mailbox = verified account, fail loud without account."""

    def test_entry_mailbox_carries_bound_account(self) -> None:
        added = self._sync("OTHER-ACCOUNT")
        self.assertEqual(1, added)

        entry = self._read_single_entry()
        self.assertEqual(set(HEAD_ENTRY_KEYS), set(entry))
        self.assertEqual("OTHER-ACCOUNT", entry["mailbox"])
        self.assertNotEqual("BOKU-MARTIN", entry["mailbox"])
        self.assertEqual(
            _expected_entry("OTHER-ACCOUNT", entry["updated_at"]),
            entry,
            "only the mailbox value may change; the entry shape stays at HEAD",
        )

    def test_missing_account_fails_loud_without_write(self) -> None:
        run_patch, details_patch = self._patched_himalaya()
        with run_patch, details_patch:
            with self.assertRaises(ValueError) as raised:
                sent_indexer.sync_sent_items_by_date(
                    DATE, folder=FOLDER, account=None, data_dir=self.data_dir
                )

        message = str(raised.exception).strip()
        self.assertTrue(message, "the fail-loud error must carry a structured message")
        lowered = message.lower()
        self.assertIn(
            "account", lowered, "the error must name the missing account binding"
        )
        self.assertIn(
            "bind",
            lowered,
            "the error must describe the failed account binding (consistent with "
            "attachments.py)",
        )
        self.assertFalse(
            (self.data_dir / SENT_INDEX_NAME).exists(),
            "no entry may be written when the account binding is missing",
        )

    def test_regression_entry_structure_identical(self) -> None:
        added = self._sync("BOKU-MARTIN")
        self.assertEqual(1, added)

        entry = self._read_single_entry()
        self.assertEqual(set(HEAD_ENTRY_KEYS), set(entry))
        self.assertIsInstance(entry["updated_at"], str)
        self.assertTrue(entry["updated_at"])
        self.assertEqual("BOKU-MARTIN", entry["mailbox"])
        self.assertEqual(
            _expected_entry("BOKU-MARTIN", entry["updated_at"]),
            entry,
            "same-account entries must match the HEAD structure exactly",
        )

    def test_no_mailbox_literal_in_source(self) -> None:
        source_path = MAIL_DESK_ROOT / "scripts" / "core" / "sent_indexer.py"
        source = source_path.read_text(encoding="utf-8")
        # Assert on a bool to keep the failure diff small (the source is long).
        self.assertFalse(
            "BOKU-MARTIN" in source,
            "the hardcoded mailbox literal must be removed from sent_indexer.py",
        )


if __name__ == "__main__":
    unittest.main()
