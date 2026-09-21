"""Reusable ambiguity policy for the mail-desk classifier (FR-13 / MD-M1-T01).

This canonical owner holds the reusable pieces of the classifier's ambiguity handling
that were previously inlined in :mod:`core.classifier`:

* :func:`resolve_scored_candidates` — deterministic ranking plus unique-choice/tie
  resolution over ``(score, candidate)`` pairs (used by topic and operation matching).
* :func:`select_unique_fallback` — the strongest unique fallback choice, unless a
  sibling signal made the decision structurally ambiguous (used by parent-topic
  fallback selection).
* :func:`cross_kind_conflict` / :func:`mark_cross_kind_conflict` — detection and
  marking of a mail that plausibly identifies both activity kinds.

The module has no catalog I/O, no side effects and never imports ``classifier.py``.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

__all__ = [
    "resolve_scored_candidates",
    "select_unique_fallback",
    "cross_kind_conflict",
    "mark_cross_kind_conflict",
]

#: Reason appended to both activity-kind candidate lists on a cross-kind conflict.
REASON_CROSS_KIND_CONFLICT = "cross_kind_conflict"


def _candidate_order_key(payload: Any) -> tuple[str, str]:
    """Deterministic tie order for one candidate payload (case-folded id, then title)."""
    if isinstance(payload, Mapping):
        return (
            str(payload.get("id", "")).casefold(),
            str(payload.get("title", "")).casefold(),
        )
    return (str(payload).casefold(), "")


def resolve_scored_candidates(scored: Iterable[tuple[int, Any]]) -> dict[str, Any]:
    """Rank ``(score, candidate)`` pairs and resolve unique choice versus ambiguity.

    Returns ``{}`` when there are no candidates, ``{"unique": candidate}`` when exactly
    one candidate holds the highest score, and ``{"candidates": [candidate, ...]}`` for
    a shared highest score. Candidates are ordered by descending score and then by
    case-folded ``(id, title)``; the input order never changes the result.
    """
    ranked = sorted(
        ((int(score), payload) for score, payload in scored),
        key=lambda item: (-item[0],) + _candidate_order_key(item[1]),
    )
    if not ranked:
        return {}
    highest = ranked[0][0]
    top = [payload for score, payload in ranked if score == highest]
    if len(top) == 1:
        return {"unique": top[0]}
    return {"candidates": top}


def select_unique_fallback(
    scored: Iterable[tuple[int, Any]], *, ambiguous: bool = False
) -> Any | None:
    """Return the unique strongest candidate, or ``None`` when tied or ``ambiguous``."""
    if ambiguous:
        return None
    return resolve_scored_candidates(scored).get("unique")


def cross_kind_conflict(decision: Mapping[str, Any]) -> bool:
    """Return whether ``decision`` resolved/candidate-listed both an operation and an event."""
    operation_present = isinstance(decision.get("operation"), str) or bool(
        decision.get("operation_candidates")
    )
    event_present = isinstance(decision.get("event"), str) or bool(
        decision.get("event_candidates")
    )
    return operation_present and event_present


def mark_cross_kind_conflict(candidates: Iterable[Any]) -> list[dict[str, Any]]:
    """Return ``candidates`` with ``cross_kind_conflict`` appended to each reasons list.

    Non-mapping entries are skipped and the marker is never duplicated, so the result is
    idempotent and no candidate loses an existing reason.
    """
    marked: list[dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        normalized = dict(candidate)
        reasons = list(normalized.get("reasons", []))
        if REASON_CROSS_KIND_CONFLICT not in reasons:
            reasons.append(REASON_CROSS_KIND_CONFLICT)
        normalized["reasons"] = reasons
        marked.append(normalized)
    return marked
