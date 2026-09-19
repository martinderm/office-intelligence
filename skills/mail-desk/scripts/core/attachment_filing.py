"""Catalog- and filemap-backed attachment filing candidate proposal (MD-A5).

Purely read-only module: no uploads, no directory creation, no filemap modification,
no catalog mutation. Generates an attachment_filing_candidate with promotion_status:
"pending_human_review".
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
from typing import Any, Callable, Mapping, Sequence

from core.common import normalize_message_id
from .attachment_extract import is_valid_run_id
from .attachment_authorization import (
    CONTEXT_FILING,
    guard_context_authorization,
)
from .attachment_fetch import (
    ApprovalReceiptMissingError,
    QuarantineInventoryError,
    ReceiptDriftError,
    SymlinkEscapeError,
    compute_review_hash,
    verify_approval_receipt,
    verify_quarantine_attachment_artifact,
)
from .attachment_handoff import (
    AttachmentHandoffError,
    HandoffDriftError,
    validate_attachment_handoff,
)
from .attachment_policy import sanitize_attachment_filename
from .attachments import (
    AccountDriftError,
    AttachmentDriftError,
    HashDriftError,
    LocationDriftError,
    MessageIdDriftError,
    PartLocatorDriftError,
    PROVENANCE_RFC822,
    validate_attachment_candidate_metadata,
    verify_attachment_drift,
)


# ==============================================================================
# Exceptions
# ==============================================================================

class AttachmentFilingError(ValueError):
    """Base exception for attachment filing errors."""
    pass


class InvalidMDA2FetchError(AttachmentFilingError):
    """Raised when an attachment input does not conform to the verified MD-A2 contract."""
    pass


# ==============================================================================
# Constants & Enums
# ==============================================================================

STATUS_PROPOSED = "proposed"
STATUS_ALREADY_PRESENT = "already_present"
STATUS_COLLISION_DETECTED = "collision_detected"
STATUS_NOT_CONFIGURED = "not_configured"
STATUS_STORAGE_REVIEW_REQUIRED = "storage_review_required"
STATUS_DIRECTORY_REVIEW_REQUIRED = "directory_review_required"

PROMOTION_STATUS_PENDING_HUMAN_REVIEW = "pending_human_review"

DEFAULT_MAX_FILEMAP_AGE_SECONDS = 86_400  # 24 hours

TIMESTAMP_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%d",
)

PART_LOCATOR_REGEX = re.compile(r"^\d+(?:\.\d+)*$")
SHA256_HEX_REGEX = re.compile(r"^[0-9a-f]{64}$")
FILEMAP_SCHEMA_URI = "https://raw.githubusercontent.com/martinderm/office-intelligence/main/skills/cloud-atlas/references/filemap.schema.json"


# ==============================================================================
# Dynamic Cloud-Atlas Validator Loader
# ==============================================================================

def _load_cloud_atlas_validate_filemap() -> Callable[..., Any]:
    """Dynamically load canonical validate_filemap from cloud-atlas scripts/gen_filemap.py."""
    if "cloud_atlas_gen_filemap" in sys.modules:
        mod = sys.modules["cloud_atlas_gen_filemap"]
        if hasattr(mod, "validate_filemap"):
            return getattr(mod, "validate_filemap")

    search_roots = (
        list(Path(__file__).resolve().parents)
        + [Path.cwd().resolve()]
        + list(Path.cwd().resolve().parents)
    )
    for base in search_roots:
        for rel in [
            Path("skills") / "cloud-atlas" / "scripts" / "gen_filemap.py",
            Path("cloud-atlas") / "scripts" / "gen_filemap.py",
        ]:
            cand = base / rel
            if cand.is_file():
                spec = importlib.util.spec_from_file_location("cloud_atlas_gen_filemap", cand)
                if spec and spec.loader:
                    mod = importlib.util.module_from_spec(spec)
                    sys.modules["cloud_atlas_gen_filemap"] = mod
                    scripts_dir = str(cand.parent)
                    old_core = sys.modules.get("core")
                    old_path = list(sys.path)
                    try:
                        sys.path.insert(0, scripts_dir)
                        if "core" in sys.modules:
                            del sys.modules["core"]
                        spec.loader.exec_module(mod)
                    finally:
                        sys.path = old_path
                        if old_core is not None:
                            sys.modules["core"] = old_core
                        elif "core" in sys.modules:
                            del sys.modules["core"]
                    return getattr(mod, "validate_filemap")
    raise RuntimeError("Canonical Cloud-Atlas validate_filemap is unavailable")



# ==============================================================================
# MD-A2 Composite Contract Validation
# ==============================================================================

def validate_mda2_attachment(
    attachment: Mapping[str, Any] | None = None,
    *,
    manifest_account: str | None = None,
    bound_account: str | None = None,
    operation: Mapping[str, Any] | None = None,
    fetch_result: Mapping[str, Any] | None = None,
    candidate: Mapping[str, Any] | None = None,
    workspace_root: Path | str | None = None,
    verify_physical_evidence: bool = False,
) -> dict[str, Any]:
    """Validate that attachment input conforms strictly to the verified MD-A2 composite contract.

    Requires an explicit composite contract comprising:
    - explicitly bound manifest account ('manifest_account' or 'bound_account')
    - canonical review-bound MD-A2 manifest operation ('operation' or 'manifest_operation')
    - successful MD-A2 fetch result ('result' or 'fetch_result')
    - externally bound MD-A1 MIME candidate ('candidate')

    Guarantees:
    - Rejects free/flat unverified dictionaries or drifted fields.
    - Requires explicit bound manifest account; binds manifest_account, candidate, and review_hash.
    - operation.account is optional; if present, must match manifest_account exactly.
    - Enforces operation.action == 'attachment_fetch'.
    - Enforces relative_path == 'data/mail-desk/attachments/<run_id>/<sanitized_filename>'.
    - If verify_physical_evidence is True, performs read-only verification of the existing
      .quarantine-inventory.json and computes actual file SHA-256 on disk via MD-A2 canonical helper.
    """
    if attachment is not None and not isinstance(attachment, Mapping):
        raise InvalidMDA2FetchError("Attachment input must be a dictionary (Mapping)")

    op = operation
    res = fetch_result
    cand = candidate

    if attachment is not None:
        op = op or attachment.get("operation") or attachment.get("manifest_operation")
        res = res or attachment.get("result") or attachment.get("fetch_result")
        cand = cand or attachment.get("candidate")

    # Reject free dictionaries that do not supply the composite contract
    if not isinstance(op, Mapping) or not isinstance(res, Mapping) or not isinstance(cand, Mapping):
        raise InvalidMDA2FetchError(
            "Free attachment dictionary rejected: composite contract with canonical 'operation', "
            "'result'/'fetch_result', and 'candidate' is required"
        )

    # 0. Validate Bound Manifest Account (strictly via explicit keyword parameter)
    norm_man = str(manifest_account).strip() if manifest_account is not None and str(manifest_account).strip() else None
    norm_bound = str(bound_account).strip() if bound_account is not None and str(bound_account).strip() else None

    if norm_man is not None and norm_bound is not None:
        if norm_man != norm_bound:
            raise InvalidMDA2FetchError(
                f"Account drift between explicit keyword parameters manifest_account '{norm_man}' "
                f"and bound_account '{norm_bound}'"
            )
        eff_bound_account = norm_man
    elif norm_man is not None:
        eff_bound_account = norm_man
    elif norm_bound is not None:
        eff_bound_account = norm_bound
    else:
        raise InvalidMDA2FetchError(
            "Missing required bound manifest account keyword parameter ('manifest_account' or 'bound_account')"
        )

    # 1. Validate MD-A1 Candidate
    is_valid_cand, cand_err = validate_attachment_candidate_metadata(dict(cand))
    if not is_valid_cand:
        raise InvalidMDA2FetchError(f"MD-A1 candidate validation failed: {cand_err}")

    cand_account = str(cand.get("account") or "").strip()
    if not cand_account:
        raise InvalidMDA2FetchError("MD-A1 candidate must include non-empty 'account'")
    if cand_account != eff_bound_account:
        raise InvalidMDA2FetchError(
            f"Account drift between candidate '{cand_account}' and bound manifest account '{eff_bound_account}'"
        )

    raw_cand_mid = cand.get("message_id")
    if not raw_cand_mid or not isinstance(raw_cand_mid, str) or not raw_cand_mid.strip():
        raise InvalidMDA2FetchError("MD-A1 candidate must include non-empty 'message_id'")
    cand_mid = normalize_message_id(raw_cand_mid)
    if not cand_mid:
        raise InvalidMDA2FetchError(f"Invalid message_id in MD-A1 candidate: {raw_cand_mid!r}")

    cand_folder = str(cand.get("folder") or "").strip()
    if not cand_folder:
        raise InvalidMDA2FetchError("MD-A1 candidate must include non-empty 'folder'")

    cand_eid = str(cand.get("envelope_id") or "").strip()
    if not cand_eid:
        raise InvalidMDA2FetchError("MD-A1 candidate must include non-empty 'envelope_id'")

    cand_locator = str(cand.get("part_locator") or "").strip()
    if not cand_locator or not PART_LOCATOR_REGEX.fullmatch(cand_locator):
        raise InvalidMDA2FetchError(f"Invalid part_locator in MD-A1 candidate: {cand_locator!r}")

    cand_sha = str(cand.get("sha256") or "").strip().lower()
    if not SHA256_HEX_REGEX.fullmatch(cand_sha):
        raise InvalidMDA2FetchError(f"Invalid SHA-256 in MD-A1 candidate: {cand_sha!r}")

    cand_fn = str(cand.get("filename") or "").strip()
    if not cand_fn or chr(0) in cand_fn:
        raise InvalidMDA2FetchError(f"Invalid filename in MD-A1 candidate: {cand_fn!r}")

    cand_size = cand.get("size_bytes")
    if cand_size is None or isinstance(cand_size, bool) or not isinstance(cand_size, int) or cand_size <= 0:
        raise InvalidMDA2FetchError(f"Invalid size_bytes in MD-A1 candidate: {cand_size!r}")

    cand_mime = str(cand.get("mime_type") or "").strip().lower()
    if not cand_mime:
        raise InvalidMDA2FetchError("MD-A1 candidate must include 'mime_type'")

    # 2. Validate MD-A2 Manifest Operation
    op_action = str(op.get("action") or "").strip()
    if op_action != "attachment_fetch":
        raise InvalidMDA2FetchError(f"Operation action must be exactly 'attachment_fetch', got: {op_action!r}")

    op_account = op.get("account")
    if op_account is not None and str(op_account).strip():
        op_account_str = str(op_account).strip()
        if op_account_str != eff_bound_account:
            raise InvalidMDA2FetchError(
                f"Account drift between operation '{op_account_str}' and bound manifest account '{eff_bound_account}'"
            )

    op_raw_mid = op.get("message_id")
    if not op_raw_mid or not isinstance(op_raw_mid, str) or not op_raw_mid.strip():
        raise InvalidMDA2FetchError("MD-A2 operation must include non-empty 'message_id'")
    op_mid = normalize_message_id(op_raw_mid)
    if not op_mid or op_mid != cand_mid:
        raise InvalidMDA2FetchError(
            f"Message-ID drift between operation '{op_mid}' and candidate '{cand_mid}'"
        )

    op_folder = str(op.get("folder") or "").strip()
    if not op_folder or op_folder != cand_folder:
        raise InvalidMDA2FetchError(
            f"Folder drift between operation '{op_folder}' and candidate '{cand_folder}'"
        )

    op_eid = str(op.get("envelope_id") or "").strip()
    if not op_eid or op_eid != cand_eid:
        raise InvalidMDA2FetchError(
            f"Envelope-ID drift between operation '{op_eid}' and candidate '{cand_eid}'"
        )

    op_locator = str(op.get("part_locator") or "").strip()
    if not op_locator or op_locator != cand_locator:
        raise InvalidMDA2FetchError(
            f"Part locator drift between operation '{op_locator}' and candidate '{cand_locator}'"
        )

    op_sha = str(op.get("inventory_sha256") or op.get("sha256") or "").strip().lower()
    if not op_sha or op_sha != cand_sha:
        raise InvalidMDA2FetchError(
            f"Inventory hash drift between operation '{op_sha}' and candidate '{cand_sha}'"
        )

    op_run_id = str(op.get("run_id") or "").strip()
    if not op_run_id or not is_valid_run_id(op_run_id):
        raise InvalidMDA2FetchError(f"Invalid or unsafe run_id in operation: {op_run_id!r}")

    # Recompute and verify review_hash
    expected_review_hash = compute_review_hash(
        account=cand_account,
        message_id=cand_mid,
        folder=cand_folder,
        envelope_id=cand_eid,
        part_locator=cand_locator,
        inventory_sha256=cand_sha,
    )
    op_rev_hash = str(op.get("review_hash") or "").strip().lower()
    if not op_rev_hash:
        raise InvalidMDA2FetchError("MD-A2 operation missing 'review_hash'")
    if op_rev_hash != expected_review_hash:
        raise InvalidMDA2FetchError(
            f"Review hash drift in operation: '{op_rev_hash}' != expected '{expected_review_hash}'"
        )

    # Verify approval_receipt
    receipt = op.get("approval_receipt")
    # Human-Approval boundary: reject the machine receipt class/type/issuer fail-closed.
    guard_context_authorization(receipt, context=CONTEXT_FILING)
    try:
        verify_approval_receipt(receipt, expected_review_hash=expected_review_hash)
    except Exception as exc:
        raise InvalidMDA2FetchError(f"Approval receipt verification failed: {exc}") from exc

    # Check embedded candidate in operation if present
    if "candidate" in op and isinstance(op["candidate"], Mapping):
        embedded_cand = op["candidate"]
        for field in ("account", "folder", "envelope_id", "part_locator", "sha256", "filename"):
            v_emb = str(embedded_cand.get(field) or "").strip().lower()
            v_cand = str(cand.get(field) or "").strip().lower()
            if v_emb and v_cand and v_emb != v_cand:
                raise InvalidMDA2FetchError(
                    f"Drift between operation's embedded candidate and caller candidate on '{field}': '{v_emb}' != '{v_cand}'"
                )

    # 3. Validate MD-A2 Fetch Result
    res_status = str(res.get("status") or "").strip().lower()
    if res_status not in ("fetched", "already_fetched"):
        raise InvalidMDA2FetchError(
            f"MD-A2 fetch result status must be 'fetched' or 'already_fetched', got: {res_status!r}"
        )

    if res.get("error") is not None:
        raise InvalidMDA2FetchError(f"MD-A2 fetch result contains error: {res.get('error')!r}")

    res_run_id = str(res.get("run_id") or "").strip()
    if not res_run_id or res_run_id != op_run_id:
        raise InvalidMDA2FetchError(
            f"Run-ID drift between result '{res_run_id}' and operation '{op_run_id}'"
        )

    res_fn = str(res.get("filename") or "").strip()
    clean_cand_fn = sanitize_attachment_filename(cand_fn)
    if not res_fn or (res_fn != cand_fn and res_fn != clean_cand_fn):
        raise InvalidMDA2FetchError(
            f"Filename drift between result '{res_fn}' and candidate '{cand_fn}'"
        )

    res_fetch_sha = str(res.get("fetch_sha256") or "").strip().lower()
    res_inv_sha = str(res.get("inventory_sha256") or "").strip().lower()
    if not SHA256_HEX_REGEX.fullmatch(res_fetch_sha) or res_fetch_sha != cand_sha:
        raise InvalidMDA2FetchError(
            f"Fetch SHA-256 drift in result: '{res_fetch_sha}' != candidate '{cand_sha}'"
        )
    if not SHA256_HEX_REGEX.fullmatch(res_inv_sha) or res_inv_sha != cand_sha:
        raise InvalidMDA2FetchError(
            f"Inventory SHA-256 drift in result: '{res_inv_sha}' != candidate '{cand_sha}'"
        )

    clean_filename = sanitize_attachment_filename(cand_fn)
    if not clean_filename or clean_filename == "unknown_attachment":
        raise InvalidMDA2FetchError(
            f"Filename '{cand_fn}' sanitized to invalid or empty name: {clean_filename!r}"
        )

    res_rel_path = str(res.get("relative_path") or "").strip().replace(chr(92), "/")
    expected_rel_path = f"data/mail-desk/attachments/{op_run_id}/{clean_filename}"
    if res_rel_path != expected_rel_path:
        raise InvalidMDA2FetchError(
            f"MD-A2 relative_path must be exactly '{expected_rel_path}', got: {res_rel_path!r}"
        )

    eff_mime = str(res.get("effective_mime_type") or "").strip().lower()
    if not eff_mime:
        raise InvalidMDA2FetchError("MD-A2 fetch result missing 'effective_mime_type'")

    res_size = res.get("size_bytes")
    if res_size is None or isinstance(res_size, bool) or not isinstance(res_size, int) or res_size <= 0:
        raise InvalidMDA2FetchError(f"Invalid size_bytes in MD-A2 fetch result: {res_size!r}")

    # 4. Quarantine Evidence (Read-Only Physical Verification when requested)
    quarantine_evidence: dict[str, Any] = {
        "run_id": op_run_id,
        "relative_path": res_rel_path,
        "fetch_status": res_status,
        "status": res_status,
        "sha256": cand_sha,
        "effective_mime_type": eff_mime,
        "size_bytes": res_size,
        "physical_verified": False,
    }

    if verify_physical_evidence:
        try:
            phys_evidence = verify_quarantine_attachment_artifact(
                run_id=op_run_id,
                relative_path=res_rel_path,
                expected_sha256=cand_sha,
                expected_size_bytes=res_size,
                message_id=cand_mid,
                workspace_root=workspace_root,
                clean_filename=clean_filename,
            )
            quarantine_evidence.update(phys_evidence)
        except (QuarantineInventoryError, SymlinkEscapeError, FileNotFoundError, ValueError, OSError) as exc:
            raise InvalidMDA2FetchError(
                f"Physical quarantine verification failed: {exc}"
            ) from exc

    return {
        "account": eff_bound_account,
        "message_id": cand_mid,
        "folder": cand_folder,
        "envelope_id": cand_eid,
        "part_locator": cand_locator,
        "filename": cand_fn,
        "original_filename": cand_fn,
        "clean_filename": clean_filename,
        "sha256": cand_sha,
        "status": res_status,
        "quarantine_path": res_rel_path,
        "quarantine_evidence": quarantine_evidence,
        "size_bytes": res_size,
        "effective_mime_type": eff_mime,
        "operation": dict(op),
        "result": dict(res),
        "candidate": dict(cand),
    }


# ==============================================================================
# Helper Functions: Freshness & Hashing
# ==============================================================================

def validate_filemap_freshness(
    filemap_data: Mapping[str, Any],
    max_age_seconds: int = DEFAULT_MAX_FILEMAP_AGE_SECONDS,
    current_time: datetime | None = None,
) -> tuple[bool, str | None, str | None]:
    """Validate filemap timestamp format and age.

    Returns:
        tuple[bool, str | None, str | None]: (is_fresh, updated_at_str, error_reason)
    """
    if not isinstance(filemap_data, Mapping):
        return False, None, "Filemap data must be a dictionary"

    updated_at_str = filemap_data.get("updated_at")
    if not updated_at_str or not isinstance(updated_at_str, str):
        return False, None, "Filemap missing valid 'updated_at' timestamp"

    dt: datetime | None = None
    for fmt in TIMESTAMP_FORMATS:
        try:
            dt = datetime.strptime(updated_at_str.strip(), fmt)
            break
        except ValueError:
            continue

    if dt is None:
        return False, updated_at_str, f"Unsupported timestamp format: '{updated_at_str}'"

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    now = current_time or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    age = (now - dt).total_seconds()
    if age < 0:
        return False, updated_at_str, f"Filemap timestamp is in the future: {updated_at_str}"
    if age > max_age_seconds:
        return (
            False,
            updated_at_str,
            f"Filemap is stale ({int(age)}s old > limit of {max_age_seconds}s)",
        )

    return True, updated_at_str, None


def compute_candidate_hash(candidate: Mapping[str, Any]) -> str:
    """Produce deterministic 64-character SHA-256 for filing candidate state."""
    canon = {
        "schema_version": candidate.get("schema_version", 1),
        "candidate_type": candidate.get("candidate_type"),
        "promotion_status": candidate.get("promotion_status"),
        "status": candidate.get("status"),
        "reason": candidate.get("reason"),
        "source": candidate.get("source"),
        "destination": candidate.get("destination"),
        "filemap_evidence": candidate.get("filemap_evidence"),
        "handoff_hash": candidate.get("handoff_hash"),
        "dedupe": candidate.get("dedupe"),
        "coverage_evidence": candidate.get("coverage_evidence"),
    }
    dumped = json.dumps(canon, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(dumped.encode("utf-8")).hexdigest()


# ==============================================================================
# Workspace Containment & Path Security
# ==============================================================================

def resolve_and_validate_filemap_path(
    storage_cfg: Mapping[str, Any],
    decision: Mapping[str, Any],
    workspace_root: Path,
) -> tuple[Path | None, str | None]:
    """Safely resolve output_json filemap path within workspace_root.

    Fails closed on:
    - absolute paths in storage_cfg["output_json"]
    - '..' traversal components
    - path escaping workspace_root after resolution
    - symlink escape in parent path hierarchy
    """
    raw_path = storage_cfg.get("output_json") or storage_cfg.get("filemap_json")
    if not raw_path:
        return get_default_filemap_path(decision, workspace_root)

    if not isinstance(raw_path, str) or not raw_path.strip():
        return None, "Invalid empty output_json in storage configuration"

    cleaned = raw_path.strip().replace(chr(92), "/")
    posix_path = PurePosixPath(cleaned)

    if posix_path.is_absolute() or cleaned.startswith("/") or re.match(r"^[A-Za-z]:/", cleaned):
        return None, f"Filemap path must be workspace-relative, absolute path forbidden: {cleaned}"

    if ".." in posix_path.parts:
        return None, f"Filemap path contains directory traversal components: {cleaned}"

    resolved_root = workspace_root.resolve()
    target_path = (resolved_root / Path(*posix_path.parts)).resolve()

    try:
        target_path.relative_to(resolved_root)
    except ValueError:
        return None, f"Filemap path escapes workspace root: {target_path} not within {resolved_root}"

    # Parent directory symlink inspection
    curr = target_path.parent
    while curr != resolved_root and curr != curr.parent:
        if curr.is_symlink():
            real_parent = curr.resolve()
            try:
                real_parent.relative_to(resolved_root)
            except ValueError:
                return None, f"Symlink escape detected in filemap parent directory: {curr}"
        curr = curr.parent

    return target_path, None


def get_default_filemap_path(
    decision: Mapping[str, Any],
    workspace_root: Path,
) -> tuple[Path | None, str | None]:
    """Derive standard filemap.json path based on decision kind and id."""
    ws = workspace_root.resolve()
    kind = str(decision.get("kind") or "").strip().lower()
    ent_id = str(decision.get("id") or "").strip()
    folder_kind = "projects" if kind == "project" else "topics"
    target = (ws / "memory" / "cloud" / folder_kind / ent_id / "filemap.json").resolve()
    try:
        target.relative_to(ws)
    except ValueError:
        return None, "Default filemap path resolves outside workspace root"
    return target, None


# ==============================================================================
# Cloud-Atlas Filemap Contract Validation
# ==============================================================================

def validate_cloud_atlas_filemap(
    filemap_data: Any,
    expected_storage_id: str,
    expected_scope: str,
    expected_project_id: str,
    storage_cfg: Mapping[str, Any],
    max_age_seconds: int = DEFAULT_MAX_FILEMAP_AGE_SECONDS,
    current_time: datetime | None = None,
    workspace_root: Path | str | None = None,
) -> tuple[bool, str | None, str | None]:
    """Validate filemap strictly against Cloud Atlas normative schema.

    Delegates structural and entry validation to canonical Cloud-Atlas validate_filemap:
    - $schema URI and schema_version == 1
    - kind == 'cloud-filemap'
    - scope in {'project', 'topic'}
    - storage_id regex
    - project and project_title non-empty strings
    - scan_dir and output_dir safe workspace-relative paths
    - files object with safe paths within scan_dir
    - file entries with required keys (version, mtime, size, sha256, description) and 64-hex SHA-256

    Validates context bindings:
    - scope matches expected_scope
    - storage_id matches expected_storage_id
    - project matches expected_project_id
    - catalog scan_dir / output_dir matches
    - timestamp freshness within max_age_seconds

    Returns:
        (is_valid, updated_at_str, error_reason)
    """
    if not isinstance(filemap_data, Mapping):
        return False, None, "Filemap data must be an object (Mapping)"

    # 1. Canonical Cloud-Atlas Validator
    validate_fn = _load_cloud_atlas_validate_filemap()
    ws_str = str(workspace_root or Path.cwd())
    try:
        validate_fn(dict(filemap_data), workspace_root=ws_str)
    except Exception as exc:
        return False, None, f"Canonical Cloud-Atlas filemap validation failed: {exc}"

    # 2. Context Bindings
    scope = filemap_data.get("scope")
    if scope != expected_scope:
        return False, None, f"Filemap scope '{scope}' does not match decision scope '{expected_scope}'"

    storage_id = filemap_data.get("storage_id")
    if storage_id != expected_storage_id:
        return (
            False,
            None,
            f"Filemap storage_id '{storage_id}' does not match expected storage '{expected_storage_id}'",
        )

    project = filemap_data.get("project")
    if project != expected_project_id:
        return (
            False,
            None,
            f"Filemap project '{project}' does not match expected entity '{expected_project_id}'",
        )

    scan_dir = filemap_data.get("scan_dir")
    if storage_cfg.get("scan_dir"):
        cfg_scan = str(storage_cfg["scan_dir"]).strip().replace(chr(92), "/")
        fm_scan = str(scan_dir).strip().replace(chr(92), "/")
        if cfg_scan != fm_scan:
            return False, None, f"Filemap scan_dir '{fm_scan}' does not match catalog scan_dir '{cfg_scan}'"

    output_dir = filemap_data.get("output_dir")
    if storage_cfg.get("output_dir"):
        cfg_out = str(storage_cfg["output_dir"]).strip().replace(chr(92), "/")
        fm_out = str(output_dir).strip().replace(chr(92), "/")
        if cfg_out != fm_out:
            return False, None, f"Filemap output_dir '{fm_out}' does not match catalog output_dir '{cfg_out}'"

    # 3. Freshness Check
    is_fresh, updated_at, freshness_err = validate_filemap_freshness(
        filemap_data,
        max_age_seconds=max_age_seconds,
        current_time=current_time,
    )
    if not is_fresh:
        return False, updated_at, freshness_err

    return True, updated_at, None


# ==============================================================================
# Catalog Resolution
# ==============================================================================

def resolve_catalog_storage(
    decision: Mapping[str, Any],
    catalogs: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """Resolve storage configuration from catalogs based on real decision schema.

    Supports:
    - kind: 'project' with id
    - kind: 'topic' with id, optional subtopic, optional event
    - legacy kind: 'subtopic' or 'event' (for backward compatibility)

    Returns:
        tuple[storage_mapping | None, reason | None, error_code | None]:
        (cloud_sync_dict, reason, error_code)
    """
    raw_kind = str(decision.get("kind") or "").strip().lower()
    entity_id = str(decision.get("id") or "").strip()
    subtopic_id = str(decision.get("subtopic") or "").strip()
    event_id = str(decision.get("event") or "").strip()

    # Legacy mapping: if kind == 'subtopic', treat as topic with subtopic scalar
    if raw_kind == "subtopic":
        raw_kind = "topic"
        topic_parent = str(decision.get("topic") or "").strip()
        if topic_parent:
            subtopic_id = entity_id
            entity_id = topic_parent

    # Legacy mapping: if kind == 'event', treat as topic with event scalar
    if raw_kind == "event":
        raw_kind = "topic"
        topic_parent = str(decision.get("topic") or "").strip()
        sub_parent = str(decision.get("subtopic") or "").strip()
        if topic_parent:
            event_id = entity_id
            entity_id = topic_parent
            subtopic_id = sub_parent

    if raw_kind == "project":
        projects = catalogs.get("projects", [])
        proj = next((p for p in projects if str(p.get("id") or "").strip() == entity_id), None)
        if not proj:
            return None, f"Project '{entity_id}' not found in catalogs", STATUS_NOT_CONFIGURED
        cloud_sync = proj.get("cloud_sync")
        if not cloud_sync or not isinstance(cloud_sync, Mapping):
            return None, f"Project '{entity_id}' has no cloud_sync configured", STATUS_NOT_CONFIGURED
        return dict(cloud_sync), None, None

    if raw_kind == "topic":
        topics = catalogs.get("topics", [])
        top = next((t for t in topics if str(t.get("id") or "").strip() == entity_id), None)
        if not top:
            return None, f"Topic '{entity_id}' not found in catalogs", STATUS_NOT_CONFIGURED

        # Check subtopic existence if requested
        sub_obj = None
        if subtopic_id:
            subtopics = top.get("subtopics", [])
            sub_obj = next((s for s in subtopics if str(s.get("id") or "").strip() == subtopic_id), None)
            if not sub_obj:
                return (
                    None,
                    f"Subtopic '{subtopic_id}' not found under topic '{entity_id}'",
                    STATUS_NOT_CONFIGURED,
                )

        # Check event existence and storage selector if requested
        if event_id:
            events_pool = []
            event_parent_sub = None
            if sub_obj:
                events_pool = sub_obj.get("events", [])
                event_parent_sub = sub_obj
            else:
                for s in top.get("subtopics", []):
                    for ev in s.get("events", []):
                        events_pool.append(ev)
                        if str(ev.get("id") or "").strip() == event_id:
                            event_parent_sub = s

            ev_obj = next((e for e in events_pool if str(e.get("id") or "").strip() == event_id), None)
            if not ev_obj:
                return (
                    None,
                    f"Event '{event_id}' not found in topic '{entity_id}' catalog",
                    STATUS_NOT_CONFIGURED,
                )

            # Event storage resolution
            ev_cloud_storage = ev_obj.get("cloud_storage")
            if isinstance(ev_cloud_storage, Mapping):
                ev_scope = str(ev_cloud_storage.get("scope") or "").strip().lower()
                ev_storage_id = str(ev_cloud_storage.get("storage_id") or "").strip()

                if ev_scope == "subtopic":
                    if not event_parent_sub:
                        return (
                            None,
                            f"Event '{event_id}' cloud_storage specifies subtopic scope, but has no parent subtopic",
                            STATUS_STORAGE_REVIEW_REQUIRED,
                        )
                    sub_sync = event_parent_sub.get("cloud_sync")
                    if not isinstance(sub_sync, Mapping) or ev_storage_id not in sub_sync:
                        return (
                            None,
                            f"Event '{event_id}' selector specifies storage '{ev_storage_id}' in subtopic scope, but subtopic has no such storage",
                            STATUS_STORAGE_REVIEW_REQUIRED,
                        )
                    return {ev_storage_id: sub_sync[ev_storage_id]}, None, None

                elif ev_scope == "topic":
                    if not isinstance(top.get("cloud_sync"), Mapping) or ev_storage_id not in top["cloud_sync"]:
                        return (
                            None,
                            f"Event '{event_id}' selector specifies storage '{ev_storage_id}' in topic scope, but topic has no such storage",
                            STATUS_STORAGE_REVIEW_REQUIRED,
                        )
                    return {ev_storage_id: top["cloud_sync"][ev_storage_id]}, None, None

            # No explicit selector: inherit from subtopic or topic
            candidate_sync = None
            if event_parent_sub and isinstance(event_parent_sub.get("cloud_sync"), Mapping) and event_parent_sub["cloud_sync"]:
                candidate_sync = event_parent_sub["cloud_sync"]
            elif isinstance(top.get("cloud_sync"), Mapping) and top["cloud_sync"]:
                candidate_sync = top["cloud_sync"]

            if not candidate_sync:
                return None, f"Event '{event_id}' has no explicitly cataloged parent or subtopic storage", STATUS_NOT_CONFIGURED
            return dict(candidate_sync), None, None

        if sub_obj:
            # Subtopic cloud_sync or inherit from topic
            if isinstance(sub_obj.get("cloud_sync"), Mapping) and sub_obj["cloud_sync"]:
                return dict(sub_obj["cloud_sync"]), None, None
            if isinstance(top.get("cloud_sync"), Mapping) and top["cloud_sync"]:
                return dict(top["cloud_sync"]), None, None
            return None, f"Subtopic '{subtopic_id}' has no explicit or inherited cloud_sync", STATUS_NOT_CONFIGURED

        top_sync = top.get("cloud_sync")
        if not top_sync or not isinstance(top_sync, Mapping):
            return None, f"Topic '{entity_id}' has no cloud_sync configured", STATUS_NOT_CONFIGURED
        return dict(top_sync), None, None

    return None, f"Unsupported decision kind '{raw_kind}' for filing", STATUS_NOT_CONFIGURED


# ==============================================================================
# Filing Proposal Generator
# ==============================================================================

def propose_attachment_filing(
    attachment: Mapping[str, Any] | None = None,
    decision: Mapping[str, Any] | None = None,
    catalogs: Mapping[str, Any] | None = None,
    *,
    manifest_account: str | None = None,
    bound_account: str | None = None,
    operation: Mapping[str, Any] | None = None,
    fetch_result: Mapping[str, Any] | None = None,
    candidate: Mapping[str, Any] | None = None,
    filemaps: Mapping[str, Any] | None = None,
    workspace_root: Path | None = None,
    handoff: Mapping[str, Any] | None = None,
    canonical_parts: Sequence[Mapping[str, Any]] | None = None,
    verify_physical_evidence: bool = False,
    max_filemap_age_seconds: int = DEFAULT_MAX_FILEMAP_AGE_SECONDS,
    current_time: datetime | None = None,
) -> dict[str, Any]:
    """Generate a read-only attachment filing proposal candidate.

    Evaluates the strict decision matrix:
    - kein Storage -> not_configured
    - mehrere Storages oder ungültige/stale Filemap -> storage_review_required
    - kein belegtes Verzeichnis -> directory_review_required
    - gleicher Hash -> already_present
    - gleicher Name/anderer Hash -> collision_detected
    - eindeutiges Ziel -> proposed

    Guarantees:
    - Strictly validates MD-A2 composite contract (fails closed on free dicts or drifted fields).
    - Requires explicit bound manifest account ('manifest_account' or 'bound_account').
    - Validates MD-A4 handoff when provided and binds handoff_hash into candidate.
    - Never modifies filemap, catalogs, or cloud files.
    - Path traversal protection on filenames and directories.
    - Returns promotion_status: 'pending_human_review'.
    - Produces deterministic candidate_hash.
    """
    if decision is None:
        raise ValueError("decision is mandatory for attachment filing proposal")
    if catalogs is None:
        raise ValueError("catalogs is mandatory for attachment filing proposal")

    # 0. Validate MD-A2 Attachment Input via Composite Contract
    norm_att = validate_mda2_attachment(
        attachment,
        manifest_account=manifest_account,
        bound_account=bound_account,
        operation=operation,
        fetch_result=fetch_result,
        candidate=candidate,
        workspace_root=workspace_root,
        verify_physical_evidence=verify_physical_evidence,
    )

    base_candidate: dict[str, Any] = {
        "schema_version": 1,
        "candidate_type": "attachment_filing_candidate",
        "promotion_status": PROMOTION_STATUS_PENDING_HUMAN_REVIEW,
        "status": STATUS_NOT_CONFIGURED,
        "reason": "",
        "source": {
            "account": norm_att["account"],
            "message_id": norm_att["message_id"],
            "folder": norm_att["folder"],
            "envelope_id": norm_att["envelope_id"],
            "part_locator": norm_att["part_locator"],
            "filename": norm_att["filename"],
            "original_filename": norm_att["original_filename"],
            "sha256": norm_att["sha256"],
            "quarantine_path": norm_att["quarantine_path"],
            "quarantine_evidence": norm_att["quarantine_evidence"],
        },
        "quarantine_evidence": norm_att["quarantine_evidence"],
        "destination": {
            "storage_id": None,
            "target_dir": None,
            "target_filename": norm_att["clean_filename"],
            "target_relative_path": None,
        },
        "filemap_evidence": {
            "filemap_path": None,
            "filemap_updated_at": None,
            "schema_version": None,
            "kind": None,
            "scope": None,
            "storage_id": None,
            "project": None,
            "scan_dir": None,
            "output_dir": None,
            "is_stale": False,
        },
        "handoff_evidence": None,
        "handoff_hash": None,
        "dedupe": {
            "already_present": False,
            "collision_detected": False,
            "existing_path": None,
            "existing_sha256": None,
        },
    }

    # 1. Validate MD-A4 Handoff if provided
    if handoff is not None:
        mail_identity = {
            "account": norm_att["account"],
            "message_id": norm_att["message_id"],
            "folder": norm_att["folder"],
            "envelope_id": norm_att["envelope_id"],
        }
        validated_handoff = validate_attachment_handoff(
            handoff,
            mail_identity=mail_identity,
            decision=decision,
            canonical_parts=canonical_parts,
        )
        base_candidate["handoff_hash"] = validated_handoff["handoff_hash"]
        base_candidate["handoff_evidence"] = {
            "handoff_hash": validated_handoff["handoff_hash"],
            "status": validated_handoff.get("status"),
            "items_count": len(validated_handoff.get("items", [])),
        }

        matching_item = None
        for it in validated_handoff.get("items", []):
            if (
                str(it.get("part_locator") or "").strip() == str(norm_att.get("part_locator") or "").strip()
                and str(it.get("source_sha256") or it.get("sha256") or "").strip().lower() == str(norm_att.get("sha256") or "").strip().lower()
            ):
                matching_item = it
                break

        if matching_item is not None:
            base_candidate["coverage_evidence"] = {
                "analysis_completeness": matching_item.get("analysis_completeness"),
                "truncation_reason": matching_item.get("truncation_reason"),
                "truncation_stage": matching_item.get("truncation_stage"),
                "handoff_character_count": matching_item.get("handoff_character_count"),
                "analysis_character_budget": matching_item.get("analysis_character_budget"),
                "source_character_count": matching_item.get("source_character_count"),
            }

    # 2. Resolve storage from catalogs
    cloud_sync, err_reason, err_status = resolve_catalog_storage(decision, catalogs)
    if not cloud_sync:
        base_candidate["status"] = err_status or STATUS_NOT_CONFIGURED
        base_candidate["reason"] = err_reason or "No cloud storage configured"
        base_candidate["candidate_hash"] = compute_candidate_hash(base_candidate)
        return base_candidate

    # 3. Check storage ambiguity, archive, and read-only flags
    all_storage_ids = list(cloud_sync.keys())
    if len(all_storage_ids) > 1:
        base_candidate["status"] = STATUS_STORAGE_REVIEW_REQUIRED
        base_candidate["reason"] = f"Multiple cloud storages declared ({', '.join(all_storage_ids)}); human selection required"
        base_candidate["candidate_hash"] = compute_candidate_hash(base_candidate)
        return base_candidate

    storage_id = all_storage_ids[0]
    storage_cfg = cloud_sync[storage_id]
    if not isinstance(storage_cfg, Mapping):
        base_candidate["status"] = STATUS_STORAGE_REVIEW_REQUIRED
        base_candidate["reason"] = f"Storage configuration for '{storage_id}' is invalid"
        base_candidate["candidate_hash"] = compute_candidate_hash(base_candidate)
        return base_candidate

    if (
        storage_cfg.get("archive") is True
        or storage_cfg.get("read_only") is True
        or storage_id.lower().startswith("archive")
    ):
        base_candidate["status"] = STATUS_STORAGE_REVIEW_REQUIRED
        base_candidate["reason"] = f"Storage '{storage_id}' is designated as archive or read-only; active filing prohibited"
        base_candidate["candidate_hash"] = compute_candidate_hash(base_candidate)
        return base_candidate

    base_candidate["destination"]["storage_id"] = storage_id

    # 4. Resolve and load filemap
    filemap_data = None
    filemap_path_str = None

    if filemaps is not None:
        if storage_id in filemaps:
            filemap_data = filemaps[storage_id]
            filemap_path_str = f"<in-memory:{storage_id}>"
        else:
            # Rejection of generic 'filemap' fallback: must be keyed by exact storage_id
            base_candidate["status"] = STATUS_STORAGE_REVIEW_REQUIRED
            base_candidate["reason"] = (
                f"Storage '{storage_id}' not found in provided filemaps "
                f"(generic 'filemap' fallback prohibited)"
            )
            base_candidate["candidate_hash"] = compute_candidate_hash(base_candidate)
            return base_candidate
    else:
        # Resolve from disk
        ws = (workspace_root or Path.cwd()).resolve()
        resolved_path, path_err = resolve_and_validate_filemap_path(storage_cfg, decision, ws)
        if path_err or not resolved_path:
            base_candidate["status"] = STATUS_STORAGE_REVIEW_REQUIRED
            base_candidate["reason"] = path_err or "Invalid filemap path"
            base_candidate["candidate_hash"] = compute_candidate_hash(base_candidate)
            return base_candidate

        if not resolved_path.is_file():
            base_candidate["status"] = STATUS_STORAGE_REVIEW_REQUIRED
            base_candidate["reason"] = f"Filemap not found for storage '{storage_id}' (searched {resolved_path})"
            base_candidate["candidate_hash"] = compute_candidate_hash(base_candidate)
            return base_candidate

        filemap_path_str = str(resolved_path)
        try:
            with resolved_path.open("r", encoding="utf-8") as fh:
                filemap_data = json.load(fh)
        except Exception as exc:
            base_candidate["status"] = STATUS_STORAGE_REVIEW_REQUIRED
            base_candidate["reason"] = f"Failed to read filemap at {resolved_path}: {exc}"
            base_candidate["candidate_hash"] = compute_candidate_hash(base_candidate)
            return base_candidate

    base_candidate["filemap_evidence"]["filemap_path"] = filemap_path_str

    # 5. Validate Cloud-Atlas Filemap Contract
    kind = str(decision.get("kind") or "").strip().lower()
    expected_scope = "project" if kind == "project" else "topic"
    expected_project_id = str(decision.get("id") or "").strip()

    is_valid, updated_at, val_err = validate_cloud_atlas_filemap(
        filemap_data,
        expected_storage_id=storage_id,
        expected_scope=expected_scope,
        expected_project_id=expected_project_id,
        storage_cfg=storage_cfg,
        max_age_seconds=max_filemap_age_seconds,
        current_time=current_time,
        workspace_root=workspace_root,
    )
    base_candidate["filemap_evidence"]["filemap_updated_at"] = updated_at

    if not is_valid:
        if val_err and "stale" in val_err.lower():
            base_candidate["filemap_evidence"]["is_stale"] = True
        base_candidate["status"] = STATUS_STORAGE_REVIEW_REQUIRED
        base_candidate["reason"] = f"Filemap validation failed: {val_err}"
        base_candidate["candidate_hash"] = compute_candidate_hash(base_candidate)
        return base_candidate

    base_candidate["filemap_evidence"].update({
        "filemap_updated_at": updated_at,
        "schema_version": filemap_data.get("schema_version"),
        "kind": filemap_data.get("kind"),
        "scope": filemap_data.get("scope"),
        "storage_id": filemap_data.get("storage_id"),
        "project": filemap_data.get("project"),
        "scan_dir": filemap_data.get("scan_dir"),
        "output_dir": filemap_data.get("output_dir"),
        "is_stale": False,
    })

    # 6. Target Directory Resolution & Occupancy
    # NOTE: decision.target_dir is strictly prohibited/ignored!
    # Target directory comes ONLY from canonical catalog configuration or unique established directory in filemap.
    files_map = filemap_data["files"]
    scan_dir = str(filemap_data.get("scan_dir") or "").strip().replace(chr(92), "/").strip("/")

    target_dir: str | None = None
    configured_target_dir = storage_cfg.get("target_dir")
    if configured_target_dir and str(configured_target_dir).strip():
        target_dir = str(configured_target_dir).strip()
    else:
        # Deduce unique directory from filemap
        distinct_dirs: set[str] = set()
        for fp in files_map.keys():
            norm_fp = PurePosixPath(fp.replace(chr(92), "/"))
            if scan_dir and (norm_fp.as_posix() == scan_dir or norm_fp.as_posix().startswith(scan_dir + "/")):
                try:
                    rel_to_scan = norm_fp.relative_to(scan_dir)
                    parent = rel_to_scan.parent.as_posix()
                    if parent and parent != ".":
                        distinct_dirs.add(parent)
                except ValueError:
                    pass
            else:
                parent = norm_fp.parent.as_posix()
                if parent and parent != ".":
                    distinct_dirs.add(parent)

        if len(distinct_dirs) == 1:
            target_dir = next(iter(distinct_dirs))
        else:
            base_candidate["status"] = STATUS_DIRECTORY_REVIEW_REQUIRED
            base_candidate["reason"] = (
                f"No target directory configured in catalog for storage '{storage_id}', "
                f"and filemap does not contain a unique established directory ({len(distinct_dirs)} distinct directories)"
            )
            base_candidate["candidate_hash"] = compute_candidate_hash(base_candidate)
            return base_candidate

    norm_target_dir = PurePosixPath(str(target_dir).replace(chr(92), "/")).as_posix().strip("/")
    if not norm_target_dir or ".." in norm_target_dir.split("/") or norm_target_dir.startswith("/"):
        base_candidate["status"] = STATUS_DIRECTORY_REVIEW_REQUIRED
        base_candidate["reason"] = f"Target directory path contains invalid traversal or format: {target_dir!r}"
        base_candidate["candidate_hash"] = compute_candidate_hash(base_candidate)
        return base_candidate

    # Verify that norm_target_dir is occupied in files_map
    dir_prefix = norm_target_dir + "/"
    is_dir_occupied = any(
        fp.startswith(dir_prefix)
        or (scan_dir and fp.startswith(f"{scan_dir}/{dir_prefix}"))
        or PurePosixPath(fp).parent.as_posix() == norm_target_dir
        for fp in files_map.keys()
    )
    if not is_dir_occupied:
        base_candidate["status"] = STATUS_DIRECTORY_REVIEW_REQUIRED
        base_candidate["reason"] = f"Target directory '{norm_target_dir}' is not established/occupied in filemap"
        base_candidate["candidate_hash"] = compute_candidate_hash(base_candidate)
        return base_candidate

    clean_filename = norm_att["clean_filename"]
    target_relative_path = f"{norm_target_dir}/{clean_filename}"
    base_candidate["destination"]["target_dir"] = norm_target_dir
    base_candidate["destination"]["target_relative_path"] = target_relative_path

    # 7. Check for identical SHA-256 (already_present)
    sha256 = norm_att["sha256"]
    for existing_relpath, file_entry in files_map.items():
        if isinstance(file_entry, Mapping):
            ext_sha = str(file_entry.get("sha256") or "").strip().lower()
            if ext_sha and ext_sha == sha256:
                base_candidate["status"] = STATUS_ALREADY_PRESENT
                base_candidate["reason"] = f"File with identical SHA-256 already exists at '{existing_relpath}'"
                base_candidate["dedupe"] = {
                    "already_present": True,
                    "collision_detected": False,
                    "existing_path": existing_relpath,
                    "existing_sha256": ext_sha,
                }
                base_candidate["candidate_hash"] = compute_candidate_hash(base_candidate)
                return base_candidate

    # 8. Check for filename collision (same name, different SHA-256)
    candidate_check_paths = {target_relative_path}
    if scan_dir:
        candidate_check_paths.add(f"{scan_dir}/{target_relative_path}")

    colliding_path: str | None = None
    for cp in candidate_check_paths:
        if cp in files_map:
            colliding_path = cp
            break

    if colliding_path:
        existing_entry = files_map[colliding_path]
        ext_sha = (
            str(existing_entry.get("sha256") or "").strip().lower()
            if isinstance(existing_entry, Mapping)
            else ""
        )
        base_candidate["status"] = STATUS_COLLISION_DETECTED
        base_candidate["reason"] = (
            f"File with name '{clean_filename}' already exists at destination '{colliding_path}' "
            f"with different SHA-256 ({ext_sha})"
        )
        base_candidate["dedupe"] = {
            "already_present": False,
            "collision_detected": True,
            "existing_path": colliding_path,
            "existing_sha256": ext_sha,
        }
        base_candidate["candidate_hash"] = compute_candidate_hash(base_candidate)
        return base_candidate

    # 9. All preconditions satisfied: propose filing!
    base_candidate["status"] = STATUS_PROPOSED
    reason_str = f"Filing candidate proposed for storage '{storage_id}' at '{target_relative_path}'"
    cov = base_candidate.get("coverage_evidence")
    if cov and cov.get("analysis_completeness") in ("truncated", "partial", "unavailable"):
        comp = cov.get("analysis_completeness")
        t_reason = cov.get("truncation_reason") or "unspecified"
        reason_str = f"{reason_str} [Coverage: {comp} ({t_reason})]"
    base_candidate["reason"] = reason_str
    base_candidate["candidate_hash"] = compute_candidate_hash(base_candidate)
    return base_candidate
