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


if __name__ == "__main__":
    unittest.main()
