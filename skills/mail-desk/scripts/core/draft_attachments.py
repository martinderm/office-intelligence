"""Draft attachments for the standalone draft API: validation, safe reading, binding, read-back.

This module extends ``core.drafts`` (it adds no second transport).  A draft with attachments is built
by ``drafts.build_draft_message`` and appended through the one existing binary-stdin
``message save`` path.  Everything here happens *before* the APPEND (validation, workspace/account/lease
binding, bounded zone-confined file reading, size limits) or *after* it (semantic read-back).

Local files are only ever read and packed -- never executed, unpacked, searched or downloaded.  Error
texts name an attachment by its index and carry no file content, caller path or OS error text.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .drafts import DraftError

#: At most this many attachments per draft.
MAX_ATTACHMENTS = 5

#: At most this many bytes per ORIGINAL file (3 MiB, inclusive).
MAX_ATTACHMENT_BYTES = 3 * 1048576

#: At most this many bytes for the FULL serialized MIME message (10 MiB, inclusive): headers, text,
#: Base64 + CRLF, boundaries.  The stdin transport CRLF is not mail content and is not counted.
MAX_MIME_MESSAGE_BYTES = 10 * 1048576

#: Upper bound for the stored message that the semantic read-back is willing to load.
MAX_READBACK_BYTES = 12 * 1048576

#: The four consumer data zones (exact, lower case, POSIX).
ATTACHMENT_ZONES = ("memory/references", "memory/evidence", "memory/cloud", "data")

#: Runtime state of this desk inside the ``data`` zone; never attachable.
_FORBIDDEN_DATA_SEGMENT = "mail-desk"

MAX_FILENAME_CHARS = 128
MAX_FILENAME_BYTES = 255
_MAX_PATH_CHARS = 1024
_READ_CHUNK = 1024 * 1024

_WIN_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{n}" for n in range(1, 10)}
    | {f"LPT{n}" for n in range(1, 10)}
)
_FILENAME_FORBIDDEN_CHARS = frozenset('/\\:*?"<>|')
_PATH_FORBIDDEN_SEGMENT_CHARS = frozenset('*?"<>|')
_BIDI_AND_SEPARATORS = frozenset(
    [" ", " ", "‎", "‏", "؜"]
    + [chr(c) for c in range(0x202A, 0x202F)]
    + [chr(c) for c in range(0x2066, 0x206A)]
)
_REPARSE_POINT = 0x400

_CONTENT_TYPE_RE = re.compile(
    r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$"
)
_SPEC_KEYS = frozenset({"path", "filename", "content_type"})

#: Deterministic extension table (no ``mimetypes``: it consults the host registry).
_EXTENSION_CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".json": "application/json",
    ".zip": "application/zip",
    ".doc": "application/msword",
    ".xls": "application/vnd.ms-excel",
    ".ppt": "application/vnd.ms-powerpoint",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
DEFAULT_CONTENT_TYPE = "application/octet-stream"


class DraftAttachmentError(DraftError):
    """A fail-closed, content-free attachment validation / reading failure."""


def _fail(reason: str, message: str) -> "DraftAttachmentError":
    return DraftAttachmentError(reason, message)


@dataclass(frozen=True)
class DraftAttachment:
    """One prepared, fully validated attachment (the bytes never appear in ``repr``)."""

    filename: str
    content_type: str
    data: bytes = field(repr=False)
    size: int = 0
    sha256: str = ""


@dataclass(frozen=True)
class AttachmentSpec:
    """A validated, not yet read attachment request."""

    path: str
    segments: tuple[str, ...]
    zone: str
    filename: str
    content_type: str


def attachment_meta(attachment: DraftAttachment) -> dict[str, Any]:
    """Content-free description used by results and the draft journal."""
    return {
        "filename": attachment.filename,
        "content_type": attachment.content_type,
        "size": attachment.size,
        "sha256": attachment.sha256,
    }


# ---------------------------------------------------------------------------
# Static validation
# ---------------------------------------------------------------------------

def _is_reserved_device(name: str) -> bool:
    stem = name.split(".", 1)[0].rstrip(" ").upper()
    return stem in _WIN_RESERVED


def validate_filename(value: Any, *, index: int) -> str:
    """Validate an attachment file name (strict, see the spec); returns it unchanged."""
    label = f"attachment #{index + 1}"
    problem = f"{label} has an invalid file name."
    if not isinstance(value, str) or not value:
        raise _fail("attachment_filename_invalid", problem)
    if len(value) > MAX_FILENAME_CHARS:
        raise _fail("attachment_filename_invalid", problem)
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise _fail("attachment_filename_invalid", problem) from None
    if len(encoded) > MAX_FILENAME_BYTES:
        raise _fail("attachment_filename_invalid", problem)
    for char in value:
        if unicodedata.category(char) in ("Cc", "Cs") or char in _BIDI_AND_SEPARATORS:
            raise _fail("attachment_filename_invalid", problem)
        if char in _FILENAME_FORBIDDEN_CHARS:
            raise _fail("attachment_filename_invalid", problem)
    if value != value.strip() or value.endswith(".") or value in (".", ".."):
        raise _fail("attachment_filename_invalid", problem)
    if _is_reserved_device(value):
        raise _fail("attachment_filename_invalid", problem)
    return value


def validate_content_type(value: Any, *, index: int) -> str:
    """Validate an explicit ``maintype/subtype`` (lower case, no parameters, not multipart/message)."""
    problem = f"attachment #{index + 1} has an invalid content type."
    if not isinstance(value, str) or not _CONTENT_TYPE_RE.fullmatch(value):
        raise _fail("attachment_content_type_invalid", problem)
    if value.split("/", 1)[0] in ("multipart", "message"):
        raise _fail("attachment_content_type_invalid", problem)
    return value


def _default_content_type(filename: str) -> str:
    return _EXTENSION_CONTENT_TYPES.get(Path(filename).suffix.lower(), DEFAULT_CONTENT_TYPE)


def _validate_path(value: Any, *, index: int) -> tuple[tuple[str, ...], str]:
    label = f"attachment #{index + 1}"
    invalid = f"{label} path is not a valid workspace-relative zone path."
    if not isinstance(value, str) or not value or len(value) > _MAX_PATH_CHARS:
        raise _fail("attachment_path_invalid", invalid)
    for char in value:
        if ord(char) < 0x20 or ord(char) == 0x7F or char in ("\\", ":") or unicodedata.category(char) in ("Cc", "Cs"):
            raise _fail("attachment_path_invalid", invalid)
    segments = tuple(value.split("/"))
    for segment in segments:
        if (
            segment in ("", ".", "..")
            or any(char in _PATH_FORBIDDEN_SEGMENT_CHARS for char in segment)
            or segment.endswith((".", " "))
            or _is_reserved_device(segment)
        ):
            raise _fail("attachment_path_invalid", invalid)
    zone = next(
        (z for z in ATTACHMENT_ZONES if segments[: len(z.split("/"))] == tuple(z.split("/"))),
        None,
    )
    if zone is None or len(segments) <= len(zone.split("/")):
        raise _fail("attachment_path_invalid", f"{label} is not a file inside one of the four data zones.")
    if zone == "data" and segments[1].casefold() == _FORBIDDEN_DATA_SEGMENT:
        raise _fail("attachment_path_forbidden", f"{label} is inside the desk's own runtime state; refusing.")
    return segments, zone


def normalize_attachment_specs(specs: Any) -> list[AttachmentSpec]:
    """Validate an attachment list without touching the file system."""
    if isinstance(specs, (str, bytes, Mapping)) or not isinstance(specs, (list, tuple)):
        raise _fail("attachment_spec_invalid", "attachments must be a list of path strings or objects.")
    if len(specs) > MAX_ATTACHMENTS:
        raise _fail(
            "attachment_count_exceeded",
            f"at most {MAX_ATTACHMENTS} attachments are allowed per draft.",
        )
    result: list[AttachmentSpec] = []
    seen_paths: set[str] = set()
    seen_names: set[str] = set()
    for index, entry in enumerate(specs):
        label = f"attachment #{index + 1}"
        if isinstance(entry, str):
            entry = {"path": entry}
        if not isinstance(entry, Mapping) or not entry or "path" not in entry:
            raise _fail("attachment_spec_invalid", f"{label} must be a path string or an object with a 'path'.")
        if not set(entry) <= _SPEC_KEYS:
            raise _fail("attachment_spec_invalid", f"{label} has unsupported keys.")
        if not isinstance(entry["path"], str):
            raise _fail("attachment_spec_invalid", f"{label} 'path' must be a string.")
        for key in ("filename", "content_type"):
            if key in entry and not isinstance(entry[key], str):
                raise _fail("attachment_spec_invalid", f"{label} '{key}' must be a string.")
        segments, zone = _validate_path(entry["path"], index=index)
        filename = validate_filename(entry["filename"] if "filename" in entry else segments[-1], index=index)
        if "content_type" in entry:
            content_type = validate_content_type(entry["content_type"], index=index)
        else:
            content_type = _default_content_type(filename)
        path_key = "/".join(segments).casefold()
        name_key = filename.casefold()
        if path_key in seen_paths or name_key in seen_names:
            raise _fail("attachment_duplicate", f"{label} duplicates an earlier attachment path or file name.")
        seen_paths.add(path_key)
        seen_names.add(name_key)
        result.append(AttachmentSpec("/".join(segments), segments, zone, filename, content_type))
    return result


# ---------------------------------------------------------------------------
# Safe, bounded reading
# ---------------------------------------------------------------------------

def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)


def _identity(info: os.stat_result) -> tuple[int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size)


def _lstat_chain(root: Path, segments: Sequence[str], label: str) -> os.stat_result:
    """``lstat`` every component below the trusted root without resolving anything."""
    current = root
    info: os.stat_result | None = None
    for segment in segments:
        current = current / segment
        try:
            info = os.lstat(current)
        except OSError:
            raise _fail("attachment_unreadable", f"{label} could not be read.") from None
        if _is_link(info):
            raise _fail("attachment_link_refused", f"{label} path crosses a symlink or junction; refusing.")
    assert info is not None
    return info


def _read_one(root: Path, spec: AttachmentSpec, index: int) -> DraftAttachment:
    label = f"attachment #{index + 1}"
    unreadable = f"{label} could not be read."
    changed = f"{label} changed while it was read; refusing."
    pre = _lstat_chain(root, spec.segments, label)
    if stat.S_ISDIR(pre.st_mode) or not stat.S_ISREG(pre.st_mode):
        raise _fail("attachment_not_regular_file", f"{label} is not a regular file.")
    if pre.st_ino == 0 and pre.st_dev == 0:
        raise _fail("attachment_unreadable", f"{label} identity cannot be established; refusing.")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    path = root.joinpath(*spec.segments)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise _fail("attachment_unreadable", unreadable) from None
    try:
        handle_info = os.fstat(descriptor)
        # The opened HANDLE must be the very file the unresolved chain pointed at, proven before the
        # first byte is read.  A directory/file swapped for a link (or any other file) fails here.
        if not stat.S_ISREG(handle_info.st_mode):
            raise _fail("attachment_not_regular_file", f"{label} is not a regular file.")
        if _identity(handle_info) != _identity(pre):
            raise _fail("attachment_changed", changed)
        _lstat_chain(root, spec.segments, label)
        _assert_inside_zone(root, path, spec, label)
        size = handle_info.st_size
        if size == 0:
            raise _fail("attachment_empty", f"{label} is empty.")
        if size > MAX_ATTACHMENT_BYTES:
            raise _fail("attachment_too_large", f"{label} exceeds the {MAX_ATTACHMENT_BYTES} byte file limit.")
        chunks: list[bytes] = []
        remaining = size + 1  # one byte more than announced detects growth
        total = 0
        try:
            while remaining > 0:
                block = os.read(descriptor, min(_READ_CHUNK, remaining))
                if not block:
                    break
                chunks.append(block)
                total += len(block)
                remaining -= len(block)
        except OSError:
            raise _fail("attachment_unreadable", unreadable) from None
        if total != size:
            raise _fail("attachment_changed", changed)
        data = b"".join(chunks)
    finally:
        os.close(descriptor)
    # Re-prove the chain and the identity after reading (a mid-read swap leaves a trace).
    post = _lstat_chain(root, spec.segments, label)
    if _identity(post) != _identity(handle_info):
        raise _fail("attachment_changed", changed)
    return DraftAttachment(
        filename=spec.filename,
        content_type=spec.content_type,
        data=data,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def _assert_inside_zone(root: Path, path: Path, spec: AttachmentSpec, label: str) -> None:
    """Resolved location must still be below ``<root>/<zone>`` and outside the desk's runtime state."""
    try:
        real = Path(os.path.realpath(path))
        zone_root = Path(os.path.realpath(root.joinpath(*spec.zone.split("/"))))
        relative = Path(os.path.normcase(real)).relative_to(Path(os.path.normcase(zone_root)))
    except (OSError, ValueError):
        raise _fail("attachment_path_invalid", f"{label} resolves outside its data zone; refusing.") from None
    if spec.zone == "data" and relative.parts and relative.parts[0].casefold() == _FORBIDDEN_DATA_SEGMENT:
        raise _fail("attachment_path_forbidden", f"{label} resolves into the desk's runtime state; refusing.")


