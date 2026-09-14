"""Deterministic review-bound attachment quarantine fetch for mail-desk (MD-A2).

Performs secure transport of verified MIME attachment candidates into an isolated,
temporary run quarantine folder under `data/mail-desk/attachments/<run-id>/`.
Enforces strict preflight drift checks, approval receipts, size/file quotas,
atomic temp-sibling promotion, re-hashing, re-typing, and active content blocking.
"""

from __future__ import annotations

import email
import email.policy
import hashlib
import os
from pathlib import Path
import re
import shutil
from typing import Any
import uuid

from core.common import normalize_message_id, resolve_data_dir, utc_now_iso
from core.attachment_policy import (
    DEFAULT_ATTACHMENT_POLICY,
    sanitize_attachment_filename,
)
from core.attachments import (
    AccountDriftError,
    AttachmentDriftError,
    HashDriftError,
    LocationDriftError,
    MessageIdDriftError,
    PartLocatorDriftError,
    validate_attachment_candidate_metadata,
    verify_attachment_drift,
)
from core import himalaya


# ==============================================================================
# Exceptions
# ==============================================================================

class ApprovalReceiptMissingError(ValueError):
    """Raised when an approval receipt is missing or empty."""


class ReceiptDriftError(ValueError):
    """Raised when the approval receipt request hash drifts from the review hash."""


class ActiveContentBlockedError(ValueError):
    """Raised when an attachment contains executable or active code."""


class QuotaExceededError(ValueError):
    """Raised when attachment size or count exceeds policy quotas."""


class QuarantineCollisionError(ValueError):
    """Raised when a file already exists in quarantine with a different hash."""


class SymlinkEscapeError(ValueError):
    """Raised when a path involves a symlink or reparse point outside boundaries."""


# ==============================================================================
# Review Hash & Approval Receipt
# ==============================================================================

WIN32_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}


