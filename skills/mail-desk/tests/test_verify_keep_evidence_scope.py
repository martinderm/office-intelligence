"""FR-26/MD-V1 verify evidence-scope contract tests.

Hermetic verify contract. Exercises the real verify mode against a
temporarily materialized index/action-log; no mailbox, network or catalog
writes outside the temp dir.

Red-Gate history: at the MD-V1-001 dispatch verify required evidence for
every item, so keep_in_folder/unknown items (no evidence home) forced
``all_consistent=False``/``recovery_required`` and emptied the synthesis
handoff whenever a batch contained any keep item (Batch 2026-W40/1: verify
PartialFailure vs reconcile completed). The tests below failed then and pin
the scoped contract since.
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

from core.completion import empty_synthesis_handoff, release_synthesis_handoff  # noqa: E402
from core.modes import verify as verify_mode  # noqa: E402


MOVED_ID = "1954756627.1848513.1785929416691@mail.yahoo.com"
KEEP_ID = "keep.item.20260927@example.test"


def _deps(verify: Mock) -> dict:
    return {"verify_in_target_folder": verify}


def _seed(dd: Path, message_id: str, final_folder: str, envelope_id: str) -> None:
    idx_p = dd / "final-location-index.json"
    index_data = {"items": {}}
    if idx_p.exists():
        index_data = json.loads(idx_p.read_text(encoding="utf-8"))
    index_data.setdefault("items", {})[message_id] = {
        "message_id": message_id,
        "backend": "himalaya",
        "final_folder": final_folder,
        "envelope_id": envelope_id,
        "in_reply_to": "",
        "references": [],
        "subject": "s",
        "from": "sender@example.test",
        "date": "2026-09-27T10:00:00Z",
        "updated_at": "2026-09-27T10:00:00Z",
    }
    idx_p.write_text(json.dumps(index_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log_entry = {
        "timestamp": "2026-09-27T10:00:00Z",
        "envelope_id": envelope_id,
        "message_id": message_id,
        "subject": "s",
        "from": "sender@example.test",
        "action": {"type": "copy_as_move", "source_folder": "INBOX", "target_folder": final_folder, "new_envelope_id": envelope_id},
        "decision": {},
        "notes": "",
    }
    with (dd / "action-log.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")


class KeepEvidenceScopeTests(unittest.TestCase):
    def test_keep_items_do_not_block_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dd = Path(tmp) / "data" / "mail-desk"
            dd.mkdir(parents=True)
            _seed(dd, KEEP_ID, "INBOX", "9001")
            # Real-world condition (Batch 2026-W40/1): the evidence root
            # exists but has no entry for a keep item — that mismatch must
            # not block completion (FR-26/MD-V1).
            (dd.parent.parent / "memory" / "evidence").mkdir(parents=True, exist_ok=True)
            config = {"items": [{"message_id": KEEP_ID, "action": {"type": "keep_in_folder", "target_folder": "INBOX"}}]}
            report = verify_mode.run_verify_mode(
                config,
                data_dir=dd,
                index_path=dd / "final-location-index.json",
                dependencies=_deps(Mock(return_value="9001")),
            )
            self.assertTrue(
                report.get("ok"),
                f"keep-only batch must verify consistent, got {report.get('status')}: {report.get('results')}",
            )
            self.assertFalse(report.get("recovery_required"))
            row = report["results"][0]
            self.assertIsNone(
                row.get("in_evidence"),
                "keep items carry in_evidence=null (not checked, not failed)",
            )

    def test_moved_items_still_require_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dd = Path(tmp) / "data" / "mail-desk"
            dd.mkdir(parents=True)
            _seed(dd, MOVED_ID, "Projekte/In Ausarbeitung/ATAEL", "82")
            # Evidence root exists but has no matching markdown: the moved
            # item is expected to be evidence-gated (in_evidence=False).
            (dd.parent.parent / "memory" / "evidence").mkdir(parents=True, exist_ok=True)
            config = {"items": [{"message_id": MOVED_ID, "final_folder": "Projekte/In Ausarbeitung/ATAEL"}]}
            report = verify_mode.run_verify_mode(
                config,
                data_dir=dd,
                index_path=dd / "final-location-index.json",
                dependencies=_deps(Mock(return_value="82")),
            )
            row = report["results"][0]
            self.assertEqual(
                "Projekte/In Ausarbeitung/ATAEL",
                row.get("indexed_folder"),
                "moved item must keep its index/log checks",
            )
            # Evidence requirement stays for moved items: the report exposes
            # in_evidence=False (fail-closed floor), so completion still gates.
            self.assertIs(row.get("in_evidence"), False)

    def test_handoff_releases_for_batch_with_keep_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dd = Path(tmp) / "data" / "mail-desk"
            dd.mkdir(parents=True)
            _seed(dd, KEEP_ID, "INBOX", "9001")
            (dd.parent.parent / "memory" / "evidence").mkdir(parents=True, exist_ok=True)
            config = {"items": [{"message_id": KEEP_ID, "action": {"type": "keep_in_folder", "target_folder": "INBOX"}}]}
            report = verify_mode.run_verify_mode(
                config,
                data_dir=dd,
                index_path=dd / "final-location-index.json",
                dependencies=_deps(Mock(return_value="9001")),
            )
            candidate = {
                "status": "pending",
                "items": [{"message_id": KEEP_ID, "subject": "s", "decision": {}, "synthesis_targets": []}],
            }
            handoff = release_synthesis_handoff(candidate, report)
            self.assertNotEqual(
                "empty",
                handoff.get("status"),
                "handoff must release when the batch (with keep items) verified consistent",
            )


if __name__ == "__main__":
    unittest.main()
