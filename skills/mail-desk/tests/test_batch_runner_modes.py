"""Behavior and compatibility tests for extracted batch-runner modes."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

import mail_desk_batch_runner as runner  # noqa: E402
from core.modes import draft as draft_mode  # noqa: E402
from core.modes import execute as execute_mode  # noqa: E402
from core.modes import inspect as inspect_mode  # noqa: E402
from core.modes import pipeline as pipeline_mode  # noqa: E402
from core.modes import resolve as resolve_mode  # noqa: E402
from core.modes import search as search_mode  # noqa: E402
from core.modes import sync_sent as sync_sent_mode  # noqa: E402
from core.modes import verify as verify_mode  # noqa: E402


class SearchModeTests(unittest.TestCase):
    def test_search_preserves_result_shape_and_atomic_output(self) -> None:
        matches = [{"message_id": "one@example.test"}]
        with tempfile.TemporaryDirectory() as temporary:
            output_path = Path(temporary) / "nested" / "search.json"
            config = {
                "query": "subject pilot",
                "message_ids": ["one@example.test"],
                "folders": ["INBOX"],
                "page_size": 25,
                "threads": 2,
                "output_file": str(output_path),
            }
            with patch.object(search_mode, "search_mailbox", return_value=matches) as search:
                result = search_mode.run_search_mode(config, account="primary")

            search.assert_called_once_with(
                query="subject pilot",
                message_ids=["one@example.test"],
                folders=["INBOX"],
                page_size=25,
                threads=2,
                account="primary",
            )
            self.assertEqual({"ok": True, "mode": "search", "total_found": 1, "matches": matches}, result)
            self.assertEqual(result, json.loads(output_path.read_text(encoding="utf-8")))


class ResolveModeTests(unittest.TestCase):
    def test_single_message_does_not_mutate_caller_items(self) -> None:
        config = {
            "items": [],
            "message_id": "case@example.test",
            "status": "resolved",
            "resolution": "answered",
            "resolved_by_message_id": "reply@example.test",
        }
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            resolve_mode,
            "resolve_case",
            return_value={"resolved": True},
        ) as resolve:
            result = resolve_mode.run_resolve_mode(config, data_dir=Path(temporary))

        self.assertEqual([], config["items"])
        self.assertTrue(result["ok"])
        self.assertEqual(1, result["total_processed"])
        resolve.assert_called_once_with(
            Path(temporary),
            "case@example.test",
            status="resolved",
            resolution="answered",
            resolved_by="reply@example.test",
        )

    def test_auto_audit_preserves_result_shape(self) -> None:
        auto_result = {"resolved_count": 2, "results": [{"resolved": True}]}
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            resolve_mode,
            "auto_resolve_replies_from_sent",
            return_value=auto_result,
        ) as auto_resolve:
            result = resolve_mode.run_resolve_mode({}, data_dir=Path(temporary))

        auto_resolve.assert_called_once_with(data_dir=Path(temporary))
        self.assertEqual(
            {"ok": True, "mode": "resolve", "auto_audit": True, **auto_result},
            result,
        )


class InspectModeTests(unittest.TestCase):
    def test_inspect_preserves_result_shape_and_atomic_outputs(self) -> None:
        emails = [{"envelope_id": "3", "message_id": "known@example.test"}]
        manifest = {"items": [{"envelope_id": "3"}]}
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            output_path = Path(temporary) / "nested" / "inspect.json"
            manifest_path = Path(temporary) / "nested" / "manifest.json"
            result = inspect_mode.run_inspect_mode(
                {
                    "envelope_ids": ["3"],
                    "output_file": str(output_path),
                    "propose_manifest": True,
                    "manifest_file": str(manifest_path),
                },
                data_dir=data_dir,
                dependencies={
                    "atomic_write_json": runner.atomic_write_json,
                    "draft_manifest": Mock(return_value=manifest),
                    "get_oldest_envelopes": Mock(),
                    "get_single_email_details": Mock(return_value=emails[0]),
                    "get_unprocessed_emails": Mock(),
                    "load_final_index": Mock(return_value={"items": {}}),
                    "resolve_data_dir": Mock(return_value=data_dir),
                    "resolve_final_index_path": Mock(return_value=data_dir / "index.json"),
                    "run_himalaya": Mock(),
                    "sleep": Mock(),
                },
            )

            self.assertEqual(True, result["ok"])
            self.assertEqual("inspect", result["mode"])
            self.assertEqual(1, result["total_inspected"])
            self.assertEqual(manifest, result["manifest_proposal"])
            self.assertEqual(str(manifest_path.resolve()), result["manifest_file_created"])
            self.assertEqual(result, json.loads(output_path.read_text(encoding="utf-8")))
            self.assertEqual(manifest, json.loads(manifest_path.read_text(encoding="utf-8")))

    def test_explicit_envelope_ids_preserve_requested_order(self) -> None:
        def details(envelope_id, *_args, **_kwargs):
            return {"envelope_id": str(envelope_id), "message_id": f"{envelope_id}@example.test"}

        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            result = inspect_mode.run_inspect_mode(
                {"envelope_ids": ["3", "1", "2"]},
                account="primary",
                data_dir=data_dir,
                dependencies={
                    "get_oldest_envelopes": Mock(),
                    "get_single_email_details": Mock(side_effect=details),
                    "get_unprocessed_emails": Mock(),
                    "load_final_index": Mock(return_value={"items": {}}),
                    "resolve_final_index_path": Mock(return_value=data_dir / "index.json"),
                },
            )

        self.assertEqual(["3", "1", "2"], [email["envelope_id"] for email in result["emails"]])


class DraftModeTests(unittest.TestCase):
    def test_draft_reuses_only_unknown_cached_inspections_and_writes_manifest(self) -> None:
        manifest = {"items": [{"envelope_id": "unseen-1"}]}
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            cached = data_dir / "batch-inspected.json"
            cached.write_text(
                json.dumps({"emails": [{"envelope_id": "known", "is_known": True}, {"envelope_id": "unseen-1"}]}),
                encoding="utf-8",
            )
            output_path = data_dir / "manifest.json"
            fetch = Mock()
            classify = Mock(return_value=manifest)
            result = draft_mode.run_draft_mode(
                {"count": 1, "output_file": str(output_path)},
                data_dir=data_dir,
                dependencies={
                    "atomic_write_json": runner.atomic_write_json,
                    "draft_manifest": classify,
                    "get_unprocessed_emails": fetch,
                    "load_sent_index": Mock(return_value={"sent": []}),
                },
            )

            fetch.assert_not_called()
            classify.assert_called_once_with(
                [{"envelope_id": "unseen-1"}],
                workspace_root=data_dir.parent.parent,
                sent_lookup={"sent": []},
                full_reader=runner.get_single_email_details,
                account=None,
            )
            self.assertEqual(manifest, json.loads(output_path.read_text(encoding="utf-8")))
            self.assertEqual({
                "ok": True, "mode": "draft", "folder": "INBOX", "order": "oldest",
                "total_drafted": 1, "expected_count": 1, "allow_fewer": False,
                "candidate_count": 1, "source_folder": "INBOX", "account": None,
                "skip_known": True, "review": manifest["review"],
                "manifest_file": str(output_path.resolve()), "draft": manifest,
            }, result)

    def test_draft_fetches_with_filters_and_preserves_progress_lifecycle(self) -> None:
        events: list[tuple[str, str]] = []

        class Tracker:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                events.append(("init", str(kwargs["total_items"])))

            def step(self, value):
                events.append(("step", value))

            def complete(self, value):
                events.append(("complete", value))

        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            fetch = Mock(return_value=([{"envelope_id": "from-fetch"}], 0))
            result = draft_mode.run_draft_mode(
                {"count": 2, "date": "2026-05-18"},
                account="primary",
                data_dir=data_dir,
                dependencies={
                    "BatchProgressTracker": Tracker,
                    "atomic_write_json": runner.atomic_write_json,
                    "draft_manifest": Mock(return_value={"items": []}),
                    "get_unprocessed_emails": fetch,
                    "load_sent_index": Mock(return_value={}),
                },
            )

        self.assertEqual("2026-05-18", fetch.call_args.kwargs["date"])
        self.assertEqual(True, fetch.call_args.kwargs["skip_known"])
        self.assertEqual([("init", "2"), ("step", "classifying_and_checking_sent")], events[:2])
        self.assertTrue(events[-1][1].startswith("Drafted 0 items"))
        self.assertEqual("draft", result["mode"])


class SyncSentModeTests(unittest.TestCase):
    def test_sync_sent_forwards_dependencies_and_preserves_result_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            sync_items = Mock(return_value=(12, 4))
            result = sync_sent_mode.run_sync_sent_mode(
                {"count": 12, "folder": "Gesendet"},
                account="primary",
                data_dir=data_dir,
                dependencies={"sync_sent_items": sync_items},
            )

        sync_items.assert_called_once_with(
            count=12,
            folder="Gesendet",
            account="primary",
            data_dir=data_dir,
            workspace_root=data_dir.parent.parent,
        )
        self.assertEqual(
            {
                "ok": True,
                "mode": "sync_sent",
                "folder": "Gesendet",
                "total_envelopes_examined": 12,
                "new_entries_indexed": 4,
                "sent_index_file": str(data_dir / "sent-index.jsonl"),
            },
            result,
        )


class ExecuteModeTests(unittest.TestCase):
    def test_execute_preserves_copy_verify_delete_and_persistence_order(self) -> None:
        events: list[str] = []

        class Tracker:
            def __init__(self, **_kwargs):
                events.append("tracker:init")

            def step(self, *_args, **_kwargs):
                events.append("tracker:step")

            def advance_item(self, *_args, **_kwargs):
                events.append("tracker:advance")

            def complete(self, *_args, **_kwargs):
                events.append("tracker:complete")

        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            calls = Mock(side_effect=lambda command, **_kwargs: events.append(f"mail:{command[1]}"))
            verify = Mock(return_value="copied-42")
            flush = Mock(side_effect=lambda *_args, **_kwargs: events.append("flush"))
            save = Mock(side_effect=lambda *_args, **_kwargs: events.append("save"))
            action = Mock(side_effect=lambda *_args, **_kwargs: events.append("action"))
            reply = Mock(side_effect=lambda *_args, **_kwargs: events.append("reply"))
            resolve = Mock(side_effect=lambda *_args, **_kwargs: events.append("resolve"))
            result = execute_mode.run_execute_mode(
                {"items": [{
                    "envelope_id": "7", "message_id": "<Case@Example.test>", "subject": "Pilot",
                    "from": "Pilot <pilot@example.test>", "date": "2026-09-07",
                    "action": {"type": "copy_as_move", "target_folder": "Projects/Pilot"},
                    "decision": {"needs_reply": True, "reply_candidate": "draft"}, "notes": "route",
                    "evidence": {"file": "memory/evidence.md", "entry": "- pilot"},
                }]},
                account="primary", data_dir=data_dir, index_path=data_dir / "index.json",
                dependencies={
                    "BatchProgressTracker": Tracker, "append_action_log_entry": action,
                    "append_replies_needed_entry": reply, "auto_resolve_replies_from_sent": resolve,
                    "flush_batch_evidence": flush, "load_final_index": Mock(return_value={"items": {}}),
                    "run_himalaya": calls, "save_final_index_atomic": save, "sleep": Mock(),
                    "verify_in_target_folder": verify, "utc_now_iso": Mock(return_value="now"),
                },
            )

        self.assertTrue(result["ok"])
        self.assertEqual("copied-42", result["results"][0]["new_envelope_id"])
        self.assertEqual(["mail:copy", "mail:delete"], [event for event in events if event.startswith("mail:")])
        self.assertLess(events.index("flush"), events.index("save"))
        self.assertLess(events.index("save"), events.index("resolve"))
        self.assertEqual(1, action.call_count)
        self.assertEqual(1, reply.call_count)
        verify.assert_called_once()

    def test_execute_reuses_matching_index_entry_without_mailbox_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            index_data = {
                "items": {
                    "case@example.test": {
                        "final_folder": "Projects/Pilot",
                        "envelope_id": "existing-22",
                    }
                }
            }
            run_mail = Mock()
            verify = Mock()
            append_action = Mock()
            save_index = Mock()
            result = execute_mode.run_execute_mode(
                {
                    "items": [
                        {
                            "envelope_id": "source-7",
                            "message_id": "<Case@Example.test>",
                            "subject": "Pilot",
                            "action": {
                                "type": "copy_as_move",
                                "target_folder": "Projects/Pilot",
                            },
                            "decision": {},
                        }
                    ]
                },
                data_dir=data_dir,
                dependencies={
                    "BatchProgressTracker": Mock(),
                    "append_action_log_entry": append_action,
                    "auto_resolve_replies_from_sent": Mock(),
                    "load_final_index": Mock(return_value=index_data),
                    "run_himalaya": run_mail,
                    "save_final_index_atomic": save_index,
                    "sleep": Mock(),
                    "utc_now_iso": Mock(return_value="now"),
                    "verify_in_target_folder": verify,
                },
            )

        self.assertTrue(result["ok"])
        self.assertEqual("existing-22", result["results"][0]["new_envelope_id"])
        self.assertEqual("ok", result["results"][0]["routing"])
        self.assertEqual("existing-22", index_data["items"]["case@example.test"]["envelope_id"])
        run_mail.assert_not_called()
        verify.assert_not_called()
        append_action.assert_called_once()
        save_index.assert_called_once()

    def test_execute_reports_partial_failure_but_keeps_successful_item_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            save = Mock()
            result = execute_mode.run_execute_mode(
                {"items": [
                    {"envelope_id": "good", "message_id": "good@example.test", "action": {"type": "keep_in_folder"}, "decision": {}},
                    {"envelope_id": "bad", "message_id": "bad@example.test", "action": {"type": "copy_as_move", "target_folder": "Projects/Pilot"}, "decision": {}},
                ]},
                data_dir=data_dir,
                dependencies={
                    "BatchProgressTracker": Mock(), "auto_resolve_replies_from_sent": Mock(),
                    "load_final_index": Mock(return_value={"items": {}}), "run_himalaya": Mock(side_effect=RuntimeError("copy failed")),
                    "save_final_index_atomic": save, "sleep": Mock(), "verify_in_target_folder": Mock(),
                },
            )

        self.assertFalse(result["ok"])
        self.assertEqual([True, False], [row["success"] for row in result["results"]])
        self.assertEqual("fail", result["results"][1]["routing"])
        save.assert_called_once()

    def test_execute_collects_telemetry_from_successful_project_and_topic_items(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            result = execute_mode.run_execute_mode(
                {"items": [
                    {"envelope_id": "project-1", "message_id": "project-1@example.test", "action": {"type": "keep_in_folder"}, "decision": {"kind": "project", "id": " meshe "}},
                    {"envelope_id": "topic-1", "message_id": "topic-1@example.test", "action": {"type": "keep_in_folder"}, "decision": {"kind": "topic", "id": "dienstreisen"}},
                    {"envelope_id": "project-duplicate", "message_id": "project-duplicate@example.test", "action": {"type": "keep_in_folder"}, "decision": {"kind": "project", "id": "meshe"}},
                    {"envelope_id": "invalid-id", "message_id": "invalid-id@example.test", "action": {"type": "keep_in_folder"}, "decision": {"kind": "topic", "id": "   "}},
                    {"envelope_id": "invalid-kind", "message_id": "invalid-kind@example.test", "action": {"type": "keep_in_folder"}, "decision": {"kind": "archive", "id": "archive"}},
                    {"envelope_id": "failed", "message_id": "failed@example.test", "action": {"type": "copy_as_move", "target_folder": "Projects/Failed"}, "decision": {"kind": "project", "id": "must-not-count"}},
                ]},
                data_dir=data_dir,
                dependencies={
                    "BatchProgressTracker": Mock(), "append_action_log_entry": Mock(),
                    "auto_resolve_replies_from_sent": Mock(),
                    "load_final_index": Mock(return_value={"items": {}}),
                    "run_himalaya": Mock(side_effect=RuntimeError("copy failed")),
                    "save_final_index_atomic": Mock(), "sleep": Mock(),
                    "verify_in_target_folder": Mock(),
                },
            )

        self.assertFalse(result["ok"])
        self.assertEqual(
            {
                "affected_projects": ["meshe"],
                "affected_topics": ["dienstreisen"],
                "synthesis_required": True,
            },
            result["telemetry"],
        )


class PipelineModeTests(unittest.TestCase):
    def test_pipeline_orchestrates_sync_classification_execute_and_verify(self) -> None:
        email = {"envelope_id": "1", "message_id": "case@example.test"}
        executable = {"envelope_id": "1", "decision": {"confidence": "high"}, "action": {"target_folder": "Projects/Pilot"}}
        review = {"envelope_id": "2", "decision": {"confidence": "low"}, "action": {"target_folder": "Projects/Pilot"}}
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            fetch = Mock(return_value=([email], 0))
            sync = Mock(return_value=(1, 0))
            execute = Mock(return_value={"ok": True, "results": [{"success": True}]})
            verify = Mock(return_value={"ok": False, "results": [{"consistent": False}]})
            result = pipeline_mode.run_pipeline_mode(
                {"count": 1, "query": "subject pilot", "check_folders": True}, account="primary", data_dir=data_dir,
                dependencies={
                    "get_unprocessed_emails": fetch, "sync_sent_items": sync, "load_sent_index": Mock(return_value={"sent": []}),
                    "draft_manifest": Mock(return_value={"items": [executable, review]}),
                    "run_execute_mode": execute, "run_verify_mode": verify,
                },
            )

        self.assertFalse(result["ok"])
        self.assertEqual(1, result["executed_count"])
        self.assertEqual(1, result["review_needed_count"])
        self.assertEqual("subject pilot", fetch.call_args.kwargs["query"])
        execute.assert_called_once()
        self.assertTrue(verify.call_args.args[0]["check_folders"])

    def test_pipeline_propagates_execute_telemetry_without_review_items(self) -> None:
        executable = {"envelope_id": "1", "decision": {"confidence": "high"}, "action": {"target_folder": "Projects/Pilot"}}
        review = {"envelope_id": "2", "decision": {"confidence": "low", "kind": "topic", "id": "review-only"}, "action": {"target_folder": "Projects/Pilot"}}
        telemetry = {"affected_projects": ["meshe"], "affected_topics": ["dienstreisen"], "synthesis_required": True}
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            result = pipeline_mode.run_pipeline_mode(
                {"verify": False, "sync_sent": False},
                data_dir=data_dir,
                dependencies={
                    "get_unprocessed_emails": Mock(return_value=([{"envelope_id": "1"}], 0)),
                    "load_sent_index": Mock(return_value={}),
                    "draft_manifest": Mock(return_value={"items": [executable, review]}),
                    "run_execute_mode": Mock(return_value={"ok": True, "results": [], "telemetry": telemetry}),
                },
            )

        self.assertEqual(telemetry, result["telemetry"])
        self.assertEqual(1, result["review_needed_count"])

    def test_pipeline_uses_empty_telemetry_without_execute_or_from_legacy_execute(self) -> None:
        empty = {"affected_projects": [], "affected_topics": [], "synthesis_required": False}
        review_item = {"envelope_id": "review", "decision": {"confidence": "low"}, "action": {"target_folder": "Projects/Pilot"}}
        executable = {"envelope_id": "execute", "decision": {"confidence": "high"}, "action": {"target_folder": "Projects/Pilot"}}
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            no_execute = pipeline_mode.run_pipeline_mode(
                {"verify": False, "sync_sent": False}, data_dir=data_dir,
                dependencies={
                    "get_unprocessed_emails": Mock(return_value=([{"envelope_id": "review"}], 0)),
                    "load_sent_index": Mock(return_value={}),
                    "draft_manifest": Mock(return_value={"items": [review_item]}),
                    "run_execute_mode": Mock(),
                },
            )
            legacy_execute = pipeline_mode.run_pipeline_mode(
                {"verify": False, "sync_sent": False}, data_dir=data_dir,
                dependencies={
                    "get_unprocessed_emails": Mock(return_value=([{"envelope_id": "execute"}], 0)),
                    "load_sent_index": Mock(return_value={}),
                    "draft_manifest": Mock(return_value={"items": [executable]}),
                    "run_execute_mode": Mock(return_value={"ok": True, "results": [{"success": True}]}),
                },
            )

        self.assertEqual(empty, no_execute["telemetry"])
        self.assertEqual(empty, legacy_execute["telemetry"])


class VerifyModeTests(unittest.TestCase):
    def test_verify_uses_batch_file_checks_folder_and_writes_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace_root = Path(temporary)
            data_dir = workspace_root / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            message_id = "case@example.test"
            index_path = data_dir / "final-location-index.json"
            index_path.write_text(
                json.dumps({"items": {message_id: {"final_folder": "Projekte/Pilot", "envelope_id": "42"}}}),
                encoding="utf-8",
            )
            (data_dir / "action-log.jsonl").write_text(
                json.dumps({"message_id": message_id, "subject": "Pilot", "action": {"target_folder": "Projekte/Pilot"}}) + "\n",
                encoding="utf-8",
            )
            evidence_path = workspace_root / "memory" / "references" / "projects" / "pilot" / "evidence" / "2026-09.md"
            evidence_path.parent.mkdir(parents=True)
            evidence_path.write_text(f"- Message-ID: `{message_id}`\n", encoding="utf-8")
            batch_file = data_dir / "batch-result.json"
            batch_file.write_text(
                json.dumps({"results": [{"message_id": message_id, "subject": "Pilot", "target_folder": "Projekte/Pilot", "evidence": {"file": "memory/references/projects/pilot/evidence/2026-09.md"}}]}),
                encoding="utf-8",
            )
            output_path = workspace_root / "nested" / "verify.json"
            verify_folder = Mock(return_value="99")

            result = verify_mode.run_verify_mode(
                {"batch_file": str(batch_file), "check_folders": True, "output_file": str(output_path)},
                account="primary",
                data_dir=data_dir,
                index_path=index_path,
                dependencies={
                    "atomic_write_json": runner.atomic_write_json,
                    "verify_in_target_folder": verify_folder,
                },
            )

            self.assertEqual(result, json.loads(output_path.read_text(encoding="utf-8")))

        verify_folder.assert_called_once_with(
            "Projekte/Pilot", message_id, subject="Pilot", account="primary"
        )
        self.assertEqual(True, result["ok"])
        self.assertEqual(1, result["total_checked"])
        self.assertEqual(
            {
                "message_id": message_id,
                "subject": "Pilot",
                "in_index": True,
                "indexed_folder": "Projekte/Pilot",
                "indexed_envelope_id": "42",
                "in_action_log": True,
                "logged_folder": "Projekte/Pilot",
                "in_evidence": True,
                "folder_verified": True,
                "current_envelope_id": "99",
                "consistent": True,
            },
            result["results"][0],
        )

    def test_verify_marks_expected_folder_mismatch_inconsistent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            message_id = "case@example.test"
            result = verify_mode.run_verify_mode(
                {"items": [{"message_id": message_id, "target_folder": "Projekte/Expected"}]},
                data_dir=data_dir,
                dependencies={
                    "load_final_index": Mock(return_value={"items": {message_id: {"final_folder": "Projekte/Other"}}}),
                },
            )

        self.assertFalse(result["ok"])
        self.assertFalse(result["results"][0]["consistent"])


class DispatcherCompatibilityTests(unittest.TestCase):
    def test_runner_exports_and_dispatches_extracted_handlers(self) -> None:
        self.assertIs(runner.run_search_mode, search_mode.run_search_mode)
        self.assertIs(runner.run_resolve_mode, resolve_mode.run_resolve_mode)

        with tempfile.TemporaryDirectory() as temporary, patch.object(
            runner,
            "run_search_mode",
            return_value={"ok": True, "mode": "search"},
        ) as search:
            result, operation = runner._dispatch(
                {"mode": "find"},
                account="primary",
                data_dir=Path(temporary),
                index_path=Path(temporary) / "index.json",
            )

        self.assertEqual("search", operation)
        self.assertTrue(result["ok"])
        search.assert_called_once()

    def test_runner_facades_keep_patched_legacy_dependencies_and_dispatch_handlers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            runner, "get_unprocessed_emails", return_value=([], 0)
        ) as fetch, patch.object(runner, "draft_manifest", return_value={"items": []}):
            data_dir = Path(temporary) / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            runner.run_inspect_mode({"query": "subject pilot"}, data_dir=data_dir)
            runner.run_draft_mode({"date": "2026-05-18"}, data_dir=data_dir)

        self.assertEqual("2026-05-18", fetch.call_args.kwargs["date"])

        with tempfile.TemporaryDirectory() as temporary, patch.object(
            runner, "run_inspect_mode", return_value={"ok": True, "mode": "inspect"}
        ) as inspect:
            result, operation = runner._dispatch(
                {"mode": "fetch"}, account="primary", data_dir=Path(temporary), index_path=Path(temporary) / "index.json"
            )

        self.assertEqual("inspect", operation)
        self.assertTrue(result["ok"])
        inspect.assert_called_once()

    def test_runner_facades_keep_sync_and_verify_patch_surfaces(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            with patch.object(runner, "sync_sent_items", return_value=(3, 1)) as sync_items:
                sync_result = runner.run_sync_sent_mode({"count": 3}, account="primary", data_dir=data_dir)
            with patch.object(runner, "load_final_index", return_value={"items": {}}) as load_index:
                verify_result = runner.run_verify_mode({"message_ids": ["missing@example.test"]}, data_dir=data_dir)

        self.assertEqual(1, sync_result["new_entries_indexed"])
        sync_items.assert_called_once()
        self.assertFalse(verify_result["ok"])
        load_index.assert_called_once()

    def test_runner_facades_keep_execute_and_pipeline_patch_surfaces(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            with patch.object(runner, "run_himalaya") as run_mail, patch.object(
                runner, "verify_in_target_folder", return_value="22"
            ) as verify:
                execute_result = runner.run_execute_mode(
                    {"items": [{"envelope_id": "2", "message_id": "case@example.test", "action": {"type": "copy_as_move", "target_folder": "Projects/Pilot"}, "decision": {}}]},
                    data_dir=data_dir,
                )
            with patch.object(runner, "get_unprocessed_emails", return_value=([{"envelope_id": "3"}], 0)) as fetch, patch.object(
                runner, "sync_sent_items", return_value=(1, 1)
            ), patch.object(runner, "load_sent_index", return_value={}), patch.object(
                runner, "draft_manifest", return_value={"items": []}
            ):
                pipeline_result = runner.run_pipeline_mode({"query": "pilot"}, data_dir=data_dir)

        self.assertTrue(execute_result["ok"])
        run_mail.assert_called()
        verify.assert_called_once()
        self.assertEqual("pilot", fetch.call_args.kwargs["query"])
        self.assertEqual(0, pipeline_result["executed_count"])


if __name__ == "__main__":
    unittest.main()
