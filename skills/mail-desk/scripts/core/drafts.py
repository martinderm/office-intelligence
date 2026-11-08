"""First-class IMAP draft operations (save / delete / replace) for mail-desk.

This is a standalone client feature.  There is deliberately **no approval gate**:
a draft is stored unsent, and sending (SMTP) is out of scope.  The batch-runner
contract is never touched -- the draft journal is a separate file with its own
schema.

Draft creation uses the Himalaya ``message save`` command (IMAP APPEND).  The
raw RFC-822 message is built with the ``SMTPUTF8`` email policy (CRLF line
endings) and passed through the binary stdin pipe -- never as an argv literal.
Himalaya 1.2.0 (rev ``1b70c4e0eaa72dee48353f0211e6cc0f0776fe98``,
``src/email/message/command/save.rs``) ignores the raw ``MESSAGE`` argument
whenever stdin is not a TTY and the output format is not JSON: it reads stdin
with Rust ``lines()``, joins the lines with CRLF and drops the final line
terminator.  ``run_himalaya`` starts the process with a non-TTY stdin, so every
draft size must use stdin (threshold 0); an argv payload would be ignored and
would additionally trip the Windows command-line limit (``WinError 206``) for
large drafts.  ``save_draft`` compensates the upstream normalization with one
extra transport CRLF, so the appended message equals the canonical builder raw
byte for byte.  Non-ASCII subject / recipient / body bytes are preserved without
a 7-bit downgrade.  The appended message is then located through the
canonical ``verify_in_target_folder`` seam, and the returned envelope carries
``{envelope_id, folder, subject, to, date}`` analogous to
``get_single_email_details``.

The target Drafts folder is resolved dynamically from the live
``folder list -o json`` catalog with a documented, deterministic fallback chain:

1. a folder whose catalog entry advertises the RFC 6154 ``\\Drafts``
   special-use attribute (any of ``attributes``/``flags``/``special_use``/
   ``description``/``desc``).  This tier is a forward-compatible, deterministic
   check rather than a live data source on Himalaya 1.2.0: ``folder list``
   exposes only ``-a``/``-w``/``-o`` (verified against ``folder list --help``)
   and its JSON payload carries folder names only;
2. a folder named ``Drafts`` (the conventional name);
3. the GroupWise convention ``Work In Progress``.

If none resolves, the operation fails loudly with
``DraftsFolderUnresolvedError`` (reason ``drafts_folder_unresolved``) -- there is
no silent fallback to a hard-coded folder name.

Deletion never performs a UID MOVE: it marks the message ``\\Deleted`` in place
(``flag add deleted`` -- Himalaya 1.2.0's ``message delete`` would *move* a
non-trash message to the trash folder) and then expunges the folder
(``folder expunge``).  ``replace_draft`` appends the replacement draft first and
only then deletes the old one; the crash window between the two steps (both
drafts observable, since ``message save`` already committed the new draft before
the delete ran) is documented by ``REPLACE_CRASH_WINDOW``.  With a journal the
entry naming both envelope ids is written *before* the delete step starts, so a
crash from the delete step onward is reconcile-visible; the one residual window
is a crash after the APPEND commits but before that journal write (see
``REPLACE_CRASH_WINDOW``).
"""

from __future__ import annotations

import email.utils
import json
import re
import unicodedata
from email.message import EmailMessage
from email.policy import SMTPUTF8
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from . import himalaya
from .common import atomic_write_json, normalize_message_id, utc_now_iso

#: RFC 6154 special-use attribute that marks the drafts mailbox.
DRAFTS_SPECIAL_USE_ATTRIBUTE = "\\Drafts"

#: Catalog entry keys that may carry a special-use attribute or description.
DRAFTS_SPECIAL_USE_KEYS = (
    "attributes",
    "flags",
    "special_use",
    "special-use",
    "special_use_attributes",
    "description",
    "desc",
)

#: Deterministic name fallback chain (conventional name first, GroupWise last).
DRAFTS_FOLDER_NAME_CANDIDATES = ("Drafts", "Work In Progress")

#: Bounded reason code for an unresolvable Drafts folder.
DRAFTS_FOLDER_UNRESOLVED_REASON = "drafts_folder_unresolved"

#: Bounded reason code for a draft that could not be located after APPEND.
DRAFT_VERIFICATION_FAILED_REASON = "draft_verification_failed"

#: Bounded reason code for an unreadable draft journal.
DRAFT_JOURNAL_CORRUPTED_REASON = "draft_journal_corrupted"

#: Bounded reason code for a failed draft APPEND (classified, fail-loud).
DRAFT_SAVE_FAILED_REASON = "draft_save_failed"

#: SMTPUTF8 policy with canonical CRLF line endings for the wire/APPEND bytes.
SMTPUTF8_CRLF = SMTPUTF8.clone(linesep="\r\n")

#: One transport CRLF compensates Himalaya's ``lines()``/CRLF-join path, which
#: drops the final line terminator of the canonical raw message.
_STDIN_TRANSPORT_TERMINATOR = b"\r\n"

#: Documented crash window of the new-then-delete replace order.
#:
#: Coverage: with a journal, ``replace_draft`` records the entry naming both
#: envelope ids *before* the delete step starts, so any crash from that point on
#: (including the delete-side ``\Deleted``/EXPUNGE window) leaves a
#: reconcile-visible journal entry.  Residual window: a crash after the APPEND has
#: committed but before the journal entry is written leaves the appended draft
#: without a journal entry.
REPLACE_CRASH_WINDOW = (
    "Replace appends the new draft before deleting the old one; a crash between the two steps "
    "leaves both drafts observable in the Drafts folder. With a journal the entry naming both "
    "envelope ids is recorded before the delete step starts, so a crash from the delete step onward "
    "is reconcile-visible; the residual window is a crash after the APPEND has committed but before "
    "that journal entry is written, leaving the appended draft without a journal entry."
)

#: Himalaya system flag that sets ``\Deleted`` in place (no UID MOVE).
DELETED_SYSTEM_FLAG = "deleted"

#: Draft journal file name (distinct from ``batch-recovery-journal.json``).
DRAFT_JOURNAL_FILENAME = "draft-journal.json"

#: Draft journal schema version.
DRAFT_JOURNAL_SCHEMA_VERSION = 1

#: Terminal draft-journal run statuses.
DRAFT_JOURNAL_TERMINAL_STATUSES = {"completed", "failed"}

