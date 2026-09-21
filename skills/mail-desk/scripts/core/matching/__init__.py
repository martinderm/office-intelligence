"""Domain-oriented matching modules for the mail-desk classifier (FR-13 / MD-M1).

MD-M1-T01 establishes this package with the canonical date parser and the reusable
ambiguity policy owner that the ``core.classifier`` facade composes. The project and
topic matching modules arrive with MD-M1-T02/T03 and are intentionally absent here.
Importing this package has no side effects and never imports ``classifier.py``.
"""

from __future__ import annotations
