"""Materiality gate and untrusted attachment handoff for mail-desk (MD-A4).

Purely declarative module: no LLM calls, no network, no external subprocesses.
Prepares hash-bound prompt context encapsulated in <untrusted_attachment_content>
with strict per-file (15,000 chars) and per-mail (30,000 chars) budgets.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from typing import Any, Mapping, Sequence


# ==============================================================================
# Constants & Allowed Values
# ==============================================================================

MAX_CHARS_PER_ATTACHMENT = 15_000
MAX_CHARS_PER_MAIL = 30_000

MATERIALITY_SUPPLEMENTARY = "supplementary"
MATERIALITY_REQUIRED_FOR_DECISION = "required_for_decision"
ALLOWED_MATERIALITY = {MATERIALITY_SUPPLEMENTARY, MATERIALITY_REQUIRED_FOR_DECISION}

HANDOFF_STATUS_READY = "ready"
HANDOFF_STATUS_BLOCKED = "blocked_on_required_attachment"
HANDOFF_STATUS_NO_ATTACHMENTS = "no_attachments"

PART_LOCATOR_REGEX = re.compile(r"^\d+(?:\.\d+)*$")
MIME_TYPE_REGEX = re.compile(r"^[a-zA-Z0-9!#$&^_.+-]+/[a-zA-Z0-9!#$&^_.+-]+$")
PROVENANCE_RFC822 = "rfc822_mime_inspection"

ALLOWED_EXTRACTION_STATUSES = {
    "extracted",
    "attachment_conversion_unavailable",
    "corrupt_attachment",
    "extraction_failed",
}

ALLOWED_QUALITIES = {
    "high",
    "medium",
    "mixed",
    "partial",
    "low",
}

ALLOWED_TRUNCATION_REASONS = {
    None,
    "",
    "max_chars_exceeded",
    "max_pages_exceeded",
    "ocr_page_limit_exceeded",
    "ocr_unavailable",
    "max_paragraphs_exceeded",
    "grid_limit_exceeded",
    "max_slides_exceeded",
    "timeout_exceeded",
}

# Pattern to detect closing or opening tag breakouts in untrusted text
CLOSING_TAG_PATTERN = re.compile(
    r"<\s*/?\s*untrusted_attachment_content\b[^>]*>",
    flags=re.IGNORECASE,
)


class InvalidMaterialityError(ValueError):
    """Raised when an invalid materiality value is provided."""


class AttachmentHandoffError(ValueError):
    """Raised when attachment handoff processing fails."""


class HandoffDriftError(AttachmentHandoffError):
    """Raised when an existing handoff does not match current mail identity, decision, or items."""


# ==============================================================================
# Sanitization & Escaping
# ==============================================================================

def escape_untrusted_content(text: str) -> str:
    """Sanitize untrusted attachment text against prompt injection breakouts.

    1. Removes null bytes.
    2. Replaces any attempt to close or spoof the XML tag <untrusted_attachment_content>.
    """
    if not text:
        return ""
    # Strip null bytes
    sanitized = text.replace("\x00", "")
    # Neutralize breakout tags
    sanitized = CLOSING_TAG_PATTERN.sub("[ESCAPED_UNTRUSTED_TAG]", sanitized)
    return sanitized


def _part_sort_key(part_locator: str | None, filename: str | None) -> tuple:
    """Generate a stable sort key for MIME parts."""
    loc = str(part_locator or "").strip()
    fn = str(filename or "").strip().lower()
    if loc:
        try:
            return (0, [int(p) for p in loc.split(".") if p.isdigit()], fn)
        except Exception:
            return (1, [loc], fn)
    return (2, [], fn)


CATALOG_DECISION_SCALARS = (
    "workpackage",
    "task",
    "deliverable",
    "milestone",
    "subtopic",
    "operation",
    "event",
)


def normalize_decision_snapshot(decision: Mapping[str, Any] | None) -> dict[str, Any]:
    """Create a canonical, deterministic snapshot of the routing decision."""
    base: dict[str, Any] = {
        "kind": "",
        "id": "",
        "confidence": "",
        "review_required": False,
        "review_reason": "",
        "needs_reply": False,
        "target_folder": "",
        "workpackage": "",
        "task": "",
        "deliverable": "",
        "milestone": "",
        "subtopic": "",
        "operation": "",
        "event": "",
    }
    if not isinstance(decision, Mapping):
        return base

    base["kind"] = str(decision.get("kind") or "").strip()
    base["id"] = str(decision.get("id") or "").strip()
    base["confidence"] = str(decision.get("confidence") or "").strip()
    base["review_required"] = bool(decision.get("review_required", False))
    base["review_reason"] = str(decision.get("review_reason") or "").strip()
    base["needs_reply"] = bool(decision.get("needs_reply", False))
    base["target_folder"] = str(decision.get("target_folder") or "").strip()

    for field in CATALOG_DECISION_SCALARS:
        base[field] = str(decision.get(field) or "").strip()

    return base


def _normalize_message_id(mid: str | None) -> str:
    """Normalize a message ID by stripping enclosing angle brackets and whitespace."""
    s = str(mid or "").strip()
    if s.startswith("<") and s.endswith(">"):
        return s[1:-1].strip()
    return s


def compute_handoff_hash(
    mail_identity: Mapping[str, Any],
    items: Sequence[Mapping[str, Any]],
    decision: Mapping[str, Any] | None = None,
) -> str:
    """Compute a deterministic 64-char SHA-256 hash binding mail identity, decision snapshot, and items."""
    norm_identity = {
        "account": str(mail_identity.get("account") or "").strip(),
        "message_id": _normalize_message_id(mail_identity.get("message_id")),
        "folder": str(mail_identity.get("folder") or "").strip(),
        "envelope_id": str(mail_identity.get("envelope_id") or "").strip(),
    }
    norm_decision = normalize_decision_snapshot(decision)

    canonical_dict = {
        "schema_version": 1,
        "mail_identity": norm_identity,
        "decision": norm_decision,
        "items": [
            {
                "part_locator": str(it.get("part_locator") or "").strip(),
                "filename": str(it.get("filename") or "").strip(),
                "source_sha256": str(it.get("source_sha256") or it.get("sha256") or "").strip().lower(),
                "mime_type": str(it.get("mime_type") or "").strip().lower(),
                "materiality": str(it.get("materiality") or "").strip(),
                "status": str(it.get("status") or "").strip(),
                "quality": str(it.get("quality") or "").strip().lower(),
                "char_count": int(it.get("char_count", 0)),
                "truncated": bool(it.get("truncated", False)),
                "truncation_reason": str(it.get("truncation_reason") or "").strip(),
                "content_hash": str(it.get("content_hash") or "").strip().lower(),
                "error": str(it.get("error") or "").strip(),
            }
            for it in items
        ],
    }
    encoded = json.dumps(canonical_dict, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded, usedforsecurity=False).hexdigest()


def validate_and_index_canonical_parts(
    canonical_parts: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Mapping[str, Any]]:
    """Validate external caller-provided canonical parts inventory and index by part_locator.

    Fails closed if:
    - canonical_parts is None or not a Sequence
    - any part is not a Mapping
    - part_locator is missing, invalid RFC-822, or duplicate
    - filename is missing, empty, whitespace, 'unknown_attachment', or contains null bytes
    - sha256 or source_sha256 is missing, non-hex, or not 64 chars
    - mime_type is missing or not a valid normalized MIME format
    - provenance is missing or not exactly PROVENANCE_RFC822 ('rfc822_mime_inspection')
    """
    if canonical_parts is None or not isinstance(canonical_parts, Sequence):
        raise AttachmentHandoffError(
            "canonical_parts must be a sequence of verified MD-A1/A2 parts mappings from caller"
        )

    parts_by_loc: dict[str, Mapping[str, Any]] = {}

    for idx, part in enumerate(canonical_parts):
        if not isinstance(part, Mapping):
            raise AttachmentHandoffError(f"Canonical part at index {idx} must be a mapping")

        raw_loc = part.get("part_locator")
        if raw_loc is None or not isinstance(raw_loc, str) or not PART_LOCATOR_REGEX.fullmatch(raw_loc.strip()):
            raise AttachmentHandoffError(f"Canonical part at index {idx} has missing or invalid part_locator: {raw_loc!r}")
        loc = raw_loc.strip()

        if loc in parts_by_loc:
            raise AttachmentHandoffError(f"Duplicate part_locator '{loc}' in canonical_parts")

        raw_fn = part.get("filename")
        if raw_fn is None or not isinstance(raw_fn, str) or not raw_fn.strip() or raw_fn.strip() == "unknown_attachment":
            raise AttachmentHandoffError(f"Canonical part '{loc}' has missing or invalid filename: {raw_fn!r}")
        filename = raw_fn.strip()
        if "\x00" in filename:
            raise AttachmentHandoffError(f"Canonical part '{loc}' filename contains forbidden null bytes")

        raw_sha = part.get("sha256") or part.get("source_sha256")
        if raw_sha is None or not isinstance(raw_sha, str) or not re.match(r"^[0-9a-fA-F]{64}$", raw_sha.strip()):
            raise AttachmentHandoffError(f"Canonical part '{loc}' has missing or invalid SHA-256 hash: {raw_sha!r}")
        sha = raw_sha.strip().lower()

        raw_mime = part.get("mime_type") or part.get("effective_mime_type")
        if raw_mime is None or not isinstance(raw_mime, str) or not MIME_TYPE_REGEX.fullmatch(raw_mime.strip().lower()):
            raise AttachmentHandoffError(f"Canonical part '{loc}' has missing or invalid normalized MIME type: {raw_mime!r}")
        mime = raw_mime.strip().lower()

        prov = part.get("provenance")
        if prov != PROVENANCE_RFC822:
            raise AttachmentHandoffError(
                f"Canonical part '{loc}' has missing or invalid provenance: {prov!r}, expected {PROVENANCE_RFC822!r}"
            )

        parts_by_loc[loc] = {
            "part_locator": loc,
            "filename": filename,
            "sha256": sha,
            "source_sha256": sha,
            "mime_type": mime,
            "provenance": PROVENANCE_RFC822,
        }

    return parts_by_loc


def verify_item_against_canonical_parts(
    item: Mapping[str, Any],
    canonical_parts_by_loc: Mapping[str, Mapping[str, Any]],
) -> None:
    """Explicitly verify that an item strictly matches caller-provided canonical MD-A1/A2 part metadata."""
    if not canonical_parts_by_loc:
        raise AttachmentHandoffError("Missing caller-verified canonical_parts for verification")

    loc = str(item.get("part_locator") or "").strip()
    if loc not in canonical_parts_by_loc:
        raise HandoffDriftError(
            f"Part locator '{loc}' not found in canonical MD-A1/A2 parts inventory"
        )

    cand = canonical_parts_by_loc[loc]
    c_fn = str(cand.get("filename") or "").strip()
    i_fn = str(item.get("filename") or "").strip()
    if c_fn and i_fn != c_fn:
        raise HandoffDriftError(
            f"Filename drift for part '{loc}': expected '{c_fn}', got '{i_fn}'"
        )

    c_sha = str(cand.get("sha256") or cand.get("source_sha256") or "").strip().lower()
    i_sha = str(item.get("source_sha256") or item.get("sha256") or "").strip().lower()
    if c_sha and i_sha != c_sha:
        raise HandoffDriftError(
            f"Hash drift for part '{loc}': expected '{c_sha}', got '{i_sha}'"
        )

    c_mime = str(cand.get("mime_type") or cand.get("effective_mime_type") or "").strip().lower()
    i_mime = str(item.get("mime_type") or "").strip().lower()
    if c_mime and i_mime != c_mime:
        raise HandoffDriftError(
            f"MIME drift for part '{loc}': expected '{c_mime}', got '{i_mime}'"
        )

    c_prov = cand.get("provenance")
    if c_prov != PROVENANCE_RFC822:
        raise HandoffDriftError(
            f"Canonical part '{loc}' has invalid provenance: '{c_prov}'"
        )


def render_untrusted_xml_block(
    part_locator: str,
    filename: str,
    source_sha256: str,
    mime_type: str,
    materiality: str,
    status: str,
    char_count: int,
    truncated: bool,
    safe_text: str,
) -> str:
    """Render the canonical untrusted XML block for an attachment."""
    xml_tag_attrs = (
        f'part_locator="{html.escape(part_locator)}" '
        f'filename="{html.escape(filename)}" '
        f'source_sha256="{html.escape(source_sha256)}" '
        f'sha256="{html.escape(source_sha256)}" '
        f'mime_type="{html.escape(mime_type)}" '
        f'materiality="{html.escape(materiality)}" '
        f'status="{html.escape(status)}" '
        f'char_count="{char_count}" '
        f'truncated="{str(truncated).lower()}"'
    )
    return (
        f"<untrusted_attachment_content {xml_tag_attrs}>\n"
        f"{safe_text}\n"
        f"</untrusted_attachment_content>"
    )


def extract_encapsulated_text_from_xml_block(xml_block: Any, filename: str) -> str:
    """Extract encapsulated text payload strictly between opening and closing XML tags."""
    if not isinstance(xml_block, str):
        raise AttachmentHandoffError(f"Handoff item '{filename}' is missing xml_block string")

    prefix_match = re.match(r"^<untrusted_attachment_content\b[^>]*>\n", xml_block)
    suffix = "\n</untrusted_attachment_content>"
    if not prefix_match or not xml_block.endswith(suffix):
        raise HandoffDriftError(f"Malformed xml_block structure in item '{filename}'")

    prefix_len = prefix_match.end()
    if len(xml_block) < prefix_len + len(suffix):
        raise HandoffDriftError(f"Malformed xml_block length in item '{filename}'")

    return xml_block[prefix_len:-len(suffix)]


# ==============================================================================
# Canonical MD-A3 Envelope Validation
# ==============================================================================

def validate_mda3_extraction_envelope(
    att: Mapping[str, Any],
    default_materiality: str | None = None,
    canonical_parts: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate that an attachment item strictly conforms to the canonical MD-A3 extraction contract.

    Fails closed if:
    - part_locator is missing, empty, or not a valid RFC-822 locator
    - filename is missing, empty, or 'unknown_attachment'
    - mime_type is missing, invalid, or unnormalized
    - sha256 or source_sha256 is missing, malformed (not 64-hex), or drifting
    - status is missing or not in ALLOWED_EXTRACTION_STATUSES
    - quality or truncation_reason has invalid/unrecognized values
    - materiality is invalid or missing
    - character_count drifts from text length (when both present)
    - canonical MD-A1/A2 part binding drifts
    """
    if not isinstance(att, Mapping):
        raise AttachmentHandoffError("Attachment item must be a mapping")

    raw_loc = att.get("part_locator")
    if raw_loc is None or not isinstance(raw_loc, str) or not PART_LOCATOR_REGEX.fullmatch(raw_loc.strip()):
        raise AttachmentHandoffError(
            f"Attachment item has missing or invalid RFC-822 part_locator: {raw_loc!r}"
        )
    part_loc = raw_loc.strip()

    raw_fn = att.get("filename")
    if raw_fn is None or not isinstance(raw_fn, str) or not raw_fn.strip() or raw_fn.strip() == "unknown_attachment":
        raise AttachmentHandoffError(
            f"Attachment item has missing or invalid filename: {raw_fn!r}"
        )
    filename = raw_fn.strip()
    if "\x00" in filename:
        raise AttachmentHandoffError(f"Attachment filename contains forbidden null bytes: {filename!r}")

    raw_mime = att.get("mime_type") or att.get("effective_mime_type")
    if raw_mime is None or not isinstance(raw_mime, str) or not MIME_TYPE_REGEX.fullmatch(raw_mime.strip().lower()):
        raise AttachmentHandoffError(
            f"Attachment '{filename}' has missing or invalid normalized mime_type: {raw_mime!r}"
        )
    mime = raw_mime.strip().lower()

    raw_prov = att.get("provenance")
    if raw_prov is not None and raw_prov != PROVENANCE_RFC822:
        raise AttachmentHandoffError(
            f"Attachment '{filename}' has invalid provenance: {raw_prov!r}, expected {PROVENANCE_RFC822!r}"
        )

    # 1. SHA-256 Hash Validation (source_sha256 canonical, sha256 checked against drift)
    source_sha = att.get("source_sha256")
    alt_sha = att.get("sha256")
    if source_sha is not None:
        source_sha = str(source_sha).strip().lower()
    if alt_sha is not None:
        alt_sha = str(alt_sha).strip().lower()

    if source_sha and alt_sha and source_sha != alt_sha:
        raise AttachmentHandoffError(
            f"Hash drift detected in attachment '{filename}': "
            f"source_sha256 '{source_sha}' does not match sha256 '{alt_sha}'"
        )

    verified_sha = source_sha or alt_sha
    if not verified_sha:
        raise AttachmentHandoffError(
            f"Attachment '{filename}' is missing required SHA-256 hash (source_sha256)"
        )
    if not re.match(r"^[0-9a-f]{64}$", verified_sha):
        raise AttachmentHandoffError(
            f"Attachment '{filename}' has invalid SHA-256 hash '{verified_sha}'. "
            f"Must be a 64-character lowercase hex string."
        )

    # 2. Status Validation
    raw_status = att.get("status")
    if raw_status is None:
        raise AttachmentHandoffError(f"Attachment '{filename}' is missing required extraction status")
    status = str(raw_status).strip()
    if status not in ALLOWED_EXTRACTION_STATUSES:
        raise AttachmentHandoffError(
            f"Attachment '{filename}' has invalid extraction status '{status}'. "
            f"Must be one of {sorted(ALLOWED_EXTRACTION_STATUSES)}"
        )

    # 3. Quality Validation
    raw_quality = att.get("quality")
    quality = None
    if raw_quality is not None:
        quality = str(raw_quality).strip().lower()
        if quality not in ALLOWED_QUALITIES:
            raise AttachmentHandoffError(
                f"Attachment '{filename}' has invalid extraction quality '{quality}'. "
                f"Must be one of {sorted(ALLOWED_QUALITIES)}"
            )

    # 4. Truncation Reason Validation
    raw_trunc = att.get("truncation_reason")
    trunc_reason = None
    if raw_trunc is not None and str(raw_trunc).strip() != "":
        trunc_reason = str(raw_trunc).strip()
        if trunc_reason not in ALLOWED_TRUNCATION_REASONS:
            raise AttachmentHandoffError(
                f"Attachment '{filename}' has invalid truncation_reason '{trunc_reason}'"
            )

    # 5. Materiality Validation
    mat = att.get("materiality") or default_materiality
    if mat not in ALLOWED_MATERIALITY:
        raise InvalidMaterialityError(
            f"Attachment {filename!r} has invalid or missing materiality: {mat!r}. "
            f"Must be one of {sorted(ALLOWED_MATERIALITY)}"
        )

    # 6. Raw Text & Character Count Validation
    raw_text = att.get("text")
    if raw_text is None:
        raw_text = att.get("extracted_text") or ""
    else:
        raw_text = str(raw_text)

    char_count = att.get("character_count")
    if char_count is not None:
        try:
            char_count_int = int(char_count)
            if char_count_int < 0:
                raise ValueError()
        except (ValueError, TypeError):
            raise AttachmentHandoffError(
                f"Attachment '{filename}' has invalid character_count: {char_count}"
            )
        if status == "extracted" and not att.get("truncated") and len(raw_text) != char_count_int:
            raise AttachmentHandoffError(
                f"Attachment '{filename}' character_count mismatch: "
                f"claimed {char_count_int}, text has {len(raw_text)}"
            )

    error_msg = att.get("error")
    if error_msg is not None:
        error_msg = str(error_msg).strip() or None

    item_dict = {
        "part_locator": part_loc,
        "filename": filename,
        "source_sha256": verified_sha,
        "sha256": verified_sha,
        "mime_type": mime,
        "provenance": PROVENANCE_RFC822,
        "materiality": mat,
        "status": status,
        "quality": quality,
        "truncation_reason": trunc_reason,
        "error": error_msg,
        "raw_text": raw_text,
        "truncated": bool(att.get("truncated", False)),
    }

    # 7. Explicit Canonical Part Binding (if external canonical_parts provided by caller)
    # att.canonical_part or att.canonical_parts MUST NEVER serve as validation anchor!
    if canonical_parts:
        if isinstance(canonical_parts, Mapping):
            parts_by_loc = canonical_parts
        else:
            parts_by_loc = validate_and_index_canonical_parts(canonical_parts)
        verify_item_against_canonical_parts(item_dict, parts_by_loc)

    return item_dict