#: Bounded number of folder names echoed in an unresolved-folder error.
_MAX_FOLDER_NAMES_IN_ERROR = 20


class DraftError(RuntimeError):
    """A fail-closed, classified draft-operation failure."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class DraftsFolderUnresolvedError(DraftError):
    """The Drafts folder could not be resolved from the live folder list."""


class DraftJournalCorruptedError(DraftError):
    """The draft journal is unreadable or structurally invalid."""


class DraftPartialError(DraftError):
    """A partial outcome that must be reported honestly.

    ``new_envelope`` carries the envelope contract of a draft that WAS appended (and, where stated,
    verified) when a later step failed; it is ``None`` when nothing usable exists.
    """

    def __init__(self, reason_code: str, message: str, new_envelope: dict[str, Any] | None = None) -> None:
        super().__init__(reason_code, message)
        self.new_envelope = new_envelope


#: Bounded reason codes of the attachment-mode save / replace paths.
DRAFT_APPEND_OUTCOME_UNKNOWN_REASON = "draft_append_outcome_unknown"
DRAFT_JOURNAL_WRITE_FAILED_REASON = "draft_journal_write_failed"
DRAFT_MESSAGE_ID_NOT_UNIQUE_REASON = "draft_message_id_not_unique"
DRAFT_REPLACE_WOULD_DROP_ATTACHMENTS_REASON = "draft_replace_would_drop_attachments"
DRAFT_OLD_UNREADABLE_REASON = "draft_old_unreadable"
DRAFT_OLD_IDENTITY_MISMATCH_REASON = "draft_old_identity_mismatch"
DRAFT_REPLACE_OLD_NOT_DELETED_REASON = "draft_replace_old_not_deleted"

#: ``run_himalaya`` reason codes after which an APPEND may nevertheless have been committed.
_UNKNOWN_APPEND_REASONS = frozenset(
    {"himalaya_timeout", "himalaya_transient", "himalaya_launch_failed", "himalaya_stdin_failed"}
)

#: Sentinel: the ``attachments`` argument of ``replace_draft`` was omitted (not "no attachments").
ATTACHMENTS_UNSPECIFIED: Any = object()


def _parse_folder_list(raw: str) -> list[Any]:
    """Parse a ``folder list -o json`` payload, tolerating a leading log line."""
    text = raw
    if "[" in text:
        text = text[text.find("["):]
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("Himalaya folder list did not return a JSON list.")
    return data


def list_folders(account: str | None = None) -> list[Any]:
    """Return the live mailbox folder catalog entries."""
    raw = himalaya.run_himalaya(["folder", "list", "-o", "json"], account=account)
    return _parse_folder_list(raw)


def _folder_name(entry: Any) -> str:
    if isinstance(entry, dict):
        return str(entry.get("name", "") or "")
    return str(entry or "")


def _special_use_tokens(entry: Any) -> list[str]:
    """Collect candidate special-use tokens from a catalog entry."""
    if not isinstance(entry, dict):
        return []
    tokens: list[str] = []
    for key in DRAFTS_SPECIAL_USE_KEYS:
        value = entry.get(key)
        if isinstance(value, str):
            tokens.extend(re.split(r"[\s,;]+", value))
        elif isinstance(value, (list, tuple, set)):
            tokens.extend(str(item) for item in value)
    return [token for token in tokens if token]


def resolve_drafts_folder(
    account: str | None = None,
    *,
    folders: Iterable[Any] | None = None,
) -> str:
    """Resolve the Drafts folder from the live catalog, or fail loudly.

    ``folders`` may be passed to reuse an already-fetched catalog; otherwise the
    live ``folder list -o json`` catalog is queried.  The deterministic chain is
    the ``\\Drafts`` special-use attribute, then the conventional ``Drafts`` name,
    then the GroupWise ``Work In Progress`` name.  An unresolvable mailbox raises
    ``DraftsFolderUnresolvedError``; no folder name is ever hard-coded.
    """
    if folders is None:
        try:
            folders = list_folders(account=account)
        except Exception as exc:  # noqa: BLE001 - classified fail-loud boundary
            raise DraftsFolderUnresolvedError(
                DRAFTS_FOLDER_UNRESOLVED_REASON,
                "Could not list mailbox folders to resolve the Drafts folder: "
                f"{str(exc)[:300]}",
            ) from exc

    catalog = list(folders)

    # Tier 1: the RFC 6154 \Drafts special-use attribute.
    for entry in catalog:
        name = _folder_name(entry)
        if not name:
            continue
        for token in _special_use_tokens(entry):
            if token.strip().lower() == DRAFTS_SPECIAL_USE_ATTRIBUTE.lower():
                return name

    # Tier 2/3: the documented, conventional name fallback chain.
    by_lower_name = {_folder_name(entry).strip().lower(): _folder_name(entry) for entry in catalog if _folder_name(entry)}
    for candidate in DRAFTS_FOLDER_NAME_CANDIDATES:
        found = by_lower_name.get(candidate.strip().lower())
        if found:
            return found

    available = sorted({_folder_name(entry) for entry in catalog if _folder_name(entry)})
    raise DraftsFolderUnresolvedError(
        DRAFTS_FOLDER_UNRESOLVED_REASON,
        "No Drafts folder could be resolved from the live folder list: no "
        f"{DRAFTS_SPECIAL_USE_ATTRIBUTE} attribute and none of "
        f"{list(DRAFTS_FOLDER_NAME_CANDIDATES)} present; refusing a hard-coded fallback. "
        f"Available folders: {available[:_MAX_FOLDER_NAMES_IN_ERROR]}",
    )


#: Bounded reason codes of the header / Message-ID validation (always content-free).
DRAFT_HEADER_INVALID_REASON = "draft_header_invalid"
DRAFT_MESSAGE_ID_INVALID_REASON = "draft_message_id_invalid"

#: Control characters, line/paragraph separators and surrogates never belong into a header value.
_HEADER_FORBIDDEN_CHARACTERS = frozenset("  ")

_MESSAGE_ID_ATOM = r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+"
_MESSAGE_ID_RE = re.compile(
    rf"(?:<(?P<l1>{_MESSAGE_ID_ATOM})@(?P<d1>[A-Za-z0-9.-]+)>|(?P<l2>{_MESSAGE_ID_ATOM})@(?P<d2>[A-Za-z0-9.-]+))"
)
_MAX_MESSAGE_ID_CHARS = 255


def _check_header_value(name: str, value: object) -> str:
    """Return ``str(value)`` or fail closed when it could break out of its header line."""
    text = str(value)
    for char in text:
        if (
            unicodedata.category(char) in ("Cc", "Cs")
            or char in _HEADER_FORBIDDEN_CHARACTERS
        ):
            raise DraftError(
                DRAFT_HEADER_INVALID_REASON,
                f"The '{name}' header contains a control, separator or invalid character; refusing.",
            )
    return text


def _check_supplied_message_id(value: str) -> str:
    text = str(value)
    if len(text) > _MAX_MESSAGE_ID_CHARS or _MESSAGE_ID_RE.fullmatch(text) is None:
        raise DraftError(
            DRAFT_MESSAGE_ID_INVALID_REASON,
            "The supplied Message-ID is not a plain <local@domain> identifier; refusing.",
        )
    return text


def _attach_parts(message: EmailMessage, attachments: Iterable[Any]) -> None:
    """Add validated attachment parts (Base64 via the standard library, always ``attachment``)."""
    from . import draft_attachments as da

    prepared = tuple(attachments)
    if len(prepared) > da.MAX_ATTACHMENTS:
        raise da.DraftAttachmentError(
            "attachment_count_exceeded", f"at most {da.MAX_ATTACHMENTS} attachments are allowed per draft."
        )
    encoded_lower_bound = 0
    for index, item in enumerate(prepared):
        if not isinstance(item, da.DraftAttachment) or not isinstance(item.data, bytes):
            raise da.DraftAttachmentError(
                "attachment_spec_invalid", f"attachment #{index + 1} is not a prepared attachment."
            )
        da.validate_filename(item.filename, index=index)
        da.validate_content_type(item.content_type, index=index)
        if item.size > da.MAX_ATTACHMENT_BYTES or len(item.data) > da.MAX_ATTACHMENT_BYTES:
            raise da.DraftAttachmentError(
                "attachment_too_large", f"attachment #{index + 1} exceeds the {da.MAX_ATTACHMENT_BYTES} byte file limit."
            )
        encoded_lower_bound += 4 * ((len(item.data) + 2) // 3)
    if encoded_lower_bound > da.MAX_MIME_MESSAGE_BYTES:
        raise da.DraftAttachmentError(
            "message_too_large",
            f"the Base64 attachments alone exceed the {da.MAX_MIME_MESSAGE_BYTES} byte message limit.",
        )
    for item in prepared:
        maintype, subtype = item.content_type.split("/", 1)
        message.add_attachment(item.data, maintype=maintype, subtype=subtype, filename=item.filename)


def build_draft_message(
    subject: str,
    to: str,
    body: str,
    *,
    from_addr: str | None = None,
    cc: str | None = None,
    date: str | None = None,
    message_id: str | None = None,
    attachments: Iterable[Any] = (),
) -> tuple[str, str, str]:
    """Build a UTF-8 (SMTPUTF8) raw RFC-822 draft message with CRLF lines.

    Returns ``(raw_message, normalized_message_id, date_header)``.  Headers are
    written as raw UTF-8 (no RFC 2047 encoded-word downgrade); the body is
    carried as a UTF-8 payload whose transfer encoding is chosen by the policy
    (``Content-Transfer-Encoding``), so no content is mangled.  The raw message
    uses canonical CRLF line endings and ends with a final CRLF, ready for the
    SMTP/IMAP wire and for the binary stdin transport.

    Header values and a supplied ``message_id`` are validated strictly
    (``draft_header_invalid`` / ``draft_message_id_invalid``).  ``attachments``
    are prepared ``DraftAttachment`` objects (see ``core.draft_attachments``):
    the message becomes ``multipart/mixed`` with the plain text first and one
    Base64 ``attachment`` part each; its full serialized length must not exceed
    ``MAX_MIME_MESSAGE_BYTES`` (``message_too_large``).  Without attachments the
    output is byte-identical to the former builder.
    """
    subject_text = _check_header_value("Subject", subject)
    to_text = _check_header_value("To", to)
    cc_text = _check_header_value("Cc", cc) if cc else None
    from_text = _check_header_value("From", from_addr) if from_addr else None
    date_text = _check_header_value("Date", date) if date else None
    generated_id = _check_supplied_message_id(message_id) if message_id is not None else email.utils.make_msgid()
    normalized_id = normalize_message_id(generated_id)
    date_header = date_text or email.utils.formatdate(localtime=True)

    message = EmailMessage(policy=SMTPUTF8_CRLF)
    if from_text:
        message["From"] = from_text
    message["To"] = to_text
    if cc_text:
        message["Cc"] = cc_text
    message["Subject"] = subject_text
    message["Date"] = date_header
    message["Message-ID"] = generated_id if str(generated_id).startswith("<") else f"<{generated_id}>"
    message.set_content(str(body), subtype="plain", charset="utf-8")
    prepared = tuple(attachments)
    if prepared:
        _attach_parts(message, prepared)

    raw_message = message.as_string()
    if not raw_message.endswith("\r\n"):
        raw_message += "\r\n"

    # The full serialized message is bounded whether or not it carries attachments.
    from . import draft_attachments as da

    if len(raw_message.encode("utf-8")) > da.MAX_MIME_MESSAGE_BYTES:
        raise da.DraftAttachmentError(
            "message_too_large",
            f"the serialized MIME message exceeds the {da.MAX_MIME_MESSAGE_BYTES} byte limit; "
            "nothing was truncated, split or uploaded.",
        )

    return raw_message, normalized_id, date_header


def _resolve_saved_envelope(
    folder: str,
    message_id: str,
    subject: str,
    to: str,
    date_header: str,
    account: str | None,
) -> dict[str, Any]:
    """Locate the just-appended draft and build its envelope contract."""
    envelope_id = himalaya.verify_in_target_folder(
        folder, message_id, subject=str(subject), account=account
    )
    if not envelope_id:
        raise DraftError(
            DRAFT_VERIFICATION_FAILED_REASON,
            f"Appended draft could not be located in folder '{folder}' by Message-ID; "
            "refusing to report an unverified draft.",
        )
    return {
        "envelope_id": str(envelope_id),
        "folder": folder,
        "subject": str(subject),
        "to": str(to),
        "date": str(date_header),
        "message_id": message_id,
    }


def _try_attempt(journal: "DraftJournal", message_id: str, **fields: Any) -> bool:
    """Best-effort attempt update after the APPEND; ``False`` when the journal write failed."""
    try:
        journal.record_attempt(message_id, **fields)
    except OSError:
        return False
    return True


def _append_and_verify(
    *,
    raw_message: str,
    normalized_id: str,
    date_header: str,
    subject: str,
    to: str,
    body: str,
    cc: str | None,
    from_addr: str | None,
    prepared: tuple[Any, ...],
    folder: str,
    account: str,
    journal: "DraftJournal",
    action: str,
    record_item: bool,
    extra_attempt: dict[str, Any] | None = None,
    verify: Callable[[bytes], list[str]] | None = None,
    extra_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Verified-APPEND core: unique-ID check, journaled intent, ONE APPEND, semantic read-back.

    Callers: attachment-mode ``save_draft``, ``replace_draft`` (with an explicit attachment list
    or a provably text-only old draft; both through ``_finish_replace``) and ``save_raw_draft``
    (Raw-EML import).  It is deliberately *not* the path of the text-only ``save_draft`` (which
    keeps its former single APPEND with envelope lookup) nor of the opt-in ``diagnose-draft``
    (``core.diagnostics`` owns its own bounded APPEND); all of them share the one stdin transport
    ``himalaya.run_himalaya(..., stdin_bytes=...)``.

    Never retries, never reports success without a semantic match of the single stored message and
    never lets backend text (which could echo content) into an exception: causes are not chained and
    only fixed reason codes appear in messages.  ``verify`` replaces the builder-draft comparison by
    another semantic comparison returning content-free problem codes (Raw-EML import: the stored
    message is compared with the prepared one); ``extra_result`` is merged into the success envelope.
    """
    from . import draft_attachments as da

    meta = [da.attachment_meta(item) for item in prepared]
    size_bytes = len(raw_message.encode("utf-8"))
    try:
        existing, complete = himalaya.find_envelopes_by_message_id(
            folder, normalized_id, subject=subject, account=account, reserve=1
        )
    except Exception:  # noqa: BLE001 - classified below without backend text
        existing, complete = [], False
    if existing or not complete:
        raise DraftError(
            DRAFT_MESSAGE_ID_NOT_UNIQUE_REASON,
            f"The Message-ID is already present in folder '{folder}' or its absence cannot be established; "
            "nothing was appended.",
        )
    attempt_fields: dict[str, Any] = {
        "action": action,
        "folder": folder,
        "subject": subject,
        "to": to,
        "date": date_header,
        "size_bytes": size_bytes,
        "attachment_count": len(meta),
        "attachments": meta,
        **(extra_attempt or {}),
    }
    try:
        journal.record_attempt(normalized_id, status="append_started", **attempt_fields)
    except OSError:
        raise DraftError(
            DRAFT_JOURNAL_WRITE_FAILED_REASON,
            "The draft journal could not be written before the APPEND; nothing was appended.",
        ) from None

    stdin_payload = raw_message.encode("utf-8") + _STDIN_TRANSPORT_TERMINATOR
    outcome: tuple[str, str] | None = None
    try:
        himalaya.run_himalaya(
            ["message", "save", "-o", "plain", "-f", folder],
            account=account,
            stdin_bytes=stdin_payload,
            max_retries=1,
        )
    except himalaya.HimalayaInvocationError as exc:
        reason = str(getattr(exc, "reason_code", "unknown"))[:64]
        outcome = ("unknown" if reason in _UNKNOWN_APPEND_REASONS else "failed", reason)
    except ValueError:
        outcome = ("failed", "request_invalid")
    except Exception:  # noqa: BLE001 - any other backend exception leaves the outcome unknown
        outcome = ("unknown", "backend_exception")
    if outcome is not None:
        kind, reason = outcome
        journaled = _try_attempt(
            journal,
            normalized_id,
            status="append_failed" if kind == "failed" else "append_outcome_unknown",
            reason=reason,
        )
        suffix = "" if journaled else " The journal could not be updated; reconcile by Message-ID manually."
        if kind == "failed":
            raise DraftError(
                DRAFT_SAVE_FAILED_REASON,
                f"Draft APPEND to folder '{folder}' failed ({reason}); nothing was verified or deleted." + suffix,
            )
        raise DraftError(
            DRAFT_APPEND_OUTCOME_UNKNOWN_REASON,
            f"Draft APPEND to folder '{folder}' has an UNKNOWN outcome ({reason}); it was not retried and the "
            f"draft may exist. Message-ID {normalized_id} is journaled for reconciliation." + suffix,
        )

    envelope_id: str | None = None
    problems: list[str]
    try:
        found, complete = himalaya.find_envelopes_by_message_id(
            folder, normalized_id, subject=subject, account=account
        )
        if len(found) != 1 or not complete:
            problems = [f"lookup:found={len(found)}:complete={complete}"]
        else:
            stored = himalaya.fetch_raw_message_eml(
                found[0], folder=folder, account=account, max_bytes=da.MAX_READBACK_BYTES
            )
            if verify is not None:
                problems = verify(stored)
            else:
                problems = da.compare_stored_message(
                    stored,
                    message_id=normalized_id,
                    subject=subject,
                    to=to,
                    body=body,
                    cc=cc,
                    from_addr=from_addr,
                    date=date_header,
                    attachments=prepared,
                )
            envelope_id = found[0]
    except Exception as exc:  # noqa: BLE001 - only the exception TYPE is reported, never its text
        problems = [f"readback:{type(exc).__name__}"]
    if problems or envelope_id is None:
        journaled = _try_attempt(journal, normalized_id, status="appended_unverified", problems=problems[:8])
        suffix = "" if journaled else " The journal could not be updated; reconcile by Message-ID manually."
        raise DraftError(
            DRAFT_VERIFICATION_FAILED_REASON,
            f"The appended draft in folder '{folder}' could not be verified ({'; '.join(problems[:8])}); it may "
            "exist unverified and no old draft was touched. Success is not reported." + suffix,
        )

    envelope = {
        "envelope_id": envelope_id,
        "folder": folder,
        "subject": str(subject),
        "to": str(to),
        "date": str(date_header),
        "message_id": normalized_id,
        "attachment_count": len(meta),
        "attachments": meta,
        "size_bytes": size_bytes,
        "verified": "content",
    }
    if extra_result:
        envelope.update(extra_result)
    try:
        journal.record_attempt(normalized_id, status="saved_verified", envelope_id=envelope_id)
        if record_item:
            journal.record(
                action=action,
                envelope_id=envelope_id,
                folder=folder,
                subject=subject,
                to=to,
                date=date_header,
            )
    except OSError:
        raise DraftPartialError(
            DRAFT_JOURNAL_WRITE_FAILED_REASON,
            f"The draft was appended and verified (envelope {envelope_id}, Message-ID {normalized_id}) but the "
            "journal could not be updated; reconcile manually.",
            new_envelope=envelope,
        ) from None
    return envelope