def resolve_workspace_root(workspace_root: Any) -> Path:
    """Resolve the trusted workspace anchor (the root itself may sit behind a link)."""
    try:
        resolved = Path(workspace_root).expanduser().resolve(strict=True)
    except (OSError, TypeError, ValueError, RuntimeError):
        raise _fail("draft_binding_failed", "The workspace root does not exist or is not usable; refusing.") from None
    if not resolved.is_dir():
        raise _fail("draft_binding_failed", "The workspace root is not a directory; refusing.")
    return resolved


def load_attachments(workspace_root: Any, specs: Any) -> tuple[DraftAttachment, ...]:
    """Validate ``specs`` and read the files: bounded, zone-confined, link-free, one file at a time."""
    normalized = normalize_attachment_specs(specs)
    if not normalized:
        return ()
    root = resolve_workspace_root(workspace_root)
    return tuple(_read_one(root, spec, index) for index, spec in enumerate(normalized))


# ---------------------------------------------------------------------------
# Workspace / account / lease binding
# ---------------------------------------------------------------------------

DRAFT_BINDING_FAILED_REASON = "draft_binding_failed"
DRAFT_LOCK_MISSING_REASON = "draft_lock_missing"


def bind_attachment_mode(
    *,
    workspace_root: Any,
    journal: Any,
    account: str | None,
    lease_id: str | None = None,
    conversation_id: str | None = None,
    lock_root: Any = None,
) -> tuple[Path, str]:
    """Bind an attachment-mode operation to workspace, journal, real lease and account.

    Order is deliberate: journal/workspace consistency, then the real shared lease guard
    (``allow_legacy=False``), then the account bound in ``.agents/mail-desk-backend.json``.
    Nothing is read from or written to the mailbox or the zones before all three hold.
    Returns ``(resolved workspace root, effective account)``.
    """
    if workspace_root is None or journal is None:
        raise _fail(
            DRAFT_BINDING_FAILED_REASON,
            "Attachment drafts require an explicit workspace_root and a draft journal; refusing.",
        )
    root = resolve_workspace_root(workspace_root)
    try:
        journal_dir = Path(journal.data_dir).resolve()
    except (OSError, AttributeError, TypeError):
        journal_dir = None
    if journal_dir is None or os.path.normcase(str(journal_dir)) != os.path.normcase(
        str(root / "data" / "mail-desk")
    ):
        raise _fail(
            DRAFT_BINDING_FAILED_REASON,
            "The draft journal does not live in <workspace_root>/data/mail-desk; refusing.",
        )
    from . import diagnostics  # lazy: diagnostics imports the draft builder

    effective_lease = lease_id if lease_id is not None else (os.environ.get("WORKSPACE_LOCK_LEASE_ID") or None)
    effective_conversation = (
        conversation_id if conversation_id is not None else (os.environ.get("WORKSPACE_LOCK_CONVERSATION_ID") or None)
    )
    try:
        diagnostics.require_owned_workspace_lock(
            root, lease_id=effective_lease, conversation_id=effective_conversation, lock_root=lock_root
        )
    except Exception:  # noqa: BLE001 - classified fail-closed boundary, no guard text is echoed
        raise _fail(
            DRAFT_LOCK_MISSING_REASON,
            "The workspace lock is missing or not owned by this invocation; nothing was read or appended.",
        ) from None
    from .readiness import bind_workspace_account

    binding = bind_workspace_account(root / "data" / "mail-desk", requested_account=account)
    if not binding.get("ok") or binding.get("account") is None or not str(binding["account"]).strip():
        raise _fail(
            DRAFT_BINDING_FAILED_REASON,
            "The workspace has no explicitly bound mail account, or it differs from the requested one; refusing.",
        )
    return root, str(binding["account"]).strip()


