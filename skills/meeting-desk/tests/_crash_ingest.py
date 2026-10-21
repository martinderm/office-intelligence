#!/usr/bin/env python3
"""Test-only crash driver: stop a real ingest process after N publications.

This is not production code and is never imported by the writer. It lets the
regression suite prove that an abrupt process death between publication steps
leaves a recoverable journal instead of a silently half-written workspace.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys


def main(argv: list[str]) -> int:
    writer_path, workspace, record_path, lease_id, lock_root, crash_after = argv[1:7]
    spec = importlib.util.spec_from_file_location("meeting_desk_crash_writer", writer_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original = module._publish_one
    counter = {"published": 0}
    threshold = int(crash_after)

    def interrupted(root, step):
        counter["published"] += 1
        if counter["published"] == threshold:
            os._exit(9)
        original(root, step)

    module._publish_one = interrupted
    record = json.loads(Path(record_path).read_text(encoding="utf-8"))
    envelope = module.ingest_record(
        record, workspace_root=workspace, lease_id=lease_id,
        trusted_workspace_lock_root=lock_root)
    print(json.dumps(envelope, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
