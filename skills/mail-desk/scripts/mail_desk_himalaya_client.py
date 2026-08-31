#!/usr/bin/env python3
"""Deterministic Himalaya client and JSON batch wrapper for mail-desk.

Executes direct Himalaya IMAP operations with automatic TLS retry,
transient error recovery, structured JSON envelopes, and JSON manifest approval.

Usage:
    # Via JSON input file (auto-deleted on success):
    python3 scripts/mail_desk_himalaya_client.py --input data/mail-desk/himalaya-op.json

    # Via direct CLI commands:
    python3 scripts/mail_desk_himalaya_client.py list-folders --json
    python3 scripts/mail_desk_himalaya_client.py list-envelopes -f INBOX -s 20 --json
    python3 scripts/mail_desk_himalaya_client.py read -f INBOX 7195 --json
    python3 scripts/mail_desk_himalaya_client.py copy -f INBOX -t "Projekte/USAGE-NG" 7195 --json
    python3 scripts/mail_desk_himalaya_client.py move -f INBOX -t "Projekte/USAGE-NG" 7195 --json
    python3 scripts/mail_desk_himalaya_client.py delete -f INBOX 7195 --json
    python3 scripts/mail_desk_himalaya_client.py search -q "USAGE-NG" --json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from core.common import normalize_message_id
from core.himalaya import (
    get_single_email_details,
    run_himalaya,
    search_mailbox,
    verify_in_target_folder,
)


def parse_args() -> argparse.Namespace:
    common_parent = argparse.ArgumentParser(add_help=False)
    common_parent.add_argument(
        "--account",
        "-a",
        type=str,
        default=None,
        help="Himalaya account name (optional).",
    )
    common_parent.add_argument(
        "--json",
        action="store_true",
        help="Output in standard JSON envelope format.",
    )

    parser = argparse.ArgumentParser(
        description="Unified deterministic Himalaya IMAP client and JSON wrapper.",
        parents=[common_parent],
    )
    parser.add_argument(
        "--input",
        "-i",
        type=str,
        default=None,
        help="Path to JSON operation manifest (executes manifest and deletes file on success).",
    )

    subparsers = parser.add_subparsers(dest="subcommand", help="Direct operation subcommands")

    # list-folders
    subparsers.add_parser("list-folders", parents=[common_parent], help="List all available IMAP mailbox folders.")

    # list-envelopes
    p_env = subparsers.add_parser("list-envelopes", parents=[common_parent], help="List envelope summaries from a folder.")
    p_env.add_argument("-f", "--folder", type=str, default="INBOX", help="IMAP folder name.")
    p_env.add_argument("-s", "--page-size", type=int, default=50, help="Number of envelopes to fetch.")
    p_env.add_argument("-q", "--query", type=str, default=None, help="Optional search filter.")

    # read
    p_read = subparsers.add_parser("read", parents=[common_parent], help="Read message headers and body preview.")
    p_read.add_argument("envelope_id", type=str, help="Envelope ID to read.")
    p_read.add_argument("-f", "--folder", type=str, default="INBOX", help="IMAP folder name.")
    p_read.add_argument("--preview-lines", type=int, default=50, help="Body preview line limit.")

    # copy
    p_copy = subparsers.add_parser("copy", parents=[common_parent], help="Copy a message from source to target folder.")
    p_copy.add_argument("envelope_id", type=str, help="Envelope ID in source folder.")
    p_copy.add_argument("-f", "--source-folder", type=str, default="INBOX", help="Source folder.")
    p_copy.add_argument("-t", "--target-folder", type=str, required=True, help="Target folder.")

    # move
    p_move = subparsers.add_parser("move", parents=[common_parent], help="Copy and delete (move) a message with verification.")
    p_move.add_argument("envelope_id", type=str, help="Envelope ID in source folder.")
    p_move.add_argument("-f", "--source-folder", type=str, default="INBOX", help="Source folder.")
    p_move.add_argument("-t", "--target-folder", type=str, required=True, help="Target folder.")

    # delete
    p_del = subparsers.add_parser("delete", parents=[common_parent], help="Delete a message from a folder.")
    p_del.add_argument("envelope_id", type=str, help="Envelope ID to delete.")
    p_del.add_argument("-f", "--folder", type=str, default="INBOX", help="IMAP folder name.")

    # search
    p_search = subparsers.add_parser("search", parents=[common_parent], help="Search messages across folders.")
    p_search.add_argument("-q", "--query", type=str, default="", help="Text search query.")
    p_search.add_argument("-m", "--message-id", type=str, default=None, help="Specific Message-ID.")
    p_search.add_argument("-f", "--folders", nargs="*", default=None, help="Folders to search in.")

    return parser.parse_args()


# ==============================================================================
# Direct Operations
# ==============================================================================

def op_list_folders(account: str | None = None) -> list[dict[str, Any]]:
    out = run_himalaya(["folder", "list", "-o", "json"], account=account)
    if "[" in out:
        out = out[out.find("["):]
    return json.loads(out)


def op_list_envelopes(
    folder: str = "INBOX",
    page_size: int = 50,
    query: str | None = None,
    account: str | None = None,
) -> list[dict[str, Any]]:
    cmd = ["-o", "json", "envelope", "list", "-f", folder, "-s", str(page_size)]
    if query:
        cmd.extend(["subject", query])
    out = run_himalaya(cmd, account=account)
    if "[" in out:
        out = out[out.find("["):]
    return json.loads(out)


def op_read_message(
    envelope_id: str,
    folder: str = "INBOX",
    preview_lines: int = 50,
    account: str | None = None,
) -> dict[str, Any]:
    return get_single_email_details(
        env_id=envelope_id,
        folder=folder,
        account=account,
        preview_lines=preview_lines,
    )


def op_copy_message(
    envelope_id: str,
    source_folder: str,
    target_folder: str,
    account: str | None = None,
) -> dict[str, Any]:
    # 1. Fetch message details before copy
    details = get_single_email_details(envelope_id, folder=source_folder, account=account)
    mid = details.get("message_id")
    subj = details.get("subject", "")

    # 2. Check if already in target folder
    new_id = None
    if mid:
        new_id = verify_in_target_folder(target_folder, mid, subject=subj, account=account)

    if not new_id:
        run_himalaya(["message", "copy", target_folder, str(envelope_id), "-f", source_folder], account=account)
        if mid:
            new_id = verify_in_target_folder(target_folder, mid, subject=subj, account=account)

    return {
        "source_folder": source_folder,
        "target_folder": target_folder,
        "source_envelope_id": str(envelope_id),
        "target_envelope_id": str(new_id) if new_id else None,
        "message_id": mid,
        "subject": subj,
        "verified": bool(new_id),
    }


def op_move_message(
    envelope_id: str,
    source_folder: str,
    target_folder: str,
    account: str | None = None,
) -> dict[str, Any]:
    copy_res = op_copy_message(
        envelope_id=envelope_id,
        source_folder=source_folder,
        target_folder=target_folder,
        account=account,
    )

    if copy_res.get("verified") or copy_res.get("target_envelope_id"):
        # Resolve live source envelope id to prevent shift errors
        mid = copy_res.get("message_id")
        subj = copy_res.get("subject", "")
        live_src_id = None
        if mid:
            live_src_id = verify_in_target_folder(source_folder, mid, subject=subj, account=account)
        del_id = live_src_id or str(envelope_id)

        try:
            run_himalaya(["message", "delete", del_id, "-f", source_folder], account=account)
            copy_res["deleted_from_source"] = True
        except Exception as e:
            copy_res["deleted_from_source"] = False
            copy_res["delete_error"] = str(e)
    else:
        copy_res["deleted_from_source"] = False
        copy_res["delete_error"] = "Verification in target folder failed; aborting delete from source."

    return copy_res


def op_delete_message(
    envelope_id: str,
    folder: str = "INBOX",
    account: str | None = None,
) -> dict[str, Any]:
    run_himalaya(["message", "delete", str(envelope_id), "-f", folder], account=account)
    return {
        "folder": folder,
        "envelope_id": str(envelope_id),
        "deleted": True,
    }


def op_search(
    query: str = "",
    message_id: str | None = None,
    folders: list[str] | None = None,
    account: str | None = None,
) -> list[dict[str, Any]]:
    mids = [message_id] if message_id else None
    return search_mailbox(
        query=query,
        message_ids=mids,
        folders=folders,
        account=account,
    )


# ==============================================================================
# Manifest Mode
# ==============================================================================

def execute_manifest(manifest_path: Path, account: str | None = None) -> dict[str, Any]:
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")

    with manifest_path.open("r", encoding="utf-8") as f:
        mdata = json.load(f)

    ops = mdata.get("operations") or mdata.get("items") or []
    if isinstance(mdata, list):
        ops = mdata

    results = []
    all_succeeded = True
    acc = account or mdata.get("account")

    for i, op in enumerate(ops):
        action = op.get("action", "").lower().replace("-", "_")
        try:
            if action in ["list_folders", "folders"]:
                res = op_list_folders(account=acc)
            elif action in ["list_envelopes", "envelopes", "list"]:
                res = op_list_envelopes(
                    folder=op.get("folder", "INBOX"),
                    page_size=int(op.get("page_size", 50)),
                    query=op.get("query"),
                    account=acc,
                )
            elif action in ["read", "read_message"]:
                res = op_read_message(
                    envelope_id=str(op.get("envelope_id", op.get("id"))),
                    folder=op.get("folder", "INBOX"),
                    preview_lines=int(op.get("preview_lines", 50)),
                    account=acc,
                )
            elif action == "copy":
                res = op_copy_message(
                    envelope_id=str(op.get("envelope_id", op.get("id"))),
                    source_folder=op.get("source_folder", op.get("from", "INBOX")),
                    target_folder=op.get("target_folder", op.get("to")),
                    account=acc,
                )
            elif action in ["move", "copy_as_move"]:
                res = op_move_message(
                    envelope_id=str(op.get("envelope_id", op.get("id"))),
                    source_folder=op.get("source_folder", op.get("from", "INBOX")),
                    target_folder=op.get("target_folder", op.get("to")),
                    account=acc,
                )
            elif action == "delete":
                res = op_delete_message(
                    envelope_id=str(op.get("envelope_id", op.get("id"))),
                    folder=op.get("folder", "INBOX"),
                    account=acc,
                )
            elif action == "search":
                res = op_search(
                    query=op.get("query", ""),
                    message_id=op.get("message_id"),
                    folders=op.get("folders"),
                    account=acc,
                )
            else:
                raise ValueError(f"Unknown operation action: {action}")

            results.append({"op_index": i, "action": action, "success": True, "result": res})
        except Exception as e:
            all_succeeded = False
            results.append({"op_index": i, "action": action, "success": False, "error": str(e)})

    # Handle deletion on success
    delete_on_success = bool(mdata.get("delete_input_on_success", True)) if isinstance(mdata, dict) else True
    input_deleted = False
    if all_succeeded and delete_on_success:
        try:
            manifest_path.unlink()
            input_deleted = True
        except Exception:
            pass

    return {
        "all_succeeded": all_succeeded,
        "total_operations": len(ops),
        "results": results,
        "input_file_deleted": input_deleted,
    }


# ==============================================================================
# Main CLI Entrypoint
# ==============================================================================

def main() -> int:
    args = parse_args()

    try:
        if args.input:
            manifest_path = Path(args.input)
            data = execute_manifest(manifest_path, account=args.account)
            status = "success" if data.get("all_succeeded") else "partial"
            envelope = {"status": status, "data": data, "error": None}
            print(json.dumps(envelope, ensure_ascii=False, indent=2))
            return 0 if data.get("all_succeeded") else 1

        if not args.subcommand:
            print("Error: No command or --input manifest specified. Run with --help.", file=sys.stderr)
            return 1

        sub = args.subcommand.replace("-", "_")
        result: Any = None

        if sub == "list_folders":
            result = op_list_folders(account=args.account)
        elif sub == "list_envelopes":
            result = op_list_envelopes(
                folder=args.folder,
                page_size=args.page_size,
                query=args.query,
                account=args.account,
            )
        elif sub == "read":
            result = op_read_message(
                envelope_id=args.envelope_id,
                folder=args.folder,
                preview_lines=args.preview_lines,
                account=args.account,
            )
        elif sub == "copy":
            result = op_copy_message(
                envelope_id=args.envelope_id,
                source_folder=args.source_folder,
                target_folder=args.target_folder,
                account=args.account,
            )
        elif sub == "move":
            result = op_move_message(
                envelope_id=args.envelope_id,
                source_folder=args.source_folder,
                target_folder=args.target_folder,
                account=args.account,
            )
        elif sub == "delete":
            result = op_delete_message(
                envelope_id=args.envelope_id,
                folder=args.folder,
                account=args.account,
            )
        elif sub == "search":
            result = op_search(
                query=args.query,
                message_id=args.message_id,
                folders=args.folders,
                account=args.account,
            )

        envelope = {"status": "success", "data": result, "error": None}
        if args.json:
            print(json.dumps(envelope, ensure_ascii=False, indent=2))
        else:
            if isinstance(result, list):
                print(f"Total results: {len(result)}")
                for item in result[:20]:
                    print(item)
            else:
                print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    except Exception as e:
        envelope = {"status": "error", "data": None, "error": str(e)}
        print(json.dumps(envelope, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
