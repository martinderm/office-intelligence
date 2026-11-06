"""Two-phase write engine, workspace-lock guard, ETag preconditions, and recovery journal.

Implements CD-3 contracts:
- CalendarWriteError with machine-readable codes
- Workspace-lock guard integration (_trusted_lock_root, _load_guard, require_owned_workspace_lock)
- Canonical JSON SHA-256 calculation (canonical_json_sha256, compute_plan_sha256)
- Lock-free dry-run planning (plan_calendar_write) for create/update/cancel/delete
- Approved execution (apply_calendar_write) under owned workspace-lock
- Precondition enforcement (If-None-Match: *, If-Match: "<etag>") and 412 mapping
- Journal state machine (in_progress -> completed / aborted_precondition)
- Local evidence publication (calendars.json register and raw ICS)
- Workspace inspection and recovery reconciliation (inspect_workspace, recover_workspace)
- Phase-2 seam respond_invitation (NOT_IMPLEMENTED_PHASE_2)
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from core.backend_config import load_backend_config, resolve_calendar_target
from core.caldav_client import CalDAVProtocolError
from core.ical_codec import build_vevent_ics, resolve_tzid, update_vevent_ics

ENV_LOCK_ROOT = "CALENDAR_DESK_WORKSPACE_LOCK_ROOT"


class CalendarWriteError(RuntimeError):
    """Raised when calendar planning, lock verification, or execution fails."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _trusted_lock_root(explicit: str | Path | None = None) -> Path:
    """Resolve the trusted root directory of the workspace-lock skill."""
    if explicit is not None:
        return Path(explicit).resolve()

    env_root = os.environ.get("WORKSPACE_LOCK_ROOT") or os.environ.get(ENV_LOCK_ROOT)
    if env_root:
        return Path(env_root).resolve()

    # Look for sibling or relative workspace-lock directory
    here = Path(__file__).resolve()
    for parent in here.parents:
        cand1 = parent / "skills" / "workspace-lock"
        if (cand1 / "scripts" / "workspace_lock_guard.py").is_file():
            return cand1
        cand2 = parent / "workspace-lock"
        if (cand2 / "scripts" / "workspace_lock_guard.py").is_file():
            return cand2

    # Default fallback to sibling skills/workspace-lock
    return here.parents[3] / "workspace-lock"


def _load_guard(trusted_root: str | Path) -> Any:
    """Load scripts/workspace_lock_guard.py from trusted_root without trusting pre-existing sys.modules."""
    root_path = Path(trusted_root).resolve()
    guard_script = root_path / "scripts" / "workspace_lock_guard.py"
    if not guard_script.is_file():
        raise CalendarWriteError(
            "WORKSPACE_LOCK_UNAVAILABLE",
            f"workspace_lock_guard.py not found under trusted root '{root_path}'",
        )

    spec = importlib.util.spec_from_file_location("workspace_lock_guard", str(guard_script))
    if spec is None or spec.loader is None:
        raise CalendarWriteError(
            "WORKSPACE_LOCK_UNAVAILABLE",
            f"Failed to create module spec for '{guard_script}'",
        )

    module = importlib.util.module_from_spec(spec)
    # Register in sys.modules so dataclasses or internal imports work cleanly
    sys.modules["workspace_lock_guard"] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise CalendarWriteError(
            "WORKSPACE_LOCK_UNAVAILABLE",
            f"Failed to load workspace_lock_guard from '{guard_script}': {exc}",
        ) from exc

    return module


def require_owned_workspace_lock(
    workspace_root: str | Path,
    *,
    lease_id: str | None = None,
    conversation_id: str | None = None,
    lock_root: str | Path | None = None,
) -> Any:
    """Assert an active invocation-owned workspace lock via workspace_lock_guard."""
    trusted_root = _trusted_lock_root(lock_root)
    guard = _load_guard(trusted_root)

    try:
        receipt = guard.require_workspace_lock(
            workspace_root,
            lease_id=lease_id,
            conversation_id=conversation_id,
            allow_legacy=False,
        )
        return receipt
    except Exception as exc:
        if isinstance(exc, CalendarWriteError):
            raise
        raise CalendarWriteError(
            "WORKSPACE_LOCK_REQUIRED",
            f"Active invocation-owned workspace lock required: {exc}",
        ) from exc


