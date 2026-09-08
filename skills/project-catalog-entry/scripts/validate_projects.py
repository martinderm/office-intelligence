#!/usr/bin/env python3
"""Read-only validation for project catalog schema v3."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


DEFAULT_CATALOG = "memory/references/projects/projects.json"
WP_STATUSES = {"active", "completed", "planned", "paused"}
ROOT_STRING_FIELDS = {
    "kuerzel", "project_website", "project_reference", "laufzeit", "gesamtbudget",
    "institution_budget", "boku_budget", "reference_md", "description", "updated_at",
}


class JsonArgumentParser(argparse.ArgumentParser):
    """Keep argument failures inside the standard CLI envelope."""

    def error(self, message: str) -> None:
        raise ValueError(message)


def envelope(success: bool, state: str, message: str, data: dict[str, Any] | None = None,
             error: dict[str, str] | None = None) -> dict[str, Any]:
    return {
        "action": "validate_projects",
        "success": success,
        "state": state,
        "message": message,
        "data": data or {},
        "error": error,
    }


def is_nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def is_slug(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value) is not None


def expect_object(value: Any, path: str, errors: list[str]) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        errors.append(f"{path}: expected object")
        return None
    return value


def expect_array(value: Any, path: str, errors: list[str]) -> list[Any] | None:
    if not isinstance(value, list):
        errors.append(f"{path}: expected array")
        return None
    return value


def validate_known_fields(item: dict[str, Any], allowed: set[str], path: str, errors: list[str]) -> None:
    for key in item:
        if key not in allowed:
            errors.append(f"{path}.{key}: unknown field in normative object")


def validate_required_strings(item: dict[str, Any], fields: tuple[str, ...], path: str,
                              errors: list[str]) -> None:
    for field in fields:
        if not is_nonempty_string(item.get(field)):
            errors.append(f"{path}.{field}: required non-empty string")


def validate_string_array(item: dict[str, Any], field: str, path: str, errors: list[str]) -> None:
    if field not in item:
        return
    values = expect_array(item[field], f"{path}.{field}", errors)
    if values is not None:
        for index, value in enumerate(values):
            if not isinstance(value, str):
                errors.append(f"{path}.{field}[{index}]: expected string")


def validate_contacts(item: dict[str, Any], field: str, path: str, errors: list[str]) -> None:
    if field not in item:
        return
    contacts = expect_array(item[field], f"{path}.{field}", errors)
    if contacts is not None:
        for index, contact in enumerate(contacts):
            expect_object(contact, f"{path}.{field}[{index}]", errors)


def duplicate_error(identifier: Any, seen: set[str], path: str, errors: list[str]) -> None:
    if is_nonempty_string(identifier):
        normalized = identifier.casefold()
        if normalized in seen:
            errors.append(f"{path}: duplicate identifier (case-insensitive): {identifier}")
        seen.add(normalized)


def validate_tasks(items: Any, path: str, seen: set[str], errors: list[str]) -> None:
    tasks = expect_array(items, path, errors)
    if tasks is None:
        return
    for index, task in enumerate(tasks):
        task_path = f"{path}[{index}]"
        task_object = expect_object(task, task_path, errors)
        if task_object is None:
            continue
        validate_known_fields(task_object, {"id", "title", "lead", "keywords"}, task_path, errors)
        validate_required_strings(task_object, ("id", "title"), task_path, errors)
        duplicate_error(task_object.get("id"), seen, f"{task_path}.id", errors)
        if "lead" in task_object and not is_nonempty_string(task_object["lead"]):
            errors.append(f"{task_path}.lead: expected non-empty string")
        validate_string_array(task_object, "keywords", task_path, errors)


def validate_deliverables(items: Any, path: str, seen: set[str], errors: list[str]) -> None:
    deliverables = expect_array(items, path, errors)
    if deliverables is None:
        return
    for index, deliverable in enumerate(deliverables):
        item_path = f"{path}[{index}]"
        item = expect_object(deliverable, item_path, errors)
        if item is None:
            continue
        validate_known_fields(item, {"id", "title", "lead", "type", "due_month"}, item_path, errors)
        validate_required_strings(item, ("id", "title"), item_path, errors)
        duplicate_error(item.get("id"), seen, f"{item_path}.id", errors)
        for field in ("lead", "type", "due_month"):
            if field in item and not is_nonempty_string(item[field]):
                errors.append(f"{item_path}.{field}: expected non-empty string")


def validate_workpackages(items: Any, path: str, errors: list[str]) -> set[str]:
    workpackages = expect_array(items, path, errors)
    wp_ids: set[str] = set()
    if workpackages is None:
        return wp_ids
    seen: set[str] = set()
    task_ids: set[str] = set()
    deliverable_ids: set[str] = set()
    allowed = {"id", "number", "title", "lead", "boku_role", "status", "aliases", "keywords",
               "contacts", "tasks", "deliverables"}
    for index, workpackage in enumerate(workpackages):
        wp_path = f"{path}[{index}]"
        wp = expect_object(workpackage, wp_path, errors)
        if wp is None:
            continue
        validate_known_fields(wp, allowed, wp_path, errors)
        validate_required_strings(wp, ("id", "title", "status"), wp_path, errors)
        identifier = wp.get("id")
        if not is_slug(identifier):
            errors.append(f"{wp_path}.id: expected lowercase slug")
        else:
            wp_ids.add(identifier.casefold())
        duplicate_error(identifier, seen, f"{wp_path}.id", errors)
        if "number" in wp and (isinstance(wp["number"], bool) or not isinstance(wp["number"], (int, float)) or wp["number"] <= 0):
            errors.append(f"{wp_path}.number: expected positive number")
        for field in ("lead", "boku_role"):
            if field in wp and not is_nonempty_string(wp[field]):
                errors.append(f"{wp_path}.{field}: expected non-empty string")
        if wp.get("status") not in WP_STATUSES:
            errors.append(f"{wp_path}.status: expected one of {', '.join(sorted(WP_STATUSES))}")
        for field in ("aliases", "keywords"):
            validate_string_array(wp, field, wp_path, errors)
        validate_contacts(wp, "contacts", wp_path, errors)
        validate_tasks(wp.get("tasks"), f"{wp_path}.tasks", task_ids, errors)
        validate_deliverables(wp.get("deliverables"), f"{wp_path}.deliverables", deliverable_ids, errors)
    return wp_ids


def validate_milestones(items: Any, wp_ids: set[str], path: str, errors: list[str]) -> None:
    milestones = expect_array(items, path, errors)
    if milestones is None:
        return
    seen: set[str] = set()
    allowed = {"id", "title", "lead", "due_month", "related_wps", "prerequisites"}
    for index, milestone in enumerate(milestones):
        item_path = f"{path}[{index}]"
        item = expect_object(milestone, item_path, errors)
        if item is None:
            continue
        validate_known_fields(item, allowed, item_path, errors)
        validate_required_strings(item, ("id", "title"), item_path, errors)
        duplicate_error(item.get("id"), seen, f"{item_path}.id", errors)
        for field in ("lead", "due_month", "prerequisites"):
            if field in item and not is_nonempty_string(item[field]):
                errors.append(f"{item_path}.{field}: expected non-empty string")
        related = item.get("related_wps")
        if related is not None:
            values = expect_array(related, f"{item_path}.related_wps", errors)
            if values is not None:
                for related_index, wp_id in enumerate(values):
                    related_path = f"{item_path}.related_wps[{related_index}]"
                    if not is_nonempty_string(wp_id):
                        errors.append(f"{related_path}: expected non-empty string")
                    elif wp_id.casefold() not in wp_ids:
                        errors.append(f"{related_path}: unknown workpackage id: {wp_id}")


def validate_project(project: Any, path: str, errors: list[str]) -> str | None:
    item = expect_object(project, path, errors)
    if item is None:
        return None
    validate_required_strings(item, ("id", "title", "mailbox_folder"), path, errors)
    project_id = item.get("id")
    if not is_slug(project_id):
        errors.append(f"{path}.id: expected lowercase slug")
    if item.get("schema_version") != 3:
        errors.append(f"{path}.schema_version: expected exactly 3")
    for field in ("workpackages", "milestones"):
        if field not in item:
            errors.append(f"{path}.{field}: required array")
    for field in ("aliases", "keywords", "domains", "typical_subject_patterns", "do_not_route_if"):
        validate_string_array(item, field, path, errors)
    for field in ROOT_STRING_FIELDS:
        if field in item and not isinstance(item[field], str):
            errors.append(f"{path}.{field}: expected string")
    validate_contacts(item, "contacts", path, errors)
    if "cloud_sync" in item and not isinstance(item["cloud_sync"], dict):
        errors.append(f"{path}.cloud_sync: expected object")
    if "routing_priority" in item and (isinstance(item["routing_priority"], bool) or not isinstance(item["routing_priority"], (int, float))):
        errors.append(f"{path}.routing_priority: expected number")
    workpackage_ids = validate_workpackages(item.get("workpackages"), f"{path}.workpackages", errors)
    validate_milestones(item.get("milestones"), workpackage_ids, f"{path}.milestones", errors)
    return project_id if is_nonempty_string(project_id) else None


def validate_catalog(document: Any) -> list[str]:
    errors: list[str] = []
    if isinstance(document, list):
        projects = document
        project_path = "$"
    elif isinstance(document, dict):
        if "projects" not in document:
            return ["$: expected a project list or an object with a projects list"]
        projects = expect_array(document["projects"], "$.projects", errors)
        project_path = "$.projects"
        if projects is None:
            return errors
    else:
        return ["$: expected a project list or an object with a projects list"]
    seen: set[str] = set()
    for index, project in enumerate(projects):
        identifier = validate_project(project, f"{project_path}[{index}]", errors)
        if identifier is not None:
            duplicate_error(identifier, seen, f"{project_path}[{index}].id", errors)
    return errors


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = JsonArgumentParser(description="Validate a project catalog against schema v3 invariants.")
    parser.add_argument("--catalog", default=DEFAULT_CATALOG,
                        help=f"Path to catalog JSON (default: {DEFAULT_CATALOG}).")
    parser.add_argument("--json", action="store_true", help="Emit the standard JSON envelope.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    json_requested = "--json" in (sys.argv[1:] if argv is None else argv)
    try:
        args = parse_args(argv)
        path = Path(args.catalog)
        if not path.is_file():
            result = envelope(False, "Failed", "Catalog file was not found; pass an existing JSON file with --catalog.",
                              error={"code": "input_not_found", "path": str(path)})
            print(json.dumps(result, ensure_ascii=False) if args.json else result["message"])
            return 2
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            result = envelope(False, "Failed", "Catalog is not valid JSON; correct the reported location and retry.",
                              error={"code": "invalid_json", "path": f"{path}:{exc.lineno}:{exc.colno}", "message": exc.msg})
            print(json.dumps(result, ensure_ascii=False) if args.json else result["message"])
            return 2
        errors = validate_catalog(document)
        if errors:
            result = envelope(False, "Invalid", "Catalog violates schema v3 invariants; correct the listed JSON paths.",
                              data={"catalog": str(path), "errors": errors},
                              error={"code": "validation_failed", "message": f"{len(errors)} validation error(s)"})
            print(json.dumps(result, ensure_ascii=False) if args.json else "\n".join(errors))
            return 1
        result = envelope(True, "Valid", "Catalog conforms to project schema v3.", data={"catalog": str(path)})
        print(json.dumps(result, ensure_ascii=False) if args.json else result["message"])
        return 0
    except Exception as exc:  # Defensive CLI boundary: retain the standard envelope.
        result = envelope(False, "Failed", "Validator failed unexpectedly; inspect the error and retry.",
                          error={"code": "runtime_error", "message": str(exc)})
        print(json.dumps(result, ensure_ascii=False) if json_requested else result["message"])
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
