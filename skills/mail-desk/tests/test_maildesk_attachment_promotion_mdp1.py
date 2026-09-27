"""Hermetic TDD tests for FR-09 / MD-P1: human-gated approval and read-only promotion preflight.

These tests are written before the production module (``core.attachment_promotion``)
exists, so the first run fails with an ImportError: that is the genuine Red gate.
They describe the complete MD-P1 contract:

1. ``compute_promotion_review_hash`` binds candidate schema/hash, the quarantine source
   identity, the re-resolved storage/scan_dir/target destination and the canonical
   filemap snapshot hash into one deterministic review hash.
2. ``verify_promotion_approval_receipt`` validates a Schema-1 receipt (receipt_type,
   decision, review_hash, timezone-aware approved_at/expires_at, optional comment) with
   an injected clock and fails closed on expired/future/implausibly-long receipts.
3. ``preflight_attachment_promotion`` is strictly read-only and validates in the
   documented order, returning a deterministic ``attachment_promotion_preflight``
   Schema 1 with bounded stopcodes and no absolute paths.

The whole module performs zero real cloud, mailbox or office access.  All filesystem
activity happens inside ``tempfile.TemporaryDirectory`` sandboxes.
"""

from __future__ import annotations

import builtins
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

from core import himalaya  # noqa: E402
from core import common as mail_common  # noqa: E402
from core.common import normalize_message_id  # noqa: E402
from core.attachment_filing import (  # noqa: E402
    FILEMAP_SCHEMA_URI,
    PROMOTION_STATUS_PENDING_HUMAN_REVIEW,
    STATUS_PROPOSED,
    compute_candidate_hash,
)
from core.attachment_fetch import WorkspaceLockError  # noqa: E402
from core.quarantine_preflight import (  # noqa: E402
    QuarantinePreflightError,
    TrackedQuarantineError,
)
from core import attachment_promotion as promotion  # noqa: E402
from core.attachment_promotion import (  # noqa: E402
    PromotionApprovalVerification,
    compute_promotion_review_hash,
    preflight_attachment_promotion,
    verify_promotion_approval_receipt,
)


NOW = datetime(2026, 9, 14, 10, 0, 0, tzinfo=timezone.utc)
APPROVED_AT = "2026-09-14T09:00:00Z"
EXPIRES_AT = "2026-09-20T09:00:00Z"

DEFAULT_SCAN_DIR = "data/cloud/PILOT"
DEFAULT_TARGET_DIR = "01_Admin/Correspondence"
DEFAULT_OUTPUT_DIR = "memory/cloud/projects/pilot-proj"
DEFAULT_OUTPUT_JSON = "memory/cloud/projects/pilot-proj/filemap.json"

DEFAULT_DATA = b"%PDF-1.4 MD-P1 promotion preflight payload"
DEFAULT_FILENAME = "minutes_2026.pdf"
DEFAULT_RUN_ID = "run_mdp1_happy"
DEFAULT_MESSAGE_ID = "msg-mdp1@example.org"

ALL_STOPCODES = (
    "approval_missing",
    "approval_invalid",
    "approval_expired",
    "candidate_drift",
    "source_drift",
    "lock_unavailable",
    "catalog_drift",
    "storage_not_writable",
    "filemap_drift",
    "unsafe_path",
    "parent_missing",
    "collision_detected",
    "preflight_error",
)


# ==============================================================================
# Fixture builders
# ==============================================================================

def make_filemap(
    *,
    storage_id: str = "primary",
    scope: str = "project",
    project: str = "pilot-proj",
    project_title: str = "Pilot Project",
    scan_dir: str = DEFAULT_SCAN_DIR,
    output_dir: str = DEFAULT_OUTPUT_DIR,
    updated_at: str = "2026-09-14 09:30:00",
    files: dict[str, dict[str, object]] | None = None,
) -> dict[str, object]:
    """Build a canonical Cloud-Atlas compliant filemap dictionary."""
    if files is None:
        files = {
            f"{scan_dir}/01_Admin/Correspondence/existing_doc.pdf": {
                "version": "1",
                "mtime": "2026-09-14 08:00:00",
                "size": "100 KB",
                "sha256": "f" * 64,
                "description": "Existing correspondence",
            }
        }
    return {
        "$schema": FILEMAP_SCHEMA_URI,
        "schema_version": 1,
        "kind": "cloud-filemap",
        "scope": scope,
        "storage_id": storage_id,
        "project": project,
        "project_title": project_title,
        "scan_dir": scan_dir,
        "output_dir": output_dir,
        "updated_at": updated_at,
        "files": files,
    }


def default_storage_cfg(
    *,
    scan_dir: str = DEFAULT_SCAN_DIR,
    target_dir: str = DEFAULT_TARGET_DIR,
    output_dir: str = DEFAULT_OUTPUT_DIR,
    output_json: str = DEFAULT_OUTPUT_JSON,
    **extra: object,
) -> dict[str, object]:
    """Build a canonical catalog cloud_sync storage configuration."""
    cfg: dict[str, object] = {
        "scan_dir": scan_dir,
        "target_dir": target_dir,
        "output_json": output_json,
        "output_dir": output_dir,
    }
    cfg.update(extra)
    return cfg


def make_catalogs() -> dict[str, object]:
    """Canonical catalog fixture with writable, archive, read-only and inactive storages."""
    return {
        "projects": [
            {"id": "pilot-proj", "title": "Pilot Project",
             "cloud_sync": {"primary": default_storage_cfg()}},
            {"id": "archive-proj", "title": "Archive Project",
             "cloud_sync": {"archive_main": default_storage_cfg(
                 scan_dir="data/cloud/ARCHIVE", output_dir="memory/cloud/projects/archive",
                 archive=True)}},
            {"id": "readonly-proj", "title": "Read Only Project",
             "cloud_sync": {"ro_main": default_storage_cfg(
                 scan_dir="data/cloud/RO", output_dir="memory/cloud/projects/ro",
                 read_only=True)}},
            {"id": "inactive-proj", "title": "Inactive Project",
             "cloud_sync": {"in_main": default_storage_cfg(
                 scan_dir="data/cloud/IN", output_dir="memory/cloud/projects/in",
                 active=False)}},
            {"id": "multi-storage-proj", "title": "Multi Storage Project",
             "cloud_sync": {
                 "drive": default_storage_cfg(scan_dir="data/cloud/DRIVE",
                                              output_dir="memory/cloud/projects/multi"),
                 "nextcloud": default_storage_cfg(scan_dir="data/cloud/NC",
                                                  output_dir="memory/cloud/projects/multi"),
             }},
            {"id": "no-cloud-proj", "title": "Project Without Cloud"},
        ],
        "topics": [],
    }


def with_project_storage(catalogs: dict[str, object], project_id: str,
                         storage_id: str, cfg: dict[str, object]) -> dict[str, object]:
    """Deep-copy catalogs and replace one project's cloud_sync with a single storage."""
    cats = copy.deepcopy(catalogs)
    for project in cats["projects"]:
        if project["id"] == project_id:
            project["cloud_sync"] = {storage_id: dict(cfg)}
    return cats


def write_quarantine(ws: Path, *, run_id: str = DEFAULT_RUN_ID, filename: str = DEFAULT_FILENAME,
                     data: bytes = DEFAULT_DATA, message_id: str = DEFAULT_MESSAGE_ID,
                     sha256_override: str | None = None, size_override: int | None = None,
                     count_override: int | None = None, total_override: int | None = None,
                     write_inventory: bool = True, write_file: bool = True) -> Path:
    """Create a real quarantine run directory with file and canonical inventory."""
    q_dir = ws / "data" / "mail-desk" / "attachments" / run_id
    q_dir.mkdir(parents=True, exist_ok=True)
    if write_file:
        (q_dir / filename).write_bytes(data)
    if write_inventory:
        norm_mid = normalize_message_id(message_id)
        inv = {
            "schema_version": 1,
            "messages": {
                norm_mid: {
                    "count": 1 if count_override is None else count_override,
                    "total_bytes": len(data) if total_override is None else total_override,
                    "files": {
                        filename: {
                            "sha256": hashlib.sha256(data).hexdigest()
                            if sha256_override is None else sha256_override,
                            "size_bytes": len(data) if size_override is None else size_override,
                        }
                    },
                }
            },
        }
        (q_dir / ".quarantine-inventory.json").write_text(json.dumps(inv), encoding="utf-8")
    return q_dir


