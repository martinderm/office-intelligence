"""Contract tests for the batch-runner's OI-10 CLI envelope boundary."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

import mail_desk_batch_runner as runner  # noqa: E402


CANONICAL_KEYS = ("action", "success", "state", "message", "data", "error")


class BatchRunnerEnvelopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name) / "data" / "mail-desk"
        self.data_dir.mkdir(parents=True)
        self.index_path = self.data_dir / "final-location-index.json"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def invoke(
        self,
        arguments: list[str],
        *,
        stdin: str = "",
        handler_result=None,
        handler_error=None,
        unlink_error: BaseException | None = None,
    ):
        stdout = io.StringIO()
        stderr = io.StringIO()

        def successful_handler(*_args, **_kwargs):
            print("progress belongs on stderr")
            if handler_error is not None:
                raise handler_error
            return handler_result or {"ok": True, "mode": "legacy", "diagnostic": "kept below data"}

        patches = (
            patch.object(runner, "resolve_data_dir", return_value=self.data_dir),
            patch.object(runner, "resolve_final_index_path", return_value=self.index_path),
            patch.object(runner, "run_inspect_mode", side_effect=successful_handler),
            patch.object(runner, "run_draft_mode", side_effect=successful_handler),
            patch.object(runner, "run_sync_sent_mode", side_effect=successful_handler),
            patch.object(runner, "run_pipeline_mode", side_effect=successful_handler),
            patch.object(runner, "run_execute_mode", side_effect=successful_handler),
            patch.object(runner, "run_verify_mode", side_effect=successful_handler),
            patch.object(runner, "run_search_mode", side_effect=successful_handler),
            patch.object(runner, "run_resolve_mode", side_effect=successful_handler),
            patch.object(sys, "argv", ["mail_desk_batch_runner.py", *arguments]),
            patch.object(sys, "stdin", io.StringIO(stdin)),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        )
        with contextlib.ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            if unlink_error is not None:
                stack.enter_context(patch.object(Path, "unlink", side_effect=unlink_error))
            exit_code = runner.main()

        self.assertEqual(1, len(stdout.getvalue().splitlines()), stdout.getvalue())
        return exit_code, json.loads(stdout.getvalue()), stderr.getvalue()

    def assert_envelope(self, envelope: dict, *, operation: str | None = None) -> None:
        self.assertEqual(CANONICAL_KEYS, tuple(envelope))
        self.assertEqual("batch_runner", envelope["action"])
        self.assertNotIn("ok", envelope)
        self.assertNotIn("status", envelope)
        self.assertNotIn("resolved", envelope)
        if operation is not None:
            self.assertEqual(operation, envelope["data"]["operation"])

    def write_manifest(self, name: str, mode: str) -> Path:
        path = self.data_dir / name
        path.write_text(json.dumps({"mode": mode}), encoding="utf-8")
        return path

    def test_all_manifest_modes_and_aliases_are_canonical_and_clean_up_on_success(self) -> None:
        aliases = {
            "inspect": ("inspect", "fetch"),
            "draft": ("draft", "propose"),
            "sync_sent": ("sync_sent", "sync-sent", "sent"),
            "pipeline": ("pipeline", "auto"),
            "execute": ("execute", "process"),
            "verify": ("verify", "validate", "check"),
            "search": ("search", "locate", "find"),
            "resolve": ("resolve", "archive"),
        }
        for operation, modes in aliases.items():
            for mode in modes:
                with self.subTest(mode=mode):
                    manifest = self.write_manifest(f"{mode}.json", mode)
                    exit_code, envelope, stderr = self.invoke(["--input", str(manifest)])
                    self.assertEqual(0, exit_code)
                    self.assert_envelope(envelope, operation=operation)
                    self.assertTrue(envelope["success"])
                    self.assertEqual("Completed", envelope["state"])
                    self.assertTrue(envelope["data"]["input_file_deleted"])
                    self.assertFalse(manifest.exists())
                    self.assertIn("progress belongs on stderr", stderr)

    def test_direct_cli_modes_are_canonical(self) -> None:
        direct_modes = {
            "pipeline": (["--pipeline", "3"], "pipeline"),
            "draft": (["--draft", "3"], "draft"),
            "inspect": (["--inspect", "3"], "inspect"),
            "sync_sent": (["--sync-sent", "3"], "sync_sent"),
            "resolve": (["--resolve"], "resolve"),
        }
        for name, (arguments, operation) in direct_modes.items():
            with self.subTest(name=name):
                exit_code, envelope, _ = self.invoke(arguments)
                self.assertEqual(0, exit_code)
                self.assert_envelope(envelope, operation=operation)
                self.assertTrue(envelope["success"])

        exit_code, envelope, stderr = self.invoke(["--help"])
        self.assertEqual(0, exit_code)
        self.assert_envelope(envelope, operation="help")
        self.assertIn("usage:", stderr)
        self.assertIn("--pipeline", stderr)

    def test_stdin_success_and_error_paths_are_canonical(self) -> None:
        exit_code, envelope, _ = self.invoke(["--stdin"], stdin=json.dumps({"mode": "inspect"}))
        self.assertEqual(0, exit_code)
        self.assert_envelope(envelope, operation="inspect")

        for payload, text in (("", "empty"), ("{", "not valid JSON")):
            with self.subTest(payload=payload):
                exit_code, envelope, _ = self.invoke(["--stdin"], stdin=payload)
                self.assertEqual(2, exit_code)
                self.assert_envelope(envelope, operation="argument_parse")
                self.assertFalse(envelope["success"])
                self.assertEqual("ArgumentError", envelope["error"]["type"])
                self.assertIn(text, envelope["message"])

    def test_missing_invalid_unknown_and_unexpected_inputs_keep_the_manifest(self) -> None:
        exit_code, envelope, _ = self.invoke([])
        self.assertEqual(1, exit_code)
        self.assert_envelope(envelope, operation="input_load")
        self.assertEqual("NotFound", envelope["state"])

        missing = self.data_dir / "missing.json"
        exit_code, envelope, _ = self.invoke(["--input", str(missing)])
        self.assertEqual(1, exit_code)
        self.assert_envelope(envelope, operation="input_load")
        self.assertEqual("NotFound", envelope["state"])

        invalid = self.data_dir / "invalid.json"
        invalid.write_text("{", encoding="utf-8")
        exit_code, envelope, _ = self.invoke(["--input", str(invalid)])
        self.assertEqual(2, exit_code)
        self.assert_envelope(envelope, operation="argument_parse")
        self.assertTrue(invalid.exists())

        unknown = self.write_manifest("unknown.json", "unrecognized")
        exit_code, envelope, _ = self.invoke(["--input", str(unknown)])
        self.assertEqual(2, exit_code)
        self.assert_envelope(envelope, operation="argument_parse")
        self.assertIn("Unsupported mode", envelope["message"])
        self.assertTrue(unknown.exists())

        unexpected = self.write_manifest("unexpected.json", "inspect")
        exit_code, envelope, _ = self.invoke(
            ["--input", str(unexpected)],
            handler_error=RuntimeError("simulated handler failure"),
        )
        self.assertEqual(1, exit_code)
        self.assert_envelope(envelope, operation="runtime")
        self.assertEqual("RuntimeError", envelope["error"]["type"])
        self.assertTrue(unexpected.exists())

        exit_code, envelope, stderr = self.invoke(["--invalid-option"])
        self.assertEqual(2, exit_code)
        self.assert_envelope(envelope, operation="argument_parse")
        self.assertEqual("ArgumentError", envelope["error"]["type"])
        self.assertIn("usage:", stderr)
        self.assertIn("unrecognized arguments", stderr)

    def test_partial_failure_preserves_input_and_nests_diagnostics(self) -> None:
        manifest = self.write_manifest("partial.json", "execute")
        result = {
            "ok": False,
            "mode": "execute",
            "results": [{"success": True, "id": "first"}, {"success": False, "id": "second"}],
            "all_succeeded": False,
        }
        exit_code, envelope, _ = self.invoke(["--input", str(manifest)], handler_result=result)

        self.assertEqual(1, exit_code)
        self.assert_envelope(envelope, operation="execute")
        self.assertEqual("PartialFailure", envelope["state"])
        self.assertEqual("PartialFailure", envelope["error"]["type"])
        self.assertEqual(result["results"], envelope["data"]["results"])
        self.assertTrue(manifest.exists())

    def test_execute_telemetry_is_nested_under_envelope_data(self) -> None:
        manifest = self.write_manifest("telemetry.json", "execute")
        telemetry = {
            "affected_projects": ["meshe"],
            "affected_topics": ["dienstreisen"],
            "synthesis_required": True,
        }
        exit_code, envelope, _ = self.invoke(
            ["--input", str(manifest)],
            handler_result={"ok": True, "mode": "execute", "results": [], "telemetry": telemetry},
        )

        self.assertEqual(0, exit_code)
        self.assert_envelope(envelope, operation="execute")
        self.assertEqual(telemetry, envelope["data"]["telemetry"])
        self.assertNotIn("telemetry", envelope)

    def test_verify_and_pipeline_mixed_outcomes_are_partial_failures(self) -> None:
        verify = self.write_manifest("verify-partial.json", "verify")
        verify_result = {
            "ok": False,
            "mode": "verify",
            "results": [{"consistent": True, "message_id": "first@example.test"}, {"consistent": False, "message_id": "second@example.test"}],
        }
        exit_code, envelope, _ = self.invoke(["--input", str(verify)], handler_result=verify_result)
        self.assertEqual(1, exit_code)
        self.assert_envelope(envelope, operation="verify")
        self.assertEqual("PartialFailure", envelope["state"])
        self.assertTrue(verify.exists())

        pipeline = self.write_manifest("pipeline-partial.json", "pipeline")
        pipeline_result = {
            "ok": False,
            "mode": "pipeline",
            "execute_summary": {"ok": False, "results": [{"success": True}, {"success": False}]},
            "verify_summary": {"ok": False, "results": [{"consistent": True}, {"consistent": False}]},
        }
        exit_code, envelope, _ = self.invoke(["--input", str(pipeline)], handler_result=pipeline_result)
        self.assertEqual(1, exit_code)
        self.assert_envelope(envelope, operation="pipeline")
        self.assertEqual("PartialFailure", envelope["state"])
        self.assertTrue(pipeline.exists())

    def test_pipeline_completed_execute_and_failed_verify_is_partial_failure(self) -> None:
        pipeline = self.write_manifest("pipeline-verify-failure.json", "pipeline")
        pipeline_result = {
            "ok": False,
            "mode": "pipeline",
            "execute_summary": {"ok": True, "results": [{"success": True}, {"success": True}]},
            "verify_summary": {"ok": False, "results": [{"consistent": False}, {"consistent": False}]},
        }
        exit_code, envelope, _ = self.invoke(["--input", str(pipeline)], handler_result=pipeline_result)

        self.assertEqual(1, exit_code)
        self.assert_envelope(envelope, operation="pipeline")
        self.assertEqual("PartialFailure", envelope["state"])
        self.assertEqual("PartialFailure", envelope["error"]["type"])
        self.assertTrue(pipeline.exists())

    def test_keep_input_preserves_successful_manifest(self) -> None:
        manifest = self.write_manifest("keep.json", "inspect")
        exit_code, envelope, _ = self.invoke(["--input", str(manifest), "--keep-input"])

        self.assertEqual(0, exit_code)
        self.assert_envelope(envelope, operation="inspect")
        self.assertFalse(envelope["data"]["input_file_deleted"])
        self.assertTrue(manifest.exists())

    def test_cleanup_failure_is_partial_failure_and_preserves_manifest(self) -> None:
        manifest = self.write_manifest("cleanup-failure.json", "inspect")
        exit_code, envelope, _ = self.invoke(
            ["--input", str(manifest)],
            unlink_error=OSError("simulated cleanup failure"),
        )

        self.assertEqual(1, exit_code)
        self.assert_envelope(envelope, operation="inspect")
        self.assertEqual("PartialFailure", envelope["state"])
        self.assertEqual("CleanupFailure", envelope["error"]["type"])
        self.assertFalse(envelope["data"]["input_file_deleted"])
        self.assertTrue(manifest.exists())

    def test_serialization_failure_is_an_error_and_preserves_successful_manifest(self) -> None:
        manifest = self.write_manifest("serialization-failure.json", "inspect")
        result = {"ok": True, "mode": "inspect", "unserializable": object()}
        exit_code, envelope, _ = self.invoke(["--input", str(manifest)], handler_result=result)

        self.assertEqual(1, exit_code)
        self.assert_envelope(envelope, operation="inspect")
        self.assertFalse(envelope["success"])
        self.assertEqual("SerializationError", envelope["error"]["type"])
        self.assertTrue(manifest.exists())


if __name__ == "__main__":
    unittest.main()
