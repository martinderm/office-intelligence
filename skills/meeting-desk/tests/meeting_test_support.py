"""Shared helpers for offline meeting-desk tests (synthetic workspaces only)."""
from __future__ import annotations

import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys


BUNDLE = Path(__file__).resolve().parents[3]
WRITER_SCRIPT = BUNDLE / "skills" / "meeting-desk" / "scripts" / "ingest_meeting.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "meeting-record-v1.json"
ENV_LOCK_ROOT = "MEETING_DESK_WORKSPACE_LOCK_ROOT"


def load_writer():
    spec = importlib.util.spec_from_file_location("meeting_desk_writer_under_test", WRITER_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def find_lock_root() -> Path | None:
    """Locate the trusted shared workspace-lock installation without a copy.

    Order: explicit operator environment, then a sibling skill of the bundle or
    of any ancestor checkout (regular multi-skill checkout). An isolated
    worktree without either is reported as missing, never bypassed.
    """
    candidates: list[Path] = []
    env = os.environ.get(ENV_LOCK_ROOT, "").strip()
    if env:
        candidates.append(Path(env))
    for parent in (BUNDLE, *BUNDLE.parents):
        candidates.append(parent / "workspace-lock")
        candidates.append(parent / "skills" / "workspace-lock")
    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.abspath(candidate)
        if key in seen:
            continue
        seen.add(key)
        guard = candidate / "scripts" / "workspace_lock_guard.py"
        cli = candidate / "scripts" / "invoke_workspace_lock.py"
        if guard.is_file() and cli.is_file():
            return candidate
    return None


def lock_cli(lock_root: Path, action: str, workspace: Path, *options: str) -> dict:
    command = [sys.executable, str(lock_root / "scripts" / "invoke_workspace_lock.py"),
               action, str(workspace), *options, "--json"]
    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:  # pragma: no cover - diagnostic
        raise AssertionError(f"workspace-lock returned malformed JSON: {completed.stderr}") from exc
    if completed.returncode != 0 or not result.get("success"):
        raise AssertionError(f"workspace-lock {action} failed: {result}")
    return result


def acquire(lock_root: Path, workspace: Path, lease_id: str, harness: str = "unittest") -> None:
    lock_cli(lock_root, "acquire", workspace, "--harness", harness,
             "--lease-id", lease_id, "--duration-minutes", "10")


def release(lock_root: Path, workspace: Path, lease_id: str) -> None:
    lock_cli(lock_root, "release", workspace, "--lease-id", lease_id)


def run_cli(lock_root: Path, args: list[str], *, stdin: bytes | None = None,
            cwd: Path | None = None, env: dict[str, str] | None = None) -> tuple[int, dict, str]:
    command = [sys.executable, str(WRITER_SCRIPT), *args, "--workspace-lock-root",
               str(lock_root), "--json"]
    environment = None
    if env:
        environment = os.environ.copy()
        environment.update(env)
    completed = subprocess.run(command, input=stdin, capture_output=True, cwd=cwd,
                               env=environment, check=False)
    stdout = completed.stdout.decode("utf-8")
    lines = [line for line in stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise AssertionError(f"expected exactly one stdout line, got {len(lines)}: {stdout!r}")
    return completed.returncode, json.loads(lines[0]), completed.stderr.decode("utf-8")


def fixture_pack() -> dict:
    return json.loads(FIXTURES.read_text(encoding="utf-8"))


def valid_record(index: int = 1) -> dict:
    return copy.deepcopy(fixture_pack()["valid_records"][index])


def write_record(path: Path, record: dict) -> Path:
    path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8", newline="\n")
    return path


def register_document(root: Path) -> dict:
    return json.loads((root / "memory/evidence/meetings/meetings.json").read_text(encoding="utf-8"))


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
