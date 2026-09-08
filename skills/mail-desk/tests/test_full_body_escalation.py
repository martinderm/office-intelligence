"""FR-02b two-pass preview/full-body classification contracts."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock


MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

from core import classifier  # noqa: E402
from core import himalaya  # noqa: E402
from core.modes import inspect as inspect_mode  # noqa: E402
from core.modes import pipeline as pipeline_mode  # noqa: E402


class FullBodyEscalationTests(unittest.TestCase):
    @staticmethod
    def project() -> dict:
        return {
            "id": "meshe",
            "kuerzel": "MESHE",
            "title": "MESHE",
            "mailbox_folder": "Projects/MESHE",
            "schema_version": 3,
            "workpackages": [{
                "id": "wp1",
                "title": "Quality",
                "status": "active",
                "tasks": [{"id": "T1.7", "title": "Quality Plan"}],
                "deliverables": [],
            }, {
                "id": "wp2",
                "title": "Focus Group Activities",
                "status": "active",
                "tasks": [{"id": "T2.2", "title": "BOKU Focus Group"}],
                "deliverables": [],
            }],
            "milestones": [],
        }

    def email(self, subject: str, preview: str = "") -> dict:
        return {
            "envelope_id": "42",
            "folder": "INBOX",
            "message_id": "source@example.test",
            "subject": subject,
            "preview": preview,
        }

    def two_pass(self, email: dict, reader: Mock) -> dict:
        return classifier.classify_email_two_pass(
            email,
            projects=[self.project()],
            topics=[],
            sent_lookup={},
            final_index={"items": {}},
            full_reader=reader,
            account="test-account",
        )

    def test_no_full_read_without_a_documented_trigger(self) -> None:
        reader = Mock()
        item = self.two_pass(self.email("MESHE routine update", "Status received."), reader)

        reader.assert_not_called()
        self.assertNotIn("read_escalation", item["decision"])

    def test_draft_substring_without_the_word_does_not_trigger(self) -> None:
        reader = Mock()
        item = self.two_pass(self.email("MESHE drafting notes", "Status received."), reader)

        reader.assert_not_called()
        self.assertNotIn("read_escalation", item["decision"])

    def test_trigger_reads_same_envelope_without_preview_and_reclassifies_full_body(self) -> None:
        reader = Mock(return_value={
            **self.email("MESHE Audit update", "Please review T1.7."),
            "preview": "The complete message concerns T1.7.",
            "error": None,
            "read_level": "full_body",
        })
        item = self.two_pass(self.email("MESHE Audit update"), reader)

        reader.assert_called_once_with(
            "42", "INBOX", "test-account", fallback_envelope=self.email("MESHE Audit update"), full_body=True
        )
        self.assertEqual("T1.7", item["decision"]["task"])
        self.assertEqual("wp1", item["decision"]["workpackage"])
        self.assertEqual(
            {"level": "full_body", "triggers": ["audit"], "status": "completed"},
            item["decision"]["read_escalation"],
        )

    def test_meshe_wp2_focus_group_pilot_context_escalates_with_concrete_triggers(self) -> None:
        reader = Mock(return_value={**self.email("MESHE WP2 BOKU Focus Group"), "preview": "Full body.", "error": None})
        item = self.two_pass(self.email("MESHE WP2 BOKU Focus Group"), reader)

        reader.assert_called_once()
        self.assertEqual(
            ["focus group", "wp2", "t2.2"],
            item["decision"]["read_escalation"]["triggers"],
        )

    def test_visible_request_triggers_full_read_without_using_preview_needs_reply(self) -> None:
        reader = Mock(return_value={**self.email("MESHE routine update"), "preview": "Done.", "error": None})
        item = self.two_pass(self.email("MESHE routine update", "Could you confirm this?"), reader)

        reader.assert_called_once()
        self.assertEqual(["visible_action_or_reply_request"], item["decision"]["read_escalation"]["triggers"])

    def test_unclassified_preview_is_documented_as_insufficient_evidence(self) -> None:
        reader = Mock(return_value={**self.email("Unrelated"), "preview": "No matching context.", "error": None})
        item = classifier.classify_email_two_pass(
            self.email("Unrelated"),
            projects=[], topics=[], sent_lookup={}, final_index={"items": {}}, full_reader=reader,
        )

        reader.assert_called_once()
        self.assertEqual(["insufficient_preview_evidence"], item["decision"]["read_escalation"]["triggers"])

    def test_full_reader_omits_preview_flag_and_preserves_the_complete_body(self) -> None:
        full = "Header ignored\n\nline one\nline two\nline three"
        with unittest.mock.patch.object(himalaya, "run_himalaya", return_value=full) as read:
            result = himalaya.get_single_email_details("42", "INBOX", full_body=True)

        args = read.call_args.args[0]
        self.assertEqual(["message", "read"], args[:2])
        self.assertNotIn("--preview", args)
        self.assertEqual("line one\nline two\nline three", result["preview"])
        self.assertEqual("full_body", result["read_level"])

    def test_full_read_failure_forces_review_and_surfaces_structured_error(self) -> None:
        reader = Mock(side_effect=RuntimeError("mocked full read failure"))
        item = self.two_pass(self.email("MESHE Draft"), reader)

        self.assertEqual("low", item["decision"]["confidence"])
        self.assertTrue(item["decision"]["review_required"])
        self.assertEqual("INBOX", item["action"]["target_folder"])
        escalation = item["decision"]["read_escalation"]
        self.assertEqual("failed", escalation["status"])
        self.assertEqual("RuntimeError", escalation["error"]["type"])
        self.assertIn("mocked full read failure", escalation["error"]["message"])

    def test_draft_manifest_uses_the_same_second_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = root / "memory" / "references" / "projects" / "projects.json"
            catalog.parent.mkdir(parents=True)
            catalog.write_text(json.dumps([self.project()]), encoding="utf-8")
            reader = Mock(return_value={**self.email("MESHE Handbook"), "preview": "T1.7", "error": None})

            manifest = classifier.draft_manifest(
                [self.email("MESHE Handbook")], workspace_root=root,
                sent_lookup={}, final_index={"items": {}}, full_reader=reader,
            )

        reader.assert_called_once()
        self.assertEqual("T1.7", manifest["items"][0]["decision"]["task"])
        self.assertEqual("completed", manifest["items"][0]["decision"]["read_escalation"]["status"])

    def test_draft_manifest_without_reader_remains_preview_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = root / "memory" / "references" / "projects" / "projects.json"
            catalog.parent.mkdir(parents=True)
            catalog.write_text(json.dumps([self.project()]), encoding="utf-8")

            with unittest.mock.patch.object(
                himalaya, "get_single_email_details"
            ) as implicit_reader:
                manifest = classifier.draft_manifest(
                    [self.email("MESHE Handbook")],
                    workspace_root=root,
                    sent_lookup={},
                    final_index={"items": {}},
                )

        implicit_reader.assert_not_called()
        self.assertNotIn("read_escalation", manifest["items"][0]["decision"])

    def test_inspect_propose_and_pipeline_forward_the_full_reader(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            reader = Mock(return_value=self.email("MESHE Audit"))
            draft = Mock(return_value={"items": []})
            inspect_mode.run_inspect_mode(
                {"envelope_ids": ["42"], "propose_manifest": True},
                account="test-account",
                data_dir=data_dir,
                dependencies={
                    "draft_manifest": draft,
                    "get_single_email_details": reader,
                    "get_oldest_envelopes": Mock(),
                    "get_unprocessed_emails": Mock(),
                    "load_final_index": Mock(return_value={"items": {}}),
                    "resolve_final_index_path": Mock(return_value=data_dir / "index.json"),
                },
            )
            self.assertIs(reader, draft.call_args.kwargs["full_reader"])
            self.assertEqual("test-account", draft.call_args.kwargs["account"])

            pipeline_draft = Mock(return_value={"items": []})
            pipeline_mode.run_pipeline_mode(
                {"verify": False, "sync_sent": False},
                account="test-account",
                data_dir=data_dir,
                dependencies={
                    "draft_manifest": pipeline_draft,
                    "get_single_email_details": reader,
                    "get_unprocessed_emails": Mock(return_value=([self.email("MESHE Audit")], 0)),
                    "load_sent_index": Mock(return_value={}),
                    "run_execute_mode": Mock(),
                },
            )

        self.assertIs(reader, pipeline_draft.call_args.kwargs["full_reader"])
        self.assertEqual("test-account", pipeline_draft.call_args.kwargs["account"])


if __name__ == "__main__":
    unittest.main()
