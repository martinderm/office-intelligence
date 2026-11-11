"""Regression tests for the batch-CLI wrapper (mail_desk_batch_cli.py).

Pins the harness-friendly wrapper contract: canonical summary envelopes, the
``--reconcile`` flag threading, neutral account handling (no workspace-specific
default; the runner auto-binds from the workspace backend file), and tmp-output
persistence hygiene.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

import mail_desk_batch_cli as batch_cli  # noqa: E402


def _make_dir_link(link: Path, target: Path) -> bool:
    """Create a real directory link (symlink, or Windows junction) if allowed."""
    try:
        link.symlink_to(target, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        if os.name == "nt":
            try:
                subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                    check=True, capture_output=True,
                )
                return True
            except (OSError, subprocess.CalledProcessError):
                return False
        return False


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
        "workspace_lease_id": None,
        "workspace_conversation_id": None,
        "since": None,
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

    def test_sync_sent_uses_only_existing_direct_mode(self):
        argv = batch_cli._build_args(_base_args(mode="sync-sent", count=42), Path("/ws"))
        self.assertEqual(argv[argv.index("--sync-sent") + 1], "42")
        for flag in ("--reconcile", "--input", "--folder", "--skip-known", "--draft"):
            self.assertNotIn(flag, argv)

    def test_sync_sent_since_becomes_a_stdin_manifest_without_direct_mode(self):
        args = _base_args(
            mode="sync-sent", count=42, since="2026-09-01", account="ACC",
        )
        argv = batch_cli._build_args(args, Path("/ws"))
        self.assertNotIn("--sync-sent", argv)
        self.assertEqual("ACC", argv[argv.index("--account") + 1])
        self.assertEqual({
            "mode": "sync_sent", "folder": "Sent Items", "count": 42,
            "since": "2026-09-01", "account": "ACC",
        }, batch_cli._sync_sent_manifest(args))

    def test_exact_delegation_only_for_draft(self):
        for mode in ("draft", "execute", "reconcile", "sync-sent"):
            argv = batch_cli._build_args(_base_args(mode=mode, workspace_lease_id="lease exact",
                                         workspace_conversation_id="conversation exact"), Path("/ws"))
            for flag, value in (("--workspace-lease-id", "lease exact"),
                                ("--workspace-conversation-id", "conversation exact")):
                if mode == "draft":
                    self.assertEqual(argv[argv.index(flag) + 1], value)
                else:
                    self.assertNotIn(flag, argv)

    def test_no_implicit_delegation(self):
        argv = batch_cli._build_args(_base_args(), Path("/ws"))
        self.assertNotIn("--workspace-lease-id", argv)
        self.assertNotIn("--workspace-conversation-id", argv)

    def test_unknown_mode_is_not_reconcile(self):
        with self.assertRaises(ValueError):
            batch_cli._build_args(_base_args(mode="typo"), Path("/ws"))

    def test_no_default_account_is_threaded_when_unspecified(self) -> None:
        argv = batch_cli._build_args(_base_args(mode="execute", account=None), Path("/ws"))
        self.assertNotIn("--account", argv, "an unspecified account must not be threaded (runner auto-binds)")

    def test_explicit_account_is_threaded(self) -> None:
        argv = batch_cli._build_args(_base_args(mode="execute", account="OTHER-ACC"), Path("/ws"))
        joined = " ".join(argv)
        self.assertIn("--account", joined)
        self.assertIn("OTHER-ACC", joined)


class SummaryAliasDetailsTests(unittest.TestCase):
    def test_summary_keeps_handoff_file_when_alias_details_are_present(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            envelope_path = root / "sync-sent-envelope.json"
            progress_path = root / "sync-sent-progress.log"
            envelope_path.write_text(json.dumps({
                "success": False,
                "state": "Failed",
                "message": "static alias",
                "data": {
                    "reason_code": "sent_index_alias_invalid",
                    "handoff": {
                        "state": "pending",
                        "file": str(root / "data" / "mail-desk" / "new-sent-items.json"),
                        "alias_file": "sent-index.jsonl",
                        "alias_kind": "hardlink",
                    },
                },
            }), encoding="utf-8")
            progress_path.write_text("", encoding="utf-8")
            summary = batch_cli._summary(
                _base_args(mode="sync-sent"), envelope_path, progress_path,
                subprocess.CompletedProcess(["runner"], 1),
            )
        self.assertEqual(
            str(root / "data" / "mail-desk" / "new-sent-items.json"),
            summary["handoff"]["file"],
        )
        self.assertNotIn("alias_file", summary["handoff"])

    def test_summary_carries_transport_stats_counters_only(self) -> None:
        stats = {
            "process_starts": 3,
            "retries": 1,
            "errors": 0,
            "consecutive_transport_errors": 0,
            "start_budget": 10000,
            "blocked": False,
            "block_reason": None,
            "commands": {"envelope list": {"starts": 2, "retries": 0, "errors": 0}},
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            envelope_path = root / "draft-envelope.json"
            progress_path = root / "draft-progress.log"
            envelope_path.write_text(json.dumps({
                "success": True,
                "state": "Completed",
                "message": "drafted",
                "data": {"transport_stats": stats},
            }), encoding="utf-8")
            progress_path.write_text("", encoding="utf-8")
            summary = batch_cli._summary(
                _base_args(mode="draft"), envelope_path, progress_path,
                subprocess.CompletedProcess(["runner"], 0),
            )
        self.assertEqual(stats, summary["transport_stats"])
        self.assertNotIn("message_ids", json.dumps(summary["transport_stats"]))

    def test_summary_ignores_non_mapping_transport_stats(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            envelope_path = root / "draft-envelope.json"
            progress_path = root / "draft-progress.log"
            envelope_path.write_text(json.dumps({
                "success": True,
                "state": "Completed",
                "message": "drafted",
                "data": {"transport_stats": ["not", "a", "mapping"]},
            }), encoding="utf-8")
            progress_path.write_text("", encoding="utf-8")
            summary = batch_cli._summary(
                _base_args(mode="draft"), envelope_path, progress_path,
                subprocess.CompletedProcess(["runner"], 0),
            )
        self.assertNotIn("transport_stats", summary)


class ReconcileApplyWrapperTests(unittest.TestCase):
    """Issue #65: ``reconcile --apply [--approved-by NAME]`` opt-in threading."""

    def test_reconcile_apply_threads_apply_and_approved_by(self) -> None:
        argv = batch_cli._build_args(
            _base_args(mode="reconcile", apply=True, approved_by="reviewer-a"), Path("/ws"),
        )
        self.assertIn("--reconcile", argv)
        self.assertIn("--apply", argv)
        self.assertEqual("reviewer-a", argv[argv.index("--approved-by") + 1])

    def test_reconcile_without_apply_is_unchanged(self) -> None:
        argv = batch_cli._build_args(
            _base_args(mode="reconcile", apply=False, approved_by=None), Path("/ws"),
        )
        self.assertNotIn("--apply", argv)
        self.assertNotIn("--approved-by", argv)

    def test_apply_is_rejected_outside_reconcile(self) -> None:
        for mode in ("draft", "execute", "sync-sent"):
            with self.subTest(mode=mode), patch.object(
                sys, "argv", ["batch-cli", mode, "--apply"],
            ), contextlib.redirect_stderr(io.StringIO()) as stderr, self.assertRaises(SystemExit) as raised:
                batch_cli.main()
            self.assertEqual(2, raised.exception.code)
            self.assertIn("--apply requires the reconcile mode", stderr.getvalue())

    def test_approved_by_without_apply_is_rejected(self) -> None:
        with patch.object(
            sys, "argv", ["batch-cli", "reconcile", "--approved-by", "reviewer-a"],
        ), contextlib.redirect_stderr(io.StringIO()) as stderr, self.assertRaises(SystemExit) as raised:
            batch_cli.main()
        self.assertEqual(2, raised.exception.code)
        self.assertIn("--approved-by requires --apply", stderr.getvalue())


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


class MainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve()

    def call(self, mode="sync-sent", extra=()):
        stream = io.StringIO()
        argv = ["batch-cli", mode, "--workspace", str(self.workspace), *extra]
        with patch.object(sys, "argv", argv), contextlib.redirect_stdout(stream):
            code = batch_cli.main()
        return code, json.loads(stream.getvalue())

    def runner(self, *, success=True, code=0):
        def run(command, **kwargs):
            self.command = command
            self.child_options = kwargs
            kwargs["stdout"].write(json.dumps({"success": success, "state": "Completed" if success else "Failed",
                "message": "synthetic", "data": {"new_entries_indexed": 2, "envelopes_examined": 8,
                                                  "date_windows_synced": 1}}).encode())
            kwargs["stderr"].write(b"synthetic progress\n")
            return subprocess.CompletedProcess(command, code)
        return run

    def test_sync_sent_defaults_summary_and_persistence(self):
        with patch.object(batch_cli, "verify_workspace_lock") as guard, \
                patch.object(batch_cli.subprocess, "run", side_effect=self.runner()):
            code, result = self.call(extra=("--workspace-lease-id", "mine"))
        guard.assert_called_once_with(self.workspace, lease_id="mine", conversation_id=None)
        self.assertEqual(code, 0)
        self.assertTrue(result["success"])
        self.assertEqual(result["data"]["new_entries_indexed"], 2)
        self.assertEqual(self.command[self.command.index("--sync-sent") + 1], "150")
        self.assertNotIn("--workspace-lease-id", self.command)
        self.assertNotIn("--keep 10", self.command)
        self.assertEqual(self.child_options["cwd"], str(self.workspace))
        self.assertEqual(
            self.command[self.command.index("--data-dir") + 1],
            str((self.workspace / "data" / "mail-desk").resolve()),
        )
        self.assertEqual(
            Path(result["data"]["envelope_file"]).resolve().parent,
            (self.workspace / "tmp" / "mail-batch").resolve(),
        )
        self.assertTrue(Path(result["data"]["envelope_file"]).is_file())
        self.assertEqual(Path(result["data"]["progress_file"]).read_text(), "synthetic progress\n")

    def test_data_dir_outside_the_verified_workspace_is_rejected(self):
        outside = self.workspace.parent / (self.workspace.name + "-outside")
        outside.mkdir()
        self.addCleanup(lambda: shutil.rmtree(outside, ignore_errors=True))
        with patch.object(batch_cli, "verify_workspace_lock") as guard, \
                patch.object(batch_cli.subprocess, "run") as run:
            code, result = self.call(extra=("--data-dir", str(outside / "data" / "mail-desk")))
        self.assertEqual(2, code)
        self.assertEqual("WorkspacePathError", result["error"]["type"])
        guard.assert_not_called()
        run.assert_not_called()
        self.assertEqual(list(self.workspace.iterdir()), [])

    def test_absolute_data_dir_inside_the_workspace_is_accepted(self):
        canonical = str((self.workspace / "data" / "mail-desk").resolve())
        with patch.object(batch_cli, "verify_workspace_lock") as guard, \
                patch.object(batch_cli.subprocess, "run", side_effect=self.runner()):
            code, result = self.call(extra=("--data-dir", canonical))
        self.assertEqual(0, code)
        self.assertTrue(result["success"])
        guard.assert_called_once_with(self.workspace, lease_id=None, conversation_id=None)
        self.assertEqual(canonical, self.command[self.command.index("--data-dir") + 1])

    def test_missing_workspace_is_rejected_before_child(self):
        missing = self.workspace / "missing-workspace"
        stream = io.StringIO()
        argv = ["batch-cli", "sync-sent", "--workspace", str(missing)]
        with patch.object(sys, "argv", argv), contextlib.redirect_stdout(stream), \
                patch.object(batch_cli.subprocess, "run") as run:
            code = batch_cli.main()
        result = json.loads(stream.getvalue())
        self.assertEqual(2, code)
        self.assertEqual("WorkspacePathError", result["error"]["type"])
        run.assert_not_called()

    def _assert_external_output_untouched(self, old: Path, external: Path, extra=()) -> None:
        before = {p.relative_to(external): p.read_bytes() for p in external.rglob("*") if p.is_file()}
        with patch.object(batch_cli, "verify_workspace_lock") as guard, \
                patch.object(batch_cli.subprocess, "run") as child, \
                patch.object(batch_cli, "_prune_output_dir") as prune:
            code, result = self.call("reconcile", extra=("--keep", "1", *extra))
        self.assertEqual(2, code)
        self.assertEqual("WorkspacePathError", result["error"]["type"])
        guard.assert_not_called()
        child.assert_not_called()
        prune.assert_not_called()
        self.assertEqual(before, {
            p.relative_to(external): p.read_bytes() for p in external.rglob("*") if p.is_file()
        })
        self.assertEqual(b"{}", old.read_bytes())

    def test_tmp_junction_escape_is_rejected_before_output_mutation(self):
        external = self.workspace.parent / (self.workspace.name + "-tmp-external")
        (external / "mail-batch").mkdir(parents=True)
        old = external / "mail-batch" / "reconcile-20260101-000000-envelope.json"
        old.write_bytes(b"{}")
        self.addCleanup(lambda: shutil.rmtree(external, ignore_errors=True))
        if not _make_dir_link(self.workspace / "tmp", external):
            self.skipTest("directory symlinks/junctions are unavailable")
        self._assert_external_output_untouched(old, external)

    def test_mail_batch_symlink_escape_is_rejected_before_output_mutation(self):
        external = self.workspace.parent / (self.workspace.name + "-batch-external")
        target = external / "mail-batch"
        target.mkdir(parents=True)
        old = target / "reconcile-20260101-000000-envelope.json"
        old.write_bytes(b"{}")
        self.addCleanup(lambda: shutil.rmtree(external, ignore_errors=True))
        (self.workspace / "tmp").mkdir()
        if not _make_dir_link(self.workspace / "tmp" / "mail-batch", target):
            self.skipTest("directory symlinks/junctions are unavailable")
        self._assert_external_output_untouched(old, external)

    def _assert_workspace_path_error(self) -> None:
        with patch.object(batch_cli, "verify_workspace_lock") as guard, \
                patch.object(batch_cli.subprocess, "run") as child:
            code, result = self.call()
        self.assertEqual(2, code)
        self.assertEqual("WorkspacePathError", result["error"]["type"])
        guard.assert_not_called()
        child.assert_not_called()

    def test_internal_tmp_junction_is_rejected(self):
        (self.workspace / "real-tmp").mkdir()
        if not _make_dir_link(self.workspace / "tmp", self.workspace / "real-tmp"):
            self.skipTest("directory symlinks/junctions are unavailable")
        self._assert_workspace_path_error()

    def test_internal_data_junction_is_rejected(self):
        (self.workspace / "real-data" / "mail-desk").mkdir(parents=True)
        if not _make_dir_link(self.workspace / "data", self.workspace / "real-data"):
            self.skipTest("directory symlinks/junctions are unavailable")
        self._assert_workspace_path_error()

    def test_broken_mail_batch_symlink_is_rejected(self):
        (self.workspace / "tmp").mkdir()
        link = self.workspace / "tmp" / "mail-batch"
        try:
            link.symlink_to(self.workspace / "missing-target", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("file symlinks are unavailable")
        self._assert_workspace_path_error()

    def test_self_referential_mail_batch_symlink_is_rejected(self):
        tmp = self.workspace / "tmp"
        tmp.mkdir()
        link = tmp / "mail-batch"
        try:
            link.symlink_to(link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("self-referential symlinks are unavailable")
        self._assert_workspace_path_error()

    def test_symlink_or_junction_data_dir_escape_is_rejected(self):
        data = self.workspace / "data"
        data.mkdir()
        outside = self.workspace.parent / (self.workspace.name + "-link-target")
        target = outside / "data" / "mail-desk"
        target.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(outside, ignore_errors=True))
        if not _make_dir_link(data / "mail-desk", target):
            self.skipTest("directory symlinks/junctions are unavailable")
        with patch.object(batch_cli, "verify_workspace_lock") as guard, \
                patch.object(batch_cli.subprocess, "run") as run:
            code, result = self.call()
        self.assertEqual(2, code)
        self.assertEqual("WorkspacePathError", result["error"]["type"])
        guard.assert_not_called()
        run.assert_not_called()
        self.assertFalse((self.workspace / "tmp").exists())

    def test_draft_default_count_and_delegation_reaches_child(self):
        with patch.object(batch_cli, "verify_workspace_lock"), \
                patch.object(batch_cli.subprocess, "run", side_effect=self.runner()):
            code, _ = self.call("draft", ("--workspace-lease-id", "mine", "--workspace-conversation-id", "session"))
        self.assertEqual(code, 0)
        for flag, value in (("--draft", "10"), ("--workspace-lease-id", "mine"),
                            ("--workspace-conversation-id", "session")):
            self.assertEqual(self.command[self.command.index(flag) + 1], value)

    def test_failed_guard_has_no_local_or_child_side_effects(self):
        with patch.object(batch_cli, "verify_workspace_lock", side_effect=RuntimeError("foreign")), \
                patch.object(batch_cli.subprocess, "run") as run:
            code, result = self.call()
        self.assertEqual(code, 2)
        self.assertFalse(result["success"])
        self.assertEqual(result["error"]["type"], "WorkspaceLockError")
        self.assertEqual(list(self.workspace.iterdir()), [])
        run.assert_not_called()

    def test_failed_guard_blocks_since_recovery_before_read_or_write(self):
        with patch.object(batch_cli, "verify_workspace_lock", side_effect=RuntimeError("foreign")), \
                patch.object(batch_cli.subprocess, "run") as run:
            code, result = self.call(
                extra=("--since", "2026-09-01", "--account", "ACC", "--workspace-lease-id", "foreign"),
            )
        self.assertEqual(code, 2)
        self.assertFalse(result["success"])
        self.assertEqual(result["error"]["type"], "WorkspaceLockError")
        self.assertEqual(list(self.workspace.iterdir()), [])
        run.assert_not_called()

    def test_sync_sent_since_dispatches_verified_manifest_through_stdin(self):
        with patch.object(batch_cli, "verify_workspace_lock") as guard, \
                patch.object(batch_cli.subprocess, "run", side_effect=self.runner()):
            code, result = self.call(
                extra=("--since", "2026-09-01", "--account", "ACC", "--workspace-lease-id", "mine"),
            )
        self.assertEqual(0, code)
        self.assertTrue(result["success"])
        guard.assert_called_once_with(self.workspace, lease_id="mine", conversation_id=None)
        self.assertIn("--stdin", self.command)
        self.assertNotIn("--sync-sent", self.command)
        self.assertEqual(
            {
                "mode": "sync_sent", "folder": "Sent Items", "count": 150,
                "since": "2026-09-01", "account": "ACC",
            },
            json.loads(self.child_options["input"]),
        )

    def test_sync_sent_since_requires_the_verified_account(self):
        stream = io.StringIO()
        argv = [
            "batch-cli", "sync-sent", "--since", "2026-09-01",
            "--workspace", str(self.workspace),
        ]
        with patch.object(sys, "argv", argv), patch.object(batch_cli.subprocess, "run") as run, \
                contextlib.redirect_stderr(io.StringIO()) as stderr, \
                self.assertRaises(SystemExit) as raised:
            batch_cli.main()
        self.assertEqual(2, raised.exception.code)
        self.assertIn("--since requires --account", stderr.getvalue())
        self.assertEqual(list(self.workspace.iterdir()), [])
        run.assert_not_called()

    def test_execute_rejects_unbound_manifest_before_wrapper_logs_or_pruning(self):
        data = self.workspace / "data" / "mail-desk"
        data.mkdir(parents=True)
        (self.workspace / ".agents").mkdir()
        (self.workspace / ".agents" / "mail-desk-backend.json").write_text(
            '{"schema_version": 1, "backend": "himalaya", "account": null}', encoding="utf-8")
        manifest = data / "batch-manifest.json"
        manifest.write_text('{"mode":"execute","items":[]}', encoding="utf-8")
        before = {p.relative_to(self.workspace): p.read_bytes() for p in self.workspace.rglob("*") if p.is_file()}
        with patch.object(batch_cli, "verify_workspace_lock"), patch.object(batch_cli.subprocess, "run", side_effect=self.runner()) as child:
            code, result = self.call("execute")
        self.assertNotEqual(0, code)
        self.assertFalse(result["success"])
        self.assertEqual("incomplete_contract", result["data"]["contract_gate"]["reason_code"])
        child.assert_not_called()
        self.assertEqual(before, {p.relative_to(self.workspace): p.read_bytes() for p in self.workspace.rglob("*") if p.is_file()})

    def test_valid_execute_sends_exact_preflighted_snapshot_and_keeps_input(self):
        from review_fixtures import approved_execute
        data = self.workspace / "data" / "mail-desk"
        data.mkdir(parents=True)
        (self.workspace / ".agents").mkdir()
        (self.workspace / ".agents" / "mail-desk-backend.json").write_text(
            '{"schema_version": 1, "backend": "himalaya", "account": null}', encoding="utf-8")
        config = approved_execute({"mode": "execute", "items": []})
        manifest = data / "batch-manifest.json"
        manifest.write_text(json.dumps(config), encoding="utf-8")
        with patch.object(batch_cli, "verify_workspace_lock"), patch.object(
            batch_cli.subprocess, "run", side_effect=self.runner(),
        ):
            code, result = self.call("execute")
        self.assertEqual(0, code)
        self.assertTrue(result["success"])
        self.assertIn("--stdin", self.command)
        self.assertEqual(config, json.loads(self.child_options["input"]))
        self.assertEqual(config, json.loads(manifest.read_text(encoding="utf-8")))

    def test_runner_failure_is_failed_summary_even_with_zero_exit(self):
        for child_exit in (0, 1, 2):
            with self.subTest(child_exit=child_exit), patch.object(batch_cli, "verify_workspace_lock"), \
                    patch.object(batch_cli.subprocess, "run", side_effect=self.runner(success=False, code=child_exit)):
                code, result = self.call()
            self.assertEqual(code, child_exit or 1)
            self.assertFalse(result["success"])

    def test_child_timeout_and_launch_error_envelopes(self):
        for error in (subprocess.TimeoutExpired("synthetic", 1), OSError("synthetic")):
            with self.subTest(error=type(error)), patch.object(batch_cli, "verify_workspace_lock"), \
                    patch.object(batch_cli.subprocess, "run", side_effect=error):
                code, result = self.call()
            self.assertEqual(code, 2)
            self.assertFalse(result["success"])

    def test_invalid_runner_output_is_failure_not_success(self):
        for payload in (b"not-json", b"[]", b"null", b"\xff"):
            def invalid(command, **kwargs):
                kwargs["stdout"].write(payload)
                return subprocess.CompletedProcess(command, 0)
            with self.subTest(payload=payload), patch.object(batch_cli, "verify_workspace_lock"), \
                    patch.object(batch_cli.subprocess, "run", side_effect=invalid):
                code, result = self.call()
            self.assertEqual(code, 1)
            self.assertFalse(result["success"])
            self.assertIn("envelope_parse_error", result["data"])

    def test_count_must_be_positive_before_guard_or_persistence(self):
        for count in ("0", "-1"):
            with patch.object(batch_cli, "verify_workspace_lock") as guard, \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                self.call(extra=("--count", count))
            self.assertEqual(error.exception.code, 2)
            guard.assert_not_called()
        self.assertEqual(list(self.workspace.iterdir()), [])

    def test_real_guard_missing_foreign_and_owned(self):
        cli = MAIL_DESK_ROOT.parents[2] / "workspace-lock/scripts/invoke_workspace_lock.py"
        base = [sys.executable, str(cli)]
        with patch.dict(os.environ, {"WORKSPACE_LOCK_LEASE_ID": "", "WORKSPACE_LOCK_CONVERSATION_ID": "",
                                     "WORKSPACE_LOCK_ALLOW_LEGACY": "1"}), \
                patch.object(batch_cli.subprocess, "run", wraps=subprocess.run) as run:
            # No runner can start before ownership, even with a legacy env flag.
            code, result = self.call()
            self.assertEqual(code, 2)
            self.assertFalse(result["success"])
        self.assertFalse((self.workspace / "tmp").exists())
        subprocess.run(base + ["acquire", str(self.workspace), "--harness", "unittest", "--lease-id", "owned",
                              "--conversation-id", "test-session", "--duration-minutes", "5", "--json"],
                       check=True, capture_output=True)
        try:
            code, _ = self.call(extra=("--workspace-lease-id", "foreign"))
            self.assertEqual(code, 2)
            self.assertFalse((self.workspace / "tmp").exists())
            # The real guard runs outside the mocked runner invocation.
            original_guard = batch_cli.verify_workspace_lock
            def checked_guard(*args, **kwargs):
                with patch.object(batch_cli.subprocess, "run", original_run):
                    return original_guard(*args, **kwargs)
            original_run = subprocess.run
            with patch.object(batch_cli, "verify_workspace_lock", side_effect=checked_guard), \
                    patch.object(batch_cli.subprocess, "run", side_effect=self.runner()):
                code, result = self.call(extra=("--workspace-conversation-id", "test-session"))
            self.assertEqual(code, 0, result)
        finally:
            subprocess.run(base + ["release", str(self.workspace), "--lease-id", "owned", "--json"],
                           check=True, capture_output=True)


if __name__ == "__main__":
    unittest.main()