def _check_attachments_argument(attachments: Any) -> None:
    """``attachments`` must be ``None`` or a list/tuple; anything else is a spec error."""
    if attachments is not None and not isinstance(attachments, (list, tuple)):
        from . import draft_attachments as da

        raise da.DraftAttachmentError(
            "attachment_spec_invalid", "attachments must be a list of path strings or objects."
        )


def _prepare_attachment_draft(
    *,
    attachments: Any,
    workspace_root: Any,
    journal: "DraftJournal | None",
    account: str | None,
    lease_id: str | None,
    conversation_id: str | None,
    lock_root: Any,
) -> tuple[Any, str, tuple[Any, ...]]:
    """Bind workspace / journal / lease / account, then read the files (in this order)."""
    from . import draft_attachments as da

    root, bound_account = da.bind_attachment_mode(
        workspace_root=workspace_root,
        journal=journal,
        account=account,
        lease_id=lease_id,
        conversation_id=conversation_id,
        lock_root=lock_root,
    )
    return root, bound_account, da.load_attachments(root, attachments)


def save_draft(
    subject: str,
    to: str,
    body: str,
    *,
    folder: str | None = None,
    account: str | None = None,
    cc: str | None = None,
    from_addr: str | None = None,
    date: str | None = None,
    message_id: str | None = None,
    journal: "DraftJournal | None" = None,
    folders: Iterable[Any] | None = None,
    attachments: Sequence[Any] | None = None,
    workspace_root: Any = None,
    lease_id: str | None = None,
    conversation_id: str | None = None,
    lock_root: Any = None,
) -> dict[str, Any]:
    """Append a UTF-8 draft into the (resolved) Drafts folder through stdin.

    Returns the envelope ``{envelope_id, folder, subject, to, date, message_id}``.

    With a non-empty ``attachments`` list (see ``core.draft_attachments``) the draft is built as
    ``multipart/mixed`` and saved in *attachment mode*: workspace root, journal, a real workspace
    lease and the workspace-bound account are mandatory and checked before any file is read; the
    files are read bounded and zone-confined; the size limits are enforced before the single
    APPEND (still the same binary stdin path, never retried); success needs a semantic
    read-back of exactly one stored message (headers, text, attachment count / name / MIME /
    decoded SHA-256).  The result then also carries ``attachment_count``, ``attachments``,
    ``size_bytes`` and ``verified = "content"``.

    Every draft size uses the binary stdin transport (threshold 0): in
    Himalaya's non-TTY plain mode an argv ``MESSAGE`` is ignored and a large one
    trips the Windows command-line limit.  The explicit ``-o plain`` output
    format keeps the JSON branch from bypassing stdin, the payload is sent as
    exact UTF-8 bytes (validated strictly at the seam, since upstream silently
    truncates invalid bytes), and the APPEND is attempted exactly once.  A
    classified ``HimalayaInvocationError`` becomes ``draft_save_failed`` with no
    verification, no journal item and no argv fallback; every other exception
    (e.g. a programming ``ValueError``) propagates unchanged.
    """
    _check_attachments_argument(attachments)
    if attachments:
        root, bound_account, prepared = _prepare_attachment_draft(
            attachments=attachments,
            workspace_root=workspace_root,
            journal=journal,
            account=account,
            lease_id=lease_id,
            conversation_id=conversation_id,
            lock_root=lock_root,
        )
        raw_message, normalized_id, date_header = build_draft_message(
            subject, to, body, from_addr=from_addr, cc=cc, date=date, message_id=message_id, attachments=prepared
        )
        target_folder = folder or resolve_drafts_folder(account=bound_account, folders=folders)
        return _append_and_verify(
            raw_message=raw_message,
            normalized_id=normalized_id,
            date_header=date_header,
            subject=subject,
            to=to,
            body=body,
            cc=cc,
            from_addr=from_addr,
            prepared=prepared,
            folder=target_folder,
            account=bound_account,
            journal=journal,
            action="save_draft",
            record_item=True,
        )
    target_folder = folder or resolve_drafts_folder(account=account, folders=folders)
    raw_message, normalized_id, date_header = build_draft_message(
        subject,
        to,
        body,
        from_addr=from_addr,
        cc=cc,
        date=date,
        message_id=message_id,
    )
    stdin_payload = raw_message.encode("utf-8") + _STDIN_TRANSPORT_TERMINATOR
    try:
        himalaya.run_himalaya(
            ["message", "save", "-o", "plain", "-f", target_folder],
            account=account,
            stdin_bytes=stdin_payload,
            max_retries=1,
        )
    except himalaya.HimalayaInvocationError as exc:
        # Only the classified fail-loud runtime error becomes a ``DraftError``;
        # programming errors (e.g. ``ValueError`` about a bad contract) keep
        # propagating unchanged so they are never masked as a save failure.
        raise DraftError(
            DRAFT_SAVE_FAILED_REASON,
            f"Draft APPEND to folder '{target_folder}' failed ({exc.reason_code}); "
            "no verification or journal write was attempted.",
        ) from exc
    envelope = _resolve_saved_envelope(
        target_folder, normalized_id, subject, to, date_header, account
    )
    if journal is not None:
        journal.record(
            action="save_draft",
            envelope_id=envelope["envelope_id"],
            folder=target_folder,
            subject=subject,
            to=to,
            date=date_header,
        )
    return envelope


