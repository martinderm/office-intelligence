#!/usr/bin/env python3
"""Unified batch runner for mail-desk operations.

Supports 12 modes:
1. inspect: Parallel/sequential header & preview fetching with deduplication check
2. draft:    Inspect unprocessed emails and draft a ready-to-review batch-manifest.json
3. sync_sent: Index recent Sent Items for reply-status reconciliation
4. execute:  Coupled routing, target verification, index upsert, logging, evidence
5. verify:   Consistency check across final index, action log, evidence, and folders
6. pipeline: End-to-end autonomous cycle (inspect -> classify -> execute -> verify)
7. search:   Global mailbox search by query or message_ids
8. resolve:  Batch resolution and archival of replies-needed and review cases
9. dossier:  Mailbox-read-only, non-executing project dossier inspection handoff
10. dossier_apply: Human-approved, project-bound execute -> verify delegation
11. dossier_synthesis: Source-bound, non-executing project synthesis work-order
12. dossier_handoff: Review-only Cloud-Atlas and Task-Desk project handoffs
"""

from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
from pathlib import Path
import sys
import time
from typing import Any, Callable

# Add parent directory to sys.path if invoked directly
_script_dir = Path(__file__).resolve().parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))

from core import (
    atomic_write_json,
    BatchProgressTracker,
    append_action_log_entry,
    append_replies_needed_entry,
    auto_resolve_replies_from_sent,
    backfill_index_signatures,
    build_signature_index,
    check_if_replied,
    classify_email,
    draft_manifest,
    extract_email_address,
    flush_batch_evidence,
    get_single_email_details,
    load_catalogs,
    load_final_index,
    load_sent_index,
    normalize_message_id,
    normalize_signature_text,
    resolve_data_dir,
    resolve_final_index_path,
    run_himalaya,
    save_final_index_atomic,
    sync_sent_items,
    update_evidence_file,
    utc_now_iso,
    verify_in_target_folder,
)
from core.envelope import build_error, build_success, emit_json
from core.modes import (
    run_draft_mode as _run_draft_mode,
    run_dossier_apply_mode as _run_dossier_apply_mode,
    run_dossier_handoff_mode as _run_dossier_handoff_mode,
    run_dossier_synthesis_mode as _run_dossier_synthesis_mode,
    run_dossier_mode as _run_dossier_mode,
    run_execute_mode as _run_execute_mode,
    run_inspect_mode as _run_inspect_mode,
    run_pipeline_mode as _run_pipeline_mode,
    run_resolve_mode,
    run_search_mode,
    run_sync_sent_mode as _run_sync_sent_mode,
    run_verify_mode as _run_verify_mode,
)


# ==============================================================================
# Helper / Envelope Fetching Functions
# ==============================================================================

def get_envelopes_list(folder: str, account: str | None = None, max_size: int = 1000) -> list[dict[str, Any]]:
    """Fetch envelope list from a folder with resilient page sizing."""
    for test_size in [str(max_size), "500", "200", "100", "50"]:
        try:
            out = run_himalaya(["-o", "json", "envelope", "list", "-f", folder, "-s", test_size], account=account, timeout=30)
            if "[" in out:
                out = out[out.find("["):]
            envs = json.loads(out)
            if isinstance(envs, list) and envs:
                return envs
        except Exception:
            continue
    return []


def get_oldest_envelopes(folder: str, count: int, account: str | None = None) -> list[dict[str, Any]]:
    """Fetch the absolute oldest envelopes from a folder reliably using order by date asc."""
    try:
        out = run_himalaya(
            ["-o", "json", "envelope", "list", "-f", folder, "-s", str(count), "order", "by", "date", "asc"],
            account=account,
            timeout=90,
            max_retries=2,
        )
        if "[" in out:
            out = out[out.find("["):]
        envs = json.loads(out)
        if isinstance(envs, list) and envs:
            return envs[:count]
    except Exception:
        pass

    # Fallback to window-based slicing if server-side sort is unavailable
    for test_size in [str(count), str(max(count, 150)), "200", "100"]:
        try:
            out = run_himalaya(["-o", "json", "envelope", "list", "-f", folder, "-s", test_size], account=account, timeout=180, max_retries=2)
            if "[" in out:
                out = out[out.find("["):]
            envs = json.loads(out)
            if isinstance(envs, list) and envs:
                slice_count = min(count, len(envs))
                oldest = envs[-slice_count:]
                oldest.reverse()
                return oldest
        except Exception:
            continue
    return []


def get_oldest_envelope_ids(folder: str, count: int, account: str | None = None) -> list[str]:
    envs = get_oldest_envelopes(folder, count, account=account)
    return [str(e["id"]) for e in envs]


