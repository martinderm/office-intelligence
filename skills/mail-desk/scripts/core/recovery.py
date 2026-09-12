"""Durable per-message recovery journal for interrupted mail-desk batches."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .common import atomic_write_json, normalize_message_id, utc_now_iso


JOURNAL_FILENAME = "batch-recovery-journal.json"
TERMINAL_PHASES = {"complete", "partial", "aborted"}


def has_completed_step(record: dict[str, Any], step: str) -> bool:
    """Return whether a durable milestone was reached, despite a later abort.

    `phase` is intentionally the latest observable state and may therefore be
    `partial` or `aborted` after an earlier side effect completed. Resume logic
    must never use that latest state as a rollback of a completed milestone.
    """
    if record.get("phase") == step:
        return True
    return any(
        isinstance(event, dict) and event.get("phase") == step
        for event in record.get("phases", [])
    )


def stable_batch_id(items: list[dict[str, Any]], explicit_id: object = None) -> str:
    """Return a deterministic recovery key without including mail content."""
    if isinstance(explicit_id, str) and explicit_id.strip():
        return explicit_id.strip()
    identity = []
    for item in items:
        action = item.get("action") if isinstance(item.get("action"), dict) else {}
        identity.append(
            {
                "message_id": normalize_message_id(str(item.get("message_id") or item.get("raw_message_id") or "")),
                "envelope_id": str(item.get("envelope_id", "")),
                "source_folder": str(item.get("source_folder", "INBOX")),
                "action": str(action.get("type", "copy_as_move")),
                "target_folder": str(action.get("target_folder", "")),
            }
        )
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "execute-" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


class BatchRecoveryJournal:
    """Atomically persist a small resumable state machine per Message-ID."""

    def __init__(self, data_dir: Path, run_id: str, *, mode: str = "execute", resume: bool = True) -> None:
        self.data_dir = data_dir
        self.path = data_dir / JOURNAL_FILENAME
        self.run_id = run_id
        self.mode = mode
        self._data = self._load()
        runs = self._data.setdefault("runs", {})
        run = runs.get(run_id)
        if not isinstance(run, dict):
            run = {
                "run_id": run_id,
                "mode": mode,
                "status": "running",
                "created_at": utc_now_iso(),
                "updated_at": utc_now_iso(),
                "items": {},
            }
            runs[run_id] = run
        run.setdefault("items", {})
        run.setdefault("mode", mode)
        if resume and run.get("status") in TERMINAL_PHASES:
            run["status"] = "running"
        self._save()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": 1, "runs": {}}
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # Never overwrite a malformed incident artifact; preserve it for review.
            raise RuntimeError("Recovery journal is unreadable; retain it and reconcile manually.")
        if not isinstance(loaded, dict) or not isinstance(loaded.get("runs"), dict):
            raise RuntimeError("Recovery journal has an invalid shape; retain it and reconcile manually.")
        loaded.setdefault("schema_version", 1)
        return loaded

    @property
    def run(self) -> dict[str, Any]:
        return self._data["runs"][self.run_id]

    def _save(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.run["updated_at"] = utc_now_iso()
        atomic_write_json(self.path, self._data)

    def ensure_item(self, item: dict[str, Any]) -> dict[str, Any]:
        raw_mid = item.get("message_id") or item.get("raw_message_id") or ""
        message_id = normalize_message_id(str(raw_mid))
        if not message_id:
            message_id = "fallback:" + str(item.get("envelope_id", ""))
        items = self.run["items"]
        record = items.setdefault(
            message_id,
            {
                "message_id": message_id,
                "envelope_id": str(item.get("envelope_id", "")),
                "source_folder": str(item.get("source_folder", "INBOX")),
                "subject": str(item.get("subject", "")),
                "from": str(item.get("from", "")),
                "date": str(item.get("date", "")),
                "action": item.get("action") if isinstance(item.get("action"), dict) else {},
                "decision": item.get("decision") if isinstance(item.get("decision"), dict) else {},
                "notes": str(item.get("notes", "")),
                "evidence": item.get("evidence") if isinstance(item.get("evidence"), dict) else None,
                "synthesis_targets": item.get("synthesis_targets") if isinstance(item.get("synthesis_targets"), list) else [],
                "phase": "selected",
                "phases": [{"phase": "selected", "at": utc_now_iso()}],
            },
        )
        self._save()
        return record

    def transition(self, record: dict[str, Any], phase: str, *, error: str | None = None, **details: Any) -> None:
        record["phase"] = phase
        record.update({key: value for key, value in details.items() if value is not None})
        event: dict[str, Any] = {"phase": phase, "at": utc_now_iso()}
        if error:
            event["error"] = error
            record["last_error"] = error
        if details:
            event["details"] = {key: value for key, value in details.items() if value is not None}
        record.setdefault("phases", []).append(event)
        self._save()

    def set_run_status(self, status: str, *, error: str | None = None) -> None:
        self.run["status"] = status
        if error:
            self.run["last_error"] = error
        self._save()

    @classmethod
    def load_run(cls, data_dir: Path, run_id: str | None = None) -> tuple[Path, dict[str, Any] | None]:
        path = data_dir / JOURNAL_FILENAME
        if not path.exists():
            return path, None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return path, None
        runs = data.get("runs") if isinstance(data, dict) else None
        if not isinstance(runs, dict) or not runs:
            return path, None
        if run_id:
            return path, runs.get(run_id) if isinstance(runs.get(run_id), dict) else None
        latest = max(
            (run for run in runs.values() if isinstance(run, dict)),
            key=lambda run: str(run.get("updated_at", "")),
            default=None,
        )
        return path, latest


def cleanup_recovery_temp_files(data_dir: Path) -> list[str]:
    """Remove only abandoned journal-owned siblings, never user manifests."""
    if not data_dir.exists():
        return []
    removed: list[str] = []
    for candidate in data_dir.glob(".batch-recovery-journal.json.*.tmp"):
        try:
            candidate.unlink()
            removed.append(str(candidate))
        except OSError:
            continue
    return removed
