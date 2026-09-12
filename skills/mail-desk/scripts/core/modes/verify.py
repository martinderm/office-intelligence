"""Consistency-verification handler for the mail-desk batch runner."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Mapping

from ..common import atomic_write_json, normalize_message_id, resolve_data_dir, resolve_final_index_path
from ..completion import completion_report, release_synthesis_handoff
from ..himalaya import verify_in_target_folder
from ..index import load_final_index
from ..synthesis_handoff import empty_synthesis_handoff


def _dependency(
    dependencies: Mapping[str, Callable[..., Any]] | None,
    name: str,
    default: Callable[..., Any],
) -> Callable[..., Any]:
    if dependencies and name in dependencies:
        return dependencies[name]
    return default


def _message_ids(rows: object, normalize_id: Callable[[object], str]) -> list[str] | None:
    """Return one exact, duplicate-free normalized source order or fail closed."""
    if not isinstance(rows, list):
        return None
    output: list[str] = []
    for row in rows:
        if not isinstance(row, Mapping):
            return None
        message_id = normalize_id(row.get("message_id") or row.get("raw_message_id") or "")
        if not message_id or message_id in output:
            return None
        output.append(message_id)
    return output


def _verified_execute_candidate(
    execute_summary: object,
    *,
    verify_scope_ids: list[str],
    normalize_id: Callable[[object], str],
) -> object | None:
    """Accept only a complete Execute result as standalone-verify provenance."""
    if not isinstance(execute_summary, Mapping):
        return None
    if (
        execute_summary.get("mode") != "execute"
        or execute_summary.get("ok") is not True
        or execute_summary.get("status") != "completed"
        or execute_summary.get("recovery_required") is not False
    ):
        return None
    results = execute_summary.get("results")
    result_ids = _message_ids(results, normalize_id)
    if result_ids is None or result_ids != verify_scope_ids:
        return None
    if not all(isinstance(row, Mapping) and row.get("success") is True for row in results):
        return None
    candidate = execute_summary.get("synthesis_candidate")
    # The candidate is canonicalized by release_synthesis_handoff.  At this
    # boundary we additionally require every source in a pending candidate to
    # originate from the successful execute/verify scope.
    if not isinstance(candidate, Mapping):
        return None
    raw_items = candidate.get("items")
    if isinstance(raw_items, list):
        candidate_ids = _message_ids(raw_items, normalize_id)
        if candidate.get("status") == "pending" and candidate_ids != verify_scope_ids:
            return None
    return candidate


def run_verify_mode(
    config: dict[str, Any],
    account: str | None = None,
    data_dir: Path | None = None,
    index_path: Path | None = None,
    *,
    dependencies: Mapping[str, Callable[..., Any]] | None = None,
) -> dict[str, Any]:
    """Check index, action log, evidence, and optionally mailbox-folder consistency."""
    resolve_data = _dependency(dependencies, "resolve_data_dir", resolve_data_dir)
    resolve_index_path = _dependency(dependencies, "resolve_final_index_path", resolve_final_index_path)
    load_index = _dependency(dependencies, "load_final_index", load_final_index)
    normalize_id = _dependency(dependencies, "normalize_message_id", normalize_message_id)
    verify_folder = _dependency(dependencies, "verify_in_target_folder", verify_in_target_folder)
    write_json = _dependency(dependencies, "atomic_write_json", atomic_write_json)

    dd = data_dir or resolve_data()
    idx_p = index_path or resolve_index_path(data_dir=dd)
    check_folders = bool(config.get("check_folders", False))
    output_file = config.get("output_file")
    execute_summary = config.get("execute_summary")

    target_items: list[dict[str, Any]] = []
    if "items" in config and isinstance(config["items"], list):
        target_items = config["items"]
    elif "message_ids" in config and isinstance(config["message_ids"], list):
        target_items = [{"message_id": mid} for mid in config["message_ids"]]
    elif "batch_file" in config:
        batch_file = Path(config["batch_file"]).expanduser().resolve()
        if batch_file.exists():
            with batch_file.open("r", encoding="utf-8") as batch_handle:
                batch_data = json.load(batch_handle)
                if isinstance(batch_data, list):
                    target_items = batch_data
                elif isinstance(batch_data, dict):
                    target_items = batch_data.get("items") or batch_data.get("results") or []
                    # A batch file is accepted only when it *is* a complete
                    # Execute result.  Do not trust arbitrary envelope data.
                    if execute_summary is None and batch_data.get("mode") == "execute":
                        execute_summary = batch_data

    index_data = load_index(idx_p)
    index_items = index_data.get("items", {})

    action_log_path = dd / "action-log.jsonl"
    action_log_map: dict[str, dict[str, Any]] = {}
    if action_log_path.exists():
        with action_log_path.open("r", encoding="utf-8") as action_log:
            for line in action_log:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    message_id = normalize_id(row.get("message_id", ""))
                    if message_id:
                        action_log_map[message_id] = row
                except Exception:
                    pass

    workspace_root = dd.parent.parent
    references_root = workspace_root / "memory" / "references"
    results: list[dict[str, Any]] = []
    all_consistent = True

    for item in target_items:
        raw_message_id = item.get("message_id") or item.get("raw_message_id", "")
        message_id = normalize_id(raw_message_id)
        if not message_id:
            continue

        subject = item.get("subject", "")
        expected_folder = item.get("final_folder") or item.get("target_folder")

        index_entry = index_items.get(message_id)
        in_index = index_entry is not None
        indexed_folder = index_entry.get("final_folder") if index_entry else None
        indexed_envelope_id = index_entry.get("envelope_id") if index_entry else None

        log_entry = action_log_map.get(message_id)
        in_action_log = log_entry is not None
        logged_folder = log_entry.get("action", {}).get("target_folder") if log_entry else None

        in_evidence: bool | None = None
        evidence_file = item.get("evidence", {}).get("file") if isinstance(item.get("evidence"), dict) else item.get("evidence_file")
        if evidence_file:
            evidence_path = (workspace_root / evidence_file).resolve() if not Path(evidence_file).is_absolute() else Path(evidence_file)
            if evidence_path.exists():
                try:
                    evidence_text = evidence_path.read_text(encoding="utf-8")
                    in_evidence = message_id in evidence_text.lower()
                except Exception:
                    in_evidence = False
            else:
                in_evidence = False
        elif references_root.exists():
            found_evidence = False
            for markdown_file in references_root.glob("**/evidence/*.md"):
                try:
                    if message_id in markdown_file.read_text(encoding="utf-8").lower():
                        found_evidence = True
                        break
                except Exception:
                    pass
            in_evidence = found_evidence if found_evidence else None

        folder_verified: bool | None = None
        current_envelope_id: str | None = None
        if check_folders and indexed_folder:
            verified = verify_folder(indexed_folder, message_id, subject=subject, account=account)
            folder_verified = verified is not None
            current_envelope_id = verified

        consistent = in_index and in_action_log
        if expected_folder and indexed_folder and expected_folder != indexed_folder:
            consistent = False
        if check_folders and folder_verified is False:
            consistent = False
        if not consistent:
            all_consistent = False

        results.append({
            "message_id": message_id,
            "subject": subject or (log_entry.get("subject") if log_entry else ""),
            "in_index": in_index,
            "indexed_folder": indexed_folder,
            "indexed_envelope_id": indexed_envelope_id,
            "in_action_log": in_action_log,
            "logged_folder": logged_folder,
            "in_evidence": in_evidence,
            "folder_verified": folder_verified,
            "current_envelope_id": current_envelope_id,
            "consistent": consistent,
        })

    output = {
        "ok": all_consistent,
        "mode": "verify",
        "total_checked": len(results),
        "all_consistent": all_consistent,
        "results": results,
    }
    verify_scope_ids = [row["message_id"] for row in results]
    synthesis_candidate = _verified_execute_candidate(
        execute_summary,
        verify_scope_ids=verify_scope_ids,
        normalize_id=normalize_id,
    )
    # Standalone Verify never rebuilds a candidate from new items or accepts an
    # arbitrary top-level candidate. A generic consistency check remains
    # non-completing unless a complete Execute result proves its provenance.
    if synthesis_candidate is not None:
        handoff = release_synthesis_handoff(synthesis_candidate, output)
        output["synthesis_handoff"] = handoff
        output["completion_report"] = completion_report(
            source="verify",
            status="completed" if all_consistent else "recovery_required",
            verified_message_ids=[row["message_id"] for row in results if row["consistent"]],
            recovery_required=not all_consistent,
            handoff=handoff,
        )
    else:
        output["synthesis_handoff"] = empty_synthesis_handoff()
    if output_file:
        output_path = Path(output_file).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(output_path, output)
    return output
