"""Issue #107: sent-ledger outcomes, legacy import and crash recovery.

This module is deliberately a thin, deterministic layer on top of
:mod:`core.sent_handoff`:

* It never invents synthesis evidence.  A ``no_evidence`` line consumes an item
  with a reason code and no Vorgang/EVID/thread anchor; an imported synthesis
  requires the full durable promotion triad (Vorgang + EVID-ID).
* The legacy import is a two-stage operation.  ``build_legacy_import_plan`` is a
  read-only dry run whose deterministic ``plan_id`` binds to the normalized
  inventory and the *current* ledger/handoff bytes.  ``apply_legacy_import``
  accepts only that exact token; a changed inventory, ledger, handoff or a
  conflict blocks before the first append.
* ``recover_promotion`` compares the observed durable promotion evidence and the
  ledger by the normalized Message-ID plus the exact ``vorgang_ref`` and
  ``evid_id`` tokens.  Exactly one evidence match and no conflicting ledger line
  append at most one ledger line; partial or ambiguous matches stop.
* ``consume_evaluated_items`` is the batch consumer path: it appends only items
  the consumer has already evaluated (``synthesized``/``no_evidence``) and leaves
  every other item open as ``not_evaluated``.  Each append reuses
  ``record_pending_outcome``, so the same lease, binding and anchor gates hold.

No mailbox, network, LLM or consumer-document access happens here.  Mutating
operations require an invocation-owned ``workspace-lock`` lease.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .common import normalize_message_id
from .quarantine.attachment_fetch import verify_workspace_lock
from .sent_handoff import (
    HANDOFF_FILENAME,
    LEDGER_FILENAME,
    LEDGER_OUTCOMES,
    LEDGER_SCHEMA_VERSION,
    LEDGER_SOURCES,
    MAX_HANDOFF_ITEMS,
    SentHandoffError,
    SentLedgerConflict,
    append_ledger_records,
    assert_append_aliases,
    is_reason_code,
    is_reference_token,
    ledger_outcome,
    ledger_source,
    load_ledger,
    pending_handoff_view,
    record_outcome,
    read_handoff,
    resolve_thread_anchor,
    thread_anchor_ids,
)
from .sent_handoff import load_pending_handoff
from .sent_handoff import norm_id, unconsumed
from .sent_handoff import (
    _identity_compatible,
    _record_identity,
)

INVENTORY_KIND = "mail-desk.sent-ledger.legacy-inventory"
INVENTORY_SCHEMA_VERSION = 1
PLAN_KIND = "mail-desk.sent-ledger.legacy-import-plan"
PLAN_VERSION = 1
PENDING_VIEW_KIND = "mail-desk.sent-ledger.pending-view"
PENDING_VIEW_VERSION = 1
CRASH_CHECK_KIND = "mail-desk.sent-ledger.crash-check"
CRASH_CHECK_VERSION = 1
EVALUATION_KIND = "mail-desk.sent-evaluation"
EVALUATION_SCHEMA_VERSION = 1
CONSUME_KIND = "mail-desk.sent-ledger.consume"
CONSUME_VERSION = 1

#: Stable correlation handle for one normalized Message-ID: a truncated SHA-256.
#: It is **not** anonymization and no irreversibility is claimed; it is personal
#: data and only keeps raw identifiers out of routine reports.
_ITEM_REF_PATTERN = re.compile(r"^sha256:[0-9a-f]{16}$")
_ITEM_REF_HEX_CHARS = 16

INVENTORY_FIELDS = frozenset({
    "schema_version", "message_id", "status", "vorgang_ref", "evid_id",
    "reason_code", "recorded_at", "thread_key",
})
INVENTORY_STATUSES = ("synthesized", "no_evidence")

#: Documented ``no_evidence`` reason codes.  The schema accepts any bounded
#: ``[a-z][a-z0-9_]{1,63}`` token; these are the canonical choices.
NO_EVIDENCE_REASON_CODES = (
    "no_matching_vorgang",
    "no_relevant_evidence",
    "thread_unresolved",
    "source_unavailable",
    "headers_incomplete",
    "conflicting_evidence",
    "legacy_status_no_evidence",
)

#: Strict shape of one consumer evaluation entry.  The consumer's content step
#: owns the fachliche Entscheidung; this document only transports its result.
_EVALUATION_FIELDS = frozenset({
    "item_ref", "message_id", "outcome", "vorgang_ref", "evid_id", "reason_code",
})
_EVALUATION_OUTCOMES = ("synthesized", "no_evidence", "not_evaluated")
_EVALUATION_ANCHOR_FIELDS = ("vorgang_ref", "evid_id", "reason_code")

#: Per-item reasons that keep exactly that item open.  Every other error is an
#: infrastructure/workspace failure and stops the whole round.
_CONSUME_ITEM_BLOCK_REASONS = frozenset({
    "anchor_conflict",
    "anchor_ambiguous",
    "reference_anchor_unresolved",
    "handoff_metadata_incomplete",
    "evid_shared_conflict",
    "ledger_conflict",
    "not_pending",
    "ambiguous_item_ref",
})


class SentLedgerError(SentHandoffError):
    """Fail-closed refusal of an outcome, import or recovery operation."""


def _iso_timestamp_ok(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _file_sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _workspace_fingerprint(workspace: Path) -> str:
    """Stable target identity for approval binding without exposing the path."""
    return "sha256:" + hashlib.sha256(str(workspace).encode("utf-8")).hexdigest()


def _inventory_error(line: int, reason: str) -> SentLedgerError:
    return SentLedgerError(
        "legacy_inventory_invalid",
        f"Legacy inventory line {line} is invalid ({reason}); nothing was planned or "
        "written. Recovery: correct the inventory record and rerun the dry run.",
        {"invalid_line": line, "reason": reason, "state": "invalid"},
    )


def _normalize_inventory_record(raw: Any, line: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise _inventory_error(line, "not_a_json_object")
    unknown = sorted(set(raw) - INVENTORY_FIELDS)
    if unknown:
        raise _inventory_error(line, f"unknown_field:{unknown[0]}")
    for name in ("schema_version", "message_id", "status", "recorded_at"):
        if name not in raw:
            raise _inventory_error(line, f"missing_field:{name}")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != INVENTORY_SCHEMA_VERSION:
        raise _inventory_error(line, "unsupported_schema_version")
    status = raw["status"]
    if not isinstance(status, str) or status not in INVENTORY_STATUSES:
        raise _inventory_error(line, "invalid_status")
    raw_mid = raw["message_id"]
    if not isinstance(raw_mid, str) or not raw_mid.strip():
        raise _inventory_error(line, "missing_message_id")
    message_id = normalize_message_id(raw_mid)
    if not message_id:
        raise _inventory_error(line, "missing_message_id")
    if not _iso_timestamp_ok(raw["recorded_at"]):
        raise _inventory_error(line, "invalid_recorded_at")

    vorgang_ref = raw.get("vorgang_ref")
    evid_id = raw.get("evid_id")
    reason_code = raw.get("reason_code")
    thread_key_raw = raw.get("thread_key")
    for name, value in (("vorgang_ref", vorgang_ref), ("evid_id", evid_id)):
        if value is not None and not is_reference_token(value):
            raise _inventory_error(line, f"{name}_invalid")
    if reason_code is not None and not is_reason_code(reason_code):
        raise _inventory_error(line, "reason_code_invalid")
    if thread_key_raw is not None and not isinstance(thread_key_raw, str):
        raise _inventory_error(line, "thread_key_invalid")

    if status == "synthesized":
        # No invented evidence: an imported synthesis needs the full triad.  A
        # missing piece is a reportable conflict, not a silent default.
        problem = None
        if not is_reference_token(vorgang_ref):
            problem = "missing_vorgang_ref"
        elif not is_reference_token(evid_id):
            problem = "missing_evid_id"
        elif reason_code is not None:
            problem = "synthesized_with_reason_code"
    else:
        problem = None
        if not is_reason_code(reason_code):
            problem = "missing_reason_code"
        elif evid_id is not None:
            problem = "no_evidence_with_evid_id"
        elif vorgang_ref is not None:
            problem = "no_evidence_with_vorgang_ref"

    thread_key = norm_id(thread_key_raw) if thread_key_raw else ""
    record = {
        "message_id": message_id,
        "status": status,
        "vorgang_ref": vorgang_ref,
        "evid_id": evid_id,
        "reason_code": reason_code,
        "recorded_at": raw["recorded_at"],
        "thread_key": thread_key,
    }
    if problem is not None:
        record["problem"] = problem
    return record


def read_legacy_inventory(path: Path) -> list[dict[str, Any]]:
    """Read the strictly validated JSONL legacy inventory (no writes)."""
    try:
        data = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise SentLedgerError(
            "legacy_inventory_invalid",
            f"Legacy inventory '{path}' could not be read ({type(error).__name__}); nothing "
            "was planned or written.",
            {"reason": "unreadable", "state": "invalid"},
        ) from error
    records: list[dict[str, Any]] = []
    for number, line in enumerate(data.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except (json.JSONDecodeError, RecursionError) as error:
            raise _inventory_error(number, "not_json") from error
        records.append(_normalize_inventory_record(raw, number))
    if not records:
        raise SentLedgerError(
            "legacy_inventory_invalid",
            f"Legacy inventory '{path}' contains no records; nothing was planned or written.",
            {"reason": "empty", "state": "invalid"},
        )
    return records


def _inventory_identity(record: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        record["status"], record.get("vorgang_ref"), record.get("evid_id"),
        record.get("reason_code"), record["recorded_at"], record.get("thread_key", ""),
        record.get("problem"),
    )


def _ledger_identity(record: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        ledger_outcome(record), record.get("vorgang_ref"), record.get("evid_id"),
        record.get("reason_code"),
    )


def _import_identity(record: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        record["status"], record.get("vorgang_ref"), record.get("evid_id"),
        record.get("reason_code"),
    )


def _conflict(message_id: str, code: str, **detail: Any) -> dict[str, Any]:
    return {"message_id": message_id, "code": code, **detail}


def _plan_entries(
    inventory: list[dict[str, Any]],
    ledger: Mapping[str, Any],
    pending_ids: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for record in inventory:
        groups.setdefault(record["message_id"], []).append(record)
    all_by_message_id = ledger["all_by_message_id"]
    ledger_evid_owners: dict[str, set[str]] = {}
    for owner_mid, lines in all_by_message_id.items():
        for line in lines:
            evid = line.get("evid_id")
            if isinstance(evid, str) and evid:
                ledger_evid_owners.setdefault(evid, set()).add(owner_mid)
    inventory_evid_owners: dict[str, set[str]] = {}
    for record in inventory:
        evid = record.get("evid_id")
        if record.get("status") == "synthesized" and isinstance(evid, str) and evid:
            inventory_evid_owners.setdefault(evid, set()).add(record["message_id"])
    entries: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    duplicates = 0
    for message_id in sorted(groups):
        group = groups[message_id]
        problems = [record for record in group if record.get("problem")]
        if problems:
            for record in problems:
                conflicts.append(_conflict(
                    message_id, str(record["problem"]), inventory_count=len(group),
                ))
            continue
        if len({_inventory_identity(record) for record in group}) != 1:
            conflicts.append(_conflict(
                message_id, "duplicate_inventory_conflict", inventory_count=len(group),
            ))
            continue
        if len(group) > 1:
            duplicates += len(group) - 1
        record = group[0]
        evid_id = record.get("evid_id")
        if isinstance(evid_id, str) and evid_id:
            inventory_owners = inventory_evid_owners.get(evid_id, set())
            ledger_owners = ledger_evid_owners.get(evid_id, set())
            if len(inventory_owners) > 1 or bool(ledger_owners - {message_id}):
                conflicts.append(_conflict(
                    message_id, "evid_shared_conflict", evid_id=evid_id,
                    inventory_owners=len(inventory_owners),
                    ledger_owners=len(ledger_owners),
                ))
                continue
        entry = {
            "message_id": message_id,
            "outcome": record["status"],
            "reason_code": record.get("reason_code"),
            "vorgang_ref": record.get("vorgang_ref"),
            "evid_id": record.get("evid_id"),
            "recorded_at": record["recorded_at"],
            "thread_key": record.get("thread_key", ""),
            "source": "legacy_import",
        }
        existing = list(all_by_message_id.get(message_id, []))
        if existing:
            identities = {_ledger_identity(line) for line in existing}
            if len(identities) > 1:
                conflicts.append(_conflict(
                    message_id, "existing_ledger_conflict", ledger_lines=len(existing),
                ))
                continue
            if identities == {_import_identity(record)}:
                entry["action"] = "already_present"
                entries.append(entry)
                continue
            conflicts.append(_conflict(
                message_id, "ledger_conflict", ledger_lines=len(existing),
            ))
            continue
        entry["action"] = "import"
        if message_id in pending_ids:
            entry["pending_handoff"] = True
        entries.append(entry)
    return entries, conflicts, duplicates


def _plan_payload(
    inventory: list[dict[str, Any]],
    entries: list[dict[str, Any]],
    ledger_sha256: str | None,
    handoff_sha256: str | None,
    target_workspace_id: str,
) -> dict[str, Any]:
    return {
        "kind": PLAN_KIND,
        "plan_version": PLAN_VERSION,
        "target_workspace_id": target_workspace_id,
        "inventory_digest": _sha256_json([
            {
                "message_id": record["message_id"],
                "status": record["status"],
                "vorgang_ref": record.get("vorgang_ref"),
                "evid_id": record.get("evid_id"),
                "reason_code": record.get("reason_code"),
                "recorded_at": record["recorded_at"],
                "thread_key": record.get("thread_key", ""),
            }
            for record in inventory
        ]),
        "ledger_sha256": ledger_sha256,
        "handoff_sha256": handoff_sha256,
        "entries": entries,
    }


def build_legacy_import_plan(
    data_dir: Path,
    inventory_path: Path,
    *,
    workspace_root: str | Path | None = None,
) -> dict[str, Any]:
    """Read-only dry run: inventory → normalized IDs → conflicts → plan token.

    The returned ``plan_id`` binds the normalized inventory to the current raw
    ledger and handoff bytes **and** to the target workspace identity.  A token
    approved for workspace A is therefore stale for identical data in workspace
    B.  Only a target hash (``target_workspace_id``) is reported, never an absolute path;
    no report file is written.
    """
    inventory_path = Path(inventory_path)
    workspace, dd = _bind_workspace_data_dir(Path(data_dir), workspace_root)
    target_workspace_id = _workspace_fingerprint(workspace)
    assert_append_aliases(dd)
    ledger = load_ledger(dd)
    handoff = read_handoff(dd)
    pending_ids = {item["norm_message_id"] for item in unconsumed(handoff, ledger)}
    inventory = read_legacy_inventory(inventory_path)
    entries, conflicts, duplicates = _plan_entries(inventory, ledger, pending_ids)
    ledger_path = dd / LEDGER_FILENAME
    handoff_path = dd / HANDOFF_FILENAME
    ledger_sha256 = _file_sha256(ledger_path)
    handoff_sha256 = _file_sha256(handoff_path)
    payload = _plan_payload(
        inventory, entries, ledger_sha256, handoff_sha256, target_workspace_id,
    )
    plan_id = "sha256:" + _sha256_json(payload)
    import_count = sum(1 for entry in entries if entry["action"] == "import")
    already_count = sum(1 for entry in entries if entry["action"] == "already_present")
    blocked = bool(conflicts)
    plan = {
        "ok": not blocked,
        "mode": "sent_ledger_legacy_import",
        "dry_run": True,
        "kind": PLAN_KIND,
        "plan_version": PLAN_VERSION,
        "plan_id": plan_id,
        "target_workspace_id": target_workspace_id,
        "inventory": {
            "name": inventory_path.name,
            "count": len(inventory),
            "digest": _sha256_json([
                _inventory_identity(record) for record in inventory
            ]),
        },
        "summary": {
            "import": import_count,
            "already_present": already_count,
            "conflict": len(conflicts),
            "inventory_duplicates": duplicates,
            "pending_handoff": sum(1 for entry in entries if entry.get("pending_handoff")),
        },
        "entries": entries,
        "conflicts": conflicts,
        "blocked": blocked,
        "state": {
            "ledger_sha256": ledger_sha256,
            "handoff_sha256": handoff_sha256,
        },
        "approval": {
            "required": import_count > 0 and not blocked,
            "token": plan_id if import_count > 0 and not blocked else None,
            "binds": [
                "target_workspace", "normalized_inventory", "ledger_bytes",
                "handoff_bytes", "plan_entries",
            ],
        },
    }
    if blocked:
        plan["next"] = (
            "Resolve every conflict in 'conflicts'; the import is blocked and no approval "
            "token is issued."
        )
    elif import_count:
        plan["next"] = (
            "Review the dry-run entries, then call the apply step with this exact plan_id as "
            "approval while holding the workspace lease."
        )
    else:
        plan["next"] = "Nothing to import; all inventory records are already present."
    return plan


def _link_or_reparse(path: Path) -> str | None:
    """Return ``symlink``/``reparse``/``unverifiable`` for a path component."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        return "unverifiable"
    if stat.S_ISLNK(info.st_mode):
        return "symlink"
    attributes = getattr(info, "st_file_attributes", 0)
    if attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
        return "reparse"
    return None


