"""FR-06c contracts for the post-batch LLM synthesis handoff."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

import mail_desk_batch_runner as runner  # noqa: E402
from core.modes import execute as execute_mode  # noqa: E402
from core.modes import pipeline as pipeline_mode  # noqa: E402
from core.synthesis_handoff import (  # noqa: E402
    canonicalize_synthesis_handoff,
    collect_synthesis_handoff,
    empty_synthesis_handoff,
)
from core.telemetry import collect_telemetry  # noqa: E402


def target(path: str) -> dict[str, str]:
    return {"file": path, "type": "statusampel"}


def item(
    envelope_id: str,
    *,
    kind: str = "project",
    identifier: str = "meshe",
    targets: list[dict[str, str]] | None = None,
    action: dict[str, str] | None = None,
) -> dict:
    result = {
        "envelope_id": envelope_id,
        "message_id": f"<{envelope_id}@EXAMPLE.TEST>",
        "subject": f"Subject {envelope_id}",
        "action": action or {"type": "keep_in_folder"},
        "decision": {"kind": kind, "id": identifier},
    }
    if targets is not None:
        result["synthesis_targets"] = targets
    return result


class SynthesisHandoffHelperTests(unittest.TestCase):
    def test_empty_and_malformed_values_are_canonical(self) -> None:
        expected = {"schema_version": 1, "status": "not_required", "items": []}
        self.assertEqual(expected, empty_synthesis_handoff())
        self.assertEqual(expected, canonicalize_synthesis_handoff(None))
        self.assertEqual(expected, canonicalize_synthesis_handoff({"schema_version": 1, "status": "pending", "items": []}))

    def test_collects_successful_pairs_in_input_order_without_deduplication(self) -> None:
        project_target = target("memory/references/projects/meshe/statusampel.md")
        items = [
            item("one", identifier=" meshe ", targets=[project_target]),
            item("two", identifier="meshe", targets=[]),
            item("three", kind="inbox-review", targets=[]),
            item("four", kind="topic", identifier="dienstreisen", targets=[]),
        ]
        results = [
            {"success": True, "message_id": "<ONE@example.test>", "subject": "First", "synthesis_targets": [project_target]},
            {"success": True, "message_id": "two@example.test", "subject": "Second", "synthesis_targets": []},
            {"success": True, "message_id": "three@example.test", "synthesis_targets": []},
            {"success": False, "message_id": "four@example.test", "synthesis_targets": []},
        ]

        handoff = collect_synthesis_handoff(items, results)

        self.assertEqual("pending", handoff["status"])
        self.assertEqual(["one@example.test", "two@example.test"], [row["message_id"] for row in handoff["items"]])
        self.assertEqual(["meshe", "meshe"], [row["id"] for row in handoff["items"]])
        self.assertFalse(handoff["items"][0]["target_selection_required"])
        self.assertTrue(handoff["items"][1]["target_selection_required"])

    def test_canonicalizer_requires_exact_targets_and_selection_flag(self) -> None:
        handoff = collect_synthesis_handoff(
            [item("one", targets=[])],
            [{"success": True, "message_id": "one@example.test", "subject": "", "synthesis_targets": []}],
        )
        self.assertEqual(handoff, canonicalize_synthesis_handoff(handoff))
        handoff["items"][0]["target_selection_required"] = False
        self.assertEqual(empty_synthesis_handoff(), canonicalize_synthesis_handoff(handoff))

    def test_telemetry_contract_is_unchanged(self) -> None:
        entries = [item("one", identifier="meshe"), item("two", kind="topic", identifier="dienstreisen")]
        results = [{"success": True}, {"success": True}]
        self.assertEqual(
            {"affected_projects": ["meshe"], "affected_topics": ["dienstreisen"], "synthesis_required": True},
            collect_telemetry(entries, results),
        )


class SynthesisHandoffModeTests(unittest.TestCase):
    @staticmethod
    def execute_dependencies(**overrides: Mock) -> dict:
        dependencies = {
            "BatchProgressTracker": Mock(),
            "append_action_log_entry": Mock(),
            "auto_resolve_replies_from_sent": Mock(),
            "flush_batch_evidence": Mock(),
            "load_final_index": Mock(return_value={"items": {}}),
            "run_himalaya": Mock(),
            "save_final_index_atomic": Mock(),
            "sleep": Mock(),
            "verify_in_target_folder": Mock(return_value="copied"),
        }
        dependencies.update(overrides)
        return dependencies

    def test_execute_partial_failure_keeps_candidate_but_never_releases_handoff(self) -> None:
        good = item("good", targets=[])
        failed = item("failed", action={"type": "copy_as_move", "target_folder": "Projects/Other"}, targets=[])
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            result = execute_mode.run_execute_mode(
                {"items": [good, failed]},
                data_dir=data_dir,
                index_path=data_dir / "index.json",
                dependencies=self.execute_dependencies(run_himalaya=Mock(side_effect=RuntimeError("copy failed"))),
            )

        self.assertFalse(result["ok"])
        self.assertEqual(empty_synthesis_handoff(), result["synthesis_handoff"])
        self.assertEqual("pending", result["synthesis_candidate"]["status"])
        self.assertEqual(["good@example.test"], [row["message_id"] for row in result["synthesis_candidate"]["items"]])

    @staticmethod
    def pipeline_dependencies(execute_result: dict, *, emails: list[dict] | None = None, verify_result: dict | None = None) -> dict:
        executable = item("pipeline", targets=[])
        executable["decision"]["confidence"] = "high"
        executable["action"]["target_folder"] = "Projects/MESHE"
        return {
            "get_unprocessed_emails": Mock(return_value=((emails if emails is not None else [{"envelope_id": "pipeline"}]), 0)),
            "load_sent_index": Mock(return_value={}),
            "draft_manifest": Mock(return_value={"items": [executable]}),
            "run_execute_mode": Mock(return_value=execute_result),
            "run_verify_mode": Mock(return_value=verify_result or {"ok": True, "results": []}),
        }

    def test_pipeline_verify_failure_requires_recovery_and_releases_nothing(self) -> None:
        handoff = collect_synthesis_handoff(
            [item("pipeline", targets=[])],
            [{"success": True, "message_id": "pipeline@example.test", "subject": "Pipeline", "synthesis_targets": []}],
        )
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            result = pipeline_mode.run_pipeline_mode(
                {"verify": True, "sync_sent": False},
                data_dir=data_dir,
                dependencies=self.pipeline_dependencies(
                    {"ok": True, "results": [], "synthesis_handoff": handoff},
                    verify_result={"ok": False, "results": []},
                ),
            )

        self.assertFalse(result["ok"])
        self.assertTrue(result["recovery_required"])
        self.assertEqual(empty_synthesis_handoff(), result["synthesis_handoff"])
        self.assertEqual("recovery_required", result["completion_report"]["status"])

    def test_pipeline_releases_one_source_bound_handoff_only_after_full_verify(self) -> None:
        handoff = collect_synthesis_handoff(
            [item("pipeline", targets=[])],
            [{"success": True, "message_id": "pipeline@example.test", "subject": "Pipeline", "synthesis_targets": []}],
        )
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            result = pipeline_mode.run_pipeline_mode(
                {"verify": True, "sync_sent": False},
                data_dir=data_dir,
                dependencies=self.pipeline_dependencies(
                    {"ok": True, "results": [], "synthesis_candidate": handoff},
                    verify_result={"ok": True, "results": [{"message_id": "pipeline@example.test", "consistent": True}]},
                ),
            )

        self.assertTrue(result["ok"])
        self.assertEqual(handoff, result["synthesis_handoff"])
        self.assertEqual("completed", result["completion_report"]["status"])

    def test_pipeline_uses_empty_handoff_for_no_mail_no_execute_legacy_and_malformed_execute(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            no_mail = pipeline_mode.run_pipeline_mode(
                {"sync_sent": False},
                data_dir=data_dir,
                dependencies={"get_unprocessed_emails": Mock(return_value=([], 0))},
            )
            no_execute = pipeline_mode.run_pipeline_mode(
                {"verify": False, "sync_sent": False},
                data_dir=data_dir,
                dependencies={
                    "get_unprocessed_emails": Mock(return_value=([{"envelope_id": "review"}], 0)),
                    "load_sent_index": Mock(return_value={}),
                    "draft_manifest": Mock(return_value={"items": [item("review", kind="inbox-review")]}),
                },
            )
            legacy = pipeline_mode.run_pipeline_mode(
                {"verify": False, "sync_sent": False},
                data_dir=data_dir,
                dependencies=self.pipeline_dependencies({"ok": True, "results": []}),
            )
            malformed = pipeline_mode.run_pipeline_mode(
                {"verify": False, "sync_sent": False},
                data_dir=data_dir,
                dependencies=self.pipeline_dependencies({"ok": True, "results": [], "synthesis_handoff": {"status": "pending"}}),
            )

        for result in (no_mail, no_execute, legacy, malformed):
            self.assertEqual(empty_synthesis_handoff(), result["synthesis_handoff"])


class SynthesisHandoffCliTests(unittest.TestCase):
    def test_cli_places_handoff_under_data_only(self) -> None:
        handoff = collect_synthesis_handoff(
            [item("cli", targets=[])],
            [{"success": True, "message_id": "cli@example.test", "subject": "CLI", "synthesis_targets": []}],
        )
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            stdout = io.StringIO()
            with patch.object(runner, "resolve_data_dir", return_value=data_dir), patch.object(
                runner, "resolve_final_index_path", return_value=data_dir / "index.json"
            ), patch.object(
                runner, "run_execute_mode", return_value={"ok": True, "mode": "execute", "synthesis_handoff": handoff}
            ), patch.object(sys, "argv", ["mail_desk_batch_runner.py", "--stdin"]), patch.object(
                sys, "stdin", io.StringIO(json.dumps({"mode": "execute"}))
            ), contextlib.redirect_stdout(stdout):
                self.assertEqual(0, runner.main())

        envelope = json.loads(stdout.getvalue())
        self.assertEqual(handoff, envelope["data"]["synthesis_handoff"])
        self.assertNotIn("synthesis_handoff", envelope)


if __name__ == "__main__":
    unittest.main()
