"""Deterministic review-bound attachment quarantine fetch for mail-desk (MD-A2).

Performs secure transport of verified MIME attachment candidates into an isolated,
temporary run quarantine folder under `data/mail-desk/attachments/<run-id>/`.
Enforces strict preflight drift checks, versioned JSON approval receipts, RFC-3339 timestamps,
per-message quotas, full parent-chain symlink/reparse point validation,
genuine OOXML inspection, MIME/extension drift detection, atomic no-clobber promotion,
and 25s download timeout pass-through.
"""

from __future__ import annotations

import email
import email.policy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
from typing import Any
import uuid
import zipfile

from core.common import normalize_message_id, resolve_data_dir
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
    """Raised when an attachment contains executable, macro, or active code."""


class DisallowedExtensionError(ActiveContentBlockedError):
    """Raised when an attachment has a disallowed file extension."""


class QuotaExceededError(ValueError):
    """Raised when attachment size or count exceeds per-message policy quotas."""


class QuarantineCollisionError(ValueError):
    """Raised when a file already exists in quarantine with a different hash."""


class SymlinkEscapeError(ValueError):
    """Raised when a path involves a symlink or reparse point outside boundaries."""


class QuarantineInventoryError(ValueError):
    """Raised when quarantine inventory is missing, corrupted, or structurally invalid."""


class MimeDriftError(AttachmentDriftError):
    """Raised when effective MIME type drifts from inventory MIME type."""


class ExtensionMimeDriftError(MimeDriftError):
    """Raised when effective MIME type is incompatible with file extension."""


def _load_workspace_lock_guard() -> Any:
    """Dynamically load canonical workspace_lock_guard.py via Path.resolve().parents."""
    if "workspace_lock_guard" in sys.modules and sys.modules["workspace_lock_guard"] is not None:
        return sys.modules["workspace_lock_guard"]

    for parent in Path(__file__).resolve().parents:
        cand = parent / "workspace-lock" / "scripts" / "workspace_lock_guard.py"
        if cand.is_file():
            spec = importlib.util.spec_from_file_location("workspace_lock_guard", cand)
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = mod
                spec.loader.exec_module(mod)
                return mod

    for parent in [Path.cwd().resolve(), *Path.cwd().resolve().parents]:
        cand = parent / "workspace-lock" / "scripts" / "workspace_lock_guard.py"
        if cand.is_file():
            spec = importlib.util.spec_from_file_location("workspace_lock_guard", cand)
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = mod
                spec.loader.exec_module(mod)
                return mod

    raise RuntimeError("Canonical workspace-lock guard is unavailable")


try:
    _guard = _load_workspace_lock_guard()
    WorkspaceLockError = _guard.WorkspaceLockError
except Exception:
    class WorkspaceLockError(RuntimeError):
        """Raised when local mutation is not authorized by an owned lock."""


def verify_workspace_lock(
    workspace_root: str | Path | None = None,
    *,
    lease_id: str | None = None,
    conversation_id: str | None = None,
    allow_legacy: bool = False,
    data_dir: Path | None = None,
) -> Any:
    """Verify invocation-owned workspace lock before any local filesystem mutation."""
    ws: Path
    if workspace_root is not None:
        ws = Path(workspace_root).resolve()
    else:
        env_ws = os.environ.get("WORKSPACE_ROOT", "").strip()
        if env_ws:
            ws = Path(env_ws).resolve()
        elif data_dir is not None:
            cand = Path(data_dir).resolve()
            found_ws = None
            for p in [cand, *cand.parents]:
                if (p / ".agents").is_dir() or (p / ".git").is_dir():
                    found_ws = p
                    break
            ws = found_ws if found_ws is not None else Path.cwd().resolve()
        else:
            cand = Path.cwd().resolve()
            found_ws = None
            for p in [cand, *cand.parents]:
                if (p / ".agents").is_dir() or (p / ".git").is_dir():
                    found_ws = p
                    break
            ws = found_ws if found_ws is not None else Path.cwd().resolve()

    eff_lease_id = lease_id if lease_id is not None else os.environ.get("WORKSPACE_LOCK_LEASE_ID") or None
    eff_conv_id = conversation_id if conversation_id is not None else os.environ.get("WORKSPACE_LOCK_CONVERSATION_ID") or None
    eff_allow_legacy = allow_legacy or (os.environ.get("WORKSPACE_LOCK_ALLOW_LEGACY", "").lower() in ("1", "true", "yes"))

    guard = _load_workspace_lock_guard()
    return guard.require_workspace_lock(
        ws,
        lease_id=eff_lease_id,
        conversation_id=eff_conv_id,
        allow_legacy=eff_allow_legacy,
    )


# ==============================================================================
# Constants & Reserved Names
# ==============================================================================

WIN32_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}

RFC3339_REGEX = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})$"
)

