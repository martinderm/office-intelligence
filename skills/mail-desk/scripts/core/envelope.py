"""Canonical JSON envelopes for mail-desk command-line tools.

The builders are intentionally small and side-effect free so that individual
CLIs can adopt the canonical response contract without sharing CLI behavior.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import json
import sys
from typing import Any, TextIO


ENVELOPE_KEYS = ("action", "success", "state", "message", "data", "error")
MAX_TEXT_LENGTH = 1000


def _bounded_text(value: object, *, limit: int = MAX_TEXT_LENGTH) -> str:
    """Return deterministic, bounded text suitable for an envelope."""
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _stable_nonempty(value: object, *, field: str) -> str:
    """Validate identifiers without silently rewriting their stable value."""
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be a non-empty, trimmed string.")
    return value


def _copy_mapping(value: Mapping[str, Any] | None, *, field: str) -> dict[str, Any]:
    """Copy caller-owned mappings so building an envelope never mutates them."""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError(f"{field} must be a mapping or None.")
    return deepcopy(dict(value))


def _validate_state(state: object) -> str:
    return _stable_nonempty(state, field="state")


def build_success(
    action: str,
    message: object = "",
    data: Mapping[str, Any] | None = None,
    *,
    state: str = "Completed",
) -> dict[str, Any]:
    """Build a successful canonical envelope without mutating ``data``."""
    return {
        "action": _stable_nonempty(action, field="action"),
        "success": True,
        "state": _validate_state(state),
        "message": _bounded_text(message),
        "data": _copy_mapping(data, field="data"),
        "error": None,
    }


def build_error(
    action: str,
    message: object = "Operation failed.",
    data: Mapping[str, Any] | None = None,
    *,
    error_type: str = "Error",
    error_details: Mapping[str, Any] | None = None,
    state: str = "Failed",
) -> dict[str, Any]:
    """Build a failed canonical envelope with structured, bounded error text."""
    error = {
        "type": _stable_nonempty(error_type, field="error_type"),
        "message": _bounded_text(message),
    }
    if error_details is not None:
        error["details"] = _copy_mapping(error_details, field="error_details")

    return {
        "action": _stable_nonempty(action, field="action"),
        "success": False,
        "state": _validate_state(state),
        "message": _bounded_text(message),
        "data": _copy_mapping(data, field="data"),
        "error": error,
    }


def _validate_envelope(envelope: Mapping[str, Any]) -> None:
    """Reject non-canonical envelopes before serializing or writing them."""
    if not isinstance(envelope, Mapping) or set(envelope) != set(ENVELOPE_KEYS):
        raise ValueError("envelope must contain exactly the canonical six keys.")
    _stable_nonempty(envelope["action"], field="action")
    _validate_state(envelope["state"])
    if not isinstance(envelope["success"], bool):
        raise TypeError("success must be a boolean.")
    if not isinstance(envelope["message"], str):
        raise TypeError("message must be a string.")
    if len(envelope["message"]) > MAX_TEXT_LENGTH:
        raise ValueError("message exceeds the envelope text limit.")
    if not isinstance(envelope["data"], Mapping):
        raise TypeError("data must be a mapping.")
    if envelope["success"]:
        if envelope["error"] is not None:
            raise ValueError("successful envelopes must have error set to None.")
        return
    if not isinstance(envelope["error"], Mapping):
        raise TypeError("failed envelopes must contain a structured error mapping.")
    _stable_nonempty(envelope["error"].get("type"), field="error.type")
    if not isinstance(envelope["error"].get("message"), str):
        raise TypeError("error.message must be a string.")
    if len(envelope["error"]["message"]) > MAX_TEXT_LENGTH:
        raise ValueError("error.message exceeds the envelope text limit.")


def emit_json(envelope: Mapping[str, Any], stream: TextIO | None = None) -> None:
    """Serialize one canonical envelope fully, then write it to ``stream``.

    ``json.dumps`` deliberately happens before the first stream write: if the
    payload is not JSON serializable, callers receive the error with no partial
    machine-readable output.
    """
    _validate_envelope(envelope)
    serialized = json.dumps(envelope, ensure_ascii=False)
    target = sys.stdout if stream is None else stream
    target.write(serialized + "\n")
