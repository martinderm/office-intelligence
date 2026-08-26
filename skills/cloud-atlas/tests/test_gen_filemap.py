import importlib.util
import tempfile
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "gen_filemap.py"
SPEC = importlib.util.spec_from_file_location("cloud_atlas_gen_filemap", SCRIPT_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


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


if __name__ == "__main__":
    unittest.main()
