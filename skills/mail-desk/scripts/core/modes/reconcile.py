"""Read-first recovery assessment for interrupted mail-desk execute runs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Mapping

from ..action_log import append_action_log_entry
from ..common import normalize_message_id, resolve_data_dir, resolve_final_index_path, utc_now_iso
from ..evidence import flush_batch_evidence
from ..himalaya import verify_in_target_folder
from ..index import load_final_index, save_final_index_atomic
from ..recovery import BatchRecoveryJournal
from ..completion import completion_report
from ..synthesis_handoff import collect_synthesis_handoff, empty_synthesis_handoff


def _dep(dependencies: Mapping[str, Callable[..., Any]] | None, name: str, default: Callable[..., Any]) -> Callable[..., Any]:
    return dependencies[name] if dependencies and name in dependencies else default


def _logged(data_dir: Path, message_id: str) -> bool:
    path = data_dir / "action-log.jsonl"
    if not path.exists():
        return False
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                if normalize_message_id(str(json.loads(line).get("message_id", ""))) == message_id:
                    return True
            except Exception:
                continue
    except OSError:
        return False
    return False


def run_reconcile_mode(
    config: dict[str, Any],
    account: str | None = None,
    data_dir: Path | None = None,
    index_path: Path | None = None,
    *,
    dependencies: Mapping[str, Callable[..., Any]] | None = None,
) -> dict[str, Any]:
    """Assess one recovery journal; local repairs require explicit approval.

    The default is deliberately read-only.  `apply_local_repairs` can fill only
    missing local index/log/evidence records after a fresh target verification;
    it never copies, deletes, or otherwise mutates the mailbox.
    """
    resolve_data = _dep(dependencies, "resolve_data_dir", resolve_data_dir)
    resolve_index = _dep(dependencies, "resolve_final_index_path", resolve_final_index_path)
    load_index = _dep(dependencies, "load_final_index", load_final_index)
    save_index = _dep(dependencies, "save_final_index_atomic", save_final_index_atomic)
    verify_folder = _dep(dependencies, "verify_in_target_folder", verify_in_target_folder)
    append_action = _dep(dependencies, "append_action_log_entry", append_action_log_entry)
    flush_evidence = _dep(dependencies, "flush_batch_evidence", flush_batch_evidence)
    now = _dep(dependencies, "utc_now_iso", utc_now_iso)

    dd = data_dir or resolve_data()
    idx_p = index_path or resolve_index(data_dir=dd)
    journal_path, run = BatchRecoveryJournal.load_run(dd, config.get("run_id"))
    if run is None:
        return {"ok": False, "mode": "reconcile", "status": "not_found", "read_only": not bool(config.get("apply_local_repairs")), "recovery_journal": str(journal_path), "results": [], "message": "No readable recovery journal run was found."}

    apply_local = bool(config.get("apply_local_repairs", False))
    approved = isinstance(config.get("approval"), dict) and config["approval"].get("state") == "approved"
    check_folders = bool(config.get("check_folders", True))
    if apply_local and not approved:
        return {"ok": False, "mode": "reconcile", "status": "approval_required", "read_only": True, "recovery_journal": str(journal_path), "run_id": run.get("run_id"), "results": [], "message": "Local repair requires an explicit approved recovery receipt."}
    if apply_local and not check_folders:
        return {"ok": False, "mode": "reconcile", "status": "verification_required", "read_only": True, "recovery_journal": str(journal_path), "run_id": run.get("run_id"), "results": [], "message": "Local repair requires fresh target-folder verification."}

    index_data = load_index(idx_p)
    index_items = index_data.setdefault("items", {})
    workspace_root = dd.parent.parent
    results: list[dict[str, Any]] = []
    needs_review = False
    repaired_count = 0
    run_id = str(run.get("run_id", ""))
    journal = BatchRecoveryJournal(dd, run_id, mode="reconcile", resume=False) if apply_local else None

    for message_id, record in run.get("items", {}).items():
        if not isinstance(record, dict):
            continue
        message_id = normalize_message_id(str(record.get("message_id") or message_id))
        mutable_record = (
            journal.run.get("items", {}).get(message_id, record)
            if journal is not None
            else record
        )
        action = record.get("action") if isinstance(record.get("action"), dict) else {}
        action_type = str(action.get("type", "copy_as_move"))
        source = str(record.get("source_folder", "INBOX"))
        final_folder = str(record.get("final_folder") or ("Trash" if action_type == "delete" else action.get("target_folder") or source))
        folder_verified: bool | None = None
        final_env: str | None = None
        if check_folders and action_type != "delete" and message_id:
            verified = verify_folder(final_folder, message_id, subject=str(record.get("subject", "")), from_addr=str(record.get("from", "")), date_str=str(record.get("date", "")), account=account, candidate_env_id=str(record.get("final_envelope_id") or record.get("envelope_id") or ""))
            folder_verified = verified is not None
            final_env = str(verified) if verified is not None else None
        elif check_folders and action_type == "delete":
            # A generic trash locator is backend-specific; never claim a delete
            # is verified merely because the source command returned.
            folder_verified = False

        in_index = message_id in index_items
        in_log = _logged(dd, message_id)
        evidence = record.get("evidence")
        evidence_ok: bool | None = None
        if isinstance(evidence, dict) and evidence.get("file"):
            path = (workspace_root / str(evidence["file"])).resolve()
            try:
                evidence_ok = path.exists() and message_id in path.read_text(encoding="utf-8").lower()
            except OSError:
                evidence_ok = False
        complete_local = in_index and in_log and (evidence_ok in {True, None})
        verifiable = folder_verified is True or (not check_folders and record.get("phase") in {"indexed", "logged", "evidenced", "complete"})
        repaired: list[str] = []

        if apply_local and verifiable:
            if not in_index:
                index_items[message_id] = {"message_id": message_id, "backend": "himalaya", "final_folder": final_folder, "envelope_id": final_env or str(record.get("final_envelope_id") or record.get("envelope_id", "")), "in_reply_to": "", "references": [], "subject": str(record.get("subject", "")), "from": str(record.get("from", "")), "date": str(record.get("date", "")) or now(), "updated_at": now()}
                index_data["updated_at"] = now()
                save_index(idx_p, index_data)
                in_index = True
                repaired.append("index")
                journal.transition(mutable_record, "indexed", final_envelope_id=final_env or record.get("final_envelope_id"), final_folder=final_folder)
            if not in_log:
                append_action(dd, {"timestamp": now(), "envelope_id": str(record.get("envelope_id", "")), "message_id": message_id, "subject": str(record.get("subject", "")), "from": str(record.get("from", "")), "action": {"type": action_type, "source_folder": source, "target_folder": final_folder, "new_envelope_id": final_env or str(record.get("final_envelope_id") or "")}, "decision": record.get("decision", {}), "notes": str(record.get("notes", "")), "reconciled": True})
                in_log = True
                repaired.append("action_log")
                journal.transition(mutable_record, "logged", final_envelope_id=final_env or record.get("final_envelope_id"), final_folder=final_folder)
            if isinstance(evidence, dict) and evidence_ok is False:
                status = flush_evidence([{"message_id": message_id, "evidence": evidence}], workspace_root=workspace_root)
                evidence_ok = bool(status) and all(status.values())
                if evidence_ok:
                    repaired.append("evidence")
                    journal.transition(mutable_record, "evidenced", final_envelope_id=final_env or record.get("final_envelope_id"), final_folder=final_folder)
            complete_local = in_index and in_log and (evidence_ok in {True, None})
            if complete_local:
                journal.transition(mutable_record, "complete", final_envelope_id=final_env or record.get("final_envelope_id"), final_folder=final_folder)
                repaired_count += 1 if repaired else 0

        recovery_state = "complete" if verifiable and complete_local else ("needs_local_repair" if verifiable else "needs_mailbox_review")
        if recovery_state != "complete":
            needs_review = True
        results.append({"message_id": message_id, "journal_phase": record.get("phase"), "final_folder": final_folder, "folder_verified": folder_verified, "current_envelope_id": final_env, "in_index": in_index, "in_action_log": in_log, "in_evidence": evidence_ok, "recovery_state": recovery_state, "repaired": repaired})

    if journal and not needs_review:
        journal.set_run_status("completed")
    elif journal:
        journal.set_run_status("partial")
    recovered_items = [
        {"message_id": record.get("message_id", message_id), "subject": record.get("subject", ""), "decision": record.get("decision", {}), "synthesis_targets": record.get("synthesis_targets", [])}
        for message_id, record in run.get("items", {}).items()
        if isinstance(record, dict)
    ]
    recovered_results = [
        {"message_id": row.get("message_id", ""), "subject": "", "success": row.get("recovery_state") == "complete", "synthesis_targets": next((item.get("synthesis_targets", []) for item in recovered_items if normalize_message_id(str(item.get("message_id", ""))) == row.get("message_id")), [])}
        for row in results
    ]
    handoff = collect_synthesis_handoff(recovered_items, recovered_results) if not needs_review else empty_synthesis_handoff()
    status = "completed" if not needs_review else "recovery_required"
    return {"ok": not needs_review, "mode": "reconcile", "status": status, "recovery_required": needs_review, "read_only": not apply_local, "recovery_journal": str(journal_path), "run_id": run_id, "total_checked": len(results), "repaired_count": repaired_count, "results": results, "synthesis_handoff": handoff, "completion_report": completion_report(source="reconcile", status=status, verified_message_ids=[row.get("message_id", "") for row in results if row.get("recovery_state") == "complete"], recovery_required=needs_review, handoff=handoff), "message": "Recovery report is read-only." if not apply_local else "Approved local recovery repairs applied; mailbox was not mutated."}
