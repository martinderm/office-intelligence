"""Sent-items synchronization handler for the mail-desk batch runner."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

from ..common import resolve_data_dir
from ..sent_indexer import sync_sent_items


def _dependency(
    dependencies: Mapping[str, Callable[..., Any]] | None,
    name: str,
    default: Callable[..., Any],
) -> Callable[..., Any]:
    if dependencies and name in dependencies:
        return dependencies[name]
    return default


def run_sync_sent_mode(
    config: dict[str, Any],
    account: str | None = None,
    data_dir: Path | None = None,
    *,
    dependencies: Mapping[str, Callable[..., Any]] | None = None,
) -> dict[str, Any]:
    """Fetch recent Sent Items and index them into sent-index.jsonl."""
    resolve_data = _dependency(dependencies, "resolve_data_dir", resolve_data_dir)
    sync_items = _dependency(dependencies, "sync_sent_items", sync_sent_items)

    dd = data_dir or resolve_data()
    workspace_root = dd.parent.parent
    count = int(config.get("count", 150))
    folder = config.get("folder", "Sent Items")

    sync_result = sync_items(
        count=count,
        folder=folder,
        account=account,
        data_dir=dd,
        workspace_root=workspace_root,
    )

    if isinstance(sync_result, Mapping):
        examined = int(sync_result.get("envelopes_examined", 0) or 0)
        envelope: dict[str, Any] = {
            "ok": True,
            "mode": "sync_sent",
            "folder": folder,
            # date_windows_synced counts the queried date buckets; the envelope
            # counts are the real, summed envelope number (MD-SE2 separation).
            "date_windows_synced": int(sync_result.get("date_windows_synced", 0) or 0),
            "envelopes_examined": examined,
            "total_envelopes_examined": examined,
            "new_entries_indexed": int(sync_result.get("new_entries_indexed", 0) or 0),
            "sent_index_file": str(dd / "sent-index.jsonl"),
        }
        truncated_days = sync_result.get("truncated_days")
        if truncated_days:
            envelope["truncated_days"] = list(truncated_days)
        follow_up_hint = sync_result.get("follow_up_hint")
        if follow_up_hint:
            envelope["follow_up_hint"] = follow_up_hint
        return envelope

    # Legacy dependency contract: an injected ``(total_examined, added)`` tuple
    # keeps the historical envelope shape (batch-runner compatibility facades).
    total_examined, added = sync_result
    return {
        "ok": True,
        "mode": "sync_sent",
        "folder": folder,
        "total_envelopes_examined": total_examined,
        "new_entries_indexed": added,
        "sent_index_file": str(dd / "sent-index.jsonl"),
    }
