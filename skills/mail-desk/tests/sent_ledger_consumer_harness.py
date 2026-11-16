"""Declared synthetic consumer fixture harness for Issue #88 (G1 cases F1-F22).

The harness is the declared B11 artifact behind the offline consumer acceptance:
it creates an isolated synthetic workspace, takes byte-exact snapshots, runs the
P1 capability probe, installs an instrumented transport stub and emits a
machine-readable result record.  It mutates only the isolated synthesis workspace
it created itself (setup, snapshots, and - on request - the workspace-lock lease
artifacts below ``.agents/``); it never performs a mailbox sync.

Evidence separation (F2)
------------------------
A lease is either **real** :class:`RealLease` - acquired and released through the
published ``workspace-lock`` skill, located through ``WORKSPACE_LOCK_ROOT`` or a
project-relative ``.agents/skills`` junction - or **injected**
:class:`InjectedLease` - a test double that replaces
``core.sent_ledger.verify_workspace_lock``.  Every :class:`CaseEvidence` carries a
``lease_source`` label, and injected evidence is never accepted as proof of a real
lease.  ``_bind_workspace_data_dir`` is not patched in either variant, so the
target binding keeps its fail-closed behavior.

Transport separation (F17)
--------------------------
:class:`TransportStub` rebinds ``core.sent_indexer.get_single_email_details``,
``core.sent_indexer.run_himalaya`` and ``core.himalaya.run_himalaya``, records
every page, detail, raw-mail and read request (including ``preview_lines`` and the
attachment-inspection path) and installs a tripwire on the process boundary
``core.himalaya._run_process`` so an escaped real ``himalaya`` invocation fails
loudly instead of touching a mailbox.

Data rule
---------
Synthetic data only: ``example.org`` Message-IDs and the synthetic accounts
``SYNTH-A``/``SYNTH-B``.  No network, no real mail, no real identifiers.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from unittest.mock import patch

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = MAIL_DESK_ROOT / "scripts"
TESTS_ROOT = Path(__file__).resolve().parent
for _path in (SCRIPTS, TESTS_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import mail_desk_sent_ledger as ledger_cli  # noqa: E402
from core import himalaya, sent_handoff, sent_indexer, sent_ledger  # noqa: E402
from core.sent_handoff import HANDOFF_FILENAME, LEDGER_FILENAME, norm_id  # noqa: E402
from sent_ledger_adapter_fixtures import (  # noqa: E402
    ACCOUNT_A,
    ACCOUNT_B,
    handoff_item,
    ledger_line,
    make_workspace,
    read_ledger_lines,
    snapshot as _snapshot,
    write_backend_config,
    write_evaluations,
    write_evidence,
    write_handoff,
    write_ledger,
)

#: Declared harness identity.  Bump only with a reviewed protocol change.
HARNESS_KIND = "mail-desk.sent-ledger.consumer-fixture-harness"
HARNESS_VERSION = "1.0"

#: The four mutating/read-only ledger entry points the harness declares.
LEDGER_SUBCOMMANDS = ("pending", "record", "consume", "crash-check")

#: Declared tools the SOP/acceptance path must name instead of ad-hoc calls (F21).
DECLARED_TOOLS = (
    {
        "id": "fixture-harness",
        "kind": "harness",
        "entry": "python -B -m unittest discover -s skills/mail-desk/tests "
                 "-p \"test_sent_ledger_consumer_harness.py\" -v",
        "mutating": "isolated synthesis workspace only",
    },
    {
        "id": "capability-probe",
        "kind": "read_only",
        "entry": "sent_ledger_consumer_harness.capability_probe",
        "mutating": False,
    },
    {
        "id": "inventory-promotion",
        "kind": "cli",
        "entry": "mail_desk_sent_ledger.py inventory-promotion",
        "mutating": False,
    },
    {
        "id": "inventory-legacy",
        "kind": "cli",
        "entry": "mail_desk_sent_ledger.py inventory-legacy",
        "mutating": False,
    },
)

#: Cases the reviewed start driver covers; the full G1 matrix stays declared in the run.
#: Deliberately unchanged: the remaining G1 cases are declared separately below so a
#: follow-up dispatch can never silently widen the reviewed start set.
HARNESS_CASE_IDS = ("F1", "F2", "F3", "F5", "F17", "F19", "F21", "F22")

#: Follow-up G1 cases (F4, F6-F10, F15, F16, F18, F20) driven by the separate
#: ``test_sent_ledger_g1_remaining.py`` module on the same declared harness.
G1_REMAINING_CASE_IDS = ("F4", "F6", "F7", "F8", "F9", "F10", "F15", "F16", "F18", "F20")

#: The twelve strictly separated crash-recovery variants of F18 (a)-(l).
F18_VARIANTS = tuple("abcdefghijkl")

#: The confirmed consumer recovery gate (contract decision A) plus its accepted
#: F18(h) exception.  When an append is required, a mutating
#: ``recover-promotion`` call needs a proven-complete evidence list, a ``unique``
#: crash classification **and** ``safe_to_append: true``.  Without a required
#: append, ``already_consumed`` is a consumer no-op: exit 0, no recovery call and
#: no write, and ``safe_to_append: false`` is explicitly not a trigger.
#: ``crash-check`` derives ``safe_to_append`` from ``append_required``, so the
#: exception is what keeps the documented (h) path reachable.
CONSUMER_RECOVERY_GATE = ("completeness_proven", "unique", "safe_to_append")

WORKSPACE_LOCK_SKILL = "workspace-lock"
WORKSPACE_LOCK_GUARD = Path("scripts") / "workspace_lock_guard.py"
WORKSPACE_LOCK_CLI = Path("scripts") / "invoke_workspace_lock.py"
SESSION_LOCK = Path(".agents") / "session.lock"
HARNESS_LEASE_OWNER = "oi88-fixture-harness"

SYNTH_FOLDER = "Sent Items"
SYNTH_DAY = "2026-11-02"
SYNTH_DAY_NEXT = "2026-11-03"

SYNTHETIC_EML = (
    "From: Synthetic Sender <sender@example.org>\r\n"
    "To: Synthetic Recipient <recipient@example.org>\r\n"
    "Message-Id: <alpha@example.org>\r\n"
    "Subject: Synthetic subject\r\n"
    "Date: Mon, 02 Nov 2026 08:00:00 +0100\r\n"
    "MIME-Version: 1.0\r\n"
    "Content-Type: text/plain; charset=\"utf-8\"\r\n"
    "\r\n"
    "Synthetic body line 1\r\n"
    "Synthetic body line 2\r\n"
).encode("utf-8")


class HarnessError(RuntimeError):
    """A harness precondition is not met (never a product-behavior assertion)."""


class TransportEscapeError(RuntimeError):
    """A real transport call escaped the stub; the fixture is invalid."""


# ---------------------------------------------------------------------------
# Paths and installed-copy discovery
# ---------------------------------------------------------------------------

def repository_root() -> Path:
    """Repository root of this bundle checkout (portable, derived, not hardcoded)."""
    return MAIL_DESK_ROOT.parents[1]


def ledger_cli_path() -> Path:
    return SCRIPTS / "mail_desk_sent_ledger.py"


def workspace_lock_root() -> Path | None:
    """Locate the published ``workspace-lock`` skill, or ``None`` when absent.

    Resolution order: explicit ``WORKSPACE_LOCK_ROOT``, the project-relative
    ``.agents/skills/workspace-lock`` installation, then the shared skills root
    behind an existing ``.agents/skills`` junction.  No host path is hardcoded.
    """
    candidates: list[Path] = []
    configured = os.environ.get("WORKSPACE_LOCK_ROOT", "").strip()
    if configured:
        candidates.append(Path(configured))
    project_relative = repository_root() / ".agents" / "skills" / WORKSPACE_LOCK_SKILL
    candidates.append(project_relative)
    shared_root = _shared_skills_root()
    if shared_root is not None:
        candidates.append(shared_root / WORKSPACE_LOCK_SKILL)
    for candidate in candidates:
        try:
            if (Path(candidate) / WORKSPACE_LOCK_GUARD).is_file():
                return Path(candidate).resolve()
        except OSError:  # pragma: no cover - unreadable installation
            continue
    return None


def _shared_skills_root() -> Path | None:
    """Shared skills installation behind a project-relative ``.agents/skills`` junction."""
    agents_skills = repository_root() / ".agents" / "skills"
    try:
        if not agents_skills.is_dir():
            return None
        inside = agents_skills.resolve()
        entries = sorted(agents_skills.iterdir())
    except OSError:  # pragma: no cover - unreadable junction
        return None
    for entry in entries:
        try:
            target = entry.resolve()
        except OSError:  # pragma: no cover - broken junction
            continue
        if target == inside or inside in target.parents:
            continue
        if (target.parent / WORKSPACE_LOCK_SKILL / WORKSPACE_LOCK_GUARD).is_file():
            return target.parent
    return None


def require_workspace_lock_root() -> Path:
    root = workspace_lock_root()
    if root is None:
        raise HarnessError(
            "the published workspace-lock skill was not found; set WORKSPACE_LOCK_ROOT "
            "to its trusted installation to collect real-lease evidence"
        )
    return root


def workspace_lock_cli() -> Path:
    cli = require_workspace_lock_root() / WORKSPACE_LOCK_CLI
    if not cli.is_file():  # pragma: no cover - incomplete installation
        raise HarnessError("the workspace-lock installation has no invoke_workspace_lock.py")
    return cli


def _git_head(root: Path) -> str | None:
    """Best-effort installed commit of ``root``; ``None`` when it is not a checkout."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=False, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - no git on host
        return None
    value = completed.stdout.strip()
    if completed.returncode != 0 or not value:
        return None
    if len(value) < 40 or any(char not in "0123456789abcdef" for char in value.lower()):
        return None
    return value


