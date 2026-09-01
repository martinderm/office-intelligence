import importlib.util
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "gen_filemap.py"
SPEC = importlib.util.spec_from_file_location("cloud_atlas_gen_filemap", SCRIPT_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class AtomicWriteTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.targets = (
            self.root / "memory" / "cloud" / "projects" / "example" / "filemap.json",
            self.root / "memory" / "cloud" / "projects" / "example" / "filemap.md",
            self.root / "memory" / "references" / "projects" / "projects.json",
        )
        for target in self.targets:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"existing target\n")

    def tearDown(self):
        self.temporary_directory.cleanup()

    def assert_targets_unchanged_and_temps_cleaned(self):
        for target in self.targets:
            with self.subTest(target=target.name):
                self.assertEqual(b"existing target\n", target.read_bytes())
                self.assertEqual([], list(target.parent.glob(f".{target.name}.*.tmp")))

    def test_interruption_preserves_all_existing_targets(self):
        for target in self.targets:
            with self.subTest(target=target.name):
                with mock.patch.object(MODULE.os, "fsync", side_effect=KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        MODULE.atomic_write_text(target, "replacement")

        self.assert_targets_unchanged_and_temps_cleaned()

    def test_partial_write_failure_preserves_all_existing_targets(self):
        real_fdopen = MODULE.os.fdopen

        class FailingWriter:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                self.stream.close()

            def write(self, content):
                self.stream.write(content[:5])
                raise OSError("simulated partial write failure")

            def flush(self):
                self.stream.flush()

            def fileno(self):
                return self.stream.fileno()

        def failing_fdopen(fd, *args, **kwargs):
            return FailingWriter(real_fdopen(fd, *args, **kwargs))

        for target in self.targets:
            with self.subTest(target=target.name):
                with mock.patch.object(MODULE.os, "fdopen", side_effect=failing_fdopen):
                    with self.assertRaisesRegex(OSError, "simulated partial write failure"):
                        MODULE.atomic_write_text(target, "replacement")

        self.assert_targets_unchanged_and_temps_cleaned()

    def test_replace_failure_preserves_all_existing_targets(self):
        for target in self.targets:
            with self.subTest(target=target.name):
                with mock.patch.object(MODULE.os, "replace", side_effect=OSError("replace failed")):
                    with self.assertRaisesRegex(OSError, "replace failed"):
                        MODULE.atomic_write_text(target, "replacement")

        self.assert_targets_unchanged_and_temps_cleaned()

    def test_catalog_update_uses_atomic_replacement(self):
        catalog = self.targets[2]
        catalog_data = [{
            "id": "example",
            "cloud_sync": {"default": {"last_synced_at": "old"}},
        }]
        catalog.write_text(json.dumps(catalog_data), encoding="utf-8")
        original = catalog.read_bytes()

        with mock.patch.object(MODULE.os, "replace", side_effect=OSError("replace failed")):
            MODULE.update_config_last_synced_at(self.root, "example", False, "default")

        self.assertEqual(original, catalog.read_bytes())
        self.assertEqual([], list(catalog.parent.glob(".projects.json.*.tmp")))


class MirrorPathPolicyTests(unittest.TestCase):
    def test_canonical_existing_mirror_replaces_legacy_path(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            mirror = root / "memory" / "cloud" / "topics" / "example" / "doc.md"
            mirror.parent.mkdir(parents=True)
            mirror.write_text("mirror", encoding="utf-8")

            selected = MODULE.select_markdown_mirror(
                root,
                "data/cloud/source/doc.pdf",
                "data/cloud/source",
                "memory/cloud/topics/example",
                {"markdown_mirror": "memory/references/topics/example/cloud/doc.md"},
            )

            self.assertEqual("memory/cloud/topics/example/doc.md", selected)

    def test_existing_custom_path_inside_output_dir_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            custom = root / "memory" / "cloud" / "topics" / "example" / "custom" / "note.md"
            custom.parent.mkdir(parents=True)
            custom.write_text("mirror", encoding="utf-8")

            selected = MODULE.select_markdown_mirror(
                root,
                "data/cloud/source/note.txt",
                "data/cloud/source",
                "memory/cloud/topics/example",
                {"markdown_mirror": "memory/cloud/topics/example/custom/note.md"},
            )

            self.assertEqual("memory/cloud/topics/example/custom/note.md", selected)

    def test_missing_or_cross_zone_path_is_dropped(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            outside = root / "memory" / "references" / "topics" / "example" / "doc.md"
            outside.parent.mkdir(parents=True)
            outside.write_text("legacy", encoding="utf-8")

            selected = MODULE.select_markdown_mirror(
                root,
                "data/cloud/source/doc.pdf",
                "data/cloud/source",
                "memory/cloud/topics/example",
                {"markdown_mirror": "memory/references/topics/example/doc.md"},
            )

            self.assertIsNone(selected)

    def test_unsafe_relative_path_is_rejected(self):
        self.assertIsNone(MODULE.normalize_workspace_relative_path("../outside.md"))
        self.assertIsNone(MODULE.normalize_workspace_relative_path("C:/outside.md"))

    def test_markdown_link_target_encodes_unicode_spaces_and_parentheses(self):
        target = "Kaufunterlagen/Zu übergeben/KYC Person (003)#final%.md"

        encoded = MODULE.encode_markdown_link_target(target)

        self.assertEqual(
            "Kaufunterlagen/Zu%20%C3%BCbergeben/KYC%20Person%20%28003%29%23final%25.md",
            encoded,
        )


    def test_canonical_derivative_path_for_doc_file(self):
        deriv = MODULE.canonical_derivative_path(
            "data/cloud/source/contracts/archive.doc",
            "data/cloud/source",
            "memory/cloud/topics/example"
        )
        self.assertEqual("memory/cloud/topics/example/_derivatives/contracts/archive.docx", deriv)

    def test_canonical_derivative_path_for_non_doc_returns_none(self):
        self.assertIsNone(MODULE.canonical_derivative_path("data/cloud/source/doc.pdf", "data/cloud/source", "memory/cloud/topics/example"))

    def test_select_derivative_picks_existing_canonical(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            deriv = root / "memory" / "cloud" / "topics" / "example" / "_derivatives" / "contracts" / "old.docx"
            deriv.parent.mkdir(parents=True)
            deriv.write_bytes(b"DERIVATIVE_DOCX")

            selected = MODULE.select_derivative(
                root,
                "data/cloud/source/contracts/old.doc",
                "data/cloud/source",
                "memory/cloud/topics/example",
                {}
            )
            self.assertIsNotNone(selected)
            self.assertEqual("memory/cloud/topics/example/_derivatives/contracts/old.docx", selected["path"])
            self.assertEqual("docx", selected["format"])
            self.assertIsNotNone(selected["sha256"])


    def test_find_workspace_root_finds_marker_and_env(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            sub = root / "nested" / "deep"
            sub.mkdir(parents=True)
            (root / "AGENTS.md").write_text("# Root\n", encoding="utf-8")

            found = MODULE.find_workspace_root(str(sub))
            self.assertEqual(str(root.resolve()), str(Path(found).resolve()))

            # Test env var override
            custom_env_root = root / "custom_env"
            custom_env_root.mkdir()
            try:
                import os
                os.environ["CLOUD_ATLAS_WORKSPACE_ROOT"] = str(custom_env_root)
                self.assertEqual(str(custom_env_root.resolve()), str(Path(MODULE.find_workspace_root(str(sub))).resolve()))
            finally:
                os.environ.pop("CLOUD_ATLAS_WORKSPACE_ROOT", None)

    def test_find_workspace_root_permission_error_resilience(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            sub = root / "folder"
            sub.mkdir()
            
            import unittest.mock as mock
            with mock.patch("os.scandir", side_effect=PermissionError("Access denied")):
                # Should not raise PermissionError
                found = MODULE.find_workspace_root(str(sub))
                self.assertIsNotNone(found)


class FilemapSchemaTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.cloud_dir = self.root / "data" / "cloud" / "TEST"
        self.output_dir = self.root / "memory" / "cloud" / "projects" / "test"
        self.cloud_dir.mkdir(parents=True)
        self.output_dir.mkdir(parents=True)
        projects_dir = self.root / "memory" / "references" / "projects"
        projects_dir.mkdir(parents=True)
        (projects_dir / "projects.json").write_text(
            json.dumps(
                [
                    {
                        "id": "test",
                        "title": "Test",
                        "cloud_sync": {
                            "default": {
                                "scan_dir": "data/cloud/TEST",
                                "output_json": "memory/cloud/projects/test/filemap.json",
                                "output_md": "memory/cloud/projects/test/filemap.md",
                                "output_dir": "memory/cloud/projects/test",
                            }
                        },
                    }
                ]
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def _run_generator(self):
        with mock.patch.object(
            MODULE.sys,
            "argv",
            ["gen_filemap.py", "--project-id", "test", "--workspace-root", str(self.root)],
        ):
            MODULE.main()

    def test_schema_delegates_artifact_metadata_to_normative_data_zone_schema(self):
        schema = json.loads(
            (Path(MODULE.__file__).resolve().parents[1] / "references" / "filemap.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(MODULE.FILEMAP_SCHEMA_URI, schema["$id"])
        self.assertEqual(MODULE.FILEMAP_SCHEMA_URI, schema["properties"]["$schema"]["const"])
        self.assertEqual(
            "https://raw.githubusercontent.com/martinderm/office-intelligence/main/skills/cloud-atlas/references/filemap.schema.json",
            MODULE.FILEMAP_SCHEMA_URI,
        )
        self.assertEqual(
            "https://github.com/martinderm/agent-architecture/schemas/data-zone-artifact.schema.json",
            schema["$defs"]["file_entry"]["properties"]["artifact_metadata"]["$ref"],
        )
        self.assertIn("not itself a data-zone-artifact", schema["description"])

    def _write_canonical_mirror(self, source_rel="contract.pdf", artifact_sha256=None):
        source = self.cloud_dir / Path(*source_rel.split("/"))
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"contract source")
        source_sha256 = MODULE.calculate_sha256(source)
        mirror = self.output_dir / Path(source_rel).with_suffix(".md")
        mirror.parent.mkdir(parents=True, exist_ok=True)
        payload = "# Contract\n"
        payload_sha256 = artifact_sha256 or hashlib.sha256(payload.encode("utf-8")).hexdigest()
        mirror.write_text(
            "---\n"
            'zone: "cloud"\n'
            'trust_level: "untrusted_external"\n'
            'status: "active"\n'
            f'source_uri: "data/cloud/TEST/{source_rel}"\n'
            f'source_sha256: "{source_sha256}"\n'
            f'artifact_sha256: "{payload_sha256}"\n'
            'synced_at: "2026-09-01T12:30:45+02:00"\n'
            'converter: "markitdown-direct"\n'
            'data_classification: "internal"\n'
            'retention_class: "project-lifecycle"\n'
            'owner: "project:test"\n'
            "instructions_are_data: true\n"
            "---\n\n"
            + payload,
            encoding="utf-8",
        )
        return source

    def test_generated_container_and_canonical_artifact_metadata_validate(self):
        self._write_canonical_mirror()

        self._run_generator()

        filemap_path = self.output_dir / "filemap.json"
        filemap = json.loads(filemap_path.read_text(encoding="utf-8"))
        self.assertTrue(MODULE.validate_filemap(filemap, self.root))
        self.assertEqual(MODULE.FILEMAP_SCHEMA_VERSION, filemap["schema_version"])
        self.assertEqual("cloud-filemap", filemap["kind"])
        self.assertEqual("project", filemap["scope"])
        self.assertEqual("default", filemap["storage_id"])
        entry = filemap["files"]["data/cloud/TEST/contract.pdf"]
        self.assertEqual("cloud", entry["artifact_metadata"]["zone"])
        self.assertEqual("data/cloud/TEST/contract.pdf", entry["artifact_metadata"]["source_uri"])

    def test_invalid_artifact_metadata_fails_closed_before_replacing_filemap(self):
        self._write_canonical_mirror(artifact_sha256="not-a-sha256")
        filemap_path = self.output_dir / "filemap.json"
        original = b'{"previous":"valid"}\n'
        filemap_path.write_bytes(original)

        with self.assertRaisesRegex(ValueError, r"artifact_sha256"):
            self._run_generator()

        self.assertEqual(original, filemap_path.read_bytes())

    def test_invalid_source_inventory_hash_is_rejected(self):
        self._write_canonical_mirror()
        self._run_generator()
        filemap = json.loads((self.output_dir / "filemap.json").read_text(encoding="utf-8"))
        filemap["files"]["data/cloud/TEST/contract.pdf"]["sha256"] = "invalid"

        with self.assertRaisesRegex(ValueError, r"files.*sha256"):
            MODULE.validate_filemap(filemap, self.root)

    def test_canonical_metadata_requires_an_available_source(self):
        source = self._write_canonical_mirror()
        self._run_generator()
        filemap = json.loads((self.output_dir / "filemap.json").read_text(encoding="utf-8"))
        source.unlink()

        with self.assertRaisesRegex(ValueError, r"source file is unavailable"):
            MODULE.validate_filemap(filemap, self.root)

    def test_legacy_mirror_remains_a_valid_inventory_entry_without_artifact_metadata(self):
        source = self._write_canonical_mirror()
        mirror = self.output_dir / "contract.md"
        mirror.write_text(
            "---\n"
            f'original_file: "data/cloud/TEST/contract.pdf"\n'
            f'original_sha256: "{MODULE.calculate_sha256(source)}"\n'
            'file_date: "2026-09-01 12:30:45"\n'
            "---\n\nLegacy payload\n",
            encoding="utf-8",
        )

        self._run_generator()

        filemap = json.loads((self.output_dir / "filemap.json").read_text(encoding="utf-8"))
        entry = filemap["files"]["data/cloud/TEST/contract.pdf"]
        self.assertNotIn("artifact_metadata", entry)
        self.assertTrue(MODULE.validate_filemap(filemap, self.root))

    def test_file_entries_are_serialized_in_stable_path_order(self):
        for relative in ("z-last.pdf", "a-first.pdf", "middle.pdf"):
            source = self.cloud_dir / relative
            source.write_bytes(relative.encode("utf-8"))

        self._run_generator()

        filemap = json.loads((self.output_dir / "filemap.json").read_text(encoding="utf-8"))
        self.assertEqual(
            list(filemap["files"]),
            [
                "data/cloud/TEST/a-first.pdf",
                "data/cloud/TEST/middle.pdf",
                "data/cloud/TEST/z-last.pdf",
            ],
        )

    def test_generation_and_validation_do_not_require_shared_memory_schema(self):
        self._write_canonical_mirror("portable.pdf")
        unavailable_schema_root = self.root / "not-installed"
        runtime_scripts = self.root / "standalone-skill" / "scripts"
        runtime_core = runtime_scripts / "core"
        runtime_core.mkdir(parents=True)
        shutil.copy2(MODULE.__file__, runtime_scripts / "gen_filemap.py")
        shutil.copy2(
            Path(MODULE.__file__).resolve().parent / "core" / "metadata.py",
            runtime_core / "metadata.py",
        )

        with mock.patch.dict(
            os.environ,
            {"AGENT_ARCHITECTURE_SCHEMA_ROOT": str(unavailable_schema_root)},
            clear=False,
        ):
            result = subprocess.run(
                [
                    sys.executable,
                    str(runtime_scripts / "gen_filemap.py"),
                    "--project-id",
                    "test",
                    "--workspace-root",
                    str(self.root),
                ],
                check=False,
                capture_output=True,
                text=True,
                env=os.environ.copy(),
            )

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

        filemap = json.loads((self.output_dir / "filemap.json").read_text(encoding="utf-8"))
        self.assertTrue(MODULE.validate_filemap(filemap, self.root))
        self.assertEqual(["data/cloud/TEST/portable.pdf"], list(filemap["files"]))


if __name__ == "__main__":
    unittest.main()
