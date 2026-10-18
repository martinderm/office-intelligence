#!/usr/bin/env python3
"""Source-bound event writer for the Dual-Evidence event folder (#14).

``apply_event`` validates a catalog-bound work order and its caller-provided
content, then writes exactly five deterministic artifacts: the normative event
index under ``memory/references/`` and the empirical evidence set (action items,
source log, attachment inventory, compliance report) under ``memory/evidence/``.

The writer is stdlib-only and never imports the mail-desk bundle, so the seven
historical ``apply_event`` pins can load it standalone.  It re-derives the
canonical JSON SHA-256 and the message-id normalization of the mail-desk
contract and performs no content approval, cloud or task synchronization.

Safety: every catalog, lock and target path is validated *before* any mkdir or
temp write.  Publication stages into a fresh exclusive ``tempfile.mkstemp`` in a
re-verified parent directory and links it into place with a no-clobber
``os.link`` (never ``os.replace`` of an existing target).  The unresolved parent
chain and the owned lease are re-checked before every mkdir, write, link,
verification and cleanup.  Rollback removes only files and directories this
invocation created, only when their recorded identity (device/inode) and bytes
still match, and only under a still-owned lease.  This closes practical check
gaps; it does not claim race-freedom against an adversarial concurrent writer.
"""
from __future__ import annotations

import argparse
from datetime import date
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import stat
import sys
import tempfile

__all__ = ["apply_event", "canonical_json_sha256", "normalize_message_id", "strict_json", "main"]

MAX_REQUEST_BYTES = 2 * 1024 * 1024
DRAFT_MARKER = "MAILBASED UNGEPRUEFTER ENTWURF"
CONTENT_REVIEW = "human_content_review_required"
ACTION = "event_documentation"
ENV_LOCK_ROOT = "EVENT_DOCUMENTATION_WORKSPACE_LOCK_ROOT"

_REJECTED_MESSAGE = ("Event work order, content, catalog, lock or target paths failed validation; "
                     "no event was written.")
_RECOVERY_MESSAGE = ("Event publication failed after it began; the event folder may contain partial, "
                     "staged or externally changed files and requires recovery review.")

_SHA = re.compile(r"[0-9a-f]{64}\Z")
_STRICT_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_PROJECT_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,99}\Z")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)
_STAGE_SUFFIX = ".eventwriter-stage"

_BASIS_SOURCE_KEYS = frozenset({"id", "kind", "message_id", "content_sha256"})
_RICH_SOURCE_KEYS = _BASIS_SOURCE_KEYS | {"locations", "thread_parents", "calendar_objects"}
_LOCATION_KEYS = frozenset({"account", "folder", "envelope_id", "message_id"})
_RICH_ATTACHMENT_KEYS = frozenset({"id", "sha256", "size_bytes", "provenance"})
_PROVENANCE_KEYS = _LOCATION_KEYS | {"part_locator", "filename", "original_filename", "mime_type"}
_CALENDAR_KEYS = frozenset({"uid", "sequence", "recurrence_id", "content_sha256"})
_CONTENT_KEYS = frozenset({"overview", "planned_agenda", "observed_decisions", "reply_triage", "todo_triage"})
_WORK_ORDER_KEYS = frozenset({"schema_version", "owner", "status", "event", "sources", "attachments",
                              "completeness", "source_snapshot_sha256"})
_PAYLOAD_KEYS = frozenset({"schema_version", "work_order", "content"})
_COMPLETENESS_LIMITATIONS = (
    "Only explicit folders, terms and bounded first-page envelopes are covered.",
    "Caller-provided snapshots require live workspace/account revalidation.",
)
_MD_ESCAPE_CHARS = "\\`*_{}[]()#!|~"


class _Reject(Exception):
    """Fail-closed validation rejection; carries no external content."""


class _WriteFailure(Exception):
    """Publication failed after it began; ``recovery_required`` marks partial state."""

    def __init__(self, recovery_required: bool):
        super().__init__("write failure")
        self.recovery_required = bool(recovery_required)


# ---------------------------------------------------------------------------
# Canonical helpers (mirror the mail-desk contract, no import coupling)
# ---------------------------------------------------------------------------


def canonical_json_sha256(value: object) -> str:
    """Return the stable canonical JSON SHA-256 used by the mail-desk contract."""
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def normalize_message_id(value: str) -> str:
    """Strip surrounding angle brackets/whitespace and lowercase."""
    text = value.strip()
    while text.startswith("<") and text.endswith(">") and len(text) >= 2:
        text = text[1:-1].strip()
    return text.lower()


def strict_json(raw: str) -> object:
    """Parse JSON rejecting duplicate keys and non-finite numbers."""

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


# ---------------------------------------------------------------------------
# Bounded shape validation
# ---------------------------------------------------------------------------


def _shape(value, allowed, required=None):
    if not isinstance(value, dict) or set(value) - allowed or (required or frozenset()) - set(value):
        raise _Reject()
    return value


def _text(value, limit=500):
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > limit or any(ord(c) < 32 for c in value)):
        raise _Reject()
    return value


def _hash(value):
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise _Reject()
    return value


