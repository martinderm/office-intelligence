"""Narrow Cloud-Atlas consumer for the mail-desk ``cloud_atlas_refresh_handoff`` (FR-09 / MD-P3).

This adapter receives a declarative, hash-bound ``cloud_atlas_refresh_handoff`` Schema 1
from mail-desk and refreshes *only* the exactly bound Cloud-Atlas storage (filemap and,
for convertible types, the Markdown mirror) through the canonical Cloud-Atlas engine.
It builds no second filemap engine: it reuses
:mod:`gen_filemap` (``resolve_all_sync_configs`` / ``run_generation`` /
``validate_filemap`` / canonical writers) and :mod:`convert_cloud_docs`
(``run_conversion`` / canonical writers) loaded dynamically so both skills keep their
own isolated ``core`` package.

Trust boundaries (read this before relying on the consumer):

* The mail-desk handoff grants **no** Cloud-Atlas authorization.  The consumer verifies
  its own workspace lock (``workspace_lock_guard.require_workspace_lock`` with
  ``allow_legacy=False``) before any mutation; a missing/foreign lock denies the run.
* The handoff hash is recomputed, and the promotion journal is **required** and
  revalidated through the mail-desk ``load_promotion_journal`` binding; a journal-less
  handoff is denied before any storage resolution or engine run.  The real target is
  re-hashed against the bound SHA-256/size.
* Only the bound storage is scanned, and its origin must be a real catalog entry
  (``cloud_sync`` in projects.json/topics.json, or a subtopic's ``cloud_sync``).  The
  generator's synthesized fallback for an uncataloged entity is rejected as
  ``storage_unbound`` before the engine runs, so an uncataloged entity can never reach
  the engine at all.
* Ordering guarantee: every denial up to and including the catalog-origin check happens
  before the engine step and therefore mutates nothing.  A denial *after* the engine ran
  (the post-refresh filemap/entry verification) reports the exact state: the refresh is
  deterministic and verified by re-reading the generated filemap, so this adapter does
  not claim "never mutate" once the engine has written.
* Binary/Office content stays ``untrusted_external``; the canonical conversion/OCR
  rules are untouched and no in-place OCR special case is added.  The consumer never
  touches the mailbox, never re-runs the promotion and never mutates the Cloud-Atlas
  catalogs (the canonical ``last_synced_at`` bookkeeping is suppressed for the bound
  scan, because the MD-P3 authority covers only filemap/mirror).
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import hashlib
import importlib.util
import io
import json
from pathlib import Path, PurePosixPath
import re
import sys
from typing import Any, Mapping

# -- Schema constants (must mirror the mail-desk attachment_promotion MD-P3 surface) --

HANDOFF_KIND = "cloud_atlas_refresh_handoff"
HANDOFF_RECEIVER = "cloud-atlas"
HANDOFF_OPERATION = "refresh_filemap"
HANDOFF_SCHEMA_VERSION = 1

REFRESH_RESULT_KIND = "cloud_atlas_refresh_result"
REFRESH_STATUS_COMPLETED = "refresh_completed"
REFRESH_STATUS_PENDING = "refresh_pending"
REFRESH_STATUS_DENIED = "refresh_denied"

REQUIRED_RECEIVING_STEPS: tuple[str, ...] = (
    "verify_cloud_atlas_lock",
    "revalidate_handoff_hash",
    "revalidate_promotion_journal",
    "reverify_real_target",
    "refresh_bound_storage",
    "verify_filemap_entry",
)
PROHIBITED_AUTOMATIC_STEPS: tuple[str, ...] = (
    "re_run_promotion",
    "mailbox_mutation",
    "catalog_mutation",
    "workspace_wide_scan",
)

_HANDOFF_KEYS: frozenset[str] = frozenset(
    {
        "schema_version",
        "kind",
        "receiver",
        "operation",
        "promotion_id",
        "journal_relative_path",
        "journal_hash",
        "candidate_hash",
        "review_hash",
        "preflight_hash",
        "scope",
        "entity_id",
        "subtopic_id",
        "storage_id",
        "scan_dir",
        "target_relative_path",
        "target_sha256",
        "target_size_bytes",
        "filemap_snapshot_hash",
        "required_receiving_steps",
        "prohibited_automatic_steps",
        "handoff_hash",
    }
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_WIN32_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


class _HandoffRejected(Exception):
    """Internal typed rejection carrying a bounded deny reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ==============================================================================
