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
from core.modes import inspect as inspect_mode  # noqa: E402
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
                [{"envelope_id": "unseen-1"}], workspace_root=data_dir.parent.parent, sent_lookup={"sent": []}
            )
            self.assertEqual(manifest, json.loads(output_path.read_text(encoding="utf-8")))
            self.assertEqual({"ok": True, "mode": "draft", "folder": "INBOX", "order": "oldest", "total_drafted": 1, "manifest_file": str(output_path.resolve()), "draft": manifest}, result)

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


if __name__ == "__main__":
    unittest.main()