def _integer(value, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        raise _Reject()
    return value


def _list(value, maximum, *, nonempty=False):
    if not isinstance(value, list) or len(value) > maximum or (nonempty and not value):
        raise _Reject()
    return value


def _text_list(value, maximum):
    return [_text(item, 2000) for item in _list(value, maximum, nonempty=True)]


def _iso_date(value):
    if not isinstance(value, str) or not _ISO_DATE.fullmatch(value):
        raise _Reject()
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise _Reject() from None
    if parsed.isoformat() != value:
        raise _Reject()
    return value


# ---------------------------------------------------------------------------
# Work-order validation
# ---------------------------------------------------------------------------


def _event(value):
    if not isinstance(value, dict):
        raise _Reject()
    owner_kind = value.get("owner_kind")
    if owner_kind == "project":
        allowed = {"owner_kind", "owner_id", "id", "title", "start", "end"}
        required = {"owner_kind", "owner_id", "id", "title", "start"}
    elif owner_kind == "topic":
        allowed = {"owner_kind", "owner_id", "id", "title", "start", "end", "subtopic_id"}
        required = {"owner_kind", "owner_id", "id", "title", "start", "subtopic_id"}
    else:
        raise _Reject()
    _shape(value, allowed, required)
    owner_id = value["owner_id"]
    event_id = value["id"]
    if not isinstance(owner_id, str) or not isinstance(event_id, str):
        raise _Reject()
    if owner_kind == "project":
        # The facade supports the legacy [a-z0-9][a-z0-9_-]{0,99} project grammar.
        if not _PROJECT_ID.fullmatch(owner_id) or not _PROJECT_ID.fullmatch(event_id):
            raise _Reject()
    else:
        # Topic, subtopic and event slugs use the stricter topic grammar.
        if not _STRICT_SLUG.fullmatch(owner_id):
            raise _Reject()
        if not isinstance(value["subtopic_id"], str) or not _STRICT_SLUG.fullmatch(value["subtopic_id"]):
            raise _Reject()
        if not _STRICT_SLUG.fullmatch(event_id):
            raise _Reject()
    _text(value["title"])
    start = _iso_date(value["start"])
    end = None
    if "end" in value:
        end = _iso_date(value["end"])
        if end < start:
            raise _Reject()
    return {"owner_kind": owner_kind, "owner_id": owner_id, "id": event_id, "title": value["title"],
            "start": start, "end": end, "subtopic_id": value.get("subtopic_id")}


def _source_profile(raw):
    if not isinstance(raw, list) or not raw or len(raw) > 100:
        raise _Reject()
    if all(isinstance(source, dict) and set(source) == _BASIS_SOURCE_KEYS for source in raw):
        return "basis"
    if all(isinstance(source, dict) and set(source) == _RICH_SOURCE_KEYS for source in raw):
        return "rich"
    raise _Reject()


def _sources(raw, scope):
    profile = "basis" if all(isinstance(source, dict) and set(source) == _BASIS_SOURCE_KEYS
                             for source in raw) else "rich"
    seen_ids, seen_mids, bound = set(), set(), []
    for source in raw:
        sid = _text(source["id"], 200)
        if sid in seen_ids:
            raise _Reject()
        seen_ids.add(sid)
        if source["kind"] != "mail":
            raise _Reject()
        mid = _text(source["message_id"], 500)
        if normalize_message_id(mid) != mid:
            raise _Reject()
        if mid in seen_mids:
            raise _Reject()
        seen_mids.add(mid)
        _hash(source["content_sha256"])
        if profile == "rich":
            if source["id"] != "mail-" + canonical_json_sha256(mid):
                raise _Reject()
            for location in _list(source["locations"], 100, nonempty=True):
                _shape(location, _LOCATION_KEYS, _LOCATION_KEYS)
                for key in _LOCATION_KEYS:
                    _text(location[key])
                if location["account"] != scope["account"] or location["folder"] not in scope["folders"]:
                    raise _Reject()
                if location["message_id"] != mid:
                    raise _Reject()
                bound.append(location)
            for parent in _list(source["thread_parents"], 10100):
                if not isinstance(parent, str) or not parent or normalize_message_id(parent) != parent:
                    raise _Reject()
            for calendar in _list(source["calendar_objects"], 10000):
                _shape(calendar, _CALENDAR_KEYS, _CALENDAR_KEYS)
                _text(calendar["uid"])
                if type(calendar["sequence"]) is not int or calendar["sequence"] < 0:
                    raise _Reject()
                if calendar["recurrence_id"] is not None:
                    _text(calendar["recurrence_id"])
                _hash(calendar["content_sha256"])
    return list(raw), bound


def _attachments(raw, profile, bound):
    if not isinstance(raw, list):
        raise _Reject()
    if profile == "basis":
        if raw:
            raise _Reject()
        return []
    if len(raw) > 10000:
        raise _Reject()
    bound_keys = {tuple(location[key] for key in ("account", "folder", "envelope_id", "message_id"))
                  for location in bound}
    seen_shas, part_map, blob_size = set(), {}, {}
    for attachment in raw:
        _shape(attachment, _RICH_ATTACHMENT_KEYS, _RICH_ATTACHMENT_KEYS)
        sha = _hash(attachment["sha256"])
        if attachment["id"] != "attachment-" + sha or sha in seen_shas:
            raise _Reject()
        seen_shas.add(sha)
        size = attachment["size_bytes"]
        if type(size) is not int or not 0 <= size <= 1024 ** 3:
            raise _Reject()
        if sha in blob_size and blob_size[sha] != size:
            raise _Reject()
        blob_size[sha] = size
        for provenance in _list(attachment["provenance"], 10000, nonempty=True):
            _shape(provenance, _PROVENANCE_KEYS, _PROVENANCE_KEYS)
            for key in _LOCATION_KEYS:
                _text(provenance[key])
            for key in ("part_locator", "filename", "original_filename", "mime_type"):
                _text(provenance[key])
            for field in ("filename", "original_filename"):
                name = provenance[field]
                if name in {".", ".."} or any(character in name for character in "/\\:"):
                    raise _Reject()
            location_key = tuple(provenance[key] for key in ("account", "folder", "envelope_id", "message_id"))
            if location_key not in bound_keys:
                raise _Reject()
            part_key = location_key + (provenance["part_locator"],)
            fingerprint = (sha, size, provenance["filename"], provenance["original_filename"],
                           provenance["mime_type"])
            if part_key in part_map and part_map[part_key] != fingerprint:
                raise _Reject()
            part_map[part_key] = fingerprint
    return list(raw)


def _scope(value):
    keys = frozenset({"account", "folders", "terms", "scan_limit_per_folder", "inspect_limit"})
    _shape(value, keys, keys)
    _text(value["account"], 100)
    for key, maximum in (("folders", 20), ("terms", 20)):
        values = [_text(item, 200) for item in _list(value[key], maximum, nonempty=True)]
        if len(set(values)) != len(values):
            raise _Reject()
    _integer(value["scan_limit_per_folder"], 500)
    _integer(value["inspect_limit"], 100)
    if value["inspect_limit"] > len(value["folders"]) * value["scan_limit_per_folder"]:
        raise _Reject()
    return value


def _canonical_completeness(scope):
    return {"exhaustive": False, "scan_basis": "first_page_envelopes", "scope": scope,
            "limitations": list(_COMPLETENESS_LIMITATIONS)}


def _completeness(value, profile):
    if not isinstance(value, dict):
        raise _Reject()
    if profile == "basis":
        _shape(value, {"exhaustive", "limitations"}, {"exhaustive", "limitations"})
        if value["exhaustive"] is not False:
            raise _Reject()
        _text_list(value["limitations"], 50)
        return None
    keys = frozenset({"exhaustive", "scan_basis", "scope", "limitations"})
    _shape(value, keys, keys)
    scope = _scope(value["scope"])
    if value != _canonical_completeness(scope):
        raise _Reject()
    return scope


def _validate_work_order(order):
    _shape(order, _WORK_ORDER_KEYS, _WORK_ORDER_KEYS)
    if type(order["schema_version"]) is not int or order["schema_version"] != 1:
        raise _Reject()
    if order["owner"] != "event-documentation" or order["status"] != "pending_content_review":
        raise _Reject()
    event = _event(order["event"])
    profile = _source_profile(order["sources"])
    scope = _completeness(order["completeness"], profile)
    sources, bound = _sources(order["sources"], scope)
    attachments = _attachments(order["attachments"], profile, bound)
    if order["source_snapshot_sha256"] != canonical_json_sha256(
            {"sources": order["sources"], "attachments": order["attachments"]}):
        raise _Reject()
    return event, sources, attachments, profile


def _content(value, known_ids):
    _shape(value, _CONTENT_KEYS, _CONTENT_KEYS)
    for key in ("overview", "planned_agenda", "observed_decisions"):
        for item in _list(value[key], 200):
            _shape(item, {"text", "source_ids"}, {"text", "source_ids"})
            _anchor(item, known_ids)
    for key in ("reply_triage", "todo_triage"):
        for item in _list(value[key], 200):
            _shape(item, {"text", "source_ids", "needed"}, {"text", "source_ids", "needed"})
            _anchor(item, known_ids)
            if not isinstance(item["needed"], bool):
                raise _Reject()
    return value


def _anchor(item, known_ids):
    _text(item["text"], 2000)
    ids = _list(item["source_ids"], 100, nonempty=True)
    for sid in ids:
        if not isinstance(sid, str) or sid not in known_ids:
            raise _Reject()
    if len(set(ids)) != len(ids):
        raise _Reject()


# ---------------------------------------------------------------------------
# Catalog-bound owner resolution
# ---------------------------------------------------------------------------


def _open_read(path):
    return os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))


