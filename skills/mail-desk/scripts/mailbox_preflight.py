#!/usr/bin/env python3
"""Preflight check for mail-desk mailbox folder targets.

Checks whether all `mailbox_folder` entries from project/topic catalogs
exist in the current IMAP account folder list.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

_script_dir = Path(__file__).resolve().parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))

from core import build_error, build_success, emit_json, run_himalaya, utc_now_iso


ACTION = "mailbox_preflight"


def load_json(path: Path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def list_imap_folders() -> set[str]:
    raw = run_himalaya(["-o", "json", "folder", "list"])
    if "[" in raw:
        raw = raw[raw.find("["):]
    folders = json.loads(raw)
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


def _write_state_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="\n",
        dir=path.parent,
        prefix=path.name + ".",
        suffix=".tmp",
        delete=False,
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary_path = Path(handle.name)
    os.replace(temporary_path, path)


def _emit_success(message: str, data: dict) -> None:
    emit_json(build_success(ACTION, message, {"operation": "preflight", **data}))


def _emit_error(message: str, exc: Exception | None = None, *, state: str = "Failed", data: dict | None = None, error_type: str | None = None) -> None:
    emit_json(
        build_error(
            ACTION,
            message,
            {"operation": "preflight", **(data or {})},
            state=state,
            error_type=error_type or (type(exc).__name__ if exc else "PreflightError"),
            error_details={"reason": str(exc)[:1000]} if exc else None,
        )
    )


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
    try:
        args = parser.parse_args()
    except SystemExit as exc:
        if exc.code not in (None, 0):
            _emit_error("Invalid command-line arguments.", ValueError("argparse rejected the arguments"), error_type="ArgumentError")
            return int(exc.code)
        raise

    try:
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
                    _emit_success(
                        "Mailbox preflight skipped because the catalog is unchanged.",
                        {
                            "skipped": True,
                            "reason": "catalog_unchanged_default_mode",
                            "checked": 0,
                            "missing_count": 0,
                            "missing": [],
                        },
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
            "skipped": False,
            "checked": len(targets),
            "missing_count": len(missing),
            "missing": [
                {"kind": kind, "id": ident, "folder": folder} for kind, ident, folder in missing
            ],
        }

        state_payload = {
            "ok": len(missing) == 0,
            "checked_at": utc_now_iso(),
            "catalog": current_catalog,
            "result": {"missing_count": result["missing_count"]},
        }
        _write_state_atomic(state_path, state_payload)

        if missing:
            _emit_error(
                "Mailbox preflight found missing target folders.",
                state="TargetsMissing",
                data=result,
                error_type="MissingTargets",
            )
            return 2
        _emit_success("Mailbox preflight completed.", result)
        return 0
    except Exception as exc:  # noqa: BLE001
        _emit_error("Mailbox preflight failed.", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
