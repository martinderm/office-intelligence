"""Raw-EML import (#18), final review round — strict MIME values, address/date grammar, part-header fidelity, part bound.

Evidence mode ``tdd``.  Matrix rows H1-H4 (one test class per residual finding; every reviewer
reproduction is a test).  Seam: ``core.draft_raw_eml.validate_raw_eml`` / ``compare_stored_raw`` — pure, no
file system, no mailbox, no network.  Expected values are literals or built with the standard library,
independent of the code under test.
"""

from __future__ import annotations

import sys
import traceback
import unittest
from pathlib import Path
from unittest.mock import patch

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import draft_raw_eml as dre  # noqa: E402
from core import drafts  # noqa: E402
from raw_eml_fixtures import (  # noqa: E402
    BODY_PART,
    attachment_part,
    headers,
    join_lines,
    make_eml,
    mixed,
    plain,
)

SECRET = "SECRET-HARDENING-MARKER-0451"


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


def part(content_type: str | None = "application/pdf", disposition: str | None = 'attachment; filename="a.pdf"',
         encoding: str | None = "base64", payload: str = "AAEC", extra: tuple[str, ...] = ()) -> list[str]:
    """A hand-written attachment part; ``None`` omits the header."""
    lines = []
    if content_type is not None:
        lines.append(f"Content-Type: {content_type}")
    if encoding is not None:
        lines.append(f"Content-Transfer-Encoding: {encoding}")
    if disposition is not None:
        lines.append(f"Content-Disposition: {disposition}")
    return [*lines, *extra, "", payload]


def body(content_type: str | None = "text/plain; charset=utf-8", encoding: str | None = "7bit",
         disposition: str | None = None, extra: tuple[str, ...] = (), text: str = "Text.") -> list[str]:
    lines = []
    if content_type is not None:
        lines.append(f"Content-Type: {content_type}")
    if encoding is not None:
        lines.append(f"Content-Transfer-Encoding: {encoding}")
    if disposition is not None:
        lines.append(f"Content-Disposition: {disposition}")
    return [*lines, *extra, "", text]


def custom_mixed(top_content_type: str, parts: list[list[str]], boundary: str = "BOUND") -> bytes:
    lines = [*headers(), "MIME-Version: 1.0", f"Content-Type: {top_content_type}", ""]
    for one in parts:
        lines.append(f"--{boundary}")
        lines.extend(one)
    lines.append(f"--{boundary}--")
    return join_lines(lines)


def top_text(content_type: str, *extra: str, text: str = "Hallo.") -> bytes:
    return join_lines([*headers(), "MIME-Version: 1.0", f"Content-Type: {content_type}", *extra, "", text])


