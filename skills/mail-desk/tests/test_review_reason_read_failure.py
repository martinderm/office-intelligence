"""FR-26/MD-A5 review-reason contract: full-read failure carries a reason.

Hermetic classifier contract. Exercises the real classifier decision paths
against catalog-driven fixtures; no mailbox, network or catalog writes.

Red-Gate history: at the MD-A5-001 dispatch ``_full_read_failure`` set
confidence/review_required/read_escalation but no ``review_reason``; the test
below failed then and pins the machine-readable reason since.
"""

from __future__ import annotations

from unittest.mock import Mock
import sys
from pathlib import Path
import unittest


MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

from core.classifier import _full_read_failure  # noqa: E402


def _preview_item() -> dict:
    return {
        "message_id": "1954756627.1848513.1785929416691@mail.yahoo.com",
        "subject": "Re: Antw: THE CALL DOCUMENT",
        "from": "sender@example.test",
        "decision": {"confidence": "high", "review_required": False},
    }


class FullReadFailureReasonTests(unittest.TestCase):
    def test_full_read_failure_carries_review_reason(self) -> None:
        item = _preview_item()
        preview = _full_read_failure(item, triggers=["ambiguous"], error=TimeoutError("boom"))
        decision = preview.get("decision", {})
        self.assertEqual(
            "read_escalation_failed",
            decision.get("review_reason"),
            "full-read failure must carry a machine-readable review_reason "
            "(FR-26/MD-A5)",
        )

    def test_fail_closed_shape_unchanged(self) -> None:
        item = _preview_item()
        preview = _full_read_failure(item, triggers=["x"], error=RuntimeError("net"))
        decision = preview.get("decision", {})
        self.assertEqual("low", decision.get("confidence"))
        self.assertTrue(decision.get("review_required"))
        self.assertEqual("failed", decision.get("read_escalation", {}).get("status"))
        self.assertEqual(
            {"type": "keep_in_folder", "target_folder": "INBOX"},
            preview.get("action"),
        )


if __name__ == "__main__":
    unittest.main()