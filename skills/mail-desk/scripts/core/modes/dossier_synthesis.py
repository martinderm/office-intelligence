"""Fail-closed, non-executing synthesis work-order for project dossiers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Mapping

from ..classifier import load_catalogs
from ..common import atomic_write_json, normalize_message_id, resolve_data_dir
from ..synthesis_handoff import canonicalize_synthesis_handoff
from .dossier import _require_project


_ALLOWED_KEYS = {
    "mode", "project", "dossier_apply_result", "dossier_apply_result_sha256",
    "delete_input_on_success", "account",
}
_RECEIPT_KEYS = {"reviewed_at", "reviewed_by", "execute_request_sha256"}


def canonical_json_sha256(value: object) -> str:
    """Return a stable SHA-256 for a JSON-compatible snapshot."""
    try:
        canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("dossier synthesis input must be canonical JSON data") from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _dependency(dependencies: Mapping[str, Callable[..., Any]] | None, name: str, default: Callable[..., Any]) -> Callable[..., Any]:
    if dependencies and name in dependencies:
        return dependencies[name]
    return default


def _normal_id(value: object, normalize_id: Callable[[object], str]) -> str:
    return normalize_id(value) if isinstance(value, str) else ""


def _require_receipt(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != _RECEIPT_KEYS:
        raise ValueError("dossier_apply approval_receipt has an invalid shape")
    receipt = {key: value[key] for key in _RECEIPT_KEYS}
    if not all(isinstance(item, str) and item.strip() for item in receipt.values()):
        raise ValueError("dossier_apply approval_receipt fields must be non-empty strings")
    return receipt


def _require_completed_apply(
    value: object, *, project_id: str, normalize_id: Callable[[object], str]
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Validate the source snapshot and return its source-bound handoff items."""
    if not isinstance(value, Mapping):
        raise ValueError("dossier_synthesis requires a dossier_apply_result object")
    if value.get("ok") is not True or value.get("mode") != "dossier_apply":
        raise ValueError("dossier_apply_result must be a completed successful dossier_apply result")
    if value.get("project") != project_id:
        raise ValueError("dossier_apply_result project must exactly match the requested project")
    receipt = _require_receipt(value.get("approval_receipt"))
    review = value.get("review")
    if (
        not isinstance(review, Mapping)
        or review.get("required") is not True
        or review.get("state") != "completed"
        or review.get("approval_receipt") != receipt
    ):
        raise ValueError("dossier_apply_result must retain its completed approval review")

    execute, verify = value.get("execute_summary"), value.get("verify_summary")
    if not isinstance(execute, Mapping) or execute.get("ok") is not True or execute.get("mode") != "execute":
        raise ValueError("dossier_apply_result must contain a successful execute_summary")
    if not isinstance(verify, Mapping) or verify.get("ok") is not True or verify.get("mode") != "verify":
        raise ValueError("dossier_apply_result must contain a successful verify_summary")

    raw_handoff = value.get("synthesis_handoff")
    handoff = canonicalize_synthesis_handoff(raw_handoff)
    if handoff != raw_handoff or handoff["status"] != "pending":
        raise ValueError("dossier_apply_result must contain an exact canonical pending synthesis_handoff")
    execute_results, verify_results = execute.get("results"), verify.get("results")
    if not isinstance(execute_results, list) or not execute_results:
        raise ValueError("execute_summary must contain successful result items")
    if not isinstance(verify_results, list) or len(verify_results) != len(execute_results):
        raise ValueError("verify_summary must cover every execute result")
    if len(handoff["items"]) != len(execute_results):
        raise ValueError("synthesis_handoff must preserve every successful dossier source")

    sources: list[dict[str, Any]] = []
    for position, (handoff_item, execute_result, verify_result) in enumerate(zip(handoff["items"], execute_results, verify_results), start=1):
        if not isinstance(execute_result, Mapping) or execute_result.get("success") is not True:
            raise ValueError(f"execute_summary result {position} is not successful")
        if not isinstance(verify_result, Mapping) or verify_result.get("consistent") is not True:
            raise ValueError(f"verify_summary result {position} is not consistent")
        message_id = _normal_id(handoff_item["message_id"], normalize_id)
        if not message_id:
            raise ValueError(f"synthesis_handoff item {position} has no valid message_id")
        if message_id != _normal_id(execute_result.get("message_id"), normalize_id):
            raise ValueError(f"synthesis_handoff item {position} does not match execute evidence")
        if message_id != _normal_id(verify_result.get("message_id"), normalize_id):
            raise ValueError(f"synthesis_handoff item {position} does not match verify evidence")
        if handoff_item["kind"] != "project" or handoff_item["id"] != project_id:
            raise ValueError(f"synthesis_handoff item {position} is outside the exact project boundary")
        if handoff_item["synthesis_targets"] != execute_result.get("synthesis_targets"):
            raise ValueError(f"synthesis_handoff item {position} targets do not match execute evidence")
        sources.append({
            "message_id": message_id,
            "evidence_anchor": {"kind": "mail_message_id", "value": message_id},
            "subject": handoff_item["subject"],
            "synthesis_targets": handoff_item["synthesis_targets"],
            "target_selection_required": handoff_item["target_selection_required"],
        })
    return receipt, sources


def run_dossier_synthesis_mode(
    config: dict[str, Any], account: str | None = None, data_dir: Path | None = None,
    *, dependencies: Mapping[str, Callable[..., Any]] | None = None,
) -> dict[str, Any]:
    """Write a project-bound, reviewable synthesis work-order without executing it."""
    del account
    unknown = set(config) - _ALLOWED_KEYS
    if unknown:
        raise ValueError(f"dossier_synthesis configuration contains unsupported fields: {', '.join(sorted(unknown))}")
    if config.get("delete_input_on_success", False) is not False:
        raise ValueError("dossier_synthesis input must be retained for review")
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
    apply_result = config.get("dossier_apply_result")
    apply_hash = canonical_json_sha256(apply_result)
    if config.get("dossier_apply_result_sha256") != apply_hash:
        raise ValueError("dossier_apply_result_sha256 must exactly bind the embedded dossier_apply_result")
    receipt, sources = _require_completed_apply(apply_result, project_id=project_id, normalize_id=normalize_id)
    source_snapshot = {
        "schema_version": 1, "project": project_id,
        "dossier_apply_result_sha256": apply_hash, "sources": sources,
    }
    source_snapshot_sha256 = canonical_json_sha256(source_snapshot)
    selection_required = any(source["target_selection_required"] for source in sources)
    work_order = {
        "schema_version": 1, "mode": "dossier_synthesis", "project": project_id,
        "source_snapshot": source_snapshot, "source_snapshot_sha256": source_snapshot_sha256,
        "review": {
            "required": True,
            "state": "target_selection_required" if selection_required else "pending_synthesis_review",
            "approval_receipt": receipt,
            "prohibited_automatic_steps": ["llm_invoke", "knowledge_write", "cloud_sync", "task_sync"],
        },
    }
    work_order["work_order_sha256"] = canonical_json_sha256(work_order)
    output_path = dd / "batch-dossier-synthesis.json"
    write_json(output_path, work_order)
    return {
        "ok": True, "mode": "dossier_synthesis", "project": project_id,
        "dossier_synthesis_file": str(output_path), "work_order": work_order,
        "message": "Prepared a source-bound dossier synthesis work-order. Review it before selecting targets or performing LLM synthesis.",
    }
