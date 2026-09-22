"""Legacy facade for :mod:`core.quarantine.attachment_extract` (FR-13 / MD-M2).

The canonical owner lives in ``core.quarantine.attachment_extract``. This shim
replaces its own ``sys.modules`` entry with the owner module object so legacy
imports, monkeypatch seams and ``core`` re-exports keep resolving to the
identical module and symbol objects.
"""

from __future__ import annotations

import sys

from core.quarantine import attachment_extract as _owner

sys.modules[__name__] = _owner
