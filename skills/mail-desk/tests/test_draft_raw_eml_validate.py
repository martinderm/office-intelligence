"""Raw-EML import (#18) — strict, offline validation and CRLF normalization of one prepared message.

Evidence mode ``tdd``.  Matrix rows R1-R9 (profile, header/Message-ID validation, structure, limits).

Seam note: ``core.draft_raw_eml.validate_raw_eml`` is the public pure function (bytes in, prepared
message or a content-free ``RawEmlError`` out).  It touches no file system, no mailbox and no network.
Expected values (limits, reason codes, header names, hashes) are literals or derived independently with
the standard library; the malformed inputs are hand-written byte strings.
"""

from __future__ import annotations

import base64
import hashlib
import sys
import traceback
import unittest
from pathlib import Path

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import draft_attachments as da  # noqa: E402
from core import draft_raw_eml as dre  # noqa: E402
from core import drafts  # noqa: E402
from raw_eml_fixtures import (  # noqa: E402
    BODY_PART,
    DEFAULT_DATE,
    attachment_part,
    headers,
    join_lines,
    make_eml,
    mixed,
    plain,
)

SECRET = "SECRET-RAW-EML-MARKER-0451"
TEN_MIB = 10 * 1048576
THREE_MIB = 3 * 1048576
BINARY = bytes(range(256)) * 20


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class _Base(unittest.TestCase):
    def assertRefused(self, reason: str, data: bytes) -> dre.RawEmlError:  # noqa: N802
        with self.assertRaises(dre.RawEmlError) as raised:
            dre.validate_raw_eml(data)
        exc = raised.exception
        self.assertEqual(reason, exc.reason_code, str(exc))
        self.assertIsInstance(exc, drafts.DraftError)
        trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        self.assertNotIn(SECRET, trace)
        return exc


