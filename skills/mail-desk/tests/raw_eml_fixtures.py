"""Synthetic Raw-EML builders for the issue #18 import tests (no real mail, no mailbox, no network).

The builders use the standard library ``EmailMessage`` directly (never the production draft builder), so
the expected bytes are independent of the code under test.  ``join_lines`` makes hand-written, deliberately
malformed messages explicit.

This module is deliberately not named ``test_*.py`` so unittest discovery does not collect it.
"""

from __future__ import annotations

from email.message import EmailMessage
from email.policy import SMTPUTF8
from typing import Iterable, Sequence

DEFAULT_DATE = "Sat, 07 Nov 2026 12:00:00 +0100"
DEFAULT_ID = "<raw1@example.org>"


def make_eml(
    *,
    subject: str = "Angebot – Prüfung",
    to: str = "Empfänger <a@example.org>",
    body: str = "Anbei die Unterlagen.\nZweite Zeile mit Grüßen 🎯\n",
    message_id: str | None = DEFAULT_ID,
    date: str | None = DEFAULT_DATE,
    from_addr: str | None = "me@example.org",
    cc: str | None = None,
    attachments: Sequence[tuple[str, str, bytes]] = (),
    extra_headers: Iterable[tuple[str, str]] = (),
    linesep: str = "\r\n",
) -> bytes:
    """A well-formed message: ``text/plain`` UTF-8 body, ``multipart/mixed`` when attachments are given."""
    message = EmailMessage(policy=SMTPUTF8.clone(linesep=linesep))
    if from_addr:
        message["From"] = from_addr
    if to:
        message["To"] = to
    if cc:
        message["Cc"] = cc
    message["Subject"] = subject
    if date:
        message["Date"] = date
    if message_id:
        message["Message-ID"] = message_id
    for name, value in extra_headers:
        message[name] = value
    message.set_content(body, subtype="plain", charset="utf-8")
    for filename, content_type, data in attachments:
        maintype, subtype = content_type.split("/", 1)
        message.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return message.as_string().encode("utf-8")


def join_lines(lines: Iterable[str], *, linesep: str = "\r\n", final: bool = True) -> bytes:
    """Hand-written message bytes from header/body lines."""
    text = linesep.join(lines)
    if final:
        text += linesep
    return text.encode("utf-8")


def headers(
    *, message_id: str | None = DEFAULT_ID, to: str | None = "a@example.org", date: str | None = DEFAULT_DATE,
    subject: str | None = "Betreff", extra: Iterable[str] = (),
) -> list[str]:
    """The header lines of a minimal valid plain-text message (no MIME headers)."""
    lines: list[str] = []
    if to is not None:
        lines.append(f"To: {to}")
    if subject is not None:
        lines.append(f"Subject: {subject}")
    if date is not None:
        lines.append(f"Date: {date}")
    if message_id is not None:
        lines.append(f"Message-ID: {message_id}")
    lines.extend(extra)
    return lines


def plain(extra_headers: Iterable[str] = (), body: Iterable[str] = ("Hallo Welt.",), **kwargs) -> bytes:
    """Minimal valid ASCII message; ``extra_headers`` are appended to the header block."""
    return join_lines([*headers(extra=extra_headers, **kwargs), "", *body])


def mixed(parts: Iterable[Iterable[str]], *, boundary: str = "BOUND", top_extra: Iterable[str] = ()) -> bytes:
    """Hand-written ``multipart/mixed`` message; every part is a list of its header + body lines."""
    lines = [
        *headers(),
        "MIME-Version: 1.0",
        f'Content-Type: multipart/mixed; boundary="{boundary}"',
        *top_extra,
        "",
    ]
    for part in parts:
        lines.append(f"--{boundary}")
        lines.extend(part)
    lines.append(f"--{boundary}--")
    return join_lines(lines)


BODY_PART = ["Content-Type: text/plain; charset=utf-8", "Content-Transfer-Encoding: 7bit", "", "Text."]


def attachment_part(
    filename: str = "a.bin", content_type: str = "application/octet-stream", payload: str = "AAEC",
    *, disposition: str = "attachment", encoding: str | None = "base64",
) -> list[str]:
    lines = [f"Content-Type: {content_type}"]
    if encoding:
        lines.append(f"Content-Transfer-Encoding: {encoding}")
    lines.append(f'Content-Disposition: {disposition}; filename="{filename}"' if filename else f"Content-Disposition: {disposition}")
    lines += ["", payload]
    return lines
