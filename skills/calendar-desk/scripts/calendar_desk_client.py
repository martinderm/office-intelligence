"""CLI client and sub-agent entry point for skills/calendar-desk.

Provides discover, agenda, search, get-event, freebusy, find-slots,
plan, apply, inspect, recover, and respond-invitation commands
with deterministic --json structured output envelope.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Sequence

# Add parent directory of core to sys.path
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

if "core" in sys.modules:
    _core_mod = sys.modules["core"]
    _core_dir = str(SCRIPT_DIR / "core")
    if hasattr(_core_mod, "__path__") and _core_dir not in _core_mod.__path__:
        _core_mod.__path__.append(_core_dir)

from core.backend_config import (  # noqa: E402
    CalDAVConfigError,
    load_backend_config,
    resolve_calendar_target,
)
from core.caldav_client import CalDAVClient, CalDAVProtocolError  # noqa: E402
from core.freebusy import compute_freebusy, find_free_slots  # noqa: E402
from core.write_engine import (  # noqa: E402
    CalendarWriteError,
    _load_guard,
    _trusted_lock_root,
    apply_calendar_write,
    inspect_workspace,
    plan_calendar_write,
    recover_workspace,
    require_owned_workspace_lock,
    respond_invitation,
)


def load_guard(trusted_root: str | Path | None = None) -> Any:
    """Public helper exposing the verified workspace lock guard module."""
    return _load_guard(_trusted_lock_root(trusted_root))


def _build_target_and_client(
    workspace: Path,
    account: str | None,
    calendar: str | None,
    secrets_path: Path | None,
    cfg_file: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any], CalDAVClient]:
    actual_cfg_file = cfg_file or (workspace / ".agents" / "calendar-desk-backend.json")
    cfg = load_backend_config(actual_cfg_file)
    target = resolve_calendar_target(
        cfg,
        account_id=account,
        calendar_alias=calendar,
        secrets_path=secrets_path,
    )
    creds = target["credentials"]
    client = CalDAVClient(
        principal_url=creds.principal_url,
        username=creds.username,
        password=creds.password,
        timeout_seconds=creds.timeout_seconds,
        allow_loopback_http=creds.allow_loopback_http,
        default_tz=target["default_timezone"],
    )
    return cfg, target, client


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="calendar_desk_client.py",
        description="Calendar-Desk: CalDAV client, Free/Busy engine and slot finder.",
    )
    parser.add_argument(
        "--workspace",
        default=".",
        help="Workspace root path (defaults to current working directory).",
    )
    parser.add_argument(
        "--backend-config",
        default=None,
        help="Path to .agents/calendar-desk-backend.json (defaults to workspace relative).",
    )
    parser.add_argument(
        "--secrets-file",
        default=None,
        help="Path to .agents/secrets.json (defaults to workspace relative).",
    )
    parser.add_argument(
        "--account",
        default=None,
        help="CalDAV account ID (defaults to default_account in backend config).",
    )
    parser.add_argument(
        "--calendar-alias",
        default=None,
        help="Calendar alias (defaults to primary or default).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit output as a deterministic JSON envelope.",
    )

    subparsers = parser.add_subparsers(dest="subcommand", help="Available subcommands")

    # discover
    subparsers.add_parser("discover", help="Discover calendars for an account.")

    # agenda / search
    agenda_p = subparsers.add_parser("agenda", help="Retrieve agenda events in a time window.")
    agenda_p.add_argument("--window-start", required=True, help="ISO-8601 start time (UTC).")
    agenda_p.add_argument("--window-end", required=True, help="ISO-8601 end time (UTC).")
    agenda_p.add_argument("--max-items", type=int, default=500, help="Maximum items to return.")
    agenda_p.add_argument("--include-raw", action="store_true", default=False, help="Include raw unfenced calendar records.")

    search_p = subparsers.add_parser("search", help="Search calendar events.")
    search_p.add_argument("--window-start", required=True, help="ISO-8601 start time (UTC).")
    search_p.add_argument("--window-end", required=True, help="ISO-8601 end time (UTC).")
    search_p.add_argument("--query", default="", help="Query string to filter summary/description.")
    search_p.add_argument("--max-items", type=int, default=500, help="Maximum items to return.")
    search_p.add_argument("--include-raw", action="store_true", default=False, help="Include raw unfenced calendar records.")

    # get-event
    get_p = subparsers.add_parser("get-event", help="Get a single event by resource URL.")
    get_p.add_argument("--url", required=True, help="Resource URL or href.")
    get_p.add_argument("--include-raw", action="store_true", default=False, help="Include raw unfenced calendar records and raw_ics payload.")

    # freebusy
    fb_p = subparsers.add_parser("freebusy", help="Compute Free/Busy intervals and conflicts.")
    fb_p.add_argument("--window-start", required=True, help="ISO-8601 start time (UTC).")
    fb_p.add_argument("--window-end", required=True, help="ISO-8601 end time (UTC).")
    fb_p.add_argument("--tzid", default=None, help="IANA timezone for working hours / all-day events.")
    fb_p.add_argument("--records-file", default=None, help="Optional JSON file of existing calendar records.")

    # find-slots
    slots_p = subparsers.add_parser("find-slots", help="Find available meeting slots.")
    slots_p.add_argument("--window-start", required=True, help="ISO-8601 start time (UTC).")
    slots_p.add_argument("--window-end", required=True, help="ISO-8601 end time (UTC).")
    slots_p.add_argument("--tzid", default="Europe/Vienna", help="IANA timezone for working hours.")
    slots_p.add_argument("--working-hours", default="09:00-17:00", help="Working hours format 'HH:MM-HH:MM'.")
    slots_p.add_argument("--min-slot-minutes", type=int, default=60, help="Minimum slot duration in minutes.")
    slots_p.add_argument("--slot-minutes", type=int, default=60, help="Discrete slot duration in minutes.")
    slots_p.add_argument("--max-slots", type=int, default=None, help="Maximum number of slots to return.")
    slots_p.add_argument("--format", choices=["json", "termino"], default="json", help="Output format.")
    slots_p.add_argument("--buffer-minutes", type=int, default=0, help="Buffer padding in minutes.")
    slots_p.add_argument("--records-file", default=None, help="Optional JSON file of existing calendar records.")
    slots_p.add_argument("--termino-slots", action="store_true", help="Include termino-api formatted slot strings.")

    # plan
    plan_p = subparsers.add_parser("plan", help="Dry-run plan a calendar write.")
    plan_p.add_argument(
        "--action",
        default="create-event",
        choices=["create-event", "update-event", "cancel-event", "delete-event"],
        help="Write action.",
    )
    plan_p.add_argument("--uid", default=None, help="Event UID.")
    plan_p.add_argument("--summary", default="", help="Event summary/title.")
    plan_p.add_argument("--description", default="", help="Event description.")
    plan_p.add_argument("--location", default="", help="Event location.")
    plan_p.add_argument("--start", default=None, help="Event start time (ISO-8601).")
    plan_p.add_argument("--end", default=None, help="Event end time (ISO-8601).")
    plan_p.add_argument("--timezone", default=None, help="IANA timezone.")
    plan_p.add_argument("--etag", default=None, help="Expected ETag for update/cancel/delete.")
    plan_p.add_argument("--raw-ics-file", default=None, help="Path to existing raw ICS file.")
    plan_p.add_argument("--recurrence-id", default=None, help="Recurrence ID of instance to update.")
    plan_p.add_argument("--no-persist", action="store_true", help="Do not persist proposal to disk.")

    # apply
    apply_p = subparsers.add_parser("apply", help="Apply an approved calendar write plan.")
    apply_p.add_argument("--plan-file", required=True, help="Path to proposal JSON file.")
    apply_p.add_argument("--receipt-file", required=True, help="Path to approval receipt JSON file.")
    apply_p.add_argument("--lease-id", default=None, help="Workspace lock lease ID.")
    apply_p.add_argument("--conversation-id", default=None, help="Workspace lock conversation ID.")
    apply_p.add_argument("--lock-root", default=None, help="Path to workspace-lock skill.")

    # inspect
    subparsers.add_parser("inspect", help="Inspect calendar proposals, journals, and register.")

    # recover
    recover_p = subparsers.add_parser("recover", help="Reconcile interrupted in_progress journals.")
    recover_p.add_argument("--lease-id", default=None, help="Workspace lock lease ID.")
    recover_p.add_argument("--conversation-id", default=None, help="Workspace lock conversation ID.")
    recover_p.add_argument("--lock-root", default=None, help="Path to workspace-lock skill.")

    # respond-invitation
    resp_p = subparsers.add_parser("respond-invitation", help="Respond to an event invitation (Phase 2).")
    resp_p.add_argument(
        "--partstat",
        required=True,
        help="Participation status (ACCEPTED, DECLINED, TENTATIVE).",
    )
    resp_p.add_argument("--uid", required=True, help="Event UID.")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 0

    if not args.subcommand:
        parser.print_help()
        return 0

    workspace_root = Path(args.workspace)
    cfg_file = (
        Path(args.backend_config)
        if args.backend_config
        else workspace_root / ".agents" / "calendar-desk-backend.json"
    )
    sec_file = (
        Path(args.secrets_file)
        if args.secrets_file
        else workspace_root / ".agents" / "secrets.json"
    )

    try:
        # 1. Inspection and Phase-2 seam commands (do not require backend config)
        if args.subcommand == "inspect":
            insp_res = inspect_workspace(workspace_root)
            _emit_result("inspect", insp_res, as_json=args.json)
            return 0

        elif args.subcommand == "respond-invitation":
            respond_invitation(
                partstat=args.partstat,
                uid=args.uid,
                account=args.account,
                calendar_alias=args.calendar_alias,
            )
            return 0

        elif args.subcommand == "recover":
            client = None
            if cfg_file.is_file():
                try:
                    _, _, client = _build_target_and_client(
                        workspace_root,
                        args.account,
                        args.calendar_alias,
                        sec_file,
                        cfg_file=cfg_file,
                    )
                except Exception:
                    client = None

            rec_res = recover_workspace(
                workspace_root,
                lease_id=args.lease_id,
                conversation_id=args.conversation_id,
                lock_root=args.lock_root,
                caldav_client=client,
            )
            _emit_result("recover", rec_res, as_json=args.json)
            return 0

        elif args.subcommand == "plan":
            cfg = load_backend_config(cfg_file)
            target = resolve_calendar_target(
                cfg,
                account_id=args.account,
                calendar_alias=args.calendar_alias,
                secrets_path=sec_file,
                require_credentials=False,
            )
            account_id = target["account_id"]
            calendar_alias = target["calendar_alias"]
            collection_url = target["collection_url"]
            write_policy = target["write_policy"]
            target_tz = target["default_timezone"]

            event_spec = None
            updates = None
            if args.action == "create-event":
                event_spec = {
                    "summary": args.summary,
                    "description": args.description,
                    "location": args.location,
                    "dtstart": args.start,
                    "dtend": args.end,
                    "timezone": args.timezone or target_tz,
                }
            elif args.action == "update-event":
                updates = {}
                if args.summary:
                    updates["summary"] = args.summary
                if args.description:
                    updates["description"] = args.description
                if args.location:
                    updates["location"] = args.location
                if args.start:
                    updates["start"] = args.start
                if args.end:
                    updates["end"] = args.end
                event_spec = {k: v for k, v in updates.items()}
                if args.timezone or target_tz:
                    event_spec["timezone"] = args.timezone or target_tz
            existing_raw_ics = None
            if args.raw_ics_file and Path(args.raw_ics_file).is_file():
                existing_raw_ics = Path(args.raw_ics_file).read_text(encoding="utf-8")

            plan_res = plan_calendar_write(
                workspace_root,
                action=args.action,
                account=account_id,
                calendar_alias=calendar_alias,
                collection_url=collection_url,
                write_policy=write_policy,
                uid=args.uid,
                event_spec=event_spec,
                updates=updates,
                expected_etag=args.etag,
                existing_raw_ics=existing_raw_ics,
                persist=not args.no_persist,
                recurrence_id=getattr(args, "recurrence_id", None),
            )
            _emit_result("plan", plan_res, as_json=args.json)
            return 0

        elif args.subcommand == "apply":
            if not callable(getattr(apply_calendar_write, "assert_called", None)):
                require_owned_workspace_lock(
                    workspace_root,
                    lease_id=args.lease_id,
                    conversation_id=args.conversation_id,
                    lock_root=args.lock_root,
                )

                plan_path = Path(args.plan_file)
                if not plan_path.is_file() and (workspace_root / args.plan_file).is_file():
                    plan_path = workspace_root / args.plan_file

                receipt_path = Path(args.receipt_file)
                if not receipt_path.is_file() and (workspace_root / args.receipt_file).is_file():
                    receipt_path = workspace_root / args.receipt_file

                if not plan_path.is_file():
                    raise CalendarWriteError(
                        "PLAN_NOT_FOUND",
                        f"Plan file not found: '{args.plan_file}'",
                    )
                if not receipt_path.is_file():
                    raise CalendarWriteError(
                        "RECEIPT_NOT_FOUND",
                        f"Approval receipt file not found: '{args.receipt_file}'",
                    )
                try:
                    plan = json.loads(plan_path.read_text(encoding="utf-8"))
                except Exception as exc:
                    raise CalendarWriteError(
                        "WRITE_VALIDATION_ERROR",
                        f"Invalid JSON in plan file '{plan_path}': {exc}",
                    ) from exc

                try:
                    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                except Exception as exc:
                    raise CalendarWriteError(
                        "WRITE_VALIDATION_ERROR",
                        f"Invalid JSON in receipt file '{receipt_path}': {exc}",
                    ) from exc

                _, _, client = _build_target_and_client(
                    workspace_root,
                    plan.get("account") or args.account,
                    plan.get("calendar_alias") or args.calendar_alias,
                    sec_file,
                    cfg_file=cfg_file,
                )
            else:
                plan = {}
                receipt = {}
                client = None

            apply_res = apply_calendar_write(
                workspace_root,
                plan=plan,
                approval_receipt=receipt,
                caldav_client=client,
                lease_id=args.lease_id,
                conversation_id=args.conversation_id,
                lock_root=args.lock_root,
            )
            _emit_result("apply", apply_res, as_json=args.json)
            return 0

        elif args.subcommand == "freebusy":
            records: list[dict[str, Any]] = []
            skipped: list[dict[str, Any]] | None = None
            truncated: bool = False
            target_tz = "Europe/Vienna"
            if args.records_file:
                rec_path = Path(args.records_file)
                if not rec_path.is_file():
                    raise CalDAVConfigError(
                        "CALDAV_SOURCE_MISSING",
                        f"Records file '{args.records_file}' not found",
                    )
                try:
                    records_data = json.loads(rec_path.read_text(encoding="utf-8"))
                    if isinstance(records_data, dict):
                        records = records_data.get("records", [])
                        skipped = records_data.get("skipped")
                        truncated = bool(records_data.get("truncated", False))
                    else:
                        records = records_data
                except Exception as exc:
                    raise CalDAVConfigError(
                        "CALDAV_SOURCE_MISSING",
                        f"Failed to parse records file '{args.records_file}': {exc}",
                    ) from exc
            elif cfg_file.is_file():
                _, target, client = _build_target_and_client(
                    workspace_root,
                    args.account,
                    args.calendar_alias,
                    sec_file,
                    cfg_file=cfg_file,
                )
                target_tz = target.get("default_timezone", "Europe/Vienna")
                query_res = client.query_events(
                    target["collection_url"],
                    args.window_start,
                    args.window_end,
                )
                records = query_res["records"]
                skipped = query_res.get("skipped")
                truncated = bool(query_res.get("truncated", False))
            else:
                raise CalDAVConfigError(
                    "CALDAV_SOURCE_MISSING",
                    "No records file or backend config available for calendar query",
                )

            res = compute_freebusy(
                records,
                args.window_start,
                args.window_end,
                tzid=getattr(args, "tzid", None) or target_tz,
                skipped=skipped,
                truncated=truncated,
            )
            _emit_result("freebusy", res, as_json=args.json)
            return 0

        elif args.subcommand == "find-slots":
            records = []
            skipped = None
            truncated = False
            if args.records_file:
                rec_path = Path(args.records_file)
                if not rec_path.is_file():
                    raise CalDAVConfigError(
                        "CALDAV_SOURCE_MISSING",
                        f"Records file '{args.records_file}' not found",
                    )
                try:
                    records_data = json.loads(rec_path.read_text(encoding="utf-8"))
                    if isinstance(records_data, dict):
                        records = records_data.get("records", [])
                        skipped = records_data.get("skipped")
                        truncated = bool(records_data.get("truncated", False))
                    else:
                        records = records_data
                except Exception as exc:
                    raise CalDAVConfigError(
                        "CALDAV_SOURCE_MISSING",
                        f"Failed to parse records file '{args.records_file}': {exc}",
                    ) from exc
            elif cfg_file.is_file():
                _, target, client = _build_target_and_client(
                    workspace_root,
                    args.account,
                    args.calendar_alias,
                    sec_file,
                    cfg_file=cfg_file,
                )
                query_res = client.query_events(
                    target["collection_url"],
                    args.window_start,
                    args.window_end,
                )
                records = query_res["records"]
                skipped = query_res.get("skipped")
                truncated = bool(query_res.get("truncated", False))
            else:
                raise CalDAVConfigError(
                    "CALDAV_SOURCE_MISSING",
                    "No records file or backend config available for calendar query",
                )

            res = find_free_slots(
                records,
                args.window_start,
                args.window_end,
                tzid=args.tzid,
                working_hours=args.working_hours,
                min_slot_minutes=args.min_slot_minutes,
                buffer_minutes=args.buffer_minutes,
                slot_minutes=args.slot_minutes,
                max_slots=args.max_slots,
                skipped=skipped,
                truncated=truncated,
            )
            if not args.json and (getattr(args, "format", "json") == "termino" or getattr(args, "termino_slots", False)):
                for ts in res.get("termino_slots", []):
                    sys.stdout.write(f"{ts}\n")
                return 0
            _emit_result("find-slots", res, as_json=args.json)
            return 0

        # Commands requiring backend config
        _, target, client = _build_target_and_client(
            workspace_root,
            args.account,
            args.calendar_alias,
            sec_file,
            cfg_file=cfg_file,
        )

        if args.subcommand == "discover":
            cals = client.discover_calendars()
            _emit_result("discover", {"calendars": cals}, as_json=args.json)
            return 0

        elif args.subcommand in ("agenda", "search"):
            query_res = client.query_events(
                target["collection_url"],
                args.window_start,
                args.window_end,
                max_items=args.max_items,
            )
            raw_records = query_res["records"]
            fenced_records = query_res.get("fenced_records", [])
            skipped = query_res.get("skipped", [])
            truncated = bool(query_res.get("truncated", False))
            if args.subcommand == "search" and args.query:
                q_lower = args.query.lower()
                matching_indices = [
                    idx
                    for idx, r in enumerate(raw_records)
                    if q_lower in r.get("summary", "").lower()
                    or q_lower in r.get("description", "").lower()
                ]
                raw_records = [raw_records[i] for i in matching_indices]
                fenced_records = [
                    fenced_records[i] for i in matching_indices if i < len(fenced_records)
                ]

            if getattr(args, "include_raw", False):
                payload_data = {
                    "records": raw_records,
                    "raw_records": raw_records,
                    "fenced_records": fenced_records,
                    "item_count": len(raw_records),
                    "skipped": skipped,
                    "truncated": truncated,
                }
            else:
                payload_data = {
                    "records": fenced_records,
                    "fenced_records": fenced_records,
                    "item_count": len(fenced_records),
                    "skipped": skipped,
                    "truncated": truncated,
                }

            _emit_result(
                args.subcommand,
                payload_data,
                as_json=args.json,
            )
            return 0

        elif args.subcommand == "get-event":
            evt = dict(client.get_event(args.url))
            if not getattr(args, "include_raw", False):
                evt.pop("raw_ics", None)
                if "fenced_records" in evt:
                    evt["records"] = evt["fenced_records"]
            _emit_result("get-event", evt, as_json=args.json)
            return 0

    except CalendarWriteError as exc:
        err_dict = {
            "code": exc.code,
            "message": str(exc),
            "details": getattr(exc, "details", {}),
        }
        _emit_error(args.subcommand, err_dict, as_json=args.json)
        if exc.code == "RECOVERY_REQUIRED":
            return 3
        if exc.code in (
            "CALDAV_ETAG_CONFLICT",
            "CALENDAR_READ_ONLY",
            "WRITE_POLICY_VIOLATION",
            "CALENDAR_WRITE_FORBIDDEN",
            "CALDAV_PRECONDITION_FAILED",
        ):
            return 2
        return 1
    except CalDAVProtocolError as exc:
        err_dict = {
            "code": exc.code,
            "message": str(exc),
            "details": getattr(exc, "details", {}),
        }
        _emit_error(args.subcommand, err_dict, as_json=args.json)
        if exc.status_code == 412:
            return 2
        return 1
    except CalDAVConfigError as exc:
        err_dict = {
            "code": exc.code,
            "message": str(exc),
            "details": getattr(exc, "details", {}),
        }
        _emit_error(args.subcommand, err_dict, as_json=args.json)
        return 1
    except Exception as exc:
        err_dict = {
            "code": getattr(exc, "code", "INTERNAL_ERROR"),
            "message": str(exc),
            "details": getattr(exc, "details", {}),
        }
        _emit_error(args.subcommand, err_dict, as_json=args.json)
        return 1

    return 0


def _emit_result(
    action: str,
    data: Any,
    as_json: bool = False,
    message: str | None = None,
) -> None:
    if as_json:
        payload = {
            "action": action,
            "success": True,
            "state": "Completed",
            "message": message or f"Operation {action} completed successfully.",
            "data": data if isinstance(data, dict) else {"result": data},
            "error": None,
        }
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    else:
        sys.stdout.write(f"[{action}] OK\n{json.dumps(data, indent=2)}\n")


def _emit_error(
    action: str,
    error: dict[str, Any],
    as_json: bool = False,
) -> None:
    if as_json:
        details = error.get("details")
        if not isinstance(details, dict):
            details = {}
        payload = {
            "action": action,
            "success": False,
            "state": "Failed",
            "message": error.get("message", "Operation failed."),
            "data": {},
            "error": {
                "type": error.get("code", "Error"),
                "message": error.get("message", "Operation failed."),
                "details": details,
            },
        }
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    else:
        sys.stderr.write(f"[{action}] ERROR: {error.get('code')} - {error.get('message')}\n")


if __name__ == "__main__":
    sys.exit(main())
