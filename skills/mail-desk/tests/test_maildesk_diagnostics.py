"""Issue #19 — bounded, opt-in, diagnosis-only diagnostic contract tests.

Evidence mode ``tdd``, risk tier ``high``.  Every test here is written to fail
against the code *before* this round's fix and to pass after it.

Pinned contract (human-authorized for issue #19, restricted):

* Diagnosis only, explicit opt-in, **default off**.  The normal draft, batch,
  dossier, attachment and non-payload Himalaya paths stay untouched; the capture
  is threaded through the existing ``run_himalaya`` entrypoint as an opt-in
  keyword and is ``None`` by default.
* A **separate** reviewed operator receipt (never a ``reviewed`` value inside
  the request manifest) is hash-bound to the exact canonical request including
  workspace id, account, folder and operation.  The account is explicit and
  non-null; no cryptographic-auth claim.
* Every read-only preflight runs **before** the one-shot attempt marker, so a
  failed preflight never burns the attempt.
* Exactly **one** child process at the payload boundary, no folder discovery,
  no verification, no retry, no fallback.  A timeout, cap crossing, reader error
  or unknown exit leaves an unknown APPEND outcome.
* A bounded, fail-closed parser that emits only fixed event kinds plus bounded
  counts; the continuation warning is recognised by its exact pinned byte
  signature.  No auth/header/body/server canary ever reaches evidence, the
  envelope or an exception traceback.
* The child stdout is **not** exposed in the diagnostics result.

Hermetic: only the Himalaya command-resolution boundary and the process OS
boundary are faked; the workspace lock is the **real** shared guard with a real
lease in a temporary consumer workspace.  No live mailbox or network.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import unittest
from typing import Any
from unittest.mock import patch

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import diagnostics as diag  # noqa: E402
from core import himalaya  # noqa: E402
import mail_desk_himalaya_client as client  # noqa: E402

BUNDLE_ROOT = Path(__file__).resolve().parents[3]

CANARY_AUTH = "AUTHENTICATE PLAIN"
CANARY_SASL = "dGVzdDpzZWNyZXQ="
CANARY_HEADER = "Subject: Secret Canary"
CANARY_BODY = "secret body canary"
CANARY_SERVER = "server said NO"


def _ts(level: str, target: str, message: str) -> bytes:
    return f"2026-10-18T10:00:00.123456Z {level} {target}: {message}\n".encode("utf-8")


def _continuation_line() -> bytes:
    return b"2026-10-18T10:00:00Z " + diag.RECTIFIED_CONTINUATION_SIGNATURE + b"\n"


def _canary_probe(exc: BaseException) -> str:
    """The full formatted exception chain, so a suppressed context is still visible."""
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


# ---------------------------------------------------------------------------
# Process OS boundary fakes
# ---------------------------------------------------------------------------

class RecordingPipe:
    """A controllable in-memory pipe used as the fake child's stdin/stdout/stderr."""

    def __init__(
        self,
        data: bytes = b"",
        *,
        block_event: threading.Event | None = None,
        reader_error: bool = False,
        max_write: int | None = None,
    ) -> None:
        self._buf = bytearray(data)
        self._block = block_event
        self._reader_error = reader_error
        self._max_write = max_write
        self.written = bytearray()
        self.closed = False

    def write(self, value: Any) -> int:
        if self.closed:
            raise ValueError("write on closed pipe")
        payload = bytes(value)
        if self._max_write is not None:
            payload = payload[: self._max_write]
        self.written.extend(payload)
        return len(payload)

    def flush(self) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        if self._reader_error:
            raise OSError("synthetic reader error")
        if self._block is not None and not self._buf:
            self._block.wait(timeout=5.0)
        if size is None or size < 0:
            data = bytes(self._buf)
            self._buf.clear()
            return data
        data = bytes(self._buf[:size])
        del self._buf[:size]
        return data

    def close(self) -> None:
        self.closed = True


class FakePopen:
    """Fake of the ``himalaya._POPEN`` process OS boundary."""

    instances: list["FakePopen"] = []

    def __init__(self, cmd: list[str]) -> None:
        self.cmd = list(cmd)
        self.kwargs: dict[str, Any] = {}
        self.stdin = RecordingPipe()
        self.stdout = RecordingPipe()
        self.stderr = RecordingPipe()
        self._exit_code = 0
        self._timeout_once = False
        self.killed = False
        FakePopen.instances.append(self)

    def wait(self, timeout: float | None = None) -> int:
        if self._timeout_once and not self.killed:
            raise subprocess.TimeoutExpired(self.cmd, timeout)
        return -9 if self.killed else self._exit_code

    def kill(self) -> None:
        self.killed = True
        for stream in (self.stdin, self.stdout, self.stderr):
            if stream._block is not None:
                stream._block.set()


def _popen_factory(**config: Any):
    def factory(cmd: list[str], **kwargs: Any) -> FakePopen:
        process = FakePopen(cmd)
        process.kwargs = dict(kwargs)
        for name in ("stdin", "stdout", "stderr"):
            stream = config.get(name)
            if isinstance(stream, (bytes, bytearray)):
                stream = RecordingPipe(bytes(stream))
            if isinstance(stream, RecordingPipe):
                setattr(process, name, stream)
        process._exit_code = int(config.get("exit_code", 0))
        process._timeout_once = bool(config.get("timeout_once", False))
        return process

    return factory


# ---------------------------------------------------------------------------
# Shared harness: real temp consumer workspace + real shared lock
# ---------------------------------------------------------------------------

def _guard_root() -> Path | None:
    env = os.environ.get(diag.DIAGNOSTIC_WORKSPACE_LOCK_ROOT_ENV, "").strip()
    candidates = [Path(env)] if env else []
    candidates.append(BUNDLE_ROOT.parent / "workspace-lock")
    for candidate in candidates:
        if (candidate / "scripts" / "workspace_lock_guard.py").is_file():
            return candidate
    return None


