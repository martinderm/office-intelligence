"""Fail-closed, non-executing Cloud-Atlas and Task-Desk dossier handoffs."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Callable, Mapping

from ..classifier import load_catalogs
from ..common import atomic_write_json, normalize_message_id, resolve_data_dir
from ..synthesis_targets import validate_execute_synthesis_targets
from .dossier import _require_project, build_cloud_atlas_preflight
from .dossier_synthesis import canonical_json_sha256


_ALLOWED_KEYS = {
    "mode", "project", "dossier_synthesis_work_order",
    "dossier_synthesis_work_order_sha256", "synthesis_review",
    "synthesis_review_sha256", "delete_input_on_success", "account",
}
_SOURCE_KEYS = {
    "message_id", "evidence_anchor", "subject", "synthesis_targets",
    "target_selection_required",
}
_WORK_ORDER_KEYS = {
    "schema_version", "mode", "project", "source_snapshot", "source_snapshot_sha256",
    "review", "work_order_sha256",
}
_SOURCE_SNAPSHOT_KEYS = {
    "schema_version", "project", "dossier_apply_result_sha256", "sources",
}
_WORK_ORDER_REVIEW_KEYS = {
    "required", "state", "approval_receipt", "prohibited_automatic_steps",
}
_APPROVAL_RECEIPT_KEYS = {"reviewed_at", "reviewed_by", "execute_request_sha256"}
_CANDIDATE_KEYS = {"message_id", "evidence_anchor", "candidate"}
_REVIEW_KEYS = {
    "state", "dossier_synthesis_work_order_sha256", "source_snapshot_sha256",
    "reviewed_at", "reviewed_by", "action_candidates",
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FR04C_REVIEW_STATES = {"target_selection_required", "pending_synthesis_review"}
_FR04C_PROHIBITED_STEPS = ["llm_invoke", "knowledge_write", "cloud_sync", "task_sync"]


def _dependency(dependencies: Mapping[str, Callable[..., Any]] | None, name: str, default: Callable[..., Any]) -> Callable[..., Any]:
    if dependencies and name in dependencies:
        return dependencies[name]
    return default


def _normal_message_id(value: object, normalize_id: Callable[[object], str]) -> str:
    return normalize_id(value) if isinstance(value, str) else ""


def _require_work_order(
    value: object, expected_hash: object, *, project_id: str, normalize_id: Callable[[object], str]
) -> tuple[str, str, set[str]]:
    if not isinstance(value, Mapping):
        raise ValueError("dossier_handoff requires a dossier_synthesis_work_order object")
    work_order = dict(value)
    if set(work_order) != _WORK_ORDER_KEYS:
        raise ValueError("dossier_synthesis_work_order has an invalid outer shape")
    work_order_hash = work_order.pop("work_order_sha256")
    if not isinstance(work_order_hash, str) or not _SHA256.fullmatch(work_order_hash) or work_order_hash != canonical_json_sha256(work_order):
        raise ValueError("dossier_synthesis_work_order must retain its exact work_order_sha256")
    if expected_hash != work_order_hash:
        raise ValueError("dossier_synthesis_work_order_sha256 must exactly bind the embedded work order")
    if work_order.get("schema_version") != 1 or work_order.get("mode") != "dossier_synthesis":
        raise ValueError("dossier_synthesis_work_order has an unsupported schema or mode")
    if work_order.get("project") != project_id:
        raise ValueError("dossier_synthesis_work_order is outside the exact project boundary")
    review = work_order["review"]
    if not isinstance(review, Mapping) or set(review) != _WORK_ORDER_REVIEW_KEYS:
        raise ValueError("dossier_synthesis_work_order has an invalid FR-04c review shape")
    receipt = review["approval_receipt"]
    if (
        review["required"] is not True
        or review["state"] not in _FR04C_REVIEW_STATES
        or review["prohibited_automatic_steps"] != _FR04C_PROHIBITED_STEPS
        or not isinstance(receipt, Mapping)
        or set(receipt) != _APPROVAL_RECEIPT_KEYS
        or not all(isinstance(receipt[key], str) and receipt[key].strip() for key in _APPROVAL_RECEIPT_KEYS)
        or not _SHA256.fullmatch(receipt["execute_request_sha256"])
    ):
        raise ValueError("dossier_synthesis_work_order must retain the exact FR-04c review gate")
    snapshot = work_order.get("source_snapshot")
    if (
        not isinstance(snapshot, Mapping)
        or set(snapshot) != _SOURCE_SNAPSHOT_KEYS
        or snapshot.get("schema_version") != 1
        or snapshot.get("project") != project_id
        or not isinstance(snapshot.get("dossier_apply_result_sha256"), str)
        or not _SHA256.fullmatch(snapshot["dossier_apply_result_sha256"])
    ):
        raise ValueError("dossier_synthesis_work_order has an invalid project source snapshot")
    snapshot_hash = work_order.get("source_snapshot_sha256")
    if not isinstance(snapshot_hash, str) or snapshot_hash != canonical_json_sha256(snapshot):
        raise ValueError("dossier_synthesis_work_order must retain its exact source_snapshot_sha256")
    sources = snapshot.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("dossier_synthesis_work_order must contain source-bound mail evidence")
    source_ids: set[str] = set()
    for position, source in enumerate(sources, start=1):
        if not isinstance(source, Mapping) or set(source) != _SOURCE_KEYS:
            raise ValueError(f"source snapshot item {position} has an invalid shape")
        message_id = _normal_message_id(source.get("message_id"), normalize_id)
        anchor = source.get("evidence_anchor")
        if not message_id or anchor != {"kind": "mail_message_id", "value": message_id}:
            raise ValueError(f"source snapshot item {position} has no exact mail EVID anchor")
        if not isinstance(source.get("subject"), str) or not isinstance(source.get("synthesis_targets"), list):
            raise ValueError(f"source snapshot item {position} has invalid source data")
        try:
            validated_targets = validate_execute_synthesis_targets([{
                "decision": {"kind": "project", "id": project_id},
                "synthesis_targets": source["synthesis_targets"],
            }])[0]
        except ValueError as exc:
            raise ValueError(f"source snapshot item {position} has invalid project synthesis targets: {exc}") from exc
        if source["synthesis_targets"] != validated_targets:
            raise ValueError(f"source snapshot item {position} synthesis targets are not canonical FR-06 targets")
        if source.get("target_selection_required") is not (not bool(validated_targets)):
            raise ValueError(f"source snapshot item {position} has inconsistent target selection state")
        source_ids.add(message_id)
    return work_order_hash, snapshot_hash, source_ids


def _require_synthesis_review(
    value: object, expected_hash: object, *, work_order_hash: str, source_snapshot_hash: str,
    source_ids: set[str], normalize_id: Callable[[object], str],
) -> tuple[str, list[dict[str, Any]]]:
    if not isinstance(value, Mapping) or set(value) != _REVIEW_KEYS:
        raise ValueError("synthesis_review has an invalid shape")
    review = dict(value)
    review_hash = canonical_json_sha256(review)
    if expected_hash != review_hash:
        raise ValueError("synthesis_review_sha256 must exactly bind the embedded synthesis_review")
    if review.get("state") != "completed":
        raise ValueError("synthesis_review must be completed before task handoff")
    if not all(isinstance(review.get(key), str) and review[key].strip() for key in ("reviewed_at", "reviewed_by")):
        raise ValueError("synthesis_review must retain non-empty reviewer provenance")
    if review.get("dossier_synthesis_work_order_sha256") != work_order_hash:
        raise ValueError("synthesis_review does not bind the exact dossier synthesis work order")
    if review.get("source_snapshot_sha256") != source_snapshot_hash:
        raise ValueError("synthesis_review does not bind the exact source snapshot")
    candidates = review.get("action_candidates")
    if not isinstance(candidates, list):
        raise ValueError("synthesis_review action_candidates must be a list")
    checked: list[dict[str, Any]] = []
    for position, candidate in enumerate(candidates, start=1):
        if not isinstance(candidate, Mapping) or set(candidate) != _CANDIDATE_KEYS:
            raise ValueError(f"action candidate {position} has an invalid shape")
        message_id = _normal_message_id(candidate.get("message_id"), normalize_id)
        if message_id not in source_ids:
            raise ValueError(f"action candidate {position} is not bound to a dossier mail source")
        if candidate.get("evidence_anchor") != {"kind": "mail_message_id", "value": message_id}:
            raise ValueError(f"action candidate {position} has no exact mail EVID anchor")
        text = candidate.get("candidate")
        if not isinstance(text, str) or not text.strip() or text != text.strip() or len(text) > 1000:
            raise ValueError(f"action candidate {position} must be a bounded non-empty data string")
        checked.append({
            "message_id": message_id,
            "evidence_anchor": {"kind": "mail_message_id", "value": message_id},
            "candidate": text,
        })
    return review_hash, checked


def run_dossier_handoff_mode(
    config: dict[str, Any], account: str | None = None, data_dir: Path | None = None,
    *, dependencies: Mapping[str, Callable[..., Any]] | None = None,
) -> dict[str, Any]:
    """Prepare review-only handoffs; never invoke Cloud-Atlas or Task-Desk."""
    del account
    unknown = set(config) - _ALLOWED_KEYS
    if unknown:
        raise ValueError(f"dossier_handoff configuration contains unsupported fields: {', '.join(sorted(unknown))}")
    if config.get("delete_input_on_success", False) is not False:
        raise ValueError("dossier_handoff input must be retained for review")
    resolve_data = _dependency(dependencies, "resolve_data_dir", resolve_data_dir)
    catalog_loader = _dependency(dependencies, "load_catalogs", load_catalogs)
    write_json = _dependency(dependencies, "atomic_write_json", atomic_write_json)
    normalize_id = _dependency(dependencies, "normalize_message_id", normalize_message_id)
    dd = data_dir or resolve_data()
    projects, _ = catalog_loader(dd.parent.parent)
    if not isinstance(projects, list):
        raise ValueError("projects catalog is unavailable or invalid")
    project = _require_project(projects, config.get("project"))
    project_id = project["id"]
    work_order_hash, snapshot_hash, source_ids = _require_work_order(
        config.get("dossier_synthesis_work_order"), config.get("dossier_synthesis_work_order_sha256"),
        project_id=project_id, normalize_id=normalize_id,
    )
    synthesis_review_hash, candidates = _require_synthesis_review(
        config.get("synthesis_review"), config.get("synthesis_review_sha256"),
        work_order_hash=work_order_hash, source_snapshot_hash=snapshot_hash,
        source_ids=source_ids, normalize_id=normalize_id,
    )
    task_handoff = {
        "schema_version": 1,
        "receiver": "task-desk",
        "operation": "review_and_dedupe",
        "project_id": project_id,
        "source_snapshot_sha256": snapshot_hash,
        "synthesis_review_sha256": synthesis_review_hash,
        "state": "review_and_dedupe_required" if candidates else "not_required",
        "action_candidates": candidates,
        "required_receiving_steps": [
            "review_action_candidates",
            "apply_workspace_routing_rules",
            "deduplicate_against_created_tasks",
            "materialize_factored_attribution_before_dispatch",
        ],
        "prohibited_automatic_steps": ["task_create", "todoist_sync", "priority_or_due_date_invention"],
    }
    handoff = {
        "schema_version": 1,
        "mode": "dossier_handoff",
        "project": project_id,
        "upstream": {
            "dossier_synthesis_work_order_sha256": work_order_hash,
            "source_snapshot_sha256": snapshot_hash,
            "synthesis_review_sha256": synthesis_review_hash,
        },
        "cloud_atlas_preflight": build_cloud_atlas_preflight(project),
        "task_desk_handoff": task_handoff,
        "review": {
            "required": True,
            "state": "handoffs_prepared",
            "prohibited_automatic_steps": ["cloud_sync", "task_create", "todoist_sync"],
        },
    }
    handoff["handoff_sha256"] = canonical_json_sha256(handoff)
    output_path = dd / "batch-dossier-handoff.json"
    write_json(output_path, handoff)
    return {
        "ok": True,
        "mode": "dossier_handoff",
        "project": project_id,
        "dossier_handoff_file": str(output_path),
        "handoff": handoff,
        "message": "Prepared project-bound Cloud-Atlas and Task-Desk review handoffs; no external operation was invoked.",
    }
