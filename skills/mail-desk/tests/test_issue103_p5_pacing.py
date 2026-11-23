"""Issue #103, slice 5 (P5): configurable pacing and transient backoff.

All tests are hermetic: no real child process, injected monotone clock/sleep.
Defaults stay ``transport_pacing_ms=0`` and ``transport_transient_backoff_s=2.0``.
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
from core.known_skip import EnvelopeThreadEntry  # noqa: E402
from core.modes import draft as draft_mode  # noqa: E402
import mail_desk_batch_runner as runner  # noqa: E402
from transport_hermetic_guard import HermeticProcessTestCase  # noqa: E402


MESSAGE_ID = "p5-61@example.test"


class _FakeClock:
    """Deterministic monotone clock whose sleep advances time."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []
        self.on_sleep = None

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if self.on_sleep is not None:
            self.on_sleep(seconds)
        self.now += seconds


class _FakeProcess:
    def __init__(self, cmd, *, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.cmd = cmd
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.instances = 0

    def communicate(self, input=None, timeout=None):  # noqa: A002 - Popen signature
        return self.stdout, self.stderr

    def kill(self) -> None:
        pass


def _popen_delegate(cmd, **_kwargs):
    return _FakeProcess(cmd, stdout="[]")


class PacingTests(unittest.TestCase):
    """Points 1-4 and 8: the central start-process seam."""

    def _stats(self, **kwargs) -> tuple[himalaya.TransportStats, _FakeClock, list[float]]:
        clock = _FakeClock()
        kwargs.setdefault("pacing_ms", 250)
        stats = himalaya.TransportStats(clock=clock, sleep=clock.sleep, **kwargs)
        starts: list[float] = []
        return stats, clock, starts

    def test_first_start_does_not_wait_second_waits_remaining(self) -> None:
        stats, clock, starts = self._stats(pacing_ms=250)

        def factory():
            starts.append(clock.now)
            return object()

        stats.start_process("envelope list", factory, purpose="list")
        clock.now += 0.1
        stats.start_process("envelope list", factory, purpose="list")

        self.assertEqual([0.0, 0.25], starts)
        self.assertEqual([0.15], clock.sleeps)
        snapshot = stats.snapshot()
        self.assertEqual(1, snapshot["pacing_waits"])
        self.assertEqual(150, snapshot["pacing_wait_ms_total"])
        self.assertEqual(250, snapshot["pacing_ms"])

    def test_zero_pacing_never_sleeps_and_keeps_the_defaults(self) -> None:
        stats, clock, _starts = self._stats(pacing_ms=0)

        def factory():
            return object()

        stats.start_process("envelope list", factory, purpose="list")
        stats.start_process("envelope list", factory, purpose="list")
        self.assertEqual([], clock.sleeps)
        snapshot = stats.snapshot()
        self.assertEqual(0, snapshot["pacing_waits"])
        self.assertEqual(0, snapshot["pacing_wait_ms_total"])
        self.assertEqual(0, snapshot["pacing_ms"])
        self.assertEqual(2.0, snapshot["transient_backoff_s"])

    def test_map_skip_starts_no_process_and_never_sleeps(self) -> None:
        clock = _FakeClock()
        sleeps: list[float] = []
        clock.sleep = lambda seconds: sleeps.append(seconds)  # type: ignore[method-assign]
        with patch.object(
            himalaya, "build_himalaya_command", side_effect=lambda command, account=None: ["himalaya", *command]
        ), patch.object(
            himalaya, "_POPEN", side_effect=lambda cmd, **kw: _FakeProcess(cmd, stdout=json.dumps([_envelope()]))
        ), patch.object(
            himalaya.time, "sleep"
        ), patch.object(
            runner, "build_signature_index", return_value={}
        ), patch.object(
            runner, "load_final_index", return_value={"items": {MESSAGE_ID: {}}}
        ), patch.object(
            runner,
            "load_envelope_thread_map",
            return_value={"61": EnvelopeThreadEntry(message_id=MESSAGE_ID, subject="Kurzfrage zum Vorgang")},
        ), himalaya.transport_stats_session(pacing_ms=250, clock=clock, sleep=clock.sleep) as stats:
            collected, known, skipped = runner._scan_unprocessed_emails(
                "INBOX", 1, account="primary", skip_known=True
            )
        self.assertEqual([], collected)
        self.assertEqual(1, known)
        self.assertEqual(1, skipped)
        self.assertEqual(1, stats.snapshot()["process_starts"])  # only the envelope list
        self.assertEqual([], sleeps)
        self.assertEqual(0, stats.snapshot()["pacing_waits"])

    def test_block_published_during_the_wait_stops_before_the_start(self) -> None:
        stats, clock, starts = self._stats(pacing_ms=250, max_consecutive_errors=1)

        def factory():
            starts.append(clock.now)
            return object()

        stats.start_process("envelope list", factory, purpose="list")

        def block(_seconds: float) -> None:
            with stats._lock:
                stats.blocked_reason = "two_consecutive_transport_errors"

        clock.on_sleep = block
        with self.assertRaises(himalaya.HimalayaInvocationError) as raised:
            stats.start_process("envelope list", factory, purpose="list")
        self.assertEqual(1, len(starts))
        self.assertEqual(
            "two_consecutive_transport_errors",
            raised.exception.details["block_reason"],
        )
        snapshot = stats.snapshot()
        self.assertTrue(snapshot["blocked"])
        self.assertEqual(1, snapshot["pacing_waits"])

    def test_budget_exhausted_during_the_wait_stops_before_the_start(self) -> None:
        stats, clock, starts = self._stats(pacing_ms=250, start_budget=2)

        def factory():
            starts.append(clock.now)
            return object()

        stats.start_process("envelope list", factory, purpose="list")

        def exhaust(_seconds: float) -> None:
            with stats._lock:
                stats.process_starts = 2

        clock.on_sleep = exhaust
        with self.assertRaises(himalaya.HimalayaInvocationError):
            stats.start_process("envelope list", factory, purpose="list")
        self.assertEqual(1, len(starts))
        self.assertEqual("start_budget_exhausted", stats.snapshot()["block_reason"])

    def test_payload_and_diagnostic_starts_do_not_pace(self) -> None:
        clock = _FakeClock()
        calls: list[str] = []

        def fake_popen(cmd, **kwargs):
            calls.append("start")
            return _FakeProcess(cmd, stdout="OK")

        with patch.object(
            himalaya, "build_himalaya_command", side_effect=lambda command, account=None: ["himalaya", *command]
        ), patch.object(himalaya, "_POPEN", side_effect=fake_popen), himalaya.transport_stats_session(
            pacing_ms=250, clock=clock, sleep=clock.sleep
        ) as stats:
            himalaya.run_himalaya(
                ["message", "save", "-o", "plain", "-f", "Drafts"],
                stdin_bytes=b"From: a@example.test\r\n\r\nBody\r\n",
            )
            stats.start_process(
                "diagnose", lambda: calls.append("start") or _FakeProcess([]), pace=False, purpose="other"
            )
        self.assertEqual(2, len(calls))
        self.assertEqual([], clock.sleeps)
        self.assertEqual(0, stats.snapshot()["pacing_waits"])
        self.assertEqual(2, stats.snapshot()["process_starts"])

    def test_failed_factory_keeps_reference_and_counters(self) -> None:
        stats, clock, starts = self._stats(pacing_ms=250)

        def factory():
            starts.append(clock.now)
            return object()

        stats.start_process("envelope list", factory, purpose="list")
        clock.now += 0.1

        def failing_factory():
            raise RuntimeError("launch failed")

        with self.assertRaises(RuntimeError):
            stats.start_process("envelope list", failing_factory, purpose="list")
        snapshot = stats.snapshot()
        self.assertEqual(1, snapshot["process_starts"])
        self.assertEqual(1, snapshot["pacing_waits"])  # the wait happened, the start did not
        self.assertEqual(1, snapshot["starts_by_purpose"]["list"])

        clock.now += 0.1
        stats.start_process("envelope list", factory, purpose="list")
        # The reference is still t=0, so the next start waits only for the
        # already-consumed gap and adds no further sleep.
        self.assertEqual([0.15], clock.sleeps)
        self.assertEqual([0.0, clock.sleeps[0] + 0.2], starts)

    def test_pace_false_start_does_not_move_the_reference(self) -> None:
        stats, clock, starts = self._stats(pacing_ms=250)

        def factory():
            starts.append(clock.now)
            return object()

        stats.start_process("envelope list", factory, purpose="list")
        clock.now += 0.1
        stats.start_process("payload", lambda: starts.append(clock.now) or object(), pace=False)
        clock.now += 0.05
        stats.start_process("envelope list", factory, purpose="list")
        self.assertEqual([0.1], [round(seconds, 6) for seconds in clock.sleeps])
        self.assertEqual([0.0, 0.1, 0.25], starts)
        self.assertEqual(1, stats.snapshot()["pacing_waits"])

    def test_metrics_are_additive_and_carry_no_content(self) -> None:
        stats, _clock, _starts = self._stats(pacing_ms=250)

        def factory():
            return object()

        stats.start_process("envelope list", factory, purpose="list")
        snapshot = stats.snapshot()
        for key in ("pacing_ms", "transient_backoff_s", "pacing_waits", "pacing_wait_ms_total"):
            self.assertIn(key, snapshot)
        self.assertIsInstance(snapshot["pacing_waits"], int)
        self.assertIsInstance(snapshot["pacing_wait_ms_total"], int)
        self.assertNotIn("argv", snapshot)
        self.assertEqual(
            snapshot["process_starts"], sum(snapshot["starts_by_purpose"].values())
        )


class RetryBackoffTests(HermeticProcessTestCase):
    """Points 3, 5 and 7: the retry backoff stays bounded and configured."""

    def setUp(self) -> None:
        super().setUp()
        himalaya.reset_transport_stats()

    def _transient_then_success(self, clock: _FakeClock):
        calls: list[str] = []

        def fake_popen(cmd, **_kwargs):
            calls.append("start")
            if len(calls) == 1:
                return _FakeProcess(cmd, stderr="connection reset by peer", returncode=1)
            return _FakeProcess(cmd, stdout="[]", returncode=0)

        return fake_popen, calls

    def test_transient_retry_pays_backoff_and_pacing_before_the_second_start(self) -> None:
        clock = _FakeClock()
        fake_popen, calls = self._transient_then_success(clock)
        with patch.object(
            himalaya, "build_himalaya_command", side_effect=lambda command, account=None: ["himalaya", *command]
        ), patch.object(himalaya, "_POPEN", side_effect=fake_popen), himalaya.transport_stats_session(
            pacing_ms=250, transient_backoff_s=0.05, clock=clock, sleep=clock.sleep
        ) as stats:
            himalaya.run_himalaya(["envelope", "list"], purpose="list", max_retries=2)
        self.assertEqual(2, len(calls))
        self.assertEqual([0.05, 0.2], clock.sleeps)
        snapshot = stats.snapshot()
        self.assertEqual(2, snapshot["process_starts"])
        self.assertEqual(1, snapshot["retries"])
        self.assertEqual(1, snapshot["pacing_waits"])
        self.assertEqual(2, snapshot["starts_by_purpose"]["list"])

    def test_retry_backoff_factor_grows_with_the_attempt(self) -> None:
        clock = _FakeClock()
        calls: list[str] = []

        def fake_popen(cmd, **_kwargs):
            calls.append("start")
            if len(calls) <= 2:
                return _FakeProcess(cmd, stderr="connection reset by peer", returncode=1)
            return _FakeProcess(cmd, stdout="[]", returncode=0)

        with patch.object(
            himalaya, "build_himalaya_command", side_effect=lambda command, account=None: ["himalaya", *command]
        ), patch.object(himalaya, "_POPEN", side_effect=fake_popen), himalaya.transport_stats_session(
            pacing_ms=0, transient_backoff_s=0.5, clock=clock, sleep=clock.sleep
        ) as stats:
            stats.max_consecutive_errors = 3  # observe the attempt>=1 factor
            himalaya.run_himalaya(["envelope", "list"], purpose="list", max_retries=3)
        self.assertEqual(3, len(calls))
        self.assertEqual([0.5, 1.0], clock.sleeps)
        self.assertEqual(0.5, stats.retry_backoff_seconds(0))
        self.assertEqual(1.0, stats.retry_backoff_seconds(1))
        self.assertEqual(2, stats.snapshot()["retries"])

    def test_session_without_injection_resolves_time_sleep_at_call_time(self) -> None:
        sleeps: list[float] = []
        calls: list[str] = []

        def fake_popen(cmd, **_kwargs):
            calls.append("start")
            if len(calls) == 1:
                return _FakeProcess(cmd, stderr="connection reset by peer", returncode=1)
            return _FakeProcess(cmd, stdout="[]", returncode=0)

        with patch.object(
            himalaya, "build_himalaya_command", side_effect=lambda command, account=None: ["himalaya", *command]
        ), patch.object(himalaya, "_POPEN", side_effect=fake_popen), patch.object(
            himalaya.time, "sleep", side_effect=sleeps.append
        ), himalaya.transport_stats_session() as stats:
            himalaya.run_himalaya(["envelope", "list"], purpose="list", max_retries=2)
        self.assertEqual([2.0], sleeps)
        self.assertEqual(2, len(calls))
        self.assertEqual(1, stats.snapshot()["retries"])

    def test_timeout_has_no_retry_and_no_backoff(self) -> None:
        clock = _FakeClock()
        calls: list[str] = []

        class _TimeoutProcess(_FakeProcess):
            def __init__(self, cmd) -> None:
                super().__init__(cmd)
                self._timeout_raised = False

            def communicate(self, input=None, timeout=None):  # noqa: A002
                if not self._timeout_raised:
                    self._timeout_raised = True
                    raise subprocess.TimeoutExpired(cmd=self.cmd, timeout=timeout)
                return "", ""

        def fake_popen(cmd, **_kwargs):
            calls.append("start")
            return _TimeoutProcess(cmd)

        with patch.object(
            himalaya, "build_himalaya_command", side_effect=lambda command, account=None: ["himalaya", *command]
        ), patch.object(himalaya, "_POPEN", side_effect=fake_popen), himalaya.transport_stats_session(
            pacing_ms=250, transient_backoff_s=0.05, clock=clock, sleep=clock.sleep
        ) as stats:
            with self.assertRaises(himalaya.HimalayaInvocationError) as raised:
                himalaya.run_himalaya(["message", "read", "-f", "INBOX", "61"], purpose="scan_preview")
        self.assertEqual("himalaya_timeout", raised.exception.reason_code)
        self.assertEqual(1, len(calls))
        self.assertEqual([], clock.sleeps)

    def test_non_transient_failure_keeps_the_unchanged_shape(self) -> None:
        clock = _FakeClock()
        calls: list[str] = []

        def fake_popen(cmd, **_kwargs):
            calls.append("start")
            return _FakeProcess(cmd, stderr="mailbox not found", returncode=1)

        with patch.object(
            himalaya, "build_himalaya_command", side_effect=lambda command, account=None: ["himalaya", *command]
        ), patch.object(himalaya, "_POPEN", side_effect=fake_popen), himalaya.transport_stats_session(
            pacing_ms=250, transient_backoff_s=0.05, clock=clock, sleep=clock.sleep
        ):
            with self.assertRaises(himalaya.HimalayaInvocationError) as raised:
                himalaya.run_himalaya(["envelope", "list"], purpose="list")
        self.assertEqual("himalaya_command_failed", raised.exception.reason_code)
        self.assertEqual(1, len(calls))
        self.assertEqual([], clock.sleeps)

    def test_defaults_are_unchanged_without_a_session(self) -> None:
        sleeps: list[float] = []
        calls: list[str] = []

        def fake_run(cmd, **_kwargs):
            calls.append("start")
            if len(calls) == 1:
                return subprocess.CompletedProcess(cmd, 1, "", "connection reset by peer")
            return subprocess.CompletedProcess(cmd, 0, "[]", "")

        with patch.object(
            himalaya, "build_himalaya_command", side_effect=lambda command, account=None: ["himalaya", *command]
        ), patch.object(himalaya.subprocess, "run", side_effect=fake_run), patch.object(
            himalaya.time, "sleep", side_effect=sleeps.append
        ):
            self.assertIsNone(himalaya.current_transport_stats())
            himalaya.run_himalaya(["envelope", "list"], purpose="list", max_retries=2)
        self.assertEqual([2.0], sleeps)
        self.assertEqual(2, len(calls))

    def test_default_session_snapshot_has_the_documented_bounds(self) -> None:
        with himalaya.transport_stats_session() as stats:
            snapshot = stats.snapshot()
        self.assertEqual(0, snapshot["pacing_ms"])
        self.assertEqual(2.0, snapshot["transient_backoff_s"])
        self.assertEqual(0, snapshot["pacing_waits"])
        self.assertEqual(0, snapshot["pacing_wait_ms_total"])


class ConfigValidationTests(HermeticProcessTestCase):
    """Point 6: invalid pacing/backoff values fail before any process."""

    def setUp(self) -> None:
        super().setUp()
        himalaya.reset_transport_stats()

    def test_transport_stats_rejects_invalid_values(self) -> None:
        for kwargs in (
            {"pacing_ms": -1},
            {"pacing_ms": True},
            {"pacing_ms": 1.5},
            {"pacing_ms": himalaya.MAX_TRANSPORT_PACING_MS + 1},
            {"pacing_ms": 10**400},
            {"transient_backoff_s": 0},
            {"transient_backoff_s": -2.0},
            {"transient_backoff_s": True},
            {"transient_backoff_s": float("nan")},
            {"transient_backoff_s": float("inf")},
            {"transient_backoff_s": float("-inf")},
            {"transient_backoff_s": 10**400},
            {"clock": "clock"},
            {"sleep": "sleep"},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    himalaya.TransportStats(**kwargs)
        self.assertEqual(
            himalaya.MAX_TRANSPORT_PACING_MS,
            himalaya.TransportStats(pacing_ms=himalaya.MAX_TRANSPORT_PACING_MS).pacing_ms,
        )

    def test_runner_helpers_reject_invalid_values(self) -> None:
        with self.assertRaises(ValueError):
            runner._transport_pacing_ms({"transport_pacing_ms": -1})
        with self.assertRaises(ValueError):
            runner._transport_pacing_ms({"transport_pacing_ms": True})
        for value in (himalaya.MAX_TRANSPORT_PACING_MS + 1, 10**400):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    runner._transport_pacing_ms({"transport_pacing_ms": value})
        self.assertEqual(
            himalaya.MAX_TRANSPORT_PACING_MS,
            runner._transport_pacing_ms(
                {"transport_pacing_ms": himalaya.MAX_TRANSPORT_PACING_MS}
            ),
        )
        with self.assertRaises(ValueError):
            runner._transport_transient_backoff_s({"transport_transient_backoff_s": 0})
        with self.assertRaises(ValueError):
            runner._transport_transient_backoff_s({"transport_transient_backoff_s": "fast"})
        for value in (float("nan"), float("inf"), float("-inf"), 10**400):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    runner._transport_transient_backoff_s(
                        {"transport_transient_backoff_s": value}
                    )

    def test_draft_rejects_pacing_before_any_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(himalaya, "_POPEN") as popen:
                with self.assertRaises(ValueError):
                    draft_mode.run_draft_mode(
                        {"count": 1, "transport_pacing_ms": -1},
                        data_dir=Path(temporary) / "data" / "mail-desk",
                    )
                for value in (himalaya.MAX_TRANSPORT_PACING_MS + 1, 10**400):
                    with self.assertRaises(ValueError):
                        draft_mode.run_draft_mode(
                            {"count": 1, "transport_pacing_ms": value},
                            data_dir=Path(temporary) / "data" / "mail-desk",
                        )
                for value in (float("nan"), float("inf"), float("-inf"), 10**400):
                    with self.assertRaises(ValueError):
                        draft_mode.run_draft_mode(
                            {
                                "count": 1,
                                "transport_transient_backoff_s": value,
                            },
                            data_dir=Path(temporary) / "data" / "mail-desk",
                        )
        popen.assert_not_called()

    def test_cli_rejects_invalid_pacing_and_requires_a_mailbox_mode(self) -> None:
        parser = runner._build_parser()
        for value in (-1, himalaya.MAX_TRANSPORT_PACING_MS + 1):
            with self.subTest(value=value):
                with self.assertRaises(runner.ArgumentParseError):
                    runner._direct_mode_config(
                        parser.parse_args(["--inspect", f"--transport-pacing-ms={value}"]),
                        Path("."),
                    )
        huge_pacing = parser.parse_args(["--inspect"])
        huge_pacing.transport_pacing_ms = 10**400
        with self.assertRaises(runner.ArgumentParseError):
            runner._direct_mode_config(huge_pacing, Path("."))
        with self.assertRaises(runner.ArgumentParseError):
            runner._direct_mode_config(
                parser.parse_args(["--transport-pacing-ms=250", "--execute"]), Path(".")
            )
        for value in ("nan", "inf", "-inf"):
            with self.subTest(value=value):
                with self.assertRaises(runner.ArgumentParseError):
                    runner._direct_mode_config(
                        parser.parse_args(
                            ["--inspect", f"--transport-transient-backoff-s={value}"]
                        ),
                        Path("."),
                    )
        huge = parser.parse_args(["--inspect"])
        huge.transport_transient_backoff_s = 10**400
        with self.assertRaises(runner.ArgumentParseError):
            runner._direct_mode_config(huge, Path("."))

    def test_cli_accepts_the_values_for_the_mailbox_modes(self) -> None:
        parser = runner._build_parser()
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data" / "mail-desk"
            config = runner._direct_mode_config(
                parser.parse_args(
                    [
                        "--inspect",
                        "--transport-pacing-ms=250",
                        "--transport-transient-backoff-s=5.0",
                    ]
                ),
                data_dir,
            )
            at_limit = runner._direct_mode_config(
                parser.parse_args(
                    [
                        "--inspect",
                        f"--transport-pacing-ms={himalaya.MAX_TRANSPORT_PACING_MS}",
                    ]
                ),
                data_dir,
            )
        self.assertIsNotNone(config)
        self.assertEqual(250, config["transport_pacing_ms"])
        self.assertEqual(5.0, config["transport_transient_backoff_s"])
        self.assertIsNotNone(at_limit)
        self.assertEqual(
            himalaya.MAX_TRANSPORT_PACING_MS, at_limit["transport_pacing_ms"]
        )


def _envelope() -> dict:
    return {
        "id": "61",
        "subject": "Kurzfrage zum Vorgang",
        "from": {"name": "Sender", "addr": "sender@example.test"},
        "date": "2026-01-01",
    }


class StandaloneClientScopeTests(unittest.TestCase):
    """Point 9: the standalone client stays outside the pacing session."""

    def test_client_module_has_no_pacing_session_seam(self) -> None:
        source = (MAIL_DESK_ROOT / "scripts" / "mail_desk_himalaya_client.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("transport_stats_session", source)
        self.assertNotIn("transport_pacing_ms", source)
        self.assertNotIn("transport-transient-backoff-s", source)


if __name__ == "__main__":
    unittest.main()