def _read_inode(path, device, inode):
    """Read a file only if its opened identity still matches (device, inode)."""
    try:
        descriptor = _open_read(path)
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_dev != device or info.st_ino != inode:
            return None
        chunks = []
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(descriptor)


def _read_catalog(root, relative):
    parts = _reject_relative(relative).parts
    _assert_parents(root, parts[:-1])
    target = root.joinpath(*parts)
    info = _lstat(target)
    if info is None or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise _Reject()
    if getattr(info, "st_file_attributes", 0) & 0x400:
        raise _Reject()
    try:
        descriptor = _open_read(target)
    except OSError:
        raise _Reject() from None
    try:
        opened = os.fstat(descriptor)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or opened.st_dev != info.st_dev or opened.st_ino != info.st_ino):
            raise _Reject()
        chunks, total = [], 0
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_REQUEST_BYTES:
                raise _Reject()
            chunks.append(chunk)
    finally:
        os.close(descriptor)
    try:
        text = b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError:
        raise _Reject() from None
    try:
        return strict_json(text)
    except (ValueError, TypeError):
        raise _Reject() from None


def _catalog_entries(value, key):
    if isinstance(value, dict):
        entries = value.get(key)
    elif isinstance(value, list):
        entries = value
    else:
        raise _Reject()
    if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
        raise _Reject()
    return entries


