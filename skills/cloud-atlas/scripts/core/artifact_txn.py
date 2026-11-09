"""Best-effort rollback of the artifacts one single-file run may touch (Issue #22).

Only paths registered before the first mutation are restored.  Backups live in a
private temporary directory outside the workspace and are removed on commit and
on rollback.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from typing import Callable


class ArtifactTransaction:
    def __init__(self, safe_path: Callable[[str], str] = lambda value: value):
        self._safe = safe_path
        self._backup_root: str | None = None
        self._files: dict[str, str | None] = {}
        self._listings: dict[str, set[str]] = {}
        self._created_roots: list[str] = []
        self._counter = 0

    def _backups(self) -> str:
        if self._backup_root is None:
            self._backup_root = tempfile.mkdtemp(prefix="cloud-atlas-only-file-")
        return self._backup_root

    def track_file(self, path: str) -> None:
        """Remember the current content (or absence) of one file."""
        absolute = os.path.abspath(path)
        if absolute in self._files:
            return
        safe = self._safe(absolute)
        if os.path.isdir(safe):
            raise IsADirectoryError(f"artifact path is a directory: {absolute}")
        if os.path.exists(safe):
            self._counter += 1
            backup = os.path.join(self._backups(), f"{self._counter}.bak")
            shutil.copy2(safe, backup)
            self._files[absolute] = backup
        else:
            self._files[absolute] = None

    def track_directory(self, directory: str, boundary: str) -> None:
        """Remember a directory listing, or the topmost directory we would create."""
        absolute = os.path.abspath(directory)
        boundary = os.path.abspath(boundary)
        if absolute in self._listings or absolute in self._created_roots:
            return
        missing = []
        current = absolute
        while not os.path.exists(self._safe(current)):
            parent = os.path.dirname(current)
            if current == boundary or parent == current:
                break
            missing.append(current)
            current = parent
        if missing:
            self._created_roots.append(missing[-1])
        else:
            self._listings[absolute] = set(os.listdir(self._safe(absolute)))

    def rollback(self) -> list[str]:
        """Restore tracked state; return human-readable errors (empty on success)."""
        errors: list[str] = []
        for path, backup in self._files.items():
            try:
                if backup is None:
                    if os.path.lexists(self._safe(path)):
                        os.unlink(self._safe(path))
                else:
                    directory, name = os.path.split(path)
                    sibling = os.path.join(directory, f".{name}.rollback.tmp")
                    try:
                        shutil.copy2(backup, self._safe(sibling))
                        os.replace(self._safe(sibling), self._safe(path))
                    finally:
                        if os.path.lexists(self._safe(sibling)):
                            os.unlink(self._safe(sibling))
            except OSError as exc:
                errors.append(f"{path}: {exc}")
        for directory, before in self._listings.items():
            try:
                for name in os.listdir(self._safe(directory)):
                    if name in before:
                        continue
                    child = os.path.join(directory, name)
                    if os.path.isdir(self._safe(child)) and not os.path.islink(self._safe(child)):
                        shutil.rmtree(self._safe(child))
                    else:
                        os.unlink(self._safe(child))
            except OSError as exc:
                errors.append(f"{directory}: {exc}")
        for root in self._created_roots:
            try:
                if os.path.lexists(self._safe(root)):
                    shutil.rmtree(self._safe(root))
            except OSError as exc:
                errors.append(f"{root}: {exc}")
        self._discard()
        return errors

    def commit(self) -> None:
        self._discard()

    def _discard(self) -> None:
        if self._backup_root is not None:
            shutil.rmtree(self._backup_root, ignore_errors=True)
            self._backup_root = None
        self._files = {}
        self._listings = {}
        self._created_roots = []
