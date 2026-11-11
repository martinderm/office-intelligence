"""Single-file mode (``--only-file``, Issue #22) with versioned publish targets.

The fixture is a synthetic topic filemap in realistic size (2,000+ entries) with
deliberately irregular foreign formatting.  A conversion writes its artifacts into
a fresh directory below ``<output_dir>/_versions/``; the filemap replace is the
only commit point.  Failure tests compare a hash snapshot of the whole workspace.
"""

import contextlib
import hashlib
import io
import json
import multiprocessing
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
FIXTURE_PDF = Path(__file__).resolve().parent / "fixtures" / "tagged_ocrmypdf_small_text.pdf"
sys.path.insert(0, str(SCRIPTS_DIR))
import convert_cloud_docs as MODULE
from core import filemap_merge
from core.publish_journal import JournalConflict, journal_key

ENTRY_COUNT = 2100
SOURCE = "data/cloud/DEMO/g_neu.doc"
FILEMAP = "memory/cloud/topics/demo/filemap.json"
VERSION_ROOT = "memory/cloud/topics/demo/_versions"
SHA = "a" * 64
DOC_MIRROR_PLACEHOLDER = "[Inhalt aus .docx-Derivat extrahiert]"
DOC_FIXTURE_LINES = ("Synthetischer Test", "Zweite Zeile")


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


LOCK_AREA = "data/.locks"


def snapshot(root):
    """Map every workspace file (hash) and directory (marker) before/after a run.

    The persistent coordination locks under ``data/.locks`` are created once and
    reused; they are not data artifacts and are ignored here.
    """
    result = {}
    for directory, dirs, files in os.walk(root):
        rel_dir = Path(directory).relative_to(root).as_posix()
        if rel_dir == LOCK_AREA or rel_dir.startswith(LOCK_AREA + "/"):
            continue
        for name in dirs:
            rel = (Path(directory) / name).relative_to(root).as_posix()
            if rel == LOCK_AREA or rel.startswith(LOCK_AREA + "/"):
                continue
            result["dir:" + rel] = "dir"
        for name in files:
            path = Path(directory) / name
            rel = path.relative_to(root).as_posix()
            if rel.startswith(LOCK_AREA + "/"):
                continue
            result[rel] = sha256_bytes(path.read_bytes())
    return result


def write_minimal_docx(path, text):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        archive.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        archive.writestr("word/document.xml", f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>')


def foreign_entry_text(index, newline="\n"):
    key = f"data/cloud/DEMO/f{index:04d}.pdf" if index < ENTRY_COUNT - 100 else f"data/cloud/DEMO/sub/s{index:04d}.pdf"
    mirror = f"memory/cloud/topics/demo/legacy/f{index:04d}.md"
    body = {
        "version": "N/A", "mtime": "2026-01-01 00:00:00", "size": f"{index % 900 + 1}.0 KB",
        "sha256": f"{index:064x}", "description": f"Eintrag {index} Fühstück",
        "markdown_mirror": mirror, "conversion_status": "converted",
    }
    if index % 113 == 0:
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
    time.sleep(seconds)
    connection.close()


class HangingProcess:
    """Stand-in worker: writes a partial file into the version staging dir, then hangs."""

    stage_root = None
    partial_paths = []

    def __init__(self, target=None, args=(), kwargs=None):
        self._process = multiprocessing.get_context("spawn").Process(target=hang_forever, args=(120, args[1]))

    def start(self):
        partial = []
        if self.stage_root:
            stages = [p for p in Path(self.stage_root).rglob("*") if p.is_dir() and not p.is_symlink()]
            if stages:
                partial = [stages[0] / "partial.stage"]
        for path in partial or self.partial_paths:
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
            for name in (
                "CLOUD_ATLAS_DOC_CONVERTER_MOCK", "CLOUD_ATLAS_OCR_MOCK",
                "CLOUD_ATLAS_MOCK_IS_SCAN", "CLOUD_ATLAS_ONLY_FILE_LOCK_TIMEOUT",
                "CLOUD_ATLAS_ONLY_FILE_CRASH_AFTER",
            )
        }
        os.environ["CLOUD_ATLAS_DOC_CONVERTER_MOCK"] = "mock:test"
        for name in ("CLOUD_ATLAS_OCR_MOCK", "CLOUD_ATLAS_MOCK_IS_SCAN",
                     "CLOUD_ATLAS_ONLY_FILE_LOCK_TIMEOUT", "CLOUD_ATLAS_ONLY_FILE_CRASH_AFTER"):
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

    def hold_storage_lock(self, filemap=None, output="memory/cloud/topics/demo", timeout=2.0):
        lock = MODULE.StorageLock(
            str(self.root), str(self.root / output), str(filemap or self.filemap_path), timeout,
        )
        lock.__enter__()
        return lock

    def storage_lock_specs(self, filemap=None, output="memory/cloud/topics/demo"):
        return MODULE.only_file_storage_lock_specs(
            str(self.root), str(self.root / output), str(filemap or self.filemap_path),
        )

    def storage_lock_paths(self, filemap=None, output="memory/cloud/topics/demo"):
        return tuple(path for path, _shared in self.storage_lock_specs(filemap, output))

    NESTED_SOURCE = "data/cloud/SUB/nested.doc"
    NESTED_FILEMAP = "memory/cloud/topics/demo/sub/filemap.json"

    def configure_nested_storage(self):
        """Add a second storage whose output directory is nested under the default."""
        nested_cloud = self.root / "data" / "cloud" / "SUB"
        nested_cloud.mkdir(parents=True, exist_ok=True)
        (nested_cloud / "nested.doc").write_bytes(b"\xd0\xcf\x11\xe0" + b"NESTED_PAYLOAD")
        nested_output = self.output / "sub"
        nested_output.mkdir(parents=True, exist_ok=True)
        nested_filemap = nested_output / "filemap.json"
        nested_filemap.write_text('{"files": {}}', encoding="utf-8")
        topics = self.root / "memory" / "references" / "topics" / "topics.json"
        topics.write_text(json.dumps([{
            "id": "demo", "title": "Demo",
            "cloud_sync": {
                "default": {
                    "scan_dir": "data/cloud/DEMO",
                    "output_json": FILEMAP,
                    "output_md": "memory/cloud/topics/demo/filemap.md",
                    "output_dir": "memory/cloud/topics/demo",
                },
                "sub": {
                    "scan_dir": "data/cloud/SUB",
                    "output_json": self.NESTED_FILEMAP,
                    "output_md": "memory/cloud/topics/demo/sub/filemap.md",
                    "output_dir": "memory/cloud/topics/demo/sub",
                },
            },
        }]), encoding="utf-8")
        return nested_filemap

    def add_configured_storage(self, sid, filemap_rel, output_rel):
        """Add another explicitly configured storage (filemap is a reference source)."""
        topics = self.root / "memory" / "references" / "topics" / "topics.json"
        data = json.loads(topics.read_text(encoding="utf-8"))
        data[0]["cloud_sync"][sid] = {
            "scan_dir": f"data/cloud/{sid.upper()}",
            "output_json": filemap_rel,
            "output_md": f"{output_rel}/filemap.md",
            "output_dir": output_rel,
        }
        topics.write_text(json.dumps(data), encoding="utf-8")
        return self.root / filemap_rel

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

    def entry(self, source=SOURCE):
        return json.loads(self.read_text())["files"].get(source)

    def mirror_rel(self, source=SOURCE):
        return (self.entry(source) or {}).get("markdown_mirror")

    def mirror_path(self, source=SOURCE):
        rel = self.mirror_rel(source)
        return self.root / rel if rel else None

    def derivative_rel(self, source=SOURCE):
        return ((self.entry(source) or {}).get("derivative") or {}).get("path")

    def derivative_path(self, source=SOURCE):
        rel = self.derivative_rel(source)
        return self.root / rel if rel else None

    def version_root(self):
        return self.root / VERSION_ROOT

    def version_dirs(self):
        root = self.version_root()
        return [p for p in root.rglob("*") if p.is_dir()] if root.exists() else []

    def version_files(self):
        root = self.version_root()
        return [p for p in root.rglob("*") if p.is_file()] if root.exists() else []

    def active_version_rel(self, source=SOURCE):
        rel = self.mirror_rel(source)
        return str(Path(rel).parent.as_posix()) if rel else None

    def assert_entry_consistent(self, source=SOURCE):
        entry = self.entry(source)
        if entry is None:
            return
        mirror = self.mirror_rel(source)
        self.assertTrue(mirror and mirror.startswith(VERSION_ROOT + "/"), mirror)
        self.assertTrue((self.root / mirror).is_file(), mirror)
        derivative = self.derivative_rel(source)
        if derivative:
            self.assertTrue(derivative.startswith(VERSION_ROOT + "/"), derivative)
            self.assertTrue((self.root / derivative).is_file(), derivative)

    def prepare_synthetic_doc_topic(self, topic, source_stem="g_neu"):
        """Create a synthetic ``.doc`` cloud source plus its catalogue and filemap.

        Requires a local LibreOffice to produce the legacy ``.doc`` from RTF.
        Returns ``(source_rel, filemap_path, output_rel)``.
        """
        soffice = MODULE.find_soffice_binary()
        if soffice is None:
            self.skipTest("no local LibreOffice available")
        fixture = Path(tempfile.mkdtemp(prefix="doc-src-"))
        self.addCleanup(shutil.rmtree, fixture, True)
        rtf = fixture / "probe.rtf"
        rtf.write_bytes(
            b"{\\rtf1\\ansi\\deff0{\\fonttbl{\\f0 Arial;}}Synthetischer Test \\par Zweite Zeile.}"
        )
        # Never touch the host's default LibreOffice profile: a unique, isolated
        # profile inside the fixture keeps this helper independent of a running
        # desktop instance and of user configuration.
        profile = fixture / "lo-profile"
        profile.mkdir()
        created = subprocess.run(
            [
                soffice,
                f"-env:UserInstallation={MODULE.path_to_file_uri(str(profile))}",
                "--headless",
                "--convert-to", "doc",
                "--outdir", str(fixture),
                str(rtf),
            ],
            capture_output=True, timeout=120,
        )
        self.assertEqual(0, created.returncode, created.stderr[-1000:])
        synthetic_doc = fixture / "probe.doc"
        self.assertTrue(synthetic_doc.is_file(), "LibreOffice must produce a synthetic .doc")

        cloud_rel = f"data/cloud/{topic.upper()}"
        output_rel = f"memory/cloud/topics/{topic}"
        source_rel = f"{cloud_rel}/{source_stem}.doc"
        cloud_dir = self.root / cloud_rel
        cloud_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(synthetic_doc, cloud_dir / f"{source_stem}.doc")
        filemap = self.root / output_rel / "filemap.json"
        filemap.parent.mkdir(parents=True, exist_ok=True)
        filemap.write_text(json.dumps({
            "$schema": filemap_merge.FILEMAP_SCHEMA_URI,
            "schema_version": 1,
            "kind": "cloud-filemap",
            "scope": "topic",
            "storage_id": "default",
            "project": topic,
            "project_title": "Synthetic",
            "scan_dir": cloud_rel,
            "output_dir": output_rel,
            "exclude_dirs": [],
            "updated_at": "2026-01-01 00:00:00",
            "files": {},
        }), encoding="utf-8")
        topics = self.root / "memory" / "references" / "topics" / "topics.json"
        data = json.loads(topics.read_text(encoding="utf-8"))
        data.append({
            "id": topic, "title": "Synthetic",
            "cloud_sync": {"default": {
                "scan_dir": cloud_rel,
                "output_json": f"{output_rel}/filemap.json",
                "output_md": f"{output_rel}/filemap.md",
                "output_dir": output_rel,
            }},
        })
        topics.write_text(json.dumps(data), encoding="utf-8")
        os.environ.pop("CLOUD_ATLAS_DOC_CONVERTER_MOCK", None)
        return source_rel, filemap, output_rel

    def assert_doc_mirror_content(self, mirror_path, *markers):
        """Assert the mirror carries the converted body, never the exception placeholder.

        The worker writes ``DOC_MIRROR_PLACEHOLDER`` when the DOCX extraction raises,
        so a success test that checks this helper cannot stay green on a lost transfer.
        """
        text = Path(mirror_path).read_text(encoding="utf-8")
        self.assertNotIn(
            DOC_MIRROR_PLACEHOLDER, text,
            "the worker fell back to the extraction placeholder instead of the document text",
        )
        for marker in markers:
            self.assertIn(marker, text)
        return text

    def assert_readable_docx(self, path):
        """Validate the derivative as a non-empty, readable OOXML Word package."""
        path = Path(path)
        self.assertGreater(path.stat().st_size, 0, "the derivative must not be empty")
        self.assertTrue(zipfile.is_zipfile(path), "the derivative must be a readable OOXML package")
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            self.assertIn("[Content_Types].xml", names)
            self.assertIn("word/document.xml", names)
            document = archive.read("word/document.xml").decode("utf-8")
        self.assertIn("wordprocessingml", document)
        visible = "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", document))
        self.assertTrue(visible.strip(), "the DOCX must contain readable text")

    def doc_staging_dirs(self):
        root = self.root / "data" / "ca-doc"
        return [path for path in root.iterdir() if path.is_dir()] if root.is_dir() else []

    def single_doc_staging_dir(self):
        staging = self.doc_staging_dirs()
        self.assertEqual(1, len(staging), staging)
        return staging[0]

    def foreign_hash(self, text, source=SOURCE):
        filemap = filemap_merge.parse_filemap(text)
        digest = hashlib.sha256()
        for member in filemap.members:
            if member.key != source:
                digest.update(text[member.key_start:member.value_end].encode("utf-8"))
                digest.update(b"\x00")
        return digest.hexdigest(), len(filemap.members)

    def without_member(self, text, key):
        filemap = filemap_merge.parse_filemap(text)
        members = filemap.members
        index = next(i for i, member in enumerate(members) if member.key == key)
        member = members[index]
        if index + 1 < len(members):
            return text[:member.key_start] + text[members[index + 1].key_start:]
        if index > 0:
            return text[:members[index - 1].value_end] + text[member.value_end:]
        return text[:filemap.files_open + 1] + text[filemap.files_close:]

    def assert_failed_without_changes(self, before, exit_code, stdout):
        self.assertNotEqual(0, exit_code)
        after = snapshot(self.root)
        # The active state is unchanged; recovery may leave an inert retained version
        # and the journal in place (it never deletes, moves or archives anything).
        versions = self.version_root().relative_to(self.root).as_posix() + "/"
        journal = self.journal_dir().relative_to(self.root).as_posix() + "/"
        for key, value in before.items():
            bare = key[4:] if key.startswith("dir:") else key
            if bare.startswith(versions) or bare.startswith(journal):
                continue
            self.assertEqual(value, after.get(key), key)
        return self.envelope(stdout)

    def read_text(self):
        return self.filemap_path.read_bytes().decode("utf-8")

    def journal_dir(self):
        return self.output / ".only-file-journal"

    def journal_file(self, source=SOURCE):
        return self.journal_dir() / f"{journal_key(filemap_merge.source_fold(source))}.json"

    def run_cli(self, *arguments, source=SOURCE, extra_env=None):
        environment = dict(os.environ)
        environment.update(extra_env or {})
        return subprocess.run(
            [
                sys.executable, str(SCRIPTS_DIR / "convert_cloud_docs.py"),
                "--topic-id", "demo", "--workspace-root", str(self.root),
                "--only-file", source, "--json", *arguments,
            ],
            capture_output=True, text=True, env=environment, timeout=180,
        )


