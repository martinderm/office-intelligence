"""CLI client and sub-agent entry point for skills/calendar-desk.

Provides discover, agenda, search, get-event, freebusy, and find-slots commands
with deterministic --json structured output envelope.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
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

    search_p = subparsers.add_parser("search", help="Search calendar events.")
    search_p.add_argument("--window-start", required=True, help="ISO-8601 start time (UTC).")
    search_p.add_argument("--window-end", required=True, help="ISO-8601 end time (UTC).")
    search_p.add_argument("--query", default="", help="Query string to filter summary/description.")
    search_p.add_argument("--max-items", type=int, default=500, help="Maximum items to return.")

    # get-event
    get_p = subparsers.add_parser("get-event", help="Get a single event by resource URL.")
    get_p.add_argument("--url", required=True, help="Resource URL or href.")

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
    slots_p.add_argument("--buffer-minutes", type=int, default=0, help="Buffer padding in minutes.")
    slots_p.add_argument("--records-file", default=None, help="Optional JSON file of existing calendar records.")
    slots_p.add_argument("--termino-slots", action="store_true", help="Include termino-api formatted slot strings.")

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
        if args.subcommand == "freebusy":
            records: list[dict[str, Any]] = []
            if args.records_file and Path(args.records_file).is_file():
                records = json.loads(Path(args.records_file).read_text(encoding="utf-8"))
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

            res = compute_freebusy(records, args.window_start, args.window_end)
            _emit_result("freebusy", res, as_json=args.json)
            return 0

        elif args.subcommand == "find-slots":
            records = []
            if args.records_file and Path(args.records_file).is_file():
                records = json.loads(Path(args.records_file).read_text(encoding="utf-8"))
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

            res = find_free_slots(
                records,
                args.window_start,
                args.window_end,
                tzid=args.tzid,
                working_hours=args.working_hours,
                min_slot_minutes=args.min_slot_minutes,
                buffer_minutes=args.buffer_minutes,
            )
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
            records = query_res["records"]
            fenced_records = query_res.get("fenced_records", [])
            if args.subcommand == "search" and args.query:
                q_lower = args.query.lower()
                matching_indices = [
                    idx for idx, r in enumerate(records)
                    if q_lower in r.get("summary", "").lower()
                    or q_lower in r.get("description", "").lower()
                ]
                records = [records[i] for i in matching_indices]
                fenced_records = [
                    fenced_records[i] for i in matching_indices
                    if i < len(fenced_records)
                ]
            _emit_result(
                args.subcommand,
                {
                    "records": records,
                    "fenced_records": fenced_records,
                    "item_count": len(records),
                },
                as_json=args.json,
            )
            return 0

        elif args.subcommand == "get-event":
            evt = client.get_event(args.url)
            _emit_result("get-event", evt, as_json=args.json)
            return 0

    except (CalDAVConfigError, CalDAVProtocolError) as exc:
        err_dict = {"code": exc.code, "message": str(exc)}
        _emit_error(args.subcommand, err_dict, as_json=args.json)
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
