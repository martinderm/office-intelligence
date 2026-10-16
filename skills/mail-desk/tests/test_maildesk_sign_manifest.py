"""Failing tests for the review-correction signature helper (DOC-P1).

Pins the contract of ``scripts/mail_desk_sign_manifest.py``: it re-signs a
reviewed ``batch-manifest.json`` by recomputing ``candidate_count`` from
``len(items)``, rebuilding the canonical execute-request hash, replacing any
prior pending ``review`` block with an approved approval receipt, self-checking
via ``validate_execute_contract``, and writing back atomically. The helper must
be deterministic (injectable clock), emit one machine-readable JSON envelope on
stdout, and touch nothing else in the workspace.

The canonical rule and receipt shape come from ``core/batch_contract.py``; the
envelope convention from ``core/envelope.py``. This module imports the helper,
so it is a genuine red until ``mail_desk_sign_manifest.py`` exists.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = MAIL_DESK_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import mail_desk_sign_manifest as signer  # noqa: E402

from core import common as core_common  # noqa: E402
from core.batch_contract import (  # noqa: E402
    add_draft_contract,
    canonical_execute_request_sha256,
    validate_execute_contract,
)
from core.envelope import ENVELOPE_KEYS, _validate_envelope  # noqa: E402


SCRIPT = SCRIPTS_DIR / "mail_desk_sign_manifest.py"
RECEIPT_KEYS = {"reviewed_at", "reviewed_by", "execute_request_sha256"}


def _items(count: int = 2, source_folder: str = "INBOX") -> list[dict]:
    return [
        {"message_id": f"msg-{index}@partner.example.org", "source_folder": source_folder}
        for index in range(count)
    ]


def _draft_manifest(
    *,
    count: int = 2,
    expected_count: int | None = None,
    allow_fewer: bool = False,
    account: str | None = None,
    source_folder: str = "INBOX",
) -> dict:
    """Build a genuine draft manifest via the canonical contract helper."""
    manifest: dict = {"mode": "execute", "items": _items(count, source_folder)}
    return add_draft_contract(
        manifest,
        expected_count=count if expected_count is None else expected_count,
        allow_fewer=allow_fewer,
        source_folder=source_folder,
        account=account,
        skip_known=True,
    )


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _stdout_envelope(completed: subprocess.CompletedProcess) -> dict:
    return json.loads(completed.stdout)


class BuildSignedManifestTests(unittest.TestCase):
    """The pure re-signing function applies the canonical contract exactly."""

    def test_resign_recomputes_count_and_approves_review(self) -> None:
        manifest = _draft_manifest(count=2)
        pending_hash = manifest["review"]["execute_request_sha256"]
        # Reviewer edits one item, then candidate_count drifts from len(items).
        manifest["items"][0]["decision"] = {"target_folder": "Projekte/Alpha"}
        manifest["candidate_count"] = 42

        signed, receipt_hash, check = signer.build_signed_manifest(
            manifest,
            reviewed_by="human-reviewer",
            reviewed_at="2026-10-16T12:00:00Z",
        )

        self.assertEqual(2, signed["candidate_count"])
        self.assertEqual(len(signed["items"]), signed["candidate_count"])
        review = signed["review"]
        self.assertEqual({"required", "state", "approval_receipt"}, set(review))
        self.assertIs(True, review["required"])
        self.assertEqual("approved", review["state"])
        self.assertNotIn("execute_request_sha256", review)  # pending key stripped
        receipt = review["approval_receipt"]
        self.assertEqual(RECEIPT_KEYS, set(receipt))
        self.assertEqual("2026-10-16T12:00:00Z", receipt["reviewed_at"])
        self.assertEqual("human-reviewer", receipt["reviewed_by"])
        # The re-signed manifest binds the canonical hash of the edited request.
        self.assertEqual(canonical_execute_request_sha256(signed), receipt_hash)
        self.assertEqual(canonical_execute_request_sha256(signed), receipt["execute_request_sha256"])
        self.assertNotEqual(pending_hash, receipt_hash)
        self.assertIs(True, check["ok"])

    def test_resigned_manifest_passes_validate_execute_contract(self) -> None:
        manifest = _draft_manifest(count=2)
        manifest["items"][1]["subject"] = "Edited by reviewer"
        signed, _hash, _check = signer.build_signed_manifest(
            manifest, reviewed_by="reviewer", reviewed_at="2026-10-16T12:00:00Z"
        )
        result = validate_execute_contract(signed, effective_account=signed.get("account"))
        self.assertIs(True, result["ok"])
        self.assertEqual("reviewed", result["state"])

    def test_injected_clock_controls_reviewed_at(self) -> None:
        manifest = _draft_manifest(count=1, expected_count=1)
        signed, _hash, _check = signer.build_signed_manifest(
            manifest, reviewed_by="reviewer", reviewed_at="2031-01-02T03:04:05Z"
        )
        self.assertEqual("2031-01-02T03:04:05Z", signed["review"]["approval_receipt"]["reviewed_at"])

    def test_missing_reviewed_by_is_rejected(self) -> None:
        manifest = _draft_manifest(count=1, expected_count=1)
        with self.assertRaises(signer.SignManifestError):
            signer.build_signed_manifest(manifest, reviewed_by="   ", reviewed_at="2026-10-16T12:00:00Z")

    def test_missing_trigger_keys_are_rejected(self) -> None:
        broken = {"mode": "execute", "items": _items(1)}
        with self.assertRaises(signer.SignManifestError) as caught:
            signer.build_signed_manifest(broken, reviewed_by="reviewer", reviewed_at="2026-10-16T12:00:00Z")
        self.assertIn("review", caught.exception.details.get("missing", []))

    def test_empty_items_are_rejected(self) -> None:
        manifest = _draft_manifest(count=1, expected_count=1)
        manifest["items"] = []
        with self.assertRaises(signer.SignManifestError):
            signer.build_signed_manifest(manifest, reviewed_by="reviewer", reviewed_at="2026-10-16T12:00:00Z")

    def test_self_check_failure_is_rejected(self) -> None:
        # Two items but expected_count 5 without allow_fewer: self-check fails.
        manifest = _draft_manifest(count=2, expected_count=5, allow_fewer=False)
        with self.assertRaises(signer.SignManifestError):
            signer.build_signed_manifest(manifest, reviewed_by="reviewer", reviewed_at="2026-10-16T12:00:00Z")


class CliTests(unittest.TestCase):
    """The CLI writes the signed manifest atomically and prints one envelope."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name)
        self.manifest_path = self.workspace / "data" / "mail-desk" / "batch-manifest.json"
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_manifest(self, payload: object) -> None:
        self.manifest_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_cli_success_is_machine_readable(self) -> None:
        self._write_manifest(_draft_manifest(count=2))
        completed = _run_cli(
            "data/mail-desk/batch-manifest.json",
            "--reviewed-by", "human-reviewer",
            "--reviewed-at", "2026-10-16T12:00:00Z",
            "--workspace", str(self.workspace),
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        envelope = _stdout_envelope(completed)
        self.assertIs(True, envelope["success"])
        self.assertEqual("mail_desk_sign_manifest", envelope["action"])
        data = envelope["data"]
        self.assertIs(True, data["ok"])
        self.assertEqual(2, data["candidate_count"])
        self.assertEqual("human-reviewer", data["reviewed_by"])
        self.assertEqual("2026-10-16T12:00:00Z", data["reviewed_at"])
        self.assertTrue(str(data["path"]).endswith("batch-manifest.json"))

        on_disk = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(canonical_execute_request_sha256(on_disk), data["execute_request_sha256"])
        result = validate_execute_contract(on_disk, effective_account=on_disk.get("account"))
        self.assertIs(True, result["ok"])

    def test_cli_default_clock_still_emits_reviewed_at(self) -> None:
        self._write_manifest(_draft_manifest(count=1, expected_count=1))
        completed = _run_cli(
            str(self.manifest_path), "--reviewed-by", "reviewer"
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        data = _stdout_envelope(completed)["data"]
        self.assertTrue(data["reviewed_at"])
        on_disk = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(data["reviewed_at"], on_disk["review"]["approval_receipt"]["reviewed_at"])

    def test_cli_missing_trigger_keys_writes_nothing(self) -> None:
        self._write_manifest({"mode": "execute", "items": _items(1)})
        before = self.manifest_path.read_bytes()
        completed = _run_cli(
            str(self.manifest_path), "--reviewed-by", "reviewer",
            "--reviewed-at", "2026-10-16T12:00:00Z",
        )
        self.assertNotEqual(0, completed.returncode)
        envelope = _stdout_envelope(completed)
        self.assertIs(False, envelope["success"])
        self.assertEqual(before, self.manifest_path.read_bytes())

    def test_cli_malformed_json_writes_nothing(self) -> None:
        self.manifest_path.write_text("{ this is not json", encoding="utf-8")
        before = self.manifest_path.read_bytes()
        completed = _run_cli(
            str(self.manifest_path), "--reviewed-by", "reviewer",
            "--reviewed-at", "2026-10-16T12:00:00Z",
        )
        self.assertNotEqual(0, completed.returncode)
        self.assertIs(False, _stdout_envelope(completed)["success"])
        self.assertEqual(before, self.manifest_path.read_bytes())

    def test_cli_non_utf8_manifest_is_clean_error_and_writes_nothing(self) -> None:
        # Invalid bytes must not escape as a traceback: the decode failure is
        # the same load-path fault as unreadable JSON and must yield the same
        # canonical, structured failure envelope, leaving the manifest intact.
        invalid_bytes = b"\xff\xfe\x00A"
        self.manifest_path.write_bytes(invalid_bytes)
        before = self.manifest_path.read_bytes()
        completed = _run_cli(
            str(self.manifest_path), "--reviewed-by", "reviewer",
            "--reviewed-at", "2026-10-16T12:00:00Z",
        )
        self.assertNotEqual(0, completed.returncode)
        self.assertNotIn("Traceback", completed.stderr)
        envelope = _stdout_envelope(completed)
        _validate_envelope(envelope)  # must satisfy the canonical envelope validator
        self.assertEqual(ENVELOPE_KEYS, tuple(envelope))
        self.assertIs(False, envelope["success"])
        self.assertEqual("Failed", envelope["state"])
        self.assertEqual("ManifestError", envelope["error"]["type"])
        self.assertTrue(envelope["error"]["message"])
        self.assertEqual(before, self.manifest_path.read_bytes())

    def test_cli_empty_reviewed_by_writes_nothing(self) -> None:
        self._write_manifest(_draft_manifest(count=1, expected_count=1))
        before = self.manifest_path.read_bytes()
        completed = _run_cli(
            str(self.manifest_path), "--reviewed-by", "   ",
            "--reviewed-at", "2026-10-16T12:00:00Z",
        )
        self.assertNotEqual(0, completed.returncode)
        self.assertIs(False, _stdout_envelope(completed)["success"])
        self.assertEqual(before, self.manifest_path.read_bytes())

    def test_cli_missing_file_is_clean_error(self) -> None:
        completed = _run_cli(
            str(self.workspace / "nope.json"), "--reviewed-by", "reviewer",
            "--reviewed-at", "2026-10-16T12:00:00Z",
        )
        self.assertNotEqual(0, completed.returncode)
        envelope = _stdout_envelope(completed)
        self.assertIs(False, envelope["success"])

    def test_cli_leaves_no_partial_sibling_files(self) -> None:
        self._write_manifest(_draft_manifest(count=2))
        completed = _run_cli(
            str(self.manifest_path), "--reviewed-by", "reviewer",
            "--reviewed-at", "2026-10-16T12:00:00Z",
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        leftovers = sorted(child.name for child in self.manifest_path.parent.iterdir())
        self.assertEqual(["batch-manifest.json"], leftovers, "atomic write must leave no temp sibling")

    def test_cli_write_trap_touches_only_the_manifest(self) -> None:
        sentinels = {
            "data/mail-desk/final-location-index.json": b'{"items": {}}\n',
            "data/mail-desk/action-log.jsonl": b"",
            "memory/references/topics/topics.json": b'{"topics": []}\n',
            ".agents/mail-desk-backend.json": b'{"schema_version": 1}\n',
        }
        for rel, blob in sentinels.items():
            path = self.workspace / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(blob)
        self._write_manifest(_draft_manifest(count=2))

        def snapshot() -> dict[str, bytes]:
            return {
                str(path.relative_to(self.workspace)).replace("\\", "/"): path.read_bytes()
                for path in self.workspace.rglob("*")
                if path.is_file()
            }

        before = snapshot()
        completed = _run_cli(
            str(self.manifest_path), "--reviewed-by", "reviewer",
            "--reviewed-at", "2026-10-16T12:00:00Z",
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        after = snapshot()

        manifest_rel = "data/mail-desk/batch-manifest.json"
        self.assertEqual(set(before), set(after), "helper must not create or remove files")
        for rel, blob in before.items():
            if rel == manifest_rel:
                continue
            self.assertEqual(blob, after[rel], f"helper must not touch {rel}")
        self.assertNotEqual(before[manifest_rel], after[manifest_rel])


class AtomicAndHygieneTests(unittest.TestCase):
    """The helper reuses the workspace's atomic writer and prints no prose."""

    def test_reuses_canonical_atomic_writer(self) -> None:
        self.assertIs(signer.atomic_write_json, core_common.atomic_write_json)

    def test_canonical_hash_helper_is_the_contract_owner(self) -> None:
        from core.batch_contract import canonical_execute_request_sha256 as canonical
        self.assertIs(signer.canonical_execute_request_sha256, canonical)

    def test_no_print_prose_in_sources(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("print(", source, "helper emits only canonical JSON envelopes")


if __name__ == "__main__":
    unittest.main()
