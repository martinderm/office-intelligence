"""Final location index data access layer."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from .common import normalize_message_id, utc_now_iso

ALLOWED_FIELDS = {
    "message_id",
    "mailbox",
    "backend",
    "final_folder",
    "final_label",
    "envelope_id",
    "gmail_message_id",
    "gmail_thread_id",
    "in_reply_to",
    "references",
    "updated_at",
}


def load_final_index(index_path: Path) -> dict[str, Any]:
    """Load final location index or initialize schema."""
    if not index_path.exists():
        return {"schema_version": 1, "updated_at": None, "items": {}}
    with index_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        return {"schema_version": 1, "updated_at": None, "items": {}}
    data.setdefault("items", {})
    return data


def save_final_index_atomic(index_path: Path, data: dict[str, Any]) -> None:
    """Save final location index atomically using a temporary file."""
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="\n",
        dir=index_path.parent,
        prefix=index_path.name + ".",
        suffix=".tmp",
        delete=False,
    ) as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
        temp_path = Path(f.name)
    temp_path.replace(index_path)


def validate_index_payload(payload: dict[str, Any], mode: str) -> None:
    """Validate index payload structure."""
    if "message_id" not in payload or not str(payload["message_id"]).strip():
        raise ValueError("'message_id' is required")

    unknown = set(payload.keys()) - ALLOWED_FIELDS
    if unknown:
        raise ValueError(f"Unsupported fields: {', '.join(sorted(unknown))}")

    backend = str(payload.get("backend", "himalaya")).strip().lower()
    if mode == "upsert-final":
        required_fields = ("final_label", "gmail_message_id") if backend == "gmail" else ("final_folder", "envelope_id")
        for field in required_fields:
            if field not in payload or not str(payload[field]).strip():
                raise ValueError(f"'{field}' is required in mode=upsert-final")

    if "references" in payload and not isinstance(payload["references"], list):
        raise ValueError("'references' must be an array when provided")


def upsert_final_index_entry(
    index_path: Path,
    payload: dict[str, Any],
    mode: str = "patch",
) -> dict[str, Any]:
    """Upsert or patch a single entry in final-location-index.json."""
    validate_index_payload(payload, mode)
    data = load_final_index(index_path)
    items = data.setdefault("items", {})

    msg_norm = normalize_message_id(str(payload["message_id"]))
    existing = items.get(msg_norm)

    if mode == "patch" and existing is None:
        raise ValueError("Cannot patch non-existing entry. Use mode=upsert-final first.")

    created = existing is None
    entry = dict(existing or {})
    entry["message_id"] = msg_norm

    if "backend" not in payload and "backend" not in entry:
        entry["backend"] = "himalaya"

    for key, value in payload.items():
        if key == "message_id":
            continue
        entry[key] = value

    if "updated_at" not in payload or not str(payload.get("updated_at", "")).strip():
        entry["updated_at"] = utc_now_iso()

    items[msg_norm] = entry
    data["updated_at"] = utc_now_iso()
    save_final_index_atomic(index_path, data)

    return {
        "ok": True,
        "mode": mode,
        "created": created,
        "message_id": msg_norm,
        "index": str(index_path),
    }


def upsert_final_index_many(
    index_path: Path,
    items_to_upsert: list[dict[str, Any]],
    mode: str = "upsert-final",
) -> dict[str, Any]:
    """Upsert or patch multiple entries atomically."""
    data = load_final_index(index_path)
    items = data.setdefault("items", {})

    created_count = 0
    updated_count = 0
    now_iso = utc_now_iso()

    for payload in items_to_upsert:
        validate_index_payload(payload, mode)
        msg_norm = normalize_message_id(str(payload["message_id"]))
        existing = items.get(msg_norm)

        if mode == "patch" and existing is None:
            raise ValueError(f"Cannot patch non-existing entry '{msg_norm}'")

        if existing is None:
            created_count += 1
        else:
            updated_count += 1

        entry = dict(existing or {})
        entry["message_id"] = msg_norm
        if "backend" not in payload and "backend" not in entry:
            entry["backend"] = "himalaya"

        for key, value in payload.items():
            if key == "message_id":
                continue
            entry[key] = value

        entry["updated_at"] = payload.get("updated_at") or now_iso
        items[msg_norm] = entry

    data["updated_at"] = now_iso
    save_final_index_atomic(index_path, data)

    return {
        "ok": True,
        "mode": mode,
        "total": len(items_to_upsert),
        "created": created_count,
        "updated": updated_count,
        "index": str(index_path),
    }


def query_final_index(
    index_path: Path,
    folder: str | None = None,
    query: str | None = None,
) -> dict[str, Any]:
    """Query and filter items in final-location-index.json."""
    data = load_final_index(index_path)
    items = data.get("items", {})
    results: dict[str, Any] = {}

    for key, val in items.items():
        if not isinstance(val, dict):
            continue

        if folder:
            location = str(val.get("final_folder") or val.get("final_label") or "")
            if folder.lower() not in location.lower():
                continue

        if query:
            q = query.lower()
            combined_fields = []
            for field_val in val.values():
                if isinstance(field_val, list):
                    combined_fields.extend([str(x).lower() for x in field_val])
                else:
                    combined_fields.append(str(field_val).lower())
            if not any(q in f for f in combined_fields):
                continue

        results[key] = val

    return {
        "count": len(results),
        "items": results,
    }


def lookup_final_index(index_path: Path, message_id: str) -> dict[str, Any] | None:
    """Lookup a single message_id in final location index."""
    data = load_final_index(index_path)
    norm = normalize_message_id(message_id)
    return data.get("items", {}).get(norm)
