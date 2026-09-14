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
# Constants & Enums
# ==============================================================================

MAX_CHARS_PER_ATTACHMENT = 15_000
MAX_CHARS_PER_MAIL = 30_000

MATERIALITY_SUPPLEMENTARY = "supplementary"
MATERIALITY_REQUIRED_FOR_DECISION = "required_for_decision"
ALLOWED_MATERIALITY = {MATERIALITY_SUPPLEMENTARY, MATERIALITY_REQUIRED_FOR_DECISION}

HANDOFF_STATUS_READY = "ready"
HANDOFF_STATUS_BLOCKED = "blocked_on_required_attachment"
HANDOFF_STATUS_NO_ATTACHMENTS = "no_attachments"

# Pattern to detect closing or opening tag breakouts in untrusted text
CLOSING_TAG_PATTERN = re.compile(
    r"<\s*/?\s*untrusted_attachment_content\b[^>]*>",
    flags=re.IGNORECASE,
)


class InvalidMaterialityError(ValueError):
    """Raised when an invalid materiality value is provided."""


class AttachmentHandoffError(Exception):
    """Raised when attachment handoff processing fails."""


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


def compute_handoff_hash(
    mail_identity: Mapping[str, Any],
    items: Sequence[Mapping[str, Any]],
) -> str:
    """Compute a deterministic 64-char SHA-256 hash binding mail identity and attachment items."""
    canonical_dict = {
        "account": str(mail_identity.get("account") or "").strip(),
        "message_id": str(mail_identity.get("message_id") or "").strip(),
        "folder": str(mail_identity.get("folder") or "").strip(),
        "envelope_id": str(mail_identity.get("envelope_id") or "").strip(),
        "items": [
            {
                "part_locator": str(it.get("part_locator") or "").strip(),
                "filename": str(it.get("filename") or "").strip(),
                "sha256": str(it.get("sha256") or "").strip().lower(),
                "materiality": str(it.get("materiality") or "").strip(),
                "status": str(it.get("status") or "").strip(),
                "char_count": int(it.get("char_count", 0)),
                "truncated": bool(it.get("truncated", False)),
                "content_hash": str(it.get("content_hash") or "").strip().lower(),
            }
            for it in items
        ],
    }
    encoded = json.dumps(canonical_dict, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded, usedforsecurity=False).hexdigest()


# ==============================================================================
# Handoff Builder
# ==============================================================================

