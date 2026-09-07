"""Validation for review-enriched post-batch synthesis targets."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any, Mapping, Sequence


_OPTIONAL_FIELDS = ("recommended_action", "task_anchor", "section")
_ALLOWED_FIELDS = {"file", "type", *_OPTIONAL_FIELDS}
_SAFE_SLUG = re.compile(r"^[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*$")
_WINDOWS_RESERVED_CHARACTERS = set('<>:"|?*')


def _error(item_index: int, detail: str, target_index: int | None = None) -> ValueError:
    context = f"Execute item {item_index}"
    if target_index is not None:
        context += f" synthesis_targets[{target_index}]"
    return ValueError(f"{context}: {detail}")


def _nonempty_string(
    value: object,
    field: str,
    item_index: int,
    target_index: int,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _error(item_index, f"{field} must be a non-empty string", target_index)
    return value.strip()


def _validate_file(
    file_value: str,
    kind: str,
    identifier: str,
    item_index: int,
    target_index: int,
) -> None:
    raw_segments = file_value.split("/")
    if (
        "\\" in file_value
        or file_value.startswith("/")
        or "//" in file_value
        or "://" in file_value
    ):
        raise _error(item_index, "file must be a safe relative POSIX path", target_index)

    path = PurePosixPath(file_value)
    parts = path.parts
    if (
        not parts
        or any(part in {".", ".."} for part in raw_segments)
        or any(re.fullmatch(r"[A-Za-z]:", part) for part in parts)
        or path.suffix != ".md"
    ):
        raise _error(
            item_index,
            "file must be a safe relative POSIX path to a .md file",
            target_index,
        )

    if any(
        any(ord(character) < 32 or ord(character) == 127 for character in segment)
        or any(character in _WINDOWS_RESERVED_CHARACTERS for character in segment)
        or segment.endswith((".", " "))
        for segment in raw_segments
    ):
        raise _error(
            item_index,
            "file contains a Windows-unsafe path segment",
            target_index,
        )

    expected_root = ("memory", "references", f"{kind}s", identifier)
    if len(parts) <= len(expected_root) or parts[: len(expected_root)] != expected_root:
        expected = "/".join((*expected_root, "..."))
        raise _error(item_index, f"file must be under {expected}", target_index)


def validate_execute_synthesis_targets(
    items: Sequence[Mapping[str, Any]],
) -> list[list[dict[str, str]]]:
    """Validate every execute item's optional targets before any mutation.

    Missing targets remain backwards-compatible as empty lists.  The returned
    values are normalized copies, so execute results never reuse untrusted
    manifest objects.
    """
    validated: list[list[dict[str, str]]] = []
    for item_index, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise _error(item_index, "must be an object")

        raw_targets = item.get("synthesis_targets", [])
        if not isinstance(raw_targets, list):
            raise _error(item_index, "synthesis_targets must be a list")
        if not raw_targets:
            validated.append([])
            continue

        decision = item.get("decision")
        if not isinstance(decision, Mapping):
            raise _error(item_index, "decision must be an object when synthesis_targets are present")
        kind = decision.get("kind")
        identifier = decision.get("id")
        if kind not in {"project", "topic"}:
            raise _error(item_index, "only project or topic decisions may have synthesis_targets")
        if not isinstance(identifier, str) or not _SAFE_SLUG.fullmatch(identifier):
            raise _error(item_index, "decision.id must be a non-empty safe slug when synthesis_targets are present")

        item_targets: list[dict[str, str]] = []
        for target_index, target in enumerate(raw_targets):
            if not isinstance(target, Mapping):
                raise _error(item_index, "must be an object", target_index)
            unknown = set(target) - _ALLOWED_FIELDS
            if unknown:
                raise _error(
                    item_index,
                    f"contains unsupported key(s): {', '.join(sorted(str(key) for key in unknown))}",
                    target_index,
                )

            target_type = _nonempty_string(
                target.get("type"), "type", item_index, target_index
            )
            if not _SAFE_SLUG.fullmatch(target_type):
                raise _error(
                    item_index,
                    "type must be a stable label matching [A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*",
                    target_index,
                )

            normalized = {
                "file": _nonempty_string(target.get("file"), "file", item_index, target_index),
                "type": target_type,
            }
            _validate_file(
                normalized["file"],
                kind,
                identifier,
                item_index,
                target_index,
            )
            for field in _OPTIONAL_FIELDS:
                if field in target:
                    normalized[field] = _nonempty_string(
                        target[field], field, item_index, target_index
                    )
            item_targets.append(normalized)
        validated.append(item_targets)
    return validated
