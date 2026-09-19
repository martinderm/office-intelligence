"""TDD tests for FR-15 / MD-E1 — T01: unconditional attachment lock ownership.

This focused suite is written before the production change (Red → Green → Refactor).
It proves that the legacy lock bypass (`WORKSPACE_LOCK_ALLOW_LEGACY` env knob and the
`allow_legacy` parameter) opens no write path across the attachment fetch / extract /
cleanup seams, while the trusted lease/conversation IDs from the harness control plane
remain honoured.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from typing import Any
from unittest.mock import patch
import uuid

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

from core import himalaya  # noqa: E402
from core import attachments  # noqa: E402
from core import attachment_fetch as afetch  # noqa: E402
from core import attachment_extract as aextract  # noqa: E402


_ACCOUNT = "BOKU-MARTIN"
_FOLDER = "INBOX"
_ENVELOPE_ID = "7301"


def _json_pdf_payload() -> bytes:
    """Return a minimal but sniffable PDF payload (used for fetch-path tests)."""
    return b"%PDF-1.4\n% MD-E1 T01 lock ownership probe\n"


def _image_only_pdf(num_pages: int = 2) -> bytes:
    """Build a deterministic image-only PDF so extraction must create an OCR derivative."""
    import pymupdf

    doc = pymupdf.open()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 50, 50), 1)
    pix.clear_with(255)
    for _ in range(num_pages):
        page = doc.new_page()
        page.insert_image(page.rect, pixmap=pix)
    data = doc.tobytes()
    doc.close()
    return data


def _mock_ocr_derivative(
    source: Path,
    deriv: Path,
    max_pages: int = 3,
    pages: Any = None,
    timeout: float = 30,
) -> tuple[bytes, str, int]:
    """Module-level picklable OCR stand-in that writes a real derivative (mutating path)."""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 50), "MDE1 OCR Derivative")
    data = doc.tobytes()
    doc.close()
    deriv.parent.mkdir(parents=True, exist_ok=True)
    deriv.write_bytes(data)
    return data, "MDE1 OCR Derivative", 1


def _build_fetch_args(tmp_dir: str, run_id: str) -> dict[str, Any]:
    """Build a complete, valid `op_attachment_fetch` invocation bound to one PDF part."""
    data_dir = Path(tmp_dir) / "data" / "mail-desk"
    pdf = _json_pdf_payload()
    sha = hashlib.sha256(pdf).hexdigest()
    message_id = f"mde1-t01-{uuid.uuid4().hex[:8]}@example.org"
    candidate = {
        "filename": "probe.pdf",
        "mime_type": "application/pdf",
        "size_bytes": len(pdf),
        "sha256": sha,
        "part_locator": "2",
        "fetch_status": "available",
        "provenance": attachments.PROVENANCE_RFC822,
        "account": _ACCOUNT,
        "folder": _FOLDER,
        "envelope_id": _ENVELOPE_ID,
        "message_id": message_id,
    }
    review_hash = afetch.compute_review_hash(
        account=_ACCOUNT,
        message_id=message_id,
        folder=_FOLDER,
        envelope_id=_ENVELOPE_ID,
        part_locator="2",
        inventory_sha256=sha,
    )
    receipt = {
        "receipt_id": "rec-mde1-t01",
        "request_hash": review_hash,
        "approved_at": "2026-09-19T09:00:00Z",
        "approved_by": "martin",
    }
    raw_eml = attachments.build_test_eml(
        subject="MDE1 T01",
        message_id=f"<{message_id}>",
        attachments=[{"filename": "probe.pdf", "mime_type": "application/pdf", "data": pdf}],
    )
    return {
        "candidate": candidate,
        "account": _ACCOUNT,
        "folder": _FOLDER,
        "envelope_id": _ENVELOPE_ID,
        "message_id": message_id,
        "part_locator": "2",
        "inventory_sha256": sha,
        "review_hash": review_hash,
        "approval_receipt": receipt,
        "run_id": run_id,
        "raw_eml": raw_eml,
        "data_dir": data_dir,
        "workspace_root": tmp_dir,
    }


class MailDeskAttachmentEvaluationMDE1LockTests(unittest.TestCase):
    """T01: no env/parameter/manifest value may re-open the legacy lock bypass."""

    def setUp(self) -> None:
        self.maxDiff = None
        self._himalaya_blocker = patch.object(
            himalaya,
            "run_himalaya",
            side_effect=RuntimeError("Real Himalaya process execution is forbidden in hermetic unit tests!"),
        )
        self._himalaya_blocker.start()
        # Neutralise any inherited lock identity so "no owned lease" is genuinely proven.
        self._env_backup = {
            key: os.environ.pop(key, None)
            for key in (
                "WORKSPACE_LOCK_ALLOW_LEGACY",
                "WORKSPACE_LOCK_LEASE_ID",
                "WORKSPACE_LOCK_CONVERSATION_ID",
                "WORKSPACE_ROOT",
            )
        }

    def tearDown(self) -> None:
        for key, value in self._env_backup.items():
            if value is not None:
                os.environ[key] = value
        self._himalaya_blocker.stop()

    # ------------------------------------------------------------------
    # Red: env legacy bypass must not open the fetch write path
    # ------------------------------------------------------------------
    def test_fetch_env_legacy_bypass_ignored_fails_closed_before_io(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_id = "run_mde1_env_legacy"
            args = _build_fetch_args(tmp_dir, run_id)
            attachments_root = Path(args["data_dir"]) / "attachments"

            with patch.dict(os.environ, {"WORKSPACE_LOCK_ALLOW_LEGACY": "1"}):
                with self.assertRaises(afetch.WorkspaceLockError):
                    afetch.op_attachment_fetch(**args)

            self.assertFalse(
                attachments_root.exists(),
                "No attachments folder may be created while only the legacy env knob is set!",
            )
            self.assertFalse((attachments_root / run_id).exists())

    # ------------------------------------------------------------------
    # Red: env legacy bypass must not open the mutating OCR derivative path
    # ------------------------------------------------------------------
    def test_extract_env_legacy_bypass_ignored_fails_closed_before_derivative_write(self) -> None:
        pdf_bytes = _image_only_pdf(num_pages=2)
        pdf_sha = hashlib.sha256(pdf_bytes).hexdigest()

        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data" / "mail-desk"
            run_dir = data_dir / "attachments" / "run_mde1_extract"
            run_dir.mkdir(parents=True)
            pdf_path = run_dir / "scanned.pdf"
            pdf_path.write_bytes(pdf_bytes)

            fetch_result = {
                "status": "fetched",
                "run_id": "run_mde1_extract",
                "relative_path": "data/mail-desk/attachments/run_mde1_extract/scanned.pdf",
                "filename": "scanned.pdf",
                "fetch_sha256": pdf_sha,
                "inventory_sha256": pdf_sha,
                "effective_mime_type": "application/pdf",
            }

            with patch.dict(os.environ, {"WORKSPACE_LOCK_ALLOW_LEGACY": "1"}):
                with self.assertRaises(afetch.WorkspaceLockError):
                    aextract.extract_attachment_content(
                        fetch_result,
                        expected_sha256=pdf_sha,
                        data_dir=data_dir,
                        workspace_root=tmp_dir,
                        _ocr_runner=_mock_ocr_derivative,
                    )

            self.assertFalse(
                (run_dir / "derivatives").exists(),
                "No OCR derivative directory may be created on legacy-only authorization!",
            )
            self.assertEqual(pdf_bytes, pdf_path.read_bytes())

    # ------------------------------------------------------------------
    # Red: env legacy bypass must not open the cleanup delete path
    # ------------------------------------------------------------------
    def test_cleanup_env_legacy_bypass_ignored_fails_closed_before_delete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data" / "mail-desk"
            run_dir = data_dir / "attachments" / "run_mde1_cleanup"
            run_dir.mkdir(parents=True)
            (run_dir / "scanned.pdf").write_bytes(_json_pdf_payload())

            with patch.dict(os.environ, {"WORKSPACE_LOCK_ALLOW_LEGACY": "1"}):
                with self.assertRaises(afetch.WorkspaceLockError):
                    afetch.cleanup_run_quarantine(
                        "run_mde1_cleanup",
                        data_dir=data_dir,
                        workspace_root=tmp_dir,
                    )

            self.assertTrue(
                run_dir.exists(),
                "Cleanup must not delete a quarantine run on legacy-only authorization!",
            )

    # ------------------------------------------------------------------
    # Green guard: trusted explicit lease/conversation remains usable and
    # receives allow_legacy=False into the shared guard
    # ------------------------------------------------------------------
    def test_guard_receives_trusted_lease_and_never_legacy(self) -> None:
        guard = afetch._load_workspace_lock_guard()
        captured: dict[str, Any] = {}

        def _recorder(workspace, *, lease_id=None, conversation_id=None, allow_legacy=False, runner=None):
            captured["lease_id"] = lease_id
            captured["conversation_id"] = conversation_id
            captured["allow_legacy"] = allow_legacy
            return guard.LockReceipt(
                workspace=Path(str(workspace)),
                state="Active",
                lease_id=lease_id,
                conversation_id=conversation_id,
            )

        with tempfile.TemporaryDirectory() as tmp_dir:
            run_id = "run_mde1_owned"
            args = _build_fetch_args(tmp_dir, run_id)
            args["lease_id"] = "lease-mde1-t01"
            args["conversation_id"] = "conv-mde1-t01"

            with patch.dict(os.environ, {"WORKSPACE_LOCK_ALLOW_LEGACY": "1"}):
                with patch.object(guard, "require_workspace_lock", side_effect=_recorder):
                    res = afetch.op_attachment_fetch(**args)

            self.assertEqual("fetched", res["status"])
            self.assertEqual("lease-mde1-t01", captured["lease_id"])
            self.assertEqual("conv-mde1-t01", captured["conversation_id"])
            self.assertIs(
                False,
                captured["allow_legacy"],
                "The shared guard must be called with a hard allow_legacy=False.",
            )
            self.assertTrue((Path(args["data_dir"]) / "attachments" / run_id).is_dir())

    # ------------------------------------------------------------------
    # Green guard: env-provided trusted lease/conversation stays honoured
    # while WORKSPACE_LOCK_ALLOW_LEGACY=1 is still ignored
    # ------------------------------------------------------------------
    def test_env_trusted_lease_and_conversation_are_forwarded_without_legacy(self) -> None:
        guard = afetch._load_workspace_lock_guard()
        captured: dict[str, Any] = {}

        def _recorder(workspace, *, lease_id=None, conversation_id=None, allow_legacy=False, runner=None):
            captured["lease_id"] = lease_id
            captured["conversation_id"] = conversation_id
            captured["allow_legacy"] = allow_legacy
            return guard.LockReceipt(
                workspace=Path(str(workspace)),
                state="Active",
                lease_id=lease_id,
                conversation_id=conversation_id,
            )

        with tempfile.TemporaryDirectory() as tmp_dir:
            run_id = "run_mde1_env_owned"
            args = _build_fetch_args(tmp_dir, run_id)
            env = {
                "WORKSPACE_LOCK_ALLOW_LEGACY": "1",
                "WORKSPACE_LOCK_LEASE_ID": "lease-env-mde1",
                "WORKSPACE_LOCK_CONVERSATION_ID": "conv-env-mde1",
            }

            with patch.dict(os.environ, env):
                with patch.object(guard, "require_workspace_lock", side_effect=_recorder):
                    res = afetch.op_attachment_fetch(**args)

            self.assertEqual("fetched", res["status"])
            self.assertEqual("lease-env-mde1", captured["lease_id"])
            self.assertEqual("conv-env-mde1", captured["conversation_id"])
            self.assertIs(False, captured["allow_legacy"])

    # ------------------------------------------------------------------
    # Green integration: a real, owned lock lease in the workspace works
    # ------------------------------------------------------------------
    def test_real_owned_lease_enables_fetch(self) -> None:
        guard = afetch._load_workspace_lock_guard()

        with tempfile.TemporaryDirectory() as tmp_dir:
            guard.acquire_workspace_lock(
                tmp_dir,
                harness="mde1-t01-test",
                lease_id="lease-real-mde1",
                conversation_id="conv-real-mde1",
            )
            run_id = "run_mde1_real_owned"
            args = _build_fetch_args(tmp_dir, run_id)
            args["lease_id"] = "lease-real-mde1"
            args["conversation_id"] = "conv-real-mde1"

            with patch.dict(os.environ, {"WORKSPACE_LOCK_ALLOW_LEGACY": "1"}):
                res = afetch.op_attachment_fetch(**args)

            self.assertEqual("fetched", res["status"])
            self.assertTrue((Path(args["data_dir"]) / "attachments" / run_id / "probe.pdf").is_file())


if __name__ == "__main__":
    unittest.main()