def create_directory_junction(link: Path, target: Path) -> bool:
    """Create a real NTFS directory junction (Windows only); return success.

    A junction needs no elevated privilege (unlike a symlink) and is the exact reparse
    point the promotion preflight must reject.  Returns ``False`` when the platform or
    filesystem cannot create one so the caller can skip the test instead of failing.
    """
    if os.name != "nt":
        return False
    try:
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
        )
    except OSError:
        return False
    if result.returncode != 0 or not link.exists():
        return False
    try:
        stat_result = os.lstat(link)
    except OSError:
        return False
    return bool(getattr(stat_result, "st_file_attributes", 0) & 0x400)


def make_candidate(
    *,
    filemap: dict[str, object],
    data: bytes = DEFAULT_DATA,
    run_id: str = DEFAULT_RUN_ID,
    filename: str = DEFAULT_FILENAME,
    message_id: str = DEFAULT_MESSAGE_ID,
    account: str = "BOKU-MARTIN",
    folder: str = "INBOX",
    envelope_id: str = "7195",
    part_locator: str = "2",
    mime_type: str = "application/pdf",
    storage_id: str = "primary",
    scan_dir: str = DEFAULT_SCAN_DIR,
    target_dir: str = DEFAULT_TARGET_DIR,
    target_filename: str | None = None,
    status: str = STATUS_PROPOSED,
    promotion_status: str = PROMOTION_STATUS_PENDING_HUMAN_REVIEW,
    candidate_type: str = "attachment_filing_candidate",
    schema_version: int = 1,
) -> dict[str, object]:
    """Build a fully-formed, hash-bound MD-A5 attachment_filing_candidate."""
    sha256 = hashlib.sha256(data).hexdigest()
    norm_mid = normalize_message_id(message_id)
    quarantine_path = f"data/mail-desk/attachments/{run_id}/{filename}"
    target_filename = target_filename if target_filename is not None else filename
    target_relative_path = f"{target_dir}/{target_filename}"
    quarantine_evidence = {
        "run_id": run_id,
        "relative_path": quarantine_path,
        "fetch_status": "fetched",
        "status": "fetched",
        "sha256": sha256,
        "effective_mime_type": mime_type,
        "size_bytes": len(data),
        "physical_verified": True,
    }
    candidate: dict[str, object] = {
        "schema_version": schema_version,
        "candidate_type": candidate_type,
        "promotion_status": promotion_status,
        "status": status,
        "reason": "Filing candidate proposed for storage 'primary'",
        "source": {
            "account": account,
            "message_id": norm_mid,
            "folder": folder,
            "envelope_id": str(envelope_id),
            "part_locator": part_locator,
            "filename": filename,
            "original_filename": filename,
            "sha256": sha256,
            "quarantine_path": quarantine_path,
            "quarantine_evidence": quarantine_evidence,
        },
        "quarantine_evidence": dict(quarantine_evidence),
        "destination": {
            "storage_id": storage_id,
            "target_dir": target_dir,
            "target_filename": target_filename,
            "target_relative_path": target_relative_path,
        },
        "filemap_evidence": {
            "filemap_path": f"<in-memory:{storage_id}>",
            "filemap_updated_at": filemap["updated_at"],
            "schema_version": filemap["schema_version"],
            "kind": filemap["kind"],
            "scope": filemap["scope"],
            "storage_id": filemap["storage_id"],
            "project": filemap["project"],
            "scan_dir": scan_dir,
            "output_dir": filemap["output_dir"],
            "is_stale": False,
        },
        "handoff_evidence": None,
        "handoff_hash": None,
        "dedupe": {
            "already_present": False,
            "collision_detected": False,
            "existing_path": None,
            "existing_sha256": None,
        },
    }
    candidate["candidate_hash"] = compute_candidate_hash(candidate)
    return candidate


def make_receipt(candidate: dict[str, object], filemap: dict[str, object], *,
                 review_hash: str | None = None,
                 receipt_type: str = "attachment_promotion_approval",
                 decision: str = "approved",
                 approved_at: str = APPROVED_AT,
                 expires_at: str = EXPIRES_AT,
                 comment: str | None = None) -> dict[str, object]:
    """Build a Schema-1 human approval receipt bound to the candidate review payload."""
    if review_hash is None:
        review_hash = compute_promotion_review_hash(candidate, filemap)
    receipt: dict[str, object] = {
        "receipt_type": receipt_type,
        "decision": decision,
        "review_hash": review_hash,
        "approved_at": approved_at,
        "expires_at": expires_at,
    }
    if comment is not None:
        receipt["comment"] = comment
    return receipt


def build_env(tmp: str, *, target_state: str = "missing", data: bytes = DEFAULT_DATA,
              run_id: str = DEFAULT_RUN_ID, filename: str = DEFAULT_FILENAME,
              message_id: str = DEFAULT_MESSAGE_ID, scan_dir: str = DEFAULT_SCAN_DIR,
              target_dir: str = DEFAULT_TARGET_DIR, target_filename: str | None = None,
              storage_id: str = "primary", filemap: dict[str, object] | None = None,
              catalogs: dict[str, object] | None = None,
              updated_at: str = "2026-09-14 09:30:00",
              collision_bytes: bytes = b"different content"):
    """Create the real workspace tree (quarantine + scan_dir), candidate and fixtures."""
    ws = Path(tmp)
    if filemap is None:
        filemap = make_filemap(storage_id=storage_id, scan_dir=scan_dir, updated_at=updated_at)
    if catalogs is None:
        catalogs = make_catalogs()
    write_quarantine(ws, run_id=run_id, filename=filename, data=data, message_id=message_id)
    eff_target_filename = target_filename if target_filename is not None else filename
    parent = ws / Path(scan_dir) / Path(target_dir)
    parent.mkdir(parents=True, exist_ok=True)
    target = parent / eff_target_filename
    if target_state == "already_present":
        target.write_bytes(data)
    elif target_state == "collision":
        target.write_bytes(collision_bytes)
    candidate = make_candidate(
        filemap=filemap, data=data, run_id=run_id, filename=filename, message_id=message_id,
        scan_dir=scan_dir, target_dir=target_dir, target_filename=target_filename,
        storage_id=storage_id,
    )
    return ws, filemap, catalogs, candidate


def tree_snapshot(ws: Path) -> list[str]:
    """Return a sorted, workspace-relative listing of every file and directory."""
    return sorted(
        path.relative_to(ws).as_posix()
        for path in ws.rglob("*")
    )


class WriteTrap:
    """Context manager that blocks every filesystem/cloud/mailbox write seam.

    Read-only ``open`` calls stay functional so the preflight can still inspect the
    real quarantine and target state; any write intent raises ``AssertionError``.
    """

    _WRITE_CHARS = ("w", "a", "x", "+")

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self._patchers: list[object] = []

    def _record(self, seam: str, detail: str) -> None:
        self.calls.append((seam, detail))
        raise AssertionError(f"MD-P1 write prohibited: {seam} ({detail})")

    def __enter__(self) -> "WriteTrap":
        trap = self
        real_path_open = Path.open
        real_builtin_open = builtins.open

        def trapped_path_open(path_self, *args, **kwargs):
            mode = kwargs.get("mode")
            if mode is None and args:
                mode = args[0]
            mode = "r" if mode is None else mode
            if any(ch in str(mode) for ch in trap._WRITE_CHARS):
                trap._record("pathlib.Path.open", str(path_self))
            return real_path_open(path_self, *args, **kwargs)

        def trapped_builtin_open(file, mode="r", *args, **kwargs):
            if any(ch in str(mode) for ch in trap._WRITE_CHARS):
                trap._record("builtins.open", str(file))
            return real_builtin_open(file, mode, *args, **kwargs)

        def blocker(seam_name):
            def _blocked(*_args, **_kwargs):
                trap._record(seam_name, "")
            return _blocked

        self._patchers.append(patch.object(Path, "open", trapped_path_open))
        self._patchers.append(patch("builtins.open", trapped_builtin_open))
        for attr in ("mkdir", "unlink", "rmdir", "touch", "write_bytes", "write_text",
                     "rename", "replace", "symlink_to", "hardlink_to"):
            self._patchers.append(patch.object(Path, attr, blocker(f"Path.{attr}")))
        for name in ("replace", "remove", "unlink", "rename", "mkdir", "makedirs", "rmdir",
                     "link", "symlink"):
            if hasattr(os, name):
                self._patchers.append(patch(f"os.{name}", blocker(f"os.{name}")))
        for name in ("copy", "copy2", "copyfile", "move", "rmtree"):
            self._patchers.append(patch(f"shutil.{name}", blocker(f"shutil.{name}")))
        self._patchers.append(patch.object(himalaya, "run_himalaya", blocker("himalaya.run_himalaya")))
        self._patchers.append(patch.object(mail_common, "atomic_write_json",
                                           blocker("common.atomic_write_json")))
        self._patchers.append(patch.object(mail_common, "atomic_write_text",
                                           blocker("common.atomic_write_text")))
        self._patchers.append(patch.object(mail_common, "atomic_rewrite_jsonl",
                                           blocker("common.atomic_rewrite_jsonl")))
        for patcher in self._patchers:
            patcher.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        for patcher in reversed(self._patchers):
            patcher.stop()
        return None


