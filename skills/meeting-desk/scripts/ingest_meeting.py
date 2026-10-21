#!/usr/bin/env python3
"""Canonical meeting record writer for meeting-desk (OI #35).

One public boundary owns every meeting ingest file mutation:

* ``ingest_record()`` validates one ``meeting-record/1`` record, resolves the
  trusted catalog routing, builds the object-register entry and publishes the
  summary/transcript/raw artifacts plus ``meetings.json``.
* ``publish_registered_transcript()`` is the only publication seam for the
  deterministic clean-transcript builder.
* ``inspect_workspace()`` is a read-only view of journals, register and
  ownership state; ``recover_workspace()`` resumes an interrupted publication
  under a freshly owned lease.

Contract highlights (see ``references/data-model.md`` and ``scripts/README.md``):

* Record-v1 has exactly the documented root/block fields; unknown extensions
  are only allowed inside the inert ``source_metadata`` object.
* Strict bounded JSON: duplicate keys, non-finite/overflowing numbers, lone
  surrogates, over-deep/oversized documents and invalid UTF-8 are rejected.
* The register is a canonical object with a ``meetings`` list. Unknown root
  metadata and untouched entries are preserved. Naked legacy arrays and
  malformed registers are rejected, never reset or migrated.
* Publication is staged and journalled under ``data/meeting-desk/``. New files
  are created no-clobber; generated updates require recorded writer ownership
  and the bound prior hash. The register is the last creation/update step.
  There is no claim of a multi-file ACID transaction: an interrupted run leaves
  a journal that blocks further mutation until explicit recovery.
* Recovery is explicit, lease-bound, forward-only, and refuses foreign bytes.
  A corrupt or inconsistent journal yields ``recovery_required`` (exit 3)
  without mutation.

Stdlib only. The module never writes into its own bundle and never performs
network access or automatic dependency installation.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile

__all__ = [
    "ingest_record",
    "ingest_local",
    "publish_registered_transcript",
    "inspect_workspace",
    "recover_workspace",
    "validate_record",
    "strict_json",
    "capabilities",
    "main",
    "WRITER_VERSION",
    "RECORD_SCHEMA",
]

ACTION = "ingest_meeting"
WRITER_VERSION = "meeting-desk/1"
RECORD_SCHEMA = "meeting-record/1"
JOURNAL_SCHEMA = "meeting-desk-journal/1"
OWNERSHIP_SCHEMA = "meeting-desk-ownership/1"
PLAN_SCHEMA = "meeting-desk-plan/1"
ENV_LOCK_ROOT = "MEETING_DESK_WORKSPACE_LOCK_ROOT"
LEGACY_ARRAY_REASON = (
    "register root is a legacy array; automatic conversion is not part of this package"
)

MAX_RECORD_BYTES = 1024 * 1024
MAX_REGISTER_BYTES = 16 * 1024 * 1024
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 200_000
MAX_ARTIFACT_CHARS = 512 * 1024
MAX_METADATA_BYTES = 64 * 1024
MAX_ENTRIES = 20_000
MAX_STRING = 500

_ZONE_ROOTS = ("memory/references", "memory/evidence", "memory/cloud", "data")
_UNROUTED = "memory/evidence/meetings/unrouted"
_REGISTER = "memory/evidence/meetings/meetings.json"
_JOURNAL_DIR = "data/meeting-desk/journal"
_OWNERSHIP = "data/meeting-desk/ownership.json"

_SYSTEMS = frozenset({"fireflies", "ticnote", "zoom", "manual", "vimeo-webvtt"})
_MAX_IDENTITY_CHARS = 512
_IDENTITY_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_SLUG = re.compile(r"[a-z0-9][a-z0-9_-]{0,99}\Z")

_RFC3339 = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})\Z"
)
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)
_RECORD_KEYS = frozenset(
    {
        "schema",
        "source",
        "meeting_id",
        "title",
        "start",
        "duration_minutes",
        "participants",
        "artifacts",
        "classification_hint",
        "review",
        "source_metadata",
    }
)
_SOURCE_KEYS = frozenset({"system", "account_ref", "external_id"})
_START_KEYS = frozenset({"utc", "calendar_date", "estimated", "basis"})
_ARTIFACT_KEYS = frozenset({"summary_md", "transcript_md", "transcript_raw_md"})
_HINT_KEYS = frozenset({"topic_slug", "project_slug", "event_slug", "basis"})
_REVIEW_KEYS = frozenset({"recommended", "reason"})
_ENTRY_KEYS = (
    "meeting_id", "source", "title", "date", "dateString", "meeting_date",
    "start_estimated", "duration_minutes", "duration", "participants",
    "summary_path", "transcript_path", "transcript_raw_path", "topic_slug",
    "project_slug", "event_slug", "classification_status", "classification_basis",
    "review_recommended", "review_reason", "content_status", "first_synced_at",
    "last_synced_at", "record_schema", "source_metadata",
)


class _Reject(Exception):
    """Fail-closed validation rejection; carries no record content."""


class _RecoveryRequired(Exception):
    """The workspace needs explicit recovery; no further mutation was done."""


class _LeaseRejected(_Reject):
    """Missing or foreign workspace lock (a rejection, not a recovery stop)."""


# ---------------------------------------------------------------------------
# Bounded strict JSON
# ---------------------------------------------------------------------------


def _bounded(value: object, depth: int = 0, nodes: list[int] | None = None) -> None:
    if nodes is None:
        nodes = [0]
    nodes[0] += 1
    if nodes[0] > MAX_JSON_NODES:
        raise _Reject("JSON document has too many nodes")
    if depth > MAX_JSON_DEPTH:
        raise _Reject("JSON document is nested too deeply")
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise _Reject("JSON contains an unpaired surrogate") from None
        if "\x00" in value:
            raise _Reject("JSON strings must not contain NUL")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise _Reject("JSON object keys must be strings")
            if len(key) > MAX_STRING:
                raise _Reject("JSON object key is too long")
            _bounded(key, depth + 1, nodes)
            _bounded(item, depth + 1, nodes)
        return
    if isinstance(value, list):
        for item in value:
            _bounded(item, depth + 1, nodes)
        return
    if isinstance(value, float) and not math.isfinite(value):
        raise _Reject("JSON contains a non-finite number")
    if isinstance(value, int) and abs(value) > 2**63 - 1:
        raise _Reject("JSON integer overflows the supported range")


def _reject_constant(_token: str) -> object:
    raise _Reject("JSON contains a non-finite number")


def strict_json(raw: str) -> object:
    """Parse JSON rejecting duplicate keys, non-finite/overflowing numbers."""

    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise _Reject("duplicate JSON key")
            result[key] = value
        return result

    def parse_int(text: str) -> int:
        value = int(text)
        if abs(value) > 2**63 - 1:
            raise _Reject("JSON integer overflows the supported range")
        return value

    def parse_float(text: str) -> float:
        value = float(text)
        if not math.isfinite(value):
            raise _Reject("JSON contains a non-finite number")
        return value

    try:
        value = json.loads(
            raw,
            object_pairs_hook=pairs,
            parse_int=parse_int,
            parse_float=parse_float,
            parse_constant=_reject_constant,
        )
    except _Reject:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError):
        raise _Reject("input is not valid JSON") from None
    _bounded(value)
    return value


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


# ---------------------------------------------------------------------------
# Record-v1 validation
# ---------------------------------------------------------------------------


def _require_keys(value: object, allowed: frozenset[str], where: str) -> dict:
    if not isinstance(value, dict):
        raise _Reject(f"{where} must be an object")
    missing = allowed - set(value)
    unknown = set(value) - allowed
    if missing:
        raise _Reject(f"{where} is missing field {sorted(missing)[0]!r}")
    if unknown:
        raise _Reject(f"{where} has unknown field {sorted(unknown)[0]!r}")
    return value


def _text(value: object, where: str, *, limit: int = MAX_STRING, empty: bool = False) -> str:
    if not isinstance(value, str):
        raise _Reject(f"{where} must be a string")
    if len(value) > limit:
        raise _Reject(f"{where} exceeds {limit} characters")
    if _CONTROL.search(value):
        raise _Reject(f"{where} contains a control character")
    if not empty and not value.strip():
        raise _Reject(f"{where} must not be empty")
    return value


def _identity(value: object, where: str) -> str:
    """Bounded opaque identity: exact Unicode, never interpreted as a path.

    Filesystem rules (reserved names, separators, trailing dots) intentionally do
    not apply: identities are only ever hashed for filenames and compared as
    strings. Control characters and unpaired surrogates are rejected; no
    normalization is performed.
    """
    if not isinstance(value, str):
        raise _Reject(f"{where} must be a string")
    if not value.strip():
        raise _Reject(f"{where} must not be empty")
    if len(value) > _MAX_IDENTITY_CHARS:
        raise _Reject(f"{where} exceeds {_MAX_IDENTITY_CHARS} characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise _Reject(f"{where} contains an unpaired surrogate") from None
    if _IDENTITY_CONTROL.search(value):
        raise _Reject(f"{where} contains a control character")
    return value


def _slug(value: object, where: str) -> str:
    text = _text(value, where, limit=100)
    if not _SLUG.match(text):
        raise _Reject(f"{where} is not a catalog slug")
    return text


def _string_list(value: object, where: str, *, limit: int, item_limit: int = 200) -> list[str]:
    if not isinstance(value, list):
        raise _Reject(f"{where} must be a list")
    if len(value) > limit:
        raise _Reject(f"{where} has too many entries")
    return [_text(item, f"{where}[{index}]", limit=item_limit)
            for index, item in enumerate(value)]


def _utc(value: object) -> tuple[str | None, int | None]:
    if value is None:
        return None, None
    text = _text(value, "start.utc", limit=40)
    if not _RFC3339.match(text):
        raise _Reject("start.utc must be RFC3339 with an explicit timezone")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise _Reject("start.utc must be RFC3339 with an explicit timezone") from None
    if parsed.tzinfo is None:
        raise _Reject("start.utc must be RFC3339 with an explicit timezone")
    utc = parsed.astimezone(timezone.utc)
    millis = int(utc.timestamp() * 1000)
    if utc.microsecond:
        rendered = utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond:06d}".rstrip("0") + "Z"
    else:
        rendered = utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    return rendered, millis


def _calendar_date(value: object) -> str | None:
    if value is None:
        return None
    text = _text(value, "start.calendar_date", limit=10)
    if not _ISO_DATE.match(text):
        raise _Reject("start.calendar_date must be an ISO calendar day")
    try:
        date.fromisoformat(text)
    except ValueError:
        raise _Reject("start.calendar_date must be an ISO calendar day") from None
    return text


def _duration(value: object) -> float | int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _Reject("duration_minutes must be a number or null")
    if isinstance(value, float) and not math.isfinite(value):
        raise _Reject("duration_minutes must be finite")
    if value < 0:
        raise _Reject("duration_minutes must not be negative")
    if value > 5_256_000:
        raise _Reject("duration_minutes exceeds the supported range")
    return value


def validate_record(value: object) -> dict:
    """Validate and normalize one Record-v1 object. Raises ``_Reject``."""
    record = _require_keys(value, _RECORD_KEYS, "record")
    if record["schema"] != RECORD_SCHEMA:
        raise _Reject(f"unsupported record schema (need {RECORD_SCHEMA!r})")

    source = _require_keys(record["source"], _SOURCE_KEYS, "record.source")
    system = source["system"]
    if not isinstance(system, str) or system not in _SYSTEMS:
        raise _Reject("record.source.system is not a supported source")
    account_ref = source["account_ref"]
    if account_ref is not None:
        account_ref = _text(account_ref, "record.source.account_ref", limit=200)
    external_id = _identity(source["external_id"], "record.source.external_id")

    meeting_id = _identity(record["meeting_id"], "record.meeting_id")
    title = _text(record["title"], "record.title", limit=500)

    start = _require_keys(record["start"], _START_KEYS, "record.start")
    utc, millis = _utc(start["utc"])
    calendar = _calendar_date(start["calendar_date"])
    estimated = start["estimated"]
    if not isinstance(estimated, bool):
        raise _Reject("record.start.estimated must be a boolean")
    basis = _string_list(start["basis"], "record.start.basis", limit=20)
    if estimated and not basis:
        raise _Reject("an estimated start needs a non-empty basis")
    if utc is None and not estimated:
        raise _Reject("a start without a known time must be estimated")
    if calendar is None and utc is not None:
        calendar = datetime.fromisoformat(utc.replace("Z", "+00:00")).date().isoformat()

    duration = _duration(record["duration_minutes"])
    participants = _string_list(record["participants"], "record.participants", limit=500,
                                item_limit=300)

    artifacts = _require_keys(record["artifacts"], _ARTIFACT_KEYS, "record.artifacts")
    normalized_artifacts: dict[str, str | None] = {}
    for key in sorted(_ARTIFACT_KEYS):
        raw = artifacts[key]
        if raw is not None:
            raw = _text(raw, f"record.artifacts.{key}", limit=MAX_ARTIFACT_CHARS, empty=True)
        normalized_artifacts[key] = raw
    summary = normalized_artifacts["summary_md"]
    transcript = normalized_artifacts["transcript_md"]
    raw_transcript = normalized_artifacts["transcript_raw_md"]
    if not (summary or "").strip() and not (transcript or "").strip():
        raise _Reject("a record needs a non-empty summary_md or transcript_md")
    if (transcript or "").strip() and not (raw_transcript or ""):
        raise _Reject("a new transcript requires the unchanged transcript_raw_md")

    hint = _require_keys(record["classification_hint"], _HINT_KEYS,
                         "record.classification_hint")
    normalized_hint = {}
    for key in ("topic_slug", "project_slug", "event_slug"):
        normalized_hint[key] = None if hint[key] is None else _slug(
            hint[key], f"record.classification_hint.{key}")
    normalized_hint["basis"] = _string_list(hint["basis"],
                                            "record.classification_hint.basis", limit=20)

    review = _require_keys(record["review"], _REVIEW_KEYS, "record.review")
    if not isinstance(review["recommended"], bool):
        raise _Reject("record.review.recommended must be a boolean")
    reason = review["reason"]
    if reason is not None:
        reason = _text(reason, "record.review.reason", limit=500)

    metadata = record["source_metadata"]
    if not isinstance(metadata, dict):
        raise _Reject("record.source_metadata must be an object")
    if len(_canonical(metadata)) > MAX_METADATA_BYTES:
        raise _Reject("record.source_metadata is too large")
    _bounded(metadata)

    return {
        "schema": RECORD_SCHEMA,
        "source": {"system": system, "account_ref": account_ref, "external_id": external_id},
        "meeting_id": meeting_id,
        "title": title,
        "start": {
            "utc": utc,
            "calendar_date": calendar,
            "estimated": estimated,
            "basis": basis,
            "epoch_ms": millis,
        },
        "duration_minutes": duration,
        "participants": participants,
        "artifacts": normalized_artifacts,
        "classification_hint": normalized_hint,
        "review": {"recommended": review["recommended"], "reason": reason},
        "source_metadata": metadata,
    }


# ---------------------------------------------------------------------------
# Paths, zones and filesystem safety
# ---------------------------------------------------------------------------


def _workspace_root(value: str | Path) -> Path:
    try:
        root = Path(value).resolve(strict=True)
    except OSError:
        raise _Reject("workspace root does not exist") from None
    if not root.is_dir():
        raise _Reject("workspace root is not a directory")
    bundle = Path(__file__).resolve().parents[3]
    if root == bundle or root.is_relative_to(bundle):
        raise _Reject("a skill bundle is not a consuming workspace")
    return root


def _lstat(path: Path):
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        raise _Reject("cannot inspect a workspace path") from None


def _is_reparse(info) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _safe_relative(value: object, where: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise _Reject(f"{where} is not a safe workspace-relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value:
        raise _Reject(f"{where} is not a safe workspace-relative path")
    for part in path.parts:
        if part in {"", ".", ".."} or part != part.strip() or part.endswith("."):
            raise _Reject(f"{where} is not a safe workspace-relative path")
        if part.split(".", 1)[0].casefold() in _WINDOWS_RESERVED:
            raise _Reject(f"{where} is not a safe workspace-relative path")
    return path


def _zone_of(relative: PurePosixPath) -> str:
    text = relative.as_posix()
    for zone in _ZONE_ROOTS:
        if text == zone or text.startswith(zone + "/"):
            return zone
    raise _Reject("path is outside the canonical data zones")


def _safe_target(root: Path, relative: str, where: str) -> Path:
    """Resolve a workspace-relative target and fail closed on unsafe components."""
    parts = _safe_relative(relative, where)
    zone = _zone_of(parts)
    current = root
    for index, part in enumerate(parts.parts):
        current = current / part
        info = _lstat(current)
        if info is None:
            continue
        if stat.S_ISLNK(info.st_mode) or _is_reparse(info):
            raise _Reject(f"{where} crosses a link or reparse point")
        last = index == len(parts.parts) - 1
        if last:
            if not stat.S_ISREG(info.st_mode):
                raise _Reject(f"{where} is not a regular file")
            if info.st_nlink > 1:
                raise _Reject(f"{where} is a hardlink alias")
        elif not stat.S_ISDIR(info.st_mode):
            raise _Reject(f"{where} has a non-directory parent")
    resolved = Path(os.path.realpath(root / parts))
    if not resolved.is_relative_to(root):
        raise _Reject(f"{where} escapes the workspace")
    bundle = Path(__file__).resolve().parents[3]
    if resolved == bundle or resolved.is_relative_to(bundle):
        raise _Reject(f"{where} points into the skill bundle")
    zone_root = Path(os.path.realpath(root / zone))
    if not resolved.is_relative_to(zone_root):
        raise _Reject(f"{where} escapes its canonical data zone")
    return root / parts


def _mkdir_chain(root: Path, relative_dir: PurePosixPath) -> None:
    zone = _zone_of(relative_dir)
    current = root
    for part in relative_dir.parts:
        current = current / part
        info = _lstat(current)
        if info is None:
            try:
                current.mkdir()
            except FileExistsError:
                info = _lstat(current)
                if info is None or stat.S_ISLNK(info.st_mode) or _is_reparse(info):
                    raise _Reject("a workspace directory changed during publication") from None
            continue
        if stat.S_ISLNK(info.st_mode) or _is_reparse(info) or not stat.S_ISDIR(info.st_mode):
            raise _Reject("a workspace directory is not a real directory")
    resolved = Path(os.path.realpath(root / relative_dir))
    if not resolved.is_relative_to(Path(os.path.realpath(root / zone))) or not resolved.is_relative_to(root):
        raise _Reject("a workspace directory escapes its canonical data zone")


def _read_bytes(path: Path, limit: int, where: str) -> bytes | None:
    info = _lstat(path)
    if info is None:
        return None
    if stat.S_ISLNK(info.st_mode) or _is_reparse(info) or not stat.S_ISREG(info.st_mode):
        raise _Reject(f"{where} is not a regular file")
    if info.st_nlink > 1:
        raise _Reject(f"{where} is a hardlink alias")
    if info.st_size > limit:
        raise _Reject(f"{where} exceeds the supported size")
    try:
        with path.open("rb") as handle:
            return handle.read(limit + 1)
    except OSError:
        raise _Reject(f"{where} cannot be read") from None


def _same_bytes(path: Path, data: bytes) -> bool:
    info = _lstat(path)
    if info is None or stat.S_ISLNK(info.st_mode) or _is_reparse(info):
        return False
    if not stat.S_ISREG(info.st_mode) or info.st_size != len(data):
        return False
    return _read_bytes(path, len(data), "target") == data


# ---------------------------------------------------------------------------
# Trusted workspace-lock guard (loaded in place; no copies, no legacy)
# ---------------------------------------------------------------------------


class _Guard:
    def __init__(self, module, workspace: Path, lease_id: str, conversation_id: str | None):
        self.module = module
        self.workspace = workspace
        self.lease_id = lease_id
        self.conversation_id = conversation_id


_GUARD_CACHE: dict[str, object] = {}


def _trusted_lock_root(explicit: str | Path | None) -> Path:
    if explicit is not None:
        return Path(explicit)
    env = os.environ.get("WORKSPACE_LOCK_ROOT", "").strip() or os.environ.get(ENV_LOCK_ROOT, "").strip()
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[3].parent / "workspace-lock"


def _load_guard(trusted_root: str | Path) -> object:
    root = Path(trusted_root)
    guard_path = root / "scripts" / "workspace_lock_guard.py"
    cli_path = root / "scripts" / "invoke_workspace_lock.py"
    if not guard_path.is_file() or not cli_path.is_file():
        raise _Reject("Install workspace-lock and set WORKSPACE_LOCK_ROOT or --workspace-lock-root "
                      "to its trusted installation; see README.md")
    key = os.path.realpath(guard_path)
    cached = _GUARD_CACHE.get(key)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location("_meeting_desk_workspace_lock_guard", guard_path)
    if spec is None or spec.loader is None:
        raise _Reject("the trusted workspace-lock guard cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(spec.name)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        raise _Reject("the trusted workspace-lock guard cannot be loaded") from None
    finally:
        if previous is None:
            sys.modules.pop(spec.name, None)
        else:
            sys.modules[spec.name] = previous
    if not callable(getattr(module, "require_workspace_lock", None)):
        raise _Reject("the trusted workspace-lock guard has no ownership check")
    _GUARD_CACHE[key] = module
    return module


def _require_lease(guard: _Guard) -> None:
    try:
        guard.module.require_workspace_lock(
            guard.workspace,
            lease_id=guard.lease_id,
            conversation_id=guard.conversation_id,
            allow_legacy=False,
        )
    except Exception:
        raise _LeaseRejected("an owned active workspace-lock lease is required") from None


# ---------------------------------------------------------------------------
# Register, ownership and catalogs
# ---------------------------------------------------------------------------


def _decode_document(raw: bytes, where: str) -> object:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _Reject(f"{where} is not valid UTF-8") from None
    return strict_json(text)


def _read_json_file(root: Path, relative: str, limit: int,
                    where: str) -> tuple[object | None, bytes | None]:
    path = _safe_target(root, relative, where)
    raw = _read_bytes(path, limit, where)
    if raw is None:
        return None, None
    return _decode_document(raw, where), raw


def _register_entries(document: object) -> list[dict]:
    if not isinstance(document, dict):
        raise _Reject(LEGACY_ARRAY_REASON if isinstance(document, list)
                      else "register root must be an object")
    if "meetings" not in document:
        raise _Reject("register root has no 'meetings' list")
    entries = document["meetings"]
    if not isinstance(entries, list):
        raise _Reject("register 'meetings' must be a list")
    if len(entries) > MAX_ENTRIES:
        raise _Reject("register has too many entries")
    for entry in entries:
        if not isinstance(entry, dict):
            raise _Reject("register entries must be objects")
    return entries


def _entry_identities(entry: dict) -> tuple[set[str], set[tuple[str, str]]]:
    """Return documented stable identities of an existing register entry."""
    names: set[str] = set()
    pairs: set[tuple[str, str]] = set()
    source = entry.get("source")
    if isinstance(source, dict):
        system = source.get("system")
        external = source.get("external_id")
        if isinstance(system, str) and isinstance(external, str) and system and external:
            pairs.add((system, external))
    for key in ("meeting_id", "id"):
        value = entry.get(key)
        if isinstance(value, str) and value:
            names.add(value)
    external = entry.get("external_id")
    if isinstance(external, str) and external:
        pairs.add(("fireflies", external))
    if isinstance(source, dict) and source.get("system") == "fireflies":
        for name in list(names):
            if name.startswith("fireflies_"):
                names.add(name[len("fireflies_"):])
    return names, pairs


def _legacy_name_proves_source(name: str, record: dict) -> bool:
    """Return True when an unsourced legacy name is provably from this source.

    A bare ID is never enough: adopting it would reassign an unrelated source.
    Only the documented per-adapter forms count.
    """
    system = record["source"]["system"]
    external = record["source"]["external_id"]
    meeting_id = record["meeting_id"]
    if system == "fireflies":
        return name in {external, f"fireflies_{external}"}
    if system == "ticnote":
        return name == f"ticnote-{external}"
    if system == "zoom":
        return name == f"zoom-{external}" or (external.startswith("zoom-") and name == external)
    if system == "vimeo-webvtt":
        return name == f"vimeo-{external}" or (
            external.startswith("vimeo-") and name == external)
    if system == "manual":
        return name == external and name.startswith("manual-")
    return False


def _effective_identity(existing: dict | None, record: dict) -> str:
    """Keep the matched entry's stable ID; never force a rekey."""
    if existing is not None:
        for key in ("meeting_id", "id"):
            value = existing.get(key)
            if isinstance(value, str) and value:
                return value
    return record["meeting_id"]


