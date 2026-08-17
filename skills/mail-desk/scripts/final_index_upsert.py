#!/usr/bin/env python3
"""Upsert/patch helper for data/mail-desk/final-location-index.json."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

_script_dir = Path(__file__).resolve().parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))

from core import resolve_final_index_path, upsert_final_index_entry


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

    index_path = resolve_final_index_path(args.index)
    result = upsert_final_index_entry(index_path, payload, mode=args.mode)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        raise SystemExit(1)
