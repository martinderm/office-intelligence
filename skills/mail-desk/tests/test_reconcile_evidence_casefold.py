"""Issue #65: reconcile evidence checks are Message-ID case-insensitive.

A physically archived evidence entry may carry the Message-ID in a different
case (Outlook/Exchange emit uppercase hex), so the normalized ID must be matched
against the lowercased evidence text.  An empty ID never matches.
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

from core.modes import reconcile  # noqa: E402

EVIDENCE_RELATIVE = "memory/evidence/projects/test/2026-09.md"
MESSAGE_ID = "casefold.evidence@example.test"


def _write_journal(data_dir: Path, message_id: str) -> None:
    record = {
        "message_id": message_id,
        "envelope_id": "7",
        "source_folder": "INBOX",
        "subject": "Evidence subject",
        "from": "sender@example.test",
        "action": {"type": "copy_as_move", "target_folder": "Projekte/Test"},
        "decision": {},
        "evidence": {"file": EVIDENCE_RELATIVE, "entry": "- archived"},
        "phase": "partial",
    }
    journal = {
        "schema_version": 1,
        "runs": {
            "run-evidence": {
                "run_id": "run-evidence",
                "mode": "execute",
                "status": "interrupted",
                "created_at": "2026-09-23T10:00:00Z",
                "updated_at": "2026-09-23T10:00:00Z",
                "items": {message_id: record},
            }
        },
    }
    (data_dir / "batch-recovery-journal.json").write_text(
        json.dumps(journal), encoding="utf-8",
    )


def _write_local_records(workspace: Path, data_dir: Path, message_id: str) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "final-location-index.json").write_text(
        json.dumps({"schema_version": 1, "items": {message_id: {"final_folder": "Projekte/Test"}}}),
        encoding="utf-8",
    )
    (data_dir / "action-log.jsonl").write_text(
        json.dumps({"message_id": message_id, "action": {"target_folder": "Projekte/Test"}}) + "\n",
        encoding="utf-8",
    )
    evidence_path = workspace / EVIDENCE_RELATIVE
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    # The evidence entry uses a different case than the normalized journal ID.
    evidence_path.write_text(f"- archived {MESSAGE_ID.upper()}\n", encoding="utf-8")


class EvidenceCasefoldTests(unittest.TestCase):
    def _report(self, workspace: Path, data_dir: Path) -> dict:
        return reconcile.run_reconcile_mode(
            {"check_folders": True},
            data_dir=data_dir,
            dependencies={"verify_in_target_folder": Mock(return_value="99")},
        )

    def test_uppercase_evidence_message_id_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            data_dir = workspace / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            _write_local_records(workspace, data_dir, MESSAGE_ID)
            _write_journal(data_dir, MESSAGE_ID)

            report = self._report(workspace, data_dir)

        self.assertEqual("complete", report["results"][0]["recovery_state"])
        self.assertTrue(report["results"][0]["in_evidence"])
        self.assertTrue(report["read_only"])

    def test_missing_evidence_entry_is_not_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            data_dir = workspace / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            _write_local_records(workspace, data_dir, MESSAGE_ID)
            (workspace / EVIDENCE_RELATIVE).write_text("- archived other@example.test\n", encoding="utf-8")
            _write_journal(data_dir, MESSAGE_ID)

            report = self._report(workspace, data_dir)

        self.assertFalse(report["results"][0]["in_evidence"])
        self.assertEqual("needs_local_repair", report["results"][0]["recovery_state"])

    def test_empty_message_id_never_matches_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            data_dir = workspace / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            evidence_path = workspace / EVIDENCE_RELATIVE
            evidence_path.parent.mkdir(parents=True, exist_ok=True)
            evidence_path.write_text("- archived without id\n", encoding="utf-8")
            _write_journal(data_dir, "")

            report = self._report(workspace, data_dir)

        self.assertFalse(report["results"][0]["in_evidence"])


if __name__ == "__main__":
    unittest.main()