def save_raw_draft(
    raw_eml: Any,
    *,
    folder: str | None = None,
    account: str | None = None,
    journal: "DraftJournal | None" = None,
    folders: Iterable[Any] | None = None,
    workspace_root: Any = None,
    lease_id: str | None = None,
    conversation_id: str | None = None,
    lock_root: Any = None,
) -> dict[str, Any]:
    """Append one prepared ``.eml`` (``raw_eml``: workspace-relative zone path) as an unsent draft.

    Raw-EML import (issue #18) on the existing verified stdin APPEND -- there is no second writer:
    the file is bound like an attachment-mode draft (workspace root, journal, a real workspace lease and
    the workspace-bound account are checked *before* the file is read), read bounded and link-free,
    validated against the strict profile of ``core.draft_raw_eml`` (RFC 5322 header block, unique
    ``Message-ID``, flat text + attachments structure, 10 MiB / 5 x 3 MiB limits) and normalized to
    canonical CRLF lines.  ``_append_and_verify`` then proves the Message-ID is absent from the target
    folder, journals the intent, performs exactly ONE ``message save`` (never retried; an uncertain
    outcome is journaled, not repeated) and succeeds only when the single stored message matches the
    prepared one semantically (header sequence, text, attachment name / type / size / SHA-256).  The
    result carries the usual envelope plus ``source`` (path, line-ending form, source / normalized
    SHA-256) and ``verified = "content"``.
    """
    from . import draft_attachments as da
    from . import draft_raw_eml as dre

    root, bound_account = da.bind_attachment_mode(
        workspace_root=workspace_root,
        journal=journal,
        account=account,
        lease_id=lease_id,
        conversation_id=conversation_id,
        lock_root=lock_root,
    )
    prepared = dre.load_raw_eml(root, raw_eml)
    target_folder = folder or resolve_drafts_folder(account=bound_account, folders=folders)
    source = {
        "path": prepared.source_path,
        "line_endings": prepared.line_endings,
        "normalized": prepared.normalized,
        "source_sha256": prepared.source_sha256,
        "normalized_sha256": prepared.normalized_sha256,
    }
    return _append_and_verify(
        raw_message=prepared.text,
        normalized_id=prepared.message_id,
        date_header=prepared.date,
        subject=prepared.subject,
        to=prepared.to,
        body="",
        cc=None,
        from_addr=None,
        prepared=prepared.attachments,
        folder=target_folder,
        account=bound_account,
        journal=journal,
        action="save_raw_draft",
        record_item=True,
        extra_attempt={
            "source_path": prepared.source_path,
            "source_line_endings": prepared.line_endings,
            "source_sha256": prepared.source_sha256,
            "normalized_sha256": prepared.normalized_sha256,
        },
        verify=lambda stored: dre.compare_stored_raw(stored, prepared),
        extra_result={"source": source},
    )


