#!/usr/bin/env python3
"""Read-only validator for the three consuming-workspace catalogs (FR-21/MD-S5).

One standalone, mutation-free CLI validates the workspace catalogs that
``mail-desk`` consumes from ``memory/references/``:

* ``topics/topics.json`` -- required.  Accepts a bare array or an object with a
  ``topics`` array, mirroring ``core.classifier.load_catalogs``.  Every topic
  needs non-empty ``id``/``title``/``mailbox_folder`` strings and may carry a
  ``typical_subject_patterns`` array.  Root-level patterns are consumed by
  ``select_topic_match`` as plain substrings without a length gate, so a
  non-empty string after trimming is enough; nested patterns
  (``subtopics[].typical_subject_patterns`` and the same field on their
  ``operations[]``/``events[]``) flow through the lookaround
  ``_subject_signal_matches`` gate and must be at least three characters after
  trimming.
* ``projects/projects.json`` -- required.  Accepts a bare array or an object with
  a ``projects`` array.  Every project needs ``id``/``title``/``mailbox_folder``/
  ``workpackages``/``milestones`` and a strict integer ``schema_version`` of
  exactly ``3``; every workpackage needs ``id``/``title``/``status``/``tasks``/
  ``deliverables`` (mirroring ``project-catalog-entry`` schema v3).
* ``mail-desk/mail-desk.json`` -- optional.  When present it mirrors the
  ``core.matching.reply_heuristics.load_reply_heuristics`` schema-1 gate exactly;
  a missing file is valid because the loader falls back to the documented
  compatibility defaults.

Where ``load_catalogs`` silently swallows a missing or malformed required catalog,
this validator fails loud with a structured report
``{"valid": bool, "errors": [{"catalog", "path", "reason"}, ...]}`` sorted by
``(catalog, path)`` so two runs are byte-stable.  The CLI emits one canonical
envelope and exits ``0`` (valid), ``1`` (drift) or ``2`` (input/runtime).
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
import json
from pathlib import Path
import sys
from typing import Any

_script_dir = Path(__file__).resolve().parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))

from core import build_error, build_success, emit_json


ACTION = "catalog_validator"

#: Canonical workspace-relative catalog paths (portable, forward slashes).
TOPICS_RELATIVE_PATH = "memory/references/topics/topics.json"
PROJECTS_RELATIVE_PATH = "memory/references/projects/projects.json"
MAIL_DESK_RELATIVE_PATH = "memory/references/mail-desk/mail-desk.json"

#: The only supported catalog schema revisions (mirrored from their owners).
PROJECTS_SCHEMA_VERSION = 3
REPLY_HEURISTICS_SCHEMA_VERSION = 1

#: Minimum effective length of a *nested* subject signal
#: (``_subject_signal_matches``); root-level patterns are plain substrings.
MIN_SUBJECT_PATTERN_LENGTH = 3

#: JSON paths always use ``$`` as the root and never leak host-specific prefixes.
_ROOT = "$"


def _error(catalog: str, path: str, reason: str) -> dict[str, str]:
    """Build one structured, deterministic drift entry."""
    return {"catalog": catalog, "path": path, "reason": reason}


def _is_nonempty_str(value: object) -> bool:
    """True for a string that survives ``strip()`` without becoming empty."""
    return isinstance(value, str) and bool(value.strip())


def _resolve_entries(
    catalog: str, data: object, key: str
) -> tuple[list[Any] | None, str, list[dict[str, str]]]:
    """Resolve a bare-array or ``{key: [...]}`` payload like ``load_catalogs``.

    Returns the entry list, the JSON path prefix of that list, and any container
    drift.  ``(None, ..., errors)`` signals that the payload shape itself is wrong.
    """
    if isinstance(data, list):
        return data, _ROOT, []
    if isinstance(data, Mapping):
        if key not in data:
            return None, _ROOT, [
                _error(
                    catalog,
                    _ROOT,
                    f"payload must be a JSON array or an object with a '{key}' array",
                )
            ]
        entries = data.get(key)
        if not isinstance(entries, list):
            path = f"{_ROOT}.{key}"
            return None, path, [_error(catalog, path, f"'{key}' must be a JSON array")]
        return entries, f"{_ROOT}.{key}", []
    return None, _ROOT, [
        _error(
            catalog,
            _ROOT,
            f"payload must be a JSON array or an object with a '{key}' array",
        )
    ]


def _validate_required_strings(
    catalog: str,
    entry: Mapping[str, Any],
    base: str,
    fields: tuple[str, ...],
    label: str,
    errors: list[dict[str, str]],
) -> None:
    """Require each field to be present and a non-empty, trimmed string."""
    for field in fields:
        if field not in entry:
            errors.append(
                _error(catalog, f"{base}.{field}", f"{label} is missing required field '{field}'")
            )
        elif not _is_nonempty_str(entry.get(field)):
            errors.append(
                _error(
                    catalog,
                    f"{base}.{field}",
                    f"{label} field '{field}' must be a non-empty string",
                )
            )


def _validate_required_list(
    catalog: str,
    entry: Mapping[str, Any],
    base: str,
    field: str,
    label: str,
    errors: list[dict[str, str]],
) -> None:
    """Require a field to be present and a JSON array."""
    if field not in entry:
        errors.append(
            _error(catalog, f"{base}.{field}", f"{label} is missing required field '{field}'")
        )
    elif not isinstance(entry.get(field), list):
        errors.append(
            _error(catalog, f"{base}.{field}", f"{label} field '{field}' must be a JSON array")
        )


def _validate_pattern_list(
    catalog: str,
    owner: Mapping[str, Any],
    base: str,
    *,
    nested: bool,
    errors: list[dict[str, str]],
) -> None:
    """Validate one optional ``typical_subject_patterns`` list.

    Root-level patterns are consumed by ``select_topic_match`` as plain
    substrings (only a truthiness gate after trimming), so a short root pattern is
    legal catalog content.  Nested patterns flow through the lookaround
    ``_subject_signal_matches`` gate, which additionally requires at least
    ``MIN_SUBJECT_PATTERN_LENGTH`` characters after trimming.
    """
    if "typical_subject_patterns" not in owner:
        return
    patterns = owner.get("typical_subject_patterns")
    path = f"{base}.typical_subject_patterns"
    if not isinstance(patterns, list):
        errors.append(_error(catalog, path, "'typical_subject_patterns' must be a JSON array"))
        return
    level = "nested" if nested else "root-level"
    for pattern_index, pattern in enumerate(patterns):
        pattern_path = f"{path}[{pattern_index}]"
        if not isinstance(pattern, str):
            errors.append(
                _error(catalog, pattern_path, f"{level} typical_subject_patterns entry must be a string")
            )
        elif not pattern.strip():
            errors.append(
                _error(
                    catalog,
                    pattern_path,
                    f"{level} typical_subject_patterns entry requires a non-empty string",
                )
            )
        elif nested and len(pattern.strip()) < MIN_SUBJECT_PATTERN_LENGTH:
            errors.append(
                _error(
                    catalog,
                    pattern_path,
                    f"{level} typical_subject_patterns entry must be at least "
                    f"{MIN_SUBJECT_PATTERN_LENGTH} characters after trimming",
                )
            )


def _validate_nested_patterns(
    catalog: str,
    subtopic: Mapping[str, Any],
    base: str,
    errors: list[dict[str, str]],
) -> None:
    """Walk one subtopic plus its ``operations[]``/``events[]`` pattern lists."""
    _validate_pattern_list(catalog, subtopic, base, nested=True, errors=errors)
    for collection_key in ("operations", "events"):
        collection = subtopic.get(collection_key)
        if not isinstance(collection, list):
            continue
        for entry_index, entry in enumerate(collection):
            if not isinstance(entry, Mapping):
                continue
            _validate_pattern_list(
                catalog,
                entry,
                f"{base}.{collection_key}[{entry_index}]",
                nested=True,
                errors=errors,
            )


def validate_topics_catalog(
    data: object, catalog: str = TOPICS_RELATIVE_PATH
) -> list[dict[str, str]]:
    """Validate one parsed ``topics.json`` payload; never raises on drift."""
    entries, prefix, errors = _resolve_entries(catalog, data, "topics")
    if entries is None:
        return errors

    for index, topic in enumerate(entries):
        base = f"{prefix}[{index}]"
        if not isinstance(topic, Mapping):
            errors.append(_error(catalog, base, "topic entry must be a JSON object"))
            continue
        _validate_required_strings(
            catalog,
            topic,
            base,
            ("id", "title", "mailbox_folder"),
            "topic",
            errors,
        )
        # Root-level patterns are plain substrings, so only non-emptiness applies.
        _validate_pattern_list(catalog, topic, base, nested=False, errors=errors)
        subtopics = topic.get("subtopics")
        if not isinstance(subtopics, list):
            continue
        for subtopic_index, subtopic in enumerate(subtopics):
            if not isinstance(subtopic, Mapping):
                continue
            _validate_nested_patterns(
                catalog,
                subtopic,
                f"{base}.subtopics[{subtopic_index}]",
                errors,
            )
    return errors


def _validate_workpackage(
    catalog: str,
    workpackage: object,
    base: str,
    errors: list[dict[str, str]],
) -> None:
    """Validate one project workpackage entry (schema v3)."""
    if not isinstance(workpackage, Mapping):
        errors.append(_error(catalog, base, "workpackage entry must be a JSON object"))
        return
    _validate_required_strings(
        catalog,
        workpackage,
        base,
        ("id", "title", "status"),
        "workpackage",
        errors,
    )
    for field in ("tasks", "deliverables"):
        _validate_required_list(catalog, workpackage, base, field, "workpackage", errors)


def validate_projects_catalog(
    data: object, catalog: str = PROJECTS_RELATIVE_PATH
) -> list[dict[str, str]]:
    """Validate one parsed ``projects.json`` payload against schema v3."""
    entries, prefix, errors = _resolve_entries(catalog, data, "projects")
    if entries is None:
        return errors

    for index, project in enumerate(entries):
        base = f"{prefix}[{index}]"
        if not isinstance(project, Mapping):
            errors.append(_error(catalog, base, "project entry must be a JSON object"))
            continue
        _validate_required_strings(
            catalog,
            project,
            base,
            ("id", "title", "mailbox_folder"),
            "project",
            errors,
        )
        schema_version = project.get("schema_version")
        # Strict type gate: ``True == 3`` and ``3.0 == 3`` would pass a loose ``==``
        # comparison, so a bool/float/str/None schema_version must fail loud.
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version != PROJECTS_SCHEMA_VERSION
        ):
            errors.append(
                _error(
                    catalog,
                    f"{base}.schema_version",
                    f"schema_version must be exactly the integer {PROJECTS_SCHEMA_VERSION}",
                )
            )
        for field in ("workpackages", "milestones"):
            _validate_required_list(catalog, project, base, field, "project", errors)
        workpackages = project.get("workpackages")
        if isinstance(workpackages, list):
            for workpackage_index, workpackage in enumerate(workpackages):
                _validate_workpackage(
                    catalog,
                    workpackage,
                    f"{base}.workpackages[{workpackage_index}]",
                    errors,
                )
    return errors


def validate_maildesk_catalog(
    data: object, catalog: str = MAIL_DESK_RELATIVE_PATH
) -> list[dict[str, str]]:
    """Validate a parsed ``mail-desk.json`` exactly like ``load_reply_heuristics``."""
    errors: list[dict[str, str]] = []
    if not isinstance(data, Mapping):
        return [_error(catalog, _ROOT, "top-level value must be a JSON object")]

    schema_version = data.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != REPLY_HEURISTICS_SCHEMA_VERSION
    ):
        errors.append(
            _error(
                catalog,
                f"{_ROOT}.schema_version",
                f"schema_version must be exactly the integer {REPLY_HEURISTICS_SCHEMA_VERSION}",
            )
        )

    block = data.get("reply_heuristics")
    if not isinstance(block, Mapping):
        errors.append(
            _error(catalog, f"{_ROOT}.reply_heuristics", "'reply_heuristics' must be a JSON object")
        )
        return errors

    triggers = block.get("reply_triggers")
    if not isinstance(triggers, list) or not triggers:
        errors.append(
            _error(
                catalog,
                f"{_ROOT}.reply_heuristics.reply_triggers",
                "'reply_triggers' is required and must be a non-empty list of strings",
            )
        )
    else:
        for trigger_index, trigger in enumerate(triggers):
            if not _is_nonempty_str(trigger):
                errors.append(
                    _error(
                        catalog,
                        f"{_ROOT}.reply_heuristics.reply_triggers[{trigger_index}]",
                        "'reply_triggers' must contain only non-empty strings",
                    )
                )

    if "no_reply_sender_tokens" in block:
        tokens = block.get("no_reply_sender_tokens")
        if not isinstance(tokens, list):
            errors.append(
                _error(
                    catalog,
                    f"{_ROOT}.reply_heuristics.no_reply_sender_tokens",
                    "'no_reply_sender_tokens' must be a list of non-empty strings",
                )
            )
        else:
            for token_index, token in enumerate(tokens):
                if not _is_nonempty_str(token):
                    errors.append(
                        _error(
                            catalog,
                            f"{_ROOT}.reply_heuristics.no_reply_sender_tokens[{token_index}]",
                            "'no_reply_sender_tokens' must contain only non-empty strings",
                        )
                    )

    if "owner_address" in block:
        owner_address = block.get("owner_address")
        if owner_address is not None and not isinstance(owner_address, str):
            errors.append(
                _error(
                    catalog,
                    f"{_ROOT}.reply_heuristics.owner_address",
                    "'owner_address' must be a string or null",
                )
            )
    return errors


def _load_catalog(
    workspace_root: Path, relative_path: str
) -> tuple[bool, object, list[dict[str, str]]]:
    """Read and parse one catalog file; return ``(loaded, data, errors)``."""
    path = workspace_root / relative_path
    if not path.is_file():
        return False, None, [_error(relative_path, _ROOT, "required catalog file is missing")]
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return False, None, [
            _error(relative_path, _ROOT, f"catalog file is unreadable ({exc})")
        ]
    try:
        return True, json.loads(text), []
    except json.JSONDecodeError as exc:
        return False, None, [
            _error(relative_path, _ROOT, f"catalog is not valid JSON ({exc})")
        ]


def validate_workspace_catalogs(workspace_root: Path | str) -> dict[str, Any]:
    """Validate all three workspace catalogs and return a sorted report.

    Raises ``ValueError`` when ``workspace_root`` is not a directory (the CLI maps
    that to exit code ``2``).  A missing ``topics.json``/``projects.json`` is drift;
    a missing ``mail-desk.json`` is the documented valid fallback.
    """
    root = Path(workspace_root)
    if not root.is_dir():
        raise ValueError(f"workspace root is not a directory: {root}")

    errors: list[dict[str, str]] = []

    loaded, data, load_errors = _load_catalog(root, TOPICS_RELATIVE_PATH)
    errors.extend(load_errors)
    if loaded:
        errors.extend(validate_topics_catalog(data))

    loaded, data, load_errors = _load_catalog(root, PROJECTS_RELATIVE_PATH)
    errors.extend(load_errors)
    if loaded:
        errors.extend(validate_projects_catalog(data))

    # ``mail-desk.json`` is optional: only a present file is validated.
    if (root / MAIL_DESK_RELATIVE_PATH).is_file():
        loaded, data, load_errors = _load_catalog(root, MAIL_DESK_RELATIVE_PATH)
        errors.extend(load_errors)
        if loaded:
            errors.extend(validate_maildesk_catalog(data))

    errors.sort(key=lambda error: (error["catalog"], error["path"]))
    return {"valid": not errors, "errors": errors}


def _emit_report_error(message: str, error_type: str, *, state: str, data: dict[str, Any]) -> None:
    emit_json(build_error(ACTION, message, data, error_type=error_type, state=state))


def main(argv: list[str] | None = None) -> int:
    """Run the CLI; return ``0`` valid, ``1`` drift or ``2`` input/runtime."""
    parser = argparse.ArgumentParser(
        prog="catalog_validator",
        description="Validate the workspace topics/projects/mail-desk catalogs (read-only).",
    )
    parser.add_argument(
        "--workspace",
        required=True,
        help="Path to the consuming workspace root.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the canonical JSON envelope (the only supported output format).",
    )
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        if exc.code not in (None, 0):
            _emit_report_error(
                "Invalid command-line arguments.",
                "ArgumentError",
                state="Failed",
                data={"valid": False, "errors": []},
            )
            return 2
        raise

    try:
        report = validate_workspace_catalogs(args.workspace)
    except Exception as exc:  # noqa: BLE001 - fail closed with a structured envelope
        _emit_report_error(
            "Workspace catalog validation could not run.",
            type(exc).__name__,
            state="Failed",
            data={"valid": False, "errors": []},
        )
        return 2

    if report["valid"]:
        emit_json(build_success(ACTION, "Workspace catalogs are valid.", report))
        return 0

    _emit_report_error(
        "Workspace catalog drift detected.",
        "CatalogDrift",
        state="Drift",
        data=report,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