class StrictMimeValueTests(_Base):  # residual finding 1
    def test_multiple_content_types_are_refused(self) -> None:  # H1
        self.assertRefused("raw_eml_mime_invalid", top_text("text/html, text/plain", "Content-Transfer-Encoding: 7bit"))
        self.assertRefused("raw_eml_mime_invalid", top_text("text/plain, text/html"))
        self.assertRefused("raw_eml_mime_invalid", mixed([BODY_PART, part(content_type="application/pdf, text/html")]))
        self.assertRefused("raw_eml_mime_invalid", mixed([body("text/plain, text/html")]))

    def test_trailing_garbage_in_content_headers_is_refused(self) -> None:  # H1
        cases = {
            "disposition garbage": part(disposition='attachment; filename="a.pdf" garbage'),
            "disposition garbage after type": part(disposition='attachment garbage; filename="a.pdf"'),
            "type garbage": part(content_type="application/pdf garbage"),
            "type garbage after param": part(content_type='application/pdf; name="a.pdf" garbage'),
            "cte garbage": part(encoding="base64 garbage"),
            "cte list": part(encoding="base64, 7bit"),
            "dangling semicolon": part(disposition='attachment; filename="a.pdf";'),
            "param without value": part(disposition="attachment; filename"),
            "space around equals": part(disposition='attachment; filename = "a.pdf"'),
            "unterminated quote": part(disposition='attachment; filename="a.pdf'),
            "comment in type": part(content_type="application/pdf (comment)"),
            "empty type": part(content_type=""),
            "type without subtype": part(content_type="application"),
        }
        for label, one in cases.items():
            with self.subTest(label):
                self.assertRefused("raw_eml_mime_invalid", mixed([BODY_PART, one]))

    def test_trailing_garbage_in_top_level_headers_is_refused(self) -> None:  # H1
        self.assertRefused("raw_eml_mime_invalid", top_text("text/plain; charset=utf-8 garbage"))
        self.assertRefused("raw_eml_mime_invalid", top_text("text/plain; charset=utf-8", "Content-Transfer-Encoding: 7bit x"))

    def test_duplicate_parameters_are_refused(self) -> None:  # H1
        self.assertRefused("raw_eml_mime_invalid", mixed([BODY_PART, part(disposition='attachment; filename="a.pdf"; filename="b.pdf"')]))
        self.assertRefused("raw_eml_mime_invalid", mixed([BODY_PART, part(disposition="attachment; filename=a.pdf; FILENAME=b.pdf")]))
        self.assertRefused("raw_eml_mime_invalid", mixed([BODY_PART, part(disposition="attachment; filename=a.pdf; filename*=utf-8''b.pdf")]))
        self.assertRefused("raw_eml_mime_invalid", mixed([BODY_PART, part(content_type='application/pdf; name="a.pdf"; name="b.pdf"')]))
        self.assertRefused("raw_eml_mime_invalid", top_text("text/plain; charset=utf-8; charset=us-ascii"))
        self.assertRefused("raw_eml_mime_invalid", mixed([body("text/plain; charset=utf-8; charset=us-ascii")]))
        self.assertRefused("raw_eml_mime_invalid", custom_mixed("multipart/mixed; boundary=BOUND; boundary=OTHER", [BODY_PART]))
        self.assertRefused("raw_eml_mime_invalid", custom_mixed("multipart/mixed; boundary=BOUND; BOUNDARY=BOUND", [BODY_PART]))

    def test_a_missing_or_invalid_boundary_is_refused(self) -> None:  # H1
        self.assertRefused("raw_eml_mime_invalid", custom_mixed("multipart/mixed", [BODY_PART]))
        self.assertRefused("raw_eml_mime_invalid", custom_mixed('multipart/mixed; boundary=""', [BODY_PART], boundary=""))
        self.assertRefused("raw_eml_mime_invalid", custom_mixed('multipart/mixed; boundary="' + "B" * 71 + '"', [BODY_PART], boundary="B" * 71))
        self.assertRefused("raw_eml_mime_invalid", custom_mixed('multipart/mixed; boundary="B "', [BODY_PART], boundary="B "))
        self.assertRefused("raw_eml_mime_invalid", custom_mixed('multipart/mixed; boundary="BäD"', [BODY_PART], boundary="BäD"))

    def test_name_and_filename_paths_and_dot_dot_are_refused(self) -> None:  # H1
        for value in ("../x.bin", "..", "a/b.bin", "a\\\\b.bin", "C:x.bin", "x/..", "/etc/passwd", "con.txt", "x."):
            with self.subTest(value):
                self.assertRefused("raw_eml_attachment_invalid", mixed([BODY_PART, part(disposition=f'attachment; filename="{value}"')]))
                self.assertRefused(
                    "raw_eml_attachment_invalid",
                    mixed([BODY_PART, part(content_type=f'application/pdf; name="{value}"', disposition='attachment; filename="a.pdf"')]),
                )
        self.assertRefused(
            "raw_eml_attachment_invalid",
            mixed([BODY_PART, part(content_type='application/pdf; name="other.pdf"', disposition='attachment; filename="a.pdf"')]),
        )

    def test_rfc_2231_filenames_are_decoded_strictly(self) -> None:  # H1
        ok = {
            "filename*=utf-8''N%C3%A4me.txt": "Näme.txt",
            "filename*=UTF-8'de'N%C3%A4me.txt": "Näme.txt",
            "filename*0=\"ab\"; filename*1=\"cd.txt\"": "abcd.txt",
            "filename*0*=utf-8''N%C3%A4; filename*1*=me.txt": "Näme.txt",
            "filename*0*=utf-8''N%C3%A4; filename*1=me.txt": "Näme.txt",
        }
        for params, expected in ok.items():
            with self.subTest(params):
                prepared = dre.validate_raw_eml(mixed([BODY_PART, part(disposition=f"attachment; {params}")]))
                self.assertEqual(expected, prepared.attachments[0].filename)
        bad = [
            "filename*=iso-8859-1''N%E4me.txt", "filename*=utf-8''N%C3me.txt", "filename*=utf-8''N%ZZ.txt",
            "filename*=utf-8'N%C3%A4me.txt", "filename*=N%C3%A4me.txt", "filename*0=ab; filename*2=cd.txt",
            "filename*0=ab; filename*0=cd.txt", "filename*1=ab; filename*2=cd.txt", "filename*0=ab; filename=cd.txt",
            "filename*0*=iso-8859-1''ab", "filename*0=ab; filename*1*=%C3%A4; filename*x=1",
        ]
        for params in bad:
            with self.subTest(params):
                self.assertRefused("raw_eml_mime_invalid", mixed([BODY_PART, part(disposition=f"attachment; {params}")]))

    def test_builder_style_long_and_unusual_names_still_round_trip(self) -> None:  # H1 (stdlib emits continuations)
        names = ["a" * 120 + ".bin", "Notiz Ärger.txt", "ä" * 60 + ".txt", "x y (1).pdf", "we;ird,name=y.txt", "日本語.txt"]
        prepared = dre.validate_raw_eml(make_eml(attachments=[(n, "application/octet-stream", b"\x01\x02\x03") for n in names][:5]))
        self.assertEqual(names[:5], [a.filename for a in prepared.attachments])

    def test_the_body_may_not_be_an_attachment_or_carry_a_file_name(self) -> None:  # H1
        self.assertRefused(
            "raw_eml_structure_unsupported",
            top_text('text/plain; charset=utf-8; name="../x"', "Content-Disposition: attachment"),
        )
        self.assertRefused("raw_eml_structure_unsupported", top_text("text/plain; charset=utf-8", "Content-Disposition: attachment"))
        self.assertRefused("raw_eml_structure_unsupported", top_text("text/plain; charset=utf-8", 'Content-Disposition: inline; filename="a.txt"'))
        self.assertRefused("raw_eml_structure_unsupported", top_text('text/plain; charset=utf-8; name="a.txt"'))
        self.assertRefused("raw_eml_structure_unsupported", mixed([body(disposition="attachment")]))
        self.assertRefused("raw_eml_structure_unsupported", mixed([body(disposition='inline; filename="a.txt"')]))
        self.assertRefused("raw_eml_structure_unsupported", mixed([body('text/plain; charset=utf-8; name="../x"')]))
        self.assertEqual("Hallo.\n", dre.validate_raw_eml(top_text("text/plain; charset=utf-8", "Content-Disposition: inline")).summary["body"])
        self.assertEqual("Text.", dre.validate_raw_eml(mixed([body(disposition="inline")])).summary["body"])

    def test_html_in_any_position_is_refused(self) -> None:  # H1
        self.assertRefused("raw_eml_structure_unsupported", top_text("text/html; charset=utf-8", text="<p>x</p>"))
        self.assertRefused("raw_eml_structure_unsupported", mixed([body("text/html; charset=utf-8", text="<p>x</p>")]))
        self.assertRefused("raw_eml_mime_invalid", top_text("text/html, text/plain; charset=utf-8", text="<p>x</p>"))

    def test_the_strict_view_must_agree_with_the_stdlib_view(self) -> None:  # H1
        prepared = dre.validate_raw_eml(mixed([BODY_PART, part(content_type='Application/PDF; Name="a.pdf"', disposition='ATTACHMENT; FILENAME="a.pdf"')]))
        self.assertEqual("application/pdf", prepared.attachments[0].content_type)

    def test_valid_text_parameters_and_quoting_are_accepted(self) -> None:  # H1
        for content_type in ('text/plain; charset="utf-8"', "text/plain; charset=UTF-8; format=flowed", "text/plain;charset=us-ascii",
                             'text/plain ; charset="utf-8"'):
            with self.subTest(content_type):
                data = join_lines([*headers(), "MIME-Version: 1.0", f"Content-Type: {content_type}", "", "Hallo."])
                self.assertEqual("Hallo.\n", dre.validate_raw_eml(data).summary["body"])

    def test_error_texts_carry_no_header_values(self) -> None:  # H1
        exc = self.assertRefused("raw_eml_mime_invalid", mixed([BODY_PART, part(disposition=f'attachment; filename="{SECRET}" garbage')]))
        self.assertNotIn(SECRET, str(exc))


