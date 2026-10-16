"""Hermetic fixtures faking multi-command Himalaya client operations for MD-R6.

The helpers never start a real ``himalaya`` subprocess and never touch the
network.  Tests inject them by patching ``core.himalaya.run_himalaya``, which is
the single command-invoker seam ``search_mailbox`` calls once per folder, so the
per-call outcome, an injected delay and the ordered folder sweep stay fully
deterministic.  ``deadline_case`` answers the ``folder list`` catalog command
with a JSON folder list, records every ``envelope list`` call per folder,
optionally sleeps an injected duration per call and optionally raises an injected
per-call ``himalaya_timeout`` error.

``DraftRunner`` (below) fakes the standalone draft operations the same way: it
answers the ``folder list -o json`` catalog, records ``message save`` raw
literals (APPEND) and hands the appended message back through the ``envelope
list`` + ``message read -H Message-Id`` seams that ``verify_in_target_folder``
uses, plus the in-place ``flag add deleted`` (``\\Deleted``) and ``folder
expunge`` pair.  ``message delete`` is modelled faithfully to Himalaya 1.2.0
(``message delete --help``: "if the given folder points to the trash folder, it
adds the 'deleted' flag to its envelope, otherwise it moves it to the trash
folder"), so a regression back to that command leaves the draft in ``Trash``
instead of expunging it in place.

This module is deliberately not named ``test_*.py`` so unittest discovery does
not collect it as a suite.
"""

from __future__ import annotations

import email
import email.policy
import json
import threading
import time
from typing import Any, Callable, Iterable, Sequence

#: Hermetic default folder catalog, sized so a per-call delay clearly sums past
#: a small overall deadline while each individual call stays well under 0.2 s.
DEFAULT_FOLDERS: tuple[str, ...] = (
    "INBOX",
    "Junk",
    "Trash",
    "Newsletter",
    "Themen/BOKU-Organisation",
    "Archive",
)


def is_folder_list(args: Sequence[str]) -> bool:
    """Whether an argv token list is the ``folder list`` catalog command."""
    tokens = list(args)
    return len(tokens) >= 2 and tokens[0] == "folder" and tokens[1] == "list"


def is_envelope_list(args: Sequence[str]) -> bool:
    """Whether an argv token list is an ``envelope list -f <folder>`` command."""
    tokens = list(args)
    return "envelope" in tokens and "list" in tokens and "-f" in tokens


def folder_from_args(args: Sequence[str]) -> str | None:
    """Extract the ``-f`` folder argument from an argv token list."""
    tokens = list(args)
    try:
        index = tokens.index("-f")
    except ValueError:
        return None
    if index + 1 < len(tokens):
        return str(tokens[index + 1])
    return None


def folder_list_json(folders: Iterable[str] = DEFAULT_FOLDERS) -> str:
    """Return a Himalaya ``folder list -o json`` payload for the given folders."""
    return json.dumps([{"name": str(name)} for name in folders])


def envelope_list_json(entries: Iterable[dict[str, Any]] | None = None) -> str:
    """Return a Himalaya ``envelope list -o json`` payload (empty by default)."""
    return json.dumps(list(entries or []))


def himalaya_timeout_error(
    message: str = "Himalaya timed out; refusing to retry the mailbox command.",
) -> BaseException:
    """Return a bounded per-call ``himalaya_timeout`` error as production raises it."""
    from core.himalaya import HimalayaInvocationError

    return HimalayaInvocationError("himalaya_timeout", message)


class FakeHimalayaRunner:
    """Injectable ``run_himalaya`` replacement for multi-folder operations."""

    def __init__(
        self,
        folders: Iterable[str] = DEFAULT_FOLDERS,
        *,
        per_call_seconds: float = 0.0,
        sleeper: Callable[[float], None] = time.sleep,
        timeout_error: BaseException | None = None,
        entries: Iterable[dict[str, Any]] | None = None,
    ) -> None:
        self.folders = tuple(str(name) for name in folders)
        self.per_call_seconds = float(per_call_seconds)
        self.sleeper = sleeper
        self.timeout_error = timeout_error
        self.entries = list(entries or [])
        self._lock = threading.Lock()
        self.calls: list[list[str]] = []

    def __call__(
        self,
        args: list[str],
        account: str | None = None,
        timeout: int = 35,
        max_retries: int = 5,
        **kwargs: Any,
    ) -> str:
        with self._lock:
            self.calls.append(list(args))
        if is_folder_list(args):
            return folder_list_json(self.folders)
        if is_envelope_list(args):
            if self.timeout_error is not None:
                raise self.timeout_error
            if self.per_call_seconds > 0:
                self.sleeper(self.per_call_seconds)
            return envelope_list_json(self.entries)
        raise AssertionError(f"unexpected Himalaya invocation: {list(args)!r}")

    @property
    def call_count(self) -> int:
        """Total number of faked Himalaya invocations."""
        with self._lock:
            return len(self.calls)

    @property
    def folder_list_calls(self) -> list[list[str]]:
        """Every faked ``folder list`` catalog invocation."""
        with self._lock:
            return [list(call) for call in self.calls if is_folder_list(call)]

    @property
    def envelope_list_calls(self) -> list[list[str]]:
        """Every faked ``envelope list`` invocation, in completion order."""
        with self._lock:
            return [list(call) for call in self.calls if is_envelope_list(call)]

    @property
    def swept_folders(self) -> list[str]:
        """The folder name of every faked ``envelope list`` invocation."""
        return [folder_from_args(call) or "" for call in self.envelope_list_calls]