def _owner_identity(entry):
    _text(entry.get("id"), 200)
    _text(entry.get("title"), 500)
    _text(entry.get("mailbox_folder"), 500)


def _route_state(entry):
    status = entry.get("status")
    if status is None:
        return
    if not isinstance(status, str) or status.casefold() not in {"", "active", "inactive"}:
        raise _Reject()
    if status.casefold() == "inactive":
        raise _Reject()


def _registered_event(topic, subtopic, event, registered):
    if registered.get("title") != event["title"]:
        raise _Reject()
    if _iso_date(registered.get("starts_on")) != event["start"]:
        raise _Reject()
    ends_on = registered.get("ends_on")
    if event["end"] is None:
        if ends_on is not None:
            raise _Reject()
    elif ends_on != event["end"]:
        raise _Reject()
    expected = (f"memory/references/topics/{topic['id']}/subtopics/{subtopic['id']}"
                f"/events/{event['id']}/index.md")
    reference = registered.get("reference_md")
    if not isinstance(reference, str) or reference != expected or "\\" in reference:
        raise _Reject()
    if str(registered.get("status", "")).casefold() not in {"", "active"}:
        raise _Reject()
    phase = registered.get("phase")
    if phase is not None and str(phase).casefold() not in {"planned", "live", "completed", "cancelled"}:
        raise _Reject()
    if "cloud_storage" in registered:
        storage = registered["cloud_storage"]
        if not isinstance(storage, dict) or set(storage) != {"scope", "storage_id"}:
            raise _Reject()
        scope, storage_id = storage.get("scope"), storage.get("storage_id")
        if scope == "topic":
            source = topic.get("cloud_sync")
        elif scope == "subtopic":
            source = subtopic.get("cloud_sync")
        else:
            raise _Reject()
        if not isinstance(storage_id, str) or not storage_id.strip() or not isinstance(source, dict) \
                or not isinstance(source.get(storage_id), dict):
            raise _Reject()


def _resolve_owner(root, event):
    if event["owner_kind"] == "project":
        entries = _catalog_entries(_read_catalog(root, "memory/references/projects/projects.json"), "projects")
        matches = [entry for entry in entries if entry.get("id") == event["owner_id"]]
        if len(matches) != 1:
            raise _Reject()
        _owner_identity(matches[0])
        return
    entries = _catalog_entries(_read_catalog(root, "memory/references/topics/topics.json"), "topics")
    topic_matches = [entry for entry in entries if entry.get("id") == event["owner_id"]]
    if len(topic_matches) != 1:
        raise _Reject()
    topic = topic_matches[0]
    _owner_identity(topic)
    _route_state(topic)
    subtopics = topic.get("subtopics")
    if not isinstance(subtopics, list) or any(not isinstance(subtopic, dict) for subtopic in subtopics):
        raise _Reject()
    subtopic_matches = [subtopic for subtopic in subtopics if subtopic.get("id") == event["subtopic_id"]]
    if len(subtopic_matches) != 1:
        raise _Reject()
    subtopic = subtopic_matches[0]
    _text(subtopic.get("id"), 200)
    _text(subtopic.get("title"), 500)
    _route_state(subtopic)
    collisions = []
    for candidate in subtopics:
        events = candidate.get("events", [])
        if events is None:
            events = []
        if not isinstance(events, list) or any(not isinstance(entry, dict) for entry in events):
            raise _Reject()
        identifiers = []
        for registered in events:
            identifier = registered.get("id")
            if not isinstance(identifier, str) or not _STRICT_SLUG.fullmatch(identifier):
                raise _Reject()
            identifiers.append(identifier)
            if identifier == event["id"]:
                collisions.append((candidate, registered))
        if len(set(identifiers)) != len(identifiers):
            raise _Reject()
    if len(collisions) != 1 or collisions[0][0] is not subtopic:
        raise _Reject()
    _registered_event(topic, subtopic, event, collisions[0][1])


# ---------------------------------------------------------------------------
# Trusted workspace-lock guard
# ---------------------------------------------------------------------------


class _Guard:
    def __init__(self, module, workspace_root, lease_id, conversation_id):
        self.module = module
        self.workspace_root = workspace_root
        self.lease_id = lease_id
        self.conversation_id = conversation_id


def _trusted_lock_root(explicit):
    if explicit is not None:
        return Path(explicit)
    env = os.environ.get(ENV_LOCK_ROOT, "").strip()
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[3].parent / "workspace-lock"


