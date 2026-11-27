"""HV3-1 version matrix: one probe per executable, v1 byte parity, fail-closed.

Evidence mode ``tdd``, risk tier ``medium``.  Every test here is hermetic: the
``himalaya`` child is replaced at both transport seams (``subprocess.run`` and the
module-level ``_POPEN`` factory), the configuration is a temporary file and no
mailbox, network or credential is touched.

Contract encoded here (binding decisions of the HV3-1 dispatch):

* exactly one ``himalaya --version`` per executable/process, without
  ``-c``/account/mailbox, with a short timeout, and excluded from
  ``TransportStats``/``process_starts``;
* ``1.2.x`` selects the pinned v1 argv, ``3.x`` the v3 argv, and an unknown,
  missing, malformed or unstartable version raises ``HimalayaInvocationError``
  *before* any mailbox command (no silent profile switch);
* the representative v1 argv of every existing call family stays byte-identical;
* ``HIMALAYA_CONFIG`` and the per-command timeouts do not change per profile,
  while the ``-c`` token is v1-normalized and v3-native.

The version probe is exercised only through the public seams that need it
(``build_himalaya_command``, the client and the batch runner), never by
importing a helper that does not exist yet.
"""

from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import patch

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mail_desk_himalaya_client as client  # noqa: E402
from core import himalaya  # noqa: E402
from himalaya_v3_fixtures import (  # noqa: E402
    EMPTY_VERSION_OUTPUT,
    MALFORMED_VERSION_OUTPUT,
    UNSUPPORTED_VERSION_OUTPUT,
    V1MailboxTransport,
    V1_VERSION_OUTPUT,
    V3MailboxTransport,
    V3_VERSION_OUTPUT,
    HimalayaTransport,
    global_prefix,
    patched_himalaya,
    strip_global_prefix,
)

#: ``-o``/``-f``-style v1 argv that must survive byte for byte (with ``-a demo``).
V1_ARGV_CASES: tuple[tuple[list[str], list[str]], ...] = (
    (
        ["-o", "json", "envelope", "list", "-f", "INBOX", "-s", "10"],
        ["-o", "json", "envelope", "list", "-a", "demo", "-f", "INBOX", "-s", "10"],
    ),
    (
        ["message", "read", "--preview", "-H", "Message-Id", "-f", "INBOX", "41"],
        ["message", "read", "-a", "demo", "--preview", "-H", "Message-Id", "-f", "INBOX", "41"],
    ),
    (
        ["message", "export", "41", "-f", "INBOX", "-F", "-d", "msg_41.eml"],
        ["message", "export", "-a", "demo", "41", "-f", "INBOX", "-F", "-d", "msg_41.eml"],
    ),
    (
        ["message", "save", "-o", "plain", "-f", "Drafts"],
        ["message", "save", "-a", "demo", "-o", "plain", "-f", "Drafts"],
    ),
    (
        ["message", "copy", "Archive", "41", "-f", "INBOX"],
        ["message", "copy", "-a", "demo", "Archive", "41", "-f", "INBOX"],
    ),
    (
        ["message", "delete", "41", "-f", "INBOX"],
        ["message", "delete", "-a", "demo", "41", "-f", "INBOX"],
    ),
    (
        ["flag", "add", "deleted", "41", "-f", "Drafts"],
        ["flag", "add", "-a", "demo", "deleted", "41", "-f", "Drafts"],
    ),
    (
        ["folder", "list", "-o", "json"],
        ["folder", "list", "-a", "demo", "-o", "json"],
    ),
    (
        ["envelope", "thread", "-f", "INBOX", "-o", "json"],
        ["envelope", "thread", "-a", "demo", "-f", "INBOX", "-o", "json"],
    ),
)

_SUBCOMMAND_HEADS = frozenset({"envelope", "mailbox", "message", "flag", "folder"})


