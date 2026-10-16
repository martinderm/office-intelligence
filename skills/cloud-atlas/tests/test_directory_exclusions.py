"""Issue #2: one storage-local scope for scanning, provenance and cleanup."""

import contextlib
import io
import json
import re
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import convert_cloud_docs as CONVERT
import gen_filemap as FILEMAP


class DirectoryExclusionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.scan = "data/cloud/shared"
        self.output = "memory/cloud/projects/example"
        self.config = {
            "scan_dir": self.scan, "output_dir": self.output,
            "output_json": f"{self.output}/filemap.json",
            "output_md": f"{self.output}/filemap.md",
        }
        self.catalog()
        self.source("keep.pdf")

    def catalog(self, kind="project"):
        item = {"id": "example", "title": "Example", "cloud_sync": {"default": self.config}}
        if kind == "subtopic":
            item = {"id": "example", "subtopics": [{**item, "id": "sub", "status": "active"}]}
        path = self.root / "memory/references" / ("projects" if kind == "project" else "topics")
        path.mkdir(parents=True, exist_ok=True)
        (path / f"{'projects' if kind == 'project' else 'topics'}.json").write_text(json.dumps([item]), encoding="utf-8")

    def source(self, path, content=b"synthetic source"):
        target = self.root / self.scan / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return target

    def run_cli(self, module, kind="project", *extra):
        argv = ["cli", "--project-id" if kind == "project" else "--topic-id", "example",
                "--workspace-root", str(self.root), "--json", *extra]
        if kind == "subtopic":
            argv += ["--subtopic-id", "sub"]
        stdout = io.StringIO()
        with mock.patch.object(module.sys, "argv", argv), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            code = module.main()
        return code, json.loads(stdout.getvalue())

    def convert(self, kind="project"):
        def fake(tasks, **kwargs):
            return {task["src_rel"]: {"success": True, "markdown_body": "# fixture\n",
                                      "conversion_method": "fixture"} for task in tasks}
        with mock.patch.object(CONVERT, "run_conversion_tasks", side_effect=fake):
            code, result = self.run_cli(CONVERT, kind)
        self.assertEqual(0, code, result)
        return result

    def filemap(self):
        return json.loads((self.root / self.config["output_json"]).read_text(encoding="utf-8"))

    def test_both_scanners_prune_nested_exclusions_but_not_similar_siblings(self):
        self.config["exclude_dirs"] = ["./Area//", "Specialized Area\\Nested/"]
        self.catalog()
        for path in ("Area/deep/private.pdf", "Area Extra/public.pdf", "Specialized Area/Nested/private.pdf",
                     "Specialized Area/visible.pdf", "area/lower.pdf"):
            self.source(path)
        expected = {f"{self.scan}/{p}" for p in ("keep.pdf", "Area Extra/public.pdf", "Specialized Area/visible.pdf")}
        converted = self.convert()
        self.assertEqual(expected, set(self.filemap()["files"]))
        self.assertEqual(["Area", "Specialized Area/Nested"], converted["data"]["storages"][0]["exclude_dirs"])
        code, result = self.run_cli(FILEMAP)
        self.assertEqual(0, code, result)
        self.assertEqual(expected, set(self.filemap()["files"]))
        self.assertEqual(["Area", "Specialized Area/Nested"], self.filemap()["exclude_dirs"])
        self.assertFalse((self.root / self.output / "Area").exists())

    def test_excluded_tree_is_pruned_before_hash_or_conversion(self):
        self.config["exclude_dirs"] = ["Area"]
        self.catalog()
        self.source("Area/private.pdf")
        for module in (CONVERT, FILEMAP):
            original = module.calculate_sha256
            def guarded(path):
                self.assertNotIn("Area", Path(path).parts)
                return original(path)
            with mock.patch.object(module, "calculate_sha256", side_effect=guarded):
                if module is CONVERT:
                    self.convert()
                else:
                    self.assertEqual(0, self.run_cli(FILEMAP)[0])

    def test_all_catalog_levels_propagate_normalized_exclusions(self):
        self.config["exclude_dirs"] = ["./Area/Nested/", "Area", "Area", "Other Folder"]
        for kind in ("project", "topic", "subtopic"):
            self.catalog(kind)
            for module in (CONVERT, FILEMAP):
                with self.subTest(kind=kind, module=module.__name__):
                    config = module.resolve_all_sync_configs(str(self.root), "example", kind != "project", None,
                                                             "sub" if kind == "subtopic" else None)["default"]
                    self.assertEqual(["Area", "Other Folder"], config["exclude_dirs"])

    def test_invalid_exclusions_fail_before_any_output_at_every_catalog_level(self):
        for value in (None, "Area", {}, [False], [""], ["."], [".."], ["A/../B"],
                      ["/absolute"], ["C:/absolute"], ["C:relative"], ["\\\\server\\share"], ["bad\x00path"]):
            self.config["exclude_dirs"] = value
            for kind in ("project", "topic", "subtopic"):
                self.catalog(kind)
                for module in (CONVERT, FILEMAP):
                    with self.subTest(value=value, kind=kind, module=module.__name__):
                        code, result = self.run_cli(module, kind)
                        self.assertNotEqual(0, code, result)
                        self.assertFalse((self.root / self.output).exists())

    def test_default_and_empty_exclusions_preserve_complete_inventory(self):
        self.source("Area/private.pdf")
        for explicit in (False, True):
            if explicit:
                self.config["exclude_dirs"] = []
            self.catalog()
            self.convert()
            self.assertEqual(0, self.run_cli(FILEMAP)[0])
            self.assertEqual(2, len(self.filemap()["files"]))
            self.assertEqual([], self.filemap()["exclude_dirs"])

    def test_changed_exclusions_retain_previous_mirrors_and_custom_derivatives(self):
        self.source("Area/private.pdf", b"same content")
        self.convert()
        old = self.filemap()
        source = f"{self.scan}/Area/private.pdf"
        mirror = self.root / old["files"][source]["markdown_mirror"]
        custom = self.root / self.output / "custom/private.pdf"
        custom.parent.mkdir(parents=True)
        custom.write_bytes(b"retained derivative")
        old["files"][source]["derivative"] = {"path": custom.relative_to(self.root).as_posix(), "format": "pdf"}
        (self.root / self.config["output_json"]).write_text(json.dumps(old), encoding="utf-8")
        mirror_bytes = mirror.read_bytes()
        self.config["exclude_dirs"] = ["Area"]
        self.catalog()
        self.convert()
        self.assertEqual(mirror_bytes, mirror.read_bytes())
        self.assertEqual(b"retained derivative", custom.read_bytes())
        self.assertNotIn(source, self.filemap()["files"])
        self.convert()  # still protected after the old entry is gone
        self.assertEqual(mirror_bytes, mirror.read_bytes())
        self.assertTrue(custom.exists())

    def test_separate_storage_with_overlapping_source_is_never_cleaned(self):
        self.source("Area/private.pdf")
        separate = self.root / "memory/cloud/topics/separate"
        separate.mkdir(parents=True)
        sentinel = separate / "private.md"
        sentinel.write_text("other storage", encoding="utf-8")
        self.config["exclude_dirs"] = ["Area"]
        self.catalog()
        before = {p: p.read_bytes() for p in (self.root / self.scan).rglob("*") if p.is_file()}
        self.convert()
        self.assertEqual("other storage", sentinel.read_text(encoding="utf-8"))
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_excluded_metadata_is_not_reused_as_rename_by_hash(self):
        self.source("Area/private.pdf", b"identical")
        self.convert()
        old = self.filemap()
        old["files"][f"{self.scan}/Area/private.pdf"]["description"] = "private annotation"
        (self.root / self.config["output_json"]).write_text(json.dumps(old), encoding="utf-8")
        self.source("new.pdf", b"identical")
        self.config["exclude_dirs"] = ["Area"]
        self.catalog()
        self.convert()
        self.assertNotEqual("private annotation", self.filemap()["files"][f"{self.scan}/new.pdf"]["description"])

    def test_exclusion_names_directories_not_a_same_named_root_file(self):
        self.source("Area")
        self.config["exclude_dirs"] = ["Area"]
        self.catalog()
        code, result = self.run_cli(FILEMAP)
        self.assertEqual(0, code, result)
        self.assertIn(f"{self.scan}/Area", self.filemap()["files"])

    def test_cleanup_removes_only_owned_in_scope_orphans_when_exclusions_active(self):
        source = self.source("orphan.pdf")
        self.convert()
        mirror = self.root / self.output / "orphan.md"
        self.assertTrue(mirror.exists())
        source.unlink()
        foreign = self.root / self.output / "unowned.md"
        foreign.write_text("unowned", encoding="utf-8")
        self.config["exclude_dirs"] = ["Area"]
        self.catalog()
        self.convert()
        self.assertFalse(mirror.exists())
        self.assertEqual("unowned", foreign.read_text(encoding="utf-8"))

    def test_generator_rejects_noncanonical_provenance_and_excluded_entries(self):
        self.source("Area/private.pdf")
        self.assertEqual(0, self.run_cli(FILEMAP)[0])
        original = self.filemap()
        for exclusions in (["./Area/"], ["Area", "Area/Nested"], ["../Area"], ["Area"]):
            broken = {**original, "exclude_dirs": exclusions}
            with self.subTest(exclusions=exclusions), self.assertRaises(ValueError):
                FILEMAP.validate_filemap(broken, self.root)

    def test_nested_storage_can_sync_its_excluded_subtree_with_separate_outputs(self):
        self.source("Area/private.pdf")
        broad_config = dict(self.config, exclude_dirs=["Area"])
        self.config = {
            "scan_dir": f"{self.scan}/Area", "output_dir": "memory/cloud/topics/separate",
            "output_json": "memory/cloud/topics/separate/filemap.json",
            "output_md": "memory/cloud/topics/separate/filemap.md",
        }
        self.catalog("topic")
        self.convert("topic")
        self.assertEqual(0, self.run_cli(FILEMAP, "topic")[0])
        nested_outputs = {p: p.read_bytes() for p in (self.root / self.config["output_dir"]).rglob("*") if p.is_file()}
        self.config = broad_config
        self.catalog()
        self.convert()
        self.assertEqual(0, self.run_cli(FILEMAP)[0])
        self.assertEqual({f"{self.scan}/keep.pdf"}, set(self.filemap()["files"]))
        self.assertEqual(nested_outputs, {p: p.read_bytes() for p in nested_outputs})

    def test_formal_schemas_constrain_the_same_exclusion_field(self):
        filemap = json.loads((SCRIPTS.parent / "references/filemap.schema.json").read_text(encoding="utf-8"))
        project = json.loads((SCRIPTS.parents[1] / "project-catalog-entry/references/projects.schema.json").read_text(encoding="utf-8"))
        definitions = (
            filemap["$defs"]["exclude_dirs"],
            project["$defs"]["project"]["properties"]["cloud_sync"]["additionalProperties"]["properties"]["exclude_dirs"],
        )
        for definition in definitions:
            self.assertEqual("array", definition["type"])
            pattern = definition["items"]["pattern"]
            for path in ("Area", "Area/Nested", "./Other Folder/", "Area\\Nested"):
                self.assertIsNotNone(re.fullmatch(pattern, path), path)
            for path in (".", "./", "../Area", "/Area", "C:Area", "A/../B", "\\\\server\\share", "bad\x00path"):
                self.assertIsNone(re.fullmatch(pattern, path), path)

    def test_legacy_filemap_without_exclusion_provenance_is_still_valid(self):
        self.assertEqual(0, self.run_cli(FILEMAP)[0])
        legacy = self.filemap()
        legacy.pop("exclude_dirs", None)
        self.assertTrue(FILEMAP.validate_filemap(legacy, self.root))

    def test_retained_custom_mirror_is_not_claimed_by_a_later_source(self):
        self.source("Area/private.pdf", b"private source")
        self.convert()
        old = self.filemap()
        source = f"{self.scan}/Area/private.pdf"
        original = self.root / old["files"][source]["markdown_mirror"]
        retained = self.root / self.output / "new.md"
        original.rename(retained)
        old["files"][source]["markdown_mirror"] = retained.relative_to(self.root).as_posix()
        (self.root / self.config["output_json"]).write_text(json.dumps(old), encoding="utf-8")
        self.config["exclude_dirs"] = ["Area"]
        self.catalog()
        self.convert()
        before = retained.read_bytes()
        self.source("new.pdf", b"unrelated source")
        for module in (FILEMAP, CONVERT):
            with self.subTest(module=module.__name__):
                def fake(tasks, **kwargs):
                    return {task["src_rel"]: {"success": True, "markdown_body": "# unrelated\n",
                                              "conversion_method": "fixture"} for task in tasks}
                with mock.patch.object(CONVERT, "run_conversion_tasks", side_effect=fake):
                    code, result = self.run_cli(module)
                self.assertNotEqual(0, code, result)
                self.assertEqual(before, retained.read_bytes())
                self.assertNotIn(f"{self.scan}/new.pdf", self.filemap()["files"])


if __name__ == "__main__":
    unittest.main()
