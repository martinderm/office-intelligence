#!/usr/bin/env python3
"""Move an email in the mailbox and automatically patch final-location-index.json."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


def normalize_message_id(value: str) -> str:
    s = value.strip()
    while s.startswith("<") and s.endswith(">") and len(s) >= 2:
        s = s[1:-1].strip()
    return s.lower()


def run_himalaya(args: list[str]) -> str:
    res = subprocess.run(["himalaya"] + args, capture_output=True, text=True, encoding="utf-8", timeout=15)
    if res.returncode != 0:
        raise RuntimeError(f"Himalaya failed: {res.stderr}")
    return res.stdout


def default_index_path() -> Path:
    env_index = os.environ.get("MAIL_DESK_FINAL_INDEX_PATH", "").strip()
    if env_index:
        return Path(env_index).expanduser()

    env_data_dir = os.environ.get("MAIL_DESK_DATA_DIR", "").strip()
    if env_data_dir:
        return Path(env_data_dir).expanduser() / "final-location-index.json"

    preferred = Path.cwd() / "data" / "mail-desk" / "final-location-index.json"
    legacy = Path(__file__).resolve().parents[3] / "data" / "mail-desk" / "final-location-index.json"

    if preferred.exists() or preferred.parent.exists():
        return preferred
    if legacy.exists() or legacy.parent.exists():
        return legacy

    raise FileNotFoundError("Could not resolve final-location-index.json.")


def load_index(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": 1, "updated_at": None, "items": {}}
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return data


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


def verify_in_target(folder: str, target_msg_id: str, account: str | None, threads: int) -> str | None:
    cmd_args = ["-o", "json", "envelope", "list", "-f", folder, "-s", "100"]
    if account:
        cmd_args.extend(["-a", account])
    try:
        stdout = run_himalaya(cmd_args)
        stdout_clean = stdout.strip()
        if "[" in stdout_clean:
            stdout_clean = stdout_clean[stdout_clean.find("["):]
        envelopes = json.loads(stdout_clean)
    except Exception:
        return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as executor:
        futures = {
            executor.submit(check_envelope_header, folder, env["id"], target_msg_id, account): env["id"]
            for env in envelopes
        }
        for future in concurrent.futures.as_completed(futures):
            res = future.result()
            if res is not None:
                return res
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Move an email and update final-location-index.json")
    parser.add_argument("--message-id", required=True, help="Message-ID of the mail to move")
    parser.add_argument("--target-folder", required=True, help="Target folder (e.g. Projekte/EVOLVE)")
    parser.add_argument("--account", "-a", help="Himalaya account override")
    parser.add_argument("--index", help="Path to final-location-index.json")
    parser.add_argument("--threads", type=int, default=3, help="Number of threads for verification (default: 3)")
    args = parser.parse_args()

    target_msg_id = normalize_message_id(args.message_id)

    # 1. Resolve current location
    try:
        index_path = Path(args.index) if args.index else default_index_path()
        index_data = load_index(index_path)
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"Failed to load index: {e}"}, ensure_ascii=False, indent=2))
        return 1

    item = index_data.get("items", {}).get(target_msg_id)
    source_folder = None
    old_env_id = None
    mailbox = args.account or os.environ.get("HIMALAYA_MAILBOX", "BOKU-MARTIN")

    if item:
        source_folder = item.get("final_folder")
        old_env_id = item.get("envelope_id")
        if item.get("mailbox"):
            mailbox = item["mailbox"]
    
    if not source_folder or not old_env_id:
        # Fallback: search mailbox for the Message-ID using the search logic
        print(f"Message-ID {target_msg_id} not found in index. Searching mailbox folders...", file=sys.stderr)
        search_script = Path(__file__).parent / "mailbox_search_by_id.py"
        search_args = ["python", str(search_script), "--message-id", target_msg_id]
        if args.account:
            search_args.extend(["-a", args.account])
        
        res = subprocess.run(search_args, capture_output=True, text=True, encoding="utf-8")
        try:
            search_res = json.loads(res.stdout)
            if search_res.get("found"):
                source_folder = search_res.get("folder")
                old_env_id = search_res.get("envelope_id")
            else:
                print(json.dumps({"ok": False, "error": "Message-ID not found in index or mailbox"}, ensure_ascii=False, indent=2))
                return 1
        except Exception as e:
            print(json.dumps({"ok": False, "error": f"Mailbox search failed: {e}. Output: {res.stdout}"}, ensure_ascii=False, indent=2))
            return 1

    # 2. Perform copy (move)
    copy_args = ["message", "copy", args.target_folder, old_env_id, "-f", source_folder]
    if args.account:
        copy_args.extend(["-a", args.account])
    
    try:
        run_himalaya(copy_args)
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"Failed to copy message: {e}"}, ensure_ascii=False, indent=2))
        return 1

    # 3. Verify and get new envelope ID
    new_env_id = verify_in_target(args.target_folder, target_msg_id, args.account, args.threads)
    if not new_env_id:
        print(json.dumps({
            "ok": False,
            "error": f"Message copied, but failed to find new envelope_id in target folder {args.target_folder}"
        }, ensure_ascii=False, indent=2))
        return 1

    # 4. Patch final-location-index
    upsert_script = Path(__file__).parent / "final_index_upsert.py"
    upsert_payload = {
        "message_id": target_msg_id,
        "final_folder": args.target_folder,
        "envelope_id": new_env_id,
        "mailbox": mailbox
    }
    
    upsert_proc = subprocess.Popen(
        ["python", str(upsert_script), "--mode", "upsert-final", "--index", str(index_path), "--stdin"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8"
    )
    stdout, stderr = upsert_proc.communicate(input=json.dumps(upsert_payload))
    
    if upsert_proc.returncode != 0:
        print(json.dumps({
            "ok": False,
            "error": f"Index update failed: {stderr.strip() or stdout.strip()}"
        }, ensure_ascii=False, indent=2))
        return 1

    output = {
        "ok": True,
        "message_id": target_msg_id,
        "moved": True,
        "source_folder": source_folder,
        "target_folder": args.target_folder,
        "new_envelope_id": new_env_id,
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
