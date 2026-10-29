"""Read-only pipeline preparation; execution requires a separate H2 approval."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping

from ..batch_contract import add_draft_contract
from ..classifier import draft_manifest
from ..common import resolve_data_dir
from ..himalaya import get_single_email_details
from ..sent_indexer import load_sent_index
from ..synthesis_handoff import empty_synthesis_handoff
from ..telemetry import empty_telemetry


def _dependency(
    dependencies: Mapping[str, Callable[..., Any]] | None,
    name: str,
    default: Callable[..., Any],
) -> Callable[..., Any]:
    if dependencies and name in dependencies:
        return dependencies[name]
    return default


def run_pipeline_mode(
    config: dict[str, Any],
    account: str | None = None,
    data_dir: Path | None = None,
    index_path: Path | None = None,
    *,
    dependencies: Mapping[str, Callable[..., Any]] | None = None,
) -> dict[str, Any]:
    """Inspect/classify only, returning a pending manifest without batch writes.

    No confidence level, manifest field or caller option grants execution here.
    Sent synchronization, execute and verify are deliberately not dependencies.
    """
    if not dependencies or "get_unprocessed_emails" not in dependencies:
        raise RuntimeError("get_unprocessed_emails must be supplied by the batch-runner compatibility adapter")
    resolve_data = _dependency(dependencies, "resolve_data_dir", resolve_data_dir)
    dd = data_dir or resolve_data()
    folder = config.get("folder", "INBOX")
    count = int(config.get("count", 20))
    expected_count = config.get("expected_count", count)
    if isinstance(expected_count, bool) or not isinstance(expected_count, int) or expected_count < 1:
        raise ValueError("expected_count must be a positive integer")
    allow_fewer = config.get("allow_fewer", False)
    skip_known = config.get("skip_known", True)
    if not isinstance(allow_fewer, bool) or not isinstance(skip_known, bool):
        raise ValueError("allow_fewer and skip_known must be booleans")
    emails, _known_count = dependencies["get_unprocessed_emails"](
        folder=folder, target_count=count, order=str(config.get("order", "oldest")).lower(),
        date=config.get("date"), query=config.get("query"), account=account,
        data_dir=dd, skip_known=skip_known, preview_lines=int(config.get("preview_lines", 30)),
    )
    load_sent = _dependency(dependencies, "load_sent_index", load_sent_index)
    draft = _dependency(dependencies, "draft_manifest", draft_manifest)
    full_reader = _dependency(dependencies, "get_single_email_details", get_single_email_details)
    manifest = draft(
        emails, workspace_root=dd.parent.parent, sent_lookup=load_sent(dd),
        full_reader=full_reader, account=account, sync_sent=False,
    )
    manifest["mode"] = "execute"
    add_draft_contract(
        manifest, expected_count=expected_count, allow_fewer=allow_fewer,
        source_folder=folder, account=account, skip_known=skip_known,
    )
    return {
        "ok": True, "mode": "pipeline", "status": "review_required",
        "message": "Pipeline prepares a pending draft only; review and execute it separately.",
        "folder": folder, "order": str(config.get("order", "oldest")).lower(),
        "total_inspected": len(emails), "executed_count": 0, "verified_count": 0,
        "review_needed_count": len(manifest.get("items", [])),
        "review_needed_items": manifest.get("items", []),
        "execute_summary": None, "verify_summary": None,
        "draft": manifest, "review": manifest["review"],
        "telemetry": empty_telemetry(), "synthesis_handoff": empty_synthesis_handoff(),
    }
