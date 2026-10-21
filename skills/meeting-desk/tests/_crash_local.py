#!/usr/bin/env python3
"""Test-only crash driver for the local-file entry (never imported in production)."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys


def main(argv: list[str]) -> int:
    (writer_path, workspace, lease_id, lock_root, crash_after, summary,
     transcript, raw, day) = argv[1:10]
    spec = importlib.util.spec_from_file_location("meeting_desk_crash_local_writer",
                                                  writer_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    plan = module.ingest_local(
        workspace_root=workspace, lease_id=lease_id, summary_path=summary,
        transcript_path=transcript, raw_path=raw or None, calendar_date=day,
        title="Synthetic local crash", trusted_workspace_lock_root=lock_root,
        dry_run=True)
    if not plan.get("success"):
        print(json.dumps(plan, ensure_ascii=False))
        return 2
    plan_sha = plan["data"]["plan_sha256"]
    original = module._publish_one
    counter = {"published": 0}
    threshold = int(crash_after)

    def interrupted(root, step):
        counter["published"] += 1
        if counter["published"] == threshold:
            os._exit(9)
        original(root, step)

    module._publish_one = interrupted
    envelope = module.ingest_local(
        workspace_root=workspace, lease_id=lease_id, summary_path=summary,
        transcript_path=transcript, raw_path=raw or None, calendar_date=day,
        title="Synthetic local crash", trusted_workspace_lock_root=lock_root,
        apply_plan_sha256=plan_sha)
    print(json.dumps(envelope, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
