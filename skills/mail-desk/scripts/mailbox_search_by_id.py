#!/usr/bin/env python3
"""Search for a Message-ID across mailbox folders using parallel Himalaya calls."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import subprocess
import sys
from typing import Any


def normalize_message_id(value: str) -> str:
    s = value.strip()
    while s.startswith("<") and s.endswith(">") and len(s) >= 2:
        s = s[1:-1].strip()
    return s.lower()


def run_himalaya(args: list[str]) -> str:
    res = subprocess.run(["himalaya"] + args, capture_output=True, text=True, encoding="utf-8", timeout=12)
    if res.returncode != 0:
        raise RuntimeError(f"Himalaya failed: {res.stderr}")
    return res.stdout


def get_folders(account: str | None) -> list[str]:
    cmd_args = ["-o", "json", "folder", "list"]
    if account:
        cmd_args.extend(["-a", account])
    
    stdout = run_himalaya(cmd_args)
    stdout_clean = stdout.strip()
    if "[" in stdout_clean:
        stdout_clean = stdout_clean[stdout_clean.find("["):]
    
    folders_data = json.loads(stdout_clean)
    return [f["name"] for f in folders_data]


def get_envelopes(folder: str, account: str | None, page_size: int) -> list[dict[str, Any]]:
    cmd_args = ["-o", "json", "envelope", "list", "-f", folder, "-s", str(page_size)]
    if account:
        cmd_args.extend(["-a", account])
    
    try:
        stdout = run_himalaya(cmd_args)
    except RuntimeError:
        return []
        
    stdout_clean = stdout.strip()
    if "[" in stdout_clean:
        stdout_clean = stdout_clean[stdout_clean.find("["):]
    else:
        return []
        
    try:
        return json.loads(stdout_clean)
    except Exception:
        return []


def check_envelope_header(folder: str, env_id: str, target_msg_id: str, account: str | None) -> str | None:
    # Do not request json format since message read does not support it
    cmd_args = ["message", "read", "--preview", "-H", "Message-Id", "-f", folder, env_id]
    if account:
        cmd_args.extend(["-a", account])
        
    try:
        stdout = run_himalaya(cmd_args)
        # Parse plain text headers
        m_id = ""
        for line in stdout.splitlines():
            if not line.strip():
                break  # Headers section ended
            if line.lower().startswith("message-id:"):
                m_id = line.split(":", 1)[1].strip()
                break
        
        if m_id and normalize_message_id(m_id) == target_msg_id:
            return env_id
    except Exception:
        pass
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Search for a Message-ID across folders in parallel")
    parser.add_argument("--message-id", required=True, help="Message-ID to search for")
    parser.add_argument("--account", "-a", help="Himalaya account to use")
    parser.add_argument("--folders", help="Comma-separated list of folders to search")
    parser.add_argument("--all-folders", action="store_true", help="Search all folders instead of just INBOX and Sent Items")
    parser.add_argument("--page-size", type=int, default=100, help="Page size for envelope list (default: 100)")
    parser.add_argument("--threads", type=int, default=3, help="Number of threads to use (default: 3)")
    args = parser.parse_args()

    target_msg_id = normalize_message_id(args.message_id)

    try:
        if args.folders:
            folders = [f.strip() for f in args.folders.split(",") if f.strip()]
        elif args.all_folders:
            folders = get_folders(args.account)
        else:
            folders = ["INBOX", "Sent Items"]
    except Exception as e:
        print(json.dumps({"found": False, "error": f"Failed to resolve folders: {e}"}, ensure_ascii=False, indent=2))
        return 1

    found_folder = None
    found_env_id = None

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.threads) as executor:
        for folder in folders:
            # Get envelopes for this folder
            envelopes = get_envelopes(folder, args.account, args.page_size)
            if not envelopes:
                continue

            # Submit tasks to check headers of each envelope in parallel
            futures = {
                executor.submit(check_envelope_header, folder, env["id"], target_msg_id, args.account): env["id"]
                for env in envelopes
            }

            for future in concurrent.futures.as_completed(futures):
                res = future.result()
                if res is not None:
                    import os
                    output = {
                        "found": True,
                        "message_id": target_msg_id,
                        "folder": folder,
                        "envelope_id": res,
                    }
                    print(json.dumps(output, ensure_ascii=False, indent=2))
                    sys.stdout.flush()
                    os._exit(0)
            
            if found_folder:
                break

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
