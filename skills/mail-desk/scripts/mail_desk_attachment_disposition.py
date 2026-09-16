#!/usr/bin/env python3
"""Unified CLI and JSON-manifest client for attachment-disposition-log.jsonl (MD-Q3)."""

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
    DISPOSITION_LOG_FILENAME,
    INDEX_FILENAME,
    ALLOWED_DECISIONS,
    DispositionError,
    DispositionSchemaError,
    DispositionDriftError,
    DispositionLockRequiredError,
    DispositionApplyError,
    ReceiptError,
    ReceiptMissingError,
    ReceiptMalformedError,
    ReceiptDriftError,
    RecoveryEvidenceMissingError,
    QuarantineIndexError,
    WorkspaceLockRequiredError,
    AttachmentIndexDriftError,
    AttachmentIndexSchemaError,
    ForbiddenContentError,
    PhysicalVerificationError,
    build_error,
    build_success,
    emit_json,
    load_disposition_log,
    record_disposition_entry,
    report_dispositions,
    apply_discard,
    resolve_data_dir,
    resolve_disposition_log_path,
    resolve_quarantine_index_path,
    utc_now_iso,
)

ACTION = "attachment_disposition"


def _emit_success(operation: str, message: str, data: dict[str, Any], json_output: bool = True) -> None:
    payload = build_success(ACTION, message, {"operation": operation, **data})
    if json_output:
        emit_json(payload)
    else:
        print(f"[OK] {message}")
        print(json.dumps(data, indent=2))


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


