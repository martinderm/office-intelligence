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

Since issue #63 the owner also detects a contact that several catalog projects claim.  When
the subject carries no catalog project identity, the body/preview scope is the arbiter
through catalog-backed keywords, other contacts, domains and project kuerzel/id/aliases.  Exactly one
contact peer with a signal wins; no signal or a tie returns the explicit review result
``{"id": "unclassified", "folder": "INBOX", ..., "review_required": True,
"review_reason": AMBIGUOUS_CONTACT_REVIEW_REASON, "candidates": [...]}`` instead of a
guessed project.  A subject naming a project and a single-affiliation contact keep their
existing behavior.
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
    "AMBIGUOUS_CONTACT_REVIEW_REASON",
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

#: The bounded review reason emitted when a contact listed by several projects cannot be
#: resolved by a unique catalog-backed body/preview signal (issue #63).
AMBIGUOUS_CONTACT_REVIEW_REASON = "ambiguous_contact_generic_subject"

#: Short/ambiguous acronyms that only match a project name as an exact subject token.
_COMMON_WORD_ACRONYMS = {"WEEK", "LATEST", "USAGE", "PILOT", "START"}

#: Contact-name titles/prefixes/suffixes that never participate in a body name signal.
_CONTACT_NAME_TITLES = frozenset({
    "dr", "prof", "professor", "mr", "mrs", "ms", "mx", "sir", "dame",
    "ing", "mag", "dipl", "phd", "msc", "bsc", "mba", "drs", "univ",
    "emeritus",
})


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


def _project_subject_names(project: Mapping[str, Any]) -> list[str]:
    """Return the catalog-backed project names that may identify it in a subject."""
    identifier = str(project.get("id", "")).strip()
    kuerzel = str(project.get("kuerzel", "")).strip()
    aliases = project.get("aliases")
    alias_values = (
        [str(alias).strip() for alias in aliases if str(alias).strip()]
        if isinstance(aliases, list)
        else []
    )
    return [name for name in [kuerzel, identifier] + alias_values if name and len(name) >= 3]


def _is_short_acronym(value: str) -> bool:
    """Return True when a name/pattern needs the exact-case short-acronym guard."""
    return (
        len(value) <= 4
        or value.upper() in _COMMON_WORD_ACRONYMS
        or (value.isupper() and len(value) <= 6)
    )


def _subject_name_matches(name: str, subject: str, normalized_subject: str) -> bool:
    """Match one project name in a subject with the documented acronym guard."""
    name_norm = re.sub(r"[-_]+", " ", name)
    if _is_short_acronym(name):
        name_upper = name.upper()
        if (
            re.search(r"\b" + re.escape(name_upper) + r"\b", subject)
            or re.search(r"\b" + re.escape(name_norm.upper()) + r"\b", normalized_subject)
        ):
            return True
        return bool(
            re.search(
                r"(?:\[|\(|projekt\s+|project\s+)" + re.escape(name) + r"(?:\]|\)|\b)",
                subject,
                re.IGNORECASE,
            )
        )
    return bool(
        re.search(r"\b" + re.escape(name) + r"\b", subject, re.IGNORECASE)
        or re.search(
            r"\b" + re.escape(name_norm) + r"\b",
            normalized_subject,
            re.IGNORECASE,
        )
    )


def _subject_pattern_matches(pattern: str, subject: str, normalized_subject: str) -> bool:
    """Match one ``typical_subject_patterns`` value with the documented guard."""
    pattern_norm = re.sub(r"[-_]+", " ", pattern)
    if _is_short_acronym(pattern):
        pattern_upper = pattern.upper()
        if (
            re.search(r"\b" + re.escape(pattern_upper) + r"\b", subject)
            or re.search(r"\b" + re.escape(pattern_norm.upper()) + r"\b", normalized_subject)
        ):
            return True
        return bool(
            re.search(
                r"(?:\[|\(|projekt\s+|project\s+)" + re.escape(pattern) + r"(?:\]|\)|\b)",
                subject,
                re.IGNORECASE,
            )
        )
    return bool(
        (pattern and pattern.lower() in subject.lower())
        or (
            pattern_norm
            and re.search(
                r"\b" + re.escape(pattern_norm) + r"\b",
                normalized_subject,
                re.IGNORECASE,
            )
        )
    )


