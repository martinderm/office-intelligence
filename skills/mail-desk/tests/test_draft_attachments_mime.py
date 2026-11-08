"""Draft attachments (#18) — MIME building, header / Message-ID validation and the 10 MiB message bound.

Evidence mode ``tdd``.  Matrix rows L4-L6, H1-H2, B1-B3.

Seam note: ``core.drafts.build_draft_message`` is the public builder every save/replace path uses.  It
catches wrong MIME structure, header injection, bad Message-IDs and an over-large serialized message
before any mailbox command; the APPEND transport and the mailbox are beyond its boundary (save suite).
Expected values are literals (golden pre-change output, RFC 4648 Base64 of ``abc``, 76-column line
arithmetic worked by hand), not recomputed with the production code.
"""

from __future__ import annotations

import email
import email.policy
import sys
import unittest
from pathlib import Path

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import draft_attachments as da  # noqa: E402
from core import drafts  # noqa: E402
from draft_attachment_fixtures import exact_size_body  # noqa: E402

TEN_MIB = 10485760
THREE_MIB = 3145728

#: Output of the builder BEFORE attachments existed (captured on the base commit).
GOLDEN_NO_ATTACHMENT_RAW = (
    "From: me@example.org\r\nTo: Empfänger <a@example.org>\r\nCc: cc@example.org\r\n"
    "Subject: Prüfung – Kärnten\r\nDate: Sat, 07 Nov 2026 12:00:00 +0100\r\n"
    "Message-ID: <golden@example.org>\r\nContent-Type: text/plain; charset=\"utf-8\"\r\n"
    "Content-Transfer-Encoding: base64\r\nMIME-Version: 1.0\r\n\r\nR3LDvMOfZQpaZWlsZSB6d2VpCg==\r\n"
)


def att(name: str, data: bytes, content_type: str = "application/octet-stream") -> da.DraftAttachment:
    import hashlib

    return da.DraftAttachment(name, content_type, data, len(data), hashlib.sha256(data).hexdigest())


def build(attachments=(), body="Text", **kw):
    kw.setdefault("date", "Sat, 07 Nov 2026 12:00:00 +0100")
    kw.setdefault("message_id", "<mime@example.org>")
    return drafts.build_draft_message("Betreff", "a@example.org", body, attachments=attachments, **kw)


class NoRegressionTests(unittest.TestCase):
    def test_without_attachments_the_output_is_byte_identical_to_the_old_builder(self) -> None:  # B1
        raw, message_id, date = drafts.build_draft_message(
            "Prüfung – Kärnten",
            "Empfänger <a@example.org>",
            "Grüße\nZeile zwei",
            from_addr="me@example.org",
            cc="cc@example.org",
            date="Sat, 07 Nov 2026 12:00:00 +0100",
            message_id="<golden@example.org>",
        )
        self.assertEqual(GOLDEN_NO_ATTACHMENT_RAW, raw)
        self.assertEqual("golden@example.org", message_id)
        self.assertEqual("Sat, 07 Nov 2026 12:00:00 +0100", date)


