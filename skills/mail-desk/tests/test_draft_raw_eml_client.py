"""Raw-EML import (#18) — CLI and JSON-manifest surface of ``mail_desk_himalaya_client``.

Evidence mode ``tdd``.  Matrix rows C1-C4.

Seam note: ``main()`` / ``execute_manifest`` / ``op_save_raw_draft`` are the public operator entry points.
They catch wrong flag wiring, a lease smuggled in through a manifest, a missing ``--eml`` and the
workspace derivation from ``MAIL_DESK_DATA_DIR``.  Mailbox behaviour sits behind the same synthetic
``MailboxBoundary`` as the save suites; the lease is the real shared guard.
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
from raw_eml_fixtures import make_eml  # noqa: E402
from test_draft_attachments_client import ClientHarness  # noqa: E402
from test_draft_attachments_save import BINARY  # noqa: E402

EML_PATH = "data/inbound/draft.eml"


class RawClientHarness(ClientHarness):
    def setUp(self) -> None:
        super().setUp()
        self.eml = make_eml(attachments=[("report.pdf", "application/pdf", BINARY)])
        target = self.root / EML_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.eml)


class CliTests(RawClientHarness):
    def test_save_raw_draft_stores_a_verified_draft(self) -> None:  # C1
        code, output = self.cli("save-raw-draft", "--eml", EML_PATH, "-a", "demo", "--json")
        self.assertEqual(0, code, output)
        payload = json.loads(output)
        self.assertEqual("save_raw_draft", payload["data"]["operation"])
        result = payload["data"]["result"]
        self.assertEqual("content", result["verified"])
        self.assertEqual(1, result["attachment_count"])
        self.assertEqual("raw1@example.org", result["message_id"])
        self.assertEqual(1, len(self.box.save_calls))
        self.assertEqual(self.eml + b"\r\n", self.box.saved_payloads[0])

    def test_the_target_folder_and_explicit_lease_flags_are_wired(self) -> None:  # C1
        with patch.dict(os.environ):
            os.environ.pop("WORKSPACE_LOCK_LEASE_ID", None)
            code, output = self.cli("save-raw-draft", "--eml", EML_PATH, "-a", "demo", "-f", "Sent", "--json")
            self.assertEqual(1, code)
            self.assertIn("workspace lock is missing", output)
            self.assertEqual([], self.box.save_calls)
            code, output = self.cli(
                "save-raw-draft", "--eml", EML_PATH, "-a", "demo", "-f", "Sent",
                "--lease-id", self.LEASE, "--json",
            )
        self.assertEqual(0, code, output)
        self.assertEqual("Sent", json.loads(output)["data"]["result"]["folder"])

    def test_eml_is_required_and_no_composition_flags_exist(self) -> None:  # C2
        with contextlib.redirect_stderr(io.StringIO()):
            for argv in (
                ["save-raw-draft"],
                ["save-raw-draft", "--eml", EML_PATH, "--attach", "data/report.pdf"],
                ["save-raw-draft", "--eml", EML_PATH, "-s", "Subject"],
                ["save-raw-draft", "--eml", EML_PATH, "-t", "a@example.org"],
                ["save-raw-draft", "--eml", EML_PATH, "-b", "body"],
                ["save-raw-draft", "--eml", EML_PATH, "--drop-attachments"],
            ):
                with self.subTest(argv):
                    with self.assertRaises(SystemExit):
                        client.parse_args(argv)

    def test_an_invalid_message_is_reported_with_its_reason_and_no_mailbox_contact(self) -> None:  # C2
        (self.root / "data" / "inbound" / "bad.eml").write_bytes(b"To: a@example.org\r\n\r\ntext\r\n")
        code, output = self.cli("save-raw-draft", "--eml", "data/inbound/bad.eml", "-a", "demo", "--json")
        self.assertEqual(1, code)
        payload = json.loads(output)
        self.assertEqual("RawEmlError", payload["error"]["type"])
        self.assertIn("'message-id' is missing", payload["error"]["details"]["reason"])
        self.assertEqual([], self.box.calls)

    def test_the_existing_draft_commands_are_unchanged(self) -> None:  # C2
        code, output = self.cli("save-draft", "-a", "demo", "-t", "a@example.org", "-s", "Betreff", "-b", "Text", "--json")
        self.assertEqual(0, code, output)
        self.assertNotIn("attachment_count", output)


class ManifestTests(RawClientHarness):
    def run_manifest(self, operations: list[dict]) -> dict:
        manifest = self.root / "data" / "mail-desk" / "op.json"
        manifest.write_text(
            json.dumps({"account": "demo", "delete_input_on_success": False, "operations": operations}),
            encoding="utf-8",
        )
        return client.execute_manifest(manifest)

    def test_manifest_save_raw_draft_stores_a_verified_draft(self) -> None:  # C3
        result = self.run_manifest([{"action": "save_raw_draft", "eml": EML_PATH}])
        self.assertTrue(result["all_succeeded"], result)
        self.assertEqual("content", result["results"][0]["result"]["verified"])
        self.assertEqual(1, len(self.box.save_calls))

    def test_the_action_name_is_normalized_like_the_other_draft_actions(self) -> None:  # C3
        result = self.run_manifest([{"action": "save-raw-draft", "eml": EML_PATH, "folder": "Sent"}])
        self.assertTrue(result["all_succeeded"], result)
        self.assertEqual("Sent", result["results"][0]["result"]["folder"])

    def test_a_lease_inside_the_manifest_is_never_honoured(self) -> None:  # C3
        with patch.dict(os.environ, {"WORKSPACE_LOCK_LEASE_ID": "wrong-lease"}):
            result = self.run_manifest([{"action": "save_raw_draft", "eml": EML_PATH, "lease_id": self.LEASE}])
        self.assertFalse(result["all_succeeded"])
        self.assertEqual([], self.box.save_calls)

    def test_eml_must_be_a_path_string(self) -> None:  # C4
        for bad in (None, 5, ["data/inbound/draft.eml"], {"path": EML_PATH}, ""):
            with self.subTest(bad):
                result = self.run_manifest([{"action": "save_raw_draft", "eml": bad}])
                self.assertFalse(result["all_succeeded"])
                self.assertEqual([], self.box.save_calls)
        result = self.run_manifest([{"action": "save_raw_draft"}])
        self.assertFalse(result["all_succeeded"])

    def test_composition_keys_are_rejected_in_a_raw_operation(self) -> None:  # C4
        for key, value in (("subject", "S"), ("to", "a@example.org"), ("body", "B"), ("attachments", ["data/report.pdf"]),
                           ("cc", "c@example.org")):
            with self.subTest(key):
                result = self.run_manifest([{"action": "save_raw_draft", "eml": EML_PATH, key: value}])
                self.assertFalse(result["all_succeeded"])
                self.assertEqual([], self.box.save_calls)


if __name__ == "__main__":
    unittest.main()
