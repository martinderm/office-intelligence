"""Deterministic script-based versioned attachment quarantine index (MD-Q2).

Provides Schema 1 storage and verification for `data/mail-desk/attachment-quarantine-index.json`.
Strictly enforces workspace lock ownership, atomic writes, deterministic attachment_id derivation,
renewed physical inventory verification, symlink/reparse point safety, fail-closed drift detection,
and read-only reconciliation without mutation.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePath, PurePosixPath
import re
import sys
import tempfile
import time
from typing import Any

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
)


# ==============================================================================
# Exceptions
# ==============================================================================

class QuarantineIndexError(ValueError):
    """Base exception for all quarantine index errors."""


class WorkspaceLockRequiredError(QuarantineIndexError):
    """Raised when mutation is attempted without a valid, owned workspace lock."""


class AttachmentIndexDriftError(QuarantineIndexError):
    """Raised when an attachment drifts in identity, hash, size, path, or inventory."""


class AttachmentIndexSchemaError(QuarantineIndexError):
    """Raised when an index entry violates Schema 1 or contains unknown fields/status values."""


class ForbiddenContentError(QuarantineIndexError):
    """Raised when forbidden content (mail body, extracted text, prompts, credentials, envelope-id) is present."""


class PhysicalVerificationError(QuarantineIndexError):
    """Raised when physical file or quarantine inventory verification fails."""


# ==============================================================================
# Constants & Enums
# ==============================================================================

INDEX_FILENAME = "attachment-quarantine-index.json"
SCHEMA_VERSION = 1

LIFECYCLE_STATE_QUARANTINED = "quarantined"
ANALYSIS_STATUS_COMPLETED = "completed"

ALLOWED_LIFECYCLE_STATES = {LIFECYCLE_STATE_QUARANTINED}
ALLOWED_ANALYSIS_STATUSES = {ANALYSIS_STATUS_COMPLETED}

ANALYSIS_COMPLETENESS_FULL = "full"
ANALYSIS_COMPLETENESS_TRUNCATED = "truncated"
ANALYSIS_COMPLETENESS_PARTIAL = "partial"
ANALYSIS_COMPLETENESS_UNAVAILABLE = "unavailable"
ANALYSIS_COMPLETENESS_UNKNOWN = "unknown"

ALLOWED_ANALYSIS_COMPLETENESS = {
    ANALYSIS_COMPLETENESS_FULL,
    ANALYSIS_COMPLETENESS_TRUNCATED,
    ANALYSIS_COMPLETENESS_PARTIAL,
    ANALYSIS_COMPLETENESS_UNAVAILABLE,
}

TRUNCATION_STAGE_NONE = "none"
TRUNCATION_STAGE_EXTRACTION = "extraction"
TRUNCATION_STAGE_HANDOFF_PER_ATTACHMENT = "handoff_per_attachment"
TRUNCATION_STAGE_HANDOFF_CUMULATIVE_MAIL = "handoff_cumulative_mail"

ALLOWED_TRUNCATION_STAGES = {
    TRUNCATION_STAGE_NONE,
    TRUNCATION_STAGE_EXTRACTION,
    TRUNCATION_STAGE_HANDOFF_PER_ATTACHMENT,
    TRUNCATION_STAGE_HANDOFF_CUMULATIVE_MAIL,
}

ALLOWED_TRUNCATION_REASONS = {
    None,
    "",
    "max_chars_exceeded",
    "max_pages_exceeded",
    "ocr_page_limit_exceeded",
    "ocr_unavailable",
    "max_paragraphs_exceeded",
    "grid_limit_exceeded",
    "max_slides_exceeded",
    "timeout_exceeded",
}

PART_LOCATOR_REGEX = re.compile(r"^\d+(?:\.\d+)*$")
MIME_TYPE_REGEX = re.compile(r"^[a-zA-Z0-9!#$&^_.+-]+/[a-zA-Z0-9!#$&^_.+-]+$")
SHA256_HEX_REGEX = re.compile(r"^[0-9a-fA-F]{64}$")
RFC3339_REGEX = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})$"
)

CONTRACT_VERSION_REGEX = re.compile(r"^[a-zA-Z0-9_.-]{1,64}$")
MAX_DISPOSITION_REF_LENGTH = 128
DISPOSITION_REF_REGEX = re.compile(r"^[a-zA-Z0-9_.:/-]{1,128}$")

BASE_ENTRY_FIELDS = {
    "attachment_id",
    "message_id",
    "account",
    "folder",
    "original_folder",
    "part_locator",
    "clean_filename",
    "filename",
    "mime_type",
    "effective_mime_type",
    "sha256",
    "size_bytes",
    "run_id",
    "quarantine_path",
    "analysis_status",
    "analyzed_at",
    "contract_version",
    "contract_hash",
    "lifecycle_state",
    "disposition_ref",
}

CORE_COVERAGE_FIELDS = {
    "analysis_completeness",
    "truncation_reason",
    "truncation_stage",
    "handoff_character_count",
    "analysis_character_budget",
}

COVERAGE_FIELDS = CORE_COVERAGE_FIELDS | {"source_character_count"}

ALLOWED_ENTRY_FIELDS = BASE_ENTRY_FIELDS | COVERAGE_FIELDS

ALLOWED_ROOT_FIELDS = {"schema_version", "updated_at", "items"}

CANONICAL_ENTRY_FIELDS = (
    "attachment_id",
    "message_id",
    "account",
    "folder",
    "part_locator",
    "clean_filename",
    "mime_type",
    "sha256",
    "size_bytes",
    "run_id",
    "quarantine_path",
    "analysis_status",
    "analyzed_at",
    "contract_version",
    "contract_hash",
    "lifecycle_state",
    "disposition_ref",
    "analysis_completeness",
    "truncation_reason",
    "truncation_stage",
    "handoff_character_count",
    "analysis_character_budget",
    "source_character_count",
)

FORBIDDEN_ENTRY_FIELDS = {
    "text",
    "extracted_text",
    "content",
    "body",
    "prompt",
    "llm_prompt",
    "response",
    "model_response",
    "credentials",
    "password",
    "token",
    "tokens",
    "api_key",
    "envelope_id",
    "himalaya_id",
}


def _find_forbidden_content_keys(obj: Any) -> set[str]:
    """Recursively find any forbidden content keys in nested dictionaries or iterables."""
    found: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in FORBIDDEN_ENTRY_FIELDS:
                found.add(str(k))
            found.update(_find_forbidden_content_keys(v))
    elif isinstance(obj, (list, tuple, set)):
        for item in obj:
            found.update(_find_forbidden_content_keys(item))
    return found


# ==============================================================================
# Deterministic Identity & Path Resolution
# ==============================================================================

def compute_attachment_id(
    message_id: str,
    part_locator: str,
    inventory_sha256: str,
) -> str:
    """Compute deterministic 64-char SHA-256 ID binding normalized mid, locator, and inventory hash."""
    norm_mid = normalize_message_id(message_id)
    norm_loc = str(part_locator).strip()
    norm_sha = str(inventory_sha256).strip().lower()

    if not norm_mid:
        raise ValueError("message_id is required to compute attachment_id")
    if not norm_loc:
        raise ValueError("part_locator is required to compute attachment_id")
    if not norm_sha:
        raise ValueError("inventory_sha256 is required to compute attachment_id")

    canonical_dict = {
        "inventory_sha256": norm_sha,
        "message_id": norm_mid,
        "part_locator": norm_loc,
    }
    canonical_json = json.dumps(canonical_dict, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def resolve_quarantine_index_path(
    index_path: Path | str | None = None,
    data_dir: Path | str | None = None,
    workspace_root: Path | str | None = None,
) -> Path:
    """Resolve absolute path to attachment-quarantine-index.json."""
    if index_path is not None:
        return Path(index_path).resolve()
    if data_dir is not None:
        return (Path(data_dir) / INDEX_FILENAME).resolve()
    if workspace_root is not None:
        return (Path(workspace_root) / "data" / "mail-desk" / INDEX_FILENAME).resolve()
    return (resolve_data_dir() / INDEX_FILENAME).resolve()


# ==============================================================================
# Data Access Layer: Load & Atomic Save
# ==============================================================================

def load_quarantine_index(index_path: Path) -> dict[str, Any]:
    """Load attachment quarantine index and strictly validate Schema 1 root and all items fail-closed."""
    if not index_path.exists():
        return {
            "schema_version": SCHEMA_VERSION,
            "updated_at": None,
            "items": {},
        }
    try:
        raw_text = index_path.read_text(encoding="utf-8")
        data = json.loads(raw_text)
    except Exception as err:
        raise QuarantineIndexError(f"Failed to read or parse quarantine index '{index_path}': {err}") from err

    if not isinstance(data, dict):
        raise QuarantineIndexError(f"Corrupted quarantine index '{index_path}': root must be a JSON object")

    # 1. Reject unknown root fields
    unknown_root = set(data.keys()) - ALLOWED_ROOT_FIELDS
    if unknown_root:
        raise AttachmentIndexSchemaError(
            f"Unknown root field(s) in quarantine index '{index_path}': {sorted(unknown_root)}"
        )

    # 2. Validate schema_version
    schema_ver = data.get("schema_version")
    if schema_ver != SCHEMA_VERSION:
        raise QuarantineIndexError(
            f"Unsupported quarantine index schema_version: {schema_ver!r} (expected {SCHEMA_VERSION})"
        )

    # 3. Validate updated_at
    updated_at = data.get("updated_at")
    if updated_at is not None:
        if not isinstance(updated_at, str) or not RFC3339_REGEX.fullmatch(updated_at):
            raise AttachmentIndexSchemaError(
                f"Invalid root 'updated_at' timestamp: {updated_at!r}"
            )

    # 4. Strictly validate items container (never fall back silently to {})
    items = data.get("items")
    if not isinstance(items, dict):
        raise AttachmentIndexSchemaError(
            f"Quarantine index 'items' must be a JSON object (dict), got {type(items).__name__}"
        )

    # 5. Strictly validate all entries fail-closed
    validated_items: dict[str, Any] = {}
    for key, entry in items.items():
        if not isinstance(key, str) or not key.strip():
            raise AttachmentIndexSchemaError(f"Invalid item key in quarantine index: {key!r}")
        validated_entry = validate_quarantine_index_entry(entry)
        if key != validated_entry["attachment_id"]:
            raise AttachmentIndexDriftError(
                f"Quarantine index key drift: dictionary key '{key}' does not match entry attachment_id '{validated_entry['attachment_id']}'"
            )
        validated_items[key] = validated_entry

    return {
        "schema_version": schema_ver,
        "updated_at": updated_at,
        "items": validated_items,
    }


def save_quarantine_index_atomic(
    index_path: Path,
    data: dict[str, Any],
) -> None:
    """Save quarantine index atomically via temporary file replacement."""
    if not isinstance(data, dict):
        raise QuarantineIndexError("Cannot save invalid quarantine index data: root must be a dict")

    unknown_root = set(data.keys()) - ALLOWED_ROOT_FIELDS
    if unknown_root:
        raise AttachmentIndexSchemaError(
            f"Unknown root field(s) when saving quarantine index: {sorted(unknown_root)}"
        )

    schema_ver = data.get("schema_version", SCHEMA_VERSION)
    if schema_ver != SCHEMA_VERSION or not isinstance(data.get("items"), dict):
        raise QuarantineIndexError(f"Cannot save invalid or unsupported schema_version: {schema_ver}")

    serializable_items: dict[str, Any] = {}
    for k, item in data.get("items", {}).items():
        if not isinstance(item, dict):
            raise AttachmentIndexSchemaError(f"Index item {k!r} must be a dictionary")
        validated_item = validate_quarantine_index_entry(item)
        if k != validated_item["attachment_id"]:
            raise AttachmentIndexDriftError(
                f"Quarantine index key drift: dictionary key '{k}' does not match entry attachment_id '{validated_item['attachment_id']}'"
            )
        serializable_items[k] = validated_item

    data_to_save = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": data.get("updated_at"),
        "items": serializable_items,
    }

    index_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="\n",
        dir=index_path.parent,
        prefix=index_path.name + ".",
        suffix=".tmp",
        delete=False,
    ) as f:
        json.dump(data_to_save, f, ensure_ascii=False, indent=2)
        f.write("\n")
        temp_path = Path(f.name)

    temp_path.replace(index_path)


# ==============================================================================
# Entry Validation & Canonical Binding
# ==============================================================================

def validate_quarantine_index_entry(
    entry: dict[str, Any],
) -> dict[str, Any]:
    """Strictly validate and normalize a candidate quarantine index entry fail-closed against Schema 1.

    Rejects:
    - Non-dictionary entries
    - Forbidden content keys (text, body, prompt, credentials, envelope_id, etc.)
    - Unknown/unsupported keys outside ALLOWED_ENTRY_FIELDS
    - Absolute quarantine paths or directory traversal outside boundaries
    - Invalid analysis_status (must be 'completed')
    - Invalid lifecycle_state (must be 'quarantined')
    - Missing required fields
    - Invalid or contradictory coverage fields
    - attachment_id drift if provided
    """
    if not isinstance(entry, dict):
        raise AttachmentIndexSchemaError("Index entry must be a dictionary")

    # 1. Reject forbidden content fields (top-level and nested anywhere)
    forbidden_present = _find_forbidden_content_keys(entry)
    if forbidden_present:
        raise ForbiddenContentError(
            f"Forbidden content keys detected in index entry: {sorted(forbidden_present)}"
        )

    # 2. Reject unknown fields
    unknown_fields = set(entry.keys()) - ALLOWED_ENTRY_FIELDS
    if unknown_fields:
        raise AttachmentIndexSchemaError(
            f"Unknown fields in quarantine index entry: {sorted(unknown_fields)}"
        )

    # 3. Message ID
    raw_mid = entry.get("message_id")
    if not raw_mid or not str(raw_mid).strip():
        raise AttachmentIndexSchemaError("Missing required 'message_id'")
    norm_mid = normalize_message_id(str(raw_mid))

    # 4. Account
    account = str(entry.get("account") or "").strip()
    if not account:
        raise AttachmentIndexSchemaError("Missing required 'account'")

    # 5. Folder (fail-closed against contradictory aliases)
    raw_f = entry.get("folder")
    raw_orig_f = entry.get("original_folder")

    if raw_f is not None and raw_orig_f is not None:
        f1 = str(raw_f).strip()
        f2 = str(raw_orig_f).strip()
        if not f1 or not f2:
            raise AttachmentIndexSchemaError("Missing or empty 'folder'")
        if f1 != f2:
            raise AttachmentIndexDriftError(
                f"Contradictory folder alias fields: 'folder' ({f1!r}) != 'original_folder' ({f2!r})"
            )
        folder = f1
    elif raw_f is not None:
        folder = str(raw_f).strip()
        if not folder:
            raise AttachmentIndexSchemaError("Missing or empty 'folder'")
    elif raw_orig_f is not None:
        folder = str(raw_orig_f).strip()
        if not folder:
            raise AttachmentIndexSchemaError("Missing or empty 'original_folder'")
    else:
        raise AttachmentIndexSchemaError("Missing required 'folder'")

    # 6. Part locator
    raw_loc = str(entry.get("part_locator") or "").strip()
    if not raw_loc or not PART_LOCATOR_REGEX.fullmatch(raw_loc):
        raise AttachmentIndexSchemaError(f"Missing or invalid 'part_locator': {raw_loc!r}")

    # 7. Clean filename (fail-closed against contradictory aliases)
    raw_clean_fn = entry.get("clean_filename")
    raw_fn = entry.get("filename")

    if raw_clean_fn is not None and raw_fn is not None:
        try:
            c1 = validate_attachment_filename(raw_clean_fn)
            c2 = validate_attachment_filename(raw_fn)
        except ValueError as err:
            raise AttachmentIndexSchemaError(f"Invalid filename: {err}") from err
        if c1 != c2:
            raise AttachmentIndexDriftError(
                f"Contradictory filename alias fields: 'clean_filename' ({c1!r}) != 'filename' ({c2!r})"
            )
        clean_fn = c1
    elif raw_clean_fn is not None:
        try:
            clean_fn = validate_attachment_filename(raw_clean_fn)
        except ValueError as err:
            raise AttachmentIndexSchemaError(f"Invalid 'clean_filename': {err}") from err
    elif raw_fn is not None:
        try:
            clean_fn = validate_attachment_filename(raw_fn)
        except ValueError as err:
            raise AttachmentIndexSchemaError(f"Invalid 'filename': {err}") from err
    else:
        raise AttachmentIndexSchemaError("Missing required 'clean_filename'")

    # 8. Normalized MIME type (fail-closed against contradictory aliases)
    raw_mime = entry.get("mime_type")
    raw_eff_mime = entry.get("effective_mime_type")

    if raw_mime is not None and raw_eff_mime is not None:
        m1 = str(raw_mime).strip().lower()
        m2 = str(raw_eff_mime).strip().lower()
        if not m1 or not MIME_TYPE_REGEX.fullmatch(m1):
            raise AttachmentIndexSchemaError(f"Missing or invalid 'mime_type': {raw_mime!r}")
        if not m2 or not MIME_TYPE_REGEX.fullmatch(m2):
            raise AttachmentIndexSchemaError(f"Missing or invalid 'effective_mime_type': {raw_eff_mime!r}")
        if m1 != m2:
            raise AttachmentIndexDriftError(
                f"Contradictory MIME type alias fields: 'mime_type' ({m1!r}) != 'effective_mime_type' ({m2!r})"
            )
        norm_mime = m1
    elif raw_mime is not None:
        m1 = str(raw_mime).strip().lower()
        if not m1 or not MIME_TYPE_REGEX.fullmatch(m1):
            raise AttachmentIndexSchemaError(f"Missing or invalid 'mime_type': {raw_mime!r}")
        norm_mime = m1
    elif raw_eff_mime is not None:
        m2 = str(raw_eff_mime).strip().lower()
        if not m2 or not MIME_TYPE_REGEX.fullmatch(m2):
            raise AttachmentIndexSchemaError(f"Missing or invalid 'effective_mime_type': {raw_eff_mime!r}")
        norm_mime = m2
    else:
        raise AttachmentIndexSchemaError("Missing required 'mime_type'")

    # 9. SHA-256
    raw_sha = str(entry.get("sha256") or "").strip()
    if not raw_sha or not SHA256_HEX_REGEX.fullmatch(raw_sha):
        raise AttachmentIndexSchemaError(f"Missing or invalid 'sha256': {raw_sha!r}")
    norm_sha = raw_sha.lower()

    # 10. Size in bytes
    size_bytes = entry.get("size_bytes")
    if size_bytes is None or isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes <= 0:
        raise AttachmentIndexSchemaError(f"'size_bytes' must be a positive integer, got: {size_bytes!r}")

    # 11. Run ID
    run_id = str(entry.get("run_id") or "").strip()
    if not run_id or not is_valid_run_id(run_id):
        raise AttachmentIndexSchemaError(f"Missing or invalid 'run_id': {run_id!r}")

    # 12. Quarantine path: strictly workspace-relative
    q_path = str(entry.get("quarantine_path") or "").strip()
    if not q_path:
        raise AttachmentIndexSchemaError("Missing required 'quarantine_path'")

    # Fail closed on absolute paths
    if os.path.isabs(q_path) or PurePath(q_path).is_absolute() or q_path.startswith("/") or q_path.startswith("\\") or (len(q_path) > 1 and q_path[1] == ":"):
        raise QuarantineIndexError(f"quarantine_path must be workspace-relative, never absolute: {q_path!r}")

    # Normalize posix relative path and check containment
    posix_path = PurePosixPath(q_path)
    if any(part == ".." for part in posix_path.parts):
        raise QuarantineIndexError(f"quarantine_path contains directory traversal: {q_path!r}")

    expected_prefix = PurePosixPath(f"data/mail-desk/attachments/{run_id}")
    try:
        posix_path.relative_to(expected_prefix)
    except ValueError:
        raise QuarantineIndexError(
            f"quarantine_path '{q_path}' must reside under expected run prefix '{expected_prefix}'"
        )

    # 13. Analysis status
    analysis_st = str(entry.get("analysis_status") or "").strip()
    if analysis_st not in ALLOWED_ANALYSIS_STATUSES:
        raise AttachmentIndexSchemaError(
            f"analysis_status must be 'completed', got: {analysis_st!r}"
        )

    # 14. Analyzed at (RFC-3339)
    analyzed_at = str(entry.get("analyzed_at") or "").strip()
    if not analyzed_at or not RFC3339_REGEX.fullmatch(analyzed_at):
        raise AttachmentIndexSchemaError(
            f"analyzed_at must be a valid RFC-3339 timestamp with timezone offset: {analyzed_at!r}"
        )

    # 15. Contract version (mandatory bounded identifier)
    raw_cv = entry.get("contract_version")
    if raw_cv is None or not str(raw_cv).strip():
        raise AttachmentIndexSchemaError("Missing required 'contract_version'")
    cv_str = str(raw_cv).strip()
    if len(cv_str) > 64 or not CONTRACT_VERSION_REGEX.fullmatch(cv_str):
        raise AttachmentIndexSchemaError(
            f"Invalid 'contract_version': must match {CONTRACT_VERSION_REGEX.pattern} (max 64 chars), got {raw_cv!r}"
        )
    contract_ver = cv_str

    # 15b. Optional contract hash (strictly 64-hex SHA-256 if provided, never an alias for version)
    raw_ch = entry.get("contract_hash")
    contract_hash = None
    if raw_ch is not None:
        if not isinstance(raw_ch, str):
            raise AttachmentIndexSchemaError(
                f"'contract_hash' must be a string, got {type(raw_ch).__name__}"
            )
        ch_str = raw_ch.strip().lower()
        if not SHA256_HEX_REGEX.fullmatch(ch_str):
            raise AttachmentIndexSchemaError(
                f"Invalid 'contract_hash': must be a 64-character hexadecimal SHA-256 hash, got {raw_ch!r}"
            )
        contract_hash = ch_str

    # 16. Lifecycle state
    lifecycle_st = str(entry.get("lifecycle_state") or "").strip()
    if lifecycle_st not in ALLOWED_LIFECYCLE_STATES:
        raise AttachmentIndexSchemaError(
            f"lifecycle_state must be 'quarantined', got: {lifecycle_st!r}"
        )

    # 17. Optional disposition ref (strictly null or bounded reference ID string)
    disposition_ref = entry.get("disposition_ref")
    if disposition_ref is not None:
        if isinstance(disposition_ref, dict):
            # Check for forbidden content inside dict before schema error
            forbidden_in_disp = _find_forbidden_content_keys(disposition_ref)
            if forbidden_in_disp:
                raise ForbiddenContentError(
                    f"Forbidden content keys detected in disposition_ref dictionary: {sorted(forbidden_in_disp)}"
                )
            raise AttachmentIndexSchemaError(
                f"'disposition_ref' must be null or string, arbitrary dictionary is forbidden: {type(disposition_ref).__name__}"
            )
        if not isinstance(disposition_ref, str):
            raise AttachmentIndexSchemaError(
                f"'disposition_ref' must be null or string, got {type(disposition_ref).__name__}"
            )
        disp_str = disposition_ref.strip()
        if not disp_str or len(disp_str) > MAX_DISPOSITION_REF_LENGTH or not DISPOSITION_REF_REGEX.fullmatch(disp_str):
            raise AttachmentIndexSchemaError(
                f"Invalid 'disposition_ref': must match {DISPOSITION_REF_REGEX.pattern} (max {MAX_DISPOSITION_REF_LENGTH} chars), got {disposition_ref!r}"
            )
        disposition_ref = disp_str

    # 18. Deterministic attachment_id derivation & drift verification
    computed_id = compute_attachment_id(norm_mid, raw_loc, norm_sha)
    provided_id = entry.get("attachment_id")
    if provided_id is not None:
        norm_provided = str(provided_id).strip().lower()
        if norm_provided != computed_id:
            raise AttachmentIndexDriftError(
                f"attachment_id drift: provided '{provided_id}' does not match computed deterministic ID '{computed_id}'"
            )

    ret = {
        "attachment_id": computed_id,
        "message_id": norm_mid,
        "account": account,
        "folder": folder,
        "original_folder": folder,
        "part_locator": raw_loc,
        "clean_filename": clean_fn,
        "filename": clean_fn,
        "mime_type": norm_mime,
        "effective_mime_type": norm_mime,
        "sha256": norm_sha,
        "size_bytes": size_bytes,
        "run_id": run_id,
        "quarantine_path": posix_path.as_posix(),
        "analysis_status": ANALYSIS_STATUS_COMPLETED,
        "analyzed_at": analyzed_at,
        "contract_version": contract_ver,
        "lifecycle_state": LIFECYCLE_STATE_QUARANTINED,
        "disposition_ref": disposition_ref,
    }
    if contract_hash is not None:
        ret["contract_hash"] = contract_hash

    # 19. Groupwise optional additive coverage fields (Schema 1)
    coverage_present = {k for k in COVERAGE_FIELDS if k in entry}
    if coverage_present:
        missing_core = CORE_COVERAGE_FIELDS - set(entry.keys())
        if missing_core:
            raise AttachmentIndexSchemaError(
                f"Incomplete coverage block: missing core coverage field(s) {sorted(missing_core)}"
            )

        raw_comp = entry.get("analysis_completeness")
        if not raw_comp or not isinstance(raw_comp, str):
            raise AttachmentIndexSchemaError("Missing or invalid 'analysis_completeness'")
        comp_str = raw_comp.strip().lower()
        if comp_str not in ALLOWED_ANALYSIS_COMPLETENESS:
            raise AttachmentIndexSchemaError(f"Invalid 'analysis_completeness': {raw_comp!r}")

        raw_trunc_r = entry.get("truncation_reason")
        trunc_reason = None
        if raw_trunc_r is not None and str(raw_trunc_r).strip() != "":
            trunc_reason = str(raw_trunc_r).strip()
            if trunc_reason not in ALLOWED_TRUNCATION_REASONS:
                raise AttachmentIndexSchemaError(f"Invalid 'truncation_reason': {raw_trunc_r!r}")

        raw_t_stage = entry.get("truncation_stage")
        if raw_t_stage is None or not isinstance(raw_t_stage, str):
            raise AttachmentIndexSchemaError(f"Missing or invalid 'truncation_stage': {raw_t_stage!r}")
        t_stage = raw_t_stage.strip()
        if t_stage not in ALLOWED_TRUNCATION_STAGES:
            raise AttachmentIndexSchemaError(f"Invalid 'truncation_stage': {raw_t_stage!r}")

        raw_h_chars = entry.get("handoff_character_count")
        if raw_h_chars is None or isinstance(raw_h_chars, bool):
            raise AttachmentIndexSchemaError(f"Invalid 'handoff_character_count': {raw_h_chars!r}")
        try:
            h_chars = int(raw_h_chars)
            if h_chars < 0:
                raise ValueError()
        except (ValueError, TypeError):
            raise AttachmentIndexSchemaError(f"Invalid 'handoff_character_count': {raw_h_chars!r}")

        raw_budget = entry.get("analysis_character_budget")
        if raw_budget is None or isinstance(raw_budget, bool):
            raise AttachmentIndexSchemaError(f"Invalid 'analysis_character_budget': {raw_budget!r}")
        try:
            budget = int(raw_budget)
            if budget < 0:
                raise ValueError()
        except (ValueError, TypeError):
            raise AttachmentIndexSchemaError(f"Invalid 'analysis_character_budget': {raw_budget!r}")

        raw_s_chars = entry.get("source_character_count")
        s_chars = None
        if raw_s_chars is not None:
            if isinstance(raw_s_chars, bool):
                raise AttachmentIndexSchemaError(f"Invalid 'source_character_count': {raw_s_chars!r}")
            try:
                s_chars = int(raw_s_chars)
                if s_chars < 0:
                    raise ValueError()
            except (ValueError, TypeError):
                raise AttachmentIndexSchemaError(f"Invalid 'source_character_count': {raw_s_chars!r}")

        if comp_str == ANALYSIS_COMPLETENESS_FULL:
            if trunc_reason is not None:
                raise AttachmentIndexSchemaError("analysis_completeness 'full' prohibited with truncation_reason")
            if t_stage != TRUNCATION_STAGE_NONE:
                raise AttachmentIndexSchemaError(f"analysis_completeness 'full' prohibited with truncation_stage '{t_stage}'")
        elif comp_str == ANALYSIS_COMPLETENESS_TRUNCATED:
            if not trunc_reason:
                raise AttachmentIndexSchemaError("analysis_completeness 'truncated' requires truncation_reason")
            if t_stage == TRUNCATION_STAGE_NONE:
                raise AttachmentIndexSchemaError("analysis_completeness 'truncated' requires truncation_stage other than 'none'")

        ret["analysis_completeness"] = comp_str
        ret["truncation_reason"] = trunc_reason
        ret["truncation_stage"] = t_stage
        ret["handoff_character_count"] = h_chars
        ret["analysis_character_budget"] = budget
        ret["source_character_count"] = s_chars

    return ret


# ==============================================================================
# Writer Operation: Lock, Verification, Idempotency & Replace
# ==============================================================================

def verify_quarantine_workspace_lock(
    workspace_root: str | Path | None = None,
    *,
    lease_id: str | None = None,
    conversation_id: str | None = None,
    data_dir: Path | None = None,
) -> Any:
    """Strictly verify invocation-owned workspace lock before quarantine index mutation.

    Legacy lock bypass is strictly forbidden for MD-Q2; allow_legacy is hardcoded to False,
    and WORKSPACE_LOCK_ALLOW_LEGACY in os.environ is ignored.
    """
    ws: Path
    if workspace_root is not None:
        ws = Path(workspace_root).resolve()
    else:
        env_ws = os.environ.get("WORKSPACE_ROOT", "").strip()
        if env_ws:
            ws = Path(env_ws).resolve()
        elif data_dir is not None:
            cand = Path(data_dir).resolve()
            found_ws = None
            for p in [cand, *cand.parents]:
                if (p / ".agents").is_dir() or (p / ".git").is_dir():
                    found_ws = p
                    break
            ws = found_ws if found_ws is not None else Path.cwd().resolve()
        else:
            cand = Path.cwd().resolve()
            found_ws = None
            for p in [cand, *cand.parents]:
                if (p / ".agents").is_dir() or (p / ".git").is_dir():
                    found_ws = p
                    break
            ws = found_ws if found_ws is not None else Path.cwd().resolve()

    eff_lease_id = lease_id if lease_id is not None else os.environ.get("WORKSPACE_LOCK_LEASE_ID") or None
    eff_conv_id = conversation_id if conversation_id is not None else os.environ.get("WORKSPACE_LOCK_CONVERSATION_ID") or None

    guard = _load_workspace_lock_guard()
    return guard.require_workspace_lock(
        ws,
        lease_id=eff_lease_id,
        conversation_id=eff_conv_id,
        allow_legacy=False,
    )


def record_quarantine_entry(
    index_path: Path | str | None = None,
    *,
    payload: dict[str, Any],
    workspace_root: Path | str | None = None,
    data_dir: Path | str | None = None,
    lease_id: str | None = None,
    conversation_id: str | None = None,
    verify_physical: bool = True,
) -> dict[str, Any]:
    """Record an analyzed attachment in the quarantine index under verified workspace lock.

    Guarantees:
    - Enforces verified workspace lock ownership before mutation (zero legacy bypass).
    - Strictly validates entry fields against Schema 1, rejecting forbidden/unknown keys.
    - Verifies physical file existence, size, SHA-256, and .quarantine-inventory.json integrity.
    - Inspects symlink and Windows reparse point safety fail-closed.
    - Pure idempotent repetition for identical entries (no-op).
    - Fail-closed drift abort across all canonical fields.
    - Atomically replaces attachment-quarantine-index.json.
    """
    ws = Path(workspace_root or Path.cwd()).resolve()
    idx_path = resolve_quarantine_index_path(index_path, data_dir=data_dir, workspace_root=ws)

    # 1. Lock Verification (strictly require invocation-owned lock, zero legacy bypass)
    try:
        verify_quarantine_workspace_lock(
            workspace_root=ws,
            lease_id=lease_id,
            conversation_id=conversation_id,
            data_dir=idx_path.parent,
        )
    except Exception as err:
        raise WorkspaceLockRequiredError(
            f"Quarantine index mutation requires an active, owned workspace lock: {err}"
        ) from err

    index_data = load_quarantine_index(idx_path)

    # 2. Entry validation & normalization
    canonical_entry = validate_quarantine_index_entry(payload)
    att_id = canonical_entry["attachment_id"]
    run_id = canonical_entry["run_id"]
    rel_path = canonical_entry["quarantine_path"]
    expected_sha = canonical_entry["sha256"]
    expected_size = canonical_entry["size_bytes"]
    clean_fn = canonical_entry["clean_filename"]
    mid = canonical_entry["message_id"]

    # 3. Renewed Physical Verification against disk and .quarantine-inventory.json
    if verify_physical:
        target_file = ws / PurePosixPath(rel_path)
        if not target_file.is_file():
            raise FileNotFoundError(f"Physical quarantine file missing at '{target_file}'")

        attachments_root = ws / "data" / "mail-desk" / "attachments"
        try:
            check_quarantine_path_security(target_file, attachments_root)
        except SymlinkEscapeError as err:
            raise PhysicalVerificationError(f"Symlink or reparse point security violation: {err}") from err

        if target_file.is_symlink() or os.path.islink(target_file):
            raise PhysicalVerificationError(f"Symlink detected at quarantine file: {target_file}")

        if os.name == "nt" and target_file.exists():
            try:
                stat_res = os.lstat(target_file)
                if getattr(stat_res, "st_file_attributes", 0) & 0x400:
                    raise PhysicalVerificationError(f"Reparse point detected at quarantine file: {target_file}")
            except OSError as err:
                raise PhysicalVerificationError(f"Failed to inspect quarantine file attributes: {err}") from err

        # Verify disk bytes & SHA-256
        actual_bytes = target_file.read_bytes()
        actual_size = len(actual_bytes)
        actual_sha = hashlib.sha256(actual_bytes).hexdigest().lower()

        if actual_size != expected_size:
            raise AttachmentIndexDriftError(
                f"Quarantine file size drift: disk file has {actual_size} bytes, entry declares {expected_size}"
            )
        if actual_sha != expected_sha:
            raise AttachmentIndexDriftError(
                f"Quarantine file hash drift: disk file has '{actual_sha}', entry declares '{expected_sha}'"
            )

        # Verify .quarantine-inventory.json via canonical validator
        verify_quarantine_attachment_artifact(
            run_id=run_id,
            relative_path=rel_path,
            expected_sha256=expected_sha,
            expected_size_bytes=expected_size,
            message_id=mid,
            workspace_root=ws,
            clean_filename=clean_fn,
        )

    # 4. Idempotency & Drift Detection
    items = index_data.setdefault("items", {})
    existing = items.get(att_id)
    if existing is not None:
        for field in CANONICAL_ENTRY_FIELDS:
            ex_val = existing.get(field)
            new_val = canonical_entry.get(field)
            if ex_val != new_val:
                raise AttachmentIndexDriftError(
                    f"Quarantine index drift on existing attachment_id '{att_id}': field '{field}' differs ({ex_val!r} != {new_val!r})"
                )

        # Idempotent repetition: identical entry exists -> no-op
        return {
            "status": "unchanged",
            "attachment_id": att_id,
            "item": existing,
        }

    # 5. Insert new entry & save atomically
    items[att_id] = canonical_entry
    index_data["updated_at"] = utc_now_iso()
    save_quarantine_index_atomic(idx_path, index_data)

    return {
        "status": "created",
        "attachment_id": att_id,
        "item": canonical_entry,
    }


def canonical_index_entry_sha256(
    entry: dict[str, Any],
) -> str:
    """Compute deterministic SHA-256 hash over canonical MD-Q2 or MD-C1 index entry."""
    canon = validate_quarantine_index_entry(entry)
    serialized = json.dumps(canon, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def remove_quarantine_entry(
    index_path: Path | str | None = None,
    *,
    attachment_id: str,
    workspace_root: Path | str | None = None,
    data_dir: Path | str | None = None,
    lease_id: str | None = None,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    """Atomically remove an entry from attachment-quarantine-index.json under verified workspace lock.

    Guarantees:
    - Enforces verified workspace lock ownership before mutation (zero legacy bypass).
    - Idempotent: if attachment_id is already absent, returns status='unchanged'.
    - Atomically replaces attachment-quarantine-index.json.
    """
    ws = Path(workspace_root or Path.cwd()).resolve()
    idx_path = resolve_quarantine_index_path(index_path, data_dir=data_dir, workspace_root=ws)

    # 1. Lock Verification
    try:
        verify_quarantine_workspace_lock(
            workspace_root=ws,
            lease_id=lease_id,
            conversation_id=conversation_id,
            data_dir=idx_path.parent,
        )
    except Exception as err:
        raise WorkspaceLockRequiredError(
            f"Quarantine index mutation requires an active, owned workspace lock: {err}"
        ) from err

    norm_id = str(attachment_id).strip().lower()
    if not SHA256_HEX_REGEX.fullmatch(norm_id):
        raise AttachmentIndexSchemaError(f"Invalid attachment_id: {attachment_id!r}")

    index_data = load_quarantine_index(idx_path)
    items = index_data.setdefault("items", {})

    if norm_id not in items:
        return {
            "status": "unchanged",
            "attachment_id": norm_id,
            "item": None,
        }

    removed_item = items.pop(norm_id)
    index_data["updated_at"] = utc_now_iso()
    save_quarantine_index_atomic(idx_path, index_data)

    return {
        "status": "removed",
        "attachment_id": norm_id,
        "item": removed_item,
    }


def update_quarantine_entry_disposition_ref(
    index_path: Path | str | None = None,
    *,
    attachment_id: str,
    disposition_ref: str | None,
    workspace_root: Path | str | None = None,
    data_dir: Path | str | None = None,
    lease_id: str | None = None,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    """Atomically update disposition_ref for an entry in attachment-quarantine-index.json under verified workspace lock."""
    ws = Path(workspace_root or Path.cwd()).resolve()
    idx_path = resolve_quarantine_index_path(index_path, data_dir=data_dir, workspace_root=ws)

    # 1. Lock Verification
    try:
        verify_quarantine_workspace_lock(
            workspace_root=ws,
            lease_id=lease_id,
            conversation_id=conversation_id,
            data_dir=idx_path.parent,
        )
    except Exception as err:
        raise WorkspaceLockRequiredError(
            f"Quarantine index mutation requires an active, owned workspace lock: {err}"
        ) from err

    norm_id = str(attachment_id).strip().lower()
    if not SHA256_HEX_REGEX.fullmatch(norm_id):
        raise AttachmentIndexSchemaError(f"Invalid attachment_id: {attachment_id!r}")

    index_data = load_quarantine_index(idx_path)
    items = index_data.setdefault("items", {})

    if norm_id not in items:
        raise KeyError(f"Attachment '{norm_id}' not found in quarantine index")

    entry = items[norm_id]
    if entry.get("disposition_ref") == disposition_ref:
        return {
            "status": "unchanged",
            "attachment_id": norm_id,
            "item": entry,
        }

    mutated = dict(entry)
    mutated["disposition_ref"] = disposition_ref
    validated = validate_quarantine_index_entry(mutated)
    items[norm_id] = validated
    index_data["updated_at"] = utc_now_iso()
    save_quarantine_index_atomic(idx_path, index_data)

    return {
        "status": "updated",
        "attachment_id": norm_id,
        "item": validated,
    }


# ==============================================================================
# Read-Only Reconciliation
# ==============================================================================

def reconcile_quarantine_index(
    index_path: Path | str | None = None,
    *,
    workspace_root: Path | str | None = None,
    data_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Perform read-only reconciliation of the quarantine index against physical disk and inventory.

    Guarantees:
    - Strictly read-only: mutates neither the index nor files on disk.
    - Classifies each entry as 'consistent', 'missing_review', or 'drift'.
    - Never silently removes or overwrites records.
    """
    ws = Path(workspace_root or Path.cwd()).resolve()
    idx_path = resolve_quarantine_index_path(index_path, data_dir=data_dir, workspace_root=ws)
    index_data = load_quarantine_index(idx_path)
    items = index_data.get("items", {})

    results: list[dict[str, Any]] = []
    consistent_count = 0
    missing_count = 0
    drift_count = 0

    attachments_root = (ws / "data" / "mail-desk" / "attachments").resolve()

    for att_id, entry in items.items():
        rel_path = entry.get("quarantine_path", "")
        run_id = entry.get("run_id", "")
        exp_sha = str(entry.get("sha256") or "").strip().lower()
        exp_size = entry.get("size_bytes")
        mid = entry.get("message_id", "")
        clean_fn = entry.get("clean_filename") or entry.get("filename") or ""

        coverage_data: dict[str, Any] = {}
        if "analysis_completeness" in entry:
            coverage_data = {
                "analysis_completeness": entry.get("analysis_completeness"),
                "truncation_reason": entry.get("truncation_reason"),
                "truncation_stage": entry.get("truncation_stage", TRUNCATION_STAGE_NONE),
                "handoff_character_count": entry.get("handoff_character_count", 0),
                "analysis_character_budget": entry.get("analysis_character_budget", 0),
                "source_character_count": entry.get("source_character_count"),
            }

        # Containment & Security Checks BEFORE any file access
        if not is_valid_run_id(run_id):
            drift_count += 1
            results.append({
                "attachment_id": att_id,
                "status": "drift",
                "message_id": mid,
                "filename": clean_fn,
                "run_id": run_id,
                "quarantine_path": rel_path,
                "details": f"Invalid run_id: {run_id!r}",
                **coverage_data,
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
            drift_count += 1
            results.append({
                "attachment_id": att_id,
                "status": "drift",
                "message_id": mid,
                "filename": clean_fn,
                "run_id": run_id,
                "quarantine_path": rel_path,
                "details": f"Absolute quarantine_path rejected: {rel_path!r}",
                **coverage_data,
            })
            continue

        posix_rel = PurePosixPath(rel_path)
        if any(part == ".." for part in posix_rel.parts):
            drift_count += 1
            results.append({
                "attachment_id": att_id,
                "status": "drift",
                "message_id": mid,
                "filename": clean_fn,
                "run_id": run_id,
                "quarantine_path": rel_path,
                "details": f"Directory traversal rejected: {rel_path!r}",
                **coverage_data,
            })
            continue

        expected_run_prefix = PurePosixPath(f"data/mail-desk/attachments/{run_id}")
        try:
            posix_rel.relative_to(expected_run_prefix)
        except ValueError:
            drift_count += 1
            results.append({
                "attachment_id": att_id,
                "status": "drift",
                "message_id": mid,
                "filename": clean_fn,
                "run_id": run_id,
                "quarantine_path": rel_path,
                "details": f"quarantine_path '{rel_path}' does not reside under expected run prefix '{expected_run_prefix}'",
                **coverage_data,
            })
            continue

        target_file = ws / posix_rel

        # Path security (symlink / boundary escape check)
        try:
            check_quarantine_path_security(target_file, attachments_root)
        except SymlinkEscapeError as err:
            drift_count += 1
            results.append({
                "attachment_id": att_id,
                "status": "drift",
                "message_id": mid,
                "filename": clean_fn,
                "run_id": run_id,
                "quarantine_path": rel_path,
                "details": f"Path security violation: {err}",
                **coverage_data,
            })
            continue

        if not target_file.exists():
            missing_count += 1
            results.append({
                "attachment_id": att_id,
                "status": "missing_review",
                "message_id": mid,
                "filename": clean_fn,
                "run_id": run_id,
                "quarantine_path": rel_path,
                "details": f"Physical file does not exist at '{target_file}'",
                **coverage_data,
            })
            continue

        if not target_file.is_file() or target_file.is_symlink() or os.path.islink(target_file):
            drift_count += 1
            results.append({
                "attachment_id": att_id,
                "status": "drift",
                "message_id": mid,
                "filename": clean_fn,
                "run_id": run_id,
                "quarantine_path": rel_path,
                "details": "Target is not a regular file or is a symlink",
                **coverage_data,
            })
            continue

        if os.name == "nt":
            try:
                stat_res = os.lstat(target_file)
                if getattr(stat_res, "st_file_attributes", 0) & 0x400:
                    drift_count += 1
                    results.append({
                        "attachment_id": att_id,
                        "status": "drift",
                        "message_id": mid,
                        "filename": clean_fn,
                        "run_id": run_id,
                        "quarantine_path": rel_path,
                        "details": "Windows reparse point detected",
                        **coverage_data,
                    })
                    continue
            except OSError:
                pass

        try:
            actual_bytes = target_file.read_bytes()
            actual_size = len(actual_bytes)
            actual_sha = hashlib.sha256(actual_bytes).hexdigest().lower()

            if actual_size != exp_size or actual_sha != exp_sha:
                drift_count += 1
                results.append({
                    "attachment_id": att_id,
                    "status": "drift",
                    "message_id": mid,
                    "filename": clean_fn,
                    "run_id": run_id,
                    "quarantine_path": rel_path,
                    "details": f"Hash or size drift: disk ({actual_sha}, {actual_size}) != index ({exp_sha}, {exp_size})",
                    **coverage_data,
                })
                continue

            # Check .quarantine-inventory.json
            verify_quarantine_attachment_artifact(
                run_id=run_id,
                relative_path=rel_path,
                expected_sha256=exp_sha,
                expected_size_bytes=exp_size,
                message_id=mid,
                workspace_root=ws,
                clean_filename=clean_fn,
            )

            consistent_count += 1
            results.append({
                "attachment_id": att_id,
                "status": "consistent",
                "message_id": mid,
                "filename": clean_fn,
                "run_id": run_id,
                "quarantine_path": rel_path,
                "details": "Physical file and inventory match quarantine index",
                **coverage_data,
            })
        except Exception as err:
            drift_count += 1
            results.append({
                "attachment_id": att_id,
                "status": "drift",
                "message_id": mid,
                "filename": clean_fn,
                "run_id": run_id,
                "quarantine_path": rel_path,
                "details": f"Physical or inventory verification error: {err}",
                **coverage_data,
            })

    overall_status = "consistent" if (missing_count == 0 and drift_count == 0) else ("drift" if drift_count > 0 else "missing_review")

    return {
        "status": overall_status,
        "total_entries": len(items),
        "consistent_count": consistent_count,
        "missing_review_count": missing_count,
        "drift_count": drift_count,
        "results": results,
    }


# ==============================================================================
# Read-Only Lookups & Statistics
# ==============================================================================

def lookup_quarantine_entry(
    index_path: Path,
    attachment_id: str | None = None,
    message_id: str | None = None,
) -> dict[str, Any] | None:
    """Lookup a quarantine index entry by deterministic attachment_id or normalized message_id.

    For existing index entries without coverage fields, reports analysis_completeness: "unknown"
    at the read/display level without mutating the on-disk file.
    """
    data = load_quarantine_index(index_path)
    items = data.get("items", {})

    target_entry = None
    if attachment_id is not None:
        norm_id = str(attachment_id).strip().lower()
        if norm_id in items:
            target_entry = items[norm_id]

    if target_entry is None and message_id is not None:
        norm_mid = normalize_message_id(message_id)
        for entry in items.values():
            if entry.get("message_id") == norm_mid:
                target_entry = entry
                break

    if target_entry is None:
        return None

    result = dict(target_entry)
    if "analysis_completeness" not in result:
        result["analysis_completeness"] = ANALYSIS_COMPLETENESS_UNKNOWN
    return result


def get_quarantine_index_stats(index_path: Path) -> dict[str, Any]:
    """Calculate summary statistics for attachment-quarantine-index.json."""
    data = load_quarantine_index(index_path)
    items = data.get("items", {})

    accounts: dict[str, int] = {}
    mime_types: dict[str, int] = {}
    lifecycle_states: dict[str, int] = {}
    total_bytes = 0

    for item in items.values():
        acc = item.get("account", "unknown")
        accounts[acc] = accounts.get(acc, 0) + 1

        mime = item.get("mime_type", "unknown")
        mime_types[mime] = mime_types.get(mime, 0) + 1

        state = item.get("lifecycle_state", "unknown")
        lifecycle_states[state] = lifecycle_states.get(state, 0) + 1

        total_bytes += int(item.get("size_bytes", 0))

    file_size_bytes = index_path.stat().st_size if index_path.exists() else 0

    return {
        "index_file": str(index_path),
        "file_size_bytes": file_size_bytes,
        "total_indexed_items": len(items),
        "total_quarantine_bytes": total_bytes,
        "schema_version": data.get("schema_version", SCHEMA_VERSION),
        "updated_at": data.get("updated_at"),
        "accounts": accounts,
        "mime_types": mime_types,
        "lifecycle_states": lifecycle_states,
    }