def _binding_error(message: str, reason: str, **details: Any) -> SentLedgerError:
    return SentLedgerError(
        "workspace_binding_invalid",
        message,
        {"reason": reason, "state": "invalid", **details},
    )


def _bind_workspace_data_dir(
    data_dir: Path,
    workspace_root: str | Path | None,
) -> tuple[Path, Path]:
    """Bind the resolved data target to one verified workspace data zone.

    A lease for workspace A must never authorize a write into workspace B.  The
    resolved ``data/mail-desk`` target must be exactly ``<workspace>/data/mail-desk``
    of the resolved workspace; symlink/junction/reparse components between the
    workspace and the data zone are rejected, and an unverifiable ``lstat`` is
    fail-closed (a not-yet-existing canonical data dir is allowed).  This check
    runs before the lease verifier and before any write.
    """
    raw_data = Path(data_dir).expanduser()
    if workspace_root is not None:
        workspace = Path(workspace_root).expanduser()
        try:
            workspace = workspace.resolve(strict=True)
        except OSError as error:
            raise _binding_error(
                f"The verified workspace cannot be resolved ({type(error).__name__}); "
                "nothing was written.",
                "workspace_unresolvable",
            ) from error
        if not workspace.is_dir():
            raise _binding_error(
                "The verified workspace is not a directory; nothing was written.",
                "workspace_not_a_directory",
            )
    else:
        inferred = raw_data.resolve()
        if inferred.name != "mail-desk" or inferred.parent.name != "data":
            raise _binding_error(
                "A relative or unbound data target requires an explicit workspace root; "
                "the target must be the workspace-local data/mail-desk directory.",
                "data_zone_mismatch",
            )
        workspace = inferred.parent.parent
        try:
            workspace = workspace.resolve(strict=True)
        except OSError as error:
            raise _binding_error(
                f"The inferred workspace cannot be resolved ({type(error).__name__}); "
                "nothing was written.",
                "workspace_unresolvable",
            ) from error

    current = workspace
    for part in Path("data", "mail-desk").parts:
        current = current / part
        kind = _link_or_reparse(current)
        if kind is not None:
            if kind == "unverifiable":
                raise _binding_error(
                    f"The data zone component '{current.name}' could not be verified for "
                    "symlink/junction/reparse aliases; refusing before read/write.",
                    "data_zone_unverifiable",
                    link_kind=kind,
                    component=current.name,
                )
            raise _binding_error(
                f"The data zone contains a {kind} path component at '{current.name}'; "
                "refusing before read/write. Repair or re-bind the workspace manually.",
                "linked_data_zone",
                link_kind=kind,
                component=current.name,
            )

    expected = (workspace / "data" / "mail-desk").resolve()
    bound = (raw_data if raw_data.is_absolute() else workspace / raw_data).resolve()
    if bound != expected or bound.parent.parent != workspace:
        raise _binding_error(
            "The resolved data target is not the verified workspace's data/mail-desk "
            "directory; refusing before read/write (traversal or foreign workspace).",
            "data_zone_mismatch",
        )
    return workspace, bound


def _require_lease(
    *,
    data_dir: Path,
    workspace_root: str | Path | None,
    lease_id: str | None,
    conversation_id: str | None,
    verifier: Callable[..., Any] | None,
) -> tuple[Path, Path]:
    """Verify the lease and return the canonical ``(workspace, data_dir)``.

    The target binding runs first: a valid lease for workspace A never authorizes
    a data_dir in workspace B, a symlinked/junction data zone or a traversal.  All
    mutating callers continue with the returned canonical data directory.
    """
    workspace, bound = _bind_workspace_data_dir(data_dir, workspace_root)
    check = verifier or verify_workspace_lock
    try:
        check(
            workspace,
            lease_id=lease_id,
            conversation_id=conversation_id,
            data_dir=bound,
        )
    except Exception as error:  # noqa: BLE001 - any lock denial is fail-closed
        raise SentLedgerError(
            "workspace_lease_required",
            "The mutating ledger operation requires an invocation-owned, verified "
            f"workspace-lock lease ({type(error).__name__}: {error}); nothing was written.",
            {"reason": "workspace_lease_required", "state": "denied"},
        ) from error
    return workspace, bound


