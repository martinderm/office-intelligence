"""TDD tests for FR-15 / MD-E1 — T01 lock ownership, T02 preflight and T03 receipt classes.

This focused suite is written before the production change (Red → Green → Refactor).

T01 proves that the legacy lock bypass (`WORKSPACE_LOCK_ALLOW_LEGACY` env knob and the
`allow_legacy` parameter) opens no write path across the attachment fetch / extract /
cleanup seams, while the trusted lease/conversation IDs from the harness control plane
remain honoured.

T02 proves that the bounded, fail-closed tracked-quarantine preflight stops
`op_attachment_fetch` before the first quarantine write and stops the mutating OCR
derivative write in `extract_attachment_content`, without ever touching `.gitignore`,
and that a clean Git index lets the write proceed. The Git invocation is injected
hermetically through a fake command runner, so no live Git checkout is required.

T03 proves the narrow context-bound receipt-class seam: an internal machine-authorization
factory mints a process-internal, non-serializable capability (`receipt_class: "machine"`,
`receipt_type: "attachment_auto_evaluation"`, trusted issuer/policy/request bindings) that
only the evaluation context accepts. A structurally identical caller-supplied dictionary is
insufficient, and every Human-Approval callsite (filing, promotion, export, disposition,
apply-discard and the manifest-driven direct fetch) rejects the machine class fail-closed
while the existing typeless human MD-A2 receipts keep working unchanged.
"""

from __future__ import annotations

import ast
import copy
from collections.abc import Mapping
import gc
import hashlib
import json
import os
from pathlib import Path
import pickle
import subprocess
import sys
import tempfile
import unittest
from typing import Any
from unittest.mock import patch
import uuid
import weakref

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

from core import himalaya  # noqa: E402
from core import attachments  # noqa: E402
from core import attachment_fetch as afetch  # noqa: E402
from core import attachment_extract as aextract  # noqa: E402
from core import attachment_authorization as authz  # noqa: E402
from core import attachment_handoff as ahandoff  # noqa: E402
from core import attachment_disposition_log as adisp  # noqa: E402
from core import attachment_filing as afiling  # noqa: E402
from core import quarantine_preflight as qpf  # noqa: E402
from core import attachment_evaluation as aevaluate  # noqa: E402
from core.attachment_policy import DEFAULT_ATTACHMENT_POLICY  # noqa: E402
import mail_desk_himalaya_client as mclient  # noqa: E402


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


def _noop_lock_verifier(*args: Any, **kwargs: Any) -> None:
    """Module-level picklable lock-verifier stub representing an owned harness lease.

    T02's preflight is injected separately; this stub only isolates the workspace lock
    so the derivative-write assertions exercise the preflight guard in isolation.
    """
    return None


# Module-level picklable Git-preflight runners (safe to cross the OCR process boundary).
_PREFLIGHT_CALLS: list[tuple[list[str], str, float]] = []


def _clean_git_runner(argv: Any, cwd: str, timeout_seconds: float) -> Any:
    """Fake `git ls-files` runner reporting a clean index."""
    return qpf.GitIndexQueryResult(returncode=0, stdout="", stderr="")


def _recording_clean_git_runner(argv: Any, cwd: str, timeout_seconds: float) -> Any:
    """Clean runner that records the bounded invocation for assertions (in-process only)."""
    _PREFLIGHT_CALLS.append((list(argv), str(cwd), float(timeout_seconds)))
    return qpf.GitIndexQueryResult(returncode=0, stdout="", stderr="")


def _tracked_git_runner(argv: Any, cwd: str, timeout_seconds: float) -> Any:
    """Fake runner that reports a tracked quarantine artefact."""
    return qpf.GitIndexQueryResult(
        returncode=0,
        stdout="data/mail-desk/attachments/run_tracked/leak.pdf\0",
        stderr="",
    )


def _failing_git_runner(argv: Any, cwd: str, timeout_seconds: float) -> Any:
    """Fake runner emulating a non-zero `git ls-files` exit (e.g. not a repository)."""
    return qpf.GitIndexQueryResult(
        returncode=128,
        stdout="",
        stderr="fatal: not a git repository",
    )


def _timeout_git_runner(argv: Any, cwd: str, timeout_seconds: float) -> Any:
    """Fake runner emulating a bounded Git timeout."""
    raise subprocess.TimeoutExpired(cmd=list(argv), timeout=timeout_seconds)


def _unreadable_git_runner(argv: Any, cwd: str, timeout_seconds: float) -> Any:
    """Fake runner returning a result object with unreadable (non-string) output."""
    return qpf.GitIndexQueryResult(returncode=0, stdout=None, stderr="")  # type: ignore[arg-type]


def _runner_with_output(payload: str) -> Any:
    """Build an in-process runner returning the given NUL-terminated payload."""
    def _runner(argv: Any, cwd: str, timeout_seconds: float) -> Any:
        return qpf.GitIndexQueryResult(returncode=0, stdout=payload, stderr="")

    return _runner


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
        # T02: isolate the tracked-quarantine preflight so the T01 lock tests stay
        # hermetic (the production default runner would query this host's repository).
        self._preflight_patcher = patch.object(
            afetch, "verify_no_tracked_quarantine", return_value=None, create=True
        )
        self._preflight_patcher.start()

    def tearDown(self) -> None:
        self._preflight_patcher.stop()
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


_GITIGNORE_BYTES = (
    b"data/*\n"
    b"!data/mail-desk/\n"
    b"!data/mail-desk/**\n"
    b"/data/mail-desk/attachments/\n"
)


