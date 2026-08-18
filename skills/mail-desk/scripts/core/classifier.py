"""Conservative classifier and manifest drafting for mail-desk."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .common import normalize_message_id


def parse_date_to_year_month(date_str: str) -> tuple[str, str]:
    """Parse email date header into (YYYY-MM, YYYY-MM-DD)."""
    # e.g., "Tue, 20 Jan 2026 10:49:09 +0100" or "2026-01-20"
    if not date_str:
        now = datetime.now()
        return now.strftime("%Y-%m"), now.strftime("%Y-%m-%d")

    # Try common formats
    months = {
        "jan": "01", "feb": "02", "mar": "03", "apr": "04", "may": "05", "jun": "06",
        "jul": "07", "aug": "08", "sep": "09", "oct": "10", "nov": "11", "dec": "12"
    }

    match = re.search(r"(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})", date_str)
    if match:
        day, mon, year = match.groups()
        mon_num = months.get(mon.lower(), "01")
        day_str = f"{int(day):02d}"
        return f"{year}-{mon_num}", f"{year}-{mon_num}-{day_str}"

    iso_match = re.search(r"(\d{4})-(\d{2})-(\d{2})", date_str)
    if iso_match:
        year, mon, day = iso_match.groups()
        return f"{year}-{mon}", f"{year}-{mon}-{day}"

    now = datetime.now()
    return now.strftime("%Y-%m"), now.strftime("%Y-%m-%d")


def load_catalogs(workspace_root: Path | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Load projects.json and topics.json from workspace references."""
    ws = workspace_root or Path.cwd()
    proj_path = ws / "memory" / "references" / "projects" / "projects.json"
    topic_path = ws / "memory" / "references" / "topics" / "topics.json"

    projects: list[dict[str, Any]] = []
    topics: list[dict[str, Any]] = []

    if proj_path.exists():
        try:
            with proj_path.open("r", encoding="utf-8") as f:
                projects = json.load(f)
        except Exception:
            projects = []

    if topic_path.exists():
        try:
            with topic_path.open("r", encoding="utf-8") as f:
                topics = json.load(f)
        except Exception:
            topics = []

    return projects, topics


