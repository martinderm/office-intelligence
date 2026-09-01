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

    return {
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
