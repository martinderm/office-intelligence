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
from email.message import EmailMessage
from email.policy import SMTPUTF8
from pathlib import Path
from typing import Any, Iterable

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


def build_draft_message(
    subject: str,
    to: str,
    body: str,
    *,
    from_addr: str | None = None,
    cc: str | None = None,
    date: str | None = None,
    message_id: str | None = None,
) -> tuple[str, str, str]:
    """Build a UTF-8 (SMTPUTF8) raw RFC-822 draft message with CRLF lines.

    Returns ``(raw_message, normalized_message_id, date_header)``.  Headers are
    written as raw UTF-8 (no RFC 2047 encoded-word downgrade); the body is
    carried as a UTF-8 payload whose transfer encoding is chosen by the policy
    (``Content-Transfer-Encoding``), so no content is mangled.  The raw message
    uses canonical CRLF line endings and ends with a final CRLF, ready for the
    SMTP/IMAP wire and for the binary stdin transport.
    """
    generated_id = message_id or email.utils.make_msgid()
    normalized_id = normalize_message_id(generated_id)
    date_header = date or email.utils.formatdate(localtime=True)

    message = EmailMessage(policy=SMTPUTF8_CRLF)
    if from_addr:
        message["From"] = str(from_addr)
    message["To"] = str(to)
    if cc:
        message["Cc"] = str(cc)
    message["Subject"] = str(subject)
    message["Date"] = date_header
    message["Message-ID"] = generated_id if str(generated_id).startswith("<") else f"<{generated_id}>"
    message.set_content(str(body), subtype="plain", charset="utf-8")

    raw_message = message.as_string()
    if not raw_message.endswith("\r\n"):
        raw_message += "\r\n"

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
) -> dict[str, Any]:
    """Append a UTF-8 draft into the (resolved) Drafts folder through stdin.

    Returns the envelope ``{envelope_id, folder, subject, to, date, message_id}``.

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
) -> dict[str, Any]:
    """Replace a draft: append the new draft first, then delete the old one.

    The new-then-delete order (see ``REPLACE_CRASH_WINDOW``) is deliberate: the
    replacement content is durably appended before the original is removed, so a
    crash can never leave the mailbox without any draft.  When a ``journal`` is
    supplied the entry naming both envelope ids is written *before* the delete
    step starts, so the delete-side orphan window is reconcile-visible.
    """
    target_folder = folder or resolve_drafts_folder(account=account, folders=folders)
    old_id = str(old_envelope_id)

    new_envelope = save_draft(
        subject,
        to,
        body,
        folder=target_folder,
        account=account,
        cc=cc,
        from_addr=from_addr,
        date=date,
        message_id=message_id,
        folders=folders,
    )

    # Record the replacement journal entry *before* deleting the old draft, so a
    # crash during the delete step (``\Deleted`` + EXPUNGE) still leaves a
    # reconcile-visible entry naming the new and old envelope ids.
    if journal is not None:
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

    delete_draft(old_id, folder=target_folder, account=account, folders=folders)

    result = dict(new_envelope)
    result.update(
        {
            "old_envelope_id": old_id,
            "replaced": True,
            "crash_window": REPLACE_CRASH_WINDOW,
        }
    )
    return result


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

    def set_run_status(self, status: str) -> None:
        """Set the run status and persist it."""
        self.run["status"] = str(status)
        self._save()
