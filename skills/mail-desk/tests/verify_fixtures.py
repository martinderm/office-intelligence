"""Hermetic fixture builders for standalone verify scope, provenance and evidence tests.

The builders assemble canonical in-memory Execute summaries, synthesis candidates,
runner envelopes and temp-root evidence layouts.  Nothing here reads a mailbox,
writes outside a caller-provided temporary root, or touches the network; every
value is deterministic so the behavior tests can pin an exact scope and evidence
outcome.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.envelope import build_success
from core.synthesis_handoff import collect_synthesis_handoff

CANONICAL_ACTION = "batch_runner"
INDEX_FILENAME = "final-location-index.json"
ACTION_LOG_FILENAME = "action-log.jsonl"
DEFAULT_FOLDER = "Projekte/Pilot"


def scope_ids(count: int = 10, prefix: str = "scope") -> list[str]:
    """Return a deterministic, duplicate-free normalized message-id scope."""
    return [f"{prefix}-{position:02d}@example.test" for position in range(count)]


def review_item(
    message_id: str,
    *,
    kind: str = "project",
    identifier: str = "pilot",
) -> dict[str, Any]:
    """Return one classify-style item with an empty synthesis-target list."""
    return {
        "message_id": message_id,
        "subject": f"Subject {message_id}",
        "decision": {"kind": kind, "id": identifier},
        "synthesis_targets": [],
    }


def mixed_batch_items(
    message_ids: list[str],
    *,
    archive_kind: str = "archive",
) -> list[dict[str, Any]]:
    """Return a batch whose last item is an archive/newsletter member."""
    items: list[dict[str, Any]] = []
    for position, message_id in enumerate(message_ids):
        if position == len(message_ids) - 1:
            items.append(review_item(message_id, kind=archive_kind, identifier="newsletter"))
        else:
            items.append(review_item(message_id))
    return items


def successful_results(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return one successful execute row per item, preserving target metadata."""
    rows: list[dict[str, Any]] = []
    for entry in items:
        rows.append(
            {
                "message_id": entry["message_id"],
                "subject": entry.get("subject", ""),
                "success": True,
                "synthesis_targets": list(entry.get("synthesis_targets", [])),
            }
        )
    return rows


def synthesis_candidate(
    items: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return the canonical pending/not_required candidate for paired items."""
    return collect_synthesis_handoff(items, results)


def execute_summary(
    items: list[dict[str, Any]],
    results: list[dict[str, Any]],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    """Return a completed Execute summary carrying the source-bound candidate."""
    return {
        "mode": "execute",
        "ok": True,
        "status": "completed",
        "recovery_required": False,
        "results": results,
        "synthesis_candidate": candidate,
    }


def runner_execute_envelope(summary: dict[str, Any]) -> dict[str, Any]:
    """Return a canonical runner envelope whose data keeps mode and ok."""
    data = dict(summary)
    data["operation"] = "execute"
    return build_success(CANONICAL_ACTION, "Batch runner execute completed.", data)


def write_json(path: Path, payload: Any) -> Path:
    """Write deterministic UTF-8 JSON, creating parent directories."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return target


def write_index(data_dir: Path, message_ids: list[str], folder: str = DEFAULT_FOLDER) -> Path:
    """Write a final-location index covering every message id."""
    items = {
        message_id: {
            "message_id": message_id,
            "final_folder": folder,
            "envelope_id": "42",
        }
        for message_id in message_ids
    }
    return write_json(Path(data_dir) / INDEX_FILENAME, {"items": items})


def write_action_log(data_dir: Path, message_ids: list[str], folder: str = DEFAULT_FOLDER) -> Path:
    """Write one action-log line per message id."""
    lines = [
        json.dumps(
            {
                "message_id": message_id,
                "subject": f"Subject {message_id}",
                "action": {"target_folder": folder},
            }
        )
        for message_id in message_ids
    ]
    target = Path(data_dir) / ACTION_LOG_FILENAME
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def canonical_evidence_path(project_id: str = "pilot", filename: str = "2026-09.md") -> str:
    """Return the canonical ``memory/evidence/**`` relative path for a project."""
    return f"memory/evidence/projects/{project_id}/{filename}"


def write_evidence_document(root: Path, relative_path: str, message_ids: list[str]) -> Path:
    """Write one evidence markdown document under a caller-provided temp root."""
    target = Path(root) / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "\n".join(f"- Message-ID: `{message_id}`" for message_id in message_ids) + "\n",
        encoding="utf-8",
    )
    return target


def empty_canonical_evidence_root(root: Path) -> Path:
    """Create the canonical evidence root without any documents."""
    target = Path(root) / "memory" / "evidence"
    target.mkdir(parents=True, exist_ok=True)
    return target


def items_with_evidence(
    items: list[dict[str, Any]],
    relative_path: str,
) -> list[dict[str, Any]]:
    """Return item copies that bind the explicit evidence-file branch."""
    return [{**entry, "evidence_file": relative_path} for entry in items]
