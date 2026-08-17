"""Action logging, replies-needed and review cases management."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .common import get_iso_week_folder, normalize_message_id, utc_now_iso


def append_action_log_entry(data_dir: Path, entry: dict[str, Any]) -> None:
    """Append entry to data/mail-desk/action-log.jsonl."""
    log_path = data_dir / "action-log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def append_replies_needed_entry(data_dir: Path, entry: dict[str, Any]) -> None:
    """Append entry to data/mail-desk/replies-needed.jsonl."""
    replies_path = data_dir / "replies-needed.jsonl"
    replies_path.parent.mkdir(parents=True, exist_ok=True)
    with replies_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def resolve_case(
    data_dir: Path,
    message_id: str,
    status: str = "resolved",
    resolution: str = "",
    resolved_by: str | None = None,
) -> dict[str, Any]:
    """Resolve and archive a case from replies-needed.jsonl or pending-review.jsonl."""
    target_msg_id = normalize_message_id(message_id)
    active_files = [
        data_dir / "replies-needed.jsonl",
        data_dir / "pending-review.jsonl",
    ]
    archive_week = get_iso_week_folder()
    archive_dir = data_dir / "archive" / archive_week

    resolved_item = None
    source_file = None

    for active_file in active_files:
        if not active_file.exists():
            continue

        lines: list[str] = []
        found = None

        with active_file.open("r", encoding="utf-8") as f:
            for line in f:
                line_str = line.strip()
                if not line_str:
                    continue
                try:
                    item = json.loads(line_str)
                    mid = item.get("message_id", "")
                    if normalize_message_id(mid) == target_msg_id:
                        found = item
                    else:
                        lines.append(line_str)
                except Exception:
                    lines.append(line_str)

        if found is not None:
            found["status"] = status
            found["resolution"] = resolution
            found["closed_at"] = utc_now_iso()
            if resolved_by:
                found["resolved_by_message_id"] = normalize_message_id(resolved_by)

            archive_dir.mkdir(parents=True, exist_ok=True)
            archive_file = archive_dir / active_file.name

            with archive_file.open("a", encoding="utf-8") as af:
                af.write(json.dumps(found, ensure_ascii=False) + "\n")

            with active_file.open("w", encoding="utf-8", newline="\n") as f:
                for l in lines:
                    f.write(l + "\n")

            resolved_item = found
            source_file = active_file.name
            break

    if resolved_item:
        return {
            "ok": True,
            "resolved": True,
            "message_id": target_msg_id,
            "source_file": source_file,
            "archived_to": str(archive_dir / (source_file or "")),
            "item": resolved_item,
        }

    return {
        "ok": True,
        "resolved": False,
        "message_id": target_msg_id,
        "note": "Message-ID not found in active replies-needed.jsonl or pending-review.jsonl",
    }