class ProfileAndNormalizationTests(_Base):
    def test_limits_are_the_confirmed_literals(self) -> None:  # R1
        self.assertEqual(TEN_MIB, dre.MAX_RAW_EML_BYTES)
        self.assertEqual(10 * 1048576, da.MAX_MIME_MESSAGE_BYTES)
        self.assertEqual(5, da.MAX_ATTACHMENTS)
        self.assertEqual(3 * 1048576, da.MAX_ATTACHMENT_BYTES)
        self.assertEqual(998, dre.MAX_LINE_BYTES)

    def test_a_crlf_message_is_unchanged_and_its_fields_are_extracted(self) -> None:  # R1
        data = make_eml()
        prepared = dre.validate_raw_eml(data)
        self.assertEqual(data, prepared.data)
        self.assertEqual(data.decode("utf-8"), prepared.text)
        self.assertEqual("crlf", prepared.line_endings)
        self.assertEqual("raw1@example.org", prepared.message_id)
        self.assertEqual("Angebot – Prüfung", prepared.subject)
        self.assertEqual("Empfänger <a@example.org>", prepared.to)
        self.assertEqual(DEFAULT_DATE, prepared.date)
        self.assertEqual((), prepared.attachments)
        self.assertEqual(hashlib.sha256(data).hexdigest(), prepared.source_sha256)
        self.assertEqual(prepared.source_sha256, prepared.normalized_sha256)
        self.assertEqual(len(data), prepared.size_bytes)

    def test_an_lf_message_is_normalized_to_crlf_for_the_himalaya_transport(self) -> None:  # R2
        crlf = make_eml()
        lf = make_eml(linesep="\n")
        self.assertNotEqual(crlf, lf)
        prepared = dre.validate_raw_eml(lf)
        self.assertEqual(crlf, prepared.data)
        self.assertEqual("lf", prepared.line_endings)
        self.assertEqual(hashlib.sha256(lf).hexdigest(), prepared.source_sha256)
        self.assertEqual(hashlib.sha256(crlf).hexdigest(), prepared.normalized_sha256)

    def test_mixed_line_endings_are_normalized_and_reported(self) -> None:  # R2
        data = b"To: a@example.org\r\nDate: " + DEFAULT_DATE.encode() + b"\nMessage-ID: <m@example.org>\r\n\r\nText\n"
        prepared = dre.validate_raw_eml(data)
        self.assertEqual("mixed", prepared.line_endings)
        self.assertNotIn(b"\n", prepared.data.replace(b"\r\n", b""))

    def test_a_missing_final_line_break_is_added_exactly_once(self) -> None:  # R2
        data = join_lines([*headers(), "", "kein Zeilenende"], final=False)
        prepared = dre.validate_raw_eml(data)
        self.assertEqual(data + b"\r\n", prepared.data)

    def test_trailing_blank_lines_are_preserved(self) -> None:  # R2
        data = join_lines([*headers(), "", "Text", "", ""])
        self.assertEqual(data, dre.validate_raw_eml(data).data)

    def test_attachments_are_decoded_and_described(self) -> None:  # R3
        data = make_eml(attachments=[("report.pdf", "application/pdf", BINARY), ("Notiz Ärger.txt", "text/plain", b"abc\r\n\x00\xff")])
        prepared = dre.validate_raw_eml(data)
        self.assertEqual(2, len(prepared.attachments))
        first, second = prepared.attachments
        self.assertEqual(("report.pdf", "application/pdf", BINARY), (first.filename, first.content_type, first.data))
        self.assertEqual(hashlib.sha256(BINARY).hexdigest(), first.sha256)
        self.assertEqual(len(BINARY), first.size)
        self.assertEqual(("Notiz Ärger.txt", "text/plain", b"abc\r\n\x00\xff"), (second.filename, second.content_type, second.data))
        self.assertNotIn(repr(BINARY[:20]), repr(first), "the bytes never appear in repr")

    def test_folded_headers_are_unfolded_and_non_ascii_headers_are_accepted(self) -> None:  # R4
        data = join_lines(
            [
                "To: a@example.org,", " b@example.org",
                "Subject: Ein sehr langer", "\tBetreff äöü",
                f"Date: {DEFAULT_DATE}",
                "Message-ID: <fold@example.org>",
                "MIME-Version: 1.0",
                "Content-Type: text/plain; charset=utf-8",
                "Content-Transfer-Encoding: 8bit",
                "", "Grüße",
            ]
        )
        prepared = dre.validate_raw_eml(data)
        self.assertEqual("a@example.org, b@example.org", prepared.to)
        self.assertEqual("Ein sehr langer Betreff äöü", prepared.subject)

    def test_body_transfer_encodings_are_decoded_for_the_summary(self) -> None:  # R4
        qp = join_lines(
            [*headers(), "MIME-Version: 1.0", "Content-Type: text/plain; charset=utf-8",
             "Content-Transfer-Encoding: quoted-printable", "", "Gr=C3=BC=C3=9Fe"]
        )
        base = join_lines(
            [*headers(), "MIME-Version: 1.0", "Content-Type: text/plain; charset=utf-8",
             "Content-Transfer-Encoding: base64", "", b64("Grüße\n".encode())]
        )
        self.assertEqual(dre.validate_raw_eml(qp).summary["body"], dre.validate_raw_eml(base).summary["body"])
        self.assertEqual("Grüße\n", dre.validate_raw_eml(qp).summary["body"])

    def test_an_empty_body_is_allowed(self) -> None:  # R4
        self.assertEqual("", dre.validate_raw_eml(join_lines([*headers(), ""])).summary["body"])

    def test_exactly_ten_mib_is_accepted_and_one_byte_more_is_not(self) -> None:  # R5
        head = join_lines([*headers(), ""])  # header block + separator line
        remaining = TEN_MIB - len(head)
        lines, rest = divmod(remaining, 72)  # 72 = 70 characters + CRLF
        if rest == 1:  # one byte cannot be a line: merge it into a 71-character line
            lines, rest = lines - 1, rest + 72
        body = "a" * 70 + "\r\n"
        data = head + body.encode() * lines + (b"b" * (rest - 2) + b"\r\n" if rest else b"")
        self.assertEqual(TEN_MIB, len(data))
        self.assertEqual(TEN_MIB, dre.validate_raw_eml(data).size_bytes)
        self.assertRefused("raw_eml_too_large", data + b"x")

    def test_an_lf_message_that_grows_past_ten_mib_when_normalized_is_refused(self) -> None:  # R5
        base = len(join_lines([*headers(), "", ""], linesep="\n"))
        lines = (TEN_MIB - base) // 2  # one-byte lines: every LF becomes CRLF
        data = join_lines([*headers(), ""], linesep="\n") + b"a\n" * lines
        self.assertLessEqual(len(data), TEN_MIB)
        self.assertRefused("raw_eml_message_too_large", data)


