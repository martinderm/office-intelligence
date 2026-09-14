"""Structured read-only MIME attachment inventory, candidate binding, and drift detection.

Part of FR-08 (MD-A1).
"""

from __future__ import annotations

import email
from email.message import EmailMessage
import hashlib
from pathlib import Path
import re
from typing import Any

from .attachment_policy import (
    DEFAULT_ATTACHMENT_POLICY,
    check_attachment_policy,
    sanitize_attachment_filename,
)
from .common import normalize_message_id


# ==============================================================================
# Constants & Validation
# ==============================================================================

PROVENANCE_RFC822 = "rfc822_mime_inspection"
PART_LOCATOR_REGEX = re.compile(r"^\d+(?:\.\d+)*$")
MIME_TYPE_REGEX = re.compile(r"^[a-zA-Z0-9!#$&^_.+-]+/[a-zA-Z0-9!#$&^_.+-]+$")
SHA256_HEX_REGEX = re.compile(r"^[0-9a-fA-F]{64}$")


class AttachmentInventoryValidationError(ValueError):
    """Raised when an attachment candidate fails strict RFC-822 inventory validation."""


def validate_attachment_candidate_metadata(item: dict[str, Any]) -> tuple[bool, str | None]:
    """Strictly validate that an attachment candidate comes from authentic RFC-822 MIME inspection.

    Rejects:
    - Non-dictionary items
    - Missing or invalid internal provenance (must be PROVENANCE_RFC822)
    - Missing or invalid part locator (must match PART_LOCATOR_REGEX; no invented locators allowed)
    - Non-positive or non-integer size
    - Missing or invalid SHA-256 (must be 64-char hex string)
    - Missing or invalid MIME type (must match MIME_TYPE_REGEX)
    - Forged fetch_status (only 'available' is legitimate for inspected parts)
    """
    if not isinstance(item, dict):
        return False, "Attachment item must be a dictionary"

    # 1. Provenance check
    prov = item.get("provenance")
    if prov != PROVENANCE_RFC822:
        return False, f"Missing or invalid provenance: expected {PROVENANCE_RFC822!r}, got {prov!r}"

    # 2. Part locator check (strictly digits separated by dots, e.g. '1', '2', '1.2')
    locator = item.get("part_locator")
    if not locator or not isinstance(locator, str) or not PART_LOCATOR_REGEX.fullmatch(locator.strip()):
        return False, f"Missing or invalid part_locator: {locator!r}"

    # 3. Positive integer size check
    size = item.get("size_bytes")
    if size is None or isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        return False, f"size_bytes must be a positive integer, got {size!r}"

    # 4. 64-character SHA-256 hex string check
    sha = item.get("sha256")
    if not sha or not isinstance(sha, str) or not SHA256_HEX_REGEX.fullmatch(sha.strip()):
        return False, f"sha256 must be a 64-character hex string, got {sha!r}"

    # 5. Normalized MIME type check
    mime = item.get("mime_type")
    if not mime or not isinstance(mime, str) or not MIME_TYPE_REGEX.fullmatch(mime.strip()):
        return False, f"mime_type must be a valid 'type/subtype' format, got {mime!r}"

    # 6. Fetch status check: cannot assert forged status such as 'downloaded' or 'verified'
    fetch_st = item.get("fetch_status")
    if fetch_st is not None and fetch_st != "available":
        return False, f"Forged or invalid fetch_status: {fetch_st!r} (must be 'available')"

    return True, None


# ==============================================================================
# Drift Exceptions
# ==============================================================================

class AttachmentDriftError(ValueError):
    """Base exception raised when an attachment candidate drifts from verified state."""


class AccountDriftError(AttachmentDriftError):
    """Raised when the attachment candidate account drifts from the bound account."""


class MessageIdDriftError(AttachmentDriftError):
    """Raised when the normalized Message-ID drifts."""


class LocationDriftError(AttachmentDriftError):
    """Raised when folder or envelope ID location drifts."""


