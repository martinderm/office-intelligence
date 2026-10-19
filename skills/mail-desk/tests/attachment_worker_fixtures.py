"""Picklable synthetic fixtures executed inside spawned extraction workers.

``attachment_extract.run_with_timeout`` sends its callable and keyword arguments
through a real multiprocessing boundary, so injected OCR runners, lock verifiers
and preflight runners are pickled by module reference.  Keeping them in the MDA3
test module would make every spawned worker re-import the complete test module
(including the Himalaya client) just to resolve the fixture.  This leaf module
holds only the fixtures and their narrow canonical dependency, so a worker import
stays independent of the test suite.

The helpers are deterministic synthetic stand-ins.  They never touch a mailbox,
the network, or a live Git index, and the production defaults they replace stay
untouched and fail-closed.  This module is deliberately *not* named ``test_*.py``
so the unittest discovery pattern does not collect it as a suite.
"""

from __future__ import annotations

from pathlib import Path
import sys
import time
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from core import quarantine_preflight as qpf  # noqa: E402


def _late_writer_target(write_path_str: str) -> str:
    """Module-level picklable helper to test process termination and late write prevention."""
    time.sleep(1.2)
    Path(write_path_str).write_text("late write succeeded", encoding="utf-8")
    return "done"


def _mock_ocr_slow(source: Path, deriv: Path, max_pages: int = 3, pages: Any = None, timeout: float = 30) -> tuple[bytes, str, int]:
    deriv.parent.mkdir(parents=True, exist_ok=True)
    deriv.write_bytes(b"Partial incomplete bytes")
    time.sleep(1.0)
    return b"", "", 0


def _noop_lock_verifier(*args: Any, **kwargs: Any) -> None:
    """Injected, hermetic lock verifier representing an owned harness lease.

    The legacy `allow_legacy` bypass is gone, so derivative-writing OCR tests inject
    this verified-ownership seam instead of opting out of lock enforcement.
    """
    return None


def _clean_tracked_quarantine_runner(argv: Any, cwd: str, timeout_seconds: float) -> qpf.GitIndexQueryResult:
    """Injected, hermetic stand-in for the bounded tracked-quarantine Git preflight.

    MDA3 exercises extraction semantics, not Git-index state. Returning an empty,
    successful index keeps every extraction test independent of a live Git checkout
    while the production default runner stays untouched and fail-closed.
    """
    return qpf.GitIndexQueryResult(returncode=0, stdout="", stderr="")
