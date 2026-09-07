"""Pure, fail-closed handoff construction for post-batch LLM synthesis."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from .common import normalize_message_id
from .synthesis_targets import validate_execute_synthesis_targets


_HANDOFF_KEYS = {"schema_version", "status", "items"}
_ITEM_KEYS = {
    "message_id",
    "subject",
    "kind",
    "id",
    "synthesis_targets",
    "target_selection_required",
}


def empty_synthesis_handoff() -> dict[str, Any]:
    """Return the canonical no-work handoff."""
    return {"schema_version": 1, "status": "not_required", "items": []}


def _normalized_message_id(*values: object) -> str:
    for value in values:
        if isinstance(value, str):
            normalized = normalize_message_id(value)
            if normalized:
                return normalized
    return ""


def _validated_targets(
    kind: str,
    identifier: str,
    value: object,
) -> list[dict[str, str]] | None:
    """Validate and copy an FR-06b target list in the item's own target scope."""
    if not isinstance(value, list):
        return None
    try:
        return validate_execute_synthesis_targets(
            [{"decision": {"kind": kind, "id": identifier}, "synthesis_targets": value}]
        )[0]
    except (TypeError, ValueError):
        return None


def collect_synthesis_handoff(
    items: Iterable[Mapping[str, Any]],
    results: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Collect one source-preserving handoff item per successful paired result.

    Zip pairing deliberately retains input order and does not deduplicate: each
    successfully handled mail remains an independent evidence source.
    """
    handoff_items: list[dict[str, Any]] = []
    for item, result in zip(items, results):
        if not isinstance(item, Mapping) or not isinstance(result, Mapping):
            continue
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

        message_id = _normalized_message_id(
            result.get("message_id"),
            item.get("message_id"),
            item.get("raw_message_id"),
        )
        if not message_id:
            continue
        subject_value = result.get("subject", item.get("subject", ""))
        subject = subject_value if isinstance(subject_value, str) else ""
        targets = _validated_targets(kind, identifier, result.get("synthesis_targets"))
        if targets is None:
            continue
        handoff_items.append(
            {
                "message_id": message_id,
                "subject": subject,
                "kind": kind,
                "id": identifier,
                "synthesis_targets": targets,
                "target_selection_required": not bool(targets),
            }
        )

    return {
        "schema_version": 1,
        "status": "pending" if handoff_items else "not_required",
        "items": handoff_items,
    }


def canonicalize_synthesis_handoff(value: object) -> dict[str, Any]:
    """Return one strict, copied contract value or the canonical empty handoff."""
    if not isinstance(value, Mapping) or set(value) != _HANDOFF_KEYS:
        return empty_synthesis_handoff()
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        return empty_synthesis_handoff()
    status = value.get("status")
    raw_items = value.get("items")
    if status not in {"pending", "not_required"} or not isinstance(raw_items, list):
        return empty_synthesis_handoff()

    items: list[dict[str, Any]] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, Mapping) or set(raw_item) != _ITEM_KEYS:
            return empty_synthesis_handoff()
        kind = raw_item.get("kind")
        identifier = raw_item.get("id")
        subject = raw_item.get("subject")
        if (
            kind not in {"project", "topic"}
            or not isinstance(identifier, str)
            or not isinstance(subject, str)
        ):
            return empty_synthesis_handoff()
        identifier = identifier.strip()
        message_id = _normalized_message_id(raw_item.get("message_id"))
        if not identifier or not message_id:
            return empty_synthesis_handoff()
        targets = _validated_targets(kind, identifier, raw_item.get("synthesis_targets"))
        selection_required = raw_item.get("target_selection_required")
        if (
            targets is None
            or type(selection_required) is not bool
            or selection_required is not (not bool(targets))
        ):
            return empty_synthesis_handoff()
        items.append(
            {
                "message_id": message_id,
                "subject": subject,
                "kind": kind,
                "id": identifier,
                "synthesis_targets": targets,
                "target_selection_required": selection_required,
            }
        )

    if (status == "pending") is not bool(items):
        return empty_synthesis_handoff()
    return {"schema_version": 1, "status": status, "items": items}
