"""Regression tests for tagged OCR PDFs, OCRmyPDF flags, and mirror retention.

Issue #50: an already OCR'd, tagged PDF with a small text layer must be treated
as a digital document (no forced OCR), OCRmyPDF must skip existing text pages by
default, and a failed conversion must never delete the mirror of a source that is
still inventoried.
"""

import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TESTS_DIR.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import convert_cloud_docs
import gen_filemap
from core.curation import (
    CURATION_SCHEMA_URI,
    GENERATED_FILEMAP_FIELDS,
    CurationOverlayError,
    load_curation_overlay,
    merge_curated_metadata,
)

FIXTURE = TESTS_DIR / "fixtures" / "tagged_ocrmypdf_small_text.pdf"
FIXTURE_SHA256 = "477c9ca5aa71b67e4a8699634828cad23f8650f013426473d521cf965d1f2d8b"


def _synthetic_pdf(extra_objects=b""):
    """Build a tiny uncompressed PDF body with optional structural markers."""
    return (
        b"%PDF-1.7\n"
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R " + extra_objects + b">>\nendobj\n"
        b"2 0 obj\n<< /Type /Pages /Kids [] /Count 0 >>\nendobj\n"
        b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"
    )


class FixtureDetectionTests(unittest.TestCase):
    def test_fixture_identity(self):
        self.assertTrue(FIXTURE.is_file())
        self.assertEqual(hashlib.sha256(FIXTURE.read_bytes()).hexdigest(), FIXTURE_SHA256)

    def test_fixture_analyze_pdf_structure_reports_tagged(self):
        structure = convert_cloud_docs.analyze_pdf_structure(str(FIXTURE))
        self.assertIs(structure["is_tagged"], True)

    def test_tagged_fixture_is_digital_with_and_without_passed_text(self):
        self.assertFalse(
            convert_cloud_docs.is_image_based_pdf(str(FIXTURE), "BAUFELD 1 / BAUFELD 2")
        )
        self.assertFalse(convert_cloud_docs.is_image_based_pdf(str(FIXTURE), ""))

    def test_byte_fallback_detects_uncompressed_tagged_pdf(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tagged = Path(temp_dir) / "tagged.pdf"
            tagged.write_bytes(_synthetic_pdf(
                b"/StructTreeRoot 3 0 R /MarkInfo << /Marked true >> "
            ))
            plain = Path(temp_dir) / "plain.pdf"
            plain.write_bytes(_synthetic_pdf())

            with mock.patch.object(convert_cloud_docs, "_load_pymupdf", return_value=(None, None)):
                self.assertIs(
                    convert_cloud_docs.analyze_pdf_structure(str(tagged))["is_tagged"], True
                )
                self.assertIs(
                    convert_cloud_docs.analyze_pdf_structure(str(plain))["is_tagged"], False
                )

    def test_missing_file_is_not_tagged(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            missing = Path(temp_dir) / "missing.pdf"
            structure = convert_cloud_docs.analyze_pdf_structure(str(missing))
            self.assertIs(structure["is_tagged"], False)


class CurationReservationTests(unittest.TestCase):
    """mirror_stale is derived generated state, never curation input."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / "data" / "cloud" / "CURATED").mkdir(parents=True)
        self.overlay_path = self.root / "curation" / "filemap-curation.json"
        self.overlay_path.parent.mkdir(parents=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    def write_overlay(self, custom):
        document = {
            "$schema": CURATION_SCHEMA_URI,
            "schema_version": 1,
            "kind": "cloud-filemap-curation",
            "scope": "project",
            "target": {"id": "curated"},
            "storage_id": "default",
            "source_receipt": {"sha256": "a" * 64, "file_count": 1},
            "entries": {
                "data/cloud/CURATED/tagged.pdf": {"custom": custom},
            },
        }
        self.overlay_path.write_text(json.dumps(document), encoding="utf-8")

    def load_overlay(self):
        return load_curation_overlay(
            self.root,
            "curation/filemap-curation.json",
            scope="project",
            target_id="curated",
            storage_id="default",
            scan_dir="data/cloud/CURATED",
        )

    def test_mirror_stale_is_reserved_and_rejected_by_the_loader(self):
        self.assertIn("mirror_stale", GENERATED_FILEMAP_FIELDS)
        for forged in ({"mirror_stale": True}, {"mirror_stale": False}):
            with self.subTest(forged=forged):
                self.write_overlay(forged)
                with self.assertRaises(CurationOverlayError):
                    self.load_overlay()

    def test_overlay_cannot_forge_or_erase_generated_mirror_stale(self):
        generated_stale = {
            "conversion_status": "conversion_required",
            "mirror_stale": True,
        }
        erased = merge_curated_metadata(
            {},
            generated_stale,
            {"custom": {"mirror_stale": False}},
            overlay_active=True,
        )
        self.assertIs(erased["mirror_stale"], True)
        self.assertEqual(erased["conversion_status"], "conversion_required")

        forged = merge_curated_metadata(
            {},
            {"conversion_status": "converted"},
            {"custom": {"mirror_stale": True}},
            overlay_active=True,
        )
        self.assertNotIn("mirror_stale", forged)


class OcrInvocationTests(unittest.TestCase):
    def _run_with_capture(self, redo_ocr):
        with tempfile.TemporaryDirectory() as temp_dir:
            src = Path(temp_dir) / "source.pdf"
            dest = Path(temp_dir) / "out.pdf"
            src.write_bytes(b"%PDF-1.7\n%source\n")

            def fake_run(cmd, **kwargs):
                with open(cmd[-1], "wb") as handle:
                    handle.write(b"%PDF-1.7\n%ocr-mock\n")
                return mock.Mock(returncode=0, stdout=b"", stderr=b"")

            with mock.patch.object(convert_cloud_docs.shutil, "which", return_value="tool"), \
                    mock.patch.object(convert_cloud_docs.subprocess, "run", side_effect=fake_run) as run:
                ok, _ = convert_cloud_docs.run_ocr_on_pdf(
                    str(src), str(dest), redo_ocr=redo_ocr
                )
            self.assertTrue(ok)
            self.assertEqual(run.call_count, 1)
            return list(run.call_args.args[0])

    def test_default_invocation_skips_existing_text(self):
        argv = self._run_with_capture(redo_ocr=False)
        self.assertIn("--skip-text", argv)
        self.assertNotIn("--redo-ocr", argv)
        self.assertNotIn("--tagged-pdf-mode", argv)

    def test_redo_invocation_ignores_tags_without_skip_text(self):
        argv = self._run_with_capture(redo_ocr=True)
        self.assertIn("--redo-ocr", argv)
        self.assertEqual(argv[argv.index("--tagged-pdf-mode") + 1], "ignore")
        self.assertNotIn("--skip-text", argv)


class TaggedWorkspaceTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

        self.cloud_dir = self.root / "data" / "cloud" / "TEST_PROJ"
        self.cloud_dir.mkdir(parents=True)
        self.output_dir = self.root / "memory" / "cloud" / "projects" / "test_proj"
        self.output_dir.mkdir(parents=True)

        projects_meta_dir = self.root / "memory" / "references" / "projects"
        projects_meta_dir.mkdir(parents=True)
        with open(projects_meta_dir / "projects.json", "w", encoding="utf-8") as handle:
            json.dump(
                [
                    {
                        "id": "test_proj",
                        "title": "Test Project",
                        "cloud_sync": {
                            "default": {
                                "scan_dir": "data/cloud/TEST_PROJ",
                                "output_json": "memory/cloud/projects/test_proj/filemap.json",
                                "output_md": "memory/cloud/projects/test_proj/filemap.md",
                                "output_dir": "memory/cloud/projects/test_proj",
                            }
                        },
                    }
                ],
                handle,
                indent=2,
            )

        (self.root / "AGENTS.md").write_text("# Test Workspace\n", encoding="utf-8")
        (self.root / ".workspace-root").touch()
        os.environ["CLOUD_ATLAS_WORKSPACE_ROOT"] = str(self.root)

        self.old_cwd = os.getcwd()
        os.chdir(self.root)

        self.source = self.cloud_dir / "tagged.pdf"
        shutil.copyfile(FIXTURE, self.source)
        self.mirror = self.output_dir / "tagged.md"
        self.filemap_path = self.output_dir / "filemap.json"

    def tearDown(self):
        os.chdir(self.old_cwd)
        self.temp_dir.cleanup()
        os.environ.pop("CLOUD_ATLAS_WORKSPACE_ROOT", None)
        for variable in ("CLOUD_ATLAS_OCR_MOCK", "CLOUD_ATLAS_MOCK_IS_SCAN"):
            os.environ.pop(variable, None)

    def run_conversion(self, *extra):
        return convert_cloud_docs.main(
            ["--project-id", "test_proj", *extra]
        )

    def run_conversion_state(self, *extra):
        args = convert_cloud_docs.parse_args(
            ["--project-id", "test_proj", *extra]
        )
        run_state = {"target": None, "storages": []}
        convert_cloud_docs.run_conversion(args, run_state)
        return run_state

    def read_filemap(self):
        return json.loads(self.filemap_path.read_text(encoding="utf-8"))

    def entry(self):
        return self.read_filemap()["files"]["data/cloud/TEST_PROJ/tagged.pdf"]


class TaggedEndToEndTests(TaggedWorkspaceTestCase):
    def test_fixture_converts_once_and_is_skipped_on_second_run(self):
        self.assertEqual(self.run_conversion(), 0)
        self.assertTrue(self.mirror.is_file(), "mirror should be written for a digital tagged PDF")
        first_entry = self.entry()
        self.assertEqual(first_entry["conversion_status"], "converted")
        self.assertNotEqual(first_entry.get("conversion_status"), "conversion_required")
        mirror_before = self.mirror.read_bytes()

        run_state = self.run_conversion_state()
        counts = run_state["storages"][0]["counts"]
        self.assertEqual(counts["converted"], 0)
        self.assertEqual(counts["skipped"], 1)
        self.assertEqual(counts["conversion_required"], 0)
        self.assertEqual(self.mirror.read_bytes(), mirror_before)


class MirrorRetentionTests(TaggedWorkspaceTestCase):
    def _convert_successfully(self):
        self.assertEqual(self.run_conversion(), 0)
        self.assertTrue(self.mirror.is_file())

    def test_conversion_required_retains_unchanged_mirror(self):
        self._convert_successfully()
        results = {
            "data/cloud/TEST_PROJ/tagged.pdf": {
                "success": False,
                "conversion_required": True,
                "error": "synthetic conversion requirement",
                "capability": "ocr_policy",
            }
        }
        with mock.patch.object(
            convert_cloud_docs, "run_conversion_tasks", return_value=results
        ):
            self.run_conversion_state("--force")

        self.assertTrue(self.mirror.is_file(), "retained mirror must survive conversion_required")
        entry = self.entry()
        self.assertEqual(entry["conversion_status"], "conversion_required")
        self.assertEqual(entry["markdown_mirror"], "memory/cloud/projects/test_proj/tagged.md")
        self.assertNotIn("mirror_stale", entry)

    def test_conversion_required_retains_derivative_reference(self):
        derivative_rel = "memory/cloud/projects/test_proj/_derivatives/tagged.pdf"
        derivative_abs = self.root / derivative_rel
        derivative_abs.parent.mkdir(parents=True, exist_ok=True)
        derivative_abs.write_bytes(b"%PDF-1.7\n%local OCR derivative\n")

        success = {
            "data/cloud/TEST_PROJ/tagged.pdf": {
                "success": True,
                "markdown_body": "# BAUFELD 1 / BAUFELD 2\n",
                "ocr_applied": True,
                "ocr_policy": "local_derivative",
                "derivative_path": derivative_rel,
                "derivative_sha256": "a" * 64,
                "conversion_method": "ocrmypdf-derivative",
                "potential_quality_loss": "OCR text layer in searchable PDF derivative.",
                "new_src_sha256": None,
                "new_src_size": None,
                "new_src_mtime": None,
                "error": None,
            }
        }
        with mock.patch.object(
            convert_cloud_docs, "run_conversion_tasks", return_value=success
        ):
            self.assertEqual(self.run_conversion(), 0)
        self.assertEqual(self.entry()["derivative"]["path"], derivative_rel)

        required = {
            "data/cloud/TEST_PROJ/tagged.pdf": {
                "success": False,
                "conversion_required": True,
                "error": "synthetic conversion requirement",
            }
        }
        with mock.patch.object(
            convert_cloud_docs, "run_conversion_tasks", return_value=required
        ):
            self.run_conversion_state("--force")

        self.assertTrue(self.mirror.is_file(), "retained mirror must survive conversion_required")
        self.assertTrue(derivative_abs.is_file(), "retained derivative must stay on disk")
        entry = self.entry()
        self.assertEqual(entry["conversion_status"], "conversion_required")
        self.assertEqual(entry["markdown_mirror"], "memory/cloud/projects/test_proj/tagged.md")
        self.assertEqual(entry["derivative"]["path"], derivative_rel)
        self.assertEqual(entry["derivative"]["sha256"], "a" * 64)
        self.assertEqual(entry["derivative"]["format"], "pdf")
        self.assertEqual(entry["derivative"]["conversion_method"], "ocrmypdf-derivative")

    def test_failed_conversion_retains_mirror(self):
        self._convert_successfully()
        results = {
            "data/cloud/TEST_PROJ/tagged.pdf": {
                "success": False,
                "error": "synthetic failure",
            }
        }
        with mock.patch.object(
            convert_cloud_docs, "run_conversion_tasks", return_value=results
        ):
            self.run_conversion_state("--force")

        self.assertTrue(self.mirror.is_file(), "retained mirror must survive a failed conversion")

    def test_changed_source_marks_retained_mirror_stale_and_filemap_stays_valid(self):
        self._convert_successfully()
        with open(self.source, "ab") as handle:
            handle.write(b"\n% source changed after mirror creation\n")

        results = {
            "data/cloud/TEST_PROJ/tagged.pdf": {
                "success": False,
                "conversion_required": True,
                "error": "source changed",
            }
        }
        with mock.patch.object(
            convert_cloud_docs, "run_conversion_tasks", return_value=results
        ):
            self.run_conversion_state("--force")

        self.assertTrue(self.mirror.is_file())
        self.assertIs(self.entry()["mirror_stale"], True)

        old_argv = sys.argv
        sys.argv = ["gen_filemap.py", "--project-id", "test_proj"]
        try:
            self.assertEqual(gen_filemap.main(), 0)
        finally:
            sys.argv = old_argv
        entry = self.entry()
        self.assertEqual(entry["conversion_status"], "conversion_required")
        self.assertIs(entry.get("mirror_stale"), True)
        self.assertNotIn("artifact_metadata", entry)
        self.assertIn("conversion_error", entry)

    def test_deleted_source_still_removes_its_mirror(self):
        self._convert_successfully()
        self.source.unlink()

        self.assertEqual(self.run_conversion(), 0)
        self.assertFalse(self.mirror.exists(), "mirror of a deleted source must be cleaned up")


if __name__ == "__main__":
    unittest.main()
