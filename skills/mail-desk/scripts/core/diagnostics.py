"""Issue #19 — bounded, opt-in, diagnosis-only Himalaya trace capture.

The standalone ``save_draft`` API has deliberately **no** approval gate: a draft
is stored unsent.  Issue #19 needs a much narrower thing: a single, explicitly
allowed diagnostic run of ``message save`` that captures a *bounded* slice of the
Himalaya trace, so an operator can see whether a mailbox emits the documented
IMAP continuation-request warning without ever exposing raw protocol or
authentication material.

This module implements exactly that restricted contract.  It is **diagnosis
only** and adds no mailbox permission: it performs one ``message save``
(IMAP APPEND) into an explicitly reviewed folder, never sends mail, never
resolves a folder by discovery, never verifies, never retries and never falls
back.  The normal draft/batch paths are untouched, and the capture is threaded
through the *existing* ``himalaya.run_himalaya`` entrypoint as an opt-in,
keyword-only ``diagnostic_capture`` object (default ``None`` keeps the original
path byte-identical).

Design boundaries (read before relying on this module):

* **Explicit opt-in, default off.**  ``run_diagnostic_save_draft`` requires
  ``opt_in=True`` in code *and* an ``opt_in: true`` canonical request manifest;
  without both it stops before any local write or process.
* **Separate reviewed operator input.**  Approval is a *separate* receipt file
  (``DiagnosticReviewReceipt``), hash-bound to the exact canonical request
  (workspace, account, folder and operation included).  A ``reviewed: true``
  value inside the request manifest is not authority: the request key set is
  closed, so such a key is rejected fail-closed.  This mirrors the existing
  human receipts and makes **no** cryptographic-authentication claim.
* **Portable workspace identity.**  ``data_dir`` must be the canonical workspace
  ``<root>/data/mail-desk`` layout and the resolved ``root.name`` must equal the
  reviewed ``workspace_id``; reparse/symlink ancestors are refused.  This is a
  human-reviewed *minimum* identity only — same-name clones are a documented,
  non-cryptographic residual.
* **All read-only preflight before the marker.**  Request/receipt validation,
  explicit-account binding, workspace binding, timeout validation, draft
  building (with header-injection refusal) and strict UTF-8 payload validation
  and config/executable resolution all run *before* the one-shot marker is
  consumed, so a failed preflight never burns the attempt.
* **One-shot attempt.**  The attempt marker is then consumed atomically *before*
  the single child process.  A replay of the same canonical request is refused.
  A crash between marker consumption and APPEND leaves an unknown outcome; it is
  never retried or reconciled automatically.
* **Bounded, fail-closed trace parser.**  Only the three fixed
  ``imap_client::stream`` messages and the exact pinned ``imap_codec::response``
  continuation-request byte signature (no dynamic remainder) survive; spans,
  free-form fields, ANSI escapes, unknown targets and any dynamic text are
  **dropped**, never redacted and never persisted.  Emitted events carry only a
  fixed kind and bounded numeric counts.
* **No secrets in envelope or evidence.**  Evidence holds no mail fields
  (subject/to/body), no credentials and no filesystem paths; the child stdout is
  *not* exposed in the diagnostics result at all.

Authorized by the human decision recorded for issue #19; see the Mail-Desk L2
system map and the himalaya backend reference for the operator-facing contract.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import sys
import threading
import time
from typing import Any, Callable, Iterable, Mapping

from .common import atomic_write_json, normalize_message_id, utc_now_iso
from .drafts import _STDIN_TRANSPORT_TERMINATOR, build_draft_message
from .himalaya import build_himalaya_command, resolve_himalaya_invocation, run_himalaya
from .readiness import bind_workspace_account, workspace_root_from_data_dir


# ==============================================================================
# Public contract constants
# ==============================================================================

DIAGNOSTIC_SCHEMA_VERSION = 1
DIAGNOSTIC_REQUEST_KIND = "mail_desk_diagnostic_save_draft"
DIAGNOSTIC_RECEIPT_KIND = "mail_desk_diagnostic_review"
DIAGNOSTIC_EVIDENCE_KIND = "mail_desk_diagnostic_evidence"
DIAGNOSTIC_OPERATION_SAVE_DRAFT = "save_draft"

#: The candidate was reviewed by an explicit operator input, not a machine.
#: The value is a label, never a cryptographic proof of identity.
DIAGNOSTIC_OPERATOR_LABEL = "human_operator"

#: Operator-environment override for the trusted shared workspace-lock skill
#: root (mirrors the ``EVENT_DOCUMENTATION_WORKSPACE_LOCK_ROOT`` pattern).
DIAGNOSTIC_WORKSPACE_LOCK_ROOT_ENV = "MAIL_DESK_DIAGNOSTIC_WORKSPACE_LOCK_ROOT"

#: Exact request key set (closed): an unknown key such as ``reviewed`` is rejected.
DIAGNOSTIC_REQUEST_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "opt_in",
        "workspace_id",
        "account",
        "folder",
        "operation",
        "draft",
    }
)
DIAGNOSTIC_DRAFT_KEYS = frozenset({"to", "subject", "body", "cc", "from_addr", "date", "message_id"})
DIAGNOSTIC_RECEIPT_KEYS = frozenset(
    {"schema_version", "kind", "receipt_id", "request_hash", "approved_at", "approved_by"}
)

WORKSPACE_ID_REGEX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
RECEIPT_ID_REGEX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256_HEX_REGEX = re.compile(r"^[0-9a-f]{64}$")

#: Bounded draft-field lengths (the whole request JSON is separately capped at
#: ``MAX_DIAGNOSTIC_REQUEST_BYTES`` by the client loader).
MAX_DRAFT_TO_LENGTH = 2048
MAX_DRAFT_SUBJECT_LENGTH = 4096
MAX_DRAFT_BODY_LENGTH = 1_048_576
MAX_DRAFT_HEADER_LENGTH = 2048

#: Fixed child tracing filter.  ``off`` first, then only the two known targets.
DIAGNOSTIC_TRACING_FILTER = "off,imap_client::stream=trace,imap_codec::response=warn"

#: Only this target may contribute a *line-based* safe event, at exactly TRACE.
#: The ``imap_codec::response`` warning is recognised by its exact pinned byte
#: signature below (it embeds CR/LF and has no dynamic remainder).
DIAGNOSTIC_ALLOWED_TARGETS: Mapping[str, str] = {"imap_client::stream": "TRACE"}

#: Exact pinned upstream warning text (``imap-codec`` 2.0.0-alpha.7,
#: ``src/response.rs``): ``log::warn!("Rectified faulty continuation request
#: `+\\r\\n` to `+ ...\\r\\n`")``.  The Rust string literal contains real CR/LF
#: bytes and *no* dynamic server text, so the whole message is a constant.  The
#: record is gated upstream by the ``quirk_empty_continue_req`` build feature, so
#: absence of the record is not evidence the quirk is inactive.
RECTIFIED_CONTINUATION_MESSAGE = "Rectified faulty continuation request `+\r\n` to `+ ...\r\n`"
#: The three physical-line segments of the constant record, split on its CR/LF.
RECTIFIED_CONTINUATION_SEGMENTS: tuple[str, ...] = tuple(RECTIFIED_CONTINUATION_MESSAGE.split("\r\n"))
#: The full fixed formatter signature (level, target, exact constant message).
RECTIFIED_CONTINUATION_SIGNATURE = (
    "WARN imap_codec::response: " + RECTIFIED_CONTINUATION_MESSAGE
).encode("utf-8")
_RESPONSE_TARGET = "imap_codec::response"
_WARN_LEVEL = "WARN"

#: Fixed, per-child environment overrides.  They override any inherited value;
#: the process-global environment is never mutated.
DIAGNOSTIC_CHILD_ENV_OVERRIDES: Mapping[str, str] = {
    "RUST_LOG": DIAGNOSTIC_TRACING_FILTER,
    "RUST_BACKTRACE": "0",
    "RUST_LIB_BACKTRACE": "0",
    "RUST_LOG_STYLE": "never",
    "NO_COLOR": "1",
    "TERM": "dumb",
    "CLICOLOR": "0",
    "CLICOLOR_FORCE": "0",
    "PAGER": "cat",
}

#: Trace-tuning variables that must never be inherited from the parent.
DIAGNOSTIC_ENV_NORMALIZED_AWAY = (
    "RUST_LOG",
    "RUST_BACKTRACE",
    "RUST_LIB_BACKTRACE",
    "RUST_LOG_STYLE",
    "NO_COLOR",
    "TERM",
    "CLICOLOR",
    "CLICOLOR_FORCE",
    "MALLOC_CONF",
)

#: Bounded resources.
DIAGNOSTIC_STDERR_CAP_BYTES = 64 * 1024
DIAGNOSTIC_STDOUT_CAP_BYTES = 64 * 1024
DIAGNOSTIC_MAX_SAFE_EVENTS = 128
DIAGNOSTIC_READ_CHUNK_BYTES = 4096
DIAGNOSTIC_MAX_LINE_BYTES = 2048

#: The whole diagnostic run (child + pipe drain + reap) stays inside the
#: published ``run_himalaya`` wall-clock budget.  The extra bounded reap budget is
#: small and only used after a kill, so the total never exceeds ``timeout`` plus
#: this constant.
MAX_DIAGNOSTIC_TIMEOUT_SECONDS = 35
DIAGNOSTIC_REAP_BUDGET_SECONDS = 5.0

#: Fixed safe event kinds.
EVENT_STREAM_WRITE = "stream_write"
EVENT_STREAM_MORE_INPUT = "stream_more_input"
EVENT_STREAM_READ = "stream_read"
EVENT_RECTIFIED_CONTINUATION = "rectified_continuation"
DIAGNOSTIC_SAFE_EVENT_KINDS = frozenset(
    {EVENT_STREAM_WRITE, EVENT_STREAM_MORE_INPUT, EVENT_STREAM_READ, EVENT_RECTIFIED_CONTINUATION}
)

#: On-disk layout below the workspace ``data/mail-desk`` directory.
DIAGNOSTIC_DIRNAME = "diagnostics"
DIAGNOSTIC_ATTEMPTS_SUBDIR = "attempts"
DIAGNOSTIC_EVIDENCE_SUBDIR = "evidence"

#: Bounded reason codes.
DIAGNOSTIC_DISABLED_REASON = "diagnostics_opt_in_required"
DIAGNOSTIC_REQUEST_INVALID_REASON = "diagnostic_request_invalid"
DIAGNOSTIC_APPROVAL_INVALID_REASON = "diagnostic_approval_invalid"
DIAGNOSTIC_APPROVAL_DRIFT_REASON = "diagnostic_approval_drift"
DIAGNOSTIC_ARGUMENT_INVALID_REASON = "diagnostic_argument_invalid"
DIAGNOSTIC_REPLAY_REASON = "diagnostic_replay_refused"
DIAGNOSTIC_MARKER_FAILED_REASON = "diagnostic_attempt_marker_failed"
DIAGNOSTIC_BINDING_REASON = "diagnostic_workspace_binding_failed"
DIAGNOSTIC_LOCK_REASON = "diagnostic_lock_unavailable"
DIAGNOSTIC_UNKNOWN_OUTCOME_REASON = "diagnostic_append_outcome_unknown"
DIAGNOSTIC_FAILED_REASON = "diagnostic_append_failed"
DIAGNOSTIC_TRANSPORT_REASON = "diagnostic_transport_error"

#: Fixed, input-free failure texts (never echo request/receipt values).
_MESSAGE_INPUT_INVALID = "Diagnostic input failed validation; no process was started."
_MESSAGE_UNKNOWN = "The diagnostic APPEND outcome is unknown; the attempt is not retried."
_MESSAGE_FAILED = "The diagnostic APPEND failed; only sanitized diagnostics were recorded."
_MESSAGE_TRANSPORT = "The diagnostic child could not be launched or communicated with."


# ==============================================================================
# Exceptions
# ==============================================================================

class DiagnosticError(RuntimeError):
    """A fail-closed, classified diagnostics-contract failure."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class DiagnosticRequestError(DiagnosticError):
    """The canonical diagnostic request is malformed or not opt-in."""


