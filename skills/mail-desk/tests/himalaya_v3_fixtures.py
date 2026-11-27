"""Hermetic Himalaya 1.2.0 / 3.0.0 fixtures for the HV3-1 version matrix.

The helpers never start a real ``himalaya`` child, read no mailbox and touch no
network, credentials or real configuration.  They answer the transport seams the
adapter can use (``subprocess.run`` and the module-level ``_POPEN`` factory) with
scripted, well-formed payloads and record the **exact final argv** of every
child, so a test can pin byte-level command parity for both CLI profiles.

Provided fixtures
-----------------
* ``V1_VERSION_OUTPUT`` / ``V3_VERSION_OUTPUT`` / ``UNSUPPORTED_VERSION_OUTPUT``
  / ``MALFORMED_VERSION_OUTPUT`` / ``EMPTY_VERSION_OUTPUT``
* ``v3_envelope`` / ``v3_schema_payload`` / ``v3_mailbox_payload``
* ``v3_message_view`` / ``v3_raw_payload`` / ``minimal_eml``
* ``HimalayaTransport`` (version probe only)
* ``V3MailboxTransport`` (v3.0.0 live array, schema object, paging, mailbox,
  read view, raw base64, add/copy/delete; v1-only commands fail loudly)
* ``PopenAdapter`` for the ``_POPEN`` seam
* ``patched_himalaya`` for a hermetic config/executable/probe bootstrap

This module is deliberately not named ``test_*.py`` so unittest discovery does
not collect it as a suite.
"""

from __future__ import annotations

from contextlib import contextmanager
import base64
import email
import email.policy
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any, Callable, Iterable, Sequence
from unittest.mock import patch

#: ``himalaya --version`` output of the pinned 1.2.0 profile.
V1_VERSION_OUTPUT = "himalaya 1.2.0\n"

#: ``himalaya --version`` output of the installed 3.0.0 profile.
V3_VERSION_OUTPUT = "himalaya 3.0.0\n"

#: A real-looking version outside both supported ranges.
UNSUPPORTED_VERSION_OUTPUT = "himalaya 2.4.0\n"

#: Output that carries no parsable ``v<major>.<minor>.<patch>`` at all.
MALFORMED_VERSION_OUTPUT = "not-a-himalaya-version\n"

#: Completely empty stdout of a version probe.
EMPTY_VERSION_OUTPUT = ""


class ForbiddenV1Command(AssertionError):
    """A v1-only CLI command was emitted under the v3 profile."""


_VALUE_FLAGS = frozenset(
    {
        "-m",
        "--mailbox",
        "-f",
        "--folder",
        "-s",
        "--page-size",
        "-t",
        "--target",
        "-o",
        "--output",
        "-d",
        "--destination",
        "-H",
        "--header",
        "-a",
        "--account",
        "-c",
        "--config",
        "--page",
        "--page-token",
        "--sort",
        "--flag",
    }
)

_SUBCOMMAND_HEADS = frozenset({"envelope", "mailbox", "message", "flag", "folder"})

_SUBCOMMAND_TAILS = frozenset(
    {"list", "search", "read", "add", "copy", "delete", "expunge", "save", "export", "thread"}
)


class Command:
    """A parsed Himalaya argv: subcommand name, flags, options and positionals."""

    __slots__ = ("name", "flags", "options", "positionals", "argv")

    def __init__(
        self,
        name: str,
        flags: set[str],
        options: dict[str, list[str]],
        positionals: list[str],
        argv: list[str],
    ) -> None:
        self.name = name
        self.flags = flags
        self.options = options
        self.positionals = positionals
        self.argv = argv

    def option(self, *names: str) -> str | None:
        """Return the first value of one of ``names`` (``None`` when absent)."""
        for name in names:
            values = self.options.get(name)
            if values:
                return values[-1]
        return None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"Command(name={self.name!r}, argv={self.argv!r})"


def is_version_command(argv: Sequence[str]) -> bool:
    """Whether an argv token list is the single ``himalaya --version`` probe."""
    return "--version" in list(argv) or "-V" in list(argv)


