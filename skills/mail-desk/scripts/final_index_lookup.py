#!/usr/bin/env python3
"""Lookup helper for data/mail-desk/final-location-index.json."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def normalize_message_id(value: str) -> str:
    s = value.strip()
    while s.startswith("<") and s.endswith(">") and len(s) >= 2:
        s = s[1:-1].strip()
    return s.lower()


def default_index_path() -> Path:
    # skills/mail-desk/scripts -> workspace/project root is parents[3]
    return Path(__file__).resolve().parents[3] / "data" / "mail-desk" / "final-location-index.json"


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
    parser.add_argument("--message-id", required=True, help="Raw or normalized Message-ID")
    parser.add_argument("--index", help="Path to final-location-index.json")
    args = parser.parse_args()

    message_id_norm = normalize_message_id(args.message_id)
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