# ==============================================================================
# Review payload hash
# ==============================================================================

class PromotionReviewHashTests(unittest.TestCase):
    """``compute_promotion_review_hash`` binds the canonical review payload."""

    def setUp(self) -> None:
        self.now = NOW
        self.filemap = make_filemap()
        self.candidate = make_candidate(filemap=self.filemap)

    def test_review_hash_is_deterministic_sha256(self) -> None:
        first = compute_promotion_review_hash(self.candidate, self.filemap)
        second = compute_promotion_review_hash(self.candidate, self.filemap)
        self.assertEqual(first, second)
        self.assertRegex(first, r"^[0-9a-f]{64}$")

    def test_review_hash_changes_when_candidate_hash_changes(self) -> None:
        baseline = compute_promotion_review_hash(self.candidate, self.filemap)
        drifted = copy.deepcopy(self.candidate)
        drifted["candidate_hash"] = "0" * 64
        self.assertNotEqual(baseline, compute_promotion_review_hash(drifted, self.filemap))

    def test_review_hash_changes_when_destination_changes(self) -> None:
        baseline = compute_promotion_review_hash(self.candidate, self.filemap)
        for field, value in (("target_dir", "Other"), ("target_filename", "x.pdf"),
                             ("target_relative_path", "Other/x.pdf"), ("storage_id", "other")):
            drifted = copy.deepcopy(self.candidate)
            drifted["destination"][field] = value
            with self.subTest(field=field):
                self.assertNotEqual(baseline, compute_promotion_review_hash(drifted, self.filemap))

    def test_review_hash_changes_when_source_identity_changes(self) -> None:
        baseline = compute_promotion_review_hash(self.candidate, self.filemap)
        for field, value in (("account", "attacker"), ("folder", "Archive"),
                             ("envelope_id", "1"), ("part_locator", "9"),
                             ("message_id", "other@example.org"), ("sha256", "a" * 64),
                             ("quarantine_path", "data/mail-desk/attachments/x/y")):
            drifted = copy.deepcopy(self.candidate)
            drifted["source"][field] = value
            with self.subTest(field=field):
                self.assertNotEqual(baseline, compute_promotion_review_hash(drifted, self.filemap))

    def test_review_hash_changes_when_filemap_snapshot_changes(self) -> None:
        baseline = compute_promotion_review_hash(self.candidate, self.filemap)
        drifted = copy.deepcopy(self.filemap)
        drifted["files"]["data/cloud/PILOT/01_Admin/Correspondence/existing_doc.pdf"]["sha256"] = "a" * 64
        self.assertNotEqual(baseline, compute_promotion_review_hash(self.candidate, drifted))

    def test_review_hash_uses_explicit_resolved_destination_overrides(self) -> None:
        baseline = compute_promotion_review_hash(self.candidate, self.filemap)
        explicit = compute_promotion_review_hash(
            self.candidate, self.filemap, storage_id="other",
            target_relative_path="Other/x.pdf",
        )
        self.assertNotEqual(baseline, explicit)


# ==============================================================================
# Approval receipt verifier
# ==============================================================================

class PromotionApprovalReceiptVerifierTests(unittest.TestCase):
    """``verify_promotion_approval_receipt`` is fail-closed and clock-injectable."""

    def setUp(self) -> None:
        self.now = NOW
        self.filemap = make_filemap()
        self.candidate = make_candidate(filemap=self.filemap)
        self.review_hash = compute_promotion_review_hash(self.candidate, self.filemap)

    def _verify(self, receipt, **kwargs):
        return verify_promotion_approval_receipt(
            receipt, expected_review_hash=self.review_hash, current_time=self.now, **kwargs
        )

    def test_valid_receipt_returns_typed_ok(self) -> None:
        receipt = make_receipt(self.candidate, self.filemap, comment="approved in review")
        result = self._verify(receipt)
        self.assertIsInstance(result, PromotionApprovalVerification)
        self.assertTrue(result.ok)
        self.assertIsNone(result.stopcode)
        self.assertEqual(self.review_hash, result.review_hash)
        self.assertEqual("approved in review", result.comment)

    def test_missing_receipt_is_approval_missing(self) -> None:
        for receipt in (None, {}, "not-a-mapping"):
            with self.subTest(receipt=receipt):
                result = self._verify(receipt)
                self.assertFalse(result.ok)
                self.assertEqual("approval_missing", result.stopcode)

    def test_wrong_receipt_type_is_approval_invalid(self) -> None:
        receipt = make_receipt(self.candidate, self.filemap, receipt_type="attachment_auto_evaluation")
        self.assertEqual("approval_invalid", self._verify(receipt).stopcode)

    def test_non_approved_decision_is_approval_invalid(self) -> None:
        receipt = make_receipt(self.candidate, self.filemap, decision="denied")
        self.assertEqual("approval_invalid", self._verify(receipt).stopcode)

    def test_review_hash_mismatch_is_approval_invalid(self) -> None:
        receipt = make_receipt(self.candidate, self.filemap, review_hash="1" * 64)
        self.assertEqual("approval_invalid", self._verify(receipt).stopcode)

    def test_machine_receipt_class_is_rejected_fail_closed(self) -> None:
        receipt = make_receipt(self.candidate, self.filemap)
        receipt["receipt_class"] = "machine"
        self.assertEqual("approval_invalid", self._verify(receipt).stopcode)

    def test_machine_approver_identity_is_rejected_even_with_human_type(self) -> None:
        receipt = make_receipt(self.candidate, self.filemap)
        receipt["approved_by"] = "mail_desk_auto_evaluator"
        self.assertEqual("approval_invalid", self._verify(receipt).stopcode)

    def test_naive_timestamps_are_rejected(self) -> None:
        receipt = make_receipt(self.candidate, self.filemap, approved_at="2026-09-14T09:00:00")
        self.assertEqual("approval_invalid", self._verify(receipt).stopcode)

    def test_future_issued_receipt_is_rejected(self) -> None:
        receipt = make_receipt(self.candidate, self.filemap,
                               approved_at="2026-09-15T09:00:00Z",
                               expires_at="2026-09-20T09:00:00Z")
        self.assertEqual("approval_invalid", self._verify(receipt).stopcode)

    def test_implausibly_long_validity_is_rejected(self) -> None:
        receipt = make_receipt(self.candidate, self.filemap,
                               approved_at=APPROVED_AT,
                               expires_at="2026-12-20T09:00:00Z")
        self.assertEqual("approval_invalid", self._verify(receipt).stopcode)

    def test_expired_receipt_is_approval_expired(self) -> None:
        receipt = make_receipt(self.candidate, self.filemap,
                               expires_at="2026-09-14T09:30:00Z")
        self.assertEqual("approval_expired", self._verify(receipt).stopcode)

    def test_boundary_expiry_equalling_now_is_expired(self) -> None:
        receipt = make_receipt(self.candidate, self.filemap,
                               expires_at="2026-09-14T10:00:00Z")
        self.assertEqual("approval_expired", self._verify(receipt).stopcode)

    def test_optional_comment_is_preserved_but_not_required(self) -> None:
        receipt = make_receipt(self.candidate, self.filemap)
        result = self._verify(receipt)
        self.assertTrue(result.ok)
        self.assertIsNone(result.comment)