# ---------------------------------------------------------------------------
# Filesystem link fixtures (F4)
# ---------------------------------------------------------------------------

def _is_junction(path: Path, info: os.stat_result) -> bool:
    """Detect an NTFS directory junction without requiring Python 3.12.

    ``os.path.isjunction`` exists only from Python 3.12.  The fallback reads the
    reparse tag that ``os.lstat`` exposes on Windows (3.8+); an unknown tag is
    reported as *not* a junction so the F4 fixture skips instead of claiming the
    wrong link kind.
    """
    probe = getattr(os.path, "isjunction", None)
    if callable(probe):
        try:
            return bool(probe(path))
        except OSError:  # pragma: no cover - unreadable component
            return False
    attributes = getattr(info, "st_file_attributes", 0)
    if not attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
        return False
    reparse_tag = getattr(info, "st_reparse_tag", None)
    mount_point = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)
    return isinstance(reparse_tag, int) and reparse_tag == mount_point


def link_kind(path: Path) -> str | None:
    """``symlink``/``junction``/``reparse``/``unverifiable`` for one path, else ``None``."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:  # pragma: no cover - unreadable component
        return "unverifiable"
    if stat.S_ISLNK(info.st_mode):
        return "symlink"
    if _is_junction(path, info):
        return "junction"
    attributes = getattr(info, "st_file_attributes", 0)
    if attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
        return "reparse"
    return None


def make_directory_link(link: Path, target: Path) -> str | None:
    """Create a real directory junction on Windows, a directory symlink elsewhere.

    Windows uses ``cmd /c mklink /J`` exactly like the established batch-wrapper
    and promotion fixtures: a junction is a genuine NTFS reparse point and needs no
    ``SeCreateSymbolicLinkPrivilege``.  A *symlink* is deliberately not substituted
    for a junction on Windows (different semantics); if the junction cannot be
    created the fixture reports ``None`` and the test skips with its own reason
    instead of silently testing a different object.

    Returns the created link kind (``junction``/``symlink``) or ``None``.
    """
    link = Path(link)
    target = Path(target)
    if not target.exists():
        raise HarnessError(f"a directory link needs an existing target: {target.name}")
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        if os.name == "nt":
            # Bytes only: ``mklink`` writes localized (non-UTF-8) console text.
            completed = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True, check=False, timeout=60,
            )
            if completed.returncode != 0:
                return None
        else:  # pragma: no cover - POSIX runner
            link.symlink_to(target, target_is_directory=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return link_kind(link)


# ---------------------------------------------------------------------------
# Result protocol
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CliResult:
    """One ledger CLI envelope plus its exit code."""

    exit_code: int
    envelope: Mapping[str, Any]

    @property
    def success(self) -> bool:
        return bool(self.envelope.get("success"))

    @property
    def data(self) -> Mapping[str, Any]:
        data = self.envelope.get("data")
        return data if isinstance(data, Mapping) else {}

    @property
    def reason_code(self) -> str | None:
        value = self.data.get("reason_code")
        return value if isinstance(value, str) else None

    @property
    def error_type(self) -> str | None:
        error = self.envelope.get("error")
        if isinstance(error, Mapping) and isinstance(error.get("type"), str):
            return str(error["type"])
        return None

    def as_evidence(self) -> dict[str, Any]:
        """Machine-readable shape without raw identifiers or host paths."""
        return {
            "exit_code": self.exit_code,
            "success": self.success,
            "state": self.envelope.get("state"),
            "reason_code": self.reason_code,
            "error_type": self.error_type,
            "action": self.envelope.get("action"),
        }


@dataclass
class CaseEvidence:
    """Result record of one fixture run, labelled with its lease provenance."""

    case_id: str
    expectation: str
    lease_source: str
    exit_code: int
    reason_code: str | None
    writes: str
    snapshot_before: dict[str, str] = field(default_factory=dict)
    snapshot_after: dict[str, str] = field(default_factory=dict)
    transport: dict[str, Any] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def files_unchanged(self) -> bool:
        return self.snapshot_before == self.snapshot_after

    @property
    def changed_files(self) -> tuple[str, ...]:
        names = set(self.snapshot_before) | set(self.snapshot_after)
        return tuple(sorted(
            name for name in names
            if self.snapshot_before.get(name) != self.snapshot_after.get(name)
        ))

    def as_evidence(self) -> dict[str, Any]:
        """Portable evidence: workspace-relative names and hashes only."""
        return {
            "harness_kind": HARNESS_KIND,
            "harness_version": HARNESS_VERSION,
            "case_id": self.case_id,
            "expectation": self.expectation,
            "lease_source": self.lease_source,
            "exit_code": self.exit_code,
            "reason_code": self.reason_code,
            "writes": self.writes,
            "files_unchanged": self.files_unchanged,
            "changed_files": list(self.changed_files),
            "snapshot_before": dict(self.snapshot_before),
            "snapshot_after": dict(self.snapshot_after),
            "transport": dict(self.transport),
            "details": dict(self.details),
        }


def run_case(
    case_id: str,
    expectation: str,
    *,
    lease: "LeaseBase | None" = None,
    transport: "TransportStub | None" = None,
    details: Mapping[str, Any] | None = None,
) -> "CaseRunner":
    """Collect before/after hashes and the labeled evidence of one fixture run."""
    return CaseRunner(
        case_id, expectation, lease=lease, transport=transport, details=details,
    )


class CaseRunner:
    """Small builder: snapshot a workspace, run the case, emit labeled evidence."""

    def __init__(
        self,
        case_id: str,
        expectation: str,
        *,
        lease: "LeaseBase | None" = None,
        transport: "TransportStub | None" = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.case_id = case_id
        self.expectation = expectation
        self.lease = lease
        self.transport = transport
        self.details = dict(details or {})

    def finish(
        self,
        *,
        result: CliResult | None = None,
        exit_code: int | None = None,
        reason_code: str | None = None,
        writes: str = "no_write",
        snapshot_before: Mapping[str, str] | None = None,
        snapshot_after: Mapping[str, str] | None = None,
    ) -> CaseEvidence:
        transport = self.transport.snapshot() if self.transport is not None else {}
        return CaseEvidence(
            case_id=self.case_id,
            expectation=self.expectation,
            lease_source=(self.lease.source if self.lease is not None else "none"),
            exit_code=result.exit_code if result is not None else int(exit_code or 0),
            reason_code=result.reason_code if result is not None else reason_code,
            writes=writes,
            snapshot_before=dict(snapshot_before or {}),
            snapshot_after=dict(snapshot_after or {}),
            transport=transport,
            details=dict(self.details),
        )


def consumer_recovery_gate(
    *,
    completeness_proven: bool,
    status: str | None,
    safe_to_append: bool,
    append_required: bool,
    exit_code: int,
) -> tuple[str, tuple[str, ...]]:
    """Decide the consumer action for one crash classification.

    Returns ``(decision, unmet)``.  ``recovery`` authorizes the mutating
    ``recover-promotion`` call; ``consumer_noop`` means the item is already
    consumed and the consumer reports exit 0 without calling recovery and without
    a write; ``refuse`` stops.  ``unmet`` names the unsatisfied entries of
    :data:`CONSUMER_RECOVERY_GATE` for a refusal (F18(b), F18(c)).  A missing
    append is never a recovery trigger, so ``safe_to_append: false`` alone does not
    make a no-op a refusal (F18(h)).
    """
    satisfied = {
        "completeness_proven": bool(completeness_proven),
        "unique": status == "unique",
        "safe_to_append": bool(safe_to_append),
    }
    unmet = tuple(name for name in CONSUMER_RECOVERY_GATE if not satisfied[name])
    if int(exit_code) != 0:
        return "refuse", unmet
    if bool(append_required):
        return ("recovery", ()) if not unmet else ("refuse", unmet)
    if status == "unique":
        # Accepted F18(h) exception: already consumed, no append required.  The
        # consumer reports exit 0 without calling recovery and without a write;
        # ``safe_to_append: false`` is not a trigger here.
        return "consumer_noop", ()
    return "refuse", unmet


# ---------------------------------------------------------------------------
# Isolated synthesis workspace
# ---------------------------------------------------------------------------

@dataclass
class SyntheticWorkspace:
    """An isolated ``<root>/data/mail-desk`` workspace with synthetic state."""

    root: Path

    @classmethod
    def create(
        cls,
        root: Path,
        *,
        account: str | None = ACCOUNT_A,
        handoff_items: Sequence[Mapping[str, Any]] | None = None,
        ledger_lines: Sequence[Mapping[str, Any]] | None = None,
        handoff_missing: bool = False,
    ) -> "SyntheticWorkspace":
        workspace, _data_dir = make_workspace(
            Path(root),
            account=account,
            handoff_items=[dict(item) for item in handoff_items] if handoff_items is not None else None,
            ledger_lines=[dict(line) for line in ledger_lines] if ledger_lines else None,
            handoff_missing=handoff_missing,
        )
        return cls(root=workspace)

    @property
    def data_dir(self) -> Path:
        return self.root / "data" / "mail-desk"

    @property
    def session_lock(self) -> Path:
        return self.root / SESSION_LOCK

    def snapshot(self) -> dict[str, str]:
        return _snapshot(self.root)

    def ledger_lines(self) -> list[dict[str, Any]]:
        return read_ledger_lines(self.data_dir)

    def handoff_bytes(self) -> bytes | None:
        path = self.data_dir / HANDOFF_FILENAME
        return path.read_bytes() if path.exists() else None

    def ledger_bytes(self) -> bytes | None:
        path = self.data_dir / LEDGER_FILENAME
        return path.read_bytes() if path.exists() else None

    def write_handoff(self, items: Sequence[Mapping[str, Any]]) -> None:
        write_handoff(self.data_dir, [dict(item) for item in items])

    def write_ledger(self, lines: Sequence[Mapping[str, Any]]) -> None:
        write_ledger(self.data_dir, [dict(line) for line in lines])

    def bind_account(self, account: str | None) -> Path:
        return write_backend_config(self.root, account=account)

    def evaluations(self, entries: Sequence[Mapping[str, Any]]) -> Path:
        return write_evaluations(self.data_dir / "sent-evaluation.json", [dict(e) for e in entries])

    def evidence(self, records: Sequence[Mapping[str, Any]]) -> Path:
        return write_evidence(self.data_dir / "durable-promotions.json", [dict(r) for r in records])


# ---------------------------------------------------------------------------
# Lease evidence: real vs. injected
# ---------------------------------------------------------------------------

class LeaseBase:
    """Common lease evidence surface; the two variants are never comparable."""

    source = "none"

    def __enter__(self) -> "LeaseBase":  # pragma: no cover - protocol
        raise NotImplementedError

    def __exit__(self, *exc: object) -> None:  # pragma: no cover - protocol
        raise NotImplementedError

    def as_evidence(self) -> dict[str, Any]:  # pragma: no cover - protocol
        raise NotImplementedError


@dataclass
class RealLease(LeaseBase):
    """A real lease through the published ``workspace-lock`` skill (acquire/release)."""

    workspace: Path
    lease_id: str
    conversation_id: str | None = None
    _acquired: dict[str, Any] | None = None
    _released: dict[str, Any] | None = None
    _acquired_at: str | None = None
    _released_at: str | None = None
    source = "real"

    def _invoke(self, action: str, *extra: str) -> dict[str, Any]:
        command = [
            sys.executable, str(workspace_lock_cli()), action, str(self.workspace),
            *extra, "--json",
        ]
        completed = subprocess.run(
            command, capture_output=True, text=True, check=False, timeout=120,
        )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise HarnessError(
                f"workspace-lock {action} returned malformed JSON "
                f"(exit {completed.returncode}): {completed.stderr.strip()}"
            ) from error
        if not isinstance(payload, dict):
            raise HarnessError(f"workspace-lock {action} returned a non-object payload")
        return payload

    def acquire(self) -> "RealLease":
        if workspace_lock_root() is None:
            raise HarnessError("no published workspace-lock installation is available")
        options = [
            "--harness", HARNESS_LEASE_OWNER,
            "--lease-id", self.lease_id,
            "--owner-pid", str(os.getpid()),
        ]
        if self.conversation_id:
            options.extend(["--conversation-id", self.conversation_id])
        payload = self._invoke("acquire", *options)
        if not payload.get("success") or payload.get("state") != "Acquired":
            raise HarnessError(
                "the real workspace lease was not acquired: "
                f"{payload.get('message') or payload.get('code') or payload.get('state')}"
            )
        lock = payload.get("lock")
        if not isinstance(lock, Mapping) or lock.get("leaseId") != self.lease_id:
            raise HarnessError("the acquired lock does not prove invocation ownership")
        if not self.workspace.joinpath(SESSION_LOCK).is_file():
            raise HarnessError("the acquired lease left no .agents/session.lock artifact")
        self._acquired = dict(lock)
        self._acquired_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return self

    def release(self) -> "RealLease":
        if self._released is not None:
            return self
        options = ["--lease-id", self.lease_id]
        if self.conversation_id:
            options.extend(["--conversation-id", self.conversation_id])
        payload = self._invoke("release", *options)
        if not payload.get("success") or payload.get("state") != "Released":
            raise HarnessError(f"the real workspace lease was not released: {payload.get('message')}")
        self._released = payload
        self._released_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return self

    def __enter__(self) -> "RealLease":
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()

    def as_evidence(self) -> dict[str, Any]:
        acquired = self._acquired or {}
        return {
            "lease_source": self.source,
            "lease_id": self.lease_id,
            "owner": acquired.get("harness"),
            "owner_pid": acquired.get("pid"),
            "acquired_at": self._acquired_at,
            "released_at": self._released_at,
            "released": self._released is not None,
            "artifact": SESSION_LOCK.as_posix(),
            "artifact_present_after_release": self.workspace.joinpath(SESSION_LOCK).exists(),
        }


LEASE_DENIALS = {
    "missing": "workspace lock is not active ('Free')",
    "expired": "workspace lock is not active ('Stale')",
    "foreign": "workspace lock is not owned by this invocation",
}


@dataclass
class InjectedLease(LeaseBase):
    """A test double for the lease verifier; never evidence of a real lease."""

    mode: str = "allow"
    lease_id: str = "INJECTED-LEASE"
    calls: list[tuple[str, str | None]] = field(default_factory=list)
    source = "injected"

    def _verifier(self, workspace: Any, *, lease_id=None, conversation_id=None, data_dir=None) -> Any:
        self.calls.append((str(workspace), lease_id))
        if self.mode == "allow":
            return {"state": "injected-allow", "lease_id": lease_id}
        raise RuntimeError(LEASE_DENIALS.get(self.mode, "workspace lock is not active"))

    def __enter__(self) -> "InjectedLease":
        self._context = patch.object(sent_ledger, "verify_workspace_lock", self._verifier)
        self._context.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._context.stop()

    def as_evidence(self) -> dict[str, Any]:
        return {
            "lease_source": self.source,
            "mode": self.mode,
            "verifier_calls": len(self.calls),
            "real_lease_proven": False,
            "artifact": None,
        }


# ---------------------------------------------------------------------------
# Instrumented transport stub
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TransportRequest:
    """One recorded transport request; never carries mail content."""

    kind: str
    envelope_id: str | None = None
    date: str | None = None
    page: int | None = None
    preview_lines: int | None = None
    inspect_attachments: bool | None = None
    has_attachment: bool | None = None
    argv: tuple[str, ...] = ()

    def as_evidence(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "envelope_id": self.envelope_id,
            "date": self.date,
            "page": self.page,
            "preview_lines": self.preview_lines,
            "inspect_attachments": self.inspect_attachments,
            "has_attachment": self.has_attachment,
            "argv": list(self.argv),
        }


def synthetic_envelope(env_id: str, *, has_attachment: bool = False) -> dict[str, Any]:
    """One adapter-shaped synthetic envelope for the page listing."""
    return {
        "id": env_id,
        "subject": "Synthetic subject",
        "from": {"name": "Synthetic Sender", "addr": "sender@example.org"},
        "to": {"name": "Synthetic Recipient", "addr": "recipient@example.org"},
        "date": "2026-11-02 08:00:00+01:00",
        "has_attachment": has_attachment,
    }


def synthetic_detail(env_id: str, mid: str, **overrides: Any) -> dict[str, Any]:
    """One synthetic detail payload as ``get_single_email_details`` returns it."""
    payload: dict[str, Any] = {
        "envelope_id": env_id,
        "folder": SYNTH_FOLDER,
        "message_id": mid,
        "raw_message_id": f"<{mid}>",
        "subject": "Synthetic subject",
        "from": "Synthetic Sender <sender@example.org>",
        "to": "Synthetic Recipient <recipient@example.org>",
        "cc": "",
        "headers_complete": True,
        "date": "Mon, 02 Nov 2026 08:00:00 +0100",
        "in_reply_to": "",
        "references": "",
        "preview": "Synthetic body line 1",
        "attachments": [],
        "attachment_status": "none",
        "attachment_error": None,
        "error": None,
        "read_error_reason": None,
        "read_level": "preview",
    }
    payload.update(overrides)
    return payload


class TransportStub:
    """Instrumented synthetic transport on the ``core.sent_indexer``/``core.himalaya`` seams."""

    def __init__(
        self,
        *,
        pages: Mapping[str, Sequence[Sequence[Mapping[str, Any]]]] | None = None,
        details: Mapping[str, Mapping[str, Any]] | None = None,
        raw_eml: Mapping[str, bytes] | None = None,
        read_body: str = "Synthetic body line 1",
    ) -> None:
        self.pages = {
            date: [list(page) for page in day_pages]
            for date, day_pages in (pages or {}).items()
        }
        self.details = {env: dict(payload) for env, payload in (details or {}).items()}
        self.raw_eml = dict(raw_eml or {})
        self.read_body = read_body
        self.requests: list[TransportRequest] = []
        self.escapes: list[tuple[str, ...]] = []
        self._originals: dict[str, Any] = {}
        self._installed = False

    # -- stubs ------------------------------------------------------------
    def run_himalaya(self, args: Sequence[str], account: str | None = None, **kwargs: Any) -> str:
        argv = tuple(str(part) for part in args)
        if "envelope" in argv and "list" in argv:
            page = self._option_int(argv, "--page", default=1)
            date = self._date_token(argv)
            self.requests.append(TransportRequest(kind="page", date=date, page=page, argv=argv))
            day_pages = self.pages.get(date or "", [])
            page_envs = day_pages[page - 1] if 0 < page <= len(day_pages) else []
            return json.dumps(list(page_envs))
        if "message" in argv and "export" in argv:
            env_id = self._export_envelope(argv)
            destination = self._option(argv, "-d")
            self.requests.append(TransportRequest(kind="raw", envelope_id=env_id, argv=argv))
            if destination is None:
                raise TransportEscapeError("the stub was asked to export without a destination")
            Path(destination).write_bytes(self.raw_eml.get(env_id or "", SYNTHETIC_EML))
            return ""
        if "message" in argv and "read" in argv:
            env_id = argv[-1]
            self.requests.append(TransportRequest(kind="read", envelope_id=env_id, argv=argv))
            return self._read_output(env_id)
        self.escapes.append(argv)
        raise TransportEscapeError(f"unexpected transport command escaped the stub: {argv}")

    def get_single_email_details(self, env_id: Any, folder: str = "INBOX", *args: Any, **kwargs: Any) -> dict[str, Any]:
        key = str(env_id)
        fallback = kwargs.get("fallback_envelope")
        preview_lines = kwargs.get("preview_lines")
        self.requests.append(TransportRequest(
            kind="detail",
            envelope_id=key,
            preview_lines=preview_lines if isinstance(preview_lines, int) else None,
            inspect_attachments=(
                kwargs.get("inspect_attachments")
                if "inspect_attachments" in kwargs else True
            ),
            has_attachment=(
                fallback.get("has_attachment") if isinstance(fallback, Mapping) else None
            ),
            argv=(str(folder),),
        ))
        payload = self.details.get(key)
        if payload is None:
            return synthetic_detail(key, f"missing-{key}@example.org")
        return json.loads(json.dumps(payload))

    def tripwire(self, *args: Any, **kwargs: Any) -> Any:
        argv = tuple(str(part) for part in (args[0] if args and isinstance(args[0], (list, tuple)) else args))
        self.escapes.append(argv)
        raise TransportEscapeError(
            f"a real Himalaya process was attempted ({argv}); the fixture is invalid"
        )

    # -- lifecycle --------------------------------------------------------
    def install(self) -> "TransportStub":
        if self._installed:
            return self
        self._originals = {
            "indexer_details": sent_indexer.get_single_email_details,
            "indexer_run": sent_indexer.run_himalaya,
            "himalaya_run": himalaya.run_himalaya,
            "himalaya_process": himalaya._run_process,
        }
        # Bind once: a fresh bound method is a different object on every attribute access.
        self._bound = {
            "details": self.get_single_email_details,
            "run": self.run_himalaya,
            "tripwire": self.tripwire,
        }
        sent_indexer.get_single_email_details = self._bound["details"]
        sent_indexer.run_himalaya = self._bound["run"]
        himalaya.run_himalaya = self._bound["run"]
        himalaya._run_process = self._bound["tripwire"]
        self._installed = True
        return self

    def uninstall(self) -> None:
        if not self._installed:
            return
        sent_indexer.get_single_email_details = self._originals["indexer_details"]
        sent_indexer.run_himalaya = self._originals["indexer_run"]
        himalaya.run_himalaya = self._originals["himalaya_run"]
        himalaya._run_process = self._originals["himalaya_process"]
        self._installed = False

    def __enter__(self) -> "TransportStub":
        return self.install()

    def __exit__(self, *exc: object) -> None:
        self.uninstall()

    # -- evidence ---------------------------------------------------------
    def requests_of(self, kind: str) -> tuple[TransportRequest, ...]:
        return tuple(request for request in self.requests if request.kind == kind)

    @property
    def detail_requests(self) -> tuple[TransportRequest, ...]:
        return self.requests_of("detail")

    @property
    def page_requests(self) -> tuple[TransportRequest, ...]:
        return self.requests_of("page")

    @property
    def raw_requests(self) -> tuple[TransportRequest, ...]:
        return self.requests_of("raw")

    def snapshot(self) -> dict[str, Any]:
        return {
            "harness_kind": HARNESS_KIND,
            "request_count": len(self.requests),
            "kinds": sorted({request.kind for request in self.requests}),
            "requests": [request.as_evidence() for request in self.requests],
            "escaped_requests": [list(argv) for argv in self.escapes],
        }

    def assert_installed(self) -> None:
        """Both call sites and the process boundary must still be the stub."""
        bound = getattr(self, "_bound", {})
        if sent_indexer.get_single_email_details is not bound.get("details"):
            raise HarnessError("core.sent_indexer.get_single_email_details is not stubbed")
        if sent_indexer.run_himalaya is not bound.get("run"):
            raise HarnessError("core.sent_indexer.run_himalaya is not stubbed")
        if himalaya.run_himalaya is not bound.get("run"):
            raise HarnessError("core.himalaya.run_himalaya is not stubbed")
        if himalaya._run_process is not bound.get("tripwire"):
            raise HarnessError("the core.himalaya process boundary is not tripwired")

    def assert_no_real_transport(self) -> None:
        """Prove that no real ``run_himalaya``/process launch could have happened."""
        self.assert_installed()
        if self.escapes:
            raise HarnessError(
                f"a real transport call escaped the stub: {self.escapes[:3]}"
            )

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _option(argv: Sequence[str], flag: str) -> str | None:
        for index, part in enumerate(argv):
            if part == flag and index + 1 < len(argv):
                return argv[index + 1]
        return None

    @classmethod
    def _option_int(cls, argv: Sequence[str], flag: str, *, default: int) -> int:
        value = cls._option(argv, flag)
        try:
            return int(str(value))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _date_token(argv: Sequence[str]) -> str | None:
        for index, part in enumerate(argv):
            if part == "date" and index + 1 < len(argv):
                return argv[index + 1]
            if part.startswith("date ") and len(part) > len("date "):
                return part[len("date "):].strip()
        return None

    @staticmethod
    def _export_envelope(argv: Sequence[str]) -> str | None:
        for index, part in enumerate(argv):
            if part == "export" and index + 1 < len(argv):
                return argv[index + 1]
        return None

    def _read_output(self, env_id: str) -> str:
        payload = self.details.get(env_id) or {}
        mid = str(payload.get("raw_message_id") or payload.get("message_id") or f"<{env_id}@example.org>")
        headers = [
            f"Message-Id: {mid}",
            "In-Reply-To: ",
            "From: Synthetic Sender <sender@example.org>",
            "To: Synthetic Recipient <recipient@example.org>",
            "Cc: ",
            "Date: Mon, 02 Nov 2026 08:00:00 +0100",
            "Subject: Synthetic subject",
        ]
        return "\n".join([*headers, "", self.read_body, ""])


# ---------------------------------------------------------------------------
# CLI drivers
# ---------------------------------------------------------------------------

def run_cli(*args: str) -> CliResult:
    """Run the ledger CLI in process and parse its canonical envelope."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        exit_code = ledger_cli.main(list(args))
    raw = out.getvalue().strip()
    if not raw:
        raise HarnessError("the ledger CLI emitted no envelope")
    return CliResult(exit_code=exit_code, envelope=json.loads(raw))