def compute_review_hash(
    account: str,
    message_id: str,
    folder: str,
    envelope_id: str | int,
    part_locator: str,
    inventory_sha256: str,
) -> str:
    """Compute a deterministic 64-character SHA-256 review hash binding request parameters."""
    norm_mid = normalize_message_id(message_id)
    canonical = (
        f"{str(account).strip()}:"
        f"{norm_mid}:"
        f"{str(folder).strip()}:"
        f"{str(envelope_id).strip()}:"
        f"{str(part_locator).strip()}:"
        f"{str(inventory_sha256).strip().lower()}"
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def verify_approval_receipt(receipt: dict[str, Any] | None, expected_review_hash: str) -> None:
    """Validate that a valid approval receipt binds the canonical review hash."""
    if receipt is None or not isinstance(receipt, dict) or not receipt:
        raise ApprovalReceiptMissingError("Approval receipt is required for attachment quarantine fetch.")

    receipt_id = receipt.get("receipt_id")
    request_hash = receipt.get("request_hash")
    approved_at = receipt.get("approved_at")

    if not receipt_id or not str(receipt_id).strip():
        raise ValueError("Approval receipt missing required field: 'receipt_id'.")
    if not request_hash or not str(request_hash).strip():
        raise ValueError("Approval receipt missing required field: 'request_hash'.")
    if not approved_at or not str(approved_at).strip():
        raise ValueError("Approval receipt missing required field: 'approved_at'.")

    norm_req_hash = str(request_hash).strip().lower()
    norm_exp_hash = str(expected_review_hash).strip().lower()

    if norm_req_hash != norm_exp_hash:
        raise ReceiptDriftError(
            f"Receipt drift detected: receipt request_hash '{norm_req_hash}' "
            f"does not match computed review_hash '{norm_exp_hash}'"
        )


def is_valid_run_id(run_id: str) -> bool:
    """Validate that a run-id is safe against traversal and platform reserved names."""
    if not run_id or not isinstance(run_id, str):
        return False
    s = run_id.strip()
    if not s or len(s) > 100:
        return False
    if not re.fullmatch(r"^[a-zA-Z0-9_-]+$", s):
        return False
    if s.upper() in WIN32_RESERVED_NAMES:
        return False
    return True


# ==============================================================================
# MIME & Active Content Detection
# ==============================================================================

BLOCKED_MAGIC_SIGNATURES: list[tuple[bytes, str]] = [
    (b"MZ", "Windows PE executable binary"),
    (b"\x7fELF", "Linux ELF executable binary"),
    (b"\xca\xfe\xba\xbe", "Mach-O / Java class binary"),
    (b"#!/", "Executable script header"),
    (b"#! /", "Executable script header"),
]


def detect_mime_and_active_content(filename: str, payload: bytes) -> tuple[str, bool, str | None]:
    """Sniff magic bytes, verify content against extension, and block active/executable content."""
    # 1. Check blocked magic signatures
    for sig, desc in BLOCKED_MAGIC_SIGNATURES:
        if payload.startswith(sig):
            return "application/x-executable", True, desc

    # 2. Check blocked extensions from policy
    blocked_exts = DEFAULT_ATTACHMENT_POLICY.get("policy", {}).get("blocked_extensions", [])
    ext = Path(filename).suffix.lower()
    if ext in blocked_exts:
        return "application/octet-stream", True, f"Blocked extension: '{ext}'"

    # 3. Determine effective MIME type by magic bytes
    if payload.startswith(b"%PDF-"):
        eff_mime = "application/pdf"
    elif payload.startswith(b"\x89PNG\r\n\x1a\n"):
        eff_mime = "image/png"
    elif payload.startswith(b"\xff\xd8\xff"):
        eff_mime = "image/jpeg"
    elif payload.startswith(b"GIF87a") or payload.startswith(b"GIF89a"):
        eff_mime = "image/gif"
    elif payload.startswith(b"PK\x03\x04"):
        if ext in [".docx"]:
            eff_mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        elif ext in [".xlsx"]:
            eff_mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        elif ext in [".pptx"]:
            eff_mime = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        else:
            eff_mime = "application/zip"
    else:
        eff_mime = "application/octet-stream"

    return eff_mime, False, None


# ==============================================================================
# Part Extraction from RFC-822 EML
# ==============================================================================

def extract_part_from_eml(
    raw_eml: bytes | str,
    part_locator: str,
    expected_message_id: str | None = None,
) -> tuple[bytes, str, str]:
    """Extract decoded payload bytes for a specific IMAP-style part locator from an EML byte stream."""
    eml_bytes = raw_eml if isinstance(raw_eml, bytes) else raw_eml.encode("utf-8", errors="replace")
    msg = email.message_from_bytes(eml_bytes, policy=email.policy.default)

    raw_mid = msg.get("Message-ID", "")
    norm_mid = normalize_message_id(raw_mid) if raw_mid else ""
    if expected_message_id:
        norm_exp = normalize_message_id(expected_message_id)
        if norm_mid != norm_exp:
            raise MessageIdDriftError(
                f"Message-ID drift detected: EML has '{norm_mid}', expected '{norm_exp}'"
            )

    target_locator = str(part_locator).strip()

    def _walk_parts(message_obj: Any, prefix: str = "") -> list[tuple[str, Any]]:
        results: list[tuple[str, Any]] = []
        if message_obj.is_multipart():
            subparts = message_obj.get_payload()
            if isinstance(subparts, list):
                for idx, sp in enumerate(subparts, start=1):
                    current_locator = f"{prefix}.{idx}" if prefix else str(idx)
                    results.append((current_locator, sp))
                    if sp.is_multipart():
                        results.extend(_walk_parts(sp, current_locator))
        else:
            locator = prefix if prefix else "1"
            results.append((locator, message_obj))
        return results

    found_part: Any | None = None
    for loc, p in _walk_parts(msg):
        if loc == target_locator:
            found_part = p
            break

    if found_part is None:
        raise PartLocatorDriftError(f"Part locator '{target_locator}' not found in message MIME tree.")

    payload = found_part.get_payload(decode=True)
    if not isinstance(payload, bytes):
        payload = b""

    content_type = found_part.get_content_type()
    filename = found_part.get_filename() or f"part_{target_locator.replace('.', '_')}"

    return payload, content_type, filename


# ==============================================================================
# Quota Verification
# ==============================================================================

def check_quarantine_quotas(
    run_dir: Path,
    new_file_bytes: int,
    policy: dict[str, Any] | None = None,
) -> None:
    """Enforce single file (15 MB), cumulative files (25 MB), and count (5) limits."""
    pol = policy or DEFAULT_ATTACHMENT_POLICY
    trans = pol.get("transport", {})
    max_single = trans.get("max_single_file_bytes", 15 * 1024 * 1024)
    max_total = trans.get("max_total_bytes_per_message", 25 * 1024 * 1024)
    max_count = trans.get("max_attachments_per_message", 5)

    # 1. Single file quota
    if new_file_bytes > max_single:
        raise QuotaExceededError(
            f"Single attachment exceeds quota: {new_file_bytes} bytes > {max_single} bytes limit"
        )

    # 2. Existing files in run directory
    if run_dir.exists():
        existing_files = [f for f in run_dir.iterdir() if f.is_file() and not f.name.endswith(".tmp")]
        if len(existing_files) >= max_count:
            raise QuotaExceededError(
                f"Quarantine run file count exceeded: {len(existing_files)} >= {max_count} limit"
            )

        total_existing_bytes = sum(f.stat().st_size for f in existing_files)
        if total_existing_bytes + new_file_bytes > max_total:
            raise QuotaExceededError(
                f"Cumulative quarantine quota exceeded: {total_existing_bytes + new_file_bytes} bytes > {max_total} bytes limit"
            )


# ==============================================================================
# Operation: attachment_fetch
# ==============================================================================

def op_attachment_fetch(
    candidate: dict[str, Any],
    account: str,
    folder: str,
    envelope_id: str | int,
    message_id: str,
    part_locator: str,
    inventory_sha256: str,
    approval_receipt: dict[str, Any] | None,
    review_hash: str | None = None,
    run_id: str | None = None,
    raw_eml: bytes | None = None,
    data_dir: Path | None = None,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute a review-bound attachment quarantine fetch."""
    # 1. Preflight Identity & Candidate Verification
    if not account or not str(account).strip():
        raise ValueError("Cannot fetch attachment: account is required.")
    if not folder or not str(folder).strip():
        raise ValueError("Cannot fetch attachment: folder is required.")
    if not envelope_id or not str(envelope_id).strip():
        raise ValueError("Cannot fetch attachment: envelope_id is required.")
    if not message_id or not str(message_id).strip():
        raise ValueError("Cannot fetch attachment: message_id is required.")
    if not part_locator or not str(part_locator).strip():
        raise ValueError("Cannot fetch attachment: part_locator is required.")
    if not inventory_sha256 or not str(inventory_sha256).strip():
        raise ValueError("Cannot fetch attachment: inventory_sha256 is required.")

    valid, reason = validate_attachment_candidate_metadata(candidate)
    if not valid:
        raise ValueError(f"Invalid attachment candidate metadata: {reason}")

    if str(candidate.get("part_locator", "")).strip() != str(part_locator).strip():
        raise PartLocatorDriftError(
            f"Part locator drift: candidate has '{candidate.get('part_locator')}', expected '{part_locator}'"
        )

    verify_attachment_drift(
        candidate=candidate,
        expected_account=str(account).strip(),
        expected_message_id=normalize_message_id(message_id),
        expected_folder=str(folder).strip(),
        expected_envelope_id=str(envelope_id).strip(),
        verify_hash=str(inventory_sha256).strip().lower(),
    )

    # 2. Review Hash & Approval Receipt
    computed_rev_hash = compute_review_hash(
        account=account,
        message_id=message_id,
        folder=folder,
        envelope_id=envelope_id,
        part_locator=part_locator,
        inventory_sha256=inventory_sha256,
    )
    if review_hash is not None and str(review_hash).strip():
        if str(review_hash).strip().lower() != computed_rev_hash:
            raise ReceiptDriftError(
                f"Review hash drift: provided '{review_hash}' != computed '{computed_rev_hash}'"
            )

    verify_approval_receipt(approval_receipt, expected_review_hash=computed_rev_hash)

    # 3. Validate Run-ID & Paths
    actual_run_id = str(run_id or f"run_{uuid.uuid4().hex[:12]}").strip()
    if not is_valid_run_id(actual_run_id):
        raise ValueError(f"Invalid or unsafe run_id: '{actual_run_id}'")

    clean_filename = sanitize_attachment_filename(candidate.get("filename"))
    if not clean_filename or clean_filename in WIN32_RESERVED_NAMES:
        clean_filename = f"part_{str(part_locator).replace('.', '_')}.dat"

    base_data_dir = data_dir or resolve_data_dir()
    attachments_root = base_data_dir / "attachments"
    run_dir = attachments_root / actual_run_id

    # Check for symlink/reparse point escaping
    if run_dir.is_symlink():
        raise SymlinkEscapeError(f"Symlink detected at quarantine run directory: {run_dir}")

    # Check initial single file quota based on candidate metadata
    candidate_size = int(candidate["size_bytes"])
    check_quarantine_quotas(run_dir, candidate_size, policy=policy)

    target_file = run_dir / clean_filename
    rel_path = f"data/mail-desk/attachments/{actual_run_id}/{clean_filename}"

    # 4. Idempotency Check
    if target_file.exists():
        existing_sha = hashlib.sha256(target_file.read_bytes()).hexdigest()
        if existing_sha.lower() == str(inventory_sha256).strip().lower():
            # Idempotent retry: existing valid file
            eff_mime, _, _ = detect_mime_and_active_content(clean_filename, target_file.read_bytes())
            return {
                "status": "already_fetched",
                "run_id": actual_run_id,
                "relative_path": rel_path,
                "inventory_sha256": str(inventory_sha256).strip().lower(),
                "fetch_sha256": existing_sha.lower(),
                "effective_mime_type": eff_mime,
                "size_bytes": target_file.stat().st_size,
                "filename": clean_filename,
                "error": None,
            }
        else:
            raise QuarantineCollisionError(
                f"Quarantine collision: '{clean_filename}' exists with different hash {existing_sha} != {inventory_sha256}"
            )

    # 5. Fetch / Transport Bytes from Backend
    if raw_eml is None:
        raw_eml = himalaya.fetch_raw_message_eml(str(envelope_id), folder=folder, account=str(account).strip())

    payload, _, _ = extract_part_from_eml(raw_eml, part_locator=str(part_locator), expected_message_id=message_id)

    # 6. Post-Fetch Verification
    fetch_sha = hashlib.sha256(payload).hexdigest().lower()
    exp_sha = str(inventory_sha256).strip().lower()
    if fetch_sha != exp_sha:
        raise HashDriftError(
            f"Hash drift detected on fetch: downloaded bytes have '{fetch_sha}', inventory has '{exp_sha}'"
        )

    eff_mime, is_active, active_reason = detect_mime_and_active_content(clean_filename, payload)
    if is_active:
        raise ActiveContentBlockedError(
            f"Active content blocked in '{clean_filename}': {active_reason}"
        )

    # Re-check quotas with exact byte length
    check_quarantine_quotas(run_dir, len(payload), policy=policy)

    # 7. Atomic Write via Sibling Temp
    run_dir.mkdir(parents=True, exist_ok=True)
    temp_file = run_dir / f".{clean_filename}.{uuid.uuid4().hex}.tmp"
    try:
        temp_file.write_bytes(payload)

        # Check collision again before final promote
        if target_file.exists():
            curr_sha = hashlib.sha256(target_file.read_bytes()).hexdigest()
            if curr_sha.lower() != fetch_sha:
                raise QuarantineCollisionError(
                    f"Quarantine collision: '{clean_filename}' exists with different hash"
                )
        os.replace(temp_file, target_file)
    finally:
        if temp_file.exists():
            try:
                temp_file.unlink()
            except OSError:
                pass

    return {
        "status": "fetched",
        "run_id": actual_run_id,
        "relative_path": rel_path,
        "inventory_sha256": exp_sha,
        "fetch_sha256": fetch_sha,
        "effective_mime_type": eff_mime,
        "size_bytes": len(payload),
        "filename": clean_filename,
        "error": None,
    }


def cleanup_run_quarantine(run_id: str, data_dir: Path | None = None) -> None:
    """Safely remove a validated quarantine run directory."""
    if not is_valid_run_id(run_id):
        raise ValueError(f"Cannot cleanup invalid run_id: '{run_id}'")

    base_data_dir = data_dir or resolve_data_dir()
    attachments_root = base_data_dir / "attachments"
    run_dir = (attachments_root / run_id).resolve()

    # Boundary safety check
    if not str(run_dir).startswith(str(attachments_root.resolve())):
        raise ValueError(f"Run directory '{run_dir}' escapes attachments root.")

    if run_dir.exists() and run_dir.is_dir():
        shutil.rmtree(run_dir)