class AddressAndDateUnicodeTests(_Base):  # residual finding 2
    def test_non_ascii_digits_in_date_are_refused(self) -> None:  # H2
        for value in (
            "Sat, ٠٧ Nov 2026 12:00:00 +0100", "Sat, 07 Nov ٢٠٢٦ 12:00:00 +0100",
            "Sat, 07 Nov 2026 ١٢:00:00 +0100", "Sat, 07 Nov 2026 12:00:00 +٠١٠٠",
            "Sat, 07 Nov 2026 12:٠٠:00 +0100", "Sat, 07 Nov 2026 12:00:٠٠ +0100",
            "Sat, ０７ Nov 2026 12:00:00 +0100",
        ):
            with self.subTest(value):
                self.assertRefused("raw_eml_date_invalid", plain(date=value))

    def test_unicode_whitespace_in_date_is_refused(self) -> None:  # H2
        for char in (" ", " ", " ", "　", " "):
            with self.subTest(repr(char)):
                self.assertRefused("raw_eml_date_invalid", plain(date=f"Sat,{char}07 Nov 2026 12:00:00 +0100"))
                self.assertRefused("raw_eml_date_invalid", plain(date=f"Sat, 07 Nov 2026 12:00:00 +0100{char}"))
                self.assertRefused("raw_eml_date_invalid", plain(date=f"{char}Sat, 07 Nov 2026 12:00:00 +0100"))

    def test_unicode_whitespace_in_to_outside_quotes_is_refused(self) -> None:  # H2
        for char in (" ", " ", " ", "　"):
            with self.subTest(repr(char)):
                self.assertRefused("raw_eml_address_invalid", plain(to=f"a@example.org,{char}b@example.org"))
                self.assertRefused("raw_eml_address_invalid", plain(to=f"{char}a@example.org"))
                self.assertRefused("raw_eml_address_invalid", plain(to=f"a@example.org{char}"))
                self.assertRefused("raw_eml_address_invalid", plain(to=f"Name{char}<a@example.org>"))

    def test_unicode_whitespace_inside_a_quoted_display_name_is_accepted(self) -> None:  # H2
        data = join_lines([*headers(to='"Doe John" <a@example.org>', extra=["MIME-Version: 1.0", "Content-Type: text/plain; charset=utf-8", "Content-Transfer-Encoding: 8bit"]), "", "ok"])
        self.assertEqual('"Doe John" <a@example.org>', dre.validate_raw_eml(data).summary["headers"][0][1])

    def test_commas_inside_quotes_and_comments_are_not_list_separators(self) -> None:  # H2
        for value in ('"Doe, , John" <a@example.org>', "a@example.org (comment, , b)", '"a, , b"@example.org',
                      'Name (x, , y) <a@example.org>', '"Doe, John" <j@example.org>, "A, , B" <b@example.org>',
                      "a@example.org (one) (two)", "a@example.org (nested (comma, , ok))", 'Team: "A, , B" <a@example.org>;',
                      '"quoted \\" , , quote" <a@example.org>'):
            with self.subTest(value):
                data = join_lines([*headers(to=value, extra=["MIME-Version: 1.0", "Content-Type: text/plain; charset=utf-8", "Content-Transfer-Encoding: 8bit"]), "", "ok"])
                self.assertEqual(value, dre.validate_raw_eml(data).summary["headers"][0][1])

    def test_real_empty_list_elements_and_unbalanced_syntax_are_refused(self) -> None:  # H2
        for value in ("Group: a@example.org,;", "a@example.org,", ",a@example.org", "a@example.org, ,b@example.org",
                      "a@example.org,,b@example.org", "Group: ,a@example.org;", "a@example.org (unclosed",
                      '"unclosed <a@example.org>', "a@example.org (x))", "Group: a@example.org, , b@example.org;",
                      "a@example.org , (c) , b@example.org", "(only a comment)", "a@example.org (x) ,"):
            with self.subTest(value):
                self.assertRefused("raw_eml_address_invalid", plain(to=value))

    def test_reviewer_examples_that_must_stay_refused(self) -> None:  # H2
        for value in ("not-an-address", "Doe, John <j@example.org>", "a@", "Name <>"):
            with self.subTest(value):
                self.assertRefused("raw_eml_address_invalid", plain(to=value))
        for value in ("not-a-date", "Sat, 99 Foo 2026 99:99:99 +9999", "Sat, 07 Nov 2026 12:00:00 GMT"):
            with self.subTest(value):
                self.assertRefused("raw_eml_date_invalid", plain(date=value))

    def test_header_summary_values_keep_unicode_whitespace_significant(self) -> None:  # H2
        a = join_lines([*headers(extra=["MIME-Version: 1.0", "Content-Type: text/plain; charset=utf-8", "Content-Transfer-Encoding: 8bit",
                                         "X-Note: a b"]), "", "t"])
        b = a.replace(b"X-Note: a b", "X-Note: a b".encode("utf-8"))
        self.assertEqual(["header:x-note"], dre.compare_stored_raw(b, dre.validate_raw_eml(a)))