def delete_draft(
    envelope_id: str,
    *,
    folder: str | None = None,
    account: str | None = None,
    journal: "DraftJournal | None" = None,
    folders: Iterable[Any] | None = None,
) -> dict[str, Any]:
    """Delete a draft via ``\\Deleted`` + EXPUNGE (never a UID MOVE).

    The ``\\Deleted`` system flag is set in place (``flag add deleted``); only the
    following ``folder expunge`` removes the message.  ``message delete`` is
    deliberately not used: for a non-trash folder Himalaya would move the message
    to the trash folder, which is exactly the forbidden UID MOVE.
    """
    target_folder = folder or resolve_drafts_folder(account=account, folders=folders)
    env_id = str(envelope_id)
    himalaya.run_himalaya(
        ["flag", "add", DELETED_SYSTEM_FLAG, env_id, "-f", target_folder], account=account
    )
    himalaya.run_himalaya(["folder", "expunge", target_folder], account=account)
    if journal is not None:
        journal.record(action="delete_draft", envelope_id=env_id, folder=target_folder)
    return {
        "envelope_id": env_id,
        "folder": target_folder,
        "deleted": True,
        "expunged": True,
    }


def _inspect_old_draft(
    old_id: str,
    folder: str,
    account: str | None,
    *,
    expected_message_id: str | None,
    require_clean: bool,
) -> tuple[str, str, int]:
    """Read the old draft bounded; return ``(normalized Message-ID, subject, non-body part count)``.

    A missing / ambiguous (duplicate) ``Message-ID`` header always makes the old draft unreadable.
    With ``require_clean`` any MIME parser defect does too: attachment freeness must never be
    derived from a defective structure (omitted attachment list).  An explicit list replaces the
    old attachments knowingly and therefore only needs a unique identity.
    """
    from . import draft_attachments as da

    expected = None
    if expected_message_id is not None:
        expected = normalize_message_id(_check_supplied_message_id(expected_message_id))
    try:
        raw = himalaya.fetch_raw_message_eml(
            old_id, folder=folder, account=account, max_bytes=da.MAX_READBACK_BYTES
        )
        message_id, subject, parts, defective = da.describe_stored_message(raw)
        if defective and require_clean:
            raise ValueError("defective MIME structure")
    except Exception:  # noqa: BLE001 - no backend text may reach the error
        raise DraftError(
            DRAFT_OLD_UNREADABLE_REASON,
            "The old draft could not be read unambiguously (bounded, defect-free); nothing was appended or deleted.",
        ) from None
    if expected is not None and message_id != expected:
        raise DraftError(
            DRAFT_OLD_IDENTITY_MISMATCH_REASON,
            "The draft behind the given envelope id does not carry the expected Message-ID; "
            "nothing was appended or deleted.",
        )
    return message_id, subject, parts


