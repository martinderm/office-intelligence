"""Canonical project, artifact and project-evidence matching for mail-desk (FR-13 / MD-M1-T02).

This module is the single canonical owner of the classifier's project-matching vertical
slice that previously lived inline in :mod:`core.classifier`:

* the conservative artifact primitives (:func:`_artifact_text_matches`,
  :func:`_artifact_code_matches`, :func:`_artifact_candidate`);
* v3 artifact resolution (:func:`_select_project_artifacts`) and the catalog-backed
  titles/context labels it feeds (:func:`_catalog_artifact_title`,
  :func:`_project_context_label`);
* the shared, bounded evidence-read escalation helper (:func:`_evidence_read_escalation`)
  that the topic evidence builders also reuse; and
* the neutral project evidence spec builder (:func:`_build_project_evidence`).

:func:`select_project_match` additionally extracts the existing inline root
project-catalog loop behind one owner callable.  Catalog order, first-match semantics,
confidence and the observable match shape are preserved: the caller passes the derived
subject/body inputs and receives either ``None`` or a
``{"id", "folder", "name", "confidence", "catalog"}`` match.

``core.classifier`` remains the compatibility facade and re-exports every callable here by
object identity.  This module never imports ``classifier.py`` (no import cycle) and performs
no I/O at import time.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..common import resolve_evidence_dir

__all__ = [
    "select_project_match",
    "_artifact_text_matches",
    "_artifact_code_matches",
    "_artifact_candidate",
    "_select_project_artifacts",
    "_catalog_artifact_title",
    "_project_context_label",
    "_evidence_read_escalation",
    "_build_project_evidence",
]

#: Short/ambiguous acronyms that only match a project name as an exact subject token.
_COMMON_WORD_ACRONYMS = {"WEEK", "LATEST", "USAGE", "PILOT", "START"}


def _artifact_text_matches(text: str, value: object) -> bool:
    """Return a conservative whole-term match for a descriptive artifact signal."""
    signal = str(value).strip()
    if len(signal) < 3:
        return False
    return bool(re.search(r"(?<!\w)" + re.escape(signal) + r"(?!\w)", text, re.IGNORECASE))


def _artifact_code_matches(text: str, identifier: object) -> bool:
    """Match a stable artifact code without accepting it as part of another token."""
    code = str(identifier).strip()
    if not code:
        return False
    return bool(re.search(r"(?<![A-Za-z0-9])" + re.escape(code) + r"(?![A-Za-z0-9])", text, re.IGNORECASE))


def _artifact_candidate(
    kind: str,
    item: dict[str, Any],
    text: str,
    *,
    workpackage: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return one catalog-backed candidate with deterministic, inspectable reasons."""
    identifier = str(item.get("id", "")).strip()
    title = str(item.get("title", "")).strip()
    if not identifier or not title:
        return None

    reasons: list[str] = []
    if _artifact_code_matches(text, identifier):
        reasons.append(f"exact_code:{identifier}")

    if kind == "workpackage":
        number = item.get("number")
        if isinstance(number, (int, float)) and not isinstance(number, bool):
            wp_code = f"WP{number:g}"
            if wp_code.casefold() != identifier.casefold() and _artifact_code_matches(text, wp_code):
                reasons.append(f"exact_code:{wp_code}")

    if _artifact_text_matches(text, title):
        reasons.append(f"title:{title}")
    for alias in item.get("aliases", []) if isinstance(item.get("aliases"), list) else []:
        if _artifact_text_matches(text, alias):
            reasons.append(f"alias:{str(alias).strip()}")
    for keyword in item.get("keywords", []) if isinstance(item.get("keywords"), list) else []:
        if _artifact_text_matches(text, keyword):
            reasons.append(f"keyword:{str(keyword).strip()}")

    if not reasons:
        return None

    candidate: dict[str, Any] = {"id": identifier, "title": title, "reasons": reasons}
    if workpackage is not None:
        parent_id = str(workpackage.get("id", "")).strip()
        if parent_id:
            candidate["workpackage"] = parent_id
    return candidate