# ==============================================================================
# Read-only preflight
# ==============================================================================

class PromotionPreflightTests(unittest.TestCase):
    """``preflight_attachment_promotion`` validates read-only and deterministically."""

    def setUp(self) -> None:
        self.maxDiff = None
        self.now = NOW
        self._lock_patcher = patch.object(promotion, "verify_workspace_lock", return_value=None)
        self._lock_patcher.start()
        self._tracked_patcher = patch.object(promotion, "verify_no_tracked_quarantine",
                                             return_value=None)
        self._tracked_patcher.start()

    def tearDown(self) -> None:
        self._tracked_patcher.stop()
        self._lock_patcher.stop()

    def _preflight(self, candidate, catalogs, filemap, ws, receipt, *, decision=None,
                   lease_id=None, conversation_id=None, now=None):
        return preflight_attachment_promotion(
            candidate, catalogs,
            receipt=receipt,
            filemap=filemap,
            workspace_root=ws,
            decision=decision,
            lease_id=lease_id,
            conversation_id=conversation_id,
            current_time=now or self.now,
        )

    def _happy_env(self, tmp):
        ws, filemap, catalogs, candidate = build_env(tmp)
        receipt = make_receipt(candidate, filemap)
        return ws, filemap, catalogs, candidate, receipt

    # ------------------------------------------------------------------
    # Ready / already_present / collision
    # ------------------------------------------------------------------

    def test_ready_happy_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws, filemap, catalogs, candidate, receipt = self._happy_env(tmp)
            out = self._preflight(candidate, catalogs, filemap, ws, receipt)
        self.assertEqual("ready", out["status"])
        self.assertEqual("preconditions_satisfied", out["reason"])
        self.assertEqual(candidate["candidate_hash"], out["candidate_hash"])
        self.assertEqual(candidate["source"]["sha256"], out["source_sha256"])
        self.assertEqual("primary", out["storage_id"])
        self.assertEqual("01_Admin/Correspondence/minutes_2026.pdf", out["target_relative_path"])
        self.assertEqual(out["review_hash"],
                         compute_promotion_review_hash(candidate, filemap))
        for field in ("candidate_hash", "review_hash", "source_sha256", "target_path_fingerprint",
                      "filemap_snapshot_hash", "preflight_hash"):
            self.assertRegex(str(out[field]), r"^[0-9a-f]{64}$", msg=field)
        self.assertIsNotNone(out["checked_at"])

    def test_output_contains_no_absolute_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws, filemap, catalogs, candidate, receipt = self._happy_env(tmp)
            out = self._preflight(candidate, catalogs, filemap, ws, receipt)
            serialized = json.dumps(out)
        self.assertNotIn(str(ws), serialized)
        self.assertNotIn("\\\\", serialized)

    def test_deterministic_output_for_identical_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws, filemap, catalogs, candidate, receipt = self._happy_env(tmp)
            first = self._preflight(candidate, catalogs, filemap, ws, receipt)
            second = self._preflight(candidate, catalogs, filemap, ws, receipt)
        self.assertEqual(first, second)
        self.assertEqual(first["preflight_hash"], second["preflight_hash"])

    def test_already_present_when_target_hash_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws, filemap, catalogs, candidate = build_env(tmp, target_state="already_present")
            receipt = make_receipt(candidate, filemap)
            out = self._preflight(candidate, catalogs, filemap, ws, receipt)
        self.assertEqual("already_present", out["status"])

    def test_collision_detected_when_target_hash_differs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws, filemap, catalogs, candidate = build_env(tmp, target_state="collision")
            receipt = make_receipt(candidate, filemap)
            out = self._preflight(candidate, catalogs, filemap, ws, receipt)
        self.assertEqual("collision_detected", out["status"])
        self.assertIn(out["status"], ALL_STOPCODES)

    def test_checked_at_and_preflight_hash_follow_injected_clock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws, filemap, catalogs, candidate, receipt = self._happy_env(tmp)
            first = self._preflight(candidate, catalogs, filemap, ws, receipt, now=self.now)
            later = self.now + timedelta(hours=2)
            second = self._preflight(candidate, catalogs, filemap, ws, receipt, now=later)
        self.assertNotEqual(first["checked_at"], second["checked_at"])
        self.assertNotEqual(first["preflight_hash"], second["preflight_hash"])
        self.assertEqual(first["review_hash"], second["review_hash"])

    # ------------------------------------------------------------------
    # Candidate gate and hash drift
    # ------------------------------------------------------------------

    def test_candidate_hash_drift_is_candidate_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws, filemap, catalogs, candidate, receipt = self._happy_env(tmp)
            candidate["destination"]["target_relative_path"] = "tampered.pdf"
            out = self._preflight(candidate, catalogs, filemap, ws, receipt)
        self.assertEqual("candidate_drift", out["status"])

    def test_non_proposed_status_is_candidate_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws, filemap, catalogs, candidate, receipt = self._happy_env(tmp)
            for status in ("storage_review_required", "directory_review_required",
                           "already_present", "collision_detected", "not_configured"):
                drifted = copy.deepcopy(candidate)
                drifted["status"] = status
                drifted["candidate_hash"] = compute_candidate_hash(drifted)
                with self.subTest(status=status):
                    out = self._preflight(drifted, catalogs, filemap, ws, receipt)
                    self.assertEqual("candidate_drift", out["status"])

    def test_wrong_promotion_status_and_candidate_type_are_candidate_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws, filemap, catalogs, candidate, receipt = self._happy_env(tmp)
            for field, value in (("promotion_status", "approved"),
                                 ("candidate_type", "other_candidate"),
                                 ("schema_version", 2)):
                drifted = copy.deepcopy(candidate)
                drifted[field] = value
                drifted["candidate_hash"] = compute_candidate_hash(drifted)
                with self.subTest(field=field):
                    self.assertEqual(
                        "candidate_drift",
                        self._preflight(drifted, catalogs, filemap, ws, receipt)["status"],
                    )

    def test_non_mapping_candidate_is_candidate_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws, filemap, catalogs, _candidate, receipt = self._happy_env(tmp)
            out = self._preflight("not-a-candidate", catalogs, filemap, ws, receipt)
        self.assertEqual("candidate_drift", out["status"])

    # ------------------------------------------------------------------
    # Source / quarantine evidence drift
    # ------------------------------------------------------------------

    def test_bound_payload_value_drift_individually_rejected(self) -> None:
        mutations = (
            ("source", "account", "attacker"),
            ("source", "folder", "Archive"),
            ("source", "envelope_id", "1"),
            ("source", "part_locator", "9"),
            ("destination", "storage_id", "other"),
            ("destination", "target_dir", "Other"),
            ("destination", "target_filename", "other.pdf"),
            ("destination", "target_relative_path", "Other/other.pdf"),
            ("filemap_evidence", "scan_dir", "data/cloud/OTHER"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            ws, filemap, catalogs, candidate, receipt = self._happy_env(tmp)
            for section, field, value in mutations:
                drifted = copy.deepcopy(candidate)
                drifted[section][field] = value
                drifted["candidate_hash"] = compute_candidate_hash(drifted)
                with self.subTest(section=section, field=field):
                    out = self._preflight(drifted, catalogs, filemap, ws, receipt)
                    self.assertEqual("approval_invalid", out["status"])

    def test_source_quarantine_identity_drift_is_source_drift(self) -> None:
        mutations = (
            ("message_id", "other-message@example.org"),
            ("sha256", "0" * 64),
            ("quarantine_path", "data/mail-desk/attachments/other/x.pdf"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            ws, filemap, catalogs, candidate, receipt = self._happy_env(tmp)
            for field, value in mutations:
                drifted = copy.deepcopy(candidate)
                drifted["source"][field] = value
                drifted["candidate_hash"] = compute_candidate_hash(drifted)
                with self.subTest(field=field):
                    self.assertEqual(
                        "source_drift",
                        self._preflight(drifted, catalogs, filemap, ws, receipt)["status"],
                    )

    def test_quarantine_disk_hash_drift_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws, filemap, catalogs, candidate, receipt = self._happy_env(tmp)
            target = ws / "data" / "mail-desk" / "attachments" / DEFAULT_RUN_ID / DEFAULT_FILENAME
            target.write_bytes(b"tampered on disk")
            out = self._preflight(candidate, catalogs, filemap, ws, receipt)
        self.assertEqual("source_drift", out["status"])

    def test_quarantine_inventory_hash_drift_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws, filemap, catalogs, candidate, receipt = self._happy_env(tmp)
            inv_path = ws / "data" / "mail-desk" / "attachments" / DEFAULT_RUN_ID / ".quarantine-inventory.json"
            inv = json.loads(inv_path.read_text(encoding="utf-8"))
            inv["messages"][normalize_message_id(DEFAULT_MESSAGE_ID)]["files"][DEFAULT_FILENAME]["sha256"] = "b" * 64
            inv_path.write_text(json.dumps(inv), encoding="utf-8")
            out = self._preflight(candidate, catalogs, filemap, ws, receipt)
        self.assertEqual("source_drift", out["status"])

    def test_quarantine_size_drift_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws, filemap, catalogs, candidate, receipt = self._happy_env(tmp)
            inv_path = ws / "data" / "mail-desk" / "attachments" / DEFAULT_RUN_ID / ".quarantine-inventory.json"
            inv = json.loads(inv_path.read_text(encoding="utf-8"))
            entry = inv["messages"][normalize_message_id(DEFAULT_MESSAGE_ID)]
            entry["files"][DEFAULT_FILENAME]["size_bytes"] = len(DEFAULT_DATA) + 5
            entry["total_bytes"] = len(DEFAULT_DATA) + 5
            inv_path.write_text(json.dumps(inv), encoding="utf-8")
            out = self._preflight(candidate, catalogs, filemap, ws, receipt)
        self.assertEqual("source_drift", out["status"])

    def test_missing_quarantine_file_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws, filemap, catalogs, candidate = build_env(tmp)
            (ws / "data" / "mail-desk" / "attachments" / DEFAULT_RUN_ID / DEFAULT_FILENAME).unlink()
            receipt = make_receipt(candidate, filemap)
            out = self._preflight(candidate, catalogs, filemap, ws, receipt)
        self.assertEqual("source_drift", out["status"])

    def test_missing_quarantine_inventory_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws, filemap, catalogs, candidate = build_env(tmp)
            (ws / "data" / "mail-desk" / "attachments" / DEFAULT_RUN_ID
             / ".quarantine-inventory.json").unlink()
            receipt = make_receipt(candidate, filemap)
            out = self._preflight(candidate, catalogs, filemap, ws, receipt)
        self.assertEqual("source_drift", out["status"])

    def test_tracked_quarantine_guard_failure_maps_to_source_drift(self) -> None:
        self._tracked_patcher.stop()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                ws, filemap, catalogs, candidate, receipt = self._happy_env(tmp)
                with patch.object(promotion, "verify_no_tracked_quarantine",
                                  side_effect=TrackedQuarantineError("tracked")):
                    out = self._preflight(candidate, catalogs, filemap, ws, receipt)
            self.assertEqual("source_drift", out["status"])
        finally:
            self._tracked_patcher.start()

    def test_tracked_quarantine_engine_error_maps_to_preflight_error(self) -> None:
        self._tracked_patcher.stop()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                ws, filemap, catalogs, candidate, receipt = self._happy_env(tmp)
                with patch.object(promotion, "verify_no_tracked_quarantine",
                                  side_effect=QuarantinePreflightError("engine")):
                    out = self._preflight(candidate, catalogs, filemap, ws, receipt)
            self.assertEqual("preflight_error", out["status"])
        finally:
            self._tracked_patcher.start()

    # ------------------------------------------------------------------
    # Approval receipt through the preflight
    # ------------------------------------------------------------------

    def test_preflight_approval_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws, filemap, catalogs, candidate, _receipt = self._happy_env(tmp)
            out = self._preflight(candidate, catalogs, filemap, ws, None)
        self.assertEqual("approval_missing", out["status"])

    def test_preflight_approval_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws, filemap, catalogs, candidate, receipt = self._happy_env(tmp)
            receipt["review_hash"] = "0" * 64
            out = self._preflight(candidate, catalogs, filemap, ws, receipt)
        self.assertEqual("approval_invalid", out["status"])

    def test_preflight_approval_expired(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws, filemap, catalogs, candidate = build_env(tmp)
            receipt = make_receipt(candidate, filemap, expires_at="2026-09-14T09:30:00Z")
            out = self._preflight(candidate, catalogs, filemap, ws, receipt)
        self.assertEqual("approval_expired", out["status"])

    def test_preflight_clock_injection_expires_a_previously_valid_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws, filemap, catalogs, candidate, receipt = self._happy_env(tmp)
            before = self._preflight(candidate, catalogs, filemap, ws, receipt, now=self.now)
            after = self._preflight(candidate, catalogs, filemap, ws, receipt,
                                    now=self.now + timedelta(days=30))
        self.assertEqual("ready", before["status"])
        self.assertEqual("approval_expired", after["status"])

    def test_machine_receipt_is_rejected_by_the_promotion_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws, filemap, catalogs, candidate, receipt = self._happy_env(tmp)
            receipt["approved_by"] = "mail_desk_auto_evaluator"
            out = self._preflight(candidate, catalogs, filemap, ws, receipt)
        self.assertEqual("approval_invalid", out["status"])

    # ------------------------------------------------------------------
    # Lock ownership
    # ------------------------------------------------------------------

    def test_missing_lock_is_lock_unavailable(self) -> None:
        self._lock_patcher.stop()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                ws, filemap, catalogs, candidate, receipt = self._happy_env(tmp)
                with patch.object(promotion, "verify_workspace_lock",
                                  side_effect=WorkspaceLockError("no active lock")):
                    out = self._preflight(candidate, catalogs, filemap, ws, receipt)
            self.assertEqual("lock_unavailable", out["status"])
        finally:
            self._lock_patcher.start()

    def test_foreign_lock_is_lock_unavailable(self) -> None:
        self._lock_patcher.stop()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                ws, filemap, catalogs, candidate, receipt = self._happy_env(tmp)
                with patch.object(promotion, "verify_workspace_lock",
                                  side_effect=WorkspaceLockError("not owned by this invocation")):
                    out = self._preflight(candidate, catalogs, filemap, ws, receipt)
            self.assertEqual("lock_unavailable", out["status"])
        finally:
            self._lock_patcher.start()

    def test_embedded_lock_and_workspace_values_are_ignored(self) -> None:
        self._lock_patcher.stop()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                ws, filemap, catalogs, candidate, receipt = self._happy_env(tmp)
                candidate["lease_id"] = "forged-candidate-lease"
                candidate["conversation_id"] = "forged-candidate-conv"
                candidate["workspace_root"] = "C:/forged"
                candidate["allow_legacy"] = True
                receipt["lease_id"] = "forged-receipt-lease"
                receipt["conversation_id"] = "forged-receipt-conv"
                receipt["allow_legacy"] = True
                seen: dict[str, object] = {}

                def _capture(*_args, **kwargs):
                    seen.update(kwargs)
                    return None

                with patch.object(promotion, "verify_workspace_lock", side_effect=_capture):
                    out = self._preflight(candidate, catalogs, filemap, ws, receipt)
            self.assertEqual("ready", out["status"])
            self.assertIsNone(seen.get("lease_id"))
            self.assertIsNone(seen.get("conversation_id"))
        finally:
            self._lock_patcher.start()

    def test_trusted_control_plane_lease_is_forwarded(self) -> None:
        self._lock_patcher.stop()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                ws, filemap, catalogs, candidate, receipt = self._happy_env(tmp)
                seen: dict[str, object] = {}

                def _capture(*_args, **kwargs):
                    seen.update(kwargs)
                    return None

                with patch.object(promotion, "verify_workspace_lock", side_effect=_capture):
                    out = self._preflight(candidate, catalogs, filemap, ws, receipt,
                                          lease_id="lease-123", conversation_id="conv-9")
            self.assertEqual("ready", out["status"])
            self.assertEqual("lease-123", seen.get("lease_id"))
            self.assertEqual("conv-9", seen.get("conversation_id"))
        finally:
            self._lock_patcher.start()

    # ------------------------------------------------------------------
    # Catalog storage re-resolution
    # ------------------------------------------------------------------

    def test_missing_catalog_entity_is_catalog_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws, filemap, catalogs, candidate, receipt = self._happy_env(tmp)
            empty = {"projects": [], "topics": []}
            out = self._preflight(candidate, empty, filemap, ws, receipt)
        self.assertEqual("catalog_drift", out["status"])

    def test_catalog_storage_id_drift_is_catalog_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws, filemap, catalogs, candidate, receipt = self._happy_env(tmp)
            drifted = with_project_storage(catalogs, "pilot-proj", "renamed",
                                           default_storage_cfg())
            out = self._preflight(candidate, drifted, filemap, ws, receipt)
        self.assertEqual("catalog_drift", out["status"])

    def test_multiple_catalog_storages_is_catalog_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws, filemap, catalogs, candidate, receipt = self._happy_env(tmp)
            multi = copy.deepcopy(catalogs)
            multi["projects"][0]["cloud_sync"]["secondary"] = default_storage_cfg(
                scan_dir="data/cloud/SECOND")
            out = self._preflight(candidate, multi, filemap, ws, receipt)
        self.assertEqual("catalog_drift", out["status"])

    def test_catalog_scan_dir_drift_is_catalog_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws, filemap, catalogs, candidate, receipt = self._happy_env(tmp)
            drifted = with_project_storage(catalogs, "pilot-proj", "primary",
                                           default_storage_cfg(scan_dir="data/cloud/OTHER"))
            out = self._preflight(candidate, drifted, filemap, ws, receipt)
        self.assertEqual("catalog_drift", out["status"])

    def test_archived_storage_is_storage_not_writable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            filemap = make_filemap(project="archive-proj", storage_id="archive_main",
                                   scan_dir="data/cloud/ARCHIVE",
                                   output_dir="memory/cloud/projects/archive")
            ws, filemap, catalogs, candidate = build_env(
                tmp, storage_id="archive_main", scan_dir="data/cloud/ARCHIVE", filemap=filemap,
                catalogs=make_catalogs())
            receipt = make_receipt(candidate, filemap)
            out = self._preflight(candidate, catalogs, filemap, ws, receipt)
        self.assertEqual("storage_not_writable", out["status"])

    def test_read_only_storage_is_storage_not_writable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            filemap = make_filemap(project="readonly-proj", storage_id="ro_main",
                                   scan_dir="data/cloud/RO",
                                   output_dir="memory/cloud/projects/ro")
            ws, filemap, catalogs, candidate = build_env(
                tmp, storage_id="ro_main", scan_dir="data/cloud/RO", filemap=filemap)
            receipt = make_receipt(candidate, filemap)
            out = self._preflight(candidate, catalogs, filemap, ws, receipt)
        self.assertEqual("storage_not_writable", out["status"])

    def test_inactive_storage_is_storage_not_writable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            filemap = make_filemap(project="inactive-proj", storage_id="in_main",
                                   scan_dir="data/cloud/IN",
                                   output_dir="memory/cloud/projects/in")
            ws, filemap, catalogs, candidate = build_env(
                tmp, storage_id="in_main", scan_dir="data/cloud/IN", filemap=filemap)
            receipt = make_receipt(candidate, filemap)
            out = self._preflight(candidate, catalogs, filemap, ws, receipt)
        self.assertEqual("storage_not_writable", out["status"])

    def test_storage_id_named_archive_without_markers_is_writable(self) -> None:
        """A storage merely *named* archive* is writable when it carries no marker.

        The old ID-prefix heuristic falsely rejected ``archive_2026_active``.  Only
        property markers (archived/archive/read_only/active/enabled/disabled/status)
        may gate writability; the identifier alone is never sufficient.
        """
        with tempfile.TemporaryDirectory() as tmp:
            catalogs = with_project_storage(
                make_catalogs(), "pilot-proj", "archive_2026_active", default_storage_cfg())
            ws, filemap, catalogs, candidate = build_env(
                tmp, storage_id="archive_2026_active", catalogs=catalogs)
            receipt = make_receipt(candidate, filemap)
            out = self._preflight(candidate, catalogs, filemap, ws, receipt)
        self.assertEqual("ready", out["status"])

    def test_candidate_bound_storage_in_multi_storage_catalog_is_storage_not_writable(
        self,
    ) -> None:
        """A non-writable candidate-bound storage stops even when siblings exist.

        With an explicit decision the candidate-bound entry is evaluated by its
        read_only/archived/inactive flags *before* the unique-storage check, per FR-09
        storage-resolution step 4, so a multi-storage catalog must not degrade to
        ``catalog_drift``.
        """
        for storage_id, extra in (
            ("ro_second", {"read_only": True}),
            ("arch_second", {"archive": True}),
            ("inactive_second", {"active": False}),
        ):
            with self.subTest(storage_id=storage_id):
                project_id = "multi-gate-proj"
                scan_dir = f"data/cloud/{storage_id.upper()}"
                with tempfile.TemporaryDirectory() as tmp:
                    filemap = make_filemap(
                        project=project_id, storage_id=storage_id, scan_dir=scan_dir,
                        output_dir="memory/cloud/projects/multi-gate",
                    )
                    catalogs = copy.deepcopy(make_catalogs())
                    catalogs["projects"].append({
                        "id": project_id,
                        "title": "Multi Gate Project",
                        "cloud_sync": {
                            "primary": default_storage_cfg(),
                            storage_id: default_storage_cfg(
                                scan_dir=scan_dir,
                                output_dir="memory/cloud/projects/multi-gate",
                                **extra),
                        },
                    })
                    ws, filemap, catalogs, candidate = build_env(
                        tmp, storage_id=storage_id, scan_dir=scan_dir,
                        filemap=filemap, catalogs=catalogs)
                    receipt = make_receipt(candidate, filemap)
                    decision = {"kind": "project", "id": project_id}
                    out = self._preflight(candidate, catalogs, filemap, ws, receipt,
                                          decision=decision)
                self.assertEqual("storage_not_writable", out["status"])

    def test_non_mapping_storage_config_is_storage_not_writable(self) -> None:
        """A non-Mapping storage entry is ``storage_not_writable`` even when siblings exist."""
        project_id = "broken-storage-proj"
        with tempfile.TemporaryDirectory() as tmp:
            filemap = make_filemap(project=project_id, storage_id="broken",
                                   output_dir="memory/cloud/projects/broken")
            catalogs = copy.deepcopy(make_catalogs())
            catalogs["projects"].append({
                "id": project_id,
                "title": "Broken Storage Project",
                "cloud_sync": {
                    "primary": default_storage_cfg(),
                    "broken": "not-a-storage-mapping",
                },
            })
            ws, filemap, catalogs, candidate = build_env(
                tmp, storage_id="broken", filemap=filemap, catalogs=catalogs)
            receipt = make_receipt(candidate, filemap)
            decision = {"kind": "project", "id": project_id}
            out = self._preflight(candidate, catalogs, filemap, ws, receipt,
                                  decision=decision)
        self.assertEqual("storage_not_writable", out["status"])

    # ------------------------------------------------------------------
    # Path safety
    # ------------------------------------------------------------------

    def test_dotdot_target_relative_path_is_unsafe_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws, filemap, catalogs, candidate, receipt = self._happy_env(tmp)
            candidate["destination"]["target_relative_path"] = "../escape.pdf"
            candidate["candidate_hash"] = compute_candidate_hash(candidate)
            receipt = make_receipt(candidate, filemap)
            out = self._preflight(candidate, catalogs, filemap, ws, receipt)
        self.assertEqual("unsafe_path", out["status"])

    def test_absolute_target_relative_path_is_unsafe_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws, filemap, catalogs, candidate, _receipt = self._happy_env(tmp)
            for value in ("/etc/passwd", "C:/Windows/system32/x.pdf", "\\\\server\\share\\x.pdf"):
                drifted = copy.deepcopy(candidate)
                drifted["destination"]["target_relative_path"] = value
                drifted["candidate_hash"] = compute_candidate_hash(drifted)
                receipt = make_receipt(drifted, filemap)
                with self.subTest(value=value):
                    out = self._preflight(drifted, catalogs, filemap, ws, receipt)
                    self.assertEqual("unsafe_path", out["status"])

    def test_windows_device_target_filename_is_unsafe_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws, filemap, catalogs, candidate, _receipt = self._happy_env(tmp)
            for value in ("CON", "CON.txt", "AUX.pdf", "NUL.dat", "COM1.doc", "LPT1.txt"):
                drifted = copy.deepcopy(candidate)
                drifted["destination"]["target_filename"] = value
                drifted["destination"]["target_relative_path"] = f"{DEFAULT_TARGET_DIR}/{value}"
                drifted["candidate_hash"] = compute_candidate_hash(drifted)
                receipt = make_receipt(drifted, filemap)
                with self.subTest(value=value):
                    out = self._preflight(drifted, catalogs, filemap, ws, receipt)
                    self.assertEqual("unsafe_path", out["status"])

    def test_target_filename_with_separator_is_unsafe_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws, filemap, catalogs, candidate, _receipt = self._happy_env(tmp)
            for value in ("sub/file.pdf", "sub\\file.pdf"):
                drifted = copy.deepcopy(candidate)
                drifted["destination"]["target_filename"] = value
                drifted["candidate_hash"] = compute_candidate_hash(drifted)
                receipt = make_receipt(drifted, filemap)
                with self.subTest(value=value):
                    out = self._preflight(drifted, catalogs, filemap, ws, receipt)
                    self.assertEqual("unsafe_path", out["status"])

    def test_absolute_scan_dir_is_unsafe_path(self) -> None:
        abs_scan = "C:/abs/scan"
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            write_quarantine(ws)
            filemap = make_filemap(scan_dir=abs_scan)
            catalogs = with_project_storage(make_catalogs(), "pilot-proj", "primary",
                                            default_storage_cfg(scan_dir=abs_scan))
            candidate = make_candidate(filemap=filemap, scan_dir=abs_scan)
            receipt = make_receipt(candidate, filemap)
            out = self._preflight(candidate, catalogs, filemap, ws, receipt)
        self.assertEqual("unsafe_path", out["status"])

    def test_symlink_or_reparse_target_is_unsafe_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws, filemap, catalogs, candidate, receipt = self._happy_env(tmp)
            with patch.object(promotion, "_has_reparse_or_symlink", return_value=True):
                out = self._preflight(candidate, catalogs, filemap, ws, receipt)
        self.assertEqual("unsafe_path", out["status"])

    def test_real_junction_at_target_parent_is_unsafe_path(self) -> None:
        """A real junction exactly on the unresolved target parent stops as unsafe_path.

        The junction points at an internal sibling that still resolves *inside*
        ``scan_dir``, so resolve()-based containment alone would accept it and hide the
        reparse point.  ``_has_reparse_or_symlink`` is deliberately not mocked.
        """
        with tempfile.TemporaryDirectory() as tmp:
            ws, filemap, catalogs, candidate = build_env(tmp)
            parent = ws / Path(DEFAULT_SCAN_DIR) / Path(DEFAULT_TARGET_DIR)
            internal = parent.parent / "RealCorrespondence"
            internal.mkdir()
            parent.rmdir()
            if not create_directory_junction(parent, internal):
                self.skipTest("NTFS directory junction creation unavailable")
            try:
                receipt = make_receipt(candidate, filemap)
                out = self._preflight(candidate, catalogs, filemap, ws, receipt)
            finally:
                try:
                    parent.rmdir()
                except OSError:
                    pass
        self.assertEqual("unsafe_path", out["status"])
        self.assertIn(out["status"], ALL_STOPCODES)

    def test_real_junction_at_scan_dir_mount_is_ready(self) -> None:
        """A real junction exactly AT scan_dir (the catalog-bound cloud mount) is ready.

        ``scan_dir`` is itself the junction to the internal mount target; the
        unresolved walk is bounded to components strictly *inside* scan_dir, so the
        legitimate mount topology is not flagged.  ``_has_reparse_or_symlink`` is
        deliberately not mocked.
        """
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            mount_target = ws / "data" / "cloud-mounts" / "pilot_mount"
            (mount_target / Path(DEFAULT_TARGET_DIR)).mkdir(parents=True)
            (ws / "data" / "cloud").mkdir(parents=True, exist_ok=True)
            if not create_directory_junction(ws / Path(DEFAULT_SCAN_DIR), mount_target):
                self.skipTest("NTFS directory junction creation unavailable")
            try:
                filemap = make_filemap()
                catalogs = make_catalogs()
                write_quarantine(ws)
                candidate = make_candidate(filemap=filemap)
                receipt = make_receipt(candidate, filemap)
                out = self._preflight(candidate, catalogs, filemap, ws, receipt)
            finally:
                try:
                    (ws / Path(DEFAULT_SCAN_DIR)).rmdir()
                except OSError:
                    pass
        self.assertEqual("ready", out["status"])

    def test_missing_target_parent_is_parent_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws, filemap, catalogs, candidate = build_env(tmp, target_dir="01_Admin")
            receipt = make_receipt(candidate, filemap)
            (ws / DEFAULT_SCAN_DIR / "01_Admin").rmdir()
            out = self._preflight(candidate, catalogs, filemap, ws, receipt)
        self.assertEqual("parent_missing", out["status"])

    # ------------------------------------------------------------------
    # Filemap validation
    # ------------------------------------------------------------------

    def test_missing_filemap_is_filemap_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws, filemap, catalogs, candidate, receipt = self._happy_env(tmp)
            out = self._preflight(candidate, catalogs, None, ws, receipt)
        self.assertEqual("filemap_drift", out["status"])

    def test_stale_filemap_is_filemap_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stale = make_filemap(updated_at="2026-09-10 09:30:00")
            ws, filemap, catalogs, candidate = build_env(tmp, filemap=stale)
            receipt = make_receipt(candidate, filemap)
            out = self._preflight(candidate, catalogs, filemap, ws, receipt)
        self.assertEqual("filemap_drift", out["status"])

    def test_filemap_storage_id_drift_is_filemap_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws, filemap, catalogs, candidate, _receipt = self._happy_env(tmp)
            filemap["storage_id"] = "other"
            receipt = make_receipt(candidate, filemap)
            out = self._preflight(candidate, catalogs, filemap, ws, receipt)
        self.assertEqual("filemap_drift", out["status"])

    def test_filemap_scope_drift_is_filemap_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws, filemap, catalogs, candidate, _receipt = self._happy_env(tmp)
            filemap["scope"] = "topic"
            receipt = make_receipt(candidate, filemap)
            out = self._preflight(candidate, catalogs, filemap, ws, receipt)
        self.assertEqual("filemap_drift", out["status"])

    def test_filemap_scan_dir_drift_is_filemap_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws, filemap, catalogs, candidate, _receipt = self._happy_env(tmp)
            filemap["scan_dir"] = "data/cloud/ELSEWHERE"
            receipt = make_receipt(candidate, filemap)
            # Candidate/catalog still bind the canonical scan_dir; only the filemap drifts.
            out = self._preflight(candidate, catalogs, filemap, ws, receipt)
        self.assertEqual("filemap_drift", out["status"])

    def test_filemap_snapshot_drift_breaks_the_review_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws, filemap, catalogs, candidate, receipt = self._happy_env(tmp)
            filemap["files"]["data/cloud/PILOT/01_Admin/Correspondence/existing_doc.pdf"][
                "sha256"] = "a" * 64
            out = self._preflight(candidate, catalogs, filemap, ws, receipt)
        self.assertEqual("approval_invalid", out["status"])

    # ------------------------------------------------------------------
    # Internal error mapping
    # ------------------------------------------------------------------

    def test_unexpected_validation_error_is_preflight_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws, filemap, catalogs, candidate, receipt = self._happy_env(tmp)
            with patch.object(promotion, "resolve_catalog_storage",
                              side_effect=RuntimeError("boom")):
                out = self._preflight(candidate, catalogs, filemap, ws, receipt)
        self.assertEqual("preflight_error", out["status"])

    def test_every_declared_stopcode_is_representable(self) -> None:
        # The bounded stopcode vocabulary is closed; the module exposes the same set.
        declared = set(getattr(promotion, "PREFLIGHT_STOPCODES", ()))
        self.assertTrue(set(ALL_STOPCODES).issubset(declared))


# ==============================================================================
# Zero-mutation proofs
# ==============================================================================

class PromotionPreflightWriteTrapTests(unittest.TestCase):
    """Every path — success and failure — must be strictly read-only."""

    def setUp(self) -> None:
        self.maxDiff = None
        self.now = NOW
        self._lock_patcher = patch.object(promotion, "verify_workspace_lock", return_value=None)
        self._lock_patcher.start()
        self._tracked_patcher = patch.object(promotion, "verify_no_tracked_quarantine",
                                             return_value=None)
        self._tracked_patcher.start()

    def tearDown(self) -> None:
        self._tracked_patcher.stop()
        self._lock_patcher.stop()

    def _preflight(self, candidate, catalogs, filemap, ws, receipt, *, now=None, decision=None):
        return preflight_attachment_promotion(
            candidate, catalogs, receipt=receipt, filemap=filemap,
            workspace_root=ws, decision=decision, current_time=now or self.now,
        )

    def _assert_no_mutation(self, tmp, out, *, expected_status, mutate=None, receipt_mutate=None,
                            catalogs=None, filemap=None):
        ws = Path(tmp)
        before_tree = tree_snapshot(ws)
        inv_path = ws / "data" / "mail-desk" / "attachments" / DEFAULT_RUN_ID / ".quarantine-inventory.json"

        if mutate is not None:
            pass  # mutation is applied by the caller before this helper is invoked
        before_filemap = json.dumps(filemap, sort_keys=True) if filemap is not None else None
        before_catalogs = json.dumps(catalogs, sort_keys=True) if catalogs is not None else None
        before_inventory = inv_path.read_bytes()

        with WriteTrap() as trap:
            result = self._preflight(candidate, catalogs, filemap, ws, receipt)
        self.assertEqual(expected_status, result["status"])
        self.assertEqual([], trap.calls, "no write seam may be invoked during MD-P1 preflight")
        self.assertEqual(before_tree, tree_snapshot(ws))
        if filemap is not None:
            self.assertEqual(before_filemap, json.dumps(filemap, sort_keys=True))
        if catalogs is not None:
            self.assertEqual(before_catalogs, json.dumps(catalogs, sort_keys=True))
        self.assertEqual(before_inventory, inv_path.read_bytes())

    def test_happy_path_performs_zero_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws, filemap, catalogs, candidate = build_env(tmp)
            receipt = make_receipt(candidate, filemap)
            before_tree = tree_snapshot(ws)
            before_filemap = json.dumps(filemap, sort_keys=True)
            before_catalogs = json.dumps(catalogs, sort_keys=True)
            inv_path = (ws / "data" / "mail-desk" / "attachments" / DEFAULT_RUN_ID
                        / ".quarantine-inventory.json")
            before_inventory = inv_path.read_bytes()
            with WriteTrap() as trap:
                out = self._preflight(candidate, catalogs, filemap, ws, receipt)
            self.assertEqual("ready", out["status"])
            self.assertEqual([], trap.calls)
            self.assertEqual(before_tree, tree_snapshot(ws))
            self.assertEqual(before_filemap, json.dumps(filemap, sort_keys=True))
            self.assertEqual(before_catalogs, json.dumps(catalogs, sort_keys=True))
            self.assertEqual(before_inventory, inv_path.read_bytes())

    def test_failure_paths_perform_zero_writes(self) -> None:
        scenarios = []

        def _sandbox():
            tmp = tempfile.mkdtemp()
            self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
            return tmp

        # candidate_drift
        tmp = _sandbox()
        ws, filemap, catalogs, candidate = build_env(tmp)
        receipt = make_receipt(candidate, filemap)
        candidate["destination"]["target_relative_path"] = "tampered.pdf"
        scenarios.append(("candidate_drift", candidate, catalogs, filemap, ws, receipt))

        # approval_invalid (review binding drift)
        tmp = _sandbox()
        ws, filemap, catalogs, candidate = build_env(tmp)
        receipt = make_receipt(candidate, filemap)
        receipt["review_hash"] = "0" * 64
        scenarios.append(("approval_invalid", candidate, catalogs, filemap, ws, receipt))

        # source_drift (missing inventory)
        tmp = _sandbox()
        ws, filemap, catalogs, candidate = build_env(tmp)
        (ws / "data" / "mail-desk" / "attachments" / DEFAULT_RUN_ID
         / ".quarantine-inventory.json").unlink()
        receipt = make_receipt(candidate, filemap)
        scenarios.append(("source_drift", candidate, catalogs, filemap, ws, receipt))

        # catalog_drift
        tmp = _sandbox()
        ws, filemap, catalogs, candidate = build_env(tmp)
        receipt = make_receipt(candidate, filemap)
        empty = {"projects": [], "topics": []}
        scenarios.append(("catalog_drift", candidate, empty, filemap, ws, receipt))

        # unsafe_path
        tmp = _sandbox()
        ws, filemap, catalogs, candidate = build_env(tmp)
        candidate["destination"]["target_relative_path"] = "../escape.pdf"
        candidate["candidate_hash"] = compute_candidate_hash(candidate)
        receipt = make_receipt(candidate, filemap)
        scenarios.append(("unsafe_path", candidate, catalogs, filemap, ws, receipt))

        # parent_missing
        tmp = _sandbox()
        ws, filemap, catalogs, candidate = build_env(tmp, target_dir="01_Admin")
        receipt = make_receipt(candidate, filemap)
        (ws / DEFAULT_SCAN_DIR / "01_Admin").rmdir()
        scenarios.append(("parent_missing", candidate, catalogs, filemap, ws, receipt))

        # filemap_drift (stale)
        tmp = _sandbox()
        stale = make_filemap(updated_at="2026-09-10 09:30:00")
        ws, filemap, catalogs, candidate = build_env(tmp, filemap=stale)
        receipt = make_receipt(candidate, filemap)
        scenarios.append(("filemap_drift", candidate, catalogs, filemap, ws, receipt))

        for status, candidate, catalogs, filemap, ws, receipt in scenarios:
            with self.subTest(status=status):
                inv_path = (ws / "data" / "mail-desk" / "attachments" / DEFAULT_RUN_ID
                            / ".quarantine-inventory.json")
                before_tree = tree_snapshot(ws)
                before_filemap = json.dumps(filemap, sort_keys=True)
                before_catalogs = json.dumps(catalogs, sort_keys=True)
                before_inventory = inv_path.read_bytes() if inv_path.exists() else None
                with WriteTrap() as trap:
                    out = self._preflight(candidate, catalogs, filemap, ws, receipt)
                self.assertEqual(status, out["status"])
                self.assertEqual([], trap.calls)
                self.assertEqual(before_tree, tree_snapshot(ws))
                self.assertEqual(before_filemap, json.dumps(filemap, sort_keys=True))
                self.assertEqual(before_catalogs, json.dumps(catalogs, sort_keys=True))
                after_inventory = inv_path.read_bytes() if inv_path.exists() else None
                self.assertEqual(before_inventory, after_inventory)

    def test_lock_failure_path_performs_zero_writes(self) -> None:
        self._lock_patcher.stop()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                ws, filemap, catalogs, candidate = build_env(tmp)
                receipt = make_receipt(candidate, filemap)
                before_tree = tree_snapshot(ws)
                with patch.object(promotion, "verify_workspace_lock",
                                  side_effect=WorkspaceLockError("no lock")):
                    with WriteTrap() as trap:
                        out = self._preflight(candidate, catalogs, filemap, ws, receipt)
                self.assertEqual("lock_unavailable", out["status"])
                self.assertEqual([], trap.calls)
                self.assertEqual(before_tree, tree_snapshot(ws))
        finally:
            self._lock_patcher.start()

    def test_preflight_output_schema_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws, filemap, catalogs, candidate = build_env(tmp)
            receipt = make_receipt(candidate, filemap)
            out = self._preflight(candidate, catalogs, filemap, ws, receipt)
        self.assertEqual(1, out["schema_version"])
        self.assertEqual("attachment_promotion_preflight", out["kind"])
        for field in ("status", "reason", "candidate_hash", "review_hash", "source_sha256",
                      "storage_id", "target_relative_path", "target_path_fingerprint",
                      "filemap_snapshot_hash", "checked_at", "preflight_hash"):
            self.assertIn(field, out)


if __name__ == "__main__":
    unittest.main()