def _project_subject_strength(project: Mapping[str, Any], subject: str) -> int:
    """Return 3 for an exact subject name, 2 for a typical pattern, else 0."""
    normalized_subject = re.sub(r"[-_]+", " ", subject)
    if any(
        _subject_name_matches(name, subject, normalized_subject)
        for name in _project_subject_names(project)
    ):
        return 3
    patterns = project.get("typical_subject_patterns")
    if isinstance(patterns, list):
        for pattern in patterns:
            value = str(pattern).strip()
            if value and _subject_pattern_matches(value, subject, normalized_subject):
                return 2
    return 0


def _body_scope_text(
    full_text: str,
    subject: str,
    from_str: str,
    to_str: str,
    cc_str: str,
) -> str:
    """Return the body/preview portion of a classifier input.

    The facade builds ``full_text`` as subject plus from/to/cc plus preview (plus an
    optional encapsulated untrusted attachment context).  Removing the first header
    occurrence yields exactly the body/preview scope the arbitration heuristic needs,
    so a project keyword in the subject cannot become a body signal.
    """
    body = full_text
    for header in (subject, from_str, to_str, cc_str):
        if header:
            body = body.replace(header, "\n", 1)
    return re.sub(r"\n{2,}", "\n", body).strip()


def _body_name_matches(name: str, body_text: str) -> bool:
    """Match a project name in body text, mirroring the subject acronym guard."""
    name_norm = re.sub(r"[-_]+", " ", name)
    if _is_short_acronym(name):
        return bool(
            re.search(r"\b" + re.escape(name.upper()) + r"\b", body_text)
            or re.search(r"\b" + re.escape(name_norm.upper()) + r"\b", body_text)
        )
    return bool(
        re.search(r"\b" + re.escape(name) + r"\b", body_text, re.IGNORECASE)
        or re.search(
            r"\b" + re.escape(name_norm) + r"\b",
            body_text,
            re.IGNORECASE,
        )
    )


def _contact_name_signature(name: object) -> str:
    """Return a conservative multi-token contact-name signature, or ``""``.

    Single-token names and initials are deliberately ignored: a surname alone is too
    weak to arbitrate between two projects, while ``Dr. Susan Ouko`` becomes
    ``susan ouko`` and is only used when both tokens occur in the body.
    """
    tokens: list[str] = []
    for token in re.split(r"[^\w]+", str(name).casefold()):
        if len(token) >= 3 and token not in _CONTACT_NAME_TITLES:
            tokens.append(token)
    return " ".join(tokens) if len(tokens) >= 2 else ""


def _address_in_text(address: str, text: str) -> bool:
    """Match a full e-mail address without accepting it inside another address.

    ``ann@example.test`` must not match the local part of ``joann@example.test``.
    The boundaries exclude every character that can occur in a local part or domain.
    """
    if not address:
        return False
    return bool(
        re.search(
            r"(?<![\w.+-])" + re.escape(address) + r"(?![\w.-])",
            text,
            re.IGNORECASE,
        )
    )


def _mask_addresses(text: str, addresses: frozenset[str]) -> str:
    """Blank out ambiguous contact addresses before arbitration scans the text.

    A signature line containing the shared person's own address must not turn that
    address's domain or an address-embedded keyword into a project signal.
    """
    masked = text
    for address in sorted(addresses, key=len, reverse=True):
        if address:
            masked = re.sub(re.escape(address), " ", masked, flags=re.IGNORECASE)
    return masked


def _signature_tokens(signature: str) -> set[str]:
    """Return the normalized tokens of a non-discriminating contact signature."""
    return {token for token in signature.split() if token}


def _name_is_non_discriminating(name: str, signatures: frozenset[str]) -> bool:
    """Return True when a project name/keyword is only the shared person's name."""
    tokens = {
        token
        for token in re.split(r"[^\w]+", str(name).casefold())
        if token
    }
    if not tokens:
        return False
    return any(tokens.issubset(_signature_tokens(signature)) for signature in signatures)