class PartLocatorDriftError(AttachmentDriftError):
    """Raised when the part locator no longer matches the message structure."""


class HashDriftError(AttachmentDriftError):
    """Raised when attachment payload SHA-256 hash drifts."""


# ==============================================================================
# MIME Inspection
# ==============================================================================

def extract_referenced_cids(msg: Any) -> set[str]:
    """Scan message text and HTML parts for referenced Content-IDs (cid:...)."""
    cids: set[str] = set()
    for part in msg.walk():
        if part.is_multipart():
            continue
        ct = part.get_content_type().lower()
        if ct in {"text/html", "text/plain"}:
            try:
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    charset = part.get_content_charset() or "utf-8"
                    payload_str = payload.decode(charset, errors="replace")
                else:
                    payload_str = str(payload or "")
                # Find all cid:... references in HTML src/href and text
                for match in re.findall(r'cid:([^\s"\'<>)]+)', payload_str, re.IGNORECASE):
                    cleaned = match.strip().strip("<>").strip()
                    if cleaned:
                        cids.add(cleaned)
            except Exception:
                pass
    return cids


def _traverse_part_locators(current_part: Any, current_locator: str) -> list[tuple[str, Any]]:
    """Recursively assign hierarchical IMAP-style locators to MIME leaf parts."""
    locators: list[tuple[str, Any]] = []
    if current_part.is_multipart():
        subparts = current_part.get_payload()
        if isinstance(subparts, list):
            for idx, subpart in enumerate(subparts, 1):
                sub_loc = f"{current_locator}.{idx}" if current_locator else str(idx)
                locators.extend(_traverse_part_locators(subpart, sub_loc))
        elif subparts is not None:
            sub_loc = f"{current_locator}.1" if current_locator else "1"
            locators.append((sub_loc, current_part))
    else:
        locators.append((current_locator or "1", current_part))
    return locators


