"""MD-H6 tests for non-interactive Himalaya bootstrap and retry boundaries."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from core import himalaya  # noqa: E402
from core.readiness import mailbox_readiness_preflight  # noqa: E402


class HimalayaBootstrapTests(unittest.TestCase):
    def test_explicit_config_builds_shell_free_command_and_preserves_account_placement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.toml"
            config.write_text("[accounts.demo]\\n", encoding="utf-8")
            with patch.dict("os.environ", {"HIMALAYA_CONFIG": str(config)}, clear=False), patch.object(
                himalaya.shutil, "which", return_value="C:/tools/himalaya.exe"
            ):
                cmd = himalaya.build_himalaya_command(["-o", "json", "folder", "list"], "demo")
        self.assertEqual(
            ["C:/tools/himalaya.exe", "-c", str(config), "-o", "json", "folder", "list", "-a", "demo"], cmd
        )

    def test_missing_or_relative_config_fails_before_subprocess_and_never_wizards(self) -> None:
        with patch.dict("os.environ", {"HIMALAYA_CONFIG": "relative.toml"}, clear=False), patch.object(
            himalaya.subprocess, "run"
        ) as run:
            with self.assertRaisesRegex(himalaya.HimalayaInvocationError, "absolute") as raised:
                himalaya.run_himalaya(["folder", "list"])
        self.assertEqual("himalaya_config_invalid", raised.exception.reason_code)
        run.assert_not_called()

        with patch.dict("os.environ", {"HIMALAYA_CONFIG": "C:/missing/config.toml"}, clear=False), patch.object(
            himalaya.subprocess, "run"
        ) as run:
            with self.assertRaises(himalaya.HimalayaInvocationError) as raised:
                himalaya.run_himalaya(["folder", "list"])
        self.assertEqual("himalaya_config_missing", raised.exception.reason_code)
        run.assert_not_called()

    def test_unavailable_executable_fails_before_subprocess(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.toml"
            config.touch()
            with patch.dict("os.environ", {"HIMALAYA_CONFIG": str(config)}, clear=False), patch.object(
                himalaya.shutil, "which", return_value=None
            ), patch.object(himalaya.subprocess, "run") as run:
                with self.assertRaises(himalaya.HimalayaInvocationError) as raised:
                    himalaya.run_himalaya(["folder", "list"])
        self.assertEqual("himalaya_unavailable", raised.exception.reason_code)
        run.assert_not_called()

    def test_non_transient_errors_do_not_retry_but_timeout_and_tls_do(self) -> None:
        failed = subprocess.CompletedProcess([], 1, "", "authentication failed")
        with patch.object(himalaya, "build_himalaya_command", return_value=["himalaya"]), patch.object(
            himalaya.subprocess, "run", return_value=failed
        ) as run, patch.object(himalaya.time, "sleep") as sleep:
            with self.assertRaises(himalaya.HimalayaInvocationError) as raised:
                himalaya.run_himalaya(["folder", "list"], max_retries=5)
        self.assertEqual("himalaya_command_failed", raised.exception.reason_code)
        self.assertEqual(1, run.call_count)
        sleep.assert_not_called()

        timeout = subprocess.TimeoutExpired(["himalaya"], 10)
        with patch.object(himalaya, "build_himalaya_command", return_value=["himalaya"]), patch.object(
            himalaya.subprocess, "run", side_effect=timeout
        ) as run, patch.object(himalaya.time, "sleep") as sleep:
            with self.assertRaises(himalaya.HimalayaInvocationError) as raised:
                himalaya.run_himalaya(["folder", "list"], max_retries=5)
        self.assertEqual("himalaya_timeout", raised.exception.reason_code)
        self.assertIsInstance(raised.exception.__cause__, subprocess.TimeoutExpired)
        self.assertEqual(1, run.call_count)
        sleep.assert_not_called()

        transient = subprocess.CompletedProcess([], 1, "", "TLS stream reset")
        with patch.object(himalaya, "build_himalaya_command", return_value=["himalaya"]), patch.object(
            himalaya.subprocess, "run", side_effect=[transient, subprocess.CompletedProcess([], 0, "[]", "")]
        ) as run, patch.object(himalaya.time, "sleep") as sleep:
            self.assertEqual("[]", himalaya.run_himalaya(["folder", "list"], max_retries=2))
        self.assertEqual(2, run.call_count)
        sleep.assert_called_once_with(2.0)

    def test_readiness_emits_structured_bootstrap_reason(self) -> None:
        invocation = himalaya.HimalayaInvocationError("himalaya_config_missing", "missing")
        result = mailbox_readiness_preflight(
            {"backend": "himalaya", "account": "demo"}, folder="INBOX", run_himalaya_fn=Mock(side_effect=invocation)
        )
        self.assertFalse(result["success"])
        self.assertEqual("HimalayaBootstrap", result["error"]["type"])
        self.assertEqual("himalaya_config_missing", result["error"]["details"]["reason_code"])


if __name__ == "__main__":
    unittest.main()
