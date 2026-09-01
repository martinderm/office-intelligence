import os
import sys
import json
import shutil
import tempfile
import unittest
from unittest import mock
from pathlib import Path

# Load modules
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import convert_cloud_docs
import gen_filemap


class DocConversionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

        # Setup workspace structure
        self.cloud_dir = self.root / "data" / "cloud" / "TEST_PROJ"
        self.cloud_dir.mkdir(parents=True)

        self.output_dir = self.root / "memory" / "cloud" / "projects" / "test_proj"
        self.output_dir.mkdir(parents=True)

        self.projects_meta_dir = self.root / "memory" / "references" / "projects"
        self.projects_meta_dir.mkdir(parents=True)

        self.projects_json = self.projects_meta_dir / "projects.json"
        projects_data = [
            {
                "id": "test_proj",
                "title": "Test Project",
                "cloud_sync": {
                    "default": {
                        "scan_dir": "data/cloud/TEST_PROJ",
                        "output_json": "memory/cloud/projects/test_proj/filemap.json",
                        "output_md": "memory/cloud/projects/test_proj/filemap.md",
                        "output_dir": "memory/cloud/projects/test_proj"
                    }
                }
            }
        ]
        with open(self.projects_json, "w", encoding="utf-8") as f:
            json.dump(projects_data, f, indent=2)

        # Mark workspace root
        (self.root / "AGENTS.md").write_text("# Test Workspace\n", encoding="utf-8")
        (self.root / ".workspace-root").touch()
        os.environ["CLOUD_ATLAS_WORKSPACE_ROOT"] = str(self.root)

        self.old_cwd = os.getcwd()
        os.chdir(self.root)

        # Enable mock converter by default for fast, deterministic testing
        os.environ["CLOUD_ATLAS_DOC_CONVERTER_MOCK"] = "mock:test"
        if "CLOUD_ATLAS_MOCK_CORRUPT" in os.environ:
            del os.environ["CLOUD_ATLAS_MOCK_CORRUPT"]

    def tearDown(self):
        os.chdir(self.old_cwd)
        self.temp_dir.cleanup()
        os.environ.pop("CLOUD_ATLAS_WORKSPACE_ROOT", None)
        if "CLOUD_ATLAS_DOC_CONVERTER_MOCK" in os.environ:
            del os.environ["CLOUD_ATLAS_DOC_CONVERTER_MOCK"]
        if "CLOUD_ATLAS_MOCK_CORRUPT" in os.environ:
            del os.environ["CLOUD_ATLAS_MOCK_CORRUPT"]
        if "CLOUD_ATLAS_OCR_MOCK" in os.environ:
            del os.environ["CLOUD_ATLAS_OCR_MOCK"]
        if "CLOUD_ATLAS_MOCK_IS_SCAN" in os.environ:
            del os.environ["CLOUD_ATLAS_MOCK_IS_SCAN"]

    def test_successful_doc_conversion_pipeline(self):
        """Test full .doc conversion: derivative created, markdown mirror generated, manifest & frontmatter populated."""
        doc_file = self.cloud_dir / "Vertrag_v2.doc"
        doc_content = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"SAMPLE_DOC_BINARY_PAYLOAD_v2"
        doc_file.write_bytes(doc_content)

        # Run conversion
        sys.argv = ["convert_cloud_docs.py", "--project-id", "test_proj"]
        convert_cloud_docs.main()

        # Run filemap generator
        sys.argv = ["gen_filemap.py", "--project-id", "test_proj"]
        gen_filemap.main()

        # 1. Check derivative existence
        deriv_file = self.output_dir / "_derivatives" / "Vertrag_v2.docx"
        self.assertTrue(deriv_file.is_file(), "Derivative .docx file should exist in _derivatives/")
        self.assertEqual(b"MOCK_DOCX_DERIVATIVE_CONTENT", deriv_file.read_bytes())

        # 2. Check markdown mirror existence and frontmatter
        md_file = self.output_dir / "Vertrag_v2.md"
        self.assertTrue(md_file.is_file(), "Markdown mirror should exist in output_dir")

        meta, body = convert_cloud_docs.parse_markdown_file(str(md_file))
        self.assertIsNotNone(meta)
        self.assertEqual(meta.get("original_file"), "data/cloud/TEST_PROJ/Vertrag_v2.doc")
        self.assertEqual(meta.get("version"), "2")
        self.assertIsNotNone(meta.get("original_sha256"))
        self.assertEqual(meta.get("original_sha256"), convert_cloud_docs.calculate_sha256(str(doc_file)))
        self.assertEqual(meta.get("derivative_file"), "memory/cloud/projects/test_proj/_derivatives/Vertrag_v2.docx")
        self.assertEqual(meta.get("derivative_sha256"), convert_cloud_docs.calculate_sha256(str(deriv_file)))
        self.assertEqual(meta.get("conversion_method"), "mock-converter")
        self.assertIn("Konvertierung von binärem .doc", meta.get("potential_quality_loss", ""))

        # 3. Check filemap.json
        filemap_json_path = self.output_dir / "filemap.json"
        self.assertTrue(filemap_json_path.is_file())
        with open(filemap_json_path, "r", encoding="utf-8") as f:
            fmap = json.load(f)

        file_info = fmap["files"]["data/cloud/TEST_PROJ/Vertrag_v2.doc"]
        self.assertEqual(file_info["conversion_status"], "converted")
        self.assertEqual(file_info["markdown_mirror"], "memory/cloud/projects/test_proj/Vertrag_v2.md")
        self.assertEqual(file_info["sha256"], convert_cloud_docs.calculate_sha256(str(doc_file)))
        self.assertIn("derivative", file_info)
        self.assertEqual(file_info["derivative"]["path"], "memory/cloud/projects/test_proj/_derivatives/Vertrag_v2.docx")
        self.assertEqual(file_info["derivative"]["sha256"], convert_cloud_docs.calculate_sha256(str(deriv_file)))
        self.assertEqual(file_info["derivative"]["format"], "docx")

        # 4. Check filemap.md
        filemap_md_path = self.output_dir / "filemap.md"
        self.assertTrue(filemap_md_path.is_file())
        md_text = filemap_md_path.read_text(encoding="utf-8")
        self.assertIn("Spiegelung: [Vertrag_v2.md]", md_text)
        self.assertIn("Derivat: [Vertrag_v2.docx]", md_text)

    def test_missing_converter_fallback(self):
        """When no converter is found, .doc files are cataloged as conversion_required without error."""
        os.environ["CLOUD_ATLAS_DOC_CONVERTER_MOCK"] = "none"

        doc_file = self.cloud_dir / "Legacy_Archiv.doc"
        doc_file.write_bytes(b"\xd0\xcf\x11\xe0" + b"LEGACY_DATA")

        # Run conversion
        sys.argv = ["convert_cloud_docs.py", "--project-id", "test_proj"]
        convert_cloud_docs.main()

        # Run filemap generator
        sys.argv = ["gen_filemap.py", "--project-id", "test_proj"]
        gen_filemap.main()

        # No derivative or mirror created
        deriv_file = self.output_dir / "_derivatives" / "Legacy_Archiv.docx"
        self.assertFalse(deriv_file.exists())
        md_file = self.output_dir / "Legacy_Archiv.md"
        self.assertFalse(md_file.exists())

        # File is cataloged in filemap.json with conversion_required
        filemap_json_path = self.output_dir / "filemap.json"
        with open(filemap_json_path, "r", encoding="utf-8") as f:
            fmap = json.load(f)

        file_entry = fmap["files"]["data/cloud/TEST_PROJ/Legacy_Archiv.doc"]
        self.assertEqual(file_entry["conversion_status"], "conversion_required")
        self.assertIn("No suitable converter found", file_entry["conversion_error"])
        self.assertEqual(file_entry["sha256"], convert_cloud_docs.calculate_sha256(str(doc_file)))
        self.assertNotIn("markdown_mirror", file_entry)

        # Filemap.md contains warning badge
        filemap_md_path = self.output_dir / "filemap.md"
        md_text = filemap_md_path.read_text(encoding="utf-8")
        self.assertIn("⚠️ **Konvertierung erforderlich**", md_text)

    def test_corrupted_doc_file(self):
        """Corrupted .doc file gracefully fails, records conversion_error, and marks conversion_required."""
        os.environ["CLOUD_ATLAS_MOCK_CORRUPT"] = "1"

        doc_file = self.cloud_dir / "Corrupted_File.doc"
        doc_file.write_bytes(b"CORRUPTED_GARBAGE_BYTES_NOT_A_VALID_DOC")

        sys.argv = ["convert_cloud_docs.py", "--project-id", "test_proj"]
        convert_cloud_docs.main()

        sys.argv = ["gen_filemap.py", "--project-id", "test_proj"]
        gen_filemap.main()

        filemap_json_path = self.output_dir / "filemap.json"
        with open(filemap_json_path, "r", encoding="utf-8") as f:
            fmap = json.load(f)

        file_entry = fmap["files"]["data/cloud/TEST_PROJ/Corrupted_File.doc"]
        self.assertEqual(file_entry["conversion_status"], "conversion_required")
        self.assertIn("Corrupted .doc file structure", file_entry["conversion_error"])

        filemap_md_path = self.output_dir / "filemap.md"
        md_text = filemap_md_path.read_text(encoding="utf-8")
        self.assertIn("⚠️ **Konvertierung erforderlich**", md_text)

    def test_duplicate_doc_files_handling(self):
        """Duplicate .doc files with identical content in different subfolders generate separate derivatives without collision."""
        sub1 = self.cloud_dir / "ordner_a"
        sub2 = self.cloud_dir / "ordner_b"
        sub1.mkdir(parents=True)
        sub2.mkdir(parents=True)

        identical_content = b"\xd0\xcf\x11\xe0" + b"IDENTICAL_CONTENT_HASH_TEST"
        doc1 = sub1 / "rechnung.doc"
        doc2 = sub2 / "rechnung.doc"
        doc1.write_bytes(identical_content)
        doc2.write_bytes(identical_content)

        self.assertEqual(convert_cloud_docs.calculate_sha256(str(doc1)), convert_cloud_docs.calculate_sha256(str(doc2)))

        sys.argv = ["convert_cloud_docs.py", "--project-id", "test_proj"]
        convert_cloud_docs.main()

        sys.argv = ["gen_filemap.py", "--project-id", "test_proj"]
        gen_filemap.main()

        deriv1 = self.output_dir / "_derivatives" / "ordner_a" / "rechnung.docx"
        deriv2 = self.output_dir / "_derivatives" / "ordner_b" / "rechnung.docx"
        self.assertTrue(deriv1.is_file())
        self.assertTrue(deriv2.is_file())

        mirror1 = self.output_dir / "ordner_a" / "rechnung.md"
        mirror2 = self.output_dir / "ordner_b" / "rechnung.md"
        self.assertTrue(mirror1.is_file())
        self.assertTrue(mirror2.is_file())

        filemap_json_path = self.output_dir / "filemap.json"
        with open(filemap_json_path, "r", encoding="utf-8") as f:
            fmap = json.load(f)

        entry1 = fmap["files"]["data/cloud/TEST_PROJ/ordner_a/rechnung.doc"]
        entry2 = fmap["files"]["data/cloud/TEST_PROJ/ordner_b/rechnung.doc"]

        self.assertEqual(entry1["sha256"], entry2["sha256"])
        self.assertEqual(entry1["derivative"]["path"], "memory/cloud/projects/test_proj/_derivatives/ordner_a/rechnung.docx")
        self.assertEqual(entry2["derivative"]["path"], "memory/cloud/projects/test_proj/_derivatives/ordner_b/rechnung.docx")

    def test_cloud_originals_never_overwritten(self):
        """Strict invariant test: cloud originals are never modified, touched, or overwritten."""
        doc_file = self.cloud_dir / "Strict_Original_Prot.doc"
        original_bytes = b"\xd0\xcf\x11\xe0\xa1\xb1" + b"CRITICAL_ORIGINAL_DOCUMENT_CONTENT"
        doc_file.write_bytes(original_bytes)

        orig_sha256 = convert_cloud_docs.calculate_sha256(str(doc_file))
        orig_stat = doc_file.stat()
        orig_mtime = orig_stat.st_mtime
        orig_size = orig_stat.st_size

        # Test safety assertion prevents writing to cloud folder
        with self.assertRaises(PermissionError):
            convert_cloud_docs.assert_not_in_cloud_dir(
                str(self.cloud_dir / "illegal_derivative.docx"),
                str(self.cloud_dir)
            )

        sys.argv = ["convert_cloud_docs.py", "--project-id", "test_proj"]
        convert_cloud_docs.main()

        # Verify original in cloud remains 100% untouched
        fresh_stat = doc_file.stat()
        self.assertEqual(doc_file.read_bytes(), original_bytes, "Original file content MUST NOT be changed")
        self.assertEqual(convert_cloud_docs.calculate_sha256(str(doc_file)), orig_sha256, "Original SHA-256 MUST NOT change")
        self.assertEqual(fresh_stat.st_mtime, orig_mtime, "Original mtime MUST NOT change")
        self.assertEqual(fresh_stat.st_size, orig_size, "Original size MUST NOT change")

        # Ensure no .docx or .md was created inside the cloud directory
        cloud_files = [p.name for p in self.cloud_dir.rglob("*") if p.is_file()]
        self.assertEqual(cloud_files, ["Strict_Original_Prot.doc"], "Cloud directory must only contain the original file")

    def test_orphaned_derivatives_and_mirrors_cleanup(self):
        """When an original .doc file is deleted from cloud, its derivative and markdown mirror are cleaned up."""
        doc_file = self.cloud_dir / "Temporary_Doc.doc"
        doc_file.write_bytes(b"\xd0\xcf\x11\xe0" + b"TEMP_CONTENT")

        # 1. First run creates derivative & mirror
        sys.argv = ["convert_cloud_docs.py", "--project-id", "test_proj"]
        convert_cloud_docs.main()

        deriv_file = self.output_dir / "_derivatives" / "Temporary_Doc.docx"
        md_file = self.output_dir / "Temporary_Doc.md"
        self.assertTrue(deriv_file.is_file())
        self.assertTrue(md_file.is_file())

        # 2. Delete original .doc
        doc_file.unlink()

        # 3. Second run cleans up orphaned files
        sys.argv = ["convert_cloud_docs.py", "--project-id", "test_proj"]
        convert_cloud_docs.main()

        self.assertFalse(deriv_file.exists(), "Orphaned derivative should have been deleted")
        self.assertFalse(md_file.exists(), "Orphaned markdown mirror should have been deleted")

    def test_ocr_policy_local_derivative_mode(self):
        """Default local_derivative mode creates a PDF derivative in _derivatives/ and leaves cloud original untouched."""
        pdf_file = self.cloud_dir / "Scanned_Document.pdf"
        original_bytes = (
            b"%PDF-1.4\n"
            b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
            b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
            b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /XObject << /Im1 4 0 R >> >> >>\nendobj\n"
            b"4 0 obj\n<< /Type /XObject /Subtype /Image /Width 10 /Height 10 /ColorSpace /DeviceRGB /BitsPerComponent 8 >>\nstream\n"
            b"MOCK_IMAGE_DATA_BYTES\nendstream\nendobj\n"
            b"xref\n0 5\n0000000000 65535 f \n0000000009 00000 n \n0000000058 00000 n \n0000000115 00000 n \n0000000218 00000 n \n"
            b"trailer\n<< /Size 5 /Root 1 0 R >>\nstartxref\n340\n%%EOF"
        )
        pdf_file.write_bytes(original_bytes)
        orig_sha = convert_cloud_docs.calculate_sha256(str(pdf_file))
        orig_stat = pdf_file.stat()

        os.environ["CLOUD_ATLAS_MOCK_IS_SCAN"] = "1"
        os.environ["CLOUD_ATLAS_OCR_MOCK"] = "1"

        # 1. Run conversion with default policy (local_derivative)
        sys.argv = ["convert_cloud_docs.py", "--project-id", "test_proj"]
        convert_cloud_docs.main()

        # Run filemap generator
        sys.argv = ["gen_filemap.py", "--project-id", "test_proj"]
        gen_filemap.main()

        # Check cloud original is 100% untouched
        self.assertEqual(pdf_file.read_bytes(), original_bytes)
        self.assertEqual(convert_cloud_docs.calculate_sha256(str(pdf_file)), orig_sha)
        self.assertEqual(pdf_file.stat().st_mtime, orig_stat.st_mtime)

        # Check derivative exists under _derivatives/
        deriv_file = self.output_dir / "_derivatives" / "Scanned_Document.pdf"
        self.assertTrue(deriv_file.is_file(), "PDF derivative must exist under _derivatives/")
        deriv_sha = convert_cloud_docs.calculate_sha256(str(deriv_file))

        # Check markdown mirror and frontmatter
        md_file = self.output_dir / "Scanned_Document.md"
        self.assertTrue(md_file.is_file())
        meta, body = convert_cloud_docs.parse_markdown_file(str(md_file))
        self.assertIsNotNone(meta)
        self.assertEqual(meta.get("ocr_applied"), "true")
        self.assertEqual(meta.get("ocr_policy"), "local_derivative")
        self.assertEqual(meta.get("derivative_file"), "memory/cloud/projects/test_proj/_derivatives/Scanned_Document.pdf")
        self.assertEqual(meta.get("derivative_sha256"), deriv_sha)
        self.assertIn("OCR", meta.get("ocr_notice", ""))

        # Check filemap.json
        filemap_json_path = self.output_dir / "filemap.json"
        with open(filemap_json_path, "r", encoding="utf-8") as f:
            fmap = json.load(f)

        entry = fmap["files"]["data/cloud/TEST_PROJ/Scanned_Document.pdf"]
        self.assertEqual(entry["conversion_status"], "converted")
        self.assertEqual(entry["ocr_applied"], True)
        self.assertEqual(entry["ocr_policy"], "local_derivative")
        self.assertEqual(entry["sha256"], orig_sha)
        self.assertIn("derivative", entry)
        self.assertEqual(entry["derivative"]["path"], "memory/cloud/projects/test_proj/_derivatives/Scanned_Document.pdf")
        self.assertEqual(entry["derivative"]["format"], "pdf")
        self.assertEqual(entry["derivative"]["conversion_method"], "ocrmypdf-derivative")

    def test_ocr_policy_enrich_source_mode(self):
        """enrich_source mode enriches writable cloud PDF in-place without creating a separate derivative."""
        pdf_file = self.cloud_dir / "Writable_Scan.pdf"
        original_bytes = (
            b"%PDF-1.4\n"
            b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
            b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
            b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /XObject << /Im1 4 0 R >> >> >>\nendobj\n"
            b"4 0 obj\n<< /Type /XObject /Subtype /Image /Width 10 /Height 10 /ColorSpace /DeviceRGB /BitsPerComponent 8 >>\nstream\n"
            b"MOCK_IMAGE_DATA_BYTES\nendstream\nendobj\n"
            b"xref\n0 5\n0000000000 65535 f \n0000000009 00000 n \n0000000058 00000 n \n0000000115 00000 n \n0000000218 00000 n \n"
            b"trailer\n<< /Size 5 /Root 1 0 R >>\nstartxref\n340\n%%EOF"
        )
        pdf_file.write_bytes(original_bytes)
        orig_sha = convert_cloud_docs.calculate_sha256(str(pdf_file))

        os.environ["CLOUD_ATLAS_MOCK_IS_SCAN"] = "1"
        os.environ["CLOUD_ATLAS_OCR_MOCK"] = "1"

        # Run conversion with enrich_source policy
        sys.argv = ["convert_cloud_docs.py", "--project-id", "test_proj", "--ocr-policy", "enrich_source"]
        convert_cloud_docs.main()

        sys.argv = ["gen_filemap.py", "--project-id", "test_proj"]
        gen_filemap.main()

        # Cloud original has been enriched in-place
        new_bytes = pdf_file.read_bytes()
        new_sha = convert_cloud_docs.calculate_sha256(str(pdf_file))
        self.assertIn(b"% MOCK_OCR_INPLACE_LAYER", new_bytes)
        self.assertNotEqual(orig_sha, new_sha)

        # No derivative PDF should exist
        deriv_file = self.output_dir / "_derivatives" / "Writable_Scan.pdf"
        self.assertFalse(deriv_file.exists(), "No derivative should be created in enrich_source mode")

        # Markdown mirror reflects in-place OCR
        md_file = self.output_dir / "Writable_Scan.md"
        self.assertTrue(md_file.is_file())
        meta, body = convert_cloud_docs.parse_markdown_file(str(md_file))
        self.assertEqual(meta.get("ocr_applied"), "true")
        self.assertEqual(meta.get("ocr_policy"), "enrich_source")
        self.assertNotIn("derivative_file", meta)

        # filemap.json
        filemap_json_path = self.output_dir / "filemap.json"
        with open(filemap_json_path, "r", encoding="utf-8") as f:
            fmap = json.load(f)

        entry = fmap["files"]["data/cloud/TEST_PROJ/Writable_Scan.pdf"]
        self.assertEqual(entry["conversion_status"], "converted")
        self.assertEqual(entry["ocr_applied"], True)
        self.assertEqual(entry["ocr_policy"], "enrich_source")
        self.assertEqual(entry["sha256"], new_sha)
        self.assertNotIn("derivative", entry)

    def test_ocr_policy_disabled_and_no_ocr_flag(self):
        """When disabled or --no-ocr is specified, no OCR is performed on scanned PDFs."""
        pdf_file = self.cloud_dir / "Disabled_OCR_Scan.pdf"
        original_bytes = (
            b"%PDF-1.4\n"
            b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
            b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
            b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>\nendobj\n"
            b"xref\n0 4\n0000000000 65535 f \n0000000009 00000 n \n0000000058 00000 n \n0000000115 00000 n \n"
            b"trailer\n<< /Size 4 /Root 1 0 R >>\nstartxref\n190\n%%EOF"
        )
        pdf_file.write_bytes(original_bytes)
        orig_sha = convert_cloud_docs.calculate_sha256(str(pdf_file))

        os.environ["CLOUD_ATLAS_MOCK_IS_SCAN"] = "1"
        os.environ["CLOUD_ATLAS_OCR_MOCK"] = "1"

        # Run conversion with --no-ocr
        sys.argv = ["convert_cloud_docs.py", "--project-id", "test_proj", "--no-ocr"]
        convert_cloud_docs.main()

        # Verify no OCR was applied
        self.assertEqual(pdf_file.read_bytes(), original_bytes)
        deriv_file = self.output_dir / "_derivatives" / "Disabled_OCR_Scan.pdf"
        self.assertFalse(deriv_file.exists())

        md_file = self.output_dir / "Disabled_OCR_Scan.md"
        self.assertTrue(md_file.is_file())
        meta, body = convert_cloud_docs.parse_markdown_file(str(md_file))
        self.assertNotEqual(meta.get("ocr_applied"), "true")

    def test_short_digital_pdf_not_treated_as_scan(self):
        """Short 1-line digital PDF (<30 chars) with digital fonts is NOT treated as a scan."""
        # PDF with digital font definitions and text operators
        short_digital_bytes = (
            b"%PDF-1.4\n"
            b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
            b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
            b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>\nendobj\n"
            b"4 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /FontDescriptor 6 0 R >>\nendobj\n"
            b"5 0 obj\n<< /Length 40 >>\nstream\nBT /F1 12 Tf (Rechnung 12345) Tj ET\nendstream\nendobj\n"
            b"6 0 obj\n<< /Type /FontDescriptor /FontName /Helvetica >>\nendobj\n"
            b"xref\n0 7\n0000000000 65535 f \n0000000009 00000 n \n0000000058 00000 n \n0000000115 00000 n \n0000000240 00000 n \n0000000330 00000 n \n0000000420 00000 n \n"
            b"trailer\n<< /Size 7 /Root 1 0 R >>\nstartxref\n500\n%%EOF"
        )
        pdf_file = self.cloud_dir / "Short_Invoice.pdf"
        pdf_file.write_bytes(short_digital_bytes)

        # Check unit detection
        is_scan = convert_cloud_docs.is_image_based_pdf(str(pdf_file), "Rechnung 12345")
        self.assertFalse(is_scan, "Short digital PDF with fonts must NOT be detected as image-based scan")

        # Run conversion
        sys.argv = ["convert_cloud_docs.py", "--project-id", "test_proj"]
        convert_cloud_docs.main()

        # No derivative or OCR applied
        deriv_file = self.output_dir / "_derivatives" / "Short_Invoice.pdf"
        self.assertFalse(deriv_file.exists(), "No derivative should be created for digital PDF")
        self.assertEqual(pdf_file.read_bytes(), short_digital_bytes, "Original must be untouched")

    def test_digitally_signed_pdf_protection_in_enrich_source(self):
        """Digitally signed PDFs are protected from in-place mutation and cataloged with conversion_required."""
        signed_pdf_bytes = (
            b"%PDF-1.4\n"
            b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
            b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
            b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>\nendobj\n"
            b"4 0 obj\n<< /Type /Sig /ByteRange [0 100 200 50] /Subtype /adbe.pkcs7.detached /Contents <01020304> >>\nendobj\n"
            b"xref\n0 5\n0000000000 65535 f \n0000000009 00000 n \n0000000058 00000 n \n0000000115 00000 n \n0000000210 00000 n \n"
            b"trailer\n<< /Size 5 /Root 1 0 R >>\nstartxref\n320\n%%EOF"
        )
        pdf_file = self.cloud_dir / "Amtssigniertes_Dokument.pdf"
        pdf_file.write_bytes(signed_pdf_bytes)
        orig_sha = convert_cloud_docs.calculate_sha256(str(pdf_file))
        orig_stat = pdf_file.stat()

        os.environ["CLOUD_ATLAS_MOCK_IS_SCAN"] = "1"
        os.environ["CLOUD_ATLAS_OCR_MOCK"] = "1"

        # Check signature detection unit function
        self.assertTrue(convert_cloud_docs.is_digitally_signed_pdf(str(pdf_file)))

        # Attempt in-place enrichment on signed document
        sys.argv = ["convert_cloud_docs.py", "--project-id", "test_proj", "--ocr-policy", "enrich_source"]
        convert_cloud_docs.main()

        sys.argv = ["gen_filemap.py", "--project-id", "test_proj"]
        gen_filemap.main()

        # Invariant: Cloud original MUST NOT be mutated
        self.assertEqual(pdf_file.read_bytes(), signed_pdf_bytes, "Signed PDF MUST NEVER be mutated in-place")
        self.assertEqual(convert_cloud_docs.calculate_sha256(str(pdf_file)), orig_sha)
        self.assertEqual(pdf_file.stat().st_mtime, orig_stat.st_mtime)

        # File is recorded with conversion_required
        filemap_json_path = self.output_dir / "filemap.json"
        with open(filemap_json_path, "r", encoding="utf-8") as f:
            fmap = json.load(f)

        entry = fmap["files"]["data/cloud/TEST_PROJ/Amtssigniertes_Dokument.pdf"]
        self.assertEqual(entry["conversion_status"], "conversion_required")
        self.assertIn("Digitally signed PDF cannot be mutated in-place", entry["conversion_error"])

    def test_failed_ocr_preserves_original_undamaged(self):
        """When OCR fails, the original cloud file is not corrupted or altered in any way."""
        pdf_file = self.cloud_dir / "Broken_Scan.pdf"
        original_bytes = (
            b"%PDF-1.4\n"
            b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
            b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
            b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>\nendobj\n"
            b"xref\n0 4\n0000000000 65535 f \n0000000009 00000 n \n0000000058 00000 n \n0000000115 00000 n \n"
            b"trailer\n<< /Size 4 /Root 1 0 R >>\nstartxref\n190\n%%EOF"
        )
        pdf_file.write_bytes(original_bytes)
        orig_sha = convert_cloud_docs.calculate_sha256(str(pdf_file))
        orig_stat = pdf_file.stat()

        os.environ["CLOUD_ATLAS_MOCK_IS_SCAN"] = "1"
        os.environ["CLOUD_ATLAS_OCR_MOCK"] = "fail"

        # Run conversion with enrich_source policy
        sys.argv = ["convert_cloud_docs.py", "--project-id", "test_proj", "--ocr-policy", "enrich_source"]
        convert_cloud_docs.main()

        # Cloud original remains 100% untouched
        self.assertEqual(pdf_file.read_bytes(), original_bytes, "Failed OCR must leave original untouched")
        self.assertEqual(convert_cloud_docs.calculate_sha256(str(pdf_file)), orig_sha)
        self.assertEqual(pdf_file.stat().st_mtime, orig_stat.st_mtime)

        filemap_json_path = self.output_dir / "filemap.json"
        with open(filemap_json_path, "r", encoding="utf-8") as f:
            fmap = json.load(f)

        entry = fmap["files"]["data/cloud/TEST_PROJ/Broken_Scan.pdf"]
        self.assertEqual(entry["conversion_status"], "conversion_required")
        self.assertIn("OCR enrichment failed", entry["conversion_error"])

    def test_pdf_structure_and_signature_detection_units(self):
        """Unit test for PDF structure analysis and signature detection helpers."""
        # 1. Plain unsigned text
        unsigned_data = b"%PDF-1.4\n/Type /Font\n/FontDescriptor\nBT Tj ET\n%%EOF"
        self.assertFalse(convert_cloud_docs.is_pdf_digitally_signed_bytes(unsigned_data))

        # 2. Signed PDF with /ByteRange and /Type /Sig
        signed_data1 = b"%PDF-1.4\n/ByteRange [0 10 20 30]\n/Type /Sig\n%%EOF"
        self.assertTrue(convert_cloud_docs.is_pdf_digitally_signed_bytes(signed_data1))

        # 3. Signed PDF with /ByteRange and /DocTimeStamp
        signed_data2 = b"%PDF-1.4\n/ByteRange [0 10 20 30]\n/Type /DocTimeStamp\n%%EOF"
        self.assertTrue(convert_cloud_docs.is_pdf_digitally_signed_bytes(signed_data2))

        # 4. ByteRange alone without Sig is not treated as signed
        partial_data = b"%PDF-1.4\n/ByteRange [0 10 20 30]\n%%EOF"
        self.assertFalse(convert_cloud_docs.is_pdf_digitally_signed_bytes(partial_data))

    def test_atomic_mock_promotion_failure_preserves_source_and_cleans_stage(self):
        """A failed replace must not modify a cloud source or leave a staging sibling."""
        pdf_file = self.cloud_dir / "Atomic_Source.pdf"
        original = b"%PDF-1.4\noriginal\n%%EOF"
        pdf_file.write_bytes(original)
        with mock.patch.object(convert_cloud_docs.os, "replace", side_effect=OSError("replace failed")):
            os.environ["CLOUD_ATLAS_OCR_MOCK"] = "1"
            ok, _ = convert_cloud_docs.run_ocr_on_pdf(
                str(pdf_file), expected_src_sha256=convert_cloud_docs.calculate_sha256(str(pdf_file)), check_signature=True
            )
        self.assertFalse(ok)
        self.assertEqual(pdf_file.read_bytes(), original)
        self.assertEqual(list(self.cloud_dir.glob(".ocr-stage-*.pdf")), [])

    def test_redo_ocr_overrides_scan_detection(self):
        """Explicit redo reaches the OCR path even when a text layer is present."""
        pdf_file = self.cloud_dir / "Already_Ocred.pdf"
        original = b"%PDF-1.4\ntext layer\n%%EOF"
        pdf_file.write_bytes(original)
        task = {
            "src_abs": str(pdf_file), "src_rel": "Already_Ocred.pdf", "dest_rel": "Already_Ocred.md",
            "is_pdf": True, "is_doc": False, "src_sha256": convert_cloud_docs.calculate_sha256(str(pdf_file)),
            "ocr_policy": "enrich_source", "redo_ocr": True, "file_timeout": 60,
        }
        parent, child = convert_cloud_docs.multiprocessing.Pipe()
        os.environ["CLOUD_ATLAS_OCR_MOCK"] = "1"
        with mock.patch.object(convert_cloud_docs, "is_image_based_pdf", return_value=False):
            convert_cloud_docs._convert_worker_target(task, child)
        status, payload = parent.recv()
        self.assertEqual(status, "ok")
        self.assertTrue(payload["ocr_applied"])
        self.assertNotEqual(pdf_file.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