class _WorkspaceCase(unittest.TestCase):
    def setUp(self) -> None:
        FakePopen.instances = []
        self.guard_root = _guard_root()
        if self.guard_root is None:
            self.skipTest("shared workspace-lock guard unavailable (set the documented env override)")
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name).resolve()
        self.data_dir = self.workspace / "data" / "mail-desk"
        self.data_dir.mkdir(parents=True)
        agents = self.workspace / ".agents"
        agents.mkdir(parents=True)
        (agents / "mail-desk-backend.json").write_text(
            json.dumps({"schema_version": 1, "backend": "himalaya", "account": "demo"}),
            encoding="utf-8",
        )
        self.lease = "diag-lease-1"
        self._guard = diag._load_workspace_lock_guard(self.guard_root)
        acquired = self._guard.acquire_workspace_lock(self.workspace, harness="test", lease_id=self.lease)
        self.assertEqual("Acquired", acquired.state)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def request(self, **overrides: Any) -> dict[str, Any]:
        request = {
            "schema_version": 1,
            "kind": diag.DIAGNOSTIC_REQUEST_KIND,
            "opt_in": True,
            "workspace_id": self.workspace.name,
            "account": "demo",
            "folder": "Drafts",
            "operation": "save_draft",
            "draft": {"to": "a@example.org", "subject": "Betreff", "body": "Text"},
        }
        request.update(overrides)
        return request

    def receipt(self, request: dict[str, Any], **overrides: Any) -> dict[str, Any]:
        receipt = {
            "schema_version": 1,
            "kind": diag.DIAGNOSTIC_RECEIPT_KIND,
            "receipt_id": "rcpt_diag_1",
            "request_hash": diag.canonical_request_sha256(request),
            "approved_at": "2026-10-18T10:00:00+00:00",
            "approved_by": "operator",
        }
        receipt.update(overrides)
        return receipt

    def run_case(self, *, request: dict[str, Any] | None = None, receipt: dict[str, Any] | None = None,
                 popen: Any = None, lease_id: str | None = "lease", resolve: Any = None, **overrides: Any) -> dict[str, Any]:
        req = request or self.request()
        fields: dict[str, Any] = {
            "receipt": receipt or self.receipt(req),
            "data_dir": self.data_dir,
            "opt_in": True,
            "lock_root": self.guard_root,
            "lease_id": self.lease if lease_id == "lease" else lease_id,
        }
        fields.update(overrides)
        factory = popen or _popen_factory(exit_code=0)
        resolve_patch = (
            patch.object(himalaya, "resolve_himalaya_invocation", side_effect=resolve)
            if resolve is not None
            else patch.object(himalaya, "resolve_himalaya_invocation", return_value=("himalaya", Path("C:/cfg/config.toml")))
        )
        with resolve_patch, patch.object(himalaya, "_POPEN", factory):
            return diag.run_diagnostic_save_draft(req, **fields)

    def evidence(self, request: dict[str, Any]) -> dict[str, Any]:
        request_hash = diag.canonical_request_sha256(request)
        path = self.data_dir / diag.DIAGNOSTIC_DIRNAME / diag.DIAGNOSTIC_EVIDENCE_SUBDIR / f"{request_hash}.json"
        return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Request / receipt contract
# ---------------------------------------------------------------------------

class RequestContractTests(unittest.TestCase):
    def test_non_utf8_request_text_is_classified_before_hashing(self) -> None:
        for field in ("account", "folder", "to", "subject", "body", "cc", "from_addr", "date", "message_id"):
            request = self._valid()
            if field in ("account", "folder"):
                request[field] = "synthetic\ud800"
            else:
                request["draft"][field] = "synthetic\ud800"
            with self.subTest(field=field):
                with self.assertRaises(diag.DiagnosticRequestError) as raised:
                    diag.canonical_request_sha256(request)
                self.assertNotIn("UnicodeEncodeError", _canary_probe(raised.exception))

    def _valid(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": diag.DIAGNOSTIC_REQUEST_KIND,
            "opt_in": True,
            "workspace_id": "demo-workspace",
            "account": "demo",
            "folder": "Drafts",
            "operation": "save_draft",
            "draft": {"to": "a@example.org", "subject": "Betreff", "body": "Text"},
        }

    def test_valid_request_normalizes(self) -> None:
        normalized = diag.validate_diagnostic_request(self._valid())
        self.assertEqual("save_draft", normalized["operation"])
        self.assertEqual("demo", normalized["account"])

    def test_opt_in_false_is_refused(self) -> None:
        with self.assertRaises(diag.DiagnosticRequestError) as raised:
            diag.validate_diagnostic_request(dict(self._valid(), opt_in=False))
        self.assertEqual(diag.DIAGNOSTIC_DISABLED_REASON, raised.exception.reason_code)

    def test_opt_in_must_be_real_boolean(self) -> None:
        for value in (1, "true", None):
            with self.subTest(value=value):
                with self.assertRaises(diag.DiagnosticRequestError):
                    diag.validate_diagnostic_request(dict(self._valid(), opt_in=value))

    def test_unknown_request_key_is_refused_without_echoing_it(self) -> None:
        request = dict(self._valid(), reviewed=True, canary=CANARY_SASL)
        with self.assertRaises(diag.DiagnosticRequestError) as raised:
            diag.validate_diagnostic_request(request)
        blob = _canary_probe(raised.exception)
        for canary in ("reviewed", "canary", CANARY_SASL):
            self.assertNotIn(canary, blob)

    def test_non_save_operation_is_refused(self) -> None:
        for operation in ("replace_draft", "delete_draft", "send", "save_drafts"):
            with self.subTest(operation=operation):
                with self.assertRaises(diag.DiagnosticRequestError):
                    diag.validate_diagnostic_request(dict(self._valid(), operation=operation))

    def test_workspace_id_rejects_path_separators(self) -> None:
        for value in ("C:/ws", "../ws", "a/b", "a\\b", ""):
            with self.subTest(value=value):
                with self.assertRaises(diag.DiagnosticRequestError):
                    diag.validate_diagnostic_request(dict(self._valid(), workspace_id=value))

    def test_account_must_be_explicit_and_nonblank(self) -> None:
        for value in (None, "", "   ", " demo", 1):
            with self.subTest(value=value):
                with self.assertRaises(diag.DiagnosticRequestError):
                    diag.validate_diagnostic_request(dict(self._valid(), account=value))

    def test_header_injection_is_refused(self) -> None:
        for field in ("to", "cc", "from_addr", "subject"):
            with self.subTest(field=field):
                draft = {"to": "a@example.org", "subject": "s", "body": "b", field: "x\r\nBcc: evil@example.org"}
                with self.assertRaises(diag.DiagnosticRequestError):
                    diag.validate_diagnostic_request(dict(self._valid(), draft=draft))