class DiagnosticApprovalError(DiagnosticError):
    """The separate reviewed operator receipt is missing, malformed or drifted."""


class DiagnosticReplayError(DiagnosticError):
    """The one-shot attempt for this exact request was already consumed."""


class DiagnosticBindingError(DiagnosticError):
    """The workspace account/identity binding could not be established."""


class DiagnosticLockError(DiagnosticError):
    """The workspace lock is missing or not owned by this invocation."""


# ==============================================================================
# Canonical request / receipt
# ==============================================================================

def _is_real_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_text(
    value: Any,
    field: str,
    *,
    max_length: int,
    pattern: re.Pattern[str] | None = None,
    error_type: type[DiagnosticError] = None,  # type: ignore[assignment]
) -> str:
    error = error_type or DiagnosticRequestError
    if not isinstance(value, str) or not value:
        raise error(DIAGNOSTIC_REQUEST_INVALID_REASON, f"Diagnostic field '{field}' must be a non-empty string.")
    if len(value) > max_length:
        raise error(DIAGNOSTIC_REQUEST_INVALID_REASON, f"Diagnostic field '{field}' exceeds the maximum length.")
    if pattern is not None and not pattern.fullmatch(value):
        raise error(DIAGNOSTIC_REQUEST_INVALID_REASON, f"Diagnostic field '{field}' has an invalid format.")
    _require_utf8(value)
    return value


