"""Failure-mode tests for shared atomic mail-desk writers."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

from core import common  # noqa: E402


class CommonAtomicWriteTests(unittest.TestCase):
    def test_replace_failure_preserves_existing_json_and_cleans_staging_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "state.json"
            target.write_text('{"before": true}\n', encoding="utf-8")

            with patch.object(common.os, "replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    common.atomic_write_json(target, {"after": True})

            self.assertEqual('{"before": true}\n', target.read_text(encoding="utf-8"))
            self.assertEqual([target], list(Path(temp_dir).iterdir()))

    def test_json_and_jsonl_writers_use_stable_utf8_documents(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            json_target = Path(temp_dir) / "state.json"
            jsonl_target = Path(temp_dir) / "state.jsonl"

            common.atomic_write_json(json_target, {"name": "Grüße"})
            common.atomic_rewrite_jsonl(jsonl_target, ({"id": 1}, {"id": 2}))

            self.assertEqual({"name": "Grüße"}, json.loads(json_target.read_text(encoding="utf-8")))
            self.assertTrue(json_target.read_bytes().endswith(b"\n"))
            self.assertEqual(
                [{"id": 1}, {"id": 2}],
                [json.loads(line) for line in jsonl_target.read_text(encoding="utf-8").splitlines()],
            )


if __name__ == "__main__":
    unittest.main()