class ReceiptContractTests(unittest.TestCase):
    def test_non_utf8_receipt_text_is_classified(self) -> None:
        request = self._request()
        with self.assertRaises(diag.DiagnosticRequestError):
            diag.validate_diagnostic_receipt(
                self._receipt(request, approved_by="synthetic\ud800"), request=request
            )

    def _request(self) -> dict[str, Any]:
        return {
            "schema_version": 1, "kind": diag.DIAGNOSTIC_REQUEST_KIND, "opt_in": True,
            "workspace_id": "ws", "account": "demo", "folder": "Drafts",
            "operation": "save_draft", "draft": {"to": "a@example.org", "subject": "s", "body": "b"},
        }

    def _receipt(self, request: dict[str, Any], **overrides: Any) -> dict[str, Any]:
        receipt = {
            "schema_version": 1, "kind": diag.DIAGNOSTIC_RECEIPT_KIND, "receipt_id": "rcpt_1",
            "request_hash": diag.canonical_request_sha256(request),
            "approved_at": "2026-10-18T10:00:00+00:00", "approved_by": "operator",
        }
        receipt.update(overrides)
        return receipt

    def test_valid_receipt_accepts(self) -> None:
        request = self._request()
        info = diag.validate_diagnostic_receipt(self._receipt(request), request=request)
        self.assertEqual(diag.canonical_request_sha256(request), info["request_hash"])

    def test_drifted_hash_is_refused(self) -> None:
        with self.assertRaises(diag.DiagnosticApprovalError) as raised:
            diag.validate_diagnostic_receipt(self._receipt(self._request(), request_hash="0" * 64), request=self._request())
        self.assertEqual(diag.DIAGNOSTIC_APPROVAL_DRIFT_REASON, raised.exception.reason_code)

    def test_receipt_unknown_key_is_refused_without_echo(self) -> None:
        request = self._request()
        receipt = self._receipt(request)
        receipt["crypto_signature"] = CANARY_AUTH
        with self.assertRaises(diag.DiagnosticApprovalError) as raised:
            diag.validate_diagnostic_receipt(receipt, request=request)
        self.assertNotIn(CANARY_AUTH, _canary_probe(raised.exception))
        self.assertNotIn("crypto_signature", _canary_probe(raised.exception))

    def test_receipt_missing_key_is_refused(self) -> None:
        request = self._request()
        receipt = self._receipt(request)
        receipt.pop("approved_by")
        with self.assertRaises(diag.DiagnosticApprovalError):
            diag.validate_diagnostic_receipt(receipt, request=request)

    def test_approved_at_must_be_a_real_instant(self) -> None:
        request = self._request()
        for value in ("yesterday", "2026-13-45T99:99:99+00:00", "2026-10-18T10:00:00"):
            with self.subTest(value=value):
                with self.assertRaises(diag.DiagnosticApprovalError):
                    diag.validate_diagnostic_receipt(self._receipt(request, approved_at=value), request=request)

    def test_receipt_id_must_be_restricted_token(self) -> None:
        request = self._request()
        for value in ("bad/id", "bad id", "..", ""):
            with self.subTest(value=value):
                with self.assertRaises(diag.DiagnosticApprovalError):
                    diag.validate_diagnostic_receipt(self._receipt(request, receipt_id=value), request=request)


# ---------------------------------------------------------------------------
# Bounded trace parser
# ---------------------------------------------------------------------------

