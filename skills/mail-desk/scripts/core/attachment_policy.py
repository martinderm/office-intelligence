"""Attachment policy, default limits, and security sanitation rules for mail-desk.

Part of FR-08 (MD-A1/MD-A2/MD-A3).
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from typing import Any

DEFAULT_ATTACHMENT_POLICY: dict[str, Any] = {
    "version": "1.0.0",
    "transport": {
        "max_attachments_per_message": 5,
        "max_single_file_bytes": 15 * 1024 * 1024,       # 15 MB
        "max_total_bytes_per_message": 25 * 1024 * 1024,  # 25 MB
        "download_timeout_seconds": 25,
        "allowed_mime_types": [
            "application/pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/msword",
            "text/plain",
            "text/csv",
            "image/png",
            "image/jpeg",
        ],
        "disallowed_extensions": [
            ".exe", ".bat", ".cmd", ".com", ".msi", ".scr", ".pif",
            ".vbs", ".js", ".jse", ".wsf", ".wsh", ".ps1",
            ".docm", ".xlsm", ".pptm",
        ],
    },
    "extraction": {
        "pdf_max_pages": 10,
        "ocr_max_pages": 3,
        "ocr_timeout_seconds": 30,
        "docx_max_paragraphs": 40,
        "pptx_max_slides": 15,
        "xlsx_max_sheets": 2,
        "xlsx_max_rows_per_sheet": 50,
        "xlsx_max_cols_per_sheet": 10,
        "extraction_timeout_seconds": 20,
    },
    "budget": {
        "max_chars_per_attachment": 15000,
        "max_chars_per_message": 30000,
        "append_truncation_notice": True,
    },
}

_DANGEROUS_EXTENSIONS = frozenset(DEFAULT_ATTACHMENT_POLICY["transport"]["disallowed_extensions"])


def sanitize_attachment_filename(raw_filename: str | None) -> str:
    """Sanitize and extract the basename of an attachment, preventing path traversal."""
    if not raw_filename:
        return "unnamed_attachment"

    clean_str = raw_filename.strip().replace("\x00", "")
    # Strip Win32 device namespace prefixes if present
    if clean_str.startswith("\\\\?\\"):
        clean_str = clean_str[4:]

    # Handle both Windows and POSIX separators
    clean_str = clean_str.replace("\\", "/")
    basename = PurePosixPath(clean_str).name.strip()

    # Disallow current/parent directory components
    if not basename or basename in {".", ".."}:
        return "unnamed_attachment"

    # Clean potentially problematic characters
    sanitized = re.sub(r'[<>:"|?*]', "_", basename)
    return sanitized or "unnamed_attachment"


def check_attachment_policy(
    filename: str,
    mime_type: str | None = None,
    size_bytes: int | None = None,
    current_index: int = 0,
    cumulative_bytes: int = 0,
    policy: dict[str, Any] | None = None,
) -> tuple[str, str | None]:
    """Validate an attachment candidate against transport, cumulative size, and security rules.

    Unbekannte oder ungültige Größe sowie unbekannte oder nicht erlaubte MIME-Typen
    dürfen niemals "allowed" ergeben (fail-closed).

    Returns:
        (policy_status, reason)
        where policy_status in {
            "allowed",
            "rejected_security",
            "rejected_unsupported_type",
            "rejected_unspecified_size",
            "skipped_oversized",
            "skipped_total_oversized",
            "skipped_count_limit",
        }
    """
    pol = policy or DEFAULT_ATTACHMENT_POLICY
    transport = pol.get("transport", {})

    # 1. Sicherheitsausschluss: Gefährliche Erweiterung / aktive Inhalte (höchste Priorität)
    clean_name = sanitize_attachment_filename(filename)
    ext = Path(clean_name).suffix.lower()
    if ext in _DANGEROUS_EXTENSIONS:
        return "rejected_security", f"Extension '{ext}' disallowed due to active content or executable risk"

    # 2. Zähllimit pro Nachricht
    max_count = transport.get("max_attachments_per_message", 5)
    if current_index >= max_count:
        return "skipped_count_limit", f"Attachment index {current_index} exceeds count limit ({max_count})"

    # 3. Unbekannte oder nicht positive Größe (Fail-Closed: niemals "allowed")
    if size_bytes is None or not isinstance(size_bytes, int) or size_bytes <= 0:
        return "rejected_unspecified_size", f"Attachment size ({size_bytes}) is unknown or invalid; cannot be allowed"

    # 4. Maximale Einzel-Dateigröße
    max_single_size = transport.get("max_single_file_bytes", 15 * 1024 * 1024)
    if size_bytes > max_single_size:
        return "skipped_oversized", f"File size ({size_bytes} bytes) exceeds maximum single file limit ({max_single_size} bytes)"

    # 5. Kumulatives Gesamtgrößenlimit pro Nachricht
    max_total_bytes = transport.get("max_total_bytes_per_message", 25 * 1024 * 1024)
    if cumulative_bytes + size_bytes > max_total_bytes:
        return "skipped_total_oversized", (
            f"Cumulative attachment size ({cumulative_bytes + size_bytes} bytes) exceeds "
            f"maximum total limit per message ({max_total_bytes} bytes)"
        )

    # 6. Unbekannte oder nicht erlaubte MIME-Typen (Fail-Closed: niemals "allowed")
    if not mime_type or not isinstance(mime_type, str) or not mime_type.strip():
        return "rejected_unsupported_type", "MIME type is unknown or missing; cannot be allowed"

    clean_mime = mime_type.strip().lower()
    allowed_mimes = set(transport.get("allowed_mime_types", []))
    if clean_mime not in allowed_mimes:
        return "rejected_unsupported_type", f"MIME type '{clean_mime}' is unknown or not permitted by policy"

    return "allowed", None
