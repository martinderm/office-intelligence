"""Human-approved project dossier execution through the canonical batch handlers.

This mode deliberately owns only the dossier-specific approval binding and
catalog boundary checks.  Routing, evidence, final-index persistence,
telemetry, handoff generation, and consistency verification remain the
responsibility of the established ``execute`` and ``verify`` handlers.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable, Mapping

from ..classifier import load_catalogs
from ..common import normalize_message_id, resolve_data_dir
from ..completion import completion_report, release_synthesis_handoff
from ..synthesis_handoff import empty_synthesis_handoff
from .dossier import _require_project
from .execute import run_execute_mode
from .verify import run_verify_mode


_ALLOWED_KEYS = {
    "mode", "project", "execute_request", "review", "delete_input_on_success", "account",
}
_RECEIPT_KEYS = {"reviewed_at", "reviewed_by", "execute_request_sha256"}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def canonical_execute_request_sha256(execute_request: object) -> str:
    """Return the stable SHA-256 binding for a reviewed execute request."""
    try:
        canonical = json.dumps(
            execute_request,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("execute_request must be canonical JSON data") from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _dependency(
    dependencies: Mapping[str, Callable[..., Any]] | None,
    name: str,
    default: Callable[..., Any],
) -> Callable[..., Any]:
    if dependencies and name in dependencies:
        return dependencies[name]
    return default


def _require_review_receipt(review: object, execute_request: dict[str, Any]) -> dict[str, str]:
    if not isinstance(review, dict):
        raise ValueError("dossier_apply requires a review object")
    if review.get("required") is not True or review.get("state") != "approved":
        raise ValueError("dossier_apply requires an externally approved human review")
    receipt = review.get("approval_receipt")
    if not isinstance(receipt, dict) or set(receipt) != _RECEIPT_KEYS:
        raise ValueError("review approval_receipt has an invalid shape")
    normalized = {key: receipt[key] for key in _RECEIPT_KEYS}
    if not all(isinstance(value, str) and value.strip() for value in normalized.values()):
        raise ValueError("review approval_receipt fields must be non-empty strings")
    approved_hash = normalized["execute_request_sha256"]
    if not _SHA256.fullmatch(approved_hash):
        raise ValueError("review approval receipt must contain a lowercase SHA-256 hash")
    if approved_hash != canonical_execute_request_sha256(execute_request):
        raise ValueError("review approval receipt does not bind this exact execute_request")
    return normalized


def _require_dossier_execute_request(
    execute_request: object,
    *,
    project_id: str,
    mailbox_folder: str,
    normalize_id: Callable[[object], str],
) -> list[dict[str, Any]]:
    """Fully validate every item before delegating any mailbox mutation."""
    if not isinstance(execute_request, dict) or execute_request.get("mode") != "execute":
        raise ValueError("execute_request mode must be exactly execute")
    items = execute_request.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("execute_request must contain at least one item")

    for position, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"execute_request item {position} must be an object")
        envelope_id = item.get("envelope_id")
        if isinstance(envelope_id, bool) or not isinstance(envelope_id, (str, int)) or not str(envelope_id).strip():
            raise ValueError(f"execute_request item {position} has an invalid envelope_id")
        if item.get("source_folder") != "INBOX":
            raise ValueError(f"execute_request item {position} must originate from exactly INBOX")
        if not normalize_id(item.get("message_id") or item.get("raw_message_id") or ""):
            raise ValueError(f"execute_request item {position} has no valid message_id")

        decision = item.get("decision")
        if not isinstance(decision, dict) or decision.get("kind") != "project" or decision.get("id") != project_id:
            raise ValueError(f"execute_request item {position} must be assigned to exactly project {project_id}")
        action = item.get("action")
        if (
            not isinstance(action, dict)
            or action.get("type") != "copy_as_move"
            or action.get("target_folder") != mailbox_folder
        ):
            raise ValueError(f"execute_request item {position} must route to the catalog mailbox_folder")
    return items


def _require_reviewed_account(execute_request: dict[str, Any], outer_account: str | None) -> str | None:
    """Use only the account covered by the reviewed execute-request hash."""
    reviewed_account = execute_request.get("account")
    if reviewed_account is not None:
        if not isinstance(reviewed_account, str) or not reviewed_account.strip():
            raise ValueError("execute_request account must be a non-empty string when present")
    if outer_account is not None:
        if outer_account != reviewed_account:
            raise ValueError("outer account must exactly match the reviewed execute_request account")
    return reviewed_account


def _failed_result(
    *,
    project_id: str,
    receipt: dict[str, str],
    execute_result: dict[str, Any] | None,
    stage: str,
    error: Exception | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": False,
        "mode": "dossier_apply",
        "project": project_id,
        "approval_receipt": receipt,
        "review": {"required": True, "state": f"{stage}_review_required", "approval_receipt": receipt},
        "execute_summary": execute_result,
        "verify_summary": None,
    }
    if execute_result:
        result["telemetry"] = execute_result.get("telemetry")
        result["synthesis_candidate"] = execute_result.get("synthesis_candidate")
        result["synthesis_handoff"] = empty_synthesis_handoff()
    if error is not None:
        result["error"] = {"stage": stage, "exception_type": type(error).__name__}
    return result


def _successful_execute_message_ids(
    execute_result: dict[str, Any],
    items: list[dict[str, Any]],
    normalize_id: Callable[[object], str],
) -> list[str]:
    """Pair the complete successful execute result back to the preflighted input."""
    results = execute_result.get("results")
    if not isinstance(results, list) or len(results) != len(items):
        raise ValueError("successful execute result does not cover every reviewed dossier item")
    expected = [normalize_id(item.get("message_id") or item.get("raw_message_id") or "") for item in items]
    actual: list[str] = []
    for position, result in enumerate(results, start=1):
        if not isinstance(result, dict) or result.get("success") is not True:
            raise ValueError(f"successful execute result item {position} is invalid")
        message_id = normalize_id(result.get("message_id") or result.get("raw_message_id") or "")
        if not message_id:
            raise ValueError(f"successful execute result item {position} has no valid message_id")
        actual.append(message_id)
    if actual != expected:
        raise ValueError("successful execute result does not match the reviewed dossier item order")
    return actual


def run_dossier_apply_mode(
    config: dict[str, Any],
    account: str | None = None,
    data_dir: Path | None = None,
    index_path: Path | None = None,
    *,
    dependencies: Mapping[str, Callable[..., Any]] | None = None,
) -> dict[str, Any]:
    """Apply a hash-bound human-reviewed project execute request, then verify it."""
    unknown = set(config) - _ALLOWED_KEYS
    if unknown:
        raise ValueError(f"dossier_apply configuration contains unsupported fields: {', '.join(sorted(unknown))}")
    if config.get("delete_input_on_success", False) is not False:
        raise ValueError("dossier_apply manifest must be retained as its approval receipt")

    resolve_data = _dependency(dependencies, "resolve_data_dir", resolve_data_dir)
    catalog_loader = _dependency(dependencies, "load_catalogs", load_catalogs)
    normalize_id = _dependency(dependencies, "normalize_message_id", normalize_message_id)
    execute = _dependency(dependencies, "run_execute_mode", run_execute_mode)
    verify = _dependency(dependencies, "run_verify_mode", run_verify_mode)

    dd = data_dir or resolve_data()
    projects, _ = catalog_loader(dd.parent.parent)
    if not isinstance(projects, list):
        raise ValueError("projects catalog is unavailable or invalid")
    project = _require_project(projects, config.get("project"))
    project_id = project["id"]
    mailbox_folder = project.get("mailbox_folder")
    if not isinstance(mailbox_folder, str) or not mailbox_folder.strip():
        raise ValueError("project must have a catalog mailbox_folder")

    execute_request = config.get("execute_request")
    if not isinstance(execute_request, dict):
        raise ValueError("execute_request must be an object")
    receipt = _require_review_receipt(config.get("review"), execute_request)
    reviewed_account = _require_reviewed_account(execute_request, account)
    items = _require_dossier_execute_request(
        execute_request,
        project_id=project_id,
        mailbox_folder=mailbox_folder,
        normalize_id=normalize_id,
    )

    try:
        execute_result = execute(
            execute_request,
            account=reviewed_account,
            data_dir=dd,
            index_path=index_path,
        )
    except Exception as exc:  # noqa: BLE001 - preserve the reviewed request for recovery
        return _failed_result(
            project_id=project_id, receipt=receipt, execute_result=None, stage="execute", error=exc,
        )
    if not isinstance(execute_result, dict) or execute_result.get("ok") is not True:
        return _failed_result(
            project_id=project_id,
            receipt=receipt,
            execute_result=execute_result if isinstance(execute_result, dict) else None,
            stage="execute",
        )

    try:
        message_ids = _successful_execute_message_ids(execute_result, items, normalize_id)
    except ValueError as exc:
        return _failed_result(
            project_id=project_id, receipt=receipt, execute_result=execute_result, stage="execute", error=exc,
        )
    try:
        verify_result = verify(
            {"mode": "verify", "message_ids": message_ids, "check_folders": False},
            account=reviewed_account,
            data_dir=dd,
            index_path=index_path,
        )
    except Exception as exc:  # noqa: BLE001 - execute state remains reviewable on verification failure
        return _failed_result(
            project_id=project_id, receipt=receipt, execute_result=execute_result, stage="verify", error=exc,
        )

    ok = isinstance(verify_result, dict) and verify_result.get("ok") is True
    if not ok:
        result = _failed_result(
            project_id=project_id, receipt=receipt, execute_result=execute_result, stage="verify",
        )
        result["verify_summary"] = verify_result if isinstance(verify_result, dict) else None
        return result
    return {
        "ok": True,
        "mode": "dossier_apply",
        "project": project_id,
        "approval_receipt": receipt,
        "review": {"required": True, "state": "completed", "approval_receipt": receipt},
        "execute_summary": execute_result,
        "verify_summary": verify_result,
        "telemetry": execute_result.get("telemetry"),
        "synthesis_handoff": release_synthesis_handoff(
            execute_result.get("synthesis_candidate", execute_result.get("synthesis_handoff")),
            verify_result,
        ),
        "completion_report": completion_report(
            source="dossier_apply",
            status="completed",
            verified_message_ids=message_ids,
            handoff=release_synthesis_handoff(
                execute_result.get("synthesis_candidate", execute_result.get("synthesis_handoff")),
                verify_result,
            ),
        ),
    }
