"""Strict, fail-closed decoding of the Himalaya v3 JSON boundary.

Two v3 shapes are supported (both documented in
``references/backends/himalaya.md``):

* the **live array** of ``himalaya envelope list --json`` (every entry carries
  the camelCase ``messageId`` key, nullable; ``inReplyTo``, ``flags``
  ``{raw,iana}``, ``from``/``to`` ``{name,email}``, ``date``, ``size`` and
  ``hasAttachment`` are typed);
* the forward-compatible **schema object** ``{envelopes,nextPage,total,errors}``.

Everything else is ``himalaya_json_incompatible``: a v1 array without
``messageId``, a snake_case-only ``message_id``, a missing or wrongly typed
``messageId``, conflicting duplicate ids, a missing schema key, a non-uint
``total`` and any non-empty ``errors``.  The array form never invents
``nextPage``/``total``; the object form is paged with a hard bound.

The normalizer never mutates the parsed source objects: it builds a new
internal, v1-compatible envelope (``id``, ``subject``, ``from {name,addr}``,
``to``, ``date``, ``flags`` raw strings, ``size``, ``has_attachment`` and the
additive ``message_id``/``in_reply_to``).
"""

from __future__ import annotations

from typing import Any, Iterable

from .common import normalize_message_id

#: Reason raised for every structurally incompatible v3 payload.
JSON_INCOMPATIBLE_REASON = "himalaya_json_incompatible"

#: Reason raised when a page token repeats without making progress.
PAGING_NOT_PROGRESSING_REASON = "himalaya_paging_not_progressing"

#: Hard page bound for following ``nextPage``.
MAX_V3_PAGES = 100

_SCHEMA_KEYS = ("envelopes", "nextPage", "total", "errors")


class JsonIncompatible(Exception):
    """A bounded refusal of a payload that is not the documented v3 shape."""

    def __init__(self, message: str, *, reason_code: str = JSON_INCOMPATIBLE_REASON) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def _fail(message: str, *, reason_code: str = JSON_INCOMPATIBLE_REASON) -> JsonIncompatible:
    return JsonIncompatible(message, reason_code=reason_code)


