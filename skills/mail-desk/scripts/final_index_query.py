#!/usr/bin/env python3
"""Query/filter helper for data/mail-desk/final-location-index.json."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

_script_dir = Path(__file__).resolve().parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))

from core import query_final_index, resolve_final_index_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Query/filter final-location-index.json")
    parser.add_argument("--folder", help="Substring filter on final_folder or final_label (case-insensitive)")
    parser.add_argument("--query", help="Keyword query on all fields (case-insensitive)")
    parser.add_argument("--index", help="Path to final-location-index.json")
    args = parser.parse_args()

    index_path = resolve_final_index_path(args.index)
    output = query_final_index(index_path, folder=args.folder, query=args.query)
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"count": 0, "error": str(exc)}, ensure_ascii=False, indent=2))
        raise SystemExit(1)