class ByteLevelRefusalTests(_Base):
    def test_invalid_utf8_is_refused_because_the_transport_requires_utf8(self) -> None:  # R6
        data = plain(body=["x"]) + b"\xff\xfe\r\n"
        self.assertRefused("raw_eml_not_utf8", data)

    def test_a_nul_byte_is_refused(self) -> None:  # R6
        self.assertRefused("raw_eml_nul_byte", plain(body=["a\x00b"]))

    def test_a_bare_carriage_return_is_refused(self) -> None:  # R6
        self.assertRefused("raw_eml_bare_cr", plain(body=["a\rb"]))

    def test_an_empty_input_is_refused(self) -> None:  # R6
        self.assertRefused("raw_eml_empty", b"")

    def test_a_non_bytes_input_is_refused(self) -> None:  # R6
        with self.assertRaises(dre.RawEmlError):
            dre.validate_raw_eml("To: a@example.org\r\n\r\n")  # type: ignore[arg-type]

    def test_a_line_longer_than_998_octets_is_refused(self) -> None:  # R6
        self.assertRefused("raw_eml_line_too_long", plain(body=["a" * 999]))
        self.assertEqual("a" * 998 + "\n", dre.validate_raw_eml(plain(body=["a" * 998])).summary["body"])

    def test_line_length_counts_octets_not_characters(self) -> None:  # R6
        body = ["ä" * 500]  # 500 characters, 1000 octets
        data = join_lines([*headers(), "MIME-Version: 1.0", "Content-Type: text/plain; charset=utf-8",
                           "Content-Transfer-Encoding: 8bit", "", *body])
        self.assertRefused("raw_eml_line_too_long", data)


class HeaderSyntaxTests(_Base):
    def test_malformed_header_blocks_are_refused(self) -> None:  # R7
        cases = {
            "space before colon": join_lines([*headers(), "X-Bad : v", "", "t"]),
            "no colon": join_lines([*headers(), "garbage line", "", "t"]),
            "empty field name": join_lines([*headers(), ": value", "", "t"]),
            "mbox from line": join_lines(["From a@example.org Sat Nov  7 12:00:00 2026", *headers(), "", "t"]),
            "first line is a continuation": join_lines([" To: a@example.org", *headers(), "", "t"]),
            "blank continuation": join_lines([*headers(), "X-Folded: a", " ", "", "t"]),
            "non-ascii field name": join_lines([*headers(), "Bétreff: v", "", "t"]),
            "control character": join_lines([*headers(), "X-Ctl: a\x0bb", "", "t"]),
            "line separator": join_lines([*headers(), "X-Sep: a b", "", "t"]),
            "no header/body separator": join_lines(headers()),
        }
        for label, data in cases.items():
            with self.subTest(label):
                self.assertRefused("raw_eml_header_invalid", data)

    def test_the_header_block_is_bounded(self) -> None:  # R7
        many = [f"X-H{index}: v" for index in range(dre.MAX_HEADER_FIELDS + 1)]
        self.assertRefused("raw_eml_header_invalid", join_lines([*headers(extra=many), "", "t"]))
        fields_ok = [f"X-H{index}: v" for index in range(dre.MAX_HEADER_FIELDS - 4)]
        dre.validate_raw_eml(join_lines([*headers(extra=fields_ok), "", "t"]))
        big = ["X-Big: " + "x" * 900] + [" " + "y" * 900] * 80
        self.assertRefused("raw_eml_header_invalid", join_lines([*headers(extra=big), "", "t"]))

    def test_every_header_including_bcc_is_preserved_and_visible_in_the_summary(self) -> None:  # R7
        data = plain(extra_headers=["X-Unsent: 1", "Bcc: hidden@example.org"], body=["t"])
        names = [name for name, _ in dre.validate_raw_eml(data).summary["headers"]]
        self.assertEqual(["to", "subject", "date", "message-id", "x-unsent", "bcc"], names)