def is_usable_extraction(norm_att: dict[str, Any]) -> tuple[bool, str | None]:
    """Determine whether an MD-A3 extraction result is complete and usable for decisions.

    Contract: Only status == 'extracted' without errors, without partial quality,
    and with non-empty extracted content constitutes a usable extraction.
    """
    status = norm_att["status"]
    error_msg = norm_att.get("error")
    quality = norm_att.get("quality")
    raw_text = norm_att.get("raw_text", "")

    if status != "extracted":
        return False, f"Extraction status '{status}' is not 'extracted'"
    if error_msg:
        return False, f"Extraction error reported: {error_msg}"
    if quality == "partial":
        return False, "Partial extraction quality ('partial')"
    if quality == "low" and not raw_text.strip():
        return False, "Low quality extraction with empty text"
    if not raw_text.strip():
        return False, "Extracted text content is empty"

    return True, None


# ==============================================================================
# Handoff Builder
# ==============================================================================

def build_attachment_analysis_handoff(
    mail_identity: Mapping[str, Any],
    attachments: Sequence[Mapping[str, Any]],
    decision: Mapping[str, Any] | None = None,
    *,
    canonical_parts: Sequence[Mapping[str, Any]] | None = None,
    default_materiality: str | None = None,
    max_chars_per_attachment: int = MAX_CHARS_PER_ATTACHMENT,
    max_chars_per_mail: int = MAX_CHARS_PER_MAIL,
) -> dict[str, Any]:
    """Build a hash-bound attachment_analysis_handoff from MD-A3 extraction results.

    - Enforces 15,000 char per-file budget INCLUDING visible marker.
    - Enforces 30,000 char cumulative per-mail budget INCLUDING visible marker.
    - Stably sorts attachments by MIME part locator / filename.
    - Encapsulates untrusted text inside <untrusted_attachment_content> tags with injection escaping.
    - Tracks failure status: supplementary failures are recorded but do not block;
      required_for_decision failures trigger blocked_on_required_attachment status
      independently of any residual text.
    - Binds canonical mail identity, decision snapshot, and items to handoff_hash.
    - Does NOT invoke LLMs, tools, or external services.
    """
    if not isinstance(mail_identity, Mapping):
        raise AttachmentHandoffError("mail_identity must be a mapping")

    norm_identity = {
        "account": str(mail_identity.get("account") or "").strip(),
        "message_id": _normalize_message_id(mail_identity.get("message_id")),
        "folder": str(mail_identity.get("folder") or ("INBOX" if not attachments else "")).strip(),
        "envelope_id": str(mail_identity.get("envelope_id") or "").strip(),
    }
    if attachments:
        for field in ("account", "message_id", "folder", "envelope_id"):
            if not norm_identity[field]:
                raise AttachmentHandoffError(
                    f"mail_identity must include non-empty '{field}' for non-empty attachment handoff"
                )
    elif not norm_identity["account"]:
        raise AttachmentHandoffError("mail_identity must include non-empty 'account'")

    norm_decision = normalize_decision_snapshot(decision)

    if default_materiality is not None and default_materiality not in ALLOWED_MATERIALITY:
        raise InvalidMaterialityError(
            f"Invalid default_materiality {default_materiality!r}. Must be one of {sorted(ALLOWED_MATERIALITY)}"
        )

    if not attachments:
        handoff_hash = compute_handoff_hash(norm_identity, [], decision=norm_decision)
        return {
            "schema_version": 1,
            "status": HANDOFF_STATUS_NO_ATTACHMENTS,
            "blocked_reason": None,
            "mail_identity": norm_identity,
            "decision_snapshot": norm_decision,
            "canonical_parts": [dict(p) for p in canonical_parts] if canonical_parts else None,
            "handoff_hash": handoff_hash,
            "total_attachments": 0,
            "total_chars": 0,
            "cumulative_chars_budget": max_chars_per_mail,
            "is_cumulative_truncated": False,
            "blocked_required_attachments": [],
            "items": [],
            "prompt_content": "",
        }

    if not canonical_parts:
        raise AttachmentHandoffError(
            "canonical_parts from verified caller is mandatory for non-empty attachment handoff"
        )
    parts_by_loc = validate_and_index_canonical_parts(canonical_parts)

    # Stably sort attachments by part_locator / filename
    sorted_attachments = sorted(
        attachments,
        key=lambda a: _part_sort_key(a.get("part_locator"), a.get("filename")),
    )

    remaining_mail_budget = max_chars_per_mail
    cumulative_chars = 0
    is_cumulative_truncated = False
    blocked_required: list[dict[str, Any]] = []
    handoff_items: list[dict[str, Any]] = []
    xml_blocks: list[str] = []

    for raw_att in sorted_attachments:
        norm_att = validate_mda3_extraction_envelope(
            raw_att,
            default_materiality=default_materiality,
            canonical_parts=parts_by_loc,
        )

        mat = norm_att["materiality"]
        filename = norm_att["filename"]
        part_loc = norm_att["part_locator"]
        verified_sha = norm_att["source_sha256"]
        mime = norm_att["mime_type"]
        status = norm_att["status"]
        quality = norm_att["quality"]
        trunc_reason = norm_att["truncation_reason"]
        error_msg = norm_att["error"]

        # Evaluate complete usability of extraction
        is_usable, failure_reason = is_usable_extraction(norm_att)
        if mat == MATERIALITY_REQUIRED_FOR_DECISION and not is_usable:
            blocked_required.append({
                "part_locator": part_loc,
                "filename": filename,
                "source_sha256": verified_sha,
                "sha256": verified_sha,
                "error": failure_reason or f"Extraction status: {status}",
            })

        safe_text = escape_untrusted_content(norm_att["raw_text"])
        is_truncated = norm_att["truncated"]
        item_trunc_reason = trunc_reason

        # 1. Enforce per-attachment limit INCLUDING visible marker
        file_marker = f"\n[... Truncated at {max_chars_per_attachment} characters ...]"
        if len(safe_text) > max_chars_per_attachment:
            allowed_len = max(0, max_chars_per_attachment - len(file_marker))
            safe_text = safe_text[:allowed_len] + file_marker
            is_truncated = True
            item_trunc_reason = "max_chars_exceeded"

        # 2. Enforce cumulative mail limit INCLUDING visible marker
        mail_marker = f"\n[... Truncated at cumulative {max_chars_per_mail} characters limit for mail ...]"
        if len(safe_text) > remaining_mail_budget:
            is_truncated = True
            is_cumulative_truncated = True
            item_trunc_reason = item_trunc_reason or "max_chars_exceeded"
            if remaining_mail_budget <= 0:
                safe_text = ""
            elif remaining_mail_budget >= len(mail_marker):
                allowed_len = remaining_mail_budget - len(mail_marker)
                safe_text = safe_text[:allowed_len] + mail_marker
                remaining_mail_budget = 0
            else:
                safe_text = mail_marker[:remaining_mail_budget]
                remaining_mail_budget = 0
        else:
            remaining_mail_budget -= len(safe_text)

        char_count = len(safe_text)
        cumulative_chars += char_count

        content_hash = hashlib.sha256(safe_text.encode("utf-8"), usedforsecurity=False).hexdigest()

        # Build XML encapsulation block
        xml_block = render_untrusted_xml_block(
            part_locator=part_loc,
            filename=filename,
            source_sha256=verified_sha,
            mime_type=mime,
            materiality=mat,
            status=status,
            char_count=char_count,
            truncated=is_truncated,
            safe_text=safe_text,
        )
        xml_blocks.append(xml_block)

        handoff_items.append({
            "part_locator": part_loc,
            "filename": filename,
            "source_sha256": verified_sha,
            "sha256": verified_sha,
            "mime_type": mime,
            "provenance": PROVENANCE_RFC822,
            "materiality": mat,
            "status": status,
            "quality": quality,
            "char_count": char_count,
            "truncated": is_truncated,
            "truncation_reason": item_trunc_reason,
            "content_hash": content_hash,
            "error": error_msg,
            "xml_block": xml_block,
        })

    handoff_hash = compute_handoff_hash(norm_identity, handoff_items, decision=norm_decision)

    overall_status = HANDOFF_STATUS_READY
    blocked_reason = None
    if blocked_required:
        overall_status = HANDOFF_STATUS_BLOCKED
        failures_summary = "; ".join(f"{b['filename']}: {b['error']}" for b in blocked_required)
        blocked_reason = f"Required attachment extraction failed: {failures_summary}"

    return {
        "schema_version": 1,
        "status": overall_status,
        "blocked_reason": blocked_reason,
        "mail_identity": norm_identity,
        "decision_snapshot": norm_decision,
        "canonical_parts": [dict(p) for p in parts_by_loc.values()],
        "handoff_hash": handoff_hash,
        "total_attachments": len(handoff_items),
        "total_chars": cumulative_chars,
        "cumulative_chars_budget": max_chars_per_mail,
        "is_cumulative_truncated": is_cumulative_truncated,
        "blocked_required_attachments": blocked_required,
        "items": handoff_items,
        "prompt_content": "\n\n".join(xml_blocks),
    }