def _envelope_date_key(envelope: dict[str, Any]) -> tuple[float, int]:
    """Return a timezone-normalized timestamp and deterministic ID tie-breaker.

    Himalaya emits RFC 5322 dates, which cannot be ordered lexicographically
    (weekday and timezone prefixes vary).  ISO-like dates are accepted as a
    defensive fallback for adapters that serialize dates differently.
    """
    raw_date = str(envelope.get("date") or "").strip()
    try:
        parsed = parsedate_to_datetime(raw_date)
    except (TypeError, ValueError, IndexError, OverflowError):
        try:
            parsed = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
        except ValueError:
            parsed = None

    if parsed is None:
        timestamp = float("inf")
    else:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        timestamp = parsed.timestamp()

    try:
        envelope_id = int(str(envelope.get("id", "")))
    except (TypeError, ValueError):
        envelope_id = 0
    return timestamp, envelope_id


def _sort_envelopes_by_date(envelopes: list[dict[str, Any]], order: str) -> list[dict[str, Any]]:
    """Sort valid dates chronologically and keep undated messages last."""
    dated = [envelope for envelope in envelopes if _envelope_date_key(envelope)[0] != float("inf")]
    undated = [envelope for envelope in envelopes if _envelope_date_key(envelope)[0] == float("inf")]
    dated.sort(key=_envelope_date_key, reverse=order == "newest")
    undated.sort(key=_envelope_date_key)
    return dated + undated


def _retry_permission_error(operation: Callable[[], Any], *, attempts: int = 3, initial_delay: float = 0.1) -> Any:
    """Retry transient Windows file locks with bounded exponential backoff."""
    for attempt in range(attempts):
        try:
            return operation()
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(initial_delay * (2**attempt))


def _known_mail_fetch_sizes(target_count: int) -> list[int]:
    """Return unique, increasing envelope-list sizes for known-mail skipping.

    Normal batches expand through the documented 2,500-envelope ceiling.  A
    caller requesting more than 2,500 receives one final fetch sized to its
    requested target, rather than a silently truncated batch.
    """
    target = max(target_count, 1)
    ceiling = max(2_500, target)
    first_size = min(max(target * 2, 25), ceiling)
    tiers = (25, 50, 150, 300, 600, 1_200, 2_500)
    inner_tiers = [size for size in tiers if first_size < size < ceiling]
    return sorted({first_size, *inner_tiers, ceiling})