class VersionMatrixTests(unittest.TestCase):
    """The version decides the profile; anything unknown fails closed."""

    def test_v1_version_selects_the_pinned_v1_argv(self) -> None:
        transport = V1MailboxTransport()
        with patched_himalaya(transport) as (_module, config, executable):
            folders = client.op_list_folders()
        self.assertEqual([{"name": "INBOX"}, {"name": "Drafts"}, {"name": "Archive"}], folders)
        self.assertEqual(1, len(transport.version_calls))
        self.assertEqual(
            global_prefix(executable, config) + ["folder", "list", "-o", "json"],
            transport.mailbox_calls[0],
        )

    def test_v3_version_selects_the_v3_argv(self) -> None:
        transport = V3MailboxTransport()
        with patched_himalaya(transport) as (_module, config, executable):
            folders = client.op_list_folders()
        self.assertEqual(["INBOX", "Drafts", "Archive"], [item["name"] for item in folders])
        self.assertEqual(1, len(transport.version_calls))
        argv = transport.mailbox_calls[0]
        self.assertEqual(global_prefix(executable, config)[:2], argv[:2])
        self.assertEqual(["mailbox", "list"], strip_global_prefix(argv, with_json=True))

    def test_unknown_major_version_fails_closed_before_any_mailbox_command(self) -> None:
        transport = HimalayaTransport(UNSUPPORTED_VERSION_OUTPUT)
        with patched_himalaya(transport):
            with self.assertRaises(himalaya.HimalayaInvocationError) as raised:
                client.op_list_folders()
        self.assertEqual("himalaya_version_unsupported", raised.exception.reason_code)
        self.assertEqual([], transport.mailbox_calls)
        self.assertEqual(1, len(transport.version_calls))

    def test_malformed_version_output_fails_closed(self) -> None:
        transport = HimalayaTransport(MALFORMED_VERSION_OUTPUT)
        with patched_himalaya(transport):
            with self.assertRaises(himalaya.HimalayaInvocationError) as raised:
                client.op_list_folders()
        self.assertEqual("himalaya_version_unsupported", raised.exception.reason_code)
        self.assertEqual([], transport.mailbox_calls)

    def test_empty_version_output_fails_closed(self) -> None:
        transport = HimalayaTransport(EMPTY_VERSION_OUTPUT)
        with patched_himalaya(transport):
            with self.assertRaises(himalaya.HimalayaInvocationError) as raised:
                client.op_list_folders()
        self.assertEqual("himalaya_version_unsupported", raised.exception.reason_code)
        self.assertEqual([], transport.mailbox_calls)

    def test_unstartable_probe_is_unavailable(self) -> None:
        transport = HimalayaTransport(V3_VERSION_OUTPUT, version_error=OSError("cannot start"))
        with patched_himalaya(transport):
            with self.assertRaises(himalaya.HimalayaInvocationError) as raised:
                client.op_list_folders()
        self.assertEqual("himalaya_unavailable", raised.exception.reason_code)
        self.assertEqual([], transport.mailbox_calls)

    def test_probe_timeout_is_unavailable(self) -> None:
        transport = HimalayaTransport(V3_VERSION_OUTPUT, version_timeout=5)
        with patched_himalaya(transport):
            with self.assertRaises(himalaya.HimalayaInvocationError) as raised:
                client.op_list_folders()
        self.assertEqual("himalaya_unavailable", raised.exception.reason_code)
        self.assertEqual([], transport.mailbox_calls)

    def test_missing_executable_fails_closed_without_starting_a_probe(self) -> None:
        transport = HimalayaTransport(V3_VERSION_OUTPUT)
        with patched_himalaya(transport, executable_exists=False):
            with self.assertRaises(himalaya.HimalayaInvocationError) as raised:
                client.op_list_folders()
        self.assertEqual("himalaya_unavailable", raised.exception.reason_code)
        self.assertEqual([], transport.calls)

    def test_undecodable_probe_output_fails_closed(self) -> None:
        error = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        transport = HimalayaTransport(V3_VERSION_OUTPUT, version_error=error)
        with patched_himalaya(transport):
            with self.assertRaises(himalaya.HimalayaInvocationError) as raised:
                client.op_list_folders()
        self.assertEqual("himalaya_version_unsupported", raised.exception.reason_code)
        self.assertEqual([], transport.mailbox_calls)

    def test_non_missing_stat_error_is_unavailable(self) -> None:
        transport = HimalayaTransport(V3_VERSION_OUTPUT)
        with patched_himalaya(transport) as (module, _config, executable):
            real_stat = module.os.stat

            def stat_for_executable(path, *args, **kwargs):
                if str(path) == str(executable):
                    raise PermissionError("denied")
                return real_stat(path, *args, **kwargs)

            with patch.object(module.os, "stat", side_effect=stat_for_executable):
                with self.assertRaises(himalaya.HimalayaInvocationError) as raised:
                    himalaya.build_himalaya_command(["folder", "list", "-o", "json"], None)
        self.assertEqual("himalaya_unavailable", raised.exception.reason_code)
        self.assertEqual([], transport.calls)


