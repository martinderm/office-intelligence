"""Sent items indexer and thread response matcher."""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .common import normalize_message_id, utc_now_iso, resolve_data_dir
from .himalaya import run_himalaya, get_single_email_details


def clean_subject(subj: str) -> str:
    """Strip reply/forward prefixes and excess whitespace."""
    s = subj.strip()
    prefix_re = re.compile(r"^\s*(re|aw|antw|wg|fwd|wtrlt)\s*:\s*", re.IGNORECASE)
    while prefix_re.match(s):
        s = prefix_re.sub("", s).strip()
    return re.sub(r"\s+", " ", s).lower()


def load_sent_index(data_dir: Path | None = None) -> dict[str, Any]:
    """Load sent-index.jsonl into structured lookup maps."""
    dd = data_dir or resolve_data_dir()
    sent_path = dd / "sent-index.jsonl"

    by_message_id: dict[str, dict[str, Any]] = {}
    by_in_reply_to: dict[str, list[dict[str, Any]]] = {}
    by_reference: dict[str, list[dict[str, Any]]] = {}
    by_subject_clean: dict[str, list[dict[str, Any]]] = {}
    all_entries: list[dict[str, Any]] = []

    if not sent_path.exists():
        return {
            "by_message_id": by_message_id,
            "by_in_reply_to": by_in_reply_to,
            "by_reference": by_reference,
            "by_subject_clean": by_subject_clean,
            "all_entries": all_entries,
        }

    with sent_path.open("r", encoding="utf-8") as f:
        for line in f:
            line_str = line.strip()
            if not line_str:
                continue
            try:
                entry = json.loads(line_str)
            except Exception:
                continue

            mid = normalize_message_id(str(entry.get("message_id", "")))
            if not mid:
                continue

            entry["norm_message_id"] = mid
            all_entries.append(entry)
            by_message_id[mid] = entry

            irt = normalize_message_id(str(entry.get("in_reply_to", "")))
            if irt:
                by_in_reply_to.setdefault(irt, []).append(entry)

            for ref in entry.get("references", []):
                ref_norm = normalize_message_id(str(ref))
                if ref_norm:
                    by_reference.setdefault(ref_norm, []).append(entry)

            subj = entry.get("subject", "")
            if subj:
                cs = clean_subject(subj)
                if cs:
                    by_subject_clean.setdefault(cs, []).append(entry)

    return {
        "by_message_id": by_message_id,
        "by_in_reply_to": by_in_reply_to,
        "by_reference": by_reference,
        "by_subject_clean": by_subject_clean,
        "all_entries": all_entries,
    }


def append_sent_index_entries(
    entries: list[dict[str, Any]],
    data_dir: Path | None = None,
) -> int:
    """Append new entries to sent-index.jsonl, skipping duplicates by message_id."""
    if not entries:
        return 0

    dd = data_dir or resolve_data_dir()
    sent_path = dd / "sent-index.jsonl"
    sent_path.parent.mkdir(parents=True, exist_ok=True)

    existing_mids: set[str] = set()
    if sent_path.exists():
        with sent_path.open("r", encoding="utf-8") as f:
            for line in f:
                line_str = line.strip()
                if not line_str:
                    continue
                try:
                    e = json.loads(line_str)
                    mid = normalize_message_id(str(e.get("message_id", "")))
                    if mid:
                        existing_mids.add(mid)
                except Exception:
                    continue

    added_count = 0
    with sent_path.open("a", encoding="utf-8") as f:
        for entry in entries:
            mid = normalize_message_id(str(entry.get("message_id", "")))
            if not mid or mid in existing_mids:
                continue
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            existing_mids.add(mid)
            added_count += 1

    return added_count


