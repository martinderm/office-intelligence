"""Single-file mode (``--only-file``, Issue #22) of ``convert_cloud_docs.py``.

The fixture is a synthetic topic filemap in realistic size (2,000+ entries) with
deliberately irregular foreign formatting.  Every failure test compares a hash
snapshot of the whole workspace before and after the run.
"""

import contextlib
import hashlib
import io
import json
import multiprocessing
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
FIXTURE_PDF = Path(__file__).resolve().parent / "fixtures" / "tagged_ocrmypdf_small_text.pdf"
sys.path.insert(0, str(SCRIPTS_DIR))
import convert_cloud_docs as MODULE
from core import filemap_merge

ENTRY_COUNT = 2100
SOURCE = "data/cloud/DEMO/g_neu.doc"
MIRROR = "memory/cloud/topics/demo/g_neu.md"
DERIVATIVE = "memory/cloud/topics/demo/_derivatives/g_neu.docx"
FILEMAP = "memory/cloud/topics/demo/filemap.json"
SHA = "a" * 64


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def snapshot(root):
    """Map every workspace file to its content hash (symlinks are not followed)."""
    result = {}
    for directory, _dirs, files in os.walk(root):
        for name in files:
            path = Path(directory) / name
            result[path.relative_to(root).as_posix()] = sha256_bytes(path.read_bytes())
    return result


def foreign_entry_text(index, newline="\n"):
    key = f"data/cloud/DEMO/f{index:04d}.pdf" if index < ENTRY_COUNT - 100 else f"data/cloud/DEMO/sub/s{index:04d}.pdf"
    mirror = f"memory/cloud/topics/demo/f{index:04d}.md"
    body = {
        "version": "N/A", "mtime": "2026-01-01 00:00:00", "size": f"{index % 900 + 1}.0 KB",
        "sha256": f"{index:064x}", "description": f"Eintrag {index} Fühstück",
        "markdown_mirror": mirror, "conversion_status": "converted",
    }
    if index % 113 == 0:
        # Irregular but valid JSON: escapes, odd spacing, inline layout.
        return (
            f'    "{key}" :   {{"version":"N/A" ,  "mtime":"2026-01-01 00:00:00","size":"{body["size"]}",'
            f'"sha256":"{body["sha256"]}","description":"Fr\\u00fchst\\u00fcck \\/ {index}",'
            f'"markdown_mirror":"{mirror}","conversion_status":"converted", "note" : [1,  2.50, 3e2]}}'
        )
    dumped = json.dumps(body, indent=2, ensure_ascii=False)
    return f'    {json.dumps(key)}: ' + dumped.replace("\n", newline + "    ")


def build_filemap_text(count=ENTRY_COUNT, newline="\n"):
    entries = [foreign_entry_text(index, newline) for index in range(count)]
    header = [
        '{',
        f'  "$schema": "{filemap_merge.FILEMAP_SCHEMA_URI}",',
        '  "schema_version": 1,',
        '  "kind": "cloud-filemap",',
        '  "scope": "topic",',
        '  "storage_id": "default",',
        '  "project": "demo",',
        '  "project_title": "Demo",',
        '  "scan_dir": "data/cloud/DEMO",',
        '  "output_dir": "memory/cloud/topics/demo",',
        '  "exclude_dirs": [],',
        '  "updated_at": "2026-01-01 00:00:00",',
        '  "files": {',
    ]
    return newline.join(header) + newline + ("," + newline).join(entries) + newline + "  }" + newline + "}" + newline


def hang_forever(seconds, connection):
    # Keep the pipe end open like a live worker; the scheduler must time out.
    time.sleep(seconds)
    connection.close()


class HangingProcess:
    """Stand-in worker: leaves partial output behind, then never finishes."""

    partial_paths = []

    def __init__(self, target=None, args=(), kwargs=None):
        self._process = multiprocessing.get_context("spawn").Process(target=hang_forever, args=(120, args[1]))

    def start(self):
        for path in self.partial_paths:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(b"PARTIAL")
        self._process.start()

    def __getattr__(self, name):
        return getattr(self._process, name)