class MessageIdAndRequiredHeaderTests(_Base):
    def test_a_missing_message_id_is_refused(self) -> None:  # R8
        self.assertRefused("raw_eml_header_missing", plain(message_id=None))

    def test_duplicate_message_id_headers_are_refused_case_insensitively(self) -> None:  # R8
        self.assertRefused("raw_eml_header_duplicate", plain(extra_headers=["message-id: <other@example.org>"]))
        self.assertRefused("raw_eml_header_duplicate", plain(extra_headers=["MESSAGE-ID: <raw1@example.org>"]))

    def test_malformed_message_ids_are_refused(self) -> None:  # R8
        bad = [
            "raw1@example.org",  # no angle brackets
            "<raw1@example.org> <b@example.org>",  # two ids
            "<raw1@example.org> (comment)",
            "<>",
            "<@example.org>",
            "<raw1@>",
            "<raw1>",
            "<raw 1@example.org>",
            "<a..b@example.org>",
            "<.a@example.org>",
            "<a.@example.org>",
            "<a@exa mple.org>",
            "<a@-example.org>",
            "<a@example..org>",
            "<a@exämple.org>",
            "<a@[127.0.0.1]>",
            '<"quoted"@example.org>',
            "<a@example.org",
            "<" + "a" * 250 + "@example.org>",
            "",
        ]
        for value in bad:
            with self.subTest(value):
                self.assertRefused("raw_eml_message_id_invalid", plain(message_id=value))

    def test_well_formed_message_ids_are_accepted_and_normalized(self) -> None:  # R8
        for value, expected in (
            ("<Raw.One+tag@Example.ORG>", "raw.one+tag@example.org"),
            ("<a1.b2@mail-1.example.org>", "a1.b2@mail-1.example.org"),
            ("  <spaced@example.org>  ", "spaced@example.org"),
        ):
            with self.subTest(value):
                self.assertEqual(expected, dre.validate_raw_eml(plain(message_id=value)).message_id)

    def test_to_and_date_are_required_and_must_not_be_blank(self) -> None:  # R8
        self.assertRefused("raw_eml_header_missing", plain(to=None))
        self.assertRefused("raw_eml_header_missing", plain(date=None))
        self.assertRefused("raw_eml_header_missing", plain(to=" "))
        self.assertRefused("raw_eml_header_missing", plain(date=""))

    def test_a_missing_subject_is_reported_as_empty(self) -> None:  # R8
        self.assertEqual("", dre.validate_raw_eml(plain(subject=None)).subject)

    def test_singleton_headers_must_not_repeat(self) -> None:  # R8
        for name in ("To", "Subject", "Date", "From", "Cc", "Reply-To", "Sender", "In-Reply-To", "Bcc"):
            with self.subTest(name):
                data = plain(extra_headers=[f"{name}: x@example.org", f"{name}: y@example.org"])
                self.assertRefused("raw_eml_header_duplicate", data)
        self.assertRefused("raw_eml_header_duplicate", plain(extra_headers=["to: z@example.org"]))
        self.assertRefused(
            "raw_eml_header_duplicate",
            plain(extra_headers=["MIME-Version: 1.0", "MIME-Version: 1.0", "Content-Type: text/plain; charset=utf-8"]),
        )


