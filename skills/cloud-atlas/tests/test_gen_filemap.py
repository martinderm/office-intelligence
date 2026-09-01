import importlib.util
import json
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
            target.write_bytes(b"existing target\r\n")

    def tearDown(self):
        self.temporary_directory.cleanup()

    def assert_targets_unchanged_and_temps_cleaned(self):
        for target in self.targets:
            with self.subTest(target=target.name):
                self.assertEqual(b"existing target\r\n", target.read_bytes())
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


if __name__ == "__main__":
    unittest.main()