def run_cli_process(*args: str) -> subprocess.CompletedProcess[str]:
    """Run the installed CLI entry point in a child process (capability surface)."""
    return subprocess.run(
        [sys.executable, str(ledger_cli_path()), *args],
        capture_output=True, text=True, check=False, timeout=120,
    )


def pending(workspace: SyntheticWorkspace, *extra: str) -> CliResult:
    return run_cli(
        "pending", "--workspace", str(workspace.root),
        "--data-dir", "data/mail-desk", *extra,
    )


def record(
    workspace: SyntheticWorkspace,
    *extra: str,
    message_id: str | None = None,
    item_ref: str | None = None,
    lease_id: str | None = None,
) -> CliResult:
    selector: list[str] = []
    if message_id is not None:
        selector = ["--message-id", message_id]
    elif item_ref is not None:
        selector = ["--item-ref", item_ref]
    lease = ["--workspace-lease-id", lease_id] if lease_id is not None else []
    return run_cli(
        "record", "--workspace", str(workspace.root),
        "--data-dir", "data/mail-desk", *selector, *lease, *extra,
    )


def consume(
    workspace: SyntheticWorkspace,
    evaluations: Path,
    *extra: str,
    lease_id: str | None = None,
) -> CliResult:
    lease = ["--workspace-lease-id", lease_id] if lease_id is not None else []
    return run_cli(
        "consume", "--workspace", str(workspace.root),
        "--data-dir", "data/mail-desk", "--evaluations", str(evaluations),
        *lease, *extra,
    )