def _match_entries(entries: list[dict], record: dict) -> tuple[int | None, list[int]]:
    """Return (index, ambiguous_indices) for the stable identity of the record."""
    system = record["source"]["system"]
    external = record["source"]["external_id"]
    meeting_id = record["meeting_id"]
    matched: list[int] = []
    for index, entry in enumerate(entries):
        names, pairs = _entry_identities(entry)
        source = entry.get("source")
        source_system = source.get("system") if isinstance(source, dict) else None
        by_pair = (system, external) in pairs
        if source_system is None:
            proven = [name for name in names if _legacy_name_proves_source(name, record)]
            by_name = bool(proven)
            if meeting_id in names and not by_name:
                raise _Reject("an unsourced legacy identity is not proven for this source")
        else:
            by_name = meeting_id in names
            if by_name and source_system != system:
                raise _Reject("the meeting id is bound to a different source system")
        if by_pair and isinstance(source_system, str) and source_system != system:
            raise _Reject("the external identity is bound to a different source system")
        if not (by_name or by_pair):
            continue
        if isinstance(source, dict):
            existing_account = source.get("account_ref")
            if (record["source"]["account_ref"] and existing_account
                    and record["source"]["account_ref"] != existing_account):
                raise _Reject("the meeting identity is bound to a different account")
        matched.append(index)
    if len(matched) > 1:
        return None, matched
    return (matched[0] if matched else None), []


