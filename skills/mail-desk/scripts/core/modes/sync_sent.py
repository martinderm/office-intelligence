"""Sent-items synchronization handler for the mail-desk batch runner."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping

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

    total_examined, added = sync_items(
        count=count,
        folder=folder,
        account=account,
        data_dir=dd,
        workspace_root=workspace_root,
    )

    return {
        "ok": True,
        "mode": "sync_sent",
        "folder": folder,
        "total_envelopes_examined": total_examined,
        "new_entries_indexed": added,
        "sent_index_file": str(dd / "sent-index.jsonl"),
    }