class StructureTests(_Base):
    def test_a_utf8_body_without_mime_headers_is_refused_as_us_ascii(self) -> None:  # R9
        self.assertRefused("raw_eml_mime_invalid", plain(body=["Grüße"]))

    def test_mime_headers_require_mime_version_1_0(self) -> None:  # R9
        data = plain(extra_headers=["Content-Type: text/plain; charset=utf-8"])
        self.assertRefused("raw_eml_mime_invalid", data)
        data = plain(extra_headers=["MIME-Version: 2.0", "Content-Type: text/plain; charset=utf-8"])
        self.assertRefused("raw_eml_mime_invalid", data)

    def test_unsupported_body_charsets_and_encodings_are_refused(self) -> None:  # R9
        self.assertRefused(
            "raw_eml_mime_invalid",
            plain(extra_headers=["MIME-Version: 1.0", "Content-Type: text/plain; charset=iso-8859-1"]),
        )
        self.assertRefused(
            "raw_eml_mime_invalid",
            plain(extra_headers=["MIME-Version: 1.0", "Content-Type: text/plain; charset=utf-8",
                                 "Content-Transfer-Encoding: binary"]),
        )

    def test_a_non_text_top_level_is_unsupported(self) -> None:  # R9
        for content_type in ("text/html; charset=utf-8", "application/pdf", "multipart/alternative; boundary=B",
                             "multipart/related; boundary=B", "message/rfc822"):
            with self.subTest(content_type):
                data = plain(extra_headers=["MIME-Version: 1.0", f"Content-Type: {content_type}"])
                self.assertRefused("raw_eml_structure_unsupported", data)

    def test_nested_multipart_and_embedded_messages_are_unsupported(self) -> None:  # R9
        nested = ["Content-Type: multipart/alternative; boundary=INNER", "", "--INNER", *BODY_PART, "--INNER--"]
        self.assertRefused("raw_eml_structure_unsupported", mixed([nested]))
        embedded = ["Content-Type: message/rfc822", "Content-Disposition: attachment; filename=\"m.eml\"", "", "To: x@example.org", "", "t"]
        self.assertRefused("raw_eml_structure_unsupported", mixed([BODY_PART, embedded]))

    def test_the_first_part_must_be_the_plain_text_body(self) -> None:  # R9
        self.assertRefused("raw_eml_structure_unsupported", mixed([attachment_part(), BODY_PART]))
        html = ["Content-Type: text/html; charset=utf-8", "", "<p>x</p>"]
        self.assertRefused("raw_eml_structure_unsupported", mixed([html]))

    def test_attachments_need_attachment_disposition_filename_and_base64(self) -> None:  # R9
        self.assertRefused("raw_eml_attachment_invalid", mixed([BODY_PART, attachment_part(disposition="inline")]))
        self.assertRefused("raw_eml_attachment_invalid", mixed([BODY_PART, attachment_part(filename="")]))
        self.assertRefused("raw_eml_attachment_invalid", mixed([BODY_PART, attachment_part(encoding="7bit", payload="text")]))
        self.assertRefused("raw_eml_attachment_invalid", mixed([BODY_PART, attachment_part(encoding=None, payload="text")]))

    def test_attachment_names_and_content_types_are_validated(self) -> None:  # R9
        for filename in ("../x.bin", "a:b.bin", "con.txt", "x."):
            with self.subTest(filename):
                self.assertRefused("raw_eml_attachment_invalid", mixed([BODY_PART, attachment_part(filename=filename)]))

    def test_duplicate_attachment_names_are_refused_case_insensitively(self) -> None:  # R9
        self.assertRefused(
            "raw_eml_attachment_invalid",
            mixed([BODY_PART, attachment_part("A.bin"), attachment_part("a.BIN", payload="AAEC")]),
        )

    def test_invalid_or_empty_base64_is_refused(self) -> None:  # R9
        for payload in ("not base64!", "AAE", "AAEC=", "", "AA EC"):
            with self.subTest(payload):
                self.assertRefused("raw_eml_attachment_invalid", mixed([BODY_PART, attachment_part(payload=payload)]))

    def test_five_attachments_are_accepted_and_six_are_refused(self) -> None:  # R9
        five = [attachment_part(f"f{index}.bin") for index in range(5)]
        self.assertEqual(5, len(dre.validate_raw_eml(mixed([BODY_PART, *five])).attachments))
        six = [attachment_part(f"f{index}.bin") for index in range(6)]
        self.assertRefused("raw_eml_attachment_count_exceeded", mixed([BODY_PART, *six]))

    def test_an_attachment_of_exactly_three_mib_is_accepted_and_one_byte_more_is_not(self) -> None:  # R9
        def with_size(size: int) -> bytes:
            encoded = base64.encodebytes(b"\x07" * size).decode("ascii").replace("\n", "\r\n").rstrip("\r\n")
            return mixed([BODY_PART, attachment_part(payload=encoded)])

        (attachment,) = dre.validate_raw_eml(with_size(THREE_MIB)).attachments
        self.assertEqual(THREE_MIB, attachment.size)
        self.assertRefused("raw_eml_attachment_too_large", with_size(THREE_MIB + 1))

    def test_preamble_and_epilogue_text_is_refused_because_it_would_be_dropped(self) -> None:  # R9
        data = mixed([BODY_PART]).replace(b"--BOUND\r\n", b"verlorener Text\r\n--BOUND\r\n", 1)
        self.assertRefused("raw_eml_mime_invalid", data)
        data = mixed([BODY_PART]) + b"verlorener Text\r\n"
        self.assertRefused("raw_eml_mime_invalid", data)

    def test_mime_parser_defects_are_refused(self) -> None:  # R9
        data = mixed([BODY_PART]).replace(b"--BOUND--\r\n", b"")
        self.assertRefused("raw_eml_mime_invalid", data)
        data = join_lines([*headers(), "MIME-Version: 1.0", "Content-Type: multipart/mixed", "", "x"])
        self.assertRefused("raw_eml_mime_invalid", data)

    def test_a_text_only_multipart_with_one_body_part_is_accepted(self) -> None:  # R9
        prepared = dre.validate_raw_eml(mixed([BODY_PART]))
        self.assertEqual("mixed", prepared.summary["structure"])
        self.assertEqual(0, len(prepared.attachments))

    def test_error_texts_never_contain_message_content(self) -> None:  # R10
        cases = [
            plain(body=[SECRET + "\x00"]),
            plain(body=[SECRET + "\r"]),
            join_lines([*headers(), f"X-Secret : {SECRET}", "", "t"]),
            plain(message_id=f"<{SECRET} x@example.org>"),
            plain(extra_headers=[f"To: {SECRET}@example.org"]),
            mixed([BODY_PART, attachment_part(filename=f"../{SECRET}", payload=b64(SECRET.encode()))]),
            plain(body=[SECRET + "ä"]),
        ]
        for data in cases:
            with self.assertRaises(dre.RawEmlError) as raised:
                dre.validate_raw_eml(data)
            trace = "".join(traceback.format_exception(type(raised.exception), raised.exception, raised.exception.__traceback__))
            self.assertNotIn(SECRET, trace)
            self.assertNotIn(b64(SECRET.encode()), trace)


def with_header(part: list[str], line: str) -> list[str]:
    """Insert one more header line into a part (before its blank separator line)."""
    index = part.index("")
    return [*part[:index], line, *part[index:]]