def _contact_name_in_text(signature: str, body_text: str) -> bool:
    """Return True when every conservative name token occurs in the body."""
    return all(
        re.search(r"\b" + re.escape(token) + r"\b", body_text, re.IGNORECASE)
        for token in signature.split()
    )


def _external_contact_emails(
    project: Mapping[str, Any], internal_domains: tuple[str, ...]
) -> list[str]:
    """Return the project contacts the matcher treats as external."""
    return [
        email
        for email in _project_contact_emails(project)
        if not any(email.endswith("@" + domain) for domain in internal_domains)
    ]


def _project_contact_emails(project: Mapping[str, Any]) -> list[str]:
    """Return every normalized catalog contact address, internal ones included."""
    contacts = project.get("contacts")
    if not isinstance(contacts, list):
        return []
    emails: list[str] = []
    for contact in contacts:
        if not isinstance(contact, Mapping):
            continue
        email = str(contact.get("email", "")).strip().lower()
        if email:
            emails.append(email)
    return emails


def _non_discriminating_contact_names(
    entries: list[dict[str, Any]], ambiguous_contacts: frozenset[str]
) -> set[str]:
    """Return contact-name signatures that cannot identify one project side.

    A person listed in more than one project (or the ambiguous contact itself under
    another address) must never become a body signal for one of the peers.
    """
    ambiguous_keys: set[str] = set()
    counts: dict[str, int] = {}
    for entry in entries:
        contacts = entry["project"].get("contacts")
        if not isinstance(contacts, list):
            continue
        seen: set[str] = set()
        for contact in contacts:
            if not isinstance(contact, Mapping):
                continue
            key = _contact_name_signature(contact.get("name", ""))
            if not key:
                continue
            seen.add(key)
            if str(contact.get("email", "")).strip().lower() in ambiguous_contacts:
                ambiguous_keys.add(key)
        for key in seen:
            counts[key] = counts.get(key, 0) + 1
    return ambiguous_keys | {key for key, count in counts.items() if count >= 2}


