"""Issue #103, slice 1 (P1 extension): real-mail EML parity on the extended class.

A completed scan-inventory export may replace the following preview
``message read`` only for the accepted, closed input class.  The golden pair is
a synthetic RFC-822 EML plus the CLI read text the reference would print
(unfolded headers, RFC 2047 decoded, address quotes removed, the one
unambiguous text/plain body part decoded, preview truncated to
``preview_lines``).  Everything else falls back to the unchanged export + read
sequence without a second export.

All tests are hermetic: only the process boundary (``_POPEN``) is faked.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import himalaya  # noqa: E402
from core.modes import draft as draft_mode  # noqa: E402
import mail_desk_batch_runner as runner  # noqa: E402
from transport_hermetic_guard import HermeticProcessTestCase  # noqa: E402


GOLDEN_MESSAGE_ID = "<golden-61@example.test>"


class _FakeProcess:
    def __init__(self, cmd, *, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.cmd = cmd
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode

    def communicate(self, input=None, timeout=None):  # noqa: A002 - Popen signature
        return self.stdout, self.stderr

    def kill(self) -> None:
        pass


def _eml(
    headers: list[tuple[str, str]],
    body: list[str],
    *,
    content_type: str | None = "text/plain; charset=utf-8",
    cte: str | None = None,
    separator: bool = True,
) -> bytes:
    lines = [f"{name}: {value}" for name, value in headers]
    if content_type is not None:
        lines.append(f"Content-Type: {content_type}")
    if cte is not None:
        lines.append(f"Content-Transfer-Encoding: {cte}")
    if separator:
        raw = "\r\n".join(lines) + "\r\n\r\n" + "\r\n".join(body) + "\r\n"
    else:
        raw = "\r\n".join(lines) + "\r\n"
    return raw.encode("utf-8")


def _multipart_eml(
    subtype: str,
    parts: list[list[str]],
    *,
    headers: list[str] | None = None,
    boundary: str = "b1",
) -> bytes:
    lines = headers if headers is not None else [
        f"Message-ID: {GOLDEN_MESSAGE_ID}",
        "Subject: Kurzfrage zum Vorgang",
        "From: Sender <sender@example.test>",
        "To: desk@example.test",
        "Date: Thu, 01 Jan 2026 00:00:00 +0000",
        "MIME-Version: 1.0",
        f'Content-Type: multipart/{subtype}; boundary="{boundary}"',
    ]
    lines = [*lines, ""]
    for part in parts:
        lines.append(f"--{boundary}")
        lines.extend(part)
    lines.append(f"--{boundary}--")
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def _part(body: list[str], content_type: str = "text/plain; charset=utf-8", *, extra: list[str] | None = None) -> list[str]:
    return [f"Content-Type: {content_type}", *(extra or []), "", *body]


def _cli_text(header_pairs: list[tuple[str, str]], body_text: str) -> str:
    """Model of the CLI read text for a body with exactly one trailing newline.

    This is a **model, not captured CLI output**: the M09 reference observed the
    CLI preview as the EML preview plus one byte (the process-terminator empty
    line).  Bodies without or with several trailing newlines are modelled here,
    not proven.
    """
    lines = "".join(f"{name}: {value}\n" for name, value in header_pairs)
    return lines + "\n" + body_text.rstrip("\n") + "\n\n"


def _cli_text_raw(header_pairs: list[tuple[str, str]], body_text: str) -> str:
    """Model of the CLI read text for a body without a trailing line break.

    Same model caveat as :func:`_cli_text`: the absent terminator line is a
    modelling choice, not a captured CLI observation.
    """
    lines = "".join(f"{name}: {value}\n" for name, value in header_pairs)
    return lines + "\n" + body_text + "\n"


def _cli_read_text(eml: bytes) -> str:
    """Model of the CLI read text for simple unencoded single-part fixtures.

    Appends the modelled process-terminator newline when the body already ends
    with one (see :func:`_cli_text`).
    """
    text = eml.decode("utf-8").replace("\r\n", "\n")
    header_block, separator, body = text.partition("\n\n")
    if not separator:
        return header_block
    return header_block + "\n\n" + body + ("\n" if body.endswith("\n") else "")


def _base_headers(message_id: str = GOLDEN_MESSAGE_ID) -> list[tuple[str, str]]:
    return [
        ("Message-ID", message_id),
        ("Subject", "Kurzfrage zum Vorgang"),
        ("From", "Sender <sender@example.test>"),
        ("To", "desk@example.test"),
        ("Date", "Thu, 01 Jan 2026 00:00:00 +0000"),
    ]


def _dispatch(eml: bytes, *, read_text: str | None, calls: list[str]):
    """Fake Popen: the export writes the EML file, the read returns CLI text."""

    def fake_popen(cmd, **_kwargs):
        if "export" in cmd:
            destination = Path(cmd[cmd.index("-d") + 1])
            destination.write_bytes(eml)
            calls.append("export")
            return _FakeProcess(cmd, stdout="")
        if "read" in cmd:
            calls.append("read")
            if read_text is None:
                raise AssertionError("unexpected CLI read")
            return _FakeProcess(cmd, stdout=read_text)
        raise AssertionError(f"unexpected command: {cmd}")

    return fake_popen


def _details(
    eml: bytes,
    *,
    allow: bool,
    cli_text: str | None = None,
    preview_lines: int = 30,
    full_body: bool = False,
    purpose: str = "scan_preview",
    fallback: dict | None = None,
) -> tuple[dict, list[str], dict]:
    calls: list[str] = []
    read_text = cli_text if cli_text is not None else _cli_read_text(eml)
    with patch.object(
        himalaya,
        "build_himalaya_command",
        side_effect=lambda command, account=None: ["himalaya", *command],
    ), patch.object(
        himalaya,
        "_POPEN",
        side_effect=_dispatch(eml, read_text=read_text, calls=calls),
    ), patch.object(
        himalaya.time, "sleep"
    ), himalaya.transport_stats_session() as stats:
        details = himalaya.get_single_email_details(
            "61",
            "INBOX",
            preview_lines=preview_lines,
            fallback_envelope=fallback,
            full_body=full_body,
            purpose=purpose,
            allow_eml_details=allow,
        )
    return details, calls, stats.snapshot()


class _NullTracker:
    def __init__(self, **_kwargs) -> None:
        pass

    def step(self, *_args, **_kwargs) -> None:
        pass

    def advance_item(self, *_args, **_kwargs) -> None:
        pass

    def complete(self, *_args, **_kwargs) -> None:
        pass


class EmlParityGoldenTests(HermeticProcessTestCase):
    """Point 1: accepted real-mail classes match the CLI read for every field."""

    def _assert_parity(
        self,
        eml: bytes,
        *,
        cli_text: str | None = None,
        preview_lines: int = 30,
    ) -> dict:
        reference, reference_calls, _ = _details(
            eml, allow=False, cli_text=cli_text, preview_lines=preview_lines
        )
        candidate, candidate_calls, snapshot = _details(
            eml, allow=True, cli_text=cli_text, preview_lines=preview_lines
        )
        self.assertEqual(["export", "read"], reference_calls)
        self.assertEqual(["export"], candidate_calls)
        self.assertEqual(reference, candidate)
        self.assertEqual(1, snapshot["starts_by_purpose"]["scan_inventory"])
        self.assertEqual(0, snapshot["starts_by_purpose"]["scan_preview"])
        self.assertEqual(
            snapshot["process_starts"], sum(snapshot["starts_by_purpose"].values())
        )
        return candidate

    def test_plain_short_body_matches_the_cli_read_field_for_field(self) -> None:
        details = self._assert_parity(_eml(_base_headers(), ["Erste Zeile.", "Zweite Zeile."]))
        self.assertEqual("golden-61@example.test", details["message_id"])
        self.assertEqual(GOLDEN_MESSAGE_ID, details["raw_message_id"])
        self.assertEqual("Kurzfrage zum Vorgang", details["subject"])
        self.assertEqual("Erste Zeile.\nZweite Zeile.\n", details["preview"])
        self.assertEqual("preview", details["read_level"])
        self.assertTrue(details["headers_complete"])
        self.assertIsNone(details["error"])
        self.assertIsNone(details["read_error_reason"])

    def test_optional_headers_are_carried_like_the_cli(self) -> None:
        headers = _base_headers() + [
            ("Cc", "cc@example.test"),
            ("In-Reply-To", "<parent@example.test>"),
            ("References", "<parent@example.test> <root@example.test>"),
        ]
        cli_headers = _base_headers() + [
            ("Cc", "cc@example.test"),
            ("In-Reply-To", "<parent@example.test>"),
            ("References", "parent@example.test root@example.test"),
        ]
        details = self._assert_parity(_eml(headers, ["Antwort."]), cli_text=_cli_text(cli_headers, "Antwort."))
        self.assertEqual("cc@example.test", details["cc"])
        self.assertEqual("<parent@example.test>", details["in_reply_to"])
        self.assertEqual("parent@example.test root@example.test", details["references"])

    def test_utf8_body_matches_the_cli_text_unchanged(self) -> None:
        body = ["Grüße aus Wien – Preis: 10 €."]
        details = self._assert_parity(_eml(_base_headers(), body))
        self.assertEqual(body[0] + "\n", details["preview"])

    def test_folded_headers_are_unfolded_with_one_space(self) -> None:
        eml = _eml(
            [
                ("Message-ID", GOLDEN_MESSAGE_ID),
                ("Subject", "Kurzfrage\r\n zum Vorgang"),
                ("From", "Sender\r\n <sender@example.test>"),
                ("To", "desk@example.test"),
                ("Date", "Thu, 01 Jan 2026 00:00:00 +0000"),
            ],
            ["Body."],
        )
        cli = _cli_text(
            [
                ("Message-ID", GOLDEN_MESSAGE_ID),
                ("Subject", "Kurzfrage zum Vorgang"),
                ("From", "Sender <sender@example.test>"),
                ("To", "desk@example.test"),
                ("Date", "Thu, 01 Jan 2026 00:00:00 +0000"),
            ],
            "Body.",
        )
        details = self._assert_parity(eml, cli_text=cli)
        self.assertEqual("Kurzfrage zum Vorgang", details["subject"])
        self.assertEqual("Sender <sender@example.test>", details["from"])

    def test_rfc2047_headers_are_decoded(self) -> None:
        headers = [
            ("Message-ID", GOLDEN_MESSAGE_ID),
            ("Subject", "=?utf-8?q?Kurzfrage_zum_Vorgang?="),
            ("From", "=?utf-8?q?S=C3=A4nder?= <sender@example.test>"),
            ("To", "=?utf-8?q?Desk?= <desk@example.test>"),
            ("Date", "Thu, 01 Jan 2026 00:00:00 +0000"),
        ]
        eml = _eml(headers, ["Body."])
        cli = _cli_text(
            [
                ("Message-ID", GOLDEN_MESSAGE_ID),
                ("Subject", "Kurzfrage zum Vorgang"),
                ("From", "Sänder <sender@example.test>"),
                ("To", "Desk <desk@example.test>"),
                ("Date", "Thu, 01 Jan 2026 00:00:00 +0000"),
            ],
            "Body.",
        )
        details = self._assert_parity(eml, cli_text=cli)
        self.assertEqual("Kurzfrage zum Vorgang", details["subject"])
        self.assertEqual("Sänder <sender@example.test>", details["from"])
        self.assertEqual("Desk <desk@example.test>", details["to"])

    def test_address_display_quotes_are_removed_like_the_cli(self) -> None:
        headers = [
            ("Message-ID", GOLDEN_MESSAGE_ID),
            ("Subject", "Kurzfrage zum Vorgang"),
            ("From", '"Doe, John" <john@example.test>'),
            ("To", '"Desk, Team" <desk@example.test>'),
            ("Date", "Thu, 01 Jan 2026 00:00:00 +0000"),
        ]
        eml = _eml(headers, ["Body."])
        cli = _cli_text(
            [
                ("Message-ID", GOLDEN_MESSAGE_ID),
                ("Subject", "Kurzfrage zum Vorgang"),
                ("From", "Doe, John <john@example.test>"),
                ("To", "Desk, Team <desk@example.test>"),
                ("Date", "Thu, 01 Jan 2026 00:00:00 +0000"),
            ],
            "Body.",
        )
        details = self._assert_parity(eml, cli_text=cli)
        self.assertEqual("Doe, John <john@example.test>", details["from"])
        self.assertEqual("Desk, Team <desk@example.test>", details["to"])

    def test_address_without_display_name_loses_the_brackets(self) -> None:
        headers = [
            ("Message-ID", GOLDEN_MESSAGE_ID),
            ("Subject", "Kurzfrage zum Vorgang"),
            ("From", "Sender <sender@example.test>"),
            ("To", "<desk@example.test>"),
            ("Date", "Thu, 01 Jan 2026 00:00:00 +0000"),
        ]
        details = self._assert_parity(
            _eml(headers, ["Body."]),
            cli_text=_cli_text(
                [
                    ("Message-ID", GOLDEN_MESSAGE_ID),
                    ("Subject", "Kurzfrage zum Vorgang"),
                    ("From", "Sender <sender@example.test>"),
                    ("To", "desk@example.test"),
                    ("Date", "Thu, 01 Jan 2026 00:00:00 +0000"),
                ],
                "Body.",
            ),
        )
        self.assertEqual("desk@example.test", details["to"])

    def test_multiple_addresses_are_rendered_per_address(self) -> None:
        headers = [
            ("Message-ID", GOLDEN_MESSAGE_ID),
            ("Subject", "Kurzfrage zum Vorgang"),
            ("From", "Sender <sender@example.test>"),
            ("To", '"Desk" <desk@example.test>, <second@example.test>'),
            ("Date", "Thu, 01 Jan 2026 00:00:00 +0000"),
        ]
        details = self._assert_parity(
            _eml(headers, ["Body."]),
            cli_text=_cli_text(
                [
                    ("Message-ID", GOLDEN_MESSAGE_ID),
                    ("Subject", "Kurzfrage zum Vorgang"),
                    ("From", "Sender <sender@example.test>"),
                    ("To", "Desk <desk@example.test>, second@example.test"),
                    ("Date", "Thu, 01 Jan 2026 00:00:00 +0000"),
                ],
                "Body.",
            ),
        )
        self.assertEqual("Desk <desk@example.test>, second@example.test", details["to"])

    def test_references_rfc2047_is_decoded(self) -> None:
        headers = _base_headers() + [
            ("References", "=?utf-8?q?=3Cparent=40example.test=3E?="),
        ]
        details = self._assert_parity(
            _eml(headers, ["Body."]),
            cli_text=_cli_text(
                _base_headers() + [("References", "parent@example.test")], "Body."
            ),
        )
        self.assertEqual("parent@example.test", details["references"])

    def test_references_list_has_no_brackets_in_reply_to_keeps_them(self) -> None:
        ids = [f"<id{index}@example.test>" for index in range(5)]
        headers = _base_headers() + [
            ("References", " ".join(ids)),
            ("In-Reply-To", "<parent@example.test>"),
        ]
        details = self._assert_parity(
            _eml(headers, ["Body."]),
            cli_text=_cli_text(
                _base_headers()
                + [
                    ("References", " ".join(f"id{index}@example.test" for index in range(5))),
                    ("In-Reply-To", "<parent@example.test>"),
                ],
                "Body.",
            ),
        )
        self.assertEqual(
            " ".join(f"id{index}@example.test" for index in range(5)),
            details["references"],
        )
        self.assertEqual("<parent@example.test>", details["in_reply_to"])

    def test_multipart_alternative_uses_the_unique_text_plain_part(self) -> None:
        eml = _multipart_eml(
            "alternative",
            [
                _part(["Erste Zeile.", "Zweite Zeile."]),
                _part(["<p>Erste Zeile.</p>"], "text/html; charset=utf-8"),
            ],
        )
        details = self._assert_parity(
            eml,
            cli_text=_cli_text_raw(_base_headers(), "Erste Zeile.\nZweite Zeile."),
        )
        self.assertEqual("Erste Zeile.\nZweite Zeile.", details["preview"])
        self.assertEqual("preview", details["read_level"])

    def test_multipart_mixed_without_attachment_uses_the_text_body(self) -> None:
        eml = _multipart_eml("mixed", [_part(["Body."])])
        details = self._assert_parity(
            eml, cli_text=_cli_text_raw(_base_headers(), "Body.")
        )
        self.assertEqual("Body.", details["preview"])

    def test_multipart_related_without_attachment_uses_the_text_body(self) -> None:
        eml = _multipart_eml("related", [_part(["Body."])])
        details = self._assert_parity(
            eml, cli_text=_cli_text_raw(_base_headers(), "Body.")
        )
        self.assertEqual("Body.", details["preview"])

    def test_preview_is_truncated_to_preview_lines(self) -> None:
        body = [f"Zeile {index}" for index in range(40)]
        details = self._assert_parity(
            _eml(_base_headers(), body),
            cli_text=_cli_text(_base_headers(), "\n".join(body)),
            preview_lines=30,
        )
        self.assertEqual(30, len(details["preview"].splitlines()))
        self.assertEqual("Zeile 29", details["preview"].splitlines()[-1])

    def test_body_with_a_trailing_newline_gains_the_cli_terminator_line(self) -> None:
        details = self._assert_parity(
            _eml(_base_headers(), ["Body."]),
            cli_text=_cli_text(_base_headers(), "Body."),
        )
        self.assertEqual("Body.\n", details["preview"])

    def test_body_without_a_trailing_newline_has_no_terminator_line(self) -> None:
        headers = [
            f"Message-ID: {GOLDEN_MESSAGE_ID}",
            "Subject: Kurzfrage zum Vorgang",
            "From: Sender <sender@example.test>",
            "To: desk@example.test",
            "Date: Thu, 01 Jan 2026 00:00:00 +0000",
        ]
        eml = ("\r\n".join(headers) + "\r\n\r\nBody.").encode("utf-8")
        cli = _cli_text_raw(_base_headers(), "Body.")
        details = self._assert_parity(eml, cli_text=cli)
        self.assertEqual("Body.", details["preview"])

    def test_preview_truncation_with_terminator_is_hard_coded(self) -> None:
        expectations = {29: (29, 116), 30: (30, 119), 31: (30, 119)}
        for count, (line_count, length) in expectations.items():
            body = [f"L{index:02d}" for index in range(1, count + 1)]
            with self.subTest(count=count):
                details = self._assert_parity(
                    _eml(_base_headers(), body),
                    cli_text=_cli_text(_base_headers(), "\n".join(body)),
                    preview_lines=30,
                )
                self.assertEqual(line_count, len(details["preview"].splitlines()))
                self.assertEqual(length, len(details["preview"]))

    def test_preview_lines_zero_and_small(self) -> None:
        body = ["A", "B", "C", "D", "E"]
        zero = self._assert_parity(
            _eml(_base_headers(), body),
            cli_text=_cli_text(_base_headers(), "\n".join(body)),
            preview_lines=0,
        )
        self.assertEqual("", zero["preview"])
        three = self._assert_parity(
            _eml(_base_headers(), body),
            cli_text=_cli_text(_base_headers(), "\n".join(body)),
            preview_lines=3,
        )
        self.assertEqual("A\nB\nC", three["preview"])

    def test_two_trailing_newlines_keep_both_terminator_lines(self) -> None:
        eml = _eml(_base_headers(), ["Body.", ""])
        cli = (
            "Message-ID: <golden-61@example.test>\n"
            "Subject: Kurzfrage zum Vorgang\n"
            "From: Sender <sender@example.test>\n"
            "To: desk@example.test\n"
            "Date: Thu, 01 Jan 2026 00:00:00 +0000\n"
            "\n"
            "Body.\n"
            "\n"
            "\n"
        )
        details = self._assert_parity(eml, cli_text=cli)
        self.assertEqual("Body.\n\n", details["preview"])

    def test_hard_coded_cli_literal_matches_a_plain_body(self) -> None:
        cli = (
            "Message-ID: <golden-61@example.test>\n"
            "Subject: Kurzfrage zum Vorgang\n"
            "From: Sender <sender@example.test>\n"
            "To: desk@example.test\n"
            "Date: Thu, 01 Jan 2026 00:00:00 +0000\n"
            "\n"
            "Body.\n"
            "\n"
        )
        details = self._assert_parity(_eml(_base_headers(), ["Body."]), cli_text=cli)
        self.assertEqual("Body.\n", details["preview"])

    def test_base64_body_with_a_trailing_cr_has_no_terminator_line(self) -> None:
        payload = base64.b64encode(b"Body.\r").decode("ascii")
        details = self._assert_parity(
            _eml(_base_headers(), [payload], cte="base64"),
            cli_text=_cli_text_raw(_base_headers(), "Body."),
        )
        self.assertEqual("Body.", details["preview"])

    def test_quoted_special_characters_stay_in_one_element(self) -> None:
        headers = [
            ("Message-ID", GOLDEN_MESSAGE_ID),
            ("Subject", "Kurzfrage zum Vorgang"),
            ("From", "Sender <sender@example.test>"),
            ("To", '"Doe: John; Jr." <john@example.test>'),
            ("Date", "Thu, 01 Jan 2026 00:00:00 +0000"),
        ]
        details = self._assert_parity(
            _eml(headers, ["Body."]),
            cli_text=_cli_text(
                [
                    ("Message-ID", GOLDEN_MESSAGE_ID),
                    ("Subject", "Kurzfrage zum Vorgang"),
                    ("From", "Sender <sender@example.test>"),
                    ("To", "Doe: John; Jr. <john@example.test>"),
                    ("Date", "Thu, 01 Jan 2026 00:00:00 +0000"),
                ],
                "Body.",
            ),
        )
        self.assertEqual("Doe: John; Jr. <john@example.test>", details["to"])

    def test_iso_8859_1_part_is_decoded_with_its_charset(self) -> None:
        headers = [
            f"Message-ID: {GOLDEN_MESSAGE_ID}",
            "Subject: Kurzfrage zum Vorgang",
            "From: Sender <sender@example.test>",
            "To: desk@example.test",
            "Date: Thu, 01 Jan 2026 00:00:00 +0000",
            "Content-Type: text/plain; charset=iso-8859-1",
        ]
        eml = (
            "\r\n".join(headers).encode("ascii")
            + b"\r\n\r\n"
            + "Grüße aus Wien.".encode("iso-8859-1")
            + b"\r\n"
        )
        details = self._assert_parity(
            eml, cli_text=_cli_text(_base_headers(), "Grüße aus Wien.")
        )
        self.assertEqual("Grüße aus Wien.\n", details["preview"])

    def test_base64_body_is_decoded_like_the_cli(self) -> None:
        payload = base64.b64encode("Grüße.".encode("utf-8")).decode("ascii")
        details = self._assert_parity(
            _eml(_base_headers(), [payload], cte="base64"),
            cli_text=_cli_text_raw(_base_headers(), "Grüße."),
        )
        self.assertEqual("Grüße.", details["preview"])

    def test_quoted_printable_body_is_decoded_like_the_cli(self) -> None:
        details = self._assert_parity(
            _eml(_base_headers(), ["Body."], cte="quoted-printable"),
            cli_text=_cli_text(_base_headers(), "Body."),
        )
        self.assertEqual("Body.\n", details["preview"])

    def test_message_id_with_single_brackets_is_accepted(self) -> None:
        details = self._assert_parity(_eml(_base_headers("<single@example.test>"), ["Body."]))
        self.assertEqual("single@example.test", details["message_id"])
        self.assertEqual("<single@example.test>", details["raw_message_id"])

    def test_message_id_bracket_shapes_are_checked_on_the_raw_value(self) -> None:
        self.assertTrue(himalaya._is_well_formed_message_id("<a@b>"))
        self.assertTrue(himalaya._is_well_formed_message_id("a@b"))
        self.assertFalse(himalaya._is_well_formed_message_id("<<a@b>>"))
        self.assertFalse(himalaya._is_well_formed_message_id("<a@b"))
        self.assertFalse(himalaya._is_well_formed_message_id("a@b>"))

    def test_cc_and_in_reply_to_match_the_cli(self) -> None:
        headers = _base_headers() + [
            ("Cc", '"Carbon, Copy" <cc@example.test>'),
            ("In-Reply-To", "<parent@example.test>"),
        ]
        details = self._assert_parity(
            _eml(headers, ["Body."]),
            cli_text=_cli_text(
                _base_headers()
                + [
                    ("Cc", "Carbon, Copy <cc@example.test>"),
                    ("In-Reply-To", "<parent@example.test>"),
                ],
                "Body.",
            ),
        )
        self.assertEqual("Carbon, Copy <cc@example.test>", details["cc"])
        self.assertEqual("<parent@example.test>", details["in_reply_to"])

    def test_subject_internal_double_space_is_preserved(self) -> None:
        headers = [
            ("Message-ID", GOLDEN_MESSAGE_ID),
            ("Subject", "Kurzfrage  zum Vorgang"),
            ("From", "Sender <sender@example.test>"),
            ("To", "desk@example.test"),
            ("Date", "Thu, 01 Jan 2026 00:00:00 +0000"),
        ]
        details = self._assert_parity(
            _eml(headers, ["Body."]),
            cli_text=_cli_text(
                [
                    ("Message-ID", GOLDEN_MESSAGE_ID),
                    ("Subject", "Kurzfrage  zum Vorgang"),
                    ("From", "Sender <sender@example.test>"),
                    ("To", "desk@example.test"),
                    ("Date", "Thu, 01 Jan 2026 00:00:00 +0000"),
                ],
                "Body.",
            ),
        )
        self.assertEqual("Kurzfrage  zum Vorgang", details["subject"])

    def test_windows_1252_part_is_decoded(self) -> None:
        headers = [
            f"Message-ID: {GOLDEN_MESSAGE_ID}",
            "Subject: Kurzfrage zum Vorgang",
            "From: Sender <sender@example.test>",
            "To: desk@example.test",
            "Date: Thu, 01 Jan 2026 00:00:00 +0000",
            "Content-Type: text/plain; charset=windows-1252",
        ]
        eml = (
            "\r\n".join(headers).encode("ascii")
            + b"\r\n\r\n"
            + "Grüße – Wien.".encode("windows-1252")
            + b"\r\n"
        )
        details = self._assert_parity(
            eml, cli_text=_cli_text(_base_headers(), "Grüße – Wien.")
        )
        self.assertEqual("Grüße – Wien.\n", details["preview"])

    def test_8bit_transfer_encoding_is_accepted(self) -> None:
        details = self._assert_parity(
            _eml(_base_headers(), ["Body."], cte="8bit"),
            cli_text=_cli_text(_base_headers(), "Body."),
        )
        self.assertEqual("Body.\n", details["preview"])

    def test_q_encoding_with_a_literal_percent_is_accepted(self) -> None:
        headers = [
            ("Message-ID", GOLDEN_MESSAGE_ID),
            ("Subject", "=?utf-8?q?100%_Zuschlag?="),
            ("From", "Sender <sender@example.test>"),
            ("To", "desk@example.test"),
            ("Date", "Thu, 01 Jan 2026 00:00:00 +0000"),
        ]
        details = self._assert_parity(
            _eml(headers, ["Body."]),
            cli_text=_cli_text(
                [
                    ("Message-ID", GOLDEN_MESSAGE_ID),
                    ("Subject", "100% Zuschlag"),
                    ("From", "Sender <sender@example.test>"),
                    ("To", "desk@example.test"),
                    ("Date", "Thu, 01 Jan 2026 00:00:00 +0000"),
                ],
                "Body.",
            ),
        )
        self.assertEqual("100% Zuschlag", details["subject"])

    def test_bare_lf_line_endings_are_accepted(self) -> None:
        headers = [
            f"Message-ID: {GOLDEN_MESSAGE_ID}",
            "Subject: Kurzfrage zum Vorgang",
            "From: Sender <sender@example.test>",
            "To: desk@example.test",
            "Date: Thu, 01 Jan 2026 00:00:00 +0000",
        ]
        eml = ("\n".join(headers) + "\n\nBody.\n").encode("utf-8")
        details = self._assert_parity(
            eml, cli_text=_cli_text(_base_headers(), "Body.")
        )
        self.assertEqual("Body.\n", details["preview"])

    def test_missing_optional_headers_use_the_envelope_fallback(self) -> None:
        headers = [
            ("Message-ID", GOLDEN_MESSAGE_ID),
        ]
        eml = _eml(headers, ["Body."])
        fallback = {
            "subject": "Envelope subject",
            "from": {"name": "Envelope", "addr": "sender@example.test"},
            "to": {"name": "Desk", "addr": "desk@example.test"},
            "date": "2026-01-01",
        }
        reference, _calls, _snapshot = _details(
            eml,
            allow=False,
            cli_text=_cli_text([("Message-ID", GOLDEN_MESSAGE_ID)], "Body."),
            fallback=fallback,
        )
        candidate, calls, _snapshot = _details(
            eml,
            allow=True,
            cli_text=_cli_text([("Message-ID", GOLDEN_MESSAGE_ID)], "Body."),
            fallback=fallback,
        )
        self.assertEqual(["export"], calls)
        self.assertEqual(reference, candidate)
        self.assertEqual("Envelope subject", candidate["subject"])
        self.assertEqual("Envelope <sender@example.test>", candidate["from"])


class EmlParityRejectionTests(HermeticProcessTestCase):
    """Point 2: every unproven class falls back to exactly one export + one read."""

    def _assert_rejected(
        self,
        eml: bytes,
        *,
        cli_text: str | None = None,
        preview_lines: int = 30,
    ) -> None:
        reference, reference_calls, _ = _details(
            eml, allow=False, cli_text=cli_text, preview_lines=preview_lines
        )
        candidate, candidate_calls, snapshot = _details(
            eml, allow=True, cli_text=cli_text, preview_lines=preview_lines
        )
        self.assertEqual(["export", "read"], reference_calls)
        self.assertEqual(["export", "read"], candidate_calls)
        self.assertEqual(reference, candidate)
        self.assertEqual(1, snapshot["starts_by_purpose"]["scan_inventory"])
        self.assertEqual(1, snapshot["starts_by_purpose"]["scan_preview"])

    def test_duplicate_message_id_is_rejected(self) -> None:
        headers = _base_headers() + [("Message-ID", "<second@example.test>")]
        self._assert_rejected(_eml(headers, ["Body."]))

    def test_missing_message_id_is_rejected(self) -> None:
        headers = [pair for pair in _base_headers() if pair[0] != "Message-ID"]
        self._assert_rejected(_eml(headers, ["Body."]))

    def test_generated_message_id_is_rejected(self) -> None:
        self._assert_rejected(_eml(_base_headers("<abc@generated>"), ["Body."]))

    def test_malformed_message_id_is_rejected(self) -> None:
        self._assert_rejected(_eml(_base_headers("<not-an-id>"), ["Body."]))

    def test_nested_message_id_brackets_are_rejected(self) -> None:
        self._assert_rejected(_eml(_base_headers("<<a@b>>"), ["Body."]))

    def test_html_only_is_rejected(self) -> None:
        self._assert_rejected(
            _eml(_base_headers(), ["<p>HTML</p>"], content_type="text/html; charset=utf-8")
        )

    def test_multipart_alternative_with_two_text_parts_is_rejected(self) -> None:
        eml = _multipart_eml(
            "alternative",
            [_part(["one"]), _part(["two"])],
        )
        self._assert_rejected(eml, cli_text=_cli_text(_base_headers(), "one"))

    def test_multipart_mixed_with_two_text_bodies_is_rejected(self) -> None:
        eml = _multipart_eml("mixed", [_part(["one"]), _part(["two"])])
        self._assert_rejected(eml, cli_text=_cli_text(_base_headers(), "one"))

    def test_multipart_without_a_text_part_is_rejected(self) -> None:
        eml = _multipart_eml(
            "mixed",
            [_part(["<p>html</p>"], "text/html; charset=utf-8")],
        )
        self._assert_rejected(eml, cli_text=_cli_text(_base_headers(), "<p>html</p>"))

    def test_attachment_only_text_part_is_never_the_body(self) -> None:
        eml = _multipart_eml(
            "mixed",
            [
                _part(
                    ["Body."],
                    extra=['Content-Disposition: attachment; filename="a.txt"'],
                )
            ],
        )
        self._assert_rejected(eml, cli_text=_cli_text(_base_headers(), "Body."))

    def test_top_level_attachment_disposition_is_rejected(self) -> None:
        self._assert_rejected(
            _eml(
                _base_headers() + [("Content-Disposition", "attachment; filename=\"a.txt\"")],
                ["Body."],
            )
        )

    def test_malformed_rfc2047_encoded_word_is_rejected(self) -> None:
        headers = [
            ("Message-ID", GOLDEN_MESSAGE_ID),
            ("Subject", "=?utf-8?b?%%%%?="),
            ("From", "Sender <sender@example.test>"),
            ("To", "desk@example.test"),
            ("Date", "Thu, 01 Jan 2026 00:00:00 +0000"),
        ]
        self._assert_rejected(_eml(headers, ["Body."]))

    def test_unknown_part_charset_is_rejected(self) -> None:
        self._assert_rejected(
            _eml(_base_headers(), ["Body."], content_type="text/plain; charset=x-unknown")
        )

    def test_invalid_utf8_bytes_with_declared_charset_are_rejected(self) -> None:
        headers = [
            f"Message-ID: {GOLDEN_MESSAGE_ID}",
            "Subject: Kurzfrage zum Vorgang",
            "From: Sender <sender@example.test>",
            "To: desk@example.test",
            "Date: Thu, 01 Jan 2026 00:00:00 +0000",
            "Content-Type: text/plain; charset=utf-8",
        ]
        eml = "\r\n".join(headers).encode("ascii") + b"\r\n\r\n" + b"\xff\xfe broken\r\n"
        self._assert_rejected(eml, cli_text=_cli_text(_base_headers(), "broken"))

    def test_empty_body_is_rejected(self) -> None:
        self._assert_rejected(_eml(_base_headers(), []))

    def test_missing_header_body_separator_is_rejected(self) -> None:
        self._assert_rejected(_eml(_base_headers(), [], separator=False))

    def test_duplicate_content_type_is_rejected(self) -> None:
        eml = _eml(_base_headers(), ["Body."])
        text = eml.decode("utf-8")
        header_block, _separator, body = text.partition("\r\n\r\n")
        raw = header_block + "\r\nContent-Type: text/plain\r\n\r\n" + body
        self._assert_rejected(raw.encode("utf-8"))

    def test_duplicate_subject_header_is_rejected(self) -> None:
        self._assert_rejected(
            _eml(_base_headers() + [("Subject", "Second subject")], ["Body."])
        )

    def test_empty_content_type_value_is_rejected(self) -> None:
        self._assert_rejected(_eml(_base_headers(), ["Body."], content_type=""))

    def test_mime_version_other_than_1_0_is_rejected(self) -> None:
        self._assert_rejected(
            _eml(_base_headers() + [("MIME-Version", "2.0")], ["Body."])
        )

    def test_duplicate_charset_is_rejected(self) -> None:
        self._assert_rejected(
            _eml(
                _base_headers(),
                ["Body."],
                content_type="text/plain; charset=utf-8; charset=utf-8",
            )
        )

    def test_format_flowed_is_rejected(self) -> None:
        self._assert_rejected(
            _eml(
                _base_headers(),
                ["Body."],
                content_type="text/plain; charset=utf-8; format=flowed",
            )
        )

    def test_non_ascii_body_with_us_ascii_charset_is_rejected(self) -> None:
        self._assert_rejected(
            _eml(
                _base_headers(),
                ["Grüße."],
                content_type="text/plain; charset=us-ascii",
            )
        )

    def test_non_ascii_body_without_content_type_is_rejected(self) -> None:
        self._assert_rejected(
            _eml(_base_headers(), ["Grüße."], content_type=None)
        )

    def test_missing_body_separator_with_content_type_is_rejected(self) -> None:
        self._assert_rejected(
            _eml(_base_headers(), [], content_type="text/plain; charset=utf-8", separator=False)
        )

    def test_null_byte_is_rejected(self) -> None:
        eml = _eml(_base_headers(), ["Body."]).replace(b"Body.", b"Bo\x00dy.")
        self._assert_rejected(eml)

    def test_bare_carriage_return_is_rejected(self) -> None:
        eml = _eml(_base_headers(), ["Body."]).replace(b"Body.", b"Bo\rrdy.")
        self._assert_rejected(eml)

    def test_raw_utf8_subject_header_is_rejected(self) -> None:
        eml = _eml(_base_headers(), ["Body."]).replace(
            b"Subject: Kurzfrage zum Vorgang", "Subject: Grüße".encode("utf-8")
        )
        self._assert_rejected(eml)

    def test_raw_utf8_from_display_header_is_rejected(self) -> None:
        eml = _eml(_base_headers(), ["Body."]).replace(
            b"From: Sender <sender@example.test>",
            "From: Sänder <sender@example.test>".encode("utf-8"),
        )
        self._assert_rejected(eml)

    def test_raw_latin1_header_bytes_are_rejected(self) -> None:
        headers = [
            f"Message-ID: {GOLDEN_MESSAGE_ID}",
            "Subject: Kurzfrage zum Vorgang",
            "From: Sender <sender@example.test>",
            "To: desk@example.test",
            "Date: Thu, 01 Jan 2026 00:00:00 +0000",
        ]
        raw = "\r\n".join(headers).encode("ascii") + b"\r\n\r\nBody.\r\n"
        raw = raw.replace(b"Subject: Kurzfrage zum Vorgang", b"Subject: Gr\xfc\xdfe")
        self._assert_rejected(raw, cli_text=_cli_text(_base_headers(), "Body."))

    def test_named_text_part_is_never_the_body(self) -> None:
        self._assert_rejected(
            _eml(
                _base_headers(),
                ["Body."],
                content_type='text/plain; charset=utf-8; name="a.txt"',
            )
        )

    def test_html_body_with_a_named_text_part_is_rejected(self) -> None:
        eml = _multipart_eml(
            "mixed",
            [
                _part(["<p>html</p>"], "text/html; charset=utf-8"),
                _part(
                    ["Named."],
                    extra=['Content-Disposition: inline; filename="a.txt"'],
                ),
            ],
        )
        self._assert_rejected(eml, cli_text=_cli_text(_base_headers(), "<p>html</p>"))

    def test_truncated_q_encoding_is_rejected(self) -> None:
        headers = [
            ("Message-ID", GOLDEN_MESSAGE_ID),
            ("Subject", "=?utf-8?q?Bad=C?="),
            ("From", "Sender <sender@example.test>"),
            ("To", "desk@example.test"),
            ("Date", "Thu, 01 Jan 2026 00:00:00 +0000"),
        ]
        self._assert_rejected(_eml(headers, ["Body."]))

    def test_unknown_encoded_word_charset_is_rejected(self) -> None:
        headers = [
            ("Message-ID", GOLDEN_MESSAGE_ID),
            ("Subject", "=?x-unknown?b?QQ==?="),
            ("From", "Sender <sender@example.test>"),
            ("To", "desk@example.test"),
            ("Date", "Thu, 01 Jan 2026 00:00:00 +0000"),
        ]
        self._assert_rejected(_eml(headers, ["Body."]))

    def test_unicode_escape_charset_part_is_rejected(self) -> None:
        self._assert_rejected(
            _eml(
                _base_headers(),
                ["Body."],
                content_type="text/plain; charset=unicode_escape",
            )
        )

    def test_utf7_charset_part_is_rejected(self) -> None:
        self._assert_rejected(
            _eml(_base_headers(), ["Body."], content_type="text/plain; charset=utf-7")
        )

    def test_delsp_part_is_rejected(self) -> None:
        self._assert_rejected(
            _eml(
                _base_headers(),
                ["Body."],
                content_type="text/plain; charset=utf-8; delsp=yes",
            )
        )

    def test_date_encoded_word_is_rejected(self) -> None:
        headers = [
            ("Message-ID", GOLDEN_MESSAGE_ID),
            ("Subject", "Kurzfrage zum Vorgang"),
            ("From", "Sender <sender@example.test>"),
            ("To", "desk@example.test"),
            ("Date", "=?utf-8?b?QQ==?="),
        ]
        self._assert_rejected(
            _eml(headers, ["Body."]),
            cli_text=_cli_text(_base_headers(), "Body."),
        )

    def test_invalid_address_is_rejected(self) -> None:
        headers = [
            ("Message-ID", GOLDEN_MESSAGE_ID),
            ("Subject", "Kurzfrage zum Vorgang"),
            ("From", "Sender <not-an-address>"),
            ("To", "desk@example.test"),
            ("Date", "Thu, 01 Jan 2026 00:00:00 +0000"),
        ]
        self._assert_rejected(_eml(headers, ["Body."]))

    def test_nested_container_with_a_second_text_part_is_rejected(self) -> None:
        inner = _multipart_eml("alternative", [_part(["Inner."])])
        inner_body = inner.decode("utf-8").partition("\r\n\r\n")[2].rstrip("\r\n")
        eml = _multipart_eml(
            "mixed",
            [
                _part(["Outer."]),
                ["Content-Type: multipart/alternative; boundary=\"b1\"", "", inner_body],
            ],
        )
        self._assert_rejected(eml, cli_text=_cli_text(_base_headers(), "Outer."))

    def test_mixed_line_endings_in_the_header_block_are_rejected(self) -> None:
        headers = [
            f"Message-ID: {GOLDEN_MESSAGE_ID}",
            "Subject: Kurzfrage zum Vorgang",
            "From: Sender <sender@example.test>",
            "To: desk@example.test",
            "Date: Thu, 01 Jan 2026 00:00:00 +0000",
        ]
        header_block = "\r\n".join(headers[:2]) + "\n" + "\r\n".join(headers[2:])
        raw = (header_block + "\r\n\r\nBody.\r\n").encode("utf-8")
        self._assert_rejected(raw, cli_text=_cli_text(_base_headers(), "Body."))

    def test_mixed_with_an_inventory_attachment_is_rejected(self) -> None:
        eml = _multipart_eml(
            "mixed",
            [
                _part(["Body."]),
                [
                    "Content-Type: application/pdf",
                    "Content-Transfer-Encoding: base64",
                    'Content-Disposition: attachment; filename="a.pdf"',
                    "",
                    base64.b64encode(b"%PDF-1.4").decode("ascii"),
                ],
            ],
        )
        self._assert_rejected(eml, cli_text=_cli_text(_base_headers(), "Body."))

    def test_related_with_an_inventory_attachment_is_rejected(self) -> None:
        eml = _multipart_eml(
            "related",
            [
                _part(["Body."]),
                [
                    "Content-Type: image/png",
                    "Content-Transfer-Encoding: base64",
                    'Content-Disposition: inline; filename="p.png"',
                    "",
                    base64.b64encode(b"\x89PNG").decode("ascii"),
                ],
            ],
        )
        self._assert_rejected(eml, cli_text=_cli_text(_base_headers(), "Body."))

    def test_inline_named_text_with_an_attachment_inventory_is_rejected(self) -> None:
        eml = _multipart_eml(
            "mixed",
            [
                _part(["Body."]),
                _part(
                    ["Named."],
                    extra=['Content-Disposition: inline; filename="a.txt"'],
                ),
            ],
        )
        self._assert_rejected(eml, cli_text=_cli_text(_base_headers(), "Body."))

    def test_date_without_a_weekday_is_rejected(self) -> None:
        headers = [
            ("Message-ID", GOLDEN_MESSAGE_ID),
            ("Subject", "Kurzfrage zum Vorgang"),
            ("From", "Sender <sender@example.test>"),
            ("To", "desk@example.test"),
            ("Date", "01 Jan 2026 00:00:00 +0000"),
        ]
        self._assert_rejected(
            _eml(headers, ["Body."]),
            cli_text=_cli_text(_base_headers(), "Body."),
        )

    def test_address_group_syntax_is_rejected(self) -> None:
        headers = [
            ("Message-ID", GOLDEN_MESSAGE_ID),
            ("Subject", "Kurzfrage zum Vorgang"),
            ("From", "Sender <sender@example.test>"),
            ("To", "Team: a@example.test;"),
            ("Date", "Thu, 01 Jan 2026 00:00:00 +0000"),
        ]
        self._assert_rejected(
            _eml(headers, ["Body."]), cli_text=_cli_text(_base_headers(), "Body.")
        )

    def test_address_comment_syntax_is_rejected(self) -> None:
        headers = [
            ("Message-ID", GOLDEN_MESSAGE_ID),
            ("Subject", "Kurzfrage zum Vorgang"),
            ("From", "Sender <sender@example.test>"),
            ("To", "a@example.test (comment)"),
            ("Date", "Thu, 01 Jan 2026 00:00:00 +0000"),
        ]
        self._assert_rejected(
            _eml(headers, ["Body."]), cli_text=_cli_text(_base_headers(), "Body.")
        )

    def test_empty_address_element_is_rejected(self) -> None:
        for value in (",a@example.test", "a@example.test,,b@example.test", "a@example.test,"):
            with self.subTest(value=value):
                headers = [
                    ("Message-ID", GOLDEN_MESSAGE_ID),
                    ("Subject", "Kurzfrage zum Vorgang"),
                    ("From", "Sender <sender@example.test>"),
                    ("To", value),
                    ("Date", "Thu, 01 Jan 2026 00:00:00 +0000"),
                ]
                self._assert_rejected(
                    _eml(headers, ["Body."]),
                    cli_text=_cli_text(_base_headers(), "Body."),
                )

    def test_duplicate_address_is_rejected(self) -> None:
        headers = [
            ("Message-ID", GOLDEN_MESSAGE_ID),
            ("Subject", "Kurzfrage zum Vorgang"),
            ("From", "Sender <sender@example.test>"),
            ("To", "a@example.test, a@example.test"),
            ("Date", "Thu, 01 Jan 2026 00:00:00 +0000"),
        ]
        self._assert_rejected(
            _eml(headers, ["Body."]), cli_text=_cli_text(_base_headers(), "Body.")
        )

    def test_case_insensitive_duplicate_address_is_rejected(self) -> None:
        headers = [
            ("Message-ID", GOLDEN_MESSAGE_ID),
            ("Subject", "Kurzfrage zum Vorgang"),
            ("From", "Sender <sender@example.test>"),
            ("To", "A@example.test, a@example.test"),
            ("Date", "Thu, 01 Jan 2026 00:00:00 +0000"),
        ]
        self._assert_rejected(
            _eml(headers, ["Body."]), cli_text=_cli_text(_base_headers(), "Body.")
        )

    def test_quoted_local_part_is_rejected(self) -> None:
        headers = [
            ("Message-ID", GOLDEN_MESSAGE_ID),
            ("Subject", "Kurzfrage zum Vorgang"),
            ("From", "Sender <sender@example.test>"),
            ("To", '"a,b"@example.test'),
            ("Date", "Thu, 01 Jan 2026 00:00:00 +0000"),
        ]
        self._assert_rejected(
            _eml(headers, ["Body."]), cli_text=_cli_text(_base_headers(), "Body.")
        )

    def test_comma_inside_the_angle_block_is_rejected(self) -> None:
        headers = [
            ("Message-ID", GOLDEN_MESSAGE_ID),
            ("Subject", "Kurzfrage zum Vorgang"),
            ("From", "Sender <sender@example.test>"),
            ("To", "Name <a@example.test,b@example.test>"),
            ("Date", "Thu, 01 Jan 2026 00:00:00 +0000"),
        ]
        self._assert_rejected(
            _eml(headers, ["Body."]), cli_text=_cli_text(_base_headers(), "Body.")
        )

    def test_date_forms_outside_the_reference_are_rejected(self) -> None:
        for value in (
            "Foo, 01 Jan 2026 00:00:00 +0000",
            "Thu, 1 Jan 2026 00:00:00 +0000",
            "Thu, 01 Jan 2026 00:00:00 GMT",
            "Fri, 01 Jan 2026 00:00:00 +0000",
            "Thu, 31 Feb 2026 00:00:00 +0000",
            "Thu, 01 Jan 2026 24:00:00 +0000",
            "Thu, 01 Jan 2026 00:00:00 +2400",
        ):
            with self.subTest(value=value):
                headers = [
                    ("Message-ID", GOLDEN_MESSAGE_ID),
                    ("Subject", "Kurzfrage zum Vorgang"),
                    ("From", "Sender <sender@example.test>"),
                    ("To", "desk@example.test"),
                    ("Date", value),
                ]
                self._assert_rejected(
                    _eml(headers, ["Body."]),
                    cli_text=_cli_text(_base_headers(), "Body."),
                )

    def test_adjacent_references_brackets_are_rejected(self) -> None:
        headers = _base_headers() + [
            ("References", "<a@example.test><b@example.test>"),
        ]
        self._assert_rejected(
            _eml(headers, ["Body."]), cli_text=_cli_text(_base_headers(), "Body.")
        )

    def test_unbalanced_references_brackets_are_rejected(self) -> None:
        for value in ("<a@example.test", "a@example.test>"):
            with self.subTest(value=value):
                headers = _base_headers() + [("References", value)]
                self._assert_rejected(
                    _eml(headers, ["Body."]),
                    cli_text=_cli_text(_base_headers(), "Body."),
                )

    def test_comma_separated_references_are_rejected(self) -> None:
        headers = _base_headers() + [
            ("References", "<a@example.test>, <b@example.test>"),
        ]
        self._assert_rejected(
            _eml(headers, ["Body."]), cli_text=_cli_text(_base_headers(), "Body.")
        )


class EmlControlPathTests(HermeticProcessTestCase):
    """Points 3-5: helper default, has_attachment=False and the full path."""

    def setUp(self) -> None:
        super().setUp()
        himalaya.reset_transport_stats()

    def test_helper_default_keeps_export_and_read(self) -> None:
        eml = _eml(_base_headers(), ["Body."])
        details, calls, _ = _details(eml, allow=False)
        self.assertEqual(["export", "read"], calls)
        self.assertEqual("Body.\n", details["preview"])

    def test_has_attachment_false_reads_once_without_export(self) -> None:
        eml = _eml(_base_headers(), ["Body."])
        details, calls, snapshot = _details(
            eml,
            allow=True,
            fallback={"has_attachment": False, "subject": "fallback"},
        )
        self.assertEqual(["read"], calls)
        self.assertEqual(1, snapshot["starts_by_purpose"]["scan_preview"])
        self.assertEqual(0, snapshot["starts_by_purpose"]["scan_inventory"])
        self.assertEqual("Body.\n", details["preview"])

    def test_full_body_with_the_flag_still_exports_and_reads(self) -> None:
        eml = _eml(_base_headers(), ["Body."])
        details, calls, snapshot = _details(
            eml, allow=True, full_body=True, purpose="full_read"
        )
        self.assertEqual(["export", "read"], calls)
        self.assertEqual("Body.\n", details["preview"])
        self.assertEqual(1, snapshot["starts_by_purpose"]["full_inventory"])
        self.assertEqual(1, snapshot["starts_by_purpose"]["full_read"])

    def test_non_boolean_flag_is_rejected_before_any_process(self) -> None:
        with patch.object(himalaya, "_POPEN") as popen:
            with self.assertRaises(ValueError):
                himalaya.get_single_email_details("61", "INBOX", allow_eml_details="yes")
        popen.assert_not_called()


class DraftEmlOptInTests(HermeticProcessTestCase):
    """Point 3: the real draft entry switches the read sequence, nothing else."""

    def setUp(self) -> None:
        super().setUp()
        himalaya.reset_transport_stats()

    def _run_draft(self, temporary: str, *, allow: bool | None, known: bool = False):
        eml = _eml(_base_headers(), ["Erste Zeile.", "Zweite Zeile."])
        calls: list[str] = []
        data_dir = Path(temporary) / "data" / "mail-desk"
        data_dir.mkdir(parents=True)
        envelope = {
            "id": "61",
            "subject": "Kurzfrage zum Vorgang",
            "from": {"name": "Sender", "addr": "sender@example.test"},
            "date": "2026-01-01",
        }
        collected_items = {"items": []}

        def fake_scan(**kwargs):
            collected, known_count, _skipped = runner._scan_unprocessed_emails(**kwargs)
            collected_items["items"] = collected
            return collected, known_count

        def fake_draft(emails_arg, *, source_sink=None, **_kwargs):
            for candidate in emails_arg:
                source_sink(candidate)
            collected_items["items"] = [
                {
                    "envelope_id": str(email.get("envelope_id")),
                    "folder": email.get("folder", "INBOX"),
                    "message_id": email.get("message_id", ""),
                    "decision": {
                        "kind": "topic",
                        "id": "general",
                        "confidence": "high",
                        "needs_reply": False,
                    },
                    "action": {"type": "keep_in_folder", "target_folder": "INBOX"},
                }
                for email in emails_arg
            ]
            return {"items": collected_items["items"]}

        dispatch = {
            "thread": {"stdout": "[]"},
            "list": {"stdout": json.dumps([envelope])},
            "export": {"stdout": ""},
            "read": {"stdout": _cli_read_text(eml)},
        }

        def fake_popen(cmd, **_kwargs):
            if "export" in cmd:
                Path(cmd[cmd.index("-d") + 1]).write_bytes(eml)
                calls.append("export")
                return _FakeProcess(cmd, stdout="")
            for token, payload in dispatch.items():
                if token in cmd:
                    calls.append(token)
                    return _FakeProcess(cmd, stdout=payload["stdout"])
            raise AssertionError(f"unexpected command: {cmd}")

        index_patch = (
            patch.object(
                runner, "load_final_index", return_value={"items": {f"golden-61@example.test": {}}}
            )
            if known
            else patch.object(runner, "load_final_index", return_value={"items": {}})
        )
        with patch.object(
            himalaya,
            "build_himalaya_command",
            side_effect=lambda command, account=None: ["himalaya", *command],
        ), patch.object(
            himalaya, "_POPEN", side_effect=fake_popen
        ), patch.object(
            himalaya.time, "sleep"
        ), patch.object(
            runner, "build_signature_index", return_value={}
        ), index_patch, himalaya.transport_stats_session() as stats:
            config = {"count": 1, "skip_known": True}
            if allow is not None:
                config["allow_eml_details"] = allow
            result = draft_mode.run_draft_mode(
                config,
                account="primary",
                data_dir=data_dir,
                dependencies={
                    "BatchProgressTracker": _NullTracker,
                    "atomic_write_json": runner.atomic_write_json,
                    "draft_manifest": fake_draft,
                    "get_unprocessed_emails": fake_scan,
                    "load_sent_index": Mock(return_value={}),
                    "attachment_evaluate": Mock(),
                    "decision_triggers_evaluation": lambda *_a, **_k: False,
                },
            )
        return result, calls, stats.snapshot(), collected_items["items"]

    def test_qualified_candidate_skips_the_preview_read_only_with_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            on_result, on_calls, on_stats, on_items = self._run_draft(temporary, allow=True)
        with tempfile.TemporaryDirectory() as temporary:
            off_result, off_calls, off_stats, off_items = self._run_draft(temporary, allow=False)

        self.assertEqual(["thread", "list", "export"], on_calls)
        self.assertEqual(["thread", "list", "export", "read"], off_calls)
        self.assertEqual(on_items, off_items)
        self.assertEqual(on_result["draft"]["items"], off_result["draft"]["items"])
        self.assertEqual(
            on_result["draft"]["review"]["execute_request_sha256"],
            off_result["draft"]["review"]["execute_request_sha256"],
        )
        self.assertEqual(on_result["injection_barrier"], off_result["injection_barrier"])
        self.assertEqual(1, on_stats["starts_by_purpose"]["scan_inventory"])
        self.assertEqual(0, on_stats["starts_by_purpose"]["scan_preview"])
        self.assertEqual(1, off_stats["starts_by_purpose"]["scan_inventory"])
        self.assertEqual(1, off_stats["starts_by_purpose"]["scan_preview"])
        self.assertEqual(1, len(on_items))

    def test_draft_default_is_off_and_keeps_export_and_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _result, calls, stats, items = self._run_draft(temporary, allow=None)

        self.assertEqual(["thread", "list", "export", "read"], calls)
        self.assertEqual(1, len(items))
        self.assertEqual(1, stats["starts_by_purpose"]["scan_inventory"])
        self.assertEqual(1, stats["starts_by_purpose"]["scan_preview"])

    def test_known_skip_decision_is_identical_with_and_without_opt_in(self) -> None:
        results = []
        for allow in (True, False):
            with tempfile.TemporaryDirectory() as temporary:
                eml = _eml(_base_headers(), ["Body."])
                calls: list[str] = []
                envelope = {
                    "id": "61",
                    "subject": "Kurzfrage zum Vorgang",
                    "from": {"name": "Sender", "addr": "sender@example.test"},
                    "date": "2026-01-01",
                }

                def fake_popen(cmd, **_kwargs):
                    if "export" in cmd:
                        Path(cmd[cmd.index("-d") + 1]).write_bytes(eml)
                        calls.append("export")
                        return _FakeProcess(cmd, stdout="")
                    if "thread" in cmd:
                        calls.append("thread")
                        return _FakeProcess(cmd, stdout="[]")
                    if "list" in cmd:
                        calls.append("list")
                        return _FakeProcess(cmd, stdout=json.dumps([envelope]))
                    if "read" in cmd:
                        calls.append("read")
                        return _FakeProcess(cmd, stdout=_cli_read_text(eml))
                    raise AssertionError(f"unexpected command: {cmd}")

                with patch.object(
                    himalaya,
                    "build_himalaya_command",
                    side_effect=lambda command, account=None: ["himalaya", *command],
                ), patch.object(
                    himalaya, "_POPEN", side_effect=fake_popen
                ), patch.object(
                    himalaya.time, "sleep"
                ), patch.object(
                    runner, "build_signature_index", return_value={}
                ), patch.object(
                    runner,
                    "load_final_index",
                    return_value={"items": {"golden-61@example.test": {}}},
                ), himalaya.transport_stats_session():
                    results.append(
                        (
                            runner._scan_unprocessed_emails(
                                "INBOX",
                                1,
                                account=None,
                                data_dir=Path(temporary) / "data" / "mail-desk",
                                skip_known=True,
                                preview_lines=30,
                                allow_eml_details=allow,
                            ),
                            list(calls),
                        )
                    )
        self.assertEqual(results[0][0], results[1][0])
        self.assertEqual([], results[0][0][0])
        self.assertEqual(1, results[0][0][1])
        self.assertEqual(1, results[0][0][2])
        self.assertEqual(["thread", "list", "export"], results[0][1])
        self.assertEqual(["thread", "list", "export", "read"], results[1][1])


class EmlPredicateTests(HermeticProcessTestCase):
    """F8: predicate results proven without the export/read seam."""

    def _predicate(self, eml: bytes):
        return himalaya._details_from_eml(
            "61",
            "INBOX",
            eml,
            preview_lines=30,
            fallback_envelope=None,
            attachments=[],
            attachment_status="none",
            attachment_error=None,
        )

    def test_base64_body_is_not_none(self) -> None:
        payload = base64.b64encode(b"Body.").decode("ascii")
        self.assertIsNotNone(self._predicate(_eml(_base_headers(), [payload], cte="base64")))

    def test_quoted_printable_body_is_not_none(self) -> None:
        self.assertIsNotNone(
            self._predicate(_eml(_base_headers(), ["Body."], cte="quoted-printable"))
        )

    def test_duplicate_content_type_is_none(self) -> None:
        eml = _eml(_base_headers(), ["Body."])
        text = eml.decode("utf-8")
        header_block, _separator, body = text.partition("\r\n\r\n")
        raw = header_block + "\r\nContent-Type: text/plain\r\n\r\n" + body
        self.assertIsNone(self._predicate(raw.encode("utf-8")))

    def test_empty_content_type_is_none(self) -> None:
        self.assertIsNone(
            self._predicate(_eml(_base_headers(), ["Body."], content_type=""))
        )

    def test_attachment_inventory_is_excluded_without_the_seam(self) -> None:
        self.assertIsNone(
            himalaya._details_from_eml(
                "61",
                "INBOX",
                _eml(_base_headers(), ["Body."]),
                preview_lines=30,
                fallback_envelope=None,
                attachments=[{"filename": "a.pdf", "size_bytes": 8}],
                attachment_status="available",
                attachment_error=None,
            )
        )

    def test_date_without_weekday_is_none_without_the_seam(self) -> None:
        headers = [
            ("Message-ID", GOLDEN_MESSAGE_ID),
            ("Subject", "Kurzfrage zum Vorgang"),
            ("From", "Sender <sender@example.test>"),
            ("To", "desk@example.test"),
            ("Date", "01 Jan 2026 00:00:00 +0000"),
        ]
        self.assertIsNone(self._predicate(_eml(headers, ["Body."])))

    def test_address_guard_rejects_group_comment_empty_and_duplicate(self) -> None:
        for value in (
            "Team: a@example.test;",
            "a@example.test (comment)",
            ",a@example.test",
            "a@example.test,,b@example.test",
            "a@example.test,",
            "a@example.test, a@example.test",
        ):
            with self.subTest(value=value):
                self.assertIsNone(himalaya._canonical_address_header(value))
        self.assertEqual(
            "Doe, John <john@example.test>",
            himalaya._canonical_address_header('"Doe, John" <john@example.test>'),
        )

    def test_address_guard_rejects_a_case_insensitive_duplicate(self) -> None:
        for value in (
            "A@example.test, a@example.test",
            "a@example.test, A@EXAMPLE.TEST",
        ):
            with self.subTest(value=value):
                self.assertIsNone(himalaya._canonical_address_header(value))

    def test_address_guard_rejects_a_quoted_local_part(self) -> None:
        for value in ('"a,b"@example.test', '"a"@example.test'):
            with self.subTest(value=value):
                self.assertIsNone(himalaya._canonical_address_header(value))

    def test_address_guard_rejects_a_comma_inside_the_angle_block(self) -> None:
        self.assertIsNone(
            himalaya._canonical_address_header(
                "Name <a@example.test,b@example.test>"
            )
        )

    def test_references_guard_rejects_adjacent_unbalanced_and_commas(self) -> None:
        for value in (
            "<a@example.test><b@example.test>",
            "<a@example.test",
            "a@example.test>",
            "<a@example.test>, <b@example.test>",
        ):
            with self.subTest(value=value):
                self.assertIsNone(himalaya._canonical_references_header(value))
        self.assertEqual(
            "a@example.test b@example.test",
            himalaya._canonical_references_header(
                "<a@example.test> <b@example.test>"
            ),
        )

    def test_date_guard_requires_the_anchored_reference_form(self) -> None:
        self.assertTrue(
            himalaya._is_p1_reference_date("Thu, 01 Jan 2026 00:00:00 +0000")
        )
        for value in (
            "Foo, 01 Jan 2026 00:00:00 +0000",
            "Thu, 1 Jan 2026 00:00:00 +0000",
            "Thu, 01 Jan 2026 00:00:00 GMT",
            "Fri, 01 Jan 2026 00:00:00 +0000",
            "Thu, 31 Feb 2026 00:00:00 +0000",
        ):
            with self.subTest(value=value):
                self.assertFalse(himalaya._is_p1_reference_date(value))

    def test_inventory_unavailable_is_none(self) -> None:
        self.assertIsNone(
            himalaya._details_from_eml(
                "61",
                "INBOX",
                _eml(_base_headers(), ["Body."]),
                preview_lines=30,
                fallback_envelope=None,
                attachments=[],
                attachment_status="attachment_inventory_unavailable",
                attachment_error="x",
            )
        )

    def test_raw_utf8_subject_is_none_without_the_seam(self) -> None:
        eml = _eml(_base_headers(), ["Body."]).replace(
            b"Subject: Kurzfrage zum Vorgang", "Subject: Grüße".encode("utf-8")
        )
        self.assertIsNone(self._predicate(eml))

    def test_raw_latin1_header_is_none_without_the_seam(self) -> None:
        eml = _eml(_base_headers(), ["Body."]).replace(
            b"Subject: Kurzfrage zum Vorgang", b"Subject: Gr\xfc\xdfe"
        )
        self.assertIsNone(self._predicate(eml))

    def test_mixed_line_endings_are_none_without_the_seam(self) -> None:
        headers = [
            f"Message-ID: {GOLDEN_MESSAGE_ID}",
            "Subject: Kurzfrage zum Vorgang",
            "From: Sender <sender@example.test>",
            "To: desk@example.test",
            "Date: Thu, 01 Jan 2026 00:00:00 +0000",
        ]
        header_block = "\r\n".join(headers[:2]) + "\n" + "\r\n".join(headers[2:])
        eml = (header_block + "\r\n\r\nBody.\r\n").encode("utf-8")
        self.assertIsNone(self._predicate(eml))

    def test_truncated_q_encoding_is_none_without_the_seam(self) -> None:
        headers = [
            ("Message-ID", GOLDEN_MESSAGE_ID),
            ("Subject", "=?utf-8?q?Bad=C?="),
            ("From", "Sender <sender@example.test>"),
            ("To", "desk@example.test"),
            ("Date", "Thu, 01 Jan 2026 00:00:00 +0000"),
        ]
        self.assertIsNone(self._predicate(_eml(headers, ["Body."])))

    def test_named_single_part_is_none_without_the_seam(self) -> None:
        eml = _eml(
            _base_headers(),
            ["Body."],
            content_type='text/plain; charset=utf-8; name="a.txt"',
        )
        self.assertIsNone(self._predicate(eml))

    def test_date_encoded_word_is_none_without_the_seam(self) -> None:
        headers = [
            ("Message-ID", GOLDEN_MESSAGE_ID),
            ("Subject", "Kurzfrage zum Vorgang"),
            ("From", "Sender <sender@example.test>"),
            ("To", "desk@example.test"),
            ("Date", "=?utf-8?b?QQ==?="),
        ]
        self.assertIsNone(self._predicate(_eml(headers, ["Body."])))

    def test_in_reply_to_encoded_word_is_none_without_the_seam(self) -> None:
        headers = _base_headers() + [("In-Reply-To", "=?utf-8?b?QQ==?=")]
        self.assertIsNone(self._predicate(_eml(headers, ["Body."])))


class EmlInventoryTests(HermeticProcessTestCase):
    """Point 7: a defective inventory stays unavailable, never 'no attachments'."""

    def test_defective_inventory_falls_back_and_keeps_unavailable(self) -> None:
        eml = _eml(_base_headers(), ["Body."], content_type='multipart/mixed; boundary="b1"')
        details, calls, _ = _details(eml, allow=True)
        self.assertEqual(["export", "read"], calls)
        self.assertEqual("attachment_inventory_unavailable", details["attachment_status"])
        self.assertNotEqual("none", details["attachment_status"])


class HermeticGuardSelfTest(HermeticProcessTestCase):
    """The incident path fails at the guard, never at a real child process."""

    def test_missing_session_fails_closed_instead_of_real_subprocess(self) -> None:
        self.assertIsNone(himalaya.current_transport_stats())
        with patch.object(
            himalaya, "build_himalaya_command", return_value=["himalaya"]
        ):
            with self.assertRaisesRegex(
                AssertionError, "real subprocess.run is forbidden"
            ):
                himalaya.run_himalaya(["envelope", "list"], max_retries=1)


if __name__ == "__main__":
    unittest.main()
