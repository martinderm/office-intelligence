#!/usr/bin/env python3
"""Move an email in the mailbox and automatically patch final-location-index.json."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

_script_dir = Path(__file__).resolve().parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))

from core import (
    load_final_index,
    normalize_message_id,
    resolve_final_index_path,
    run_himalaya,
    search_mailbox,
    upsert_final_index_entry,
    verify_in_target_folder,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Move an email and update final-location-index.json")
    parser.add_argument("--message-id", required=True, help="Message-ID of the mail to move")
    parser.add_argument("--target-folder", required=True, help="Target folder (e.g. Projekte/EVOLVE)")
    parser.add_argument("--account", "-a", help="Himalaya account override")
    parser.add_argument("--index", help="Path to final-location-index.json")
    args = parser.parse_args()

    target_msg_id = normalize_message_id(args.message_id)
    index_path = resolve_final_index_path(args.index)
    index_data = load_final_index(index_path)

    item = index_data.get("items", {}).get(target_msg_id)
    source_folder = None
    old_env_id = None
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
            print(json.dumps({"ok": False, "error": "Message-ID not found in index or mailbox"}, ensure_ascii=False, indent=2))
            return 1

    # Copy to target
    run_himalaya(["message", "copy", "-f", source_folder, "-t", args.target_folder, str(old_env_id)], account=args.account)

    # Verify in target
    new_env_id = verify_in_target_folder(args.target_folder, target_msg_id, account=args.account)
    if not new_env_id:
        print(json.dumps({
            "ok": False,
            "error": f"Message copied, but failed to find new envelope_id in target folder {args.target_folder}"
        }, ensure_ascii=False, indent=2))
        return 1

    # Delete from source if different
    if source_folder != args.target_folder:
        try:
            run_himalaya(["message", "delete", "-f", source_folder, str(old_env_id)], account=args.account)
        except Exception:
            pass

    # Patch index
    upsert_payload = {
        "message_id": target_msg_id,
        "final_folder": args.target_folder,
        "envelope_id": str(new_env_id),
        "mailbox": mailbox,
    }
    upsert_final_index_entry(index_path, upsert_payload, mode="upsert-final")

    output = {
        "ok": True,
        "message_id": target_msg_id,
        "moved": True,
        "source_folder": source_folder,
        "target_folder": args.target_folder,
        "new_envelope_id": str(new_env_id),
        "index_updated": True,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        raise SystemExit(1)