def _load_guard(trusted_root):
    guard_path = Path(trusted_root) / "scripts" / "workspace_lock_guard.py"
    if not guard_path.is_file():
        raise _Reject()
    spec = importlib.util.spec_from_file_location("_event_documentation_workspace_lock_guard", str(guard_path))
    if spec is None or spec.loader is None:
        raise _Reject()
    module = importlib.util.module_from_spec(spec)
    name = spec.name
    # ``@dataclass`` resolves string annotations through ``sys.modules`` during
    # class creation, so the freshly-executed trusted module must be visible for
    # the duration of exec.  A pre-existing entry is never read; it is only
    # saved and restored, so no foreign cached module is trusted.
    previous = sys.modules.get(name)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        raise _Reject() from None
    finally:
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous
    if not callable(getattr(module, "require_workspace_lock", None)):
        raise _Reject()
    return module


def _require_lease(guard):
    try:
        guard.module.require_workspace_lock(guard.workspace_root, lease_id=guard.lease_id,
                                            conversation_id=guard.conversation_id, allow_legacy=False)
    except Exception:
        raise _Reject() from None


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


def _reject_relative(relative):
    if not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative:
        raise _Reject()
    path = PurePosixPath(relative)
    if path.is_absolute() or path.as_posix() != relative:
        raise _Reject()
    for part in path.parts:
        if part in {"", ".", ".."} or part != part.strip() or part.endswith("."):
            raise _Reject()
        if part.split(".")[0].casefold() in _WINDOWS_RESERVED:
            raise _Reject()
    return path


def _lstat(path):
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        raise _Reject() from None


def _check_root(root):
    """Require the caller-designated root to resolve to a directory.

    The root itself may be a junction/symlink (the caller's trust anchor); the
    root alias is never used to resolve away child reparse points.
    """
    try:
        info = os.stat(root)
    except OSError:
        raise _Reject() from None
    if not stat.S_ISDIR(info.st_mode):
        raise _Reject()


def _assert_parents(root, parts):
    _check_root(root)
    current = root
    for part in parts:
        current = current / part
        info = _lstat(current)
        if (info is None or stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
                or (getattr(info, "st_file_attributes", 0) & 0x400)):
            raise _Reject()


def _walk(root, parts):
    current = root
    for index, part in enumerate(parts):
        current = current / part
        info = _lstat(current)
        if info is None:
            return current, False
        if stat.S_ISLNK(info.st_mode) or (getattr(info, "st_file_attributes", 0) & 0x400):
            raise _Reject()
        if index != len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise _Reject()
    return current, True


def _paths(event):
    if event["owner_kind"] == "project":
        reference_dir = f"memory/references/projects/{event['owner_id']}/events/{event['id']}"
        evidence_dir = f"memory/evidence/projects/{event['owner_id']}/events/{event['id']}"
    else:
        reference_dir = (f"memory/references/topics/{event['owner_id']}/subtopics/"
                         f"{event['subtopic_id']}/events/{event['id']}")
        evidence_dir = f"memory/evidence/topics/{event['owner_id']}/events/{event['id']}"
    return [f"{reference_dir}/index.md", f"{evidence_dir}/action-items.md", f"{evidence_dir}/source-log.json",
            f"{evidence_dir}/attachment-inventory.json", f"{evidence_dir}/compliance-report.json"]


# ---------------------------------------------------------------------------
# Deterministic, inert artifact rendering
# ---------------------------------------------------------------------------


def _yaml(text):
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    for separator in ("\u2028", "\u2029", "\u0085"):
        text = text.replace(separator, " ")
    return text


def _md(text):
    """Render untrusted text inert for Markdown and HTML."""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    for character in _MD_ESCAPE_CHARS:
        text = text.replace(character, "\\" + character)
    for separator in ("\u2028", "\u2029", "\u0085"):
        text = text.replace(separator, " ")
    return text


def _bullets(items):
    if not items:
        return ["- (keine)"]
    return [f"- {_md(item['text'])} — Quellen: {', '.join(_md(source_id) for source_id in item['source_ids'])}"
            for item in items]


def _triage(items):
    if not items:
        return ["- (keine)"]
    return [f"- Bedarf: {'ja' if item['needed'] else 'nein'} — {_md(item['text'])} — "
            f"Quellen: {', '.join(_md(source_id) for source_id in item['source_ids'])}" for item in items]


