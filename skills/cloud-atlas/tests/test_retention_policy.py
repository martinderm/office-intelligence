"""Retention and logical quarantine behavior for versioned targets (Issue #97).

The audit is read-only: every test compares the workspace before and after and
the policy never authorizes a delete or move.  The tests cover the documented
count/age boundaries, unexpected layout, links, journals and foreign references.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from core import retention  # noqa: E402
import retention_audit  # noqa: E402

OUTPUT = "memory/cloud/topics/demo"
FILEMAP = f"{OUTPUT}/filemap.json"
VERSION_ROOT = f"{OUTPUT}/_versions"
KEY = "0123456789abcdef"
SOURCE = "data/cloud/DEMO/a.doc"
NOW = datetime(2026, 11, 11, 12, 0, 0, tzinfo=timezone.utc)
SHA_A = "a" * 64
SHA_B = "b" * 64


def make_directory_link(link, target):
    """Symlink, or a Windows junction when symlinks are not permitted."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
        )
        return completed.returncode == 0
    return False


def snapshot(root):
    """Every file hash and directory marker; the audit must not change any."""
    result = {}
    for directory, dirs, files in os.walk(root):
        rel_dir = Path(directory).relative_to(root).as_posix()
        for name in dirs:
            result["dir:" + (Path(rel_dir) / name).as_posix()] = "dir"
        for name in files:
            path = Path(directory) / name
            result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


class RetentionTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        (self.root / ".workspace-root").touch()
        (self.root / OUTPUT).mkdir(parents=True)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def write_catalog(self, *, output_json=FILEMAP, output_dir=OUTPUT):
        catalog = self.root / "memory" / "references" / "topics"
        catalog.mkdir(parents=True, exist_ok=True)
        (catalog / "topics.json").write_text(json.dumps([{
            "id": "demo",
            "title": "Demo",
            "cloud_sync": {
                "default": {
                    "scan_dir": "data/cloud/DEMO",
                    "output_json": output_json,
                    "output_md": f"{OUTPUT}/filemap.md",
                    "output_dir": output_dir,
                },
            },
        }]), encoding="utf-8")

    def write_filemap(self, *, mirror=None, derivative=None, source=SOURCE, extra=None):
        entry = {
            "version": "N/A",
            "mtime": "2026-01-01 00:00:00",
            "size": "1 KB",
            "sha256": SHA_A,
            "description": "-",
            "conversion_status": "converted" if mirror else "conversion_required",
        }
        if mirror:
            entry["markdown_mirror"] = mirror
        if derivative:
            entry["derivative"] = {
                "path": derivative, "sha256": SHA_B, "format": "docx",
            }
        files = {source: entry}
        files.update(extra or {})
        (self.root / FILEMAP).write_text(
            json.dumps({"output_dir": OUTPUT, "files": files}, ensure_ascii=False),
            encoding="utf-8",
        )

    def make_version(self, run, age_days, *, key=KEY, files=None):
        path = self.root / VERSION_ROOT / key / run
        path.mkdir(parents=True, exist_ok=True)
        if files is None:
            files = {"a.md": b"mirror"}
        for name, data in files.items():
            target = path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        timestamp = NOW.timestamp() - age_days * 86400
        os.utime(path, (timestamp, timestamp))
        return path

    def audit(self, **kwargs):
        return retention.classify_versions(
            str(self.root), OUTPUT, FILEMAP, now=NOW, **kwargs,
        )

    def entry(self, report, run, key=KEY):
        for entry in report["entries"]:
            if entry["path"] == f"{VERSION_ROOT}/{key}/{run}":
                return entry
        self.fail(f"missing entry for {run}: {report['entries']}")

    def run_cli(self, *extra):
        command = [
            sys.executable, str(SCRIPTS_DIR / "retention_audit.py"),
            "--workspace-root", str(self.root),
            "--output-dir", OUTPUT,
            "--filemap", FILEMAP,
            "--now", NOW.isoformat(),
            "--json",
            *extra,
        ]
        return subprocess.run(command, capture_output=True, text=True, timeout=60)

    def run_cli_human(self):
        command = [
            sys.executable, str(SCRIPTS_DIR / "retention_audit.py"),
            "--workspace-root", str(self.root),
            "--output-dir", OUTPUT,
            "--filemap", FILEMAP,
            "--now", NOW.isoformat(),
        ]
        return subprocess.run(command, capture_output=True, text=True, timeout=60)

    # -- active protection -------------------------------------------------
    def test_active_version_is_active_even_when_absolutely_old(self):
        path = self.make_version("active", 1000)
        self.write_filemap(mirror=f"{VERSION_ROOT}/{KEY}/active/a.md")
        report = self.audit()
        active = self.entry(report, "active")
        self.assertEqual("active", active["classification"])
        self.assertEqual(["filemap_reference"], active["reasons"])
        self.assertTrue(active["active"])
        self.assertTrue(path.is_dir())
        self.assertFalse(active["safe_to_delete"])
        self.assertFalse(active["safe_to_move"])
        self.assertEqual("none", active["action"])

    def test_derivative_reference_also_marks_the_version_active(self):
        self.make_version("active", 500)
        self.write_filemap(
            mirror=f"{VERSION_ROOT}/{KEY}/active/a.md",
            derivative=f"{VERSION_ROOT}/{KEY}/active/a.docx",
        )
        report = self.audit()
        self.assertEqual("active", self.entry(report, "active")["classification"])

    def test_windows_alias_and_case_insensitive_reference_marks_active(self):
        self.make_version("active", 1)
        self.write_filemap(
            mirror=f"{OUTPUT.upper()}/_versions./{KEY.upper()}/active./a.md",
        )
        with mock.patch.object(retention, "_aliases_enabled", return_value=True):
            report = self.audit()
        entry = self.entry(report, "active")
        self.assertEqual("active", entry["classification"])
        self.assertEqual(1, report["references"]["matched"])
        self.assertEqual([], report["references"]["unresolved"])

    def test_case_sensitive_platform_does_not_equate_case_variants(self):
        self.make_version("active", 1)
        self.write_filemap(mirror=f"{VERSION_ROOT}/{KEY}/Active/a.md")
        with mock.patch.object(retention, "_aliases_enabled", return_value=False):
            report = self.audit()
        entry = self.entry(report, "active")
        self.assertNotEqual("active", entry["classification"])
        self.assertEqual(0, report["references"]["matched"])
        self.assertEqual(
            [f"{VERSION_ROOT}/{KEY}/Active"], report["references"]["unresolved"],
        )

    def test_uppercase_source_key_is_recognized_and_can_be_active(self):
        upper = KEY.upper()
        self.make_version("active", 1, key=upper)
        self.write_filemap(mirror=f"{VERSION_ROOT}/{upper}/active/a.md")
        report = self.audit()
        entry = self.entry(report, "active", key=upper)
        self.assertEqual("active", entry["classification"])
        self.assertEqual(1, report["references"]["matched"])

    def test_filemap_output_dir_alias_is_accepted(self):
        self.make_version("active", 1)
        (self.root / FILEMAP).write_text(json.dumps({
            "output_dir": f"{OUTPUT.upper()}/",
            "files": {
                SOURCE: {
                    "markdown_mirror": f"{VERSION_ROOT}/{KEY}/active/a.md",
                },
            },
        }), encoding="utf-8")
        with mock.patch.object(retention, "_aliases_enabled", return_value=True):
            report = self.audit()
        self.assertEqual("active", self.entry(report, "active")["classification"])

    def test_a_foreign_version_without_this_filemap_reference_is_not_active(self):
        self.make_version("local", 10)
        self.write_filemap(mirror=f"memory/cloud/topics/other/_versions/{KEY}/run/a.md")
        report = self.audit()
        self.assertEqual("retained", self.entry(report, "local")["classification"])
        # The reference belongs to another output and is not an active version here.
        self.assertEqual(0, report["references"]["active_version_directories"])
        self.assertEqual(0, report["references"]["matched"])

    # -- documented count/age boundaries ----------------------------------
    def test_default_policy_retains_newest_two_and_the_grace_age(self):
        self.make_version("active", 1)
        self.make_version("new", 10)
        self.make_version("middle", 20)
        self.make_version("beyond-count-but-young", 30)
        self.write_filemap(mirror=f"{VERSION_ROOT}/{KEY}/active/a.md")
        report = self.audit()
        self.assertEqual("retained", self.entry(report, "new")["classification"])
        self.assertEqual(["within_retained_count"], self.entry(report, "new")["reasons"])
        self.assertEqual("retained", self.entry(report, "middle")["classification"])
        self.assertEqual(["within_retained_count"], self.entry(report, "middle")["reasons"])
        self.assertEqual(
            "retained", self.entry(report, "beyond-count-but-young")["classification"],
        )
        self.assertEqual(
            ["within_retention_grace_age"],
            self.entry(report, "beyond-count-but-young")["reasons"],
        )

    def test_beyond_count_and_grace_is_a_logical_candidate(self):
        self.make_version("active", 1)
        self.make_version("new", 10)
        self.make_version("middle", 20)
        self.make_version("old", 120)
        self.write_filemap(mirror=f"{VERSION_ROOT}/{KEY}/active/a.md")
        report = self.audit()
        old = self.entry(report, "old")
        self.assertEqual("quarantine_candidate", old["classification"])
        self.assertEqual(
            ["count_exceeded", "retention_grace_age_exceeded"], old["reasons"],
        )
        self.assertTrue(old["quarantine"]["logical"])
        self.assertFalse(old["quarantine"]["physical"])
        self.assertEqual("none", old["action"])
        self.assertFalse(old["safe_to_delete"])
        self.assertFalse(old["safe_to_move"])

    def test_absolute_age_never_overrides_the_active_reference(self):
        self.make_version("old", 400)
        self.write_filemap(mirror=f"{VERSION_ROOT}/{KEY}/old/a.md")
        # The active version is protected even when it is older than the
        # absolute quarantine age; only the filemap reference decides.
        report = self.audit()
        self.assertEqual("active", self.entry(report, "old")["classification"])

    def test_absolute_age_candidate_even_within_the_count(self):
        self.make_version("new", 10)
        self.make_version("old", 400)
        self.write_filemap(mirror=f"{VERSION_ROOT}/{KEY}/new/a.md")
        report = self.audit()
        old = self.entry(report, "old")
        self.assertEqual("quarantine_candidate", old["classification"])
        self.assertEqual(["absolute_age_exceeded"], old["reasons"])

    def test_exact_grace_age_is_still_retained(self):
        self.make_version("new", 10)
        self.make_version("middle", 20)
        self.make_version("second", 30)
        self.make_version("exactly-grace", 90)
        self.write_filemap(mirror=f"{VERSION_ROOT}/{KEY}/new/a.md")
        report = self.audit()
        entry = self.entry(report, "exactly-grace")
        self.assertEqual("retained", entry["classification"])
        self.assertEqual(["within_retention_grace_age"], entry["reasons"])

    def test_exact_absolute_age_is_not_a_candidate(self):
        self.make_version("active", 1)
        self.make_version("exactly-absolute", 365)
        self.write_filemap(mirror=f"{VERSION_ROOT}/{KEY}/active/a.md")
        report = self.audit()
        entry = self.entry(report, "exactly-absolute")
        self.assertEqual("retained", entry["classification"])
        self.assertEqual(["within_retained_count"], entry["reasons"])

    def test_grace_boundary_uses_exact_age_not_rounded_days(self):
        self.make_version("active", 1)
        self.make_version("new", 10)
        self.make_version("middle", 20)
        self.make_version("minus-one-second", 90 - 1 / 86400)
        self.make_version("plus-one-second", 90 + 1 / 86400)
        self.write_filemap(mirror=f"{VERSION_ROOT}/{KEY}/active/a.md")
        report = self.audit()
        minus = self.entry(report, "minus-one-second")
        plus = self.entry(report, "plus-one-second")
        self.assertEqual("retained", minus["classification"])
        self.assertEqual(["within_retention_grace_age"], minus["reasons"])
        self.assertEqual("quarantine_candidate", plus["classification"])
        self.assertEqual(
            ["count_exceeded", "retention_grace_age_exceeded"], plus["reasons"],
        )
        self.assertEqual(minus["age_days"], plus["age_days"])

    def test_absolute_boundary_uses_exact_age_not_rounded_days(self):
        self.make_version("active", 1)
        self.make_version("minus-one-second", 365 - 1 / 86400)
        self.make_version("plus-one-second", 365 + 1 / 86400)
        self.write_filemap(mirror=f"{VERSION_ROOT}/{KEY}/active/a.md")
        report = self.audit()
        minus = self.entry(report, "minus-one-second")
        plus = self.entry(report, "plus-one-second")
        self.assertEqual("retained", minus["classification"])
        self.assertEqual(["within_retained_count"], minus["reasons"])
        self.assertEqual("quarantine_candidate", plus["classification"])
        self.assertEqual(["absolute_age_exceeded"], plus["reasons"])
        self.assertEqual(minus["age_days"], plus["age_days"])

    def test_custom_policy_is_applied_consistently(self):
        self.make_version("new", 1)
        self.make_version("old", 2)
        self.write_filemap(mirror=f"{VERSION_ROOT}/{KEY}/new/a.md")
        policy = retention.RetentionPolicy(
            retained_inactive_versions=0,
            retained_inactive_age_days=0,
            quarantine_age_days=365,
        )
        report = self.audit(policy=policy)
        self.assertEqual("quarantine_candidate", self.entry(report, "old")["classification"])
        self.assertEqual(
            ["count_exceeded", "retention_grace_age_exceeded"],
            self.entry(report, "old")["reasons"],
        )

    # -- non-destructive and quarantine boundaries ------------------------
    def test_audit_never_writes_or_creates_the_reserved_area(self):
        self.make_version("active", 1)
        self.make_version("old", 120)
        self.write_filemap(mirror=f"{VERSION_ROOT}/{KEY}/active/a.md")
        before = snapshot(self.root)
        report = self.audit()
        self.assertEqual(before, snapshot(self.root))
        self.assertFalse((self.root / OUTPUT / retention.QUARANTINE_DIR).exists())
        self.assertFalse(report["reserved_area_present"])
        self.assertEqual(f"{OUTPUT}/{retention.QUARANTINE_DIR}", report["quarantine_area"])

    def test_existing_quarantine_area_is_only_reported(self):
        area = self.root / OUTPUT / retention.QUARANTINE_DIR
        area.mkdir()
        sentinel = area / "manual-sentinel.txt"
        sentinel.write_bytes(b"manual")
        self.make_version("active", 1)
        self.make_version("old", 400)
        self.write_filemap(mirror=f"{VERSION_ROOT}/{KEY}/active/a.md")
        before = snapshot(self.root)
        report = self.audit()
        self.assertTrue(report["reserved_area_present"])
        self.assertEqual(before, snapshot(self.root))
        self.assertFalse(any(
            entry["path"].startswith(f"{OUTPUT}/{retention.QUARANTINE_DIR}")
            for entry in report["entries"]
        ))

    def test_every_entry_is_unsafe_to_delete_or_move(self):
        self.make_version("active", 1)
        self.make_version("old", 120)
        self.write_filemap(mirror=f"{VERSION_ROOT}/{KEY}/active/a.md")
        report = self.audit()
        self.assertTrue(report["entries"])
        for entry in report["entries"]:
            self.assertEqual("none", entry["action"])
            self.assertEqual("unknown", entry["foreign_reference_risk"])
            self.assertFalse(entry["safe_to_delete"])
            self.assertFalse(entry["safe_to_move"])

    def test_audit_uses_no_destructive_filesystem_calls(self):
        self.make_version("active", 1)
        self.make_version("old", 400)
        self.write_filemap(mirror=f"{VERSION_ROOT}/{KEY}/active/a.md")
        with mock.patch.object(os, "unlink", side_effect=AssertionError("unlink")), \
             mock.patch.object(os, "remove", side_effect=AssertionError("remove")), \
             mock.patch.object(os, "rename", side_effect=AssertionError("rename")), \
             mock.patch.object(os, "replace", side_effect=AssertionError("replace")):
            report = self.audit()
        self.assertEqual("quarantine_candidate", self.entry(report, "old")["classification"])

    def test_cli_reports_the_same_read_only_result(self):
        self.make_version("active", 1)
        self.make_version("old", 400)
        self.write_filemap(mirror=f"{VERSION_ROOT}/{KEY}/active/a.md")
        before = snapshot(self.root)
        completed = self.run_cli()
        self.assertEqual(0, completed.returncode, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertTrue(result["success"])
        self.assertEqual("Completed", result["state"])
        report = result["data"]["retention"]
        self.assertEqual(retention.POLICY_ID, report["policy"]["id"])
        self.assertEqual("quarantine_candidate", self.entry(report, "old")["classification"])
        self.assertEqual(before, snapshot(self.root))

    def test_configured_catalog_verifies_identity(self):
        self.write_catalog()
        self.make_version("active", 1)
        self.write_filemap(mirror=f"{VERSION_ROOT}/{KEY}/active/a.md")
        report = self.audit()
        self.assertEqual("verified", report["identity"]["status"])
        self.assertTrue(report["classification_authoritative"])
        self.assertTrue(self.entry(report, "active")["authoritative"])

    def test_same_output_dir_with_unconfigured_filemap_is_non_authoritative(self):
        self.write_catalog(output_json=f"{OUTPUT}/other-filemap.json")
        self.make_version("active", 1)
        self.make_version("old", 400)
        self.write_filemap(mirror=f"{VERSION_ROOT}/{KEY}/active/a.md")
        report = self.audit()
        self.assertEqual("unverified", report["identity"]["status"])
        self.assertFalse(report["classification_authoritative"])
        self.assertFalse(self.entry(report, "old")["authoritative"])
        summary = retention_audit._summary(report)
        self.assertIn("UNVERIFIZIERT", summary)
        self.assertIn("Prüfhinweis (nicht autoritativ)", summary)
        self.assertNotIn("  Kandidat:", summary)

    def test_output_ancestor_placeholder_is_rejected_before_resolution(self):
        self.make_version("active", 1)
        self.write_filemap(mirror=f"{VERSION_ROOT}/{KEY}/active/a.md")
        ancestor = self.root / "memory" / "cloud" / "topics"
        real = retention.is_cloud_placeholder

        def fake(path):
            return os.path.normcase(str(path)) == os.path.normcase(str(ancestor)) or real(path)

        before = snapshot(self.root)
        with mock.patch.object(retention, "is_cloud_placeholder", side_effect=fake):
            with self.assertRaises(retention.RetentionError):
                self.audit()
        self.assertEqual(before, snapshot(self.root))

    def test_filemap_ancestor_placeholder_is_rejected_before_resolution(self):
        self.make_version("active", 1)
        alt_filemap = "memory/evidence/filemap.json"
        alt_path = self.root / alt_filemap
        alt_path.parent.mkdir(parents=True)
        alt_path.write_text(json.dumps({
            "output_dir": OUTPUT,
            "files": {SOURCE: {"markdown_mirror": f"{VERSION_ROOT}/{KEY}/active/a.md"}},
        }), encoding="utf-8")
        ancestor = self.root / "memory" / "evidence"
        real = retention.is_cloud_placeholder

        def fake(path):
            return os.path.normcase(str(path)) == os.path.normcase(str(ancestor)) or real(path)

        with mock.patch.object(retention, "is_cloud_placeholder", side_effect=fake):
            with self.assertRaises(retention.RetentionError):
                retention.classify_versions(
                    str(self.root), OUTPUT, alt_filemap, now=NOW,
                )

    def test_linked_catalog_ancestor_is_not_traversed(self):
        outside = self.root / "outside-catalog"
        topics = outside / "topics"
        topics.mkdir(parents=True)
        (topics / "topics.json").write_text(json.dumps([{
            "id": "demo",
            "title": "Demo",
            "cloud_sync": {
                "default": {
                    "output_json": FILEMAP,
                    "output_dir": OUTPUT,
                },
            },
        }]), encoding="utf-8")
        (self.root / "memory").mkdir(parents=True, exist_ok=True)
        link = self.root / "memory" / "references"
        if not make_directory_link(link, outside):
            self.skipTest("cannot create a directory link")
        self.make_version("active", 1)
        self.write_filemap(mirror=f"{VERSION_ROOT}/{KEY}/active/a.md")
        report = self.audit()
        self.assertEqual("unverified", report["identity"]["status"])
        self.assertFalse(report["classification_authoritative"])
        self.assertTrue(any("link" in error.lower() for error in report["identity"]["errors"]))

    def test_catalog_ancestor_placeholder_prevents_verification_not_audit(self):
        self.write_catalog()
        self.make_version("active", 1)
        self.write_filemap(mirror=f"{VERSION_ROOT}/{KEY}/active/a.md")
        ancestor = self.root / "memory" / "references"
        real = retention.is_cloud_placeholder

        def fake(path):
            return os.path.normcase(str(path)) == os.path.normcase(str(ancestor)) or real(path)

        with mock.patch.object(retention, "is_cloud_placeholder", side_effect=fake):
            report = self.audit()
        self.assertEqual("unverified", report["identity"]["status"])
        self.assertFalse(report["classification_authoritative"])
        self.assertEqual("active", self.entry(report, "active")["classification"])

    def test_broken_other_catalog_blocks_verification_even_with_valid_match(self):
        self.write_catalog()
        projects = self.root / "memory" / "references" / "projects"
        projects.mkdir(parents=True, exist_ok=True)
        (projects / "projects.json").write_text("{", encoding="utf-8")
        self.make_version("active", 1)
        self.write_filemap(mirror=f"{VERSION_ROOT}/{KEY}/active/a.md")
        report = self.audit()
        self.assertEqual("unverified", report["identity"]["status"])
        self.assertFalse(report["classification_authoritative"])
        self.assertTrue(report["identity"]["errors"])

    def test_catalog_aliases_verify_identity(self):
        catalog = self.root / "memory" / "references" / "topics"
        catalog.mkdir(parents=True, exist_ok=True)
        (catalog / "topics.json").write_text(json.dumps([{
            "id": "demo",
            "title": "Demo",
            "cloud_sync": {
                "default": {
                    "filemap_json": FILEMAP,
                    "cloud_mirror_dir": OUTPUT,
                },
            },
        }]), encoding="utf-8")
        self.make_version("active", 1)
        self.write_filemap(mirror=f"{VERSION_ROOT}/{KEY}/active/a.md")
        report = self.audit()
        self.assertEqual("verified", report["identity"]["status"])

    def test_synthesized_catalog_defaults_verify_identity(self):
        catalog = self.root / "memory" / "references" / "topics"
        catalog.mkdir(parents=True, exist_ok=True)
        (catalog / "topics.json").write_text(json.dumps([{
            "id": "demo",
            "title": "Demo",
        }]), encoding="utf-8")
        self.make_version("active", 1)
        self.write_filemap(mirror=f"{VERSION_ROOT}/{KEY}/active/a.md")
        report = self.audit()
        self.assertEqual("verified", report["identity"]["status"])

    def test_catalog_ambiguity_is_not_authoritative(self):
        catalog = self.root / "memory" / "references" / "topics"
        catalog.mkdir(parents=True, exist_ok=True)
        (catalog / "topics.json").write_text(json.dumps([{
            "id": "demo",
            "title": "Demo",
            "cloud_sync": {
                "one": {"output_json": FILEMAP, "output_dir": OUTPUT},
                "two": {"filemap_json": FILEMAP, "cloud_mirror_dir": OUTPUT},
            },
        }]), encoding="utf-8")
        self.make_version("active", 1)
        self.write_filemap(mirror=f"{VERSION_ROOT}/{KEY}/active/a.md")
        report = self.audit()
        self.assertEqual("ambiguous", report["identity"]["status"])
        self.assertFalse(report["classification_authoritative"])

    def test_json_argument_errors_use_the_canonical_envelope(self):
        completed = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "retention_audit.py"), "--json"],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(2, completed.returncode)
        result = json.loads(completed.stdout)
        self.assertEqual(
            {"action", "success", "state", "message", "data", "error"}, set(result),
        )
        self.assertFalse(result["success"])
        self.assertEqual("arguments", result["error"]["phase"])

    def test_json_invalid_now_uses_the_canonical_envelope(self):
        completed = subprocess.run([
            sys.executable, str(SCRIPTS_DIR / "retention_audit.py"),
            "--workspace-root", str(self.root),
            "--output-dir", OUTPUT,
            "--filemap", FILEMAP,
            "--now", "not-a-timestamp",
            "--json",
        ], capture_output=True, text=True, timeout=60)
        self.assertEqual(2, completed.returncode)
        result = json.loads(completed.stdout)
        self.assertEqual("arguments", result["error"]["phase"])
        self.assertFalse(result["success"])

    # -- layout, links, journals and invalid input ------------------------
    def test_unexpected_layout_is_a_candidate_and_is_not_traversed(self):
        bad_key = self.make_version("run", 1, key="not-a-source-key")
        stray = self.root / VERSION_ROOT / KEY / "stray.txt"
        stray.parent.mkdir(parents=True)
        stray.write_bytes(b"stray")
        self.write_filemap(mirror=f"{VERSION_ROOT}/{KEY}/active/a.md")
        report = self.audit()
        paths = {entry["path"]: entry for entry in report["entries"]}
        self.assertEqual(
            "quarantine_candidate", paths[f"{VERSION_ROOT}/not-a-source-key"]["classification"],
        )
        self.assertEqual(
            ["unexpected_layout"], paths[f"{VERSION_ROOT}/not-a-source-key"]["reasons"],
        )
        self.assertEqual(
            "quarantine_candidate", paths[f"{VERSION_ROOT}/{KEY}/stray.txt"]["classification"],
        )
        self.assertTrue(bad_key.is_dir())
        self.assertTrue(stray.is_file())

    def test_a_linked_version_is_not_followed(self):
        outside = self.root / "outside"
        outside.mkdir()
        marker = outside / "secret.md"
        marker.write_bytes(b"SECRET")
        key_dir = self.root / VERSION_ROOT / KEY
        key_dir.mkdir(parents=True)
        if not make_directory_link(key_dir / "linked", outside):
            self.skipTest("cannot create a directory link")
        self.write_filemap(mirror=f"{VERSION_ROOT}/{KEY}/active/a.md")
        before = snapshot(self.root)
        report = self.audit()
        entry = self.entry(report, "linked")
        self.assertEqual("quarantine_candidate", entry["classification"])
        self.assertEqual(["unsafe_version_directory"], entry["reasons"])
        self.assertEqual(b"SECRET", marker.read_bytes())
        self.assertEqual(before, snapshot(self.root))

    def test_a_linked_run_referenced_active_is_protected_and_not_traversed(self):
        outside = self.root / "outside"
        outside.mkdir()
        marker = outside / "a.md"
        marker.write_bytes(b"SECRET")
        key_dir = self.root / VERSION_ROOT / KEY
        key_dir.mkdir(parents=True)
        if not make_directory_link(key_dir / "linked", outside):
            self.skipTest("cannot create a directory link")
        self.write_filemap(mirror=f"{VERSION_ROOT}/{KEY}/linked/a.md")
        before = snapshot(self.root)
        report = self.audit()
        entry = self.entry(report, "linked")
        self.assertEqual("active", entry["classification"])
        self.assertEqual(["filemap_reference"], entry["reasons"])
        self.assertTrue(entry["unsafe"])
        self.assertEqual(1, report["references"]["matched"])
        self.assertEqual([], report["references"]["unresolved"])
        self.assertEqual(b"SECRET", marker.read_bytes())
        self.assertEqual(before, snapshot(self.root))

    def test_linked_source_key_with_active_reference_is_unresolved_not_candidate(self):
        outside = self.root / "outside-key"
        outside.mkdir()
        (outside / "a.md").write_bytes(b"SECRET")
        key_dir = self.root / VERSION_ROOT / KEY
        key_dir.parent.mkdir(parents=True, exist_ok=True)
        if not make_directory_link(key_dir, outside):
            self.skipTest("cannot create a directory link")
        self.write_filemap(mirror=f"{VERSION_ROOT}/{KEY}/active/a.md")
        before = snapshot(self.root)
        report = self.audit()
        entry = next(
            item for item in report["entries"]
            if item["path"] == f"{VERSION_ROOT}/{KEY}"
        )
        self.assertEqual("unresolved", entry["classification"])
        self.assertTrue(entry["potentially_active"])
        self.assertEqual(["unsafe_source_directory"], entry["reasons"])
        self.assertFalse(entry["quarantine"]["logical"])
        self.assertEqual(
            [f"{VERSION_ROOT}/{KEY}/active"], report["references"]["unresolved"],
        )
        summary = retention_audit._summary(report)
        self.assertIn("Unaufgelöste aktive Referenzen", summary)
        self.assertIn(f"{VERSION_ROOT}/{KEY}/active", summary)
        self.assertEqual(before, snapshot(self.root))

    def test_unrecognized_source_key_with_active_reference_is_unresolved(self):
        key_path = self.root / VERSION_ROOT / KEY
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_bytes(b"not a directory")
        self.write_filemap(mirror=f"{VERSION_ROOT}/{KEY}/active/a.md")
        report = self.audit()
        entry = next(
            item for item in report["entries"]
            if item["path"] == f"{VERSION_ROOT}/{KEY}"
        )
        self.assertEqual("unresolved", entry["classification"])
        self.assertEqual(["unexpected_layout"], entry["reasons"])
        self.assertTrue(entry["potentially_active"])

    def test_a_linked_version_root_fails_closed(self):
        outside = self.root / "outside"
        outside.mkdir()
        root = self.root / VERSION_ROOT
        root.parent.mkdir(parents=True, exist_ok=True)
        if not make_directory_link(root, outside):
            self.skipTest("cannot create a directory link")
        self.write_filemap(mirror=f"{VERSION_ROOT}/{KEY}/active/a.md")
        baseline = snapshot(self.root)
        with self.assertRaises(retention.RetentionError):
            self.audit()
        self.assertEqual(baseline, snapshot(self.root))
        self.assertFalse(any(outside.iterdir()))

    def test_missing_isjunction_falls_back_to_reparse_attributes(self):
        with mock.patch.object(retention, "_on_windows", return_value=True), \
             mock.patch.object(retention.os.path, "isjunction", None, create=True), \
             mock.patch.object(retention.os, "lstat", return_value=mock.Mock(st_file_attributes=0)):
            self.assertFalse(retention._is_junction("probe"))
        with mock.patch.object(retention, "_on_windows", return_value=True), \
             mock.patch.object(retention.os.path, "isjunction", None, create=True), \
             mock.patch.object(retention.os, "lstat", return_value=mock.Mock(st_file_attributes=0x400)):
            self.assertTrue(retention._is_junction("probe"))

    def test_missing_isjunction_without_attributes_fails_closed(self):
        with mock.patch.object(retention, "_on_windows", return_value=True), \
             mock.patch.object(retention.os.path, "isjunction", None, create=True), \
             mock.patch.object(retention.os, "lstat", return_value=object()):
            self.assertTrue(retention._is_junction("probe"))

    def test_missing_isjunction_does_not_reject_non_windows_paths(self):
        if os.name == "nt":
            self.skipTest("non-Windows regression")
        with mock.patch.object(retention.os.path, "isjunction", None, create=True):
            self.assertFalse(retention._is_junction("probe"))

    def test_missing_isjunction_fails_closed_for_protected_paths(self):
        self.make_version("active", 1)
        self.write_filemap(mirror=f"{VERSION_ROOT}/{KEY}/active/a.md")
        with mock.patch.object(retention, "_on_windows", return_value=True), \
             mock.patch.object(retention.os.path, "isjunction", None, create=True), \
             mock.patch.object(retention.os, "lstat", return_value=object()):
            with self.assertRaises(retention.RetentionError):
                self.audit()

    def test_journal_placeholder_is_checked_before_realpath(self):
        journal = self.root / OUTPUT / ".only-file-journal"
        journal.mkdir()
        self.make_version("active", 1)
        self.write_filemap(mirror=f"{VERSION_ROOT}/{KEY}/active/a.md")
        real_placeholder = retention.is_cloud_placeholder
        real_realpath = retention.os.path.realpath

        def fake_placeholder(path):
            return os.path.normcase(str(path)) == os.path.normcase(str(journal)) or real_placeholder(path)

        def guarded_realpath(path, *args, **kwargs):
            if os.path.normcase(str(path)) == os.path.normcase(str(journal)):
                raise AssertionError("journal was resolved before the placeholder guard")
            return real_realpath(path, *args, **kwargs)

        with mock.patch.object(retention, "is_cloud_placeholder", side_effect=fake_placeholder), \
             mock.patch.object(retention.os.path, "realpath", side_effect=guarded_realpath):
            report = self.audit()
        self.assertTrue(report["journals"]["present"])
        self.assertTrue(report["journals"]["unsafe"])
        self.assertIsNone(report["journals"]["entries"])

    def test_cloud_placeholder_run_is_never_traversed(self):
        run = self.make_version("placeholder", 1, files={"nested/secret": b"x"})
        nested = run / "nested" / "secret"
        real = retention.is_cloud_placeholder

        def fake(path):
            return os.path.normcase(str(path)) == os.path.normcase(str(run)) or real(path)

        self.write_filemap(mirror=f"{VERSION_ROOT}/{KEY}/active/a.md")
        before = snapshot(self.root)
        with mock.patch.object(retention, "is_cloud_placeholder", side_effect=fake):
            report = self.audit()
        entry = self.entry(report, "placeholder")
        self.assertEqual("quarantine_candidate", entry["classification"])
        self.assertTrue(entry["placeholders"])
        self.assertEqual(["cloud_placeholder_version_directory"], entry["reasons"])
        self.assertEqual(0, entry["size_bytes"])
        self.assertEqual(0, entry["file_count"])
        self.assertEqual(b"x", nested.read_bytes())
        self.assertEqual(before, snapshot(self.root))

    def test_cloud_placeholder_run_referenced_active_stays_active(self):
        run = self.make_version("active", 1, files={"nested/secret": b"x"})
        nested = run / "nested" / "secret"
        real = retention.is_cloud_placeholder

        def fake(path):
            return os.path.normcase(str(path)) == os.path.normcase(str(run)) or real(path)

        self.write_filemap(mirror=f"{VERSION_ROOT}/{KEY}/active/a.md")
        with mock.patch.object(retention, "is_cloud_placeholder", side_effect=fake):
            report = self.audit()
        entry = self.entry(report, "active")
        self.assertEqual("active", entry["classification"])
        self.assertTrue(entry["placeholders"])
        self.assertTrue(entry["unsafe"])
        self.assertEqual(0, entry["size_bytes"])
        self.assertEqual(0, entry["file_count"])
        self.assertEqual(1, report["references"]["matched"])
        self.assertEqual(b"x", nested.read_bytes())

    def test_placeholder_run_is_not_resolved_before_the_guard(self):
        run = self.make_version("placeholder", 1)
        real_placeholder = retention.is_cloud_placeholder
        real_realpath = retention.os.path.realpath

        def fake_placeholder(path):
            return os.path.normcase(str(path)) == os.path.normcase(str(run)) or real_placeholder(path)

        def guarded_realpath(path, *args, **kwargs):
            if os.path.normcase(str(path)) == os.path.normcase(str(run)):
                raise AssertionError("placeholder run was resolved")
            return real_realpath(path, *args, **kwargs)

        self.write_filemap(mirror=f"{VERSION_ROOT}/{KEY}/active/a.md")
        with mock.patch.object(retention, "is_cloud_placeholder", side_effect=fake_placeholder), \
             mock.patch.object(retention.os.path, "realpath", side_effect=guarded_realpath):
            report = self.audit()
        entry = self.entry(report, "placeholder")
        self.assertEqual("quarantine_candidate", entry["classification"])
        self.assertTrue(entry["placeholders"])

    def test_placeholder_content_is_not_resolved_before_the_guard(self):
        run = self.make_version("version", 1, files={})
        nested = run / "nested"
        nested.mkdir()
        real_placeholder = retention.is_cloud_placeholder
        real_realpath = retention.os.path.realpath

        def fake_placeholder(path):
            return os.path.normcase(str(path)) == os.path.normcase(str(nested)) or real_placeholder(path)

        def guarded_realpath(path, *args, **kwargs):
            if os.path.normcase(str(path)) == os.path.normcase(str(nested)):
                raise AssertionError("placeholder content was resolved")
            return real_realpath(path, *args, **kwargs)

        self.write_filemap(mirror=f"{VERSION_ROOT}/{KEY}/active/a.md")
        with mock.patch.object(retention, "is_cloud_placeholder", side_effect=fake_placeholder), \
             mock.patch.object(retention.os.path, "realpath", side_effect=guarded_realpath):
            report = self.audit()
        entry = self.entry(report, "version")
        self.assertEqual("quarantine_candidate", entry["classification"])
        self.assertTrue(entry["placeholders"])

    def test_cloud_placeholder_content_directory_is_never_traversed(self):
        run = self.make_version("version", 1, files={})
        nested = run / "nested"
        nested.mkdir()
        secret = nested / "secret"
        secret.write_bytes(b"x")
        real = retention.is_cloud_placeholder

        def fake(path):
            return os.path.normcase(str(path)) == os.path.normcase(str(nested)) or real(path)

        self.write_filemap(mirror=f"{VERSION_ROOT}/{KEY}/active/a.md")
        with mock.patch.object(retention, "is_cloud_placeholder", side_effect=fake):
            report = self.audit()
        entry = self.entry(report, "version")
        self.assertEqual("quarantine_candidate", entry["classification"])
        self.assertTrue(entry["placeholders"])
        self.assertEqual(["cloud_placeholder_version_directory"], entry["reasons"])
        self.assertEqual(0, entry["size_bytes"])
        self.assertEqual(0, entry["file_count"])
        self.assertEqual(b"x", secret.read_bytes())

    def test_cloud_placeholder_source_directory_is_reported_without_scan(self):
        key_dir = self.root / VERSION_ROOT / KEY
        (key_dir / "run").mkdir(parents=True)
        secret = key_dir / "run" / "secret"
        secret.write_bytes(b"x")
        real = retention.is_cloud_placeholder

        def fake(path):
            return os.path.normcase(str(path)) == os.path.normcase(str(key_dir)) or real(path)

        self.write_filemap(mirror=f"{VERSION_ROOT}/{KEY}/active/a.md")
        before = snapshot(self.root)
        with mock.patch.object(retention, "is_cloud_placeholder", side_effect=fake):
            report = self.audit()
        entry = next(
            item for item in report["entries"]
            if item["path"] == f"{VERSION_ROOT}/{KEY}"
        )
        self.assertEqual("unresolved", entry["classification"])
        self.assertTrue(entry["potentially_active"])
        self.assertTrue(entry["placeholders"])
        self.assertEqual(["cloud_placeholder_source_directory"], entry["reasons"])
        self.assertEqual(
            [f"{VERSION_ROOT}/{KEY}/active"], report["references"]["unresolved"],
        )
        self.assertFalse(entry["quarantine"]["logical"])
        self.assertEqual(b"x", secret.read_bytes())
        self.assertEqual(before, snapshot(self.root))

    def test_journals_are_reported_but_never_retention_entries(self):
        journal_dir = self.root / OUTPUT / ".only-file-journal"
        journal_dir.mkdir()
        journal = journal_dir / f"{KEY}.json"
        journal.write_text('{"state": "committed"}', encoding="utf-8")
        leftover = journal_dir / "commit.tmp"
        leftover.write_bytes(b"leftover")
        self.make_version("active", 1)
        self.write_filemap(mirror=f"{VERSION_ROOT}/{KEY}/active/a.md")
        before = snapshot(self.root)
        report = self.audit()
        self.assertEqual(before, snapshot(self.root))
        self.assertTrue(report["journals"]["present"])
        self.assertEqual(2, report["journals"]["entries"])
        self.assertEqual("journal_not_managed", report["journals"]["classification"])
        self.assertFalse(any(
            entry["path"].startswith(f"{OUTPUT}/.only-file-journal")
            for entry in report["entries"]
        ))

    def test_missing_version_root_is_an_empty_report(self):
        self.write_filemap()
        report = self.audit()
        self.assertFalse(report["version_root_present"])
        self.assertEqual(
            {"active": 0, "retained": 0, "quarantine_candidate": 0, "unresolved": 0},
            report["counts"],
        )
        self.assertEqual([], report["entries"])

    def test_invalid_filemap_fails_closed_without_writes(self):
        (self.root / FILEMAP).write_text("{", encoding="utf-8")
        before = snapshot(self.root)
        with self.assertRaises(retention.RetentionError):
            self.audit()
        self.assertEqual(before, snapshot(self.root))

    def test_duplicate_filemap_keys_fail_closed(self):
        (self.root / FILEMAP).write_text(
            '{"files": {"a": {}, "a": {}}}', encoding="utf-8",
        )
        before = snapshot(self.root)
        with self.assertRaises(retention.RetentionError):
            self.audit()
        self.assertEqual(before, snapshot(self.root))

    def test_output_outside_the_data_zones_is_rejected(self):
        with self.assertRaises(retention.RetentionError):
            retention.classify_versions(str(self.root), "outside/output", FILEMAP, now=NOW)

    def test_filemap_for_a_different_output_is_rejected(self):
        self.make_version("active", 1)
        (self.root / FILEMAP).write_text(json.dumps({
            "output_dir": "memory/cloud/topics/other",
            "files": {SOURCE: {"markdown_mirror": f"{VERSION_ROOT}/{KEY}/active/a.md"}},
        }), encoding="utf-8")
        before = snapshot(self.root)
        with self.assertRaises(retention.RetentionError):
            self.audit()
        self.assertEqual(before, snapshot(self.root))

    def test_legacy_filemap_without_output_dir_fails_closed(self):
        self.make_version("active", 1)
        (self.root / FILEMAP).write_text(json.dumps({
            "files": {SOURCE: {"markdown_mirror": f"{VERSION_ROOT}/{KEY}/active/a.md"}},
        }), encoding="utf-8")
        before = snapshot(self.root)
        with self.assertRaises(retention.RetentionError):
            self.audit()
        self.assertEqual(before, snapshot(self.root))


if __name__ == "__main__":
    unittest.main()