def _select_project_artifacts(project: dict[str, Any], text: str) -> dict[str, Any]:
    """Resolve v3 artifacts only when the current mail makes one candidate unambiguous.

    Exact codes outrank descriptive matches.  A descriptive match is selected only
    when it yields one candidate for its artifact type; otherwise candidates remain
    visible for review and no scalar decision field is emitted.
    """
    candidates: dict[str, list[dict[str, Any]]] = {
        "workpackage": [], "task": [], "deliverable": [], "milestone": []
    }
    workpackages = project.get("workpackages")
    if isinstance(workpackages, list):
        for wp in workpackages:
            if not isinstance(wp, dict):
                continue
            candidate = _artifact_candidate("workpackage", wp, text)
            if candidate:
                candidates["workpackage"].append(candidate)
            for task in wp.get("tasks", []) if isinstance(wp.get("tasks"), list) else []:
                if isinstance(task, dict):
                    candidate = _artifact_candidate("task", task, text, workpackage=wp)
                    if candidate:
                        candidates["task"].append(candidate)
            for deliverable in wp.get("deliverables", []) if isinstance(wp.get("deliverables"), list) else []:
                if isinstance(deliverable, dict):
                    candidate = _artifact_candidate("deliverable", deliverable, text, workpackage=wp)
                    if candidate:
                        candidates["deliverable"].append(candidate)

    milestones = project.get("milestones")
    if isinstance(milestones, list):
        for milestone in milestones:
            if isinstance(milestone, dict):
                candidate = _artifact_candidate("milestone", milestone, text)
                if candidate:
                    candidates["milestone"].append(candidate)

    result: dict[str, Any] = {"match_reasons": {}, "candidates": {}}
    selected_parent_wps: list[str] = []
    for kind in ("workpackage", "task", "deliverable", "milestone"):
        matches = sorted(candidates[kind], key=lambda row: (row["id"].casefold(), row["title"].casefold()))
        exact = [row for row in matches if any(reason.startswith("exact_code:") for reason in row["reasons"])]
        choices = exact if exact else matches
        if len(choices) == 1:
            selected = choices[0]
            result[kind] = selected["id"]
            result["match_reasons"][kind] = selected["reasons"]
            parent_id = selected.get("workpackage")
            if isinstance(parent_id, str):
                selected_parent_wps.append(parent_id)
        elif choices:
            result["candidates"][kind] = choices

    if "workpackage" not in result and selected_parent_wps:
        parent_ids = sorted(set(selected_parent_wps), key=str.casefold)
        if len(parent_ids) == 1:
            result["workpackage"] = parent_ids[0]
            result["match_reasons"]["workpackage"] = ["inferred_from_nested_artifact"]

    if not result["match_reasons"]:
        result.pop("match_reasons")
    if not result["candidates"]:
        result.pop("candidates")
    return result


def _catalog_artifact_title(
    project: dict[str, Any],
    kind: str,
    identifier: str,
) -> str:
    """Return a catalog title for one already-unambiguous decision scalar."""
    workpackages = project.get("workpackages", [])
    if kind == "milestone":
        for milestone in project.get("milestones", []) if isinstance(project.get("milestones"), list) else []:
            if isinstance(milestone, dict) and str(milestone.get("id", "")).casefold() == identifier.casefold():
                return str(milestone.get("title", "")).strip()
        return ""
    if not isinstance(workpackages, list):
        return ""
    for wp in workpackages:
        if not isinstance(wp, dict):
            continue
        if kind == "workpackage" and str(wp.get("id", "")).casefold() == identifier.casefold():
            return str(wp.get("title", "")).strip()
        collection = "tasks" if kind == "task" else "deliverables"
        for artifact in wp.get(collection, []) if isinstance(wp.get(collection), list) else []:
            if isinstance(artifact, dict) and str(artifact.get("id", "")).casefold() == identifier.casefold():
                return str(artifact.get("title", "")).strip()
    return ""


def _project_context_label(project: dict[str, Any], decision: dict[str, Any]) -> str:
    """Build evidence context exclusively from unambiguous catalog-backed scalars."""
    project_label = str(project.get("kuerzel") or project.get("id", "")).strip().upper()
    artifact_parts: list[str] = []
    for kind, display in (
        ("workpackage", ""),
        ("task", ""),
        ("deliverable", ""),
        ("milestone", ""),
    ):
        identifier = decision.get(kind)
        if not isinstance(identifier, str) or not identifier.strip():
            continue
        title = _catalog_artifact_title(project, kind, identifier)
        label = f"{display}{identifier.upper() if kind == 'workpackage' else identifier}"
        artifact_parts.append(f"{label} ({title})" if title else label)
    if project_label and artifact_parts:
        return f"{project_label} | " + " / ".join(artifact_parts)
    return project_label or " / ".join(artifact_parts)


