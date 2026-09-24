"""Failing tests for the batch-CLI wrapper (mail_desk_batch_cli.py).

Pins the harness-friendly wrapper contract: canonical summary envelopes, the
``--reconcile`` flag threading, neutral account handling (no workspace-specific
default; the runner auto-binds from the workspace backend file), and tmp-output
persistence hygiene.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

import mail_desk_batch_cli as batch_cli  # noqa: E402


def _base_args(**overrides):
    defaults = {
        "mode": "draft",
        "count": 10,
        "input": "data/mail-desk/batch-reconcile.json",
        "account": None,
        "data_dir": "data/mail-desk",
        "workspace": ".",
        "timeout": 900,
        "keep": 10,
    }
    defaults.update(overrides)
    ns = type("Args", (), {})()
    for key, value in defaults.items():
        setattr(ns, key, value)
    return ns


class BuildArgsTests(unittest.TestCase):
    """The wrapper threads the right runner flags per mode."""

    def test_reconcile_mode_passes_the_reconcile_flag(self) -> None:
        args = _base_args(mode="reconcile")
        argv = batch_cli._build_args(args, Path("/ws"))
        joined = " ".join(argv)
        # Runner: read-only reconcile is invoked via the --reconcile flag; the
        # reconcile recovery journal is discovered, no --input manifest exists.
        self.assertIn("--reconcile", argv, f"--reconcile flag missing in runner args: {argv}")
        self.assertNotIn("--input", argv, "reconcile reads the recovery journal; --input is invalid")
        self.assertIn("mail_desk_batch_runner.py", argv)

    def test_draft_mode_passes_draft_count(self) -> None:
        argv = batch_cli._build_args(_base_args(mode="draft", count=7), Path("/ws"))
        joined = " ".join(argv)
        self.assertIn("--draft 7", joined)
        self.assertIn("--data-dir", joined)

    def test_execute_mode_passes_keep_input(self) -> None:
        argv = batch_cli._build_args(_base_args(mode="execute"), Path("/ws"))
        self.assertIn("--keep-input", argv)
        self.assertNotIn("--draft", argv)

    def test_no_default_account_is_threaded_when_unspecified(self) -> None:
        argv = batch_cli._build_args(_base_args(mode="execute", account=None), Path("/ws"))
        self.assertNotIn("--account", argv, "an unspecified account must not be threaded (runner auto-binds)")

    def test_explicit_account_is_threaded(self) -> None:
        argv = batch_cli._build_args(_base_args(mode="execute", account="OTHER-ACC"), Path("/ws"))
        joined = " ".join(argv)
        self.assertIn("--account", joined)
        self.assertIn("OTHER-ACC", joined)


class ResolveAccountTests(unittest.TestCase):
    """Account derivation: unset means None (runner auto-binds); never a workspace literal."""

    def test_unset_account_resolves_to_none(self) -> None:
        self.assertIsNone(batch_cli._resolve_account(_base_args(mode="draft", account=None)))

    def test_explicit_account_is_returned(self) -> None:
        self.assertEqual(batch_cli._resolve_account(_base_args(mode="draft", account="X")), "X")

    def test_no_workspace_specific_default_literal_exists(self) -> None:
        source = (Path(batch_cli.__file__).read_text(encoding="utf-8"))
        for literal in ("BOKU-MARTIN", "BOKU", "martin"):
            self.assertNotIn(literal, source, f"workspace-specific default literal {literal!r} in bundle script")


class KeepParameterTests(unittest.TestCase):
    """tmp/mail-batch persistence hygiene: --keep N retains the newest N pairs."""

    def test_build_args_threads_keep(self) -> None:
        argv = batch_cli._build_args(_base_args(mode="execute", keep=5), Path("/ws"))
        self.assertIn("--keep 5", argv)

    def test_prune_output_dir_keeps_newest_pairs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            # 4 envelope/progress pairs, timestamps increasing (pair i = files i & 2026010{i})
            for i in range(4):
                (run_dir / f"draft-2026010{i}-000000-envelope.json").write_text("{}", encoding="utf-8")
                (run_dir / f"{i}-progress.log").write_text("p", encoding="utf-8")
            batch_cli._prune_output_dir(run_dir, keep=2)
            remaining = sorted(p.name for p in run_dir.iterdir())
            # The two newest pairs (i=2, i=3) survive; the two oldest (i=0, i=1) are gone.
            self.assertEqual(len(remaining), 4, f"keep=2 must retain 2 pairs (4 files), got {remaining}")
            self.assertFalse(any(name.startswith("0-") or name.startswith("1-") for name in remaining), "two oldest progress files pruned")
            self.assertFalse(any("20260100-" in name or "20260101-" in name for name in remaining), "two oldest envelopes pruned")
            self.assertTrue(any("20260102-" in name and name.endswith("envelope.json") for name in remaining), "newest envelopes kept")
            self.assertTrue(any("3-progress.log" in name for name in remaining), "newest progress kept")


class ErrorEnvelopeTests(unittest.TestCase):
    """Errors use the canonical envelope path, never bare SystemExit."""

    def test_module_has_no_bare_systemexit(self) -> None:
        source = batch_cli.__doc__ and Path(batch_cli.__file__).read_text(encoding="utf-8")
        self.assertNotIn("raise SystemExit", source, "wrapper errors must emit canonical envelopes")


if __name__ == "__main__":
    unittest.main()