BLOCKED_MAGIC_SIGNATURES: list[tuple[bytes, str]] = [
    (b"MZ", "Windows PE executable binary"),
    (bytes([0x7f, 0x45, 0x4c, 0x46]), "Linux ELF executable binary"),
    (bytes([0xca, 0xfe, 0xba, 0xbe]), "Java class or Mach-O binary"),
    (b"#!/", "Executable script header"),
    (b"#! /", "Executable script header"),
]

MIME_EXTENSION_MAP: dict[str, set[str]] = {
    "application/pdf": {".pdf"},
    "image/png": {".png"},
    "image/jpeg": {".jpg", ".jpeg"},
    "image/gif": {".gif"},
    "text/plain": {".txt", ".text", ".log", ".md"},
    "text/csv": {".csv"},
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": {".docx"},
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {".xlsx"},
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": {".pptx"},
    "application/msword": {".doc"},
}


# ==============================================================================
# Review Hash & Approval Receipt
# ==============================================================================

def compute_review_hash(
    account: str,
    message_id: str,
    folder: str,
    envelope_id: str | int,
    part_locator: str,
    inventory_sha256: str,
    schema_version: int = 1,
) -> str:
    """Compute a deterministic 64-character SHA-256 review hash over canonical versioned JSON."""
    canonical_dict = {
        "schema_version": schema_version,
        "account": str(account).strip(),
        "message_id": normalize_message_id(message_id),
        "folder": str(folder).strip(),
        "envelope_id": str(envelope_id).strip(),
        "part_locator": str(part_locator).strip(),
        "inventory_sha256": str(inventory_sha256).strip().lower(),
    }
    canonical_json = json.dumps(canonical_dict, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def verify_approval_receipt(
    approval_receipt: dict[str, Any] | None,
    expected_review_hash: str,
) -> None:
    """Verify that an approval receipt is present, well-formed, and matches the review hash."""
    if not approval_receipt or not isinstance(approval_receipt, dict):
        raise ApprovalReceiptMissingError(
            "Attachment fetch requires an explicit, non-empty approval_receipt."
        )

    receipt_id = approval_receipt.get("receipt_id")
    request_hash = approval_receipt.get("request_hash")
    approved_at = approval_receipt.get("approved_at")

    if not receipt_id or not str(receipt_id).strip():
        raise ValueError("Approval receipt is missing required 'receipt_id'.")
    if not request_hash or not str(request_hash).strip():
        raise ValueError("Approval receipt is missing required 'request_hash'.")
    if not approved_at or not str(approved_at).strip():
        raise ValueError("Approval receipt is missing required 'approved_at'.")

    approved_at_str = str(approved_at).strip()
    if not RFC3339_REGEX.fullmatch(approved_at_str):
        raise ValueError(
            f"Approval receipt 'approved_at' must be a valid RFC-3339 timestamp with timezone offset: {approved_at_str!r}"
        )

    norm_req_hash = str(request_hash).strip().lower()
    norm_exp_hash = str(expected_review_hash).strip().lower()

    if norm_req_hash != norm_exp_hash:
        raise ReceiptDriftError(
            f"Receipt drift detected: receipt request_hash '{norm_req_hash}' "
            f"does not match computed review_hash '{norm_exp_hash}'"
        )


def validate_attachment_filename(raw_filename: Any) -> str:
    """Validate candidate attachment filename fail-closed against traversal and Windows reserved names.

    Rejects before any I/O:
    - Non-string or empty filename
    - Free paths with directory separators (/ or \\)
    - Traversal sequences (..)
    - Windows reserved device names with or without extension (CON, CON.txt, AUX.pdf, etc.)
    - Forbidden filesystem characters (<>:"|?* or null bytes)
    """
    if not isinstance(raw_filename, str) or not raw_filename.strip():
        raise ValueError("Attachment filename is missing or empty.")

    cleaned = raw_filename.strip()

    # 1. Reject free candidate paths and path traversal sequences
    if "/" in cleaned or "\\" in cleaned or ".." in cleaned:
        raise ValueError(
            f"Free candidate path or directory traversal rejected before I/O: {raw_filename!r}"
        )

    # 2. Reject null bytes and illegal characters
    if "\x00" in cleaned or re.search(r'[<>:"|?*]', cleaned):
        raise ValueError(
            f"Illegal characters in attachment filename rejected before I/O: {raw_filename!r}"
        )

    # 3. Reject Windows reserved device names (both with and without extension)
    stem = cleaned.split(".")[0].strip().upper()
    if stem in WIN32_RESERVED_NAMES or cleaned.upper() in WIN32_RESERVED_NAMES:
        raise ValueError(
            f"Windows reserved device name rejected before I/O: {raw_filename!r}"
        )

    return cleaned


def is_valid_run_id(run_id: str) -> bool:
    """Validate that a run-id is safe against traversal and platform reserved names."""
    if not run_id or not isinstance(run_id, str):
        return False
    s = run_id.strip()
    if not s or len(s) > 100:
        return False
    if not re.fullmatch(r"^[a-zA-Z0-9_-]+$", s):
        return False
    stem = s.split(".")[0].strip().upper()
    if stem in WIN32_RESERVED_NAMES or s.upper() in WIN32_RESERVED_NAMES:
        return False
    return True


# ==============================================================================
# Path Containment & Reparse Point Protection
# ==============================================================================

def check_quarantine_path_security(target_file: Path, attachments_root: Path) -> None:
    """Validate that target_file and all parent directories do not escape via symlink or reparse point."""
    resolved_root = attachments_root.resolve()
    resolved_target = target_file.resolve()

    # 1. Resolved containment check
    try:
        resolved_target.relative_to(resolved_root)
    except ValueError:
        raise SymlinkEscapeError(
            f"Containment violation: target '{resolved_target}' escapes attachments root '{resolved_root}'"
        )

    # 2. Check attachments_root itself
    if attachments_root.is_symlink() or os.path.islink(attachments_root):
        raise SymlinkEscapeError(f"Symlink detected at attachments root: {attachments_root}")

    if os.name == "nt" and attachments_root.exists():
        try:
            stat_res = os.lstat(attachments_root)
            if getattr(stat_res, "st_file_attributes", 0) & 0x400:
                raise SymlinkEscapeError(f"Reparse point detected at attachments root: {attachments_root}")
        except SymlinkEscapeError:
            raise
        except OSError as err:
            raise SymlinkEscapeError(
                f"Failed to inspect attachments root attributes (lstat error): '{attachments_root}': {err}"
            ) from err

    # 3. Check every existing directory along the path up to attachments_root
    cur: Path = target_file.parent
    while True:
        if cur.is_symlink() or os.path.islink(cur):
            raise SymlinkEscapeError(f"Symlink detected in quarantine path: {cur}")

        if os.name == "nt" and cur.exists():
            try:
                stat_res = os.lstat(cur)
                if getattr(stat_res, "st_file_attributes", 0) & 0x400:
                    raise SymlinkEscapeError(f"Windows reparse point detected in quarantine path: {cur}")
            except SymlinkEscapeError:
                raise
            except OSError as err:
                raise SymlinkEscapeError(
                    f"Failed to inspect quarantine path attributes (lstat error): '{cur}': {err}"
                ) from err

        if cur.resolve() == resolved_root or cur == attachments_root:
            break
        if cur.parent == cur:
            raise SymlinkEscapeError(f"Path '{cur}' not under attachments root '{attachments_root}'")
        cur = cur.parent


# ==============================================================================
# MIME, OOXML & Active Content Detection
# ==============================================================================

def detect_mime_and_active_content(
    filename: str,
    payload: bytes,
    policy: dict[str, Any] | None = None,
) -> tuple[str, bool, str | None]:
    """Sniff magic bytes, verify OOXML packages, and block active/executable content fail-closed."""
    pol = policy or DEFAULT_ATTACHMENT_POLICY
    trans = pol.get("transport", {})

    # 1. Check disallowed extensions from transport policy
    disallowed_exts = trans.get(
        "disallowed_extensions",
        DEFAULT_ATTACHMENT_POLICY["transport"]["disallowed_extensions"],
    )
    ext = Path(filename).suffix.lower()
    if ext in set(e.lower() for e in disallowed_exts):
        return "application/octet-stream", True, f"Disallowed extension: '{ext}'"

    # 2. Check blocked magic signatures
    for sig, desc in BLOCKED_MAGIC_SIGNATURES:
        if payload.startswith(sig):
            return "application/x-executable", True, desc

    # 3. Sniff magic bytes & inspect structured formats
    eff_mime: str | None = None

    if payload.startswith(b"%PDF-"):
        eff_mime = "application/pdf"
    elif payload.startswith(bytes([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])):
        eff_mime = "image/png"
    elif payload.startswith(bytes([0xff, 0xd8, 0xff])):
        eff_mime = "image/jpeg"
    elif payload.startswith(b"GIF87a") or payload.startswith(b"GIF89a"):
        eff_mime = "image/gif"
    elif payload.startswith(bytes([0x50, 0x4b, 0x03, 0x04])):
        # Genuine OOXML vs general ZIP inspection
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as zf:
                namelist = zf.namelist()
                has_content_types = "[Content_Types].xml" in namelist
                has_word = any(n.startswith("word/") for n in namelist)
                has_xl = any(n.startswith("xl/") for n in namelist)
                has_ppt = any(n.startswith("ppt/") for n in namelist)

                # Check for macro active content inside OOXML
                has_vba = any("vbaProject.bin" in n for n in namelist)
                if has_vba:
                    return "application/vnd.ms-office.active-macro", True, "Macro-bearing Office document detected: contains 'vbaProject.bin'"

                if has_content_types:
                    # Check [Content_Types].xml for macroEnabled types
                    try:
                        ct_bytes = zf.read("[Content_Types].xml")
                        if b"macroEnabled" in ct_bytes or b"vbaProject" in ct_bytes:
                            return "application/vnd.ms-office.active-macro", True, "Macro-bearing OOXML content type detected in [Content_Types].xml"
                    except Exception:
                        pass

                    if has_word:
                        eff_mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                    elif has_xl:
                        eff_mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    elif has_ppt:
                        eff_mime = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
                    else:
                        eff_mime = "application/zip"
                else:
                    eff_mime = "application/zip"
        except Exception:
            eff_mime = "application/zip"
    elif payload.startswith(bytes([0xd0, 0xcf, 0x11, 0xe0, 0xa1, 0xb1, 0x1a, 0xe1])):
        eff_mime = "application/msword"
    else:
        # Check text heuristics
        is_binary = bytes([0]) in payload[:1024]
        if not is_binary:
            try:
                payload.decode("utf-8")
                if ext == ".csv":
                    eff_mime = "text/csv"
                else:
                    eff_mime = "text/plain"
            except UnicodeDecodeError:
                eff_mime = "application/octet-stream"
        else:
            eff_mime = "application/octet-stream"

    # 4. Check whether format is considered active
    blocked_mimes = {
        "application/x-executable",
        "application/x-msdownload",
        "application/x-sh",
        "application/x-bat",
        "application/vnd.ms-office.active-macro",
    }
    if eff_mime in blocked_mimes:
        return eff_mime, True, f"Blocked MIME type signature: {eff_mime}"

    return eff_mime, False, None


def validate_mime_and_extension(
    effective_mime: str,
    inventory_mime: str | None,
    filename: str,
    policy: dict[str, Any] | None = None,
) -> None:
    """Validate that effective MIME does not drift from inventory and matches file extension."""
    pol = policy or DEFAULT_ATTACHMENT_POLICY
    trans = pol.get("transport", {})
    allowed_mimes = set(trans.get("allowed_mime_types", DEFAULT_ATTACHMENT_POLICY["transport"]["allowed_mime_types"]))

    # 1. Allowed MIME types
    if effective_mime not in allowed_mimes:
        raise MimeDriftError(
            f"Effective MIME type '{effective_mime}' is not permitted by policy."
        )

    # 2. Check drift between inventory stated MIME and effective detected MIME
    if inventory_mime:
        inv_norm = inventory_mime.split(";")[0].strip().lower()
        eff_norm = effective_mime.split(";")[0].strip().lower()

        # If inventory is octet-stream, we accept sniffed concrete allowed types
        if inv_norm != "application/octet-stream" and inv_norm != eff_norm:
            raise MimeDriftError(
                f"MIME drift detected: inventory stated '{inv_norm}', but effective detected MIME is '{eff_norm}'"
            )

    # 3. Check extension compatibility with effective MIME
    ext = Path(filename).suffix.lower()
    if effective_mime in MIME_EXTENSION_MAP:
        valid_exts = MIME_EXTENSION_MAP[effective_mime]
        if ext not in valid_exts:
            raise ExtensionMimeDriftError(
                f"Extension drift detected: file extension '{ext}' is not compatible with effective MIME type '{effective_mime}'. Expected one of: {sorted(valid_exts)}"
            )


# ==============================================================================
# MIME Part Extraction
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
# Message-Scoped Quarantine Inventory & Quotas
# ==============================================================================

INVENTORY_FILENAME = ".quarantine-inventory.json"
INVENTORY_LOCK_FILENAME = ".quarantine-inventory.lock"


class _QuarantineInventoryLock:
    """Atomic cross-platform lock file for serializing quarantine inventory operations."""

    def __init__(self, run_dir: Path, timeout: float = 10.0):
        self.lock_path = run_dir / INVENTORY_LOCK_FILENAME
        self.timeout = timeout
        self.fd: int | None = None

    def __enter__(self):
        run_dir = self.lock_path.parent
        run_dir.mkdir(parents=True, exist_ok=True)
        start = time.time()
        while True:
            try:
                self.fd = os.open(
                    str(self.lock_path),
                    os.O_CREAT | os.O_EXCL | os.O_RDWR,
                )
                return self
            except FileExistsError:
                if time.time() - start > self.timeout:
                    raise TimeoutError(f"Timed out waiting for quarantine inventory lock: '{self.lock_path}'")
                time.sleep(0.02)

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            try:
                os.unlink(self.lock_path)
            except OSError:
                pass


def _validate_inventory_schema(inv: Any, inv_file: Path) -> dict[str, Any]:
    if not isinstance(inv, dict):
        raise QuarantineInventoryError(
            f"Corrupted quarantine inventory '{inv_file}': root must be a JSON object, got {type(inv).__name__}"
        )
    schema_ver = inv.get("schema_version")
    if schema_ver != 1:
        raise QuarantineInventoryError(
            f"Unsupported inventory schema_version in '{inv_file}': {schema_ver!r} (expected 1)"
        )
    msgs = inv.get("messages")
    if not isinstance(msgs, dict):
        raise QuarantineInventoryError(
            f"Corrupted quarantine inventory '{inv_file}': 'messages' field must be a JSON object"
        )
    for mid, mdata in msgs.items():
        if not isinstance(mdata, dict):
            raise QuarantineInventoryError(
                f"Corrupted quarantine inventory '{inv_file}': message entry for '{mid}' must be a JSON object"
            )
        files = mdata.get("files")
        if not isinstance(files, dict):
            raise QuarantineInventoryError(
                f"Corrupted quarantine inventory '{inv_file}': 'files' for message '{mid}' must be a JSON object"
            )
        count = mdata.get("count")
        if not isinstance(count, int) or count < 0:
            raise QuarantineInventoryError(
                f"Corrupted quarantine inventory '{inv_file}': 'count' for message '{mid}' must be non-negative integer"
            )
        total_bytes = mdata.get("total_bytes")
        if not isinstance(total_bytes, int) or total_bytes < 0:
            raise QuarantineInventoryError(
                f"Corrupted quarantine inventory '{inv_file}': 'total_bytes' for message '{mid}' must be non-negative integer"
            )
        for fname, finfo in files.items():
            if not isinstance(finfo, dict):
                raise QuarantineInventoryError(
                    f"Corrupted quarantine inventory '{inv_file}': file entry '{fname}' must be an object"
                )
            sha = finfo.get("sha256")
            if not isinstance(sha, str) or len(sha) != 64:
                raise QuarantineInventoryError(
                    f"Corrupted quarantine inventory '{inv_file}': invalid sha256 for '{fname}' in message '{mid}'"
                )
            sbytes = finfo.get("size_bytes")
            if not isinstance(sbytes, int) or sbytes < 0:
                raise QuarantineInventoryError(
                    f"Corrupted quarantine inventory '{inv_file}': invalid size_bytes for '{fname}' in message '{mid}'"
                )
    return inv


def _load_quarantine_inventory(run_dir: Path, is_recording: bool = False) -> dict[str, Any]:
    """Load and validate quarantine inventory fail-closed against corruption and missing inventories."""
    if not run_dir.exists():
        return {"schema_version": 1, "messages": {}}

    inv_file = run_dir / INVENTORY_FILENAME
    if inv_file.exists():
        try:
            raw_text = inv_file.read_text(encoding="utf-8")
            data = json.loads(raw_text)
        except Exception as err:
            raise QuarantineInventoryError(
                f"Failed to read or parse quarantine inventory '{inv_file}': {err}"
            ) from err
        return _validate_inventory_schema(data, inv_file)

    # inv_file does not exist, but run_dir exists:
    if is_recording:
        # Initializing inventory for the newly written / promoted file(s)
        return {"schema_version": 1, "messages": {}}

    # Check if run_dir contains existing non-temp files (meaning an existing run with missing inventory)
    existing_items = [
        p for p in run_dir.iterdir()
        if not p.name.endswith(".tmp") and p.name != INVENTORY_LOCK_FILENAME
    ]
    if existing_items:
        raise QuarantineInventoryError(
            f"Quarantine inventory '{INVENTORY_FILENAME}' is missing in existing run directory '{run_dir}' containing files"
        )

    return {"schema_version": 1, "messages": {}}


def _record_in_quarantine_inventory_unlocked(
    run_dir: Path,
    message_id: str,
    filename: str,
    sha256: str,
    size_bytes: int,
) -> None:
    """Record a file in .quarantine-inventory.json assuming lock is already held."""
    inv = _load_quarantine_inventory(run_dir, is_recording=True)
    norm_mid = normalize_message_id(message_id) or "__default__"
    msgs = inv.setdefault("messages", {})
    msg_entry = msgs.setdefault(norm_mid, {"files": {}, "count": 0, "total_bytes": 0})
    files = msg_entry.setdefault("files", {})

    files[filename] = {
        "sha256": sha256.strip().lower(),
        "size_bytes": int(size_bytes),
    }
    msg_entry["count"] = len(files)
    msg_entry["total_bytes"] = sum(f["size_bytes"] for f in files.values())

    inv_file = run_dir / INVENTORY_FILENAME
    tmp_inv = run_dir / f".inv.{uuid.uuid4().hex}.tmp"
    with tmp_inv.open("w", encoding="utf-8") as fh:
        json.dump(inv, fh, indent=2, sort_keys=True)
    os.replace(tmp_inv, inv_file)


def _record_in_quarantine_inventory(
    run_dir: Path,
    message_id: str,
    filename: str,
    sha256: str,
    size_bytes: int,
) -> None:
    """Record a file in .quarantine-inventory.json under lock to prevent lost concurrent updates."""
    with _QuarantineInventoryLock(run_dir):
        _record_in_quarantine_inventory_unlocked(
            run_dir=run_dir,
            message_id=message_id,
            filename=filename,
            sha256=sha256,
            size_bytes=size_bytes,
        )


def check_quarantine_quotas(
    run_dir: Path,
    new_file_bytes: int,
    message_id: str | None = None,
    filename: str | None = None,
    sha256: str | None = None,
    policy: dict[str, Any] | None = None,
) -> None:
    """Enforce single file (15 MB), cumulative files (25 MB), and count (5) limits PER MESSAGE."""
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

    # 2. Per-message quotas
    norm_mid = normalize_message_id(message_id) if message_id else "__default__"
    inv = _load_quarantine_inventory(run_dir)
    msg_data = inv.get("messages", {}).get(norm_mid, {"files": {}, "count": 0, "total_bytes": 0})
    files = msg_data.get("files", {})

    # Check if this exact file is already recorded (idempotent fetch consumes no additional quota)
    is_existing = (
        filename is not None
        and filename in files
        and sha256 is not None
        and files[filename].get("sha256") == sha256
    )

    if not is_existing:
        current_count = len(files)
        current_total_bytes = sum(f.get("size_bytes", 0) for f in files.values())

        if current_count >= max_count:
            raise QuotaExceededError(
                f"Quarantine file count limit exceeded for message '{norm_mid}': {current_count} >= {max_count} limit"
            )

        if current_total_bytes + new_file_bytes > max_total:
            raise QuotaExceededError(
                f"Cumulative quarantine quota exceeded for message '{norm_mid}': "
                f"{current_total_bytes + new_file_bytes} bytes > {max_total} bytes limit"
            )


# ==============================================================================
# Atomic No-Clobber Promotion
# ==============================================================================

def _atomic_no_clobber_promote(temp_file: Path, target_file: Path, expected_sha: str) -> str:
    """Promote temp_file to target_file with strict atomic no-clobber semantics.

    Returns:
        status: "fetched" or "already_fetched"
    Raises:
        QuarantineCollisionError: If target_file exists or appears with a different hash.
        RuntimeError: If atomic no-clobber primitive is unavailable or fails.
    """
    exp_sha_lower = expected_sha.strip().lower()

    # Pre-check if target already exists prior to promotion:
    if target_file.exists():
        curr_sha = hashlib.sha256(target_file.read_bytes()).hexdigest().lower()
        temp_file.unlink(missing_ok=True)
        if curr_sha == exp_sha_lower:
            return "already_fetched"
        raise QuarantineCollisionError(
            f"Quarantine collision: '{target_file.name}' already exists with different hash {curr_sha} != {exp_sha_lower}"
        )

    # Perform atomic no-clobber link:
    # On NTFS and POSIX, os.link atomically links temp_file to target_file.
    # If target_file already exists, it atomically fails with FileExistsError WITHOUT modifying target_file.
    try:
        os.link(temp_file, target_file)
        temp_file.unlink(missing_ok=True)
        return "fetched"
    except FileExistsError:
        # Race condition: target appeared concurrently right before or during os.link
        temp_file.unlink(missing_ok=True)
        curr_sha = hashlib.sha256(target_file.read_bytes()).hexdigest().lower()
        if curr_sha == exp_sha_lower:
            return "already_fetched"
        raise QuarantineCollisionError(
            f"Quarantine collision race: '{target_file.name}' appeared with different hash {curr_sha} != {exp_sha_lower}"
        )
    except OSError as err:
        # Fail-closed: do NOT fall back to os.replace(), which clobbers target files under races!
        temp_file.unlink(missing_ok=True)
        raise RuntimeError(
            f"Atomic no-clobber promotion failed: filesystem does not support atomic link primitive: {err}"
        ) from err


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
    review_hash: str,
    approval_receipt: dict[str, Any] | None,
    run_id: str | None = None,
    raw_eml: bytes | None = None,
    data_dir: Path | None = None,
    policy: dict[str, Any] | None = None,
    workspace_root: str | Path | None = None,
    lease_id: str | None = None,
    conversation_id: str | None = None,
    allow_legacy: bool = False,
) -> dict[str, Any]:
    """Execute review-bound attachment fetch into temporary run quarantine."""
    pol = policy or DEFAULT_ATTACHMENT_POLICY

    # 1. Preflight Drift Guard (Strict Fail-Closed before any I/O)
    valid_cand, cand_err = validate_attachment_candidate_metadata(candidate)
    if not valid_cand:
        raise ValueError(f"Invalid attachment candidate metadata: {cand_err}")
    verify_attachment_drift(
        candidate=candidate,
        expected_account=str(account).strip(),
        expected_folder=str(folder).strip(),
        expected_envelope_id=str(envelope_id).strip(),
        expected_message_id=str(message_id).strip(),
        verify_hash=str(inventory_sha256).strip().lower(),
    )
    if str(candidate.get("part_locator", "")).strip() != str(part_locator).strip():
        raise PartLocatorDriftError(
            f"Part locator drift: candidate has '{candidate.get('part_locator')}', requested '{part_locator}'"
        )

    # 2. Receipt Verification & Review Hash Consistency
    computed_rev_hash = compute_review_hash(
        account=account,
        message_id=message_id,
        folder=folder,
        envelope_id=envelope_id,
        part_locator=part_locator,
        inventory_sha256=inventory_sha256,
    )
    if str(review_hash).strip().lower() != computed_rev_hash:
        raise ReceiptDriftError(
            f"Review hash drift: provided '{review_hash}' does not match computed '{computed_rev_hash}'"
        )
    verify_approval_receipt(approval_receipt, expected_review_hash=computed_rev_hash)

    # 3. Validate Run-ID & Candidate Filename before any I/O
    actual_run_id = str(run_id or f"run_{uuid.uuid4().hex[:12]}").strip()
    if not is_valid_run_id(actual_run_id):
        raise ValueError(f"Invalid or unsafe run_id: '{actual_run_id}'")

    raw_filename = candidate.get("filename")
    if raw_filename is None:
        clean_filename = f"part_{str(part_locator).replace('.', '_')}.dat"
    else:
        clean_filename = validate_attachment_filename(raw_filename)

    base_data_dir = data_dir or resolve_data_dir()
    attachments_root = base_data_dir / "attachments"
    run_dir = attachments_root / actual_run_id
    target_file = run_dir / clean_filename
    rel_path = f"data/mail-desk/attachments/{actual_run_id}/{clean_filename}"

    # Check path security & reparse points across parent hierarchy
    check_quarantine_path_security(target_file, attachments_root)

    # Check initial per-message quota based on candidate metadata
    candidate_size = int(candidate["size_bytes"])
    check_quarantine_quotas(
        run_dir,
        candidate_size,
        message_id=message_id,
        filename=clean_filename,
        sha256=str(inventory_sha256).strip().lower(),
        policy=pol,
    )

    # 4. Mandatory Workspace Lock Enforcement (Fail-Closed before any local mutation)
    verify_workspace_lock(
        workspace_root=workspace_root,
        lease_id=lease_id,
        conversation_id=conversation_id,
        allow_legacy=allow_legacy,
        data_dir=base_data_dir,
    )

    # 5. Idempotency Check (Pre-existing Target File)
    if target_file.exists():
        with _QuarantineInventoryLock(run_dir):
            if target_file.exists():
                existing_bytes = target_file.read_bytes()
                existing_sha = hashlib.sha256(existing_bytes).hexdigest().lower()
                exp_sha = str(inventory_sha256).strip().lower()

                if existing_sha != exp_sha:
                    raise QuarantineCollisionError(
                        f"Quarantine collision: '{clean_filename}' exists with different hash {existing_sha} != {exp_sha}"
                    )

                # 5a. Full Active Content & Disallowed Extension Check on pre-existing file
                eff_mime, is_active, active_reason = detect_mime_and_active_content(clean_filename, existing_bytes, policy=pol)
                if is_active:
                    if active_reason and "disallowed extension" in active_reason.lower():
                        raise DisallowedExtensionError(
                            f"Disallowed extension blocked in pre-existing '{clean_filename}': {active_reason}"
                        )
                    raise ActiveContentBlockedError(
                        f"Active content blocked in pre-existing '{clean_filename}': {active_reason}"
                    )

                # 5b. Full MIME & Extension Drift Validation on pre-existing file
                validate_mime_and_extension(
                    effective_mime=eff_mime,
                    inventory_mime=candidate.get("mime_type"),
                    filename=clean_filename,
                    policy=pol,
                )

                # 5c. Re-validate Quota with actual file size
                check_quarantine_quotas(
                    run_dir,
                    len(existing_bytes),
                    message_id=message_id,
                    filename=clean_filename,
                    sha256=existing_sha,
                    policy=pol,
                )

                # 5d. Ensure pre-existing file is recorded in message inventory
                _record_in_quarantine_inventory_unlocked(
                    run_dir,
                    message_id=message_id,
                    filename=clean_filename,
                    sha256=existing_sha,
                    size_bytes=len(existing_bytes),
                )

                return {
                    "status": "already_fetched",
                    "run_id": actual_run_id,
                    "relative_path": rel_path,
                    "inventory_sha256": exp_sha,
                    "fetch_sha256": existing_sha,
                    "effective_mime_type": eff_mime,
                    "size_bytes": len(existing_bytes),
                    "filename": clean_filename,
                    "error": None,
                }

    # 6. Fetch / Transport Bytes from Backend with Policy Timeout (25s default)
    download_timeout = pol.get("transport", {}).get("download_timeout_seconds", 25)
    if raw_eml is None:
        raw_eml = himalaya.fetch_raw_message_eml(
            str(envelope_id),
            folder=folder,
            account=str(account).strip(),
            timeout=download_timeout,
        )

    payload, _, _ = extract_part_from_eml(raw_eml, part_locator=str(part_locator), expected_message_id=message_id)

    # 7. Post-Fetch Verification: Hash, Active Content, MIME/Extension Drift
    fetch_sha = hashlib.sha256(payload).hexdigest().lower()
    exp_sha = str(inventory_sha256).strip().lower()
    if fetch_sha != exp_sha:
        raise HashDriftError(
            f"Hash drift detected on fetch: downloaded bytes have '{fetch_sha}', inventory has '{exp_sha}'"
        )

    eff_mime, is_active, active_reason = detect_mime_and_active_content(clean_filename, payload, policy=pol)
    if is_active:
        if active_reason and "disallowed extension" in active_reason.lower():
            raise DisallowedExtensionError(
                f"Disallowed extension blocked in '{clean_filename}': {active_reason}"
            )
        raise ActiveContentBlockedError(
            f"Active content blocked in '{clean_filename}': {active_reason}"
        )

    validate_mime_and_extension(
        effective_mime=eff_mime,
        inventory_mime=candidate.get("mime_type"),
        filename=clean_filename,
        policy=pol,
    )

    # 8. Unified Critical Section: Target Re-Check, Quota Re-Check, Promotion, Inventory Update
    run_dir.mkdir(parents=True, exist_ok=True)
    check_quarantine_path_security(target_file, attachments_root)

    with _QuarantineInventoryLock(run_dir):
        # 8a. Target re-check: another concurrent thread may have promoted this file
        if target_file.exists():
            existing_bytes = target_file.read_bytes()
            existing_sha = hashlib.sha256(existing_bytes).hexdigest().lower()
            if existing_sha != exp_sha:
                raise QuarantineCollisionError(
                    f"Quarantine collision: '{clean_filename}' exists with different hash {existing_sha} != {exp_sha}"
                )
            check_quarantine_quotas(
                run_dir,
                len(existing_bytes),
                message_id=message_id,
                filename=clean_filename,
                sha256=existing_sha,
                policy=pol,
            )
            _record_in_quarantine_inventory_unlocked(
                run_dir,
                message_id=message_id,
                filename=clean_filename,
                sha256=existing_sha,
                size_bytes=len(existing_bytes),
            )
            return {
                "status": "already_fetched",
                "run_id": actual_run_id,
                "relative_path": rel_path,
                "inventory_sha256": exp_sha,
                "fetch_sha256": existing_sha,
                "effective_mime_type": eff_mime,
                "size_bytes": len(existing_bytes),
                "filename": clean_filename,
                "error": None,
            }

        # 8b. Re-check per-message quotas with exact byte length under lock
        check_quarantine_quotas(
            run_dir,
            len(payload),
            message_id=message_id,
            filename=clean_filename,
            sha256=fetch_sha,
            policy=pol,
        )

        # 8c. Atomic No-Clobber Write via Sibling Temp
        temp_file = run_dir / f".{clean_filename}.{uuid.uuid4().hex}.tmp"
        try:
            temp_file.write_bytes(payload)
            promote_status = _atomic_no_clobber_promote(temp_file, target_file, exp_sha)
        except Exception:
            temp_file.unlink(missing_ok=True)
            raise

        # 8d. Update Inventory under the same lock
        _record_in_quarantine_inventory_unlocked(
            run_dir,
            message_id=message_id,
            filename=clean_filename,
            sha256=fetch_sha,
            size_bytes=len(payload),
        )

    return {
        "status": promote_status,
        "run_id": actual_run_id,
        "relative_path": rel_path,
        "inventory_sha256": exp_sha,
        "fetch_sha256": fetch_sha,
        "effective_mime_type": eff_mime,
        "size_bytes": len(payload),
        "filename": clean_filename,
        "error": None,
    }


def cleanup_run_quarantine(
    run_id: str,
    data_dir: Path | None = None,
    workspace_root: str | Path | None = None,
    lease_id: str | None = None,
    conversation_id: str | None = None,
    allow_legacy: bool = False,
) -> None:
    """Safely remove a validated quarantine run directory."""
    if not is_valid_run_id(run_id):
        raise ValueError(f"Cannot cleanup invalid run_id: '{run_id}'")

    base_data_dir = data_dir or resolve_data_dir()
    verify_workspace_lock(
        workspace_root=workspace_root,
        lease_id=lease_id,
        conversation_id=conversation_id,
        allow_legacy=allow_legacy,
        data_dir=base_data_dir,
    )
    attachments_root = base_data_dir / "attachments"
    run_dir = (attachments_root / run_id).resolve()

    # Boundary safety check
    if not str(run_dir).startswith(str(attachments_root.resolve())):
        raise ValueError(f"Run directory '{run_dir}' escapes attachments root.")

    if run_dir.exists() and run_dir.is_dir():
        shutil.rmtree(run_dir)