def _evidence_read_escalation(decision: dict[str, Any]) -> dict[str, Any] | None:
    """Expose bounded machine metadata while never copying body or error text."""
    escalation = decision.get("read_escalation")
    if not isinstance(escalation, dict):
        return None
    result: dict[str, Any] = {}
    for key in ("level", "status"):
        if isinstance(escalation.get(key), str):
            result[key] = escalation[key]
    if isinstance(escalation.get("triggers"), list):
        result["triggers"] = [value for value in escalation["triggers"] if isinstance(value, str)]
    if isinstance(escalation.get("error"), dict) and isinstance(escalation["error"].get("type"), str):
        result["error_type"] = escalation["error"]["type"]
    return result or None


def _build_project_evidence(
    project: dict[str, Any],
    decision: dict[str, Any],
    *,
    workspace_root: Path,
    year_month: str,
    date: str,
    subject: str,
    message_id: str,
    from_str: str,
    to_str: str,
) -> dict[str, Any]:
    """Create a writer-compatible, neutral project evidence spec without mail body."""
    project_id = str(project.get("id", "")).strip()
    evidence_dir = resolve_evidence_dir("projects", project_id, workspace_root=workspace_root)
    try:
        evidence_dir_rel = str(evidence_dir.relative_to(workspace_root).as_posix())
    except ValueError:
        evidence_dir_rel = str(evidence_dir.as_posix())

    participants = from_str or "Unbekannt"
    if to_str:
        participants = f"{participants}; An: {to_str}"
    context = _project_context_label(project, decision)
    entry_lines = [
        f"- {date} — {subject}.",
        f"  - Message-ID: `{message_id}`",
        f"  - Beteiligte: {participants}",
        f"  - Kontext: [{context}]" if context else "  - Kontext: [Projekt]",
        f"  - Mailgegenstand: {subject}",
    ]
    spec: dict[str, Any] = {
        "file": f"{evidence_dir_rel}/{year_month}.md",
        "entry": "\n".join(entry_lines),
    }
    read_escalation = _evidence_read_escalation(decision)
    if read_escalation:
        spec["read_escalation"] = read_escalation
    return spec


