"""Validated, declarative curation overlays for Cloud Atlas filemaps.

The overlay is deliberately independent from a generated filemap.  It contains
only human-maintained fields and a receipt for the extraction it was curated
from; scanner and converter fields are always recomputed by the current run.
"""

from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath
from typing import Any


CURATION_SCHEMA_VERSION = 1
CURATION_SCHEMA_URI = (
    "https://raw.githubusercontent.com/martinderm/office-intelligence/main/"
    "skills/cloud-atlas/references/filemap-curation.schema.json"
)
GENERATED_FILEMAP_FIELDS = frozenset({
    "version", "mtime", "size", "sha256", "markdown_mirror", "derivative",
    "conversion_status", "conversion_error", "ocr_applied", "ocr_policy",
    "artifact_metadata",
})
_ENTRY_FIELDS = frozenset({"source_sha256", "description", "custom"})
_SHA256 = re.compile(r"^[a-fA-F0-9]{64}$")


class CurationOverlayError(ValueError):
    """A fail-closed curation catalog error, raised before output mutation."""


def normalize_workspace_relative_path(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip().replace("\\", "/")
    if raw.startswith(("/", "//")) or re.match(r"^[A-Za-z]:/", raw):
        return None
    path = PurePosixPath(raw)
    if ".." in path.parts:
        return None
    return path.as_posix()


def _within(path: str, parent: str) -> bool:
    try:
        PurePosixPath(path).relative_to(PurePosixPath(parent))
        return True
    except ValueError:
        return False


def _require_sha256(label: str, value: Any) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise CurationOverlayError(f"{label} must be a 64-character SHA-256 hex string")
    return value.lower()


def _expected_target(scope: str, target_id: str, subtopic_id: str | None) -> dict[str, str]:
    if scope == "subtopic":
        return {"topic_id": target_id, "subtopic_id": subtopic_id or ""}
    return {"id": target_id}


class CurationOverlay:
    """Validated curation entries with exact-path then unique-SHA matching."""

    def __init__(self, entries: dict[str, dict[str, Any]]):
        self.entries = entries

    def match(self, source_path: str, source_sha256: str | None) -> dict[str, Any] | None:
        exact = self.entries.get(source_path)
        if exact is not None:
            return exact
        if not source_sha256:
            return None
        matches = [
            entry for entry in self.entries.values()
            if entry.get("source_sha256") == source_sha256.lower()
        ]
        if len(matches) > 1:
            raise CurationOverlayError(
                f"curation overlay has ambiguous source_sha256 matches for {source_path!r}"
            )
        return matches[0] if matches else None


def load_curation_overlay(
    workspace_root: str | Path,
    relative_path: Any,
    *,
    scope: str,
    target_id: str,
    storage_id: str,
    scan_dir: str,
    subtopic_id: str | None = None,
) -> CurationOverlay | None:
    """Load one catalog-declared overlay and validate it before any mutation.

    ``relative_path`` is intentionally sourced only from ``cloud_sync``.  A
    missing key preserves legacy behavior; a configured but broken overlay is
    an error, not a fallback to the generated filemap.
    """
    if relative_path is None:
        return None
    normalized_path = normalize_workspace_relative_path(relative_path)
    if not normalized_path:
        raise CurationOverlayError("curation_json must be a workspace-relative path")
    root = Path(workspace_root).resolve()
    overlay_path = (root / Path(*PurePosixPath(normalized_path).parts)).resolve()
    try:
        overlay_path.relative_to(root)
    except ValueError as exc:
        raise CurationOverlayError("curation_json resolves outside the workspace") from exc
    if not overlay_path.is_file():
        raise CurationOverlayError(f"curation_json does not exist: {normalized_path}")
    try:
        document = json.loads(overlay_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CurationOverlayError(f"curation_json is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise CurationOverlayError("curation overlay must be an object")
    required = {
        "$schema", "schema_version", "kind", "scope", "target", "storage_id",
        "source_receipt", "entries",
    }
    if set(document) != required:
        raise CurationOverlayError(
            f"curation overlay keys must be exactly {sorted(required)}"
        )
    if document["$schema"] != CURATION_SCHEMA_URI:
        raise CurationOverlayError("curation overlay does not identify the bundled schema")
    if document["schema_version"] != CURATION_SCHEMA_VERSION:
        raise CurationOverlayError(f"curation overlay schema_version must be {CURATION_SCHEMA_VERSION}")
    if document["kind"] != "cloud-filemap-curation":
        raise CurationOverlayError("curation overlay kind must be 'cloud-filemap-curation'")
    if document["scope"] != scope:
        raise CurationOverlayError("curation overlay scope does not match the selected cloud_sync target")
    expected_target = _expected_target(scope, target_id, subtopic_id)
    if document["target"] != expected_target:
        raise CurationOverlayError("curation overlay target does not match the selected cloud_sync target")
    if document["storage_id"] != storage_id:
        raise CurationOverlayError("curation overlay storage_id does not match the selected storage")

    receipt = document["source_receipt"]
    if not isinstance(receipt, dict) or "sha256" not in receipt:
        raise CurationOverlayError("curation overlay source_receipt must include sha256")
    allowed_receipt = {"sha256", "file_count", "created_at"}
    if not set(receipt) <= allowed_receipt:
        raise CurationOverlayError("curation overlay source_receipt has unknown keys")
    _require_sha256("source_receipt.sha256", receipt["sha256"])
    if "file_count" in receipt and (
        not isinstance(receipt["file_count"], int) or isinstance(receipt["file_count"], bool)
        or receipt["file_count"] < 0
    ):
        raise CurationOverlayError("source_receipt.file_count must be a non-negative integer")
    if "created_at" in receipt and (
        not isinstance(receipt["created_at"], str) or not receipt["created_at"].strip()
    ):
        raise CurationOverlayError("source_receipt.created_at must be a non-empty string")

    scan_rel = normalize_workspace_relative_path(scan_dir)
    entries = document["entries"]
    if not scan_rel or not isinstance(entries, dict):
        raise CurationOverlayError("curation overlay entries must be an object for a safe scan_dir")
    normalized_entries: dict[str, dict[str, Any]] = {}
    for source_path, entry in entries.items():
        source_rel = normalize_workspace_relative_path(source_path)
        if not source_rel or not _within(source_rel, scan_rel):
            raise CurationOverlayError(f"curation entry path is outside scan_dir: {source_path!r}")
        if not isinstance(entry, dict) or not set(entry) <= _ENTRY_FIELDS:
            raise CurationOverlayError(f"curation entry {source_path!r} has forbidden fields")
        if not entry:
            raise CurationOverlayError(f"curation entry {source_path!r} must not be empty")
        normalized = dict(entry)
        if "source_sha256" in normalized:
            normalized["source_sha256"] = _require_sha256(
                f"curation entry {source_path!r}.source_sha256", normalized["source_sha256"]
            )
        if "description" in normalized and not isinstance(normalized["description"], str):
            raise CurationOverlayError(f"curation entry {source_path!r}.description must be a string")
        if "custom" in normalized:
            custom = normalized["custom"]
            if not isinstance(custom, dict):
                raise CurationOverlayError(f"curation entry {source_path!r}.custom must be an object")
            forbidden = set(custom) & (GENERATED_FILEMAP_FIELDS | {"description", "source_sha256"})
            if forbidden:
                raise CurationOverlayError(
                    f"curation entry {source_path!r}.custom overrides generated fields: {sorted(forbidden)}"
                )
            if any(not isinstance(key, str) or not key.strip() for key in custom):
                raise CurationOverlayError(f"curation entry {source_path!r}.custom has an invalid key")
        normalized_entries[source_rel] = normalized
    return CurationOverlay(normalized_entries)


def merge_curated_metadata(
    existing_entry: Any,
    generated_entry: dict[str, Any],
    overlay_entry: dict[str, Any] | None = None,
    *,
    overlay_active: bool = False,
) -> dict[str, Any]:
    """Use legacy curation only when no declarative overlay is configured.

    Existing filemap data still reaches callers for technical mirror and
    derivative continuity.  Once an overlay is active, however, its entries
    are the complete curation view: a removed entry or custom key must not be
    resurrected from the local generated state.
    """
    result = dict(generated_entry)
    if not overlay_active and isinstance(existing_entry, dict):
        existing_description = existing_entry.get("description")
        if isinstance(existing_description, str) and existing_description not in {"", "-"}:
            result["description"] = existing_description
        for key, value in existing_entry.items():
            if key not in GENERATED_FILEMAP_FIELDS and key != "description" and key not in result:
                result[key] = value
    if overlay_entry:
        if "description" in overlay_entry:
            result["description"] = overlay_entry["description"]
        for key, value in overlay_entry.get("custom", {}).items():
            result[key] = value
    return result
