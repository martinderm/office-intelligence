"""Literal storage-local directory exclusions shared by both Cloud Atlas scans."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath, PureWindowsPath
from collections.abc import Iterator


def normalize_exclude_dirs(value: object) -> list[str]:
    """Canonicalize separators/dot segments; reject roots, drives and traversal.

    Comparisons are case-insensitive on every platform, conservatively matching
    Windows cloud workspaces. Redundant nested paths collapse into their parent.
    """
    if not isinstance(value, list):
        raise ValueError("exclude_dirs must be a list of relative directory paths")
    paths: dict[str, str] = {}
    for item in value:
        if not isinstance(item, str) or not item or "\x00" in item or ":" in item:
            raise ValueError("exclude_dirs entries must be non-empty relative directory paths")
        raw = item.replace("\\", "/")
        path = PurePosixPath(raw)
        if path.is_absolute() or PureWindowsPath(raw).drive or ".." in path.parts or not path.parts:
            raise ValueError("exclude_dirs must not contain absolute paths, roots or parent traversal")
        normalized = path.as_posix()
        paths.setdefault(normalized.casefold(), normalized)
    effective: list[str] = []
    for key in sorted(paths):
        if not any(key.startswith(parent.casefold() + "/") for parent in effective):
            effective.append(paths[key])
    return effective


def source_in_scope(source: str, scan_dir: str, exclude_dirs: list[str], *, directory: bool = False) -> bool:
    """Check directory boundaries, never string prefixes or glob expressions."""
    source_path = PurePosixPath(source.replace("\\", "/"))
    scan_path = PurePosixPath(scan_dir.replace("\\", "/"))
    parts = tuple(part.casefold() for part in source_path.parts)
    root = tuple(part.casefold() for part in scan_path.parts)
    if ".." in source_path.parts or parts[:len(root)] != root or len(parts) <= len(root):
        return False
    relative = parts[len(root):]
    return not any((directory or len(relative) > len(excluded)) and relative[:len(excluded)] == excluded for excluded in (
        tuple(part.casefold() for part in PurePosixPath(path).parts) for path in exclude_dirs
    ))


def walk_scoped(scan_root: str, exclude_dirs: list[str]) -> Iterator[tuple[str, list[str], list[str]]]:
    """Prune excluded directory entries before os.walk can descend into them."""
    for root, dirs, files in os.walk(scan_root):
        relative = Path(root).relative_to(scan_root)
        dirs[:] = [name for name in dirs if source_in_scope(
            (PurePosixPath("scan") / relative.as_posix() / name).as_posix(), "scan", exclude_dirs, directory=True
        )]
        yield root, dirs, files


def retained_artifact_paths(
    workspace_root: str, output_dir: str, existing_files: dict,
    scan_dir: str, exclude_dirs: list[str],
) -> set[str]:
    """Reserve artifacts without positive in-scope ownership, even on later runs.

    The active filemap drops excluded entries. Therefore reservation must inspect
    disk on every run rather than relying on those entries staying in the map.
    Case-folded workspace paths also protect collisions on Windows.
    """
    if not exclude_dirs:
        return set()
    owned: set[str] = set()
    for source, info in existing_files.items():
        if not isinstance(info, dict) or not source_in_scope(source, scan_dir, exclude_dirs):
            continue
        paths = [info.get("markdown_mirror")]
        if isinstance(info.get("derivative"), dict):
            paths.append(info["derivative"].get("path"))
        owned.update(path.replace("\\", "/").casefold() for path in paths if isinstance(path, str))
    reserved: set[str] = set()
    for root, _dirs, files in os.walk(Path(workspace_root) / output_dir):
        for name in files:
            path = (Path(root) / name).relative_to(workspace_root).as_posix().casefold()
            if path not in owned:
                reserved.add(path)
    return reserved


def assert_unreserved_targets(targets: list[str], reserved: set[str]) -> None:
    """Fail before writes or inventory adoption can claim a retained artifact."""
    for target in targets:
        if target.replace("\\", "/").casefold() in reserved:
            raise ValueError(f"Planned target collides with a retained or unowned artifact: {target}")