def deadline_case(
    folders: Iterable[str] = DEFAULT_FOLDERS,
    *,
    per_call_seconds: float = 0.0,
    sleeper: Callable[[float], None] = time.sleep,
    timeout_error: BaseException | None = None,
    entries: Iterable[dict[str, Any]] | None = None,
) -> FakeHimalayaRunner:
    """Build a hermetic multi-command runner for overall-deadline tests."""
    return FakeHimalayaRunner(
        folders=folders,
        per_call_seconds=per_call_seconds,
        sleeper=sleeper,
        timeout_error=timeout_error,
        entries=entries,
    )


# ---------------------------------------------------------------------------
# Draft (save / delete / replace) fixtures
# ---------------------------------------------------------------------------

#: Convenience default folder set that already contains a conventional Drafts.
DEFAULT_DRAFT_FOLDERS: tuple[str, ...] = ("INBOX", "Sent", "Drafts")


def is_message_save(args: Sequence[str]) -> bool:
    """Whether an argv token list is a ``message save`` (APPEND) command."""
    tokens = list(args)
    return len(tokens) >= 2 and tokens[0] == "message" and tokens[1] == "save"


def is_message_delete(args: Sequence[str]) -> bool:
    """Whether an argv token list is a ``message delete`` command."""
    tokens = list(args)
    return len(tokens) >= 2 and tokens[0] == "message" and tokens[1] == "delete"


def is_flag_add(args: Sequence[str]) -> bool:
    """Whether an argv token list is a ``flag add ...`` command."""
    tokens = list(args)
    return len(tokens) >= 3 and tokens[0] == "flag" and tokens[1] == "add"


#: System flags Himalaya 1.2.0 documents for ``flag add``.
KNOWN_FLAG_NAMES: frozenset[str] = frozenset(
    {"seen", "answered", "flagged", "deleted", "draft"}
)


def is_folder_expunge(args: Sequence[str]) -> bool:
    """Whether an argv token list is a ``folder expunge`` command."""
    tokens = list(args)
    return len(tokens) >= 2 and tokens[0] == "folder" and tokens[1] == "expunge"


def is_message_read_headers(args: Sequence[str]) -> bool:
    """Whether an argv token list is a ``message read -H ...`` header command."""
    tokens = list(args)
    return len(tokens) >= 2 and tokens[0] == "message" and tokens[1] == "read"


def _value_after(tokens: Sequence[str], flag: str) -> str | None:
    """Return the token following ``flag``, or ``None`` when it is absent."""
    token_list = list(tokens)
    try:
        index = token_list.index(flag)
    except ValueError:
        return None
    if index + 1 < len(token_list):
        return str(token_list[index + 1])
    return None


def _header(raw: str, name: str) -> str:
    """Extract one header value from a raw RFC-822 literal (UTF-8 aware)."""
    try:
        message = email.message_from_string(raw, policy=email.policy.SMTPUTF8)
    except Exception:
        return ""
    return str(message.get(name, "") or "")


