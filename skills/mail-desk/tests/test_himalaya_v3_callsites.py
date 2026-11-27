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
    assert_no_v1_tokens,
    assert_v3_command,
    minimal_eml,
    parse_command,
    patched_himalaya,
    strip_global_prefix,
    v3_envelope,
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
        self.assertIn("subject:Rechnung Q4", command.positionals)
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
        self.assertEqual([], mailbox_commands(transport))


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

    def test_delete_draft_fails_closed_without_any_write(self) -> None:
        transport = V3MailboxTransport()
        with patched_himalaya(transport), tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(drafts.DraftError) as raised:
                client.op_delete_draft(
                    "41", folder="Drafts", account="demo", data_dir=Path(temporary)
                )
        self.assertEqual("himalaya_expunge_unsupported", raised.exception.reason_code)
        self.assertEqual([], mailbox_commands(transport), "the refusal must precede every mailbox command")

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
        recorded: list[list[str]] = []

        def runner(args, account=None, timeout=35, max_retries=5, **kwargs: object) -> str:
            recorded.append(list(args))
            return json.dumps(
                [
                    v3_envelope(
                        "41",
                        message_id="child@example.org",
                        subject="Re: Original",
                        in_reply_to=("parent@example.org",),
                    ),
                    v3_envelope("42", message_id="parent@example.org", subject="Original"),
                ]
            )

        transport = HimalayaTransport(V3_VERSION_OUTPUT)
        with patched_himalaya(transport):
            mapping = known_skip.load_envelope_thread_map(
                "INBOX", account="demo", run_himalaya_fn=runner
            )
        self.assertEqual(1, len(recorded))
        argv = recorded[0]
        command = parse_command(argv)
        self.assertNotEqual("envelope thread", command.name, "v3 has no envelope thread command")
        self.assertIn(command.name, {"envelope list", "envelope search"})
        self.assertEqual("INBOX", command.option("-m", "--mailbox"))
        self.assertIn("--json", argv)
        self.assertEqual("child@example.org", mapping["41"].message_id)
        self.assertEqual("Original", mapping["42"].subject)

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