def build_attachment_analysis_handoff(
    mail_identity: Mapping[str, Any],
    attachments: Sequence[Mapping[str, Any]],
    decision: Mapping[str, Any] | None = None,
    *,
    default_materiality: str | None = None,
    max_chars_per_attachment: int = MAX_CHARS_PER_ATTACHMENT,
    max_chars_per_mail: int = MAX_CHARS_PER_MAIL,
) -> dict[str, Any]:
    """Build a hash-bound attachment_analysis_handoff from MD-A3 extraction results.

    - Enforces 15,000 char per-file budget.
    - Enforces 30,000 char cumulative per-mail budget.
    - Stably sorts attachments by MIME part locator / filename.
    - Encapsulates untrusted text inside <untrusted_attachment_content> tags with injection escaping.
    - Tracks failure status: supplementary failures are recorded but do not block;
      required_for_decision failures trigger blocked_on_required_attachment status.
    - Does NOT invoke LLMs, tools, or external services.
    """
    if not isinstance(mail_identity, Mapping):
        raise ValueError("mail_identity must be a mapping")

    norm_identity = {
        "account": str(mail_identity.get("account") or "").strip(),
        "message_id": str(mail_identity.get("message_id") or "").strip(),
        "folder": str(mail_identity.get("folder") or "INBOX").strip(),
        "envelope_id": str(mail_identity.get("envelope_id") or "").strip(),
    }
    if not norm_identity["account"]:
        raise ValueError("mail_identity must include non-empty account")

    if default_materiality is not None and default_materiality not in ALLOWED_MATERIALITY:
        raise InvalidMaterialityError(
            f"Invalid default_materiality {default_materiality!r}. Must be one of {sorted(ALLOWED_MATERIALITY)}"
        )

    if not attachments:
        handoff_hash = compute_handoff_hash(norm_identity, [])
        return {
            "schema_version": 1,
            "status": HANDOFF_STATUS_NO_ATTACHMENTS,
            "mail_identity": norm_identity,
            "handoff_hash": handoff_hash,
            "total_attachments": 0,
            "total_chars": 0,
            "cumulative_chars_budget": max_chars_per_mail,
            "is_cumulative_truncated": False,
            "blocked_required_attachments": [],
            "items": [],
            "prompt_content": "",
        }

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

    for att in sorted_attachments:
        mat = att.get("materiality") or default_materiality
        if mat not in ALLOWED_MATERIALITY:
            raise InvalidMaterialityError(
                f"Attachment {att.get('filename')!r} has invalid or missing materiality: {mat!r}. "
                f"Must be one of {sorted(ALLOWED_MATERIALITY)}"
            )

        filename = str(att.get("filename") or "unknown_attachment")
        part_loc = str(att.get("part_locator") or "")
        sha = str(att.get("sha256") or "")
        mime = str(att.get("mime_type") or "application/octet-stream")
        status = str(att.get("status") or "extracted")
        error_msg = att.get("error")

        # Determine if extraction failed
        is_failure = (
            status in ("attachment_conversion_unavailable", "error", "unavailable", "failed")
            or bool(error_msg)
        )

        raw_text = att.get("text") or att.get("extracted_text") or ""
        if is_failure and not raw_text:
            if mat == MATERIALITY_REQUIRED_FOR_DECISION:
                blocked_required.append({
                    "part_locator": part_loc,
                    "filename": filename,
                    "sha256": sha,
                    "error": error_msg or f"Extraction status: {status}",
                })

        safe_text = escape_untrusted_content(raw_text)
        is_truncated = bool(att.get("truncated", False))

        # Check per-attachment limit
        if len(safe_text) > max_chars_per_attachment:
            safe_text = safe_text[:max_chars_per_attachment] + "\n[... Truncated at 15000 characters ...]"
            is_truncated = True

        # Check cumulative mail limit
        if len(safe_text) > remaining_mail_budget:
            if remaining_mail_budget > 0:
                safe_text = safe_text[:remaining_mail_budget] + f"\n[... Truncated at cumulative {max_chars_per_mail} characters limit for mail ...]"
                remaining_mail_budget = 0
            else:
                safe_text = f"[... Truncated: cumulative budget of {max_chars_per_mail} characters exceeded ...]"
            is_truncated = True
            is_cumulative_truncated = True
        else:
            remaining_mail_budget -= len(safe_text)

        char_count = len(safe_text)
        cumulative_chars += char_count

        content_hash = hashlib.sha256(safe_text.encode("utf-8"), usedforsecurity=False).hexdigest()

        # Build XML encapsulation block
        xml_tag_attrs = (
            f'part_locator="{html.escape(part_loc)}" '
            f'filename="{html.escape(filename)}" '
            f'sha256="{html.escape(sha)}" '
            f'mime_type="{html.escape(mime)}" '
            f'materiality="{html.escape(mat)}" '
            f'status="{html.escape(status)}" '
            f'char_count="{char_count}" '
            f'truncated="{str(is_truncated).lower()}"'
        )
        xml_block = (
            f"<untrusted_attachment_content {xml_tag_attrs}>\n"
            f"{safe_text}\n"
            f"</untrusted_attachment_content>"
        )
        xml_blocks.append(xml_block)

        handoff_items.append({
            "part_locator": part_loc,
            "filename": filename,
            "sha256": sha,
            "mime_type": mime,
            "materiality": mat,
            "status": status,
            "char_count": char_count,
            "truncated": is_truncated,
            "content_hash": content_hash,
            "error": error_msg,
            "xml_block": xml_block,
        })

    handoff_hash = compute_handoff_hash(norm_identity, handoff_items)

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
# Manifest & Item Application
# ==============================================================================

def apply_attachment_handoff_to_item(
    item: dict[str, Any],
    handoff: dict[str, Any],
) -> dict[str, Any]:
    """Apply an attachment_analysis_handoff to a manifest or classifier item.

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