def _render_index(event, content, sources, completeness, evidence_link):
    owner = f"{event['owner_kind']}/{event['owner_id']}"
    if event["subtopic_id"]:
        owner = f"{owner}/{event['subtopic_id']}"
    lines = [
        "---", "document_type: event-spec", "evidence_level: unreviewed-draft",
        "review_state: unreviewed", "status: draft", "source_bound: true",
        f'draft_notice: "{DRAFT_MARKER}"', f'title: "{_yaml(event["title"])}"',
        f'starts_on: "{event["start"]}"',
    ]
    if event["end"]:
        lines.append(f'ends_on: "{event["end"]}"')
    lines += [f'owner_kind: "{event["owner_kind"]}"', f'owner_id: "{event["owner_id"]}"']
    if event["subtopic_id"]:
        lines.append(f'subtopic_id: "{event["subtopic_id"]}"')
    lines += [
        f'event_id: "{event["id"]}"', "---", "", f"# {_md(event['title'])}", "",
        f"> **{DRAFT_MARKER}** — Dieser Entwurf ist ausschließlich aus gebundenen Mail-Quellen abgeleitet.",
        "> Keine amtlichen Fakten, keine Inhaltsfreigabe und kein Echtheitsbeweis.",
        "> Vor jeder Veröffentlichung menschlich prüfen.", "", "## Eckdaten",
        f"- **Titel:** {_md(event['title'])}", f"- **Start:** {event['start']}",
        f"- **Ende:** {event['end'] or 'offen'}", f"- **Owner:** {owner}",
        f"- **Event-ID:** {event['id']}", "", "## Überblick",
    ]
    lines += _bullets(content["overview"])
    lines += ["", "## Geplante Agenda"]
    lines += _bullets(content["planned_agenda"])
    lines += [
        "", "## Beobachtete Entscheidungen",
        f"- REPORTED, UNREVIEWED evidence is recorded in the evidence pillar: [action-items.md]({evidence_link})",
        "- Diese Angaben sind quellengebundene, ungeprüfte Meldungen und keine bestätigten Entscheidungen.",
        "", "## Quellen",
    ]
    for source in sources:
        # Backslash escapes are ignored inside CommonMark code spans. Keep
        # untrusted identifiers in ordinary escaped text; only the validated
        # fixed-grammar digest is placed in a code span.
        lines.append(f"- {_md(source['id'])} ({_md(source['kind'])}) message_id "
                     f"{_md(source['message_id'])} content_sha256 `{source['content_sha256']}`")
    lines += ["", "## Unsicherheit und Vollständigkeit",
              f"- Vollständig: nein (exhaustive={str(completeness['exhaustive']).lower()})"]
    for limitation in completeness["limitations"]:
        lines.append(f"- {_md(limitation)}")
    lines.append("")
    return "\n".join(lines)


def _render_action_items(event, content):
    lines = [
        "---", "document_type: action-items", "evidence_level: observed",
        "review_state: unreviewed", "status: draft", f'draft_notice: "{DRAFT_MARKER}"',
        f'event_id: "{event["id"]}"', "---", "", f"# Action Items — {_md(event['title'])}", "",
        f"> **{DRAFT_MARKER}** — ausschließlich aus gebundenen Mail-Quellen abgeleitet; keine Freigabe.",
        "", "## Beobachtete Entscheidungen (REPORTED, UNREVIEWED)",
    ]
    lines += _bullets(content["observed_decisions"])
    lines += ["", "## Reply-Triage"]
    lines += _triage(content["reply_triage"])
    lines += ["", "## Todo-Triage"]
    lines += _triage(content["todo_triage"])
    lines.append("")
    return "\n".join(lines)


def _json_doc(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"


def _build_outputs(event, content, sources, attachments, order, work_order_sha256):
    relative = _paths(event)
    evidence_link = posixpath.relpath(relative[1], start=posixpath.dirname(relative[0]))
    identity = {key: event[key] for key in ("owner_kind", "owner_id", "id", "subtopic_id")}
    source_log = _json_doc({"schema_version": 1, "draft_notice": DRAFT_MARKER, "source_bound": True,
                            "event": identity, "sources": sources, "completeness": order["completeness"]})
    inventory = _json_doc({"schema_version": 1, "draft_notice": DRAFT_MARKER, "attachments": attachments})
    compliance = _json_doc({"schema_version": 1, "draft_notice": DRAFT_MARKER, "content_approved": False,
                            "content_review": CONTENT_REVIEW, "work_order_sha256": work_order_sha256,
                            "source_snapshot_sha256": order["source_snapshot_sha256"],
                            "output_count": len(relative), "completeness": order["completeness"],
                            "source_ids": [source["id"] for source in sources]})
    documents = [_render_index(event, content, sources, order["completeness"], evidence_link),
                 _render_action_items(event, content), source_log, inventory, compliance]
    return [(relative[index], document.encode("utf-8")) for index, document in enumerate(documents)]


# ---------------------------------------------------------------------------
# Preflight and staged, no-clobber publication
# ---------------------------------------------------------------------------


def _preflight(root, outputs_bytes):
    _check_root(root)
    existing = 0
    for relative, expected in outputs_bytes:
        parts = _reject_relative(relative).parts
        target, present = _walk(root, parts)
        if present:
            info = _lstat(target)
            if info is None or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise _Reject()
            if target.read_bytes() != expected:
                raise _Reject()
            existing += 1
    directories = []
    for relative, _expected in outputs_bytes:
        parent = str(PurePosixPath(relative).parent)
        if parent not in directories:
            directories.append(parent)
    for relative_dir in directories:
        path, present = _walk(root, _reject_relative(relative_dir).parts)
        if not present:
            continue
        with os.scandir(path) as iterator:
            entries = list(iterator)
        if not entries:
            raise _Reject()
        for entry in entries:
            if entry.name.endswith(_STAGE_SUFFIX):
                raise _Reject()
    if existing == len(outputs_bytes):
        return "idempotent"
    if existing:
        raise _Reject()
    return "write"


def _ensure_dirs(root, directories, created, guard):
    _check_root(root)
    for relative_dir in directories:
        current = root
        for part in _reject_relative(relative_dir).parts:
            current = current / part
            info = _lstat(current)
            if info is None:
                _assert_parents(root, current.parent.relative_to(root).parts)
                _require_lease(guard)
                os.mkdir(current)
                made = _lstat(current)
                if made is None or not stat.S_ISDIR(made.st_mode):
                    raise _Reject()
                created.append((current, made.st_dev, made.st_ino))
                continue
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) \
                    or (getattr(info, "st_file_attributes", 0) & 0x400):
                raise _Reject()


