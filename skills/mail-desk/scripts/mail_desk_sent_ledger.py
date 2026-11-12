#!/usr/bin/env python3
"""CLI for Issue #107 sent-ledger outcomes, legacy import and crash recovery.

Commands are deliberately offline-only and operate on a consumer's
``data/mail-desk`` directory:

* ``plan``: read-only dry run with the deterministic approval token.
* ``apply``: appends an approved plan under a verified workspace lease.
* ``recover-promotion``: verifies a durable promotion triad before appending at
  most one missing ledger line.

No mailbox, network or consumer-document access happens here.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from core.envelope import build_error, build_success, emit_json
from core.sent_handoff import SentHandoffError
from core.sent_ledger import (
    SentLedgerError,
    apply_legacy_import,
    build_legacy_import_plan,
    recover_promotion,
)


def _add_common(parser: argparse.ArgumentParser, *, mutating: bool) -> None:
    parser.add_argument("--workspace", default=None, help="verified consumer workspace root")
    parser.add_argument(
        "--data-dir", default="data/mail-desk",
        help="workspace-relative or absolute data/mail-desk directory",
    )
    if mutating:
        parser.add_argument("--workspace-lease-id", default=None,
                            help="invocation-owned workspace lease id")
        parser.add_argument("--workspace-conversation-id", default=None,
                            help="harness conversation id bound to the lease")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mail_desk_sent_ledger.py")
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan", help="read-only legacy-import dry run")
    _add_common(plan, mutating=False)
    plan.add_argument("--inventory", required=True, help="legacy inventory JSONL file")

    apply = subparsers.add_parser("apply", help="append an approved legacy-import plan")
    _add_common(apply, mutating=True)
    apply.add_argument("--inventory", required=True, help="legacy inventory JSONL file")
    apply.add_argument("--approval", required=True, help="dry-run plan_id approval token")

    recover = subparsers.add_parser(
        "recover-promotion",
        help="check a durable promotion triad and append at most one missing ledger line",
    )
    _add_common(recover, mutating=True)
    recover.add_argument("--message-id", required=True)
    recover.add_argument("--vorgang-ref", required=True)
    recover.add_argument("--evid-id", required=True)
    recover.add_argument(
        "--evidence-file", required=True,
        help="JSON array of durable promotion records (or {\"promotions\": [...]})",
    )
    return parser


def _data_dir(args: argparse.Namespace) -> Path:
    raw = Path(args.data_dir).expanduser()
    if not raw.is_absolute():
        if not args.workspace:
            raise ValueError("a relative --data-dir requires --workspace")
        raw = Path(args.workspace).expanduser() / raw
    return raw.resolve()


def _read_promotion_evidence(path: Path) -> list[dict[str, Any]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise SentLedgerError(
            "promotion_recovery_incomplete",
            f"Promotion evidence '{path}' could not be read as JSON "
            f"({type(error).__name__}); nothing was written.",
            {"reason": "evidence_unreadable", "state": "incomplete"},
        ) from error
    if isinstance(document, dict) and set(document) == {"promotions"}:
        document = document["promotions"]
    if not isinstance(document, list) or not all(isinstance(item, dict) for item in document):
        raise SentLedgerError(
            "promotion_recovery_incomplete",
            "Promotion evidence must be a JSON array of objects; nothing was written.",
            {"reason": "evidence_not_a_list", "state": "incomplete"},
        )
    return document


def _command(args: argparse.Namespace) -> dict[str, Any]:
    data_dir = _data_dir(args)
    if args.command == "plan":
        return build_legacy_import_plan(
            data_dir,
            Path(args.inventory).expanduser(),
            workspace_root=Path(args.workspace).expanduser() if args.workspace else None,
        )
    if args.command == "apply":
        return apply_legacy_import(
            data_dir,
            Path(args.inventory).expanduser(),
            approval=args.approval,
            workspace_root=Path(args.workspace).expanduser() if args.workspace else None,
            lease_id=args.workspace_lease_id,
            conversation_id=args.workspace_conversation_id,
        )
    evidence = _read_promotion_evidence(Path(args.evidence_file).expanduser())
    return recover_promotion(
        data_dir,
        message_id=args.message_id,
        vorgang_ref=args.vorgang_ref,
        evid_id=args.evid_id,
        promotion_evidence=evidence,
        workspace_root=Path(args.workspace).expanduser() if args.workspace else None,
        lease_id=args.workspace_lease_id,
        conversation_id=args.workspace_conversation_id,
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = _command(args)
    except SentHandoffError as error:
        envelope = build_error(
            args.command,
            str(error),
            data={"reason_code": error.reason_code, **error.details},
            error_type=type(error).__name__,
            error_details=error.details,
            state="Blocked",
        )
        emit_json(envelope)
        return 2
    except Exception as error:  # noqa: BLE001 - CLI keeps a structured failure envelope
        envelope = build_error(
            args.command,
            f"{type(error).__name__}: {error}",
            data={"reason_code": "sent_ledger_failed"},
            error_type=type(error).__name__,
            state="Failed",
        )
        emit_json(envelope)
        return 1
    envelope = build_success(
        args.command,
        message=str(result.get("status") or result.get("next") or "completed"),
        data=result,
    )
    emit_json(envelope)
    return 0 if result.get("ok", True) else 2


if __name__ == "__main__":
    sys.exit(main())
