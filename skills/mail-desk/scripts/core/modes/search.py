"""Read-only mailbox search mode for the mail-desk batch runner."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..common import atomic_write_json
from ..himalaya import search_mailbox


def run_search_mode(
    config: dict[str, Any],
    account: str | None = None,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    """Search configured mailbox folders and optionally persist the result."""
    del data_dir  # Kept for the stable mode-handler call contract.
    query = config.get("query", "").strip()
    raw_mids = config.get("message_ids", [])
    folders = config.get("folders")
    page_size = int(config.get("page_size", 50))
    threads = int(config.get("threads", 4))
    output_file = config.get("output_file")

    matches = search_mailbox(
        query=query,
        message_ids=raw_mids,
        folders=folders,
        page_size=page_size,
        threads=threads,
        account=account,
    )

    result = {
        "ok": True,
        "mode": "search",
        "total_found": len(matches),
        "matches": matches,
    }

    if output_file:
        output_path = Path(output_file).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(output_path, result)

    return result