class OnlyFileTestCase(unittest.TestCase):
    keys = {"action", "success", "state", "message", "data", "error"}

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        (self.root / "AGENTS.md").write_text("# Test workspace\n", encoding="utf-8")
        (self.root / ".workspace-root").touch()
        self.cloud = self.root / "data" / "cloud" / "DEMO"
        self.cloud.mkdir(parents=True)
        self.output = self.root / "memory" / "cloud" / "topics" / "demo"
        self.output.mkdir(parents=True)
        topics = self.root / "memory" / "references" / "topics"
        topics.mkdir(parents=True)
        (topics / "topics.json").write_text(json.dumps([{
            "id": "demo", "title": "Demo",
            "cloud_sync": {"default": {
                "scan_dir": "data/cloud/DEMO",
                "output_json": FILEMAP,
                "output_md": "memory/cloud/topics/demo/filemap.md",
                "output_dir": "memory/cloud/topics/demo",
            }},
        }]), encoding="utf-8")
        self.filemap_path = self.root / FILEMAP
        self.saved_environment = {
            name: os.environ.get(name)
            for name in ("CLOUD_ATLAS_DOC_CONVERTER_MOCK", "CLOUD_ATLAS_OCR_MOCK", "CLOUD_ATLAS_MOCK_IS_SCAN")
        }
        os.environ["CLOUD_ATLAS_DOC_CONVERTER_MOCK"] = "mock:test"
        for name in ("CLOUD_ATLAS_OCR_MOCK", "CLOUD_ATLAS_MOCK_IS_SCAN"):
            os.environ.pop(name, None)
        self.source_path = self.root / SOURCE
        self.source_path.write_bytes(b"\xd0\xcf\x11\xe0" + b"NEW_DOC_PAYLOAD")

    def tearDown(self):
        for name, value in self.saved_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.temporary_directory.cleanup()

    # -- helpers ---------------------------------------------------------
    def write_filemap(self, text=None):
        text = build_filemap_text() if text is None else text
        self.filemap_path.write_bytes(text.encode("utf-8"))
        return text

    def run_main(self, *arguments, patches=()):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            stack.enter_context(mock.patch.object(MODULE.sys, "argv", ["convert_cloud_docs.py", *arguments]))
            stack.enter_context(contextlib.redirect_stdout(stdout))
            stack.enter_context(contextlib.redirect_stderr(stderr))
            exit_code = MODULE.main()
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def only_file(self, source=SOURCE, *extra, patches=()):
        return self.run_main(
            "--topic-id", "demo", "--workspace-root", str(self.root),
            "--only-file", source, "--json", *extra, patches=patches,
        )

    def envelope(self, stdout):
        result = json.loads(stdout)
        self.assertEqual(self.keys, set(result))
        self.assertEqual("convert_cloud_docs", result["action"])
        return result

    def foreign_hash(self, text, source=SOURCE):
        """Hash of every member except ``source``, over the raw character spans."""
        filemap = filemap_merge.parse_filemap(text)
        digest = hashlib.sha256()
        for member in filemap.members:
            if member.key != source:
                digest.update(text[member.key_start:member.value_end].encode("utf-8"))
                digest.update(b"\x00")
        return digest.hexdigest(), len(filemap.members)

    def assert_failed_without_changes(self, before, exit_code, stdout):
        self.assertNotEqual(0, exit_code)
        self.assertEqual(before, snapshot(self.root))
        return self.envelope(stdout)

    def read_text(self):
        return self.filemap_path.read_bytes().decode("utf-8")