def sync_sent_items_by_date(
    date_str: str,
    folder: str = "Sent Items",
    account: str | None = None,
    data_dir: Path | None = None,
) -> int:
    """Fetch envelopes for a specific date (YYYY-MM-DD) from Sent Items and stream into sent-index.jsonl."""
    dd = data_dir or resolve_data_dir()
    existing_index = load_sent_index(dd)
    existing_eids = {str(e.get("sent_envelope_id", "")) for e in existing_index["all_entries"] if e.get("sent_envelope_id")}
    existing_mids = set(existing_index["by_message_id"].keys())

    # Date query via Himalaya (fast single-day filter)
    try:
        out = run_himalaya(
            ["-o", "json", "envelope", "list", "-f", folder, "-s", "100", f"date {date_str}"],
            account=account,
            timeout=30,
        )
        if "[" in out:
            out = out[out.find("["):]
        envs = json.loads(out)
    except Exception:
        return 0

    if not isinstance(envs, list) or not envs:
        return 0

    unhandled_envs = [e for e in envs if str(e.get("id")) not in existing_eids]
    if not unhandled_envs:
        return 0

    sent_path = dd / "sent-index.jsonl"
    sent_path.parent.mkdir(parents=True, exist_ok=True)
    added_count = 0
    now_iso = utc_now_iso()

    with sent_path.open("a", encoding="utf-8") as out_f:
        for env in unhandled_envs:
            eid = str(env.get("id"))
            email_res = get_single_email_details(eid, folder, account=account, preview_lines=5, fallback_envelope=env)
            mid = normalize_message_id(str(email_res.get("message_id", "")))
            if not mid:
                mid = f"sent-env-{eid}"

            if mid in existing_mids:
                continue

            raw_mid = email_res.get("raw_message_id") or mid
            raw_irt = email_res.get("in_reply_to", "")
            raw_refs = email_res.get("references", [])
            if isinstance(raw_refs, str):
                raw_refs = [r for r in raw_refs.split() if r]

            to_field = email_res.get("to", "")
            to_list = [t.strip() for t in to_field.split(",") if t.strip()] if isinstance(to_field, str) else to_field

            entry: dict[str, Any] = {
                "schema_version": 1,
                "at": email_res.get("date") or now_iso,
                "updated_at": now_iso,
                "mailbox": "BOKU-MARTIN",
                "message_id": f"<{raw_mid}>" if not raw_mid.startswith("<") else raw_mid,
                "in_reply_to": f"<{raw_irt}>" if raw_irt and not raw_irt.startswith("<") else (raw_irt or ""),
                "references": [f"<{r}>" if not str(r).startswith("<") else str(r) for r in raw_refs],
                "subject": email_res.get("subject", ""),
                "from": email_res.get("from", ""),
                "to": to_list,
                "folder": folder,
                "sent_envelope_id": eid,
                "backend_locator": f"{eid}|{mid}",
                "note": f"Gesendet: {email_res.get('subject', '')}",
            }

            out_f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            out_f.flush()
            existing_mids.add(mid)
            existing_eids.add(eid)
            added_count += 1

    return added_count


def sync_sent_items_by_dates(
    dates: list[str],
    folder: str = "Sent Items",
    account: str | None = None,
    data_dir: Path | None = None,
    include_next_day: bool = True,
) -> int:
    """Sync Sent Items for a list of ISO dates (e.g. ['2026-01-19', '2026-01-20'])."""
    unique_dates: set[str] = set()
    for d in dates:
        d_clean = d.strip()[:10]
        if re.match(r"^\d{4}-\d{2}-\d{2}$", d_clean):
            unique_dates.add(d_clean)
            if include_next_day:
                try:
                    dt = datetime.strptime(d_clean, "%Y-%m-%d")
                    next_dt = dt + timedelta(days=1)
                    unique_dates.add(next_dt.strftime("%Y-%m-%d"))
                except Exception:
                    pass

    total_added = 0
    for d_str in sorted(unique_dates):
        added = sync_sent_items_by_date(d_str, folder=folder, account=account, data_dir=data_dir)
        total_added += added

    return total_added


def sync_sent_items(
    count: int = 100,
    folder: str = "Sent Items",
    dates: list[str] | None = None,
    account: str | None = None,
    data_dir: Path | None = None,
    workspace_root: Path | None = None,
) -> tuple[int, int]:
    """Sync sent items either by explicit dates (fast) or by recent days."""
    dd = data_dir or resolve_data_dir()
    if dates:
        added = sync_sent_items_by_dates(dates, folder=folder, account=account, data_dir=dd)
        return len(dates), added

    # If no dates provided, sync today and past 7 days
    today = datetime.now()
    recent_dates = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
    added = sync_sent_items_by_dates(recent_dates, folder=folder, account=account, data_dir=dd)
    return len(recent_dates), added


