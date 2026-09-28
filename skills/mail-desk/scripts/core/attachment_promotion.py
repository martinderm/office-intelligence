"""Human-gated approval binding, read-only promotion preflight (FR-09 / MD-P1)
and the atomic, idempotent, no-clobber storage writer (FR-09 / MD-P2).

MD-P1 is the first, deliberately non-mutating package of FR-09.  It exposes exactly
three public functions:

* :func:`compute_promotion_review_hash` -- canonical review payload hash binding the
  MD-A5 candidate schema/``candidate_hash``, the quarantine source identity, the
  re-resolved storage/``scan_dir``/target destination and the canonical filemap
  snapshot hash.
* :func:`verify_promotion_approval_receipt` -- Schema-1 human approval receipt
  verification (``receipt_type``, ``decision``, ``review_hash``, timezone-aware
  ``approved_at``/``expires_at`` and an optional comment) with an injected clock.
* :func:`preflight_attachment_promotion` -- the strictly read-only preflight that
  validates every promotion precondition in the documented FR-09 order and returns a
  deterministic ``attachment_promotion_preflight`` Schema 1.

The clearly separated writer section at the bottom of this module adds the MD-P2
public surface -- :func:`derive_promotion_id`, :func:`load_promotion_journal` and
:func:`promote_attachment` -- without changing any MD-P1 behaviour.  The writer
re-executes the MD-P1 preflight immediately before its first write and transfers
exactly one approved attachment via a single ``os.link`` no-clobber promotion,
journaling every phase atomically.

Trust boundaries (read this before relying on the preflight):

* Only Schema-1 candidates with ``candidate_type: "attachment_filing_candidate"``,
  ``status: "proposed"`` and ``promotion_status: "pending_human_review"`` may reach a
  writer.  Every other status is rejected fail-closed (``candidate_drift``).
* ``candidate_hash`` is recomputed canonically through
  :func:`core.quarantine.attachment_filing.compute_candidate_hash`; quarantine
  evidence is re-verified against the real on-disk artefact and inventory.  Nothing
  in the receipt is trusted for storage, ``scan_dir``, destination, catalogue or
  filemap data: those are re-resolved from the current catalogs and the candidate and
  only then cross-checked against the approved review hash.
* Lock ownership comes exclusively from the trusted harness control plane.  Embedded
  ``lease_id``/``conversation_id``/``workspace_root``/``allow_legacy`` values in a
  candidate or receipt are ignored.
* Storage writability is decided *only* by property markers on the candidate-bound
  storage entry, never by its identifier.  The complete marker vocabulary that yields
  ``storage_not_writable`` is: ``archived: true``, ``archive: true``,
  ``read_only: true``, ``active: false``, ``enabled: false``, ``disabled: true`` and
  ``status`` in ``{"inactive", "disabled", "archived"}``.  A storage merely named
  ``archive*`` (for example ``archive_2026_active``) with none of these markers is
  writable.
* Storage, ``scan_dir`` and target paths are always reconstructed as
  ``workspace_root / scan_dir / target_relative_path``; absolute paths, ``..``
  traversal, Windows device names and symlink/junction/reparse escapes stop
  fail-closed.  The target parent must already exist: MD-P1 creates no directories.
* The preflight is strictly read-only.  It never opens for write, never creates or
  removes files, never touches ``filemap.json``, catalogs or mailbox/cloud adapters.

Validator reuse: the module delegates to the existing public seams
:func:`compute_candidate_hash`, :func:`resolve_catalog_storage` and
:func:`validate_cloud_atlas_filemap` (attachment filing),
:func:`verify_quarantine_attachment_artifact` and :func:`verify_workspace_lock`
(attachment fetch), the tracked-quarantine guard
(:func:`verify_no_tracked_quarantine`) and
:func:`core.modes.dossier_synthesis.canonical_json_sha256`.  It implements no second
copy of any of those validators.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
from typing import Any, Callable, Mapping
import uuid

from core.attachment_authorization import (
    CONTEXT_PROMOTION,
    RECEIPT_TYPE_ATTACHMENT_AUTO_EVALUATION,
    guard_context_authorization,
)
from core.common import normalize_message_id, utc_now_iso
from core.modes.dossier_synthesis import canonical_json_sha256
from core.quarantine.attachment_fetch import (
    INVENTORY_FILENAME,
    QuarantineInventoryError,
    RFC3339_REGEX,
    SymlinkEscapeError,
    TrackedQuarantineError,
    WIN32_RESERVED_NAMES,
    _QuarantineInventoryLock,
    _load_quarantine_inventory,
    is_valid_run_id,
    resolve_workspace_root,
    verify_no_tracked_quarantine,
    verify_quarantine_attachment_artifact,
    verify_workspace_lock,
)
from core.quarantine.attachment_filing import (
    DEFAULT_MAX_FILEMAP_AGE_SECONDS,
    compute_candidate_hash,
    resolve_catalog_storage,
    validate_cloud_atlas_filemap,
)


# ==============================================================================
# Constants & bounded vocabularies
# ==============================================================================

SCHEMA_VERSION = 1
PREFLIGHT_KIND = "attachment_promotion_preflight"

CANDIDATE_TYPE_ATTACHMENT_FILING = "attachment_filing_candidate"
CANDIDATE_STATUS_PROPOSED = "proposed"
PROMOTION_STATUS_PENDING_HUMAN_REVIEW = "pending_human_review"

RECEIPT_TYPE_ATTACHMENT_PROMOTION_APPROVAL = "attachment_promotion_approval"
RECEIPT_DECISION_APPROVED = "approved"

#: A human approval receipt may not outlive this window (bounded, fail-closed).
MAX_APPROVAL_VALIDITY_SECONDS = 30 * 24 * 60 * 60

STATUS_READY = "ready"
STATUS_ALREADY_PRESENT = "already_present"

STOPCODE_APPROVAL_MISSING = "approval_missing"
STOPCODE_APPROVAL_INVALID = "approval_invalid"
STOPCODE_APPROVAL_EXPIRED = "approval_expired"
STOPCODE_CANDIDATE_DRIFT = "candidate_drift"
STOPCODE_SOURCE_DRIFT = "source_drift"
STOPCODE_LOCK_UNAVAILABLE = "lock_unavailable"
STOPCODE_CATALOG_DRIFT = "catalog_drift"
STOPCODE_STORAGE_NOT_WRITABLE = "storage_not_writable"
STOPCODE_FILEMAP_DRIFT = "filemap_drift"
STOPCODE_UNSAFE_PATH = "unsafe_path"
STOPCODE_PARENT_MISSING = "parent_missing"
STOPCODE_COLLISION_DETECTED = "collision_detected"
STOPCODE_PREFLIGHT_ERROR = "preflight_error"

#: The closed negative-status vocabulary the preflight may return.
PREFLIGHT_STOPCODES: frozenset[str] = frozenset(
    {
        STOPCODE_APPROVAL_MISSING,
        STOPCODE_APPROVAL_INVALID,
        STOPCODE_APPROVAL_EXPIRED,
        STOPCODE_CANDIDATE_DRIFT,
        STOPCODE_SOURCE_DRIFT,
        STOPCODE_LOCK_UNAVAILABLE,
        STOPCODE_CATALOG_DRIFT,
        STOPCODE_STORAGE_NOT_WRITABLE,
        STOPCODE_FILEMAP_DRIFT,
        STOPCODE_UNSAFE_PATH,
        STOPCODE_PARENT_MISSING,
        STOPCODE_COLLISION_DETECTED,
        STOPCODE_PREFLIGHT_ERROR,
    }
)

#: The filemap snapshot projection hashed into the review payload.
_FILEMAP_SNAPSHOT_KEYS: tuple[str, ...] = (
    "schema_version",
    "kind",
    "scope",
    "storage_id",
    "project",
    "project_title",
    "scan_dir",
    "output_dir",
    "updated_at",
    "files",
)

#: Source identity fields bound into the canonical review payload.
_SOURCE_REVIEW_KEYS: tuple[str, ...] = (
    "account",
    "message_id",
    "folder",
    "envelope_id",
    "part_locator",
    "quarantine_path",
    "sha256",
)

_SHA256_HEX_REGEX = re.compile(r"^[0-9a-f]{64}$")
_DRIVE_PREFIX_REGEX = re.compile(r"^[A-Za-z]:")


# ==============================================================================
# Typed verification result
# ==============================================================================

@dataclass(frozen=True)
class PromotionApprovalVerification:
    """Typed, fail-closed outcome of Schema-1 promotion approval verification."""

    ok: bool
    reason: str | None
    stopcode: str | None
    review_hash: str | None = None
    approved_at: str | None = None
    expires_at: str | None = None
    comment: str | None = None


def _verification_failure(stopcode: str, reason: str) -> PromotionApprovalVerification:
    return PromotionApprovalVerification(ok=False, reason=reason, stopcode=stopcode)


# ==============================================================================
# Small pure helpers
# ==============================================================================

def _is_nonempty_text(value: Any) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _is_sha256_hex(value: Any) -> bool:
    return isinstance(value, str) and bool(_SHA256_HEX_REGEX.fullmatch(value.strip().lower()))


def _normalize_slashes(value: Any) -> str:
    return str(value or "").strip().replace("\\", "/")


def _parse_rfc3339(value: Any) -> datetime | None:
    """Parse a timezone-aware RFC-3339 timestamp or return ``None`` (fail-closed)."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not RFC3339_REGEX.fullmatch(text):
        return None
    normalized = text[:-1] + "+00:00" if text[-1] in "Zz" else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _filemap_snapshot(filemap: Mapping[str, Any]) -> dict[str, Any]:
    """Return the deterministic filemap snapshot projection hashed into the payload."""
    if not isinstance(filemap, Mapping):
        raise ValueError("A promotion review payload requires a filemap mapping.")
    return {key: filemap.get(key) for key in _FILEMAP_SNAPSHOT_KEYS}


def _filemap_snapshot_hash(filemap: Mapping[str, Any]) -> str:
    return canonical_json_sha256(_filemap_snapshot(filemap))


def _guard_human_promotion_receipt(receipt: Mapping[str, Any]) -> None:
    """Reuse the shared context guard while accepting the FR-09 typed human receipt.

    The shared guard treats every unknown non-empty ``receipt_type`` as foreign.  The
    FR-09 promotion schema is itself a legitimate human type, so only the machine
    identity signals (``receipt_class``, the machine issuer and the machine
    ``receipt_type``) are probed; a machine/foreign class therefore still fails closed.
    """
    probe: dict[str, Any] = {
        "receipt_class": receipt.get("receipt_class"),
        "approved_by": receipt.get("approved_by"),
    }
    receipt_type = receipt.get("receipt_type")
    if (
        receipt_type is not None
        and str(receipt_type).strip().lower() == RECEIPT_TYPE_ATTACHMENT_AUTO_EVALUATION
    ):
        probe["receipt_type"] = receipt_type
    guard_context_authorization(probe, context=CONTEXT_PROMOTION)


# ==============================================================================
# Public: canonical review payload hash
# ==============================================================================

def compute_promotion_review_hash(
    candidate: Mapping[str, Any],
    filemap_snapshot: Mapping[str, Any],
    *,
    candidate_hash: str | None = None,
    storage_id: str | None = None,
    scan_dir: str | None = None,
    target_dir: str | None = None,
    target_filename: str | None = None,
    target_relative_path: str | None = None,
) -> str:
    """Return the canonical 64-hex SHA-256 of the promotion review payload.

    The payload binds candidate schema and ``candidate_hash``, the quarantine source
    identity, the storage/``scan_dir``/destination values and the canonical hash of
    the fully-validated filemap snapshot.  Callers may override the resolved
    destination values explicitly; otherwise they are derived from the candidate so a
    receipt can later be compared against the same canonical payload.
    """
    if not isinstance(candidate, Mapping):
        raise ValueError("compute_promotion_review_hash requires a candidate mapping.")
    source = candidate.get("source")
    destination = candidate.get("destination")
    filemap_evidence = candidate.get("filemap_evidence")
    if not isinstance(source, Mapping):
        source = {}
    if not isinstance(destination, Mapping):
        destination = {}
    if not isinstance(filemap_evidence, Mapping):
        filemap_evidence = {}

    effective_candidate_hash = (
        candidate_hash if candidate_hash is not None else candidate.get("candidate_hash")
    )
    effective_storage_id = storage_id if storage_id is not None else destination.get("storage_id")
    effective_scan_dir = scan_dir if scan_dir is not None else filemap_evidence.get("scan_dir")
    effective_target_dir = target_dir if target_dir is not None else destination.get("target_dir")
    effective_target_filename = (
        target_filename if target_filename is not None else destination.get("target_filename")
    )
    effective_target_relative_path = (
        target_relative_path
        if target_relative_path is not None
        else destination.get("target_relative_path")
    )

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "candidate": {
            "schema_version": candidate.get("schema_version"),
            "candidate_type": candidate.get("candidate_type"),
            "candidate_hash": effective_candidate_hash,
        },
        "source": {key: source.get(key) for key in _SOURCE_REVIEW_KEYS},
        "destination": {
            "storage_id": effective_storage_id,
            "scan_dir": effective_scan_dir,
            "target_dir": effective_target_dir,
            "target_filename": effective_target_filename,
            "target_relative_path": effective_target_relative_path,
        },
        "filemap_snapshot_hash": _filemap_snapshot_hash(filemap_snapshot),
    }
    return canonical_json_sha256(payload)


# ==============================================================================
# Public: Schema-1 human approval receipt verification
# ==============================================================================

