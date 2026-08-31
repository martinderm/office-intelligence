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
                projects = data.get("projects", [])
        except Exception:
            pass

    if topics_file.exists():
        try:
            with topics_file.open("r", encoding="utf-8") as f:
                data = json.load(f)
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
        },
        "usage-ng": {
            "folder": "Projekte/USAGE-NG",
            "id": "usage-ng",
        },
        "usage ng": {
            "folder": "Projekte/USAGE-NG",
            "id": "usage-ng",
        },
        "meshe": {
            "folder": "Projekte/MESHE",
            "id": "meshe",
        },
        "evolve": {
            "folder": "Projekte/EVOLVE",
            "id": "evolve",
        },
        "li4lam": {
            "folder": "Projekte/LI4LAM",
            "id": "li4lam",
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
    # 2. Topic & System Matching
    # --------------------------------------------------------------------------
    if not matched_project:
        # Check BOKU-Organisation / IT / Administration / Gremien
        is_spam_quarantine = "quarantine-notification@boku.ac.at" in from_str.lower() or "spam quarantine notification" in subject.lower()
        is_account_manager = "do_not_reply@boku.ac.at" in from_str.lower() and "accountmanager" in subject.lower()
        is_boku_it = "boku-it" in from_str.lower() or "helpdesk.boku.ac.at" in from_str.lower() or "[ticket#" in subject.lower() or "znuny" in full_text_lower or "[otrs-agents]" in subject.lower() or "wartungsarbeiten" in full_text_lower
        is_podcast = "leadinglights@boku.ac.at" in from_str.lower() or "boku leading lights" in subject.lower()
        is_zoom = "eu.zoom.us" in from_str.lower() or "boku-lll zoom room" in subject.lower() or "zoom room" in subject.lower()
        is_hr_contract = "nt per" in subject.lower() or "dienstvertrag" in full_text_lower or "dienstverhältnis" in full_text_lower
        is_org_bulletin = "mitteilungsblatt" in full_text_lower or "[mitarbeiter-" in subject.lower() or "gesunde boku" in full_text_lower
        is_wb_ak = "wb ak" in full_text_lower or "weiterbildungsarbeitskreis" in full_text_lower or "lehrentwicklung" in full_text_lower or "klimaneutrale boku" in full_text_lower or "donau-uni.ac.at" in from_str.lower()

        if is_spam_quarantine or is_account_manager or is_boku_it or is_podcast or is_zoom or is_hr_contract or is_org_bulletin or is_wb_ak:
            target_folder = "Themen/BOKU-Organisation"
            decision = {
                "kind": "topic",
                "id": "boku-organisation",
                "confidence": "high",
                "needs_reply": False,
            }
            notes = f"BOKU-Organisation / IT-Meldung: {subject}."

        # Check EUCEN / EJULL / SAMUELE
        elif "ejull" in full_text_lower or "eucen" in full_text_lower or "samuele" in full_text_lower:
            target_folder = "Themen/Netzwerke/EUCEN"
            decision = {
                "kind": "topic",
                "id": "netzwerke/eucen",
                "confidence": "high" if ("ejull" in full_text_lower or "eucen" in full_text_lower) else "medium",
                "needs_reply": needs_reply,
            }
            notes = f"EUCEN / EJULL Netzwerk- & Publikationsabstimmung (Betreff: {subject})."

        # Check AUCEN
        elif "aucen" in full_text_lower or "office@aucen.ac.at" in from_str.lower():
            target_folder = "Themen/Netzwerke/AUCEN"
            decision = {
                "kind": "topic",
                "id": "netzwerke/aucen",
                "confidence": "high",
                "needs_reply": needs_reply,
            }
            notes = f"AUCEN Netzwerkabstimmung (Betreff: {subject})."

        # Check AIxLLL
        elif "ai tutor" in full_text_lower or "ai-tutor" in full_text_lower or "aixlll" in full_text_lower:
            target_folder = "Themen/AIxLLL"
            decision = {
                "kind": "topic",
                "id": "aixlll",
                "confidence": "high",
                "needs_reply": needs_reply,
            }
            notes = f"AIxLLL Abstimmung (Betreff: {subject})."

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