def inspect_mime_tree(
    raw_eml: bytes | str,
    policy: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Parse raw RFC 822 MIME bytes and return structured attachment inventory.

    Strictly inspects MIME headers and structure; untrusted body text is never
    scanned for fabricated attachment markers.
    """
    if isinstance(raw_eml, str):
        eml_bytes = raw_eml.encode("utf-8", errors="replace")
    else:
        eml_bytes = bytes(raw_eml)

    if not eml_bytes.strip():
        return []

    msg = email.message_from_bytes(eml_bytes, policy=email.policy.default)
    pol = policy or DEFAULT_ATTACHMENT_POLICY

    referenced_cids = extract_referenced_cids(msg)
    leaf_parts = _traverse_part_locators(msg, "")

    attachments: list[dict[str, Any]] = []
    cumulative_bytes = 0

    for locator, part in leaf_parts:
        content_type = part.get_content_type().lower()
        content_disposition = part.get_content_disposition()
        raw_filename = part.get_filename()

        raw_cid = part.get("Content-ID", "")
        cid = raw_cid.strip().strip("<>").strip() if raw_cid else None

        # Check if this part is an inline asset
        is_inline = False
        if content_type.startswith("image/") or content_disposition == "inline":
            # STRICT: An image is ONLY inline if its concrete Content-ID is referenced in the body!
            if cid and cid in referenced_cids:
                is_inline = True

        # Determine if this part is an attachment or asset
        is_attachment = False
        if content_disposition == "attachment":
            is_attachment = True
        elif is_inline:
            is_attachment = True
        elif raw_filename:
            is_attachment = True
        elif not content_type.startswith("text/"):
            # Non-text payload without disposition is treated as an attachment/asset
            is_attachment = True

        if not is_attachment:
            # Main text or HTML body of the message; not an attachment
            continue

        # Extract decoded payload, size and hash
        payload = part.get_payload(decode=True)
        if isinstance(payload, bytes):
            size_bytes = len(payload)
            sha256 = hashlib.sha256(payload).hexdigest()
        else:
            size_bytes = None
            sha256 = None

        clean_filename = sanitize_attachment_filename(raw_filename or f"part_{locator.replace('.', '_')}")
        current_idx = len(attachments)

        policy_status, policy_reason = check_attachment_policy(
            filename=clean_filename,
            mime_type=content_type,
            size_bytes=size_bytes,
            current_index=current_idx,
            cumulative_bytes=cumulative_bytes,
            policy=pol,
        )

        if policy_status == "allowed" and size_bytes:
            cumulative_bytes += size_bytes

        entry: dict[str, Any] = {
            "filename": clean_filename,
            "mime_type": content_type,
            "size_bytes": size_bytes,
            "sha256": sha256,
            "part_locator": locator,
            "content_id": cid,
            "content_disposition": content_disposition or ("inline" if is_inline else "attachment"),
            "fetch_status": "available",
            "policy_status": policy_status,
            "is_inline": is_inline,
            "provenance": PROVENANCE_RFC822,
        }
        if policy_reason:
            entry["policy_reason"] = policy_reason

        attachments.append(entry)

    return attachments


# ==============================================================================
# Candidate Binding & Drift Verification
# ==============================================================================

def bind_attachment_candidate(
    attachment: dict[str, Any],
    account: str | None,
    folder: str,
    envelope_id: str | int,
    message_id: str,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind an attachment candidate strictly to account, folder, envelope_id, and Message-ID.

    Re-evaluates policy status canonically to prevent unverified input from asserting 'allowed'.
    Rejects candidates that fail strict RFC-822 inventory validation.
    """
    if not account or not str(account).strip():
        raise ValueError("Cannot bind attachment candidate: account is required.")
    if not message_id or not str(message_id).strip():
        raise ValueError("Cannot bind attachment candidate: normalized message_id is required.")
    if not folder or not str(folder).strip():
        raise ValueError("Cannot bind attachment candidate: folder is required.")
    if envelope_id is None or not str(envelope_id).strip():
        raise ValueError("Cannot bind attachment candidate: envelope_id is required.")

    valid, reason = validate_attachment_candidate_metadata(attachment)
    if not valid:
        raise AttachmentInventoryValidationError(f"Cannot bind invalid candidate: {reason}")

    norm_mid = normalize_message_id(message_id)
    clean_filename = sanitize_attachment_filename(attachment.get("filename"))
    pol = policy or DEFAULT_ATTACHMENT_POLICY

    size_bytes = int(attachment["size_bytes"])
    mime_type = str(attachment["mime_type"]).strip().lower()

    policy_status, policy_reason = check_attachment_policy(
        filename=clean_filename,
        mime_type=mime_type,
        size_bytes=size_bytes,
        policy=pol,
    )

    bound = dict(attachment)
    bound["filename"] = clean_filename
    bound["mime_type"] = mime_type
    bound["size_bytes"] = size_bytes
    bound["sha256"] = str(attachment["sha256"]).strip().lower()
    bound["part_locator"] = str(attachment["part_locator"]).strip()
    bound["fetch_status"] = "available"
    bound["provenance"] = PROVENANCE_RFC822
    bound["account"] = str(account).strip()
    bound["folder"] = str(folder).strip()
    bound["envelope_id"] = str(envelope_id).strip()
    bound["message_id"] = norm_mid
    bound["policy_status"] = policy_status
    if policy_reason:
        bound["policy_reason"] = policy_reason
    else:
        bound.pop("policy_reason", None)

    return bound


def canonicalize_and_bind_attachments(
    raw_attachments: list[dict[str, Any]],
    account: str | None,
    folder: str,
    envelope_id: str | int,
    message_id: str,
    policy: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Validate and canonically bind attachment items to account, folder, envelope_id, and Message-ID.

    NEVER trusts caller-supplied policy_status, invented part-locators, or unverified MIME metadata.
    Accepts strictly validated inventories from RFC-822 inspection with authentic provenance,
    valid part locator, positive integer size, 64-char SHA-256, and normalized MIME type.
    Recomputes policy_status via check_attachment_policy with cumulative size tracking.
    Fails closed if account or message_id is missing, or if candidates fail validation.
    """
    if not account or not str(account).strip():
        raise ValueError("Missing account: cannot bind attachment inventory to manifest without verified account.")
    if not message_id or not str(message_id).strip():
        raise ValueError("Cannot bind attachment candidate: normalized message_id is required.")
    if not folder or not str(folder).strip():
        raise ValueError("Cannot bind attachment candidate: folder is required.")
    if envelope_id is None or not str(envelope_id).strip():
        raise ValueError("Cannot bind attachment candidate: envelope_id is required.")

    norm_mid = normalize_message_id(message_id)
    pol = policy or DEFAULT_ATTACHMENT_POLICY

    bound_list: list[dict[str, Any]] = []
    cumulative_bytes = 0

    for idx, item in enumerate(raw_attachments):
        valid, reason = validate_attachment_candidate_metadata(item)
        if not valid:
            raise AttachmentInventoryValidationError(
                f"Invalid attachment candidate at index {idx}: {reason}"
            )

        clean_filename = sanitize_attachment_filename(item.get("filename"))
        mime_type = str(item["mime_type"]).strip().lower()
        size_bytes = int(item["size_bytes"])
        sha256 = str(item["sha256"]).strip().lower()
        part_locator = str(item["part_locator"]).strip()

        policy_status, policy_reason = check_attachment_policy(
            filename=clean_filename,
            mime_type=mime_type,
            size_bytes=size_bytes,
            current_index=idx,
            cumulative_bytes=cumulative_bytes,
            policy=pol,
        )

        if policy_status == "allowed":
            cumulative_bytes += size_bytes

        bound: dict[str, Any] = {
            "filename": clean_filename,
            "mime_type": mime_type,
            "size_bytes": size_bytes,
            "sha256": sha256,
            "part_locator": part_locator,
            "content_id": item.get("content_id"),
            "content_disposition": item.get("content_disposition") or ("inline" if item.get("is_inline") else "attachment"),
            "fetch_status": "available",
            "policy_status": policy_status,
            "is_inline": bool(item.get("is_inline", False)),
            "provenance": PROVENANCE_RFC822,
            "account": str(account).strip(),
            "folder": str(folder).strip(),
            "envelope_id": str(envelope_id).strip(),
            "message_id": norm_mid,
        }
        if policy_reason:
            bound["policy_reason"] = policy_reason

        bound_list.append(bound)

    return bound_list


def verify_attachment_drift(
    candidate: dict[str, Any],
    *,
    expected_account: str | None = None,
    expected_folder: str | None = None,
    expected_message_id: str | None = None,
    expected_envelope_id: str | None = None,
    current_mime_parts: list[dict[str, Any]] | None = None,
    verify_hash: str | None = None,
) -> dict[str, Any]:
    """Perform strict Account-, Message-ID-, Location-, Part-Locator-, and Hash-Drift checks.

    Raises specific AttachmentDriftError subclasses upon drift detection.
    Returns verification dictionary on success.
    """
    # 1. Account Drift
    if expected_account is not None:
        cand_account = candidate.get("account")
        if cand_account != expected_account:
            raise AccountDriftError(
                f"Account drift detected: candidate bound to '{cand_account}', "
                f"but expected/active account is '{expected_account}'"
            )

    # 2. Message-ID Drift
    if expected_message_id is not None:
        cand_mid = normalize_message_id(candidate.get("message_id", ""))
        exp_mid = normalize_message_id(expected_message_id)
        if not cand_mid or cand_mid != exp_mid:
            raise MessageIdDriftError(
                f"Message-ID drift detected: candidate has '{cand_mid}', "
                f"but verified message has '{exp_mid}'"
            )

    # 3. Location Drift (Folder and Envelope-ID)
    if expected_folder is not None:
        cand_folder = candidate.get("folder")
        if cand_folder != expected_folder:
            raise LocationDriftError(
                f"Folder drift detected: candidate is in folder '{cand_folder}', "
                f"expected '{expected_folder}'"
            )

    if expected_envelope_id is not None:
        cand_eid = str(candidate.get("envelope_id", ""))
        exp_eid = str(expected_envelope_id)
        if cand_eid != exp_eid:
            raise LocationDriftError(
                f"Envelope-ID drift detected: candidate has ID '{cand_eid}', "
                f"expected '{exp_eid}'"
            )

    # 4. Part Locator Drift
    if current_mime_parts is not None:
        locator = candidate.get("part_locator")
        matched = next((p for p in current_mime_parts if p.get("part_locator") == locator), None)
        if not matched:
            raise PartLocatorDriftError(
                f"Part locator drift detected: part locator '{locator}' not found in current message"
            )
        if matched.get("filename") != candidate.get("filename"):
            raise PartLocatorDriftError(
                f"Part locator drift detected for '{locator}': filename changed from "
                f"'{candidate.get('filename')}' to '{matched.get('filename')}'"
            )
        if matched.get("mime_type") != candidate.get("mime_type"):
            raise PartLocatorDriftError(
                f"Part locator drift detected for '{locator}': MIME type changed from "
                f"'{candidate.get('mime_type')}' to '{matched.get('mime_type')}'"
            )
        if candidate.get("sha256") and matched.get("sha256") and candidate.get("sha256") != matched.get("sha256"):
            raise HashDriftError(
                f"Hash drift detected for part '{locator}': candidate hash '{candidate.get('sha256')}' "
                f"does not match current part hash '{matched.get('sha256')}'"
            )

    # 5. Direct Hash Drift Check
    if verify_hash is not None:
        cand_hash = candidate.get("sha256")
        if cand_hash != verify_hash:
            raise HashDriftError(
                f"Hash drift detected: candidate hash '{cand_hash}' does not match verified hash '{verify_hash}'"
            )

    return {
        "status": "verified",
        "candidate": candidate,
        "part_locator": candidate.get("part_locator"),
        "sha256": candidate.get("sha256"),
    }


# ==============================================================================
# Hermetic Test Helpers
# ==============================================================================

def build_test_eml(
    *,
    subject: str = "Test Subject",
    from_addr: str = "sender@example.test",
    to_addr: str = "recipient@example.test",
    date_str: str = "Mon, 14 Sep 2026 06:00:00 +0200",
    message_id: str | None = "<test-msg-123@example.test>",
    body_text: str = "Hello,\nthis is the body.",
    body_html: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
    inline_images: list[dict[str, Any]] | None = None,
) -> bytes:
    """Build a deterministic, RFC 822 compliant MIME email byte string for hermetic tests."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Date"] = date_str
    if message_id:
        msg["Message-ID"] = message_id

    if body_html:
        msg.set_content(body_text)
        msg.add_alternative(body_html, subtype="html")
    else:
        msg.set_content(body_text)

    # Add inline images
    if inline_images:
        for img in inline_images:
            payload = img.get("data", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
            cid = img.get("cid", "logo123")
            fname = img.get("filename", "image.png")
            mime_main, mime_sub = img.get("mime_type", "image/png").split("/", 1)
            msg.add_attachment(
                payload,
                maintype=mime_main,
                subtype=mime_sub,
                filename=fname,
                cid=f"<{cid}>",
                disposition="inline",
            )

    # Add regular attachments
    if attachments:
        for att in attachments:
            payload = att.get("data", b"%PDF-1.4 test document content")
            fname = att.get("filename", "document.pdf")
            raw_mime = att.get("mime_type", "application/pdf")
            if "/" in raw_mime:
                mime_main, mime_sub = raw_mime.split("/", 1)
            else:
                mime_main, mime_sub = "application", "octet-stream"
            msg.add_attachment(
                payload,
                maintype=mime_main,
                subtype=mime_sub,
                filename=fname,
                disposition=att.get("disposition", "attachment"),
            )

    return msg.as_bytes()