def select_project_match(
    projects: list[dict[str, Any]],
    *,
    subject: str,
    full_text: str,
    full_text_lower: str,
    from_str: str,
    to_str: str,
    parties: str,
) -> dict[str, Any] | None:
    """Run the root project-catalog matching loop and return the first match.

    Extracted verbatim from the inline ``classify_email`` loop so the facade keeps the
    same catalog order, first-match semantics and high/medium confidence policy.  Returns
    ``{"id", "folder", "name", "confidence", "catalog"}`` for the winning catalog entry,
    where ``catalog`` is the matched project object, or ``None`` when no entry matches.
    """
    for proj in projects or []:
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

        # Check do_not_route_if conditions from catalog
        do_not_route = False
        for dnr in proj.get("do_not_route_if", []) if isinstance(proj.get("do_not_route_if"), list) else []:
            dnr_clean = str(dnr).strip().lower()
            if not dnr_clean:
                continue
            if dnr_clean == "newsletter" and ("newsletter" in full_text_lower or "verteilerliste" in full_text_lower or "mailing list" in full_text_lower or "list." in from_str.lower() or "list." in to_str.lower()):
                do_not_route = True
                break
            if dnr_clean == "mailing list" and ("verteilerliste" in full_text_lower or "mailing list" in full_text_lower or "list." in from_str.lower() or "list." in to_str.lower()):
                do_not_route = True
                break
            if dnr_clean == "no-reply" and ("no-reply" in from_str.lower() or "do_not_reply" in from_str.lower() or "quarantine" in from_str.lower() or "mailer-daemon" in from_str.lower()):
                do_not_route = True
                break
            if dnr_clean in ("automatic reply", "out of office") and any(ph in full_text_lower for ph in ("abwesenheitsnotiz", "out of office", "automatische antwort")):
                do_not_route = True
                break
        if do_not_route:
            continue

        matched_project: dict[str, Any] | None = None
        matched_proj_confidence = "low"

        # 1a. Match explicit ID, Kürzel, or Alias in Subject (High confidence)
        names = [n for n in [kuerzel, p_id] + aliases if n and len(n) >= 3]
        subj_norm = re.sub(r"[-_]+", " ", subject)
        for name in names:
            name_norm = re.sub(r"[-_]+", " ", name)
            is_short_acronym = len(name) <= 4 or name.upper() in _COMMON_WORD_ACRONYMS or (name.isupper() and len(name) <= 6)
            matched_name = False
            if is_short_acronym:
                name_upper = name.upper()
                if re.search(r"\b" + re.escape(name_upper) + r"\b", subject) or re.search(r"\b" + re.escape(name_norm.upper()) + r"\b", subj_norm):
                    matched_name = True
                elif re.search(r"(?:\[|\(|projekt\s+|project\s+)" + re.escape(name) + r"(?:\]|\)|\b)", subject, re.IGNORECASE):
                    matched_name = True
            else:
                if re.search(r"\b" + re.escape(name) + r"\b", subject, re.IGNORECASE) or re.search(r"\b" + re.escape(name_norm) + r"\b", subj_norm, re.IGNORECASE):
                    matched_name = True

            if matched_name:
                matched_project = {"id": p_id, "folder": mb_folder, "name": kuerzel or p_id}
                matched_proj_confidence = "high"
                break
        if matched_project:
            return {**matched_project, "confidence": matched_proj_confidence, "catalog": proj}

        # 1b. Typical Subject Patterns in Subject
        for pat in typical_patterns:
            pat_norm = re.sub(r"[-_]+", " ", pat)
            is_short_pat = len(pat) <= 4 or pat.upper() in _COMMON_WORD_ACRONYMS or (pat.isupper() and len(pat) <= 6)
            matched_pat = False
            if is_short_pat:
                pat_upper = pat.upper()
                if re.search(r"\b" + re.escape(pat_upper) + r"\b", subject) or re.search(r"\b" + re.escape(pat_norm.upper()) + r"\b", subj_norm):
                    matched_pat = True
                elif re.search(r"(?:\[|\(|projekt\s+|project\s+)" + re.escape(pat) + r"(?:\]|\)|\b)", subject, re.IGNORECASE):
                    matched_pat = True
            else:
                if (pat and pat.lower() in subject.lower()) or (pat_norm and re.search(r"\b" + re.escape(pat_norm) + r"\b", subj_norm, re.IGNORECASE)):
                    matched_pat = True

            if matched_pat:
                matched_project = {"id": p_id, "folder": mb_folder, "name": kuerzel or p_id}
                matched_proj_confidence = "high"
                break
        if matched_project:
            return {**matched_project, "confidence": matched_proj_confidence, "catalog": proj}

        # 1c. Name in body with matching domain/contact or project keyword
        external_contacts = [c for c in contacts if c and not c.endswith("@boku.ac.at")]
        external_domains = [d for d in domains if d and d != "boku.ac.at"]
        has_name_in_body = any(
            (re.search(r"\b" + re.escape(n.upper()) + r"\b", full_text) if (len(n) <= 4 or n.upper() in _COMMON_WORD_ACRONYMS or (n.isupper() and len(n) <= 6))
             else re.search(r"\b" + re.escape(n) + r"\b", full_text, re.IGNORECASE))
            for n in names
        )
        has_contact_match = any(c in parties for c in external_contacts if c)
        has_domain_match = any(d in parties for d in external_domains if d)
        has_kw_match = any(kw.lower() in full_text_lower for kw in keywords if len(kw) >= 4)

        if has_name_in_body and (has_contact_match or has_domain_match or has_kw_match):
            matched_project = {"id": p_id, "folder": mb_folder, "name": kuerzel or p_id}
            matched_proj_confidence = "high"
        elif has_contact_match or (has_domain_match and not any(gen in parties for gen in ["boku.ac.at", "gmail.com", "outlook.com", "yahoo.com"])):
            matched_project = {"id": p_id, "folder": mb_folder, "name": kuerzel or p_id}
            matched_proj_confidence = "high"
        elif has_kw_match and (has_contact_match or has_domain_match):
            matched_project = {"id": p_id, "folder": mb_folder, "name": kuerzel or p_id}
            matched_proj_confidence = "medium"

        if matched_project:
            return {**matched_project, "confidence": matched_proj_confidence, "catalog": proj}

    return None
