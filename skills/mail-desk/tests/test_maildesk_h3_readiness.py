"""MD-H3 regressions for workspace-bound backend readiness."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import mail_desk_batch_runner as runner  # noqa: E402
from core.readiness import mailbox_readiness_preflight  # noqa: E402
from core import himalaya  # noqa: E402


class MailDeskReadinessTests(unittest.TestCase):
    def make_workspace(self, account: str | None = "primary") -> tuple[tempfile.TemporaryDirectory[str], Path]:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        data_dir = root / "data" / "mail-desk"
        data_dir.mkdir(parents=True)
        agents = root / ".agents"
        agents.mkdir()
        (agents / "mail-desk-backend.json").write_text(
            json.dumps({"schema_version": 1, "backend": "himalaya", "account": account}),
            encoding="utf-8",
        )
        return temporary, data_dir

    @staticmethod
    def item() -> dict:
        return {
            "envelope_id": "42",
            "source_folder": "INBOX",
            "message_id": "msg-42@example.test",
            "action": {"type": "keep_in_folder", "target_folder": "INBOX"},
            "decision": {},
        }

    def test_execute_binds_account_and_runs_bounded_readiness_before_handler(self) -> None:
        temporary, data_dir = self.make_workspace()
        with temporary, patch.object(runner, "run_himalaya", return_value="[]") as mail, patch.object(
            runner, "_run_execute_mode", return_value={"ok": True, "mode": "execute", "results": []},
        ) as execute:
            result = runner.run_execute_mode({"items": [self.item()]}, account="primary", data_dir=data_dir)

        self.assertTrue(result["ok"])
        execute.assert_called_once()
        mail.assert_called_once_with(
            ["-o", "json", "envelope", "list", "-f", "INBOX", "-s", "1"],
            account="primary", timeout=10, max_retries=1,
        )
        self.assertEqual("mailbox_readiness", result["readiness_preflight"]["action"])
        self.assertTrue(result["readiness_preflight"]["success"])

    def test_missing_workspace_config_stops_before_adapter_or_execute(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            with patch.object(runner, "run_himalaya") as mail, patch.object(runner, "_run_execute_mode") as execute:
                result = runner.run_execute_mode({"items": [self.item()]}, data_dir=data_dir)

        self.assertFalse(result["ok"])
        self.assertEqual("WorkspaceConfiguration", result["readiness_preflight"]["error"]["type"])
        mail.assert_not_called()
        execute.assert_not_called()

    def test_wrong_account_stops_before_adapter_or_execute(self) -> None:
        temporary, data_dir = self.make_workspace("configured")
        with temporary, patch.object(runner, "run_himalaya") as mail, patch.object(runner, "_run_execute_mode") as execute:
            result = runner.run_execute_mode({"items": [self.item()]}, account="other", data_dir=data_dir)

        self.assertFalse(result["ok"])
        self.assertEqual("account_mismatch", result["readiness_preflight"]["error"]["details"]["reason_code"])
        mail.assert_not_called()
        execute.assert_not_called()

    def test_timeout_missing_adapter_and_invalid_response_stop_before_execute(self) -> None:
        for failure, error_type in (
            (subprocess.TimeoutExpired(["himalaya"], 10), "Timeout"),
            (FileNotFoundError(), "AdapterUnavailable"),
            ("not-json", "InvalidResponse"),
        ):
            with self.subTest(error_type=error_type):
                temporary, data_dir = self.make_workspace()
                side_effect = failure if isinstance(failure, BaseException) else None
                with temporary, patch.object(
                    runner, "run_himalaya", side_effect=side_effect, return_value=None if side_effect else failure,
                ), patch.object(runner, "_run_execute_mode") as execute:
                    result = runner.run_execute_mode({"items": [self.item()]}, account="primary", data_dir=data_dir)
                self.assertFalse(result["ok"])
                self.assertEqual(error_type, result["readiness_preflight"]["error"]["type"])
                execute.assert_not_called()

    def test_pipeline_readiness_stops_before_harvest_or_mutation(self) -> None:
        temporary, data_dir = self.make_workspace()
        with temporary, patch.object(runner, "run_himalaya", return_value="{}"), patch.object(
            runner, "_run_pipeline_mode",
        ) as pipeline:
            result = runner.run_pipeline_mode({"folder": "INBOX"}, account="primary", data_dir=data_dir)

        self.assertFalse(result["ok"])
        self.assertEqual("InvalidResponse", result["readiness_preflight"]["error"]["type"])
        pipeline.assert_not_called()

    def test_read_only_mailbox_facades_bind_configured_account_and_reject_drift(self) -> None:
        cases = (
            ("inspect", runner.run_inspect_mode, "_run_inspect_mode"),
            ("search", runner.run_search_mode, "_run_search_mode"),
            ("sync_sent", runner.run_sync_sent_mode, "_run_sync_sent_mode"),
            ("verify", runner.run_verify_mode, "_run_verify_mode"),
        )
        for mode, facade, handler_name in cases:
            with self.subTest(mode=mode):
                temporary, data_dir = self.make_workspace("bound")
                with temporary, patch.object(
                    runner, handler_name, return_value={"ok": True, "mode": mode},
                ) as handler:
                    result = facade({}, data_dir=data_dir)
                    self.assertTrue(result["ok"])
                    self.assertEqual("bound", handler.call_args.kwargs["account"])

                temporary, data_dir = self.make_workspace("bound")
                with temporary, patch.object(runner, handler_name) as handler:
                    result = facade({}, account="drift", data_dir=data_dir)
                    self.assertFalse(result["ok"])
                    self.assertEqual(
                        "account_mismatch",
                        result["backend_binding"]["error"]["details"]["reason_code"],
                    )
                    handler.assert_not_called()

    def test_single_attempt_timeout_or_transient_failure_never_sleeps(self) -> None:
        timeout = subprocess.TimeoutExpired(["himalaya"], 10)
        with patch.object(himalaya.subprocess, "run", side_effect=timeout), patch.object(
            himalaya.time, "sleep",
        ) as sleep:
            with self.assertRaises(subprocess.TimeoutExpired):
                himalaya.run_himalaya(["folder", "list"], max_retries=1)
        sleep.assert_not_called()

        transient = subprocess.CompletedProcess([], 1, "", "cannot connect")
        with patch.object(himalaya.subprocess, "run", return_value=transient), patch.object(
            himalaya.time, "sleep",
        ) as sleep:
            with self.assertRaisesRegex(RuntimeError, "transient error"):
                himalaya.run_himalaya(["folder", "list"], max_retries=1)
        sleep.assert_not_called()

    def test_readiness_is_a_canonical_envelope(self) -> None:
        result = mailbox_readiness_preflight(
            {"backend": "himalaya", "account": None, "config_path": "C:/test/.agents/mail-desk-backend.json"},
            folder="INBOX",
            run_himalaya_fn=Mock(return_value="[]"),
        )
        self.assertEqual({"action", "success", "state", "message", "data", "error"}, set(result))
        self.assertTrue(result["success"])

    def test_himalaya_account_flag_placement(self) -> None:
        cases = [
            (["-o", "json", "folder", "list"], ["-o", "json", "folder", "list", "-a", "BOKU-MARTIN"]),
            (["folder", "list", "-o", "json"], ["folder", "list", "-a", "BOKU-MARTIN", "-o", "json"]),
            (["message", "read", "--preview", "-f", "INBOX", "9072"], ["message", "read", "-a", "BOKU-MARTIN", "--preview", "-f", "INBOX", "9072"]),
            (["folder", "create", "Projekte/AG-SMART"], ["folder", "create", "-a", "BOKU-MARTIN", "Projekte/AG-SMART"]),
            (["envelope", "list", "-f", "INBOX", "-s", "1"], ["envelope", "list", "-a", "BOKU-MARTIN", "-f", "INBOX", "-s", "1"]),
            (["envelope", "list", "-a", "EXISTING"], ["envelope", "list", "-a", "EXISTING"]),
        ]
        for input_args, expected_args in cases:
            with self.subTest(input_args=input_args):
                self.assertEqual(expected_args, himalaya._insert_account_arg(input_args, "BOKU-MARTIN"))

        # Verify run_himalaya passes the correctly placed args to subprocess.run
        with patch.object(himalaya.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "ok", "")) as mock_run:
            himalaya.run_himalaya(["-o", "json", "folder", "list"], account="BOKU-MARTIN", max_retries=1)
            mock_run.assert_called_once()
            called_cmd = mock_run.call_args[0][0]
            self.assertEqual(["himalaya", "-o", "json", "folder", "list", "-a", "BOKU-MARTIN"], called_cmd)


if __name__ == "__main__":
    unittest.main()
