#!/usr/bin/env python3
"""Preflight check for mail-desk mailbox folder targets.

Checks whether all `mailbox_folder` entries from project/topic catalogs
exist in the current IMAP account folder list.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timezone


def load_json(path: Path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def list_imap_folders() -> set[str]:
    proc = subprocess.run(
        ["himalaya", "-o", "json", "folder", "list"],
        check=True,
        capture_output=True,
        text=True,
    )
    folders = json.loads(proc.stdout)
    return {x["name"] for x in folders if "name" in x}


def collect_targets(items: list[dict], kind: str) -> list[tuple[str, str, str]]:
    out = []
    for item in items:
        ident = item.get("id", "<missing-id>")
        folder = item.get("mailbox_folder")
        if not folder:
            out.append((kind, ident, "<missing>"))
            continue
        out.append((kind, ident, folder))
    return out


def _catalog_state(path: Path) -> dict:
    st = path.stat()
    return {
        "path": str(path),
        "mtime_ns": st.st_mtime_ns,
        "size": st.st_size,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--projects",
        default="memory/references/projects/projects.json",
        help="Path to projects.json",
    )
    parser.add_argument(
        "--topics",
        default="memory/references/topics/topics.json",
        help="Path to topics.json",
    )
    parser.add_argument(
        "--always",
        action="store_true",
        help="Always run check (disable change-based skip logic).",
    )
    parser.add_argument(
        "--state-file",
        default="data/mail-desk/preflight-state.json",
        help="Path to cached preflight state file.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force run even when --if-catalog-changed is set and no change detected.",
    )
    args = parser.parse_args()

    projects_path = Path(args.projects)
    topics_path = Path(args.topics)
    state_path = Path(args.state_file)

    current_catalog = {
        "projects": _catalog_state(projects_path),
        "topics": _catalog_state(topics_path),
    }

    if not args.always and not args.force and state_path.exists():
        try:
            prev = json.loads(state_path.read_text(encoding="utf-8"))
            prev_cat = prev.get("catalog", {})
            if prev.get("ok") is True and prev_cat == current_catalog:
                print(
                    json.dumps(
                        {
                            "ok": True,
                            "skipped": True,
                            "reason": "catalog_unchanged_default_mode",
                            "checked": 0,
                            "missing_count": 0,
                            "missing": [],
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return 0
        except Exception:
            pass

    projects = load_json(projects_path)
    topics = load_json(topics_path)
    folders = list_imap_folders()

    targets = collect_targets(projects, "project") + collect_targets(topics, "topic")

    missing = []
    for kind, ident, folder in targets:
        if folder == "<missing>":
            missing.append((kind, ident, "mailbox_folder missing in catalog"))
        elif folder not in folders:
            missing.append((kind, ident, folder))

    result = {
        "ok": len(missing) == 0,
        "skipped": False,
        "checked": len(targets),
        "missing_count": len(missing),
        "missing": [
            {"kind": kind, "id": ident, "folder": folder} for kind, ident, folder in missing
        ],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))

    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_payload = {
        "ok": result["ok"],
        "checked_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "catalog": current_catalog,
        "result": {
            "missing_count": result["missing_count"],
        },
    }
    state_path.write_text(json.dumps(state_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return 0 if result["ok"] else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "himalaya command failed",
                    "returncode": exc.returncode,
                    "stderr": exc.stderr,
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        raise SystemExit(1)