class DraftRunner:
    """Injectable ``run_himalaya`` replacement for draft save/delete/replace.

    It models the minimal mailbox state the draft operations need: a folder
    catalog, per-folder envelopes (with transient ``id`` and durable
    ``Message-ID``), the system flags of an envelope and an explicit expunge.
    Every call is recorded so tests can pin command order and the appended raw
    literal.
    """

    def __init__(
        self,
        folders: Iterable[Any] | None = None,
        *,
        next_envelope_id: int = 42,
    ) -> None:
        raw_folders = list(folders) if folders is not None else list(DEFAULT_DRAFT_FOLDERS)
        self.folders: list[Any] = list(raw_folders)
        self._next_id = int(next_envelope_id)
        self._envelopes: dict[str, list[dict[str, Any]]] = {}
        self._lock = threading.Lock()
        self.calls: list[list[str]] = []
        self.saved_raw: list[str] = []

    # -- recording ---------------------------------------------------------
    def add_envelope(
        self,
        folder: str,
        *,
        id: str,
        subject: str = "",
        message_id: str = "",
        date: str = "",
    ) -> None:
        """Pre-seed an existing envelope in a folder (transient id + Message-ID)."""
        self._envelopes.setdefault(folder, []).append(
            {
                "id": str(id),
                "subject": str(subject),
                "message_id": str(message_id),
                "date": str(date),
                "flags": set(),
            }
        )

    # -- command surface ---------------------------------------------------
    def __call__(
        self,
        args: list[str],
        account: str | None = None,
        timeout: int = 35,
        max_retries: int = 5,
        **kwargs: Any,
    ) -> str:
        with self._lock:
            self.calls.append(list(args))
        tokens = list(args)
        if is_folder_list(tokens):
            return json.dumps(self._folder_entries())
        if is_message_save(tokens):
            return self._handle_save(tokens)
        if is_flag_add(tokens):
            return self._handle_flag_add(tokens)
        if is_message_delete(tokens):
            return self._handle_message_delete(tokens)
        if is_folder_expunge(tokens):
            return self._handle_expunge(tokens)
        if is_envelope_list(tokens):
            return self._handle_envelope_list(tokens)
        if is_message_read_headers(tokens):
            return self._handle_read_headers(tokens)
        raise AssertionError(f"unexpected Himalaya invocation: {tokens!r}")

    # -- helpers -----------------------------------------------------------
    def _folder_entries(self) -> list[Any]:
        return [entry if isinstance(entry, dict) else {"name": str(entry)} for entry in self.folders]

    def _handle_save(self, tokens: list[str]) -> str:
        folder = _value_after(tokens, "-f") or "INBOX"
        raw = tokens[-1]
        with self._lock:
            self.saved_raw.append(raw)
            envelope_id = str(self._next_id)
            self._next_id += 1
            self._envelopes.setdefault(folder, []).append(
                {
                    "id": envelope_id,
                    "subject": _header(raw, "Subject"),
                    "message_id": _header(raw, "Message-ID"),
                    "date": _header(raw, "Date"),
                    "flags": set(),
                }
            )
        return ""

    def _handle_flag_add(self, tokens: list[str]) -> str:
        """Apply a ``flag add`` call to the matching envelopes, in place."""
        folder = _value_after(tokens, "-f") or "INBOX"
        wanted = {
            token.lower() for token in tokens[2:] if token.lower() in KNOWN_FLAG_NAMES
        }
        ids = {token for token in tokens[2:] if token.isdigit()}
        for envelope in self._envelopes.get(folder, []):
            if envelope["id"] in ids:
                envelope["flags"].update(wanted)
        return ""

    def _handle_message_delete(self, tokens: list[str]) -> str:
        """Model Himalaya 1.2.0: a non-trash message is **moved** to the trash folder."""
        envelope_id = tokens[2]
        folder = _value_after(tokens, "-f") or "INBOX"
        moved = [env for env in self._envelopes.get(folder, []) if env["id"] == envelope_id]
        self._envelopes[folder] = [
            env for env in self._envelopes.get(folder, []) if env["id"] != envelope_id
        ]
        if folder.strip().lower() != "trash":
            self._envelopes.setdefault("Trash", []).extend(moved)
        return ""

    def _handle_expunge(self, tokens: list[str]) -> str:
        folder = tokens[2]
        remaining = [
            env for env in self._envelopes.get(folder, []) if "deleted" not in env["flags"]
        ]
        self._envelopes[folder] = remaining
        return ""

    def _handle_envelope_list(self, tokens: list[str]) -> str:
        folder = _value_after(tokens, "-f") or "INBOX"
        payload = [
            {
                "id": env["id"],
                "subject": env["subject"],
                "from": {"name": "", "addr": ""},
                "date": env["date"],
                "flags": sorted(env["flags"]),
            }
            for env in self._envelopes.get(folder, [])
            if "deleted" not in env["flags"]
        ]
        return json.dumps(payload)

    def _handle_read_headers(self, tokens: list[str]) -> str:
        envelope_id = tokens[-1]
        folder = _value_after(tokens, "-f") or "INBOX"
        for env in self._envelopes.get(folder, []):
            if env["id"] == envelope_id:
                return f"Message-Id: <{env['message_id']}>\nSubject: {env['subject']}\n\n"
        return ""

    # -- assertions --------------------------------------------------------
    def commands(self) -> list[list[str]]:
        """All recorded invocations, in order."""
        with self._lock:
            return [list(call) for call in self.calls]

    def actions(self, *prefix: str) -> list[list[str]]:
        """Recorded invocations whose leading tokens match ``prefix``."""
        wanted = list(prefix)
        return [call for call in self.commands() if call[: len(wanted)] == wanted]

    def index_of_first(self, *prefix: str) -> int:
        """Index of the first recorded invocation matching ``prefix`` (-1 if none)."""
        wanted = list(prefix)
        for index, call in enumerate(self.commands()):
            if call[: len(wanted)] == wanted:
                return index
        return -1

    def envelope_ids(self, folder: str) -> list[str]:
        """Current (unexpunged) transient envelope ids of one folder, in order."""
        return [
            env["id"] for env in self._envelopes.get(folder, []) if "deleted" not in env["flags"]
        ]


def drafts_case(
    folders: Iterable[Any] | None = None,
    *,
    next_envelope_id: int = 42,
) -> DraftRunner:
    """Build a hermetic draft-operation runner."""
    return DraftRunner(folders=folders, next_envelope_id=next_envelope_id)