class SingleEntryMergeTests(OnlyFileTestCase):
    def setUp(self):
        super().setUp()
        self.write_filemap()

    def test_fixture_has_realistic_size(self):
        text = self.write_filemap()
        self.assertEqual(2100, len(filemap_merge.parse_filemap(text).members))

    def test_creates_exactly_one_entry_with_versioned_targets(self):
        text = self.write_filemap()
        before_foreign = self.foreign_hash(text)

        exit_code, stdout, stderr = self.only_file()

        self.assertEqual(0, exit_code, stderr)
        only = self.envelope(stdout)["data"]["only_file"]
        self.assertEqual("created", only["action"])
        self.assertEqual(before_foreign[0], self.foreign_hash(self.read_text())[0])
        entry = self.entry()
        self.assertTrue(entry["markdown_mirror"].startswith(VERSION_ROOT + "/"), entry["markdown_mirror"])
        self.assertTrue(entry["derivative"]["path"].startswith(VERSION_ROOT + "/"))
        self.assertEqual(only["version"], self.active_version_rel())
        self.assert_entry_consistent()
        self.assertTrue((self.root / entry["markdown_mirror"]).is_file())
        self.assertTrue((self.root / entry["derivative"]["path"]).is_file())
        metadata, body = MODULE.parse_markdown_file(str(self.root / entry["markdown_mirror"]))
        self.assertEqual(SOURCE, metadata["source_uri"])
        self.assertEqual(entry["artifact_metadata"]["source_sha256"], metadata["source_sha256"])

    def test_second_run_is_idempotent_for_entry_artifacts_and_filemap(self):
        self.assertEqual(0, self.only_file()[0])
        before = snapshot(self.root)
        filemap_before = self.read_text()
        mirror_mtime = self.mirror_path().stat().st_mtime_ns
        derivative_mtime = self.derivative_path().stat().st_mtime_ns

        exit_code, stdout, stderr = self.only_file()

        self.assertEqual(0, exit_code, stderr)
        only = self.envelope(stdout)["data"]["only_file"]
        self.assertEqual("unchanged", only["action"])
        self.assertFalse(only["entry_changed"])
        self.assertEqual(before, snapshot(self.root))
        self.assertEqual(filemap_before, self.read_text())
        self.assertEqual(mirror_mtime, self.mirror_path().stat().st_mtime_ns)
        self.assertEqual(derivative_mtime, self.derivative_path().stat().st_mtime_ns)

    def test_changed_source_switches_to_a_new_version_and_keeps_the_old_one(self):
        self.assertEqual(0, self.only_file()[0])
        old_entry = self.entry()
        old_mirror = self.root / old_entry["markdown_mirror"]
        old_mirror_bytes = old_mirror.read_bytes()
        old_derivative = self.root / old_entry["derivative"]["path"]
        foreign_before = self.foreign_hash(self.read_text())[0]

        self.source_path.write_bytes(b"\xd0\xcf\x11\xe0" + b"SECOND_REVISION")
        exit_code, stdout, stderr = self.only_file()

        self.assertEqual(0, exit_code, stderr)
        only = self.envelope(stdout)["data"]["only_file"]
        self.assertEqual("updated", only["action"])
        new_entry = self.entry()
        self.assertNotEqual(old_entry["markdown_mirror"], new_entry["markdown_mirror"])
        self.assertNotEqual(old_entry["derivative"]["path"], new_entry["derivative"]["path"])
        self.assert_entry_consistent()
        # Retention: the previous version is not deleted automatically.
        self.assertTrue(old_mirror.is_file())
        self.assertEqual(old_mirror_bytes, old_mirror.read_bytes())
        self.assertTrue(old_derivative.is_file())
        self.assertEqual(foreign_before, self.foreign_hash(self.read_text())[0])

    def test_force_rewrites_only_the_one_source_and_keeps_curated_fields(self):
        entry = {
            "version": "N/A", "mtime": "2026-01-01 00:00:00", "size": "1.0 KB", "sha256": SHA,
            "description": "Handgepflegt", "markdown_mirror": "memory/cloud/topics/demo/legacy/g_neu.md",
            "conversion_status": "converted", "owner_note": "bleibt",
        }
        text = self.write_filemap()
        text = filemap_merge.splice_entry(filemap_merge.parse_filemap(text), SOURCE, entry)
        self.write_filemap(text)
        foreign_before = self.foreign_hash(self.read_text())[0]

        exit_code, stdout, stderr = self.only_file(SOURCE, "--force")

        self.assertEqual(0, exit_code, stderr)
        after = json.loads(self.read_text())["files"][SOURCE]
        self.assertEqual("Handgepflegt", after["description"])
        self.assertEqual("bleibt", after["owner_note"])
        self.assertTrue(after["markdown_mirror"].startswith(VERSION_ROOT + "/"))
        self.assertEqual(foreign_before, self.foreign_hash(self.read_text())[0])
        self.assert_entry_consistent()

    def test_foreign_files_and_unlisted_sources_are_never_touched(self):
        self.write_filemap()
        (self.cloud / "decoy_unlisted.doc").write_bytes(b"\xd0\xcf\x11\xe0DECOY")
        (self.cloud / "sub").mkdir()
        (self.cloud / "sub" / "other.pdf").write_bytes(b"%PDF-1.4 other")
        (self.output / "legacy").mkdir()
        (self.output / "legacy" / "orphan.md").write_text("orphan mirror\n", encoding="utf-8")
        (self.output / "empty_dir").mkdir()
        (self.root / "memory" / "notes.md").write_text("fremd\n", encoding="utf-8")
        before = snapshot(self.root)
        walked = []
        real_walk = os.walk

        def recording_walk(top, *args, **keywords):
            walked.append(os.path.normcase(str(top)))
            return real_walk(top, *args, **keywords)

        with mock.patch.object(MODULE, "walk_scoped", side_effect=AssertionError("scan")), \
             mock.patch.object(MODULE, "retained_artifact_paths", side_effect=AssertionError("scan")), \
             mock.patch.object(MODULE.os, "walk", recording_walk):
            exit_code, stdout, stderr = self.only_file()

        self.assertEqual(0, exit_code, stderr)
        after = snapshot(self.root)
        changed = {path for path in set(before) | set(after) if before.get(path) != after.get(path)}
        changed_files = {
            path for path in changed
            if not path.startswith("dir:") and ".only-file-journal" not in path
        }
        allowed = {FILEMAP, self.mirror_rel(), self.derivative_rel()}
        self.assertEqual(allowed, changed_files)
        for path in self.version_files():
            self.assertTrue(str(path).startswith(str(self.version_root())), path)
        self.assertNotIn("decoy_unlisted", self.read_text())
        self.assertTrue((self.output / "legacy" / "orphan.md").is_file())
        self.assertFalse((self.output / "_derivatives").exists())
        self.envelope(stdout)

    def test_pdf_source_uses_the_non_derivative_path(self):
        self.write_filemap()
        pdf = self.cloud / "form.pdf"
        shutil.copyfile(FIXTURE_PDF, pdf)
        foreign_before = self.foreign_hash(self.read_text(), "data/cloud/DEMO/form.pdf")[0]

        exit_code, stdout, stderr = self.only_file("data/cloud/DEMO/form.pdf", "--ocr-policy", "disabled")

        self.assertEqual(0, exit_code, stderr)
        entry = self.entry("data/cloud/DEMO/form.pdf")
        self.assertTrue(entry["markdown_mirror"].startswith(VERSION_ROOT + "/"))
        self.assertNotIn("derivative", entry)
        self.assertEqual(MODULE.calculate_sha256(str(pdf)), entry["sha256"])
        self.assertEqual(foreign_before, self.foreign_hash(self.read_text(), "data/cloud/DEMO/form.pdf")[0])


class ForeignFormattingTests(OnlyFileTestCase):
    def assert_merge_preserves(self, text, expect_in_text=None):
        before_foreign = self.foreign_hash(text)
        self.write_filemap(text)

        exit_code, stdout, stderr = self.only_file()

        self.assertEqual(0, exit_code, stderr)
        after = self.read_text()
        self.assertEqual(before_foreign[0], self.foreign_hash(after)[0])
        self.assertIn(SOURCE, json.loads(after)["files"])
        if expect_in_text:
            self.assertIn(expect_in_text, after)
        self.assert_entry_consistent()

    def container(self, files_text, newline="\n", indent="  "):
        return (
            "{\n"
            f'{indent}"$schema": "{filemap_merge.FILEMAP_SCHEMA_URI}",\n'
            f'{indent}"schema_version": 1,\n'
            f'{indent}"kind": "cloud-filemap",\n'
            f'{indent}"scope": "topic",\n'
            f'{indent}"storage_id": "default",\n'
            f'{indent}"project": "demo",\n'
            f'{indent}"project_title": "Demo",\n'
            f'{indent}"scan_dir": "data/cloud/DEMO",\n'
            f'{indent}"output_dir": "memory/cloud/topics/demo",\n'
            f'{indent}"exclude_dirs": [],\n'
            f'{indent}"updated_at": "2026-01-01 00:00:00",\n'
            f'{indent}"files": {{{files_text}\n{indent}}}\n'
            "}\n"
        ).replace("\n", newline)

    def test_crlf_and_tab_indentation_is_continued(self):
        text = self.container('\n\t"data/cloud/DEMO/a.pdf": {\n\t\t"version": "N/A",\n\t\t"mtime": "2026-01-01 00:00:00",\n\t\t"size": "1.0 KB",\n\t\t"sha256": "' + SHA + '",\n\t\t"description": "-",\n\t\t"markdown_mirror": "memory/cloud/topics/demo/legacy/a.md",\n\t\t"conversion_status": "converted"\n\t}', newline="\r\n")
        self.assert_merge_preserves(text)

    def test_empty_files_object_with_and_without_indentation(self):
        for files_text in ("", "\n  "):
            with self.subTest(files_text=repr(files_text)):
                self.write_filemap(self.container(files_text))
                exit_code, stdout, stderr = self.only_file()
                self.assertEqual(0, exit_code, stderr)
                self.assertIn(SOURCE, json.loads(self.read_text())["files"])
                self.assert_entry_consistent()

    def test_unsorted_map_appends_without_reordering(self):
        text = self.container('\n  "data/cloud/DEMO/zzz.pdf": {"version":"N/A","mtime":"2026-01-01 00:00:00","size":"1.0 KB","sha256":"' + SHA + '","description":"-","markdown_mirror":"memory/cloud/topics/demo/legacy/zzz.md","conversion_status":"converted"}')
        self.assert_merge_preserves(text)

    def test_legacy_converter_map_without_container_keys_is_accepted(self):
        entry = {"version": "N/A", "mtime": "2026-01-01 00:00:00", "size": "1.0 KB", "sha256": SHA,
                 "description": "-", "markdown_mirror": "memory/cloud/topics/demo/legacy/a.md",
                 "conversion_status": "converted"}
        text = "{\n  \"project\": \"demo\",\n  \"project_title\": \"Demo\",\n  \"files\": {\n    \"data/cloud/DEMO/a.pdf\": " + json.dumps(entry) + "\n  },\n  \"exclude_dirs\": [],\n  \"updated_at\": \"2026-01-01 00:00:00\"\n}\n"
        self.assert_merge_preserves(text)


