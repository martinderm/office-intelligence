"""MD-E2 draft-side attachment evaluation and single reclassification (FR-15 / MD-E2-T01).

Narrow orchestration seam that turns the already-tested MD-E1 ``attachment_evaluate``
backend into the default-on ``draft`` behaviour:

1. the existing preview/body/full-read classification always runs first (the caller
   passes the finished manifest items plus the transient effective source emails the
   classifier actually used for each final initial decision);
2. only a still-ambiguous item may acquire raw MIME and call ``attachment_evaluate``;
3. a validated ``ready`` handoff is presented **once** to the existing classifier rules
   as a distinct ``untrusted_external`` input;
4. the item receives exactly one final, bounded ``attachment_evaluation``.

This module re-implements **no** fetch, MIME, policy, quota, extraction or hash
validation: those stay authoritative in MD-E1 and its canonical seams.  It also never
persists the raw handoff content as durable extraction output -- only the bounded
``files[]`` metadata survives.

``used_for_classification: true`` plus a 64-hex ``classifier_revision`` is set **only**
when one actual second classification consumed the validated handoff and produced a
successful, unambiguous result.  Every other terminal outcome stays ``false``/``null``.
The revision is content-addressed over the active classification rules and the sorted
exact attachment hashes actually consumed; no caller, mail or manifest value can set it.

MD-E2-T02 hardens the seam into a fail-closed production boundary: every bounded MD-E1
no-op/failure reason is preserved item-locally with Review/``INBOX`` fallback, a ready
handoff is revalidated with the canonical ``validate_attachment_handoff`` against the
trusted identity, the pre-reclassification decision and the canonical attachment
inventory before any classification, a continued ambiguity keeps the MD-E1
``auto_evaluated`` provenance and safe ``files[]``, and the code-level classifier rule
module is bound into the revision.  Genuinely unexpected backend/programmer contract
violations raise :class:`AttachmentReclassificationContractError` fail-loud instead of
being silently relabelled as ``fetch_failed`` or ``still_ambiguous``.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .attachment_evaluation import (
    ALLOWED_ATTACHMENT_EVALUATION_AUTHORIZATIONS,
    ALLOWED_ATTACHMENT_EVALUATION_COVERAGES,
    ALLOWED_ATTACHMENT_EVALUATION_REASONS,
    ALLOWED_ATTACHMENT_EVALUATION_STATUSES,
    AUTHORIZATION_AUTO_EVALUATED,
    AUTHORIZATION_NOT_APPLICABLE,
    REASON_CLASSIFICATION_CLEAR,
    REASON_FETCH_FAILED,
    REASON_HANDOFF_READY,
    REASON_HANDOFF_INVALID,
    REASON_STILL_AMBIGUOUS,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_NOT_NEEDED,
    STATUS_SKIPPED,
)
from .attachment_fetch import is_valid_run_id
from .attachment_handoff import (
    HANDOFF_STATUS_READY,
    AttachmentHandoffError,
    InvalidMaterialityError,
    validate_attachment_handoff,
)
from .common import normalize_message_id
from .himalaya import HimalayaInvocationError

_SHA256_LOWER = re.compile(r"^[0-9a-f]{64}$")

# ==============================================================================
# Bounded MD-E2 vocabulary
# ==============================================================================

#: MD-E2 installs this terminal reason when the operator disabled evaluation.  MD-E1 never
#: emits it, so it is added to (not merged into) the MD-E1 runtime reason set.
REASON_EVALUATION_DISABLED = "evaluation_disabled"

#: The bounded reason set a *final* DraftManifest ``attachment_evaluation`` may carry.
ALLOWED_DRAFT_ATTACHMENT_EVALUATION_REASONS = frozenset(
    set(ALLOWED_ATTACHMENT_EVALUATION_REASONS) | {REASON_EVALUATION_DISABLED}
)

#: Version tag for the active canonical classifier rules bound into ``classifier_revision``.
CLASSIFIER_RULES_VERSION = "md-e2-classifier-rules-v1"

_CATALOG_SOURCES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("projects", ("memory", "references", "projects", "projects.json")),
    ("topics", ("memory", "references", "topics", "topics.json")),
)

#: The active, code-level classifier rule modules whose normalized ASTs are
#: content-addressed into ``classifier_revision``.  Resolved next to this module so no
#: host path is ever hashed.  MD-M1-T01 binds the facade plus the canonical date and
#: ambiguity owners; MD-M1-T02 appends the canonical project-matching owner, and
#: MD-M1-T03 appends the canonical topic-matching owner to complete the five-module set.
_CLASSIFIER_MODULE_PATHS: tuple[Path, ...] = (
    Path(__file__).resolve().parent / "classifier.py",
    Path(__file__).resolve().parent / "matching" / "ambiguity.py",
    Path(__file__).resolve().parent / "matching" / "date_parser.py",
    Path(__file__).resolve().parent / "matching" / "project_matching.py",
    Path(__file__).resolve().parent / "matching" / "topic_matching.py",
)

#: Fields the single reclassification may replace on the draft item.  All are produced by
#: the existing classifier rules; MD-E2 never invents a target.
_RECLASSIFICATION_FIELDS = ("decision", "action", "notes", "evidence", "synthesis_targets")

#: Deterministic, PII-free per-message evaluation run-id namespace material.
_EVAL_RUN_ID_PREFIX = "eval"
_EVAL_RUN_ID_DIGEST_CHARS = 32
_EVAL_RUN_ID_MAX_LENGTH = 100

#: The classifier-level status for a mail whose MIME inventory could not be verified.
#: Such an item is already a bounded, fail-closed Review outcome: it carries no trusted
#: inventory, so MD-E2 must not run a second canonical export for it in the same run.
ATTACHMENT_INVENTORY_UNAVAILABLE = "attachment_inventory_unavailable"


# ==============================================================================
# Bounded contract exceptions
# ==============================================================================

class AttachmentReclassificationContractError(ValueError):
    """Raised when an MD-E1 backend/programmer contract is violated.

    This is a bounded, fail-loud error type: a non-mapping backend result, a malformed
    staged object, an unexpected status/reason/authorization/files vocabulary or a
    non-mapping reclassifier result never silently degrades into a bounded policy outcome
    such as ``fetch_failed`` or ``still_ambiguous``.  The message never carries raw
    content, an absolute path, an exception message or a capability.
    """


# ==============================================================================
# Deterministic per-message evaluation run-id
# ==============================================================================

def derive_evaluation_run_id(
    identity: Mapping[str, Any], *, namespace: str | None = None
) -> str:
    """Derive a deterministic, PII-free evaluation run-id from trusted mail identity.

    The run-id is stable for one immutable message so a repeated ``draft`` invocation
    reaches the canonical MD-E1 ``already_fetched`` path instead of allocating a fresh
    quarantine run and fetching the same attachment again.  It binds account, folder,
    envelope and normalized message id by hash, so distinct messages never collide and no
    address is exposed.  A caller-supplied trusted ``namespace`` becomes a base prefix and
    still isolates every message deterministically.
    """
    normalized = {
        "account": str(identity.get("account") or "").strip(),
        "folder": str(identity.get("folder") or "").strip(),
        "envelope_id": str(identity.get("envelope_id") or "").strip(),
        "message_id": normalize_message_id(str(identity.get("message_id") or "")),
    }
    payload = json.dumps(
        normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    digest = hashlib.sha256(
        f"md-e2-evaluation-run-id:{payload}".encode("utf-8"), usedforsecurity=False
    ).hexdigest()[:_EVAL_RUN_ID_DIGEST_CHARS]

    base = ""
    if namespace is not None and str(namespace).strip():
        sanitized = re.sub(r"[^A-Za-z0-9_-]", "", str(namespace).strip())
        base = sanitized[: _EVAL_RUN_ID_MAX_LENGTH - len(digest) - 1]
    candidate = f"{base}_{digest}" if base else f"{_EVAL_RUN_ID_PREFIX}_{digest}"
    if is_valid_run_id(candidate):
        return candidate
    return f"{_EVAL_RUN_ID_PREFIX}_{digest}"


# ==============================================================================
# Content-addressed classifier revision
# ==============================================================================

def _classifier_code_digest() -> str:
    """Return a normalized-AST SHA-256 over the ordered active classifier rule modules.

    Each bound module's normalized AST (``ast.dump``) is folded into one digest in the
    deterministic ``_CLASSIFIER_MODULE_PATHS`` order, so the result is insensitive to
    formatting, comments and host paths but moves on any code-level rule or constant
    change in any bound module.  It never hashes bytecode, an absolute path, a runtime
    object repr or raw source formatting.  A missing, unreadable or unparseable bound
    module fails closed with no partial digest.
    """
    digest = hashlib.sha256(usedforsecurity=False)
    for module_path in _CLASSIFIER_MODULE_PATHS:
        try:
            source = module_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:  # pragma: no cover - deployment fault
            raise AttachmentReclassificationContractError(
                "A canonical classifier rule module is unavailable."
            ) from exc
        try:
            tree = ast.parse(source, filename=module_path.name)
        except SyntaxError as exc:  # pragma: no cover - deployment fault
            raise AttachmentReclassificationContractError(
                "A canonical classifier rule module is not parseable."
            ) from exc
        normalized = ast.dump(tree, annotate_fields=True, include_attributes=False)
        digest.update(normalized.encode("utf-8"))
    return digest.hexdigest()


def classifier_rules_fingerprint(workspace_root: str | Path) -> str:
    """Return a deterministic SHA-256 over the active canonical classification rules.

    The rules that decide ``kind``/``id``/catalog-bound subdecisions are driven both by the
    code-level classifier rule module and by the workspace project/topic catalogs.  The
    normalized classifier-module AST, the canonical catalog JSON and a fixed rules version
    tag are bound together, so formatting-only edits cannot move the fingerprint but any
    rule-relevant code or catalog content change does.
    """
    material: dict[str, Any] = {
        "version": CLASSIFIER_RULES_VERSION,
        "classifier_code_sha256": _classifier_code_digest(),
        "catalogs": {},
    }
    base = Path(workspace_root)
    for name, parts in _CATALOG_SOURCES:
        catalog_path = base.joinpath(*parts)
        canonical: Any = None
        if catalog_path.is_file():
            try:
                canonical = json.loads(catalog_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeDecodeError):
                canonical = None
        material["catalogs"][name] = canonical
    payload = json.dumps(
        material, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(payload.encode("utf-8"), usedforsecurity=False).hexdigest()


def compute_classifier_revision(
    rules_fingerprint: str, used_input_hashes: Iterable[str]
) -> str:
    """Return the 64-hex revision binding the rules and the sorted hashes actually consumed.

    Input ordering never influences the result because the hashes are de-duplicated and
    sorted.  A changed rule fingerprint or a changed consumed hash changes the revision.
    """
    normalized = sorted(
        {str(value).strip().lower() for value in used_input_hashes if str(value).strip()}
    )
    payload = json.dumps(
        {"rules": str(rules_fingerprint), "inputs": normalized},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8"), usedforsecurity=False).hexdigest()


# ==============================================================================
# Bounded evaluation objects
# ==============================================================================

def _terminal(status: str, reason: str, authorization: str) -> dict[str, Any]:
    """Build a terminal, non-classifying ``attachment_evaluation`` object."""
    return {
        "status": status,
        "reason": reason,
        "authorization": authorization,
        "files": [],
        "used_for_classification": False,
        "classifier_revision": None,
    }


def _bounded_staged(staged: Mapping[str, Any]) -> dict[str, Any]:
    """Reduce an MD-E1 staged/failed envelope to the final bounded six-field object.

    The staged ``used_for_classification``/``classifier_revision`` can never leak a
    ``true``/revision pair here; only :func:`_successful_evaluation` sets those.
    """
    return {
        "status": str(staged.get("status") or STATUS_FAILED),
        "reason": str(staged.get("reason") or REASON_FETCH_FAILED),
        "authorization": str(staged.get("authorization") or AUTHORIZATION_NOT_APPLICABLE),
        "files": list(staged.get("files") or []),
        "used_for_classification": False,
        "classifier_revision": None,
    }


def _successful_evaluation(staged: Mapping[str, Any], revision: str) -> dict[str, Any]:
    """Build the one allowed ``used_for_classification: true`` final object."""
    return {
        "status": STATUS_COMPLETED,
        "reason": REASON_CLASSIFICATION_CLEAR,
        "authorization": AUTHORIZATION_AUTO_EVALUATED,
        "files": list(staged.get("files") or []),
        "used_for_classification": True,
        "classifier_revision": revision,
    }


def _still_ambiguous(staged: Mapping[str, Any]) -> dict[str, Any]:
    """Preserve the MD-E1 authorization and safe ``files[]`` for a continued ambiguity.

    The second classification ran exactly once and stayed ambiguous, so the item keeps the
    MD-E1 ``auto_evaluated`` provenance and the canonical, bounded ``files[]`` (including
    coverage/truncation) while remaining ``false``/``null``.
    """
    return {
        "status": STATUS_COMPLETED,
        "reason": REASON_STILL_AMBIGUOUS,
        "authorization": str(
            staged.get("authorization") or AUTHORIZATION_NOT_APPLICABLE
        ),
        "files": list(staged.get("files") or []),
        "used_for_classification": False,
        "classifier_revision": None,
    }


def _validate_staged_vocabulary(
    status: str, reason: str, authorization: str, files: Any
) -> None:
    """Enforce the final bounded status/reason/authorization/files vocabulary.

    A malformed staged object is a backend-contract violation and fails loud instead of
    being silently relabelled as a bounded policy outcome.  Long-lived ``files[]`` entries
    remain the canonical safe MD-E1 metadata records only.
    """
    if status not in ALLOWED_ATTACHMENT_EVALUATION_STATUSES:
        raise AttachmentReclassificationContractError(
            "The attachment_evaluation status is outside the bounded vocabulary."
        )
    if reason not in ALLOWED_DRAFT_ATTACHMENT_EVALUATION_REASONS:
        raise AttachmentReclassificationContractError(
            "The attachment_evaluation reason is outside the bounded vocabulary."
        )
    if authorization not in ALLOWED_ATTACHMENT_EVALUATION_AUTHORIZATIONS:
        raise AttachmentReclassificationContractError(
            "The attachment_evaluation authorization is outside the bounded vocabulary."
        )
    if not isinstance(files, list):
        raise AttachmentReclassificationContractError(
            "The attachment_evaluation files must be a list of safe metadata records."
        )
    for entry in files:
        if not isinstance(entry, Mapping):
            raise AttachmentReclassificationContractError(
                "Every attachment_evaluation files entry must be a safe metadata record."
            )
        sha256 = str(entry.get("sha256") or "").strip()
        if not _SHA256_LOWER.fullmatch(sha256):
            raise AttachmentReclassificationContractError(
                "Every attachment_evaluation files entry must carry a lowercase 64-hex sha256."
            )
        coverage = entry.get("coverage")
        if coverage is not None and coverage not in ALLOWED_ATTACHMENT_EVALUATION_COVERAGES:
            raise AttachmentReclassificationContractError(
                "The attachment_evaluation files coverage is outside the bounded vocabulary."
            )


def _validated_consumed_hashes(validated: Mapping[str, Any]) -> list[str]:
    """Collect the exact validated MD-E1-bound attachment hashes actually consumed."""
    hashes: list[str] = []
    items = validated.get("items")
    if isinstance(items, Sequence):
        for entry in items:
            if not isinstance(entry, Mapping):
                continue
            value = entry.get("source_sha256") or entry.get("sha256")
            text = str(value or "").strip().lower()
            if text:
                hashes.append(text)
    return hashes


def _staged_file_hashes(staged: Mapping[str, Any]) -> list[str]:
    """Collect the bounded staged ``files[]`` SHA-256 values for binding cross-checks."""
    hashes: list[str] = []
    files = staged.get("files")
    if isinstance(files, Sequence):
        for entry in files:
            if not isinstance(entry, Mapping):
                continue
            hashes.append(str(entry.get("sha256") or "").strip().lower())
    return hashes


def _consumed_hashes_are_bound(consumed: Sequence[str], staged: Mapping[str, Any]) -> bool:
    """True only when the consumed hashes are valid, unique and match the staged files.

    The revision may bind only the exact hashes of the validated handoff items that were
    actually passed, so missing, duplicate, invalid or staged-mismatched hashes stop as a
    binding failure before any reclassification or revision.
    """
    if not consumed:
        return False
    if any(not _SHA256_LOWER.fullmatch(value) for value in consumed):
        return False
    if len(set(consumed)) != len(consumed):
        return False
    return sorted(consumed) == sorted(_staged_file_hashes(staged))


def _force_review_inbox(item: dict[str, Any], reason: str) -> None:
    """Force a fail-closed / ambiguous item to stay in Review/``INBOX`` at low confidence."""
    item["action"] = {"type": "keep_in_folder", "target_folder": "INBOX"}
    decision = item.get("decision")
    if not isinstance(decision, dict):
        decision = {}
        item["decision"] = decision
    decision["review_required"] = True
    decision["review_reason"] = str(reason)
    decision["confidence"] = "low"


def _inventory_is_unavailable(item: Mapping[str, Any]) -> bool:
    """True when the item's MIME inventory could not be verified (fail-closed review item).

    The classifier marks such an item with ``attachment_inventory_unavailable`` and/or an
    ``attachment_error``.  The state is item-local and needs no second MIME export.
    """
    if str(item.get("attachment_status") or "").strip() == ATTACHMENT_INVENTORY_UNAVAILABLE:
        return True
    return bool(item.get("attachment_error"))


def discard_inventory_contradicting_evaluations(
    items: Sequence[dict[str, Any]],
) -> None:
    """Discard a completed MD-E2 install that contradicts an unavailable inventory in place.

    ``attachments[]``/``attachment_status``/``attachment_error`` and
    ``attachment_evaluation``/``files[]`` must stay mutually consistent: a fail-closed item
    (``attachment_inventory_unavailable`` or an ``attachment_error``) must never coexist with
    a completed ``attachment_evaluation`` that carries a non-empty ``files[]``.  The MD-E2
    trigger gate in :func:`_evaluate_item` already prevents the contradiction from arising;
    this post-install invariant keeps the field contract true for every caller that composes
    a manifest from the installed items.  The result stays a bounded, item-local terminal
    (never a contract error), so no batch is ever aborted by a policy inconsistency.
    """
    for item in items:
        if not isinstance(item, dict) or not _inventory_is_unavailable(item):
            continue
        evaluation = item.get("attachment_evaluation")
        if not isinstance(evaluation, Mapping):
            continue
        if (
            str(evaluation.get("status") or "") == STATUS_COMPLETED
            and list(evaluation.get("files") or [])
        ):
            item["attachment_evaluation"] = _terminal(
                STATUS_FAILED, REASON_FETCH_FAILED, AUTHORIZATION_NOT_APPLICABLE
            )


# ==============================================================================
# Item-source pairing
# ==============================================================================

def _index_sources(
    sources: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any] | None]:
    """Identity-index the transient effective sources; a duplicate identity poisons the key.

    The classifier reports one effective source per item (preview email, or the full-read
    email when it re-read the envelope).  Pairing is by envelope identity, never by position,
    and a duplicate identity fails closed instead of silently attaching the wrong source.
    """
    index: dict[str, dict[str, Any] | None] = {}
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        envelope_id = str(source.get("envelope_id") or "").strip()
        if not envelope_id:
            continue
        index[envelope_id] = None if envelope_id in index else dict(source)
    return index


def _matching_source(
    item: Mapping[str, Any], index: Mapping[str, dict[str, Any] | None]
) -> dict[str, Any] | None:
    """Return the identity-matched effective source, or ``None`` (missing/duplicate/mismatch)."""
    envelope_id = str(item.get("envelope_id") or "").strip()
    if not envelope_id:
        return None
    matched = index.get(envelope_id)
    if matched is None:
        return None
    item_folder = str(item.get("source_folder") or "INBOX").strip() or "INBOX"
    source_folder = str(matched.get("folder") or "INBOX").strip() or "INBOX"
    if item_folder != source_folder:
        return None
    return dict(matched)


def _adopt_reclassification(item: dict[str, Any], replacement: Mapping[str, Any]) -> None:
    """Adopt only the existing-classifier-derived decision/action fields."""
    for field in _RECLASSIFICATION_FIELDS:
        if field in replacement:
            item[field] = replacement[field]


# ==============================================================================
# Per-item evaluation
# ==============================================================================

def _evaluate_item(
    item: dict[str, Any],
    source_index: Mapping[str, dict[str, Any] | None],
    *,
    evaluate_attachments: bool,
    evaluate: Callable[..., dict[str, Any]],
    reclassify: Callable[..., dict[str, Any]],
    read_raw_mime: Callable[..., bytes],
    triggers_evaluation: Callable[..., bool],
    rules_fingerprint: str,
    workspace_root: Path,
    data_dir: Path,
    account: str | None,
    policy: Mapping[str, Any] | None,
    run_id: str | None,
    lease_id: str | None,
    conversation_id: str | None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Resolve exactly one final ``attachment_evaluation`` and an optional reclassification."""
    if not evaluate_attachments:
        return (
            _terminal(STATUS_SKIPPED, REASON_EVALUATION_DISABLED, AUTHORIZATION_NOT_APPLICABLE),
            None,
        )

    # A classifier-level fail-closed item whose MIME inventory could not be verified already
    # carries a bounded Review decision and no trusted inventory.  A second canonical export
    # inside the same run would only repeat the failing I/O, so the trigger gate short-circuits
    # to a bounded, non-completed terminal and leaves the item's review decision untouched
    # (exactly one canonical MIME export per item per run).
    if _inventory_is_unavailable(item):
        return (
            _terminal(STATUS_FAILED, REASON_FETCH_FAILED, AUTHORIZATION_NOT_APPLICABLE),
            None,
        )

    decision = item.get("decision")
    if not isinstance(decision, Mapping):
        decision = {}
    if not triggers_evaluation(decision):
        return (
            _terminal(STATUS_NOT_NEEDED, REASON_CLASSIFICATION_CLEAR, AUTHORIZATION_NOT_APPLICABLE),
            None,
        )

    email = _matching_source(item, source_index)
    if email is None:
        # An identity/source pairing failure is a binding failure, never a successful no-op.
        _force_review_inbox(item, REASON_HANDOFF_INVALID)
        return _terminal(STATUS_FAILED, REASON_HANDOFF_INVALID, AUTHORIZATION_NOT_APPLICABLE), None

    envelope_id = str(item.get("envelope_id") or email.get("envelope_id") or "").strip()
    message_id = str(item.get("message_id") or email.get("message_id") or "").strip()
    folder = str(email.get("folder") or item.get("source_folder") or "INBOX").strip() or "INBOX"
    normalized_account = str(account or "").strip()
    if not envelope_id or not message_id or not normalized_account:
        _force_review_inbox(item, REASON_HANDOFF_INVALID)
        return _terminal(STATUS_FAILED, REASON_HANDOFF_INVALID, AUTHORIZATION_NOT_APPLICABLE), None

    try:
        raw_eml = read_raw_mime(envelope_id, folder=folder, account=account)
    # Only the canonical raw-MIME acquisition failures map to the bounded item-local
    # `fetch_failed` outcome; unexpected programmer errors stay fail-loud.
    except (HimalayaInvocationError, OSError, RuntimeError):
        _force_review_inbox(item, REASON_FETCH_FAILED)
        return _terminal(STATUS_FAILED, REASON_FETCH_FAILED, AUTHORIZATION_NOT_APPLICABLE), None

    # A deterministic, PII-free per-message run-id lets a repeated invocation reuse the
    # canonical MD-E1 `already_fetched` path instead of fetching the same immutable
    # attachment again.  A caller-supplied run_id is only a trusted base namespace.
    effective_run_id = derive_evaluation_run_id(
        {
            "account": normalized_account,
            "folder": folder,
            "envelope_id": envelope_id,
            "message_id": message_id,
        },
        namespace=run_id,
    )

    result = evaluate(
        raw_eml=raw_eml,
        account=normalized_account,
        folder=folder,
        envelope_id=envelope_id,
        message_id=message_id,
        decision=decision,
        policy=policy,
        run_id=effective_run_id,
        data_dir=data_dir,
        workspace_root=workspace_root,
        lease_id=lease_id,
        conversation_id=conversation_id,
    )
    if not isinstance(result, Mapping):
        raise AttachmentReclassificationContractError(
            "attachment_evaluate returned a non-mapping result."
        )

    staged = result.get("attachment_evaluation")
    if not isinstance(staged, Mapping):
        raise AttachmentReclassificationContractError(
            "attachment_evaluate returned a non-mapping attachment_evaluation."
        )

    staged_status = str(staged.get("status") or "")
    staged_reason = str(staged.get("reason") or "")
    staged_authorization = str(staged.get("authorization") or "")
    _validate_staged_vocabulary(
        staged_status, staged_reason, staged_authorization, staged.get("files")
    )

    # A staged ready result and a ready handoff sibling are mutually coherent only as a
    # pair; any other combination is an impossible backend contract and fails loud before
    # reclassification or write.  In particular a ready stage must never be silently
    # installed when the handoff is missing, non-mapping or not `ready`.
    handoff = result.get("attachment_analysis_handoff")
    staged_is_ready = (
        staged_status == STATUS_COMPLETED
        and staged_reason == REASON_HANDOFF_READY
        and staged_authorization == AUTHORIZATION_AUTO_EVALUATED
    )
    handoff_is_ready = (
        isinstance(handoff, Mapping)
        and str(handoff.get("status") or "") == HANDOFF_STATUS_READY
    )
    if staged_is_ready and not handoff_is_ready:
        raise AttachmentReclassificationContractError(
            "A finished handoff_ready attachment_evaluation requires exactly one ready "
            "attachment_analysis_handoff."
        )
    if handoff_is_ready and not staged_is_ready:
        raise AttachmentReclassificationContractError(
            "A ready attachment_analysis_handoff requires a finished handoff_ready "
            "attachment_evaluation."
        )

    if not staged_is_ready:
        # not_needed / blocked / failed stay bounded and never classify.  A failed outcome
        # and a continued ambiguity are forced back to Review/INBOX at low confidence.
        if staged_status == STATUS_FAILED or (
            staged_status == STATUS_COMPLETED and staged_reason == REASON_STILL_AMBIGUOUS
        ):
            _force_review_inbox(item, staged_reason)
        return _bounded_staged(staged), None

    # Revalidate the ready handoff against the trusted identity, the pre-reclassification
    # decision and the canonical item/source attachment inventory before any classification.
    # A manipulated, missing, duplicate or hash-mismatched handoff stops here with no call.
    mail_identity = {
        "account": normalized_account,
        "message_id": message_id,
        "folder": folder,
        "envelope_id": envelope_id,
    }
    try:
        validated = validate_attachment_handoff(
            handoff,
            mail_identity=mail_identity,
            decision=decision,
            canonical_parts=handoff.get("canonical_parts"),
        )
    except (InvalidMaterialityError, AttachmentHandoffError):
        _force_review_inbox(item, REASON_HANDOFF_INVALID)
        return _terminal(STATUS_FAILED, REASON_HANDOFF_INVALID, AUTHORIZATION_NOT_APPLICABLE), None

    consumed_hashes = _validated_consumed_hashes(validated)
    if not _consumed_hashes_are_bound(consumed_hashes, staged):
        _force_review_inbox(item, REASON_HANDOFF_INVALID)
        return _terminal(STATUS_FAILED, REASON_HANDOFF_INVALID, AUTHORIZATION_NOT_APPLICABLE), None

    # Exactly one second invocation of the existing classifier rules, consuming the
    # validated handoff as a distinct, encapsulated untrusted_external input.
    replacement = reclassify(
        email,
        workspace_root=workspace_root,
        account=account,
        untrusted_external_text=str(validated.get("prompt_content") or ""),
    )
    if not isinstance(replacement, Mapping):
        raise AttachmentReclassificationContractError(
            "The reclassifier returned a non-mapping result."
        )
    new_decision = replacement.get("decision")
    if not isinstance(new_decision, Mapping):
        raise AttachmentReclassificationContractError(
            "The reclassifier returned a malformed decision."
        )
    if triggers_evaluation(new_decision):
        _force_review_inbox(item, REASON_STILL_AMBIGUOUS)
        return _still_ambiguous(staged), None

    revision = compute_classifier_revision(rules_fingerprint, consumed_hashes)
    return _successful_evaluation(staged, revision), dict(replacement)


