"""Canonical project, artifact and project-evidence matching for mail-desk (FR-13 / MD-M1-T02).

This module is the single canonical owner of the classifier's project-matching vertical
slice that previously lived inline in :mod:`core.classifier`:

* the conservative artifact primitives (:func:`_artifact_text_matches`,
  :func:`_artifact_code_matches`, :func:`_artifact_candidate`);
* v3 artifact resolution (:func:`_select_project_artifacts`) and the catalog-backed
  titles/context labels it feeds (:func:`_catalog_artifact_title`,
  :func:`_project_context_label`);
* the shared, bounded evidence-read escalation helper (:func:`_evidence_read_escalation`)
  that the topic evidence builders also reuse;
* the shared subject/header-scoped ``do_not_route_if`` predicate
  (:func:`evaluate_do_not_route_signal`) that the topic owner also imports; and
* the neutral project evidence spec builder (:func:`_build_project_evidence`).

:func:`select_project_match` additionally owns the root project-catalog loop behind one
owner callable.  It selects by ``(signal strength, routing priority, catalog order)``: an
entry that matches on an exact subject ID/kuerzel/alias outranks one that matches only a
typical subject pattern, which outranks a pure body/contact/domain match.  Numerically
higher ``routing_priority`` orders equally strong candidates; missing or invalid values
(including booleans and strings) are treated as neutral-lowest, and stable catalog order
breaks the remaining ties.  The caller passes the derived subject/body inputs and receives
either ``None`` or a ``{"id", "folder", "name", "confidence", "catalog"}`` match.  Entries
excluded by their ``do_not_route_if`` declaration never match; when a collector list is
supplied, each matched exclusion is surfaced as a catalog-only
``{"kind", "id", "suppression_reason"}`` row.

``core.classifier`` remains the compatibility facade and re-exports every callable here by
object identity.  This module never imports ``classifier.py`` (no import cycle) and performs
no I/O at import time.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

from ..common import ensure_sentence_end, normalize_message_id, resolve_evidence_dir
from .date_parser import parse_date_to_year_month
from .reply_heuristics import DEFAULT_INTERNAL_DOMAINS

__all__ = [
    "select_project_match",
    "GENERIC_FREEMAIL_DOMAINS",
    "evaluate_do_not_route_signal",
    "evaluate_thread_sibling_do_not_route",
    "match_thread_project_inheritance",
    "resolve_full_read_project_evidence",
    "_artifact_text_matches",
    "_artifact_code_matches",
    "_artifact_candidate",
    "_select_project_artifacts",
    "_catalog_artifact_title",
    "_project_context_label",
    "_evidence_read_escalation",
    "_build_project_evidence",
    "_header_scope_text",
]

#: Generic freemail domains the root matchers never accept as a domain gate on
#: their own, independent of the configurable internal-domain boundary (FR-22/MD-ID3).
#: This mirrors the HEAD-observable project/topic guard exactly (``outlook.com`` in,
#: ``hotmail.com`` out; the latter belongs to the separate sent-indexer whitelist).
GENERIC_FREEMAIL_DOMAINS: tuple[str, ...] = ("gmail.com", "outlook.com", "yahoo.com")

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
        f"- {date} — {ensure_sentence_end(subject)}",
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


#: ``do_not_route_if`` predicates restricted to subject/header signals and their tokens.
#: The newsletter predicate never inspects body or preview text, so a body-only
#: occurrence of one of these words cannot suppress routing.
_HEADER_ONLY_DNR_TOKENS: dict[str, tuple[str, ...]] = {
    "newsletter": ("newsletter", "verteilerliste", "mailing list", "list."),
    "mailing list": ("verteilerliste", "mailing list", "list."),
    "automatic reply": ("abwesenheitsnotiz", "out of office", "automatische antwort"),
    "out of office": ("abwesenheitsnotiz", "out of office", "automatische antwort"),
}

#: Automated-sender tokens that the ``no-reply`` predicate recognises in the from header.
_NO_REPLY_SENDER_TOKENS = ("no-reply", "do_not_reply", "quarantine", "mailer-daemon")

#: ``do_not_route_if`` tokens that stay scoped to the from/to headers at HEAD.  They never
#: match the subject, so a subject-only ``list.`` mention (e.g. "distribution list.") cannot
#: suppress routing.
_DNR_HEADER_SCOPE_ONLY_TOKENS = frozenset({"list."})


def _header_scope_text(from_str: str, to_str: str, cc_str: str) -> str:
    """Fold the from/to/cc headers into the single text the DNR predicate scans."""
    return f"{from_str}\n{to_str}\n{cc_str}"


def _header_signal_scope(subject_lower: str, header_lower: str, tokens: tuple[str, ...]) -> str:
    """Return the first field scope carrying a header-scoped predicate token, else ``""``.

    Tokens in :data:`_DNR_HEADER_SCOPE_ONLY_TOKENS` are matched against the folded headers
    only (their HEAD semantics); every other token may also match the subject.
    """
    for token in tokens:
        if token in _DNR_HEADER_SCOPE_ONLY_TOKENS:
            if token in header_lower:
                return "header"
            continue
        if token in subject_lower:
            return "subject"
        if token in header_lower:
            return "header"
    return ""


def evaluate_do_not_route_signal(
    entry: Mapping[str, Any],
    *,
    subject: str,
    header_text: str,
    from_str: str,
) -> dict[str, Any] | None:
    """Return the first matching ``do_not_route_if`` exclusion for one catalog entry.

    Only subject and header signals are consulted: ``header_text`` folds the from/to/cc
    and list headers, so a predicate word that occurs only in the body or preview never
    suppresses routing.  ``no-reply`` stays from-only.  The result is
    ``{"reason": <predicate>, "scope": <field>}`` (``scope`` is ``"subject"``, ``"header"``
    or ``"from"``) or ``None`` when the entry declares no matching exclusion.
    """
    declarations = entry.get("do_not_route_if")
    if not isinstance(declarations, list):
        return None
    subject_lower = subject.casefold()
    header_lower = header_text.casefold()
    from_lower = from_str.casefold()
    for declaration in declarations:
        reason = str(declaration).strip().casefold()
        if not reason:
            continue
        if reason == "no-reply":
            if any(token in from_lower for token in _NO_REPLY_SENDER_TOKENS):
                return {"reason": "no-reply", "scope": "from"}
            continue
        tokens = _HEADER_ONLY_DNR_TOKENS.get(reason)
        if tokens is None:
            continue
        scope = _header_signal_scope(subject_lower, header_lower, tokens)
        if scope:
            return {"reason": reason, "scope": scope}
    return None


def _routing_priority(entry: Mapping[str, Any]) -> float:
    """Return the numeric ``routing_priority``, treating missing/invalid types as neutral.

    ``bool`` is an ``int`` subclass, so booleans (like strings and other non-numeric
    values) are treated as missing/neutral-lowest instead of being coerced into a rank.
    """
    value = entry.get("routing_priority")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return float("-inf")
    return float(value)


def select_project_match(
    projects: list[dict[str, Any]],
    *,
    subject: str,
    full_text: str,
    full_text_lower: str,
    from_str: str,
    to_str: str,
    parties: str,
    cc_str: str = "",
    suppressed_candidates: list[dict[str, Any]] | None = None,
    internal_domains: tuple[str, ...] | None = None,
) -> dict[str, Any] | None:
    """Run the root project-catalog matching loop and return the strongest match.

    Entries are evaluated by signal strength first (an exact subject ID/kuerzel/alias
    before a typical subject pattern before a body/contact/domain match), then by
    numerically higher ``routing_priority`` and finally by stable catalog order, so an
    exact subject code beats a pure contact match of another project.  Non-numeric
    ``routing_priority`` values are treated as missing/neutral-lowest.  Returns
    ``{"id", "folder", "name", "confidence", "catalog"}`` for the winning catalog entry,
    where ``catalog`` is the matched project object, or ``None`` when no entry matches.
    Entries excluded by their ``do_not_route_if`` declaration never match and are
    reported on ``suppressed_candidates`` as catalog-only
    ``{"kind", "id", "suppression_reason"}`` rows when a collector list is supplied.

    ``internal_domains`` is the catalog-configured internal-domain boundary (FR-22/MD-ID3):
    ``None`` resolves to :data:`~core.matching.reply_heuristics.DEFAULT_INTERNAL_DOMAINS`,
    which is byte-identical to the previous ``@boku.ac.at`` literal.  The generic freemail
    exclusion (:data:`GENERIC_FREEMAIL_DOMAINS`) stays invariant regardless of the parameter.
    """
    resolved_internal_domains = (
        DEFAULT_INTERNAL_DOMAINS if internal_domains is None else tuple(internal_domains)
    )
    best_key: tuple[float, float, int] | None = None
    best_match: dict[str, Any] | None = None
    header_text = _header_scope_text(from_str, to_str, cc_str)
    for catalog_index, proj in enumerate(projects or []):
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

        exclusion = evaluate_do_not_route_signal(
            proj, subject=subject, header_text=header_text, from_str=from_str
        )
        if exclusion is not None:
            if suppressed_candidates is not None:
                suppressed_candidates.append({
                    "kind": "project",
                    "id": p_id,
                    "suppression_reason": exclusion["reason"],
                })
            continue

        matched_project: dict[str, Any] | None = None
        matched_proj_confidence = "low"
        matched_strength = 0

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
                matched_strength = 3
                break

        # 1b. Typical Subject Patterns in Subject
        if matched_project is None:
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
                    matched_strength = 2
                    break

        # 1c. Name in body with matching domain/contact or project keyword
        if matched_project is None:
            external_contacts = [
                c
                for c in contacts
                if c and not any(c.endswith("@" + d) for d in resolved_internal_domains)
            ]
            external_domains = [
                d for d in domains if d and d not in resolved_internal_domains
            ]
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
                matched_strength = 1
            elif has_contact_match or (
                has_domain_match
                and not any(
                    gen in parties
                    for gen in (*resolved_internal_domains, *GENERIC_FREEMAIL_DOMAINS)
                )
            ):
                matched_project = {"id": p_id, "folder": mb_folder, "name": kuerzel or p_id}
                matched_proj_confidence = "high"
                matched_strength = 1
            elif has_kw_match and (has_contact_match or has_domain_match):
                matched_project = {"id": p_id, "folder": mb_folder, "name": kuerzel or p_id}
                matched_proj_confidence = "medium"
                matched_strength = 1

        if matched_project is None:
            continue
        candidate_key = (float(matched_strength), _routing_priority(proj), -catalog_index)
        if best_key is None or candidate_key > best_key:
            best_key = candidate_key
            best_match = {**matched_project, "confidence": matched_proj_confidence, "catalog": proj}

    return best_match


def evaluate_thread_sibling_do_not_route(
    projects: list[dict[str, Any]],
    parent_folder: str,
    *,
    subject: str,
    from_str: str,
    to_str: str,
    cc_str: str = "",
) -> dict[str, Any] | None:
    """Return the catalog-only suppression row for a do-not-route references sibling.

    Only an unambiguous parent folder -- exactly one catalog project whose
    ``mailbox_folder`` equals it -- is evaluated, so an ambiguous mapping can never
    suppress a sibling auto-route.  The current mail's subject and from/to/cc headers
    are matched through the shared :func:`evaluate_do_not_route_signal` predicate; body
    and preview text are never consulted.  ``None`` means the sibling is not gated, so
    the facade keeps the DNR-free parent-folder inheritance.  A match returns the
    bounded, catalog-data-only ``{"kind", "id", "suppression_reason"}`` row that the
    facade surfaces on ``suppressed_candidates``.
    """
    requested = parent_folder.lower()
    owners: list[dict[str, Any]] = []
    for project in projects or []:
        project_id = str(project.get("id", "")).strip()
        kuerzel = str(project.get("kuerzel", "")).strip()
        mailbox_folder = project.get("mailbox_folder") or f"Projekte/{kuerzel or project_id.upper()}"
        if str(mailbox_folder).lower() == requested:
            owners.append(project)
    if len(owners) != 1:
        return None
    exclusion = evaluate_do_not_route_signal(
        owners[0],
        subject=subject,
        header_text=_header_scope_text(from_str, to_str, cc_str),
        from_str=from_str,
    )
    if exclusion is None:
        return None
    return {
        "kind": "project",
        "id": str(owners[0].get("id", "")).strip(),
        "suppression_reason": exclusion["reason"],
    }


def match_thread_project_inheritance(
    projects: list[dict[str, Any]],
    parent_folder: str,
    *,
    workspace_root: Path,
    email: Mapping[str, Any],
    needs_reply: bool,
    parent_mid: str,
) -> dict[str, Any] | None:
    """Resolve thread inheritance to a project target from the recorded parent folder.

    The facade keeps the In-Reply-To/References parsing and the final-index parent lookup;
    this owner owns only the project part of the parent-folder mapping plus its decision,
    notes and monthly evidence spec.  ``None`` means the parent folder is not a project
    folder, so the facade falls through to the topic owner and then to the generic folder.
    Catalog order and first-match semantics are preserved.
    """
    matched_project: dict[str, Any] | None = None
    for project in projects or []:
        project_id = project.get("id", "").strip()
        kuerzel = project.get("kuerzel", "").strip()
        mailbox_folder = project.get("mailbox_folder") or f"Projekte/{kuerzel or project_id.upper()}"
        if mailbox_folder.lower() == parent_folder.lower():
            matched_project = project
            break
    if not matched_project:
        return None

    subject = str(email.get("subject", "")).strip()
    from_str = str(email.get("from", "")).strip()
    message_id = normalize_message_id(email.get("message_id") or email.get("raw_message_id", ""))
    year_month, date_value = parse_date_to_year_month(str(email.get("date", "")).strip())
    project_id = matched_project.get("id", "")
    project_name = matched_project.get("kuerzel") or project_id
    evidence_dir = resolve_evidence_dir("projects", project_id, workspace_root=workspace_root)
    try:
        evidence_dir_rel = str(evidence_dir.relative_to(workspace_root).as_posix())
    except ValueError:
        evidence_dir_rel = str(evidence_dir.as_posix())
    return {
        "project": matched_project,
        "target_folder": parent_folder,
        "decision": {
            "kind": "project",
            "id": project_id,
            "confidence": "high",
            "needs_reply": needs_reply,
        },
        "notes": (
            f"Thread-Vererbung via In-Reply-To ({parent_mid[:20]}...) zu "
            f"{project_name.upper()} ({parent_folder})."
        ),
        "evidence": {
            "type": "project_evidence",
            "file": f"{evidence_dir_rel}/{year_month}.md",
            "entry": (
                f"- {date_value} — {ensure_sentence_end(subject)}\n"
                f"  - Message-ID: `{message_id}`\n"
                f"  - Beteiligte: {from_str}\n"
            ),
        },
    }


def resolve_full_read_project_evidence(
    projects: list[dict[str, Any]],
    *,
    decision: dict[str, Any],
    workspace_root: Path,
    email: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Rebuild the project evidence spec after a full-body re-classification.

    The facade keeps the full-reader I/O and the two-pass orchestration; this owner
    resolves the winning catalog project by id and rebuilds only the project-domain
    evidence spec.  ``None`` (non-project decision or absent catalog entry) leaves any
    preview-derived evidence untouched.
    """
    if decision.get("kind") != "project":
        return None
    project_id = str(decision.get("id", "")).casefold()
    matched_project = next(
        (
            project for project in projects
            if isinstance(project, dict) and str(project.get("id", "")).casefold() == project_id
        ),
        None,
    )
    if matched_project is None:
        return None
    year_month, date_value = parse_date_to_year_month(str(email.get("date", "")))
    return _build_project_evidence(
        matched_project,
        decision,
        workspace_root=workspace_root,
        year_month=year_month,
        date=date_value,
        subject=str(email.get("subject", "")),
        message_id=normalize_message_id(email.get("message_id") or email.get("raw_message_id", "")),
        from_str=str(email.get("from", "")),
        to_str=str(email.get("to", "")),
    )
