"""Inspect-mode handler for the mail-desk batch runner.

The runner supplies its legacy fetch helpers as dependencies.  That keeps its
long-standing patch surface stable while the mode implementation remains
independent of the CLI module.
"""

from __future__ import annotations

import concurrent.futures
import json
from pathlib import Path
import time
from typing import Any, Callable, Mapping

from ..attachment_evaluation import attachment_evaluate, decision_triggers_evaluation
from ..attachment_reclassification import install_draft_attachment_evaluations
from ..classifier import classify_email, draft_manifest
from ..common import atomic_write_json, resolve_data_dir, resolve_final_index_path
from ..himalaya import fetch_raw_message_eml, get_single_email_details, run_himalaya
from ..index import load_final_index


def _required_dependency(
    dependencies: Mapping[str, Callable[..., Any]] | None,
    name: str,
) -> Callable[..., Any]:
    if dependencies and name in dependencies:
        return dependencies[name]
    raise RuntimeError(f"{name} must be supplied by the batch-runner compatibility adapter")


def _dependency(
    dependencies: Mapping[str, Callable[..., Any]] | None,
    name: str,
    default: Callable[..., Any],
) -> Callable[..., Any]:
    if dependencies and name in dependencies:
        return dependencies[name]
    return default


def run_inspect_mode(
    config: dict[str, Any],
    account: str | None = None,
    data_dir: Path | None = None,
    *,
    dependencies: Mapping[str, Callable[..., Any]] | None = None,
) -> dict[str, Any]:
    """Inspect mail envelopes and optionally persist an inspection or manifest."""
    get_details = _dependency(dependencies, "get_single_email_details", get_single_email_details)
    run_himalaya_fn = _dependency(dependencies, "run_himalaya", run_himalaya)
    load_index = _dependency(dependencies, "load_final_index", load_final_index)
    resolve_index_path = _dependency(dependencies, "resolve_final_index_path", resolve_final_index_path)
    resolve_data = _dependency(dependencies, "resolve_data_dir", resolve_data_dir)
    draft = _dependency(dependencies, "draft_manifest", draft_manifest)
    write_json = _dependency(dependencies, "atomic_write_json", atomic_write_json)
    sleep = _dependency(dependencies, "sleep", time.sleep)
    # FR-15/MD-E2-T03 reuses the already-hardened draft item flow; the MD-E1 backend and the
    # existing classifier stay authoritative and are resolved through the same patchable
    # dependency boundary as ``run_draft_mode``.
    evaluate_backend = _dependency(dependencies, "attachment_evaluate", attachment_evaluate)
    reclassify = _dependency(dependencies, "classify_email", classify_email)
    raw_mime_reader = _dependency(dependencies, "fetch_raw_message_eml", fetch_raw_message_eml)
    triggers_evaluation = _dependency(
        dependencies, "decision_triggers_evaluation", decision_triggers_evaluation
    )

    folder = config.get("folder", "INBOX")
    count = int(config.get("count", 20))
    order = str(config.get("order", "oldest")).lower()
    date = config.get("date")
    query = config.get("query")
    threads = min(int(config.get("threads", 2)), 2)
    preview_lines = int(config.get("preview_lines", 30))
    explicit_ids = config.get("envelope_ids")
    output_file = config.get("output_file")
    check_known = bool(config.get("check_known", True))
    skip_known = bool(config.get("skip_known", False))
    # FR-15/MD-E2-T03: inspect stays inspection-only by default.  The opt-in
    # ``evaluate_attachments`` flag implicitly enables exactly the same non-executable
    # ``manifest_proposal`` that an explicit ``propose_manifest`` already produced; an
    # executable batch manifest is still written only to an explicit ``manifest_file``.
    evaluate_attachments = config.get("evaluate_attachments", False)
    if not isinstance(evaluate_attachments, bool):
        raise ValueError("evaluate_attachments must be a boolean")
    propose_manifest = bool(config.get("propose_manifest", False) or config.get("propose", False))
    manifest_file = config.get("manifest_file")

    dd = data_dir or resolve_data()
    workspace_root = dd.parent.parent
    ordered_emails: list[dict[str, Any]] = []
    known_count = 0

    if explicit_ids and isinstance(explicit_ids, list):
        target_env_ids = [str(value) for value in explicit_ids]
        if len(target_env_ids) > 20:
            for envelope_id in target_env_ids:
                ordered_emails.append(get_details(envelope_id, folder, account, preview_lines))
                sleep(0.05)
        else:
            results_map: dict[str, dict[str, Any]] = {}
            with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as executor:
                futures = {
                    executor.submit(get_details, envelope_id, folder, account, preview_lines): envelope_id
                    for envelope_id in target_env_ids
                }
                for future in concurrent.futures.as_completed(futures):
                    result = future.result()
                    results_map[result["envelope_id"]] = result
            ordered_emails = [results_map[envelope_id] for envelope_id in target_env_ids if envelope_id in results_map]
    elif skip_known or date or query:
        get_unprocessed = _required_dependency(dependencies, "get_unprocessed_emails")
        ordered_emails, known_count = get_unprocessed(
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
    else:
        get_oldest = _required_dependency(dependencies, "get_oldest_envelopes")
        oldest_envelopes = get_oldest(folder, count, account=account) if order == "oldest" else []
        if not oldest_envelopes:
            try:
                output = run_himalaya_fn(
                    ["-o", "json", "envelope", "list", "-f", folder, "-s", str(count)],
                    account=account,
                    timeout=30,
                )
                if "[" in output:
                    output = output[output.find("["):]
                oldest_envelopes = json.loads(output)
            except Exception:
                oldest_envelopes = []

        envelope_map = {str(envelope.get("id")): envelope for envelope in oldest_envelopes}
        for envelope_id in [str(envelope.get("id")) for envelope in oldest_envelopes]:
            ordered_emails.append(
                get_details(envelope_id, folder, account, preview_lines, fallback_envelope=envelope_map.get(envelope_id))
            )
            sleep(0.02)

    if check_known and known_count == 0:
        known_items = load_index(resolve_index_path(data_dir=dd)).get("items", {})
        for email in ordered_emails:
            message_id = email.get("message_id")
            if message_id and message_id in known_items:
                email["is_known"] = True
                email["known_location"] = known_items[message_id].get("final_folder") or known_items[message_id].get("final_label")
                known_count += 1
            else:
                email["is_known"] = False
                email["known_location"] = None

    output_data: dict[str, Any] = {
        "ok": True,
        "mode": "inspect",
        "folder": folder,
        "order": order,
        "total_inspected": len(ordered_emails),
        "known_count": known_count,
        "emails": ordered_emails,
    }
    if propose_manifest or evaluate_attachments:
        # Initial preview/body/full-read classification always completes first.  The
        # classifier reports the effective source it actually used through the transient
        # ``source_sink``, so the single reclassification consumes the same effective source
        # (preview or full-read) rather than the raw inspection entry.
        effective_sources: list[dict[str, Any]] = []
        manifest = draft(
            ordered_emails,
            workspace_root=workspace_root,
            full_reader=get_details,
            account=account,
            source_sink=effective_sources.append,
        )
        install_draft_attachment_evaluations(
            manifest.get("items", []),
            effective_sources,
            evaluate_attachments=evaluate_attachments,
            workspace_root=workspace_root,
            data_dir=dd,
            account=account,
            evaluate=evaluate_backend,
            reclassify=reclassify,
            read_raw_mime=raw_mime_reader,
            triggers_evaluation=triggers_evaluation,
            policy=config.get("attachment_policy"),
            run_id=config.get("attachment_run_id"),
            lease_id=config.get("lease_id"),
            conversation_id=config.get("conversation_id"),
        )
        output_data["manifest_proposal"] = manifest
        if manifest_file:
            manifest_path = Path(manifest_file).expanduser().resolve()
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            write_json(manifest_path, manifest)
            output_data["manifest_file_created"] = str(manifest_path)

    if output_file:
        output_path = Path(output_file).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(output_path, output_data)
    return output_data
