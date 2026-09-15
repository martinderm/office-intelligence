"""Himalaya CLI adapter and IMAP interaction utilities."""

from __future__ import annotations

import concurrent.futures
import email
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from typing import Any

from .attachments import inspect_mime_tree, validate_attachment_candidate_metadata
from .common import normalize_message_id


class HimalayaInvocationError(RuntimeError):
    """A fail-closed, classified Himalaya bootstrap or command failure."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def _default_himalaya_config_path() -> Path:
    """Return the platform's non-interactive Himalaya config location."""
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "himalaya" / "config.toml"
    return Path.home() / ".config" / "himalaya" / "config.toml"


def resolve_himalaya_invocation() -> tuple[str, Path]:
    """Resolve an installed CLI and a readable config without invoking either.

    ``HIMALAYA_CONFIG`` is the only supported override.  It is intentionally a
    config *path*, not an arbitrary command hook: accepting a command string
    would turn workspace configuration into a shell-execution boundary.
    """
    configured = os.environ.get("HIMALAYA_CONFIG")
    config_path = Path(configured).expanduser() if configured else _default_himalaya_config_path()
    if not config_path.is_absolute():
        raise HimalayaInvocationError(
            "himalaya_config_invalid", "HIMALAYA_CONFIG must be an absolute configuration file path."
        )
    if not config_path.is_file():
        raise HimalayaInvocationError(
            "himalaya_config_missing", "Himalaya configuration is missing; refusing interactive setup."
        )
    executable = shutil.which("himalaya")
    if not executable:
        raise HimalayaInvocationError(
            "himalaya_unavailable", "Himalaya executable is unavailable; no mailbox command was started."
        )
    return executable, config_path


def build_himalaya_command(args: list[str], account: str | None = None) -> list[str]:
    """Build a shell-free CLI command after fail-fast bootstrap validation."""
    if not isinstance(args, list) or not args or any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in args):
        raise ValueError("Himalaya arguments must be a non-empty list of non-empty text tokens.")
    executable, config_path = resolve_himalaya_invocation()
    return [executable, "-c", str(config_path)] + _insert_account_arg(args, account)


def _insert_account_arg(args: list[str], account: str | None) -> list[str]:
    """Insert -a <account> into args at the correct position for Himalaya CLI v1.x."""
    if not account or "-a" in args or "--account" in args:
        return list(args)

    cmd_tokens = list(args)
    flags_with_val = {"-c", "--config", "-o", "--output"}
    boolean_flags = {"--quiet", "--debug", "--trace", "-h", "--help", "-V", "--version"}

    i = 0
    while i < len(cmd_tokens):
        token = cmd_tokens[i]
        if token in flags_with_val:
            i += 2
            continue
        if token in boolean_flags or token.startswith("-"):
            i += 1
            continue
        if i + 1 < len(cmd_tokens) and not cmd_tokens[i + 1].startswith("-"):
            return cmd_tokens[: i + 2] + ["-a", account] + cmd_tokens[i + 2 :]
        else:
            return cmd_tokens[: i + 1] + ["-a", account] + cmd_tokens[i + 1 :]

    return cmd_tokens + ["-a", account]


def run_himalaya(args: list[str], account: str | None = None, timeout: int = 35, max_retries: int = 5) -> str:
    """Execute a non-interactive Himalaya command, retrying transient failures only."""
    if not isinstance(timeout, int) or timeout <= 0:
        raise ValueError("timeout must be a positive integer.")
    if not isinstance(max_retries, int) or max_retries < 1:
        raise ValueError("max_retries must be at least one.")
    env_vars = os.environ.copy()
    env_vars["PAGER"] = "cat"
    cmd = build_himalaya_command(args, account)

    last_err = None
    for attempt in range(max_retries):
        try:
            res = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env_vars,
                timeout=timeout,
            )
            if res.returncode != 0:
                err_msg = res.stderr.strip()
                # Check for transient connection errors
                if _is_transient_himalaya_error(err_msg):
                    last_err = HimalayaInvocationError("himalaya_transient", f"Himalaya transient error: {err_msg}")
                    if attempt < max_retries - 1:
                        time.sleep(2.0 * (attempt + 1))
                        continue
                    break
                raise HimalayaInvocationError("himalaya_command_failed", f"Himalaya failed: {err_msg}")
            return res.stdout
        except subprocess.TimeoutExpired as te:
            raise HimalayaInvocationError(
                "himalaya_timeout", "Himalaya timed out; refusing to retry the mailbox command."
            ) from te

    raise last_err or HimalayaInvocationError("himalaya_timeout", "Himalaya timed out.")


def _is_transient_himalaya_error(stderr: str) -> bool:
    """Keep retries narrowly limited to transport failures."""
    message = stderr.lower()
    return any(token in message for token in ("10054", "tls stream", "cannot connect", "broken pipe", "connection reset", "timed out"))


