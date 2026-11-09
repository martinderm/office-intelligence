"""Deterministic Sent-Items synthesis handoff, consumption ledger and thread anchors.

``sync-sent`` indexes outgoing mail as flat metadata.  This module owns the
bundle-side contract that lets a consumer synthesis phase pick up *newly
indexed* Sent Items without any content judgement or LLM logic:

* ``data/mail-desk/new-sent-items.json`` -- the bounded, atomically written
  transit handoff of unconsumed new Sent Items (operational evidence).
* ``data/mail-desk/sent-synthesis-ledger.jsonl`` -- the append-only consumption
  ledger.  A handoff item counts as *consumed* iff its normalized Message-ID has
  a ledger line; the ledger is also the dedupe source ("already synthesized")
  and the anchor source for thread follow-ups.
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
import stat
from datetime import datetime
from email.utils import getaddresses
from pathlib import Path
from typing import Any, Iterable

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
HANDOFF_STATES = ("none", "written", "pending", "invalid")

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
_LEDGER_FIELDS = frozenset((*_LEDGER_REQUIRED, "thread_key", "vorgang_ref", "anchored_to"))
_LEDGER_ROLES = frozenset(("primary", "follow_up"))


class SentHandoffError(RuntimeError):
    """Fail-closed refusal; nothing was written to the handoff or the sent-index.

    ``reason_code``: ``handoff_pending`` (unconsumed items would be extended),
    ``handoff_invalid`` (handoff unreadable, inconsistent, oversized or foreign),
    ``handoff_overflow`` (legacy producer refusals; current sync batches instead),
    ``handoff_deferred`` (legacy int date API saw a deferred bounded batch),
    ``ledger_invalid`` (a ledger line violates the documented schema),
    ``sent_index_alias_invalid``/``ledger_alias_invalid`` (static symlink/hardlink
    alias on the JSONL target or its ``.torn`` sidecar; a misconfiguration, not a
    transient rerun).
    ``details["state"]`` is the ``handoff.state``.
    """

    def __init__(self, reason_code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.details = dict(details or {})


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
    """Return ``symlink``/``hardlink`` for a static alias, else ``None``."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
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
    raise SentHandoffError(
        reason_code,
        f"Static {alias} alias detected for '{path.name}' ({label}); nothing was written or "
        "read through it. This is a filesystem misconfiguration, not a transient failure: "
        "repair or quarantine the aliased file manually; a rerun without fixing it fails the "
        "same way. No autonomous deletion.",
        {
            "reason": f"static_{alias}_alias",
            "file": path.name,
            "alias": alias,
            "label": label,
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
    """Preflight both append targets and their sidecars before any handoff/index write."""
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
    anchored = record.get("anchored_to")
    if record["role"] == "follow_up" and not anchored:
        return "follow_up_without_anchor"
    if record["role"] == "primary" and anchored:
        return "primary_with_anchor"
    return None


def load_ledger(data_dir: Path) -> dict[str, Any]:
    """Load and fully validate the consumption ledger; fail closed on any invalid line.

    Returns ``{"by_message_id", "by_thread_key", "torn_tail"}``.  Every complete line
    must satisfy the documented schema, else ``SentHandoffError("ledger_invalid")``
    names the first offending line numbers.  An unterminated, unparsable *last* line
    is a crash artifact of an interrupted append: it is ignored (``torn_tail``),
    preserved on disk and moved to ``<ledger>.torn`` by the next append.
    """
    by_message_id: dict[str, dict[str, Any]] = {}
    by_thread_key: dict[str, dict[str, Any]] = {}
    path = data_dir / LEDGER_FILENAME
    complete, tail = read_jsonl_lines(path)
    problems: list[tuple[int, str]] = []
    for number, raw in enumerate(complete, start=1):
        record = parse_json_object(raw)
        problem = "not_a_json_object" if record is None else _ledger_problem(record)
        if problem is not None:
            problems.append((number, problem))
            continue
        _index_ledger_record(by_message_id, by_thread_key, record)
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
            _index_ledger_record(by_message_id, by_thread_key, record)
        elif record is not None:
            raise SentHandoffError(
                "ledger_invalid",
                f"Ledger '{LEDGER_FILENAME}' ends in a complete but schema-violating record.",
                {"invalid_lines": [len(complete) + 1], "state": "invalid"},
            )
        else:
            torn_tail = True
    return {"by_message_id": by_message_id, "by_thread_key": by_thread_key, "torn_tail": torn_tail}


def _index_ledger_record(
    by_message_id: dict[str, dict[str, Any]],
    by_thread_key: dict[str, dict[str, Any]],
    record: dict[str, Any],
) -> None:
    """Index one validated ledger record; the first line for a Message-ID wins.

    A duplicate line must not introduce a different thread anchor: the first
    valid line is authoritative for **every** index (``by_message_id`` and
    ``by_thread_key``), so a later conflicting duplicate is ignored as a whole
    instead of re-anchoring replies to its (different) Vorgang.
    """
    message_id = record["message_id"]
    if message_id in by_message_id:
        return
    by_message_id[message_id] = record
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
        if record is not None:
            return {
                "vorgang_ref": record.get("vorgang_ref"),
                "matched_message_id": candidate,
                "match_type": "in_reply_to" if candidate == in_reply_to else "references",
            }
    key = norm_id(item.get("thread_key"))
    record = ledger.get("by_thread_key", {}).get(key) if key else None
    if record is not None and record["message_id"] != own:
        return {
            "vorgang_ref": record.get("vorgang_ref"),
            "matched_message_id": record["message_id"],
            "match_type": "thread_key",
        }
    return None


def is_synthesized(item: dict[str, Any], ledger: dict[str, Any]) -> bool:
    """Whether the item's own Message-ID already has a ledger line."""
    return norm_id(item.get("norm_message_id") or item.get("message_id")) in ledger.get("by_message_id", {})


def record_synthesized(
    data_dir: Path,
    items: Iterable[dict[str, Any]],
    *,
    vorgang_ref: str | None = None,
) -> int:
    """Mark handoff items as consumed (append-only, idempotent); returns lines added.

    Call this only after the item's synthesis result is durably persisted: the
    ledger line is what releases the item from the handoff and from re-promotion.
    The anchor is **re-resolved here** against the current ledger including the
    lines of this very call, so a reply recorded after its ancestor becomes a
    ``follow_up`` of the ancestor's Vorgang even if the handoff snapshot said
    otherwise.  ``vorgang_ref`` precedence: the item's own, then the argument, then
    the anchor's.
    """
    reject_static_aliases(
        data_dir / LEDGER_FILENAME, reason_code="ledger_alias_invalid", label="ledger",
    )
    ledger = load_ledger(data_dir)
    by_message_id, by_thread_key = ledger["by_message_id"], ledger["by_thread_key"]
    records: list[dict[str, Any]] = []
    for item in items:
        mid = norm_id(item.get("norm_message_id") or item.get("message_id"))
        if not mid:
            raise ValueError("Cannot record a ledger line without a Message-ID.")
        if mid in by_message_id:
            continue
        anchor = resolve_thread_anchor(item, ledger)
        ref = item.get("vorgang_ref") or vorgang_ref or (anchor or {}).get("vorgang_ref")
        if ref is not None and not isinstance(ref, str):
            raise ValueError("vorgang_ref must be a string.")
        record = {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "message_id": mid,
            "thread_key": norm_id(item.get("thread_key")) or thread_key(
                item.get("in_reply_to"), _split_references(item.get("references")), mid
            ),
            "vorgang_ref": ref,
            "role": "follow_up" if anchor else "primary",
            "anchored_to": anchor["matched_message_id"] if anchor else None,
            "recorded_at": utc_now_iso(),
        }
        _index_ledger_record(by_message_id, by_thread_key, record)
        records.append(record)
    return durable_append_jsonl(
        data_dir / LEDGER_FILENAME, records,
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
    Raises ``SentHandoffError`` for an invalid handoff or ledger.
    """
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


def peek_handoff_state(data_dir: Path) -> dict[str, Any]:
    """Read-only ``{"state", "pending_count"}`` for error envelopes; never raises."""
    try:
        pending = len(unconsumed(read_handoff(data_dir), load_ledger(data_dir)))
    except Exception:  # noqa: BLE001 - diagnostics must never mask the real failure
        return {"state": "invalid", "pending_count": None}
    return {"state": "pending" if pending else "none", "pending_count": pending}