class PartHeaderFidelityTests(_Base):  # residual finding 3
    def prepared(self, **kwargs):
        att = part(content_type="application/pdf", disposition='attachment; filename="a.pdf"',
                   extra=("Content-ID: <a@x>", "Content-Description: first", "Content-Language: de", "Content-Location: http://x/a"))
        bod = body(extra=("Content-Language: de",))
        return mixed([bod, att]), bod, att

    def test_an_identical_stored_copy_has_no_problems(self) -> None:  # H3
        data, _, _ = self.prepared()
        prepared = dre.validate_raw_eml(data)
        self.assertEqual([], dre.compare_stored_raw(data, prepared))
        self.assertEqual([], dre.compare_stored_raw(data.replace(b"\r\n", b"\n"), prepared))

    def test_changed_attachment_part_header_values_are_reported(self) -> None:  # H3
        data, _, att = self.prepared()
        prepared = dre.validate_raw_eml(data)
        cases = {
            "Content-ID": ("Content-ID: <a@x>", "Content-ID: <b@x>", "part[2]:header:content-id"),
            "Content-Description": ("Content-Description: first", "Content-Description: second", "part[2]:header:content-description"),
            "Content-Language": ("Content-Language: de", "Content-Language: en", None),
            "Content-Location": ("Content-Location: http://x/a", "Content-Location: http://x/b", "part[2]:header:content-location"),
            "disposition parameter": ('Content-Disposition: attachment; filename="a.pdf"', 'Content-Disposition: attachment; filename="a.pdf"; size=3', "part[2]:header:content-disposition"),
        }
        for label, (old, new, expected) in cases.items():
            with self.subTest(label):
                stored = data.replace(old.encode(), new.encode())
                self.assertNotEqual(data, stored)
                problems = dre.compare_stored_raw(stored, prepared)
                self.assertTrue(problems, "a changed part header must be reported")
                if expected:
                    self.assertIn(expected, problems)
        stored = data.replace(b"Content-Language: de\r\nContent-Location", b"Content-Language: en\r\nContent-Location")
        self.assertIn("part[2]:header:content-language", dre.compare_stored_raw(stored, prepared))

    def test_changed_body_part_headers_are_reported(self) -> None:  # H3
        bod = body(encoding="7bit", extra=("Content-Language: de",), text="Text.")
        data = mixed([bod, part()])
        prepared = dre.validate_raw_eml(data)
        qp = dre.compare_stored_raw(data.replace(b"Content-Transfer-Encoding: 7bit", b"Content-Transfer-Encoding: quoted-printable"), prepared)
        self.assertEqual(["part[1]:header:content-transfer-encoding"], qp)
        charset = dre.compare_stored_raw(data.replace(b"charset=utf-8", b"charset=us-ascii", 1), prepared)
        self.assertEqual(["part[1]:header:content-type"], charset)
        inline = dre.compare_stored_raw(data.replace(b"Content-Language: de", b"Content-Language: de\r\nContent-Disposition: inline"), prepared)
        self.assertIn("part[1]:header:content-disposition", inline)

    def test_added_removed_and_reordered_part_headers_are_reported(self) -> None:  # H3
        data, _, _ = self.prepared()
        prepared = dre.validate_raw_eml(data)
        added = dre.compare_stored_raw(data.replace(b"Content-ID: <a@x>", b"Content-ID: <a@x>\r\nX-Added: 1"), prepared)
        self.assertEqual(["part[2]:header:x-added"], added)
        removed = dre.compare_stored_raw(data.replace(b"Content-ID: <a@x>\r\n", b""), prepared)
        self.assertEqual(["part[2]:header:content-id"], removed)
        reordered = dre.compare_stored_raw(
            data.replace(b"Content-ID: <a@x>\r\nContent-Description: first", b"Content-Description: first\r\nContent-ID: <a@x>"), prepared
        )
        self.assertEqual(["part[2]:header_order"], reordered)

    def test_the_part_headers_are_part_of_the_summary(self) -> None:  # H3
        data, _, _ = self.prepared()
        parts = dre.validate_raw_eml(data).summary["parts"]
        self.assertEqual(2, len(parts))
        self.assertIn(("content-id", "<a@x>"), parts[1])
        self.assertIn(("content-language", "de"), parts[0])
        self.assertEqual([], dre.validate_raw_eml(make_eml()).summary["parts"])

    def test_part_header_whitespace_is_squashed_but_unicode_whitespace_is_not(self) -> None:  # H3
        a = mixed([BODY_PART, part(extra=("Content-Description: a   b",))])
        b = mixed([BODY_PART, part(extra=("Content-Description: a b",))])
        c = mixed([BODY_PART, part(extra=("Content-Description: a b",))])
        self.assertEqual([], dre.compare_stored_raw(b, dre.validate_raw_eml(a)))
        self.assertEqual(["part[2]:header:content-description"], dre.compare_stored_raw(c, dre.validate_raw_eml(a)))

    def test_text_only_messages_have_no_part_header_problems(self) -> None:  # H3
        data = make_eml()
        self.assertEqual([], dre.compare_stored_raw(data, dre.validate_raw_eml(data)))


