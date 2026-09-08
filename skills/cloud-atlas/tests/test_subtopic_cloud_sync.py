"""FR-03b2a contracts for explicit topic-subtopic cloud synchronization."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CONVERT = load("convert_cloud_docs")
FILEMAP = load("gen_filemap")
SYNC = load("sync_project_cloud")


class SubtopicCloudSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.catalog = self.root / "memory" / "references" / "topics"
        self.catalog.mkdir(parents=True)
        self.write_catalog()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_catalog(self, subtopics: list[dict] | None = None) -> None:
        alpha = {
            "id": "alpha", "title": "Alpha", "status": "active",
            "cloud_sync": {
                "sub-store": {
                    "scan_dir": "data/cloud/SUB",
                    "output_json": "memory/cloud/topics/topic/alpha/filemap.json",
                    "output_md": "memory/cloud/topics/topic/alpha/filemap.md",
                    "output_dir": "memory/cloud/topics/topic/alpha/mirrors",
                    "last_synced_at": "old-alpha",
                }
            },
        }
        document = [{
            "id": "topic", "title": "Topic", "cloud_sync": {
                "parent-store": {"scan_dir": "data/cloud/PARENT", "last_synced_at": "parent-old"}
            },
            "subtopics": subtopics if subtopics is not None else [alpha],
        }]
        (self.catalog / "topics.json").write_text(json.dumps(document), encoding="utf-8")

    def test_exact_active_subtopic_uses_only_nested_storage(self) -> None:
        for module in (CONVERT, FILEMAP):
            with self.subTest(module=module.__name__):
                configs = module.resolve_all_sync_configs(str(self.root), "topic", True, None, "alpha")
                self.assertEqual(["sub-store"], list(configs))
                self.assertEqual("data/cloud/SUB", configs["sub-store"]["scan_dir"])
                self.assertEqual("alpha", configs["sub-store"]["subtopic_id"])

    def test_unknown_inactive_duplicate_missing_and_storage_id_fail_closed(self) -> None:
        inactive = {"id": "inactive", "title": "Inactive", "status": "inactive", "cloud_sync": {}}
        missing = {"id": "missing", "title": "Missing", "status": "active"}
        duplicate = {"id": "alpha", "title": "Duplicate", "status": "active", "cloud_sync": {}}
        self.write_catalog([json.loads(json.dumps({
            "id": "alpha", "title": "Alpha", "status": "active", "cloud_sync": {
                "sub-store": {"scan_dir": "data/cloud/SUB", "output_json": "a.json", "output_md": "a.md", "output_dir": "mirrors"}
            }})), inactive, missing, duplicate])
        for subtopic in ("unknown", "inactive", "missing", "alpha"):
            with self.subTest(subtopic=subtopic):
                with self.assertRaises(ValueError):
                    CONVERT.resolve_all_sync_configs(str(self.root), "topic", True, None, subtopic)
        self.write_catalog()
        with self.assertRaises(ValueError):
            FILEMAP.resolve_all_sync_configs(str(self.root), "topic", True, "absent", "alpha")

    def test_timestamp_updates_only_exact_nested_storage(self) -> None:
        self.assertIsNone(FILEMAP.update_config_last_synced_at(str(self.root), "topic", True, "sub-store", "alpha"))
        data = json.loads((self.catalog / "topics.json").read_text(encoding="utf-8"))
        topic = data[0]
        self.assertEqual("parent-old", topic["cloud_sync"]["parent-store"]["last_synced_at"])
        self.assertNotEqual("old-alpha", topic["subtopics"][0]["cloud_sync"]["sub-store"]["last_synced_at"])

    def test_json_argument_and_exact_nested_path_contract(self) -> None:
        stdout = io.StringIO()
        with mock.patch.object(CONVERT.sys, "argv", ["convert_cloud_docs.py", "--project-id", "x", "--subtopic-id", "alpha", "--json"]), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(2, CONVERT.main())
        self.assertEqual("arguments", json.loads(stdout.getvalue())["error"]["phase"])

        (self.root / "data" / "cloud" / "SUB").mkdir(parents=True)
        stdout = io.StringIO()
        with mock.patch.object(CONVERT.sys, "argv", ["convert_cloud_docs.py", "--topic-id", "topic", "--subtopic-id", "alpha", "--workspace-root", str(self.root), "--json"]), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(0, CONVERT.main())
        converted = json.loads(stdout.getvalue())["data"]["storages"][0]
        self.assertEqual("data/cloud/SUB", converted["scan_dir"])
        self.assertEqual("memory/cloud/topics/topic/alpha/mirrors", converted["output_dir"])
        self.assertEqual("memory/cloud/topics/topic/alpha/filemap.json", converted["output_json"])
        self.assertTrue((self.root / converted["output_json"]).is_file())

        stdout = io.StringIO()
        with mock.patch.object(FILEMAP.sys, "argv", ["gen_filemap.py", "--topic-id", "topic", "--subtopic-id", "alpha", "--workspace-root", str(self.root), "--json"]), contextlib.redirect_stdout(stdout):
            self.assertEqual(0, FILEMAP.main())
        result = json.loads(stdout.getvalue())["data"]
        target = result["target"]
        self.assertEqual({"kind": "subtopic", "topic_id": "topic", "subtopic_id": "alpha"}, target)
        storage = result["storages"][0]
        self.assertEqual("memory/cloud/topics/topic/alpha/filemap.json", storage["output_json"])
        self.assertEqual("memory/cloud/topics/topic/alpha/filemap.md", storage["output_md"])
        self.assertTrue((self.root / storage["output_json"]).is_file())
        self.assertTrue((self.root / storage["output_md"]).is_file())

    def test_subtopic_path_overrides_are_rejected_by_both_clis(self) -> None:
        cases = (
            (CONVERT, "convert_cloud_docs.py", ("--cloud-dir", "elsewhere")),
            (CONVERT, "convert_cloud_docs.py", ("--output-dir", "elsewhere")),
            (CONVERT, "convert_cloud_docs.py", ("--filemap-json", "elsewhere.json")),
            (FILEMAP, "gen_filemap.py", ("--scan-dir", "elsewhere")),
            (FILEMAP, "gen_filemap.py", ("--output-json", "elsewhere.json")),
            (FILEMAP, "gen_filemap.py", ("--output-md", "elsewhere.md")),
        )
        for module, program, override in cases:
            with self.subTest(override=override[0]):
                stdout = io.StringIO()
                argv = [program, "--topic-id", "topic", "--subtopic-id", "alpha", *override, "--json"]
                with mock.patch.object(module.sys, "argv", argv), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(2, module.main())
                envelope = json.loads(stdout.getvalue())
                self.assertEqual("arguments", envelope["error"]["phase"])

    def test_orchestrator_forwards_subtopic_to_both_steps(self) -> None:
        commands: list[list[str]] = []

        def record(_step, command, _json_mode):
            commands.append(command)
            return mock.Mock(returncode=0), None

        stdout = io.StringIO()
        with mock.patch.object(SYNC.sys, "argv", ["sync_project_cloud.py", "--topic-id", "topic", "--subtopic-id", "alpha", "--json"]), mock.patch.object(SYNC, "run_step", side_effect=record), contextlib.redirect_stdout(stdout):
            self.assertEqual(0, SYNC.main())
        self.assertEqual(2, len(commands))
        self.assertTrue(all("--subtopic-id" in command and "alpha" in command for command in commands))
        self.assertEqual("subtopic", json.loads(stdout.getvalue())["data"]["target"]["kind"])


if __name__ == "__main__":
    unittest.main()
