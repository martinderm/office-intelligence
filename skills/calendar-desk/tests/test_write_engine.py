"""Unit tests for Calendar-Desk two-phase write engine, workspace-lock guard, and recovery journal.

Tests skills/calendar-desk/scripts/core/write_engine.py contracts:
- CalendarWriteError with deterministic machine-readable error codes
- Workspace-lock guard integration (_trusted_lock_root, _load_guard, require_owned_workspace_lock)
- Canonical JSON deterministic SHA-256 calculation
- Phase 1: plan_calendar_write (create-event, update-event, cancel-event, delete-event)
- Write policy enforcement (CALENDAR_WRITE_FORBIDDEN on read_only calendars)
- Interrupted journal blocking (RECOVERY_REQUIRED)
- Phase 2: apply_calendar_write (lock verification, approval receipt validation, journal state machine)
- CalDAV ETag precondition enforcement and conflict mapping (CALDAV_ETAG_CONFLICT -> aborted_precondition)
- Local evidence publication (calendars.json register and raw ICS)
- inspect_workspace and recover_workspace reconciliation under lock
- Phase-2 seam respond_invitation (NOT_IMPLEMENTED_PHASE_2)
- Hermetic loopback mock-CalDAV server write integration
- CLI write subcommands in calendar_desk_client.py
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import http.server
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path
from typing import Any, Optional

# Add calendar-desk scripts to sys.path
CALENDAR_DESK_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = CALENDAR_DESK_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from core.backend_config import CalDAVConfigError  # noqa: E402
from core.caldav_client import CalDAVClient, CalDAVProtocolError  # noqa: E402
from core.write_engine import (  # noqa: E402
    ENV_LOCK_ROOT,
    CalendarWriteError,
    _check_no_in_progress_journal,
    _load_guard,
    _trusted_lock_root,
    apply_calendar_write,
    canonical_json_sha256,
    compute_plan_sha256,
    inspect_workspace,
    plan_calendar_write,
    recover_workspace,
    require_owned_workspace_lock,
    respond_invitation,
)
import calendar_desk_client  # noqa: E402


def make_mock_guard_dir(
    parent_dir: Path,
    lock_state: str = "Active",
    owner_lease: str = "test-lease-123",
    owner_conv: str = "test-conv-456",
) -> Path:
    """Creates a mock workspace-lock skill directory with workspace_lock_guard.py."""
    guard_root = parent_dir / "workspace-lock"
    scripts_dir = guard_root / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    guard_code = f'''"""Mock workspace_lock_guard for hermetic testing."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Any

class WorkspaceLockError(RuntimeError):
    pass

@dataclass(frozen=True)
class LockReceipt:
    workspace: Path
    state: str
    lease_id: str | None
    conversation_id: str | None
    legacy: bool = False

def require_workspace_lock(
    workspace_root: str | Path,
    *,
    lease_id: str | None = None,
    conversation_id: str | None = None,
    allow_legacy: bool = False,
    runner: Any = None,
) -> LockReceipt:
    ws = Path(workspace_root).resolve()
    state = "{lock_state}"
    expected_lease = "{owner_lease}"
    expected_conv = "{owner_conv}"
    if state == "Free":
        if allow_legacy:
            return LockReceipt(ws, "Legacy", lease_id, conversation_id, legacy=True)
        raise WorkspaceLockError("workspace lock is not active ('Free')")
    if state != "Active":
        raise WorkspaceLockError(f"workspace lock is not active ({{state!r}})")
    if (lease_id != expected_lease) and (conversation_id != expected_conv):
        raise WorkspaceLockError("workspace lock is not owned by this invocation")
    return LockReceipt(ws, "Active", lease_id, conversation_id)