def parse_date_to_datetime(s: str) -> datetime | None:
    """Parse various email/ISO date formats to timezone-naive datetime."""
    if not s:
        return None
    s_str = str(s).strip()
    if len(s_str) >= 10 and s_str[4] == "-" and s_str[7] == "-":
        try:
            return datetime.strptime(s_str[:10], "%Y-%m-%d")
        except Exception:
            pass
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(s_str)
        if dt:
            return dt.replace(tzinfo=None)
    except Exception:
        pass
    return None


def extract_email_address(s: str) -> str:
    """Extract raw email address from header string like 'Name <user@domain.com>'."""
    if not s:
        return ""
    m = re.search(r"[\w\.-]+@[\w\.-]+", s)
    return m.group(0).lower() if m else s.strip().lower()


def check_if_replied(
    email: dict[str, Any],
    sent_lookup: dict[str, Any],
) -> dict[str, Any] | None:
    """Check whether an incoming email has been replied to according to sent items."""
    norm_mid = normalize_message_id(str(email.get("message_id", "")))
    if not norm_mid:
        return None

    # 1. Direct match on in_reply_to (unambiguous technical link, even if subject changed)
    irt_matches = sent_lookup.get("by_in_reply_to", {}).get(norm_mid, [])
    if irt_matches:
        latest = irt_matches[-1]
        return {
            "replied": True,
            "confidence": "high",
            "match_type": "in_reply_to",
            "sent_message_id": latest.get("message_id", ""),
            "sent_date": latest.get("at", ""),
            "sent_envelope_id": latest.get("sent_envelope_id", ""),
            "sent_subject": latest.get("subject", ""),
        }

    # 2. Match on references (unambiguous thread link)
    ref_matches = sent_lookup.get("by_reference", {}).get(norm_mid, [])
    if ref_matches:
        latest = ref_matches[-1]
        return {
            "replied": True,
            "confidence": "high",
            "match_type": "references",
            "sent_message_id": latest.get("message_id", ""),
            "sent_date": latest.get("at", ""),
            "sent_envelope_id": latest.get("sent_envelope_id", ""),
            "sent_subject": latest.get("subject", ""),
        }

    # 3. Clean subject match (heuristically for replies where In-Reply-To was omitted)
    subj = email.get("subject", "")
    if subj:
        cs = clean_subject(subj)
        subj_matches = sent_lookup.get("by_subject_clean", {}).get(cs, [])
        if subj_matches:
            latest = subj_matches[-1]
            return {
                "replied": True,
                "confidence": "high",
                "match_type": "subject_clean",
                "sent_message_id": latest.get("message_id", ""),
                "sent_date": latest.get("at", ""),
                "sent_envelope_id": latest.get("sent_envelope_id", ""),
                "sent_subject": latest.get("subject", ""),
            }

    # 4. Context & Candidate Search:
    # When In-Reply-To is missing and subject changed, check for candidate sent emails
    # to the sender / partner in the same project context within 0-3 days.
    from_raw = email.get("from", "")
    from_addr = extract_email_address(from_raw)
    all_sent = sent_lookup.get("all_entries", [])

    if from_addr and all_sent:
        sender_domain = from_addr.split("@")[-1] if "@" in from_addr else ""
        stopwords = {"antwort", "betreff", "anfrage", "update", "fwd", "wtrlt", "2025", "2026", "boku", "mail"}
        keywords = [w.lower() for w in re.split(r"[\s\-_:/]+", subj) if len(w) >= 4 and w.lower() not in stopwords]

        in_date_raw = str(email.get("date") or email.get("at") or "")
        in_dt = parse_date_to_datetime(in_date_raw)

        best_candidate = None
        for s_entry in all_sent:
            to_field = s_entry.get("to", [])
            if isinstance(to_field, str):
                to_addrs = [extract_email_address(to_field)]
            else:
                to_addrs = [extract_email_address(t) for t in to_field]

            direct_to_sender = from_addr in to_addrs
            domain_to_sender = bool(sender_domain and sender_domain not in {"boku.ac.at", "gmail.com", "yahoo.com", "hotmail.com"} and any(sender_domain in t for t in to_addrs))

            if not (direct_to_sender or domain_to_sender):
                continue

            s_date_raw = str(s_entry.get("at", ""))
            s_dt = parse_date_to_datetime(s_date_raw)

            # Temporal check: Sent email should be on/after incoming email within 30 days
            if in_dt and s_dt:
                delta_days = (s_dt - in_dt).days
                if delta_days < -1 or delta_days > 30:
                    continue
                # If only recipient matched without keywords, require tighter window (<= 3 days)
                if not any(kw in s_entry.get("subject", "").lower() for kw in keywords) and delta_days > 3:
                    continue

            s_subj = s_entry.get("subject", "")
            s_subj_lower = s_subj.lower()
            s_date_str = s_dt.strftime("%Y-%m-%d") if s_dt else s_date_raw[:10]

            matched_kws = [kw for kw in keywords if kw in s_subj_lower]

            if matched_kws or direct_to_sender:
                candidate_confidence = "high" if (direct_to_sender and matched_kws) else "medium"
                best_candidate = {
                    "sent_message_id": s_entry.get("message_id", ""),
                    "sent_date": s_entry.get("at", ""),
                    "sent_envelope_id": s_entry.get("sent_envelope_id", ""),
                    "sent_subject": s_subj,
                    "matched_recipient": from_addr if direct_to_sender else to_addrs[0],
                    "matched_keywords": matched_kws,
                    "confidence": candidate_confidence,
                    "reason": f"Gesendet an {from_addr} am {s_date_str} mit Betreff '{s_subj}' (Keywords: {', '.join(matched_kws) if matched_kws else 'Empfänger-Match'})",
                }
                if candidate_confidence == "high":
                    break

        if best_candidate:
            return {
                "replied": False,
                "has_candidate": True,
                "confidence": best_candidate["confidence"],
                "candidate": best_candidate,
            }

    return None