# ==============================================================================
# Public entry point
# ==============================================================================

def install_draft_attachment_evaluations(
    items: Sequence[dict[str, Any]],
    sources: Sequence[Mapping[str, Any]],
    *,
    evaluate_attachments: bool,
    workspace_root: str | Path,
    data_dir: Path,
    account: str | None,
    evaluate: Callable[..., dict[str, Any]],
    reclassify: Callable[..., dict[str, Any]],
    read_raw_mime: Callable[..., bytes],
    triggers_evaluation: Callable[..., bool],
    policy: Mapping[str, Any] | None = None,
    run_id: str | None = None,
    lease_id: str | None = None,
    conversation_id: str | None = None,
) -> None:
    """Install exactly one final ``attachment_evaluation`` on every draft item in place.

    ``sources`` is the transient, order-preserving list of effective source emails the
    classifier reported for each item's final initial decision.  Only an approved (ambiguous)
    item may fetch raw MIME and call ``attachment_evaluate``; a clear item performs no raw
    fetch, evaluation or reclassification.

    Every bounded MD-E1 outcome is installed item-locally and never aborts the batch.  A
    genuinely unexpected backend/programmer contract violation instead raises the bounded
    :class:`AttachmentReclassificationContractError` fail-loud rather than being relabelled
    as an expected policy outcome.  ``run_id`` is a trusted base namespace only; the effective
    run-id is always derived deterministically per message.
    """
    workspace = Path(workspace_root)
    rules_fingerprint = classifier_rules_fingerprint(workspace)
    source_index = _index_sources(sources)
    for item in items:
        if not isinstance(item, dict):
            continue
        evaluation, replacement = _evaluate_item(
            item,
            source_index,
            evaluate_attachments=evaluate_attachments,
            evaluate=evaluate,
            reclassify=reclassify,
            read_raw_mime=read_raw_mime,
            triggers_evaluation=triggers_evaluation,
            rules_fingerprint=rules_fingerprint,
            workspace_root=workspace,
            data_dir=data_dir,
            account=account,
            policy=policy,
            run_id=run_id,
            lease_id=lease_id,
            conversation_id=conversation_id,
        )
        item["attachment_evaluation"] = evaluation
        if replacement is not None:
            _adopt_reclassification(item, replacement)


__all__ = [
    "ALLOWED_DRAFT_ATTACHMENT_EVALUATION_REASONS",
    "ATTACHMENT_INVENTORY_UNAVAILABLE",
    "AttachmentReclassificationContractError",
    "CLASSIFIER_RULES_VERSION",
    "REASON_EVALUATION_DISABLED",
    "classifier_rules_fingerprint",
    "compute_classifier_revision",
    "derive_evaluation_run_id",
    "discard_inventory_contradicting_evaluations",
    "install_draft_attachment_evaluations",
]
