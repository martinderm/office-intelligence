#!/usr/bin/env python3
"""Explicit, temporary adapter from canonical mail-desk envelopes to legacy JSON.

This tool is intentionally a JSON translator, not a command wrapper.  It never
starts another process and only reads one canonical envelope from standard input
or a caller-supplied, read-only input file.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from core.envelope import build_error, build_success, emit_json

CANONICAL_KEYS = ("action", "success", "state", "message", "data", "error")
ADAPTER_ACTION = "legacy_cli_adapter"

# The inventory in references/legacy-cli-adapter.md is deliberately reflected
# here.  Adding an action requires both a historical shape and an explicit
# profile update; there is no generic fallback profile.
PROFILE_ACTIONS = {
    "mail-desk-status-v1": frozenset({"inspect_manifest", "himalaya_client"}),
    "mail-desk-ok-v1": frozenset(
        {"final_location_index", "mailbox_preflight", "move_and_patch", "batch_runner"}
    ),
}
FLAT_RESERVED_KEYS = frozenset({"ok", "error"})


class AdapterError(ValueError):
    """A user-correctable adapter input, profile, or collision error."""


class StrictArgumentParser(argparse.ArgumentParser):
    """Keep parser failures inside the documented canonical adapter contract."""

    def error(self, message: str) -> None:
        raise AdapterError(message)


def _nonempty_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise AdapterError(f"{field} must be a non-empty, trimmed string.")
    return value


def validate_canonical_envelope(value: object) -> dict[str, Any]:
    """Validate exactly the canonical six-key JSON envelope and its core types."""
    if not isinstance(value, dict):
        raise AdapterError("input must be one JSON object containing the canonical envelope.")
    if set(value) != set(CANONICAL_KEYS):
        raise AdapterError(
            "input must contain exactly these canonical keys: " + ", ".join(CANONICAL_KEYS) + "."
        )

    _nonempty_string(value["action"], "action")
    if not isinstance(value["success"], bool):
        raise AdapterError("success must be a boolean.")
    _nonempty_string(value["state"], "state")
    if not isinstance(value["message"], str):
        raise AdapterError("message must be a string.")
    if not isinstance(value["data"], dict):
        raise AdapterError("data must be a JSON object.")

    error = value["error"]
    if value["success"]:
        if error is not None:
            raise AdapterError("successful canonical envelopes must have error set to null.")
    else:
        if not isinstance(error, dict):
            raise AdapterError("failed canonical envelopes must have a structured error object.")
        _nonempty_string(error.get("type"), "error.type")
        if not isinstance(error.get("message"), str):
            raise AdapterError("error.message must be a string.")

    return value


def _mapping(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AdapterError(f"{field} must be a JSON object for this legacy profile.")
    return value


def _required_mapping(data: dict[str, Any], key: str, operation: str) -> dict[str, Any]:
    if key not in data:
        raise AdapterError(f"{operation} is missing canonical data.{key}; its historical payload cannot be reconstructed.")
    return _mapping(data[key], f"data.{key}")


def _legacy_himalaya_manifest(data: dict[str, Any], success: bool) -> dict[str, Any]:
    """Restore the old manifest payload as far as retained canonical data allows."""
    raw_results = data.get("results")
    if not isinstance(raw_results, list):
        raise AdapterError("himalaya manifest is missing canonical data.results.")
    results = []
    for raw in raw_results:
        item = _mapping(raw, "data.results[]")
        operation = _nonempty_string(item.get("operation"), "data.results[].operation")
        item_success = item.get("success")
        if not isinstance(item_success, bool):
            raise AdapterError("data.results[].success must be a boolean.")
        restored = {"action": operation, "success": item_success}
        if item_success:
            restored["result"] = item.get("data")
        else:
            restored["error"] = item.get("error")
        results.append(restored)
    return {
        "all_succeeded": success,
        "total_operations": data.get("total_operations", len(results)),
        "results": results,
        "input_file_deleted": bool(data.get("input_file_deleted")),
    }


def _legacy_status_payload(envelope: dict[str, Any]) -> object:
    """Reconstruct the two observed status/data/error payload families."""
    data = envelope["data"]
    action = envelope["action"]
    if not envelope["success"] and envelope["state"] != "PartialFailure":
        # Both historical direct-error branches emitted data: null.
        return None
    if action == "inspect_manifest":
        return _required_mapping(data, "result", "inspect_manifest")
    operation = data.get("operation")
    if operation == "manifest":
        return _legacy_himalaya_manifest(data, envelope["success"])
    # Direct Himalaya operations historically put the raw command result below
    # data, rather than the canonical operation/result wrapper.
    return data.get("result")


def _without_reserved(data: dict[str, Any], profile: str, fixed_keys: frozenset[str] = frozenset()) -> None:
    collisions = sorted((FLAT_RESERVED_KEYS | fixed_keys).intersection(data))
    if collisions:
        raise AdapterError(
            f"{profile} cannot flatten data containing legacy control key(s): "
            + ", ".join(collisions)
            + ". Use the canonical envelope or a non-colliding profile."
        )


def _legacy_final_index_operation(
    operation: str,
    success: bool,
    data: dict[str, Any],
    error: object,
    *,
    state: str = "Completed",
    manifest_result: bool = False,
) -> dict[str, Any]:
    """Restore documented direct and manifest final-index operation records."""
    if operation in {"stats", "query"}:
        if success:
            payload = (
                data
                if manifest_result and operation == "query"
                else _required_mapping(data, "result", f"final_location_index {operation}")
            )
        else:
            payload = {key: value for key, value in data.items() if key != "operation"}
        _without_reserved(payload, "final_location_index", frozenset({"action"}))
        legacy = {"ok": success, "action": operation, **payload}
    elif operation == "lookup":
        legacy = {
            # The historical direct lookup deliberately used ok:true for a
            # completed lookup whose found value was false.  Retain that
            # payload signal for canonical NotFound only; other failures stay
            # false and still carry the structured adapter error below.
            "ok": success or state == "NotFound",
            "action": "lookup",
            "found": bool(data.get("found", data.get("item") is not None)),
            "message_id": data.get("message_id"),
            "item": data.get("item"),
        }
    elif operation in {"upsert", "batch_import"}:
        payload = (
            _required_mapping(data, "result", f"final_location_index {operation}")
            if success
            else {key: value for key, value in data.items() if key != "operation"}
        )
        _without_reserved(payload, "final_location_index")
        legacy = {"ok": success, "action": operation, "result": payload}
    else:
        raise AdapterError(f"final_location_index operation '{operation}' has no documented legacy shape.")
    if not success:
        legacy["error"] = error
    return legacy


def _legacy_final_index(envelope: dict[str, Any]) -> dict[str, Any]:
    data = envelope["data"]
    operation = _nonempty_string(data.get("operation"), "data.operation")
    if operation != "manifest":
        return _legacy_final_index_operation(
            operation, envelope["success"], data, envelope["error"], state=envelope["state"]
        )

    raw_results = data.get("results")
    raw_list = raw_results if isinstance(raw_results, list) else [raw_results]
    restored = []
    for raw in raw_list:
        item = _mapping(raw, "data.results[]")
        restored.append(
            _legacy_final_index_operation(
                _nonempty_string(item.get("operation"), "data.results[].operation"),
                bool(item.get("success")),
                _mapping(item.get("data"), "data.results[].data"),
                item.get("error"),
                manifest_result=True,
            )
        )
    legacy = {
        "ok": envelope["success"],
        "total_operations": data.get("total_operations", len(restored)),
        "results": restored if isinstance(raw_results, list) else restored[0],
        **({"error": envelope["error"]} if not envelope["success"] else {}),
    }
    if "input_file_deleted" in data:
        legacy["input_file_deleted"] = bool(data["input_file_deleted"])
    return legacy


def _legacy_ok_payload(envelope: dict[str, Any]) -> dict[str, Any]:
    """Restore action-specific flat ok payloads without leaking `operation`."""
    data = envelope["data"]
    action = envelope["action"]
    if action == "batch_runner":
        _without_reserved(data, "mail-desk-ok-v1", frozenset({"mode"}))
        operation = _nonempty_string(data.get("operation"), "data.operation")
        legacy = {"ok": envelope["success"], "mode": operation}
        legacy.update({key: value for key, value in data.items() if key != "operation"})
    elif action == "final_location_index":
        legacy = _legacy_final_index(envelope)
    elif action == "mailbox_preflight":
        _without_reserved(data, "mail-desk-ok-v1")
        legacy = {"ok": envelope["success"], **{key: value for key, value in data.items() if key != "operation"}}
    else:  # move_and_patch
        if envelope["success"]:
            mailbox = _mapping(data.get("mailbox"), "data.mailbox")
            index = _mapping(data.get("index"), "data.index")
            legacy = {
                "ok": True,
                "message_id": data.get("message_id"),
                "moved": bool(mailbox.get("copy_completed")),
                "source_folder": mailbox.get("source_folder"),
                "target_folder": mailbox.get("target_folder"),
                "new_envelope_id": mailbox.get("target_envelope_id"),
                "index_updated": bool(index.get("updated")),
            }
        else:
            # Historical failures only exposed ok/error.  The canonical data
            # cannot restore a single former branch without guessing.
            legacy = {"ok": False}
    if not envelope["success"]:
        # Historic errors were strings.  Deliberately preserve the newer,
        # structured error rather than losing actionable failure metadata.
        legacy["error"] = envelope["error"]
    return legacy


def translate(envelope: dict[str, Any], profile: str) -> dict[str, Any]:
    """Translate a validated envelope through one documented legacy profile."""
    allowed_actions = PROFILE_ACTIONS.get(profile)
    if allowed_actions is None:
        raise AdapterError(
            "unsupported profile; choose one of: " + ", ".join(sorted(PROFILE_ACTIONS)) + "."
        )
    if envelope["action"] not in allowed_actions:
        raise AdapterError(
            f"profile '{profile}' does not support action '{envelope['action']}'; allowed actions: "
            + ", ".join(sorted(allowed_actions))
            + "."
        )

    if profile == "mail-desk-status-v1":
        status = "success" if envelope["success"] else ("partial" if envelope["state"] == "PartialFailure" else "error")
        return {"status": status, "data": _legacy_status_payload(envelope), "error": envelope["error"]}
    return _legacy_ok_payload(envelope)


def canonical_adapter_error(message: str, *, profile: str | None = None) -> dict[str, Any]:
    """Return the one documented error contract for adapter-owned failures."""
    data: dict[str, Any] = {"supported_profiles": sorted(PROFILE_ACTIONS)}
    if profile is not None:
        data["requested_profile"] = profile
    return build_error(ADAPTER_ACTION, message, data, error_type="AdapterInputError")


def emit(payload: dict[str, Any]) -> None:
    """Write exactly one compact JSON object after serialization succeeds."""
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write(serialized + "\n")


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON keys instead of silently accepting the last value."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AdapterError(f"input contains duplicate JSON key: {key}.")
        result[key] = value
    return result


def read_envelope(input_path: str | None) -> dict[str, Any]:
    """Read one JSON value without mutating a caller-owned input file."""
    try:
        if input_path is None:
            raw = sys.stdin.read()
        else:
            raw = Path(input_path).read_text(encoding="utf-8")
    except OSError as exc:
        raise AdapterError(f"unable to read --input file: {exc}") from exc
    if not raw.strip():
        raise AdapterError("input is empty; provide one canonical JSON envelope via stdin or --input.")
    try:
        return validate_canonical_envelope(json.loads(raw, object_pairs_hook=_object_without_duplicate_keys))
    except json.JSONDecodeError as exc:
        raise AdapterError(f"input is not valid JSON: {exc.msg}.") from exc


def _build_parser() -> StrictArgumentParser:
    parser = StrictArgumentParser(add_help=False, description=__doc__)
    parser.add_argument("--profile", choices=sorted(PROFILE_ACTIONS))
    parser.add_argument("--input", metavar="PATH")
    parser.add_argument("--help", action="store_true")
    return parser


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not args.help and args.profile is None:
        raise AdapterError("--profile is required; choose one of: " + ", ".join(sorted(PROFILE_ACTIONS)) + ".")
    return args


def canonical_adapter_success(message: str, data: dict[str, Any]) -> dict[str, Any]:
    """Return the canonical success contract for adapter-owned commands."""
    return build_success(ADAPTER_ACTION, message, data)


def main(argv: list[str] | None = None) -> int:
    """Translate one envelope; translated failures keep a non-zero exit status."""
    profile: str | None = None
    try:
        args = parse_args(list(sys.argv[1:] if argv is None else argv))
    except AdapterError as exc:
        sys.stderr.write(_build_parser().format_usage())
        sys.stderr.write(f"error: {exc}\n")
        emit_json(canonical_adapter_error(str(exc)))
        return 2

    if args.help:
        sys.stderr.write(_build_parser().format_help())
        emit_json(canonical_adapter_success("Legacy CLI adapter help.", {"operation": "help"}))
        return 0

    try:
        profile = args.profile
        envelope = read_envelope(args.input)
        emit(translate(envelope, profile))
        return 0 if envelope["success"] else 1
    except AdapterError as exc:
        emit_json(canonical_adapter_error(str(exc), profile=profile))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
