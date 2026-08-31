"""Batch progress monitoring and deterministic time estimation for mail-desk."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .common import utc_now_iso


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

        # Baseline average seconds per item based on IMAP benchmarks
        if mode in ("draft", "inspect"):
            self.baseline_sec = 0.8
        elif mode in ("execute", "pipeline"):
            self.baseline_sec = 3.2
        else:
            self.baseline_sec = 1.0

        self.current_step = "initializing"
        self.status = "running"
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
        eta_dt = datetime.now() + timedelta(seconds=remaining_sec)
        pct = round((self.completed_items / self.total_items) * 100.0, 1)

        state = {
            "schema_version": 1,
            "run_id": self.run_id,
            "mode": self.mode,
            "status": self.status,
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
            },
            "error": error,
        }

        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            temp_fd, temp_path = tempfile.mkstemp(
                prefix="progress_", suffix=".tmp", dir=str(self.data_dir)
            )
            with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(temp_path, self.progress_file)
        except Exception:
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
            env_info = f"Env {envelope_id}: " if envelope_id else ""
            subj_info = f"'{subject[:35]}...' " if subject else ""
            sys.stdout.write(
                f"[{self.completed_items}/{self.total_items} - {pct}%] {env_info}{subj_info}({duration:.1f}s | ETA: {rem_sec}s)\n"
            )
            sys.stdout.flush()

    def complete(self, message: str = "Batch completed successfully.") -> None:
        """Mark batch as finished."""
        self.status = "completed"
        self.current_step = "done"
        self.completed_items = self.total_items
        self._write_state()
        if self.console_log:
            elapsed = round(time.time() - self.start_time, 1)
            sys.stdout.write(f"\n[OK] {message} (Total: {elapsed}s for {self.total_items} items)\n")
            sys.stdout.flush()

    def fail(self, error_message: str) -> None:
        """Mark batch as failed."""
        self.status = "failed"
        self.current_step = "failed"
        self._write_state(error=error_message)
        if self.console_log:
            sys.stderr.write(f"\n[ERROR] Batch failed: {error_message}\n")
            sys.stderr.flush()