'''
    (scripts_dir / "workspace_lock_guard.py").write_text(guard_code, encoding="utf-8")
    return guard_root


def make_backend_config_file(ws: Path, write_policy: str = "dry_run_and_approval") -> Path:
    """Creates a valid .agents/calendar-desk-backend.json configuration."""
    cfg = {
        "schema": "calendar-desk-backend/1",
        "default_account": "primary-account",
        "default_timezone": "Europe/Vienna",
        "accounts": {
            "primary-account": {
                "principal_url": "https://cal.example.com/dav/principals/",
                "allow_loopback_http": True,
                "timeout_seconds": 10.0,
                "calendars": {
                    "primary": {
                        "collection_path": "/dav/calendars/user/primary/",
                        "display_name": "Primary Calendar",
                        "role": "primary",
                        "write_policy": write_policy,
                        "default_timezone": "Europe/Vienna",
                    }
                },
            }
        },
    }
    agents_dir = ws / ".agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    cfg_file = agents_dir / "calendar-desk-backend.json"
    cfg_file.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return cfg_file


class MockCalDAVClient:
    """In-memory mock of CalDAV client for fast unit tests."""

    def __init__(
        self,
        *,
        conflict_on_put: bool = False,
        conflict_on_delete: bool = False,
        network_error_on_put: bool = False,
        omit_etag_on_put: bool = False,
    ) -> None:
        self.events: dict[str, dict[str, str]] = {}
        self.put_calls: list[dict[str, Any]] = []
        self.delete_calls: list[dict[str, Any]] = []
        self.conflict_on_put = conflict_on_put
        self.conflict_on_delete = conflict_on_delete
        self.network_error_on_put = network_error_on_put
        self.omit_etag_on_put = omit_etag_on_put

    def put_event_ics(
        self,
        resource_url: str,
        raw_ics: str,
        *,
        if_none_match_star: bool = False,
        if_match_etag: str | None = None,
    ) -> dict[str, Any]:
        if self.network_error_on_put:
            raise CalDAVProtocolError("CALDAV_NETWORK_ERROR", "Network drop during PUT")
        if self.conflict_on_put:
            raise CalDAVProtocolError("CALDAV_ETAG_CONFLICT", "Precondition Failed (412)", status_code=412)
        if if_none_match_star and resource_url in self.events:
            raise CalDAVProtocolError("CALDAV_ETAG_CONFLICT", "Resource already exists", status_code=412)
        if if_match_etag and (
            resource_url not in self.events or self.events[resource_url]["etag"] != if_match_etag
        ):
            raise CalDAVProtocolError("CALDAV_ETAG_CONFLICT", "ETag mismatch", status_code=412)

        new_etag = f'"etag-{hashlib.sha256(raw_ics.encode("utf-8")).hexdigest()[:8]}"'
        self.events[resource_url] = {"etag": new_etag, "raw_ics": raw_ics}
        call_info = {
            "resource_url": resource_url,
            "raw_ics": raw_ics,
            "etag": new_etag,
            "if_none_match_star": if_none_match_star,
            "if_match_etag": if_match_etag,
        }
        self.put_calls.append(call_info)
        if self.omit_etag_on_put:
            return {"status": 201, "etag": None, "resource_url": resource_url}
        return {"status": 201, "etag": new_etag, "resource_url": resource_url}

    def delete_event_resource(
        self,
        resource_url: str,
        *,
        if_match_etag: str,
    ) -> dict[str, Any]:
        if self.conflict_on_delete:
            raise CalDAVProtocolError("CALDAV_ETAG_CONFLICT", "Precondition Failed (412)", status_code=412)
        if resource_url not in self.events or self.events[resource_url]["etag"] != if_match_etag:
            raise CalDAVProtocolError("CALDAV_ETAG_CONFLICT", "ETag mismatch", status_code=412)

        del self.events[resource_url]
        self.delete_calls.append({"resource_url": resource_url, "if_match_etag": if_match_etag})
        return {"status": 204, "resource_url": resource_url}

    def get_event(self, resource_url: str) -> dict[str, Any]:
        if resource_url not in self.events:
            raise CalDAVProtocolError("CALDAV_NOT_FOUND", "Event not found", status_code=404)
        item = self.events[resource_url]
        return {
            "etag": item["etag"],
            "href": resource_url,
            "raw_ics": item["raw_ics"],
            "records": [],
            "fenced_records": [],
        }


SAMPLE_VEVENT_ICS = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "PRODID:-//office-intelligence//calendar-desk//EN\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:event-123@test\r\n"
    "DTSTAMP:20261101T080000Z\r\n"
    "DTSTART:20261102T090000Z\r\n"
    "DTEND:20261102T100000Z\r\n"
    "SUMMARY:Initial Event\r\n"
    "SEQUENCE:0\r\n"
    "STATUS:CONFIRMED\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)


class TestWriteEngineSeams(unittest.TestCase):
    """Test constant declarations and error class behavior."""

    def test_env_lock_root_constant(self) -> None:
        self.assertEqual(ENV_LOCK_ROOT, "CALENDAR_DESK_WORKSPACE_LOCK_ROOT")

    def test_calendar_write_error_properties(self) -> None:
        err = CalendarWriteError("CALENDAR_WRITE_FORBIDDEN", "Write policy is read_only")
        self.assertEqual(err.code, "CALENDAR_WRITE_FORBIDDEN")
        self.assertIn("read_only", str(err))
        self.assertIsInstance(err, RuntimeError)

    def test_calendar_write_error_codes(self) -> None:
        codes = [
            "CALENDAR_WRITE_FORBIDDEN",
            "WORKSPACE_LOCK_UNAVAILABLE",
            "WORKSPACE_LOCK_REQUIRED",
            "PLAN_NOT_FOUND",
            "PLAN_HASH_MISMATCH",
            "APPROVAL_RECEIPT_INVALID",
            "RECOVERY_REQUIRED",
            "CALDAV_ETAG_CONFLICT",
            "NOT_IMPLEMENTED_PHASE_2",
            "WRITE_VALIDATION_ERROR",
        ]
        for c in codes:
            e = CalendarWriteError(c, f"Error with code {c}")
            self.assertEqual(e.code, c)


class TestWorkspaceLockGuardIntegration(unittest.TestCase):
    """Test workspace-lock discovery, dynamic loading, and enforcement."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_trusted_lock_root_explicit(self) -> None:
        guard_dir = make_mock_guard_dir(self.base_path)
        resolved = _trusted_lock_root(guard_dir)
        self.assertEqual(resolved.resolve(), guard_dir.resolve())

    def test_trusted_lock_root_env_workspace_lock_root(self) -> None:
        guard_dir = make_mock_guard_dir(self.base_path)
        old_env1 = os.environ.get("WORKSPACE_LOCK_ROOT")
        old_env2 = os.environ.get("CALENDAR_DESK_WORKSPACE_LOCK_ROOT")
        try:
            os.environ["WORKSPACE_LOCK_ROOT"] = str(guard_dir)
            if "CALENDAR_DESK_WORKSPACE_LOCK_ROOT" in os.environ:
                del os.environ["CALENDAR_DESK_WORKSPACE_LOCK_ROOT"]
            resolved = _trusted_lock_root()
            self.assertEqual(resolved.resolve(), guard_dir.resolve())
        finally:
            if old_env1 is not None:
                os.environ["WORKSPACE_LOCK_ROOT"] = old_env1
            else:
                os.environ.pop("WORKSPACE_LOCK_ROOT", None)
            if old_env2 is not None:
                os.environ["CALENDAR_DESK_WORKSPACE_LOCK_ROOT"] = old_env2

    def test_trusted_lock_root_env_calendar_desk_lock_root(self) -> None:
        guard_dir = make_mock_guard_dir(self.base_path)
        old_env1 = os.environ.get("WORKSPACE_LOCK_ROOT")
        old_env2 = os.environ.get("CALENDAR_DESK_WORKSPACE_LOCK_ROOT")
        try:
            os.environ.pop("WORKSPACE_LOCK_ROOT", None)
            os.environ["CALENDAR_DESK_WORKSPACE_LOCK_ROOT"] = str(guard_dir)
            resolved = _trusted_lock_root()
            self.assertEqual(resolved.resolve(), guard_dir.resolve())
        finally:
            if old_env1 is not None:
                os.environ["WORKSPACE_LOCK_ROOT"] = old_env1
            if old_env2 is not None:
                os.environ["CALENDAR_DESK_WORKSPACE_LOCK_ROOT"] = old_env2
            else:
                os.environ.pop("CALENDAR_DESK_WORKSPACE_LOCK_ROOT", None)

    def test_load_guard_missing_raises_lock_unavailable(self) -> None:
        empty_dir = self.base_path / "empty-dir"
        empty_dir.mkdir(parents=True, exist_ok=True)
        with self.assertRaises(CalendarWriteError) as ctx:
            _load_guard(empty_dir)
        self.assertEqual(ctx.exception.code, "WORKSPACE_LOCK_UNAVAILABLE")

    def test_load_guard_ignores_tampered_sys_modules(self) -> None:
        guard_dir = make_mock_guard_dir(self.base_path)
        old_module = sys.modules.get("workspace_lock_guard")
        try:
            sys.modules["workspace_lock_guard"] = "tampered-object"  # type: ignore[assignment]
            guard_mod = _load_guard(guard_dir)
            self.assertTrue(hasattr(guard_mod, "require_workspace_lock"))
            self.assertNotEqual(guard_mod, "tampered-object")
        finally:
            if old_module is not None:
                sys.modules["workspace_lock_guard"] = old_module
            else:
                sys.modules.pop("workspace_lock_guard", None)

    def test_require_owned_workspace_lock_active_owned(self) -> None:
        guard_dir = make_mock_guard_dir(self.base_path, lock_state="Active", owner_lease="lease-xyz")
        receipt = require_owned_workspace_lock(
            self.base_path,
            lease_id="lease-xyz",
            lock_root=guard_dir,
        )
        self.assertIsNotNone(receipt)

    def test_require_owned_workspace_lock_unowned_raises(self) -> None:
        guard_dir = make_mock_guard_dir(self.base_path, lock_state="Active", owner_lease="lease-xyz")
        with self.assertRaises(CalendarWriteError) as ctx:
            require_owned_workspace_lock(
                self.base_path,
                lease_id="other-lease",
                conversation_id="other-conv",
                lock_root=guard_dir,
            )
        self.assertEqual(ctx.exception.code, "WORKSPACE_LOCK_REQUIRED")

    def test_require_owned_workspace_lock_free_state_raises(self) -> None:
        guard_dir = make_mock_guard_dir(self.base_path, lock_state="Free")
        with self.assertRaises(CalendarWriteError) as ctx:
            require_owned_workspace_lock(
                self.base_path,
                lease_id="lease-xyz",
                lock_root=guard_dir,
            )
        self.assertEqual(ctx.exception.code, "WORKSPACE_LOCK_REQUIRED")


class TestCanonicalJsonAndHashing(unittest.TestCase):
    """Test deterministic canonical JSON and plan hashing."""

    def test_canonical_json_sha256_deterministic(self) -> None:
        dict1 = {"b": 2, "a": 1, "c": {"y": "ä", "x": 10}}
        dict2 = {"c": {"x": 10, "y": "ä"}, "a": 1, "b": 2}
        hash1 = canonical_json_sha256(dict1)
        hash2 = canonical_json_sha256(dict2)
        self.assertEqual(hash1, hash2)
        self.assertEqual(len(hash1), 64)
        self.assertTrue(all(c in "0123456789abcdef" for c in hash1))

    def test_compute_plan_sha256(self) -> None:
        plan = {
            "schema": "calendar-desk-plan/1",
            "account": "primary-account",
            "calendar_alias": "primary",
            "action": "create-event",
            "uid": "event-123@test",
            "resource_url": "https://cal.example.com/dav/calendars/user/primary/event-123@test.ics",
            "precondition": {"if_none_match_star": True, "if_match_etag": None},
            "canonical_ics_sha256": "abc123def456",
        }
        h1 = compute_plan_sha256(plan)
        # Adding existing plan_sha256 should not change the computed hash
        plan_with_hash = dict(plan)
        plan_with_hash["plan_sha256"] = h1
        h2 = compute_plan_sha256(plan_with_hash)
        self.assertEqual(h1, h2)


class TestPlanningEngine(unittest.TestCase):
    """Test lock-free dry-run planning for calendar writes."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.ws = Path(self.temp_dir.name)
        make_backend_config_file(self.ws, write_policy="dry_run_and_approval")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_plan_calendar_write_forbidden_policy(self) -> None:
        # Recreate backend config with read_only policy
        make_backend_config_file(self.ws, write_policy="read_only")
        with self.assertRaises(CalendarWriteError) as ctx:
            plan_calendar_write(
                self.ws,
                action="create-event",
                account="primary-account",
                calendar_alias="primary",
                collection_url="/dav/calendars/user/primary/",
                write_policy="read_only",
                uid="evt-test-1",
                event_spec={
                    "summary": "Team Sync",
                    "dtstart": "2026-11-02T10:00:00",
                    "dtend": "2026-11-02T11:00:00",
                    "timezone": "Europe/Vienna",
                },
            )
        self.assertEqual(ctx.exception.code, "CALENDAR_WRITE_FORBIDDEN")

    def test_plan_calendar_write_unresolved_recovery_journal_blocks(self) -> None:
        journal_dir = self.ws / "data" / "calendar-desk" / "journal"
        journal_dir.mkdir(parents=True, exist_ok=True)
        (journal_dir / "interrupted-plan.json").write_text(
            json.dumps({"status": "in_progress", "plan_id": "interrupted-plan"}),
            encoding="utf-8",
        )
        with self.assertRaises(CalendarWriteError) as ctx:
            plan_calendar_write(
                self.ws,
                action="create-event",
                account="primary-account",
                calendar_alias="primary",
                collection_url="/dav/calendars/user/primary/",
                write_policy="dry_run_and_approval",
                uid="evt-test-2",
                event_spec={"summary": "Sync", "dtstart": "2026-11-02T10:00:00"},
            )
        self.assertEqual(ctx.exception.code, "RECOVERY_REQUIRED")

    def test_plan_calendar_write_invalid_action(self) -> None:
        with self.assertRaises(CalendarWriteError) as ctx:
            plan_calendar_write(
                self.ws,
                action="invalid-action",
                account="primary-account",
                calendar_alias="primary",
                collection_url="/dav/calendars/user/primary/",
                write_policy="dry_run_and_approval",
            )
        self.assertEqual(ctx.exception.code, "WRITE_VALIDATION_ERROR")

    def test_plan_calendar_write_create_event_persisted(self) -> None:
        event_spec = {
            "summary": "Team Planning",
            "dtstart": "2026-11-02T09:00:00",
            "dtend": "2026-11-02T10:00:00",
            "timezone": "Europe/Vienna",
            "location": "Room A",
            "description": "Quarterly planning sync",
        }
        plan = plan_calendar_write(
            self.ws,
            action="create-event",
            account="primary-account",
            calendar_alias="primary",
            collection_url="/dav/calendars/user/primary/",
            write_policy="dry_run_and_approval",
            uid="evt-new-123",
            event_spec=event_spec,
            persist=True,
        )
        self.assertEqual(plan["schema"], "calendar-desk-plan/1")
        self.assertEqual(plan["action"], "create-event")
        self.assertEqual(plan["account"], "primary-account")
        self.assertEqual(plan["calendar_alias"], "primary")
        self.assertEqual(plan["uid"], "evt-new-123")
        self.assertEqual(
            plan["precondition"],
            {"if_none_match_star": True, "if_match_etag": None},
        )
        self.assertIn("BEGIN:VCALENDAR", plan["staged_ics"])
        self.assertIn("UID:evt-new-123", plan["staged_ics"])
        self.assertIn("SUMMARY:Team Planning", plan["staged_ics"])
        self.assertTrue(bool(plan["plan_id"]))
        self.assertTrue(bool(plan["plan_sha256"]))
        self.assertIn("approval_template", plan)

        # Check proposal file on disk
        proposal_file = self.ws / "data" / "calendar-desk" / "proposals" / f"{plan['plan_id']}.json"
        self.assertTrue(proposal_file.exists())
        loaded = json.loads(proposal_file.read_text(encoding="utf-8"))
        self.assertEqual(loaded["plan_id"], plan["plan_id"])
        self.assertEqual(loaded["plan_sha256"], plan["plan_sha256"])

    def test_plan_calendar_write_create_event_persist_false(self) -> None:
        event_spec = {
            "summary": "Dry Run Event",
            "dtstart": "2026-11-02T09:00:00",
            "dtend": "2026-11-02T10:00:00",
            "timezone": "Europe/Vienna",
        }
        plan = plan_calendar_write(
            self.ws,
            action="create-event",
            account="primary-account",
            calendar_alias="primary",
            collection_url="/dav/calendars/user/primary/",
            write_policy="dry_run_and_approval",
            uid="evt-dry-1",
            event_spec=event_spec,
            persist=False,
        )
        proposal_file = self.ws / "data" / "calendar-desk" / "proposals" / f"{plan['plan_id']}.json"
        self.assertFalse(proposal_file.exists())

    def test_plan_calendar_write_update_event(self) -> None:
        plan = plan_calendar_write(
            self.ws,
            action="update-event",
            account="primary-account",
            calendar_alias="primary",
            collection_url="/dav/calendars/user/primary/",
            write_policy="dry_run_and_approval",
            uid="event-123@test",
            expected_etag='"etag-v1"',
            existing_raw_ics=SAMPLE_VEVENT_ICS,
            updates={"summary": "Updated Meeting Summary"},
            persist=True,
        )
        self.assertEqual(plan["action"], "update-event")
        self.assertEqual(
            plan["precondition"],
            {"if_none_match_star": False, "if_match_etag": '"etag-v1"'},
        )
        self.assertIn("SUMMARY:Updated Meeting Summary", plan["staged_ics"])
        self.assertIn("SEQUENCE:1", plan["staged_ics"])

    def test_plan_calendar_write_update_missing_etag_raises(self) -> None:
        with self.assertRaises(CalendarWriteError) as ctx:
            plan_calendar_write(
                self.ws,
                action="update-event",
                account="primary-account",
                calendar_alias="primary",
                collection_url="/dav/calendars/user/primary/",
                write_policy="dry_run_and_approval",
                uid="event-123@test",
                existing_raw_ics=SAMPLE_VEVENT_ICS,
                expected_etag="",
            )
        self.assertEqual(ctx.exception.code, "WRITE_VALIDATION_ERROR")

    def test_plan_calendar_write_cancel_event(self) -> None:
        plan = plan_calendar_write(
            self.ws,
            action="cancel-event",
            account="primary-account",
            calendar_alias="primary",
            collection_url="/dav/calendars/user/primary/",
            write_policy="dry_run_and_approval",
            uid="event-123@test",
            expected_etag='"etag-v1"',
            existing_raw_ics=SAMPLE_VEVENT_ICS,
            persist=True,
        )
        self.assertEqual(plan["action"], "cancel-event")
        self.assertEqual(
            plan["precondition"],
            {"if_none_match_star": False, "if_match_etag": '"etag-v1"'},
        )
        self.assertIn("STATUS:CANCELLED", plan["staged_ics"])
        self.assertIn("SEQUENCE:1", plan["staged_ics"])

    def test_plan_calendar_write_delete_event(self) -> None:
        plan = plan_calendar_write(
            self.ws,
            action="delete-event",
            account="primary-account",
            calendar_alias="primary",
            collection_url="/dav/calendars/user/primary/",
            write_policy="dry_run_and_approval",
            uid="event-123@test",
            expected_etag='"etag-v1"',
            persist=True,
        )
        self.assertEqual(plan["action"], "delete-event")
        self.assertEqual(
            plan["precondition"],
            {"if_none_match_star": False, "if_match_etag": '"etag-v1"'},
        )
        self.assertIsNone(plan.get("staged_ics"))


class TestApplyEngine(unittest.TestCase):
    """Test approved write execution, lock enforcement, and recovery journal."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.ws = Path(self.temp_dir.name)
        make_backend_config_file(self.ws, write_policy="dry_run_and_approval")
        self.guard_dir = make_mock_guard_dir(self.ws, lock_state="Active", owner_lease="lease-ok-1")
        self.caldav_client = MockCalDAVClient()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _make_plan_and_receipt(
        self,
        action: str = "create-event",
        uid: str = "evt-test-100",
        expected_etag: str | None = None,
        existing_raw_ics: str | None = None,
        updates: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        plan = plan_calendar_write(
            self.ws,
            action=action,
            account="primary-account",
            calendar_alias="primary",
            collection_url="/dav/calendars/user/primary/",
            write_policy="dry_run_and_approval",
            uid=uid,
            event_spec={
                "summary": "Sync Meeting",
                "dtstart": "2026-11-02T10:00:00",
                "dtend": "2026-11-02T11:00:00",
                "timezone": "Europe/Vienna",
            }
            if action == "create-event"
            else None,
            expected_etag=expected_etag,
            existing_raw_ics=existing_raw_ics,
            updates=updates,
            persist=True,
        )
        receipt = {
            "schema": "calendar-desk-approval/1",
            "plan_id": plan["plan_id"],
            "plan_sha256": plan["plan_sha256"],
            "approved": True,
            "approved_by": "operator@test",
            "approved_at": "2026-11-02T08:00:00Z",
        }
        return plan, receipt

    def test_apply_calendar_write_missing_lock_raises(self) -> None:
        plan, receipt = self._make_plan_and_receipt()
        # Invalid lease
        with self.assertRaises(CalendarWriteError) as ctx:
            apply_calendar_write(
                self.ws,
                plan=plan,
                approval_receipt=receipt,
                caldav_client=self.caldav_client,
                lease_id="wrong-lease",
                lock_root=self.guard_dir,
            )
        self.assertEqual(ctx.exception.code, "WORKSPACE_LOCK_REQUIRED")

    def test_apply_calendar_write_unresolved_journal_blocks(self) -> None:
        plan, receipt = self._make_plan_and_receipt()
        journal_dir = self.ws / "data" / "calendar-desk" / "journal"
        journal_dir.mkdir(parents=True, exist_ok=True)
        (journal_dir / "stray-in-progress.json").write_text(
            json.dumps({"status": "in_progress", "plan_id": "stray"}),
            encoding="utf-8",
        )
        with self.assertRaises(CalendarWriteError) as ctx:
            apply_calendar_write(
                self.ws,
                plan=plan,
                approval_receipt=receipt,
                caldav_client=self.caldav_client,
                lease_id="lease-ok-1",
                lock_root=self.guard_dir,
            )
        self.assertEqual(ctx.exception.code, "RECOVERY_REQUIRED")

    def test_apply_calendar_write_invalid_approval_receipt_schema(self) -> None:
        plan, receipt = self._make_plan_and_receipt()
        receipt["schema"] = "invalid-schema/0"
        with self.assertRaises(CalendarWriteError) as ctx:
            apply_calendar_write(
                self.ws,
                plan=plan,
                approval_receipt=receipt,
                caldav_client=self.caldav_client,
                lease_id="lease-ok-1",
                lock_root=self.guard_dir,
            )
        self.assertEqual(ctx.exception.code, "APPROVAL_RECEIPT_INVALID")

    def test_apply_calendar_write_unapproved_receipt(self) -> None:
        plan, receipt = self._make_plan_and_receipt()
        receipt["approved"] = False
        with self.assertRaises(CalendarWriteError) as ctx:
            apply_calendar_write(
                self.ws,
                plan=plan,
                approval_receipt=receipt,
                caldav_client=self.caldav_client,
                lease_id="lease-ok-1",
                lock_root=self.guard_dir,
            )
        self.assertEqual(ctx.exception.code, "APPROVAL_RECEIPT_INVALID")

    def test_apply_calendar_write_missing_approved_by(self) -> None:
        plan, receipt = self._make_plan_and_receipt()
        receipt["approved_by"] = ""
        with self.assertRaises(CalendarWriteError) as ctx:
            apply_calendar_write(
                self.ws,
                plan=plan,
                approval_receipt=receipt,
                caldav_client=self.caldav_client,
                lease_id="lease-ok-1",
                lock_root=self.guard_dir,
            )
        self.assertEqual(ctx.exception.code, "APPROVAL_RECEIPT_INVALID")

    def test_apply_calendar_write_plan_hash_mismatch(self) -> None:
        plan, receipt = self._make_plan_and_receipt()
        receipt["plan_sha256"] = "0" * 64
        with self.assertRaises(CalendarWriteError) as ctx:
            apply_calendar_write(
                self.ws,
                plan=plan,
                approval_receipt=receipt,
                caldav_client=self.caldav_client,
                lease_id="lease-ok-1",
                lock_root=self.guard_dir,
            )
        self.assertEqual(ctx.exception.code, "PLAN_HASH_MISMATCH")

    def test_apply_calendar_write_create_event_success(self) -> None:
        plan, receipt = self._make_plan_and_receipt(action="create-event", uid="evt-create-1")
        result = apply_calendar_write(
            self.ws,
            plan=plan,
            approval_receipt=receipt,
            caldav_client=self.caldav_client,
            lease_id="lease-ok-1",
            lock_root=self.guard_dir,
        )
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["plan_id"], plan["plan_id"])
        self.assertEqual(result["plan_sha256"], plan["plan_sha256"])
        self.assertEqual(result["uid"], "evt-create-1")
        self.assertTrue(bool(result.get("new_etag")))

        # Verify CalDAV call was made with If-None-Match: *
        self.assertEqual(len(self.caldav_client.put_calls), 1)
        put_call = self.caldav_client.put_calls[0]
        self.assertTrue(put_call["if_none_match_star"])

        # Verify journal status transitioned to completed
        journal_file = self.ws / "data" / "calendar-desk" / "journal" / f"{plan['plan_id']}.json"
        self.assertTrue(journal_file.exists())
        journal_data = json.loads(journal_file.read_text(encoding="utf-8"))
        self.assertEqual(journal_data["status"], "completed")

        # Verify evidence register published
        reg_file = self.ws / "memory" / "evidence" / "calendar" / "calendars.json"
        self.assertTrue(reg_file.exists())
        reg_data = json.loads(reg_file.read_text(encoding="utf-8"))
        self.assertEqual(reg_data["schema"], "calendar-desk-register/1")
        self.assertIn("evt-create-1", [e.get("uid") for e in reg_data.get("events", [])])

        # Verify raw ICS stored
        raw_file = (
            self.ws
            / "memory"
            / "evidence"
            / "calendar"
            / "raw"
            / "primary-account"
            / "primary"
            / "evt-create-1.ics"
        )
        self.assertTrue(raw_file.exists())
        self.assertIn("UID:evt-create-1", raw_file.read_text(encoding="utf-8"))

    def test_apply_calendar_write_update_event_success(self) -> None:
        # Prepopulate event in mock client
        res_url = "/dav/calendars/user/primary/event-123@test.ics"
        self.caldav_client.events[res_url] = {"etag": '"etag-v1"', "raw_ics": SAMPLE_VEVENT_ICS}

        plan, receipt = self._make_plan_and_receipt(
            action="update-event",
            uid="event-123@test",
            expected_etag='"etag-v1"',
            existing_raw_ics=SAMPLE_VEVENT_ICS,
            updates={"summary": "Updated Title"},
        )
        result = apply_calendar_write(
            self.ws,
            plan=plan,
            approval_receipt=receipt,
            caldav_client=self.caldav_client,
            lease_id="lease-ok-1",
            lock_root=self.guard_dir,
        )
        self.assertEqual(result["status"], "applied")
        self.assertEqual(len(self.caldav_client.put_calls), 1)
        self.assertEqual(self.caldav_client.put_calls[0]["if_match_etag"], '"etag-v1"')

    def test_apply_calendar_write_cancel_event_success(self) -> None:
        res_url = "/dav/calendars/user/primary/event-123@test.ics"
        self.caldav_client.events[res_url] = {"etag": '"etag-v1"', "raw_ics": SAMPLE_VEVENT_ICS}

        plan, receipt = self._make_plan_and_receipt(
            action="cancel-event",
            uid="event-123@test",
            expected_etag='"etag-v1"',
            existing_raw_ics=SAMPLE_VEVENT_ICS,
        )
        result = apply_calendar_write(
            self.ws,
            plan=plan,
            approval_receipt=receipt,
            caldav_client=self.caldav_client,
            lease_id="lease-ok-1",
            lock_root=self.guard_dir,
        )
        self.assertEqual(result["status"], "applied")
        self.assertEqual(len(self.caldav_client.put_calls), 1)

        # Evidence raw ICS should have STATUS:CANCELLED
        raw_file = (
            self.ws
            / "memory"
            / "evidence"
            / "calendar"
            / "raw"
            / "primary-account"
            / "primary"
            / "event-123@test.ics"
        )
        self.assertTrue(raw_file.exists())
        self.assertIn("STATUS:CANCELLED", raw_file.read_text(encoding="utf-8"))

    def test_apply_calendar_write_delete_event_success(self) -> None:
        res_url = "/dav/calendars/user/primary/event-123@test.ics"
        self.caldav_client.events[res_url] = {"etag": '"etag-v1"', "raw_ics": SAMPLE_VEVENT_ICS}

        # Create pre-existing raw file in evidence to test removal
        raw_file = (
            self.ws
            / "memory"
            / "evidence"
            / "calendar"
            / "raw"
            / "primary-account"
            / "primary"
            / "event-123@test.ics"
        )
        raw_file.parent.mkdir(parents=True, exist_ok=True)
        raw_file.write_text(SAMPLE_VEVENT_ICS, encoding="utf-8")

        plan, receipt = self._make_plan_and_receipt(
            action="delete-event",
            uid="event-123@test",
            expected_etag='"etag-v1"',
        )
        result = apply_calendar_write(
            self.ws,
            plan=plan,
            approval_receipt=receipt,
            caldav_client=self.caldav_client,
            lease_id="lease-ok-1",
            lock_root=self.guard_dir,
        )
        self.assertEqual(result["status"], "applied")
        self.assertEqual(len(self.caldav_client.delete_calls), 1)
        self.assertEqual(self.caldav_client.delete_calls[0]["if_match_etag"], '"etag-v1"')

        # Raw file should be deleted or marked
        self.assertFalse(raw_file.exists())

    def test_apply_calendar_write_etag_conflict(self) -> None:
        # Client configured to raise CALDAV_ETAG_CONFLICT
        conflict_client = MockCalDAVClient(conflict_on_put=True)
        plan, receipt = self._make_plan_and_receipt(action="create-event", uid="evt-conflict-1")

        with self.assertRaises(CalendarWriteError) as ctx:
            apply_calendar_write(
                self.ws,
                plan=plan,
                approval_receipt=receipt,
                caldav_client=conflict_client,
                lease_id="lease-ok-1",
                lock_root=self.guard_dir,
            )
        self.assertEqual(ctx.exception.code, "CALDAV_ETAG_CONFLICT")

        # Journal must transition to aborted_precondition so workspace is NOT blocked
        journal_file = self.ws / "data" / "calendar-desk" / "journal" / f"{plan['plan_id']}.json"
        self.assertTrue(journal_file.exists())
        journal_data = json.loads(journal_file.read_text(encoding="utf-8"))
        self.assertEqual(journal_data["status"], "aborted_precondition")


class TestInspectAndRecover(unittest.TestCase):
    """Test workspace state inspection and recovery reconciliation."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.ws = Path(self.temp_dir.name)
        make_backend_config_file(self.ws, write_policy="dry_run_and_approval")
        self.guard_dir = make_mock_guard_dir(self.ws, lock_state="Active", owner_lease="lease-rec-1")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_inspect_workspace(self) -> None:
        proposals_dir = self.ws / "data" / "calendar-desk" / "proposals"
        proposals_dir.mkdir(parents=True, exist_ok=True)
        (proposals_dir / "p1.json").write_text('{"plan_id": "p1"}', encoding="utf-8")
        (proposals_dir / "p2.json").write_text('{"plan_id": "p2"}', encoding="utf-8")

        journal_dir = self.ws / "data" / "calendar-desk" / "journal"
        journal_dir.mkdir(parents=True, exist_ok=True)
        (journal_dir / "j1.json").write_text('{"status": "completed", "plan_id": "p1"}', encoding="utf-8")
        (journal_dir / "j2.json").write_text('{"status": "aborted_precondition", "plan_id": "p2"}', encoding="utf-8")
        (journal_dir / "j3.json").write_text('{"status": "in_progress", "plan_id": "p3"}', encoding="utf-8")

        inspection = inspect_workspace(self.ws)
        self.assertEqual(inspection["proposals_count"], 2)
        self.assertEqual(inspection["journals_by_status"].get("completed"), 1)
        self.assertEqual(inspection["journals_by_status"].get("aborted_precondition"), 1)
        self.assertEqual(inspection["journals_by_status"].get("in_progress"), 1)
        self.assertEqual(len(inspection["in_progress_plans"]), 1)
        self.assertEqual(inspection["in_progress_plans"][0]["plan_id"], "p3")

    def test_recover_workspace_clean(self) -> None:
        recovery = recover_workspace(
            self.ws,
            lease_id="lease-rec-1",
            lock_root=self.guard_dir,
        )
        self.assertEqual(recovery["status"], "recovered")
        self.assertEqual(recovery["recovered_count"], 0)

    def test_recover_workspace_in_progress_reconciled(self) -> None:
        journal_dir = self.ws / "data" / "calendar-desk" / "journal"
        journal_dir.mkdir(parents=True, exist_ok=True)
        (journal_dir / "plan-rec-99.json").write_text(
            json.dumps({
                "status": "in_progress",
                "plan_id": "plan-rec-99",
                "action": "create-event",
                "account": "primary-account",
                "calendar_alias": "primary",
                "uid": "evt-rec-99",
                "resource_url": "/dav/calendars/user/primary/evt-rec-99.ics",
            }),
            encoding="utf-8",
        )
        caldav_client = MockCalDAVClient()

        recovery = recover_workspace(
            self.ws,
            lease_id="lease-rec-1",
            lock_root=self.guard_dir,
            caldav_client=caldav_client,
        )
        self.assertEqual(recovery["status"], "recovered")
        self.assertEqual(recovery["recovered_count"], 1)

        # Journal status updated
        j_data = json.loads((journal_dir / "plan-rec-99.json").read_text(encoding="utf-8"))
        self.assertIn(j_data["status"], ("recovered", "recovered_aborted", "recovered_applied"))


class TestPhase2Seam(unittest.TestCase):
    """Test Phase-2 seam respond_invitation raises NOT_IMPLEMENTED_PHASE_2."""

    def test_respond_invitation_raises_not_implemented_phase_2(self) -> None:
        with self.assertRaises(CalendarWriteError) as ctx:
            respond_invitation(
                partstat="ACCEPTED",
                uid="evt-invite-1",
                account="primary-account",
                calendar_alias="primary",
            )
        self.assertEqual(ctx.exception.code, "NOT_IMPLEMENTED_PHASE_2")
        self.assertIn("Phase 2", str(ctx.exception))


class MockLoopbackCalDAVHandler(http.server.BaseHTTPRequestHandler):
    """Hermetic loopback CalDAV server for write tests."""

    events: dict[str, dict[str, str]] = {}
    should_conflict: bool = False

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        path = self.path
        if path in self.events:
            data = self.events[path]["raw_ics"].encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/calendar; charset=utf-8")
            self.send_header("ETag", self.events[path]["etag"])
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)
            return

        self.send_response(404)
        self.send_header("Connection", "close")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_PUT(self) -> None:
        if self.should_conflict:
            self.send_response(412)
            self.send_header("Connection", "close")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if_none_match = self.headers.get("If-None-Match")
        if_match = self.headers.get("If-Match")

        if if_none_match == "*" and self.path in self.events:
            self.send_response(412)
            self.send_header("Connection", "close")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if if_match and (self.path not in self.events or self.events[self.path]["etag"] != if_match):
            self.send_response(412)
            self.send_header("Connection", "close")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        raw_ics = self.rfile.read(length).decode("utf-8") if length > 0 else ""
        etag = f'"etag-{hashlib.sha256(raw_ics.encode("utf-8")).hexdigest()[:8]}"'
        self.events[self.path] = {"etag": etag, "raw_ics": raw_ics}

        self.send_response(201)
        self.send_header("ETag", etag)
        self.send_header("Connection", "close")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_DELETE(self) -> None:
        if self.should_conflict:
            self.send_response(412)
            self.send_header("Connection", "close")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if_match = self.headers.get("If-Match")
        if not if_match or self.path not in self.events or self.events[self.path]["etag"] != if_match:
            self.send_response(412)
            self.send_header("Connection", "close")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        del self.events[self.path]
        self.send_response(204)
        self.send_header("Connection", "close")
        self.send_header("Content-Length", "0")
        self.end_headers()


class TestLoopbackCalDAVWriteIntegration(unittest.TestCase):
    """End-to-end write tests against a loopback HTTP server with real CalDAVClient."""

    @classmethod
    def setUpClass(cls) -> None:
        MockLoopbackCalDAVHandler.events = {}
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), MockLoopbackCalDAVHandler)
        cls.port = cls.server.server_address[1]
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=2.0)

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.ws = Path(self.temp_dir.name)
        make_backend_config_file(self.ws, write_policy="dry_run_and_approval")
        self.guard_dir = make_mock_guard_dir(self.ws, lock_state="Active", owner_lease="lease-lb-1")
        self.client = CalDAVClient(
            principal_url=f"{self.base_url}/dav/principals",
            username="user",
            password="pass",
            allow_loopback_http=True,
            timeout_seconds=5.0,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_loopback_create_and_delete_flow(self) -> None:
        coll_url = f"{self.base_url}/dav/calendars/testuser/primary/"
        plan = plan_calendar_write(
            self.ws,
            action="create-event",
            account="primary-account",
            calendar_alias="primary",
            collection_url=coll_url,
            write_policy="dry_run_and_approval",
            uid="evt-loopback-1",
            event_spec={
                "summary": "Loopback Integration Event",
                "dtstart": "2026-11-02T14:00:00",
                "dtend": "2026-11-02T15:00:00",
                "timezone": "Europe/Vienna",
            },
            persist=True,
        )
        receipt = {
            "schema": "calendar-desk-approval/1",
            "plan_id": plan["plan_id"],
            "plan_sha256": plan["plan_sha256"],
            "approved": True,
            "approved_by": "qa-tester",
        }
        res_create = apply_calendar_write(
            self.ws,
            plan=plan,
            approval_receipt=receipt,
            caldav_client=self.client,
            lease_id="lease-lb-1",
            lock_root=self.guard_dir,
        )
        self.assertEqual(res_create["status"], "applied")
        created_etag = res_create["new_etag"]

        # Now delete the event using its ETag
        plan_del = plan_calendar_write(
            self.ws,
            action="delete-event",
            account="primary-account",
            calendar_alias="primary",
            collection_url=coll_url,
            write_policy="dry_run_and_approval",
            uid="evt-loopback-1",
            expected_etag=created_etag,
            persist=True,
        )
        del_receipt = {
            "schema": "calendar-desk-approval/1",
            "plan_id": plan_del["plan_id"],
            "plan_sha256": plan_del["plan_sha256"],
            "approved": True,
            "approved_by": "qa-tester",
        }
        res_del = apply_calendar_write(
            self.ws,
            plan=plan_del,
            approval_receipt=del_receipt,
            caldav_client=self.client,
            lease_id="lease-lb-1",
            lock_root=self.guard_dir,
        )
        self.assertEqual(res_del["status"], "applied")


class TestCalendarDeskClientCliWrites(unittest.TestCase):
    """Test CLI subcommands for plan, apply, inspect, recover, and respond-invitation."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.ws = Path(self.temp_dir.name)
        make_backend_config_file(self.ws, write_policy="dry_run_and_approval")
        self.guard_dir = make_mock_guard_dir(self.ws, lock_state="Active", owner_lease="lease-cli-1")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_cli_plan_subcommand_json(self) -> None:
        stdout_buf = io.StringIO()
        with contextlib.redirect_stdout(stdout_buf):
            ret = calendar_desk_client.main([
                "--workspace",
                str(self.ws),
                "--json",
                "plan",
                "--action",
                "create-event",
                "--summary",
                "CLI Created Event",
                "--start",
                "2026-11-02T10:00:00",
                "--end",
                "2026-11-02T11:00:00",
            ])
        self.assertEqual(ret, 0)
        output = json.loads(stdout_buf.getvalue())
        self.assertEqual(set(output.keys()), {"action", "success", "state", "message", "data", "error"})
        self.assertTrue(output["success"])
        self.assertEqual(output["state"], "Completed")
        self.assertEqual(output["action"], "plan")
        self.assertIsInstance(output["message"], str)
        self.assertIsNone(output["error"])
        self.assertIn("plan_id", output.get("data", {}))
        self.assertIn("plan_sha256", output.get("data", {}))

    def test_cli_inspect_subcommand_json(self) -> None:
        stdout_buf = io.StringIO()
        with contextlib.redirect_stdout(stdout_buf):
            ret = calendar_desk_client.main([
                "--workspace",
                str(self.ws),
                "--json",
                "inspect",
            ])
        self.assertEqual(ret, 0)
        output = json.loads(stdout_buf.getvalue())
        self.assertEqual(set(output.keys()), {"action", "success", "state", "message", "data", "error"})
        self.assertTrue(output["success"])
        self.assertEqual(output["state"], "Completed")
        self.assertEqual(output["action"], "inspect")
        self.assertIsInstance(output["message"], str)
        self.assertIsNone(output["error"])
        self.assertIn("proposals_count", output.get("data", {}))

    def test_cli_respond_invitation_phase_2_error(self) -> None:
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
            ret = calendar_desk_client.main([
                "--workspace",
                str(self.ws),
                "--json",
                "respond-invitation",
                "--partstat",
                "ACCEPTED",
                "--uid",
                "evt-123",
            ])
        self.assertEqual(ret, 1)
        output = json.loads(stdout_buf.getvalue())
        self.assertEqual(set(output.keys()), {"action", "success", "state", "message", "data", "error"})
        self.assertFalse(output["success"])
        self.assertEqual(output["state"], "Failed")
        self.assertEqual(output["action"], "respond-invitation")
        self.assertIsInstance(output["message"], str)
        self.assertEqual(output["data"], {})
        self.assertIsNotNone(output["error"])
        self.assertIsInstance(output["error"], dict)
        self.assertEqual(set(output["error"].keys()), {"type", "message", "details"})
        self.assertEqual(output["error"]["type"], "NOT_IMPLEMENTED_PHASE_2")

    def test_cli_exit_code_0_success_state_completed(self) -> None:
        """PR #82 Review #26: Successful execution yields exit code 0 and state Completed."""
        stdout_buf = io.StringIO()
        with contextlib.redirect_stdout(stdout_buf):
            ret = calendar_desk_client.main([
                "--workspace", str(self.ws),
                "--json",
                "inspect",
            ])
        self.assertEqual(ret, 0)
        output = json.loads(stdout_buf.getvalue())
        self.assertEqual(set(output.keys()), {"action", "success", "state", "message", "data", "error"})
        self.assertTrue(output["success"])
        self.assertEqual(output["state"], "Completed")
        self.assertIsNone(output["error"])

    def test_cli_exit_code_1_configuration_error(self) -> None:
        """PR #82 Review #26: Configuration or input error yields exit code 1 and state Failed."""
        with mock.patch("calendar_desk_client.apply_calendar_write") as mock_apply:
            mock_apply.side_effect = CalDAVConfigError("CONFIG_ERROR", "Invalid config file")
            stdout_buf = io.StringIO()
            with contextlib.redirect_stdout(stdout_buf):
                ret = calendar_desk_client.main([
                    "--workspace", str(self.ws),
                    "--json",
                    "apply",
                    "--plan-file", "dummy-plan.json",
                    "--receipt-file", "dummy-receipt.json",
                    "--lease-id", "lease-1",
                ])
            self.assertEqual(ret, 1)
            output = json.loads(stdout_buf.getvalue())
            self.assertEqual(set(output.keys()), {"action", "success", "state", "message", "data", "error"})
            self.assertFalse(output["success"])
            self.assertEqual(output["state"], "Failed")
            self.assertEqual(output["data"], {})
            self.assertIsNotNone(output["error"])
            self.assertEqual(output["error"]["type"], "CONFIG_ERROR")

    def test_cli_exit_code_2_write_policy_violation(self) -> None:
        """PR #82 Review #26: Precondition / write-policy violation yields exit code 2 and state Failed."""
        with mock.patch("calendar_desk_client.apply_calendar_write") as mock_apply:
            mock_apply.side_effect = CalendarWriteError("WRITE_POLICY_VIOLATION", "Write policy violation")
            stdout_buf = io.StringIO()
            with contextlib.redirect_stdout(stdout_buf):
                ret = calendar_desk_client.main([
                    "--workspace", str(self.ws),
                    "--json",
                    "apply",
                    "--plan-file", "dummy-plan.json",
                    "--receipt-file", "dummy-receipt.json",
                    "--lease-id", "lease-1",
                ])
            self.assertEqual(ret, 2)
            output = json.loads(stdout_buf.getvalue())
            self.assertEqual(set(output.keys()), {"action", "success", "state", "message", "data", "error"})
            self.assertFalse(output["success"])
            self.assertEqual(output["state"], "Failed")
            self.assertEqual(output["data"], {})
            self.assertIsNotNone(output["error"])
            self.assertEqual(output["error"]["type"], "WRITE_POLICY_VIOLATION")

    def test_cli_exit_code_2_calendar_read_only(self) -> None:
        """PR #82 Review #26: Read-only calendar yields exit code 2 and state Failed."""
        with mock.patch("calendar_desk_client.apply_calendar_write") as mock_apply:
            mock_apply.side_effect = CalendarWriteError("CALENDAR_READ_ONLY", "Calendar is read-only")
            stdout_buf = io.StringIO()
            with contextlib.redirect_stdout(stdout_buf):
                ret = calendar_desk_client.main([
                    "--workspace", str(self.ws),
                    "--json",
                    "apply",
                    "--plan-file", "dummy-plan.json",
                    "--receipt-file", "dummy-receipt.json",
                    "--lease-id", "lease-1",
                ])
            self.assertEqual(ret, 2)
            output = json.loads(stdout_buf.getvalue())
            self.assertEqual(set(output.keys()), {"action", "success", "state", "message", "data", "error"})
            self.assertFalse(output["success"])
            self.assertEqual(output["state"], "Failed")
            self.assertEqual(output["data"], {})
            self.assertIsNotNone(output["error"])
            self.assertEqual(output["error"]["type"], "CALENDAR_READ_ONLY")

    def test_cli_exit_code_2_caldav_etag_conflict(self) -> None:
        """PR #82 Review #26: CalDAV ETag conflict (412) yields exit code 2 and state Failed."""
        with mock.patch("calendar_desk_client.apply_calendar_write") as mock_apply:
            mock_apply.side_effect = CalendarWriteError("CALDAV_ETAG_CONFLICT", "Precondition Failed (412)")
            stdout_buf = io.StringIO()
            with contextlib.redirect_stdout(stdout_buf):
                ret = calendar_desk_client.main([
                    "--workspace", str(self.ws),
                    "--json",
                    "apply",
                    "--plan-file", "dummy-plan.json",
                    "--receipt-file", "dummy-receipt.json",
                    "--lease-id", "lease-1",
                ])
            self.assertEqual(ret, 2)
            output = json.loads(stdout_buf.getvalue())
            self.assertEqual(set(output.keys()), {"action", "success", "state", "message", "data", "error"})
            self.assertFalse(output["success"])
            self.assertEqual(output["state"], "Failed")
            self.assertEqual(output["data"], {})
            self.assertIsNotNone(output["error"])
            self.assertEqual(output["error"]["type"], "CALDAV_ETAG_CONFLICT")

    def test_cli_exit_code_3_recovery_required(self) -> None:
        """PR #82 Review #26: Recovery-required yields exit code 3 and state Failed."""
        with mock.patch("calendar_desk_client.apply_calendar_write") as mock_apply:
            mock_apply.side_effect = CalendarWriteError("RECOVERY_REQUIRED", "In-progress journal requires recovery")
            stdout_buf = io.StringIO()
            with contextlib.redirect_stdout(stdout_buf):
                ret = calendar_desk_client.main([
                    "--workspace", str(self.ws),
                    "--json",
                    "apply",
                    "--plan-file", "dummy-plan.json",
                    "--receipt-file", "dummy-receipt.json",
                    "--lease-id", "lease-1",
                ])
            self.assertEqual(ret, 3)
            output = json.loads(stdout_buf.getvalue())
            self.assertEqual(set(output.keys()), {"action", "success", "state", "message", "data", "error"})
            self.assertFalse(output["success"])
            self.assertEqual(output["state"], "Failed")
            self.assertEqual(output["data"], {})
            self.assertIsNotNone(output["error"])
            self.assertEqual(output["error"]["type"], "RECOVERY_REQUIRED")


class TestWritePolicyAndTargetValidation(unittest.TestCase):
    """Test #4: apply_calendar_write validates write_policy and collection_url containment."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.ws = Path(self.temp_dir.name)
        make_backend_config_file(self.ws, write_policy="read_only")
        self.guard_dir = make_mock_guard_dir(self.ws, lock_state="Active", owner_lease="lease-val-1")
        self.client = MockCalDAVClient()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_apply_reverifies_write_policy_read_only_raises(self) -> None:
        plan = {
            "schema": "calendar-desk-plan/1",
            "plan_id": "plan-policy-1",
            "action": "create-event",
            "account": "primary-account",
            "calendar_alias": "primary",
            "collection_url": "https://cal.example.com/dav/calendars/user/primary/",
            "resource_url": "https://cal.example.com/dav/calendars/user/primary/evt-pol-1.ics",
            "uid": "evt-pol-1",
            "precondition": {"if_none_match_star": True, "if_match_etag": None},
            "staged_ics": SAMPLE_VEVENT_ICS,
            "canonical_ics_sha256": hashlib.sha256(SAMPLE_VEVENT_ICS.encode("utf-8")).hexdigest(),
            "diff_summary": "Create event",
        }
        plan["plan_sha256"] = compute_plan_sha256(plan)
        receipt = {
            "schema": "calendar-desk-approval/1",
            "plan_id": plan["plan_id"],
            "plan_sha256": plan["plan_sha256"],
            "approved": True,
            "approved_by": "operator@test",
        }
        with self.assertRaises(CalendarWriteError) as ctx:
            apply_calendar_write(
                self.ws,
                plan=plan,
                approval_receipt=receipt,
                caldav_client=self.client,
                lease_id="lease-val-1",
                lock_root=self.guard_dir,
            )
        self.assertEqual(ctx.exception.code, "CALENDAR_WRITE_FORBIDDEN")

    def test_apply_rejects_resource_url_outside_collection(self) -> None:
        make_backend_config_file(self.ws, write_policy="dry_run_and_approval")
        plan = {
            "schema": "calendar-desk-plan/1",
            "plan_id": "plan-outside-1",
            "action": "create-event",
            "account": "primary-account",
            "calendar_alias": "primary",
            "collection_url": "/dav/calendars/user/primary/",
            "resource_url": "/dav/calendars/other-user/stolen/evt-outside-1.ics",
            "uid": "evt-outside-1",
            "precondition": {"if_none_match_star": True, "if_match_etag": None},
            "staged_ics": SAMPLE_VEVENT_ICS,
            "canonical_ics_sha256": hashlib.sha256(SAMPLE_VEVENT_ICS.encode("utf-8")).hexdigest(),
            "diff_summary": "Create event outside collection",
        }
        plan["plan_sha256"] = compute_plan_sha256(plan)
        receipt = {
            "schema": "calendar-desk-approval/1",
            "plan_id": plan["plan_id"],
            "plan_sha256": plan["plan_sha256"],
            "approved": True,
            "approved_by": "operator@test",
        }
        with self.assertRaises(CalendarWriteError) as ctx:
            apply_calendar_write(
                self.ws,
                plan=plan,
                approval_receipt=receipt,
                caldav_client=self.client,
                lease_id="lease-val-1",
                lock_root=self.guard_dir,
            )
        self.assertIn(ctx.exception.code, ("CALENDAR_WRITE_FORBIDDEN", "WRITE_VALIDATION_ERROR"))


class TestUnknownRemoteOutcomeAndExitCode(unittest.TestCase):
    """Test #5: Network drop during PUT marks journal unknown_remote_outcome and exits code 3."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.ws = Path(self.temp_dir.name)
        make_backend_config_file(self.ws, write_policy="dry_run_and_approval")
        self.guard_dir = make_mock_guard_dir(self.ws, lock_state="Active", owner_lease="lease-net-1")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_apply_network_drop_marks_journal_unknown_outcome_and_blocks(self) -> None:
        client = MockCalDAVClient(network_error_on_put=True)
        plan = {
            "schema": "calendar-desk-plan/1",
            "plan_id": "plan-net-drop-1",
            "action": "create-event",
            "account": "primary-account",
            "calendar_alias": "primary",
            "collection_url": "/dav/calendars/user/primary/",
            "resource_url": "/dav/calendars/user/primary/evt-net-1.ics",
            "uid": "evt-net-1",
            "precondition": {"if_none_match_star": True, "if_match_etag": None},
            "staged_ics": SAMPLE_VEVENT_ICS,
            "canonical_ics_sha256": hashlib.sha256(SAMPLE_VEVENT_ICS.encode("utf-8")).hexdigest(),
            "diff_summary": "Create event",
        }
        plan["plan_sha256"] = compute_plan_sha256(plan)
        receipt = {
            "schema": "calendar-desk-approval/1",
            "plan_id": plan["plan_id"],
            "plan_sha256": plan["plan_sha256"],
            "approved": True,
            "approved_by": "operator@test",
        }
        with self.assertRaises((CalendarWriteError, CalDAVProtocolError)):
            apply_calendar_write(
                self.ws,
                plan=plan,
                approval_receipt=receipt,
                caldav_client=client,
                lease_id="lease-net-1",
                lock_root=self.guard_dir,
            )

        journal_file = self.ws / "data" / "calendar-desk" / "journal" / f"{plan['plan_id']}.json"
        self.assertTrue(journal_file.exists())
        journal_data = json.loads(journal_file.read_text(encoding="utf-8"))
        self.assertIn(journal_data["status"], ("unknown_remote_outcome", "recovery_required"))

        # Subsequent writes must be blocked by _check_no_in_progress_journal
        with self.assertRaises(CalendarWriteError) as ctx:
            _check_no_in_progress_journal(self.ws)
        self.assertEqual(ctx.exception.code, "RECOVERY_REQUIRED")

    def test_cli_apply_network_drop_exits_code_3_recovery_required(self) -> None:
        proposal_file = self.ws / "data" / "calendar-desk" / "proposals" / "plan-cli-net.json"
        proposal_file.parent.mkdir(parents=True, exist_ok=True)
        plan = {
            "schema": "calendar-desk-plan/1",
            "plan_id": "plan-cli-net",
            "action": "create-event",
            "account": "primary-account",
            "calendar_alias": "primary",
            "collection_url": "/dav/calendars/user/primary/",
            "resource_url": "/dav/calendars/user/primary/evt-cli-net.ics",
            "uid": "evt-cli-net",
            "precondition": {"if_none_match_star": True, "if_match_etag": None},
            "staged_ics": SAMPLE_VEVENT_ICS,
            "canonical_ics_sha256": hashlib.sha256(SAMPLE_VEVENT_ICS.encode("utf-8")).hexdigest(),
            "diff_summary": "CLI network test",
        }
        plan["plan_sha256"] = compute_plan_sha256(plan)
        proposal_file.write_text(json.dumps(plan, indent=2), encoding="utf-8")

        receipt_file = self.ws / "receipt.json"
        receipt = {
            "schema": "calendar-desk-approval/1",
            "plan_id": plan["plan_id"],
            "plan_sha256": plan["plan_sha256"],
            "approved": True,
            "approved_by": "operator@test",
        }
        receipt_file.write_text(json.dumps(receipt, indent=2), encoding="utf-8")

        secrets_file = self.ws / ".agents" / "secrets.json"
        secrets_file.write_text(json.dumps({
            "schema": "calendar-desk-secrets/1",
            "accounts": {"primary-account": {"username": "u", "password": "p"}}
        }), encoding="utf-8")

        mock_client = MockCalDAVClient(network_error_on_put=True)
        with mock.patch("calendar_desk_client.CalDAVClient", return_value=mock_client):
            stdout_buf = io.StringIO()
            with contextlib.redirect_stdout(stdout_buf):
                exit_code = calendar_desk_client.main([
                    "--workspace", str(self.ws),
                    "--json",
                    "apply",
                    "--plan-file", str(proposal_file),
                    "--receipt-file", str(receipt_file),
                    "--lease-id", "lease-net-1",
                    "--lock-root", str(self.guard_dir),
                ])
        self.assertEqual(exit_code, 3)
        output = json.loads(stdout_buf.getvalue())
        self.assertEqual(set(output.keys()), {"action", "success", "state", "message", "data", "error"})
        self.assertFalse(output["success"])
        self.assertEqual(output["state"], "Failed")
        self.assertEqual(output["action"], "apply")
        self.assertEqual(output["data"], {})
        self.assertIsNotNone(output["error"])
        self.assertEqual(output["error"]["type"], "RECOVERY_REQUIRED")


class TestCrashContractAndDuplicateApply(unittest.TestCase):
    """Test #6: Journal contains pre_state and staged payload; rejects duplicate apply."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.ws = Path(self.temp_dir.name)
        make_backend_config_file(self.ws, write_policy="dry_run_and_approval")
        self.guard_dir = make_mock_guard_dir(self.ws, lock_state="Active", owner_lease="lease-cc-1")
        self.client = MockCalDAVClient()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_journal_contains_pre_state_and_staged_ics(self) -> None:
        res_url = "/dav/calendars/user/primary/event-123@test.ics"
        self.client.events[res_url] = {"etag": '"etag-v1"', "raw_ics": SAMPLE_VEVENT_ICS}

        plan = plan_calendar_write(
            self.ws,
            action="update-event",
            account="primary-account",
            calendar_alias="primary",
            collection_url="/dav/calendars/user/primary/",
            write_policy="dry_run_and_approval",
            uid="event-123@test",
            expected_etag='"etag-v1"',
            existing_raw_ics=SAMPLE_VEVENT_ICS,
            updates={"summary": "Updated Title"},
            persist=True,
        )
        receipt = {
            "schema": "calendar-desk-approval/1",
            "plan_id": plan["plan_id"],
            "plan_sha256": plan["plan_sha256"],
            "approved": True,
            "approved_by": "operator@test",
        }
        apply_calendar_write(
            self.ws,
            plan=plan,
            approval_receipt=receipt,
            caldav_client=self.client,
            lease_id="lease-cc-1",
            lock_root=self.guard_dir,
        )
        journal_file = self.ws / "data" / "calendar-desk" / "journal" / f"{plan['plan_id']}.json"
        self.assertTrue(journal_file.exists())
        jdata = json.loads(journal_file.read_text(encoding="utf-8"))

        self.assertIn("staged_ics", jdata)
        self.assertEqual(jdata["staged_ics"], plan["staged_ics"])
        self.assertIn("pre_state", jdata)
        self.assertIsInstance(jdata["pre_state"], dict)
        self.assertEqual(jdata["pre_state"].get("etag"), '"etag-v1"')

    def test_apply_duplicate_plan_raises_if_journal_exists(self) -> None:
        plan = plan_calendar_write(
            self.ws,
            action="create-event",
            account="primary-account",
            calendar_alias="primary",
            collection_url="/dav/calendars/user/primary/",
            write_policy="dry_run_and_approval",
            uid="evt-dup-1",
            event_spec={"summary": "Dup", "dtstart": "2026-11-02T10:00:00"},
            persist=True,
        )
        receipt = {
            "schema": "calendar-desk-approval/1",
            "plan_id": plan["plan_id"],
            "plan_sha256": plan["plan_sha256"],
            "approved": True,
            "approved_by": "operator@test",
        }
        journal_dir = self.ws / "data" / "calendar-desk" / "journal"
        journal_dir.mkdir(parents=True, exist_ok=True)
        (journal_dir / f"{plan['plan_id']}.json").write_text(
            json.dumps({"status": "completed", "plan_id": plan["plan_id"]}),
            encoding="utf-8",
        )
        with self.assertRaises(CalendarWriteError) as ctx:
            apply_calendar_write(
                self.ws,
                plan=plan,
                approval_receipt=receipt,
                caldav_client=self.client,
                lease_id="lease-cc-1",
                lock_root=self.guard_dir,
            )
        self.assertIn(ctx.exception.code, ("WRITE_VALIDATION_ERROR", "RECOVERY_REQUIRED"))


class TestDamagedJournalFailClosed(unittest.TestCase):
    """Test #7: Damaged or unreadable journals fail closed across inspect, check, and recover."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.ws = Path(self.temp_dir.name)
        make_backend_config_file(self.ws, write_policy="dry_run_and_approval")
        self.guard_dir = make_mock_guard_dir(self.ws, lock_state="Active", owner_lease="lease-dam-1")
        self.journal_dir = self.ws / "data" / "calendar-desk" / "journal"
        self.journal_dir.mkdir(parents=True, exist_ok=True)
        (self.journal_dir / "corrupted-in-prog.json").write_text('{"status": "in_prog', encoding="utf-8")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_check_no_in_progress_journal_damaged_fails_closed(self) -> None:
        with self.assertRaises(CalendarWriteError) as ctx:
            _check_no_in_progress_journal(self.ws)
        self.assertIn(ctx.exception.code, ("RECOVERY_REQUIRED", "WRITE_VALIDATION_ERROR"))

    def test_inspect_workspace_damaged_journal_fails_closed(self) -> None:
        with self.assertRaises(CalendarWriteError) as ctx:
            inspect_workspace(self.ws)
        self.assertIn(ctx.exception.code, ("RECOVERY_REQUIRED", "WRITE_VALIDATION_ERROR"))

    def test_recover_workspace_damaged_journal_fails_closed(self) -> None:
        with self.assertRaises(CalendarWriteError) as ctx:
            recover_workspace(
                self.ws,
                lease_id="lease-dam-1",
                lock_root=self.guard_dir,
            )
        self.assertIn(ctx.exception.code, ("RECOVERY_REQUIRED", "WRITE_VALIDATION_ERROR"))


class TestRecoveryPostconditions(unittest.TestCase):
    """Test #8: Postcondition verification in recover_workspace."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.ws = Path(self.temp_dir.name)
        make_backend_config_file(self.ws, write_policy="dry_run_and_approval")
        self.guard_dir = make_mock_guard_dir(self.ws, lock_state="Active", owner_lease="lease-post-1")
        self.journal_dir = self.ws / "data" / "calendar-desk" / "journal"
        self.journal_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_recover_delete_event_404_sets_recovered_applied(self) -> None:
        res_url = "/dav/calendars/user/primary/evt-del-1.ics"
        jfile = self.journal_dir / "plan-del-1.json"
        jfile.write_text(json.dumps({
            "status": "in_progress",
            "plan_id": "plan-del-1",
            "action": "delete-event",
            "account": "primary-account",
            "calendar_alias": "primary",
            "uid": "evt-del-1",
            "resource_url": res_url,
            "precondition": {"if_match_etag": '"etag-v1"'},
        }), encoding="utf-8")

        client = MockCalDAVClient()
        res = recover_workspace(
            self.ws,
            lease_id="lease-post-1",
            lock_root=self.guard_dir,
            caldav_client=client,
        )
        self.assertEqual(res["status"], "recovered")
        jdata = json.loads(jfile.read_text(encoding="utf-8"))
        self.assertEqual(jdata["status"], "recovered_applied")

    def test_recover_delete_event_still_present_sets_recovered_aborted(self) -> None:
        res_url = "/dav/calendars/user/primary/evt-del-2.ics"
        jfile = self.journal_dir / "plan-del-2.json"
        jfile.write_text(json.dumps({
            "status": "in_progress",
            "plan_id": "plan-del-2",
            "action": "delete-event",
            "account": "primary-account",
            "calendar_alias": "primary",
            "uid": "evt-del-2",
            "resource_url": res_url,
            "precondition": {"if_match_etag": '"etag-v1"'},
        }), encoding="utf-8")

        client = MockCalDAVClient()
        client.events[res_url] = {"etag": '"etag-v1"', "raw_ics": SAMPLE_VEVENT_ICS}

        res = recover_workspace(
            self.ws,
            lease_id="lease-post-1",
            lock_root=self.guard_dir,
            caldav_client=client,
        )
        self.assertEqual(res["status"], "recovered")
        jdata = json.loads(jfile.read_text(encoding="utf-8"))
        self.assertEqual(jdata["status"], "recovered_aborted")

    def test_recover_update_event_etag_match_sets_recovered_applied(self) -> None:
        res_url = "/dav/calendars/user/primary/evt-upd-1.ics"
        jfile = self.journal_dir / "plan-upd-1.json"
        jfile.write_text(json.dumps({
            "status": "in_progress",
            "plan_id": "plan-upd-1",
            "action": "update-event",
            "account": "primary-account",
            "calendar_alias": "primary",
            "uid": "evt-upd-1",
            "resource_url": res_url,
            "precondition": {"if_match_etag": '"etag-v1"'},
            "pre_state": {"etag": '"etag-v1"'},
        }), encoding="utf-8")

        client = MockCalDAVClient()
        client.events[res_url] = {"etag": '"etag-v2-new"', "raw_ics": SAMPLE_VEVENT_ICS}

        recover_workspace(
            self.ws,
            lease_id="lease-post-1",
            lock_root=self.guard_dir,
            caldav_client=client,
        )
        jdata = json.loads(jfile.read_text(encoding="utf-8"))
        self.assertEqual(jdata["status"], "recovered_applied")

    def test_recover_update_event_old_etag_sets_recovered_aborted(self) -> None:
        res_url = "/dav/calendars/user/primary/evt-upd-2.ics"
        jfile = self.journal_dir / "plan-upd-2.json"
        jfile.write_text(json.dumps({
            "status": "in_progress",
            "plan_id": "plan-upd-2",
            "action": "update-event",
            "account": "primary-account",
            "calendar_alias": "primary",
            "uid": "evt-upd-2",
            "resource_url": res_url,
            "precondition": {"if_match_etag": '"etag-v1"'},
            "pre_state": {"etag": '"etag-v1"'},
        }), encoding="utf-8")

        client = MockCalDAVClient()
        client.events[res_url] = {"etag": '"etag-v1"', "raw_ics": SAMPLE_VEVENT_ICS}

        recover_workspace(
            self.ws,
            lease_id="lease-post-1",
            lock_root=self.guard_dir,
            caldav_client=client,
        )
        jdata = json.loads(jfile.read_text(encoding="utf-8"))
        self.assertEqual(jdata["status"], "recovered_aborted")

    def test_recover_without_client_or_unreachable_server_stays_recovery_required(self) -> None:
        jfile = self.journal_dir / "plan-no-client.json"
        jfile.write_text(json.dumps({
            "status": "in_progress",
            "plan_id": "plan-no-client",
            "action": "create-event",
            "account": "primary-account",
            "calendar_alias": "primary",
            "uid": "evt-no-cli",
            "resource_url": "/dav/calendars/user/primary/evt-no-cli.ics",
        }), encoding="utf-8")

        with self.assertRaises(CalendarWriteError) as ctx:
            recover_workspace(
                self.ws,
                lease_id="lease-post-1",
                lock_root=self.guard_dir,
                caldav_client=None,
            )
        self.assertEqual(ctx.exception.code, "RECOVERY_REQUIRED")
        jdata = json.loads(jfile.read_text(encoding="utf-8"))
        self.assertNotEqual(jdata.get("status"), "recovered")


class TestPathTraversalContainment(unittest.TestCase):
    """Test #9: Rejection of path and URL traversal attacks in uid and plan_id."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.ws = Path(self.temp_dir.name)
        make_backend_config_file(self.ws, write_policy="dry_run_and_approval")
        self.guard_dir = make_mock_guard_dir(self.ws, lock_state="Active", owner_lease="lease-trav-1")
        self.client = MockCalDAVClient()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_plan_rejects_path_traversal_in_uid(self) -> None:
        traversal_uids = [
            "../../../../escaped",
            "..\\..\\..\\escaped",
            "/etc/passwd",
            "subdir/event",
        ]
        for bad_uid in traversal_uids:
            with self.subTest(uid=bad_uid):
                with self.assertRaises(CalendarWriteError) as ctx:
                    plan_calendar_write(
                        self.ws,
                        action="create-event",
                        account="primary-account",
                        calendar_alias="primary",
                        collection_url="/dav/calendars/user/primary/",
                        write_policy="dry_run_and_approval",
                        uid=bad_uid,
                        event_spec={"summary": "Escape", "dtstart": "2026-11-02T10:00:00"},
                    )
                self.assertEqual(ctx.exception.code, "WRITE_VALIDATION_ERROR")

    def test_apply_rejects_path_traversal_in_uid_or_plan_id(self) -> None:
        plan = {
            "schema": "calendar-desk-plan/1",
            "plan_id": "../evil-plan",
            "action": "create-event",
            "account": "primary-account",
            "calendar_alias": "primary",
            "collection_url": "/dav/calendars/user/primary/",
            "resource_url": "/dav/calendars/user/primary/safe-uid.ics",
            "uid": "safe-uid",
            "precondition": {"if_none_match_star": True, "if_match_etag": None},
            "staged_ics": SAMPLE_VEVENT_ICS,
            "canonical_ics_sha256": hashlib.sha256(SAMPLE_VEVENT_ICS.encode("utf-8")).hexdigest(),
            "diff_summary": "Escape",
        }
        plan["plan_sha256"] = compute_plan_sha256(plan)
        receipt = {
            "schema": "calendar-desk-approval/1",
            "plan_id": plan["plan_id"],
            "plan_sha256": plan["plan_sha256"],
            "approved": True,
            "approved_by": "operator@test",
        }
        with self.assertRaises(CalendarWriteError) as ctx:
            apply_calendar_write(
                self.ws,
                plan=plan,
                approval_receipt=receipt,
                caldav_client=self.client,
                lease_id="lease-trav-1",
                lock_root=self.guard_dir,
            )
        self.assertEqual(ctx.exception.code, "WRITE_VALIDATION_ERROR")


class TestPlanUpdateEventDataPropagation(unittest.TestCase):
    """Test #22: plan update-event carries forward summary, start, end, etc."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.ws = Path(self.temp_dir.name)
        make_backend_config_file(self.ws, write_policy="dry_run_and_approval")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_plan_update_event_propagates_summary_start_end_to_staged_ics(self) -> None:
        plan = plan_calendar_write(
            self.ws,
            action="update-event",
            account="primary-account",
            calendar_alias="primary",
            collection_url="/dav/calendars/user/primary/",
            write_policy="dry_run_and_approval",
            uid="event-123@test",
            expected_etag='"etag-v1"',
            existing_raw_ics=SAMPLE_VEVENT_ICS,
            event_spec={
                "summary": "Updated Meeting Title",
                "start": "2026-11-03T14:00:00",
                "end": "2026-11-03T15:00:00",
                "timezone": "Europe/Vienna",
            },
            persist=True,
        )
        self.assertIn("SUMMARY:Updated Meeting Title", plan["staged_ics"])
        self.assertIn("20261103", plan["staged_ics"])

    def test_cli_plan_update_event_propagates_cli_args_to_proposal(self) -> None:
        raw_file = self.ws / "existing.ics"
        raw_file.write_text(SAMPLE_VEVENT_ICS, encoding="utf-8")

        stdout_buf = io.StringIO()
        with contextlib.redirect_stdout(stdout_buf):
            ret = calendar_desk_client.main([
                "--workspace", str(self.ws),
                "--json",
                "plan",
                "--action", "update-event",
                "--uid", "event-123@test",
                "--etag", '"etag-v1"',
                "--raw-ics-file", str(raw_file),
                "--summary", "CLI Updated Meeting",
                "--start", "2026-11-03T14:00:00",
                "--end", "2026-11-03T15:00:00",
            ])
        self.assertEqual(ret, 0)
        output = json.loads(stdout_buf.getvalue())
        self.assertEqual(set(output.keys()), {"action", "success", "state", "message", "data", "error"})
        self.assertTrue(output["success"])
        self.assertEqual(output["state"], "Completed")
        self.assertIsNone(output["error"])
        data = output.get("data", {})
        self.assertIn("SUMMARY:CLI Updated Meeting", data.get("staged_ics", ""))
        self.assertIn("20261103", data.get("staged_ics", ""))


class TestRegisterIntegrityAndDeduplication(unittest.TestCase):
    """Test #25: calendars.json fails closed on corruption and deduplicates across calendar/account."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.ws = Path(self.temp_dir.name)
        make_backend_config_file(self.ws, write_policy="dry_run_and_approval")
        self.guard_dir = make_mock_guard_dir(self.ws, lock_state="Active", owner_lease="lease-reg-1")
        self.client = MockCalDAVClient()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_apply_damaged_calendars_json_fails_closed(self) -> None:
        evidence_dir = self.ws / "memory" / "evidence" / "calendar"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        reg_file = evidence_dir / "calendars.json"
        reg_file.write_text('{"schema": "calendar-desk-register/1", "events": [broken', encoding="utf-8")

        plan = {
            "schema": "calendar-desk-plan/1",
            "plan_id": "plan-corrupt-reg-1",
            "action": "create-event",
            "account": "primary-account",
            "calendar_alias": "primary",
            "collection_url": "/dav/calendars/user/primary/",
            "resource_url": "/dav/calendars/user/primary/evt-corrupt-1.ics",
            "uid": "evt-corrupt-1",
            "precondition": {"if_none_match_star": True, "if_match_etag": None},
            "staged_ics": SAMPLE_VEVENT_ICS,
            "canonical_ics_sha256": hashlib.sha256(SAMPLE_VEVENT_ICS.encode("utf-8")).hexdigest(),
            "diff_summary": "Test corrupted register",
        }
        plan["plan_sha256"] = compute_plan_sha256(plan)
        receipt = {
            "schema": "calendar-desk-approval/1",
            "plan_id": plan["plan_id"],
            "plan_sha256": plan["plan_sha256"],
            "approved": True,
            "approved_by": "operator@test",
        }
        with self.assertRaises(CalendarWriteError) as ctx:
            apply_calendar_write(
                self.ws,
                plan=plan,
                approval_receipt=receipt,
                caldav_client=self.client,
                lease_id="lease-reg-1",
                lock_root=self.guard_dir,
            )
        self.assertIn(ctx.exception.code, ("WRITE_VALIDATION_ERROR", "RECOVERY_REQUIRED"))

    def test_apply_same_uid_different_account_or_calendar_not_evicted(self) -> None:
        evidence_dir = self.ws / "memory" / "evidence" / "calendar"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        reg_file = evidence_dir / "calendars.json"
        existing_reg = {
            "schema": "calendar-desk-register/1",
            "events": [
                {
                    "uid": "shared-uid-100",
                    "account": "secondary-account",
                    "calendar_alias": "work",
                    "resource_url": "/dav/calendars/sec/work/shared-uid-100.ics",
                    "etag": '"etag-sec-1"',
                    "status": "active",
                }
            ],
        }
        reg_file.write_text(json.dumps(existing_reg, indent=2), encoding="utf-8")

        plan = {
            "schema": "calendar-desk-plan/1",
            "plan_id": "plan-shared-uid-1",
            "action": "create-event",
            "account": "primary-account",
            "calendar_alias": "primary",
            "collection_url": "/dav/calendars/user/primary/",
            "resource_url": "/dav/calendars/user/primary/shared-uid-100.ics",
            "uid": "shared-uid-100",
            "precondition": {"if_none_match_star": True, "if_match_etag": None},
            "staged_ics": SAMPLE_VEVENT_ICS,
            "canonical_ics_sha256": hashlib.sha256(SAMPLE_VEVENT_ICS.encode("utf-8")).hexdigest(),
            "diff_summary": "Test shared UID",
        }
        plan["plan_sha256"] = compute_plan_sha256(plan)
        receipt = {
            "schema": "calendar-desk-approval/1",
            "plan_id": plan["plan_id"],
            "plan_sha256": plan["plan_sha256"],
            "approved": True,
            "approved_by": "operator@test",
        }
        apply_calendar_write(
            self.ws,
            plan=plan,
            approval_receipt=receipt,
            caldav_client=self.client,
            lease_id="lease-reg-1",
            lock_root=self.guard_dir,
        )
        updated_reg = json.loads(reg_file.read_text(encoding="utf-8"))
        events = updated_reg.get("events", [])
        self.assertEqual(len(events), 2)
        accounts = {e["account"] for e in events}
        self.assertIn("secondary-account", accounts)
        self.assertIn("primary-account", accounts)


class TestETagReadback(unittest.TestCase):
    """Test #26: ETag readback occurs when PUT does not return an ETag header."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.ws = Path(self.temp_dir.name)
        make_backend_config_file(self.ws, write_policy="dry_run_and_approval")
        self.guard_dir = make_mock_guard_dir(self.ws, lock_state="Active", owner_lease="lease-etag-1")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_apply_put_without_etag_performs_readback(self) -> None:
        client = MockCalDAVClient(omit_etag_on_put=True)
        plan = {
            "schema": "calendar-desk-plan/1",
            "plan_id": "plan-etag-readback-1",
            "action": "create-event",
            "account": "primary-account",
            "calendar_alias": "primary",
            "collection_url": "/dav/calendars/user/primary/",
            "resource_url": "/dav/calendars/user/primary/evt-readback-1.ics",
            "uid": "evt-readback-1",
            "precondition": {"if_none_match_star": True, "if_match_etag": None},
            "staged_ics": SAMPLE_VEVENT_ICS,
            "canonical_ics_sha256": hashlib.sha256(SAMPLE_VEVENT_ICS.encode("utf-8")).hexdigest(),
            "diff_summary": "Test ETag readback",
        }
        plan["plan_sha256"] = compute_plan_sha256(plan)
        receipt = {
            "schema": "calendar-desk-approval/1",
            "plan_id": plan["plan_id"],
            "plan_sha256": plan["plan_sha256"],
            "approved": True,
            "approved_by": "operator@test",
        }
        result = apply_calendar_write(
            self.ws,
            plan=plan,
            approval_receipt=receipt,
            caldav_client=client,
            lease_id="lease-etag-1",
            lock_root=self.guard_dir,
        )
        self.assertIsNotNone(result.get("new_etag"))
        self.assertTrue(result["new_etag"].startswith('"etag-'))

        journal_file = self.ws / "data" / "calendar-desk" / "journal" / f"{plan['plan_id']}.json"
        jdata = json.loads(journal_file.read_text(encoding="utf-8"))
        self.assertEqual(jdata.get("new_etag"), result["new_etag"])

        reg_file = self.ws / "memory" / "evidence" / "calendar" / "calendars.json"
        reg_data = json.loads(reg_file.read_text(encoding="utf-8"))
        matching = [e for e in reg_data.get("events", []) if e.get("uid") == "evt-readback-1"]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0].get("etag"), result["new_etag"])


if __name__ == "__main__":
    unittest.main()