def _replace_with_explicit_attachments(
    old_envelope_id: str,
    subject: str,
    to: str,
    body: str,
    *,
    folder: str | None,
    account: str | None,
    cc: str | None,
    from_addr: str | None,
    date: str | None,
    message_id: str | None,
    journal: "DraftJournal | None",
    folders: Iterable[Any] | None,
    attachments: Any,
    old_message_id: str | None,
    workspace_root: Any,
    lease_id: str | None,
    conversation_id: str | None,
    lock_root: Any,
) -> dict[str, Any]:
    """Replace with the complete desired attachment list (attachment mode, see ``replace_draft``)."""
    old_id = str(old_envelope_id)
    if old_message_id is None:
        raise DraftError(
            DRAFT_OLD_IDENTITY_MISMATCH_REASON,
            "Replacing with an explicit attachment list requires the old draft's Message-ID; refusing.",
        )
    expected_old = normalize_message_id(_check_supplied_message_id(old_message_id))
    _check_attachments_argument(attachments)
    root, bound_account, prepared = _prepare_attachment_draft(
        attachments=attachments,
        workspace_root=workspace_root,
        journal=journal,
        account=account,
        lease_id=lease_id,
        conversation_id=conversation_id,
        lock_root=lock_root,
    )
    raw_message, normalized_id, date_header = build_draft_message(
        subject, to, body, from_addr=from_addr, cc=cc, date=date, message_id=message_id, attachments=prepared
    )
    if normalized_id == expected_old:
        raise DraftError(
            DRAFT_MESSAGE_ID_NOT_UNIQUE_REASON,
            "The new draft must not reuse the old draft's Message-ID; nothing was appended.",
        )
    target_folder = folder or resolve_drafts_folder(account=bound_account, folders=folders)
    _, old_subject, old_parts = _inspect_old_draft(
        old_id, target_folder, bound_account, expected_message_id=expected_old, require_clean=False
    )
    return _finish_replace(
        old_id=old_id,
        expected_old=expected_old,
        old_subject=old_subject,
        old_parts=old_parts,
        raw_message=raw_message,
        normalized_id=normalized_id,
        date_header=date_header,
        subject=subject,
        to=to,
        body=body,
        cc=cc,
        from_addr=from_addr,
        prepared=prepared,
        target_folder=target_folder,
        bound_account=bound_account,
        journal=journal,
        folders=folders,
    )