class PartCountBoundTests(_Base):  # residual finding 4
    def test_the_bound_is_sixty_four(self) -> None:  # H4
        self.assertEqual(64, dre.MAX_MIME_PARTS)

    def test_normal_messages_with_up_to_five_attachments_are_unchanged(self) -> None:  # H4
        five = [attachment_part(f"f{index}.bin") for index in range(5)]
        self.assertEqual(5, len(dre.validate_raw_eml(mixed([BODY_PART, *five])).attachments))
        six = [attachment_part(f"f{index}.bin") for index in range(6)]
        self.assertRefused("raw_eml_attachment_count_exceeded", mixed([BODY_PART, *six]))

    def test_between_seven_and_sixty_four_parts_still_report_the_attachment_count(self) -> None:  # H4
        parts = [BODY_PART, *[attachment_part(f"f{index}.bin") for index in range(63)]]
        self.assertEqual(64, len(parts))
        self.assertRefused("raw_eml_attachment_count_exceeded", mixed(parts))

    def test_sixty_five_parts_are_refused_with_the_own_code(self) -> None:  # H4
        parts = [BODY_PART, *[attachment_part(f"f{index}.bin") for index in range(64)]]
        self.assertEqual(65, len(parts))
        exc = self.assertRefused("raw_eml_part_count_exceeded", mixed(parts))
        self.assertNotIn("65", str(exc))

    def test_many_boundaries_are_refused_before_any_part_is_materialized(self) -> None:  # H4
        lines = [*headers(), "MIME-Version: 1.0", 'Content-Type: multipart/mixed; boundary="BOUND"', ""]
        lines += ["--BOUND"] * 200000
        data = join_lines(lines)
        self.assertLess(len(data), 10 * 1048576)
        real = dre._parse_header_block

        def guarded(*args, **kwargs):
            if kwargs.get("part"):
                raise AssertionError("a part header block was parsed before the part bound fired")
            return real(*args, **kwargs)

        with patch.object(dre, "_parse_header_block", guarded), \
                patch.object(dre.da, "_parse", side_effect=AssertionError("the stdlib parser ran before the part bound")):
            self.assertRefused("raw_eml_part_count_exceeded", data)

    def test_many_closing_free_delimiters_and_content_lines_are_bounded_too(self) -> None:  # H4
        lines = [*headers(), "MIME-Version: 1.0", 'Content-Type: multipart/mixed; boundary="BOUND"', ""]
        for _ in range(70):
            lines += ["--BOUND", "Content-Type: text/plain", "", "x"]
        self.assertRefused("raw_eml_part_count_exceeded", join_lines(lines))

    def test_exactly_the_bound_of_delimiters_does_not_trip_the_part_count_code(self) -> None:  # H4
        lines = [*headers(), "MIME-Version: 1.0", 'Content-Type: multipart/mixed; boundary="BOUND"', ""]
        for _ in range(64):
            lines += ["--BOUND", "Content-Type: text/plain", "", "x"]
        lines.append("--BOUND--")
        with self.assertRaises(dre.RawEmlError) as raised:
            dre.validate_raw_eml(join_lines(lines))
        self.assertNotEqual("raw_eml_part_count_exceeded", raised.exception.reason_code)