def verify_promotion_approval_receipt(
    receipt: Mapping[str, Any] | None,
    *,
    expected_review_hash: str,
    current_time: datetime | None = None,
    max_validity_seconds: int = MAX_APPROVAL_VALIDITY_SECONDS,
) -> PromotionApprovalVerification:
    """Verify a Schema-1 promotion approval receipt, fail-closed.

    Rejects missing receipts, machine/foreign receipt classes, unknown
    ``receipt_type``/``decision`` values, review-hash drift, naive or malformed
    timestamps, future-issued receipts, already-expired receipts and implausibly long
    validity windows.  ``current_time`` is the injectable clock.
    """
    if not isinstance(receipt, Mapping) or len(receipt) == 0:
        return _verification_failure(STOPCODE_APPROVAL_MISSING, "approval_missing")

    try:
        _guard_human_promotion_receipt(receipt)
    except Exception:
        return _verification_failure(STOPCODE_APPROVAL_INVALID, "receipt_class_rejected")

    receipt_type = str(receipt.get("receipt_type") or "").strip()
    if receipt_type != RECEIPT_TYPE_ATTACHMENT_PROMOTION_APPROVAL:
        return _verification_failure(STOPCODE_APPROVAL_INVALID, "receipt_type")

    decision = str(receipt.get("decision") or "").strip()
    if decision != RECEIPT_DECISION_APPROVED:
        return _verification_failure(STOPCODE_APPROVAL_INVALID, "decision")

    review_hash = str(receipt.get("review_hash") or "").strip().lower()
    if not _is_sha256_hex(review_hash):
        return _verification_failure(STOPCODE_APPROVAL_INVALID, "review_hash_format")
    expected = str(expected_review_hash or "").strip().lower()
    if not _is_sha256_hex(expected) or review_hash != expected:
        return _verification_failure(STOPCODE_APPROVAL_INVALID, "review_hash_mismatch")

    approved_at = receipt.get("approved_at")
    expires_at = receipt.get("expires_at")
    approved_dt = _parse_rfc3339(approved_at)
    expires_dt = _parse_rfc3339(expires_at)
    if approved_dt is None or expires_dt is None:
        return _verification_failure(STOPCODE_APPROVAL_INVALID, "timestamp")

    now = current_time if isinstance(current_time, datetime) else datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        now = now.replace(tzinfo=timezone.utc)

    if approved_dt > now:
        return _verification_failure(STOPCODE_APPROVAL_INVALID, "future_issued")
    if expires_dt <= now:
        return _verification_failure(STOPCODE_APPROVAL_EXPIRED, "approval_expired")
    if expires_dt <= approved_dt:
        return _verification_failure(STOPCODE_APPROVAL_INVALID, "validity_window")
    if (expires_dt - approved_dt).total_seconds() > float(max_validity_seconds):
        return _verification_failure(STOPCODE_APPROVAL_INVALID, "validity_too_long")

    comment = receipt.get("comment")
    if comment is not None and not isinstance(comment, str):
        return _verification_failure(STOPCODE_APPROVAL_INVALID, "comment")

    return PromotionApprovalVerification(
        ok=True,
        reason=None,
        stopcode=None,
        review_hash=review_hash,
        approved_at=str(approved_at).strip(),
        expires_at=str(expires_at).strip(),
        comment=comment,
    )


# ==============================================================================
# Preflight path & catalog helpers (private)
# ==============================================================================

def _is_safe_filename(name: Any) -> bool:
    """Reject absolute, traversing, separators, illegal characters and device names."""
    if not isinstance(name, str):
        return False
    cleaned = name.strip()
    if not cleaned or cleaned != name:
        return False
    if cleaned in (".", ".."):
        return False
    if "/" in cleaned or "\\" in cleaned:
        return False
    if "\x00" in cleaned:
        return False
    if re.search(r'[<>:"|?*]', cleaned):
        return False
    if cleaned.endswith(".") or cleaned.endswith(" "):
        return False
    stem = cleaned.split(".")[0].strip().upper()
    if stem in WIN32_RESERVED_NAMES or cleaned.upper() in WIN32_RESERVED_NAMES:
        return False
    return True


def _is_safe_relative_path(value: Any) -> bool:
    """Reject absolute/drive/traversal/device-name relative POSIX paths per component."""
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return False
    normalized = text.replace("\\", "/")
    if normalized.startswith("/") or _DRIVE_PREFIX_REGEX.match(normalized):
        return False
    parts = PurePosixPath(normalized).parts
    if not parts:
        return False
    return all(_is_safe_filename(part) for part in parts)


#: Backward-compatible private alias: directory validation uses the same predicate.
_is_safe_relative_dir = _is_safe_relative_path


def _has_reparse_or_symlink(start: Path, stop: Path) -> bool:
    """Return True when an existing component strictly inside ``stop`` is a symlink/reparse point.

    Both paths are inspected *unresolved* and only the components from ``start`` down
    to -- but excluding -- ``stop`` are examined.  ``stop`` is the trust boundary: for
    promotion it is the catalog-bound ``scan_dir`` root, which is the legitimate cloud
    mount (``data/cloud/<X>`` may itself be a junction to the internal mount target)
    and therefore must not be flagged; the caller's resolved-containment check still
    rejects a ``scan_dir`` root -- or an ancestor of it -- that resolves outside the
    workspace.  A ``start`` that is not inside ``stop`` fails closed.

    This must run before any ``resolve()``: a junction/reparse point sitting exactly on
    the target parent (or an interior ``target_dir`` component) would otherwise be
    dereferenced away and hidden from the containment check.  Strictly read-only --
    only ``os.path``/``lstat`` calls, never a mutation.
    """
    try:
        start.relative_to(stop)
    except ValueError:
        return True
    current = start
    while current != stop:
        if current.is_symlink():
            return True
        try:
            stat_result = os.lstat(current)
        except FileNotFoundError:
            stat_result = None
        except OSError:
            return True
        if stat_result is not None and os.name == "nt":
            if getattr(stat_result, "st_file_attributes", 0) & 0x400:
                return True
        if current.parent == current:
            break
        current = current.parent
    return False


def _target_path(workspace_root: Path, scan_dir: str, target_relative_path: str) -> Path:
    scan_parts = PurePosixPath(_normalize_slashes(scan_dir).strip("/")).parts
    target_parts = PurePosixPath(_normalize_slashes(target_relative_path).strip("/")).parts
    return workspace_root.joinpath(*scan_parts, *target_parts)


