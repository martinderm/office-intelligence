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



class ProcessBoundaryTests(unittest.TestCase):
    def test_writer_module_has_no_network_or_install_imports(self):
        source = WRITER_SCRIPT.read_text(encoding="utf-8")
        for forbidden in ("import requests", "import urllib", "pip install",
                          "subprocess.run(\"pip", "socket."):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