class TraceParserTests(unittest.TestCase):
    def test_known_stream_events_are_captured(self) -> None:
        chunks = [
            _ts("TRACE", "imap_client::stream", "wrote 42 bytes"),
            _ts("TRACE", "imap_client::stream", "more input needed"),
            _ts("TRACE", "imap_client::stream", "read 1024/4096 bytes"),
        ]
        result = diag.parse_trace_stream(chunks)
        self.assertEqual(
            [diag.EVENT_STREAM_WRITE, diag.EVENT_STREAM_MORE_INPUT, diag.EVENT_STREAM_READ],
            [event["kind"] for event in result["safe_events"]],
        )
        self.assertEqual(42, result["safe_events"][0]["count"])
        self.assertEqual([1, 2, 3], [event["ordinal"] for event in result["safe_events"]])

    def test_continuation_warning_signature_is_recognized_exactly(self) -> None:
        result = diag.parse_trace_stream([_continuation_line()])
        self.assertEqual([diag.EVENT_RECTIFIED_CONTINUATION], [event["kind"] for event in result["safe_events"]])

    def test_continuation_lookalike_is_not_recognized(self) -> None:
        blob = b"2026-10-18T10:00:00Z WARN imap_codec::response: Rectified faulty continuation request " + CANARY_SERVER.encode() + b"\n"
        result = diag.parse_trace_stream([blob])
        self.assertEqual([], result["safe_events"])
        self.assertNotIn(CANARY_SERVER, json.dumps(result))

    def test_unknown_targets_messages_and_levels_are_dropped(self) -> None:
        chunks = [
            _ts("TRACE", "himalaya::tasks", f"enqueue task command={CANARY_AUTH} {CANARY_SASL}"),
            _ts("TRACE", "imap_client::stream", f"wrote 1 bytes {CANARY_SASL}"),
            _ts("INFO", "imap_client::stream", "wrote 5 bytes"),
        ]
        result = diag.parse_trace_stream(chunks)
        self.assertEqual([], result["safe_events"])
        self.assertGreaterEqual(result["dropped_unknown"], 3)
        self.assertNotIn(CANARY_AUTH, json.dumps(result))
        self.assertNotIn(CANARY_SASL, json.dumps(result))

    def test_ansi_span_and_free_form_lines_are_dropped(self) -> None:
        chunk = b"".join(
            [
                b"\x1b[31m2026-10-18T10:00:00Z TRACE imap_client::stream: wrote 1 bytes\x1b[0m\n",
                b"2026-10-18T10:00:00Z TRACE read_bytes: imap_client::stream: wrote 1 bytes\n",
                b"garbage " + CANARY_HEADER.encode() + b"\n",
            ]
        )
        result = diag.parse_trace_stream([chunk])
        self.assertEqual([], result["safe_events"])
        self.assertGreaterEqual(result["dropped_malformed"] + result["dropped_unknown"], 3)

    def test_partial_and_oversize_lines_are_dropped(self) -> None:
        partial = _ts("TRACE", "imap_client::stream", "wrote 1 bytes")[:-1]
        self.assertEqual([], diag.parse_trace_stream([partial])["safe_events"])
        oversize = b"x" * (diag.DIAGNOSTIC_MAX_LINE_BYTES + 50) + b"\n"
        self.assertEqual([], diag.parse_trace_stream([oversize])["safe_events"])

    def test_overlong_line_suffix_is_not_recognized_as_a_line(self) -> None:
        # An over-long line whose tail *looks* like a valid line must be dropped,
        # never re-parsed as a fresh line.
        valid = _ts("TRACE", "imap_client::stream", "wrote 7 bytes")[:-1]
        overlong = b"z" * (diag.DIAGNOSTIC_MAX_LINE_BYTES + 50) + valid + b"\n"
        result = diag.parse_trace_stream([overlong])
        self.assertEqual([], result["safe_events"])
        self.assertGreaterEqual(result["dropped_malformed"], 1)

        # A genuinely new line after the overlong one is still recognized.
        after = overlong + _ts("TRACE", "imap_client::stream", "wrote 8 bytes")
        result2 = diag.parse_trace_stream([after])
        self.assertEqual([diag.EVENT_STREAM_WRITE], [event["kind"] for event in result2["safe_events"]])
        self.assertEqual(8, result2["safe_events"][0]["count"])

    def test_fragmented_chunks_are_reassembled(self) -> None:
        line = _ts("TRACE", "imap_client::stream", "read 8/16 bytes")
        mid = len(line) // 2
        result = diag.parse_trace_stream([line[:mid], line[mid:]])
        self.assertEqual(1, len(result["safe_events"]))

    def test_event_flood_is_capped_only_by_classified_events(self) -> None:
        valid = b"".join(_ts("TRACE", "imap_client::stream", "wrote 1 bytes") for _ in range(500))
        result = diag.parse_trace_stream([valid], max_events=10)
        self.assertEqual(10, len(result["safe_events"]))
        self.assertTrue(result["events_overflow"])

    def test_unknown_messages_do_not_count_as_overflow(self) -> None:
        known = b"".join(_ts("TRACE", "imap_client::stream", "wrote 1 bytes") for _ in range(2))
        unknown = b"".join(_ts("TRACE", "imap_client::stream", "unexpected message") for _ in range(50))
        result = diag.parse_trace_stream([known, unknown], max_events=2)
        self.assertEqual(2, len(result["safe_events"]))
        self.assertFalse(result["events_overflow"])
        self.assertEqual(50, result["dropped_unknown"])

    def test_crlf_lines_are_tolerated(self) -> None:
        result = diag.parse_trace_stream([b"2026-10-18T10:00:00Z TRACE imap_client::stream: wrote 3 bytes\r\n"])
        self.assertEqual(1, len(result["safe_events"]))

    def test_canaries_never_survive(self) -> None:
        chunk = b"".join(
            [
                _ts("TRACE", "imap_client::stream", f"wrote 1 bytes {CANARY_SASL}"),
                _ts("WARN", "imap_codec::response", "Rectified faulty continuation request " + CANARY_AUTH),
                b"| " + CANARY_BODY.encode() + b"\n",
            ]
        )
        blob = json.dumps(diag.parse_trace_stream([chunk]))
        for canary in (CANARY_AUTH, CANARY_SASL, CANARY_BODY):
            self.assertNotIn(canary, blob)


class ChildEnvironmentTests(unittest.TestCase):
    def test_fixed_filter_overrides_manipulated_inherited_logging(self) -> None:
        base = {"RUST_LOG": "tasks=trace,resolver=debug,wire=trace", "RUST_BACKTRACE": "full", "PATH": "/usr/bin"}
        env = diag.build_diagnostic_child_environment(base)
        self.assertEqual(diag.DIAGNOSTIC_TRACING_FILTER, env["RUST_LOG"])
        self.assertEqual("0", env["RUST_BACKTRACE"])
        self.assertEqual("/usr/bin", env["PATH"])

    def test_never_enables_tasks_resolver_or_wire(self) -> None:
        filter_value = diag.DIAGNOSTIC_TRACING_FILTER
        for token in ("tasks", "resolver", "wire", "=debug"):
            self.assertNotIn(token, filter_value)

    def test_os_environ_is_not_mutated(self) -> None:
        before = dict(os.environ)
        with patch.dict(os.environ, {"RUST_LOG": "wire=trace"}):
            diag.build_diagnostic_child_environment()
        self.assertEqual(before, dict(os.environ))


class AttemptMarkerTests(unittest.TestCase):
    def test_consume_once_then_replay_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data" / "mail-desk"
            request = {
                "schema_version": 1, "kind": diag.DIAGNOSTIC_REQUEST_KIND, "opt_in": True,
                "workspace_id": "ws", "account": "demo", "folder": "Drafts",
                "operation": "save_draft", "draft": {"to": "a@example.org", "subject": "s", "body": "b"},
            }
            request_hash = diag.canonical_request_sha256(request)
            diag.consume_attempt_marker(data_dir, request_hash=request_hash, request=request)
            with self.assertRaises(diag.DiagnosticReplayError) as raised:
                diag.consume_attempt_marker(data_dir, request_hash=request_hash, request=request)
            self.assertEqual(diag.DIAGNOSTIC_REPLAY_REASON, raised.exception.reason_code)


# ---------------------------------------------------------------------------
# r/run_himalaya integration (default None path unchanged)
# ---------------------------------------------------------------------------

