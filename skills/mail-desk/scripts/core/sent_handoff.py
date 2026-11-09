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
* Pure helpers for the thread key and the thread anchor lookup.

The bundle never writes synthesis results; the consumer owns the ledger writes
(``record_synthesized`` or a direct append of the documented line schema).
"""

from __future__ import annotations

import json
import os
from email.utils import getaddresses
from pathlib import Path
from typing import Any, Iterable

from .common import atomic_write_json, normalize_message_id, utc_now_iso

HANDOFF_FILENAME = "new-sent-items.json"
LEDGER_FILENAME = "sent-synthesis-ledger.jsonl"
HANDOFF_SCHEMA_VERSION = 1
LEDGER_SCHEMA_VERSION = 1

#: Upper bound of unconsumed items in the handoff file.  One run hands off at
#: most this many new items; the surplus (newest first) is *deferred*, i.e. not
#: indexed either, and is picked up by a later run once the handoff is consumed.
MAX_HANDOFF_ITEMS = 200

#: Per-item field bounds (the sent-index entry keeps the full values).  A bounded
#: field is listed in the item's ``truncated_fields`` instead of being cut silently.
MAX_REFERENCES = 50
MAX_RECIPIENTS = 100
MAX_SUBJECT_CHARS = 1000


class SentHandoffError(RuntimeError):
    """Fail-closed handoff refusal; nothing was written to the sent-index.

    ``reason_code`` is one of ``handoff_pending`` (unconsumed items would be
    overwritten/extended) or ``handoff_invalid`` (existing file unreadable or not
    a valid handoff document).
    """

    def __init__(self, reason_code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.details = dict(details or {})


class SentSyncPartialFailure(RuntimeError):
    """Some date windows failed; the successful windows were handed off and indexed.

    ``result`` is the regular ``sync_sent_items`` result of the committed part.
    """

    def __init__(self, message: str, result: dict[str, Any], failed_dates: list[str]) -> None:
        super().__init__(message)
        self.result = result
        self.failed_dates = list(failed_dates)


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


# ------------------------------------------------------------------ durable append


def durable_append_jsonl(path: Path, entries: Iterable[dict[str, Any]]) -> int:
    """Append JSON lines (LF) and fsync; repair a torn final line first.

    A crash can leave a last line without its newline; without the repair the next
    append would glue two records into one unparsable line.
    """
    lines = [json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries]
    if not lines:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    needs_newline = False
    if path.exists() and path.stat().st_size > 0:
        with path.open("rb") as existing:
            existing.seek(-1, os.SEEK_END)
            needs_newline = existing.read(1) != b"\n"
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        if needs_newline:
            handle.write("\n")
        handle.writelines(lines)
        handle.flush()
        os.fsync(handle.fileno())
    return len(lines)


# ------------------------------------------------------------------ ledger


def load_ledger(data_dir: Path) -> dict[str, Any]:
    """Load the consumption ledger.

    Returns ``{"by_message_id", "by_thread_key", "invalid_lines"}``.  A malformed
    line is skipped and counted: the affected item then simply stays unconsumed,
    which is the fail-closed direction.
    """
    by_message_id: dict[str, dict[str, Any]] = {}
    by_thread_key: dict[str, dict[str, Any]] = {}
    invalid = 0
    path = data_dir / LEDGER_FILENAME
    if path.exists():
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                text = line.strip()
                if not text:
                    continue
                try:
                    record = json.loads(text)
                except json.JSONDecodeError:
                    invalid += 1
                    continue
                mid = norm_id(record.get("message_id")) if isinstance(record, dict) else ""
                if not mid or record.get("schema_version") != LEDGER_SCHEMA_VERSION:
                    invalid += 1
                    continue
                by_message_id.setdefault(mid, record)
                key = norm_id(record.get("thread_key"))
                if key:
                    existing = by_thread_key.get(key)
                    # Prefer the first record that carries a Vorgang reference.
                    if existing is None or (not existing.get("vorgang_ref") and record.get("vorgang_ref")):
                        by_thread_key[key] = record
    return {"by_message_id": by_message_id, "by_thread_key": by_thread_key, "invalid_lines": invalid}


def resolve_thread_anchor(item: dict[str, Any], ledger: dict[str, Any]) -> dict[str, Any] | None:
    """Anchor a sent item to an already synthesized ancestor, or ``None``.

    Match order: ``In-Reply-To``, ``References`` (nearest first), then the shared
    thread key.  The result names the ledger's ``vorgang_ref`` (may be ``None`` when
    the consumer recorded none) so the follow-up evidence attaches to the same Vorgang.
    """
    by_message_id = ledger.get("by_message_id", {})
    in_reply_to = norm_id(item.get("in_reply_to"))
    own = norm_id(item.get("norm_message_id") or item.get("message_id"))
    for candidate in item.get("anchor_candidates") or thread_anchor_ids(
        item.get("in_reply_to"), item.get("references") or [], own
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
    if record is not None and norm_id(record.get("message_id")) != own:
        return {
            "vorgang_ref": record.get("vorgang_ref"),
            "matched_message_id": norm_id(record.get("message_id")),
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
    An item may carry its own ``vorgang_ref``; otherwise ``vorgang_ref`` or the
    item's resolved ``anchored_to.vorgang_ref`` is used.
    """
    ledger = load_ledger(data_dir)
    known = set(ledger["by_message_id"])
    records: list[dict[str, Any]] = []
    for item in items:
        mid = norm_id(item.get("norm_message_id") or item.get("message_id"))
        if not mid:
            raise ValueError("Cannot record a ledger line without a Message-ID.")
        if mid in known:
            continue
        known.add(mid)
        anchor = item.get("anchored_to") or {}
        records.append({
            "schema_version": LEDGER_SCHEMA_VERSION,
            "message_id": mid,
            "thread_key": norm_id(item.get("thread_key")) or thread_key(
                item.get("in_reply_to"), _split_references(item.get("references")), mid
            ),
            "vorgang_ref": item.get("vorgang_ref") or vorgang_ref or anchor.get("vorgang_ref"),
            "role": "follow_up" if anchor else "primary",
            "anchored_to": anchor.get("matched_message_id"),
            "recorded_at": utc_now_iso(),
        })
    return durable_append_jsonl(data_dir / LEDGER_FILENAME, records)


