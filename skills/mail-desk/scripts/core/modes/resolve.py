"""Case resolution mode for the mail-desk batch runner."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..action_log import resolve_case
from ..common import resolve_data_dir
from ..sent_indexer import auto_resolve_replies_from_sent


def run_resolve_mode(
    config: dict[str, Any],
    data_dir: Path | None = None,
) -> dict[str, Any]:
    """Resolve explicit cases or audit open reply cases against Sent Items."""
    resolved_data_dir = data_dir or resolve_data_dir()
    items = list(config.get("items", []))
    if "message_id" in config:
        items.append(
            {
                "message_id": config["message_id"],
                "status": config.get("status", "resolved"),
                "resolution": config.get("resolution", ""),
                "resolved_by": config.get("resolved_by_message_id") or config.get("resolved_by"),
            }
        )

    if items:
        resolved_results: list[dict[str, Any]] = []
        all_resolved = True

        for item in items:
            message_id = item.get("message_id", "")
            status = item.get("status", "resolved")
            resolution = item.get("resolution", "")
            resolved_by = item.get("resolved_by_message_id") or item.get("resolved_by")
            result = resolve_case(
                resolved_data_dir,
                message_id,
                status=status,
                resolution=resolution,
                resolved_by=resolved_by,
            )
            resolved_results.append(result)
            if not result.get("resolved"):
                all_resolved = False

        return {
            "ok": all_resolved,
            "mode": "resolve",
            "total_processed": len(resolved_results),
            "all_resolved": all_resolved,
            "results": resolved_results,
        }

    auto_result = auto_resolve_replies_from_sent(data_dir=resolved_data_dir)
    return {
        "ok": True,
        "mode": "resolve",
        "auto_audit": True,
        **auto_result,
    }
