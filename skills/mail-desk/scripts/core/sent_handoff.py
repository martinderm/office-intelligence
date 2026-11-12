"""Deterministic Sent-Items synthesis handoff, consumption ledger and thread anchors.

``sync-sent`` indexes outgoing mail as flat metadata.  This module owns the
bundle-side contract that lets a consumer synthesis phase pick up *newly
indexed* Sent Items without any content judgement or LLM logic:

* ``data/mail-desk/new-sent-items.json`` -- the bounded, atomically written
  transit handoff of unconsumed new Sent Items (operational evidence).
* ``data/mail-desk/sent-synthesis-ledger.jsonl`` -- the append-only consumption
  ledger.  A handoff item counts as *consumed* iff its normalized Message-ID has
  a ledger line; the ledger is also the dedupe source ("already synthesized")
  and the anchor source for thread follow-ups.  Issue #107 adds the explicit
  ``synthesized``/``no_evidence`` outcome and the ``legacy_import`` provenance on
  the same schema-1 line shape.
* Pure helpers for the thread key, the thread anchor lookup and the consumer
  view ``load_pending_handoff`` (unconsumed items, anchors re-resolved against the
  current ledger, ancestors first).

Crash contract: a *process* crash or kill.  Files are fsynced before the atomic
replace and, where the platform allows it, the directory entry is fsynced too;
sudden power loss on platforms without directory fsync (Windows) is not covered.

The bundle never writes synthesis results; the consumer owns the ledger writes
(``record_synthesized`` or a direct append of the documented line schema).
"""

from __future__ import annotations

import json
import os
import re
import stat
from datetime import datetime
from email.utils import getaddresses
from pathlib import Path
from typing import Any, Iterable, Mapping

from .common import atomic_write_json, normalize_message_id, utc_now_iso

HANDOFF_FILENAME = "new-sent-items.json"
LEDGER_FILENAME = "sent-synthesis-ledger.jsonl"
SENT_INDEX_FILENAME = "sent-index.jsonl"
HANDOFF_SCHEMA_VERSION = 1
LEDGER_SCHEMA_VERSION = 1
HANDOFF_KIND = "mail-desk.new-sent-items"

#: Upper bound of items in one handoff batch.  More new items are committed in
#: deterministic batches (oldest first); the rest stays unindexed and is re-read
#: after consumption.  The bounded file is written before the index append.
MAX_HANDOFF_ITEMS = 200

#: Per-item field bounds (the sent-index entry keeps the full values).  A bounded
#: field is listed in the item's ``truncated_fields`` instead of being cut silently.
MAX_REFERENCES = 50
MAX_RECIPIENTS = 100
MAX_SUBJECT_CHARS = 1000

#: ``handoff.state`` values reported in every sync envelope (success and failure).
HANDOFF_STATES = ("none", "written", "pending", "invalid", "unknown")
STATIC_ALIAS_REASON_CODES = frozenset({
    "sent_index_alias_invalid", "ledger_alias_invalid", "handoff_alias_invalid",
})

_ITEM_STRING_FIELDS = (
    "message_id", "norm_message_id", "date", "subject", "from", "in_reply_to",
    "envelope_id", "folder", "mailbox", "thread_key",
)
_ITEM_LIST_FIELDS = ("to", "cc", "references", "anchor_candidates")
_ITEM_OPTIONAL_FIELDS = ("truncated_fields",)
_ITEM_FIELDS = frozenset((*_ITEM_STRING_FIELDS, *_ITEM_LIST_FIELDS, *_ITEM_OPTIONAL_FIELDS, "anchored_to", "headers_complete"))
_TRUNCATABLE = frozenset(("references", "to", "cc", "subject"))
_MATCH_TYPES = frozenset(("in_reply_to", "references", "thread_key"))

_LEDGER_REQUIRED = ("schema_version", "message_id", "role", "recorded_at")
_LEDGER_FIELDS = frozenset((
    *_LEDGER_REQUIRED, "thread_key", "vorgang_ref", "anchored_to",
    "outcome", "reason_code", "source", "evid_id",
))
_LEDGER_ROLES = frozenset(("primary", "follow_up"))

#: Consumption outcome of a ledger line.  Issue #107 adds the explicit
#: ``no_evidence`` outcome; a schema-1 line without ``outcome`` is a legacy
#: synthesized line and stays valid (see ``ledger_outcome``).
LEDGER_OUTCOMES = ("synthesized", "no_evidence")
LEDGER_OUTCOME_DEFAULT = "synthesized"
#: Provenance of a ledger line.  ``legacy_import`` is provenance, not a third
#: outcome: an imported no-op stays ``no_evidence`` and an imported synthesis
#: stays ``synthesized``.
LEDGER_SOURCES = ("synthesis", "legacy_import")
LEDGER_SOURCE_DEFAULT = "synthesis"

_REASON_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


def is_reference_token(value: Any) -> bool:
    """Whether ``value`` is a bounded, exact opaque token (Vorgang/EVID-ID)."""
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 256
        and value == value.strip()
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
    )


def is_reason_code(value: Any) -> bool:
    """Whether ``value`` is a documented, bounded ``no_evidence`` reason token."""
    return isinstance(value, str) and _REASON_CODE_PATTERN.fullmatch(value) is not None


def ledger_outcome(record: Mapping[str, Any]) -> str:
    """Outcome of one ledger line; absent means the legacy ``synthesized``."""
    outcome = record.get("outcome", LEDGER_OUTCOME_DEFAULT)
    return outcome if isinstance(outcome, str) else LEDGER_OUTCOME_DEFAULT


def ledger_source(record: Mapping[str, Any]) -> str:
    """Provenance of one ledger line; absent means the legacy ``synthesis``."""
    source = record.get("source", LEDGER_SOURCE_DEFAULT)
    return source if isinstance(source, str) else LEDGER_SOURCE_DEFAULT