def fetch_raw_message_eml(
    env_id: str | int,
    folder: str = "INBOX",
    account: str | None = None,
    timeout: int = 30,
) -> bytes:
    """Fetch raw RFC 822 .eml bytes via himalaya message export -F."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        dest_eml = Path(tmp_dir) / f"msg_{env_id}.eml"
        args = [
            "message", "export", str(env_id),
            "-f", folder,
            "-F",
            "-d", str(dest_eml),
        ]
        run_himalaya(args, account=account, timeout=timeout, max_retries=2)
        if not dest_eml.exists() or dest_eml.stat().st_size == 0:
            raise RuntimeError(f"Himalaya export produced empty or missing .eml for envelope {env_id}")
        return dest_eml.read_bytes()


def get_single_email_details(
    env_id: str | int,
    folder: str = "INBOX",
    account: str | None = None,
    preview_lines: int = 30,
    fallback_envelope: dict[str, Any] | None = None,
    full_body: bool = False,
    inspect_attachments: bool = True,
    raw_eml: bytes | None = None,
) -> dict[str, Any]:
    """Fetch headers, preview/body, and structured MIME attachments for one envelope."""
    attachments_list: list[dict[str, Any]] = []
    attachment_status: str = "none"
    attachment_error: str | None = None

    if raw_eml is not None:
        try:
            attachments_list = inspect_mime_tree(raw_eml)
            inv_reasons = []
            for a in attachments_list:
                v, r = validate_attachment_candidate_metadata(a)
                if not v:
                    inv_reasons.append(r)
            if inv_reasons:
                attachments_list = []
                attachment_status = "attachment_inventory_unavailable"
                attachment_error = f"Incomplete attachment inventory in MIME: {'; '.join(inv_reasons)}"
            else:
                attachment_status = "available" if attachments_list else "none"
            eml_b = raw_eml if isinstance(raw_eml, bytes) else raw_eml.encode("utf-8", errors="replace")
            msg_obj = email.message_from_bytes(eml_b, policy=email.policy.default)
            raw_mid = str(msg_obj.get("Message-ID", "") or "")
            norm_mid = normalize_message_id(raw_mid) if raw_mid else ""
            subj = str(msg_obj.get("Subject", "") or "")
            from_hdr = str(msg_obj.get("From", "") or "")
            to_hdr = str(msg_obj.get("To", "") or "")
            date_hdr = str(msg_obj.get("Date", "") or "")
            in_reply_to = str(msg_obj.get("In-Reply-To", "") or "")
            references = str(msg_obj.get("References", "") or "")

            body_text = ""
            try:
                body_part = msg_obj.get_body(preferencelist=("plain", "html"))
                if body_part:
                    content = body_part.get_content()
                    body_text = content if isinstance(content, str) else str(content or "")
            except Exception:
                pass
            if not body_text:
                for part in msg_obj.walk():
                    if not part.is_multipart() and part.get_content_type().lower() in ("text/plain", "text/html"):
                        payload = part.get_payload(decode=True)
                        if isinstance(payload, bytes):
                            body_text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                            break

            body_lines = body_text.splitlines()
            preview = "\n".join(body_lines if full_body else body_lines[:preview_lines])

            return {
                "envelope_id": str(env_id),
                "folder": folder,
                "message_id": norm_mid,
                "raw_message_id": raw_mid,
                "subject": subj or (fallback_envelope.get("subject", "") if fallback_envelope else ""),
                "from": from_hdr,
                "to": to_hdr,
                "date": date_hdr,
                "in_reply_to": in_reply_to,
                "references": references,
                "preview": preview,
                "attachments": attachments_list,
                "attachment_status": attachment_status,
                "attachment_error": attachment_error,
                "error": None,
                "read_level": "full_body" if full_body else "preview",
            }
        except Exception as exc:
            attachments_list = []
            attachment_status = "attachment_inventory_unavailable"
            attachment_error = f"Failed to parse MIME tree: {exc}"
    elif fallback_envelope and fallback_envelope.get("has_attachment") is False:
        attachments_list = []
        attachment_status = "none"
        attachment_error = None
    elif inspect_attachments:
        try:
            eml_data = fetch_raw_message_eml(env_id, folder=folder, account=account)
            attachments_list = inspect_mime_tree(eml_data)
            inv_reasons = []
            for a in attachments_list:
                v, r = validate_attachment_candidate_metadata(a)
                if not v:
                    inv_reasons.append(r)
            if inv_reasons:
                attachments_list = []
                attachment_status = "attachment_inventory_unavailable"
                attachment_error = f"Incomplete attachment inventory in MIME: {'; '.join(inv_reasons)}"
            else:
                attachment_status = "available" if attachments_list else "none"
                attachment_error = None
        except Exception as exc:
            attachments_list = []
            attachment_status = "attachment_inventory_unavailable"
            attachment_error = f"Failed to export or inspect message MIME: {exc}"
    elif fallback_envelope and "attachments" in fallback_envelope:
        # Fallback attachments without live MIME inspection cannot be trusted as 'available'
        attachments_list = []
        attachment_status = "attachment_inventory_unavailable"
        attachment_error = "Fallback envelope attachments cannot be trusted without verified MIME inspection"

    args = [
        "message", "read",
        "-H", "Message-Id", "-H", "In-Reply-To", "-H", "References",
        "-H", "From", "-H", "To", "-H", "Cc", "-H", "Date", "-H", "Subject",
        "-f", folder, str(env_id),
    ]
    if not full_body:
        args.insert(2, "--preview")
    stdout = None
    last_err = None
    read_timeout = 30 if full_body else 12
    try:
        stdout = run_himalaya(args, account=account, timeout=read_timeout, max_retries=2)
    except Exception as e:
        last_err = e

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
        if full_body:
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
                "attachments": attachments_list,
                "attachment_status": attachment_status,
                "attachment_error": attachment_error,
                "error": str(last_err) if last_err else "Full message read failed.",
                "read_level": "full_body",
            }
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
            "attachments": attachments_list,
            "attachment_status": attachment_status,
            "attachment_error": attachment_error,
            "error": None if norm_mid else str(last_err),
            "read_level": "preview",
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
        read_error = None
        if not headers:
            read_error = f"Envelope {env_id} in folder '{folder}' returned no message headers."

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
            "preview": "\n".join(body_lines if full_body else body_lines[:preview_lines]),
            "attachments": attachments_list,
            "attachment_status": attachment_status,
            "attachment_error": attachment_error,
            "error": read_error,
            "read_level": "full_body" if full_body else "preview",
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
            "attachments": attachments_list,
            "attachment_status": attachment_status,
            "attachment_error": attachment_error,
            "error": str(e),
            "read_level": "full_body" if full_body else "preview",
        }


def verify_in_target_folder(
    target_folder: str,
    target_msg_id: str,
    subject: str = "",
    from_addr: str = "",
    date_str: str = "",
    account: str | None = None,
    candidate_env_id: str | None = None,
) -> str | None:
    """Verify presence of a message in target folder and return its new envelope_id."""
    norm_target = normalize_message_id(target_msg_id)

    # Fast-path: if a specific candidate envelope is known (e.g. retained in-place), test it directly
    if candidate_env_id:
        try:
            h_out = run_himalaya(["message", "read", "--preview", "-H", "Message-Id", "-f", target_folder, str(candidate_env_id)], account=account, timeout=10, max_retries=1)
            for line in h_out.splitlines():
                if line.lower().startswith("message-id:"):
                    m_id = normalize_message_id(line.split(":", 1)[1])
                    if m_id == norm_target:
                        return str(candidate_env_id)
        except Exception:
            pass

    try:
        out = run_himalaya(["-o", "json", "envelope", "list", "-f", target_folder, "-s", "150"], account=account, timeout=30, max_retries=2)
        if "[" in out:
            out = out[out.find("["):]
        envelopes = json.loads(out)
    except Exception:
        return None

    candidates: list[str] = []

    # 1. Filter candidates by matching subject (exact or prefix, or both empty)
    for env in envelopes:
        env_subj = env.get("subject", "").strip()
        if subject:
            if env_subj.lower() == subject.strip().lower() or (len(subject.strip()) > 15 and env_subj.startswith(subject.strip()[:30])):
                candidates.append(str(env["id"]))
        elif not subject and not env_subj:
            candidates.append(str(env["id"]))

    # 1b. Fallback to date only if no subject candidate found
    if not candidates and date_str:
        from .classifier import parse_date_to_year_month
        _, ymd = parse_date_to_year_month(date_str)
        for env in envelopes:
            env_date = env.get("date", "").strip()
            if ymd in env_date or date_str[:10] in env_date:
                candidates.append(str(env["id"]))

    # 2. Check candidate headers (newest first, limit to top 5)
    for cid in list(reversed(candidates))[:5]:
        try:
            h_out = run_himalaya(["message", "read", "--preview", "-H", "Message-Id", "-f", target_folder, cid], account=account, timeout=10, max_retries=1)
            for line in h_out.splitlines():
                if line.lower().startswith("message-id:"):
                    m_id = normalize_message_id(line.split(":", 1)[1])
                    if m_id == norm_target:
                        return cid
        except Exception:
            pass

    # 3. Quick fallback: check top 5 newest envelopes if no candidates were found
    if not candidates:
        for env in list(reversed(envelopes))[:5]:
            cid = str(env["id"])
            try:
                h_out = run_himalaya(["message", "read", "--preview", "-H", "Message-Id", "-f", target_folder, cid], account=account, timeout=10, max_retries=1)
                for line in h_out.splitlines():
                    if line.lower().startswith("message-id:"):
                        m_id = normalize_message_id(line.split(":", 1)[1])
                        if m_id == norm_target:
                            return cid
            except Exception:
                pass

    return None


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
        subj = str(env.get("subject") or "")
        from_info = env.get("from", {})
        from_str = (
            f"{str(from_info.get('name') or '')} {str(from_info.get('addr') or '')}"
            if isinstance(from_info, dict)
            else str(from_info or "")
        )
        date_str = str(env.get("date") or "")

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
                if query_str in str(e.get("subject") or "").lower() or query_str in str(e.get("from") or "").lower()
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
