"""Issue #67: draft result signals an empty batch (``is_empty``/``inbox_zero``).

A terminal inbox-zero draft is a completed no-op for the SOP: ``total_drafted``
stays ``0``, ``ok`` stays ``True`` and no mailbox or manifest contract changes.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

from core.modes import draft as draft_mode  # noqa: E402
from core.common import atomic_write_json  # noqa: E402


def _item(index: int = 1, folder: str = "INBOX") -> dict:
    return {
        "envelope_id": str(index),
        "message_id": f"msg-{index}@partner.example.org",
        "source_folder": folder,
        "subject": f"Subject {index}",
    }


class _Tracker:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs

    def step(self, *_args, **_kwargs) -> None:
        return None

    def complete(self, *_args, **_kwargs) -> None:
        return None

    def advance_item(self, *_args, **_kwargs) -> None:
        return None


class DraftEmptyInboxTests(unittest.TestCase):
    def _draft(self, *, items: list[dict], folder: str = "INBOX", expected: int = 10) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            output_path = data_dir / "batch-manifest.json"
            return draft_mode.run_draft_mode(
                {
                    "count": expected,
                    "folder": folder,
                    "output_file": str(output_path),
                    "evaluate_attachments": False,
                },
                data_dir=data_dir,
                dependencies={
                    "BatchProgressTracker": _Tracker,
                    "atomic_write_json": atomic_write_json,
                    "draft_manifest": Mock(return_value={"mode": "execute", "items": items}),
                    "get_unprocessed_emails": Mock(return_value=(items, 0)),
                    "load_sent_index": Mock(return_value={}),
                },
            )

    def test_empty_inbox_is_a_completed_noop(self) -> None:
        result = self._draft(items=[])

        self.assertIs(True, result["ok"])
        self.assertEqual("draft", result["mode"])
        self.assertEqual(0, result["total_drafted"])
        self.assertIs(True, result["is_empty"])
        self.assertIs(True, result["inbox_zero"])
        self.assertEqual(0, result["candidate_count"])
        self.assertEqual(0, result["draft"]["candidate_count"])

    def test_non_empty_draft_is_not_empty(self) -> None:
        result = self._draft(items=[_item()])

        self.assertEqual(1, result["total_drafted"])
        self.assertIs(False, result["is_empty"])
        self.assertIs(False, result["inbox_zero"])

    def test_empty_non_inbox_folder_is_not_inbox_zero(self) -> None:
        result = self._draft(items=[], folder="Archive")

        self.assertEqual(0, result["total_drafted"])
        self.assertIs(True, result["is_empty"])
        self.assertIs(False, result["inbox_zero"])

    def test_inbox_zero_is_case_insensitive(self) -> None:
        result = self._draft(items=[], folder="inbox")

        self.assertIs(True, result["inbox_zero"])


if __name__ == "__main__":
    unittest.main()
