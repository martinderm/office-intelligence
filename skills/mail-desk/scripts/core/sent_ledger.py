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

No mailbox, network, LLM or consumer-document access happens here.  Mutating
operations require an invocation-owned ``workspace-lock`` lease.
"""

from __future__ import annotations

import hashlib
import json
import stat
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .common import normalize_message_id
from .quarantine.attachment_fetch import verify_workspace_lock
from .sent_handoff import (
    HANDOFF_FILENAME,
    LEDGER_FILENAME,
    LEDGER_SCHEMA_VERSION,
    SentHandoffError,
    SentLedgerConflict,
    append_ledger_records,
    assert_append_aliases,
    is_reason_code,
    is_reference_token,
    ledger_outcome,
    load_ledger,
    record_outcome,
    read_handoff,
    resolve_thread_anchor,
)
from .sent_handoff import norm_id, unconsumed

INVENTORY_KIND = "mail-desk.sent-ledger.legacy-inventory"
INVENTORY_SCHEMA_VERSION = 1
PLAN_KIND = "mail-desk.sent-ledger.legacy-import-plan"
PLAN_VERSION = 1

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

    # The unique-EVID check ran before the already_consumed/ledger-conflict branch.
    item: dict[str, Any] = {
        "message_id": mid,
        "vorgang_ref": vorgang_ref,
        "evid_id": evid_id,
    }
    # Optional thread metadata from the same durable promotion record is used
    # only for the ledger line's thread relation, never for the triad match.
    for name in ("in_reply_to", "thread_key"):
        value = observed.get(name)
        if isinstance(value, str) and value.strip():
            item[name] = value
    references = observed.get("references")
    if isinstance(references, list) and all(isinstance(ref, str) for ref in references):
        item["references"] = list(references)
    anchor = resolve_thread_anchor(item, ledger)
    anchor_ref = (anchor or {}).get("vorgang_ref")
    if anchor_ref is not None and anchor_ref != vorgang_ref:
        raise SentLedgerError(
            "promotion_anchor_conflict",
            "The durable promotion triad names a different Vorgang than the resolved "
            f"thread anchor ('{anchor_ref}' vs '{vorgang_ref}'); nothing was written. "
            "Recovery: reconcile the ancestor and promotion evidence manually; a "
            "follow-up line must not anchor to another Vorgang.",
            {
                "message_id": mid,
                "reason": "anchor_vorgang_mismatch",
                "state": "conflict",
            },
        )
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


__all__ = [
    "INVENTORY_KIND",
    "INVENTORY_SCHEMA_VERSION",
    "NO_EVIDENCE_REASON_CODES",
    "PLAN_KIND",
    "PLAN_VERSION",
    "SentLedgerError",
    "apply_legacy_import",
    "build_legacy_import_plan",
    "read_legacy_inventory",
    "recover_promotion",
]
