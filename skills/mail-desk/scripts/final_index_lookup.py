#!/usr/bin/env python3
"""Lookup helper for data/mail-desk/final-location-index.json."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

_script_dir = Path(__file__).resolve().parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))

from core import lookup_final_index, normalize_message_id, resolve_final_index_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Lookup a message in final-location-index.json")
    parser.add_argument(
        "--message-id",
        required=True,
        help="Message-ID to look up",
    )
    parser.add_argument("--index", help="Path to final-location-index.json")
    args = parser.parse_args()

    norm_mid = normalize_message_id(args.message_id)
    if not norm_mid:
        raise ValueError("--message-id must not be empty")

    index_path = resolve_final_index_path(args.index)
    item = lookup_final_index(index_path, norm_mid)

    if item is None:
        output = {
            "found": False,
            "message_id": norm_mid,
        }
    else:
        output = {
            "found": True,
            "message_id": norm_mid,
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