class SingleEntryMergeTests(OnlyFileTestCase):
    def test_fixture_has_realistic_size(self):
        text = self.write_filemap()
        self.assertGreaterEqual(len(json.loads(text)["files"]), 2000)

    def test_creates_exactly_one_entry_and_keeps_every_other_byte(self):
        before = self.write_filemap()
        before_hash, before_count = self.foreign_hash(before)
        before_map = filemap_merge.parse_filemap(before)

        exit_code, stdout, stderr = self.only_file()

        result = self.envelope(stdout)
        self.assertEqual(0, exit_code, stderr)
        self.assertTrue(result["success"])
        self.assertEqual("Completed", result["state"])
        only = result["data"]["only_file"]
        self.assertEqual(SOURCE, only["source"])
        self.assertEqual("created", only["action"])
        self.assertTrue(only["entry_changed"])
        self.assertFalse(only["duplicate_source"])
        self.assertIn("Einzeldatei-Modus", stderr)
        self.assertNotIn("Einzeldatei-Modus", stdout)

        after = self.read_text()
        after_hash, after_count = self.foreign_hash(after)
        self.assertEqual(before_hash, after_hash)
        self.assertEqual(before_count + 1, after_count)
        after_map = filemap_merge.parse_filemap(after)
        # Container header and tail (including updated_at) are untouched bytes.
        self.assertEqual(before[:before_map.files_open + 1], after[:after_map.files_open + 1])
        self.assertTrue(after.endswith(before[before_map.files_close:]))
        self.assertEqual("2026-01-01 00:00:00", json.loads(after)["updated_at"])
        # Sorted insertion keeps the generator order.
        keys = [member.key for member in after_map.members]
        self.assertEqual(sorted(keys), keys)

        entry = json.loads(after)["files"][SOURCE]
        self.assertEqual("converted", entry["conversion_status"])
        self.assertEqual(MIRROR, entry["markdown_mirror"])
        self.assertEqual(DERIVATIVE, entry["derivative"]["path"])
        self.assertEqual(MODULE.calculate_sha256(str(self.source_path)), entry["sha256"])
        self.assertEqual(entry["sha256"], entry["artifact_metadata"]["source_sha256"])
        self.assertTrue((self.root / MIRROR).is_file())
        self.assertEqual(b"MOCK_DOCX_DERIVATIVE_CONTENT", (self.root / DERIVATIVE).read_bytes())
        self.assertEqual(1, result["data"]["storages"][0]["counts"]["converted"])
        self.assertEqual(ENTRY_COUNT + 1, result["data"]["storages"][0]["file_count"])

    def test_second_run_is_idempotent_for_entry_artifacts_and_filemap(self):
        self.write_filemap()
        self.assertEqual(0, self.only_file()[0])
        first = snapshot(self.root)
        filemap_mtime = self.filemap_path.stat().st_mtime_ns
        mirror_mtime = (self.root / MIRROR).stat().st_mtime_ns

        exit_code, stdout, stderr = self.only_file()

        self.assertEqual(0, exit_code, stderr)
        only = self.envelope(stdout)["data"]["only_file"]
        self.assertEqual("unchanged", only["action"])
        self.assertFalse(only["entry_changed"])
        self.assertFalse(only["artifacts_changed"])
        self.assertEqual(first, snapshot(self.root))
        self.assertEqual(filemap_mtime, self.filemap_path.stat().st_mtime_ns)
        self.assertEqual(mirror_mtime, (self.root / MIRROR).stat().st_mtime_ns)

    def test_changed_source_updates_only_that_entry_in_place(self):
        self.write_filemap()
        self.assertEqual(0, self.only_file()[0])
        middle = self.read_text()
        middle_hash, count = self.foreign_hash(middle)
        positions = [m.key for m in filemap_merge.parse_filemap(middle).members]
        self.source_path.write_bytes(b"\xd0\xcf\x11\xe0" + b"CHANGED_PAYLOAD")

        exit_code, stdout, stderr = self.only_file()

        self.assertEqual(0, exit_code, stderr)
        self.assertEqual("updated", self.envelope(stdout)["data"]["only_file"]["action"])
        after = self.read_text()
        self.assertEqual((middle_hash, count), self.foreign_hash(after))
        self.assertEqual(positions, [m.key for m in filemap_merge.parse_filemap(after).members])
        self.assertEqual(MODULE.calculate_sha256(str(self.source_path)), json.loads(after)["files"][SOURCE]["sha256"])

    def test_force_rewrites_only_the_one_source_and_keeps_curated_fields(self):
        self.write_filemap()
        self.assertEqual(0, self.only_file()[0])
        data = json.loads(self.read_text())
        data["files"][SOURCE]["description"] = "Handgepflegt"
        data["files"][SOURCE]["owner_note"] = "bleibt"
        middle = self.read_text()
        filemap = filemap_merge.parse_filemap(middle)
        member = next(m for m in filemap.members if m.key == SOURCE)
        patched = middle[:member.value_start] + json.dumps(data["files"][SOURCE], indent=2).replace("\n", "\n    ") + middle[member.value_end:]
        self.write_filemap(patched)
        before_hash = self.foreign_hash(patched)

        exit_code, _stdout, stderr = self.only_file(SOURCE, "--force")

        self.assertEqual(0, exit_code, stderr)
        after = self.read_text()
        self.assertEqual(before_hash, self.foreign_hash(after))
        entry = json.loads(after)["files"][SOURCE]
        self.assertEqual("Handgepflegt", entry["description"])
        self.assertEqual("bleibt", entry["owner_note"])

    def test_foreign_files_and_unlisted_sources_are_never_touched(self):
        self.write_filemap()
        (self.cloud / "decoy_unlisted.doc").write_bytes(b"\xd0\xcf\x11\xe0DECOY")
        (self.cloud / "sub").mkdir()
        (self.cloud / "sub" / "other.pdf").write_bytes(b"%PDF-1.4 other")
        (self.output / "orphan.md").write_text("orphan mirror\n", encoding="utf-8")
        (self.output / "_derivatives").mkdir()
        (self.output / "_derivatives" / "orphan.docx").write_bytes(b"orphan derivative")
        (self.output / "empty_dir").mkdir()
        (self.root / "memory" / "notes.md").write_text("fremd\n", encoding="utf-8")
        before = snapshot(self.root)
        directories_before = sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_dir())

        with mock.patch.object(MODULE, "walk_scoped", side_effect=AssertionError("scan")), \
             mock.patch.object(MODULE, "retained_artifact_paths", side_effect=AssertionError("scan")), \
             mock.patch.object(MODULE.os, "walk", side_effect=AssertionError("walk")):
            exit_code, stdout, stderr = self.only_file()

        self.assertEqual(0, exit_code, stderr)
        after = snapshot(self.root)
        changed = {path for path in set(before) | set(after) if before.get(path) != after.get(path)}
        self.assertEqual({FILEMAP, MIRROR, DERIVATIVE}, changed)
        self.assertNotIn("decoy_unlisted", self.read_text())
        directories_after = sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_dir())
        self.assertEqual(directories_before, directories_after)
        self.envelope(stdout)

    def test_pdf_source_uses_the_non_derivative_path(self):
        self.write_filemap()
        pdf = self.cloud / "form.pdf"
        shutil.copyfile(FIXTURE_PDF, pdf)
        before_hash = self.foreign_hash(self.read_text(), "data/cloud/DEMO/form.pdf")

        exit_code, stdout, stderr = self.only_file("data/cloud/DEMO/form.pdf", "--ocr-policy", "disabled")

        self.assertEqual(0, exit_code, stderr)
        after = self.read_text()
        self.assertEqual(before_hash[0], self.foreign_hash(after, "data/cloud/DEMO/form.pdf")[0])
        entry = json.loads(after)["files"]["data/cloud/DEMO/form.pdf"]
        self.assertEqual("memory/cloud/topics/demo/form.md", entry["markdown_mirror"])
        self.assertNotIn("derivative", entry)
        self.assertEqual(MODULE.calculate_sha256(str(pdf)), entry["sha256"])
        self.assertEqual("created", self.envelope(stdout)["data"]["only_file"]["action"])


