"""Deterministic script-based attachment disposition log and safe cleanup engine (MD-Q3).

Provides:
- Append-only versioned logging for `data/mail-desk/attachment-disposition-log.jsonl`.
- Verifiable Receipt contracts with canonical request-hash binding (no raw 64-hex authorization).
- Persisted, hash-bound Apply-/Recovery-Journal (`attachment-discard-journal.json`) with strict state transitions.
- Read-only reporting classifying quarantined attachments into `eligible`, `protected`, and `invalid`.
- Exactly-one-attachment Discard-Apply engine enforcing 10 pre-unlink conditions under workspace lock.
- Atomic `.quarantine-inventory.json` updates via sibling temp file + replace with fail-closed error propagation.
- FR-09 promotion link without duplicate promotion engine or remote cloud-sync.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePath, PurePosixPath
import re
import sys
import uuid
from typing import Any, Mapping

from core.common import normalize_message_id, resolve_data_dir, utc_now_iso
from core.attachment_policy import sanitize_attachment_filename
from core.attachment_fetch import (
    INVENTORY_FILENAME,
    SymlinkEscapeError,
    QuarantineInventoryError,
    check_quarantine_path_security,
    is_valid_run_id,
    validate_attachment_filename,
    verify_quarantine_attachment_artifact,
    verify_workspace_lock,
    _load_quarantine_inventory,
    _validate_inventory_schema,
    _QuarantineInventoryLock,
)
from core.attachment_quarantine_index import (
    INDEX_FILENAME,
    SCHEMA_VERSION as QUARANTINE_SCHEMA_VERSION,
    SHA256_HEX_REGEX,
    RFC3339_REGEX,
    QuarantineIndexError,
    WorkspaceLockRequiredError,
    AttachmentIndexDriftError,
    AttachmentIndexSchemaError,
    ForbiddenContentError,
    PhysicalVerificationError,
    _find_forbidden_content_keys,
    canonical_index_entry_sha256,
    load_quarantine_index,
    remove_quarantine_entry,
    resolve_quarantine_index_path,
    save_quarantine_index_atomic,
    update_quarantine_entry_disposition_ref,
    validate_quarantine_index_entry,
    verify_quarantine_workspace_lock,
)
from core.attachment_authorization import (
    CONTEXT_APPLY,
    CONTEXT_DISPOSITION,
    guard_context_authorization,
)


# ==============================================================================
# Exceptions
# ==============================================================================

class DispositionError(ValueError):
    """Base exception for all attachment disposition log and cleanup errors."""


class DispositionSchemaError(DispositionError):
    """Raised when a disposition entry or log line violates Schema 1 or contains unknown/contradictory fields."""


class DispositionDriftError(DispositionError):
    """Raised when a decision drifts in identity, index entry hash, or content."""


class DispositionLockRequiredError(DispositionError):
    """Raised when disposition mutation or cleanup is attempted without a verified, owned workspace lock."""


class DispositionApplyError(DispositionError):
    """Raised when discard apply preconditions fail or partial recovery is required."""


class ReceiptError(DispositionError):
    """Base exception for authorization receipt errors."""


class ReceiptMissingError(ReceiptError):
    """Raised when a required approval or apply receipt is missing or empty."""


class ReceiptMalformedError(ReceiptError):
    """Raised when a receipt is not a dictionary or misses required contract fields."""


class ReceiptDriftError(ReceiptError):
    """Raised when a receipt's request_hash does not match the computed canonical request hash."""


class RecoveryEvidenceMissingError(DispositionApplyError):
    """Raised when a target file is missing from disk and lacks matching file_deleted recovery journal evidence."""


class RecoveryJournalCorruptedError(DispositionApplyError):
    """Raised when the discard recovery journal is structurally invalid or unreadable."""


class InventoryUpdateError(DispositionApplyError):
    """Raised when updating .quarantine-inventory.json fails."""


# ==============================================================================
# Constants & Enums
# ==============================================================================

DISPOSITION_LOG_FILENAME = "attachment-disposition-log.jsonl"
DISCARD_JOURNAL_FILENAME = "attachment-discard-journal.json"

DECISION_RETAIN = "retain"
DECISION_DISCARD = "discard"
DECISION_PROMOTE = "promote"

ALLOWED_DECISIONS = {DECISION_RETAIN, DECISION_DISCARD, DECISION_PROMOTE}

STATUS_ELIGIBLE = "eligible"
STATUS_PROTECTED = "protected"
STATUS_INVALID = "invalid"

ALLOWED_REPORT_STATUSES = {STATUS_ELIGIBLE, STATUS_PROTECTED, STATUS_INVALID}

RATIONALE_MAX_LENGTH = 500
PROMOTION_ID_MAX_LENGTH = 128
ALLOWED_PROMOTION_STATUSES = {"pending", "promoted", "failed", "rejected"}

RECEIPT_REQUIRED_KEYS = {"receipt_id", "request_hash", "approved_at", "approved_by"}

JOURNAL_STATE_PREPARED = "prepared"
JOURNAL_STATE_FILE_DELETED = "file_deleted"
JOURNAL_STATE_INVENTORY_UPDATED = "inventory_updated"
JOURNAL_STATE_INDEX_UPDATED = "index_updated"
JOURNAL_STATE_COMPLETED = "completed"
JOURNAL_STATE_FAILED = "failed"

ALLOWED_JOURNAL_STATES = {
    JOURNAL_STATE_PREPARED,
    JOURNAL_STATE_FILE_DELETED,
    JOURNAL_STATE_INVENTORY_UPDATED,
    JOURNAL_STATE_INDEX_UPDATED,
    JOURNAL_STATE_COMPLETED,
    JOURNAL_STATE_FAILED,
}

STATE_ORDER = {
    JOURNAL_STATE_PREPARED: 1,
    JOURNAL_STATE_FILE_DELETED: 2,
    JOURNAL_STATE_INVENTORY_UPDATED: 3,
    JOURNAL_STATE_INDEX_UPDATED: 4,
    JOURNAL_STATE_COMPLETED: 5,
}

DISCARD_JOURNAL_ENTRY_ALLOWED_KEYS = frozenset({
    "journal_entry_id",
    "attachment_id",
    "decision_id",
    "apply_receipt_hash",
    "apply_request_hash",
    "previous_index_entry_sha256",
    "quarantine_path",
    "sha256",
    "size_bytes",
    "run_id",
    "state",
    "last_successful_state",
    "status",
    "created_at",
    "updated_at",
    "history",
    "failure_stage",
    "error",
})

DISCARD_JOURNAL_HISTORY_ALLOWED_KEYS = frozenset({
    "state",
    "status",
    "timestamp",
    "transition",
    "stage",
    "error",
})

ALLOWED_DISPOSITION_ENTRY_FIELDS = {
    "decision_id",
    "attachment_id",
    "index_entry_sha256",
    "decision",
    "timestamp",
    "human_receipt_hash",
    "rationale",
    "review_after",
    "candidate_review_hash",
    "promotion_id",
    "promotion_status",
}

CANONICAL_DISPOSITION_COMMON_FIELDS = (
    "decision_id",
    "attachment_id",
    "index_entry_sha256",
    "decision",
    "timestamp",
    "human_receipt_hash",
)


# ==============================================================================
# Path Resolution
# ==============================================================================

def resolve_disposition_log_path(
    log_path: Path | str | None = None,
    data_dir: Path | str | None = None,
    workspace_root: Path | str | None = None,
) -> Path:
    """Resolve absolute path to attachment-disposition-log.jsonl."""
    if log_path is not None:
        return Path(log_path).resolve()
    if data_dir is not None:
        return (Path(data_dir) / DISPOSITION_LOG_FILENAME).resolve()
    if workspace_root is not None:
        return (Path(workspace_root) / "data" / "mail-desk" / DISPOSITION_LOG_FILENAME).resolve()
    return (resolve_data_dir() / DISPOSITION_LOG_FILENAME).resolve()


def resolve_discard_journal_path(
    journal_path: Path | str | None = None,
    data_dir: Path | str | None = None,
    workspace_root: Path | str | None = None,
) -> Path:
    """Resolve absolute path to attachment-discard-journal.json."""
    if journal_path is not None:
        return Path(journal_path).resolve()
    if data_dir is not None:
        return (Path(data_dir) / DISCARD_JOURNAL_FILENAME).resolve()
    if workspace_root is not None:
        return (Path(workspace_root) / "data" / "mail-desk" / DISCARD_JOURNAL_FILENAME).resolve()
    return (resolve_data_dir() / DISCARD_JOURNAL_FILENAME).resolve()


# ==============================================================================
# Verifiable Receipt Contracts & Canonical Request Hashing
# ==============================================================================

def validate_receipt_structure(receipt: Any, *, context: str = "disposition") -> dict[str, Any]:
    """Strictly validate receipt contract structure fail-closed.

    Raw 64-hex strings are rejected. Must be a mapping containing:
    - receipt_id: non-empty string
    - request_hash: 64-hex SHA-256
    - approved_at: RFC-3339 timestamp
    - approved_by: non-empty string
    """
    if receipt is None:
        raise ReceiptMissingError(f"Missing required approval receipt for {context}")
    if not isinstance(receipt, (dict, Mapping)):
        raise ReceiptMalformedError(
            f"Approval receipt for {context} must be a dictionary, got {type(receipt).__name__}. "
            "Raw 64-hex strings do not constitute authorization."
        )

    receipt_dict = dict(receipt)
    missing = RECEIPT_REQUIRED_KEYS - set(receipt_dict.keys())
    if missing:
        raise ReceiptMalformedError(
            f"Approval receipt for {context} missing required field(s): {sorted(missing)}"
        )

    # Check forbidden content in receipt
    forbidden = _find_forbidden_content_keys(receipt_dict)
    if forbidden:
        raise ForbiddenContentError(
            f"Forbidden content keys detected in {context} receipt: {sorted(forbidden)}"
        )

    rid = str(receipt_dict["receipt_id"]).strip()
    if not rid or len(rid) > 128:
        raise ReceiptMalformedError(f"Invalid 'receipt_id' in {context} receipt: must be 1-128 chars")

    rhash = str(receipt_dict["request_hash"]).strip().lower()
    if not SHA256_HEX_REGEX.fullmatch(rhash):
        raise ReceiptMalformedError(
            f"Invalid 'request_hash' in {context} receipt: must be 64-hex SHA-256, got {receipt_dict['request_hash']!r}"
        )

    rat = str(receipt_dict["approved_at"]).strip()
    if not RFC3339_REGEX.fullmatch(rat):
        raise ReceiptMalformedError(
            f"Invalid 'approved_at' in {context} receipt: must be RFC-3339, got {receipt_dict['approved_at']!r}"
        )

    rby = str(receipt_dict["approved_by"]).strip()
    if not rby or len(rby) > 128:
        raise ReceiptMalformedError(f"Invalid 'approved_by' in {context} receipt: must be 1-128 chars")

    return {
        "receipt_id": rid,
        "request_hash": rhash,
        "approved_at": rat,
        "approved_by": rby,
    }


