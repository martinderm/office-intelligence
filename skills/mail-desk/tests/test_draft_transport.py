"""Issue #11 — draft APPEND transport: binary stdin, never raw argv.

Evidence mode ``tdd``, risk tier ``medium``.  This module is written **before**
the production change and is Red against the current argv transport.

The confirmed failure mode (orchestrator, 2026-10-17): a 40,000-character raw
RFC-822 draft passed as one Windows argv literal fails in ``CreateProcess`` with
``WinError 206`` (exit 1).  Independently, Himalaya 1.2.0 (rev
``1b70c4e0eaa72dee48353f0211e6cc0f0776fe98``) ignores the raw ``MESSAGE``
argument whenever stdin is not a TTY and the output is not JSON: public
``src/email/message/command/save.rs`` reads stdin with Rust ``lines()``, joins
the lines with ``\\r\\n`` and drops the final line terminator.  ``run_himalaya``
already starts Himalaya with ``stdin=DEVNULL``, so an argv payload was never
read in plain mode.

Pinned contract:

* **All draft sizes use the stdin pipe** (threshold 0).  There is no size split:
  even a short argv payload is ignored in non-TTY plain mode, and a large one
  additionally trips the Windows command-line limit, so any argv path is
  wrong by construction.
* **Exact canonical SMTPUTF8 bytes.**  The builder emits CRLF lines ending in
  CRLF.  The command receives those exact UTF-8 bytes through
  ``subprocess.run(input=...)`` (binary mode, no ``text=True`` newline
  translation).  One extra transport CRLF compensates the upstream
  ``lines()``/join normalization, so the binary appends the canonical message
  unchanged.
* **Explicit ``-o plain``** keeps the JSON branch from bypassing stdin.
* **Fail loud, never fall back to argv, never retry an APPEND.**  A save
  failure is classified, performs no verification and writes no journal item;
  the save call uses ``max_retries=1``.
* **Backward compatible seam.**  ``run_himalaya`` keeps its published
  ``timeout=35``/``max_retries=5`` defaults and its non-payload path.

The tests drive the real ``core.drafts.save_draft`` and the real
``core.himalaya.run_himalaya``.  Only the configuration/executable resolution
boundary and the ``subprocess.run`` system boundary are faked, plus one real
synthetic Python pipe round-trip.  No live Himalaya mailbox command is issued.
"""

from __future__ import annotations

import base64
import email
import email.policy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from typing import Any
from unittest.mock import patch

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import drafts  # noqa: E402
from core import himalaya  # noqa: E402
from himalaya_fixtures import himalaya_stdin_normalize  # noqa: E402

UTF8_SUBJECT = "Prüfung – Kärnten \U0001F3AF"
UTF8_TO = "Empfänger Öztürk <a@example.org>"
UTF8_BODY = "Grüße aus Wien – 100 % – \U0001F3AF"


def _token_after(tokens: list[str], flag: str) -> str | None:
    """Return the token following ``flag`` in a command, if present."""
    try:
        index = tokens.index(flag)
    except ValueError:
        return None
    if index + 1 < len(tokens):
        return str(tokens[index + 1])
    return None


def _is_save(tokens: list[str]) -> bool:
    return "message" in tokens and "save" in tokens


def _is_envelope_list(tokens: list[str]) -> bool:
    return "envelope" in tokens and "list" in tokens


def _is_read(tokens: list[str]) -> bool:
    return "message" in tokens and "read" in tokens


