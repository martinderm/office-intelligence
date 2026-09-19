"""Policy-bound attachment-evaluation orchestrator skeleton (FR-15 / MD-E1-T04).

This module owns the single, separately testable ``attachment_evaluate`` seam.  It qualifies the
automatic-evaluation trigger, revalidates the real RFC-822 MIME candidates against the trusted
policy, mints and immediately guards the internal machine authorization for every eligible part,
and returns the canonical staged ``attachment_evaluation`` intermediate.

Scope of T04 (deliberately bounded):

* T04 is a **skeleton**.  It never fetches an attachment, never extracts content, never builds or
  installs an ``attachment_analysis_handoff`` and never mutates a ``DraftManifest``.  Those legs
  belong to MD-E1-T05 and are wired directly inside this orchestrator later.  The honest pre-T05
  outcome for an ambiguous mail with at least one eligible attachment is therefore
  ``status: "skipped"`` / ``reason: "evaluation_pending"`` / ``authorization: "auto_evaluated"``
  with an empty ``files`` list -- never a claim of completed extraction or handoff.
* The public envelope is exactly ``{"attachment_evaluation": staged_object}``.  T05 may later add a
  sibling ``attachment_analysis_handoff``; T04 never does.
* ``used_for_classification`` is **always** ``False`` and ``classifier_revision`` is **always**
  ``None`` in the staged intermediate.  ``status`` describes the evaluation stage only, never the
  classification result.

Trust boundary (read this before relying on the seam):

* The API accepts only the raw MIME bytes plus the trusted message/binding context and the
  decision / read-escalation metadata.  It never accepts caller-supplied candidate inventory,
  policy status, fetch status, machine receipt, authorization label, staged status/reason or
  files.  Those are computed internally from the revalidated MIME structure and the trusted policy.
* Every eligible attachment is revalidated through the existing public seams
  (:func:`core.attachments.inspect_mime_tree`,
  :func:`core.attachments.canonicalize_and_bind_attachments` -- which already re-runs
  :func:`core.attachment_policy.check_attachment_policy` with cumulative quotas -- and
  :func:`core.attachments.verify_attachment_drift`).  No second downloader, MIME parser, Office
  converter, OCR path or hash/request validator is introduced.
* The machine authorization is derived internally with
  :func:`core.attachment_authorization.create_machine_authorization` and immediately validated
  with :func:`core.attachment_authorization.guard_context_authorization` in the ``evaluation``
  context.  The opaque capability is process-internal, is never serialized and is never exposed in
  the returned envelope.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .attachment_authorization import (
    CONTEXT_EVALUATION,
    create_machine_authorization,
    guard_context_authorization,
)
from .attachment_fetch import compute_review_hash
from .attachment_policy import DEFAULT_ATTACHMENT_POLICY
from .attachments import (
    canonicalize_and_bind_attachments,
    inspect_mime_tree,
    verify_attachment_drift,
)


# ==============================================================================
# Bounded status / authorization / reason vocabulary
# ==============================================================================

STATUS_COMPLETED = "completed"
STATUS_NOT_NEEDED = "not_needed"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"

#: The full staged status set from the FR-15 contract.  T04 emits only ``not_needed`` and
#: ``skipped``; ``completed`` / ``failed`` are reserved for T05/T06.
ALLOWED_ATTACHMENT_EVALUATION_STATUSES = frozenset(
    {STATUS_COMPLETED, STATUS_NOT_NEEDED, STATUS_SKIPPED, STATUS_FAILED}
)

AUTHORIZATION_AUTO_EVALUATED = "auto_evaluated"
AUTHORIZATION_NOT_APPLICABLE = "not_applicable"
ALLOWED_ATTACHMENT_EVALUATION_AUTHORIZATIONS = frozenset(
    {AUTHORIZATION_AUTO_EVALUATED, AUTHORIZATION_NOT_APPLICABLE}
)

REASON_CLASSIFICATION_CLEAR = "classification_clear"
REASON_NO_ATTACHMENTS = "no_attachments"
REASON_NO_ALLOWED_ATTACHMENTS = "no_allowed_attachments"
#: The bounded T04 pre-T05 outcome for a genuinely eligible (allowed + available) attachment:
#: the orchestrator has authorized the part but has not yet fetched/extracted/handed it off.
REASON_EVALUATION_PENDING = "evaluation_pending"

#: Bounded reason set emitted by T04.  T06 extends this with the remaining FR-15 fail-closed
#: reasons (``lock_unavailable``, ``policy_blocked``, ``quota_exceeded``, ``fetch_failed``,
#: ``extraction_failed``, ``handoff_invalid``, ``still_ambiguous``).  T04 never claims those.
ALLOWED_ATTACHMENT_EVALUATION_REASONS = frozenset(
    {
        REASON_CLASSIFICATION_CLEAR,
        REASON_NO_ATTACHMENTS,
        REASON_NO_ALLOWED_ATTACHMENTS,
        REASON_EVALUATION_PENDING,
    }
)

#: The only documented read-escalation terminal statuses that count as a completed attempt.
_READ_ESCALATION_TERMINAL_STATUSES = frozenset({"failed", "completed"})


# ==============================================================================
# Exceptions
# ==============================================================================

class AttachmentEvaluationError(ValueError):
    """Raised when the orchestrator is called with malformed trusted input.

    This is a fail-closed contract error: a malformed binding, decision, read-escalation or
    policy revision never silently degrades into a staged outcome.
    """


# ==============================================================================
# Internal helpers
# ==============================================================================

def _require_non_empty_text(value: Any, field: str) -> str:
    if value is None:
        raise AttachmentEvaluationError(
            f"attachment_evaluate requires a non-empty trusted '{field}' (got None)."
        )
    text = str(value).strip()
    if not text:
        raise AttachmentEvaluationError(
            f"attachment_evaluate requires a non-empty trusted '{field}'."
        )
    return text


def _derive_effective_policy(policy: Any) -> tuple[dict[str, Any], str]:
    """Return the effective trusted policy and its non-empty ``version`` as the revision.

    A caller-supplied revision is never accepted; it is always derived from the effective trusted
    policy.  A missing, empty or non-string version fails closed.
    """
    if policy is None:
        effective: dict[str, Any] = dict(DEFAULT_ATTACHMENT_POLICY)
    elif isinstance(policy, Mapping):
        effective = {str(key): value for key, value in policy.items()}
    else:
        raise AttachmentEvaluationError(
            "attachment_evaluate 'policy' must be a mapping or None."
        )

    version = effective.get("version")
    if not isinstance(version, str) or not version.strip():
        raise AttachmentEvaluationError(
            "The effective attachment policy must declare a non-empty string 'version'."
        )
    return effective, version.strip()


def _decision_is_ambiguous(decision: Mapping[str, Any]) -> bool:
    """The verbatim FR-15 trigger predicates for an ambiguous body/full-read decision.

    These are normative **exact** equalities: ``kind`` / ``id`` / ``confidence`` are compared
    verbatim (no strip/lowercase coercion) and ``review_required`` is an exact identity check,
    so caller casing, padding or truthy lookalikes never fabricate a trigger.
    """
    if decision.get("kind") == "unknown":
        return True
    if decision.get("id") == "unclassified":
        return True
    if decision.get("confidence") == "low":
        return True
    if decision.get("review_required") is True:
        return True
    return False


def _effective_read_escalation(
    decision: Mapping[str, Any], read_escalation: Any
) -> Mapping[str, Any] | None:
    """Resolve the escalation metadata.

    Production emits the classifier's ``read_escalation`` both as an item sibling (the explicit
    parameter) and nested inside ``decision``; the explicit parameter wins when supplied.  A
    nested value that is present but neither ``None`` nor a mapping is malformed trusted input and
    fails closed rather than silently degrading to "absent".
    """
    if read_escalation is not None:
        if not isinstance(read_escalation, Mapping):
            raise AttachmentEvaluationError(
                "attachment_evaluate 'read_escalation' must be a mapping or None."
            )
        return read_escalation
    nested = decision.get("read_escalation")
    if nested is None:
        return None
    if not isinstance(nested, Mapping):
        raise AttachmentEvaluationError(
            "attachment_evaluate decision 'read_escalation' must be a mapping or None."
        )
    return nested


def _read_escalation_without_clear_assignment(
    decision: Mapping[str, Any], escalation: Mapping[str, Any] | None
) -> bool:
    """A documented failed/completed escalation that left the final decision ambiguous.

    A read escalation never overrides an otherwise clear final decision merely because it
    occurred, so a clear assignment always short-circuits this predicate.
    """
    if escalation is None:
        return False
    status = str(escalation.get("status", "")).strip().lower()
    if status not in _READ_ESCALATION_TERMINAL_STATUSES:
        return False
    return _decision_is_ambiguous(decision)


def _should_evaluate(
    decision: Mapping[str, Any], escalation: Mapping[str, Any] | None
) -> bool:
    if _decision_is_ambiguous(decision):
        return True
    return _read_escalation_without_clear_assignment(decision, escalation)


def _staged(status: str, reason: str, authorization: str) -> dict[str, Any]:
    """Build the canonical staged ``attachment_evaluation`` object (bounded, no content)."""
    return {
        "status": status,
        "reason": reason,
        "authorization": authorization,
        "files": [],
        "used_for_classification": False,
        "classifier_revision": None,
    }


def _envelope(staged: dict[str, Any]) -> dict[str, Any]:
    return {"attachment_evaluation": staged}


def _is_eligible(part: Mapping[str, Any]) -> bool:
    """Canonically allowed + available: the only parts the orchestrator may authorize."""
    fetch_status = str(part.get("fetch_status", "")).strip().lower()
    policy_status = str(part.get("policy_status", "")).strip().lower()
    return fetch_status == "available" and policy_status == "allowed"


def _mint_and_guard_authorization(
    part: Mapping[str, Any],
    *,
    policy_revision: str,
    account: str,
    folder: str,
    envelope_id: str,
    message_id: str,
    inspected_parts: list[dict[str, Any]],
) -> None:
    """Revalidate drift, then mint and immediately guard the internal machine authorization.

    The canonical review hash is recomputed from the trusted bindings and the revalidated MIME
    candidate SHA-256 (matching the existing MD-A2 fetch semantics, where ``inventory_sha256`` is
    the payload hash of the part).  The opaque capability is consumed in-place and never returned.
    """
    part_locator = str(part.get("part_locator", "")).strip()
    inventory_sha256 = str(part.get("sha256", "")).strip().lower()

    verify_attachment_drift(
        candidate=dict(part),
        expected_account=account,
        expected_folder=folder,
        expected_message_id=message_id,
        expected_envelope_id=envelope_id,
        current_mime_parts=inspected_parts,
        verify_hash=inventory_sha256,
    )

    review_hash = compute_review_hash(
        account=account,
        message_id=message_id,
        folder=folder,
        envelope_id=envelope_id,
        part_locator=part_locator,
        inventory_sha256=inventory_sha256,
    )

    authorization = create_machine_authorization(
        request_hash=review_hash,
        policy_revision=policy_revision,
        account=account,
        message_id=message_id,
        folder=folder,
        envelope_id=envelope_id,
        part_locator=part_locator,
        inventory_sha256=inventory_sha256,
    )
    guard_context_authorization(
        authorization,
        context=CONTEXT_EVALUATION,
        expected_request_hash=review_hash,
        expected_policy_revision=policy_revision,
    )


# ==============================================================================
# Public orchestrator
# ==============================================================================

def attachment_evaluate(
    *,
    raw_eml: bytes | str,
    account: str,
    folder: str,
    envelope_id: str | int,
    message_id: str,
    decision: Mapping[str, Any],
    read_escalation: Mapping[str, Any] | None = None,
    policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the canonical staged ``attachment_evaluation`` for one message.

    Trusted inputs only: the raw MIME bytes, the message/binding context, the existing body/
    full-read ``decision`` and its optional ``read_escalation``, plus the effective trusted
    ``policy``.  Caller-claimed candidates, policy/fetch/part statuses, machine receipts,
    authorization labels, staged status/reason and files are not accepted.
    """
    if not isinstance(decision, Mapping):
        raise AttachmentEvaluationError("attachment_evaluate 'decision' must be a mapping.")

    if not isinstance(raw_eml, (bytes, str)):
        raise AttachmentEvaluationError(
            "attachment_evaluate 'raw_eml' must be bytes or str."
        )

    norm_account = _require_non_empty_text(account, "account")
    norm_folder = _require_non_empty_text(folder, "folder")
    norm_envelope = _require_non_empty_text(envelope_id, "envelope_id")
    norm_message_id = _require_non_empty_text(message_id, "message_id")
    effective_policy, policy_revision = _derive_effective_policy(policy)
    # Validate/resolve the escalation up front so a malformed value fails closed even when the
    # decision is already unambiguously ambiguous.
    effective_escalation = _effective_read_escalation(decision, read_escalation)

    # A clear final decision (or an escalation that did not leave it ambiguous) is a bounded no-op:
    # the decision itself is already authoritative and no attachment is processed.
    if not _should_evaluate(decision, effective_escalation):
        return _envelope(
            _staged(STATUS_NOT_NEEDED, REASON_CLASSIFICATION_CLEAR, AUTHORIZATION_NOT_APPLICABLE)
        )

    inspected_parts = inspect_mime_tree(raw_eml, policy=effective_policy)
    if not inspected_parts:
        return _envelope(
            _staged(STATUS_NOT_NEEDED, REASON_NO_ATTACHMENTS, AUTHORIZATION_NOT_APPLICABLE)
        )

    # Canonicalization re-runs check_attachment_policy with cumulative quotas internally, so the
    # orchestrator never duplicates that validator.
    bound_parts = canonicalize_and_bind_attachments(
        inspected_parts,
        norm_account,
        norm_folder,
        norm_envelope,
        norm_message_id,
        policy=effective_policy,
    )
    eligible_parts = [part for part in bound_parts if _is_eligible(part)]
    if not eligible_parts:
        return _envelope(
            _staged(
                STATUS_NOT_NEEDED, REASON_NO_ALLOWED_ATTACHMENTS, AUTHORIZATION_NOT_APPLICABLE
            )
        )

    for part in eligible_parts:
        _mint_and_guard_authorization(
            part,
            policy_revision=policy_revision,
            account=norm_account,
            folder=norm_folder,
            envelope_id=norm_envelope,
            message_id=norm_message_id,
            inspected_parts=inspected_parts,
        )

    # Honest pre-T05 state: the eligible parts are authorized, but fetch/extract/handoff and any
    # DraftManifest installation belong to T05 and have not happened.
    return _envelope(
        _staged(STATUS_SKIPPED, REASON_EVALUATION_PENDING, AUTHORIZATION_AUTO_EVALUATED)
    )


__all__ = [
    "ALLOWED_ATTACHMENT_EVALUATION_AUTHORIZATIONS",
    "ALLOWED_ATTACHMENT_EVALUATION_REASONS",
    "ALLOWED_ATTACHMENT_EVALUATION_STATUSES",
    "AUTHORIZATION_AUTO_EVALUATED",
    "AUTHORIZATION_NOT_APPLICABLE",
    "AttachmentEvaluationError",
    "REASON_CLASSIFICATION_CLEAR",
    "REASON_EVALUATION_PENDING",
    "REASON_NO_ALLOWED_ATTACHMENTS",
    "REASON_NO_ATTACHMENTS",
    "STATUS_COMPLETED",
    "STATUS_FAILED",
    "STATUS_NOT_NEEDED",
    "STATUS_SKIPPED",
    "attachment_evaluate",
]
