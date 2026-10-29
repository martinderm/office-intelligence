"""Explicit test-only human approval fixtures; never used by runtime callers."""

from copy import deepcopy

from core.batch_contract import add_draft_contract, canonical_execute_request_sha256


def approved_execute(config, *, account=None):
    """Bind the complete test request, preserving all non-contract options."""
    request = deepcopy(config)
    items = request.get("items", [])
    add_draft_contract(
        request, expected_count=max(1, len(items)), allow_fewer=not items,
        source_folder=request.get("source_folder", "INBOX"),
        account=account, skip_known=True,
    )
    request["review"] = {
        "required": True, "state": "approved", "approval_receipt": {
            "reviewed_at": "2026-10-28T22:00:00Z", "reviewed_by": "test-human",
            "execute_request_sha256": canonical_execute_request_sha256(request),
        },
    }
    return request
