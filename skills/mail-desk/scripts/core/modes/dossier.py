"""Mailbox-read-only, catalog-led project dossier preparation mode.

The non-executing mode intentionally does not access a mailbox. It resolves one
active project from the local catalog and writes a reviewable, bounded ``inspect``
follow-up request. That output is a local workspace mutation and therefore requires
the normal workspace lock. A human must review and run the request separately.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Mapping

from ..classifier import load_catalogs
from ..common import atomic_write_json, resolve_data_dir


MAX_DOSSIER_COUNT = 50
MAX_QUERY_CLAUSES = 24
_SAFE_QUERY_VALUE = re.compile(r'^[^\x00-\x1f"\\]{2,120}$')
_SAFE_DOMAIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_SAFE_STORAGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_ALLOWED_KEYS = {
    "mode", "project", "source_folder", "max_count", "auto_query_from_catalog",
    "delete_input_on_success", "account",
}


def _safe_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text if _SAFE_QUERY_VALUE.fullmatch(text) else None


def _safe_domain(value: object) -> str | None:
    """Normalize exactly one optional leading @ before strict domain validation."""
    text = _safe_text(value)
    if text is None:
        return None
    normalized = text[1:] if text.startswith("@") else text
    return normalized.lower() if _SAFE_DOMAIN.fullmatch(normalized) else None


def _append_signal(signals: list[dict[str, str]], *, field: str, value: object, source: str) -> None:
    text = _safe_text(value)
    if text is None:
        return
    candidate = {"field": field, "value": text, "source": source}
    if candidate not in signals:
        signals.append(candidate)


def _catalog_signals(project: dict[str, Any]) -> tuple[list[dict[str, str]], bool]:
    """Extract a capped, deterministic signal list from safe project fields only."""
    signals: list[dict[str, str]] = []
    _append_signal(signals, field="subject", value=project.get("id"), source="project_id")
    _append_signal(signals, field="subject", value=project.get("kuerzel"), source="kuerzel")
    for alias in project.get("aliases", []) if isinstance(project.get("aliases"), list) else []:
        _append_signal(signals, field="subject", value=alias, source="alias")
    for domain in project.get("domains", []) if isinstance(project.get("domains"), list) else []:
        text = _safe_domain(domain)
        if text:
            _append_signal(signals, field="from", value=text, source="domain")
    for contact in project.get("contacts", []) if isinstance(project.get("contacts"), list) else []:
        if isinstance(contact, dict):
            text = _safe_text(contact.get("email"))
            if text and "@" in text and " " not in text:
                _append_signal(signals, field="from", value=text.lower(), source="contact")
    return signals[:MAX_QUERY_CLAUSES], len(signals) > MAX_QUERY_CLAUSES


def _build_query(signals: list[dict[str, str]]) -> str:
    if not signals:
        raise ValueError("project has no safe catalog signals for a dossier search")
    return " or ".join(f'{signal["field"]} "{signal["value"]}"' for signal in signals)


def _require_project(projects: list[dict[str, Any]], requested_id: object) -> dict[str, Any]:
    if not isinstance(requested_id, str) or not requested_id:
        raise ValueError("project must be a non-empty exact project ID")
    matches = [project for project in projects if isinstance(project, dict) and project.get("id") == requested_id]
    if len(matches) != 1:
        raise ValueError("project must identify exactly one catalog project")
    status = matches[0].get("status")
    if "status" in matches[0] and (not isinstance(status, str) or status.casefold() != "active"):
        raise ValueError("project is not active")
    return matches[0]


def _require_max_count(value: object) -> int:
    if value is None:
        return MAX_DOSSIER_COUNT
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_DOSSIER_COUNT:
        raise ValueError(f"max_count must be an integer between 1 and {MAX_DOSSIER_COUNT}")
    return value


def build_cloud_atlas_preflight(project: Mapping[str, Any]) -> dict[str, Any]:
    """Return a catalog-only Cloud-Atlas preflight handoff for one project.

    This deliberately names no paths and invokes no Cloud-Atlas command.  The
    receiving skill remains responsible for interpreting its ``cloud_sync``
    entries, acquiring the consuming-workspace lock and obtaining any required
    human approval before a sync.
    """
    project_id = project.get("id")
    if not isinstance(project_id, str) or not project_id:
        raise ValueError("project must have a non-empty ID for cloud preflight")
    cloud_sync = project.get("cloud_sync")
    base = {
        "schema_version": 1,
        "receiver": "cloud-atlas",
        "operation": "project_preflight",
        "project_id": project_id,
        "required_receiving_steps": [
            "verify_catalog_cloud_sync",
            "acquire_consuming_workspace_lock",
            "obtain_required_human_approval",
        ],
        "prohibited_automatic_steps": ["cloud_sync", "path_override", "storage_invention"],
    }
    if cloud_sync is None:
        return {
            **base,
            "state": "not_configured",
            "storage_ids": [],
            "reason": "project.cloud_sync is not configured",
        }
    if not isinstance(cloud_sync, Mapping) or not cloud_sync:
        return {
            **base,
            "state": "review_required",
            "storage_ids": [],
            "reason": "project.cloud_sync is not a non-empty storage mapping",
        }
    storage_ids = list(cloud_sync)
    if (
        not all(isinstance(storage_id, str) and _SAFE_STORAGE_ID.fullmatch(storage_id) for storage_id in storage_ids)
        or not all(isinstance(storage, Mapping) for storage in cloud_sync.values())
    ):
        return {
            **base,
            "state": "review_required",
            "storage_ids": [],
            "reason": "project.cloud_sync contains an invalid storage declaration",
        }
    return {
        **base,
        "state": "pending_review",
        "storage_ids": storage_ids,
    }


def run_dossier_mode(
    config: dict[str, Any], account: str | None = None, data_dir: Path | None = None,
    *, dependencies: Mapping[str, Callable[..., Any]] | None = None,
) -> dict[str, Any]:
    """Create a non-executing, catalog-derived inspect handoff for one project."""
    del account  # The mode never accesses a mailbox.
    unknown = set(config) - _ALLOWED_KEYS
    if unknown:
        raise ValueError(f"dossier configuration contains unsupported fields: {', '.join(sorted(unknown))}")
    if config.get("source_folder", "INBOX") != "INBOX":
        raise ValueError("dossier source_folder must be exactly INBOX")
    if config.get("auto_query_from_catalog", True) is not True:
        raise ValueError("dossier requires auto_query_from_catalog: true")
    if config.get("delete_input_on_success", False) is not False:
        raise ValueError("dossier input must be retained for review")

    dd = data_dir or resolve_data_dir()
    catalog_loader = dependencies.get("load_catalogs", load_catalogs) if dependencies else load_catalogs
    write_json = dependencies.get("atomic_write_json", atomic_write_json) if dependencies else atomic_write_json
    workspace_root = dd.parent.parent
    projects, _ = catalog_loader(workspace_root)
    if not isinstance(projects, list):
        raise ValueError("projects catalog is unavailable or invalid")
    project = _require_project(projects, config.get("project"))
    max_count = _require_max_count(config.get("max_count"))
    signals, signals_truncated = _catalog_signals(project)

    inspect_request = {
        "mode": "inspect", "folder": "INBOX", "count": max_count, "order": "oldest",
        "skip_known": True, "query": _build_query(signals), "delete_input_on_success": False,
    }
    manifest = {
        "schema_version": 1, "mode": "dossier", "project": project["id"],
        "project_title": str(project.get("title", project["id"])), "source_folder": "INBOX",
        "max_count": max_count, "auto_query_from_catalog": True, "catalog_signals": signals,
        "signals_truncated": signals_truncated, "next_request": inspect_request,
        "cloud_atlas_preflight": build_cloud_atlas_preflight(project),
        "review": {
            "required": True, "state": "pending_inspect", "allowed_next_modes": ["inspect", "draft"],
            "prohibited_automatic_steps": ["execute", "pipeline", "sync_sent", "synthesis", "cloud_sync", "task_sync"],
        },
    }
    output_path = dd / "batch-dossier.json"
    write_json(output_path, manifest)
    return {
        "ok": True, "mode": "dossier", "project": project["id"], "max_count": max_count,
        "dossier_file": str(output_path), "dossier": manifest,
        "message": f"Prepared mailbox-read-only, non-executing dossier manifest for {project['id']}. Review and run its inspect request separately.",
    }