class ForeignFormattingTests(OnlyFileTestCase):
    def assert_merge_preserves(self, text, expect_in_text=None):
        self.write_filemap(text)
        before_hash = self.foreign_hash(text)
        exit_code, _stdout, stderr = self.only_file()
        self.assertEqual(0, exit_code, stderr)
        after = self.read_text()
        self.assertEqual(before_hash[0], self.foreign_hash(after)[0])
        self.assertIn(SOURCE, json.loads(after)["files"])
        if expect_in_text:
            self.assertIn(expect_in_text, after)
        return after

    def container(self, files_text, newline="\n", indent="  "):
        return (
            f'{{{newline}{indent}"$schema": "{filemap_merge.FILEMAP_SCHEMA_URI}",{newline}'
            f'{indent}"schema_version": 1,{newline}{indent}"kind": "cloud-filemap",{newline}'
            f'{indent}"scope": "topic",{newline}{indent}"storage_id": "default",{newline}'
            f'{indent}"project": "demo",{newline}{indent}"project_title": "Demo",{newline}'
            f'{indent}"scan_dir": "data/cloud/DEMO",{newline}{indent}"output_dir": "memory/cloud/topics/demo",{newline}'
            f'{indent}"updated_at": "2026-01-01 00:00:00",{newline}{indent}"files": {files_text}{newline}}}'
        )

    def test_crlf_and_tab_indentation_is_continued(self):
        entry = '{"version": "N/A", "mtime": "2026-01-01 00:00:00", "size": "1.0 KB", "sha256": "%s", "description": "x", "markdown_mirror": "memory/cloud/topics/demo/a.md", "conversion_status": "converted"}' % ("1" * 64)
        files = '{\r\n\t\t"data/cloud/DEMO/a.pdf": ' + entry + '\r\n\t}'
        after = self.assert_merge_preserves(self.container(files, "\r\n", "\t"))
        self.assertIn('\r\n\t\t"data/cloud/DEMO/g_neu.doc": {\r\n', after)
        self.assertNotIn("\n", after.replace("\r\n", ""))

    def test_empty_files_object_with_and_without_indentation(self):
        pretty = self.assert_merge_preserves(self.container("{}"))
        self.assertEqual(1, len(json.loads(pretty)["files"]))
        self.filemap_path.unlink()
        shutil.rmtree(self.output)
        self.output.mkdir(parents=True)
        flat = self.assert_merge_preserves(self.container("{ }", indent=""), None)
        self.assertEqual(1, len(json.loads(flat)["files"]))

    def test_unsorted_map_appends_without_reordering(self):
        entry = '{"version": "N/A", "mtime": "2026-01-01 00:00:00", "size": "1.0 KB", "sha256": "%s", "description": "x"}'
        files = '{"data/cloud/DEMO/z.pdf": %s, "data/cloud/DEMO/b.pdf": %s}' % (entry % ("1" * 64), entry % ("2" * 64))
        after = self.assert_merge_preserves(self.container(files), None)
        self.assertEqual(["data/cloud/DEMO/z.pdf", "data/cloud/DEMO/b.pdf", SOURCE], list(json.loads(after)["files"]))

    def test_legacy_converter_map_without_container_keys_is_accepted(self):
        text = json.dumps({"project": "demo", "project_title": "Demo", "files": {}, "updated_at": "2026-01-01 00:00:00"}, indent=2)
        after = self.assert_merge_preserves(text)
        self.assertEqual("2026-01-01 00:00:00", json.loads(after)["updated_at"])


