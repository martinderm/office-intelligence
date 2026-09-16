"""TDD hermetic contract tests for FR-11 MD-Q1: Quarantine Git hygiene and integration recommendation."""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest


MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
CLI_OPERATIONS_DOC = MAIL_DESK_ROOT / "references" / "cli-operations.md"
REPO_ROOT = Path(__file__).resolve().parents[3]


def extract_documented_gitignore_blocks(doc_path: Path | str | None = None) -> list[str]:
    """Extract all recommended gitignore blocks from cli-operations.md."""
    path = Path(doc_path or CLI_OPERATIONS_DOC)
    if not path.is_file():
        raise FileNotFoundError(f"Documentation file not found at {path}")
    text = path.read_text(encoding="utf-8")
    blocks = re.findall(r"```gitignore\s*\n(.*?)\n```", text, re.DOTALL)
    if not blocks:
        raise ValueError(f"Could not find any ```gitignore blocks in {path}")
    return [b.strip() for b in blocks]


def find_tracked_quarantine_files(repo_path: Path | str) -> list[str]:
    """Check git index for any accidentally tracked quarantine files under data/mail-desk/attachments/."""
    cmd = ["git", "ls-files", "data/mail-desk/attachments/", "data/mail-desk/attachments/**", "**/attachments/**", "**/.quarantine-inventory.json"]
    proc = subprocess.run(
        cmd,
        cwd=str(repo_path),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git ls-files failed in {repo_path}: {proc.stderr}")
    tracked = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    quarantine_tracked = [
        f for f in tracked
        if (
            f.startswith("data/mail-desk/attachments/")
            or f.endswith(".quarantine-inventory.json")
            or f.endswith(".quarantine-inventory.lock")
        )
    ]
    return quarantine_tracked


def assert_no_tracked_quarantine_files(repo_path: Path | str) -> None:
    """Fail-closed stop condition: raise RuntimeError if any quarantine files are tracked in git index."""
    tracked = find_tracked_quarantine_files(repo_path)
    if tracked:
        raise RuntimeError(
            f"STOP CONDITION: accidentally tracked quarantine files found in git index: {tracked}. "
            "These must never be committed and require human resolution."
        )


class MailDeskAttachmentQuarantineMDQ1Tests(unittest.TestCase):
    """Test suite for FR-11 / MD-Q1 quarantine git hygiene and contract verification."""

    def test_cli_operations_documentation_recommendation_semantics(self) -> None:
        """Verify cli-operations.md documents integration recommendations, not universal duty, and all invariants."""
        self.assertTrue(CLI_OPERATIONS_DOC.is_file(), f"CLI operations doc must exist at {CLI_OPERATIONS_DOC}")
        text = CLI_OPERATIONS_DOC.read_text(encoding="utf-8")

        # 1. Section exists and is framed as an integration recommendation
        self.assertIn("Workspace-Integration: Attachment-Quarantäne", text)
        self.assertTrue(
            "Empfehlung" in text or "Integrationsempfehlung" in text or "Integrationsvorschlag" in text,
            "Section must be framed as a recommendation, not universal duty",
        )

        # 2. Extract blocks: should have full block for new workspaces, existing base block, and single rule
        blocks = extract_documented_gitignore_blocks(CLI_OPERATIONS_DOC)
        self.assertGreaterEqual(len(blocks), 2, "Must contain at least full block and single rule")

        full_block = blocks[0]
        full_lines = [l.strip() for l in full_block.splitlines() if l.strip()]
        expected_full_lines = [
            "data/*",
            "!data/mail-desk/",
            "!data/mail-desk/**",
            "/data/mail-desk/attachments/",
        ]
        self.assertEqual(expected_full_lines, full_lines)

        # Single rule for existing workspaces
        self.assertTrue(
            any(b.strip() == "/data/mail-desk/attachments/" for b in blocks),
            "Must document single rule '/data/mail-desk/attachments/' for existing workspaces",
        )

        # 3. Specific explanations:
        # a) Check existing rules before adoption; do not replace existing rules
        self.assertTrue(
            "bestehende Regeln prüfen" in text or "bestehende Workspace-Regeln" in text,
            "Must document checking existing rules before adoption",
        )
        self.assertTrue(
            "keine vorhandenen Regeln ersetzen" in text or "nicht unbesehen ersetzt" in text,
            "Must state not to replace existing rules",
        )

        # b) Skill does not autonomously mutate consumer .gitignore
        self.assertTrue(
            "nie autonom" in text or "nicht autonom" in text,
            "Must state that the skill does not autonomously change consumer .gitignore",
        )

        # c) Fluechtige Laufzeitdaten vs. versionierbare Metadaten
        self.assertIn(".quarantine-inventory.json", text)
        self.assertIn(".quarantine-inventory.lock", text)
        self.assertIn("attachment-quarantine-index.json", text)
        self.assertIn("action-log.jsonl", text)

        # d) Stop condition
        self.assertIn("Stop-Bedingung", text)

    def test_hermetic_git_repository_full_integration_block(self) -> None:
        """Verify hermetically that the full recommended integration block ignores quarantine files while keeping metadata trackable."""
        blocks = extract_documented_gitignore_blocks(CLI_OPERATIONS_DOC)
        full_block = blocks[0]

        with tempfile.TemporaryDirectory() as td:
            repo_dir = Path(td)
            subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "MD-Q1 New Workspace"], cwd=repo_dir, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "new@example.org"], cwd=repo_dir, check=True, capture_output=True)

            # Write .gitignore containing the full block
            (repo_dir / ".gitignore").write_text(full_block + "\n", encoding="utf-8")

            # 1. Populate quarantine attachment subtree: data/mail-desk/attachments/<run_id>/
            run_dir = repo_dir / "data" / "mail-desk" / "attachments" / "run_20260916_test_q1"
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "document.pdf").write_bytes(b"%PDF-1.4 sample content")
            (run_dir / "executable.bin").write_bytes(b"\x00\x01\x02\x03 binary bytes")
            (run_dir / ".quarantine-inventory.json").write_text('{"schema_version": 1, "messages": {}}', encoding="utf-8")
            (run_dir / ".quarantine-inventory.lock").write_text("lock", encoding="utf-8")
            (run_dir / ".document.pdf.4a8b.tmp").write_bytes(b"temp sibling content")

            derivatives_dir = run_dir / "derivatives"
            derivatives_dir.mkdir(parents=True, exist_ok=True)
            (derivatives_dir / "extracted_text.txt").write_text("derivative text", encoding="utf-8")

            # 2. Populate trackable metadata outside attachments subtree
            mail_desk_dir = repo_dir / "data" / "mail-desk"
            (mail_desk_dir / "action-log.jsonl").write_text('{"action": "test"}\n', encoding="utf-8")
            (mail_desk_dir / "final-location-index.json").write_text('{"schema_version": 1, "messages": {}}', encoding="utf-8")
            (mail_desk_dir / "batch-manifest.json").write_text('{"operations": []}', encoding="utf-8")
            (mail_desk_dir / "batch-recovery-journal.json").write_text('{"recovery": []}', encoding="utf-8")
            (mail_desk_dir / "runner-progress.json").write_text('{"status": "idle"}', encoding="utf-8")
            (mail_desk_dir / "replies-needed.jsonl").write_text('{"mid": "1"}\n', encoding="utf-8")
            (mail_desk_dir / "pending-review.jsonl").write_text('{"mid": "2"}\n', encoding="utf-8")
            (mail_desk_dir / "attachment-quarantine-index.json").write_text('{"schema_version": 1}', encoding="utf-8")

            # 3. Check git status
            status_proc = subprocess.run(
                ["git", "status", "--porcelain", "-uall"],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                check=True,
            )
            status_lines = [l.strip() for l in status_proc.stdout.splitlines() if l.strip()]

            # Quarantine files must NOT appear anywhere in git status output
            for sl in status_lines:
                self.assertNotIn("data/mail-desk/attachments", sl)
                self.assertNotIn(".quarantine-inventory", sl)

            # Metadata files outside attachments MUST appear in git status as untracked (??)
            expected_untracked = [
                ".gitignore",
                "data/mail-desk/action-log.jsonl",
                "data/mail-desk/final-location-index.json",
                "data/mail-desk/batch-manifest.json",
                "data/mail-desk/attachment-quarantine-index.json",
            ]
            for exp in expected_untracked:
                found = any(exp in sl for sl in status_lines)
                self.assertTrue(found, f"Expected trackable file '{exp}' to appear in git status, but got: {status_lines}")

            # 4. Verify check-ignore behavior on quarantine files
            quarantine_files = [
                "data/mail-desk/attachments/run_20260916_test_q1/document.pdf",
                "data/mail-desk/attachments/run_20260916_test_q1/executable.bin",
                "data/mail-desk/attachments/run_20260916_test_q1/.quarantine-inventory.json",
                "data/mail-desk/attachments/run_20260916_test_q1/.quarantine-inventory.lock",
                "data/mail-desk/attachments/run_20260916_test_q1/.document.pdf.4a8b.tmp",
                "data/mail-desk/attachments/run_20260916_test_q1/derivatives/extracted_text.txt",
            ]
            for qf in quarantine_files:
                r = subprocess.run(["git", "check-ignore", "-v", qf], cwd=repo_dir, capture_output=True, text=True, check=False)
                self.assertEqual(0, r.returncode, f"Quarantine file '{qf}' must be ignored")

    def test_hermetic_git_repository_existing_consumer_block_appended_rule(self) -> None:
        """Verify hermetically that in an existing consumer workspace with data/* and !data/mail-desk/**, appending ONLY the attachment rule works."""
        with tempfile.TemporaryDirectory() as td:
            repo_dir = Path(td)
            subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "MD-Q1 Existing Workspace"], cwd=repo_dir, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "existing@example.org"], cwd=repo_dir, check=True, capture_output=True)

            # 1. Existing consumer .gitignore with prior mail-desk negation chain
            existing_gitignore = (
                "# Existing consumer workspace rules\n"
                "node_modules/\n"
                "data/*\n"
                "!data/mail-desk/\n"
                "!data/mail-desk/**\n"
            )
            (repo_dir / ".gitignore").write_text(existing_gitignore, encoding="utf-8")

            # Create test files
            run_dir = repo_dir / "data" / "mail-desk" / "attachments" / "run_20260916_existing"
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "document.pdf").write_bytes(b"%PDF-1.4 sample")
            (run_dir / ".quarantine-inventory.json").write_text('{"schema_version": 1}', encoding="utf-8")

            mail_desk_dir = repo_dir / "data" / "mail-desk"
            (mail_desk_dir / "action-log.jsonl").write_text('{"action": "test"}\n', encoding="utf-8")
            (mail_desk_dir / "attachment-quarantine-index.json").write_text('{"schema_version": 1}', encoding="utf-8")

            # Before appending the attachment rule: attachments/ is untracked
            status_before = subprocess.run(
                ["git", "status", "--porcelain", "-uall"],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertTrue(
                any("data/mail-desk/attachments" in l for l in status_before.stdout.splitlines()),
                "Attachments should be untracked before appending /data/mail-desk/attachments/",
            )

            # 2. Append ONLY the single recommended rule: /data/mail-desk/attachments/
            updated_gitignore = existing_gitignore + "/data/mail-desk/attachments/\n"
            (repo_dir / ".gitignore").write_text(updated_gitignore, encoding="utf-8")

            # After appending: attachments/ is completely ignored
            status_after = subprocess.run(
                ["git", "status", "--porcelain", "-uall"],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                check=True,
            )
            after_lines = [l.strip() for l in status_after.stdout.splitlines() if l.strip()]

            # Zero quarantine files in git status
            for l in after_lines:
                self.assertNotIn("data/mail-desk/attachments", l)
                self.assertNotIn(".quarantine-inventory", l)

            # Metadata files outside attachments remain untracked / trackable
            self.assertTrue(any("action-log.jsonl" in l for l in after_lines))
            self.assertTrue(any("attachment-quarantine-index.json" in l for l in after_lines))

            # Staging metadata succeeds without staging attachments
            subprocess.run(["git", "add", "data/mail-desk/attachment-quarantine-index.json"], cwd=repo_dir, check=True)
            cached_proc = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=repo_dir, capture_output=True, text=True, check=True)
            staged = cached_proc.stdout.splitlines()
            self.assertIn("data/mail-desk/attachment-quarantine-index.json", staged)

    def test_real_workspace_contains_no_tracked_quarantine_files(self) -> None:
        """Verify the active office-intelligence git repository contains zero tracked quarantine files."""
        tracked = find_tracked_quarantine_files(REPO_ROOT)
        self.assertEqual(
            [],
            tracked,
            f"Active repository {REPO_ROOT} contains tracked quarantine files: {tracked}",
        )

    def test_tracked_quarantine_files_stop_condition_behavior(self) -> None:
        """Verify that assert_no_tracked_quarantine_files raises RuntimeError if files are in index."""
        with tempfile.TemporaryDirectory() as td:
            repo_dir = Path(td)
            subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_dir, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.org"], cwd=repo_dir, check=True, capture_output=True)

            assert_no_tracked_quarantine_files(repo_dir)

            # Force-add a quarantine file
            qfile = repo_dir / "data" / "mail-desk" / "attachments" / "run_bad" / "bad.pdf"
            qfile.parent.mkdir(parents=True, exist_ok=True)
            qfile.write_bytes(b"bad")
            subprocess.run(["git", "add", "-f", str(qfile)], cwd=repo_dir, check=True, capture_output=True)

            with self.assertRaises(RuntimeError) as ctx:
                assert_no_tracked_quarantine_files(repo_dir)
            self.assertIn("STOP CONDITION", str(ctx.exception))
            self.assertIn("bad.pdf", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
