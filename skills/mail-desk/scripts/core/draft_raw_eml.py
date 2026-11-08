"""Raw-EML import for the standalone draft API: strict offline profile, CRLF normalization, read-back model.

This module adds **no transport**.  A prepared ``.eml`` is validated here and then appended through the one
existing binary-stdin ``message save`` path of ``core.drafts`` (``save_raw_draft`` reuses
``_append_and_verify``: unique-ID check, journaled intent, ONE APPEND, never retried, semantic read-back).

Supported profile (everything else fails closed with a bounded, content-free reason code):

* one regular ``*.eml`` file below one of the four consumer data zones (never a link or junction), at most
  ``MAX_RAW_EML_BYTES`` (10 MiB) on disk and after normalization;
* valid UTF-8 without NUL and without a bare CR; every line at most ``MAX_LINE_BYTES`` (998) octets.  The
  stdin transport of Himalaya 1.2.0 reads UTF-8 lines, joins them with CRLF and strips every ``\\r``, so
  the message is normalized to canonical CRLF lines *before* the APPEND (exactly one final CRLF is added
  when missing; trailing blank lines are kept) -- the stored message must equal the normalized one;
* a strict RFC 5322 header block: ``name: value`` fields (printable US-ASCII names, no space before the
  colon), folding only by leading space / tab, no control characters (tab excepted) in values, at most
  ``MAX_HEADER_FIELDS`` fields and ``MAX_HEADER_BLOCK_BYTES`` bytes, the header/body separator present;
* exactly one ``Message-ID`` of the plain form ``<dot-atom@hostname>``; ``To`` and ``Date`` present;
  RFC 5322 singleton headers (``From``, ``To``, ``Cc``, ``Bcc``, ``Subject``, ``Date``, ``Sender``,
  ``Reply-To``, ``In-Reply-To``, ``References``, ``Message-ID`` and the MIME headers) at most once;
* structure ``text/plain`` (UTF-8 or US-ASCII; 7bit / 8bit / quoted-printable / base64), or a flat
  ``multipart/mixed`` with that text part first followed by at most ``MAX_ATTACHMENTS`` (5) base64
  ``attachment`` parts of at most ``MAX_ATTACHMENT_BYTES`` (3 MiB decoded) each with a valid, unique
  file name; no preamble / epilogue text, no parser defect, no nested multipart, no ``message/*`` part,
  no HTML / alternative body.

Fidelity is semantic, not byte equality of arbitrary mail: header sequence (names and whitespace-squashed
values), decoded text body and per attachment file name, content type, size and SHA-256 are compared
between the prepared and the stored message.  Error texts carry no file content, caller path or OS text.
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import datetime
import hashlib
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from email.policy import SMTPUTF8
from typing import Any

from . import draft_attachments as da
from .common import normalize_message_id

#: Largest accepted ``.eml`` (file as read and message after normalization): 10 MiB, inclusive.
MAX_RAW_EML_BYTES = da.MAX_MIME_MESSAGE_BYTES

#: RFC 5322 hard line limit in octets (excluding the CRLF).
MAX_LINE_BYTES = 998

#: Header budget (fields and bytes of the whole header block).
MAX_HEADER_FIELDS = 100
MAX_HEADER_BLOCK_BYTES = 64 * 1024

RAW_EML_SUFFIX = ".eml"
_LABEL = "raw EML file"
_MAX_MESSAGE_ID_CHARS = 255

#: Headers that must occur at most once (RFC 5322 3.6 plus the MIME headers the profile reads).
_SINGLETON_HEADERS = frozenset(
    {
        "from", "sender", "reply-to", "to", "cc", "bcc", "message-id", "in-reply-to", "references", "subject",
        "date", "mime-version", "content-type", "content-transfer-encoding", "content-disposition",
    }
)
_ALLOWED_BODY_CHARSETS = frozenset({"utf-8", "us-ascii"})
_BODY_ENCODINGS = frozenset({"7bit", "8bit", "quoted-printable", "base64"})

_FIELD_RE = re.compile(r"([\x21-\x39\x3b-\x7e]+):(.*)", re.DOTALL)
_BARE_CR_RE = re.compile(r"\r(?!\n)")
_ATOM = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
_HOST_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
_MESSAGE_ID_RE = re.compile(rf"<{_ATOM}(?:\.{_ATOM})*@{_HOST_LABEL}(?:\.{_HOST_LABEL})*>")
_BASE64_RE = re.compile(r"[A-Za-z0-9+/]+={0,2}")


class RawEmlError(da.DraftAttachmentError):
    """A fail-closed, content-free Raw-EML validation / reading failure."""


def _fail(reason: str, message: str) -> RawEmlError:
    return RawEmlError(reason, message)


@dataclass(frozen=True)
class PreparedRawEml:
    """A fully validated, normalized message (content never appears in ``repr``)."""

    data: bytes = field(repr=False)
    text: str = field(repr=False)
    message_id: str
    subject: str = field(repr=False)
    to: str = field(repr=False)
    date: str
    attachments: tuple[da.DraftAttachment, ...]
    summary: dict[str, Any] = field(repr=False)
    line_endings: str
    source_sha256: str
    normalized_sha256: str
    size_bytes: int
    source_path: str = ""

    @property
    def normalized(self) -> bool:
        """Whether line-ending normalization changed any byte of the source."""
        return self.source_sha256 != self.normalized_sha256


# ---------------------------------------------------------------------------
# Bytes -> normalized text
# ---------------------------------------------------------------------------

def _squash(value: str) -> str:
    return " ".join(value.split())


def _decode_and_normalize(data: Any) -> tuple[str, str]:
    """Return ``(normalized CRLF text, original line-ending form)``; byte level checks only."""
    if not isinstance(data, bytes):
        raise _fail("raw_eml_invalid", "The raw EML input must be bytes.")
    if not data:
        raise _fail("raw_eml_empty", f"The {_LABEL} is empty.")
    if len(data) > MAX_RAW_EML_BYTES:
        raise _fail("raw_eml_too_large", f"The {_LABEL} exceeds the {MAX_RAW_EML_BYTES} byte limit.")
    if b"\x00" in data:
        raise _fail("raw_eml_nul_byte", f"The {_LABEL} contains a NUL byte; refusing.")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise _fail(
            "raw_eml_not_utf8",
            f"The {_LABEL} is not valid UTF-8; the stdin transport requires UTF-8 and nothing was converted.",
        ) from None
    if _BARE_CR_RE.search(text):
        raise _fail("raw_eml_bare_cr", f"The {_LABEL} contains a bare carriage return; refusing.")
    crlf = text.count("\r\n")
    lf_only = text.count("\n") - crlf
    form = "crlf" if lf_only == 0 else ("lf" if crlf == 0 else "mixed")
    normalized = text.replace("\r\n", "\n").replace("\n", "\r\n")
    if not normalized.endswith("\r\n"):
        normalized += "\r\n"
    if len(normalized.encode("utf-8")) > MAX_RAW_EML_BYTES:
        raise _fail(
            "raw_eml_message_too_large",
            f"The {_LABEL} exceeds the {MAX_RAW_EML_BYTES} byte limit after line-ending normalization.",
        )
    for line in normalized.split("\r\n")[:-1]:
        if len(line) > MAX_LINE_BYTES or (not line.isascii() and len(line.encode("utf-8")) > MAX_LINE_BYTES):
            raise _fail("raw_eml_line_too_long", f"A line of the {_LABEL} exceeds {MAX_LINE_BYTES} octets; refusing.")
    return normalized, form


# ---------------------------------------------------------------------------
# Header block
# ---------------------------------------------------------------------------

def _header_invalid(detail: str, *, part: bool = False) -> RawEmlError:
    if part:
        return _fail(
            "raw_eml_part_header_invalid",
            f"A MIME part header block of the {_LABEL} is invalid ({detail}).",
        )
    return _fail("raw_eml_header_invalid", f"The {_LABEL} has an invalid header block ({detail}).")


def _parse_header_block(lines: list[str], *, part: bool = False) -> tuple[list[tuple[str, str]], int]:
    """Return ``([(field name, unfolded value)], index of the blank separator line)`` or fail closed.

    The same strict rules apply to the message header block and to every MIME part header block; only
    the reason code differs (``raw_eml_header_invalid`` / ``raw_eml_part_header_invalid``).
    """
    fields: list[tuple[str, list[str]]] = []
    block_bytes = 0
    for index, line in enumerate(lines):
        if line == "":
            return [(name, "".join(parts)) for name, parts in fields], index
        block_bytes += len(line.encode("utf-8")) + 2
        if block_bytes > MAX_HEADER_BLOCK_BYTES:
            raise _header_invalid("header block too large", part=part)
        for char in line:
            if char != "\t" and unicodedata.category(char) in ("Cc", "Cs", "Zl", "Zp"):
                raise _header_invalid("control or separator character", part=part)
        if line[0] in " \t":
            if not fields or not line.strip(" \t"):
                raise _header_invalid("misplaced or blank continuation line", part=part)
            fields[-1][1].append(line)
            continue
        match = _FIELD_RE.fullmatch(line)
        if match is None:
            raise _header_invalid("malformed field line", part=part)
        if len(fields) >= MAX_HEADER_FIELDS:
            raise _header_invalid("too many header fields", part=part)
        fields.append((match.group(1), [match.group(2)]))
    raise _header_invalid("header/body separator missing", part=part)


def _split_parts(body_lines: list[str], boundary: str) -> list[list[str]]:
    """Split the lines after the message header block into the line lists of the MIME parts.

    A delimiter is a line that is exactly ``--boundary`` (``--boundary--`` closes), optionally followed by
    spaces / tabs; anything before the first delimiter or after the closing one is not part content.
    """
    delimiter, closing = "--" + boundary, "--" + boundary + "--"
    parts: list[list[str]] = []
    current: list[str] | None = None
    for line in body_lines:
        stripped = line.rstrip(" \t")
        if stripped == closing:
            if current is not None:
                parts.append(current)
            return parts
        if stripped == delimiter:
            if current is not None:
                parts.append(current)
            current = []
        elif current is not None:
            current.append(line)
    raise _mime_invalid("closing boundary missing")


def _check_part_headers(part_blocks: list[list[str]]) -> None:
    """Strict structural rules for every MIME part header block (fail closed, content-free)."""
    for block in part_blocks:
        fields, _ = _parse_header_block(block, part=True)
        counts = Counter(name.casefold() for name, _ in fields)
        for name, count in counts.items():
            if count > 1 and (name.startswith("content-") or name == "mime-version"):
                raise _header_invalid("a Content-* header occurs more than once", part=True)


def _validate_message_id(value: str) -> str:
    text = value.strip(" \t")
    if len(text) > _MAX_MESSAGE_ID_CHARS or _MESSAGE_ID_RE.fullmatch(text) is None:
        raise _fail(
            "raw_eml_message_id_invalid",
            "The Message-ID header is not a single plain <local@domain> identifier; refusing.",
        )
    return normalize_message_id(text)


def _validate_address_list(value: str) -> None:
    """``To`` must be a well-formed RFC 5322 address list (mailboxes and groups), ASCII addr-specs."""
    invalid = _fail("raw_eml_address_invalid", "The 'to' header is not a valid RFC 5322 address list; refusing.")
    text = value.strip()
    if text.startswith(",") or text.endswith(",") or re.search(r",\s*,", text):
        raise invalid
    try:
        header = SMTPUTF8.header_factory("To", text)
        groups = list(header.groups)
        defects = list(header.defects)
        addresses = [address for group in groups for address in group.addresses]
    except Exception:  # noqa: BLE001 - any parser failure is a refusal
        raise invalid from None
    if defects or not groups:
        raise invalid
    for address in addresses:
        if not address.username or not address.domain or not address.addr_spec.isascii():
            raise invalid


_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_DATE_RE = re.compile(
    r"(?:(?P<dow>[A-Za-z]{3}),[ ]?)?(?P<day>\d{1,2}) (?P<mon>[A-Za-z]{3}) (?P<year>\d{4}) "
    r"(?P<hour>\d{2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))? (?P<sign>[+-])(?P<zh>\d{2})(?P<zm>\d{2})"
    r"(?: \((?:[^()\\]|\\.)*\))?"
)


def _validate_date(value: str) -> None:
    """``Date`` must be an RFC 5322 date-time: numeric zone, real calendar date, consistent weekday.

    Obsolete forms (two-digit years, ``GMT`` / ``EST`` zones) are refused; the optional trailing
    comment must be a single unnested parenthesis group.  Years 1900-9999.
    """
    invalid = _fail("raw_eml_date_invalid", "The 'date' header is not a valid RFC 5322 date-time; refusing.")
    match = _DATE_RE.fullmatch(value.strip())
    if match is None:
        raise invalid
    month = _MONTHS.index(match["mon"].lower()) + 1 if match["mon"].lower() in _MONTHS else 0
    year = int(match["year"])
    if not month or year < 1900:
        raise invalid
    if int(match["hour"]) > 23 or int(match["minute"]) > 59 or int(match["second"] or 0) > 60:
        raise invalid
    if int(match["zh"]) > 23 or int(match["zm"]) > 59:
        raise invalid
    try:
        day = datetime.date(year, month, int(match["day"]))
    except ValueError:
        raise invalid from None
    if match["dow"] is not None and (
        match["dow"].lower() not in _WEEKDAYS or _WEEKDAYS.index(match["dow"].lower()) != day.weekday()
    ):
        raise invalid


def _check_required_headers(fields: list[tuple[str, str]]) -> dict[str, str]:
    """Singleton / required header rules; returns the squashed value of every singleton present."""
    counts = Counter(name.casefold() for name, _ in fields)
    for name in sorted(set(_SINGLETON_HEADERS) | {n for n in counts if n.startswith("content-")}):
        if counts[name] > 1:
            raise _fail("raw_eml_header_duplicate", f"The header '{name}' occurs more than once; refusing.")
    values = {name.casefold(): _squash(value) for name, value in fields if name.casefold() in _SINGLETON_HEADERS}
    if "message-id" not in values:
        raise _fail("raw_eml_header_missing", "The required header 'message-id' is missing; refusing.")
    for name in ("to", "date"):
        if not values.get(name):
            raise _fail("raw_eml_header_missing", f"The required header '{name}' is missing or empty; refusing.")
    return values


# ---------------------------------------------------------------------------
# MIME structure
# ---------------------------------------------------------------------------

def _mime_invalid(detail: str) -> RawEmlError:
    return _fail("raw_eml_mime_invalid", f"The {_LABEL} has an invalid or unsupported MIME encoding ({detail}).")


def _unsupported(detail: str) -> RawEmlError:
    return _fail("raw_eml_structure_unsupported", f"The {_LABEL} structure is not supported ({detail}).")


def _invalid_attachment(index: int, detail: str) -> RawEmlError:
    return _fail("raw_eml_attachment_invalid", f"attachment #{index} is invalid ({detail}).")


def _decode_text_part(part: Any) -> str:
    charset = (part.get_content_charset() or "us-ascii").lower()
    if charset not in _ALLOWED_BODY_CHARSETS:
        raise _mime_invalid("body charset")
    encoding = str(part.get("Content-Transfer-Encoding") or "7bit").strip().lower()
    if encoding not in _BODY_ENCODINGS:
        raise _mime_invalid("body transfer encoding")
    try:
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            raise ValueError
        return payload.decode(charset)
    except (UnicodeDecodeError, ValueError, LookupError):
        raise _mime_invalid("body text") from None


def _decode_attachment(part: Any, index: int) -> da.DraftAttachment:
    if part.is_multipart() or part.get_content_maintype() in ("multipart", "message"):
        raise _unsupported("nested or embedded message part")
    if part.get_content_disposition() != "attachment":
        raise _invalid_attachment(index, "not an attachment part")
    filename = part.get_filename()
    if filename is None:
        raise _invalid_attachment(index, "no file name")
    try:
        da.validate_filename(filename, index=index - 1)
        content_type = da.validate_content_type(part.get_content_type(), index=index - 1)
    except da.DraftAttachmentError:
        raise _invalid_attachment(index, "file name or content type") from None
    if str(part.get("Content-Transfer-Encoding") or "").strip().lower() != "base64":
        raise _invalid_attachment(index, "transfer encoding is not base64")
    payload = part.get_payload()
    compact = payload.replace("\r\n", "") if isinstance(payload, str) else ""
    if _BASE64_RE.fullmatch(compact) is None:
        raise _invalid_attachment(index, "malformed base64")
    try:
        data = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError):
        raise _invalid_attachment(index, "malformed base64") from None
    if not data:
        raise _invalid_attachment(index, "empty content")
    if len(data) > da.MAX_ATTACHMENT_BYTES:
        raise _fail(
            "raw_eml_attachment_too_large",
            f"attachment #{index} exceeds the {da.MAX_ATTACHMENT_BYTES} byte limit.",
        )
    return da.DraftAttachment(
        filename=filename,
        content_type=content_type,
        data=data,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def _exact_text(value: str) -> str:
    """Line-ending neutral but otherwise exact text: trailing newlines are part of the body."""
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _analyse_structure(
    message: Any, values: dict[str, str], body_lines: list[str]
) -> tuple[str, str, tuple[da.DraftAttachment, ...]]:
    """Return ``(layout, body text, attachments)`` of the profile or fail closed.

    ``body_lines`` are the lines after the message header block; for ``multipart/mixed`` every part
    header block is parsed strictly from them before the tolerant stdlib parser's view is trusted.
    """
    version = values.get("mime-version")
    has_content_header = any(str(key).lower().startswith("content-") for key in message.keys())
    if version is not None and version != "1.0":
        raise _mime_invalid("MIME-Version")
    if version is None and has_content_header:
        raise _mime_invalid("Content-* header without MIME-Version")
    content_type = message.get_content_type()
    if content_type == "text/plain":
        layout = "text"
    elif content_type == "multipart/mixed":
        layout = "mixed"
    else:
        raise _unsupported("top-level content type")
    part_blocks: list[list[str]] = []
    if layout == "mixed":
        boundary = message.get_boundary()
        if not boundary:
            raise _mime_invalid("multipart boundary missing")
        part_blocks = _split_parts(body_lines, boundary)
        _check_part_headers(part_blocks)
    if da._all_defects(message):
        raise _mime_invalid("parser defect")
    if layout == "text":
        return layout, _exact_text(_decode_text_part(message)), ()
    if (message.preamble or "").strip() or (message.epilogue or "").strip():
        raise _mime_invalid("text outside the MIME parts")
    parts = message.get_payload()
    if not isinstance(parts, list) or not parts or len(parts) != len(part_blocks):
        raise _mime_invalid("MIME part structure")
    body_part, attachment_parts = parts[0], parts[1:]
    if (
        body_part.is_multipart()
        or body_part.get_content_type() != "text/plain"
        or body_part.get_content_disposition() == "attachment"
        or body_part.get_filename() is not None
    ):
        raise _unsupported("the first part is not a plain-text body")
    if len(attachment_parts) > da.MAX_ATTACHMENTS:
        raise _fail(
            "raw_eml_attachment_count_exceeded",
            f"at most {da.MAX_ATTACHMENTS} attachments are allowed.",
        )
    body = _exact_text(_decode_text_part(body_part))
    attachments = tuple(_decode_attachment(part, index) for index, part in enumerate(attachment_parts, start=1))
    seen: set[str] = set()
    for index, attachment in enumerate(attachments, start=1):
        key = attachment.filename.casefold()
        if key in seen:
            raise _invalid_attachment(index, "duplicate file name")
        seen.add(key)
    return layout, body, attachments


def _display(message: Any, name: str, fallback: str) -> str:
    try:
        return _squash(str(message.get(name) or "")) or fallback
    except Exception:  # noqa: BLE001 - header decoding is cosmetic; the raw value is the fallback
        return fallback


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def validate_raw_eml(data: Any) -> PreparedRawEml:
    """Validate and normalize ``data`` (the bytes of one ``.eml``); pure, no file system, no mailbox."""
    normalized, form = _decode_and_normalize(data)
    lines = normalized.split("\r\n")[:-1]
    fields, separator = _parse_header_block(lines)
    values = _check_required_headers(fields)
    message_id = _validate_message_id(next(value for name, value in fields if name.casefold() == "message-id"))
    _validate_address_list(values["to"])
    _validate_date(values["date"])
    message = da._parse(normalized.encode("utf-8"))
    layout, body, attachments = _analyse_structure(message, values, lines[separator + 1:])
    if da._all_defects(message):  # defects found while decoding the parts
        raise _mime_invalid("parser defect")
    normalized_bytes = normalized.encode("utf-8")
    summary = {
        "structure": layout,
        "headers": [(name.lower(), _squash(value)) for name, value in fields],
        "body": body,
        "attachments": [da.attachment_meta(item) for item in attachments],
    }
    return PreparedRawEml(
        data=normalized_bytes,
        text=normalized,
        message_id=message_id,
        subject=_display(message, "Subject", values.get("subject", "")),
        to=_display(message, "To", values["to"]),
        date=values["date"],
        attachments=attachments,
        summary=summary,
        line_endings=form,
        source_sha256=hashlib.sha256(data).hexdigest(),
        normalized_sha256=hashlib.sha256(normalized_bytes).hexdigest(),
        size_bytes=len(normalized_bytes),
    )


def load_raw_eml(workspace_root: Any, path: Any) -> PreparedRawEml:
    """Read one ``.eml`` below a data zone (bounded, link-free, single reader) and validate it."""
    segments, zone = da.validate_zone_path(path, label=_LABEL, prefix="raw_eml", error=RawEmlError)
    name = segments[-1]
    if len(name) <= len(RAW_EML_SUFFIX) or not name.lower().endswith(RAW_EML_SUFFIX):
        raise _fail("raw_eml_extension_invalid", f"The {_LABEL} must be a '*.eml' file; refusing.")
    root = da.resolve_workspace_root(workspace_root)
    data = da.read_zone_file(
        root, segments, zone, _LABEL, max_bytes=MAX_RAW_EML_BYTES, prefix="raw_eml", error=RawEmlError
    )
    return dataclasses.replace(validate_raw_eml(data), source_path="/".join(segments))


def compare_stored_raw(stored: bytes, prepared: PreparedRawEml) -> list[str]:
    """Compare a stored message semantically with the prepared one; ``[]`` means every aspect matches.

    The stored bytes must themselves satisfy the whole profile (``stored:<reason>`` otherwise).  The
    codes are content-free: ``structure``, ``header:<name>``, ``header_order``, ``body:text``,
    ``attachment_count:<stored>!=<expected>`` and ``attachment[n]:<filename|content_type|size|sha256>``.
    """
    try:
        other = validate_raw_eml(stored)
    except RawEmlError as exc:
        return [f"stored:{exc.reason_code}"]
    except Exception:  # noqa: BLE001 - never raise from the verification path
        return ["stored:unparseable"]
    expected, actual = prepared.summary, other.summary
    if expected == actual:
        return []
    problems: list[str] = []
    if expected["structure"] != actual["structure"]:
        problems.append("structure")

    def grouped(headers: list[tuple[str, str]]) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for name, value in headers:
            result.setdefault(name, []).append(value)
        return result

    want, got = grouped(expected["headers"]), grouped(actual["headers"])
    for name in sorted(set(want) | set(got)):
        if want.get(name) != got.get(name):
            problems.append(f"header:{name}")
    if not problems and expected["headers"] != actual["headers"]:
        problems.append("header_order")
    if expected["body"] != actual["body"]:
        problems.append("body:text")
    if len(expected["attachments"]) != len(actual["attachments"]):
        problems.append(f"attachment_count:{len(actual['attachments'])}!={len(expected['attachments'])}")
    else:
        for index, (want_meta, got_meta) in enumerate(zip(expected["attachments"], actual["attachments"]), start=1):
            for key in ("filename", "content_type", "size", "sha256"):
                if want_meta[key] != got_meta[key]:
                    problems.append(f"attachment[{index}]:{key}")
    return problems


__all__ = [
    "MAX_HEADER_BLOCK_BYTES",
    "MAX_HEADER_FIELDS",
    "MAX_LINE_BYTES",
    "MAX_RAW_EML_BYTES",
    "PreparedRawEml",
    "RAW_EML_SUFFIX",
    "RawEmlError",
    "compare_stored_raw",
    "load_raw_eml",
    "validate_raw_eml",
]
