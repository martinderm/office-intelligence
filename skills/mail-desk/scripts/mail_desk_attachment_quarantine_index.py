#!/usr/bin/env python3
"""Unified CLI and JSON-manifest client for attachment-quarantine-index.json (MD-Q2)."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any

_script_dir = Path(__file__).resolve().parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))

from core import (
    INDEX_FILENAME,
    SCHEMA_VERSION,
    QuarantineIndexError,
    WorkspaceLockRequiredError,
    AttachmentIndexDriftError,
    AttachmentIndexSchemaError,
    ForbiddenContentError,
    PhysicalVerificationError,
    build_error,
    build_success,
    compute_attachment_id,
    emit_json,
    get_quarantine_index_stats,
    load_quarantine_index,
    lookup_quarantine_entry,
    normalize_message_id,
    reconcile_quarantine_index,
    record_quarantine_entry,
    resolve_data_dir,
    resolve_quarantine_index_path,
)

ACTION = "attachment_quarantine_index"


def _emit_success(operation: str, message: str, data: dict[str, Any], json_output: bool = True) -> None:
    payload = build_success(ACTION, message, {"operation": operation, **data})
    if json_output:
        emit_json(payload)
    else:
        print(f"[OK] {message}")
        if "stats" in data:
            print(json.dumps(data["stats"], indent=2))
        elif "item" in data:
            print(json.dumps(data["item"], indent=2))
        elif "reconcile" in data:
            print(json.dumps(data["reconcile"], indent=2))


def _emit_error(
    operation: str,
    message: str,
    exc: Exception | None = None,
    *,
    state: str = "Failed",
    data: dict[str, Any] | None = None,
    json_output: bool = True,
) -> None:
    payload = build_error(
        ACTION,
        message,
        {"operation": operation, **(data or {})},
        state=state,
        error_type=type(exc).__name__ if exc else "OperationError",
        error_details={"reason": str(exc)[:1000]} if exc else None,
    )
    if json_output:
        emit_json(payload)
    else:
        print(f"[ERROR] {message}: {exc}", file=sys.stderr)
    sys.exit(1)


def execute_record(
    payload: dict[str, Any],
    *,
    data_dir: Path | None = None,
    workspace_root: Path | None = None,
    lease_id: str | None = None,
    conversation_id: str | None = None,
    allow_legacy: bool = False,
    index_path: Path | None = None,
) -> dict[str, Any]:
    """Execute a single record operation under workspace lock."""
    idx = index_path or resolve_quarantine_index_path(data_dir=data_dir, workspace_root=workspace_root)
    res = record_quarantine_entry(
        idx,
        payload=payload,
        workspace_root=workspace_root,
        data_dir=data_dir,
        lease_id=lease_id,
        conversation_id=conversation_id,
        allow_legacy=allow_legacy,
    )
    return res


def main() -> None:
    common_parser = argparse.ArgumentParser(add_help=False)
    common_parser.add_argument("--json", action="store_true", default=True, help="Emit canonical JSON envelope (default: true)")
    common_parser.add_argument("--no-json", action="store_false", dest="json", help="Emit human-readable output")
    common_parser.add_argument("--data-dir", type=Path, default=None, help="Explicit data directory")
    common_parser.add_argument("--workspace-root", type=Path, default=None, help="Explicit workspace root")
    common_parser.add_argument("--lease-id", type=str, default=None, help="Workspace lock lease ID")
    common_parser.add_argument("--conversation-id", type=str, default=None, help="Workspace lock conversation ID")
    common_parser.add_argument("--allow-legacy", action="store_true", default=False, help="Allow legacy single-session lock")

    parser = argparse.ArgumentParser(
        parents=[common_parser],
        description="Script-based client for attachment-quarantine-index.json.",
    )
    parser.add_argument("--input", type=Path, default=None, help="Path to JSON file containing payload for record operation")

    subparsers = parser.add_subparsers(dest="subcommand")

    # stats
    subparsers.add_parser("stats", parents=[common_parser], help="Show summary statistics for quarantine index")

    # lookup
    lookup_p = subparsers.add_parser("lookup", parents=[common_parser], help="Lookup an entry by attachment_id or message_id")
    lookup_p.add_argument("--attachment-id", type=str, default=None, help="Deterministic attachment_id")
    lookup_p.add_argument("--mid", type=str, default=None, help="Normalized or raw Message-ID")

    # record
    rec_p = subparsers.add_parser("record", parents=[common_parser], help="Record an analyzed attachment entry into quarantine index")
    rec_p.add_argument("--input", type=Path, default=None, help="Path to entry JSON file")

    # reconcile
    subparsers.add_parser("reconcile", parents=[common_parser], help="Perform read-only reconciliation of index against disk and inventory")

    args = parser.parse_args()

    # Resolve paths
    ws = args.workspace_root or (Path(os.environ["WORKSPACE_ROOT"]).resolve() if "WORKSPACE_ROOT" in os.environ else None)
    dd = args.data_dir or (resolve_data_dir() if ws is None else ws / "data" / "mail-desk")
    idx_path = resolve_quarantine_index_path(data_dir=dd, workspace_root=ws)

    subcmd = args.subcommand
    if subcmd is None and args.input:
        subcmd = "record"

    if subcmd == "stats" or subcmd is None:
        try:
            stats = get_quarantine_index_stats(idx_path)
            _emit_success("stats", "Quarantine index statistics loaded successfully", {"stats": stats}, json_output=args.json)
        except Exception as exc:
            _emit_error("stats", "Failed to calculate quarantine index stats", exc, json_output=args.json)

    elif subcmd == "lookup":
        try:
            if not args.attachment_id and not args.mid:
                raise ValueError("Must provide either --attachment-id or --mid for lookup")
            item = lookup_quarantine_entry(idx_path, attachment_id=args.attachment_id, message_id=args.mid)
            if item is None:
                _emit_success("lookup", "Attachment entry not found", {"found": False, "item": None}, json_output=args.json)
            else:
                _emit_success("lookup", "Attachment entry found", {"found": True, "item": item}, json_output=args.json)
        except Exception as exc:
            _emit_error("lookup", "Failed to lookup attachment entry", exc, json_output=args.json)

    elif subcmd == "record":
        try:
            input_file = args.input
            if not input_file or not input_file.is_file():
                raise ValueError(f"Valid --input file required for record command, got: {input_file}")
            raw_text = input_file.read_text(encoding="utf-8")
            payload = json.loads(raw_text)

            eff_lease = args.lease_id or os.environ.get("WORKSPACE_LOCK_LEASE_ID")
            eff_conv = args.conversation_id or os.environ.get("WORKSPACE_LOCK_CONVERSATION_ID")
            eff_legacy = args.allow_legacy or (os.environ.get("WORKSPACE_LOCK_ALLOW_LEGACY", "").lower() in ("1", "true"))

            res = execute_record(
                payload,
                data_dir=dd,
                workspace_root=ws,
                lease_id=eff_lease,
                conversation_id=eff_conv,
                allow_legacy=eff_legacy,
                index_path=idx_path,
            )
            _emit_success("record", f"Quarantine entry {res['status']}", res, json_output=args.json)
        except Exception as exc:
            _emit_error("record", "Failed to record quarantine entry", exc, json_output=args.json)

    elif subcmd == "reconcile":
        try:
            recon = reconcile_quarantine_index(idx_path, workspace_root=ws, data_dir=dd)
            _emit_success("reconcile", f"Reconcile completed: {recon['status']}", {"reconcile": recon}, json_output=args.json)
        except Exception as exc:
            _emit_error("reconcile", "Failed to reconcile quarantine index", exc, json_output=args.json)

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