class MimeStructureTests(unittest.TestCase):
    def test_binary_and_unicode_attachments_roundtrip_exactly(self) -> None:  # B2, B3
        every_byte = bytes(range(256)) * 3
        crlf_mix = b"line1\r\nline2\nline3\rEND\x00\xff"
        raw, _, _ = build(
            [att("Prüfung Ärger – 🎯.pdf", every_byte, "application/pdf"), att("mix.bin", crlf_mix)],
            body="Grüße – 100 %\nZeile",
        )
        self.assertNotIn("\n", raw.replace("\r\n", ""), "CRLF only")
        self.assertNotIn("\r", raw.replace("\r\n", ""), "no lone CR")
        parsed = email.message_from_bytes(raw.encode("utf-8"), policy=email.policy.SMTPUTF8)
        self.assertEqual("multipart/mixed", parsed.get_content_type())
        parts = list(parsed.iter_parts())
        self.assertEqual(["text/plain", "application/pdf", "application/octet-stream"], [p.get_content_type() for p in parts])
        self.assertEqual("Grüße – 100 %\nZeile\n", parts[0].get_content().replace("\r\n", "\n"))
        names = [p.get_filename() for p in list(parsed.iter_attachments())]
        self.assertEqual(["Prüfung Ärger – 🎯.pdf", "mix.bin"], names)
        decoded = [p.get_payload(decode=True) for p in parsed.iter_attachments()]
        self.assertEqual([every_byte, crlf_mix], decoded)
        for part in parsed.iter_attachments():
            self.assertEqual("attachment", part.get_content_disposition())
            self.assertEqual("base64", part["Content-Transfer-Encoding"])
        self.assertNotIn("text/html", raw)
        self.assertNotIn("Content-ID", raw)
        # The unicode file name travels as raw UTF-8 (SMTPUTF8), like every other header.
        self.assertIn('filename="Prüfung Ärger – 🎯.pdf"', raw)

    def test_base64_is_rfc4648_with_76_column_lines(self) -> None:  # B2
        raw, _, _ = build([att("a.bin", b"abc"), att("b.bin", bytes(1000))])
        self.assertIn("\r\n\r\nYWJj\r\n", raw, "RFC 4648: base64('abc') == 'YWJj'")
        parsed = email.message_from_bytes(raw.encode("utf-8"), policy=email.policy.SMTPUTF8)
        second = list(parsed.iter_attachments())[1]
        lines = [line for line in second.get_payload().replace("\r\n", "\n").split("\n") if line]
        # 1000 bytes -> 1336 base64 characters -> 17 full 76-column lines + one line of 44.
        self.assertEqual([76] * 17 + [44], [len(line) for line in lines])

    def test_text_part_comes_first_and_only_one_plain_body_exists(self) -> None:  # B3
        raw, _, _ = build([att("a.txt", b"x", "text/plain")], body="Nur Text")
        parsed = email.message_from_bytes(raw.encode("utf-8"), policy=email.policy.SMTPUTF8)
        first = next(parsed.iter_parts())
        self.assertEqual("text/plain", first.get_content_type())
        self.assertIsNone(first.get_filename())
        self.assertEqual(1, len(list(parsed.iter_attachments())))


class SizeBoundTests(unittest.TestCase):
    """The bound is the FULL serialized message in bytes (inclusive 10 MiB)."""

    @staticmethod
    def body_for_size(target: int, attachments) -> str:
        return exact_size_body(target, attachments, message_id="<mime@example.org>")

    def test_serialized_message_of_exactly_ten_mib_is_accepted_and_one_more_byte_rejected(self) -> None:  # L4
        files = [att("a.bin", bytes(range(256)) * 12288), att("b.bin", bytes(range(256)) * 12288)]
        self.assertEqual(THREE_MIB, files[0].size)
        exact = self.body_for_size(TEN_MIB, files)
        raw, _, _ = build(files, body=exact)
        self.assertEqual(10485760, len(raw.encode("utf-8")))
        over = self.body_for_size(TEN_MIB + 1, files)
        with self.assertRaises(da.DraftAttachmentError) as raised:
            build(files, body=over)
        self.assertEqual("message_too_large", raised.exception.reason_code)
        self.assertNotIn("aaaaaaaaaa", str(raised.exception))

    def test_file_sum_below_ten_mib_is_still_rejected_because_of_base64_overhead(self) -> None:  # L5
        files = [att(f"f{n}.bin", bytes(range(256)) * 12288) for n in range(3)]
        self.assertEqual(9 * 1048576, sum(f.size for f in files), "originals total 9 MiB, below the bound")
        with self.assertRaises(da.DraftAttachmentError) as raised:
            build(files)
        self.assertEqual("message_too_large", raised.exception.reason_code)

    def test_the_bound_applies_with_zero_attachments_too(self) -> None:  # L4 (review R1 / 4)
        exact = exact_size_body(TEN_MIB, (), message_id="<mime@example.org>")
        raw, _, _ = build((), body=exact)
        self.assertEqual(10485760, len(raw.encode("utf-8")))
        over = exact_size_body(TEN_MIB + 1, (), message_id="<mime@example.org>")
        for attachments in ((), []):
            with self.subTest(attachments=attachments):
                with self.assertRaises(da.DraftAttachmentError) as raised:
                    build(attachments, body=over)
                self.assertEqual("message_too_large", raised.exception.reason_code)

    def test_stdin_transport_crlf_is_not_counted_as_mail_content(self) -> None:  # L6
        self.assertEqual(b"\r\n", drafts._STDIN_TRANSPORT_TERMINATOR)
        files = [att("a.bin", bytes(range(256)) * 12288), att("b.bin", bytes(range(256)) * 12288)]
        raw, _, _ = build(files, body=self.body_for_size(TEN_MIB, files))
        payload = raw.encode("utf-8") + drafts._STDIN_TRANSPORT_TERMINATOR
        self.assertEqual(TEN_MIB + 2, len(payload))
        self.assertLessEqual(len(raw.encode("utf-8")), da.MAX_MIME_MESSAGE_BYTES)


