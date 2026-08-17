#!/usr/bin/env python3
"""Unified batch runner for mail-desk operations.

Supports 5 modes:
1. inspect: Parallel header & preview fetching with deduplication check
2. execute: Coupled routing, target verification, index upsert, logging, evidence
3. verify:  Consistency check across final index, action log, evidence, and folders
4. search:  Global mailbox search by query or message_ids
5. resolve: Batch resolution and archival of replies-needed and review cases
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
from pathlib import Path
import sys
from typing import Any

# Add parent directory to sys.path if invoked directly
_script_dir = Path(__file__).resolve().parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))

from core import (
    append_action_log_entry,
    append_replies_needed_entry,
    get_single_email_details,
    load_final_index,
    normalize_message_id,
    resolve_case,
    resolve_data_dir,
    resolve_final_index_path,
    run_himalaya,
    save_final_index_atomic,
    search_mailbox,
    update_evidence_file,
    utc_now_iso,
    verify_in_target_folder,
)


# ==============================================================================
# Inspect Mode
# ==============================================================================

def get_oldest_envelope_ids(folder: str, count: int, account: str | None = None) -> list[str]:
    page_size = 50
    page = 1
    # Step 1: probe exponentially
    while True:
        try:
            out = run_himalaya(["-o", "json", "envelope", "list", "-f", folder, "-s", str(page_size), "-p", str(page)], account=account, timeout=15)
            if "[" in out:
                out = out[out.find("["):]
            envs = json.loads(out)
            if not envs:
                break
            page *= 2
        except Exception:
            break

    # Step 2: binary search
    low = max(1, page // 2)
    high = page
    last_valid_page = 1
    last_page_envs = []
    while low <= high:
        mid = (low + high) // 2
        try:
            out = run_himalaya(["-o", "json", "envelope", "list", "-f", folder, "-s", str(page_size), "-p", str(mid)], account=account, timeout=15)
            if "[" in out:
                out = out[out.find("["):]
            envs = json.loads(out)
            if envs:
                last_valid_page = mid
                last_page_envs = envs
                low = mid + 1
            else:
                high = mid - 1
        except Exception:
            high = mid - 1

    # Step 3: collect envelopes
    collected = list(last_page_envs)
    curr_page = last_valid_page - 1
    while len(collected) < count and curr_page >= 1:
        try:
            out = run_himalaya(["-o", "json", "envelope", "list", "-f", folder, "-s", str(page_size), "-p", str(curr_page)], account=account, timeout=15)
            if "[" in out:
                out = out[out.find("["):]
            envs = json.loads(out)
            collected = envs + collected
            curr_page -= 1
        except Exception:
            break

    oldest = collected[-count:] if count < len(collected) else collected
    oldest.reverse()
    return [str(e["id"]) for e in oldest]


def run_inspect_mode(
    config: dict[str, Any],
    account: str | None = None,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    folder = config.get("folder", "INBOX")
    count = int(config.get("count", 20))
    order = str(config.get("order", "newest")).lower()
    threads = min(int(config.get("threads", 3)), 3)
    preview_lines = int(config.get("preview_lines", 30))
    explicit_ids = config.get("envelope_ids")
    output_file = config.get("output_file")
    check_known = bool(config.get("check_known", True))

    target_env_ids: list[str] = []

    if explicit_ids and isinstance(explicit_ids, list):
        target_env_ids = [str(x) for x in explicit_ids]
    elif order == "oldest":
        target_env_ids = get_oldest_envelope_ids(folder, count, account=account)
    else:
        out = run_himalaya(["-o", "json", "envelope", "list", "-f", folder, "-s", str(count)], account=account, timeout=30)
        if "[" in out:
            out = out[out.find("["):]
        envelopes = json.loads(out)
        target_env_ids = [str(env["id"]) for env in envelopes[:count]]

    results_map: dict[str, dict[str, Any]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as executor:
        futures = {
            executor.submit(get_single_email_details, eid, folder, account, preview_lines): eid
            for eid in target_env_ids
        }
        for fut in concurrent.futures.as_completed(futures):
            res = fut.result()
            results_map[res["envelope_id"]] = res

    ordered_emails = [results_map[eid] for eid in target_env_ids if eid in results_map]

    # Check known / processed status against final-location-index.json
    known_count = 0
    if check_known:
        dd = data_dir or resolve_data_dir()
        index_data = load_final_index(resolve_final_index_path(data_dir=dd))
        known_items = index_data.get("items", {})

        for email in ordered_emails:
            mid = email.get("message_id")
            if mid and mid in known_items:
                email["is_known"] = True
                email["known_location"] = known_items[mid].get("final_folder") or known_items[mid].get("final_label")
                known_count += 1
            else:
                email["is_known"] = False
                email["known_location"] = None

    output_data = {
        "ok": True,
        "mode": "inspect",
        "folder": folder,
        "total_inspected": len(ordered_emails),
        "known_count": known_count,
        "emails": ordered_emails,
    }

    if output_file:
        out_path = Path(output_file).expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as f:
            json.dump(output_data, f, ensure_ascii=False, indent=2)
            f.write("\n")

    return output_data


# ==============================================================================
# Execute Mode
# ==============================================================================

def run_execute_mode(
    config: dict[str, Any],
    account: str | None = None,
    data_dir: Path | None = None,
    index_path: Path | None = None,
) -> dict[str, Any]:
    dd = data_dir or resolve_data_dir()
    idx_p = index_path or resolve_final_index_path(data_dir=dd)
    items: list[dict[str, Any]] = config.get("items", [])
    workspace_root = dd.parent.parent

    index_data = load_final_index(idx_p)
    index_items = index_data.setdefault("items", {})

    results: list[dict[str, Any]] = []
    all_succeeded = True

    for item in items:
        env_id = str(item["envelope_id"])
        source_folder = item.get("source_folder", "INBOX")
        raw_mid = item.get("message_id") or item.get("raw_message_id", "")
        norm_mid = normalize_message_id(raw_mid)
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

        # 1. Routing
        if action_type == "copy_as_move" and target_folder and target_folder != source_folder:
            try:
                # Idempotent check: check if already in target folder
                new_env_id = verify_in_target_folder(
                    target_folder,
                    norm_mid,
                    subject=subject,
                    from_addr=from_str,
                    date_str=date_str,
                    account=account,
                )
                if not new_env_id:
                    run_himalaya(["message", "copy", target_folder, env_id, "-f", source_folder], account=account)
                    new_env_id = verify_in_target_folder(
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
                        run_himalaya(["message", "delete", env_id, "-f", source_folder], account=account)
                    except Exception:
                        pass
                else:
                    routing_ok = False
            except Exception:
                routing_ok = False
        elif action_type == "keep_in_folder" or not target_folder or target_folder == source_folder:
            new_env_id = env_id
            routing_ok = True
        elif action_type == "delete":
            try:
                run_himalaya(["message", "delete", env_id, "-f", source_folder], account=account)
                routing_ok = True
                final_folder = "Trash"
                new_env_id = env_id
            except Exception:
                routing_ok = True
                final_folder = "Trash"
                new_env_id = env_id

        # 2. Evidence update
        if evidence_spec and norm_mid:
            try:
                ev_ok = update_evidence_file(evidence_spec, norm_mid, workspace_root=workspace_root)
                ref_source_status = "ok" if ev_ok else "fail"
            except Exception:
                ref_source_status = "fail"

        # 3. Final location index entry
        if routing_ok and norm_mid:
            idx_entry = {
                "message_id": norm_mid,
                "backend": "himalaya",
                "final_folder": final_folder,
                "envelope_id": str(new_env_id) if new_env_id else str(env_id),
                "in_reply_to": item.get("in_reply_to", ""),
                "references": item.get("references", []),
                "updated_at": utc_now_iso(),
            }
            index_items[norm_mid] = idx_entry
            index_ok = True

        # 4. Action logging
        if routing_ok:
            log_entry = {
                "timestamp": utc_now_iso(),
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
            append_action_log_entry(dd, log_entry)
            meta_ok = True

            # If needs reply, append to replies-needed.jsonl
            if decision.get("needs_reply"):
                rep_entry = {
                    "timestamp": utc_now_iso(),
                    "envelope_id": str(new_env_id) if new_env_id else str(env_id),
                    "message_id": norm_mid,
                    "subject": subject,
                    "from": from_str,
                    "folder": final_folder,
                    "reply_status": "needed",
                    "reply_note": notes,
                }
                append_replies_needed_entry(dd, rep_entry)

        item_success = routing_ok and index_ok and meta_ok
        if not item_success:
            all_succeeded = False

        results.append({
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
        })

    # Save index atomically once for the whole batch
    if index_items:
        index_data["updated_at"] = utc_now_iso()
        save_final_index_atomic(idx_p, index_data)

    return {
        "ok": all_succeeded,
        "mode": "execute",
        "total_processed": len(results),
        "all_succeeded": all_succeeded,
        "results": results,
    }


# ==============================================================================
# Verify Mode
# ==============================================================================

def run_verify_mode(
    config: dict[str, Any],
    account: str | None = None,
    data_dir: Path | None = None,
    index_path: Path | None = None,
) -> dict[str, Any]:
    dd = data_dir or resolve_data_dir()
    idx_p = index_path or resolve_final_index_path(data_dir=dd)
    check_folders = bool(config.get("check_folders", False))
    output_file = config.get("output_file")

    target_items: list[dict[str, Any]] = []
    if "items" in config and isinstance(config["items"], list):
        target_items = config["items"]
    elif "message_ids" in config and isinstance(config["message_ids"], list):
        target_items = [{"message_id": mid} for mid in config["message_ids"]]
    elif "batch_file" in config:
        bf = Path(config["batch_file"]).expanduser().resolve()
        if bf.exists():
            with bf.open("r", encoding="utf-8") as f:
                bdata = json.load(f)
                if isinstance(bdata, list):
                    target_items = bdata
                elif isinstance(bdata, dict):
                    target_items = bdata.get("items") or bdata.get("results") or []

    index_data = load_final_index(idx_p)
    index_items = index_data.get("items", {})

    action_log_path = dd / "action-log.jsonl"
    action_log_map: dict[str, dict[str, Any]] = {}
    if action_log_path.exists():
        with action_log_path.open("r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    m = normalize_message_id(row.get("message_id", ""))
                    if m:
                        action_log_map[m] = row
                except Exception:
                    pass

    workspace_root = dd.parent.parent
    references_root = workspace_root / "memory" / "references"

    results: list[dict[str, Any]] = []
    all_consistent = True

    for item in target_items:
        raw_mid = item.get("message_id") or item.get("raw_message_id", "")
        norm_mid = normalize_message_id(raw_mid)
        if not norm_mid:
            continue

        subject = item.get("subject", "")
        expected_folder = item.get("final_folder") or item.get("target_folder")

        # 1. Index check
        idx_entry = index_items.get(norm_mid)
        in_index = idx_entry is not None
        indexed_folder = idx_entry.get("final_folder") if idx_entry else None
        indexed_env_id = idx_entry.get("envelope_id") if idx_entry else None

        # 2. Action log check
        log_entry = action_log_map.get(norm_mid)
        in_log = log_entry is not None
        logged_folder = log_entry.get("action", {}).get("target_folder") if log_entry else None

        # 3. Evidence check
        in_evidence: bool | None = None
        evidence_file = item.get("evidence", {}).get("file") if isinstance(item.get("evidence"), dict) else item.get("evidence_file")
        if evidence_file:
            ev_path = (workspace_root / evidence_file).resolve() if not Path(evidence_file).is_absolute() else Path(evidence_file)
            if ev_path.exists():
                try:
                    ev_text = ev_path.read_text(encoding="utf-8")
                    in_evidence = norm_mid in ev_text.lower()
                except Exception:
                    in_evidence = False
            else:
                in_evidence = False
        elif references_root.exists():
            found_ev = False
            for md_file in references_root.glob("**/evidence/*.md"):
                try:
                    if norm_mid in md_file.read_text(encoding="utf-8").lower():
                        found_ev = True
                        break
                except Exception:
                    pass
            in_evidence = found_ev if found_ev else None

        # 4. Folder verification if requested
        folder_verified: bool | None = None
        current_env_id: str | None = None
        if check_folders and indexed_folder:
            verified = verify_in_target_folder(indexed_folder, norm_mid, subject=subject, account=account)
            folder_verified = verified is not None
            current_env_id = verified

        consistent = in_index and in_log
        if expected_folder and indexed_folder and expected_folder != indexed_folder:
            consistent = False
        if check_folders and folder_verified is False:
            consistent = False

        if not consistent:
            all_consistent = False

        results.append({
            "message_id": norm_mid,
            "subject": subject or (log_entry.get("subject") if log_entry else ""),
            "in_index": in_index,
            "indexed_folder": indexed_folder,
            "indexed_envelope_id": indexed_env_id,
            "in_action_log": in_log,
            "logged_folder": logged_folder,
            "in_evidence": in_evidence,
            "folder_verified": folder_verified,
            "current_envelope_id": current_env_id,
            "consistent": consistent,
        })

    out = {
        "ok": all_consistent,
        "mode": "verify",
        "total_checked": len(results),
        "all_consistent": all_consistent,
        "results": results,
    }

    if output_file:
        out_path = Path(output_file).expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
            f.write("\n")

    return out


# ==============================================================================
# Search Mode
# ==============================================================================

def run_search_mode(
    config: dict[str, Any],
    account: str | None = None,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    query = config.get("query", "").strip()
    raw_mids = config.get("message_ids", [])
    folders = config.get("folders")
    page_size = int(config.get("page_size", 50))
    threads = int(config.get("threads", 4))
    output_file = config.get("output_file")

    matches = search_mailbox(
        query=query,
        message_ids=raw_mids,
        folders=folders,
        page_size=page_size,
        threads=threads,
        account=account,
    )

    out = {
        "ok": True,
        "mode": "search",
        "total_found": len(matches),
        "matches": matches,
    }

    if output_file:
        out_path = Path(output_file).expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
            f.write("\n")

    return out


# ==============================================================================
# Resolve Mode
# ==============================================================================

def run_resolve_mode(
    config: dict[str, Any],
    data_dir: Path | None = None,
) -> dict[str, Any]:
    dd = data_dir or resolve_data_dir()
    items = config.get("items", [])
    if "message_id" in config:
        items.append({
            "message_id": config["message_id"],
            "status": config.get("status", "resolved"),
            "resolution": config.get("resolution", ""),
            "resolved_by": config.get("resolved_by_message_id"),
        })

    resolved_results: list[dict[str, Any]] = []
    all_resolved = True

    for item in items:
        mid = item.get("message_id", "")
        status = item.get("status", "resolved")
        resolution = item.get("resolution", "")
        resolved_by = item.get("resolved_by_message_id") or item.get("resolved_by")
        res = resolve_case(dd, mid, status=status, resolution=resolution, resolved_by=resolved_by)
        resolved_results.append(res)
        if not res.get("resolved"):
            all_resolved = False

    return {
        "ok": all_resolved,
        "mode": "resolve",
        "total_processed": len(resolved_results),
        "all_resolved": all_resolved,
        "results": resolved_results,
    }


# ==============================================================================
# Main Entry Point
# ==============================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Unified batch runner for mail-desk (inspect, execute, verify, search, resolve)."
    )
    parser.add_argument("--input", "-i", help="Path to input JSON file in data/")
    parser.add_argument("--stdin", action="store_true", help="Read JSON configuration from stdin")
    parser.add_argument("--account", "-a", help="Himalaya account override")
    parser.add_argument("--data-dir", help="Override path to data/mail-desk/")
    parser.add_argument("--index", help="Override path to final-location-index.json")
    parser.add_argument("--keep-input", action="store_true", help="Do not delete input file on success")
    args = parser.parse_args()

    data_dir = resolve_data_dir(args.data_dir)
    input_path: Path | None = None
    config: dict[str, Any] = {}

    if args.stdin:
        raw = sys.stdin.read().strip()
        if not raw:
            print(json.dumps({"ok": False, "error": "stdin payload is empty"}, ensure_ascii=False, indent=2))
            return 1
        config = json.loads(raw)
    elif args.input:
        input_path = Path(args.input).expanduser().resolve()
        if not input_path.exists():
            print(json.dumps({"ok": False, "error": f"Input file not found: {input_path}"}, ensure_ascii=False, indent=2))
            return 1
        with input_path.open("r", encoding="utf-8") as f:
            config = json.load(f)
    else:
        # Default auto-discovery in data/mail-desk/
        candidates = [
            data_dir / "batch-manifest.json",
            data_dir / "batch-inspect.json",
            data_dir / "batch-verify.json",
            data_dir / "batch-search.json",
            data_dir / "batch-resolve.json",
        ]
        found_input = False
        for cand in candidates:
            if cand.exists():
                input_path = cand
                with input_path.open("r", encoding="utf-8") as f:
                    config = json.load(f)
                found_input = True
                break
        if not found_input:
            candidate_names = ", ".join(c.name for c in candidates)
            err_msg = (
                "Neither --input nor --stdin was provided, and no default input file "
                f"({candidate_names}) was found in {data_dir}."
            )
            print(json.dumps({"ok": False, "error": err_msg}, ensure_ascii=False, indent=2))
            return 1

    mode = config.get("mode", "execute").lower()
    index_path = resolve_final_index_path(args.index, data_dir=data_dir)
    account = args.account or config.get("account")

    if mode in ("inspect", "fetch"):
        out = run_inspect_mode(config, account=account, data_dir=data_dir)
    elif mode in ("execute", "process"):
        out = run_execute_mode(config, account=account, data_dir=data_dir, index_path=index_path)
    elif mode in ("verify", "validate", "check"):
        out = run_verify_mode(config, account=account, data_dir=data_dir, index_path=index_path)
    elif mode in ("search", "locate", "find"):
        out = run_search_mode(config, account=account, data_dir=data_dir)
    elif mode in ("resolve", "archive"):
        out = run_resolve_mode(config, data_dir=data_dir)
    else:
        print(json.dumps({"ok": False, "error": f"Unsupported mode: {mode}"}, ensure_ascii=False, indent=2))
        return 1

    # Delete input file on confirmed success if requested
    delete_input = config.get("delete_input_on_success", True) and not args.keep_input
    if input_path and input_path.exists() and delete_input and out.get("ok"):
        try:
            input_path.unlink()
            out["input_file_deleted"] = True
        except Exception as e:
            out["input_file_deleted"] = False
            out["input_file_delete_error"] = str(e)

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        raise SystemExit(1)