def _finish_replace(
    *,
    old_id: str,
    expected_old: str,
    old_subject: str,
    old_parts: int,
    raw_message: str,
    normalized_id: str,
    date_header: str,
    subject: str,
    to: str,
    body: str,
    cc: str | None,
    from_addr: str | None,
    prepared: tuple[Any, ...],
    target_folder: str,
    bound_account: str | None,
    journal: "DraftJournal",
    folders: Iterable[Any] | None,
) -> dict[str, Any]:
    """Shared replace tail (explicit AND omitted attachment list): verified append, journal, delete.

    The new mail is appended once and read back semantically; only then the journal item naming both
    ids is written, the old draft is re-resolved by its Message-ID (never by the possibly stale
    transient id) and deleted.  Any unclear result keeps the old draft and is reported honestly.
    """
    new_envelope = _append_and_verify(
        raw_message=raw_message,
        normalized_id=normalized_id,
        date_header=date_header,
        subject=subject,
        to=to,
        body=body,
        cc=cc,
        from_addr=from_addr,
        prepared=prepared,
        folder=target_folder,
        account=bound_account,
        journal=journal,
        action="replace_draft",
        record_item=False,
        extra_attempt={
            "old_envelope_id": old_id,
            "old_message_id": expected_old,
            "old_attachment_count": old_parts,
        },
    )
    try:
        journal.record(
            action="replace_draft",
            envelope_id=new_envelope["envelope_id"],
            folder=target_folder,
            subject=subject,
            to=to,
            date=new_envelope["date"],
            old_envelope_id=old_id,
            crash_window=REPLACE_CRASH_WINDOW,
        )
        journal.record_attempt(normalized_id, status="replace_old_pending_delete")
    except OSError:
        raise DraftPartialError(
            DRAFT_JOURNAL_WRITE_FAILED_REASON,
            f"The new draft was appended and verified (envelope {new_envelope['envelope_id']}) but the journal "
            "could not be updated; the old draft was NOT deleted. Reconcile manually.",
            new_envelope=new_envelope,
        ) from None

    def partial(reason_text: str, status: str) -> DraftPartialError:
        _try_attempt(journal, normalized_id, status=status)
        return DraftPartialError(
            DRAFT_REPLACE_OLD_NOT_DELETED_REASON,
            f"The new draft (envelope {new_envelope['envelope_id']}) is appended and verified, but the old "
            f"draft was not deleted: {reason_text}. Both drafts may now exist; the journal names both.",
            new_envelope=new_envelope,
        )

    try:
        old_ids, complete = himalaya.find_envelopes_by_message_id(
            target_folder, expected_old, subject=old_subject, account=bound_account
        )
    except Exception:  # noqa: BLE001
        old_ids, complete = [], False
    if not complete or len(old_ids) > 1:
        raise partial("the old draft could not be resolved unambiguously", "old_delete_failed")
    result = dict(new_envelope)
    result.update(
        {
            "old_envelope_id": old_id,
            "crash_window": REPLACE_CRASH_WINDOW,
            "replaced": True,
        }
    )
    if not old_ids:
        result.update({"old_deleted": False, "old_status": "old_draft_not_found"})
        _try_attempt(journal, normalized_id, status="replaced", old_status="old_draft_not_found")
        return result
    try:
        delete_draft(old_ids[0], folder=target_folder, account=bound_account, folders=folders)
    except Exception:  # noqa: BLE001 - no backend text in the error
        raise partial("the delete step failed", "old_delete_failed") from None
    result.update({"old_deleted": True, "old_status": "deleted", "resolved_old_envelope_id": old_ids[0]})
    if not _try_attempt(journal, normalized_id, status="replaced", old_status="deleted"):
        raise DraftPartialError(
            DRAFT_JOURNAL_WRITE_FAILED_REASON,
            "The replacement completed (new draft verified, old draft deleted) but the journal could not be "
            "updated; reconcile manually.",
            new_envelope=result,
        )
    return result


