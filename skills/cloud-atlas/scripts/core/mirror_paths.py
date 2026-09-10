"""Deterministic, fail-closed planning for Cloud Atlas Markdown mirrors."""

from pathlib import PurePosixPath


MIRROR_SOURCE_EXTENSIONS = frozenset({".pdf", ".docx", ".xlsx", ".pptx", ".doc"})


def normalize_supported_extensions(extensions=None):
    """Return normalized lowercase extensions, defaulting to Cloud Atlas defaults."""
    if extensions is None:
        return MIRROR_SOURCE_EXTENSIONS
    values = extensions.split(",") if isinstance(extensions, str) else extensions
    try:
        normalized = {
            (value.strip().lower() if value.strip().startswith(".") else f".{value.strip().lower()}")
            for value in values
            if isinstance(value, str) and value.strip()
        }
    except TypeError as exc:
        raise ValueError("supported_extensions must be a string or iterable of strings") from exc
    return frozenset(normalized)


def normalize_workspace_relative_path(path_value):
    """Return a safe, normalized workspace-relative POSIX path or ``None``."""
    if not isinstance(path_value, str) or not path_value.strip():
        return None
    raw = path_value.strip().replace("\\", "/")
    if raw.startswith("/") or raw.startswith("//") or (len(raw) >= 3 and raw[1:3] == ":/"):
        return None
    normalized = PurePosixPath(raw)
    if ".." in normalized.parts:
        return None
    return normalized.as_posix()


def path_is_within(path_value, parent_value):
    """Return whether a safe relative path is within its declared parent."""
    path_rel = normalize_workspace_relative_path(path_value)
    parent_rel = normalize_workspace_relative_path(parent_value)
    if not path_rel or not parent_rel:
        return False
    try:
        PurePosixPath(path_rel).relative_to(PurePosixPath(parent_rel))
        return True
    except ValueError:
        return False


def legacy_canonical_mirror_path(source_path, scan_dir, output_dir, supported_extensions=None):
    """Return the pre-B2d7 stem-based mirror path for a supported source."""
    source_rel = normalize_workspace_relative_path(source_path)
    scan_rel = normalize_workspace_relative_path(scan_dir)
    output_rel = normalize_workspace_relative_path(output_dir)
    if not source_rel or not scan_rel or not output_rel:
        return None
    try:
        relative_source = PurePosixPath(source_rel).relative_to(PurePosixPath(scan_rel))
    except ValueError:
        return None
    if relative_source.suffix.lower() not in normalize_supported_extensions(supported_extensions):
        return None
    return (PurePosixPath(output_rel) / relative_source.with_suffix(".md")).as_posix()


def explicit_custom_mirror_path(source_path, scan_dir, output_dir, mirror_path):
    """Return an existing custom path, never a historical canonical path."""
    mirror_rel = normalize_workspace_relative_path(mirror_path)
    legacy = legacy_canonical_mirror_path(source_path, scan_dir, output_dir)
    if not mirror_rel or not legacy or not path_is_within(mirror_rel, output_dir):
        return None
    return None if mirror_rel.casefold() == legacy.casefold() else mirror_rel


def plan_markdown_mirrors(
    source_paths, scan_dir, output_dir, existing_mirrors=None,
    supported_extensions=None,
):
    """Plan one unique Markdown mirror per supported source.

    A group which would have shared the historical ``stem.md`` keeps no preferred
    member: each source uses ``original-name.ext.md``.  Target comparison uses
    ``casefold`` on every platform, the conservative rule needed for Windows
    workspaces.  Existing non-canonical custom paths remain only if they are in
    the output zone and do not collide with any planned target.
    """
    scan_rel = normalize_workspace_relative_path(scan_dir)
    output_rel = normalize_workspace_relative_path(output_dir)
    if not scan_rel or not output_rel:
        raise ValueError("scan_dir and output_dir must be safe workspace-relative paths")

    existing_mirrors = existing_mirrors or {}
    records = []
    for source_path in source_paths:
        source_rel = normalize_workspace_relative_path(source_path)
        legacy = legacy_canonical_mirror_path(
            source_rel, scan_rel, output_rel, supported_extensions,
        )
        if source_rel and legacy:
            records.append((source_rel, legacy))
    records.sort(key=lambda record: (record[0].casefold(), record[0]))

    by_legacy_target = {}
    for source_rel, legacy in records:
        by_legacy_target.setdefault(legacy.casefold(), []).append((source_rel, legacy))

    planned = {}
    ambiguous_sources = set()
    for group in by_legacy_target.values():
        if len(group) == 1:
            source_rel, legacy = group[0]
            planned[source_rel] = legacy
            continue
        for source_rel, _legacy in group:
            relative_source = PurePosixPath(source_rel).relative_to(PurePosixPath(scan_rel))
            planned[source_rel] = (
                PurePosixPath(output_rel)
                / relative_source.parent
                / f"{relative_source.name}.md"
            ).as_posix()
            ambiguous_sources.add(source_rel)

    # A custom path is deliberately never an escape hatch for a detected
    # collision group: retaining its old stem path could resurrect ambiguity.
    for source_rel, legacy in records:
        if source_rel in ambiguous_sources:
            continue
        existing = normalize_workspace_relative_path(existing_mirrors.get(source_rel))
        if not existing or existing.casefold() == legacy.casefold() or not path_is_within(existing, output_rel):
            continue
        occupied = {
            target.casefold()
            for other_source, target in planned.items()
            if other_source != source_rel
        }
        if existing.casefold() not in occupied:
            planned[source_rel] = existing

    seen_targets = {}
    for source_rel, target in planned.items():
        if not path_is_within(target, output_rel):
            raise ValueError(f"planned markdown mirror is outside output_dir: {target}")
        folded = target.casefold()
        previous = seen_targets.get(folded)
        if previous is not None:
            raise ValueError(
                "planned markdown mirrors are not unique under case-insensitive comparison: "
                f"{previous} and {source_rel} -> {target}"
            )
        seen_targets[folded] = source_rel
    return planned
