"""Shared canonical MD-E2 handoff fixtures for the draft-seam tests.

FR-15/MD-E2-T02 requires every ready handoff to be revalidated with the canonical
``validate_attachment_handoff`` before classification, so the T01/T02 draft-seam tests
must supply genuinely canonical (MD-E1-shaped) handoffs instead of hand-written stubs.
This module builds those fixtures deterministically; it is intentionally *not* named
``test_*.py`` so the unittest discovery pattern does not collect it as a suite.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from core import attachment_handoff as ahandoff  # noqa: E402

DEFAULT_TEXT = "Project PILOT kickoff next week"
DEFAULT_FILENAME = "clue.txt"
DEFAULT_PART_LOCATOR = "2"
DEFAULT_RUN_ID = "run-mde2-1"

#: (part_locator, filename, text, mime_type)
PartSpec = tuple[str, str, str, str]


def canonical_ready_evaluation(
    *,
    account: str,
    folder: str,
    envelope_id: str | int,
    message_id: str,
    decision: Mapping[str, Any],
    text: str = DEFAULT_TEXT,
    filename: str = DEFAULT_FILENAME,
    part_locator: str = DEFAULT_PART_LOCATOR,
    run_id: str = DEFAULT_RUN_ID,
    mime_type: str = "text/plain",
) -> dict[str, Any]:
    """Return a canonical MD-E1-shaped ready evaluation for one attachment."""
    return canonical_ready_evaluation_multi(
        account=account,
        folder=folder,
        envelope_id=envelope_id,
        message_id=message_id,
        decision=decision,
        parts=[(part_locator, filename, text, mime_type)],
        run_id=run_id,
    )


def canonical_ready_evaluation_multi(
    *,
    account: str,
    folder: str,
    envelope_id: str | int,
    message_id: str,
    decision: Mapping[str, Any],
    parts: Sequence[PartSpec],
    run_id: str = DEFAULT_RUN_ID,
) -> dict[str, Any]:
    """Return a canonical MD-E1-shaped ready evaluation for one or more attachments.

    The returned mapping is exactly the ``attachment_evaluate`` success envelope:
    ``{"attachment_evaluation": staged, "attachment_analysis_handoff": handoff}``.  The
    staged ``files[]`` mirror the validated handoff items, so the consumed-hash binding in
    production matches the staged records.
    """
    canonical_parts: list[dict[str, Any]] = []
    extractions: list[dict[str, Any]] = []
    for part_locator, filename, text, mime_type in parts:
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        canonical_parts.append(
            {
                "part_locator": part_locator,
                "filename": filename,
                "sha256": sha,
                "source_sha256": sha,
                "mime_type": mime_type,
                "provenance": ahandoff.PROVENANCE_RFC822,
            }
        )
        extractions.append(
            {
                "part_locator": part_locator,
                "filename": filename,
                "mime_type": mime_type,
                "source_sha256": sha,
                "status": "extracted",
                "quality": "high",
                "truncation_reason": None,
                "character_count": len(text),
                "source_character_count": len(text),
                "text": text,
                "error": None,
            }
        )
    handoff = ahandoff.build_attachment_analysis_handoff(
        mail_identity={
            "account": account,
            "message_id": message_id,
            "folder": folder,
            "envelope_id": str(envelope_id),
        },
        attachments=extractions,
        decision=decision,
        canonical_parts=canonical_parts,
        default_materiality=ahandoff.MATERIALITY_REQUIRED_FOR_DECISION,
    )
    files = [
        {
            "filename": item["filename"],
            "sha256": item["source_sha256"],
            "mime_type": item["mime_type"],
            "chars": item["char_count"],
            "coverage": "full" if item["analysis_completeness"] == "full" else "truncated",
            "run_id": run_id,
        }
        for item in handoff["items"]
    ]
    staged = {
        "status": "completed",
        "reason": "handoff_ready",
        "authorization": "auto_evaluated",
        "files": files,
        "used_for_classification": False,
        "classifier_revision": None,
    }
    return {"attachment_evaluation": staged, "attachment_analysis_handoff": handoff}