class PartHeaderTests(_Base):  # review finding 1
    def test_duplicate_or_conflicting_content_headers_in_a_part_are_refused(self) -> None:  # P1
        cases = {
            "conflicting disposition": with_header(attachment_part(), "Content-Disposition: inline"),
            "same disposition twice": with_header(attachment_part(), 'Content-Disposition: attachment; filename="a.bin"'),
            "second content type": with_header(attachment_part(), "Content-Type: text/plain"),
            "second transfer encoding": with_header(attachment_part(), "Content-Transfer-Encoding: 7bit"),
            "second content id": with_header(with_header(attachment_part(), "Content-ID: <a@x>"), "Content-ID: <b@x>"),
            "second description": with_header(with_header(attachment_part(), "Content-Description: a"), "Content-Description: b"),
            "case-insensitive": with_header(attachment_part(), "content-type: text/plain"),
        }
        for label, part in cases.items():
            with self.subTest(label):
                self.assertRefused("raw_eml_part_header_invalid", mixed([BODY_PART, part]))

    def test_duplicate_content_headers_in_the_body_part_are_refused(self) -> None:  # P1
        for line in ("Content-Type: text/html", "Content-Transfer-Encoding: base64", "Content-Disposition: attachment"):
            with self.subTest(line):
                body = with_header(with_header(BODY_PART, line), line)
                self.assertRefused("raw_eml_part_header_invalid", mixed([body]))
        conflicting = with_header(BODY_PART, "Content-Type: application/pdf")
        self.assertRefused("raw_eml_part_header_invalid", mixed([conflicting]))

    def test_malformed_part_header_lines_are_refused(self) -> None:  # P2
        cases = {
            "no colon": "garbage line",
            "space before colon": "X-Bad : v",
            "empty field name": ": value",
            "non-ascii field name": "Bétreff: v",
            "continuation without field": " continuation",
        }
        for label, line in cases.items():
            with self.subTest(label):
                part = [line, *attachment_part()]  # first line of the part header block
                self.assertRefused("raw_eml_part_header_invalid", mixed([BODY_PART, part]))
        blank_continuation = with_header(with_header(attachment_part(), "X-Folded: a"), " ")
        self.assertRefused("raw_eml_part_header_invalid", mixed([BODY_PART, blank_continuation]))

    def test_control_and_separator_characters_in_part_headers_are_refused(self) -> None:  # P3
        for char in ("\x0b", "\x0c", "\x1f", "\x7f", " ", " ", "\x85"):
            with self.subTest(repr(char)):
                part = with_header(attachment_part(), f"X-Note: a{char}b")
                self.assertRefused("raw_eml_part_header_invalid", mixed([BODY_PART, part]))
                body = with_header(BODY_PART, f"X-Note: a{char}b")
                self.assertRefused("raw_eml_part_header_invalid", mixed([body]))

    def test_part_header_blocks_are_bounded(self) -> None:  # P2
        many = [f"X-H{index}: v" for index in range(dre.MAX_HEADER_FIELDS + 1)]
        part = attachment_part()
        index = part.index("")
        self.assertRefused("raw_eml_part_header_invalid", mixed([BODY_PART, [*part[:index], *many, *part[index:]]]))

    def test_a_part_without_a_header_separator_is_refused(self) -> None:  # P2
        part = ["Content-Type: application/octet-stream", "Content-Transfer-Encoding: base64",
                'Content-Disposition: attachment; filename="a.bin"']
        with self.assertRaises(dre.RawEmlError):
            dre.validate_raw_eml(mixed([BODY_PART, part]))

    def test_error_texts_for_part_headers_carry_no_content(self) -> None:  # P3
        part = with_header(attachment_part(), f"X-Secret : {SECRET}")
        exc = self.assertRefused("raw_eml_part_header_invalid", mixed([BODY_PART, part]))
        self.assertNotIn(SECRET, str(exc))

    def test_well_formed_part_headers_are_still_accepted(self) -> None:  # P4
        folded = [
            "Content-Type: application/pdf; name=\"a.pdf\"",
            "Content-Transfer-Encoding: base64",
            "Content-Disposition: attachment;",
            ' filename="a.pdf"',
            "Content-ID: <part1@example.org>",
            "Content-Description: Angebot",
            "X-Note: einmal",
            "", "AAEC",
        ]
        prepared = dre.validate_raw_eml(mixed([BODY_PART, folded]))
        self.assertEqual("a.pdf", prepared.attachments[0].filename)

    def test_every_duplicated_top_level_content_header_is_refused(self) -> None:  # P5
        for name in ("Content-Description", "Content-ID", "Content-Language", "Content-Location", "Content-Disposition",
                     "Content-Type", "Content-Transfer-Encoding"):
            with self.subTest(name):
                data = plain(extra_headers=["MIME-Version: 1.0", "Content-Type: text/plain; charset=utf-8",
                                            "Content-Transfer-Encoding: 7bit", f"{name}: x", f"{name}: y"])
                self.assertRefused("raw_eml_header_duplicate", data)


