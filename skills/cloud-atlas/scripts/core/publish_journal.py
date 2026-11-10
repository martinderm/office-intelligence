"""Versioned publish with a minimal intent journal for ``--only-file`` (Issue #22).

Publish model
-------------
A conversion writes its mirror and derivative into a **new, versioned target
directory** below the reserved root ``<output_dir>/_versions/`` and fsyncs them.
The filemap entry is then replaced atomically and is the **single commit point**:
consumers only ever see the old version (entry unchanged) or the new version
(entry replaced).  No in-place artifact is overwritten or restored, so a process
failure before the commit leaves the old version active and only unreferenced new
files behind.

Failure model
-------------
* **Nothing is ever deleted, moved, renamed or archived.**  A failure leaves the
  journal and every staged/version file exactly where they are.  There is no
  ``unlink``/``rmtree``, no quarantine and no journal archive; the reserved version
  root has no cleanup logic at all.
* **Recovery only reports.**  The next run for the same source reads the journal
  and proves whether the previous run committed: ``published``/``committed`` plus a
  live filemap entry that equals the recorded digest (and a version whose files
  still match their checksums).  Only that state allows continuing.  Every other
  state is unclear and fails closed before this run writes anything.
* The journal (``planned -> staged -> published -> committed``) is a crash marker,
  not a lifecycle object.  ``planned`` is written before the version-directory
  rename; ``staged`` immediately before the filemap replace.
* Corruption is caught by SHA-256; targeted manipulation resistance is not the
  threat model.  Uncooperative writers that ignore the harness lease are outside
  this contract.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import time
import uuid
from .filemap_merge import normalize_relative
from pathlib import PurePosixPath
from typing import Callable

JOURNAL_DIR = ".only-file-journal"
VERSION_DIR = "_versions"
SCHEMA_VERSION = 3
STATES = ("planned", "staged", "published", "committed")
CRASH_ENV = "CLOUD_ATLAS_ONLY_FILE_CRASH_AFTER"


class JournalError(RuntimeError):
    """The journal cannot be applied safely; nothing was changed by this step."""


class JournalConflict(JournalError):
    """The live filemap does not match the journal (fail-closed)."""


def crash_point(name: str) -> None:
    """Test hook (like the other ``CLOUD_ATLAS_*`` hooks): hard-kill after a transition."""
    if os.environ.get(CRASH_ENV) == name:
        os._exit(137)


def sha256_file(path: str) -> str | None:
    if not os.path.isfile(path) or os.path.islink(path):
        return None
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def entry_digest(entry) -> str | None:
    if entry is None:
        return None
    canonical = json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def journal_key(source_fold: str) -> str:
    return hashlib.sha256(source_fold.encode("utf-8")).hexdigest()[:40]


def version_root_rel(output_rel: str) -> str:
    return f"{output_rel}/{VERSION_DIR}"


def _fsync_directory(directory: str) -> None:
    """Best effort: only POSIX makes a directory fsync meaningful."""
    if os.name == "nt":
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def fsync_directory(directory: str) -> None:
    """Best-effort directory fsync (POSIX); a no-op where it is not meaningful."""
    _fsync_directory(directory)


def atomic_write_bytes(path: str, data: bytes, safe: Callable[[str], str] = lambda p: p, mode: int | None = None) -> None:
    directory, name = os.path.split(os.path.abspath(path))
    fd, temporary = tempfile.mkstemp(prefix=f".{name}.", suffix=".tmp", dir=safe(directory))
    try:
        with os.fdopen(fd, "wb") as handle:
            fd = None
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temporary, stat.S_IMODE(mode))
        os.replace(temporary, safe(os.path.abspath(path)))
    finally:
        if fd is not None:
            os.close(fd)
        # No unlink: a failed temp write stays next to the target and is left in
        # place (the product path never deletes files).
    _fsync_directory(safe(directory))


def fsync_file(path: str) -> None:
    with open(path, "rb+") as handle:
        handle.flush()
        os.fsync(handle.fileno())


class OnlyFileJournal:
    """One journal per source; version targets live in unique per-run directories.

    The journal is a crash marker, not a lifecycle object.  It is written before the
    version rename and immediately before the filemap replace, and it is **never**
    renamed, archived or deleted.  The next run only *reads* it; nothing here
    mutates an artifact or any other path.
    """

    def __init__(self, workspace_root: str, output_rel: str, source_rel: str, source_fold: str,
                 filemap_rel: str, safe: Callable[[str], str] = lambda p: p):
        self.workspace_root = workspace_root
        self.output_rel = output_rel
        self.source_rel = source_rel
        self.filemap_rel = filemap_rel
        self.safe = safe
        self.key = journal_key(source_fold)
        self.dir_abs = self.abs_of(f"{output_rel}/{JOURNAL_DIR}")
        self.path = os.path.join(self.dir_abs, f"{self.key}.json")
        self.retained_version: str | None = None

    # -- paths ---------------------------------------------------------------
    def abs_of(self, rel: str) -> str:
        return os.path.normpath(os.path.join(self.workspace_root, *PurePosixPath(rel).parts))

    def version_root(self) -> str:
        return f"{self.output_rel}/{VERSION_DIR}"

    # -- persistence -----------------------------------------------------------
    def exists(self) -> bool:
        return os.path.lexists(self.safe(self.path))

    def load(self) -> dict:
        if os.path.islink(self.safe(self.path)):
            raise JournalError(f"journal {self.path} is a link; refusing to follow it")
        try:
            with open(self.safe(self.path), "rb") as handle:
                record = json.loads(handle.read().decode("utf-8"))
        except (OSError, ValueError) as exc:
            raise JournalError(f"journal {self.path} is unreadable: {exc}") from exc
        self.validate(record)
        return record

    def save(self, record: dict) -> None:
        os.makedirs(self.safe(self.dir_abs), exist_ok=True)
        payload = json.dumps(record, indent=2, sort_keys=True).encode("utf-8")
        atomic_write_bytes(self.path, payload, self.safe)

    def validate(self, record) -> None:
        """Reject anything that could redirect cleanup outside the reserved root."""
        def fail(message):
            raise JournalError(f"journal {self.path} is invalid: {message}")

        if not isinstance(record, dict):
            fail("not an object")
        if record.get("schema") != SCHEMA_VERSION:
            fail("unknown schema")
        if record.get("state") not in STATES:
            fail("unknown state")
        if record.get("source") != self.source_rel:
            fail("belongs to a different source")
        filemap = record.get("filemap")
        if not isinstance(filemap, dict) or filemap.get("path") != self.filemap_rel:
            fail("names a different filemap")
        entry = record.get("entry")
        if not isinstance(entry, dict):
            fail("entry record")
        for field in ("old_sha256", "new_sha256"):
            value = entry.get(field)
            if value is not None and not _is_sha(value):
                fail("entry hash")

        version = record.get("version")
        if version is not None:
            if not isinstance(version, dict):
                fail("version record")
            directory = version.get("dir")
            if not isinstance(directory, str):
                fail("version directory")
            if "tmp" in version:
                fail("version record must not carry a staging path")
            if not _inside_version_root(directory, self.version_root()):
                fail("version directory outside the reserved root")
            if _has_unsafe_segments(directory) or "\\" in directory:
                fail("version directory is not a plain POSIX path")
            if not _bound_to_source_key(directory, self.version_root(), self.key):
                fail("version directory belongs to a different source key")
            seen = set()
            for role in ("mirror", "derivative"):
                artifact = version.get(role)
                if artifact is None:
                    continue
                if not isinstance(artifact, dict):
                    fail(f"{role} record")
                path = artifact.get("path")
                digest = artifact.get("sha256")
                if (
                    not isinstance(path, str)
                    or not _within(path, directory)
                    or PurePosixPath(path).as_posix() == PurePosixPath(directory).as_posix()
                    or _has_unsafe_segments(path)
                    or "\\" in path
                ):
                    fail(f"{role} path")
                if not _is_sha(digest):
                    fail(f"{role} hash")
                folded = PurePosixPath(path).as_posix().casefold()
                if folded in seen:
                    fail("duplicate version path")
                seen.add(folded)
            # The planned record describes the intent; from staged on the mirror and
            # its checksum must be recorded.
            if record.get("state") != "planned" and version.get("mirror") is None:
                fail("version without a mirror")

    # -- version handling --------------------------------------------------------
    def _require_version_path(self, rel: str, real_output: str) -> str:
        """Resolve a version directory against the real output dir; reject links."""
        if _has_unsafe_segments(rel) or "\\" in rel:
            raise JournalConflict(f"version path {rel} is not a plain POSIX path")
        absolute = self.abs_of(rel)
        root = version_root_rel(self.output_rel)
        rest = PurePosixPath(rel).parts[len(PurePosixPath(root).parts):]
        expected_root = os.path.join(real_output, VERSION_DIR)
        if not _same_path(os.path.realpath(self.abs_of(root)), expected_root):
            raise JournalConflict("the version root resolves through a link or outside output_dir")
        expected = os.path.join(expected_root, *rest)
        if os.path.islink(self.safe(absolute)) or not _same_path(os.path.realpath(absolute), expected):
            raise JournalConflict(f"version path {rel} resolves through a link or outside the version root")
        return absolute

    def filemap_temp_path(self) -> str:
        """A unique filemap-commit temp path (never reused, never deleted)."""
        return os.path.join(
            self.dir_abs,
            f"{self.key}.filemap.tmp.{os.getpid()}-{uuid.uuid4().hex[:8]}",
        )

    def existing_version_path(self, record: dict, real_output: str) -> str | None:
        """The recorded version directory, if it actually exists (report only)."""
        version = (record or {}).get("version")
        if not isinstance(version, dict):
            return None
        rel = version.get("dir")
        if not isinstance(rel, str):
            return None
        try:
            absolute = self._require_version_path(rel, real_output)
        except JournalConflict:
            return None
        return rel if os.path.lexists(self.safe(absolute)) else None

    # -- report-only recovery ----------------------------------------------------
    def _verify_active_version(self, record: dict, real_output: str) -> None:
        version = record.get("version")
        if not version:
            return
        for label in ("dir",):
            absolute = self._require_version_path(version[label], real_output)
            if not os.path.isdir(self.safe(absolute)) or os.path.islink(self.safe(absolute)):
                raise JournalError(f"active version directory {version[label]} is missing or a link")
            for role in ("mirror", "derivative"):
                artifact = version.get(role)
                if not artifact:
                    continue
                candidate = os.path.join(absolute, PurePosixPath(artifact["path"]).name)
                if (
                    not os.path.isfile(self.safe(candidate))
                    or os.path.islink(self.safe(candidate))
                    or sha256_file(self.safe(candidate)) != artifact["sha256"]
                ):
                    raise JournalError(
                        f"active version file {version[label]}/{PurePosixPath(artifact['path']).name} "
                        "is missing or does not match its recorded checksum"
                    )

    def recover(self, record: dict, current_entry_digest: Callable[[], str | None],
                real_output: str) -> str:
        """Report an open journal; the only state that allows continuing is a commit.

        Recovery performs no roll-forward, no roll-back, no archiving and no removal.
        It verifies that the recorded commit is the live filemap entry (and that the
        recorded version still matches its checksums) and reports the version path.
        Every other state is unclear: the caller fails closed before mutating
        anything, and the journal as well as all staged/version files stay in place.
        """
        self.retained_version = self.existing_version_path(record, real_output)
        new = record["entry"].get("new_sha256")
        live = current_entry_digest()
        if new is not None and live == new:
            # The live entry proves the replace happened (a crash between the
            # replace and the ``published`` record leaves ``staged`` behind).
            self._verify_active_version(record, real_output)
            return "committed"
        if record["state"] in ("published", "committed"):
            # The previous run provably completed; a later change of the entry (by
            # another run or a full scan) does not make that journal unclear.
            return "committed"
        raise JournalConflict(
            f"journal state {record['state']} is unclear and the live filemap entry "
            f"does not prove a commit; inspect {self.path}"
        )


def _has_unsafe_segments(path: str) -> bool:
    parsed = PurePosixPath(path)
    return (
        parsed.is_absolute()
        or ".." in parsed.parts
        or "." in parsed.parts
        or ":" in path
        or (path and path[0] in ("/", "\\"))
    )


def _bound_to_source_key(path: str, root: str, key: str) -> bool:
    """The path must be ``<root>/<qell-key>/<run>`` (the key root is not a version)."""
    try:
        rest = PurePosixPath(path).relative_to(PurePosixPath(root)).parts
    except ValueError:
        return False
    return len(rest) >= 2 and rest[0] == key[:16]


def _is_sha(value) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _within(path: str, parent: str) -> bool:
    try:
        PurePosixPath(path).relative_to(PurePosixPath(parent))
        return True
    except ValueError:
        return False


def _inside_version_root(path: str, root: str) -> bool:
    """Strictly below the reserved root (never the root itself)."""
    if not _within(path, root) or PurePosixPath(path).as_posix().casefold() == PurePosixPath(root).as_posix().casefold():
        return False
    parsed = PurePosixPath(path)
    return not parsed.is_absolute() and ".." not in parsed.parts and ":" not in path


def _same_path(left: str, right: str) -> bool:
    return os.path.normcase(left) == os.path.normcase(right)
