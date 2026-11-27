"""HV3L-1 live-fix regression tests: v3 config token, stdout error channel, timeout details.

Evidence mode ``tdd`` (Red round), risk tier ``medium``.  Every test here is
hermetic: the ``himalaya`` child is answered at both transport seams
(``subprocess.run`` and the module-level ``_POPEN`` factory), the configuration
is a temporary file and no mailbox, network, credential or configuration
content is touched.

Pinned end behavior (binding spec
``runs/office-intelligence/2026-11-27-himalaya-v3live/spec.md``):

* ``build_himalaya_command`` gives the v3 profile a native ``-c`` token (a native
  override stays native, a local admin-share override is denormalized to the
  native drive path) while v1 keeps today's normalized UNC token
  byte-identically;
* a non-zero v3 child exposes its bounded stdout error object as
  ``himalaya_error_code``/``himalaya_error_message`` plus ``exit_code``; invalid,
  oversized or empty stdout falls back to stderr and raw stdout is never stored;
* ``himalaya_timeout`` carries the configured timeout, the duration and both
  partial-pipe lengths, without any partial content.

Only existing public adapter seams are used, plus two small local transport
subclasses that script one failing or one timing-out child; no production helper
is imported before it exists.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Sequence
import unittest
from unittest.mock import patch

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import himalaya  # noqa: E402
from core.himalaya_profile import V3_PROFILE  # noqa: E402
from himalaya_v3_fixtures import (  # noqa: E402
    V1MailboxTransport,
    V1_VERSION_OUTPUT,
    V3MailboxTransport,
    V3_VERSION_OUTPUT,
    HimalayaTransport,
    is_version_command,
    patched_himalaya,
)

#: The one live v3 error shape: a JSON object on stdout, stderr stays empty.
ACCOUNT_MISSING_STDOUT = json.dumps(
    {
        "code": "account-missing",
        "error": "Account __NO_SUCH_ACCOUNT__ not found",
        "sources": [],
        "backtrace": None,
    }
)


class _ScriptedFailureTransport(HimalayaTransport):
    """Answer the version probe normally and fail one mailbox command verbatim."""

    def __init__(
        self,
        *,
        stdout: str,
        stderr: str,
        exit_code: int = 1,
        version_output: str = V3_VERSION_OUTPUT,
    ) -> None:
        super().__init__(version_output)
        self.failure_stdout = stdout
        self.failure_stderr = stderr
        self.exit_code = exit_code

    def execute(self, argv: Sequence[str], kwargs: dict[str, Any]) -> tuple[int, Any, Any]:
        if is_version_command(argv):
            return super().execute(argv, kwargs)
        tokens = [str(token) for token in argv]
        self.calls.append((tokens, dict(kwargs)))
        if bool(kwargs.get("text")):
            return self.exit_code, self.failure_stdout, self.failure_stderr
        return (
            self.exit_code,
            self.failure_stdout.encode("utf-8"),
            self.failure_stderr.encode("utf-8"),
        )


class _TimeoutTransport(HimalayaTransport):
    """Answer the version probe normally and time out the mailbox command."""

    def __init__(
        self,
        *,
        partial_stdout: str | bytes | None = None,
        partial_stderr: str | bytes | None = None,
        version_output: str = V3_VERSION_OUTPUT,
    ) -> None:
        super().__init__(version_output)
        self.partial_stdout = partial_stdout
        self.partial_stderr = partial_stderr

    def execute(self, argv: Sequence[str], kwargs: dict[str, Any]) -> tuple[int, Any, Any]:
        if is_version_command(argv):
            return super().execute(argv, kwargs)
        raise subprocess.TimeoutExpired(
            [str(token) for token in argv],
            kwargs.get("timeout", 35),
            output=self.partial_stdout,
            stderr=self.partial_stderr,
        )


def _admin_share_spelling(config: Path) -> str:
    """Return the v1 local admin-share spelling of one native Windows path."""
    native = str(config)
    drive, _separator, rest = native.partition(":")
    return "\\\\localhost\\" + drive + "$\\" + rest.lstrip("\\/").replace("/", "\\")


class NativeConfigTokenTests(unittest.TestCase):
    """The ``-c`` token is profile-specific: v3 native, v1 normalized UNC."""

    @unittest.skipUnless(os.name == "nt", "the admin-share/native split is a Windows behavior")
    def test_v3_passes_a_native_config_override_verbatim(self) -> None:
        transport = V3MailboxTransport()
        with patched_himalaya(transport) as (module, config, _executable):
            argv = module.build_himalaya_command(["folder", "list"])
        self.assertEqual("-c", argv[1])
        self.assertEqual(str(config), argv[2])
        self.assertFalse(
            argv[2].startswith("\\\\localhost\\"),
            f"v3 must not receive the v1 admin-share spelling: {argv[2]!r}",
        )

    @unittest.skipUnless(os.name == "nt", "the admin-share/native split is a Windows behavior")
    def test_v3_denormalizes_a_local_admin_share_override_to_the_native_token(self) -> None:
        transport = V3MailboxTransport()
        with patched_himalaya(transport) as (module, config, _executable):
            override = _admin_share_spelling(config)
            with patch.dict(os.environ, {"HIMALAYA_CONFIG": override}, clear=False):
                argv = module.build_himalaya_command(["folder", "list"])
        self.assertNotEqual(
            override,
            argv[2],
            "v3 must not receive the local admin-share spelling it hung on live",
        )
        self.assertEqual(str(config), argv[2])

    def test_v3_leaves_a_real_remote_unc_config_unchanged(self) -> None:
        """A remote UNC path has no native equivalent and must stay verbatim.

        No executable probe or network: both resolution seams are patched, so the
        test only exercises the token selection.
        """
        remote = Path("\\\\fileserver\\share\\config.toml")
        with patch.object(
            himalaya, "resolve_himalaya_invocation", return_value=("himalaya", remote)
        ), patch.object(himalaya, "_profile_for_executable", return_value=V3_PROFILE):
            argv = himalaya.build_himalaya_command(["folder", "list"])
        self.assertEqual("-c", argv[1])
        self.assertEqual(str(remote), argv[2])
        self.assertTrue(argv[2].startswith("\\\\fileserver\\"), argv[2])

    @unittest.skipUnless(os.name == "nt", "the admin-share/native split is a Windows behavior")
    def test_v3_denormalizes_a_case_and_slash_tolerant_admin_share_spelling(self) -> None:
        transport = V3MailboxTransport()
        with patched_himalaya(transport) as (module, config, _executable):
            drive, _separator, rest = str(config).partition(":")
            override = (
                "\\\\LOCALHOST\\"
                + drive.lower()
                + "$/"
                + rest.lstrip("\\/").replace("\\", "/")
            )
            with patch.dict(os.environ, {"HIMALAYA_CONFIG": override}, clear=False):
                argv = module.build_himalaya_command(["folder", "list"])
        self.assertEqual(
            os.path.normcase(str(config)),
            os.path.normcase(argv[2]),
            "a case/slash-tolerant local admin share must map to the native path",
        )
        self.assertNotIn("$", argv[2])

    def test_v1_keeps_the_normalized_unc_token_byte_identically(self) -> None:
        transport = V1MailboxTransport()
        with patched_himalaya(transport) as (module, config, executable):
            expected = str(module.normalize_himalaya_config_path(config))
            argv = module.build_himalaya_command(["folder", "list"])
        if os.name == "nt":
            self.assertTrue(
                expected.startswith("\\\\localhost\\"),
                f"the v1 token must stay the local admin-share form: {expected!r}",
            )
        self.assertEqual([str(executable), "-c", expected, "folder", "list"], argv)


class V3ErrorChannelTests(unittest.TestCase):
    """v3 writes its structured error on stdout; v1 stays stderr-only."""

    def test_v3_stdout_error_object_becomes_bounded_details(self) -> None:
        transport = _ScriptedFailureTransport(stdout=ACCOUNT_MISSING_STDOUT, stderr="")
        with patched_himalaya(transport):
            with self.assertRaises(himalaya.HimalayaInvocationError) as raised:
                himalaya.run_himalaya(["folder", "list"])
        error = raised.exception
        self.assertEqual("himalaya_command_failed", error.reason_code)
        self.assertIn("himalaya_error_code", error.details)
        self.assertIn("himalaya_error_message", error.details)
        self.assertEqual("account-missing", error.details["himalaya_error_code"])
        self.assertIn(
            "Account __NO_SUCH_ACCOUNT__ not found",
            error.details["himalaya_error_message"],
        )
        self.assertEqual(1, error.details["exit_code"])
        self.assertIn("account-missing", str(error))
        self.assertIn("Account __NO_SUCH_ACCOUNT__ not found", str(error))
        serialized = json.dumps(error.details, sort_keys=True, default=str)
        self.assertNotIn("backtrace", serialized, "raw stdout must never reach details")
        self.assertNotIn("sources", serialized, "raw stdout must never reach details")
        self.assertNotIn(ACCOUNT_MISSING_STDOUT.strip(), serialized)

    def test_v3_invalid_json_falls_back_to_stderr(self) -> None:
        transport = _ScriptedFailureTransport(stdout="not a json object", stderr="Error: boom")
        with patched_himalaya(transport):
            with self.assertRaises(himalaya.HimalayaInvocationError) as raised:
                himalaya.run_himalaya(["folder", "list"])
        error = raised.exception
        self.assertEqual("himalaya_command_failed", error.reason_code)
        self.assertEqual("Himalaya failed: Error: boom", str(error))
        self.assertNotIn("himalaya_error_code", error.details)
        self.assertNotIn("himalaya_error_message", error.details)

    def test_v3_empty_error_output_keeps_the_generic_message(self) -> None:
        transport = _ScriptedFailureTransport(stdout="", stderr="")
        with patched_himalaya(transport):
            with self.assertRaises(himalaya.HimalayaInvocationError) as raised:
                himalaya.run_himalaya(["folder", "list"])
        error = raised.exception
        self.assertEqual("himalaya_command_failed", error.reason_code)
        self.assertEqual("Himalaya failed: ", str(error))
        self.assertNotIn("himalaya_error_code", error.details)
        self.assertNotIn("himalaya_error_message", error.details)

    def test_v1_never_parses_stdout_on_a_command_failure(self) -> None:
        transport = _ScriptedFailureTransport(
            stdout=ACCOUNT_MISSING_STDOUT,
            stderr="",
            version_output=V1_VERSION_OUTPUT,
        )
        with patched_himalaya(transport):
            with self.assertRaises(himalaya.HimalayaInvocationError) as raised:
                himalaya.run_himalaya(["folder", "list"])
        error = raised.exception
        self.assertEqual("himalaya_command_failed", error.reason_code)
        self.assertEqual("Himalaya failed: ", str(error))
        self.assertEqual({}, error.details)

    def test_v3_oversized_stdout_error_is_ignored(self) -> None:
        oversized = json.dumps(
            {
                "code": "account-missing",
                "error": "Account __NO_SUCH_ACCOUNT__ not found",
                "padding": "x" * 70_000,
            }
        )
        self.assertGreater(len(oversized), 64 * 1024)
        transport = _ScriptedFailureTransport(stdout=oversized, stderr="Error: payload too large")
        with patched_himalaya(transport):
            with self.assertRaises(himalaya.HimalayaInvocationError) as raised:
                himalaya.run_himalaya(["folder", "list"])
        error = raised.exception
        self.assertEqual("himalaya_command_failed", error.reason_code)
        self.assertEqual("Himalaya failed: Error: payload too large", str(error))
        self.assertNotIn("himalaya_error_code", error.details)
        self.assertNotIn("himalaya_error_message", error.details)

    def test_v3_long_error_message_is_truncated_to_500_characters(self) -> None:
        long_message = "E" * 800
        transport = _ScriptedFailureTransport(
            stdout=json.dumps({"code": "account-missing", "error": long_message}),
            stderr="",
        )
        with patched_himalaya(transport):
            with self.assertRaises(himalaya.HimalayaInvocationError) as raised:
                himalaya.run_himalaya(["folder", "list"])
        error = raised.exception
        self.assertIn("himalaya_error_message", error.details)
        bounded = error.details["himalaya_error_message"]
        self.assertEqual(500, len(bounded))
        self.assertTrue(long_message.startswith(bounded))

    def test_v3_deeply_nested_stdout_falls_back_to_stderr(self) -> None:
        """A pathological nesting depth must not escape as a raw ``RecursionError``."""
        transport = _ScriptedFailureTransport(stdout="[" * 65536, stderr="Error: boom")
        with patched_himalaya(transport):
            with self.assertRaises(himalaya.HimalayaInvocationError) as raised:
                himalaya.run_himalaya(["folder", "list"])
        error = raised.exception
        self.assertEqual("himalaya_command_failed", error.reason_code)
        self.assertEqual("Himalaya failed: Error: boom", str(error))
        self.assertNotIn("himalaya_error_code", error.details)
        self.assertNotIn("himalaya_error_message", error.details)

    def test_v3_details_collapse_whitespace(self) -> None:
        transport = _ScriptedFailureTransport(
            stdout=json.dumps(
                {"code": "  account-missing  ", "error": " Account \n not   found "}
            ),
            stderr="",
        )
        with patched_himalaya(transport):
            with self.assertRaises(himalaya.HimalayaInvocationError) as raised:
                himalaya.run_himalaya(["folder", "list"])
        error = raised.exception
        self.assertEqual("account-missing", error.details["himalaya_error_code"])
        self.assertEqual("Account not found", error.details["himalaya_error_message"])
        self.assertEqual(
            "Himalaya failed: account-missing: Account not found", str(error)
        )

    def test_v3_transient_stdout_error_retries_with_bounded_details(self) -> None:
        transport = _ScriptedFailureTransport(
            stdout=json.dumps(
                {"code": "connection-reset", "error": "connection reset by peer"}
            ),
            stderr="",
        )
        with patched_himalaya(transport), patch.object(himalaya.time, "sleep") as sleep:
            with self.assertRaises(himalaya.HimalayaInvocationError) as raised:
                himalaya.run_himalaya(["folder", "list"], max_retries=2)
        error = raised.exception
        self.assertEqual("himalaya_transient", error.reason_code)
        self.assertEqual(2, len(transport.mailbox_calls), "one retry only")
        sleep.assert_called_once_with(2.0)
        self.assertEqual("connection-reset", error.details["himalaya_error_code"])
        self.assertEqual(
            "connection reset by peer", error.details["himalaya_error_message"]
        )
        self.assertEqual(1, error.details["exit_code"])

    def test_v3_transient_stdout_error_on_the_payload_path_stays_single_attempt(self) -> None:
        transport = _ScriptedFailureTransport(
            stdout=json.dumps(
                {"code": "connection-reset", "error": "connection reset by peer"}
            ),
            stderr="",
        )
        with patched_himalaya(transport), patch.object(himalaya.time, "sleep") as sleep:
            with self.assertRaises(himalaya.HimalayaInvocationError) as raised:
                himalaya.run_himalaya(
                    ["message", "save", "-o", "plain", "-f", "INBOX"],
                    stdin_bytes=b"Subject: draft\r\n\r\nBody\r\n\r\n",
                    stdin_v1_terminator=b"\r\n",
                )
        error = raised.exception
        self.assertEqual("himalaya_transient", error.reason_code)
        self.assertEqual(1, len(transport.mailbox_calls), "a payload APPEND is never retried")
        sleep.assert_not_called()
        self.assertEqual("connection-reset", error.details["himalaya_error_code"])
        self.assertEqual(1, error.details["exit_code"])


class TimeoutDetailTests(unittest.TestCase):
    """``himalaya_timeout`` distinguishes no output from partial output."""

    def test_timeout_reports_timeout_duration_and_partial_lengths(self) -> None:
        transport = _TimeoutTransport(
            partial_stdout="partial-out", partial_stderr="partial-err"
        )
        with patched_himalaya(transport):
            with self.assertRaises(himalaya.HimalayaInvocationError) as raised:
                himalaya.run_himalaya(["folder", "list"], timeout=17)
        error = raised.exception
        self.assertEqual("himalaya_timeout", error.reason_code)
        details = error.details
        self.assertIn("timeout_seconds", details)
        self.assertIn("duration_ms", details)
        self.assertIn("stdout_chars", details)
        self.assertIn("stderr_chars", details)
        self.assertEqual(17, details["timeout_seconds"])
        self.assertIsInstance(details["duration_ms"], int)
        self.assertGreaterEqual(details["duration_ms"], 0)
        self.assertEqual(len("partial-out"), details["stdout_chars"])
        self.assertEqual(len("partial-err"), details["stderr_chars"])
        serialized = json.dumps(details, sort_keys=True, default=str)
        self.assertNotIn("partial-out", serialized, "no partial content in details")
        self.assertNotIn("partial-err", serialized, "no partial content in details")

    def test_timeout_without_partial_pipes_reports_zero_lengths(self) -> None:
        transport = _TimeoutTransport()
        with patched_himalaya(transport):
            with self.assertRaises(himalaya.HimalayaInvocationError) as raised:
                himalaya.run_himalaya(["folder", "list"], timeout=9)
        error = raised.exception
        self.assertEqual("himalaya_timeout", error.reason_code)
        details = error.details
        self.assertIn("timeout_seconds", details)
        self.assertIn("stdout_chars", details)
        self.assertIn("stderr_chars", details)
        self.assertEqual(9, details["timeout_seconds"])
        self.assertEqual(0, details["stdout_chars"])
        self.assertEqual(0, details["stderr_chars"])

    def test_timeout_lengths_use_the_decoded_text_for_byte_pipes(self) -> None:
        transport = _TimeoutTransport(
            partial_stdout="grüße".encode("utf-8"), partial_stderr=b"err"
        )
        with patched_himalaya(transport):
            with self.assertRaises(himalaya.HimalayaInvocationError) as raised:
                himalaya.run_himalaya(["folder", "list"], timeout=11)
        error = raised.exception
        self.assertEqual("himalaya_timeout", error.reason_code)
        details = error.details
        self.assertEqual(11, details["timeout_seconds"])
        self.assertEqual(len("grüße"), details["stdout_chars"], "chars, not UTF-8 bytes")
        self.assertEqual(len("err"), details["stderr_chars"])
        serialized = json.dumps(details, sort_keys=True, default=str)
        self.assertNotIn("grüße", serialized, "no partial content in details")


class ProfileGuardTests(unittest.TestCase):
    """The pre-write guard keeps the patchable seam and validates the raw path."""

    def test_guard_falls_back_to_the_raw_config_path(self) -> None:
        transport = V3MailboxTransport()
        missing = himalaya.HimalayaInvocationError("himalaya_config_missing", "missing")
        with patched_himalaya(transport) as (module, _config, _executable):
            with patch.object(module, "resolve_himalaya_invocation", side_effect=missing):
                profile = module.resolve_himalaya_profile_or_none()
        self.assertIsNotNone(profile, "the raw config path must still resolve the profile")
        self.assertEqual(V3_PROFILE.name, profile.name)
        self.assertTrue(transport.version_calls, "the bounded --version probe must run")


if __name__ == "__main__":  # pragma: no cover - manual invocation only
    unittest.main()