def _require_utf8(value: str) -> None:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise DiagnosticRequestError(
            DIAGNOSTIC_REQUEST_INVALID_REASON,
            "Diagnostic text must be encodable as strict UTF-8; refusing before any process.",
        ) from None


def _reject_unknown_keys(mapping: Mapping[str, Any], allowed: frozenset[str], *, where: str) -> None:
    if set(mapping) - allowed:
        # Fixed, input-free message: never echo caller keys (they can be canaries).
        raise DiagnosticRequestError(
            DIAGNOSTIC_REQUEST_INVALID_REASON,
            f"The diagnostic {where} key set is not exactly the canonical closed set.",
        )


def _reject_header_injection(value: str, field: str) -> None:
    if "\r" in value or "\n" in value or "\x00" in value:
        raise DiagnosticRequestError(
            DIAGNOSTIC_REQUEST_INVALID_REASON,
            f"Diagnostic draft field '{field}' must not contain control or newline characters.",
        )


def validate_diagnostic_request(request: Any) -> dict[str, Any]:
    """Validate and normalize the closed, versioned diagnostic request.

    Adds no authority: it only proves the *shape*.  Approval lives in the
    separate operator receipt validated by :func:`validate_diagnostic_receipt`.
    """
    if not isinstance(request, Mapping):
        raise DiagnosticRequestError(DIAGNOSTIC_REQUEST_INVALID_REASON, "Diagnostic request must be an object.")
    _reject_unknown_keys(request, DIAGNOSTIC_REQUEST_KEYS, where="request")
    for value in request.values():
        if isinstance(value, str):
            _require_utf8(value)
    if isinstance(request.get("draft"), Mapping):
        for value in request["draft"].values():
            if isinstance(value, str):
                _require_utf8(value)

    if not _is_real_int(request.get("schema_version")) or request["schema_version"] != DIAGNOSTIC_SCHEMA_VERSION:
        raise DiagnosticRequestError(
            DIAGNOSTIC_REQUEST_INVALID_REASON,
            f"Diagnostic request schema_version must be the integer {DIAGNOSTIC_SCHEMA_VERSION}.",
        )
    if request.get("kind") != DIAGNOSTIC_REQUEST_KIND:
        raise DiagnosticRequestError(
            DIAGNOSTIC_REQUEST_INVALID_REASON, f"Diagnostic request kind must be '{DIAGNOSTIC_REQUEST_KIND}'."
        )
    if request.get("opt_in") is not True:
        raise DiagnosticRequestError(
            DIAGNOSTIC_DISABLED_REASON, "Diagnostic request must set opt_in to the boolean true."
        )

    workspace_id = request.get("workspace_id")
    if not isinstance(workspace_id, str) or not WORKSPACE_ID_REGEX.fullmatch(workspace_id):
        raise DiagnosticRequestError(
            DIAGNOSTIC_REQUEST_INVALID_REASON,
            "Diagnostic request workspace_id must be a portable token (letters/digits/._-, no path separators).",
        )

    # Explicit, non-null account is required: a ``null`` request account would
    # bind a different effective account than the review hash covers.
    account = request.get("account")
    if not isinstance(account, str) or not account.strip() or account != account.strip():
        raise DiagnosticRequestError(
            DIAGNOSTIC_REQUEST_INVALID_REASON,
            "Diagnostic request account must be an explicit, non-blank, trimmed string.",
        )

    folder = _require_text(request.get("folder"), "folder", max_length=512)
    if any(ord(ch) < 32 for ch in folder):
        raise DiagnosticRequestError(
            DIAGNOSTIC_REQUEST_INVALID_REASON, "Diagnostic request folder must not contain control characters."
        )

    if request.get("operation") != DIAGNOSTIC_OPERATION_SAVE_DRAFT:
        raise DiagnosticRequestError(
            DIAGNOSTIC_REQUEST_INVALID_REASON,
            "The diagnostic contract allows exactly one operation: 'save_draft'.",
        )

    draft = request.get("draft")
    if not isinstance(draft, Mapping):
        raise DiagnosticRequestError(DIAGNOSTIC_REQUEST_INVALID_REASON, "Diagnostic request draft must be an object.")
    _reject_unknown_keys(draft, DIAGNOSTIC_DRAFT_KEYS, where="draft")
    to = _require_text(draft.get("to"), "draft.to", max_length=MAX_DRAFT_TO_LENGTH)
    subject = draft.get("subject", "")
    body = draft.get("body", "")
    if not isinstance(subject, str) or len(subject) > MAX_DRAFT_SUBJECT_LENGTH:
        raise DiagnosticRequestError(
            DIAGNOSTIC_REQUEST_INVALID_REASON,
            f"Diagnostic request draft.subject must be a string of at most {MAX_DRAFT_SUBJECT_LENGTH} characters.",
        )
    if not isinstance(body, str) or len(body) > MAX_DRAFT_BODY_LENGTH:
        raise DiagnosticRequestError(
            DIAGNOSTIC_REQUEST_INVALID_REASON,
            f"Diagnostic request draft.body must be a string of at most {MAX_DRAFT_BODY_LENGTH} characters.",
        )
    normalized_draft: dict[str, Any] = {"to": to, "subject": subject, "body": body}
    for name in ("cc", "from_addr", "date", "message_id"):
        value = draft.get(name)
        if value is None:
            continue
        if not isinstance(value, str) or not value or len(value) > MAX_DRAFT_HEADER_LENGTH:
            raise DiagnosticRequestError(
                DIAGNOSTIC_REQUEST_INVALID_REASON,
                f"Diagnostic request draft.{name} must be a non-empty string of at most "
                f"{MAX_DRAFT_HEADER_LENGTH} characters.",
            )
        normalized_draft[name] = value
    for name in ("to", "cc", "from_addr", "subject", "date", "message_id"):
        if name in normalized_draft:
            _reject_header_injection(normalized_draft[name], name)

    return {
        "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
        "kind": DIAGNOSTIC_REQUEST_KIND,
        "opt_in": True,
        "workspace_id": workspace_id,
        "account": account,
        "folder": folder,
        "operation": DIAGNOSTIC_OPERATION_SAVE_DRAFT,
        "draft": normalized_draft,
    }


