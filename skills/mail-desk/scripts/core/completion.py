"""Fail-closed completion gate for post-batch synthesis handoffs.

Execution can create a source-bound *candidate*, but it cannot release work to
the LLM synthesis stage.  Release is allowed only after every source message
has been verified, or after a reconcile has completed the same evidence chain.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from .common import normalize_message_id
from .synthesis_handoff import canonicalize_synthesis_handoff, empty_synthesis_handoff


def _verified_ids(results: object) -> list[str] | None:
    if not isinstance(results, list):
        return None
    output: list[str] = []
    for row in results:
        if not isinstance(row, Mapping) or row.get("consistent") is not True:
            return None
        message_id = normalize_message_id(str(row.get("message_id", "")))
        if not message_id or message_id in output:
            return None
        output.append(message_id)
    return output


def release_synthesis_handoff(candidate: object, verify_result: object) -> dict[str, Any]:
    """Release one canonical candidate only when its sources were all verified."""
    handoff = canonicalize_synthesis_handoff(candidate)
    if not isinstance(verify_result, Mapping) or verify_result.get("ok") is not True:
        return empty_synthesis_handoff()
    verified_ids = _verified_ids(verify_result.get("results"))
    if verified_ids is None:
        return empty_synthesis_handoff()
    if handoff["status"] == "not_required":
        return handoff
    source_ids = [item["message_id"] for item in handoff["items"]]
    if any(message_id not in verified_ids for message_id in source_ids):
        return empty_synthesis_handoff()
    return handoff


def completion_report(
    *,
    source: str,
    status: str,
    verified_message_ids: Iterable[str] = (),
    recovery_required: bool = False,
    handoff: object = None,
) -> dict[str, Any]:
    """Return a compact, source-bound terminal report for humans and agents."""
    ids: list[str] = []
    for raw in verified_message_ids:
        message_id = normalize_message_id(str(raw))
        if message_id and message_id not in ids:
            ids.append(message_id)
    released = canonicalize_synthesis_handoff(handoff)
    completed = status == "completed" and not recovery_required
    report_status = "completed" if completed else ("verification_required" if status == "verification_required" and not recovery_required else "recovery_required")
    return {
        "schema_version": 1,
        "status": report_status,
        "source": source,
        "verified_message_ids": ids,
        "handoff_released": completed,
        "synthesis_required": released["status"] == "pending",
        "message": (
            "Batch verification completed; the source-bound synthesis handoff is ready."
            if completed and released["status"] == "pending"
            else "Batch verification completed; no synthesis handoff is required."
            if completed
            else "Verification is required before synthesis can be released."
            if report_status == "verification_required"
            else "Batch completion is blocked; reconcile is required before synthesis."
        ),
    }
