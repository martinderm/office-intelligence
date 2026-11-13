"""Issue #103 follow-up: item isolation, transport telemetry and progress liveness.

Three defect classes from the 2026-11-13 live gate are pinned here:

1. **Guard semantics:** a per-item timeout (``message read``/``message export``
   for one envelope) is an item failure, not a run abort.  F6 stays unchanged
   for transport failures without item relation (network, launch, list/thread)
   and for item failures without any mailbox progress in between.
2. **Telemetry:** per command class counts, errors, retries and bounded
   durations; the abort envelope names the triggering call with class,
   envelope id, duration and exit status.
3. **Progress liveness:** a killed run's leftover ``status: running`` state is
   settled to ``aborted`` once its owner pid is gone; a live owner is never
   rewritten.

All tests are hermetic: only the process boundary (``_POPEN``) or the CLI
seams are faked; no mailbox is touched.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

from core import himalaya  # noqa: E402
from core import classifier  # noqa: E402
from core.himalaya import HimalayaInvocationError  # noqa: E402
from core.progress import (  # noqa: E402
    PROGRESS_INTERRUPTED_REASON,
    BatchProgressTracker,
    abort_active_trackers,
    read_runner_progress,
    settle_stale_runner_progress,
)
import mail_desk_batch_runner as runner  # noqa: E402


#: A pid that cannot belong to a live process on the test platforms.
DEAD_PID = 999_999_999


class _FakeProcess:
    """Minimal Popen stand-in for the stats-active process gate."""

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


def _run_with(payloads: list[dict]) -> tuple[object, object, object]:
    """Return patches driving ``run_himalaya`` with scripted process outcomes."""
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


def _timeout(**kwargs) -> dict:
    return {"timeout": True, **kwargs}


def _failed(stderr: str, *, returncode: int = 1) -> dict:
    return {"stderr": stderr, "returncode": returncode}


class TransportGuardItemIsolationTests(unittest.TestCase):
    """Point 1: item timeouts stay item failures; F6 is not weakened."""

    def setUp(self) -> None:
        himalaya.reset_transport_stats()

    def test_two_item_read_timeouts_do_not_abort_and_success_resets(self) -> None:
        build, run, sleep = _run_with(
            [_timeout(), _timeout(), {"stdout": "Message-Id: <a@example.test>\n"}]
        )
        with build, run, sleep, himalaya.transport_stats_session() as stats:
            for env_id in ("7071", "7072"):
                with self.assertRaises(HimalayaInvocationError) as raised:
                    himalaya.run_himalaya(
                        ["message", "read", "--preview", "-f", "INBOX", env_id],
                        max_retries=1,
                    )
                self.assertEqual("himalaya_timeout", raised.exception.reason_code)
            self.assertFalse(stats.snapshot()["blocked"])
            self.assertEqual(2, stats.snapshot()["consecutive_item_failures"])
            self.assertEqual(0, stats.snapshot()["consecutive_transport_errors"])

            himalaya.run_himalaya(
                ["message", "read", "--preview", "-f", "INBOX", "7073"],
                max_retries=1,
            )
            snapshot = stats.snapshot()
            self.assertEqual(0, snapshot["consecutive_item_failures"])
            self.assertEqual(0, snapshot["consecutive_transport_errors"])

    def test_item_export_timeout_is_tolerated_as_item_failure(self) -> None:
        build, run, sleep = _run_with([_timeout()])
        with build, run, sleep, himalaya.transport_stats_session() as stats:
            with self.assertRaises(HimalayaInvocationError) as raised:
                himalaya.run_himalaya(
                    ["message", "export", "7071", "-f", "INBOX", "-F", "-d", "x.eml"],
                    max_retries=1,
                )

        self.assertEqual("himalaya_timeout", raised.exception.reason_code)
        self.assertFalse(stats.snapshot()["blocked"])
        self.assertEqual(1, stats.snapshot()["consecutive_item_failures"])

    def test_read_without_envelope_id_is_not_item_scoped(self) -> None:
        build, run, sleep = _run_with([_timeout(), _timeout()])
        with build, run as run_mock, sleep, himalaya.transport_stats_session() as stats:
            with self.assertRaises(HimalayaInvocationError) as first:
                himalaya.run_himalaya(
                    ["message", "read", "--preview", "-f", "INBOX"], max_retries=1
                )
            with self.assertRaises(HimalayaInvocationError) as second:
                himalaya.run_himalaya(
                    ["message", "read", "--preview", "-f", "INBOX"], max_retries=1
                )

        self.assertEqual("himalaya_timeout", first.exception.reason_code)
        self.assertEqual("mailbox_transport_blocked", second.exception.reason_code)
        self.assertEqual(
            "two_consecutive_transport_errors", stats.snapshot()["block_reason"]
        )
        self.assertEqual(2, run_mock.call_count)

    def test_item_timeout_does_not_reset_a_pending_transport_error(self) -> None:
        transient = _failed("TLS stream reset")
        build, run, sleep = _run_with([transient, _timeout(), transient])
        with build, run, sleep, himalaya.transport_stats_session() as stats:
            with self.assertRaises(HimalayaInvocationError) as first:
                himalaya.run_himalaya(
                    ["message", "read", "--preview", "-f", "INBOX", "7071"],
                    max_retries=1,
                )
            with self.assertRaises(HimalayaInvocationError) as item:
                himalaya.run_himalaya(
                    ["message", "read", "--preview", "-f", "INBOX", "7072"],
                    max_retries=1,
                )
            with self.assertRaises(HimalayaInvocationError) as third:
                himalaya.run_himalaya(
                    ["message", "read", "--preview", "-f", "INBOX", "7073"],
                    max_retries=1,
                )

        self.assertEqual("himalaya_transient", first.exception.reason_code)
        self.assertEqual("himalaya_timeout", item.exception.reason_code)
        self.assertEqual("mailbox_transport_blocked", third.exception.reason_code)
        self.assertEqual(
            "two_consecutive_transport_errors", stats.snapshot()["block_reason"]
        )

    def test_item_timeouts_without_progress_abort_on_the_threshold(self) -> None:
        build, run, sleep = _run_with([_timeout() for _ in range(5)])
        with build, run, sleep, himalaya.transport_stats_session() as stats:
            for env_id in ("7071", "7072", "7073", "7074"):
                with self.assertRaises(HimalayaInvocationError) as raised:
                    himalaya.run_himalaya(
                        ["message", "read", "--preview", "-f", "INBOX", env_id],
                        max_retries=1,
                    )
                self.assertEqual("himalaya_timeout", raised.exception.reason_code)
            self.assertFalse(stats.snapshot()["blocked"])

            with self.assertRaises(HimalayaInvocationError) as raised:
                himalaya.run_himalaya(
                    ["message", "read", "--preview", "-f", "INBOX", "7075"],
                    max_retries=1,
                )

        self.assertEqual("mailbox_transport_blocked", raised.exception.reason_code)
        snapshot = stats.snapshot()
        self.assertEqual(
            himalaya.ITEM_FAILURE_BLOCK_REASON, snapshot["block_reason"]
        )
        self.assertEqual(5, snapshot["consecutive_item_failures"])
        self.assertEqual("7075", snapshot["block_trigger"]["envelope_id"])
        self.assertIsNone(snapshot["block_trigger"]["exit_code"])
        self.assertIsNotNone(snapshot["block_trigger"]["duration_ms"])

    def test_two_list_timeouts_still_abort_fail_closed(self) -> None:
        build, run, sleep = _run_with([_timeout(), _timeout()])
        with build, run as run_mock, sleep, himalaya.transport_stats_session() as stats:
            with self.assertRaises(HimalayaInvocationError) as first:
                himalaya.run_himalaya(["folder", "list"], max_retries=1)
            with self.assertRaises(HimalayaInvocationError) as second:
                himalaya.run_himalaya(["folder", "list"], max_retries=1)

        self.assertEqual("himalaya_timeout", first.exception.reason_code)
        self.assertEqual("mailbox_transport_blocked", second.exception.reason_code)
        self.assertEqual(
            "two_consecutive_transport_errors", stats.snapshot()["block_reason"]
        )
        self.assertEqual(2, run_mock.call_count)

    def test_item_network_failure_still_counts_toward_f6(self) -> None:
        transient = _failed("TLS stream reset")
        build, run, sleep = _run_with([transient, transient])
        with build, run, sleep, himalaya.transport_stats_session() as stats:
            with self.assertRaises(HimalayaInvocationError) as first:
                himalaya.run_himalaya(
                    ["message", "read", "--preview", "-f", "INBOX", "7071"],
                    max_retries=1,
                )
            with self.assertRaises(HimalayaInvocationError) as second:
                himalaya.run_himalaya(
                    ["message", "read", "--preview", "-f", "INBOX", "7072"],
                    max_retries=1,
                )

        self.assertEqual("himalaya_transient", first.exception.reason_code)
        self.assertEqual("mailbox_transport_blocked", second.exception.reason_code)
        self.assertEqual(
            "two_consecutive_transport_errors", stats.snapshot()["block_reason"]
        )

    def test_item_process_start_failure_still_aborts(self) -> None:
        build, run, sleep = _run_with([OSError("launch failed"), OSError("launch failed")])
        with build, run, sleep, himalaya.transport_stats_session() as stats:
            with self.assertRaises(HimalayaInvocationError) as first:
                himalaya.run_himalaya(
                    ["message", "read", "--preview", "-f", "INBOX", "7071"],
                    max_retries=1,
                )
            with self.assertRaises(HimalayaInvocationError) as second:
                himalaya.run_himalaya(
                    ["message", "read", "--preview", "-f", "INBOX", "7072"],
                    max_retries=1,
                )

        self.assertEqual("himalaya_launch_failed", first.exception.reason_code)
        self.assertEqual("mailbox_transport_blocked", second.exception.reason_code)
        self.assertEqual(
            "two_consecutive_transport_errors", stats.snapshot()["block_reason"]
        )

    def test_preview_read_timeout_reports_item_reason_without_escaping(self) -> None:
        timeout = HimalayaInvocationError(
            "himalaya_timeout", "Himalaya timed out; refusing to retry the mailbox command."
        )
        with patch.object(himalaya, "run_himalaya", side_effect=timeout):
            details = himalaya.get_single_email_details(
                "7071",
                "INBOX",
                fallback_envelope={
                    "id": "7071",
                    "subject": "slow item",
                    "from": {"name": "Sender", "addr": "sender@example.test"},
                    "date": "2026-01-21",
                },
            )

        self.assertFalse(details["headers_complete"])
        self.assertEqual("timeout", details["read_error_reason"])
        self.assertIn("timed out", details["error"])

    def test_full_read_timeout_falls_back_to_preview_for_review(self) -> None:
        preview_item = {
            "decision": {"kind": "unknown", "confidence": "low", "needs_reply": False},
            "notes": "",
        }
        full_read = {
            "error": "Himalaya timed out; refusing to retry the mailbox command.",
            "read_error_reason": "timeout",
        }
        email = {
            "envelope_id": "9392",
            "folder": "INBOX",
            "subject": "Kurzfrage",
            "from": "sender@example.test",
            "preview": "Kurze Rueckfrage ohne weitere Details.",
        }
        with patch.object(classifier, "classify_email", return_value=preview_item), \
                patch.object(
                    classifier, "full_body_triggers", return_value=["low_confidence"]
                ):
            item = classifier.classify_email_two_pass(
                email,
                full_reader=lambda *_args, **_kwargs: full_read,
                sent_lookup={},
                final_index={},
            )

        escalation = item["decision"]["read_escalation"]
        self.assertEqual("failed", escalation["status"])
        self.assertEqual("preview", escalation["fallback"])
        self.assertEqual("timeout", escalation["reason"])
        self.assertEqual("RuntimeError", escalation["error"]["type"])
        self.assertTrue(item["decision"]["review_required"])
        self.assertEqual(
            {"type": "keep_in_folder", "target_folder": "INBOX"}, item["action"]
        )


class TransportTelemetryTests(unittest.TestCase):
    """Point 2: per-class durations and the structured block trigger."""

    def setUp(self) -> None:
        himalaya.reset_transport_stats()

    def test_per_class_counts_durations_and_exit_are_reported(self) -> None:
        build, run, sleep = _run_with(
            [
                {"stdout": "[]", "returncode": 0},
                _failed("mailbox does not exist"),
            ]
        )
        with build, run, sleep, himalaya.transport_stats_session() as stats:
            himalaya.run_himalaya(["envelope", "list"], max_retries=1)
            with self.assertRaises(HimalayaInvocationError):
                himalaya.run_himalaya(
                    ["message", "read", "--preview", "-f", "INBOX", "7071"],
                    max_retries=1,
                )

        snapshot = stats.snapshot()
        # The stable F6 counter shape stays exactly as before.
        self.assertEqual(
            {"starts": 1, "retries": 0, "errors": 0},
            snapshot["commands"]["envelope list"],
        )
        self.assertEqual(
            {"starts": 1, "retries": 0, "errors": 1},
            snapshot["commands"]["message read"],
        )
        telemetry = snapshot["command_telemetry"]
        self.assertEqual(1, telemetry["envelope list"]["starts"])
        self.assertEqual(0, telemetry["envelope list"]["last_exit_code"])
        read = telemetry["message read"]
        self.assertEqual(1, read["errors"])
        self.assertEqual(1, read["last_exit_code"])
        self.assertEqual("himalaya_command_failed", read["last_reason_code"])
        self.assertIsNotNone(read["last_duration_ms"])
        self.assertEqual(1, len(read["durations_ms"]))
        self.assertGreaterEqual(read["duration_ms_total"], read["last_duration_ms"])
        self.assertEqual(
            himalaya.TRANSPORT_DURATION_SAMPLES,
            snapshot["duration_samples_per_command"],
        )

    def test_duration_samples_are_bounded(self) -> None:
        payloads = [{"stdout": "", "returncode": 0} for _ in range(
            himalaya.TRANSPORT_DURATION_SAMPLES + 3
        )]
        build, run, sleep = _run_with(payloads)
        with build, run, sleep, himalaya.transport_stats_session() as stats:
            for _ in range(himalaya.TRANSPORT_DURATION_SAMPLES + 3):
                himalaya.run_himalaya(["folder", "list"], max_retries=1)

        samples = stats.snapshot()["command_telemetry"]["folder list"]["durations_ms"]
        self.assertEqual(himalaya.TRANSPORT_DURATION_SAMPLES, len(samples))

    def test_block_envelope_names_the_trigger_with_item_identity(self) -> None:
        def blocked_mode(*_args, **_kwargs):
            with himalaya.transport_stats_session() as stats:
                for env_id in ("7071", "7072", "7073", "7074"):
                    stats.record_error(
                        "message read",
                        "himalaya_timeout",
                        duration_ms=12_000,
                        item_scoped=True,
                        envelope_id=env_id,
                    )
                stats.record_error(
                    "message read",
                    "himalaya_timeout",
                    duration_ms=13_500,
                    item_scoped=True,
                    envelope_id="7075",
                )
                raise stats.blocked_error("message read")

        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            with patch.object(runner, "resolve_data_dir", return_value=data_dir), \
                    patch.object(
                        runner,
                        "resolve_final_index_path",
                        return_value=data_dir / "index.json",
                    ), \
                    patch.object(runner, "run_draft_mode", side_effect=blocked_mode), \
                    patch.object(
                        sys, "argv", ["mail_desk_batch_runner.py", "--draft", "1"]
                    ), \
                    patch("sys.stdout", stdout):
                code = runner.main()

        envelope = json.loads(stdout.getvalue())
        self.assertEqual(1, code)
        self.assertFalse(envelope["success"])
        self.assertEqual("TransportBlocked", envelope["error"]["type"])
        details = envelope["error"]["details"]
        self.assertEqual("mailbox_transport_blocked", details["reason_code"])
        self.assertEqual(
            himalaya.ITEM_FAILURE_BLOCK_REASON,
            details["transport_stats"]["block_reason"],
        )
        trigger = details["transport_trigger"]
        self.assertEqual("message read", trigger["command_class"])
        self.assertEqual("7075", trigger["envelope_id"])
        self.assertEqual(13_500, trigger["duration_ms"])
        self.assertIsNone(trigger["exit_code"])
        self.assertEqual("himalaya_timeout", trigger["reason_code"])


class ProgressLivenessTests(unittest.TestCase):
    """Point 3: a killed run is never read as still running."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)
        self.progress_file = self.data_dir / "runner-progress.json"

    def _write_running(self, *, pid: int, run_id: str = "draft_dead") -> None:
        state = {
            "schema_version": 1,
            "run_id": run_id,
            "mode": "draft",
            "status": "running",
            "pid": pid,
            "heartbeat_at": "2026-11-13T10:00:00Z",
            "started_at": "2026-11-13T09:59:00",
            "updated_at": "2026-11-13T10:00:00Z",
            "progress": {
                "total_items": 10,
                "completed_items": 2,
                "percent": 20.0,
                "current_step": "inspecting_email",
                "current_envelope_id": "7115",
                "current_subject": "slow item",
            },
            "timing": {
                "elapsed_seconds": 36.3,
                "avg_seconds_per_item": 4.0,
                "estimated_remaining_seconds": 32.0,
                "eta_timestamp": "2026-11-13 11:01:00",
                "recommended_next_timer_seconds": 30,
                "timer_rule": "Delta_t = max(30, min(0.75 * remaining_sec, 360))",
            },
            "error": None,
        }
        self.progress_file.write_text(json.dumps(state), encoding="utf-8")

    def _read(self) -> dict:
        return json.loads(self.progress_file.read_text(encoding="utf-8"))

    def test_running_state_with_dead_owner_is_settled_to_aborted(self) -> None:
        self._write_running(pid=DEAD_PID)

        state = settle_stale_runner_progress(self.progress_file)

        self.assertEqual("aborted", state["status"])
        self.assertEqual(PROGRESS_INTERRUPTED_REASON, state["error"])
        self.assertEqual("interrupted", state["progress"]["current_step"])
        self.assertEqual(0.0, state["timing"]["estimated_remaining_seconds"])
        self.assertIsNone(state["timing"]["eta_timestamp"])
        persisted = self._read()
        self.assertEqual("aborted", persisted["status"])
        self.assertNotEqual("running", persisted["status"])
        self.assertEqual(2, persisted["progress"]["completed_items"])

    def test_read_runner_progress_reports_aborted_without_writing(self) -> None:
        self._write_running(pid=DEAD_PID)

        state = read_runner_progress(self.progress_file)

        self.assertEqual("aborted", state["status"])
        self.assertEqual(PROGRESS_INTERRUPTED_REASON, state["error"])
        # The read-only view never mutates the file; the next leased tracker
        # (or an explicit settle) performs the atomic rewrite.
        self.assertEqual("running", self._read()["status"])

    def test_running_state_with_live_owner_is_never_rewritten(self) -> None:
        self._write_running(pid=os.getpid())

        state = settle_stale_runner_progress(self.progress_file)

        self.assertEqual("running", state["status"])
        self.assertEqual("running", self._read()["status"])

    def test_legacy_running_state_without_pid_is_left_unchanged(self) -> None:
        self._write_running(pid=0)
        state = self._read()
        state.pop("pid")
        self.progress_file.write_text(json.dumps(state), encoding="utf-8")

        settled = settle_stale_runner_progress(self.progress_file)

        self.assertEqual("running", settled["status"])
        self.assertEqual("running", self._read()["status"])

    def test_new_tracker_settles_the_killed_run_and_keeps_its_identity(self) -> None:
        self._write_running(pid=DEAD_PID)

        tracker = BatchProgressTracker(
            mode="draft", total_items=10, data_dir=self.data_dir, run_id="draft_new",
            console_log=False,
        )
        try:
            state = self._read()
            self.assertEqual("running", state["status"])
            self.assertEqual("draft_new", state["run_id"])
            previous = state["interrupted_previous_run"]
            self.assertEqual("draft_dead", previous["run_id"])
            self.assertEqual("interrupted", previous["status"])
            self.assertEqual(2, previous["completed_items"])
            self.assertNotEqual("draft_dead", state["run_id"])
        finally:
            tracker.abort("test cleanup")

    def test_abort_active_trackers_leaves_no_running_state(self) -> None:
        tracker = BatchProgressTracker(
            mode="draft", total_items=10, data_dir=self.data_dir, run_id="draft_live",
            console_log=False,
        )
        self.assertEqual("running", self._read()["status"])

        aborted = abort_active_trackers("interrupted: test kill")

        self.assertGreaterEqual(aborted, 1)
        self.assertEqual("aborted", self._read()["status"])
        self.assertEqual("interrupted: test kill", self._read()["error"])
        self.assertEqual(0, abort_active_trackers("interrupted: test kill"))

    def test_terminal_state_is_not_overwritten_by_exit_settlement(self) -> None:
        tracker = BatchProgressTracker(
            mode="draft", total_items=10, data_dir=self.data_dir, run_id="draft_done",
            console_log=False,
        )
        tracker.complete()
        self.assertEqual(0, abort_active_trackers("interrupted: test exit"))
        self.assertEqual("completed", self._read()["status"])


if __name__ == "__main__":
    unittest.main()
