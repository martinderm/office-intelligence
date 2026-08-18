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


def check_if_replied(
    email: dict[str, Any],
    sent_lookup: dict[str, Any],
) -> dict[str, Any] | None:
    """Check whether an incoming email has been replied to according to sent items."""
    norm_mid = normalize_message_id(str(email.get("message_id", "")))
    if not norm_mid:
        return None

    # 1. Direct match on in_reply_to
    irt_matches = sent_lookup.get("by_in_reply_to", {}).get(norm_mid, [])
    if irt_matches:
        latest = irt_matches[-1]
        return {
            "replied": True,
            "match_type": "in_reply_to",
            "sent_message_id": latest.get("message_id", ""),
            "sent_date": latest.get("at", ""),
            "sent_envelope_id": latest.get("sent_envelope_id", ""),
            "sent_subject": latest.get("subject", ""),
        }

    # 2. Match on references
    ref_matches = sent_lookup.get("by_reference", {}).get(norm_mid, [])
    if ref_matches:
        latest = ref_matches[-1]
        return {
            "replied": True,
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
                "match_type": "subject_clean",
                "sent_message_id": latest.get("message_id", ""),
                "sent_date": latest.get("at", ""),
                "sent_envelope_id": latest.get("sent_envelope_id", ""),
                "sent_subject": latest.get("subject", ""),
            }

    return None
