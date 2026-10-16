#!/usr/bin/env python3
"""Compose bounded event-dossier stages without invoking mailbox/event writers."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

from core.envelope import build_error, build_success, emit_json
from core.event_dossier import MAX_REQUEST_BYTES, process_request, strict_json

ACTION = "mail_desk_event_dossier"


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("Invalid arguments")


def main(argv=None):
    parser = _Parser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--request", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--json", action="store_true")
    try:
        args = parser.parse_args(argv)
        with Path(args.request).open("rb") as stream:
            raw = stream.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError("Request exceeds bound")
        result = process_request(strict_json(raw.decode("utf-8")), workspace_root=args.workspace)
        if not result["ok"]:
            raise ValueError("Invalid request")
    except (ValueError, TypeError, OSError, RecursionError):
        emit_json(build_error(ACTION, "Invalid request, evidence or arguments; no action performed.",
                              error_type="InvalidRequest"))
        return 2
    emit_json(build_success(ACTION, "Event dossier stage prepared or verified; no content approval implied.", result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