def crash_check(
    workspace: SyntheticWorkspace,
    *,
    message_id: str,
    vorgang_ref: str,
    evid_id: str,
    evidence_file: Path | None = None,
) -> CliResult:
    """Read-only exact-triad classification (``unique``/``conflict``/``absent``)."""
    evidence = ["--evidence-file", str(evidence_file)] if evidence_file is not None else []
    return run_cli(
        "crash-check", "--workspace", str(workspace.root),
        "--data-dir", "data/mail-desk", "--message-id", message_id,
        "--vorgang-ref", vorgang_ref, "--evid-id", evid_id, *evidence,
    )


def recover_promotion(
    workspace: SyntheticWorkspace,
    *,
    message_id: str,
    vorgang_ref: str,
    evid_id: str,
    evidence_file: Path,
    lease_id: str | None = None,
) -> CliResult:
    """Mutating recovery; the caller proves the crash-check gate before calling it."""
    lease = ["--workspace-lease-id", lease_id] if lease_id is not None else []
    return run_cli(
        "recover-promotion", "--workspace", str(workspace.root),
        "--data-dir", "data/mail-desk", "--message-id", message_id,
        "--vorgang-ref", vorgang_ref, "--evid-id", evid_id,
        "--evidence-file", str(evidence_file), *lease,
    )


