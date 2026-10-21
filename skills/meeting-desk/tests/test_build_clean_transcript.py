"""Offline regression tests; only synthetic transcripts in temporary workspaces.

The pure correction engine is tested directly. Publication is isolated behind
``_delegate`` (the canonical writer boundary): these tests never write a second
register. Real builder→writer→file integration is covered by
``test_ingest_meeting.py``.
"""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/build_clean_transcript.py"
spec = importlib.util.spec_from_file_location("builder", SCRIPT)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class BuilderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="meeting-builder-test-")
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve()
        self.zone = self.workspace / "memory/evidence/meetings"
        self.zone.mkdir(parents=True)
        self.ref = "memory/references/topics/example/index.md"
        reference = self.workspace / self.ref
        reference.parent.mkdir(parents=True)
        reference.write_text("# Synthetic entity reference\n", encoding="utf-8")
        self.raw = self.zone / "example.transcript.raw.md"
        self.corr = self.zone / "example.corrections.json"
        self.out = self.zone / "example.transcript.md"
        self.raw.write_bytes(b"---\ntitle: raw\n---\nSpeaker: Music in G, Ki, Unknown.\n")
        self.data = {"schema": builder.SCHEMA, "pairs": [self.pair()]}
        self.save()

    def pair(self, orig="Music in G", corr="USAGE-NG"):
        return {"orig": orig, "corr": corr, "cat": "entity", "ref": self.ref}

    def save(self):
        self.corr.write_text(json.dumps(self.data), encoding="utf-8")

    def args(self):
        return ["--workspace", str(self.workspace), "--raw", str(self.raw),
                "--corrections", str(self.corr), "--out", str(self.out),
                "--meeting-id", "manual-synthetic", "--lease-id", "test-owner", "--json"]

    def call(self, args=None):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = builder.main(self.args() if args is None else args)
        result = json.loads(stream.getvalue())
        self.assertEqual(set(result), {"action", "success", "state", "message", "data", "error"})
        return code, result

    def write_through(self, envelope=None):
        """Fake the canonical writer: persist the deterministic bytes, report success."""
        def delegate(workspace, cleaned, out_path, meeting_id, lease_id,
                     conversation_id, lock_root):
            if envelope is None or envelope.get("success"):
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_bytes(cleaned.encode("utf-8"))
            return envelope or {"success": True, "state": "Applied", "message": "published",
                                "data": {"changed": True}, "error": None}
        return delegate

    def build(self, extra=(), envelope=None):
        with patch.object(builder, "_delegate", side_effect=self.write_through(envelope)):
            code, result = self.call(self.args() + list(extra))
        self.assertEqual(code, 0, result)
        return result

    def test_build_rebuild_and_check_preserve_sources(self):
        before = (self.raw.read_bytes(), self.corr.read_bytes())
        result = self.build()
        first = self.out.read_bytes()
        self.assertEqual(result["data"]["corrections_count"], 1)
        self.assertEqual(result["action"], "build_clean_transcript")
        self.build()
        self.assertEqual(first, self.out.read_bytes())
        self.assertEqual(self.call(self.args() + ["--check"])[0], 0)
        self.assertEqual(before, (self.raw.read_bytes(), self.corr.read_bytes()))

    def test_orphan_publication_is_refused(self):
        rejected = {"success": False, "state": "Rejected", "message": "orphan refused",
                    "data": {}, "error": {"type": "Rejected", "message": "orphan refused"}}
        with patch.object(builder, "_delegate", side_effect=self.write_through(rejected)):
            code, result = self.call(self.args())
        self.assertEqual(code, 2, result)
        self.assertEqual(result["state"], "Rejected")
        self.assertFalse(self.out.exists())

    def test_missing_meeting_id_and_lease_fail_before_delegation(self):
        args = self.args()
        args[args.index("--meeting-id") + 1] = ""
        code, result = self.call(args)
        self.assertEqual(code, 2)
        self.assertIn("--meeting-id", result["message"])
        args = self.args()
        args[args.index("--lease-id") + 1] = ""
        code, result = self.call(args)
        self.assertEqual(code, 2)
        self.assertIn("lease", result["message"])

    def test_recovery_required_maps_to_exit_three(self):
        recovery = {"success": False, "state": "RecoveryRequired",
                    "message": "open journal", "data": {},
                    "error": {"type": "RecoveryRequired", "message": "open journal"}}
        with patch.object(builder, "_delegate", side_effect=self.write_through(recovery)):
            code, result = self.call(self.args())
        self.assertEqual(code, 3)
        self.assertEqual(result["state"], "RecoveryRequired")

    def test_input_aliases_rejected_even_with_force(self):
        for source in (self.raw, self.corr):
            with self.subTest(source=source.name):
                before = source.read_bytes()
                args = self.args()
                args[args.index("--out") + 1] = str(source)
                self.assertEqual(self.call(args + ["--force"])[0], 2)
                self.assertEqual(source.read_bytes(), before)

    def test_hardlink_alias_rejected(self):
        try:
            self.out.hardlink_to(self.raw)
        except OSError:
            self.skipTest("hardlinks unavailable")
        self.assertEqual(self.call(self.args() + ["--force"])[0], 2)

    def test_outside_zone_and_bundle_rejected(self):
        bundle = SCRIPT.parents[3]
        with self.assertRaisesRegex(ValueError, "bundle"):
            builder._workspace_path(bundle, bundle / "data/forbidden.json")
        for target in (self.workspace / "outside.md", SCRIPT.parent / "forbidden.json"):
            args = self.args()
            args[args.index("--out") + 1] = str(target)
            self.assertEqual(self.call(args)[0], 2)
            self.assertFalse(target.exists())

    def test_symlink_escape_rejected(self):
        with tempfile.TemporaryDirectory(prefix="meeting-escape-") as external:
            link = self.zone / "escape"
            try:
                link.symlink_to(external, target_is_directory=True)
            except OSError:
                self.skipTest("symlinks unavailable")
            with self.assertRaises(ValueError):
                builder._workspace_path(self.workspace, link / "out.md")

    def test_byte_drift_detects_crlf(self):
        self.build()
        self.out.write_bytes(self.out.read_bytes().replace(b"\n", b"\r\n"))
        code, result = self.call(self.args() + ["--check"])
        self.assertEqual(code, 1)
        self.assertEqual(result["state"], "Drift")

    def test_check_is_read_only_and_does_not_delegate(self):
        self.build()
        with patch.object(builder, "_delegate") as delegate:
            self.assertEqual(self.call(self.args() + ["--check"])[0], 0)
        delegate.assert_not_called()

    def test_unicode_error_message_is_utf8_under_cp1252(self):
        unicode_dir = self.workspace / "\u4f1a\u8bae\U0001f680"
        unicode_dir.mkdir()
        out = unicode_dir / "missing.transcript.md"
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), "--workspace", str(self.workspace),
             "--raw", str(self.raw), "--corrections", str(self.corr),
             "--out", str(out), "--meeting-id", "x", "--check", "--json"],
            capture_output=True, env={**os.environ, "PYTHONIOENCODING": "cp1252",
                                      "PYTHONUTF8": "0"}, check=False)
        self.assertEqual(completed.returncode, 2, completed.stderr)
        envelope = json.loads(completed.stdout.decode("utf-8"))
        self.assertFalse(envelope["success"])
        self.assertNotIn(b"Traceback", completed.stderr)


        for field, value in (("ref", ""), ("orig", 123), ("cat", None)):
            with self.subTest(field=field):
                self.data["pairs"] = [self.pair()]
                self.data["pairs"][0][field] = value
                self.save()
                self.assertEqual(self.call()[0], 2)

    def test_non_workspace_refs_fail(self):
        for ref in ("meeting-intern", "memory/references/missing.md", "memory/references/../../outside.md"):
            self.data["pairs"][0]["ref"] = ref
            self.save()
            self.assertEqual(self.call()[0], 2)

    def test_invalid_regex_and_duplicate_rule_ids(self):
        self.data["regex_pairs"] = [{"pattern": "[", "corr": "x", "cat": "entity", "ref": self.ref}]
        self.save()
        self.assertEqual(self.call()[0], 2)
        self.data.pop("regex_pairs")
        rule = {"id": "R1", "pattern": "Ki", "repl": "KI", "note": "Spelling", "ref": self.ref}
        self.data["rules"] = [rule, rule]
        self.save()
        self.assertEqual(self.call()[0], 2)

    def test_no_generated_markup_is_rematched(self):
        data = {"pairs": [self.pair("Ki", "KI")],
                "unklar": [{"token": "KI", "why": "No reference"}]}
        body, result = builder._apply(data, "Ki")
        self.assertIn('„Ki“ → „KI“', body)
        self.assertNotIn("[unklar:", body)
        self.assertEqual(result["stats"]["unklar_count"], 0)

    def test_overlaps_and_zero_width_fail(self):
        for data in ({"pairs": [self.pair("Ki", "KI"), self.pair("Ki", "Other")]},
                     {"regex_pairs": [{"pattern": "(?=Ki)", "corr": "KI", "cat": "x", "ref": self.ref}]}):
            with self.assertRaises(ValueError):
                builder._apply(data, "Ki")

    def test_regex_unicode_and_counts(self):
        data = {"regex_pairs": [{"pattern": r"\bOok\b", "corr": "AUCEN", "cat": "entity", "ref": self.ref}]}
        body, result = builder._apply(data, '„Ook“ und Ook')
        self.assertEqual(result["stats"]["corrections_count"], 2)
        self.assertEqual(body.count("[korr:"), 2)

    def test_rules_only_change_original_spans(self):
        data = {"pairs": [self.pair("Music in G", "Ki")], "rules": [
            {"id": "R1", "pattern": r"\bKi\b", "repl": "KI", "note": "Spelling", "ref": self.ref}]}
        body, result = builder._apply(data, "Music in G Ki")
        self.assertIn('→ „Ki“', body)
        self.assertTrue(body.endswith(" KI"))
        self.assertEqual(result["stats"]["rules"]["R1"], 1)

    def test_yaml_and_table_escaping(self):
        title = 'A "quote" \\ path\nnext'
        row = builder._table_row(['a|b', '\\', 'next\nline', '<tag>'])
        self.assertEqual(row.count("|"), 5)
        self.assertIn("&#124;", row)
        self.assertIn("&#10;", row)
        header = builder._header("id", title, "", "", "raw.md", builder._apply({}, "")[1])
        title_line = next(line for line in header.splitlines() if line.startswith("title:"))
        self.assertEqual(json.loads(title_line.removeprefix("title: ")), title)
        self.assertIn("date: null\n", header)

    def test_internal_thematic_breaks_are_preserved(self):
        self.raw.write_text("Intro\n---\nMiddle\n---\nEnd\n", encoding="utf-8")
        self.build()
        self.assertTrue(self.out.read_text(encoding="utf-8").endswith("Intro\n---\nMiddle\n---\nEnd\n"))

    def test_io_and_cli_errors_are_envelopes(self):
        self.assertEqual(self.call(["--unknown"])[0], 2)
        self.corr.write_bytes(b"\xff")
        self.assertEqual(self.call()[0], 2)
        self.corr.unlink()
        self.assertEqual(self.call()[0], 2)

    def test_template_needs_lock_data_zone_and_no_open_journal(self):
        data_dir = self.workspace / "data"
        data_dir.mkdir()
        target = data_dir / "template.json"
        args = ["--workspace", str(self.workspace), "--init-template", str(target)]
        self.assertEqual(self.call(args)[0], 2)
        with patch.object(builder, "_require_lock"):
            self.assertEqual(self.call(args)[0], 0)
            self.assertEqual(self.call(args)[0], 2)
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), builder._template())

        other = self.workspace / "memory/evidence/meetings/not-data-template.json"
        code, _ = self.call(["--workspace", str(self.workspace), "--init-template",
                             str(other)])
        self.assertEqual(code, 2)
        self.assertFalse(other.exists())

        journal = self.workspace / "data/meeting-desk/journal/blocker.journal.json"
        journal.parent.mkdir(parents=True)
        journal.write_text("{}", encoding="utf-8")
        blocked = data_dir / "blocked.json"
        code, envelope = self.call(["--workspace", str(self.workspace), "--init-template",
                                    str(blocked)])
        self.assertEqual(code, 3)
        self.assertEqual(envelope["state"], "RecoveryRequired")
        self.assertFalse(blocked.exists())
        self.assertEqual(journal.read_text(encoding="utf-8"), "{}")


if __name__ == "__main__":
    unittest.main()
