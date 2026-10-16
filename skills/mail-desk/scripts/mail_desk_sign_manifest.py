#!/usr/bin/env python3
"""Re-sign a reviewed mail-desk ``batch-manifest.json`` for ``execute``.

After a reviewer edits the selected items of a draft manifest, the pending
``review`` block no longer binds the request. This standard-library helper
applies the canonical review-correction step without reading any source:

    python -B scripts/mail_desk_sign_manifest.py <manifest.json> \\
        --reviewed-by <name> [--reviewed-at <ISO 8601>] [--workspace <root>]

It recomputes ``candidate_count`` from ``len(items)``, rebuilds the canonical
execute-request hash through ``core.batch_contract``, replaces the pending
``review`` with an approved approval receipt (exactly ``reviewed_at``,
``reviewed_by``, ``execute_request_sha256``), self-checks the result with
``validate_execute_contract``, and writes the manifest back atomically. It emits
one machine-readable JSON envelope and never contacts a mailbox or mutates any
other workspace file. The manifest path resolves like any CLI argument, with an
optional ``--workspace`` fallback for a workspace-relative path.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from core.batch_contract import (
    canonical_execute_request_sha256,
    validate_execute_contract,
)
from core.common import atomic_write_json, utc_now_iso
from core.envelope import build_error, build_success, emit_json


ACTION = "mail_desk_sign_manifest"

#: A manifest only participates in the review-bound contract when these keys
#: are present; the full contract is then enforced by the self-check.
_TRIGGER_KEYS = ("expected_count", "candidate_count", "allow_fewer", "review")


class SignManifestError(Exception):
    """Raised when a manifest cannot be signed without breaking the contract."""

    def __init__(
        self,
        message: str,
        *,
        exit_code: int = 1,
        error_type: str = "SignManifestError",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.error_type = error_type
        self.details = details or {}


def build_signed_manifest(
    manifest: object,
    *,
    reviewed_by: str,
    reviewed_at: str,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """Return ``(signed manifest, execute_request_sha256, self-check result)``.

    Pure: performs no I/O. Raises ``SignManifestError`` when the input does not
    carry the review-bound contract or the self-check fails closed.
    """
    if not isinstance(manifest, dict):
        raise SignManifestError(
            "Manifest must be a JSON object.",
            exit_code=2,
            error_type="ManifestShapeError",
        )

    reviewer = (reviewed_by or "").strip()
    if not reviewer:
        raise SignManifestError("--reviewed-by must be a non-empty reviewer name.")

    timestamp = (reviewed_at or "").strip()
    if not timestamp:
        raise SignManifestError("--reviewed-at must be a non-empty ISO-8601 timestamp.")

    missing = [key for key in _TRIGGER_KEYS if key not in manifest]
    if missing:
        raise SignManifestError(
            "Manifest does not carry the review-bound execute contract.",
            details={"missing": missing},
        )

    items = manifest.get("items")
    if not isinstance(items, list) or not items:
        raise SignManifestError("Manifest items must be a non-empty list.")

    signed = dict(manifest)
    signed["candidate_count"] = len(items)
    # The canonical helper deliberately excludes the review block, so the hash
    # is computed before the approved receipt is attached and after the count
    # is corrected; any prior pending hash is discarded with the old review.
    execute_request_sha256 = canonical_execute_request_sha256(signed)
    signed["review"] = {
        "required": True,
        "state": "approved",
        "approval_receipt": {
            "reviewed_at": timestamp,
            "reviewed_by": reviewer,
            "execute_request_sha256": execute_request_sha256,
        },
    }

    self_check = validate_execute_contract(signed, effective_account=signed.get("account"))
    if self_check.get("ok") is not True:
        raise SignManifestError(
            "Signed manifest failed its own execute-contract self-check.",
            details={"reason_code": self_check.get("reason_code"), "self_check": self_check},
        )
    return signed, execute_request_sha256, self_check


def _resolve_manifest(raw: str, workspace: Path) -> Path:
    """Resolve the manifest like a CLI argument, with a workspace fallback."""
    candidate = Path(raw).expanduser()
    if candidate.is_absolute() or candidate.exists():
        return candidate.resolve()
    workspace_candidate = workspace / candidate
    if workspace_candidate.exists():
        return workspace_candidate.resolve()
    return candidate.resolve()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("manifest", help="Path to the reviewed batch-manifest.json to sign.")
    parser.add_argument("--reviewed-by", required=True, help="Non-empty reviewer name bound into the receipt.")
    parser.add_argument("--reviewed-at", default=None, help="ISO-8601 review timestamp (default: current UTC).")
    parser.add_argument("--workspace", default=".", help="workspace root used to resolve a relative manifest (default: current directory)")
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).expanduser().resolve()
    path = _resolve_manifest(args.manifest, workspace)
    if not path.is_file():
        emit_json(build_error(
            ACTION, "Manifest file not found.",
            {"path": str(path)},
            error_type="NotFound",
        ))
        return 1

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        emit_json(build_error(
            ACTION, "Manifest is not readable JSON.",
            {"path": str(path)},
            error_type="ManifestError",
            error_details={"reason": str(exc)[:1000]},
        ))
        return 2

    reviewed_at = (args.reviewed_at or "").strip() or utc_now_iso()
    try:
        signed, execute_request_sha256, self_check = build_signed_manifest(
            data, reviewed_by=args.reviewed_by, reviewed_at=reviewed_at
        )
    except SignManifestError as exc:
        emit_json(build_error(
            ACTION, str(exc),
            {"path": str(path), **exc.details},
            error_type=exc.error_type,
        ))
        return exc.exit_code

    atomic_write_json(path, signed)
    emit_json(build_success(
        ACTION, "Manifest re-signed for execute.",
        {
            "ok": True,
            "path": str(path),
            "candidate_count": signed["candidate_count"],
            "execute_request_sha256": execute_request_sha256,
            "reviewed_at": signed["review"]["approval_receipt"]["reviewed_at"],
            "reviewed_by": signed["review"]["approval_receipt"]["reviewed_by"],
            "self_check": self_check,
        },
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