class RunHimalayaIntegrationTests(unittest.TestCase):
    def test_default_none_keeps_the_non_diagnostic_path(self) -> None:
        healthy = subprocess.CompletedProcess(["himalaya"], 0, "[]", "")
        with patch.object(himalaya, "build_himalaya_command", return_value=["himalaya"]), \
             patch.object(himalaya.subprocess, "run", return_value=healthy) as run, \
             patch.object(himalaya, "_POPEN") as popen:
            output = himalaya.run_himalaya(["folder", "list"])
        self.assertEqual("[]", output)
        self.assertTrue(run.called)
        popen.assert_not_called()

    def test_capture_without_payload_is_rejected(self) -> None:
        with patch.object(himalaya, "build_himalaya_command", return_value=["himalaya"]):
            with self.assertRaises(ValueError):
                himalaya.run_himalaya(["message", "save", "-o", "plain"], diagnostic_capture=object())

    def test_payload_bytes_are_written_and_stdout_is_returned(self) -> None:
        payload = "A: ä\r\n\r\nleer\r\nEnde\r\n\r\n".encode("utf-8")
        stdin = RecordingPipe()
        stdout = RecordingPipe("ok\n".encode())
        with patch.object(himalaya, "build_himalaya_command", return_value=["himalaya", "-a", "demo"]), \
             patch.object(himalaya, "_POPEN", _popen_factory(stdin=stdin, stdout=stdout, exit_code=0)):
            output = himalaya.run_himalaya(
                ["-o", "plain", "message", "save", "-f", "Drafts"], max_retries=1,
                stdin_bytes=payload, diagnostic_capture=diag.DiagnosticTraceCapture(),
            )
        self.assertEqual("ok\n", output)
        self.assertEqual(payload, bytes(stdin.written))

    def test_short_writes_are_completed(self) -> None:
        payload = b"x" * 100
        stdin = RecordingPipe(max_write=7)
        with patch.object(himalaya, "build_himalaya_command", return_value=["himalaya"]), \
             patch.object(himalaya, "_POPEN", _popen_factory(stdin=stdin, exit_code=0)):
            himalaya.run_himalaya(
                ["-o", "plain", "message", "save"], max_retries=1,
                stdin_bytes=payload, diagnostic_capture=diag.DiagnosticTraceCapture(),
            )
        self.assertEqual(payload, bytes(stdin.written))


# ---------------------------------------------------------------------------
# End-to-end orchestration (real lock, real command resolution, fake Popen)
# ---------------------------------------------------------------------------

def _success_stderr() -> bytes:
    return b"".join(
        [
            _ts("TRACE", "imap_client::stream", "wrote 120 bytes"),
            _ts("TRACE", "imap_client::stream", "read 64/64 bytes"),
            _continuation_line(),
            _ts("TRACE", "himalaya::tasks", f"enqueue task command={CANARY_AUTH} {CANARY_SASL}"),
        ]
    )


