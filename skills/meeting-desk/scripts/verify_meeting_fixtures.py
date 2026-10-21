#!/usr/bin/env python3
"""Read-only conformance check for the shared meeting-record fixture pack.

Adapters vendor ``tests/fixtures/meeting-record-v1.json`` byte-identically.
This helper verifies the manifest digest and runs every fixture record through
the canonical writer validation, so a silent fixture fork or a writer drift is
reported instead of skipped.

Exit codes: 0 all checks pass, 2 mismatch/invalid fixture. Standard six-field
JSON envelope on stdout; no workspace, no lease and no file mutation.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

ACTION = "verify_meeting_fixtures"
DEFAULT_FIXTURES = (Path(__file__).resolve().parents[1]
                    / "tests" / "fixtures" / "meeting-record-v1.json")
EXPECTED_SCHEMA = "meeting-record-fixtures/1"


def _load_writer():
    path = Path(__file__).with_name("ingest_meeting.py")
    spec = importlib.util.spec_from_file_location("_meeting_desk_fixture_writer", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _digest(pack: dict) -> str:
    covered = {key: pack[key] for key in
               ("valid_records", "invalid_records", "raw_invalid_json")}
    canonical = json.dumps(covered, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _envelope(success: bool, state: str, message: str, data: dict | None = None) -> dict:
    return {"action": ACTION, "success": success, "state": state,
            "message": message[:1000], "data": data or {},
            "error": None if success else {"type": state, "message": message[:1000]}}


def verify(path: Path) -> tuple[dict, int]:
    if not path.is_file():
        return _envelope(False, "Rejected", "fixture pack not found"), 2
    try:
        pack = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return _envelope(False, "Rejected", "fixture pack is not valid JSON"), 2
    if not isinstance(pack, dict) or pack.get("schema") != EXPECTED_SCHEMA:
        return _envelope(False, "Rejected", "unsupported fixture pack schema"), 2
    writer = _load_writer()
    data: dict = {"path": str(path), "record_schema": pack.get("record_schema"),
                  "writer_version": pack.get("writer_version"),
                  "valid": 0, "invalid": 0, "raw_invalid": 0}
    expected = (pack.get("manifest") or {}).get("digest")
    actual = _digest(pack)
    data["digest"] = actual
    if expected != actual:
        return _envelope(False, "DigestMismatch",
                         "fixture pack digest does not match its manifest", data), 2
    if pack.get("record_schema") != writer.RECORD_SCHEMA:
        return _envelope(False, "SchemaMismatch",
                         "fixture pack targets another record schema", data), 2
    for index, record in enumerate(pack.get("valid_records", [])):
        try:
            writer.validate_record(record)
        except Exception:
            return _envelope(False, "ValidRecordRejected",
                             f"valid_records[{index}] does not validate", data), 2
        data["valid"] += 1
    for index, item in enumerate(pack.get("invalid_records", [])):
        if not isinstance(item, dict) or "record" not in item:
            return _envelope(False, "InvalidRecordMalformed",
                             f"invalid_records[{index}] is malformed", data), 2
        try:
            writer.validate_record(item["record"])
        except Exception:
            data["invalid"] += 1
            continue
        return _envelope(False, "InvalidRecordAccepted",
                         f"invalid_records[{index}] unexpectedly validates", data), 2
    for index, item in enumerate(pack.get("raw_invalid_json", [])):
        try:
            writer.strict_json(item["raw"])
        except Exception:
            data["raw_invalid"] += 1
            continue
        return _envelope(False, "RawFixtureAccepted",
                         f"raw_invalid_json[{index}] unexpectedly parses", data), 2
    return _envelope(True, "Verified", "Fixture pack digest and records conform.", data), 0


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def _emit(envelope: dict) -> None:
    text = json.dumps(envelope, ensure_ascii=False, sort_keys=True) + "\n"
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        buffer.write(text.encode("utf-8"))
        buffer.flush()
    else:
        sys.stdout.write(text)
        sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description="Verify the shared meeting-record fixture pack.")
    parser.add_argument("--fixtures", default=str(DEFAULT_FIXTURES))
    parser.add_argument("--json", action="store_true", help="canonical JSON envelope")
    try:
        args = parser.parse_args(argv)
        envelope, code = verify(Path(args.fixtures))
    except (OSError, UnicodeError, ValueError):
        envelope, code = _envelope(False, "Rejected", "fixture verification failed"), 2
    _emit(envelope)
    return code


if __name__ == "__main__":
    sys.exit(main())