class CanonicalParameterPositionTests(_Base):  # final-round finding 1
    """``name`` lives only in Content-Type, ``filename`` only in Content-Disposition -- everywhere."""

    def test_filename_in_content_type_and_name_in_disposition_are_refused_for_text(self) -> None:  # F1
        for value in ('"../x"', '"x.txt"', "x"):
            with self.subTest(value):
                self.assertRefused(
                    "raw_eml_structure_unsupported",
                    top_text(f"text/plain; charset=utf-8; filename={value}"),
                )
                self.assertRefused(
                    "raw_eml_structure_unsupported",
                    top_text("text/plain; charset=utf-8", f"Content-Disposition: inline; name={value}"),
                )
                self.assertRefused(
                    "raw_eml_structure_unsupported", mixed([body(f"text/plain; charset=utf-8; filename={value}")])
                )
                self.assertRefused(
                    "raw_eml_structure_unsupported", mixed([body(disposition=f"inline; name={value}")])
                )

    def test_rfc_2231_forms_in_the_wrong_header_are_refused_for_text(self) -> None:  # F1
        self.assertRefused(
            "raw_eml_structure_unsupported", top_text("text/plain; charset=utf-8; filename*=utf-8''x.txt")
        )
        self.assertRefused(
            "raw_eml_structure_unsupported",
            top_text("text/plain; charset=utf-8", "Content-Disposition: inline; name*=utf-8''x.txt"),
        )
        self.assertRefused("raw_eml_structure_unsupported", mixed([body("text/plain; charset=utf-8; filename*0=a; filename*1=b")]))

    def test_text_with_an_attachment_disposition_is_refused(self) -> None:  # F1
        for disposition in ("attachment", "attachment; filename=\"a.txt\"", "ATTACHMENT", "attachment; size=3"):
            with self.subTest(disposition):
                self.assertRefused(
                    "raw_eml_structure_unsupported",
                    top_text("text/plain; charset=utf-8", f"Content-Disposition: {disposition}"),
                )
                self.assertRefused("raw_eml_structure_unsupported", mixed([body(disposition=disposition)]))

    def test_the_reviewer_reproductions_for_attachments_are_refused(self) -> None:  # F1
        cases = {
            "path filename in Content-Type": part(
                content_type='application/pdf; filename="../x"', disposition='attachment; filename="a.pdf"'),
            "safe equal filename in Content-Type": part(
                content_type='application/pdf; filename="a.pdf"', disposition='attachment; filename="a.pdf"'),
            "path name in Content-Disposition": part(disposition='attachment; filename="a.pdf"; name="../x"'),
            "different name in Content-Disposition": part(disposition='attachment; filename="a.pdf"; name="b.pdf"'),
            "safe equal name in Content-Disposition": part(disposition='attachment; filename="a.pdf"; name="a.pdf"'),
            "extended filename in Content-Type": part(
                content_type="application/pdf; filename*=utf-8''a.pdf", disposition='attachment; filename="a.pdf"'),
            "extended name in Content-Disposition": part(disposition="attachment; filename=\"a.pdf\"; name*=utf-8''a.pdf"),
            "filename only in the wrong header": part(content_type='application/pdf; filename="a.pdf"', disposition="attachment"),
        }
        for label, one in cases.items():
            with self.subTest(label):
                self.assertRefused("raw_eml_attachment_invalid", mixed([BODY_PART, one]))

    def test_canonical_positions_with_equal_safe_names_are_still_accepted(self) -> None:  # F1
        for content_type in ("application/pdf", 'application/pdf; name="a.pdf"'):
            with self.subTest(content_type):
                prepared = dre.validate_raw_eml(mixed([BODY_PART, part(content_type=content_type)]))
                self.assertEqual("a.pdf", prepared.attachments[0].filename)

    def test_multipart_containers_reject_name_and_filename_entirely(self) -> None:  # F1
        cases = {
            "filename in disposition": ("multipart/mixed; boundary=BOUND", 'Content-Disposition: attachment; filename="../x"'),
            "safe filename in disposition": ("multipart/mixed; boundary=BOUND", 'Content-Disposition: inline; filename="a.txt"'),
            "name in disposition": ("multipart/mixed; boundary=BOUND", 'Content-Disposition: inline; name="a.txt"'),
            "name in Content-Type": ('multipart/mixed; boundary=BOUND; name="a.txt"', None),
            "filename in Content-Type": ('multipart/mixed; boundary=BOUND; filename="a.txt"', None),
            "extended name in Content-Type": ("multipart/mixed; boundary=BOUND; name*=utf-8''a.txt", None),
        }
        for label, (content_type, extra) in cases.items():
            with self.subTest(label):
                lines = [*headers(), "MIME-Version: 1.0", f"Content-Type: {content_type}"]
                if extra:
                    lines.append(extra)
                lines += ["", "--BOUND", *BODY_PART, "--BOUND--"]
                self.assertRefused("raw_eml_structure_unsupported", join_lines(lines))

    def test_a_plain_container_without_file_name_parameters_is_accepted(self) -> None:  # F1
        lines = [*headers(), "MIME-Version: 1.0", "Content-Type: multipart/mixed; boundary=BOUND", "Content-Disposition: inline",
                 "", "--BOUND", *BODY_PART, "--BOUND--"]
        self.assertEqual("mixed", dre.validate_raw_eml(join_lines(lines)).summary["structure"])

    def test_error_texts_carry_no_parameter_values(self) -> None:  # F1
        exc = self.assertRefused("raw_eml_structure_unsupported", top_text(f'text/plain; charset=utf-8; filename="{SECRET}"'))
        self.assertNotIn(SECRET, str(exc))
        exc = self.assertRefused(
            "raw_eml_attachment_invalid",
            mixed([BODY_PART, part(content_type=f'application/pdf; filename="{SECRET}"')]),
        )
        self.assertNotIn(SECRET, str(exc))


