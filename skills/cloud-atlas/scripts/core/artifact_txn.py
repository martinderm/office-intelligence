"""Best-effort, in-process rollback of the artifacts one single-file run may touch.

This is deliberately *not* a crash-safe transaction: there is no journal, so a hard
kill, power loss or a failed restore can leave a mixed state.  What it does give:

* only paths registered before the first mutation are restored or removed;
* the only other things removed are the run's own temporary files, recognised by
  name pattern, never "everything new in the directory";
* directories the run created are removed only when they are empty again;
* backups live in a private temporary directory outside the workspace and are
  kept (and reported) whenever a restore fails, so the previous state stays
  recoverable by hand.
"""

from __future__ import annotations

import fnmatch
import glob
import os
import shutil
import tempfile
from typing import Callable

# Temporary names the conversion helpers create next to their targets.
_STAGE_PATTERN = ".ocr-stage-*.pdf"


class ArtifactTransaction:
    def __init__(self, safe_path: Callable[[str], str] = lambda value: value):
        self._safe = safe_path
        self._backup_root: str | None = None
        self._files: dict[str, str | None] = {}
        self._listings: dict[str, set[str]] = {}
        self._created_dirs: list[str] = []
        self._counter = 0
        self.recovery_dir: str | None = None
        self.left_behind: list[str] = []

    def _backups(self) -> str:
        if self._backup_root is None:
            self._backup_root = tempfile.mkdtemp(prefix="cloud-atlas-only-file-")
        return self._backup_root

    def track_file(self, path: str) -> None:
        """Remember the current content (or absence) of one regular file."""
        absolute = os.path.abspath(path)
        if absolute in self._files:
            return
        safe = self._safe(absolute)
        if os.path.islink(safe):
            raise OSError(f"artifact path is a link; refusing to track it: {absolute}")
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
        """Remember an existing directory's listing, or the directories we would create."""
        absolute = os.path.abspath(directory)
        boundary = os.path.abspath(boundary)
        if absolute in self._listings or absolute in self._created_dirs:
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
            for created in missing:
                if created not in self._created_dirs:
                    self._created_dirs.append(created)
        else:
            self._listings[absolute] = set(os.listdir(self._safe(absolute)))

    def _own_temporary(self, directory: str, name: str) -> bool:
        if fnmatch.fnmatchcase(name, _STAGE_PATTERN):
            return True
        for tracked in self._files:
            if os.path.dirname(tracked) != directory:
                continue
            base = os.path.basename(tracked)
            if fnmatch.fnmatchcase(name, f".{glob.escape(base)}.*.tmp"):
                return True
        return False

    def _restore(self, path: str, backup: str) -> None:
        directory, name = os.path.split(path)
        fd, temporary = tempfile.mkstemp(
            prefix=f".{name}.rollback.", suffix=".tmp", dir=self._safe(directory),
        )
        os.close(fd)
        try:
            shutil.copyfile(backup, temporary)
            shutil.copystat(backup, temporary)
            os.replace(temporary, self._safe(path))
        finally:
            if os.path.lexists(temporary):
                os.unlink(temporary)

    def rollback(self) -> list[str]:
        """Restore tracked state; return human-readable errors (empty on success)."""
        errors: list[str] = []
        failed_backups: set[str] = set()
        for path, backup in self._files.items():
            try:
                if backup is None:
                    if os.path.lexists(self._safe(path)):
                        os.unlink(self._safe(path))
                else:
                    self._restore(path, backup)
            except OSError as exc:
                errors.append(f"{path}: {exc}")
                if backup is not None:
                    failed_backups.add(backup)
        for directory, before in self._listings.items():
            try:
                for name in os.listdir(self._safe(directory)):
                    if name in before or not self._own_temporary(directory, name):
                        continue
                    child = self._safe(os.path.join(directory, name))
                    if os.path.isfile(child) or os.path.islink(child):
                        os.unlink(child)
            except OSError as exc:
                errors.append(f"{directory}: {exc}")
        # Created directories: temporary files of ours first, then only empty ones.
        for directory in sorted(self._created_dirs, key=len, reverse=True):
            try:
                if not os.path.isdir(self._safe(directory)):
                    continue
                for name in os.listdir(self._safe(directory)):
                    if self._own_temporary(directory, name):
                        child = self._safe(os.path.join(directory, name))
                        if os.path.isfile(child) or os.path.islink(child):
                            os.unlink(child)
                if os.listdir(self._safe(directory)):
                    self.left_behind.append(directory)
                else:
                    os.rmdir(self._safe(directory))
            except OSError as exc:
                errors.append(f"{directory}: {exc}")
        if failed_backups:
            self.recovery_dir = self._backup_root
            errors.append(f"previous content kept for manual recovery in {self._backup_root}")
            self._backup_root = None  # never delete a backup that is still needed
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
        self._created_dirs = []
