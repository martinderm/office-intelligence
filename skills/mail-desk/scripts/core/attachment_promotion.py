"""Human-gated approval binding and read-only promotion preflight (FR-09 / MD-P1).

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
import os
from pathlib import Path, PurePosixPath
import re
from typing import Any, Mapping

from core.attachment_authorization import (
    CONTEXT_PROMOTION,
    RECEIPT_TYPE_ATTACHMENT_AUTO_EVALUATION,
    guard_context_authorization,
)
from core.modes.dossier_synthesis import canonical_json_sha256
from core.quarantine.attachment_fetch import (
    QuarantineInventoryError,
    RFC3339_REGEX,
    SymlinkEscapeError,
    TrackedQuarantineError,
    WIN32_RESERVED_NAMES,
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


__all__ = [
    "compute_promotion_review_hash",
    "verify_promotion_approval_receipt",
    "preflight_attachment_promotion",
]