def _ownership_document(raw: object | None) -> dict:
    if raw is None:
        return {"schema": OWNERSHIP_SCHEMA, "files": []}
    if not isinstance(raw, dict) or raw.get("schema") != OWNERSHIP_SCHEMA:
        raise _Reject("the writer ownership record has an unsupported schema")
    files = raw.get("files")
    if not isinstance(files, list):
        raise _Reject("the writer ownership record has no 'files' list")
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise _Reject("the writer ownership record has an invalid entry")
    return raw


def _ownership_entry(document: dict, relative: str) -> dict | None:
    for item in document["files"]:
        if item["path"] == relative:
            return item
    return None


def _catalog_entries(root: Path, relative: str, key: str) -> list[dict]:
    document, _raw = _read_json_file(root, relative, MAX_REGISTER_BYTES, relative)
    if document is None:
        raise _Reject(f"the routing catalog {relative} is missing")
    if isinstance(document, list):
        entries = document
    elif isinstance(document, dict):
        entries = document.get(key, [])
    else:
        raise _Reject(f"the routing catalog {relative} is invalid")
    if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
        raise _Reject(f"the routing catalog {relative} is invalid")
    return entries


def _catalog_entry(entries: list[dict], slug: str, where: str) -> dict:
    found = [entry for entry in entries if entry.get("id") == slug]
    if len(found) != 1:
        raise _Reject(f"{where} {slug!r} is not uniquely catalogued")
    entry = found[0]
    status = entry.get("status")
    if status is not None:
        if not isinstance(status, str) or status.casefold() not in {"", "active", "inactive"}:
            raise _Reject(f"{where} {slug!r} has an unsupported routing status")
        if status.casefold() == "inactive":
            raise _Reject(f"{where} {slug!r} is not active in its catalog")
    return entry


def _event_ids(entry: dict) -> set[str]:
    ids: set[str] = set()
    events = entry.get("events")
    if isinstance(events, list):
        for event in events:
            if isinstance(event, dict) and isinstance(event.get("id"), str):
                ids.add(event["id"])
    subtopics = entry.get("subtopics")
    if isinstance(subtopics, list):
        for subtopic in subtopics:
            if isinstance(subtopic, dict):
                ids |= _event_ids(subtopic)
    return ids


def _resolve_routing(root: Path, topic: str | None, project: str | None,
                     event: str | None) -> dict:
    """Resolve a trusted operator routing choice against the catalogs."""
    if event is not None and topic is None and project is None:
        raise _Reject("--event requires a catalogued --topic or --project owner")
    if topic is not None and project is not None:
        project = None  # Topic wins for double tagging (skill contract).
    if topic is not None:
        topic = _slug(topic, "topic routing choice")
        entry = _catalog_entry(_catalog_entries(root, "memory/references/topics/topics.json",
                                                "topics"), topic, "topic")
        if event is not None:
            event = _slug(event, "event routing choice")
            if event not in _event_ids(entry):
                raise _Reject("the event is not catalogued under the selected topic")
        return {"kind": "topic", "owner": topic, "topic_slug": topic, "project_slug": None,
                "event_slug": event}
    if project is not None:
        project = _slug(project, "project routing choice")
        entry = _catalog_entry(_catalog_entries(root, "memory/references/projects/projects.json",
                                                "projects"), project, "project")
        if event is not None:
            event = _slug(event, "event routing choice")
            if event not in _event_ids(entry):
                raise _Reject("the event is not catalogued under the selected project")
        return {"kind": "project", "owner": project, "topic_slug": None,
                "project_slug": project, "event_slug": event}
    return {"kind": "unmapped", "owner": None, "topic_slug": None, "project_slug": None,
            "event_slug": None}


def _routing_dir(routing: dict) -> str:
    if routing["kind"] == "unmapped":
        return _UNROUTED
    base = (f"memory/evidence/topics/{routing['owner']}"
            if routing["kind"] == "topic"
            else f"memory/evidence/projects/{routing['owner']}")
    if routing["event_slug"]:
        return f"{base}/events/{routing['event_slug']}/meetings"
    return f"{base}/meetings"


