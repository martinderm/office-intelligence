"""Issue #103, slice 1 (P1): EML-derived preview details only on proven parity.

A completed scan-inventory export may replace the following preview
``message read`` only for an accepted, closed input class.  The golden pair is
a synthetic RFC-822 EML plus the CLI read text derived from it (never two
hand-written identical dicts).  Everything else falls back to the unchanged
export + read sequence without a second export.

All tests are hermetic: only the process boundary (``_POPEN``) is faked.
"""

from __future__ import annotations

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


def _cli_read_text(eml: bytes) -> str:
    """Derive the CLI read output from the EML (independent of the P1 parser)."""
    text = eml.decode("utf-8").replace("\r\n", "\n")
    header_block, separator, body = text.partition("\n\n")
    return header_block + (separator + body if separator else "")


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
    preview_lines: int = 30,
    full_body: bool = False,
    purpose: str = "scan_preview",
    fallback: dict | None = None,
) -> tuple[dict, list[str], dict]:
    calls: list[str] = []
    read_text = _cli_read_text(eml)
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
    """Point 1: accepted classes match the CLI read for every field."""

    def _assert_parity(self, eml: bytes, *, preview_lines: int = 30) -> dict:
        reference, reference_calls, _ = _details(
            eml, allow=False, preview_lines=preview_lines
        )
        candidate, candidate_calls, snapshot = _details(
            eml, allow=True, preview_lines=preview_lines
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
        self.assertEqual("Erste Zeile.\nZweite Zeile.", details["preview"])
        self.assertEqual("preview", details["read_level"])
        self.assertTrue(details["headers_complete"])
        self.assertIsNone(details["error"])
        self.assertIsNone(details["read_error_reason"])
        self.assertEqual("", details["cc"])
        self.assertEqual("", details["references"])
        self.assertEqual("", details["in_reply_to"])

    def test_optional_headers_are_carried_like_the_cli(self) -> None:
        headers = _base_headers() + [
            ("Cc", "cc@example.test"),
            ("In-Reply-To", "<parent@example.test>"),
            ("References", "<parent@example.test> <root@example.test>"),
        ]
        details = self._assert_parity(_eml(headers, ["Antwort."]))
        self.assertEqual("cc@example.test", details["cc"])
        self.assertEqual("<parent@example.test>", details["in_reply_to"])
        self.assertEqual("<parent@example.test> <root@example.test>", details["references"])

    def test_utf8_body_matches_the_cli_text_unchanged(self) -> None:
        body = ["Grüße aus Wien – Preis: 10 €."]
        details = self._assert_parity(_eml(_base_headers(), body))
        self.assertEqual(body[0], details["preview"])
        self.assertEqual("golden-61@example.test", details["message_id"])

    def test_message_id_with_single_brackets_is_accepted(self) -> None:
        details = self._assert_parity(_eml(_base_headers("<single@example.test>"), ["Body."]))
        self.assertEqual("single@example.test", details["message_id"])
        self.assertEqual("<single@example.test>", details["raw_message_id"])

    def test_message_id_bracket_shapes_are_checked_on_the_raw_value(self) -> None:
        # Test correction (documented): the canonical inventory rejects a bare
        # Message-ID as an email-policy defect before P1 can run, so the F6
        # bracket contract is pinned directly on the predicate.
        self.assertTrue(himalaya._is_well_formed_message_id("<a@b>"))
        self.assertTrue(himalaya._is_well_formed_message_id("a@b"))
        self.assertFalse(himalaya._is_well_formed_message_id("<<a@b>>"))
        self.assertFalse(himalaya._is_well_formed_message_id("<a@b"))
        self.assertFalse(himalaya._is_well_formed_message_id("a@b>"))


class EmlParityRejectionTests(HermeticProcessTestCase):
    """Point 2: every unproven class falls back to exactly one export + one read."""

    def _assert_rejected(self, eml: bytes, *, preview_lines: int = 30) -> None:
        reference, reference_calls, _ = _details(
            eml, allow=False, preview_lines=preview_lines
        )
        candidate, candidate_calls, snapshot = _details(
            eml, allow=True, preview_lines=preview_lines
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

    def test_html_only_is_rejected(self) -> None:
        self._assert_rejected(
            _eml(_base_headers(), ["<p>HTML</p>"], content_type="text/html; charset=utf-8")
        )

    def test_multipart_alternative_is_rejected(self) -> None:
        body = [
            "--b1",
            "Content-Type: text/plain; charset=utf-8",
            "",
            "plain",
            "--b1",
            "Content-Type: text/html; charset=utf-8",
            "",
            "<p>html</p>",
            "--b1--",
        ]
        self._assert_rejected(
            _eml(
                _base_headers(),
                body,
                content_type='multipart/alternative; boundary="b1"',
            )
        )

    def test_mime_defect_duplicate_content_type_is_rejected(self) -> None:
        eml = _eml(_base_headers(), ["Body."])
        # Insert a second Content-Type header line into the header block.
        text = eml.decode("utf-8")
        header_block, separator, body = text.partition("\r\n\r\n")
        raw = header_block + "\r\nContent-Type: text/plain\r\n\r\n" + body
        self._assert_rejected(raw.encode("utf-8"))

    def test_folded_header_is_rejected(self) -> None:
        eml = _eml(_base_headers(), ["Body."])
        text = eml.decode("utf-8").replace(
            "Subject: Kurzfrage zum Vorgang",
            "Subject: Kurzfrage\r\n zum Vorgang",
        )
        self._assert_rejected(text.encode("utf-8"))

    def test_rfc2047_encoded_header_is_rejected(self) -> None:
        headers = _base_headers()
        headers = [
            (name, "=?utf-8?b?SMOk?=" if name == "Subject" else value)
            for name, value in headers
        ]
        self._assert_rejected(_eml(headers, ["Body."]))

    def test_preview_boundary_is_rejected(self) -> None:
        self._assert_rejected(
            _eml(_base_headers(), [f"Zeile {index}" for index in range(6)]),
            preview_lines=5,
        )

    def test_empty_body_is_rejected(self) -> None:
        self._assert_rejected(_eml(_base_headers(), []))

    def test_missing_header_body_separator_is_rejected(self) -> None:
        self._assert_rejected(_eml(_base_headers(), [], separator=False))

    # --- F1: Content-Type parameters are a closed class -------------------
    def test_charset_with_whitespace_around_equals_is_rejected(self) -> None:
        for content_type in (
            "text/plain; charset = utf-8",
            "text/plain; charset= utf-8",
            "text/plain; charset =utf-8",
        ):
            with self.subTest(content_type=content_type):
                self._assert_rejected(_eml(_base_headers(), ["Body."], content_type=content_type))

    def test_duplicate_charset_is_rejected(self) -> None:
        self._assert_rejected(
            _eml(
                _base_headers(),
                ["Body."],
                content_type="text/plain; charset=utf-8; charset=utf-8",
            )
        )

    def test_format_flowed_and_delsp_are_rejected(self) -> None:
        for content_type in (
            "text/plain; charset=utf-8; format=flowed",
            "text/plain; charset=utf-8; delsp=yes",
        ):
            with self.subTest(content_type=content_type):
                self._assert_rejected(_eml(_base_headers(), ["Body."], content_type=content_type))

    def test_empty_content_type_value_is_rejected(self) -> None:
        self._assert_rejected(_eml(_base_headers(), ["Body."], content_type=""))

    # --- F2: non-ASCII bodies only with a declared UTF-8 charset ----------
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

    # --- F5: further closed-class rejections ------------------------------
    def test_iso_8859_1_charset_is_rejected(self) -> None:
        self._assert_rejected(
            _eml(
                _base_headers(),
                ["Body."],
                content_type="text/plain; charset=iso-8859-1",
            )
        )

    def test_utf8_alias_charset_is_rejected(self) -> None:
        self._assert_rejected(
            _eml(
                _base_headers(),
                ["Body."],
                content_type="text/plain; charset=utf8",
            )
        )

    def test_quoted_printable_transfer_encoding_is_rejected(self) -> None:
        self._assert_rejected(
            _eml(_base_headers(), ["Body."], cte="quoted-printable")
        )

    def test_base64_transfer_encoding_is_rejected(self) -> None:
        self._assert_rejected(_eml(_base_headers(), ["Body."], cte="base64"))

    def test_content_disposition_is_rejected(self) -> None:
        self._assert_rejected(
            _eml(_base_headers() + [("Content-Disposition", "inline")], ["Body."])
        )

    def test_mime_version_other_than_1_0_is_rejected(self) -> None:
        self._assert_rejected(
            _eml(_base_headers() + [("MIME-Version", "2.0")], ["Body."])
        )

    def test_name_parameter_is_rejected(self) -> None:
        self._assert_rejected(
            _eml(
                _base_headers(),
                ["Body."],
                content_type='text/plain; name="a.txt"; charset=utf-8',
            )
        )

    def test_duplicate_subject_header_is_rejected(self) -> None:
        self._assert_rejected(
            _eml(_base_headers() + [("Subject", "Second subject")], ["Body."])
        )

    # --- F6: bracket shape on the raw Message-ID --------------------------
    def test_nested_message_id_brackets_are_rejected(self) -> None:
        self._assert_rejected(_eml(_base_headers("<<a@b>>"), ["Body."]))


class EmlControlPathTests(HermeticProcessTestCase):
    """Points 3-5: helper default, has_attachment=False and the full path."""

    def setUp(self) -> None:
        super().setUp()
        himalaya.reset_transport_stats()

    def test_helper_default_keeps_export_and_read(self) -> None:
        eml = _eml(_base_headers(), ["Body."])
        details, calls, _ = _details(eml, allow=False)
        self.assertEqual(["export", "read"], calls)
        self.assertEqual("Body.", details["preview"])

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
        self.assertEqual("Body.", details["preview"])

    def test_full_body_with_the_flag_still_exports_and_reads(self) -> None:
        eml = _eml(_base_headers(), ["Body."])
        details, calls, snapshot = _details(
            eml, allow=True, full_body=True, purpose="full_read"
        )
        self.assertEqual(["export", "read"], calls)
        self.assertEqual("Body.", details["preview"])
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
        self.assertEqual(
            on_result["draft"]["items"], off_result["draft"]["items"]
        )
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

    def test_unproven_candidate_keeps_one_export_and_one_read(self) -> None:
        headers = _base_headers()
        headers = [
            (name, "=?utf-8?b?SMOk?=" if name == "Subject" else value)
            for name, value in headers
        ]
        eml = _eml(headers, ["Body."])
        calls: list[str] = []
        data_dir = None
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            envelope = {
                "id": "61",
                "subject": "Kurzfrage",
                "from": {"name": "Sender", "addr": "sender@example.test"},
                "date": "2026-01-01",
            }

            def fake_scan(**kwargs):
                return runner._scan_unprocessed_emails(**kwargs)[:2]

            def fake_draft(emails_arg, *, source_sink=None, **_kwargs):
                for candidate in emails_arg:
                    source_sink(candidate)
                return {"items": []}

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
                runner, "load_final_index", return_value={"items": {}}
            ), himalaya.transport_stats_session() as stats:
                draft_mode.run_draft_mode(
                    {"count": 1, "skip_known": True, "allow_eml_details": True},
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

        self.assertEqual(
            ["thread", "list", "export", "read"], calls
        )
        self.assertEqual(1, stats.snapshot()["starts_by_purpose"]["scan_inventory"])
        self.assertEqual(1, stats.snapshot()["starts_by_purpose"]["scan_preview"])

    def test_known_skip_decision_is_identical_with_and_without_opt_in(self) -> None:
        results = []
        for allow in (True, False):
            with tempfile.TemporaryDirectory() as temporary:
                calls: list[str] = []
                eml = _eml(_base_headers(), ["Body."])
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
    """F8: predicate rejections proven without the export/read seam."""

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

    def test_base64_transfer_encoding_is_none(self) -> None:
        self.assertIsNone(
            self._predicate(_eml(_base_headers(), ["Body."], cte="base64"))
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