def _stdlib_has_empty_group_bug() -> bool:
    from email.policy import SMTPUTF8

    try:
        SMTPUTF8.header_factory("To", "Group:; , a@example.org")
    except AttributeError:
        return True
    except Exception:  # noqa: BLE001
        return False
    return False


class EmptyGroupCfwsTests(_Base):  # final-round finding 2
    """Python 3.12 ``email.headerregistry`` raises ``AttributeError`` for an empty group followed by CFWS.

    The profile does not patch the stdlib parser: such lists stay refused (fail closed) and the limitation
    is documented.  Every other group shape keeps working.
    """

    def test_an_empty_group_followed_by_cfws_fails_closed(self) -> None:  # F2
        if not _stdlib_has_empty_group_bug():
            self.skipTest("this interpreter's email.headerregistry parses empty groups followed by CFWS")
        for value in ("Group:; , a@example.org", "a@example.org, Group:; , b@example.org", "Group:; (c), a@example.org",
                      "Group:;(c), a@example.org", "Group:; , Other:;"):
            with self.subTest(value):
                exc = self.assertRefused("raw_eml_address_invalid", plain(to=value))
                self.assertNotIn("AttributeError", str(exc))

    def test_other_group_shapes_keep_working(self) -> None:  # F2
        good = ["Group:;", "Group:;, a@example.org", "Group: ;, a@example.org", "a@example.org, Group:;", "Group:;,Other:;",
                "Group: a@example.org; , b@example.org", "Group: a@example.org; (c), b@example.org",
                "undisclosed-recipients:;", "Team: a@example.org, b@example.org;"]
        for value in good:
            with self.subTest(value):
                data = join_lines([*headers(to=value, extra=["MIME-Version: 1.0", "Content-Type: text/plain; charset=utf-8", "Content-Transfer-Encoding: 8bit"]), "", "ok"])
                self.assertEqual(value, dre.validate_raw_eml(data).summary["headers"][0][1])

    def test_a_trailing_comma_or_text_after_an_empty_group_stays_refused(self) -> None:  # F2
        for value in ("Group:;,", "Group:;, ,a@example.org", "Group:; a@example.org", "Group:;;"):
            with self.subTest(value):
                self.assertRefused("raw_eml_address_invalid", plain(to=value))