def legacy_plan(workspace: SyntheticWorkspace, inventory: Path) -> CliResult:
    """Read-only legacy-import dry run (``plan``) without a lease."""
    return run_cli(
        "plan", "--workspace", str(workspace.root),
        "--data-dir", "data/mail-desk", "--inventory", str(inventory),
    )


def legacy_apply(
    workspace: SyntheticWorkspace,
    inventory: Path,
    *,
    approval: str,
    lease_id: str | None = None,
) -> CliResult:
    """Approved legacy import (``apply``); needs a verified lease before any write."""
    lease = ["--workspace-lease-id", lease_id] if lease_id is not None else []
    return run_cli(
        "apply", "--workspace", str(workspace.root),
        "--data-dir", "data/mail-desk", "--inventory", str(inventory),
        "--approval", approval, *lease,
    )


def inventory_legacy(workspace: SyntheticWorkspace, inventory: Path) -> CliResult:
    """Read-only legacy-inventory report (declared F21 tool)."""
    return run_cli(
        "inventory-legacy", "--workspace", str(workspace.root),
        "--data-dir", "data/mail-desk", "--inventory", str(inventory),
    )


# ---------------------------------------------------------------------------
# Capability probe (P1)
# ---------------------------------------------------------------------------

HANDOFF_SYMBOLS = (
    "load_pending_handoff",
    "pending_handoff_view",
    "record_synthesized",
    "record_no_evidence",
    "peek_handoff_state",
)
LEDGER_SYMBOLS = (
    "build_legacy_import_plan",
    "apply_legacy_import",
    "recover_promotion",
    "build_pending_view",
    "record_pending_outcome",
    "consume_evaluated_items",
    "read_sent_evaluations",
    "check_crash_triad",
)
ALL_LEDGER_SUBCOMMANDS = ("plan", "apply", "recover-promotion", *LEDGER_SUBCOMMANDS)


