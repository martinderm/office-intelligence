"""Catalog- and filemap-backed attachment filing candidate proposal (MD-A5).

Purely read-only module: no uploads, no directory creation, no filemap modification,
no catalog mutation. Generates an attachment_filing_candidate with promotion_status:
"pending_human_review".
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any, Mapping, Sequence

from .attachment_policy import sanitize_attachment_filename


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
        return False, None, "Filemap missing 'updated_at' timestamp"

    dt: datetime | None = None
    cleaned_ts = updated_at_str.strip()
    for fmt in TIMESTAMP_FORMATS:
        try:
            dt = datetime.strptime(cleaned_ts, fmt)
            break
        except ValueError:
            continue

    if dt is None:
        return False, updated_at_str, f"Unparseable filemap 'updated_at' timestamp: {updated_at_str!r}"

    now = current_time or datetime.now(timezone.utc)
    if dt.tzinfo is not None and now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    elif dt.tzinfo is None and now.tzinfo is not None:
        dt = dt.replace(tzinfo=timezone.utc)

    age_seconds = (now - dt).total_seconds()
    if age_seconds < -3600:  # Clock drift into future > 1 hour
        return False, updated_at_str, f"Filemap timestamp is in the future ({age_seconds:.0f}s ahead)"
    if age_seconds > max_age_seconds:
        return False, updated_at_str, f"Filemap is stale ({age_seconds:.0f}s old, max allowed {max_age_seconds}s)"

    return True, updated_at_str, None


def compute_candidate_hash(candidate_dict: Mapping[str, Any]) -> str:
    """Compute deterministic 64-char SHA-256 binding all candidate fields."""
    norm = {k: v for k, v in candidate_dict.items() if k != "candidate_hash"}
    encoded = json.dumps(norm, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded, usedforsecurity=False).hexdigest()


# ==============================================================================
# Catalog Resolution
# ==============================================================================

def resolve_catalog_storage(
    decision: Mapping[str, Any],
    catalogs: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """Resolve storage configuration from catalogs based on decision.

    Returns:
        tuple[storage_mapping | None, reason | None, error_code | None]:
        (cloud_sync_dict, reason, error_code)
    """
    kind = str(decision.get("kind") or "").strip().lower()
    entity_id = str(decision.get("id") or "").strip()
    subtopic_id = str(decision.get("subtopic") or "").strip()
    event_id = str(decision.get("event") or "").strip()

    projects = catalogs.get("projects", [])
    topics = catalogs.get("topics", [])

    if kind == "project":
        proj = next((p for p in projects if isinstance(p, dict) and p.get("id") == entity_id), None)
        if not proj:
            return None, f"Project '{entity_id}' not found in catalog", STATUS_NOT_CONFIGURED
        cloud_sync = proj.get("cloud_sync")
        if not cloud_sync or not isinstance(cloud_sync, Mapping):
            return None, f"Project '{entity_id}' has no cloud_sync configured", STATUS_NOT_CONFIGURED
        return dict(cloud_sync), None, None

    if kind == "topic":
        top = next((t for t in topics if isinstance(t, dict) and t.get("id") == entity_id), None)
        if not top:
            return None, f"Topic '{entity_id}' not found in catalog", STATUS_NOT_CONFIGURED
        cloud_sync = top.get("cloud_sync")
        if not cloud_sync or not isinstance(cloud_sync, Mapping):
            return None, f"Topic '{entity_id}' has no cloud_sync configured", STATUS_NOT_CONFIGURED
        return dict(cloud_sync), None, None

    if kind == "subtopic":
        # Look for parent topic
        parent_topic = next((t for t in topics if isinstance(t, dict) and (t.get("id") == entity_id or any(s.get("id") == (subtopic_id or entity_id) for s in t.get("subtopics", [])))), None)
        sub = None
        if parent_topic:
            sub = next((s for s in parent_topic.get("subtopics", []) if s.get("id") in (subtopic_id, entity_id)), None)

        if sub and sub.get("cloud_sync") and isinstance(sub.get("cloud_sync"), Mapping):
            return dict(sub["cloud_sync"]), None, None
        if parent_topic and parent_topic.get("cloud_sync") and isinstance(parent_topic.get("cloud_sync"), Mapping):
            return dict(parent_topic["cloud_sync"]), None, None
        return None, f"Subtopic '{subtopic_id or entity_id}' has no explicit or inherited cloud_sync", STATUS_NOT_CONFIGURED

    if kind == "event":
        # Events only inherit explicitly cataloged parent or subtopic storages
        parent_topic = next((t for t in topics if isinstance(t, dict) and (t.get("id") == entity_id or any(s.get("id") == subtopic_id for s in t.get("subtopics", [])))), None)
        sub = None
        if parent_topic and subtopic_id:
            sub = next((s for s in parent_topic.get("subtopics", []) if s.get("id") == subtopic_id), None)

        if sub and sub.get("cloud_sync") and isinstance(sub.get("cloud_sync"), Mapping):
            return dict(sub["cloud_sync"]), None, None
        if parent_topic and parent_topic.get("cloud_sync") and isinstance(parent_topic.get("cloud_sync"), Mapping):
            return dict(parent_topic["cloud_sync"]), None, None
        return None, f"Event '{event_id or entity_id}' has no explicitly cataloged parent or subtopic storage", STATUS_NOT_CONFIGURED

    return None, f"Unsupported decision kind '{kind}' for filing", STATUS_NOT_CONFIGURED


# ==============================================================================
# Filing Proposal Generator
# ==============================================================================

def propose_attachment_filing(
    attachment: Mapping[str, Any],
    decision: Mapping[str, Any],
    catalogs: Mapping[str, Any],
    *,
    filemaps: Mapping[str, Any] | None = None,
    workspace_root: Path | None = None,
    handoff: Mapping[str, Any] | None = None,
    max_filemap_age_seconds: int = DEFAULT_MAX_FILEMAP_AGE_SECONDS,
    current_time: datetime | None = None,
) -> dict[str, Any]:
    """Generate a read-only attachment filing proposal candidate.

    Evaluates the strict decision matrix:
    - kein Storage -> not_configured
    - mehrere Storages oder stale Filemap -> storage_review_required
    - kein belegtes Verzeichnis -> directory_review_required
    - gleicher Hash -> already_present
    - gleicher Name/anderer Hash -> collision_detected
    - eindeutiges Ziel -> proposed

    Guarantees:
    - Never modifies filemap, catalogs, or cloud files.
    - Path traversal protection on filenames and directories.
    - Returns promotion_status: "pending_human_review".
    - Produces deterministic candidate_hash.
    """
    raw_filename = attachment.get("filename") or "attachment"
    clean_filename = sanitize_attachment_filename(raw_filename)
    sha256 = str(attachment.get("sha256") or "").strip().lower()

    source_info = {
        "account": str(attachment.get("account") or "").strip(),
        "message_id": str(attachment.get("message_id") or "").strip(),
        "folder": str(attachment.get("folder") or "INBOX").strip(),
        "envelope_id": str(attachment.get("envelope_id") or "").strip(),
        "part_locator": str(attachment.get("part_locator") or "").strip(),
        "filename": clean_filename,
        "sha256": sha256,
        "quarantine_path": str(attachment.get("quarantine_path") or "").strip(),
    }

    base_candidate: dict[str, Any] = {
        "schema_version": 1,
        "candidate_type": "attachment_filing_candidate",
        "promotion_status": PROMOTION_STATUS_PENDING_HUMAN_REVIEW,
        "status": STATUS_NOT_CONFIGURED,
        "reason": "",
        "source": source_info,
        "destination": {
            "storage_id": None,
            "target_dir": None,
            "target_filename": clean_filename,
            "target_relative_path": None,
        },
        "filemap_evidence": {
            "filemap_path": None,
            "filemap_updated_at": None,
            "is_stale": False,
        },
        "dedupe": {
            "already_present": False,
            "collision_detected": False,
            "existing_path": None,
            "existing_sha256": None,
        },
    }

    # 1. Resolve storage from catalogs
    cloud_sync, err_reason, err_status = resolve_catalog_storage(decision, catalogs)
    if not cloud_sync:
        base_candidate["status"] = err_status or STATUS_NOT_CONFIGURED
        base_candidate["reason"] = err_reason or "No cloud storage configured"
        base_candidate["candidate_hash"] = compute_candidate_hash(base_candidate)
        return base_candidate

    # 2. Check storage ambiguity, archive, and read-only flags
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

    # 3. Determine target directory
    target_dir = (
        decision.get("target_dir")
        or storage_cfg.get("target_dir")
        or storage_cfg.get("documents_dir")
        or storage_cfg.get("default_dir")
    )
    if not target_dir or not str(target_dir).strip():
        base_candidate["status"] = STATUS_DIRECTORY_REVIEW_REQUIRED
        base_candidate["reason"] = f"No target directory specified in decision or storage configuration for '{storage_id}'"
        base_candidate["candidate_hash"] = compute_candidate_hash(base_candidate)
        return base_candidate

    norm_target_dir = PurePosixPath(str(target_dir).strip().replace("\\", "/")).as_posix().strip("/")
    if ".." in norm_target_dir.split("/") or norm_target_dir.startswith("/"):
        base_candidate["status"] = STATUS_DIRECTORY_REVIEW_REQUIRED
        base_candidate["reason"] = f"Target directory path contains invalid traversal components: {target_dir!r}"
        base_candidate["candidate_hash"] = compute_candidate_hash(base_candidate)
        return base_candidate

    base_candidate["destination"]["target_dir"] = norm_target_dir
    target_relative_path = f"{norm_target_dir}/{clean_filename}"
    base_candidate["destination"]["target_relative_path"] = target_relative_path

    # 4. Resolve and validate filemap
    filemap_data = None
    filemap_path_str = None

    if filemaps and storage_id in filemaps:
        filemap_data = filemaps[storage_id]
        filemap_path_str = f"<in-memory:{storage_id}>"
    elif filemaps and "filemap" in filemaps:
        filemap_data = filemaps["filemap"]
        filemap_path_str = "<in-memory:filemap>"
    else:
        # Resolve from storage config or workspace standard paths
        configured_path = storage_cfg.get("output_json") or storage_cfg.get("filemap_json")
        ws = workspace_root or Path.cwd()
        resolved_filemap_path: Path | None = None
        if configured_path:
            p = Path(configured_path)
            resolved_filemap_path = p if p.is_absolute() else (ws / p)
        else:
            kind = str(decision.get("kind") or "").strip().lower()
            ent_id = str(decision.get("id") or "").strip()
            folder_kind = "projects" if kind == "project" else "topics"
            resolved_filemap_path = ws / "memory" / "cloud" / folder_kind / ent_id / "filemap.json"

        if resolved_filemap_path and resolved_filemap_path.is_file():
            filemap_path_str = str(resolved_filemap_path)
            try:
                with resolved_filemap_path.open("r", encoding="utf-8") as fh:
                    filemap_data = json.load(fh)
            except Exception as exc:
                base_candidate["status"] = STATUS_STORAGE_REVIEW_REQUIRED
                base_candidate["reason"] = f"Failed to read filemap at {resolved_filemap_path}: {exc}"
                base_candidate["candidate_hash"] = compute_candidate_hash(base_candidate)
                return base_candidate
        else:
            base_candidate["status"] = STATUS_STORAGE_REVIEW_REQUIRED
            base_candidate["reason"] = f"Filemap not found for storage '{storage_id}' (searched {resolved_filemap_path})"
            base_candidate["candidate_hash"] = compute_candidate_hash(base_candidate)
            return base_candidate

    base_candidate["filemap_evidence"]["filemap_path"] = filemap_path_str

    # 5. Check filemap freshness
    is_fresh, updated_at, freshness_err = validate_filemap_freshness(
        filemap_data,
        max_age_seconds=max_filemap_age_seconds,
        current_time=current_time,
    )
    base_candidate["filemap_evidence"]["filemap_updated_at"] = updated_at
    if not is_fresh:
        base_candidate["filemap_evidence"]["is_stale"] = True
        base_candidate["status"] = STATUS_STORAGE_REVIEW_REQUIRED
        base_candidate["reason"] = f"Filemap is stale or invalid: {freshness_err}"
        base_candidate["candidate_hash"] = compute_candidate_hash(base_candidate)
        return base_candidate

    # 6. Check that target directory is established/occupied in filemap
    files_map = filemap_data.get("files")
    if not isinstance(files_map, Mapping):
        base_candidate["status"] = STATUS_STORAGE_REVIEW_REQUIRED
        base_candidate["reason"] = "Filemap 'files' entry is missing or not a dictionary"
        base_candidate["candidate_hash"] = compute_candidate_hash(base_candidate)
        return base_candidate

    dir_prefix = norm_target_dir + "/"
    is_dir_occupied = any(
        filepath.startswith(dir_prefix) or filepath == norm_target_dir
        for filepath in files_map
    )
    if not is_dir_occupied:
        base_candidate["status"] = STATUS_DIRECTORY_REVIEW_REQUIRED
        base_candidate["reason"] = f"Target directory '{norm_target_dir}' is not established/occupied in filemap"
        base_candidate["candidate_hash"] = compute_candidate_hash(base_candidate)
        return base_candidate

    # 7. Check for identical SHA-256 (already_present)
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
    if target_relative_path in files_map:
        existing_entry = files_map[target_relative_path]
        ext_sha = str(existing_entry.get("sha256") or "").strip().lower() if isinstance(existing_entry, Mapping) else ""
        base_candidate["status"] = STATUS_COLLISION_DETECTED
        base_candidate["reason"] = f"File with name '{clean_filename}' already exists at destination with different SHA-256 ({ext_sha})"
        base_candidate["dedupe"] = {
            "already_present": False,
            "collision_detected": True,
            "existing_path": target_relative_path,
            "existing_sha256": ext_sha,
        }
        base_candidate["candidate_hash"] = compute_candidate_hash(base_candidate)
        return base_candidate

    # 9. All preconditions satisfied: propose filing!
    base_candidate["status"] = STATUS_PROPOSED
    base_candidate["reason"] = f"Filing candidate proposed for storage '{storage_id}' at '{target_relative_path}'"
    base_candidate["candidate_hash"] = compute_candidate_hash(base_candidate)
    return base_candidate
