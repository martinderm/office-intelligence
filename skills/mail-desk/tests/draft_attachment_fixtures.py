"""Synthetic fixtures for the draft-attachment tests (no live mailbox, no real mail or files).

* ``make_workspace`` builds a temporary consumer workspace with the four data zones, the
  credentials-free backend binding and ``data/mail-desk``.
* ``make_dir_link`` / ``make_file_symlink`` create a real symlink, or on Windows a real directory
  junction, so link-boundary tests exercise the operating system instead of a stub.
* ``MailboxBoundary`` fakes only the ``subprocess.run`` system boundary of ``core.himalaya``: the real
  ``run_himalaya`` (stdin payload, retries, error classification) and the real ``fetch_raw_message_eml``
  /``verify_in_target_folder`` code run against it.  It models the minimal Himalaya 1.2.0 mailbox
  semantics the draft operations need (``message save`` with the documented stdin normalization,
  ``envelope list``, ``message read -H``, ``message export -F -d``, ``flag add``, ``folder expunge``,
  ``folder list``) and offers failure / corruption injection.

This module is deliberately not named ``test_*.py`` so unittest discovery does not collect it.
"""

from __future__ import annotations

import email
import email.policy
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

from himalaya_fixtures import himalaya_stdin_normalize

_REAL_SUBPROCESS_RUN = subprocess.run

ZONES = ("memory/references", "memory/evidence", "memory/cloud", "data")
ACCOUNT = "demo"


def make_workspace(parent: Path, *, account: str | None = ACCOUNT) -> Path:
    """Create a consumer workspace skeleton below ``parent`` and return its resolved root."""
    root = (Path(parent) / "workspace").resolve()
    for zone in ZONES:
        (root / zone).mkdir(parents=True, exist_ok=True)
    (root / "data" / "mail-desk").mkdir(parents=True, exist_ok=True)
    (root / ".agents").mkdir(parents=True, exist_ok=True)
    (root / ".agents" / "mail-desk-backend.json").write_text(
        json.dumps({"schema_version": 1, "backend": "himalaya", "account": account}),
        encoding="utf-8",
    )
    return root


