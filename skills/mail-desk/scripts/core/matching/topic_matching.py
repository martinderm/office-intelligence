"""Canonical topic, subtopic, operation, event and evidence matching (FR-13 / MD-M1-T03).

This module is the single canonical owner of the classifier's topic-matching vertical
slice that previously lived inline in :mod:`core.classifier`:

* the topic/subtopic subject signals (:func:`_topic_parent_subject_signal`,
  :func:`_subject_signal_matches`);
* subtopic resolution (:func:`_select_topic_subtopic`) and its context label
  (:func:`_topic_context_label`);
* operation resolution (:func:`_select_subtopic_operation`) and its context label
  (:func:`_operation_context_label`);
* event resolution (:func:`_select_subtopic_event`), event validation
  (:func:`_event_validation_reasons`) and its context label (:func:`_event_context_label`);
* the neutral topic/operation/event evidence builders (:func:`_build_topic_evidence`,
  :func:`_build_operation_evidence`, :func:`_build_event_evidence`); and
* the safe synthesis-target guards (:func:`_safe_subtopic_reference_target`,
  :func:`_safe_operation_reference_target`, :func:`_safe_event_dossier_target`,
  :func:`_has_canonical_operation_reference`).

Two concrete owner callables extract the existing inline topic orchestration:

* :func:`select_topic_match` owns the ordered root topic-catalog loop together with the
  unique-subtopic fallback/override policy.  It returns the exact winning catalog object
  under ``catalog`` so the facade can hand it to :func:`materialize_topic_details` without a
  second inline topic-catalog lookup, plus the resolved ``preselected_subtopic`` (or ``None``).
* :func:`materialize_topic_details` owns selected-topic subtopic/operation/event enrichment,
  domain evidence, event validation, cross-kind conflict handling and the safe synthesis
  targets.

``core.classifier`` remains the compatibility facade and re-exports every callable here by
object identity; the facade keeps catalog I/O, full-body escalation, thread inheritance,
sent/final-index context, attachment binding and manifest drafting.  This module never
imports ``classifier.py`` (no import cycle) and performs no I/O at import time.  It reuses the
project-owned :func:`_evidence_read_escalation` and :func:`_artifact_text_matches` helpers and
the shared ambiguity policy below the facade rather than duplicating them.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any

from ..common import resolve_evidence_dir
from . import ambiguity
from .project_matching import _artifact_text_matches, _evidence_read_escalation

__all__ = [
    "select_topic_match",
    "materialize_topic_details",
    "_topic_parent_subject_signal",
    "_subject_signal_matches",
    "_select_topic_subtopic",
    "_topic_context_label",
    "_select_subtopic_operation",
    "_operation_context_label",
    "_select_subtopic_event",
    "_event_validation_reasons",
    "_event_context_label",
    "_build_topic_evidence",
    "_build_operation_evidence",
    "_build_event_evidence",
    "_safe_subtopic_reference_target",
    "_safe_operation_reference_target",
    "_safe_event_dossier_target",
    "_has_canonical_operation_reference",
]


def _topic_parent_subject_signal(topic: dict[str, Any], subject: str) -> bool:
    """Return whether the current subject independently identifies the parent topic.

    A subtopic contact is deliberately not enough on its own: it may only refine a
    topic whose parent has an explicit title, ID, alias, or subject-pattern signal.
    """
    subject_normalized = re.sub(r"[-_]+", " ", subject)
    values = [topic.get("title", ""), topic.get("id", "")]
    values.extend(topic.get("aliases", []) if isinstance(topic.get("aliases"), list) else [])
    values.extend(
        topic.get("typical_subject_patterns", [])
        if isinstance(topic.get("typical_subject_patterns"), list)
        else []
    )
    for value in values:
        signal = str(value).strip()
        if len(signal) < 3:
            continue
        normalized = re.sub(r"[-_]+", " ", signal)
        if re.search(r"(?<!\w)" + re.escape(normalized) + r"(?!\w)", subject_normalized, re.IGNORECASE):
            return True
    return False


def _subject_signal_matches(subject: str, value: object) -> bool:
    """Match one documented subject signal without accepting fragments of words."""
    signal = str(value).strip()
    if len(signal) < 3:
        return False
    normalized_subject = re.sub(r"[-_]+", " ", subject)
    normalized_signal = re.sub(r"[-_]+", " ", signal)
    return bool(
        re.search(r"(?<!\w)" + re.escape(signal) + r"(?!\w)", subject, re.IGNORECASE)
        or re.search(r"(?<!\w)" + re.escape(normalized_signal) + r"(?!\w)", normalized_subject, re.IGNORECASE)
    )


def _select_topic_subtopic(
    topic: dict[str, Any],
    *,
    subject: str,
    full_text: str,
    from_str: str,
) -> dict[str, Any]:
    """Resolve only an unambiguous active subtopic after parent-topic selection.

    Subject patterns and exact subject terms outrank preview keywords. A contact may
    contribute only where the parent is independently explicit in the subject and
    the contact belongs to exactly one active subtopic.
    """
    active_subtopics = [
        sub for sub in topic.get("subtopics", [])
        if isinstance(sub, dict) and str(sub.get("status", "")).casefold() in {"", "active"}
    ] if isinstance(topic.get("subtopics"), list) else []
    parent_subject_signaled = _topic_parent_subject_signal(topic, subject)
    sender = from_str.casefold()
    contact_counts: dict[str, int] = {}
    for subtopic in active_subtopics:
        for contact in subtopic.get("contacts", []) if isinstance(subtopic.get("contacts"), list) else []:
            if isinstance(contact, dict):
                email = str(contact.get("email", "")).strip().casefold()
                if email:
                    contact_counts[email] = contact_counts.get(email, 0) + 1

    candidates: list[tuple[int, dict[str, Any]]] = []
    for subtopic in active_subtopics:
        identifier = str(subtopic.get("id", "")).strip()
        title = str(subtopic.get("title", "")).strip()
        if not identifier or not title:
            continue
        reasons: list[str] = []
        score = 0
        for pattern in subtopic.get("typical_subject_patterns", []) if isinstance(subtopic.get("typical_subject_patterns"), list) else []:
            if _subject_signal_matches(subject, pattern):
                reasons.append(f"subject_pattern:{str(pattern).strip()}")
                score = max(score, 500)
        for value, label in ((identifier, "subject_id"), (title, "subject_title")):
            if _subject_signal_matches(subject, value):
                reasons.append(f"{label}:{value}")
                score = max(score, 400)
        for alias in subtopic.get("aliases", []) if isinstance(subtopic.get("aliases"), list) else []:
            if _subject_signal_matches(subject, alias):
                reasons.append(f"subject_alias:{str(alias).strip()}")
                score = max(score, 400)
        for keyword in subtopic.get("keywords", []) if isinstance(subtopic.get("keywords"), list) else []:
            keyword_text = str(keyword).strip()
            if _subject_signal_matches(subject, keyword_text):
                reasons.append(f"subject_keyword:{keyword_text}")
                score = max(score, 300)
            elif _artifact_text_matches(full_text, keyword_text):
                reasons.append(f"keyword:{keyword_text}")
                score = max(score, 100)
        if parent_subject_signaled:
            for contact in subtopic.get("contacts", []) if isinstance(subtopic.get("contacts"), list) else []:
                if not isinstance(contact, dict):
                    continue
                email = str(contact.get("email", "")).strip().casefold()
                if email and contact_counts.get(email) == 1 and email in sender:
                    reasons.append(f"unique_contact:{email}")
                    score = max(score, 200)
        if reasons:
            candidates.append((score, {"id": identifier, "title": title, "reasons": reasons, "_source": subtopic}))

    resolved = ambiguity.resolve_scored_candidates(candidates)
    if not resolved:
        return {}
    if "unique" in resolved:
        selected = resolved["unique"]
        return {
            "subtopic": selected["id"],
            "match_reasons": selected["reasons"],
            "source": selected["_source"],
        }
    return {
        "candidates": [
            {"id": candidate["id"], "title": candidate["title"], "reasons": candidate["reasons"]}
            for candidate in resolved["candidates"]
        ]
    }


def _topic_context_label(topic: dict[str, Any], subtopic: dict[str, Any]) -> str:
    """Build neutral evidence context solely from catalog-backed topic identifiers."""
    topic_label = str(topic.get("id", "")).strip().upper()
    subtopic_id = str(subtopic.get("id", "")).strip()
    subtopic_title = str(subtopic.get("title", "")).strip()
    subtopic_label = f"{subtopic_id} ({subtopic_title})" if subtopic_title else subtopic_id
    return f"{topic_label} | {subtopic_label}".strip(" |")


def _select_subtopic_operation(
    subtopic: dict[str, Any],
    *,
    subject: str,
    full_text: str,
) -> dict[str, Any]:
    """Resolve a documented operation only within one already-selected subtopic.

    Operations deliberately have no contact signal: an operation is a durable
    process taxonomy node, not a recipient-based routing override.  The same
    score order as subtopics keeps a visible subject signal stronger than a
    preview-only keyword, and equal candidates remain reviewable.
    """
    operations = [
        operation for operation in subtopic.get("operations", [])
        if isinstance(operation, dict)
        and str(operation.get("status", "")).casefold() in {"", "active"}
    ] if isinstance(subtopic.get("operations"), list) else []
    candidates: list[tuple[int, dict[str, Any]]] = []
    for operation in operations:
        identifier = str(operation.get("id", "")).strip()
        title = str(operation.get("title", "")).strip()
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", identifier) or not title:
            continue
        reasons: list[str] = []
        score = 0
        for pattern in operation.get("typical_subject_patterns", []) if isinstance(operation.get("typical_subject_patterns"), list) else []:
            if _subject_signal_matches(subject, pattern):
                reasons.append(f"subject_pattern:{str(pattern).strip()}")
                score = max(score, 500)
        for value, label in ((identifier, "subject_id"), (title, "subject_title")):
            if _subject_signal_matches(subject, value):
                reasons.append(f"{label}:{value}")
                score = max(score, 400)
        for alias in operation.get("aliases", []) if isinstance(operation.get("aliases"), list) else []:
            if _subject_signal_matches(subject, alias):
                reasons.append(f"subject_alias:{str(alias).strip()}")
                score = max(score, 400)
        for keyword in operation.get("keywords", []) if isinstance(operation.get("keywords"), list) else []:
            keyword_text = str(keyword).strip()
            if _subject_signal_matches(subject, keyword_text):
                reasons.append(f"subject_keyword:{keyword_text}")
                score = max(score, 300)
            elif _artifact_text_matches(full_text, keyword_text):
                reasons.append(f"keyword:{keyword_text}")
                score = max(score, 100)
        if reasons:
            candidates.append((score, {"id": identifier, "title": title, "reasons": reasons, "_source": operation}))

    resolved = ambiguity.resolve_scored_candidates(candidates)
    if not resolved:
        return {}
    if "unique" in resolved:
        choices = [resolved["unique"]]
    else:
        choices = list(resolved["candidates"])
    # Duplicate active/legacy operation IDs make every matching operation
    # structurally ambiguous, even if one duplicate happened to score lower.
    ids = [str(operation.get("id", "")).strip().casefold() for operation in operations]
    duplicate_ids = {identifier for identifier in ids if identifier and ids.count(identifier) > 1}
    if duplicate_ids and any(candidate["id"].casefold() in duplicate_ids for candidate in choices):
        choices.extend(
            {"id": operation["id"], "title": str(operation.get("title", "")).strip(), "reasons": ["duplicate_operation_id"], "_source": operation}
            for operation in operations
            if str(operation.get("id", "")).strip().casefold() in duplicate_ids
            and not any(candidate["_source"] is operation for candidate in choices)
        )
        choices.sort(key=lambda candidate: (candidate["id"].casefold(), candidate["title"].casefold()))
    if len(choices) == 1:
        selected = choices[0]
        return {"operation": selected["id"], "match_reasons": selected["reasons"], "source": selected["_source"]}
    return {
        "candidates": [
            {"id": candidate["id"], "title": candidate["title"], "reasons": candidate["reasons"]}
            for candidate in choices
        ]
    }


def _operation_context_label(topic: dict[str, Any], subtopic: dict[str, Any], operation: dict[str, Any]) -> str:
    """Build an evidence label only from the chosen catalog objects."""
    subtopic_context = _topic_context_label(topic, subtopic)
    operation_id = str(operation.get("id", "")).strip()
    operation_title = str(operation.get("title", "")).strip()
    operation_label = f"{operation_id} ({operation_title})" if operation_title else operation_id
    return f"{subtopic_context} / {operation_label}".strip(" / ")


def _select_subtopic_event(
    subtopic: dict[str, Any],
    *,
    subject: str,
    full_text: str,
) -> dict[str, Any]:
    """Resolve a documented, active event inside one proven subtopic only."""
    events = [
        event for event in subtopic.get("events", [])
        if isinstance(event, dict) and str(event.get("status", "")).casefold() != "inactive"
    ] if isinstance(subtopic.get("events"), list) else []
    candidates: list[tuple[int, dict[str, Any]]] = []
    for event in events:
        identifier = str(event.get("id", "")).strip()
        title = str(event.get("title", "")).strip()
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", identifier) or not title:
            continue
        reasons: list[str] = []
        score = 0
        for pattern in event.get("typical_subject_patterns", []) if isinstance(event.get("typical_subject_patterns"), list) else []:
            if _subject_signal_matches(subject, pattern):
                reasons.append(f"subject_pattern:{str(pattern).strip()}")
                score = max(score, 500)
        for value, label in ((identifier, "subject_id"), (title, "subject_title")):
            if _subject_signal_matches(subject, value):
                reasons.append(f"{label}:{value}")
                score = max(score, 400)
        for alias in event.get("aliases", []) if isinstance(event.get("aliases"), list) else []:
            if _subject_signal_matches(subject, alias):
                reasons.append(f"subject_alias:{str(alias).strip()}")
                score = max(score, 400)
        for keyword in event.get("keywords", []) if isinstance(event.get("keywords"), list) else []:
            keyword_text = str(keyword).strip()
            if _subject_signal_matches(subject, keyword_text):
                reasons.append(f"subject_keyword:{keyword_text}")
                score = max(score, 300)
            elif _artifact_text_matches(full_text, keyword_text):
                reasons.append(f"keyword:{keyword_text}")
                score = max(score, 100)
        if reasons:
            candidates.append((score, {"id": identifier, "title": title, "reasons": reasons, "_source": event}))
    if not candidates:
        return {}
    highest_score = max(score for score, _ in candidates)
    choices = [candidate for score, candidate in candidates if score == highest_score]
    choices.sort(key=lambda candidate: (candidate["id"].casefold(), candidate["title"].casefold()))
    ids = [str(event.get("id", "")).strip().casefold() for event in events]
    duplicate_ids = {identifier for identifier in ids if identifier and ids.count(identifier) > 1}
    if duplicate_ids and any(candidate["id"].casefold() in duplicate_ids for candidate in choices):
        choices.extend(
            {"id": str(event.get("id", "")).strip(), "title": str(event.get("title", "")).strip(), "reasons": ["duplicate_event_id"], "_source": event}
            for event in events
            if str(event.get("id", "")).strip().casefold() in duplicate_ids
            and not any(candidate["_source"] is event for candidate in choices)
        )
        choices.sort(key=lambda candidate: (candidate["id"].casefold(), candidate["title"].casefold()))
    if len(choices) == 1:
        selected = choices[0]
        return {"event": selected["id"], "match_reasons": selected["reasons"], "source": selected["_source"]}
    return {
        "candidates": [
            {"id": candidate["id"], "title": candidate["title"], "reasons": candidate["reasons"]}
            for candidate in choices
        ]
    }


def _event_validation_reasons(
    topic: dict[str, Any], subtopic: dict[str, Any], event: dict[str, Any], workspace_root: Path
) -> list[str]:
    """Validate catalog-only event structure before it becomes a scalar or path."""
    reasons: list[str] = []
    identifier = str(event.get("id", "")).strip()
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", identifier):
        reasons.append("invalid_event_id")
    if not isinstance(event.get("title"), str) or not event["title"].strip():
        reasons.append("missing_event_title")
    if str(event.get("status", "")).casefold() not in {"", "active", "inactive"}:
        reasons.append("invalid_event_status")
    starts_on = event.get("starts_on")
    ends_on = event.get("ends_on")
    try:
        start_date = (
            date.fromisoformat(starts_on)
            if isinstance(starts_on, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", starts_on)
            else None
        )
    except ValueError:
        start_date = None
    if start_date is None:
        reasons.append("invalid_starts_on")
    if ends_on is not None:
        try:
            end_date = (
                date.fromisoformat(ends_on)
                if isinstance(ends_on, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", ends_on)
                else None
            )
        except ValueError:
            end_date = None
        if end_date is None or (start_date is not None and end_date < start_date):
            reasons.append("invalid_ends_on")
    phase = event.get("phase")
    if phase is not None and str(phase).casefold() not in {"planned", "live", "completed", "cancelled"}:
        reasons.append("invalid_event_phase")
    expected_reference = (
        f"memory/references/topics/{str(topic.get('id', '')).strip()}/subtopics/"
        f"{str(subtopic.get('id', '')).strip()}/events/{identifier}/index.md"
    )
    reference = event.get("reference_md")
    if not isinstance(reference, str) or reference.strip() != expected_reference or "\\" in reference:
        reasons.append("noncanonical_event_reference_md")
    elif not workspace_root.joinpath(*PurePosixPath(reference.strip()).parts).is_file():
        reasons.append("missing_event_dossier")
    if "cloud_storage" in event:
        storage = event.get("cloud_storage")
        if not isinstance(storage, dict) or set(storage) != {"scope", "storage_id"}:
            reasons.append("invalid_cloud_storage")
        else:
            scope = storage.get("scope")
            storage_id = storage.get("storage_id")
            source = topic.get("cloud_sync") if scope == "topic" else subtopic.get("cloud_sync") if scope == "subtopic" else None
            if not isinstance(storage_id, str) or not storage_id.strip() or not isinstance(source, dict) or not isinstance(source.get(storage_id), dict):
                reasons.append("invalid_cloud_storage")
    return list(dict.fromkeys(reasons))


def _event_context_label(topic: dict[str, Any], subtopic: dict[str, Any], event: dict[str, Any]) -> str:
    """Build evidence context from catalog-backed event fields only."""
    base = _topic_context_label(topic, subtopic)
    identifier = str(event.get("id", "")).strip()
    title = str(event.get("title", "")).strip()
    return f"{base} / {identifier} ({title})".strip(" / ")


def _build_topic_evidence(
    topic: dict[str, Any],
    subtopic: dict[str, Any],
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
    """Create canonical monthly topic evidence without persisting mail body text."""
    topic_id = str(topic.get("id", "")).strip()
    evidence_dir = resolve_evidence_dir("topics", topic_id, workspace_root=workspace_root)
    try:
        evidence_dir_rel = str(evidence_dir.relative_to(workspace_root).as_posix())
    except ValueError:
        evidence_dir_rel = str(evidence_dir.as_posix())
    participants = from_str or "Unbekannt"
    if to_str:
        participants = f"{participants}; An: {to_str}"
    entry_lines = [
        f"- {date} — {subject}.",
        f"  - Message-ID: `{message_id}`",
        f"  - Beteiligte: {participants}",
        f"  - Kontext: [{_topic_context_label(topic, subtopic)}]",
        f"  - Mailgegenstand: {subject}",
    ]
    spec: dict[str, Any] = {
        "type": "topic_evidence",
        "file": f"{evidence_dir_rel}/{year_month}.md",
        "entry": "\n".join(entry_lines),
    }
    read_escalation = _evidence_read_escalation(decision)
    if read_escalation:
        spec["read_escalation"] = read_escalation
    return spec


def _build_operation_evidence(
    topic: dict[str, Any],
    subtopic: dict[str, Any],
    operation: dict[str, Any],
    decision: dict[str, Any],
    *,
    year_month: str,
    date: str,
    subject: str,
    message_id: str,
    from_str: str,
    to_str: str,
) -> dict[str, Any]:
    """Create operation-scoped evidence without making process claims from mail text."""
    topic_id = str(topic.get("id", "")).strip()
    subtopic_id = str(subtopic.get("id", "")).strip()
    operation_id = str(operation.get("id", "")).strip()
    evidence_file = (
        f"memory/evidence/topics/{topic_id}/subtopics/{subtopic_id}/operations/"
        f"{operation_id}/{year_month}.md"
    )
    participants = from_str or "Unbekannt"
    if to_str:
        participants = f"{participants}; An: {to_str}"
    entry_lines = [
        f"- {date} — {subject}.",
        f"  - Message-ID: `{message_id}`",
        f"  - Beteiligte: {participants}",
        f"  - Kontext: [{_operation_context_label(topic, subtopic, operation)}]",
        f"  - Mailgegenstand: {subject}",
    ]
    spec: dict[str, Any] = {
        "type": "operation_evidence",
        "file": evidence_file,
        "entry": "\n".join(entry_lines),
    }
    read_escalation = _evidence_read_escalation(decision)
    if read_escalation:
        spec["read_escalation"] = read_escalation
    return spec


def _build_event_evidence(
    topic: dict[str, Any],
    subtopic: dict[str, Any],
    event: dict[str, Any],
    decision: dict[str, Any],
    *,
    year_month: str,
    date: str,
    subject: str,
    message_id: str,
    from_str: str,
    to_str: str,
) -> dict[str, Any]:
    """Create event-scoped monthly evidence without persisting mail body text."""
    topic_id = str(topic.get("id", "")).strip()
    event_id = str(event.get("id", "")).strip()
    participants = from_str or "Unbekannt"
    if to_str:
        participants = f"{participants}; An: {to_str}"
    spec: dict[str, Any] = {
        "type": "event_evidence",
        "file": f"memory/evidence/topics/{topic_id}/events/{event_id}/{year_month}.md",
        "entry": "\n".join([
            f"- {date} — {subject}.",
            f"  - Message-ID: `{message_id}`",
            f"  - Beteiligte: {participants}",
            f"  - Kontext: [{_event_context_label(topic, subtopic, event)}]",
            f"  - Mailgegenstand: {subject}",
        ]),
    }
    read_escalation = _evidence_read_escalation(decision)
    if read_escalation:
        spec["read_escalation"] = read_escalation
    return spec


def _safe_subtopic_reference_target(
    topic: dict[str, Any], subtopic: dict[str, Any], workspace_root: Path
) -> list[dict[str, str]]:
    """Return one catalog-declared target only for an existing canonical subtopic file."""
    topic_id = str(topic.get("id", "")).strip()
    subtopic_id = str(subtopic.get("id", "")).strip()
    reference = subtopic.get("reference_md")
    if not topic_id or not subtopic_id or not isinstance(reference, str) or not reference.strip():
        return []
    raw_path = reference.strip()
    expected = f"memory/references/topics/{topic_id}/subtopics/{subtopic_id}.md"
    if raw_path != expected or "\\" in raw_path:
        return []
    path = PurePosixPath(raw_path)
    if any(part in {"", ".", ".."} for part in path.parts):
        return []
    candidate = workspace_root.joinpath(*path.parts)
    if not candidate.is_file():
        return []
    return [{"file": raw_path, "type": "subtopic_reference"}]


def _safe_operation_reference_target(
    topic: dict[str, Any], subtopic: dict[str, Any], operation: dict[str, Any], workspace_root: Path
) -> list[dict[str, str]]:
    """Return an operation target only for its exact catalogued index.md path."""
    topic_id = str(topic.get("id", "")).strip()
    subtopic_id = str(subtopic.get("id", "")).strip()
    operation_id = str(operation.get("id", "")).strip()
    reference = operation.get("reference_md")
    if not all((topic_id, subtopic_id, operation_id)) or not isinstance(reference, str) or not reference.strip():
        return []
    expected = (
        f"memory/references/topics/{topic_id}/subtopics/{subtopic_id}/operations/"
        f"{operation_id}/index.md"
    )
    raw_path = reference.strip()
    if raw_path != expected or "\\" in raw_path:
        return []
    path = PurePosixPath(raw_path)
    if any(part in {"", ".", ".."} for part in path.parts):
        return []
    if not workspace_root.joinpath(*path.parts).is_file():
        return []
    return [{"file": raw_path, "type": "operation_reference"}]


def _safe_event_dossier_target(
    topic: dict[str, Any], subtopic: dict[str, Any], event: dict[str, Any], workspace_root: Path
) -> list[dict[str, str]]:
    """Return the already-validated, existing canonical event dossier target."""
    if _event_validation_reasons(topic, subtopic, event, workspace_root):
        return []
    return [{"file": str(event["reference_md"]).strip(), "type": "event_dossier"}]


def _has_canonical_operation_reference(
    topic: dict[str, Any], subtopic: dict[str, Any], operation: dict[str, Any]
) -> bool:
    """Accept an omitted reference, but reject a supplied noncanonical one."""
    reference = operation.get("reference_md")
    if reference in (None, ""):
        return True
    if not isinstance(reference, str):
        return False
    expected = (
        f"memory/references/topics/{str(topic.get('id', '')).strip()}/subtopics/"
        f"{str(subtopic.get('id', '')).strip()}/operations/"
        f"{str(operation.get('id', '')).strip()}/index.md"
    )
    return reference.strip() == expected and "\\" not in reference


def select_topic_match(
    topics: list[dict[str, Any]],
    *,
    subject: str,
    full_text: str,
    full_text_lower: str,
    from_str: str,
    to_str: str,
    parties: str,
) -> dict[str, Any] | None:
    """Run the ordered root topic-catalog loop plus the unique-subtopic fallback/override.

    Extracted verbatim from the inline ``classify_email`` logic so the facade keeps the
    same catalog order, first-match semantics, high/medium confidence policy and the
    unique-subtopic fallback/override.  Returns
    ``{"id", "folder", "title", "confidence", "catalog", "preselected_subtopic"}`` for the
    winning catalog entry, where ``catalog`` is the exact matched topic object and
    ``preselected_subtopic`` is the resolved subtopic selection (or ``None``), or ``None``
    when no entry matches.
    """
    matched_topic: dict[str, Any] | None = None
    matched_topic_confidence = "low"
    matched_topic_source = ""
    matched_topic_strength = 0
    matched_catalog: dict[str, Any] | None = None
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

        # 2a. Match Title, ID, or Alias in Subject
        t_names = [n for n in [title, t_id] + aliases if n and len(n) >= 3]
        for name in t_names:
            name_norm = re.sub(r"[-_]+", " ", name)
            if re.search(r"\b" + re.escape(name) + r"\b", subject, re.IGNORECASE) or re.search(r"\b" + re.escape(name_norm) + r"\b", subj_norm, re.IGNORECASE):
                matched_topic = {"id": t_id, "folder": mb_folder, "title": title or t_id}
                matched_topic_confidence = "high"
                matched_topic_source = "root_name"
                matched_topic_strength = 400
                break
        if matched_topic:
            matched_catalog = top
            break

        # 2b. Typical Subject Patterns in Subject
        for pat in typical_patterns:
            pat_norm = re.sub(r"[-_]+", " ", pat)
            if (pat and pat.lower() in subject.lower()) or (pat_norm and re.search(r"\b" + re.escape(pat_norm) + r"\b", subj_norm, re.IGNORECASE)):
                matched_topic = {"id": t_id, "folder": mb_folder, "title": title or t_id}
                matched_topic_confidence = "high"
                matched_topic_source = "root_pattern"
                matched_topic_strength = 300
                break
        if matched_topic:
            matched_catalog = top
            break

        # 2c. Keywords or domain/contact matching
        has_kw_subj = any(
            re.search(r"\b" + re.escape(kw) + r"\b", subject, re.IGNORECASE)
            or re.search(r"\b" + re.escape(re.sub(r"[-_]+", " ", kw)) + r"\b", subj_norm, re.IGNORECASE)
            for kw in keywords if len(kw) >= 3
        )
        has_kw_body = any(kw.lower() in full_text_lower for kw in keywords if len(kw) >= 4)
        has_contact = any(c in parties for c in contacts if c)
        has_domain = any(d in from_str.lower() for d in domains if d) or any(
            d in parties for d in domains if d and d not in ("boku.ac.at", "gmail.com", "outlook.com", "yahoo.com")
        )

        if has_kw_subj:
            matched_topic = {"id": t_id, "folder": mb_folder, "title": title or t_id}
            matched_topic_confidence = "high"
            matched_topic_source = "root_keyword"
            matched_topic_strength = 200
            matched_catalog = top
            break
        elif has_kw_body and (has_contact or has_domain):
            matched_topic = {"id": t_id, "folder": mb_folder, "title": title or t_id}
            matched_topic_confidence = "medium"
            matched_topic_source = "root_context"
            matched_topic_strength = 100
            matched_catalog = top
            break

    # Preserve established routing for an explicit subtopic-only signal while
    # refusing to guess between two parent topics. A unique, documented
    # subtopic subject pattern may also beat a weaker generic root pattern or
    # keyword from another topic; explicit parent names always remain stronger.
    fallback_matches: list[tuple[int, tuple[dict[str, Any], dict[str, Any], int]]] = []
    fallback_ambiguity = False
    for top in (topics or []):
        if not isinstance(top, dict):
            continue
        resolution = _select_topic_subtopic(
            top,
            subject=subject,
            full_text=full_text,
            from_str=from_str,
        )
        reasons = resolution.get("match_reasons", [])
        subject_strength = 0
        if isinstance(reasons, list):
            for reason in reasons:
                if not isinstance(reason, str):
                    continue
                if reason.startswith("subject_pattern:"):
                    subject_strength = max(subject_strength, 500)
                elif reason.startswith(("subject_id:", "subject_title:", "subject_alias:")):
                    subject_strength = max(subject_strength, 400)
                elif reason.startswith("subject_keyword:"):
                    subject_strength = max(subject_strength, 300)
        if isinstance(resolution.get("subtopic"), str) and subject_strength:
            fallback_matches.append((subject_strength, (top, resolution, subject_strength)))
        elif isinstance(resolution.get("candidates"), list):
            fallback_ambiguity = True
    selected_fallback = ambiguity.select_unique_fallback(
        fallback_matches, ambiguous=fallback_ambiguity
    )
    preselected_subtopic: dict[str, Any] | None = None
    if selected_fallback is not None:
        fallback_topic, fallback_resolution, fallback_strength = selected_fallback
        can_select_fallback = not matched_topic
        can_override_generic_root = (
            matched_topic_source in {"root_pattern", "root_keyword", "root_context"}
            and fallback_strength > matched_topic_strength
            and fallback_strength >= 400
        )
        if can_select_fallback or can_override_generic_root:
            preselected_subtopic = fallback_resolution
            fallback_id = str(fallback_topic.get("id", "")).strip()
            fallback_title = str(fallback_topic.get("title", fallback_id)).strip() or fallback_id
            fallback_folder = fallback_topic.get("mailbox_folder") or f"Themen/{fallback_title}"
            matched_topic = {"id": fallback_id, "folder": fallback_folder, "title": fallback_title}
            matched_topic_confidence = "high"
            matched_catalog = fallback_topic

    if not matched_topic:
        return None
    return {
        "id": matched_topic["id"],
        "folder": matched_topic["folder"],
        "title": matched_topic["title"],
        "confidence": matched_topic_confidence,
        "catalog": matched_catalog,
        "preselected_subtopic": preselected_subtopic,
    }


def materialize_topic_details(
    topic: dict[str, Any],
    decision: dict[str, Any],
    *,
    subject: str,
    full_text: str,
    preselected_subtopic: dict[str, Any] | None,
    workspace_root: Path,
    year_month: str,
    date: str,
    message_id: str,
    from_str: str,
    to_str: str,
) -> dict[str, Any]:
    """Enrich one already-selected topic with subtopic/operation/event detail.

    Extracted verbatim from the inline ``classify_email`` detail step so the facade keeps
    the same subtopic refinement, operation/event selection, event validation, cross-kind
    conflict handling, domain evidence and safe synthesis targets.  ``decision`` is enriched
    in place and returned under ``decision`` together with the derived ``evidence`` (or
    ``None``) and ``synthesis_targets``.
    """
    evidence_spec: dict[str, Any] | None = None
    synthesis_targets: list[dict[str, str]] = []

    subtopic_resolution = preselected_subtopic or _select_topic_subtopic(
        topic, subject=subject, full_text=full_text, from_str=from_str
    )
    subtopic_source = subtopic_resolution.get("source")
    if isinstance(subtopic_source, dict) and isinstance(subtopic_resolution.get("subtopic"), str):
        decision["subtopic"] = subtopic_resolution["subtopic"]
        decision["subtopic_match_reasons"] = subtopic_resolution.get("match_reasons", [])
        evidence_spec = _build_topic_evidence(
            topic,
            subtopic_source,
            decision,
            workspace_root=workspace_root,
            year_month=year_month,
            date=date,
            subject=subject,
            message_id=message_id,
            from_str=from_str,
            to_str=to_str,
        )
        synthesis_targets = _safe_subtopic_reference_target(topic, subtopic_source, workspace_root)
        # An operation may only refine an already proven subtopic.  It never
        # changes parent routing and thread inheritance supplies neither an
        # operation scalar nor operation evidence on its own.
        operation_resolution = _select_subtopic_operation(
            subtopic_source, subject=subject, full_text=full_text
        )
        operation_source = operation_resolution.get("source")
        if isinstance(operation_source, dict) and isinstance(operation_resolution.get("operation"), str):
            if _has_canonical_operation_reference(topic, subtopic_source, operation_source):
                decision["operation"] = operation_resolution["operation"]
                decision["operation_match_reasons"] = operation_resolution.get("match_reasons", [])
                evidence_spec = _build_operation_evidence(
                    topic,
                    subtopic_source,
                    operation_source,
                    decision,
                    year_month=year_month,
                    date=date,
                    subject=subject,
                    message_id=message_id,
                    from_str=from_str,
                    to_str=to_str,
                )
                synthesis_targets = _safe_operation_reference_target(
                    topic, subtopic_source, operation_source, workspace_root
                )
            else:
                decision["operation_candidates"] = [{
                    "id": operation_resolution["operation"],
                    "title": str(operation_source.get("title", "")).strip(),
                    "reasons": list(operation_resolution.get("match_reasons", [])) + ["noncanonical_reference_md"],
                }]
        elif isinstance(operation_resolution.get("candidates"), list):
            decision["operation_candidates"] = operation_resolution["candidates"]

        # Events are finite/dated and deliberately separate from durable
        # operations. They can only be considered after the same current-mail
        # topic and subtopic proof; no contact or thread value participates.
        event_resolution = _select_subtopic_event(subtopic_source, subject=subject, full_text=full_text)
        event_source = event_resolution.get("source")
        if isinstance(event_source, dict) and isinstance(event_resolution.get("event"), str):
            validation_reasons = _event_validation_reasons(topic, subtopic_source, event_source, workspace_root)
            if validation_reasons:
                decision["event_candidates"] = [{
                    "id": event_resolution["event"],
                    "title": str(event_source.get("title", "")).strip(),
                    "reasons": list(event_resolution.get("match_reasons", [])) + validation_reasons,
                }]
            else:
                decision["event"] = event_resolution["event"]
                decision["event_match_reasons"] = event_resolution.get("match_reasons", [])
                evidence_spec = _build_event_evidence(
                    topic,
                    subtopic_source,
                    event_source,
                    decision,
                    year_month=year_month,
                    date=date,
                    subject=subject,
                    message_id=message_id,
                    from_str=from_str,
                    to_str=to_str,
                )
                synthesis_targets = _safe_event_dossier_target(topic, subtopic_source, event_source, workspace_root)
        elif isinstance(event_resolution.get("candidates"), list):
            decision["event_candidates"] = event_resolution["candidates"]

        # A mail that plausibly identifies both activity kinds is structurally
        # ambiguous even when only one side was individually unique. Convert
        # every scalar back into a candidate and retain only subtopic-level
        # evidence/targets; no event or operation path may survive the conflict.
        if ambiguity.cross_kind_conflict(decision):
            if isinstance(decision.get("operation"), str):
                decision["operation_candidates"] = [{
                    "id": decision.pop("operation"),
                    "title": str(operation_source.get("title", "")).strip() if isinstance(operation_source, dict) else "",
                    "reasons": list(decision.pop("operation_match_reasons", [])),
                }]
            if isinstance(decision.get("event"), str):
                decision["event_candidates"] = [{
                    "id": decision.pop("event"),
                    "title": str(event_source.get("title", "")).strip() if isinstance(event_source, dict) else "",
                    "reasons": list(decision.pop("event_match_reasons", [])),
                }]
            for field in ("operation_candidates", "event_candidates"):
                decision[field] = ambiguity.mark_cross_kind_conflict(decision.get(field, []))
            evidence_spec = _build_topic_evidence(
                topic,
                subtopic_source,
                decision,
                workspace_root=workspace_root,
                year_month=year_month,
                date=date,
                subject=subject,
                message_id=message_id,
                from_str=from_str,
                to_str=to_str,
            )
            synthesis_targets = _safe_subtopic_reference_target(topic, subtopic_source, workspace_root)
    elif isinstance(subtopic_resolution.get("candidates"), list):
        decision["subtopic_candidates"] = subtopic_resolution["candidates"]

    return {
        "decision": decision,
        "evidence": evidence_spec,
        "synthesis_targets": synthesis_targets,
    }