def _target_path_fingerprint(scan_dir: str, target_relative_path: str) -> str:
    return canonical_json_sha256(
        {
            "scan_dir": _normalize_slashes(scan_dir).strip("/"),
            "target_relative_path": _normalize_slashes(target_relative_path).strip("/"),
        }
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest().lower()


def _validate_candidate_gate(candidate: Mapping[str, Any]) -> str | None:
    """Return a stopcode when the candidate may not reach a writer, else ``None``."""
    if candidate.get("schema_version") != SCHEMA_VERSION:
        return STOPCODE_CANDIDATE_DRIFT
    if str(candidate.get("candidate_type") or "") != CANDIDATE_TYPE_ATTACHMENT_FILING:
        return STOPCODE_CANDIDATE_DRIFT
    if str(candidate.get("status") or "") != CANDIDATE_STATUS_PROPOSED:
        return STOPCODE_CANDIDATE_DRIFT
    if str(candidate.get("promotion_status") or "") != PROMOTION_STATUS_PENDING_HUMAN_REVIEW:
        return STOPCODE_CANDIDATE_DRIFT

    source = candidate.get("source")
    destination = candidate.get("destination")
    filemap_evidence = candidate.get("filemap_evidence")
    if not isinstance(source, Mapping) or not isinstance(destination, Mapping):
        return STOPCODE_CANDIDATE_DRIFT
    if not isinstance(filemap_evidence, Mapping):
        return STOPCODE_CANDIDATE_DRIFT
    for field in ("storage_id", "target_dir", "target_filename", "target_relative_path"):
        if not _is_nonempty_text(destination.get(field)):
            return STOPCODE_CANDIDATE_DRIFT
    for field in ("account", "message_id", "folder", "envelope_id", "part_locator",
                  "sha256", "quarantine_path"):
        if not _is_nonempty_text(source.get(field)):
            return STOPCODE_CANDIDATE_DRIFT
    if not _is_sha256_hex(source.get("sha256")):
        return STOPCODE_CANDIDATE_DRIFT
    return None


def _verify_quarantine_evidence(candidate: Mapping[str, Any], workspace_root: Path) -> str | None:
    """Re-verify tracked-path guard and real quarantine artefact; return a stopcode."""
    try:
        verify_no_tracked_quarantine(workspace_root)
    except TrackedQuarantineError:
        return STOPCODE_SOURCE_DRIFT
    except Exception:
        return STOPCODE_PREFLIGHT_ERROR

    source = candidate.get("source")
    destination = candidate.get("destination")
    if not isinstance(source, Mapping) or not isinstance(destination, Mapping):
        return STOPCODE_SOURCE_DRIFT

    sha256 = str(source.get("sha256") or "").strip().lower()
    relative_path = _normalize_slashes(source.get("quarantine_path"))
    message_id = str(source.get("message_id") or "").strip()
    source_evidence = source.get("quarantine_evidence")
    if not isinstance(source_evidence, Mapping):
        return STOPCODE_SOURCE_DRIFT

    run_id = str(source_evidence.get("run_id") or "").strip()
    if not run_id or not is_valid_run_id(run_id):
        return STOPCODE_SOURCE_DRIFT
    size_bytes = source_evidence.get("size_bytes")
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes <= 0:
        return STOPCODE_SOURCE_DRIFT
    if _normalize_slashes(source_evidence.get("relative_path")) != relative_path:
        return STOPCODE_SOURCE_DRIFT
    if str(source_evidence.get("sha256") or "").strip().lower() != sha256:
        return STOPCODE_SOURCE_DRIFT

    top_evidence = candidate.get("quarantine_evidence")
    if isinstance(top_evidence, Mapping):
        if str(top_evidence.get("run_id") or "").strip() != run_id:
            return STOPCODE_SOURCE_DRIFT
        if str(top_evidence.get("sha256") or "").strip().lower() != sha256:
            return STOPCODE_SOURCE_DRIFT
        if top_evidence.get("size_bytes") != size_bytes:
            return STOPCODE_SOURCE_DRIFT

    clean_filename = PurePosixPath(relative_path).name
    try:
        physical = verify_quarantine_attachment_artifact(
            run_id=run_id,
            relative_path=relative_path,
            expected_sha256=sha256,
            expected_size_bytes=size_bytes,
            message_id=message_id,
            workspace_root=workspace_root,
            clean_filename=clean_filename or None,
        )
    except (QuarantineInventoryError, SymlinkEscapeError, FileNotFoundError, ValueError, OSError):
        return STOPCODE_SOURCE_DRIFT
    except Exception:
        return STOPCODE_PREFLIGHT_ERROR

    if str(physical.get("sha256") or "").strip().lower() != sha256:
        return STOPCODE_SOURCE_DRIFT
    if physical.get("size_bytes") != size_bytes:
        return STOPCODE_SOURCE_DRIFT
    return None


def _resolve_current_storage(
    candidate: Mapping[str, Any],
    catalogs: Mapping[str, Any],
    decision: Mapping[str, Any] | None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Re-resolve the storage from the current catalogs; return ``(cfg, stopcode)``."""
    filemap_evidence = candidate.get("filemap_evidence")
    destination = candidate.get("destination")
    if not isinstance(filemap_evidence, Mapping) or not isinstance(destination, Mapping):
        return None, STOPCODE_CATALOG_DRIFT

    scope = str(filemap_evidence.get("scope") or "").strip().lower()
    entity_id = str(filemap_evidence.get("project") or "").strip()
    candidate_storage_id = str(destination.get("storage_id") or "").strip()

    effective_decision: Mapping[str, Any]
    if isinstance(decision, Mapping):
        effective_decision = decision
    else:
        effective_decision = {"kind": scope, "id": entity_id}

    cloud_sync, _reason, _status = resolve_catalog_storage(effective_decision, catalogs)
    if not cloud_sync:
        return None, STOPCODE_CATALOG_DRIFT

    # Evaluate the candidate-bound storage entry itself first: its read_only/archived/
    # inactive flags must stop as storage_not_writable even when the catalog offers
    # several storages (FR-09 storage-resolution step 4), and a non-Mapping entry is
    # likewise not writable.  Only afterwards does the unique-storage requirement apply.
    if candidate_storage_id not in cloud_sync:
        return None, STOPCODE_CATALOG_DRIFT
    storage_cfg = cloud_sync[candidate_storage_id]
    if not isinstance(storage_cfg, Mapping):
        return None, STOPCODE_STORAGE_NOT_WRITABLE

    # Writability is decided exclusively by property markers on the candidate-bound
    # storage entry -- never by the identifier.  A storage merely *named* ``archive*``
    # (e.g. ``archive_2026_active``) with no marker below stays writable.
    status_token = str(storage_cfg.get("status") or "").strip().lower()
    if (
        storage_cfg.get("archived") is True
        or storage_cfg.get("archive") is True
        or storage_cfg.get("read_only") is True
        or storage_cfg.get("active") is False
        or storage_cfg.get("enabled") is False
        or storage_cfg.get("disabled") is True
        or status_token in {"inactive", "disabled", "archived"}
    ):
        return None, STOPCODE_STORAGE_NOT_WRITABLE

    if len(cloud_sync) != 1:
        return None, STOPCODE_CATALOG_DRIFT

    return dict(storage_cfg), None


def _validate_target_paths(
    workspace_root: Path,
    destination: Mapping[str, Any],
    scan_dir: str,
) -> tuple[Path | None, str | None]:
    """Validate scan_dir/target containment and return ``(target_path, stopcode)``."""
    normalized_scan_dir = _normalize_slashes(scan_dir).strip("/")
    target_dir = destination.get("target_dir")
    target_filename = destination.get("target_filename")
    target_relative_path = destination.get("target_relative_path")

    if not _is_safe_relative_dir(normalized_scan_dir):
        return None, STOPCODE_UNSAFE_PATH
    if not _is_safe_relative_path(target_relative_path):
        return None, STOPCODE_UNSAFE_PATH
    if not _is_safe_filename(target_filename):
        return None, STOPCODE_UNSAFE_PATH
    if _normalize_slashes(target_relative_path).strip("/") != (
        f"{_normalize_slashes(target_dir).strip('/')}/{str(target_filename).strip()}"
    ):
        return None, STOPCODE_UNSAFE_PATH

    resolved_root = workspace_root.resolve()
    resolved_scan = (resolved_root / Path(*PurePosixPath(normalized_scan_dir).parts)).resolve()
    target_path = _target_path(workspace_root, normalized_scan_dir, str(target_relative_path))

    # Inspect the UNRESOLVED parent chain first.  resolve() would dereference a
    # junction/reparse point sitting exactly on the target parent (or any interior
    # component) and hide it from the containment check below.  The walk is bounded to
    # components strictly INSIDE the unresolved scan_dir root: the catalog-bound
    # scan_dir itself is the legitimate cloud mount and is explicitly not flagged here.
    unresolved_scan_root = workspace_root.joinpath(*PurePosixPath(normalized_scan_dir).parts)
    if _has_reparse_or_symlink(target_path.parent, unresolved_scan_root):
        return None, STOPCODE_UNSAFE_PATH

    resolved_target = target_path.resolve()

    for candidate_root in (resolved_scan, resolved_root):
        try:
            resolved_target.relative_to(candidate_root)
        except ValueError:
            return None, STOPCODE_UNSAFE_PATH

    if not resolved_target.parent.is_dir():
        return None, STOPCODE_PARENT_MISSING
    return resolved_target, None


def _finalize(output: dict[str, Any], status: str, reason: str) -> dict[str, Any]:
    """Return the deterministic output envelope with its canonical preflight hash."""
    final = dict(output)
    final["status"] = status
    final["reason"] = reason
    final["preflight_hash"] = canonical_json_sha256(
        {key: value for key, value in final.items() if key != "preflight_hash"}
    )
    return final


# ==============================================================================
# Public: read-only preflight
# ==============================================================================

def preflight_attachment_promotion(
    candidate: Mapping[str, Any],
    catalogs: Mapping[str, Any],
    *,
    receipt: Mapping[str, Any] | None,
    filemap: Mapping[str, Any] | None,
    workspace_root: Path | str | None = None,
    decision: Mapping[str, Any] | None = None,
    lease_id: str | None = None,
    conversation_id: str | None = None,
    data_dir: Path | None = None,
    current_time: datetime | None = None,
    max_filemap_age_seconds: int = DEFAULT_MAX_FILEMAP_AGE_SECONDS,
) -> dict[str, Any]:
    """Run the strictly read-only promotion preflight in the documented FR-09 order.

    Order: candidate structure/hash -> quarantine evidence (tracked-path guard plus
    real on-disk artefact) -> receipt against the recomputed review payload (including
    the current filemap snapshot) -> workspace-lock ownership -> current-catalog
    storage re-resolution -> target path checks -> canonical filemap validation and
    the real target state.  Returns a deterministic ``attachment_promotion_preflight``
    Schema 1; it performs zero mutations on every path.
    """
    workspace = resolve_workspace_root(workspace_root, data_dir=data_dir)
    now = current_time if isinstance(current_time, datetime) else datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        now = now.replace(tzinfo=timezone.utc)
    checked_at = (
        now.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )

    output: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": PREFLIGHT_KIND,
        "status": None,
        "reason": None,
        "candidate_hash": None,
        "review_hash": None,
        "source_sha256": None,
        "storage_id": None,
        "target_relative_path": None,
        "target_path_fingerprint": None,
        "filemap_snapshot_hash": None,
        "checked_at": checked_at,
    }

    try:
        # 1. Candidate structure and canonical hash.
        if not isinstance(candidate, Mapping):
            return _finalize(output, STOPCODE_CANDIDATE_DRIFT, "candidate_malformed")
        gate = _validate_candidate_gate(candidate)
        if gate is not None:
            return _finalize(output, gate, gate)

        stored_hash = str(candidate.get("candidate_hash") or "").strip().lower()
        recomputed_hash = compute_candidate_hash(candidate)
        if not _is_sha256_hex(stored_hash) or stored_hash != recomputed_hash:
            return _finalize(output, STOPCODE_CANDIDATE_DRIFT, "candidate_hash_drift")
        output["candidate_hash"] = recomputed_hash

        source = candidate["source"]
        destination = candidate["destination"]
        filemap_evidence = candidate["filemap_evidence"]
        output["source_sha256"] = str(source.get("sha256") or "").strip().lower()
        output["storage_id"] = str(destination.get("storage_id") or "").strip() or None
        output["target_relative_path"] = (
            str(destination.get("target_relative_path") or "").strip() or None
        )

        # 2. Quarantine evidence (tracked-path guard + real on-disk artefact/inventory).
        quarantine_stop = _verify_quarantine_evidence(candidate, workspace)
        if quarantine_stop is not None:
            return _finalize(output, quarantine_stop, quarantine_stop)

        # 3. Review payload (current filemap snapshot) and receipt verification.
        if not isinstance(filemap, Mapping):
            return _finalize(output, STOPCODE_FILEMAP_DRIFT, "filemap_missing")
        try:
            snapshot_hash = _filemap_snapshot_hash(filemap)
            review_hash = compute_promotion_review_hash(candidate, filemap)
        except Exception:
            return _finalize(output, STOPCODE_FILEMAP_DRIFT, "filemap_invalid")
        output["filemap_snapshot_hash"] = snapshot_hash
        output["review_hash"] = review_hash

        verification = verify_promotion_approval_receipt(
            receipt, expected_review_hash=review_hash, current_time=now
        )
        if not verification.ok:
            return _finalize(
                output,
                verification.stopcode or STOPCODE_APPROVAL_INVALID,
                verification.reason or STOPCODE_APPROVAL_INVALID,
            )

        # 4. Lock ownership from the trusted control plane only.
        try:
            verify_workspace_lock(
                workspace_root=workspace,
                lease_id=lease_id,
                conversation_id=conversation_id,
                data_dir=data_dir,
            )
        except Exception:
            return _finalize(output, STOPCODE_LOCK_UNAVAILABLE, STOPCODE_LOCK_UNAVAILABLE)

        # 5. Storage re-resolution from the current catalogs.
        scan_dir = _normalize_slashes(filemap_evidence.get("scan_dir"))
        storage_cfg, catalog_stop = _resolve_current_storage(candidate, catalogs, decision)
        if catalog_stop is not None:
            return _finalize(output, catalog_stop, catalog_stop)
        if storage_cfg is None:
            return _finalize(output, STOPCODE_PREFLIGHT_ERROR, STOPCODE_PREFLIGHT_ERROR)
        if _normalize_slashes(storage_cfg.get("scan_dir")) != scan_dir:
            return _finalize(output, STOPCODE_CATALOG_DRIFT, "catalog_scan_dir_drift")

        # 6. scan_dir / target path checks.
        target_path, path_stop = _validate_target_paths(workspace, destination, scan_dir)
        if path_stop is not None:
            return _finalize(output, path_stop, path_stop)
        if target_path is None:
            return _finalize(output, STOPCODE_PREFLIGHT_ERROR, STOPCODE_PREFLIGHT_ERROR)

        # 7. Canonical filemap validation, snapshot hash and the real target state.
        scope = str(filemap_evidence.get("scope") or "").strip().lower()
        project_id = str(filemap_evidence.get("project") or "").strip()
        is_valid, _updated_at, _err = validate_cloud_atlas_filemap(
            filemap,
            expected_storage_id=str(output["storage_id"] or ""),
            expected_scope=scope,
            expected_project_id=project_id,
            storage_cfg=storage_cfg,
            max_age_seconds=max_filemap_age_seconds,
            current_time=now,
            workspace_root=workspace,
        )
        if not is_valid:
            return _finalize(output, STOPCODE_FILEMAP_DRIFT, "filemap_drift")

        output["target_path_fingerprint"] = _target_path_fingerprint(
            scan_dir, str(output["target_relative_path"] or "")
        )

        if target_path.is_file():
            if _sha256_file(target_path) == output["source_sha256"]:
                return _finalize(output, STATUS_ALREADY_PRESENT, "target_already_present")
            return _finalize(output, STOPCODE_COLLISION_DETECTED, STOPCODE_COLLISION_DETECTED)
        if target_path.exists():
            return _finalize(output, STOPCODE_UNSAFE_PATH, "target_not_a_regular_file")
        return _finalize(output, STATUS_READY, "preconditions_satisfied")
    except Exception:
        return _finalize(output, STOPCODE_PREFLIGHT_ERROR, STOPCODE_PREFLIGHT_ERROR)


# ==============================================================================
# Writer: atomic, idempotent, no-clobber storage promotion (FR-09 / MD-P2)
# ==============================================================================
#
# This section is the only mutating part of the module.  MD-P1 (the three public
# functions above) is untouched.  The writer reuses -- never reimplements --
# ``compute_candidate_hash``, the MD-P1 review payload / receipt / preflight, the
# quarantine artefact verifier and the quarantine inventory lock.
#
# Trust boundaries (read this before relying on the writer):
#
# * The MD-P1 preflight envelope passed by the caller is *evidence*, not authority:
#   its own ``preflight_hash`` and every bound field are revalidated, and the full
#   MD-P1 preflight is re-executed from the current catalogs/filemap/receipt/source
#   immediately before the first write.  Any drift stops fail-closed.
# * The writer transfers exactly one candidate.  It never chooses a destination,
#   never creates directories, never converts content and never touches
#   ``filemap.json``, any catalog, the mailbox or Cloud-Atlas.
# * Target promotion is a single ``os.link`` no-clobber primitive.  An existing
#   target is never replaced and ``os.replace`` is never used as a fallback; an
#   unsupported filesystem stops fail-closed after removing only the writer's own
#   temp sibling.
# * The quarantine source is removed only after the target has been re-opened and
#   size/SHA-256 verified.  The run inventory is updated atomically while holding
#   the run's existing inventory lock; a cleanup failure leaves the promotion
#   successful as ``source_cleanup_pending``.
# * Every phase is journaled atomically under
#   ``data/mail-desk/attachment-promotions/<promotion_id>/promotion-journal.json``
#   with a hash chain binding the previous journal hash, the source/target hashes,
#   the relative paths, a timestamp and a bounded failure code.  The journal and the
#   real target are reconciled before any retry writes.  An
#   ``already_present_verified`` outcome is journaled with the same discipline
#   (``approved -> preflight_verified -> completed`` plus the ``already_present``
#   annotation), so every success carries a trust anchor for the MD-P3 handoff.

PROMOTION_RESULT_KIND = "attachment_promotion_result"
PROMOTION_JOURNAL_KIND = "attachment_promotion_journal"
PROMOTION_ID_KIND = "attachment_promotion_identity"
PROMOTION_DIR_NAME = "attachment-promotions"
JOURNAL_FILENAME = "promotion-journal.json"

STATUS_PROMOTION_COMPLETED = "promotion_completed"
STATUS_ALREADY_PRESENT_VERIFIED = "already_present_verified"
STATUS_SOURCE_CLEANUP_PENDING = "source_cleanup_pending"
STATUS_COLLISION_DETECTED = "collision_detected"
STATUS_RECOVERY_REQUIRED = "recovery_required"

#: The closed set of end states an ``attachment_promotion_result`` may carry.
PROMOTION_RESULT_STATUSES: frozenset[str] = frozenset(
    {
        STATUS_PROMOTION_COMPLETED,
        STATUS_ALREADY_PRESENT_VERIFIED,
        STATUS_SOURCE_CLEANUP_PENDING,
        STATUS_COLLISION_DETECTED,
        STATUS_RECOVERY_REQUIRED,
    }
)

PHASE_APPROVED = "approved"
PHASE_PREFLIGHT_VERIFIED = "preflight_verified"
PHASE_TEMP_WRITTEN = "temp_written"
PHASE_TARGET_PROMOTED = "target_promoted"
PHASE_TARGET_VERIFIED = "target_verified"
PHASE_SOURCE_CLEANUP_PENDING = "source_cleanup_pending"
PHASE_COMPLETED = "completed"
PHASE_FAILED = "failed"
PHASE_RECOVERY_REQUIRED = "recovery_required"

#: The ordered success phases (the ``failed``/``recovery_required`` annotations are
#: terminal and not part of the monotonic success chain).
_PHASE_SEQUENCE: tuple[str, ...] = (
    PHASE_APPROVED,
    PHASE_PREFLIGHT_VERIFIED,
    PHASE_TEMP_WRITTEN,
    PHASE_TARGET_PROMOTED,
    PHASE_TARGET_VERIFIED,
    PHASE_SOURCE_CLEANUP_PENDING,
    PHASE_COMPLETED,
)
_PHASE_RANK: dict[str, int] = {phase: index + 1 for index, phase in enumerate(_PHASE_SEQUENCE)}

#: The complete journal phase vocabulary, success phases plus terminal annotations.
JOURNAL_PHASES: tuple[str, ...] = _PHASE_SEQUENCE + (PHASE_FAILED, PHASE_RECOVERY_REQUIRED)

#: Allowed success-chain transitions.  A phase may only advance to its documented
#: successor (``target_verified`` may either record a cleanup failure or complete).
#: ``preflight_verified`` may also complete directly for an ``already_present``
#: verification, which journals no transfer phases but reaches ``completed`` with the
#: ``already_present`` annotation.
_ALLOWED_TRANSITIONS: dict[str | None, frozenset[str]] = {
    None: frozenset({PHASE_APPROVED}),
    PHASE_APPROVED: frozenset({PHASE_PREFLIGHT_VERIFIED}),
    PHASE_PREFLIGHT_VERIFIED: frozenset({PHASE_TEMP_WRITTEN, PHASE_COMPLETED}),
    PHASE_TEMP_WRITTEN: frozenset({PHASE_TARGET_PROMOTED}),
    PHASE_TARGET_PROMOTED: frozenset({PHASE_TARGET_VERIFIED}),
    PHASE_TARGET_VERIFIED: frozenset({PHASE_SOURCE_CLEANUP_PENDING, PHASE_COMPLETED}),
    PHASE_SOURCE_CLEANUP_PENDING: frozenset({PHASE_COMPLETED}),
    PHASE_COMPLETED: frozenset(),
}

CODE_PREFLIGHT_DRIFT = "preflight_drift"
CODE_PREFLIGHT_NOT_READY = "preflight_not_ready"
CODE_RECEIPT_DRIFT = "receipt_drift"
CODE_CANDIDATE_DRIFT = "candidate_drift"
CODE_JOURNAL_CORRUPTED = "journal_corrupted"
CODE_JOURNAL_UNKNOWN_PHASE = "journal_unknown_phase"
CODE_JOURNAL_SKIPPED_PHASE = "journal_skipped_phase"
CODE_JOURNAL_CONTRADICTORY = "journal_contradictory"
CODE_TEMP_WRITE_FAILED = "temp_write_failed"
CODE_TEMP_VERIFY_FAILED = "temp_verify_failed"
CODE_TARGET_COLLISION = "collision_detected"
CODE_TARGET_UNSUPPORTED_FS = "unsupported_filesystem"
CODE_TARGET_VERIFY_FAILED = "target_verify_failed"
CODE_SOURCE_DRIFT = "source_drift"
CODE_CLEANUP_FAILED = "cleanup_failed"
CODE_INTERNAL_ERROR = "internal_error"


class PromotionJournalError(ValueError):
    """Raised when a promotion journal is corrupted, swapped or contradictory."""

    def __init__(self, message: str, *, code: str = CODE_JOURNAL_CORRUPTED) -> None:
        super().__init__(message)
        self.code = code


class _PromotionFailure(RuntimeError):
    """Internal typed failure carrying a bounded code and the failing phase."""

    def __init__(self, code: str, phase: str) -> None:
        super().__init__(code)
        self.code = code
        self.phase = phase


def _inject(fault_hook: Callable[[str], None] | None, phase: str) -> None:
    """Invoke the injected fault hook (test seam); a raising hook propagates."""
    if fault_hook is not None:
        fault_hook(phase)


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _relative_posix(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def _canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def derive_promotion_id(review_hash: str, candidate_hash: str) -> str:
    """Return the deterministic 64-hex promotion id for a review/candidate pair."""
    review = str(review_hash or "").strip().lower()
    candidate = str(candidate_hash or "").strip().lower()
    if not _is_sha256_hex(review) or not _is_sha256_hex(candidate):
        raise ValueError("derive_promotion_id requires two 64-hex SHA-256 values.")
    return canonical_json_sha256(
        {
            "schema_version": SCHEMA_VERSION,
            "kind": PROMOTION_ID_KIND,
            "review_hash": review,
            "candidate_hash": candidate,
        }
    )


def _validate_journal(journal: Any) -> dict[str, Any]:
    """Validate a Schema-1 journal, its bindings and its hash-chained phases."""
    if not isinstance(journal, Mapping):
        raise PromotionJournalError("Promotion journal root must be an object.")
    if journal.get("schema_version") != SCHEMA_VERSION:
        raise PromotionJournalError("Unsupported promotion journal schema_version.")
    if journal.get("kind") != PROMOTION_JOURNAL_KIND:
        raise PromotionJournalError("Wrong promotion journal kind.")

    promotion_id = str(journal.get("promotion_id") or "").strip().lower()
    review_hash = str(journal.get("review_hash") or "").strip().lower()
    candidate_hash = str(journal.get("candidate_hash") or "").strip().lower()
    if not (_is_sha256_hex(promotion_id) and _is_sha256_hex(review_hash)
            and _is_sha256_hex(candidate_hash)):
        raise PromotionJournalError("Promotion journal identity hashes are malformed.")
    if promotion_id != derive_promotion_id(review_hash, candidate_hash):
        raise PromotionJournalError(
            "Promotion journal id does not match review_hash + candidate_hash.",
            code=CODE_JOURNAL_CONTRADICTORY,
        )

    declared_hash = str(journal.get("journal_hash") or "").strip().lower()
    if not _is_sha256_hex(declared_hash):
        raise PromotionJournalError("Promotion journal_hash is missing or malformed.")
    body = {key: value for key, value in journal.items() if key != "journal_hash"}
    if declared_hash != canonical_json_sha256(body):
        raise PromotionJournalError("Promotion journal_hash does not recompute.")

    phases = journal.get("phases")
    if not isinstance(phases, list) or not phases:
        raise PromotionJournalError("Promotion journal must contain at least one phase.")

    previous_hash: str | None = None
    current: str | None = None
    expect_next: str | None = None
    saw_terminal = False
    for entry in phases:
        if not isinstance(entry, Mapping):
            raise PromotionJournalError("Promotion journal phase entry must be an object.")
        record = dict(entry)
        entry_hash = record.pop("entry_hash", None)
        if not _is_sha256_hex(entry_hash):
            raise PromotionJournalError("Promotion journal phase entry_hash is malformed.")
        if canonical_json_sha256(record) != str(entry_hash).strip().lower():
            raise PromotionJournalError("Promotion journal phase entry_hash does not recompute.")
        if record.get("previous_hash") != previous_hash:
            raise PromotionJournalError("Promotion journal phase chain is broken.")
        phase = record.get("phase")
        if phase not in JOURNAL_PHASES:
            raise PromotionJournalError(
                f"Unknown promotion journal phase: {phase!r}.",
                code=CODE_JOURNAL_UNKNOWN_PHASE,
            )
        if saw_terminal:
            raise PromotionJournalError(
                "Promotion journal continues after a terminal phase.",
                code=CODE_JOURNAL_CONTRADICTORY,
            )
        if phase == PHASE_RECOVERY_REQUIRED:
            saw_terminal = True
        elif phase == PHASE_FAILED:
            failed_phase = record.get("failed_phase")
            if failed_phase not in _PHASE_RANK:
                raise PromotionJournalError(
                    f"Promotion journal failed entry names unknown phase {failed_phase!r}.",
                    code=CODE_JOURNAL_UNKNOWN_PHASE,
                )
            expect_next = failed_phase
        else:
            if expect_next is not None:
                if phase != expect_next:
                    raise PromotionJournalError(
                        "Promotion journal did not retry the failed phase.",
                        code=CODE_JOURNAL_CONTRADICTORY,
                    )
                expect_next = None
            else:
                allowed = _ALLOWED_TRANSITIONS.get(current, frozenset())
                if phase not in allowed:
                    current_rank = _PHASE_RANK.get(current, 0)
                    phase_rank = _PHASE_RANK.get(phase, 0)
                    if phase_rank <= current_rank:
                        raise PromotionJournalError(
                            f"Contradictory promotion phase transition {current!r} -> {phase!r}.",
                            code=CODE_JOURNAL_CONTRADICTORY,
                        )
                    raise PromotionJournalError(
                        f"Skipped promotion phase transition {current!r} -> {phase!r}.",
                        code=CODE_JOURNAL_SKIPPED_PHASE,
                    )
            current = phase
        previous_hash = str(entry_hash).strip().lower()

    last = phases[-1]
    if journal.get("phase") != last.get("phase"):
        raise PromotionJournalError(
            "Promotion journal top-level phase does not match the last entry.",
            code=CODE_JOURNAL_CONTRADICTORY,
        )
    if journal.get("failed_phase") != last.get("failed_phase"):
        raise PromotionJournalError(
            "Promotion journal failed_phase does not match the last entry.",
            code=CODE_JOURNAL_CONTRADICTORY,
        )
    if journal.get("error_code") != last.get("error_code"):
        raise PromotionJournalError(
            "Promotion journal error_code does not match the last entry.",
            code=CODE_JOURNAL_CONTRADICTORY,
        )
    expected_status = (
        "completed" if last.get("phase") == PHASE_COMPLETED
        else last.get("phase") if last.get("phase") in (PHASE_FAILED, PHASE_RECOVERY_REQUIRED)
        else "in_progress"
    )
    if journal.get("status") != expected_status:
        raise PromotionJournalError(
            "Promotion journal status is inconsistent with its phase.",
            code=CODE_JOURNAL_CONTRADICTORY,
        )
    return dict(journal)


def load_promotion_journal(
    journal_path: Path | str,
    *,
    expected_promotion_id: str | None = None,
    expected_candidate_hash: str | None = None,
    expected_review_hash: str | None = None,
    expected_source_sha256: str | None = None,
    expected_target_relative_path: str | None = None,
) -> dict[str, Any]:
    """Load and fully validate a promotion journal, fail-closed on any drift.

    Raises:
        PromotionJournalError: On unreadable/corrupted JSON, a broken hash chain, an
            unknown/skipped/contradictory phase history, or a binding mismatch
            against the supplied expectations.
    """
    path = Path(journal_path)
    try:
        raw = path.read_text(encoding="utf-8")
        journal = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise PromotionJournalError(f"Unreadable promotion journal '{path.name}': {exc}") from exc
    validated = _validate_journal(journal)

    def _mismatch(field: str, expected: str, actual: str) -> None:
        if str(expected).strip().lower() != str(actual).strip().lower():
            raise PromotionJournalError(
                f"Promotion journal binding drift on '{field}'.",
                code=CODE_JOURNAL_CONTRADICTORY,
            )

    if expected_promotion_id is not None:
        _mismatch("promotion_id", expected_promotion_id, validated["promotion_id"])
    if expected_candidate_hash is not None:
        _mismatch("candidate_hash", expected_candidate_hash, validated["candidate_hash"])
    if expected_review_hash is not None:
        _mismatch("review_hash", expected_review_hash, validated["review_hash"])
    if expected_source_sha256 is not None:
        _mismatch("source_sha256", expected_source_sha256, validated.get("source_sha256"))
    if expected_target_relative_path is not None:
        _mismatch("target_relative_path", expected_target_relative_path,
                  validated.get("target_relative_path"))
    return validated


def _write_journal_atomic(journal_path: Path, journal: dict[str, Any]) -> None:
    """Atomically and durably write the promotion journal (sibling temp + fsync)."""
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal["updated_at"] = utc_now_iso()
    journal["journal_hash"] = canonical_json_sha256(
        {key: value for key, value in journal.items() if key != "journal_hash"}
    )
    data = _canonical_json_bytes(journal)
    tmp_path = journal_path.parent / f".{journal_path.name}.{uuid.uuid4().hex}.tmp"
    try:
        descriptor = os.open(
            tmp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
        )
    except OSError:
        raise
    try:
        handle = os.fdopen(descriptor, "wb")
    except OSError:
        try:
            os.close(descriptor)
        except OSError:
            pass
        _unlink_quiet(tmp_path)
        raise
    try:
        with handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, journal_path)
    except OSError:
        _unlink_quiet(tmp_path)
        raise


def _bound_subtopic_id(decision: Mapping[str, Any] | None) -> str | None:
    """Derive the catalog-bound subtopic id from a filing decision (additive MD-P3).

    ``build_cloud_atlas_refresh_handoff`` needs the subtopic id to let the Cloud-Atlas
    consumer resolve a *subtopic-owned* storage, but the canonical MD-A5 candidate never
    carries it (its ``filemap_evidence`` records only scope/entity/storage/scan_dir).
    The subtopic exists solely in the decision that MD-A5 resolved through
    :func:`resolve_catalog_storage`, so the writer persists it into the journal -- the
    candidate's bound decision context -- and the handoff reads it from there.  This
    mirrors ``resolve_catalog_storage``'s subtopic resolution exactly:

    * ``kind: "topic"`` reads the ``subtopic`` scalar;
    * legacy ``kind: "subtopic"`` binds the entity id as the subtopic when a parent
      ``topic`` scalar is present, otherwise the ``subtopic`` scalar;
    * legacy ``kind: "event"`` reads the ``subtopic`` scalar.

    Returns ``None`` for project scope or when no non-empty subtopic is bound.
    """
    if not isinstance(decision, Mapping):
        return None
    raw_kind = str(decision.get("kind") or "").strip().lower()
    if raw_kind not in ("topic", "subtopic", "event"):
        return None
    subtopic = decision.get("subtopic")
    if raw_kind == "subtopic" and str(decision.get("topic") or "").strip():
        subtopic = decision.get("id")
    text = str(subtopic or "").strip()
    return text or None


def _new_journal(
    *,
    candidate_hash: str,
    review_hash: str,
    preflight_hash: str,
    source_sha256: str,
    source_size_bytes: int,
    source_relative_path: str,
    target_relative_path: str,
    storage_id: str,
    run_id: str,
    message_id: str,
    filename: str,
    subtopic_id: str | None = None,
) -> dict[str, Any]:
    now = utc_now_iso()
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": PROMOTION_JOURNAL_KIND,
        "promotion_id": derive_promotion_id(review_hash, candidate_hash),
        "candidate_hash": candidate_hash,
        "review_hash": review_hash,
        "preflight_hash": preflight_hash,
        "source_sha256": source_sha256,
        "target_sha256": source_sha256,
        "source_size_bytes": source_size_bytes,
        "source_relative_path": source_relative_path,
        "target_relative_path": target_relative_path,
        "storage_id": storage_id,
        "subtopic_id": subtopic_id,
        "run_id": run_id,
        "source_message_id": normalize_message_id(message_id),
        "source_filename": filename,
        "created_at": now,
        "updated_at": now,
        "status": "in_progress",
        "phase": None,
        "failed_phase": None,
        "error_code": None,
        "phases": [],
        "journal_hash": None,
    }


def _phase_entry(journal: dict[str, Any], phase: str, *, failed_phase: str | None,
                 error_code: str | None) -> dict[str, Any]:
    phases = journal["phases"]
    entry: dict[str, Any] = {
        "phase": phase,
        "sequence": len(phases) + 1,
        "previous_hash": phases[-1]["entry_hash"] if phases else None,
        "source_sha256": journal["source_sha256"],
        "target_sha256": journal["target_sha256"],
        "source_relative_path": journal["source_relative_path"],
        "target_relative_path": journal["target_relative_path"],
        "timestamp": utc_now_iso(),
        "error_code": error_code,
        "failed_phase": failed_phase,
    }
    entry["entry_hash"] = canonical_json_sha256(entry)
    return entry


def _append_phase(journal_path: Path, journal: dict[str, Any], phase: str,
                  fault_hook: Callable[[str], None] | None) -> dict[str, Any]:
    journal["phases"].append(_phase_entry(journal, phase, failed_phase=None, error_code=None))
    journal["phase"] = phase
    journal["failed_phase"] = None
    journal["error_code"] = None
    journal["status"] = "completed" if phase == PHASE_COMPLETED else "in_progress"
    _write_journal_atomic(journal_path, journal)
    _inject(fault_hook, phase)
    return journal


def _record_failure(journal_path: Path, journal: dict[str, Any], failed_phase: str,
                    error_code: str, fault_hook: Callable[[str], None] | None) -> dict[str, Any]:
    journal["phases"].append(
        _phase_entry(journal, PHASE_FAILED, failed_phase=failed_phase, error_code=error_code)
    )
    journal["phase"] = PHASE_FAILED
    journal["failed_phase"] = failed_phase
    journal["error_code"] = error_code
    journal["status"] = "failed"
    _write_journal_atomic(journal_path, journal)
    _inject(fault_hook, PHASE_FAILED)
    return journal


def _target_state(target_path: Path, expected_sha256: str) -> str:
    """Classify the real target as ``missing``, ``same`` or ``different`` (fail-closed)."""
    if not target_path.exists():
        return "missing"
    if target_path.is_file():
        try:
            if _sha256_file(target_path) == expected_sha256:
                return "same"
        except OSError:
            return "different"
        return "different"
    return "different"


def _classified_target(target_path: Path, expected_sha256: str, expected_size: int) -> str:
    """Classify the real target by size AND SHA-256 (``missing``/``same``/``different``).

    The hash-chained journal proves the recorded claim, not the current disk state, so
    every resumed/completed path re-opens the real target through this predicate before
    any irreversible quarantine cleanup.
    """
    if not target_path.exists():
        return "missing"
    if not target_path.is_file():
        return "different"
    try:
        if target_path.stat().st_size != expected_size:
            return "different"
        if _sha256_file(target_path) != expected_sha256:
            return "different"
    except OSError:
        return "different"
    return "same"


def _clean_journal_temp(journal_path: Path) -> None:
    """Remove only this promotion's own stale journal temp siblings (crash hygiene)."""
    prefix = f".{journal_path.name}."
    try:
        stale_temps = list(journal_path.parent.glob(f"{prefix}*.tmp"))
    except OSError:
        return
    for stale in stale_temps:
        _unlink_quiet(stale)


def _quarantine_attachments_root(workspace_root: Path) -> Path:
    """Return the quarantine attachments root exactly as the MD-P1 verifier pins it.

    ``verify_quarantine_attachment_artifact`` always resolves
    ``<workspace_root>/data/mail-desk/attachments`` and ignores ``data_dir``; the writer
    must rewrite the very inventory that was verified, so it derives the same root.
    """
    return workspace_root / "data" / "mail-desk" / "attachments"


def _clean_own_temp(target_path: Path, promotion_id: str) -> None:
    """Remove only the writer's own temp siblings for this promotion id."""
    prefix = f".{target_path.name}.{promotion_id}."
    for stale in target_path.parent.glob(f"{prefix}*.tmp"):
        _unlink_quiet(stale)


def _ensure_temp(target_path: Path, promotion_id: str, source_path: Path,
                 source_sha256: str, expected_size: int) -> Path:
    """Exclusively create, durably write and re-verify a sibling temp file."""
    _clean_own_temp(target_path, promotion_id)
    try:
        source_bytes = source_path.read_bytes()
    except OSError as exc:
        raise _PromotionFailure(CODE_SOURCE_DRIFT, PHASE_TEMP_WRITTEN) from exc
    if (hashlib.sha256(source_bytes).hexdigest() != source_sha256
            or len(source_bytes) != expected_size):
        raise _PromotionFailure(CODE_SOURCE_DRIFT, PHASE_TEMP_WRITTEN)

    temp_path = target_path.parent / f".{target_path.name}.{promotion_id}.{uuid.uuid4().hex}.tmp"
    try:
        descriptor = os.open(
            temp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
        )
    except OSError as exc:
        raise _PromotionFailure(CODE_TEMP_WRITE_FAILED, PHASE_TEMP_WRITTEN) from exc
    try:
        handle = os.fdopen(descriptor, "wb")
    except OSError as exc:
        try:
            os.close(descriptor)
        except OSError:
            pass
        _unlink_quiet(temp_path)
        raise _PromotionFailure(CODE_TEMP_WRITE_FAILED, PHASE_TEMP_WRITTEN) from exc
    try:
        with handle:
            handle.write(source_bytes)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        _unlink_quiet(temp_path)
        raise _PromotionFailure(CODE_TEMP_WRITE_FAILED, PHASE_TEMP_WRITTEN) from exc

    try:
        written = temp_path.read_bytes()
    except OSError as exc:
        _unlink_quiet(temp_path)
        raise _PromotionFailure(CODE_TEMP_VERIFY_FAILED, PHASE_TEMP_WRITTEN) from exc
    if (hashlib.sha256(written).hexdigest() != source_sha256
            or len(written) != expected_size):
        _unlink_quiet(temp_path)
        raise _PromotionFailure(CODE_TEMP_VERIFY_FAILED, PHASE_TEMP_WRITTEN)
    return temp_path


def _flush_parent_dir(directory: Path) -> None:
    """Best-effort parent-directory fsync on platforms that support it."""
    if os.name == "nt":
        return
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


def _write_inventory_atomic(inv_file: Path, data: bytes) -> None:
    """Atomically replace the quarantine inventory under its caller-held lock."""
    tmp_path = inv_file.parent / f".{INVENTORY_FILENAME}.{uuid.uuid4().hex}.tmp"
    try:
        descriptor = os.open(
            tmp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
        )
    except OSError:
        raise
    try:
        handle = os.fdopen(descriptor, "wb")
    except OSError:
        try:
            os.close(descriptor)
        except OSError:
            pass
        _unlink_quiet(tmp_path)
        raise
    try:
        with handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, inv_file)
    except OSError:
        _unlink_quiet(tmp_path)
        raise


def _remove_inventory_entry(run_dir: Path, message_id: str, filename: str) -> None:
    """Remove exactly one file entry and rewrite the inventory atomically."""
    try:
        inv = _load_quarantine_inventory(run_dir)
    except QuarantineInventoryError as exc:
        raise _PromotionFailure(CODE_CLEANUP_FAILED, PHASE_SOURCE_CLEANUP_PENDING) from exc
    messages = inv.get("messages")
    if not isinstance(messages, dict):
        messages = {}
        inv["messages"] = messages
    norm = normalize_message_id(message_id) or "__default__"
    key: str | None = norm if norm in messages else None
    if key is None:
        for candidate_key in list(messages):
            if normalize_message_id(candidate_key) == norm:
                key = candidate_key
                break
    if key is not None and isinstance(messages.get(key), dict):
        entry = messages[key]
        files = entry.get("files")
        if isinstance(files, dict):
            files.pop(filename, None)
            entry["count"] = len(files)
            entry["total_bytes"] = sum(
                int(meta.get("size_bytes", 0))
                for meta in files.values()
                if isinstance(meta, Mapping)
            )
            if entry["count"] == 0:
                messages.pop(key, None)
    payload = json.dumps(inv, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8")
    _write_inventory_atomic(run_dir / INVENTORY_FILENAME, payload)


def _cleanup_quarantine_source(source_path: Path, run_dir: Path, message_id: str,
                               filename: str) -> None:
    """Remove the verified quarantine source and update its inventory under lock."""
    with _QuarantineInventoryLock(run_dir):
        if source_path.exists():
            source_path.unlink()
        _remove_inventory_entry(run_dir, message_id, filename)


def _build_result(
    *,
    status: str,
    reason: str,
    error_code: str | None,
    phase: str | None,
    promotion_id: str | None,
    candidate_hash: str | None = None,
    review_hash: str | None = None,
    preflight_hash: str | None = None,
    storage_id: str | None = None,
    target_relative_path: str | None = None,
    target_sha256: str | None = None,
    target_size_bytes: int | None = None,
    journal_relative_path: str | None = None,
    journal_hash: str | None = None,
) -> dict[str, Any]:
    """Return a deterministic ``attachment_promotion_result`` Schema 1."""
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": PROMOTION_RESULT_KIND,
        "status": status,
        "reason": reason,
        "error_code": error_code,
        "phase": phase,
        "promotion_id": promotion_id,
        "candidate_hash": candidate_hash,
        "review_hash": review_hash,
        "preflight_hash": preflight_hash,
        "storage_id": storage_id,
        "target_relative_path": target_relative_path,
        "target_sha256": target_sha256,
        "target_size_bytes": target_size_bytes,
        "journal_relative_path": journal_relative_path,
        "journal_hash": journal_hash,
    }
    payload["result_hash"] = canonical_json_sha256(payload)
    return payload


def _result_from_journal(ws: Path, journal_path: Path, journal: Mapping[str, Any], *,
                         status: str, reason: str,
                         error_code: str | None = None) -> dict[str, Any]:
    return _build_result(
        status=status,
        reason=reason,
        error_code=error_code,
        phase=journal.get("phase"),
        promotion_id=journal.get("promotion_id"),
        candidate_hash=journal.get("candidate_hash"),
        review_hash=journal.get("review_hash"),
        preflight_hash=journal.get("preflight_hash"),
        storage_id=journal.get("storage_id"),
        target_relative_path=journal.get("target_relative_path"),
        target_sha256=journal.get("target_sha256"),
        target_size_bytes=journal.get("source_size_bytes"),
        journal_relative_path=_relative_posix(ws, journal_path),
        journal_hash=journal.get("journal_hash"),
    )


def _reverify_failure_result(
    ws: Path,
    journal_path: Path,
    journal: Mapping[str, Any],
    *,
    completed_state: str,
    promotion_id: str | None,
    candidate_hash: str | None,
    review_hash: str | None,
    storage_id: str | None,
    target_relative_path: str | None,
    source_sha256: str | None,
    expected_size: int | None,
) -> dict[str, Any]:
    """Map a failed real-target re-verification to a bounded, non-destructive result.

    A different-hash target is a collision (never overwritten or cleaned); a missing or
    otherwise unverifiable target is ``recovery_required``.  In both cases the quarantine
    source is left untouched and ``promotion_completed`` is never reported.
    """
    status = (
        STATUS_COLLISION_DETECTED if completed_state == "different"
        else STATUS_RECOVERY_REQUIRED
    )
    reason = CODE_TARGET_COLLISION if completed_state == "different" else CODE_TARGET_VERIFY_FAILED
    return _build_result(
        status=status,
        reason=reason,
        error_code=reason,
        phase=journal.get("phase"),
        promotion_id=promotion_id,
        candidate_hash=candidate_hash,
        review_hash=review_hash,
        preflight_hash=journal.get("preflight_hash"),
        storage_id=storage_id,
        target_relative_path=target_relative_path,
        target_sha256=source_sha256,
        target_size_bytes=expected_size,
        journal_relative_path=_relative_posix(ws, journal_path),
        journal_hash=journal.get("journal_hash"),
    )


def promote_attachment(
    candidate: Mapping[str, Any],
    catalogs: Mapping[str, Any],
    preflight: Mapping[str, Any],
    *,
    receipt: Mapping[str, Any] | None,
    filemap: Mapping[str, Any] | None,
    workspace_root: Path | str | None = None,
    decision: Mapping[str, Any] | None = None,
    lease_id: str | None = None,
    conversation_id: str | None = None,
    data_dir: Path | None = None,
    current_time: datetime | None = None,
    max_filemap_age_seconds: int = DEFAULT_MAX_FILEMAP_AGE_SECONDS,
    _fault_hook: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Atomically and idempotently promote exactly one approved attachment (MD-P2).

    The MD-P1 preflight envelope is revalidated as evidence and the full MD-P1
    preflight is re-executed immediately before the first write.  The writer never
    clobbers a target, never uses ``os.replace`` for the target and never touches
    filemap, catalogs, the mailbox or Cloud-Atlas.  ``_fault_hook`` is a test-only
    seam invoked after each durably journaled phase; a raising hook simulates a
    crash and propagates unchanged.
    """
    now = current_time if isinstance(current_time, datetime) else datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        now = now.replace(tzinfo=timezone.utc)
    ws = resolve_workspace_root(workspace_root, data_dir=data_dir)
    base_data = Path(data_dir) if data_dir is not None else (ws / "data" / "mail-desk")

    def recovery(code: str, *, promotion_id: str | None = None, phase: str | None = None,
                 candidate_hash: str | None = None, review_hash: str | None = None,
                 preflight_hash: str | None = None) -> dict[str, Any]:
        return _build_result(
            status=STATUS_RECOVERY_REQUIRED, reason=code, error_code=code, phase=phase,
            promotion_id=promotion_id, candidate_hash=candidate_hash,
            review_hash=review_hash, preflight_hash=preflight_hash,
        )

    # -- 1. Structural validation of candidate + preflight envelope (evidence) --
    if not isinstance(candidate, Mapping):
        return recovery(CODE_CANDIDATE_DRIFT)
    stored_hash = str(candidate.get("candidate_hash") or "").strip().lower()
    candidate_hash = compute_candidate_hash(candidate)
    if not _is_sha256_hex(stored_hash) or stored_hash != candidate_hash:
        return recovery(CODE_CANDIDATE_DRIFT)

    if not isinstance(preflight, Mapping):
        return recovery(CODE_PREFLIGHT_DRIFT)
    declared_hash = str(preflight.get("preflight_hash") or "").strip().lower()
    if not _is_sha256_hex(declared_hash):
        return recovery(CODE_PREFLIGHT_DRIFT)
    envelope_body = {key: value for key, value in preflight.items() if key != "preflight_hash"}
    if declared_hash != canonical_json_sha256(envelope_body):
        return recovery(CODE_PREFLIGHT_DRIFT)
    if str(preflight.get("status") or "") not in (
        STATUS_READY, STATUS_ALREADY_PRESENT, STOPCODE_COLLISION_DETECTED
    ):
        return recovery(CODE_PREFLIGHT_NOT_READY)
    if str(preflight.get("candidate_hash") or "").strip().lower() != candidate_hash:
        return recovery(CODE_PREFLIGHT_DRIFT)

    source = candidate.get("source")
    destination = candidate.get("destination")
    filemap_evidence = candidate.get("filemap_evidence")
    if not all(isinstance(part, Mapping) for part in (source, destination, filemap_evidence)):
        return recovery(CODE_CANDIDATE_DRIFT)
    source_evidence = source.get("quarantine_evidence")
    if not isinstance(source_evidence, Mapping):
        return recovery(CODE_CANDIDATE_DRIFT)

    source_sha = str(source.get("sha256") or "").strip().lower()
    source_rel = _normalize_slashes(source.get("quarantine_path"))
    expected_size = source_evidence.get("size_bytes")
    run_id = str(source_evidence.get("run_id") or "").strip()
    message_id = str(source.get("message_id") or "").strip()
    filename = PurePosixPath(source_rel).name
    storage_id = str(destination.get("storage_id") or "").strip()
    target_rel = _normalize_slashes(destination.get("target_relative_path"))
    scan_dir = _normalize_slashes(filemap_evidence.get("scan_dir"))
    review_hash = str(preflight.get("review_hash") or "").strip().lower()

    if (not _is_sha256_hex(source_sha) or not _is_sha256_hex(review_hash)
            or not isinstance(expected_size, int) or isinstance(expected_size, bool)
            or expected_size <= 0 or not is_valid_run_id(run_id) or not filename
            or not storage_id):
        return recovery(CODE_CANDIDATE_DRIFT)

    target_path, path_stop = _validate_target_paths(ws, destination, scan_dir)
    if path_stop is not None or target_path is None:
        return recovery(path_stop or CODE_INTERNAL_ERROR, candidate_hash=candidate_hash,
                        review_hash=review_hash)
    source_path = ws / PurePosixPath(source_rel)

    # -- 2. Derive journal identity and reconcile any existing journal --
    promotion_id = derive_promotion_id(review_hash, candidate_hash)
    # The candidate does not carry the subtopic; the decision bound to it does.  Persist
    # it into the journal so the MD-P3 handoff can reach subtopic-owned storages even
    # though build_cloud_atlas_refresh_handoff only receives the candidate.
    bound_subtopic = _bound_subtopic_id(decision)
    journal_path = base_data / PROMOTION_DIR_NAME / promotion_id / JOURNAL_FILENAME
    # Crash hygiene: remove this promotion's own orphaned journal temp siblings from
    # an earlier interrupted _write_journal_atomic before starting or resuming.
    _clean_journal_temp(journal_path)
    journal: dict[str, Any] | None = None
    resume_rank = 0
    preflight_hash = declared_hash
    if journal_path.exists():
        try:
            journal = load_promotion_journal(
                journal_path,
                expected_promotion_id=promotion_id,
                expected_candidate_hash=candidate_hash,
                expected_review_hash=review_hash,
                expected_source_sha256=source_sha,
                expected_target_relative_path=target_rel,
            )
        except PromotionJournalError as exc:
            return recovery(exc.code or CODE_JOURNAL_CORRUPTED, promotion_id=promotion_id,
                            candidate_hash=candidate_hash, review_hash=review_hash)
        if journal.get("status") == "completed":
            # The journal proves the recorded completion, not the current disk state:
            # re-open the real target before reporting promotion_completed.
            completed_state = _classified_target(target_path, source_sha, expected_size)
            if completed_state != "same":
                return _reverify_failure_result(
                    ws, journal_path, journal,
                    completed_state=completed_state, promotion_id=promotion_id,
                    candidate_hash=candidate_hash, review_hash=review_hash,
                    storage_id=storage_id, target_relative_path=target_rel,
                    source_sha256=source_sha, expected_size=expected_size,
                )
            if journal.get("already_present") is True:
                return _result_from_journal(ws, journal_path, journal,
                                            status=STATUS_ALREADY_PRESENT_VERIFIED,
                                            reason="target_already_present")
            return _result_from_journal(ws, journal_path, journal,
                                        status=STATUS_PROMOTION_COMPLETED,
                                        reason="promotion_already_completed")
        last_phase = journal.get("phase")
        if last_phase == PHASE_RECOVERY_REQUIRED:
            return recovery(str(journal.get("error_code") or CODE_JOURNAL_CORRUPTED),
                            promotion_id=promotion_id, phase=last_phase,
                            candidate_hash=candidate_hash, review_hash=review_hash)
        if last_phase == PHASE_FAILED:
            failed_phase = str(journal.get("failed_phase") or "")
            if failed_phase not in _PHASE_RANK:
                return recovery(CODE_JOURNAL_CORRUPTED, promotion_id=promotion_id)
            resume_rank = _PHASE_RANK[failed_phase] - 1
        else:
            resume_rank = _PHASE_RANK[last_phase]
        preflight_hash = str(journal.get("preflight_hash") or declared_hash)

    # -- 3. Re-execute the full MD-P1 preflight immediately before the first write --
    if resume_rank <= _PHASE_RANK[PHASE_TARGET_PROMOTED]:
        try:
            fresh = preflight_attachment_promotion(
                candidate, catalogs, receipt=receipt, filemap=filemap,
                workspace_root=ws, decision=decision, lease_id=lease_id,
                conversation_id=conversation_id, data_dir=data_dir, current_time=now,
                max_filemap_age_seconds=max_filemap_age_seconds,
            )
        except Exception:
            return recovery(CODE_PREFLIGHT_DRIFT, promotion_id=promotion_id,
                            candidate_hash=candidate_hash, review_hash=review_hash)
        fresh_status = fresh.get("status")
        if fresh_status == STOPCODE_COLLISION_DETECTED:
            return _build_result(
                status=STATUS_COLLISION_DETECTED, reason=CODE_TARGET_COLLISION,
                error_code=CODE_TARGET_COLLISION, phase=None, promotion_id=promotion_id,
                candidate_hash=candidate_hash, review_hash=review_hash,
                preflight_hash=str(fresh.get("preflight_hash") or declared_hash),
                storage_id=storage_id, target_relative_path=target_rel,
                target_sha256=source_sha, target_size_bytes=expected_size,
            )
        if fresh_status not in (STATUS_READY, STATUS_ALREADY_PRESENT):
            return recovery(str(fresh_status or CODE_PREFLIGHT_NOT_READY),
                            promotion_id=promotion_id, candidate_hash=candidate_hash,
                            review_hash=review_hash)
        for key, expected in (
            ("candidate_hash", candidate_hash),
            ("review_hash", review_hash),
            ("source_sha256", source_sha),
            ("target_relative_path", target_rel),
            ("storage_id", storage_id),
        ):
            if str(fresh.get(key) or "").strip().lower() != str(expected).strip().lower():
                return recovery(CODE_PREFLIGHT_DRIFT, promotion_id=promotion_id,
                                candidate_hash=candidate_hash, review_hash=review_hash)
        if (fresh_status == STATUS_ALREADY_PRESENT
                and (journal is None or journal.get("phase") != PHASE_FAILED)
                and resume_rank <= _PHASE_RANK[PHASE_PREFLIGHT_VERIFIED]):
            # An already-present verification is journaled like any other success so
            # the Cloud-Atlas handoff always has a hash-chained trust anchor.  The
            # phases are approved -> preflight_verified -> completed with the
            # already_present annotation; the completed retry path maps that
            # annotation back to already_present_verified.  The quarantine source is
            # never removed (the target already holds the verified bytes).  This path
            # is only reachable when the loaded journal's last entry is NOT failed: a
            # terminal failed journal must first retry the failed phase (appending
            # completed directly would make the journal permanently contradictory).
            preflight_hash = str(fresh.get("preflight_hash") or declared_hash)
            if journal is None:
                journal = _new_journal(
                    candidate_hash=candidate_hash, review_hash=review_hash,
                    preflight_hash=preflight_hash, source_sha256=source_sha,
                    source_size_bytes=expected_size, source_relative_path=source_rel,
                    target_relative_path=target_rel, storage_id=storage_id, run_id=run_id,
                    message_id=message_id, filename=filename, subtopic_id=bound_subtopic,
                )
                try:
                    journal["already_present"] = True
                    journal = _append_phase(journal_path, journal, PHASE_APPROVED, _fault_hook)
                except OSError:
                    return recovery(CODE_TEMP_WRITE_FAILED, promotion_id=promotion_id,
                                    candidate_hash=candidate_hash, review_hash=review_hash)
            else:
                journal["already_present"] = True
            if resume_rank < _PHASE_RANK[PHASE_PREFLIGHT_VERIFIED]:
                journal = _append_phase(
                    journal_path, journal, PHASE_PREFLIGHT_VERIFIED, _fault_hook
                )
            journal = _append_phase(journal_path, journal, PHASE_COMPLETED, _fault_hook)
            return _result_from_journal(
                ws, journal_path, journal,
                status=STATUS_ALREADY_PRESENT_VERIFIED, reason="target_already_present",
            )
        if fresh_status == STATUS_READY and resume_rank == 0:
            preflight_hash = str(fresh.get("preflight_hash") or declared_hash)

    # -- 4. Start or resume the journaled transfer --
    if journal is None:
        journal = _new_journal(
            candidate_hash=candidate_hash, review_hash=review_hash,
            preflight_hash=preflight_hash, source_sha256=source_sha,
            source_size_bytes=expected_size, source_relative_path=source_rel,
            target_relative_path=target_rel, storage_id=storage_id, run_id=run_id,
            message_id=message_id, filename=filename, subtopic_id=bound_subtopic,
        )
        try:
            journal = _append_phase(journal_path, journal, PHASE_APPROVED, _fault_hook)
        except OSError:
            return recovery(CODE_TEMP_WRITE_FAILED, promotion_id=promotion_id,
                            candidate_hash=candidate_hash, review_hash=review_hash)
        resume_rank = _PHASE_RANK[PHASE_APPROVED]

    if resume_rank < _PHASE_RANK[PHASE_PREFLIGHT_VERIFIED]:
        journal = _append_phase(journal_path, journal, PHASE_PREFLIGHT_VERIFIED, _fault_hook)

    if resume_rank < _PHASE_RANK[PHASE_TEMP_WRITTEN]:
        try:
            _ensure_temp(target_path, promotion_id, source_path, source_sha, expected_size)
        except _PromotionFailure as exc:
            journal = _record_failure(journal_path, journal, exc.phase, exc.code, _fault_hook)
            return _result_from_journal(ws, journal_path, journal,
                                        status=STATUS_RECOVERY_REQUIRED, reason=exc.code,
                                        error_code=exc.code)
        journal = _append_phase(journal_path, journal, PHASE_TEMP_WRITTEN, _fault_hook)

    if resume_rank < _PHASE_RANK[PHASE_TARGET_PROMOTED]:
        state = _target_state(target_path, source_sha)
        if state == "same":
            # A crash between a successful link and the target_promoted append leaves
            # the journal at temp_written.  The target is already the verified bytes:
            # reconcile forward (target_promoted -> verify -> cleanup -> completed)
            # instead of returning early and leaving the journal in_progress forever.
            _clean_own_temp(target_path, promotion_id)
        elif state == "different":
            _clean_own_temp(target_path, promotion_id)
            return _build_result(
                status=STATUS_COLLISION_DETECTED, reason=CODE_TARGET_COLLISION,
                error_code=CODE_TARGET_COLLISION, phase=journal.get("phase"),
                promotion_id=promotion_id, candidate_hash=candidate_hash,
                review_hash=review_hash, preflight_hash=preflight_hash,
                storage_id=storage_id, target_relative_path=target_rel,
                target_sha256=source_sha, target_size_bytes=expected_size,
                journal_relative_path=_relative_posix(ws, journal_path),
                journal_hash=journal.get("journal_hash"),
            )
        else:
            try:
                temp_path = _ensure_temp(target_path, promotion_id, source_path, source_sha,
                                         expected_size)
            except _PromotionFailure as exc:
                journal = _record_failure(journal_path, journal, exc.phase, exc.code,
                                          _fault_hook)
                return _result_from_journal(ws, journal_path, journal,
                                            status=STATUS_RECOVERY_REQUIRED, reason=exc.code,
                                            error_code=exc.code)
            try:
                os.link(temp_path, target_path)
            except FileExistsError:
                _clean_own_temp(target_path, promotion_id)
                raced = _target_state(target_path, source_sha)
                if raced == "different":
                    return _build_result(
                        status=STATUS_COLLISION_DETECTED, reason=CODE_TARGET_COLLISION,
                        error_code=CODE_TARGET_COLLISION, phase=journal.get("phase"),
                        promotion_id=promotion_id, candidate_hash=candidate_hash,
                        review_hash=review_hash, preflight_hash=preflight_hash,
                        storage_id=storage_id, target_relative_path=target_rel,
                        target_sha256=source_sha, target_size_bytes=expected_size,
                        journal_relative_path=_relative_posix(ws, journal_path),
                        journal_hash=journal.get("journal_hash"),
                    )
                if raced != "same":
                    journal = _record_failure(journal_path, journal, PHASE_TARGET_PROMOTED,
                                              CODE_TARGET_VERIFY_FAILED, _fault_hook)
                    return _result_from_journal(ws, journal_path, journal,
                                                status=STATUS_RECOVERY_REQUIRED,
                                                reason=CODE_TARGET_VERIFY_FAILED,
                                                error_code=CODE_TARGET_VERIFY_FAILED)
                # raced == "same": the racer wrote identical bytes; reconcile forward
                # rather than reporting a second, journal-desynchronising success.
            except OSError:
                _clean_own_temp(target_path, promotion_id)
                journal = _record_failure(journal_path, journal, PHASE_TARGET_PROMOTED,
                                          CODE_TARGET_UNSUPPORTED_FS, _fault_hook)
                return _result_from_journal(ws, journal_path, journal,
                                            status=STATUS_RECOVERY_REQUIRED,
                                            reason=CODE_TARGET_UNSUPPORTED_FS,
                                            error_code=CODE_TARGET_UNSUPPORTED_FS)
            _unlink_quiet(temp_path)
        journal = _append_phase(journal_path, journal, PHASE_TARGET_PROMOTED, _fault_hook)

    if resume_rank < _PHASE_RANK[PHASE_TARGET_VERIFIED]:
        verified = target_path.is_file()
        if verified:
            try:
                verified = (
                    _sha256_file(target_path) == source_sha
                    and target_path.stat().st_size == expected_size
                )
            except OSError:
                verified = False
        if not verified:
            journal = _record_failure(journal_path, journal, PHASE_TARGET_VERIFIED,
                                      CODE_TARGET_VERIFY_FAILED, _fault_hook)
            return _result_from_journal(ws, journal_path, journal,
                                        status=STATUS_RECOVERY_REQUIRED,
                                        reason=CODE_TARGET_VERIFY_FAILED,
                                        error_code=CODE_TARGET_VERIFY_FAILED)
        _flush_parent_dir(target_path.parent)
        journal = _append_phase(journal_path, journal, PHASE_TARGET_VERIFIED, _fault_hook)

    # The hash-chained journal proves the recorded claim, not the current disk state:
    # re-open the real target and re-verify size + SHA-256 against the journal-bound
    # source identity before any irreversible quarantine cleanup -- and before any
    # promotion_completed from a resumed/completed journal path.  A missing target is
    # recovery_required; a different-hash target is a collision; the source is kept.
    guard_state = _classified_target(target_path, source_sha, expected_size)
    if guard_state != "same":
        return _reverify_failure_result(
            ws, journal_path, journal, completed_state=guard_state,
            promotion_id=promotion_id, candidate_hash=candidate_hash,
            review_hash=review_hash, storage_id=storage_id,
            target_relative_path=target_rel, source_sha256=source_sha,
            expected_size=expected_size,
        )

    if resume_rank < _PHASE_RANK[PHASE_COMPLETED]:
        try:
            _cleanup_quarantine_source(source_path,
                                       _quarantine_attachments_root(ws) / run_id,
                                       message_id, filename)
        except Exception:
            journal = _append_phase(journal_path, journal, PHASE_SOURCE_CLEANUP_PENDING,
                                    _fault_hook)
            return _result_from_journal(ws, journal_path, journal,
                                        status=STATUS_SOURCE_CLEANUP_PENDING,
                                        reason=CODE_CLEANUP_FAILED,
                                        error_code=CODE_CLEANUP_FAILED)
        journal = _append_phase(journal_path, journal, PHASE_COMPLETED, _fault_hook)

    return _result_from_journal(ws, journal_path, journal,
                                status=STATUS_PROMOTION_COMPLETED,
                                reason="promotion_completed")


# ==============================================================================
# Handoff: Cloud-Atlas refresh handoff + outcome coupling (FR-09 / MD-P3)
# ==============================================================================
#
# This final, clearly separated section is the only MD-P3 surface.  It neither
# changes nor reuses any MD-P1/MD-P2 behaviour: it consumes an already verified
# ``attachment_promotion_result`` and produces the declarative
# ``cloud_atlas_refresh_handoff`` Schema 1 for Cloud-Atlas, plus the combined
# outcome that couples the verified promotion to the receiving refresh.
#
# Trust boundaries (read this before relying on the handoff):
#
# * The handoff may only be derived from a canonically revalidated MD-P2 result
#   whose status is ``promotion_completed`` or ``already_present_verified``.  Both
#   statuses require an intact promotion journal (the trust anchor); a journal-less
#   result is rejected fail-closed.  The result hash, the promotion journal
#   (``load_promotion_journal``), the recorded ``preflight_hash``, the recomputed
#   candidate hash, the recomputed review payload (which binds the previous filemap
#   snapshot) and every storage/path/size binding must all agree; any drift raises
#   ``PromotionHandoffError`` fail-closed.
# * The handoff is pure bounded metadata: no absolute paths, no mail/attachment
#   text, no descriptions and no receiving instructions are copied.  Cloud-Atlas
#   receives an exact storage/path/hash binding, never an authorization.
# * ``compose_promotion_outcome`` never mutates and never re-runs MD-P2.  A missing
#   adapter, refresh error, timeout or verify error leaves the verified promotion
#   untouched (``promotion_completed_refresh_pending``); only a verified refresh
#   yields ``promotion_completed``/``refresh_completed``; journal/target drift stops
#   as ``recovery_required``.

CLOUD_ATLAS_REFRESH_HANDOFF_KIND = "cloud_atlas_refresh_handoff"
CLOUD_ATLAS_REFRESH_HANDOFF_RECEIVER = "cloud-atlas"
CLOUD_ATLAS_REFRESH_HANDOFF_OPERATION = "refresh_filemap"
CLOUD_ATLAS_REFRESH_HANDOFF_SCHEMA_VERSION = 1

#: The ordered steps the receiving Cloud-Atlas consumer must perform itself.
REQUIRED_RECEIVING_STEPS: tuple[str, ...] = (
    "verify_cloud_atlas_lock",
    "revalidate_handoff_hash",
    "revalidate_promotion_journal",
    "reverify_real_target",
    "refresh_bound_storage",
    "verify_filemap_entry",
)

#: Steps the receiving consumer is explicitly forbidden to perform automatically.
PROHIBITED_AUTOMATIC_STEPS: tuple[str, ...] = (
    "re_run_promotion",
    "mailbox_mutation",
    "catalog_mutation",
    "workspace_wide_scan",
)

#: The Cloud-Atlas refresh result kind and its closed status vocabulary.
REFRESH_RESULT_KIND = "cloud_atlas_refresh_result"
REFRESH_STATUS_COMPLETED = "refresh_completed"
REFRESH_STATUS_PENDING = "refresh_pending"
REFRESH_STATUS_DENIED = "refresh_denied"
REFRESH_RESULT_STATUSES: frozenset[str] = frozenset(
    {REFRESH_STATUS_COMPLETED, REFRESH_STATUS_PENDING, REFRESH_STATUS_DENIED}
)

#: Combined promotion outcome when the local promotion succeeded but the receiving
#: Cloud-Atlas refresh has not (yet) been verified.
STATUS_PROMOTION_COMPLETED_REFRESH_PENDING = "promotion_completed_refresh_pending"

#: Refresh denials that indicate real drift and therefore stop as recovery_required.
_HANDOFF_DRIFT_REASONS: frozenset[str] = frozenset(
    {"journal_drift", "target_drift", "handoff_drift"}
)

_HANDOFF_KEY_ORDER: tuple[str, ...] = (
    "schema_version",
    "kind",
    "receiver",
    "operation",
    "promotion_id",
    "journal_relative_path",
    "journal_hash",
    "candidate_hash",
    "review_hash",
    "preflight_hash",
    "scope",
    "entity_id",
    "subtopic_id",
    "storage_id",
    "scan_dir",
    "target_relative_path",
    "target_sha256",
    "target_size_bytes",
    "filemap_snapshot_hash",
    "required_receiving_steps",
    "prohibited_automatic_steps",
    "handoff_hash",
)


class PromotionHandoffError(ValueError):
    """Raised when an MD-P2 result cannot yield a canonical Cloud-Atlas handoff."""


def _require_handoff_text(value: Any, field: str) -> str:
    if not _is_nonempty_text(value):
        raise PromotionHandoffError(f"Cloud-Atlas refresh handoff requires a non-empty '{field}'.")
    return str(value).strip()


def _require_handoff_sha256(value: Any, field: str) -> str:
    text = str(value or "").strip().lower()
    if not _is_sha256_hex(text):
        raise PromotionHandoffError(f"Cloud-Atlas refresh handoff '{field}' is not 64-hex.")
    return text


def _verified_promotion_result(promotion_result: Any) -> dict[str, Any]:
    """Validate an ``attachment_promotion_result`` and return a shallow copy."""
    if not isinstance(promotion_result, Mapping):
        raise PromotionHandoffError("Cloud-Atlas refresh handoff requires a promotion result object.")
    if promotion_result.get("schema_version") != SCHEMA_VERSION:
        raise PromotionHandoffError("Unsupported promotion result schema_version.")
    if promotion_result.get("kind") != PROMOTION_RESULT_KIND:
        raise PromotionHandoffError("Wrong promotion result kind.")
    declared = str(promotion_result.get("result_hash") or "").strip().lower()
    body = {key: value for key, value in promotion_result.items() if key != "result_hash"}
    if not _is_sha256_hex(declared) or declared != canonical_json_sha256(body):
        raise PromotionHandoffError("Promotion result_hash does not recompute.")
    return dict(promotion_result)


def _build_outcome_result(
    promotion_result: Mapping[str, Any],
    *,
    status: str,
    reason: str,
    error_code: str | None,
    refresh_status: str | None = None,
    refresh_result_hash: str | None = None,
    handoff_hash: str | None = None,
) -> dict[str, Any]:
    """Return the deterministic combined ``attachment_promotion_result`` Schema 1."""
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": PROMOTION_RESULT_KIND,
        "status": status,
        "reason": reason,
        "error_code": error_code,
        "phase": promotion_result.get("phase"),
        "promotion_id": promotion_result.get("promotion_id"),
        "candidate_hash": promotion_result.get("candidate_hash"),
        "review_hash": promotion_result.get("review_hash"),
        "preflight_hash": promotion_result.get("preflight_hash"),
        "storage_id": promotion_result.get("storage_id"),
        "target_relative_path": promotion_result.get("target_relative_path"),
        "target_sha256": promotion_result.get("target_sha256"),
        "target_size_bytes": promotion_result.get("target_size_bytes"),
        "journal_relative_path": promotion_result.get("journal_relative_path"),
        "journal_hash": promotion_result.get("journal_hash"),
        "handoff_hash": handoff_hash,
        "refresh_status": refresh_status,
        "refresh_result_hash": refresh_result_hash,
    }
    payload["result_hash"] = canonical_json_sha256(payload)
    return payload


def build_cloud_atlas_refresh_handoff(
    promotion_result: Mapping[str, Any],
    candidate: Mapping[str, Any],
    filemap: Mapping[str, Any],
    *,
    workspace_root: Path | str | None = None,
    data_dir: Path | None = None,
    journal_path: Path | str | None = None,
) -> dict[str, Any]:
    """Derive the ``cloud_atlas_refresh_handoff`` Schema 1 from a verified MD-P2 result.

    Only ``promotion_completed`` and ``already_present_verified`` are accepted, and
    *both* require an intact promotion journal: the journal is the trust anchor, so a
    journal-less result is rejected fail-closed.  The journal is revalidated through
    :func:`load_promotion_journal`, the candidate hash and review payload are
    recomputed and every result/journal/candidate/storage/path/size binding -- including
    the recorded ``preflight_hash`` and the ``already_present`` annotation -- is
    cross-checked.  Any drift raises :class:`PromotionHandoffError`.  No filesystem
    artifact is written.
    """
    result = _verified_promotion_result(promotion_result)
    status = str(result.get("status") or "")
    if status not in (STATUS_PROMOTION_COMPLETED, STATUS_ALREADY_PRESENT_VERIFIED):
        raise PromotionHandoffError(
            f"Cloud-Atlas refresh handoff requires a verified promotion, got {status!r}."
        )
    if not isinstance(candidate, Mapping) or not isinstance(filemap, Mapping):
        raise PromotionHandoffError("Cloud-Atlas refresh handoff requires a candidate and a filemap.")

    candidate_hash = compute_candidate_hash(candidate)
    if candidate_hash != _require_handoff_sha256(result.get("candidate_hash"), "candidate_hash"):
        raise PromotionHandoffError("Candidate hash does not match the promotion result.")
    review_hash = compute_promotion_review_hash(candidate, filemap)
    if review_hash != _require_handoff_sha256(result.get("review_hash"), "review_hash"):
        raise PromotionHandoffError("Review payload hash does not match the promotion result.")
    promotion_id = _require_handoff_sha256(result.get("promotion_id"), "promotion_id")
    if promotion_id != derive_promotion_id(review_hash, candidate_hash):
        raise PromotionHandoffError("Promotion id does not derive from review + candidate hash.")
    preflight_hash = _require_handoff_sha256(result.get("preflight_hash"), "preflight_hash")
    target_sha256 = _require_handoff_sha256(result.get("target_sha256"), "target_sha256")
    target_size = result.get("target_size_bytes")
    if not isinstance(target_size, int) or isinstance(target_size, bool) or target_size <= 0:
        raise PromotionHandoffError("Promotion result target_size_bytes must be a positive integer.")
    storage_id = _require_handoff_text(result.get("storage_id"), "storage_id")
    target_relative_path = _normalize_slashes(result.get("target_relative_path")).strip("/")
    if not _is_safe_relative_path(target_relative_path):
        raise PromotionHandoffError("Promotion result target_relative_path is not a safe path.")

    destination = candidate.get("destination")
    filemap_evidence = candidate.get("filemap_evidence")
    if not isinstance(destination, Mapping) or not isinstance(filemap_evidence, Mapping):
        raise PromotionHandoffError("Candidate is missing its destination/filemap evidence.")
    if str(destination.get("storage_id") or "").strip() != storage_id:
        raise PromotionHandoffError("Candidate storage_id does not match the promotion result.")
    if _normalize_slashes(destination.get("target_relative_path")).strip("/") != target_relative_path:
        raise PromotionHandoffError("Candidate target path does not match the promotion result.")

    scope = str(filemap_evidence.get("scope") or "").strip().lower()
    if scope not in ("project", "topic"):
        raise PromotionHandoffError("Candidate filemap scope must be 'project' or 'topic'.")
    entity_id = _require_handoff_text(filemap_evidence.get("project"), "entity_id")
    raw_subtopic = filemap_evidence.get("subtopic_id") or filemap_evidence.get("subtopic")
    candidate_subtopic = str(raw_subtopic).strip() if _is_nonempty_text(raw_subtopic) else None
    scan_dir = _normalize_slashes(filemap_evidence.get("scan_dir")).strip("/")
    if not _is_safe_relative_dir(scan_dir):
        raise PromotionHandoffError("Candidate scan_dir is not a safe workspace-relative path.")

    ws_root = resolve_workspace_root(workspace_root, data_dir=data_dir)
    resolved_journal_path: Path | None = None
    journal_relative_path: str | None = None
    raw_journal_relative = result.get("journal_relative_path")
    if _is_nonempty_text(raw_journal_relative):
        rel = _normalize_slashes(raw_journal_relative).strip("/")
        if not _is_safe_relative_path(rel):
            raise PromotionHandoffError("Promotion journal path is not a safe relative path.")
        journal_relative_path = rel
        resolved_journal_path = ws_root / PurePosixPath(rel)
    elif journal_path is not None:
        resolved_journal_path = Path(journal_path)
        rel = _relative_posix(ws_root, resolved_journal_path)
        if not _is_safe_relative_path(rel):
            raise PromotionHandoffError("Promotion journal path is not a safe relative path.")
        journal_relative_path = rel

    # Both verified statuses require an intact, revalidatable journal: the journal --
    # not the result -- is the trust anchor.  A journal-less result (or one whose
    # journal is missing) is rejected fail-closed; format-only result checks are not
    # sufficient, and the recorded preflight_hash is cross-checked below.
    if resolved_journal_path is None or journal_relative_path is None:
        raise PromotionHandoffError(
            "Cloud-Atlas refresh handoff requires a promotion journal for both result statuses."
        )
    if not resolved_journal_path.is_file():
        raise PromotionHandoffError("Cloud-Atlas refresh handoff journal does not exist.")

    try:
        journal = load_promotion_journal(
            resolved_journal_path,
            expected_promotion_id=promotion_id,
            expected_candidate_hash=candidate_hash,
            expected_review_hash=review_hash,
            expected_source_sha256=target_sha256,
            expected_target_relative_path=target_relative_path,
        )
    except PromotionJournalError as exc:
        raise PromotionHandoffError(f"Promotion journal revalidation failed: {exc}") from exc
    for field, expected in (
        ("preflight_hash", preflight_hash),
        ("storage_id", storage_id),
        ("target_relative_path", target_relative_path),
        ("target_sha256", target_sha256),
        ("source_sha256", target_sha256),
    ):
        if str(journal.get(field) or "").strip().lower() != str(expected).strip().lower():
            raise PromotionHandoffError(f"Promotion journal drift on '{field}'.")
    if journal.get("source_size_bytes") != target_size:
        raise PromotionHandoffError("Promotion journal size does not match the result.")
    if str(journal.get("status") or "") != "completed":
        raise PromotionHandoffError("Promotion journal is not completed.")
    expected_already_present = status == STATUS_ALREADY_PRESENT_VERIFIED
    if bool(journal.get("already_present")) != expected_already_present:
        raise PromotionHandoffError(
            "Promotion journal already_present annotation does not match the result status."
        )
    journal_hash = _require_handoff_sha256(journal.get("journal_hash"), "journal_hash")

    # The canonical candidate never records the subtopic (its filemap_evidence holds
    # only scope/entity/storage/scan_dir).  The writer persists the decision-bound
    # subtopic into the journal (see promote_attachment / _bound_subtopic_id), so the
    # journal is the authoritative source; a legacy candidate-carried value is only a
    # fallback.  A project-scope handoff must never carry a subtopic.
    journal_subtopic = journal.get("subtopic_id")
    subtopic_id = (
        str(journal_subtopic).strip()
        if _is_nonempty_text(journal_subtopic)
        else candidate_subtopic
    )
    if subtopic_id is not None and scope != "topic":
        raise PromotionHandoffError("A project-scope handoff must not carry a subtopic_id.")

    handoff: dict[str, Any] = {
        "schema_version": CLOUD_ATLAS_REFRESH_HANDOFF_SCHEMA_VERSION,
        "kind": CLOUD_ATLAS_REFRESH_HANDOFF_KIND,
        "receiver": CLOUD_ATLAS_REFRESH_HANDOFF_RECEIVER,
        "operation": CLOUD_ATLAS_REFRESH_HANDOFF_OPERATION,
        "promotion_id": promotion_id,
        "journal_relative_path": journal_relative_path,
        "journal_hash": journal_hash,
        "candidate_hash": candidate_hash,
        "review_hash": review_hash,
        "preflight_hash": preflight_hash,
        "scope": scope,
        "entity_id": entity_id,
        "subtopic_id": subtopic_id,
        "storage_id": storage_id,
        "scan_dir": scan_dir,
        "target_relative_path": target_relative_path,
        "target_sha256": target_sha256,
        "target_size_bytes": target_size,
        "filemap_snapshot_hash": _filemap_snapshot_hash(filemap),
        "required_receiving_steps": list(REQUIRED_RECEIVING_STEPS),
        "prohibited_automatic_steps": list(PROHIBITED_AUTOMATIC_STEPS),
    }
    handoff["handoff_hash"] = canonical_json_sha256(handoff)
    return handoff


def _revalidate_outcome_journal(
    result: Mapping[str, Any],
    status: str,
    *,
    workspace_root: Path | str | None,
    journal_path: Path | str | None,
) -> str | None:
    """Revalidate the promotion journal anchor for :func:`compose_promotion_outcome`.

    Returns a bounded drift reason when the anchor is missing or does not revalidate,
    else ``None``.  The journal -- not the result -- is the trust anchor, so composition
    enforces the same requirement as :func:`build_cloud_atlas_refresh_handoff`.  Without a
    supplied ``workspace_root``/``journal_path`` only the binding presence can be checked
    (no workspace root is fabricated); a resolvable location is fully re-loaded through
    :func:`load_promotion_journal` and cross-checked against the result.
    """
    if status not in (STATUS_PROMOTION_COMPLETED, STATUS_ALREADY_PRESENT_VERIFIED):
        return None
    relative = result.get("journal_relative_path")
    anchor = str(result.get("journal_hash") or "").strip().lower()
    if not _is_nonempty_text(relative) or not _is_sha256_hex(anchor):
        return CODE_JOURNAL_CORRUPTED
    if workspace_root is None and journal_path is None:
        return None
    ws_root = resolve_workspace_root(workspace_root, data_dir=None)
    if journal_path is not None:
        resolved = Path(journal_path)
    else:
        normalized = _normalize_slashes(relative).strip("/")
        if not _is_safe_relative_path(normalized):
            return CODE_JOURNAL_CORRUPTED
        resolved = ws_root / PurePosixPath(normalized)
    try:
        journal = load_promotion_journal(
            resolved,
            expected_promotion_id=result.get("promotion_id"),
            expected_candidate_hash=result.get("candidate_hash"),
            expected_review_hash=result.get("review_hash"),
            expected_source_sha256=result.get("target_sha256"),
            expected_target_relative_path=result.get("target_relative_path"),
        )
    except (PromotionJournalError, OSError):
        return "journal_drift"
    if str(journal.get("journal_hash") or "").strip().lower() != anchor:
        return "journal_drift"
    if str(journal.get("status") or "") != "completed":
        return "journal_drift"
    if bool(journal.get("already_present")) != (status == STATUS_ALREADY_PRESENT_VERIFIED):
        return "journal_drift"
    for field in ("preflight_hash", "storage_id", "target_relative_path"):
        if str(journal.get(field) or "").strip().lower() != str(
            result.get(field) or ""
        ).strip().lower():
            return "journal_drift"
    return None


def compose_promotion_outcome(
    promotion_result: Mapping[str, Any],
    refresh_outcome: Mapping[str, Any] | None = None,
    *,
    workspace_root: Path | str | None = None,
    journal_path: Path | str | None = None,
) -> dict[str, Any]:
    """Couple a verified promotion with a Cloud-Atlas refresh result, without mutation.

    A missing/invalid adapter, a refresh error/timeout/verify error or an untrusted
    refresh result yields ``promotion_completed_refresh_pending`` with the unchanged
    ``promotion_id``.  A verified refresh for the same target yields
    ``promotion_completed``/``refresh_completed``.  Journal/target/cross-binding drift
    yields ``recovery_required``.  MD-P2 is never re-run.

    The promotion journal is revalidated for every verified status (the same trust
    anchor :func:`build_cloud_atlas_refresh_handoff` enforces): with no
    ``workspace_root``/``journal_path`` the result must at least carry a valid journal
    binding, and a supplied location is re-loaded through
    :func:`load_promotion_journal`.
    """
    result = _verified_promotion_result(promotion_result)
    promotion_status = str(result.get("status") or "")
    if promotion_status in (STATUS_RECOVERY_REQUIRED, STATUS_COLLISION_DETECTED):
        return _build_outcome_result(
            result, status=STATUS_RECOVERY_REQUIRED,
            reason=str(result.get("reason") or promotion_status),
            error_code=str(result.get("error_code") or promotion_status),
        )
    if promotion_status not in (
        STATUS_PROMOTION_COMPLETED, STATUS_ALREADY_PRESENT_VERIFIED, STATUS_SOURCE_CLEANUP_PENDING
    ):
        return _build_outcome_result(
            result, status=STATUS_RECOVERY_REQUIRED,
            reason=CODE_JOURNAL_CORRUPTED, error_code=CODE_JOURNAL_CORRUPTED,
        )

    journal_reason = _revalidate_outcome_journal(
        result, promotion_status,
        workspace_root=workspace_root, journal_path=journal_path,
    )
    if journal_reason is not None:
        return _build_outcome_result(
            result, status=STATUS_RECOVERY_REQUIRED,
            reason=journal_reason, error_code=journal_reason,
        )

    refresh_status: str | None = None
    refresh_result_hash: str | None = None
    handoff_hash: str | None = None
    if isinstance(refresh_outcome, Mapping) and refresh_outcome.get("kind") == REFRESH_RESULT_KIND:
        declared = str(refresh_outcome.get("result_hash") or "").strip().lower()
        body = {key: value for key, value in refresh_outcome.items() if key != "result_hash"}
        if _is_sha256_hex(declared) and declared == canonical_json_sha256(body):
            if str(refresh_outcome.get("promotion_id") or "").strip().lower() != str(
                result.get("promotion_id") or ""
            ).strip().lower():
                return _build_outcome_result(
                    result, status=STATUS_RECOVERY_REQUIRED,
                    reason="refresh_promotion_drift", error_code="refresh_promotion_drift",
                )
            refresh_result_hash = declared
            handoff_hash = str(refresh_outcome.get("handoff_hash") or "").strip() or None
            token = str(refresh_outcome.get("status") or "")
            if token == REFRESH_STATUS_COMPLETED:
                same_target = (
                    str(refresh_outcome.get("target_relative_path") or "")
                    == str(result.get("target_relative_path") or "")
                    and str(refresh_outcome.get("target_sha256") or "").strip().lower()
                    == str(result.get("target_sha256") or "").strip().lower()
                )
                if not same_target:
                    return _build_outcome_result(
                        result, status=STATUS_RECOVERY_REQUIRED,
                        reason="refresh_target_drift", error_code="refresh_target_drift",
                    )
                refresh_status = REFRESH_STATUS_COMPLETED
            elif token == REFRESH_STATUS_DENIED and str(
                refresh_outcome.get("reason") or ""
            ) in _HANDOFF_DRIFT_REASONS:
                return _build_outcome_result(
                    result, status=STATUS_RECOVERY_REQUIRED,
                    reason=str(refresh_outcome.get("reason") or REFRESH_STATUS_DENIED),
                    error_code=str(refresh_outcome.get("reason") or REFRESH_STATUS_DENIED),
                )
            else:
                refresh_status = REFRESH_STATUS_PENDING

    if refresh_status == REFRESH_STATUS_COMPLETED:
        return _build_outcome_result(
            result, status=STATUS_PROMOTION_COMPLETED, reason="promotion_completed",
            error_code=None, refresh_status=REFRESH_STATUS_COMPLETED,
            refresh_result_hash=refresh_result_hash, handoff_hash=handoff_hash,
        )
    return _build_outcome_result(
        result, status=STATUS_PROMOTION_COMPLETED_REFRESH_PENDING,
        reason=REFRESH_STATUS_PENDING, error_code=None,
        refresh_status=REFRESH_STATUS_PENDING, refresh_result_hash=refresh_result_hash,
        handoff_hash=handoff_hash,
    )


def write_cloud_atlas_refresh_handoff(handoff: Mapping[str, Any], output_path: Path | str) -> Path:
    """Atomically persist a revalidated handoff to ``output_path`` and return it.

    The handoff hash is recomputed before writing; a tampered mapping raises
    :class:`PromotionHandoffError`.  The write uses a flushed sibling temp file plus
    ``os.replace`` (the dossier-handoff pattern) and never touches any other path.
    """
    if not isinstance(handoff, Mapping):
        raise PromotionHandoffError("write_cloud_atlas_refresh_handoff requires a handoff mapping.")
    declared = str(handoff.get("handoff_hash") or "").strip().lower()
    body = {key: value for key, value in handoff.items() if key != "handoff_hash"}
    if not _is_sha256_hex(declared) or declared != canonical_json_sha256(body):
        raise PromotionHandoffError("Refusing to write a handoff whose handoff_hash does not recompute.")
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _canonical_json_bytes(dict(handoff))
    tmp_path = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(
        tmp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    )
    try:
        handle = os.fdopen(descriptor, "wb")
    except OSError:
        try:
            os.close(descriptor)
        except OSError:
            pass
        _unlink_quiet(tmp_path)
        raise
    try:
        with handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except OSError:
        _unlink_quiet(tmp_path)
        raise
    return path


__all__ = [
    "compute_promotion_review_hash",
    "verify_promotion_approval_receipt",
    "preflight_attachment_promotion",
    "derive_promotion_id",
    "load_promotion_journal",
    "promote_attachment",
    "build_cloud_atlas_refresh_handoff",
    "compose_promotion_outcome",
    "write_cloud_atlas_refresh_handoff",
    "PromotionJournalError",
    "PromotionHandoffError",
    "JOURNAL_PHASES",
    "PROMOTION_RESULT_STATUSES",
    "CLOUD_ATLAS_REFRESH_HANDOFF_KIND",
    "CLOUD_ATLAS_REFRESH_HANDOFF_RECEIVER",
    "CLOUD_ATLAS_REFRESH_HANDOFF_OPERATION",
    "REQUIRED_RECEIVING_STEPS",
    "PROHIBITED_AUTOMATIC_STEPS",
    "REFRESH_RESULT_KIND",
    "REFRESH_RESULT_STATUSES",
    "REFRESH_STATUS_COMPLETED",
    "REFRESH_STATUS_PENDING",
    "REFRESH_STATUS_DENIED",
    "STATUS_PROMOTION_COMPLETED_REFRESH_PENDING",
]