@dataclass(frozen=True)
class CapabilityProbe:
    """P1 probe result: symbol presence, schema version and CLI surface."""

    symbols: dict[str, bool]
    ledger_schema_version: int | None
    ledger_outcomes: tuple[str, ...]
    ledger_sources: tuple[str, ...]
    cli_commands: tuple[str, ...]
    cli_json_flag: dict[str, bool]
    install_commit: str | None
    harness_version: str = HARNESS_VERSION

    @property
    def missing_symbols(self) -> tuple[str, ...]:
        return tuple(sorted(name for name, present in self.symbols.items() if not present))

    @property
    def missing_commands(self) -> tuple[str, ...]:
        return tuple(name for name in ALL_LEDGER_SUBCOMMANDS if name not in self.cli_commands)

    @property
    def ok(self) -> bool:
        return (
            not self.missing_symbols
            and not self.missing_commands
            and self.ledger_schema_version == sent_handoff.LEDGER_SCHEMA_VERSION
            and all(self.cli_json_flag.get(name, False) for name in LEDGER_SUBCOMMANDS)
        )

    def as_evidence(self) -> dict[str, Any]:
        """Booleans, counters and the installed commit; no paths."""
        return {
            "harness_kind": HARNESS_KIND,
            "harness_version": self.harness_version,
            "symbols": dict(self.symbols),
            "missing_symbols": list(self.missing_symbols),
            "ledger_schema_version": self.ledger_schema_version,
            "ledger_outcomes": list(self.ledger_outcomes),
            "ledger_sources": list(self.ledger_sources),
            "cli_commands": list(self.cli_commands),
            "missing_commands": list(self.missing_commands),
            "cli_json_flag": dict(self.cli_json_flag),
            "install_commit": self.install_commit,
            "ok": self.ok,
        }


