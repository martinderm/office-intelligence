"""Domain-oriented attachment quarantine modules for the mail-desk desk (FR-13 / MD-M2).

MD-M2-T01 establishes this package with the canonical attachment quarantine owners
(``quarantine_index``, ``attachment_fetch``, ``attachment_extract``,
``attachment_filing``, ``attachment_policy`` and ``attachment_handoff``). Every
former ``core/attachment_*.py`` path stays available as a thin facade that aliases
its canonical owner in ``sys.modules`` by object identity. Importing this package
has no side effects and never imports ``classifier.py``.
"""

from __future__ import annotations