# ------------------------------------------------------------------ handoff file


def read_handoff(data_dir: Path) -> list[dict[str, Any]]:
    """Read the handoff items; ``[]`` when absent.  Fail closed when invalid."""
    path = data_dir / HANDOFF_FILENAME
    if not path.exists():
        return []

    def invalid(reason: str) -> SentHandoffError:
        return SentHandoffError(
            "handoff_invalid",
            f"Existing sent handoff '{HANDOFF_FILENAME}' is not a valid handoff document ({reason}); "
            "it was left untouched and nothing was indexed.",
            {"reason": reason},
        )

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise invalid(type(error).__name__) from error
    if not isinstance(document, dict) or document.get("schema_version") != HANDOFF_SCHEMA_VERSION:
        raise invalid("unsupported_schema")
    items = document.get("items")
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise invalid("items_not_a_list_of_objects")
    seen: set[str] = set()
    for item in items:
        mid = norm_id(item.get("norm_message_id") or item.get("message_id"))
        if not mid or mid in seen:
            raise invalid("missing_or_duplicate_message_id")
        seen.add(mid)
    return items


def write_handoff(data_dir: Path, items: list[dict[str, Any]]) -> None:
    """Atomically replace the handoff document (never called with more than the bound)."""
    if len(items) > MAX_HANDOFF_ITEMS:
        raise SentHandoffError(
            "handoff_invalid",
            f"Refusing to write {len(items)} handoff items (bound {MAX_HANDOFF_ITEMS}).",
        )
    atomic_write_json(
        data_dir / HANDOFF_FILENAME,
        {
            "schema_version": HANDOFF_SCHEMA_VERSION,
            "kind": "mail-desk.new-sent-items",
            "generated_at": utc_now_iso(),
            "max_items": MAX_HANDOFF_ITEMS,
            "count": len(items),
            "items": items,
        },
    )
