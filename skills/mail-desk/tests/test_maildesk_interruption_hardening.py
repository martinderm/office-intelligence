"""Regression tests for interrupted and misleading mail-desk runs."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import mail_desk_batch_runner as runner  # noqa: E402
import mail_desk_himalaya_client as client  # noqa: E402
from core import himalaya  # noqa: E402
from core.modes import draft, execute, pipeline  # noqa: E402


class SkipKnownTests(unittest.TestCase):
    def test_pipeline_honors_false(self):
        fetch = Mock(return_value=([], 0))
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data" / "mail-desk"
            data.mkdir(parents=True)
            pipeline.run_pipeline_mode(
                {"skip_known": False, "sync_sent": False}, data_dir=data,
                dependencies={"get_unprocessed_emails": fetch},
            )
        self.assertFalse(fetch.call_args.kwargs["skip_known"])

    def test_draft_and_cli_honor_false(self):
        fetch = Mock(return_value=([], 0))
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data" / "mail-desk"
            data.mkdir(parents=True)
            draft.run_draft_mode(
                {"skip_known": False}, data_dir=data,
                dependencies={
                    "BatchProgressTracker": Mock(),
                    "atomic_write_json": Mock(),
                    "draft_manifest": Mock(return_value={"items": []}),
                    "get_unprocessed_emails": fetch,
                    "load_sent_index": Mock(return_value={}),
                },
            )
            args = runner._build_parser().parse_args(["--draft", "1", "--no-skip-known"])
            config = runner._direct_mode_config(args, data)
        self.assertFalse(fetch.call_args.kwargs["skip_known"])
        self.assertFalse(config["skip_known"])


class FailClosedTests(unittest.TestCase):
    def test_sent_sync_failure_stops_before_classify_and_execute(self):
        classify = Mock()
        mutate = Mock()
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data" / "mail-desk"
            data.mkdir(parents=True)
            result = pipeline.run_pipeline_mode(
                {}, data_dir=data,
                dependencies={
                    "get_unprocessed_emails": Mock(return_value=([{"envelope_id": "1"}], 0)),
                    "sync_sent_items": Mock(side_effect=RuntimeError("sent unavailable")),
                    "draft_manifest": classify,
                    "run_execute_mode": mutate,
                },
            )
        self.assertFalse(result["ok"])
        self.assertEqual("sync_sent", result["error"]["phase"])
        classify.assert_not_called()
        mutate.assert_not_called()

    def test_delete_failure_fails_result_and_progress(self):
        tracker = Mock()
        log = Mock()
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data" / "mail-desk"
            data.mkdir(parents=True)
            result = execute.run_execute_mode(
                {"items": [{"envelope_id": "7", "message_id": "delete@example.test",
                            "action": {"type": "delete"}, "decision": {}}]},
                data_dir=data,
                dependencies={
                    "BatchProgressTracker": Mock(return_value=tracker),
                    "append_action_log_entry": log,
                    "auto_resolve_replies_from_sent": Mock(),
                    "load_final_index": Mock(return_value={"items": {}}),
                    "run_himalaya": Mock(side_effect=RuntimeError("delete denied")),
                    "save_final_index_atomic": Mock(),
                },
            )
        self.assertFalse(result["ok"])
        self.assertEqual("fail", result["results"][0]["routing"])
        log.assert_not_called()
        tracker.fail.assert_called_once()
        tracker.complete.assert_not_called()


class HimalayaEdgeTests(unittest.TestCase):
    def test_search_handles_null_sender_name_and_subject(self):
        envelopes = '[{"id":"9","subject":null,"from":{"name":null,"addr":"sender@example.test"},"date":null}]'
        with patch.object(himalaya, "run_himalaya", side_effect=[
            envelopes, "Message-Id: <case@example.test>\n\n"
        ]):
            matches = himalaya.search_mailbox(
                message_ids=["case@example.test"], folders=["INBOX"], threads=1,
            )
        self.assertEqual("sender@example.test", matches[0]["from"])
        self.assertEqual("", matches[0]["subject"])

    def test_empty_read_is_surfaced_as_error(self):
        with patch.object(himalaya, "run_himalaya", return_value=""):
            details = himalaya.get_single_email_details("404", folder="INBOX")
        self.assertIn("returned no message headers", details["error"])
        with patch.object(client, "get_single_email_details", return_value=details):
            with self.assertRaisesRegex(RuntimeError, "returned no message headers"):
                client.op_read_message("404")


if __name__ == "__main__":
    unittest.main()