def canonical_json_sha256(payload: Any) -> str:
    """Calculate deterministic SHA-256 hex digest over sorted compact JSON."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def compute_plan_sha256(plan: Mapping[str, Any]) -> str:
    """Compute plan_sha256 over canonical plan dictionary excluding plan_sha256 and approval_template."""
    cleaned = {k: v for k, v in plan.items() if k not in ("plan_sha256", "approval_template")}
    return canonical_json_sha256(cleaned)


def _check_no_in_progress_journal(ws_path: Path) -> None:
    """Verify that no interrupted write journal exists in data/calendar-desk/journal/."""
    journal_dir = ws_path / "data" / "calendar-desk" / "journal"
    if not journal_dir.is_dir():
        return
    for jf in journal_dir.glob("*.json"):
        try:
            jdata = json.loads(jf.read_text(encoding="utf-8"))
            if isinstance(jdata, dict) and jdata.get("status") == "in_progress":
                raise CalendarWriteError(
                    "RECOVERY_REQUIRED",
                    f"Interrupted write journal exists at '{jf.name}'. Workspace recovery required.",
                )
        except (json.JSONDecodeError, OSError):
            pass


def _normalize_spec_endpoint(dt_val: Any, tz_str: str, default_tz: str = "Europe/Vienna") -> dict[str, Any]:
    """Convert string or dict datetime specification into standardized calendar-record endpoint dict."""
    if isinstance(dt_val, dict):
        return dt_val
    if not dt_val:
        raise CalendarWriteError("WRITE_VALIDATION_ERROR", "Missing datetime value in event_spec")

    clean_val = str(dt_val).strip()

    # Check for all-day date: YYYY-MM-DD (10 chars, no 'T')
    if len(clean_val) == 10 and "T" not in clean_val and clean_val.count("-") == 2:
        return {
            "utc": None,
            "local": None,
            "tzid": None,
            "all_day": True,
            "date": clean_val,
        }

    # Timezone-aware or naive datetime parsing
    tz = resolve_tzid(tz_str or default_tz, default_tz=default_tz)
    iso_str = clean_val.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso_str)
    except ValueError:
        try:
            dt = datetime.strptime(clean_val.replace("-", "").replace(":", ""), "%Y%m%dT%H%M%S")
        except ValueError as exc:
            raise CalendarWriteError(
                "WRITE_VALIDATION_ERROR", f"Invalid datetime string '{clean_val}'"
            ) from exc

    if dt.tzinfo is None:
        dt_local = dt.replace(tzinfo=tz)
        dt_utc = dt_local.astimezone(timezone.utc)
    else:
        dt_utc = dt.astimezone(timezone.utc)
        dt_local = dt.astimezone(tz)

    return {
        "utc": dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "local": dt_local.isoformat(),
        "tzid": tz.key,
        "all_day": False,
        "date": dt_local.strftime("%Y-%m-%d"),
    }


def plan_calendar_write(
    workspace_root: str | Path,
    *,
    action: str,
    account: str,
    calendar_alias: str,
    collection_url: str,
    write_policy: str,
    uid: str | None = None,
    event_spec: dict[str, Any] | None = None,
    updates: dict[str, Any] | None = None,
    expected_etag: str | None = None,
    existing_raw_ics: str | None = None,
    resource_url: str | None = None,
    persist: bool = True,
    caldav_client: Any = None,
) -> dict[str, Any]:
    """Execute dry-run planning without requiring a workspace lock."""
    ws_path = Path(workspace_root).resolve()

    if write_policy != "dry_run_and_approval":
        raise CalendarWriteError(
            "CALENDAR_WRITE_FORBIDDEN",
            f"Write rejected: calendar policy is '{write_policy}', must be 'dry_run_and_approval'",
        )

    _check_no_in_progress_journal(ws_path)

    allowed_actions = {"create-event", "update-event", "cancel-event", "delete-event"}
    if action not in allowed_actions:
        raise CalendarWriteError(
            "WRITE_VALIDATION_ERROR",
            f"Unsupported action '{action}'. Must be one of {sorted(allowed_actions)}",
        )

    if not uid:
        if action == "create-event":
            uid = f"evt-{uuid.uuid4().hex[:16]}"
        else:
            raise CalendarWriteError("WRITE_VALIDATION_ERROR", f"UID is required for '{action}'")

    if not resource_url:
        col = collection_url.rstrip("/") + "/"
        resource_url = f"{col}{uid}.ics"

    staged_ics: str | None = None
    precondition: dict[str, Any] = {}
    diff_summary = ""

    if action == "create-event":
        if not event_spec or not isinstance(event_spec, dict):
            raise CalendarWriteError("WRITE_VALIDATION_ERROR", "event_spec is required for create-event")

        tz_name = event_spec.get("timezone") or "Europe/Vienna"
        start_val = event_spec.get("start") or event_spec.get("dtstart")
        if not start_val:
            raise CalendarWriteError(
                "WRITE_VALIDATION_ERROR", "event_spec must specify start or dtstart"
            )
        start_endpoint = _normalize_spec_endpoint(start_val, tz_name)

        end_val = event_spec.get("end") or event_spec.get("dtend")
        if end_val:
            end_endpoint = _normalize_spec_endpoint(end_val, tz_name)
        else:
            if start_endpoint["all_day"]:
                d_start = datetime.strptime(start_endpoint["date"], "%Y-%m-%d").date()
                d_end = d_start + timedelta(days=1)
                end_endpoint = {
                    "utc": None,
                    "local": None,
                    "tzid": None,
                    "all_day": True,
                    "date": d_end.strftime("%Y-%m-%d"),
                }
            else:
                dt_s = datetime.fromisoformat(start_endpoint["utc"].replace("Z", "+00:00"))
                dt_e = dt_s + timedelta(hours=1)
                tz = resolve_tzid(tz_name)
                dt_e_local = dt_e.astimezone(tz)
                end_endpoint = {
                    "utc": dt_e.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "local": dt_e_local.isoformat(),
                    "tzid": tz.key,
                    "all_day": False,
                    "date": dt_e_local.strftime("%Y-%m-%d"),
                }

        record = {
            "schema": "calendar-record/1",
            "uid": uid,
            "summary": event_spec.get("summary", ""),
            "description": event_spec.get("description", ""),
            "location": event_spec.get("location", ""),
            "status": event_spec.get("status", "CONFIRMED"),
            "transp": event_spec.get("transp", "OPAQUE"),
            "start": start_endpoint,
            "end": end_endpoint,
            "rrule": event_spec.get("rrule"),
        }
        staged_ics = build_vevent_ics(record)
        precondition = {"if_none_match_star": True, "if_match_etag": None}
        diff_summary = f"Create new event '{record.get('summary', '')}' (UID: {uid})"

    elif action in ("update-event", "cancel-event"):
        if not expected_etag or not expected_etag.strip():
            raise CalendarWriteError("WRITE_VALIDATION_ERROR", f"expected_etag is required for '{action}'")

        if not existing_raw_ics:
            if caldav_client:
                fetched = caldav_client.get_event(resource_url)
                existing_raw_ics = fetched["raw_ics"]
            else:
                raise CalendarWriteError(
                    "WRITE_VALIDATION_ERROR",
                    f"existing_raw_ics is required for '{action}' when caldav_client is omitted",
                )

        if action == "cancel-event":
            staged_ics = update_vevent_ics(
                existing_raw_ics, {"status": "CANCELLED"}, increment_sequence=True
            )
            diff_summary = f"Cancel event (UID: {uid}, ETag: {expected_etag})"
        else:
            update_dict = dict(updates or {})
            staged_ics = update_vevent_ics(
                existing_raw_ics, update_dict, increment_sequence=True
            )
            diff_summary = f"Update event (UID: {uid}, ETag: {expected_etag})"

        precondition = {"if_none_match_star": False, "if_match_etag": expected_etag}

    elif action == "delete-event":
        if not expected_etag or not expected_etag.strip():
            raise CalendarWriteError("WRITE_VALIDATION_ERROR", "expected_etag is required for delete-event")
        staged_ics = None
        precondition = {"if_none_match_star": False, "if_match_etag": expected_etag}
        diff_summary = f"Delete event (UID: {uid}, ETag: {expected_etag})"

    plan_id = f"plan-{uuid.uuid4().hex[:12]}"
    canonical_ics_sha256 = (
        hashlib.sha256(staged_ics.encode("utf-8")).hexdigest() if staged_ics else None
    )

    plan = {
        "schema": "calendar-desk-plan/1",
        "plan_id": plan_id,
        "action": action,
        "account": account,
        "calendar_alias": calendar_alias,
        "collection_url": collection_url,
        "resource_url": resource_url,
        "uid": uid,
        "precondition": precondition,
        "staged_ics": staged_ics,
        "canonical_ics_sha256": canonical_ics_sha256,
        "diff_summary": diff_summary,
    }

    plan_sha256 = compute_plan_sha256(plan)
    plan["plan_sha256"] = plan_sha256
    plan["approval_template"] = {
        "schema": "calendar-desk-approval/1",
        "plan_id": plan_id,
        "plan_sha256": plan_sha256,
        "approved": False,
        "approved_by": None,
        "approved_at": None,
    }

    if persist:
        proposals_dir = ws_path / "data" / "calendar-desk" / "proposals"
        proposals_dir.mkdir(parents=True, exist_ok=True)
        plan_file = proposals_dir / f"{plan_id}.json"
        tmp_file = proposals_dir / f".tmp-{plan_id}.json"
        tmp_file.write_text(json.dumps(plan, indent=2), encoding="utf-8")
        os.replace(str(tmp_file), str(plan_file))

    return plan


def apply_calendar_write(
    workspace_root: str | Path,
    *,
    plan: dict[str, Any],
    approval_receipt: dict[str, Any],
    caldav_client: Any,
    lease_id: str | None = None,
    conversation_id: str | None = None,
    lock_root: str | Path | None = None,
) -> dict[str, Any]:
    """Execute approved calendar write under active workspace lock."""
    ws_path = Path(workspace_root).resolve()

    # 1. Require active owned workspace lock
    require_owned_workspace_lock(
        ws_path,
        lease_id=lease_id,
        conversation_id=conversation_id,
        lock_root=lock_root,
    )

    # 2. Check no existing in_progress journal
    _check_no_in_progress_journal(ws_path)

    # 3. Validate approval receipt
    if approval_receipt.get("schema") != "calendar-desk-approval/1":
        raise CalendarWriteError(
            "APPROVAL_RECEIPT_INVALID",
            f"Invalid approval receipt schema: {approval_receipt.get('schema')!r}",
        )
    if approval_receipt.get("approved") is not True:
        raise CalendarWriteError("APPROVAL_RECEIPT_INVALID", "Approval receipt approved flag must be True")

    approved_by = approval_receipt.get("approved_by")
    if not approved_by or not isinstance(approved_by, str) or not approved_by.strip():
        raise CalendarWriteError(
            "APPROVAL_RECEIPT_INVALID",
            "Approval receipt approved_by must be a non-empty string",
        )

    if approval_receipt.get("plan_id") != plan.get("plan_id"):
        raise CalendarWriteError(
            "PLAN_HASH_MISMATCH",
            f"Receipt plan_id '{approval_receipt.get('plan_id')}' does not match plan '{plan.get('plan_id')}'",
        )

    expected_hash = compute_plan_sha256(plan)
    if plan.get("plan_sha256") != expected_hash:
        raise CalendarWriteError(
            "PLAN_HASH_MISMATCH",
            f"Plan hash tamper detected: stored {plan.get('plan_sha256')} vs computed {expected_hash}",
        )
    if approval_receipt.get("plan_sha256") != plan.get("plan_sha256"):
        raise CalendarWriteError(
            "PLAN_HASH_MISMATCH",
            f"Receipt plan_sha256 '{approval_receipt.get('plan_sha256')}' does not match plan '{plan.get('plan_sha256')}'",
        )

    # 4. Stage journal file (in_progress)
    journal_dir = ws_path / "data" / "calendar-desk" / "journal"
    journal_dir.mkdir(parents=True, exist_ok=True)
    journal_file = journal_dir / f"{plan['plan_id']}.json"

    journal_data = {
        "status": "in_progress",
        "plan_id": plan["plan_id"],
        "plan_sha256": plan["plan_sha256"],
        "action": plan["action"],
        "account": plan["account"],
        "calendar_alias": plan["calendar_alias"],
        "uid": plan["uid"],
        "resource_url": plan["resource_url"],
        "precondition": plan["precondition"],
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    journal_file.write_text(json.dumps(journal_data, indent=2), encoding="utf-8")

    # 5. Remote CalDAV execution
    action = plan["action"]
    resource_url = plan["resource_url"]
    precondition = plan["precondition"]
    staged_ics = plan.get("staged_ics")
    new_etag: str | None = None

    try:
        if action in ("create-event", "update-event", "cancel-event"):
            if not staged_ics:
                raise CalendarWriteError("WRITE_VALIDATION_ERROR", f"Missing staged_ics for {action}")
            resp = caldav_client.put_event_ics(
                resource_url,
                staged_ics,
                if_none_match_star=precondition.get("if_none_match_star", False),
                if_match_etag=precondition.get("if_match_etag"),
            )
            new_etag = resp.get("etag")
        elif action == "delete-event":
            resp = caldav_client.delete_event_resource(
                resource_url,
                if_match_etag=precondition.get("if_match_etag"),
            )
            new_etag = None
    except CalDAVProtocolError as exc:
        if exc.code == "CALDAV_ETAG_CONFLICT" or getattr(exc, "status_code", None) == 412:
            journal_data["status"] = "aborted_precondition"
            journal_data["error"] = str(exc)
            journal_file.write_text(json.dumps(journal_data, indent=2), encoding="utf-8")
            raise CalendarWriteError(
                "CALDAV_ETAG_CONFLICT",
                f"CalDAV ETag precondition failed (HTTP 412): {exc}",
            ) from exc

        journal_data["status"] = "aborted_error"
        journal_data["error"] = str(exc)
        journal_file.write_text(json.dumps(journal_data, indent=2), encoding="utf-8")
        raise

    # 6. Re-verify lock ownership before publishing local evidence
    require_owned_workspace_lock(
        ws_path,
        lease_id=lease_id,
        conversation_id=conversation_id,
        lock_root=lock_root,
    )

    # 7. Update local evidence register & raw ICS
    evidence_dir = ws_path / "memory" / "evidence" / "calendar"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    reg_file = evidence_dir / "calendars.json"

    reg_data: dict[str, Any] = {"schema": "calendar-desk-register/1", "events": []}
    if reg_file.is_file():
        try:
            reg_data = json.loads(reg_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            reg_data = {"schema": "calendar-desk-register/1", "events": []}

    events_list: list[dict[str, Any]] = reg_data.get("events", [])
    raw_dir = evidence_dir / "raw" / plan["account"] / plan["calendar_alias"]
    raw_file = raw_dir / f"{plan['uid']}.ics"

    if action == "delete-event":
        if raw_file.exists():
            try:
                raw_file.unlink()
            except OSError:
                pass
        events_list = [e for e in events_list if e.get("uid") != plan["uid"]]
    else:
        raw_dir.mkdir(parents=True, exist_ok=True)
        if staged_ics:
            raw_file.write_text(staged_ics, encoding="utf-8")

        events_list = [e for e in events_list if e.get("uid") != plan["uid"]]
        events_list.append({
            "uid": plan["uid"],
            "account": plan["account"],
            "calendar_alias": plan["calendar_alias"],
            "resource_url": plan["resource_url"],
            "etag": new_etag,
            "status": "cancelled" if action == "cancel-event" else "active",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })

    reg_data["events"] = events_list
    reg_data["updated_at"] = datetime.now(timezone.utc).isoformat()
    reg_file.write_text(json.dumps(reg_data, indent=2), encoding="utf-8")

    # 8. Mark journal completed
    journal_data["status"] = "completed"
    journal_data["completed_at"] = datetime.now(timezone.utc).isoformat()
    journal_data["new_etag"] = new_etag
    journal_file.write_text(json.dumps(journal_data, indent=2), encoding="utf-8")

    register_rel = str(reg_file.relative_to(ws_path)).replace("\\", "/")
    return {
        "status": "applied",
        "plan_id": plan["plan_id"],
        "plan_sha256": plan["plan_sha256"],
        "action": plan["action"],
        "uid": plan["uid"],
        "new_etag": new_etag,
        "register_path": register_rel,
    }


def inspect_workspace(workspace_root: str | Path) -> dict[str, Any]:
    """Inspect proposals, journals, and local evidence register."""
    ws_path = Path(workspace_root).resolve()

    proposals_dir = ws_path / "data" / "calendar-desk" / "proposals"
    proposals_count = len(list(proposals_dir.glob("*.json"))) if proposals_dir.is_dir() else 0

    journal_dir = ws_path / "data" / "calendar-desk" / "journal"
    journals_by_status: dict[str, int] = {}
    in_progress_plans: list[dict[str, Any]] = []

    if journal_dir.is_dir():
        for jf in journal_dir.glob("*.json"):
            try:
                jdata = json.loads(jf.read_text(encoding="utf-8"))
                if isinstance(jdata, dict):
                    status = jdata.get("status", "unknown")
                    journals_by_status[status] = journals_by_status.get(status, 0) + 1
                    if status == "in_progress":
                        in_progress_plans.append(jdata)
            except (json.JSONDecodeError, OSError):
                pass

    reg_file = ws_path / "memory" / "evidence" / "calendar" / "calendars.json"
    register_stats: dict[str, Any] = {"present": reg_file.is_file()}
    if reg_file.is_file():
        try:
            reg_data = json.loads(reg_file.read_text(encoding="utf-8"))
            register_stats["event_count"] = len(reg_data.get("events", []))
            register_stats["updated_at"] = reg_data.get("updated_at")
        except (json.JSONDecodeError, OSError):
            pass

    return {
        "proposals_count": proposals_count,
        "journals_by_status": journals_by_status,
        "in_progress_plans": in_progress_plans,
        "active_in_progress_count": len(in_progress_plans),
        "register": register_stats,
    }


def recover_workspace(
    workspace_root: str | Path,
    *,
    lease_id: str | None = None,
    conversation_id: str | None = None,
    lock_root: str | Path | None = None,
    caldav_client: Any = None,
) -> dict[str, Any]:
    """Reconcile in_progress journals under active workspace lock."""
    ws_path = Path(workspace_root).resolve()
    require_owned_workspace_lock(
        ws_path,
        lease_id=lease_id,
        conversation_id=conversation_id,
        lock_root=lock_root,
    )

    journal_dir = ws_path / "data" / "calendar-desk" / "journal"
    recovered_count = 0

    if journal_dir.is_dir():
        for jf in journal_dir.glob("*.json"):
            try:
                jdata = json.loads(jf.read_text(encoding="utf-8"))
                if isinstance(jdata, dict) and jdata.get("status") == "in_progress":
                    new_status = "recovered"
                    if caldav_client:
                        res_url = jdata.get("resource_url")
                        try:
                            if res_url:
                                caldav_client.get_event(res_url)
                                new_status = "recovered_applied"
                        except Exception:
                            new_status = "recovered_aborted"

                    jdata["status"] = new_status
                    jdata["recovered_at"] = datetime.now(timezone.utc).isoformat()
                    jf.write_text(json.dumps(jdata, indent=2), encoding="utf-8")
                    recovered_count += 1
            except (json.JSONDecodeError, OSError):
                pass

    return {
        "status": "recovered",
        "recovered_count": recovered_count,
    }


def respond_invitation(*args: Any, **kwargs: Any) -> Any:
    """Phase-2 seam for iTIP PARTSTAT scheduling responses."""
    raise CalendarWriteError(
        "NOT_IMPLEMENTED_PHASE_2",
        "iTIP PARTSTAT scheduling responses (ACCEPTED/DECLINED/TENTATIVE) are reserved for Phase 2 (CD-5).",
    )
