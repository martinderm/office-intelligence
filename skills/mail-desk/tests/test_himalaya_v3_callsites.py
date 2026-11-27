"""HV3-1 call sites: the v3 command map, the expunge limit and the search allow-list.

Evidence mode ``tdd``, risk tier ``medium``.  Every test is hermetic: the
``himalaya`` child is answered by an in-process transport script, no mailbox,
network, credential or real configuration is involved.

Contract encoded here (binding decisions of the HV3-1 dispatch):

* every productive call site emits the documented v3 argv: ``mailbox list``,
  ``envelope list/search`` with ``-m``/``--json``, ``message read --json`` and
  ``--raw --json`` (base64), ``message add --mailbox <f> --flag draft`` with the
  payload on stdin (no ``-o plain``), ``message copy -f <src> -t <tgt> <id>``,
  ``message delete -m <f> <id>``;
* the global ``-a`` sits after ``-c`` and before the subcommand;
* v3 has no in-place expunge: ``delete_draft`` and the delete step of
  ``replace_draft`` fail closed with ``himalaya_expunge_unsupported`` before any
  write and never degrade to ``message delete``/move-to-Trash;
* v3 has no ``envelope thread``; the skip map is derived from the listing and a
  malformed/error/paging failure only disables the accelerator (``{}``);
* search translates only the documented existing forms (``subject <q>``,
  ``date <D>``, ``order by date asc|desc``); an unsupported query fails closed
  with ``himalaya_search_query_unsupported`` instead of being silently dropped;
* the representative v1 seams stay byte-identical.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mail_desk_batch_runner as batch_runner  # noqa: E402
import mail_desk_himalaya_client as client  # noqa: E402
from core import drafts  # noqa: E402
from core import himalaya  # noqa: E402
from core import known_skip  # noqa: E402
from core.common import normalize_message_id  # noqa: E402
from himalaya_v3_fixtures import (  # noqa: E402
    V1MailboxTransport,
    V3MailboxTransport,
    V3_VERSION_OUTPUT,
    HimalayaTransport,
    UNSUPPORTED_VERSION_OUTPUT,
    assert_no_v1_tokens,
    assert_v3_command,
    minimal_eml,
    parse_command,
    patched_himalaya,
    strip_global_prefix,
    v3_envelope,
    v3_message_view,
)


def mailbox_commands(transport) -> list[list[str]]:
    """Every recorded non-version argv of one transport."""
    return list(transport.mailbox_calls)


class V3ListSeamsTests(unittest.TestCase):
    """Envelope list/search and the oldest-first listing."""

    def test_envelope_list_uses_the_mailbox_flag_and_json(self) -> None:
        transport = V3MailboxTransport(
            envelopes={"INBOX": [v3_envelope("41", message_id="a@example.org", subject="A")]}
        )
        with patched_himalaya(transport):
            result = client.op_list_envelopes("INBOX", 10)
        self.assertEqual(["41"], [entry["id"] for entry in result])
        assert_v3_command(
            self, mailbox_commands(transport)[0], ["envelope", "list", "-m", "INBOX", "-s", "10"], require_json=True
        )

    def test_oldest_envelopes_requests_oldest_first(self) -> None:
        transport = V3MailboxTransport(
            envelopes={
                "INBOX": [
                    v3_envelope("43", message_id="c@example.org", date="2026-11-27T09:00:00Z"),
                    v3_envelope("42", message_id="b@example.org", date="2025-01-02T09:00:00Z"),
                    v3_envelope("41", message_id="a@example.org", date="2024-01-02T09:00:00Z"),
                ]
            }
        )
        with patched_himalaya(transport):
            result = batch_runner.get_oldest_envelopes("INBOX", 2)
        self.assertEqual(["41", "42"], [entry["id"] for entry in result])
        argv = mailbox_commands(transport)[0]
        self.assertIn("--sort", argv)
        self.assertIn("oldest-first", argv)
        self.assertEqual("INBOX", parse_command(argv).option("-m", "--mailbox"))
        for v1_token in ("order", "by", "asc"):
            self.assertNotIn(v1_token, argv, f"v1 ordering token survived under v3: {argv!r}")
        assert_no_v1_tokens(self, argv)

    def test_subject_query_is_translated_to_envelope_search(self) -> None:
        transport = V3MailboxTransport(
            envelopes={"INBOX": [v3_envelope("41", message_id="a@example.org", subject="Rechnung Q4")]}
        )
        with patched_himalaya(transport):
            client.op_list_envelopes("INBOX", 10, query="Rechnung Q4")
        argv = mailbox_commands(transport)[0]
        command = parse_command(argv)
        self.assertEqual("envelope search", command.name)
        self.assertIn('subject:"Rechnung Q4"', command.positionals)
        self.assertNotIn("subject", command.positionals, "the v1 positional query form must not survive")
        self.assertEqual("INBOX", command.option("-m", "--mailbox"))
        self.assertIn("--json", argv)
        assert_no_v1_tokens(self, argv)

    def test_message_id_query_is_translated_to_mid(self) -> None:
        raw_id = "Case@Example.org"
        normalized = normalize_message_id(raw_id)
        transport = V3MailboxTransport(
            envelopes={"INBOX": [v3_envelope("41", message_id=normalized, subject="Fall")]}
        )
        with patched_himalaya(transport):
            result = client.op_search(message_id=raw_id, folders=["INBOX"])
        search_calls = [argv for argv in mailbox_commands(transport) if parse_command(argv).name == "envelope search"]
        self.assertEqual(1, len(search_calls))
        command = parse_command(search_calls[0])
        self.assertEqual([f"mid:{normalized}"], command.positionals)
        self.assertEqual("INBOX", command.option("-m", "--mailbox"))
        self.assertEqual(normalized, result[0]["message_id"])

    def test_unsupported_query_fails_closed_instead_of_being_dropped(self) -> None:
        transport = V3MailboxTransport()
        with patched_himalaya(transport):
            with self.assertRaises(himalaya.HimalayaInvocationError) as raised:
                client.op_search(query="invoice 2026", folders=["INBOX"])
        self.assertEqual("himalaya_search_query_unsupported", raised.exception.reason_code)
        names = [parse_command(argv).name for argv in mailbox_commands(transport)]
        self.assertNotIn("message read", names, "no per-envelope read after the refusal")


class V3ReadSeamsTests(unittest.TestCase):
    """``message read`` view/preview and the raw base64 export replacement."""

    def setUp(self) -> None:
        self.transport = V3MailboxTransport()
        self.transport.add_envelope("INBOX", v3_envelope("41", message_id="old@example.org", subject="Alt"))
        self.transport.register_eml("41", minimal_eml("old@example.org"))

    def test_read_uses_the_json_view_and_keeps_the_preview_semantics(self) -> None:
        with patched_himalaya(self.transport):
            try:
                result = client.op_read_message("41", "INBOX", preview_lines=2)
            except Exception as exc:  # noqa: BLE001 - the red must be a bounded assertion
                self.fail(f"v3 message read failed: {type(exc).__name__}: {exc}")
        self.assertEqual("Alt", result["subject"])
        self.assertEqual("old@example.org", result["message_id"])
        self.assertEqual("preview", result["read_level"])
        self.assertLessEqual(len(result["preview"].splitlines()), 2)
        read_calls = [argv for argv in mailbox_commands(self.transport) if parse_command(argv).name == "message read"]
        self.assertTrue(read_calls)
        assert_v3_command(self, read_calls[-1], ["message", "read", "41", "-m", "INBOX"], require_json=True)

    def test_raw_export_uses_read_raw_json_instead_of_export(self) -> None:
        with patched_himalaya(self.transport):
            data = himalaya.fetch_raw_message_eml("41", folder="INBOX")
        self.assertEqual(minimal_eml("old@example.org"), data)
        argv = mailbox_commands(self.transport)[-1]
        assert_v3_command(self, argv, ["message", "read", "41", "-m", "INBOX", "--raw"], require_json=True)
        self.assertNotIn("export", argv)


class V3DraftSeamsTests(unittest.TestCase):
    """Draft append stays on stdin; the v3 expunge gap fails closed."""

    def test_save_draft_uses_message_add_with_a_stdin_payload(self) -> None:
        transport = V3MailboxTransport()
        with patched_himalaya(transport):
            envelope = drafts.save_draft(
                "Betreff",
                "grace@example.org",
                "Textkörper",
                folder="Drafts",
                account="demo",
                message_id="Draft-1@Example.org",
            )
        add_calls = [argv for argv in mailbox_commands(transport) if parse_command(argv).name == "message add"]
        self.assertEqual(1, len(add_calls))
        argv = add_calls[0]
        command = parse_command(argv)
        self.assertEqual("Drafts", command.option("--mailbox", "-m"))
        self.assertEqual(["draft"], command.options["--flag"])
        self.assertIn("--flag", argv)
        self.assertNotIn("-o", argv, "the v1 '-o plain' output flag must not survive under v3")
        self.assertNotIn("save", argv, "message save is v1-only")
        assert_no_v1_tokens(self, argv)
        payload = transport.stdin_payloads[-1]
        self.assertIn(b"Message-ID: <Draft-1@Example.org>", payload)
        self.assertEqual("draft-1@example.org", envelope["message_id"])
        self.assertEqual("Drafts", envelope["folder"])

    def test_save_payload_is_the_canonical_raw_without_the_v1_terminator(self) -> None:
        transport = V3MailboxTransport()
        date = "Thu, 27 Nov 2026 09:00:00 +0000"
        with patched_himalaya(transport):
            drafts.save_draft(
                "Betreff",
                "grace@example.org",
                "Textkörper",
                folder="Drafts",
                account="demo",
                date=date,
                message_id="Draft-1@Example.org",
            )
        add_calls = [argv for argv in mailbox_commands(transport) if parse_command(argv).name == "message add"]
        self.assertEqual(1, len(add_calls))
        argv = add_calls[0]
        self.assertNotIn("--json", argv, "message add must not request JSON for the stdin save")
        expected, _mid, _date = drafts.build_draft_message(
            "Betreff",
            "grace@example.org",
            "Textkörper",
            date=date,
            message_id="Draft-1@Example.org",
        )
        payload = transport.stdin_payloads[-1]
        self.assertEqual(
            expected.encode("utf-8"),
            payload,
            "v3 message add stores stdin as given, so the v1 CRLF compensator must be removed",
        )
        self.assertTrue(payload.endswith(b"\r\n"))
        self.assertFalse(payload.endswith(b"\r\n\r\n"), "no extra empty line from the v1 terminator")

    def test_delete_draft_fails_closed_without_any_write(self) -> None:
        transport = V3MailboxTransport()
        with patched_himalaya(transport), tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(drafts.DraftError) as raised:
                client.op_delete_draft(
                    "41", folder="Drafts", account="demo", data_dir=Path(temporary)
                )
        self.assertEqual("himalaya_expunge_unsupported", raised.exception.reason_code)
        self.assertEqual([], mailbox_commands(transport), "the refusal must precede every mailbox command")
        self.assertTrue(
            transport.version_calls,
            "the guard must resolve the real profile through the bounded --version probe",
        )

    def test_replace_draft_fails_closed_before_any_write(self) -> None:
        transport = V3MailboxTransport()
        transport.add_envelope("Drafts", v3_envelope("41", message_id="old@example.org", subject="Alt"))
        transport.register_eml("41", minimal_eml("old@example.org"))
        with patched_himalaya(transport), tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(drafts.DraftError) as raised:
                client.op_replace_draft(
                    "41",
                    "Neu",
                    "grace@example.org",
                    "Textkörper",
                    folder="Drafts",
                    account="demo",
                    data_dir=Path(temporary),
                )
        self.assertEqual("himalaya_expunge_unsupported", raised.exception.reason_code)
        write_shaped = [
            argv
            for argv in mailbox_commands(transport)
            if parse_command(argv).name in {
                "message add",
                "message copy",
                "message delete",
                "flag add",
                "folder expunge",
            }
        ]
        self.assertEqual([], write_shaped, "no append, flag, delete or expunge may start before the refusal")


class StdinTerminatorTests(unittest.TestCase):
    """The v1 CRLF compensator is declared, so v3 can remove exactly that suffix."""

    def test_a_declared_suffix_that_does_not_end_the_payload_is_refused(self) -> None:
        transport = V3MailboxTransport()
        with patched_himalaya(transport):
            with self.assertRaises(ValueError):
                himalaya.run_himalaya(
                    ["message", "save", "-o", "plain", "-f", "Drafts"],
                    stdin_bytes=b"Subject: Alt\r\n\r\nText\r\n",
                    stdin_v1_terminator=b"\n\n",
                )
        self.assertEqual([], transport.calls, "the payload must be refused before any child starts")


class V3MoveSeamsTests(unittest.TestCase):
    """Copy and delete use the v3 source/target/mailbox flags."""

    def test_copy_uses_the_source_and_target_flags(self) -> None:
        transport = V3MailboxTransport()
        transport.add_envelope(
            "INBOX", v3_envelope("41", message_id="Case@example.org", subject="Alt")
        )
        transport.register_eml("41", minimal_eml("Case@example.org"))
        with patched_himalaya(transport):
            result = client.op_copy_message("41", "INBOX", "Archive", account="demo")
        copy_calls = [argv for argv in mailbox_commands(transport) if parse_command(argv).name == "message copy"]
        self.assertEqual(1, len(copy_calls))
        assert_v3_command(self, copy_calls[0], ["message", "copy", "-f", "INBOX", "-t", "Archive", "41"])
        self.assertEqual(
            ["41"],
            parse_command(copy_calls[0]).positionals,
            "the v1 'message copy <target> <id>' positional target must not survive",
        )
        self.assertTrue(result["verified"])
        self.assertIsNotNone(result["target_envelope_id"])

    def test_delete_uses_the_mailbox_flag(self) -> None:
        transport = V3MailboxTransport()
        transport.add_envelope("INBOX", v3_envelope("41", message_id="a@example.org", subject="Alt"))
        with patched_himalaya(transport):
            result = client.op_delete_message("41", "INBOX", account="demo")
        delete_calls = [argv for argv in mailbox_commands(transport) if parse_command(argv).name == "message delete"]
        self.assertEqual(1, len(delete_calls))
        assert_v3_command(self, delete_calls[0], ["message", "delete", "-m", "INBOX", "41"])
        self.assertEqual({"folder": "INBOX", "envelope_id": "41", "deleted": True}, result)
        self.assertEqual(["41"], [entry["id"] for entry in transport.state.get("Trash", [])])


class V3ThreadMapTests(unittest.TestCase):
    """The skip accelerator is derived from the listing and never fails the scan."""

    def test_thread_map_is_derived_from_the_listing(self) -> None:
        transport = V3MailboxTransport(
            envelopes={
                "INBOX": [
                    v3_envelope(
                        "41",
                        message_id="child@example.org",
                        subject="Re: Original",
                        in_reply_to=("parent@example.org",),
                    ),
                    v3_envelope("42", message_id="parent@example.org", subject="Original"),
                ]
            }
        )
        with patched_himalaya(transport):
            mapping = known_skip.load_envelope_thread_map(
                "INBOX", account="demo", run_himalaya_fn=himalaya.run_himalaya
            )
        self.assertEqual("child@example.org", mapping["41"].message_id)
        self.assertEqual("Original", mapping["42"].subject)
        argv = mailbox_commands(transport)[0]
        command = parse_command(argv)
        self.assertEqual("envelope list", command.name)
        self.assertNotEqual("envelope thread", command.name, "v3 has no envelope thread command")
        self.assertEqual("INBOX", command.option("-m", "--mailbox"))
        self.assertEqual("500", command.option("-s", "--page-size"))
        self.assertIn("--json", argv)

    def test_thread_map_failure_only_disables_the_accelerator(self) -> None:
        def failing(args, **kwargs: object) -> str:
            raise himalaya.HimalayaInvocationError("himalaya_command_failed", "exit 3")

        cases = {
            "command failure": failing,
            "malformed json": lambda args, **kwargs: "{not json",
            "schema errors": lambda args, **kwargs: json.dumps(
                {"envelopes": [], "nextPage": None, "total": None, "errors": [{"kind": "page-token-invalid"}]}
            ),
        }
        transport = HimalayaTransport(V3_VERSION_OUTPUT)
        with patched_himalaya(transport):
            for label, runner in cases.items():
                with self.subTest(label):
                    self.assertEqual(
                        {},
                        known_skip.load_envelope_thread_map(
                            "INBOX", account="demo", run_himalaya_fn=runner
                        ),
                    )


class V3PreCommandGuardTests(unittest.TestCase):
    """The pre-write guard resolves the real profile and fails closed."""

    def test_delete_guard_fails_closed_on_an_unsupported_version(self) -> None:
        transport = HimalayaTransport(UNSUPPORTED_VERSION_OUTPUT)
        with patched_himalaya(transport), tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(himalaya.HimalayaInvocationError) as raised:
                client.op_delete_draft(
                    "41", folder="Drafts", account="demo", data_dir=Path(temporary)
                )
        self.assertEqual("himalaya_version_unsupported", raised.exception.reason_code)
        self.assertEqual([], mailbox_commands(transport))

    def test_replace_guard_fails_closed_on_an_unsupported_version(self) -> None:
        transport = HimalayaTransport(UNSUPPORTED_VERSION_OUTPUT)
        with patched_himalaya(transport), tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(himalaya.HimalayaInvocationError) as raised:
                client.op_replace_draft(
                    "41",
                    "Neu",
                    "grace@example.org",
                    "Textkörper",
                    folder="Drafts",
                    account="demo",
                    data_dir=Path(temporary),
                )
        self.assertEqual("himalaya_version_unsupported", raised.exception.reason_code)
        self.assertEqual([], mailbox_commands(transport))

    def test_missing_executable_still_fails_closed_before_any_write(self) -> None:
        transport = V3MailboxTransport()
        with patched_himalaya(transport, executable_exists=False), tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(himalaya.HimalayaInvocationError) as raised:
                client.op_delete_draft(
                    "41", folder="Drafts", account="demo", data_dir=Path(temporary)
                )
        self.assertEqual("himalaya_unavailable", raised.exception.reason_code)
        self.assertEqual([], mailbox_commands(transport))


class V3SearchRefusalTests(unittest.TestCase):
    """A refused v3 search escapes instead of degrading to an empty result."""

    def test_free_text_query_escapes_the_serial_sweep(self) -> None:
        transport = V3MailboxTransport()
        with patched_himalaya(transport):
            with self.assertRaises(himalaya.HimalayaInvocationError) as raised:
                himalaya.search_mailbox(query="invoice 2026", folders=["INBOX"], threads=1)
        self.assertEqual("himalaya_search_query_unsupported", raised.exception.reason_code)
        names = [parse_command(argv).name for argv in mailbox_commands(transport)]
        self.assertNotIn("message read", names)
        self.assertNotIn("envelope search", names)

    def test_free_text_query_escapes_the_threaded_sweep(self) -> None:
        transport = V3MailboxTransport()
        with patched_himalaya(transport):
            with self.assertRaises(himalaya.HimalayaInvocationError) as raised:
                himalaya.search_mailbox(query="invoice", folders=["INBOX"], threads=2)
        self.assertEqual("himalaya_search_query_unsupported", raised.exception.reason_code)

    def test_incompatible_v3_page_escapes_instead_of_returning_empty(self) -> None:
        transport = V3MailboxTransport()
        transport.on("envelope list", lambda command: json.dumps({"unexpected": []}))
        with patched_himalaya(transport):
            with self.assertRaises(himalaya.HimalayaInvocationError) as raised:
                himalaya.search_mailbox(message_ids=["a@example.org"], folders=["INBOX"], threads=1)
        self.assertEqual("himalaya_json_incompatible", raised.exception.reason_code)


class V3FlagCommandTests(unittest.TestCase):
    """``flag add`` uses the mandatory ``--flag`` option; ``deleted`` fails closed."""

    def test_flag_add_uses_the_flag_option_and_mailbox(self) -> None:
        transport = V3MailboxTransport()
        with patched_himalaya(transport):
            argv = himalaya.build_himalaya_command(
                ["flag", "add", "seen", "41", "-f", "Drafts"], "demo"
            )
        assert_v3_command(self, argv, ["flag", "add", "--flag", "seen", "-m", "Drafts", "41"])
        self.assertEqual([], mailbox_commands(transport))

    def test_deleted_is_not_a_v3_flag_and_fails_closed(self) -> None:
        transport = V3MailboxTransport()
        with patched_himalaya(transport):
            with self.assertRaises(himalaya.HimalayaInvocationError) as raised:
                himalaya.build_himalaya_command(
                    ["flag", "add", "deleted", "41", "-f", "Drafts"], "demo"
                )
        self.assertEqual("himalaya_flag_unsupported", raised.exception.reason_code)
        self.assertEqual([], mailbox_commands(transport))


class V3DraftsFolderTests(unittest.TestCase):
    """v3 ``mailbox list`` roles resolve the Drafts folder without an explicit one."""

    def test_v3_role_drafts_resolves_without_an_explicit_folder(self) -> None:
        transport = V3MailboxTransport()
        with patched_himalaya(transport):
            resolved = drafts.resolve_drafts_folder(account="demo")
        self.assertEqual("Drafts", resolved)
        assert_v3_command(self, mailbox_commands(transport)[0], ["mailbox", "list"], require_json=True)

    def test_an_unknown_role_never_resolves_the_folder(self) -> None:
        transport = V3MailboxTransport()
        transport.on(
            "mailbox list",
            lambda command: json.dumps(
                {
                    "mailboxes": [
                        {"id": "1", "name": "INBOX", "role": None, "total": 0, "unread": 0},
                        {"id": "2", "name": "Entwürfe", "role": "sent", "total": 0, "unread": 0},
                    ],
                    "errors": [],
                }
            ),
        )
        with patched_himalaya(transport):
            with self.assertRaises(drafts.DraftsFolderUnresolvedError):
                drafts.resolve_drafts_folder(account="demo")


class V3ReadHeaderTests(unittest.TestCase):
    """Typed v3 ``headers`` preserve cc/references in the detail fields."""

    def test_cc_and_references_survive_into_the_sent_indexer_fields(self) -> None:
        transport = V3MailboxTransport()
        transport.add_envelope("INBOX", v3_envelope("41", message_id="old@example.org", subject="Alt"))
        view = v3_message_view(
            "41",
            message_id="old@example.org",
            subject="Alt",
            cc=(("Cc Person", "cc@example.org"),),
            references=("root@example.org", "mid@example.org"),
            in_reply_to=("parent@example.org",),
        )
        transport.on("message read", lambda command: json.dumps(view))
        with patched_himalaya(transport):
            result = himalaya.get_single_email_details(
                "41", folder="INBOX", preview_lines=2, inspect_attachments=False
            )
        self.assertEqual("Cc Person <cc@example.org>", result["cc"])
        self.assertEqual(
            "<root@example.org> <mid@example.org>",
            result["references"],
            "the Sent indexer splits references on whitespace and re-brackets them",
        )
        self.assertEqual("<parent@example.org>", result["in_reply_to"])
        self.assertTrue(result["headers_complete"])
        self.assertEqual("old@example.org", result["message_id"])
        self.assertEqual("2026-11-27T09:00:00Z", result["date"])
        self.assertIn("grace@example.org", result["to"])

    def test_a_string_header_map_is_not_headers_complete(self) -> None:
        payload = {
            "id": "41",
            "messageId": "old@example.org",
            "subject": "Alt",
            "text": "Zeile\n",
            "headers": {"Message-Id": "<old@example.org>", "Subject": "Alt"},
        }
        result = himalaya._details_from_v3_view(
            "41",
            "INBOX",
            payload,
            preview_lines=2,
            full_body=False,
            fallback_envelope=None,
            attachments_list=[],
            attachment_status="none",
            attachment_error=None,
        )
        self.assertFalse(result["headers_complete"])
        self.assertEqual("", result["references"])
        self.assertEqual("", result["cc"])


class V3ExecuteReadIdTests(unittest.TestCase):
    """``modes.execute`` reads the typed v3 header Message-ID, not a string map."""

    def test_typed_headers_message_id_is_read(self) -> None:
        from core.modes.execute import _v3_read_message_id

        view = v3_message_view("41", message_id="old@example.org")
        self.assertEqual("old@example.org", _v3_read_message_id(json.dumps(view)))

    def test_the_invented_string_header_map_is_not_accepted(self) -> None:
        from core.modes.execute import _v3_read_message_id

        legacy = {"messageId": None, "headers": {"Message-Id": "<old@example.org>"}}
        self.assertIsNone(_v3_read_message_id(json.dumps(legacy)))


class V3InspectModeTests(unittest.TestCase):
    """``modes.inspect`` uses the profile-aware v3 decoder."""

    def test_v3_envelope_list_is_decoded_not_sliced(self) -> None:
        from core.himalaya import HimalayaOutput
        from core.modes import inspect as inspect_mode

        entry = v3_envelope("41", message_id="old@example.org", subject="Alt")
        calls: list[dict] = []

        class Transport:
            def __call__(self, args, account=None, timeout=35, **kwargs):
                calls.append({"args": list(args)})
                return HimalayaOutput(json.dumps([entry]), "v3")

        def get_details(envelope_id, folder, account, preview_lines, **kwargs):
            calls.append({"envelope_id": envelope_id, "fallback": kwargs.get("fallback_envelope")})
            return {
                "envelope_id": str(envelope_id),
                "folder": folder,
                "subject": "Alt",
                "from": "",
                "to": "",
                "preview": "",
                "attachments": [],
                "message_id": "old@example.org",
            }

        with tempfile.TemporaryDirectory() as temporary:
            result = inspect_mode.run_inspect_mode(
                {
                    "folder": "INBOX",
                    "count": 5,
                    "order": "oldest",
                    "check_known": False,
                    "skip_known": False,
                },
                account="demo",
                data_dir=Path(temporary),
                dependencies={
                    "run_himalaya": Transport(),
                    "get_oldest_envelopes": lambda *args, **kwargs: [],
                    "get_single_email_details": get_details,
                    "sleep": lambda seconds: None,
                },
            )
        self.assertEqual(1, result["total_inspected"])
        fallback = [call["fallback"] for call in calls if "fallback" in call][0]
        self.assertEqual("41", fallback["id"])
        self.assertEqual("old@example.org", fallback["message_id"])

    def test_incompatible_v3_page_fails_closed(self) -> None:
        from core.himalaya import HimalayaInvocationError, HimalayaOutput
        from core.modes import inspect as inspect_mode

        def transport(args, account=None, timeout=35, **kwargs):
            return HimalayaOutput(json.dumps({"unexpected": []}), "v3")

        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(HimalayaInvocationError) as raised:
                inspect_mode.run_inspect_mode(
                    {"folder": "INBOX", "count": 5, "order": "oldest", "check_known": False},
                    account="demo",
                    data_dir=Path(temporary),
                    dependencies={
                        "run_himalaya": transport,
                        "get_oldest_envelopes": lambda *args, **kwargs: [],
                        "get_single_email_details": lambda *args, **kwargs: {},
                        "sleep": lambda seconds: None,
                    },
                )
        self.assertEqual("himalaya_json_incompatible", raised.exception.reason_code)


class V1SeamParityTests(unittest.TestCase):
    """The documented v1 seams keep their byte-identical argv."""

    def test_v1_list_seams_stay_byte_identical(self) -> None:
        entries = [
            {
                "id": "4711",
                "subject": "Alt",
                "from": {"name": "Ada Lovelace", "addr": "ada@example.org"},
                "date": "2026-11-27T09:00:00Z",
                "flags": ["seen"],
            }
        ]
        transport = V1MailboxTransport(envelopes={"INBOX": entries})
        with patched_himalaya(transport):
            folders = client.op_list_folders()
            envelopes = client.op_list_envelopes("INBOX", 10)
        self.assertEqual([{"name": "INBOX"}, {"name": "Drafts"}, {"name": "Archive"}], folders)
        self.assertEqual(entries, envelopes)
        first, second = mailbox_commands(transport)
        self.assertEqual(["folder", "list", "-o", "json"], strip_global_prefix(first))
        self.assertEqual(
            ["-o", "json", "envelope", "list", "-f", "INBOX", "-s", "10"],
            strip_global_prefix(second),
        )


if __name__ == "__main__":  # pragma: no cover - manual invocation only
    unittest.main()
