"""Issue #103, slice 0: additive process-start telemetry by purpose.

The purpose is a closed label set (``TRANSPORT_PURPOSES``) carried from the
caller through ``run_himalaya`` into ``TransportStats.start_process``.  Every
successful child creation counts exactly one purpose, a failed factory counts
none, and ``snapshot()["starts_by_purpose"]`` always carries all nine labels.

All tests are hermetic: only the process boundary (``_POPEN``) or the CLI seams
are faked; no mailbox, credential or GroupWise path is touched.
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

from core import batch_contract  # noqa: E402
from core import classifier  # noqa: E402
from core import himalaya  # noqa: E402
from core.himalaya import HimalayaInvocationError  # noqa: E402
from core.modes import draft as draft_mode  # noqa: E402
import mail_desk_batch_runner as runner  # noqa: E402
from transport_hermetic_guard import HermeticProcessTestCase  # noqa: E402


EXPECTED_PURPOSES = (
    "thread_map",
    "list",
    "scan_preview",
    "scan_inventory",
    "full_read",
    "full_inventory",
    "evaluation_raw",
    "id_fallback",
    "other",
)

ZERO_PURPOSES = {purpose: 0 for purpose in EXPECTED_PURPOSES}


class _FakeProcess:
    """Minimal Popen stand-in; only ``communicate`` and ``kill`` are used."""

    def __init__(
        self,
        cmd: list[str],
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
        timeout: bool = False,
    ) -> None:
        self.cmd = cmd
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.timeout = timeout
        self.killed = False

    def communicate(self, input=None, timeout=None):  # noqa: A002 - Popen signature
        if self.timeout and not self.killed:
            raise subprocess.TimeoutExpired(self.cmd, timeout or 1)
        return self.stdout, self.stderr

    def kill(self) -> None:
        self.killed = True


def _payloads(payloads: list[dict]) -> tuple[object, object, object]:
    remaining = list(payloads)

    def fake_popen(cmd, **_kwargs):
        payload = remaining.pop(0)
        if isinstance(payload, BaseException):
            raise payload
        return _FakeProcess(cmd, **payload)

    return (
        patch.object(himalaya, "build_himalaya_command", return_value=["himalaya"]),
        patch.object(himalaya, "_POPEN", side_effect=fake_popen),
        patch.object(himalaya.time, "sleep"),
    )


def _transient() -> dict:
    return {"stderr": "TLS stream reset", "returncode": 1}


def _scan_dispatch(envelopes: list[dict]):
    """Return a process-boundary dispatcher for a deterministic two-item scan."""

    def fake_popen(cmd, **_kwargs):
        if "thread" in cmd:
            return _FakeProcess(cmd, stdout="[]")
        if "list" in cmd:
            return _FakeProcess(cmd, stdout=json.dumps(envelopes))
        if "export" in cmd:
            # The fake never writes the export file; the production helper then
            # degrades the inventory to ``attachment_inventory_unavailable``.
            return _FakeProcess(cmd, stdout="")
        if "read" in cmd:
            envelope_id = cmd[-1]
            return _FakeProcess(
                cmd,
                stdout=(
                    f"Message-Id: <{envelope_id}@example.test>\n"
                    f"Subject: item {envelope_id}\n"
                    "From: sender@example.test\n"
                    "To: desk@example.test\n"
                    "Date: Thu, 01 Jan 2026 00:00:00 +0000\n"
                    "\nbody\n"
                ),
            )
        raise AssertionError(f"unexpected command: {cmd}")

    return fake_popen


def _scan_envelopes() -> list[dict]:
    return [
        {
            "id": "11",
            "subject": "item 11",
            "from": {"name": "Sender", "addr": "sender@example.test"},
            "date": "2026-01-01",
        },
        {
            "id": "12",
            "subject": "item 12",
            "from": {"name": "Sender", "addr": "sender@example.test"},
            "date": "2026-01-02",
        },
    ]


class PurposeDistributionTests(HermeticProcessTestCase):
    """Point 1: a deterministic scan binds the exact purpose of every start."""

    def setUp(self) -> None:
        super().setUp()
        himalaya.reset_transport_stats()

    def test_scan_attributes_every_start_to_its_exact_purpose(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            himalaya,
            "build_himalaya_command",
            side_effect=lambda command, account=None: ["himalaya", *command],
        ), patch.object(
            himalaya, "_POPEN", side_effect=_scan_dispatch(_scan_envelopes())
        ) as run_mock, patch.object(
            himalaya.time, "sleep"
        ), himalaya.transport_stats_session() as stats:
            collected, known_count, skipped_known = runner._scan_unprocessed_emails(
                "INBOX",
                2,
                account=None,
                data_dir=Path(temporary) / "data" / "mail-desk",
                skip_known=True,
                preview_lines=5,
            )

        snapshot = stats.snapshot()
        self.assertEqual(2, len(collected))
        self.assertEqual(0, known_count)
        self.assertEqual(0, skipped_known)
        # One thread map, one successful listing and (export + read) per item.
        self.assertEqual(6, run_mock.call_count)
        self.assertEqual(6, snapshot["process_starts"])
        self.assertEqual(
            {
                **ZERO_PURPOSES,
                "thread_map": 1,
                "list": 1,
                "scan_preview": 2,
                "scan_inventory": 2,
            },
            snapshot["starts_by_purpose"],
        )
        self.assertEqual(
            snapshot["process_starts"], sum(snapshot["starts_by_purpose"].values())
        )
        self.assertEqual(
            {"envelope thread": 1, "envelope list": 1, "message export": 2, "message read": 2},
            {
                command: counts["starts"]
                for command, counts in snapshot["commands"].items()
            },
        )


class SumInvariantTests(HermeticProcessTestCase):
    """Point 2: the purposes are a partition of the starts, not an extra summand."""

    def setUp(self) -> None:
        super().setUp()
        himalaya.reset_transport_stats()

    def test_retries_and_commands_share_the_single_start_sum(self) -> None:
        build, run, sleep = _payloads([_transient(), {"stdout": "", "returncode": 0}])
        with build, run, sleep, himalaya.transport_stats_session() as stats:
            himalaya.run_himalaya(
                ["envelope", "list"], max_retries=2, purpose="list"
            )

        snapshot = stats.snapshot()
        self.assertEqual(2, snapshot["process_starts"])
        self.assertEqual(1, snapshot["retries"])
        self.assertEqual(2, snapshot["starts_by_purpose"]["list"])
        self.assertEqual(
            2, sum(counts["starts"] for counts in snapshot["commands"].values())
        )
        self.assertEqual(
            snapshot["process_starts"], sum(snapshot["starts_by_purpose"].values())
        )

    def test_blocked_case_keeps_the_same_sum_invariant(self) -> None:
        build, run, sleep = _payloads([_transient(), _transient()])
        with build, run, sleep, himalaya.transport_stats_session() as stats:
            with self.assertRaises(HimalayaInvocationError):
                himalaya.run_himalaya(["envelope", "list"], max_retries=1, purpose="list")
            with self.assertRaises(HimalayaInvocationError):
                himalaya.run_himalaya(["envelope", "list"], max_retries=1, purpose="list")

        snapshot = stats.snapshot()
        self.assertTrue(snapshot["blocked"])
        self.assertEqual(2, snapshot["process_starts"])
        self.assertEqual(2, snapshot["starts_by_purpose"]["list"])
        self.assertEqual(
            snapshot["process_starts"], sum(snapshot["starts_by_purpose"].values())
        )


class RetryPurposeTests(HermeticProcessTestCase):
    """Point 3: a retry is a second start of the same purpose."""

    def setUp(self) -> None:
        super().setUp()
        himalaya.reset_transport_stats()

    def test_transient_then_success_is_two_starts_of_one_purpose(self) -> None:
        build, run, sleep = _payloads([_transient(), {"stdout": "", "returncode": 0}])
        with build, run, sleep, himalaya.transport_stats_session() as stats:
            himalaya.run_himalaya(
                ["message", "read", "--preview", "-f", "INBOX", "7071"],
                max_retries=2,
                purpose="scan_preview",
            )

        snapshot = stats.snapshot()
        self.assertEqual(2, snapshot["process_starts"])
        self.assertEqual(1, snapshot["retries"])
        self.assertEqual(2, snapshot["starts_by_purpose"]["scan_preview"])
        self.assertEqual(0, snapshot["starts_by_purpose"]["list"])


class StartFailureTests(HermeticProcessTestCase):
    """Point 4: a failed child creation counts no start and no purpose."""

    def setUp(self) -> None:
        super().setUp()
        himalaya.reset_transport_stats()

    def test_launch_failure_has_no_start_and_keeps_the_error_form(self) -> None:
        build, run, sleep = _payloads([OSError("launch failed")])
        with build, run, sleep, himalaya.transport_stats_session() as stats:
            with self.assertRaises(HimalayaInvocationError) as raised:
                himalaya.run_himalaya(
                    ["message", "read", "--preview", "-f", "INBOX", "7071"],
                    max_retries=1,
                    purpose="full_read",
                )

        snapshot = stats.snapshot()
        self.assertEqual("himalaya_launch_failed", raised.exception.reason_code)
        self.assertEqual(0, snapshot["process_starts"])
        self.assertEqual(1, snapshot["errors"])
        self.assertEqual(ZERO_PURPOSES, snapshot["starts_by_purpose"])

    def test_factory_error_counts_no_start_in_start_process(self) -> None:
        stats = himalaya.TransportStats()

        def failing_factory():
            raise OSError("launch failed")

        with self.assertRaises(OSError):
            stats.start_process("envelope list", failing_factory, purpose="list")

        snapshot = stats.snapshot()
        self.assertEqual(0, snapshot["process_starts"])
        self.assertEqual(ZERO_PURPOSES, snapshot["starts_by_purpose"])


class BlockSnapshotTests(HermeticProcessTestCase):
    """Point 5: F6 starts nothing further and reports a consistent purpose snapshot."""

    def setUp(self) -> None:
        super().setUp()
        himalaya.reset_transport_stats()

    def test_block_starts_nothing_further_and_reports_the_snapshot(self) -> None:
        build, run, sleep = _payloads([_transient(), _transient()])
        with build, run as run_mock, sleep, himalaya.transport_stats_session() as stats:
            with self.assertRaises(HimalayaInvocationError):
                himalaya.run_himalaya(["envelope", "list"], max_retries=1, purpose="list")
            with self.assertRaises(HimalayaInvocationError) as blocked:
                himalaya.run_himalaya(["envelope", "list"], max_retries=1, purpose="list")
            self.assertEqual("mailbox_transport_blocked", blocked.exception.reason_code)
            with self.assertRaises(HimalayaInvocationError):
                himalaya.run_himalaya(["envelope", "list"], max_retries=1, purpose="list")

        snapshot = stats.snapshot()
        self.assertEqual(2, run_mock.call_count)
        self.assertEqual(2, snapshot["process_starts"])
        self.assertEqual(
            {**ZERO_PURPOSES, "list": 2}, snapshot["starts_by_purpose"]
        )
        self.assertEqual(
            snapshot["process_starts"], sum(snapshot["starts_by_purpose"].values())
        )

    def test_transport_blocked_envelope_carries_the_purpose_snapshot(self) -> None:
        import io

        def blocked_mode(*_args, **_kwargs):
            with himalaya.transport_stats_session():
                last: HimalayaInvocationError | None = None
                for _ in range(2):
                    try:
                        himalaya.run_himalaya(
                            ["envelope", "list"], max_retries=1, purpose="list"
                        )
                    except HimalayaInvocationError as exc:
                        last = exc
                assert last is not None
                raise last

        build, run, sleep = _payloads([_transient(), _transient()])
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary, build, run, sleep, patch.object(
            runner, "resolve_data_dir", return_value=Path(temporary)
        ), patch.object(
            runner,
            "resolve_final_index_path",
            return_value=Path(temporary) / "index.json",
        ), patch.object(
            runner, "run_draft_mode", side_effect=blocked_mode
        ), patch.object(
            sys, "argv", ["mail_desk_batch_runner.py", "--draft", "1"]
        ), patch(
            "sys.stdout", stdout
        ):
            code = runner.main()

        envelope = json.loads(stdout.getvalue())
        self.assertEqual(1, code)
        self.assertEqual("TransportBlocked", envelope["error"]["type"])
        details = envelope["error"]["details"]
        self.assertEqual("mailbox_transport_blocked", details["reason_code"])
        self.assertEqual(
            {**ZERO_PURPOSES, "list": 2},
            details["transport_stats"]["starts_by_purpose"],
        )


class PurposeValidationTests(HermeticProcessTestCase):
    """Point 6: an unknown purpose is rejected before any child creation."""

    def test_run_himalaya_rejects_an_unknown_purpose_without_a_process(self) -> None:
        with patch.object(himalaya, "_POPEN") as popen:
            with self.assertRaises(ValueError):
                himalaya.run_himalaya(["folder", "list"], purpose="not_a_purpose")

        popen.assert_not_called()

    def test_start_process_rejects_an_unknown_purpose_without_a_factory_call(self) -> None:
        stats = himalaya.TransportStats()
        called: list[int] = []

        with self.assertRaises(ValueError):
            stats.start_process(
                "envelope list", lambda: called.append(1), purpose="not_a_purpose"
            )

        self.assertEqual([], called)
        self.assertEqual(0, stats.snapshot()["process_starts"])

    def test_diagnostic_spawn_is_attributed_to_other(self) -> None:
        capture = object()

        def fake_diagnostic(cmd, *, stdin_bytes, timeout, capture, spawn=None):
            # Exercise the real spawn wrapper so the attribution is observed, not
            # bypassed by a fake diagnostic run.
            assert spawn is not None
            spawn(cmd, stdin=subprocess.PIPE)
            return "ok"

        with patch.object(
            himalaya, "build_himalaya_command", return_value=["himalaya"]
        ), patch.object(
            himalaya, "_POPEN", side_effect=lambda cmd, **_kw: _FakeProcess(cmd)
        ), patch.object(
            himalaya, "_run_himalaya_diagnostic", side_effect=fake_diagnostic
        ):
            with himalaya.transport_stats_session() as active:
                himalaya.run_himalaya(
                    ["message", "save", "-o", "plain"],
                    max_retries=1,
                    stdin_bytes=b"payload",
                    diagnostic_capture=capture,
                    purpose="scan_preview",
                )

        snapshot = active.snapshot()
        self.assertEqual(1, snapshot["process_starts"])
        self.assertEqual({**ZERO_PURPOSES, "other": 1}, snapshot["starts_by_purpose"])


class ReaderPurposeBindingTests(HermeticProcessTestCase):
    """The helper binds full-read/evaluation exports and keeps other callers at other."""

    def setUp(self) -> None:
        super().setUp()
        himalaya.reset_transport_stats()

    _HEADERS = (
        "Message-Id: <item@example.test>\n"
        "Subject: item\n"
        "From: sender@example.test\n"
        "\nbody\n"
    )

    def test_full_read_export_binds_full_inventory(self) -> None:
        build, run, sleep = _payloads(
            [{"stdout": ""}, {"stdout": self._HEADERS}]
        )
        with build, run, sleep, himalaya.transport_stats_session() as stats:
            himalaya.get_single_email_details(
                "21", "INBOX", full_body=True, purpose="full_read"
            )

        snapshot = stats.snapshot()
        self.assertEqual(2, snapshot["process_starts"])
        self.assertEqual(
            {**ZERO_PURPOSES, "full_read": 1, "full_inventory": 1},
            snapshot["starts_by_purpose"],
        )

    def test_unbound_caller_stays_other(self) -> None:
        build, run, sleep = _payloads([{"stdout": self._HEADERS}])
        with build, run, sleep, himalaya.transport_stats_session() as stats:
            himalaya.get_single_email_details("22", "INBOX", inspect_attachments=False)

        self.assertEqual(
            {**ZERO_PURPOSES, "other": 1}, stats.snapshot()["starts_by_purpose"]
        )


class SnapshotShapeTests(HermeticProcessTestCase):
    """Point 8: exactly the nine closed purposes, integer values, deterministic."""

    def test_fresh_snapshot_lists_all_purposes_with_zero(self) -> None:
        snapshot = himalaya.TransportStats().snapshot()
        self.assertEqual(list(sorted(EXPECTED_PURPOSES)), list(snapshot["starts_by_purpose"]))
        self.assertEqual(set(EXPECTED_PURPOSES), set(snapshot["starts_by_purpose"]))
        for purpose, value in snapshot["starts_by_purpose"].items():
            self.assertIs(type(value), int, purpose)


class ReviewPayloadTests(HermeticProcessTestCase):
    """Point 7: purposes never reach the review payload or change its hash."""

    EMAIL = {
        "envelope_id": "5001",
        "folder": "INBOX",
        "message_id": "sender-5001@example.test",
        "subject": "Kurzfrage",
        "from": "sender@example.test",
        "to": "desk@example.test",
        "cc": "",
        "date": "Thu, 01 Jan 2026 00:00:00 +0000",
        "preview": "Kurze Rueckfrage ohne weitere Details.",
    }

    def _review_hash(self, purpose: str, starts: int) -> str:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / "data" / "mail-desk").mkdir(parents=True)
            with himalaya.transport_stats_session() as stats:
                for _ in range(starts):
                    stats.start_process("envelope list", lambda: object(), purpose=purpose)
                manifest = classifier.draft_manifest(
                    [dict(self.EMAIL)],
                    workspace_root=workspace,
                    sent_lookup={},
                    final_index={},
                    sync_sent=False,
                )
                batch_contract.add_draft_contract(
                    manifest,
                    expected_count=1,
                    allow_fewer=False,
                    source_folder="INBOX",
                    account=None,
                    skip_known=True,
                )
            serialized = json.dumps(manifest, ensure_ascii=False, sort_keys=True)
        self.assertNotIn("starts_by_purpose", serialized)
        self.assertNotIn("transport_stats", serialized)
        return manifest["review"]["execute_request_sha256"]

    def test_same_draft_with_different_purposes_keeps_the_same_review_hash(self) -> None:
        baseline = self._review_hash("other", 0)
        with_list = self._review_hash("list", 3)
        with_full_read = self._review_hash("full_read", 2)
        self.assertEqual(baseline, with_list)
        self.assertEqual(baseline, with_full_read)


class _NullTracker:
    """Inert progress tracker for draft-entry tests that must not write progress."""

    def __init__(self, **_kwargs) -> None:
        pass

    def step(self, *_args, **_kwargs) -> None:
        pass

    def advance_item(self, *_args, **_kwargs) -> None:
        pass

    def complete(self, *_args, **_kwargs) -> None:
        pass


def _headers(envelope_id: str) -> str:
    return (
        f"Message-Id: <{envelope_id}@example.test>\n"
        "Subject: item\n"
        "From: sender@example.test\n"
        "To: desk@example.test\n"
        "Date: Thu, 01 Jan 2026 00:00:00 +0000\n"
        "\nbody\n"
    )


def _preview_email(envelope_id: str, *, subject: str = "item", preview: str = "body") -> dict:
    return {
        "envelope_id": envelope_id,
        "folder": "INBOX",
        "message_id": f"{envelope_id}@example.test",
        "subject": subject,
        "from": "sender@example.test",
        "to": "desk@example.test",
        "cc": "",
        "date": "Thu, 01 Jan 2026 00:00:00 +0000",
        "preview": preview,
    }


def _command_dispatch(handlers: dict[str, dict]):
    """Return a process boundary that dispatches on one argv token."""

    def fake_popen(cmd, **_kwargs):
        for token, payload in handlers.items():
            if token in cmd:
                if isinstance(payload, BaseException):
                    raise payload
                return _FakeProcess(cmd, **payload)
        raise AssertionError(f"unexpected command: {cmd}")

    return fake_popen


class DraftEntryPurposeTests(HermeticProcessTestCase):
    """F1: the real draft entry tags injected/default readers via the process seam."""

    def setUp(self) -> None:
        super().setUp()
        himalaya.reset_transport_stats()

    def _run(self, data_dir: Path, dependencies: dict, output: Path) -> dict:
        return draft_mode.run_draft_mode(
            {"count": 1, "output_file": str(output)},
            account="primary",
            data_dir=data_dir,
            dependencies=dependencies,
        )

    def test_draft_evaluation_raw_uses_the_default_wrapped_reader(self) -> None:
        email = _preview_email("31")
        item = {
            "envelope_id": "31",
            "folder": "INBOX",
            "message_id": "31@example.test",
            "decision": {
                "kind": "unknown",
                "id": "unclassified",
                "confidence": "low",
                "needs_reply": False,
                "review_required": True,
                "review_reason": "insufficient_context",
            },
            "action": {"type": "keep_in_folder", "target_folder": "INBOX"},
        }

        def fake_draft(emails_arg, *, source_sink=None, **_kwargs):
            for candidate in emails_arg:
                source_sink(candidate)
            return {"items": [item]}

        evaluate = Mock(
            side_effect=AssertionError("no evaluation without a raw export file")
        )
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            himalaya,
            "build_himalaya_command",
            side_effect=lambda command, account=None: ["himalaya", *command],
        ), patch.object(
            himalaya, "_POPEN", side_effect=_command_dispatch({"export": {"stdout": ""}})
        ), patch.object(
            himalaya.time, "sleep"
        ), himalaya.transport_stats_session() as stats:
            data_dir = Path(temporary) / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            self._run(
                data_dir,
                {
                    "BatchProgressTracker": _NullTracker,
                    "atomic_write_json": runner.atomic_write_json,
                    "draft_manifest": fake_draft,
                    "get_unprocessed_emails": Mock(return_value=([email], 0)),
                    "load_sent_index": Mock(return_value={}),
                    "attachment_evaluate": evaluate,
                },
                data_dir / "batch-manifest.json",
            )

        snapshot = stats.snapshot()
        self.assertEqual(1, snapshot["process_starts"])
        self.assertEqual(
            {**ZERO_PURPOSES, "evaluation_raw": 1}, snapshot["starts_by_purpose"]
        )
        evaluate.assert_not_called()

    def test_draft_full_read_and_full_inventory_use_default_wrapped_readers(self) -> None:
        email = _preview_email(
            "32", subject="Routine update", preview="Could you confirm this?"
        )
        dispatch = _command_dispatch(
            {"export": {"stdout": ""}, "read": {"stdout": _headers("32")}}
        )
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            himalaya,
            "build_himalaya_command",
            side_effect=lambda command, account=None: ["himalaya", *command],
        ), patch.object(
            himalaya, "_POPEN", side_effect=dispatch
        ), patch.object(
            himalaya.time, "sleep"
        ), himalaya.transport_stats_session() as stats:
            data_dir = Path(temporary) / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            result = self._run(
                data_dir,
                {
                    "BatchProgressTracker": _NullTracker,
                    "atomic_write_json": runner.atomic_write_json,
                    "get_unprocessed_emails": Mock(return_value=([email], 0)),
                    "load_sent_index": Mock(return_value={}),
                    "attachment_evaluate": Mock(),
                    "decision_triggers_evaluation": lambda *_a, **_k: False,
                },
                data_dir / "batch-manifest.json",
            )

        self.assertEqual(1, result["draft"]["candidate_count"])
        snapshot = stats.snapshot()
        self.assertEqual(2, snapshot["process_starts"])
        self.assertEqual(
            {**ZERO_PURPOSES, "full_read": 1, "full_inventory": 1},
            snapshot["starts_by_purpose"],
        )

    def test_preview_failure_then_message_id_fallback_is_id_fallback(self) -> None:
        build, run, sleep = _payloads(
            [
                {"stderr": "mailbox does not exist", "returncode": 1},
                {"stdout": "Message-Id: <51@example.test>\n"},
            ]
        )
        with build, run, sleep, himalaya.transport_stats_session() as stats:
            details = himalaya.get_single_email_details(
                "51",
                "INBOX",
                inspect_attachments=False,
                purpose="scan_preview",
                fallback_envelope={"id": "51", "subject": "item 51"},
            )

        self.assertEqual("51@example.test", details["message_id"])
        snapshot = stats.snapshot()
        self.assertEqual(2, snapshot["process_starts"])
        self.assertEqual(
            {**ZERO_PURPOSES, "scan_preview": 1, "id_fallback": 1},
            snapshot["starts_by_purpose"],
        )

    def test_query_listing_and_first_scan_loop_bind_list_and_scan_preview(self) -> None:
        envelopes = [
            {
                "id": "41",
                "subject": "item 41",
                "from": {"name": "Sender", "addr": "sender@example.test"},
                "date": "2026-01-01",
            }
        ]
        dispatch = _command_dispatch(
            {
                "thread": {"stdout": "[]"},
                "list": {"stdout": json.dumps(envelopes)},
                "export": {"stdout": ""},
                "read": {"stdout": _headers("41")},
            }
        )
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            himalaya,
            "build_himalaya_command",
            side_effect=lambda command, account=None: ["himalaya", *command],
        ), patch.object(
            himalaya, "_POPEN", side_effect=dispatch
        ), patch.object(
            himalaya.time, "sleep"
        ), himalaya.transport_stats_session() as stats:
            collected, known_count, skipped_known = runner._scan_unprocessed_emails(
                "INBOX",
                1,
                query="from sender@example.test",
                account=None,
                data_dir=Path(temporary) / "data" / "mail-desk",
                skip_known=True,
                preview_lines=5,
            )

        self.assertEqual(1, len(collected))
        self.assertEqual(0, known_count)
        self.assertEqual(0, skipped_known)
        snapshot = stats.snapshot()
        self.assertEqual(4, snapshot["process_starts"])
        self.assertEqual(
            {
                **ZERO_PURPOSES,
                "thread_map": 1,
                "list": 1,
                "scan_preview": 1,
                "scan_inventory": 1,
            },
            snapshot["starts_by_purpose"],
        )


if __name__ == "__main__":
    unittest.main()