class OrchestrationTests(_WorkspaceCase):
    def test_linked_diagnostic_subdirectory_is_refused_before_attempt(self) -> None:
        outside = self.workspace / "outside-zone"
        outside.mkdir()
        diagnostic_dir = self.data_dir / "diagnostics"
        diagnostic_dir.mkdir()
        link = diagnostic_dir / "attempts"
        if os.name == "nt":
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                check=True, capture_output=True, timeout=10,
            )
        else:
            link.symlink_to(outside, target_is_directory=True)
        try:
            with self.assertRaises(diag.DiagnosticBindingError):
                self.run_case()
            self.assertEqual([], list(outside.iterdir()))
            self.assertEqual([], FakePopen.instances)
        finally:
            if os.name == "nt":
                os.rmdir(link)
            else:
                link.unlink()

    def test_opt_in_default_off_refuses_before_any_process(self) -> None:
        request = self.request()
        with self.assertRaises(diag.DiagnosticError) as raised:
            self.run_case(request=request, opt_in=False)
        self.assertEqual(diag.DIAGNOSTIC_DISABLED_REASON, raised.exception.reason_code)
        self.assertEqual([], FakePopen.instances)

    def test_success_runs_exactly_one_canonical_process_and_writes_safe_evidence(self) -> None:
        request = self.request()
        result = self.run_case(request=request, popen=_popen_factory(stderr=_success_stderr(), exit_code=0))
        evidence = self.evidence(request)

        self.assertEqual(1, len(FakePopen.instances), "exactly one child process")
        command = FakePopen.instances[0].cmd
        self.assertEqual("demo", command[command.index("-a") + 1])
        self.assertIn("-c", command)
        self.assertIn("save", command)
        self.assertEqual("Drafts", command[command.index("-f") + 1])
        self.assertEqual("plain", command[command.index("-o") + 1])
        for forbidden in ("list", "read", "expunge", "copy", "move", "send"):
            self.assertNotIn(forbidden, command)

        self.assertTrue(result["appended"])
        self.assertFalse(result["verified"])
        self.assertNotIn("child_stdout", result)
        self.assertEqual("appended", evidence["outcome"])
        self.assertEqual({diag.EVENT_STREAM_WRITE, diag.EVENT_STREAM_READ, diag.EVENT_RECTIFIED_CONTINUATION},
                         {event["kind"] for event in evidence["safe_events"]})

    def test_envelope_and_evidence_contain_no_mail_fields_or_canaries(self) -> None:
        request = self.request()
        stdout = RecordingPipe(b"Message 42 successfully saved to Drafts " + CANARY_BODY.encode() + b"\n")
        result = self.run_case(
            request=request,
            popen=_popen_factory(stderr=_success_stderr(), stdout=stdout, exit_code=0),
        )
        evidence = self.evidence(request)
        evidence_blob = json.dumps(evidence)
        combined = json.dumps(result) + evidence_blob
        # Canaries never reach the envelope or the evidence.
        for forbidden in (CANARY_AUTH, CANARY_SASL, CANARY_HEADER, CANARY_BODY, CANARY_SERVER):
            self.assertNotIn(forbidden, combined)
        # Raw child stdout is never exposed.
        self.assertNotIn("child_stdout", combined)
        self.assertNotIn("successfully saved", combined)
        # The evidence carries no mail fields (subject/recipient/body) or paths.
        self.assertNotIn("Betreff", evidence_blob)
        self.assertNotIn("a@example.org", evidence_blob)
        self.assertNotIn("Text", evidence_blob)
        self.assertNotIn(str(self.workspace).replace("\\", "/"), evidence_blob)

    def test_payload_bytes_match_builder_utf8_and_transport_terminator(self) -> None:
        request = self.request()
        self.run_case(request=request, popen=_popen_factory(exit_code=0))
        written = bytes(FakePopen.instances[0].stdin.written)
        self.assertTrue(written.endswith(b"\r\n\r\n"))
        raw = written[:-2].decode("utf-8")
        self.assertIn("Betreff", raw)
        self.assertNotIn("=?", raw)

    def test_marker_is_consumed_before_the_process_starts(self) -> None:
        request = self.request()
        request_hash = diag.canonical_request_sha256(request)
        marker = self.data_dir / diag.DIAGNOSTIC_DIRNAME / diag.DIAGNOSTIC_ATTEMPTS_SUBDIR / f"{request_hash}.json"
        observed: dict[str, bool] = {}

        def factory(cmd: list[str], **kwargs: Any) -> FakePopen:
            observed["marker_exists"] = marker.exists()
            return _popen_factory(exit_code=0)(cmd, **kwargs)

        self.run_case(request=request, popen=factory)
        self.assertTrue(observed.get("marker_exists"))

    def test_failure_is_generic_and_never_leaks_in_the_traceback(self) -> None:
        request = self.request()
        stderr = _ts("ERROR", "imap_client::stream", f"login failed {CANARY_AUTH} {CANARY_SASL}")
        with self.assertRaises(diag.DiagnosticError) as raised:
            self.run_case(request=request, popen=_popen_factory(stderr=stderr, exit_code=1))
        self.assertEqual(diag.DIAGNOSTIC_FAILED_REASON, raised.exception.reason_code)
        blob = _canary_probe(raised.exception)
        for canary in (CANARY_AUTH, CANARY_SASL):
            self.assertNotIn(canary, blob)
        self.assertEqual("failed", self.evidence(request)["outcome"])
        self.assertEqual(1, len(FakePopen.instances))

    def test_timeout_is_unknown_and_never_retried(self) -> None:
        request = self.request()
        with self.assertRaises(diag.DiagnosticError) as raised:
            self.run_case(request=request, popen=_popen_factory(timeout_once=True))
        self.assertEqual(diag.DIAGNOSTIC_UNKNOWN_OUTCOME_REASON, raised.exception.reason_code)
        self.assertEqual(1, len(FakePopen.instances))
        self.assertTrue(FakePopen.instances[0].killed)
        self.assertEqual("unknown", self.evidence(request)["outcome"])

    def test_blocking_reader_does_not_leak_a_thread(self) -> None:
        request = self.request()
        block = threading.Event()
        started = time.monotonic()
        with self.assertRaises(diag.DiagnosticError):
            self.run_case(
                request=request,
                popen=_popen_factory(stdout=RecordingPipe(block_event=block), timeout_once=True),
            )
        self.assertLess(time.monotonic() - started, 20.0, "bounded cleanup must not hang")
        self.assertEqual(1, len(FakePopen.instances))
        self.assertTrue(FakePopen.instances[0].killed)

    def test_reader_error_is_unknown(self) -> None:
        request = self.request()
        stderr = RecordingPipe(reader_error=True)
        self.run_case_expect_unknown(request, stderr=stderr)

    def test_stdout_flood_is_unknown_and_killed(self) -> None:
        request = self.request()
        stdout = RecordingPipe(b"x" * (diag.DIAGNOSTIC_STDOUT_CAP_BYTES + 4096))
        self.run_case_expect_unknown(request, stdout=stdout)

    def test_stderr_flood_is_unknown_and_killed(self) -> None:
        request = self.request()
        flood = b"".join(_ts("TRACE", "imap_client::stream", "wrote 1 bytes") for _ in range(20000))
        self.run_case_expect_unknown(request, stderr=RecordingPipe(flood))

    def run_case_expect_unknown(self, request: dict[str, Any], **streams: Any) -> None:
        with self.assertRaises(diag.DiagnosticError) as raised:
            self.run_case(request=request, popen=_popen_factory(exit_code=0, **streams))
        self.assertEqual(diag.DIAGNOSTIC_UNKNOWN_OUTCOME_REASON, raised.exception.reason_code)
        self.assertEqual(1, len(FakePopen.instances))
        self.assertTrue(FakePopen.instances[0].killed)
        self.assertEqual("unknown", self.evidence(request)["outcome"])

    def test_replay_is_refused_and_drift_gets_a_fresh_attempt(self) -> None:
        request = self.request()
        self.run_case(request=request)
        with self.assertRaises(diag.DiagnosticReplayError):
            self.run_case(request=request)
        self.assertEqual(1, len(FakePopen.instances))

        drifted = self.request(folder="Later")
        self.run_case(request=drifted, receipt=self.receipt(drifted))
        self.assertEqual(2, len(FakePopen.instances))

    def test_effective_account_mismatch_refuses_before_process(self) -> None:
        request = self.request()
        with self.assertRaises(diag.DiagnosticBindingError):
            self.run_case(
                request=request,
                account_binder=lambda dd, *, requested_account: {"ok": True, "account": "other"},
            )
        self.assertEqual([], FakePopen.instances)

    def test_workspace_root_mismatch_refuses_before_process(self) -> None:
        request = self.request()
        with self.assertRaises(diag.DiagnosticBindingError):
            self.run_case(request=request, workspace_root=Path(tempfile.gettempdir()))
        self.assertEqual([], FakePopen.instances)

    def test_workspace_id_must_match_root_name(self) -> None:
        request = self.request(workspace_id="some-other-name")
        with self.assertRaises(diag.DiagnosticBindingError):
            self.run_case(request=request, receipt=self.receipt(request))
        self.assertEqual([], FakePopen.instances)

    def test_rejected_preflight_does_not_burn_the_marker(self) -> None:
        request = self.request()
        request_hash = diag.canonical_request_sha256(request)
        marker = self.data_dir / diag.DIAGNOSTIC_DIRNAME / diag.DIAGNOSTIC_ATTEMPTS_SUBDIR / f"{request_hash}.json"
        with self.assertRaises(diag.DiagnosticError):
            self.run_case(request=request, resolve=RuntimeError("no config"))
        self.assertFalse(marker.exists(), "a failed preflight must not consume the attempt")
        self.assertEqual([], FakePopen.instances)

    def test_lock_env_credentials_are_used(self) -> None:
        request = self.request()
        with patch.dict(os.environ, {"WORKSPACE_LOCK_LEASE_ID": self.lease}, clear=False):
            result = self.run_case(request=request, lease_id=None, popen=_popen_factory(exit_code=0))
        self.assertTrue(result["appended"])

    def test_missing_lock_refuses_before_process(self) -> None:
        request = self.request()
        with self.assertRaises(diag.DiagnosticLockError):
            self.run_case(request=request, lease_id="foreign-lease")
        self.assertEqual([], FakePopen.instances)

    def test_child_receives_the_fixed_filter(self) -> None:
        request = self.request()
        with patch.dict(os.environ, {"RUST_LOG": "tasks=trace,wire=trace"}, clear=False):
            self.run_case(request=request, popen=_popen_factory(exit_code=0))
        env = FakePopen.instances[0].kwargs["env"]
        self.assertEqual(diag.DIAGNOSTIC_TRACING_FILTER, env["RUST_LOG"])


