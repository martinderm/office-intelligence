#!/usr/bin/env python3
"""Resolve and archive needs-reply and pending-review cases in data/mail-desk/."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def get_iso_week_folder() -> str:
    now = datetime.now(timezone.utc)
    iso_year, iso_week, _ = now.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def normalize_message_id(value: str) -> str:
    s = value.strip()
    while s.startswith("<") and s.endswith(">") and len(s) >= 2:
        s = s[1:-1].strip()
    return s.lower()


def default_data_dir() -> Path:
    env_dir = os.environ.get("MAIL_DESK_DATA_DIR", "").strip()
    if env_dir:
        return Path(env_dir).expanduser()
    
    preferred = Path.cwd() / "data" / "mail-desk"
    legacy = Path(__file__).resolve().parents[3] / "data" / "mail-desk"
    
    if preferred.exists() or preferred.parent.exists():
        return preferred
    if legacy.exists() or legacy.parent.exists():
        return legacy
        
    raise FileNotFoundError(
        "Could not resolve data/mail-desk directory. "
        "Use --data-dir or set MAIL_DESK_DATA_DIR."
    )


def process_file(
    filepath: Path,
    target_msg_id: str,
    status: str,
    resolution: str,
    resolved_by: str | None,
    archive_dir: Path
) -> dict[str, Any] | None:
    if not filepath.exists():
        return None

    lines = []
    found_item = None

    with filepath.open("r", encoding="utf-8") as f:
        for line in f:
            line_str = line.strip()
            if not line_str:
                continue
            try:
                item = json.loads(line_str)
                msg_id_in_item = item.get("message_id", "")
                if normalize_message_id(msg_id_in_item) == target_msg_id:
                    found_item = item
                else:
                    lines.append(line_str)
            except Exception:
                lines.append(line_str)

    if found_item is not None:
        # Update the found item
        found_item["status"] = status
        found_item["resolution"] = resolution
        found_item["closed_at"] = utc_now_iso()
        if resolved_by:
            found_item["resolved_by_message_id"] = normalize_message_id(resolved_by)

        # Ensure archive dir exists
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_file = archive_dir / filepath.name

        # Write to archive
        with archive_file.open("a", encoding="utf-8") as af:
            af.write(json.dumps(found_item, ensure_ascii=False) + "\n")

        # Rewrite active file without the resolved item
        with filepath.open("w", encoding="utf-8", newline="\n") as f:
            for l in lines:
                f.write(l + "\n")
                
        return found_item

    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Resolve and archive mail-desk cases")
    parser.add_argument("--message-id", required=True, help="Message-ID of the case to resolve")
    parser.add_argument("--status", default="resolved", help="Resolution status (default: resolved)")
    parser.add_argument("--resolution", required=True, help="Resolution explanation")
    parser.add_argument("--resolved-by-message-id", help="Optional Message-ID of the reply mail")
    parser.add_argument("--data-dir", help="Path to data/mail-desk directory")
    args = parser.parse_args()

    target_msg_id = normalize_message_id(args.message_id)
    try:
        data_dir = Path(args.data_dir) if args.data_dir else default_data_dir()
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False, indent=2))
        return 1

    active_files = [
        data_dir / "replies-needed.jsonl",
        data_dir / "pending-review.jsonl"
    ]

    archive_week = get_iso_week_folder()
    archive_dir = data_dir / "archive" / archive_week

    resolved_item = None
    source_file = None

    for active_file in active_files:
        item = process_file(
            active_file,
            target_msg_id,
            args.status,
            args.resolution,
            args.resolved_by_message_id,
            archive_dir
        )
        if item:
            resolved_item = item
            source_file = active_file.name
            break

    if resolved_item:
        output = {
            "ok": True,
            "resolved": True,
            "message_id": target_msg_id,
            "source_file": source_file,
            "archived_to": str(archive_dir / source_file),
            "item": resolved_item,
        }
    else:
        output = {
            "ok": True,
            "resolved": False,
            "message_id": target_msg_id,
            "note": "Message-ID not found in active replies-needed.jsonl or pending-review.jsonl",
        }

    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        raise SystemExit(1)