class SentHandoffError(RuntimeError):
    """Fail-closed refusal; nothing was written to the handoff or the sent-index.

    ``reason_code``: ``handoff_pending`` (unconsumed items would be extended),
    ``handoff_invalid`` (handoff unreadable, inconsistent, oversized or foreign),
    ``handoff_overflow`` (legacy producer refusals; current sync batches instead),
    ``handoff_deferred`` (legacy int date API saw a deferred bounded batch),
    ``ledger_invalid`` (a ledger line violates the documented schema),
    ``ledger_conflict`` (a new append/import/recovery disagrees with durable ledger
    evidence; first-line-wins never hides it),
    ``sent_index_alias_invalid``/``ledger_alias_invalid``/``handoff_alias_invalid``
    (static symlink/hardlink alias on the JSONL target or its ``.torn`` sidecar, or
    the handoff document; a misconfiguration, not a transient rerun).
    ``details["state"]`` is the ``handoff.state``.
    """

    def __init__(self, reason_code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.details = dict(details or {})


class SentLedgerConflict(SentHandoffError):
    """A new ledger operation conflicts with already durable ledger evidence.

    ``reason_code`` is always ``ledger_conflict``.  The conflict is never hidden
    by the first-line-wins read compatibility: new records, imports and recovery
    stop before any append when an existing line disagrees on the operation's
    identity.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("ledger_conflict", message, details)


class SentSyncPartialFailure(RuntimeError):
    """Some date windows failed; the run is fail-closed and wrote nothing.

    ``result`` describes the attempted windows with zero new entries; ``failed_dates``
    names every failing window.  Resolve them and rerun (a newer watermark must never
    jump over an older failed window).
    """

    def __init__(self, message: str, result: dict[str, Any], failed_dates: list[str]) -> None:
        super().__init__(message)
        self.result = result
        self.failed_dates = list(failed_dates)


class SentIndexAppendError(RuntimeError):
    """The sent-index append failed after the handoff was persisted.

    The handoff is durable, but the index may be empty, partially written or
    complete: the on-disk progress is *unknown*.  A rerun re-detects the same
    items and merges idempotently by Message-ID.
    """


class SentHandoffPublishError(RuntimeError):
    """The handoff write failed and may already have replaced the handoff file.

    The atomic replace can publish the new document before a later step fails
    (e.g. a directory fsync/close error).  The caller must report unverified
    progress instead of claiming that nothing was written; a rerun validates the
    published handoff and merges idempotently by Message-ID.
    """


class SentStateError(RuntimeError):
    """The coverage state write failed and may already have been published.

    No handoff/index entry is part of this failure, but the state file may have
    replaced the previous barrier.  Report ``write_state: unknown`` until a rerun
    re-reads and repairs the coverage state.
    """

    write_state = "unknown"


def norm_id(value: Any) -> str:
    """Normalized Message-ID (no brackets, lower case); empty for blank input."""
    return normalize_message_id(str(value or ""))


# ------------------------------------------------------------------ thread helpers


def parse_address_list(value: Any) -> list[str]:
    """Parse a To/Cc header (string or list) into ``Name <addr>`` / ``addr`` strings.

    Uses RFC-aware parsing so display names containing commas stay intact.
    """
    if value is None:
        return []
    raw_values = [str(v) for v in value] if isinstance(value, (list, tuple)) else [str(value)]
    result: list[str] = []
    for name, address in getaddresses(raw_values):
        name, address = name.strip(), address.strip()
        if address:
            result.append(f"{name} <{address}>" if name else address)
        elif name:
            result.append(name)
    return result


def thread_anchor_ids(in_reply_to: Any, references: Iterable[Any], own_message_id: Any = "") -> list[str]:
    """Normalized ancestor Message-IDs, nearest first: ``In-Reply-To``, then ``References`` reversed."""
    own = norm_id(own_message_id)
    ordered = [norm_id(in_reply_to), *(norm_id(ref) for ref in reversed(list(references)))]
    anchors: list[str] = []
    for candidate in ordered:
        if candidate and candidate != own and candidate not in anchors:
            anchors.append(candidate)
    return anchors


def thread_key(in_reply_to: Any, references: Iterable[Any], own_message_id: Any) -> str:
    """Deterministic thread key: the thread root (first Reference), else the parent, else self."""
    refs = [norm_id(ref) for ref in references]
    for candidate in (refs[0] if refs else "", norm_id(in_reply_to), norm_id(own_message_id)):
        if candidate:
            return candidate
    return ""


def _split_references(value: Any) -> list[str]:
    if isinstance(value, str):
        return [part for part in value.split() if part]
    return [str(part) for part in (value or []) if str(part).strip()]


def build_handoff_item(
    entry: dict[str, Any],
    *,
    to: Any,
    cc: Any,
    headers_complete: bool,
    anchored_to: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the bounded handoff item for one freshly built sent-index ``entry``."""
    truncated: list[str] = []

    references = _split_references(entry.get("references"))
    if len(references) > MAX_REFERENCES:
        # Keep the thread root and the nearest ancestors; both drive anchoring.
        references = [references[0], *references[-(MAX_REFERENCES - 1):]]
        truncated.append("references")

    recipients: dict[str, list[str]] = {}
    for name, header in (("to", to), ("cc", cc)):
        parsed = parse_address_list(header)
        if len(parsed) > MAX_RECIPIENTS:
            parsed = parsed[:MAX_RECIPIENTS]
            truncated.append(name)
        recipients[name] = parsed

    subject = str(entry.get("subject", ""))
    if len(subject) > MAX_SUBJECT_CHARS:
        subject = subject[:MAX_SUBJECT_CHARS]
        truncated.append("subject")

    in_reply_to = str(entry.get("in_reply_to", "") or "")
    own = entry.get("message_id", "")
    item: dict[str, Any] = {
        "message_id": str(own),
        "norm_message_id": norm_id(own),
        "date": str(entry.get("at", "")),
        "subject": subject,
        "from": str(entry.get("from", "")),
        "to": recipients["to"],
        "cc": recipients["cc"],
        "in_reply_to": in_reply_to,
        "references": references,
        "envelope_id": str(entry.get("sent_envelope_id", "")),
        "folder": str(entry.get("folder", "")),
        "mailbox": entry.get("mailbox"),
        "thread_key": thread_key(in_reply_to, references, own),
        "anchor_candidates": thread_anchor_ids(in_reply_to, references, own),
        "anchored_to": anchored_to,
        "headers_complete": bool(headers_complete),
    }
    if truncated:
        item["truncated_fields"] = truncated
    return item


# ------------------------------------------------------------------ JSONL I/O


def read_jsonl_lines(path: Path) -> tuple[list[bytes], bytes]:
    """Return ``(complete lines, unterminated tail)`` of a JSONL file as raw bytes.

    Decoding is left to the caller per line, so a crash-torn UTF-8 sequence in the
    tail can never make the whole file unreadable.  Blank lines are dropped.
    """
    if not path.exists():
        return [], b""
    data = path.read_bytes()
    cut = data.rfind(b"\n") + 1
    complete = [line for line in data[:cut].split(b"\n") if line.strip()]
    return complete, data[cut:]


def parse_json_object(raw: bytes) -> dict[str, Any] | None:
    """Strict UTF-8 + JSON object parse of one line; ``None`` when not a valid object."""
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def tolerant_jsonl_records(path: Path) -> list[dict[str, Any]]:
    """Valid JSON-object lines (including a complete unterminated last line); junk is skipped."""
    complete, tail = read_jsonl_lines(path)
    records: list[dict[str, Any]] = []
    for raw in [*complete, *([tail] if tail.strip() else [])]:
        record = parse_json_object(raw)
        if record is not None:
            records.append(record)
    return records


def _file_alias_kind(path: Path) -> str | None:
    """Return ``symlink``/``hardlink``/``unverifiable`` for a static alias, else ``None``."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        # Permission/IO errors must fail closed, never be treated as "no alias".
        return "unverifiable"
    if stat.S_ISLNK(info.st_mode):
        return "symlink"
    if getattr(info, "st_nlink", 1) != 1:
        return "hardlink"
    return None


def _reject_file_alias(path: Path, *, reason_code: str, label: str) -> None:
    """Refuse a static symlink/hardlink alias before any mutation or read-through.

    Static pre-check only: a concurrent filesystem mutator is outside the
    cooperative lease scope (issue #91).
    """
    alias = _file_alias_kind(path)
    if alias is None:
        return
    if alias == "unverifiable":
        message = (
            f"Cannot verify '{path.name}' ({label}) for symlink/hardlink aliases; no "
            "handoff/index/coverage mutation was performed and the path was not used for a "
            "write. This is a filesystem/permission misconfiguration, not a transient failure: "
            "repair permissions or quarantine the file manually; a rerun without fixing it "
            "fails the same way. No autonomous deletion."
        )
        reason = "alias_unverifiable"
    else:
        message = (
            f"Static {alias} alias detected for '{path.name}' ({label}); no handoff/index/coverage "
            "mutation was performed and the alias was not used for a write. This is a filesystem "
            "misconfiguration, not a transient failure: repair or quarantine the aliased file "
            "manually; a rerun without fixing it fails the same way. No autonomous deletion."
        )
        reason = f"static_{alias}_alias"
    raise SentHandoffError(
        reason_code,
        message,
        {
            "reason": reason,
            "alias_file": path.name,
            "alias_kind": alias,
            "alias_label": label,
        },
    )


def reject_static_aliases(path: Path, *, reason_code: str, label: str) -> None:
    """Reject static aliases on a JSONL file and its ``.torn`` append sidecar."""
    _reject_file_alias(path, reason_code=reason_code, label=label)
    _reject_file_alias(
        path.with_name(path.name + ".torn"),
        reason_code=reason_code,
        label=f"{label} torn sidecar",
    )


def assert_append_aliases(data_dir: Path) -> None:
    """Preflight index, ledger and handoff (each with sidecar) before any read/write."""
    reject_static_aliases(
        data_dir / SENT_INDEX_FILENAME,
        reason_code="sent_index_alias_invalid",
        label="sent-index",
    )
    reject_static_aliases(
        data_dir / LEDGER_FILENAME,
        reason_code="ledger_alias_invalid",
        label="ledger",
    )
    # Keep the handoff preflight identical to its readers: the JSON document and
    # the defensive .torn sidecar are both statically checked.
    reject_static_aliases(
        data_dir / HANDOFF_FILENAME,
        reason_code="handoff_alias_invalid",
        label="handoff",
    )


def _repair_torn_tail(path: Path, *, reason_code: str, label: str) -> None:
    """Make the file end on a newline without destroying valid evidence.

    A complete last record merely missing its newline gets the newline.  An
    unparsable fragment (e.g. a record cut inside a multi-byte character) is moved
    to the append-only sidecar ``<name>.torn`` (fsynced) before it is cut off, so
    the next append can never glue two records into one line. Both the target and
    the sidecar are statically alias-checked before any mutation.
    """
    _reject_file_alias(path, reason_code=reason_code, label=label)
    sidecar_path = path.with_name(path.name + ".torn")
    _reject_file_alias(
        sidecar_path, reason_code=reason_code, label=f"{label} torn sidecar",
    )
    if not path.exists() or path.stat().st_size == 0:
        return
    data = path.read_bytes()
    if data.endswith(b"\n"):
        return
    cut = data.rfind(b"\n") + 1
    tail = data[cut:]
    if parse_json_object(tail) is not None:
        with path.open("ab") as handle:
            handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        return
    with sidecar_path.open("ab") as sidecar:
        sidecar.write(tail + b"\n")
        sidecar.flush()
        os.fsync(sidecar.fileno())
    with path.open("r+b") as handle:
        handle.truncate(cut)
        handle.flush()
        os.fsync(handle.fileno())


def durable_append_jsonl(
    path: Path,
    entries: Iterable[dict[str, Any]],
    *,
    alias_reason_code: str,
    alias_label: str,
) -> int:
    """Append JSON lines (LF) and fsync; repair a torn final line first (see ``_repair_torn_tail``)."""
    lines = [json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries]
    if not lines:
        return 0
    _reject_file_alias(path, reason_code=alias_reason_code, label=alias_label)
    path.parent.mkdir(parents=True, exist_ok=True)
    _repair_torn_tail(path, reason_code=alias_reason_code, label=alias_label)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.writelines(lines)
        handle.flush()
        os.fsync(handle.fileno())
    return len(lines)


def _fsync_directory(directory: Path) -> None:
    """Best-effort directory fsync after an atomic replace; a no-op where unsupported (Windows)."""
    try:
        descriptor = os.open(str(directory), os.O_RDONLY)
    except (OSError, AttributeError):
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        # A close failure after a successful atomic replace must not surface as a
        # failed handoff: publication already happened and the caller reports
        # unverified progress. Best-effort closes keep this helper optional.
        try:
            os.close(descriptor)
        except OSError:
            pass


# ------------------------------------------------------------------ ledger


def _iso_timestamp_ok(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _ledger_problem(record: dict[str, Any]) -> str | None:
    unknown = sorted(set(record) - _LEDGER_FIELDS)
    if unknown:
        return f"unknown_field:{unknown[0]}"
    for name in _LEDGER_REQUIRED:
        if name not in record:
            return f"missing_field:{name}"
    if type(record["schema_version"]) is not int or record["schema_version"] != LEDGER_SCHEMA_VERSION:
        return "unsupported_schema_version"
    message_id = record["message_id"]
    if not isinstance(message_id, str) or not message_id or message_id != norm_id(message_id):
        return "message_id_not_normalized"
    if not isinstance(record["role"], str) or record["role"] not in _LEDGER_ROLES:
        return "invalid_role"
    if not _iso_timestamp_ok(record["recorded_at"]):
        return "invalid_recorded_at"
    for name in ("thread_key", "vorgang_ref", "anchored_to"):
        if record.get(name) is not None and not isinstance(record[name], str):
            return f"{name}_not_string"
    outcome = record.get("outcome", LEDGER_OUTCOME_DEFAULT)
    if not isinstance(outcome, str) or outcome not in LEDGER_OUTCOMES:
        return "invalid_outcome"
    source = record.get("source", LEDGER_SOURCE_DEFAULT)
    if not isinstance(source, str) or source not in LEDGER_SOURCES:
        return "invalid_source"
    reason_code = record.get("reason_code")
    if reason_code is not None and not is_reason_code(reason_code):
        return "reason_code_invalid"
    evid_id = record.get("evid_id")
    if evid_id is not None and not is_reference_token(evid_id):
        return "evid_id_invalid"
    if outcome == "no_evidence":
        if not is_reason_code(reason_code):
            return "no_evidence_without_reason_code"
        if evid_id is not None:
            return "no_evidence_with_evid_id"
        if record.get("vorgang_ref") is not None:
            return "no_evidence_with_vorgang_ref"
        if record.get("anchored_to") is not None:
            return "no_evidence_with_anchor"
        if record["role"] != "primary":
            return "no_evidence_role_not_primary"
    elif reason_code is not None:
        return "synthesized_with_reason_code"
    if source == "legacy_import":
        # An imported synthesis must carry the full promotion triad; otherwise
        # the import would invent either a Vorgang or an EVID anchor.
        if outcome == "synthesized":
            if not is_reference_token(record.get("vorgang_ref")):
                return "legacy_import_missing_vorgang_ref"
            if not is_reference_token(evid_id):
                return "legacy_import_missing_evid_id"
    anchored = record.get("anchored_to")
    if record["role"] == "follow_up" and not anchored:
        return "follow_up_without_anchor"
    if record["role"] == "primary" and anchored:
        return "primary_with_anchor"
    return None


def load_ledger(data_dir: Path) -> dict[str, Any]:
    """Load and fully validate the consumption ledger; fail closed on any invalid line.

    Returns ``{"by_message_id", "by_thread_key", "all_by_message_id", "torn_tail"}``.
    Every complete line must satisfy the documented schema, else
    ``SentHandoffError("ledger_invalid")`` names the first offending line numbers.
    ``by_message_id`` keeps the documented first-line-wins view; the new
    ``all_by_message_id`` exposes *every* valid line per Message-ID so migration and
    recovery can detect a conflict that first-wins would otherwise hide.  An
    unterminated, unparsable *last* line is a crash artifact of an interrupted
    append: it is ignored (``torn_tail``), preserved on disk and moved to
    ``<ledger>.torn`` by the next append.
    """
    by_message_id: dict[str, dict[str, Any]] = {}
    by_thread_key: dict[str, dict[str, Any]] = {}
    all_by_message_id: dict[str, list[dict[str, Any]]] = {}
    path = data_dir / LEDGER_FILENAME
    reject_static_aliases(path, reason_code="ledger_alias_invalid", label="ledger")
    complete, tail = read_jsonl_lines(path)
    problems: list[tuple[int, str]] = []
    for number, raw in enumerate(complete, start=1):
        record = parse_json_object(raw)
        problem = "not_a_json_object" if record is None else _ledger_problem(record)
        if problem is not None:
            problems.append((number, problem))
            continue
        _index_ledger_record(by_message_id, by_thread_key, record, all_by_message_id)
    if problems:
        shown = ", ".join(f"line {number}: {reason}" for number, reason in problems[:5])
        raise SentHandoffError(
            "ledger_invalid",
            f"Ledger '{LEDGER_FILENAME}' violates its schema ({len(problems)} invalid line(s); {shown}). "
            "Nothing was handed off or indexed; repair the ledger and rerun.",
            {"invalid_lines": [number for number, _ in problems[:20]], "state": "invalid"},
        )
    torn_tail = False
    if tail.strip():
        record = parse_json_object(tail)
        if record is not None and _ledger_problem(record) is None:
            _index_ledger_record(by_message_id, by_thread_key, record, all_by_message_id)
        elif record is not None:
            raise SentHandoffError(
                "ledger_invalid",
                f"Ledger '{LEDGER_FILENAME}' ends in a complete but schema-violating record.",
                {"invalid_lines": [len(complete) + 1], "state": "invalid"},
            )
        else:
            torn_tail = True
    return {
        "by_message_id": by_message_id,
        "by_thread_key": by_thread_key,
        "all_by_message_id": all_by_message_id,
        "torn_tail": torn_tail,
    }


def _index_ledger_record(
    by_message_id: dict[str, dict[str, Any]],
    by_thread_key: dict[str, dict[str, Any]],
    record: dict[str, Any],
    all_by_message_id: dict[str, list[dict[str, Any]]] | None = None,
) -> None:
    """Index one validated ledger record; the first line for a Message-ID wins.

    A duplicate line must not introduce a different thread anchor: the first
    valid line is authoritative for **every** index (``by_message_id`` and
    ``by_thread_key``), so a later conflicting duplicate is ignored as a whole
    instead of re-anchoring replies to its (different) Vorgang.  ``all_by_message_id``
    keeps every line so new operations can detect exactly that conflict instead of
    silently inheriting first-wins.  A ``no_evidence`` line is consumed state but
    never a thread anchor: it carries no Vorgang and no anchored target.
    """
    message_id = record["message_id"]
    if all_by_message_id is not None:
        all_by_message_id.setdefault(message_id, []).append(record)
    if message_id in by_message_id:
        return
    by_message_id[message_id] = record
    if ledger_outcome(record) != "synthesized":
        return
    key = norm_id(record.get("thread_key"))
    if key:
        existing = by_thread_key.get(key)
        # Different messages of one thread may refine the anchor: prefer the first
        # record that carries a Vorgang reference. A duplicate of the *same*
        # Message-ID never reaches this point (first line wins above).
        if existing is None or (not existing.get("vorgang_ref") and record.get("vorgang_ref")):
            by_thread_key[key] = record


def resolve_thread_anchor(item: dict[str, Any], ledger: dict[str, Any]) -> dict[str, Any] | None:
    """Anchor a sent item to an already synthesized ancestor, or ``None``.

    Match order: ``In-Reply-To``, ``References`` (nearest first), then the shared
    thread key.  The result names the ledger's ``vorgang_ref`` (may be ``None`` when
    the consumer recorded none) so the follow-up evidence attaches to the same Vorgang.
    Always pass the *current* ledger: an anchor stored in a handoff is a snapshot.
    """
    by_message_id = ledger.get("by_message_id", {})
    in_reply_to = norm_id(item.get("in_reply_to"))
    own = norm_id(item.get("norm_message_id") or item.get("message_id"))
    for candidate in item.get("anchor_candidates") or thread_anchor_ids(
        item.get("in_reply_to"), _split_references(item.get("references")), own
    ):
        record = by_message_id.get(candidate)
        if record is not None and ledger_outcome(record) == "synthesized":
            return {
                "vorgang_ref": record.get("vorgang_ref"),
                "matched_message_id": candidate,
                "match_type": "in_reply_to" if candidate == in_reply_to else "references",
            }
    key = norm_id(item.get("thread_key"))
    record = ledger.get("by_thread_key", {}).get(key) if key else None
    if record is not None and record["message_id"] != own and ledger_outcome(record) == "synthesized":
        return {
            "vorgang_ref": record.get("vorgang_ref"),
            "matched_message_id": record["message_id"],
            "match_type": "thread_key",
        }
    return None


def is_synthesized(item: dict[str, Any], ledger: dict[str, Any]) -> bool:
    """Whether the item's Message-ID is consumed by a ledger line (any outcome).

    The historical name is kept for callers; ``synthesized`` and ``no_evidence``
    both consume the item, so neither is handed off again.
    """
    return norm_id(item.get("norm_message_id") or item.get("message_id")) in ledger.get("by_message_id", {})


def _record_identity(record: Mapping[str, Any]) -> tuple[Any, Any, Any, Any]:
    """Semantic identity used to detect a conflict before a new append."""
    return (
        ledger_outcome(record),
        record.get("vorgang_ref"),
        record.get("evid_id"),
        record.get("reason_code"),
    )


def _raise_ledger_conflict(
    message_id: str,
    requested: tuple[Any, Any, Any, Any],
    existing: list[dict[str, Any]],
) -> None:
    raise SentLedgerConflict(
        f"Ledger '{LEDGER_FILENAME}' already holds {len(existing)} line(s) for Message-ID "
        f"'{message_id}' that conflict with the requested outcome/provenance; nothing was "
        "appended. First-line-wins is a read compatibility only and never resolves a new "
        "conflict. Recovery: reconcile the durable promotion evidence manually; do not "
        "overwrite or delete ledger lines.",
        {
            "message_id": message_id,
            "existing_count": len(existing),
            "requested_outcome": requested[0],
            "existing_outcomes": sorted({str(ledger_outcome(record)) for record in existing}),
            "state": "conflict",
        },
    )


def _identity_compatible(
    existing: tuple[Any, Any, Any, Any],
    requested: tuple[Any, Any, Any, Any],
) -> bool:
    """Whether an existing identity satisfies a requested operation.

    ``outcome`` and ``reason_code`` must match exactly.  An omitted optional
    ``vorgang_ref``/``evid_id`` (``None`` in the request) is a wildcard: an old
    caller that repeats ``record_synthesized`` without the optional arguments
    stays idempotent even when the durable line already carries a Vorgang/EVID.
    An explicitly supplied value must match exactly, including against a missing
    existing value; explicit contradictions remain conflicts.
    """
    existing_outcome, existing_ref, existing_evid, existing_reason = existing
    requested_outcome, requested_ref, requested_evid, requested_reason = requested
    if existing_outcome != requested_outcome or existing_reason != requested_reason:
        return False
    if requested_ref is not None and existing_ref != requested_ref:
        return False
    if requested_evid is not None and existing_evid != requested_evid:
        return False
    return True


def _check_existing_identity(
    all_by_message_id: Mapping[str, list[dict[str, Any]]],
    message_id: str,
    requested: tuple[Any, Any, Any, Any],
    *,
    allow_unspecified: bool = False,
) -> bool:
    """Return True when a matching line exists; raise on any disagreement.

    ``allow_unspecified`` is used by runtime appends: omitted optional fields are
    idempotent wildcards.  Imports use the strict equality form because an
    imported line always carries its complete identity.
    """
    existing = list(all_by_message_id.get(message_id, []))
    if not existing:
        return False
    identities = {_record_identity(record) for record in existing}
    if len(identities) > 1:
        _raise_ledger_conflict(message_id, requested, existing)
    if allow_unspecified:
        if all(_identity_compatible(identity, requested) for identity in identities):
            return True
    elif identities == {requested}:
        return True
    _raise_ledger_conflict(message_id, requested, existing)


def record_outcome(
    data_dir: Path,
    items: Iterable[dict[str, Any]],
    *,
    outcome: str = "synthesized",
    reason_code: str | None = None,
    vorgang_ref: str | None = None,
    evid_id: str | None = None,
) -> int:
    """Append explicit consumption outcomes (append-only, idempotent, conflict-aware).

    ``synthesized`` resolves the thread anchor exactly like ``record_synthesized``
    and may carry an ``EVID-ID``.  ``no_evidence`` records a durable, reasoned
    no-op: it consumes the item but never creates a Vorgang, an EVID anchor or a
    thread anchor.  Existing lines are compared by outcome/Vorgang/EVID/reason;
    a disagreement is a conflict and nothing is appended (first-line-wins never
    conceals a new conflict).
    """
    if outcome not in LEDGER_OUTCOMES:
        raise ValueError(f"outcome must be one of {LEDGER_OUTCOMES}")
    if outcome == "no_evidence":
        if vorgang_ref is not None:
            raise ValueError("no_evidence must not carry a Vorgang reference.")
        if evid_id is not None:
            raise ValueError("no_evidence must not carry an EVID anchor.")
        if not is_reason_code(reason_code):
            raise ValueError("no_evidence requires a documented reason_code.")
    else:
        if reason_code is not None:
            raise ValueError("synthesized must not carry a no_evidence reason_code.")
        if evid_id is not None and not is_reference_token(evid_id):
            raise ValueError("evid_id must be a bounded exact token.")

    reject_static_aliases(
        data_dir / LEDGER_FILENAME, reason_code="ledger_alias_invalid", label="ledger",
    )
    ledger = load_ledger(data_dir)
    by_message_id = ledger["by_message_id"]
    by_thread_key = ledger["by_thread_key"]
    all_by_message_id = ledger["all_by_message_id"]
    records: list[dict[str, Any]] = []
    for item in items:
        mid = norm_id(item.get("norm_message_id") or item.get("message_id"))
        if not mid:
            raise ValueError("Cannot record a ledger line without a Message-ID.")
        item_reason = item.get("reason_code", reason_code)
        if item_reason is None:
            item_reason = reason_code
        item_evid = item.get("evid_id", evid_id)
        if item_evid is None:
            item_evid = evid_id
        explicit_ref = item.get("vorgang_ref")
        if explicit_ref is None:
            explicit_ref = vorgang_ref
        if outcome == "no_evidence":
            if item.get("vorgang_ref") is not None or item_evid is not None:
                raise ValueError("no_evidence must not carry a Vorgang or EVID anchor.")
            if not is_reason_code(item_reason):
                raise ValueError("no_evidence requires a documented reason_code.")
            ref = None
            anchor = None
            role = "primary"
            recorded_evid = None
            recorded_reason = item_reason
        else:
            if item_reason is not None:
                raise ValueError("synthesized must not carry a no_evidence reason_code.")
            if item_evid is not None and not is_reference_token(item_evid):
                raise ValueError("evid_id must be a bounded exact token.")
            anchor = resolve_thread_anchor(item, ledger)
            # Only a caller-supplied Vorgang is an explicit identity assertion.
            # A Vorgang derived from a later thread anchor is informational for a
            # new line but must not turn an old no-op repeat into a conflict.
            ref = explicit_ref if explicit_ref is not None else (anchor or {}).get("vorgang_ref")
            if ref is not None and not is_reference_token(ref):
                raise ValueError("vorgang_ref must be a bounded exact token.")
            role = "follow_up" if anchor else "primary"
            recorded_evid = item_evid
            recorded_reason = None
        record: dict[str, Any] = {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "message_id": mid,
            "thread_key": norm_id(item.get("thread_key")) or thread_key(
                item.get("in_reply_to"), _split_references(item.get("references")), mid
            ),
            "vorgang_ref": ref,
            "role": role,
            "anchored_to": anchor["matched_message_id"] if anchor else None,
            "recorded_at": utc_now_iso(),
            "outcome": outcome,
            "source": LEDGER_SOURCE_DEFAULT,
        }
        if recorded_reason is not None:
            record["reason_code"] = recorded_reason
        if recorded_evid is not None:
            record["evid_id"] = recorded_evid
        problem = _ledger_problem(record)
        if problem is not None:  # pragma: no cover - the constructor is the schema owner
            raise ValueError(f"Refusing to write an invalid ledger line ({problem}).")
        check_identity = (
            outcome,
            explicit_ref if outcome == "synthesized" else None,
            item_evid if outcome == "synthesized" else None,
            recorded_reason,
        )
        if _check_existing_identity(
            all_by_message_id, mid, check_identity, allow_unspecified=True,
        ):
            continue
        _index_ledger_record(by_message_id, by_thread_key, record, all_by_message_id)
        records.append(record)
    return durable_append_jsonl(
        data_dir / LEDGER_FILENAME, records,
        alias_reason_code="ledger_alias_invalid", alias_label="ledger",
    )


def record_synthesized(
    data_dir: Path,
    items: Iterable[dict[str, Any]],
    *,
    vorgang_ref: str | None = None,
    evid_id: str | None = None,
) -> int:
    """Mark handoff items as consumed (append-only, idempotent); returns lines added.

    Call this only after the item's synthesis result is durably persisted: the
    ledger line is what releases the item from the handoff and from re-promotion.
    The anchor is **re-resolved here** against the current ledger including the
    lines of this very call, so a reply recorded after its ancestor becomes a
    ``follow_up`` of the ancestor's Vorgang even if the handoff snapshot said
    otherwise.  ``vorgang_ref`` precedence: the item's own, then the argument, then
    the anchor's.  ``evid_id`` is the optional durable evidence anchor; it is
    required for legacy-imported synthesized lines and for promotion recovery.
    A repeated call may omit ``vorgang_ref``/``evid_id``: an omitted optional value
    is compatible with an already durable non-null value, while an explicitly
    supplied contradictory value raises ``SentLedgerConflict``.
    """
    return record_outcome(
        data_dir, items, outcome="synthesized", vorgang_ref=vorgang_ref, evid_id=evid_id,
    )


def record_no_evidence(
    data_dir: Path,
    items: Iterable[dict[str, Any]],
    *,
    reason_code: str,
) -> int:
    """Consume items with a durable, reasoned no-evidence outcome.

    The line never carries a Vorgang, an EVID anchor or a thread anchor.  It is
    still a consumption decision: the item leaves the handoff and is not promoted
    again.  ``reason_code`` is a bounded, documented token (see
    ``references/sent-ledger-migration.md``).
    """
    return record_outcome(
        data_dir, items, outcome="no_evidence", reason_code=reason_code,
    )


def append_ledger_records(data_dir: Path, records: Iterable[dict[str, Any]]) -> int:
    """Append already validated, fully formed ledger records (used by import).

    Every record must pass the same schema as a runtime line.  Existing lines are
    compared by outcome/Vorgang/EVID/reason; a disagreement stops before any
    append, while an identical line is an idempotent skip.  The caller owns the
    approval/lease discipline around this low-level writer.
    """
    pending: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("Ledger records must be JSON objects.")
        problem = _ledger_problem(record)
        if problem is not None:
            raise ValueError(f"Refusing to append an invalid ledger record ({problem}).")
        pending.append(record)
    if not pending:
        return 0
    reject_static_aliases(
        data_dir / LEDGER_FILENAME, reason_code="ledger_alias_invalid", label="ledger",
    )
    ledger = load_ledger(data_dir)
    by_message_id = ledger["by_message_id"]
    by_thread_key = ledger["by_thread_key"]
    all_by_message_id = ledger["all_by_message_id"]
    appendable: list[dict[str, Any]] = []
    for record in pending:
        mid = record["message_id"]
        if _check_existing_identity(all_by_message_id, mid, _record_identity(record)):
            continue
        _index_ledger_record(by_message_id, by_thread_key, record, all_by_message_id)
        appendable.append(record)
    return durable_append_jsonl(
        data_dir / LEDGER_FILENAME, appendable,
        alias_reason_code="ledger_alias_invalid", alias_label="ledger",
    )


# ------------------------------------------------------------------ handoff file


def _anchored_to_problem(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"vorgang_ref", "matched_message_id", "match_type"}:
        return "anchored_to_malformed"
    if value["vorgang_ref"] is not None and not isinstance(value["vorgang_ref"], str):
        return "anchored_to_malformed"
    if not isinstance(value["matched_message_id"], str) or not value["matched_message_id"]:
        return "anchored_to_malformed"
    if not isinstance(value["match_type"], str) or value["match_type"] not in _MATCH_TYPES:
        return "anchored_to_malformed"
    return None


def _item_problem(item: dict[str, Any]) -> str | None:
    unknown = sorted(set(item) - _ITEM_FIELDS)
    if unknown:
        return f"unknown_field:{unknown[0]}"
    for name in (*_ITEM_STRING_FIELDS, *_ITEM_LIST_FIELDS, "headers_complete", "anchored_to"):
        if name not in item:
            return f"missing_field:{name}"
    for name in _ITEM_STRING_FIELDS:
        if not isinstance(item[name], str):
            return f"{name}_not_string"
    for name in _ITEM_LIST_FIELDS:
        if not isinstance(item[name], list) or not all(isinstance(entry, str) for entry in item[name]):
            return f"{name}_not_string_list"
    if not isinstance(item["headers_complete"], bool):
        return "headers_complete_not_bool"
    mid = norm_id(item["message_id"])
    if not mid or item["norm_message_id"] != mid:
        return "identity_mismatch"
    if not item["mailbox"].strip():
        return "mailbox_missing"
    if item["thread_key"] != thread_key(item["in_reply_to"], item["references"], item["message_id"]):
        return "thread_key_mismatch"
    if item["anchor_candidates"] != thread_anchor_ids(item["in_reply_to"], item["references"], item["message_id"]):
        return "anchor_candidates_mismatch"
    truncated = item.get("truncated_fields", [])
    if (
        not isinstance(truncated, list)
        or not all(isinstance(field, str) for field in truncated)
        or not set(truncated) <= _TRUNCATABLE
    ):
        return "truncated_fields_malformed"
    # Documented field bounds are enforced on read as well: a handoff written by
    # any other producer must never smuggle oversized fields into the consumer.
    if len(item["subject"]) > MAX_SUBJECT_CHARS:
        return "subject_too_long"
    if len(item["references"]) > MAX_REFERENCES:
        return "references_too_long"
    for name in ("to", "cc"):
        if len(item[name]) > MAX_RECIPIENTS:
            return f"{name}_too_many"
    return _anchored_to_problem(item["anchored_to"])


def read_handoff(data_dir: Path) -> list[dict[str, Any]]:
    """Read the handoff items; ``[]`` when absent.  Fully validated, fail closed.

    Document shape, count, bound, every item field and the identity consistency
    (normalized Message-ID, thread key, anchor candidates, no duplicates) are
    checked; the canonical item bound (``MAX_HANDOFF_ITEMS``) and the documented
    per-item field bounds apply regardless of the file's own ``max_items``.  Any
    violation raises ``SentHandoffError("handoff_invalid")`` and the file is never
    consumed or rewritten.
    """
    path = data_dir / HANDOFF_FILENAME
    reject_static_aliases(
        path, reason_code="handoff_alias_invalid", label="handoff",
    )
    if not path.exists():
        return []

    def invalid(reason: str) -> SentHandoffError:
        return SentHandoffError(
            "handoff_invalid",
            f"Existing sent handoff '{HANDOFF_FILENAME}' is not a valid handoff document ({reason}); "
            "it was left untouched and nothing was indexed.",
            {"reason": reason, "state": "invalid"},
        )

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise invalid("json_too_deep" if isinstance(error, RecursionError) else type(error).__name__) from error
    if (
        not isinstance(document, dict)
        or type(document.get("schema_version")) is not int
        or document.get("schema_version") != HANDOFF_SCHEMA_VERSION
    ):
        raise invalid("unsupported_schema")
    if document.get("kind") != HANDOFF_KIND or not _iso_timestamp_ok(document.get("generated_at")):
        raise invalid("document_header_malformed")
    max_items, count, items = document.get("max_items"), document.get("count"), document.get("items")
    # The canonical bound is authoritative; a higher file-local ``max_items``
    # must never widen what this reader accepts.
    if (
        not isinstance(max_items, int)
        or isinstance(max_items, bool)
        or not 1 <= max_items <= MAX_HANDOFF_ITEMS
    ):
        raise invalid("max_items_malformed")
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise invalid("items_not_a_list_of_objects")
    if type(count) is not int or count != len(items) or len(items) > max_items or len(items) > MAX_HANDOFF_ITEMS:
        raise invalid("count_or_bound_mismatch")
    seen: set[str] = set()
    for index, item in enumerate(items):
        problem = _item_problem(item)
        if problem is None and item["norm_message_id"] in seen:
            problem = "duplicate_message_id"
        if problem is not None:
            raise invalid(f"item[{index}]: {problem}")
        seen.add(item["norm_message_id"])
    return items


def write_handoff(data_dir: Path, items: list[dict[str, Any]]) -> None:
    """Atomically replace the handoff document (never called with more than the bound).

    Only callers that hold no unconsumed item may replace the file; see
    ``sent_indexer._commit_sent_candidates``.
    """
    if len(items) > MAX_HANDOFF_ITEMS:
        raise SentHandoffError(
            "handoff_invalid",
            f"Refusing to write {len(items)} handoff items (bound {MAX_HANDOFF_ITEMS}).",
            {"state": "invalid"},
        )
    atomic_write_json(
        data_dir / HANDOFF_FILENAME,
        {
            "schema_version": HANDOFF_SCHEMA_VERSION,
            "kind": HANDOFF_KIND,
            "generated_at": utc_now_iso(),
            "max_items": MAX_HANDOFF_ITEMS,
            "count": len(items),
            "items": items,
        },
    )
    _fsync_directory(data_dir)


def unconsumed(items: list[dict[str, Any]], ledger: dict[str, Any]) -> list[dict[str, Any]]:
    """Handoff items whose Message-ID has no ledger line (file order)."""
    consumed = ledger["by_message_id"]
    return [item for item in items if item["norm_message_id"] not in consumed]


def load_pending_handoff(data_dir: Path) -> dict[str, Any]:
    """Consumer view: the unconsumed items, re-anchored against the *current* ledger.

    Read-only.  Each returned item carries a fresh ``anchored_to`` and ``waits_for``
    (pending ancestors from the same handoff, which must be promoted and recorded
    first); items are ordered ancestors first, otherwise in handoff order.  Consume
    loop: take the first item, promote it, ``record_synthesized`` it, call this
    again -- replies then resolve against their just-recorded ancestors.
    Raises ``SentHandoffError`` for an invalid or aliased handoff/ledger.
    """
    reject_static_aliases(
        data_dir / LEDGER_FILENAME, reason_code="ledger_alias_invalid", label="ledger",
    )
    reject_static_aliases(
        data_dir / HANDOFF_FILENAME, reason_code="handoff_alias_invalid", label="handoff",
    )
    ledger = load_ledger(data_dir)
    items = read_handoff(data_dir)
    pending = unconsumed(items, ledger)
    pending_ids = {item["norm_message_id"] for item in pending}
    resolved = [
        {
            **item,
            "anchored_to": resolve_thread_anchor(item, ledger),
            "waits_for": [c for c in item["anchor_candidates"] if c in pending_ids],
        }
        for item in pending
    ]
    ordered: list[dict[str, Any]] = []
    placed: set[str] = set()
    remaining = list(resolved)
    while remaining:
        for candidate in remaining:
            if all(wait in placed for wait in candidate["waits_for"]):
                ordered.append(candidate)
                placed.add(candidate["norm_message_id"])
                remaining.remove(candidate)
                break
        else:  # ancestry cycle in untrusted headers: keep handoff order for the rest
            ordered.extend(remaining)
            break
    return {"items": ordered, "pending_count": len(ordered), "consumed_ignored": len(items) - len(pending)}


def handoff_allows_noop(handoff: Mapping[str, Any] | None) -> bool:
    """True only for a known-empty handoff; uncertainty always blocks the phase.

    ``unknown``/``invalid`` or a null/non-zero ``pending_count`` forbids both the
    synthesis start and the ``inbox_zero`` no-op until a human fixes the cause.
    """
    if not isinstance(handoff, Mapping):
        return False
    state = handoff.get("state")
    pending = handoff.get("pending_count")
    if state not in HANDOFF_STATES or state in {"unknown", "invalid"}:
        return False
    if type(pending) is not int or pending != 0:
        return False
    return state in {"none", "written"}


def peek_handoff_state(data_dir: Path) -> dict[str, Any]:
    """Read-only ``{"state", "pending_count"}`` for error envelopes; never raises.

    A statically aliased ledger is never read: the state is explicitly ``unknown``
    instead of a potentially false ``none``/``0`` from a foreign file.
    """
    try:
        reject_static_aliases(
            data_dir / LEDGER_FILENAME, reason_code="ledger_alias_invalid", label="ledger",
        )
        reject_static_aliases(
            data_dir / HANDOFF_FILENAME, reason_code="handoff_alias_invalid", label="handoff",
        )
    except (SentHandoffError, OSError):
        return {"state": "unknown", "pending_count": None}
    try:
        pending = len(unconsumed(read_handoff(data_dir), load_ledger(data_dir)))
    except Exception:  # noqa: BLE001 - diagnostics must never mask the real failure
        return {"state": "invalid", "pending_count": None}
    return {"state": "pending" if pending else "none", "pending_count": pending}
