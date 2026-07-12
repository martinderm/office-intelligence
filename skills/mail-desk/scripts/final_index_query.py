#!/usr/bin/env python3
"""Query/filter helper for data/mail-desk/final-location-index.json."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any


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
    parser = argparse.ArgumentParser(description="Query/filter final-location-index.json")
    parser.add_argument("--folder", help="Substring filter on final_folder or final_label (case-insensitive)")
    parser.add_argument("--query", help="Keyword query on all fields (case-insensitive)")
    parser.add_argument("--index", help="Path to final-location-index.json")
    args = parser.parse_args()

    index_path = Path(args.index) if args.index else default_index_path()
    data = load_index(index_path)
    items = data.get("items", {})

    results = {}
    for key, val in items.items():
        if not isinstance(val, dict):
            continue

        # Filter by folder
        if args.folder:
            location = str(val.get("final_folder") or val.get("final_label") or "")
            if args.folder.lower() not in location.lower():
                continue

        # Filter by query
        if args.query:
            q = args.query.lower()
            # Combine all string/list representation of the item to search
            combined_fields = []
            for field_val in val.values():
                if isinstance(field_val, list):
                    combined_fields.extend([str(x).lower() for x in field_val])
                else:
                    combined_fields.append(str(field_val).lower())
            
            if not any(q in f for f in combined_fields):
                continue

        results[key] = val

    output = {
        "count": len(results),
        "items": results,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"count": 0, "error": str(exc)}, ensure_ascii=False, indent=2))
        raise SystemExit(1)
