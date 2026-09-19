"""Bounded, fail-closed tracked-quarantine preflight (FR-15 / MD-E1-T02).

The production guard detects whether any attachment-quarantine artefact is already
tracked in the Git index of the trusted workspace root.  Quarantine artefacts are
ephemeral runtime data and must never be committed; if one is tracked, the mail-desk
stops fail-closed before it performs any quarantine write.

Design constraints (security-relevant):

* The Git invocation is a fixed, deterministic ``git ls-files`` argument vector.
  ``shell=True`` is never used and no caller/mail/manifest value can influence the
  command, so no shell interpolation is possible.
* Every pathspec carries Git's documented ``:(icase)`` magic.  On case-insensitive
  worktrees (e.g. Windows) ``Data/Mail-Desk/Attachments/`` denotes the same quarantine
  namespace as ``data/mail-desk/attachments/``; the query must therefore match paths
  case-insensitively and the returned path must classify the same way.
* The invocation is bounded by a hard timeout.  A non-zero exit, a timeout, missing
  or unreadable output, or a runner failure is a bounded stop, never a silent pass.
* The preflight is strictly read-only: it never edits ``.gitignore``, never stages
  or removes files, and never mutates repository configuration.
* The Git runner is injectable (``runner``) so callers and unit tests can exercise
  the guard hermetically without a live Git checkout.

The detection semantics mirror the established MD-Q1 test helper
(``find_tracked_quarantine_files`` / ``assert_no_tracked_quarantine_files``); the
helper now delegates here so a single implementation is authoritative.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
from typing import Any, Callable, Sequence


DEFAULT_GIT_TIMEOUT_SECONDS = 10.0

# Git's documented case-insensitive pathspec magic.  Quarantine paths are a
# security namespace, so a case variant (``Data/Mail-Desk/Attachments/``) must match
# exactly like the canonical lower-case form.
GIT_ICASE_PATHSPEC_MAGIC = ":(icase)"

# Fixed read-only invocation.  ``-z`` yields NUL-separated, unambiguous paths.
GIT_LS_FILES_ARGV: tuple[str, ...] = (
    "git",
    "ls-files",
    "-z",
    "--",
    f"{GIT_ICASE_PATHSPEC_MAGIC}data/mail-desk/attachments/",
    f"{GIT_ICASE_PATHSPEC_MAGIC}data/mail-desk/attachments/**",
    f"{GIT_ICASE_PATHSPEC_MAGIC}**/attachments/**",
    f"{GIT_ICASE_PATHSPEC_MAGIC}**/.quarantine-inventory.json",
    f"{GIT_ICASE_PATHSPEC_MAGIC}**/.quarantine-inventory.lock",
)

ATTACHMENTS_PREFIX = "data/mail-desk/attachments/"
QUARANTINE_INVENTORY_SUFFIXES = (
    ".quarantine-inventory.json",
    ".quarantine-inventory.lock",
)


class QuarantinePreflightError(RuntimeError):
    """Raised when the tracked-quarantine preflight cannot complete safely (fail-closed)."""


class TrackedQuarantineError(QuarantinePreflightError):
    """Raised when quarantine artefacts are already tracked in the Git index."""


@dataclass(frozen=True)
class GitIndexQueryResult:
    """Minimal, dependency-free result of a bounded Git index query.

    The default runner maps a ``subprocess.CompletedProcess`` into this shape; tests
    can construct it directly without importing ``subprocess`` internals.
    """

    returncode: int
    stdout: str
    stderr: str = ""


GitRunner = Callable[[Sequence[str], str, float], GitIndexQueryResult]


def _default_git_runner(argv: Sequence[str], cwd: str, timeout_seconds: float) -> GitIndexQueryResult:
    """Run a bounded, shell-free Git command and capture its output."""
    proc = subprocess.run(
        list(argv),
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        shell=False,
        check=False,
    )
    return GitIndexQueryResult(
        returncode=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
    )


def _is_quarantine_path(path: str) -> bool:
    """Return True for any path that must never be tracked in the Git index.

    Classification is case-insensitive on case-insensitive worktrees and normalises
    Windows separators, so a returned ``Data/Mail-Desk/Attachments/Leak.PDF`` or
    ``Shared/.Quarantine-Inventory.JSON`` is treated exactly like its canonical form.
    """
    normalized = path.replace("\\", "/").lower()
    return normalized.startswith(ATTACHMENTS_PREFIX) or normalized.endswith(
        QUARANTINE_INVENTORY_SUFFIXES
    )


def find_tracked_quarantine_files(
    repo_root: str | Path,
    *,
    runner: GitRunner | None = None,
    timeout_seconds: float = DEFAULT_GIT_TIMEOUT_SECONDS,
) -> list[str]:
    """Return tracked quarantine artefacts under ``repo_root`` (bounded, fail-closed).

    Raises:
        QuarantinePreflightError: If the Git index cannot be queried safely (non-zero
            exit, timeout, runner failure, or unreadable output).
    """
    active_runner = runner or _default_git_runner
    try:
        result = active_runner(GIT_LS_FILES_ARGV, str(repo_root), float(timeout_seconds))
    except subprocess.TimeoutExpired as exc:
        raise QuarantinePreflightError(
            "Tracked-quarantine preflight timed out while querying the Git index; refusing to write."
        ) from exc
    except Exception as exc:  # noqa: BLE001 - any runner failure is a bounded stop
        raise QuarantinePreflightError(
            "Tracked-quarantine preflight could not query the Git index; refusing to write."
        ) from exc

    if result is None:
        raise QuarantinePreflightError(
            "Tracked-quarantine preflight received no Git index result; refusing to write."
        )
    if getattr(result, "returncode", 1) != 0:
        raise QuarantinePreflightError(
            "Tracked-quarantine preflight: 'git ls-files' exited non-zero; refusing to write."
        )
    stdout = getattr(result, "stdout", None)
    if not isinstance(stdout, str):
        raise QuarantinePreflightError(
            "Tracked-quarantine preflight: unreadable Git index output; refusing to write."
        )

    tracked = [entry for entry in stdout.split("\0") if entry]
    return [entry for entry in tracked if _is_quarantine_path(entry)]


def assert_no_tracked_quarantine_files(
    repo_root: str | Path,
    *,
    runner: GitRunner | None = None,
    timeout_seconds: float = DEFAULT_GIT_TIMEOUT_SECONDS,
) -> None:
    """Fail-closed stop condition if any quarantine file is tracked in the Git index."""
    tracked = find_tracked_quarantine_files(
        repo_root, runner=runner, timeout_seconds=timeout_seconds
    )
    if tracked:
        raise TrackedQuarantineError(
            "STOP CONDITION: accidentally tracked quarantine files found in git index: "
            f"{tracked}. These must never be committed and require human resolution."
        )


def resolve_workspace_root(
    workspace_root: str | Path | None = None,
    *,
    data_dir: Path | None = None,
) -> Path:
    """Resolve the trusted workspace root exactly like the attachment lock guard.

    Precedence: explicit ``workspace_root``, then ``WORKSPACE_ROOT``, then the nearest
    ancestor of ``data_dir`` (or the current directory) containing ``.git``/``.agents``.
    """
    if workspace_root is not None:
        return Path(workspace_root).resolve()
    env_ws = os.environ.get("WORKSPACE_ROOT", "").strip()
    if env_ws:
        return Path(env_ws).resolve()
    start = Path(data_dir).resolve() if data_dir is not None else Path.cwd().resolve()
    for parent in [start, *start.parents]:
        if (parent / ".git").is_dir() or (parent / ".agents").is_dir():
            return parent
    return Path.cwd().resolve()


def verify_no_tracked_quarantine(
    workspace_root: str | Path | None = None,
    *,
    data_dir: Path | None = None,
    runner: GitRunner | None = None,
    timeout_seconds: float = DEFAULT_GIT_TIMEOUT_SECONDS,
) -> None:
    """Resolve the trusted workspace root and assert no quarantine artefact is tracked."""
    root = resolve_workspace_root(workspace_root, data_dir=data_dir)
    assert_no_tracked_quarantine_files(root, runner=runner, timeout_seconds=timeout_seconds)


__all__ = [
    "DEFAULT_GIT_TIMEOUT_SECONDS",
    "GIT_ICASE_PATHSPEC_MAGIC",
    "GIT_LS_FILES_ARGV",
    "GitIndexQueryResult",
    "QuarantinePreflightError",
    "TrackedQuarantineError",
    "assert_no_tracked_quarantine_files",
    "find_tracked_quarantine_files",
    "resolve_workspace_root",
    "verify_no_tracked_quarantine",
]