class VersionProbeTests(unittest.TestCase):
    """One bounded, account-free, unaccounted probe per executable."""

    def test_probe_runs_exactly_once_per_executable(self) -> None:
        transport = V3MailboxTransport()
        with patched_himalaya(transport):
            client.op_list_folders()
            client.op_list_folders()
        self.assertEqual(1, len(transport.version_calls))
        self.assertEqual(2, len(transport.mailbox_calls))

    def test_probe_is_version_only_and_bounded(self) -> None:
        transport = V3MailboxTransport()
        with patched_himalaya(transport) as (_module, _config, executable):
            client.op_list_folders()
        probe_argv, probe_kwargs = transport.calls[0]
        self.assertEqual([str(executable), "--version"], probe_argv)
        for forbidden in (
            "-c",
            "--config",
            "-a",
            "--account",
            "-m",
            "--mailbox",
            "-f",
            "--folder",
            "envelope",
            "folder",
            "message",
            "--json",
            "-o",
        ):
            self.assertNotIn(forbidden, probe_argv, f"probe argv must stay account/config-free: {probe_argv!r}")
        self.assertIn("timeout", probe_kwargs, "the version probe must carry a short subprocess timeout")
        self.assertGreater(float(probe_kwargs["timeout"]), 0)
        self.assertLessEqual(float(probe_kwargs["timeout"]), 10)

    def test_pre_command_lookup_shares_the_cached_probe(self) -> None:
        transport = V3MailboxTransport()
        with patched_himalaya(transport):
            profile = himalaya.resolve_himalaya_profile_or_none()
            client.op_list_folders()
        self.assertIsNotNone(profile)
        self.assertEqual("v3", profile.name)
        self.assertEqual(1, len(transport.version_calls))

    def test_probe_is_excluded_from_transport_stats(self) -> None:
        transport = V3MailboxTransport()
        with patched_himalaya(transport):
            himalaya.reset_transport_stats()
            with himalaya.transport_stats_session() as stats:
                client.op_list_folders()
            snapshot = stats.snapshot()
        self.assertEqual(1, snapshot["process_starts"])
        self.assertEqual(1, sum(snapshot["starts_by_purpose"].values()))
        self.assertEqual(["mailbox", "list"], strip_global_prefix(transport.mailbox_calls[0], with_json=True))


class V1ArgvParityTests(unittest.TestCase):
    """The representative v1 argv of every call family stays byte-identical."""

    def test_representative_v1_argv_is_byte_identical(self) -> None:
        transport = HimalayaTransport(V1_VERSION_OUTPUT)
        with patched_himalaya(transport) as (_module, config, executable):
            prefix = global_prefix(executable, config)
            for args, expected_tail in V1_ARGV_CASES:
                with self.subTest(args=args):
                    self.assertEqual(
                        prefix + expected_tail,
                        himalaya.build_himalaya_command(list(args), "demo"),
                    )


class ProfileInvariantTests(unittest.TestCase):
    """Account placement and timeouts do not change per profile; the config token is
    profile-specific (v1 normalized admin share, v3 native)."""

    def test_config_token_is_profile_specific_and_timeout_is_shared(self) -> None:
        v1 = V1MailboxTransport()
        with patched_himalaya(v1) as (module, v1_config, _executable):
            client.op_list_folders()
        v3 = V3MailboxTransport()
        with patched_himalaya(v3) as (_module, v3_config, _executable):
            client.op_list_folders()

        v1_argv, v1_kwargs = v1.calls[-1]
        v3_argv, v3_kwargs = v3.calls[-1]
        self.assertEqual(v1_argv[1], "-c")
        self.assertEqual(v3_argv[1], "-c")
        self.assertEqual(str(module.normalize_himalaya_config_path(v1_config)), v1_argv[2])
        self.assertEqual(str(v3_config), v3_argv[2])
        self.assertEqual(v1_kwargs.get("timeout"), v3_kwargs.get("timeout"))

    def test_v3_global_account_precedes_the_subcommand(self) -> None:
        transport = V3MailboxTransport()
        with patched_himalaya(transport):
            client.op_list_folders(account="demo")
        argv = transport.mailbox_calls[0]
        config_index = argv.index("-c")
        account_index = argv.index("-a")
        subcommand_index = next(
            index for index, token in enumerate(argv) if token in _SUBCOMMAND_HEADS
        )
        self.assertEqual("demo", argv[account_index + 1])
        self.assertGreater(account_index, config_index + 1, "global -a must follow -c <path>")
        self.assertLess(account_index, subcommand_index, "global -a must precede the subcommand")


if __name__ == "__main__":  # pragma: no cover - manual invocation only
    unittest.main()
