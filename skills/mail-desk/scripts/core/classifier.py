"""Conservative classifier and manifest drafting for mail-desk."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .common import normalize_message_id, resolve_data_dir, resolve_evidence_dir
from .sent_indexer import check_if_replied, load_sent_index, sync_sent_items


def parse_date_to_year_month(date_str: str) -> tuple[str, str]:
    """Parse date string into ('YYYY-MM', 'YYYY-MM-DD'). Default to current year-month if invalid."""
    if not date_str:
        return "2026-01", "2026-01-01"

    # Match standard formats like "Thu, 15 Jan 2026 13:11:40 +0000" or "2026-01-15"
    months = {
        "jan": "01", "feb": "02", "mar": "03", "apr": "04", "may": "05", "jun": "06",
        "jul": "07", "aug": "08", "sep": "09", "oct": "10", "nov": "11", "dec": "12"
    }

    # ISO format check
    iso_match = re.search(r"(\d{4})-(\d{2})-(\d{2})", date_str)
    if iso_match:
        y, m, d = iso_match.group(1), iso_match.group(2), iso_match.group(3)
        return f"{y}-{m}", f"{y}-{m}-{d}"

    # RFC 2822 format check: "15 Jan 2026"
    rfc_match = re.search(r"(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})", date_str)
    if rfc_match:
        d = int(rfc_match.group(1))
        mon = rfc_match.group(2).lower()
        y = rfc_match.group(3)
        m = months.get(mon, "01")
        return f"{y}-{m}", f"{y}-{m}-{d:02d}"

    return "2026-01", "2026-01-01"


def load_catalogs(workspace_root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Load projects and topics from catalogs in workspace memory."""
    projects_file = workspace_root / "memory" / "references" / "projects" / "projects.json"
    topics_file = workspace_root / "memory" / "references" / "topics" / "topics.json"

    projects: list[dict[str, Any]] = []
    topics: list[dict[str, Any]] = []

    if projects_file.exists():
        try:
            with projects_file.open("r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    projects = data
                elif isinstance(data, dict):
                    projects = data.get("projects", [])
        except Exception:
            pass

    if topics_file.exists():
        try:
            with topics_file.open("r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    topics = data
                elif isinstance(data, dict):
                    topics = data.get("topics", [])
        except Exception:
            pass

    return projects, topics


def classify_email(
    email: dict[str, Any],
    workspace_root: Path | None = None,
    projects: list[dict[str, Any]] | None = None,
    topics: list[dict[str, Any]] | None = None,
    sent_lookup: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify a single email conservatively and determine recommended target folder and evidence."""
    ws = workspace_root or Path.cwd()
    if projects is None or topics is None:
        p, t = load_catalogs(ws)
        projects = projects if projects is not None else p
        topics = topics if topics is not None else t

    if sent_lookup is None:
        try:
            sent_lookup = load_sent_index(ws / "data" / "mail-desk")
        except Exception:
            sent_lookup = None

    subject = str(email.get("subject", "")).strip()
    from_str = str(email.get("from", "")).strip()
    preview = str(email.get("preview", "")).strip()
    date_str = str(email.get("date", "")).strip()
    envelope_id = str(email.get("envelope_id", "")).strip()
    raw_mid = email.get("message_id") or email.get("raw_message_id", "")
    norm_mid = normalize_message_id(raw_mid)

    ym, ymd = parse_date_to_year_month(date_str)
    full_text = f"{subject}\n{from_str}\n{preview}"
    full_text_lower = full_text.lower()

    # Default fallback
    target_folder = "INBOX"
    decision = {
        "kind": "unknown",
        "id": "unclassified",
        "confidence": "low",
        "needs_reply": False,
    }
    notes = ""
    evidence_spec: dict[str, Any] | None = None

    # Check for reply requirement directed at Martin
    needs_reply = False
    reply_triggers = [
        "martin bitte", "bitte martin", "frage an martin", "hallo martin", "lieber martin",
        "martin kannst du", "martin ?", "martin, bitte", "@martin"
    ]
    for trigger in reply_triggers:
        if trigger in full_text_lower:
            needs_reply = True
            break

    # Automated / no-reply senders never require a manual reply
    if "no-reply" in from_str.lower() or "do_not_reply" in from_str.lower() or "quarantine" in from_str.lower() or "mailer-daemon" in from_str.lower():
        needs_reply = False

    # --------------------------------------------------------------------------
    # 1. Dynamic Project Catalog Matching (High / Medium confidence)
    # --------------------------------------------------------------------------
    matched_project = None
    matched_proj_confidence = "low"

    for proj in (projects or []):
        p_id = proj.get("id", "").strip()
        kuerzel = proj.get("kuerzel", "").strip()
        aliases = [str(a).strip() for a in proj.get("aliases", []) if str(a).strip()]
        keywords = [str(k).strip() for k in proj.get("keywords", []) if str(k).strip()]
        typical_patterns = [str(p).strip() for p in proj.get("typical_subject_patterns", []) if str(p).strip()]
        domains = [str(d).strip().lower() for d in proj.get("domains", []) if str(d).strip()]
        contacts = [
            str(c.get("email", "")).strip().lower()
            for c in proj.get("contacts", [])
            if isinstance(c, dict) and str(c.get("email", "")).strip()
        ]
        mb_folder = proj.get("mailbox_folder") or f"Projekte/{kuerzel or p_id.upper()}"

        # 1a. Match explicit ID, Kürzel, or Alias in Subject (High confidence)
        names = [n for n in [kuerzel, p_id] + aliases if n and len(n) >= 3]
        subj_norm = re.sub(r"[-_]+", " ", subject)
        for name in names:
            name_norm = re.sub(r"[-_]+", " ", name)
            if re.search(r"\b" + re.escape(name) + r"\b", subject, re.IGNORECASE) or re.search(r"\b" + re.escape(name_norm) + r"\b", subj_norm, re.IGNORECASE):
                matched_project = {"id": p_id, "folder": mb_folder, "name": kuerzel or p_id}
                matched_proj_confidence = "high"
                break
        if matched_project:
            break

        # 1b. Typical Subject Patterns in Subject
        for pat in typical_patterns:
            pat_norm = re.sub(r"[-_]+", " ", pat)
            if (pat and pat.lower() in subject.lower()) or (pat_norm and re.search(r"\b" + re.escape(pat_norm) + r"\b", subj_norm, re.IGNORECASE)):
                matched_project = {"id": p_id, "folder": mb_folder, "name": kuerzel or p_id}
                matched_proj_confidence = "high"
                break
        if matched_project:
            break

        # 1c. Name in body with matching domain/contact or project keyword
        has_name_in_body = any(re.search(r"\b" + re.escape(n) + r"\b", full_text, re.IGNORECASE) for n in names)
        has_contact_match = any(c in from_str.lower() for c in contacts if c)
        has_domain_match = any(d in from_str.lower() for d in domains if d)
        has_kw_match = any(kw.lower() in full_text_lower for kw in keywords if len(kw) >= 4)

        if has_name_in_body and (has_contact_match or has_domain_match or has_kw_match):
            matched_project = {"id": p_id, "folder": mb_folder, "name": kuerzel or p_id}
            matched_proj_confidence = "high"
            break
        elif has_kw_match and (has_contact_match or has_domain_match):
            matched_project = {"id": p_id, "folder": mb_folder, "name": kuerzel or p_id}
            matched_proj_confidence = "medium"
            break

    if matched_project:
        target_folder = matched_project["folder"]
        pid = matched_project["id"]
        decision = {
            "kind": "project",
            "id": pid,
            "confidence": matched_proj_confidence,
            "needs_reply": needs_reply,
        }
        notes = f"Projektbezogene Abstimmung zu {matched_project['name'].upper()} (Betreff: {subject})."

        ev_dir = resolve_evidence_dir("projects", pid, workspace_root=ws)
        try:
            ev_dir_rel = str(ev_dir.relative_to(ws).as_posix())
        except ValueError:
            ev_dir_rel = str(ev_dir.as_posix())
        ev_file_rel = f"{ev_dir_rel}/{ym}.md"
        ev_entry = (
            f"- {ymd} — {subject}.\n"
            f"  - Message-ID: `{norm_mid}` ({from_str})\n"
            f"  - Aussagekern: {notes}\n"
            f"  - Einordnung: Dokumentation der laufenden Projektkommunikation zu {pid.upper()}."
        )
        evidence_spec = {
            "file": ev_file_rel,
            "entry": ev_entry,
        }

    # --------------------------------------------------------------------------
    # 2. Dynamic Topic Catalog Matching (High / Medium confidence)
    # --------------------------------------------------------------------------
    if not matched_project:
        matched_topic = None
        matched_topic_confidence = "low"
        subj_norm = re.sub(r"[-_]+", " ", subject)

        for top in (topics or []):
            t_id = top.get("id", "").strip()
            title = top.get("title", "").strip()
            aliases = [str(a).strip() for a in top.get("aliases", []) if str(a).strip()]
            keywords = [str(k).strip() for k in top.get("keywords", []) if str(k).strip()]
            typical_patterns = [str(p).strip() for p in top.get("typical_subject_patterns", []) if str(p).strip()]
            domains = [str(d).strip().lower() for d in top.get("domains", []) if str(d).strip()]
            contacts = [
                str(c.get("email", "")).strip().lower()
                for c in top.get("contacts", [])
                if isinstance(c, dict) and str(c.get("email", "")).strip()
            ]
            mb_folder = top.get("mailbox_folder") or f"Themen/{title or t_id}"

            # Check subtopics keywords & aliases as well
            for sub in top.get("subtopics", []):
                if isinstance(sub, dict):
                    aliases.extend([str(a).strip() for a in sub.get("aliases", []) if str(a).strip()])
                    keywords.extend([str(k).strip() for k in sub.get("keywords", []) if str(k).strip()])

            # 2a. Match Title, ID, or Alias in Subject
            t_names = [n for n in [title, t_id] + aliases if n and len(n) >= 3]
            for name in t_names:
                name_norm = re.sub(r"[-_]+", " ", name)
                if re.search(r"\b" + re.escape(name) + r"\b", subject, re.IGNORECASE) or re.search(r"\b" + re.escape(name_norm) + r"\b", subj_norm, re.IGNORECASE):
                    matched_topic = {"id": t_id, "folder": mb_folder, "title": title or t_id}
                    matched_topic_confidence = "high"
                    break
            if matched_topic:
                break

            # 2b. Typical Subject Patterns in Subject
            for pat in typical_patterns:
                pat_norm = re.sub(r"[-_]+", " ", pat)
                if (pat and pat.lower() in subject.lower()) or (pat_norm and re.search(r"\b" + re.escape(pat_norm) + r"\b", subj_norm, re.IGNORECASE)):
                    matched_topic = {"id": t_id, "folder": mb_folder, "title": title or t_id}
                    matched_topic_confidence = "high"
                    break
            if matched_topic:
                break

            # 2c. Keywords or domain/contact matching
            has_kw_subj = any(
                re.search(r"\b" + re.escape(kw) + r"\b", subject, re.IGNORECASE)
                or re.search(r"\b" + re.escape(re.sub(r"[-_]+", " ", kw)) + r"\b", subj_norm, re.IGNORECASE)
                for kw in keywords if len(kw) >= 3
            )
            has_kw_body = any(kw.lower() in full_text_lower for kw in keywords if len(kw) >= 4)
            has_contact = any(c in from_str.lower() for c in contacts if c)
            has_domain = any(d in from_str.lower() for d in domains if d)

            if has_kw_subj:
                matched_topic = {"id": t_id, "folder": mb_folder, "title": title or t_id}
                matched_topic_confidence = "high"
                break
            elif has_kw_body and (has_contact or has_domain):
                matched_topic = {"id": t_id, "folder": mb_folder, "title": title or t_id}
                matched_topic_confidence = "medium"
                break

        if matched_topic:
            target_folder = matched_topic["folder"]
            tid = matched_topic["id"]
            decision = {
                "kind": "topic",
                "id": tid,
                "confidence": matched_topic_confidence,
                "needs_reply": needs_reply,
            }
            notes = f"Themenbezogene Zuordnung zu {matched_topic['title']} (Betreff: {subject})."

    # --------------------------------------------------------------------------
    # 3. Sent Items Reply Check
    # --------------------------------------------------------------------------
    if sent_lookup and (needs_reply or decision.get("needs_reply")):
        reply_info = check_if_replied(email, sent_lookup)
        if reply_info:
            if reply_info.get("replied"):
                needs_reply = False
                decision["needs_reply"] = False
                decision["replied_via_sent"] = reply_info
                notes = f"{notes} (Bereits beantwortet via Sent Items: {reply_info.get('sent_subject', '')})"
            elif reply_info.get("has_candidate"):
                cand = reply_info.get("candidate", {})
                decision["reply_candidate"] = cand
                cand_subj = cand.get("sent_subject", "")
                cand_date = str(cand.get("sent_date", ""))[:10]
                cand_to = cand.get("matched_recipient", "")
                notes = f"{notes} [Antwort-Kandidat: '{cand_subj}' am {cand_date} an {cand_to}]"

    action = {
        "type": "copy_as_move" if target_folder != "INBOX" else "keep_in_folder",
        "target_folder": target_folder,
    }

    return {
        "envelope_id": envelope_id,
        "source_folder": email.get("folder", "INBOX"),
        "message_id": norm_mid,
        "raw_message_id": raw_mid,
        "subject": subject,
        "from": from_str,
        "to": email.get("to", ""),
        "date": date_str,
        "in_reply_to": email.get("in_reply_to", ""),
        "references": email.get("references", []),
        "action": action,
        "decision": decision,
        "notes": notes or f"Klassifikation: {subject}",
        "evidence": evidence_spec,
    }


def draft_manifest(
    emails: list[dict[str, Any]],
    workspace_root: Path | None = None,
    delete_input_on_success: bool = True,
    sent_lookup: dict[str, Any] | None = None,
    sync_sent: bool = True,
) -> dict[str, Any]:
    """Generate a batch manifest dictionary from a list of inspected emails."""
    ws = workspace_root or Path.cwd()
    projects, topics = load_catalogs(ws)
    dd = ws / "data" / "mail-desk"

    if sent_lookup is None:
        if sync_sent and emails:
            try:
                batch_dates = [parse_date_to_year_month(str(e.get("date", "")))[1] for e in emails if e.get("date")]
                if batch_dates:
                    sync_sent_items(dates=batch_dates, data_dir=dd, workspace_root=ws)
            except Exception:
                pass
        try:
            sent_lookup = load_sent_index(dd)
        except Exception:
            sent_lookup = None

    items: list[dict[str, Any]] = []
    for email in emails:
        item = classify_email(
            email,
            workspace_root=ws,
            projects=projects,
            topics=topics,
            sent_lookup=sent_lookup,
        )
        items.append(item)

    return {
        "mode": "execute",
        "delete_input_on_success": delete_input_on_success,
        "items": items,
    }