def canonical_receipt_sha256(receipt: Mapping[str, Any], *, context: str = "disposition") -> str:
    """Compute deterministic 64-hex SHA-256 over a validated canonical receipt."""
    validated = validate_receipt_structure(receipt, context=context)
    canonical_json = json.dumps(validated, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def verify_approval_receipt(
    receipt: Any,
    expected_request_hash: str,
    *,
    context: str = "disposition",
) -> dict[str, Any]:
    """Verify receipt structure, exact request_hash match, and return canonical receipt info."""
    norm_exp_hash = str(expected_request_hash).strip().lower()
    if not SHA256_HEX_REGEX.fullmatch(norm_exp_hash):
        raise ValueError(f"Invalid expected_request_hash for {context}: {expected_request_hash!r}")

    validated = validate_receipt_structure(receipt, context=context)
    if validated["request_hash"] != norm_exp_hash:
        raise ReceiptDriftError(
            f"Receipt drift detected for {context}: receipt request_hash '{validated['request_hash']}' "
            f"does not match computed request hash '{norm_exp_hash}'"
        )

    receipt_hash = canonical_receipt_sha256(validated, context=context)
    return {
        "receipt": validated,
        "receipt_hash": receipt_hash,
    }


# ==============================================================================
# Disposition Request Hashing & Decision ID
# ==============================================================================

def build_disposition_request(
    *,
    attachment_id: str,
    index_entry_sha256: str,
    decision: str,
    rationale: str | None = None,
    review_after: str | None = None,
    candidate_review_hash: str | None = None,
    promotion_id: str | None = None,
    promotion_status: str | None = None,
    schema_version: int = 1,
) -> dict[str, Any]:
    """Construct the canonical disposition request structure to be bound by receipt."""
    norm_att_id = str(attachment_id).strip().lower()
    norm_idx_hash = str(index_entry_sha256).strip().lower()
    norm_dec = str(decision).strip().lower()

    if not SHA256_HEX_REGEX.fullmatch(norm_att_id):
        raise DispositionSchemaError(f"Invalid attachment_id: {attachment_id!r}")
    if not SHA256_HEX_REGEX.fullmatch(norm_idx_hash):
        raise DispositionSchemaError(f"Invalid index_entry_sha256: {index_entry_sha256!r}")
    if norm_dec not in ALLOWED_DECISIONS:
        raise DispositionSchemaError(f"Invalid decision: {decision!r}")

    req: dict[str, Any] = {
        "action": "attachment_disposition",
        "attachment_id": norm_att_id,
        "decision": norm_dec,
        "index_entry_sha256": norm_idx_hash,
        "schema_version": int(schema_version),
    }
    if rationale is not None:
        req["rationale"] = str(rationale).strip()
    if review_after is not None:
        req["review_after"] = str(review_after).strip()
    if candidate_review_hash is not None:
        req["candidate_review_hash"] = str(candidate_review_hash).strip().lower()
    if promotion_id is not None:
        req["promotion_id"] = str(promotion_id).strip()
    if promotion_status is not None:
        req["promotion_status"] = str(promotion_status).strip().lower()

    return req


def canonical_disposition_request_sha256(request: Mapping[str, Any]) -> str:
    """Compute deterministic SHA-256 hash of a disposition request."""
    if not isinstance(request, (dict, Mapping)):
        raise TypeError("disposition request must be a mapping")
    req_dict = dict(request)
    # Exclude any receipts or extra internal keys
    req_dict.pop("receipt", None)
    req_dict.pop("approval_receipt", None)
    req_dict.pop("decision_id", None)
    req_dict.pop("timestamp", None)
    req_dict.pop("human_receipt_hash", None)

    canonical_json = json.dumps(req_dict, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def build_disposition_receipt(
    *,
    request_hash: str,
    receipt_id: str | None = None,
    approved_at: str | None = None,
    approved_by: str = "human_reviewer",
) -> dict[str, Any]:
    """Helper to construct a valid disposition approval receipt for tests and callers."""
    return {
        "receipt_id": receipt_id or f"rcpt_{uuid.uuid4().hex[:16]}",
        "request_hash": str(request_hash).strip().lower(),
        "approved_at": approved_at or utc_now_iso(),
        "approved_by": str(approved_by).strip(),
    }


def compute_decision_id(
    *,
    attachment_id: str,
    index_entry_sha256: str,
    decision: str,
    timestamp: str,
    human_receipt_hash: str,
    candidate_review_hash: str | None = None,
    review_after: str | None = None,
    rationale: str | None = None,
    promotion_id: str | None = None,
    promotion_status: str | None = None,
) -> str:
    """Compute deterministic 64-char SHA-256 ID binding all canonical decision identity elements."""
    norm_att_id = str(attachment_id).strip().lower()
    norm_idx_hash = str(index_entry_sha256).strip().lower()
    norm_dec = str(decision).strip().lower()
    norm_ts = str(timestamp).strip()
    norm_hr_hash = str(human_receipt_hash).strip().lower()

    if not SHA256_HEX_REGEX.fullmatch(norm_att_id):
        raise ValueError(f"Invalid attachment_id: {attachment_id!r}")
    if not SHA256_HEX_REGEX.fullmatch(norm_idx_hash):
        raise ValueError(f"Invalid index_entry_sha256: {index_entry_sha256!r}")
    if norm_dec not in ALLOWED_DECISIONS:
        raise ValueError(f"Invalid decision: {decision!r} (expected one of {sorted(ALLOWED_DECISIONS)})")
    if not RFC3339_REGEX.fullmatch(norm_ts):
        raise ValueError(f"Invalid timestamp: {timestamp!r}")
    if not SHA256_HEX_REGEX.fullmatch(norm_hr_hash):
        raise ValueError(f"Invalid human_receipt_hash: {human_receipt_hash!r}")

    canon: dict[str, Any] = {
        "attachment_id": norm_att_id,
        "decision": norm_dec,
        "human_receipt_hash": norm_hr_hash,
        "index_entry_sha256": norm_idx_hash,
        "timestamp": norm_ts,
    }
    if candidate_review_hash is not None:
        canon["candidate_review_hash"] = str(candidate_review_hash).strip().lower()
    if promotion_id is not None:
        canon["promotion_id"] = str(promotion_id).strip()
    if promotion_status is not None:
        canon["promotion_status"] = str(promotion_status).strip().lower()
    if rationale is not None:
        canon["rationale"] = str(rationale).strip()
    if review_after is not None:
        canon["review_after"] = str(review_after).strip()

    serialized = json.dumps(canon, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


# ==============================================================================
# Apply Request Hashing & Receipt
# ==============================================================================

def build_apply_request(
    *,
    attachment_id: str,
    decision_id: str,
    index_entry_sha256: str,
    quarantine_path: str,
    sha256: str,
    size_bytes: int,
    run_id: str,
    schema_version: int = 1,
) -> dict[str, Any]:
    """Construct the exact canonical apply request bounding the deletion scope."""
    norm_att_id = str(attachment_id).strip().lower()
    norm_dec_id = str(decision_id).strip().lower()
    norm_idx_hash = str(index_entry_sha256).strip().lower()
    norm_sha = str(sha256).strip().lower()
    norm_run = str(run_id).strip()
    norm_path = PurePosixPath(quarantine_path).as_posix()

    if not SHA256_HEX_REGEX.fullmatch(norm_att_id):
        raise DispositionApplyError(f"Invalid attachment_id: {attachment_id!r}")
    if not SHA256_HEX_REGEX.fullmatch(norm_dec_id):
        raise DispositionApplyError(f"Invalid decision_id: {decision_id!r}")
    if not SHA256_HEX_REGEX.fullmatch(norm_idx_hash):
        raise DispositionApplyError(f"Invalid index_entry_sha256: {index_entry_sha256!r}")
    if not SHA256_HEX_REGEX.fullmatch(norm_sha):
        raise DispositionApplyError(f"Invalid sha256: {sha256!r}")
    if not is_valid_run_id(norm_run):
        raise DispositionApplyError(f"Invalid run_id: {run_id!r}")
    if not isinstance(size_bytes, int) or isinstance(size_bytes, bool) or size_bytes <= 0:
        raise DispositionApplyError(f"Invalid size_bytes: {size_bytes!r} (must be positive integer > 0)")

    return {
        "action": "discard",
        "attachment_id": norm_att_id,
        "decision_id": norm_dec_id,
        "index_entry_sha256": norm_idx_hash,
        "quarantine_path": norm_path,
        "run_id": norm_run,
        "schema_version": int(schema_version),
        "sha256": norm_sha,
        "size_bytes": int(size_bytes),
    }


def canonical_apply_request_sha256(request: Mapping[str, Any]) -> str:
    """Compute deterministic SHA-256 hash of an apply request."""
    if not isinstance(request, (dict, Mapping)):
        raise TypeError("apply request must be a mapping")
    req_dict = dict(request)
    req_dict.pop("receipt", None)
    req_dict.pop("apply_receipt", None)
    canonical_json = json.dumps(req_dict, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def build_apply_receipt(
    *,
    request_hash: str,
    receipt_id: str | None = None,
    approved_at: str | None = None,
    approved_by: str = "human_operator",
) -> dict[str, Any]:
    """Helper to construct a valid apply approval receipt for tests and callers."""
    return {
        "receipt_id": receipt_id or f"rcpt_apply_{uuid.uuid4().hex[:16]}",
        "request_hash": str(request_hash).strip().lower(),
        "approved_at": approved_at or utc_now_iso(),
        "approved_by": str(approved_by).strip(),
    }


def verify_apply_receipt(
    receipt: Any,
    expected_request_hash: str,
) -> dict[str, Any]:
    """Verify an apply approval receipt fail-closed."""
    # Human-Approval boundary: reject the machine receipt class/type/issuer fail-closed.
    guard_context_authorization(receipt, context=CONTEXT_APPLY)
    return verify_approval_receipt(receipt, expected_request_hash, context="apply_discard")


# ==============================================================================
# Schema 1 Validation
# ==============================================================================

def validate_disposition_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Strictly validate a disposition entry against Schema 1 fail-closed."""
    if not isinstance(entry, dict):
        raise DispositionSchemaError(f"Disposition entry must be a dict, got {type(entry).__name__}")

    # 1. Reject unknown fields
    unknown = set(entry.keys()) - ALLOWED_DISPOSITION_ENTRY_FIELDS
    if unknown:
        raise DispositionSchemaError(f"Unknown field(s) in disposition entry: {sorted(unknown)}")

    # 2. Check forbidden content
    forbidden = _find_forbidden_content_keys(entry)
    if forbidden:
        raise ForbiddenContentError(f"Forbidden content keys detected in disposition entry: {sorted(forbidden)}")

    # 3. Attachment ID
    raw_att_id = entry.get("attachment_id")
    if not raw_att_id or not isinstance(raw_att_id, str):
        raise DispositionSchemaError("Missing or invalid required 'attachment_id'")
    norm_att_id = raw_att_id.strip().lower()
    if not SHA256_HEX_REGEX.fullmatch(norm_att_id):
        raise DispositionSchemaError(f"Invalid 'attachment_id': must be 64-hex SHA-256, got {raw_att_id!r}")

    # 4. Index Entry SHA-256
    raw_idx_hash = entry.get("index_entry_sha256")
    if not raw_idx_hash or not isinstance(raw_idx_hash, str):
        raise DispositionSchemaError("Missing or invalid required 'index_entry_sha256'")
    norm_idx_hash = raw_idx_hash.strip().lower()
    if not SHA256_HEX_REGEX.fullmatch(norm_idx_hash):
        raise DispositionSchemaError(f"Invalid 'index_entry_sha256': must be 64-hex SHA-256, got {raw_idx_hash!r}")

    # 5. Decision
    raw_decision = entry.get("decision")
    if not raw_decision or not isinstance(raw_decision, str):
        raise DispositionSchemaError("Missing or invalid required 'decision'")
    norm_decision = raw_decision.strip().lower()
    if norm_decision not in ALLOWED_DECISIONS:
        raise DispositionSchemaError(
            f"Invalid 'decision': must be one of {sorted(ALLOWED_DECISIONS)}, got {raw_decision!r}"
        )

    # 6. Timestamp
    raw_ts = entry.get("timestamp")
    if not raw_ts or not isinstance(raw_ts, str):
        raise DispositionSchemaError("Missing or invalid required 'timestamp'")
    norm_ts = raw_ts.strip()
    if not RFC3339_REGEX.fullmatch(norm_ts):
        raise DispositionSchemaError(f"Invalid 'timestamp': must be RFC-3339 format, got {raw_ts!r}")

    # 7. Human Receipt Hash
    raw_hr_hash = entry.get("human_receipt_hash")
    if not raw_hr_hash or not isinstance(raw_hr_hash, str):
        raise DispositionSchemaError("Missing or invalid required 'human_receipt_hash'")
    norm_hr_hash = raw_hr_hash.strip().lower()
    if not SHA256_HEX_REGEX.fullmatch(norm_hr_hash):
        raise DispositionSchemaError(f"Invalid 'human_receipt_hash': must be 64-hex SHA-256, got {raw_hr_hash!r}")

    # 8. Rationale (optional bounded string)
    raw_rationale = entry.get("rationale")
    norm_rationale = None
    if raw_rationale is not None:
        if not isinstance(raw_rationale, str):
            raise DispositionSchemaError(f"'rationale' must be a string, got {type(raw_rationale).__name__}")
        rat_str = raw_rationale.strip()
        if len(rat_str) > RATIONALE_MAX_LENGTH:
            raise DispositionSchemaError(
                f"'rationale' exceeds maximum length of {RATIONALE_MAX_LENGTH} characters: length={len(rat_str)}"
            )
        if any(c in rat_str for c in ("\n", "\r")):
            raise DispositionSchemaError("'rationale' must be a single line, newline characters are forbidden")
        norm_rationale = rat_str

    # 9. Conditional fields per decision
    norm_review_after = None
    norm_candidate_hash = None
    norm_promotion_id = None
    norm_promotion_status = None

    if norm_decision == DECISION_RETAIN:
        if entry.get("candidate_review_hash") is not None:
            raise DispositionSchemaError("'candidate_review_hash' is forbidden when decision is 'retain'")
        if entry.get("promotion_id") is not None:
            raise DispositionSchemaError("'promotion_id' is forbidden when decision is 'retain'")
        if entry.get("promotion_status") is not None:
            raise DispositionSchemaError("'promotion_status' is forbidden when decision is 'retain'")
        raw_ra = entry.get("review_after")
        if raw_ra is not None:
            if not isinstance(raw_ra, str):
                raise DispositionSchemaError(f"'review_after' must be a string, got {type(raw_ra).__name__}")
            ra_str = raw_ra.strip()
            if not RFC3339_REGEX.fullmatch(ra_str):
                raise DispositionSchemaError(f"Invalid 'review_after': must be RFC-3339 format, got {raw_ra!r}")
            norm_review_after = ra_str

    elif norm_decision == DECISION_PROMOTE:
        if entry.get("review_after") is not None:
            raise DispositionSchemaError("'review_after' is forbidden when decision is 'promote'")
        raw_cand = entry.get("candidate_review_hash")
        if not raw_cand or not isinstance(raw_cand, str):
            raise DispositionSchemaError("Missing required 'candidate_review_hash' for decision 'promote'")
        cand_str = raw_cand.strip().lower()
        if not SHA256_HEX_REGEX.fullmatch(cand_str):
            raise DispositionSchemaError(
                f"Invalid 'candidate_review_hash': must be 64-hex SHA-256, got {raw_cand!r}"
            )
        norm_candidate_hash = cand_str

        raw_pid = entry.get("promotion_id")
        if raw_pid is not None:
            if not isinstance(raw_pid, str):
                raise DispositionSchemaError(f"'promotion_id' must be a string, got {type(raw_pid).__name__}")
            pid_str = raw_pid.strip()
            if len(pid_str) > PROMOTION_ID_MAX_LENGTH:
                raise DispositionSchemaError(
                    f"'promotion_id' exceeds maximum length of {PROMOTION_ID_MAX_LENGTH} characters"
                )
            norm_promotion_id = pid_str

        raw_pstatus = entry.get("promotion_status")
        if raw_pstatus is not None:
            if not isinstance(raw_pstatus, str):
                raise DispositionSchemaError(f"'promotion_status' must be a string, got {type(raw_pstatus).__name__}")
            pst_str = raw_pstatus.strip().lower()
            if pst_str not in ALLOWED_PROMOTION_STATUSES:
                raise DispositionSchemaError(
                    f"Invalid 'promotion_status': must be one of {sorted(ALLOWED_PROMOTION_STATUSES)}, got {raw_pstatus!r}"
                )
            norm_promotion_status = pst_str

    elif norm_decision == DECISION_DISCARD:
        if entry.get("review_after") is not None:
            raise DispositionSchemaError("'review_after' is forbidden when decision is 'discard'")
        if entry.get("candidate_review_hash") is not None:
            raise DispositionSchemaError("'candidate_review_hash' is forbidden when decision is 'discard'")
        if entry.get("promotion_id") is not None:
            raise DispositionSchemaError("'promotion_id' is forbidden when decision is 'discard'")
        if entry.get("promotion_status") is not None:
            raise DispositionSchemaError("'promotion_status' is forbidden when decision is 'discard'")

    # 10. Deterministic decision_id computation & verification
    computed_id = compute_decision_id(
        attachment_id=norm_att_id,
        index_entry_sha256=norm_idx_hash,
        decision=norm_decision,
        timestamp=norm_ts,
        human_receipt_hash=norm_hr_hash,
        candidate_review_hash=norm_candidate_hash,
        review_after=norm_review_after,
        rationale=norm_rationale,
        promotion_id=norm_promotion_id,
        promotion_status=norm_promotion_status,
    )

    provided_id = entry.get("decision_id")
    if provided_id is not None:
        norm_prov = str(provided_id).strip().lower()
        if norm_prov != computed_id:
            raise DispositionDriftError(
                f"decision_id drift: provided '{provided_id}' does not match computed deterministic ID '{computed_id}'"
            )

    result: dict[str, Any] = {
        "decision_id": computed_id,
        "attachment_id": norm_att_id,
        "index_entry_sha256": norm_idx_hash,
        "decision": norm_decision,
        "timestamp": norm_ts,
        "human_receipt_hash": norm_hr_hash,
        "rationale": norm_rationale,
    }
    if norm_review_after is not None:
        result["review_after"] = norm_review_after
    if norm_candidate_hash is not None:
        result["candidate_review_hash"] = norm_candidate_hash
    if norm_promotion_id is not None:
        result["promotion_id"] = norm_promotion_id
    if norm_promotion_status is not None:
        result["promotion_status"] = norm_promotion_status

    return result


# ==============================================================================
# Data Access: Log Loader
# ==============================================================================

def load_disposition_log(log_path: Path) -> dict[str, Any]:
    """Load and parse attachment-disposition-log.jsonl fail-closed against corruption and drift."""
    entries: list[dict[str, Any]] = []
    by_decision_id: dict[str, dict[str, Any]] = {}
    by_attachment_id: dict[str, list[dict[str, Any]]] = {}
    latest_by_attachment_id: dict[str, dict[str, Any]] = {}

    if not log_path.exists():
        return {
            "entries": entries,
            "by_decision_id": by_decision_id,
            "by_attachment_id": by_attachment_id,
            "latest_by_attachment_id": latest_by_attachment_id,
        }

    raw_text = log_path.read_text(encoding="utf-8")
    lines = raw_text.splitlines()

    for line_num, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except Exception as err:
            raise DispositionSchemaError(
                f"Corrupted JSON on line {line_num} of disposition log '{log_path}': {err}"
            ) from err

        validated = validate_disposition_entry(parsed)
        dec_id = validated["decision_id"]
        att_id = validated["attachment_id"]

        existing = by_decision_id.get(dec_id)
        if existing is not None:
            if existing != validated:
                raise DispositionDriftError(
                    f"Duplicate decision_id '{dec_id}' with conflicting content on line {line_num}"
                )
            continue

        entries.append(validated)
        by_decision_id[dec_id] = validated
        by_attachment_id.setdefault(att_id, []).append(validated)
        latest_by_attachment_id[att_id] = validated

    return {
        "entries": entries,
        "by_decision_id": by_decision_id,
        "by_attachment_id": by_attachment_id,
        "latest_by_attachment_id": latest_by_attachment_id,
    }


# ==============================================================================
# Append-Only Writer
# ==============================================================================

def record_disposition_entry(
    log_path: Path | str | None = None,
    *,
    payload: dict[str, Any],
    approval_receipt: Mapping[str, Any] | None = None,
    workspace_root: Path | str | None = None,
    data_dir: Path | str | None = None,
    index_path: Path | str | None = None,
    lease_id: str | None = None,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    """Append a validated disposition decision into attachment-disposition-log.jsonl under workspace lock."""
    ws = Path(workspace_root or Path.cwd()).resolve()
    lp = resolve_disposition_log_path(log_path, data_dir=data_dir, workspace_root=ws)
    idx_p = resolve_quarantine_index_path(index_path, data_dir=data_dir, workspace_root=ws)

    # 1. Lock Verification
    try:
        verify_quarantine_workspace_lock(
            workspace_root=ws,
            lease_id=lease_id,
            conversation_id=conversation_id,
            data_dir=idx_p.parent,
        )
    except Exception as err:
        raise DispositionLockRequiredError(
            f"Recording a disposition requires an active, owned workspace lock: {err}"
        ) from err

    # 2. Extract and Verify Receipt Contract
    receipt_to_verify = approval_receipt or payload.get("approval_receipt") or payload.get("receipt")
    if receipt_to_verify is None:
        raw_receipt_hash = payload.get("human_receipt_hash")
        if raw_receipt_hash:
            raise ReceiptMalformedError(
                "Raw 64-hex 'human_receipt_hash' is not accepted as authorization. "
                "An explicit verifiable 'approval_receipt' mapping is required."
            )
        raise ReceiptMissingError("Missing required 'approval_receipt' mapping.")

    # 2b. Human-Approval boundary: reject the machine receipt class/type/issuer fail-closed
    # before any index read or write.
    guard_context_authorization(receipt_to_verify, context=CONTEXT_DISPOSITION)

    # 3. Verify target attachment in quarantine index
    att_id = payload.get("attachment_id")
    if not att_id or not isinstance(att_id, str):
        raise DispositionSchemaError("Payload missing required 'attachment_id'")
    norm_att_id = att_id.strip().lower()

    idx_data = load_quarantine_index(idx_p)
    items = idx_data.get("items", {})
    if norm_att_id not in items:
        raise DispositionDriftError(
            f"Cannot record disposition for attachment '{norm_att_id}': not present in quarantine index '{idx_p}'"
        )
    target_idx_entry = items[norm_att_id]
    current_canonical_idx_hash = canonical_index_entry_sha256(target_idx_entry)

    # 4. Canonical Request Hash computation
    disp_request = build_disposition_request(
        attachment_id=norm_att_id,
        index_entry_sha256=current_canonical_idx_hash,
        decision=payload.get("decision", ""),
        rationale=payload.get("rationale"),
        review_after=payload.get("review_after"),
        candidate_review_hash=payload.get("candidate_review_hash"),
        promotion_id=payload.get("promotion_id"),
        promotion_status=payload.get("promotion_status"),
        schema_version=1,
    )
    computed_req_hash = canonical_disposition_request_sha256(disp_request)

    # 5. Verify Receipt against computed request hash
    verified_receipt_info = verify_approval_receipt(
        receipt_to_verify,
        computed_req_hash,
        context="disposition",
    )
    receipt_hash = verified_receipt_info["receipt_hash"]

    # 6. Build final entry payload
    prepared_entry = dict(payload)
    prepared_entry.pop("approval_receipt", None)
    prepared_entry.pop("receipt", None)
    prepared_entry["attachment_id"] = norm_att_id
    prepared_entry["index_entry_sha256"] = current_canonical_idx_hash
    prepared_entry["human_receipt_hash"] = receipt_hash
    if "timestamp" not in prepared_entry or not prepared_entry["timestamp"]:
        prepared_entry["timestamp"] = utc_now_iso()

    validated_entry = validate_disposition_entry(prepared_entry)
    dec_id = validated_entry["decision_id"]

    # 7. Check existing log state (idempotency check)
    current_log = load_disposition_log(lp)
    existing = current_log["by_decision_id"].get(dec_id)
    if existing is not None:
        if existing == validated_entry:
            return {
                "status": "unchanged",
                "decision_id": dec_id,
                "attachment_id": norm_att_id,
                "entry": validated_entry,
            }
        raise DispositionDriftError(
            f"Decision '{dec_id}' already exists in log '{lp}' with differing content"
        )

    lp.parent.mkdir(parents=True, exist_ok=True)
    line_bytes = (json.dumps(validated_entry, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    with open(lp, "ab") as f:
        f.write(line_bytes)
        f.flush()
        os.fsync(f.fileno())

    return {
        "status": "recorded",
        "decision_id": dec_id,
        "attachment_id": norm_att_id,
        "entry": validated_entry,
    }


# ==============================================================================
# Active Run & Journal Detection
# ==============================================================================

def check_active_run_evidence(run_id: str, workspace_root: Path) -> tuple[bool, str | None]:
    """Check whether a run directory exhibits active locks, temporary files, or running journals."""
    norm_run = str(run_id).strip()
    if not is_valid_run_id(norm_run):
        return True, f"Invalid run_id: {run_id!r}"

    run_dir = workspace_root / "data" / "mail-desk" / "attachments" / norm_run
    if not run_dir.exists():
        return False, None

    inv_lock = run_dir / ".quarantine-inventory.lock"
    if inv_lock.exists():
        return True, f"Active inventory lock present in run directory: {inv_lock}"

    try:
        temp_files = [p for p in run_dir.iterdir() if p.name.endswith(".tmp") or p.name.startswith(".inv.")]
        if temp_files:
            return True, f"Active temporary files present in run directory: {[p.name for p in temp_files]}"
    except OSError as err:
        return True, f"Failed to inspect run directory: {err}"

    journal_path = workspace_root / "data" / "mail-desk" / "batch-recovery-journal.json"
    if journal_path.exists():
        try:
            jdata = json.loads(journal_path.read_text(encoding="utf-8"))
            runs = jdata.get("runs", {})
            run_entry = runs.get(run_id)
            if isinstance(run_entry, dict) and run_entry.get("status") == "running":
                return True, f"Run '{run_id}' is marked as running in recovery journal"
        except Exception:
            pass

    return False, None


# ==============================================================================
# Discard & Recovery Journal
# ==============================================================================

def load_discard_journal(journal_path: Path) -> dict[str, Any]:
    """Load, parse and strictly validate attachment-discard-journal.json fail-closed."""
    if not journal_path.exists():
        return {"schema_version": 1, "updated_at": utc_now_iso(), "entries": {}}

    try:
        raw_text = journal_path.read_text(encoding="utf-8")
        data = json.loads(raw_text)
    except Exception as err:
        raise RecoveryJournalCorruptedError(f"Failed to parse discard journal '{journal_path}': {err}") from err

    if not isinstance(data, dict):
        raise RecoveryJournalCorruptedError(f"Discard journal root must be dict, got {type(data).__name__}")

    # Root allowed keys
    allowed_root_keys = {"schema_version", "updated_at", "entries"}
    extra_root_keys = set(data.keys()) - allowed_root_keys
    if extra_root_keys:
        raise RecoveryJournalCorruptedError(f"Unknown root field(s) in discard journal: {sorted(extra_root_keys)}")

    forbidden_root = _find_forbidden_content_keys(data)
    if forbidden_root:
        raise ForbiddenContentError(f"Forbidden content keys detected in discard journal: {sorted(forbidden_root)}")

    if data.get("schema_version") != 1:
        raise RecoveryJournalCorruptedError(f"Unsupported discard journal schema_version: {data.get('schema_version')}")

    root_updated_at = data.get("updated_at")
    if not isinstance(root_updated_at, str) or not RFC3339_REGEX.fullmatch(root_updated_at):
        raise RecoveryJournalCorruptedError(f"Invalid root 'updated_at' timestamp: {root_updated_at!r}")

    entries = data.get("entries")
    if not isinstance(entries, dict):
        raise RecoveryJournalCorruptedError("Discard journal missing 'entries' dict")

    # Validate each entry in entries
    for entry_key, entry in entries.items():
        if not isinstance(entry_key, str) or not SHA256_HEX_REGEX.fullmatch(entry_key):
            raise RecoveryJournalCorruptedError(f"Invalid journal entry key: {entry_key!r}")
        if not isinstance(entry, dict):
            raise RecoveryJournalCorruptedError(f"Journal entry '{entry_key}' must be dict, got {type(entry).__name__}")

        # Check unknown fields in entry
        extra_entry_keys = set(entry.keys()) - DISCARD_JOURNAL_ENTRY_ALLOWED_KEYS
        if extra_entry_keys:
            raise RecoveryJournalCorruptedError(
                f"Unknown field(s) in journal entry '{entry_key}': {sorted(extra_entry_keys)}"
            )

        # Check forbidden content in entry
        forbidden_entry = _find_forbidden_content_keys(entry)
        if forbidden_entry:
            raise ForbiddenContentError(
                f"Forbidden content keys detected in journal entry '{entry_key}': {sorted(forbidden_entry)}"
            )

        # Validate required string fields
        for fld in (
            "journal_entry_id", "attachment_id", "decision_id",
            "apply_receipt_hash", "apply_request_hash", "previous_index_entry_sha256",
            "quarantine_path", "sha256", "run_id", "state", "created_at", "updated_at",
        ):
            if fld not in entry:
                raise RecoveryJournalCorruptedError(f"Journal entry '{entry_key}' missing required field '{fld}'")

        # Hash validations (64-hex lowercase)
        for hfld in (
            "journal_entry_id", "attachment_id", "decision_id",
            "apply_receipt_hash", "apply_request_hash", "previous_index_entry_sha256", "sha256"
        ):
            val = str(entry[hfld]).strip().lower()
            if not SHA256_HEX_REGEX.fullmatch(val):
                raise RecoveryJournalCorruptedError(
                    f"Journal entry '{entry_key}' invalid {hfld}: must be 64-hex SHA-256, got {entry[hfld]!r}"
                )

        if entry["journal_entry_id"].lower() != entry_key.lower():
            raise RecoveryJournalCorruptedError(
                f"Journal entry '{entry_key}' key does not match entry['journal_entry_id']: {entry['journal_entry_id']}"
            )

        # Recompute journal_entry_id deterministically
        expected_jid = hashlib.sha256(
            f"{entry['attachment_id'].lower()}:{entry['decision_id'].lower()}:{entry['apply_receipt_hash'].lower()}:{entry['previous_index_entry_sha256'].lower()}".encode("utf-8")
        ).hexdigest()
        if entry["journal_entry_id"].lower() != expected_jid:
            raise RecoveryJournalCorruptedError(
                f"Journal entry '{entry_key}' journal_entry_id mismatch: computed {expected_jid}, stored {entry['journal_entry_id']}"
            )

        # Validate run_id
        run_id = str(entry["run_id"]).strip()
        if not is_valid_run_id(run_id):
            raise RecoveryJournalCorruptedError(f"Journal entry '{entry_key}' invalid run_id: {run_id!r}")

        # Validate size_bytes
        size_bytes = entry.get("size_bytes")
        if not isinstance(size_bytes, int) or isinstance(size_bytes, bool) or size_bytes <= 0:
            raise RecoveryJournalCorruptedError(
                f"Journal entry '{entry_key}' invalid size_bytes: {size_bytes!r} (must be positive integer > 0)"
            )

        # Validate quarantine_path
        qpath = str(entry["quarantine_path"]).strip()
        if (
            not qpath
            or os.path.isabs(qpath)
            or PurePath(qpath).is_absolute()
            or qpath.startswith("/")
            or qpath.startswith("\\")
            or (len(qpath) > 1 and qpath[1] == ":")
        ):
            raise RecoveryJournalCorruptedError(f"Journal entry '{entry_key}' invalid quarantine_path: {qpath!r}")
        posix_rel = PurePosixPath(qpath)
        if any(part == ".." for part in posix_rel.parts):
            raise RecoveryJournalCorruptedError(f"Journal entry '{entry_key}' directory traversal in quarantine_path: {qpath!r}")
        expected_run_prefix = PurePosixPath(f"data/mail-desk/attachments/{run_id}")
        try:
            posix_rel.relative_to(expected_run_prefix)
        except ValueError:
            raise RecoveryJournalCorruptedError(
                f"Journal entry '{entry_key}' quarantine_path '{qpath}' does not reside under expected run prefix '{expected_run_prefix}'"
            )

        # Validate timestamps
        if not RFC3339_REGEX.fullmatch(str(entry["created_at"]).strip()):
            raise RecoveryJournalCorruptedError(f"Journal entry '{entry_key}' invalid created_at timestamp: {entry['created_at']!r}")
        if not RFC3339_REGEX.fullmatch(str(entry["updated_at"]).strip()):
            raise RecoveryJournalCorruptedError(f"Journal entry '{entry_key}' invalid updated_at timestamp: {entry['updated_at']!r}")

        # Recompute apply_request_hash from stored scope fields
        reconstructed_apply_req = build_apply_request(
            attachment_id=entry["attachment_id"].lower(),
            decision_id=entry["decision_id"].lower(),
            index_entry_sha256=entry["previous_index_entry_sha256"].lower(),
            quarantine_path=qpath,
            sha256=entry["sha256"].lower(),
            size_bytes=size_bytes,
            run_id=run_id,
            schema_version=1,
        )
        computed_apply_hash = canonical_apply_request_sha256(reconstructed_apply_req)
        if entry["apply_request_hash"].lower() != computed_apply_hash:
            raise RecoveryJournalCorruptedError(
                f"Journal entry '{entry_key}' apply_request_hash drift: computed {computed_apply_hash}, stored {entry['apply_request_hash']}"
            )

        # Validate status, state, last_successful_state, failure_stage, and error consistency
        status = entry.get("status")
        if status not in ("in_progress", "completed", "failed"):
            raise RecoveryJournalCorruptedError(f"Journal entry '{entry_key}' invalid status: {status!r}")

        state = str(entry["state"]).strip().lower()
        if state not in ALLOWED_JOURNAL_STATES:
            raise RecoveryJournalCorruptedError(f"Journal entry '{entry_key}' invalid state: {state!r}")

        last_succ = entry.get("last_successful_state")
        if last_succ is None:
            raise RecoveryJournalCorruptedError(f"Journal entry '{entry_key}' missing last_successful_state")
        last_succ = str(last_succ).strip().lower()
        if last_succ not in STATE_ORDER:
            raise RecoveryJournalCorruptedError(f"Journal entry '{entry_key}' invalid last_successful_state: {last_succ!r}")

        failure_stage = entry.get("failure_stage")
        err_obj = entry.get("error")

        if status == "completed":
            if state != JOURNAL_STATE_COMPLETED:
                raise RecoveryJournalCorruptedError(
                    f"Journal entry '{entry_key}' status 'completed' requires state '{JOURNAL_STATE_COMPLETED}', got '{state}'"
                )
            if last_succ != JOURNAL_STATE_COMPLETED:
                raise RecoveryJournalCorruptedError(
                    f"Journal entry '{entry_key}' status 'completed' requires last_successful_state '{JOURNAL_STATE_COMPLETED}', got '{last_succ}'"
                )
            if failure_stage is not None:
                raise RecoveryJournalCorruptedError(
                    f"Journal entry '{entry_key}' status 'completed' requires failure_stage=None, got {failure_stage!r}"
                )
            if err_obj is not None:
                raise RecoveryJournalCorruptedError(
                    f"Journal entry '{entry_key}' status 'completed' requires error=None, got {err_obj!r}"
                )

        elif status == "in_progress":
            if state == JOURNAL_STATE_COMPLETED or last_succ == JOURNAL_STATE_COMPLETED:
                raise RecoveryJournalCorruptedError(
                    f"Journal entry '{entry_key}' status 'in_progress' cannot have completed state"
                )
            if state not in (JOURNAL_STATE_PREPARED, JOURNAL_STATE_FILE_DELETED, JOURNAL_STATE_INVENTORY_UPDATED, JOURNAL_STATE_INDEX_UPDATED):
                raise RecoveryJournalCorruptedError(
                    f"Journal entry '{entry_key}' status 'in_progress' invalid with state '{state}'"
                )
            if state != last_succ:
                raise RecoveryJournalCorruptedError(
                    f"Journal entry '{entry_key}' status 'in_progress' state '{state}' must match last_successful_state '{last_succ}'"
                )
            if failure_stage is not None:
                raise RecoveryJournalCorruptedError(
                    f"Journal entry '{entry_key}' status 'in_progress' requires failure_stage=None, got {failure_stage!r}"
                )
            if err_obj is not None:
                raise RecoveryJournalCorruptedError(
                    f"Journal entry '{entry_key}' status 'in_progress' requires error=None, got {err_obj!r}"
                )

        elif status == "failed":
            if state == JOURNAL_STATE_COMPLETED:
                raise RecoveryJournalCorruptedError(
                    f"Journal entry '{entry_key}' status 'failed' cannot have state '{JOURNAL_STATE_COMPLETED}'"
                )
            if failure_stage is None or not str(failure_stage).strip():
                raise RecoveryJournalCorruptedError(
                    f"Journal entry '{entry_key}' status 'failed' requires non-empty failure_stage"
                )
            if err_obj is None or not isinstance(err_obj, dict):
                raise RecoveryJournalCorruptedError(
                    f"Journal entry '{entry_key}' status 'failed' requires error dict"
                )
            forbidden_err = _find_forbidden_content_keys(err_obj)
            if forbidden_err:
                raise ForbiddenContentError(f"Forbidden content in journal error field: {sorted(forbidden_err)}")
            if last_succ not in (JOURNAL_STATE_PREPARED, JOURNAL_STATE_FILE_DELETED, JOURNAL_STATE_INVENTORY_UPDATED, JOURNAL_STATE_INDEX_UPDATED):
                raise RecoveryJournalCorruptedError(
                    f"Journal entry '{entry_key}' status 'failed' has invalid last_successful_state '{last_succ}'"
                )

        # Validate history
        history = entry.get("history")
        if not isinstance(history, list) or len(history) == 0:
            raise RecoveryJournalCorruptedError(f"Journal entry '{entry_key}' missing or empty history list")

        # Trace monotonic progression through history
        last_rank = 0
        derived_successful_state = None
        for i, h_item in enumerate(history):
            if not isinstance(h_item, dict):
                raise RecoveryJournalCorruptedError(f"Journal entry '{entry_key}' history item {i} must be dict")

            extra_h_keys = set(h_item.keys()) - DISCARD_JOURNAL_HISTORY_ALLOWED_KEYS
            if extra_h_keys:
                raise RecoveryJournalCorruptedError(
                    f"Journal entry '{entry_key}' history item {i} has unknown field(s): {sorted(extra_h_keys)}"
                )

            forbidden_h = _find_forbidden_content_keys(h_item)
            if forbidden_h:
                raise ForbiddenContentError(f"Forbidden content in history item {i}: {sorted(forbidden_h)}")

            h_ts = h_item.get("timestamp")
            if not h_ts or not RFC3339_REGEX.fullmatch(str(h_ts).strip()):
                raise RecoveryJournalCorruptedError(f"Journal entry '{entry_key}' history item {i} invalid timestamp: {h_ts!r}")

            h_status = h_item.get("status")
            h_trans = h_item.get("transition")

            if h_status == "failed" or h_trans == "failed":
                if last_rank == 0:
                    raise RecoveryJournalCorruptedError(
                        f"Journal entry '{entry_key}' history item {i} failure entry cannot precede '{JOURNAL_STATE_PREPARED}'"
                    )
                if h_status != "failed" or h_trans != "failed":
                    raise RecoveryJournalCorruptedError(
                        f"Journal entry '{entry_key}' history item {i} failure item must have transition='failed' and status='failed'"
                    )
                if not h_item.get("stage") or not str(h_item.get("stage")).strip():
                    raise RecoveryJournalCorruptedError(
                        f"Journal entry '{entry_key}' history item {i} failure item missing stage"
                    )
                if h_item.get("error") is None:
                    raise RecoveryJournalCorruptedError(
                        f"Journal entry '{entry_key}' history item {i} failure item missing error"
                    )
                continue

            if h_status != "success":
                raise RecoveryJournalCorruptedError(
                    f"Journal entry '{entry_key}' history item {i} non-failure item must have status='success', got {h_status!r}"
                )
            h_state = h_item.get("state")
            if h_state not in STATE_ORDER:
                raise RecoveryJournalCorruptedError(f"Journal entry '{entry_key}' history item {i} invalid state: {h_state!r}")

            rank = STATE_ORDER[h_state]
            if last_rank == 0:
                if rank != 1 or h_state != JOURNAL_STATE_PREPARED:
                    raise RecoveryJournalCorruptedError(
                        f"Journal entry '{entry_key}' first history state must be '{JOURNAL_STATE_PREPARED}', got '{h_state}'"
                    )
            else:
                if rank != last_rank + 1:
                    raise RecoveryJournalCorruptedError(
                        f"Journal entry '{entry_key}' invalid transition in history from rank {last_rank} to {rank} ('{h_state}')"
                    )

            last_rank = rank
            derived_successful_state = h_state

        if derived_successful_state is None:
            raise RecoveryJournalCorruptedError(f"Journal entry '{entry_key}' has no successful state in history")

        expected_last_state = entry.get("last_successful_state") or entry.get("state")
        if expected_last_state != derived_successful_state:
            raise RecoveryJournalCorruptedError(
                f"Journal entry '{entry_key}' state/last_successful_state '{expected_last_state}' does not match history-derived '{derived_successful_state}'"
            )

        # Validate that top-level status is consistent with the latest history item
        last_h_item = history[-1]
        if status == "failed":
            if (
                last_h_item.get("transition") != "failed"
                or last_h_item.get("status") != "failed"
            ):
                raise RecoveryJournalCorruptedError(
                    f"Journal entry '{entry_key}' status 'failed' requires last history item to be a failure entry, "
                    f"got status={last_h_item.get('status')!r}, transition={last_h_item.get('transition')!r}"
                )
            h_stage = last_h_item.get("stage")
            if not h_stage or not str(h_stage).strip():
                raise RecoveryJournalCorruptedError(
                    f"Journal entry '{entry_key}' status 'failed' last history failure item missing stage"
                )
            if str(h_stage).strip() != str(failure_stage).strip():
                raise RecoveryJournalCorruptedError(
                    f"Journal entry '{entry_key}' failure_stage drift: top-level '{failure_stage}' != history '{h_stage}'"
                )
            hist_err = last_h_item.get("error")
            if hist_err is None:
                raise RecoveryJournalCorruptedError(
                    f"Journal entry '{entry_key}' status 'failed' last history failure item missing error"
                )

            # Canonical comparison of top-level error dict and last history error
            canon_top_err = json.dumps(err_obj, sort_keys=True)
            if isinstance(hist_err, (dict, list)):
                canon_hist_err = json.dumps(hist_err, sort_keys=True)
            elif isinstance(hist_err, str):
                try:
                    parsed = json.loads(hist_err)
                    if isinstance(parsed, (dict, list)):
                        canon_hist_err = json.dumps(parsed, sort_keys=True)
                    else:
                        canon_hist_err = hist_err
                except Exception:
                    canon_hist_err = hist_err
            else:
                canon_hist_err = str(hist_err)

            if canon_top_err != canon_hist_err and hist_err != err_obj:
                raise RecoveryJournalCorruptedError(
                    f"Journal entry '{entry_key}' error drift: top-level error does not match last history error"
                )
        else:
            # status is in_progress or completed
            if last_h_item.get("status") == "failed" or last_h_item.get("transition") == "failed":
                raise RecoveryJournalCorruptedError(
                    f"Journal entry '{entry_key}' status '{status}' cannot have unhandled failure as last history item"
                )

    return data


def _save_discard_journal_atomic(journal_path: Path, journal_data: dict[str, Any]) -> None:
    """Save attachment-discard-journal.json atomically using sibling temp file and replace."""
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = journal_path.parent / f"{journal_path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
    journal_data["updated_at"] = utc_now_iso()
    try:
        data_bytes = json.dumps(journal_data, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8")
        with open(tmp_path, "wb") as f:
            f.write(data_bytes)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, journal_path)
    except Exception as err:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise RecoveryJournalCorruptedError(f"Failed to atomically write discard journal '{journal_path}': {err}") from err


def record_journal_state(
    journal_path: Path,
    *,
    attachment_id: str,
    decision_id: str,
    apply_receipt_hash: str,
    apply_request_hash: str,
    previous_index_entry_sha256: str,
    quarantine_path: str,
    sha256: str,
    size_bytes: int,
    run_id: str,
    state: str,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist a journal state transition atomically enforcing monotonic order and immutable bindings."""
    norm_att_id = str(attachment_id).strip().lower()
    norm_dec_id = str(decision_id).strip().lower()
    norm_rcpt_hash = str(apply_receipt_hash).strip().lower()
    norm_req_hash = str(apply_request_hash).strip().lower()
    norm_prev_hash = str(previous_index_entry_sha256).strip().lower()
    norm_sha = str(sha256).strip().lower()
    norm_run = str(run_id).strip()
    norm_state = str(state).strip().lower()

    if not isinstance(size_bytes, int) or isinstance(size_bytes, bool) or size_bytes <= 0:
        raise ValueError(f"Invalid size_bytes: {size_bytes!r} (must be positive integer > 0)")

    if norm_state == JOURNAL_STATE_FAILED:
        return record_journal_failure(
            journal_path,
            attachment_id=norm_att_id,
            decision_id=norm_dec_id,
            apply_receipt_hash=norm_rcpt_hash,
            apply_request_hash=norm_req_hash,
            previous_index_entry_sha256=norm_prev_hash,
            quarantine_path=quarantine_path,
            sha256=norm_sha,
            size_bytes=size_bytes,
            run_id=norm_run,
            stage=error.get("stage", "unknown") if isinstance(error, dict) else "unknown",
            error=error or {"stage": "unknown", "error": "failed"},
        )

    if norm_state not in STATE_ORDER:
        raise ValueError(f"Invalid journal state: {state!r}")

    journal = load_discard_journal(journal_path)
    entries = journal.setdefault("entries", {})

    journal_entry_id = hashlib.sha256(
        f"{norm_att_id}:{norm_dec_id}:{norm_rcpt_hash}:{norm_prev_hash}".encode("utf-8")
    ).hexdigest()

    now_iso = utc_now_iso()
    entry = entries.get(journal_entry_id)

    if entry is None:
        if norm_state != JOURNAL_STATE_PREPARED:
            raise RecoveryJournalCorruptedError(
                f"Initial journal state must be '{JOURNAL_STATE_PREPARED}', cannot initialize in '{norm_state}'"
            )
        entry = {
            "journal_entry_id": journal_entry_id,
            "attachment_id": norm_att_id,
            "decision_id": norm_dec_id,
            "apply_receipt_hash": norm_rcpt_hash,
            "apply_request_hash": norm_req_hash,
            "previous_index_entry_sha256": norm_prev_hash,
            "quarantine_path": quarantine_path,
            "sha256": norm_sha,
            "size_bytes": size_bytes,
            "run_id": norm_run,
            "state": norm_state,
            "last_successful_state": norm_state,
            "status": "completed" if norm_state == JOURNAL_STATE_COMPLETED else "in_progress",
            "created_at": now_iso,
            "updated_at": now_iso,
            "history": [{"state": norm_state, "status": "success", "timestamp": now_iso}],
            "failure_stage": None,
            "error": None,
        }
        entries[journal_entry_id] = entry
    else:
        # Check all immutable binding fields
        binding_checks = [
            ("attachment_id", norm_att_id),
            ("decision_id", norm_dec_id),
            ("apply_receipt_hash", norm_rcpt_hash),
            ("apply_request_hash", norm_req_hash),
            ("previous_index_entry_sha256", norm_prev_hash),
            ("quarantine_path", quarantine_path),
            ("sha256", norm_sha),
            ("size_bytes", size_bytes),
            ("run_id", norm_run),
        ]
        for fld, expected_val in binding_checks:
            if entry.get(fld) != expected_val:
                raise RecoveryJournalCorruptedError(
                    f"Journal entry '{journal_entry_id}' immutable field '{fld}' drift: "
                    f"stored {entry.get(fld)!r}, provided {expected_val!r}"
                )

        curr_state = entry.get("last_successful_state") or entry.get("state")
        curr_rank = STATE_ORDER[curr_state]
        new_rank = STATE_ORDER[norm_state]

        if new_rank == curr_rank:
            # Idempotent re-recording
            if entry.get("status") == "failed":
                entry["status"] = "in_progress"
                entry["failure_stage"] = None
                entry["error"] = None
                entry["updated_at"] = now_iso
                _save_discard_journal_atomic(journal_path, journal)
            return entry

        if new_rank < curr_rank:
            raise RecoveryJournalCorruptedError(
                f"Invalid backward transition in journal: cannot transition from '{curr_state}' to '{norm_state}'"
            )

        if new_rank > curr_rank + 1:
            raise RecoveryJournalCorruptedError(
                f"Invalid skipped transition in journal: cannot transition from '{curr_state}' to '{norm_state}'"
            )

        entry["state"] = norm_state
        entry["last_successful_state"] = norm_state
        entry["status"] = "completed" if norm_state == JOURNAL_STATE_COMPLETED else "in_progress"
        entry["updated_at"] = now_iso
        entry["failure_stage"] = None
        entry["error"] = None
        entry["history"].append({"state": norm_state, "status": "success", "timestamp": now_iso})

    _save_discard_journal_atomic(journal_path, journal)
    return entry


def record_journal_failure(
    journal_path: Path,
    *,
    attachment_id: str,
    decision_id: str,
    apply_receipt_hash: str,
    apply_request_hash: str,
    previous_index_entry_sha256: str,
    quarantine_path: str,
    sha256: str,
    size_bytes: int,
    run_id: str,
    stage: str,
    error: dict[str, Any] | str,
) -> dict[str, Any]:
    """Record a failure without overwriting last_successful_state or violating monotonicity."""
    norm_att_id = str(attachment_id).strip().lower()
    norm_dec_id = str(decision_id).strip().lower()
    norm_rcpt_hash = str(apply_receipt_hash).strip().lower()
    norm_req_hash = str(apply_request_hash).strip().lower()
    norm_prev_hash = str(previous_index_entry_sha256).strip().lower()
    norm_sha = str(sha256).strip().lower()
    norm_run = str(run_id).strip()

    if not isinstance(size_bytes, int) or isinstance(size_bytes, bool) or size_bytes <= 0:
        raise ValueError(f"Invalid size_bytes: {size_bytes!r} (must be positive integer > 0)")

    journal = load_discard_journal(journal_path)
    entries = journal.setdefault("entries", {})

    journal_entry_id = hashlib.sha256(
        f"{norm_att_id}:{norm_dec_id}:{norm_rcpt_hash}:{norm_prev_hash}".encode("utf-8")
    ).hexdigest()

    now_iso = utc_now_iso()
    entry = entries.get(journal_entry_id)

    err_dict = {"stage": stage, "error": str(error)} if not isinstance(error, dict) else error

    if entry is None:
        entry = {
            "journal_entry_id": journal_entry_id,
            "attachment_id": norm_att_id,
            "decision_id": norm_dec_id,
            "apply_receipt_hash": norm_rcpt_hash,
            "apply_request_hash": norm_req_hash,
            "previous_index_entry_sha256": norm_prev_hash,
            "quarantine_path": quarantine_path,
            "sha256": norm_sha,
            "size_bytes": size_bytes,
            "run_id": norm_run,
            "state": JOURNAL_STATE_PREPARED,
            "last_successful_state": JOURNAL_STATE_PREPARED,
            "status": "failed",
            "created_at": now_iso,
            "updated_at": now_iso,
            "history": [
                {"state": JOURNAL_STATE_PREPARED, "status": "success", "timestamp": now_iso},
                {"transition": "failed", "stage": stage, "status": "failed", "error": err_dict, "timestamp": now_iso},
            ],
            "failure_stage": stage,
            "error": err_dict,
        }
        entries[journal_entry_id] = entry
    else:
        binding_checks = [
            ("attachment_id", norm_att_id),
            ("decision_id", norm_dec_id),
            ("apply_receipt_hash", norm_rcpt_hash),
            ("apply_request_hash", norm_req_hash),
            ("previous_index_entry_sha256", norm_prev_hash),
            ("quarantine_path", quarantine_path),
            ("sha256", norm_sha),
            ("size_bytes", size_bytes),
            ("run_id", norm_run),
        ]
        for fld, expected_val in binding_checks:
            if entry.get(fld) != expected_val:
                raise RecoveryJournalCorruptedError(
                    f"Journal entry '{journal_entry_id}' immutable field '{fld}' drift: "
                    f"stored {entry.get(fld)!r}, provided {expected_val!r}"
                )

        entry["status"] = "failed"
        entry["failure_stage"] = stage
        entry["error"] = err_dict
        entry["updated_at"] = now_iso
        entry["history"].append({
            "transition": "failed",
            "stage": stage,
            "status": "failed",
            "error": err_dict,
            "timestamp": now_iso,
        })

    _save_discard_journal_atomic(journal_path, journal)
    return entry


# ==============================================================================
# Atomic Inventory Mutation
# ==============================================================================

def _find_inventory_message_entry(
    msgs: dict[str, Any], mid: str
) -> tuple[str | None, dict[str, Any] | None]:
    """Find a message entry in inventory dict by matching normalized, literal, or bracketed message ID."""
    if not isinstance(msgs, dict):
        return None, None
    norm_mid = normalize_message_id(mid) or "__default__"
    if norm_mid in msgs and isinstance(msgs[norm_mid], dict):
        return norm_mid, msgs[norm_mid]
    if mid in msgs and isinstance(msgs[mid], dict):
        return mid, msgs[mid]
    raw_bracketed = f"<{norm_mid}>"
    if raw_bracketed in msgs and isinstance(msgs[raw_bracketed], dict):
        return raw_bracketed, msgs[raw_bracketed]
    for k, v in msgs.items():
        if isinstance(v, dict) and normalize_message_id(k) == norm_mid:
            return k, v
    return None, None


def update_quarantine_inventory_atomic(
    run_dir: Path,
    message_id: str,
    clean_filename: str,
) -> None:
    """Update .quarantine-inventory.json atomically using sibling temp file and replace.

    Fail-closed: Never swallows errors. Re-validates inventory schema before and after.
    """
    inv_file = run_dir / INVENTORY_FILENAME
    if not inv_file.exists():
        raise InventoryUpdateError(f"Missing inventory file in run directory: {inv_file}")

    try:
        raw_text = inv_file.read_text(encoding="utf-8")
        inv = json.loads(raw_text)
    except Exception as err:
        raise InventoryUpdateError(f"Failed to read inventory file '{inv_file}': {err}") from err

    _validate_inventory_schema(inv, inv_file)

    msgs = inv.get("messages", {})
    matched_key, msg_entry = _find_inventory_message_entry(msgs, message_id)
    if msg_entry and isinstance(msg_entry.get("files"), dict):
        files_dict = msg_entry["files"]
        files_dict.pop(clean_filename, None)
        msg_entry["count"] = len(files_dict)
        msg_entry["total_bytes"] = sum(f.get("size_bytes", 0) for f in files_dict.values())
        if msg_entry["count"] == 0 and matched_key is not None:
            msgs.pop(matched_key, None)

    _validate_inventory_schema(inv, inv_file)

    tmp_file = run_dir / f"{INVENTORY_FILENAME}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
    try:
        inv_bytes = json.dumps(inv, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8")
        with open(tmp_file, "wb") as f:
            f.write(inv_bytes)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_file, inv_file)
    except Exception as err:
        if tmp_file.exists():
            try:
                tmp_file.unlink()
            except OSError:
                pass
        raise InventoryUpdateError(f"Failed to atomically write inventory '{inv_file}': {err}") from err


# ==============================================================================
# Read-Only Reporting
# ==============================================================================

def report_dispositions(
    *,
    workspace_root: Path | str | None = None,
    data_dir: Path | str | None = None,
    index_path: Path | str | None = None,
    log_path: Path | str | None = None,
    journal_path: Path | str | None = None,
) -> dict[str, Any]:
    """Perform strictly read-only reporting classifying every quarantined attachment."""
    ws = Path(workspace_root or Path.cwd()).resolve()
    idx_p = resolve_quarantine_index_path(index_path, data_dir=data_dir, workspace_root=ws)
    lp = resolve_disposition_log_path(log_path, data_dir=data_dir, workspace_root=ws)
    jp = resolve_discard_journal_path(journal_path, data_dir=data_dir, workspace_root=ws)

    try:
        index_data = load_quarantine_index(idx_p)
        items = index_data.get("items", {})
    except Exception:
        try:
            raw_text = idx_p.read_text(encoding="utf-8")
            raw_data = json.loads(raw_text)
            items = raw_data.get("items", {}) if isinstance(raw_data, dict) else {}
        except Exception:
            items = {}

    log_data = load_disposition_log(lp)
    latest_decisions = log_data.get("latest_by_attachment_id", {})
    journal_data = load_discard_journal(jp)
    journal_entries = journal_data.get("entries", {})

    attachments_root = (ws / "data" / "mail-desk" / "attachments").resolve()

    results: list[dict[str, Any]] = []
    eligible_count = 0
    protected_count = 0
    invalid_count = 0

    for att_id, entry in sorted(items.items()):
        rel_path = entry.get("quarantine_path", "")
        run_id = entry.get("run_id", "")
        exp_sha = str(entry.get("sha256") or "").strip().lower()
        exp_size = entry.get("size_bytes")
        mid = entry.get("message_id", "")
        clean_fn = entry.get("clean_filename") or entry.get("filename") or ""

        # 1. Run-ID & Containment validation
        if not is_valid_run_id(run_id):
            invalid_count += 1
            results.append({
                "attachment_id": att_id,
                "status": STATUS_INVALID,
                "decision": None,
                "message_id": mid,
                "filename": clean_fn,
                "run_id": run_id,
                "quarantine_path": rel_path,
                "reason": f"Invalid run_id: {run_id!r}",
            })
            continue

        if (
            not rel_path
            or os.path.isabs(rel_path)
            or PurePath(rel_path).is_absolute()
            or rel_path.startswith("/")
            or rel_path.startswith("\\")
            or (len(rel_path) > 1 and rel_path[1] == ":")
        ):
            invalid_count += 1
            results.append({
                "attachment_id": att_id,
                "status": STATUS_INVALID,
                "decision": None,
                "message_id": mid,
                "filename": clean_fn,
                "run_id": run_id,
                "quarantine_path": rel_path,
                "reason": f"Absolute quarantine_path rejected: {rel_path!r}",
            })
            continue

        posix_rel = PurePosixPath(rel_path)
        if any(part == ".." for part in posix_rel.parts):
            invalid_count += 1
            results.append({
                "attachment_id": att_id,
                "status": STATUS_INVALID,
                "decision": None,
                "message_id": mid,
                "filename": clean_fn,
                "run_id": run_id,
                "quarantine_path": rel_path,
                "reason": f"Directory traversal in quarantine_path: {rel_path!r}",
            })
            continue

        expected_run_prefix = PurePosixPath(f"data/mail-desk/attachments/{run_id}")
        try:
            posix_rel.relative_to(expected_run_prefix)
        except ValueError:
            invalid_count += 1
            results.append({
                "attachment_id": att_id,
                "status": STATUS_INVALID,
                "decision": None,
                "message_id": mid,
                "filename": clean_fn,
                "run_id": run_id,
                "quarantine_path": rel_path,
                "reason": f"quarantine_path '{rel_path}' does not reside under expected run prefix '{expected_run_prefix}'",
            })
            continue

        target_file = ws / posix_rel

        # Path security (symlinks / boundary escape)
        try:
            check_quarantine_path_security(target_file, attachments_root)
        except SymlinkEscapeError as err:
            invalid_count += 1
            results.append({
                "attachment_id": att_id,
                "status": STATUS_INVALID,
                "decision": None,
                "message_id": mid,
                "filename": clean_fn,
                "run_id": run_id,
                "quarantine_path": rel_path,
                "reason": f"Path security violation: {err}",
            })
            continue

        # Physical file presence & Recovery check
        if not target_file.exists():
            matching_journal = any(
                je.get("attachment_id") == att_id and je.get("state") in (
                    JOURNAL_STATE_FILE_DELETED, JOURNAL_STATE_INVENTORY_UPDATED, JOURNAL_STATE_INDEX_UPDATED
                )
                for je in journal_entries.values()
            )
            if matching_journal:
                eligible_count += 1
                results.append({
                    "attachment_id": att_id,
                    "status": STATUS_ELIGIBLE,
                    "decision": DECISION_DISCARD,
                    "message_id": mid,
                    "filename": clean_fn,
                    "run_id": run_id,
                    "quarantine_path": rel_path,
                    "reason": "Physical file deleted; pending recovery index update",
                })
            else:
                invalid_count += 1
                results.append({
                    "attachment_id": att_id,
                    "status": STATUS_INVALID,
                    "decision": None,
                    "message_id": mid,
                    "filename": clean_fn,
                    "run_id": run_id,
                    "quarantine_path": rel_path,
                    "reason": f"Physical file missing from disk without recovery journal evidence: {target_file}",
                })
            continue

        if not target_file.is_file() or target_file.is_symlink() or os.path.islink(target_file):
            invalid_count += 1
            results.append({
                "attachment_id": att_id,
                "status": STATUS_INVALID,
                "decision": None,
                "message_id": mid,
                "filename": clean_fn,
                "run_id": run_id,
                "quarantine_path": rel_path,
                "reason": f"Target path is not a regular file or is a symlink: {target_file}",
            })
            continue

        if os.name == "nt":
            try:
                stat_res = os.lstat(target_file)
                if getattr(stat_res, "st_file_attributes", 0) & 0x400:
                    invalid_count += 1
                    results.append({
                        "attachment_id": att_id,
                        "status": STATUS_INVALID,
                        "decision": None,
                        "message_id": mid,
                        "filename": clean_fn,
                        "run_id": run_id,
                        "quarantine_path": rel_path,
                        "reason": f"Windows reparse point detected: {target_file}",
                    })
                    continue
            except OSError as err:
                invalid_count += 1
                results.append({
                    "attachment_id": att_id,
                    "status": STATUS_INVALID,
                    "decision": None,
                    "message_id": mid,
                    "filename": clean_fn,
                    "run_id": run_id,
                    "quarantine_path": rel_path,
                    "reason": f"Failed to inspect file attributes: {err}",
                })
                continue

        # Physical byte verification
        try:
            actual_bytes = target_file.read_bytes()
            actual_size = len(actual_bytes)
            actual_sha = hashlib.sha256(actual_bytes).hexdigest().lower()
        except OSError as err:
            invalid_count += 1
            results.append({
                "attachment_id": att_id,
                "status": STATUS_INVALID,
                "decision": None,
                "message_id": mid,
                "filename": clean_fn,
                "run_id": run_id,
                "quarantine_path": rel_path,
                "reason": f"Failed to read disk bytes: {err}",
            })
            continue

        if exp_size is not None and actual_size != exp_size:
            invalid_count += 1
            results.append({
                "attachment_id": att_id,
                "status": STATUS_INVALID,
                "decision": None,
                "message_id": mid,
                "filename": clean_fn,
                "run_id": run_id,
                "quarantine_path": rel_path,
                "reason": f"File size drift on disk: expected {exp_size}, got {actual_size}",
            })
            continue

        if exp_sha and actual_sha != exp_sha:
            invalid_count += 1
            results.append({
                "attachment_id": att_id,
                "status": STATUS_INVALID,
                "decision": None,
                "message_id": mid,
                "filename": clean_fn,
                "run_id": run_id,
                "quarantine_path": rel_path,
                "reason": f"SHA-256 drift on disk: expected {exp_sha}, got {actual_sha}",
            })
            continue

        # Verify against .quarantine-inventory.json
        run_dir = target_file.parent
        inv_file = run_dir / INVENTORY_FILENAME
        if not inv_file.exists():
            invalid_count += 1
            results.append({
                "attachment_id": att_id,
                "status": STATUS_INVALID,
                "decision": None,
                "message_id": mid,
                "filename": clean_fn,
                "run_id": run_id,
                "quarantine_path": rel_path,
                "reason": f"Missing inventory file: {inv_file}",
            })
            continue

        try:
            inv = json.loads(inv_file.read_text(encoding="utf-8"))
            _validate_inventory_schema(inv, inv_file)
            msgs = inv.get("messages", {})
            _, msg_entry = _find_inventory_message_entry(msgs, mid)
            files_dict = msg_entry.get("files", {}) if msg_entry else {}
            inv_file_entry = files_dict.get(clean_fn)
            if not inv_file_entry:
                invalid_count += 1
                results.append({
                    "attachment_id": att_id,
                    "status": STATUS_INVALID,
                    "decision": None,
                    "message_id": mid,
                    "filename": clean_fn,
                    "run_id": run_id,
                    "quarantine_path": rel_path,
                    "reason": f"File '{clean_fn}' not found in inventory for message '{norm_mid}'",
                })
                continue
            if inv_file_entry.get("sha256", "").lower() != exp_sha:
                invalid_count += 1
                results.append({
                    "attachment_id": att_id,
                    "status": STATUS_INVALID,
                    "decision": None,
                    "message_id": mid,
                    "filename": clean_fn,
                    "run_id": run_id,
                    "quarantine_path": rel_path,
                    "reason": "SHA-256 mismatch between index and inventory",
                })
                continue
        except Exception as err:
            invalid_count += 1
            results.append({
                "attachment_id": att_id,
                "status": STATUS_INVALID,
                "decision": None,
                "message_id": mid,
                "filename": clean_fn,
                "run_id": run_id,
                "quarantine_path": rel_path,
                "reason": f"Inventory check failed: {err}",
            })
            continue

        # Check Active Run Evidence
        active_run, active_reason = check_active_run_evidence(run_id, ws)
        if active_run:
            protected_count += 1
            results.append({
                "attachment_id": att_id,
                "status": STATUS_PROTECTED,
                "decision": None,
                "message_id": mid,
                "filename": clean_fn,
                "run_id": run_id,
                "quarantine_path": rel_path,
                "reason": f"Active run evidence: {active_reason}",
            })
            continue

        # Evaluate latest disposition decision
        latest_disp = latest_decisions.get(att_id)
        if latest_disp is None:
            protected_count += 1
            results.append({
                "attachment_id": att_id,
                "status": STATUS_PROTECTED,
                "decision": None,
                "message_id": mid,
                "filename": clean_fn,
                "run_id": run_id,
                "quarantine_path": rel_path,
                "reason": "No disposition decision recorded yet",
            })
            continue

        dec = latest_disp["decision"]
        recorded_idx_hash = latest_disp["index_entry_sha256"]
        current_canonical_idx_hash = canonical_index_entry_sha256(entry)

        if recorded_idx_hash != current_canonical_idx_hash:
            invalid_count += 1
            results.append({
                "attachment_id": att_id,
                "status": STATUS_INVALID,
                "decision": dec,
                "message_id": mid,
                "filename": clean_fn,
                "run_id": run_id,
                "quarantine_path": rel_path,
                "reason": f"Quarantine index entry has drifted since decision was recorded (recorded={recorded_idx_hash}, current={current_canonical_idx_hash})",
            })
            continue

        if dec == DECISION_RETAIN:
            protected_count += 1
            results.append({
                "attachment_id": att_id,
                "status": STATUS_PROTECTED,
                "decision": DECISION_RETAIN,
                "message_id": mid,
                "filename": clean_fn,
                "run_id": run_id,
                "quarantine_path": rel_path,
                "review_after": latest_disp.get("review_after"),
                "reason": "Explicit retain decision recorded",
            })
            continue

        if dec == DECISION_PROMOTE:
            protected_count += 1
            results.append({
                "attachment_id": att_id,
                "status": STATUS_PROTECTED,
                "decision": DECISION_PROMOTE,
                "message_id": mid,
                "filename": clean_fn,
                "run_id": run_id,
                "quarantine_path": rel_path,
                "candidate_review_hash": latest_disp.get("candidate_review_hash"),
                "promotion_status": latest_disp.get("promotion_status"),
                "reason": "Explicit promote decision recorded; awaits FR-09 promotion completion",
            })
            continue

        if dec == DECISION_DISCARD:
            eligible_count += 1
            results.append({
                "attachment_id": att_id,
                "status": STATUS_ELIGIBLE,
                "decision": DECISION_DISCARD,
                "message_id": mid,
                "filename": clean_fn,
                "run_id": run_id,
                "quarantine_path": rel_path,
                "decision_id": latest_disp["decision_id"],
                "reason": "Authorized discard decision recorded and all preconditions satisfied",
            })
            continue

    return {
        "status": "completed",
        "summary": {
            "total": len(items),
            "eligible": eligible_count,
            "protected": protected_count,
            "invalid": invalid_count,
        },
        "items": results,
    }


# ==============================================================================
# Mutating Discard Apply Engine (Single Attachment Bound)
# ==============================================================================

def apply_discard(
    *,
    attachment_id: str,
    apply_receipt: Mapping[str, Any],
    workspace_root: Path | str | None = None,
    data_dir: Path | str | None = None,
    index_path: Path | str | None = None,
    log_path: Path | str | None = None,
    journal_path: Path | str | None = None,
    lease_id: str | None = None,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    """Apply authorized physical deletion and index removal for EXACTLY ONE attachment.

    Guarantees:
    - Zwingender Workspace-Lock (`DispositionLockRequiredError`).
    - Exakter Löschumfang: bulk apply is strictly forbidden; `attachment_id` is mandatory.
    - Verifiable `apply_receipt` bound to canonical apply request hash (no raw 64-hex bypass).
    - Persisted Apply-/Recovery-Journal (`attachment-discard-journal.json`) written prior to unlink.
    - Re-verification of all 10 preconditions immediately before unlinking.
    - Atomic `.quarantine-inventory.json` update; errors are never swallowed.
    - Atomic index removal only after verified successful inventory update.
    - Missing files are only recovered if valid, matching `file_deleted` journal evidence exists.
    """
    ws = Path(workspace_root or Path.cwd()).resolve()
    idx_p = resolve_quarantine_index_path(index_path, data_dir=data_dir, workspace_root=ws)
    lp = resolve_disposition_log_path(log_path, data_dir=data_dir, workspace_root=ws)
    jp = resolve_discard_journal_path(journal_path, data_dir=data_dir, workspace_root=ws)

    # 1. Lock Verification
    try:
        verify_quarantine_workspace_lock(
            workspace_root=ws,
            lease_id=lease_id,
            conversation_id=conversation_id,
            data_dir=idx_p.parent,
        )
    except Exception as err:
        raise DispositionLockRequiredError(
            f"Discard apply requires an active, owned workspace lock: {err}"
        ) from err

    # 2. Scope & Receipt Structure Verification: Exactly one attachment_id required
    if not attachment_id or not isinstance(attachment_id, str):
        raise DispositionApplyError("attachment_id is mandatory; bulk apply is forbidden")
    norm_att_id = str(attachment_id).strip().lower()
    if not SHA256_HEX_REGEX.fullmatch(norm_att_id):
        raise DispositionSchemaError(f"Invalid attachment_id: {attachment_id!r}")

    # Validate receipt contract format immediately (fail closed on malformed receipt)
    validate_receipt_structure(apply_receipt, context="apply_receipt")

    # 3. Load Quarantine Index & verify entry presence
    index_data = load_quarantine_index(idx_p)
    items = index_data.get("items", {})
    entry = items.get(norm_att_id)
    if entry is None:
        journal = load_discard_journal(jp)
        matching_entries = [
            je for je in journal.get("entries", {}).values()
            if je.get("attachment_id") == norm_att_id
        ]
        if not matching_entries:
            raise DispositionApplyError(
                f"Attachment '{norm_att_id}' not found in quarantine index '{idx_p}'"
            )

        found_valid_match = False
        for candidate_je in matching_entries:
            cand_size = candidate_je.get("size_bytes")
            if not isinstance(cand_size, int) or isinstance(cand_size, bool) or cand_size <= 0:
                continue

            try:
                reconstructed_req = build_apply_request(
                    attachment_id=candidate_je["attachment_id"],
                    decision_id=candidate_je["decision_id"],
                    index_entry_sha256=candidate_je["previous_index_entry_sha256"],
                    quarantine_path=candidate_je["quarantine_path"],
                    sha256=candidate_je["sha256"],
                    size_bytes=cand_size,
                    run_id=candidate_je["run_id"],
                    schema_version=1,
                )
            except (DispositionApplyError, DispositionSchemaError):
                continue
            req_hash = canonical_apply_request_sha256(reconstructed_req)
            if req_hash != candidate_je.get("apply_request_hash"):
                continue

            try:
                verified_rcpt = verify_apply_receipt(apply_receipt, req_hash)
            except (ReceiptError, ReceiptDriftError):
                continue

            if verified_rcpt["receipt_hash"] != candidate_je.get("apply_receipt_hash"):
                continue

            expected_jid = hashlib.sha256(
                f"{norm_att_id}:{candidate_je['decision_id']}:{candidate_je['apply_receipt_hash']}:{candidate_je['previous_index_entry_sha256']}".encode("utf-8")
            ).hexdigest()
            if candidate_je.get("journal_entry_id") != expected_jid:
                continue

            cand_status = candidate_je.get("status")
            cand_state = candidate_je.get("state")
            cand_last_succ = candidate_je.get("last_successful_state")
            cand_fail_stage = candidate_je.get("failure_stage")
            cand_err = candidate_je.get("error")

            if cand_status == "completed":
                if (
                    cand_state == JOURNAL_STATE_COMPLETED
                    and cand_last_succ == JOURNAL_STATE_COMPLETED
                    and cand_fail_stage is None
                    and cand_err is None
                ):
                    found_valid_match = True
                    break
            elif cand_last_succ == JOURNAL_STATE_INDEX_UPDATED:
                # Advance partial success (index_updated) to completed
                record_journal_state(
                    jp,
                    attachment_id=candidate_je["attachment_id"],
                    decision_id=candidate_je["decision_id"],
                    apply_receipt_hash=candidate_je["apply_receipt_hash"],
                    apply_request_hash=candidate_je["apply_request_hash"],
                    previous_index_entry_sha256=candidate_je["previous_index_entry_sha256"],
                    quarantine_path=candidate_je["quarantine_path"],
                    sha256=candidate_je["sha256"],
                    size_bytes=cand_size,
                    run_id=candidate_je["run_id"],
                    state=JOURNAL_STATE_COMPLETED,
                )
                found_valid_match = True
                break

        if not found_valid_match:
            raise DispositionApplyError(
                f"Attachment '{norm_att_id}' is not in quarantine index, and no matching completed journal entry exists for the provided apply_receipt"
            )

        return {
            "status": "completed",
            "message": f"Attachment '{norm_att_id}' already discarded and removed from index",
            "attachment_id": norm_att_id,
            "deleted_count": 0,
            "idempotent": True,
        }

    rel_path = entry["quarantine_path"]
    run_id = entry["run_id"]
    exp_sha = entry["sha256"]
    exp_size = entry.get("size_bytes")
    if not isinstance(exp_size, int) or isinstance(exp_size, bool) or exp_size <= 0:
        raise DispositionSchemaError(
            f"Attachment '{norm_att_id}' index entry invalid size_bytes: {exp_size!r} (must be positive integer > 0)"
        )
    mid = entry["message_id"]
    clean_fn = entry.get("clean_filename") or entry.get("filename")
    expected_idx_hash = canonical_index_entry_sha256(entry)

    # 4. Load Dispositions Log & verify latest decision is 'discard'
    log_data = load_disposition_log(lp)
    latest_disp = log_data["latest_by_attachment_id"].get(norm_att_id)
    if not latest_disp:
        raise DispositionApplyError(
            f"No disposition decision recorded for attachment '{norm_att_id}' in '{lp}'"
        )
    if latest_disp["decision"] != DECISION_DISCARD:
        raise DispositionApplyError(
            f"Attachment '{norm_att_id}' latest decision is '{latest_disp['decision']}', not 'discard'"
        )
    if latest_disp["index_entry_sha256"] != expected_idx_hash:
        raise DispositionDriftError(
            f"Attachment '{norm_att_id}' index entry has drifted since discard decision was recorded"
        )
    decision_id = latest_disp["decision_id"]

    # 5. Build Canonical Apply Request & Verify Receipt
    apply_req = build_apply_request(
        attachment_id=norm_att_id,
        decision_id=decision_id,
        index_entry_sha256=expected_idx_hash,
        quarantine_path=rel_path,
        sha256=exp_sha,
        size_bytes=exp_size,
        run_id=run_id,
        schema_version=1,
    )
    computed_apply_req_hash = canonical_apply_request_sha256(apply_req)

    verified_apply_receipt_info = verify_apply_receipt(apply_receipt, computed_apply_req_hash)
    apply_receipt_hash = verified_apply_receipt_info["receipt_hash"]

    # 6. Physical File & Recovery Evidence Check
    attachments_root = (ws / "data" / "mail-desk" / "attachments").resolve()
    target_file = ws / PurePosixPath(rel_path)

    journal = load_discard_journal(jp)
    journal_entry_id = hashlib.sha256(
        f"{norm_att_id}:{decision_id}:{apply_receipt_hash}:{expected_idx_hash}".encode("utf-8")
    ).hexdigest()
    existing_journal_entry = journal.get("entries", {}).get(journal_entry_id)

    if not target_file.exists():
        if (
            not existing_journal_entry
            or existing_journal_entry.get("attachment_id") != norm_att_id
            or existing_journal_entry.get("decision_id") != decision_id
            or existing_journal_entry.get("apply_receipt_hash") != apply_receipt_hash
            or existing_journal_entry.get("previous_index_entry_sha256") != expected_idx_hash
        ):
            raise RecoveryEvidenceMissingError(
                f"Target file '{target_file}' is missing from disk, but no valid matching file_deleted recovery journal entry exists for receipt '{apply_receipt_hash}'. Index will not be modified."
            )

        current_state = existing_journal_entry.get("last_successful_state") or existing_journal_entry.get("state")
        if current_state not in (
            JOURNAL_STATE_FILE_DELETED,
            JOURNAL_STATE_INVENTORY_UPDATED,
            JOURNAL_STATE_INDEX_UPDATED,
            JOURNAL_STATE_COMPLETED,
        ):
            raise RecoveryEvidenceMissingError(
                f"Target file '{target_file}' is missing from disk, but recovery journal state is '{current_state}', not in (file_deleted, inventory_updated, index_updated, completed)."
            )

        run_dir = ws / "data" / "mail-desk" / "attachments" / run_id

        if current_state == JOURNAL_STATE_FILE_DELETED:
            try:
                with _QuarantineInventoryLock(run_dir):
                    update_quarantine_inventory_atomic(run_dir, mid, clean_fn)
                record_journal_state(
                    jp,
                    attachment_id=norm_att_id,
                    decision_id=decision_id,
                    apply_receipt_hash=apply_receipt_hash,
                    apply_request_hash=computed_apply_req_hash,
                    previous_index_entry_sha256=expected_idx_hash,
                    quarantine_path=rel_path,
                    sha256=exp_sha,
                    size_bytes=exp_size,
                    run_id=run_id,
                    state=JOURNAL_STATE_INVENTORY_UPDATED,
                )
                current_state = JOURNAL_STATE_INVENTORY_UPDATED
            except Exception as err:
                record_journal_failure(
                    jp,
                    attachment_id=norm_att_id,
                    decision_id=decision_id,
                    apply_receipt_hash=apply_receipt_hash,
                    apply_request_hash=computed_apply_req_hash,
                    previous_index_entry_sha256=expected_idx_hash,
                    quarantine_path=rel_path,
                    sha256=exp_sha,
                    size_bytes=exp_size,
                    run_id=run_id,
                    stage="inventory_update",
                    error=str(err),
                )
                raise DispositionApplyError(f"Recovery failed at inventory update: {err}") from err

        if current_state == JOURNAL_STATE_INVENTORY_UPDATED:
            remove_quarantine_entry(
                idx_p,
                attachment_id=norm_att_id,
                workspace_root=ws,
                lease_id=lease_id,
                conversation_id=conversation_id,
            )
            record_journal_state(
                jp,
                attachment_id=norm_att_id,
                decision_id=decision_id,
                apply_receipt_hash=apply_receipt_hash,
                apply_request_hash=computed_apply_req_hash,
                previous_index_entry_sha256=expected_idx_hash,
                quarantine_path=rel_path,
                sha256=exp_sha,
                size_bytes=exp_size,
                run_id=run_id,
                state=JOURNAL_STATE_INDEX_UPDATED,
            )
            current_state = JOURNAL_STATE_INDEX_UPDATED

        if current_state == JOURNAL_STATE_INDEX_UPDATED:
            record_journal_state(
                jp,
                attachment_id=norm_att_id,
                decision_id=decision_id,
                apply_receipt_hash=apply_receipt_hash,
                apply_request_hash=computed_apply_req_hash,
                previous_index_entry_sha256=expected_idx_hash,
                quarantine_path=rel_path,
                sha256=exp_sha,
                size_bytes=exp_size,
                run_id=run_id,
                state=JOURNAL_STATE_COMPLETED,
            )

        return {
            "status": "completed",
            "message": "Recovered discard apply from journal evidence",
            "attachment_id": norm_att_id,
            "deleted_count": 1,
            "apply_receipt_hash": apply_receipt_hash,
            "recovered": True,
        }

    # 7. Pre-Unlink Gate: Re-verify all 10 conditions immediately before unlinking
    active_run, active_reason = check_active_run_evidence(run_id, ws)
    if active_run:
        raise DispositionApplyError(f"Active run evidence detected before unlink: {active_reason}")

    posix_rel = PurePosixPath(rel_path)
    expected_run_prefix = PurePosixPath(f"data/mail-desk/attachments/{run_id}")
    try:
        posix_rel.relative_to(expected_run_prefix)
    except ValueError as err:
        raise DispositionApplyError(f"Path containment violation: {err}") from err

    check_quarantine_path_security(target_file, attachments_root)
    if target_file.is_symlink() or os.path.islink(target_file):
        raise DispositionApplyError("Target file is a symlink")
    if os.name == "nt":
        stat_res = os.lstat(target_file)
        if getattr(stat_res, "st_file_attributes", 0) & 0x400:
            raise DispositionApplyError("Windows reparse point detected on target file")

    actual_bytes = target_file.read_bytes()
    if len(actual_bytes) != exp_size:
        raise PhysicalVerificationError(
            f"File size drift immediately before unlink: expected {exp_size}, got {len(actual_bytes)}"
        )
    actual_sha = hashlib.sha256(actual_bytes).hexdigest().lower()
    if actual_sha != exp_sha:
        raise PhysicalVerificationError(
            f"File SHA-256 drift immediately before unlink: expected {exp_sha}, got {actual_sha}"
        )

    run_dir = target_file.parent
    inv_file = run_dir / INVENTORY_FILENAME
    if not inv_file.exists():
        raise PhysicalVerificationError(f"Inventory missing immediately before unlink: {inv_file}")
    inv = json.loads(inv_file.read_text(encoding="utf-8"))
    _validate_inventory_schema(inv, inv_file)
    _, msg_entry = _find_inventory_message_entry(inv.get("messages", {}), mid)
    file_inv = msg_entry.get("files", {}).get(clean_fn) if msg_entry else None
    if not file_inv or file_inv.get("sha256", "").lower() != exp_sha:
        raise PhysicalVerificationError("Inventory entry mismatch immediately before unlink")

    # 8. Step 1: Journal State 'prepared'
    record_journal_state(
        jp,
        attachment_id=norm_att_id,
        decision_id=decision_id,
        apply_receipt_hash=apply_receipt_hash,
        apply_request_hash=computed_apply_req_hash,
        previous_index_entry_sha256=expected_idx_hash,
        quarantine_path=rel_path,
        sha256=exp_sha,
        size_bytes=exp_size,
        run_id=run_id,
        state=JOURNAL_STATE_PREPARED,
    )

    # 9. Step 2: Physical Unlink
    try:
        target_file.unlink()
    except OSError as err:
        record_journal_failure(
            jp,
            attachment_id=norm_att_id,
            decision_id=decision_id,
            apply_receipt_hash=apply_receipt_hash,
            apply_request_hash=computed_apply_req_hash,
            previous_index_entry_sha256=expected_idx_hash,
            quarantine_path=rel_path,
            sha256=exp_sha,
            size_bytes=exp_size,
            run_id=run_id,
            stage="unlink",
            error=str(err),
        )
        raise DispositionApplyError(f"Failed to unlink target file '{target_file}': {err}") from err

    if target_file.exists():
        record_journal_failure(
            jp,
            attachment_id=norm_att_id,
            decision_id=decision_id,
            apply_receipt_hash=apply_receipt_hash,
            apply_request_hash=computed_apply_req_hash,
            previous_index_entry_sha256=expected_idx_hash,
            quarantine_path=rel_path,
            sha256=exp_sha,
            size_bytes=exp_size,
            run_id=run_id,
            stage="unlink_verification",
            error="File still exists on disk",
        )
        raise DispositionApplyError("Target file still exists on disk after unlink")

    # Record journal state 'file_deleted'
    record_journal_state(
        jp,
        attachment_id=norm_att_id,
        decision_id=decision_id,
        apply_receipt_hash=apply_receipt_hash,
        apply_request_hash=computed_apply_req_hash,
        previous_index_entry_sha256=expected_idx_hash,
        quarantine_path=rel_path,
        sha256=exp_sha,
        size_bytes=exp_size,
        run_id=run_id,
        state=JOURNAL_STATE_FILE_DELETED,
    )

    # 10. Step 3: Atomic Inventory Mutation
    try:
        with _QuarantineInventoryLock(run_dir):
            update_quarantine_inventory_atomic(run_dir, mid, clean_fn)
    except Exception as err:
        record_journal_failure(
            jp,
            attachment_id=norm_att_id,
            decision_id=decision_id,
            apply_receipt_hash=apply_receipt_hash,
            apply_request_hash=computed_apply_req_hash,
            previous_index_entry_sha256=expected_idx_hash,
            quarantine_path=rel_path,
            sha256=exp_sha,
            size_bytes=exp_size,
            run_id=run_id,
            stage="inventory_update",
            error=str(err),
        )
        raise DispositionApplyError(
            f"Physical file was deleted, but inventory update failed: {err}. Index left unchanged for recovery."
        ) from err

    # Record journal state 'inventory_updated'
    record_journal_state(
        jp,
        attachment_id=norm_att_id,
        decision_id=decision_id,
        apply_receipt_hash=apply_receipt_hash,
        apply_request_hash=computed_apply_req_hash,
        previous_index_entry_sha256=expected_idx_hash,
        quarantine_path=rel_path,
        sha256=exp_sha,
        size_bytes=exp_size,
        run_id=run_id,
        state=JOURNAL_STATE_INVENTORY_UPDATED,
    )

    # 11. Step 4: Atomic Index Removal
    remove_quarantine_entry(
        idx_p,
        attachment_id=norm_att_id,
        workspace_root=ws,
        lease_id=lease_id,
        conversation_id=conversation_id,
    )

    # Record journal state 'index_updated'
    record_journal_state(
        jp,
        attachment_id=norm_att_id,
        decision_id=decision_id,
        apply_receipt_hash=apply_receipt_hash,
        apply_request_hash=computed_apply_req_hash,
        previous_index_entry_sha256=expected_idx_hash,
        quarantine_path=rel_path,
        sha256=exp_sha,
        size_bytes=exp_size,
        run_id=run_id,
        state=JOURNAL_STATE_INDEX_UPDATED,
    )

    # Record journal state 'completed'
    record_journal_state(
        jp,
        attachment_id=norm_att_id,
        decision_id=decision_id,
        apply_receipt_hash=apply_receipt_hash,
        apply_request_hash=computed_apply_req_hash,
        previous_index_entry_sha256=expected_idx_hash,
        quarantine_path=rel_path,
        sha256=exp_sha,
        size_bytes=exp_size,
        run_id=run_id,
        state=JOURNAL_STATE_COMPLETED,
    )

    return {
        "status": "completed",
        "attachment_id": norm_att_id,
        "quarantine_path": rel_path,
        "clean_filename": clean_fn,
        "message_id": mid,
        "sha256": exp_sha,
        "apply_receipt_hash": apply_receipt_hash,
        "deleted_count": 1,
    }