def parse_command(argv: Sequence[str]) -> Command:
    """Split one final argv into its subcommand, options, flags and positionals."""
    tokens = [str(token) for token in argv]
    start = 0
    name = ""
    for index in range(len(tokens) - 1):
        if tokens[index] in _SUBCOMMAND_HEADS and tokens[index + 1] in _SUBCOMMAND_TAILS:
            name = f"{tokens[index]} {tokens[index + 1]}"
            start = index + 2
            break
    options: dict[str, list[str]] = {}
    flags: set[str] = set()
    positionals: list[str] = []
    index = start
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("--") and "=" in token:
            key, _, value = token.partition("=")
            if key in _VALUE_FLAGS:
                options.setdefault(key, []).append(value)
                index += 1
                continue
        if token in _VALUE_FLAGS:
            if index + 1 < len(tokens):
                options.setdefault(token, []).append(tokens[index + 1])
            index += 2
            continue
        if token.startswith("-"):
            flags.add(token)
            index += 1
            continue
        positionals.append(token)
        index += 1
    return Command(name, flags, options, positionals, tokens)


# ---------------------------------------------------------------------------
# v3 JSON payload builders (live array form and forward-compatible schema form)
# ---------------------------------------------------------------------------


def v3_envelope(
    envelope_id: str | int,
    *,
    message_id: str | None = None,
    subject: str = "",
    from_name: str | None = "Ada Lovelace",
    from_email: str = "ada@example.org",
    to: Iterable[tuple[str, str]] = (("Grace Hopper", "grace@example.org"),),
    date: str = "2026-11-27T09:00:00Z",
    flags: Iterable[tuple[str, str | None]] = (("Seen", "\\Seen"),),
    size: int | None = 1024,
    has_attachment: bool | None = False,
    in_reply_to: Iterable[str] = (),
) -> dict[str, Any]:
    """One v3 ``envelope list --json`` entry with the documented camelCase keys.

    ``from_name`` and ``iana`` accept ``None`` because the v3 schema types them
    ``string|null``/``IanaFlag|null`` (a nameless sender, a custom keyword).
    """
    return {
        "id": str(envelope_id),
        "messageId": message_id,
        "inReplyTo": [str(value) for value in in_reply_to],
        "flags": [{"raw": str(raw), "iana": iana} for raw, iana in flags],
        "subject": subject,
        "from": [{"name": from_name, "email": from_email}] if from_name or from_email else [],
        "to": [{"name": name, "email": address} for name, address in to],
        "date": date,
        "size": size,
        "hasAttachment": has_attachment,
    }


