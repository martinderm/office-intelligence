"""Coupled routing, index, log, and evidence handler for mail-desk batches."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from ..action_log import append_action_log_entry, append_replies_needed_entry
from ..batch_contract import validate_execute_contract
from ..common import normalize_message_id, resolve_data_dir, resolve_final_index_path, utc_now_iso
from ..evidence import flush_batch_evidence
from ..himalaya import run_himalaya, verify_in_target_folder
from ..index import load_final_index, save_final_index_atomic
from ..progress import BatchProgressTracker
from ..recovery import BatchRecoveryJournal, cleanup_recovery_temp_files, has_completed_step, stable_batch_id
from ..sent_indexer import auto_resolve_replies_from_sent
from ..synthesis_handoff import collect_synthesis_handoff, empty_synthesis_handoff
from ..synthesis_targets import validate_execute_synthesis_targets
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
    return _execute_with_journal(
        config,
        account=account,
        data_dir=data_dir,
        index_path=index_path,
        dependencies=dependencies,
    )


def _legacy_run_execute_mode(
    config: dict[str, Any],
    account: str | None = None,
    data_dir: Path | None = None,
    index_path: Path | None = None,
    *,
    dependencies: Mapping[str, Callable[..., Any]] | None = None,
) -> dict[str, Any]:
    """Pre-H4 implementation retained as a compatibility reference only."""
    contract = validate_execute_contract(config, effective_account=account)
    if not contract["ok"]:
        return {
            "ok": False,
            "mode": "execute",
            "message": contract["message"],
            "total_processed": 0,
            "all_succeeded": False,
            "results": [],
            "telemetry": collect_telemetry([], []),
            "synthesis_handoff": collect_synthesis_handoff([], []),
            "contract_gate": contract,
        }
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

    items: list[dict[str, Any]] = config.get("items", [])
    if not isinstance(items, list):
        raise ValueError("Execute items must be a list")
    validated_synthesis_targets = validate_execute_synthesis_targets(items)

    dd = data_dir or resolve_data()
    idx_p = index_path or resolve_index_path(data_dir=dd)
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

    for item_index, item in enumerate(items):
        item_synthesis_targets = validated_synthesis_targets[item_index]
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
        elif action_type == "keep_in_folder" or (
            action_type != "delete" and (not target_folder or target_folder == source_folder)
        ):
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
                routing_ok = False

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
                "synthesis_targets": item_synthesis_targets if item_success else [],
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
        tracker.fail(f"Executed batch: {succeeded_count}/{len(results)} succeeded.")

    return {
        "ok": all_succeeded,
        "mode": "execute",
        "total_processed": len(results),
        "all_succeeded": all_succeeded,
        "results": results,
        "telemetry": collect_telemetry(items, results),
        "synthesis_handoff": collect_synthesis_handoff(items, results),
        "contract_gate": contract,
    }


def _action_log_contains(data_dir: Path, message_id: str) -> bool:
    """Avoid duplicate append-only action records during an idempotent resume."""
    path = data_dir / "action-log.jsonl"
    if not path.exists():
        return False
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except Exception:
                continue
            if normalize_message_id(str(row.get("message_id", ""))) == message_id:
                return True
    except OSError:
        return False
    return False


def _reply_log_contains(data_dir: Path, message_id: str) -> bool:
    """Keep replies-needed append-only state idempotent across interrupted runs."""
    path = data_dir / "replies-needed.jsonl"
    if not path.exists():
        return False
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except Exception:
                continue
            if normalize_message_id(str(row.get("message_id", ""))) == message_id:
                return True
    except OSError:
        return False
    return False


def _checkpoint(
    dependencies: Mapping[str, Callable[..., Any]] | None,
    phase: str,
    item: dict[str, Any],
) -> None:
    """Explicit fault-injection seam used only by deterministic regression tests."""
    callback = dependencies.get("fault_inject") if dependencies else None
    if callable(callback):
        callback(phase, item)


def _execute_with_journal(
    config: dict[str, Any],
    account: str | None,
    data_dir: Path | None,
    index_path: Path | None,
    dependencies: Mapping[str, Callable[..., Any]] | None,
) -> dict[str, Any]:
    """Run the H4 durable per-item state machine.

    The journal is written *before* crossing every external/local boundary.
    Restarting a run first verifies an earlier copied target and consequently
    never issues a second copy for the same durable Message-ID.
    """
    contract = validate_execute_contract(config, effective_account=account)
    if not contract["ok"]:
        return {"ok": False, "mode": "execute", "message": contract["message"], "total_processed": 0, "all_succeeded": False, "results": [], "telemetry": collect_telemetry([], []), "synthesis_handoff": collect_synthesis_handoff([], []), "contract_gate": contract}
    items = config.get("items", [])
    if not isinstance(items, list):
        raise ValueError("Execute items must be a list")
    targets = validate_execute_synthesis_targets(items)
    resolve_data = _dependency(dependencies, "resolve_data_dir", resolve_data_dir)
    resolve_index = _dependency(dependencies, "resolve_final_index_path", resolve_final_index_path)
    tracker_class = _dependency(dependencies, "BatchProgressTracker", BatchProgressTracker)
    load_index = _dependency(dependencies, "load_final_index", load_final_index)
    save_index = _dependency(dependencies, "save_final_index_atomic", save_final_index_atomic)
    run_mail = _dependency(dependencies, "run_himalaya", run_himalaya)
    verify_folder = _dependency(dependencies, "verify_in_target_folder", verify_in_target_folder)
    normalize_id = _dependency(dependencies, "normalize_message_id", normalize_message_id)
    append_action = _dependency(dependencies, "append_action_log_entry", append_action_log_entry)
    append_reply = _dependency(dependencies, "append_replies_needed_entry", append_replies_needed_entry)
    flush_evidence = _dependency(dependencies, "flush_batch_evidence", flush_batch_evidence)
    now = _dependency(dependencies, "utc_now_iso", utc_now_iso)
    auto_resolve = _dependency(dependencies, "auto_resolve_replies_from_sent", auto_resolve_replies_from_sent)
    sleep = _dependency(dependencies, "sleep", time.sleep)

    dd = data_dir or resolve_data()
    idx_p = index_path or resolve_index(data_dir=dd)
    workspace_root = dd.parent.parent
    run_id = stable_batch_id(items, config.get("recovery_run_id"))
    journal = BatchRecoveryJournal(dd, run_id)
    removed_temp_files = cleanup_recovery_temp_files(dd)
    tracker = tracker_class(mode="execute", total_items=len(items), data_dir=dd, run_id=run_id)
    index_data = load_index(idx_p)
    index_items = index_data.setdefault("items", {})
    results: list[dict[str, Any]] = []
    aborted = False
    all_succeeded = True

    for item_position, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError("Each execute item must be an object")
        record = journal.ensure_item(item)
        env_id = str(item.get("envelope_id", ""))
        message_id = normalize_id(str(item.get("message_id") or item.get("raw_message_id") or ""))
        source = str(item.get("source_folder", "INBOX"))
        action = item.get("action") if isinstance(item.get("action"), dict) else {}
        action_type = str(action.get("type", "copy_as_move"))
        target = str(action.get("target_folder") or source)
        final_folder = "Trash" if action_type == "delete" else target
        final_env = record.get("final_envelope_id")
        routing_ok = False
        index_ok = message_id in index_items
        metadata_ok = _action_log_contains(dd, message_id) if message_id else False
        evidence_status = "not-applicable" if not record.get("evidence") else "pending"
        error: str | None = None

        try:
            if not message_id:
                raise RuntimeError("Missing durable Message-ID.")
            tracker.step(f"routing to {final_folder}", envelope_id=env_id, subject=str(item.get("subject", "")))
            copied_before = has_completed_step(record, "copied")

            # The stored target is an idempotency lead, not proof by itself: read
            # it again before deciding whether a new copy is allowed.
            if action_type == "copy_as_move" and target != source and copied_before:
                verified = verify_folder(target, message_id, subject=str(item.get("subject", "")), from_addr=str(item.get("from", "")), date_str=str(item.get("date", "")), account=account)
                if verified:
                    final_env = str(verified)
                    journal.transition(record, "verified", final_envelope_id=final_env, final_folder=target)

            if action_type == "copy_as_move" and target != source:
                if not final_env and copied_before:
                    raise RuntimeError("Previously copied target could not be verified; reconcile before retry.")
                if not final_env:
                    journal.transition(record, "copy_started")
                    run_mail(["message", "copy", target, env_id, "-f", source], account=account, timeout=45, max_retries=2)
                    journal.transition(record, "copied")
                    _checkpoint(dependencies, "after_copy", item)
                    sleep(0.3)
                    verified = verify_folder(target, message_id, subject=str(item.get("subject", "")), from_addr=str(item.get("from", "")), date_str=str(item.get("date", "")), account=account)
                    if not verified:
                        sleep(0.5)
                        verified = verify_folder(target, message_id, subject=str(item.get("subject", "")), from_addr=str(item.get("from", "")), date_str=str(item.get("date", "")), account=account)
                    if not verified:
                        raise RuntimeError("Target verification failed after copy.")
                    final_env = str(verified)
                    journal.transition(record, "verified", final_envelope_id=final_env, final_folder=target)
                    _checkpoint(dependencies, "after_verify", item)
                if not has_completed_step(record, "source_deleted"):
                    journal.transition(record, "delete_started", final_envelope_id=str(final_env), final_folder=target)
                    run_mail(["message", "delete", env_id, "-f", source], account=account, timeout=20, max_retries=2)
                    journal.transition(record, "source_deleted", final_envelope_id=str(final_env), final_folder=target)
                    _checkpoint(dependencies, "after_delete", item)
                routing_ok = True
            elif action_type == "delete":
                if not has_completed_step(record, "source_deleted"):
                    journal.transition(record, "delete_started")
                    run_mail(["message", "delete", env_id, "-f", source], account=account, timeout=20, max_retries=2)
                    verified = verify_folder("Trash", message_id, subject=str(item.get("subject", "")), from_addr=str(item.get("from", "")), date_str=str(item.get("date", "")), account=account)
                    if not verified:
                        raise RuntimeError("Trash verification failed after delete.")
                    final_env = str(verified)
                    journal.transition(record, "source_deleted", final_envelope_id=final_env, final_folder="Trash")
                    _checkpoint(dependencies, "after_delete", item)
                routing_ok = True
            else:
                verified = verify_folder(source, message_id, subject=str(item.get("subject", "")), from_addr=str(item.get("from", "")), date_str=str(item.get("date", "")), account=account)
                if not verified:
                    raise RuntimeError("Final location verification failed for retained item.")
                final_env = str(verified)
                journal.transition(record, "verified", final_envelope_id=final_env, final_folder=source)
                routing_ok = True

            # Index, log and evidence are intentionally per-item; each journal
            # phase lets a later reconcile repair only what is missing.
            if not has_completed_step(record, "indexed"):
                index_items[message_id] = {"message_id": message_id, "backend": "himalaya", "final_folder": final_folder, "envelope_id": str(final_env or env_id), "in_reply_to": item.get("in_reply_to", ""), "references": item.get("references", []), "subject": str(item.get("subject", "")), "from": str(item.get("from", "")), "date": str(item.get("date", "")) or now(), "updated_at": now()}
                index_data["updated_at"] = now()
                save_index(idx_p, index_data)
                index_ok = True
                journal.transition(record, "indexed", final_envelope_id=str(final_env or env_id), final_folder=final_folder)
                _checkpoint(dependencies, "after_index", item)
            else:
                index_ok = message_id in index_items
            if not has_completed_step(record, "logged"):
                if not _action_log_contains(dd, message_id):
                    append_action(dd, {"timestamp": now(), "envelope_id": env_id, "message_id": message_id, "subject": str(item.get("subject", "")), "from": str(item.get("from", "")), "action": {"type": action_type, "source_folder": source, "target_folder": final_folder, "new_envelope_id": str(final_env or env_id)}, "decision": item.get("decision", {}), "notes": str(item.get("notes", ""))})
                metadata_ok = True
                if item.get("decision", {}).get("needs_reply") and not _reply_log_contains(dd, message_id):
                    append_reply(dd, {"timestamp": now(), "envelope_id": str(final_env or env_id), "message_id": message_id, "subject": str(item.get("subject", "")), "from": str(item.get("from", "")), "folder": final_folder, "reply_status": "needed", "reply_note": str(item.get("notes", ""))})
                _checkpoint(dependencies, "after_reply", item)
                journal.transition(record, "logged", final_envelope_id=str(final_env or env_id), final_folder=final_folder)
                _checkpoint(dependencies, "after_log", item)
            if record.get("evidence"):
                if not has_completed_step(record, "evidenced"):
                    evidence_result = flush_evidence([{"message_id": message_id, "evidence": record["evidence"]}], workspace_root=workspace_root)
                    if not evidence_result or not all(evidence_result.values()):
                        raise RuntimeError("Evidence write failed after verified routing.")
                    journal.transition(record, "evidenced", final_envelope_id=str(final_env or env_id), final_folder=final_folder)
                evidence_status = "ok"
            journal.transition(record, "complete", final_envelope_id=str(final_env or env_id), final_folder=final_folder)
        except (KeyboardInterrupt, TimeoutError) as exc:
            error = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            journal.transition(record, "aborted", error=error, final_envelope_id=str(final_env) if final_env else None, final_folder=final_folder)
            journal.set_run_status("aborted", error=error)
            if hasattr(tracker, "abort"):
                tracker.abort(error)
            else:
                tracker.fail(error)
            aborted = True
            all_succeeded = False
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            journal.transition(record, "partial", error=error, final_envelope_id=str(final_env) if final_env else None, final_folder=final_folder)
            all_succeeded = False

        success = routing_ok and index_ok and metadata_ok and evidence_status != "fail" and error is None
        if not success:
            all_succeeded = False
        phase = "aborted" if aborted else ("complete" if success else "partial")
        results.append({"envelope_id": env_id, "message_id": message_id, "subject": str(item.get("subject", "")), "final_folder": final_folder, "new_envelope_id": str(final_env) if final_env else None, "routing": "ok" if routing_ok else "fail", "metadata": "ok" if metadata_ok else "fail", "final-index-script": "ok" if index_ok else "fail", "reference-source-id": evidence_status, "recovery_phase": phase, "success": success, "error": error, "synthesis_targets": targets[item_position] if success else []})
        if not aborted:
            tracker.advance_item(envelope_id=env_id, subject=str(item.get("subject", "")), step_name="routed" if success else "partial recovery required")
        if aborted:
            break

    if aborted:
        journal.set_run_status("aborted")
    elif all_succeeded:
        journal.set_run_status("completed")
        tracker.complete(f"Executed batch of {len(results)} items successfully.")
        try:
            auto_resolve(data_dir=dd, workspace_root=workspace_root)
        except Exception:
            pass
    else:
        journal.set_run_status("partial")
        tracker.fail(f"Executed batch: {sum(1 for result in results if result['success'])}/{len(results)} succeeded.")
    # Execute is deliberately not a completion boundary.  Keep the source-bound
    # candidate for the following verify/reconcile step, but never release a
    # synthesis handoff from a merely routed batch.
    candidate = collect_synthesis_handoff(items, results)
    return {"ok": all_succeeded and not aborted, "mode": "execute", "status": "aborted" if aborted else ("completed" if all_succeeded else "partial"), "recovery_required": aborted or not all_succeeded, "recovery_journal": {"path": str(journal.path), "run_id": run_id, "status": journal.run.get("status"), "removed_temp_files": removed_temp_files}, "total_processed": len(results), "all_succeeded": all_succeeded and not aborted, "results": results, "telemetry": collect_telemetry(items, results), "synthesis_candidate": candidate, "synthesis_handoff": empty_synthesis_handoff(), "contract_gate": contract}
