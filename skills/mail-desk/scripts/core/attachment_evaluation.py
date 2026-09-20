"""Policy-bound attachment-evaluation orchestrator (FR-15 / MD-E1-T04 + T05 + T06).

This module owns the single, separately testable ``attachment_evaluate`` seam.  It qualifies the
automatic-evaluation trigger, revalidates the real RFC-822 MIME candidates against the trusted
policy, mints and immediately guards the internal machine authorization for every eligible part,
and then composes the existing canonical seams **linearly** to produce a validated handoff:

``attachment_fetch.verify_workspace_lock`` (upfront) -> ``op_attachment_fetch`` ->
``extract_attachment_content`` -> ``build_attachment_analysis_handoff`` ->
``validate_attachment_handoff``.

Scope (deliberately bounded):

* The orchestrator returns the staged ``attachment_evaluation`` plus the validated
  ``attachment_analysis_handoff``.  It never installs anything into a persisted ``DraftManifest``
  (that is MD-E2) and never classifies.
* ``used_for_classification`` is **always** ``False`` and ``classifier_revision`` is **always**
  ``None`` in the staged intermediate.  ``status`` describes the evaluation stage only, never the
  classification result.
* Only the validated, encapsulated handoff may carry bounded attachment content.  The staged
  object never contains raw extraction text, an absolute path or a caller claim.
* T06 closes the negative matrix: a canonically expected exception (or a terminal
  ``extraction_failed`` status) is caught **exactly** and mapped to a bounded
  ``failed`` staged envelope with ``authorization: "not_applicable"``, ``files: []`` and no
  handoff sibling.  Exception text, raw content and absolute paths never leak into the output.

Trust boundary (read this before relying on the seam):

* The API accepts only the raw MIME bytes, the trusted message/binding context, the
  decision / read-escalation metadata, the effective trusted policy and the minimal trusted
  fetch/extract control-plane bindings (``run_id``, ``data_dir``, ``workspace_root``,
  ``lease_id``, ``conversation_id``).  It never accepts caller-supplied candidate inventory,
  policy status, fetch status, machine receipt, approval receipt, authorization label, staged
  status/reason, files, materiality or handoff.  Those are computed internally from the
  revalidated MIME structure, the trusted policy and the canonical seams.
* Every eligible attachment is revalidated through the existing public seams
  (:func:`core.attachments.inspect_mime_tree`,
  :func:`core.attachments.canonicalize_and_bind_attachments` -- which already re-runs
  :func:`core.attachment_policy.check_attachment_policy` with cumulative quotas -- and
  :func:`core.attachments.verify_attachment_drift`).  No second downloader, MIME parser, Office
  converter, OCR path or hash/request validator is introduced.
* The machine authorization is derived internally with
  :func:`core.attachment_authorization.create_machine_authorization` and immediately validated
  with :func:`core.attachment_authorization.guard_context_authorization` in the ``evaluation``
  context.  Only after that guard succeeds is the plain ``capability.to_dict()`` snapshot handed
  to the unchanged ``op_attachment_fetch`` as the non-authoritative structural
  ``approval_receipt``.  The opaque capability itself is process-internal, is never serialized
  and is never exposed in the returned envelope.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import attachment_fetch
from .attachment_authorization import (
    CONTEXT_EVALUATION,
    create_machine_authorization,
    guard_context_authorization,
)
from .attachment_extract import extract_attachment_content
from .attachment_fetch import (
    ActiveContentBlockedError,
    DisallowedExtensionError,
    ExtensionMimeDriftError,
    MimeDriftError,
    QuarantineCollisionError,
    QuarantineInventoryError,
    QuarantinePreflightError,
    QuotaExceededError,
    SymlinkEscapeError,
    TrackedQuarantineError,
    WorkspaceLockError,
    compute_review_hash,
    op_attachment_fetch,
)
from .attachment_handoff import (
    HANDOFF_STATUS_BLOCKED,
    HANDOFF_STATUS_READY,
    MATERIALITY_REQUIRED_FOR_DECISION,
    AttachmentHandoffError,
    InvalidMaterialityError,
    build_attachment_analysis_handoff,
    validate_attachment_handoff,
)
from .attachment_policy import DEFAULT_ATTACHMENT_POLICY
from .attachments import (
    AttachmentDriftError,
    canonicalize_and_bind_attachments,
    inspect_mime_tree,
    verify_attachment_drift,
)
from .common import resolve_data_dir


# ==============================================================================
# Bounded status / authorization / reason / coverage vocabulary
# ==============================================================================

STATUS_COMPLETED = "completed"
STATUS_NOT_NEEDED = "not_needed"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"

#: The full staged status set from the FR-15 contract.  T05 emits ``not_needed`` and
#: ``completed``; ``skipped`` / ``failed`` remain reserved for the T06 failure matrix.
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
#: T05: the eligible parts were fetched, extracted and a validated ready handoff was produced.
REASON_HANDOFF_READY = "handoff_ready"
#: T05: a validated ``blocked_on_required_attachment`` handoff remains ambiguous (never
#: downgraded to supplementary).
REASON_STILL_AMBIGUOUS = "still_ambiguous"
#: T06: the invocation does not own the workspace lock (missing or foreign).
REASON_LOCK_UNAVAILABLE = "lock_unavailable"
#: T06: a canonical policy/security guard blocked the part (active content, disallowed
#: extension, MIME/extension drift, tracked quarantine or an unreadable preflight).
REASON_POLICY_BLOCKED = "policy_blocked"
#: T06: a canonical transport/quarantine quota (per-file, cumulative or count) was exceeded.
REASON_QUOTA_EXCEEDED = "quota_exceeded"
#: T06: canonical identity/hash drift, a quarantine collision/inventory fault or a symlink
#: escape stopped the fetch fail-closed.
REASON_FETCH_FAILED = "fetch_failed"
#: T06: the canonical extraction result status is ``extraction_failed`` (including a timeout).
REASON_EXTRACTION_FAILED = "extraction_failed"
#: T06: the canonical handoff builder/validator rejected the handoff.
REASON_HANDOFF_INVALID = "handoff_invalid"
#: Historical T04 constant only.  Eligible runtime paths no longer emit it; it is retained
#: for compatibility with the documented T04 intermediate and is deliberately absent from the
#: bounded runtime reason set below.
REASON_EVALUATION_PENDING = "evaluation_pending"

#: Bounded reason set emitted by the orchestrator.  ``evaluation_pending`` is intentionally
#: absent: it is a historical non-runtime constant, never a yielded reason.
ALLOWED_ATTACHMENT_EVALUATION_REASONS = frozenset(
    {
        REASON_CLASSIFICATION_CLEAR,
        REASON_NO_ATTACHMENTS,
        REASON_NO_ALLOWED_ATTACHMENTS,
        REASON_HANDOFF_READY,
        REASON_STILL_AMBIGUOUS,
        REASON_LOCK_UNAVAILABLE,
        REASON_POLICY_BLOCKED,
        REASON_QUOTA_EXCEEDED,
        REASON_FETCH_FAILED,
        REASON_EXTRACTION_FAILED,
        REASON_HANDOFF_INVALID,
    }
)

COVERAGE_FULL = "full"
COVERAGE_TRUNCATED = "truncated"
ALLOWED_ATTACHMENT_EVALUATION_COVERAGES = frozenset({COVERAGE_FULL, COVERAGE_TRUNCATED})

#: The only documented read-escalation terminal statuses that count as a completed attempt.
_READ_ESCALATION_TERMINAL_STATUSES = frozenset({"failed", "completed"})

#: Canonical MD-A3 extraction status that is a hard, terminal failure for the evaluation stage.
_EXTRACTION_STATUS_FAILED = "extraction_failed"

# ==============================================================================
# Bounded fail-closed exception mapping (exact expected classes only)
# ==============================================================================

#: Exceptions from the canonical policy/security guards that map to ``policy_blocked``.
#: ``MimeDriftError`` is a subclass of the shared ``AttachmentDriftError`` family, so this
#: tuple must be matched before the drift family below.
_POLICY_BLOCKED_EXCEPTIONS = (
    ActiveContentBlockedError,
    DisallowedExtensionError,
    MimeDriftError,
    ExtensionMimeDriftError,
    QuarantinePreflightError,
    TrackedQuarantineError,
)

#: Canonical storage/collision guards that map to ``fetch_failed``.
_FETCH_FAILED_STORAGE_EXCEPTIONS = (
    QuarantineCollisionError,
    QuarantineInventoryError,
    SymlinkEscapeError,
)


# ==============================================================================
# Exceptions
# ==============================================================================

class AttachmentEvaluationError(ValueError):
    """Raised when the orchestrator is called with malformed trusted input.

    This is a fail-closed contract error: a malformed binding, decision, read-escalation,
    policy revision or an unexpected validated-handoff status never silently degrades into a
    staged outcome.
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


