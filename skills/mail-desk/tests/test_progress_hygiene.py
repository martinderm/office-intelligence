"""Hermetic regression tests for progress atomic-write temp hygiene."""

from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from core.progress import BatchProgressTracker  # noqa: E402


class ProgressTempHygieneTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)

    def make_tracker(self) -> BatchProgressTracker:
        return BatchProgressTracker(
            mode="execute",
            total_items=2,
            data_dir=self.data_dir,
            console_log=False,
        )

    def temp_files(self) -> list[str]:
        return sorted(path.name for path in self.data_dir.glob("progress_*.tmp"))

    def read_state(self) -> dict:
        raw = (self.data_dir / "runner-progress.json").read_text(encoding="utf-8")
        return json.loads(raw)

    def test_success_replaces_progress_and_leaves_no_temp(self):
        tracker = self.make_tracker()
        tracker.step("routing")
        tracker.advance_item()
        tracker.complete()

        self.assertEqual([], self.temp_files())
        self.assertEqual("completed", self.read_state()["status"])

    def test_replace_failure_removes_temp(self):
        with patch("core.progress.os.replace", side_effect=OSError("locked")):
            tracker = self.make_tracker()
            tracker.step("routing")

        self.assertEqual([], self.temp_files())
        self.assertFalse((self.data_dir / "runner-progress.json").exists())

    def test_serialization_failure_removes_temp(self):
        with patch("core.progress.json.dump", side_effect=ValueError("bad state")):
            tracker = self.make_tracker()
            tracker.step("routing")

        self.assertEqual([], self.temp_files())

    def test_failed_update_preserves_previous_progress_and_removes_temp(self):
        tracker = self.make_tracker()
        tracker.step("routing")

        with patch("core.progress.os.replace", side_effect=OSError("locked")):
            tracker.step("second")

        self.assertEqual([], self.temp_files())
        self.assertEqual("routing", self.read_state()["progress"]["current_step"])

    def test_abort_after_midrun_exception_leaves_no_temp(self):
        tracker = self.make_tracker()
        tracker.step("routing")

        try:
            raise RuntimeError("transport failure")
        except RuntimeError:
            tracker.abort("transport failure")

        self.assertEqual([], self.temp_files())
        self.assertEqual("aborted", self.read_state()["status"])

    def test_aborted_write_failure_removes_temp(self):
        with patch("core.progress.os.replace", side_effect=OSError("locked")):
            tracker = self.make_tracker()
            tracker.abort("interrupted")

        self.assertEqual([], self.temp_files())


if __name__ == "__main__":
    unittest.main()
