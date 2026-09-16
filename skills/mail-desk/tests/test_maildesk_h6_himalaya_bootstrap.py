"""MD-H6 tests for non-interactive Himalaya bootstrap and retry boundaries."""

from __future__ import annotations

import json
import os
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
        expected_config = str(himalaya.normalize_himalaya_config_path(config))
        self.assertEqual(
            ["C:/tools/himalaya.exe", "-c", expected_config, "-o", "json", "folder", "list", "-a", "demo"], cmd
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

    def test_normalize_config_path_upper_and_lowercase_drive_letters(self) -> None:
        self.assertEqual(
            Path(r"\\localhost\C$\Users\dagobert-ai\.config\himalaya\config.toml"),
            himalaya.normalize_himalaya_config_path(r"C:\Users\dagobert-ai\.config\himalaya\config.toml"),
        )
        self.assertEqual(
            Path(r"\\localhost\c$\Users\dagobert-ai\.config\himalaya\config.toml"),
            himalaya.normalize_himalaya_config_path(r"c:\Users\dagobert-ai\.config\himalaya\config.toml"),
        )
        self.assertEqual(
            Path(r"\\localhost\D$\config\himalaya\config.toml"),
            himalaya.normalize_himalaya_config_path("D:/config/himalaya/config.toml"),
        )
        self.assertEqual(
            Path(r"\\localhost\d$\config\himalaya\config.toml"),
            himalaya.normalize_himalaya_config_path("d:/config/himalaya/config.toml"),
        )

    def test_normalize_config_path_backslash_and_forward_slash_variants(self) -> None:
        expected = Path(r"\\localhost\C$\Users\dagobert-ai\.config\himalaya\config.toml")
        # Pure backslashes
        self.assertEqual(
            expected,
            himalaya.normalize_himalaya_config_path(r"C:\Users\dagobert-ai\.config\himalaya\config.toml"),
        )
        # Pure forward slashes
        self.assertEqual(
            expected,
            himalaya.normalize_himalaya_config_path("C:/Users/dagobert-ai/.config/himalaya/config.toml"),
        )
        # Mixed slashes
        self.assertEqual(
            expected,
            himalaya.normalize_himalaya_config_path(r"C:\Users/dagobert-ai\.config/himalaya\config.toml"),
        )

    def test_normalize_config_path_paths_with_spaces(self) -> None:
        self.assertEqual(
            Path(r"\\localhost\C$\Program Files\Himalaya App\config.toml"),
            himalaya.normalize_himalaya_config_path(r"C:\Program Files\Himalaya App\config.toml"),
        )
        self.assertEqual(
            Path(r"\\localhost\d$\My Documents\Mail Config\config.toml"),
            himalaya.normalize_himalaya_config_path("d:/My Documents/Mail Config/config.toml"),
        )

    def test_normalize_config_path_existing_localhost_unc_preserved(self) -> None:
        expected = Path(r"\\localhost\C$\Users\dagobert-ai\.config\himalaya\config.toml")
        self.assertEqual(
            expected,
            himalaya.normalize_himalaya_config_path(r"\\localhost\C$\Users\dagobert-ai\.config\himalaya\config.toml"),
        )
        self.assertEqual(
            expected,
            himalaya.normalize_himalaya_config_path("//localhost/C$/Users/dagobert-ai/.config/himalaya/config.toml"),
        )

    def test_normalize_config_path_other_unc_hosts_preserved(self) -> None:
        self.assertEqual(
            Path(r"\\fileserver\share\himalaya\config.toml"),
            himalaya.normalize_himalaya_config_path(r"\\fileserver\share\himalaya\config.toml"),
        )
        self.assertEqual(
            Path(r"\\backup-nas\mail\config.toml"),
            himalaya.normalize_himalaya_config_path("//backup-nas/mail/config.toml"),
        )

    def test_normalize_config_path_posix_paths_preserved(self) -> None:
        posix_1 = "/home/user/.config/himalaya/config.toml"
        res_1 = himalaya.normalize_himalaya_config_path(posix_1)
        self.assertNotIn("localhost", str(res_1))
        self.assertFalse(str(res_1).startswith(r"\\localhost"))
        self.assertEqual(Path(posix_1), res_1)

        posix_2 = "/etc/himalaya/config.toml"
        res_2 = himalaya.normalize_himalaya_config_path(posix_2)
        self.assertNotIn("localhost", str(res_2))
        self.assertEqual(Path(posix_2), res_2)

    def test_normalize_config_path_relative_paths_remain_invalid(self) -> None:
        # Relative paths should not be transformed to UNC and remain relative
        rel_1 = himalaya.normalize_himalaya_config_path("relative.toml")
        self.assertEqual(Path("relative.toml"), rel_1)
        self.assertFalse(rel_1.is_absolute())

        rel_2 = himalaya.normalize_himalaya_config_path("sub/config.toml")
        self.assertEqual(Path("sub/config.toml"), rel_2)
        self.assertFalse(rel_2.is_absolute())

        # Drive-relative path without slash (e.g. C:relative.toml) must not be transformed
        rel_drive = himalaya.normalize_himalaya_config_path("C:relative.toml")
        self.assertEqual(Path("C:relative.toml"), rel_drive)
        self.assertFalse(rel_drive.is_absolute())

        # Fail-closed in resolve_himalaya_invocation
        with patch.dict("os.environ", {"HIMALAYA_CONFIG": "C:relative.toml"}, clear=False):
            with self.assertRaises(himalaya.HimalayaInvocationError) as raised:
                himalaya.resolve_himalaya_invocation()
            self.assertEqual("himalaya_config_invalid", raised.exception.reason_code)

    def test_normalized_path_used_for_is_file_and_command_construction(self) -> None:
        raw_path = "C:/my/config.toml"
        expected_unc = Path(r"\\localhost\C$\my\config.toml")

        with patch.dict("os.environ", {"HIMALAYA_CONFIG": raw_path}, clear=False), patch.object(
            himalaya.shutil, "which", return_value="C:/tools/himalaya.exe"
        ), patch("pathlib.Path.is_file", autospec=True) as mock_is_file:
            def fake_is_file(path_self: Path) -> bool:
                return str(path_self) == str(expected_unc)

            mock_is_file.side_effect = fake_is_file

            exe, resolved_path = himalaya.resolve_himalaya_invocation()
            self.assertEqual("C:/tools/himalaya.exe", exe)
            self.assertEqual(expected_unc, resolved_path)
            self.assertEqual(str(expected_unc), str(resolved_path))

            cmd = himalaya.build_himalaya_command(["folder", "list"])
            self.assertEqual(["C:/tools/himalaya.exe", "-c", str(expected_unc), "folder", "list"], cmd)
            # Verify -c is followed by a single string token containing the normalized UNC path
            c_index = cmd.index("-c")
            self.assertEqual(str(expected_unc), cmd[c_index + 1])

    def test_fresh_process_windows_regression(self) -> None:
        """Hermetic regression test executing resolve_himalaya_invocation in a separate python process."""
        import os
        with tempfile.TemporaryDirectory() as temporary:
            cfg = Path(temporary) / "config.toml"
            cfg.write_text("[accounts.fresh]\n", encoding="utf-8")
            drive_path = str(cfg)

            sub_code = (
                "import sys, os, json\n"
                f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
                "from core import himalaya\n"
                "from unittest.mock import patch\n"
                "with patch.object(himalaya.shutil, 'which', return_value='C:/tools/himalaya.exe'):\n"
                "    cmd = himalaya.build_himalaya_command(['folder', 'list'])\n"
                "    print(json.dumps(cmd))\n"
            )
            env = os.environ.copy()
            env["HIMALAYA_CONFIG"] = drive_path
            res = subprocess.run(
                [sys.executable, "-c", sub_code],
                capture_output=True,
                text=True,
                env=env,
                check=True,
            )
            cmd = json.loads(res.stdout.strip())
            c_idx = cmd.index("-c")
            config_arg = cmd[c_idx + 1]
            if os.name == "nt" and len(cfg.drive) >= 2 and cfg.drive[1] == ":":
                drive_letter = cfg.drive[0]
                expected_prefix = f"\\\\localhost\\{drive_letter}$\\"
                self.assertTrue(
                    config_arg.startswith(expected_prefix),
                    f"Expected config arg to start with {expected_prefix!r}, got: {config_arg!r}",
                )


if __name__ == "__main__":
    unittest.main()