def _rollback(root, created, published, stages, guard):
    """Remove only owned, unchanged artifacts under a still-owned lease."""
    recovery = False

    def lease_ok():
        try:
            _require_lease(guard)
            return True
        except _Reject:
            return False

    def safe_parents(path):
        try:
            _assert_parents(root, path.parent.relative_to(root).parts)
            return True
        except (ValueError, _Reject):
            return False

    if not lease_ok():
        return True
    for target, device, inode, data in reversed(published):
        if not lease_ok():
            return True
        if not safe_parents(target):
            recovery = True
            continue
        try:
            info = _lstat(target)
        except _Reject:
            recovery = True
            continue
        if info is None:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_dev != device or info.st_ino != inode:
            recovery = True
            continue
        if _read_inode(target, device, inode) != data:
            recovery = True
            continue
        if not lease_ok():
            return True
        if not safe_parents(target):
            recovery = True
            continue
        try:
            os.unlink(target)
        except OSError:
            recovery = True
    for stage, device, inode, data in list(stages):
        if not lease_ok():
            return True
        if not safe_parents(stage):
            recovery = True
            continue
        try:
            info = _lstat(stage)
        except _Reject:
            recovery = True
            continue
        if info is None:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_dev != device or info.st_ino != inode:
            recovery = True
            continue
        if _read_inode(stage, device, inode) != data:
            recovery = True
            continue
        if not lease_ok():
            return True
        if not safe_parents(stage):
            recovery = True
            continue
        try:
            os.unlink(stage)
        except OSError:
            recovery = True
    for directory, device, inode in reversed(created):
        if not lease_ok():
            return True
        if not safe_parents(directory):
            recovery = True
            continue
        try:
            info = _lstat(directory)
        except _Reject:
            recovery = True
            continue
        if info is None:
            continue
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
                or (getattr(info, "st_file_attributes", 0) & 0x400)
                or info.st_dev != device or info.st_ino != inode):
            recovery = True
            continue
        try:
            with os.scandir(directory) as iterator:
                nonempty = next(iterator, None) is not None
        except OSError:
            recovery = True
            continue
        if nonempty:
            recovery = True
            continue
        if not lease_ok():
            return True
        if not safe_parents(directory):
            recovery = True
            continue
        try:
            os.rmdir(directory)
        except OSError:
            recovery = True
    return recovery


def _verify_published(root, outputs_bytes, published, guard):
    by_path = {str(path): (device, inode, data) for path, device, inode, data in published}
    for relative, data in outputs_bytes:
        parts = _reject_relative(relative).parts
        _assert_parents(root, parts[:-1])
        _require_lease(guard)
        target = root.joinpath(*parts)
        info = _lstat(target)
        if info is None or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise _Reject()
        record = by_path.get(str(target))
        if record is None or record[0] != info.st_dev or record[1] != info.st_ino:
            raise _Reject()
        if _read_inode(target, info.st_dev, info.st_ino) != data:
            raise _Reject()


def _publish(root, outputs_bytes, guard):
    directories = []
    for relative, _expected in outputs_bytes:
        parent = str(PurePosixPath(relative).parent)
        if parent not in directories:
            directories.append(parent)
    created, stages, published = [], [], []
    try:
        _ensure_dirs(root, directories, created, guard)
        for relative, data in outputs_bytes:
            parts = _reject_relative(relative).parts
            target = root.joinpath(*parts)
            _assert_parents(root, parts[:-1])
            _require_lease(guard)
            descriptor, stage_name = tempfile.mkstemp(prefix=".eventwriter-", suffix=_STAGE_SUFFIX,
                                                      dir=str(target.parent))
            stage = Path(stage_name)
            created_info = os.fstat(descriptor)
            # Record the actual newly-created inode before writing. Even a
            # failed fsync must use lease/path/identity-checked rollback; never
            # perform an unconditional unlink from a write-error handler.
            stages.append((stage, created_info.st_dev, created_info.st_ino, data))
            with os.fdopen(descriptor, "wb") as handle:
                _assert_parents(root, parts[:-1])
                _require_lease(guard)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            info = _lstat(stage)
            if (info is None or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_dev != created_info.st_dev or info.st_ino != created_info.st_ino):
                raise _Reject()
            if _read_inode(stage, info.st_dev, info.st_ino) != data:
                raise _Reject()
            _assert_parents(root, parts[:-1])
            _require_lease(guard)
            os.link(stage, target)
            target_info = _lstat(target)
            if target_info is None or not stat.S_ISREG(target_info.st_mode) \
                    or target_info.st_dev != info.st_dev or target_info.st_ino != info.st_ino:
                raise _Reject()
            published.append((target, target_info.st_dev, target_info.st_ino, data))
            if _read_inode(target, target_info.st_dev, target_info.st_ino) != data:
                raise _Reject()
            if _read_inode(stage, info.st_dev, info.st_ino) != data:
                raise _Reject()
            _require_lease(guard)
            _assert_parents(root, parts[:-1])
            os.unlink(stage)
            stages.pop()
            final = _lstat(target)
            if final is None or not stat.S_ISREG(final.st_mode) or final.st_nlink != 1:
                raise _Reject()
        _verify_published(root, outputs_bytes, published, guard)
        _require_lease(guard)
    except BaseException:
        try:
            recovery = _rollback(root, created, published, stages, guard)
        except Exception:
            # An inaccessible cleanup target must never turn partially
            # published state into a false zero-write rejection.
            recovery = True
        raise _WriteFailure(recovery) from None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _failure(code, message):
    return {"ok": False, "error": {"code": code, "message": message}}


