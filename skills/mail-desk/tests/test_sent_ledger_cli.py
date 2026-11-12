"""Issue #107 CLI envelopes for plan/apply/recover-promotion (offline)."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

import mail_desk_sent_ledger as cli  # noqa: E402
from core import sent_ledger  # noqa: E402


def _inventory_record(mid: str, **over) -> dict:
    base = {
        "schema_version": 1, "message_id": f"<{mid}>", "status": "synthesized",
        "vorgang_ref": "V-1", "evid_id": "E-1", "reason_code": None,
        "recorded_at": "2026-10-01T10:00:00Z",
    }
    base.update(over)
    return base


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws = Path(tmp.name)
        self.dd = self.ws / "data" / "mail-desk"
        self.dd.mkdir(parents=True)
        self.inventory = self.ws / "legacy-inventory.jsonl"
        self.inventory.write_text(json.dumps(_inventory_record("a@example.test")) + "\n", encoding="utf-8")

    def run_cli(self, *args: str) -> tuple[int, dict]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(list(args))
        return code, json.loads(out.getvalue())

    def test_plan_returns_the_approval_token_in_a_success_envelope(self) -> None:
        code, envelope = self.run_cli(
            "plan", "--workspace", str(self.ws), "--inventory", str(self.inventory),
        )
        self.assertEqual(0, code)
        self.assertTrue(envelope["success"])
        self.assertEqual("plan", envelope["action"])
        self.assertTrue(envelope["data"]["dry_run"])
        self.assertTrue(envelope["data"]["plan_id"].startswith("sha256:"))
        self.assertNotIn(str(self.ws), json.dumps(envelope))
        self.assertNotIn(str(self.dd), json.dumps(envelope))
        self.assertNotIn(str(self.inventory), json.dumps(envelope))

    def test_apply_requires_approval_and_returns_a_blocked_envelope(self) -> None:
        _, plan = self.run_cli(
            "plan", "--workspace", str(self.ws), "--inventory", str(self.inventory),
        )
        token = plan["data"]["plan_id"]
        with patch.object(sent_ledger, "verify_workspace_lock", return_value=object()):
            code, envelope = self.run_cli(
                "apply", "--workspace", str(self.ws), "--inventory", str(self.inventory),
                "--approval", "sha256:wrong",
            )
        self.assertEqual(2, code)
        self.assertFalse(envelope["success"])
        self.assertEqual("legacy_import_approval_stale", envelope["data"]["reason_code"])

        with patch.object(sent_ledger, "verify_workspace_lock", return_value=object()):
            code, envelope = self.run_cli(
                "apply", "--workspace", str(self.ws), "--inventory", str(self.inventory),
                "--approval", token, "--workspace-lease-id", "LEASE-1",
            )
        self.assertEqual(0, code)
        self.assertTrue(envelope["success"])
        self.assertEqual("applied", envelope["data"]["status"])

    def test_recover_promotion_reads_evidence_array(self) -> None:
        evidence = self.ws / "promotion-evidence.json"
        evidence.write_text(json.dumps([
            {"message_id": "<m@example.test>", "vorgang_ref": "V-1", "evid_id": "E-1"},
        ]), encoding="utf-8")
        with patch.object(sent_ledger, "verify_workspace_lock", return_value=object()):
            code, envelope = self.run_cli(
                "recover-promotion", "--workspace", str(self.ws),
                "--message-id", "<m@example.test>", "--vorgang-ref", "V-1",
                "--evid-id", "E-1", "--evidence-file", str(evidence),
                "--workspace-lease-id", "LEASE-1",
            )
        self.assertEqual(0, code)
        self.assertTrue(envelope["success"])
        self.assertEqual(("recovered", 1), (
            envelope["data"]["status"], envelope["data"]["appended"],
        ))

    def test_relative_data_dir_without_workspace_is_a_structured_failure(self) -> None:
        code, envelope = self.run_cli("plan", "--inventory", str(self.inventory))
        self.assertEqual(1, code)
        self.assertFalse(envelope["success"])
        self.assertIn("relative --data-dir", envelope["error"]["message"])

    def test_apply_rejects_workspace_a_with_data_dir_in_b(self) -> None:
        other_ws = self.ws / "cli-other-workspace"
        other_dd = other_ws / "data" / "mail-desk"
        other_dd.mkdir(parents=True)
        _, plan = self.run_cli(
            "plan", "--workspace", str(other_ws), "--inventory", str(self.inventory),
        )
        with patch.object(sent_ledger, "verify_workspace_lock") as verify:
            code, envelope = self.run_cli(
                "apply", "--workspace", str(self.ws), "--data-dir", str(other_dd),
                "--inventory", str(self.inventory), "--approval", plan["data"]["plan_id"],
                "--workspace-lease-id", "LEASE-1",
            )
        self.assertEqual(2, code)
        self.assertEqual("workspace_binding_invalid", envelope["data"]["reason_code"])
        verify.assert_not_called()
        self.assertFalse((other_dd / "sent-synthesis-ledger.jsonl").exists())

    def test_conflict_plan_exits_two_with_conflict_data(self) -> None:
        (self.dd / "sent-synthesis-ledger.jsonl").write_text(json.dumps({
            "schema_version": 1, "message_id": "a@example.test", "vorgang_ref": "V-9",
            "role": "primary", "anchored_to": None, "recorded_at": "2026-10-01T10:00:00Z",
            "outcome": "synthesized", "source": "synthesis", "evid_id": "E-9",
        }) + "\n", encoding="utf-8")
        code, envelope = self.run_cli(
            "plan", "--workspace", str(self.ws), "--inventory", str(self.inventory),
        )
        self.assertEqual(2, code)
        self.assertTrue(envelope["data"]["blocked"])
        self.assertEqual("ledger_conflict", envelope["data"]["conflicts"][0]["code"])


if __name__ == "__main__":
    unittest.main()
