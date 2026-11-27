"""Common utility functions for mail-desk."""

from __future__ import annotations

import os
import json
import sys
import tempfile
import time
import unicodedata
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# Ensure standard streams handle UTF-8 cleanly on Windows
try:
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if sys.stderr and hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format with Z suffix."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


#: Windows error codes meaning "another handle blocks the rename": 5 access
#: denied, 32 sharing violation, 33 lock violation. Only these are retried.
TRANSIENT_REPLACE_WINERRORS = frozenset({5, 32, 33})

#: Backoff before each re-attempt of ``os.replace`` (issue #70): at most
#: ``len + 1`` attempts and about 0.4 s in total, so a stuck handle cannot hang
#: a batch. Only the replace is retried, never the staging write.
REPLACE_RETRY_DELAYS_SECONDS = (0.02, 0.05, 0.1, 0.2)


class AtomicReplaceError(OSError):
    """``os.replace`` stayed blocked after the bounded retries; target untouched."""

    reason_code = "atomic_replace_failed"

    def __init__(self, target_name: str, attempts: int, cause: OSError) -> None:
        self.attempts = attempts
        self.cause_winerror = getattr(cause, "winerror", None)
        super().__init__(
            f"Atomic replace of '{target_name}' failed after {attempts} attempts "
            f"(winerror={self.cause_winerror}): another process or handle keeps the "
            "target open, or access is denied. The previous file is preserved."
        )


def _is_transient_replace_error(error: OSError) -> bool:
    return (
        isinstance(error, PermissionError)
        and getattr(error, "winerror", None) in TRANSIENT_REPLACE_WINERRORS
    )


def replace_with_retry(source: Path, target: Path) -> None:
    """``os.replace`` with a bounded retry for transient Windows sharing errors.

    Anything that is not a transient sharing/denied error surfaces unchanged on
    the first attempt. When the budget is exhausted a classified
    ``AtomicReplaceError`` (an ``OSError``) is raised, chained to the last error.
    """
    attempts = len(REPLACE_RETRY_DELAYS_SECONDS) + 1
    for attempt in range(1, attempts + 1):
        try:
            os.replace(source, target)
            return
        except OSError as error:
            if not _is_transient_replace_error(error):
                raise
            if attempt == attempts:
                raise AtomicReplaceError(Path(target).name, attempts, error) from error
        time.sleep(REPLACE_RETRY_DELAYS_SECONDS[attempt - 1])


def atomic_write_text(target: Path, content: str) -> None:
    """Durably write UTF-8 text to a sibling temporary file, then replace target."""
    target.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor = -1
    temporary_path: Path | None = None
    try:
        file_descriptor, temporary_name = tempfile.mkstemp(
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as handle:
            file_descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        replace_with_retry(temporary_path, target)
        temporary_path = None
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass


def atomic_write_json(target: Path, payload: Any) -> None:
    """Atomically replace a JSON document with stable UTF-8 formatting."""
    atomic_write_text(target, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def atomic_rewrite_jsonl(target: Path, entries: Iterable[dict[str, Any]]) -> None:
    """Atomically replace a JSONL document; append-only writers remain lock-bound."""
    content = "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries)
    atomic_write_text(target, content)


def get_iso_week_folder() -> str:
    """Return current ISO week string in YYYY-Www format."""
    now = datetime.now(timezone.utc)
    iso_year, iso_week, _ = now.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def normalize_message_id(value: str) -> str:
    """Strip surrounding whitespace and angle brackets and return lowercase."""
    s = value.strip()
    while s.startswith("<") and s.endswith(">") and len(s) >= 2:
        s = s[1:-1].strip()
    return s.lower()


def is_well_formed_message_id(value: Any) -> bool:
    """Return whether ``value`` is a well-formed Message-ID after normalization.

    The check deliberately runs on the normalized form: ``message read`` emits
    the header without angle brackets while ``envelope thread`` wraps it in
    ``<...>``.  Both spellings are therefore accepted, and only the normalized
    value must be well formed (issue #103 follow-up).
    """
    if not isinstance(value, str):
        return False
    normalized = normalize_message_id(value)
    if not normalized or normalized.count("@") != 1:
        return False
    if any(
        char in "<>" or char.isspace() or unicodedata.category(char).startswith("C")
        for char in normalized
    ):
        return False
    local, domain = normalized.split("@")
    if not local or not domain:
        return False
    return domain != "generated"


#: Envelope keys a newer Himalaya exposes the Message-ID under: the serialized
#: camelCase field and the tolerated snake_case spelling of the same value.
ENVELOPE_MESSAGE_ID_KEYS = ("messageId", "message_id")


def normalize_envelope_message_id(envelope: Any) -> str | None:
    """Return the normalized Message-ID an ``envelope list`` entry carries.

    Total and side-effect free: a non-mapping input, a missing field and every
    unusable value (absent, ``null``, empty, non-string, not well formed) yield
    ``None`` instead of raising.  When both spellings are present they must be
    usable and normalize to the same value; a conflict is unresolved and also
    yields ``None``.  The result is derived convenience only, never durable
    identity; envelope IDs remain locators (issue #103).
    """
    if not isinstance(envelope, Mapping):
        return None
    normalized: list[str] = []
    for key in ENVELOPE_MESSAGE_ID_KEYS:
        if key not in envelope:
            continue
        raw = envelope[key]
        if not is_well_formed_message_id(raw):
            return None
        normalized.append(normalize_message_id(raw))
    if not normalized:
        return None
    if len(normalized) == 2 and normalized[0] != normalized[1]:
        return None
    return normalized[0]


def ensure_sentence_end(text: str) -> str:
    """Append a period unless the text already ends with sentence punctuation."""
    value = str(text).strip()
    if value and value[-1] not in ".!?…":
        return f"{value}."
    return value


def resolve_data_dir(custom: str | Path | None = None) -> Path:
    """Resolve data/mail-desk directory path."""
    if custom:
        return Path(custom).expanduser().resolve()
    env_dir = os.environ.get("MAIL_DESK_DATA_DIR", "").strip()
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    cwd_data = Path.cwd() / "data" / "mail-desk"
    if cwd_data.exists() or cwd_data.parent.exists():
        return cwd_data.resolve()
    # fallback to script-relative
    fallback = Path(__file__).resolve().parents[4] / "data" / "mail-desk"
    return fallback.resolve()


def resolve_final_index_path(custom_path: str | Path | None = None, data_dir: Path | None = None) -> Path:
    """Resolve final-location-index.json path."""
    if custom_path:
        return Path(custom_path).expanduser().resolve()
    env_index = os.environ.get("MAIL_DESK_FINAL_INDEX_PATH", "").strip()
    if env_index:
        return Path(env_index).expanduser().resolve()
    dd = data_dir or resolve_data_dir()
    return dd / "final-location-index.json"


def resolve_evidence_dir(kind: str, item_id: str, workspace_root: Path | None = None) -> Path:
    """
    Dual-Path evidence resolver (Dual-Evidence Standard):
    1. If memory/evidence/<kind>/<item_id> exists, or memory/evidence exists, use memory/evidence/<kind>/<item_id>
    2. Fallback to legacy memory/references/<kind>/<item_id>/evidence if it exists
    3. Default to memory/evidence/<kind>/<item_id>
    """
    ws = workspace_root or Path.cwd()
    new_path = ws / "memory" / "evidence" / kind / item_id
    if new_path.exists() or (ws / "memory" / "evidence").exists():
        return new_path

    legacy_path = ws / "memory" / "references" / kind / item_id / "evidence"
    if legacy_path.exists():
        return legacy_path

    return new_path
