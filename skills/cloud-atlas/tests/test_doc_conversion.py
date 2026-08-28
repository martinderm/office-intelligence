import os
import sys
import json
import shutil
import tempfile
import unittest
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
            
        self.old_cwd = os.getcwd()
        os.chdir(self.root)
        
        # Enable mock converter by default for fast, deterministic testing
        os.environ["CLOUD_ATLAS_DOC_CONVERTER_MOCK"] = "mock:test"
        if "CLOUD_ATLAS_MOCK_CORRUPT" in os.environ:
            del os.environ["CLOUD_ATLAS_MOCK_CORRUPT"]

    def tearDown(self):
        os.chdir(self.old_cwd)
        self.temp_dir.cleanup()
        if "CLOUD_ATLAS_DOC_CONVERTER_MOCK" in os.environ:
            del os.environ["CLOUD_ATLAS_DOC_CONVERTER_MOCK"]
        if "CLOUD_ATLAS_MOCK_CORRUPT" in os.environ:
            del os.environ["CLOUD_ATLAS_MOCK_CORRUPT"]

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


if __name__ == "__main__":
    unittest.main()