def _probed_modules() -> tuple[Any, Any]:
    """Ensure the probed modules come from this installation, not another copy."""
    expected = (MAIL_DESK_ROOT / "scripts").resolve()
    for module in (sent_handoff, sent_ledger):
        module_file = Path(getattr(module, "__file__", "")).resolve()
        if expected not in module_file.parents:
            raise HarnessError(
                f"module {module.__name__} was imported from outside the probed "
                "installation; the capability probe would be meaningless"
            )
    return sent_handoff, sent_ledger


def capability_probe(*, probe_cli: bool = True) -> CapabilityProbe:
    """Run the P1 probe against the installed copy and the CLI entry point."""
    handoff_module, ledger_module = _probed_modules()
    symbols = {
        name: callable(getattr(handoff_module, name, None)) for name in HANDOFF_SYMBOLS
    }
    symbols.update({
        name: callable(getattr(ledger_module, name, None)) for name in LEDGER_SYMBOLS
    })
    commands = declared_subcommands()
    json_flag: dict[str, bool] = {}
    if probe_cli:
        top = run_cli_process("--help")
        if top.returncode != 0:
            raise HarnessError(f"the ledger CLI --help failed: {top.stderr.strip()}")
        for name in LEDGER_SUBCOMMANDS:
            completed = run_cli_process(name, "--help")
            text = completed.stdout + completed.stderr
            json_flag[name] = completed.returncode == 0 and "--json" in text
    return CapabilityProbe(
        symbols=symbols,
        ledger_schema_version=getattr(handoff_module, "LEDGER_SCHEMA_VERSION", None),
        ledger_outcomes=tuple(getattr(handoff_module, "LEDGER_OUTCOMES", ())),
        ledger_sources=tuple(getattr(handoff_module, "LEDGER_SOURCES", ())),
        cli_commands=commands,
        cli_json_flag=json_flag,
        install_commit=_git_head(repository_root()),
    )


