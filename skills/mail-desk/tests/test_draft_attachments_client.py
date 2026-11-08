"""Draft attachments (#18) — CLI and JSON-manifest surface of ``mail_desk_himalaya_client``.

Evidence mode ``tdd``.  Matrix rows C1, C2 (and C3 via the environment lease).

Seam note: ``main()`` / ``execute_manifest`` / ``op_*_draft`` are the public operator entry points.  They
catch wrong flag wiring, a lease smuggled in through a manifest, the omission-protection of replace and
the workspace derivation from ``MAIL_DESK_DATA_DIR``.  Mailbox behaviour sits behind the same synthetic
``MailboxBoundary`` as the save / replace suites; the lease is the real shared guard.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mail_desk_himalaya_client as client  # noqa: E402
from core import drafts  # noqa: E402
from core import himalaya  # noqa: E402
from draft_attachment_fixtures import synthetic_old_draft  # noqa: E402
from test_draft_attachments_save import SaveHarness  # noqa: E402

OLD_ID = "<old@example.org>"


class ClientHarness(SaveHarness):
    def setUp(self) -> None:
        super().setUp()
        env = {
            "MAIL_DESK_DATA_DIR": str(self.root / "data" / "mail-desk"),
            "WORKSPACE_LOCK_LEASE_ID": self.LEASE,
            "WORKSPACE_LOCK_ROOT": str(self.guard),
        }
        patcher = patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        resolve = patch.object(himalaya, "resolve_himalaya_invocation", return_value=("himalaya", Path("C:/fake/config.toml")))
        run = patch.object(himalaya.subprocess, "run", self.box)
        resolve.start()
        run.start()
        self.addCleanup(resolve.stop)
        self.addCleanup(run.stop)

    def cli(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with patch.object(sys, "argv", ["client", *argv]), contextlib.redirect_stdout(out):
            code = client.main()
        return code, out.getvalue()


class CliTests(ClientHarness):
    def test_save_draft_with_attach_flags_stores_a_verified_draft(self) -> None:  # C1
        code, output = self.cli(
            "save-draft", "-a", "demo", "-t", "a@example.org", "-s", "Betreff", "-b", "Text",
            "--attach", "data/report.pdf", "--attach", "memory/cloud/Notiz Ärger.txt", "--json",
        )
        self.assertEqual(0, code, output)
        payload = json.loads(output)
        result = payload["data"]["result"]
        self.assertEqual(2, result["attachment_count"])
        self.assertEqual("content", result["verified"])
        self.assertEqual(1, len(self.box.save_calls))

    def test_replace_draft_requires_the_old_message_id_with_attachments(self) -> None:  # C1
        old = self.box.seed("Drafts", synthetic_old_draft(OLD_ID, subject="Alt", attachment=b"OLD"))
        code, output = self.cli("replace-draft", old, "-a", "demo", "-t", "a@example.org", "-s", "Neu",
                                "--attach", "data/report.pdf", "--json")
        self.assertEqual(1, code)
        self.assertIn("draft_old_identity_mismatch", output + str(drafts.DRAFT_OLD_IDENTITY_MISMATCH_REASON))
        self.assertEqual([], self.box.save_calls)
        code, output = self.cli("replace-draft", old, "-a", "demo", "-t", "a@example.org", "-s", "Neu",
                                "--attach", "data/report.pdf", "--old-message-id", OLD_ID, "--json")
        self.assertEqual(0, code, output)
        self.assertTrue(json.loads(output)["data"]["result"]["old_deleted"])

    def test_replace_draft_without_a_list_is_refused_for_an_old_draft_with_attachments(self) -> None:  # C1
        old = self.box.seed("Drafts", synthetic_old_draft(OLD_ID, subject="Alt", attachment=b"OLD"))
        code, output = self.cli("replace-draft", old, "-a", "demo", "-t", "a@example.org", "-s", "Neu", "--json")
        self.assertEqual(1, code)
        self.assertIn("silently drop", output)
        self.assertEqual([], self.box.save_calls)

    def test_drop_attachments_flag_removes_them_knowingly(self) -> None:  # C1
        old = self.box.seed("Drafts", synthetic_old_draft(OLD_ID, subject="Alt", attachment=b"OLD"))
        code, output = self.cli("replace-draft", old, "-a", "demo", "-t", "a@example.org", "-s", "Neu",
                                "--drop-attachments", "--old-message-id", OLD_ID, "--json")
        self.assertEqual(0, code, output)
        self.assertEqual(0, json.loads(output)["data"]["result"]["attachment_count"])

    def test_drop_and_attach_exclude_each_other_and_save_has_no_drop_flag(self) -> None:  # C1
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                client.parse_args(["replace-draft", "1", "-t", "a@example.org", "-s", "S", "--drop-attachments",
                                   "--attach", "data/report.pdf"])
            with self.assertRaises(SystemExit):
                client.parse_args(["save-draft", "-t", "a@example.org", "-s", "S", "--drop-attachments"])

    def test_text_only_save_draft_cli_is_unchanged(self) -> None:  # C1
        code, output = self.cli("save-draft", "-a", "demo", "-t", "a@example.org", "-s", "Betreff", "-b", "Text", "--json")
        self.assertEqual(0, code, output)
        self.assertNotIn("attachment_count", output)


class ManifestTests(ClientHarness):
    def run_manifest(self, operations: list[dict], **env_patch: str) -> dict:
        manifest = self.root / "data" / "mail-desk" / "op.json"
        manifest.write_text(json.dumps({"account": "demo", "delete_input_on_success": False, "operations": operations}),
                            encoding="utf-8")
        return client.execute_manifest(manifest)

    def test_manifest_save_draft_accepts_attachments(self) -> None:  # C2
        result = self.run_manifest([{"action": "save_draft", "to": "a@example.org", "subject": "S", "body": "T",
                                     "attachments": ["data/report.pdf", {"path": "memory/cloud/Notiz Ärger.txt",
                                                                         "filename": "Notiz.txt"}]}])
        self.assertTrue(result["all_succeeded"], result)
        self.assertEqual(2, result["results"][0]["result"]["attachment_count"])
        self.assertEqual("Notiz.txt", result["results"][0]["result"]["attachments"][1]["filename"])

    def test_a_lease_inside_the_manifest_is_never_honoured(self) -> None:  # C2
        with patch.dict(os.environ, {"WORKSPACE_LOCK_LEASE_ID": "wrong-lease"}):
            result = self.run_manifest([{"action": "save_draft", "to": "a@example.org", "subject": "S",
                                         "attachments": ["data/report.pdf"], "lease_id": self.LEASE}])
        self.assertFalse(result["all_succeeded"])
        self.assertEqual([], self.box.save_calls)

    def test_manifest_replace_follows_the_omission_rule_and_the_explicit_list(self) -> None:  # C2
        old = self.box.seed("Drafts", synthetic_old_draft(OLD_ID, subject="Alt", attachment=b"OLD"))
        refused = self.run_manifest([{"action": "replace_draft", "envelope_id": old, "to": "a@example.org", "subject": "N"}])
        self.assertFalse(refused["all_succeeded"])
        self.assertIn("silently drop", refused["results"][0]["error"])
        self.assertEqual([], self.box.save_calls)
        done = self.run_manifest([{"action": "replace_draft", "envelope_id": old, "to": "a@example.org", "subject": "N",
                                   "attachments": ["data/report.pdf"], "old_message_id": OLD_ID}])
        self.assertTrue(done["all_succeeded"], done)
        self.assertTrue(done["results"][0]["result"]["old_deleted"])

    def test_manifest_attachments_must_be_a_list(self) -> None:  # C2
        result = self.run_manifest([{"action": "save_draft", "to": "a@example.org", "subject": "S",
                                     "attachments": "data/report.pdf"}])
        self.assertFalse(result["all_succeeded"])
        self.assertEqual([], self.box.save_calls)


if __name__ == "__main__":
    unittest.main()