# Small pure helpers
# ==============================================================================

def _canonical_json_sha256(value: object) -> str:
    canonical = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value.strip().lower()) is not None


def _normalize_slashes(value: Any) -> str:
    return str(value or "").strip().replace("\\", "/")


def _local_wall_clock(current_time: datetime.datetime | None) -> datetime.datetime:
    """Return a naive local wall-clock reading for offset-invariant freshness.

    The canonical generator writes naive local timestamps.  An injected aware clock is
    converted to that same local naive frame before the age computation, so the
    freshness verdict does not depend on which offset the caller used to express the
    instant (and never misreads a local timestamp as UTC).
    """
    if current_time is None:
        return datetime.datetime.now()
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        return current_time
    return current_time.astimezone().replace(tzinfo=None)


def _is_safe_component(name: str) -> bool:
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        return False
    if "\x00" in name or re.search(r'[<>:"|?*]', name):
        return False
    if name.endswith(".") or name.endswith(" "):
        return False
    stem = name.split(".")[0].strip().upper()
    return stem not in _WIN32_RESERVED and name.upper() not in _WIN32_RESERVED


def _is_safe_relative(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return False
    normalized = text.replace("\\", "/")
    if normalized.startswith("/") or _DRIVE_RE.match(normalized):
        return False
    parts = PurePosixPath(normalized).parts
    return bool(parts) and all(_is_safe_component(part) for part in parts)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest().lower()


def _local_path(root: Path, relative: str) -> Path:
    normalized = _normalize_slashes(relative).strip("/")
    if not _is_safe_relative(normalized):
        raise _HandoffRejected("handoff_invalid")
    candidate = root.joinpath(*PurePosixPath(normalized).parts).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise _HandoffRejected("handoff_invalid") from exc
    return candidate


def _target_path(root: Path, scan_dir: str, target_relative_path: str) -> Path:
    combined = f"{_normalize_slashes(scan_dir).strip('/')}/{_normalize_slashes(target_relative_path).strip('/')}"
    return _local_path(root, combined)


def _load_catalog_entries(root: Path, relative: str) -> list[Any] | None:
    """Read one canonical catalog list (projects.json / topics.json), fail-closed."""
    try:
        data = json.loads((root / PurePosixPath(relative)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, list) else None


def _catalog_cloud_sync(root: Path, handoff: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Return the catalog-declared ``cloud_sync`` for the bound entity, or ``None``.

    Proves catalog origin: the bound storage must be declared by an actual
    projects.json entry (project scope) or topics.json topic/subtopic entry (topic
    scope) -- never synthesized by the generator's fallback for an uncataloged entity.
    """
    if handoff.get("scope") == "topic":
        entries = _load_catalog_entries(root, "memory/references/topics/topics.json")
    else:
        entries = _load_catalog_entries(root, "memory/references/projects/projects.json")
    if not entries:
        return None
    entity_id = str(handoff.get("entity_id") or "")
    match = next(
        (item for item in entries
         if isinstance(item, Mapping) and str(item.get("id") or "") == entity_id),
        None,
    )
    if not isinstance(match, Mapping):
        return None
    cloud_sync: Any = match.get("cloud_sync")
    subtopic_id = handoff.get("subtopic_id")
    if handoff.get("scope") == "topic" and subtopic_id:
        subtopics = match.get("subtopics")
        sub_match = next(
            (sub for sub in subtopics
             if isinstance(sub, Mapping) and str(sub.get("id") or "") == str(subtopic_id)),
            None,
        ) if isinstance(subtopics, list) else None
        if not isinstance(sub_match, Mapping):
            return None
        cloud_sync = sub_match.get("cloud_sync")
    return cloud_sync if isinstance(cloud_sync, Mapping) else None


# ==============================================================================
# Dynamic module loading (isolated cross-skill reuse)
# ==============================================================================

def _load_isolated(name: str, path: Path, scripts_dir: Path) -> Any:
    """Load a skill script under an isolated top-level ``core`` package.

    Both skills ship their own ``scripts/core`` package; deleting and restoring the
    top-level ``core`` entry around ``exec_module`` mirrors the established
    mail-desk/Cloud-Atlas cross-skill loader pattern and keeps both packages intact.
    """
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load module '{name}' from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    old_core = sys.modules.get("core")
    old_path = list(sys.path)
    try:
        sys.path.insert(0, str(scripts_dir))
        if "core" in sys.modules:
            del sys.modules["core"]
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    finally:
        sys.path = old_path
        if old_core is not None:
            sys.modules["core"] = old_core
        elif "core" in sys.modules:
            del sys.modules["core"]
    return module


def _scripts_dir() -> Path:
    return Path(__file__).resolve().parent


def _load_cloud_atlas_gen_filemap() -> Any:
    return _load_isolated(
        "cloud_atlas_promotion_gen_filemap", _scripts_dir() / "gen_filemap.py", _scripts_dir()
    )


def _load_cloud_atlas_convert_cloud_docs() -> Any:
    return _load_isolated(
        "cloud_atlas_promotion_convert", _scripts_dir() / "convert_cloud_docs.py", _scripts_dir()
    )


def _converter_extension_vocabulary() -> tuple[frozenset[str], str]:
    """Derive the convertible extension vocabulary from the converter itself.

    The canonical converter declares ``DEFAULT_EXTENSIONS`` as a comma-separated
    string; the consumer reads that single source of truth instead of duplicating it.
    A non-convertible target skips conversion and is refreshed through the filemap
    generator alone.
    """
    try:
        module = _load_cloud_atlas_convert_cloud_docs()
    except Exception:
        return frozenset(), ""
    raw = getattr(module, "DEFAULT_EXTENSIONS", "")
    if not isinstance(raw, str):
        return frozenset(), ""
    chunks = [chunk.strip().lower() for chunk in raw.split(",") if chunk.strip()]
    normalized = frozenset(chunk if chunk.startswith(".") else f".{chunk}" for chunk in chunks)
    return normalized, raw.strip()


CONVERTIBLE_EXTENSIONS, _CONVERTER_EXTENSIONS_ARG = _converter_extension_vocabulary()


def _load_mail_desk_promotion() -> Any:
    for base in list(Path(__file__).resolve().parents) + [Path.cwd().resolve()]:
        candidate = base / "mail-desk" / "scripts" / "core" / "attachment_promotion.py"
        if not candidate.is_file():
            candidate = base / "skills" / "mail-desk" / "scripts" / "core" / "attachment_promotion.py"
        if candidate.is_file():
            return _load_isolated(
                "mail_desk_attachment_promotion", candidate, candidate.parent.parent
            )
    raise RuntimeError("mail-desk attachment_promotion is unavailable")


def _load_workspace_lock_guard() -> Any:
    cached = sys.modules.get("workspace_lock_guard")
    if cached is not None:
        return cached
    for base in list(Path(__file__).resolve().parents) + [Path.cwd().resolve(), *Path.cwd().resolve().parents]:
        candidate = base / "workspace-lock" / "scripts" / "workspace_lock_guard.py"
        if candidate.is_file():
            spec = importlib.util.spec_from_file_location("workspace_lock_guard", candidate)
            if spec is not None and spec.loader is not None:
                module = importlib.util.module_from_spec(spec)
                sys.modules["workspace_lock_guard"] = module
                spec.loader.exec_module(module)
                return module
    raise RuntimeError("Canonical workspace-lock guard is unavailable")


def verify_workspace_lock(
    workspace_root: Path,
    *,
    lease_id: str | None = None,
    conversation_id: str | None = None,
) -> Any:
    """Require an invocation-owned Cloud-Atlas workspace lock (never legacy)."""
    guard = _load_workspace_lock_guard()
    return guard.require_workspace_lock(
        Path(workspace_root),
        lease_id=lease_id,
        conversation_id=conversation_id,
        allow_legacy=False,
    )


# ==============================================================================
# Result envelope
# ==============================================================================

def _refresh_result(
    status: str,
    reason: str,
    handoff: Mapping[str, Any] | None,
    *,
    filemap_relative_path: str | None = None,
    filemap_sha256: str | None = None,
    storage_ids: list[str] | None = None,
) -> dict[str, Any]:
    source = handoff if isinstance(handoff, Mapping) else {}
    payload: dict[str, Any] = {
        "schema_version": 1,
        "kind": REFRESH_RESULT_KIND,
        "status": status,
        "reason": reason,
        "promotion_id": source.get("promotion_id"),
        "handoff_hash": source.get("handoff_hash"),
        "storage_id": source.get("storage_id"),
        "scan_dir": source.get("scan_dir"),
        "target_relative_path": source.get("target_relative_path"),
        "target_sha256": source.get("target_sha256"),
        "filemap_relative_path": filemap_relative_path,
        "filemap_sha256": filemap_sha256,
        "storage_ids": list(storage_ids or []),
    }
    payload["result_hash"] = _canonical_json_sha256(payload)
    return payload


# ==============================================================================
# Handoff validation
# ==============================================================================

def _validate_handoff(handoff: Any) -> dict[str, Any]:
    if not isinstance(handoff, Mapping):
        raise _HandoffRejected("handoff_invalid")
    if set(handoff) != _HANDOFF_KEYS:
        raise _HandoffRejected("handoff_invalid")
    if handoff.get("schema_version") != HANDOFF_SCHEMA_VERSION:
        raise _HandoffRejected("handoff_invalid")
    if handoff.get("kind") != HANDOFF_KIND:
        raise _HandoffRejected("handoff_invalid")
    if handoff.get("receiver") != HANDOFF_RECEIVER or handoff.get("operation") != HANDOFF_OPERATION:
        raise _HandoffRejected("handoff_invalid")
    if list(handoff.get("required_receiving_steps") or []) != list(REQUIRED_RECEIVING_STEPS):
        raise _HandoffRejected("handoff_invalid")
    if list(handoff.get("prohibited_automatic_steps") or []) != list(PROHIBITED_AUTOMATIC_STEPS):
        raise _HandoffRejected("handoff_invalid")

    for field in ("promotion_id", "candidate_hash", "review_hash", "preflight_hash",
                  "target_sha256", "filemap_snapshot_hash"):
        if not _is_sha256(handoff.get(field)):
            raise _HandoffRejected("handoff_invalid")
    if not isinstance(handoff.get("target_size_bytes"), int) or isinstance(
        handoff.get("target_size_bytes"), bool
    ) or handoff["target_size_bytes"] <= 0:
        raise _HandoffRejected("handoff_invalid")
    if str(handoff.get("scope") or "") not in ("project", "topic"):
        raise _HandoffRejected("handoff_invalid")
    for field in ("entity_id", "storage_id"):
        if not isinstance(handoff.get(field), str) or not handoff[field].strip():
            raise _HandoffRejected("handoff_invalid")
    subtopic = handoff.get("subtopic_id")
    if subtopic is not None and (not isinstance(subtopic, str) or not subtopic.strip()):
        raise _HandoffRejected("handoff_invalid")
    if not _is_safe_relative(handoff.get("scan_dir")):
        raise _HandoffRejected("handoff_invalid")
    if not _is_safe_relative(handoff.get("target_relative_path")):
        raise _HandoffRejected("handoff_invalid")
    journal_relative = handoff.get("journal_relative_path")
    if journal_relative is not None and not _is_safe_relative(journal_relative):
        raise _HandoffRejected("handoff_invalid")
    journal_hash = handoff.get("journal_hash")
    if journal_hash is not None and not _is_sha256(journal_hash):
        raise _HandoffRejected("handoff_invalid")
    if (journal_relative is None) != (journal_hash is None):
        raise _HandoffRejected("handoff_invalid")

    declared = str(handoff.get("handoff_hash") or "").strip().lower()
    body = {key: value for key, value in handoff.items() if key != "handoff_hash"}
    if not _is_sha256(declared) or declared != _canonical_json_sha256(body):
        raise _HandoffRejected("handoff_hash_drift")

    # Every verified promotion carries a hash-chained journal; the journal -- not the
    # handoff -- is the trust anchor.  A journal-less handoff is denied fail-closed
    # before any storage resolution or engine run.
    if journal_relative is None or journal_hash is None:
        raise _HandoffRejected("journal_missing")
    return dict(handoff)


# ==============================================================================
# Bound refresh invocation
# ==============================================================================

def _namespace(**kwargs: Any) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


def _invoke_bound_generation(gen_module: Any, args: argparse.Namespace) -> Any:
    """Run the canonical generator while keeping catalogs read-only for this consumer."""
    original = getattr(gen_module, "update_config_last_synced_at", None)
    gen_module.update_config_last_synced_at = lambda *a, **k: None
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            return gen_module.run_generation(args)
    finally:
        if original is not None:
            gen_module.update_config_last_synced_at = original


def _invoke_bound_conversion(convert_module: Any, args: argparse.Namespace) -> dict[str, Any]:
    run_state: dict[str, Any] = {"target": None, "storages": []}
    with contextlib.redirect_stdout(io.StringIO()):
        convert_module.run_conversion(args, run_state)
    return run_state


def _generation_args(handoff: Mapping[str, Any], workspace_root: Path) -> argparse.Namespace:
    is_topic = handoff["scope"] == "topic"
    return _namespace(
        workspace_root=str(workspace_root),
        project_id=None if is_topic else handoff["entity_id"],
        topic_id=handoff["entity_id"] if is_topic else None,
        topic=is_topic,
        subtopic_id=handoff["subtopic_id"],
        project_title=None,
        scan_dir=None,
        output_json=None,
        output_md=None,
        storage_id=handoff["storage_id"],
        json=True,
    )


def _conversion_args(handoff: Mapping[str, Any], workspace_root: Path) -> argparse.Namespace:
    is_topic = handoff["scope"] == "topic"
    return _namespace(
        workspace_root=str(workspace_root),
        project_id=None if is_topic else handoff["entity_id"],
        topic_id=handoff["entity_id"] if is_topic else None,
        subtopic_id=handoff["subtopic_id"],
        cloud_dir=None,
        output_dir=None,
        filemap_json=None,
        extensions=_CONVERTER_EXTENSIONS_ARG,
        force=False,
        topic=is_topic,
        storage_id=handoff["storage_id"],
        file_timeout=60,
        jobs=2,
        ocr_policy="local_derivative",
        no_ocr=False,
        redo_ocr=False,
        json=True,
    )


# ==============================================================================
# Public consumer
# ==============================================================================

def consume_promotion_refresh_handoff(
    handoff: Mapping[str, Any],
    workspace_root: Path | str | None = None,
    *,
    lease_id: str | None = None,
    conversation_id: str | None = None,
    current_time: datetime.datetime | None = None,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    """Consume one mail-desk refresh handoff and refresh only the bound storage.

    Returns a deterministic ``cloud_atlas_refresh_result`` Schema 1 with status
    ``refresh_completed`` (verified), ``refresh_pending`` (adapter/tool error or
    timeout) or ``refresh_denied`` (invalid or journal-less handoff, lock, drift or
    unbound storage).  Every denial up to and including the catalog-origin check
    happens before the engine step and mutates nothing; a denial *after* the engine ran
    (post-refresh filemap/entry verification) reports the exact state rather than
    claiming "never mutate", because the deterministic refresh is verified by
    re-reading the generated filemap.
    """
    del data_dir  # reserved for symmetry with the mail-desk side; unused
    root = Path(workspace_root).resolve() if workspace_root is not None else Path.cwd().resolve()

    try:
        validated = _validate_handoff(handoff)
    except _HandoffRejected as exc:
        return _refresh_result(
            REFRESH_STATUS_DENIED, exc.reason,
            handoff if isinstance(handoff, Mapping) else None,
        )

    # 1. Own lock: the mail-desk handoff can never authorize Cloud-Atlas.
    try:
        verify_workspace_lock(root, lease_id=lease_id, conversation_id=conversation_id)
    except Exception:
        return _refresh_result(REFRESH_STATUS_DENIED, "lock_unavailable", validated)

    # 2. Promotion journal revalidation: the handoff's trust anchor.  Both result
    #    statuses carry a journal; only a revalidated, *completed* journal may authorize
    #    the refresh, and the journal's bound subtopic id is authoritative -- the handoff
    #    must never redirect the refresh to a different subtopic's paths.
    try:
        mail_module = _load_mail_desk_promotion()
        journal_path = _local_path(root, validated["journal_relative_path"])
        journal = mail_module.load_promotion_journal(
            journal_path,
            expected_promotion_id=validated["promotion_id"],
            expected_candidate_hash=validated["candidate_hash"],
            expected_review_hash=validated["review_hash"],
            expected_source_sha256=validated["target_sha256"],
            expected_target_relative_path=validated["target_relative_path"],
        )
        if str(journal.get("journal_hash") or "").strip().lower() != validated["journal_hash"]:
            raise _HandoffRejected("journal_drift")
        if str(journal.get("preflight_hash") or "").strip().lower() != validated["preflight_hash"]:
            raise _HandoffRejected("journal_drift")
        if str(journal.get("storage_id") or "").strip() != validated["storage_id"]:
            raise _HandoffRejected("journal_drift")
        journal_subtopic = journal.get("subtopic_id")
        normalized_journal_subtopic = (
            journal_subtopic.strip()
            if isinstance(journal_subtopic, str) and journal_subtopic.strip()
            else None
        )
        handoff_subtopic = validated.get("subtopic_id")
        normalized_handoff_subtopic = (
            handoff_subtopic.strip()
            if isinstance(handoff_subtopic, str) and handoff_subtopic.strip()
            else None
        )
        if normalized_journal_subtopic != normalized_handoff_subtopic:
            raise _HandoffRejected("journal_drift")
        if str(journal.get("status") or "").strip().lower() != "completed":
            raise _HandoffRejected("journal_drift")
    except _HandoffRejected:
        return _refresh_result(REFRESH_STATUS_DENIED, "journal_drift", validated)
    except Exception:
        return _refresh_result(REFRESH_STATUS_DENIED, "journal_drift", validated)

    # 3. Real target re-verification against the bound SHA-256/size.
    try:
        target = _target_path(root, validated["scan_dir"], validated["target_relative_path"])
    except _HandoffRejected:
        return _refresh_result(REFRESH_STATUS_DENIED, "handoff_invalid", validated)
    if not target.is_file():
        return _refresh_result(REFRESH_STATUS_DENIED, "target_missing", validated)
    try:
        if (target.stat().st_size != validated["target_size_bytes"]
                or _sha256_file(target) != validated["target_sha256"]):
            return _refresh_result(REFRESH_STATUS_DENIED, "target_drift", validated)
    except OSError:
        return _refresh_result(REFRESH_STATUS_DENIED, "target_drift", validated)

    # 4. Resolve exactly the bound storage (never a workspace-wide scan).  First prove
    #    catalog origin: the bound storage must be declared by a real catalog
    #    cloud_sync entry, so the generator's synthesized fallback for an uncataloged
    #    entity can never be reached.  This stops before any engine run or mutation.
    declared_cloud_sync = _catalog_cloud_sync(root, validated)
    if (declared_cloud_sync is None
            or validated["storage_id"] not in declared_cloud_sync
            or not isinstance(declared_cloud_sync.get(validated["storage_id"]), Mapping)):
        return _refresh_result(REFRESH_STATUS_DENIED, "storage_unbound", validated)
    try:
        gen_module = _load_cloud_atlas_gen_filemap()
    except Exception:
        return _refresh_result(REFRESH_STATUS_PENDING, "adapter_missing", validated)
    try:
        configs = gen_module.resolve_all_sync_configs(
            str(root),
            validated["entity_id"],
            validated["scope"] == "topic",
            validated["storage_id"],
            validated["subtopic_id"],
        )
    except Exception:
        return _refresh_result(REFRESH_STATUS_PENDING, "storage_resolution_error", validated)
    if not isinstance(configs, Mapping) or list(configs) != [validated["storage_id"]]:
        return _refresh_result(REFRESH_STATUS_DENIED, "storage_unbound", validated)
    storage_cfg = configs[validated["storage_id"]]
    if not isinstance(storage_cfg, Mapping):
        return _refresh_result(REFRESH_STATUS_DENIED, "storage_unbound", validated)
    if _normalize_slashes(storage_cfg.get("scan_dir")).strip("/") != validated["scan_dir"]:
        return _refresh_result(REFRESH_STATUS_DENIED, "storage_drift", validated)
    storage_ids = list(configs)

    # 5. Bound refresh through the canonical engine (conversion only for convertible
    #    types; the filemap generator always runs and owns the target entry).
    extension = PurePosixPath(validated["target_relative_path"]).suffix.lower()
    try:
        if extension in CONVERTIBLE_EXTENSIONS:
            convert_module = _load_cloud_atlas_convert_cloud_docs()
            _invoke_bound_conversion(convert_module, _conversion_args(validated, root))
        _invoke_bound_generation(gen_module, _generation_args(validated, root))
    except Exception:
        return _refresh_result(REFRESH_STATUS_PENDING, "refresh_error", validated,
                               storage_ids=storage_ids)

    # 6. Post-refresh verification: whole-filemap plus the exact target entry.
    try:
        filemap_path = _local_path(root, str(storage_cfg.get("output_json")))
        new_filemap = json.loads(filemap_path.read_text(encoding="utf-8"))
    except Exception:
        return _refresh_result(REFRESH_STATUS_DENIED, "filemap_unreadable", validated,
                               storage_ids=storage_ids)
    try:
        mail_module = _load_mail_desk_promotion()
        # The generator writes naive local wall-clock timestamps; compare against the
        # same local naive frame (the MD-A5 freshness validator labels naive
        # timestamps as UTC, so an aware injected clock is converted to local naive
        # first -- offset-invariant).
        freshness_clock = _local_wall_clock(current_time)
        is_valid, _updated_at, _error = mail_module.validate_cloud_atlas_filemap(
            new_filemap,
            expected_storage_id=validated["storage_id"],
            expected_scope=validated["scope"],
            expected_project_id=validated["entity_id"],
            storage_cfg=storage_cfg,
            current_time=freshness_clock,
            workspace_root=str(root),
        )
    except Exception:
        return _refresh_result(REFRESH_STATUS_DENIED, "filemap_invalid", validated,
                               storage_ids=storage_ids)
    if not is_valid:
        return _refresh_result(REFRESH_STATUS_DENIED, "filemap_invalid", validated,
                               storage_ids=storage_ids)

    entry_key = f'{validated["scan_dir"]}/{validated["target_relative_path"]}'
    entry = (new_filemap.get("files") if isinstance(new_filemap, Mapping) else None) or {}
    present = entry.get(entry_key)
    if not isinstance(present, Mapping) or str(present.get("sha256") or "").strip().lower() != validated["target_sha256"]:
        return _refresh_result(REFRESH_STATUS_DENIED, "filemap_entry_missing", validated,
                               storage_ids=storage_ids)

    try:
        filemap_sha256 = _sha256_file(filemap_path)
    except OSError:
        return _refresh_result(REFRESH_STATUS_DENIED, "filemap_unreadable", validated,
                               storage_ids=storage_ids)
    return _refresh_result(
        REFRESH_STATUS_COMPLETED,
        "refresh_completed",
        validated,
        filemap_relative_path=_normalize_slashes(str(storage_cfg.get("output_json"))),
        filemap_sha256=filemap_sha256,
        storage_ids=storage_ids,
    )


__all__ = [
    "consume_promotion_refresh_handoff",
    "verify_workspace_lock",
    "HANDOFF_KIND",
    "HANDOFF_RECEIVER",
    "HANDOFF_OPERATION",
    "HANDOFF_SCHEMA_VERSION",
    "REFRESH_RESULT_KIND",
    "REFRESH_STATUS_COMPLETED",
    "REFRESH_STATUS_PENDING",
    "REFRESH_STATUS_DENIED",
    "REQUIRED_RECEIVING_STEPS",
    "PROHIBITED_AUTOMATIC_STEPS",
]