def classify_email(
    email: dict[str, Any],
    workspace_root: Path | None = None,
    projects: list[dict[str, Any]] | None = None,
    topics: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Conservatively classify an email and propose routing, decision, notes, and evidence."""
    ws = workspace_root or Path.cwd()
    if projects is None or topics is None:
        p_cat, t_cat = load_catalogs(ws)
        projects = projects if projects is not None else p_cat
        topics = topics if topics is not None else t_cat

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

    # --------------------------------------------------------------------------
    # 1. Project Catalog Matching (High / Medium confidence)
    # --------------------------------------------------------------------------
    matched_project = None
    high_match = False

    # Check known project acronyms / explicit identifiers
    acronym_map = {
        "atael": {
            "folder": "Projekte/In Ausarbeitung/ATAEL",
            "id": "atael",
            "evidence_dir": "memory/references/projects/atael/evidence",
        },
        "usage-ng": {
            "folder": "Projekte/USAGE-NG",
            "id": "usage-ng",
            "evidence_dir": "memory/references/projects/usage-ng/evidence",
        },
        "usage ng": {
            "folder": "Projekte/USAGE-NG",
            "id": "usage-ng",
            "evidence_dir": "memory/references/projects/usage-ng/evidence",
        },
        "meshe": {
            "folder": "Projekte/MESHE",
            "id": "meshe",
            "evidence_dir": "memory/references/projects/meshe/evidence",
        },
        "evolve": {
            "folder": "Projekte/EVOLVE",
            "id": "evolve",
            "evidence_dir": "memory/references/projects/evolve/evidence",
        },
        "li4lam": {
            "folder": "Projekte/LI4LAM",
            "id": "li4lam",
            "evidence_dir": "memory/references/projects/li4lam/evidence",
        },
    }

    # Subject explicit check first
    for acr, meta in acronym_map.items():
        if re.search(r"\b" + re.escape(acr) + r"\b", subject, re.IGNORECASE):
            matched_project = meta
            high_match = True
            break

    # If not in subject, check dynamic projects catalog
    if not matched_project:
        for proj in projects:
            p_id = proj.get("id", "").lower()
            kuerzel = proj.get("kuerzel", "").lower()
            aliases = [a.lower() for a in proj.get("aliases", [])]
            all_names = [p_id, kuerzel] + aliases

            for name in all_names:
                if name and re.search(r"\b" + re.escape(name) + r"\b", subject, re.IGNORECASE):
                    mb_folder = proj.get("mailbox_folder", f"Projekte/{proj.get('kuerzel', p_id)}")
                    matched_project = {
                        "folder": mb_folder,
                        "id": p_id,
                        "evidence_dir": f"memory/references/projects/{p_id}/evidence",
                    }
                    high_match = True
                    break
            if matched_project:
                break

    # If still not matched, check preview / full text for clear acronyms with partner context
    if not matched_project:
        for acr, meta in acronym_map.items():
            if re.search(r"\b" + re.escape(acr) + r"\b", full_text, re.IGNORECASE):
                # Check supporting signals
                is_atael = acr == "atael" or "erasmus" in full_text_lower and ("africa" in full_text_lower or "partner" in full_text_lower)
                is_usage = "usage" in acr and ("wp4" in full_text_lower or "wp2" in full_text_lower or "doh" in full_text_lower or "mandler" in full_text_lower)
                is_meshe = "meshe" in acr or ("microcredentials" in full_text_lower and "eucen" in full_text_lower)

                if is_atael or is_usage or is_meshe:
                    matched_project = meta
                    high_match = True
                    break
                else:
                    matched_project = meta
                    high_match = False
                    break

    if matched_project:
        target_folder = matched_project["folder"]
        pid = matched_project["id"]
        confidence = "high" if high_match else "medium"
        decision = {
            "kind": "project",
            "id": pid,
            "confidence": confidence,
            "needs_reply": needs_reply,
        }
        notes = f"Projektbezogene Abstimmung zu {pid.upper()} (Betreff: {subject})."

        ev_dir = ws / matched_project["evidence_dir"]
        ev_file_rel = f"{matched_project['evidence_dir']}/{ym}.md"
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
    # 2. Topic & System Matching
    # --------------------------------------------------------------------------
    if not matched_project:
        # Check BOKU-Organisation / IT / Administration
        is_spam_quarantine = "quarantine-notification@boku.ac.at" in from_str.lower() or "spam quarantine notification" in subject.lower()
        is_account_manager = "do_not_reply@boku.ac.at" in from_str.lower() and "accountmanager" in subject.lower()
        is_boku_it = "boku-it" in from_str.lower() or "helpdesk.boku.ac.at" in from_str.lower() or "[ticket#" in subject.lower()
        is_podcast = "leadinglights@boku.ac.at" in from_str.lower() or "boku leading lights" in subject.lower()
        is_zoom = "eu.zoom.us" in from_str.lower() or "boku-lll zoom room" in subject.lower()
        is_hr_contract = "nt per" in subject.lower() or "dienstvertrag" in full_text_lower or "dienstverhältnis" in full_text_lower

        if is_spam_quarantine or is_account_manager or is_boku_it or is_podcast or is_zoom or is_hr_contract:
            target_folder = "Themen/BOKU-Organisation"
            decision = {
                "kind": "topic",
                "id": "boku-organisation",
                "confidence": "high",
                "needs_reply": False,
            }
            notes = f"BOKU-Organisation / IT-Meldung: {subject}."

        # Check AIxLLL / Publications
        elif "ejull" in full_text_lower or "ai tutor" in full_text_lower or "ai-tutor" in full_text_lower or "aucen" in full_text_lower:
            target_folder = "Themen/AIxLLL"
            decision = {
                "kind": "topic",
                "id": "aixlll",
                "confidence": "high" if ("ejull" in full_text_lower or "ai tutor" in full_text_lower) else "medium",
                "needs_reply": needs_reply,
            }
            notes = f"AIxLLL / Publikationsabstimmung (Betreff: {subject})."

        # Check EUCEN / SAMUELE
        elif "samuele" in full_text_lower or "eucen" in full_text_lower:
            target_folder = "Themen/Netzwerke/EUCEN"
            decision = {
                "kind": "topic",
                "id": "netzwerke/eucen",
                "confidence": "high" if "samuele" in full_text_lower else "medium",
                "needs_reply": needs_reply,
            }
            notes = f"EUCEN Netzwerkabstimmung (Betreff: {subject})."

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
) -> dict[str, Any]:
    """Generate a batch manifest dictionary from a list of inspected emails."""
    ws = workspace_root or Path.cwd()
    projects, topics = load_catalogs(ws)

    items: list[dict[str, Any]] = []
    for email in emails:
        item = classify_email(email, workspace_root=ws, projects=projects, topics=topics)
        items.append(item)

    return {
        "mode": "execute",
        "delete_input_on_success": delete_input_on_success,
        "items": items,
    }