class WorkspaceBindingTests(unittest.TestCase):
    def test_same_name_clone_is_a_documented_non_crypto_residual(self) -> None:
        """Two different absolute roots with the same name both pass the name check."""
        roots = []
        try:
            for _ in range(2):
                tmp = tempfile.TemporaryDirectory()
                root = (Path(tmp.name) / "clone").resolve()
                (root / "data" / "mail-desk").mkdir(parents=True)
                roots.append((tmp, root))
            self.assertNotEqual(roots[0][1], roots[1][1])
            for _tmp, root in roots:
                bound = diag.bind_diagnostic_workspace(root / "data" / "mail-desk", workspace_id="clone")
                self.assertEqual(root, bound)
        finally:
            for tmp, _root in roots:
                tmp.cleanup()

    def test_non_canonical_data_dir_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(diag.DiagnosticBindingError):
                diag.bind_diagnostic_workspace(Path(tmp) / "data" / "other", workspace_id="x")


# ---------------------------------------------------------------------------
# Client CLI surface
# ---------------------------------------------------------------------------

class DiagnoseCliSurfaceTests(_WorkspaceCase):
    def test_parser_exposes_the_diagnose_flags(self) -> None:
        args = client.parse_args(["diagnose-draft", "--request", "r.json", "--review", "v.json", "--opt-in"])
        self.assertEqual("diagnose-draft", args.subcommand)
        self.assertTrue(args.opt_in)

    def test_default_off_and_account_override(self) -> None:
        request = self.request()
        with self.assertRaises(diag.DiagnosticError) as raised:
            client.op_diagnose_draft(request, self.receipt(request), opt_in=False)
        self.assertEqual(diag.DIAGNOSTIC_DISABLED_REASON, raised.exception.reason_code)
        with self.assertRaises(diag.DiagnosticRequestError):
            client.op_diagnose_draft(request, self.receipt(request), opt_in=True, account_override="other", data_dir=self.data_dir)

    def test_op_diagnose_draft_wires_the_contract(self) -> None:
        request = self.request()
        with patch.object(diag, "run_diagnostic_save_draft", return_value={"appended": True}) as run:
            result = client.op_diagnose_draft(request, self.receipt(request), opt_in=True, data_dir=self.data_dir)
        self.assertTrue(result["appended"])
        self.assertTrue(run.call_args.kwargs["opt_in"])


class StuckPipe(RecordingPipe):
    """A reader pipe that never reaches EOF and is not unblocked by ``kill``.

    Models a stuck background reader so the bounded-cleanup path can be pinned.
    """

    def __init__(self) -> None:
        super().__init__()
        self._stop = threading.Event()

    def read(self, size: int = -1) -> bytes:
        while not self._stop.is_set():
            time.sleep(0.02)
        return b""

    def release(self) -> None:
        self._stop.set()


class Round2ParserTests(unittest.TestCase):
    def test_raw_signature_inside_unknown_content_is_never_accepted(self) -> None:
        """The pinned constant inside unknown server content must not emit an event."""
        segment = diag.RECTIFIED_CONTINUATION_SEGMENTS[0].encode("utf-8")
        blob = b"".join(
            [
                b"* NO server text " + diag.RECTIFIED_CONTINUATION_SIGNATURE + b" trailing\n",
                _ts("WARN", "example::server", "echo " + diag.RECTIFIED_CONTINUATION_MESSAGE),
                _ts("WARN", "imap_codec::response", "different " + diag.RECTIFIED_CONTINUATION_SEGMENTS[0]),
                b"2026-10-18T10:00:00Z WARN imap_codec::response: " + segment + b" extra\n",
            ]
        )
        result = diag.parse_trace_stream([blob])
        self.assertEqual([], result["safe_events"])

    def test_exact_three_line_record_is_accepted_across_chunk_boundaries(self) -> None:
        record = b"2026-10-18T10:00:00Z " + diag.RECTIFIED_CONTINUATION_SIGNATURE + b"\n"
        for cut in (5, 30, 60, len(record) - 3):
            with self.subTest(cut=cut):
                result = diag.parse_trace_stream([record[:cut], record[cut:]])
                self.assertEqual(
                    [diag.EVENT_RECTIFIED_CONTINUATION],
                    [event["kind"] for event in result["safe_events"]],
                )

    def test_incomplete_continuation_record_is_not_accepted(self) -> None:
        partial = (
            b"2026-10-18T10:00:00Z WARN imap_codec::response: "
            + diag.RECTIFIED_CONTINUATION_SEGMENTS[0].encode("utf-8")
            + b"\n"
        )
        result = diag.parse_trace_stream([partial])
        self.assertEqual([], result["safe_events"])


