"""Batch progress monitoring and deterministic time estimation for mail-desk."""

from __future__ import annotations

import atexit
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .common import atomic_write_json, replace_with_retry, utc_now_iso


#: Bounded reason a killed run's leftover ``running`` state is settled with.
PROGRESS_INTERRUPTED_REASON = "interrupted: the runner process is no longer running"

#: Trackers that are still ``running`` in this process.  A graceful interpreter
#: exit (normal end, exception, ``KeyboardInterrupt``) aborts them so no
#: ``status: running`` survives; an OS kill is caught by the reader-side
#: :func:`settle_stale_runner_progress` through the persisted owner pid.
_ACTIVE_TRACKERS: list["BatchProgressTracker"] = []


def _process_is_alive(pid: int) -> bool | None:
    """Best-effort liveness of one owner pid (``None`` when unknowable)."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            synchronize = 0x00100000
            handle = kernel32.OpenProcess(synchronize, False, pid)
            if not handle:
                # ERROR_ACCESS_DENIED: the process exists but is not inspectable.
                return True if ctypes.get_last_error() == 5 else False
            kernel32.CloseHandle(handle)
            return True
        except Exception:  # noqa: BLE001 - unknown liveness must not rewrite state
            return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def _stale_running_state(state: dict[str, Any]) -> bool:
    """Whether ``state`` is a running record whose owner process is gone.

    Legacy states without an owner pid keep their status: ownership is not
    provable, and guessing could overwrite a live run from an older version.
    """
    if state.get("status") != "running":
        return False
    pid = state.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    if pid == os.getpid():
        return False
    return _process_is_alive(pid) is False


def _settled_state(state: dict[str, Any]) -> dict[str, Any]:
    """Return the terminal ``aborted`` variant of one stale running state."""
    settled = dict(state)
    now = utc_now_iso()
    settled["status"] = "aborted"
    settled["updated_at"] = now
    settled["interrupted_at"] = now
    settled["error"] = PROGRESS_INTERRUPTED_REASON
    progress = settled.get("progress")
    if isinstance(progress, dict):
        progress = dict(progress)
        progress["current_step"] = "interrupted"
        settled["progress"] = progress
    timing = settled.get("timing")
    if isinstance(timing, dict):
        timing = dict(timing)
        timing["estimated_remaining_seconds"] = 0.0
        timing["eta_timestamp"] = None
        timing["recommended_next_timer_seconds"] = 0
        settled["timing"] = timing
    return settled


def _load_progress_state(path: Path) -> dict[str, Any] | None:
    """Read one progress document; ``None`` when missing or unreadable."""
    try:
        raw = path.read_text(encoding="utf-8")
        state = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None
    return state if isinstance(state, dict) else None


def _settled_view(state: dict[str, Any]) -> dict[str, Any]:
    """Return the liveness-checked view without writing anything."""
    return _settled_state(state) if _stale_running_state(state) else state


def settle_stale_runner_progress(path: Path) -> dict[str, Any] | None:
    """Read progress state and *mutate* a killed run's leftover ``running`` state.

    This is the writer-side settlement; it runs inside the tracker constructor
    (and therefore inside the owning run's lease).  A pure reader must use
    :func:`read_runner_progress`, which returns the same liveness-checked view
    without writing.
    """
    state = _load_progress_state(path)
    if state is None or not _stale_running_state(state):
        return state
    # Narrow the read/write race: settle only the exact record that was
    # classified stale.  A newer run that wrote in between is returned as-is.
    latest = _load_progress_state(path)
    if latest is None:
        return _settled_state(state)
    if (
        latest.get("run_id") != state.get("run_id")
        or latest.get("pid") != state.get("pid")
        or latest.get("status") != "running"
    ):
        return _settled_view(latest)
    settled = _settled_state(state)
    try:
        atomic_write_json(path, settled)
    except Exception:  # noqa: BLE001 - the settled view is still returned
        pass
    return settled


def read_runner_progress(path: Path) -> dict[str, Any] | None:
    """Return the liveness-checked progress view; never writes and needs no lease."""
    state = _load_progress_state(path)
    return _settled_view(state) if state is not None else None


def _abort_active_trackers(reason: str) -> int:
    """Abort every tracker that is still running in this process (best effort)."""
    aborted = 0
    for tracker in list(_ACTIVE_TRACKERS):
        try:
            if tracker._abort_for_exit(reason):
                aborted += 1
        except Exception:  # noqa: BLE001 - exit paths must never raise
            pass
        finally:
            try:
                _ACTIVE_TRACKERS.remove(tracker)
            except ValueError:
                pass
    return aborted


def abort_active_trackers(reason: str = PROGRESS_INTERRUPTED_REASON) -> int:
    """Public helper for a harness that catches an interruption itself."""
    return _abort_active_trackers(reason)


atexit.register(lambda: _abort_active_trackers(PROGRESS_INTERRUPTED_REASON))


class BatchProgressTracker:
    """Tracks batch execution progress and maintains data/mail-desk/runner-progress.json."""

    def __init__(
        self,
        mode: str,
        total_items: int,
        data_dir: Path,
        run_id: str | None = None,
        console_log: bool = True,
    ):
        self.mode = mode
        self.total_items = max(1, total_items)
        self.completed_items = 0
        self.data_dir = data_dir
        self.console_log = console_log
        self.run_id = run_id or f"{mode}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.start_time = time.time()
        self.last_item_time = self.start_time
        self.item_durations: list[float] = []
        self.progress_file = data_dir / "runner-progress.json"
        # A previous run may have been killed before it could write a terminal
        # state.  Settle it now and keep its bounded identity in the new state;
        # if the new write fails, the old file is already no longer "running".
        previous = settle_stale_runner_progress(self.progress_file)
        self.interrupted_previous_run: dict[str, Any] | None = None
        if (
            isinstance(previous, dict)
            and previous.get("status") == "aborted"
            and previous.get("error") == PROGRESS_INTERRUPTED_REASON
        ):
            progress = previous.get("progress") if isinstance(previous.get("progress"), dict) else {}
            self.interrupted_previous_run = {
                "run_id": previous.get("run_id"),
                "mode": previous.get("mode"),
                "status": "interrupted",
                "completed_items": progress.get("completed_items"),
                "updated_at": previous.get("updated_at"),
            }

        # Baseline average seconds per item based on IMAP benchmarks
        if mode in ("draft", "inspect"):
            self.baseline_sec = 0.8
        elif mode in ("execute", "pipeline"):
            self.baseline_sec = 3.2
        else:
            self.baseline_sec = 1.0

        self.current_step = "initializing"
        self.status = "running"
        _ACTIVE_TRACKERS.append(self)
        self._write_state()

    def _write_state(
        self,
        envelope_id: str | None = None,
        subject: str | None = None,
        error: str | None = None,
    ) -> None:
        now = time.time()
        elapsed = round(now - self.start_time, 1)

        if self.item_durations:
            avg_sec = round(sum(self.item_durations) / len(self.item_durations), 2)
        elif self.completed_items > 0:
            avg_sec = round(elapsed / self.completed_items, 2)
        else:
            avg_sec = self.baseline_sec

        remaining_items = max(0, self.total_items - self.completed_items)
        remaining_sec = round(remaining_items * avg_sec, 1)
        next_timer_sec = max(30, min(int(0.75 * remaining_sec), 360)) if remaining_sec > 0 else 0
        eta_dt = datetime.now() + timedelta(seconds=remaining_sec)
        pct = round((self.completed_items / self.total_items) * 100.0, 1)

        state = {
            "schema_version": 1,
            "run_id": self.run_id,
            "mode": self.mode,
            "status": self.status,
            "pid": os.getpid(),
            "heartbeat_at": utc_now_iso(),
            "started_at": datetime.fromtimestamp(self.start_time).strftime("%Y-%m-%dT%H:%M:%S"),
            "updated_at": utc_now_iso(),
            "progress": {
                "total_items": self.total_items,
                "completed_items": self.completed_items,
                "percent": min(100.0, pct),
                "current_step": self.current_step,
                "current_envelope_id": envelope_id,
                "current_subject": subject[:80] if subject else None,
            },
            "timing": {
                "elapsed_seconds": elapsed,
                "avg_seconds_per_item": avg_sec,
                "estimated_remaining_seconds": remaining_sec if self.status == "running" else 0.0,
                "eta_timestamp": eta_dt.strftime("%Y-%m-%d %H:%M:%S") if self.status == "running" else None,
                "recommended_next_timer_seconds": next_timer_sec if self.status == "running" else 0,
                "timer_rule": "Delta_t = max(30, min(0.75 * remaining_sec, 360))",
            },
            "error": error,
        }
        if self.interrupted_previous_run is not None:
            state["interrupted_previous_run"] = dict(self.interrupted_previous_run)

        temp_path: str | None = None
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            temp_fd, temp_path = tempfile.mkstemp(
                prefix="progress_", suffix=".tmp", dir=str(self.data_dir)
            )
            with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
                f.write("\n")
            replace_with_retry(Path(temp_path), self.progress_file)
            temp_path = None
        except Exception:
            pass
        finally:
            if temp_path is not None:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

    def step(
        self,
        step_name: str,
        envelope_id: str | None = None,
        subject: str | None = None,
    ) -> None:
        """Update current step description."""
        self.current_step = step_name
        self._write_state(envelope_id=envelope_id, subject=subject)
        if self.console_log:
            env_tag = f" [Env {envelope_id}]" if envelope_id else ""
            subj_tag = f" '{subject[:40]}...'" if subject else ""
            pct = round((self.completed_items / self.total_items) * 100.0, 0)
            sys.stdout.write(f"[{int(pct)}%] {step_name}{env_tag}{subj_tag}\n")
            sys.stdout.flush()

    def advance_item(
        self,
        envelope_id: str | None = None,
        subject: str | None = None,
        step_name: str | None = None,
    ) -> None:
        """Mark one item as completed and record timing."""
        now = time.time()
        duration = now - self.last_item_time
        self.last_item_time = now
        self.item_durations.append(duration)
        self.completed_items += 1

        if step_name:
            self.current_step = step_name

        self._write_state(envelope_id=envelope_id, subject=subject)

        if self.console_log:
            pct = round((self.completed_items / self.total_items) * 100.0, 1)
            remaining_items = max(0, self.total_items - self.completed_items)
            avg_sec = sum(self.item_durations) / len(self.item_durations)
            rem_sec = int(remaining_items * avg_sec)
            next_timer_sec = max(30, min(int(0.75 * rem_sec), 360)) if rem_sec > 0 else 0
            env_info = f"Env {envelope_id}: " if envelope_id else ""
            subj_info = f"'{subject[:35]}...' " if subject else ""
            sys.stdout.write(
                f"[{self.completed_items}/{self.total_items} - {pct}%] {env_info}{subj_info}({duration:.1f}s | ETA: {rem_sec}s | Next Timer: {next_timer_sec}s)\n"
            )
            sys.stdout.flush()

    def complete(self, message: str = "Batch completed successfully.") -> None:
        """Mark batch as finished."""
        self.status = "completed"
        self.current_step = "done"
        self.completed_items = self.total_items
        self._write_state()
        self._unregister()
        if self.console_log:
            elapsed = round(time.time() - self.start_time, 1)
            sys.stdout.write(f"\n[OK] {message} (Total: {elapsed}s for {self.total_items} items)\n")
            sys.stdout.flush()

    def fail(self, error_message: str) -> None:
        """Mark batch as failed."""
        self.status = "failed"
        self.current_step = "failed"
        self._write_state(error=error_message)
        self._unregister()
        if self.console_log:
            sys.stderr.write(f"\n[ERROR] Batch failed: {error_message}\n")
            sys.stderr.flush()

    def abort(self, reason: str = "Batch interrupted.") -> None:
        """Persist an interruption distinctly from an ordinary item failure."""
        self.status = "aborted"
        self.current_step = "aborted"
        self._write_state(error=reason)
        self._unregister()
        if self.console_log:
            sys.stderr.write(f"\n[ABORTED] Batch interrupted: {reason}\n")
            sys.stderr.flush()

    def _unregister(self) -> None:
        try:
            _ACTIVE_TRACKERS.remove(self)
        except ValueError:
            pass

    def _abort_for_exit(self, reason: str) -> bool:
        """Silently settle a still-running tracker before the process exits."""
        if self.status != "running":
            self._unregister()
            return False
        self.status = "aborted"
        self.current_step = "interrupted"
        self._write_state(error=reason)
        self._unregister()
        return True
