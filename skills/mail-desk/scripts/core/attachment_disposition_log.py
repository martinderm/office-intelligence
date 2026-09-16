"""Deterministic script-based attachment disposition log and safe cleanup engine (MD-Q3).

Provides:
- Append-only versioned logging for `data/mail-desk/attachment-disposition-log.jsonl`.
- Fail-closed Schema 1 validation and deterministic hash-bound `decision_id` computation.
- Read-only reporting classifying quarantined attachments into `eligible`, `protected`, and `invalid`.
- Separately authorized, atomic discard-apply engine enforcing 10 pre-conditions under workspace lock.
- FR-09 promotion link without implementing a duplicate promotion engine or cloud-sync.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePath, PurePosixPath
import re
import sys
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
    _load_workspace_lock_guard,
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


# ==============================================================================
# Constants & Enums
# ==============================================================================

DISPOSITION_LOG_FILENAME = "attachment-disposition-log.jsonl"

DECISION_RETAIN = "retain"
DECISION_DISCARD = "discard"
DECISION_PROMOTE = "promote"

ALLOWED_DECISIONS = {DECISION_RETAIN, DECISION_DISCARD, DECISION_PROMOTE}

STATUS_ELIGIBLE = "eligible"
STATUS_PROTECTED = "protected"
STATUS_INVALID = "invalid"

ALLOWED_REPORT_STATUSES = {STATUS_ELIGIBLE, STATUS_PROTECTED, STATUS_INVALID}

RATIONALE_MAX_LENGTH = 256
PROMOTION_ID_MAX_LENGTH = 128
ALLOWED_PROMOTION_STATUSES = {"pending", "promoted", "failed", "rejected"}

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


# ==============================================================================
# Deterministic Identity & Hash Binding
# ==============================================================================

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
# Schema 1 Validation
# ==============================================================================

def validate_disposition_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Strictly validate a disposition entry against Schema 1 fail-closed.

    Enforces:
    - Rejection of unknown root fields.
    - Recursive rejection of forbidden content keys (prompt, credentials, body, tokens, etc.).
    - Strict regex matching on attachment_id, index_entry_sha256, human_receipt_hash, timestamp.
    - Exclusive decision semantics:
      - 'retain': optional review_after (RFC-3339), forbidden candidate_review_hash / promotion fields.
      - 'promote': mandatory candidate_review_hash (64-hex), forbidden review_after.
      - 'discard': forbidden candidate_review_hash, review_after, promotion fields.
    - Bounded rationale without multiline breaks or path traversal.
    - Deterministic decision_id derivation and fail-closed identity drift detection.
    """
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
        if "\n" in rat_str or "\r" in rat_str:
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

        # Duplicate ID check: idempotent repetition vs drift
        existing = by_decision_id.get(dec_id)
        if existing is not None:
            if existing != validated:
                raise DispositionDriftError(
                    f"Duplicate decision_id '{dec_id}' with conflicting content on line {line_num}"
                )
            # Identical duplicate: accept as no-op without duplicating in index list
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
    workspace_root: Path | str | None = None,
    data_dir: Path | str | None = None,
    index_path: Path | str | None = None,
    lease_id: str | None = None,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    """Append a validated disposition decision into attachment-disposition-log.jsonl under workspace lock.

    Guarantees:
    - Strictly enforces active, invocation-owned workspace lock (zero legacy bypass).
    - Validates attachment_id exists in attachment-quarantine-index.json.
    - Validates index_entry_sha256 matches the current canonical hash of the index entry on disk.
    - Strictly validates entry fields against Schema 1 and rejects forbidden content.
    - Pure idempotent repetition for identical decision entries (no-op).
    - Fail-closed drift abort if same decision_id has differing content.
    - Append-only write: existing bytes are NEVER rewritten or truncated.
    """
    ws = Path(workspace_root or Path.cwd()).resolve()
    idx_p = resolve_quarantine_index_path(index_path, data_dir=data_dir, workspace_root=ws)
    lp = resolve_disposition_log_path(log_path, data_dir=data_dir, workspace_root=ws)

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
            f"Disposition log mutation requires an active, owned workspace lock: {err}"
        ) from err

    # 2. Validate entry schema
    canonical_entry = validate_disposition_entry(payload)
    att_id = canonical_entry["attachment_id"]
    dec_id = canonical_entry["decision_id"]
    declared_idx_hash = canonical_entry["index_entry_sha256"]

    # 3. Verify against quarantine index on disk
    index_data = load_quarantine_index(idx_p)
    items = index_data.get("items", {})
    if att_id not in items:
        raise KeyError(
            f"Attachment '{att_id}' does not exist in quarantine index '{idx_p}'"
        )

    current_idx_entry = items[att_id]
    actual_idx_hash = canonical_index_entry_sha256(current_idx_entry)

    if actual_idx_hash != declared_idx_hash:
        raise AttachmentIndexDriftError(
            f"Quarantine index entry hash drift for '{att_id}': "
            f"log declares '{declared_idx_hash}', current index computes '{actual_idx_hash}'"
        )

    # 4. Check existing disposition log for idempotency and drift
    log_data = load_disposition_log(lp)
    existing = log_data["by_decision_id"].get(dec_id)
    if existing is not None:
        if existing == canonical_entry:
            return {
                "status": "unchanged",
                "decision_id": dec_id,
                "entry": existing,
            }
        raise DispositionDriftError(
            f"Conflicting existing decision for decision_id '{dec_id}' in disposition log"
        )

    # 5. Append-only write with flush and fsync
    lp.parent.mkdir(parents=True, exist_ok=True)
    serialized_line = json.dumps(canonical_entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"

    with open(lp, "a", encoding="utf-8") as fh:
        fh.write(serialized_line)
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError:
            pass

    return {
        "status": "recorded",
        "decision_id": dec_id,
        "entry": canonical_entry,
    }


# ==============================================================================
# Active Run & Promotion State Inspection
# ==============================================================================

def check_active_run_evidence(run_id: str, workspace_root: Path) -> tuple[bool, str | None]:
    """Inspect whether a quarantine run is currently active or locked."""
    if not is_valid_run_id(run_id):
        return True, f"Invalid run_id: {run_id!r}"

    run_dir = workspace_root / "data" / "mail-desk" / "attachments" / run_id
    if not run_dir.exists():
        return False, None

    # Check .quarantine-inventory.lock
    lock_file = run_dir / ".quarantine-inventory.lock"
    if lock_file.exists():
        return True, f"Active inventory lock found at '{lock_file}'"

    # Check temp files
    try:
        temp_files = [p for p in run_dir.iterdir() if p.name.endswith(".tmp") or p.name.startswith(".inv.")]
        if temp_files:
            return True, f"Active temporary files present in run directory: {[p.name for p in temp_files]}"
    except OSError as err:
        return True, f"Failed to inspect run directory: {err}"

    # Check batch-recovery-journal.json
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
# Read-Only Reporting
# ==============================================================================

def report_dispositions(
    *,
    workspace_root: Path | str | None = None,
    data_dir: Path | str | None = None,
    index_path: Path | str | None = None,
    log_path: Path | str | None = None,
) -> dict[str, Any]:
    """Perform strictly read-only reporting classifying every quarantined attachment.

    Guarantees:
    - Strictly read-only: mutates neither the index, log, inventories, nor disk files.
    - Classifies each entry into exactly one of: 'eligible', 'protected', 'invalid'.
    """
    ws = Path(workspace_root or Path.cwd()).resolve()
    idx_p = resolve_quarantine_index_path(index_path, data_dir=data_dir, workspace_root=ws)
    lp = resolve_disposition_log_path(log_path, data_dir=data_dir, workspace_root=ws)

    try:
        index_data = load_quarantine_index(idx_p)
        items = index_data.get("items", {})
    except Exception:
        # Fall back to raw JSON parsing so individual invalid items can still be classified
        try:
            raw_text = idx_p.read_text(encoding="utf-8")
            raw_data = json.loads(raw_text)
            items = raw_data.get("items", {}) if isinstance(raw_data, dict) else {}
        except Exception:
            items = {}

    log_data = load_disposition_log(lp)
    latest_decisions = log_data.get("latest_by_attachment_id", {})

    attachments_root = (ws / "data" / "mail-desk" / "attachments").resolve()

    results: list[dict[str, Any]] = []
    eligible_count = 0
    protected_count = 0
    invalid_count = 0

    for att_id, entry in items.items():
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

        # Physical file presence
        if not target_file.exists():
            invalid_count += 1
            results.append({
                "attachment_id": att_id,
                "status": STATUS_INVALID,
                "decision": None,
                "message_id": mid,
                "filename": clean_fn,
                "run_id": run_id,
                "quarantine_path": rel_path,
                "reason": f"Physical file missing from disk: {target_file}",
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
                "reason": "Target file is not a regular file or is a symlink",
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
                        "reason": "Windows reparse point detected on target file",
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

        # Verify disk bytes & SHA-256
        actual_bytes = target_file.read_bytes()
        actual_size = len(actual_bytes)
        actual_sha = hashlib.sha256(actual_bytes).hexdigest().lower()

        if actual_size != exp_size or actual_sha != exp_sha:
            invalid_count += 1
            results.append({
                "attachment_id": att_id,
                "status": STATUS_INVALID,
                "decision": None,
                "message_id": mid,
                "filename": clean_fn,
                "run_id": run_id,
                "quarantine_path": rel_path,
                "reason": f"File hash or size drift: actual({actual_size}B, {actual_sha}) != expected({exp_size}B, {exp_sha})",
            })
            continue

        # Verify inventory
        try:
            verify_quarantine_attachment_artifact(
                run_id=run_id,
                relative_path=rel_path,
                expected_sha256=exp_sha,
                expected_size_bytes=exp_size,
                message_id=mid,
                workspace_root=ws,
                clean_filename=clean_fn,
            )
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
                "reason": f"Inventory verification failure: {err}",
            })
            continue

        # 2. Check Disposition Log
        disp = latest_decisions.get(att_id)
        if disp is None:
            protected_count += 1
            results.append({
                "attachment_id": att_id,
                "status": STATUS_PROTECTED,
                "decision": None,
                "message_id": mid,
                "filename": clean_fn,
                "run_id": run_id,
                "quarantine_path": rel_path,
                "reason": "No disposition decision recorded; quarantined by default",
            })
            continue

        # Check index entry hash drift between index and disposition log
        current_canonical_idx_hash = canonical_index_entry_sha256(entry)
        if disp["index_entry_sha256"] != current_canonical_idx_hash:
            invalid_count += 1
            results.append({
                "attachment_id": att_id,
                "status": STATUS_INVALID,
                "decision": disp["decision"],
                "message_id": mid,
                "filename": clean_fn,
                "run_id": run_id,
                "quarantine_path": rel_path,
                "reason": f"Disposition binds index entry hash '{disp['index_entry_sha256']}', but current index computes '{current_canonical_idx_hash}'",
            })
            continue

        decision_type = disp["decision"]

        # 3. Decision classification
        if decision_type == DECISION_RETAIN:
            protected_count += 1
            review_after = disp.get("review_after")
            results.append({
                "attachment_id": att_id,
                "status": STATUS_PROTECTED,
                "decision": DECISION_RETAIN,
                "message_id": mid,
                "filename": clean_fn,
                "run_id": run_id,
                "quarantine_path": rel_path,
                "review_after": review_after,
                "reason": f"Retained per decision '{disp['decision_id']}'" + (f" (review_after: {review_after})" if review_after else ""),
            })
            continue

        if decision_type == DECISION_PROMOTE:
            protected_count += 1
            results.append({
                "attachment_id": att_id,
                "status": STATUS_PROTECTED,
                "decision": DECISION_PROMOTE,
                "message_id": mid,
                "filename": clean_fn,
                "run_id": run_id,
                "quarantine_path": rel_path,
                "candidate_review_hash": disp.get("candidate_review_hash"),
                "promotion_status": disp.get("promotion_status") or "pending",
                "reason": f"Promote decision '{disp['decision_id']}' pending or bound to FR-09",
            })
            continue

        if decision_type == DECISION_DISCARD:
            # Check for active run
            is_active, active_reason = check_active_run_evidence(run_id, ws)
            if is_active:
                protected_count += 1
                results.append({
                    "attachment_id": att_id,
                    "status": STATUS_PROTECTED,
                    "decision": DECISION_DISCARD,
                    "message_id": mid,
                    "filename": clean_fn,
                    "run_id": run_id,
                    "quarantine_path": rel_path,
                    "reason": f"Active run protects quarantine file: {active_reason}",
                })
                continue

            # Eligible for discard cleanup
            eligible_count += 1
            results.append({
                "attachment_id": att_id,
                "status": STATUS_ELIGIBLE,
                "decision": DECISION_DISCARD,
                "decision_id": disp["decision_id"],
                "message_id": mid,
                "filename": clean_fn,
                "run_id": run_id,
                "quarantine_path": rel_path,
                "human_receipt_hash": disp["human_receipt_hash"],
                "reason": "Valid discard decision and verified physical state; eligible for apply_discard",
            })

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
# Discard-Apply Engine
# ==============================================================================

def apply_discard(
    *,
    apply_receipt_hash: str,
    attachment_id: str | None = None,
    workspace_root: Path | str | None = None,
    data_dir: Path | str | None = None,
    index_path: Path | str | None = None,
    log_path: Path | str | None = None,
    lease_id: str | None = None,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    """Execute authorized physical deletion and atomic index update for eligible discard items.

    Guarantees:
    - Enforces verified workspace lock ownership (zero legacy bypass).
    - Requires valid 64-hex apply_receipt_hash.
    - Re-verifies all 10 pre-conditions per attachment immediately before file deletion.
    - Updates .quarantine-inventory.json under inventory lock upon physical file deletion.
    - Atomically updates attachment-quarantine-index.json only AFTER physical deletion.
    - Does NOT claim a whole run is cleaned if only individual files were deleted.
    - Fully idempotent: retry after partial failure completes pending index updates cleanly.
    - Dispositionslog remains strictly append-only and unmodified.
    """
    ws = Path(workspace_root or Path.cwd()).resolve()
    idx_p = resolve_quarantine_index_path(index_path, data_dir=data_dir, workspace_root=ws)
    lp = resolve_disposition_log_path(log_path, data_dir=data_dir, workspace_root=ws)

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

    # 2. Validate Apply Receipt Hash
    if not apply_receipt_hash or not isinstance(apply_receipt_hash, str):
        raise DispositionApplyError("Missing required 'apply_receipt_hash'")
    norm_apply_hash = apply_receipt_hash.strip().lower()
    if not SHA256_HEX_REGEX.fullmatch(norm_apply_hash):
        raise DispositionApplyError(
            f"Invalid 'apply_receipt_hash': must be 64-hex SHA-256, got {apply_receipt_hash!r}"
        )

    # 3. Read-only Report to find eligible candidates
    report = report_dispositions(
        workspace_root=ws,
        data_dir=data_dir,
        index_path=idx_p,
        log_path=lp,
    )

    eligible_map = {item["attachment_id"]: item for item in report["items"] if item["status"] == STATUS_ELIGIBLE}

    # If specific attachment requested:
    target_ids: list[str] = []
    if attachment_id is not None:
        norm_target_id = str(attachment_id).strip().lower()
        if not SHA256_HEX_REGEX.fullmatch(norm_target_id):
            raise DispositionSchemaError(f"Invalid attachment_id: {attachment_id!r}")
        if norm_target_id not in eligible_map:
            # Check if this item is in recovery state (already deleted on disk, but index update pending)
            index_data = load_quarantine_index(idx_p)
            log_data = load_disposition_log(lp)
            items = index_data.get("items", {})
            if norm_target_id in items:
                latest_disp = log_data["latest_by_attachment_id"].get(norm_target_id)
                if latest_disp and latest_disp["decision"] == DECISION_DISCARD:
                    target_file = ws / PurePosixPath(items[norm_target_id]["quarantine_path"])
                    if not target_file.exists():
                        # Recovery candidate!
                        target_ids.append(norm_target_id)
            if not target_ids:
                raise DispositionApplyError(
                    f"Attachment '{norm_target_id}' is not eligible for discard (current status is protected or invalid)"
                )
        else:
            target_ids.append(norm_target_id)
    else:
        target_ids = list(eligible_map.keys())

    if not target_ids:
        return {
            "status": "unchanged",
            "message": "No eligible attachments to discard",
            "deleted_count": 0,
            "deleted_files": [],
            "failed_files": [],
        }

    deleted_files: list[dict[str, Any]] = []
    failed_files: list[dict[str, Any]] = []

    attachments_root = (ws / "data" / "mail-desk" / "attachments").resolve()

    # Load current index data under lock for mutations
    index_data = load_quarantine_index(idx_p)
    items = index_data.setdefault("items", {})

    for att_id in target_ids:
        entry = items.get(att_id)
        if entry is None:
            # Already removed from index (idempotent no-op)
            continue

        rel_path = entry["quarantine_path"]
        run_id = entry["run_id"]
        exp_sha = entry["sha256"]
        exp_size = entry["size_bytes"]
        mid = entry["message_id"]
        clean_fn = entry.get("clean_filename") or entry.get("filename")

        target_file = ws / PurePosixPath(rel_path)

        # Case A: File exists -> perform verified deletion
        if target_file.exists():
            # Security re-check immediately before unlinking
            try:
                check_quarantine_path_security(target_file, attachments_root)
            except SymlinkEscapeError as err:
                failed_files.append({"attachment_id": att_id, "error": f"Security violation: {err}"})
                continue

            if target_file.is_symlink() or os.path.islink(target_file):
                failed_files.append({"attachment_id": att_id, "error": "Target is a symlink"})
                continue

            if os.name == "nt":
                try:
                    stat_res = os.lstat(target_file)
                    if getattr(stat_res, "st_file_attributes", 0) & 0x400:
                        failed_files.append({"attachment_id": att_id, "error": "Windows reparse point detected"})
                        continue
                except OSError as err:
                    failed_files.append({"attachment_id": att_id, "error": f"Failed to check attributes: {err}"})
                    continue

            # Verify bytes
            actual_bytes = target_file.read_bytes()
            if len(actual_bytes) != exp_size or hashlib.sha256(actual_bytes).hexdigest().lower() != exp_sha:
                failed_files.append({"attachment_id": att_id, "error": "Disk bytes drifted before unlink"})
                continue

            # Physical deletion
            try:
                target_file.unlink()
            except OSError as err:
                failed_files.append({"attachment_id": att_id, "error": f"Failed to unlink physical file: {err}"})
                continue

            if target_file.exists():
                failed_files.append({"attachment_id": att_id, "error": "File still exists after unlink"})
                continue

            # Update .quarantine-inventory.json under lock
            run_dir = target_file.parent
            try:
                with _QuarantineInventoryLock(run_dir):
                    inv_file = run_dir / INVENTORY_FILENAME
                    if inv_file.exists():
                        try:
                            inv = json.loads(inv_file.read_text(encoding="utf-8"))
                            msgs = inv.get("messages", {})
                            norm_mid = normalize_message_id(mid) or "__default__"
                            msg_entry = msgs.get(norm_mid)
                            if msg_entry and isinstance(msg_entry.get("files"), dict):
                                files_dict = msg_entry["files"]
                                files_dict.pop(clean_fn, None)
                                msg_entry["count"] = len(files_dict)
                                msg_entry["total_bytes"] = sum(f.get("size_bytes", 0) for f in files_dict.values())
                                if msg_entry["count"] == 0:
                                    msgs.pop(norm_mid, None)
                                inv_file.write_text(json.dumps(inv, indent=2, sort_keys=True), encoding="utf-8")
                        except Exception:
                            pass
            except Exception:
                pass

            # Atomically update quarantine index (remove discarded item)
            items.pop(att_id, None)
            index_data["updated_at"] = utc_now_iso()
            save_quarantine_index_atomic(idx_p, index_data)

            deleted_files.append({
                "attachment_id": att_id,
                "quarantine_path": rel_path,
                "clean_filename": clean_fn,
                "message_id": mid,
                "sha256": exp_sha,
                "status": "deleted",
            })

        else:
            # Case B: File already gone on disk (Recovery after aborted index update)
            items.pop(att_id, None)
            index_data["updated_at"] = utc_now_iso()
            save_quarantine_index_atomic(idx_p, index_data)

            deleted_files.append({
                "attachment_id": att_id,
                "quarantine_path": rel_path,
                "clean_filename": clean_fn,
                "message_id": mid,
                "sha256": exp_sha,
                "status": "recovered_index_updated",
            })

    status_str = "completed" if not failed_files else ("partial" if deleted_files else "failed")

    return {
        "status": status_str,
        "apply_receipt_hash": norm_apply_hash,
        "deleted_count": len(deleted_files),
        "deleted_files": deleted_files,
        "failed_files": failed_files,
    }
