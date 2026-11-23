"""Issue #103, slice 3 (P3): preview-first for known-heavy scans.

The explicit ``scan_profile="known_heavy"`` draft profile reads one preview per
candidate first, so a known message is skipped without any inventory export.
Unknown messages keep that preview and complete it with exactly one export and
no second read.  The default ``balanced`` profile keeps today's sequence.

All tests are hermetic: only the process boundary (``_POPEN``) is faked.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import himalaya  # noqa: E402
from core.modes import draft as draft_mode  # noqa: E402
from core.scan_source_reuse import ScanSourceContext  # noqa: E402
import mail_desk_batch_runner as runner  # noqa: E402
from transport_hermetic_guard import HermeticProcessTestCase  # noqa: E402


MESSAGE_ID = "<p3-61@example.test>"


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


def _eml(body_lines: list[str], *, message_id: str = MESSAGE_ID) -> bytes:
    headers = [
        f"Message-ID: {message_id}",
        "Subject: Kurzfrage zum Vorgang",
        "From: Sender <sender@example.test>",
        "To: desk@example.test",
        "Date: Thu, 01 Jan 2026 00:00:00 +0000",
        "Content-Type: text/plain; charset=utf-8",
    ]
    return (
        "\r\n".join(headers) + "\r\n\r\n" + "\r\n".join(body_lines) + "\r\n"
    ).encode("utf-8")


def _attachment_eml(*, message_id: str = MESSAGE_ID) -> bytes:
    lines = [
        f"Message-ID: {message_id}",
        "Subject: Kurzfrage zum Vorgang",
        "From: Sender <sender@example.test>",
        "To: desk@example.test",
        "Date: Thu, 01 Jan 2026 00:00:00 +0000",
        "MIME-Version: 1.0",
        'Content-Type: multipart/mixed; boundary="b1"',
        "",
        "--b1",
        "Content-Type: text/plain; charset=utf-8",
        "",
        "Body.",
        "--b1",
        "Content-Type: application/pdf",
        "Content-Transfer-Encoding: base64",
        'Content-Disposition: attachment; filename="a.pdf"',
        "",
        "JVBERi0xLjQK",
        "--b1--",
    ]
    return "\r\n".join(lines).encode("utf-8")


def _cli_read_text(eml: bytes) -> str:
    text = eml.decode("utf-8").replace("\r\n", "\n")
    header_block, separator, body = text.partition("\n\n")
    return header_block + (separator + body if separator else "")


def _envelope(envelope_id: str, *, has_attachment=None) -> dict:
    envelope = {
        "id": envelope_id,
        "subject": "Kurzfrage zum Vorgang",
        "from": {"name": "Sender", "addr": "sender@example.test"},
        "date": "2026-01-01",
    }
    if has_attachment is not None:
        envelope["has_attachment"] = has_attachment
    return envelope


class _NullTracker:
    def __init__(self, **_kwargs) -> None:
        pass

    def step(self, *_args, **_kwargs) -> None:
        pass

    def advance_item(self, *_args, **_kwargs) -> None:
        pass

    def complete(self, *_args, **_kwargs) -> None:
        pass


class PreviewFirstScanTests(HermeticProcessTestCase):
    """Points 1-8 and 10: the two scan loops under the known-heavy profile."""

    def setUp(self) -> None:
        super().setUp()
        himalaya.reset_transport_stats()

    def _scan(
        self,
        temporary: str,
        *,
        emls: dict[str, bytes],
        envelopes: list[dict],
        index_items: dict | None = None,
        scan_profile: str | None = None,
        skip_known: bool = True,
        target: int = 1,
        query: str | None = None,
        date: str | None = None,
        source_context: ScanSourceContext | None = None,
        read_texts: dict[str, list[str]] | None = None,
        export_emls: dict[str, bytes] | None = None,
    ):
        calls: list[str] = []
        reads: dict[str, int] = {}
        preview_kwargs: list[dict] = []
        read_texts = read_texts or {}
        export_emls = export_emls or emls

        def fake_popen(cmd, **_kwargs):
            if "export" in cmd:
                eid = str(cmd[cmd.index("export") + 1])
                Path(cmd[cmd.index("-d") + 1]).write_bytes(export_emls[eid])
                calls.append(f"export:{eid}")
                return _FakeProcess(cmd, stdout="")
            if "thread" in cmd:
                calls.append("thread")
                return _FakeProcess(cmd, stdout="[]")
            if "list" in cmd:
                calls.append("list")
                return _FakeProcess(cmd, stdout=json.dumps(envelopes))
            if "read" in cmd:
                eid = str(cmd[-1])
                reads[eid] = reads.get(eid, 0) + 1
                calls.append(f"read:{eid}")
                if eid in read_texts:
                    texts = read_texts[eid]
                    text = texts[min(reads[eid] - 1, len(texts) - 1)]
                else:
                    text = _cli_read_text(emls[eid])
                return _FakeProcess(cmd, stdout=text)
            raise AssertionError(f"unexpected command: {cmd}")

        original = himalaya.get_single_email_details

        def spy(*args, **kwargs):
            preview_kwargs.append(dict(kwargs))
            return original(*args, **kwargs)

        index = index_items or {}
        with patch.object(
            himalaya,
            "build_himalaya_command",
            side_effect=lambda command, account=None: ["himalaya", *command],
        ), patch.object(himalaya, "_POPEN", side_effect=fake_popen), patch.object(
            himalaya.time, "sleep"
        ), patch.object(
            runner, "build_signature_index", return_value={}
        ), patch.object(
            runner, "load_final_index", return_value={"items": index}
        ), patch.object(
            runner, "get_single_email_details", side_effect=spy
        ), himalaya.transport_stats_session() as stats:
            kwargs = {}
            if scan_profile is not None:
                kwargs["scan_profile"] = scan_profile
            collected, known_count, skipped = runner._scan_unprocessed_emails(
                "INBOX",
                target,
                account="primary",
                data_dir=Path(temporary) / "data" / "mail-desk",
                skip_known=skip_known,
                preview_lines=30,
                query=query,
                date=date,
                source_context=source_context,
                **kwargs,
            )
            snapshot = stats.snapshot()
        return collected, known_count, skipped, calls, snapshot, preview_kwargs

    def test_known_gap_needs_one_preview_and_no_export(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            collected, known, skipped, calls, snapshot, _kwargs = self._scan(
                temporary,
                emls={"61": _attachment_eml()},
                envelopes=[_envelope("61")],
                index_items={"p3-61@example.test": {}},
                scan_profile="known_heavy",
            )
        self.assertEqual(["thread", "list", "read:61"], calls)
        self.assertEqual([], collected)
        self.assertEqual(1, known)
        self.assertEqual(1, skipped)
        self.assertEqual(1, snapshot["starts_by_purpose"]["scan_preview"])
        self.assertEqual(0, snapshot["starts_by_purpose"]["scan_inventory"])
        self.assertEqual(
            snapshot["process_starts"], sum(snapshot["starts_by_purpose"].values())
        )

    def test_unknown_with_attachment_uses_one_export_and_no_second_read(self) -> None:
        eml = _attachment_eml()
        with tempfile.TemporaryDirectory() as temporary:
            collected, known, skipped, calls, snapshot, _kwargs = self._scan(
                temporary,
                emls={"61": eml},
                envelopes=[_envelope("61")],
                scan_profile="known_heavy",
            )
        self.assertEqual(["thread", "list", "read:61", "export:61"], calls)
        self.assertEqual(0, known)
        self.assertEqual(0, skipped)
        self.assertEqual(1, len(collected))
        self.assertEqual("available", collected[0]["attachment_status"])
        self.assertTrue(collected[0]["attachments"])
        self.assertEqual(1, snapshot["starts_by_purpose"]["scan_preview"])
        self.assertEqual(1, snapshot["starts_by_purpose"]["scan_inventory"])
        self.assertEqual(
            snapshot["process_starts"], sum(snapshot["starts_by_purpose"].values())
        )

    def test_unknown_without_attachment_reads_no_export(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            collected, _known, _skipped, calls, snapshot, _kwargs = self._scan(
                temporary,
                emls={"61": _eml(["Body."])},
                envelopes=[_envelope("61", has_attachment=False)],
                scan_profile="known_heavy",
            )
        self.assertEqual(["thread", "list", "read:61"], calls)
        self.assertEqual(1, len(collected))
        self.assertEqual("none", collected[0]["attachment_status"])
        self.assertEqual(1, snapshot["starts_by_purpose"]["scan_preview"])
        self.assertEqual(0, snapshot["starts_by_purpose"]["scan_inventory"])

    def test_mixed_order_counters_and_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            collected, known, skipped, calls, _snapshot, _kwargs = self._scan(
                temporary,
                emls={"61": _attachment_eml(), "62": _attachment_eml(message_id="<p3-62@example.test>"), "63": _eml(["Body."], message_id="<p3-63@example.test>")},
                envelopes=[_envelope("61"), _envelope("62"), _envelope("63")],
                index_items={"p3-61@example.test": {}},
                scan_profile="known_heavy",
                target=1,
            )
        self.assertEqual(
            ["thread", "list", "read:61", "read:62", "export:62"], calls
        )
        self.assertEqual(1, known)
        self.assertEqual(1, skipped)
        self.assertEqual(["62"], [item["envelope_id"] for item in collected])

    def test_no_skip_known_has_no_preview_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            collected, known, skipped, calls, snapshot, kwargs = self._scan(
                temporary,
                emls={"61": _attachment_eml()},
                envelopes=[_envelope("61")],
                index_items={"p3-61@example.test": {}},
                scan_profile="known_heavy",
                skip_known=False,
            )
        self.assertEqual(["list", "export:61", "read:61"], calls)
        self.assertEqual(1, known)
        self.assertEqual(0, skipped)
        self.assertEqual(1, len(collected))
        self.assertFalse(any(entry.get("preview_only") for entry in kwargs))
        self.assertEqual(1, snapshot["starts_by_purpose"]["scan_preview"])
        self.assertEqual(1, snapshot["starts_by_purpose"]["scan_inventory"])

    def test_identity_drift_takes_the_regular_path_without_mixing(self) -> None:
        preview_text = _cli_read_text(_eml(["Body."], message_id="<drift-a@example.test>"))
        regular_text = _cli_read_text(
            _eml(["Body."], message_id="<drift-b@example.test>")
        )
        context = ScanSourceContext(target_count=1)
        with tempfile.TemporaryDirectory() as temporary:
            collected, _known, _skipped, calls, snapshot, _kwargs = self._scan(
                temporary,
                emls={"61": _attachment_eml(message_id="<drift-b@example.test>")},
                envelopes=[_envelope("61")],
                scan_profile="known_heavy",
                source_context=context,
                read_texts={"61": [preview_text, regular_text]},
            )
        self.assertEqual(
            ["thread", "list", "read:61", "export:61", "export:61", "read:61"], calls
        )
        self.assertEqual(1, len(collected))
        self.assertEqual("drift-b@example.test", collected[0]["message_id"])
        self.assertEqual("available", collected[0]["attachment_status"])
        self.assertIsNone(
            context.lookup("61", "INBOX", "primary", "drift-a@example.test")
        )
        self.assertIsNotNone(
            context.lookup("61", "INBOX", "primary", "drift-b@example.test")
        )
        self.assertEqual(2, snapshot["starts_by_purpose"]["scan_inventory"])
        self.assertEqual(2, snapshot["starts_by_purpose"]["scan_preview"])

    def test_empty_preview_message_id_takes_the_regular_path_without_a_pre_export(self) -> None:
        eml = _eml(["Body."]).replace(b"Message-ID: <p3-61@example.test>\r\n", b"")
        context = ScanSourceContext(target_count=1)
        with tempfile.TemporaryDirectory() as temporary:
            collected, _known, _skipped, calls, snapshot, _kwargs = self._scan(
                temporary,
                emls={"61": eml},
                envelopes=[_envelope("61")],
                scan_profile="known_heavy",
                source_context=context,
            )
        self.assertEqual(["thread", "list", "read:61", "export:61", "read:61"], calls)
        self.assertEqual(1, len(collected))
        self.assertEqual("", collected[0]["message_id"])
        self.assertEqual(0, context.held_count)
        self.assertEqual(1, snapshot["starts_by_purpose"]["scan_inventory"])
        self.assertEqual(2, snapshot["starts_by_purpose"]["scan_preview"])

    def test_empty_preview_message_id_without_attachment_returns_the_preview(self) -> None:
        eml = _eml(["Body."]).replace(b"Message-ID: <p3-61@example.test>\r\n", b"")
        context = ScanSourceContext(target_count=1)
        with tempfile.TemporaryDirectory() as temporary:
            collected, _known, _skipped, calls, snapshot, _kwargs = self._scan(
                temporary,
                emls={"61": eml},
                envelopes=[_envelope("61", has_attachment=False)],
                scan_profile="known_heavy",
                source_context=context,
            )
        self.assertEqual(["thread", "list", "read:61"], calls)
        self.assertEqual(1, len(collected))
        self.assertEqual("", collected[0]["message_id"])
        self.assertEqual("none", collected[0]["attachment_status"])
        self.assertEqual(0, context.held_count)
        self.assertEqual(0, snapshot["starts_by_purpose"]["scan_inventory"])
        self.assertEqual(1, snapshot["starts_by_purpose"]["scan_preview"])

    def test_query_loop_uses_the_same_preview_first_skip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            collected, known, skipped, calls, snapshot, _kwargs = self._scan(
                temporary,
                emls={"61": _attachment_eml()},
                envelopes=[_envelope("61")],
                index_items={"p3-61@example.test": {}},
                scan_profile="known_heavy",
                query="subject foo",
            )
        self.assertEqual(["thread", "list", "read:61"], calls)
        self.assertEqual([], collected)
        self.assertEqual(1, known)
        self.assertEqual(1, skipped)
        self.assertEqual(0, snapshot["starts_by_purpose"]["scan_inventory"])

    def test_balanced_profile_is_the_unchanged_path_without_a_probe(self) -> None:
        for profile in (None, "balanced"):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as temporary:
                collected, known, _skipped, calls, snapshot, kwargs = self._scan(
                    temporary,
                    emls={"61": _attachment_eml()},
                    envelopes=[_envelope("61")],
                    index_items={"p3-61@example.test": {}},
                    scan_profile=profile,
                )
            self.assertEqual(["thread", "list", "export:61", "read:61"], calls)
            self.assertEqual([], collected)
            self.assertEqual(1, known)
            self.assertFalse(any(entry.get("preview_only") for entry in kwargs))
            self.assertEqual(1, snapshot["starts_by_purpose"]["scan_inventory"])
            self.assertEqual(1, snapshot["starts_by_purpose"]["scan_preview"])

    def test_unknown_profile_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            runner.get_unprocessed_emails(
                "INBOX", 1, scan_profile="turbo", data_dir=Path(".")
            )
        with self.assertRaises(ValueError):
            runner._scan_unprocessed_emails("INBOX", 1, scan_profile="turbo")


class PreviewFirstSourceReuseTests(HermeticProcessTestCase):
    """Point 9: preview-first feeds the P2 source store; P1/P2 stay untouched."""

    def setUp(self) -> None:
        super().setUp()
        himalaya.reset_transport_stats()

    def _draft(self, temporary: str, *, scan_profile: str):
        eml = _attachment_eml()
        data_dir = Path(temporary) / "data" / "mail-desk"
        data_dir.mkdir(parents=True, exist_ok=True)
        calls: list[str] = []
        captured: dict[str, object] = {}
        evaluation: dict[str, object] = {}

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
                return _FakeProcess(cmd, stdout=json.dumps([_envelope("61")]))
            if "read" in cmd:
                calls.append("read")
                return _FakeProcess(cmd, stdout=_cli_read_text(eml))
            raise AssertionError(f"unexpected command: {cmd}")

        def fake_scan(*, source_context=None, **kwargs):
            captured["context"] = source_context
            collected, known_count, _skipped = runner._scan_unprocessed_emails(
                source_context=source_context, **kwargs
            )
            return collected, known_count

        def fake_draft(emails_arg, *, full_reader=None, source_sink=None, account=None, **_kwargs):
            items = []
            for email in emails_arg:
                details = full_reader(
                    str(email["envelope_id"]),
                    email.get("folder", "INBOX"),
                    account,
                    fallback_envelope=email,
                    full_body=True,
                )
                if source_sink is not None:
                    source_sink(details)
                items.append(
                    {
                        "envelope_id": str(email.get("envelope_id")),
                        "source_folder": email.get("folder", "INBOX"),
                        "message_id": email.get("message_id", ""),
                        "attachments": details.get("attachments", []),
                        "attachment_status": details.get("attachment_status", "none"),
                        "attachment_error": details.get("attachment_error"),
                        "decision": {
                            "kind": "topic",
                            "id": "general",
                            "confidence": "low",
                            "needs_reply": True,
                        },
                        "action": {"type": "keep_in_folder", "target_folder": "INBOX"},
                    }
                )
            return {"items": items}

        def evaluate(**kwargs):
            evaluation["raw_eml"] = kwargs["raw_eml"]
            return {
                "attachment_evaluation": {
                    "status": "failed",
                    "reason": "fetch_failed",
                    "authorization": "not_applicable",
                    "files": [],
                }
            }

        def forbidden_raw_mime(*_args, **_kwargs):
            raise AssertionError("a bound source must not start an evaluation_raw export")

        with patch.object(
            himalaya,
            "build_himalaya_command",
            side_effect=lambda command, account=None: ["himalaya", *command],
        ), patch.object(himalaya, "_POPEN", side_effect=fake_popen), patch.object(
            himalaya.time, "sleep"
        ), patch.object(
            runner, "build_signature_index", return_value={}
        ), patch.object(
            runner, "load_final_index", return_value={"items": {}}
        ), patch.object(
            himalaya, "_details_from_eml", side_effect=AssertionError("P1 must stay inactive")
        ), himalaya.transport_stats_session() as stats:
            result = draft_mode.run_draft_mode(
                {"count": 1, "skip_known": True, "scan_profile": scan_profile},
                account="primary",
                data_dir=data_dir,
                dependencies={
                    "BatchProgressTracker": _NullTracker,
                    "atomic_write_json": runner.atomic_write_json,
                    "draft_manifest": fake_draft,
                    "get_unprocessed_emails": fake_scan,
                    "load_sent_index": Mock(return_value={}),
                    "fetch_raw_message_eml": forbidden_raw_mime,
                    "attachment_evaluate": evaluate,
                    "decision_triggers_evaluation": lambda *_a, **_k: True,
                },
            )
            snapshot = stats.snapshot()
        return result, calls, snapshot, captured.get("context"), evaluation

    def test_preview_first_binds_the_source_and_keeps_p1_p2_inactive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result, calls, snapshot, context, evaluation = self._draft(
                temporary, scan_profile="known_heavy"
            )
        self.assertEqual(
            ["thread", "list", "read", "export", "export", "read"], calls
        )
        self.assertEqual(1, snapshot["starts_by_purpose"]["scan_inventory"])
        self.assertEqual(0, snapshot["starts_by_purpose"]["evaluation_raw"])
        self.assertEqual(1, snapshot["starts_by_purpose"]["full_read"])
        self.assertEqual(1, snapshot["starts_by_purpose"]["full_inventory"])
        self.assertEqual("available", result["draft"]["items"][0]["attachment_status"])
        self.assertEqual(_attachment_eml(), evaluation["raw_eml"])
        self.assertIsNotNone(context)
        self.assertEqual(0, context.held_count)  # cleared by the draft finally path

    def test_invalid_draft_profile_is_rejected_before_any_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(himalaya, "_POPEN") as popen:
                with self.assertRaises(ValueError):
                    draft_mode.run_draft_mode(
                        {"count": 1, "scan_profile": "turbo"},
                        data_dir=Path(temporary) / "data" / "mail-desk",
                    )
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