def get_unprocessed_emails(
    folder: str,
    target_count: int,
    order: str = "oldest",
    date: str | None = None,
    query: str | None = None,
    account: str | None = None,
    data_dir: Path | None = None,
    skip_known: bool = True,
    preview_lines: int = 30,
    tracker: BatchProgressTracker | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Fetch target_count unprocessed (or all) emails in oldest/newest order or by date/query reliably."""
    dd = data_dir or resolve_data_dir()
    index_data = load_final_index(resolve_final_index_path(data_dir=dd))
    known_items = index_data.get("items", {})
    signature_index = build_signature_index(dd)

    collected: list[dict[str, Any]] = []
    known_count = 0
    inspected_eids: set[str] = set()

    # Fast direct path if date or query filter is provided
    if date or query:
        if date and query:
            raise ValueError("date and query filters are mutually exclusive")
        search_arg = f"date {date}" if date else str(query)
        if order == "oldest" and "order by" not in search_arg.lower():
            search_arg = f"{search_arg} order by date asc"
        elif order == "newest" and "order by" not in search_arg.lower():
            search_arg = f"{search_arg} order by date desc"
        if tracker:
            tracker.step(f"listing_envelopes ({search_arg})")
        out = run_himalaya(
            ["-o", "json", "envelope", "list", "-f", folder, "-s", str(max(target_count * 2, 50)), search_arg],
            account=account,
            timeout=45,
            max_retries=2,
        )
        if "[" in out:
            out = out[out.find("["):]
        if not out.strip().startswith("["):
            raise RuntimeError(f"Himalaya did not return a valid envelope list for '{search_arg}': {out.strip()[:300]}")
        try:
            candidate_envs = json.loads(out)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Himalaya returned invalid envelope JSON for '{search_arg}': {exc.msg}") from exc
        if not isinstance(candidate_envs, list):
            raise RuntimeError(f"Himalaya did not return an envelope list for '{search_arg}'")

        candidate_envs = _sort_envelopes_by_date(candidate_envs, order)

        for env in candidate_envs:
            eid = str(env.get("id"))
            if eid in inspected_eids:
                continue
            inspected_eids.add(eid)

            # Fast O(1) in-memory signature check before making any IMAP network calls
            env_subj = normalize_signature_text(env.get("subject"))
            from_info = env.get("from")
            from_addr_str = from_info.get("addr") if isinstance(from_info, dict) else str(from_info or "")
            env_from = extract_email_address(from_addr_str)
            matched_entry = signature_index.get((env_subj, env_from)) or signature_index.get((env_subj, ""))

            if matched_entry and skip_known:
                known_count += 1
                if tracker:
                    tracker.step("skipping_known_email", envelope_id=eid, subject=env.get("subject", ""))
                continue

            if tracker:
                tracker.step("inspecting_email", envelope_id=eid, subject=env.get("subject", ""))
            email_res = get_single_email_details(eid, folder, account, preview_lines, fallback_envelope=env)
            mid = email_res.get("message_id")
            is_known = bool((mid and mid in known_items) or matched_entry)

            if is_known:
                known_count += 1
                email_res["is_known"] = True
                email_res["known_location"] = (
                    (known_items.get(mid, {}) if mid else {}).get("final_folder")
                    or (matched_entry.get("final_folder") if matched_entry else None)
                )
                if skip_known:
                    if tracker:
                        tracker.step("skipping_known_email", envelope_id=eid, subject=email_res.get("subject", ""))
                    continue
            else:
                email_res["is_known"] = False
                email_res["known_location"] = None

            collected.append(email_res)
            if tracker:
                tracker.advance_item(envelope_id=eid, subject=email_res.get("subject", ""), step_name="inspected")
            time.sleep(0.02)

            if len(collected) >= target_count:
                return collected, known_count

        return collected, known_count

    fetch_sizes = _known_mail_fetch_sizes(target_count) if skip_known else [target_count]

    for f_size in fetch_sizes:
        if tracker:
            tracker.step(f"listing_envelopes (fetch_count={f_size})")
        candidate_envs = get_oldest_envelopes(folder, f_size, account=account)
        if not candidate_envs:
            return [], 0

        if order != "oldest":
            candidate_envs.reverse()

        for env in candidate_envs:
            eid = str(env.get("id"))
            if eid in inspected_eids:
                continue
            inspected_eids.add(eid)

            # Fast O(1) in-memory signature check before making any IMAP network calls
            env_subj = normalize_signature_text(env.get("subject"))
            from_info = env.get("from")
            from_addr_str = from_info.get("addr") if isinstance(from_info, dict) else str(from_info or "")
            env_from = extract_email_address(from_addr_str)
            matched_entry = signature_index.get((env_subj, env_from)) or signature_index.get((env_subj, ""))

            if matched_entry and skip_known:
                known_count += 1
                if tracker:
                    tracker.step("skipping_known_email", envelope_id=eid, subject=env.get("subject", ""))
                continue

            if tracker:
                tracker.step("inspecting_email", envelope_id=eid, subject=env.get("subject", ""))
            email_res = get_single_email_details(eid, folder, account, preview_lines, fallback_envelope=env)
            mid = email_res.get("message_id")
            is_known = bool((mid and mid in known_items) or matched_entry)

            if is_known:
                known_count += 1
                email_res["is_known"] = True
                email_res["known_location"] = (
                    (known_items.get(mid, {}) if mid else {}).get("final_folder")
                    or (matched_entry.get("final_folder") if matched_entry else None)
                )
                if skip_known:
                    if tracker:
                        tracker.step("skipping_known_email", envelope_id=eid, subject=email_res.get("subject", ""))
                    continue
            else:
                email_res["is_known"] = False
                email_res["known_location"] = None

            collected.append(email_res)
            if tracker:
                tracker.advance_item(envelope_id=eid, subject=email_res.get("subject", ""), step_name="inspected")
            time.sleep(0.02)

            if len(collected) >= target_count:
                return collected, known_count

        if len(collected) >= target_count or len(candidate_envs) < f_size:
            return collected, known_count

    return collected, known_count


# ==============================================================================
# Inspect Mode
# ==============================================================================

def run_inspect_mode(
    config: dict[str, Any],
    account: str | None = None,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    """Compatibility facade for the extracted inspect-mode handler.

    Dependencies are resolved at call time so existing runner-level patches stay
    effective during the incremental modularization.
    """
    return _run_inspect_mode(
        config,
        account=account,
        data_dir=data_dir,
        dependencies={
            "atomic_write_json": atomic_write_json,
            "draft_manifest": draft_manifest,
            "get_oldest_envelopes": get_oldest_envelopes,
            "get_single_email_details": get_single_email_details,
            "get_unprocessed_emails": get_unprocessed_emails,
            "load_final_index": load_final_index,
            "resolve_data_dir": resolve_data_dir,
            "resolve_final_index_path": resolve_final_index_path,
            "run_himalaya": run_himalaya,
            "sleep": time.sleep,
        },
    )


# ==============================================================================
# Draft Mode
# ==============================================================================

def run_draft_mode(
    config: dict[str, Any],
    account: str | None = None,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    """Compatibility facade for the extracted draft-mode handler."""
    return _run_draft_mode(
        config,
        account=account,
        data_dir=data_dir,
        dependencies={
            "BatchProgressTracker": BatchProgressTracker,
            "atomic_write_json": atomic_write_json,
            "draft_manifest": draft_manifest,
            "get_unprocessed_emails": get_unprocessed_emails,
            "get_single_email_details": get_single_email_details,
            "load_sent_index": load_sent_index,
            "resolve_data_dir": resolve_data_dir,
        },
    )


def run_dossier_mode(
    config: dict[str, Any],
    account: str | None = None,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    """Compatibility facade for the mailbox-read-only dossier-mode handler."""
    return _run_dossier_mode(
        config,
        account=account,
        data_dir=data_dir,
        dependencies={
            "atomic_write_json": atomic_write_json,
            "load_catalogs": load_catalogs,
        },
    )


def run_dossier_apply_mode(
    config: dict[str, Any],
    account: str | None = None,
    data_dir: Path | None = None,
    index_path: Path | None = None,
) -> dict[str, Any]:
    """Compatibility facade for the reviewed dossier execution handler."""
    return _run_dossier_apply_mode(
        config,
        account=account,
        data_dir=data_dir,
        index_path=index_path,
        dependencies={
            "load_catalogs": load_catalogs,
            "normalize_message_id": normalize_message_id,
            "resolve_data_dir": resolve_data_dir,
            "run_execute_mode": run_execute_mode,
            "run_verify_mode": run_verify_mode,
        },
    )


def run_dossier_synthesis_mode(
    config: dict[str, Any],
    account: str | None = None,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    """Compatibility facade for the source-bound dossier synthesis work-order."""
    return _run_dossier_synthesis_mode(
        config,
        account=account,
        data_dir=data_dir,
        dependencies={
            "atomic_write_json": atomic_write_json,
            "load_catalogs": load_catalogs,
            "normalize_message_id": normalize_message_id,
            "resolve_data_dir": resolve_data_dir,
        },
    )


def run_dossier_handoff_mode(
    config: dict[str, Any],
    account: str | None = None,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    """Compatibility facade for review-only downstream dossier handoffs."""
    return _run_dossier_handoff_mode(
        config,
        account=account,
        data_dir=data_dir,
        dependencies={
            "atomic_write_json": atomic_write_json,
            "load_catalogs": load_catalogs,
            "normalize_message_id": normalize_message_id,
            "resolve_data_dir": resolve_data_dir,
        },
    )


# ==============================================================================
# Sync Sent Mode
# ==============================================================================

def run_sync_sent_mode(
    config: dict[str, Any],
    account: str | None = None,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    """Compatibility facade for the extracted sent-items synchronization handler."""
    return _run_sync_sent_mode(
        config,
        account=account,
        data_dir=data_dir,
        dependencies={
            "resolve_data_dir": resolve_data_dir,
            "sync_sent_items": sync_sent_items,
        },
    )


# ==============================================================================
# Execute Mode
# ==============================================================================

def run_execute_mode(
    config: dict[str, Any],
    account: str | None = None,
    data_dir: Path | None = None,
    index_path: Path | None = None,
) -> dict[str, Any]:
    """Compatibility facade for the extracted coupled execute-mode handler."""
    return _run_execute_mode(
        config,
        account=account,
        data_dir=data_dir,
        index_path=index_path,
        dependencies={
            "BatchProgressTracker": BatchProgressTracker,
            "append_action_log_entry": append_action_log_entry,
            "append_replies_needed_entry": append_replies_needed_entry,
            "auto_resolve_replies_from_sent": auto_resolve_replies_from_sent,
            "flush_batch_evidence": flush_batch_evidence,
            "load_final_index": load_final_index,
            "normalize_message_id": normalize_message_id,
            "resolve_data_dir": resolve_data_dir,
            "resolve_final_index_path": resolve_final_index_path,
            "run_himalaya": run_himalaya,
            "save_final_index_atomic": save_final_index_atomic,
            "sleep": time.sleep,
            "utc_now_iso": utc_now_iso,
            "verify_in_target_folder": verify_in_target_folder,
        },
    )


# ==============================================================================
# Verify Mode
# ==============================================================================

def run_verify_mode(
    config: dict[str, Any],
    account: str | None = None,
    data_dir: Path | None = None,
    index_path: Path | None = None,
) -> dict[str, Any]:
    """Compatibility facade for the extracted consistency-verification handler."""
    return _run_verify_mode(
        config,
        account=account,
        data_dir=data_dir,
        index_path=index_path,
        dependencies={
            "atomic_write_json": atomic_write_json,
            "load_final_index": load_final_index,
            "normalize_message_id": normalize_message_id,
            "resolve_data_dir": resolve_data_dir,
            "resolve_final_index_path": resolve_final_index_path,
            "verify_in_target_folder": verify_in_target_folder,
        },
    )


# ==============================================================================
# Pipeline Mode (Autonomous End-to-End Cycle)
# ==============================================================================

def run_pipeline_mode(
    config: dict[str, Any],
    account: str | None = None,
    data_dir: Path | None = None,
    index_path: Path | None = None,
) -> dict[str, Any]:
    """Compatibility facade for the extracted autonomous pipeline handler."""
    return _run_pipeline_mode(
        config,
        account=account,
        data_dir=data_dir,
        index_path=index_path,
        dependencies={
            "draft_manifest": draft_manifest,
            "get_unprocessed_emails": get_unprocessed_emails,
            "get_single_email_details": get_single_email_details,
            "load_sent_index": load_sent_index,
            "resolve_data_dir": resolve_data_dir,
            "run_execute_mode": run_execute_mode,
            "run_verify_mode": run_verify_mode,
            "sync_sent_items": sync_sent_items,
        },
    )


# ==============================================================================
# Main Entry Point
# ==============================================================================

CANONICAL_ACTION = "batch_runner"
MODE_ALIASES = {
    "inspect": "inspect",
    "fetch": "inspect",
    "draft": "draft",
    "propose": "draft",
    "dossier": "dossier",
    "dossier_apply": "dossier_apply",
    "dossier_synthesis": "dossier_synthesis",
    "dossier_handoff": "dossier_handoff",
    "sync_sent": "sync_sent",
    "sync-sent": "sync_sent",
    "sent": "sync_sent",
    "pipeline": "pipeline",
    "auto": "pipeline",
    "execute": "execute",
    "process": "execute",
    "verify": "verify",
    "validate": "verify",
    "check": "verify",
    "search": "search",
    "locate": "search",
    "find": "search",
    "resolve": "resolve",
    "archive": "resolve",
}


class ArgumentParseError(ValueError):
    """An argparse failure that can be reported through the JSON contract."""


class EnvelopeArgumentParser(argparse.ArgumentParser):
    """Keep argparse diagnostics off stdout so it remains machine-readable."""

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        sys.stderr.write(f"{self.prog}: error: {message}\n")
        raise ArgumentParseError(message)


def _legacy_result_data(result: dict[str, Any], operation: str) -> dict[str, Any]:
    """Move legacy runner result fields below the canonical envelope boundary."""
    data = {key: value for key, value in result.items() if key not in {"ok", "mode", "message"}}
    data["operation"] = operation
    return data


def _is_partial_failure(result: dict[str, Any]) -> bool:
    """Recognize mixed item outcomes, including verify and pipeline summaries."""
    execute_summary = result.get("execute_summary")
    verify_summary = result.get("verify_summary")
    # A fully completed execute stage may already have routed and persisted
    # mutations.  If its follow-up verification stage then fails wholesale,
    # the pipeline is still only partially complete even without mixed lists.
    if (
        isinstance(execute_summary, dict)
        and execute_summary.get("ok") is True
        and isinstance(verify_summary, dict)
        and verify_summary.get("ok") is False
    ):
        return True

    results = result.get("results")
    if isinstance(results, list):
        outcomes = [
            entry[key]
            for entry in results
            if isinstance(entry, dict)
            for key in ("success", "resolved", "consistent")
            if isinstance(entry.get(key), bool)
        ]
        if any(outcome is True for outcome in outcomes) and any(outcome is False for outcome in outcomes):
            return True

    return any(
        isinstance(summary, dict) and _is_partial_failure(summary)
        for summary in (execute_summary, verify_summary)
    )


def _result_envelope(result: dict[str, Any], operation: str) -> dict[str, Any]:
    """Translate a legacy in-process result into the OI-10 CLI envelope."""
    data = _legacy_result_data(result, operation)
    if bool(result.get("ok")):
        return build_success(
            CANONICAL_ACTION,
            result.get("message") or f"Batch runner {operation} completed.",
            data,
        )

    partial = _is_partial_failure(result)
    state = "PartialFailure" if partial else "Failed"
    message = result.get("message") or (
        f"Batch runner {operation} completed with partial failures."
        if partial
        else f"Batch runner {operation} failed."
    )
    return build_error(
        CANONICAL_ACTION,
        message,
        data,
        error_type="PartialFailure" if partial else "OperationFailed",
        state=state,
    )


def _emit(envelope: dict[str, Any]) -> bool:
    """Emit exactly one canonical envelope, including serialization failures."""
    try:
        emit_json(envelope)
        return True
    except Exception as exc:  # noqa: BLE001 - final CLI boundary must stay JSON-only
        try:
            emit_json(
                build_error(
                    CANONICAL_ACTION,
                    "Unable to serialize batch runner result.",
                    error_type="SerializationError",
                    error_details={"exception_type": type(exc).__name__},
                )
            )
        except Exception:  # noqa: BLE001 - stdout may be unavailable, but never emit a non-envelope
            pass
        return False


def _serialization_error_envelope(exc: Exception, operation: str) -> dict[str, Any]:
    """Build the recoverable error used before any success-only cleanup."""
    return build_error(
        CANONICAL_ACTION,
        "Unable to serialize batch runner result.",
        {"operation": operation},
        error_type="SerializationError",
        error_details={"exception_type": type(exc).__name__},
    )


def _is_json_serializable(envelope: dict[str, Any]) -> tuple[bool, Exception | None]:
    """Preflight stdout serialization before deleting a successful manifest."""
    try:
        json.dumps(envelope, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        return False, exc
    return True, None


def _failure_envelope(
    message: object,
    *,
    error_type: str,
    operation: str = "argument_parse",
    state: str = "Failed",
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return build_error(
        CANONICAL_ACTION,
        message,
        {"operation": operation},
        error_type=error_type,
        error_details=details,
        state=state,
    )


def _build_parser() -> EnvelopeArgumentParser:
    parser = EnvelopeArgumentParser(
        add_help=False,
        description="Unified batch runner for mail-desk (inspect, draft, dossier, dossier_apply, dossier_synthesis, execute, verify, pipeline, search, resolve).",
    )
    parser.add_argument("-h", "--help", action="store_true", help="Show JSON-compatible CLI help metadata")
    parser.add_argument("--input", "-i", help="Path to input JSON file in data/")
    parser.add_argument("--stdin", action="store_true", help="Read JSON configuration from stdin")
    parser.add_argument("--account", "-a", help="Himalaya account override")
    parser.add_argument("--data-dir", help="Override path to data/mail-desk/")
    parser.add_argument("--index", help="Override path to final-location-index.json")
    parser.add_argument("--keep-input", action="store_true", help="Do not delete input file on success")

    # CLI Direct Modes
    parser.add_argument("--pipeline", "-p", type=int, nargs="?", const=20, help="Run autonomous pipeline for N items")
    parser.add_argument("--draft", "-d", type=int, nargs="?", const=20, help="Inspect N items and draft batch-manifest.json")
    parser.add_argument("--inspect", nargs="?", const=20, type=int, help="Inspect N emails")
    parser.add_argument("--dossier", metavar="PROJECT_ID", help="Prepare a mailbox-read-only project dossier inspection handoff")
    parser.add_argument("--max-count", type=int, help="Maximum dossier matches (1-50; default: 50)")
    parser.add_argument("--sync-sent", nargs="?", const=150, type=int, help="Fetch and index N recent Sent Items")
    parser.add_argument("--resolve", "-r", action="store_true", help="Auto-audit and resolve replies-needed cases against Sent Items")
    parser.add_argument("--order", choices=["oldest", "newest"], default="oldest", help="Processing order (default: oldest)")
    parser.add_argument("--folder", "-f", default="INBOX", help="Target mailbox folder (default: INBOX)")
    parser.add_argument("--skip-known", action="store_true", default=True, help="Skip already processed emails")
    parser.add_argument("--no-skip-known", dest="skip_known", action="store_false", help="Do not skip known emails")
    filters = parser.add_mutually_exclusive_group()
    filters.add_argument("--query", "-q", help="Search query filter for envelopes (e.g. 'after 2026-05-01')")
    filters.add_argument("--date", help="Exact date filter for envelopes (YYYY-MM-DD)")
    parser.add_argument("--min-confidence", choices=["high", "medium", "low"], default="high", help="Minimum confidence threshold for pipeline auto-execution")
    parser.add_argument("--expected-count", type=int, help="Required selected count for --draft review manifest (defaults to N)")
    parser.add_argument("--allow-fewer", action="store_true", help="Permit fewer reviewed candidates than --expected-count for --draft")
    return parser


def _direct_mode_config(args: argparse.Namespace, data_dir: Path) -> dict[str, Any] | None:
    if (args.expected_count is not None or args.allow_fewer) and args.draft is None:
        raise ArgumentParseError("--expected-count/--allow-fewer require --draft")
    if args.dossier is not None:
        if args.query or args.date:
            raise ArgumentParseError("--dossier derives its query from the project catalog; --query/--date are not allowed")
        if any((args.pipeline is not None, args.draft is not None, args.inspect is not None, args.sync_sent is not None, args.resolve)):
            raise ArgumentParseError("--dossier cannot be combined with another direct mode")
        return {
            "mode": "dossier",
            "project": args.dossier,
            "source_folder": args.folder,
            "max_count": args.max_count,
            "auto_query_from_catalog": True,
            "delete_input_on_success": False,
        }
    if args.max_count is not None:
        raise ArgumentParseError("--max-count requires --dossier")
    if args.pipeline is not None:
        cfg = {
            "mode": "pipeline",
            "count": args.pipeline,
            "order": args.order,
            "folder": args.folder,
            "skip_known": args.skip_known,
            "min_confidence": args.min_confidence,
            "verify": True,
        }
        if args.query:
            cfg["query"] = args.query
        if args.date:
            cfg["date"] = args.date
        return cfg
    if args.sync_sent is not None:
        return {"mode": "sync_sent", "count": args.sync_sent, "folder": "Sent Items"}
    if args.resolve:
        return {"mode": "resolve", "auto_from_sent": True}
    if args.draft is not None:
        expected_count = args.expected_count if args.expected_count is not None else args.draft
        if expected_count < 1:
            raise ArgumentParseError("--expected-count must be a positive integer")
        if expected_count != args.draft:
            raise ArgumentParseError("--expected-count must equal the requested --draft count")
        cfg = {
            "mode": "draft",
            "count": args.draft,
            "order": args.order,
            "folder": args.folder,
            "skip_known": args.skip_known,
            "expected_count": expected_count,
            "allow_fewer": args.allow_fewer,
            "output_file": str(data_dir / "batch-manifest.json"),
        }
        if args.query:
            cfg["query"] = args.query
        if args.date:
            cfg["date"] = args.date
        return cfg
    if args.inspect is not None:
        cfg = {
            "mode": "inspect",
            "count": args.inspect,
            "order": args.order,
            "folder": args.folder,
            "skip_known": args.skip_known,
            "output_file": str(data_dir / "batch-inspected.json"),
        }
        if args.query:
            cfg["query"] = args.query
        if args.date:
            cfg["date"] = args.date
        return cfg
    return None


def _load_configuration(args: argparse.Namespace, data_dir: Path) -> tuple[dict[str, Any], Path | None]:
    """Load direct, stdin, explicit, or auto-discovered input without printing."""
    direct = _direct_mode_config(args, data_dir)
    if direct is not None:
        return direct, None

    if args.query or args.date:
        raise ArgumentParseError("--query/--date require --inspect, --draft, or --pipeline; put filters in a manifest instead")

    if args.stdin:
        raw = sys.stdin.read().strip()
        if not raw:
            raise ArgumentParseError("stdin payload is empty")
        try:
            config = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ArgumentParseError(f"stdin payload is not valid JSON: {exc.msg}") from exc
        return config, None

    if args.input:
        input_path = Path(args.input).expanduser().resolve()
        if not input_path.exists():
            raise FileNotFoundError(f"Input file not found: {input_path}")
        try:
            return _retry_permission_error(
                lambda: json.loads(input_path.read_text(encoding="utf-8")),
            ), input_path
        except json.JSONDecodeError as exc:
            raise ArgumentParseError(f"Input file is not valid JSON: {exc.msg}") from exc

    candidates = [
        data_dir / "batch-manifest.json",
        data_dir / "batch-pipeline.json",
        data_dir / "batch-draft.json",
        data_dir / "batch-dossier-request.json",
        data_dir / "batch-dossier-apply.json",
        data_dir / "batch-dossier-synthesis-request.json",
        data_dir / "batch-dossier-handoff-request.json",
        data_dir / "batch-inspect.json",
        data_dir / "batch-verify.json",
        data_dir / "batch-search.json",
        data_dir / "batch-resolve.json",
        data_dir / "batch-sync-sent.json",
    ]
    for candidate in candidates:
        if candidate.exists():
            try:
                return _retry_permission_error(
                    lambda: json.loads(candidate.read_text(encoding="utf-8")),
                ), candidate
            except json.JSONDecodeError as exc:
                raise ArgumentParseError(f"Auto-discovered input is not valid JSON: {exc.msg}") from exc

    candidate_names = ", ".join(candidate.name for candidate in candidates)
    raise FileNotFoundError(
        "Neither --input nor --stdin was provided, and no default input file "
        f"({candidate_names}) was found in {data_dir}."
    )


def _dispatch(
    config: dict[str, Any],
    *,
    account: str | None,
    data_dir: Path,
    index_path: Path,
) -> tuple[dict[str, Any], str]:
    raw_mode = config.get("mode", "execute")
    if not isinstance(raw_mode, str):
        raise ArgumentParseError("mode must be a string")
    mode = raw_mode.lower()
    operation = MODE_ALIASES.get(mode)
    if operation is None:
        raise ArgumentParseError(f"Unsupported mode: {mode}")

    # The runner has always produced a JSON result.  Redirect legacy progress
    # messages while it runs so stdout contains exactly that one result envelope.
    with contextlib.redirect_stdout(sys.stderr):
        if operation == "inspect":
            return run_inspect_mode(config, account=account, data_dir=data_dir), operation
        if operation == "draft":
            return run_draft_mode(config, account=account, data_dir=data_dir), operation
        if operation == "dossier":
            return run_dossier_mode(config, account=account, data_dir=data_dir), operation
        if operation == "dossier_apply":
            return run_dossier_apply_mode(config, account=account, data_dir=data_dir, index_path=index_path), operation
        if operation == "dossier_synthesis":
            return run_dossier_synthesis_mode(config, account=account, data_dir=data_dir), operation
        if operation == "dossier_handoff":
            return run_dossier_handoff_mode(config, account=account, data_dir=data_dir), operation
        if operation == "sync_sent":
            return run_sync_sent_mode(config, account=account, data_dir=data_dir), operation
        if operation == "pipeline":
            return run_pipeline_mode(config, account=account, data_dir=data_dir, index_path=index_path), operation
        if operation == "execute":
            return run_execute_mode(config, account=account, data_dir=data_dir, index_path=index_path), operation
        if operation == "verify":
            return run_verify_mode(config, account=account, data_dir=data_dir, index_path=index_path), operation
        if operation == "search":
            return run_search_mode(config, account=account, data_dir=data_dir), operation
        return run_resolve_mode(config, data_dir=data_dir), operation


def main() -> int:
    try:
        parser = _build_parser()
        args = parser.parse_args()
        if args.help:
            sys.stderr.write(parser.format_help())
            _emit(build_success(CANONICAL_ACTION, "Batch runner CLI help.", {"operation": "help"}))
            return 0

        data_dir = resolve_data_dir(args.data_dir)
        config, input_path = _load_configuration(args, data_dir)
        if not isinstance(config, dict):
            raise ArgumentParseError("configuration must be a JSON object")
        if str(config.get("mode", "")).lower() in {"dossier", "dossier_apply", "dossier_synthesis", "dossier_handoff"}:
            # The result is a review artifact; retain its request unless a caller
            # attempts the explicitly rejected destructive lifecycle override.
            config.setdefault("delete_input_on_success", False)

        index_path = resolve_final_index_path(args.index, data_dir=data_dir)
        account = args.account or config.get("account")
        result, operation = _dispatch(
            config,
            account=account,
            data_dir=data_dir,
            index_path=index_path,
        )
        if not isinstance(result, dict):
            raise TypeError("mode handler returned a non-object result")

        # A handler result must be serializable before it can count as a
        # successful run.  In particular, do this before deleting its manifest:
        # a caller needs that input to recover from a serialization failure.
        envelope = _result_envelope(result, operation)
        serializable, serialization_error = _is_json_serializable(envelope)
        if not serializable:
            envelope = _serialization_error_envelope(serialization_error, operation)

        # Preserve manifests unless the complete mode result succeeded.  A
        # cleanup failure is a partial failure because the requested lifecycle
        # was not fully completed, but the input remains available for recovery.
        delete_input = config.get("delete_input_on_success", True) and not args.keep_input
        cleanup_failure: Exception | None = None
        input_deleted = False
        if input_path is not None and input_path.exists() and delete_input and envelope["success"]:
            try:
                _retry_permission_error(input_path.unlink)
                input_deleted = True
            except Exception as exc:  # noqa: BLE001 - report cleanup under the envelope
                cleanup_failure = exc

        if input_path is not None:
            result["input_file_deleted"] = input_deleted
            if envelope["success"]:
                envelope["data"]["input_file_deleted"] = input_deleted
        if cleanup_failure is not None:
            result["ok"] = False
            result["message"] = "Batch completed, but input manifest cleanup failed."
            result["cleanup_error"] = str(cleanup_failure)
            envelope = build_error(
                CANONICAL_ACTION,
                result["message"],
                _legacy_result_data(result, operation),
                error_type="CleanupFailure",
                error_details={"exception_type": type(cleanup_failure).__name__},
                state="PartialFailure",
            )
        emitted = _emit(envelope)
        return 0 if emitted and envelope["success"] else 1
    except ArgumentParseError as exc:
        _emit(_failure_envelope(str(exc), error_type="ArgumentError"))
        return 2
    except FileNotFoundError as exc:
        _emit(_failure_envelope(str(exc), error_type="InputNotFound", operation="input_load", state="NotFound"))
        return 1
    except Exception as exc:  # noqa: BLE001 - every CLI failure must retain the envelope contract
        _emit(
            _failure_envelope(
                str(exc) or type(exc).__name__,
                error_type=type(exc).__name__,
                operation="runtime",
                details={"exception_type": type(exc).__name__},
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