def make_dir_link(link: Path, target: Path) -> None:
    """Create a symlink, or on Windows a real directory junction, ``link`` -> ``target``."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except OSError:
        if os.name != "nt":
            raise
    import _winapi  # type: ignore[import-not-found]

    _winapi.CreateJunction(str(target), str(link))


def make_file_symlink(link: Path, target: Path) -> bool:
    """Create a file symlink; ``False`` when the host forbids it (Windows without privilege)."""
    try:
        os.symlink(target, link)
        return True
    except OSError:
        return False


def remove_dir_link(link: Path) -> None:
    """Remove a directory link / junction without touching its target."""
    os.rmdir(link)


# ---------------------------------------------------------------------------
# Mailbox model on the subprocess boundary
# ---------------------------------------------------------------------------

def _token_after(tokens: list[str], flag: str) -> str | None:
    try:
        index = tokens.index(flag)
    except ValueError:
        return None
    return str(tokens[index + 1]) if index + 1 < len(tokens) else None


class MailboxBoundary:
    """Callable replacement for ``subprocess.run`` modelling a small Himalaya mailbox."""

    def __init__(self, folders: list[Any] | None = None) -> None:
        self.folders: list[Any] = folders if folders is not None else ["INBOX", "Sent", "Drafts"]
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.saved_payloads: list[bytes] = []
        #: Per-folder stored messages: ``{"id", "raw"(str, normalized), "flags"}``.
        self.messages: dict[str, list[dict[str, Any]]] = {}
        self._next_id = 42
        #: Failure injection for ``message save``.
        self.save_exit = 0
        self.save_stderr = ""
        self.save_raises: BaseException | None = None
        #: Called at the very start of every ``message save`` (e.g. to inspect the journal mid-APPEND).
        self.on_save: Callable[[], None] | None = None
        #: Called after a successful ``message save`` stored its message (e.g. to renumber / remove another one).
        self.after_save: Callable[[], None] | None = None
        #: When ``False`` the save exits 0 but stores nothing (lost APPEND).
        self.save_stores = True
        #: Hook ``(raw) -> raw`` applied to the stored copy (corruption injection).
        self.mutate_stored: Callable[[str], str] | None = None
        #: Duplicate every stored message once (ambiguity injection).
        self.duplicate_on_save = False
        #: ``message export`` failure injection.
        self.export_exit = 0
        self.export_stderr = ""
        #: The "exported file" is a directory (not a regular file).
        self.export_as_directory = False
        #: Make a delete step fail.
        self.flag_exit = 0
        self.flag_stderr = ""

    # -- seeding -----------------------------------------------------------
    def seed(self, folder: str, raw: str, *, envelope_id: str | None = None) -> str:
        """Place a pre-existing message (CRLF raw text) into ``folder``; returns its id."""
        env_id = envelope_id or str(self._next_id)
        if envelope_id is None:
            self._next_id += 1
        self.messages.setdefault(folder, []).append({"id": env_id, "raw": raw, "flags": set()})
        return env_id

    # -- subprocess.run ----------------------------------------------------
    def __call__(self, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        tokens = list(cmd)
        if not tokens or tokens[0] != "himalaya":
            # ``subprocess.run`` is patched process-wide; anything that is not the Himalaya binary
            # (e.g. the real workspace-lock guard) must reach the genuine implementation.
            return _REAL_SUBPROCESS_RUN(cmd, **kwargs)
        self.calls.append((tokens, dict(kwargs)))
        if "folder" in tokens and "list" in tokens:
            return self._ok(kwargs, json.dumps([e if isinstance(e, dict) else {"name": str(e)} for e in self.folders]))
        if "message" in tokens and "save" in tokens:
            return self._save(tokens, kwargs)
        if "envelope" in tokens and "list" in tokens:
            return self._envelope_list(tokens, kwargs)
        if "message" in tokens and "read" in tokens:
            return self._read_header(tokens, kwargs)
        if "message" in tokens and "export" in tokens:
            return self._export(tokens, kwargs)
        if "flag" in tokens and "add" in tokens:
            return self._flag_add(tokens, kwargs)
        if "folder" in tokens and "expunge" in tokens:
            folder = tokens[-1]
            self.messages[folder] = [m for m in self.messages.get(folder, []) if "deleted" not in m["flags"]]
            return self._ok(kwargs, "")
        raise AssertionError(f"unexpected subprocess invocation: {tokens!r}")

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _ok(kwargs: dict[str, Any], stdout: str, stderr: str = "", code: int = 0) -> subprocess.CompletedProcess[Any]:
        if kwargs.get("text"):
            return subprocess.CompletedProcess([], code, stdout, stderr)
        return subprocess.CompletedProcess([], code, stdout.encode("utf-8"), stderr.encode("utf-8"))

    def _save(self, tokens: list[str], kwargs: dict[str, Any]) -> subprocess.CompletedProcess[Any]:
        payload = kwargs.get("input")
        assert isinstance(payload, bytes), "message save must receive a binary stdin payload"
        assert not kwargs.get("text"), "stdin payload must be binary"
        self.saved_payloads.append(bytes(payload))
        if self.on_save is not None:
            self.on_save()
        if self.save_raises is not None:
            raise self.save_raises
        if self.save_exit != 0:
            return self._ok(kwargs, "", self.save_stderr, self.save_exit)
        raw = himalaya_stdin_normalize(payload)
        if self.mutate_stored is not None:
            raw = self.mutate_stored(raw)
        folder = _token_after(tokens, "-f") or "INBOX"
        if self.save_stores:
            for _ in range(2 if self.duplicate_on_save else 1):
                self.messages.setdefault(folder, []).append({"id": str(self._next_id), "raw": raw, "flags": set()})
                self._next_id += 1
        if self.after_save is not None:
            self.after_save()
        return self._ok(kwargs, "Message saved\n")

    def _live(self, folder: str) -> list[dict[str, Any]]:
        return [m for m in self.messages.get(folder, []) if "deleted" not in m["flags"]]

    @staticmethod
    def _parse(raw: str) -> Any:
        return email.message_from_string(raw, policy=email.policy.SMTPUTF8)

    def _envelope_list(self, tokens: list[str], kwargs: dict[str, Any]) -> subprocess.CompletedProcess[Any]:
        folder = _token_after(tokens, "-f") or "INBOX"
        size = int(_token_after(tokens, "-s") or "50")
        payload = []
        for message in reversed(self._live(folder)):  # newest first
            parsed = self._parse(message["raw"])
            payload.append(
                {
                    "id": message["id"],
                    "subject": str(parsed.get("Subject", "") or ""),
                    "from": {"name": "", "addr": ""},
                    "date": str(parsed.get("Date", "") or ""),
                    "flags": sorted(message["flags"]),
                }
            )
        return self._ok(kwargs, json.dumps(payload[:size]))

    def _find(self, folder: str, env_id: str) -> dict[str, Any] | None:
        for message in self._live(folder):
            if message["id"] == env_id:
                return message
        return None

    def _read_header(self, tokens: list[str], kwargs: dict[str, Any]) -> subprocess.CompletedProcess[Any]:
        folder = _token_after(tokens, "-f") or "INBOX"
        message = self._find(folder, tokens[-1])
        if message is None:
            return self._ok(kwargs, "", "no such envelope", 1)
        parsed = self._parse(message["raw"])
        # Himalaya prints one line per header occurrence, so duplicate Message-ID headers show up twice.
        lines = "".join(f"Message-Id: {value}\n" for value in (parsed.get_all("Message-ID") or []))
        return self._ok(kwargs, f"{lines}\n")

    def _export(self, tokens: list[str], kwargs: dict[str, Any]) -> subprocess.CompletedProcess[Any]:
        if self.export_exit != 0:
            return self._ok(kwargs, "", self.export_stderr, self.export_exit)
        folder = _token_after(tokens, "-f") or "INBOX"
        env_id = next(t for t in tokens[tokens.index("export") + 1:] if t.isdigit())
        message = self._find(folder, env_id)
        if message is None:
            return self._ok(kwargs, "", "no such envelope", 1)
        destination = Path(_token_after(tokens, "-d") or "")
        if self.export_as_directory:
            destination.mkdir()
        else:
            destination.write_bytes(message["raw"].encode("utf-8"))
        return self._ok(kwargs, "exported\n")

    def _flag_add(self, tokens: list[str], kwargs: dict[str, Any]) -> subprocess.CompletedProcess[Any]:
        if self.flag_exit != 0:
            return self._ok(kwargs, "", self.flag_stderr, self.flag_exit)
        folder = _token_after(tokens, "-f") or "INBOX"
        ids = {t for t in tokens[2:] if t.isdigit()}
        for message in self.messages.get(folder, []):
            if message["id"] in ids:
                message["flags"].add("deleted")
        return self._ok(kwargs, "")

    # -- assertions --------------------------------------------------------
    def commands(self, *needles: str) -> list[list[str]]:
        return [c for c, _ in self.calls if all(n in c for n in needles)]

    @property
    def save_calls(self) -> list[list[str]]:
        return self.commands("message", "save")

    def stored(self, folder: str = "Drafts") -> list[str]:
        return [m["raw"] for m in self._live(folder)]


def exact_size_body(target: int, attachments: Any, **build_kwargs: Any) -> str:
    """Plain-text body that makes the serialized draft exactly ``target`` bytes long.

    Lines of 70 ASCII characters stay 7bit (<= 78 columns) and serialize to 72 bytes each; the head
    size is measured once with the builder, the rest is arithmetic.
    """
    from core import drafts

    kwargs = {"date": "Sat, 07 Nov 2026 12:00:00 +0100", "message_id": "<size@example.org>", **build_kwargs}
    line = "a" * 70 + "\n"
    head = len(
        drafts.build_draft_message("Betreff", "a@example.org", line, attachments=attachments, **kwargs)[0].encode("utf-8")
    ) - 72
    lines, rest = divmod(target - head, 72)
    if rest in (1, 2):
        lines, rest = lines - 1, rest + 72
    return line * lines + ("a" * (rest - 2) if rest else "")


def synthetic_old_draft(message_id: str, subject: str = "Alt", *, attachment: bytes | None = None) -> str:
    """Return a CRLF raw draft; with ``attachment`` a one-attachment multipart/mixed message."""
    from email.message import EmailMessage
    from email.policy import SMTPUTF8

    msg = EmailMessage(policy=SMTPUTF8.clone(linesep="\r\n"))
    msg["To"] = "old@example.org"
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    msg.set_content("Alter Text\n", subtype="plain", charset="utf-8")
    if attachment is not None:
        msg.add_attachment(attachment, maintype="application", subtype="pdf", filename="alt.pdf")
    return msg.as_string()


def temp_workspace() -> tuple[tempfile.TemporaryDirectory[str], Path]:
    tmp = tempfile.TemporaryDirectory()
    return tmp, make_workspace(Path(tmp.name))