def _title_slug(title: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-")
    return text[:60].strip("-") or "meeting"


def _file_stem(record: dict) -> str:
    day = record["start"]["calendar_date"] or "undated"
    suffix = _sha(record["meeting_id"].encode("utf-8"))[:8]
    return f"{day}-{_title_slug(record['title'])}-{suffix}"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _workspace_fingerprint(root: Path) -> str:
    return _sha(root.as_posix().casefold().encode("utf-8"))[:32]


# ---------------------------------------------------------------------------
# Entry projection and artifact rendering
# ---------------------------------------------------------------------------


def _entry_paths(routing: dict, stem: str, artifacts: dict) -> dict:
    directory = _routing_dir(routing)
    return {
        "summary_path": f"{directory}/{stem}.summary.md" if artifacts["summary_md"] else None,
        "transcript_path": (f"{directory}/{stem}.transcript.md"
                            if artifacts["transcript_md"] else None),
        "transcript_raw_path": (f"{directory}/{stem}.transcript.raw.md"
                                if artifacts["transcript_raw_md"] else None),
    }


def _review_state(record: dict, status: str, changed: bool,
                  existing: dict | None) -> tuple[bool, str | None]:
    if existing is not None and not changed:
        requested = bool(record["review"]["recommended"])
        recommended = bool(existing.get("review_recommended")) or requested
        reason = existing.get("review_reason")
        if requested and not reason:
            reason = "source-requested"
        return recommended, reason
    reasons: list[str] = []
    if status != "classified":
        reasons.append("unmapped-routing")
    if record["start"]["estimated"]:
        reasons.append("estimated-start")
    if record["duration_minutes"] is None:
        reasons.append("unknown-duration")
    if changed:
        reasons.append("content-changed")
    if record["review"]["recommended"]:
        reasons.append("source-requested")
    if existing and existing.get("review_recommended"):
        return True, existing.get("review_reason") or "; ".join(reasons) or None
    reason = "; ".join(dict.fromkeys(reasons)) or None
    adapter = record["review"]["reason"]
    if adapter:
        reason = f"{reason}; adapter: {adapter}" if reason else f"adapter: {adapter}"
    return bool(reasons), reason


def _preserve(entry: dict | None, key: str, value: object) -> object:
    """Keep an existing value when the record carries no new information."""
    if entry is None:
        return value
    current = entry.get(key)
    if value in (None, [], "") and current not in (None, [], ""):
        return current
    return value


def _project_entry(record: dict, existing: dict | None, routing: dict, paths: dict,
                   *, changed: bool, now: str, effective_id: str) -> dict:
    entry = dict(existing) if existing else {}
    status = "classified" if routing["kind"] != "unmapped" else "unmapped"
    entry["meeting_id"] = effective_id
    merged_source = dict(existing.get("source")) if existing and isinstance(
        existing.get("source"), dict) else {}
    merged_source["system"] = record["source"]["system"]
    merged_source["external_id"] = record["source"]["external_id"]
    if record["source"]["account_ref"] is not None:
        merged_source["account_ref"] = record["source"]["account_ref"]
    elif "account_ref" not in merged_source:
        merged_source["account_ref"] = None
    entry["source"] = merged_source
    entry["title"] = record["title"]
    if record["start"]["utc"] is not None:
        entry["date"] = record["start"]["epoch_ms"]
        entry["dateString"] = record["start"]["utc"]
        entry["start_estimated"] = bool(record["start"]["estimated"])
    elif existing is not None and ("date" in existing or "dateString" in existing):
        # Legacy time values are preserved, never parsed or normalized by a
        # date-only reingest.
        entry["date"] = existing.get("date")
        entry["dateString"] = existing.get("dateString")
        entry["start_estimated"] = existing.get("start_estimated",
                                                bool(record["start"]["estimated"]))
    else:
        entry["date"] = None
        entry["dateString"] = None
        entry["start_estimated"] = bool(record["start"]["estimated"])
    entry["meeting_date"] = _preserve(existing, "meeting_date",
                                      record["start"]["calendar_date"])
    duration = _preserve(existing, "duration_minutes", record["duration_minutes"])
    entry["duration_minutes"] = duration
    entry["duration"] = duration
    entry["participants"] = _preserve(existing, "participants", list(record["participants"]))
    entry["summary_path"] = paths["summary_path"]
    entry["transcript_path"] = paths["transcript_path"]
    entry["transcript_raw_path"] = paths["transcript_raw_path"]
    if routing.get("preserved"):
        entry["topic_slug"] = routing.get("topic_slug")
        entry["project_slug"] = routing.get("project_slug")
        entry["event_slug"] = routing.get("event_slug")
        entry["classification_status"] = routing.get("classification_status", status)
        entry["classification_basis"] = list(routing.get("classification_basis") or [])
    else:
        entry["topic_slug"] = routing["topic_slug"] if routing["kind"] != "unmapped" else None
        entry["project_slug"] = (routing["project_slug"]
                                 if routing["kind"] != "unmapped" else None)
        entry["event_slug"] = routing["event_slug"] if routing["kind"] != "unmapped" else None
        entry["classification_status"] = status
        entry["classification_basis"] = ["operator-choice"] if status == "classified" else []
    recommended, reason = _review_state(record, entry["classification_status"], changed, existing)
    entry["review_recommended"] = recommended
    entry["review_reason"] = reason
    if existing and not changed:
        entry["content_status"] = existing.get("content_status", "provisional")
    else:
        entry["content_status"] = "provisional"
    entry["first_synced_at"] = (existing or {}).get("first_synced_at") or now
    entry["last_synced_at"] = (existing.get("last_synced_at") if existing and not changed
                               else now)
    entry["record_schema"] = RECORD_SCHEMA
    metadata = dict(existing.get("source_metadata")) if existing and isinstance(
        existing.get("source_metadata"), dict) else {}
    metadata.update(record["source_metadata"])
    entry["source_metadata"] = metadata
    if existing is None:
        return {key: entry[key] for key in _ENTRY_KEYS}
    return entry


def _render_frontmatter(record: dict, routing: dict, paths: dict, document_type: str) -> str:
    def scalar(value: object) -> str:
        return json.dumps(value, ensure_ascii=False)

    lines = ["---", f"document_type: {document_type}", "evidence_level: observed",
             "status: provisional", f"meeting_id: {scalar(record['meeting_id'])}",
             f"title: {scalar(record['title'])}"]
    lines.append(f"date: {scalar(record['start']['calendar_date'])}")
    lines.append(f"topic: {scalar(routing['topic_slug'])}")
    lines.append(f"project: {scalar(routing['project_slug'])}")
    lines.append(f"event: {scalar(routing['event_slug'])}")
    if record["participants"]:
        lines.append("participants:")
        for participant in record["participants"]:
            lines.append(f"  - {scalar(participant)}")
    else:
        lines.append("participants: []")
    if document_type == "meeting-summary":
        lines.append(f"summary_path: {scalar(paths['summary_path'])}")
    else:
        lines.append(f"transcript_path: {scalar(paths['transcript_path'])}")
        lines.append(f"source_raw: {scalar(paths['transcript_raw_path'])}")
    lines.append("---")
    return "\n".join(lines) + "\n\n"


def _artifact_payload(record: dict, routing: dict, paths: dict) -> dict[str, bytes]:
    payload: dict[str, bytes] = {}
    if record["artifacts"]["summary_md"]:
        text = (_render_frontmatter(record, routing, paths, "meeting-summary")
                + record["artifacts"]["summary_md"])
        payload[paths["summary_path"]] = text.encode("utf-8")
    if record["artifacts"]["transcript_md"]:
        text = (_render_frontmatter(record, routing, paths, "meeting-transcript")
                + record["artifacts"]["transcript_md"])
        payload[paths["transcript_path"]] = text.encode("utf-8")
    if record["artifacts"]["transcript_raw_md"]:
        payload[paths["transcript_raw_path"]] = record["artifacts"][
            "transcript_raw_md"].encode("utf-8")
    return payload


# ---------------------------------------------------------------------------
# Journal-backed publication
# ---------------------------------------------------------------------------


def _journal_state(root: Path) -> tuple[list[Path], list[str]]:
    """Return journal files and any unexpected journal-directory entries.

    Only regular ``*.journal.json`` files and the single-level stage directory
    bound to an existing journal are recognized. Everything else is reported as
    foreign; it is never traversed, deleted or "cleaned to pass".
    """
    directory = root / _JOURNAL_DIR
    info = _lstat(directory)
    if info is None:
        return [], []
    if stat.S_ISLNK(info.st_mode) or _is_reparse(info) or not stat.S_ISDIR(info.st_mode):
        raise _RecoveryRequired("the journal directory is not a real directory")
    entries = sorted(directory.iterdir(), key=lambda item: item.name)
    journals: list[Path] = []
    unexpected: list[str] = []
    bound: set[str] = set()
    for entry in entries:
        entry_info = _lstat(entry)
        if entry.name.endswith(".journal.json"):
            if (entry_info is None or stat.S_ISLNK(entry_info.st_mode)
                    or _is_reparse(entry_info) or not stat.S_ISREG(entry_info.st_mode)
                    or entry_info.st_nlink > 1):
                unexpected.append(entry.name)
            else:
                journals.append(entry)
                bound.add(entry.name[: -len(".journal.json")])
    for entry in entries:
        if entry.name.endswith(".journal.json"):
            continue
        entry_info = _lstat(entry)
        if (entry.name in bound and entry_info is not None
                and stat.S_ISDIR(entry_info.st_mode)
                and not stat.S_ISLNK(entry_info.st_mode)
                and not _is_reparse(entry_info)):
            continue
        unexpected.append(entry.name)
    return journals, unexpected


def _require_quiet_journals(root: Path) -> None:
    journals, unexpected = _journal_state(root)
    if journals or unexpected:
        raise _RecoveryRequired(
            "an open journal or unverifiable journal-directory entry blocks mutation")


def _journal_digest(body: dict) -> str:
    return _sha(_canonical(body))


def _write_journal(path: Path, body: dict) -> None:
    document = {"integrity": {"algorithm": "sha256", "digest": _journal_digest(body)},
                "journal": body}
    _write_atomic(path, _json_text(document).encode("utf-8"), None)


def _read_journal(path: Path) -> dict:
    raw = _read_bytes(path, MAX_REGISTER_BYTES, "journal")
    if raw is None:
        raise _RecoveryRequired("journal disappeared")
    document = _decode_document(raw, "journal")
    if (not isinstance(document, dict) or not isinstance(document.get("journal"), dict)
            or not isinstance(document.get("integrity"), dict)):
        raise _RecoveryRequired("journal structure is unexpected")
    body = document["journal"]
    integrity = document["integrity"]
    if integrity.get("algorithm") != "sha256" or integrity.get("digest") != _journal_digest(body):
        raise _RecoveryRequired("journal integrity check failed")
    if body.get("schema") != JOURNAL_SCHEMA:
        raise _RecoveryRequired("journal schema is unsupported")
    steps = body.get("steps")
    if not isinstance(steps, list) or any(not isinstance(step, dict) for step in steps):
        raise _RecoveryRequired("journal steps are invalid")
    return body


def _write_atomic(path: Path, data: bytes, expected_sha: str | None) -> None:
    if expected_sha is not None:
        current = _read_bytes(path, MAX_REGISTER_BYTES, "target")
        if current is None or _sha(current) != expected_sha:
            raise _Reject("target bytes changed before publication")
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if expected_sha is not None:
            current = _read_bytes(path, MAX_REGISTER_BYTES, "target")
            if current is None or _sha(current) != expected_sha:
                raise _Reject("target bytes changed before publication")
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _create_no_clobber(path: Path, data: bytes) -> None:
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            raise _Reject("target appeared before publication") from None
        except OSError:
            # Filesystems without hardlink support: exclusive create is still no-clobber.
            exclusive = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                | getattr(os, "O_BINARY", 0))
            try:
                with os.fdopen(exclusive, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
            except BaseException:
                try:
                    os.unlink(path)
                except OSError:
                    pass
                raise
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def _ensure_parent(root: Path, relative: str) -> None:
    parts = _safe_relative(relative, "publication target")
    if len(parts.parts) > 1:
        _mkdir_chain(root, PurePosixPath(*parts.parts[:-1]))


def _publish_one(root: Path, step: dict) -> None:
    """Publish exactly one journal step (module-level so a crash test can stop it)."""
    _ensure_parent(root, step["path"])
    target = _safe_target(root, step["path"], "publication target")
    kind = step["kind"]
    if kind == "create":
        data = _stage_bytes(root, step)
        _create_no_clobber(target, data)
        if not _same_bytes(target, data):
            raise OSError("published bytes do not match the plan")
    elif kind == "replace":
        data = _stage_bytes(root, step)
        current = _read_bytes(target, MAX_REGISTER_BYTES, "publication target")
        if current is None or _sha(current) != step["pre_sha256"]:
            raise OSError("target bytes changed before replace")
        _write_atomic(target, data, step["pre_sha256"])
        if not _same_bytes(target, data):
            raise OSError("published bytes do not match the plan")
    elif kind == "delete":
        current = _read_bytes(target, MAX_REGISTER_BYTES, "publication target")
        if current is None:
            return
        if _sha(current) != step["pre_sha256"]:
            raise OSError("target bytes changed before deletion")
        try:
            os.unlink(target)
        except OSError:
            raise OSError("target cannot be removed") from None
    else:
        raise _Reject("journal step has an unsupported kind")


def _stage_bytes(root: Path, step: dict) -> bytes:
    stage = step.get("stage")
    if not isinstance(stage, str):
        raise _Reject("journal step has no staged payload")
    path = _safe_target(root, stage, "staged payload")
    data = _read_bytes(path, MAX_REGISTER_BYTES, "staged payload")
    if data is None or _sha(data) != step["post_sha256"]:
        raise _RecoveryRequired("staged payload is missing or changed")
    if len(data) != step["bytes"]:
        raise _RecoveryRequired("staged payload size changed")
    return data


def _persist_journal(path: Path, body: dict) -> None:
    body["updated_at"] = _iso_now()
    _write_journal(path, body)


def _artifact_kind(relative: str) -> str:
    if relative.endswith(".transcript.raw.md"):
        return "transcript_raw"
    if relative.endswith(".transcript.md"):
        return "transcript"
    return "summary"


def _new_ownership(ownership: dict, payload: dict[str, bytes], record: dict,
                   *, drop: set[str], claimable: set[str] | None = None) -> dict:
    files = [dict(item) for item in ownership["files"] if item["path"] not in drop]
    by_path = {item["path"]: item for item in files}
    for relative, data in payload.items():
        if claimable is not None and relative not in claimable:
            continue
        digest = _sha(data)
        current = by_path.get(relative)
        if current is not None and current.get("sha256") == digest:
            continue
        files = [item for item in files if item["path"] != relative]
        files.append({
            "path": relative, "meeting_id": record["meeting_id"], "sha256": digest,
            "bytes": len(data), "kind": _artifact_kind(relative),
            "writer_version": WRITER_VERSION, "written_at": _iso_now()})
    files.sort(key=lambda item: item["path"])
    return {"schema": OWNERSHIP_SCHEMA, "files": files}


def _file_steps(root: Path, payload: dict[str, bytes], ownership: dict) -> tuple[list[dict], bool]:
    steps: list[dict] = []
    changed = False
    for relative, data in sorted(payload.items()):
        target = root / relative
        digest = _sha(data)
        if _same_bytes(target, data):
            continue
        changed = True
        info = _lstat(target)
        if info is None:
            steps.append({"kind": "create", "path": relative, "phase": "files", "stage": None,
                          "pre_sha256": None, "post_sha256": digest, "bytes": len(data)})
            continue
        owner = _ownership_entry(ownership, relative)
        current = _read_bytes(target, MAX_REGISTER_BYTES, "artifact")
        if (owner is None or not isinstance(owner.get("sha256"), str)
                or _sha(current) != owner["sha256"]):
            raise _Reject("an existing artifact is not provably writer-owned; refusing to overwrite")
        if _artifact_kind(relative) == "transcript_raw":
            raise _Reject("the raw transcript artifact is immutable")
        steps.append({"kind": "replace", "path": relative, "phase": "files", "stage": None,
                      "pre_sha256": _sha(current), "post_sha256": digest, "bytes": len(data)})
    return steps, changed


def _ownership_step(root: Path, ownership: dict, new_ownership: dict) -> tuple[dict | None, bool]:
    current = _read_bytes(root / _OWNERSHIP, MAX_REGISTER_BYTES, "ownership record")
    data = _json_text(new_ownership).encode("utf-8")
    if current == data:
        return None, False
    return ({"kind": "create" if current is None else "replace", "path": _OWNERSHIP,
             "phase": "ownership", "stage": None,
             "pre_sha256": None if current is None else _sha(current),
             "post_sha256": _sha(data), "bytes": len(data)}, True)


def _register_step(root: Path, existed: bool, data: bytes,
                   current: bytes | None) -> tuple[dict | None, bool]:
    if current == data:
        return None, False
    if not existed and current is not None:
        raise _Reject("register appeared after the plan was computed")
    return ({"kind": "create" if current is None else "replace", "path": _REGISTER,
             "phase": "register", "stage": None,
             "pre_sha256": None if current is None else _sha(current),
             "post_sha256": _sha(data), "bytes": len(data)}, True)


def _apply(root: Path, record: dict, payload: dict[str, bytes], ownership: dict,
           register_bytes: bytes | None, steps: list[dict], guard: _Guard,
           *, action: str) -> dict:
    journal_id = _sha(f"{_iso_now()}-{record['meeting_id']}-{os.getpid()}".encode())[:24]
    journal_rel = f"{_JOURNAL_DIR}/{journal_id}.journal.json"
    journal_path = _safe_target(root, journal_rel, "journal path")
    _require_lease(guard)
    _mkdir_chain(root, PurePosixPath(*PurePosixPath(_JOURNAL_DIR).parts))

    register_pre = _read_bytes(root / _REGISTER, MAX_REGISTER_BYTES, "register")
    ownership_pre = _read_bytes(root / _OWNERSHIP, MAX_REGISTER_BYTES, "ownership record")
    body = {
        "schema": JOURNAL_SCHEMA,
        "journal_id": journal_id,
        "action": action,
        "writer_version": WRITER_VERSION,
        "workspace_fingerprint": _workspace_fingerprint(root),
        "created_at": _iso_now(),
        "updated_at": _iso_now(),
        "lease_id": guard.lease_id,
        "conversation_id": guard.conversation_id,
        "meeting_id": record["meeting_id"],
        "state": "prepared",
        "prestate": {
            "register_sha256": _sha(register_pre) if register_pre is not None else None,
            "ownership_sha256": _sha(ownership_pre) if ownership_pre is not None else None,
        },
        "poststate": {"register_sha256": _sha(register_bytes) if register_bytes else None},
        "steps": [],
    }
    artifacts = dict(payload)
    artifacts[_OWNERSHIP] = _json_text(ownership).encode("utf-8")
    if register_bytes is not None:
        artifacts[_REGISTER] = register_bytes
    for index, step in enumerate(steps):
        if step["kind"] != "delete":
            data = artifacts.get(step["path"])
            if data is None:
                raise _Reject("planned step has no payload")
            relative = f"{_JOURNAL_DIR}/{journal_id}/stage/{index:03d}.bin"
            target = _safe_target(root, relative, "staging path")
            _require_lease(guard)
            _mkdir_chain(root, PurePosixPath(*PurePosixPath(relative).parts[:-1]))
            if _lstat(target) is not None:
                raise _Reject("staging target already exists")
            _require_lease(guard)
            _create_no_clobber(target, data)
            step["stage"] = relative
        step["state"] = "pending"
        body["steps"].append(step)
    _require_lease(guard)
    _write_journal(journal_path, body)
    try:
        body["state"] = "publishing"
        _require_lease(guard)
        _persist_journal(journal_path, body)
        for step in body["steps"]:
            _require_lease(guard)
            _publish_one(root, step)
            step["state"] = "published" if step["kind"] != "delete" else "deleted"
            _require_lease(guard)
            _persist_journal(journal_path, body)
        body["state"] = "completed"
        _require_lease(guard)
        _persist_journal(journal_path, body)
        _require_lease(guard)
        _finalize_journal(root, journal_path, body, guard)
    except Exception:
        _mark_failed(journal_path, body, guard)
        raise _RecoveryRequired("publication failed after it began") from None
    return {"journal_id": journal_id, "steps": len(body["steps"])}


def _mark_failed(journal_path: Path, body: dict, guard: _Guard) -> None:
    try:
        _require_lease(guard)
        body["state"] = "failed"
        _persist_journal(journal_path, body)
    except (OSError, ValueError, _Reject):
        pass


def _finalize_journal(root: Path, journal_path: Path, body: dict, guard: _Guard | None = None) -> None:
    """Remove only journal-bound own stage payloads, then the journal.

    No recursive traversal and no deletion of unknown subtrees: every stage file
    must be the exact single-level path recorded in the journal, a regular
    non-linked file with the recorded hash/size. Any foreign byte leaves the
    journal open (``recovery_required``).
    """
    stem = journal_path.name[: -len(".journal.json")]
    stage_root = f"{_JOURNAL_DIR}/{stem}"
    staged: list[tuple[str, dict]] = []
    for step in body.get("steps", []):
        relative = step.get("stage")
        if relative is None:
            continue
        if not isinstance(relative, str):
            raise _RecoveryRequired("journal stage path is invalid")
        prefix = f"{stage_root}/stage/"
        tail = relative[len(prefix):] if relative.startswith(prefix) else ""
        if not tail or "/" in tail or "\\" in tail or not tail.endswith(".bin"):
            raise _RecoveryRequired("journal stage path is outside its bound stage directory")
        staged.append((relative, step))
    # Verify the journal file itself before deleting any staged bytes: a
    # replaced/changed journal must leave the stage intact for review.
    info = _lstat(journal_path)
    if (info is None or stat.S_ISLNK(info.st_mode) or _is_reparse(info)
            or not stat.S_ISREG(info.st_mode) or info.st_nlink > 1):
        raise _RecoveryRequired("the journal file is not a regular owned file")
    current = _read_journal(journal_path)
    if (current.get("journal_id") != body.get("journal_id")
            or _canonical(current) != _canonical(body)):
        raise _RecoveryRequired("the journal changed before cleanup")
    for relative, step in staged:
        target = _safe_target(root, relative, "staged payload")
        info = _lstat(target)
        if info is None:
            continue
        if info.st_nlink > 1:
            raise _RecoveryRequired("a staged payload is a hardlink alias")
        data = _read_bytes(target, MAX_REGISTER_BYTES, "staged payload")
        if data is None or _sha(data) != step.get("post_sha256") or len(data) != step.get("bytes"):
            raise _RecoveryRequired("a staged payload changed before cleanup")
        if guard is not None:
            _require_lease(guard)
        os.unlink(target)
    for relative in (f"{stage_root}/stage", stage_root):
        path = root / relative
        info = _lstat(path)
        if info is None:
            continue
        if (stat.S_ISLNK(info.st_mode) or _is_reparse(info)
                or not stat.S_ISDIR(info.st_mode)):
            raise _RecoveryRequired("a journal stage directory is not a real directory")
        if guard is not None:
            _require_lease(guard)
        try:
            path.rmdir()
        except OSError:
            raise _RecoveryRequired("a journal stage directory is not empty") from None
    if guard is not None:
        _require_lease(guard)
    os.unlink(journal_path)


# ---------------------------------------------------------------------------
# Preparation (plan) for record ingest
# ---------------------------------------------------------------------------


def _read_register(root: Path) -> tuple[dict, list[dict], bytes | None]:
    document, raw = _read_json_file(root, _REGISTER, MAX_REGISTER_BYTES, "register")
    if document is None:
        return {"document": {"meetings": []}, "existed": False}, [], None
    entries = _register_entries(document)
    return {"document": document, "existed": True}, entries, raw


def _resolve_preserved_routing(existing: dict) -> dict | None:
    status = existing.get("classification_status")
    topic = existing.get("topic_slug")
    project = existing.get("project_slug")
    if status != "classified" or not (topic or project):
        return None
    return {
        "kind": "topic" if topic else "project",
        "owner": topic or project,
        "topic_slug": topic,
        "project_slug": project,
        "event_slug": existing.get("event_slug"),
        "classification_status": status,
        "classification_basis": existing.get("classification_basis", []),
        "preserved": True,
        "preserve_paths": True,
    }


def _plan_document(record: dict, routing: dict, paths: dict, payload: dict[str, bytes],
                   ownership: dict, existed: bool, entry_count: int,
                   *, action: str, changed: bool,
                   cleanup: list[dict] | None = None,
                   local_sources: list[dict] | None = None) -> dict:
    files = []
    for relative, data in sorted(payload.items()):
        owned = _ownership_entry(ownership, relative)
        files.append({"path": relative, "sha256": _sha(data), "bytes": len(data),
                      "exists": _lstat(Path(record["_workspace"]) / relative) is not None,
                      "owned_sha256": (owned or {}).get("sha256")})
    return {
        "schema": PLAN_SCHEMA,
        "writer_version": WRITER_VERSION,
        "record_schema": RECORD_SCHEMA,
        "action": action,
        "meeting_id": record["meeting_id"],
        "source_system": record["source"]["system"],
        "routing": {key: routing.get(key) for key in
                    ("kind", "topic_slug", "project_slug", "event_slug")},
        "paths": {"summary_path": paths["summary_path"],
                  "transcript_path": paths["transcript_path"],
                  "transcript_raw_path": paths["transcript_raw_path"]},
        "register": {"path": _REGISTER, "existed": existed, "entry_count": entry_count},
        "files": files,
        "delete_after_register": sorted(step["path"] for step in (cleanup or [])),
        "local_sources": [
            {"path": item.get("path"), "kind": item.get("kind")}
            for item in (local_sources or [])],
        "changed": changed,
    }


def _prepare(root: Path, record: dict, *, topic: str | None, project: str | None,
             event: str | None, now: str,
             local_sources: list[dict] | None = None,
             identity_derived: bool = False,
             local_hashes: list[str] | None = None) -> dict:
    routing = _resolve_routing(root, topic, project, event)
    register, entries, register_raw = _read_register(root)
    ownership = _ownership_document(
        _read_json_file(root, _OWNERSHIP, MAX_REGISTER_BYTES, "ownership record")[0])
    index, ambiguous = _match_entries(entries, record)
    if ambiguous:
        raise _Reject("the meeting identity matches more than one register entry")
    existing = entries[index] if index is not None else None
    if existing is not None and identity_derived:
        metadata = existing.get("source_metadata")
        previous = metadata.get("source_sha256") if isinstance(metadata, dict) else None
        if not (isinstance(metadata, dict) and metadata.get("local_entry") is True
                and isinstance(previous, list)
                and sorted(str(item) for item in previous) == sorted(local_hashes or [])):
            raise _Reject("the derived local meeting id collides with another meeting; "
                          "pass an explicit --meeting-id")

    if routing["kind"] == "unmapped" and existing is not None:
        preserved = _resolve_preserved_routing(existing)
        if preserved is not None:
            routing = preserved

    effective_id = _effective_identity(existing, record)
    record = dict(record)
    record["meeting_id"] = effective_id

    stem = _file_stem(record)
    expected = _entry_paths(routing, stem, record["artifacts"])
    artifact_fields = {
        "summary_path": "summary_md",
        "transcript_path": "transcript_md",
        "transcript_raw_path": "transcript_raw_md",
    }
    supplied = {key: bool(record["artifacts"][field])
                for key, field in artifact_fields.items()}
    registered = {key: existing.get(key) for key in artifact_fields} if existing else {}
    registered_dirs = {PurePosixPath(value).parent.as_posix()
                       for value in registered.values() if isinstance(value, str)}
    target_dir = _routing_dir(routing)
    explicit_choice = any(value is not None for value in (topic, project, event))
    move_required = bool(existing) and bool(registered_dirs) and any(
        value != target_dir for value in registered_dirs)
    if existing is not None and move_required and explicit_choice:
        incomplete = [key for key in artifact_fields
                      if isinstance(registered.get(key), str) and not supplied[key]]
        if incomplete:
            raise _Reject("an explicit reroute needs a full artifact snapshot; "
                          "omitted registered artifacts would be split")
        paths = expected
    elif existing is not None:
        # Null means "not supplied", never "delete". Keep every registered path
        # and fill only genuinely new supplied artifacts next to the old ones.
        paths = dict(registered)
        base_dir = (next(iter(registered_dirs)) if len(registered_dirs) == 1
                    else target_dir)
        for key in artifact_fields:
            if supplied[key] and not isinstance(paths.get(key), str):
                target = expected.get(key)
                if target:
                    paths[key] = f"{base_dir}/{PurePosixPath(target).name}"
    else:
        paths = expected

    payload = _artifact_payload(record, routing, paths)
    for relative in payload:
        _safe_target(root, relative, "artifact path")

    ownership_record = dict(record)
    ownership_record["_workspace"] = str(root)
    file_steps, files_changed = _file_steps(root, payload, ownership)
    entry = _project_entry(record, existing, routing, paths, changed=files_changed, now=now, effective_id=effective_id)

    entry_changed = existing is None or entry != existing
    cleanup_steps: list[dict] = []
    drop: set[str] = set()
    if existing is not None and move_required and explicit_choice:
        new_paths = {value for value in paths.values() if value}
        for key in artifact_fields:
            old = registered.get(key)
            if not isinstance(old, str) or old in new_paths:
                continue
            old_path = root / old
            _safe_target(root, old, f"previous {key}")
            if _lstat(old_path) is None:
                continue
            owner = _ownership_entry(ownership, old)
            current = _read_bytes(old_path, MAX_REGISTER_BYTES, "previous artifact")
            if not owner or not isinstance(owner.get("sha256"), str) or _sha(current) != owner["sha256"]:
                raise _Reject("a moved legacy artifact is not provably writer-owned")
            cleanup_steps.append({"kind": "delete", "path": old, "phase": "cleanup",
                                  "stage": None, "pre_sha256": owner["sha256"],
                                  "post_sha256": None, "bytes": 0})
            drop.add(old)

    published = {step["path"] for step in file_steps}
    registered_paths = {
        value for entry in entries if entry is not existing for key in
        ("summary_path", "transcript_path", "transcript_raw_path")
        for value in [entry.get(key)] if isinstance(value, str)}
    kind_paths = {"summary": paths["summary_path"], "transcript": paths["transcript_path"],
                  "transcript_raw": paths["transcript_raw_path"]}
    for source in local_sources or []:
        relative = source.get("path")
        target = source.get("target") or kind_paths.get(source.get("kind"))
        if not isinstance(relative, str):
            continue
        if relative in registered_paths and relative not in payload:
            raise _Reject("a local source is already a registered artifact of another meeting")
        if relative in payload or target not in published:
            continue
        _safe_target(root, relative, "local source")
        current = _read_bytes(root / relative, MAX_REGISTER_BYTES, "local source")
        if current is None or _sha(current) != source.get("sha256"):
            raise _Reject("a local source file changed before publication")
        cleanup_steps.append({"kind": "delete", "path": relative, "phase": "cleanup",
                              "stage": None, "pre_sha256": source["sha256"],
                              "post_sha256": None, "bytes": 0})

    updated_ownership = _new_ownership(
        ownership, payload, record, drop=drop,
        claimable={relative for relative in payload
                   if _lstat(root / relative) is None
                   or _ownership_entry(ownership, relative) is not None})
    ownership_step, ownership_changed = _ownership_step(root, ownership, updated_ownership)

    register_bytes: bytes | None = None
    register_changed = False
    if entry_changed:
        document = dict(register["document"])
        updated = list(entries)
        if index is None:
            updated.append(entry)
        else:
            updated[index] = entry
        document["meetings"] = updated
        register_bytes = _json_text(document).encode("utf-8")
        register_changed = True
    else:
        register_bytes = register_raw
    register_step, register_step_changed = _register_step(
        root, register["existed"], register_bytes, register_raw)
    if not entry_changed:
        register_step, register_step_changed = None, False

    steps = file_steps
    if ownership_step is not None:
        steps.append(ownership_step)
    if register_step is not None:
        steps.append(register_step)
    # The register is published before old-location cleanup so that it never
    # points at already-deleted files; cleanup remains journalled and recoverable.
    steps.extend(cleanup_steps)
    changed = bool(file_steps or cleanup_steps or ownership_changed or register_step_changed
                   or entry_changed)
    plan = _plan_document(record if "_workspace" in record else ownership_record, routing, paths,
                          payload, ownership, register["existed"], len(entries),
                          action="ingest_record", changed=changed,
                          cleanup=cleanup_steps, local_sources=local_sources)
    return {"plan": plan, "routing": routing, "entry": entry, "payload": payload,
            "register_bytes": register_bytes, "ownership": updated_ownership,
            "steps": steps, "changed": changed, "effective_id": effective_id}


# ---------------------------------------------------------------------------
# Envelopes and public API
# ---------------------------------------------------------------------------


def _empty_envelope(success: bool, state: str, message: str, data: dict | None = None,
                    error: dict | None = None) -> dict:
    return {"action": ACTION, "success": success, "state": state, "message": message[:1000],
            "data": data or {}, "error": error}


def _recovery_envelope(message: str, data: dict | None = None) -> dict:
    return _empty_envelope(False, "RecoveryRequired", message, data,
                           {"type": "RecoveryRequired", "message": message[:1000]})


def _success_envelope(state: str, message: str, data: dict) -> dict:
    return _empty_envelope(True, state, message, data)


def _ingest_normalized(normalized: dict, *, workspace_root, lease_id, conversation_id=None,
                       trusted_workspace_lock_root=None, topic_slug=None, project_slug=None,
                       event_slug=None, dry_run=False, apply_plan_sha256=None,
                       local_sources=None, identity_derived=False,
                       local_hashes=None) -> dict:
    try:
        workspace = _workspace_root(workspace_root)
        if dry_run and apply_plan_sha256 is not None:
            raise _Reject("--dry-run cannot be combined with --apply-plan")
        manual = normalized["source"]["system"] == "manual"
        if manual and not dry_run and apply_plan_sha256 is None:
            dry_run = True
        if not dry_run:
            _require_quiet_journals(workspace)
        plan_context = _prepare(workspace, normalized, topic=topic_slug,
                                project=project_slug, event=event_slug, now=_iso_now(),
                                local_sources=local_sources,
                                identity_derived=identity_derived,
                                local_hashes=local_hashes)
        plan = plan_context["plan"]
        plan_sha = _sha(_canonical(plan))
        effective_id = plan_context["effective_id"]
        if dry_run:
            data = {"meeting_id": effective_id, "plan": plan,
                    "plan_sha256": plan_sha, "changed": plan_context["changed"],
                    "dry_run": True, "apply_required": manual}
            state = "Plan" if manual else "DryRun"
            message = ("Manual ingest produced a plan; apply needs --apply-plan with this hash."
                       if manual else "Dry run: no workspace mutation.")
            return _success_envelope(state, message, data)
        if apply_plan_sha256 is not None and apply_plan_sha256 != plan_sha:
            raise _Reject("the apply plan hash does not match the current workspace state")
        if not plan_context["changed"]:
            return _success_envelope(
                "Unchanged", "The meeting is already registered with identical content.",
                {"meeting_id": effective_id, "entry": plan_context["entry"],
                 "paths": plan["paths"], "changed": False, "plan_sha256": plan_sha})

        guard_module = _load_guard(_trusted_lock_root(trusted_workspace_lock_root))
        guard = _Guard(guard_module, workspace, lease_id, conversation_id)
        _require_lease(guard)
        current = _prepare(workspace, normalized, topic=topic_slug, project=project_slug,
                           event=event_slug, now=_iso_now(), local_sources=local_sources,
                           identity_derived=identity_derived, local_hashes=local_hashes)
        if not current["changed"]:
            return _success_envelope(
                "Unchanged", "The meeting is already registered with identical content.",
                {"meeting_id": current["effective_id"], "entry": current["entry"],
                 "paths": current["plan"]["paths"], "changed": False})
        applied_plan_sha = _sha(_canonical(current["plan"]))
        if apply_plan_sha256 is not None and apply_plan_sha256 != applied_plan_sha:
            raise _Reject("the apply plan hash no longer matches the workspace state")
        publication_record = dict(normalized)
        publication_record["meeting_id"] = current["effective_id"]
        publication = _apply(workspace, publication_record, current["payload"],
                             current["ownership"], current["register_bytes"],
                             current["steps"], guard, action="ingest_record")
        return _success_envelope(
            "Applied", "Meeting record published.",
            {"meeting_id": current["effective_id"], "entry": current["entry"],
             "paths": current["plan"]["paths"], "changed": True,
             "plan_sha256": applied_plan_sha, **publication})
    except _RecoveryRequired as exc:
        return _recovery_envelope(str(exc) or "recovery is required")
    except _Reject as exc:
        return _empty_envelope(False, "Rejected", str(exc),
                               error={"type": "Rejected", "message": str(exc)})
    except (OSError, UnicodeError, ValueError):
        return _empty_envelope(False, "Rejected",
                               "input, catalog, lock or target validation failed",
                               error={"type": "Rejected",
                                      "message": "input, catalog, lock or target validation failed"})
    except Exception:
        return _empty_envelope(False, "Rejected", "writer failure",
                               error={"type": "Rejected", "message": "writer failure"})


def ingest_record(record, *, workspace_root, lease_id, conversation_id=None,
                  trusted_workspace_lock_root=None, topic_slug=None, project_slug=None,
                  event_slug=None, dry_run=False, apply_plan_sha256=None) -> dict:
    """Validate and publish one meeting record; returns the six-field envelope."""
    try:
        normalized = validate_record(record)
    except _Reject as exc:
        return _empty_envelope(False, "Rejected", str(exc),
                               error={"type": "Rejected", "message": str(exc)})
    return _ingest_normalized(
        normalized, workspace_root=workspace_root, lease_id=lease_id,
        conversation_id=conversation_id,
        trusted_workspace_lock_root=trusted_workspace_lock_root,
        topic_slug=topic_slug, project_slug=project_slug, event_slug=event_slug,
        dry_run=dry_run, apply_plan_sha256=apply_plan_sha256)


def _reserved_local_source(relative: str) -> bool:
    if relative in {_REGISTER, _OWNERSHIP,
                    "memory/references/topics/topics.json",
                    "memory/references/projects/projects.json",
                    "memory/references/mail-desk/mail-desk.json"}:
        return True
    return relative == _JOURNAL_DIR or relative.startswith(_JOURNAL_DIR + "/")


def _source_text(root: Path, value: str, label: str,
                 rewrite_frontmatter: bool) -> tuple[str, str, str]:
    """Read one local source file inside a canonical zone.

    Returns ``(workspace_relative_path, text, original_sha256)``. The lexical
    path is validated before any resolution: a child symlink/junction/reparse
    point or hardlink is rejected so a later move cannot delete the real file
    through an alias. The workspace root itself is the trusted anchor and may be
    an alias. Bytes are decoded strictly as UTF-8; without
    ``rewrite_frontmatter`` the text is kept byte-exact (line endings included)
    and any existing frontmatter is body.
    """
    if not isinstance(value, str) or not value:
        raise _Reject(f"{label} path is required")
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    lexical = Path(os.path.abspath(path))
    if not lexical.is_relative_to(root):
        raise _Reject(f"{label} is outside the workspace")
    relative = lexical.relative_to(root).as_posix()
    if _reserved_local_source(relative):
        raise _Reject(f"{label} names reserved writer state or catalog data")
    _safe_target(root, relative, label)
    raw = _read_bytes(root / relative, MAX_RECORD_BYTES, label)
    if raw is None:
        raise _Reject(f"{label} does not exist")
    original_sha = _sha(raw)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _Reject(f"{label} is not valid UTF-8") from None
    if rewrite_frontmatter:
        stripped = re.sub(r"\A\ufeff?---\r?\n.*?\r?\n---[ \t]*(?:\r?\n)?", "", text, count=1,
                          flags=re.S)
        text = stripped
    return relative, text, original_sha


def _local_title(paths: list[str], explicit: str | None) -> str:
    if explicit:
        return explicit
    for value in paths:
        stem = Path(value).stem
        for suffix in (".transcript.raw", ".transcript", ".summary", ".raw"):
            if stem.endswith(suffix):
                stem = stem[: -len(suffix)]
        if stem:
            return stem
    return "Local meeting"


def _local_record(*, workspace_root: Path, summary_path: str | None,
                  transcript_path: str | None, raw_path: str | None,
                  calendar_date: str | None, start_utc: str | None,
                  duration_minutes: float | None, participants: list[str] | None,
                  title: str | None, meeting_id: str | None,
                  rewrite_frontmatter: bool) -> tuple[dict, list[dict]]:
    summary = transcript = raw = None
    sources: list[dict] = []
    if summary_path:
        relative, summary, digest = _source_text(workspace_root, summary_path, "summary",
                                                 rewrite_frontmatter)
        sources.append({"path": relative, "sha256": digest, "kind": "summary"})
    if transcript_path:
        relative, transcript, digest = _source_text(workspace_root, transcript_path,
                                                    "transcript", rewrite_frontmatter)
        sources.append({"path": relative, "sha256": digest, "kind": "transcript"})
    if raw_path:
        relative, raw, digest = _source_text(workspace_root, raw_path, "raw",
                                             rewrite_frontmatter=False)
        sources.append({"path": relative, "sha256": digest, "kind": "transcript_raw"})
    elif transcript:
        raw = transcript
    if not (summary or "").strip() and not (transcript or "").strip():
        raise _Reject("--local needs a non-empty --summary or --transcript")
    if transcript and not raw:
        raise _Reject("a local transcript needs --raw unless the transcript itself is the raw text")

    normalized_utc, _millis = _utc(start_utc)
    calendar = _calendar_date(calendar_date)
    if normalized_utc is not None and calendar is None:
        calendar = datetime.fromisoformat(
            normalized_utc.replace("Z", "+00:00")).date().isoformat()
    estimated = normalized_utc is None
    if estimated:
        basis = ["date-only", "local-file"] if calendar else ["unknown", "local-file"]
    else:
        basis = []
    duration = _duration(duration_minutes)
    raw_names: list[str] = []
    for value in participants or []:
        raw_names.extend(part.strip() for part in value.split(",") if part.strip())
    names = _string_list(raw_names, "local participants", limit=500, item_limit=300)
    display = _text(_local_title([value for value in (summary_path, transcript_path) if value],
                                 title), "local title", limit=500)
    if meeting_id is not None:
        identity = _identity(meeting_id, "local meeting_id")
    else:
        day = calendar or "undated"
        identity = f"manual-{day}-{_title_slug(display)}"[:200]
        identity = _identity(identity, "local meeting_id")
    record = {
        "schema": RECORD_SCHEMA,
        "source": {"system": "manual", "account_ref": None, "external_id": identity},
        "meeting_id": identity,
        "title": display,
        "start": {"utc": normalized_utc, "calendar_date": calendar,
                  "estimated": estimated, "basis": basis},
        "duration_minutes": duration,
        "participants": names,
        "artifacts": {"summary_md": summary, "transcript_md": transcript,
                      "transcript_raw_md": raw},
        "classification_hint": {"topic_slug": None, "project_slug": None,
                                "event_slug": None, "basis": []},
        "review": {"recommended": True, "reason": "local-manual-import"},
        "source_metadata": {"local_entry": True,
                            "source_sha256": sorted(item["sha256"] for item in sources)},
    }
    return validate_record(record), sources


def ingest_local(*, workspace_root, lease_id, conversation_id=None,
                 trusted_workspace_lock_root=None, summary_path=None, transcript_path=None,
                 raw_path=None, calendar_date=None, start_utc=None,
                 duration_minutes=None, participants=None, title=None, meeting_id=None,
                 topic_slug=None, project_slug=None, event_slug=None, dry_run=False,
                 apply_plan_sha256=None, rewrite_frontmatter=False) -> dict:
    """Second CLI entry: local files become the same Record-v1 and writer.

    Plan/dry-run is the default. Apply needs the matching plan hash plus an owned
    lease. Source files are moved only after the register points at the
    published artifact, via journal-bound hash-checked deletes.
    """
    try:
        root = _workspace_root(workspace_root)
        normalized, sources = _local_record(
            workspace_root=root, summary_path=summary_path,
            transcript_path=transcript_path, raw_path=raw_path,
            calendar_date=calendar_date, start_utc=start_utc,
            duration_minutes=duration_minutes, participants=participants,
            title=title, meeting_id=meeting_id,
            rewrite_frontmatter=rewrite_frontmatter)
    except _Reject as exc:
        return _empty_envelope(False, "Rejected", str(exc),
                               error={"type": "Rejected", "message": str(exc)})
    env = _ingest_normalized(
        normalized, workspace_root=root, lease_id=lease_id,
        conversation_id=conversation_id,
        trusted_workspace_lock_root=trusted_workspace_lock_root,
        topic_slug=topic_slug, project_slug=project_slug, event_slug=event_slug,
        dry_run=dry_run, apply_plan_sha256=apply_plan_sha256,
        local_sources=sources, identity_derived=meeting_id is None,
        local_hashes=sorted(item["sha256"] for item in sources))
    return env


def publish_registered_transcript(content, *, workspace_root, lease_id, meeting_id,
                                  conversation_id=None, trusted_workspace_lock_root=None,
                                  dry_run=False) -> dict:
    """Publish builder output only onto the registered transcript path."""
    try:
        workspace = _workspace_root(workspace_root)
        if not isinstance(meeting_id, str) or not meeting_id:
            raise _Reject("a meeting id is required to publish a transcript")
        if not isinstance(content, str):
            raise _Reject("transcript content must be text")
        if not content.strip():
            raise _Reject("transcript content must not be empty")
        data = content.encode("utf-8")
        context = _transcript_plan(workspace, meeting_id, data)
        plan, plan_sha = context["plan"], _sha(_canonical(context["plan"]))
        if dry_run:
            return _success_envelope("DryRun", "Dry run: no workspace mutation.",
                                     {"meeting_id": meeting_id, "plan": plan,
                                      "plan_sha256": plan_sha, "changed": context["changed"]})
        if not context["changed"]:
            return _success_envelope("Unchanged", "The registered transcript is unchanged.",
                                     {"meeting_id": meeting_id, "paths": plan["paths"],
                                      "changed": False, "plan_sha256": plan_sha})
        _require_quiet_journals(workspace)
        guard_module = _load_guard(_trusted_lock_root(trusted_workspace_lock_root))
        guard = _Guard(guard_module, workspace, lease_id, conversation_id)
        _require_lease(guard)
        current = _transcript_plan(workspace, meeting_id, data)
        if not current["changed"]:
            return _success_envelope("Unchanged", "The registered transcript is unchanged.",
                                     {"meeting_id": meeting_id,
                                      "paths": current["plan"]["paths"], "changed": False})
        record = {"meeting_id": meeting_id, "_workspace": str(workspace),
                  "_register_existed": True}
        publication = _apply(workspace, record, current["payload"], current["ownership"],
                             current["register_bytes"], current["steps"], guard,
                             action="publish_transcript")
        return _success_envelope("Applied", "Registered transcript updated.",
                                 {"meeting_id": meeting_id, "entry": current["entry"],
                                  "paths": current["plan"]["paths"], "changed": True,
                                  "plan_sha256": plan_sha, **publication})
    except _RecoveryRequired as exc:
        return _recovery_envelope(str(exc) or "recovery is required")
    except _Reject as exc:
        return _empty_envelope(False, "Rejected", str(exc),
                               error={"type": "Rejected", "message": str(exc)})
    except (OSError, UnicodeError, ValueError):
        return _empty_envelope(False, "Rejected",
                               "input, catalog, lock or target validation failed",
                               error={"type": "Rejected",
                                      "message": "input, catalog, lock or target validation failed"})
    except Exception:
        return _empty_envelope(False, "Rejected", "writer failure",
                               error={"type": "Rejected", "message": "writer failure"})


def _transcript_plan(root: Path, meeting_id: str, data: bytes) -> dict:
    register, entries, register_raw = _read_register(root)
    matches = [entry for entry in entries if meeting_id in _entry_identities(entry)[0]]
    if len(matches) != 1:
        raise _Reject("the meeting id does not identify exactly one registered meeting")
    existing = matches[0]
    relative = existing.get("transcript_path")
    if not isinstance(relative, str) or not relative:
        raise _Reject("the registered meeting has no transcript_path")
    if not relative.startswith("memory/evidence/"):
        raise _Reject("the registered transcript_path is outside the evidence zone")
    _safe_target(root, relative, "registered transcript_path")
    ownership = _ownership_document(
        _read_json_file(root, _OWNERSHIP, MAX_REGISTER_BYTES, "ownership record")[0])
    target = root / relative
    owner = _ownership_entry(ownership, relative)
    exists = _lstat(target) is not None
    current_bytes: bytes | None = None
    if exists:
        current_bytes = _read_bytes(target, MAX_REGISTER_BYTES, "registered transcript")
        if (owner is None or not isinstance(owner.get("sha256"), str)
                or _sha(current_bytes) != owner["sha256"]):
            raise _Reject("the registered transcript is not provably writer-owned; refusing "
                          "orphan publication")
    digest = _sha(data)
    changed = not _same_bytes(target, data)
    entry = dict(existing)
    entry_changed = False
    if changed:
        entry["content_status"] = "provisional"
        entry["review_recommended"] = True
        reasons = [existing.get("review_reason"), "content-changed"]
        entry["review_reason"] = "; ".join(dict.fromkeys(item for item in reasons if item))
        entry["last_synced_at"] = _iso_now()
        entry_changed = True
    document = dict(register["document"])
    document["meetings"] = [entry if item is existing else item for item in entries]
    register_bytes = _json_text(document).encode("utf-8") if entry_changed else register_raw
    updated_ownership = _new_ownership(ownership, {relative: data},
                                       {"meeting_id": meeting_id}, drop=set())
    ownership_step, ownership_changed = _ownership_step(root, ownership, updated_ownership)

    steps: list[dict] = []
    if changed:
        steps.append({"kind": "create" if not exists else "replace", "path": relative,
                      "phase": "files", "stage": None,
                      "pre_sha256": None if current_bytes is None else _sha(current_bytes),
                      "post_sha256": digest, "bytes": len(data)})
    if ownership_step is not None:
        steps.append(ownership_step)
    register_step, register_step_changed = (
        _register_step(root, register["existed"], register_bytes, register_raw)
        if entry_changed else (None, False))
    if register_step is not None:
        steps.append(register_step)
    changed = changed or ownership_changed or register_step_changed
    plan = {"schema": PLAN_SCHEMA, "writer_version": WRITER_VERSION,
            "record_schema": RECORD_SCHEMA, "action": "publish_transcript",
            "meeting_id": meeting_id, "paths": {"transcript_path": relative},
            "register": {"path": _REGISTER, "existed": register["existed"]},
            "files": [{"path": relative, "sha256": digest, "bytes": len(data),
                       "exists": exists,
                       "owned_sha256": (owner or {}).get("sha256")}],
            "changed": changed}
    return {"plan": plan, "entry": entry, "payload": {relative: data},
            "register_bytes": register_bytes, "ownership": updated_ownership,
            "steps": steps, "changed": changed}


# ---------------------------------------------------------------------------
# Inspect and recovery
# ---------------------------------------------------------------------------


def _classify_step(root: Path, step: dict) -> str:
    try:
        target = _safe_target(root, step["path"], "journal target")
    except _Reject:
        return "foreign"
    info = _lstat(target)
    if step["kind"] == "delete":
        if info is None:
            return "post"
        current = _read_bytes(target, MAX_REGISTER_BYTES, "journal target")
        return "pre" if current is not None and _sha(current) == step["pre_sha256"] else "foreign"
    if info is None:
        return "pre" if step["kind"] == "create" else "missing"
    current = _read_bytes(target, MAX_REGISTER_BYTES, "journal target")
    if current is None:
        return "missing"
    digest = _sha(current)
    if digest == step["post_sha256"]:
        return "post"
    if step["kind"] == "replace" and digest == step["pre_sha256"]:
        return "pre"
    return "foreign"


def inspect_workspace(workspace_root) -> dict:
    """Read-only inspection of journals, register and ownership state."""
    try:
        root = _workspace_root(workspace_root)
        journal_files, unexpected = _journal_state(root)
        journals = []
        for path in journal_files:
            try:
                body = _read_journal(path)
                steps = []
                for step in body["steps"]:
                    try:
                        state = _classify_step(root, step)
                    except _Reject:
                        state = "corrupt"
                    steps.append({"path": step.get("path"), "kind": step.get("kind"),
                                  "state": state})
                journals.append({"journal": path.name, "state": body.get("state"),
                                 "action": body.get("action"),
                                 "meeting_id": body.get("meeting_id"),
                                 "created_at": body.get("created_at"), "steps": steps})
            except _RecoveryRequired:
                journals.append({"journal": path.name, "state": "corrupt", "steps": []})
        register_state = "missing"
        register_entries = 0
        try:
            document, _raw = _read_json_file(root, _REGISTER, MAX_REGISTER_BYTES, "register")
            if document is not None:
                entries = _register_entries(document)
                register_state = "object"
                register_entries = len(entries)
        except _Reject as exc:
            register_state = "legacy-array" if LEGACY_ARRAY_REASON in str(exc) else "invalid"
        ownership_state = "missing"
        try:
            raw = _read_json_file(root, _OWNERSHIP, MAX_REGISTER_BYTES, "ownership record")[0]
            if raw is not None:
                _ownership_document(raw)
                ownership_state = "valid"
        except _Reject:
            ownership_state = "invalid"
        return _success_envelope("Inspected", "Read-only writer state inspection.",
                                 {"journals": journals,
                                  "unexpected_journal_entries": unexpected,
                                  "recovery_required": bool(journals or unexpected),
                                  "register": {"path": _REGISTER, "state": register_state,
                                               "entries": register_entries},
                                  "ownership": {"path": _OWNERSHIP, "state": ownership_state}})
    except _Reject as exc:
        return _empty_envelope(False, "Rejected", str(exc),
                               error={"type": "Rejected", "message": str(exc)})
    except _RecoveryRequired as exc:
        return _recovery_envelope(str(exc) or "inspection requires recovery review")
    except (OSError, ValueError):
        return _empty_envelope(False, "Rejected", "inspection failed",
                               error={"type": "Rejected", "message": "inspection failed"})


def recover_workspace(workspace_root, *, lease_id, conversation_id=None,
                      trusted_workspace_lock_root=None) -> dict:
    """Explicit forward-only recovery of an interrupted publication."""
    try:
        root = _workspace_root(workspace_root)
    except _Reject as exc:
        return _empty_envelope(False, "Rejected", str(exc),
                               error={"type": "Rejected", "message": str(exc)})
    try:
        journals, unexpected = _journal_state(root)
        if unexpected:
            return _recovery_envelope(
                "unverifiable entries in the writer journal directory; recovery stops "
                "without mutation and preserves every byte",
                {"unexpected_journal_entries": unexpected,
                 "journals": [path.name for path in journals]})
        if not journals:
            return _success_envelope("Clean", "No open journal requires recovery.",
                                     {"recovered": False, "journals": []})
        if len(journals) > 1:
            return _recovery_envelope("more than one journal is open; manual review is required",
                                      {"journals": [path.name for path in journals]})
        guard_module = _load_guard(_trusted_lock_root(trusted_workspace_lock_root))
        guard = _Guard(guard_module, root, lease_id, conversation_id)
        _require_lease(guard)
        path = journals[0]
        body = _read_journal(path)
        if body.get("workspace_fingerprint") != _workspace_fingerprint(root):
            return _recovery_envelope("the journal belongs to a different workspace")
        for step in body["steps"]:
            state = _classify_step(root, step)
            if state in ("foreign", "missing"):
                return _recovery_envelope(
                    "a journal target changed outside the writer; recovery stops without mutation",
                    {"journal": path.name, "step": step.get("path")})
        _require_lease(guard)
        _persist_journal(path, body)
        for step in body["steps"]:
            state = _classify_step(root, step)
            if state == "post":
                step["state"] = "published" if step["kind"] != "delete" else "deleted"
                continue
            _require_lease(guard)
            _publish_one(root, step)
            step["state"] = "published" if step["kind"] != "delete" else "deleted"
            _require_lease(guard)
            _persist_journal(path, body)
        body["state"] = "completed"
        _require_lease(guard)
        _persist_journal(path, body)
        _require_lease(guard)
        _finalize_journal(root, path, body, guard)
        return _success_envelope("Recovered", "Interrupted publication resumed and finalized.",
                                 {"recovered": True, "journal": path.name})
    except _RecoveryRequired as exc:
        return _recovery_envelope(str(exc) or "recovery is required")
    except _LeaseRejected as exc:
        return _empty_envelope(False, "Rejected", str(exc),
                               error={"type": "Rejected", "message": str(exc)})
    except _Reject:
        return _recovery_envelope(
            "recovery stopped on an unsafe or unverifiable workspace path; no further "
            "mutation was performed")
    except (OSError, ValueError):
        return _recovery_envelope("recovery failed; the journal remains open")


# ---------------------------------------------------------------------------
# Capabilities and CLI
# ---------------------------------------------------------------------------


def capabilities() -> dict:
    return {
        "record_schema": RECORD_SCHEMA,
        "writer_version": WRITER_VERSION,
        "actions": ["ingest_record", "ingest_local", "publish_registered_transcript",
                    "inspect", "recover"],
        "record_root_fields": sorted(_RECORD_KEYS),
        "source_systems": sorted(_SYSTEMS),
        "exit_codes": {"0": "success", "2": "rejected", "3": "recovery_required"},
        "limits": {
            "record_bytes": MAX_RECORD_BYTES,
            "register_bytes": MAX_REGISTER_BYTES,
            "json_depth": MAX_JSON_DEPTH,
            "json_nodes": MAX_JSON_NODES,
            "artifact_chars": MAX_ARTIFACT_CHARS,
            "source_metadata_bytes": MAX_METADATA_BYTES,
            "register_entries": MAX_ENTRIES,
            "identity_chars": _MAX_IDENTITY_CHARS,
        },
        "register": {"path": _REGISTER, "root": "object", "list_field": "meetings"},
        "journal": {"path": _JOURNAL_DIR, "ownership": _OWNERSHIP,
                    "register_last": True, "recovery": "explicit-forward-only"},
        "routing": {"operator_flags": ["--topic", "--project", "--event"],
                    "basis": "operator-choice", "unmapped": _UNROUTED},
    }


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _Reject(message)


def _read_record_argument(value: str) -> str:
    if value == "-":
        raw = sys.stdin.buffer.read(MAX_RECORD_BYTES + 1)
        if len(raw) > MAX_RECORD_BYTES:
            raise _Reject("record input exceeds the supported size")
    else:
        path = Path(value)
        try:
            info = path.lstat()
        except OSError:
            raise _Reject("record file cannot be read") from None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_size > MAX_RECORD_BYTES:
            raise _Reject("record file must be a regular file within the size limit")
        raw = path.read_bytes()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _Reject("record input is not valid UTF-8") from None


def _run(argv: list[str] | None = None) -> tuple[dict, int]:
    parser = _Parser(description="Canonical meeting-desk record writer.")
    parser.add_argument("--capabilities", action="store_true")
    parser.add_argument("--record")
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--summary")
    parser.add_argument("--transcript")
    parser.add_argument("--raw")
    parser.add_argument("--date")
    parser.add_argument("--start-utc")
    parser.add_argument("--duration", type=float)
    parser.add_argument("--participants", action="append")
    parser.add_argument("--title")
    parser.add_argument("--meeting-id")
    parser.add_argument("--rewrite-frontmatter", action="store_true")
    parser.add_argument("--workspace")
    parser.add_argument("--lease-id")
    parser.add_argument("--conversation-id")
    parser.add_argument("--workspace-lock-root")
    parser.add_argument("--topic")
    parser.add_argument("--project")
    parser.add_argument("--event")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply-plan")
    parser.add_argument("--inspect", action="store_true")
    parser.add_argument("--recover", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.capabilities:
        if any((args.record, args.local, args.summary, args.transcript, args.raw,
                args.workspace, args.lease_id, args.inspect, args.recover,
                args.topic, args.project, args.event, args.apply_plan)):
            raise _Reject("--capabilities cannot be combined with workspace operations")
        return _success_envelope("Available", "Writer capabilities.", capabilities()), 0
    if args.inspect and args.recover:
        raise _Reject("--inspect and --recover are mutually exclusive")
    if (args.inspect or args.recover) and (args.record or args.local):
        raise _Reject("--record/--local cannot be combined with --inspect or --recover")
    if args.local and args.record:
        raise _Reject("--local and --record are mutually exclusive")
    if not args.workspace:
        raise _Reject("--workspace is required")
    if args.inspect:
        envelope = inspect_workspace(args.workspace)
        return envelope, _exit_code(envelope)
    if args.recover:
        if not args.lease_id:
            raise _Reject("--recover requires an owned --lease-id")
        envelope = recover_workspace(
            args.workspace, lease_id=args.lease_id, conversation_id=args.conversation_id,
            trusted_workspace_lock_root=args.workspace_lock_root)
        return envelope, _exit_code(envelope)
    if args.local:
        if not args.summary and not args.transcript:
            raise _Reject("--local requires --summary and/or --transcript")
        if args.apply_plan and not args.lease_id:
            raise _Reject("--lease-id is required for a confirmed local apply")
        envelope = ingest_local(
            workspace_root=args.workspace, lease_id=args.lease_id,
            conversation_id=args.conversation_id,
            trusted_workspace_lock_root=args.workspace_lock_root,
            summary_path=args.summary, transcript_path=args.transcript,
            raw_path=args.raw, calendar_date=args.date, start_utc=args.start_utc,
            duration_minutes=args.duration, participants=args.participants,
            title=args.title, meeting_id=args.meeting_id, topic_slug=args.topic,
            project_slug=args.project, event_slug=args.event, dry_run=args.dry_run,
            apply_plan_sha256=args.apply_plan,
            rewrite_frontmatter=args.rewrite_frontmatter)
        return envelope, _exit_code(envelope)
    if not args.record:
        raise _Reject("--record, --local, --inspect, --recover or --capabilities is required")
    record = strict_json(_read_record_argument(args.record))
    manual = (isinstance(record, dict) and isinstance(record.get("source"), dict)
              and record["source"].get("system") == "manual")
    if not args.lease_id and not args.dry_run and not (manual and not args.apply_plan):
        raise _Reject("--lease-id is required for publication")
    envelope = ingest_record(
        record, workspace_root=args.workspace, lease_id=args.lease_id,
        conversation_id=args.conversation_id,
        trusted_workspace_lock_root=args.workspace_lock_root, topic_slug=args.topic,
        project_slug=args.project, event_slug=args.event, dry_run=args.dry_run,
        apply_plan_sha256=args.apply_plan)
    return envelope, _exit_code(envelope)


def _exit_code(envelope: dict) -> int:
    if envelope["success"]:
        return 0
    return 3 if envelope["state"] == "RecoveryRequired" else 2


def _emit(envelope: dict) -> None:
    """Write the one-line envelope as UTF-8 bytes, independent of console codepage.

    A text-only stream (tests with ``StringIO``) still works; real stdout uses
    its byte buffer so cp1252 consoles cannot turn success into a crash.
    """
    text = json.dumps(envelope, ensure_ascii=False, sort_keys=True) + "\n"
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        buffer.write(text.encode("utf-8"))
        buffer.flush()
    else:
        sys.stdout.write(text)
        sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    try:
        envelope, code = _run(argv)
    except _Reject as exc:
        envelope = _empty_envelope(False, "Rejected", str(exc),
                                   error={"type": "Rejected", "message": str(exc)})
        code = 2
    except _RecoveryRequired as exc:
        envelope = _recovery_envelope(str(exc) or "recovery is required")
        code = 3
    except (OSError, UnicodeError, ValueError):
        envelope = _empty_envelope(False, "Rejected", "operation failed",
                                   error={"type": "Rejected", "message": "operation failed"})
        code = 2
    except Exception:
        envelope = _empty_envelope(False, "Rejected", "operation failed",
                                   error={"type": "Rejected", "message": "operation failed"})
        code = 2
    _emit(envelope)
    return code


if __name__ == "__main__":
    sys.exit(main())
