"""End-to-end orchestration handler for mail-desk batches."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping

from ..classifier import draft_manifest
from ..common import resolve_data_dir
from ..himalaya import get_single_email_details
from ..sent_indexer import load_sent_index, sync_sent_items
from ..synthesis_handoff import canonicalize_synthesis_handoff, empty_synthesis_handoff
from ..telemetry import canonicalize_telemetry, empty_telemetry
from .execute import run_execute_mode
from .verify import run_verify_mode


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


def run_pipeline_mode(
    config: dict[str, Any],
    account: str | None = None,
    data_dir: Path | None = None,
    index_path: Path | None = None,
    *,
    dependencies: Mapping[str, Callable[..., Any]] | None = None,
) -> dict[str, Any]:
    """Inspect, classify, execute eligible items, and optionally verify them."""
    resolve_data = _dependency(dependencies, "resolve_data_dir", resolve_data_dir)
    get_unprocessed = _required_dependency(dependencies, "get_unprocessed_emails")
    sync_sent = _dependency(dependencies, "sync_sent_items", sync_sent_items)
    load_sent = _dependency(dependencies, "load_sent_index", load_sent_index)
    draft = _dependency(dependencies, "draft_manifest", draft_manifest)
    full_reader = _dependency(dependencies, "get_single_email_details", get_single_email_details)
    execute = _dependency(dependencies, "run_execute_mode", run_execute_mode)
    verify = _dependency(dependencies, "run_verify_mode", run_verify_mode)

    dd = data_dir or resolve_data()
    workspace_root = dd.parent.parent
    folder = config.get("folder", "INBOX")
    count = int(config.get("count", 20))
    order = str(config.get("order", "oldest")).lower()
    date = config.get("date")
    query = config.get("query")
    min_confidence = str(config.get("min_confidence", "high")).lower()
    do_verify = bool(config.get("verify", True))
    check_folders = bool(config.get("check_folders", False))
    preview_lines = int(config.get("preview_lines", 30))
    skip_known = bool(config.get("skip_known", True))

    emails, _known_count = get_unprocessed(
        folder=folder,
        target_count=count,
        order=order,
        date=date,
        query=query,
        account=account,
        data_dir=dd,
        skip_known=skip_known,
        preview_lines=preview_lines,
    )
    if not emails:
        return {
            "ok": True,
            "mode": "pipeline",
            "message": "No unprocessed emails found in folder.",
            "total_inspected": 0,
            "executed_count": 0,
            "review_needed_count": 0,
            "telemetry": empty_telemetry(),
            "synthesis_handoff": empty_synthesis_handoff(),
        }

    if config.get("sync_sent", True):
        try:
            sync_sent(
                count=int(config.get("sent_count", 150)), account=account,
                data_dir=dd, workspace_root=workspace_root,
            )
        except Exception as exc:
            return {
                "ok": False, "mode": "pipeline", "folder": folder, "order": order,
                "total_inspected": len(emails), "executed_count": 0,
                "verified_count": 0, "review_needed_count": 0,
                "review_needed_items": [], "all_succeeded": False,
                "execute_summary": None, "verify_summary": None,
                "telemetry": empty_telemetry(),
                "synthesis_handoff": empty_synthesis_handoff(),
                "error": {
                    "type": type(exc).__name__,
                    "message": f"Sent-items synchronization failed: {exc}",
                    "phase": "sync_sent",
                },
            }

    sent_lookup = load_sent(dd)
    draft_result = draft(
        emails,
        workspace_root=workspace_root,
        sent_lookup=sent_lookup,
        full_reader=full_reader,
        account=account,
    )
    all_drafted_items = draft_result.get("items", [])
    confidence_rank = {"high": 3, "medium": 2, "low": 1}
    min_rank = confidence_rank.get(min_confidence, 3)

    executable_items: list[dict[str, Any]] = []
    review_items: list[dict[str, Any]] = []
    for item in all_drafted_items:
        confidence = item.get("decision", {}).get("confidence", "low").lower()
        rank = confidence_rank.get(confidence, 1)
        target_folder = item.get("action", {}).get("target_folder", "INBOX")

        if rank >= min_rank and target_folder != "INBOX":
            executable_items.append(item)
        else:
            review_items.append(item)

    exec_result: dict[str, Any] = {"ok": True, "results": []}
    if executable_items:
        exec_result = execute(
            {"items": executable_items},
            account=account,
            data_dir=dd,
            index_path=index_path,
        )

    # An interrupted execute is explicitly a recovery boundary.  Do not run a
    # follow-up verify or emit a completion-shaped synthesis handoff for it.
    if exec_result.get("status") == "aborted" or exec_result.get("recovery_required"):
        return {
            "ok": False,
            "mode": "pipeline",
            "status": "aborted" if exec_result.get("status") == "aborted" else "recovery_required",
            "recovery_required": True,
            "folder": folder,
            "order": order,
            "total_inspected": len(emails),
            "executed_count": len(executable_items),
            "verified_count": 0,
            "review_needed_count": len(review_items),
            "review_needed_items": review_items,
            "all_succeeded": False,
            "execute_summary": exec_result,
            "verify_summary": None,
            "telemetry": empty_telemetry(),
            "synthesis_handoff": empty_synthesis_handoff(),
        }

    telemetry = canonicalize_telemetry(exec_result.get("telemetry"))
    synthesis_handoff = canonicalize_synthesis_handoff(
        exec_result.get("synthesis_handoff")
    )

    verify_result: dict[str, Any] | None = None
    if do_verify and executable_items:
        verify_result = verify(
            {"items": executable_items, "check_folders": check_folders},
            account=account,
            data_dir=dd,
            index_path=index_path,
        )

    pipeline_ok = bool(exec_result.get("ok", True))
    if verify_result and not verify_result.get("ok", True):
        pipeline_ok = False

    return {
        "ok": pipeline_ok,
        "mode": "pipeline",
        "folder": folder,
        "order": order,
        "total_inspected": len(emails),
        "executed_count": len(executable_items),
        "verified_count": len(verify_result.get("results", [])) if verify_result else 0,
        "review_needed_count": len(review_items),
        "review_needed_items": review_items,
        "all_succeeded": pipeline_ok,
        "execute_summary": exec_result,
        "verify_summary": verify_result,
        "telemetry": telemetry,
        "synthesis_handoff": synthesis_handoff,
    }
