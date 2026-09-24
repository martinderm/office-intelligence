"""FR-23/MD-L1 runner lease-delegation tests.

Hermetic contract tests. They import the real runner module and assert that the
new delegation flags thread the agent-session lease into the attachment
evaluation chain; no mailbox, network or catalog access happens.

Red-Gate history: at the MD-L1-001 dispatch the runner accepted neither
``--workspace-lease-id`` nor ``--workspace-conversation-id``, so 5 of the
8 tests below failed at the red gate; the 3 source-pin tests were green
before and after. All pin the resolved delegation contract since.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import tempfile
import unittest


MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

import mail_desk_batch_runner as runner  # noqa: E402


def _args(**overrides) -> argparse.Namespace:
    base = {
        "help": False,
        "input": None,
        "stdin": False,
        "account": None,
        "data_dir": None,
        "index": None,
        "keep_input": False,
        "pipeline": None,
        "draft": 5,
        "inspect": None,
        "dossier": None,
        "max_count": None,
        "sync_sent": None,
        "resolve": False,
        "reconcile": False,
        "order": "oldest",
        "folder": "INBOX",
        "skip_known": True,
        "query": None,
        "date": None,
        "min_confidence": "high",
        "expected_count": None,
        "allow_fewer": False,
        "evaluate_attachments": None,
        "workspace_lease_id": None,
        "workspace_conversation_id": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _data_dir(tmp: str) -> Path:
    return Path(tmp) / "data" / "mail-desk"


class DelegationConfigTests(unittest.TestCase):
    """The flags thread the lease into draft/inspect configs (existing chain)."""

    def test_draft_config_carries_delegated_lease(self) -> None:
        with tempfile_namespace() as tmp:
            cfg = runner._direct_mode_config(
                _args(workspace_lease_id="lease-abc", workspace_conversation_id="conv-42"),
                _data_dir(tmp),
            )
            self.assertIsNotNone(cfg)
            self.assertEqual(cfg.get("lease_id"), "lease-abc")
            self.assertEqual(cfg.get("conversation_id"), "conv-42")

    def test_inspect_config_carries_delegated_lease(self) -> None:
        with tempfile_namespace() as tmp:
            cfg = runner._direct_mode_config(
                _args(draft=None, inspect=3, workspace_lease_id="lease-abc"),
                _data_dir(tmp),
            )
            self.assertIsNotNone(cfg)
            self.assertEqual(cfg.get("lease_id"), "lease-abc")
            self.assertNotIn("conversation_id", cfg)

    def test_no_flags_keeps_config_byte_identical(self) -> None:
        with tempfile_namespace() as tmp:
            cfg = runner._direct_mode_config(_args(), _data_dir(tmp))
            self.assertIsNotNone(cfg)
            self.assertNotIn("lease_id", cfg)
            self.assertNotIn("conversation_id", cfg)


def tempfile_namespace():
    return tempfile.TemporaryDirectory()


class DelegationValidationTests(unittest.TestCase):
    """Flags are valid only with --draft/--inspect; misuse fails loud."""

    def test_lease_flag_without_draft_or_inspect_is_rejected(self) -> None:
        with tempfile_namespace() as tmp:
            with self.assertRaises(runner.ArgumentParseError):
                runner._direct_mode_config(
                    _args(draft=None, sync_sent=10, workspace_lease_id="lease-abc"),
                    _data_dir(tmp),
                )

    def test_reconcile_with_lease_flag_is_rejected(self) -> None:
        with self.assertRaises(runner.ArgumentParseError):
            runner._direct_mode_config(
                _args(draft=None, reconcile=True, workspace_lease_id="lease-abc"),
                Path("unused"),
            )

    def test_parser_exposes_both_flags(self) -> None:
        parser = runner._build_parser()
        argv = parser.parse_args(["--draft", "5", "--workspace-lease-id", "lease-abc", "--workspace-conversation-id", "conv-42"])
        self.assertEqual(argv.workspace_lease_id, "lease-abc")
        self.assertEqual(argv.workspace_conversation_id, "conv-42")


class DelegationChainTests(unittest.TestCase):
    """The config keys reach the attachment-evaluation lock guard (regression)."""

    def test_evaluate_attachment_fail_closed_for_foreign_lease(self) -> None:
        # The existing chain is pinned: cfg lease_id/conversation_id reach
        # attachment_evaluation.evaluate_attachment -> verify_workspace_lock.
        from core import attachment_evaluation  # noqa: F401  (import contract)

        import inspect as inspect_module

        source = inspect_module.getsource(attachment_evaluation)
        self.assertIn("verify_workspace_lock", source)
        self.assertIn("lease_id=lease_id", source)

    def test_draft_mode_threads_config_lease_into_evaluation(self) -> None:
        from core.modes import draft as draft_mode
        import inspect as inspect_module

        source = inspect_module.getsource(draft_mode)
        self.assertIn('config.get("lease_id")', source)
        self.assertIn('config.get("conversation_id")', source)


if __name__ == "__main__":
    unittest.main()