class AddressAndDateGrammarTests(_Base):  # review finding 2
    def test_not_an_address_is_refused_as_to(self) -> None:  # G1
        bad = [
            "not-an-address", "x", "a@", "@example.org", "a b@example.org", "a@@example.org", "<a@example.org",
            "a@example.org,", "a@example.org, ,b@example.org", "Name <>", "Bob <bob@example.org> extra",
            "A <a@example.org> <b@example.org>", "Doe, John <j@example.org>", "a@example..org", "ä@example.org",
            "a@exämple.org", "a@example.org b@example.org",
        ]
        for value in bad:
            with self.subTest(value):
                self.assertRefused("raw_eml_address_invalid", plain(to=value))

    def test_valid_rfc_5322_to_values_are_accepted(self) -> None:  # G1
        good = [
            "a@example.org", "Empfänger <a@example.org>", '"Doe, John" <j@example.org>',
            "a@example.org, b@example.org", "=?utf-8?q?Empf=C3=A4nger?= <a@example.org>",
            "undisclosed-recipients:;", "Team: a@example.org, b@example.org;", "a@example.org (Kommentar)",
        ]
        for value in good:
            with self.subTest(value):
                header = ["MIME-Version: 1.0", "Content-Type: text/plain; charset=utf-8", "Content-Transfer-Encoding: 8bit"]
                data = join_lines([*headers(to=value, extra=header), "", "ok"])
                self.assertEqual(" ".join(value.split()), dre.validate_raw_eml(data).summary["headers"][0][1])

    def test_invalid_dates_are_refused(self) -> None:  # G2
        bad = [
            "Sat, 99 Foo 2026 99:99:99 +9999", "not-a-date", "Sat", "", "Sat, 07 Nov 2026 25:00:00 +0100",
            "Sat, 07 Nov 2026 12:60:00 +0100", "Sat, 07 Nov 2026 12:00:61 +0100", "Sat, 31 Nov 2026 12:00:00 +0100",
            "Sat, 30 Feb 2026 12:00:00 +0100", "Mon, 07 Nov 2026 12:00:00 +0100", "07 Nov 2026 12:00:00",
            "Sat, 07 Nov 2026 12:00:00 +0160", "Sat, 07 Nov 2026 12:00:00 +2500", "Sat, 07 Nov 26 12:00:00 +0100",
            "Sat, 07 Nov 2026 12:00:00 GMT", "Sat, 07 Nov 2026 12:00:00 UT", "Sat, 07 Nov 2026 12:00:00 +0100 x",
            "Sat, 007 Nov 2026 12:00:00 +0100", "Sat, 07 Nov 2026 1:00:00 +0100", "Sat, 07 Nov 2026 12:00:00 +01:00",
            "Sat, 00 Nov 2026 12:00:00 +0100", "Sat, 07 Nov 1899 12:00:00 +0100", "Sat, 07 Nov 12026 12:00:00 +0100",
            "Sat, 07 Nov 2026 12:00:00 +0100 (unclosed",
        ]
        for value in bad:
            with self.subTest(value):
                self.assertRefused("raw_eml_header_missing" if not value else "raw_eml_date_invalid", plain(date=value))

    def test_valid_rfc_5322_dates_are_accepted(self) -> None:  # G2
        good = [
            "Sat, 07 Nov 2026 12:00:00 +0100", "Sat, 7 Nov 2026 12:00:00 +0100", "07 Nov 2026 12:00:00 +0100",
            "Sat, 07 Nov 2026 12:00 +0100", "Sat, 07 Nov 2026 12:00:00 -0000", "Sat, 07 Nov 2026 23:59:60 +1400",
            "Sun, 29 Feb 2032 00:00:00 +0000", "Sat, 07 Nov 2026 12:00:00 -1200",
            "Sat, 07 Nov 2026 12:00:00 +0100 (CET)", "Sat,07 Nov 2026 12:00:00 +0100", "Sat, 07 nov 2026 12:00:00 +0100",
            "SAT, 07 NOV 2026 12:00:00 +0100",
        ]
        for value in good:
            with self.subTest(value):
                self.assertEqual(value, dre.validate_raw_eml(plain(date=value)).date)

    def test_the_grammar_errors_carry_no_content(self) -> None:  # G3
        exc = self.assertRefused("raw_eml_address_invalid", plain(to=SECRET))
        self.assertNotIn(SECRET, str(exc))
        exc = self.assertRefused("raw_eml_date_invalid", plain(date=SECRET))
        self.assertNotIn(SECRET, str(exc))


