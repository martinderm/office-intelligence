"""Draft-mode handler for the mail-desk batch runner."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Mapping

from ..classifier import draft_manifest
from ..batch_contract import add_draft_contract
from ..common import atomic_write_json, resolve_data_dir
from ..himalaya import get_single_email_details
from ..progress import BatchProgressTracker
from ..sent_indexer import load_sent_index


def _dependency(
    dependencies: Mapping[str, Callable[..., Any]] | None,
    name: str,
    default: Callable[..., Any],
) -> Callable[..., Any]:
    if dependencies and name in dependencies:
        return dependencies[name]
    return default


def _required_dependency(
    dependencies: Mapping[str, Callable[..., Any]] | None,
    name: str,
) -> Callable[..., Any]:
    if dependencies and name in dependencies:
        return dependencies[name]
    raise RuntimeError(f"{name} must be supplied by the batch-runner compatibility adapter")


def run_draft_mode(
    config: dict[str, Any],
    account: str | None = None,
    data_dir: Path | None = None,
    *,
    dependencies: Mapping[str, Callable[..., Any]] | None = None,
) -> dict[str, Any]:
    """Fetch or reuse inspections, then create the reviewable batch manifest."""
    resolve_data = _dependency(dependencies, "resolve_data_dir", resolve_data_dir)
    progress_tracker = _dependency(dependencies, "BatchProgressTracker", BatchProgressTracker)
    load_sent = _dependency(dependencies, "load_sent_index", load_sent_index)
    draft = _dependency(dependencies, "draft_manifest", draft_manifest)
    full_reader = _dependency(dependencies, "get_single_email_details", get_single_email_details)
    write_json = _dependency(dependencies, "atomic_write_json", atomic_write_json)

    dd = data_dir or resolve_data()
    workspace_root = dd.parent.parent
    folder = config.get("folder", "INBOX")
    count = int(config.get("count", 20))
    order = str(config.get("order", "oldest")).lower()
    date = config.get("date")
    query = config.get("query")
    preview_lines = int(config.get("preview_lines", 30))
    skip_known = bool(config.get("skip_known", True))
    output_file = config.get("output_file", str(dd / "batch-manifest.json"))
    inspected_file = config.get("inspected_file") or (dd / "batch-inspected.json")
    tracker = progress_tracker(mode="draft", total_items=count, data_dir=dd)
    emails: list[dict[str, Any]] = []

    if not config.get("force_fetch") and not date and not query and Path(inspected_file).exists():
        try:
            with Path(inspected_file).open("r", encoding="utf-8") as inspected_handle:
                inspected_data = json.load(inspected_handle)
            inspected_emails = inspected_data.get("emails", [])
            unprocessed = (
                [email for email in inspected_emails if not email.get("is_known")]
                if skip_known else inspected_emails
            )
            if unprocessed:
                emails = unprocessed[:count]
        except Exception:
            emails = []

    if not emails:
        get_unprocessed = _required_dependency(dependencies, "get_unprocessed_emails")
        emails, _ = get_unprocessed(
            folder=folder,
            target_count=count,
            order=order,
            date=date,
            query=query,
            account=account,
            data_dir=dd,
            skip_known=skip_known,
            preview_lines=preview_lines,
            tracker=tracker,
        )

    tracker.step("classifying_and_checking_sent")
    manifest = draft(
        emails,
        workspace_root=workspace_root,
        sent_lookup=load_sent(dd),
        full_reader=full_reader,
        account=account,
    )
    expected_count = config.get("expected_count", count)
    if isinstance(expected_count, bool) or not isinstance(expected_count, int) or expected_count < 1:
        raise ValueError("expected_count must be a positive integer")
    allow_fewer = config.get("allow_fewer", False)
    if not isinstance(allow_fewer, bool):
        raise ValueError("allow_fewer must be a boolean")
    manifest = add_draft_contract(
        manifest,
        expected_count=expected_count,
        allow_fewer=allow_fewer,
        source_folder=folder,
        account=account,
        skip_known=skip_known,
    )
    output_path = Path(output_file).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(output_path, manifest)
    tracker.complete(f"Drafted {len(manifest.get('items', []))} items to {output_path.name}")
    return {
        "ok": True,
        "mode": "draft",
        "folder": folder,
        "order": order,
        "total_drafted": len(manifest.get("items", [])),
        "expected_count": expected_count,
        "allow_fewer": manifest["allow_fewer"],
        "candidate_count": manifest["candidate_count"],
        "source_folder": folder,
        "account": account,
        "skip_known": skip_known,
        "review": manifest["review"],
        "manifest_file": str(output_path),
        "draft": manifest,
    }
