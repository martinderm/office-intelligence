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


def _resolve_env_secrets(secrets_file: Path, account_id: str | None) -> dict[str, str]:
    """Helper to support top-level accounts in secrets.json via environment fallback."""
    env_override = dict(os.environ)
    if not account_id or not secrets_file.is_file():
        return env_override
    try:
        sdata = json.loads(secrets_file.read_text(encoding="utf-8"))
        top_accounts = sdata.get("accounts", {})
        if account_id in top_accounts:
            acct_data = top_accounts[account_id]
            u_key = account_id.upper().replace("-", "_")
            if acct_data.get("username"):
                env_override[f"CALDAV_USERNAME_{u_key}"] = acct_data["username"]
            if acct_data.get("password"):
                env_override[f"CALDAV_PASSWORD_{u_key}"] = acct_data["password"]
            if acct_data.get("principal_url"):
                env_override[f"CALDAV_PRINCIPAL_URL_{u_key}"] = acct_data["principal_url"]
    except Exception:
        pass
    return env_override


def load_guard(trusted_root: str | Path | None = None) -> Any:
    """Public helper exposing the verified workspace lock guard module."""
    import importlib.util

    if trusted_root is not None:
        root = Path(trusted_root)
    else:
        env_val = os.environ.get("WORKSPACE_LOCK_ROOT", "").strip()
        if env_val:
            root = Path(env_val)
        else:
            here = Path(__file__).resolve()
            found = None
            for cand in (
                here.parents[4] / "workspace-lock",
                here.parents[3] / "skills" / "workspace-lock",
                here.parents[3] / "workspace-lock",
                here.parents[2] / "workspace-lock",
            ):
                if (cand / "scripts" / "workspace_lock_guard.py").is_file():
                    found = cand
                    break
            root = found if found is not None else (here.parents[3] / "workspace-lock")

    path = root.resolve() / "scripts" / "workspace_lock_guard.py"
    if not path.is_file():
        raise RuntimeError(
            "Install workspace-lock and set WORKSPACE_LOCK_ROOT "
            "to its trusted installation; see README.md"
        )

    spec = importlib.util.spec_from_file_location("_calendar_desk_workspace_lock_guard", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("canonical workspace-lock guard is unavailable")
    guard_mod = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(spec.name)
    sys.modules[spec.name] = guard_mod
    try:
        spec.loader.exec_module(guard_mod)
    finally:
        if previous is None:
            sys.modules.pop(spec.name, None)
        else:
            sys.modules[spec.name] = previous
    return guard_mod


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
                    cfg = load_backend_config(cfg_file)
                    target_acct = args.account or cfg.get("default_account")
                    env_override = _resolve_env_secrets(sec_file, target_acct)
                    target = resolve_calendar_target(
                        cfg,
                        account_id=args.account,
                        calendar_alias=args.calendar_alias,
                        secrets_path=sec_file,
                        env=env_override,
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
            try:
                target = resolve_calendar_target(
                    cfg,
                    account_id=args.account,
                    calendar_alias=args.calendar_alias,
                    secrets_path=sec_file,
                )
                account_id = target["account_id"]
                calendar_alias = target["calendar_alias"]
                collection_url = target["collection_url"]
                write_policy = target["write_policy"]
                target_tz = target["default_timezone"]
            except CalDAVConfigError as exc:
                if exc.code == "CALDAV_SECRET_MISSING":
                    # Lock-free dry-run planning without secrets
                    target_account = args.account or cfg.get("default_account")
                    accounts = cfg.get("accounts", {})
                    if not target_account or target_account not in accounts:
                        raise CalDAVConfigError(
                            "CALDAV_ACCOUNT_NOT_FOUND",
                            f"Account '{target_account}' not found in configuration",
                        )
                    acct_meta = accounts[target_account] or {}
                    calendars = acct_meta.get("calendars", {})
                    target_alias = args.calendar_alias or "primary"
                    if target_alias not in calendars:
                        if args.calendar_alias is None and len(calendars) == 1:
                            target_alias = next(iter(calendars))
                        else:
                            raise CalDAVConfigError(
                                "CALDAV_CALENDAR_NOT_FOUND",
                                f"Calendar alias '{target_alias}' not found in account '{target_account}'",
                            )
                    cal_meta = calendars[target_alias] or {}
                    principal_url = acct_meta.get("principal_url", "")
                    if cal_meta.get("collection_url"):
                        collection_url = cal_meta["collection_url"]
                    else:
                        collection_url = urllib.parse.urljoin(principal_url, cal_meta.get("collection_path", ""))
                    account_id = target_account
                    calendar_alias = target_alias
                    write_policy = cal_meta.get("write_policy", "read_only")
                    target_tz = (
                        cal_meta.get("default_timezone")
                        or acct_meta.get("default_timezone")
                        or cfg.get("default_timezone", "Europe/Vienna")
                    )
                else:
                    raise

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
            )
            _emit_result("plan", plan_res, as_json=args.json)
            return 0

        elif args.subcommand == "apply":
            plan_path = Path(args.plan_file)
            if not plan_path.is_file():
                raise CalendarWriteError("PLAN_NOT_FOUND", f"Plan file '{args.plan_file}' not found")
            plan = json.loads(plan_path.read_text(encoding="utf-8"))

            receipt_path = Path(args.receipt_file)
            if not receipt_path.is_file():
                raise CalendarWriteError(
                    "APPROVAL_RECEIPT_INVALID", f"Receipt file '{args.receipt_file}' not found"
                )
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))

            cfg = load_backend_config(cfg_file)
            target_acct = plan.get("account") or args.account or cfg.get("default_account")
            env_override = _resolve_env_secrets(sec_file, target_acct)
            target = resolve_calendar_target(
                cfg,
                account_id=plan.get("account") or args.account,
                calendar_alias=plan.get("calendar_alias") or args.calendar_alias,
                secrets_path=sec_file,
                env=env_override,
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
            if args.records_file:
                rec_path = Path(args.records_file)
                if not rec_path.is_file():
                    raise CalDAVConfigError(
                        "CALDAV_SOURCE_MISSING",
                        f"Records file '{args.records_file}' not found",
                    )
                try:
                    records = json.loads(rec_path.read_text(encoding="utf-8"))
                except Exception as exc:
                    raise CalDAVConfigError(
                        "CALDAV_SOURCE_MISSING",
                        f"Failed to parse records file '{args.records_file}': {exc}",
                    ) from exc
            elif cfg_file.is_file():
                cfg = load_backend_config(cfg_file)
                target = resolve_calendar_target(
                    cfg,
                    account_id=args.account,
                    calendar_alias=args.calendar_alias,
                    secrets_path=sec_file,
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
                query_res = client.query_events(
                    target["collection_url"],
                    args.window_start,
                    args.window_end,
                )
                records = query_res["records"]
            else:
                raise CalDAVConfigError(
                    "CALDAV_SOURCE_MISSING",
                    "No records file or backend config available for calendar query",
                )

            res = compute_freebusy(records, args.window_start, args.window_end)
            _emit_result("freebusy", res, as_json=args.json)
            return 0

        elif args.subcommand == "find-slots":
            records = []
            if args.records_file:
                rec_path = Path(args.records_file)
                if not rec_path.is_file():
                    raise CalDAVConfigError(
                        "CALDAV_SOURCE_MISSING",
                        f"Records file '{args.records_file}' not found",
                    )
                try:
                    records = json.loads(rec_path.read_text(encoding="utf-8"))
                except Exception as exc:
                    raise CalDAVConfigError(
                        "CALDAV_SOURCE_MISSING",
                        f"Failed to parse records file '{args.records_file}': {exc}",
                    ) from exc
            elif cfg_file.is_file():
                cfg = load_backend_config(cfg_file)
                target = resolve_calendar_target(
                    cfg,
                    account_id=args.account,
                    calendar_alias=args.calendar_alias,
                    secrets_path=sec_file,
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
                query_res = client.query_events(
                    target["collection_url"],
                    args.window_start,
                    args.window_end,
                )
                records = query_res["records"]
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
            )
            if not args.json and (getattr(args, "format", "json") == "termino" or getattr(args, "termino_slots", False)):
                for ts in res.get("termino_slots", []):
                    sys.stdout.write(f"{ts}\n")
                return 0
            _emit_result("find-slots", res, as_json=args.json)
            return 0

        # Commands requiring backend config
        cfg = load_backend_config(cfg_file)
        target = resolve_calendar_target(
            cfg,
            account_id=args.account,
            calendar_alias=args.calendar_alias,
            secrets_path=sec_file,
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
                }
            else:
                payload_data = {
                    "records": fenced_records,
                    "fenced_records": fenced_records,
                    "item_count": len(fenced_records),
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

    except (CalDAVConfigError, CalDAVProtocolError, CalendarWriteError) as exc:
        err_dict = {"code": exc.code, "message": str(exc)}
        _emit_error(args.subcommand, err_dict, as_json=args.json)
        if getattr(exc, "code", None) == "RECOVERY_REQUIRED":
            return 3
        return 1
    except Exception as exc:
        err_dict = {"code": "INTERNAL_ERROR", "message": str(exc)}
        _emit_error(args.subcommand, err_dict, as_json=args.json)
        return 1

    return 0


def _emit_result(action: str, data: Any, as_json: bool = False) -> None:
    if as_json:
        payload = {"status": "ok", "action": action, "data": data}
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    else:
        sys.stdout.write(f"[{action}] OK\n{json.dumps(data, indent=2)}\n")


def _emit_error(action: str, error: dict[str, Any], as_json: bool = False) -> None:
    if as_json:
        payload = {"status": "error", "action": action, "error": error}
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    else:
        sys.stderr.write(f"[{action}] ERROR: {error.get('code')} - {error.get('message')}\n")


if __name__ == "__main__":
    sys.exit(main())
