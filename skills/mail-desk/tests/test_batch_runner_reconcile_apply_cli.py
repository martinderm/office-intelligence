"""Issue #65: reconcile CLI ``--apply`` opt-in and approval receipt.

``--reconcile`` alone stays read-only.  ``--apply`` is valid only together with
``--reconcile`` and produces an explicit CLI approval receipt; ``--approved-by``
only refines that receipt.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sys
import unittest

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

import mail_desk_batch_runner as runner  # noqa: E402


class ReconcileApplyCliTests(unittest.TestCase):
    def _config(self, *arguments: str) -> dict:
        args = runner._build_parser().parse_args(list(arguments))
        return runner._direct_mode_config(args, Path("data/mail-desk"))

    def test_reconcile_alone_stays_read_only_without_approval(self) -> None:
        config = self._config("--reconcile")

        self.assertEqual("reconcile", config["mode"])
        self.assertFalse(config["apply_local_repairs"])
        self.assertNotIn("approval", config)
        self.assertTrue(config["check_folders"])

    def test_apply_requires_an_approved_cli_receipt(self) -> None:
        config = self._config("--reconcile", "--apply")

        self.assertTrue(config["apply_local_repairs"])
        self.assertTrue(config["check_folders"])
        approval = config["approval"]
        self.assertEqual("approved", approval["state"])
        self.assertEqual("cli-operator", approval["reviewed_by"])
        self.assertIsInstance(datetime.fromisoformat(approval["reviewed_at"]), datetime)

    def test_approved_by_lands_in_the_receipt(self) -> None:
        config = self._config("--reconcile", "--apply", "--approved-by", "reviewer-a")

        self.assertEqual("reviewer-a", config["approval"]["reviewed_by"])

    def test_apply_without_reconcile_is_an_argument_error(self) -> None:
        for arguments in (("--apply",), ("--draft", "1", "--apply")):
            with self.subTest(arguments=arguments), self.assertRaisesRegex(
                runner.ArgumentParseError, "--apply requires --reconcile",
            ):
                self._config(*arguments)

    def test_approved_by_without_apply_is_an_argument_error(self) -> None:
        with self.assertRaisesRegex(runner.ArgumentParseError, "--approved-by requires --apply"):
            self._config("--reconcile", "--approved-by", "reviewer-a")


if __name__ == "__main__":
    unittest.main()
