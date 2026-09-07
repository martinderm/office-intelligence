"""Common utility functions for mail-desk."""

from __future__ import annotations

import os
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# Ensure standard streams handle UTF-8 cleanly on Windows
try:
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if sys.stderr and hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format with Z suffix."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def atomic_write_text(target: Path, content: str) -> None:
    """Durably write UTF-8 text to a sibling temporary file, then replace target."""
    target.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor = -1
    temporary_path: Path | None = None
    try:
        file_descriptor, temporary_name = tempfile.mkstemp(
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as handle:
            file_descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, target)
        temporary_path = None
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass


def atomic_write_json(target: Path, payload: Any) -> None:
    """Atomically replace a JSON document with stable UTF-8 formatting."""
    atomic_write_text(target, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def atomic_rewrite_jsonl(target: Path, entries: Iterable[dict[str, Any]]) -> None:
    """Atomically replace a JSONL document; append-only writers remain lock-bound."""
    content = "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries)
    atomic_write_text(target, content)


def get_iso_week_folder() -> str:
    """Return current ISO week string in YYYY-Www format."""
    now = datetime.now(timezone.utc)
    iso_year, iso_week, _ = now.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def normalize_message_id(value: str) -> str:
    """Strip surrounding whitespace and angle brackets and return lowercase."""
    s = value.strip()
    while s.startswith("<") and s.endswith(">") and len(s) >= 2:
        s = s[1:-1].strip()
    return s.lower()


def resolve_data_dir(custom: str | Path | None = None) -> Path:
    """Resolve data/mail-desk directory path."""
    if custom:
        return Path(custom).expanduser().resolve()
    env_dir = os.environ.get("MAIL_DESK_DATA_DIR", "").strip()
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    cwd_data = Path.cwd() / "data" / "mail-desk"
    if cwd_data.exists() or cwd_data.parent.exists():
        return cwd_data.resolve()
    # fallback to script-relative
    fallback = Path(__file__).resolve().parents[4] / "data" / "mail-desk"
    return fallback.resolve()


def resolve_final_index_path(custom_path: str | Path | None = None, data_dir: Path | None = None) -> Path:
    """Resolve final-location-index.json path."""
    if custom_path:
        return Path(custom_path).expanduser().resolve()
    env_index = os.environ.get("MAIL_DESK_FINAL_INDEX_PATH", "").strip()
    if env_index:
        return Path(env_index).expanduser().resolve()
    dd = data_dir or resolve_data_dir()
    return dd / "final-location-index.json"


def resolve_evidence_dir(kind: str, item_id: str, workspace_root: Path | None = None) -> Path:
    """
    Dual-Path evidence resolver (Dual-Evidence Standard):
    1. If memory/evidence/<kind>/<item_id> exists, or memory/evidence exists, use memory/evidence/<kind>/<item_id>
    2. Fallback to legacy memory/references/<kind>/<item_id>/evidence if it exists
    3. Default to memory/evidence/<kind>/<item_id>
    """
    ws = workspace_root or Path.cwd()
    new_path = ws / "memory" / "evidence" / kind / item_id
    if new_path.exists() or (ws / "memory" / "evidence").exists():
        return new_path

    legacy_path = ws / "memory" / "references" / kind / item_id / "evidence"
    if legacy_path.exists():
        return legacy_path

    return new_path