def v3_schema_payload(
    envelopes: Iterable[dict[str, Any]] | None = None,
    *,
    next_page: str | None = None,
    total: int | None = None,
    errors: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """The ``himalaya json-schema`` object form: envelopes/nextPage/total/errors."""
    return {
        "envelopes": list(envelopes or []),
        "nextPage": next_page,
        "total": total,
        "errors": list(errors),
    }


def v3_mailbox_payload(
    mailboxes: Iterable[dict[str, Any]] | None = None,
    *,
    errors: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """The v3 ``mailbox list --json`` envelope."""
    if mailboxes is None:
        mailboxes = [
            {"id": "1", "name": "INBOX", "role": None, "total": 3, "unread": 1},
            {"id": "2", "name": "Drafts", "role": "drafts", "total": 0, "unread": 0},
            {"id": "3", "name": "Archive", "role": None, "total": 7, "unread": 0},
        ]
    return {"mailboxes": list(mailboxes), "errors": list(errors)}


def v3_message_view(
    envelope_id: str | int = "41",
    *,
    message_id: str = "old@example.org",
    subject: str = "Alt",
    from_name: str | None = "Ada Lovelace",
    from_email: str = "ada@example.org",
    to: Iterable[tuple[str, str]] = (("Grace Hopper", "grace@example.org"),),
    cc: Iterable[tuple[str, str]] = (),
    references: Iterable[str] = (),
    date: str = "2026-11-27T09:00:00Z",
    text: str = "Zeile 1\nZeile 2\nZeile 3\n",
    in_reply_to: Iterable[str] = (),
) -> dict[str, Any]:
    """The v3 ``message read --json`` view, with the real typed ``headers`` object.

    ``headers`` mirrors ``himalaya json-schema himalaya-message-read``: address
    arrays (``name`` may be ``null``), bracket-less id arrays (`references`,
    ``inReplyTo``) and ``subject``/``date``/``messageId`` strings.
    """
    reply_to = [str(value) for value in in_reply_to]
    reference_ids = [str(value) for value in references]
    return {
        "id": str(envelope_id),
        "messageId": message_id,
        "inReplyTo": list(reply_to),
        "flags": [{"raw": "Seen", "iana": "seen"}],
        "subject": subject,
        "from": [{"name": from_name, "email": from_email}],
        "to": [{"name": name, "email": address} for name, address in to],
        "date": date,
        "size": 128,
        "hasAttachment": False,
        "etag": None,
        "headers": {
            "from": [{"name": from_name, "email": from_email}],
            "to": [{"name": name, "email": address} for name, address in to],
            "cc": [{"name": name, "email": address} for name, address in cc],
            "bcc": [],
            "replyTo": [],
            "sender": [],
            "subject": subject,
            "date": date,
            "messageId": message_id,
            "inReplyTo": list(reply_to),
            "references": list(reference_ids),
            "listId": None,
            "listUnsubscribe": None,
            "precedence": None,
            "autoSubmitted": None,
            "invalid": {
                "to": [],
                "cc": [],
                "bcc": [],
                "replyTo": [],
                "from": [],
                "sender": [],
            },
        },
        "text": text,
        "html": None,
        "parts": [],
    }


def v3_raw_payload(eml: bytes) -> dict[str, Any]:
    """The v3 ``message read --raw --json`` payload with base64 ``data``."""
    return {
        "data": base64.b64encode(eml).decode("ascii"),
        "mime": "message/rfc822",
        "size": len(eml),
    }


def minimal_eml(
    message_id: str = "old@example.org",
    *,
    subject: str = "Alt",
    to: str = "grace@example.org",
    body: str = "Synthetischer Text\r\n",
) -> bytes:
    """A minimal, attachment-free RFC-822 message as exact CRLF bytes."""
    raw = (
        f"Message-ID: <{message_id}>\r\n"
        f"Subject: {subject}\r\n"
        f"To: {to}\r\n"
        "Date: Thu, 27 Nov 2026 09:00:00 +0000\r\n"
        "MIME-Version: 1.0\r\n"
        "Content-Type: text/plain; charset=us-ascii\r\n"
        "\r\n"
        f"{body}"
    )
    return raw.encode("utf-8")


# ---------------------------------------------------------------------------
# Transport fakes (``subprocess.run`` and ``_POPEN`` seams)
# ---------------------------------------------------------------------------


class _FakeProcess:
    """The minimal ``Popen`` surface ``_run_process`` needs."""

    def __init__(self, transport: "HimalayaTransport", argv: list[str], kwargs: dict[str, Any]) -> None:
        self._transport = transport
        self._argv = argv
        self._kwargs = dict(kwargs)
        self.returncode = 0

    def communicate(self, input: Any = None, timeout: Any = None) -> tuple[Any, Any]:
        """Answer the child from the transport script and return its streams."""
        kwargs = dict(self._kwargs)
        for name in ("stdin", "stdout", "stderr"):
            kwargs.pop(name, None)
        if input is not None:
            kwargs["input"] = input
        code, stdout, stderr = self._transport.execute(self._argv, kwargs)
        self.returncode = code
        return stdout, stderr

    def kill(self) -> None:
        """Mirror ``Popen.kill`` without a real child."""
        self.returncode = -1


class PopenAdapter:
    """``_POPEN`` replacement that routes every child through a transport script."""

    def __init__(self, transport: "HimalayaTransport") -> None:
        self.transport = transport

    def __call__(self, argv: Sequence[str], **kwargs: Any) -> _FakeProcess:
        return _FakeProcess(self.transport, [str(token) for token in argv], kwargs)


class HimalayaTransport:
    """Record every child argv and answer the version probe; nothing else.

    This is the strict variant used by the version-matrix tests: any mailbox
    command raises, so a test that sees it knows the adapter ran a mailbox
    command where the profile was not yet resolved.
    """

    def __init__(
        self,
        version_output: str = V3_VERSION_OUTPUT,
        *,
        version_error: BaseException | None = None,
        version_timeout: int | None = None,
    ) -> None:
        self.version_output = version_output
        self.version_error = version_error
        self.version_timeout = version_timeout
        #: ``(argv, kwargs)`` of every child the adapter tried to start.
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    # -- subprocess.run ----------------------------------------------------
    def __call__(self, argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess:
        code, stdout, stderr = self.execute(argv, kwargs)
        return subprocess.CompletedProcess([str(token) for token in argv], code, stdout, stderr)

    # -- shared ------------------------------------------------------------
    def execute(self, argv: Sequence[str], kwargs: dict[str, Any]) -> tuple[int, Any, Any]:
        """Return ``(returncode, stdout, stderr)`` for one child invocation."""
        tokens = [str(token) for token in argv]
        self.calls.append((tokens, dict(kwargs)))
        text = bool(kwargs.get("text"))
        if is_version_command(tokens):
            stdout = self._version_stdout(tokens, kwargs)
        else:
            stdout = self._mailbox_stdout(tokens)
        empty_err = "" if text else b""
        return 0, (stdout if text else stdout.encode("utf-8")), empty_err

    def _version_stdout(self, argv: list[str], kwargs: dict[str, Any]) -> str:
        if self.version_timeout is not None:
            raise subprocess.TimeoutExpired(argv, self.version_timeout)
        if self.version_error is not None:
            raise self.version_error
        return self.version_output

    def _mailbox_stdout(self, argv: list[str]) -> str:
        raise AssertionError(f"no mailbox command may start before the profile is known: {argv!r}")

    # -- assertions --------------------------------------------------------
    @property
    def version_calls(self) -> list[list[str]]:
        """Every recorded ``--version`` invocation, in order."""
        return [argv for argv, _kwargs in self.calls if is_version_command(argv)]

    @property
    def mailbox_calls(self) -> list[list[str]]:
        """Every recorded non-version invocation, in order."""
        return [argv for argv, _kwargs in self.calls if not is_version_command(argv)]

    @property
    def stdin_payloads(self) -> list[Any]:
        """The ``input=`` payload of every child that received one."""
        return [kwargs["input"] for _argv, kwargs in self.calls if kwargs.get("input") is not None]


def unquote_query_value(value: str) -> str:
    """Return one pimdir query value with surrounding phrases unquoted.

    Mirrors the adapter's quoting: a value wrapped in double quotes is a phrase
    and backslash escapes the next character.  Unquoted values stay verbatim.
    """
    if len(value) < 2 or value[0] != '"' or value[-1] != '"':
        return value
    inner = value[1:-1]
    unescaped: list[str] = []
    index = 0
    while index < len(inner):
        character = inner[index]
        if character == "\\" and index + 1 < len(inner):
            unescaped.append(inner[index + 1])
            index += 2
            continue
        unescaped.append(character)
        index += 1
    return "".join(unescaped)


class V3MailboxTransport(HimalayaTransport):
    """A stateful, hermetic v3.0.0 CLI: list/search/read/raw/add/copy/delete.

    Folder state is kept as v3 camelCase envelopes.  The default ``envelope
    list`` answer is the **live top-level array**; ``schema_pages`` switches the
    same command to the forward-compatible object form keyed by ``--page-token``.
    Every v1-only command (``folder list``, ``folder expunge``, ``message save``,
    ``message export``, ``envelope thread``, ``envelope list -f ...``) raises
    ``ForbiddenV1Command``.
    """

    def __init__(
        self,
        version_output: str = V3_VERSION_OUTPUT,
        *,
        folders: Iterable[str] = ("INBOX", "Drafts", "Archive"),
        envelopes: dict[str, Iterable[dict[str, Any]]] | None = None,
    ) -> None:
        super().__init__(version_output)
        self.folders: list[str] = [str(name) for name in folders]
        self.state: dict[str, list[dict[str, Any]]] = {name: [] for name in self.folders}
        for folder, entries in (envelopes or {}).items():
            self.state.setdefault(str(folder), []).extend(dict(entry) for entry in entries)
        #: ``--page-token`` value (or ``None`` for the first page) -> object payload.
        self.schema_pages: dict[str | None, dict[str, Any]] = {}
        #: Raw EML bytes keyed by envelope id, served by ``message read --raw``.
        self.raw_eml: dict[str, bytes] = {}
        #: Per-command overrides: ``"envelope list"`` -> callable(Command) -> str.
        self.responders: dict[str, Callable[[Command], str]] = {}
        self._next_id = 900

    # -- seeding -----------------------------------------------------------
    def add_envelope(self, folder: str, entry: dict[str, Any]) -> dict[str, Any]:
        """Seed one envelope in a folder and return it."""
        self.state.setdefault(folder, []).append(dict(entry))
        return entry

    def register_eml(self, envelope_id: str | int, eml: bytes) -> None:
        """Serve ``eml`` for one envelope id through ``message read --raw``."""
        self.raw_eml[str(envelope_id)] = bytes(eml)

    def on(self, name: str, responder: Callable[[Command], str]) -> None:
        """Override the answer of one parsed command name."""
        self.responders[name] = responder

    # -- dispatch ----------------------------------------------------------
    def _mailbox_stdout(self, argv: list[str]) -> str:
        command = parse_command(argv)
        responder = self.responders.get(command.name)
        if responder is not None:
            return responder(command)
        handler = _V3_HANDLERS.get(command.name)
        if handler is None:
            raise ForbiddenV1Command(
                f"v1-only Himalaya command emitted under the v3 profile: {argv!r}"
            )
        return handler(self, command)

    # -- handlers ----------------------------------------------------------
    def _h_mailbox_list(self, command: Command) -> str:
        return json.dumps(
            v3_mailbox_payload(
                [
                    {
                        "id": str(index + 1),
                        "name": name,
                        "role": "drafts" if name.lower() == "drafts" else None,
                        "total": len(self.state.get(name, [])),
                        "unread": 0,
                    }
                    for index, name in enumerate(self.folders)
                ]
            )
        )

    def _h_envelope_list(self, command: Command) -> str:
        folder = command.option("-m", "--mailbox") or ""
        if not folder:
            raise ForbiddenV1Command(
                f"v3 envelope list needs -m/--mailbox (the v1 -f form must not survive): {command.argv!r}"
            )
        if folder not in self.state:
            raise AssertionError(f"unknown mailbox in fixture: {folder!r}")
        if self.schema_pages:
            token = command.option("--page-token")
            payload = self.schema_pages.get(token)
            if payload is None:
                return json.dumps(
                    v3_schema_payload(errors=[{"kind": "page-token-invalid"}])
                )
            return json.dumps(payload)
        entries = [dict(entry) for entry in self.state[folder]]
        size = command.option("-s", "--page-size")
        if size is not None and str(size).isdigit():
            entries = entries[: int(size)]
        if command.option("--sort") == "oldest-first":
            entries.sort(key=lambda entry: str(entry.get("date") or ""))
        return json.dumps(entries)

    def _h_envelope_search(self, command: Command) -> str:
        folder = command.option("-m", "--mailbox") or ""
        if not folder:
            raise ForbiddenV1Command(
                f"v3 envelope search needs -m/--mailbox: {command.argv!r}"
            )
        entries = [dict(entry) for entry in self.state.get(folder, [])]
        queries = [token for token in command.positionals if token]
        for query in queries:
            prefix, _, value = query.partition(":")
            value = unquote_query_value(value)
            if prefix == "subject":
                entries = [entry for entry in entries if value.lower() in str(entry.get("subject") or "").lower()]
            elif prefix == "from":
                entries = [
                    entry
                    for entry in entries
                    if value.lower() in json.dumps(entry.get("from") or [], ensure_ascii=False).lower()
                ]
            elif prefix == "mid":
                entries = [entry for entry in entries if str(entry.get("messageId") or "").lower() == value.lower()]
            elif prefix == "date":
                entries = [entry for entry in entries if str(entry.get("date") or "").startswith(value)]
            elif prefix == "is" and value == "unread":
                entries = [
                    entry
                    for entry in entries
                    if not any(str(flag.get("raw", "")).lower() == "seen" for flag in entry.get("flags") or [])
                ]
            else:
                raise AssertionError(f"unsupported v3 search token reached the fixture: {query!r}")
        if "--sort" in command.flags:
            entries.sort(key=lambda entry: str(entry.get("date") or ""))
        if command.option("--sort") == "oldest-first":
            entries.sort(key=lambda entry: str(entry.get("date") or ""))
        return json.dumps(entries)

    def _h_message_read(self, command: Command) -> str:
        envelope_id = command.positionals[-1] if command.positionals else ""
        if "--raw" in command.flags:
            eml = self.raw_eml.get(envelope_id)
            if eml is None:
                raise AssertionError(f"no raw EML registered for envelope {envelope_id!r}")
            return json.dumps(v3_raw_payload(eml))
        folder = command.option("-m", "--mailbox") or ""
        for entry in self.state.get(folder, []):
            if str(entry.get("id")) == envelope_id:
                to_pairs = [
                    (str(item.get("name") or ""), str(item.get("email") or ""))
                    for item in entry.get("to") or []
                    if isinstance(item, dict)
                ]
                return json.dumps(
                    v3_message_view(
                        envelope_id,
                        message_id=str(entry.get("messageId") or ""),
                        subject=str(entry.get("subject") or ""),
                        from_name=(entry.get("from") or [{}])[0].get("name")
                        if entry.get("from")
                        else None,
                        from_email=str((entry.get("from") or [{}])[0].get("email") or ""),
                        to=tuple(to_pairs),
                        date=str(entry.get("date") or ""),
                    )
                )
        raise AssertionError(f"no envelope {envelope_id!r} in mailbox {folder!r}")

    def _h_message_add(self, command: Command) -> str:
        payload = self.stdin_payloads
        if not payload:
            raise AssertionError("message add must receive the raw message through stdin")
        raw = bytes(payload[-1])
        parsed = email.message_from_bytes(raw, policy=email.policy.SMTPUTF8)
        folder = command.option("--mailbox", "-m") or "INBOX"
        entry = v3_envelope(
            self._next_id,
            message_id=str(parsed.get("Message-ID", "")).strip("<>"),
            subject=str(parsed.get("Subject", "")),
            from_email=str(parsed.get("From", "")),
            to=(),
            flags=(("Draft", "\\Draft"),),
        )
        self._next_id += 1
        self.state.setdefault(folder, []).append(entry)
        return ""

    def _h_message_copy(self, command: Command) -> str:
        source = command.option("-f", "--folder") or ""
        target = command.option("-t", "--target") or ""
        envelope_id = command.positionals[-1] if command.positionals else ""
        for entry in list(self.state.get(source, [])):
            if str(entry.get("id")) == envelope_id:
                copied = dict(entry)
                copied["id"] = str(self._next_id)
                self._next_id += 1
                self.state.setdefault(target, []).append(copied)
                return ""
        raise AssertionError(f"no envelope {envelope_id!r} in mailbox {source!r}")

    def _h_message_delete(self, command: Command) -> str:
        folder = command.option("-m", "--mailbox") or ""
        envelope_id = command.positionals[-1] if command.positionals else ""
        kept = [entry for entry in self.state.get(folder, []) if str(entry.get("id")) != envelope_id]
        removed = [entry for entry in self.state.get(folder, []) if str(entry.get("id")) == envelope_id]
        self.state[folder] = kept
        self.state.setdefault("Trash", []).extend(removed)
        return ""

    def _h_flag_add(self, command: Command) -> str:
        folder = command.option("-m", "--mailbox") or ""
        flag = command.option("--flag", "-f")
        if not flag:
            raise ForbiddenV1Command(
                f"v3 flag add requires --flag <FLAG>: {command.argv!r}"
            )
        envelope_id = command.positionals[-1] if command.positionals else ""
        for entry in self.state.get(folder, []):
            if str(entry.get("id")) == envelope_id:
                iana = flag if flag in {"seen", "answered", "flagged", "draft", "important", "junk"} else None
                entry.setdefault("flags", []).append({"raw": flag, "iana": iana})
        return ""


_V3_HANDLERS: dict[str, Callable[[V3MailboxTransport, Command], str]] = {
    "mailbox list": V3MailboxTransport._h_mailbox_list,
    "envelope list": V3MailboxTransport._h_envelope_list,
    "envelope search": V3MailboxTransport._h_envelope_search,
    "message read": V3MailboxTransport._h_message_read,
    "message add": V3MailboxTransport._h_message_add,
    "message copy": V3MailboxTransport._h_message_copy,
    "message delete": V3MailboxTransport._h_message_delete,
    "flag add": V3MailboxTransport._h_flag_add,
}


class V1MailboxTransport(HimalayaTransport):
    """A minimal, hermetic v1.2.0 CLI for byte-parity checks at the old seams."""

    def __init__(
        self,
        version_output: str = V1_VERSION_OUTPUT,
        *,
        folders: Iterable[str] = ("INBOX", "Drafts", "Archive"),
        envelopes: dict[str, Iterable[dict[str, Any]]] | None = None,
    ) -> None:
        super().__init__(version_output)
        self.folders = [str(name) for name in folders]
        self.state: dict[str, list[dict[str, Any]]] = {name: [] for name in self.folders}
        for folder, entries in (envelopes or {}).items():
            self.state.setdefault(str(folder), []).extend(dict(entry) for entry in entries)

    def _mailbox_stdout(self, argv: list[str]) -> str:
        command = parse_command(argv)
        if command.name == "folder list":
            return json.dumps([{"name": name} for name in self.folders])
        if command.name == "envelope list":
            folder = command.option("-f", "--folder") or "INBOX"
            return json.dumps([dict(entry) for entry in self.state.get(folder, [])])
        if command.name == "message read":
            envelope_id = command.positionals[-1] if command.positionals else ""
            return f"Message-Id: <{envelope_id}@example.org>\nSubject: Alt\n\n"
        raise AssertionError(f"unexpected v1 invocation in fixture: {argv!r}")


# ---------------------------------------------------------------------------
# Hermetic bootstrap
# ---------------------------------------------------------------------------


@contextmanager
def patched_himalaya(transport: HimalayaTransport, *, executable_exists: bool = True):
    """Patch config, executable lookup and both transport seams for one test.

    Yields ``(himalaya_module, config_path, executable_path)``.  The temporary
    executable path is unique per ``with`` block, so a per-executable version
    cache can never leak a version from a previous test.  The adapter's
    pre-command profile lookup is *not* patched: it resolves the same executable
    and answers through the scripted ``--version`` seam, so the real guard path
    is exercised.
    """
    from core import himalaya  # imported lazily: the caller owns sys.path

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        config = root / "config.toml"
        config.write_text("[accounts.demo]\n", encoding="utf-8")
        executable = root / "himalaya.exe"
        executable.write_bytes(b"fake-himalaya-binary")
        with patch.dict(os.environ, {"HIMALAYA_CONFIG": str(config)}, clear=False), patch.object(
            himalaya.shutil, "which", return_value=str(executable) if executable_exists else None
        ), patch.object(himalaya.subprocess, "run", transport), patch.object(
            himalaya, "_POPEN", PopenAdapter(transport)
        ):
            yield himalaya, config, executable


def global_prefix(executable: str, config: str) -> list[str]:
    """Return the expected ``[exe, -c, <normalized config>]`` global prefix."""
    from core import himalaya  # imported lazily: the caller owns sys.path

    return [str(executable), "-c", str(himalaya.normalize_himalaya_config_path(config))]


def strip_global_prefix(argv: Sequence[str], *, with_json: bool = False) -> list[str]:
    """Drop ``[exe, -c, cfg]``, any global ``-a <name>`` and optionally ``--json``."""
    rest = [str(token) for token in argv]
    if len(rest) >= 3 and rest[1] == "-c":
        rest = rest[3:]
    tokens: list[str] = []
    index = 0
    while index < len(rest):
        token = rest[index]
        if token == "-a" and index + 1 < len(rest):
            index += 2
            continue
        if with_json and token == "--json":
            index += 1
            continue
        tokens.append(token)
        index += 1
    return tokens


_LONG_TO_SHORT = {
    "--mailbox": "-m",
    "--folder": "-f",
    "--target": "-t",
    "--page-size": "-s",
}


def normalized_v3_tokens(argv: Sequence[str]) -> list[str]:
    """Return the v3 subcommand tokens with globals, ``--json`` and long flags normalized.

    ``--json`` is a global flag and ``-m``/``--mailbox`` (and the other short/long
    pairs) are equivalent in v3, so a byte-exact order pin would over-specify the
    CLI.  The semantic token sequence stays exact.
    """
    return [_LONG_TO_SHORT.get(token, token) for token in strip_global_prefix(argv, with_json=True)]


def assert_v3_command(
    case: Any,
    argv: Sequence[str],
    expected: Sequence[str],
    *,
    require_json: bool = False,
) -> None:
    """Assert one v3 argv semantically: no v1 token, exact normalized tokens."""
    assert_no_v1_tokens(case, argv)
    if require_json:
        case.assertIn("--json", list(argv), f"this v3 command must carry --json: {list(argv)!r}")
    case.assertEqual(list(expected), normalized_v3_tokens(argv))


def assert_no_v1_tokens(case: Any, argv: Sequence[str]) -> None:
    """Fail when a v3 argv still carries a v1-only flag or subcommand word."""
    tokens = [str(token) for token in argv]
    for forbidden in ("-o", "--preview", "-H", "-F", "-d", "export", "thread", "explode"):
        case.assertNotIn(forbidden, tokens, f"v1-only token {forbidden!r} in v3 argv {tokens!r}")
    case.assertNotIn("folder", tokens, f"v1 'folder' subcommand in v3 argv {tokens!r}")
