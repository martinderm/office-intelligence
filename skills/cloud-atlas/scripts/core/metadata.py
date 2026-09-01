"""Build canonical metadata for artifacts derived from cloud sources."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import re
from typing import Any


_SHA256_PATTERN = re.compile(r"^[a-fA-F0-9]{64}$")
_DATA_CLASSIFICATIONS = frozenset(
    {"public", "internal", "confidential", "restricted"}
)
CANONICAL_CLOUD_METADATA_KEYS = frozenset(
    {
        "zone", "trust_level", "status", "source_uri", "source_version",
        "source_sha256", "artifact_sha256", "synced_at", "converter",
        "data_classification", "retention_class", "owner",
        "instructions_are_data", "origin_classifications", "export_policy",
        "promotion_policy",
    }
)
REQUIRED_CLOUD_METADATA_KEYS = frozenset(
    {
        "zone", "trust_level", "status", "source_uri", "source_sha256",
        "artifact_sha256", "synced_at", "converter", "data_classification",
        "retention_class", "owner", "instructions_are_data",
    }
)
_POLICY_VALUES = {
    "export_policy": frozenset({"allowed", "approval_required", "prohibited"}),
    "promotion_policy": frozenset({"allowed", "approval_required", "prohibited"}),
}


def _non_empty_string(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _sha256(name: str, value: Any) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must contain exactly 64 hexadecimal characters")
    return value.lower()


def _rfc3339_timestamp(value: Any) -> str:
    if not isinstance(value, datetime):
        raise ValueError("synced_at must be a timezone-aware datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("synced_at must be timezone-aware")
    return value.isoformat(timespec="seconds")


def validate_cloud_artifact_metadata(
    metadata: Any,
    *,
    expected_source_uri: str | None = None,
) -> bool:
    """Validate one canonical cloud artifact metadata object.

    This is intentionally a small, explicit runtime contract.  The normative
    JSON Schema remains the review-time SSOT, while the bundled skill stays
    usable without a neighboring checkout or a third-party JSON-Schema
    package.
    """
    if not isinstance(metadata, dict):
        raise ValueError("cloud artifact metadata must be an object")
    missing = sorted(REQUIRED_CLOUD_METADATA_KEYS - set(metadata))
    if missing:
        raise ValueError(f"cloud artifact metadata is missing required keys: {missing}")
    unknown = sorted(set(metadata) - CANONICAL_CLOUD_METADATA_KEYS)
    if unknown:
        raise ValueError(f"cloud artifact metadata has non-canonical keys: {unknown}")

    if metadata["zone"] != "cloud":
        raise ValueError("cloud artifact metadata.zone must be 'cloud'")
    if metadata["trust_level"] != "untrusted_external":
        raise ValueError("cloud artifact metadata.trust_level must be 'untrusted_external'")
    if not isinstance(metadata["status"], str) or metadata["status"] not in {
        "active", "superseded", "tombstoned", "archived", "expired"
    }:
        raise ValueError("cloud artifact metadata.status is invalid")
    if metadata["instructions_are_data"] is not True:
        raise ValueError("cloud artifact metadata.instructions_are_data must be true")

    for field in ("source_uri", "converter", "retention_class", "owner"):
        _non_empty_string(field, metadata[field])
    for field in ("source_sha256", "artifact_sha256"):
        _sha256(field, metadata[field])
    if (
        not isinstance(metadata["data_classification"], str)
        or metadata["data_classification"] not in _DATA_CLASSIFICATIONS
    ):
        raise ValueError("cloud artifact metadata.data_classification is invalid")
    if expected_source_uri is not None and metadata["source_uri"] != expected_source_uri:
        raise ValueError(
            f"cloud artifact metadata.source_uri must match {expected_source_uri!r}"
        )

    synced_at = metadata["synced_at"]
    if not isinstance(synced_at, str) or not synced_at.strip():
        raise ValueError("cloud artifact metadata.synced_at must be an RFC-3339 timestamp")
    try:
        parsed = datetime.fromisoformat(synced_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("cloud artifact metadata.synced_at must be an RFC-3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("cloud artifact metadata.synced_at must include a timezone")

    if "source_version" in metadata and metadata["source_version"] is not None:
        _non_empty_string("source_version", metadata["source_version"])
    if "origin_classifications" in metadata:
        classifications = metadata["origin_classifications"]
        if (
            not isinstance(classifications, list)
            or any(not isinstance(item, str) or not item.strip() for item in classifications)
            or len(set(classifications)) != len(classifications)
        ):
            raise ValueError("cloud artifact metadata.origin_classifications must be unique non-empty strings")
    for field, allowed in _POLICY_VALUES.items():
        if field in metadata and (
            not isinstance(metadata[field], str) or metadata[field] not in allowed
        ):
            raise ValueError(f"cloud artifact metadata.{field} is invalid")
    return True


def build_cloud_artifact_metadata(
    *,
    source_uri: str,
    source_sha256: str,
    artifact_sha256: str,
    converter: str,
    data_classification: str,
    retention_class: str,
    owner: str,
    synced_at: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, object]:
    """Return schema-ready metadata for one active cloud-derived artifact.

    ``synced_at`` is explicit when supplied. Otherwise ``clock`` is called once;
    callers can inject it for deterministic runs and tests. The returned mapping
    intentionally contains only canonical data-zone schema properties.
    """

    source_uri = _non_empty_string("source_uri", source_uri)
    converter = _non_empty_string("converter", converter)
    retention_class = _non_empty_string("retention_class", retention_class)
    owner = _non_empty_string("owner", owner)

    if not isinstance(data_classification, str):
        raise ValueError("data_classification must be a string")
    if data_classification not in _DATA_CLASSIFICATIONS:
        allowed = ", ".join(sorted(_DATA_CLASSIFICATIONS))
        raise ValueError(f"data_classification must be one of: {allowed}")

    if synced_at is not None and clock is not None:
        raise ValueError("provide either synced_at or clock, not both")
    timestamp = synced_at
    if timestamp is None:
        timestamp = clock() if clock is not None else datetime.now(timezone.utc)

    metadata = {
        "zone": "cloud",
        "trust_level": "untrusted_external",
        "status": "active",
        "source_uri": source_uri,
        "source_sha256": _sha256("source_sha256", source_sha256),
        "artifact_sha256": _sha256("artifact_sha256", artifact_sha256),
        "synced_at": _rfc3339_timestamp(timestamp),
        "converter": converter,
        "data_classification": data_classification,
        "retention_class": retention_class,
        "owner": owner,
        "instructions_are_data": True,
    }
    validate_cloud_artifact_metadata(metadata)
    return metadata
