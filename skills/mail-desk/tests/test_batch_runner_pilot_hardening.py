"""Focused regressions for the U-1 to U-5 batch-runner pilot hardening."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, call, patch


MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

import mail_desk_batch_runner as runner  # noqa: E402


class BatchRunnerPilotHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace_root = Path(self.temporary.name)
        self.data_dir = self.workspace_root / "data" / "mail-desk"
        self.data_dir.mkdir(parents=True)
        (self.workspace_root / ".agents").mkdir()
        (self.workspace_root / ".agents" / "mail-desk-backend.json").write_text(
            '{"schema_version": 1, "backend": "himalaya", "account": null}', encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_direct_cli_filters_are_forwarded_for_all_fetching_modes(self) -> None:
        parser = runner._build_parser()
        expected_modes = ("pipeline", "draft", "inspect")
        for mode in expected_modes:
            with self.subTest(mode=mode):
                arguments = [f"--{mode}", "2", "--query", "from pilot@example.test"]
                config = runner._direct_mode_config(parser.parse_args(arguments), self.data_dir)
                self.assertEqual("from pilot@example.test", config["query"])

                date_config = runner._direct_mode_config(
                    parser.parse_args([f"--{mode}", "2", "--date", "2026-05-18"]),
                    self.data_dir,
                )
                self.assertEqual("2026-05-18", date_config["date"])

    def test_inspect_draft_and_pipeline_pass_filters_to_fetch(self) -> None:
        with patch.object(runner, "get_unprocessed_emails", return_value=([], 0)) as fetch:
            runner.run_inspect_mode(
                {"count": 2, "query": "subject pilot"}, data_dir=self.data_dir,
            )
            self.assertEqual("subject pilot", fetch.call_args.kwargs["query"])

        with patch.object(runner, "get_unprocessed_emails", return_value=([], 0)) as fetch, patch.object(
            runner, "draft_manifest", return_value={"items": []},
        ):
            runner.run_draft_mode(
                {"count": 2, "date": "2026-05-18"}, data_dir=self.data_dir,
            )
            self.assertEqual("2026-05-18", fetch.call_args.kwargs["date"])

        with patch.object(runner, "get_unprocessed_emails", return_value=([], 0)) as fetch, patch.object(
            runner, "run_himalaya", return_value="[]",
        ):
            runner.run_pipeline_mode(
                {"count": 2, "query": "subject pilot"}, data_dir=self.data_dir,
            )
            self.assertEqual("subject pilot", fetch.call_args.kwargs["query"])

    def test_query_results_use_rfc_dates_not_lexicographic_order(self) -> None:
        envelopes = [
            {"id": "2", "date": "Mon, 5 Jan 2026 09:00:00 +0000", "subject": "later"},
            {"id": "1", "date": "Tue, 30 Dec 2025 09:00:00 +0000", "subject": "earlier"},
            {"id": "3", "date": "not a date", "subject": "undated"},
        ]

        def details(envelope_id, *_args, **_kwargs):
            return {"envelope_id": str(envelope_id), "message_id": f"{envelope_id}@example.test"}

        with patch.object(runner, "load_final_index", return_value={"items": {}}), patch.object(
            runner, "build_signature_index", return_value={},
        ), patch.object(runner, "run_himalaya", return_value=json.dumps(envelopes)), patch.object(
            runner, "get_single_email_details", side_effect=details,
        ), patch.object(runner.time, "sleep"):
            oldest, _ = runner.get_unprocessed_emails(
                "INBOX", 3, order="oldest", query="from pilot@example.test", data_dir=self.data_dir, skip_known=False,
            )
            newest, _ = runner.get_unprocessed_emails(
                "INBOX", 3, order="newest", query="from pilot@example.test", data_dir=self.data_dir, skip_known=False,
            )

        self.assertEqual(["1", "2", "3"], [item["envelope_id"] for item in oldest])
        self.assertEqual(["2", "1", "3"], [item["envelope_id"] for item in newest])

    def test_query_result_subject_can_contain_error_like_text(self) -> None:
        envelopes = [{"id": "1", "date": "Mon, 5 Jan 2026 09:00:00 +0000", "subject": "Error: cannot parse draft"}]

        with patch.object(runner, "load_final_index", return_value={"items": {}}), patch.object(
            runner, "build_signature_index", return_value={},
        ), patch.object(runner, "run_himalaya", return_value=json.dumps(envelopes)), patch.object(
            runner, "get_single_email_details", return_value={"envelope_id": "1", "message_id": "1@example.test"},
        ), patch.object(runner.time, "sleep"):
            emails, _ = runner.get_unprocessed_emails(
                "INBOX", 1, query="subject pilot", data_dir=self.data_dir, skip_known=False,
            )

        self.assertEqual(["1@example.test"], [email["message_id"] for email in emails])

    def test_known_mail_paging_reaches_the_first_unseen_message(self) -> None:
        known_items = {f"{number}@example.test": {} for number in range(1, 151)}
        requested_sizes: list[int] = []

        def list_envelopes(_folder, count, account=None):
            requested_sizes.append(count)
            highest = 150 if count < 300 else 151
            return [{"id": str(number), "subject": f"message {number}"} for number in range(1, highest + 1)]

        def details(envelope_id, *_args, **_kwargs):
            return {"envelope_id": str(envelope_id), "message_id": f"{envelope_id}@example.test"}

        with patch.object(runner, "load_final_index", return_value={"items": known_items}), patch.object(
            runner, "build_signature_index", return_value={},
        ), patch.object(runner, "get_oldest_envelopes", side_effect=list_envelopes), patch.object(
            runner, "get_single_email_details", side_effect=details,
        ), patch.object(runner.time, "sleep"):
            emails, known_count = runner.get_unprocessed_emails("INBOX", 1, data_dir=self.data_dir)

        self.assertEqual([25, 50, 150, 300], requested_sizes)
        self.assertEqual(["151@example.test"], [email["message_id"] for email in emails])
        self.assertEqual(150, known_count)

    def test_known_mail_paging_schedule_is_unique_monotonic_and_target_aware(self) -> None:
        normal = runner._known_mail_fetch_sizes(1)
        medium_target = runner._known_mail_fetch_sizes(100)
        large_target = runner._known_mail_fetch_sizes(3_000)

        self.assertEqual([25, 50, 150, 300, 600, 1_200, 2_500], normal)
        self.assertEqual(normal, sorted(set(normal)))
        self.assertEqual(200, medium_target[0])
        self.assertEqual(2_500, medium_target[-1])
        self.assertEqual(medium_target, sorted(set(medium_target)))
        self.assertEqual([3_000], large_target)

    def test_query_errors_are_reported_instead_of_silently_looking_empty(self) -> None:
        with patch.object(runner, "load_final_index", return_value={"items": {}}), patch.object(
            runner, "build_signature_index", return_value={},
        ), patch.object(runner, "run_himalaya", return_value="[{"):
            with self.assertRaisesRegex(RuntimeError, "invalid envelope JSON"):
                runner.get_unprocessed_emails(
                    "INBOX", 1, query="since definitely-not-a-date", data_dir=self.data_dir,
                )

    def test_permission_retries_use_bounded_exponential_backoff(self) -> None:
        operation = Mock(side_effect=[PermissionError("locked"), PermissionError("locked"), "ok"])
        with patch.object(runner.time, "sleep") as sleep:
            self.assertEqual("ok", runner._retry_permission_error(operation))

        self.assertEqual(3, operation.call_count)
        self.assertEqual([call(0.1), call(0.2)], sleep.call_args_list)

    def test_configuration_read_retries_transient_permission_error(self) -> None:
        manifest = self.data_dir / "inspect.json"
        manifest.write_text('{"mode": "inspect"}', encoding="utf-8")
        args = runner._build_parser().parse_args(["--input", str(manifest)])

        with patch.object(Path, "read_text", side_effect=[PermissionError("locked"), PermissionError("locked"), '{"mode": "inspect"}']) as read_text, patch.object(
            runner.time, "sleep",
        ) as sleep:
            config, input_path = runner._load_configuration(args, self.data_dir)

        self.assertEqual({"mode": "inspect"}, config)
        self.assertEqual(manifest.resolve(), input_path)
        self.assertEqual(3, read_text.call_count)
        self.assertEqual([call(0.1), call(0.2)], sleep.call_args_list)

    def test_cleanup_retries_transient_permission_error_and_removes_manifest(self) -> None:
        manifest = self.data_dir / "cleanup.json"
        manifest.write_text('{"mode": "inspect"}', encoding="utf-8")
        original_unlink = Path.unlink
        attempts = 0

        def locked_then_unlink(path, *args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise PermissionError("locked")
            return original_unlink(path, *args, **kwargs)

        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(runner, "resolve_data_dir", return_value=self.data_dir), patch.object(
            runner, "resolve_final_index_path", return_value=self.data_dir / "final-location-index.json",
        ), patch.object(runner, "run_inspect_mode", return_value={"ok": True, "mode": "inspect"}), patch.object(
            Path, "unlink", autospec=True, side_effect=locked_then_unlink,
        ), patch.object(runner.time, "sleep") as sleep, patch.object(
            sys, "argv", ["mail_desk_batch_runner.py", "--input", str(manifest)],
        ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(0, runner.main())

        self.assertEqual(3, attempts)
        self.assertEqual([call(0.1), call(0.2)], sleep.call_args_list)
        self.assertFalse(manifest.exists())

    def test_filter_flags_without_a_fetch_mode_are_rejected(self) -> None:
        parser = runner._build_parser()
        for option, value in (("--query", "subject pilot"), ("--date", "2026-05-18")):
            with self.subTest(option=option):
                args = parser.parse_args([option, value])
                with self.assertRaisesRegex(runner.ArgumentParseError, "require --inspect"):
                    runner._load_configuration(args, self.data_dir)


if __name__ == "__main__":
    unittest.main()
