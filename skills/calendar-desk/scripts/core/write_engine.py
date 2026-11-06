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
import re
import sys
import tempfile
import urllib.parse
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


def _atomic_write_text(file_path: Path, content: str, encoding: str = "utf-8") -> None:
    """Safely write content to file_path atomically via NamedTemporaryFile + os.replace."""
    parent = file_path.parent
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding=encoding,
        dir=str(parent),
        prefix=f".tmp-{file_path.name}-",
        delete=False,
    ) as tmp:
        tmp.write(content)
        tmp.flush()
        tmp_name = tmp.name
    os.replace(tmp_name, str(file_path))


def _validate_safe_identifier(val: Any, name: str) -> None:
    """Validate identifier against path traversal and dangerous characters."""
    if not isinstance(val, str) or not val.strip():
        raise CalendarWriteError("WRITE_VALIDATION_ERROR", f"{name} must be a non-empty string")
    clean = val.strip()
    if (
        ".." in clean
        or "/" in clean
        or "\\" in clean
        or clean.startswith(".")
        or "\x00" in clean
    ):
        raise CalendarWriteError(
            "WRITE_VALIDATION_ERROR",
            f"Invalid {name} '{val}': path traversal or illegal characters detected",
        )


def _validate_resource_url_within_collection(resource_url: str, collection_url: str) -> None:
    """Ensure resource_url is properly contained within collection_url."""
    res_parsed = urllib.parse.urlsplit(resource_url)
    col_parsed = urllib.parse.urlsplit(collection_url)

    if col_parsed.scheme and res_parsed.scheme:
        if col_parsed.scheme.lower() != res_parsed.scheme.lower():
            raise CalendarWriteError(
                "WRITE_VALIDATION_ERROR",
                f"resource_url scheme '{res_parsed.scheme}' does not match collection scheme '{col_parsed.scheme}'",
            )
    if col_parsed.netloc and res_parsed.netloc:
        if col_parsed.netloc.lower() != res_parsed.netloc.lower():
            raise CalendarWriteError(
                "WRITE_VALIDATION_ERROR",
                f"resource_url host '{res_parsed.netloc}' does not match collection host '{col_parsed.netloc}'",
            )

    col_path = col_parsed.path.rstrip("/") + "/"
    res_path = res_parsed.path
    if not res_path.startswith(col_path):
        raise CalendarWriteError(
            "WRITE_VALIDATION_ERROR",
            f"resource_url path '{res_path}' is outside collection path '{col_path}'",
        )

    remainder = res_path[len(col_path):]
    if "/" in remainder or "\\" in remainder or ".." in remainder or not remainder:
        raise CalendarWriteError(
            "WRITE_VALIDATION_ERROR",
            f"resource_url path contains traversal or subdirectories: '{remainder}'",
        )


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
            content = jf.read_text(encoding="utf-8")
            jdata = json.loads(content)
            if not isinstance(jdata, dict):
                raise CalendarWriteError(
                    "RECOVERY_REQUIRED",
                    f"Corrupted write journal at '{jf.name}'. Workspace recovery required.",
                )
            status = jdata.get("status")
            if status in ("in_progress", "unknown_remote_outcome", "recovery_required"):
                raise CalendarWriteError(
                    "RECOVERY_REQUIRED",
                    f"Interrupted write journal exists at '{jf.name}' (status: '{status}'). Workspace recovery required.",
                )
        except (json.JSONDecodeError, OSError) as exc:
            raise CalendarWriteError(
                "RECOVERY_REQUIRED",
                f"Corrupt or unreadable write journal at '{jf.name}': {exc}. Workspace recovery required.",
            ) from exc


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

    _validate_safe_identifier(account, "account")
    _validate_safe_identifier(calendar_alias, "calendar_alias")
    if uid is not None:
        _validate_safe_identifier(uid, "uid")

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
        safe_seg = urllib.parse.quote(uid, safe="@_.-") + ".ics"
        col = collection_url.rstrip("/") + "/"
        resource_url = f"{col}{safe_seg}"
    else:
        _validate_resource_url_within_collection(resource_url, collection_url)

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
            if event_spec and isinstance(event_spec, dict):
                tz_name = event_spec.get("timezone") or "Europe/Vienna"
                for k in ("summary", "description", "location", "status", "transp", "classification"):
                    if k in event_spec and k not in update_dict:
                        update_dict[k] = event_spec[k]
                st_val = event_spec.get("start") or event_spec.get("dtstart")
                if st_val and "start" not in update_dict:
                    update_dict["start"] = _normalize_spec_endpoint(st_val, tz_name)
                end_val = event_spec.get("end") or event_spec.get("dtend")
                if end_val and "end" not in update_dict:
                    update_dict["end"] = _normalize_spec_endpoint(end_val, tz_name)

            if "start" in update_dict and not isinstance(update_dict["start"], dict):
                update_dict["start"] = _normalize_spec_endpoint(update_dict["start"], "Europe/Vienna")
            if "end" in update_dict and not isinstance(update_dict["end"], dict):
                update_dict["end"] = _normalize_spec_endpoint(update_dict["end"], "Europe/Vienna")

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
        "pre_state": {
            "etag": expected_etag,
            "raw_ics": existing_raw_ics,
        },
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
        plan_file = proposals_dir / f"{plan_id}.json"
        _atomic_write_text(plan_file, json.dumps(plan, indent=2))

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

    # 3. Validate plan identifiers
    _validate_safe_identifier(plan.get("plan_id"), "plan_id")
    _validate_safe_identifier(plan.get("uid"), "uid")
    _validate_safe_identifier(plan.get("account"), "account")
    _validate_safe_identifier(plan.get("calendar_alias"), "calendar_alias")

    # 4. Re-verify write policy from backend config
    cfg_file = ws_path / ".agents" / "calendar-desk-backend.json"
    if cfg_file.is_file():
        cfg = load_backend_config(cfg_file)
        accounts = cfg.get("accounts", {})
        acct_meta = accounts.get(plan.get("account", ""))
        if acct_meta:
            cals = acct_meta.get("calendars", {})
            cal_meta = cals.get(plan.get("calendar_alias", ""))
            if cal_meta:
                policy = cal_meta.get("write_policy", "read_only")
                if policy != "dry_run_and_approval":
                    raise CalendarWriteError(
                        "CALENDAR_WRITE_FORBIDDEN",
                        f"Write rejected: calendar policy is '{policy}', must be 'dry_run_and_approval'",
                    )
    if plan.get("write_policy") and plan["write_policy"] != "dry_run_and_approval":
        raise CalendarWriteError(
            "CALENDAR_WRITE_FORBIDDEN",
            f"Write rejected: plan write_policy is '{plan['write_policy']}', must be 'dry_run_and_approval'",
        )

    # 5. Validate resource_url containment
    if plan.get("resource_url") and plan.get("collection_url"):
        _validate_resource_url_within_collection(plan["resource_url"], plan["collection_url"])

    # 6. Validate approval receipt
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

    # 7. Check for duplicate apply / existing journal
    journal_dir = ws_path / "data" / "calendar-desk" / "journal"
    journal_dir.mkdir(parents=True, exist_ok=True)
    journal_file = journal_dir / f"{plan['plan_id']}.json"

    if journal_file.exists():
        try:
            existing_j = json.loads(journal_file.read_text(encoding="utf-8"))
            existing_status = existing_j.get("status") if isinstance(existing_j, dict) else "corrupted"
        except Exception:
            existing_status = "corrupted"
        if existing_status in ("applied", "completed", "in_progress", "unknown_remote_outcome", "recovery_required", "corrupted"):
            raise CalendarWriteError(
                "WRITE_VALIDATION_ERROR",
                f"Journal already exists for plan '{plan['plan_id']}' with status '{existing_status}'. Duplicate apply forbidden.",
            )

    # 8. Stage journal file (in_progress)
    pre_state = plan.get("pre_state")
    if not isinstance(pre_state, dict):
        pre_state = {
            "etag": plan.get("precondition", {}).get("if_match_etag"),
            "raw_ics": plan.get("existing_raw_ics"),
        }

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
        "pre_state": pre_state,
        "staged_ics": plan.get("staged_ics"),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_write_text(journal_file, json.dumps(journal_data, indent=2))

    # 9. Remote CalDAV execution
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
            if new_etag is None and caldav_client is not None:
                try:
                    readback = caldav_client.get_event(resource_url)
                    new_etag = readback.get("etag")
                except Exception:
                    pass
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
            _atomic_write_text(journal_file, json.dumps(journal_data, indent=2))
            raise CalendarWriteError(
                "CALDAV_ETAG_CONFLICT",
                f"CalDAV ETag precondition failed (HTTP 412): {exc}",
            ) from exc
        elif exc.code == "CALDAV_NETWORK_ERROR" or "network" in str(exc).lower():
            journal_data["status"] = "unknown_remote_outcome"
            journal_data["error"] = str(exc)
            _atomic_write_text(journal_file, json.dumps(journal_data, indent=2))
            raise CalendarWriteError(
                "RECOVERY_REQUIRED",
                f"Network error during write to '{resource_url}': {exc}. Status unknown, recovery required.",
            ) from exc
        else:
            journal_data["status"] = "aborted_error"
            journal_data["error"] = str(exc)
            _atomic_write_text(journal_file, json.dumps(journal_data, indent=2))
            raise
    except (TimeoutError, OSError) as exc:
        journal_data["status"] = "unknown_remote_outcome"
        journal_data["error"] = str(exc)
        _atomic_write_text(journal_file, json.dumps(journal_data, indent=2))
        raise CalendarWriteError(
            "RECOVERY_REQUIRED",
            f"Network error/timeout during write to '{resource_url}': {exc}. Status unknown, recovery required.",
        ) from exc

    # 10. Re-verify lock ownership before publishing local evidence
    require_owned_workspace_lock(
        ws_path,
        lease_id=lease_id,
        conversation_id=conversation_id,
        lock_root=lock_root,
    )

    # 11. Update local evidence register & raw ICS
    evidence_dir = ws_path / "memory" / "evidence" / "calendar"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    reg_file = evidence_dir / "calendars.json"

    reg_data: dict[str, Any] = {"schema": "calendar-desk-register/1", "events": []}
    if reg_file.is_file():
        try:
            reg_data = json.loads(reg_file.read_text(encoding="utf-8"))
            if not isinstance(reg_data, dict) or "events" not in reg_data:
                raise ValueError("Register corrupt")
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            raise CalendarWriteError(
                "RECOVERY_REQUIRED",
                f"Damaged evidence register at '{reg_file}': {exc}",
            ) from exc

    events_list: list[dict[str, Any]] = reg_data.get("events", [])
    raw_dir = evidence_dir / "raw" / plan["account"] / plan["calendar_alias"]
    safe_uid = re.sub(r"[^A-Za-z0-9_@.-]", "_", plan["uid"])
    raw_file = (raw_dir / f"{safe_uid}.ics").resolve()
    if not raw_file.is_relative_to(evidence_dir.resolve()):
        raise CalendarWriteError("WRITE_VALIDATION_ERROR", "Path traversal detected in raw file path")

    # Composite key deduplication: (account, calendar_alias, uid)
    events_list = [
        e for e in events_list
        if not (
            e.get("account") == plan["account"]
            and e.get("calendar_alias") == plan["calendar_alias"]
            and e.get("uid") == plan["uid"]
        )
    ]

    if action == "delete-event":
        if raw_file.exists():
            try:
                raw_file.unlink()
            except OSError:
                pass
    else:
        raw_dir.mkdir(parents=True, exist_ok=True)
        if staged_ics:
            _atomic_write_text(raw_file, staged_ics)

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
    _atomic_write_text(reg_file, json.dumps(reg_data, indent=2))

    # 12. Mark journal completed
    journal_data["status"] = "completed"
    journal_data["completed_at"] = datetime.now(timezone.utc).isoformat()
    journal_data["new_etag"] = new_etag
    _atomic_write_text(journal_file, json.dumps(journal_data, indent=2))

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
                content = jf.read_text(encoding="utf-8")
                jdata = json.loads(content)
                if not isinstance(jdata, dict):
                    raise CalendarWriteError(
                        "RECOVERY_REQUIRED",
                        f"Corrupt journal file '{jf.name}': root must be a dict",
                    )
                status = jdata.get("status", "unknown")
                journals_by_status[status] = journals_by_status.get(status, 0) + 1
                if status in ("in_progress", "unknown_remote_outcome", "recovery_required"):
                    in_progress_plans.append(jdata)
            except (json.JSONDecodeError, OSError) as exc:
                raise CalendarWriteError(
                    "RECOVERY_REQUIRED",
                    f"Corrupt journal file '{jf.name}': {exc}",
                ) from exc

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

    if not journal_dir.is_dir():
        return {
            "status": "recovered",
            "recovered_count": 0,
        }

    pending_journals: list[tuple[Path, dict[str, Any]]] = []
    for jf in journal_dir.glob("*.json"):
        try:
            content = jf.read_text(encoding="utf-8")
            jdata = json.loads(content)
            if not isinstance(jdata, dict):
                raise CalendarWriteError(
                    "RECOVERY_REQUIRED",
                    f"Corrupt journal file '{jf.name}': root must be a dict",
                )
            if jdata.get("status") in ("in_progress", "unknown_remote_outcome", "recovery_required"):
                pending_journals.append((jf, jdata))
        except (json.JSONDecodeError, OSError) as exc:
            raise CalendarWriteError(
                "RECOVERY_REQUIRED",
                f"Corrupt journal file '{jf.name}': {exc}",
            ) from exc

    if not pending_journals:
        return {
            "status": "recovered",
            "recovered_count": 0,
        }

    if caldav_client is None:
        raise CalendarWriteError(
            "RECOVERY_REQUIRED",
            "Cannot recover workspace without active caldav_client to verify remote postconditions",
        )

    for jf, jdata in pending_journals:
        action = jdata.get("action")
        res_url = jdata.get("resource_url")
        precondition = jdata.get("precondition") or {}
        pre_state = jdata.get("pre_state") or {}
        if_match_etag = precondition.get("if_match_etag") or pre_state.get("etag")

        fetched: dict[str, Any] | None = None
        try:
            if res_url:
                fetched = caldav_client.get_event(res_url)
        except CalDAVProtocolError as exc:
            if exc.code == "CALDAV_NOT_FOUND" or getattr(exc, "status_code", None) == 404:
                fetched = None
            elif exc.code == "CALDAV_NETWORK_ERROR" or "network" in str(exc).lower():
                raise CalendarWriteError(
                    "RECOVERY_REQUIRED",
                    f"Server unreachable during recovery of '{res_url}': {exc}",
                ) from exc
            else:
                raise
        except (TimeoutError, OSError) as exc:
            raise CalendarWriteError(
                "RECOVERY_REQUIRED",
                f"Server unreachable during recovery of '{res_url}': {exc}",
            ) from exc

        if action == "delete-event":
            if fetched is None:
                new_status = "recovered_applied"
            else:
                new_status = "recovered_aborted"
        elif action in ("update-event", "cancel-event"):
            if fetched is None:
                new_status = "recovered_aborted"
            else:
                remote_etag = fetched.get("etag")
                if if_match_etag and remote_etag and remote_etag != if_match_etag:
                    new_status = "recovered_applied"
                elif not if_match_etag:
                    new_status = "recovered_applied"
                else:
                    new_status = "recovered_aborted"
        elif action == "create-event":
            if fetched is not None:
                new_status = "recovered_applied"
            else:
                new_status = "recovered_aborted"
        else:
            new_status = "recovered_aborted"

        jdata["status"] = new_status
        jdata["recovered_at"] = datetime.now(timezone.utc).isoformat()
        _atomic_write_text(jf, json.dumps(jdata, indent=2))
        recovered_count += 1

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