def apply_event(payload, *, workspace_root, lease_id, conversation_id=None, trusted_workspace_lock_root=None):
    """Validate and write the five source-bound event artifacts.

    Returns ``{"ok": True, "artifacts": [...], "output_receipt": {...}, ...}`` on
    success or a bounded ``{"ok": False, "error": {...}}`` failure.  Never claims
    content approval and never echoes external content or host paths.
    """
    try:
        if not isinstance(payload, dict):
            raise _Reject()
        raw = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        if len(raw.encode("utf-8")) > MAX_REQUEST_BYTES:
            raise _Reject()
        payload = strict_json(raw)
        _shape(payload, _PAYLOAD_KEYS, _PAYLOAD_KEYS)
        if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
            raise _Reject()
        if not isinstance(lease_id, str) or not lease_id or lease_id != lease_id.strip():
            raise _Reject()
        if conversation_id is not None and (not isinstance(conversation_id, str) or not conversation_id.strip()):
            raise _Reject()
        order = payload["work_order"]
        event, sources, attachments, _profile = _validate_work_order(order)
        content = _content(payload["content"], {source["id"] for source in sources})
        # Resolve only the caller-designated trust anchor, never output children.
        # This also pins a stable alias target for the official lock guard.
        root = Path(workspace_root).resolve(strict=True)
        bundle = Path(__file__).resolve().parents[3]
        if root == bundle or root.is_relative_to(bundle):
            raise _Reject()
        _resolve_owner(root, event)
        guard = _Guard(_load_guard(_trusted_lock_root(trusted_workspace_lock_root)), root, lease_id, conversation_id)
        _require_lease(guard)
        work_order_sha256 = canonical_json_sha256(order)
        outputs_bytes = _build_outputs(event, content, sources, attachments, order, work_order_sha256)
        mode = _preflight(root, outputs_bytes)
        if mode == "write":
            try:
                _publish(root, outputs_bytes, guard)
            except _WriteFailure as failure:
                if failure.recovery_required:
                    return _failure("event_write_recovery_required", _RECOVERY_MESSAGE)
                return _failure("event_write_rejected", _REJECTED_MESSAGE)
        artifacts = [relative for relative, _data in outputs_bytes]
        receipt = {"schema_version": 1, "work_order_sha256": work_order_sha256,
                   "source_snapshot_sha256": order["source_snapshot_sha256"],
                   "outputs": [{"path": relative, "sha256": hashlib.sha256(data).hexdigest()}
                               for relative, data in outputs_bytes]}
        return {"ok": True, "artifacts": artifacts, "output_receipt": receipt,
                "content_review": CONTENT_REVIEW, "content_approved": False,
                "work_order_sha256": work_order_sha256,
                "source_snapshot_sha256": order["source_snapshot_sha256"],
                "idempotent": mode == "idempotent"}
    except Exception:
        return _failure("event_write_rejected", _REJECTED_MESSAGE)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("Invalid arguments")


def _envelope(success, state, message, data=None, error=None):
    return {"action": ACTION, "success": success, "state": state, "message": message,
            "data": data if data is not None else {}, "error": error}


def _emit(envelope):
    sys.stdout.write(json.dumps(envelope, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main(argv=None):
    parser = _Parser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--request", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--lease-id", required=True)
    parser.add_argument("--conversation-id", default=None)
    parser.add_argument("--workspace-lock-root", default=None)
    parser.add_argument("--json", action="store_true")
    try:
        args = parser.parse_args(argv)
        with open(args.request, "rb") as stream:
            raw = stream.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError("Request exceeds bound")
        payload = strict_json(raw.decode("utf-8"))
        result = apply_event(payload, workspace_root=args.workspace, lease_id=args.lease_id,
                             conversation_id=args.conversation_id,
                             trusted_workspace_lock_root=args.workspace_lock_root)
    except (ValueError, TypeError, OSError, RecursionError, UnicodeDecodeError):
        message = "Invalid request, arguments or evidence; no event was written."
        _emit(_envelope(False, "Failed", message, error={"type": "InvalidRequest", "message": message}))
        return 2
    if not result.get("ok"):
        error = result.get("error") or {}
        message = error.get("message") or _REJECTED_MESSAGE
        error_type = "RecoveryRequired" if error.get("code") == "event_write_recovery_required" else "Rejected"
        _emit(_envelope(False, "Failed", message, error={"type": error_type, "message": message}))
        return 2
    _emit(_envelope(True, "Completed", "Source-bound event draft written; no content approval implied.",
                    data=result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
