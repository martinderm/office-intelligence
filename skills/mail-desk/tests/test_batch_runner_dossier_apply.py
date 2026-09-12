"""Focused contracts for the human-reviewed FR-04b dossier execution bridge."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock


MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

from core.modes.dossier_apply import (  # noqa: E402
    canonical_execute_request_sha256,
    run_dossier_apply_mode,
)


PROJECT = {
    "id": "meshe",
    "title": "MESHE",
    "status": "active",
    "mailbox_folder": "Projekte/MESHE",
}


def reviewed_config(execute_request: dict) -> dict:
    return {
        "mode": "dossier_apply",
        "project": "meshe",
        "delete_input_on_success": False,
        "execute_request": execute_request,
        "review": {
            "required": True,
            "state": "approved",
            "approval_receipt": {
                "reviewed_at": "2026-09-09T12:00:00Z",
                "reviewed_by": "human-reviewer",
                "execute_request_sha256": canonical_execute_request_sha256(execute_request),
            },
        },
    }


def execute_request(*items: dict) -> dict:
    return {"mode": "execute", "items": list(items)}


def item(message_id: str = "case@example.test", **overrides: object) -> dict:
    value = {
        "envelope_id": "42",
        "source_folder": "INBOX",
        "message_id": message_id,
        "subject": "MESHE update",
        "action": {"type": "copy_as_move", "target_folder": "Projekte/MESHE"},
        "decision": {"kind": "project", "id": "meshe", "confidence": "high", "needs_reply": False},
    }
    value.update(overrides)
    return value


class DossierApplyModeTests(unittest.TestCase):
    def run_mode(
        self,
        config: dict,
        *,
        execute: Mock | None = None,
        verify: Mock | None = None,
        projects: list[dict] | None = None,
        account: str | None = None,
    ):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        data_dir = Path(temporary.name) / "data" / "mail-desk"
        data_dir.mkdir(parents=True)
        execute = execute or Mock(return_value={
            "ok": True,
            "results": [{"success": True, "message_id": row["message_id"]} for row in config["execute_request"]["items"]],
            "telemetry": {"affected_projects": ["meshe"]},
            "synthesis_candidate": {
                "schema_version": 1,
                "status": "pending",
                "items": [{
                    "message_id": row["message_id"],
                    "subject": row.get("subject", ""),
                    "kind": "project",
                    "id": "meshe",
                    "synthesis_targets": [],
                    "target_selection_required": True,
                } for row in config["execute_request"]["items"]],
            },
        })
        verify = verify or Mock(return_value={"ok": True, "results": [{"message_id": row["message_id"], "consistent": True} for row in config["execute_request"]["items"]]})
        result = run_dossier_apply_mode(
            config,
            account=account,
            data_dir=data_dir,
            index_path=data_dir / "final-location-index.json",
            dependencies={
                "load_catalogs": lambda _root: ([PROJECT] if projects is None else projects, []),
                "run_execute_mode": execute,
                "run_verify_mode": verify,
            },
        )
        return result, execute, verify, data_dir

    def test_delegates_exact_reviewed_request_to_existing_execute_then_verify(self) -> None:
        request = execute_request(item("First@Example.test"), item("second@example.test", envelope_id="43"))
        request["account"] = "primary"
        result, execute, verify, data_dir = self.run_mode(reviewed_config(request), account="primary")

        self.assertTrue(result["ok"])
        execute.assert_called_once_with(request, account="primary", data_dir=data_dir, index_path=data_dir / "final-location-index.json")
        verify.assert_called_once_with(
            {"mode": "verify", "message_ids": ["first@example.test", "second@example.test"], "check_folders": False},
            account="primary",
            data_dir=data_dir,
            index_path=data_dir / "final-location-index.json",
        )
        self.assertEqual(["meshe"], result["telemetry"]["affected_projects"])
        self.assertEqual("pending", result["synthesis_handoff"]["status"])
        self.assertEqual("completed", result["review"]["state"])

    def test_rejects_unreviewed_or_mismatched_outer_account_before_execute(self) -> None:
        request = execute_request(item())
        execute = Mock()
        for config, account in (
            (reviewed_config(request), "primary"),
            (reviewed_config({**request, "account": "reviewed"}), "other"),
        ):
            with self.subTest(account=account):
                with self.assertRaisesRegex(ValueError, "outer account"):
                    self.run_mode(config, execute=execute, account=account)
        execute.assert_not_called()

    def test_uses_the_reviewed_account_when_no_outer_override_is_given(self) -> None:
        request = execute_request(item())
        request["account"] = "reviewed-primary"
        result, execute, verify, data_dir = self.run_mode(reviewed_config(request))

        self.assertTrue(result["ok"])
        self.assertEqual("reviewed-primary", execute.call_args.kwargs["account"])
        self.assertEqual("reviewed-primary", verify.call_args.kwargs["account"])
        self.assertEqual(data_dir, execute.call_args.kwargs["data_dir"])

    def test_rejects_tampered_execute_request_before_any_side_effect(self) -> None:
        request = execute_request(item())
        config = reviewed_config(request)
        request["items"][0]["subject"] = "tampered after review"

        with self.assertRaisesRegex(ValueError, "does not bind"):
            self.run_mode(config)

    def test_full_preflight_rejects_late_out_of_scope_item_without_executing_first(self) -> None:
        request = execute_request(
            item("good@example.test"),
            item("bad@example.test", decision={"kind": "topic", "id": "meshe"}),
        )
        execute = Mock()
        verify = Mock()

        with self.assertRaisesRegex(ValueError, "exactly project"):
            self.run_mode(reviewed_config(request), execute=execute, verify=verify)

        execute.assert_not_called()
        verify.assert_not_called()

    def test_requires_exactly_one_routing_project_before_execute(self) -> None:
        request = execute_request(item())
        execute = Mock()

        with self.assertRaisesRegex(ValueError, "exactly one"):
            self.run_mode(
                reviewed_config(request),
                execute=execute,
                projects=[PROJECT, dict(PROJECT)],
            )

        execute.assert_not_called()

    def test_rejects_source_and_target_outside_exact_catalog_boundary(self) -> None:
        for invalid_item, expected in (
            (item(source_folder="Archive"), "exactly INBOX"),
            (item(action={"type": "copy_as_move", "target_folder": "Projekte/Other"}), "catalog mailbox_folder"),
            (item(action={"type": "archive", "target_folder": "Projekte/MESHE"}), "catalog mailbox_folder"),
        ):
            with self.subTest(invalid_item=invalid_item):
                execute = Mock()
                with self.assertRaisesRegex(ValueError, expected):
                    self.run_mode(reviewed_config(execute_request(invalid_item)), execute=execute)
                execute.assert_not_called()

    def test_execute_failure_is_fail_closed_without_verify_and_preserves_handoff(self) -> None:
        request = execute_request(item())
        execute = Mock(return_value={
            "ok": False,
            "results": [{"success": False}],
            "telemetry": {"affected_projects": [], "affected_topics": [], "synthesis_required": False},
            "synthesis_handoff": {"schema_version": 1, "status": "not_required", "items": []},
        })
        verify = Mock()
        result, _, verify, _ = self.run_mode(reviewed_config(request), execute=execute, verify=verify)

        self.assertFalse(result["ok"])
        self.assertEqual("execute_review_required", result["review"]["state"])
        self.assertEqual("not_required", result["synthesis_handoff"]["status"])
        self.assertIsNotNone(result["execute_summary"])
        verify.assert_not_called()

    def test_verify_failure_withholds_execute_candidate_for_recovery(self) -> None:
        request = execute_request(item())
        execute = Mock(return_value={
            "ok": True,
            "results": [{"success": True, "message_id": "case@example.test"}],
            "telemetry": {"affected_projects": ["meshe"], "affected_topics": [], "synthesis_required": True},
            "synthesis_handoff": {"schema_version": 1, "status": "pending", "items": []},
        })
        verify = Mock(return_value={"ok": False, "results": [{"consistent": False}]})
        result, _, _, _ = self.run_mode(reviewed_config(request), execute=execute, verify=verify)

        self.assertFalse(result["ok"])
        self.assertEqual("verify_review_required", result["review"]["state"])
        self.assertEqual("not_required", result["synthesis_handoff"]["status"])
        self.assertEqual(False, result["verify_summary"]["ok"])

    def test_malformed_successful_execute_result_is_not_verified(self) -> None:
        request = execute_request(item())
        execute = Mock(return_value={"ok": True, "results": []})
        verify = Mock()
        result, _, verify, _ = self.run_mode(reviewed_config(request), execute=execute, verify=verify)

        self.assertFalse(result["ok"])
        self.assertEqual("execute_review_required", result["review"]["state"])
        self.assertEqual("ValueError", result["error"]["exception_type"])
        verify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