class HeaderValidationTests(unittest.TestCase):
    BAD_VALUES = [
        "x\r\nBcc: evil@example.org", "x\nBcc: evil@example.org", "x\rBcc: e", "nul\x00", "del\x7f", "c1\x85", "sep x",
        "para x", "tab\tx", "lone\ud800",
    ]

    def test_header_injection_and_control_characters_are_rejected(self) -> None:  # H1
        for field in ("subject", "to", "cc", "from_addr", "date"):
            for value in self.BAD_VALUES:
                with self.subTest(field=field, value=ascii(value)):
                    args = {"subject": "S", "to": "a@example.org"}
                    kwargs: dict = {"message_id": "<ok@example.org>", "date": "Sat, 07 Nov 2026 12:00:00 +0100"}
                    if field in ("subject", "to"):
                        args[field] = value
                    else:
                        kwargs[field] = value
                    with self.assertRaises(drafts.DraftError) as raised:
                        drafts.build_draft_message(args["subject"], args["to"], "Text", **kwargs)
                    self.assertEqual("draft_header_invalid", raised.exception.reason_code)
                    self.assertNotIn("evil", str(raised.exception))

    def test_unicode_headers_without_control_characters_stay_valid(self) -> None:  # H1
        raw, _, _ = drafts.build_draft_message("Prüfung – 🎯", "Öztürk <a@example.org>", "Text", cc="Ä <c@example.org>")
        self.assertIn("Subject: Prüfung – 🎯", raw)

    def test_message_id_is_validated_strictly(self) -> None:  # H2
        for ok in ("<a.b-c_d+e@example.org>", "<x@host>", "bare@example.org", "<Mixed.Case@Example.ORG>"):
            with self.subTest(ok=ok):
                _, normalized, _ = drafts.build_draft_message("S", "a@example.org", "T", message_id=ok)
                self.assertEqual(ok.strip("<>").lower(), normalized)
        for bad in ("", "<>", "x", "<a b@x.org>", "<a@@x.org>", "<a@x.org><b@x.org>", "<a@x.org>\r\nBcc: e", "<@x.org>",
                    "<a@>", "<a@x.org", "a@x.org>", "<ä@x.org>", "<a@x.org>x", "<" + "a" * 250 + "@x.org>", "<a(b)@x.org>"):
            with self.subTest(bad=ascii(bad)):
                with self.assertRaises(drafts.DraftError) as raised:
                    drafts.build_draft_message("S", "a@example.org", "T", message_id=bad)
                self.assertEqual("draft_message_id_invalid", raised.exception.reason_code)

    def test_generated_message_id_is_valid(self) -> None:  # H2
        raw, normalized, _ = drafts.build_draft_message("S", "a@example.org", "T")
        self.assertRegex(normalized, r"^[^@\s]+@[^@\s]+$")
        self.assertIn(f"Message-ID: <{normalized}>".lower(), raw.lower())


if __name__ == "__main__":
    unittest.main()
