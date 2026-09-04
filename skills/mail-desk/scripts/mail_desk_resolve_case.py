#!/usr/bin/env python3
"""Resolve and archive needs-reply and pending-review cases in data/mail-desk/."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Any

_script_dir = Path(__file__).resolve().parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))

from core import build_error, build_success, emit_json, resolve_case, resolve_data_dir


ACTION = "resolve_case"


def _case_data(result: dict[str, Any]) -> dict[str, Any]:
    """Expose the archival result without retaining legacy truth fields."""
    data = {
        "operation": "resolve",
        "message_id": result.get("message_id"),
        "source_file": result.get("source_file"),
        "archived_to": result.get("archived_to"),
    }
    if result.get("item") is not None:
        data["archived_item"] = result["item"]
    return data


def _emit_error(
    operation: str,
    message: str,
    exc: Exception | None = None,
    *,
    state: str = "Failed",
    data: dict[str, Any] | None = None,
    error_type: str | None = None,
) -> None:
    emit_json(
        build_error(
            ACTION,
            message,
            {"operation": operation, **(data or {})},
            state=state,
            error_type=error_type or (type(exc).__name__ if exc else "ResolveCaseError"),
            error_details={"reason": str(exc)[:1000]} if exc else None,
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Resolve and archive mail-desk cases")
    parser.add_argument("--message-id", required=True, help="Message-ID of the case to resolve")
    parser.add_argument("--status", default="resolved", help="Resolution status (default: resolved)")
    parser.add_argument("--resolution", required=True, help="Resolution explanation")
    parser.add_argument("--resolved-by-message-id", help="Optional Message-ID of the reply mail")
    parser.add_argument("--data-dir", help="Path to data/mail-desk directory")
    try:
        args = parser.parse_args()
    except SystemExit as exc:
        if exc.code not in (None, 0):
            _emit_error(
                "argument_parse",
                "Invalid command-line arguments.",
                ValueError("argparse rejected the arguments"),
                error_type="ArgumentError",
            )
            return int(exc.code)
        raise

    try:
        data_dir = resolve_data_dir(args.data_dir)
        res = resolve_case(
            data_dir=data_dir,
            message_id=args.message_id,
            status=args.status,
            resolution=args.resolution,
            resolved_by=args.resolved_by_message_id,
        )
    except Exception as exc:  # noqa: BLE001
        _emit_error("resolve", "Case resolution failed.", exc)
        return 1

    data = _case_data(res)
    if not res.get("resolved"):
        _emit_error(
            "resolve",
            "Message ID was not found in active cases.",
            state="NotFound",
            data=data,
            error_type="CaseNotFound",
        )
        return 2
    emit_json(build_success(ACTION, "Case resolved and archived.", data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
