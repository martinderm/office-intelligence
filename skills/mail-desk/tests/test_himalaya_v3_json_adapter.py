"""HV3-1 v3 JSON boundary: live array, schema object, fail-closed normalization.

Evidence mode ``tdd``, risk tier ``medium``.  Everything is hermetic: the
``himalaya`` child is answered by an in-process transport script, no mailbox,
network, credential or real configuration is involved.

Contract encoded here (binding decisions of the HV3-1 dispatch):

* the **live top-level array** of ``himalaya envelope list --json`` is the
  primary v3.0.0 shape: every entry needs ``id`` and the camelCase
  ``messageId`` key (nullable, ``string|null``); ``inReplyTo``,
  ``hasAttachment`` and the other typed camelCase fields are checked;
* the ``json-schema`` object ``{envelopes,nextPage,total,errors}`` is the second
  forward-compatible v3 shape; ``nextPage`` is followed, ``total`` validated and
  non-empty ``errors`` fail closed;
* a v1 array without ``messageId``, a snake_case-only ``message_id``, a missing
  or wrongly typed ``messageId`` and conflicting non-null duplicates are
  fail-closed under v3 (``himalaya_json_incompatible``); there is no snake_case
  fallback under v3 and the v1 fallback stays v1-only;
* the array form invents no ``nextPage``/``total``;
* normalization keeps ``id``/``subject``/``from``/``to``/``date``/``flags``/
  ``size``/``has_attachment`` and adds ``message_id``/``in_reply_to``;
* ``mailbox list`` is read from ``{mailboxes,errors}``, ``message read --json``
  maps onto the existing detail fields, and ``message read --raw --json`` is
  base64-decoded with ``max_bytes`` applied to the **decoded** size;
* the v1 list payload stays byte-identical.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
import sys
import unittest

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mail_desk_himalaya_client as client  # noqa: E402
from core import himalaya  # noqa: E402
from himalaya_v3_fixtures import (  # noqa: E402
    V1MailboxTransport,
    V3MailboxTransport,
    assert_v3_command,
    minimal_eml,
    parse_command,
    patched_himalaya,
    strip_global_prefix,
    v3_envelope,
    v3_mailbox_payload,
    v3_schema_payload,
)

INCOMPATIBLE = "himalaya_json_incompatible"

#: A complete, valid live-array entry used by several tests.
LIVE_ENVELOPE = v3_envelope(
    "41",
    message_id="Thread@Example.org",
    subject="Prüfung Unterlagen",
    from_name="Ada Lovelace",
    from_email="ada@example.org",
    to=(("Grace Hopper", "grace@example.org"),),
    date="2026-11-27T09:00:00Z",
    flags=(("Seen", "\\Seen"), ("Flagged", "\\Flagged")),
    size=2048,
    has_attachment=True,
    in_reply_to=("parent@example.org",),
)


def _payload_without(entry: dict, *keys: str) -> dict:
    """Return a copy of ``entry`` without the named keys."""
    return {key: value for key, value in entry.items() if key not in keys}


class _V3ListCase(unittest.TestCase):
    """Shared v3 fixture: one transport whose ``envelope list`` is scripted."""

    def setUp(self) -> None:
        self.transport = V3MailboxTransport()

    def list_envelopes(self, payload: object, *, page_size: int = 10):
        """Answer ``envelope list`` with ``payload`` and call the client seam."""
        self.transport.on("envelope list", lambda command: json.dumps(payload))
        with patched_himalaya(self.transport):
            return client.op_list_envelopes("INBOX", page_size)

    def expect_accepted(self, payload: object, *, page_size: int = 10):
        """Assert a valid v3 payload is accepted and return the parsed entries."""
        try:
            return self.list_envelopes(payload, page_size=page_size)
        except Exception as exc:  # noqa: BLE001 - the red must be a bounded assertion
            self.fail(f"valid v3 payload was not accepted: {type(exc).__name__}: {exc}")

    def assert_incompatible(self, payload: object) -> None:
        """Assert the v3 boundary rejects ``payload`` with the bounded reason."""
        try:
            result = self.list_envelopes(payload)
        except himalaya.HimalayaInvocationError as exc:
            self.assertEqual(INCOMPATIBLE, exc.reason_code)
            return
        except Exception as exc:  # noqa: BLE001 - never let a raw parse error mask the missing guard
            self.fail(
                "the v3 boundary raised "
                f"{type(exc).__name__} instead of a bounded fail-closed error: {exc}"
            )
        self.fail(f"the v3 boundary accepted an incompatible payload and returned {result!r}")


class V3LiveArrayTests(_V3ListCase):
    """The live top-level array is accepted and normalized to the v1 shape."""

    def test_live_array_is_accepted_and_normalized(self) -> None:
        result = self.expect_accepted([LIVE_ENVELOPE])
        self.assertEqual(1, len(result))
        entry = result[0]
        self.assertEqual("41", entry["id"])
        self.assertEqual("Prüfung Unterlagen", entry["subject"])
        self.assertEqual({"name": "Ada Lovelace", "addr": "ada@example.org"}, entry["from"])
        self.assertEqual({"name": "Grace Hopper", "addr": "grace@example.org"}, entry["to"])
        self.assertEqual("2026-11-27T09:00:00Z", entry["date"])
        self.assertEqual(["Seen", "Flagged"], entry["flags"])
        self.assertEqual(2048, entry["size"])
        self.assertIs(True, entry["has_attachment"])
        self.assertEqual("thread@example.org", entry["message_id"])
        self.assertIsInstance(entry["in_reply_to"], str)
        self.assertIn("parent@example.org", entry["in_reply_to"])

        calls = [command for command in self.transport.mailbox_calls if "envelope" in command]
        self.assertEqual(1, len(calls))
        assert_v3_command(
            self, calls[0], ["envelope", "list", "-m", "INBOX", "-s", "10"], require_json=True
        )

    def test_null_message_id_is_accepted_without_adding_the_field(self) -> None:
        result = self.expect_accepted([v3_envelope("41", message_id=None, subject="Ohne ID")])
        self.assertEqual("Ohne ID", result[0]["subject"])
        self.assertNotIn("message_id", result[0])

    def test_missing_message_id_key_fails_closed(self) -> None:
        self.assert_incompatible([_payload_without(LIVE_ENVELOPE, "messageId")])

    def test_snake_case_only_message_id_fails_closed(self) -> None:
        entry = _payload_without(LIVE_ENVELOPE, "messageId")
        entry["message_id"] = "thread@example.org"
        self.assert_incompatible([entry])

    def test_v1_array_without_message_id_fails_closed(self) -> None:
        self.assert_incompatible(
            [
                {
                    "id": "41",
                    "subject": "Alt",
                    "from": {"name": "Ada Lovelace", "addr": "ada@example.org"},
                    "date": "2026-11-27T09:00:00Z",
                    "flags": ["seen"],
                }
            ]
        )

    def test_wrong_message_id_or_in_reply_to_types_fail_closed(self) -> None:
        cases = {
            "messageId as integer": {**LIVE_ENVELOPE, "messageId": 4711},
            "inReplyTo as string": {**LIVE_ENVELOPE, "inReplyTo": "parent@example.org"},
            "from as string": {**LIVE_ENVELOPE, "from": "Ada Lovelace <ada@example.org>"},
            "flags as string": {**LIVE_ENVELOPE, "flags": "Seen"},
            "hasAttachment as string": {**LIVE_ENVELOPE, "hasAttachment": "yes"},
        }
        for label, entry in cases.items():
            with self.subTest(label):
                self.assert_incompatible([entry])

    def test_conflicting_duplicate_message_id_fails_closed(self) -> None:
        first = v3_envelope("41", message_id="a@example.org", subject="Alt")
        second = v3_envelope("41", message_id="b@example.org", subject="Alt")
        self.assert_incompatible([first, second])

    def test_array_form_invents_no_next_page_or_total(self) -> None:
        result = self.expect_accepted(
            [v3_envelope("41", message_id="a@example.org"), v3_envelope("42", message_id="b@example.org")]
        )
        self.assertEqual(["41", "42"], [entry["id"] for entry in result])
        self.assertNotIn("nextPage", result)
        self.assertNotIn("total", result)
        calls = [command for command in self.transport.mailbox_calls if "envelope" in command]
        self.assertEqual(1, len(calls), "the array form carries no page token and needs exactly one call")
        self.assertIsNone(parse_command(calls[0]).option("--page-token"))

    def test_empty_array_is_a_valid_empty_page(self) -> None:
        self.assertEqual([], self.list_envelopes([]))


class V3SchemaObjectTests(_V3ListCase):
    """The forward-compatible object form is validated and paged strictly."""

    def test_schema_object_is_accepted(self) -> None:
        payload = v3_schema_payload([LIVE_ENVELOPE], next_page=None, total=1)
        result = self.expect_accepted(payload)
        self.assertEqual(["41"], [entry["id"] for entry in result])
        self.assertEqual("thread@example.org", result[0]["message_id"])

    def test_missing_schema_keys_fail_closed(self) -> None:
        complete = v3_schema_payload([LIVE_ENVELOPE], next_page=None, total=1)
        for missing in ("envelopes", "nextPage", "total", "errors"):
            with self.subTest(missing=missing):
                self.assert_incompatible(_payload_without(complete, missing))

    def test_non_uint_total_fails_closed(self) -> None:
        for total in (-1, "1", 1.5, True):
            with self.subTest(total=total):
                self.assert_incompatible(v3_schema_payload([LIVE_ENVELOPE], next_page=None, total=total))

    def test_non_empty_errors_fail_closed_even_with_readable_entries(self) -> None:
        payload = v3_schema_payload(
            [LIVE_ENVELOPE], next_page=None, total=1, errors=[{"kind": "search-invalid"}]
        )
        self.assert_incompatible(payload)

    def test_next_page_is_followed_with_the_page_token(self) -> None:
        self.transport.schema_pages = {
            None: v3_schema_payload([v3_envelope("41", message_id="a@example.org")], next_page="tok-2", total=2),
            "tok-2": v3_schema_payload([v3_envelope("42", message_id="b@example.org")], next_page=None, total=2),
        }
        with patched_himalaya(self.transport):
            try:
                result = client.op_list_envelopes("INBOX", 1)
            except Exception as exc:  # noqa: BLE001 - the red must be a bounded assertion
                self.fail(f"paged v3 listing failed: {type(exc).__name__}: {exc}")
        self.assertEqual(["41", "42"], [entry["id"] for entry in result])
        calls = [command for command in self.transport.mailbox_calls if "envelope" in command]
        self.assertEqual(2, len(calls))
        self.assertIsNone(parse_command(calls[0]).option("--page-token"))
        self.assertEqual("tok-2", parse_command(calls[1]).option("--page-token"))

    def test_non_progressing_page_token_fails_closed(self) -> None:
        requests = {"count": 0}

        def responder(command) -> str:
            requests["count"] += 1
            if requests["count"] > 150:
                raise AssertionError("v3 paging did not terminate and has no bounded page limit")
            return json.dumps(
                v3_schema_payload([v3_envelope("41", message_id="a@example.org")], next_page="tok-2", total=None)
            )

        self.transport.on("envelope list", responder)
        with patched_himalaya(self.transport):
            try:
                client.op_list_envelopes("INBOX", 1)
            except himalaya.HimalayaInvocationError as exc:
                self.assertTrue(exc.reason_code.startswith("himalaya_"))
                return
            except Exception as exc:  # noqa: BLE001 - the red must be a bounded assertion
                self.fail(
                    "non-progressing paging raised "
                    f"{type(exc).__name__} instead of a bounded fail-closed error: {exc}"
                )
        self.fail("non-progressing paging returned results instead of failing closed")

    def test_invalid_page_token_fails_closed(self) -> None:
        self.transport.schema_pages = {
            None: v3_schema_payload(
                [v3_envelope("41", message_id="a@example.org")], next_page="tok-missing", total=2
            ),
            # "tok-missing" is absent: the fixture answers a page-token-invalid error.
        }
        with patched_himalaya(self.transport):
            try:
                client.op_list_envelopes("INBOX", 1)
            except himalaya.HimalayaInvocationError as exc:
                self.assertTrue(exc.reason_code.startswith("himalaya_"))
                return
            except Exception as exc:  # noqa: BLE001 - the red must be a bounded assertion
                self.fail(
                    "invalid page token raised "
                    f"{type(exc).__name__} instead of a bounded fail-closed error: {exc}"
                )
        self.fail("an invalid page token returned results instead of failing closed")


class V3MailboxListTests(unittest.TestCase):
    """``mailbox list --json`` is read from ``{mailboxes,errors}``."""

    def test_mailbox_list_object_is_accepted(self) -> None:
        transport = V3MailboxTransport()
        with patched_himalaya(transport):
            mailboxes = client.op_list_folders()
        self.assertEqual(["INBOX", "Drafts", "Archive"], [item["name"] for item in mailboxes])
        self.assertEqual("drafts", mailboxes[1]["role"])
        assert_v3_command(self, transport.mailbox_calls[0], ["mailbox", "list"], require_json=True)

    def test_missing_mailbox_envelope_fails_closed(self) -> None:
        transport = V3MailboxTransport()
        transport.on("mailbox list", lambda command: json.dumps({"errors": []}))
        with patched_himalaya(transport):
            with self.assertRaises(himalaya.HimalayaInvocationError) as raised:
                client.op_list_folders()
        self.assertEqual(INCOMPATIBLE, raised.exception.reason_code)

    def test_v1_bare_folder_array_stays_accepted(self) -> None:
        transport = V1MailboxTransport()
        with patched_himalaya(transport):
            mailboxes = client.op_list_folders()
        self.assertEqual(
            [{"name": "INBOX"}, {"name": "Drafts"}, {"name": "Archive"}],
            mailboxes,
        )


class V3MessageReadTests(unittest.TestCase):
    """``message read`` view and raw payloads map onto the existing detail seam."""

    def setUp(self) -> None:
        self.transport = V3MailboxTransport()
        self.transport.add_envelope(
            "INBOX",
            v3_envelope("41", message_id="old@example.org", subject="Alt"),
        )

    def test_view_maps_onto_the_existing_detail_fields(self) -> None:
        with patched_himalaya(self.transport):
            result = himalaya.get_single_email_details(
                "41", folder="INBOX", preview_lines=2, inspect_attachments=False
            )
        self.assertEqual("41", result["envelope_id"])
        self.assertEqual("INBOX", result["folder"])
        self.assertEqual("old@example.org", result["message_id"])
        self.assertEqual("Alt", result["subject"])
        self.assertIn("ada@example.org", result["from"])
        self.assertIn("grace@example.org", result["to"])
        self.assertEqual("2026-11-27T09:00:00Z", result["date"])
        self.assertEqual("preview", result["read_level"])
        self.assertIsNone(result["error"])
        self.assertLessEqual(len(result["preview"].splitlines()), 2)
        self.assertEqual([], result["attachments"])

        calls = [command for command in self.transport.mailbox_calls if "message" in command]
        self.assertEqual(1, len(calls))
        assert_v3_command(self, calls[0], ["message", "read", "41", "-m", "INBOX"], require_json=True)

    def test_raw_base64_is_decoded_with_max_bytes_on_the_decoded_size(self) -> None:
        eml = minimal_eml("raw@example.org", subject="Roh")
        self.transport.register_eml("41", eml)
        with patched_himalaya(self.transport):
            data = himalaya.fetch_raw_message_eml("41", folder="INBOX", max_bytes=len(eml))
        self.assertEqual(eml, data)

        calls = [command for command in self.transport.mailbox_calls if "message" in command]
        assert_v3_command(self, calls[-1], ["message", "read", "41", "-m", "INBOX", "--raw"], require_json=True)
        self.assertGreater(len(base64.b64encode(eml)), len(eml) - 1, "base64 is longer than the decoded size")

        with patched_himalaya(self.transport):
            with self.assertRaises(himalaya.HimalayaInvocationError) as raised:
                himalaya.fetch_raw_message_eml("41", folder="INBOX", max_bytes=len(eml) - 1)
        self.assertEqual("himalaya_export_too_large", raised.exception.reason_code)


class V1EnvelopeParityTests(unittest.TestCase):
    """The 1.2.0 array stays byte-identical at the documented list seam."""

    def test_v1_envelope_payload_is_byte_identical(self) -> None:
        entries = [
            {
                "id": "4711",
                "subject": "Prüfung Unterlagen",
                "from": {"name": "Ada Lovelace", "addr": "ada@example.org"},
                "date": "2026-11-27T09:00:00Z",
                "flags": ["seen"],
            }
        ]
        transport = V1MailboxTransport(envelopes={"INBOX": entries})
        with patched_himalaya(transport):
            result = client.op_list_envelopes("INBOX", 10)
        self.assertEqual(json.dumps(entries), json.dumps(result))
        self.assertEqual(
            ["-o", "json", "envelope", "list", "-f", "INBOX", "-s", "10"],
            strip_global_prefix(transport.mailbox_calls[0]),
        )


if __name__ == "__main__":  # pragma: no cover - manual invocation only
    unittest.main()
