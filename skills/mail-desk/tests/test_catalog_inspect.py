"""Failing tests for the catalog inspector (catalog_inspect.py).

Pins the read-only inspector contract: canonical envelopes (no bare SystemExit),
field selection honored in single-entry mode, and bounded output.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

import catalog_inspect as inspector  # noqa: E402


class ExtractItemsTests(unittest.TestCase):
    """Broken catalog payloads surface as canonical errors, not SystemExit."""

    def test_extract_items_returns_list_payload(self) -> None:
        self.assertEqual(inspector._extract_items("topics", [{"id": "a"}]), [{"id": "a"}])

    def test_extract_items_returns_topics_object(self) -> None:
        self.assertEqual(inspector._extract_items("topics", {"topics": [{"id": "a"}]}), [{"id": "a"}])

    def test_broken_payload_raises_catalog_error_not_systemexit(self) -> None:
        with self.assertRaises(inspector.CatalogInspectError):
            inspector._extract_items("topics", {"unrelated": True})


class SelectEntryTests(unittest.TestCase):
    """--fields applies in single-entry mode too (bounded output)."""

    def test_selected_entry_respects_fields(self) -> None:
        entry = {"id": "meshe", "title": "MESHE", "mailbox_folder": "P/M", "workpackages": [{"x": 1}], "keywords": ["k"]}
        out = inspector._select_entry(entry, fields=("id", "mailbox_folder"))
        self.assertEqual(out, {"id": "meshe", "mailbox_folder": "P/M"})
        self.assertNotIn("workpackages", out)

    def test_selected_entry_defaults_keep_small_fields(self) -> None:
        entry = {"id": "a", "title": "T", "mailbox_folder": "F", "typical_subject_patterns": ["p"], "workpackages": [{"big": True}]}
        out = inspector._select_entry(entry, fields=inspector.DEFAULT_FIELDS)
        self.assertNotIn("workpackages", out)


class NoSystemExitTests(unittest.TestCase):
    """Errors use canonical envelopes; no bare SystemExit remains."""

    def test_module_has_no_bare_systemexit(self) -> None:
        source = Path(inspector.__file__).read_text(encoding="utf-8")
        self.assertNotIn("raise SystemExit", source, "inspector errors must emit canonical envelopes")


if __name__ == "__main__":
    unittest.main()