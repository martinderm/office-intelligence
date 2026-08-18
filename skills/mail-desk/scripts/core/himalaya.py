"""Himalaya CLI adapter and IMAP interaction utilities."""

from __future__ import annotations

import concurrent.futures
import json
import os
import subprocess
import time
from typing import Any

from .common import normalize_message_id


def run_himalaya(args: list[str], account: str | None = None, timeout: int = 30) -> str:
    """Execute himalaya CLI command safely with UTF-8 replacement."""
    env_vars = os.environ.copy()
    env_vars["PAGER"] = "cat"
    cmd = ["himalaya"]
    if account:
        cmd.extend(["-a", account])
    cmd.extend(args)
    res = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env_vars,
        timeout=timeout,
    )
    if res.returncode != 0:
        raise RuntimeError(f"Himalaya failed: {' '.join(cmd)}\nStderr: {res.stderr.strip()}")
    return res.stdout


def get_single_email_details(
    env_id: str | int,
    folder: str = "INBOX",
    account: str | None = None,
    preview_lines: int = 30,
    fallback_envelope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fetch headers and body preview for a single envelope with fallback support."""
    args = [
        "message", "read", "--preview",
        "-H", "Message-Id", "-H", "In-Reply-To", "-H", "References",
        "-H", "From", "-H", "To", "-H", "Cc", "-H", "Date", "-H", "Subject",
        "-f", folder, str(env_id),
    ]
    stdout = None
    last_err = None
    for attempt in range(2):
        try:
            stdout = run_himalaya(args, account=account, timeout=20)
            if stdout:
                break
        except Exception as e:
            last_err = e
            time.sleep(0.5)

    fb_from = ""
    fb_subj = ""
    fb_to = ""
    fb_date = ""
    if fallback_envelope:
        fb_subj = fallback_envelope.get("subject", "")
        f_info = fallback_envelope.get("from", {})
        fb_from = f"{f_info.get('name', '')} <{f_info.get('addr', '')}>".strip() if isinstance(f_info, dict) else str(f_info)
        t_info = fallback_envelope.get("to", {})
        fb_to = f"{t_info.get('name', '')} <{t_info.get('addr', '')}>".strip() if isinstance(t_info, dict) else str(t_info)
        fb_date = fallback_envelope.get("date", "")

    if stdout is None:
        # Lightweight fallback: try reading just Message-ID
        raw_mid = ""
        try:
            mid_out = run_himalaya(["message", "read", "-H", "Message-Id", "-f", folder, str(env_id)], account=account, timeout=10)
            for line in mid_out.splitlines():
                if line.lower().startswith("message-id:"):
                    raw_mid = line.split(":", 1)[1].strip()
                    break
        except Exception:
            pass

        norm_mid = normalize_message_id(raw_mid) if raw_mid else ""
        return {
            "envelope_id": str(env_id),
            "folder": folder,
            "message_id": norm_mid,
            "raw_message_id": raw_mid,
            "subject": fb_subj,
            "from": fb_from,
            "to": fb_to,
            "date": fb_date,
            "in_reply_to": "",
            "references": "",
            "preview": "",
            "error": None if norm_mid else str(last_err),
        }

    try:
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
            "subject": headers.get("subject", "") or fb_subj,
            "from": headers.get("from", "") or fb_from,
            "to": headers.get("to", "") or fb_to,
            "date": headers.get("date", "") or fb_date,
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
            "subject": fb_subj,
            "from": fb_from,
            "to": fb_to,
            "date": fb_date,
            "in_reply_to": "",
            "references": "",
            "preview": "",
            "error": str(e),
        }


def verify_in_target_folder(
    target_folder: str,
    target_msg_id: str,
    subject: str = "",
    from_addr: str = "",
    date_str: str = "",
    account: str | None = None,
) -> str | None:
    """Verify presence of a message in target folder and return its new envelope_id."""
    try:
        out = run_himalaya(["-o", "json", "envelope", "list", "-f", target_folder, "-s", "100"], account=account, timeout=30)
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
            h_out = run_himalaya(["message", "read", "--preview", "-H", "Message-Id", "-f", target_folder, cid], account=account, timeout=15)
            for line in h_out.splitlines():
                if line.lower().startswith("message-id:"):
                    m_id = normalize_message_id(line.split(":", 1)[1])
                    if m_id == norm_target:
                        return cid
        except Exception:
            pass

    # 3. Fallback: check the 10 newest envelopes in target folder
    for env in envelopes[:10]:
        cid = str(env["id"])
        if cid in candidates:
            continue
        try:
            h_out = run_himalaya(["message", "read", "--preview", "-H", "Message-Id", "-f", target_folder, cid], account=account, timeout=15)
            for line in h_out.splitlines():
                if line.lower().startswith("message-id:"):
                    m_id = normalize_message_id(line.split(":", 1)[1])
                    if m_id == norm_target:
                        return cid
        except Exception:
            pass

    if candidates:
        return candidates[0]
    return envelopes[0]["id"] if envelopes else None


def search_mailbox(
    query: str = "",
    message_ids: list[str] | None = None,
    folders: list[str] | None = None,
    page_size: int = 50,
    threads: int = 4,
    account: str | None = None,
) -> list[dict[str, Any]]:
    """Search for messages across specified folders by query or message_ids."""
    query_str = query.strip().lower()
    target_mids = set(normalize_message_id(m) for m in message_ids) if message_ids else set()

    if not folders:
        try:
            f_out = run_himalaya(["folder", "list", "-o", "json"], account=account, timeout=30)
            if "[" in f_out:
                f_out = f_out[f_out.find("["):]
            folders = [f["name"] for f in json.loads(f_out)]
        except Exception:
            folders = ["INBOX", "Junk", "Trash", "Newsletter", "Themen/BOKU-Organisation"]

    matches: list[dict[str, Any]] = []

    def check_env_header(fld: str, env: dict[str, Any]) -> dict[str, Any] | None:
        eid = str(env.get("id", ""))
        subj = env.get("subject", "")
        from_info = env.get("from", {})
        from_str = from_info.get("name", "") + " " + from_info.get("addr", "") if isinstance(from_info, dict) else str(from_info)
        date_str = env.get("date", "")

        try:
            h = run_himalaya(["message", "read", "--preview", "-H", "Message-Id", "-f", fld, eid], account=account, timeout=15)
            env_mid = ""
            for line in h.splitlines():
                if line.lower().startswith("message-id:"):
                    env_mid = normalize_message_id(line.split(":", 1)[1])
                    break

            if target_mids and env_mid in target_mids:
                return {
                    "folder": fld,
                    "envelope_id": eid,
                    "message_id": env_mid,
                    "subject": subj,
                    "from": from_str.strip(),
                    "date": date_str,
                }
            if query_str and (query_str in subj.lower() or query_str in from_str.lower()):
                return {
                    "folder": fld,
                    "envelope_id": eid,
                    "message_id": env_mid,
                    "subject": subj,
                    "from": from_str.strip(),
                    "date": date_str,
                }
        except Exception:
            pass
        return None

    def search_single_folder(fld: str) -> list[dict[str, Any]]:
        found_in_folder: list[dict[str, Any]] = []
        try:
            out = run_himalaya(["-o", "json", "envelope", "list", "-f", fld, "-s", str(page_size)], account=account, timeout=30)
            if "[" in out:
                out = out[out.find("["):]
            envelopes = json.loads(out)
        except Exception:
            return found_in_folder

        if query_str and not target_mids:
            candidates = [
                e for e in envelopes
                if query_str in e.get("subject", "").lower() or query_str in str(e.get("from", "")).lower()
            ]
        else:
            candidates = envelopes

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as exec_env:
            futs = [exec_env.submit(check_env_header, fld, env) for env in candidates]
            for f in concurrent.futures.as_completed(futs):
                res = f.result()
                if res:
                    found_in_folder.append(res)

        return found_in_folder

    with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as executor:
        futures = {executor.submit(search_single_folder, fld): fld for fld in folders}
        for fut in concurrent.futures.as_completed(futures):
            res = fut.result()
            if res:
                matches.extend(res)

    return matches