# ---------------------------------------------------------------------------
# Semantic read-back
# ---------------------------------------------------------------------------

def _squash(value: Any) -> str:
    return " ".join(str(value or "").split())


def _normalize_text(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")


def _parse(raw: bytes) -> Any:
    import email
    import email.policy

    return email.message_from_bytes(raw, policy=email.policy.SMTPUTF8)


def split_parts(message: Any) -> tuple[Any, list[Any]]:
    """Return ``(plain body part or None, every other leaf part)`` of a parsed message."""
    leaves = [part for part in message.walk() if not part.is_multipart()]
    body = None
    others: list[Any] = []
    for part in leaves:
        if (
            body is None
            and part.get_content_type() == "text/plain"
            and part.get_content_disposition() != "attachment"
            and part.get_filename() is None
        ):
            body = part
        else:
            others.append(part)
    return body, others


#: Headers that must occur at most once (a second occurrence makes the identity / recipients ambiguous).
_SINGLETON_HEADERS = ("Message-ID", "Subject", "To", "Cc", "From", "Date", "MIME-Version", "Content-Type")

#: Recipient / routing headers a draft built by this module never carries.
_NEVER_EXPECTED_HEADERS = ("Bcc", "Sender", "Reply-To")


def _all_defects(message: Any) -> list[Any]:
    """Parser defects of the message and every part (including those found while decoding)."""
    defects: list[Any] = []
    for part in message.walk():
        defects.extend(getattr(part, "defects", []) or [])
    return defects


def _header_count(message: Any, name: str) -> int:
    return len(message.get_all(name) or [])


def describe_stored_message(raw: bytes) -> tuple[str, str, int, bool]:
    """Return ``(normalized Message-ID, subject, number of non-body parts, defective)``.

    Raises ``ValueError`` unless the message has exactly one ``Message-ID`` header: an ambiguous
    identity is never usable.  ``defective`` is ``True`` when the tolerant parser recorded any
    defect, so a caller must not derive "no attachments" from such a structure.
    """
    from .common import normalize_message_id

    message = _parse(raw)
    if _header_count(message, "Message-ID") != 1:
        raise ValueError("ambiguous or missing Message-ID header")
    _, others = split_parts(message)
    return (
        normalize_message_id(str(message.get("Message-ID", "") or "")),
        _squash(message.get("Subject")),
        len(others),
        bool(_all_defects(message)),
    )


def compare_stored_message(
    raw: bytes,
    *,
    message_id: str,
    subject: str,
    to: str,
    body: str,
    cc: str | None = None,
    from_addr: str | None = None,
    date: str | None = None,
    attachments: Sequence[DraftAttachment] = (),
) -> list[str]:
    """Compare a stored message semantically with the intended draft.

    Returns a list of content-free problem codes; empty means every compared aspect matches:
    parser defects (none allowed), duplicate identity / recipient headers, expected headers
    (Message-ID, Subject, To, Cc, From, Date), absence of unintended recipient headers (Cc / From when
    not intended, Bcc, Sender, Reply-To, Resent-*), plain text, MIME structure (flat
    ``multipart/mixed`` exactly when attachments are intended), attachment count, and per attachment
    file name, content type, ``attachment`` disposition and the decoded bytes (length and SHA-256).
    This is a semantic comparison, not byte equality of arbitrary raw messages.
    """
    from .common import normalize_message_id

    try:
        message = _parse(raw)
    except Exception:  # noqa: BLE001
        return ["unparseable"]
    problems: list[str] = []
    for name in _SINGLETON_HEADERS:
        if _header_count(message, name) > 1:
            problems.append(f"header_duplicate:{name}")
    if normalize_message_id(str(message.get("Message-ID", "") or "")) != normalize_message_id(message_id):
        problems.append("header:Message-ID")
    for name, expected in (("Subject", subject), ("To", to)):
        if _squash(message.get(name)) != _squash(expected):
            problems.append(f"header:{name}")
    for name, expected in (("Cc", cc), ("From", from_addr)):
        present = _header_count(message, name) > 0
        if expected:
            if _squash(message.get(name)) != _squash(expected):
                problems.append(f"header:{name}")
        elif present:
            problems.append(f"unexpected_header:{name}")
    if date is not None and _squash(message.get("Date")) != _squash(date):
        problems.append("header:Date")
    for name in _NEVER_EXPECTED_HEADERS:
        if _header_count(message, name):
            problems.append(f"unexpected_header:{name}")
    if any(str(key).lower().startswith("resent-") for key in message.keys()):
        problems.append("unexpected_header:Resent-*")
    wants_multipart = len(attachments) > 0
    if wants_multipart:
        if message.get_content_type() != "multipart/mixed":
            problems.append("structure:top_level")
    elif message.is_multipart():
        problems.append("structure:top_level")
    if any(part.is_multipart() for part in message.walk() if part is not message):
        problems.append("structure:nested_multipart")
    body_part, others = split_parts(message)
    if body_part is None:
        problems.append("body:missing")
    else:
        try:
            if _normalize_text(body_part.get_content()) != _normalize_text(str(body)):
                problems.append("body:text")
        except Exception:  # noqa: BLE001
            problems.append("body:undecodable")
    if len(others) != len(attachments):
        problems.append(f"attachment_count:{len(others)}!={len(attachments)}")
        return problems
    for index, (part, expected_att) in enumerate(zip(others, attachments), start=1):
        tag = f"attachment[{index}]"
        if part.get_filename() != expected_att.filename:
            problems.append(f"{tag}:filename")
        if part.get_content_type() != expected_att.content_type:
            problems.append(f"{tag}:content_type")
        if part.get_content_disposition() != "attachment":
            problems.append(f"{tag}:disposition")
        try:
            payload = part.get_payload(decode=True)
        except Exception:  # noqa: BLE001
            payload = None
        if not isinstance(payload, bytes) or len(payload) != expected_att.size:
            problems.append(f"{tag}:size")
        elif hashlib.sha256(payload).hexdigest() != expected_att.sha256:
            problems.append(f"{tag}:sha256")
    if _all_defects(message):
        problems.append("mime_defect")
    return problems


__all__ = [
    "ATTACHMENT_ZONES",
    "AttachmentSpec",
    "DEFAULT_CONTENT_TYPE",
    "DraftAttachment",
    "DraftAttachmentError",
    "MAX_ATTACHMENTS",
    "MAX_ATTACHMENT_BYTES",
    "MAX_MIME_MESSAGE_BYTES",
    "MAX_READBACK_BYTES",
    "attachment_meta",
    "bind_attachment_mode",
    "compare_stored_message",
    "describe_stored_message",
    "load_attachments",
    "split_parts",
    "normalize_attachment_specs",
    "resolve_workspace_root",
    "validate_content_type",
    "validate_filename",
]
