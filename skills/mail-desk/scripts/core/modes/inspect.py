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

from ..classifier import draft_manifest
from ..common import atomic_write_json, resolve_data_dir, resolve_final_index_path
from ..himalaya import get_single_email_details, run_himalaya
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
    if propose_manifest:
        manifest = draft(
            ordered_emails,
            workspace_root=workspace_root,
            full_reader=get_details,
            account=account,
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