def declared_subcommands() -> tuple[str, ...]:
    """Subcommands the installed CLI parser declares (no subprocess, no state)."""
    parser = ledger_cli._parser()  # declared CLI surface of this entry point
    for action in parser._actions:  # noqa: SLF001 - argparse has no public accessor
        choices = getattr(action, "choices", None)
        if isinstance(choices, Mapping):
            return tuple(choices)
    return ()


# ---------------------------------------------------------------------------
# Fixture assertions
# ---------------------------------------------------------------------------

def assert_no_host_paths(payload: Any) -> None:
    """No emitted value may contain an absolute host path fragment."""
    text = json.dumps(payload, ensure_ascii=False)
    for fragment in ("D:\\", "C:\\", "/home/", "/Users/", "/private/", "/tmp/"):
        if fragment in text:
            raise HarnessError(f"evidence contains a host path fragment {fragment!r}")


def item_ref(message_id: str) -> str:
    """Documented stable ``sha256:`` reference of one normalized Message-ID."""
    return "sha256:" + hashlib.sha256(norm_id(message_id).encode("utf-8")).hexdigest()[:16]


def synthetic_id(kind: str, index: int) -> str:
    """Deterministic synthetic Message-ID local part on ``example.org``."""
    return f"{kind}{index}@example.org"


__all__ = [
    "ACCOUNT_A",
    "ACCOUNT_B",
    "ALL_LEDGER_SUBCOMMANDS",
    "CapabilityProbe",
    "CaseEvidence",
    "CaseRunner",
    "CliResult",
    "DECLARED_TOOLS",
    "CONSUMER_RECOVERY_GATE",
    "F18_VARIANTS",
    "G1_REMAINING_CASE_IDS",
    "HARNESS_CASE_IDS",
    "HARNESS_KIND",
    "HARNESS_VERSION",
    "HarnessError",
    "InjectedLease",
    "LEDGER_SUBCOMMANDS",
    "LeaseBase",
    "RealLease",
    "SESSION_LOCK",
    "SYNTH_DAY",
    "SYNTH_DAY_NEXT",
    "SYNTH_FOLDER",
    "SYNTHETIC_EML",
    "SyntheticWorkspace",
    "TransportEscapeError",
    "TransportRequest",
    "TransportStub",
    "assert_no_host_paths",
    "capability_probe",
    "consume",
    "crash_check",
    "declared_subcommands",
    "handoff_item",
    "inventory_legacy",
    "item_ref",
    "legacy_apply",
    "legacy_plan",
    "ledger_cli_path",
    "ledger_line",
    "link_kind",
    "make_directory_link",
    "norm_id",
    "pending",
    "record",
    "recover_promotion",
    "repository_root",
    "require_workspace_lock_root",
    "run_case",
    "run_cli",
    "run_cli_process",
    "synthetic_detail",
    "synthetic_envelope",
    "synthetic_id",
    "workspace_lock_cli",
    "workspace_lock_root",
]
