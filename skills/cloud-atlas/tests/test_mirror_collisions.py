"""B2d7 regression tests for deterministic Markdown mirror planning."""

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import convert_cloud_docs
import gen_filemap
from core.mirror_paths import plan_markdown_mirrors


class MirrorPlanningTests(unittest.TestCase):
    scan_dir = "data/cloud/SOURCE"
    output_dir = "memory/cloud/projects/example"

    def test_noncolliding_sources_keep_legacy_paths(self):
        planned = plan_markdown_mirrors(
            ["data/cloud/SOURCE/notes.pdf", "data/cloud/SOURCE/sub/report.docx"],
            self.scan_dir,
            self.output_dir,
        )
        self.assertEqual("memory/cloud/projects/example/notes.md", planned["data/cloud/SOURCE/notes.pdf"])
        self.assertEqual("memory/cloud/projects/example/sub/report.md", planned["data/cloud/SOURCE/sub/report.docx"])

    def test_custom_extension_noncollision_uses_its_legacy_stem_path(self):
        source = "data/cloud/SOURCE/notes.custom"
        self.assertEqual({}, plan_markdown_mirrors([source], self.scan_dir, self.output_dir))
        planned = plan_markdown_mirrors(
            [source], self.scan_dir, self.output_dir,
            supported_extensions=["custom"],
        )
        self.assertEqual("memory/cloud/projects/example/notes.md", planned[source])

    def test_same_stem_multiple_extensions_disambiguate_every_member(self):
        sources = [
            "data/cloud/SOURCE/contract.doc",
            "data/cloud/SOURCE/contract.pdf",
            "data/cloud/SOURCE/contract.docx",
        ]
        planned = plan_markdown_mirrors(sources, self.scan_dir, self.output_dir)
        self.assertEqual(
            {
                "data/cloud/SOURCE/contract.doc": "memory/cloud/projects/example/contract.doc.md",
                "data/cloud/SOURCE/contract.pdf": "memory/cloud/projects/example/contract.pdf.md",
                "data/cloud/SOURCE/contract.docx": "memory/cloud/projects/example/contract.docx.md",
            },
            planned,
        )

    def test_custom_extension_same_stem_disambiguates_every_member(self):
        custom = "data/cloud/SOURCE/invoice.custom"
        pdf = "data/cloud/SOURCE/invoice.pdf"
        planned = plan_markdown_mirrors(
            [custom, pdf], self.scan_dir, self.output_dir,
            supported_extensions=[".custom", ".PDF"],
        )
        self.assertEqual("memory/cloud/projects/example/invoice.custom.md", planned[custom])
        self.assertEqual("memory/cloud/projects/example/invoice.pdf.md", planned[pdf])

    def test_casefold_collision_detection_and_order_are_stable(self):
        sources = ["data/cloud/SOURCE/Report.pdf", "data/cloud/SOURCE/report.DOCX"]
        planned = plan_markdown_mirrors(sources, self.scan_dir, self.output_dir)
        self.assertEqual(planned, plan_markdown_mirrors(list(reversed(sources)), self.scan_dir, self.output_dir))
        self.assertEqual("memory/cloud/projects/example/Report.pdf.md", planned[sources[0]])
        self.assertEqual("memory/cloud/projects/example/report.DOCX.md", planned[sources[1]])

    def test_unresolvable_case_only_target_collision_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "not unique"):
            plan_markdown_mirrors(
                ["data/cloud/SOURCE/Report.PDF", "data/cloud/SOURCE/report.pdf"],
                self.scan_dir,
                self.output_dir,
            )

    def test_noncolliding_explicit_custom_path_is_preserved(self):
        source = "data/cloud/SOURCE/notes.pdf"
        planned = plan_markdown_mirrors(
            [source], self.scan_dir, self.output_dir,
            {source: "memory/cloud/projects/example/curated/notes.md"},
        )
        self.assertEqual("memory/cloud/projects/example/curated/notes.md", planned[source])

    def test_collision_does_not_resurrect_a_legacy_or_custom_stem_path(self):
        pdf = "data/cloud/SOURCE/invoice.pdf"
        docx = "data/cloud/SOURCE/invoice.docx"
        planned = plan_markdown_mirrors(
            [pdf, docx], self.scan_dir, self.output_dir,
            {pdf: "memory/cloud/projects/example/invoice.md"},
        )
        self.assertEqual("memory/cloud/projects/example/invoice.pdf.md", planned[pdf])
        self.assertEqual("memory/cloud/projects/example/invoice.docx.md", planned[docx])


class MirrorCollisionConverterFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.cloud_dir = self.root / "data" / "cloud" / "SOURCE"
        self.output_dir = self.root / "memory" / "cloud" / "projects" / "example"
        self.cloud_dir.mkdir(parents=True)
        self.output_dir.mkdir(parents=True)
        projects = self.root / "memory" / "references" / "projects"
        projects.mkdir(parents=True)
        (projects / "projects.json").write_text(json.dumps([{
            "id": "example",
            "title": "Example",
            "cloud_sync": {"default": {
                "scan_dir": "data/cloud/SOURCE",
                "output_dir": "memory/cloud/projects/example",
                "output_json": "memory/cloud/projects/example/filemap.json",
                "output_md": "memory/cloud/projects/example/filemap.md",
            }},
        }]), encoding="utf-8")
        (self.root / "AGENTS.md").write_text("# fixture\n", encoding="utf-8")
        os.environ["CLOUD_ATLAS_WORKSPACE_ROOT"] = str(self.root)
        self.old_cwd = os.getcwd()
        os.chdir(self.root)

    def tearDown(self):
        os.chdir(self.old_cwd)
        os.environ.pop("CLOUD_ATLAS_WORKSPACE_ROOT", None)
        self.temporary_directory.cleanup()

    @staticmethod
    def fake_conversion(tasks, **_kwargs):
        return {
            task["src_rel"]: {
                "success": True,
                "markdown_body": f"# {Path(task['src_abs']).name}\n",
                "conversion_method": "fixture-converter",
            }
            for task in tasks
        }

    def test_converter_and_generator_share_disambiguated_paths_and_cleanup_legacy(self):
        pdf = self.cloud_dir / "invoice.pdf"
        docx = self.cloud_dir / "invoice.docx"
        pdf.write_bytes(b"invoice pdf source")
        docx.write_bytes(b"invoice docx source")
        old_ambiguous = self.output_dir / "invoice.md"
        old_ambiguous.write_text("old ambiguous mirror", encoding="utf-8")

        with mock.patch.object(convert_cloud_docs, "run_conversion_tasks", side_effect=self.fake_conversion), \
             mock.patch.object(convert_cloud_docs.sys, "argv", ["convert_cloud_docs.py", "--project-id", "example"]):
            self.assertEqual(0, convert_cloud_docs.main())

        pdf_mirror = self.output_dir / "invoice.pdf.md"
        docx_mirror = self.output_dir / "invoice.docx.md"
        self.assertTrue(pdf_mirror.is_file())
        self.assertTrue(docx_mirror.is_file())
        self.assertFalse(old_ambiguous.exists(), "old ambiguous path is cleaned only after the batch succeeds")
        for source, mirror in ((pdf, pdf_mirror), (docx, docx_mirror)):
            metadata, _body = convert_cloud_docs.parse_markdown_file(str(mirror))
            self.assertEqual(
                f"data/cloud/SOURCE/{source.name}", metadata["source_uri"],
            )
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), metadata["source_sha256"])

        with mock.patch.object(gen_filemap.sys, "argv", ["gen_filemap.py", "--project-id", "example"]):
            self.assertEqual(0, gen_filemap.main())
        filemap = json.loads((self.output_dir / "filemap.json").read_text(encoding="utf-8"))
        self.assertEqual(
            "memory/cloud/projects/example/invoice.pdf.md",
            filemap["files"]["data/cloud/SOURCE/invoice.pdf"]["markdown_mirror"],
        )
        self.assertEqual(
            "memory/cloud/projects/example/invoice.docx.md",
            filemap["files"]["data/cloud/SOURCE/invoice.docx"]["markdown_mirror"],
        )

    def test_converter_plans_a_noncolliding_custom_extension(self):
        source = self.cloud_dir / "notes.custom"
        source.write_bytes(b"custom extension source")

        with mock.patch.object(convert_cloud_docs, "run_conversion_tasks", side_effect=self.fake_conversion), \
             mock.patch.object(convert_cloud_docs.sys, "argv", [
                 "convert_cloud_docs.py", "--project-id", "example", "--extensions", ".custom",
             ]):
            self.assertEqual(0, convert_cloud_docs.main())

        self.assertTrue((self.output_dir / "notes.md").is_file())

    def test_converter_disambiguates_custom_extension_same_stem_collision(self):
        custom = self.cloud_dir / "invoice.custom"
        pdf = self.cloud_dir / "invoice.pdf"
        custom.write_bytes(b"custom extension source")
        pdf.write_bytes(b"pdf source")

        with mock.patch.object(convert_cloud_docs, "run_conversion_tasks", side_effect=self.fake_conversion), \
             mock.patch.object(convert_cloud_docs.sys, "argv", [
                 "convert_cloud_docs.py", "--project-id", "example", "--extensions", ".custom,.pdf",
             ]):
            self.assertEqual(0, convert_cloud_docs.main())

        self.assertTrue((self.output_dir / "invoice.custom.md").is_file())
        self.assertTrue((self.output_dir / "invoice.pdf.md").is_file())


class FilemapMirrorUniquenessTests(unittest.TestCase):
    def test_validator_rejects_case_insensitive_duplicate_mirrors(self):
        entry = {
            "version": "N/A", "mtime": "2026-09-10 08:00:00", "size": "1 B",
            "sha256": "a" * 64, "description": "-", "conversion_status": "converted",
        }
        filemap = {
            "$schema": gen_filemap.FILEMAP_SCHEMA_URI,
            "schema_version": gen_filemap.FILEMAP_SCHEMA_VERSION,
            "kind": "cloud-filemap", "scope": "project", "storage_id": "default",
            "project": "example", "project_title": "Example", "scan_dir": "data/cloud/SOURCE",
            "output_dir": "memory/cloud/projects/example", "updated_at": "2026-09-10 08:00:00",
            "files": {
                "data/cloud/SOURCE/a.pdf": {**entry, "markdown_mirror": "memory/cloud/projects/example/invoice.md"},
                "data/cloud/SOURCE/b.docx": {**entry, "markdown_mirror": "memory/cloud/projects/example/INVOICE.md"},
            },
        }
        with self.assertRaisesRegex(ValueError, "duplicate markdown_mirror"):
            gen_filemap.validate_filemap(filemap)


if __name__ == "__main__":
    unittest.main()
