"""FR-17 / MD-R6 behavior tests for bounded Himalaya client deadlines.

Evidence mode ``tdd``, risk tier ``high``.  ``Target...`` classes define the
MD-R6 semantics and are Red before the production change in
``core.himalaya``; the ``...Characterization`` classes pin single-command
behavior that must stay green before and after.

B-8 reproduction: ``mail_desk_himalaya_client.py --input`` with
``{"action": "search", "query": ...}`` and no ``folders`` hung for more than five
minutes with no structured timeout.  ``search_mailbox`` issues one Himalaya
command per folder, each bounded only by its own subprocess timeout, and a
per-folder failure is swallowed at ``core/himalaya.py:602`` so the sweep keeps
going across every folder.  The operation therefore has no overall budget.

Intended MD-R6 contract encoded here:
  * a multi-folder operation honours an ``overall_deadline_seconds`` wall-clock
    budget and aborts with a bounded ``himalaya_timeout`` error instead of
    hanging; its reason distinguishes the overall deadline from a per-call
    timeout;
  * a per-call ``himalaya_timeout`` stops the multi-folder sweep fail-closed
    instead of being swallowed and retried across folders;
  * deadline exhaustion is terminal: no folder is attempted twice and no partial
    result is returned as if the sweep were complete;
  * ``mail_desk_himalaya_client.op_search`` accepts and forwards the deadline;
  * an invalid overall deadline (non-finite, non-positive or non-numeric) fails
    closed with a bounded ``ValueError`` before the first mailbox command, so a
    NaN/manifest value can never silently disable the sweep bound; ``None`` stays
    the documented disable;
  * single-command ``run_himalaya`` semantics stay unchanged (``timeout`` default
    35, ``max_retries`` default 5, immediate terminal ``himalaya_timeout`` on a
    per-call timeout, bounded transport retry only).

The keyword seam ``overall_deadline_seconds`` is introduced by the MD-R6
production dispatch; until then the deadline tests fail with an explicit
assertion naming the missing seam.  Everything here is hermetic: the Himalaya
command invoker is faked, no subprocess or network is used, and every injected
delay stays at 0.1 s or below.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mail_desk_himalaya_client as client  # noqa: E402
from core import himalaya  # noqa: E402
from himalaya_fixtures import (  # noqa: E402
    DEFAULT_FOLDERS,
    deadline_case,
    himalaya_timeout_error,
)

#: The overall-deadline keyword seam introduced by the MD-R6 production change.
DEADLINE_KWARG = "overall_deadline_seconds"

#: Per-call injected delay; small enough that the whole module stays well under 5 s.
PER_CALL_SECONDS = 0.1

#: Overall budget crossed after the second injected call.
OVERALL_DEADLINE_SECONDS = 0.15

#: Generous post-deadline bound proving the sweep stopped early (today: six calls).
BOUNDED_ELAPSED_SECONDS = 0.45


def _has_deadline_seam(callable_object: object) -> bool:
    """Whether a callable exposes the MD-R6 overall-deadline keyword seam."""
    parameters = inspect.signature(callable_object).parameters
    if DEADLINE_KWARG in parameters:
        return True
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )


class CharacterizationSingleCommandContractTests(unittest.TestCase):
    """Green now and after: single ``run_himalaya`` commands stay as they are."""

    def test_healthy_single_command_returns_stdout(self) -> None:
        """A healthy command returns its stdout unchanged."""
        healthy = subprocess.CompletedProcess(["himalaya"], 0, '{"ok": true}', "")
        with patch.object(
            himalaya, "build_himalaya_command", return_value=["himalaya"]
        ), patch.object(himalaya.subprocess, "run", return_value=healthy):
            output = himalaya.run_himalaya(["-o", "json", "folder", "list"])
        self.assertEqual('{"ok": true}', output)

    def test_per_call_timeout_is_terminal_himalaya_timeout_without_retry(self) -> None:
        """A per-call timeout raises ``himalaya_timeout`` after one attempt, never retrying."""
        timeout = subprocess.TimeoutExpired(["himalaya"], 10)
        with patch.object(
            himalaya, "build_himalaya_command", return_value=["himalaya"]
        ), patch.object(himalaya.subprocess, "run", side_effect=timeout) as run, patch.object(
            himalaya.time, "sleep"
        ) as sleep:
            with self.assertRaises(himalaya.HimalayaInvocationError) as raised:
                himalaya.run_himalaya(["folder", "list"], max_retries=5)
        self.assertEqual("himalaya_timeout", raised.exception.reason_code)
        self.assertIsInstance(raised.exception.__cause__, subprocess.TimeoutExpired)
        self.assertEqual(1, run.call_count)
        sleep.assert_not_called()
        self.assertNotIn("deadline", str(raised.exception).lower())

    def test_invalid_timeout_and_retry_bounds_are_rejected(self) -> None:
        """Non-positive or non-integer bounds fail fast with ``ValueError``."""
        with patch.object(
            himalaya, "build_himalaya_command", return_value=["himalaya"]
        ):
            for bad_timeout in (0, -1, 1.5, "35", None):
                with self.subTest(timeout=bad_timeout):
                    with self.assertRaises(ValueError):
                        himalaya.run_himalaya(
                            ["folder", "list"], timeout=bad_timeout, max_retries=1
                        )
            for bad_retries in (0, -2, 1.5, "3"):
                with self.subTest(max_retries=bad_retries):
                    with self.assertRaises(ValueError):
                        himalaya.run_himalaya(
                            ["folder", "list"], timeout=35, max_retries=bad_retries
                        )

    def test_single_command_defaults_are_unchanged(self) -> None:
        """The published ``timeout`` and ``max_retries`` defaults stay 35 and 5."""
        parameters = inspect.signature(himalaya.run_himalaya).parameters
        self.assertEqual(35, parameters["timeout"].default)
        self.assertEqual(5, parameters["max_retries"].default)

    def test_transient_transport_error_retries_within_existing_bound(self) -> None:
        """A clear transport error retries once and recovers, per the existing policy."""
        transient = subprocess.CompletedProcess(["himalaya"], 1, "", "TLS stream reset")
        healthy = subprocess.CompletedProcess(["himalaya"], 0, "[]", "")
        with patch.object(
            himalaya, "build_himalaya_command", return_value=["himalaya"]
        ), patch.object(
            himalaya.subprocess, "run", side_effect=[transient, healthy]
        ) as run, patch.object(himalaya.time, "sleep") as sleep:
            output = himalaya.run_himalaya(["folder", "list"], max_retries=2)
        self.assertEqual("[]", output)
        self.assertEqual(2, run.call_count)
        sleep.assert_called_once_with(2.0)

    def test_single_folder_sweep_hits_each_folder_exactly_once(self) -> None:
        """A successful multi-folder search does one sweep, one command per folder."""
        runner = deadline_case(["INBOX", "Junk", "Trash"])
        with patch.object(himalaya, "run_himalaya", runner):
            matches = himalaya.search_mailbox(
                query="anything", folders=["INBOX", "Junk", "Trash"], threads=1
            )
        self.assertEqual([], matches)
        self.assertEqual(["INBOX", "Junk", "Trash"], runner.swept_folders)
        self.assertEqual(0, len(runner.folder_list_calls))


class TargetOverallDeadlineTests(unittest.TestCase):
    """Red now: a multi-folder search must honour an overall deadline."""

    def test_b8_search_without_folders_aborts_at_overall_deadline(self) -> None:
        """B-8 analog: slow per-folder successes still abort at the overall deadline."""
        if not _has_deadline_seam(himalaya.search_mailbox):
            self.fail(
                "search_mailbox must accept overall_deadline_seconds "
                "(MD-R6 overall-deadline seam) so a folder sweep cannot hang"
            )
        runner = deadline_case(DEFAULT_FOLDERS, per_call_seconds=PER_CALL_SECONDS)
        started = time.monotonic()
        with patch.object(himalaya, "run_himalaya", runner):
            with self.assertRaises(himalaya.HimalayaInvocationError) as raised:
                himalaya.search_mailbox(
                    query="Besuch von Univ. Kenia",
                    folders=None,
                    threads=1,
                    overall_deadline_seconds=OVERALL_DEADLINE_SECONDS,
                )
        elapsed = time.monotonic() - started
        self.assertEqual("himalaya_timeout", raised.exception.reason_code)
        self.assertIn("deadline", str(raised.exception).lower())
        self.assertLess(elapsed, BOUNDED_ELAPSED_SECONDS)
        self.assertGreaterEqual(len(runner.swept_folders), 1)
        self.assertLess(len(runner.swept_folders), len(DEFAULT_FOLDERS))

    def test_overall_deadline_error_is_bounded_and_distinct_from_per_call(self) -> None:
        """The overall-deadline failure is a bounded ``himalaya_timeout`` naming the deadline."""
        if not _has_deadline_seam(himalaya.search_mailbox):
            self.fail(
                "search_mailbox must accept overall_deadline_seconds "
                "(MD-R6 overall-deadline seam) and bound the sweep"
            )
        runner = deadline_case(DEFAULT_FOLDERS, per_call_seconds=PER_CALL_SECONDS)
        with patch.object(himalaya, "run_himalaya", runner):
            with self.assertRaises(himalaya.HimalayaInvocationError) as raised:
                himalaya.search_mailbox(
                    query="BOKU batch",
                    threads=1,
                    overall_deadline_seconds=OVERALL_DEADLINE_SECONDS,
                )
        error = raised.exception
        self.assertEqual("himalaya_timeout", error.reason_code)
        self.assertIn("deadline", str(error).lower())
        self.assertLessEqual(len(str(error)), 1000)

    def test_deadline_exhaustion_is_terminal_without_second_sweep(self) -> None:
        """Deadline exhaustion aborts terminally: no folder is attempted twice."""
        if not _has_deadline_seam(himalaya.search_mailbox):
            self.fail(
                "search_mailbox must accept overall_deadline_seconds "
                "(MD-R6 overall-deadline seam) and stop after the deadline"
            )
        runner = deadline_case(DEFAULT_FOLDERS, per_call_seconds=PER_CALL_SECONDS)
        with patch.object(himalaya, "run_himalaya", runner):
            with self.assertRaises(himalaya.HimalayaInvocationError):
                himalaya.search_mailbox(
                    query="BOKU batch",
                    threads=1,
                    overall_deadline_seconds=OVERALL_DEADLINE_SECONDS,
                )
        swept = runner.swept_folders
        self.assertEqual(1, len(runner.folder_list_calls))
        self.assertGreaterEqual(len(swept), 1)
        self.assertEqual(len(swept), len(set(swept)))
        self.assertLess(len(swept), len(DEFAULT_FOLDERS))

    def test_client_op_search_forwards_the_overall_deadline(self) -> None:
        """The client dispatch seam accepts and forwards the overall deadline."""
        if not _has_deadline_seam(client.op_search):
            self.fail(
                "op_search must accept overall_deadline_seconds "
                "(MD-R6 client deadline seam) and forward it to search_mailbox"
            )
        with patch.object(client, "search_mailbox", return_value=[]) as search:
            result = client.op_search(
                query="Besuch von Univ. Kenia",
                folders=None,
                account="demo",
                overall_deadline_seconds=0.25,
            )
        self.assertEqual([], result)
        self.assertEqual(
            0.25, search.call_args.kwargs[DEADLINE_KWARG]
        )


class TargetPerCallTimeoutStopsSweepTests(unittest.TestCase):
    """Red now: a per-call timeout must stop the multi-folder sweep fail-closed."""

    def test_multi_folder_search_stops_after_first_per_call_timeout(self) -> None:
        """After one per-call ``himalaya_timeout`` no further folder command is issued."""
        runner = deadline_case(
            ["INBOX", "Junk", "Trash"],
            timeout_error=himalaya_timeout_error(),
        )
        with patch.object(himalaya, "run_himalaya", runner):
            with self.assertRaises(himalaya.HimalayaInvocationError) as raised:
                himalaya.search_mailbox(
                    query="BOKU", folders=["INBOX", "Junk", "Trash"], threads=1
                )
        self.assertEqual("himalaya_timeout", raised.exception.reason_code)
        self.assertEqual(1, len(runner.swept_folders))
        self.assertEqual(["INBOX"], runner.swept_folders)


class TargetOverallDeadlineValidationTests(unittest.TestCase):
    """Red now: an invalid overall deadline must fail closed before any command.

    A NaN/Inf/0/negative or non-numeric ``overall_deadline_seconds`` must never
    silently disable or neuter the sweep bound: the value is rejected at entry
    with a bounded ``ValueError`` and zero Himalaya invocations.  ``None`` stays
    the documented disable and still sweeps every folder.
    """

    def test_none_overall_deadline_disables_the_bound_and_sweeps_all_folders(self) -> None:
        """``None`` remains the documented disable: every folder is swept once."""
        runner = deadline_case(DEFAULT_FOLDERS, per_call_seconds=PER_CALL_SECONDS)
        with patch.object(himalaya, "run_himalaya", runner):
            matches = himalaya.search_mailbox(
                query="irrelevant",
                folders=None,
                threads=1,
                overall_deadline_seconds=None,
            )
        self.assertEqual([], matches)
        self.assertEqual(list(DEFAULT_FOLDERS), runner.swept_folders)

    def test_non_positive_overall_deadline_fails_closed_before_any_command(self) -> None:
        """0/negative budgets fail closed at entry and issue no mailbox command."""
        for bad in (0, -1, -0.5):
            with self.subTest(overall_deadline_seconds=bad):
                runner = deadline_case(DEFAULT_FOLDERS, per_call_seconds=PER_CALL_SECONDS)
                with patch.object(himalaya, "run_himalaya", runner):
                    with self.assertRaises(ValueError) as raised:
                        himalaya.search_mailbox(
                            query="BOKU",
                            folders=None,
                            threads=1,
                            overall_deadline_seconds=bad,
                        )
                self.assertIn(DEADLINE_KWARG, str(raised.exception))
                self.assertLessEqual(len(str(raised.exception)), 1000)
                self.assertEqual(0, runner.call_count)

    def test_non_finite_overall_deadline_is_rejected_before_any_command(self) -> None:
        """NaN/Inf budgets are rejected at entry instead of neutering the bound."""
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(overall_deadline_seconds=bad):
                runner = deadline_case(DEFAULT_FOLDERS, per_call_seconds=PER_CALL_SECONDS)
                with patch.object(himalaya, "run_himalaya", runner):
                    with self.assertRaises(ValueError) as raised:
                        himalaya.search_mailbox(
                            query="BOKU",
                            folders=None,
                            threads=1,
                            overall_deadline_seconds=bad,
                        )
                self.assertIn(DEADLINE_KWARG, str(raised.exception))
                self.assertLessEqual(len(str(raised.exception)), 1000)
                self.assertEqual(0, runner.call_count)

    def test_non_numeric_overall_deadline_is_rejected_before_any_command(self) -> None:
        """Non-numeric budgets fail closed rather than raising a deep ``TypeError``."""
        for bad in ("35", []):
            with self.subTest(overall_deadline_seconds=bad):
                runner = deadline_case(DEFAULT_FOLDERS, per_call_seconds=PER_CALL_SECONDS)
                with patch.object(himalaya, "run_himalaya", runner):
                    with self.assertRaises(ValueError):
                        himalaya.search_mailbox(
                            query="BOKU",
                            folders=None,
                            threads=1,
                            overall_deadline_seconds=bad,
                        )
                self.assertEqual(0, runner.call_count)

    def test_manifest_invalid_overall_deadline_fails_closed_bounded(self) -> None:
        """A NaN manifest budget yields a bounded failure envelope, never a sweep."""
        runner = deadline_case(DEFAULT_FOLDERS, per_call_seconds=PER_CALL_SECONDS)
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "op.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "operations": [
                            {
                                "action": "search",
                                "query": "BOKU",
                                "overall_deadline_seconds": float("nan"),
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(himalaya, "run_himalaya", runner):
                result = client.execute_manifest(manifest_path)
            self.assertTrue(manifest_path.exists())
        self.assertFalse(result["all_succeeded"])
        self.assertEqual(1, result["total_operations"])
        failure = result["results"][0]
        self.assertFalse(failure["success"])
        self.assertIn(DEADLINE_KWARG, str(failure["error"]))
        self.assertLessEqual(len(str(failure["error"])), 1000)
        self.assertEqual(0, runner.call_count)


if __name__ == "__main__":
    unittest.main()
