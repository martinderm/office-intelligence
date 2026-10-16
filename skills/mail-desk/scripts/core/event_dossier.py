"""Read-only event preparation; mailbox and event writes stay with their owners.

This module validates snapshots, not live mailbox truth or human authority.
Review receipts bind bytes; they are never generated or upgraded here.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat

from .batch_contract import canonical_execute_request_sha256 as inner_hash, validate_execute_contract
from .common import normalize_message_id
from .modes.dossier_apply import (
    canonical_execute_request_sha256 as outer_hash,
    _require_dossier_execute_request, _require_review_receipt,
)
from .modes.dossier_synthesis import canonical_json_sha256, _require_completed_apply

MAX_REQUEST_BYTES = 2 * 1024 * 1024
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_SLUG = re.compile(r"[a-z0-9][a-z0-9_-]{0,99}\Z")
_STAGES = {
    "plan": {"scope", "inspect_ids"},
    "review": {"scope", "sources", "operative_request", "attachment_requests"},
    "apply": {"scope", "sources", "operative_request", "attachment_requests"},
    "handoff": {"scope", "sources", "apply_request", "apply_result", "apply_result_sha256", "event"},
    "report": {"artifacts", "work_order", "output_receipt"},
}
_LOCATION = {"account", "folder", "envelope_id", "message_id"}
_READ = _LOCATION | {"content_sha256", "references", "in_reply_to"}
_ATTACHMENT = _LOCATION | {
    "filename", "original_filename", "mime_type", "size_bytes", "sha256", "part_locator",
    "fetch_status", "provenance", "policy_status",
}


def strict_json(raw: str) -> object:
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("Duplicate JSON field")
            result[key] = value
        return result

    def constant(_value):
        raise ValueError("Non-finite JSON number")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def _shape(value, allowed, required=None):
    if not isinstance(value, dict) or set(value) - allowed or (required or set()) - set(value):
        raise ValueError("Unsupported or missing fields")
    return value


def _text(value, limit=500):
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > limit or any(ord(c) < 32 for c in value)):
        raise ValueError("Expected bounded non-empty data string")
    return value


def _integer(value, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError("Expected bounded positive integer")
    return value


def _hash(value):
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise ValueError("Expected lowercase SHA-256")
    return value


def _list(value, maximum, *, nonempty=False):
    if not isinstance(value, list) or len(value) > maximum or (nonempty and not value):
        raise ValueError("Expected bounded list")
    return value


def _scope(value):
    keys = {"account", "folders", "terms", "scan_limit_per_folder", "inspect_limit"}
    scope = _shape(value, keys, keys)
    _text(scope["account"], 100)
    for key, maximum in (("folders", 20), ("terms", 20)):
        values = [_text(v, 200) for v in _list(scope[key], maximum, nonempty=True)]
        if len(set(values)) != len(values):
            raise ValueError("Duplicate scope entries")
    _integer(scope["scan_limit_per_folder"], 500)
    _integer(scope["inspect_limit"], 100)
    if scope["inspect_limit"] > len(scope["folders"]) * scope["scan_limit_per_folder"]:
        raise ValueError("Inspection exceeds scan scope")
    return scope


def _completeness(scope):
    return {"exhaustive": False, "scan_basis": "first_page_envelopes", "scope": deepcopy(scope),
            "limitations": ["Only explicit folders, terms and bounded first-page envelopes are covered.",
                            "Caller-provided snapshots require live workspace/account revalidation."]}


def _location(value, scope):
    for key in _LOCATION:
        _text(value.get(key))
    if value["account"] != scope["account"] or value["folder"] not in scope["folders"]:
        raise ValueError("Source outside explicit account/folder scope")
    mid = normalize_message_id(value["message_id"])
    if not mid:
        raise ValueError("Missing message identity")
    return {**{key: value[key] for key in ("account", "folder", "envelope_id")}, "message_id": mid}


def _sources(raw, scope):
    messages, blobs, calendars, locators, parts = {}, {}, {}, {}, {}
    parents = set()
    for source in _list(raw, scope["inspect_limit"], nonempty=True):
        _shape(source, {"search", "read", "attachments", "calendar_objects"},
               {"search", "read", "attachments", "calendar_objects"})
        search = _shape(source["search"], _LOCATION, _LOCATION)
        read = _shape(source["read"], _READ, _READ)
        location = _location(search, scope)
        if _location(read, scope) != location:
            raise ValueError("Read/search locator drift")
        mid, digest = location["message_id"], _hash(read["content_sha256"])
        locator = tuple(location[key] for key in ("account", "folder", "envelope_id"))
        identity = (mid, digest)
        if locator in locators and locators[locator] != identity:
            raise ValueError("Conflicting source locator")
        locators[locator] = identity
        if mid in messages and messages[mid]["content_sha256"] != digest:
            raise ValueError("Conflicting content for one message identity")
        references = [normalize_message_id(_text(v)) for v in _list(read["references"], 100)]
        reply = read["in_reply_to"]
        if reply is not None:
            references.append(normalize_message_id(_text(reply)))
        if any(not v for v in references):
            raise ValueError("Invalid thread parent")
        parents.update(references)
        message = messages.setdefault(mid, {"id": "mail-" + canonical_json_sha256(mid), "kind": "mail",
            "message_id": mid, "content_sha256": digest, "locations": [], "thread_parents": [], "calendar_objects": []})
        if location not in message["locations"]:
            message["locations"].append(location)
        message["thread_parents"] = sorted(set(message["thread_parents"]) | set(references))
        for attachment in _list(source["attachments"], 100):
            _shape(attachment, _ATTACHMENT, _ATTACHMENT)
            if _location(attachment, scope) != location:
                raise ValueError("Attachment locator drift")
            sha = _hash(attachment["sha256"])
            for field in ("filename", "original_filename", "mime_type", "part_locator"):
                _text(attachment[field])
            for field in ("filename", "original_filename"):
                name = attachment[field]
                if name in {".", ".."} or any(c in name for c in '/\\:'):
                    raise ValueError("Attachment name must be a literal basename")
            size = attachment["size_bytes"]
            if type(size) is not int or not 0 <= size <= 1024 ** 3:
                raise ValueError("Invalid attachment size")
            if (attachment["fetch_status"] != "available" or attachment["policy_status"] != "allowed"
                    or attachment["provenance"] != "rfc822_mime"):
                raise ValueError("Attachment is not verified available MIME evidence")
            provenance = {**location, **{key: attachment[key] for key in
                ("part_locator", "filename", "original_filename", "mime_type")}}
            part = (*locator, attachment["part_locator"])
            fingerprint = (sha, size, attachment["filename"], attachment["original_filename"], attachment["mime_type"])
            if part in parts and parts[part] != fingerprint:
                raise ValueError("Conflicting attachment part locator")
            parts[part] = fingerprint
            blob = blobs.setdefault(sha, {"id": "attachment-" + sha, "sha256": sha,
                                        "size_bytes": size, "provenance": []})
            if blob["size_bytes"] != size:
                raise ValueError("Conflicting attachment size for one hash")
            if provenance not in blob["provenance"]:
                blob["provenance"].append(provenance)
        for calendar in _list(source["calendar_objects"], 100):
            keys = {"uid", "sequence", "recurrence_id", "content_sha256"}
            _shape(calendar, keys, keys)
            _text(calendar["uid"])
            if type(calendar["sequence"]) is not int or calendar["sequence"] < 0:
                raise ValueError("Invalid calendar sequence")
            if calendar["recurrence_id"] is not None:
                _text(calendar["recurrence_id"])
            _hash(calendar["content_sha256"])
            variants = calendars.setdefault(mid, [])
            if calendar not in variants:
                variants.append(deepcopy(calendar))
            if calendar not in message["calendar_objects"]:
                message["calendar_objects"].append(deepcopy(calendar))
    conflicts = [{"message_id": mid, "variants": variants}
                 for mid, variants in sorted(calendars.items()) if len(variants) > 1]
    # Also flag a UID/recurrence version conflict across different messages.
    versions = {}
    for variants in calendars.values():
        for variant in variants:
            key = (variant["uid"], variant["recurrence_id"])
            versions.setdefault(key, set()).add((variant["sequence"], variant["content_sha256"]))
    conflicts.extend({"uid": key[0], "recurrence_id": key[1], "versions": sorted(value)}
                     for key, value in sorted(versions.items(), key=lambda item: str(item[0])) if len(value) > 1)
    return list(messages.values()), list(blobs.values()), conflicts, sorted(parents - messages.keys())


def _operative(value, scope, sources, *, approved=False):
    keys = {"mode", "project", "account", "execute_request", "delete_input_on_success", "review"}
    request = _shape(value, keys, keys)
    if (request["mode"] != "dossier_apply" or request["delete_input_on_success"] is not False
            or request["account"] != scope["account"]):
        raise ValueError("Invalid operative request boundary")
    if not isinstance(request["project"], str) or not _SLUG.fullmatch(request["project"]):
        raise ValueError("Invalid project ID")
    execute = _shape(request["execute_request"], {
        "mode", "account", "items", "expected_count", "allow_fewer", "candidate_count",
        "source_folder", "skip_known", "review",
    }, {"mode", "account", "items", "review"})
    if execute["account"] != scope["account"]:
        raise ValueError("Execute account drift")
    items = _list(execute["items"], scope["inspect_limit"], nonempty=True)
    locations = [location for source in sources for location in source["locations"]]
    for item in items:
        _shape(item, {"envelope_id", "source_folder", "message_id", "subject", "action", "decision",
                      "synthesis_targets", "raw_message_id"}, {"envelope_id", "source_folder", "message_id", "action", "decision"})
        _shape(item["action"], {"type", "target_folder"}, {"type", "target_folder"})
        _shape(item["decision"], {"kind", "id", "confidence", "needs_reply"}, {"kind", "id"})
        _text(item["action"]["target_folder"])
        location = {"account": execute["account"], "folder": item["source_folder"],
                    "envelope_id": str(item["envelope_id"]), "message_id": normalize_message_id(_text(item["message_id"]))}
        if location not in locations:
            raise ValueError("Operative item is not bound to inspected evidence")
    ids = [normalize_message_id(item["message_id"]) for item in items]
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate operative source")
    _require_dossier_execute_request(execute, project_id=request["project"],
        mailbox_folder=items[0]["action"]["target_folder"], normalize_id=normalize_message_id)
    for review, digest, outer in ((execute["review"], inner_hash(execute), False),
                                  (request["review"], outer_hash(execute), True)):
        _shape(review, {"required", "state", "execute_request_sha256", "approval_receipt"}, {"required", "state"})
        if review["required"] is not True or review["state"] not in {"pending", "approved"}:
            raise ValueError("Invalid review state")
        if "execute_request_sha256" in review and review["execute_request_sha256"] != digest:
            raise ValueError("Pending review hash drift")
        if review["state"] == "approved":
            receipt = _shape(review.get("approval_receipt"),
                             {"reviewed_at", "reviewed_by", "execute_request_sha256"},
                             {"reviewed_at", "reviewed_by", "execute_request_sha256"})
            _text(receipt["reviewed_at"])
            _text(receipt["reviewed_by"])
            if receipt["execute_request_sha256"] != digest:
                raise ValueError("Review receipt hash drift")
            if outer:
                _require_review_receipt(review, execute)
        elif approved or "approval_receipt" in review:
            raise ValueError("Explicit human approval required")
    contract = validate_execute_contract(execute, effective_account=scope["account"])
    if not contract["ok"] and contract.get("reason_code") != "review_required":
        raise ValueError("Invalid inner batch contract")
    if request["review"]["state"] == "approved" and execute["review"]["state"] != "approved":
        raise ValueError("Inner approval must precede outer approval")
    return {"inner_sha256": inner_hash(execute), "outer_sha256": outer_hash(execute),
            "approval_order": ["inner_execute", "outer_dossier_apply"]}


def _review(request, scope, *, approved=False):
    sources, attachments, conflicts, missing = _sources(request.get("sources"), scope)
    binding = _operative(request.get("operative_request"), scope, sources, approved=approved)
    # No fetching in this facade. Only exact allowlisted attachment inventories
    # may be handed back as requests, retaining the full original MIME locator.
    attachment_requests = _list(request.get("attachment_requests", []), 100)
    available = [entry for source in request["sources"] for entry in source["attachments"]]
    for item in attachment_requests:
        _shape(item, _ATTACHMENT, _ATTACHMENT)
        if item not in available:
            raise ValueError("Attachment request drift")
    return {"sources": sources, "attachments": attachments, "calendar_conflicts": conflicts,
            "missing_thread_parents": missing, "review_required": bool(conflicts or missing),
            "operative_request": deepcopy(request["operative_request"]),
            "attachment_requests": deepcopy(attachment_requests), "review_binding": binding}


def _event(value, project):
    required = {"owner_kind", "owner_id", "id", "title", "start"}
    event = _shape(value, required | {"end"}, required)
    if event["owner_kind"] != "project" or event["owner_id"] != project:
        raise ValueError("Event owner must match operative project")
    if not isinstance(event["id"], str) or not _SLUG.fullmatch(event["id"]):
        raise ValueError("Invalid event slug")
    _text(event["title"])
    start = date.fromisoformat(event["start"])
    if start.isoformat() != event["start"]:
        raise ValueError("Event start must use ISO YYYY-MM-DD")
    if "end" in event:
        end = date.fromisoformat(event["end"])
        if end.isoformat() != event["end"] or end < start:
            raise ValueError("Invalid event end")
    return deepcopy(event)


def _handoff(request, scope):
    reviewed = _review({"sources": request.get("sources"), "operative_request": request.get("apply_request")},
                       scope, approved=True)
    if reviewed["review_required"]:
        raise ValueError("Unresolved semantic review; do not hand off")
    operative, applied = request["apply_request"], request.get("apply_result")
    if request.get("apply_result_sha256") != canonical_json_sha256(applied):
        raise ValueError("Apply result hash drift")
    receipt, completed = _require_completed_apply(applied, project_id=operative["project"], normalize_id=normalize_message_id)
    if receipt != operative["review"]["approval_receipt"]:
        raise ValueError("Apply receipt does not bind reviewed request")
    expected = [normalize_message_id(item["message_id"]) for item in operative["execute_request"]["items"]]
    if [item["message_id"] for item in completed] != expected:
        raise ValueError("Partial or substituted apply result")
    for source, item in zip(completed, operative["execute_request"]["items"]):
        if source["synthesis_targets"] != item.get("synthesis_targets", []):
            raise ValueError("Apply result changed reviewed synthesis targets")
    # An event work order contains exactly the successfully applied source set,
    # never extra inspected candidates that were not approved for this apply.
    sources = [source for source in reviewed["sources"] if source["message_id"] in expected]
    attachments = []
    for attachment in reviewed["attachments"]:
        item = deepcopy(attachment)
        item["provenance"] = [p for p in item["provenance"] if p["message_id"] in expected]
        if item["provenance"]:
            attachments.append(item)
    order = {"schema_version": 1, "owner": "event-documentation", "status": "pending_content_review",
             "event": _event(request.get("event"), operative["project"]),
             "sources": sources, "attachments": attachments, "completeness": _completeness(scope)}
    order["source_snapshot_sha256"] = canonical_json_sha256({"sources": sources, "attachments": attachments})
    return {"work_order": order, "work_order_sha256": canonical_json_sha256(order),
            "apply_result_sha256": request["apply_result_sha256"]}


def _safe_output(root, raw, event):
    raw = _text(raw, 500)
    path = PurePosixPath(raw)
    if (path.is_absolute() or path.as_posix() != raw or any(p in {".", ".."} for p in path.parts)
            or "\\" in raw or ":" in raw):
        raise ValueError("Expected canonical workspace-relative output")
    tails = [PurePosixPath("memory") / zone / "projects" / event["owner_id"] / "events" / event["id"]
             for zone in ("references", "evidence")]
    if not any(path.is_relative_to(prefix) and path != prefix for prefix in tails):
        raise ValueError("Output outside exact event data zones")
    target = root
    for part in path.parts:
        target = target / part
        info = target.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Reparse output is not trusted")
    if not target.is_file() or target.stat().st_nlink != 1:
        raise ValueError("Output must be an independent regular file")
    return target


def _report(request, root):
    keys = {"schema_version", "owner", "status", "event", "sources", "attachments", "completeness", "source_snapshot_sha256"}
    order = _shape(request.get("work_order"), keys, keys)
    if (type(order["schema_version"]) is not int or order["schema_version"] != 1
            or order["owner"] != "event-documentation" or order["status"] != "pending_content_review"):
        raise ValueError("Invalid work order")
    event_value = _shape(order["event"], {"owner_kind", "owner_id", "id", "title", "start", "end"}, {"owner_id"})
    event = _event(event_value, event_value["owner_id"])
    if not _SLUG.fullmatch(event["owner_id"]):
        raise ValueError("Invalid event owner ID")
    if order["source_snapshot_sha256"] != canonical_json_sha256({"sources": order["sources"], "attachments": order["attachments"]}):
        raise ValueError("Source snapshot drift")
    _list(order["sources"], 100, nonempty=True)
    scope = _scope(order["completeness"].get("scope") if isinstance(order["completeness"], dict) else None)
    if order["completeness"] != _completeness(scope):
        raise ValueError("Noncanonical completeness provenance")
    source_ids, locations = set(), []
    for source in order["sources"]:
        source_keys = {"id", "kind", "message_id", "content_sha256", "locations", "thread_parents", "calendar_objects"}
        _shape(source, source_keys, source_keys)
        mid = normalize_message_id(_text(source["message_id"]))
        if (mid != source["message_id"] or mid in source_ids or source["kind"] != "mail"
                or source["id"] != "mail-" + canonical_json_sha256(mid)):
            raise ValueError("Invalid or duplicate work-order source identity")
        source_ids.add(mid)
        _hash(source["content_sha256"])
        for location in _list(source["locations"], 100, nonempty=True):
            _shape(location, _LOCATION, _LOCATION)
            if _location(location, scope) != location or location["message_id"] != mid:
                raise ValueError("Work-order locator drift")
            locations.append(location)
        for parent in _list(source["thread_parents"], 10100):
            if normalize_message_id(_text(parent)) != parent:
                raise ValueError("Noncanonical work-order thread identity")
        for calendar in _list(source["calendar_objects"], 10000):
            calendar_keys = {"uid", "sequence", "recurrence_id", "content_sha256"}
            _shape(calendar, calendar_keys, calendar_keys)
            _text(calendar["uid"])
            _hash(calendar["content_sha256"])
            if type(calendar["sequence"]) is not int or calendar["sequence"] < 0:
                raise ValueError("Invalid work-order calendar sequence")
            if calendar["recurrence_id"] is not None:
                _text(calendar["recurrence_id"])
    attachment_ids = set()
    for attachment in _list(order["attachments"], 10000):
        keys = {"id", "sha256", "size_bytes", "provenance"}
        _shape(attachment, keys, keys)
        sha = _hash(attachment["sha256"])
        if attachment["id"] != "attachment-" + sha or sha in attachment_ids:
            raise ValueError("Invalid work-order attachment identity")
        attachment_ids.add(sha)
        if type(attachment["size_bytes"]) is not int or not 0 <= attachment["size_bytes"] <= 1024 ** 3:
            raise ValueError("Invalid work-order attachment size")
        for provenance in _list(attachment["provenance"], 10000, nonempty=True):
            keys = _LOCATION | {"part_locator", "filename", "original_filename", "mime_type"}
            _shape(provenance, keys, keys)
            if _location(provenance, scope) not in locations:
                raise ValueError("Unbound work-order attachment")
            for field in ("part_locator", "filename", "original_filename", "mime_type"):
                _text(provenance[field])
    if not isinstance(order["completeness"], dict) or order["completeness"].get("exhaustive") is not False:
        raise ValueError("Report must preserve limited completeness")
    receipt = _shape(request.get("output_receipt"), {"schema_version", "work_order_sha256", "source_snapshot_sha256", "outputs"},
                     {"schema_version", "work_order_sha256", "source_snapshot_sha256", "outputs"})
    if (type(receipt["schema_version"]) is not int or receipt["schema_version"] != 1
            or receipt["work_order_sha256"] != canonical_json_sha256(order)
            or receipt["source_snapshot_sha256"] != order["source_snapshot_sha256"]):
        raise ValueError("Output receipt is not bound to the exact work order")
    artifacts = [_text(v) for v in _list(request.get("artifacts"), 100, nonempty=True)]
    outputs = _list(receipt["outputs"], 100, nonempty=True)
    for output in outputs:
        _shape(output, {"path", "sha256"}, {"path", "sha256"})
        _text(output["path"])
        _hash(output["sha256"])
    paths = [output["path"] for output in outputs]
    if len(set(paths)) != len(paths) or len(set(artifacts)) != len(artifacts) or set(paths) != set(artifacts):
        raise ValueError("Report must verify exactly every receipted output")
    index = f"memory/references/projects/{event['owner_id']}/events/{event['id']}/index.md"
    if index not in paths:
        raise ValueError("Event index is required")
    for output in outputs:
        target = _safe_output(root, output["path"], event)
        digest = hashlib.sha256()
        with target.open("rb") as stream:
            for chunk in iter(lambda: stream.read(65536), b""):
                digest.update(chunk)
        if digest.hexdigest() != output["sha256"]:
            raise ValueError("Output content drift")
    return {"verified_outputs": artifacts, "content_approved": False,
            "content_review": "human_content_review_required", "work_order_sha256": canonical_json_sha256(order)}


def process_request(request: object, *, workspace_root: str | Path) -> dict:
    """Pure composition plus report-time file verification; never execute writes."""
    try:
        # Copy through strict JSON to reject non-JSON values, duplicate keys in
        # CLI input, non-finite numbers and oversized direct API payloads alike.
        raw = json.dumps(request, ensure_ascii=False, allow_nan=False)
        if len(raw.encode("utf-8")) > MAX_REQUEST_BYTES:
            raise ValueError("Request exceeds size bound")
        request = strict_json(raw)
        if not isinstance(request, dict) or type(request.get("schema_version")) is not int or request["schema_version"] != 1:
            raise ValueError("Unsupported schema")
        stage = request.get("stage")
        if not isinstance(stage, str) or stage not in _STAGES:
            raise ValueError("Unknown stage")
        _shape(request, _STAGES[stage] | {"schema_version", "stage"})
        root = Path(workspace_root).resolve()
        if stage == "report":
            result = _report(request, root)
        else:
            scope = _scope(request.get("scope"))
            result = {"completeness": _completeness(scope), "account_binding": "pending_workspace_revalidation"}
            if stage == "plan":
                inspect = [_text(v) for v in _list(request.get("inspect_ids", []), scope["inspect_limit"])]
                if len(set(inspect)) != len(inspect):
                    raise ValueError("Duplicate inspect IDs")
                if inspect and len(scope["folders"]) != 1:
                    raise ValueError("Inspect IDs require exactly one explicit folder")
                result["search_requests"] = [{"mode": "search", "account": scope["account"],
                    "folder": folder, "folders": [folder], "query": term,
                    "page_size": scope["scan_limit_per_folder"]} for folder in scope["folders"] for term in scope["terms"]]
                result["inspect_requests"] = [{"op": "read", "account": scope["account"],
                    "folder": scope["folders"][0], "envelope_id": eid} for eid in inspect]
            elif stage in {"review", "apply"}:
                reviewed = _review(request, scope, approved=stage == "apply")
                result.update(reviewed)
                if stage == "apply":
                    if reviewed["review_required"]:
                        raise ValueError("Unresolved semantic review; do not apply")
                    result.update({"dispatch_required": True, "applied": False,
                                   "batch_request": deepcopy(request["operative_request"])})
            else:
                result.update(_handoff(request, scope))
        return {"ok": True, "schema_version": 1, "stage": stage, **result}
    except (ValueError, TypeError, KeyError, OSError, RecursionError, OverflowError):
        # Never echo external content, paths, exceptions or partial work orders.
        return {"ok": False, "error": {"code": "invalid_event_dossier_request",
                "message": "Request or bound evidence failed validation; no action was performed."}}
