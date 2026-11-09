"""Sent-items synchronization handler for the mail-desk batch runner."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

from ..common import resolve_data_dir
from ..sent_handoff import (
    HANDOFF_FILENAME,
    HANDOFF_STATES,
    SentHandoffError,
    SentSyncPartialFailure,
    peek_handoff_state,
)
from ..sent_indexer import sync_sent_items


def _dependency(
    dependencies: Mapping[str, Callable[..., Any]] | None,
    name: str,
    default: Callable[..., Any],
) -> Callable[..., Any]:
    if dependencies and name in dependencies:
        return dependencies[name]
    return default


def _success_envelope(sync_result: Mapping[str, Any], folder: str, dd: Path) -> dict[str, Any]:
    examined = int(sync_result.get("envelopes_examined", 0) or 0)
    new_items = list(sync_result.get("new_items") or [])
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
        # Additive (#88): the Sent Items that are new relative to the previous index state.
        "new_count": int(sync_result.get("new_count", len(new_items)) or 0),
        "new_items": new_items,
        "sent_index_file": str(dd / "sent-index.jsonl"),
    }
    handoff = sync_result.get("handoff")
    if isinstance(handoff, Mapping):
        envelope["handoff"] = {**handoff, "file": str(dd / HANDOFF_FILENAME)}
    truncated_days = sync_result.get("truncated_days")
    if truncated_days:
        envelope["truncated_days"] = list(truncated_days)
    follow_up_hint = sync_result.get("follow_up_hint")
    if follow_up_hint:
        envelope["follow_up_hint"] = follow_up_hint
    return envelope


def run_sync_sent_mode(
    config: dict[str, Any],
    account: str | None = None,
    data_dir: Path | None = None,
    *,
    dependencies: Mapping[str, Callable[..., Any]] | None = None,
) -> dict[str, Any]:
    """Fetch recent Sent Items, hand off the new ones and index them into sent-index.jsonl.

    A refused handoff (``handoff_pending``/``handoff_invalid``) or a partial date
    failure is returned as ``ok: False`` so the runner exits non-zero; the former
    wrote nothing, the latter committed (handed off + indexed) the successful dates.
    """
    resolve_data = _dependency(dependencies, "resolve_data_dir", resolve_data_dir)
    sync_items = _dependency(dependencies, "sync_sent_items", sync_sent_items)

    dd = data_dir or resolve_data()
    workspace_root = dd.parent.parent
    count = int(config.get("count", 150))
    folder = config.get("folder", "Sent Items")

    try:
        sync_result = sync_items(
            count=count,
            folder=folder,
            account=account,
            data_dir=dd,
            workspace_root=workspace_root,
        )
    except SentHandoffError as error:
        return _failure_envelope(dd, folder, str(error), error.reason_code, state=error.details.get("state"), details=error.details)
    except SentSyncPartialFailure as error:
        partial = _success_envelope(error.result, folder, dd)
        partial.update(
            ok=False,
            message=str(error),
            reason_code="sync_partial_failure",
            failed_dates=list(error.failed_dates),
        )
        return partial
    except Exception as error:  # noqa: BLE001 - every failed sync stays a failed, shaped envelope
        # Read/transport/append failures (including those after the handoff was
        # persisted) are never reported as success and never lose the shape.
        return _failure_envelope(
            dd, folder, f"Sent sync failed: {type(error).__name__}: {error}"[:1000], "sync_failed",
            details={"error_type": type(error).__name__},
        )

    if isinstance(sync_result, Mapping):
        return _success_envelope(sync_result, folder, dd)

    # Legacy dependency contract: an injected ``(total_examined, added)`` tuple
    # keeps the historical envelope shape (batch-runner compatibility facades) and
    # additionally reports the always-present new_count/new_items (items unknown).
    total_examined, added = sync_result
    return {
        "ok": True,
        "mode": "sync_sent",
        "folder": folder,
        "total_envelopes_examined": total_examined,
        "new_entries_indexed": added,
        "new_count": int(added or 0),
        "new_items": [],
        "new_items_unavailable": True,
        "sent_index_file": str(dd / "sent-index.jsonl"),
    }


def _failure_envelope(
    dd: Path,
    folder: str,
    message: str,
    reason_code: str,
    *,
    state: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """``ok: false`` result that always carries ``new_count``/``new_items``/``handoff``.

    ``handoff.state`` is one of ``none|written|pending|invalid``; the refusal itself
    is ``reason_code``.  Pending counts come from a read-only peek that never raises.
    """
    peek = peek_handoff_state(dd)
    handoff = {key: value for key, value in (details or {}).items() if key != "state"}
    handoff.setdefault("pending_count", peek["pending_count"])
    handoff["state"] = state if state in HANDOFF_STATES else peek["state"]
    handoff["file"] = str(dd / HANDOFF_FILENAME)
    return {
        "ok": False,
        "mode": "sync_sent",
        "folder": folder,
        "message": message,
        "reason_code": reason_code,
        "new_count": 0,
        "new_items": [],
        "new_entries_indexed": 0,
        "handoff": handoff,
        "sent_index_file": str(dd / "sent-index.jsonl"),
    }
