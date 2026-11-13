#!/usr/bin/env python3
"""CLI for Issue #107/#88 sent-ledger outcomes, legacy import, crash recovery and
the consumer consumption adapter.

Commands are deliberately offline-only and operate on a consumer's
``data/mail-desk`` directory:

* ``plan``: read-only dry run with the deterministic approval token.
* ``apply``: appends an approved plan under a verified workspace lease.
* ``recover-promotion``: verifies a durable promotion triad before appending at
  most one missing ledger line.
* ``pending``: read-only view of the open ledger/handoff items (counters and
  truncated hash references; never raw mail content or raw IDs).
* ``record``: appends exactly one consumption outcome for one open item under a
  verified workspace lease; append equals consumption, normalized Message-ID is
  the idempotency key.
* ``consume``: appends every open item the consumer has already evaluated
  (``synthesized``/``no_evidence``) in one lease-bound round; ``not_evaluated``
  items stay open.  ``--dry-run`` validates and projects without a lease or write.
* ``crash-check``: read-only exact triad classification (``unique``/``conflict``/
  ``absent``) without any write or lease.

No mailbox, network or consumer-document access happens here beyond the explicitly
supplied evaluation document of ``consume``; that file is input, not a ledger
report.  ``--json`` is accepted for the documented consumer calls; JSON is the
only output mode.
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
    build_pending_view,
    check_crash_triad,
    consume_evaluated_items,
    read_sent_evaluations,
    record_pending_outcome,
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
    parser.add_argument(
        "--json", action="store_true",
        help="emit the canonical JSON envelope (the only output mode; accepted for "
             "documented consumer calls)",
    )


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

    pending = subparsers.add_parser(
        "pending", help="read-only view of the open ledger/handoff items",
    )
    _add_common(pending, mutating=False)
    pending.add_argument(
        "--account", default=None,
        help="verified workspace account; one foreign open item stops the whole view",
    )

    record = subparsers.add_parser(
        "record", help="append one consumption outcome for one open handoff item",
    )
    _add_common(record, mutating=True)
    selector = record.add_mutually_exclusive_group(required=True)
    selector.add_argument("--message-id", default=None,
                          help="raw Message-ID of the open item")
    selector.add_argument("--item-ref", default=None,
                          help="stable sha256 item reference from the pending view")
    record.add_argument("--outcome", required=True,
                        choices=("synthesized", "no_evidence"))
    record.add_argument("--vorgang-ref", default=None,
                        help="exact Vorgang reference (required for synthesized)")
    record.add_argument("--evid-id", default=None,
                        help="optional EVID anchor (synthesized only)")
    record.add_argument("--reason-code", default=None,
                        help="documented reason code (required for no_evidence)")
    record.add_argument(
        "--account", default=None,
        help="verified workspace account; one foreign open item stops the round",
    )

    consume = subparsers.add_parser(
        "consume",
        help="append every already evaluated open handoff item in one lease-bound round",
    )
    _add_common(consume, mutating=True)
    consume.add_argument(
        "--evaluations", required=True,
        help="consumer evaluation JSON (schema_version 1, kind mail-desk.sent-evaluation)",
    )
    consume.add_argument(
        "--account", default=None,
        help="verified workspace account; one foreign open item stops the whole round",
    )
    consume.add_argument(
        "--dry-run", action="store_true",
        help="validate and project only; no lease is taken and nothing is written",
    )

    crash = subparsers.add_parser(
        "crash-check",
        help="read-only crash classification of one exact message_id/Vorgang/EVID triad",
    )
    _add_common(crash, mutating=False)
    crash.add_argument("--message-id", required=True)
    crash.add_argument("--vorgang-ref", required=True)
    crash.add_argument("--evid-id", required=True)
    crash.add_argument(
        "--evidence-file", default=None,
        help="optional complete durable promotion evidence (JSON array or "
             "{\"promotions\": [...]})",
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
    workspace = Path(args.workspace).expanduser() if args.workspace else None
    if args.command == "plan":
        return build_legacy_import_plan(
            data_dir,
            Path(args.inventory).expanduser(),
            workspace_root=workspace,
        )
    if args.command == "apply":
        return apply_legacy_import(
            data_dir,
            Path(args.inventory).expanduser(),
            approval=args.approval,
            workspace_root=workspace,
            lease_id=args.workspace_lease_id,
            conversation_id=args.workspace_conversation_id,
        )
    if args.command == "pending":
        return build_pending_view(
            data_dir,
            workspace_root=workspace,
            requested_account=args.account,
        )
    if args.command == "record":
        return record_pending_outcome(
            data_dir,
            outcome=args.outcome,
            message_id=args.message_id,
            item_ref=args.item_ref,
            vorgang_ref=args.vorgang_ref,
            evid_id=args.evid_id,
            reason_code=args.reason_code,
            requested_account=args.account,
            workspace_root=workspace,
            lease_id=args.workspace_lease_id,
            conversation_id=args.workspace_conversation_id,
        )
    if args.command == "consume":
        return consume_evaluated_items(
            data_dir,
            evaluations=read_sent_evaluations(Path(args.evaluations).expanduser()),
            dry_run=bool(args.dry_run),
            requested_account=args.account,
            workspace_root=workspace,
            lease_id=args.workspace_lease_id,
            conversation_id=args.workspace_conversation_id,
        )
    if args.command == "crash-check":
        evidence = (
            _read_promotion_evidence(Path(args.evidence_file).expanduser())
            if args.evidence_file
            else None
        )
        return check_crash_triad(
            data_dir,
            message_id=args.message_id,
            vorgang_ref=args.vorgang_ref,
            evid_id=args.evid_id,
            promotion_evidence=evidence,
            workspace_root=workspace,
        )
    evidence = _read_promotion_evidence(Path(args.evidence_file).expanduser())
    return recover_promotion(
        data_dir,
        message_id=args.message_id,
        vorgang_ref=args.vorgang_ref,
        evid_id=args.evid_id,
        promotion_evidence=evidence,
        workspace_root=workspace,
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
