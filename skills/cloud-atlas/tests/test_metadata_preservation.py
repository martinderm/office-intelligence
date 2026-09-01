import os
import sys
import json
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

# Load scripts directory
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import convert_cloud_docs
import gen_filemap


def _data_zone_artifact_schema():
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "Shared-Memory" / "agent-architecture" / "schemas" / "data-zone-artifact.schema.json"
        if candidate.is_file():
            return json.loads(candidate.read_text(encoding="utf-8"))
    raise FileNotFoundError("data-zone-artifact.schema.json not found")


class MetadataPreservationTests(unittest.TestCase):
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
        
        # Enable mock converter for .doc files
        os.environ["CLOUD_ATLAS_DOC_CONVERTER_MOCK"] = "mock:test"

    def tearDown(self):
        os.chdir(self.old_cwd)
        self.temp_dir.cleanup()
        os.environ.pop("CLOUD_ATLAS_WORKSPACE_ROOT", None)
        if "CLOUD_ATLAS_DOC_CONVERTER_MOCK" in os.environ:
            del os.environ["CLOUD_ATLAS_DOC_CONVERTER_MOCK"]

    def _run_full_sync(self, force=False):
        """Execute conversion then filemap generation."""
        args_convert = ["convert_cloud_docs.py", "--project-id", "test_proj", "--workspace-root", str(self.root)]
        if force:
            args_convert.append("--force")
        sys.argv = args_convert
        convert_cloud_docs.main()
        
        sys.argv = ["gen_filemap.py", "--project-id", "test_proj", "--workspace-root", str(self.root)]
        gen_filemap.main()

    def _run_mocked_conversion(self, source_file, markdown_body, **overrides):
        source_rel = source_file.relative_to(self.root).as_posix()
        result = {
            "success": True,
            "markdown_body": markdown_body,
            "ocr_applied": False,
            "derivative_path": None,
            "derivative_sha256": None,
            "conversion_method": "markitdown-direct",
            "potential_quality_loss": None,
            "ocr_policy": "disabled",
            "new_src_sha256": None,
            "new_src_size": None,
            "new_src_mtime": None,
            "error": None,
        }
        result.update(overrides)
        with mock.patch.object(
            convert_cloud_docs,
            "run_conversion_tasks",
            return_value={source_rel: result},
        ):
            sys.argv = [
                "convert_cloud_docs.py", "--project-id", "test_proj",
                "--workspace-root", str(self.root),
            ]
            convert_cloud_docs.main()

    def test_legacy_frontmatter_is_read_and_unchanged_mirror_is_byte_exact(self):
        """Dual-read accepts legacy provenance without using a skip run as migration."""
        source = self.cloud_dir / "legacy.pdf"
        source.write_bytes(b"legacy cloud source")
        source_hash = convert_cloud_docs.calculate_sha256(str(source))
        mirror = self.output_dir / "legacy.md"
        original = (
            "---\n"
            "original_file: \"data/cloud/TEST_PROJ/legacy.pdf\"\n"
            f"original_sha256: \"{source_hash}\"\n"
            "file_date: \"2000-01-01 00:00:00\"\n"
            "last_verified_date: \"2000-01-01 00:00:00\"\n"
            "---\n\n"
            "Legacy payload that must remain byte-exact.\n"
        ).encode("utf-8")
        mirror.write_bytes(original)

        metadata, body = convert_cloud_docs.parse_markdown_file(str(mirror))
        self.assertEqual(metadata["original_file"], "data/cloud/TEST_PROJ/legacy.pdf")
        self.assertEqual(convert_cloud_docs.mirror_source_sha256(metadata), source_hash)
        self.assertIn("byte-exact", body)

        self._run_full_sync()

        self.assertEqual(mirror.read_bytes(), original)

    def test_new_normal_mirror_has_only_canonical_schema_frontmatter_and_payload_hash(self):
        source = self.cloud_dir / "normal.pdf"
        source.write_bytes(b"normal cloud source")
        payload = "# Normal\n\nCanonical Markdown payload.\n"

        self._run_mocked_conversion(source, payload)

        metadata, body = convert_cloud_docs.parse_markdown_file(str(self.output_dir / "normal.md"))
        expected_keys = {
            "zone", "trust_level", "status", "source_uri", "source_sha256",
            "artifact_sha256", "synced_at", "converter", "data_classification",
            "retention_class", "owner", "instructions_are_data",
        }
        self.assertEqual(set(metadata), expected_keys)
        self.assertTrue(set(metadata) <= convert_cloud_docs.CANONICAL_CLOUD_FRONTMATTER_KEYS)
        schema = _data_zone_artifact_schema()
        self.assertFalse(schema["additionalProperties"])
        self.assertTrue(set(schema["required"]) <= set(metadata))
        self.assertTrue(set(metadata) <= set(schema["properties"]))
        self.assertEqual(metadata["zone"], "cloud")
        self.assertEqual(metadata["trust_level"], "untrusted_external")
        self.assertEqual(metadata["status"], "active")
        self.assertIsNotNone(datetime.fromisoformat(metadata["synced_at"]))
        self.assertEqual(metadata["source_uri"], "data/cloud/TEST_PROJ/normal.pdf")
        self.assertEqual(metadata["source_sha256"], convert_cloud_docs.calculate_sha256(str(source)))
        self.assertEqual(metadata["artifact_sha256"], convert_cloud_docs.calculate_markdown_payload_sha256(payload))
        self.assertEqual(metadata["artifact_sha256"], convert_cloud_docs.calculate_markdown_payload_sha256(body))
        self.assertEqual(metadata["owner"], "project:test_proj")
        self.assertEqual(metadata["retention_class"], "project-lifecycle")
        self.assertEqual(metadata["data_classification"], "internal")
        self.assertTrue(metadata["instructions_are_data"])
        self.assertNotIn("original_file", metadata)
        self.assertNotIn("ocr_applied", metadata)

    def test_ocr_derivative_mirror_keeps_details_in_filemap_not_frontmatter(self):
        source = self.cloud_dir / "scan.pdf"
        source.write_bytes(b"scanned cloud source")
        derivative = self.output_dir / "_derivatives" / "scan.pdf"
        derivative.parent.mkdir(parents=True)
        derivative.write_bytes(b"OCR derivative")
        payload = "# Scan\n\nOCR payload.\n"

        self._run_mocked_conversion(
            source,
            payload,
            ocr_applied=True,
            ocr_policy="local_derivative",
            derivative_path="memory/cloud/projects/test_proj/_derivatives/scan.pdf",
            derivative_sha256=convert_cloud_docs.calculate_sha256(str(derivative)),
            conversion_method="ocrmypdf-derivative",
            potential_quality_loss="OCR detail belongs in the filemap.",
        )

        metadata, body = convert_cloud_docs.parse_markdown_file(str(self.output_dir / "scan.md"))
        self.assertTrue(set(metadata) <= convert_cloud_docs.CANONICAL_CLOUD_FRONTMATTER_KEYS)
        self.assertEqual(metadata["artifact_sha256"], convert_cloud_docs.calculate_markdown_payload_sha256(body))
        self.assertFalse({"ocr_applied", "ocr_policy", "derivative_file", "derivative_sha256"} & set(metadata))
        filemap = json.loads((self.output_dir / "filemap.json").read_text(encoding="utf-8"))
        entry = filemap["files"]["data/cloud/TEST_PROJ/scan.pdf"]
        self.assertTrue(entry["ocr_applied"])
        self.assertEqual(entry["derivative"]["path"], "memory/cloud/projects/test_proj/_derivatives/scan.pdf")

    def test_actual_refresh_upgrades_legacy_frontmatter_to_canonical(self):
        source = self.cloud_dir / "refresh.pdf"
        source.write_bytes(b"version one")
        old_hash = convert_cloud_docs.calculate_sha256(str(source))
        mirror = self.output_dir / "refresh.md"
        mirror.write_text(
            "---\n"
            "original_file: \"data/cloud/TEST_PROJ/refresh.pdf\"\n"
            f"original_sha256: \"{old_hash}\"\n"
            "file_date: \"2000-01-01 00:00:00\"\n"
            "---\n\nlegacy body\n",
            encoding="utf-8",
        )
        source.write_bytes(b"version two requires a refresh")

        self._run_mocked_conversion(source, "# Refresh\n\nNew payload.\n")

        metadata, _ = convert_cloud_docs.parse_markdown_file(str(mirror))
        self.assertTrue(set(metadata) <= convert_cloud_docs.CANONICAL_CLOUD_FRONTMATTER_KEYS)
        self.assertEqual(metadata["source_sha256"], convert_cloud_docs.calculate_sha256(str(source)))
        self.assertNotIn("original_sha256", metadata)

    def test_metadata_policy_uses_declared_storage_values_before_stable_defaults(self):
        policy = convert_cloud_docs.resolve_cloud_metadata_policy(
            {
                "owner": "project-owner",
                "retention_class": "project-retention",
                "data_classification": "confidential",
            },
            {"owner": "storage-owner", "data_classification": "restricted"},
            "test_proj",
            False,
        )
        self.assertEqual(
            policy,
            {
                "owner": "storage-owner",
                "retention_class": "project-retention",
                "data_classification": "restricted",
            },
        )
        self.assertEqual(
            convert_cloud_docs.resolve_cloud_metadata_policy({}, {}, "test_topic", True),
            {
                "owner": "topic:test_topic",
                "retention_class": "project-lifecycle",
                "data_classification": "internal",
            },
        )

    def test_manual_description_and_custom_metadata_preservation_on_force(self):
        """Curated description and custom metadata keys are never lost during sync and --force."""
        pdf_file = self.cloud_dir / "Vertrag_v1.pdf"
        pdf_file.write_bytes(b"%PDF-1.4 sample contract content")
        
        doc_file = self.cloud_dir / "Anlage.doc"
        doc_file.write_bytes(b"\xd0\xcf\x11\xe0" + b"sample doc binary")
        
        # 1. Initial sync
        self._run_full_sync()
        
        # 2. Add manual descriptions and custom curated metadata to filemap.json
        filemap_json_path = self.output_dir / "filemap.json"
        with open(filemap_json_path, "r", encoding="utf-8") as f:
            fmap = json.load(f)
            
        pdf_key = "data/cloud/TEST_PROJ/Vertrag_v1.pdf"
        doc_key = "data/cloud/TEST_PROJ/Anlage.doc"
        
        fmap["files"][pdf_key]["description"] = "Hauptvertrag mit Kundensignatur"
        fmap["files"][pdf_key]["category"] = "legal"
        fmap["files"][pdf_key]["tags"] = ["nda", "2026"]
        fmap["files"][pdf_key]["curator"] = "Martin"
        
        fmap["files"][doc_key]["description"] = "Technische Anlage zum Vertrag"
        fmap["files"][doc_key]["priority"] = "high"
        
        with open(filemap_json_path, "w", encoding="utf-8") as f:
            json.dump(fmap, f, indent=2)
            
        # 3. Run sync with --force (re-converting everything)
        self._run_full_sync(force=True)
        
        # 4. Check filemap.json
        with open(filemap_json_path, "r", encoding="utf-8") as f:
            fmap_after = json.load(f)
            
        pdf_entry = fmap_after["files"][pdf_key]
        self.assertEqual(pdf_entry["description"], "Hauptvertrag mit Kundensignatur")
        self.assertEqual(pdf_entry["category"], "legal")
        self.assertEqual(pdf_entry["tags"], ["nda", "2026"])
        self.assertEqual(pdf_entry["curator"], "Martin")
        self.assertEqual(pdf_entry["conversion_status"], "converted")
        
        doc_entry = fmap_after["files"][doc_key]
        self.assertEqual(doc_entry["description"], "Technische Anlage zum Vertrag")
        self.assertEqual(doc_entry["priority"], "high")
        
        # 5. Check filemap.md
        filemap_md_path = self.output_dir / "filemap.md"
        md_text = filemap_md_path.read_text(encoding="utf-8")
        self.assertIn("Hauptvertrag mit Kundensignatur", md_text)
        self.assertIn("Technische Anlage zum Vertrag", md_text)

    def test_modified_file_updates_automated_fields_and_preserves_curated_fields(self):
        """When a cloud file is modified, sha256/mtime update, but description & custom keys stay intact."""
        file_path = self.cloud_dir / "Bericht.docx"
        file_path.write_bytes(b"VERSION 1 CONTENT")
        
        self._run_full_sync()
        
        # Curate metadata
        filemap_json_path = self.output_dir / "filemap.json"
        with open(filemap_json_path, "r", encoding="utf-8") as f:
            fmap = json.load(f)
        key = "data/cloud/TEST_PROJ/Bericht.docx"
        fmap["files"][key]["description"] = "Monatsbericht Q1"
        fmap["files"][key]["department"] = "Finance"
        fmap["files"][key]["custom_rating"] = 5
        old_sha = fmap["files"][key]["sha256"]
        
        with open(filemap_json_path, "w", encoding="utf-8") as f:
            json.dump(fmap, f, indent=2)
            
        # Modify cloud file content
        file_path.write_bytes(b"VERSION 2 UPDATED CONTENT WITH DIFFERENT HASH")
        new_calculated_sha = convert_cloud_docs.calculate_sha256(str(file_path))
        self.assertNotEqual(old_sha, new_calculated_sha)
        
        # Re-sync
        self._run_full_sync()
        
        with open(filemap_json_path, "r", encoding="utf-8") as f:
            fmap_after = json.load(f)
            
        entry = fmap_after["files"][key]
        self.assertEqual(entry["sha256"], new_calculated_sha, "sha256 must update to new content")
        self.assertEqual(entry["description"], "Monatsbericht Q1", "description must be preserved")
        self.assertEqual(entry["department"], "Finance", "custom key must be preserved")
        self.assertEqual(entry["custom_rating"], 5, "custom key must be preserved")

    def test_renamed_file_inherits_metadata_by_sha256(self):
        """When a file is renamed or moved in cloud, its metadata is migrated via SHA-256 matching."""
        old_file = self.cloud_dir / "old_draft_name.pdf"
        old_file.write_bytes(b"%PDF-1.4 UNIQUE_PAYLOAD_FOR_RENAME_TEST")
        
        self._run_full_sync()
        
        # Curate metadata on old path
        filemap_json_path = self.output_dir / "filemap.json"
        with open(filemap_json_path, "r", encoding="utf-8") as f:
            fmap = json.load(f)
            
        old_key = "data/cloud/TEST_PROJ/old_draft_name.pdf"
        fmap["files"][old_key]["description"] = "Entwurf zur Projektvereinbarung"
        fmap["files"][old_key]["confidentiality"] = "internal"
        fmap["files"][old_key]["custom_list"] = [1, 2, 3]
        
        with open(filemap_json_path, "w", encoding="utf-8") as f:
            json.dump(fmap, f, indent=2)
            
        # Rename file in cloud (move to subfolder)
        subfolder = self.cloud_dir / "final_docs"
        subfolder.mkdir(parents=True)
        new_file = subfolder / "project_agreement_signed.pdf"
        shutil.move(str(old_file), str(new_file))
        
        # Run sync
        self._run_full_sync()
        
        with open(filemap_json_path, "r", encoding="utf-8") as f:
            fmap_after = json.load(f)
            
        new_key = "data/cloud/TEST_PROJ/final_docs/project_agreement_signed.pdf"
        
        # Old key should be removed, new key should have inherited the metadata
        self.assertNotIn(old_key, fmap_after["files"])
        self.assertIn(new_key, fmap_after["files"])
        
        migrated_entry = fmap_after["files"][new_key]
        self.assertEqual(migrated_entry["description"], "Entwurf zur Projektvereinbarung")
        self.assertEqual(migrated_entry["confidentiality"], "internal")
        self.assertEqual(migrated_entry["custom_list"], [1, 2, 3])
        self.assertEqual(migrated_entry["markdown_mirror"], "memory/cloud/projects/test_proj/final_docs/project_agreement_signed.md")
        
        # Filemap.md should reflect the migrated description
        filemap_md_path = self.output_dir / "filemap.md"
        md_text = filemap_md_path.read_text(encoding="utf-8")
        self.assertIn("Entwurf zur Projektvereinbarung", md_text)
        self.assertIn("final_docs/project_agreement_signed.pdf", md_text)

    def test_deleted_files_and_filemap_protection_from_orphan_cleanup(self):
        """When a cloud file is deleted, orphaned mirror is removed, but filemap.md and filemap.json are NEVER deleted."""
        file1 = self.cloud_dir / "keep.pdf"
        file1.write_bytes(b"%PDF-1.4 KEEP_ME")
        
        file2 = self.cloud_dir / "delete_me.pdf"
        file2.write_bytes(b"%PDF-1.4 DELETE_ME")
        
        self._run_full_sync()
        
        mirror1 = self.output_dir / "keep.md"
        mirror2 = self.output_dir / "delete_me.md"
        filemap_json_path = self.output_dir / "filemap.json"
        filemap_md_path = self.output_dir / "filemap.md"
        
        self.assertTrue(mirror1.is_file())
        self.assertTrue(mirror2.is_file())
        self.assertTrue(filemap_json_path.is_file())
        self.assertTrue(filemap_md_path.is_file())
        
        # Delete file2 from cloud
        file2.unlink()
        
        # Re-sync
        self._run_full_sync()
        
        # mirror2 should be cleaned up
        self.assertTrue(mirror1.is_file())
        self.assertFalse(mirror2.exists(), "Orphaned mirror for deleted cloud file must be removed")
        
        # filemap.json and filemap.md must be strictly preserved
        self.assertTrue(filemap_json_path.is_file(), "filemap.json must NEVER be deleted by orphan cleanup")
        self.assertTrue(filemap_md_path.is_file(), "filemap.md must NEVER be deleted by orphan cleanup")
        
        with open(filemap_json_path, "r", encoding="utf-8") as f:
            fmap = json.load(f)
        self.assertIn("data/cloud/TEST_PROJ/keep.pdf", fmap["files"])
        self.assertNotIn("data/cloud/TEST_PROJ/delete_me.pdf", fmap["files"])

    def test_repeated_sync_runs_idempotency(self):
        """Multiple repeated sync runs produce stable, non-duplicated metadata."""
        file_path = self.cloud_dir / "Stabiledoc.docx"
        file_path.write_bytes(b"STABLE_CONTENT")
        
        self._run_full_sync()
        
        filemap_json_path = self.output_dir / "filemap.json"
        with open(filemap_json_path, "r", encoding="utf-8") as f:
            fmap = json.load(f)
            
        key = "data/cloud/TEST_PROJ/Stabiledoc.docx"
        fmap["files"][key]["description"] = "Stabile Beschreibung"
        fmap["files"][key]["my_tag"] = "test"
        
        with open(filemap_json_path, "w", encoding="utf-8") as f:
            json.dump(fmap, f, indent=2)
            
        # Run 3 consecutive syncs
        for _ in range(3):
            self._run_full_sync()
            
        with open(filemap_json_path, "r", encoding="utf-8") as f:
            fmap_final = json.load(f)
            
        self.assertEqual(len(fmap_final["files"]), 1)
        entry = fmap_final["files"][key]
        self.assertEqual(entry["description"], "Stabile Beschreibung")
        self.assertEqual(entry["my_tag"], "test")


if __name__ == "__main__":
    unittest.main()