class _SubprocessBoundary:
    """Fake the ``subprocess.run`` system boundary, not Himalaya semantics.

    It records the exact command and keyword arguments.  On ``message save`` it
    asserts that the raw message arrived as binary ``input`` and then applies
    the documented upstream stdin normalization before answering the
    verification commands from the appended message.
    """

    def __init__(self, *, save_exit: int = 0, save_stderr: str = "") -> None:
        self.save_exit = save_exit
        self.save_stderr = save_stderr
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.saved_payloads: list[bytes] = []
        self.normalized_raw: list[str] = []
        self._message_id = ""
        self._subject = ""

    def __call__(self, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        tokens = list(cmd)
        self.calls.append((tokens, dict(kwargs)))
        if _is_save(tokens):
            return self._handle_save(tokens, kwargs)
        if _is_envelope_list(tokens):
            return self._result(
                tokens,
                kwargs,
                0,
                json.dumps(
                    [
                        {
                            "id": "42",
                            "subject": self._subject,
                            "from": {"name": "", "addr": ""},
                            "date": "",
                        }
                    ]
                    if self._subject
                    else []
                ),
            )
        if _is_read(tokens):
            return self._result(
                tokens,
                kwargs,
                0,
                f"Message-Id: {self._message_id}\nSubject: {self._subject}\n\n",
            )
        raise AssertionError(f"unexpected subprocess invocation: {tokens!r}")

    def _handle_save(self, tokens: list[str], kwargs: dict[str, Any]) -> subprocess.CompletedProcess[Any]:
        payload = kwargs.get("input")
        if payload is None:
            raise AssertionError(
                "message save reached the process boundary without a stdin payload; "
                "the raw draft must never travel as an argv literal"
            )
        if not isinstance(payload, bytes):
            raise AssertionError(f"stdin payload must be bytes, got {type(payload).__name__}")
        if kwargs.get("text"):
            raise AssertionError("the stdin payload must use binary mode, not text=True")
        self.saved_payloads.append(bytes(payload))
        raw = himalaya_stdin_normalize(payload)
        self.normalized_raw.append(raw)
        parsed = email.message_from_string(raw, policy=email.policy.SMTPUTF8)
        self._message_id = str(parsed.get("Message-ID", "") or "")
        self._subject = str(parsed.get("Subject", "") or "")
        if self.save_exit != 0:
            return self._result(tokens, kwargs, self.save_exit, "", self.save_stderr)
        return self._result(tokens, kwargs, 0, "Message 42 successfully saved to Drafts\n")

    @staticmethod
    def _result(
        tokens: list[str],
        kwargs: dict[str, Any],
        code: int,
        stdout: str,
        stderr: str = "",
    ) -> subprocess.CompletedProcess[Any]:
        if kwargs.get("text"):
            return subprocess.CompletedProcess(tokens, code, stdout, stderr)
        return subprocess.CompletedProcess(
            tokens, code, stdout.encode("utf-8"), stderr.encode("utf-8")
        )

    @property
    def save_calls(self) -> list[tuple[list[str], dict[str, Any]]]:
        return [call for call in self.calls if _is_save(call[0])]

    @property
    def verify_calls(self) -> list[tuple[list[str], dict[str, Any]]]:
        return [
            call
            for call in self.calls
            if _is_envelope_list(call[0]) or _is_read(call[0])
        ]


class _TransportHarness(unittest.TestCase):
    """Shared helpers for save-through-the-real-seam transport tests."""

    def _save(
        self,
        body: str,
        *,
        journal: "drafts.DraftJournal | None" = None,
        boundary: "_SubprocessBoundary | None" = None,
    ) -> tuple[_SubprocessBoundary, dict[str, Any]]:
        active_boundary = boundary or _SubprocessBoundary()
        with patch.object(
            himalaya,
            "resolve_himalaya_invocation",
            return_value=("himalaya", Path("C:/fake/himalaya/config.toml")),
        ), patch.object(himalaya.subprocess, "run", active_boundary):
            envelope = drafts.save_draft(
                UTF8_SUBJECT,
                UTF8_TO,
                body,
                folder="Drafts",
                account="demo",
                cc="Kopie Öztürk <cc@example.org>",
                date="Sat, 17 Oct 2026 12:00:00 +0200",
                message_id="<issue11@example.org>",
                journal=journal,
            )
        return active_boundary, envelope

    def _assert_stdin_transport(
        self,
        boundary: _SubprocessBoundary,
        body: str,
        *,
        minimum_payload_bytes: int,
    ) -> None:
        self.assertEqual(1, len(boundary.save_calls), "exactly one APPEND attempt")
        save_tokens, save_kwargs = boundary.save_calls[0]
        payload = boundary.saved_payloads[0]

        # Explicit plain output and account/folder flags, but no raw message.
        self.assertEqual("plain", _token_after(save_tokens, "-o"))
        self.assertEqual("demo", _token_after(save_tokens, "-a"))
        self.assertEqual("Drafts", _token_after(save_tokens, "-f"))
        self.assertIn("-c", save_tokens, "the resolved config is threaded")
        self.assertGreaterEqual(len(payload), minimum_payload_bytes)

        decoded_payload = payload.decode("utf-8")
        for token in save_tokens:
            self.assertNotIn(UTF8_BODY, token)
            self.assertNotIn(UTF8_SUBJECT, token)
            self.assertNotIn("MIME-Version", token)
            self.assertLess(len(token), 4096, "no argv token may approach the Windows limit")

        # Binary transport: the exact payload went to ``input`` unchanged.
        self.assertEqual(payload, save_kwargs["input"])
        self.assertFalse(save_kwargs.get("text", False))
        self.assertNotIn("stdin", save_kwargs)

        # Canonical builder bytes, compensated exactly once for upstream lines()/join.
        normalized = boundary.normalized_raw[0]
        self.assertEqual(normalized + "\r\n", decoded_payload)
        self.assertTrue(decoded_payload.endswith("\r\n\r\n"))
        self.assertTrue(normalized.endswith("\r\n"))
        self.assertNotIn("\n", normalized.replace("\r\n", ""), "canonical raw uses CRLF only")
        self.assertNotIn("\r\r\n", decoded_payload, "exactly one compensation CRLF")

        parsed = email.message_from_string(normalized, policy=email.policy.SMTPUTF8)
        expected_body = UTF8_BODY if body == UTF8_BODY else body
        self.assertEqual(UTF8_SUBJECT, str(parsed["Subject"]))
        self.assertEqual(UTF8_TO, str(parsed["To"]))
        parsed_body = parsed.get_body(("plain",)).get_content().rstrip("\r\n").replace("\r\n", "\n")
        self.assertEqual(expected_body.replace("\r\n", "\n"), parsed_body)
        # Non-ASCII header values travel as exact raw UTF-8, never as RFC 2047
        # encoded words; the body content survives whatever transfer encoding
        # the SMTPUTF8 policy selects.
        self.assertIn(UTF8_SUBJECT.encode("utf-8"), payload)
        self.assertNotIn(b"=?", payload, "no RFC 2047 downgrade")

        # Verification ran only after the APPEND.
        self.assertGreaterEqual(len(boundary.verify_calls), 1)


class SaveDraftStdinTransportTests(_TransportHarness):
    """Red now: every draft size travels through the binary stdin pipe."""

    def test_short_utf8_draft_uses_stdin_pipe_and_verifies_after_append(self) -> None:
        boundary, envelope = self._save(UTF8_BODY)
        self._assert_stdin_transport(boundary, UTF8_BODY, minimum_payload_bytes=0)
        self.assertEqual("42", envelope["envelope_id"])
        self.assertEqual("Drafts", envelope["folder"])
        self.assertEqual(UTF8_SUBJECT, envelope["subject"])

    def test_8192_byte_body_boundary_uses_stdin_pipe(self) -> None:
        body = ("ä" * 4096) + "\n\n" + ("x" * 100)  # 8192 UTF-8 bytes for the umlauts
        boundary, _ = self._save(body)
        self._assert_stdin_transport(boundary, body, minimum_payload_bytes=8192)
        self.assertGreaterEqual(len(boundary.saved_payloads[0]), 8192)

    def test_large_unicode_draft_over_40k_bytes_uses_stdin_pipe(self) -> None:
        body = "ä" * 25000  # 50,000 UTF-8 bytes: far beyond the Windows argv limit
        boundary, _ = self._save(body)
        self._assert_stdin_transport(boundary, body, minimum_payload_bytes=40001)
        self.assertGreater(len(boundary.saved_payloads[0]), 40000)

    def test_blank_lines_and_utf8_round_trip_without_normalization_drift(self) -> None:
        body = "Erster Absatz\n\nZweiter Absatz – mit Umlauten\n\n\nEnde"
        boundary, _ = self._save(body)
        self._assert_stdin_transport(boundary, body, minimum_payload_bytes=0)
        parsed = email.message_from_string(
            boundary.normalized_raw[0], policy=email.policy.SMTPUTF8
        )
        self.assertEqual(
            "Erster Absatz\n\nZweiter Absatz – mit Umlauten\n\n\nEnde",
            parsed.get_body(("plain",)).get_content().rstrip("\r\n").replace("\r\n", "\n"),
        )

    def test_save_failure_is_classified_without_verification_journal_or_fallback(self) -> None:
        boundary = _SubprocessBoundary(save_exit=1, save_stderr="synthetic upstream failure")
        with tempfile.TemporaryDirectory() as tmp:
            journal = drafts.DraftJournal(Path(tmp), "issue11-failure")
            with self.assertRaises(drafts.DraftError) as raised:
                self._save(UTF8_BODY, journal=journal, boundary=boundary)
            payload = json.loads(
                (Path(tmp) / drafts.DRAFT_JOURNAL_FILENAME).read_text(encoding="utf-8")
            )
        error = raised.exception
        self.assertEqual(drafts.DRAFT_SAVE_FAILED_REASON, error.reason_code)
        self.assertIsInstance(error.__cause__, himalaya.HimalayaInvocationError)
        self.assertEqual("himalaya_command_failed", error.__cause__.reason_code)
        self.assertEqual(1, len(boundary.save_calls), "an APPEND is never retried")
        self.assertEqual([], boundary.verify_calls, "no verification after a failed APPEND")
        self.assertEqual({}, payload["runs"]["issue11-failure"]["items"])
        self.assertNotIn(UTF8_BODY, str(error), "the failure must not leak the payload")

    def test_transient_save_error_is_not_retried(self) -> None:
        boundary = _SubprocessBoundary(save_exit=1, save_stderr="TLS stream reset")
        with tempfile.TemporaryDirectory() as tmp:
            journal = drafts.DraftJournal(Path(tmp), "issue11-transient")
            with self.assertRaises(drafts.DraftError) as raised:
                self._save(UTF8_BODY, journal=journal, boundary=boundary)
            payload = json.loads(
                (Path(tmp) / drafts.DRAFT_JOURNAL_FILENAME).read_text(encoding="utf-8")
            )
        self.assertEqual(drafts.DRAFT_SAVE_FAILED_REASON, raised.exception.reason_code)
        self.assertEqual("himalaya_transient", raised.exception.__cause__.reason_code)
        self.assertEqual(1, len(boundary.save_calls))
        self.assertEqual([], boundary.verify_calls)
        self.assertEqual({}, payload["runs"]["issue11-transient"]["items"])


class RunHimalayaStdinContractTests(unittest.TestCase):
    """Red now: ``run_himalaya`` must accept binary stdin without text drift."""

    def test_direct_stdin_bytes_are_passed_through_binary_unchanged(self) -> None:
        payload = "A: ä\r\n\r\nleer\r\nEnde\r\n\r\n".encode("utf-8")
        boundary = _SubprocessBoundary()
        with patch.object(
            himalaya,
            "resolve_himalaya_invocation",
            return_value=("himalaya", Path("C:/fake/config.toml")),
        ), patch.object(himalaya.subprocess, "run", boundary):
            output = himalaya.run_himalaya(
                ["-o", "plain", "message", "save", "-f", "Drafts"],
                account="demo",
                max_retries=1,
                stdin_bytes=payload,
            )
        self.assertIn("successfully saved", output)
        (tokens, kwargs), = boundary.save_calls
        self.assertEqual(payload, kwargs["input"])
        self.assertNotIn("text", kwargs)
        for token in tokens:
            self.assertNotIn(payload.decode("utf-8"), token)

    def test_real_synthetic_binary_pipe_round_trip(self) -> None:
        payload = "Äußerung – 100 %\r\n\r\nsecond block\r\n".encode("utf-8")
        script = (
            "import base64, sys; "
            "sys.stdout.write(base64.b64encode(sys.stdin.buffer.read()).decode('ascii'))"
        )
        with patch.object(
            himalaya,
            "build_himalaya_command",
            return_value=[sys.executable, "-c", script],
        ):
            encoded = himalaya.run_himalaya(
                ["-o", "plain", "message", "save", "-f", "Drafts"],
                max_retries=1,
                stdin_bytes=payload,
            )
        self.assertEqual(payload, base64.b64decode(encoded))

    def test_stdin_bytes_requires_an_explicit_plain_output(self) -> None:
        with patch.object(
            himalaya,
            "build_himalaya_command",
            return_value=["himalaya"],
        ):
            for args in (
                ["message", "save", "-f", "Drafts"],
                ["-o", "json", "message", "save", "-f", "Drafts"],
                ["--output", "json", "message", "save", "-f", "Drafts"],
            ):
                with self.subTest(args=args):
                    with self.assertRaises(ValueError):
                        himalaya.run_himalaya(args, stdin_bytes=b"raw")

    def test_invalid_utf8_payload_is_rejected_before_any_resolution_or_execution(self) -> None:
        """Fix round (Issue #11): a non-UTF-8 payload must never reach Himalaya.

        Upstream ``save.rs`` reads stdin with ``lines().map_while(Result::ok)``,
        so an invalid UTF-8 byte would silently truncate the draft.  Strict
        validation must run *before* executable/config resolution and before any
        subprocess, and must not leak the payload.
        """
        payload = b"Subject: ok\r\n\r\nnot-leaked-marker\xff\xfe"
        boundary = _SubprocessBoundary()
        with patch.object(himalaya, "build_himalaya_command") as build, patch.object(
            himalaya.subprocess, "run", boundary
        ):
            build.side_effect = AssertionError(
                "reachable only after the payload passed UTF-8 validation"
            )
            with self.assertRaises(ValueError) as raised:
                himalaya.run_himalaya(
                    ["-o", "plain", "message", "save", "-f", "Drafts"],
                    stdin_bytes=payload,
                )
        build.assert_not_called()
        self.assertEqual([], boundary.calls, "no subprocess may start for an invalid payload")
        self.assertNotIn("Subject: ok", str(raised.exception))
        self.assertNotIn("not-leaked-marker", str(raised.exception))

    def test_stdin_bytes_is_typed_and_non_empty(self) -> None:
        with patch.object(
            himalaya,
            "build_himalaya_command",
            return_value=["himalaya"],
        ):
            for invalid in (b"", bytearray(b"raw"), "raw", 42):
                with self.subTest(value=type(invalid).__name__):
                    with self.assertRaises(ValueError):
                        himalaya.run_himalaya(
                            ["-o", "plain", "message", "save"],
                            stdin_bytes=invalid,  # type: ignore[arg-type]
                        )

    def test_launch_oserror_is_classified_without_payload_leak(self) -> None:
        payload = b"Subject: x\r\n\r\nsecret body\r\n"
        failure = OSError(206, "The filename or extension is too long")
        with patch.object(
            himalaya,
            "resolve_himalaya_invocation",
            return_value=("himalaya", Path("C:/fake/config.toml")),
        ), patch.object(himalaya.subprocess, "run", side_effect=failure):
            with self.assertRaises(himalaya.HimalayaInvocationError) as raised:
                himalaya.run_himalaya(
                    ["-o", "plain", "message", "save"], stdin_bytes=payload
                )
        self.assertEqual("himalaya_launch_failed", raised.exception.reason_code)
        self.assertNotIn("secret body", str(raised.exception))
        message = str(raised.exception).lower()
        self.assertNotIn("no mailbox command ran", message)
        self.assertIn("launch", message)
        self.assertIn("may be unknown", message)
        self.assertIn("not retried", message)

    def test_non_payload_oserror_is_classified_as_unknown_launch(self) -> None:
        """The same unknown-outcome classification covers the DEVNULL/text path."""
        with patch.object(
            himalaya, "build_himalaya_command", return_value=["himalaya"]
        ), patch.object(himalaya.subprocess, "run", side_effect=OSError(2, "spawn failed")):
            with self.assertRaises(himalaya.HimalayaInvocationError) as raised:
                himalaya.run_himalaya(["folder", "list"])
        self.assertEqual("himalaya_launch_failed", raised.exception.reason_code)
        message = str(raised.exception).lower()
        self.assertIn("launch", message)
        self.assertIn("not retried", message)

    def test_broken_pipe_is_classified_without_payload_leak(self) -> None:
        payload = b"Subject: x\r\n\r\nsecret body\r\n"
        with patch.object(
            himalaya,
            "resolve_himalaya_invocation",
            return_value=("himalaya", Path("C:/fake/config.toml")),
        ), patch.object(himalaya.subprocess, "run", side_effect=BrokenPipeError()):
            with self.assertRaises(himalaya.HimalayaInvocationError) as raised:
                himalaya.run_himalaya(
                    ["-o", "plain", "message", "save"], stdin_bytes=payload
                )
        self.assertEqual("himalaya_stdin_failed", raised.exception.reason_code)
        self.assertNotIn("secret body", str(raised.exception))

    def test_non_payload_callers_keep_the_devnull_text_path(self) -> None:
        healthy = subprocess.CompletedProcess(["himalaya"], 0, "[]", "")
        with patch.object(
            himalaya, "build_himalaya_command", return_value=["himalaya"]
        ), patch.object(himalaya.subprocess, "run", return_value=healthy) as run:
            output = himalaya.run_himalaya(["folder", "list"])
        self.assertEqual("[]", output)
        _, kwargs = run.call_args
        self.assertIs(subprocess.DEVNULL, kwargs["stdin"])
        self.assertTrue(kwargs["text"])


class SaveFailureTaxonomyTests(_TransportHarness):
    """Fix round (Issue #11): only the classified invocation error is wrapped.

    A programming ``ValueError`` raised at the ``run_himalaya`` seam must reach
    the caller unchanged; only the fail-loud runtime class
    ``HimalayaInvocationError`` becomes ``draft_save_failed``.
    """

    SAVE_KWARGS = dict(
        subject="Betreff",
        to="a@example.org",
        body="Text",
        folder="Drafts",
        account="demo",
    )

    def test_programming_value_error_is_not_reclassified(self) -> None:
        programming_error = ValueError("programming contract bug")
        with patch.object(himalaya, "run_himalaya", side_effect=programming_error):
            with self.assertRaises(ValueError) as raised:
                drafts.save_draft(**self.SAVE_KWARGS)
        self.assertIs(programming_error, raised.exception)
        self.assertNotIsInstance(raised.exception, drafts.DraftError)

    def test_expected_invocation_error_is_classified(self) -> None:
        invocation_error = himalaya.HimalayaInvocationError(
            "himalaya_command_failed", "Himalaya failed: synthetic"
        )
        with patch.object(himalaya, "run_himalaya", side_effect=invocation_error):
            with self.assertRaises(drafts.DraftError) as raised:
                drafts.save_draft(**self.SAVE_KWARGS)
        self.assertEqual(drafts.DRAFT_SAVE_FAILED_REASON, raised.exception.reason_code)
        self.assertIs(invocation_error, raised.exception.__cause__)


if __name__ == "__main__":
    unittest.main()