class Round2OutcomeTests(unittest.TestCase):
    def test_only_a_real_command_failure_is_a_definitive_failure(self) -> None:
        self.assertEqual(("failed", diag.DIAGNOSTIC_FAILED_REASON), diag._outcome_for_reason("himalaya_command_failed"))
        for reason in ("himalaya_timeout", "himalaya_diagnostic_overflow", "himalaya_outcome_unknown",
                       "himalaya_reader_failed"):
            with self.subTest(reason=reason):
                outcome, mapped = diag._outcome_for_reason(reason)
                self.assertEqual("unknown", outcome)
                self.assertEqual(diag.DIAGNOSTIC_UNKNOWN_OUTCOME_REASON, mapped)
        # An unclassified/bootstrap-TOCTOU reason is unknown transport, not failed.
        for reason in ("himalaya_launch_failed", "himalaya_stdin_failed", "something_unclassified"):
            with self.subTest(reason=reason):
                outcome, mapped = diag._outcome_for_reason(reason)
                self.assertEqual("unknown", outcome)
                self.assertEqual(diag.DIAGNOSTIC_TRANSPORT_REASON, mapped)


class Round2MarkerTests(unittest.TestCase):
    def test_marker_short_writes_are_completed(self) -> None:
        request = {
            "schema_version": 1, "kind": diag.DIAGNOSTIC_REQUEST_KIND, "opt_in": True,
            "workspace_id": "ws", "account": "demo", "folder": "Drafts",
            "operation": "save_draft", "draft": {"to": "a@example.org", "subject": "s", "body": "b"},
        }
        request_hash = diag.canonical_request_sha256(request)
        real_write = os.write
        calls = {"n": 0}

        def short_write(fd: int, data: Any) -> int:
            calls["n"] += 1
            return real_write(fd, bytes(data)[:3])

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data" / "mail-desk"
            with patch.object(diag.os, "write", short_write):
                diag.consume_attempt_marker(data_dir, request_hash=request_hash, request=request)
            marker = data_dir / diag.DIAGNOSTIC_DIRNAME / diag.DIAGNOSTIC_ATTEMPTS_SUBDIR / f"{request_hash}.json"
            payload = json.loads(marker.read_text(encoding="utf-8"))
        self.assertGreater(calls["n"], 1, "the marker write must loop over short writes")
        self.assertEqual("attempt_consumed", payload["phase"])


class Round2PreflightTests(unittest.TestCase):
    def _request(self) -> dict[str, Any]:
        return {
            "schema_version": 1, "kind": diag.DIAGNOSTIC_REQUEST_KIND, "opt_in": True,
            "workspace_id": "ws", "account": "demo", "folder": "Drafts",
            "operation": "save_draft", "draft": {"to": "a@example.org", "subject": "s", "body": "b"},
        }

    def test_body_and_subject_are_bounded(self) -> None:
        with self.assertRaises(diag.DiagnosticRequestError):
            diag.validate_diagnostic_request(
                dict(self._request(), draft={"to": "a@example.org", "subject": "s", "body": "x" * (1_048_576 + 1)})
            )
        with self.assertRaises(diag.DiagnosticRequestError):
            diag.validate_diagnostic_request(
                dict(self._request(), draft={"to": "a@example.org", "subject": "s" * 4097, "body": "b"})
            )


class Round2ClientLoaderTests(unittest.TestCase):
    def _load(self, tmp: str, content: bytes, label: str = "request"):
        path = Path(tmp) / f"{label}.json"
        path.write_bytes(content)
        max_bytes = client.MAX_DIAGNOSTIC_REQUEST_BYTES if label == "request" else client.MAX_DIAGNOSTIC_RECEIPT_BYTES
        return client._load_diagnostic_json(str(path), label=label, max_bytes=max_bytes)

    def test_duplicate_json_keys_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "request.json"
            path.write_bytes(b'{"zqxcanarykey": 1, "zqxcanarykey": 2}')
            with self.assertRaises(diag.DiagnosticRequestError) as raised:
                client._load_diagnostic_json(
                    str(path), label="request", max_bytes=client.MAX_DIAGNOSTIC_REQUEST_BYTES
                )
        self.assertNotIn("zqxcanarykey", str(raised.exception))

    def test_nonfinite_json_is_rejected(self) -> None:
        for payload in (b'{"a": NaN}', b'{"a": Infinity}', b'{"a": -Infinity}'):
            with self.subTest(payload=payload):
                with tempfile.TemporaryDirectory() as tmp:
                    with self.assertRaises(diag.DiagnosticRequestError):
                        self._load(tmp, payload)

    def test_bad_utf8_and_oversize_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(diag.DiagnosticRequestError):
                self._load(tmp, b'{"a": "\xff\xfe"}')
            with self.assertRaises(diag.DiagnosticRequestError):
                self._load(tmp, b"{" + b" " * client.MAX_DIAGNOSTIC_RECEIPT_BYTES + b"}", label="review")


class Round2CleanupRaceTests(_WorkspaceCase):
    def test_never_ending_reader_forces_unknown_and_cleanup_incomplete(self) -> None:
        request = self.request()
        stuck = StuckPipe()
        try:
            with patch.object(himalaya, "_DIAGNOSTIC_REAP_BUDGET_SECONDS", 0.3):
                with self.assertRaises(diag.DiagnosticError) as raised:
                    self.run_case(
                        request=request,
                        timeout=1,
                        popen=_popen_factory(stderr=stuck, exit_code=0),
                    )
            evidence = self.evidence(request)
        finally:
            stuck.release()
        self.assertEqual(diag.DIAGNOSTIC_UNKNOWN_OUTCOME_REASON, raised.exception.reason_code)
        self.assertTrue(evidence["cleanup_incomplete"])

    def test_success_evidence_records_cleanup_and_reap_flags(self) -> None:
        request = self.request()
        self.run_case(request=request, popen=_popen_factory(stderr=_success_stderr(), exit_code=0))
        evidence = self.evidence(request)
        self.assertFalse(evidence["cleanup_incomplete"])
        self.assertTrue(evidence["child_reaped"])


if __name__ == "__main__":
    unittest.main()