def replace_draft(
    old_envelope_id: str,
    subject: str,
    to: str,
    body: str,
    *,
    folder: str | None = None,
    account: str | None = None,
    cc: str | None = None,
    from_addr: str | None = None,
    date: str | None = None,
    message_id: str | None = None,
    journal: "DraftJournal | None" = None,
    folders: Iterable[Any] | None = None,
    attachments: Any = ATTACHMENTS_UNSPECIFIED,
    old_message_id: str | None = None,
    workspace_root: Any = None,
    lease_id: str | None = None,
    conversation_id: str | None = None,
    lock_root: Any = None,
) -> dict[str, Any]:
    """Replace a draft: append the new draft first, then delete the old one.

    The new-then-delete order (see ``REPLACE_CRASH_WINDOW``) is deliberate: the
    replacement content is durably appended before the original is removed, so a
    crash can never leave the mailbox without any draft.  When a ``journal`` is
    supplied the entry naming both envelope ids is written *before* the delete
    step starts, so the delete-side orphan window is reconcile-visible.

    ``attachments`` decides what happens to attachments of the old draft:

    * **omitted** (or ``None``): the old draft is read first (bounded); if it carries any
      attachment / non-body part the replace is refused (``draft_replace_would_drop_attachments``)
      and an unreadable old draft is refused too (``draft_old_unreadable``) -- nothing is appended
      or deleted.  Only a provably text-only old draft keeps the former behaviour.
    * **a list** (``[]`` removes all attachments knowingly): the complete desired list is
      authoritative.  Attachment mode applies (workspace binding, real lease, bounded reading,
      size limits, semantic read-back of exactly one stored message), ``old_message_id`` is
      required, and the old draft is deleted only after the new mail was verified and the old
      draft was re-resolved by its Message-ID.
    """
    if attachments is not ATTACHMENTS_UNSPECIFIED and attachments is not None:
        return _replace_with_explicit_attachments(
            old_envelope_id,
            subject,
            to,
            body,
            folder=folder,
            account=account,
            cc=cc,
            from_addr=from_addr,
            date=date,
            message_id=message_id,
            journal=journal,
            folders=folders,
            attachments=attachments,
            old_message_id=old_message_id,
            workspace_root=workspace_root,
            lease_id=lease_id,
            conversation_id=conversation_id,
            lock_root=lock_root,
        )
    # Omitted list: the SAME verified replace as for an explicit list, restricted to a provably
    # text-only old draft.  No workspace / lease is involved (no file is read); the journal is
    # mandatory because the append-then-delete window needs a reconcile trail.
    if journal is None:
        raise DraftError(
            "draft_binding_failed",
            "Replacing a draft needs a draft journal (reconcile trail for the append-then-delete window); refusing.",
        )
    old_id = str(old_envelope_id)
    expected_old = (
        normalize_message_id(_check_supplied_message_id(old_message_id)) if old_message_id is not None else None
    )
    raw_message, normalized_id, date_header = build_draft_message(
        subject, to, body, from_addr=from_addr, cc=cc, date=date, message_id=message_id
    )
    target_folder = folder or resolve_drafts_folder(account=account, folders=folders)
    old_mid, old_subject, old_parts = _inspect_old_draft(
        old_id, target_folder, account, expected_message_id=expected_old, require_clean=True
    )
    if old_parts:
        raise DraftError(
            DRAFT_REPLACE_WOULD_DROP_ATTACHMENTS_REASON,
            f"The old draft carries {old_parts} attachment / non-body part(s); replacing it without an "
            "explicit attachment list would silently drop them. Pass the complete desired list "
            "(an empty list removes them knowingly). Nothing was appended or deleted.",
        )
    if normalized_id == old_mid:
        raise DraftError(
            DRAFT_MESSAGE_ID_NOT_UNIQUE_REASON,
            "The new draft must not reuse the old draft's Message-ID; nothing was appended.",
        )
    return _finish_replace(
        old_id=old_id,
        expected_old=old_mid,
        old_subject=old_subject,
        old_parts=old_parts,
        raw_message=raw_message,
        normalized_id=normalized_id,
        date_header=date_header,
        subject=subject,
        to=to,
        body=body,
        cc=cc,
        from_addr=from_addr,
        prepared=(),
        target_folder=target_folder,
        bound_account=account,
        journal=journal,
        folders=folders,
    )


class DraftJournal:
    """Atomically persist a draft-operation journal per run.

    The shape mirrors ``BatchRecoveryJournal`` (``schema_version`` /
    ``runs`` / ``items``) but lives in its own file
    (``data/mail-desk/draft-journal.json``); the batch recovery contract is never
    read or written here.
    """

    def __init__(self, data_dir: Path, run_id: str, *, mode: str = "draft", resume: bool = True) -> None:
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / DRAFT_JOURNAL_FILENAME
        self.run_id = str(run_id)
        self.mode = str(mode)
        self._data = self._load()
        runs = self._data.setdefault("runs", {})
        run = runs.get(self.run_id)
        if not isinstance(run, dict):
            run = {
                "run_id": self.run_id,
                "mode": self.mode,
                "status": "running",
                "created_at": utc_now_iso(),
                "updated_at": utc_now_iso(),
                "items": {},
            }
            runs[self.run_id] = run
        run.setdefault("items", {})
        run.setdefault("mode", self.mode)
        if resume and run.get("status") in DRAFT_JOURNAL_TERMINAL_STATUSES:
            run["status"] = "running"
        self._save()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": DRAFT_JOURNAL_SCHEMA_VERSION, "runs": {}}
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DraftJournalCorruptedError(
                DRAFT_JOURNAL_CORRUPTED_REASON,
                "Draft journal is unreadable; retain it and reconcile manually.",
            ) from exc
        if not isinstance(loaded, dict) or not isinstance(loaded.get("runs"), dict):
            raise DraftJournalCorruptedError(
                DRAFT_JOURNAL_CORRUPTED_REASON,
                "Draft journal has an invalid shape; retain it and reconcile manually.",
            )
        loaded.setdefault("schema_version", DRAFT_JOURNAL_SCHEMA_VERSION)
        return loaded

    @property
    def run(self) -> dict[str, Any]:
        return self._data["runs"][self.run_id]

    def _save(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.run["updated_at"] = utc_now_iso()
        atomic_write_json(self.path, self._data)

    def record(
        self,
        *,
        action: str,
        envelope_id: str,
        folder: str,
        subject: str = "",
        to: str = "",
        date: str = "",
        old_envelope_id: str | None = None,
        crash_window: str | None = None,
    ) -> dict[str, Any]:
        """Record one draft operation keyed by the resulting envelope id."""
        entry: dict[str, Any] = {
            "envelope_id": str(envelope_id),
            "folder": str(folder),
            "subject": str(subject),
            "to": str(to),
            "date": str(date),
            "action": str(action),
            "recorded_at": utc_now_iso(),
        }
        if old_envelope_id is not None:
            entry["old_envelope_id"] = str(old_envelope_id)
        if crash_window is not None:
            entry["crash_window"] = str(crash_window)
        self.run["items"][str(envelope_id)] = entry
        self._save()
        return entry

    def record_attempt(self, message_id: str, *, status: str, **fields: Any) -> dict[str, Any]:
        """Record or update the attempt of one attachment-mode APPEND, keyed by Message-ID.

        Attempts exist before the envelope id is known: ``append_started`` is written before the
        APPEND, so an unknown outcome (timeout, crash) stays reconcile-visible.  Later updates merge
        into the same entry (``append_failed``, ``append_outcome_unknown``, ``appended_unverified``,
        ``saved_verified``, ``replace_old_pending_delete``, ``old_delete_failed``, ``replaced``).
        Entries hold metadata only -- never attachment content.
        """
        key = normalize_message_id(message_id)
        attempts = self.run.setdefault("attempts", {})
        previous = attempts.get(key)
        entry: dict[str, Any] = dict(previous) if isinstance(previous, dict) else {"created_at": utc_now_iso()}
        entry.update(fields)
        entry["message_id"] = key
        entry["status"] = str(status)
        entry["updated_at"] = utc_now_iso()
        attempts[key] = entry
        self._save()
        return entry

    def set_run_status(self, status: str) -> None:
        """Set the run status and persist it."""
        self.run["status"] = str(status)
        self._save()
