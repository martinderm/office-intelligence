#!/usr/bin/env python3
"""Lookup helper for data/mail-desk/final-location-index.json."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any


def normalize_message_id(value: str) -> str:
    s = value.strip()
    return s.lower()


def validate_lookup_message_id(value: str) -> str:
    s = value.strip()
    if not s:
        raise ValueError("--message-id must not be empty")
    if s.startswith("<") or s.endswith(">"):
        raise ValueError(
            "--message-id must be passed in normalized form without angle brackets, "
            "e.g. 4DB7DEC0-E705-4F5C-85E7-0BA35CBDF068@boku.ac.at"
        )
    return normalize_message_id(s)


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


def load_index(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": 1, "updated_at": None, "items": {}}
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("Index root must be a JSON object")
    if not isinstance(data.get("items", {}), dict):
        raise ValueError("Index field 'items' must be an object")
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description="Lookup a message in final-location-index.json")
    parser.add_argument(
        "--message-id",
        required=True,
        help="Normalized Message-ID without angle brackets",
    )
    parser.add_argument("--index", help="Path to final-location-index.json")
    args = parser.parse_args()

    message_id_norm = validate_lookup_message_id(args.message_id)
    index_path = Path(args.index) if args.index else default_index_path()

    data = load_index(index_path)
    item = data.get("items", {}).get(message_id_norm)

    if item is None:
        output = {
            "found": False,
            "message_id": message_id_norm,
        }
    else:
        output = {
            "found": True,
            "message_id": message_id_norm,
            "item": item,
        }

    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"found": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        raise SystemExit(1)