class BodyFidelityTests(_Base):  # review finding 3
    def test_the_summary_keeps_trailing_newlines_of_the_text(self) -> None:  # B1
        one = dre.validate_raw_eml(make_eml(body="Text\n"))
        three = dre.validate_raw_eml(make_eml(body="Text\n\n\n"))
        self.assertEqual("Text\n", one.summary["body"])
        self.assertEqual("Text\n\n\n", three.summary["body"])
        self.assertNotEqual(one.summary, three.summary)

    def test_a_stored_copy_with_other_trailing_newlines_differs(self) -> None:  # B1
        prepared = dre.validate_raw_eml(make_eml(body="Text\n", message_id="<tn@example.org>"))
        stored = make_eml(body="Text\n\n\n", message_id="<tn@example.org>")
        self.assertEqual(["body:text"], dre.compare_stored_raw(stored, prepared))

    def test_crlf_blank_line_counts_differ_in_a_raw_text_message(self) -> None:  # B1
        a = join_lines([*headers(), "", "Text"])
        b = join_lines([*headers(), "", "Text", "", ""])
        self.assertEqual(["body:text"], dre.compare_stored_raw(b, dre.validate_raw_eml(a)))
        self.assertEqual([], dre.compare_stored_raw(b.replace(b"\r\n", b"\n"), dre.validate_raw_eml(b)))

    def test_trailing_newlines_of_a_multipart_body_part_are_compared(self) -> None:  # B1
        body_a = ["Content-Type: text/plain; charset=utf-8", "Content-Transfer-Encoding: 7bit", "", "Text."]
        body_b = ["Content-Type: text/plain; charset=utf-8", "Content-Transfer-Encoding: 7bit", "", "Text.", "", ""]
        att = attachment_part()
        self.assertEqual(
            ["body:text"],
            dre.compare_stored_raw(mixed([body_b, att]), dre.validate_raw_eml(mixed([body_a, att]))),
        )

    def test_line_ending_form_alone_still_does_not_matter(self) -> None:  # B1
        data = make_eml(body="A\n\nB\n")
        self.assertEqual([], dre.compare_stored_raw(data.replace(b"\r\n", b"\n"), dre.validate_raw_eml(data)))


class SummaryComparisonTests(_Base):
    def test_a_semantically_identical_stored_copy_has_no_problems(self) -> None:  # V1
        data = make_eml(attachments=[("report.pdf", "application/pdf", BINARY)])
        prepared = dre.validate_raw_eml(data)
        self.assertEqual([], dre.compare_stored_raw(data, prepared))
        self.assertEqual([], dre.compare_stored_raw(data.replace(b"\r\n", b"\n"), prepared),
                         "the line ending form of the stored copy does not matter")

    def test_every_semantic_difference_is_reported(self) -> None:  # V1
        base = dict(attachments=[("report.pdf", "application/pdf", BINARY)])
        prepared = dre.validate_raw_eml(make_eml(**base))
        cases = {
            "subject": (make_eml(subject="Anders", **base), "header:subject"),
            "body": (make_eml(body="Anderer Text\n", **base), "body:text"),
            "added header": (make_eml(extra_headers=[("X-New", "1")], **base), "header:x-new"),
            "to": (make_eml(to="b@example.org", **base), "header:to"),
            "no attachment": (make_eml(), "attachment_count:0!=1"),
            "attachment bytes": (make_eml(attachments=[("report.pdf", "application/pdf", BINARY[:-1] + b"\x00")]), "attachment[1]:sha256"),
            "attachment name": (make_eml(attachments=[("other.pdf", "application/pdf", BINARY)]), "attachment[1]:filename"),
            "attachment type": (make_eml(attachments=[("report.pdf", "application/zip", BINARY)]), "attachment[1]:content_type"),
            "message id": (make_eml(message_id="<other@example.org>", **base), "header:message-id"),
        }
        for label, (stored, expected) in cases.items():
            with self.subTest(label):
                self.assertIn(expected, dre.compare_stored_raw(stored, prepared))

    def test_header_order_is_part_of_the_comparison(self) -> None:  # V1
        a = join_lines([*headers(extra=["X-One: 1", "X-Two: 2"]), "", "t"])
        b = join_lines([*headers(extra=["X-Two: 2", "X-One: 1"]), "", "t"])
        self.assertIn("header_order", dre.compare_stored_raw(b, dre.validate_raw_eml(a)))

    def test_an_invalid_stored_message_is_a_problem_not_an_exception(self) -> None:  # V1
        prepared = dre.validate_raw_eml(make_eml())
        self.assertEqual(["stored:raw_eml_header_missing"], dre.compare_stored_raw(plain(message_id=None), prepared))
        self.assertEqual(["stored:raw_eml_not_utf8"], dre.compare_stored_raw(b"\xff\xfe", prepared))


if __name__ == "__main__":
    unittest.main()
