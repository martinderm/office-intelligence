#!/usr/bin/env python3
"""Batch upsert helper for data/mail-desk/final-location-index.json."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

_script_dir = Path(__file__).resolve().parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))

from core import resolve_final_index_path, upsert_final_index_many


def load_payloads(text: str) -> list[dict]:
    payloads: list[dict] = []
    for idx, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict):
            raise ValueError(f"Line {idx}: payload must be a JSON object")
        payloads.append(payload)
    if not payloads:
        raise ValueError("JSONL payload is empty")
    return payloads


def main() -> int:
    parser = argparse.ArgumentParser(description="Batch upsert final-location-index.json from JSONL stdin or file")
    parser.add_argument("--mode", choices=["upsert-final", "patch"], default="patch")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--stdin", action="store_true", help="Read JSONL from stdin")
    source.add_argument("--file", help="Read JSONL payloads from file")
    parser.add_argument("--index", help="Path to final-location-index.json")
    args = parser.parse_args()

    raw = sys.stdin.read() if args.stdin else Path(args.file).read_text(encoding="utf-8")
    payloads = load_payloads(raw)

    index_path = resolve_final_index_path(args.index)
    res = upsert_final_index_many(index_path, payloads, mode=args.mode)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        raise SystemExit(1)
