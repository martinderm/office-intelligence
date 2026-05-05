#!/usr/bin/env python3
"""Upsert/patch helper for data/mail-desk/final-location-index.json."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ALLOWED_FIELDS = {
    "message_id",
    "mailbox",
    "final_folder",
    "envelope_id",
    "in_reply_to",
    "references",
    "updated_at",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_message_id(value: str) -> str:
    s = value.strip()
    while s.startswith("<") and s.endswith(">") and len(s) >= 2:
        s = s[1:-1].strip()
    return s.lower()


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

    raise FileNotFoundError(
        "Could not resolve final-location-index.json. "
        "Use --index, set MAIL_DESK_FINAL_INDEX_PATH, or set MAIL_DESK_DATA_DIR."
    )


def validate_payload(payload: dict[str, Any], mode: str) -> None:
    if "message_id" not in payload or not str(payload["message_id"]).strip():
        raise ValueError("'message_id' is required")

    unknown = set(payload.keys()) - ALLOWED_FIELDS
    if unknown:
        raise ValueError(f"Unsupported fields: {', '.join(sorted(unknown))}")

    if mode == "upsert-final":
        for field in ("final_folder", "envelope_id"):
            if field not in payload or not str(payload[field]).strip():
                raise ValueError(f"'{field}' is required in mode=upsert-final")

    if "references" in payload and not isinstance(payload["references"], list):
        raise ValueError("'references' must be an array when provided")


def ensure_index(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": 1, "updated_at": None, "items": {}}
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("Index root must be a JSON object")
    if not isinstance(data.get("items", {}), dict):
        raise ValueError("Index field 'items' must be an object")
    return data


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    temp_path.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Upsert or patch final-location-index.json")
    parser.add_argument("--mode", choices=["upsert-final", "patch"], default="patch")
    parser.add_argument("--stdin", action="store_true", required=True, help="Read one JSON object from stdin")
    parser.add_argument("--index", help="Path to final-location-index.json")
    args = parser.parse_args()

    raw = sys.stdin.read().strip()
    if not raw:
        raise ValueError("stdin JSON payload is empty")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("stdin payload must be a JSON object")

    validate_payload(payload, args.mode)

    index_path = Path(args.index) if args.index else default_index_path()
    data = ensure_index(index_path)
    items = data.setdefault("items", {})

    msg_norm = normalize_message_id(str(payload["message_id"]))
    existing = items.get(msg_norm)

    if args.mode == "patch" and existing is None:
        raise ValueError("Cannot patch non-existing entry. Use mode=upsert-final first.")

    created = existing is None
    entry = dict(existing or {})

    # Always keep normalized message_id in entry
    entry["message_id"] = msg_norm

    for key, value in payload.items():
        if key == "message_id":
            continue
        entry[key] = value

    if "updated_at" not in payload or not str(payload.get("updated_at", "")).strip():
        entry["updated_at"] = utc_now_iso()

    items[msg_norm] = entry
    data["updated_at"] = utc_now_iso()

    atomic_write_json(index_path, data)

    result = {
        "ok": True,
        "mode": args.mode,
        "created": created,
        "message_id": msg_norm,
        "index": str(index_path),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        raise SystemExit(1)
