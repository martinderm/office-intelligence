"""Review-bound candidate-count contract for standard mail-desk batches.

The contract is deliberately attached only to manifests produced by ``draft``.
Existing explicit autonomous pipelines and the separately reviewed FR-04 dossier
path keep their established authorization contracts.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RECEIPT_KEYS = {"reviewed_at", "reviewed_by", "execute_request_sha256"}
_CONTRACT_KEYS = {
    "expected_count", "allow_fewer", "candidate_count", "source_folder",
    "account", "skip_known", "review",
}
_CONTRACT_TRIGGER_KEYS = {"expected_count", "candidate_count", "allow_fewer", "review"}


def canonical_execute_request_sha256(config: object) -> str:
    """Hash the full execute request while deliberately excluding its review receipt."""
    if not isinstance(config, dict):
        raise ValueError("execute request must be an object")
    payload = dict(config)
    payload.pop("review", None)
    try:
        canonical = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("execute request must be canonical JSON data") from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def add_draft_contract(
    manifest: dict[str, Any],
    *,
    expected_count: int,
    allow_fewer: bool,
    source_folder: str,
    account: str | None,
    skip_known: bool,
) -> dict[str, Any]:
    """Attach the visible review contract to a freshly generated execute manifest."""
    manifest.update(
        {
            "expected_count": expected_count,
            "allow_fewer": allow_fewer,
            "candidate_count": len(manifest.get("items", [])),
            "source_folder": source_folder,
            "account": account,
            "skip_known": skip_known,
            "review": {"required": True, "state": "pending"},
        }
    )
    manifest["review"]["execute_request_sha256"] = canonical_execute_request_sha256(manifest)
    return manifest


def _failure(reason_code: str, message: str, **details: Any) -> dict[str, Any]:
    return {"ok": False, "reason_code": reason_code, "message": message, **details}


def validate_execute_contract(
    config: dict[str, Any], *, effective_account: str | None,
) -> dict[str, Any]:
    """Fail closed for a draft-generated manifest before any execute-side effect.

    A completely unbound legacy request is intentionally accepted so FR-04 and
    explicit pipeline callers retain their already-existing review boundaries.
    A request that declares any H2 field must satisfy the entire contract.
    """
    present = _CONTRACT_TRIGGER_KEYS.intersection(config)
    if not present:
        return {"ok": True, "state": "legacy_unbound", "candidate_count": None}
    missing = sorted(_CONTRACT_KEYS - set(config))
    if missing:
        return _failure(
            "incomplete_contract",
            "Execute manifest has an incomplete MD-H2 review contract.",
            missing=missing,
        )

    expected_count = config["expected_count"]
    if isinstance(expected_count, bool) or not isinstance(expected_count, int) or expected_count < 1:
        return _failure("invalid_expected_count", "expected_count must be a positive integer.")
    if not isinstance(config["allow_fewer"], bool):
        return _failure("invalid_allow_fewer", "allow_fewer must be a boolean.")
    if not isinstance(config["skip_known"], bool):
        return _failure("invalid_skip_known", "skip_known must be a boolean.")
    source_folder = config["source_folder"]
    if not isinstance(source_folder, str) or not source_folder.strip():
        return _failure("invalid_source_folder", "source_folder must be a non-empty string.")
    reviewed_account = config["account"]
    if reviewed_account is not None and (not isinstance(reviewed_account, str) or not reviewed_account.strip()):
        return _failure("invalid_account", "account must be null or a non-empty string.")
    if reviewed_account != effective_account:
        return _failure(
            "account_mismatch",
            "The effective account is not the account bound into the reviewed manifest.",
            reviewed_account=reviewed_account,
            effective_account=effective_account,
        )

    items = config.get("items")
    if not isinstance(items, list):
        return _failure("invalid_items", "Execute items must be a list.")
    selected_count = len(items)
    candidate_count = config["candidate_count"]
    if isinstance(candidate_count, bool) or not isinstance(candidate_count, int) or candidate_count < 0:
        return _failure("invalid_candidate_count", "candidate_count must be a non-negative integer.")
    if candidate_count != selected_count:
        return _failure(
            "candidate_count_mismatch",
            "candidate_count must equal the number of selected manifest items.",
            candidate_count=candidate_count, selected_count=selected_count,
        )
    if selected_count > expected_count:
        return _failure(
            "more_than_expected",
            "Selected candidate count exceeds expected_count; allow_fewer never permits extra items.",
            expected_count=expected_count, selected_count=selected_count,
        )
    if selected_count < expected_count and not config["allow_fewer"]:
        return _failure(
            "fewer_than_expected",
            "Selected candidate count is below expected_count; set allow_fewer=true only after review.",
            expected_count=expected_count, selected_count=selected_count,
        )
    for position, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            return _failure("invalid_item", f"Manifest item {position} must be an object.")
        if item.get("source_folder", source_folder) != source_folder:
            return _failure(
                "source_folder_mismatch",
                "Every selected item must retain the reviewed source_folder.",
                item_position=position, source_folder=source_folder,
            )

    review = config["review"]
    if not isinstance(review, dict) or review.get("required") is not True or review.get("state") != "approved":
        return _failure(
            "review_required",
            "Execute requires an explicitly approved review receipt bound to this exact manifest.",
        )
    receipt = review.get("approval_receipt")
    if not isinstance(receipt, dict) or set(receipt) != _RECEIPT_KEYS:
        return _failure("invalid_review_receipt", "review approval_receipt has an invalid shape.")
    if not all(isinstance(receipt[key], str) and receipt[key].strip() for key in _RECEIPT_KEYS):
        return _failure("invalid_review_receipt", "review approval_receipt fields must be non-empty strings.")
    approved_hash = receipt["execute_request_sha256"]
    if not _SHA256.fullmatch(approved_hash) or approved_hash != canonical_execute_request_sha256(config):
        return _failure(
            "review_hash_mismatch",
            "review approval receipt does not bind this exact execute manifest.",
        )
    return {
        "ok": True,
        "state": "reviewed",
        "expected_count": expected_count,
        "selected_count": selected_count,
        "allow_fewer": config["allow_fewer"],
    }
