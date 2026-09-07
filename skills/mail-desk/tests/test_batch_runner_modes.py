"""Behavior and compatibility tests for extracted batch-runner modes."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

import mail_desk_batch_runner as runner  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