def _record_from_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "message_id": entry["message_id"],
        "vorgang_ref": entry.get("vorgang_ref"),
        "role": "primary",
        "anchored_to": None,
        "recorded_at": entry["recorded_at"],
        "outcome": entry["outcome"],
        "source": "legacy_import",
    }
    thread_key = str(entry.get("thread_key") or "")
    if thread_key:
        record["thread_key"] = thread_key
    if entry["outcome"] == "no_evidence":
        record["reason_code"] = entry["reason_code"]
    else:
        record["evid_id"] = entry["evid_id"]
    return record


def apply_legacy_import(
    data_dir: Path,
    inventory_path: Path,
    *,
    approval: str | None,
    workspace_root: str | Path | None = None,
    lease_id: str | None = None,
    conversation_id: str | None = None,
    verifier: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Apply an approved dry-run plan; idempotent per normalized Message-ID.

    Requires an invocation-owned lease before the first write.  A conflict, a stale
    approval or a changed inventory/ledger/handoff writes nothing.  Re-running after
    a completed import is a safe no-op; re-running after a partial import requires a
    fresh dry run and approval token, then appends only the missing records.
    """
    dd = Path(data_dir)
    _workspace, dd = _require_lease(
        data_dir=dd, workspace_root=workspace_root, lease_id=lease_id,
        conversation_id=conversation_id, verifier=verifier,
    )
    plan = build_legacy_import_plan(dd, inventory_path, workspace_root=_workspace)
    if plan["blocked"]:
        raise SentLedgerError(
            "legacy_import_conflict",
            "The legacy import is blocked by conflicts; nothing was written. "
            "Recovery: resolve the reported conflicts, rerun the dry run and approve only "
            "the resulting plan_id.",
            {"conflicts": plan["conflicts"], "state": "conflict"},
        )
    imports = [entry for entry in plan["entries"] if entry["action"] == "import"]
    if not imports:
        return {
            **plan,
            "dry_run": False,
            "status": "already_complete",
            "applied": 0,
            "ledger_sha256_after": _file_sha256(dd / LEDGER_FILENAME),
        }
    if not approval:
        raise SentLedgerError(
            "legacy_import_approval_required",
            "An explicit approval token is required for the legacy import; nothing was "
            f"written. Use the dry-run plan_id {plan['plan_id']}.",
            {"plan_id": plan["plan_id"], "state": "approval_required"},
        )
    if approval != plan["plan_id"]:
        raise SentLedgerError(
            "legacy_import_approval_stale",
            "The approval token no longer matches the current inventory/ledger/handoff "
            "state; nothing was written. Recovery: rerun the dry run and approve the new "
            f"plan_id {plan['plan_id']}.",
            {"plan_id": plan["plan_id"], "state": "approval_stale"},
        )
    records = [_record_from_entry(entry) for entry in imports]
    try:
        added = append_ledger_records(dd, records)
    except SentLedgerConflict:
        raise
    except Exception as error:  # noqa: BLE001 - append progress may be partial
        raise SentLedgerError(
            "legacy_import_write_failed",
            "The legacy import append failed; the ledger may be partially written. No line "
            "is retried blindly: rerun the dry run, compare already-present records and "
            f"approve the refreshed plan_id ({type(error).__name__}: {error}).",
            {"reason": "append_failed", "state": "unknown"},
        ) from error
    return {
        **plan,
        "dry_run": False,
        "status": "applied",
        "applied": added,
        "ledger_sha256_after": _file_sha256(dd / LEDGER_FILENAME),
    }


def _triad_problem(message_id: Any, vorgang_ref: Any, evid_id: Any) -> str | None:
    if not norm_id(message_id):
        return "missing_message_id"
    if not is_reference_token(vorgang_ref):
        return "missing_vorgang_ref"
    if not is_reference_token(evid_id):
        return "missing_evid_id"
    return None


def recover_promotion(
    data_dir: Path,
    *,
    message_id: str,
    vorgang_ref: str,
    evid_id: str,
    promotion_evidence: Iterable[Mapping[str, Any]],
    workspace_root: str | Path | None = None,
    lease_id: str | None = None,
    conversation_id: str | None = None,
    verifier: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Resume after a consumer crash between durable promotion and ledger append.

    The caller supplies the complete durable promotion evidence it observed.  The
    check is exact: normalized ``message_id``, then exact ``vorgang_ref`` and
    ``evid_id`` tokens.  Exactly one evidence record may match; no match, a partial
    match or more than one match stops.  A missing ledger line is appended only
    after that uniqueness proof; a matching ledger line is a safe no-op; a
    conflicting or duplicated ledger line stops.  At most one line is appended.
    Thread metadata from the handoff item and the matching evidence is type-checked
    (``promotion_metadata_invalid``); an item that still waits for an open ancestor
    in the same handoff is refused with ``ancestor_pending`` before the append, exactly
    like ``record``.  Both checks write nothing.
    """
    dd = Path(data_dir)
    triad = _triad_problem(message_id, vorgang_ref, evid_id)
    if triad is not None:
        raise SentLedgerError(
            "promotion_recovery_incomplete",
            f"Promotion recovery needs the complete triad (message_id + vorgang_ref + "
            f"EVID-ID); {triad} is missing or invalid. Nothing was written.",
            {"reason": triad, "state": "incomplete"},
        )
    mid = norm_id(message_id)
    _workspace, dd = _require_lease(
        data_dir=dd, workspace_root=workspace_root, lease_id=lease_id,
        conversation_id=conversation_id, verifier=verifier,
    )
    evidence = list(promotion_evidence)
    same_message: list[Mapping[str, Any]] = []
    for record in evidence:
        if not isinstance(record, Mapping):
            raise SentLedgerError(
                "promotion_recovery_incomplete",
                "Promotion evidence must be a list of JSON objects; nothing was written.",
                {"reason": "evidence_not_an_object", "state": "incomplete"},
            )
        if norm_id(record.get("message_id")) == mid:
            same_message.append(record)
    if not same_message:
        raise SentLedgerError(
            "promotion_not_found",
            f"No durable promotion evidence matches Message-ID '{mid}'; nothing was written. "
            "Do not promote again until the durable evidence is reconciled.",
            {"message_id": mid, "state": "not_found"},
        )
    if len(same_message) != 1:
        raise SentLedgerError(
            "promotion_ambiguous",
            f"{len(same_message)} durable promotion records match Message-ID '{mid}'; the "
            "triad is ambiguous and nothing was written. Recovery: reconcile the promotion "
            "evidence manually; do not pick one silently.",
            {"message_id": mid, "evidence_count": len(same_message), "state": "ambiguous"},
        )
    for record in evidence:
        if record.get("evid_id") != evid_id:
            continue
        other_mid = norm_id(record.get("message_id"))
        if not other_mid:
            raise SentLedgerError(
                "promotion_recovery_incomplete",
                f"Competing promotion evidence carries EVID-ID '{evid_id}' without a usable "
                "message_id; the triad cannot be proven unique and nothing was written.",
                {"message_id": mid, "reason": "competing_evidence_missing_message_id",
                 "state": "incomplete"},
            )
        if other_mid != mid:
            raise SentLedgerError(
                "evid_shared_conflict",
                f"EVID-ID '{evid_id}' is already bound to a different Message-ID in the "
                "durable promotion evidence; the triad is ambiguous and nothing was written.",
                {"message_id": mid, "state": "conflict"},
            )
    observed = same_message[0]
    if (observed.get("vorgang_ref") != vorgang_ref
            or observed.get("evid_id") != evid_id):
        raise SentLedgerError(
            "promotion_conflict",
            "The durable promotion evidence does not carry the exact requested "
            "vorgang_ref/EVID-ID pair; nothing was written. Recovery: reconcile the partial "
            "match manually.",
            {"message_id": mid, "reason": "partial_triad_match", "state": "conflict"},
        )

    ledger = load_ledger(dd)
    lines = list(ledger["all_by_message_id"].get(mid, []))
    # An EVID-ID is a durable evidence anchor; it must not already belong to a
    # different Message-ID (a unique triad includes a unique EVID anchor).  This
    # runs before the already_consumed no-op: a matching line for this Message-ID
    # must not hide a foreign owner of the same EVID.
    for other_mid, other_lines in ledger["all_by_message_id"].items():
        if other_mid == mid:
            continue
        for other in other_lines:
            if other.get("evid_id") == evid_id:
                raise SentLedgerError(
                    "evid_shared_conflict",
                    f"EVID-ID '{evid_id}' is already anchored to a different Message-ID; "
                    "the recovery triad is ambiguous and nothing was written.",
                    {"message_id": mid, "state": "conflict"},
                )
    handoff_items = read_handoff(dd)
    handoff_item = next(
        (item for item in handoff_items if item["norm_message_id"] == mid), None,
    )
    anchor_item, metadata_problem = _thread_anchor_item(handoff_item, observed, mid)
    if metadata_problem is not None:
        if metadata_problem == "promotion_metadata_invalid":
            metadata_message = (
                "The durable promotion evidence or the handoff carries malformed thread "
                "metadata (in_reply_to/thread_key must be strings, references must be a "
                "list of non-empty strings); nothing was written. Keep the item open and "
                "repair the evidence manually."
            )
            metadata_state = "invalid"
        else:
            metadata_message = (
                "The handoff and durable promotion evidence do not provide a consistent, "
                "complete thread relation; nothing was written. Keep the item open and "
                "reconcile the metadata manually."
            )
            metadata_state = "conflict"
        raise SentLedgerError(
            metadata_problem,
            metadata_message,
            {"message_id": mid, "state": metadata_state},
        )
    if handoff_item is not None or observed is not None:
        anchor_problem = _thread_anchor_problem(anchor_item or {}, ledger, vorgang_ref)
        if anchor_problem is not None:
            reason, details = anchor_problem
            raise SentLedgerError(
                _ANCHOR_REASON_MAP.get(reason, reason),
                "The fresh thread anchor is missing, ambiguous or contradicts the durable "
                f"promotion triad ({_ANCHOR_REASON_MAP.get(reason, reason)}); nothing was "
                "written. Reconcile the ancestor and promotion evidence manually; a "
                "follow-up line must not anchor to another Vorgang and no Vorgang is "
                "derived from a reference-less anchor.",
                {"message_id": mid, **details},
            )

    if len(lines) > 1:
        raise SentLedgerError(
            "ledger_ambiguous",
            f"Ledger '{LEDGER_FILENAME}' holds {len(lines)} lines for Message-ID '{mid}'; "
            "the recovery target is ambiguous and nothing was written.",
            {"message_id": mid, "ledger_lines": len(lines), "state": "ambiguous"},
        )
    if len(lines) == 1:
        if _ledger_identity(lines[0]) == ("synthesized", vorgang_ref, evid_id, None):
            return {
                "ok": True,
                "mode": "sent_ledger_promotion_recovery",
                "status": "already_consumed",
                "message_id": mid,
                "vorgang_ref": vorgang_ref,
                "evid_id": evid_id,
                "appended": 0,
                "ledger_sha256_after": _file_sha256(dd / LEDGER_FILENAME),
            }
        raise SentLedgerError(
            "ledger_conflict",
            f"Ledger '{LEDGER_FILENAME}' already holds a conflicting line for Message-ID "
            f"'{mid}'; nothing was written. First-line-wins is a read compatibility and "
            "never resolves this conflict.",
            {"message_id": mid, "state": "conflict"},
        )

    # The unique-EVID and anchor checks ran before the already_consumed/ledger-conflict
    # branch.  The checked thread relation is also the line's relation.
    waits = _pending_waits_for(
        handoff_items, ledger, mid, (anchor_item or {}).get("anchor_candidates") or [],
    )
    if waits:
        # Same fail-closed gate as ``record``: an append here would anchor before
        # the ancestor has a ledger line, so the fresh anchor is not yet stable.
        raise SentLedgerError(
            "ancestor_pending",
            "An ancestor of the message is still an open handoff item; consume the "
            "ancestor first. Nothing was written.",
            {
                "message_id": mid,
                "waits_for": [_ref_token(wait) for wait in waits],
                "state": "blocked",
            },
        )
    item: dict[str, Any] = dict(anchor_item or {})
    item.update({
        "message_id": mid,
        "vorgang_ref": vorgang_ref,
        "evid_id": evid_id,
    })
    try:
        added = record_outcome(dd, [item], outcome="synthesized")
    except SentLedgerConflict:
        raise
    except Exception as error:  # noqa: BLE001 - append progress is unverified
        raise SentLedgerError(
            "promotion_recovery_write_failed",
            "The recovery append failed; the ledger may be partially written. Rerun the "
            f"recovery check before any new promotion ({type(error).__name__}: {error}).",
            {"reason": "append_failed", "state": "unknown"},
        ) from error
    return {
        "ok": True,
        "mode": "sent_ledger_promotion_recovery",
        "status": "recovered",
        "message_id": mid,
        "vorgang_ref": vorgang_ref,
        "evid_id": evid_id,
        "appended": added,
        "ledger_sha256_after": _file_sha256(dd / LEDGER_FILENAME),
    }


def _ref_token(value: str) -> str:
    """Truncated SHA-256 correlation reference for one identifier (not anonymization)."""
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return "sha256:" + digest[:_ITEM_REF_HEX_CHARS]


def _item_ref(value: Any) -> str:
    return _ref_token(norm_id(value))


def _ledger_composition(ledger: Mapping[str, Any]) -> tuple[int, int, dict[str, int], dict[str, int]]:
    """Line count, duplicate Message-ID count, outcome and source histograms."""
    outcomes = {name: 0 for name in LEDGER_OUTCOMES}
    sources = {name: 0 for name in LEDGER_SOURCES}
    lines = 0
    duplicates = 0
    for group in ledger["all_by_message_id"].values():
        lines += len(group)
        if len(group) > 1:
            duplicates += 1
        for line in group:
            outcomes[ledger_outcome(line)] = outcomes.get(ledger_outcome(line), 0) + 1
            sources[ledger_source(line)] = sources.get(ledger_source(line), 0) + 1
    return lines, duplicates, outcomes, sources


def _consumed_composition(
    items: list[dict[str, Any]],
    ledger: Mapping[str, Any],
    pending_ids: set[str],
) -> tuple[dict[str, int], dict[str, int]]:
    """Outcome/source histogram of handoff items already consumed by a ledger line.

    ``no_evidence`` and ``legacy_import`` are ordinary consumption states here: the
    item leaves the handoff regardless of outcome or provenance.  The first valid
    ledger line per Message-ID is the documented read view.
    """
    outcomes = {name: 0 for name in LEDGER_OUTCOMES}
    sources = {name: 0 for name in LEDGER_SOURCES}
    by_message_id = ledger["by_message_id"]
    for item in items:
        mid = item["norm_message_id"]
        if mid in pending_ids:
            continue
        line = by_message_id.get(mid)
        if line is None:  # defensive: unconsumed() and the index agree by construction
            continue
        outcomes[ledger_outcome(line)] = outcomes.get(ledger_outcome(line), 0) + 1
        sources[ledger_source(line)] = sources.get(ledger_source(line), 0) + 1
    return outcomes, sources


def _pending_item_view(item: Mapping[str, Any]) -> dict[str, Any]:
    """Structural metadata for one open item without raw mail content or raw IDs."""
    anchor = item.get("anchored_to")
    anchor_view: dict[str, Any] | None = None
    if isinstance(anchor, Mapping) and anchor:
        anchor_view = {
            "vorgang_ref": anchor.get("vorgang_ref"),
            "matched_ref": _item_ref(anchor.get("matched_message_id")),
            "match_type": anchor.get("match_type"),
        }
    waits = [norm_id(wait) for wait in (item.get("waits_for") or [])]
    own = norm_id(item.get("norm_message_id") or item.get("message_id"))
    thread = norm_id(item.get("thread_key"))
    return {
        "item_ref": _item_ref(own),
        # The handoff sets thread_key to the item's own Message-ID when no real
        # ancestor exists; that adds no information beyond item_ref.
        "thread_ref": _item_ref(thread) if thread and thread != own else None,
        "headers_complete": bool(item.get("headers_complete")),
        "truncated_fields": list(item.get("truncated_fields") or []),
        "anchored": anchor_view is not None,
        "anchor": anchor_view,
        "waits_for": [_ref_token(wait) for wait in waits],
        "waits_for_count": len(waits),
    }


def _verify_pending_account(
    data_dir: Path,
    items: list[dict[str, Any]],
    requested_account: str | None,
) -> str | None:
    """Optional account/mailbox gate for the whole open round.

    ``load_pending_handoff`` does not know an account and never validates
    ``item.mailbox``.  When the caller passes ``--account`` the workspace backend
    binding (``.agents/mail-desk-backend.json``) is authoritative; one foreign item
    stops the whole round before any text read, promotion or ledger append.  The
    report carries only truncated hash references (not anonymization).
    """
    if requested_account is None:
        return None
    from .readiness import bind_workspace_account  # local import: keep this module light

    binding = bind_workspace_account(data_dir, requested_account=requested_account)
    if not binding.get("ok"):
        raise SentLedgerError(
            str(binding.get("reason_code") or "workspace_config_invalid"),
            f"The workspace account binding is not usable ({binding.get('reason_code')}: "
            f"{binding.get('message')}); nothing was written.",
            {"reason": str(binding.get("reason_code") or "workspace_config_invalid"),
             "state": "invalid"},
        )
    account = binding.get("account")
    if not isinstance(account, str) or not account.strip():
        raise SentLedgerError(
            "verified_account_required",
            "The workspace backend binds no explicit account; a ledger consumption "
            "without a verified account is not allowed. Nothing was written.",
            {"reason": "verified_account_required", "state": "invalid"},
        )
    mismatched = [
        item for item in items
        if str(item.get("mailbox") or "") != account
    ]
    if mismatched:
        raise SentLedgerError(
            "mailbox_mismatch",
            "At least one open handoff item is not bound to the verified account; the "
            "whole round stops before any text read, promotion or ledger line. Nothing "
            "was written.",
            {
                "reason": "mailbox_mismatch",
                "verified_account": account,
                "mismatch_count": len(mismatched),
                "mismatch_refs": [
                    _item_ref(item.get("norm_message_id") or item.get("message_id"))
                    for item in mismatched
                ],
                "state": "invalid",
            },
        )
    return account


def build_pending_view(
    data_dir: Path,
    *,
    workspace_root: str | Path | None = None,
    requested_account: str | None = None,
) -> dict[str, Any]:
    """Read-only consumer view of the open ledger/handoff items.

    The payload carries counters and truncated hash references (``sha256:`` prefix
    of the normalized Message-ID; not anonymization) only: no subject, address,
    date, envelope id or raw Message-ID.  A ``no_evidence`` line and a ``legacy_import`` line consume an item
    exactly like a runtime synthesis line; both are counted separately and a
    ``no_evidence`` line never resolves a thread anchor.  Missing and empty handoff
    files are distinguished (``handoff_present``/``handoff_item_count``) even though
    both report ``handoff_state: none`` with zero pending items.
    """
    _workspace, dd = _bind_workspace_data_dir(Path(data_dir), workspace_root)
    assert_append_aliases(dd)
    ledger = load_ledger(dd)
    items = read_handoff(dd)
    ordered = pending_handoff_view(items, ledger)
    verified_account = _verify_pending_account(dd, ordered, requested_account)

    pending_ids = {item["norm_message_id"] for item in ordered}
    consumed_outcomes, consumed_sources = _consumed_composition(items, ledger, pending_ids)
    line_count, duplicate_ids, outcomes, sources = _ledger_composition(ledger)
    pending_views = [_pending_item_view(item) for item in ordered]
    pending_count = len(pending_views)
    return {
        "ok": True,
        "mode": "sent_ledger_pending",
        "kind": PENDING_VIEW_KIND,
        "view_version": PENDING_VIEW_VERSION,
        "read_only": True,
        "status": "pending" if pending_count else "empty",
        "handoff_state": "pending" if pending_count else "none",
        "handoff_present": (dd / HANDOFF_FILENAME).exists(),
        "handoff_item_count": len(items),
        "pending_count": pending_count,
        "consumed_ignored": len(items) - pending_count,
        "verified_account": verified_account,
        "counts": {
            "pending": pending_count,
            "consumed_ignored": len(items) - pending_count,
            "with_anchor": sum(1 for item in pending_views if item["anchored"]),
            "without_anchor": sum(1 for item in pending_views if not item["anchored"]),
            "with_pending_ancestors": sum(1 for item in pending_views if item["waits_for_count"]),
            "headers_incomplete": sum(1 for item in pending_views if not item["headers_complete"]),
            "truncated": sum(1 for item in pending_views if item["truncated_fields"]),
        },
        "consumed_outcomes": consumed_outcomes,
        "consumed_sources": consumed_sources,
        "ledger": {
            "lines": line_count,
            "duplicate_message_ids": duplicate_ids,
            "outcomes": outcomes,
            "sources": sources,
            "torn_tail": bool(ledger["torn_tail"]),
            "sha256": _file_sha256(dd / LEDGER_FILENAME),
        },
        "items": pending_views,
        "next": (
            "Consume the evaluated items in one lease-bound 'consume' round (or one item "
            "through 'record'); the handoff file is never rewritten."
            if pending_count
            else "No open ledger item; the handoff is known empty."
        ),
    }


def _anchor_candidate_ids(item: Mapping[str, Any]) -> list[str]:
    """Normalized ancestor IDs plus the thread key, without duplicates."""
    candidates = [norm_id(candidate) for candidate in (item.get("anchor_candidates") or [])]
    thread = norm_id(item.get("thread_key"))
    if thread:
        candidates.append(thread)
    return list(dict.fromkeys(candidate for candidate in candidates if candidate))


def _pending_waits_for(
    items: list[dict[str, Any]],
    ledger: Mapping[str, Any],
    message_id: str,
    anchor_candidates: Iterable[Any] = (),
) -> list[str]:
    """Open ancestors of one handoff-related message (empty when none are open).

    The target's own handoff entry is authoritative when it is still unconsumed
    (same ancestor-first view as ``record``).  When the target is not listed (an
    evidence-only recovery after a replaced/missing handoff), the passed evidence
    anchor candidates are checked against the currently pending handoff items, so
    an open ancestor can never slip through the gate.  A consumed or unrelated
    item has no pending ancestor.
    """
    pending = pending_handoff_view(items, ledger)
    pending_ids = {entry["norm_message_id"] for entry in pending}
    entry = next(
        (entry for entry in pending if entry["norm_message_id"] == message_id),
        None,
    )
    candidates = (
        entry.get("anchor_candidates") if entry is not None
        else list(anchor_candidates)
    )
    return [
        candidate for candidate in dict.fromkeys(norm_id(value) for value in candidates or [])
        if candidate and candidate in pending_ids
    ]


def _thread_anchor_problem(
    item: Mapping[str, Any],
    ledger: Mapping[str, Any],
    chosen_ref: str,
) -> tuple[str, dict[str, Any]] | None:
    """Conservative fresh thread-anchor check shared by every consuming path.

    Checks **all** ledger lines of every ancestor candidate and of the resolved
    anchor, not the first-line-wins view: two divergent lines for one ancestor
    Message-ID are ``anchor_ambiguous``.  A resolved ``synthesized`` ancestor
    without a Vorgang is ``reference_anchor_unresolved`` (no follow-up Vorgang is
    invented), and any non-empty anchor Vorgang that differs from the chosen one is
    ``anchor_conflict``.  ``handoff_metadata_incomplete`` stops when the caller
    supplied an item whose headers are known incomplete.
    """
    if item.get("headers_complete") is False:
        return "handoff_metadata_incomplete", {"state": "incomplete"}
    candidates = _anchor_candidate_ids(item)
    ambiguous: list[str] = []
    for candidate in candidates:
        lines = ledger["all_by_message_id"].get(candidate, [])
        if len({_ledger_identity(line) for line in lines}) > 1:
            ambiguous.append(_ref_token(candidate))
    anchor = resolve_thread_anchor(dict(item), ledger)
    if anchor is not None:
        matched = norm_id(anchor.get("matched_message_id"))
        lines = ledger["all_by_message_id"].get(matched, [])
        if len({_ledger_identity(line) for line in lines}) > 1:
            ambiguous.append(_ref_token(matched))
    if ambiguous:
        return "anchor_ambiguous", {
            "ambiguous_refs": sorted(set(ambiguous)), "state": "conflict",
        }
    if anchor is None:
        return None
    reference = anchor.get("vorgang_ref")
    if not (isinstance(reference, str) and reference):
        return "reference_anchor_unresolved", {
            "anchor_ref": _ref_token(anchor.get("matched_message_id")),
            "state": "conflict",
        }
    references = [reference]
    for candidate in candidates:
        for line in ledger["all_by_message_id"].get(candidate, []):
            if ledger_outcome(line) != "synthesized":
                continue
            candidate_ref = line.get("vorgang_ref")
            if isinstance(candidate_ref, str) and candidate_ref and candidate_ref not in references:
                references.append(candidate_ref)
    conflicting = sorted({value for value in references if value != chosen_ref})
    if conflicting:
        return "anchor_conflict", {
            "conflicting_refs": conflicting, "state": "conflict",
        }
    return None


def _thread_anchor_item(
    handoff_item: Mapping[str, Any] | None,
    evidence: Mapping[str, Any] | None,
    message_id: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Build the anchor-check item from handoff metadata plus durable evidence.

    The handoff item is authoritative when present; a declared incomplete header set
    stops as ``handoff_metadata_incomplete``.  Non-empty evidence metadata may fill
    gaps but must not contradict the handoff (``promotion_metadata_conflict``).  Every
    optional thread field is type-checked before normalization: ``in_reply_to`` and
    ``thread_key`` must be strings (or absent/empty), ``references`` must be a list
    of non-empty strings.  A wrong shape is ``promotion_metadata_invalid`` and is
    never coerced through ``norm_id`` (a string ``references`` value would otherwise
    be iterated character by character).  When neither a handoff item nor evidence
    thread metadata exists, the message is treated as a primary/root line (no anchor
    is claimed); no foreign anchor can be asserted without metadata.
    """
    if handoff_item is not None and handoff_item.get("headers_complete") is False:
        return None, "handoff_metadata_incomplete"

    def metadata_id(value: Any) -> tuple[bool, str]:
        """Strict optional Message-ID field: absent, exactly empty, or a real id."""
        if value is None:
            return True, ""
        if not isinstance(value, str):
            return False, ""
        normalized = norm_id(value)
        if not normalized:
            # Only the literal empty string means "no relation"; whitespace-only
            # or a bracket-only token is malformed and must not be coerced.
            return (value == ""), ""
        return True, normalized

    def metadata_references(value: Any) -> tuple[bool, list[str]]:
        """Strict optional references: a list of non-empty strings, never a string."""
        if value is None:
            return True, []
        if not isinstance(value, list):
            return False, []
        normalized: list[str] = []
        for entry in value:
            if not isinstance(entry, str):
                return False, []
            reference = norm_id(entry)
            if not reference or not entry.strip():
                return False, []
            normalized.append(reference)
        return True, normalized

    source = handoff_item or {}
    source_ok, in_reply_to = metadata_id(source.get("in_reply_to"))
    references_ok, references = metadata_references(source.get("references"))
    thread_ok, thread = metadata_id(source.get("thread_key"))
    if not (source_ok and references_ok and thread_ok):
        return None, "promotion_metadata_invalid"
    if evidence is not None:
        evidence_ok, evidence_reply = metadata_id(evidence.get("in_reply_to"))
        evidence_references_ok, evidence_references = metadata_references(
            evidence.get("references")
        )
        evidence_thread_ok, evidence_thread = metadata_id(evidence.get("thread_key"))
        if not (evidence_ok and evidence_references_ok and evidence_thread_ok):
            return None, "promotion_metadata_invalid"
        if in_reply_to and evidence_reply and in_reply_to != evidence_reply:
            return None, "promotion_metadata_conflict"
        if references and evidence_references and references != evidence_references:
            return None, "promotion_metadata_conflict"
        if thread and evidence_thread and thread != evidence_thread:
            return None, "promotion_metadata_conflict"
        in_reply_to = in_reply_to or evidence_reply
        references = references or evidence_references
        thread = thread or evidence_thread
    return {
        "message_id": message_id,
        "norm_message_id": message_id,
        "in_reply_to": in_reply_to,
        "references": references,
        "thread_key": thread,
        "anchor_candidates": thread_anchor_ids(in_reply_to, references, message_id),
        "headers_complete": True,
    }, None


_ANCHOR_REASON_MAP = {
    "anchor_conflict": "promotion_anchor_conflict",
    "anchor_ambiguous": "promotion_anchor_ambiguous",
    "reference_anchor_unresolved": "promotion_anchor_unresolved",
}


def _evid_owner(
    ledger: Mapping[str, Any],
    evid_id: str,
    message_id: str,
) -> str | None:
    """Return the other Message-ID that already owns ``evid_id`` (never a path/id)."""
    for owner, lines in ledger["all_by_message_id"].items():
        if owner == message_id:
            continue
        for line in lines:
            if line.get("evid_id") == evid_id:
                return owner
    return None


def record_pending_outcome(
    data_dir: Path,
    *,
    outcome: str,
    message_id: str | None = None,
    item_ref: str | None = None,
    vorgang_ref: str | None = None,
    evid_id: str | None = None,
    reason_code: str | None = None,
    requested_account: str | None = None,
    workspace_root: str | Path | None = None,
    lease_id: str | None = None,
    conversation_id: str | None = None,
    verifier: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Append one consumption line for one open handoff item (append = consumption).

    Exactly one selector (normalized ``message_id`` or truncated hash
    ``item_ref``) is required.  ``synthesized`` needs an exact Vorgang reference, forbids a reason
    code and may carry an EVID-ID that must not already belong to another
    Message-ID.  ``no_evidence`` needs a schema-valid reason code and forbids every
    anchor.  The append re-checks the fresh thread anchor against **all** ledger
    lines of every ancestor: a different non-empty Vorgang is ``anchor_conflict``,
    divergent duplicate ancestor lines are ``anchor_ambiguous``, a resolved anchor
    without a Vorgang is ``reference_anchor_unresolved`` and a handoff item with
    declared incomplete headers is ``handoff_metadata_incomplete``.

    The target is bound to the verified workspace and the invocation-owned lease is
    verified **before** the first append (``record_outcome`` itself has no such
    guard).  A matching already durable identity is an idempotent no-op; a differing
    identity is ``ledger_conflict``.  The handoff file is never rewritten.
    """
    if outcome not in LEDGER_OUTCOMES:
        raise SentLedgerError(
            "invalid_outcome",
            f"outcome must be one of {LEDGER_OUTCOMES}; nothing was written.",
            {"state": "invalid"},
        )
    if (message_id is None) == (item_ref is None):
        raise SentLedgerError(
            "invalid_selector",
            "Exactly one of message_id/item_ref is required; nothing was written.",
            {"state": "invalid"},
        )
    target = norm_id(message_id) if message_id is not None else None
    if message_id is not None and not target:
        raise SentLedgerError(
            "invalid_message_id",
            "The supplied Message-ID is empty after normalization; nothing was written.",
            {"state": "invalid"},
        )
    if item_ref is not None and (not isinstance(item_ref, str)
                                 or _ITEM_REF_PATTERN.fullmatch(item_ref) is None):
        raise SentLedgerError(
            "invalid_item_ref",
            "item_ref must be the stable 'sha256:' reference from the pending view; "
            "nothing was written.",
            {"state": "invalid"},
        )

    chosen_ref = vorgang_ref if vorgang_ref else None
    chosen_evid = evid_id if evid_id else None
    chosen_reason = reason_code if reason_code else None
    if outcome == "synthesized":
        if not is_reference_token(chosen_ref):
            raise SentLedgerError(
                "missing_vorgang_ref",
                "A synthesized line requires an exact Vorgang reference; nothing was "
                "written.",
                {"state": "invalid"},
            )
        if chosen_reason is not None:
            raise SentLedgerError(
                "reason_code_not_allowed",
                "A synthesized line must not carry a no_evidence reason code; nothing "
                "was written.",
                {"state": "invalid"},
            )
        if chosen_evid is not None and not is_reference_token(chosen_evid):
            raise SentLedgerError(
                "invalid_evid_id",
                "evid_id must be a bounded exact token; nothing was written.",
                {"state": "invalid"},
            )
    else:
        if chosen_ref is not None or chosen_evid is not None:
            raise SentLedgerError(
                "no_evidence_with_anchor",
                "A no_evidence line must not carry a Vorgang reference or an EVID anchor; "
                "nothing was written.",
                {"state": "invalid"},
            )
        if chosen_reason is None:
            raise SentLedgerError(
                "missing_reason_code",
                "A no_evidence line requires a documented reason code; nothing was "
                "written.",
                {"state": "invalid"},
            )
        if not is_reason_code(chosen_reason):
            raise SentLedgerError(
                "reason_code_invalid",
                f"reason_code '{chosen_reason}' is not a schema-valid bounded token; "
                "nothing was written.",
                {"state": "invalid"},
            )

    _workspace, dd = _require_lease(
        data_dir=Path(data_dir), workspace_root=workspace_root, lease_id=lease_id,
        conversation_id=conversation_id, verifier=verifier,
    )
    ledger = load_ledger(dd)
    items = read_handoff(dd)
    ordered = pending_handoff_view(items, ledger)
    _verify_pending_account(dd, ordered, requested_account)

    if target is not None:
        selected = next(
            (entry for entry in ordered if entry["norm_message_id"] == target), None,
        )
    else:
        matches = [
            entry for entry in ordered if _item_ref(entry["norm_message_id"]) == item_ref
        ]
        if len(matches) > 1:  # pragma: no cover - 64-bit collision, fail closed anyway
            raise SentLedgerError(
                "ambiguous_item_ref",
                "The item reference matches more than one open item; nothing was written.",
                {"state": "conflict"},
            )
        selected = matches[0] if matches else None
        if selected is not None:
            target = selected["norm_message_id"]

    requested_identity = (
        outcome,
        chosen_ref if outcome == "synthesized" else None,
        chosen_evid if outcome == "synthesized" else None,
        chosen_reason if outcome == "no_evidence" else None,
    )
    if target is not None:
        existing = list(ledger["all_by_message_id"].get(target, []))
        if existing:
            identities = {_record_identity(record) for record in existing}
            if (
                len(identities) == 1
                and all(_identity_compatible(identity, requested_identity)
                        for identity in identities)
            ):
                return {
                    "ok": True,
                    "mode": "sent_ledger_record",
                    "status": "already_consumed",
                    "outcome": outcome,
                    "item_ref": _ref_token(target),
                    "norm_message_id": target if message_id is not None else None,
                    "vorgang_ref": chosen_ref,
                    "evid_id": chosen_evid,
                    "reason_code": chosen_reason,
                    "appended": 0,
                    "pending_before": len(ordered),
                    "pending_after": len(ordered),
                    "ledger_sha256_before": _file_sha256(dd / LEDGER_FILENAME),
                    "ledger_sha256_after": _file_sha256(dd / LEDGER_FILENAME),
                    "handoff_unchanged": True,
                }
            raise SentLedgerError(
                "ledger_conflict",
                f"Ledger '{LEDGER_FILENAME}' already holds a line for this Message-ID "
                "with a different outcome/Vorgang/EVID/reason; nothing was appended. "
                "Reconcile the durable evidence manually.",
                {"reason": "identity_conflict", "state": "conflict"},
            )
    if selected is None:
        raise SentLedgerError(
            "not_pending",
            "The selected Message-ID is not (or no longer) an open handoff item; "
            "nothing was written.",
            {"state": "not_found"},
        )
    if selected.get("waits_for"):
        raise SentLedgerError(
            "ancestor_pending",
            "The item still waits for an open ancestor in the same handoff; consume the "
            "ancestor first. Nothing was written.",
            {
                "waits_for": [_ref_token(wait) for wait in selected["waits_for"]],
                "state": "blocked",
            },
        )

    if outcome == "synthesized" and chosen_evid is not None:
        owner = _evid_owner(ledger, chosen_evid, target)
        if owner is not None:
            raise SentLedgerError(
                "evid_shared_conflict",
                "The EVID-ID is already anchored to a different Message-ID; the triad is "
                "ambiguous and nothing was written.",
                {"evid_id": chosen_evid, "owner_ref": _ref_token(owner),
                 "state": "conflict"},
            )
    if outcome == "synthesized":
        anchor_problem = _thread_anchor_problem(selected, ledger, chosen_ref)
        if anchor_problem is not None:
            anchor_reason, anchor_details = anchor_problem
            messages = {
                "anchor_conflict": (
                    "The chosen Vorgang reference contradicts a fresh thread anchor; a "
                    "follow-up line must not point to another Vorgang. Nothing was written."
                ),
                "anchor_ambiguous": (
                    "An ancestor Message-ID has contradictory ledger lines; the fresh "
                    "thread anchor is ambiguous and nothing was written."
                ),
                "reference_anchor_unresolved": (
                    "The resolved thread anchor is a synthesized line without a Vorgang "
                    "reference; deriving a follow-up Vorgang from it would invent one. "
                    "Nothing was written."
                ),
                "handoff_metadata_incomplete": (
                    "The handoff item declares incomplete headers, so the thread relation "
                    "cannot be proven; the item stays open and nothing was written."
                ),
            }
            raise SentLedgerError(
                anchor_reason, messages.get(anchor_reason, messages["anchor_conflict"]),
                anchor_details,
            )

    ledger_before = _file_sha256(dd / LEDGER_FILENAME)
    handoff_before = _file_sha256(dd / HANDOFF_FILENAME)
    try:
        added = record_outcome(
            dd,
            [selected],
            outcome=outcome,
            reason_code=chosen_reason if outcome == "no_evidence" else None,
            vorgang_ref=chosen_ref if outcome == "synthesized" else None,
            evid_id=chosen_evid if outcome == "synthesized" else None,
        )
    except SentHandoffError:
        raise
    except Exception as error:  # noqa: BLE001 - append progress may be partial
        raise SentLedgerError(
            "sent_ledger_write_failed",
            "The ledger append failed; the ledger may be partially written. Rerun the "
            "pending view before any new decision. "
            f"({type(error).__name__}: {error})",
            {"reason": "append_failed", "state": "unknown"},
        ) from error
    handoff_after = _file_sha256(dd / HANDOFF_FILENAME)
    return {
        "ok": True,
        "mode": "sent_ledger_record",
        "status": "appended" if added else "already_consumed",
        "outcome": outcome,
        "item_ref": _ref_token(target),
        "norm_message_id": target if message_id is not None else None,
        "vorgang_ref": chosen_ref,
        "evid_id": chosen_evid,
        "reason_code": chosen_reason,
        "appended": added,
        "pending_before": len(ordered),
        "pending_after": len(ordered) - (1 if added else 0),
        "ledger_sha256_before": ledger_before,
        "ledger_sha256_after": _file_sha256(dd / LEDGER_FILENAME),
        "handoff_sha256_before": handoff_before,
        "handoff_sha256_after": handoff_after,
        "handoff_unchanged": handoff_before == handoff_after,
    }


def _evaluation_problem(entry: Any) -> str | None:
    """Strict shape check for one consumer evaluation entry."""
    if not isinstance(entry, dict):
        return "not_an_object"
    unknown = sorted(set(entry) - _EVALUATION_FIELDS)
    if unknown:
        return "unknown_field"
    outcome = entry.get("outcome")
    if not isinstance(outcome, str) or outcome not in _EVALUATION_OUTCOMES:
        return "outcome_invalid"
    has_ref = entry.get("item_ref") is not None
    has_mid = entry.get("message_id") is not None
    if has_ref == has_mid:
        return "selector_missing_or_ambiguous"
    if has_ref:
        if not isinstance(entry["item_ref"], str) or _ITEM_REF_PATTERN.fullmatch(
            entry["item_ref"]
        ) is None:
            return "item_ref_invalid"
    elif not isinstance(entry["message_id"], str) or not norm_id(entry["message_id"]):
        return "message_id_invalid"
    if outcome == "synthesized":
        if not is_reference_token(entry.get("vorgang_ref")):
            return "vorgang_ref_required"
        evid = entry.get("evid_id")
        if evid is not None and not is_reference_token(evid):
            return "evid_id_invalid"
        if entry.get("reason_code") is not None:
            return "reason_code_not_allowed"
    elif outcome == "no_evidence":
        if not is_reason_code(entry.get("reason_code")):
            return "reason_code_required_or_invalid"
        if entry.get("vorgang_ref") is not None or entry.get("evid_id") is not None:
            return "no_evidence_with_anchor"
    elif any(entry.get(name) is not None for name in _EVALUATION_ANCHOR_FIELDS):
        return "not_evaluated_with_data"
    return None


def read_sent_evaluations(path: Path) -> list[dict[str, Any]]:
    """Read a consumer evaluation document; every shape error writes nothing.

    The document is ``{"schema_version": 1, "kind": "mail-desk.sent-evaluation",
    "items": [...]}``.  Each entry names exactly one target (``item_ref`` from the
    pending view or a raw ``message_id``), one ``outcome`` (``synthesized``,
    ``no_evidence`` or ``not_evaluated``) and only the fields that outcome permits.
    Unknown fields, duplicate selectors, missing anchors or a wrong schema stop
    with ``evaluation_invalid``.  The document is consumer-owned transit input; it
    is read only and never rewritten.
    """
    def invalid(reason: str, **details: Any) -> SentLedgerError:
        return SentLedgerError(
            "evaluation_invalid",
            f"The sent evaluation document is not usable ({reason}); nothing was written.",
            {"reason": reason, "state": "invalid", **details},
        )

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise invalid("evaluation_unreadable") from error
    if (
        not isinstance(document, dict)
        or set(document) != {"schema_version", "kind", "items"}
        or type(document.get("schema_version")) is not int
        or document.get("schema_version") != EVALUATION_SCHEMA_VERSION
        or document.get("kind") != EVALUATION_KIND
    ):
        raise invalid("evaluation_header_malformed")
    entries = document.get("items")
    if not isinstance(entries, list):
        raise invalid("evaluation_items_not_a_list")
    if len(entries) > MAX_HANDOFF_ITEMS:
        raise invalid("evaluation_items_over_bound")
    seen_refs: set[str] = set()
    seen_mids: set[str] = set()
    for index, entry in enumerate(entries):
        problem = _evaluation_problem(entry)
        if problem is not None:
            raise invalid(f"item[{index}]: {problem}")
        if entry.get("item_ref") is not None:
            if entry["item_ref"] in seen_refs:
                raise invalid(f"item[{index}]: duplicate_item_ref")
            seen_refs.add(entry["item_ref"])
        else:
            mid = norm_id(entry["message_id"])
            if mid in seen_mids:
                raise invalid(f"item[{index}]: duplicate_message_id")
            seen_mids.add(mid)
    return [dict(entry) for entry in entries]


def _evaluation_identity(entry: Mapping[str, Any]) -> tuple[Any, Any, Any, Any]:
    """Ledger identity requested by one evaluation entry."""
    outcome = entry["outcome"]
    return (
        outcome,
        entry.get("vorgang_ref") if outcome == "synthesized" else None,
        entry.get("evid_id") if outcome == "synthesized" else None,
        entry.get("reason_code") if outcome == "no_evidence" else None,
    )


def _evaluation_ref(entry: Mapping[str, Any]) -> str:
    """Stable hash reference for reports; never the raw Message-ID."""
    if entry.get("item_ref") is not None:
        return str(entry["item_ref"])
    return _item_ref(entry.get("message_id"))


def _resolve_evaluation_target(
    entry: Mapping[str, Any],
    pending_by_ref: Mapping[str, str],
    pending_ids: set[str],
    ledger: Mapping[str, Any],
) -> tuple[str, str | None]:
    """Resolve one entry to ``pending``/``consumed``/``unknown`` plus its ID."""
    if entry.get("item_ref") is not None:
        ref = str(entry["item_ref"])
        mid = pending_by_ref.get(ref)
        if mid is not None:
            return "pending", mid
        owners = [
            owner for owner in ledger["all_by_message_id"] if _item_ref(owner) == ref
        ]
        if len(owners) > 1:
            raise SentLedgerError(
                "ambiguous_item_ref",
                "The item reference matches more than one durable Message-ID; nothing "
                "was written.",
                {"state": "conflict"},
            )
        return ("consumed", owners[0]) if owners else ("unknown", None)
    mid = norm_id(entry.get("message_id"))
    if mid in pending_ids:
        return "pending", mid
    if mid in ledger["all_by_message_id"]:
        return "consumed", mid
    return "unknown", None


def _durable_identity_matches(
    ledger: Mapping[str, Any],
    message_id: str,
    identity: tuple[Any, Any, Any, Any],
) -> bool:
    """Whether the durable ledger line(s) satisfy one requested identity."""
    existing = list(ledger["all_by_message_id"].get(message_id, []))
    if not existing:
        return False
    identities = {_record_identity(record) for record in existing}
    return len(identities) == 1 and all(
        _identity_compatible(one, identity) for one in identities
    )


def consume_evaluated_items(
    data_dir: Path,
    *,
    evaluations: Iterable[Mapping[str, Any]],
    dry_run: bool = False,
    requested_account: str | None = None,
    workspace_root: str | Path | None = None,
    lease_id: str | None = None,
    conversation_id: str | None = None,
    verifier: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Append the evaluated open items in one lease-bound round; the rest stay open.

    Only items with an explicit ``synthesized``/``no_evidence`` evaluation are
    consumed; an item without an entry (or with ``not_evaluated``) is reported and
    left open.  Every append reuses :func:`record_pending_outcome`, so all hardened
    gates apply unchanged (fresh thread anchor, exact Vorgang/EVID, no open
    ancestor, workspace binding and lease before the first write).  The evaluation
    document is validated completely before the first append; duplicate targets,
    conflicting durable identities and a shared EVID-ID stop before any write.  A
    per-item anchor/identity conflict keeps only that item open and is reported as
    ``consume_blocked`` (exit 2); earlier appends remain durable and a rerun is
    idempotent.  ``dry_run`` validates and projects without a lease and without any
    write.  ``pending_before``/``pending_after`` count the open handoff items.
    """
    entries = [dict(entry) for entry in evaluations]
    for index, entry in enumerate(entries):
        problem = _evaluation_problem(entry)
        if problem is not None:
            raise SentLedgerError(
                "evaluation_invalid",
                f"The evaluation entry {index} is not usable ({problem}); nothing was "
                "written.",
                {"reason": problem, "state": "invalid"},
            )
    dd = Path(data_dir)
    if dry_run:
        _workspace, dd = _bind_workspace_data_dir(dd, workspace_root)
    else:
        _workspace, dd = _require_lease(
            data_dir=dd, workspace_root=workspace_root, lease_id=lease_id,
            conversation_id=conversation_id, verifier=verifier,
        )
    assert_append_aliases(dd)
    ledger = load_ledger(dd)
    ordered = load_pending_handoff(dd)["items"]
    pending_before = len(ordered)
    verified_account = _verify_pending_account(dd, ordered, requested_account)
    ledger_before = _file_sha256(dd / LEDGER_FILENAME)
    handoff_before = _file_sha256(dd / HANDOFF_FILENAME)

    pending_ids = {item["norm_message_id"] for item in ordered}
    pending_by_ref: dict[str, str] = {}
    for item in ordered:
        mid = item["norm_message_id"]
        ref = _item_ref(mid)
        previous = pending_by_ref.get(ref)
        if previous is not None and previous != mid:  # pragma: no cover - 64-bit collision
            raise SentLedgerError(
                "ambiguous_item_ref",
                "The item reference matches more than one open item; nothing was written.",
                {"state": "conflict"},
            )
        pending_by_ref[ref] = mid

    entry_by_ref: dict[str, dict[str, Any]] = {}
    entry_by_mid: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if entry.get("item_ref") is not None:
            entry_by_ref[entry["item_ref"]] = entry
        else:
            entry_by_mid[norm_id(entry["message_id"])] = entry

    resolutions = [
        _resolve_evaluation_target(entry, pending_by_ref, pending_ids, ledger)
        for entry in entries
    ]
    covered: dict[str, int] = {}
    for index, (_entry, (state, mid)) in enumerate(zip(entries, resolutions)):
        if state == "unknown":
            continue
        assert mid is not None
        if mid in covered:
            raise SentLedgerError(
                "evaluation_duplicate",
                "Two evaluation entries describe the same Message-ID; nothing was "
                "written.",
                {"state": "invalid"},
            )
        covered[mid] = index

    already_consumed_refs: list[str] = []
    for entry, (state, mid) in zip(entries, resolutions):
        if state != "consumed" or entry["outcome"] == "not_evaluated":
            continue
        assert mid is not None
        if not _durable_identity_matches(ledger, mid, _evaluation_identity(entry)):
            raise SentLedgerError(
                "ledger_conflict",
                "An evaluation entry contradicts the durable ledger identity for its "
                "Message-ID; nothing was written. Reconcile the evidence manually.",
                {"reason": "identity_conflict", "state": "conflict"},
            )
        already_consumed_refs.append(_item_ref(mid))

    evid_owners: dict[str, str] = {}
    for entry, (_state, mid) in zip(entries, resolutions):
        if entry["outcome"] != "synthesized":
            continue
        evid = entry.get("evid_id")
        if evid is None:
            continue
        owner = mid if mid is not None else "unknown:" + _evaluation_ref(entry)
        previous = evid_owners.get(evid)
        if previous is not None and previous != owner:
            raise SentLedgerError(
                "evid_shared_conflict",
                "Two evaluated items share one EVID-ID; a durable EVID anchor belongs "
                "to exactly one Message-ID. Nothing was written.",
                {"state": "conflict"},
            )
        evid_owners[evid] = owner
    for evid, owner in evid_owners.items():
        for other_mid, lines in ledger["all_by_message_id"].items():
            if other_mid == owner:
                continue
            if any(line.get("evid_id") == evid for line in lines):
                raise SentLedgerError(
                    "evid_shared_conflict",
                    "An evaluated EVID-ID is already anchored to a different Message-ID; "
                    "nothing was written.",
                    {"state": "conflict"},
                )

    unmatched_refs = [
        _evaluation_ref(entry)
        for entry, (state, _mid) in zip(entries, resolutions)
        if state == "unknown"
    ]

    def entry_for(item: Mapping[str, Any]) -> dict[str, Any] | None:
        mid = item["norm_message_id"]
        by_ref = entry_by_ref.get(_item_ref(mid))
        if by_ref is not None:
            return by_ref
        return entry_by_mid.get(mid)

    attempted: set[str] = set()
    blocked: dict[str, dict[str, Any]] = {}
    simulated: set[str] = set()
    would_synth = 0
    would_noev = 0
    consumed_synth = 0
    consumed_noev = 0

    while True:
        current = load_pending_handoff(dd)["items"]
        candidate: tuple[dict[str, Any], dict[str, Any]] | None = None
        for item in current:
            mid = item["norm_message_id"]
            if mid in attempted:
                continue
            entry = entry_for(item)
            if entry is None or entry["outcome"] == "not_evaluated":
                continue
            waits = [
                wait for wait in (item.get("waits_for") or []) if wait not in simulated
            ]
            if waits:
                continue
            candidate = (item, entry)
            break
        if candidate is None:
            break
        item, entry = candidate
        mid = item["norm_message_id"]
        attempted.add(mid)
        if dry_run:
            simulated.add(mid)
            if entry["outcome"] == "synthesized":
                would_synth += 1
            else:
                would_noev += 1
            continue
        try:
            result = record_pending_outcome(
                dd,
                outcome=entry["outcome"],
                item_ref=_item_ref(mid),
                vorgang_ref=entry.get("vorgang_ref"),
                evid_id=entry.get("evid_id"),
                reason_code=entry.get("reason_code"),
                requested_account=verified_account,
                workspace_root=_workspace,
                lease_id=lease_id,
                conversation_id=conversation_id,
                verifier=verifier,
            )
        except SentLedgerError as error:
            reason = error.reason_code or "consume_item_failed"
            if reason not in _CONSUME_ITEM_BLOCK_REASONS:
                raise
            blocked[mid] = {
                "item_ref": _item_ref(mid),
                "outcome": entry["outcome"],
                "reason_code": reason,
            }
            continue
        if int(result.get("appended") or 0) == 0:
            # A concurrent writer already made this item durable; it is a no-op,
            # not an append of this round.
            already_consumed_refs.append(_item_ref(mid))
            continue
        if entry["outcome"] == "synthesized":
            consumed_synth += 1
        else:
            consumed_noev += 1

    if dry_run:
        view = ordered
        pending_after = pending_before
    else:
        view = load_pending_handoff(dd)["items"]
        pending_after = len(view)

    not_evaluated: list[str] = []
    deferred: list[dict[str, Any]] = []
    blocked_list: list[dict[str, Any]] = []
    for item in view:
        mid = item["norm_message_id"]
        if dry_run and mid in simulated:
            continue
        entry = entry_for(item)
        if entry is None or entry["outcome"] == "not_evaluated":
            not_evaluated.append(_item_ref(mid))
            continue
        waits = [
            wait for wait in (item.get("waits_for") or [])
            if not (dry_run and wait in simulated)
        ]
        if waits:
            deferred.append({
                "item_ref": _item_ref(mid),
                "reason_code": "ancestor_pending",
                "waits_for": [_ref_token(wait) for wait in waits],
            })
            continue
        if mid in blocked:
            blocked_list.append(blocked[mid])
            continue
        if dry_run:
            deferred.append({"item_ref": _item_ref(mid), "reason_code": "not_consumed"})
            continue
        blocked_list.append({"item_ref": _item_ref(mid), "reason_code": "not_consumed"})

    if blocked_list:
        raise SentLedgerError(
            "consume_blocked",
            "consume stopped after a per-item conflict; earlier appends are durable and "
            "the blocked items stay open. Rerun 'pending' and reconcile before another "
            "consume.",
            {
                "status": "blocked",
                "pending_before": pending_before,
                "pending_after": pending_after,
                "consumed": {
                    "synthesized": consumed_synth, "no_evidence": consumed_noev,
                },
                "blocked": blocked_list,
                "not_evaluated": not_evaluated,
                "deferred": deferred,
                "state": "conflict",
            },
        )

    if dry_run:
        would_consume = would_synth + would_noev
        projected_pending_after = pending_before - would_consume
        if pending_before == 0:
            status = "empty"
        elif would_consume:
            status = "planned"
        elif deferred:
            status = "partial"
        else:
            status = "not_evaluated"
        appended = 0
    else:
        projected_pending_after = pending_after
        if pending_before == 0:
            status = "empty"
        elif pending_after == 0:
            status = "consumed"
        elif consumed_synth + consumed_noev or deferred:
            status = "partial"
        else:
            status = "not_evaluated"
        appended = consumed_synth + consumed_noev

    if pending_after == 0:
        next_hint = (
            "No open ledger item remains; the handoff file is unchanged."
        )
    elif deferred:
        next_hint = (
            "The deferred items wait for an open ancestor; consume the ancestor first."
        )
    else:
        next_hint = (
            "The remaining items were not evaluated; they stay open by design "
            "(fail-closed), and no outcome was invented."
        )
    ledger_after = _file_sha256(dd / LEDGER_FILENAME)
    handoff_after = _file_sha256(dd / HANDOFF_FILENAME)
    return {
        "ok": True,
        "mode": "sent_ledger_consume",
        "kind": CONSUME_KIND,
        "view_version": CONSUME_VERSION,
        "status": status,
        "dry_run": dry_run,
        "pending_before": pending_before,
        "pending_after": pending_after,
        "projected_pending_after": projected_pending_after,
        "consumed": {
            "synthesized": consumed_synth, "no_evidence": consumed_noev,
        },
        "appended": appended,
        "would_consume": (would_synth + would_noev) if dry_run else 0,
        "would_consume_outcomes": (
            {"synthesized": would_synth, "no_evidence": would_noev}
            if dry_run else {"synthesized": 0, "no_evidence": 0}
        ),
        "already_consumed": already_consumed_refs,
        "already_consumed_count": len(already_consumed_refs),
        "not_evaluated": not_evaluated,
        "not_evaluated_count": len(not_evaluated),
        "deferred": deferred,
        "deferred_count": len(deferred),
        "blocked": [],
        "blocked_count": 0,
        "evaluation_entries": len(entries),
        "unmatched_evaluations": len(unmatched_refs),
        "unmatched_refs": unmatched_refs,
        "verified_account": verified_account,
        "ledger_sha256_before": ledger_before,
        "ledger_sha256_after": ledger_after,
        "handoff_sha256_before": handoff_before,
        "handoff_sha256_after": handoff_after,
        "handoff_unchanged": handoff_before == handoff_after,
        "next": next_hint,
    }


def _classify_promotion_evidence(
    evidence: list[Any],
    message_id: str,
    vorgang_ref: str,
    evid_id: str,
) -> tuple[str, str | None, Mapping[str, Any] | None, int | None]:
    """Read-only triad classification of a caller-supplied promotion evidence list."""
    for record in evidence:
        if not isinstance(record, Mapping):
            return "conflict", "evidence_not_an_object", None, None
    # A foreign owner of the requested EVID-ID is a conflict even when no record
    # matches the target Message-ID: otherwise a shared evidence anchor would be
    # misreported as "absent" (and could authorize a second promotion).
    for record in evidence:
        if record.get("evid_id") != evid_id:
            continue
        other = norm_id(record.get("message_id"))
        if not other:
            return "conflict", "competing_evidence_missing_message_id", None, 1
        if other != message_id:
            return "conflict", "evid_shared_conflict", None, 1
    same = [record for record in evidence
            if norm_id(record.get("message_id")) == message_id]
    if not same:
        return "absent", "promotion_not_found", None, 0
    if len(same) != 1:
        return "conflict", "promotion_ambiguous", None, len(same)
    observed = same[0]
    if observed.get("vorgang_ref") != vorgang_ref or observed.get("evid_id") != evid_id:
        return "conflict", "promotion_conflict", None, 1
    return "unique", None, observed, 1


def check_crash_triad(
    data_dir: Path,
    *,
    message_id: str,
    vorgang_ref: str,
    evid_id: str,
    promotion_evidence: Iterable[Mapping[str, Any]] | None = None,
    workspace_root: str | Path | None = None,
) -> dict[str, Any]:
    """Read-only crash classification of one exact ``message_id``/Vorgang/EVID triad.

    ``status`` is ``unique`` (exactly one matching durable state, no contradiction),
    ``conflict`` (partial/ambiguous evidence, a different or duplicated ledger line,
    a foreign EVID owner or a foreign thread anchor) or ``absent`` (no durable
    evidence for the Message-ID).  Matching is exact: normalized Message-ID,
    byte/case-exact tokens, never substrings.  No lease is taken and nothing is
    written; ``safe_to_append`` is true only when the caller supplied unique durable
    evidence and the ledger line is still missing.  It is **never** true while the
    handoff item still waits for an open ancestor (``ancestor_pending``) and is never
    derived from malformed thread metadata (``promotion_metadata_invalid``).  A
    ``conflict`` raises :class:`SentLedgerError` (fail-closed failed envelope) instead
    of returning a success envelope; ``unique``/``absent`` return normally.
    """
    problem = _triad_problem(message_id, vorgang_ref, evid_id)
    if problem is not None:
        raise SentLedgerError(
            "promotion_recovery_incomplete",
            f"The crash check needs the complete triad (message_id + vorgang_ref + "
            f"EVID-ID); {problem} is missing or invalid. Nothing was written.",
            {"reason": problem, "state": "incomplete"},
        )
    target = norm_id(message_id)
    _workspace, dd = _bind_workspace_data_dir(Path(data_dir), workspace_root)
    assert_append_aliases(dd)
    ledger = load_ledger(dd)

    evidence_provided = promotion_evidence is not None
    evidence_list = list(promotion_evidence or [])
    evidence_state = "not_provided"
    evidence_reason: str | None = None
    observed: Mapping[str, Any] | None = None
    evidence_matches: int | None = None
    if evidence_provided:
        evidence_state, evidence_reason, observed, evidence_matches = (
            _classify_promotion_evidence(evidence_list, target, vorgang_ref, evid_id)
        )

    lines = list(ledger["all_by_message_id"].get(target, []))
    ledger_state = "absent"
    ledger_reason: str | None = None
    if len(lines) > 1:
        ledger_state, ledger_reason = "conflict", "ledger_ambiguous"
    elif len(lines) == 1:
        if _ledger_identity(lines[0]) == ("synthesized", vorgang_ref, evid_id, None):
            ledger_state = "unique"
        else:
            ledger_state, ledger_reason = "conflict", "ledger_conflict"

    owner = _evid_owner(ledger, evid_id, target)
    handoff_items = read_handoff(dd)
    handoff_item = next(
        (item for item in handoff_items if item["norm_message_id"] == target), None,
    )
    anchor_reason: str | None = None
    anchor_details: dict[str, Any] = {}
    anchor_item, metadata_problem = _thread_anchor_item(handoff_item, observed, target)
    if metadata_problem is not None:
        anchor_reason = metadata_problem
    elif handoff_item is not None or observed is not None:
        anchor_problem = _thread_anchor_problem(anchor_item or {}, ledger, vorgang_ref)
        if anchor_problem is not None:
            raw_reason, anchor_details = anchor_problem
            anchor_reason = _ANCHOR_REASON_MAP.get(raw_reason, raw_reason)
    waits = _pending_waits_for(
        handoff_items, ledger, target, (anchor_item or {}).get("anchor_candidates") or [],
    )

    status = "unique"
    reason: str | None = None
    if ledger_state == "conflict":
        status, reason = "conflict", ledger_reason
    elif owner is not None:
        status, reason = "conflict", "evid_shared_conflict"
    elif evidence_provided and evidence_state == "conflict":
        status, reason = "conflict", evidence_reason
    elif anchor_reason is not None:
        status, reason = "conflict", anchor_reason
    elif waits:
        # A still-pending ancestor keeps the fresh thread anchor unstable, so no
        # append is safe; the read-only check must never recommend one.
        status, reason = "conflict", "ancestor_pending"
        anchor_details = {
            "waits_for": [_ref_token(wait) for wait in waits],
            "state": "blocked",
        }
    elif evidence_provided and evidence_state == "absent":
        if ledger_state == "unique":
            status, reason = "conflict", "evidence_incomplete"
        else:
            status = "absent"
    elif not evidence_provided and ledger_state == "absent":
        status = "absent"

    append_required = (
        status == "unique" and ledger_state == "absent" and evidence_state == "unique"
    )
    item_ref = _ref_token(target)
    if status == "conflict":
        # Fail-closed stop: the CLI renders a failed envelope (never the documented
        # special case of a blocked plan, which keeps success: true).
        raise SentLedgerError(
            reason or "ledger_conflict",
            f"The crash check classified the triad as a conflict ({reason or 'ledger_conflict'}); "
            "nothing was written. Reconcile the durable promotion evidence and the ledger "
            "manually; do not promote again.",
            {
                "status": status,
                "ledger_state": ledger_state,
                "evidence_state": evidence_state,
                "ledger_lines": len(lines),
                "evidence_matches": evidence_matches,
                "append_required": False,
                "safe_to_append": False,
                "item_ref": item_ref,
                **anchor_details,
                "state": "conflict",
            },
        )
    return {
        "ok": True,
        "mode": "sent_ledger_crash_check",
        "kind": CRASH_CHECK_KIND,
        "view_version": CRASH_CHECK_VERSION,
        "read_only": True,
        "status": status,
        "reason_code": reason,
        "ledger_state": ledger_state,
        "evidence_state": evidence_state,
        "ledger_lines": len(lines),
        "evidence_matches": evidence_matches,
        "append_required": append_required,
        "safe_to_append": append_required,
        "item_ref": item_ref,
        "ledger_sha256": _file_sha256(dd / LEDGER_FILENAME),
    }


__all__ = [
    "CONSUME_KIND",
    "CONSUME_VERSION",
    "CRASH_CHECK_KIND",
    "CRASH_CHECK_VERSION",
    "EVALUATION_KIND",
    "EVALUATION_SCHEMA_VERSION",
    "INVENTORY_KIND",
    "INVENTORY_SCHEMA_VERSION",
    "NO_EVIDENCE_REASON_CODES",
    "PENDING_VIEW_KIND",
    "PENDING_VIEW_VERSION",
    "PLAN_KIND",
    "PLAN_VERSION",
    "SentLedgerError",
    "apply_legacy_import",
    "build_legacy_import_plan",
    "build_pending_view",
    "check_crash_triad",
    "consume_evaluated_items",
    "read_legacy_inventory",
    "read_sent_evaluations",
    "record_pending_outcome",
    "recover_promotion",
]