class MailDeskAttachmentEvaluationMDE1PreflightTests(unittest.TestCase):
    """T02: bounded, fail-closed tracked-quarantine preflight before the first write."""

    def setUp(self) -> None:
        self.maxDiff = None
        self._himalaya_blocker = patch.object(
            himalaya,
            "run_himalaya",
            side_effect=RuntimeError("Real Himalaya process execution is forbidden in hermetic unit tests!"),
        )
        self._himalaya_blocker.start()
        self._env_backup = {
            key: os.environ.pop(key, None)
            for key in (
                "WORKSPACE_ROOT",
                "WORKSPACE_LOCK_ALLOW_LEGACY",
                "WORKSPACE_LOCK_LEASE_ID",
                "WORKSPACE_LOCK_CONVERSATION_ID",
            )
        }
        _PREFLIGHT_CALLS.clear()

    def tearDown(self) -> None:
        for key, value in self._env_backup.items():
            if value is not None:
                os.environ[key] = value
        self._himalaya_blocker.stop()

    @staticmethod
    def _acquire(workspace_root: str) -> None:
        guard = afetch._load_workspace_lock_guard()
        guard.acquire_workspace_lock(
            workspace_root,
            harness="mde1-t02-test",
            lease_id="lease-t02",
            conversation_id="conv-t02",
        )

    # ------------------------------------------------------------------
    # Unit: detection semantics reuse the MD-Q1 helper contract
    # ------------------------------------------------------------------
    def test_preflight_detects_attachment_inventory_and_lock_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            for artifact in (
                "data/mail-desk/attachments/run_x/a.pdf\0",
                "data/mail-desk/attachments/run_x/.quarantine-inventory.json\0",
                "data/mail-desk/attachments/run_x/.quarantine-inventory.lock\0",
                "elsewhere/.quarantine-inventory.json\0",
                "elsewhere/.quarantine-inventory.lock\0",
            ):
                with self.assertRaises(qpf.TrackedQuarantineError) as ctx:
                    qpf.assert_no_tracked_quarantine_files(tmp_dir, runner=_runner_with_output(artifact))
                self.assertIn("STOP CONDITION", str(ctx.exception))

            # Unrelated tracked paths are not a quarantine stop condition.
            qpf.assert_no_tracked_quarantine_files(
                tmp_dir, runner=_runner_with_output("other/tracked.txt\0")
            )

    def test_preflight_is_bounded_and_invokes_git_without_shell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            qpf.assert_no_tracked_quarantine_files(tmp_dir, runner=_recording_clean_git_runner)

        self.assertEqual(1, len(_PREFLIGHT_CALLS))
        argv, cwd, timeout_seconds = _PREFLIGHT_CALLS[0]
        self.assertIsInstance(argv, list)
        self.assertEqual("git", argv[0])
        self.assertIn("ls-files", argv)
        self.assertNotIn("-c", argv)
        self.assertEqual(str(Path(tmp_dir)), cwd)
        self.assertGreater(timeout_seconds, 0)

    def test_preflight_git_failure_timeout_and_unreadable_output_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            for runner in (
                _failing_git_runner,
                _timeout_git_runner,
                _unreadable_git_runner,
            ):
                with self.assertRaises(qpf.QuarantinePreflightError):
                    qpf.assert_no_tracked_quarantine_files(tmp_dir, runner=runner)

    def test_preflight_detects_tracked_inventory_lock_end_to_end_with_real_git(self) -> None:
        """A tracked `**/.quarantine-inventory.lock` must be detected by the real runner.

        Exercises the production default runner through the public
        `assert_no_tracked_quarantine_files()` so the fixed lock pathspec is proven
        end-to-end (the injected-output unit test bypasses the pathspec entirely).
        All Git state is confined to a throwaway temporary repo; no Git command is
        run against the target checkout, and the preflight is proven read-only.
        """
        target_root = Path(__file__).resolve().parents[3]
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            # Isolation: the temporary repo is never part of the target checkout.
            self.assertFalse(
                str(repo.resolve()).lower().startswith(str(target_root.resolve()).lower())
            )
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
            subprocess.run(
                ["git", "config", "user.name", "MDE1 T02"], cwd=repo, check=True, capture_output=True
            )
            subprocess.run(
                ["git", "config", "user.email", "mde1-t02@example.org"],
                cwd=repo,
                check=True,
                capture_output=True,
            )

            elsewhere = repo / "elsewhere"
            elsewhere.mkdir(parents=True)
            (elsewhere / ".quarantine-inventory.lock").write_text("lock", encoding="utf-8")
            (elsewhere / "notes.txt").write_text("tracked non-quarantine", encoding="utf-8")
            subprocess.run(
                ["git", "add", "-f", "elsewhere/.quarantine-inventory.lock", "elsewhere/notes.txt"],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            tracked_before = subprocess.run(
                ["git", "ls-files"], cwd=repo, capture_output=True, text=True, check=True
            ).stdout

            # Real default runner (no injection): the fixed pathspec must surface the lock.
            with self.assertRaises(qpf.TrackedQuarantineError) as ctx:
                qpf.assert_no_tracked_quarantine_files(repo)

            message = str(ctx.exception)
            self.assertIn(".quarantine-inventory.lock", message)
            self.assertNotIn("notes.txt", message)

            # Read-only preflight: index/working tree untouched, no .gitignore fabricated.
            tracked_after = subprocess.run(
                ["git", "ls-files"], cwd=repo, capture_output=True, text=True, check=True
            ).stdout
            self.assertEqual(tracked_before, tracked_after)
            porcelain = subprocess.run(
                ["git", "status", "--porcelain", "-uall"],
                cwd=repo,
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            self.assertNotIn("??", porcelain)
            self.assertFalse((repo / ".gitignore").exists())

        # No repo artifacts leaked into the target checkout.
        self.assertFalse((target_root / "elsewhere" / ".quarantine-inventory.lock").exists())

    def test_preflight_detects_mixed_case_quarantine_paths_with_real_git(self) -> None:
        """Case-variant quarantine paths must be detected by the production default runner.

        On a case-insensitive worktree (Windows) a tracked ``Data/Mail-Desk/Attachments/Leak.PDF``
        names the same quarantine namespace as its lower-case form, so the bounded pathspec
        query must match case-insensitively and the returned path must still classify as a
        quarantine artefact.  All Git state lives in a throwaway temporary repo; the real
        default runner is exercised without injection.
        """
        target_root = Path(__file__).resolve().parents[3]
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            # Isolation: the temporary repo is never part of the target checkout.
            self.assertFalse(
                str(repo.resolve()).lower().startswith(str(target_root.resolve()).lower())
            )
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
            subprocess.run(
                ["git", "config", "user.name", "MDE1 T02"], cwd=repo, check=True, capture_output=True
            )
            subprocess.run(
                ["git", "config", "user.email", "mde1-t02@example.org"],
                cwd=repo,
                check=True,
                capture_output=True,
            )

            mixed_attachment = "Data/Mail-Desk/Attachments/run_mixed/Leak.PDF"
            mixed_inventory_json = "Shared/.Quarantine-Inventory.JSON"
            mixed_inventory_lock = "Shared/.Quarantine-Inventory.LOCK"
            unrelated_tracked = "Notes.TXT"
            for rel in (
                mixed_attachment,
                mixed_inventory_json,
                mixed_inventory_lock,
                unrelated_tracked,
            ):
                artifact = repo / rel
                artifact.parent.mkdir(parents=True, exist_ok=True)
                artifact.write_bytes(b"tracked")
            subprocess.run(
                [
                    "git",
                    "add",
                    "-f",
                    mixed_attachment,
                    mixed_inventory_json,
                    mixed_inventory_lock,
                    unrelated_tracked,
                ],
                cwd=repo,
                check=True,
                capture_output=True,
            )

            tracked = qpf.find_tracked_quarantine_files(repo)
            self.assertCountEqual(
                [mixed_attachment, mixed_inventory_json, mixed_inventory_lock],
                tracked,
                "Mixed-case quarantine paths must be surfaced by the case-insensitive pathspec query",
            )

            with self.assertRaises(qpf.TrackedQuarantineError) as ctx:
                qpf.assert_no_tracked_quarantine_files(repo)

            message = str(ctx.exception)
            self.assertIn(mixed_attachment, message)
            self.assertIn(mixed_inventory_json, message)
            self.assertIn(mixed_inventory_lock, message)
            self.assertNotIn(unrelated_tracked, message)

    # ------------------------------------------------------------------
    # Integration: fetch leg stops before the first quarantine write
    # ------------------------------------------------------------------
    def test_fetch_tracked_quarantine_stops_before_first_write_and_keeps_gitignore(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            gitignore = Path(tmp_dir) / ".gitignore"
            gitignore.write_bytes(_GITIGNORE_BYTES)

            run_id = "run_mde1_t02_tracked"
            args = _build_fetch_args(tmp_dir, run_id)
            args["lease_id"] = "lease-t02"
            args["conversation_id"] = "conv-t02"
            args["_preflight_runner"] = _tracked_git_runner
            self._acquire(tmp_dir)

            with self.assertRaises(qpf.TrackedQuarantineError):
                afetch.op_attachment_fetch(**args)

            self.assertFalse(
                (Path(args["data_dir"]) / "attachments").exists(),
                "Tracked quarantine must stop the run before the first quarantine write!",
            )
            self.assertFalse((Path(args["data_dir"]) / "attachments" / run_id).exists())
            self.assertEqual(_GITIGNORE_BYTES, gitignore.read_bytes())

    def test_fetch_git_failure_fails_closed_before_first_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_id = "run_mde1_t02_gitfail"
            args = _build_fetch_args(tmp_dir, run_id)
            args["lease_id"] = "lease-t02"
            args["conversation_id"] = "conv-t02"
            args["_preflight_runner"] = _failing_git_runner
            self._acquire(tmp_dir)

            with self.assertRaises(qpf.QuarantinePreflightError):
                afetch.op_attachment_fetch(**args)

            self.assertFalse((Path(args["data_dir"]) / "attachments").exists())

    def test_fetch_clean_preflight_proceeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_id = "run_mde1_t02_clean"
            args = _build_fetch_args(tmp_dir, run_id)
            args["lease_id"] = "lease-t02"
            args["conversation_id"] = "conv-t02"
            args["_preflight_runner"] = _clean_git_runner
            self._acquire(tmp_dir)

            res = afetch.op_attachment_fetch(**args)

            self.assertEqual("fetched", res["status"])
            self.assertTrue((Path(args["data_dir"]) / "attachments" / run_id / "probe.pdf").is_file())

    # ------------------------------------------------------------------
    # Integration: extraction leg stops before the mutating derivative write
    # ------------------------------------------------------------------
    def _image_only_extract_args(self, tmp_dir: str) -> dict[str, Any]:
        pdf_bytes = _image_only_pdf(num_pages=2)
        pdf_sha = hashlib.sha256(pdf_bytes).hexdigest()
        data_dir = Path(tmp_dir) / "data" / "mail-desk"
        run_dir = data_dir / "attachments" / "run_mde1_t02_extract"
        run_dir.mkdir(parents=True)
        (run_dir / "scanned.pdf").write_bytes(pdf_bytes)
        fetch_result = {
            "status": "fetched",
            "run_id": "run_mde1_t02_extract",
            "relative_path": "data/mail-desk/attachments/run_mde1_t02_extract/scanned.pdf",
            "filename": "scanned.pdf",
            "fetch_sha256": pdf_sha,
            "inventory_sha256": pdf_sha,
            "effective_mime_type": "application/pdf",
        }
        return {
            "fetch_result": fetch_result,
            "data_dir": data_dir,
            "pdf_sha": pdf_sha,
            "run_dir": run_dir,
        }

    def test_extract_tracked_quarantine_stops_before_derivative_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            gitignore = Path(tmp_dir) / ".gitignore"
            gitignore.write_bytes(_GITIGNORE_BYTES)
            ctx = self._image_only_extract_args(tmp_dir)

            with self.assertRaises(qpf.TrackedQuarantineError):
                aextract.extract_attachment_content(
                    ctx["fetch_result"],
                    expected_sha256=ctx["pdf_sha"],
                    data_dir=ctx["data_dir"],
                    workspace_root=tmp_dir,
                    _lock_verifier=_noop_lock_verifier,
                    _ocr_runner=_mock_ocr_derivative,
                    _preflight_runner=_tracked_git_runner,
                )

            self.assertFalse(
                (ctx["run_dir"] / "derivatives").exists(),
                "Tracked quarantine must stop the extraction before the derivative write!",
            )
            self.assertEqual(_GITIGNORE_BYTES, gitignore.read_bytes())

    def test_extract_clean_preflight_proceeds_to_derivative_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            ctx = self._image_only_extract_args(tmp_dir)

            res = aextract.extract_attachment_content(
                ctx["fetch_result"],
                expected_sha256=ctx["pdf_sha"],
                data_dir=ctx["data_dir"],
                workspace_root=tmp_dir,
                _lock_verifier=_noop_lock_verifier,
                _ocr_runner=_mock_ocr_derivative,
                _preflight_runner=_clean_git_runner,
            )

            self.assertEqual("extracted", res["status"])
            self.assertTrue((ctx["run_dir"] / "derivatives" / "scanned.ocr.pdf").is_file())


def _machine_factory_kwargs() -> dict[str, Any]:
    """Build canonical trusted bindings for the internal machine-authorization factory."""
    inventory_sha256 = hashlib.sha256(b"mde1-t03-inventory").hexdigest()
    message_id = f"mde1-t03-{uuid.uuid4().hex[:8]}@example.org"
    request_hash = afetch.compute_review_hash(
        account=_ACCOUNT,
        message_id=message_id,
        folder=_FOLDER,
        envelope_id=_ENVELOPE_ID,
        part_locator="2",
        inventory_sha256=inventory_sha256,
    )
    return {
        "request_hash": request_hash,
        "policy_revision": "policy-rev-1",
        "account": _ACCOUNT,
        "message_id": message_id,
        "folder": _FOLDER,
        "envelope_id": _ENVELOPE_ID,
        "part_locator": "2",
        "inventory_sha256": inventory_sha256,
    }


def _typeless_human_receipt(request_hash: str | None = None, *, approved_by: str = "martin") -> dict[str, Any]:
    """A legacy FR-08 human MD-A2 receipt: persists unchanged and carries no receipt_type."""
    return {
        "receipt_id": "rec-human-mde1-t03",
        "request_hash": request_hash or ("a" * 64),
        "approved_at": "2026-09-19T09:00:00Z",
        "approved_by": approved_by,
    }


def _build_mda2_composite(receipt: Any = None) -> dict[str, Any]:
    """Build a valid MD-A2 composite contract (operation/result/candidate) bound to `_ACCOUNT`.

    When no receipt is supplied a correctly bound typeless human receipt is used, so the
    composite exercises the unchanged FR-08 human path.
    """
    sha256 = "a" * 64
    filename = "minutes_2026.pdf"
    run_id = "run_mde1_t03_filing"
    candidate = {
        "account": _ACCOUNT,
        "message_id": f"<mde1-t03-filing-{uuid.uuid4().hex[:8]}@example.org>",
        "folder": _FOLDER,
        "envelope_id": _ENVELOPE_ID,
        "part_locator": "2",
        "filename": filename,
        "sha256": sha256,
        "size_bytes": 1024,
        "mime_type": "application/pdf",
        "fetch_status": "available",
        "provenance": attachments.PROVENANCE_RFC822,
    }
    review_hash = afetch.compute_review_hash(
        account=_ACCOUNT,
        message_id=candidate["message_id"],
        folder=_FOLDER,
        envelope_id=_ENVELOPE_ID,
        part_locator="2",
        inventory_sha256=sha256,
    )
    if receipt is None:
        receipt = _typeless_human_receipt(review_hash)
    operation = {
        "action": "attachment_fetch",
        "account": _ACCOUNT,
        "message_id": candidate["message_id"],
        "folder": _FOLDER,
        "envelope_id": _ENVELOPE_ID,
        "part_locator": "2",
        "inventory_sha256": sha256,
        "review_hash": review_hash,
        "approval_receipt": receipt,
        "run_id": run_id,
        "candidate": candidate,
    }
    result = {
        "status": "fetched",
        "run_id": run_id,
        "filename": filename,
        "inventory_sha256": sha256,
        "fetch_sha256": sha256,
        "effective_mime_type": "application/pdf",
        "size_bytes": 1024,
        "relative_path": f"data/mail-desk/attachments/{run_id}/{filename}",
        "error": None,
    }
    return {"operation": operation, "result": result, "candidate": candidate}


class MailDeskAttachmentEvaluationMDE1MachineAuthorizationTests(unittest.TestCase):
    """T03: internal machine-authorization factory and the evaluation-context guard."""

    def setUp(self) -> None:
        self.maxDiff = None

    def _mint(self, **overrides: Any) -> Any:
        kwargs = _machine_factory_kwargs()
        kwargs.update(overrides)
        return authz.create_machine_authorization(**kwargs)

    @staticmethod
    def _expected(kwargs: dict[str, Any]) -> dict[str, Any]:
        return {
            "expected_request_hash": kwargs["request_hash"],
            "expected_policy_revision": kwargs["policy_revision"],
        }

    # ------------------------------------------------------------------
    # Factory: trusted, unique, canonical bindings as an opaque mapping
    # ------------------------------------------------------------------
    def test_factory_mints_mapping_with_trusted_bindings(self) -> None:
        kwargs = _machine_factory_kwargs()
        auth = authz.create_machine_authorization(**kwargs)

        # Mapping behaviour/shape only; the implementation class is intentionally private.
        self.assertIsInstance(auth, Mapping)
        self.assertNotIsInstance(auth, dict)
        self.assertIn("receipt_class", auth)
        self.assertEqual(auth.get("receipt_class"), authz.RECEIPT_CLASS_MACHINE)
        self.assertEqual(auth["receipt_type"], authz.RECEIPT_TYPE_ATTACHMENT_AUTO_EVALUATION)
        self.assertEqual(auth["approved_by"], authz.MACHINE_APPROVER_ID)
        self.assertEqual(len(dict(auth)), len(list(auth.keys())))
        self.assertEqual(
            {
                "receipt_class": authz.RECEIPT_CLASS_MACHINE,
                "receipt_type": authz.RECEIPT_TYPE_ATTACHMENT_AUTO_EVALUATION,
                "approved_by": authz.MACHINE_APPROVER_ID,
                "request_hash": kwargs["request_hash"],
                "policy_revision": kwargs["policy_revision"],
                "account": kwargs["account"],
                "message_id": kwargs["message_id"],
                "folder": kwargs["folder"],
                "envelope_id": kwargs["envelope_id"],
                "part_locator": kwargs["part_locator"],
                "inventory_sha256": kwargs["inventory_sha256"],
            },
            {
                key: value
                for key, value in dict(auth).items()
                if key not in ("receipt_id", "approved_at")
            },
        )
        self.assertTrue(str(auth["receipt_id"]).strip())
        self.assertTrue(str(auth["approved_at"]).strip())

    def test_capability_is_not_a_plain_dict_and_not_directly_serializable(self) -> None:
        auth = self._mint()
        # The capability is an opaque mapping, so it cannot leak via dict-oriented serializers.
        with self.assertRaises(TypeError):
            json.dumps(auth)
        self.assertNotIsInstance(auth, dict)

    def test_factory_receipt_ids_are_unique_and_nonempty(self) -> None:
        first = self._mint()
        second = self._mint()
        self.assertTrue(str(first["receipt_id"]).strip())
        self.assertTrue(str(second["receipt_id"]).strip())
        self.assertNotEqual(first["receipt_id"], second["receipt_id"])

    def test_factory_rejects_request_hash_that_is_not_canonical_review_hash(self) -> None:
        with self.assertRaises(authz.AttachmentAuthorizationError):
            self._mint(request_hash="b" * 64)

    def test_factory_rejects_missing_binding(self) -> None:
        kwargs = _machine_factory_kwargs()
        kwargs["part_locator"] = ""
        with self.assertRaises(authz.AttachmentAuthorizationError):
            authz.create_machine_authorization(**kwargs)

    def test_factory_rejects_none_for_every_required_binding(self) -> None:
        for field in (
            "request_hash",
            "policy_revision",
            "account",
            "message_id",
            "folder",
            "envelope_id",
            "part_locator",
            "inventory_sha256",
        ):
            with self.subTest(field=field):
                kwargs = _machine_factory_kwargs()
                kwargs[field] = None
                with self.assertRaises(authz.AttachmentAuthorizationError):
                    authz.create_machine_authorization(**kwargs)

    # ------------------------------------------------------------------
    # Evaluation context: accepts only the exact internally issued object
    # ------------------------------------------------------------------
    def test_evaluation_context_accepts_internal_machine_authorization(self) -> None:
        kwargs = _machine_factory_kwargs()
        auth = authz.create_machine_authorization(**kwargs)

        info = authz.guard_context_authorization(
            auth, context=authz.CONTEXT_EVALUATION, **self._expected(kwargs)
        )

        self.assertEqual("machine", info["class"])
        self.assertEqual("auto_evaluated", info["authorization"])

    def test_evaluation_context_requires_explicit_nonempty_expected_bindings(self) -> None:
        kwargs = _machine_factory_kwargs()
        auth = authz.create_machine_authorization(**kwargs)

        # A genuine capability must not be accepted when either trusted expected binding is omitted.
        with self.assertRaises(authz.MachineBindingError):
            authz.guard_context_authorization(
                auth,
                context=authz.CONTEXT_EVALUATION,
                expected_request_hash=kwargs["request_hash"],
            )
        with self.assertRaises(authz.MachineBindingError):
            authz.guard_context_authorization(
                auth,
                context=authz.CONTEXT_EVALUATION,
                expected_policy_revision=kwargs["policy_revision"],
            )
        for blank in ("", "   "):
            with self.subTest(blank=repr(blank)):
                with self.assertRaises(authz.MachineBindingError):
                    authz.guard_context_authorization(
                        auth,
                        context=authz.CONTEXT_EVALUATION,
                        expected_request_hash=blank,
                        expected_policy_revision=kwargs["policy_revision"],
                    )

    def test_evaluation_context_rejects_structurally_identical_plain_dict(self) -> None:
        kwargs = _machine_factory_kwargs()
        auth = authz.create_machine_authorization(**kwargs)
        forged = dict(auth)
        self.assertIs(type(forged), dict)
        self.assertEqual(dict(auth), forged)

        with self.assertRaises(authz.MachineAuthorizationRequiredError):
            authz.guard_context_authorization(
                forged, context=authz.CONTEXT_EVALUATION, **self._expected(kwargs)
            )

    def test_evaluation_context_rejects_second_instance_with_same_content(self) -> None:
        kwargs = _machine_factory_kwargs()
        auth = authz.create_machine_authorization(**kwargs)

        for builder in (lambda: type(auth)(dict(auth)), lambda: dict(auth)):
            try:
                forged = builder()
            except TypeError:
                continue  # a constructor/shape mismatch is itself a rejection
            self.assertIsNot(forged, auth)
            with self.subTest(builder=builder):
                with self.assertRaises(authz.MachineAuthorizationRequiredError):
                    authz.guard_context_authorization(
                        forged, context=authz.CONTEXT_EVALUATION, **self._expected(kwargs)
                    )

    def test_evaluation_context_rejects_borrowed_internals(self) -> None:
        kwargs = _machine_factory_kwargs()
        auth = authz.create_machine_authorization(**kwargs)

        # An attacker who reads every exposed value off a genuine capability still cannot
        # reconstruct an accepted second object.
        attempts: list[Any] = [dict(auth)]
        for name in dir(auth):
            if name.startswith("__"):
                continue
            try:
                borrowed = getattr(auth, name)
            except AttributeError:  # pragma: no cover - defensive
                continue
            try:
                attempts.append(type(auth)(dict(auth), borrowed))
            except TypeError:
                continue
        for forged in attempts:
            with self.assertRaises(authz.AttachmentAuthorizationError):
                authz.guard_context_authorization(
                    forged, context=authz.CONTEXT_EVALUATION, **self._expected(kwargs)
                )

    def test_evaluation_context_rejects_wrong_class_or_type(self) -> None:
        kwargs = _machine_factory_kwargs()
        auth = authz.create_machine_authorization(**kwargs)

        wrong_class = dict(auth)
        wrong_class["receipt_class"] = "human"
        wrong_type = dict(auth)
        wrong_type["receipt_type"] = "attachment_manual_approval"

        for forged in (wrong_class, wrong_type):
            with self.subTest(forged=forged):
                with self.assertRaises(authz.MachineAuthorizationRequiredError):
                    authz.guard_context_authorization(
                        forged, context=authz.CONTEXT_EVALUATION, **self._expected(kwargs)
                    )

    def test_evaluation_context_rejects_tampered_issuer_policy_and_bindings(self) -> None:
        kwargs = _machine_factory_kwargs()
        for field, tampered in (
            ("approved_by", "attacker"),
            ("policy_revision", "evil-policy"),
            ("account", "attacker@example.org"),
            ("request_hash", "c" * 64),
        ):
            with self.subTest(field=field):
                auth = self._mint()
                auth[field] = tampered
                with self.assertRaises(authz.MachineAuthorizationProvenanceError):
                    authz.guard_context_authorization(
                        auth, context=authz.CONTEXT_EVALUATION, **self._expected(kwargs)
                    )

    def test_evaluation_context_rejects_missing_binding_on_genuine_object(self) -> None:
        kwargs = _machine_factory_kwargs()
        auth = self._mint()
        del auth["part_locator"]
        with self.assertRaises(authz.MachineAuthorizationProvenanceError):
            authz.guard_context_authorization(
                auth, context=authz.CONTEXT_EVALUATION, **self._expected(kwargs)
            )

    def test_evaluation_context_rejects_wrong_expected_policy_revision(self) -> None:
        kwargs = _machine_factory_kwargs()
        auth = self._mint(policy_revision="policy-v1")
        with self.assertRaises(authz.MachineBindingError):
            authz.guard_context_authorization(
                auth,
                context=authz.CONTEXT_EVALUATION,
                expected_request_hash=kwargs["request_hash"],
                expected_policy_revision="policy-v2",
            )

    def test_evaluation_context_rejects_wrong_expected_request_hash(self) -> None:
        kwargs = _machine_factory_kwargs()
        auth = self._mint()
        with self.assertRaises(authz.MachineBindingError):
            authz.guard_context_authorization(
                auth,
                context=authz.CONTEXT_EVALUATION,
                expected_request_hash="d" * 64,
                expected_policy_revision=kwargs["policy_revision"],
            )

    def test_machine_authorization_is_process_internal_and_not_serializable(self) -> None:
        auth = self._mint()
        for operation in (
            lambda: pickle.dumps(auth),
            lambda: copy.copy(auth),
            lambda: copy.deepcopy(auth),
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(TypeError):
                    operation()
        # No provenance/material attribute is exposed on the capability itself.
        self.assertFalse(hasattr(auth, "_provenance"))

    def test_issued_capability_is_not_kept_alive_by_the_registry(self) -> None:
        # Safe cleanup: the weak identity registry must not pin a dropped capability.
        auth = self._mint()
        ref = weakref.ref(auth)
        del auth
        gc.collect()
        self.assertIsNone(ref(), "The identity registry must release collected capabilities.")

    def test_unknown_context_is_rejected(self) -> None:
        auth = self._mint()
        with self.assertRaises(authz.UnknownAuthorizationContextError):
            authz.guard_context_authorization(auth, context="not_a_context")

    # ------------------------------------------------------------------
    # Human-Approval contexts: fail-closed on the machine class, typeless intact
    # ------------------------------------------------------------------
    def test_human_contexts_reject_machine_class_fail_closed(self) -> None:
        kwargs = _machine_factory_kwargs()
        auth = authz.create_machine_authorization(**kwargs)
        for context in (
            authz.CONTEXT_FILING,
            authz.CONTEXT_PROMOTION,
            authz.CONTEXT_EXPORT,
            authz.CONTEXT_DISPOSITION,
            authz.CONTEXT_APPLY,
            authz.CONTEXT_DIRECT_FETCH,
        ):
            with self.subTest(context=context):
                with self.assertRaises(authz.ReceiptClassRejectedError):
                    authz.guard_context_authorization(auth, context=context)
                # A structurally identical caller-supplied dictionary is insufficient too.
                with self.assertRaises(authz.ReceiptClassRejectedError):
                    authz.guard_context_authorization(dict(auth), context=context)

    def test_human_contexts_reject_machine_approver_identity_even_without_class(self) -> None:
        stripped = _typeless_human_receipt(approved_by=authz.MACHINE_APPROVER_ID)
        with self.assertRaises(authz.ReceiptClassRejectedError):
            authz.guard_context_authorization(stripped, context=authz.CONTEXT_DISPOSITION)

    def test_human_contexts_reject_unknown_receipt_class(self) -> None:
        forged = _typeless_human_receipt()
        forged["receipt_class"] = "android"
        with self.assertRaises(authz.ReceiptClassRejectedError):
            authz.guard_context_authorization(forged, context=authz.CONTEXT_FILING)

    def test_human_contexts_accept_typeless_human_receipt_unchanged(self) -> None:
        receipt = _typeless_human_receipt()
        for context in (
            authz.CONTEXT_FILING,
            authz.CONTEXT_PROMOTION,
            authz.CONTEXT_EXPORT,
            authz.CONTEXT_DISPOSITION,
            authz.CONTEXT_APPLY,
            authz.CONTEXT_DIRECT_FETCH,
        ):
            with self.subTest(context=context):
                info = authz.guard_context_authorization(receipt, context=context)
                self.assertEqual("human", info["class"])
        # No hidden mutation / migration of the persisted typeless form.
        self.assertNotIn("receipt_type", receipt)
        self.assertNotIn("receipt_class", receipt)


class MailDeskAttachmentEvaluationMDE1CallsiteTests(unittest.TestCase):
    """T03: the named Human-Approval callsites reject the machine class fail-closed."""

    def setUp(self) -> None:
        self.maxDiff = None
        self._himalaya_blocker = patch.object(
            himalaya,
            "run_himalaya",
            side_effect=RuntimeError("Real Himalaya process execution is forbidden in hermetic unit tests!"),
        )
        self._himalaya_blocker.start()
        self._env_backup = {
            key: os.environ.pop(key, None)
            for key in (
                "WORKSPACE_ROOT",
                "WORKSPACE_LOCK_ALLOW_LEGACY",
                "WORKSPACE_LOCK_LEASE_ID",
                "WORKSPACE_LOCK_CONVERSATION_ID",
            )
        }

    def tearDown(self) -> None:
        for key, value in self._env_backup.items():
            if value is not None:
                os.environ[key] = value
        self._himalaya_blocker.stop()

    def _mint_machine(self) -> Any:
        return authz.create_machine_authorization(**_machine_factory_kwargs())

    # ------------------------------------------------------------------
    # Filing (attachment_filing.validate_mda2_attachment)
    # ------------------------------------------------------------------
    def test_filing_callsite_rejects_machine_authorization(self) -> None:
        composite = _build_mda2_composite(dict(self._mint_machine()))
        with self.assertRaises(authz.ReceiptClassRejectedError):
            afiling.validate_mda2_attachment(composite, manifest_account=_ACCOUNT)

    def test_filing_callsite_still_accepts_typeless_human_receipt(self) -> None:
        composite = _build_mda2_composite()
        normalized = afiling.validate_mda2_attachment(composite, manifest_account=_ACCOUNT)
        self.assertEqual(_ACCOUNT, normalized["account"])

    # ------------------------------------------------------------------
    # Disposition + apply (attachment_disposition_log)
    # ------------------------------------------------------------------
    def test_record_disposition_callsite_rejects_machine_authorization(self) -> None:
        guard = afetch._load_workspace_lock_guard()
        with tempfile.TemporaryDirectory() as tmp_dir:
            guard.acquire_workspace_lock(
                tmp_dir,
                harness="mde1-t03-test",
                lease_id="lease-t03",
                conversation_id="conv-t03",
            )
            machine = dict(self._mint_machine())
            with self.assertRaises(authz.ReceiptClassRejectedError):
                adisp.record_disposition_entry(
                    payload={
                        "attachment_id": "a" * 64,
                        "decision": "discard",
                        "approval_receipt": machine,
                    },
                    workspace_root=tmp_dir,
                    lease_id="lease-t03",
                    conversation_id="conv-t03",
                )

    def test_verify_apply_receipt_rejects_machine_authorization(self) -> None:
        with self.assertRaises(authz.ReceiptClassRejectedError):
            adisp.verify_apply_receipt(dict(self._mint_machine()), "e" * 64)

    def test_verify_apply_receipt_still_accepts_typeless_human_receipt(self) -> None:
        apply_request = {
            "action": "discard",
            "attachment_id": "a" * 64,
            "decision_id": "b" * 64,
            "index_entry_sha256": "c" * 64,
            "quarantine_path": "data/mail-desk/attachments/run_x/a.pdf",
            "sha256": "d" * 64,
            "size_bytes": 12,
            "run_id": "run_x",
            "schema_version": 1,
        }
        request_hash = adisp.canonical_apply_request_sha256(apply_request)
        receipt = adisp.build_apply_receipt(request_hash=request_hash, approved_by="human_operator")
        info = adisp.verify_apply_receipt(receipt, request_hash)
        self.assertEqual(request_hash, info["receipt"]["request_hash"])

    # ------------------------------------------------------------------
    # Manifest-driven direct fetch (mail_desk_himalaya_client)
    # ------------------------------------------------------------------
    def _write_manifest(self, tmp_dir: str, receipt: Any) -> Path:
        manifest_path = Path(tmp_dir) / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "account": _ACCOUNT,
                    "delete_input_on_success": False,
                    "operations": [
                        {
                            "action": "attachment_fetch",
                            "account": _ACCOUNT,
                            "folder": _FOLDER,
                            "envelope_id": _ENVELOPE_ID,
                            "message_id": "mde1-t03@example.org",
                            "part_locator": "2",
                            "inventory_sha256": "a" * 64,
                            "review_hash": "a" * 64,
                            "run_id": "run_mde1_t03_fetch",
                            "approval_receipt": receipt,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return manifest_path

    def test_manifest_direct_fetch_rejects_machine_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            manifest_path = self._write_manifest(tmp_dir, dict(self._mint_machine()))
            result = mclient.execute_manifest(manifest_path)

        self.assertFalse(result["all_succeeded"])
        error = str(result["results"][0]["error"]).lower()
        self.assertIn("machine", error)

    def test_manifest_direct_fetch_still_reaches_typeless_human_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            manifest_path = self._write_manifest(tmp_dir, _typeless_human_receipt())
            with patch.object(
                afetch,
                "op_attachment_fetch",
                return_value={"status": "fetched", "run_id": "run_mde1_t03_fetch"},
            ) as mocked_fetch:
                result = mclient.execute_manifest(manifest_path)

        self.assertTrue(mocked_fetch.called, "A typeless human receipt must pass the direct-fetch guard.")
        self.assertTrue(result["all_succeeded"])


# ======================================================================
# T04: `attachment_evaluate` orchestrator skeleton
# ======================================================================

_T04_MESSAGE_ID = "mde1-t04-orchestrator@example.org"

#: T05 fetch/extract/handoff and every downstream mutation seam.  The T04 orchestrator must
#: neither import nor call any of these.
_T04_FORBIDDEN_CALLABLES = frozenset(
    {
        "op_attachment_fetch",
        "extract_attachment_content",
        "build_attachment_analysis_handoff",
        "apply_attachment_handoff_to_item",
        "propose_attachment_filing",
        "record_disposition_entry",
        "apply_discard",
        "record_quarantine_entry",
        "save_quarantine_index_atomic",
        "update_quarantine_inventory_atomic",
    }
)
_T04_FORBIDDEN_MODULES = frozenset(
    {
        "attachment_extract",
        "attachment_handoff",
        "attachment_disposition_log",
        "attachment_filing",
        "attachment_quarantine_index",
        "quarantine_preflight",
    }
)


def _t04_pdf_payload(marker: str = "MDE1-T04-PDF-CONTENT") -> bytes:
    return b"%PDF-1.4\n% " + marker.encode("utf-8") + b"\n"


def _t04_docx_payload(marker: str = "MDE1-T04-DOCX-CONTENT") -> bytes:
    return b"PK\x03\x04" + marker.encode("utf-8")


def _t04_attachment_part(
    filename: str = "report.pdf",
    *,
    mime_type: str = "application/pdf",
    data: bytes | None = None,
) -> dict[str, Any]:
    return {
        "filename": filename,
        "mime_type": mime_type,
        "data": _t04_pdf_payload() if data is None else data,
    }


def _t04_raw_eml(
    *,
    attachments_list: list[dict[str, Any]] | None = None,
    body_text: str = "Body",
    message_id: str = f"<{_T04_MESSAGE_ID}>",
) -> bytes:
    return attachments.build_test_eml(
        subject="MDE1 T04",
        message_id=message_id,
        body_text=body_text,
        attachments=attachments_list or [],
    )


def _t04_clear_decision(**overrides: Any) -> dict[str, Any]:
    """A concrete, assigned final decision: nothing in the trigger contract fires."""
    decision: dict[str, Any] = {
        "kind": "project",
        "id": "boku-lll",
        "confidence": "high",
        "needs_reply": False,
    }
    decision.update(overrides)
    return decision


def _t04_ambiguous_decision(**overrides: Any) -> dict[str, Any]:
    """The classifier's canonical ambiguous fallback decision."""
    decision: dict[str, Any] = {
        "kind": "unknown",
        "id": "unclassified",
        "confidence": "low",
        "review_required": True,
    }
    decision.update(overrides)
    return decision


def _t04_custom_policy(version: str) -> dict[str, Any]:
    policy = json.loads(json.dumps(DEFAULT_ATTACHMENT_POLICY))
    policy["version"] = version
    return policy


class MailDeskAttachmentEvaluationMDE1OrchestratorTests(unittest.TestCase):
    """T04: public `attachment_evaluate` seam — trigger matrix and bounded stage outcomes."""

    def setUp(self) -> None:
        self.maxDiff = None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _evaluate(
        self,
        *,
        decision: dict[str, Any],
        raw_eml: bytes,
        read_escalation: Any = None,
        policy: Any = None,
        message_id: str = _T04_MESSAGE_ID,
        account: str = _ACCOUNT,
        folder: str = _FOLDER,
        envelope_id: str | int = _ENVELOPE_ID,
    ) -> dict[str, Any]:
        return aevaluate.attachment_evaluate(
            raw_eml=raw_eml,
            account=account,
            folder=folder,
            envelope_id=envelope_id,
            message_id=message_id,
            decision=decision,
            read_escalation=read_escalation,
            policy=policy,
        )

    @staticmethod
    def _staged(result: dict[str, Any]) -> dict[str, Any]:
        return result["attachment_evaluation"]

    def _assert_staged_contract(
        self,
        staged: dict[str, Any],
        *,
        status: str,
        reason: str,
        authorization: str,
    ) -> None:
        self.assertEqual(
            {
                "status",
                "reason",
                "authorization",
                "files",
                "used_for_classification",
                "classifier_revision",
            },
            set(staged),
            "The staged object must expose exactly the canonical six fields.",
        )
        self.assertEqual(status, staged["status"])
        self.assertEqual(reason, staged["reason"])
        self.assertEqual(authorization, staged["authorization"])
        self.assertEqual([], staged["files"])
        self.assertIs(False, staged["used_for_classification"])
        self.assertIsNone(staged["classifier_revision"])
        self.assertIn(staged["status"], aevaluate.ALLOWED_ATTACHMENT_EVALUATION_STATUSES)
        self.assertIn(staged["reason"], aevaluate.ALLOWED_ATTACHMENT_EVALUATION_REASONS)
        self.assertIn(
            staged["authorization"], aevaluate.ALLOWED_ATTACHMENT_EVALUATION_AUTHORIZATIONS
        )

    # ------------------------------------------------------------------
    # Clear decision: no evaluation, no attachment processing
    # ------------------------------------------------------------------
    def test_clear_decision_with_allowed_pdf_is_not_needed(self) -> None:
        raw_eml = _t04_raw_eml(attachments_list=[_t04_attachment_part()])

        result = self._evaluate(decision=_t04_clear_decision(), raw_eml=raw_eml)

        self.assertEqual({"attachment_evaluation"}, set(result))
        self._assert_staged_contract(
            self._staged(result),
            status="not_needed",
            reason="classification_clear",
            authorization="not_applicable",
        )

    def test_missing_review_required_is_false_and_clear_decision_is_not_retriggered(self) -> None:
        # A clear final decision with no `review_required` key must not trigger.
        raw_eml = _t04_raw_eml(attachments_list=[_t04_attachment_part()])
        decision = _t04_clear_decision()
        self.assertNotIn("review_required", decision)

        result = self._evaluate(decision=decision, raw_eml=raw_eml)

        self.assertEqual("classification_clear", self._staged(result)["reason"])

    def test_clear_decision_is_resolved_before_any_mint_or_guard(self) -> None:
        raw_eml = _t04_raw_eml(attachments_list=[_t04_attachment_part()])
        with patch.object(
            aevaluate, "create_machine_authorization", side_effect=AssertionError("must not mint")
        ) as minted, patch.object(
            aevaluate, "guard_context_authorization", side_effect=AssertionError("must not guard")
        ) as guarded:
            result = self._evaluate(decision=_t04_clear_decision(), raw_eml=raw_eml)

        minted.assert_not_called()
        guarded.assert_not_called()
        self.assertEqual("classification_clear", self._staged(result)["reason"])

    # ------------------------------------------------------------------
    # Trigger predicates, each exercised independently
    # ------------------------------------------------------------------
    def test_each_trigger_predicate_triggers_evaluation(self) -> None:
        raw_eml = _t04_raw_eml(attachments_list=[_t04_attachment_part()])
        cases = {
            "kind_unknown": {
                "kind": "unknown",
                "id": "proj-x",
                "confidence": "high",
                "needs_reply": False,
            },
            "id_unclassified": {
                "kind": "project",
                "id": "unclassified",
                "confidence": "high",
                "needs_reply": False,
            },
            "confidence_low": {
                "kind": "project",
                "id": "proj-x",
                "confidence": "low",
                "needs_reply": False,
            },
            "review_required_true": {
                "kind": "project",
                "id": "proj-x",
                "confidence": "high",
                "needs_reply": False,
                "review_required": True,
            },
        }
        for name, decision in cases.items():
            with self.subTest(predicate=name):
                result = self._evaluate(decision=decision, raw_eml=raw_eml)
                self._assert_staged_contract(
                    self._staged(result),
                    status="skipped",
                    reason="evaluation_pending",
                    authorization="auto_evaluated",
                )

    def test_trigger_predicates_require_exact_values(self) -> None:
        """FR-15 trigger predicates are exact equalities — no strip/lowercase coercion.

        `decision.kind`, `decision.id` and `decision.confidence` must be compared verbatim, so
        an uppercase or space-padded variant of `unknown` / `unclassified` / `low` (or a
        non-string) must **not** trigger an evaluation and must therefore never mint or guard an
        authorization.  `review_required is True` stays an exact identity check.
        """
        raw_eml = _t04_raw_eml(attachments_list=[_t04_attachment_part()])
        non_triggering = {
            "kind_uppercase": {"kind": "UNKNOWN"},
            "kind_mixed_case": {"kind": "Unknown"},
            "kind_padded": {"kind": " unknown "},
            "kind_trailing_pad": {"kind": "unknown "},
            "kind_none": {"kind": None},
            "id_uppercase": {"id": "UNCLASSIFIED"},
            "id_mixed_case": {"id": "Unclassified"},
            "id_padded": {"id": " unclassified "},
            "confidence_uppercase": {"confidence": "LOW"},
            "confidence_mixed_case": {"confidence": "Low"},
            "confidence_padded": {"confidence": " low "},
            "confidence_non_string": {"confidence": 1},
            "review_required_truthy_int": {"review_required": 1},
            "review_required_string": {"review_required": "true"},
        }
        with patch.object(
            aevaluate, "create_machine_authorization", side_effect=AssertionError("must not mint")
        ) as minted:
            for name, override in non_triggering.items():
                with self.subTest(predicate=name):
                    result = self._evaluate(
                        decision=_t04_clear_decision(**override), raw_eml=raw_eml
                    )
                    self._assert_staged_contract(
                        self._staged(result),
                        status="not_needed",
                        reason="classification_clear",
                        authorization="not_applicable",
                    )
        minted.assert_not_called()

    def test_read_escalation_with_ambiguous_decision_triggers(self) -> None:
        raw_eml = _t04_raw_eml(attachments_list=[_t04_attachment_part()])
        escalations = {
            "failed": {
                "level": "full_body",
                "triggers": ["insufficient_preview_evidence"],
                "status": "failed",
                "error": {"type": "RuntimeError", "message": "full read failed"},
            },
            "completed": {
                "level": "full_body",
                "triggers": ["visible_action_or_reply_request"],
                "status": "completed",
            },
        }
        for name, escalation in escalations.items():
            with self.subTest(escalation_status=name):
                result = self._evaluate(
                    decision=_t04_ambiguous_decision(),
                    raw_eml=raw_eml,
                    read_escalation=escalation,
                )
                self._assert_staged_contract(
                    self._staged(result),
                    status="skipped",
                    reason="evaluation_pending",
                    authorization="auto_evaluated",
                )

    def test_read_escalation_nested_inside_decision_triggers(self) -> None:
        # Production also emits the escalation as an item sibling nested under the decision.
        raw_eml = _t04_raw_eml(attachments_list=[_t04_attachment_part()])
        decision = _t04_ambiguous_decision(
            read_escalation={"level": "full_body", "status": "failed", "triggers": ["x"]}
        )

        result = self._evaluate(decision=decision, raw_eml=raw_eml)

        self.assertEqual("evaluation_pending", self._staged(result)["reason"])

    def test_malformed_nested_read_escalation_fails_closed(self) -> None:
        """A present-but-non-mapping nested `read_escalation` is a contract violation.

        Neither `None` nor a mapping is acceptable when `decision["read_escalation"]` is
        present; the malformed value must fail closed instead of being silently treated as
        absent, for an already-ambiguous decision as well as an otherwise-clear one.
        """
        raw_eml = _t04_raw_eml(attachments_list=[_t04_attachment_part()])
        for malformed in ("not-a-mapping", 123, ["full_body"], object()):
            with self.subTest(malformed=repr(malformed)):
                ambiguous = _t04_ambiguous_decision(read_escalation=malformed)
                with self.assertRaises(aevaluate.AttachmentEvaluationError):
                    self._evaluate(decision=ambiguous, raw_eml=raw_eml)

                clear = _t04_clear_decision(read_escalation=malformed)
                with self.assertRaises(aevaluate.AttachmentEvaluationError):
                    self._evaluate(decision=clear, raw_eml=raw_eml)

    def test_explicit_read_escalation_takes_precedence_over_malformed_nested(self) -> None:
        """An explicitly supplied valid escalation wins; the nested value is never examined."""
        raw_eml = _t04_raw_eml(attachments_list=[_t04_attachment_part()])
        explicit = {"level": "full_body", "status": "failed", "triggers": ["x"]}
        decision = _t04_ambiguous_decision(read_escalation="not-a-mapping")

        result = self._evaluate(decision=decision, raw_eml=raw_eml, read_escalation=explicit)

        self.assertEqual("evaluation_pending", self._staged(result)["reason"])

    def test_nested_read_escalation_none_is_treated_as_absent(self) -> None:
        """A nested `read_escalation` of `None` is the documented absent form, not malformed."""
        raw_eml = _t04_raw_eml(attachments_list=[_t04_attachment_part()])
        decision = _t04_ambiguous_decision(read_escalation=None)

        result = self._evaluate(decision=decision, raw_eml=raw_eml)

        self.assertEqual("evaluation_pending", self._staged(result)["reason"])

    def test_read_escalation_does_not_retrigger_an_otherwise_clear_decision(self) -> None:
        raw_eml = _t04_raw_eml(attachments_list=[_t04_attachment_part()])
        for status in ("completed", "failed"):
            with self.subTest(escalation_status=status):
                escalation: dict[str, Any] = {"level": "full_body", "status": status, "triggers": ["x"]}
                if status == "failed":
                    escalation["error"] = {"type": "RuntimeError", "message": "boom"}
                result = self._evaluate(
                    decision=_t04_clear_decision(),
                    raw_eml=raw_eml,
                    read_escalation=escalation,
                )
                self.assertEqual("classification_clear", self._staged(result)["reason"])

    # ------------------------------------------------------------------
    # Bounded no-op outcomes
    # ------------------------------------------------------------------
    def test_ambiguous_without_attachments_is_not_needed_no_attachments(self) -> None:
        raw_eml = _t04_raw_eml(attachments_list=[])

        result = self._evaluate(decision=_t04_ambiguous_decision(), raw_eml=raw_eml)

        self._assert_staged_contract(
            self._staged(result),
            status="not_needed",
            reason="no_attachments",
            authorization="not_applicable",
        )

    def test_ambiguous_with_only_disallowed_attachment_is_not_needed_no_allowed(self) -> None:
        raw_eml = _t04_raw_eml(
            attachments_list=[
                _t04_attachment_part(
                    "tool.exe",
                    mime_type="application/octet-stream",
                    data=b"MZ\x90\x00active",
                )
            ]
        )

        result = self._evaluate(decision=_t04_ambiguous_decision(), raw_eml=raw_eml)

        self._assert_staged_contract(
            self._staged(result),
            status="not_needed",
            reason="no_allowed_attachments",
            authorization="not_applicable",
        )

    def test_ambiguous_without_eligible_attachment_never_mints(self) -> None:
        raw_eml = _t04_raw_eml(
            attachments_list=[
                _t04_attachment_part(
                    "tool.exe",
                    mime_type="application/octet-stream",
                    data=b"MZ\x90\x00active",
                )
            ]
        )
        with patch.object(
            aevaluate, "create_machine_authorization", side_effect=AssertionError("must not mint")
        ) as minted:
            self._evaluate(decision=_t04_ambiguous_decision(), raw_eml=raw_eml)

        minted.assert_not_called()

    # ------------------------------------------------------------------
    # Eligible path: bounded pre-T05 staged outcome
    # ------------------------------------------------------------------
    def test_ambiguous_with_allowed_attachment_is_skipped_evaluation_pending(self) -> None:
        raw_eml = _t04_raw_eml(attachments_list=[_t04_attachment_part()])

        result = self._evaluate(decision=_t04_ambiguous_decision(), raw_eml=raw_eml)

        self._assert_staged_contract(
            self._staged(result),
            status="skipped",
            reason="evaluation_pending",
            authorization="auto_evaluated",
        )

    def test_all_outcomes_pin_false_and_null_and_empty_files(self) -> None:
        allowed = _t04_raw_eml(attachments_list=[_t04_attachment_part()])
        no_attachment = _t04_raw_eml(attachments_list=[])
        disallowed = _t04_raw_eml(
            attachments_list=[
                _t04_attachment_part(
                    "tool.exe", mime_type="application/octet-stream", data=b"MZ\x90\x00"
                )
            ]
        )
        matrix = [
            (self._evaluate(decision=_t04_clear_decision(), raw_eml=allowed), "classification_clear"),
            (self._evaluate(decision=_t04_ambiguous_decision(), raw_eml=no_attachment), "no_attachments"),
            (
                self._evaluate(decision=_t04_ambiguous_decision(), raw_eml=disallowed),
                "no_allowed_attachments",
            ),
            (
                self._evaluate(decision=_t04_ambiguous_decision(), raw_eml=allowed),
                "evaluation_pending",
            ),
        ]
        for result, reason in matrix:
            with self.subTest(reason=reason):
                staged = self._staged(result)
                self.assertEqual(reason, staged["reason"])
                self.assertEqual([], staged["files"])
                self.assertIs(False, staged["used_for_classification"])
                self.assertIsNone(staged["classifier_revision"])

    # ------------------------------------------------------------------
    # Machine-authorization factory/guard binding
    # ------------------------------------------------------------------
    def test_machine_authorization_is_minted_and_guarded_with_canonical_bindings(self) -> None:
        payload = _t04_pdf_payload()
        sha256 = hashlib.sha256(payload).hexdigest()
        raw_eml = _t04_raw_eml(attachments_list=[_t04_attachment_part(data=payload)])
        expected_review_hash = afetch.compute_review_hash(
            account=_ACCOUNT,
            message_id=_T04_MESSAGE_ID,
            folder=_FOLDER,
            envelope_id=_ENVELOPE_ID,
            part_locator="2",
            inventory_sha256=sha256,
        )
        real_create = aevaluate.create_machine_authorization
        real_guard = aevaluate.guard_context_authorization

        with patch.object(
            aevaluate, "create_machine_authorization", side_effect=real_create
        ) as minted, patch.object(
            aevaluate, "guard_context_authorization", side_effect=real_guard
        ) as guarded:
            result = self._evaluate(
                decision=_t04_ambiguous_decision(),
                raw_eml=raw_eml,
                message_id=_T04_MESSAGE_ID,
            )

        self.assertEqual(1, minted.call_count)
        mint_kwargs = minted.call_args.kwargs
        self.assertEqual(expected_review_hash, mint_kwargs["request_hash"])
        self.assertEqual("1.0.0", mint_kwargs["policy_revision"])
        self.assertEqual(sha256, mint_kwargs["inventory_sha256"])
        self.assertEqual("2", mint_kwargs["part_locator"])
        self.assertEqual(_ACCOUNT, mint_kwargs["account"])
        self.assertEqual(_FOLDER, mint_kwargs["folder"])
        self.assertEqual(str(_ENVELOPE_ID), mint_kwargs["envelope_id"])
        self.assertEqual(_T04_MESSAGE_ID, mint_kwargs["message_id"])

        self.assertEqual(1, guarded.call_count)
        guard_kwargs = guarded.call_args.kwargs
        self.assertEqual(authz.CONTEXT_EVALUATION, guard_kwargs["context"])
        self.assertEqual(expected_review_hash, guard_kwargs["expected_request_hash"])
        self.assertEqual("1.0.0", guard_kwargs["expected_policy_revision"])

        # The opaque capability is never serialized or leaked into the staged output.
        self.assertEqual("auto_evaluated", self._staged(result)["authorization"])
        json.dumps(result)

    def test_each_eligible_part_gets_its_own_authorization(self) -> None:
        pdf_payload = _t04_pdf_payload()
        docx_payload = _t04_docx_payload()
        raw_eml = _t04_raw_eml(
            attachments_list=[
                _t04_attachment_part("report.pdf", data=pdf_payload),
                _t04_attachment_part(
                    "notes.docx",
                    mime_type=(
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                    ),
                    data=docx_payload,
                ),
            ]
        )
        expected_hashes = {
            afetch.compute_review_hash(
                account=_ACCOUNT,
                message_id=_T04_MESSAGE_ID,
                folder=_FOLDER,
                envelope_id=_ENVELOPE_ID,
                part_locator="2",
                inventory_sha256=hashlib.sha256(pdf_payload).hexdigest(),
            ),
            afetch.compute_review_hash(
                account=_ACCOUNT,
                message_id=_T04_MESSAGE_ID,
                folder=_FOLDER,
                envelope_id=_ENVELOPE_ID,
                part_locator="3",
                inventory_sha256=hashlib.sha256(docx_payload).hexdigest(),
            ),
        }
        real_create = aevaluate.create_machine_authorization
        real_guard = aevaluate.guard_context_authorization

        with patch.object(
            aevaluate, "create_machine_authorization", side_effect=real_create
        ) as minted, patch.object(
            aevaluate, "guard_context_authorization", side_effect=real_guard
        ) as guarded:
            result = self._evaluate(decision=_t04_ambiguous_decision(), raw_eml=raw_eml)

        self.assertEqual(2, minted.call_count)
        self.assertEqual(2, guarded.call_count)
        self.assertEqual(
            expected_hashes,
            {call.kwargs["request_hash"] for call in minted.call_args_list},
        )
        self.assertEqual("evaluation_pending", self._staged(result)["reason"])
        self.assertEqual("auto_evaluated", self._staged(result)["authorization"])

    def test_policy_revision_is_derived_from_the_effective_trusted_policy(self) -> None:
        raw_eml = _t04_raw_eml(attachments_list=[_t04_attachment_part()])
        real_create = aevaluate.create_machine_authorization
        real_guard = aevaluate.guard_context_authorization

        with patch.object(
            aevaluate, "create_machine_authorization", side_effect=real_create
        ) as minted, patch.object(
            aevaluate, "guard_context_authorization", side_effect=real_guard
        ) as guarded:
            self._evaluate(
                decision=_t04_ambiguous_decision(),
                raw_eml=raw_eml,
                policy=_t04_custom_policy("2.3.4"),
            )

        self.assertEqual("2.3.4", minted.call_args.kwargs["policy_revision"])
        self.assertEqual("2.3.4", guarded.call_args.kwargs["expected_policy_revision"])

    def test_malformed_or_missing_policy_revision_fails_closed(self) -> None:
        raw_eml = _t04_raw_eml(attachments_list=[_t04_attachment_part()])
        for bad_policy in (
            {"version": ""},
            {"version": "   "},
            {"version": None},
            {"version": 123},
            {},
            "not-a-mapping",
        ):
            with self.subTest(policy=bad_policy):
                with self.assertRaises(aevaluate.AttachmentEvaluationError):
                    self._evaluate(
                        decision=_t04_ambiguous_decision(),
                        raw_eml=raw_eml,
                        policy=bad_policy,
                    )

    # ------------------------------------------------------------------
    # No caller authority may enter through the API
    # ------------------------------------------------------------------
    def test_forged_caller_like_inputs_cannot_enter_the_api(self) -> None:
        raw_eml = _t04_raw_eml(attachments_list=[_t04_attachment_part()])
        base = {
            "raw_eml": raw_eml,
            "account": _ACCOUNT,
            "folder": _FOLDER,
            "envelope_id": _ENVELOPE_ID,
            "message_id": _T04_MESSAGE_ID,
            "decision": _t04_ambiguous_decision(),
        }
        forged_inputs: list[dict[str, Any]] = [
            {"candidate": {"filename": "x.pdf"}},
            {"candidates": [{"filename": "x.pdf"}]},
            {"policy_status": "allowed"},
            {"fetch_status": "available"},
            {"machine_receipt": {"receipt_id": "rec"}},
            {"authorization": "auto_evaluated"},
            {"staged_status": "skipped"},
            {"status": "skipped"},
            {"reason": "evaluation_pending"},
            {"files": [{"filename": "x.pdf"}]},
            {"used_for_classification": True},
            {"classifier_revision": "a" * 64},
            {"expected_policy_revision": "9.9.9"},
            {"expected_request_hash": "a" * 64},
        ]
        for forged in forged_inputs:
            with self.subTest(forged=sorted(forged)):
                with self.assertRaises(TypeError):
                    aevaluate.attachment_evaluate(**base, **forged)

    def test_forged_decision_metadata_cannot_influence_the_result(self) -> None:
        allowed = _t04_raw_eml(attachments_list=[_t04_attachment_part()])
        disallowed = _t04_raw_eml(
            attachments_list=[
                _t04_attachment_part(
                    "tool.exe", mime_type="application/octet-stream", data=b"MZ\x90\x00"
                )
            ]
        )
        forged_clear = _t04_clear_decision(
            policy_status="allowed",
            authorization="auto_evaluated",
            receipt_class="machine",
            files=[{"filename": "x.pdf"}],
            used_for_classification=True,
        )
        forged_ambiguous = _t04_ambiguous_decision(
            fetch_status="available", policy_status="allowed"
        )

        clear_result = self._evaluate(decision=forged_clear, raw_eml=allowed)
        disallowed_result = self._evaluate(decision=forged_ambiguous, raw_eml=disallowed)

        self.assertEqual("classification_clear", self._staged(clear_result)["reason"])
        self.assertEqual("no_allowed_attachments", self._staged(disallowed_result)["reason"])

    # ------------------------------------------------------------------
    # No raw content or absolute paths in long-lived outputs
    # ------------------------------------------------------------------
    def test_outputs_contain_no_raw_content_or_absolute_paths(self) -> None:
        secret = "TOPSECRETBODY-MDE1-T04"
        windows_path = "C:\\synthetic-root\\secret\\leak.pdf"
        raw_eml = _t04_raw_eml(
            attachments_list=[_t04_attachment_part()],
            body_text=f"{secret} {windows_path}",
        )

        result = self._evaluate(decision=_t04_ambiguous_decision(), raw_eml=raw_eml)
        serialized = json.dumps(result)

        self.assertNotIn(secret, serialized)
        self.assertNotIn(windows_path, serialized)
        self.assertNotIn("report.pdf", serialized)
        self.assertNotRegex(serialized, r"[A-Za-z]:\\\\")

    # ------------------------------------------------------------------
    # No T05 / mutation seam may be imported or called
    # ------------------------------------------------------------------
    def test_no_forbidden_t05_or_mutation_seam_is_imported(self) -> None:
        module_path = MAIL_DESK_ROOT / "scripts" / "core" / "attachment_evaluation.py"
        tree = ast.parse(module_path.read_text(encoding="utf-8"))

        imported_modules: set[str] = set()
        referenced_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_modules.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported_modules.add(node.module.split(".")[0])
                for alias in node.names:
                    referenced_names.add(alias.name)
            elif isinstance(node, ast.Attribute):
                referenced_names.add(node.attr)
            elif isinstance(node, ast.Name):
                referenced_names.add(node.id)

        self.assertFalse(
            _T04_FORBIDDEN_CALLABLES & referenced_names,
            f"T04 imported/referenced forbidden callable(s): "
            f"{sorted(_T04_FORBIDDEN_CALLABLES & referenced_names)}",
        )
        self.assertFalse(
            _T04_FORBIDDEN_MODULES & imported_modules,
            f"T04 imported forbidden module(s): {sorted(_T04_FORBIDDEN_MODULES & imported_modules)}",
        )

    def test_no_forbidden_t05_or_mutation_seam_is_called(self) -> None:
        raw_eml = _t04_raw_eml(attachments_list=[_t04_attachment_part()])
        blockers = [
            patch.object(afetch, "op_attachment_fetch", side_effect=AssertionError("T05 fetch ran")),
            patch.object(
                aextract, "extract_attachment_content", side_effect=AssertionError("T05 extract ran")
            ),
            patch.object(
                ahandoff,
                "build_attachment_analysis_handoff",
                side_effect=AssertionError("T05 handoff ran"),
            ),
            patch.object(
                ahandoff,
                "apply_attachment_handoff_to_item",
                side_effect=AssertionError("handoff install ran"),
            ),
            patch.object(
                afiling, "propose_attachment_filing", side_effect=AssertionError("filing ran")
            ),
            patch.object(
                adisp, "record_disposition_entry", side_effect=AssertionError("disposition ran")
            ),
            patch.object(adisp, "apply_discard", side_effect=AssertionError("discard ran")),
        ]
        started = [blocker.start() for blocker in blockers]
        try:
            result = self._evaluate(decision=_t04_ambiguous_decision(), raw_eml=raw_eml)
        finally:
            for blocker in blockers:
                blocker.stop()

        for mock in started:
            mock.assert_not_called()
        self.assertEqual("evaluation_pending", self._staged(result)["reason"])

    def test_non_bytes_or_str_raw_eml_fails_closed(self) -> None:
        """`raw_eml` accepts only bytes or str; any other type fails closed before parsing."""
        for malformed in (None, 123, bytearray(b"%PDF-1.4\n"), ["%PDF-1.4"]):
            with self.subTest(raw_eml=repr(malformed)):
                with self.assertRaises(aevaluate.AttachmentEvaluationError):
                    self._evaluate(
                        decision=_t04_ambiguous_decision(),
                        raw_eml=malformed,  # type: ignore[arg-type]
                    )

    def test_clear_decision_with_malformed_raw_eml_fails_closed(self) -> None:
        """A malformed `raw_eml` is a contract error even when the decision is already clear.

        The trusted-input type check must run before the clear-decision no-op branch, so a
        malformed `raw_eml` never silently degrades into a staged `classification_clear`
        outcome.
        """
        for malformed in (None, 123, bytearray(b"%PDF-1.4\n"), ["%PDF-1.4"]):
            with self.subTest(raw_eml=repr(malformed)):
                with self.assertRaises(aevaluate.AttachmentEvaluationError):
                    self._evaluate(
                        decision=_t04_clear_decision(),
                        raw_eml=malformed,  # type: ignore[arg-type]
                    )

    def test_invalid_binding_and_decision_inputs_fail_closed(self) -> None:
        raw_eml = _t04_raw_eml(attachments_list=[_t04_attachment_part()])
        invalid_cases = [
            {"account": ""},
            {"folder": "   "},
            {"envelope_id": ""},
            {"message_id": ""},
        ]
        for override in invalid_cases:
            with self.subTest(override=override):
                with self.assertRaises(aevaluate.AttachmentEvaluationError):
                    self._evaluate(
                        decision=_t04_ambiguous_decision(), raw_eml=raw_eml, **override
                    )
        with self.assertRaises(aevaluate.AttachmentEvaluationError):
            self._evaluate(decision="not-a-mapping", raw_eml=raw_eml)  # type: ignore[arg-type]
        with self.assertRaises(aevaluate.AttachmentEvaluationError):
            self._evaluate(
                decision=_t04_ambiguous_decision(),
                raw_eml=raw_eml,
                read_escalation="not-a-mapping",
            )


if __name__ == "__main__":
    unittest.main()
