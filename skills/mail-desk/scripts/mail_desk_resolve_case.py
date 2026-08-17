#!/usr/bin/env python3
"""Resolve and archive needs-reply and pending-review cases in data/mail-desk/."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

_script_dir = Path(__file__).resolve().parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))

from core import resolve_case, resolve_data_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="Resolve and archive mail-desk cases")
    parser.add_argument("--message-id", required=True, help="Message-ID of the case to resolve")
    parser.add_argument("--status", default="resolved", help="Resolution status (default: resolved)")
    parser.add_argument("--resolution", required=True, help="Resolution explanation")
    parser.add_argument("--resolved-by-message-id", help="Optional Message-ID of the reply mail")
    parser.add_argument("--data-dir", help="Path to data/mail-desk directory")
    args = parser.parse_args()

    data_dir = resolve_data_dir(args.data_dir)
    res = resolve_case(
        data_dir=data_dir,
        message_id=args.message_id,
        status=args.status,
        resolution=args.resolution,
        resolved_by=args.resolved_by_message_id,
    )
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("resolved") else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        raise SystemExit(1)