class FailClosedPathTests(OnlyFileTestCase):
    def setUp(self):
        super().setUp()
        self.write_filemap()
        self.before = snapshot(self.root)

    def assert_rejected(self, source, phase=None, *extra, patches=()):
        exit_code, stdout, _stderr = self.only_file(source, *extra, patches=patches)
        result = self.assert_failed_without_changes(self.before, exit_code, stdout)
        self.assertFalse(result["success"])
        self.assertEqual("Failed", result["state"])
        if phase:
            self.assertEqual(phase, result["error"]["phase"])
        self.assertFalse(result["data"]["only_file"]["entry_changed"])
        return result

    def test_path_outside_cloud_dir(self):
        other = self.root / "data" / "cloud" / "OTHER"
        other.mkdir()
        (other / "x.doc").write_bytes(b"\xd0\xcf\x11\xe0x")
        self.before = snapshot(self.root)
        self.assert_rejected("data/cloud/OTHER/x.doc", "only-file")

    def test_prefix_sibling_of_cloud_dir_is_outside(self):
        sibling = self.root / "data" / "cloud" / "DEMO2"
        sibling.mkdir()
        (sibling / "x.doc").write_bytes(b"\xd0\xcf\x11\xe0x")
        self.before = snapshot(self.root)
        self.assert_rejected("data/cloud/DEMO2/x.doc", "only-file")

    def test_explicit_cloud_dir_override_must_contain_the_file(self):
        exit_code, stdout, _ = self.run_main(
            "--topic-id", "demo", "--workspace-root", str(self.root),
            "--cloud-dir", "data/cloud/DEMO/sub", "--only-file", SOURCE, "--json",
        )
        self.assert_failed_without_changes(self.before, exit_code, stdout)

    def test_parent_traversal(self):
        other = self.root / "data" / "cloud" / "OTHER"
        other.mkdir()
        (other / "x.doc").write_bytes(b"\xd0\xcf\x11\xe0x")
        self.before = snapshot(self.root)
        self.assert_rejected("data/cloud/DEMO/../OTHER/x.doc", "only-file")
        self.assert_rejected("data\\cloud\\DEMO\\..\\OTHER\\x.doc", "only-file")

    def test_absolute_and_drive_paths(self):
        self.assert_rejected(str(self.source_path), "only-file")
        self.assert_rejected("/data/cloud/DEMO/g_neu.doc", "only-file")
        self.assert_rejected("C:data/cloud/DEMO/g_neu.doc", "only-file")
        self.assert_rejected("data/cloud/DEMO/g_neu.doc:stream", "only-file")

    def test_missing_file_and_directory(self):
        self.assert_rejected("data/cloud/DEMO/missing.doc", "only-file")
        (self.cloud / "folder.doc").mkdir()
        self.before = snapshot(self.root)
        self.assert_rejected("data/cloud/DEMO/folder.doc", "only-file")

    def test_unsupported_and_hidden_names(self):
        (self.cloud / "note.txt").write_text("x", encoding="utf-8")
        (self.cloud / "~$lock.doc").write_bytes(b"x")
        self.before = snapshot(self.root)
        self.assert_rejected("data/cloud/DEMO/note.txt", "only-file")
        self.assert_rejected("data/cloud/DEMO/~$lock.doc", "only-file")

    def test_symlink_escape_is_rejected(self):
        outside = Path(tempfile.mkdtemp(prefix="cloud-atlas-outside-"))
        self.addCleanup(shutil.rmtree, outside, True)
        (outside / "secret.doc").write_bytes(b"\xd0\xcf\x11\xe0SECRET")
        link = self.cloud / "link.doc"
        try:
            os.symlink(outside / "secret.doc", link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available")
        directory_link = self.cloud / "linked_dir"
        try:
            os.symlink(outside, directory_link, target_is_directory=True)
        except (OSError, NotImplementedError):
            directory_link = None
        self.before = snapshot(self.root)
        self.assert_rejected("data/cloud/DEMO/link.doc", "only-file")
        if directory_link is not None:
            self.assert_rejected("data/cloud/DEMO/linked_dir/secret.doc", "only-file")

    @unittest.skipUnless(os.name == "nt", "directory junctions are Windows-specific")
    def test_junction_escape_is_rejected(self):
        import subprocess

        outside = Path(tempfile.mkdtemp(prefix="cloud-atlas-outside-"))
        self.addCleanup(shutil.rmtree, outside, True)
        (outside / "secret.doc").write_bytes(b"\xd0\xcf\x11\xe0SECRET")
        junction = self.cloud / "junction"
        completed = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(outside)], capture_output=True)
        if completed.returncode != 0:
            self.skipTest("cannot create a junction")
        self.before = snapshot(self.root)
        self.assert_rejected("data/cloud/DEMO/junction/secret.doc", "only-file")
        self.assertTrue((outside / "secret.doc").exists())

    def test_simulated_link_redirect_is_rejected(self):
        real_realpath = os.path.realpath

        def redirect(path, *args, **kwargs):
            if os.path.basename(str(path)) == "g_neu.doc":
                return real_realpath(self.root / "elsewhere" / "g_neu.doc")
            return real_realpath(path, *args, **kwargs)

        self.assert_rejected(SOURCE, "only-file", patches=[mock.patch.object(MODULE.os.path, "realpath", redirect)])

    def test_wrong_path_casing_is_rejected(self):
        # On case-insensitive disks the file exists; the key must still match it.
        exit_code, stdout, _ = self.only_file("data/cloud/DEMO/G_NEU.doc")
        self.assertNotEqual(0, exit_code)
        self.assertEqual(self.before, snapshot(self.root))
        self.envelope(stdout)

    def test_linked_output_dir_is_rejected(self):
        outside = Path(tempfile.mkdtemp(prefix="cloud-atlas-outside-"))
        self.addCleanup(shutil.rmtree, outside, True)
        derivative_link = self.output / "_derivatives"
        try:
            os.symlink(outside, derivative_link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available")
        self.before = snapshot(self.root)
        outside_before = snapshot(outside)
        self.assert_rejected(SOURCE, "mirror-plan")
        self.assertEqual(outside_before, snapshot(outside))


class FailClosedFilemapTests(OnlyFileTestCase):
    def reject(self, text=None, expected_phase="filemap", delete=False):
        if delete:
            if self.filemap_path.exists():
                self.filemap_path.unlink()
        elif text is not None:
            self.filemap_path.write_bytes(text.encode("utf-8"))
        before = snapshot(self.root)
        exit_code, stdout, _stderr = self.only_file()
        result = self.assert_failed_without_changes(before, exit_code, stdout)
        self.assertEqual(expected_phase, result["error"]["phase"])
        return result

    def test_missing_filemap_is_not_created(self):
        self.reject(delete=True, expected_phase="only-file")
        self.assertFalse(self.filemap_path.exists())

    def test_invalid_json_and_wrong_container(self):
        good = build_filemap_text(5)
        self.reject("{ not json")
        self.reject("﻿" + good)
        self.reject(good.replace('"kind": "cloud-filemap"', '"kind": "other"'))
        self.reject(good.replace('"scope": "topic"', '"scope": "galaxy"'))
        self.reject(good.replace('"scan_dir": "data/cloud/DEMO"', '"scan_dir": "data/cloud/ELSE"'))
        self.reject(good.replace('"files": {', '"files": [], "x": {'))
        self.reject(json.dumps([1, 2]))
        self.reject('{"project": "demo"}')
        self.reject(good.replace('"updated_at": "2026-01-01 00:00:00",', '"updated_at": "yesterday",'))

    def test_nan_and_trailing_garbage(self):
        good = build_filemap_text(5)
        self.reject(good.replace('"schema_version": 1', '"schema_version": NaN'))
        self.reject(good + "{}")

    def test_entry_schema_violations(self):
        good = build_filemap_text(5)
        self.reject(good.replace(f'"sha256": "{2:064x}"', '"sha256": "zz"'))
        self.reject(good.replace('"size": "3.0 KB"', '"size": "huge"', 1))
        self.reject(good.replace('"conversion_status": "converted"', '"conversion_status": "maybe"', 1))
        self.reject(good.replace('"description": "Eintrag 1 Fühstück",', "", 1))
        self.reject(good.replace('"markdown_mirror": "memory/cloud/topics/demo/f0001.md"', '"markdown_mirror": "elsewhere/f0001.md"'))
        self.reject(good.replace("data/cloud/DEMO/f0002.pdf", "data/cloud/OTHER/f0002.pdf"))

    def test_duplicate_mirror_between_foreign_entries(self):
        good = build_filemap_text(5)
        self.reject(good.replace("memory/cloud/topics/demo/f0002.md", "memory/cloud/topics/demo/F0001.md"))

    def test_duplicate_json_key_for_the_target_source(self):
        good = build_filemap_text(5)
        entry = foreign_entry_text(1).strip().replace("f0001.pdf", "g_neu.doc")
        key_line = entry
        text = good.replace('  }\n}\n', f',\n    {key_line},\n    {key_line}\n  }}\n}}\n')
        result = self.reject(text)
        self.assertTrue(result["data"]["only_file"]["duplicate_source"])

    def test_duplicate_json_key_elsewhere_is_rejected_too(self):
        good = build_filemap_text(5)
        text = good.replace('"kind": "cloud-filemap",', '"kind": "cloud-filemap", "kind": "cloud-filemap",')
        result = self.reject(text)
        self.assertFalse(result["data"]["only_file"]["duplicate_source"])

    def test_case_variant_source_is_a_duplicate(self):
        good = build_filemap_text(5)
        entry = foreign_entry_text(1).strip().replace("f0001.pdf", "G_NEU.doc").replace("f0001.md", "g_other.md")
        text = good.replace('  }\n}\n', f',\n    {entry}\n  }}\n}}\n')
        result = self.reject(text, expected_phase="only-file")
        self.assertTrue(result["data"]["only_file"]["duplicate_source"])

    def test_two_case_variants_of_the_source_are_a_duplicate(self):
        good = build_filemap_text(5)
        first = foreign_entry_text(1).strip().replace("f0001.pdf", "G_neu.doc").replace("f0001.md", "g_one.md")
        second = foreign_entry_text(1).strip().replace("f0001.pdf", "g_NEU.doc").replace("f0001.md", "g_two.md")
        text = good.replace('  }\n}\n', f',\n    {first},\n    {second}\n  }}\n}}\n')
        result = self.reject(text, expected_phase="only-file")
        self.assertTrue(result["data"]["only_file"]["duplicate_source"])

    def test_foreign_entry_claiming_the_target_mirror(self):
        good = build_filemap_text(5)
        self.reject(good.replace("memory/cloud/topics/demo/f0003.md", MIRROR), expected_phase="mirror-plan")
        self.reject(
            good.replace('"markdown_mirror": "memory/cloud/topics/demo/f0003.md"', '"markdown_mirror": "memory/cloud/topics/demo/f0003.md", "derivative": {"path": "%s", "sha256": "%s", "format": "docx"}' % (DERIVATIVE, "b" * 64)),
            expected_phase="mirror-plan",
        )

    def test_unowned_existing_mirror_is_not_overwritten(self):
        self.write_filemap(build_filemap_text(5))
        (self.output / "g_neu.md").write_text("fremde Datei\n", encoding="utf-8")
        before = snapshot(self.root)
        exit_code, stdout, _ = self.only_file()
        result = self.assert_failed_without_changes(before, exit_code, stdout)
        self.assertEqual("mirror-plan", result["error"]["phase"])

    def test_stem_collision_group_gets_original_name_mirror(self):
        good = build_filemap_text(5)
        self.write_filemap(good)
        (self.cloud / "g_neu.docx").write_bytes(b"PK")
        entry = foreign_entry_text(1).strip().replace("f0001.pdf", "g_neu.docx").replace("f0001.md", "g_neu.md")
        self.write_filemap(good.replace('  }\n}\n', f',\n    {entry}\n  }}\n}}\n'))
        before_hash = self.foreign_hash(self.read_text())

        exit_code, _stdout, stderr = self.only_file()

        self.assertEqual(0, exit_code, stderr)
        after = self.read_text()
        self.assertEqual(before_hash[0], self.foreign_hash(after)[0])
        self.assertEqual("memory/cloud/topics/demo/g_neu.doc.md", json.loads(after)["files"][SOURCE]["markdown_mirror"])
        self.assertEqual("memory/cloud/topics/demo/g_neu.md", json.loads(after)["files"]["data/cloud/DEMO/g_neu.docx"]["markdown_mirror"])

    def test_protected_filemap_name_is_never_a_mirror(self):
        self.write_filemap(build_filemap_text(5))
        (self.cloud / "filemap.doc").write_bytes(b"\xd0\xcf\x11\xe0x")
        before = snapshot(self.root)
        exit_code, stdout, _ = self.only_file("data/cloud/DEMO/filemap.doc")
        result = self.assert_failed_without_changes(before, exit_code, stdout)
        self.assertEqual("mirror-plan", result["error"]["phase"])

    def test_ambiguous_storage_requires_storage_id(self):
        topics = self.root / "memory" / "references" / "topics" / "topics.json"
        data = json.loads(topics.read_text(encoding="utf-8"))
        data[0]["cloud_sync"]["second"] = dict(data[0]["cloud_sync"]["default"], output_json="memory/cloud/topics/demo/filemap-second.json", output_dir="memory/cloud/topics/demo/second")
        topics.write_text(json.dumps(data), encoding="utf-8")
        self.write_filemap(build_filemap_text(5))
        before = snapshot(self.root)
        exit_code, stdout, _ = self.only_file()
        result = self.assert_failed_without_changes(before, exit_code, stdout)
        self.assertEqual("only-file", result["error"]["phase"])
        exit_code, _stdout, stderr = self.only_file(SOURCE, "--storage-id", "default")
        self.assertEqual(0, exit_code, stderr)


class ArgumentContractTests(OnlyFileTestCase):
    def test_extensions_are_rejected_with_a_clear_json_error(self):
        self.write_filemap(build_filemap_text(5))
        before = snapshot(self.root)
        exit_code, stdout, stderr = self.only_file(SOURCE, "--extensions", ".doc")
        self.assertEqual(2, exit_code)
        result = self.envelope(stdout)
        self.assertEqual("arguments", result["error"]["phase"])
        self.assertIn("--only-file cannot be combined with --extensions", stderr)
        self.assertEqual(before, snapshot(self.root))

    def test_empty_value_is_rejected(self):
        exit_code, _stdout, stderr = self.only_file("  ")
        self.assertEqual(2, exit_code)
        self.assertIn("--only-file requires", stderr)

    def test_default_extensions_are_unchanged_for_full_scans(self):
        args = MODULE.parse_args(["--project-id", "x"])
        self.assertEqual(MODULE.DEFAULT_EXTENSIONS, args.extensions)
        self.assertIsNone(args.only_file)
        args = MODULE.parse_args(["--project-id", "x", "--extensions", ".pdf"])
        self.assertEqual(".pdf", args.extensions)

    def test_subtopic_direct_path_overrides_stay_forbidden(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            MODULE.parse_args(["--topic-id", "t", "--subtopic-id", "s", "--only-file", "a.doc", "--cloud-dir", "x"])


class RollbackTests(OnlyFileTestCase):
    def setUp(self):
        super().setUp()
        self.write_filemap()
        self.before = snapshot(self.root)

    @staticmethod
    def failing_filemap_replace():
        real_replace = os.replace

        def replace(source, destination, *args, **kwargs):
            if Path(str(destination)).name == "filemap.json":
                raise OSError("simulated replace failure")
            return real_replace(source, destination, *args, **kwargs)

        return mock.patch("os.replace", replace)

    def test_filemap_replace_failure_removes_new_artifacts_and_leaves_filemap(self):
        exit_code, stdout, stderr = self.only_file(patches=[self.failing_filemap_replace()])

        result = self.assert_failed_without_changes(self.before, exit_code, stdout)
        self.assertEqual("runtime", result["error"]["phase"])
        self.assertIn("simulated replace failure", result["error"]["message"])
        self.assertTrue(result["data"]["only_file"]["rolled_back"])
        self.assertFalse((self.root / MIRROR).exists())
        self.assertFalse((self.output / "_derivatives").exists())
        self.assertEqual([], [p.name for p in self.root.rglob("*.tmp")])

    def test_filemap_replace_failure_restores_previous_artifacts_on_update(self):
        self.assertEqual(0, self.only_file()[0])
        committed = snapshot(self.root)
        self.source_path.write_bytes(b"\xd0\xcf\x11\xe0" + b"SECOND_REVISION")
        committed[SOURCE] = sha256_bytes(self.source_path.read_bytes())

        exit_code, stdout, _stderr = self.only_file(SOURCE, "--force", patches=[self.failing_filemap_replace()])

        self.assertNotEqual(0, exit_code)
        self.assertEqual(committed, snapshot(self.root))
        self.assertTrue(self.envelope(stdout)["data"]["only_file"]["rolled_back"])

    def test_failure_after_mirror_write_rolls_back_mirror_and_derivative(self):
        with mock.patch.object(MODULE, "verify_splice", side_effect=filemap_merge.FilemapError("splice broke")):
            exit_code, stdout, _ = self.only_file()
        result = self.assert_failed_without_changes(self.before, exit_code, stdout)
        self.assertEqual("filemap", result["error"]["phase"])

    def test_mirror_write_failure_rolls_back_derivative(self):
        with mock.patch.object(MODULE, "write_markdown_file", side_effect=OSError("disk full")):
            exit_code, stdout, _ = self.only_file()
        self.assert_failed_without_changes(self.before, exit_code, stdout)

    def test_timeout_leaves_no_partial_artifacts(self):
        HangingProcess.partial_paths = [self.root / DERIVATIVE, self.output / "_derivatives" / ".ocr-stage-x.pdf"]
        started = time.time()
        exit_code, stdout, stderr = self.only_file(
            SOURCE, "--file-timeout", "1",
            patches=[mock.patch.object(MODULE.multiprocessing, "Process", HangingProcess)],
        )
        self.assertLess(time.time() - started, 60)
        result = self.assert_failed_without_changes(self.before, exit_code, stdout)
        self.assertEqual("ConversionRequired", result["state"], json.dumps(result["error"]))
        self.assertIn("Timeout nach 1s", json.dumps(result))
        self.assertTrue(result["data"]["only_file"]["rolled_back"])
        self.assertFalse((self.output / "_derivatives").exists())

    def test_timeout_for_a_non_doc_source_is_a_failure(self):
        pdf = self.cloud / "slow.pdf"
        shutil.copyfile(FIXTURE_PDF, pdf)
        self.before = snapshot(self.root)
        HangingProcess.partial_paths = []
        exit_code, stdout, _ = self.only_file(
            "data/cloud/DEMO/slow.pdf", "--file-timeout", "1", "--ocr-policy", "disabled",
            patches=[mock.patch.object(MODULE.multiprocessing, "Process", HangingProcess)],
        )
        result = self.assert_failed_without_changes(self.before, exit_code, stdout)
        self.assertEqual("conversion", result["error"]["phase"])

    def test_conversion_error_does_not_touch_anything(self):
        pdf_source = "data/cloud/DEMO/failing.pdf"
        shutil.copyfile(FIXTURE_PDF, self.root / pdf_source)
        self.before = snapshot(self.root)
        failure = {pdf_source: {"success": False, "markdown_body": None, "ocr_applied": False, "error": "boom"}}

        def failing_scheduler(tasks, **_kwargs):
            # A worker that already left an artifact behind before failing.
            leftover = Path(tasks[0]["dest_abs"])
            leftover.write_bytes(b"HALF")
            return failure

        exit_code, stdout, _ = self.only_file(
            pdf_source, "--ocr-policy", "disabled",
            patches=[mock.patch.object(MODULE, "run_conversion_tasks", failing_scheduler)],
        )
        result = self.assert_failed_without_changes(self.before, exit_code, stdout)
        self.assertEqual("conversion", result["error"]["phase"])

    def test_missing_doc_converter_is_reported_without_writing(self):
        os.environ["CLOUD_ATLAS_DOC_CONVERTER_MOCK"] = "none"
        exit_code, stdout, _ = self.only_file()
        result = self.assert_failed_without_changes(self.before, exit_code, stdout)
        self.assertEqual("ConversionRequired", result["state"])
        self.assertEqual("doc_converter", result["error"]["requirements"][0]["capability"])

    def test_enrich_source_mutation_is_rolled_back_with_the_filemap_failure(self):
        os.environ["CLOUD_ATLAS_OCR_MOCK"] = "1"
        os.environ["CLOUD_ATLAS_MOCK_IS_SCAN"] = "1"
        pdf = self.cloud / "scan.pdf"
        shutil.copyfile(FIXTURE_PDF, pdf)
        self.before = snapshot(self.root)
        original = pdf.read_bytes()

        exit_code, stdout, _ = self.only_file(
            "data/cloud/DEMO/scan.pdf", "--ocr-policy", "enrich_source",
            patches=[self.failing_filemap_replace()],
        )

        self.assertNotEqual(0, exit_code)
        self.assertEqual(original, pdf.read_bytes())
        self.assertEqual(self.before, snapshot(self.root))
        self.assertTrue(self.envelope(stdout)["data"]["only_file"]["rolled_back"])

    def test_enrich_source_success_records_the_new_source_hash(self):
        os.environ["CLOUD_ATLAS_OCR_MOCK"] = "1"
        os.environ["CLOUD_ATLAS_MOCK_IS_SCAN"] = "1"
        pdf = self.cloud / "scan.pdf"
        shutil.copyfile(FIXTURE_PDF, pdf)
        original = pdf.read_bytes()

        exit_code, _stdout, stderr = self.only_file("data/cloud/DEMO/scan.pdf", "--ocr-policy", "enrich_source")

        self.assertEqual(0, exit_code, stderr)
        self.assertNotEqual(original, pdf.read_bytes())
        entry = json.loads(self.read_text())["files"]["data/cloud/DEMO/scan.pdf"]
        self.assertEqual(MODULE.calculate_sha256(str(pdf)), entry["sha256"])
        self.assertTrue(entry["ocr_applied"])


class ModuleLevelTests(unittest.TestCase):
    def test_transaction_restores_files_and_removes_created_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            existing = root / "keep" / "a.md"
            existing.parent.mkdir()
            existing.write_bytes(b"old")
            new_directory = root / "created" / "deep"
            transaction = MODULE.ArtifactTransaction()
            transaction.track_directory(str(existing.parent), str(root))
            transaction.track_directory(str(new_directory), str(root))
            transaction.track_file(str(existing))
            transaction.track_file(str(new_directory / "b.md"))
            existing.write_bytes(b"new")
            (existing.parent / "stray.tmp").write_bytes(b"x")
            new_directory.mkdir(parents=True)
            (new_directory / "b.md").write_bytes(b"b")

            self.assertEqual([], transaction.rollback())

            self.assertEqual(b"old", existing.read_bytes())
            self.assertEqual(["a.md"], sorted(p.name for p in existing.parent.iterdir()))
            self.assertFalse((root / "created").exists())

    def test_schema_uri_matches_the_generator(self):
        import gen_filemap
        self.assertEqual(gen_filemap.FILEMAP_SCHEMA_URI, filemap_merge.FILEMAP_SCHEMA_URI)


if __name__ == "__main__":
    unittest.main()
