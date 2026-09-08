#!/usr/bin/env python3
"""Conservative, idempotent v2-to-v3 project-catalog migration tool."""
from __future__ import annotations

import argparse
import copy
import difflib
import importlib.util
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any


DEFAULT_CATALOG = "memory/references/projects/projects.json"
DEFAULT_PROJECTS_ROOT = "memory/references/projects"
STATUS = {"active", "completed", "planned", "paused"}
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def envelope(success: bool, state: str, message: str, data: dict[str, Any] | None = None,
             error: dict[str, str] | None = None) -> dict[str, Any]:
    return {"action": "migrate_project_wps", "success": success, "state": state,
            "message": message, "data": data or {}, "error": error}


def load_validator() -> Any:
    path = Path(__file__).with_name("validate_projects.py")
    spec = importlib.util.spec_from_file_location("project_catalog_validator", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load validate_projects.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_guard() -> Any:
    path = Path(__file__).resolve().parents[4] / "workspace-lock" / "scripts" / "workspace_lock_guard.py"
    spec = importlib.util.spec_from_file_location("workspace_lock_guard", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("canonical workspace-lock guard is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def project_list(document: Any) -> tuple[list[Any], str] | None:
    if isinstance(document, list):
        return document, "$"
    if isinstance(document, dict) and isinstance(document.get("projects"), list):
        return document["projects"], "$.projects"
    return None


def slug(value: str) -> str | None:
    candidate = value.strip().lower().replace(" ", "")
    return candidate if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", candidate) else None


def section(lines: list[str], name: str) -> list[str] | None:
    start = next((i + 1 for i, line in enumerate(lines) if re.fullmatch(rf"##\s+{re.escape(name)}(?:\s*\([^)]*\))?\s*", line.strip(), re.I)), None)
    if start is None:
        return None
    result: list[str] = []
    for line in lines[start:]:
        if line.strip().startswith("## "):
            break
        result.append(line)
    return result


def labeled(parts: list[str], names: dict[str, str]) -> dict[str, str] | None:
    values: dict[str, str] = {}
    for part in parts:
        key, sep, value = part.partition(":")
        if not sep or key.strip().casefold() not in names or not value.strip():
            return None
        values[names[key.strip().casefold()]] = value.strip()
    return values


def parse_items(lines: list[str] | None, kind: str, path: Path, diagnostics: list[dict[str, str]]) -> list[dict[str, str]]:
    if lines is None:
        return []
    result: list[dict[str, str]] = []
    names = ({"lead": "lead"} if kind == "task" else
             {"lead": "lead", "type": "type", "due": "due_month", "due_month": "due_month"})
    for number, line in enumerate(lines, 1):
        text = line.strip()
        if not text or line[:1].isspace() or "BEGIN:managed" in text or "END:managed" in text:
            continue
        if not text.startswith("- "):
            continue
        parts = [part.strip() for part in text[2:].split("—")]
        if len(parts) < 2 or not parts[0] or not parts[1]: continue
        values = labeled(parts[2:], names)
        if values is None:
            continue
        result.append({"id": parts[0].strip("* "), "title": parts[1].strip("* "), **values})
    return result


def parse_wp(path: Path, diagnostics: list[dict[str, str]]) -> dict[str, Any] | None:
    lines = path.read_text(encoding="utf-8").splitlines()
    for number, line in enumerate(lines, 1):
        normalized = line.strip().casefold()
        if "checkpoint" in normalized:
            diagnostics.append({"path": f"{path}:{number}", "code": "unstable_checkpoint", "severity": "warning", "message": "checkpoint without stable MS id is not migrated"})
    header = next((line.strip()[2:] for line in lines if line.strip().startswith("# ")), None)
    if not header or "—" not in header:
        diagnostics.append({"path": str(path), "code": "ambiguous_markdown", "message": "WP heading must be '# <id> — <title>'"})
        return None
    raw_id, title = (part.strip() for part in header.split("—", 1))
    wp_id = slug(raw_id)
    if not wp_id or not title:
        diagnostics.append({"path": str(path), "code": "ambiguous_markdown", "message": "WP heading has unsafe id or empty title"})
        return None
    wp: dict[str, Any] = {"id": wp_id, "title": title, "tasks": [], "deliverables": []}
    scope = section(lines, "Scope") or []
    for line in scope:
        text = line.strip()[2:] if line.strip().startswith("- ") else ""
        key, sep, value = text.partition(":")
        mapping = {"lead": "lead", "boku-rolle": "boku_role", "status": "status", "wp-nummer": "number"}
        if not sep or key.strip().casefold() not in mapping:
            continue
        target, value = mapping[key.strip().casefold()], value.strip()
        if target == "number":
            try: wp[target] = float(value) if "." in value else int(value)
            except ValueError: diagnostics.append({"path": str(path), "code": "ambiguous_markdown", "message": "invalid WP number"})
        elif target == "status":
            if value not in STATUS: diagnostics.append({"path": str(path), "code": "ambiguous_markdown", "message": "invalid WP status"})
            else: wp[target] = value
        elif value: wp[target] = value
    task_lines = section(lines, "Tasks") or []
    deliverable_lines = section(lines, "Deliverables") or []
    wp["tasks"] = parse_items(task_lines, "task", path, diagnostics)
    wp["deliverables"] = parse_items(deliverable_lines, "deliverable", path, diagnostics)
    for line in task_lines:
        match = re.fullmatch(r"###\s+(Task\s+[^—]+)\s+—\s+(.+)", line.strip(), re.I)
        if match:
            wp["tasks"].append({"id": match.group(1).strip(), "title": match.group(2).strip()})
        if line[:1].isspace(): continue
        stable = re.fullmatch(r"-\s+(T\d+(?:\.\d+)?)\s+—\s+(.+)", line.strip(), re.I)
        if stable:
            if not any(item.get("id", "").casefold() == stable.group(1).casefold() for item in wp["tasks"]):
                wp["tasks"].append({"id": stable.group(1), "title": stable.group(2).strip("* ")})
    for line in deliverable_lines:
        if line[:1].isspace(): continue
        stable = re.fullmatch(r"-\s+(D\d+(?:\.\d+)?)\s+—\s+(.+)", line.strip(), re.I)
        if stable and not any(item.get("id", "").casefold() == stable.group(1).casefold() for item in wp["deliverables"]):
            wp["deliverables"].append({"id": stable.group(1), "title": stable.group(2).strip("* ")})
    return wp


def merge_artifacts(existing: list[Any], parsed: list[dict[str, Any]], kind: str, diagnostics: list[dict[str, str]], path: str) -> list[Any]:
    result = copy.deepcopy(existing); by_id = {item.get("id", "").casefold(): item for item in result if isinstance(item, dict)}
    for item in parsed:
        prior = by_id.get(item["id"].casefold())
        if prior is None: result.append(item); by_id[item["id"].casefold()] = item; continue
        for key, value in item.items():
            if key in prior and prior[key] != value:
                diagnostics.append({"path": path, "code": f"{kind}_conflict", "message": f"conflicting {kind} {item['id']} field {key}"})
            elif key not in prior: prior[key] = value
    return result


def wp_identity(value: Any) -> str:
    """Normalize only stable WP/Activity-Cluster spellings for conservative matching."""
    text = str(value or "").casefold()
    text = re.sub(r"activity\s*cluster", "ac", text)
    return re.sub(r"[^a-z0-9]", "", text)


def match_existing_wp(parsed: dict[str, Any], existing: list[Any], diagnostics: list[dict[str, str]], path: str) -> dict[str, Any] | None:
    parsed_id = str(parsed.get("id", "")).casefold()
    exact = [item for item in existing if isinstance(item, dict) and str(item.get("id", "")).casefold() == parsed_id]
    if len(exact) == 1:
        return exact[0]
    title = str(parsed.get("title", "")).casefold().strip()
    candidates: list[dict[str, Any]] = []
    for item in existing:
        if not isinstance(item, dict):
            continue
        aliases = [item.get("id"), *(item.get("aliases") if isinstance(item.get("aliases"), list) else [])]
        title_match = title and str(item.get("title", "")).casefold().strip() == title
        alias_match = wp_identity(parsed.get("id")) and any(wp_identity(value) == wp_identity(parsed.get("id")) for value in aliases)
        if title_match or alias_match:
            candidates.append(item)
    unique = {str(item.get("id", "")).casefold(): item for item in candidates}
    if len(unique) == 1:
        return next(iter(unique.values()))
    if len(unique) > 1:
        diagnostics.append({"path": path, "code": "ambiguous_wp_match", "message": f"multiple existing WPs match parsed {parsed.get('id')}"})
    return None


def parse_milestones(path: Path, diagnostics: list[dict[str, str]]) -> list[dict[str, Any]]:
    lines = section(path.read_text(encoding="utf-8").splitlines(), "Milestones")
    if lines is None: return []
    result: list[dict[str, Any]] = []
    for number, line in enumerate(lines, 1):
        text = line.strip()
        if not text or line[:1].isspace() or "managed" in text.casefold(): continue
        parts = [part.strip() for part in text[2:].split("—")] if text.startswith("- ") else []
        values = labeled(parts[2:], {"lead": "lead", "due": "due_month", "due_month": "due_month", "related wps": "related_wps", "prerequisites": "prerequisites"}) if len(parts) >= 2 else None
        if values is None:
            diagnostics.append({"path": f"{path}:{number}", "code": "ambiguous_markdown", "message": "cannot parse milestone entry"}); continue
        item: dict[str, Any] = {"id": parts[0], "title": parts[1]}
        for key, value in values.items(): item[key] = [part.strip() for part in value.split(",") if part.strip()] if key == "related_wps" else value
        result.append(item)
    return result


def parse_wp_milestones(path: Path, wp_id: str, diagnostics: list[dict[str, str]]) -> list[dict[str, Any]]:
    lines = section(path.read_text(encoding="utf-8").splitlines(), "Milestones") or []
    result: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in lines:
        text = line.strip()
        stable = re.fullmatch(r"-\s+(MS\d+(?:\.\d+)?)\s+—\s+(.+)", text, re.I) if not line[:1].isspace() else None
        if stable:
            current = {"id": stable.group(1), "title": stable.group(2).strip("* "), "related_wps": [wp_id]}
            result.append(current); continue
        if current is None or not line[:1].isspace() or not text.startswith("-"): continue
        key, sep, value = text[1:].strip().partition(":")
        if not sep or not value.strip(): continue
        if key.strip().casefold() in {"lead", "lead beneficiary"}: current["lead"] = value.strip()
        elif key.strip().casefold() in {"fälligkeit", "due", "due month"}: current["due_month"] = value.strip()
    return result


def parse_project_sources(project_id: str, projects_root: Path, diagnostics: list[dict[str, str]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
    directory = projects_root / project_id / "workpackages"
    if not directory.is_dir():
        return None
    files = sorted(path for path in directory.glob("*.md") if path.name.casefold() != "readme.md")
    if not files:
        return None
    diagnostic_start = len(diagnostics)
    result = [parse_wp(path, diagnostics) for path in files]
    first_checkpoint = True
    compacted = diagnostics[:diagnostic_start]
    for item in diagnostics[diagnostic_start:]:
        if item.get("code") == "unstable_checkpoint":
            if not first_checkpoint: continue
            first_checkpoint = False
        compacted.append(item)
    diagnostics[:] = compacted
    index = directory.parent / "index.md"
    milestones = parse_milestones(index, diagnostics) if index.is_file() else []
    for file, wp in zip(files, result):
        if wp is not None: milestones.extend(parse_wp_milestones(file, wp["id"], diagnostics))
    deduped: dict[str, dict[str, Any]] = {}
    for milestone in milestones:
        key = milestone["id"].casefold(); prior = deduped.get(key)
        if prior is None: deduped[key] = milestone; continue
        if prior.get("title", "").casefold() != milestone.get("title", "").casefold():
            diagnostics.append({"path": str(directory), "code": "milestone_conflict", "message": f"conflicting milestone id: {milestone['id']}"}); continue
        prior["related_wps"] = sorted(set(prior.get("related_wps", []) + milestone.get("related_wps", [])))
        for field in ("lead", "due_month", "prerequisites"):
            if field not in prior and field in milestone: prior[field] = milestone[field]
    return [item for item in result if item is not None], list(deduped.values())


def transform_project(project: dict[str, Any], projects_root: Path, diagnostics: list[dict[str, str]]) -> dict[str, Any]:
    result = copy.deepcopy(project)
    project_id = result.get("id")
    if result.get("schema_version") == 3:
        return result
    if not isinstance(project_id, str):
        diagnostics.append({"path": "$.id", "code": "invalid_project", "message": "project has no string id"})
        return result
    source = parse_project_sources(project_id, projects_root, diagnostics)
    existing = result.get("workpackages", [])
    if source is not None:
        source_wps, source_milestones = source
        replacements: dict[str, dict[str, Any]] = {}
        new_workpackages: list[dict[str, Any]] = []
        for wp in source_wps:
            diagnostic_count = len(diagnostics)
            matched = match_existing_wp(wp, existing, diagnostics, f"$.{project_id}.workpackages")
            if matched is None and any(item.get("code") == "ambiguous_wp_match" for item in diagnostics[diagnostic_count:]):
                continue
            prior = copy.deepcopy(matched or {})
            for key, value in wp.items():
                if key == "id" and matched is not None:
                    continue
                if key in {"tasks", "deliverables"}:
                    if not value and key in prior: continue
                    prior[key] = merge_artifacts(prior.get(key, []), value, key[:-1], diagnostics, f"$.{project_id}.{wp['id']}.{key}")
                    continue
                prior[key] = value
            prior.setdefault("status", "active")
            prior.setdefault("tasks", [])
            prior.setdefault("deliverables", [])
            if matched is None:
                new_workpackages.append(prior)
            else:
                replacements[str(matched.get("id", "")).casefold()] = prior
        merged: list[Any] = []
        for prior in existing:
            if isinstance(prior, dict):
                identifier = str(prior.get("id", "")).casefold()
                merged.append(replacements.get(identifier, dict(prior, tasks=prior.get("tasks", []), deliverables=prior.get("deliverables", []))))
            else:
                merged.append(prior)
        merged.extend(new_workpackages)
        result["workpackages"] = merged
    elif not isinstance(existing, list):
        diagnostics.append({"path": f"$.{project_id}.workpackages", "code": "invalid_catalog", "message": "workpackages is not an array"})
    else:
        result["workpackages"] = [dict(item, tasks=item.get("tasks", []), deliverables=item.get("deliverables", [])) if isinstance(item, dict) else item for item in existing]
    if source is not None:
        existing_milestones = result.get("milestones", [])
        if not isinstance(existing_milestones, list):
            diagnostics.append({"path": f"$.{project_id}.milestones", "code": "invalid_catalog", "message": "milestones is not an array"})
        else:
            merged_milestones = copy.deepcopy(existing_milestones)
            by_id = {item.get("id", "").casefold(): item for item in merged_milestones if isinstance(item, dict)}
            for milestone in source_milestones:
                prior = by_id.get(milestone["id"].casefold())
                if prior is None:
                    merged_milestones.append(milestone); by_id[milestone["id"].casefold()] = milestone; continue
                if prior.get("title", "").casefold() != milestone.get("title", "").casefold():
                    diagnostics.append({"path": f"$.{project_id}.milestones", "code": "milestone_conflict", "message": f"conflicting milestone id: {milestone['id']}"}); continue
                prior["related_wps"] = sorted(set(prior.get("related_wps", []) + milestone.get("related_wps", [])))
            result["milestones"] = merged_milestones
    elif "milestones" not in result: result["milestones"] = []
    if not isinstance(result["milestones"], list):
        diagnostics.append({"path": f"$.{project_id}.milestones", "code": "invalid_catalog", "message": "milestones is not an array"})
    result["schema_version"] = 3
    return result


def transform(document: Any, projects_root: Path, scopes: list[str]) -> tuple[Any, list[dict[str, str]]]:
    result = copy.deepcopy(document); diagnostics: list[dict[str, str]] = []
    located = project_list(result)
    if located is None:
        return result, [{"path": "$", "code": "invalid_root", "message": "expected list or object with projects list"}]
    projects, _ = located; selected = set(scopes)
    found: set[str] = set()
    for index, project in enumerate(projects):
        if not isinstance(project, dict):
            diagnostics.append({"path": f"$[{index}]", "code": "invalid_project", "message": "expected object"}); continue
        identifier = project.get("id")
        if selected and identifier not in selected: continue
        if isinstance(identifier, str): found.add(identifier)
        projects[index] = transform_project(project, projects_root, diagnostics)
    for missing in sorted(selected - found):
        diagnostics.append({"path": "$", "code": "unknown_project_scope", "message": f"project scope not found: {missing}"})
    return result, diagnostics


def atomic_write(path: Path, document: Any) -> None:
    payload = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False)
    try:
        handle.write(payload); handle.flush(); os.fsync(handle.fileno()); handle.close()
        os.replace(handle.name, path)
    except Exception:
        handle.close()
        try: Path(handle.name).unlink(missing_ok=True)
        except OSError: pass
        raise


def diff(before: Any, after: Any, name: str) -> str:
    left = (json.dumps(before, ensure_ascii=False, indent=2) + "\n").splitlines(True)
    right = (json.dumps(after, ensure_ascii=False, indent=2) + "\n").splitlines(True)
    return "".join(difflib.unified_diff(left, right, fromfile=f"{name} (before)", tofile=f"{name} (after)"))


def args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = Parser(description="Conservatively migrate project catalog v2 entries to v3.")
    parser.add_argument("--catalog", default=DEFAULT_CATALOG); parser.add_argument("--projects-root", default=DEFAULT_PROJECTS_ROOT)
    parser.add_argument("--project", "--project-scope", action="append", default=[])
    mode = parser.add_mutually_exclusive_group(); mode.add_argument("--apply", action="store_true"); mode.add_argument("--dry-run", action="store_true")
    parser.add_argument("--workspace-root"); parser.add_argument("--lease-id"); parser.add_argument("--conversation-id")
    parser.add_argument("--accept-warning", action="append", default=[], metavar="CODE",
                        help="Human-approved warning code required for Apply; repeatable.")
    parser.add_argument("--json", action="store_true"); return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    json_mode = "--json" in (sys.argv[1:] if argv is None else argv)
    try:
        options = args(argv); catalog = Path(options.catalog); root = Path(options.projects_root)
        if not catalog.is_file(): raise FileNotFoundError(f"catalog not found: {catalog}")
        try: before = json.loads(catalog.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc: raise ValueError(f"invalid JSON at {exc.lineno}:{exc.colno}: {exc.msg}")
        after, diagnostics = transform(before, root, options.project)
        plan = {"catalog": str(catalog), "projects_root": str(root), "scopes": options.project, "diff": diff(before, after, str(catalog)), "diagnostics": diagnostics}
        warnings = [item for item in diagnostics if item.get("severity") == "warning"]
        warning_codes = sorted({str(item.get("code")) for item in warnings})
        accepted_codes = sorted(set(options.accept_warning))
        missing_codes = sorted(set(accepted_codes) - set(warning_codes))
        accepted_warnings = [item for item in warnings if item.get("code") in accepted_codes]
        unaccepted_codes = sorted(set(warning_codes) - set(accepted_codes))
        plan.update({"warning_codes": warning_codes, "accepted_warning_codes": accepted_codes,
                     "unaccepted_warning_codes": unaccepted_codes, "accepted_warnings": accepted_warnings})
        if missing_codes:
            result = envelope(False, "Invalid", "Requested warning acceptance is absent from this run; correct --accept-warning and retry.", plan,
                              {"code": "unknown_warning_acceptance", "message": ", ".join(missing_codes)})
            print(json.dumps(result, ensure_ascii=False) if options.json else result["message"]); return 1
        blocking = [item for item in diagnostics if item.get("severity") != "warning"]
        if blocking or (options.apply and unaccepted_codes):
            result = envelope(False, "PendingReview", "Ambiguous or invalid source data; no catalog was written.", plan, {"code": "pending_review", "message": "resolve diagnostics and retry"})
            print(json.dumps(result, ensure_ascii=False) if options.json else result["message"]); return 1
        selected_after = after
        if options.project:
            located = project_list(after)
            assert located is not None
            selected_after = [project for project in located[0] if isinstance(project, dict) and project.get("id") in set(options.project)]
        validation_errors = load_validator().validate_catalog(after if options.apply or not options.project else selected_after)
        if validation_errors:
            result = envelope(False, "Invalid", "Migration result fails schema v3 validation; no catalog was written.", {**plan, "validation_errors": validation_errors}, {"code": "validation_failed", "message": "correct source data"})
            print(json.dumps(result, ensure_ascii=False) if options.json else result["message"]); return 1
        if not options.apply:
            plan["full_catalog_validation"] = "deferred" if options.project else "passed"
            result = envelope(True, "NoChanges" if before == after else "DryRun", "Dry-run completed; no catalog was written.", plan)
            print(json.dumps(result, ensure_ascii=False) if options.json else result["message"]); return 0
        workspace = Path(options.workspace_root) if options.workspace_root else catalog.parents[3]
        load_guard().require_workspace_lock(workspace, lease_id=options.lease_id, conversation_id=options.conversation_id)
        atomic_write(catalog, after)
        result = envelope(True, "NoChanges" if before == after else "Applied", "Migration applied and validated.", plan)
        print(json.dumps(result, ensure_ascii=False) if options.json else result["message"]); return 0
    except (OSError, ValueError, RuntimeError) as exc:
        result = envelope(False, "Failed", "Migration failed; correct input, lock ownership, or runtime configuration and retry.", error={"code": "runtime_error", "message": str(exc)})
        print(json.dumps(result, ensure_ascii=False) if json_mode else result["message"]); return 2


if __name__ == "__main__": raise SystemExit(main())