def auto_resolve_replies_from_sent(data_dir: Path | None = None) -> dict[str, Any]:
    """Audit replies-needed.jsonl against sent-index.jsonl and resolve answered cases."""
    from .action_log import resolve_case

    dd = data_dir or resolve_data_dir()
    sent_idx = load_sent_index(dd)
    rn_path = dd / "replies-needed.jsonl"

    if not rn_path.exists():
        return {
            "ok": True,
            "total_checked": 0,
            "resolved_count": 0,
            "resolved_items": [],
            "remaining_open": [],
        }

    with rn_path.open("r", encoding="utf-8") as f:
        entries = [json.loads(line.strip()) for line in f if line.strip()]

    resolved_items: list[dict[str, Any]] = []
    remaining_open: list[dict[str, Any]] = []
    updated_entries: list[dict[str, Any]] = []
    needs_rewrite = False

    for entry in entries:
        reply_info = check_if_replied(entry, sent_idx)
        if reply_info and reply_info.get("replied"):
            mid = normalize_message_id(entry.get("message_id", ""))
            resolve_case(
                data_dir=dd,
                message_id=mid,
                status="replied",
                resolution=f"Beantwortet via Sent Items: {reply_info.get('sent_subject', '')}",
                resolved_by=reply_info.get("sent_message_id"),
            )
            resolved_items.append({
                "message_id": mid,
                "subject": entry.get("subject", ""),
                "sent_message_id": reply_info.get("sent_message_id"),
                "sent_subject": reply_info.get("sent_subject"),
                "status": "replied",
            })
            needs_rewrite = True
        else:
            cand = reply_info.get("candidate") if (reply_info and reply_info.get("has_candidate")) else None
            if entry.get("reply_candidate") != cand:
                if cand:
                    entry["reply_candidate"] = cand
                else:
                    entry.pop("reply_candidate", None)
                needs_rewrite = True
            updated_entries.append(entry)
            remaining_open.append({
                "message_id": entry.get("message_id"),
                "subject": entry.get("subject"),
                "from": entry.get("from"),
                "reply_candidate": entry.get("reply_candidate"),
            })

    if needs_rewrite:
        with rn_path.open("w", encoding="utf-8", newline="\n") as f:
            for e in updated_entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")

    return {
        "ok": True,
        "total_checked": len(entries),
        "resolved_count": len(resolved_items),
        "resolved_items": resolved_items,
        "remaining_open_count": len(remaining_open),
        "remaining_open": remaining_open,
    }
