"""Hermetic process guard for the Issue #103 telemetry/P1 test modules.

Incident follow-up: a test that forgot the ``transport_stats_session`` made
``_run_process`` fall back to the real ``subprocess.run`` and started read-only
Himalaya commands.  Every test class of the affected modules now inherits this
base: ``subprocess.run`` and ``_POPEN`` fail closed with ``AssertionError``
inside ``setUp`` (before the test body), and both patches are restored via
``addCleanup``.  A per-test ``_POPEN`` fake deliberately overrides the guard and
is reset back to it afterwards.
"""

from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import patch

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

from core import himalaya  # noqa: E402

RUN_GUARD_MESSAGE = (
    "real subprocess.run is forbidden in hermetic tests; "
    "wrap run_himalaya in transport_stats_session and fake _POPEN explicitly"
)
POPEN_GUARD_MESSAGE = (
    "real Popen is forbidden in hermetic tests; fake _POPEN explicitly"
)


class HermeticProcessTestCase(unittest.TestCase):
    """Base case that forbids any real child process during the test body."""

    def setUp(self) -> None:
        super().setUp()
        self._run_guard = patch.object(
            himalaya.subprocess, "run", side_effect=AssertionError(RUN_GUARD_MESSAGE)
        )
        self._popen_guard = patch.object(
            himalaya, "_POPEN", side_effect=AssertionError(POPEN_GUARD_MESSAGE)
        )
        self._run_guard.start()
        self._popen_guard.start()
        self.addCleanup(self._popen_guard.stop)
        self.addCleanup(self._run_guard.stop)
