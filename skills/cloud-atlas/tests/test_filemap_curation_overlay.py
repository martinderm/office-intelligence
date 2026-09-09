"""B2c1 contract tests for tracked Cloud Atlas curation overlays."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import convert_cloud_docs as convert
import gen_filemap as filemap
from core.curation import CURATION_SCHEMA_URI, CurationOverlayError, load_curation_overlay


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class FilemapCurationOverlayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cloud = self.root / "data" / "cloud" / "CURATED"
        self.cloud.mkdir(parents=True)
        self.output = self.root / "memory" / "cloud" / "projects" / "curated"
        self.catalog = self.root / "memory" / "references" / "projects"
        self.catalog.mkdir(parents=True)
        (self.root / "AGENTS.md").write_text("# workspace\n", encoding="utf-8")
        self.write_catalog()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_catalog(self, curation_json: str | None = None) -> None:
        storage = {
            "scan_dir": "data/cloud/CURATED",
            "output_json": "memory/cloud/projects/curated/filemap.json",
            "output_md": "memory/cloud/projects/curated/filemap.md",
            "output_dir": "memory/cloud/projects/curated",
        }
        if curation_json is not None:
            storage["curation_json"] = curation_json
        (self.catalog / "projects.json").write_text(json.dumps([{
            "id": "curated", "title": "Curated", "cloud_sync": {"default": storage},
        }]), encoding="utf-8")

    def overlay(self, entries: dict, **overrides: object) -> Path:
        doc = {
            "$schema": CURATION_SCHEMA_URI,
            "schema_version": 1,
            "kind": "cloud-filemap-curation",
            "scope": "project",
            "target": {"id": "curated"},
            "storage_id": "default",
            "source_receipt": {"sha256": sha("source filemap"), "file_count": len(entries)},
            "entries": entries,
        }
        doc.update(overrides)
        path = self.root / "curation" / "filemap-curation.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc), encoding="utf-8")
        self.write_catalog("curation/filemap-curation.json")
        return path

    def run_generator(self) -> int:
        with mock.patch.object(filemap.sys, "argv", [
            "gen_filemap.py", "--project-id", "curated", "--workspace-root", str(self.root),
        ]), contextlib.redirect_stderr(io.StringIO()):
            return filemap.main()

    def run_convert(self) -> int:
        result = {
            "success": True, "markdown_body": "# Converted\n", "ocr_applied": False,
            "ocr_policy": "disabled", "derivative_path": None, "derivative_sha256": None,
            "conversion_method": "markitdown-direct", "potential_quality_loss": None,
            "new_src_sha256": None, "new_src_size": None, "new_src_mtime": None,
            "error": None,
        }
        source_rel = "data/cloud/CURATED/contract.pdf"
        with mock.patch.object(convert, "run_conversion_tasks", return_value={source_rel: result}), \
             mock.patch.object(convert.sys, "argv", [
                 "convert_cloud_docs.py", "--project-id", "curated", "--workspace-root", str(self.root),
             ]), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return convert.main()

    def entry(self, name: str, content: str, **curated: object) -> tuple[str, dict]:
        source = self.cloud / name
        source.write_text(content, encoding="utf-8")
        key = f"data/cloud/CURATED/{name}"
        value: dict[str, object] = {"source_sha256": sha(content)}
        value.update(curated)
        return key, value

    def test_absent_overlay_keeps_legacy_generation(self) -> None:
        self.entry("plain.txt", "plain")
        self.assertEqual(0, self.run_generator())
        data = json.loads((self.output / "filemap.json").read_text(encoding="utf-8"))
        self.assertEqual("-", data["files"]["data/cloud/CURATED/plain.txt"]["description"])

    def test_valid_overlay_wins_over_old_filemap_and_preserves_technical_fields(self) -> None:
        key, entry = self.entry("contract.pdf", "contract", description="Tracked description", custom={"category": "legal"})
        self.overlay({key: entry})
        self.output.mkdir(parents=True)
        (self.output / "filemap.json").write_text(json.dumps({"files": {
            key: {"description": "Random old description", "category": "old", "sha256": "bad"}
        }}), encoding="utf-8")
        self.assertEqual(0, self.run_generator())
        got = json.loads((self.output / "filemap.json").read_text(encoding="utf-8"))["files"][key]
        self.assertEqual("Tracked description", got["description"])
        self.assertEqual("legal", got["category"])
        self.assertEqual(sha("contract"), got["sha256"])

    def test_active_overlay_without_entry_drops_old_description_and_custom_keys(self) -> None:
        key, _ = self.entry("contract.pdf", "contract")
        self.overlay({})
        self.output.mkdir(parents=True)
        (self.output / "filemap.json").write_text(json.dumps({"files": {
            key: {"description": "Stale description", "category": "stale", "sha256": "bad"}
        }}), encoding="utf-8")
        self.assertEqual(0, self.run_generator())
        got = json.loads((self.output / "filemap.json").read_text(encoding="utf-8"))["files"][key]
        self.assertEqual("-", got["description"])
        self.assertNotIn("category", got)

    def test_active_overlay_entry_drops_old_custom_keys_it_omits(self) -> None:
        key, entry = self.entry("contract.pdf", "contract", description="Tracked description")
        self.overlay({key: entry})
        self.output.mkdir(parents=True)
        (self.output / "filemap.json").write_text(json.dumps({"files": {
            key: {"description": "Stale description", "category": "stale", "sha256": "bad"}
        }}), encoding="utf-8")
        self.assertEqual(0, self.run_generator())
        got = json.loads((self.output / "filemap.json").read_text(encoding="utf-8"))["files"][key]
        self.assertEqual("Tracked description", got["description"])
        self.assertNotIn("category", got)

    def test_absent_overlay_retains_legacy_curation_from_old_filemap(self) -> None:
        key, _ = self.entry("contract.pdf", "contract")
        self.output.mkdir(parents=True)
        (self.output / "filemap.json").write_text(json.dumps({"files": {
            key: {"description": "Legacy description", "category": "legacy", "sha256": "bad"}
        }}), encoding="utf-8")
        self.assertEqual(0, self.run_generator())
        got = json.loads((self.output / "filemap.json").read_text(encoding="utf-8"))["files"][key]
        self.assertEqual("Legacy description", got["description"])
        self.assertEqual("legacy", got["category"])

    def test_convert_and_generator_apply_same_overlay_semantics(self) -> None:
        key, entry = self.entry("contract.pdf", "contract", description="Tracked description", custom={"reviewed": True})
        self.overlay({key: entry})
        self.assertEqual(0, self.run_convert())
        converted = json.loads((self.output / "filemap.json").read_text(encoding="utf-8"))["files"][key]
        self.assertEqual(("Tracked description", True), (converted["description"], converted["reviewed"]))
        self.assertEqual(0, self.run_generator())
        generated = json.loads((self.output / "filemap.json").read_text(encoding="utf-8"))["files"][key]
        self.assertEqual(("Tracked description", True), (generated["description"], generated["reviewed"]))

    def test_unique_sha_renamed_entry_is_continued(self) -> None:
        key, entry = self.entry("renamed.pdf", "same", description="Moved")
        old_key = "data/cloud/CURATED/old-name.pdf"
        self.overlay({old_key: entry})
        self.assertEqual(0, self.run_generator())
        data = json.loads((self.output / "filemap.json").read_text(encoding="utf-8"))
        self.assertEqual("Moved", data["files"][key]["description"])

    def test_ambiguous_sha_fails_before_output_write(self) -> None:
        self.entry("renamed.pdf", "same")
        first = {"source_sha256": sha("same"), "description": "first"}
        second = {"source_sha256": sha("same"), "description": "second"}
        self.overlay({"data/cloud/CURATED/old-a.pdf": first, "data/cloud/CURATED/old-b.pdf": second})
        self.output.mkdir(parents=True)
        destination = self.output / "filemap.json"
        destination.write_text("unchanged", encoding="utf-8")
        self.assertEqual(1, self.run_generator())
        self.assertEqual("unchanged", destination.read_text(encoding="utf-8"))

    def test_invalid_overlay_variants_fail_closed_without_converter_output(self) -> None:
        self.entry("contract.pdf", "contract")
        variants = [
            ({}, "missing"),
            ({"$schema": "invalid"}, "schema"),
            ({"target": {"id": "other"}}, "identity"),
            ({"entries": {"../escape.pdf": {"description": "no"}}}, "path"),
            ({"entries": {"data/cloud/CURATED/contract.pdf": {"custom": {"size": "no"}}}}, "generated"),
        ]
        for overrides, label in variants:
            with self.subTest(label=label):
                if label == "missing":
                    self.write_catalog("curation/missing.json")
                else:
                    path = self.overlay({})
                    document = json.loads(path.read_text(encoding="utf-8"))
                    document.update(overrides)
                    path.write_text(json.dumps(document), encoding="utf-8")
                self.assertEqual(1, self.run_convert())
                self.assertFalse((self.output / "filemap.json").exists())
                self.assertFalse((self.output / "contract.md").exists())

    def test_malformed_json_and_identity_are_rejected_by_loader(self) -> None:
        path = self.root / "curation" / "bad.json"
        path.parent.mkdir(parents=True)
        path.write_text("{", encoding="utf-8")
        with self.assertRaises(CurationOverlayError):
            load_curation_overlay(self.root, "curation/bad.json", scope="project", target_id="curated", storage_id="default", scan_dir="data/cloud/CURATED")
        path.write_text(json.dumps({}), encoding="utf-8")
        with self.assertRaises(CurationOverlayError):
            load_curation_overlay(self.root, "curation/bad.json", scope="project", target_id="curated", storage_id="default", scan_dir="data/cloud/CURATED")

    def test_subtopic_storage_identity_is_routed(self) -> None:
        topics = self.root / "memory" / "references" / "topics"
        topics.mkdir(parents=True)
        (topics / "topics.json").write_text(json.dumps([{
            "id": "topic", "subtopics": [{"id": "sub", "status": "active", "cloud_sync": {
                "store": {"scan_dir": "data/cloud/CURATED", "output_json": "x.json", "output_md": "x.md", "output_dir": "x", "curation_json": "curation/filemap-curation.json"}
            }}]
        }]), encoding="utf-8")
        configs = filemap.resolve_all_sync_configs(str(self.root), "topic", True, "store", "sub")
        self.assertEqual("curation/filemap-curation.json", configs["store"]["curation_json"])


if __name__ == "__main__":
    unittest.main()
