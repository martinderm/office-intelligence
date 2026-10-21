"""Bounded header diagnostics: synthetic responses, no mailbox or subprocess."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from core import himalaya


class VerifyHeaderDiagnosticsTests(unittest.TestCase):
    def verify(self, diagnostics, *, hint=None):
        return himalaya.verify_in_target_folder(
            "Target", "<wanted@example.invalid>", subject="Synthetic subject",
            account="synthetic-account", candidate_env_id=hint, diagnostics=diagnostics,
        )

    def listing(self, count=7, subject="Synthetic subject"):
        return json.dumps([{"id": str(i), "subject": subject} for i in range(count)])

    def test_fast_path_match_records_only_safe_fields_and_elapsed_time(self):
        facts = {}
        with patch.object(himalaya, "run_himalaya", return_value="Message-ID: <wanted@example.invalid>\n") as run, \
                patch.object(himalaya.time, "monotonic", side_effect=[10.0, 11.25]):
            self.assertEqual(self.verify(facts, hint="private-envelope"), "private-envelope")
        self.assertEqual(facts["header_reads"], [{"phase": "fast_path", "elapsed_ms": 1250, "outcome": "match"}])
        self.assertNotIn("wanted", json.dumps(facts))
        self.assertNotIn("private-envelope", json.dumps(facts))
        run.assert_called_once_with(
            ["message", "read", "--preview", "-H", "Message-Id", "-f", "Target", "private-envelope"],
            account="synthetic-account", timeout=10, max_retries=1,
        )

    def test_candidates_keep_order_and_stop_on_exact_message_id(self):
        facts = {}
        with patch.object(himalaya, "run_himalaya", side_effect=[self.listing(3),
                "Message-ID: <wrong@example.invalid>", "Message-ID: <wanted@example.invalid>"]) as run:
            self.assertEqual(self.verify(facts), "1")
        self.assertEqual([row["outcome"] for row in facts["header_reads"]], ["mismatch", "match"])
        self.assertTrue(all(row["phase"] == "candidate" for row in facts["header_reads"]))
        self.assertEqual([call.args[0][-1] for call in run.call_args_list[1:]], ["2", "1"])
        self.assertEqual(facts["envelopes_seen"], 3)
        self.assertEqual(facts["candidate_count"], 3)

    def test_missing_header_does_not_verify(self):
        facts = {}
        with patch.object(himalaya, "run_himalaya", side_effect=[self.listing(1), "Subject: Synthetic subject"]):
            self.assertIsNone(self.verify(facts))
        self.assertEqual(facts["header_reads"][0]["outcome"], "missing_message_id")

    def test_known_timeouts_are_classified_without_raw_exception_text(self):
        errors = [TimeoutError("private error text"), subprocess.TimeoutExpired("private-command", 10),
                  himalaya.HimalayaInvocationError("himalaya_timeout", "private diagnostic")]
        for error in errors:
            with self.subTest(error=type(error)), patch.object(himalaya, "run_himalaya",
                    side_effect=[self.listing(1), error]):
                facts = {}
                self.assertIsNone(self.verify(facts))
            self.assertEqual(facts["header_reads"][0]["outcome"], "timeout")
            self.assertNotIn("private", json.dumps(facts))

    def test_other_failures_are_error_not_timeout(self):
        for error in (RuntimeError("private"), himalaya.HimalayaInvocationError("himalaya_launch_failed", "private")):
            with self.subTest(error=type(error)), patch.object(himalaya, "run_himalaya",
                    side_effect=[self.listing(1), error]):
                facts = {}
                self.assertIsNone(self.verify(facts))
            self.assertEqual(facts["header_reads"][0]["outcome"], "error")
            self.assertNotIn("private", json.dumps(facts))

    def test_six_header_bound_including_failed_hint_and_unchanged_budgets(self):
        facts = {}
        def response(args, **kwargs):
            return self.listing(150) if "list" in args else "Message-ID: <other@example.invalid>"
        with patch.object(himalaya, "run_himalaya", side_effect=response) as run:
            self.assertIsNone(self.verify(facts, hint="known"))
        self.assertEqual(len(facts["header_reads"]), 6)
        self.assertEqual(run.call_count, 7)
        for call in run.call_args_list:
            expected = (30, 2) if "list" in call.args[0] else (10, 1)
            self.assertEqual((call.kwargs["timeout"], call.kwargs["max_retries"]), expected)
            self.assertEqual(call.kwargs["account"], "synthetic-account")
            self.assertEqual(call.args[0][call.args[0].index("-f") + 1], "Target")
        self.assertEqual(facts["candidate_count"], 150)

    def test_fallback_has_five_header_bound_and_accurate_phase(self):
        facts = {}
        with patch.object(himalaya, "run_himalaya", side_effect=[self.listing(8, "Other")] + ["No ID"] * 5) as run:
            self.assertIsNone(self.verify(facts))
        self.assertEqual(len(facts["header_reads"]), 5)
        self.assertEqual(run.call_count, 6)
        self.assertTrue(all(row["phase"] == "fallback" for row in facts["header_reads"]))

    def test_reused_collector_does_not_accumulate_across_invocations(self):
        facts = {"caller_tag": "keep"}
        with patch.object(himalaya, "run_himalaya", return_value="Message-ID: <wanted@example.invalid>"):
            for _ in range(10):
                self.assertEqual(self.verify(facts, hint="known"), "known")
                self.assertEqual(len(facts["header_reads"]), 1)
        self.assertEqual(facts["caller_tag"], "keep")

    def test_listing_failure_does_not_keep_previous_header_diagnostics(self):
        facts = {"header_reads": [{"outcome": "old"}]}
        with patch.object(himalaya, "run_himalaya", side_effect=RuntimeError("private")):
            self.assertIsNone(self.verify(facts))
        self.assertEqual(facts["header_reads"], [])

    def test_no_collector_preserves_return_and_argv_without_timing_calls(self):
        calls = []
        for facts in (None, {}):
            with patch.object(himalaya, "run_himalaya", side_effect=[self.listing(1),
                    "Message-ID: <wanted@example.invalid>"]) as run, \
                    patch.object(himalaya.time, "monotonic", return_value=1.0) as clock:
                self.assertEqual(self.verify(facts), "0")
            calls.append(run.call_args_list)
            if facts is None:
                clock.assert_not_called()
        self.assertEqual(calls[0], calls[1])

    def test_invalid_header_response_is_error(self):
        facts = {}
        with patch.object(himalaya, "run_himalaya", side_effect=[self.listing(1), None]):
            self.assertIsNone(self.verify(facts))
        self.assertEqual(facts["header_reads"][0]["outcome"], "error")

    def test_multiple_header_lines_preserve_existing_match_semantics(self):
        facts = {}
        with patch.object(himalaya, "run_himalaya", return_value=(
                "Message-ID: <wrong@example.invalid>\nMessage-ID: <wanted@example.invalid>\n")):
            self.assertEqual(self.verify(facts, hint="known"), "known")
        self.assertEqual(facts["header_reads"][0]["outcome"], "match")


if __name__ == "__main__":
    unittest.main()