def _contact_arbitration_signals(
    project: Mapping[str, Any],
    body_text: str,
    *,
    parties_lower: str,
    internal_domains: tuple[str, ...],
    ambiguous_contacts: frozenset[str],
    non_discriminating_names: frozenset[str],
) -> list[str]:
    """Return the catalog-backed arbitration signals for one project.

    Only declared catalog data is consulted: a uniquely affiliated external contact in
    the mail's parties, keywords, other project contacts, project domains and project
    kuerzel/id/aliases.  The ambiguous contact itself and any name shared by several
    projects are excluded, so the signal set can actually break the tie between the
    contact peers.
    """
    if not body_text.strip() and not parties_lower.strip():
        return []

    # The shared person's own signature address must not leak its domain or an
    # address-embedded keyword into the arbitration scans.
    arbitration_text = _mask_addresses(body_text, ambiguous_contacts)
    signals: list[str] = []

    # A contact that belongs to exactly this project (and no ambiguity peer) is a
    # decisive participant signal, even when the body stays neutral.
    for email in _external_contact_emails(project, internal_domains):
        if email not in ambiguous_contacts and _address_in_text(email, parties_lower):
            signals.append("unique_contact")

    keywords = project.get("keywords")
    if isinstance(keywords, list):
        for keyword in keywords:
            value = str(keyword).strip()
            if (
                value
                and not _name_is_non_discriminating(value, non_discriminating_names)
                and _artifact_text_matches(arbitration_text, value)
            ):
                signals.append(f"keyword:{value}")

    contacts = project.get("contacts")
    if isinstance(contacts, list):
        for contact in contacts:
            if not isinstance(contact, Mapping):
                continue
            email = str(contact.get("email", "")).strip().lower()
            name_key = _contact_name_signature(contact.get("name", ""))
            if (
                email
                and email not in ambiguous_contacts
                and _address_in_text(email, arbitration_text)
            ):
                signals.append("contact")
            elif (
                name_key
                and name_key not in non_discriminating_names
                and _contact_name_in_text(name_key, arbitration_text)
            ):
                signals.append("contact_name")

    domains = project.get("domains")
    if isinstance(domains, list):
        for domain in domains:
            value = str(domain).strip().lower()
            if (
                value
                and value not in GENERIC_FREEMAIL_DOMAINS
                and value not in internal_domains
                and re.search(
                    r"(?<![\w.-])" + re.escape(value) + r"(?![\w.-])",
                    arbitration_text,
                    re.IGNORECASE,
                )
            ):
                signals.append(f"domain:{value}")

    for name in _project_subject_names(project):
        if (
            not _name_is_non_discriminating(name, non_discriminating_names)
            and _body_name_matches(name, arbitration_text)
        ):
            signals.append("name")

    return list(dict.fromkeys(signals))


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
    body_text: str | None = None,
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

    Contact ambiguity (issue #63): when an external contact of the mail belongs to
    more than one non-suppressed catalog project and the subject carries no project
    identity, a pure contact match alone is not a routing decision.  The body/preview
    scope (``body_text``; otherwise derived from ``full_text`` by removing the
    subject/from/to/cc headers) is used as the arbiter through catalog-backed
    keywords, other project contacts, project domains and project kuerzel/id/aliases.
    Exactly one contact peer with a body signal wins; no signal or a tie returns the explicit
    review result ``{"id": "unclassified", "folder": "INBOX", "name": "",
    "confidence": "low", "catalog": None, "review_required": True,
    "review_reason": "ambiguous_contact_generic_subject", "candidates": [...]}``
    instead of a guessed project.  A subject that names a project bypasses this path,
    and a single-affiliation contact keeps its existing ``high`` behavior.

    ``internal_domains`` is the catalog-configured internal-domain boundary (FR-22/MD-ID3):
    ``None`` resolves to :data:`~core.matching.reply_heuristics.DEFAULT_INTERNAL_DOMAINS`,
    which is byte-identical to the previous ``@boku.ac.at`` literal.  The generic freemail
    exclusion (:data:`GENERIC_FREEMAIL_DOMAINS`) stays invariant regardless of the parameter.
    """
    resolved_internal_domains = (
        DEFAULT_INTERNAL_DOMAINS if internal_domains is None else tuple(internal_domains)
    )
    header_text = _header_scope_text(from_str, to_str, cc_str)
    parties_lower = str(parties or "").lower()

    # First pass: exclude catalog entries and collect the data both the root match and
    # the contact-ambiguity arbiter need.  Suppression rows keep their catalog order.
    entries: list[dict[str, Any]] = []
    for catalog_index, proj in enumerate(projects or []):
        exclusion = evaluate_do_not_route_signal(
            proj, subject=subject, header_text=header_text, from_str=from_str
        )
        if exclusion is not None:
            if suppressed_candidates is not None:
                suppressed_candidates.append({
                    "kind": "project",
                    "id": str(proj.get("id", "")).strip(),
                    "suppression_reason": exclusion["reason"],
                })
            continue
        p_id = str(proj.get("id", "")).strip()
        kuerzel = str(proj.get("kuerzel", "")).strip()
        keywords = proj.get("keywords")
        typical_patterns = proj.get("typical_subject_patterns")
        domains = proj.get("domains")
        entries.append({
            "catalog_index": catalog_index,
            "project": proj,
            "id": p_id,
            "kuerzel": kuerzel,
            "names": _project_subject_names(proj),
            "keywords": (
                [str(k).strip() for k in keywords if str(k).strip()]
                if isinstance(keywords, list)
                else []
            ),
            "patterns": (
                [str(p).strip() for p in typical_patterns if str(p).strip()]
                if isinstance(typical_patterns, list)
                else []
            ),
            "domains": (
                [str(d).strip().lower() for d in domains if str(d).strip()]
                if isinstance(domains, list)
                else []
            ),
            "external_contacts": _external_contact_emails(proj, resolved_internal_domains),
            "folder": proj.get("mailbox_folder") or f"Projekte/{kuerzel or p_id.upper()}",
        })

    # Second pass precondition: a contact that several projects claim, opposite a subject
    # without any catalog project identity.  A subject naming a project keeps the
    # existing strength-3/2 precedence, and a single-affiliation contact stays high.
    subject_identified = {
        entry["catalog_index"]
        for entry in entries
        if _project_subject_strength(entry["project"], subject) > 0
    }
    affiliations: dict[str, list[int]] = {}
    for entry in entries:
        for email in entry["external_contacts"]:
            affiliations.setdefault(email, []).append(entry["catalog_index"])
    ambiguous_contacts = frozenset(
        email for email, owners in affiliations.items() if len(set(owners)) > 1
    )
    matched_ambiguous = sorted(
        email for email in ambiguous_contacts if _address_in_text(email, parties_lower)
    )
    ambiguity_active = bool(matched_ambiguous) and not subject_identified
    ambiguity_candidates = sorted(
        {
            entry["kuerzel"] or entry["id"]
            for entry in entries
            if set(entry["external_contacts"]).intersection(matched_ambiguous)
        },
        key=str.casefold,
    )

    subj_norm = re.sub(r"[-_]+", " ", subject)
    best_key: tuple[float, float, int] | None = None
    best_match: dict[str, Any] | None = None
    matches: dict[int, dict[str, Any]] = {}

    for entry in entries:
        proj = entry["project"]
        catalog_index = entry["catalog_index"]
        p_id = entry["id"]
        kuerzel = entry["kuerzel"]
        mb_folder = entry["folder"]
        names = entry["names"]

        matched_project: dict[str, Any] | None = None
        matched_proj_confidence = "low"
        matched_strength = 0

        # 1a. Match explicit ID, Kürzel, or Alias in Subject (High confidence)
        for name in names:
            if _subject_name_matches(name, subject, subj_norm):
                matched_project = {"id": p_id, "folder": mb_folder, "name": kuerzel or p_id}
                matched_proj_confidence = "high"
                matched_strength = 3
                break

        # 1b. Typical Subject Patterns in Subject
        if matched_project is None:
            for pat in entry["patterns"]:
                if _subject_pattern_matches(pat, subject, subj_norm):
                    matched_project = {"id": p_id, "folder": mb_folder, "name": kuerzel or p_id}
                    matched_proj_confidence = "high"
                    matched_strength = 2
                    break

        # 1c. Name in body with matching domain/contact or project keyword
        if matched_project is None:
            external_contacts = entry["external_contacts"]
            external_domains = [
                d for d in entry["domains"] if d and d not in resolved_internal_domains
            ]
            has_name_in_body = any(
                _body_name_matches(name, full_text) for name in names
            )
            has_contact_match = any(
                _address_in_text(c, parties_lower) for c in external_contacts if c
            )
            has_domain_match = any(d in parties_lower for d in external_domains if d)
            has_kw_match = any(
                kw.lower() in full_text_lower for kw in entry["keywords"] if len(kw) >= 4
            )

            if has_name_in_body and (has_contact_match or has_domain_match or has_kw_match):
                matched_project = {"id": p_id, "folder": mb_folder, "name": kuerzel or p_id}
                matched_proj_confidence = "high"
                matched_strength = 1
            elif has_contact_match or (
                has_domain_match
                and not any(
                    gen in parties_lower
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
        match = {**matched_project, "confidence": matched_proj_confidence, "catalog": proj}
        matches[catalog_index] = match
        candidate_key = (
            float(matched_strength),
            _routing_priority(proj),
            -catalog_index,
        )
        if best_key is None or candidate_key > best_key:
            best_key = candidate_key
            best_match = match

    if ambiguity_active:
        body_scope = (
            str(body_text)
            if body_text is not None
            else _body_scope_text(full_text, subject, from_str, to_str, cc_str)
        )
        non_discriminating_names = frozenset(
            _non_discriminating_contact_names(entries, ambiguous_contacts)
        )
        signal_owners = [
            catalog_index
            for catalog_index, match in matches.items()
            if _contact_arbitration_signals(
                match["catalog"],
                body_scope,
                parties_lower=parties_lower,
                internal_domains=resolved_internal_domains,
                ambiguous_contacts=ambiguous_contacts,
                non_discriminating_names=non_discriminating_names,
            )
        ]
        if len(signal_owners) == 1:
            return matches[signal_owners[0]]
        return {
            "id": "unclassified",
            "folder": "INBOX",
            "name": "",
            "confidence": "low",
            "catalog": None,
            "review_required": True,
            "review_reason": AMBIGUOUS_CONTACT_REVIEW_REASON,
            "candidates": ambiguity_candidates,
        }

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
