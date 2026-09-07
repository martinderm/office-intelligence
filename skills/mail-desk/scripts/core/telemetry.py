"""Pure telemetry helpers for completed mail-desk batch items."""

from __future__ import annotations

from typing import Any, Iterable, Mapping


def empty_telemetry() -> dict[str, Any]:
    """Return the canonical telemetry object for a batch without affected targets."""
    return {
        "affected_projects": [],
        "affected_topics": [],
        "synthesis_required": False,
    }


def canonicalize_telemetry(value: object) -> dict[str, Any]:
    """Return a contract-shaped telemetry object or the canonical empty object."""
    if not isinstance(value, Mapping):
        return empty_telemetry()

    projects = value.get("affected_projects")
    topics = value.get("affected_topics")
    required = value.get("synthesis_required")
    if (
        not isinstance(projects, list)
        or not isinstance(topics, list)
        or not all(isinstance(identifier, str) for identifier in [*projects, *topics])
        or type(required) is not bool
    ):
        return empty_telemetry()
    return {
        "affected_projects": projects.copy(),
        "affected_topics": topics.copy(),
        "synthesis_required": required,
    }


def collect_telemetry(
    items: Iterable[Mapping[str, Any]],
    results: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Collect affected targets from aligned input items and successful results.

    IDs retain their manifest spelling apart from surrounding whitespace. The
    paired iteration preserves the input-batch order and ignores missing results.
    """
    telemetry = empty_telemetry()
    projects = telemetry["affected_projects"]
    topics = telemetry["affected_topics"]
    seen_projects: set[str] = set()
    seen_topics: set[str] = set()

    for item, result in zip(items, results):
        if result.get("success") is not True:
            continue
        decision = item.get("decision")
        if not isinstance(decision, Mapping):
            continue
        kind = decision.get("kind")
        identifier = decision.get("id")
        if kind not in {"project", "topic"} or not isinstance(identifier, str):
            continue
        identifier = identifier.strip()
        if not identifier:
            continue

        affected, seen = (
            (projects, seen_projects)
            if kind == "project"
            else (topics, seen_topics)
        )
        if identifier not in seen:
            affected.append(identifier)
            seen.add(identifier)

    telemetry["synthesis_required"] = bool(projects or topics)
    return telemetry