# ==============================================================================
# Handoff Re-Validation & Verification
# ==============================================================================

def validate_attachment_handoff(
    handoff: Mapping[str, Any],
    mail_identity: Mapping[str, Any],
    decision: Mapping[str, Any] | None = None,
    *,
    canonical_parts: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Completely re-validate a pre-submitted attachment_analysis_handoff.

    Fails closed with HandoffDriftError or AttachmentHandoffError if:
    - handoff is not a valid Mapping or schema_version != 1
    - mail_identity drifts (account, message_id, folder, envelope_id)
    - decision drifts (kind, id, confidence, review_required, needs_reply)
    - items are missing, forged, or have invalid MD-A3 envelope values
    - part_locator is missing, empty, or not a valid RFC-822 locator
    - filename is missing, empty, or 'unknown_attachment'
    - mime_type is missing, invalid, or unnormalized
    - canonical MD-A1/A2 part binding drifts
    - content_hash does not match SHA-256 of encapsulated text in xml_block
    - xml_block does not match canonical reconstructed XML block from item fields
    - prompt_content does not match canonical reconstructed concatenation of xml_blocks
    - handoff_hash does not match recomputed hash
    - budget limits (15k per file, 30k cumulative) are violated
    """
    if not isinstance(handoff, Mapping):
        raise AttachmentHandoffError("Handoff must be a mapping")

    if handoff.get("schema_version") != 1:
        raise AttachmentHandoffError(f"Unsupported schema_version: {handoff.get('schema_version')}")

    items = handoff.get("items")
    if not isinstance(items, Sequence):
        raise AttachmentHandoffError("Handoff items must be a sequence")

    # 1. Validate Mail Identity
    h_id = handoff.get("mail_identity")
    if not isinstance(h_id, Mapping):
        raise HandoffDriftError("Handoff is missing mail_identity")

    if not isinstance(mail_identity, Mapping):
        raise HandoffDriftError("mail_identity must be a mapping")

    norm_mail_id = {
        "account": str(mail_identity.get("account") or "").strip(),
        "message_id": _normalize_message_id(mail_identity.get("message_id")),
        "folder": str(mail_identity.get("folder") or ("INBOX" if not items else "")).strip(),
        "envelope_id": str(mail_identity.get("envelope_id") or "").strip(),
    }

    if items:
        # Mandatory complete mail identity for non-empty attachment handoff
        for field in ("account", "message_id", "folder", "envelope_id"):
            if not norm_mail_id[field]:
                raise HandoffDriftError(
                    f"mail_identity must include non-empty '{field}' for non-empty attachment handoff"
                )
            h_field_val = (
                _normalize_message_id(h_id.get(field))
                if field == "message_id"
                else str(h_id.get(field) or "").strip()
            )
            if not h_field_val:
                raise HandoffDriftError(
                    f"Handoff mail_identity is missing non-empty '{field}'"
                )

    # Exact comparison without conditional bypass ('if expected_v' removed!)
    for k in ("account", "message_id", "folder", "envelope_id"):
        if k == "message_id":
            actual_v = _normalize_message_id(h_id.get(k))
        else:
            actual_v = str(h_id.get(k) or "").strip()
        expected_v = norm_mail_id[k]
        if actual_v != expected_v:
            raise HandoffDriftError(
                f"Mail identity drift for '{k}': expected '{expected_v}', got '{actual_v}'"
            )

    # 2. Validate Decision Snapshot
    norm_decision = normalize_decision_snapshot(decision)
    h_decision = handoff.get("decision_snapshot") or handoff.get("decision")
    if decision is not None:
        norm_h_decision = normalize_decision_snapshot(h_decision)
        if norm_decision != norm_h_decision:
            raise HandoffDriftError(
                f"Decision drift in handoff: current decision {norm_decision} does not match "
                f"handoff decision snapshot {norm_h_decision}"
            )

    # 3. Resolve & Verify Canonical Parts from Caller
    # handoff["canonical_parts"] MUST NEVER serve as validation anchor!
    parts_by_loc: dict[str, Mapping[str, Any]] = {}
    if items:
        if not canonical_parts:
            raise HandoffDriftError(
                "canonical_parts from verified caller is mandatory to validate non-empty attachment handoff"
            )
        parts_by_loc = validate_and_index_canonical_parts(canonical_parts)

        # Embedded canonical_parts in handoff is strictly hashed evidence; verify it against caller's inventory!
        embedded_parts = handoff.get("canonical_parts")
        if embedded_parts is not None:
            if not isinstance(embedded_parts, Sequence):
                raise HandoffDriftError("Embedded canonical_parts must be a sequence")
            embedded_by_loc: dict[str, Mapping[str, Any]] = {}
            for ep in embedded_parts:
                if not isinstance(ep, Mapping) or not ep.get("part_locator"):
                    raise HandoffDriftError("Malformed embedded canonical part in handoff")
                e_loc = str(ep["part_locator"]).strip()
                if e_loc in embedded_by_loc:
                    raise HandoffDriftError(f"Duplicate part_locator '{e_loc}' in embedded canonical_parts")
                embedded_by_loc[e_loc] = ep

            if set(embedded_by_loc.keys()) != set(parts_by_loc.keys()):
                raise HandoffDriftError(
                    f"Embedded canonical_parts locators {set(embedded_by_loc.keys())} do not match "
                    f"external verified locators {set(parts_by_loc.keys())}"
                )

            for loc, c_part in parts_by_loc.items():
                e_part = embedded_by_loc[loc]
                for field in ["filename", "sha256", "mime_type", "provenance"]:
                    c_val = str(c_part.get(field) or "").strip().lower()
                    e_val = str(e_part.get(field) or "").strip().lower()
                    if c_val != e_val:
                        raise HandoffDriftError(
                            f"Embedded canonical part '{loc}' drift in '{field}': "
                            f"expected '{c_val}', got '{e_val}'"
                        )
    elif canonical_parts and len(canonical_parts) > 0:
        raise HandoffDriftError(
            "Handoff has no items, but non-empty canonical_parts was provided by caller"
        )

    cumulative_chars = 0
    reconstructed_xml_blocks: list[str] = []

    for it in items:
        if not isinstance(it, Mapping):
            raise AttachmentHandoffError("Each handoff item must be a mapping")

        # Validate part_locator (RFC-822)
        raw_loc = it.get("part_locator")
        if raw_loc is None or not isinstance(raw_loc, str) or not PART_LOCATOR_REGEX.fullmatch(raw_loc.strip()):
            raise AttachmentHandoffError(f"Handoff item has missing or invalid RFC-822 part_locator: {raw_loc!r}")
        part_loc = raw_loc.strip()

        # Validate filename
        raw_fn = it.get("filename")
        if raw_fn is None or not isinstance(raw_fn, str) or not raw_fn.strip() or raw_fn.strip() == "unknown_attachment":
            raise AttachmentHandoffError(f"Handoff item has missing or invalid filename: {raw_fn!r}")
        filename = raw_fn.strip()
        if "\x00" in filename:
            raise AttachmentHandoffError(f"Filename contains forbidden null bytes: {filename!r}")

        # Validate mime_type
        raw_mime = it.get("mime_type") or it.get("effective_mime_type")
        if raw_mime is None or not isinstance(raw_mime, str) or not MIME_TYPE_REGEX.fullmatch(raw_mime.strip().lower()):
            raise AttachmentHandoffError(f"Handoff item '{filename}' has missing or invalid normalized mime_type: {raw_mime!r}")
        mime = raw_mime.strip().lower()

        # Validate provenance if present
        raw_prov = it.get("provenance")
        if raw_prov is not None and raw_prov != PROVENANCE_RFC822:
            raise AttachmentHandoffError(f"Handoff item '{filename}' has invalid provenance: {raw_prov!r}")

        # Validate hash
        source_sha = it.get("source_sha256")
        alt_sha = it.get("sha256")
        if source_sha is not None:
            source_sha = str(source_sha).strip().lower()
        if alt_sha is not None:
            alt_sha = str(alt_sha).strip().lower()
        if source_sha and alt_sha and source_sha != alt_sha:
            raise HandoffDriftError(f"Hash drift in handoff item '{filename}': source_sha256 '{source_sha}' != sha256 '{alt_sha}'")
        verified_sha = source_sha or alt_sha
        if not verified_sha or not re.match(r"^[0-9a-f]{64}$", verified_sha):
            raise AttachmentHandoffError(f"Handoff item '{filename}' has invalid SHA-256 hash: '{verified_sha}'")

        # Validate status
        status = str(it.get("status") or "").strip()
        if status not in ALLOWED_EXTRACTION_STATUSES:
            raise AttachmentHandoffError(f"Handoff item '{filename}' has invalid status: '{status}'")

        # Validate quality
        quality = it.get("quality")
        if quality is not None:
            quality = str(quality).strip().lower()
            if quality not in ALLOWED_QUALITIES:
                raise AttachmentHandoffError(f"Handoff item '{filename}' has invalid quality: '{quality}'")

        # Validate truncation_reason
        trunc_reason = it.get("truncation_reason")
        if trunc_reason is not None and str(trunc_reason).strip() != "":
            trunc_reason = str(trunc_reason).strip()
            if trunc_reason not in ALLOWED_TRUNCATION_REASONS:
                raise AttachmentHandoffError(f"Handoff item '{filename}' has invalid truncation_reason: '{trunc_reason}'")

        # Validate materiality
        mat = str(it.get("materiality") or "").strip()
        if mat not in ALLOWED_MATERIALITY:
            raise InvalidMaterialityError(f"Handoff item '{filename}' has invalid materiality: '{mat}'")

        # Validate char_count
        try:
            char_count = int(it.get("char_count", 0))
            if char_count < 0:
                raise ValueError()
        except (ValueError, TypeError):
            raise AttachmentHandoffError(f"Handoff item '{filename}' has invalid char_count: {it.get('char_count')}")

        if char_count > MAX_CHARS_PER_ATTACHMENT:
            raise AttachmentHandoffError(
                f"Item char_count {char_count} exceeds limit of {MAX_CHARS_PER_ATTACHMENT}"
            )
        cumulative_chars += char_count

        # Validate content_hash
        claimed_content_hash = str(it.get("content_hash") or "").strip().lower()
        if not re.match(r"^[0-9a-f]{64}$", claimed_content_hash):
            raise AttachmentHandoffError(f"Handoff item '{filename}' has invalid content_hash: '{claimed_content_hash}'")

        # Explicit verification against canonical MD-A1/A2 parts
        verify_item_against_canonical_parts(
            {
                "part_locator": part_loc,
                "filename": filename,
                "source_sha256": verified_sha,
                "mime_type": mime,
            },
            parts_by_loc,
        )

        # Extract and verify encapsulated text inside xml_block
        raw_xml_block = it.get("xml_block")
        encapsulated_text = extract_encapsulated_text_from_xml_block(raw_xml_block, filename)

        # Check content_hash against encapsulated text
        actual_content_hash = hashlib.sha256(
            encapsulated_text.encode("utf-8"), usedforsecurity=False
        ).hexdigest()
        if actual_content_hash != claimed_content_hash:
            raise HandoffDriftError(
                f"Content hash mismatch for item '{filename}': "
                f"claimed content_hash '{claimed_content_hash}' does not match "
                f"actual SHA-256 of encapsulated text '{actual_content_hash}'"
            )

        # Check char_count against encapsulated text
        if len(encapsulated_text) != char_count:
            raise HandoffDriftError(
                f"Character count mismatch for item '{filename}': "
                f"item claims {char_count}, but encapsulated text length is {len(encapsulated_text)}"
            )

        # Check prompt injection breakout safety in encapsulated text
        if escape_untrusted_content(encapsulated_text) != encapsulated_text:
            raise HandoffDriftError(
                f"Prompt injection breakout detected in encapsulated text for item '{filename}'"
            )

        # Reconstruct canonical xml_block and verify exact match
        reconstructed_xml_block = render_untrusted_xml_block(
            part_locator=part_loc,
            filename=filename,
            source_sha256=verified_sha,
            mime_type=mime,
            materiality=mat,
            status=status,
            char_count=char_count,
            truncated=bool(it.get("truncated", False)),
            safe_text=encapsulated_text,
        )
        if raw_xml_block != reconstructed_xml_block:
            raise HandoffDriftError(
                f"xml_block drift in item '{filename}': provided xml_block does not match "
                f"canonical reconstructed xml_block"
            )
        reconstructed_xml_blocks.append(reconstructed_xml_block)

    # 5. Validate Cumulative Budget & Total Chars
    if cumulative_chars > MAX_CHARS_PER_MAIL:
        raise AttachmentHandoffError(
            f"Cumulative characters {cumulative_chars} exceeds limit of {MAX_CHARS_PER_MAIL}"
        )

    if handoff.get("total_chars") != cumulative_chars:
        raise AttachmentHandoffError(
            f"Handoff total_chars ({handoff.get('total_chars')}) does not match sum of item char_counts ({cumulative_chars})"
        )

    # 6. Reconstruct Entire prompt_content and Verify Exact Match
    expected_prompt_content = "\n\n".join(reconstructed_xml_blocks)
    actual_prompt_content = str(handoff.get("prompt_content") or "")
    if actual_prompt_content != expected_prompt_content:
        raise HandoffDriftError(
            "prompt_content drift: handoff prompt_content does not match "
            "canonical reconstructed prompt_content from items"
        )

    # 7. Validate Hash Binding
    effective_decision = norm_decision if decision is not None else normalize_decision_snapshot(h_decision)
    recomputed_hash = compute_handoff_hash(norm_mail_id, items, decision=effective_decision)
    if handoff.get("handoff_hash") != recomputed_hash:
        raise HandoffDriftError(
            f"Handoff hash mismatch: claimed '{handoff.get('handoff_hash')}', recomputed '{recomputed_hash}'"
        )

    return dict(handoff)


# ==============================================================================
# Manifest & Item Application
# ==============================================================================

def apply_attachment_handoff_to_item(
    item: dict[str, Any],
    handoff: dict[str, Any],
) -> dict[str, Any]:
    """Apply an attachment_analysis_handoff to a manifest or classifier item.

    - Validates envelope_id and mail identity if present on item.
    - Binds handoff to item['attachment_analysis_handoff'].
    - If status == blocked_on_required_attachment:
      - Forces action to keep_in_folder in INBOX.
      - Sets decision['review_required'] = True.
      - Sets decision['review_reason'] = 'blocked_on_required_attachment'.
      - Sets decision['confidence'] = 'low'.
      - Prepends review note to item['notes'].
      - Crucially preserves decision['needs_reply'] unaltered!
    - If status == ready:
      - Keeps original action, decision, routing, and needs_reply unaltered.
    """
    if not isinstance(handoff, Mapping):
        raise AttachmentHandoffError("Handoff must be a mapping")

    item_env = item.get("envelope_id")
    h_env = handoff.get("mail_identity", {}).get("envelope_id")
    if item_env and h_env and str(item_env).strip() != str(h_env).strip():
        raise HandoffDriftError(
            f"Envelope ID drift: item has '{item_env}', handoff has '{h_env}'"
        )

    item["attachment_analysis_handoff"] = handoff

    if handoff.get("status") == HANDOFF_STATUS_BLOCKED:
        item["action"] = {
            "type": "keep_in_folder",
            "target_folder": "INBOX",
        }
        dec = item.get("decision")
        if not isinstance(dec, dict):
            dec = {}
            item["decision"] = dec

        # Preserve needs_reply exactly as determined before
        saved_needs_reply = dec.get("needs_reply", False)
        dec["review_required"] = True
        dec["review_reason"] = "blocked_on_required_attachment"
        dec["confidence"] = "low"
        dec["needs_reply"] = saved_needs_reply

        reason_str = handoff.get("blocked_reason") or "erforderlicher Anhang nicht verfügbar"
        existing_notes = str(item.get("notes") or "").strip()
        item["notes"] = f"[Review: Erforderlicher Anhang nicht extrahierbar ({reason_str})] {existing_notes}".strip()

    return item
