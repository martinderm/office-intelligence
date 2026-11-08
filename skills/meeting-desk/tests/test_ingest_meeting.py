"""Regression tests for the canonical meeting writer (OI #35).

All workspaces, records and catalogs are synthetic and live in temporary
directories. The real shared workspace-lock guard is loaded in place; no lock
copy and no legacy bypass exists in the tested code path.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from meeting_test_support import (
    FIXTURES,
    WRITER_SCRIPT,
    acquire,
    find_lock_root,
    fixture_pack,
    load_writer,
    read_json,
    register_document,
    release,
    run_cli,
    valid_record,
    write_record,
)

ENVELOPE_FIELDS = {"action", "success", "state", "message", "data", "error"}
CRASH_DRIVER = Path(__file__).resolve().parent / "_crash_ingest.py"


class WriterTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lock_root = find_lock_root()
        if cls.lock_root is None:
            raise unittest.SkipTest(
                "trusted workspace-lock installation not available; set "
                "MEETING_DESK_WORKSPACE_LOCK_ROOT to an explicit installation")
        cls.writer = load_writer()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="meeting-writer-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / "memory/evidence/meetings").mkdir(parents=True)
        self.register_path = self.root / "memory/evidence/meetings/meetings.json"
        self.lease = f"lease-{uuid4().hex[:12]}"
        self.acquired = False

    # -- helpers ---------------------------------------------------------
    def acquire(self, lease: str | None = None):
        lease = lease or self.lease
        if not self.acquired:
            acquire(self.lock_root, self.root, lease)
            self.acquired = True
            self.addCleanup(release, self.lock_root, self.root, lease)
        return lease

    def ingest(self, record, **options):
        lease = self.acquire()
        options.setdefault("workspace_root", self.root)
        options.setdefault("lease_id", lease)
        options.setdefault("trusted_workspace_lock_root", self.lock_root)
        return self.writer.ingest_record(record, **options)

    def publish_transcript(self, content, meeting_id, **options):
        lease = self.acquire()
        options.setdefault("workspace_root", self.root)
        options.setdefault("lease_id", lease)
        options.setdefault("trusted_workspace_lock_root", self.lock_root)
        return self.writer.publish_registered_transcript(
            content, meeting_id=meeting_id, **options)

    def assert_envelope(self, envelope):
        self.assertEqual(set(envelope), ENVELOPE_FIELDS)

    def assert_success(self, envelope, state=None):
        self.assert_envelope(envelope)
        self.assertTrue(envelope["success"], envelope)
        self.assertIsNone(envelope["error"])
        if state is not None:
            self.assertEqual(envelope["state"], state)

    def assert_rejected(self, envelope):
        self.assert_envelope(envelope)
        self.assertFalse(envelope["success"], envelope)
        self.assertEqual(envelope["state"], "Rejected")
        self.assertNotEqual(envelope["error"]["type"], "RecoveryRequired")

    def write_catalogs(self, topics=None, projects=None):
        for key, entries in (("topics", topics), ("projects", projects)):
            if entries is None:
                continue
            path = self.root / f"memory/references/{key}/{key}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({key: entries}, ensure_ascii=False), encoding="utf-8")

    def write_register(self, value):
        self.register_path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                                      encoding="utf-8", newline="\n")

    def artifacts(self, entry):
        return [entry.get(key) for key in
                ("summary_path", "transcript_path", "transcript_raw_path")
                if entry.get(key)]

    def journal_dir(self):
        return self.root / "data/meeting-desk/journal"


class CapabilitiesAndCliTests(WriterTestBase):
    def test_capabilities_is_read_only_and_needs_no_workspace(self):
        with tempfile.TemporaryDirectory(prefix="meeting-caps-") as cwd:
            code, envelope, stderr = run_cli(
                self.lock_root, ["--capabilities"], cwd=Path(cwd))
            self.assertEqual(code, 0, stderr)
            self.assertEqual(set(envelope), ENVELOPE_FIELDS)
            self.assertTrue(envelope["success"])
            self.assertEqual(envelope["data"]["record_schema"], "meeting-record/1")
            self.assertEqual(envelope["data"]["writer_version"], "meeting-desk/1")
            self.assertEqual(list(Path(cwd).iterdir()), [])

    def test_cli_record_from_stdin_publishes_one_envelope(self):
        record = valid_record(1)
        lease = self.acquire()
        code, envelope, stderr = run_cli(
            self.lock_root,
            ["--record", "-", "--workspace", str(self.root), "--lease-id", lease],
            stdin=json.dumps(record).encode("utf-8"))
        self.assertEqual(code, 0, stderr)
        self.assert_success(envelope, "Applied")
        self.assertEqual(envelope["data"]["meeting_id"], record["meeting_id"])
        self.assertTrue(envelope["data"]["changed"])
        entry = register_document(self.root)["meetings"][0]
        self.assertTrue((self.root / entry["summary_path"]).is_file())
        self.assertFalse(self.journal_dir().exists()
                         and any(self.journal_dir().iterdir()))

    def test_cli_exit_codes_for_rejection_and_bounded_input(self):
        lease = self.acquire()
        code, envelope, _ = run_cli(
            self.lock_root, ["--record", "-", "--workspace", str(self.root),
                             "--lease-id", lease],
            stdin=b'{"schema": "meeting-record/2"}')
        self.assertEqual(code, 2)
        self.assert_rejected(envelope)
        for raw in (b"\xff\xfe{}", b"{" + b" " * (1024 * 1024 + 10)):
            code, envelope, _ = run_cli(
                self.lock_root, ["--record", "-", "--workspace", str(self.root),
                                 "--lease-id", lease], stdin=raw)
            self.assertEqual(code, 2, raw[:20])
            self.assert_rejected(envelope)
        self.assertFalse(self.register_path.exists())

    def test_cli_dry_run_needs_no_lease_and_writes_nothing(self):
        record = valid_record(1)
        code, envelope, stderr = run_cli(
            self.lock_root, ["--record", "-", "--workspace", str(self.root),
                             "--dry-run"],
            stdin=json.dumps(record).encode("utf-8"))
        self.assertEqual(code, 0, stderr)
        self.assert_success(envelope, "DryRun")
        self.assertTrue(envelope["data"]["dry_run"])
        self.assertEqual(envelope["data"]["plan"]["meeting_id"], record["meeting_id"])
        self.assertEqual(len(envelope["data"]["plan_sha256"]), 64)
        self.assertFalse(self.register_path.exists())

    def test_manual_entry_defaults_to_plan_and_requires_hash_to_apply(self):
        record = valid_record(3)
        self.assertEqual(record["source"]["system"], "manual")
        record_path = write_record(self.root / "manual.json", record)
        code, plan_envelope, stderr = run_cli(
            self.lock_root, ["--record", str(record_path), "--workspace", str(self.root)])
        self.assertEqual(code, 0, stderr)
        self.assert_success(plan_envelope, "Plan")
        self.assertTrue(plan_envelope["data"]["apply_required"])
        plan_sha = plan_envelope["data"]["plan_sha256"]
        self.assertFalse(self.register_path.exists())

        lease = self.acquire()
        code, wrong, _ = run_cli(
            self.lock_root, ["--record", str(record_path), "--workspace", str(self.root),
                             "--lease-id", lease, "--apply-plan", "0" * 64])
        self.assertEqual(code, 2)
        self.assert_rejected(wrong)
        self.assertFalse(self.register_path.exists())

        code, applied, stderr = run_cli(
            self.lock_root, ["--record", str(record_path), "--workspace", str(self.root),
                             "--lease-id", lease, "--apply-plan", plan_sha])
        self.assertEqual(code, 0, stderr)
        self.assert_success(applied, "Applied")
        entry = register_document(self.root)["meetings"][0]
        self.assertEqual(entry["meeting_id"], record["meeting_id"])
        body = (self.root / entry["summary_path"]).read_bytes()
        self.assertTrue(body.endswith(record["artifacts"]["summary_md"].encode("utf-8")))


class EncodingAndLeaseTests(WriterTestBase):
    def test_successful_apply_stdout_is_utf8_under_cp1252(self):
        record = valid_record(1)
        record["title"] = "Synthetic \u4f1a\u8bae \U0001f680"
        lease = self.acquire()
        code, envelope, stderr = run_cli(
            self.lock_root,
            ["--record", "-", "--workspace", str(self.root), "--lease-id", lease],
            stdin=json.dumps(record).encode("utf-8"),
            env={"PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"})
        self.assertEqual(code, 0, stderr)
        self.assert_success(envelope, "Applied")
        self.assertEqual(envelope["data"]["entry"]["title"], record["title"])
        self.assertNotIn("Traceback", stderr)

    def test_capabilities_and_fixture_verifier_are_utf8_under_cp1252(self):
        env = {"PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"}
        code, envelope, stderr = run_cli(self.lock_root, ["--capabilities"], env=env)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(envelope["data"]["writer_version"], "meeting-desk/1")
        verifier = WRITER_SCRIPT.with_name("verify_meeting_fixtures.py")
        completed = subprocess.run(
            [sys.executable, str(verifier), "--fixtures", str(FIXTURES), "--json"],
            capture_output=True, env={**os.environ, **env}, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(json.loads(completed.stdout.decode("utf-8"))["success"])

    def test_lease_loss_before_staging_leaves_no_mutation(self):
        record = valid_record(1)
        self.acquire()
        original = self.writer._require_lease
        calls = {"count": 0}

        def flaky(guard):
            calls["count"] += 1
            if calls["count"] >= 2:
                raise self.writer._LeaseRejected("lease lost")
            return original(guard)

        with patch.object(self.writer, "_require_lease", side_effect=flaky):
            envelope = self.writer.ingest_record(
                record, workspace_root=self.root, lease_id=self.lease,
                trusted_workspace_lock_root=self.lock_root)
        self.assert_envelope(envelope)
        self.assertFalse(envelope["success"])
        self.assertFalse(self.register_path.exists())
        self.assertFalse(self.journal_dir().exists())
        self.assertEqual(list((self.root / "memory/evidence").glob("topics/**/*")), [])

    def test_preserved_classification_keeps_original_basis(self):
        record = valid_record(1)
        self.write_catalogs(topics=[{"id": "catalogue-topic", "title": "Synthetic"}])
        self.assert_success(self.ingest(record, topic_slug="catalogue-topic"), "Applied")
        document = register_document(self.root)
        document["meetings"][0]["classification_basis"] = ["legacy-human-decision"]
        self.write_register(document)
        self.assert_success(self.ingest(record), "Unchanged")
        entry = register_document(self.root)["meetings"][0]
        self.assertEqual(entry["classification_basis"], ["legacy-human-decision"])


class RecordValidationTests(WriterTestBase):
    def test_fixture_pack_is_self_verifying(self):
        pack = fixture_pack()
        self.assertEqual(pack["schema"], "meeting-record-fixtures/1")
        self.assertEqual(pack["record_schema"], "meeting-record/1")
        self.assertEqual(pack["writer_version"], "meeting-desk/1")
        covered = {key: pack[key] for key in
                   ("valid_records", "invalid_records", "raw_invalid_json")}
        digest = hashlib.sha256(json.dumps(
            covered, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")).hexdigest()
        self.assertEqual(pack["manifest"]["digest"], digest)
        self.assertEqual(pack["manifest"]["algorithm"], "sha256")
        self.assertTrue(pack["provenance"]["synthetic"])

    def test_fixture_valid_and_invalid_records_match_the_writer_contract(self):
        pack = fixture_pack()
        for index, record in enumerate(pack["valid_records"]):
            with self.subTest(index=index):
                normalized = self.writer.validate_record(record)
                self.assertEqual(normalized["schema"], "meeting-record/1")
        for index, item in enumerate(pack["invalid_records"]):
            with self.subTest(index=index, reason=item["reason"]):
                with self.assertRaises(self.writer._Reject):
                    self.writer.validate_record(item["record"])
        for index, item in enumerate(pack["raw_invalid_json"]):
            with self.subTest(index=index, reason=item["reason"]):
                with self.assertRaises(self.writer._Reject):
                    self.writer.strict_json(item["raw"])

    def test_bounded_json_rejects_duplicates_nonfinite_overflow_and_surrogates(self):
        cases = [
            '{"a": 1, "a": 2}',
            '{"a": NaN}',
            '{"a": Infinity}',
            '{"a": 18446744073709551616}',
            '{"a": "\\ud800"}',
            "[" * 40 + "]" * 40,
        ]
        for raw in cases:
            with self.subTest(raw=raw[:30]):
                with self.assertRaises(self.writer._Reject):
                    self.writer.strict_json(raw)
        with patch.object(self.writer, "MAX_JSON_NODES", 10):
            with self.assertRaises(self.writer._Reject):
                self.writer.strict_json(json.dumps(list(range(20))))

    def test_record_source_metadata_is_inert_and_optional_extensions_rejected(self):
        record = valid_record(6)
        record["source_metadata"]["topic_slug"] = "catalogue-topic"
        record["source_metadata"]["summary_path"] = "memory/evidence/evil.md"
        envelope = self.ingest(record)
        self.assert_success(envelope, "Applied")
        entry = register_document(self.root)["meetings"][0]
        self.assertEqual(entry["topic_slug"], None)
        self.assertEqual(entry["classification_status"], "unmapped")
        self.assertTrue(entry["summary_path"].startswith(
            "memory/evidence/meetings/unrouted/"))
        bad = valid_record(1)
        bad["provider"] = {"anything": True}
        self.assert_rejected(self.ingest(bad))

    def test_fixture_verifier_cli_checks_digest_and_records(self):
        verifier = WRITER_SCRIPT.with_name("verify_meeting_fixtures.py")
        command = [sys.executable, str(verifier), "--fixtures", str(FIXTURES), "--json"]
        completed = subprocess.run(command, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", check=False)
        self.assertEqual(completed.returncode, 0, completed.stdout)
        envelope = json.loads(completed.stdout)
        self.assertTrue(envelope["success"])
        self.assertEqual(envelope["data"]["valid"], 11)
        with tempfile.TemporaryDirectory(prefix="meeting-fixture-drift-") as temporary:
            copy = Path(temporary) / "pack.json"
            pack = fixture_pack()
            pack["valid_records"][0]["title"] = "Mutated fixture"
            copy.write_text(json.dumps(pack), encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, str(verifier), "--fixtures", str(copy), "--json"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                check=False)
            self.assertEqual(completed.returncode, 2)
            self.assertEqual(json.loads(completed.stdout)["state"], "DigestMismatch")

    def test_crlf_and_unicode_artifacts_are_byte_exact(self):
        record = valid_record(5)
        envelope = self.ingest(record)
        self.assert_success(envelope, "Applied")
        entry = register_document(self.root)["meetings"][0]
        raw_bytes = (self.root / entry["transcript_raw_path"]).read_bytes()
        self.assertEqual(raw_bytes, record["artifacts"]["transcript_raw_md"].encode("utf-8"))
        transcript_bytes = (self.root / entry["transcript_path"]).read_bytes()
        self.assertTrue(transcript_bytes.endswith(
            record["artifacts"]["transcript_md"].encode("utf-8")))
        self.assertIn("\u00c4u\u00dferung".encode("utf-8"), transcript_bytes)


class RegisterAndLegacyTests(WriterTestBase):
    def test_object_register_root_extras_and_untouched_entries_are_preserved(self):
        unrelated = {"meeting_id": "legacy-unrelated", "unknown": {"keep": [1, 2]}}
        self.write_register({"schema": "legacy-root/1", "owner": "synthetic",
                             "meetings": [copy.deepcopy(unrelated)]})
        record = valid_record(1)
        envelope = self.ingest(record)
        self.assert_success(envelope, "Applied")
        document = register_document(self.root)
        self.assertEqual(document["schema"], "legacy-root/1")
        self.assertEqual(document["owner"], "synthetic")
        self.assertEqual(document["meetings"][0], unrelated)
        self.assertEqual(document["meetings"][1]["meeting_id"], record["meeting_id"])

    def test_legacy_array_register_is_rejected_without_mutation(self):
        original = json.dumps([{"meeting_id": "legacy"}]).encode("utf-8")
        self.register_path.write_bytes(original)
        envelope = self.ingest(valid_record(1))
        self.assert_rejected(envelope)
        self.assertEqual(self.register_path.read_bytes(), original)
        self.assertEqual(list((self.root / "memory/evidence/meetings").glob("**/*.md")), [])

    def test_malformed_register_is_rejected_without_replacement(self):
        original = b'{"meetings": ['
        self.register_path.write_bytes(original)
        envelope = self.ingest(valid_record(1))
        self.assert_rejected(envelope)
        self.assertEqual(self.register_path.read_bytes(), original)

    def test_legacy_alias_entry_is_updated_in_place_without_rekeying(self):
        record = valid_record(6)
        self.write_register({"meetings": [{
            "id": "legacy-zoom-id",
            "meeting_id": record["meeting_id"],
            "source": {"system": "ticnote", "external_id": record["source"]["external_id"]},
            "title": "Legacy title",
            "duration": 5,
            "duration_minutes": 5,
            "dateString": "2026-10-18 17:45:10",
            "first_synced_at": "2025-01-01T00:00:00Z",
            "custom_field": {"keep": True},
        }]})
        envelope = self.ingest(record)
        self.assert_success(envelope, "Applied")
        document = register_document(self.root)
        self.assertEqual(len(document["meetings"]), 1)
        entry = document["meetings"][0]
        self.assertEqual(entry["meeting_id"], record["meeting_id"])
        self.assertEqual(entry["id"], "legacy-zoom-id")
        self.assertEqual(entry["custom_field"], {"keep": True})
        self.assertEqual(entry["duration"], 0.25)
        self.assertEqual(entry["duration_minutes"], 0.25)
        self.assertEqual(entry["dateString"], "2026-10-18T17:45:10.25Z")
        self.assertEqual(entry["first_synced_at"], "2025-01-01T00:00:00Z")

    def test_legacy_time_and_duration_are_preserved_by_date_only_reingest(self):
        record = valid_record(1)
        self.write_register({"meetings": [{
            "meeting_id": record["meeting_id"],
            "source": {"system": "ticnote", "external_id": record["source"]["external_id"]},
            "title": "Legacy title",
            "date": 1234567890123,
            "dateString": "2026-10-21 00:00:00",
            "duration": 7,
            "duration_minutes": 7,
        }]})
        self.assert_success(self.ingest(record), "Applied")
        entry = register_document(self.root)["meetings"][0]
        self.assertEqual(entry["date"], 1234567890123)
        self.assertEqual(entry["dateString"], "2026-10-21 00:00:00")
        self.assertEqual(entry["duration_minutes"], 7)
        self.assertEqual(entry["meeting_date"], "2026-10-21")

    def test_ambiguous_identity_and_conflicting_source_stop_before_writes(self):
        record = valid_record(6)
        duplicate = {"meeting_id": "ticnote-tn-900007",
                     "source": {"system": "ticnote", "external_id": "tn-900007"}}
        other = {"meeting_id": "ticnote-tn-900007-other",
                 "source": {"system": "ticnote", "external_id": "tn-900007"}}
        self.write_register({"meetings": [duplicate, other]})
        original = self.register_path.read_bytes()
        self.assert_rejected(self.ingest(record))
        self.assertEqual(self.register_path.read_bytes(), original)
        self.assertEqual(list((self.root / "memory/evidence").glob("topics/**/*")), [])

        self.write_register({"meetings": [{
            "meeting_id": record["meeting_id"],
            "source": {"system": "zoom", "external_id": record["source"]["external_id"]}}]})
        original = self.register_path.read_bytes()
        self.assert_rejected(self.ingest(record))
        self.assertEqual(self.register_path.read_bytes(), original)

    def test_account_binding_conflict_stops_before_writes(self):
        record = valid_record(6)
        self.assertIsNotNone(record["source"]["account_ref"])
        self.write_register({"meetings": [{
            "meeting_id": record["meeting_id"],
            "source": {"system": "ticnote", "account_ref": "acct-other",
                       "external_id": record["source"]["external_id"]}}]})
        original = self.register_path.read_bytes()
        self.assert_rejected(self.ingest(record))
        self.assertEqual(self.register_path.read_bytes(), original)

    def test_observed_start_basis_is_accepted_for_known_timestamp(self):
        record = valid_record(7)
        self.assertFalse(record["start"]["estimated"])
        self.assertEqual(record["start"]["basis"], ["provider-datestring-zone"])
        self.assert_success(self.ingest(record), "Applied")
        entry = register_document(self.root)["meetings"][0]
        self.assertEqual(entry["dateString"], "2026-10-21T10:00:00Z")
        self.assertFalse(entry["start_estimated"])

    def test_matched_raw_meeting_id_and_paths_are_preserved(self):
        record = valid_record(0)  # fireflies, external ff-900001, meeting_id fireflies_ff-900001
        self.assert_success(self.ingest(record), "Applied")
        entry = register_document(self.root)["meetings"][0]
        old_paths = {key: entry.get(key) for key in
                     ("summary_path", "transcript_path", "transcript_raw_path")}
        self.assertTrue(old_paths["summary_path"])
        entry["meeting_id"] = record["source"]["external_id"]
        document = register_document(self.root)
        document["meetings"][0] = entry
        self.write_register(document)

        again = self.ingest(record)
        self.assert_success(again)
        self.assertEqual(again["data"]["meeting_id"], record["source"]["external_id"])
        after = register_document(self.root)["meetings"][0]
        self.assertEqual(after["meeting_id"], record["source"]["external_id"])
        for key, value in old_paths.items():
            self.assertEqual(after.get(key), value)

    def test_reverse_alias_match_keeps_prefixed_identity_and_paths(self):
        record = valid_record(0)
        prefixed = copy.deepcopy(record)
        prefixed["meeting_id"] = "fireflies_abc-fireflies"
        prefixed["source"]["external_id"] = "abc-fireflies"
        self.assert_success(self.ingest(prefixed), "Applied")
        entry = register_document(self.root)["meetings"][0]
        paths = {key: entry.get(key) for key in
                 ("summary_path", "transcript_path", "transcript_raw_path")}
        incoming = copy.deepcopy(prefixed)
        incoming["meeting_id"] = "abc-fireflies"
        again = self.ingest(incoming)
        self.assert_success(again, "Unchanged")
        after = register_document(self.root)["meetings"][0]
        self.assertEqual(after["meeting_id"], "fireflies_abc-fireflies")
        for key, value in paths.items():
            self.assertEqual(after.get(key), value)

    def test_id_only_legacy_alias_keeps_id_and_derives_stable_identity(self):
        record = valid_record(0)
        external = record["source"]["external_id"]
        self.write_register({"meetings": [{"id": f"fireflies_{external}",
                                           "title": "Legacy id-only entry"}]})
        envelope = self.ingest(record)
        self.assert_success(envelope, "Applied")
        entry = register_document(self.root)["meetings"][0]
        self.assertEqual(entry["id"], f"fireflies_{external}")
        self.assertEqual(entry["meeting_id"], f"fireflies_{external}")
        self.assertEqual(envelope["data"]["meeting_id"], f"fireflies_{external}")

    def test_unknown_unsourced_identity_is_not_adopted(self):
        record = valid_record(2)  # zoom
        record["meeting_id"] = "bare-id"
        record["source"]["external_id"] = "abc"
        self.write_register({"meetings": [{"meeting_id": "bare-id",
                                           "title": "Unsourced human entry"}]})
        original = self.register_path.read_bytes()
        self.assert_rejected(self.ingest(record))
        self.assertEqual(self.register_path.read_bytes(), original)

    def test_source_unknown_fields_survive_targeted_update(self):
        record = valid_record(6)
        account = record["source"]["account_ref"]
        self.write_register({"meetings": [{
            "meeting_id": record["meeting_id"],
            "source": {"system": "ticnote", "account_ref": account,
                       "external_id": record["source"]["external_id"],
                       "vendor_binding": {"keep": True}},
        }]})
        self.assert_success(self.ingest(record), "Applied")
        source = register_document(self.root)["meetings"][0]["source"]
        self.assertEqual(source["vendor_binding"], {"keep": True})
        self.assertEqual(source["account_ref"], account)

    def test_identical_reingest_is_unchanged_and_preserves_accepted_content(self):
        self.write_catalogs(topics=[{"id": "catalogue-topic", "title": "Synthetic"}])
        record = valid_record(6)
        first = self.ingest(record, topic_slug="catalogue-topic")
        self.assert_success(first, "Applied")
        document = register_document(self.root)
        entry = document["meetings"][0]
        entry["content_status"] = "accepted"
        entry["review_recommended"] = False
        entry["review_reason"] = "human-accepted"
        self.write_register(document)
        accepted_bytes = self.register_path.read_bytes()
        summary_before = (self.root / entry["summary_path"]).read_bytes()

        second = self.ingest(record, topic_slug="catalogue-topic")
        self.assert_success(second, "Unchanged")
        self.assertFalse(second["data"]["changed"])
        self.assertEqual(self.register_path.read_bytes(), accepted_bytes)
        self.assertEqual((self.root / entry["summary_path"]).read_bytes(), summary_before)


class PartialUpdateTests(WriterTestBase):
    def setUp(self):
        super().setUp()
        self.write_catalogs(
            topics=[{"id": "catalogue-topic", "title": "Synthetic"}],
            projects=[{"id": "catalogue-project", "title": "Synthetic"}])

    def apply_full(self, record, **options):
        self.assert_success(self.ingest(record, **options), "Applied")
        return register_document(self.root)["meetings"][0]

    def snapshot(self, entry):
        return {key: (self.root / entry[key]).read_bytes()
                for key in ("summary_path", "transcript_path", "transcript_raw_path")
                if entry.get(key)}

    def partial_cli(self, record, *extra, lease=None):
        lease = lease or self.acquire()
        return run_cli(
            self.lock_root,
            ["--record", "-", "--workspace", str(self.root), "--lease-id", lease,
             *extra],
            stdin=json.dumps(record).encode("utf-8"))

    def test_summary_only_partial_retains_clean_and_raw(self):
        record = valid_record(0)
        entry = self.apply_full(record)
        paths = {key: entry[key] for key in
                 ("summary_path", "transcript_path", "transcript_raw_path")}
        before = self.snapshot(entry)
        partial = copy.deepcopy(record)
        partial["artifacts"]["transcript_md"] = None
        partial["artifacts"]["transcript_raw_md"] = None
        code, envelope, _ = self.partial_cli(partial)
        self.assertEqual(code, 0)
        after = register_document(self.root)["meetings"][0]
        for key, value in paths.items():
            self.assertEqual(after[key], value)
        self.assertEqual(self.snapshot(after), before)

    def test_transcript_and_raw_partial_retains_summary(self):
        record = valid_record(0)
        entry = self.apply_full(record)
        paths = {key: entry[key] for key in
                 ("summary_path", "transcript_path", "transcript_raw_path")}
        before = self.snapshot(entry)
        partial = copy.deepcopy(record)
        partial["artifacts"]["summary_md"] = None
        code, envelope, _ = self.partial_cli(partial)
        self.assertEqual(code, 0)
        after = register_document(self.root)["meetings"][0]
        for key, value in paths.items():
            self.assertEqual(after[key], value)
        self.assertEqual(self.snapshot(after), before)

    def test_accepted_content_survives_identical_partial_update(self):
        record = valid_record(6)
        entry = self.apply_full(record, topic_slug="catalogue-topic")
        document = register_document(self.root)
        document["meetings"][0]["content_status"] = "accepted"
        document["meetings"][0]["review_recommended"] = False
        self.write_register(document)
        accepted = self.register_path.read_bytes()
        partial = copy.deepcopy(record)
        partial["artifacts"]["transcript_md"] = None
        partial["artifacts"]["transcript_raw_md"] = None
        code, envelope, _ = self.partial_cli(partial)
        self.assertEqual(code, 0)
        self.assertEqual(self.register_path.read_bytes(), accepted)
        after = register_document(self.root)["meetings"][0]
        self.assertEqual(after["content_status"], "accepted")
        self.assertFalse(after["review_recommended"])

    def test_human_derived_registered_clean_survives_summary_only(self):
        record = valid_record(0)
        entry = self.apply_full(record)
        clean_path = entry["transcript_path"]
        clean_before = (self.root / clean_path).read_bytes()
        ownership = read_json(self.root / "data/meeting-desk/ownership.json")
        ownership["files"] = [item for item in ownership["files"]
                              if item["path"] != clean_path]
        (self.root / "data/meeting-desk/ownership.json").write_text(
            json.dumps(ownership), encoding="utf-8")
        partial = copy.deepcopy(record)
        partial["artifacts"]["transcript_md"] = None
        partial["artifacts"]["transcript_raw_md"] = None
        code, envelope, _ = self.partial_cli(partial)
        self.assertEqual(code, 0)
        after = register_document(self.root)["meetings"][0]
        self.assertEqual(after["transcript_path"], clean_path)
        self.assertEqual((self.root / clean_path).read_bytes(), clean_before)

    def test_partial_explicit_reroute_rejects_before_mutation(self):
        record = valid_record(0)
        entry = self.apply_full(record)
        paths = {key: entry[key] for key in
                 ("summary_path", "transcript_path", "transcript_raw_path")}
        before = self.snapshot(entry)
        register_before = self.register_path.read_bytes()
        ownership_path = self.root / "data/meeting-desk/ownership.json"
        ownership_before = ownership_path.read_bytes()
        partial = copy.deepcopy(record)
        partial["artifacts"]["transcript_md"] = None
        partial["artifacts"]["transcript_raw_md"] = None
        code, envelope, _ = self.partial_cli(partial, "--topic", "catalogue-topic")
        self.assertEqual(code, 2)
        self.assertEqual(envelope["state"], "Rejected")
        self.assertEqual(self.register_path.read_bytes(), register_before)
        self.assertEqual(ownership_path.read_bytes(), ownership_before)
        after = register_document(self.root)["meetings"][0]
        for key, value in paths.items():
            self.assertEqual(after[key], value)
        self.assertEqual(self.snapshot(after), before)
        self.assertFalse(any(self.journal_dir().glob("*.journal.json")))

    def test_all_null_artifacts_rejects_without_deletion(self):
        record = valid_record(0)
        entry = self.apply_full(record)
        before = self.snapshot(entry)
        empty = copy.deepcopy(record)
        empty["artifacts"] = {"summary_md": None, "transcript_md": None,
                              "transcript_raw_md": None}
        code, envelope, _ = self.partial_cli(empty)
        self.assertEqual(code, 2)
        self.assertEqual(envelope["state"], "Rejected")
        after = register_document(self.root)["meetings"][0]
        self.assertEqual(self.snapshot(after), before)

    def test_raw_change_on_partial_update_is_still_rejected(self):
        record = valid_record(0)
        entry = self.apply_full(record)
        raw_path = entry["transcript_raw_path"]
        raw_before = (self.root / raw_path).read_bytes()
        changed = copy.deepcopy(record)
        changed["artifacts"]["transcript_md"] = None
        changed["artifacts"]["transcript_raw_md"] = "different raw\n"
        code, envelope, _ = self.partial_cli(changed)
        self.assertEqual(code, 2)
        self.assertEqual(envelope["state"], "Rejected")
        self.assertEqual((self.root / raw_path).read_bytes(), raw_before)


class RoutingTests(WriterTestBase):
    def setUp(self):
        super().setUp()
        self.write_catalogs(
            topics=[{"id": "catalogue-topic", "title": "Synthetic topic",
                     "status": "active",
                     "events": [{"id": "synthetic-event", "title": "Synthetic event"}]},
                    {"id": "inactive-topic", "title": "Inactive", "status": "archived"}],
            projects=[{"id": "catalogue-project", "title": "Synthetic project",
                       "status": "active",
                       "events": [{"id": "project-event", "title": "Project event"}]}])

    def test_trusted_topic_and_event_routing(self):
        record = valid_record(1)
        envelope = self.ingest(record, topic_slug="catalogue-topic",
                               event_slug="synthetic-event")
        self.assert_success(envelope, "Applied")
        entry = register_document(self.root)["meetings"][0]
        self.assertEqual(entry["classification_status"], "classified")
        self.assertEqual(entry["classification_basis"], ["operator-choice"])
        self.assertEqual(entry["topic_slug"], "catalogue-topic")
        self.assertEqual(entry["event_slug"], "synthetic-event")
        self.assertTrue(entry["summary_path"].startswith(
            "memory/evidence/topics/catalogue-topic/events/synthetic-event/meetings/"))

    def test_topic_wins_double_tagging(self):
        envelope = self.ingest(valid_record(1), topic_slug="catalogue-topic",
                               project_slug="catalogue-project")
        self.assert_success(envelope, "Applied")
        entry = register_document(self.root)["meetings"][0]
        self.assertEqual(entry["topic_slug"], "catalogue-topic")
        self.assertIsNone(entry["project_slug"])

    def test_unknown_or_inactive_catalog_choices_are_rejected(self):
        for options in ({"topic_slug": "missing-topic"},
                        {"topic_slug": "inactive-topic"},
                        {"project_slug": "missing-project"},
                        {"event_slug": "synthetic-event"}):
            with self.subTest(options=options):
                envelope = self.ingest(valid_record(1), **options)
                self.assert_rejected(envelope)
                self.assertFalse(self.register_path.exists())

    def test_event_must_belong_to_the_selected_owner(self):
        self.assert_rejected(self.ingest(valid_record(1), topic_slug="catalogue-topic",
                                         event_slug="project-event"))

    def test_untrusted_record_hint_never_routes(self):
        record = valid_record(0)  # hint names an existing catalogue topic
        self.assertIsNotNone(record["classification_hint"]["topic_slug"])
        envelope = self.ingest(record)
        self.assert_success(envelope, "Applied")
        entry = register_document(self.root)["meetings"][0]
        self.assertEqual(entry["classification_status"], "unmapped")
        self.assertIsNone(entry["topic_slug"])
        self.assertTrue(entry["summary_path"].startswith(
            "memory/evidence/meetings/unrouted/"))

    def test_existing_human_routing_survives_reingest_without_selection(self):
        record = valid_record(1)
        self.assert_success(self.ingest(record, topic_slug="catalogue-topic"), "Applied")
        first = register_document(self.root)["meetings"][0]
        self.assert_success(self.ingest(record), "Unchanged")
        second = register_document(self.root)["meetings"][0]
        self.assertEqual(second["topic_slug"], "catalogue-topic")
        self.assertEqual(second["classification_status"], "classified")
        self.assertEqual(second["summary_path"], first["summary_path"])

    def test_dry_run_plan_shows_reroute_and_cleanup_without_mutation(self):
        record = valid_record(0)
        self.assert_success(self.ingest(record), "Applied")
        entry = register_document(self.root)["meetings"][0]
        old_paths = set(self.artifacts(entry))
        register_before = self.register_path.read_bytes()
        plan = self.ingest(record, topic_slug="catalogue-topic", dry_run=True)
        self.assert_success(plan, "DryRun")
        self.assertEqual(plan["data"]["plan"]["routing"]["topic_slug"], "catalogue-topic")
        self.assertEqual(set(plan["data"]["plan"]["delete_after_register"]), old_paths)
        self.assertEqual(self.register_path.read_bytes(), register_before)
        for relative in old_paths:
            self.assertTrue((self.root / relative).exists())

    def test_reroute_moves_registered_artifacts_and_updates_register(self):
        record = valid_record(0)
        first = self.ingest(record)
        self.assert_success(first, "Applied")
        old = register_document(self.root)["meetings"][0]
        old_paths = self.artifacts(old)
        for relative in old_paths:
            self.assertTrue((self.root / relative).is_file())

        second = self.ingest(record, topic_slug="catalogue-topic")
        self.assert_success(second, "Applied")
        new = register_document(self.root)["meetings"][0]
        self.assertTrue(new["summary_path"].startswith(
            "memory/evidence/topics/catalogue-topic/meetings/"))
        for relative in old_paths:
            self.assertFalse((self.root / relative).exists())
        ownership = read_json(self.root / "data/meeting-desk/ownership.json")
        paths = {item["path"] for item in ownership["files"]}
        self.assertEqual(paths, set(self.artifacts(new)))


class SafetyTests(WriterTestBase):
    def test_opaque_identities_apply_without_path_semantics(self):
        pack = fixture_pack()
        wanted = ["../escape", "zoom-AbC/def+ghi==", "meeting id with internal space"]
        records = [record for record in pack["valid_records"]
                   if record["meeting_id"] in wanted]
        self.assertEqual(len(records), len(wanted))
        for record in records:
            with self.subTest(meeting_id=record["meeting_id"]):
                envelope = self.ingest(record)
                if (envelope.get("success") and envelope.get("state") == "Plan"
                        and record["source"]["system"] == "manual"):
                    envelope = self.ingest(
                        record, apply_plan_sha256=envelope["data"]["plan_sha256"])
                self.assert_success(envelope, "Applied")
                entry = [item for item in register_document(self.root)["meetings"]
                         if item["meeting_id"] == record["meeting_id"]][0]
                self.assertEqual(entry["meeting_id"], record["meeting_id"])
                for key in ("summary_path", "transcript_path", "transcript_raw_path"):
                    relative = entry.get(key)
                    if relative:
                        self.assertTrue(relative.startswith("memory/evidence/"))
                        self.assertTrue((self.root / relative).is_file())
                self.assertFalse((self.root.parent / "escape.summary.md").exists())
                self.assertFalse((self.root.parent / "escape.transcript.md").exists())
                summary = (self.root / entry["summary_path"]).read_text(encoding="utf-8")
                self.assertIn(json.dumps(record["meeting_id"], ensure_ascii=False), summary)
                if entry.get("transcript_raw_path"):
                    self.assertEqual(
                        (self.root / entry["transcript_raw_path"]).read_bytes(),
                        record["artifacts"]["transcript_raw_md"].encode("utf-8"))

    def test_opaque_identity_is_exact_in_journal_and_recovery(self):
        pack = fixture_pack()
        record = [item for item in pack["valid_records"]
                  if item["meeting_id"] == "zoom-AbC/def+ghi=="][0]
        lease = self.acquire()
        record_path = write_record(self.root / "opaque-record.json", record)
        completed = subprocess.run(
            [sys.executable, str(CRASH_DRIVER), str(WRITER_SCRIPT), str(self.root),
             str(record_path), lease, str(self.lock_root), "1"],
            capture_output=True, check=False)
        self.assertEqual(completed.returncode, 9)
        journal = list(self.journal_dir().glob("*.journal.json"))[0]
        body = self.writer._read_journal(journal)
        self.assertEqual(body["meeting_id"], record["meeting_id"])
        recovered = self.writer.recover_workspace(
            self.root, lease_id=lease, trusted_workspace_lock_root=self.lock_root)
        self.assertTrue(recovered["success"], recovered)
        entry = register_document(self.root)["meetings"][0]
        self.assertEqual(entry["meeting_id"], record["meeting_id"])
        frontmatter = (self.root / entry["summary_path"]).read_text(encoding="utf-8")
        self.assertIn(json.dumps(record["meeting_id"], ensure_ascii=False), frontmatter)
        self.assertEqual((self.root / entry["transcript_raw_path"]).read_bytes(),
                         record["artifacts"]["transcript_raw_md"].encode("utf-8"))

    def test_raw_only_transcript_is_published_and_registered(self):
        record = valid_record(0)
        record["artifacts"]["transcript_md"] = None
        self.assertTrue(record["artifacts"]["transcript_raw_md"])
        self.assert_success(self.ingest(record), "Applied")
        entry = register_document(self.root)["meetings"][0]
        self.assertIsNone(entry["transcript_path"])
        self.assertTrue(entry["transcript_raw_path"])
        self.assertEqual((self.root / entry["transcript_raw_path"]).read_bytes(),
                         record["artifacts"]["transcript_raw_md"].encode("utf-8"))

    def test_raw_only_partial_update_retains_clean_transcript(self):
        record = valid_record(0)
        self.assert_success(self.ingest(record), "Applied")
        first = register_document(self.root)["meetings"][0]
        raw_path = first["transcript_raw_path"]
        clean_path = first["transcript_path"]
        clean_before = (self.root / clean_path).read_bytes()
        raw_only = copy.deepcopy(record)
        raw_only["artifacts"]["transcript_md"] = None
        self.assert_success(self.ingest(raw_only), "Unchanged")
        entry = register_document(self.root)["meetings"][0]
        self.assertEqual(entry["transcript_raw_path"], raw_path)
        self.assertEqual(entry["transcript_path"], clean_path)
        self.assertEqual((self.root / raw_path).read_bytes(),
                         record["artifacts"]["transcript_raw_md"].encode("utf-8"))
        self.assertEqual((self.root / clean_path).read_bytes(), clean_before)

    def test_existing_human_file_is_never_clobbered(self):
        record = valid_record(1)
        normalized = self.writer.validate_record(record)
        stem = self.writer._file_stem(normalized)
        target = (self.root / "memory/evidence/meetings/unrouted" /
                  f"{stem}.summary.md")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"human summary, not writer-owned\n")
        envelope = self.ingest(record)
        self.assert_rejected(envelope)
        self.assertEqual(target.read_bytes(), b"human summary, not writer-owned\n")
        self.assertFalse(self.register_path.exists())

    def test_identical_unowned_file_is_not_silently_adopted(self):
        record = valid_record(1)
        normalized = self.writer.validate_record(record)
        routing = self.writer._resolve_routing(self.root, None, None, None)
        stem = self.writer._file_stem(normalized)
        paths = self.writer._entry_paths(routing, stem, normalized["artifacts"])
        payload = self.writer._artifact_payload(normalized, routing, paths)
        target = self.root / paths["summary_path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload[paths["summary_path"]])
        before = target.read_bytes()
        envelope = self.ingest(record)
        self.assert_success(envelope, "Applied")
        self.assertEqual(target.read_bytes(), before)
        ownership = read_json(self.root / "data/meeting-desk/ownership.json")
        self.assertNotIn(paths["summary_path"], {item["path"] for item in ownership["files"]})
        changed = copy.deepcopy(record)
        changed["artifacts"]["summary_md"] = "changed synthetic summary\n"
        self.assert_rejected(self.ingest(changed))
        self.assertEqual(target.read_bytes(), before)

    def test_foreign_modification_of_writer_owned_artifact_stops(self):
        record = valid_record(6)
        self.assert_success(self.ingest(record), "Applied")
        entry = register_document(self.root)["meetings"][0]
        target = self.root / entry["summary_path"]
        target.write_bytes(target.read_bytes() + b"externally appended\n")
        changed = copy.deepcopy(record)
        changed["artifacts"]["summary_md"] = "different synthetic summary\n"
        self.assert_rejected(self.ingest(changed))
        self.assertIn(b"externally appended", target.read_bytes())

    def test_raw_transcript_is_never_replaced(self):
        record = valid_record(6)
        self.assert_success(self.ingest(record), "Applied")
        entry = register_document(self.root)["meetings"][0]
        raw_path = self.root / entry["transcript_raw_path"]
        before = raw_path.read_bytes()
        changed = copy.deepcopy(record)
        changed["artifacts"]["transcript_md"] = "Robin: corrected\n"
        changed["artifacts"]["transcript_raw_md"] = "Robin: other raw\n"
        self.assert_rejected(self.ingest(changed))
        self.assertEqual(raw_path.read_bytes(), before)

    def test_path_traversal_zone_ads_and_windows_aliases_fail_closed(self):
        unsafe = [
            "memory/evidence/../outside.md",
            "memory/evidence/meetings/a:b.md",
            "memory/evidence/meetings/con.md",
            "memory/evidence/meetings/trailing.md ",
            "memory/evidence/meetings/sub/../escape.md",
            "docs/outside.md",
            "memory/evidence/meetings/CON.txt",
            "memory/evidence/meetings/bad\\name.md",
        ]
        for relative in unsafe:
            with self.subTest(relative=relative):
                with self.assertRaises(self.writer._Reject):
                    self.writer._safe_target(self.root, relative, "test target")

    def test_symlink_or_junction_escape_fails_closed(self):
        outside = tempfile.TemporaryDirectory(prefix="meeting-outside-")
        self.addCleanup(outside.cleanup)
        link = self.root / "memory/evidence/meetings/linked"
        try:
            link.symlink_to(outside.name, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                self.skipTest("symlinks/junctions unavailable on this platform")
            completed = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), outside.name],
                capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
            if completed.returncode != 0:
                self.skipTest("symlinks/junctions unavailable on this platform")
        with self.assertRaises(self.writer._Reject):
            self.writer._safe_target(self.root, "memory/evidence/meetings/linked/x.md",
                                     "test target")

    def test_hardlink_alias_fails_closed(self):
        target = self.root / "memory/evidence/meetings/hardlink.md"
        target.write_bytes(b"x")
        alias = self.root / "memory/evidence/meetings/hardlink-alias.md"
        try:
            alias.hardlink_to(target)
        except OSError:
            self.skipTest("hardlinks unavailable on this platform")
        with self.assertRaises(self.writer._Reject):
            self.writer._safe_target(self.root, "memory/evidence/meetings/hardlink-alias.md",
                                     "test target")


class JournalAndRecoveryTests(WriterTestBase):
    def test_orphan_stage_directory_blocks_and_preserves_bytes(self):
        orphan = self.journal_dir() / "orphan-stage/stage"
        orphan.mkdir(parents=True)
        payload = orphan / "000.bin"
        payload.write_bytes(b"unreachable foreign stage bytes")
        envelope = self.ingest(valid_record(1))
        self.assert_envelope(envelope)
        self.assertFalse(envelope["success"])
        self.assertEqual(envelope["state"], "RecoveryRequired")
        self.assertEqual(payload.read_bytes(), b"unreachable foreign stage bytes")
        self.assertFalse(self.register_path.exists())
        recovered = self.writer.recover_workspace(
            self.root, lease_id=self.lease,
            trusted_workspace_lock_root=self.lock_root)
        self.assertEqual(recovered["state"], "RecoveryRequired")
        self.assertEqual(payload.read_bytes(), b"unreachable foreign stage bytes")

    def test_junction_in_journal_directory_blocks_and_keeps_external_bytes(self):
        outside = tempfile.TemporaryDirectory(prefix="meeting-journal-outside-")
        self.addCleanup(outside.cleanup)
        keep = Path(outside.name) / "keepme.txt"
        keep.write_bytes(b"external keepme\n")
        self.journal_dir().mkdir(parents=True)
        link = self.journal_dir() / "escape"
        if os.name != "nt":
            self.skipTest("NTFS junction test is Windows-only")
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), outside.name],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
        if completed.returncode != 0:
            self.skipTest("junction creation unavailable")
        self.addCleanup(lambda: os.rmdir(link))
        envelope = self.ingest(valid_record(1))
        self.assert_envelope(envelope)
        self.assertFalse(envelope["success"])
        self.assertEqual(envelope["state"], "RecoveryRequired")
        self.assertEqual(keep.read_bytes(), b"external keepme\n")
        self.assertFalse(self.register_path.exists())
        recovered = self.writer.recover_workspace(
            self.root, lease_id=self.lease, trusted_workspace_lock_root=self.lock_root)
        self.assertEqual(recovered["state"], "RecoveryRequired")
        self.assertEqual(keep.read_bytes(), b"external keepme\n")

    def test_unexpected_journal_directory_file_blocks_and_survives(self):
        self.journal_dir().mkdir(parents=True)
        human = self.journal_dir() / "human-note.txt"
        human.write_bytes(b"human bytes\n")
        envelope = self.ingest(valid_record(1))
        self.assertEqual(envelope["state"], "RecoveryRequired")
        self.assertEqual(human.read_bytes(), b"human bytes\n")
        self.assertFalse(self.register_path.exists())

    def test_corrupt_journal_blocks_mutation_and_is_reported(self):
        self.assert_success(self.ingest(valid_record(1)), "Applied")
        self.journal_dir().mkdir(parents=True, exist_ok=True)
        broken = self.journal_dir() / "broken.journal.json"
        broken.write_text('{"journal": {}, "integrity": {"algorithm": "sha256", '
                          '"digest": "deadbeef"}}', encoding="utf-8")
        record = valid_record(2)
        envelope = self.ingest(record)
        self.assert_envelope(envelope)
        self.assertFalse(envelope["success"])
        self.assertEqual(envelope["state"], "RecoveryRequired")
        self.assertFalse(any("zoom-00000000" in path.name
                             for path in (self.root / "memory/evidence").glob("**/*")))

        code, inspected, _ = run_cli(
            self.lock_root, ["--inspect", "--workspace", str(self.root)])
        self.assertEqual(code, 0)
        self.assertTrue(inspected["data"]["recovery_required"])
        self.assertEqual(inspected["data"]["journals"][0]["state"], "corrupt")

        code, recovered, _ = run_cli(
            self.lock_root, ["--recover", "--workspace", str(self.root),
                             "--lease-id", self.lease])
        self.assertEqual(code, 3)
        self.assertEqual(recovered["state"], "RecoveryRequired")
        self.assertTrue(broken.exists())

    def test_crash_after_each_publication_step_is_recoverable(self):
        record = valid_record(0)
        total_steps = 5  # summary, transcript, raw, ownership, register
        for crash_after in range(1, total_steps + 1):
            with self.subTest(crash_after=crash_after):
                temporary = tempfile.TemporaryDirectory(prefix=f"meeting-crash-{crash_after}-")
                self.addCleanup(temporary.cleanup)
                root = Path(temporary.name).resolve()
                (root / "memory/evidence/meetings").mkdir(parents=True)
                lease = f"crash-{uuid4().hex[:10]}"
                acquire(self.lock_root, root, lease)
                self.addCleanup(release, self.lock_root, root, lease)
                record_path = write_record(root / "record.json", record)
                completed = subprocess.run(
                    [sys.executable, str(CRASH_DRIVER), str(WRITER_SCRIPT), str(root),
                     str(record_path), lease, str(self.lock_root), str(crash_after)],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
                self.assertEqual(completed.returncode, 9, completed.stderr)
                self.assertTrue(list((root / "data/meeting-desk/journal").glob(
                    "*.journal.json")))

                blocked = self.writer.ingest_record(
                    record, workspace_root=root, lease_id=lease,
                    trusted_workspace_lock_root=self.lock_root)
                self.assertEqual(blocked["state"], "RecoveryRequired")

                inspected = self.writer.inspect_workspace(root)
                self.assertTrue(inspected["data"]["recovery_required"])
                recovered = self.writer.recover_workspace(
                    root, lease_id=lease,
                    trusted_workspace_lock_root=self.lock_root)
                self.assertTrue(recovered["success"], recovered)
                self.assertEqual(recovered["state"], "Recovered")

                document = register_document(root)
                entry = document["meetings"][0]
                for relative in self.artifacts(entry):
                    self.assertTrue((root / relative).is_file(), relative)
                ownership = read_json(root / "data/meeting-desk/ownership.json")
                self.assertEqual({item["path"] for item in ownership["files"]},
                                 set(self.artifacts(entry)))
                self.assertEqual(list((root / "data/meeting-desk/journal").glob(
                    "*.journal.json")), [])
                again = self.writer.ingest_record(
                    record, workspace_root=root, lease_id=lease,
                    trusted_workspace_lock_root=self.lock_root)
                self.assert_success(again, "Unchanged")

    def test_recovery_refuses_foreign_bytes(self):
        temporary = tempfile.TemporaryDirectory(prefix="meeting-foreign-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        (root / "memory/evidence/meetings").mkdir(parents=True)
        lease = f"foreign-{uuid4().hex[:10]}"
        acquire(self.lock_root, root, lease)
        self.addCleanup(release, self.lock_root, root, lease)
        record = valid_record(0)
        record_path = write_record(root / "record.json", record)
        completed = subprocess.run(
            [sys.executable, str(CRASH_DRIVER), str(WRITER_SCRIPT), str(root),
             str(record_path), lease, str(self.lock_root), "1"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
        self.assertEqual(completed.returncode, 9, completed.stderr)

        normalized = self.writer.validate_record(record)
        stem = self.writer._file_stem(normalized)
        foreign = root / "memory/evidence/meetings/unrouted" / f"{stem}.summary.md"
        foreign.parent.mkdir(parents=True, exist_ok=True)
        foreign.write_bytes(b"foreign bytes\n")
        recovered = self.writer.recover_workspace(
            root, lease_id=lease, trusted_workspace_lock_root=self.lock_root)
        self.assertEqual(recovered["state"], "RecoveryRequired")
        self.assertEqual(foreign.read_bytes(), b"foreign bytes\n")
        self.assertTrue(list((root / "data/meeting-desk/journal").glob("*.journal.json")))

    def test_recovery_unsafe_target_directory_returns_exit_three(self):
        temporary = tempfile.TemporaryDirectory(prefix="meeting-unsafe-dir-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        (root / "memory/evidence/meetings").mkdir(parents=True)
        lease = f"unsafe-{uuid4().hex[:10]}"
        acquire(self.lock_root, root, lease)
        self.addCleanup(release, self.lock_root, root, lease)
        record_path = write_record(root / "record.json", valid_record(0))
        completed = subprocess.run(
            [sys.executable, str(CRASH_DRIVER), str(WRITER_SCRIPT), str(root),
             str(record_path), lease, str(self.lock_root), "1"],
            capture_output=True, check=False)
        self.assertEqual(completed.returncode, 9)
        journals = list((root / "data/meeting-desk/journal").glob("*.journal.json"))
        self.assertEqual(len(journals), 1)
        body = self.writer._read_journal(journals[0])
        target = root / body["steps"][1]["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.mkdir()
        recovered = self.writer.recover_workspace(
            root, lease_id=lease, trusted_workspace_lock_root=self.lock_root)
        self.assertEqual(recovered["state"], "RecoveryRequired")
        self.assertTrue(target.is_dir())
        self.assertTrue(journals[0].exists())

    def test_recovery_requires_an_owned_lease(self):
        temporary = tempfile.TemporaryDirectory(prefix="meeting-nolease-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        (root / "memory/evidence/meetings").mkdir(parents=True)
        lease = f"nolease-{uuid4().hex[:10]}"
        acquire(self.lock_root, root, lease)
        self.addCleanup(release, self.lock_root, root, lease)
        record_path = write_record(root / "record.json", valid_record(0))
        subprocess.run(
            [sys.executable, str(CRASH_DRIVER), str(WRITER_SCRIPT), str(root),
             str(record_path), lease, str(self.lock_root), "1"],
            capture_output=True, check=False)
        result = self.writer.recover_workspace(
            root, lease_id="not-the-owner",
            trusted_workspace_lock_root=self.lock_root)
        self.assertFalse(result["success"])
        self.assertEqual(result["state"], "Rejected")


class BuilderDelegationTests(WriterTestBase):
    def setUp(self):
        super().setUp()
        self.write_catalogs(topics=[{"id": "catalogue-topic", "title": "Synthetic"}])
        reference = self.root / "memory/references/topics/example/index.md"
        reference.parent.mkdir(parents=True, exist_ok=True)
        reference.write_text("# Synthetic entity reference\n", encoding="utf-8")
        self.record = valid_record(6)
        self.assert_success(self.ingest(self.record, topic_slug="catalogue-topic"),
                            "Applied")
        self.entry = register_document(self.root)["meetings"][0]
        self.raw_path = self.root / self.entry["transcript_raw_path"]
        self.out_path = self.root / self.entry["transcript_path"]
        self.corrections = self.root / "memory/evidence/topics/catalogue-topic/meetings/corr.json"
        self.corrections.write_text(json.dumps({
            "schema": "meeting-corrections/1",
            "pairs": [{"orig": "Robin", "corr": "Robin Example",
                       "cat": "entity", "ref": "memory/references/topics/example/index.md"}]}),
            encoding="utf-8")

    def builder_module(self):
        import importlib.util
        path = WRITER_SCRIPT.with_name("build_clean_transcript.py")
        spec = importlib.util.spec_from_file_location("meeting_builder_under_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def builder_args(self, *extra):
        return [str(self.builder_module().__file__), "--workspace", str(self.root),
                "--raw", str(self.root / self.entry["transcript_raw_path"]),
                "--corrections", str(self.corrections), "--out",
                str(self.root / self.entry["transcript_path"]),
                "--meeting-id", self.entry["meeting_id"],
                "--lease-id", self.lease, "--workspace-lock-root", str(self.lock_root),
                "--json", *extra]

    def run_builder(self, *extra):
        completed = subprocess.run([sys.executable, *self.builder_args(*extra)],
                                   capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, completed.stderr)
        return completed.returncode, json.loads(lines[0])

    def test_builder_publishes_only_the_registered_transcript(self):
        before_raw = self.raw_path.read_bytes()
        before_corrections = self.corrections.read_bytes()
        code, envelope = self.run_builder()
        self.assertEqual(code, 0, envelope)
        self.assertTrue(envelope["success"])
        self.assertEqual(envelope["action"], "build_clean_transcript")
        self.assertIn("Robin Example", self.out_path.read_text(encoding="utf-8"))
        self.assertEqual(self.raw_path.read_bytes(), before_raw)
        self.assertEqual(self.corrections.read_bytes(), before_corrections)
        entry = register_document(self.root)["meetings"][0]
        self.assertEqual(entry["content_status"], "provisional")
        self.assertTrue(entry["review_recommended"])

        code, again = self.run_builder()
        self.assertEqual(code, 0, again)
        self.assertEqual(again["state"], "Built")

        completed = subprocess.run([sys.executable, *self.builder_args("--check")],
                                   capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
        check = json.loads(completed.stdout)
        self.assertEqual(completed.returncode, 0, check)
        self.assertFalse(check["data"]["drift"])

    def test_builder_refuses_unregistered_output_and_unowned_file(self):
        other = self.root / "memory/evidence/topics/catalogue-topic/meetings/orphan.transcript.md"
        self.lease = self.acquire()
        args = self.builder_args()
        args[args.index("--out") + 1] = str(other)
        completed = subprocess.run([sys.executable, *args], capture_output=True,
                                   text=True, encoding="utf-8", errors="replace", check=False)
        self.assertEqual(completed.returncode, 2, completed.stdout)
        self.assertFalse(other.exists())

        self.out_path.write_bytes(b"human-owned transcript\n")
        code, envelope = self.run_builder()
        self.assertEqual(code, 2, envelope)
        self.assertFalse(envelope["success"])
        self.assertEqual(self.out_path.read_bytes(), b"human-owned transcript\n")

    def test_builder_requires_an_owned_lease(self):
        args = self.builder_args()
        args[args.index("--lease-id") + 1] = "not-the-owner"
        completed = subprocess.run([sys.executable, *args], capture_output=True,
                                   text=True, encoding="utf-8", errors="replace", check=False)
        self.assertEqual(completed.returncode, 2, completed.stdout)
        envelope = json.loads(completed.stdout)
        self.assertFalse(envelope["success"])
        self.assertEqual(envelope["state"], "Rejected")

    # -- OI #52 P2/P3 through the real builder CLI, writer and trusted guard --
    EXPECTED_HINTS = ("scripts/workspace_lock_guard.py", "scripts/invoke_workspace_lock.py",
                      ".agents/skills/workspace-lock")

    def snapshot(self):
        return {path.relative_to(self.root).as_posix(): path.read_bytes()
                for path in sorted(self.root.rglob("*")) if path.is_file()}

    def run_with(self, args, env=None):
        completed = subprocess.run(
            [sys.executable, *args], capture_output=True, text=True, encoding="utf-8",
            errors="replace", check=False,
            env=None if env is None else {**os.environ, **env})
        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, completed.stderr)
        return completed.returncode, json.loads(lines[0])

    def mistaken_parent(self):
        """A skills directory that merely *contains* an installation (the usual slip)."""
        parent = self.root / ".agents/skills"
        scripts = parent / "workspace-lock/scripts"
        scripts.mkdir(parents=True)
        for name in ("workspace_lock_guard.py", "invoke_workspace_lock.py"):
            (scripts / name).write_text(
                "from pathlib import Path\n"
                "Path(__file__).with_name('executed.marker').write_text('x')\n",
                encoding="utf-8")
        return parent

    def with_root(self, args, root):
        args = list(args)
        args[args.index("--workspace-lock-root") + 1] = str(root)
        return args

    def assert_actionable_root_error(self, message, parent):
        for expected in (*self.EXPECTED_HINTS, parent.as_posix()):
            self.assertIn(expected, message)
        self.assertNotIn("owned active workspace-lock lease", message)
        self.assertEqual(list(parent.rglob("executed.marker")), [])

    def test_iso_day_is_published_as_a_quoted_calendar_date(self):
        code, envelope = self.run_builder("--date", "2026-10-27", "--title", "Montag")
        self.assertEqual(code, 0, envelope)
        text = self.out_path.read_bytes().decode("utf-8")
        self.assertIn('title: "Montag"\ndate: "2026-10-27"\ndateString: "2026-10-27"\n', text)
        first = self.out_path.read_bytes()
        code, again = self.run_builder("--date", "2026-10-27", "--title", "Montag")
        self.assertEqual(code, 0, again)
        self.assertEqual(self.out_path.read_bytes(), first)
        completed = subprocess.run(
            [sys.executable, *self.builder_args("--date", "2026-10-27", "--title", "Montag",
                                                "--check")],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertFalse(json.loads(completed.stdout)["data"]["drift"])
        register = register_document(self.root)["meetings"][0]
        self.assertEqual(register["transcript_path"], self.entry["transcript_path"])
        # Epoch millis stay an integer through the same real seam.
        code, epoch = self.run_builder("--date", "1792483867000", "--title", "Montag")
        self.assertEqual(code, 0, epoch)
        self.assertIn('date: 1792483867000\ndateString: ""\n',
                      self.out_path.read_bytes().decode("utf-8"))

    def test_invalid_date_changes_nothing_through_the_real_writer(self):
        self.acquire()
        before = self.snapshot()
        for value in ("2026-02-30", "27.10.2026", "abc"):
            with self.subTest(value=value):
                code, envelope = self.run_builder("--date", value)
                self.assertEqual(code, 2, envelope)
                self.assertIn("--date", envelope["message"])
                self.assertEqual(self.snapshot(), before)
        target = self.root / "data/bad-date-template.json"
        (self.root / "data").mkdir(exist_ok=True)
        before = self.snapshot()
        code, envelope = self.run_with(
            [str(self.builder_module().__file__), "--workspace", str(self.root),
             "--init-template", str(target), "--lease-id", self.lease,
             "--workspace-lock-root", str(self.lock_root), "--date", "2026-02-30",
             "--json"])
        self.assertEqual(code, 2, envelope)
        self.assertFalse(target.exists())
        self.assertEqual(self.snapshot(), before)

    def test_publication_with_a_mistaken_lock_root_is_actionable_and_fails_closed(self):
        self.acquire()
        parent = self.mistaken_parent()
        before = self.snapshot()
        code, envelope = self.run_with(self.with_root(self.builder_args(), parent))
        self.assertEqual(code, 2, envelope)
        self.assertEqual(envelope["state"], "Rejected")
        self.assert_actionable_root_error(envelope["message"], parent)
        self.assertEqual(self.snapshot(), before)

    def test_operator_environment_mistake_is_diagnosed_like_the_flag(self):
        self.acquire()
        parent = self.mistaken_parent()
        args = self.builder_args()
        index = args.index("--workspace-lock-root")
        del args[index:index + 2]
        before = self.snapshot()
        for variable in ("WORKSPACE_LOCK_ROOT", "MEETING_DESK_WORKSPACE_LOCK_ROOT"):
            with self.subTest(variable=variable):
                env = {"WORKSPACE_LOCK_ROOT": "", "MEETING_DESK_WORKSPACE_LOCK_ROOT": "",
                       variable: str(parent)}
                code, envelope = self.run_with(args, env=env)
                self.assertEqual(code, 2, envelope)
                self.assert_actionable_root_error(envelope["message"], parent)
                self.assertEqual(self.snapshot(), before)

    def test_template_with_the_right_root_needs_the_owned_lease(self):
        (self.root / "data").mkdir(exist_ok=True)
        target = self.root / "data/template.json"
        script = str(self.builder_module().__file__)

        def args(lease):
            return [script, "--workspace", str(self.root), "--init-template", str(target),
                    "--lease-id", lease, "--workspace-lock-root", str(self.lock_root),
                    "--json"]

        code, envelope = self.run_with(args("not-the-owner"))
        self.assertEqual(code, 2, envelope)
        self.assertIn("owned active workspace-lock lease is required", envelope["message"])
        self.assertFalse(target.exists())
        self.acquire()
        code, envelope = self.run_with(args(self.lease))
        self.assertEqual(code, 0, envelope)
        self.assertTrue(target.is_file())

    def test_writer_cli_and_api_report_a_mistaken_lock_root(self):
        lease = self.acquire()
        parent = self.mistaken_parent()
        record = valid_record(1)
        before = self.snapshot()
        code, envelope, stderr = run_cli(
            parent, ["--record", "-", "--workspace", str(self.root), "--lease-id", lease],
            stdin=json.dumps(record).encode("utf-8"))
        self.assertEqual(code, 2, stderr)
        self.assert_rejected(envelope)
        self.assert_actionable_root_error(envelope["message"], parent)
        self.assertEqual(self.snapshot(), before)
        envelope = self.writer.publish_registered_transcript(
            "---\ntitle: x\n---\n\nBody\n", workspace_root=self.root, lease_id=lease,
            meeting_id=self.entry["meeting_id"], trusted_workspace_lock_root=parent)
        self.assert_rejected(envelope)
        self.assert_actionable_root_error(envelope["message"], parent)
        self.assertEqual(self.snapshot(), before)

    def test_missing_foreign_lease_keeps_the_lease_message_with_a_valid_root(self):
        for lease in ("not-the-owner", ""):
            with self.subTest(lease=lease):
                args = self.builder_args()
                args[args.index("--lease-id") + 1] = lease
                before = self.snapshot()
                code, envelope = self.run_with(args)
                self.assertEqual(code, 2, envelope)
                self.assertNotIn("scripts/workspace_lock_guard.py", envelope["message"])
                self.assertIn("lease", envelope["message"])
                self.assertEqual(self.snapshot(), before)


class RegisteredSummaryPublishTests(WriterTestBase):
    """OI #52 P1: ``publish_registered_summary`` replaces only the summary."""

    NEW_SUMMARY = ("---\ndocument_type: meeting-summary\nstatus: provisional\n---\n\n"
                   "# Reviewed summary\n\nDecision: ship it. Umlaut check: Müller, 日本語.\n")
    VOLATILE = {"content_status", "review_recommended", "review_reason", "last_synced_at"}

    def setUp(self):
        super().setUp()
        self.write_catalogs(topics=[{"id": "catalogue-topic", "title": "Synthetic"}])
        self.record = valid_record(6)
        self.assert_success(self.ingest(self.record, topic_slug="catalogue-topic"),
                            "Applied")
        self.entry = register_document(self.root)["meetings"][0]
        self.meeting_id = self.entry["meeting_id"]
        self.ownership_path = self.root / "data/meeting-desk/ownership.json"

    # -- helpers ---------------------------------------------------------
    def publish_summary(self, content=None, meeting_id=None, **options):
        lease = self.acquire()
        options.setdefault("workspace_root", self.root)
        options.setdefault("lease_id", lease)
        options.setdefault("trusted_workspace_lock_root", self.lock_root)
        return self.writer.publish_registered_summary(
            self.NEW_SUMMARY if content is None else content,
            meeting_id=meeting_id or self.meeting_id, **options)

    def sha(self, path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def artifact_state(self):
        """Bytes + hashes of the three artifacts plus register/ownership bytes."""
        entry = register_document(self.root)["meetings"][0]
        state = {key: (self.root / entry[key]).read_bytes()
                 for key in ("summary_path", "transcript_path", "transcript_raw_path")}
        state["register"] = self.register_path.read_bytes()
        state["ownership"] = self.ownership_path.read_bytes()
        return state

    def ownership_by_path(self):
        return {item["path"]: item
                for item in read_json(self.ownership_path)["files"]}

    def assert_untouched(self, before):
        """A rejection or no-op leaves every byte and the journal dir alone."""
        self.assertEqual(self.artifact_state(), before)
        self.assertFalse(self.journal_dir().exists()
                         and any(self.journal_dir().iterdir()))

    def mutate_register(self, mutate):
        document = register_document(self.root)
        mutate(document)
        self.write_register(document)

    # -- success and byte identity ----------------------------------------
    def test_replace_summary_keeps_transcript_raw_and_their_ownership(self):
        before = self.artifact_state()
        owners_before = self.ownership_by_path()
        entry_before = self.entry
        summary_rel = entry_before["summary_path"]
        transcript_rel = entry_before["transcript_path"]
        raw_rel = entry_before["transcript_raw_path"]
        envelope = self.publish_summary()
        self.assert_success(envelope, "Applied")
        self.assertTrue(envelope["data"]["changed"])
        self.assertEqual(envelope["data"]["paths"], {"summary_path": summary_rel})

        after = self.artifact_state()
        self.assertEqual((self.root / summary_rel).read_bytes(),
                         self.NEW_SUMMARY.encode("utf-8"))
        self.assertNotEqual(after["summary_path"], before["summary_path"])
        # Transcript and raw: byte-identical, hash-identical.
        for key in ("transcript_path", "transcript_raw_path"):
            self.assertEqual(after[key], before[key])
            self.assertEqual(hashlib.sha256(after[key]).hexdigest(),
                             hashlib.sha256(before[key]).hexdigest())
        # Ownership: untouched artifacts keep the exact same records.
        owners_after = self.ownership_by_path()
        self.assertEqual(set(owners_after), set(owners_before))
        for relative in (transcript_rel, raw_rel):
            self.assertEqual(owners_after[relative], owners_before[relative])
        new_owner = owners_after[summary_rel]
        self.assertEqual(new_owner["sha256"], self.sha(self.root / summary_rel))
        self.assertEqual(new_owner["bytes"], len(self.NEW_SUMMARY.encode("utf-8")))
        self.assertEqual(new_owner["kind"], "summary")
        self.assertEqual(new_owner["meeting_id"], self.meeting_id)
        self.assertEqual(new_owner["writer_version"], "meeting-desk/1")
        # Register: same paths/identity; only the review state moves.
        entry_after = register_document(self.root)["meetings"][0]
        stable = lambda item: {k: v for k, v in item.items() if k not in self.VOLATILE}
        self.assertEqual(stable(entry_after), stable(entry_before))
        self.assertEqual(entry_after["content_status"], "provisional")
        self.assertTrue(entry_after["review_recommended"])
        self.assertIn("content-changed", entry_after["review_reason"])
        self.assertFalse(self.journal_dir().exists() and any(self.journal_dir().iterdir()))

    def test_replacement_is_utf8_exact_without_newline_translation(self):
        content = "---\ndocument_type: meeting-summary\n---\r\nzeile\r\nzwei\n"
        self.assert_success(self.publish_summary(content), "Applied")
        entry = register_document(self.root)["meetings"][0]
        self.assertEqual((self.root / entry["summary_path"]).read_bytes(),
                         content.encode("utf-8"))

    def test_accepted_summary_returns_to_provisional_review(self):
        self.mutate_register(lambda doc: doc["meetings"][0].update(
            content_status="accepted", review_recommended=False))
        self.assert_success(self.publish_summary(), "Applied")
        entry = register_document(self.root)["meetings"][0]
        self.assertEqual(entry["content_status"], "provisional")
        self.assertTrue(entry["review_recommended"])

    # -- idempotence and dry run -------------------------------------------
    def test_repeated_publication_is_unchanged_and_writes_nothing(self):
        self.assert_success(self.publish_summary(), "Applied")
        settled = self.artifact_state()
        again = self.publish_summary()
        self.assert_success(again, "Unchanged")
        self.assertFalse(again["data"]["changed"])
        self.assertEqual(self.artifact_state(), settled)
        self.assertFalse(self.journal_dir().exists() and any(self.journal_dir().iterdir()))

    def test_unchanged_content_on_fresh_workspace_is_a_no_op(self):
        current = (self.root / self.entry["summary_path"]).read_text(encoding="utf-8")
        before = self.artifact_state()
        envelope = self.publish_summary(current)
        self.assert_success(envelope, "Unchanged")
        self.assert_untouched(before)

    def test_dry_run_plans_the_summary_only_and_mutates_nothing(self):
        before = self.artifact_state()
        envelope = self.publish_summary(dry_run=True, lease_id=None)
        self.assert_success(envelope, "DryRun")
        plan = envelope["data"]["plan"]
        self.assertEqual(plan["action"], "publish_summary")
        self.assertEqual(plan["paths"], {"summary_path": self.entry["summary_path"]})
        self.assertEqual([item["path"] for item in plan["files"]],
                         [self.entry["summary_path"]])
        self.assertTrue(envelope["data"]["changed"])
        self.assertEqual(len(envelope["data"]["plan_sha256"]), 64)
        self.assert_untouched(before)

    # -- capability --------------------------------------------------------
    def test_capabilities_expose_the_new_action_and_keep_existing_names(self):
        actions = self.writer.capabilities()["actions"]
        self.assertIn("publish_registered_summary", actions)
        for name in ("ingest_record", "ingest_local", "publish_registered_transcript",
                     "inspect", "recover"):
            self.assertIn(name, actions)
        self.assertIn("publish_registered_summary", self.writer.__all__)
        code, envelope, stderr = run_cli(self.lock_root, ["--capabilities"])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(envelope["data"]["actions"], actions)

    # -- fail-closed ownership ----------------------------------------------
    def test_human_edited_summary_is_not_overwritten(self):
        target = self.root / self.entry["summary_path"]
        target.write_bytes(b"human edited summary\n")
        before = self.artifact_state()
        envelope = self.publish_summary()
        self.assert_rejected(envelope)
        self.assertIn("not provably writer-owned", envelope["message"])
        self.assert_untouched(before)

    def test_summary_without_ownership_record_is_not_overwritten(self):
        ownership = read_json(self.ownership_path)
        ownership["files"] = [item for item in ownership["files"]
                              if item["path"] != self.entry["summary_path"]]
        self.ownership_path.write_text(json.dumps(ownership), encoding="utf-8")
        before = self.artifact_state()
        self.assert_rejected(self.publish_summary())
        self.assert_untouched(before)

    def test_duplicate_or_foreign_kind_ownership_records_are_ambiguous(self):
        summary_rel = self.entry["summary_path"]
        self.addCleanup(self.ownership_path.write_bytes, self.ownership_path.read_bytes())
        ownership = read_json(self.ownership_path)
        record = next(item for item in ownership["files"] if item["path"] == summary_rel)
        expected = {"duplicate": "ownership record is ambiguous",
                    "kind": "kind other than summary",
                    "other meeting": "not bound to this meeting",
                    "no meeting_id": "not bound to this meeting",
                    "no kind": "kind other than summary",
                    "neither": "kind other than summary",
                    "alias-bound": "not bound to this meeting"}
        for label, files in (
                ("duplicate", ownership["files"] + [dict(record)]),
                ("kind", [dict(item, kind="transcript") if item["path"] == summary_rel
                          else item for item in ownership["files"]]),
                ("other meeting", [dict(item, meeting_id="ticnote-other-meeting")
                                   if item["path"] == summary_rel else item
                                   for item in ownership["files"]]),
                # Hash matches the file, but the binding fields are absent.
                ("no meeting_id", [{k: v for k, v in item.items() if k != "meeting_id"}
                                   if item["path"] == summary_rel else item
                                   for item in ownership["files"]]),
                ("no kind", [{k: v for k, v in item.items() if k != "kind"}
                             if item["path"] == summary_rel else item
                             for item in ownership["files"]]),
                ("neither", [{k: v for k, v in item.items()
                              if k not in ("kind", "meeting_id")}
                             if item["path"] == summary_rel else item
                             for item in ownership["files"]]),
                # A record bound to the alias instead of the stable id is foreign too.
                ("alias-bound", [dict(item, meeting_id="legacy-alias")
                                 if item["path"] == summary_rel else item
                                 for item in ownership["files"]])):
            with self.subTest(label):
                self.ownership_path.write_text(
                    json.dumps({**ownership, "files": files}), encoding="utf-8")
                before = self.artifact_state()
                envelope = self.publish_summary()
                self.assert_rejected(envelope)
                # The hash still matches the file: the ownership guard must be
                # what rejects, not the hash check.
                self.assertIn(expected[label], envelope["message"])
                self.assertNotIn("not provably writer-owned", envelope["message"])
                self.assert_untouched(before)

    def test_alias_match_binds_ownership_and_plan_to_the_stable_id(self):
        self.mutate_register(lambda doc: doc["meetings"][0].update(id="legacy-alias"))
        planned = self.publish_summary(meeting_id="legacy-alias", dry_run=True)
        self.assert_success(planned, "DryRun")
        self.assertEqual(planned["data"]["plan"]["meeting_id"], self.meeting_id)
        envelope = self.publish_summary(meeting_id="legacy-alias")
        self.assert_success(envelope, "Applied")
        self.assertEqual(envelope["data"]["meeting_id"], "legacy-alias")
        owner = self.ownership_by_path()[self.entry["summary_path"]]
        self.assertEqual(owner["meeting_id"], self.meeting_id)
        self.assert_success(self.publish_summary(meeting_id="legacy-alias"), "Unchanged")

    def test_entry_with_only_an_id_field_binds_to_that_id(self):
        def only_id(document):
            entry = document["meetings"][0]
            entry["id"] = entry.pop("meeting_id")
        self.mutate_register(only_id)
        self.assert_success(self.publish_summary(), "Applied")
        owner = self.ownership_by_path()[self.entry["summary_path"]]
        self.assertEqual(owner["meeting_id"], self.meeting_id)

    def test_multiple_register_matches_are_ambiguous(self):
        def duplicate(document):
            document["meetings"].append(copy.deepcopy(document["meetings"][0]))
        self.mutate_register(duplicate)
        before = self.artifact_state()
        envelope = self.publish_summary()
        self.assert_rejected(envelope)
        self.assertIn("exactly one registered meeting", envelope["message"])
        self.assert_untouched(before)

    def test_summary_path_shared_with_another_meeting_is_ambiguous(self):
        def share(document):
            other = copy.deepcopy(document["meetings"][0])
            other["meeting_id"] = "ticnote-other-meeting"
            other["source"] = {"system": "ticnote", "account_ref": None,
                               "external_id": "other-meeting"}
            document["meetings"].append(other)
        self.mutate_register(share)
        before = self.artifact_state()
        envelope = self.publish_summary()
        self.assert_rejected(envelope)
        self.assertIn("shared with another registered artifact", envelope["message"])
        self.assert_untouched(before)

    def test_summary_path_aliasing_transcript_or_raw_is_rejected(self):
        original = register_document(self.root)
        self.addCleanup(self.write_register, original)
        for alias in ("transcript_path", "transcript_raw_path"):
            with self.subTest(alias):
                document = copy.deepcopy(original)
                document["meetings"][0]["summary_path"] = document["meetings"][0][alias]
                self.write_register(document)
                before = self.artifact_state()
                self.assert_rejected(self.publish_summary())
                self.assertEqual(before["transcript_path"],
                                 (self.root / self.entry["transcript_path"]).read_bytes())
                self.assertEqual(before["transcript_raw_path"],
                                 (self.root / self.entry["transcript_raw_path"]).read_bytes())
                self.assertFalse(self.journal_dir().exists()
                                 and any(self.journal_dir().iterdir()))

    def test_summary_path_outside_evidence_zone_is_rejected(self):
        self.mutate_register(lambda doc: doc["meetings"][0].update(
            summary_path="data/escape.summary.md"))
        before = self.register_path.read_bytes()
        self.assert_rejected(self.publish_summary())
        self.assertFalse((self.root / "data/escape.summary.md").exists())
        self.assertEqual(self.register_path.read_bytes(), before)

    # -- no registered summary: defined, documented behavior -----------------
    def test_unknown_meeting_is_rejected_without_creating_anything(self):
        before = self.artifact_state()
        envelope = self.publish_summary(meeting_id="ticnote-does-not-exist")
        self.assert_rejected(envelope)
        self.assertIn("exactly one registered meeting", envelope["message"])
        self.assert_untouched(before)

    def test_meeting_without_registered_summary_is_rejected_not_created(self):
        self.mutate_register(lambda doc: doc["meetings"][0].update(summary_path=None))
        before = self.register_path.read_bytes()
        files_before = sorted(path.relative_to(self.root).as_posix()
                              for path in self.root.rglob("*") if path.is_file())
        envelope = self.publish_summary()
        self.assert_rejected(envelope)
        self.assertIn("no summary_path", envelope["message"])
        self.assertEqual(self.register_path.read_bytes(), before)
        self.assertEqual(sorted(path.relative_to(self.root).as_posix()
                                for path in self.root.rglob("*") if path.is_file()),
                         files_before)

    def test_registered_but_missing_summary_file_is_not_recreated(self):
        target = self.root / self.entry["summary_path"]
        target.unlink()
        register_before = self.register_path.read_bytes()
        ownership_before = self.ownership_path.read_bytes()
        envelope = self.publish_summary()
        self.assert_rejected(envelope)
        self.assertIn("only replaces an existing registered file", envelope["message"])
        self.assertFalse(target.exists())
        self.assertEqual(self.register_path.read_bytes(), register_before)
        self.assertEqual(self.ownership_path.read_bytes(), ownership_before)
        self.assertFalse(self.journal_dir().exists() and any(self.journal_dir().iterdir()))

    # -- input, lease and error classification -------------------------------
    def test_invalid_content_is_rejected_before_any_write(self):
        before = self.artifact_state()
        for label, content in (("empty", ""), ("blank", "  \n\t"), ("none", None),
                               ("bytes", b"x"), ("nul", "a\x00b"), ("control", "a\x07b"),
                               ("oversize", "x" * (4 * 1024 * 1024 + 1))):
            with self.subTest(label):
                lease = self.acquire()
                envelope = self.writer.publish_registered_summary(
                    content, workspace_root=self.root, lease_id=lease,
                    meeting_id=self.meeting_id,
                    trusted_workspace_lock_root=self.lock_root)
                self.assert_rejected(envelope)
        self.assert_untouched(before)

    def test_missing_meeting_id_is_rejected(self):
        before = self.artifact_state()
        for meeting_id in ("", None, 7):
            lease = self.acquire()
            envelope = self.writer.publish_registered_summary(
                self.NEW_SUMMARY, workspace_root=self.root, lease_id=lease,
                meeting_id=meeting_id, trusted_workspace_lock_root=self.lock_root)
            self.assert_rejected(envelope)
        self.assert_untouched(before)

    def test_foreign_lease_is_rejected_without_mutation(self):
        before = self.artifact_state()
        envelope = self.publish_summary(lease_id="not-the-owner")
        self.assert_rejected(envelope)
        self.assert_untouched(before)

    def test_open_journal_blocks_summary_publication(self):
        self.journal_dir().mkdir(parents=True, exist_ok=True)
        (self.journal_dir() / "human-note.txt").write_bytes(b"foreign\n")
        before = self.artifact_state()
        envelope = self.publish_summary()
        self.assert_envelope(envelope)
        self.assertFalse(envelope["success"])
        self.assertEqual(envelope["state"], "RecoveryRequired")
        self.assertEqual(self.artifact_state(), before)
        self.assertEqual((self.journal_dir() / "human-note.txt").read_bytes(), b"foreign\n")

    def test_interrupted_publication_requires_recovery_and_recovers_forward(self):
        real_publish = self.writer._publish_one
        calls = []

        def failing(root, step):
            calls.append(step["path"])
            if len(calls) == 2:  # summary written, ownership step interrupted
                raise OSError("simulated crash")
            return real_publish(root, step)

        transcript = (self.root / self.entry["transcript_path"]).read_bytes()
        raw = (self.root / self.entry["transcript_raw_path"]).read_bytes()
        with patch.object(self.writer, "_publish_one", failing):
            envelope = self.publish_summary()
        self.assert_envelope(envelope)
        self.assertFalse(envelope["success"])
        self.assertEqual(envelope["state"], "RecoveryRequired")
        # A retry is blocked by the open journal; nothing is retried silently.
        blocked = self.publish_summary()
        self.assertEqual(blocked["state"], "RecoveryRequired")
        inspected = self.writer.inspect_workspace(self.root)
        self.assertTrue(inspected["data"]["recovery_required"])
        recovered = self.writer.recover_workspace(
            self.root, lease_id=self.lease, trusted_workspace_lock_root=self.lock_root)
        self.assert_success(recovered, "Recovered")
        summary_rel = self.entry["summary_path"]
        self.assertEqual((self.root / summary_rel).read_bytes(),
                         self.NEW_SUMMARY.encode("utf-8"))
        self.assertEqual(self.ownership_by_path()[summary_rel]["sha256"],
                         self.sha(self.root / summary_rel))
        self.assertEqual((self.root / self.entry["transcript_path"]).read_bytes(), transcript)
        self.assertEqual((self.root / self.entry["transcript_raw_path"]).read_bytes(), raw)
        self.assert_success(self.publish_summary(), "Unchanged")

    def test_transcript_publication_still_uses_its_own_contract(self):
        """The shared plan path keeps the transcript seam unchanged."""
        content = "---\ndocument_type: meeting-transcript\n---\n\ncleaned\n"
        summary_before = (self.root / self.entry["summary_path"]).read_bytes()
        raw_before = (self.root / self.entry["transcript_raw_path"]).read_bytes()
        envelope = self.publish_transcript(content, self.meeting_id)
        self.assert_success(envelope, "Applied")
        self.assertEqual(envelope["data"]["paths"],
                         {"transcript_path": self.entry["transcript_path"]})
        self.assertEqual((self.root / self.entry["summary_path"]).read_bytes(), summary_before)
        self.assertEqual((self.root / self.entry["transcript_raw_path"]).read_bytes(),
                         raw_before)
        self.assert_success(self.publish_transcript(content, self.meeting_id), "Unchanged")


class RetitleRegisteredMeetingTests(WriterTestBase):
    """OI #52 P5: ``retitle_registered_meeting`` changes the title and nothing else."""

    NEW_TITLE = "Förderanträge – Abstimmung \"LLL\" 日本語"
    # Builder-style header: extra fields and a body that must survive verbatim.
    CLEAN_TRANSCRIPT = (
        "---\nmeeting_id: \"ticnote-tn-900007\"\ntitle: \"Aufnahme 27.10.\"\n"
        "date: null\ndateString: \"\"\nsource_raw: \"raw.md\"\ncorrections_count: 16\n"
        "unklar_count: 16\nprovenance: meeting-desk §2b (Zwei-Artefakt-Prinzip)\n---\n\n"
        "# Bereinigungsliste\n\n| # | Original |\n|---|---|\n| 1 | Robin |\n\n---\n\n"
        "title: kein Frontmatter, nur Body\r\nRobin Example: Text.\n")

    def setUp(self):
        super().setUp()
        self.write_catalogs(topics=[{"id": "catalogue-topic", "title": "Synthetic"}])
        self.record = valid_record(6)
        self.assert_success(self.ingest(self.record, topic_slug="catalogue-topic"), "Applied")
        self.entry = register_document(self.root)["meetings"][0]
        self.meeting_id = self.entry["meeting_id"]
        self.ownership_path = self.root / "data/meeting-desk/ownership.json"
        self.summary_rel = self.entry["summary_path"]
        self.transcript_rel = self.entry["transcript_path"]
        self.raw_rel = self.entry["transcript_raw_path"]

    # -- helpers ---------------------------------------------------------
    def retitle(self, title=None, meeting_id=None, **options):
        lease = self.acquire()
        options.setdefault("workspace_root", self.root)
        options.setdefault("lease_id", lease)
        options.setdefault("trusted_workspace_lock_root", self.lock_root)
        return self.writer.retitle_registered_meeting(
            self.NEW_TITLE if title is None else title,
            meeting_id=meeting_id or self.meeting_id, **options)

    def use_clean_transcript(self):
        """Publish a builder-style transcript through the sanctioned seam."""
        self.assert_success(self.publish_transcript(self.CLEAN_TRANSCRIPT, self.meeting_id),
                            "Applied")

    def state(self):
        entry = register_document(self.root)["meetings"][0]
        state = {key: (self.root / entry[key]).read_bytes()
                 for key in ("summary_path", "transcript_path", "transcript_raw_path")}
        state["register"] = self.register_path.read_bytes()
        state["ownership"] = self.ownership_path.read_bytes()
        return state

    def tree(self):
        return {path.relative_to(self.root).as_posix(): path.read_bytes()
                for path in sorted(self.root.rglob("*")) if path.is_file()}

    def owners(self):
        return {item["path"]: item for item in read_json(self.ownership_path)["files"]}

    def sha(self, relative):
        return hashlib.sha256((self.root / relative).read_bytes()).hexdigest()

    def mutate_register(self, mutate):
        document = register_document(self.root)
        mutate(document)
        self.write_register(document)

    def assert_untouched(self, before_tree):
        self.assertEqual(self.tree(), before_tree)
        self.assertFalse(self.journal_dir().exists() and any(self.journal_dir().iterdir()))

    @staticmethod
    def frontmatter_title_line(data: bytes) -> str:
        return next(line for line in data.decode("utf-8").splitlines()
                    if line.startswith("title:"))

    # -- success, consistency and byte identity ----------------------------
    def test_retitle_updates_register_summary_and_transcript_only(self):
        self.use_clean_transcript()
        before = self.state()
        tree_before = self.tree()
        owners_before = self.owners()
        entry_before = register_document(self.root)["meetings"][0]

        envelope = self.retitle()
        self.assert_success(envelope, "Applied")
        self.assertTrue(envelope["data"]["changed"])
        self.assertEqual(envelope["data"]["paths"],
                         {"summary_path": self.summary_rel,
                          "transcript_path": self.transcript_rel})

        after = self.state()
        encoded = json.dumps(self.NEW_TITLE, ensure_ascii=False)
        entry_after = register_document(self.root)["meetings"][0]
        self.assertEqual(entry_after["title"], self.NEW_TITLE)
        for key in ("summary_path", "transcript_path"):
            self.assertEqual(self.frontmatter_title_line(after[key]), f"title: {encoded}")
        # Paths and slugs are stable: no file appeared, vanished or moved.
        self.assertEqual(set(self.tree()) - {"data/meeting-desk/ownership.json"},
                         set(tree_before) - {"data/meeting-desk/ownership.json"})
        for key in ("summary_path", "transcript_path", "transcript_raw_path"):
            self.assertEqual(entry_after[key], entry_before[key])
        self.assertIn("synthetic-fractional-duration", self.summary_rel)
        self.assertNotIn("forderantrage", self.summary_rel)
        # Every other register field is unchanged (title is the only difference).
        self.assertEqual({k: v for k, v in entry_after.items() if k != "title"},
                         {k: v for k, v in entry_before.items() if k != "title"})
        # Raw is byte-identical and its ownership record is exactly the same.
        self.assertEqual(after["transcript_raw_path"], before["transcript_raw_path"])
        self.assertEqual(self.owners()[self.raw_rel], owners_before[self.raw_rel])

    def test_only_the_title_line_changes_in_the_artifacts(self):
        self.use_clean_transcript()
        before = self.state()
        self.assert_success(self.retitle(), "Applied")
        after = self.state()
        encoded = json.dumps(self.NEW_TITLE, ensure_ascii=False)
        for key in ("summary_path", "transcript_path"):
            old_lines = before[key].decode("utf-8").split("\n")
            new_lines = after[key].decode("utf-8").split("\n")
            self.assertEqual(len(old_lines), len(new_lines))
            changed = [(a, b) for a, b in zip(old_lines, new_lines) if a != b]
            self.assertEqual(len(changed), 1, key)
            self.assertTrue(changed[0][0].startswith("title: "))
            self.assertEqual(changed[0][1], f"title: {encoded}")

    def test_transcript_body_and_builder_frontmatter_fields_survive(self):
        self.use_clean_transcript()
        self.assert_success(self.retitle(), "Applied")
        text = (self.root / self.transcript_rel).read_bytes().decode("utf-8")
        expected = self.CLEAN_TRANSCRIPT.replace(
            'title: "Aufnahme 27.10."',
            "title: " + json.dumps(self.NEW_TITLE, ensure_ascii=False), 1)
        self.assertEqual(text, expected)
        for fragment in ("corrections_count: 16", "unklar_count: 16",
                         "provenance: meeting-desk §2b (Zwei-Artefakt-Prinzip)",
                         "| 1 | Robin |", "title: kein Frontmatter, nur Body\r\n"):
            self.assertIn(fragment, text)

    def test_writer_rendered_frontmatter_stays_otherwise_identical(self):
        before = (self.root / self.summary_rel).read_bytes().decode("utf-8")
        self.assert_success(self.retitle("Neuer Titel"), "Applied")
        after = (self.root / self.summary_rel).read_bytes().decode("utf-8")
        self.assertEqual(after, before.replace(
            'title: "Synthetic fractional duration"', 'title: "Neuer Titel"', 1))

    def test_crlf_frontmatter_keeps_its_line_endings(self):
        content = "---\r\ndocument_type: meeting-summary\r\ntitle: \"Alt\"\r\n---\r\n\r\nBody\r\n"
        self.assert_success(self.publish_summary(content), "Applied")
        self.assert_success(self.retitle("Neu"), "Applied")
        self.assertEqual((self.root / self.summary_rel).read_bytes(),
                         content.replace('"Alt"', '"Neu"').encode("utf-8"))

    def publish_summary(self, content):
        lease = self.acquire()
        return self.writer.publish_registered_summary(
            content, workspace_root=self.root, lease_id=lease, meeting_id=self.meeting_id,
            trusted_workspace_lock_root=self.lock_root)

    def test_ownership_hashes_match_and_untouched_records_are_identical(self):
        self.use_clean_transcript()
        owners_before = self.owners()
        self.assert_success(self.retitle(), "Applied")
        owners = self.owners()
        self.assertEqual(set(owners), set(owners_before))
        for relative, kind in ((self.summary_rel, "summary"),
                               (self.transcript_rel, "transcript")):
            data = (self.root / relative).read_bytes()
            self.assertEqual(owners[relative]["sha256"], hashlib.sha256(data).hexdigest())
            self.assertEqual(owners[relative]["bytes"], len(data))
            self.assertEqual(owners[relative]["kind"], kind)
            self.assertEqual(owners[relative]["meeting_id"], self.meeting_id)
            self.assertEqual(owners[relative]["writer_version"], "meeting-desk/1")
            self.assertNotEqual(owners[relative]["sha256"], owners_before[relative]["sha256"])
        self.assertEqual(owners[self.raw_rel], owners_before[self.raw_rel])
        # Other writer operations stay possible without a human edit in between.
        self.assert_success(self.publish_transcript(
            self.CLEAN_TRANSCRIPT.replace("16", "17"), self.meeting_id), "Applied")
        self.assert_success(self.publish_summary(
            "---\ntitle: \"X\"\n---\n\nBody\n"), "Applied")

    def test_reingest_after_retitle_is_still_allowed(self):
        self.assert_success(self.retitle(), "Applied")
        record = copy.deepcopy(self.record)
        record["title"] = self.NEW_TITLE
        record["artifacts"]["summary_md"] = "Reviewed summary.\n"
        envelope = self.ingest(record, topic_slug="catalogue-topic")
        self.assert_success(envelope, "Applied")
        self.assertEqual(register_document(self.root)["meetings"][0]["summary_path"],
                         self.summary_rel)

    def test_unrelated_entries_and_root_extras_are_preserved(self):
        def extend(document):
            document["note"] = {"keep": True}
            other = copy.deepcopy(document["meetings"][0])
            other["meeting_id"] = "ticnote-other"
            other["source"] = {"system": "ticnote", "account_ref": None,
                               "external_id": "other"}
            other["summary_path"] = other["transcript_path"] = other["transcript_raw_path"] = None
            document["meetings"].append(other)
        self.mutate_register(extend)
        before = register_document(self.root)
        self.assert_success(self.retitle(), "Applied")
        after = register_document(self.root)
        self.assertEqual(after["note"], {"keep": True})
        self.assertEqual(after["meetings"][1], before["meetings"][1])

    # -- idempotence and dry run -------------------------------------------
    def test_repeat_is_unchanged_and_writes_nothing(self):
        self.use_clean_transcript()
        self.assert_success(self.retitle(), "Applied")
        settled = self.tree()
        again = self.retitle()
        self.assert_success(again, "Unchanged")
        self.assertFalse(again["data"]["changed"])
        self.assertEqual(self.tree(), settled)

    def test_current_title_on_fresh_workspace_is_a_no_op(self):
        before = self.tree()
        envelope = self.retitle(self.record["title"])
        self.assert_success(envelope, "Unchanged")
        self.assert_untouched(before)

    def test_partial_inconsistency_is_repaired_without_touching_consistent_files(self):
        self.assert_success(self.retitle(), "Applied")
        self.mutate_register(lambda doc: doc["meetings"][0].update(title="stale"))
        summary_before = (self.root / self.summary_rel).read_bytes()
        owners_before = self.owners()
        self.assert_success(self.retitle(), "Applied")
        self.assertEqual(register_document(self.root)["meetings"][0]["title"], self.NEW_TITLE)
        self.assertEqual((self.root / self.summary_rel).read_bytes(), summary_before)
        self.assertEqual(self.owners(), owners_before)

    def test_dry_run_plans_the_change_and_mutates_nothing(self):
        before = self.tree()
        envelope = self.retitle(dry_run=True, lease_id=None)
        self.assert_success(envelope, "DryRun")
        plan = envelope["data"]["plan"]
        self.assertEqual(plan["action"], "retitle_meeting")
        self.assertEqual(plan["title"], {"from": self.record["title"], "to": self.NEW_TITLE})
        self.assertEqual(plan["paths"], {"summary_path": self.summary_rel,
                                         "transcript_path": self.transcript_rel})
        self.assertEqual([item["path"] for item in plan["files"]],
                         [self.summary_rel, self.transcript_rel])
        self.assertEqual([item["frontmatter_title"] for item in plan["files"]],
                         ["updated", "updated"])
        self.assertTrue(envelope["data"]["changed"])
        self.assertEqual(len(envelope["data"]["plan_sha256"]), 64)
        self.assert_untouched(before)
        current = self.retitle(self.record["title"], dry_run=True, lease_id=None)
        self.assertEqual([item["frontmatter_title"] for item in current["data"]["plan"]["files"]],
                         ["current", "current"])

    # -- capability ----------------------------------------------------------
    def test_capabilities_expose_the_action_and_keep_existing_names(self):
        actions = self.writer.capabilities()["actions"]
        self.assertIn("retitle_registered_meeting", actions)
        for name in ("ingest_record", "ingest_local", "publish_registered_transcript",
                     "publish_registered_summary", "inspect", "recover"):
            self.assertIn(name, actions)
        self.assertIn("retitle_registered_meeting", self.writer.__all__)
        code, envelope, stderr = run_cli(self.lock_root, ["--capabilities"])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(envelope["data"]["actions"], actions)

    # -- frontmatter without a (single) title line ---------------------------
    def test_artifact_without_a_title_line_is_left_alone(self):
        content = "---\ndocument_type: meeting-summary\n---\n\nBody\n"
        self.assert_success(self.publish_summary(content), "Applied")
        owners_before = self.owners()
        self.assert_success(self.retitle(), "Applied")
        self.assertEqual((self.root / self.summary_rel).read_bytes(), content.encode("utf-8"))
        self.assertEqual(self.owners()[self.summary_rel], owners_before[self.summary_rel])
        self.assertEqual(register_document(self.root)["meetings"][0]["title"], self.NEW_TITLE)
        self.assertEqual(
            self.frontmatter_title_line((self.root / self.transcript_rel).read_bytes()),
            "title: " + json.dumps(self.NEW_TITLE, ensure_ascii=False))

    def test_ambiguous_or_unparseable_frontmatter_title_is_rejected(self):
        cases = {
            "duplicate": ("---\ntitle: \"A\"\ntitle: \"B\"\n---\n\nBody\n", "several title lines"),
            "plain scalar": ("---\ntitle: Plain\n---\n\nBody\n", "single-line quoted string"),
            "json garbage": ("---\ntitle: \"a\" \"b\"\n---\n\nBody\n", "single-line quoted string"),
            "unterminated": ("---\ntitle: \"A\"\n\nBody without close\n", "unterminated"),
        }
        for label, (content, message) in cases.items():
            with self.subTest(label):
                self.assert_success(self.publish_summary(content), "Applied")
                before = self.tree()
                envelope = self.retitle()
                self.assert_rejected(envelope)
                self.assertIn(message, envelope["message"])
                self.assert_untouched(before)

    # -- fail-closed ownership ----------------------------------------------
    def test_human_edited_artifact_is_not_rewritten(self):
        for key in ("summary_path", "transcript_path"):
            with self.subTest(key):
                target = self.root / self.entry[key]
                original = target.read_bytes()
                target.write_bytes(b"---\ntitle: \"Hand\"\n---\n\nhuman edited\n")
                before = self.tree()
                envelope = self.retitle()
                self.assert_rejected(envelope)
                self.assertIn("not provably writer-owned", envelope["message"])
                self.assert_untouched(before)
                target.write_bytes(original)

    def test_artifact_without_ownership_record_is_rejected(self):
        ownership = read_json(self.ownership_path)
        ownership["files"] = [item for item in ownership["files"]
                              if item["path"] != self.transcript_rel]
        self.ownership_path.write_text(json.dumps(ownership), encoding="utf-8")
        before = self.tree()
        self.assert_rejected(self.retitle())
        self.assert_untouched(before)

    def test_ambiguous_or_foreign_ownership_records_are_rejected(self):
        ownership = read_json(self.ownership_path)
        record = next(item for item in ownership["files"] if item["path"] == self.summary_rel)
        cases = {
            "duplicate": ownership["files"] + [dict(record)],
            "other meeting": [dict(item, meeting_id="ticnote-other") if item is record
                              else item for item in ownership["files"]],
            "wrong kind": [dict(item, kind="transcript") if item is record else item
                           for item in ownership["files"]],
            "alias-bound": [dict(item, meeting_id="legacy-alias") if item is record
                            else item for item in ownership["files"]],
        }
        for label, files in cases.items():
            with self.subTest(label):
                self.ownership_path.write_text(
                    json.dumps({**ownership, "files": files}), encoding="utf-8")
                before = self.tree()
                self.assert_rejected(self.retitle())
                self.assert_untouched(before)

    def test_unknown_and_multiply_matching_meetings_are_rejected(self):
        before = self.tree()
        envelope = self.retitle(meeting_id="ticnote-does-not-exist")
        self.assert_rejected(envelope)
        self.assertIn("exactly one registered meeting", envelope["message"])
        self.assert_untouched(before)
        self.mutate_register(lambda doc: doc["meetings"].append(
            copy.deepcopy(doc["meetings"][0])))
        before = self.tree()
        envelope = self.retitle()
        self.assert_rejected(envelope)
        self.assertIn("exactly one registered meeting", envelope["message"])
        self.assert_untouched(before)

    def test_shared_or_aliased_paths_are_rejected(self):
        original = register_document(self.root)
        variants = {
            "summary as transcript": ("summary_path", "transcript_path"),
            "transcript as raw": ("transcript_path", "transcript_raw_path"),
            "summary as raw": ("summary_path", "transcript_raw_path"),
        }
        for label, (target, source) in variants.items():
            with self.subTest(label):
                document = copy.deepcopy(original)
                document["meetings"][0][target] = document["meetings"][0][source]
                self.write_register(document)
                before = self.tree()
                self.assert_rejected(self.retitle())
                self.assert_untouched(before)
        document = copy.deepcopy(original)
        other = copy.deepcopy(document["meetings"][0])
        other.update(meeting_id="ticnote-other",
                     source={"system": "ticnote", "account_ref": None, "external_id": "o"})
        document["meetings"].append(other)
        self.write_register(document)
        before = self.tree()
        self.assert_rejected(self.retitle())
        self.assert_untouched(before)

    def test_missing_registered_file_or_path_outside_evidence_is_rejected(self):
        (self.root / self.transcript_rel).unlink()
        before = self.tree()
        envelope = self.retitle()
        self.assert_rejected(envelope)
        self.assertIn("missing", envelope["message"])
        self.assert_untouched(before)
        self.mutate_register(lambda doc: doc["meetings"][0].update(
            summary_path="data/escape.summary.md"))
        before = self.tree()
        self.assert_rejected(self.retitle())
        self.assert_untouched(before)

    def test_meeting_without_summary_or_transcript_is_rejected(self):
        self.mutate_register(lambda doc: doc["meetings"][0].update(
            summary_path=None, transcript_path=None))
        before = self.tree()
        envelope = self.retitle()
        self.assert_rejected(envelope)
        self.assertIn("no summary or transcript", envelope["message"])
        self.assert_untouched(before)

    def test_rewrite_that_would_exceed_the_read_bound_is_rejected_before_staging(self):
        content = "---\ntitle: \"Alt\"\n---\n\n" + "x" * 6000 + "\n"
        self.assert_success(self.publish_summary(content), "Applied")
        before = self.tree()
        bound = len(content.encode("utf-8")) + 10
        with patch.object(self.writer, "MAX_REGISTER_BYTES", bound):
            envelope = self.retitle("T" * 100)
        self.assert_rejected(envelope)
        self.assertIn("would exceed the supported size", envelope["message"])
        self.assert_untouched(before)

    def test_meeting_with_only_a_summary_retitles_that_summary(self):
        self.mutate_register(lambda doc: doc["meetings"][0].update(transcript_path=None))
        transcript_before = (self.root / self.transcript_rel).read_bytes()
        envelope = self.retitle()
        self.assert_success(envelope, "Applied")
        self.assertEqual(envelope["data"]["paths"], {"summary_path": self.summary_rel})
        self.assertEqual((self.root / self.transcript_rel).read_bytes(), transcript_before)

    # -- input, lease and error classification -------------------------------
    def test_invalid_title_or_meeting_id_is_rejected_before_any_write(self):
        before = self.tree()
        for label, title in (("empty", ""), ("blank", "  "), ("none", None), ("bytes", b"x"),
                             ("nul", "a\x00b"), ("control", "a\x07b"),
                             ("surrogate", "a\ud800b"), ("long", "x" * 501)):
            with self.subTest(label):
                lease = self.acquire()
                envelope = self.writer.retitle_registered_meeting(
                    title, workspace_root=self.root, lease_id=lease,
                    meeting_id=self.meeting_id, trusted_workspace_lock_root=self.lock_root)
                self.assert_rejected(envelope)
        for meeting_id in ("", None, 7):
            with self.subTest(meeting_id=meeting_id):
                lease = self.acquire()
                envelope = self.writer.retitle_registered_meeting(
                    "Titel", workspace_root=self.root, lease_id=lease, meeting_id=meeting_id,
                    trusted_workspace_lock_root=self.lock_root)
                self.assert_rejected(envelope)
        self.assert_untouched(before)

    def test_foreign_lease_is_rejected_without_mutation(self):
        before = self.tree()
        envelope = self.retitle(lease_id="not-the-owner")
        self.assert_rejected(envelope)
        self.assert_untouched(before)

    def test_open_journal_blocks_retitle(self):
        self.journal_dir().mkdir(parents=True, exist_ok=True)
        (self.journal_dir() / "human-note.txt").write_bytes(b"foreign\n")
        before = self.tree()
        envelope = self.retitle()
        self.assert_envelope(envelope)
        self.assertFalse(envelope["success"])
        self.assertEqual(envelope["state"], "RecoveryRequired")
        self.assertEqual(self.tree(), before)

    def test_interrupted_retitle_requires_recovery_and_recovers_forward(self):
        real_publish = self.writer._publish_one
        calls = []

        def failing(root, step):
            calls.append(step["path"])
            if len(calls) == 2:  # summary written, transcript step interrupted
                raise OSError("simulated crash")
            return real_publish(root, step)

        raw = (self.root / self.raw_rel).read_bytes()
        with patch.object(self.writer, "_publish_one", failing):
            envelope = self.retitle()
        self.assert_envelope(envelope)
        self.assertEqual(envelope["state"], "RecoveryRequired")
        self.assertEqual(self.retitle()["state"], "RecoveryRequired")
        self.assertTrue(self.writer.inspect_workspace(self.root)["data"]["recovery_required"])
        recovered = self.writer.recover_workspace(
            self.root, lease_id=self.lease, trusted_workspace_lock_root=self.lock_root)
        self.assert_success(recovered, "Recovered")
        encoded = json.dumps(self.NEW_TITLE, ensure_ascii=False)
        for relative in (self.summary_rel, self.transcript_rel):
            self.assertEqual(self.frontmatter_title_line((self.root / relative).read_bytes()),
                             f"title: {encoded}")
            self.assertEqual(self.owners()[relative]["sha256"], self.sha(relative))
        self.assertEqual(register_document(self.root)["meetings"][0]["title"], self.NEW_TITLE)
        self.assertEqual((self.root / self.raw_rel).read_bytes(), raw)
        self.assert_success(self.retitle(), "Unchanged")

    # -- identity -------------------------------------------------------------
    def test_alias_match_binds_ownership_and_plan_to_the_stable_id(self):
        self.mutate_register(lambda doc: doc["meetings"][0].update(id="legacy-alias"))
        planned = self.retitle(meeting_id="legacy-alias", dry_run=True)
        self.assert_success(planned, "DryRun")
        self.assertEqual(planned["data"]["plan"]["meeting_id"], self.meeting_id)
        envelope = self.retitle(meeting_id="legacy-alias")
        self.assert_success(envelope, "Applied")
        self.assertEqual(envelope["data"]["meeting_id"], "legacy-alias")
        for relative in (self.summary_rel, self.transcript_rel):
            self.assertEqual(self.owners()[relative]["meeting_id"], self.meeting_id)
        self.assert_success(self.retitle(meeting_id="legacy-alias"), "Unchanged")
        self.assert_success(self.retitle(), "Unchanged")

    def test_entry_with_only_an_id_field_keeps_that_id(self):
        def only_id(document):
            entry = document["meetings"][0]
            entry["id"] = entry.pop("meeting_id")
        self.mutate_register(only_id)
        self.assert_success(self.retitle(), "Applied")
        entry = register_document(self.root)["meetings"][0]
        self.assertNotIn("meeting_id", entry)
        self.assertEqual(entry["id"], self.meeting_id)
        self.assertEqual(self.owners()[self.summary_rel]["meeting_id"], self.meeting_id)


class LocalEntryTests(WriterTestBase):
    def setUp(self):
        super().setUp()
        self.inbox = self.root / "data/inbox"
        self.inbox.mkdir(parents=True)
        self.summary = self.inbox / "local.summary.md"
        self.transcript = self.inbox / "local.transcript.md"
        self.raw = self.inbox / "local.transcript.raw.md"
        self.summary.write_bytes("Local summary \u4f1a\u8bae \U0001f680\r\nline two\r\n".encode("utf-8"))
        self.transcript.write_bytes("Speaker: local transcript\r\n".encode("utf-8"))
        self.raw.write_bytes("Speaker: local raw\nline two\n".encode("utf-8"))

    def local_args(self, *extra):
        return ["--local", "--summary", str(self.summary),
                "--transcript", str(self.transcript), "--raw", str(self.raw),
                "--date", "2026-10-21", "--title", "Local \u4f1a\u8bae \U0001f680",
                "--participants", "alex@example.invalid,sam@example.invalid", *extra]

    def plan(self, *extra):
        code, envelope, stderr = run_cli(
            self.lock_root, [*self.local_args(*extra), "--workspace", str(self.root)],
            env={"PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"})
        self.assertEqual(code, 0, stderr)
        return envelope

    def test_local_default_plan_then_hash_confirmed_apply_moves_sources(self):
        summary_before = self.summary.read_bytes()
        transcript_before = self.transcript.read_bytes()
        raw_before = self.raw.read_bytes()
        plan = self.plan()
        self.assertEqual(plan["state"], "Plan")
        self.assertTrue(plan["data"]["apply_required"])
        self.assertFalse(self.register_path.exists())
        plan_sha = plan["data"]["plan_sha256"]
        lease = self.acquire()
        code, applied, stderr = run_cli(
            self.lock_root,
            [*self.local_args("--workspace", str(self.root), "--lease-id", lease,
                              "--apply-plan", plan_sha)],
            env={"PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"})
        self.assertEqual(code, 0, stderr)
        self.assert_success(applied, "Applied")
        entry = register_document(self.root)["meetings"][0]
        self.assertEqual(entry["source"]["system"], "manual")
        self.assertEqual(entry["meeting_date"], "2026-10-21")
        summary_target = self.root / entry["summary_path"]
        transcript_target = self.root / entry["transcript_path"]
        raw_target = self.root / entry["transcript_raw_path"]
        self.assertTrue(summary_target.read_bytes().endswith(summary_before))
        self.assertTrue(transcript_target.read_bytes().endswith(transcript_before))
        self.assertEqual(raw_target.read_bytes(), raw_before)
        self.assertEqual(entry["participants"],
                         ["alex@example.invalid", "sam@example.invalid"])
        self.assertFalse(self.summary.exists())
        self.assertFalse(self.transcript.exists())
        self.assertFalse(self.raw.exists())

    def test_local_frontmatter_is_rewritten_only_on_explicit_flag(self):
        source = self.inbox / "front.summary.md"
        source.write_text("---\nlegacy_key: keepme\n---\nbody stays\n", encoding="utf-8")
        lease = self.acquire()

        def plan_for(path, title, rewrite=False):
            args = ["--local", "--summary", str(path), "--date", "2026-10-21",
                    "--title", title, "--workspace", str(self.root), "--json"]
            if rewrite:
                args.append("--rewrite-frontmatter")
            code, envelope, stderr = run_cli(self.lock_root, args)
            self.assertEqual(code, 0, stderr)
            return envelope

        plan = plan_for(source, "Frontmatter keep")
        code, applied, _ = run_cli(
            self.lock_root,
            ["--local", "--summary", str(source), "--date", "2026-10-21",
             "--title", "Frontmatter keep", "--workspace", str(self.root),
             "--lease-id", lease, "--apply-plan", plan["data"]["plan_sha256"]])
        self.assertEqual(code, 0)
        entry = register_document(self.root)["meetings"][0]
        target = (self.root / entry["summary_path"]).read_text(encoding="utf-8")
        self.assertIn("legacy_key: keepme", target)

        other = self.inbox / "front2.summary.md"
        other.write_text("---\nlegacy_key: dropme\n---\nrewritten body\n", encoding="utf-8")
        plan2 = plan_for(other, "Frontmatter rewrite", rewrite=True)
        code, applied, _ = run_cli(
            self.lock_root,
            ["--local", "--summary", str(other), "--date", "2026-10-21",
             "--title", "Frontmatter rewrite", "--workspace", str(self.root),
             "--lease-id", lease, "--apply-plan", plan2["data"]["plan_sha256"],
             "--rewrite-frontmatter"])
        self.assertEqual(code, 0)
        entries = [item for item in register_document(self.root)["meetings"]
                   if item["title"] == "Frontmatter rewrite"]
        self.assertEqual(len(entries), 1)
        target2 = (self.root / entries[0]["summary_path"]).read_text(encoding="utf-8")
        self.assertNotIn("legacy_key: dropme", target2)
        self.assertTrue(target2.endswith("rewritten body\n"))

    def test_local_transcript_without_raw_uses_transcript_bytes_as_raw(self):
        plan = self.plan("--raw", "")
        # Passing an empty path is a CLI value error; use the API without --raw.
        self.assertEqual(plan["state"], "Plan")
        lease = self.acquire()
        transcript_before = self.transcript.read_bytes()
        result = self.writer.ingest_local(
            workspace_root=self.root, lease_id=lease, summary_path=None,
            transcript_path=str(self.transcript), calendar_date="2026-10-21",
            title="Transcript only", trusted_workspace_lock_root=self.lock_root,
            dry_run=True)
        self.assertTrue(result["success"])
        applied = self.writer.ingest_local(
            workspace_root=self.root, lease_id=lease, summary_path=None,
            transcript_path=str(self.transcript), calendar_date="2026-10-21",
            title="Transcript only", trusted_workspace_lock_root=self.lock_root,
            apply_plan_sha256=result["data"]["plan_sha256"])
        self.assertTrue(applied["success"], applied)
        entry = [item for item in register_document(self.root)["meetings"]
                 if item["title"] == "Transcript only"][0]
        transcript_bytes = (self.root / entry["transcript_path"]).read_bytes()
        raw_bytes = (self.root / entry["transcript_raw_path"]).read_bytes()
        self.assertTrue(transcript_bytes.endswith(transcript_before))
        self.assertEqual(raw_bytes, transcript_before)

    def test_local_source_outside_zone_and_unknown_upgrade_are_rejected(self):
        outside = tempfile.TemporaryDirectory(prefix="meeting-local-outside-")
        self.addCleanup(outside.cleanup)
        external = Path(outside.name) / "external.md"
        external.write_text("outside\n", encoding="utf-8")
        result = self.writer.ingest_local(
            workspace_root=self.root, lease_id=None, summary_path=str(external),
            calendar_date="2026-10-21", dry_run=True)
        self.assertFalse(result["success"])
        self.assertEqual(result["state"], "Rejected")
        code, envelope, _ = run_cli(
            self.lock_root, [*self.local_args(), "--workspace", str(self.root),
                             "--upgrade"])
        self.assertEqual(code, 2)
        self.assertFalse(envelope["success"])

    def test_local_human_target_is_not_clobbered(self):
        plan = self.plan()
        target = self.root / plan["data"]["plan"]["paths"]["summary_path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"human local target\n")
        code, blocked, _ = run_cli(
            self.lock_root, [*self.local_args(), "--workspace", str(self.root)])
        self.assertEqual(code, 2)
        self.assertFalse(blocked["success"])
        self.assertEqual(blocked["state"], "Rejected")
        self.assertEqual(target.read_bytes(), b"human local target\n")
        self.assertTrue(self.summary.exists())

    def test_local_registered_foreign_artifact_is_not_moved(self):
        lease = self.acquire()
        plan = self.writer.ingest_local(
            workspace_root=self.root, lease_id=lease, summary_path=str(self.summary),
            calendar_date="2026-10-21", title="First local",
            trusted_workspace_lock_root=self.lock_root, dry_run=True)
        applied = self.writer.ingest_local(
            workspace_root=self.root, lease_id=lease, summary_path=str(self.summary),
            calendar_date="2026-10-21", title="First local",
            trusted_workspace_lock_root=self.lock_root,
            apply_plan_sha256=plan["data"]["plan_sha256"])
        self.assertTrue(applied["success"], applied)
        entry = register_document(self.root)["meetings"][0]
        registered = self.root / entry["summary_path"]
        before = registered.read_bytes()
        other = self.writer.ingest_local(
            workspace_root=self.root, lease_id=lease, summary_path=str(registered),
            calendar_date="2026-10-22", title="Second local",
            trusted_workspace_lock_root=self.lock_root, dry_run=True)
        self.assertFalse(other["success"])
        self.assertEqual(other["state"], "Rejected")
        self.assertEqual(registered.read_bytes(), before)

    def test_local_derived_id_collision_stops_without_suffix(self):
        lease = self.acquire()
        plan = self.writer.ingest_local(
            workspace_root=self.root, lease_id=lease, summary_path=str(self.summary),
            calendar_date="2026-10-21", title="Collision meeting",
            trusted_workspace_lock_root=self.lock_root, dry_run=True)
        self.assertTrue(plan["success"])
        applied = self.writer.ingest_local(
            workspace_root=self.root, lease_id=lease, summary_path=str(self.summary),
            calendar_date="2026-10-21", title="Collision meeting",
            trusted_workspace_lock_root=self.lock_root,
            apply_plan_sha256=plan["data"]["plan_sha256"])
        self.assertTrue(applied["success"], applied)
        self.assertFalse(self.summary.exists())
        self.summary.write_bytes(b"different local bytes\n")
        collision = self.writer.ingest_local(
            workspace_root=self.root, lease_id=lease, summary_path=str(self.summary),
            calendar_date="2026-10-21", title="Collision meeting",
            trusted_workspace_lock_root=self.lock_root, dry_run=True)
        self.assertFalse(collision["success"])
        self.assertEqual(collision["state"], "Rejected")
        self.assertTrue(self.summary.exists())
        entries = register_document(self.root)["meetings"]
        self.assertEqual(len(entries), 1)

    def test_local_summary_plus_raw_without_clean_transcript_moves_raw(self):
        lease = self.acquire()
        plan = self.writer.ingest_local(
            workspace_root=self.root, lease_id=lease, summary_path=str(self.summary),
            raw_path=str(self.raw), calendar_date="2026-10-21",
            title="Raw only local", trusted_workspace_lock_root=self.lock_root,
            dry_run=True)
        self.assertTrue(plan["success"], plan)
        applied = self.writer.ingest_local(
            workspace_root=self.root, lease_id=lease, summary_path=str(self.summary),
            raw_path=str(self.raw), calendar_date="2026-10-21",
            title="Raw only local", trusted_workspace_lock_root=self.lock_root,
            apply_plan_sha256=plan["data"]["plan_sha256"])
        self.assertTrue(applied["success"], applied)
        entry = register_document(self.root)["meetings"][0]
        self.assertIsNone(entry["transcript_path"])
        self.assertTrue(entry["transcript_raw_path"])
        self.assertEqual((self.root / entry["transcript_raw_path"]).read_bytes(),
                         b"Speaker: local raw\nline two\n")
        self.assertFalse(self.summary.exists())
        self.assertFalse(self.raw.exists())

    def test_local_source_symlink_or_junction_is_rejected_without_deletion(self):
        real = self.inbox / "real-source.md"
        real.write_bytes(b"real local bytes\n")
        alias = self.inbox / "alias-source.md"
        source = alias
        try:
            alias.symlink_to(real)
            self.addCleanup(lambda: alias.unlink(missing_ok=True))
        except OSError:
            if os.name != "nt":
                self.skipTest("symlinks/junctions unavailable")
            real.unlink()
            real.mkdir()
            (real / "source.md").write_bytes(b"real local bytes\n")
            completed = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(alias), str(real)],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", check=False)
            if completed.returncode != 0:
                self.skipTest("symlinks/junctions unavailable")
            self.addCleanup(lambda: os.rmdir(alias))
            source = alias / "source.md"
        result = self.writer.ingest_local(
            workspace_root=self.root, lease_id=None, summary_path=str(source),
            calendar_date="2026-10-21", title="Alias source", dry_run=True)
        self.assertFalse(result["success"])
        self.assertEqual(result["state"], "Rejected")
        self.assertTrue(real.exists())
        self.assertTrue(alias.exists())
        if source != alias:
            self.assertTrue(source.exists())
        self.assertFalse(self.register_path.exists())
        self.assertFalse(self.journal_dir().exists())

    def test_local_reserved_writer_state_sources_are_rejected_before_mutation(self):
        self.register_path.parent.mkdir(parents=True, exist_ok=True)
        self.register_path.write_text(json.dumps({"meetings": []}), encoding="utf-8")
        ownership = self.root / "data/meeting-desk/ownership.json"
        ownership.parent.mkdir(parents=True, exist_ok=True)
        ownership.write_text(json.dumps({"schema": "meeting-desk-ownership/1",
                                         "files": []}), encoding="utf-8")
        register_before = self.register_path.read_bytes()
        ownership_before = ownership.read_bytes()
        for source in (self.register_path, ownership):
            result = self.writer.ingest_local(
                workspace_root=self.root, lease_id=None, summary_path=str(source),
                calendar_date="2026-10-21", title="Reserved source", dry_run=True)
            self.assertFalse(result["success"])
            self.assertEqual(result["state"], "Rejected")
        raw_result = self.writer.ingest_local(
            workspace_root=self.root, lease_id=None, summary_path=str(self.summary),
            raw_path=str(ownership), calendar_date="2026-10-21",
            title="Reserved raw", dry_run=True)
        self.assertFalse(raw_result["success"])
        self.assertEqual(raw_result["state"], "Rejected")
        self.assertEqual(self.register_path.read_bytes(), register_before)
        self.assertEqual(ownership.read_bytes(), ownership_before)
        self.assertFalse(self.journal_dir().exists())

    def test_local_crash_before_register_then_recovery_deletes_sources(self):
        lease = self.acquire()
        driver = Path(__file__).resolve().parent / "_crash_local.py"
        completed = subprocess.run(
            [sys.executable, str(driver), str(WRITER_SCRIPT), str(self.root), lease,
             str(self.lock_root), "5", str(self.summary), str(self.transcript),
             str(self.raw), "2026-10-21"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
        self.assertEqual(completed.returncode, 9, completed.stderr)
        self.assertTrue(self.summary.exists())
        self.assertTrue(self.transcript.exists())
        self.assertTrue(self.raw.exists())
        self.assertTrue(list(self.journal_dir().glob("*.journal.json")))
        recovered = self.writer.recover_workspace(
            self.root, lease_id=lease, trusted_workspace_lock_root=self.lock_root)
        self.assertTrue(recovered["success"], recovered)
        self.assertFalse(self.summary.exists())
        self.assertFalse(self.transcript.exists())
        self.assertFalse(self.raw.exists())
        entry = register_document(self.root)["meetings"][0]
        self.assertTrue((self.root / entry["summary_path"]).is_file())



class LocalArtifactLimitTests(WriterTestBase):
    """OI #49: ``--local`` artifacts are bounded in bytes (4 MiB), records are not."""

    LOCAL_LIMIT = 4 * 1024 * 1024
    ASR_LINE = "Sprecher 1: Grüße aus Wien — wir starten den Tag.\r\n"

    def setUp(self):
        super().setUp()
        self.inbox = self.root / "data/inbox"
        self.inbox.mkdir(parents=True)

    def stage(self, name, data):
        path = self.inbox / name
        path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
        return path

    def asr_text(self, characters):
        return (self.ASR_LINE * (characters // len(self.ASR_LINE) + 1))[:characters]

    def ascii_bytes(self, size):
        unit = b"0123456789abcdef\n"
        return (unit * (size // len(unit) + 1))[:size]

    def snapshot(self):
        return {path.relative_to(self.root).as_posix(): path.read_bytes()
                for path in sorted(self.root.rglob("*")) if path.is_file()}

    def local_args(self, **sources):
        args = ["--local"]
        for key in ("summary", "transcript", "raw"):
            if key in sources:
                args += [f"--{key}", sources[key]]
        return [*args, "--date", "2026-10-07", "--title", "Limit meeting",
                "--workspace", str(self.root)]

    def plan_and_apply(self, args):
        code, plan, stderr = run_cli(self.lock_root, args)
        self.assertEqual(code, 0, plan)
        self.assert_success(plan, "Plan")
        lease = self.acquire()
        code, applied, stderr = run_cli(
            self.lock_root, [*args, "--lease-id", lease,
                             "--apply-plan", plan["data"]["plan_sha256"]])
        self.assertEqual(code, 0, applied)
        self.assert_success(applied, "Applied")
        return plan, applied, register_document(self.root)["meetings"][0]

    def assert_owned(self, relative, data):
        ownership = read_json(self.root / "data/meeting-desk/ownership.json")
        item = [item for item in ownership["files"] if item["path"] == relative]
        self.assertEqual(len(item), 1, relative)
        written = (self.root / relative).read_bytes()
        self.assertEqual(item[0]["sha256"], hashlib.sha256(written).hexdigest())
        self.assertEqual(item[0]["bytes"], len(written))
        self.assertTrue(written.endswith(data))

    def assert_inspect_clean(self, entries=1):
        code, inspected, stderr = run_cli(
            self.lock_root, ["--inspect", "--workspace", str(self.root)])
        self.assertEqual(code, 0, stderr)
        self.assertFalse(inspected["data"]["recovery_required"], inspected)
        self.assertEqual(inspected["data"]["journals"], [])
        self.assertEqual(inspected["data"]["register"]["entries"], entries)
        self.assertEqual(inspected["data"]["ownership"]["state"], "valid")

    def assert_local_rejection(self, args, *fragments):
        before = self.snapshot()
        code, envelope, _ = run_cli(self.lock_root, args)
        self.assertEqual(code, 2, envelope)
        self.assert_rejected(envelope)
        message = envelope["error"]["message"]
        for fragment in fragments:
            self.assertIn(fragment, message)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.register_path.exists())
        self.assertFalse(self.journal_dir().exists())
        return message

    def test_issue_49_realcase_583368_characters_publish_byte_exact(self):
        text = self.asr_text(583_368)
        data = text.encode("utf-8")
        self.assertEqual(len(text), 583_368)
        self.assertGreater(len(data), 583_368)
        summary = "Summary of day one 会议\n".encode("utf-8")
        self.stage("day1.summary.md", summary)
        self.stage("day1.transcript.txt", data)
        args = ["--local", "--summary", "data/inbox/day1.summary.md",
                "--transcript", "data/inbox/day1.transcript.txt",
                "--date", "2026-10-07", "--start-utc", "2026-10-07T07:00:00Z",
                "--title", "Day one kick-off", "--workspace", str(self.root)]
        plan, applied, entry = self.plan_and_apply(args)
        self.assertEqual(plan["data"]["plan"]["local_sources"],
                         [{"path": "data/inbox/day1.summary.md", "kind": "summary"},
                          {"path": "data/inbox/day1.transcript.txt", "kind": "transcript"}])
        transcript = (self.root / entry["transcript_path"]).read_bytes()
        raw = (self.root / entry["transcript_raw_path"]).read_bytes()
        self.assertEqual(raw, data)
        self.assertTrue(transcript.endswith(b"---\n\n" + data))
        self.assertEqual(transcript.count(data), 1)
        self.assertTrue((self.root / entry["summary_path"]).read_bytes().endswith(summary))
        self.assert_owned(entry["transcript_raw_path"], data)
        self.assert_owned(entry["transcript_path"], data)
        self.assertFalse((self.inbox / "day1.summary.md").exists())
        self.assertFalse((self.inbox / "day1.transcript.txt").exists())
        self.assert_inspect_clean()

    def test_cjk_bytes_over_one_mib_but_under_four_mib_publish_byte_exact(self):
        text = "会议" * 240_000
        data = text.encode("utf-8")
        self.assertEqual(len(text), 480_000)
        self.assertGreater(len(data), 1024 * 1024)
        self.assertLess(len(data), self.LOCAL_LIMIT)
        self.stage("cjk.summary.md", "cjk summary\n")
        self.stage("cjk.transcript.raw.md", data)
        args = self.local_args(summary=str(self.inbox / "cjk.summary.md"),
                               raw=str(self.inbox / "cjk.transcript.raw.md"))
        _plan, _applied, entry = self.plan_and_apply(args)
        self.assertIsNone(entry["transcript_path"])
        self.assertEqual((self.root / entry["transcript_raw_path"]).read_bytes(), data)
        self.assert_owned(entry["transcript_raw_path"], data)
        self.assertFalse((self.inbox / "cjk.transcript.raw.md").exists())
        self.assert_inspect_clean()

    def test_exactly_four_mib_is_accepted_and_one_more_byte_is_rejected(self):
        limit = self.LOCAL_LIMIT
        self.assertEqual(self.writer.MAX_LOCAL_ARTIFACT_BYTES, limit)
        data = self.ascii_bytes(limit)
        self.stage("edge.summary.md", "edge summary\n")
        self.stage("edge.transcript.md", data)
        args = self.local_args(summary=str(self.inbox / "edge.summary.md"),
                               transcript=str(self.inbox / "edge.transcript.md"))
        _plan, _applied, entry = self.plan_and_apply(args)
        self.assertEqual((self.root / entry["transcript_raw_path"]).read_bytes(), data)
        self.assert_owned(entry["transcript_path"], data)
        self.assert_inspect_clean()

    def test_over_four_mib_is_rejected_with_limit_and_path_without_mutation(self):
        limit = self.LOCAL_LIMIT
        over = self.ascii_bytes(limit + 1)
        small = "small summary\n"
        for kind in ("summary", "transcript", "raw"):
            with self.subTest(kind=kind):
                name = f"over-{kind}.md"
                self.stage(name, over)
                sources = {"summary": str(self.stage("ok.summary.md", small))}
                sources[kind] = str(self.inbox / name)
                if kind != "raw":
                    sources.setdefault("raw", str(self.stage("ok.raw.md", small)))
                message = self.assert_local_rejection(
                    self.local_args(**sources),
                    f"local artifact '{kind}'", f"data/inbox/{name}",
                    f"exceeds {limit} bytes (4 MiB)")
                self.assertNotIn(str(self.root), message)
                (self.inbox / name).unlink()

    def test_control_characters_are_named_and_never_normalized(self):
        for kind, flag in (("raw", "raw"), ("transcript", "transcript")):
            for char in ("\x00", "\x0b", "\x0c", "\x1b", "\x7f"):
                with self.subTest(kind=kind, codepoint=f"U+{ord(char):04X}"):
                    name = f"ctl.{kind}.md"
                    self.stage(name, f"before{char}after\n".encode("utf-8"))
                    sources = {flag: str(self.inbox / name)}
                    if kind == "raw":
                        sources["summary"] = str(self.stage("ctl.summary.md", "ok\n"))
                    self.assert_local_rejection(
                        self.local_args(**sources),
                        f"local artifact '{kind}'", f"data/inbox/{name}",
                        f"U+{ord(char):04X}")
                    (self.inbox / name).unlink()
        self.stage("ctl.summary.md", "sum\x0cmary\n")
        self.assert_local_rejection(
            self.local_args(summary=str(self.inbox / "ctl.summary.md")),
            "local artifact 'summary'", "data/inbox/ctl.summary.md", "U+000C")

    def test_tab_newline_carriage_return_bom_and_crlf_stay_byte_exact(self):
        raw = b"\xef\xbb\xbfSpeaker:\ttab\r\nline\rtwo\nlast\r\n"
        transcript = b"\xef\xbb\xbfClean:\ttab\r\nline\rtwo\r\n"
        self.stage("bytes.summary.md", b"\xef\xbb\xbfsummary\r\n")
        self.stage("bytes.transcript.md", transcript)
        self.stage("bytes.transcript.raw.md", raw)
        args = self.local_args(summary=str(self.inbox / "bytes.summary.md"),
                               transcript=str(self.inbox / "bytes.transcript.md"),
                               raw=str(self.inbox / "bytes.transcript.raw.md"))
        _plan, _applied, entry = self.plan_and_apply(args)
        self.assertEqual((self.root / entry["transcript_raw_path"]).read_bytes(), raw)
        self.assertTrue((self.root / entry["transcript_path"]).read_bytes()
                        .endswith(b"---\n\n" + transcript))
        self.assertTrue((self.root / entry["summary_path"]).read_bytes()
                        .endswith(b"\xef\xbb\xbfsummary\r\n"))
        self.assert_inspect_clean()

    def test_record_transport_limits_are_unchanged(self):
        limit_chars = self.writer.MAX_ARTIFACT_CHARS
        self.assertEqual(limit_chars, 524_288)
        self.assertEqual(self.writer.MAX_RECORD_BYTES, 1024 * 1024)
        record = valid_record(1)
        record["artifacts"]["summary_md"] = "x" * limit_chars
        self.writer.validate_record(record)
        record["artifacts"]["summary_md"] = "x" * (limit_chars + 1)
        with self.assertRaisesRegex(self.writer._Reject, "exceeds 524288 characters"):
            self.writer.validate_record(record)
        path = write_record(self.root / "over-chars.json", record)
        code, envelope, _ = run_cli(
            self.lock_root, ["--record", str(path), "--workspace", str(self.root),
                             "--dry-run"])
        self.assertEqual(code, 2, envelope)
        self.assert_rejected(envelope)
        self.assertIn("exceeds 524288 characters", envelope["error"]["message"])

        record["artifacts"]["summary_md"] = "x" * limit_chars
        record["artifacts"]["transcript_md"] = "y" * limit_chars
        record["artifacts"]["transcript_raw_md"] = "y" * limit_chars
        path = write_record(self.root / "over-bytes.json", record)
        self.assertGreater(path.stat().st_size, self.writer.MAX_RECORD_BYTES)
        code, envelope, _ = run_cli(
            self.lock_root, ["--record", str(path), "--workspace", str(self.root),
                             "--dry-run"])
        self.assertEqual(code, 2, envelope)
        self.assert_rejected(envelope)
        self.assertIn("size limit", envelope["error"]["message"])
        self.assertFalse(self.register_path.exists())

    def test_capabilities_report_local_and_record_limits_separately(self):
        code, envelope, stderr = run_cli(self.lock_root, ["--capabilities"])
        self.assertEqual(code, 0, stderr)
        limits = envelope["data"]["limits"]
        self.assertEqual(limits["local_artifact_bytes"], 4_194_304)
        self.assertEqual(limits["artifact_chars"], 524_288)
        self.assertEqual(limits["record_bytes"], 1_048_576)
        self.assertEqual(limits["register_bytes"], 16 * 1024 * 1024)

    def test_large_local_crash_before_register_then_recovery_is_byte_exact(self):
        data = self.asr_text(583_368).encode("utf-8")
        summary = self.stage("big.summary.md", "big summary\n")
        transcript = self.stage("big.transcript.md", data)
        raw = self.stage("big.transcript.raw.md", data)
        lease = self.acquire()
        driver = Path(__file__).resolve().parent / "_crash_local.py"
        completed = subprocess.run(
            [sys.executable, str(driver), str(WRITER_SCRIPT), str(self.root), lease,
             str(self.lock_root), "5", str(summary), str(transcript), str(raw),
             "2026-10-21"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
        self.assertEqual(completed.returncode, 9, completed.stderr)
        for source in (summary, transcript, raw):
            self.assertTrue(source.exists())
        self.assertTrue(list(self.journal_dir().glob("*.journal.json")))
        recovered = self.writer.recover_workspace(
            self.root, lease_id=lease, trusted_workspace_lock_root=self.lock_root)
        self.assertTrue(recovered["success"], recovered)
        for source in (summary, transcript, raw):
            self.assertFalse(source.exists())
        entry = register_document(self.root)["meetings"][0]
        self.assertEqual((self.root / entry["transcript_raw_path"]).read_bytes(), data)
        self.assert_owned(entry["transcript_path"], data)
        self.assert_inspect_clean()


class ProcessBoundaryTests(unittest.TestCase):
    def test_writer_module_has_no_network_or_install_imports(self):
        source = WRITER_SCRIPT.read_text(encoding="utf-8")
        for forbidden in ("import requests", "import urllib", "pip install",
                          "subprocess.run(\"pip", "socket."):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
