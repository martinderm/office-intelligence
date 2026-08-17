#!/usr/bin/env python3
"""Search for a Message-ID across mailbox folders using parallel Himalaya calls."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

_script_dir = Path(__file__).resolve().parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))

from core import normalize_message_id, search_mailbox


def main() -> int:
    parser = argparse.ArgumentParser(description="Search for a Message-ID across folders in parallel")
    parser.add_argument("--message-id", required=True, help="Message-ID to search for")
    parser.add_argument("--account", "-a", help="Himalaya account to use")
    parser.add_argument("--folders", help="Comma-separated list of folders to search")
    parser.add_argument("--all-folders", action="store_true", help="Search all folders")
    parser.add_argument("--page-size", type=int, default=100, help="Page size for envelope list (default: 100)")
    parser.add_argument("--threads", type=int, default=4, help="Number of threads to use (default: 4)")
    args = parser.parse_args()

    target_msg_id = normalize_message_id(args.message_id)
    folders = None
    if args.folders:
        folders = [f.strip() for f in args.folders.split(",") if f.strip()]
    elif not args.all_folders:
        folders = ["INBOX", "Sent Items"]

    matches = search_mailbox(
        message_ids=[target_msg_id],
        folders=folders,
        page_size=args.page_size,
        threads=args.threads,
        account=args.account,
    )

    if matches:
        first = matches[0]
        output = {
            "found": True,
            "message_id": target_msg_id,
            "folder": first.get("folder"),
            "envelope_id": first.get("envelope_id"),
            "matches": matches,
        }
    else:
        output = {
            "found": False,
            "message_id": target_msg_id,
        }

    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"found": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        raise SystemExit(1)
