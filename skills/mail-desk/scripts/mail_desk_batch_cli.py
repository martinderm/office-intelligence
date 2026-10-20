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
from core.quarantine.attachment_fetch import verify_workspace_lock

SCRIPTS_DIR = Path(__file__).resolve().parent


def _absolute(data_dir: str, workspace: Path) -> str:
    path = Path(data_dir)
    return str(path if path.is_absolute() else workspace / path)


def _positive_int(value: str) -> int:
    """Argparse type enforcing ``keep >= 1`` (fail loud instead of guessing)."""
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected an integer, got {value!r}") from exc
    if number < 1:
        raise argparse.ArgumentTypeError(f"expected a value >= 1, got {number}")
    return number


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
    elif args.mode == "sync-sent":
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
                    "total_envelopes_examined", "new_entries_indexed"):
            if runner_data.get(key) is not None:
                data[key] = runner_data[key]
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=("draft", "execute", "reconcile", "sync-sent"))
    parser.add_argument("--count", "-c", type=_positive_int, default=None, help="draft candidates (default 10) or recent Sent Items (default 150)")
    parser.add_argument("--account", default=None, help="optional account override; default: workspace backend binding")
    parser.add_argument("--data-dir", default="data/mail-desk", help="mail-desk data dir relative to --workspace")
    parser.add_argument("--workspace", default=".", help="workspace root (default: current directory)")
    parser.add_argument("--workspace-lease-id", default=None, help="owned workspace lease; delegated to the runner for draft")
    parser.add_argument("--workspace-conversation-id", default=None, help="owned workspace conversation; delegated to the runner for draft")
    parser.add_argument("--timeout", type=int, default=900, help="runner timeout in seconds")
    parser.add_argument("--keep", type=_positive_int, default=10, help="retain the newest N envelope/progress pairs in tmp/mail-batch (default 10)")
    args = parser.parse_args()
    if args.count is None:
        args.count = 150 if args.mode == "sync-sent" else 10

    workspace = Path(args.workspace).resolve()
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
    run_dir = workspace / "tmp" / "mail-batch"
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

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        with open(envelope_path, "wb") as out, open(progress_path, "wb") as err:
            proc = subprocess.run(
                command,
                stdout=out, stderr=err, cwd=str(workspace), env=env,
                timeout=args.timeout,
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
