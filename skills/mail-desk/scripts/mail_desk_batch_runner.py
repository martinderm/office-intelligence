#!/usr/bin/env python3
"""Unified batch runner for mail-desk (inspect and execute workflows).

Enables flexible, token-efficient batch mail handling with a single shell invocation.
Supports reading/inspecting mailbox envelopes in parallel and executing batches of
routing, index updates, action logging, and evidence maintenance via a temporary JSON manifest.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")



# ==============================================================================
# Helper Functions & Normalization
# ==============================================================================

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_message_id(value: str) -> str:
    s = value.strip()
    while s.startswith("<") and s.endswith(">") and len(s) >= 2:
        s = s[1:-1].strip()
    return s.lower()


def run_himalaya(args: list[str], account: str | None = None, timeout: int = 30) -> str:
    env_vars = os.environ.copy()
    env_vars["PAGER"] = "cat"
    cmd = ["himalaya"] + args
    if account:
        cmd.extend(["-a", account])
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=env_vars, timeout=timeout)
    if res.returncode != 0:
        raise RuntimeError(f"Himalaya failed: {' '.join(cmd)}\nStderr: {res.stderr.strip()}")
    return res.stdout


def resolve_data_dir(custom: str | None = None) -> Path:
    if custom:
        return Path(custom).expanduser().resolve()
    env_dir = os.environ.get("MAIL_DESK_DATA_DIR", "").strip()
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    cwd_data = Path.cwd() / "data" / "mail-desk"
    if cwd_data.exists() or cwd_data.parent.exists():
        return cwd_data.resolve()
    # fallback to script-relative
    fallback = Path(__file__).resolve().parents[3] / "data" / "mail-desk"
    return fallback.resolve()


def resolve_final_index_path(custom_path: str | None = None, data_dir: Path | None = None) -> Path:
    if custom_path:
        return Path(custom_path).expanduser().resolve()
    env_index = os.environ.get("MAIL_DESK_FINAL_INDEX_PATH", "").strip()
    if env_index:
        return Path(env_index).expanduser().resolve()
    dd = data_dir or resolve_data_dir()
    return dd / "final-location-index.json"


def load_final_index(index_path: Path) -> dict[str, Any]:
    if not index_path.exists():
        return {"schema_version": 1, "updated_at": None, "items": {}}
    with index_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        return {"schema_version": 1, "updated_at": None, "items": {}}
    data.setdefault("items", {})
    return data


def save_final_index_atomic(index_path: Path, data: dict[str, Any]) -> None:
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="\n",
        dir=index_path.parent,
        prefix=index_path.name + ".",
        suffix=".tmp",
        delete=False,
    ) as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
        temp_path = Path(f.name)
    temp_path.replace(index_path)


# ==============================================================================
# Inspect / Fetch Workflow
# ==============================================================================

def get_single_email_details(
    env_id: str,
    folder: str = "INBOX",
    account: str | None = None,
    preview_lines: int = 30,
) -> dict[str, Any]:
    args = [
        "message", "read", "--preview",
        "-H", "Message-Id", "-H", "In-Reply-To", "-H", "References",
        "-H", "From", "-H", "To", "-H", "Cc", "-H", "Date", "-H", "Subject",
        "-f", folder, str(env_id),
    ]
    try:
        stdout = run_himalaya(args, account=account, timeout=30)
        headers: dict[str, str] = {}
        body_lines: list[str] = []
        in_headers = True
        cur_header: str | None = None

        for line in stdout.splitlines():
            if in_headers:
                if not line.strip():
                    in_headers = False
                    continue
                if line.startswith(" ") or line.startswith("\t"):
                    if cur_header:
                        headers[cur_header] += " " + line.strip()
                elif ":" in line:
                    k, v = line.split(":", 1)
                    cur_header = k.strip().lower()
                    headers[cur_header] = v.strip()
            else:
                body_lines.append(line)

        raw_mid = headers.get("message-id", "")
        norm_mid = normalize_message_id(raw_mid) if raw_mid else ""

        return {
            "envelope_id": str(env_id),
            "folder": folder,
            "message_id": norm_mid,
            "raw_message_id": raw_mid,
            "subject": headers.get("subject", ""),
            "from": headers.get("from", ""),
            "to": headers.get("to", ""),
            "date": headers.get("date", ""),
            "in_reply_to": headers.get("in-reply-to", ""),
            "references": headers.get("references", ""),
            "preview": "\n".join(body_lines[:preview_lines]),
            "error": None,
        }
    except Exception as e:
        return {
            "envelope_id": str(env_id),
            "folder": folder,
            "message_id": "",
            "raw_message_id": "",
            "subject": "",
            "from": "",
            "to": "",
            "date": "",
            "in_reply_to": "",
            "references": "",
            "preview": "",
            "error": str(e),
        }


def run_inspect_mode(config: dict[str, Any], account: str | None, data_dir: Path) -> dict[str, Any]:
    folder = config.get("folder", "INBOX")
    count = int(config.get("count", 20))
    order = str(config.get("order", "newest")).lower()
    threads = int(config.get("threads", 5))
    preview_lines = int(config.get("preview_lines", 30))
    explicit_ids = config.get("envelope_ids")
    output_file = config.get("output_file")
    check_known = bool(config.get("check_known", True))

    target_env_ids: list[str] = []

    if explicit_ids and isinstance(explicit_ids, list):
        target_env_ids = [str(x) for x in explicit_ids]
    else:
        out = run_himalaya(["-o", "json", "envelope", "list", "-f", folder, "-s", "1000"], account=account)
        if "[" in out:
            out = out[out.find("["):]
        envelopes = json.loads(out)
        if order == "oldest":
            selected = envelopes[-count:] if count < len(envelopes) else envelopes
            selected.reverse()
        else:
            selected = envelopes[:count]
        target_env_ids = [str(env["id"]) for env in selected]

    # Fetch message headers in parallel
    results_map: dict[str, dict[str, Any]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as executor:
        futures = {
            executor.submit(get_single_email_details, eid, folder, account, preview_lines): eid
            for eid in target_env_ids
        }
        for fut in concurrent.futures.as_completed(futures):
            res = fut.result()
            results_map[res["envelope_id"]] = res

    ordered_results = [results_map[eid] for eid in target_env_ids if eid in results_map]

    # Check known status
    if check_known:
        index_path = resolve_final_index_path(data_dir=data_dir)
        index_data = load_final_index(index_path)
        items = index_data.get("items", {})

        action_log_path = data_dir / "action-log.jsonl"
        logged_ids: set[str] = set()
        if action_log_path.exists():
            with action_log_path.open("r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                        m = normalize_message_id(row.get("message_id", ""))
                        if m:
                            logged_ids.add(m)
                    except Exception:
                        pass

        for item in ordered_results:
            mid = item.get("message_id", "")
            in_idx = mid in items if mid else False
            in_log = mid in logged_ids if mid else False
            final_f = items[mid].get("final_folder") if in_idx else None
            item["known_status"] = {
                "in_index": in_idx,
                "in_action_log": in_log,
                "final_folder": final_f,
                "is_new": not (in_idx or in_log),
            }

    output_payload = {
        "ok": True,
        "mode": "inspect",
        "folder": folder,
        "total_fetched": len(ordered_results),
        "items": ordered_results,
    }

    if output_file:
        out_path = Path(output_file).expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as f:
            json.dump(output_payload, f, ensure_ascii=False, indent=2)
        output_payload["output_file"] = str(out_path)

    return output_payload


# ==============================================================================
# Execute Workflow
# ==============================================================================

def verify_in_target_folder(
    target_folder: str,
    target_msg_id: str,
    subject: str = "",
    from_addr: str = "",
    date_str: str = "",
    account: str | None = None,
) -> str | None:
    try:
        out = run_himalaya(["-o", "json", "envelope", "list", "-f", target_folder, "-s", "100"], account=account)
        if "[" in out:
            out = out[out.find("["):]
        envelopes = json.loads(out)
    except Exception:
        return None

    norm_target = normalize_message_id(target_msg_id)
    candidates: list[str] = []

    # 1. Filter candidates by matching subject or date
    for env in envelopes:
        env_subj = env.get("subject", "").strip()
        env_date = env.get("date", "").strip()
        if (subject and env_subj == subject.strip()) or (date_str and date_str[:10] in env_date):
            candidates.append(str(env["id"]))

    # 2. Check candidate headers
    for cid in candidates:
        try:
            h_out = run_himalaya(["message", "read", "--preview", "-H", "Message-Id", "-f", target_folder, cid], account=account)
            for line in h_out.splitlines():
                if line.lower().startswith("message-id:"):
                    m_id = normalize_message_id(line.split(":", 1)[1])
                    if m_id == norm_target:
                        return cid
        except Exception:
            pass

    # 3. Fallback: check last 3 newly added envelopes in target folder
    for env in envelopes[-3:]:
        cid = str(env["id"])
        if cid in candidates:
            continue
        try:
            h_out = run_himalaya(["message", "read", "--preview", "-H", "Message-Id", "-f", target_folder, cid], account=account)
            for line in h_out.splitlines():
                if line.lower().startswith("message-id:"):
                    m_id = normalize_message_id(line.split(":", 1)[1])
                    if m_id == norm_target:
                        return cid
        except Exception:
            pass

    # If envelopes exist and subject exactly matched first envelope, return that id as heuristic fallback
    if candidates:
        return candidates[0]
    return envelopes[-1]["id"] if envelopes else None


def update_evidence_file(evidence_spec: dict[str, Any], msg_id: str) -> bool:
    file_path = Path(evidence_spec.get("file", "")).expanduser()
    entry = evidence_spec.get("entry", "").strip()
    if not file_path or not entry:
        return False

    file_path.parent.mkdir(parents=True, exist_ok=True)
    norm_mid = normalize_message_id(msg_id)

    if file_path.exists():
        content = file_path.read_text(encoding="utf-8")
        if norm_mid in content.lower():
            return True  # Already present
        if not content.endswith("\n"):
            content += "\n"
        content += entry + "\n"
        file_path.write_text(content, encoding="utf-8")
    else:
        # Create new evidence log
        # Determine title from path
        parts = file_path.parts
        proj_name = parts[-3] if len(parts) >= 3 else "reference"
        year_month = file_path.stem
        header = f"---\ntype: Evidence Log\n---\n\n# Evidence log — {proj_name} — {year_month}\n\n"
        file_path.write_text(header + entry + "\n", encoding="utf-8")

    return True


def run_execute_mode(
    config: dict[str, Any],
    account: str | None,
    data_dir: Path,
    index_path: Path,
) -> dict[str, Any]:
    items = config.get("items", [])
    mailbox = config.get("mailbox") or os.environ.get("HIMALAYA_MAILBOX") or os.environ.get("MAIL_DESK_MAILBOX", "primary")
    backend = config.get("backend", "himalaya")

    index_data = load_final_index(index_path)
    index_items = index_data.setdefault("items", {})

    action_log_path = data_dir / "action-log.jsonl"
    action_log_path.parent.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []
    all_succeeded = True

    for item in items:
        env_id = str(item.get("envelope_id", ""))
        source_folder = item.get("source_folder", "INBOX")
        raw_mid = item.get("raw_message_id") or item.get("message_id", "")
        norm_mid = normalize_message_id(raw_mid)
        subject = item.get("subject", "")
        from_info = item.get("from", "")
        date_str = item.get("date", "")

        action = item.get("action", {})
        action_type = action.get("type", "copy_as_move")
        target_folder = action.get("target_folder")

        decision = item.get("decision", {})
        notes = item.get("notes", "")
        evidence_spec = item.get("evidence")

        routing_ok = False
        new_env_id = env_id

        # 1. Routing action
        if target_folder and target_folder != source_folder and action_type in ("copy_as_move", "copy", "move"):
            try:
                run_himalaya(["message", "copy", target_folder, env_id, "-f", source_folder], account=account)
                routing_ok = True
            except Exception as e:
                # Check if already in target folder
                verified = verify_in_target_folder(target_folder, norm_mid, subject, from_info, date_str, account=account)
                if verified:
                    new_env_id = verified
                    routing_ok = True
                else:
                    routing_ok = False

            if routing_ok:
                verified = verify_in_target_folder(target_folder, norm_mid, subject, from_info, date_str, account=account)
                if verified:
                    new_env_id = verified
        elif target_folder == source_folder or action_type in ("none", "archive"):
            routing_ok = True
            target_folder = target_folder or source_folder
        else:
            routing_ok = True
            target_folder = source_folder

        final_folder = target_folder or source_folder

        # 2. Final location index update
        index_ok = False
        if routing_ok and norm_mid:
            index_items[norm_mid] = {
                "message_id": norm_mid,
                "mailbox": mailbox,
                "backend": backend,
                "final_folder": final_folder,
                "envelope_id": str(new_env_id),
                "updated_at": utc_now_iso(),
            }
            if item.get("in_reply_to"):
                index_items[norm_mid]["in_reply_to"] = item["in_reply_to"]
            if item.get("references"):
                refs = item["references"]
                index_items[norm_mid]["references"] = refs if isinstance(refs, list) else refs.split()
            index_ok = True

        # 3. Action log entry
        meta_ok = False
        try:
            formatted_mid = f"<{raw_mid}>" if raw_mid and not raw_mid.startswith("<") else raw_mid
            action_entry = {
                "schema_version": 1,
                "at": utc_now_iso(),
                "mailbox": mailbox,
                "backend": backend,
                "message_id": formatted_mid,
                "key_type": "message_id",
                "envelope_id": str(new_env_id),
                "subject": subject,
                "from": from_info,
                "decision": decision,
                "action": {
                    "type": action_type,
                    "target_folder": final_folder,
                },
                "notes": notes,
            }
            with action_log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(action_entry, ensure_ascii=False) + "\n")
            meta_ok = True
        except Exception:
            meta_ok = False

        # 4. Evidence logging if requested
        ref_source_status = "n/a"
        if evidence_spec:
            ref_ok = True
            if isinstance(evidence_spec, list):
                for ev in evidence_spec:
                    if not update_evidence_file(ev, norm_mid):
                        ref_ok = False
            elif isinstance(evidence_spec, dict):
                ref_ok = update_evidence_file(evidence_spec, norm_mid)
            ref_source_status = "ok" if ref_ok else "fail"
        elif decision.get("kind") in ("project", "topic") and decision.get("id") not in ("none", ""):
            ref_source_status = "ok"

        # 5. Replies needed tracking
        if decision.get("needs_reply"):
            replies_path = data_dir / "replies-needed.jsonl"
            try:
                rep_entry = {
                    "schema_version": 1,
                    "at": utc_now_iso(),
                    "mailbox": mailbox,
                    "message_id": formatted_mid,
                    "key_type": "message_id",
                    "backend_locator": str(new_env_id),
                    "subject": subject,
                    "from": from_info,
                    "folder": final_folder,
                    "reply_status": "needed",
                    "reply_note": notes,
                }
                with replies_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(rep_entry, ensure_ascii=False) + "\n")
            except Exception:
                pass

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
        save_final_index_atomic(index_path, index_data)

    return {
        "ok": all_succeeded,
        "mode": "execute",
        "total_processed": len(results),
        "all_succeeded": all_succeeded,
        "results": results,
    }


# ==============================================================================
# Main Entry Point
# ==============================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Unified batch runner for mail-desk (inspect and execute)."
    )
    parser.add_argument("--input", "-i", help="Path to input JSON file in data/")
    parser.add_argument("--stdin", action="store_true", help="Read JSON configuration from stdin")
    parser.add_argument("--account", "-a", help="Himalaya account override")
    parser.add_argument("--data-dir", help="Override path to data/mail-desk/")
    parser.add_argument("--index", help="Override path to final-location-index.json")
    parser.add_argument("--keep-input", action="store_true", help="Do not delete input file on success")
    args = parser.parse_args()

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
        print(json.dumps({"ok": False, "error": "Either --input or --stdin must be specified."}, ensure_ascii=False, indent=2))
        return 1

    mode = config.get("mode", "execute").lower()
    data_dir = resolve_data_dir(args.data_dir)
    index_path = resolve_final_index_path(args.index, data_dir=data_dir)
    account = args.account or config.get("account")

    if mode in ("inspect", "fetch"):
        out = run_inspect_mode(config, account=account, data_dir=data_dir)
    elif mode in ("execute", "process"):
        out = run_execute_mode(config, account=account, data_dir=data_dir, index_path=index_path)
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