def decision_triggers_evaluation(
    decision: Mapping[str, Any] | None,
    read_escalation: Mapping[str, Any] | None = None,
) -> bool:
    """Public, authoritative view of the MD-E1 trigger predicate for the MD-E2 integration.

    MD-E2 must decide *before* calling :func:`attachment_evaluate` whether an item is
    eligible, so that a clear item performs no raw fetch/evaluation at all.  This helper
    exposes the verbatim FR-15 predicate instead of re-implementing it.  A malformed
    decision or escalation fails closed (``False``); a genuinely ambiguous decision is
    still reported as triggering regardless of a malformed escalation sibling.
    """
    if not isinstance(decision, Mapping):
        return False
    if _decision_is_ambiguous(decision):
        return True
    try:
        escalation = _effective_read_escalation(decision, read_escalation)
    except AttachmentEvaluationError:
        return False
    return _read_escalation_without_clear_assignment(decision, escalation)


def _staged(
    status: str,
    reason: str,
    authorization: str,
    *,
    files: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the canonical staged ``attachment_evaluation`` object (bounded, no content)."""
    return {
        "status": status,
        "reason": reason,
        "authorization": authorization,
        "files": list(files) if files else [],
        "used_for_classification": False,
        "classifier_revision": None,
    }


def _envelope(staged: dict[str, Any]) -> dict[str, Any]:
    return {"attachment_evaluation": staged}


def _failed_envelope(reason: str) -> dict[str, Any]:
    """Build the bounded, fail-closed staged envelope for a failed evaluation.

    A failed outcome is fully declarative: no ``files``, ``authorization: "not_applicable"``,
    pinned ``used_for_classification`` / ``classifier_revision`` and **no** handoff sibling.
    The exception message, raw content and absolute paths are never surfaced.
    """
    return _envelope(_staged(STATUS_FAILED, reason, AUTHORIZATION_NOT_APPLICABLE))


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
) -> tuple[str, Any]:
    """Revalidate drift, then mint and immediately guard the internal machine authorization.

    The canonical review hash is recomputed from the trusted bindings and the revalidated MIME
    candidate SHA-256 (matching the existing MD-A2 fetch semantics, where ``inventory_sha256`` is
    the payload hash of the part).  Returns ``(review_hash, capability)``; the opaque capability
    is consumed in-place by the caller and never returned to the API consumer.
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
    return review_hash, authorization


def _extraction_envelope(
    part: Mapping[str, Any], extraction: Mapping[str, Any]
) -> dict[str, Any]:
    """Build the canonical MD-A3 envelope from the bound part and the extraction result.

    Every value is sourced from the revalidated bound part or the canonical extraction output --
    never from a caller claim.
    """
    return {
        "part_locator": str(part.get("part_locator", "")).strip(),
        "filename": str(part.get("filename", "")).strip(),
        "mime_type": str(part.get("mime_type", "")).strip().lower(),
        "source_sha256": str(part.get("sha256", "")).strip().lower(),
        "status": extraction.get("status"),
        "quality": extraction.get("quality"),
        "truncation_reason": extraction.get("truncation_reason"),
        "character_count": extraction.get("character_count", 0),
        "source_character_count": extraction.get("source_character_count"),
        "text": extraction.get("text", ""),
        "error": extraction.get("error"),
    }


def _safe_file_entries(
    validated_handoff: Mapping[str, Any],
    run_id_by_locator: Mapping[str, str],
) -> list[dict[str, Any]]:
    """Project the validated handoff items onto the bounded, safe staged ``files[]`` metadata."""
    entries: list[dict[str, Any]] = []
    for item in validated_handoff.get("items", []):
        part_locator = str(item.get("part_locator", "")).strip()
        completeness = str(item.get("analysis_completeness", "")).strip().lower()
        coverage = COVERAGE_FULL if completeness == "full" else COVERAGE_TRUNCATED
        entries.append(
            {
                "filename": str(item.get("filename", "")).strip(),
                "sha256": str(item.get("source_sha256", "")).strip().lower(),
                "mime_type": str(item.get("mime_type", "")).strip().lower(),
                "chars": int(item.get("char_count", 0)),
                "coverage": coverage,
                "run_id": run_id_by_locator.get(part_locator, ""),
            }
        )
    return entries


def _mail_identity(
    account: str, message_id: str, folder: str, envelope_id: str
) -> dict[str, str]:
    return {
        "account": account,
        "message_id": message_id,
        "folder": folder,
        "envelope_id": envelope_id,
    }


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
    run_id: str | None = None,
    data_dir: Path | None = None,
    workspace_root: str | Path | None = None,
    lease_id: str | None = None,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    """Return the staged ``attachment_evaluation`` and the validated analysis handoff.

    Trusted inputs only: the raw MIME bytes, the message/binding context, the existing body/
    full-read ``decision`` and its optional ``read_escalation``, the effective trusted ``policy``
    plus the minimal trusted fetch/extract control-plane bindings.  Caller-claimed candidates,
    policy/fetch/part statuses, machine receipts, approval receipts, authorization labels, staged
    status/reason, files, materiality, classification or handoff values are not accepted.
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

    base_data_dir = data_dir or resolve_data_dir()

    # Upfront canonical lock guard: no fetch/extract I/O may happen before the invocation owns the
    # workspace lock.  `op_attachment_fetch` retains its own lock/preflight/drift checks as well.
    # A missing or foreign lock is a bounded fail-closed outcome, never a partial result.
    try:
        attachment_fetch.verify_workspace_lock(
            workspace_root=workspace_root,
            lease_id=lease_id,
            conversation_id=conversation_id,
            data_dir=base_data_dir,
        )
    except WorkspaceLockError:
        return _failed_envelope(REASON_LOCK_UNAVAILABLE)

    # One run_id for every attachment of the message, so cumulative count/size quotas cannot be
    # bypassed by spreading parts over separate runs.  When none is supplied the first canonical
    # fetch allocates it and the returned run_id is reused for all subsequent fetches.
    effective_run_id = run_id
    extraction_envelopes: list[dict[str, Any]] = []
    run_id_by_locator: dict[str, str] = {}

    try:
        for part in eligible_parts:
            part_locator = str(part.get("part_locator", "")).strip()
            inventory_sha256 = str(part.get("sha256", "")).strip().lower()

            review_hash, authorization = _mint_and_guard_authorization(
                part,
                policy_revision=policy_revision,
                account=norm_account,
                folder=norm_folder,
                envelope_id=norm_envelope,
                message_id=norm_message_id,
                inspected_parts=inspected_parts,
            )

            # Only after the evaluation-context guard succeeded is the non-authoritative structural
            # snapshot handed to the unchanged fetch.  The capability itself is never exposed.
            fetch_result = op_attachment_fetch(
                candidate=dict(part),
                account=norm_account,
                folder=norm_folder,
                envelope_id=norm_envelope,
                message_id=norm_message_id,
                part_locator=part_locator,
                inventory_sha256=inventory_sha256,
                review_hash=review_hash,
                approval_receipt=authorization.to_dict(),
                run_id=effective_run_id,
                raw_eml=raw_eml,
                data_dir=base_data_dir,
                policy=effective_policy,
                workspace_root=workspace_root,
                lease_id=lease_id,
                conversation_id=conversation_id,
            )
            effective_run_id = str(fetch_result["run_id"])
            run_id_by_locator[part_locator] = effective_run_id

            # Both `fetched` and `already_fetched` proceed identically through the extractor.
            # Office/PDF formats use only this canonical extractor -- no converter/OCR/parser
            # duplicate.
            extraction = extract_attachment_content(
                fetch_result,
                expected_sha256=inventory_sha256,
                data_dir=base_data_dir,
                policy=effective_policy,
                workspace_root=workspace_root,
                lease_id=lease_id,
                conversation_id=conversation_id,
            )
            # Inspect the canonical extraction result rather than treating every partial/unavailable
            # status as an error: only a terminal `extraction_failed` (including a timeout) is a
            # hard failure.  `corrupt_attachment` / `attachment_conversion_unavailable` stay
            # required evidence and flow through the canonical handoff.
            if str(extraction.get("status", "")).strip() == _EXTRACTION_STATUS_FAILED:
                return _failed_envelope(REASON_EXTRACTION_FAILED)
            extraction_envelopes.append(_extraction_envelope(part, extraction))
    # Exact expected exceptions only; programming faults (incl. the trusted-input
    # `AttachmentEvaluationError`/`AttachmentAuthorizationError`) stay fail-loud.  The order is
    # inheritance-sensitive: `MimeDriftError` is matched before its `AttachmentDriftError` family.
    except QuotaExceededError:
        return _failed_envelope(REASON_QUOTA_EXCEEDED)
    except _POLICY_BLOCKED_EXCEPTIONS:
        return _failed_envelope(REASON_POLICY_BLOCKED)
    except WorkspaceLockError:
        return _failed_envelope(REASON_LOCK_UNAVAILABLE)
    except AttachmentDriftError:
        return _failed_envelope(REASON_FETCH_FAILED)
    except _FETCH_FAILED_STORAGE_EXCEPTIONS:
        return _failed_envelope(REASON_FETCH_FAILED)

    mail_identity = _mail_identity(norm_account, norm_message_id, norm_folder, norm_envelope)

    # One handoff from the enriched extraction envelopes and the canonically bound parts, with the
    # normative required-for-decision materiality.  The canonical 15k/30k budgets and their visible
    # truncation markers live in the builder and are never re-implemented here.
    try:
        handoff = build_attachment_analysis_handoff(
            mail_identity=mail_identity,
            attachments=extraction_envelopes,
            decision=decision,
            canonical_parts=eligible_parts,
            default_materiality=MATERIALITY_REQUIRED_FOR_DECISION,
        )
        validated_handoff = validate_attachment_handoff(
            handoff,
            mail_identity=mail_identity,
            decision=decision,
            canonical_parts=eligible_parts,
        )
    except InvalidMaterialityError:
        return _failed_envelope(REASON_HANDOFF_INVALID)
    except AttachmentHandoffError:
        return _failed_envelope(REASON_HANDOFF_INVALID)

    files = _safe_file_entries(validated_handoff, run_id_by_locator)
    handoff_status = validated_handoff.get("status")
    if handoff_status == HANDOFF_STATUS_BLOCKED:
        # A validated blocked required attachment stays required_for_decision and remains
        # ambiguous; it is never downgraded to supplementary.
        staged = _staged(
            STATUS_COMPLETED, REASON_STILL_AMBIGUOUS, AUTHORIZATION_AUTO_EVALUATED, files=files
        )
    elif handoff_status == HANDOFF_STATUS_READY:
        staged = _staged(
            STATUS_COMPLETED, REASON_HANDOFF_READY, AUTHORIZATION_AUTO_EVALUATED, files=files
        )
    else:
        raise AttachmentEvaluationError(
            f"Unexpected validated handoff status {handoff_status!r}."
        )

    return {
        "attachment_evaluation": staged,
        "attachment_analysis_handoff": validated_handoff,
    }


__all__ = [
    "ALLOWED_ATTACHMENT_EVALUATION_AUTHORIZATIONS",
    "ALLOWED_ATTACHMENT_EVALUATION_COVERAGES",
    "ALLOWED_ATTACHMENT_EVALUATION_REASONS",
    "ALLOWED_ATTACHMENT_EVALUATION_STATUSES",
    "AUTHORIZATION_AUTO_EVALUATED",
    "AUTHORIZATION_NOT_APPLICABLE",
    "AttachmentEvaluationError",
    "COVERAGE_FULL",
    "COVERAGE_TRUNCATED",
    "REASON_CLASSIFICATION_CLEAR",
    "REASON_EVALUATION_PENDING",
    "REASON_EXTRACTION_FAILED",
    "REASON_FETCH_FAILED",
    "REASON_HANDOFF_INVALID",
    "REASON_HANDOFF_READY",
    "REASON_LOCK_UNAVAILABLE",
    "REASON_NO_ALLOWED_ATTACHMENTS",
    "REASON_NO_ATTACHMENTS",
    "REASON_POLICY_BLOCKED",
    "REASON_QUOTA_EXCEEDED",
    "REASON_STILL_AMBIGUOUS",
    "STATUS_COMPLETED",
    "STATUS_FAILED",
    "STATUS_NOT_NEEDED",
    "STATUS_SKIPPED",
    "attachment_evaluate",
    "decision_triggers_evaluation",
]