def canonical_request_sha256(request: Any) -> str:
    """Deterministic SHA-256 over the validated canonical request.

    The hash covers ``workspace_id``, ``account``, ``folder``, ``operation`` and
    the draft payload, so a receipt can never be reused across a drifted request.
    """
    normalized = validate_diagnostic_request(request)
    canonical = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse_rfc3339(value: str) -> datetime | None:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def validate_diagnostic_receipt(receipt: Any, *, request: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the separate reviewed operator receipt against the exact request.

    The receipt is trusted *reviewed operator input* like every other mail-desk
    receipt.  This function makes no cryptographic-authentication claim; it
    verifies structure, real timestamp and hash-binding only.
    """
    if not isinstance(receipt, Mapping):
        raise DiagnosticApprovalError(
            DIAGNOSTIC_APPROVAL_INVALID_REASON, "Diagnostic review receipt must be an object."
        )
    if set(receipt) - DIAGNOSTIC_RECEIPT_KEYS:
        raise DiagnosticApprovalError(
            DIAGNOSTIC_APPROVAL_INVALID_REASON,
            "The diagnostic review receipt key set is not exactly the canonical closed set.",
        )
    if DIAGNOSTIC_RECEIPT_KEYS - set(receipt):
        raise DiagnosticApprovalError(
            DIAGNOSTIC_APPROVAL_INVALID_REASON, "The diagnostic review receipt is missing required keys."
        )
    if not _is_real_int(receipt.get("schema_version")) or receipt["schema_version"] != DIAGNOSTIC_SCHEMA_VERSION:
        raise DiagnosticApprovalError(
            DIAGNOSTIC_APPROVAL_INVALID_REASON,
            f"Diagnostic review receipt schema_version must be the integer {DIAGNOSTIC_SCHEMA_VERSION}.",
        )
    if receipt.get("kind") != DIAGNOSTIC_RECEIPT_KIND:
        raise DiagnosticApprovalError(
            DIAGNOSTIC_APPROVAL_INVALID_REASON, f"Diagnostic review receipt kind must be '{DIAGNOSTIC_RECEIPT_KIND}'."
        )
    receipt_id = receipt.get("receipt_id")
    if not isinstance(receipt_id, str) or not RECEIPT_ID_REGEX.fullmatch(receipt_id):
        raise DiagnosticApprovalError(
            DIAGNOSTIC_APPROVAL_INVALID_REASON,
            "Diagnostic review receipt receipt_id must be a restricted token (letters/digits/._-).",
        )
    approved_by = _require_text(receipt.get("approved_by"), "approved_by", max_length=128, error_type=DiagnosticApprovalError)
    approved_at = _require_text(receipt.get("approved_at"), "approved_at", max_length=64, error_type=DiagnosticApprovalError)
    if _parse_rfc3339(approved_at) is None:
        raise DiagnosticApprovalError(
            DIAGNOSTIC_APPROVAL_INVALID_REASON, "Diagnostic review receipt approved_at must be a real RFC-3339 instant."
        )
    expected_hash = canonical_request_sha256(dict(request))
    provided_hash = receipt.get("request_hash")
    if not isinstance(provided_hash, str) or not SHA256_HEX_REGEX.fullmatch(provided_hash.lower()):
        raise DiagnosticApprovalError(
            DIAGNOSTIC_APPROVAL_INVALID_REASON, "Diagnostic review receipt request_hash must be a 64-hex SHA-256."
        )
    if provided_hash.lower() != expected_hash:
        raise DiagnosticApprovalError(
            DIAGNOSTIC_APPROVAL_DRIFT_REASON,
            "Diagnostic review receipt request_hash does not match the exact canonical request; refusing.",
        )
    return {
        "receipt_id": receipt_id,
        "approved_by": approved_by,
        "approved_at": approved_at,
        "request_hash": expected_hash,
    }


# ==============================================================================
# Bounded trace parsing
# ==============================================================================

_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\s+"
    r"(TRACE|DEBUG|INFO|WARN|ERROR)\s+"
    r"([A-Za-z0-9_-]+(?:::[A-Za-z0-9_-]+)*):\s?(.*)$"
)
_WROTE_RE = re.compile(r"^wrote (\d{1,10}) bytes$")
_READ_RE = re.compile(r"^read (\d{1,10})/(\d{1,10}) bytes$")
_MORE_INPUT_MESSAGE = "more input needed"
_STREAM_TARGET = "imap_client::stream"


class BoundedTraceParser:
    """Incremental, fail-closed parser that only ever emits fixed safe events.

    Unknown targets, disallowed levels, span/free-formatted lines, ANSI escapes,
    multiline fragments and oversize lines are dropped.  Dynamic message text is
    never retained: an event carries only its fixed kind and bounded numbers.
    The continuation warning is accepted positively only as the exact three-line
    record with the allowlisted ``WARN imap_codec::response:`` prefix and the
    pinned constant; the constant appearing inside unknown server content is
    never accepted.
    """

    def __init__(
        self,
        *,
        max_events: int = DIAGNOSTIC_MAX_SAFE_EVENTS,
        max_line_bytes: int = DIAGNOSTIC_MAX_LINE_BYTES,
    ) -> None:
        self._max_events = int(max_events)
        self._max_line_bytes = int(max_line_bytes)
        self._buffer = bytearray()
        self._in_overlong = False
        self._continuation_state = 0
        self.bytes_seen = 0
        self.dropped_unknown = 0
        self.dropped_malformed = 0
        self.events_overflow = False
        self.events: list[dict[str, Any]] = []
        self._finished = False

    def feed(self, chunk: bytes) -> bool:
        """Consume one chunk; return ``False`` once no further input is needed."""
        if self._finished or not chunk:
            return not self._finished
        self.bytes_seen += len(chunk)
        self._buffer.extend(chunk)
        while True:
            newline = self._buffer.find(b"\n")
            if newline < 0:
                break
            raw_line = bytes(self._buffer[:newline])
            del self._buffer[: newline + 1]
            if self._in_overlong:
                # The newline terminates the discarded overlong run; the tail is
                # never treated as a fresh line.
                self._in_overlong = False
                continue
            self._process_line(raw_line)
            if self.events_overflow:
                self._finished = True
                return False
        if not self._in_overlong and len(self._buffer) > self._max_line_bytes:
            # Discard the whole overlong line (until the next newline) instead of
            # clearing the prefix and later parsing the suffix as a line.
            self._buffer.clear()
            self._in_overlong = True
            self.dropped_malformed += 1
        elif self._in_overlong:
            self._buffer.clear()
        return True

    def finish(self) -> dict[str, Any]:
        if self._buffer:
            self.dropped_malformed += 1
        self._buffer.clear()
        self._finished = True
        return self.result()

    def result(self) -> dict[str, Any]:
        return {
            "stderr_bytes": int(self.bytes_seen),
            "safe_events": [dict(event) for event in self.events],
            "event_count": len(self.events),
            "dropped_unknown": int(self.dropped_unknown),
            "dropped_malformed": int(self.dropped_malformed),
            "events_overflow": bool(self.events_overflow),
        }

    def _append_event(self, event: dict[str, Any]) -> None:
        if len(self.events) >= self._max_events:
            self.events_overflow = True
            return
        event["ordinal"] = len(self.events) + 1
        self.events.append(event)

    def _process_line(self, raw_line: bytes) -> None:
        if len(raw_line) > self._max_line_bytes:
            self.dropped_malformed += 1
            return
        if raw_line.endswith(b"\r"):
            raw_line = raw_line[:-1]
        try:
            line = raw_line.decode("utf-8")
        except UnicodeDecodeError:
            self.dropped_malformed += 1
            self._continuation_state = 0
            return
        if "\x1b" in line or "\r" in line or "\x00" in line:
            self.dropped_malformed += 1
            self._continuation_state = 0
            return

        # Positive continuation-record state machine: only the exact three-line
        # constant, immediately after the allowlisted trace prefix, is accepted.
        if self._continuation_state == 1:
            if line == RECTIFIED_CONTINUATION_SEGMENTS[1]:
                self._continuation_state = 2
                return
            self._continuation_state = 0
        elif self._continuation_state == 2:
            if line == RECTIFIED_CONTINUATION_SEGMENTS[2]:
                self._continuation_state = 0
                self._append_event({"kind": EVENT_RECTIFIED_CONTINUATION})
                return
            self._continuation_state = 0

        match = _LINE_RE.match(line)
        if match is None:
            self.dropped_malformed += 1
            return
        _timestamp, level, target, message = match.groups()
        if level == _WARN_LEVEL and target == _RESPONSE_TARGET:
            if message == RECTIFIED_CONTINUATION_SEGMENTS[0]:
                self._continuation_state = 1
                return
            self.dropped_unknown += 1
            return
        if target != _STREAM_TARGET or level != DIAGNOSTIC_ALLOWED_TARGETS.get(_STREAM_TARGET):
            self.dropped_unknown += 1
            return
        event = self._classify_stream(message)
        if event is None:
            # Classify first: an unknown message never counts as an overflow.
            self.dropped_unknown += 1
            return
        self._append_event(event)

    @staticmethod
    def _classify_stream(message: str) -> dict[str, Any] | None:
        wrote = _WROTE_RE.match(message)
        if wrote is not None:
            return {"kind": EVENT_STREAM_WRITE, "count": int(wrote.group(1))}
        read = _READ_RE.match(message)
        if read is not None:
            return {"kind": EVENT_STREAM_READ, "count": int(read.group(1)), "total": int(read.group(2))}
        if message == _MORE_INPUT_MESSAGE:
            return {"kind": EVENT_STREAM_MORE_INPUT}
        return None


def parse_trace_stream(
    chunks: Iterable[bytes],
    *,
    max_events: int = DIAGNOSTIC_MAX_SAFE_EVENTS,
    max_line_bytes: int = DIAGNOSTIC_MAX_LINE_BYTES,
) -> dict[str, Any]:
    """Parse a bounded iterable of stderr byte chunks into safe events only."""
    parser = BoundedTraceParser(max_events=max_events, max_line_bytes=max_line_bytes)
    for chunk in chunks:
        if not parser.feed(chunk):
            break
    return parser.finish()


# ==============================================================================
# Diagnostic capture object (threaded into run_himalaya)
# ==============================================================================

def build_diagnostic_child_environment(base_env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return the fixed, per-child environment for a diagnostic run.

    Any inherited tracing/backtrace/colour variables are overwritten (or
    removed), so a manipulated ``RUST_LOG`` in the parent cannot widen the
    filter or enable an unsafe target.  ``os.environ`` is never mutated.
    """
    source = dict(os.environ if base_env is None else base_env)
    for name in DIAGNOSTIC_ENV_NORMALIZED_AWAY:
        source.pop(name, None)
    source.update(DIAGNOSTIC_CHILD_ENV_OVERRIDES)
    return source


class DiagnosticTraceCapture:
    """Typed, opt-in, internal diagnostic collector for ``run_himalaya``.

    The object carries the fixed child environment and resource bounds and
    receives the bounded, structured run result.  It never holds raw stderr or
    stdout; pass it as ``run_himalaya(..., diagnostic_capture=capture)``.
    """

    schema_version = DIAGNOSTIC_SCHEMA_VERSION

    def __init__(
        self,
        *,
        stderr_cap_bytes: int = DIAGNOSTIC_STDERR_CAP_BYTES,
        stdout_cap_bytes: int = DIAGNOSTIC_STDOUT_CAP_BYTES,
        max_safe_events: int = DIAGNOSTIC_MAX_SAFE_EVENTS,
        read_chunk_bytes: int = DIAGNOSTIC_READ_CHUNK_BYTES,
        base_env: Mapping[str, str] | None = None,
    ) -> None:
        self.stderr_cap_bytes = int(stderr_cap_bytes)
        self.stdout_cap_bytes = int(stdout_cap_bytes)
        self.max_safe_events = int(max_safe_events)
        self.read_chunk_bytes = int(read_chunk_bytes)
        self._base_env = base_env
        self._result: dict[str, Any] | None = None

    def child_environment(self, base_env: Mapping[str, str] | None = None) -> dict[str, str]:
        return build_diagnostic_child_environment(self._base_env if base_env is None else base_env)

    def parse_stderr(self, chunks: Iterable[bytes]) -> dict[str, Any]:
        """Parse an already-frozen list of bounded stderr chunks into safe events."""
        return parse_trace_stream(chunks, max_events=self.max_safe_events)

    def record_run(self, result: Mapping[str, Any]) -> None:
        self._result = dict(result)

    def result(self) -> dict[str, Any] | None:
        return None if self._result is None else dict(self._result)


# ==============================================================================
# One-shot attempt marker
# ==============================================================================

def resolve_diagnostic_dir(data_dir: str | Path) -> Path:
    """Return the diagnostics root below the workspace ``data/mail-desk`` dir."""
    root = Path(data_dir) / DIAGNOSTIC_DIRNAME
    for path in (root, root / DIAGNOSTIC_ATTEMPTS_SUBDIR, root / DIAGNOSTIC_EVIDENCE_SUBDIR):
        _assert_no_reparse_ancestors(path)
    return root


def consume_attempt_marker(data_dir: str | Path, *, request_hash: str, request: Mapping[str, Any]) -> dict[str, Any]:
    """Atomically consume the one-shot attempt marker for one exact request.

    The marker is created with an exclusive create *before* the single child
    process is started.  A second call for the same request hash raises
    ``DiagnosticReplayError``; there is no retry, replay or automatic cleanup.
    """
    if not isinstance(request_hash, str) or not SHA256_HEX_REGEX.fullmatch(request_hash.lower()):
        raise DiagnosticRequestError(
            DIAGNOSTIC_REQUEST_INVALID_REASON, "Attempt marker requires a 64-hex request hash."
        )
    attempts_dir = resolve_diagnostic_dir(data_dir) / DIAGNOSTIC_ATTEMPTS_SUBDIR
    attempts_dir.mkdir(parents=True, exist_ok=True)
    marker_path = attempts_dir / f"{request_hash.lower()}.json"
    payload = {
        "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
        "request_hash": request_hash.lower(),
        "workspace_id": request["workspace_id"],
        "account": request.get("account"),
        "folder": request["folder"],
        "operation": request["operation"],
        "consumed_at": utc_now_iso(),
        "phase": "attempt_consumed",
    }
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    try:
        handle = os.open(marker_path, flags)
    except FileExistsError:
        raise DiagnosticReplayError(
            DIAGNOSTIC_REPLAY_REASON,
            "The one-shot diagnostic attempt for this exact request was already consumed; refusing a replay.",
        ) from None
    except OSError:
        raise DiagnosticError(
            DIAGNOSTIC_MARKER_FAILED_REASON,
            "The one-shot attempt marker could not be created; refusing before any process.",
        ) from None
    try:
        view = memoryview(encoded)
        total = 0
        while total < len(view):
            written = os.write(handle, view[total:])
            if written <= 0:
                raise OSError("attempt marker write made no progress")
            total += int(written)
        os.fsync(handle)
    except OSError:
        # Fail closed: the exclusive marker may now be partial, and a retry is
        # refused by design.  There is no background retry or cleanup.
        raise DiagnosticError(
            DIAGNOSTIC_MARKER_FAILED_REASON,
            "The one-shot attempt marker could not be written completely; refusing before any process.",
        ) from None
    finally:
        os.close(handle)
    return payload


# ==============================================================================
# Workspace identity binding
# ==============================================================================

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _is_reparse_point(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT) or path.is_symlink()


def _assert_no_reparse_ancestors(path: Path) -> None:
    """Refuse a path whose own chain up to the filesystem root uses a link/junction."""
    current = path
    while True:
        if _is_reparse_point(current):
            raise DiagnosticBindingError(
                DIAGNOSTIC_BINDING_REASON,
                "The diagnostic workspace path or one of its ancestors is a link/junction; refusing.",
            )
        parent = current.parent
        if parent == current:
            return
        current = parent


def bind_diagnostic_workspace(data_dir: str | Path, *, workspace_id: str) -> Path:
    """Resolve and validate the canonical workspace root for a diagnostic run.

    ``data_dir`` must be exactly the workspace ``<root>/data/mail-desk`` layout
    and the resolved ``root.name`` must equal the reviewed ``workspace_id``; a
    same-name clone is a documented, non-cryptographic residual, and link/
    junction/hardlink ancestors are refused.
    """
    try:
        root = workspace_root_from_data_dir(Path(data_dir))
    except ValueError as exc:
        raise DiagnosticBindingError(
            DIAGNOSTIC_BINDING_REASON,
            "data_dir must be the canonical workspace data/mail-desk directory; refusing.",
        ) from None
    if root.name != workspace_id:
        raise DiagnosticBindingError(
            DIAGNOSTIC_BINDING_REASON,
            "The resolved workspace root name does not match the reviewed workspace_id; refusing.",
        )
    _assert_no_reparse_ancestors(root / "data" / "mail-desk")
    return root


# ==============================================================================
# Workspace lock (shared guard, allow_legacy=False)
# ==============================================================================

def _trusted_lock_root(explicit: str | Path | None = None) -> Path:
    if explicit is not None:
        return Path(explicit)
    env = (os.environ.get("WORKSPACE_LOCK_ROOT", "").strip()
           or os.environ.get(DIAGNOSTIC_WORKSPACE_LOCK_ROOT_ENV, "").strip())
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[5] / "workspace-lock"


def _load_workspace_lock_guard(explicit_root: str | Path | None = None) -> Any:
    """Load the canonical shared guard from a bounded, trusted root.

    Mirrors the documented per-skill override pattern: an explicit root wins,
    then ``WORKSPACE_LOCK_ROOT``, ``MAIL_DESK_DIAGNOSTIC_WORKSPACE_LOCK_ROOT``, then the bundle-relative
    default.  A freshly executed module is used; a pre-existing ``sys.modules``
    entry is never trusted (only saved and restored for ``@dataclass``).
    """
    guard_path = _trusted_lock_root(explicit_root) / "scripts" / "workspace_lock_guard.py"
    if not guard_path.is_file():
        raise DiagnosticLockError(
             DIAGNOSTIC_LOCK_REASON, "Install workspace-lock and set WORKSPACE_LOCK_ROOT "
             "to its trusted installation; see README.md. Refusing mutation."
        )
    name = "_mail_desk_diagnostic_workspace_lock_guard"
    spec = importlib.util.spec_from_file_location(name, str(guard_path))
    if spec is None or spec.loader is None:
        raise DiagnosticLockError(DIAGNOSTIC_LOCK_REASON, "The shared workspace-lock guard could not be loaded.")
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(name)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        raise DiagnosticLockError(
            DIAGNOSTIC_LOCK_REASON, "The shared workspace-lock guard could not be executed; refusing."
        ) from None
    finally:
        if previous is None:
            sys.modules.pop(name, None)
    return module


def require_owned_workspace_lock(
    workspace_root: str | Path,
    *,
    lease_id: str | None = None,
    conversation_id: str | None = None,
    lock_root: str | Path | None = None,
) -> Any:
    """Require an invocation-owned lock through the shared guard, never legacy."""
    guard = _load_workspace_lock_guard(lock_root)
    return guard.require_workspace_lock(
        workspace_root,
        lease_id=lease_id,
        conversation_id=conversation_id,
        allow_legacy=False,
    )


# ==============================================================================
# Orchestration
# ==============================================================================

def _validate_timeout(timeout: Any) -> int:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise DiagnosticError(DIAGNOSTIC_ARGUMENT_INVALID_REASON, "Diagnostic timeout must be a finite number.")
    if not math.isfinite(timeout) or not (0 < timeout <= MAX_DIAGNOSTIC_TIMEOUT_SECONDS):
        raise DiagnosticError(
            DIAGNOSTIC_ARGUMENT_INVALID_REASON,
            f"Diagnostic timeout must be finite and within (0, {MAX_DIAGNOSTIC_TIMEOUT_SECONDS}].",
        )
    if not float(timeout).is_integer():
        raise DiagnosticError(
            DIAGNOSTIC_ARGUMENT_INVALID_REASON, "Diagnostic timeout must be a whole number of seconds."
        )
    return int(timeout)


def _effective_lock_credentials(lease_id: str | None, conversation_id: str | None) -> tuple[str | None, str | None]:
    effective_lease = lease_id if lease_id is not None else (os.environ.get("WORKSPACE_LOCK_LEASE_ID") or None)
    effective_conversation = (
        conversation_id
        if conversation_id is not None
        else (os.environ.get("WORKSPACE_LOCK_CONVERSATION_ID") or None)
    )
    return effective_lease, effective_conversation


def _empty_evidence_metrics() -> dict[str, Any]:
    return {
        "child_exit_code": None,
        "child_timed_out": False,
        "child_reaped": False,
        "cleanup_incomplete": False,
        "stderr_bytes": 0,
        "stderr_overflow": False,
        "stdout_overflow": False,
        "events_overflow": False,
        "reader_error": False,
        "safe_events": [],
        "dropped_unknown": 0,
        "dropped_malformed": 0,
        "stdout_bytes": 0,
        "stdout_truncated": False,
    }


def _build_evidence(
    *,
    request: Mapping[str, Any],
    request_hash: str,
    receipt: Mapping[str, Any],
    outcome: str,
    run_result: Mapping[str, Any] | None,
    reason_code: str | None = None,
) -> dict[str, Any]:
    """Assemble additive structured evidence; never any mail field or path."""
    evidence: dict[str, Any] = {
        "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
        "kind": DIAGNOSTIC_EVIDENCE_KIND,
        "request_hash": request_hash,
        "workspace_id": request["workspace_id"],
        "account": request.get("account"),
        "folder": request["folder"],
        "operation": request["operation"],
        "operator_label": DIAGNOSTIC_OPERATOR_LABEL,
        "receipt_id": receipt.get("receipt_id"),
        "outcome": outcome,
        "generated_at": utc_now_iso(),
    }
    if reason_code is not None:
        evidence["reason_code"] = reason_code
    metrics = _empty_evidence_metrics()
    if run_result is not None:
        stderr = run_result.get("stderr") or {}
        metrics.update(
            {
                "child_exit_code": run_result.get("exit_code"),
                "child_timed_out": bool(run_result.get("timed_out")),
                "child_reaped": bool(run_result.get("child_reaped")),
                "cleanup_incomplete": bool(run_result.get("cleanup_incomplete")),
                "stderr_bytes": int(stderr.get("stderr_bytes", 0)),
                "stderr_overflow": bool(run_result.get("stderr_overflow")),
                "stdout_overflow": bool(run_result.get("stdout_overflow")),
                "events_overflow": bool(stderr.get("events_overflow")),
                "reader_error": bool(run_result.get("reader_error")),
                "safe_events": list(stderr.get("safe_events", [])),
                "dropped_unknown": int(stderr.get("dropped_unknown", 0)),
                "dropped_malformed": int(stderr.get("dropped_malformed", 0)),
                "stdout_bytes": int(run_result.get("stdout_bytes", 0)),
                "stdout_truncated": bool(run_result.get("stdout_truncated")),
            }
        )
    evidence.update(metrics)
    return evidence


def _write_evidence(data_dir: Path, request_hash: str, evidence: Mapping[str, Any]) -> Path:
    evidence_dir = resolve_diagnostic_dir(data_dir) / DIAGNOSTIC_EVIDENCE_SUBDIR
    target = evidence_dir / f"{request_hash}.json"
    atomic_write_json(target, dict(evidence))
    return target


def _outcome_for_reason(reason_code: str) -> tuple[str, str]:
    """Map a classified child failure to ``(outcome, reason_code)``.

    Only a child that actually ran and returned a non-zero exit is a definitive
    failure.  Everything else -- including a bootstrap/resolve error after the
    read-only preflight passed (a TOCTOU window), or an unclassified reason -- is
    an *unknown* outcome, never a definitive failure.
    """
    if reason_code == "himalaya_command_failed":
        return "failed", DIAGNOSTIC_FAILED_REASON
    if reason_code in ("himalaya_timeout", "himalaya_diagnostic_overflow", "himalaya_outcome_unknown",
                       "himalaya_reader_failed"):
        return "unknown", DIAGNOSTIC_UNKNOWN_OUTCOME_REASON
    return "unknown", DIAGNOSTIC_TRANSPORT_REASON


def run_diagnostic_save_draft(
    request: Any,
    *,
    receipt: Any,
    data_dir: str | Path,
    opt_in: bool = False,
    workspace_root: str | Path | None = None,
    lease_id: str | None = None,
    conversation_id: str | None = None,
    timeout: float = MAX_DIAGNOSTIC_TIMEOUT_SECONDS,
    lock_verifier: Callable[..., Any] | None = None,
    account_binder: Callable[..., Any] | None = None,
    lock_root: str | Path | None = None,
) -> dict[str, Any]:
    """Run the single, authorized, bounded diagnostic ``save_draft``.

    Ordering is deliberate.  Every read-only preflight (request, receipt,
    workspace and account binding, timeout, draft building, strict UTF-8 payload
    validation and config/executable resolution) runs **before** the one-shot
    attempt marker is consumed, so a failed preflight never burns the attempt.
    Only then are the marker and the single ``message save`` child started,
    through the existing ``run_himalaya`` entrypoint.
    """
    if opt_in is not True:
        raise DiagnosticError(
            DIAGNOSTIC_DISABLED_REASON,
            "Bounded diagnostics are opt-in and default off; pass the explicit opt-in to continue.",
        )

    normalized = validate_diagnostic_request(request)
    receipt_info = validate_diagnostic_receipt(receipt, request=normalized)
    request_hash = receipt_info["request_hash"]
    effective_timeout = _validate_timeout(timeout)

    resolved_data_dir = Path(data_dir)
    root = bind_diagnostic_workspace(resolved_data_dir, workspace_id=normalized["workspace_id"])
    if workspace_root is not None:
        explicit_root = Path(workspace_root).expanduser().resolve()
        if explicit_root != root:
            raise DiagnosticBindingError(
                DIAGNOSTIC_BINDING_REASON,
                "The explicit workspace_root does not match the canonical data_dir root; refusing.",
            )

    binder = account_binder or bind_workspace_account
    try:
        binding = binder(resolved_data_dir, requested_account=normalized["account"])
    except DiagnosticError:
        raise
    except Exception:
        raise DiagnosticBindingError(
            DIAGNOSTIC_BINDING_REASON,
            "Workspace account binding failed before any process; refusing.",
        ) from None
    if not isinstance(binding, Mapping) or not binding.get("ok"):
        raise DiagnosticBindingError(
            DIAGNOSTIC_BINDING_REASON,
            "Workspace account binding failed before any process; refusing.",
        )
    if binding.get("account") is None or str(binding.get("account")).strip() != normalized["account"]:
        raise DiagnosticBindingError(
            DIAGNOSTIC_BINDING_REASON,
            "The effective workspace account does not exactly match the reviewed account; refusing.",
        )
    effective_account = normalized["account"]

    eff_lease, eff_conversation = _effective_lock_credentials(lease_id, conversation_id)
    verifier = lock_verifier or (
        lambda ws, *, lease_id, conversation_id: require_owned_workspace_lock(
            ws, lease_id=lease_id, conversation_id=conversation_id, lock_root=lock_root
        )
    )
    try:
        verifier(root, lease_id=eff_lease, conversation_id=eff_conversation)
    except DiagnosticError:
        raise
    except Exception:
        raise DiagnosticLockError(
            DIAGNOSTIC_LOCK_REASON,
            "The workspace lock is missing or not owned by this invocation; no process was started.",
        ) from None

    # Draft building + header validation + strict UTF-8 payload validation, and
    # config/executable resolution, all before the attempt is consumed.
    draft = normalized["draft"]
    raw_message, normalized_id, date_header = build_draft_message(
        draft["subject"],
        draft["to"],
        draft.get("body", ""),
        from_addr=draft.get("from_addr"),
        cc=draft.get("cc"),
        date=draft.get("date"),
        message_id=draft.get("message_id"),
    )
    stdin_payload = raw_message.encode("utf-8") + _STDIN_TRANSPORT_TERMINATOR
    try:
        stdin_payload.decode("utf-8")
    except UnicodeDecodeError:
        raise DiagnosticError(
            DIAGNOSTIC_ARGUMENT_INVALID_REASON, "The diagnostic payload is not valid UTF-8; no process was started."
        ) from None
    try:
        resolve_himalaya_invocation()
    except Exception:
        raise DiagnosticError(
            DIAGNOSTIC_ARGUMENT_INVALID_REASON,
            "The Himalaya executable/config could not be resolved; no process was started.",
        ) from None
    # Pre-resolve the exact command shape (read-only) so a resolve failure is a
    # pre-marker failure; run_himalaya re-resolves the same deterministic command.
    try:
        build_himalaya_command(
            ["message", "save", "-o", "plain", "-f", normalized["folder"]], effective_account
        )
    except Exception:
        raise DiagnosticError(
            DIAGNOSTIC_ARGUMENT_INVALID_REASON,
            "The diagnostic command could not be constructed; no process was started.",
        ) from None

    # Consume the one-shot attempt *before* the first (and only) child process.
    resolve_diagnostic_dir(resolved_data_dir)
    marker = consume_attempt_marker(resolved_data_dir, request_hash=request_hash, request=normalized)

    capture = DiagnosticTraceCapture()

    def _record_and_raise(reason_code: str):
        run_result = capture.result()
        outcome, mapped_reason = _outcome_for_reason(reason_code)
        evidence = _build_evidence(
            request=normalized,
            request_hash=request_hash,
            receipt=receipt_info,
            outcome=outcome,
            run_result=run_result,
            reason_code=mapped_reason,
        )
        _write_evidence(resolved_data_dir, request_hash, evidence)
        if outcome == "unknown":
            raise DiagnosticError(DIAGNOSTIC_UNKNOWN_OUTCOME_REASON, _MESSAGE_UNKNOWN) from None
        raise DiagnosticError(DIAGNOSTIC_FAILED_REASON, _MESSAGE_FAILED) from None

    try:
        run_himalaya(
            ["message", "save", "-o", "plain", "-f", normalized["folder"]],
            account=effective_account,
            timeout=effective_timeout,
            max_retries=1,
            stdin_bytes=stdin_payload,
            diagnostic_capture=capture,
        )
    except Exception as exc:  # noqa: BLE001 - classified fail-closed boundary
        reason_code = getattr(exc, "reason_code", None)
        if reason_code is None:
            # An unexpected launch/communication failure: transport, unknown.
            reason_code = "himalaya_launch_failed"
        _record_and_raise(str(reason_code))

    run_result = capture.result() or {}
    if run_result.get("over_budget") or run_result.get("timed_out") or run_result.get("reader_error"):
        _record_and_raise("himalaya_diagnostic_overflow")
    if run_result.get("exit_code") is None:
        _record_and_raise("himalaya_outcome_unknown")
    if run_result.get("exit_code") != 0:
        _record_and_raise("himalaya_command_failed")

    evidence = _build_evidence(
        request=normalized,
        request_hash=request_hash,
        receipt=receipt_info,
        outcome="appended",
        run_result=run_result,
        reason_code=None,
    )
    _write_evidence(resolved_data_dir, request_hash, evidence)

    return {
        "folder": normalized["folder"],
        "subject": draft["subject"],
        "to": draft["to"],
        "date": date_header,
        "message_id": normalized_id,
        "appended": True,
        "verified": False,
        "diagnostics": {
            "request_hash": request_hash,
            "attempt_marker": marker.get("phase"),
            "safe_event_count": len((run_result.get("stderr") or {}).get("safe_events", [])),
        },
    }


__all__ = [
    "BoundedTraceParser",
    "DIAGNOSTIC_ALLOWED_TARGETS",
    "DIAGNOSTIC_APPROVAL_DRIFT_REASON",
    "DIAGNOSTIC_APPROVAL_INVALID_REASON",
    "DIAGNOSTIC_ARGUMENT_INVALID_REASON",
    "DIAGNOSTIC_BINDING_REASON",
    "DIAGNOSTIC_DIRNAME",
    "DIAGNOSTIC_DISABLED_REASON",
    "DIAGNOSTIC_EVIDENCE_KIND",
    "DIAGNOSTIC_EVIDENCE_SUBDIR",
    "DIAGNOSTIC_FAILED_REASON",
    "DIAGNOSTIC_LOCK_REASON",
    "DIAGNOSTIC_MAX_SAFE_EVENTS",
    "DIAGNOSTIC_OPERATION_SAVE_DRAFT",
    "DIAGNOSTIC_OPERATOR_LABEL",
    "DIAGNOSTIC_RECEIPT_KIND",
    "DIAGNOSTIC_REPLAY_REASON",
    "DIAGNOSTIC_REQUEST_KIND",
    "DIAGNOSTIC_SAFE_EVENT_KINDS",
    "DIAGNOSTIC_SCHEMA_VERSION",
    "DIAGNOSTIC_STDERR_CAP_BYTES",
    "DIAGNOSTIC_STDOUT_CAP_BYTES",
    "DIAGNOSTIC_TRACING_FILTER",
    "DIAGNOSTIC_TRANSPORT_REASON",
    "DIAGNOSTIC_UNKNOWN_OUTCOME_REASON",
    "DIAGNOSTIC_WORKSPACE_LOCK_ROOT_ENV",
    "DiagnosticApprovalError",
    "DiagnosticBindingError",
    "DiagnosticError",
    "DiagnosticLockError",
    "DiagnosticReplayError",
    "DiagnosticRequestError",
    "DiagnosticTraceCapture",
    "EVENT_RECTIFIED_CONTINUATION",
    "EVENT_STREAM_MORE_INPUT",
    "EVENT_STREAM_READ",
    "EVENT_STREAM_WRITE",
    "MAX_DIAGNOSTIC_TIMEOUT_SECONDS",
    "RECTIFIED_CONTINUATION_MESSAGE",
    "RECTIFIED_CONTINUATION_SIGNATURE",
    "bind_diagnostic_workspace",
    "build_diagnostic_child_environment",
    "canonical_request_sha256",
    "consume_attempt_marker",
    "parse_trace_stream",
    "require_owned_workspace_lock",
    "resolve_diagnostic_dir",
    "run_diagnostic_save_draft",
    "validate_diagnostic_receipt",
    "validate_diagnostic_request",
]
