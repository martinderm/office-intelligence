#!/usr/bin/env python3
"""Move an email in the mailbox and automatically patch final-location-index.json."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Any

_script_dir = Path(__file__).resolve().parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))

from core import (
    build_error,
    build_success,
    emit_json,
    load_final_index,
    normalize_message_id,
    resolve_final_index_path,
    run_himalaya,
    search_mailbox,
    upsert_final_index_entry,
    verify_in_target_folder,
)


ACTION = "move_and_patch"


def _mailbox_data(
    message_id: str | None = None,
    source_folder: str | None = None,
    target_folder: str | None = None,
    source_envelope_id: str | None = None,
    target_envelope_id: str | None = None,
    *,
    copy_completed: bool = False,
    target_verified: bool = False,
    source_delete_attempted: bool = False,
    source_deleted: bool | None = None,
    index_updated: bool = False,
    index_path: Path | None = None,
) -> dict[str, Any]:
    """Keep confirmed mailbox work visible if a later step fails."""
    return {
        "operation": "move_and_patch",
        "message_id": message_id,
        "mailbox": {
            "source_folder": source_folder,
            "target_folder": target_folder,
            "source_envelope_id": source_envelope_id,
            "target_envelope_id": target_envelope_id,
            "copy_completed": copy_completed,
            "target_verified": target_verified,
            "source_delete_attempted": source_delete_attempted,
            "source_deleted": source_deleted,
        },
        "index": {
            "updated": index_updated,
            "path": str(index_path) if index_path else None,
        },
    }


def _emit_error(
    message: str,
    exc: Exception | None = None,
    *,
    state: str = "Failed",
    data: dict[str, Any] | None = None,
    error_type: str | None = None,
    error_details: dict[str, Any] | None = None,
) -> None:
    details = error_details or ({"reason": str(exc)[:1000]} if exc else None)
    emit_json(
        build_error(
            ACTION,
            message,
            data or {"operation": "move_and_patch"},
            state=state,
            error_type=error_type or (type(exc).__name__ if exc else "MoveAndPatchError"),
            error_details=details,
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Move an email and update final-location-index.json")
    parser.add_argument("--message-id", required=True, help="Message-ID of the mail to move")
    parser.add_argument("--target-folder", required=True, help="Target folder (e.g. Projekte/EVOLVE)")
    parser.add_argument("--account", "-a", help="Himalaya account override")
    parser.add_argument("--index", help="Path to final-location-index.json")
    try:
        args = parser.parse_args()
    except SystemExit as exc:
        if exc.code not in (None, 0):
            _emit_error(
                "Invalid command-line arguments.",
                ValueError("argparse rejected the arguments"),
                error_type="ArgumentError",
                data={"operation": "argument_parse"},
            )
            return int(exc.code)
        raise

    target_msg_id: str | None = None
    source_folder: str | None = None
    old_env_id: str | None = None
    new_env_id: str | None = None
    index_path: Path | None = None
    copy_completed = False
    target_verified = False
    source_delete_attempted = False
    source_deleted: bool | None = None
    phase = "lookup"

    try:
        target_msg_id = normalize_message_id(args.message_id)
        index_path = resolve_final_index_path(args.index)
        index_data = load_final_index(index_path)

        item = index_data.get("items", {}).get(target_msg_id)
        mailbox = args.account or "primary"
        if item:
            source_folder = item.get("final_folder")
            old_env_id = item.get("envelope_id")
            if item.get("mailbox"):
                mailbox = item["mailbox"]

        if not source_folder or not old_env_id:
            matches = search_mailbox(message_ids=[target_msg_id], account=args.account)
            if matches:
                source_folder = matches[0].get("folder")
                old_env_id = matches[0].get("envelope_id")
            else:
                _emit_error(
                    "Message ID was not found in the index or mailbox.",
                    state="NotFound",
                    error_type="MessageNotFound",
                    data=_mailbox_data(target_msg_id, target_folder=args.target_folder, index_path=index_path),
                )
                return 2

        phase = "copy"
        run_himalaya(["message", "copy", "-f", source_folder, "-t", args.target_folder, str(old_env_id)], account=args.account)
        copy_completed = True

        phase = "verify"
        new_env_id = verify_in_target_folder(args.target_folder, target_msg_id, account=args.account)
        if not new_env_id:
            _emit_error(
                "Message was copied but could not be verified in the target folder.",
                state="PartialFailure",
                error_type="TargetVerificationError",
                data=_mailbox_data(
                    target_msg_id, source_folder, args.target_folder, str(old_env_id),
                    copy_completed=copy_completed, target_verified=False, index_path=index_path,
                ),
            )
            return 1
        target_verified = True
        new_env_id = str(new_env_id)

        # Preserve the established best-effort source deletion behavior.
        if source_folder != args.target_folder:
            phase = "delete"
            source_delete_attempted = True
            try:
                run_himalaya(["message", "delete", "-f", source_folder, str(old_env_id)], account=args.account)
                source_deleted = True
            except Exception:
                source_deleted = False

        upsert_payload = {
            "message_id": target_msg_id,
            "final_folder": args.target_folder,
            "envelope_id": new_env_id,
            "mailbox": mailbox,
        }
        phase = "index"
        upsert_final_index_entry(index_path, upsert_payload, mode="upsert-final")
    except Exception as exc:  # noqa: BLE001
        partial = copy_completed or target_verified or source_delete_attempted
        messages = {
            "lookup": "Mailbox source lookup failed.",
            "copy": "Mailbox copy failed.",
            "verify": "Target verification failed after mailbox copy.",
            "delete": "Source deletion failed after target verification.",
            "index": "Final-location index update failed after mailbox work.",
        }
        failure_data = _mailbox_data(
            target_msg_id, source_folder, args.target_folder, str(old_env_id) if old_env_id else None,
            new_env_id, copy_completed=copy_completed, target_verified=target_verified,
            source_delete_attempted=source_delete_attempted, source_deleted=source_deleted,
            index_path=index_path,
        )
        failure_data["failure_phase"] = phase
        _emit_error(
            messages.get(phase, "Mailbox move and index update failed."),
            exc,
            state="PartialFailure" if partial else "Failed",
            data=failure_data,
            error_details={"phase": phase, "reason": str(exc)[:1000]},
        )
        return 1

    emit_json(
        build_success(
            ACTION,
            "Mailbox move and final-location index update completed.",
            _mailbox_data(
                target_msg_id, source_folder, args.target_folder, str(old_env_id), new_env_id,
                copy_completed=True, target_verified=True,
                source_delete_attempted=source_delete_attempted, source_deleted=source_deleted,
                index_updated=True, index_path=index_path,
            ),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