def main() -> None:
    common_parser = argparse.ArgumentParser(add_help=False)
    common_parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="Emit canonical JSON envelope (default: true)")
    common_parser.add_argument("--no-json", action="store_false", dest="json", default=argparse.SUPPRESS, help="Emit human-readable output")
    common_parser.add_argument("--data-dir", type=Path, default=argparse.SUPPRESS, help="Explicit data directory")
    common_parser.add_argument("--workspace-root", type=Path, default=argparse.SUPPRESS, help="Explicit workspace root")
    common_parser.add_argument("--lease-id", type=str, default=argparse.SUPPRESS, help="Workspace lock lease ID")
    common_parser.add_argument("--conversation-id", type=str, default=argparse.SUPPRESS, help="Workspace lock conversation ID")

    parser = argparse.ArgumentParser(
        parents=[common_parser],
        description="Script-based client for attachment-disposition-log.jsonl and safe cleanup.",
    )

    subparsers = parser.add_subparsers(dest="subcommand")

    # record
    rec_p = subparsers.add_parser("record", parents=[common_parser], help="Record a disposition decision into attachment-disposition-log.jsonl")
    rec_p.add_argument("--input", type=Path, default=None, help="Path to JSON file containing payload (with approval_receipt)")
    rec_p.add_argument("--receipt", type=str, default=None, help="Path to approval receipt JSON file or raw JSON string")
    rec_p.add_argument("--attachment-id", type=str, default=None, help="Attachment ID")
    rec_p.add_argument("--index-entry-sha256", type=str, default=None, help="Canonical SHA-256 hash of quarantine index entry")
    rec_p.add_argument("--decision", type=str, choices=list(sorted(ALLOWED_DECISIONS)), default=None, help="Disposition decision (retain, discard, promote)")
    rec_p.add_argument("--timestamp", type=str, default=None, help="RFC-3339 timestamp (default: now)")
    rec_p.add_argument("--rationale", type=str, default=None, help="Optional bounded rationale")
    rec_p.add_argument("--review-after", type=str, default=None, help="Optional RFC-3339 review date for retain")
    rec_p.add_argument("--candidate-review-hash", type=str, default=None, help="Mandatory 64-hex candidate hash for promote")
    rec_p.add_argument("--promotion-id", type=str, default=None, help="Optional promotion ID for promote")
    rec_p.add_argument("--promotion-status", type=str, default=None, help="Optional promotion status for promote")

    # report
    subparsers.add_parser("report", parents=[common_parser], help="Perform read-only reporting on quarantined attachments")

    # apply-discard
    apply_p = subparsers.add_parser("apply-discard", parents=[common_parser], help="Apply authorized physical deletion for an eligible discard attachment")
    apply_p.add_argument("--attachment-id", type=str, required=True, help="Specific attachment ID to discard (mandatory; bulk apply is forbidden)")
    apply_p.add_argument("--receipt", type=str, required=True, help="Path to apply approval receipt JSON file or raw JSON string")

    args = parser.parse_args()

    # Extract common arguments safely
    is_json: bool = getattr(args, "json", True)
    ws_arg: Path | None = getattr(args, "workspace_root", None)
    dd_arg: Path | None = getattr(args, "data_dir", None)
    lease_id_arg: str | None = getattr(args, "lease_id", None)
    conv_id_arg: str | None = getattr(args, "conversation_id", None)

    # Resolve paths
    ws = ws_arg or (Path(os.environ["WORKSPACE_ROOT"]).resolve() if "WORKSPACE_ROOT" in os.environ else None)
    dd = dd_arg or (resolve_data_dir() if ws is None else ws / "data" / "mail-desk")
    lp = resolve_disposition_log_path(data_dir=dd, workspace_root=ws)
    idx_p = resolve_quarantine_index_path(data_dir=dd, workspace_root=ws)

    # 1. record subcommand
    if args.subcommand == "record":
        receipt_obj = None
        if args.receipt is not None:
            r_path = Path(args.receipt)
            if r_path.is_file():
                try:
                    receipt_obj = json.loads(r_path.read_text(encoding="utf-8"))
                except Exception as err:
                    _emit_error("record", f"Failed to parse receipt file '{args.receipt}': {err}", exc=err, json_output=is_json)
            else:
                try:
                    receipt_obj = json.loads(args.receipt)
                except Exception as err:
                    _emit_error("record", f"Invalid receipt JSON: {err}", exc=err, json_output=is_json)

        payload: dict[str, Any] = {}
        if args.input is not None:
            if not args.input.is_file():
                _emit_error("record", f"Input file '{args.input}' not found", json_output=is_json)
            try:
                payload = json.loads(args.input.read_text(encoding="utf-8"))
            except Exception as err:
                _emit_error("record", f"Failed to parse input file '{args.input}': {err}", exc=err, json_output=is_json)
            if receipt_obj is not None:
                payload["approval_receipt"] = receipt_obj
        else:
            if not args.attachment_id or not args.index_entry_sha256 or not args.decision:
                _emit_error(
                    "record",
                    "Missing required arguments: --attachment-id, --index-entry-sha256, --decision (and --receipt or --input)",
                    json_output=is_json,
                )
            if receipt_obj is None:
                _emit_error(
                    "record",
                    "Missing required approval receipt: provide --receipt <file_or_json>. Raw hashes are not authorization.",
                    json_output=is_json,
                )
            payload = {
                "attachment_id": args.attachment_id,
                "index_entry_sha256": args.index_entry_sha256,
                "decision": args.decision,
                "approval_receipt": receipt_obj,
                "timestamp": args.timestamp or utc_now_iso(),
            }
            if args.rationale is not None:
                payload["rationale"] = args.rationale
            if args.review_after is not None:
                payload["review_after"] = args.review_after
            if args.candidate_review_hash is not None:
                payload["candidate_review_hash"] = args.candidate_review_hash
            if args.promotion_id is not None:
                payload["promotion_id"] = args.promotion_id
            if args.promotion_status is not None:
                payload["promotion_status"] = args.promotion_status

        try:
            res = record_disposition_entry(
                lp,
                payload=payload,
                workspace_root=ws,
                data_dir=dd,
                index_path=idx_p,
                lease_id=lease_id_arg,
                conversation_id=conv_id_arg,
            )
            _emit_success("record", f"Disposition decision {res['status']}", res, json_output=is_json)
        except Exception as err:
            _emit_error("record", f"Failed to record disposition: {err}", exc=err, json_output=is_json)

    # 2. report subcommand
    elif args.subcommand == "report":
        try:
            report_data = report_dispositions(
                workspace_root=ws,
                data_dir=dd,
                index_path=idx_p,
                log_path=lp,
            )
            _emit_success("report", "Read-only disposition report generated", {"report": report_data}, json_output=is_json)
        except Exception as err:
            _emit_error("report", f"Failed to generate disposition report: {err}", exc=err, json_output=is_json)

    # 3. apply-discard subcommand
    elif args.subcommand == "apply-discard":
        apply_receipt_obj = None
        if args.receipt is not None:
            r_path = Path(args.receipt)
            if r_path.is_file():
                try:
                    apply_receipt_obj = json.loads(r_path.read_text(encoding="utf-8"))
                except Exception as err:
                    _emit_error("apply-discard", f"Failed to parse receipt file '{args.receipt}': {err}", exc=err, json_output=is_json)
            else:
                try:
                    apply_receipt_obj = json.loads(args.receipt)
                except Exception as err:
                    _emit_error("apply-discard", f"Invalid receipt JSON: {err}", exc=err, json_output=is_json)

        if not apply_receipt_obj:
            _emit_error(
                "apply-discard",
                "Missing required --receipt mapping. Verifiable apply receipt contract is required.",
                json_output=is_json,
            )

        try:
            apply_res = apply_discard(
                attachment_id=args.attachment_id,
                apply_receipt=apply_receipt_obj,
                workspace_root=ws,
                data_dir=dd,
                index_path=idx_p,
                log_path=lp,
                lease_id=lease_id_arg,
                conversation_id=conv_id_arg,
            )
            _emit_success("apply-discard", f"Discard apply {apply_res['status']}", apply_res, json_output=is_json)
        except Exception as err:
            _emit_error("apply-discard", f"Failed to execute discard apply: {err}", exc=err, json_output=is_json)

    else:
        parser.print_help(sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