class UnicodeWhitespaceContextTests(_Base):  # final-round finding 3
    """Unicode whitespace: allowed inside comments and quoted display names, refused everywhere else."""

    MIME = ["MIME-Version: 1.0", "Content-Type: text/plain; charset=utf-8", "Content-Transfer-Encoding: 8bit"]

    def to_message(self, value: str) -> bytes:
        return join_lines([*headers(to=value, extra=self.MIME), "", "ok"])

    def test_unicode_whitespace_inside_to_comments_is_accepted(self) -> None:  # F3
        for char in (" ", " ", " ", "　", " "):
            for value in (f"a@example.org (c{char}x)", f"Name <a@example.org> (c{char}x)", f"a@example.org (a (b{char}c) d)",
                          f"a@example.org (c\\{char}x)", f"(c{char}x) a@example.org"):
                with self.subTest(repr(char), value=value):
                    self.assertEqual(value, dre.validate_raw_eml(self.to_message(value)).summary["headers"][0][1])

    def test_unicode_whitespace_inside_quoted_display_names_is_accepted(self) -> None:  # F3
        for char in (" ", " ", "　"):
            value = f'"Doe{char}John" <a@example.org>'
            with self.subTest(repr(char)):
                self.assertEqual(value, dre.validate_raw_eml(self.to_message(value)).summary["headers"][0][1])

    def test_unicode_whitespace_outside_comments_and_quotes_is_refused(self) -> None:  # F3
        for char in (" ", " ", " ", "　"):
            for value in (f"a@example.org,{char}b@example.org", f"{char}a@example.org", f"a@example.org{char}",
                          f"Name{char}<a@example.org>", f"a@exa{char}mple.org", f"a@example.org{char}(comment)",
                          f"(comment){char}a@example.org,{char}b@example.org"):
                with self.subTest(repr(char), value=value):
                    self.assertRefused("raw_eml_address_invalid", plain(to=value))

    def test_unicode_whitespace_in_an_addr_spec_is_refused_even_when_quoted(self) -> None:  # F3
        for value in ('"a b"@example.org', '"a b"@example.org', 'Name <"a b"@example.org>'):
            with self.subTest(value):
                self.assertRefused("raw_eml_address_invalid", plain(to=value))

    def test_unicode_whitespace_inside_the_date_comment_is_accepted(self) -> None:  # F3
        for char in (" ", " ", " ", "　"):
            value = f"Sat, 07 Nov 2026 12:00:00 +0100 (CET{char}x)"
            with self.subTest(repr(char)):
                self.assertEqual(value, dre.validate_raw_eml(plain(date=value)).date)

    def test_unicode_whitespace_in_the_date_outside_the_comment_is_refused(self) -> None:  # F3
        for char in (" ", " ", " ", "　"):
            for value in (f"Sat, 07 Nov 2026 12:00:00 +0100{char}(CET)", f"Sat, 07 Nov 2026{char}12:00:00 +0100",
                          f"Sat, 07 Nov 2026 12:00:00 +0100 (CET){char}", f"{char}Sat, 07 Nov 2026 12:00:00 +0100",
                          f"Sat, 07 Nov 2026 12:00:00 +0100 (CET) {char}"):
                with self.subTest(repr(char), value=value):
                    self.assertRefused("raw_eml_date_invalid", plain(date=value))

    def test_a_nested_or_unclosed_date_comment_stays_refused(self) -> None:  # F3
        for value in ("Sat, 07 Nov 2026 12:00:00 +0100 (a (b))", "Sat, 07 Nov 2026 12:00:00 +0100 (a", "Sat, 07 Nov 2026 12:00:00 +0100 (a) (b)"):
            with self.subTest(value):
                self.assertRefused("raw_eml_date_invalid", plain(date=value))

    def test_comment_whitespace_stays_significant_in_the_readback(self) -> None:  # F3
        a = self.to_message("a@example.org (c x)")
        b = self.to_message("a@example.org (c x)")
        self.assertEqual(["header:to"], dre.compare_stored_raw(b, dre.validate_raw_eml(a)))


if __name__ == "__main__":
    unittest.main()