class FailClosedPathTests(OnlyFileTestCase):
    def setUp(self):
        super().setUp()
        self.write_filemap(build_filemap_text(5))
        self.before = snapshot(self.root)

    def assert_rejected(self, source, phase=None, *extra, patches=()):
        exit_code, stdout, _stderr = self.only_file(source, *extra, patches=patches)
        self.assertNotEqual(0, exit_code)
        self.assertEqual(self.before, snapshot(self.root))
        result = self.envelope(stdout)
        if phase:
            self.assertEqual(phase, result["error"]["phase"], result["error"]["message"])
        self.assertFalse(result["data"]["only_file"]["entry_changed"])
        return result

    def test_path_outside_cloud_dir(self):
        self.assert_rejected("data/cloud/OTHER/x.doc", "only-file")

    def test_prefix_sibling_of_cloud_dir_is_outside(self):
        self.assert_rejected("data/cloud/DEMO2/x.doc", "only-file")

    def test_explicit_cloud_dir_override_must_contain_the_file(self):
        exit_code, stdout, _ = self.run_main(
            "--topic-id", "demo", "--workspace-root", str(self.root),
            "--cloud-dir", "data/cloud/DEMO/sub", "--only-file", SOURCE, "--json",
        )
        self.assertNotEqual(0, exit_code)
        self.assertEqual(self.before, snapshot(self.root))
        self.assertEqual("only-file", self.envelope(stdout)["error"]["phase"])

    def test_parent_traversal(self):
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
        (self.cloud / "~$lock.doc").write_bytes(b"\xd0\xcf\x11\xe0x")
        self.before = snapshot(self.root)
        self.assert_rejected("data/cloud/DEMO/note.txt", "only-file")
        self.assert_rejected("data/cloud/DEMO/~$lock.doc", "only-file")

    def test_symlink_escape_is_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.doc").write_bytes(b"\xd0\xcf\x11\xe0SECRET")
        link = self.cloud / "link.doc"
        try:
            os.symlink(outside / "secret.doc", link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available")
        self.before = snapshot(self.root)
        self.assert_rejected("data/cloud/DEMO/link.doc", "only-file")

    def test_junction_escape_is_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.doc").write_bytes(b"\xd0\xcf\x11\xe0SECRET")
        junction = self.cloud / "junction"
        if not make_directory_link(junction, outside):
            self.skipTest("cannot create a directory link")
        self.before = snapshot(self.root)
        self.assert_rejected("data/cloud/DEMO/junction/secret.doc", "only-file")

    def test_simulated_link_redirect_is_rejected(self):
        real_realpath = os.path.realpath

        def redirect(path, *args, **kwargs):
            if os.path.normcase(str(path)).endswith(os.path.normcase(SOURCE.replace("/", os.sep))):
                return str(self.root / "outside" / "x.doc")
            return real_realpath(path, *args, **kwargs)

        self.assert_rejected(SOURCE, "only-file", patches=[mock.patch.object(MODULE.os.path, "realpath", redirect)])

    def test_wrong_path_casing_is_rejected(self):
        exit_code, stdout, _ = self.only_file("data/cloud/DEMO/G_NEU.doc")
        self.assertNotEqual(0, exit_code)
        self.assertEqual(self.before, snapshot(self.root))
        self.assertEqual("only-file", self.envelope(stdout)["error"]["phase"])

    def test_linked_output_dir_is_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        shutil.rmtree(self.output)
        if not make_directory_link(self.output, outside):
            self.skipTest("cannot create a directory link")
        self.before = snapshot(self.root)
        result = self.assert_rejected(SOURCE, "only-file")
        self.assertIn("output_dir", result["error"]["message"])


class FailClosedFilemapTests(OnlyFileTestCase):
    def reject(self, text=None, expected_phase="filemap", delete=False):
        if delete:
            self.filemap_path.unlink(missing_ok=True)
        elif text is not None:
            self.write_filemap(text)
        before = snapshot(self.root)
        exit_code, stdout, _stderr = self.only_file()
        self.assertNotEqual(0, exit_code)
        self.assertEqual(before, snapshot(self.root))
        result = self.envelope(stdout)
        self.assertEqual(expected_phase, result["error"]["phase"], result["error"]["message"])
        return result

    def test_missing_filemap_is_not_created(self):
        self.reject(delete=True, expected_phase="only-file")

    def test_invalid_json_and_wrong_container(self):
        self.reject("{", expected_phase="filemap")
        self.reject("[]", expected_phase="filemap")
        self.reject('{"files": []}', expected_phase="filemap")

    def test_nan_and_trailing_garbage(self):
        self.reject(build_filemap_text(3).replace('"updated_at": "2026-01-01 00:00:00"', '"updated_at": NaN'), expected_phase="filemap")
        self.reject(build_filemap_text(3) + "x", expected_phase="filemap")

    def test_container_contract_is_complete(self):
        text = build_filemap_text(3)
        self.reject(text.replace('"scope": "topic"', '"scope": "project"'), expected_phase="filemap")
        self.reject(text.replace('"storage_id": "default"', '"storage_id": "other"'), expected_phase="filemap")
        self.reject(text.replace('"project": "demo"', '"project": "other"'), expected_phase="filemap")

    def test_entry_schema_violations(self):
        text = build_filemap_text(3)
        broken_entry = {
            "version": "N/A", "mtime": "not-a-timestamp", "size": "1.0 KB",
            "sha256": SHA, "description": "-",
            "markdown_mirror": "memory/cloud/topics/demo/legacy/a.md",
            "conversion_status": "converted",
        }
        broken = filemap_merge.splice_entry(
            filemap_merge.parse_filemap(text), "data/cloud/DEMO/broken.pdf", broken_entry,
        )
        self.reject(broken, expected_phase="filemap")

    def test_duplicate_mirror_between_foreign_entries(self):
        text = build_filemap_text(3)
        data = json.loads(text)
        first_key, second_key = list(data["files"])[:2]
        data["files"][second_key]["markdown_mirror"] = data["files"][first_key]["markdown_mirror"]
        with self.assertRaises(filemap_merge.FilemapError):
            filemap_merge.validate_filemap_document(
                data, scan_dir="data/cloud/DEMO", output_dir="memory/cloud/topics/demo", expected=None,
            )

    def test_duplicate_json_key_for_the_target_source(self):
        text = build_filemap_text(3)
        entry = {"version": "N/A", "mtime": "2026-01-01 00:00:00", "size": "1.0 KB", "sha256": SHA,
                 "description": "-", "markdown_mirror": "memory/cloud/topics/demo/legacy/g_neu.md",
                 "conversion_status": "converted"}
        text = filemap_merge.splice_entry(filemap_merge.parse_filemap(text), SOURCE, entry)
        parsed = filemap_merge.parse_filemap(text)
        member = next(item for item in parsed.members if item.key == SOURCE)
        duplicate = (
            text[:member.value_end]
            + ", " + json.dumps(SOURCE) + ": " + json.dumps(entry)
            + text[member.value_end:]
        )
        result = self.reject(duplicate, expected_phase="filemap")
        self.assertTrue(result["data"]["only_file"]["duplicate_source"])

    def test_case_variant_source_is_a_duplicate(self):
        text = build_filemap_text(3)
        variant = text.replace('f0000.pdf', 'G_NEU.doc', 1).replace(
            'memory/cloud/topics/demo/legacy/f0000.md', 'memory/cloud/topics/demo/legacy/g_neu.md', 1,
        )
        self.write_filemap(variant)
        exit_code, stdout, _ = self.only_file("data/cloud/DEMO/g_neu.doc")
        self.assertNotEqual(0, exit_code)
        self.assertTrue(self.envelope(stdout)["data"]["only_file"]["duplicate_source"])

    def test_same_stem_sources_get_separate_version_directories(self):
        self.write_filemap(build_filemap_text(3))
        (self.cloud / "sub").mkdir()
        (self.cloud / "sub" / "g_neu.doc").write_bytes(b"\xd0\xcf\x11\xe0X")
        self.assertEqual(0, self.only_file(SOURCE)[0])
        first = self.active_version_rel()
        self.assertEqual(0, self.only_file("data/cloud/DEMO/sub/g_neu.doc")[0])
        second = self.active_version_rel("data/cloud/DEMO/sub/g_neu.doc")
        self.assertNotEqual(first, second)
        self.assert_entry_consistent("data/cloud/DEMO/sub/g_neu.doc")

    def test_unowned_existing_legacy_mirror_is_left_alone(self):
        self.write_filemap(build_filemap_text(3))
        legacy = self.output / "g_neu.md"
        legacy.write_text("foreign legacy mirror\n", encoding="utf-8")
        before = legacy.read_bytes()

        exit_code, stdout, stderr = self.only_file()

        self.assertEqual(0, exit_code, stderr)
        self.assertEqual(before, legacy.read_bytes())
        self.assertTrue(self.mirror_rel().startswith(VERSION_ROOT + "/"))

    def test_source_named_like_the_filemap_uses_a_version_directory(self):
        (self.cloud / "filemap.doc").write_bytes(b"\xd0\xcf\x11\xe0X")
        self.write_filemap(build_filemap_text(3))
        exit_code, stdout, stderr = self.only_file("data/cloud/DEMO/filemap.doc")
        self.assertEqual(0, exit_code, stderr)
        entry = self.entry("data/cloud/DEMO/filemap.doc")
        self.assertTrue(entry["markdown_mirror"].startswith(VERSION_ROOT + "/"))

    def test_ambiguous_storage_requires_storage_id(self):
        topics = self.root / "memory" / "references" / "topics" / "topics.json"
        data = json.loads(topics.read_text(encoding="utf-8"))
        data[0]["cloud_sync"]["second"] = dict(
            data[0]["cloud_sync"]["default"],
            output_json="memory/cloud/topics/demo/filemap-second.json",
            output_dir="memory/cloud/topics/demo/second",
        )
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

    def test_enrich_source_is_rejected_with_a_clear_json_error(self):
        self.write_filemap(build_filemap_text(5))
        before = snapshot(self.root)
        exit_code, stdout, stderr = self.only_file(SOURCE, "--ocr-policy", "enrich_source")
        self.assertEqual(2, exit_code)
        result = self.envelope(stdout)
        self.assertEqual("arguments", result["error"]["phase"])
        self.assertIn("--only-file never mutates the source", stderr)
        self.assertIn("local_derivative", stderr)
        self.assertNotIn("only_file", result["data"])
        self.assertEqual(before, snapshot(self.root))

    def test_local_derivative_and_disabled_policies_stay_allowed(self):
        for policy in ("local_derivative", "disabled"):
            with self.subTest(policy=policy):
                args = MODULE.parse_args([
                    "--topic-id", "demo", "--only-file", "a.pdf", "--ocr-policy", policy,
                ])
                self.assertEqual(policy, args.ocr_policy)

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


def make_directory_link(link, target):
    """Symlink, or a Windows junction when symlinks are not permitted."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        completed = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
        return completed.returncode == 0
    return False


def tree_worker(connection, stage_path, heartbeat_path, ready_path):
    Path(stage_path).parent.mkdir(parents=True, exist_ok=True)
    stage = open(stage_path, "wb")
    code = (
        "import sys, time\ndeadline = time.time() + 90\nwhile time.time() < deadline:\n"
        "    open(sys.argv[1], 'w').write(str(time.time()))\n    time.sleep(0.1)\n"
    )
    subprocess.Popen([sys.executable, "-c", code, heartbeat_path])
    Path(ready_path).write_text("ready")
    time.sleep(120)
    stage.close()
    connection.close()


class HangingTreeProcess:
    """Worker that has started an external converter child, then never finishes."""

    stage_root = None
    stage_path = heartbeat_path = ready_path = None

    def __init__(self, target=None, args=(), kwargs=None):
        if self.stage_root:
            stages = [p for p in Path(self.stage_root).rglob("*") if p.is_dir() and not p.is_symlink()]
            if stages:
                HangingTreeProcess.stage_path = str(stages[0] / "partial.stage")
        self._process = multiprocessing.get_context("spawn").Process(
            target=tree_worker, args=(args[1], self.stage_path, self.heartbeat_path, self.ready_path),
        )

    def start(self):
        self._process.start()
        deadline = time.time() + 60
        while not Path(self.ready_path).exists():
            if time.time() > deadline:
                raise TimeoutError("tree worker did not become ready")
            time.sleep(0.1)

    def __getattr__(self, name):
        return getattr(self._process, name)


class FailureCleanupTests(OnlyFileTestCase):
    def setUp(self):
        super().setUp()
        self.write_filemap()

    @staticmethod
    def failing_filemap_replace():
        real_replace = os.replace

        def replace(source, destination, *args, **kwargs):
            if Path(str(destination)).name == "filemap.json":
                raise OSError("simulated replace failure")
            return real_replace(source, destination, *args, **kwargs)

        return mock.patch("os.replace", replace)

    def test_filemap_replace_failure_retains_the_new_version(self):
        before = snapshot(self.root)
        exit_code, stdout, _stderr = self.only_file(patches=[self.failing_filemap_replace()])
        result = self.assert_failed_without_changes(before, exit_code, stdout)
        only = result["data"]["only_file"]
        self.assertTrue(only["retained_version"], only)
        self.assertTrue(self.version_root().exists(), "recovery never deletes the new version")
        self.assertIsNone(self.entry())

    def test_filemap_replace_failure_keeps_the_old_version_on_update(self):
        self.assertEqual(0, self.only_file()[0])
        committed = snapshot(self.root)
        old_entry = self.entry()
        old_version = self.active_version_rel()
        self.source_path.write_bytes(b"\xd0\xcf\x11\xe0" + b"SECOND_REVISION")
        committed[SOURCE] = sha256_bytes(self.source_path.read_bytes())

        exit_code, stdout, _stderr = self.only_file(SOURCE, "--force", patches=[self.failing_filemap_replace()])

        self.assertNotEqual(0, exit_code)
        only = self.envelope(stdout)["data"]["only_file"]
        self.assertTrue(only["retained_version"], only)
        self.assertEqual(old_entry, self.entry())
        self.assertEqual(old_version, self.active_version_rel())
        self.assert_entry_consistent()

    def test_mirror_write_failure_retains_the_staging(self):
        before = snapshot(self.root)
        with mock.patch.object(MODULE, "write_markdown_file", side_effect=OSError("disk full")):
            exit_code, stdout, _ = self.only_file()
        result = self.assert_failed_without_changes(before, exit_code, stdout)
        only = result["data"]["only_file"]
        self.assertTrue(only["retained_version"], only)
        self.assertTrue(self.version_root().exists())

    def test_timeout_leaves_the_staging_in_place(self):
        HangingProcess.stage_root = self.version_root()
        HangingProcess.partial_paths = []
        before = snapshot(self.root)
        started = time.time()
        exit_code, stdout, _stderr = self.only_file(
            SOURCE, "--file-timeout", "1",
            patches=[mock.patch.object(MODULE.multiprocessing, "Process", HangingProcess)],
        )
        self.assertLess(time.time() - started, 60)
        result = self.assert_failed_without_changes(before, exit_code, stdout)
        self.assertEqual("ConversionRequired", result["state"])
        only = result["data"]["only_file"]
        self.assertTrue(only["retained_version"], only)
        self.assertTrue(self.version_root().exists())

    def test_conversion_error_does_not_publish_a_version(self):
        failure = {SOURCE: {"success": False, "markdown_body": None, "ocr_applied": False, "error": "boom"}}

        def failing_scheduler(tasks, **_kwargs):
            leftover = Path(tasks[0]["derivative_abs"])
            leftover.parent.mkdir(parents=True, exist_ok=True)
            leftover.write_bytes(b"HALF")
            return failure

        before = snapshot(self.root)
        exit_code, stdout, _ = self.only_file(patches=[mock.patch.object(MODULE, "run_conversion_tasks", failing_scheduler)])
        result = self.assert_failed_without_changes(before, exit_code, stdout)
        self.assertEqual("conversion", result["error"]["phase"])
        only = result["data"]["only_file"]
        self.assertTrue(only["retained_version"], only)
        self.assertTrue(self.version_root().exists())

    def test_missing_doc_converter_is_reported_without_writing(self):
        os.environ["CLOUD_ATLAS_DOC_CONVERTER_MOCK"] = "none"
        before = snapshot(self.root)
        exit_code, stdout, _ = self.only_file()
        result = self.assert_failed_without_changes(before, exit_code, stdout)
        self.assertEqual("ConversionRequired", result["state"])
        self.assertEqual("doc_converter", result["error"]["requirements"][0]["capability"])

    def test_enrich_source_is_rejected_before_any_mutation(self):
        os.environ["CLOUD_ATLAS_OCR_MOCK"] = "1"
        os.environ["CLOUD_ATLAS_MOCK_IS_SCAN"] = "1"
        pdf = self.cloud / "scan.pdf"
        shutil.copyfile(FIXTURE_PDF, pdf)
        before = snapshot(self.root)
        original = pdf.read_bytes()

        exit_code, stdout, stderr = self.only_file("data/cloud/DEMO/scan.pdf", "--ocr-policy", "enrich_source")

        self.assertEqual(2, exit_code, stderr)
        self.assertIn("--only-file never mutates the source", stderr)
        self.assertEqual(before, snapshot(self.root))
        self.assertEqual(original, pdf.read_bytes())
        self.assertEqual("arguments", self.envelope(stdout)["error"]["phase"])

    def test_enrich_source_programmatic_guard_is_fail_closed(self):
        shutil.copyfile(FIXTURE_PDF, self.cloud / "scan.pdf")
        before = snapshot(self.root)
        original = (self.cloud / "scan.pdf").read_bytes()
        args = MODULE.parse_args([
            "--topic-id", "demo", "--workspace-root", str(self.root),
            "--only-file", "data/cloud/DEMO/scan.pdf", "--json",
        ])
        configs = MODULE.resolve_all_sync_configs(str(self.root), "demo", False, None, None)
        with self.assertRaises(MODULE.ConversionRunError) as raised:
            MODULE.run_only_file_conversion(
                args, {"target": None, "storages": []}, str(self.root),
                configs, {}, "enrich_source", False,
            )
        self.assertIn("never mutates the source", str(raised.exception))
        self.assertEqual(before, snapshot(self.root))
        self.assertEqual(original, (self.cloud / "scan.pdf").read_bytes())

    def test_timeout_kills_the_converter_process_tree(self):
        with tempfile.TemporaryDirectory() as scratch:
            HangingTreeProcess.stage_root = self.version_root()
            HangingTreeProcess.heartbeat_path = str(Path(scratch) / "heartbeat")
            HangingTreeProcess.ready_path = str(Path(scratch) / "ready")
            before = snapshot(self.root)
            exit_code, stdout, _ = self.only_file(
                SOURCE, "--file-timeout", "1",
                patches=[mock.patch.object(MODULE.multiprocessing, "Process", HangingTreeProcess)],
            )
            result = self.assert_failed_without_changes(before, exit_code, stdout)
            self.assertEqual("ConversionRequired", result["state"])
            heartbeat = Path(HangingTreeProcess.heartbeat_path)
            time.sleep(1.0)
            first = heartbeat.stat().st_mtime_ns
            time.sleep(0.8)
            self.assertEqual(first, heartbeat.stat().st_mtime_ns, "the converter child kept running after the timeout")


class CommitPointTests(OnlyFileTestCase):
    def setUp(self):
        super().setUp()
        self.write_filemap()

    def test_output_failure_after_the_filemap_replace_keeps_the_committed_state(self):
        real_print = print

        def failing_print(*values, **keywords):
            if values and str(values[0]).lstrip().startswith(("Saved filemap", "Zusammenfassung")):
                raise OSError("stream closed")
            return real_print(*values, **keywords)

        with mock.patch("builtins.print", failing_print):
            exit_code, stdout, stderr = self.only_file()

        self.assertEqual(0, exit_code, stderr)
        only = self.envelope(stdout)["data"]["only_file"]
        self.assertEqual("created", only["action"])
        self.assert_entry_consistent()

    def test_an_exception_right_after_the_filemap_replace_is_reported_as_success(self):
        real_write = MODULE.write_text_atomic_cas

        def write_then_fail(*args, **keywords):
            real_write(*args, **keywords)
            raise RuntimeError("after replace")

        with mock.patch.object(MODULE, "write_text_atomic_cas", write_then_fail):
            exit_code, stdout, stderr = self.only_file()

        self.assertEqual(0, exit_code, stderr)
        envelope = self.envelope(stdout)
        self.assertTrue(envelope["success"])
        only = envelope["data"]["only_file"]
        self.assertEqual("created", only["action"])
        self.assert_entry_consistent()

    def test_the_journal_stays_in_place_after_a_successful_run(self):
        exit_code, stdout, stderr = self.only_file()
        self.assertEqual(0, exit_code, stderr)

        journal = self.journal_file()

        self.assertTrue(journal.is_file(), "the journal is never archived or deleted")
        self.assertEqual("committed", json.loads(journal.read_text(encoding="utf-8"))["state"])
        # A second run proves the recorded commit and continues idempotently.
        exit_code, stdout, stderr = self.only_file()
        self.assertEqual(0, exit_code, stderr)
        only = self.envelope(stdout)["data"]["only_file"]
        self.assertEqual("committed", only["recovered"])
        self.assertEqual("unchanged", only["action"])
        self.assertTrue(journal.is_file())

class ConcurrencyLockTests(OnlyFileTestCase):
    def setUp(self):
        super().setUp()
        self.write_filemap()
        self.foreign = self.foreign_hash(self.read_text())[0]

    def add_foreign_entry(self):
        entry = {
            "version": "N/A", "mtime": "2026-01-01 00:00:00", "size": "1.0 KB",
            "sha256": "e" * 64, "description": "fremd",
            "markdown_mirror": "memory/cloud/topics/demo/legacy/zzz.md",
            "conversion_status": "converted",
        }
        text = filemap_merge.splice_entry(
            filemap_merge.parse_filemap(self.read_text()), "data/cloud/DEMO/zzz.pdf", entry,
        )
        self.write_filemap(text)
        self.foreign = self.foreign_hash(self.read_text())[0]
        return "data/cloud/DEMO/zzz.pdf"

    def save_hook(self, action, when="staged"):
        real_save = MODULE.OnlyFileJournal.save

        def save(journal, record):
            real_save(journal, record)
            version = record.get("version") or {}
            if when == "staged" and record["state"] == "staged":
                action()
            elif when == "version-ready" and record["state"] == "planned" and version.get("mirror"):
                action()

        return mock.patch.object(MODULE.OnlyFileJournal, "save", save)

    def test_the_filemap_lock_blocks_a_second_writer(self):
        held = self.hold_storage_lock()
        os.environ["CLOUD_ATLAS_ONLY_FILE_LOCK_TIMEOUT"] = "0.5"
        before = snapshot(self.root)
        try:
            exit_code, stdout, stderr = self.only_file()
        finally:
            os.environ.pop("CLOUD_ATLAS_ONLY_FILE_LOCK_TIMEOUT", None)
            held.__exit__()

        self.assertNotEqual(0, exit_code)
        self.assertEqual("conflict", self.envelope(stdout)["error"]["phase"])
        self.assertEqual(before, snapshot(self.root))

    def test_one_storage_lock_is_shared_by_all_sources_of_the_storage(self):
        first = self.storage_lock_specs()
        second = self.storage_lock_specs()
        self.assertEqual(first, second)
        exclusive = {path for path, shared in first if not shared}
        self.assertEqual(2, len(exclusive), "output and filemap are exclusive")
        # Same output, different filemap: the output lock is identical.
        shared_output = self.storage_lock_specs(
            filemap=self.root / "memory/cloud/topics/demo/other.json",
        )
        self.assertTrue(exclusive & {path for path, _ in shared_output if path in exclusive})
        # A nested output takes the parent's output lock as a *shared* lock.
        nested = self.storage_lock_specs(
            filemap=self.root / "memory/cloud/topics/demo/sub/filemap.json",
            output="memory/cloud/topics/demo/sub",
        )
        nested_shared = {path for path, shared in nested if shared}
        self.assertTrue(exclusive & nested_shared, "nested output must cooperate with its parent")
        # Sibling outputs only share shared ancestors, never an exclusive path.
        sibling = self.storage_lock_specs(
            filemap=self.root / "memory/cloud/topics/demo/other/filemap.json",
            output="memory/cloud/topics/demo/other",
        )
        self.assertFalse(exclusive & {path for path, shared in sibling if not shared})
        for path in self.storage_lock_paths():
            self.assertTrue(path.startswith(str(self.root / "data" / ".locks" / "cloud-atlas")), path)

    def test_an_only_file_filemap_override_cannot_bypass_the_storage_lock(self):
        self.write_filemap()
        alternative = self.root / "memory/cloud/topics/demo/alt.json"
        alternative.write_bytes(self.filemap_path.read_bytes())
        held = self.hold_storage_lock()
        os.environ["CLOUD_ATLAS_ONLY_FILE_LOCK_TIMEOUT"] = "0.5"
        before = snapshot(self.root)
        try:
            exit_code, stdout, stderr = self.only_file(
                SOURCE, "--filemap-json", "memory/cloud/topics/demo/alt.json",
            )
        finally:
            os.environ.pop("CLOUD_ATLAS_ONLY_FILE_LOCK_TIMEOUT", None)
            held.__exit__()

        self.assertNotEqual(0, exit_code)
        self.assertEqual("conflict", self.envelope(stdout)["error"]["phase"])
        self.assertEqual(before, snapshot(self.root))

    def test_a_linked_lock_directory_is_rejected(self):
        outside = self.root / "outside-locks"
        outside.mkdir()
        locks = self.root / "data" / ".locks"
        if not make_directory_link(locks, outside):
            self.skipTest("cannot create a directory link")
        before = snapshot(self.root)
        exit_code, stdout, stderr = self.only_file()
        self.assertNotEqual(0, exit_code)
        self.assertEqual(before, snapshot(self.root))

    def test_a_killed_lock_holder_does_not_block_the_next_process(self):
        scratch = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, scratch, True)
        lock_path = next(
            path for path, shared in MODULE.only_file_storage_lock_specs(
                str(self.root), str(self.root / "memory/cloud/topics/demo"), str(scratch / "f.json"),
            ) if not shared
        )
        code = (
            "import sys; sys.path.insert(0, r'%s');"
            "import convert_cloud_docs as m;"
            "lock = m.OnlyFileLock(r'%s', timeout=2); lock.__enter__();"
            "import os; os._exit(7)"
        ) % (SCRIPTS_DIR, lock_path)
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
        self.assertEqual(7, result.returncode, result.stderr)
        with MODULE.OnlyFileLock(lock_path, timeout=2.0):
            pass

    def test_a_foreign_entry_added_while_staging_is_preserved(self):
        other = None

        def action():
            nonlocal other
            other = self.add_foreign_entry()

        with self.save_hook(action, when="version-ready"):
            exit_code, stdout, stderr = self.only_file()

        self.assertEqual(0, exit_code, stderr)
        files = json.loads(self.read_text())["files"]
        self.assertIn(other, files)
        self.assertEqual(self.foreign, self.foreign_hash(self.read_text())[0])
        self.assert_entry_consistent()

    def test_a_concurrent_change_of_the_own_entry_stops_the_publish(self):
        def action():
            entry = {
                "version": "N/A", "mtime": "2026-01-01 00:00:00", "size": "1.0 KB",
                "sha256": "d" * 64, "description": "foreign",
                "markdown_mirror": "memory/cloud/topics/demo/legacy/g_neu.md",
                "conversion_status": "converted",
            }
            text = filemap_merge.splice_entry(
                filemap_merge.parse_filemap(self.read_text()), SOURCE, entry,
            )
            self.write_filemap(text)

        with self.save_hook(action, when="version-ready"):
            exit_code, stdout, stderr = self.only_file()

        self.assertNotEqual(0, exit_code)
        self.assertEqual("conflict", self.envelope(stdout)["error"]["phase"])
        entry = self.entry()
        self.assertEqual("foreign", entry["description"])
        self.assertEqual("memory/cloud/topics/demo/legacy/g_neu.md", entry["markdown_mirror"])
        self.assertTrue(self.envelope(stdout)["data"]["only_file"]["retained_version"])
        self.assertTrue(self.version_root().exists())

    def test_a_change_after_the_armed_save_aborts_before_the_replace(self):
        def action():
            entry = {
                "version": "N/A", "mtime": "2026-01-01 00:00:00", "size": "1.0 KB",
                "sha256": "d" * 64, "description": "foreign",
                "markdown_mirror": "memory/cloud/topics/demo/legacy/g_neu.md",
                "conversion_status": "converted",
            }
            text = filemap_merge.splice_entry(
                filemap_merge.parse_filemap(self.read_text()), SOURCE, entry,
            )
            self.write_filemap(text)

        with self.save_hook(action):
            exit_code, stdout, stderr = self.only_file()

        self.assertNotEqual(0, exit_code)
        self.assertEqual("filemap", self.envelope(stdout)["error"]["phase"])
        entry = self.entry()
        self.assertEqual("foreign", entry["description"])
        self.assertTrue(self.envelope(stdout)["data"]["only_file"]["retained_version"])
        self.assertTrue(self.version_root().exists())
        journal = self.journal_file()
        self.assertTrue(journal.is_file(), "the journal is never archived after an aborted publish")

    def test_a_foreign_entry_written_directly_before_the_replace_survives(self):
        real_cas = MODULE.write_text_atomic_cas
        injected = {}

        def inject(path, text, **keywords):
            injected["key"] = self.add_foreign_entry()
            return real_cas(path, text, **keywords)

        with mock.patch.object(MODULE, "write_text_atomic_cas", inject):
            exit_code, stdout, stderr = self.only_file()

        self.assertNotEqual(0, exit_code)
        self.assertIn("filemap changed while this run was staging", stdout)
        files = json.loads(self.read_text())["files"]
        self.assertIn(injected["key"], files)
        self.assertNotIn(SOURCE, files)
        self.assertEqual(self.foreign, self.foreign_hash(self.read_text())[0])
        self.assertTrue(self.envelope(stdout)["data"]["only_file"]["retained_version"])
        self.assertTrue(self.version_root().exists())


class CrashRecoveryTests(OnlyFileTestCase):
    """Hard kill after every step: only a proven commit lets the next run continue."""

    PRE_COMMIT = ("planned", "writing", "temp", "filemap_tmp", "staged")
    POST_COMMIT = ("filemap", "published", "committed")

    def setUp(self):
        super().setUp()
        self.write_filemap()

    def crash(self, point):
        result = self.run_cli(extra_env={"CLOUD_ATLAS_ONLY_FILE_CRASH_AFTER": point})
        self.assertEqual(137, result.returncode, result.stderr[-1500:])
        return result

    def assert_no_mixed_state(self):
        entry = self.entry()
        if entry is not None:
            self.assert_entry_consistent()

    def test_a_crash_before_the_commit_fails_closed_and_changes_nothing(self):
        for point in self.PRE_COMMIT:
            with self.subTest(point=point):
                self.tearDown()
                self.setUp()
                self.crash(point)
                self.assertIsNone(self.entry(), "the filemap must not reference an unpublished version")
                journal = self.journal_file()
                self.assertTrue(journal.is_file())
                journal_bytes = journal.read_bytes()
                before = snapshot(self.root)

                exit_code, stdout, stderr = self.only_file()

                self.assertNotEqual(0, exit_code)
                envelope = self.envelope(stdout)
                self.assertEqual("journal", envelope["error"]["phase"], point)
                only = envelope["data"]["only_file"]
                self.assertEqual(
                    f"{self.journal_dir().relative_to(self.root).as_posix()}/{journal.name}",
                    only["journal"],
                )
                self.assertEqual(before, snapshot(self.root), "recovery never writes, moves or deletes")
                self.assertEqual(journal_bytes, journal.read_bytes())

                # Operator action: removing the journal is the documented way out.
                journal.unlink()
                exit_code, stdout, stderr = self.only_file()
                self.assertEqual(0, exit_code, stderr)
                self.assert_entry_consistent()
                self.assert_no_mixed_state()

    def test_a_crash_after_the_commit_lets_the_next_run_continue(self):
        for point in self.POST_COMMIT:
            with self.subTest(point=point):
                self.tearDown()
                self.setUp()
                self.crash(point)
                self.assertIsNotNone(self.entry())
                journal = self.journal_file()
                self.assertTrue(journal.is_file())

                exit_code, stdout, stderr = self.only_file()

                self.assertEqual(0, exit_code, stderr)
                only = self.envelope(stdout)["data"]["only_file"]
                self.assertEqual("committed", only["recovered"])
                self.assertEqual("unchanged", only["action"])
                self.assert_entry_consistent()
                self.assertTrue(journal.is_file())

    def test_a_staged_crash_keeps_the_version_and_its_bytes(self):
        self.crash("staged")
        versions = {p.name: p for p in self.version_dirs() if not p.name.endswith(".tmp")}
        snapshot_of_versions = {
            str(p.relative_to(self.root)): p.read_bytes()
            for p in self.version_files()
        }

        exit_code, stdout, stderr = self.only_file()

        self.assertNotEqual(0, exit_code)
        only = self.envelope(stdout)["data"]["only_file"]
        self.assertTrue(only["retained_version"], only)
        self.assertTrue((self.root / only["retained_version"]).exists())
        self.assertEqual(
            versions,
            {p.name: p for p in self.version_dirs() if not p.name.endswith(".tmp")},
        )
        for rel, content in snapshot_of_versions.items():
            self.assertEqual(content, (self.root / rel).read_bytes(), rel)

    def test_an_unreadable_journal_fails_closed(self):
        self.crash("planned")
        self.journal_file().write_text("{not json", encoding="utf-8")
        before = snapshot(self.root)

        exit_code, stdout, stderr = self.only_file()

        self.assertNotEqual(0, exit_code)
        self.assertEqual("journal", self.envelope(stdout)["error"]["phase"])
        self.assertEqual(before, snapshot(self.root))

    def test_a_corrupted_active_version_after_a_committed_crash_fails_closed(self):
        self.crash("filemap")
        mirror = self.mirror_path()
        self.assertTrue(mirror.is_file())
        mirror.write_bytes(b"CORRUPT")

        exit_code, stdout, stderr = self.only_file()

        self.assertNotEqual(0, exit_code)
        envelope = self.envelope(stdout)
        self.assertEqual("journal", envelope["error"]["phase"])
        self.assertTrue(envelope["data"]["only_file"]["retained_version"])

    def test_recovery_never_deletes_moves_or_writes(self):
        self.crash("staged")
        forbidden = mock.patch.object(MODULE.os, "unlink", side_effect=AssertionError("unlink")), \
            mock.patch.object(MODULE.os, "remove", side_effect=AssertionError("remove")), \
            mock.patch.object(MODULE.os, "rename", side_effect=AssertionError("rename")), \
            mock.patch.object(MODULE.os, "replace", side_effect=AssertionError("replace")), \
            mock.patch.object(MODULE.shutil, "rmtree", side_effect=AssertionError("rmtree"))

        with contextlib.ExitStack() as stack:
            for patch in forbidden:
                stack.enter_context(patch)
            exit_code, stdout, stderr = self.only_file()

        self.assertNotEqual(0, exit_code)
        self.assertEqual("journal", self.envelope(stdout)["error"]["phase"])
        self.assertTrue(self.journal_file().is_file())

class RecoveryJournalTests(unittest.TestCase):
    """Recovery reports and fails closed; it never writes, moves or deletes."""

    def make_workspace(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        output = root / "memory" / "cloud" / "topic"
        output.mkdir(parents=True)
        filemap = output / "filemap.json"
        filemap.write_text('{"files": {}}', encoding="utf-8")
        return root, output, filemap

    def make_journal(self, root):
        from core.publish_journal import OnlyFileJournal
        return OnlyFileJournal(
            str(root), "memory/cloud/topic", "data/cloud/D/one.doc",
            filemap_merge.source_fold("data/cloud/D/one.doc"),
            "memory/cloud/topic/filemap.json",
        )

    def paths(self, journal, run="run1"):
        key = journal.key[:16]
        base = f"memory/cloud/topic/_versions/{key}"
        return {
            "dir": f"{base}/{run}",
            "mirror": f"{base}/{run}/one.md",
            "derivative": f"{base}/{run}/one.docx",
        }

    def record(self, journal, version, state="staged", entry=None):
        return {
            "schema": 3,
            "source": "data/cloud/D/one.doc",
            "state": state,
            "filemap": {"path": "memory/cloud/topic/filemap.json"},
            "entry": entry or {"old_sha256": None, "new_sha256": "f" * 64},
            "version": version,
        }

    def committed_layout(self, root, journal):
        paths = self.paths(journal)
        run = root / paths["dir"]
        run.mkdir(parents=True)
        mirror = run / "one.md"
        mirror.write_bytes(b"MIRROR")
        digest = hashlib.sha256(b"MIRROR").hexdigest()
        version = {"dir": paths["dir"],
                   "mirror": {"path": paths["mirror"], "sha256": digest}, "derivative": None}
        return paths, mirror, version

    def test_a_committed_journal_with_the_live_entry_continues(self):
        root, output, filemap = self.make_workspace()
        journal = self.make_journal(root)
        paths, mirror, version = self.committed_layout(root, journal)
        new_digest = MODULE.entry_digest({"markdown_mirror": paths["mirror"]})
        record = self.record(journal, version, state="committed",
                             entry={"old_sha256": "b" * 64, "new_sha256": new_digest})
        journal.save(record)

        outcome = journal.recover(record, lambda: new_digest, str(output))

        self.assertEqual("committed", outcome)
        self.assertTrue(journal.exists(), "recovery never archives or deletes the journal")
        self.assertTrue(mirror.is_file())

    def test_a_staged_journal_whose_commit_crossed_continues(self):
        root, output, filemap = self.make_workspace()
        journal = self.make_journal(root)
        paths, mirror, version = self.committed_layout(root, journal)
        new_digest = MODULE.entry_digest({"markdown_mirror": paths["mirror"]})
        record = self.record(journal, version, state="staged",
                             entry={"old_sha256": "b" * 64, "new_sha256": new_digest})
        journal.save(record)

        self.assertEqual("committed", journal.recover(record, lambda: new_digest, str(output)))

    def test_a_planned_journal_is_unclear_and_reports_the_existing_version(self):
        root, output, filemap = self.make_workspace()
        journal = self.make_journal(root)
        paths = self.paths(journal)
        (root / paths["dir"]).mkdir(parents=True)
        version = {"dir": paths["dir"], "mirror": None, "derivative": None}
        record = self.record(journal, version, state="planned",
                             entry={"old_sha256": None, "new_sha256": None})
        journal.save(record)

        with self.assertRaises(MODULE.JournalError):
            journal.recover(record, lambda: None, str(output))

        self.assertEqual(paths["dir"], journal.retained_version)

    def test_a_staged_journal_that_did_not_commit_is_unclear(self):
        root, output, filemap = self.make_workspace()
        journal = self.make_journal(root)
        paths, mirror, version = self.committed_layout(root, journal)
        record = self.record(journal, version, state="staged")

        with self.assertRaises(MODULE.JournalError):
            journal.recover(record, lambda: "c" * 64, str(output))

        self.assertEqual(paths["dir"], journal.retained_version)

    def test_active_version_corruption_is_detected(self):
        root, output, filemap = self.make_workspace()
        journal = self.make_journal(root)
        paths, mirror, version = self.committed_layout(root, journal)
        mirror.write_bytes(b"CORRUPT")
        new_digest = MODULE.entry_digest({"markdown_mirror": paths["mirror"]})
        record = self.record(journal, version, state="committed",
                             entry={"old_sha256": "b" * 64, "new_sha256": new_digest})

        with self.assertRaises(MODULE.JournalError):
            journal.recover(record, lambda: new_digest, str(output))

    def test_validate_accepts_the_bound_version_and_rejects_escapes(self):
        root, output, filemap = self.make_workspace()
        journal = self.make_journal(root)
        paths = self.paths(journal)
        version = {"dir": paths["dir"],
                   "mirror": {"path": paths["mirror"], "sha256": "a" * 64}, "derivative": None}
        journal.validate(self.record(journal, version))
        for bad in (
            {**version, "dir": "memory/cloud/other/run1"},
            {**version, "dir": f"memory/cloud/topic/_versions/{journal.key[:16]}"},
            {**version, "dir": f"memory/cloud/topic/_versions/{'deadbeefdeadbeef'}/run1"},
            {**version, "mirror": {"path": "memory/cloud/topic/one.md", "sha256": "a" * 64}},
            {**version, "mirror": {"path": paths["dir"] + "/../one.md", "sha256": "a" * 64}},
            {**version, "mirror": {"path": paths["dir"] + "\\one.md", "sha256": "a" * 64}},
            {**version, "mirror": {"path": paths["mirror"], "sha256": "x"}},
            {**version, "tmp": f"memory/cloud/topic/_versions/{journal.key[:16]}/other.tmp"},
        ):
            with self.subTest(bad=bad), self.assertRaises(MODULE.JournalError):
                journal.validate(self.record(journal, bad))
        staged_without_mirror = {"dir": paths["dir"], "mirror": None, "derivative": None}
        with self.assertRaises(MODULE.JournalError):
            journal.validate(self.record(journal, staged_without_mirror, state="staged"))

    def test_version_root_link_outside_the_output_is_refused(self):
        root, output, filemap = self.make_workspace()
        journal = self.make_journal(root)
        paths = self.paths(journal)
        holder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, holder, True)
        link = root / "memory" / "cloud" / "topic" / "_versions"
        link.parent.mkdir(parents=True, exist_ok=True)
        if not make_directory_link(link, holder):
            self.skipTest("cannot create a directory link")
        version = {"dir": paths["dir"], "mirror": None, "derivative": None}

        with self.assertRaises(MODULE.JournalError):
            journal._require_version_path(paths["dir"], str(output))

    def test_recovery_never_calls_unlink_or_rmtree(self):
        root, output, filemap = self.make_workspace()
        journal = self.make_journal(root)
        paths = self.paths(journal)
        (root / paths["dir"]).mkdir(parents=True)
        version = {"dir": paths["dir"], "mirror": None, "derivative": None}
        record = self.record(journal, version, state="planned",
                             entry={"old_sha256": None, "new_sha256": None})
        journal.save(record)
        before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}

        with mock.patch.object(MODULE.os, "unlink", side_effect=AssertionError("unlink")), \
             mock.patch.object(MODULE.os, "remove", side_effect=AssertionError("remove")), \
             mock.patch.object(MODULE.os, "replace", side_effect=AssertionError("replace")), \
             mock.patch.object(MODULE.shutil, "rmtree", side_effect=AssertionError("rmtree")):
            with self.assertRaises(MODULE.JournalError):
                journal.recover(record, lambda: None, str(output))

        self.assertEqual(before, {p: p.read_bytes() for p in root.rglob("*") if p.is_file()})

    def test_a_foreign_file_outside_the_version_root_is_never_touched(self):
        root, output, filemap = self.make_workspace()
        journal = self.make_journal(root)
        foreign = root / "memory" / "cloud" / "topic" / "foreign.md"
        foreign.write_text("keep me", encoding="utf-8")
        paths = self.paths(journal)
        (root / paths["dir"]).mkdir(parents=True)
        version = {"dir": "memory/cloud/other/run1",
                   "mirror": None, "derivative": None}
        record = self.record(journal, version, state="staged",
                             entry={"old_sha256": None, "new_sha256": "f" * 64})

        with self.assertRaises(MODULE.JournalError):
            journal.recover(record, lambda: None, str(output))

        self.assertEqual("keep me", foreign.read_text(encoding="utf-8"))

class StorageAndIdempotenceTests(OnlyFileTestCase):
    def setUp(self):
        super().setUp()
        self.write_filemap(build_filemap_text(5))

    def reject_flag(self, flag, value, phase="only-file", source=SOURCE, *extra):
        before = snapshot(self.root)
        exit_code, stdout, _ = self.run_main(
            "--topic-id", "demo", "--workspace-root", str(self.root),
            "--only-file", source, "--json", flag, value, *extra,
        )
        result = self.assert_failed_without_changes(before, exit_code, stdout)
        self.assertEqual(phase, result["error"]["phase"])
        return result

    def test_drive_relative_segments_in_storage_paths_are_rejected(self):
        self.reject_flag("--output-dir", "memory/cloud/e:y")
        self.reject_flag("--cloud-dir", "data/cloud/DE:MO")
        self.reject_flag("--filemap-json", "memory/cloud/topics/demo/x:y/filemap.json")
        self.reject_flag("--filemap-json", "C:/outside/filemap.json")

    def test_output_and_filemap_must_be_in_a_canonical_data_zone(self):
        result = self.reject_flag("--output-dir", "scratch/out")
        self.assertIn("Data Zones", result["error"]["message"])
        self.reject_flag("--filemap-json", "scratch/filemap.json")

    def test_excluded_directory_is_rejected(self):
        topics = self.root / "memory" / "references" / "topics" / "topics.json"
        data = json.loads(topics.read_text(encoding="utf-8"))
        data[0]["cloud_sync"]["default"]["exclude_dirs"] = ["sub"]
        topics.write_text(json.dumps(data), encoding="utf-8")
        (self.cloud / "sub").mkdir()
        (self.cloud / "sub" / "x.doc").write_bytes(b"\xd0\xcf\x11\xe0x")
        self.write_filemap(build_filemap_text(5))
        before = snapshot(self.root)
        exit_code, stdout, _ = self.only_file("data/cloud/DEMO/sub/x.doc")
        self.assert_failed_without_changes(before, exit_code, stdout)

    def test_project_target_uses_scope_project(self):
        projects = self.root / "memory" / "references" / "projects"
        projects.mkdir(parents=True)
        (projects / "projects.json").write_text(json.dumps([{
            "id": "proj", "title": "Proj",
            "cloud_sync": {"default": {
                "scan_dir": "data/cloud/DEMO", "output_json": FILEMAP,
                "output_md": "memory/cloud/topics/demo/filemap.md", "output_dir": "memory/cloud/topics/demo",
            }},
        }]), encoding="utf-8")
        good = build_filemap_text(5).replace('"scope": "topic"', '"scope": "project"').replace('"project": "demo"', '"project": "proj"')
        self.write_filemap(good)
        exit_code, _stdout, stderr = self.run_main(
            "--project-id", "proj", "--workspace-root", str(self.root), "--only-file", SOURCE, "--json",
        )
        self.assertEqual(0, exit_code, stderr)
        self.assert_entry_consistent()

    def test_subtopic_target_reads_only_the_catalogued_storage(self):
        topics = self.root / "memory" / "references" / "topics" / "topics.json"
        topics.write_text(json.dumps([{
            "id": "demo", "title": "Demo",
            "subtopics": [{
                "id": "sub1", "title": "Sub", "status": "active",
                "cloud_sync": {"default": {
                    "scan_dir": "data/cloud/DEMO", "output_json": FILEMAP,
                    "output_md": "memory/cloud/topics/demo/filemap.md", "output_dir": "memory/cloud/topics/demo",
                }},
            }],
        }]), encoding="utf-8")
        self.write_filemap(build_filemap_text(5))
        exit_code, stdout, stderr = self.run_main(
            "--topic-id", "demo", "--subtopic-id", "sub1", "--workspace-root", str(self.root),
            "--only-file", SOURCE, "--json",
        )
        self.assertEqual(0, exit_code, stderr)
        self.assertEqual("created", self.envelope(stdout)["data"]["only_file"]["action"])
        self.assert_entry_consistent()

    def test_up_to_date_doc_does_not_need_a_converter(self):
        self.assertEqual(0, self.only_file()[0])
        before = snapshot(self.root)
        os.environ["CLOUD_ATLAS_DOC_CONVERTER_MOCK"] = "none"
        exit_code, stdout, stderr = self.only_file()
        self.assertEqual(0, exit_code, stderr)
        result = self.envelope(stdout)
        self.assertEqual("Completed", result["state"])
        self.assertEqual("unchanged", result["data"]["only_file"]["action"])
        self.assertEqual(before, snapshot(self.root))

    def test_valid_short_office_document_is_idempotent(self):
        write_minimal_docx(self.cloud / "kurz.docx", "Hi")
        source = "data/cloud/DEMO/kurz.docx"
        self.assertEqual(0, self.only_file(source)[0])
        before = snapshot(self.root)
        mirror_mtime = self.mirror_path(source).stat().st_mtime_ns
        exit_code, stdout, stderr = self.only_file(source)
        self.assertEqual(0, exit_code, stderr)
        self.assertEqual("unchanged", self.envelope(stdout)["data"]["only_file"]["action"])
        self.assertEqual(before, snapshot(self.root))
        self.assertEqual(mirror_mtime, self.mirror_path(source).stat().st_mtime_ns)

    def test_index_and_readme_sources_are_legitimate(self):
        for name in ("index", "readme"):
            shutil.copyfile(FIXTURE_PDF, self.cloud / f"{name}.pdf")
            exit_code, _stdout, stderr = self.only_file(f"data/cloud/DEMO/{name}.pdf", "--ocr-policy", "disabled")
            self.assertEqual(0, exit_code, stderr)
            self.assert_entry_consistent(f"data/cloud/DEMO/{name}.pdf")

    def test_a_version_root_link_cannot_redirect_writes(self):
        outside = self.root / "outside"
        outside.mkdir()
        if not make_directory_link(self.output / "_versions", outside):
            self.skipTest("cannot create a directory link")
        before = snapshot(self.root)
        exit_code, stdout, stderr = self.only_file()
        self.assertNotEqual(0, exit_code)
        self.assertEqual(before, snapshot(self.root))
        self.assertFalse(any(outside.iterdir()))


class CrossModeAndContractTests(OnlyFileTestCase):
    def setUp(self):
        super().setUp()
        self.write_filemap()

    def full_scan(self):
        return self.run_main(
            "--topic-id", "demo", "--workspace-root", str(self.root), "--json",
        )

    def test_pdf_local_derivative_without_ocr_succeeds(self):
        shutil.copyfile(FIXTURE_PDF, self.cloud / "form.pdf")
        source = "data/cloud/DEMO/form.pdf"
        exit_code, stdout, stderr = self.only_file(source, "--ocr-policy", "local_derivative")
        self.assertEqual(0, exit_code, stderr)
        entry = self.entry(source)
        self.assertNotIn("derivative", entry)
        self.assert_entry_consistent(source)

    def test_the_locks_live_in_the_workspace_data_zone(self):
        expected = str(self.root / "data" / ".locks" / "cloud-atlas")
        specs = self.storage_lock_specs()
        self.assertEqual(2, len([path for path, shared in specs if not shared]))
        self.assertTrue([path for path, shared in specs if shared], "ancestors are locked shared")
        for path in self.storage_lock_paths():
            self.assertTrue(os.path.normcase(path).startswith(os.path.normcase(expected)), path)

    def test_recall_on_open_and_stat_errors_are_placeholders(self):
        from core import artifact_refs

        class _RecallOnOpen:
            st_file_attributes = 0x40000

        with mock.patch("core.artifact_refs.os.stat", return_value=_RecallOnOpen()):
            self.assertTrue(artifact_refs.is_cloud_placeholder(str(self.source_path)))
        with mock.patch("core.artifact_refs.os.stat", side_effect=OSError("denied")):
            self.assertTrue(artifact_refs.is_cloud_placeholder(str(self.source_path)))
        self.assertFalse(artifact_refs.is_cloud_placeholder(str(self.root / "missing.bin")))

    def test_the_target_filemap_is_rechecked_right_before_it_is_read(self):
        def placeholder(path):
            return os.path.normcase(str(path)) == os.path.normcase(str(self.filemap_path))

        with mock.patch.object(MODULE, "is_cloud_placeholder", side_effect=placeholder):
            with self.assertRaises(OSError):
                MODULE._read_only_file_entry(str(self.filemap_path), SOURCE)

    def test_the_cas_rechecks_the_filemap_placeholder_status(self):
        text = self.read_text()
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()

        with mock.patch("core.artifact_refs.is_cloud_placeholder", return_value=True):
            with self.assertRaises(MODULE.FilemapError):
                MODULE.write_text_atomic_cas(
                    str(self.filemap_path), text, expected_sha256=digest,
                )

    def test_every_source_access_rechecks_the_placeholder_status(self):
        accessors = [
            (MODULE.convert_to_markdown_raw, (str(self.source_path),)),
            (MODULE.extract_pdf_form_fields, (str(self.source_path),)),
            (MODULE.extract_pdf_form_field_values, (str(self.source_path),)),
            (MODULE.analyze_pdf_structure, (str(self.source_path),)),
            (MODULE.run_ocr_on_pdf, (str(self.source_path),)),
        ]
        for accessor, arguments in accessors:
            with self.subTest(accessor=accessor.__name__):
                with mock.patch.object(MODULE, "is_cloud_placeholder", return_value=True):
                    with self.assertRaises(OSError):
                        accessor(*arguments)

    def test_a_status_change_between_two_accesses_is_caught(self):
        calls = {"count": 0}

        def flaky(path):
            if os.path.normcase(str(path)) != os.path.normcase(str(self.source_path)):
                return False
            calls["count"] += 1
            return calls["count"] > 1

        with mock.patch.object(MODULE, "is_cloud_placeholder", side_effect=flaky):
            MODULE.require_local_path(str(self.source_path), "source")
            with self.assertRaises(OSError):
                MODULE.convert_to_markdown_raw(str(self.source_path))

    def test_a_journal_path_claimed_by_the_target_filemap_fails_closed(self):
        journal_rel = (
            "memory/cloud/topics/demo/.only-file-journal/"
            f"{journal_key(filemap_merge.source_fold(SOURCE))}.json"
        )
        data = json.loads(self.read_text())
        data["files"]["data/cloud/DEMO/zzz.pdf"] = {
            "version": "N/A", "mtime": "2026-01-01 00:00:00", "size": "1.0 KB",
            "sha256": "d" * 64, "description": "claims the journal path",
            "markdown_mirror": journal_rel,
            "conversion_status": "converted",
        }
        before = self.write_filemap(json.dumps(data))

        exit_code, stdout, stderr = self.only_file()

        self.assertNotEqual(0, exit_code)
        envelope = self.envelope(stdout)
        self.assertEqual("journal", envelope["error"]["phase"])
        self.assertIn("claimed", envelope["error"]["message"])
        self.assertFalse(self.journal_file().exists(), "the journal must not be written")
        self.assertEqual(before, self.read_text())
        self.assertIsNone(self.entry())

    def test_a_windows_alias_of_the_journal_path_is_claimed_too(self):
        key = journal_key(filemap_merge.source_fold(SOURCE))
        for alias in (f".only-file-journal./{key}.json", f".only-file-journal /{key}.json"):
            with self.subTest(alias=alias):
                self.tearDown()
                self.setUp()
                data = json.loads(self.read_text())
                data["files"]["data/cloud/DEMO/zzz.pdf"] = {
                    "version": "N/A", "mtime": "2026-01-01 00:00:00", "size": "1.0 KB",
                    "sha256": "d" * 64, "description": "aliased journal claim",
                    "markdown_mirror": f"memory/cloud/topics/demo/{alias}",
                    "conversion_status": "converted",
                }
                before = self.write_filemap(json.dumps(data))

                exit_code, stdout, stderr = self.only_file()

                self.assertNotEqual(0, exit_code)
                self.assertEqual("journal", self.envelope(stdout)["error"]["phase"])
                self.assertFalse(self.journal_file().exists(), "the journal must not be written")
                self.assertEqual(before, self.read_text())

    def test_the_own_source_entry_claiming_the_journal_path_fails_closed(self):
        key = journal_key(filemap_merge.source_fold(SOURCE))
        journal_rel = f"memory/cloud/topics/demo/.only-file-journal/{key}.json"
        data = json.loads(self.read_text())
        data["files"][SOURCE] = {
            "version": "N/A", "mtime": "2026-01-01 00:00:00", "size": "1.0 KB",
            "sha256": "d" * 64, "description": "own entry claims the journal",
            "markdown_mirror": journal_rel,
            "conversion_status": "converted",
        }
        before = self.write_filemap(json.dumps(data))

        exit_code, stdout, stderr = self.only_file()

        self.assertNotEqual(0, exit_code)
        envelope = self.envelope(stdout)
        self.assertEqual("journal", envelope["error"]["phase"])
        self.assertIn("claimed", envelope["error"]["message"])
        self.assertFalse(self.journal_file().exists())
        self.assertEqual(before, self.read_text())

    def test_the_doc_short_staging_stays_in_the_short_data_zone_root(self):
        staging_root = self.root / "data" / "ca-doc" / "probe2"
        version_dir = self.output / "_versions" / "doc-probe"
        version_dir.mkdir(parents=True, exist_ok=True)
        failure = mock.Mock(returncode=1, stdout=b"", stderr=b"libreoffice failed")

        with mock.patch.object(MODULE, "find_soffice_binary", return_value="soffice"), \
             mock.patch.object(MODULE.subprocess, "run", return_value=failure), \
             mock.patch.object(MODULE.shutil, "rmtree", side_effect=AssertionError("rmtree")):
            ok, message = MODULE.convert_doc_with_short_staging(
                str(self.source_path), str(version_dir), "libreoffice-headless",
                timeout=5, keep_staging=True, staging_root=str(staging_root),
            )

        self.assertFalse(ok)
        self.assertIn("LibreOffice failed", message)
        self.assertTrue(
            (staging_root / "source.doc").is_file(),
            "the working copy of the cloud source stays in the short Data-Zone root",
        )
        self.assertTrue(
            MODULE.normalized_local_path(str(staging_root)).startswith(
                MODULE.normalized_local_path(str(self.root / "data")) + os.sep
            )
        )
        self.assertEqual([], list(version_dir.iterdir()), "nothing is written next to the version")

    def test_the_direct_libreoffice_profile_uri_is_short_and_in_the_data_zone(self):
        staging_root = self.root / "data" / "ca-doc" / "probe1"
        version_dir = self.output / "_versions" / "doc-direct"
        version_dir.mkdir(parents=True, exist_ok=True)
        failure = mock.Mock(returncode=1, stdout=b"", stderr=b"libreoffice failed")
        run_mock = mock.Mock(return_value=failure)

        with mock.patch.object(MODULE, "get_doc_converter", return_value="libreoffice-headless"), \
             mock.patch.object(MODULE, "requires_short_external_converter_paths", return_value=False), \
             mock.patch.object(MODULE, "find_soffice_binary", return_value="soffice"), \
             mock.patch.object(MODULE.subprocess, "run", run_mock), \
             mock.patch.object(MODULE.shutil, "rmtree", side_effect=AssertionError("rmtree")), \
             mock.patch.object(MODULE, "convert_doc_with_short_staging",
                               side_effect=AssertionError("short staging")), \
             mock.patch.object(MODULE.tempfile, "mkdtemp", side_effect=AssertionError("mkdtemp")):
            ok, message, converter = MODULE.convert_doc_file(
                str(self.source_path), str(version_dir),
                timeout=5, keep_staging=True, staging_root=str(staging_root),
            )

        self.assertFalse(ok)
        self.assertIn("LibreOffice failed", message)
        self.assertEqual("libreoffice-headless", converter)
        command = run_mock.call_args[0][0]
        uri = next(arg.split("=", 1)[1] for arg in command if arg.startswith("-env:UserInstallation="))
        self.assertTrue(uri.startswith("file:///"), uri)
        self.assertNotIn("?", uri, "no extended-length prefix in the profile URI")
        self.assertNotIn("\\", uri, "no backslashes in the profile URI")
        self.assertLessEqual(len(uri), MODULE.DOC_PROFILE_URI_LIMIT, uri)
        self.assertTrue(
            MODULE.normalized_local_path(str(staging_root)).startswith(
                MODULE.normalized_local_path(str(self.root / "data")) + os.sep
            )
        )
        self.assertTrue(staging_root.is_dir(), "the profile root is the short Data-Zone run directory")

    def test_a_full_only_file_doc_run_with_a_long_output_path(self):
        if os.name != "nt":
            self.skipTest("the planned-path threshold only takes effect on Windows")
        topic = "long-" + "a" * 24
        output_rel = f"memory/cloud/topics/{topic}"
        # Choose a source basename long enough that the *planned* DOCX derivative
        # path actually crosses EXTERNAL_CONVERTER_PATH_LIMIT on this host.
        padding = 0
        while True:
            stem = "g_neu_" + "x" * padding
            planned = (
                self.root / output_rel / "_versions" / ("0" * 16)
                / f"{'0' * 22}-999999-ffffffff" / f"{stem}.docx"
            )
            if len(str(planned)) >= MODULE.EXTERNAL_CONVERTER_PATH_LIMIT:
                break
            padding += MODULE.EXTERNAL_CONVERTER_PATH_LIMIT - len(str(planned)) + 10
        source_rel, filemap, _ = self.prepare_synthetic_doc_topic(topic, stem)

        exit_code, stdout, stderr = self.run_main(
            "--topic-id", topic, "--workspace-root", str(self.root),
            "--only-file", source_rel, "--json",
        )

        self.assertEqual(0, exit_code, stderr[-2500:])
        envelope = self.envelope(stdout)
        self.assertTrue(envelope["success"], envelope)
        self.assertEqual("Completed", envelope["state"])
        only = envelope["data"]["only_file"]
        self.assertEqual(source_rel, only["source"])
        self.assertEqual("created", only["action"])
        storage = envelope["data"]["storages"][0]
        self.assertTrue(storage["success"], storage)
        self.assertEqual(1, storage["counts"]["converted"])

        entry = json.loads(filemap.read_text(encoding="utf-8"))["files"][source_rel]
        derivative_abs = self.root / entry["derivative"]["path"]
        self.assertGreaterEqual(
            len(str(derivative_abs)), MODULE.EXTERNAL_CONVERTER_PATH_LIMIT,
            "the test data must cross the Windows short-path threshold",
        )
        self.assertTrue(MODULE.requires_short_external_converter_paths(str(derivative_abs)))

        self.assertEqual("converted", entry["conversion_status"])
        self.assertEqual("docx", entry["derivative"]["format"])
        self.assertEqual("libreoffice-headless", entry["derivative"]["conversion_method"])
        self.assertEqual("libreoffice-headless", entry["artifact_metadata"]["converter"])
        self.assertEqual(
            MODULE.calculate_sha256(MODULE.get_safe_path(str(derivative_abs))),
            entry["derivative"]["sha256"],
        )
        self.assertEqual(entry["markdown_mirror"], only["mirror"])
        self.assertTrue(
            entry["markdown_mirror"].startswith(only["version"] + "/"),
            entry["markdown_mirror"],
        )

        # The short-path branch executed: only it copies the source into the short
        # Data-Zone run directory as source.doc (and converts that copy to
        # source.docx).  The direct path never creates either file.
        staging = self.single_doc_staging_dir()
        self.assertTrue(
            (staging / "source.doc").is_file(),
            "the short-path branch must copy the cloud source into the staging directory",
        )
        self.assertTrue((staging / "source.docx").is_file(), "the staged derivative stays in the Data Zone")
        self.assertTrue((staging / "user").is_dir(), "the kept LibreOffice profile stays in the Data Zone")

        self.assert_readable_docx(derivative_abs)
        mirror_text = self.assert_doc_mirror_content(
            self.root / entry["markdown_mirror"], *DOC_FIXTURE_LINES,
        )
        self.assertIn('converter: "libreoffice-headless"', mirror_text)

    def test_a_full_only_file_doc_run_uses_the_direct_path_for_a_short_output_path(self):
        source_rel, filemap, _ = self.prepare_synthetic_doc_topic("direct", "g_neu")

        exit_code, stdout, stderr = self.run_main(
            "--topic-id", "direct", "--workspace-root", str(self.root),
            "--only-file", source_rel, "--json",
        )

        self.assertEqual(0, exit_code, stderr[-2500:])
        envelope = self.envelope(stdout)
        self.assertTrue(envelope["success"], envelope)
        entry = json.loads(filemap.read_text(encoding="utf-8"))["files"][source_rel]
        derivative_abs = self.root / entry["derivative"]["path"]
        self.assertFalse(MODULE.requires_short_external_converter_paths(str(derivative_abs)))
        if os.name == "nt":
            self.assertLess(
                len(str(derivative_abs)), MODULE.EXTERNAL_CONVERTER_PATH_LIMIT,
                "the direct path precondition is below the Windows threshold",
            )
        self.assertEqual("converted", entry["conversion_status"])
        self.assertEqual("libreoffice-headless", entry["derivative"]["conversion_method"])

        # The direct path passes the cloud source to LibreOffice: the short run
        # directory holds only the profile, never a source.doc copy.
        staging = self.single_doc_staging_dir()
        self.assertFalse(
            (staging / "source.doc").exists(),
            "the direct path must not copy the cloud source into the staging directory",
        )
        self.assertFalse((staging / "source.docx").exists())
        self.assertTrue((staging / "user").is_dir(), "the kept profile still lives in the Data Zone")

        self.assert_readable_docx(derivative_abs)
        self.assert_doc_mirror_content(self.root / entry["markdown_mirror"], *DOC_FIXTURE_LINES)

    def test_requires_short_external_converter_paths_is_windows_only_and_threshold_based(self):
        long_path = str(Path(tempfile.gettempdir()) / ("x" * (MODULE.EXTERNAL_CONVERTER_PATH_LIMIT + 10)))
        short_path = str(Path(tempfile.gettempdir()) / "a.docx")

        with mock.patch.object(MODULE.os, "name", "posix"):
            self.assertFalse(MODULE.requires_short_external_converter_paths(long_path))
        with mock.patch.object(MODULE.os, "name", "nt"):
            self.assertTrue(MODULE.requires_short_external_converter_paths(long_path))
            self.assertFalse(MODULE.requires_short_external_converter_paths(short_path))

    def test_a_path_length_error_retries_through_short_staging_below_the_threshold(self):
        """The retry is OS-independent and not limited to planned paths above 240.

        On POSIX the planned-path precheck is always False, so the detected
        LibreOffice path-length error is the only way to enter the short staging.
        """
        out_dir = self.output / "_versions" / "retry"
        out_dir.mkdir(parents=True, exist_ok=True)
        final = out_dir / "g_neu.docx"
        final.write_bytes(b"DOCX")
        calls = []

        def direct(*args, **kwargs):
            calls.append("direct")
            return False, "LibreOffice failed (code 1): file name too long"

        def short(*args, **kwargs):
            calls.append("short")
            return True, str(final)

        with mock.patch.object(MODULE, "get_doc_converter", return_value="libreoffice-headless"), \
             mock.patch.object(MODULE, "requires_short_external_converter_paths", return_value=False), \
             mock.patch.object(MODULE, "convert_doc_to_docx_libreoffice", side_effect=direct), \
             mock.patch.object(MODULE, "convert_doc_with_short_staging", side_effect=short):
            ok, result, converter = MODULE.convert_doc_file(
                str(self.source_path), str(out_dir), timeout=5,
            )

        self.assertTrue(ok, result)
        self.assertEqual(str(final), result)
        self.assertEqual("libreoffice-headless", converter)
        self.assertEqual(["direct", "short"], calls)

    def test_the_placeholder_makes_the_content_assertion_fail(self):
        """An extraction exception must not look like a successful content transfer.

        The worker writes DOC_MIRROR_PLACEHOLDER when the DOCX extraction raises.
        This injects exactly that exception and proves the strengthened success
        assertion rejects the resulting mirror.
        """
        derivative = self.root / "data" / "ca-doc" / "probe.docx"
        derivative.parent.mkdir(parents=True, exist_ok=True)
        write_minimal_docx(derivative, "Synthetischer Test")
        task = {
            "src_abs": str(self.source_path),
            "src_sha256": SHA,
            "derivative_abs": str(self.root / "memory" / "cloud" / "topics" / "demo"
                                  / "_versions" / "v" / "g_neu.docx"),
            "derivative_rel": "memory/cloud/topics/demo/_versions/v/g_neu.docx",
            "is_doc": True,
            "file_timeout": 5,
        }
        connection = mock.Mock()

        with mock.patch.object(
            MODULE, "convert_doc_file",
            return_value=(True, str(derivative), "libreoffice-headless"),
        ), mock.patch.object(
            MODULE, "convert_to_markdown_raw",
            side_effect=RuntimeError("extraction exploded"),
        ):
            MODULE._convert_worker_target(task, connection)

        kind, payload = connection.send.call_args[0][0]
        self.assertEqual("ok", kind)
        self.assertEqual("libreoffice-headless", payload["conversion_method"])
        self.assertIn(DOC_MIRROR_PLACEHOLDER, payload["text"])
        mirror = self.root / "placeholder-mirror.md"
        mirror.write_text(f"---\n---\n\n{payload['text']}", encoding="utf-8")
        with self.assertRaises(AssertionError):
            self.assert_doc_mirror_content(mirror, *DOC_FIXTURE_LINES)

    def test_the_libreoffice_profile_uri_is_a_plain_file_uri(self):
        directory = Path(tempfile.mkdtemp(prefix="uri probe "))
        self.addCleanup(shutil.rmtree, directory, True)

        uri = MODULE.path_to_file_uri(str(directory))

        self.assertTrue(uri.startswith("file:///"), uri)
        self.assertNotIn("?", uri, "no extended-length prefix in the URI")
        self.assertNotIn("\\", uri, "no backslashes in the URI")
        self.assertIn("%20", uri, "spaces are percent-encoded")
        self.assertTrue(uri.endswith(directory.name.replace(" ", "%20")), uri)

    def test_the_extended_length_prefix_is_stripped_on_windows_only(self):
        if os.name != "nt":
            self.skipTest("the extended-length prefix is Windows-specific")
        directory = Path(tempfile.mkdtemp(prefix="uri probe "))
        self.addCleanup(shutil.rmtree, directory, True)

        uri = MODULE.path_to_file_uri(str(directory))

        self.assertEqual(uri, MODULE.path_to_file_uri("\\\\?\\" + str(directory)))
        self.assertNotIn("?", uri, "no extended-length prefix in the URI")

    def test_posix_backslashes_in_a_filename_are_not_separators(self):
        if os.name == "nt":
            self.skipTest("a backslash cannot be part of a Windows file name")
        directory = Path(tempfile.mkdtemp(prefix="uri backslash "))
        self.addCleanup(shutil.rmtree, directory, True)
        literal = directory / "a\\b"
        literal.write_text("x", encoding="utf-8")

        uri = MODULE.path_to_file_uri(str(literal))

        self.assertTrue(uri.endswith("a%5Cb"), uri)
        self.assertNotIn("/a/b", uri)

    def test_separator_normalization_in_the_uri_depends_on_the_os(self):
        with mock.patch.object(MODULE.os.path, "abspath", return_value="/tmp/a\\b"):
            with mock.patch.object(MODULE.os, "name", "posix"):
                self.assertEqual("file:///tmp/a%5Cb", MODULE.path_to_file_uri("ignored"))
            with mock.patch.object(MODULE.os, "name", "nt"):
                self.assertEqual("file:///tmp/a/b", MODULE.path_to_file_uri("ignored"))

    def test_the_unc_profile_uri_uses_the_server_form(self):
        if os.name != "nt":
            self.skipTest("UNC paths are Windows-specific")

        self.assertEqual(
            "file://server/share/a%20b",
            MODULE.path_to_file_uri("\\\\server\\share\\a b"),
        )
        self.assertEqual(
            "file://server/share/a%20b",
            MODULE.path_to_file_uri("\\\\?\\UNC\\server\\share\\a b"),
        )

    def test_a_real_libreoffice_conversion_with_the_kept_profile(self):
        if MODULE.find_soffice_binary() is None:
            self.skipTest("no local LibreOffice available")
        staging = self.root / "data" / "ca-doc" / "lo-probe"
        staging.mkdir(parents=True, exist_ok=True)
        source = staging / "probe.rtf"
        source.write_bytes(b"{\\rtf1\\ansi\\deff0 Synthetischer Test}")
        started = time.time()

        ok, message = MODULE.convert_doc_to_docx_libreoffice(
            str(source), str(staging), timeout=90, keep_profile=True,
        )

        self.assertTrue(ok, message)
        self.assertLess(time.time() - started, 85, "LibreOffice must not hang on the profile URI")
        self.assertTrue((staging / "probe.docx").is_file(), "the derivative is produced")
        self.assertTrue((staging / "user").is_dir(), "the kept LibreOffice profile lives here")
        self.assertLessEqual(len(MODULE.path_to_file_uri(str(staging))), MODULE.DOC_PROFILE_URI_LIMIT)

    def test_a_successful_ocr_derivative_needs_no_replace(self):
        shutil.copyfile(FIXTURE_PDF, self.cloud / "scan.pdf")
        target_dir = self.output / "_versions" / "ocr-ok"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "scan.pdf"
        os.environ["CLOUD_ATLAS_OCR_MOCK"] = "1"

        try:
            with mock.patch.object(MODULE.os, "replace", side_effect=AssertionError("replace")):
                ok, message = MODULE.run_ocr_on_pdf(
                    str(self.cloud / "scan.pdf"), str(target), keep_staging=True,
                )
        finally:
            os.environ.pop("CLOUD_ATLAS_OCR_MOCK", None)

        self.assertTrue(ok, message)
        self.assertTrue(target.is_file(), "the derivative is written in place")

    def test_the_version_is_written_without_a_directory_rename(self):
        with mock.patch.object(MODULE.os, "rename", side_effect=AssertionError("rename")):
            exit_code, stdout, stderr = self.only_file()

        self.assertEqual(0, exit_code, stderr)
        self.assert_entry_consistent()
        self.assertTrue(self.version_files(), "the artifacts are written directly into their version")

    def test_a_failed_ocr_in_the_only_file_mode_never_deletes_its_output(self):
        shutil.copyfile(FIXTURE_PDF, self.cloud / "scan.pdf")
        target_dir = self.output / "_versions" / "ocr-probe"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "scan.pdf"
        os.environ["CLOUD_ATLAS_OCR_MOCK"] = "1"

        try:
            with mock.patch.object(MODULE.os, "unlink", side_effect=AssertionError("unlink")):
                ok, message = MODULE.run_ocr_on_pdf(
                    str(self.cloud / "scan.pdf"), str(target),
                    expected_src_sha256="0" * 64, keep_staging=True,
                )
        finally:
            os.environ.pop("CLOUD_ATLAS_OCR_MOCK", None)

        self.assertFalse(ok)
        self.assertIn("changed", message)
        self.assertTrue(target.is_file(), "the written derivative stays in place")
        self.assertEqual([], [p for p in target_dir.iterdir() if p.name.startswith(".ocr-stage-")])

    def test_a_failed_ocr_still_cleans_up_outside_the_only_file_mode(self):
        shutil.copyfile(FIXTURE_PDF, self.cloud / "scan.pdf")
        target_dir = self.output / "_versions" / "ocr-probe"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "scan.pdf"
        os.environ["CLOUD_ATLAS_OCR_MOCK"] = "1"

        try:
            ok, _message = MODULE.run_ocr_on_pdf(
                str(self.cloud / "scan.pdf"), str(target),
                expected_src_sha256="0" * 64,
            )
        finally:
            os.environ.pop("CLOUD_ATLAS_OCR_MOCK", None)

        self.assertFalse(ok)
        self.assertFalse(target.exists(), "an unvalidated derivative is never promoted")
        self.assertEqual(
            [], [p for p in target_dir.iterdir() if p.name.startswith(".ocr-stage-")],
            "the full-scan behaviour is unchanged",
        )

    def test_a_leftover_commit_temp_is_not_deleted_or_moved(self):
        self.assertEqual(0, self.only_file()[0])
        leftover = self.journal_dir() / "leftover.filemap.tmp"
        leftover.parent.mkdir(parents=True, exist_ok=True)
        leftover.write_text("LEFTOVER", encoding="utf-8")
        self.assertEqual(0, self.only_file()[0])
        self.assertTrue(leftover.is_file())
        self.assertEqual("LEFTOVER", leftover.read_text(encoding="utf-8"))

    def test_only_file_writes_stay_inside_its_own_version_root(self):
        nested_filemap = self.configure_nested_storage()
        self.write_filemap()
        exit_code, stdout, stderr = self.run_main(
            "--topic-id", "demo", "--workspace-root", str(self.root),
            "--storage-id", "sub", "--only-file", self.NESTED_SOURCE, "--json",
        )
        self.assertEqual(0, exit_code, stderr)
        entry = json.loads(nested_filemap.read_text(encoding="utf-8"))["files"][self.NESTED_SOURCE]
        self.assertTrue(
            entry["markdown_mirror"].startswith("memory/cloud/topics/demo/sub/_versions/"),
            entry["markdown_mirror"],
        )
        self.assertFalse(
            (self.output / "_versions").exists(),
            "a nested run must not write into the parent's version root",
        )

    def test_a_source_placeholder_is_rejected_without_hydration(self):
        with mock.patch.object(MODULE, "is_cloud_placeholder", return_value=True), \
             mock.patch.object(MODULE, "calculate_sha256", side_effect=AssertionError("source opened")):
            exit_code, stdout, stderr = self.only_file()

        self.assertNotEqual(0, exit_code)
        envelope = self.envelope(stdout)
        self.assertEqual("only-file", envelope["error"]["phase"])
        self.assertIn("placeholder", envelope["error"]["message"])

    def test_a_filemap_placeholder_is_rejected_without_hydration(self):
        def placeholder(path):
            return os.path.normcase(str(path)) == os.path.normcase(str(self.filemap_path))

        with mock.patch.object(MODULE, "is_cloud_placeholder", side_effect=placeholder):
            exit_code, stdout, stderr = self.only_file()

        self.assertNotEqual(0, exit_code)
        envelope = self.envelope(stdout)
        self.assertEqual("filemap", envelope["error"]["phase"])
        self.assertIn("placeholder", envelope["error"]["message"])

    def test_a_broken_target_filemap_reports_journal_and_retained_version(self):
        result = self.run_cli(extra_env={"CLOUD_ATLAS_ONLY_FILE_CRASH_AFTER": "temp"})
        self.assertEqual(137, result.returncode, result.stderr[-1500:])
        self.filemap_path.write_text('{"files": {', encoding="utf-8")

        exit_code, stdout, stderr = self.only_file()

        self.assertNotEqual(0, exit_code)
        envelope = self.envelope(stdout)
        self.assertEqual("filemap", envelope["error"]["phase"])
        only = envelope["data"]["only_file"]
        self.assertTrue(only["retained_version"], only)
        self.assertTrue(only["journal"], only)
        self.assertTrue((self.root / only["retained_version"]).exists())

    def test_a_failed_commit_leaves_its_temp_file(self):
        self.write_filemap()
        text = self.read_text()
        temp = self.journal_dir() / "commit.filemap.tmp"
        temp.parent.mkdir(parents=True, exist_ok=True)

        with self.assertRaises(RuntimeError):
            MODULE.write_text_atomic_cas(
                str(self.filemap_path), text,
                expected_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                safe_path=lambda value: value, tmp_path=str(temp),
                before_replace=lambda: (_ for _ in ()).throw(RuntimeError("stop")),
            )

        self.assertTrue(temp.is_file(), "a failed commit must not unlink its temp file")

class ModuleLevelTests(unittest.TestCase):
    def test_schema_uri_matches_the_generator(self):
        import gen_filemap
        self.assertEqual(gen_filemap.FILEMAP_SCHEMA_URI, filemap_merge.FILEMAP_SCHEMA_URI)


if __name__ == "__main__":
    unittest.main()
