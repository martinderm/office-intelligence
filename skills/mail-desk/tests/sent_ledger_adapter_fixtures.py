"""Synthetic, offline fixtures for the Issue #88 sent-ledger consumer adapter.

Everything here is hermetic: only ``example.org`` identifiers, synthetic accounts
``SYNTH-A``/``SYNTH-B`` and temporary directories.  No mailbox, network, real
address or real Message-ID is touched.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = MAIL_DESK_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from core.sent_handoff import (  # noqa: E402
    HANDOFF_FILENAME,
    LEDGER_FILENAME,
    thread_anchor_ids,
    thread_key,
)

ACCOUNT_A = "SYNTH-A"
ACCOUNT_B = "SYNTH-B"
RECORDED_AT = "2026-11-01T09:00:00Z"
HANDOFF_AT = "2026-11-01T09:30:00Z"


def _wrapped(mid: str) -> str:
    return f"<{mid}>"


def handoff_item(
    mid: str,
    *,
    mailbox: str = ACCOUNT_A,
    in_reply_to: str = "",
    references: list[str] | None = None,
    subject: str = "Synthetic subject",
    headers_complete: bool = True,
    truncated_fields: list[str] | None = None,
) -> dict[str, Any]:
    """One schema-valid handoff item (thread key and anchors derived consistently)."""
    refs = [_wrapped(reference) for reference in (references or [])]
    reply_to = _wrapped(in_reply_to) if in_reply_to else ""
    item: dict[str, Any] = {
        "message_id": _wrapped(mid),
        "norm_message_id": mid,
        "date": "Mon, 02 Nov 2026 08:00:00 +0100",
        "subject": subject,
        "from": "Synthetic Sender <sender@example.org>",
        "to": ["Synthetic Recipient <recipient@example.org>"],
        "cc": [],
        "in_reply_to": reply_to,
        "references": refs,
        "envelope_id": "9001",
        "folder": "Sent Items",
        "mailbox": mailbox,
        "thread_key": thread_key(reply_to, refs, _wrapped(mid)),
        "anchor_candidates": thread_anchor_ids(reply_to, refs, _wrapped(mid)),
        "anchored_to": None,
        "headers_complete": headers_complete,
    }
    if truncated_fields:
        item["truncated_fields"] = list(truncated_fields)
    return item


def ledger_line(
    mid: str,
    *,
    outcome: str = "synthesized",
    source: str = "synthesis",
    vorgang_ref: str | None = "V-1",
    evid_id: str | None = "E-1",
    reason_code: str | None = None,
    role: str = "primary",
    anchored_to: str | None = None,
    thread: str = "",
    recorded_at: str = RECORDED_AT,
) -> dict[str, Any]:
    """One schema-valid ledger line; ``no_evidence`` never carries an anchor."""
    line: dict[str, Any] = {
        "schema_version": 1,
        "message_id": mid,
        "role": role,
        "recorded_at": recorded_at,
        "outcome": outcome,
        "source": source,
    }
    if thread:
        line["thread_key"] = thread
    if outcome == "synthesized":
        line["vorgang_ref"] = vorgang_ref
        if evid_id is not None:
            line["evid_id"] = evid_id
    else:
        line["reason_code"] = reason_code or "no_relevant_evidence"
    if anchored_to:
        line["anchored_to"] = anchored_to
    return line


def write_handoff(
    data_dir: Path,
    items: list[dict[str, Any]],
    *,
    generated_at: str = HANDOFF_AT,
    max_items: int = 200,
) -> None:
    document = {
        "schema_version": 1,
        "kind": "mail-desk.new-sent-items",
        "generated_at": generated_at,
        "max_items": max_items,
        "count": len(items),
        "items": items,
    }
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / HANDOFF_FILENAME).write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )


def write_ledger(data_dir: Path, lines: list[dict[str, Any]]) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / LEDGER_FILENAME).write_text(
        "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines),
        encoding="utf-8",
    )


def read_ledger_lines(data_dir: Path) -> list[dict[str, Any]]:
    path = data_dir / LEDGER_FILENAME
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_backend_config(
    workspace: Path,
    *,
    account: str | None = ACCOUNT_A,
) -> Path:
    path = workspace / ".agents" / "mail-desk-backend.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema_version": 1, "backend": "himalaya", "account": account}) + "\n",
        encoding="utf-8",
    )
    return path


def make_workspace(
    root: Path,
    *,
    account: str | None = ACCOUNT_A,
    handoff_items: list[dict[str, Any]] | None = None,
    ledger_lines: list[dict[str, Any]] | None = None,
    handoff_missing: bool = False,
) -> tuple[Path, Path]:
    """Create ``<root>/data/mail-desk`` with optional synthetic state."""
    workspace = Path(root)
    data_dir = workspace / "data" / "mail-desk"
    data_dir.mkdir(parents=True, exist_ok=True)
    if account is not None:
        write_backend_config(workspace, account=account)
    if not handoff_missing and handoff_items is not None:
        write_handoff(data_dir, handoff_items)
    if ledger_lines:
        write_ledger(data_dir, ledger_lines)
    return workspace, data_dir


def write_evidence(path: Path, records: list[dict[str, Any]]) -> Path:
    path.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def write_evaluations(
    path: Path,
    entries: list[dict[str, Any]],
    *,
    schema_version: int = 1,
    kind: str = "mail-desk.sent-evaluation",
) -> Path:
    """One synthetic consumer evaluation document for ``consume``."""
    path.write_text(
        json.dumps(
            {"schema_version": schema_version, "kind": kind, "items": entries},
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    return path


def snapshot(root: Path) -> dict[str, str]:
    """Byte hashes of every file under ``root`` (read-only proof)."""
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(Path(root).rglob("*"))
        if path.is_file()
    }
