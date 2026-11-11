"""Read-only retention classification for versioned cloud-atlas targets (Issue #97).

The ``--only-file`` publish path writes every conversion into a fresh, immutable
directory below ``<output_dir>/_versions/<source-key>/<run>/``.  Its contract
(``core/publish_journal.py``) deliberately has no cleanup, no archive and no
quarantine.  This module adds the missing *policy* without adding an action:

* It only reads the target filemap, the reserved version root, a journal summary
  and the workspace catalogs; every path component is checked before resolving
  or descending, and links/junctions/placeholders fail closed.
* Active references use Windows path aliases only on Windows (case-insensitive,
  trailing dots/spaces ignored). A referenced link/placeholder run stays
  ``active`` but its contents are never traversed.
* The filemap must declare an ``output_dir`` that matches the audited output;
  a legacy filemap without it fails closed because its ownership is unprovable.
  The filemap/output pair is bound to a catalog entry (``output_json`` and
  ``output_dir``, including ``filemap_json``/``cloud_mirror_dir`` aliases and
  synthesized defaults); any catalog read error, or not exactly one match, makes
  the classification explicitly non-authoritative. An unreadable source-key parent
  with an active reference is reported as ``unresolved``/``potentially_active``
  instead of a candidate.
* It classifies existing version directories as ``active``, ``retained`` or
  ``quarantine_candidate``.  A classification is advice for an operator, never
  an authorization to delete or move anything.
* It never writes, creates, renames, moves or deletes a path.  The physical
  quarantine namespace is reserved in the documentation only; this module does
  not create it.
* Foreign filemaps and consumers are not discoverable.  Every entry therefore
  keeps ``foreign_reference_risk: "unknown"`` and ``safe_to_delete``/
  ``safe_to_move`` are always ``false``.

The thresholds are advisory reporting labels, not a storage bound.  A version
root can still grow without limit; only a human operator can decide whether a
candidate is safe to handle outside this contract.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath

from .artifact_refs import is_cloud_placeholder
from .filemap_merge import FilemapError, normalize_relative, parse_filemap
from .publish_journal import JOURNAL_DIR, VERSION_DIR

POLICY_ID = "cloud-atlas-retention-v1"
QUARANTINE_DIR = "_quarantine"

# Policy v1.  These are *classification* thresholds, not a quota and not a
# deletion schedule.  The active version is always the one referenced by the
# target filemap; additionally, the newest two inactive versions per source and
# every inactive version younger than 90 days are retained.  Inactive versions
# older than 365 days are candidates regardless of the count.
RETAINED_INACTIVE_VERSIONS = 2
RETAINED_INACTIVE_AGE_DAYS = 90.0
QUARANTINE_AGE_DAYS = 365.0

DATA_ZONES = ("memory/references", "memory/evidence", "memory/cloud", "data")
_SOURCE_KEY = re.compile(r"[0-9a-f]{16}")
_SECONDS_PER_DAY = 86400.0


class RetentionError(ValueError):
    """The retention audit cannot classify the target safely (read-only)."""


@dataclass(frozen=True)
class RetentionPolicy:
    """The documented, advisory thresholds of retention policy v1."""

    retained_inactive_versions: int = RETAINED_INACTIVE_VERSIONS
    retained_inactive_age_days: float = RETAINED_INACTIVE_AGE_DAYS
    quarantine_age_days: float = QUARANTINE_AGE_DAYS

    def __post_init__(self):
        if self.retained_inactive_versions < 0:
            raise RetentionError("retained_inactive_versions must not be negative")
        if self.retained_inactive_age_days < 0 or self.quarantine_age_days < 0:
            raise RetentionError("retention ages must not be negative")
        if self.quarantine_age_days < self.retained_inactive_age_days:
            raise RetentionError("quarantine_age_days must not be lower than the retention grace age")

    def as_dict(self) -> dict:
        return {
            "id": POLICY_ID,
            "active": "the version directory referenced by the target filemap",
            "retained_inactive_versions": self.retained_inactive_versions,
            "retained_inactive_age_days": self.retained_inactive_age_days,
            "quarantine_age_days": self.quarantine_age_days,
            "quarantine": "logical classification only; no physical area is created or written",
            "automatic_actions": "none",
            "storage_bound": "none; these thresholds are advisory labels, not a quota",
        }


DEFAULT_POLICY = RetentionPolicy()


def _same_path(left: str, right: str) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def _on_windows() -> bool:
    return os.name == "nt"


def _aliases_enabled() -> bool:
    """Windows aliasing is only valid on a case-insensitive Windows filesystem."""
    return _on_windows()


def _is_junction(path: str) -> bool:
    """Fail-closed junction check, also when ``os.path.isjunction`` is missing."""
    if not _on_windows():
        return False
    checker = getattr(os.path, "isjunction", None)
    if checker is not None:
        try:
            return bool(checker(path))
        except OSError:
            return True
    try:
        stat_result = os.lstat(path)
    except OSError:
        return True
    attributes = getattr(stat_result, "st_file_attributes", None)
    if attributes is None:
        # Cannot prove that the path is not a junction: fail closed.
        return True
    reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_point)


def _entry_is_junction(entry) -> bool:
    checker = getattr(entry, "is_junction", None)
    if checker is not None:
        try:
            return bool(checker())
        except OSError:
            return True
    return _is_junction(entry.path)


def _alias_parts(value: str) -> tuple[str, ...] | None:
    """Path components for identity comparison.

    On Windows a component is case-insensitive and ignores trailing dots/spaces.
    On case-sensitive platforms the exact component is preserved instead; equal
    spellings are never conflated there.
    """
    normalized = normalize_relative(value)
    if not normalized:
        return None
    parts = []
    for part in PurePosixPath(normalized).parts:
        if _aliases_enabled():
            part = part.rstrip(" .").casefold()
        if not part:
            return None
        parts.append(part)
    return tuple(parts)


def _alias_key(value: str) -> str | None:
    parts = _alias_parts(value)
    return "/".join(parts) if parts is not None else None


def _in_data_zone(rel: str) -> bool:
    parts = tuple(part.casefold() for part in PurePosixPath(rel).parts)
    return any(
        parts[: len(zone)] == zone and len(parts) > len(zone)
        for zone in (tuple(PurePosixPath(value).parts) for value in DATA_ZONES)
    )


def _normalize_rel(value, label: str) -> str:
    normalized = normalize_relative(value)
    if not normalized or ":" in normalized:
        raise RetentionError(f"{label} must be a workspace-relative POSIX path")
    if not _in_data_zone(normalized):
        raise RetentionError(
            f"{label} '{normalized}' is outside the canonical Data Zones ({', '.join(DATA_ZONES)})"
        )
    return normalized


def _absolute(workspace_root: str, rel: str) -> str:
    return os.path.normpath(os.path.join(workspace_root, *PurePosixPath(rel).parts))


def _guard_components(workspace_root: str, rel: str, label: str, *, allow_missing: bool = False) -> bool:
    """Check every lexical component before resolving or descending.

    A link, junction or cloud placeholder anywhere in the workspace-relative path
    is rejected before ``realpath``/``scandir``/``open`` sees the next component.
    Missing components are reported only when the caller requires the path.
    """
    current = os.path.abspath(workspace_root)
    if os.path.islink(current) or _is_junction(current) or is_cloud_placeholder(current):
        raise RetentionError(f"{label} workspace root is a link, junction or cloud placeholder")
    for part in PurePosixPath(rel).parts:
        current = os.path.join(current, part)
        if not os.path.lexists(current):
            if allow_missing:
                return False
            raise RetentionError(f"{label} is missing at component {part!r}")
        if os.path.islink(current) or _is_junction(current):
            raise RetentionError(f"{label} traverses a link or junction at component {part!r}")
        if is_cloud_placeholder(current):
            raise RetentionError(f"{label} is a cloud placeholder at component {part!r}")
    return True


def _require_local(workspace_root: str, rel: str, label: str, *, directory: bool) -> str:
    _guard_components(workspace_root, rel, label)
    absolute = _absolute(workspace_root, rel)
    expected = os.path.join(os.path.realpath(workspace_root), *PurePosixPath(rel).parts)
    if not _same_path(os.path.realpath(absolute), expected):
        raise RetentionError(f"{label} resolves through a link or outside the workspace")
    predicate = os.path.isdir if directory else os.path.isfile
    if not predicate(absolute):
        kind = "directory" if directory else "file"
        raise RetentionError(f"{label} is not an existing {kind}")
    return absolute


def _artifact_reference(entry: dict, source: str, field: str):
    value = entry.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise RetentionError(f"filemap.files[{source!r}].{field} is not a path string")
    normalized = normalize_relative(value)
    if not normalized or ":" in normalized:
        raise RetentionError(f"filemap.files[{source!r}].{field} is not a safe relative path")
    return normalized


def _active_references(document, version_root_rel: str) -> set[str]:
    """Return the folded version directories claimed by this one filemap.

    Only paths below this output's ``_versions`` root count; references to a
    different output remain foreign and unknown.
    """
    references: set[str] = set()
    root_parts = _alias_parts(version_root_rel)
    if not root_parts:
        return references
    for source, entry in document.data["files"].items():
        if not isinstance(entry, dict):
            raise RetentionError(f"filemap.files[{source!r}] is not an object")
        paths = [_artifact_reference(entry, source, "markdown_mirror")]
        derivative = entry.get("derivative")
        if derivative is not None:
            if not isinstance(derivative, dict):
                raise RetentionError(f"filemap.files[{source!r}].derivative is not an object")
            paths.append(_artifact_reference(derivative, source, "path"))
        # A target is stored as <output_dir>/_versions/<source-key>/<run>/<file>.
        # Only this exact layout can name a version below this output's root.
        for path in paths:
            if path is None:
                continue
            parts = _alias_parts(path)
            if not parts or parts[: len(root_parts)] != root_parts:
                continue
            relative = parts[len(root_parts):]
            if len(relative) < 3 or not _SOURCE_KEY.fullmatch(relative[0].casefold()):
                continue
            key = _alias_key(f"{version_root_rel}/{relative[0]}/{relative[1]}")
            if key is not None:
                references.add(key)
    return references


def _lstat(path: str):
    try:
        return os.stat(path, follow_symlinks=False)
    except OSError:
        return None


def _tree_usage(path: str, real_root: str):
    """Return usage flags without following links or hydrating placeholders."""
    total = 0
    files = 0
    contains_links = False
    unexpected = False
    contains_placeholders = False
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink() or _entry_is_junction(entry):
                            contains_links = True
                            continue
                        if is_cloud_placeholder(entry.path):
                            contains_placeholders = True
                            continue
                        real = os.path.realpath(entry.path)
                        if not _same_path(real, os.path.join(real_root, os.path.relpath(entry.path, path))):
                            contains_links = True
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                            files += 1
                        else:
                            unexpected = True
                    except OSError:
                        unexpected = True
        except OSError:
            unexpected = True
    return total, files, contains_links, unexpected, contains_placeholders


def _record(rel: str, source_key: str | None, mtime: float, size: int, files: int,
            *, active: bool, unsafe: bool, unexpected: bool, placeholders: bool,
            now: datetime) -> dict:
    age_days = max(0.0, (now.timestamp() - mtime) / _SECONDS_PER_DAY)
    return {
        "path": rel,
        "source_key": source_key,
        "classification": "active" if active else "retained",
        "reasons": [],
        "active": active,
        "age_days": round(age_days, 3),
        "mtime": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
        "size_bytes": size,
        "file_count": files,
        "unsafe": unsafe,
        "unexpected": unexpected,
        "placeholders": placeholders,
        "potentially_active": False,
        "action": "none",
        "foreign_reference_risk": "unknown",
        "safe_to_delete": False,
        "safe_to_move": False,
        "quarantine": {"logical": False, "physical": False},
        "_mtime_epoch": mtime,
    }


def _unrecognized(rel: str, path: str, now: datetime, *, reason: str = "unexpected_layout") -> dict:
    stat_result = _lstat(path)
    mtime = stat_result.st_mtime if stat_result is not None else now.timestamp()
    return {
        "path": rel,
        "source_key": None,
        "classification": "quarantine_candidate",
        "reasons": [reason],
        "active": False,
        "age_days": round(max(0.0, (now.timestamp() - mtime) / _SECONDS_PER_DAY), 3),
        "mtime": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
        "size_bytes": None,
        "file_count": None,
        "unsafe": True,
        "unexpected": True,
        "placeholders": reason.startswith("cloud_placeholder"),
        "potentially_active": False,
        "action": "none",
        "foreign_reference_risk": "unknown",
        "safe_to_delete": False,
        "safe_to_move": False,
        "quarantine": {"logical": True, "physical": False},
    }


def _active_under(active: set[str], parent_alias: str) -> list[str]:
    prefix = parent_alias.rstrip("/") + "/"
    return sorted(reference for reference in active if reference.startswith(prefix))


def _unresolved_parent(rel: str, path: str, now: datetime, *, reason: str,
                       active_references: list[str]) -> dict:
    """Protect an unreadable source-key parent that contains an active reference."""
    record = _unrecognized(rel, path, now, reason=reason)
    record["classification"] = "unresolved"
    record["potentially_active"] = True
    record["unresolved_references"] = active_references
    record["quarantine"] = {"logical": False, "physical": False}
    return record


def _apply_policy(record: dict, rank: int, policy: RetentionPolicy, now_timestamp: float) -> None:
    if record["active"]:
        record["classification"] = "active"
        record["reasons"] = ["filemap_reference"]
        return
    if record["unsafe"]:
        record["classification"] = "quarantine_candidate"
        record["reasons"] = [
            "cloud_placeholder_version_directory" if record["placeholders"]
            else "unsafe_version_directory"
        ]
        record["quarantine"] = {"logical": True, "physical": False}
        return
    if record["unexpected"]:
        record["classification"] = "quarantine_candidate"
        record["reasons"] = ["unexpected_version_contents"]
        record["quarantine"] = {"logical": True, "physical": False}
        return

    age = max(0.0, (now_timestamp - record["_mtime_epoch"]) / _SECONDS_PER_DAY)
    over_absolute = age > policy.quarantine_age_days
    within_count = rank < policy.retained_inactive_versions
    within_grace = age <= policy.retained_inactive_age_days

    if over_absolute:
        record["classification"] = "quarantine_candidate"
        record["reasons"] = ["absolute_age_exceeded"]
        record["quarantine"] = {"logical": True, "physical": False}
    elif within_count:
        record["classification"] = "retained"
        record["reasons"] = ["within_retained_count"]
    elif within_grace:
        record["classification"] = "retained"
        record["reasons"] = ["within_retention_grace_age"]
    else:
        record["classification"] = "quarantine_candidate"
        record["reasons"] = ["count_exceeded", "retention_grace_age_exceeded"]
        record["quarantine"] = {"logical": True, "physical": False}


def _journal_summary(output_abs: str, output_rel: str) -> dict:
    path = os.path.join(output_abs, JOURNAL_DIR)
    rel = f"{output_rel}/{JOURNAL_DIR}"
    if not os.path.lexists(path):
        return {"path": rel, "present": False, "classification": "journal_not_managed",
                "entries": 0, "bytes": 0, "unsafe": False}
    expected = os.path.join(os.path.realpath(output_abs), JOURNAL_DIR)
    if (
        os.path.islink(path)
        or _is_junction(path)
        or is_cloud_placeholder(path)
        or not os.path.isdir(path)
        or not _same_path(os.path.realpath(path), expected)
    ):
        return {"path": rel, "present": True, "classification": "journal_not_managed",
                "entries": None, "bytes": None, "unsafe": True}
    entries = 0
    total = 0
    unsafe = False
    try:
        with os.scandir(path) as iterator:
            for entry in iterator:
                try:
                    if entry.is_symlink() or _entry_is_junction(entry):
                        unsafe = True
                        continue
                    entries += 1
                    if entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    unsafe = True
    except OSError:
        unsafe = True
    return {"path": rel, "present": True, "classification": "journal_not_managed",
            "entries": entries, "bytes": total, "unsafe": unsafe}


CATALOG_SOURCES = (
    ("memory/references/projects/projects.json", "project"),
    ("memory/references/topics/topics.json", "topic"),
)


def _catalog_storages(workspace_root: str):
    """Collect effective cloud_sync storages without touching unknown paths."""
    storages = []
    errors = []
    for catalog_rel, scope in CATALOG_SOURCES:
        catalog_abs = _absolute(workspace_root, catalog_rel)
        try:
            if not _guard_components(workspace_root, catalog_rel, catalog_rel, allow_missing=True):
                continue
        except RetentionError as exc:
            errors.append(f"{catalog_rel}: {exc}")
            continue
        try:
            with open(catalog_abs, "rb") as handle:
                document = json.loads(handle.read().decode("utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            errors.append(f"{catalog_rel}: catalog is unreadable: {exc}")
            continue
        if not isinstance(document, list):
            errors.append(f"{catalog_rel}: catalog is not a list")
            continue
        for item in document:
            if not isinstance(item, dict):
                errors.append(f"{catalog_rel}: catalog item is not an object")
                continue
            owner_id = item.get("id")
            if not isinstance(owner_id, str) or not owner_id.strip():
                errors.append(f"{catalog_rel}: catalog item has no usable id")
                continue
            _collect_catalog_item(storages, errors, scope, item, owner_id)
    return storages, errors


def _catalog_base(scope: str, owner_id: str) -> str:
    return f"memory/cloud/{'topics' if scope == 'topic' else 'projects'}/{owner_id}"


def _append_catalog_storage(storages, errors, *, scope, owner_id, subtopic_id,
                            storage_id, output_json, output_dir):
    if not isinstance(storage_id, str) or not storage_id.strip():
        errors.append(f"{scope}:{owner_id}: storage id is invalid")
        return
    if not isinstance(output_json, str) or not output_json.strip():
        errors.append(f"{scope}:{owner_id}:{storage_id}: output_json is invalid")
        return
    if not isinstance(output_dir, str) or not output_dir.strip():
        errors.append(f"{scope}:{owner_id}:{storage_id}: output_dir is invalid")
        return
    storages.append({
        "scope": scope,
        "owner_id": owner_id,
        "subtopic_id": subtopic_id,
        "storage_id": storage_id,
        "output_json": output_json,
        "output_dir": output_dir,
    })


def _collect_catalog_item(storages, errors, scope, item, owner_id):
    """Apply the established cloud_sync aliases and synthesized defaults."""
    base = _catalog_base(scope, owner_id)
    cloud_sync = item.get("cloud_sync")
    if not cloud_sync:
        _append_catalog_storage(
            storages, errors, scope=scope, owner_id=owner_id, subtopic_id=None,
            storage_id="default", output_json=f"{base}/filemap.json", output_dir=base,
        )
    elif not isinstance(cloud_sync, dict):
        errors.append(f"{scope}:{owner_id}: cloud_sync is not an object")
    else:
        for storage_id, config in cloud_sync.items():
            if not isinstance(config, dict):
                errors.append(f"{scope}:{owner_id}:{storage_id}: storage config is not an object")
                continue
            suffix = f"-{storage_id}" if storage_id != "default" else ""
            output_json = config.get("output_json") or config.get("filemap_json")
            output_dir = config.get("output_dir") or config.get("cloud_mirror_dir")
            _append_catalog_storage(
                storages, errors, scope=scope, owner_id=owner_id, subtopic_id=None,
                storage_id=storage_id,
                output_json=output_json or f"{base}/filemap{suffix}.json",
                output_dir=output_dir or (base + (f"/{storage_id}" if storage_id != "default" else "")),
            )

    subtopics = item.get("subtopics")
    if subtopics is None:
        return
    if not isinstance(subtopics, list):
        errors.append(f"{scope}:{owner_id}: subtopics is not a list")
        return
    for subtopic in subtopics:
        if not isinstance(subtopic, dict):
            errors.append(f"{scope}:{owner_id}: subtopic is not an object")
            continue
        subtopic_id = subtopic.get("id")
        if not isinstance(subtopic_id, str) or not subtopic_id.strip():
            errors.append(f"{scope}:{owner_id}: subtopic has no usable id")
            continue
        sub_sync = subtopic.get("cloud_sync")
        if not sub_sync:
            continue
        if not isinstance(sub_sync, dict):
            errors.append(f"{scope}:{owner_id}:{subtopic_id}: cloud_sync is not an object")
            continue
        for storage_id, config in sub_sync.items():
            if not isinstance(config, dict):
                errors.append(
                    f"{scope}:{owner_id}:{subtopic_id}:{storage_id}: storage config is not an object"
                )
                continue
            _append_catalog_storage(
                storages, errors, scope=scope, owner_id=owner_id, subtopic_id=subtopic_id,
                storage_id=storage_id,
                output_json=config.get("output_json") or config.get("filemap_json"),
                output_dir=config.get("output_dir") or config.get("cloud_mirror_dir"),
            )


def _configured_identity(workspace_root: str, filemap_rel: str, output_rel: str) -> dict:
    """Bind the audit to a configured catalog storage, or mark it unverified."""
    storages, errors = _catalog_storages(workspace_root)
    filemap_key = _alias_key(filemap_rel)
    output_key = _alias_key(output_rel)
    matches = [
        storage for storage in storages
        if _alias_key(storage.get("output_json")) == filemap_key
        and _alias_key(storage.get("output_dir")) == output_key
    ]
    if errors:
        # A read/parse error in any catalog means uniqueness cannot be proven.
        status = "unverified"
        target = None
    elif len(matches) == 1:
        status = "verified"
        target = matches[0]
    elif len(matches) > 1:
        status = "ambiguous"
        target = None
        errors.append("catalog has multiple storages for this filemap/output pair")
    else:
        status = "unverified"
        target = None
    return {
        "status": status,
        "classification_authoritative": status == "verified",
        "source": "catalog" if matches else "none",
        "match_count": len(matches),
        "target": (
            {
                "scope": target["scope"],
                "owner_id": target["owner_id"],
                "subtopic_id": target["subtopic_id"],
                "storage_id": target["storage_id"],
                "output_json": target["output_json"],
                "output_dir": target["output_dir"],
            }
            if target is not None else None
        ),
        "errors": errors,
    }


def classify_versions(workspace_root, output_dir, filemap_path, *, now=None,
                      policy: RetentionPolicy | None = None) -> dict:
    """Classify the version root of one output directory without writing anything.

    The target filemap is the only reference source for ``active``.  Other
    filemaps, consumers and manual references are deliberately not discovered;
    all records therefore report an unknown foreign-reference risk.  Nothing in
    the returned report is an instruction to delete or move a path.
    """
    policy = policy or DEFAULT_POLICY
    root = os.path.abspath(workspace_root)
    output_rel = _normalize_rel(output_dir, "output_dir")
    filemap_rel = _normalize_rel(filemap_path, "filemap")

    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    output_abs = _require_local(root, output_rel, "output_dir", directory=True)
    filemap_abs = _require_local(root, filemap_rel, "filemap", directory=False)
    try:
        with open(filemap_abs, "rb") as handle:
            text = handle.read().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RetentionError(f"filemap is unreadable: {exc}") from exc
    try:
        document = parse_filemap(text)
    except FilemapError as exc:
        raise RetentionError(f"filemap is invalid: {exc}") from exc
    declared_output = document.data.get("output_dir")
    if declared_output is None:
        raise RetentionError(
            "filemap has no output_dir; it cannot be proven to belong to the audited output"
        )
    if _alias_key(declared_output) != _alias_key(output_rel):
        raise RetentionError("filemap.output_dir does not match the audited output directory")

    identity = _configured_identity(root, filemap_rel, output_rel)
    version_rel = f"{output_rel}/{VERSION_DIR}"
    active = _active_references(document, version_rel)
    version_abs = _absolute(root, version_rel)
    expected_root = os.path.join(os.path.realpath(output_abs), VERSION_DIR)
    version_root_present = os.path.lexists(version_abs)
    if version_root_present and os.path.islink(version_abs):
        raise RetentionError("the version root resolves through a link or outside output_dir")
    if version_root_present and is_cloud_placeholder(version_abs):
        raise RetentionError("the version root is a cloud placeholder; refusing to hydrate it")
    if version_root_present and not _same_path(os.path.realpath(version_abs), expected_root):
        raise RetentionError("the version root resolves through a link or outside output_dir")
    if version_root_present and not os.path.isdir(version_abs):
        raise RetentionError("the version root is not a directory")

    versions: list[dict] = []
    unrecognized: list[dict] = []
    matched_active: set[str] = set()

    if version_root_present:
        try:
            top_entries = list(os.scandir(version_abs))
        except OSError as exc:
            raise RetentionError(f"cannot read the version root: {exc}") from exc
        for key_entry in top_entries:
            key = key_entry.name
            key_valid = bool(_SOURCE_KEY.fullmatch(key.casefold()))
            key_rel = f"{version_rel}/{key}"
            parent_alias = _alias_key(f"{version_rel}/{key}") or f"{version_rel}/{key}"
            key_active = _active_under(active, parent_alias)

            def key_entry_unreadable(reason):
                if key_active:
                    unrecognized.append(_unresolved_parent(
                        key_rel, key_entry.path, now, reason=reason,
                        active_references=key_active,
                    ))
                else:
                    unrecognized.append(_unrecognized(key_rel, key_entry.path, now, reason=reason))

            if key_entry.is_symlink() or _entry_is_junction(key_entry):
                key_entry_unreadable("unsafe_source_directory")
                continue
            if is_cloud_placeholder(key_entry.path):
                key_entry_unreadable("cloud_placeholder_source_directory")
                continue
            if not key_entry.is_dir(follow_symlinks=False) or not key_valid:
                key_entry_unreadable("unexpected_layout")
                continue
            expected_key = os.path.join(os.path.realpath(version_abs), key)
            if not _same_path(os.path.realpath(key_entry.path), expected_key):
                key_entry_unreadable("unsafe_source_directory")
                continue
            try:
                run_entries = list(os.scandir(key_entry.path))
            except OSError as exc:
                raise RetentionError(f"cannot read source-key directory '{key_rel}': {exc}") from exc
            for run_entry in run_entries:
                run_rel = f"{key_rel}/{run_entry.name}"
                folded = _alias_key(run_rel)
                is_active = folded is not None and folded in active
                stat_result = _lstat(run_entry.path)
                stat_failed = stat_result is None
                mtime = stat_result.st_mtime if stat_result is not None else now.timestamp()
                if run_entry.is_symlink() or _entry_is_junction(run_entry):
                    if is_active:
                        matched_active.add(folded)
                    record = _record(run_rel, key, mtime, 0, 0, active=is_active,
                                     unsafe=True, unexpected=False, placeholders=False, now=now)
                    versions.append(record)
                    continue
                if not run_entry.is_dir(follow_symlinks=False):
                    unrecognized.append(_unrecognized(run_rel, run_entry.path, now))
                    continue
                if is_cloud_placeholder(run_entry.path):
                    if is_active:
                        matched_active.add(folded)
                    record = _record(run_rel, key, mtime, 0, 0, active=is_active,
                                     unsafe=True, unexpected=False, placeholders=True, now=now)
                    versions.append(record)
                    continue
                expected_run = os.path.join(expected_key, run_entry.name)
                if not _same_path(os.path.realpath(run_entry.path), expected_run):
                    if is_active:
                        matched_active.add(folded)
                    record = _record(run_rel, key, mtime, 0, 0, active=is_active,
                                     unsafe=True, unexpected=False, placeholders=False, now=now)
                    versions.append(record)
                    continue
                size, files, contains_links, unexpected, placeholders = _tree_usage(
                    run_entry.path, os.path.realpath(run_entry.path),
                )
                if is_active:
                    matched_active.add(folded)
                record = _record(
                    run_rel, key, mtime, size, files, active=is_active,
                    unsafe=stat_failed or contains_links or placeholders,
                    unexpected=unexpected, placeholders=placeholders, now=now,
                )
                versions.append(record)

    grouped: dict[str, list[dict]] = {}
    for record in versions:
        grouped.setdefault(record["source_key"], []).append(record)
    for group in grouped.values():
        group.sort(key=lambda item: (item["_mtime_epoch"], item["path"]), reverse=True)
        rank = 0
        for record in group:
            _apply_policy(record, rank, policy, now.timestamp())
            if not record["active"]:
                rank += 1

    all_entries = versions + unrecognized
    for record in all_entries:
        record.pop("_mtime_epoch", None)
        record["authoritative"] = identity["classification_authoritative"]
    counts = {"active": 0, "retained": 0, "quarantine_candidate": 0, "unresolved": 0}
    bytes_by_class = {"active": 0, "retained": 0, "quarantine_candidate": 0, "unresolved": 0}
    for record in all_entries:
        counts[record["classification"]] += 1
        if record["size_bytes"] is not None:
            bytes_by_class[record["classification"]] += record["size_bytes"]

    quarantine_abs = os.path.join(output_abs, QUARANTINE_DIR)
    return {
        "policy": policy.as_dict(),
        "generated_at": now.isoformat(),
        "output_dir": output_rel,
        "filemap": filemap_rel,
        "version_root": version_rel,
        "version_root_present": version_root_present,
        "identity": identity,
        "classification_authoritative": identity["classification_authoritative"],
        "quarantine_area": f"{output_rel}/{QUARANTINE_DIR}",
        "reserved_area_present": os.path.lexists(quarantine_abs),
        "journals": _journal_summary(output_abs, output_rel),
        "references": {
            "active_version_directories": len(active),
            "matched": len(matched_active),
            "unresolved": sorted(active - matched_active),
        },
        "counts": counts,
        "bytes": {
            "known_version_bytes": sum(bytes_by_class.values()),
            "by_classification": bytes_by_class,
            "unrecognized": None,
        },
        "entries": all_entries,
        "advisory": (
            (
                "Classification only; the filemap/output identity is not verified "
                "by a catalog entry, so candidate labels are non-authoritative. "
                if not identity["classification_authoritative"] else
                "Classification only. "
            )
            + "No entry is safe to delete or move: foreign filemaps and consumers "
            "are not enumerable. The thresholds do not bound disk usage; only an "
            "operator can act, outside this contract."
        ),
    }