def _is_uint(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _typed_address_list(value: object, *, field: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise _fail(f"'{field}' must be an array of address objects.")
    addresses: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise _fail(f"'{field}' entries must be address objects.")
        name = item.get("name", "")
        email = item.get("email", "")
        # ``Address.name`` is ``string|null`` in the v3 schema: a nameless sender
        # is valid and normalizes to the empty display name.
        if name is None:
            name = ""
        if not isinstance(name, str) or not isinstance(email, str):
            raise _fail(f"'{field}' entries must carry string name/email values.")
        addresses.append({"name": name, "email": email})
    return addresses


def _typed_flags(value: object) -> list[str]:
    if not isinstance(value, list):
        raise _fail("'flags' must be an array of flag objects.")
    raw_flags: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            raise _fail("'flags' entries must be objects.")
        raw = item.get("raw")
        iana = item.get("iana", "")
        # ``Flag.iana`` is ``IanaFlag|null``: a custom keyword carries no IANA tag.
        if iana is None:
            iana = ""
        if not isinstance(raw, str) or not isinstance(iana, str):
            raise _fail("'flags' entries must carry string raw/iana values.")
        raw_flags.append(raw)
    return raw_flags


def _validate_camel_envelope(entry: object) -> dict[str, Any]:
    """Validate one v3 envelope and return it unchanged (no mutation, no copy)."""
    if not isinstance(entry, dict):
        raise _fail("Every v3 envelope must be an object.")
    identity = entry.get("id")
    if isinstance(identity, bool) or not isinstance(identity, (str, int)):
        raise _fail("Every v3 envelope needs a string id.")
    if "messageId" not in entry:
        raise _fail(
            "Every v3 envelope needs the camelCase 'messageId' key; a v1 array or "
            "a snake_case-only 'message_id' is not the v3 contract."
        )
    message_id = entry["messageId"]
    if message_id is not None and not isinstance(message_id, str):
        raise _fail("'messageId' must be a string or null.")
    if "inReplyTo" in entry:
        if not isinstance(entry["inReplyTo"], list) or any(
            not isinstance(value, str) for value in entry["inReplyTo"]
        ):
            raise _fail("'inReplyTo' must be an array of strings.")
    if "flags" in entry:
        _typed_flags(entry["flags"])
    if "from" in entry:
        _typed_address_list(entry["from"], field="from")
    if "to" in entry:
        _typed_address_list(entry["to"], field="to")
    if "subject" in entry and entry["subject"] is not None and not isinstance(entry["subject"], str):
        raise _fail("'subject' must be a string or null.")
    if "date" in entry and entry["date"] is not None and not isinstance(entry["date"], str):
        raise _fail("'date' must be a string or null.")
    if "size" in entry and entry["size"] is not None and not _is_uint(entry["size"]):
        raise _fail("'size' must be an unsigned integer or null.")
    if "hasAttachment" in entry and entry["hasAttachment"] is not None and not isinstance(
        entry["hasAttachment"], bool
    ):
        raise _fail("'hasAttachment' must be a boolean or null.")
    return entry


def _bracketed_message_ids(values: Iterable[str]) -> str:
    parts: list[str] = []
    for value in values:
        normalized = normalize_message_id(value)
        if normalized:
            parts.append(f"<{normalized}>")
    return " ".join(parts)


def _first_address(value: object) -> dict[str, str]:
    if isinstance(value, list) and value and isinstance(value[0], dict):
        first = value[0]
        name = first.get("name")
        return {
            "name": "" if name is None else str(name),
            "addr": str(first.get("email", "")),
        }
    return {"name": "", "addr": ""}


def normalize_camel_envelope(entry: dict[str, Any]) -> dict[str, Any]:
    """Map one validated v3 envelope onto the internal v1-compatible shape.

    The source object is never mutated.  A ``null``/empty ``messageId`` adds no
    ``message_id`` key, mirroring the additive v1 behaviour.
    """
    raw_message_id = entry.get("messageId")
    normalized: dict[str, Any] = {
        "id": str(entry.get("id")),
        "subject": entry.get("subject") if isinstance(entry.get("subject"), str) else "",
        "from": _first_address(entry.get("from")),
        "to": _first_address(entry.get("to")),
        "date": entry.get("date") if isinstance(entry.get("date"), str) else "",
        "flags": _typed_flags(entry.get("flags", [])),
        "size": entry.get("size"),
        "has_attachment": entry.get("hasAttachment"),
    }
    if isinstance(raw_message_id, str):
        message_id = normalize_message_id(raw_message_id)
        if message_id:
            normalized["message_id"] = message_id
    reply_to = entry.get("inReplyTo")
    if isinstance(reply_to, list):
        normalized["in_reply_to"] = _bracketed_message_ids(reply_to)
    return normalized


def _validate_duplicates(entries: list[dict[str, Any]]) -> None:
    """Refuse conflicting duplicate envelope ids (never resolve by order).

    Two occurrences agree when their ``messageId`` normalizes to the same value
    (or both are absent); a differing non-null value is unresolvable.
    """
    seen: dict[str, str | None] = {}
    for entry in entries:
        identity = str(entry.get("id"))
        raw_message_id = entry.get("messageId")
        normalized = (
            normalize_message_id(raw_message_id)
            if isinstance(raw_message_id, str) and raw_message_id.strip()
            else None
        ) or None
        if identity in seen and seen[identity] != normalized:
            raise _fail("Conflicting duplicate v3 envelope ids are not resolvable.")
        seen[identity] = normalized


def decode_envelope_array(payload: object) -> list[dict[str, Any]]:
    """Validate and normalize one live v3 envelope array."""
    if not isinstance(payload, list):
        raise _fail("The v3 live envelope payload must be an array.")
    entries = [_validate_camel_envelope(entry) for entry in payload]
    _validate_duplicates(entries)
    return [normalize_camel_envelope(entry) for entry in entries]


def decode_envelope_page(payload: object) -> tuple[list[dict[str, Any]], str | None]:
    """Validate one v3 envelope page and return ``(entries, next_page)``.

    An array is one complete page without ``nextPage``.  The schema object is
    validated strictly (all four keys, uint/null ``total``, no ``errors``) and
    its ``nextPage`` is returned for the caller's bounded paging loop.
    """
    if isinstance(payload, list):
        return decode_envelope_array(payload), None
    if not isinstance(payload, dict):
        raise _fail("The v3 envelope payload must be an array or the schema object.")
    for key in _SCHEMA_KEYS:
        if key not in payload:
            raise _fail(f"The v3 schema object is missing the '{key}' key.")
    envelopes = payload["envelopes"]
    if not isinstance(envelopes, list):
        raise _fail("The v3 schema 'envelopes' value must be an array.")
    errors = payload["errors"]
    if not isinstance(errors, list):
        raise _fail("The v3 schema 'errors' value must be an array.")
    if errors:
        raise _fail("The v3 schema reported item errors; the page is not a partial success.")
    next_page = payload["nextPage"]
    if next_page is not None and not isinstance(next_page, str):
        raise _fail("The v3 schema 'nextPage' value must be a string or null.")
    total = payload["total"]
    if total is not None and not _is_uint(total):
        raise _fail("The v3 schema 'total' value must be an unsigned integer or null.")
    entries = [_validate_camel_envelope(entry) for entry in envelopes]
    _validate_duplicates(entries)
    return [normalize_camel_envelope(entry) for entry in entries], next_page


def decode_mailbox_list(payload: object) -> list[dict[str, Any]]:
    """Validate ``mailbox list --json`` (``{mailboxes,errors}``) and return the mailboxes."""
    if not isinstance(payload, dict):
        raise _fail("The v3 mailbox payload must be the {mailboxes,errors} object.")
    if "mailboxes" not in payload or "errors" not in payload:
        raise _fail("The v3 mailbox payload is missing the mailboxes/errors envelope.")
    mailboxes = payload["mailboxes"]
    errors = payload["errors"]
    if not isinstance(mailboxes, list):
        raise _fail("The v3 mailbox 'mailboxes' value must be an array.")
    if not isinstance(errors, list):
        raise _fail("The v3 mailbox 'errors' value must be an array.")
    if errors:
        raise _fail("The v3 mailbox listing reported item errors.")
    result: list[dict[str, Any]] = []
    for mailbox in mailboxes:
        if not isinstance(mailbox, dict) or not isinstance(mailbox.get("name"), str):
            raise _fail("Every v3 mailbox entry must be an object with a string name.")
        result.append(dict(mailbox))
    return result
