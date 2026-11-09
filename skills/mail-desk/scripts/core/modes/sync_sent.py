"""Sent-items synchronization handler for the mail-desk batch runner."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ..common import resolve_data_dir
from ..sent_handoff import (
    HANDOFF_FILENAME,
    HANDOFF_STATES,
    MAX_HANDOFF_ITEMS,
    SentHandoffError,
    SentHandoffPublishError,
    SentIndexAppendError,
    SentSyncPartialFailure,
    peek_handoff_state,
)
from ..sent_indexer import SENT_SYNC_MAX_PAGES, sync_sent_items

#: Honesty of the ``ok: false`` envelope about write progress.  ``none``: the run
#: refused or failed before any handoff/index write; ``partial``: the commit path
#: completed for the successful date windows and the reported counts are
#: authoritative (some windows failed); ``unknown``: a write was attempted and
#: failed, so the on-disk progress is unverified (a rerun merges idempotently).
WRITE_STATES = ("none", "state", "partial", "unknown")


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
    # Coverage result (count is a day limit, not an item limit).
    envelope["truncated"] = bool(sync_result.get("truncated", False))
    for key in ("coverage_start", "coverage_end", "covered_days", "total_days",
                "coverage_complete", "next_start"):
        if key in sync_result:
            envelope[key] = sync_result[key]
    follow_up_hint = sync_result.get("follow_up_hint")
    if follow_up_hint:
        envelope["follow_up_hint"] = follow_up_hint
    return envelope


def _default_handoff(dd: Path) -> dict[str, Any]:
    """The documented handoff shape before any refusal-specific detail is merged in."""
    return {
        "state": "none",
        "written_count": 0,
        "pending_count": 0,
        "consumed_ignored": 0,
        "deferred_count": 0,
        "headers_incomplete_count": 0,
        "already_synthesized_skipped": 0,
        "max_items": MAX_HANDOFF_ITEMS,
        "file": str(dd / HANDOFF_FILENAME),
    }


def failure_envelope(
    dd: Path,
    folder: str,
    message: str,
    reason_code: str,
    *,
    state: str | None = None,
    write_state: str = "none",
    details: Mapping[str, Any] | None = None,
    new_count: int | None = 0,
    new_items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """``ok: false`` result that always carries ``new_count``/``new_items``/``handoff``.

    ``handoff.state`` is one of ``none|written|pending|invalid``; the refusal itself
    is ``reason_code``.  Pending counts come from a read-only peek that never raises.
    ``write_state`` distinguishes a pre-write refusal (``none``) from a run that
    only persisted the coverage state/marker (``state``, no handoff/index write), a
    confirmed partial commit (``partial``) and unverified progress after a failed
    write (``unknown``); only ``unknown`` reports ``new_count: null``.
    """
    peek = peek_handoff_state(dd)
    handoff = _default_handoff(dd)
    # Peek-derived state first, so refusal-specific details (e.g. the exact
    # pending_count of handoff_pending) can override it.
    handoff.update(state=peek["state"], pending_count=peek["pending_count"])
    handoff.update({key: value for key, value in (details or {}).items() if key != "state"})
    handoff["state"] = state if state in HANDOFF_STATES else peek["state"]
    envelope: dict[str, Any] = {
        "ok": False,
        "mode": "sync_sent",
        "folder": folder,
        "message": message,
        "reason_code": reason_code,
        "write_state": write_state if write_state in WRITE_STATES else "unknown",
        "new_count": new_count,
        "new_items": list(new_items or []),
        "new_entries_indexed": new_count,
        "handoff": handoff,
        "sent_index_file": str(dd / "sent-index.jsonl"),
    }
    if new_count is None:
        envelope["new_items_unavailable"] = True
    return envelope


def _positive_count(config: Mapping[str, Any]) -> int:
    """Read the run count, rejecting non-integer and non-positive manifests."""
    raw = config.get("count", 150)
    if isinstance(raw, bool) or isinstance(raw, float):
        raise ValueError("count must be a positive integer")
    count = int(raw)
    if count < 1:
        raise ValueError("count must be a positive integer")
    return count


def run_sync_sent_mode(
    config: dict[str, Any],
    account: str | None = None,
    data_dir: Path | None = None,
    *,
    dependencies: Mapping[str, Callable[..., Any]] | None = None,
) -> dict[str, Any]:
    """Fetch recent Sent Items, hand off the new ones and index them into sent-index.jsonl.

    A refused handoff (``handoff_pending``/``handoff_invalid``), an invalid manifest
    (``sync_invalid_count``), a truncated Sent window (``sent_window_truncated``) or
    any failed date window (``sync_window_failed``) is returned as ``ok: False`` with
    ``write_state`` ``none``/``state``: no handoff or index was written (only the
    coverage state may have been persisted).
    An unexpected failure during the write path is ``sync_failed`` with
    ``write_state: unknown``: the handoff may already be persisted and the index
    may have grown, so the envelope never claims that nothing was written.
    """
    resolve_data = _dependency(dependencies, "resolve_data_dir", resolve_data_dir)
    sync_items = _dependency(dependencies, "sync_sent_items", sync_sent_items)

    dd = data_dir or resolve_data()
    workspace_root = dd.parent.parent
    raw_folder = config.get("folder", "Sent Items")
    folder = raw_folder if isinstance(raw_folder, str) else "Sent Items"

    try:
        count = _positive_count(config)
    except (TypeError, ValueError) as error:
        return failure_envelope(
            dd,
            folder,
            (
                "Sent sync refused: configuration field 'count' is not a positive integer "
                f"({type(error).__name__}: {error}). Nothing was read or written. "
                "Recovery: set count to a positive integer and rerun."
            )[:1000],
            "sync_invalid_count",
            details={"error_type": type(error).__name__, "invalid_field": "count"},
        )

    since = config.get("since")
    if since is not None:
        if not isinstance(since, str) or len(since) != 10:
            return failure_envelope(
                dd, folder, "Sent sync refused: 'since' must be YYYY-MM-DD without a time part. "
                "Recovery: correct since or omit it and rerun.",
                "sync_invalid_since", details={"invalid_field": "since"},
            )
        try:
            since_date = datetime.strptime(since, "%Y-%m-%d")
        except ValueError:
            return failure_envelope(
                dd, folder, "Sent sync refused: 'since' must be a valid YYYY-MM-DD date without a "
                "time part. Recovery: correct since or omit it and rerun.",
                "sync_invalid_since", details={"invalid_field": "since"},
            )
        if since_date.date() > datetime.now().date():
            return failure_envelope(
                dd, folder, f"Sent sync refused: 'since' {since!r} lies in the future. "
                "Recovery: use a date on/before today and rerun.",
                "sync_invalid_since", details={"invalid_field": "since", "reason": "future"},
            )
    raw_max_pages = config.get("max_pages", SENT_SYNC_MAX_PAGES)
    try:
        if isinstance(raw_max_pages, bool) or isinstance(raw_max_pages, float):
            raise ValueError("max_pages must be a positive integer")
        max_pages = int(raw_max_pages)
        if max_pages < 1:
            raise ValueError("max_pages must be a positive integer")
    except (TypeError, ValueError) as error:
        return failure_envelope(
            dd, folder, f"Sent sync refused: configuration field 'max_pages' is invalid ({error}). "
            "Recovery: set max_pages to a positive integer and rerun.",
            "sync_invalid_max_pages", details={"invalid_field": "max_pages"},
        )

    try:
        sync_result = sync_items(
            count=count,
            folder=folder,
            account=account,
            data_dir=dd,
            workspace_root=workspace_root,
            since=since,
            max_pages=max_pages,
        )
    except SentHandoffError as error:
        return failure_envelope(
            dd,
            folder,
            str(error),
            error.reason_code,
            state=error.details.get("state"),
            write_state=getattr(error, "write_state", "none"),
            details={key: value for key, value in error.details.items() if key != "state"},
        )
    except SentSyncPartialFailure as error:
        # Fail-closed: if any date window failed, nothing was handed off or indexed.
        # Committing newer days would move the watermark past the failed ones.
        partial = failure_envelope(
            dd,
            folder,
            str(error),
            "sync_window_failed",
            write_state=getattr(error, "write_state", "none"),
            details={"error_type": "DateWindowFailure"},
            new_count=0,
        )
        partial["failed_dates"] = list(error.failed_dates)
        partial["date_windows_synced"] = int(error.result.get("date_windows_synced", 0) or 0)
        partial["envelopes_examined"] = int(error.result.get("envelopes_examined", 0) or 0)
        partial["total_envelopes_examined"] = partial["envelopes_examined"]
        return partial
    except SentHandoffPublishError as error:
        # The atomic replace may already have published the new handoff before a
        # later step failed: *always* report unverified progress, never "no writes".
        return failure_envelope(
            dd,
            folder,
            str(error)[:1000],
            "sync_failed",
            write_state="unknown",
            details={
                "error_type": type(error.__cause__).__name__ if error.__cause__ else type(error).__name__,
                "handoff_publish_failed": True,
            },
            new_count=None,
        )
    except SentIndexAppendError as error:
        # The handoff is durable, but the index may be empty, partially written or
        # complete: report unverified progress instead of claiming zero entries.
        return failure_envelope(
            dd,
            folder,
            str(error)[:1000],
            "sync_failed",
            write_state="unknown",
            details={
                "error_type": type(error.__cause__).__name__ if error.__cause__ else type(error).__name__,
                "append_failed": True,
            },
            new_count=None,
        )
    except Exception as error:  # noqa: BLE001 - every failed sync stays a failed, shaped envelope
        # Everything else failed before the commit path wrote anything (handoff
        # publication and index append are reported as ``unknown`` above).
        return failure_envelope(
            dd,
            folder,
            f"Sent sync failed: {type(error).__name__}: {error}"[:1000],
            "sync_failed",
            write_state=getattr(error, "write_state", "none"),
            details={"error_type": type(error).__name__},
            new_count=None if getattr(error, "write_state", "none") == "unknown" else 0,
        )

    if isinstance(sync_result, Mapping):
        envelope = _success_envelope(sync_result, folder, dd)
        if envelope.get("truncated") and envelope.get("new_count") == 0:
            envelope.update(
                ok=False,
                reason_code="sync_no_progress",
                write_state="state",
                message=(
                    "Coverage stopped at the item budget with no new items; nothing was handed off "
                    "or indexed. " + str(sync_result.get("follow_up_hint") or "")
                )[:1000],
            )
        return envelope

    # Legacy dependency contract: an injected ``(total_examined, added)`` tuple
    # keeps the historical envelope shape (batch-runner compatibility facades) and
    # additionally reports the always-present new_count/new_items/handoff (items
    # unknown, the handoff state is read-only peeked).
    total_examined, added = sync_result
    peek = peek_handoff_state(dd)
    handoff = _default_handoff(dd)
    handoff.update(state=peek["state"], pending_count=peek["pending_count"])
    return {
        "ok": True,
        "mode": "sync_sent",
        "folder": folder,
        "total_envelopes_examined": total_examined,
        "new_entries_indexed": added,
        "new_count": int(added or 0),
        "new_items": [],
        "new_items_unavailable": True,
        "handoff": handoff,
        "sent_index_file": str(dd / "sent-index.jsonl"),
    }
