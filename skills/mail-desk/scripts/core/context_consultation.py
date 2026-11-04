"""Advisory context-consultation metadata for classified draft/inspect items.

After classification, ``draft`` and ``inspect`` call
:func:`add_context_consultation_metadata` to attach bounded, read-only hints to
``decision.artifact_candidates``:

* ``events`` -- existing, catalog-consistent event dossiers of the decided
  project or (decided) subtopic;
* ``cloud_storage`` -- storage IDs declared in the matching ``cloud_sync`` maps.

The hints tell the later LLM synthesis which canonical contexts it must consult
(see ``references/synthesis.md``).  They carry no routing, evidence, target or
approval effect: ``action``, ``notes``, ``evidence``, ``synthesis_targets``,
``event``, ``event_candidates``, ``confidence`` and ``review_required`` are never
touched, and the classifier/matching modules are not involved, so
``classifier_revision`` does not rotate.  The module only reads files below the
workspace, never writes, never uses the network or a mailbox, and emits
workspace-relative POSIX paths only.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any, Iterable, Mapping

from .classifier import load_catalogs

__all__ = ["add_context_consultation_metadata"]

#: Maximum candidates emitted per list (``events`` and ``cloud_storage``).
MAX_CANDIDATES = 3
#: Maximum directory entries inspected below one project ``events/`` directory.
MAX_SCANNED_PROJECT_EVENTS = 500
#: Maximum bytes read from a dossier ``index.md`` to find its title.
MAX_TITLE_READ_BYTES = 8 * 1024
MAX_TITLE_CHARS = 200
MAX_REASONS = 3
MAX_REASON_CHARS = 120
MAX_MATCH_TEXT_CHARS = 8 * 1024

_PROJECT_EVENT_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,99}$")
_CATALOG_EVENT_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SAFE_OWNER_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_SAFE_STORAGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

_SCOPE_ORDER = {"subtopic": 0, "project": 0, "topic": 1}


def add_context_consultation_metadata(
    items: Iterable[Any],
    *,
    workspace_root: Path,
    projects: list[dict[str, Any]] | None = None,
    topics: list[dict[str, Any]] | None = None,
) -> None:
    """Attach bounded event/cloud hints to ``decision.artifact_candidates`` in place."""
    root = Path(workspace_root)
    if projects is None or topics is None:
        loaded_projects, loaded_topics = load_catalogs(root)
        projects = loaded_projects if projects is None else projects
        topics = loaded_topics if topics is None else topics
    project_rows = [row for row in projects if isinstance(row, dict)] if isinstance(projects, list) else []
    topic_rows = [row for row in topics if isinstance(row, dict)] if isinstance(topics, list) else []

    for item in items:
        if not isinstance(item, dict):
            continue
        decision = item.get("decision")
        if not isinstance(decision, dict):
            continue
        kind = decision.get("kind")
        identifier = decision.get("id")
        if kind not in ("project", "topic") or not isinstance(identifier, str):
            continue
        text = _match_text(item)
        events: list[dict[str, Any]] = []
        storages: list[dict[str, Any]] = []
        if kind == "project":
            project = _lookup(project_rows, identifier)
            if project is None:
                continue
            events = _project_events(root, str(project["id"]), text)
            storages = _cloud_storages(project.get("cloud_sync"), "project")
        else:
            topic = _lookup(topic_rows, identifier)
            if topic is None:
                continue
            storages = _cloud_storages(topic.get("cloud_sync"), "topic")
            subtopic_id = decision.get("subtopic")
            if isinstance(subtopic_id, str) and isinstance(topic.get("subtopics"), list):
                subtopic = _lookup(
                    [row for row in topic["subtopics"] if isinstance(row, dict)], subtopic_id
                )
                if subtopic is not None:
                    events = _topic_events(
                        root,
                        topic,
                        subtopic,
                        text,
                        selected_event=decision.get("event"),
                        candidate_events=decision.get("event_candidates"),
                    )
                    storages = _bounded_storages(
                        _cloud_storages(subtopic.get("cloud_sync"), "subtopic") + storages
                    )
        if not events and not storages:
            continue
        existing = decision.get("artifact_candidates")
        if existing is None:
            existing = {}
        elif not isinstance(existing, dict):
            continue
        added = False
        if events and "events" not in existing:
            existing["events"] = events
            added = True
        if storages and "cloud_storage" not in existing:
            existing["cloud_storage"] = storages
            added = True
        if added:
            decision["artifact_candidates"] = existing


def _lookup(rows: list[dict[str, Any]], identifier: str) -> dict[str, Any] | None:
    """Exact catalog ID first, else one unique case-insensitive match."""
    wanted = identifier.strip()
    if not wanted:
        return None
    valid = [row for row in rows if isinstance(row.get("id"), str) and _SAFE_OWNER_SEGMENT.match(row["id"].strip())]
    for row in valid:
        if row["id"].strip() == wanted:
            return dict(row, id=row["id"].strip())
    folded = [row for row in valid if row["id"].strip().casefold() == wanted.casefold()]
    if len(folded) == 1:
        return dict(folded[0], id=folded[0]["id"].strip())
    return None


def _match_text(item: Mapping[str, Any]) -> str:
    parts = [str(item.get(field) or "") for field in ("subject", "preview")]
    return "\n".join(parts)[:MAX_MATCH_TEXT_CHARS]


def _signal_matches(text: str, signal: object) -> bool:
    value = str(signal).strip()
    if len(value) < 3 or not text:
        return False
    normalized_text = re.sub(r"[-_]+", " ", text)
    normalized_signal = re.sub(r"[-_]+", " ", value)
    return bool(
        re.search(r"(?<!\w)" + re.escape(value) + r"(?!\w)", text, re.IGNORECASE)
        or re.search(r"(?<!\w)" + re.escape(normalized_signal) + r"(?!\w)", normalized_text, re.IGNORECASE)
    )


def _reason(label: str, value: object) -> str:
    return f"{label}:{str(value).strip()}"[:MAX_REASON_CHARS]


def _score(text: str, identifier: str, title: str, event: Mapping[str, Any]) -> tuple[int, list[str]]:
    reasons: list[str] = []
    score = 0
    patterns = event.get("typical_subject_patterns")
    for pattern in patterns if isinstance(patterns, list) else []:
        if _signal_matches(text, pattern):
            reasons.append(_reason("subject_pattern", pattern))
            score = max(score, 500)
    for value, label in ((identifier, "subject_id"), (title, "subject_title")):
        if value and _signal_matches(text, value):
            reasons.append(_reason(label, value))
            score = max(score, 400)
    aliases = event.get("aliases")
    for alias in aliases if isinstance(aliases, list) else []:
        if _signal_matches(text, alias):
            reasons.append(_reason("subject_alias", alias))
            score = max(score, 400)
    keywords = event.get("keywords")
    for keyword in keywords if isinstance(keywords, list) else []:
        if _signal_matches(text, keyword):
            reasons.append(_reason("subject_keyword", keyword))
            score = max(score, 300)
    return score, list(dict.fromkeys(reasons))[:MAX_REASONS]


def _contained_regular_file(root: Path, relative: str) -> bool:
    """Return True only for a regular file with no symlink/reparse component below root."""
    current = root
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    for part in PurePosixPath(relative).parts:
        current = current / part
        try:
            info = current.lstat()
        except OSError:
            return False
        if current.is_symlink():
            return False
        if reparse_flag and getattr(info, "st_file_attributes", 0) & reparse_flag:
            return False
    try:
        return current.is_file()
    except OSError:
        return False


def _first_heading(path: Path) -> str | None:
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_TITLE_READ_BYTES)
    except OSError:
        return None
    for line in raw.decode("utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            title = _CONTROL.sub(" ", stripped.lstrip("#")).strip()
            return title[:MAX_TITLE_CHARS] if title else None
    return None


def _project_events(root: Path, project_id: str, text: str) -> list[dict[str, Any]]:
    relative = f"memory/references/projects/{project_id}/events"
    events_dir = root.joinpath(*PurePosixPath(relative).parts)
    try:
        names = sorted(os.listdir(events_dir))[:MAX_SCANNED_PROJECT_EVENTS]
    except OSError:
        return []
    ranked: list[tuple[int, str, dict[str, Any]]] = []
    for name in names:
        if not _PROJECT_EVENT_SLUG.match(name):
            continue
        relative_file = f"{relative}/{name}/index.md"
        if not _contained_regular_file(root, relative_file):
            continue
        title = _first_heading(root.joinpath(*PurePosixPath(relative_file).parts))
        score, reasons = _score(text, name, title or "", {})
        row: dict[str, Any] = {"id": name}
        if title:
            row["title"] = title
        row["reference_md"] = relative_file
        row["reasons"] = reasons or ["existing_event_dossier"]
        ranked.append((score, name, row))
    ranked.sort(key=lambda entry: (-entry[0], entry[1]))
    return [row for _, _, row in ranked[:MAX_CANDIDATES]]


def _topic_events(
    root: Path,
    topic: Mapping[str, Any],
    subtopic: Mapping[str, Any],
    text: str,
    *,
    selected_event: object = None,
    candidate_events: object = None,
) -> list[dict[str, Any]]:
    raw_events = subtopic.get("events")
    if not isinstance(raw_events, list):
        return []
    topic_id = str(topic["id"])
    subtopic_id = str(subtopic["id"])
    identifiers = [
        str(event.get("id", "")).strip().casefold() for event in raw_events if isinstance(event, dict)
    ]
    priority: dict[str, tuple[int, str]] = {}
    if isinstance(selected_event, str) and selected_event.strip():
        priority[selected_event.strip().casefold()] = (1000, "selected_event")
    if isinstance(candidate_events, list):
        for row in candidate_events:
            if isinstance(row, dict):
                candidate = str(row.get("id", "")).strip().casefold()
            elif isinstance(row, str):
                candidate = row.strip().casefold()
            else:
                continue
            if candidate:
                priority.setdefault(candidate, (900, "event_candidate"))
    ranked: list[tuple[int, str, dict[str, Any]]] = []
    for event in raw_events:
        if not isinstance(event, dict):
            continue
        identifier = str(event.get("id", "")).strip()
        title = event.get("title")
        if not _CATALOG_EVENT_ID.match(identifier) or identifiers.count(identifier.casefold()) != 1:
            continue
        if not isinstance(title, str) or not title.strip():
            continue
        if str(event.get("status", "")).casefold() not in {"", "active"}:
            continue
        expected = (
            f"memory/references/topics/{topic_id}/subtopics/{subtopic_id}/events/{identifier}/index.md"
        )
        reference = event.get("reference_md")
        if not isinstance(reference, str) or reference.strip() != expected:
            continue
        if not _contained_regular_file(root, expected):
            continue
        score, reasons = _score(text, identifier, title.strip(), event)
        if identifier.casefold() in priority:
            priority_score, priority_reason = priority[identifier.casefold()]
            score = max(score, priority_score)
            reasons = list(dict.fromkeys([priority_reason, *reasons]))[:MAX_REASONS]
        ranked.append(
            (
                score,
                identifier,
                {
                    "id": identifier,
                    "title": title.strip()[:MAX_TITLE_CHARS],
                    "reference_md": expected,
                    "subtopic": subtopic_id,
                    "reasons": reasons or ["catalog_event_dossier"],
                },
            )
        )
    ranked.sort(key=lambda entry: (-entry[0], entry[1]))
    return [row for _, _, row in ranked[:MAX_CANDIDATES]]


def _cloud_storages(cloud_sync: object, scope: str) -> list[dict[str, Any]]:
    if not isinstance(cloud_sync, dict):
        return []
    rows: list[dict[str, Any]] = []
    for key in cloud_sync:
        if not isinstance(key, str) or not _SAFE_STORAGE_ID.match(key):
            continue
        rows.append({"scope": scope, "storage_id": key, "reasons": ["declared_cloud_sync"]})
    return _bounded_storages(rows)


def _bounded_storages(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique = {(row["scope"], row["storage_id"]): row for row in rows}
    ordered = sorted(
        unique.values(),
        key=lambda row: (_SCOPE_ORDER[row["scope"]], row["storage_id"].casefold(), row["storage_id"]),
    )
    return ordered[:MAX_CANDIDATES]
