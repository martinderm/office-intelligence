"""Domain-oriented matching modules for the mail-desk classifier (FR-13 / MD-M1).

MD-M1-T01 establishes this package with the canonical date parser and the reusable
ambiguity policy owner that the ``core.classifier`` facade composes. MD-M1-T02 adds the
canonical project/artifact/evidence owner (``project_matching.py``), and MD-M1-T03
completes the domain verticals with the canonical topic/subtopic/operation/event owner
(``topic_matching.py``). Every owner is bound to the facade by object identity.
Importing this package has no side effects and never imports ``classifier.py``.
"""

from __future__ import annotations
