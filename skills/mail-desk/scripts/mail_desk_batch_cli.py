"""Run the mail-desk batch runner with stdout/stderr persisted to workspace tmp files.

The runner's canonical envelope (stdout) and its live progress log (stderr) are
persisted under ``<workspace>/tmp/mail-batch/``; the CLI then emits a compact
canonical summary envelope instead of the full runner envelope. Run from the
consumer workspace root.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from core.envelope import build_error, build_success, emit_json
from core.batch_contract import validate_execute_contract
from core.readiness import bind_workspace_account, workspace_root_from_data_dir
from core.quarantine.attachment_fetch import verify_workspace_lock

SCRIPTS_DIR = Path(__file__).resolve().parent


def _absolute(data_dir: str, workspace: Path) -> str:
    path = Path(data_dir)
    return str(path if path.is_absolute() else workspace / path)


def _resolve_bound_paths(args: argparse.Namespace) -> tuple[Path, Path]:
    """Canonicalize the verified workspace and its bound ``data/mail-desk``.

    The wrapper lease belongs to ``--workspace``; a ``--data-dir`` outside it
    would let the runner bind and write another consumer without its lease.
    Symlinks/junctions are resolved (escape detection) and the result must match
    the runner's own workspace-root inference exactly. A non-existent workspace
    fails closed; a missing ``data/mail-desk`` is allowed because sync creates it.
    """
    workspace = Path(args.workspace).expanduser()
    try:
        workspace = workspace.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"workspace does not exist or cannot be resolved: {exc}") from exc
    if not workspace.is_dir():
        raise ValueError("workspace must be an existing directory")

    raw_data_dir = Path(args.data_dir).expanduser()
    data_dir = (raw_data_dir if raw_data_dir.is_absolute() else workspace / raw_data_dir).resolve()
    try:
        inferred = workspace_root_from_data_dir(data_dir)
    except ValueError as exc:
        raise ValueError(
            "--data-dir must be the verified workspace's canonical data/mail-desk directory"
        ) from exc
    expected = (workspace / "data" / "mail-desk").resolve()
    if data_dir != expected or inferred != workspace:
        raise ValueError(
            "--data-dir resolves outside the verified workspace; refusing before child/read/write"
        )
    return workspace, data_dir


def _resolve_run_dir(workspace: Path) -> Path:
    """Bind the wrapper's output directory to its canonical workspace path.

    ``tmp`` or ``tmp/mail-batch`` may be symlinks/junctions; the canonical path
    must be exactly ``<workspace>/tmp/mail-batch`` so pruning/writes cannot touch
    another consumer's files without its lease. Missing internal paths are fine.
    """
    expected = workspace / "tmp" / "mail-batch"
    resolved = expected.resolve()
    if resolved != expected:
        raise ValueError(
            "workspace tmp/mail-batch resolves outside or away from the canonical output "
            "directory; refusing before mkdir/prune/child"
        )
    return resolved


def _positive_int(value: str) -> int:
    """Argparse type enforcing ``keep >= 1`` (fail loud instead of guessing)."""
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected an integer, got {value!r}") from exc
    if number < 1:
        raise argparse.ArgumentTypeError(f"expected a value >= 1, got {number}")
    return number


def _iso_date(value: str) -> str:
    """Argparse type enforcing a bare ``YYYY-MM-DD`` verified recovery start."""
    if len(value) != 10:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {value!r}")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a valid YYYY-MM-DD date, got {value!r}") from exc
    return value


def _resolve_account(args: argparse.Namespace) -> str | None:
    """Return the explicit account override, or ``None`` to let the runner bind.

    ``None`` means the runner resolves the account itself from the consumer
    workspace backend binding (``bind_workspace_account``); the wrapper must not
    thread ``--account`` in that case. The bundle intentionally holds no
    workspace-specific account literal.
    """
    return args.account


def _build_args(args: argparse.Namespace, workspace: Path) -> list[str]:
    """Build the runner argv for ``args.mode``, resolved against ``workspace``.

    Purpose: reconcile reads the recovery journal (``--reconcile``, no input
    manifest), so no ``--input`` is threaded for any mode. ``--account`` is
    threaded only when ``_resolve_account`` returns an explicit override.

    The returned list additionally carries the wrapper-only persistence policy
    as a single trailing ``"--keep N"`` metadata marker. The runner has no
    ``--keep`` flag, so ``main`` strips that final marker before invoking the
    runner and uses it to prune ``tmp/mail-batch``. The runner is referenced by
    its bare script name; ``main`` anchors it to ``SCRIPTS_DIR``.
    """
    runner = "mail_desk_batch_runner.py"
    data_dir = _absolute(args.data_dir, workspace)
    if args.mode == "draft":
        argv = [
            runner, "--draft", str(args.count), "--order", "oldest",
            "--folder", "INBOX", "--skip-known", "--data-dir", data_dir,
        ]
    elif args.mode == "execute":
        argv = [runner, "--data-dir", data_dir, "--keep-input"]
    elif args.mode == "reconcile":
        argv = [runner, "--data-dir", data_dir, "--reconcile"]
        if getattr(args, "apply", False):
            argv.append("--apply")
            approved_by = getattr(args, "approved_by", None)
            if approved_by:
                argv += ["--approved-by", approved_by]
    elif args.mode == "sync-sent":
        if getattr(args, "since", None) is not None:
            # Recovery carries since in a stdin manifest; the direct --sync-sent
            # mode would win and silently ignore it.
            argv = [runner, "--data-dir", data_dir]
        else:
            argv = [runner, "--data-dir", data_dir, "--sync-sent", str(args.count)]
    else:
        raise ValueError(f"Unsupported wrapper mode: {args.mode}")
    account = _resolve_account(args)
    if account is not None:
        argv += ["--account", account]
    # The runner accepts delegation flags only for draft/inspect. Other modes
    # still need ownership for wrapper persistence, checked separately in main.
    if args.mode == "draft":
        for name in ("workspace_lease_id", "workspace_conversation_id"):
            value = getattr(args, name, None)
            if value is not None:
                argv += ["--" + name.replace("_", "-"), value]
    argv.append(f"--keep {args.keep}")
    return argv


def _sync_sent_manifest(args: argparse.Namespace) -> dict[str, object] | None:
    """Build the verified sent-recovery manifest sent to the runner via stdin.

    Only ``sync-sent`` with ``--since`` uses a manifest; normal runs keep the
    direct ``--sync-sent`` mode. The verified account is mandatory for recovery.
    """
    since = getattr(args, "since", None)
    if args.mode != "sync-sent" or since is None:
        return None
    return {
        "mode": "sync_sent",
        "folder": "Sent Items",
        "count": args.count,
        "since": since,
        "account": _resolve_account(args),
    }


def _prune_output_dir(run_dir: Path, keep: int) -> None:
    """Retain the newest ``keep`` envelope/progress files, delete the rest.

    Envelope and progress file names share a ``<mode>-<timestamp>`` prefix, so
    descending name order is newest-first. Files matching neither pattern are
    left untouched; an empty or missing ``run_dir`` is a no-op.
    """
    if not run_dir.is_dir():
        return
    for pattern in ("*-envelope.json", "*-progress.log"):
        files = sorted(run_dir.glob(pattern), key=lambda path: path.name, reverse=True)
        for stale in files[keep:]:
            stale.unlink()


def _summary(args: argparse.Namespace, envelope_path: Path, progress_path: Path, proc: subprocess.CompletedProcess) -> dict:
    data: dict[str, object] = {
        "exit_code": proc.returncode,
        "mode": args.mode,
        "envelope_file": str(envelope_path),
        "progress_file": str(progress_path),
    }
    try:
        runner_envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        data["envelope_parse_error"] = str(exc)
        return data
    if not isinstance(runner_envelope, dict):
        data["envelope_parse_error"] = "Runner envelope must be a JSON object."
        return data
    data["state"] = runner_envelope.get("state")
    data["success"] = runner_envelope.get("success")
    data["message"] = runner_envelope.get("message")
    runner_data = runner_envelope.get("data") or {}
    if isinstance(runner_data, dict):
        review = runner_data.get("review")
        if isinstance(review, dict):
            data["review_state"] = review.get("state")
            data["execute_request_sha256"] = review.get("execute_request_sha256")
        draft = runner_data.get("draft")
        if isinstance(draft, dict):
            data["drafted"] = draft.get("total_drafted")
        if runner_data.get("executed_count") is not None:
            data["executed_count"] = runner_data.get("executed_count")
        for key in ("recovery_required", "date_windows_synced", "envelopes_examined",
                    "total_envelopes_examined", "new_entries_indexed", "new_count", "reason_code",
                    "write_state", "truncated", "coverage_start", "coverage_end", "covered_days",
                    "total_days", "coverage_complete", "next_start", "follow_up_hint",
                    "since_requested", "since_ignored", "since_ignored_reason",
                    "effective_coverage_start"):
            if runner_data.get(key) is not None:
                data[key] = runner_data[key]
        handoff = runner_data.get("handoff")
        if isinstance(handoff, dict):
            # Counts only: the items stay in the persisted runner envelope and the handoff file.
            data["handoff"] = {key: handoff[key] for key in (
                "state", "pending_count", "written_count", "deferred_count", "file",
            ) if key in handoff}
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=("draft", "execute", "reconcile", "sync-sent"))
    parser.add_argument("--count", "-c", type=_positive_int, default=None, help="draft candidates (default 10) or recent Sent Items (default 150)")
    parser.add_argument(
        "--since", type=_iso_date, default=None,
        help="verified conservative start (YYYY-MM-DD) for sync-sent recovery; requires --account",
    )
    parser.add_argument("--account", default=None, help="optional account override; default: workspace backend binding")
    parser.add_argument("--data-dir", default="data/mail-desk", help="mail-desk data dir relative to --workspace")
    parser.add_argument("--workspace", default=".", help="workspace root (default: current directory)")
    parser.add_argument("--workspace-lease-id", default=None, help="owned workspace lease; delegated to the runner for draft")
    parser.add_argument("--workspace-conversation-id", default=None, help="owned workspace conversation; delegated to the runner for draft")
    parser.add_argument("--apply", action="store_true", help="with reconcile: apply verified local repairs (explicit CLI approval; never mutates the mailbox)")
    parser.add_argument("--approved-by", default=None, help="reviewer identity recorded in the --apply recovery receipt")
    parser.add_argument("--timeout", type=int, default=900, help="runner timeout in seconds")
    parser.add_argument("--keep", type=_positive_int, default=10, help="retain the newest N envelope/progress pairs in tmp/mail-batch (default 10)")
    args = parser.parse_args()
    if args.apply and args.mode != "reconcile":
        parser.error("--apply requires the reconcile mode")
    if args.approved_by is not None and not args.apply:
        parser.error("--approved-by requires --apply")
    if args.since is not None and args.mode != "sync-sent":
        parser.error("--since requires the sync-sent mode")
    if args.since is not None and not (args.account and args.account.strip()):
        parser.error("--since requires --account with the verified account")
    if args.count is None:
        args.count = 150 if args.mode == "sync-sent" else 10

    try:
        workspace, data_dir = _resolve_bound_paths(args)
    except (OSError, ValueError) as exc:
        emit_json(build_error(
            "batch_cli", "Workspace/data-dir binding could not be verified.",
            {"mode": args.mode}, error_type="WorkspacePathError",
            error_details={"reason": str(exc)[:1000]},
        ))
        return 2
    # All downstream paths use the canonical bound directory, exactly as the
    # runner infers its workspace root from ``data_dir``.
    args.data_dir = str(data_dir)
    try:
        run_dir = _resolve_run_dir(workspace)
    except (OSError, ValueError) as exc:
        emit_json(build_error(
            "batch_cli", "Workspace output directory could not be verified.",
            {"mode": args.mode}, error_type="WorkspacePathError",
            error_details={"reason": str(exc)[:1000]},
        ))
        return 2
    try:
        verify_workspace_lock(
            workspace, lease_id=args.workspace_lease_id,
            conversation_id=args.workspace_conversation_id,
        )
    except (RuntimeError, OSError, ValueError) as exc:
        emit_json(build_error("batch_cli", "Workspace lock ownership could not be verified.",
                              {"mode": args.mode}, error_type="WorkspaceLockError",
                              error_details={"reason": str(exc)[:1000]}))
        return 2
    execute_input = None
    if args.mode == "execute":
        # Use the runner's existing discovery/parser, but validate before opening
        # wrapper logs or pruning. Send the validated snapshot to avoid re-reading
        # a potentially changed file in the child; its execute gate still rechecks.
        from mail_desk_batch_runner import _build_parser, _load_configuration, MODE_ALIASES

        try:
            dd = data_dir
            runner_args = _build_parser().parse_args(_build_args(args, workspace)[1:-1])
            config, _ = _load_configuration(runner_args, dd)
            if not isinstance(config, dict) or MODE_ALIASES.get(str(config.get("mode", "execute")).lower()) != "execute":
                raise ValueError("execute wrapper requires an execute manifest")
            contract = validate_execute_contract(config, effective_account=config.get("account"))
            if not contract["ok"]:
                emit_json(build_error("batch_cli", contract["message"],
                                      {"mode": "execute", "contract_gate": contract},
                                      error_type="ExecuteContractError"))
                return 1
            binding = bind_workspace_account(dd, requested_account=args.account or config.get("account"))
            if not binding["ok"]:
                raise ValueError("Workspace backend/account binding failed")
            contract = validate_execute_contract(config, effective_account=binding["account"])
            if not contract["ok"]:
                emit_json(build_error("batch_cli", contract["message"],
                                      {"mode": "execute", "contract_gate": contract},
                                      error_type="ExecuteContractError"))
                return 1
            execute_input = json.dumps(config, ensure_ascii=False).encode("utf-8")
        except (OSError, ValueError) as exc:
            emit_json(build_error("batch_cli", "Execute preflight failed.",
                                  {"mode": "execute"}, error_type=type(exc).__name__))
            return 1
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        emit_json(build_error("batch_cli", "Output directory could not be created.",
                              {"error": str(exc)}))
        return 2
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    envelope_path = run_dir / f"{args.mode}-{stamp}-envelope.json"
    progress_path = run_dir / f"{args.mode}-{stamp}-progress.log"

    runner_argv = _build_args(args, workspace)
    if runner_argv and runner_argv[-1].startswith("--keep "):
        # Wrapper-only persistence marker; the runner has no --keep flag.
        runner_argv = runner_argv[:-1]
    # Anchor the bare runner name returned by _build_args to the scripts dir.
    command = [sys.executable, str(SCRIPTS_DIR / runner_argv[0]), *runner_argv[1:]]
    child_input = {}
    if execute_input is not None:
        child_input["input"] = execute_input
    sync_sent_manifest = _sync_sent_manifest(args)
    if sync_sent_manifest is not None:
        child_input["input"] = json.dumps(sync_sent_manifest, ensure_ascii=False).encode("utf-8")
    if child_input:
        command.append("--stdin")

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        with open(envelope_path, "wb") as out, open(progress_path, "wb") as err:
            proc = subprocess.run(
                command,
                stdout=out, stderr=err, cwd=str(workspace), env=env,
                timeout=args.timeout, **child_input,
            )
    except subprocess.TimeoutExpired:
        emit_json(build_error(
            "batch_cli", "Runner timed out.",
            {"mode": args.mode, "envelope_file": str(envelope_path), "progress_file": str(progress_path)},
            error_type="Timeout",
        ))
        return 2
    except OSError as exc:
        emit_json(build_error("batch_cli", "Runner could not be started.", {"error": str(exc)}))
        return 2

    summary = _summary(args, envelope_path, progress_path, proc)
    try:
        _prune_output_dir(run_dir, args.keep)
    except OSError as exc:
        # Pruning is hygiene only; never mask the runner's outcome.
        summary["prune_error"] = str(exc)
    ok = proc.returncode == 0 and summary.get("success") is True
    if ok:
        emit_json(build_success("batch_cli", "Runner finished.", summary, state="Completed"))
    else:
        emit_json(build_error("batch_cli", "Runner finished with failures.", summary,
                              state="PartialFailure", error_type="RunnerFailure"))
    return proc.returncode or (0 if ok else 1)


if __name__ == "__main__":
    sys.exit(main())
