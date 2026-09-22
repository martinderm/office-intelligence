"""Hermetic fixtures faking multi-command Himalaya client operations for MD-R6.

The helpers never start a real ``himalaya`` subprocess and never touch the
network.  Tests inject them by patching ``core.himalaya.run_himalaya``, which is
the single command-invoker seam ``search_mailbox`` calls once per folder, so the
per-call outcome, an injected delay and the ordered folder sweep stay fully
deterministic.  ``deadline_case`` answers the ``folder list`` catalog command
with a JSON folder list, records every ``envelope list`` call per folder,
optionally sleeps an injected duration per call and optionally raises an injected
per-call ``himalaya_timeout`` error.

This module is deliberately not named ``test_*.py`` so unittest discovery does
not collect it as a suite.
"""

from __future__ import annotations

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
