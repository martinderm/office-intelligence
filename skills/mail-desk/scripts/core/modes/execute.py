"""Coupled routing, index, log, and evidence handler for mail-desk batches."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Mapping

from ..action_log import append_action_log_entry, append_replies_needed_entry
from ..common import normalize_message_id, resolve_data_dir, resolve_final_index_path, utc_now_iso
from ..evidence import flush_batch_evidence
from ..himalaya import run_himalaya, verify_in_target_folder
from ..index import load_final_index, save_final_index_atomic
from ..progress import BatchProgressTracker
from ..sent_indexer import auto_resolve_replies_from_sent
from ..telemetry import collect_telemetry


def _dependency(
    dependencies: Mapping[str, Callable[..., Any]] | None,
    name: str,
    default: Callable[..., Any],
) -> Callable[..., Any]:
    if dependencies and name in dependencies:
        return dependencies[name]
    return default


def run_execute_mode(
    config: dict[str, Any],
    account: str | None = None,
    data_dir: Path | None = None,
    index_path: Path | None = None,
    *,
    dependencies: Mapping[str, Callable[..., Any]] | None = None,
) -> dict[str, Any]:
    """Route each item, then serially persist its verified operational record."""
    resolve_data = _dependency(dependencies, "resolve_data_dir", resolve_data_dir)
    resolve_index_path = _dependency(
        dependencies,
        "resolve_final_index_path",
        resolve_final_index_path,
    )
    progress_tracker = _dependency(
        dependencies,
        "BatchProgressTracker",
        BatchProgressTracker,
    )
    load_index = _dependency(dependencies, "load_final_index", load_final_index)
    run_mail = _dependency(dependencies, "run_himalaya", run_himalaya)
    verify_folder = _dependency(
        dependencies,
        "verify_in_target_folder",
        verify_in_target_folder,
    )
    normalize_id = _dependency(dependencies, "normalize_message_id", normalize_message_id)
    now = _dependency(dependencies, "utc_now_iso", utc_now_iso)
    append_action = _dependency(
        dependencies,
        "append_action_log_entry",
        append_action_log_entry,
    )
    append_reply = _dependency(
        dependencies,
        "append_replies_needed_entry",
        append_replies_needed_entry,
    )
    flush_evidence = _dependency(
        dependencies,
        "flush_batch_evidence",
        flush_batch_evidence,
    )
    save_index = _dependency(
        dependencies,
        "save_final_index_atomic",
        save_final_index_atomic,
    )
    auto_resolve = _dependency(
        dependencies,
        "auto_resolve_replies_from_sent",
        auto_resolve_replies_from_sent,
    )
    sleep = _dependency(dependencies, "sleep", time.sleep)

    dd = data_dir or resolve_data()
    idx_p = index_path or resolve_index_path(data_dir=dd)
    items: list[dict[str, Any]] = config.get("items", [])
    workspace_root = dd.parent.parent
    tracker = progress_tracker(
        mode="execute",
        total_items=len(items),
        data_dir=dd,
    )
    index_data = load_index(idx_p)
    index_items = index_data.setdefault("items", {})
    results: list[dict[str, Any]] = []
    pending_evidence: list[dict[str, Any]] = []
    all_succeeded = True

    for item in items:
        env_id = str(item["envelope_id"])
        source_folder = item.get("source_folder", "INBOX")
        raw_mid = item.get("message_id") or item.get("raw_message_id", "")
        norm_mid = normalize_id(raw_mid)
        subject = item.get("subject", "")
        from_str = item.get("from", "")
        date_str = item.get("date", "")
        action_spec = item.get("action", {})
        action_type = action_spec.get("type", "copy_as_move")
        target_folder = action_spec.get("target_folder")
        decision = item.get("decision", {})
        notes = item.get("notes", "")
        evidence_spec = item.get("evidence")

        routing_ok = False
        meta_ok = False
        index_ok = False
        ref_source_status = "not-applicable"
        new_env_id = None
        final_folder = target_folder or source_folder
        tracker.step(
            f"routing to {final_folder}",
            envelope_id=env_id,
            subject=subject,
        )

        if action_type == "copy_as_move" and target_folder and target_folder != source_folder:
            if norm_mid and norm_mid in index_items:
                known_entry = index_items[norm_mid]
                if known_entry.get("final_folder") == target_folder:
                    new_env_id = known_entry.get("envelope_id") or env_id
                    routing_ok = True

            if not routing_ok:
                try:
                    run_mail(
                        ["message", "copy", target_folder, env_id, "-f", source_folder],
                        account=account,
                        timeout=45,
                        max_retries=2,
                    )
                    sleep(0.3)
                    new_env_id = verify_folder(
                        target_folder,
                        norm_mid,
                        subject=subject,
                        from_addr=from_str,
                        date_str=date_str,
                        account=account,
                    )
                    if not new_env_id:
                        sleep(0.5)
                        new_env_id = verify_folder(
                            target_folder,
                            norm_mid,
                            subject=subject,
                            from_addr=from_str,
                            date_str=date_str,
                            account=account,
                        )
                    if new_env_id:
                        routing_ok = True
                        try:
                            run_mail(
                                ["message", "delete", env_id, "-f", source_folder],
                                account=account,
                                timeout=20,
                                max_retries=2,
                            )
                        except Exception:
                            pass
                except Exception:
                    routing_ok = False
            sleep(0.15)
        elif action_type == "keep_in_folder" or not target_folder or target_folder == source_folder:
            new_env_id = env_id
            routing_ok = True
        elif action_type == "delete":
            try:
                run_mail(
                    ["message", "delete", env_id, "-f", source_folder],
                    account=account,
                    timeout=20,
                    max_retries=2,
                )
                routing_ok = True
                final_folder = "Trash"
                new_env_id = env_id
            except Exception:
                routing_ok = True
                final_folder = "Trash"
                new_env_id = env_id

        if evidence_spec and norm_mid and routing_ok:
            pending_evidence.append(
                {"message_id": norm_mid, "evidence": evidence_spec}
            )
            ref_source_status = "ok"

        if routing_ok and norm_mid:
            index_items[norm_mid] = {
                "message_id": norm_mid,
                "backend": "himalaya",
                "final_folder": final_folder,
                "envelope_id": str(new_env_id) if new_env_id else str(env_id),
                "in_reply_to": item.get("in_reply_to", ""),
                "references": item.get("references", []),
                "subject": subject,
                "from": from_str,
                "date": date_str or now(),
                "updated_at": now(),
            }
            index_ok = True

        if routing_ok:
            log_entry = {
                "timestamp": now(),
                "envelope_id": env_id,
                "message_id": norm_mid,
                "subject": subject,
                "from": from_str,
                "action": {
                    "type": action_type,
                    "source_folder": source_folder,
                    "target_folder": final_folder,
                    "new_envelope_id": str(new_env_id) if new_env_id else str(env_id),
                },
                "decision": decision,
                "notes": notes,
            }
            append_action(dd, log_entry)
            meta_ok = True

            if decision.get("needs_reply"):
                rep_entry = {
                    "timestamp": now(),
                    "envelope_id": str(new_env_id) if new_env_id else str(env_id),
                    "message_id": norm_mid,
                    "subject": subject,
                    "from": from_str,
                    "folder": final_folder,
                    "reply_status": "needed",
                    "reply_note": notes,
                }
                if decision.get("reply_candidate"):
                    rep_entry["reply_candidate"] = decision["reply_candidate"]
                append_reply(dd, rep_entry)

        item_success = routing_ok and index_ok and meta_ok
        if not item_success:
            all_succeeded = False

        results.append(
            {
                "envelope_id": env_id,
                "message_id": norm_mid,
                "subject": subject,
                "final_folder": final_folder,
                "new_envelope_id": str(new_env_id),
                "routing": "ok" if routing_ok else "fail",
                "metadata": "ok" if meta_ok else "fail",
                "final-index-script": "ok" if index_ok else "fail",
                "reference-source-id": ref_source_status,
                "success": item_success,
            }
        )
        tracker.advance_item(
            envelope_id=env_id,
            subject=subject,
            step_name=(
                f"routed to {final_folder}"
                if item_success
                else f"failed routing {env_id}"
            ),
        )

    if pending_evidence:
        flush_evidence(pending_evidence, workspace_root=workspace_root)

    if index_items:
        index_data["updated_at"] = now()
        save_index(idx_p, index_data)

    try:
        auto_resolve(data_dir=dd, workspace_root=workspace_root)
    except Exception:
        pass

    if all_succeeded:
        tracker.complete(f"Executed batch of {len(results)} items successfully.")
    else:
        succeeded_count = sum(1 for result in results if result["success"])
        tracker.complete(
            f"Executed batch: {succeeded_count}/{len(results)} succeeded."
        )

    return {
        "ok": all_succeeded,
        "mode": "execute",
        "total_processed": len(results),
        "all_succeeded": all_succeeded,
        "results": results,
        "telemetry": collect_telemetry(items, results),
    }
