"""File-level OS-boundary security tests for the source-bound event writer (#14).

These tests exercise ``apply_event`` and its CLI only through public seams and
real filesystem/process boundaries (lease CLI, ``os.link``, symlinks, reparse
attributes).  Writer internals are never mocked; injected failures sit on real
OS boundaries.  Expected hashes are computed independently with ``hashlib``.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BUNDLE = Path(__file__).resolve().parents[3]
WRITER = BUNDLE / "skills" / "event-documentation" / "scripts" / "event_documentation.py"
LOCK_ROOT = Path(
    os.environ.get("EVENT_DOCUMENTATION_WORKSPACE_LOCK_ROOT") or (BUNDLE.parent / "workspace-lock")
)
LOCK_CLI = LOCK_ROOT / "scripts" / "invoke_workspace_lock.py"
DRAFT = "MAILBASED UNGEPRUEFTER ENTWURF"
SHA_A = "a" * 64
SHA_B = "b" * 64


def canonical(value):
    """Independent canonical JSON SHA-256 (mirrors the mail-desk contract)."""
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()


def load_writer():
    spec = importlib.util.spec_from_file_location("event_documentation_security", WRITER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WriterSecurityBase(unittest.TestCase):
    def setUp(self):
        self.api = load_writer()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.lease = "synthetic-event-writer-security"
        self.write_projects()
        self.write_topics()
        self.acquire(self.lease)

    # --- environment helpers -------------------------------------------------
    def write_json(self, rel, value):
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

    def write_projects(self, projects=None):
        self.write_json(
            "memory/references/projects/projects.json",
            {"projects": projects if projects is not None else [
                {"id": "demo", "title": "Demo", "status": "active", "mailbox_folder": "Projects/demo"}]},
        )

    def write_topics(self, topics=None):
        self.write_json(
            "memory/references/topics/topics.json",
            {"topics": topics if topics is not None else [self.topic_entry()]},
        )

    @staticmethod
    def topic_entry(topic="topic-a", subtopic="sub-a", event="event-a", **overrides):
        entry = {
            "id": topic, "title": "Topic A", "mailbox_folder": "Themen/Topic A",
            "subtopics": [{
                "id": subtopic, "title": "Sub A",
                "events": [{
                    "id": event, "title": "Event A", "starts_on": "2026-02-01",
                    "reference_md": f"memory/references/topics/{topic}/subtopics/{subtopic}/events/{event}/index.md",
                    "status": "active", "phase": "planned",
                }],
            }],
        }
        entry.update(overrides)
        return entry

    def acquire(self, lease, extra=(), workspace=None):
        run = subprocess.run(
            [sys.executable, "-B", str(LOCK_CLI), "acquire", str(workspace or self.root),
             "--harness", "synthetic-test", "--lease-id", lease, "--duration-minutes", "10", "--json", *extra],
            capture_output=True, text=True)
        self.assertEqual(0, run.returncode, run.stderr)

    def release(self, lease):
        subprocess.run([sys.executable, "-B", str(LOCK_CLI), "release", str(self.root),
                        "--lease-id", lease, "--json"], capture_output=True, text=True)

    # --- payload helpers -----------------------------------------------------
    def basis_source(self, sid="mail-1", mid="source@example.test", sha=SHA_A):
        return {"id": sid, "kind": "mail", "message_id": mid, "content_sha256": sha}

    def rich_source(self, sid=None, mid="source@example.test", sha=SHA_A, folder="INBOX", eid="1"):
        mid = mid.lower()
        return {
            "id": sid or ("mail-" + canonical(mid)), "kind": "mail", "message_id": mid,
            "content_sha256": sha,
            "locations": [{"account": "synthetic", "folder": folder, "envelope_id": eid, "message_id": mid}],
            "thread_parents": [], "calendar_objects": [],
        }

    @staticmethod
    def scope():
        return {"account": "synthetic", "folders": ["INBOX"], "terms": ["workshop"],
                "scan_limit_per_folder": 20, "inspect_limit": 5}

    @staticmethod
    def rich_completeness():
        return {"exhaustive": False, "scan_basis": "first_page_envelopes", "scope": WriterSecurityBase.scope(),
                "limitations": ["Only explicit folders, terms and bounded first-page envelopes are covered.",
                                "Caller-provided snapshots require live workspace/account revalidation."]}

    @staticmethod
    def basis_completeness():
        return {"exhaustive": False, "limitations": ["Only selected folders and first-page envelopes"]}

    def work_order(self, event, sources, attachments=None, completeness=None):
        attachments = [] if attachments is None else attachments
        order = {"schema_version": 1, "owner": "event-documentation", "status": "pending_content_review",
                 "event": event, "sources": sources, "attachments": attachments,
                 "completeness": completeness if completeness is not None else self.basis_completeness()}
        order["source_snapshot_sha256"] = canonical({"sources": sources, "attachments": attachments})
        return order

    @staticmethod
    def project_event(owner="demo", event="2026-01-01-workshop", title="Synthetic workshop", start="2026-01-01"):
        return {"owner_kind": "project", "owner_id": owner, "id": event, "title": title, "start": start}

    @staticmethod
    def topic_event(topic="topic-a", subtopic="sub-a", event="event-a", title="Event A", start="2026-02-01"):
        return {"owner_kind": "topic", "owner_id": topic, "subtopic_id": subtopic,
                "id": event, "title": title, "start": start}

    @staticmethod
    def content(ids, *, reply=True, todo=True, overview="A scheduled workshop.",
                agenda="Discuss options", decisions=None):
        return {
            "overview": [{"text": overview, "source_ids": list(ids)}],
            "planned_agenda": [{"text": agenda, "source_ids": list(ids)}],
            "observed_decisions": list(decisions or []),
            "reply_triage": ([{"text": "No reply required", "source_ids": list(ids), "needed": reply}] if reply else []),
            "todo_triage": ([{"text": "Clarification pending", "source_ids": list(ids), "needed": todo}] if todo else []),
        }

    def payload(self, order, content):
        return {"schema_version": 1, "work_order": order, "content": content}

    def project_payload(self, **kwargs):
        source = self.basis_source()
        order = self.work_order(self.project_event(**kwargs), [source])
        return self.payload(order, self.content([source["id"]]))

    def apply(self, payload, lease="__own__", **kwargs):
        return self.api.apply_event(
            payload, workspace_root=self.root, lease_id=self.lease if lease == "__own__" else lease,
            trusted_workspace_lock_root=str(LOCK_ROOT), **kwargs)

    def outputs(self, owner_kind="project", topic="topic-a", subtopic="sub-a", event=None, owner="demo"):
        if owner_kind == "project":
            event = event or "2026-01-01-workshop"
            ref = f"memory/references/projects/{owner}/events/{event}"
            ev = f"memory/evidence/projects/{owner}/events/{event}"
        else:
            event = event or "event-a"
            ref = f"memory/references/topics/{topic}/subtopics/{subtopic}/events/{event}"
            ev = f"memory/evidence/topics/{topic}/events/{event}"
        return [f"{ref}/index.md", f"{ev}/action-items.md", f"{ev}/source-log.json",
                f"{ev}/attachment-inventory.json", f"{ev}/compliance-report.json"]

    # --- shared assertions ---------------------------------------------------
    def assert_bounded_failure(self, result):
        self.assertIsInstance(result, dict)
        self.assertIs(result.get("ok"), False)
        self.assertEqual({"ok", "error"}, set(result))
        self.assertEqual({"code", "message"}, set(result["error"]))
        self.assertIsInstance(result["error"]["code"], str)
        self.assertNotIn(str(self.root), json.dumps(result))
        self.assertNotIn("Traceback", json.dumps(result))

    def assert_no_event_dirs(self):
        self.assertFalse((self.root / "memory/evidence").exists())
        self.assertFalse((self.root / "memory/references/projects/demo/events").exists())
        self.assertFalse((self.root / "memory/references/topics/topic-a/subtopics/sub-a/events").exists())

    def read_all(self, paths):
        return {rel: (self.root / rel).read_bytes() for rel in paths}


class HappyPathAndReceiptTests(WriterSecurityBase):
    def test_project_write_is_source_bound_receipted_and_idempotent(self):
        payload = self.project_payload()
        snapshot = copy.deepcopy(payload)
        first = self.apply(payload)
        self.assertTrue(first["ok"], first)
        rels = self.outputs()
        self.assertEqual(rels, first["artifacts"])
        self.assertEqual("human_content_review_required", first["content_review"])
        self.assertIs(False, first["content_approved"])
        self.assertNotIn("approval_receipt", first)
        self.assertEqual(snapshot, payload, "apply_event must not mutate its input")
        before = self.read_all(rels)
        index = (self.root / rels[0]).read_text(encoding="utf-8")
        self.assertIn("Discuss options", index)
        self.assertIn("mail-1", index)
        self.assertIn(DRAFT, index)
        self.assertIn(DRAFT, (self.root / rels[1]).read_text(encoding="utf-8"))
        for rel in rels:
            text = (self.root / rel).read_text(encoding="utf-8")
            self.assertNotIn(str(self.root), text)
            self.assertNotIn(self.lease, text)
            self.assertIsNone(re.search(r"\d{4}-\d{2}-\d{2}T\d{2}", text))
        receipt = first["output_receipt"]
        self.assertEqual(canonical(payload["work_order"]), receipt["work_order_sha256"])
        self.assertEqual(payload["work_order"]["source_snapshot_sha256"], receipt["source_snapshot_sha256"])
        self.assertEqual({rel for rel in rels}, {o["path"] for o in receipt["outputs"]})
        for output in receipt["outputs"]:
            self.assertEqual(hashlib.sha256((self.root / output["path"]).read_bytes()).hexdigest(), output["sha256"])
        second = self.apply(self.project_payload())
        self.assertTrue(second["ok"], second)
        self.assertEqual(before, self.read_all(rels))
        self.assertEqual(receipt, second["output_receipt"])

    def test_topic_write_bootstraps_missing_canonical_index_and_keeps_draft_marking(self):
        source = self.basis_source()
        order = self.work_order(self.topic_event(), [source])
        result = self.apply(self.payload(order, self.content([source["id"]])))
        self.assertTrue(result["ok"], result)
        rels = self.outputs(owner_kind="topic")
        self.assertEqual(rels, result["artifacts"])
        index = (self.root / rels[0]).read_text(encoding="utf-8")
        self.assertIn(DRAFT, index)
        self.assertIn("status: draft", index)
        # Evidence path deliberately carries no subtopic segment.
        self.assertTrue((self.root / "memory/evidence/topics/topic-a/events/event-a").is_dir())
        self.assertFalse((self.root / "memory/evidence/topics/topic-a/subtopics").exists())

    def test_human_extra_notes_are_never_deleted(self):
        ev = self.root / "memory/evidence/projects/demo/events/2026-01-01-workshop"
        ev.mkdir(parents=True)
        note = ev / "human-notes.md"
        note.write_text("keep me", encoding="utf-8")
        result = self.apply(self.project_payload())
        self.assertTrue(result["ok"], result)
        self.assertEqual("keep me", note.read_text(encoding="utf-8"))

    def test_rich_profile_is_accepted_and_receipted(self):
        source = self.rich_source()
        order = self.work_order(self.project_event(), [source], completeness=self.rich_completeness())
        result = self.apply(self.payload(order, self.content([source["id"]])))
        self.assertTrue(result["ok"], result)
        self.assertEqual(canonical(order), result["output_receipt"]["work_order_sha256"])


class ContentBoundaryTests(WriterSecurityBase):
    def test_unknown_empty_duplicate_and_injected_anchors_fail(self):
        cases = {
            "unknown": lambda c: c["planned_agenda"][0].update({"source_ids": ["missing"]}),
            "empty": lambda c: c["planned_agenda"][0].update({"source_ids": []}),
            "duplicate": lambda c: c["planned_agenda"][0].update({"source_ids": ["mail-1", "mail-1"]}),
            "unknown-key": lambda c: c.update({"extra": []}),
            "control": lambda c: c["overview"][0].update({"text": "line\nbreak"}),
            "blank": lambda c: c["overview"][0].update({"text": "  "}),
        }
        for name, mutate in cases.items():
            with self.subTest(case=name):
                payload = self.project_payload()
                mutate(payload["content"])
                self.assert_bounded_failure(self.apply(payload))
                self.assert_no_event_dirs()

    def test_reply_and_todo_need_independent_strict_booleans(self):
        for value in ("yes", 1, None, [], {}):
            with self.subTest(value=value):
                payload = self.project_payload()
                payload["content"]["todo_triage"][0]["needed"] = value
                self.assert_bounded_failure(self.apply(payload))
        payload = self.project_payload()
        payload["content"]["reply_triage"][0]["needed"] = True
        payload["content"]["todo_triage"][0]["needed"] = False
        self.assertTrue(self.apply(payload)["ok"])

    def test_planned_agenda_is_not_promoted_to_observed_decisions(self):
        payload = self.project_payload()
        payload["content"]["planned_agenda"] = [{"text": "Approve budget", "source_ids": ["mail-1"]}]
        payload["content"]["observed_decisions"] = []
        result = self.apply(payload)
        self.assertTrue(result["ok"], result)
        index = (self.root / self.outputs()[0]).read_text(encoding="utf-8")
        self.assertIn("Approve budget", index)
        decisions = index.split("## Beobachtete Entscheidungen", 1)[1].split("##", 1)[0]
        self.assertNotIn("Approve budget", decisions)


class OwnerAndCatalogTests(WriterSecurityBase):
    def test_uncatalogued_owner_and_malformed_catalog_fail_before_writes(self):
        self.assert_bounded_failure(self.apply(self.project_payload(owner="missing")))
        self.assert_no_event_dirs()
        self.write_projects([{"id": "demo", "title": "Demo"}])
        (self.root / "memory/references/projects/projects.json").write_text("{not json", encoding="utf-8")
        self.assert_bounded_failure(self.apply(self.project_payload()))
        (self.root / "memory/references/projects/projects.json").write_text(
            '{"projects":[{"id":"demo","id":"demo"}]}', encoding="utf-8")
        self.assert_bounded_failure(self.apply(self.project_payload()))
        self.assert_no_event_dirs()

    def test_topic_owner_requires_subtopic_and_registered_event_match(self):
        source = self.basis_source()
        mismatches = {
            "no-subtopic": lambda e: e.pop("subtopic_id"),
            "unknown-subtopic": lambda e: e.update({"subtopic_id": "nope"}),
            "unknown-topic": lambda e: e.update({"owner_id": "nope"}),
            "unknown-event": lambda e: e.update({"id": "nope"}),
            "title-drift": lambda e: e.update({"title": "Other"}),
            "start-drift": lambda e: e.update({"start": "2026-03-01"}),
        }
        for name, mutate in mismatches.items():
            with self.subTest(case=name):
                event = self.topic_event()
                mutate(event)
                order = self.work_order(event, [source])
                self.assert_bounded_failure(self.apply(self.payload(order, self.content(["mail-1"]))))
                self.assert_no_event_dirs()

    def test_noncanonical_reference_and_invalid_status_phase_fail(self):
        source = self.basis_source()
        for mutate in (
            lambda t: t["subtopics"][0]["events"][0].update({"reference_md": "memory/elsewhere/index.md"}),
            lambda t: t["subtopics"][0]["events"][0].update({"status": "retired"}),
            lambda t: t["subtopics"][0]["events"][0].update({"phase": "maybe"}),
            lambda t: t["subtopics"][0]["events"][0].update({"starts_on": "2026-13-40"}),
        ):
            with self.subTest(mutate=mutate):
                self.write_topics([self.topic_entry()])
                entry = self.topic_entry()
                mutate(entry)
                self.write_topics([entry])
                order = self.work_order(self.topic_event(), [source])
                self.assert_bounded_failure(self.apply(self.payload(order, self.content(["mail-1"]))))

    def test_sibling_subtopics_with_same_event_id_collide_before_writes(self):
        first = self.topic_entry(subtopic="sub-a", event="shared")
        second = self.topic_entry(subtopic="sub-b", event="shared")
        self.write_topics([{**first, "subtopics": first["subtopics"] + second["subtopics"]}])
        source = self.basis_source()
        order = self.work_order(self.topic_event(subtopic="sub-a", event="shared"), [source])
        self.assert_bounded_failure(self.apply(self.payload(order, self.content(["mail-1"]))))
        self.assert_no_event_dirs()

    def test_duplicate_event_id_within_one_subtopic_fails(self):
        entry = self.topic_entry()
        entry["subtopics"][0]["events"].append(dict(entry["subtopics"][0]["events"][0]))
        self.write_topics([entry])
        source = self.basis_source()
        order = self.work_order(self.topic_event(), [source])
        self.assert_bounded_failure(self.apply(self.payload(order, self.content(["mail-1"]))))

    def test_path_escape_owner_ids_are_rejected(self):
        for owner in ("../escape", "a/b", "C:/x", "con", "trail."):
            with self.subTest(owner=owner):
                payload = self.project_payload(owner=owner)
                self.assert_bounded_failure(self.apply(payload))
        for event in ("../escape", "a/b", "a\\b", "nul"):
            with self.subTest(event=event):
                order = self.work_order(self.project_event(event=event), [self.basis_source()])
                self.assert_bounded_failure(self.apply(self.payload(order, self.content(["mail-1"]))))


class ProfileAndDriftTests(WriterSecurityBase):
    def test_mixed_or_partial_profiles_fail(self):
        rich = self.rich_source()
        basis = self.basis_source(sid="mail-2")
        order = self.work_order(self.project_event(), [rich, basis], completeness=self.basis_completeness())
        self.assert_bounded_failure(self.apply(self.payload(order, self.content([rich["id"], "mail-2"]))))
        partial = {"id": "mail-3", "kind": "mail", "message_id": "x@example.test", "content_sha256": SHA_A,
                   "locations": []}
        order = self.work_order(self.project_event(), [partial])
        self.assert_bounded_failure(self.apply(self.payload(order, self.content(["mail-3"]))))

    def test_basis_profile_rejects_attachments_and_rich_completeness(self):
        order = self.work_order(self.project_event(), [self.basis_source()],
                                attachments=[{"id": "attachment-" + SHA_A, "sha256": SHA_A, "size_bytes": 1,
                                              "provenance": []}])
        self.assert_bounded_failure(self.apply(self.payload(order, self.content(["mail-1"]))))
        order = self.work_order(self.project_event(), [self.basis_source()], completeness=self.rich_completeness())
        self.assert_bounded_failure(self.apply(self.payload(order, self.content(["mail-1"]))))

    def test_source_snapshot_drift_and_noncanonical_identity_fail(self):
        order = self.work_order(self.project_event(), [self.basis_source()])
        order["sources"][0]["content_sha256"] = SHA_B
        self.assert_bounded_failure(self.apply(self.payload(order, self.content(["mail-1"]))))
        rich = self.rich_source()
        rich["id"] = "mail-wrong"
        order = self.work_order(self.project_event(), [rich], completeness=self.rich_completeness())
        self.assert_bounded_failure(self.apply(self.payload(order, self.content([rich["id"]]))))

    def test_schema_and_top_level_shape_are_strict(self):
        payload = self.project_payload()
        payload["schema_version"] = True
        self.assert_bounded_failure(self.apply(payload))
        payload = self.project_payload()
        payload["cloud_sync"] = True
        self.assert_bounded_failure(self.apply(payload))
        payload = self.project_payload()
        payload["work_order"]["owner"] = "other"
        self.assert_bounded_failure(self.apply(payload))
        payload = self.project_payload()
        payload["content"]["observed_decisions"] = [{"text": "x", "source_ids": ["mail-1"], "needed": False}]
        self.assert_bounded_failure(self.apply(payload))


class ExistingStateTests(WriterSecurityBase):
    def test_conflicting_existing_target_preserves_bytes_and_creates_nothing(self):
        rels = self.outputs()
        index = self.root / rels[0]
        index.parent.mkdir(parents=True)
        index.write_text("Existing human notes", encoding="utf-8")
        self.assert_bounded_failure(self.apply(self.project_payload()))
        self.assertEqual("Existing human notes", index.read_text(encoding="utf-8"))
        self.assertFalse((self.root / "memory/evidence").exists())

    def test_partial_state_is_recovery_required_and_not_autofilled(self):
        rels = self.outputs()
        (self.root / rels[0]).parent.mkdir(parents=True)
        (self.root / rels[0]).write_text("partial", encoding="utf-8")
        self.assert_bounded_failure(self.apply(self.project_payload()))
        self.assertFalse((self.root / rels[1]).exists())

    def test_empty_event_directory_is_treated_as_crash_leftover(self):
        (self.root / "memory/evidence/projects/demo/events/2026-01-01-workshop").mkdir(parents=True)
        self.assert_bounded_failure(self.apply(self.project_payload()))

    def test_leftover_staging_blocks_apply(self):
        ev = self.root / "memory/evidence/projects/demo/events/2026-01-01-workshop"
        ev.mkdir(parents=True)
        (ev / ".source-log.json.123.eventwriter-stage").write_text("x", encoding="utf-8")
        self.assert_bounded_failure(self.apply(self.project_payload()))

    def test_hardlinked_existing_target_is_rejected(self):
        rels = self.outputs()
        index = self.root / rels[0]
        index.parent.mkdir(parents=True)
        outside = self.root / "outside.md"
        outside.write_text("outside", encoding="utf-8")
        os.link(outside, index)
        self.assert_bounded_failure(self.apply(self.project_payload()))
        self.assertEqual("outside", outside.read_text(encoding="utf-8"))


class LockTests(WriterSecurityBase):
    def test_missing_foreign_expired_and_unverifiable_locks_fail(self):
        self.assert_bounded_failure(self.apply(self.project_payload(), lease=None))
        self.assert_bounded_failure(self.apply(self.project_payload(), lease="foreign-lease"))
        # Expired: stale heartbeat, matching lease id.
        lock = self.root / ".agents/session.lock"
        lock.write_text(json.dumps({"harness": "x", "pid": 1, "user": "u",
                                    "started": "2000-01-01T00:00:00+00:00", "heartbeat": "2000-01-01T00:00:00+00:00",
                                    "leaseId": self.lease}), encoding="utf-8")
        self.assert_bounded_failure(self.apply(self.project_payload()))
        # Unverifiable: malformed lock JSON.
        lock.write_text("{not json", encoding="utf-8")
        self.assert_bounded_failure(self.apply(self.project_payload()))
        self.assert_no_event_dirs()

    def test_lease_loss_midway_leaves_partial_state_and_next_apply_stops(self):
        real_link = os.link
        calls = {"n": 0}

        def flaky(src, dst, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 3:
                self.release(self.lease)
                raise OSError("injected lease loss")
            return real_link(src, dst, *args, **kwargs)

        with mock.patch.object(os, "link", flaky):
            result = self.apply(self.project_payload())
        self.assert_bounded_failure(result)
        rels = self.outputs()
        published = [rel for rel in rels if (self.root / rel).is_file()]
        self.assertTrue(0 < len(published) < len(rels), published)
        self.acquire(self.lease)
        self.assert_bounded_failure(self.apply(self.project_payload()))

    def test_publication_collision_after_preflight_rolls_back_without_clobber(self):
        real_link = os.link

        def collide(src, dst, *args, **kwargs):
            Path(dst).write_bytes(b"external-writer")
            return real_link(src, dst, *args, **kwargs)

        with mock.patch.object(os, "link", collide):
            result = self.apply(self.project_payload())
        self.assert_bounded_failure(result)
        rels = self.outputs()
        self.assertEqual(b"external-writer", (self.root / rels[0]).read_bytes())
        for rel in rels[1:]:
            self.assertFalse((self.root / rel).exists())

    def test_midway_oserror_rolls_back_only_own_files(self):
        real_link = os.link
        calls = {"n": 0}

        def flaky(src, dst, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 4:
                raise OSError("injected publish failure")
            return real_link(src, dst, *args, **kwargs)

        with mock.patch.object(os, "link", flaky):
            result = self.apply(self.project_payload())
        self.assert_bounded_failure(result)
        for rel in self.outputs():
            self.assertFalse((self.root / rel).exists())
        self.assertFalse((self.root / "memory/evidence/projects/demo/events/2026-01-01-workshop").exists())


class FilesystemThreatTests(WriterSecurityBase):
    def _make_reparse(self, link, target):
        (self.root / "memory").mkdir(parents=True, exist_ok=True)
        try:
            link.symlink_to(target, target_is_directory=True)
            return True
        except OSError:
            pass
        if os.name == "nt":
            made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                                  capture_output=True)
            return made.returncode == 0
        return False

    def test_reparse_parent_is_rejected_before_event_mutation(self):
        with tempfile.TemporaryDirectory() as outside:
            link = self.root / "memory/evidence"
            if not self._make_reparse(link, Path(outside)):
                self.skipTest("Platform does not permit synthetic reparse directories")
            self.assert_bounded_failure(self.apply(self.project_payload()))
            self.assertEqual([], list(Path(outside).iterdir()))
            self.assertFalse((self.root / "memory/references/projects/demo/events").exists())

    def test_reparse_attribute_on_existing_parent_is_rejected(self):
        rels = self.outputs()
        parent = (self.root / rels[0]).parent
        parent.mkdir(parents=True)
        parent = parent.resolve()
        injected = []
        original = Path.lstat

        def lstat(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if str(path) == str(parent):
                injected.append(path)
                return mock.Mock(st_mode=info.st_mode, st_file_attributes=0x400, st_nlink=1)
            return info

        with mock.patch.object(Path, "lstat", lstat):
            self.assert_bounded_failure(self.apply(self.project_payload()))
        self.assertTrue(injected)


class CliTests(WriterSecurityBase):
    def run_cli(self, request_text=None, args=None):
        if args is None:
            request = self.root / "request.json"
            request.write_text(request_text, encoding="utf-8")
            args = ["--request", str(request), "--workspace", str(self.root),
                    "--lease-id", self.lease, "--workspace-lock-root", str(LOCK_ROOT), "--json"]
        return subprocess.run([sys.executable, "-B", str(WRITER), *args], capture_output=True, text=True)

    def test_cli_success_envelope_has_six_fields(self):
        payload = self.project_payload()
        run = self.run_cli(json.dumps(payload))
        self.assertEqual(0, run.returncode, run.stderr)
        envelope = json.loads(run.stdout)
        self.assertEqual({"action", "success", "state", "message", "data", "error"}, set(envelope))
        self.assertTrue(envelope["success"])
        self.assertIsNone(envelope["error"])

    def test_cli_invalid_inputs_return_bounded_errors(self):
        for text in ('{"schema_version":1,"schema_version":1}', '{"schema_version":NaN}', "{}", "not json"):
            with self.subTest(text=text):
                run = self.run_cli(text)
                self.assertNotEqual(0, run.returncode)
                self.assertLess(len(run.stdout), 2000)
                self.assertNotIn("Traceback", run.stdout + run.stderr)
                self.assertNotIn(str(self.root), run.stdout)
                envelope = json.loads(run.stdout)
                self.assertEqual({"action", "success", "state", "message", "data", "error"}, set(envelope))
                self.assertFalse(envelope["success"])

    def test_cli_parser_errors_are_bounded(self):
        run = self.run_cli(args=["--" + "x" * 5000, "--json"])
        self.assertNotEqual(0, run.returncode)
        self.assertLess(len(run.stdout), 2000)
        self.assertFalse(json.loads(run.stdout)["success"])


class OwnerSelectionTests(WriterSecurityBase):
    def test_duplicate_selected_ids_and_invalid_owner_records_are_rejected(self):
        self.write_projects([{"id": "demo", "title": "Demo", "mailbox_folder": "Projects/demo"},
                             {"id": "demo", "title": "Other", "mailbox_folder": "Projects/other"}])
        self.assert_bounded_failure(self.apply(self.project_payload()))
        for entry in ({"id": "demo", "title": "Demo"}, {"id": "demo", "mailbox_folder": "Projects/demo"},
                      {"id": "demo", "title": "", "mailbox_folder": "Projects/demo"}):
            with self.subTest(entry=entry):
                self.write_projects([entry])
                self.assert_bounded_failure(self.apply(self.project_payload()))
        self.write_projects([{"id": "demo", "title": "Demo", "mailbox_folder": "Projects/demo"}, "junk"])
        self.assert_bounded_failure(self.apply(self.project_payload()))
        self.assert_no_event_dirs()
        self.write_topics([self.topic_entry(), self.topic_entry()])
        source = self.basis_source()
        order = self.work_order(self.topic_event(), [source])
        self.assert_bounded_failure(self.apply(self.payload(order, self.content(["mail-1"]))))
        entry = self.topic_entry()
        entry["subtopics"].append(dict(entry["subtopics"][0]))
        self.write_topics([entry])
        order = self.work_order(self.topic_event(), [source])
        self.assert_bounded_failure(self.apply(self.payload(order, self.content(["mail-1"]))))

    def test_event_must_reside_in_the_explicitly_selected_subtopic(self):
        entry = {"id": "topic-a", "title": "Topic A", "mailbox_folder": "Themen/Topic A", "subtopics": [
            {"id": "sub-a", "title": "Sub A", "events": []},
            {"id": "sub-b", "title": "Sub B", "events": [
                {"id": "event-a", "title": "Event A", "starts_on": "2026-02-01",
                 "reference_md": "memory/references/topics/topic-a/subtopics/sub-b/events/event-a/index.md",
                 "status": "active"}]}]}
        self.write_topics([entry])
        source = self.basis_source()
        order = self.work_order(self.topic_event(subtopic="sub-a", event="event-a"), [source])
        self.assert_bounded_failure(self.apply(self.payload(order, self.content(["mail-1"]))))
        self.assert_no_event_dirs()

    def test_basis_duplicate_message_identity_is_rejected(self):
        first = self.basis_source(sid="mail-1", mid="same@example.test")
        second = self.basis_source(sid="mail-2", mid="same@example.test")
        order = self.work_order(self.project_event(), [first, second])
        self.assert_bounded_failure(self.apply(self.payload(order, self.content(["mail-1", "mail-2"]))))

    def test_rich_source_and_provenance_bind_to_scope_and_message(self):
        source = self.rich_source()
        source["locations"] = [{"account": "synthetic", "folder": "ELSEWHERE", "envelope_id": "1",
                                "message_id": "source@example.test"}]
        order = self.work_order(self.project_event(), [source], completeness=self.rich_completeness())
        self.assert_bounded_failure(self.apply(self.payload(order, self.content([source["id"]]))))
        source = self.rich_source()
        attachments = [{"id": "attachment-" + SHA_A, "sha256": SHA_A, "size_bytes": 1, "provenance": [
            {"account": "synthetic", "folder": "ELSEWHERE", "envelope_id": "1", "message_id": "source@example.test",
             "part_locator": "2", "filename": "p.pdf", "original_filename": "p.pdf", "mime_type": "application/pdf"}]}]
        order = self.work_order(self.project_event(), [source], attachments=attachments,
                                completeness=self.rich_completeness())
        self.assert_bounded_failure(self.apply(self.payload(order, self.content([source["id"]]))))
        self.assert_no_event_dirs()

    def test_forged_or_mixed_rich_completeness_is_rejected(self):
        source = self.rich_source()
        forged = self.rich_completeness()
        forged["limitations"] = ["arbitrary caller text"]
        order = self.work_order(self.project_event(), [source], completeness=forged)
        self.assert_bounded_failure(self.apply(self.payload(order, self.content([source["id"]]))))
        order = self.work_order(self.project_event(), [source], completeness=self.basis_completeness())
        self.assert_bounded_failure(self.apply(self.payload(order, self.content([source["id"]]))))


class ContentInjectionTests(WriterSecurityBase):
    def test_index_and_evidence_are_inert_and_marked_unreviewed(self):
        payload = self.project_payload()
        payload["content"]["overview"] = [{"text": "![x](https://evil.example/x.png) <script>alert(1)</script>",
                                           "source_ids": ["mail-1"]}]
        payload["content"]["planned_agenda"] = [{"text": "Plan <b>bold</b>", "source_ids": ["mail-1"]}]
        payload["content"]["observed_decisions"] = [{"text": "Decided <script>bad</script>", "source_ids": ["mail-1"]}]
        payload["work_order"]["completeness"]["limitations"] = ["<script>lim</script>"]
        result = self.apply(payload)
        self.assertTrue(result["ok"], result)
        rels = self.outputs()
        index = (self.root / rels[0]).read_text(encoding="utf-8")
        actions = (self.root / rels[1]).read_text(encoding="utf-8")
        self.assertNotIn("<script", index)
        self.assertNotIn("](http", index)
        self.assertNotIn("![", index)
        self.assertIn("&lt;script&gt;", index)
        self.assertIn("evidence_level: unreviewed-draft", index)
        self.assertNotIn("normative", index)
        self.assertIn(DRAFT, index)
        self.assertIn("REPORTED", actions)
        self.assertIn("&lt;script&gt;bad&lt;/script&gt;", actions)
        decisions = index.split("## Beobachtete Entscheidungen", 1)[1].split("##", 1)[0]
        self.assertNotIn("Plan ", decisions)
        self.assertIn("action-items.md", decisions)

    def test_unicode_line_separators_are_inert(self):
        payload = self.project_payload()
        payload["content"]["overview"] = [{"text": "line\u2028sep\u2029end", "source_ids": ["mail-1"]}]
        result = self.apply(payload)
        self.assertTrue(result["ok"], result)
        index = (self.root / self.outputs()[0]).read_text(encoding="utf-8")
        self.assertNotIn("\u2028", index)
        self.assertNotIn("\u2029", index)


class ErrorTruthfulnessTests(WriterSecurityBase):
    def test_validation_failure_truthfully_reports_no_write(self):
        payload = self.project_payload()
        payload["content"]["overview"][0]["source_ids"] = ["missing"]
        result = self.apply(payload)
        self.assert_bounded_failure(result)
        self.assertEqual("event_write_rejected", result["error"]["code"])
        self.assertIn("no event was written", result["error"]["message"])

    def test_lease_loss_reports_recovery_without_zero_write_claim(self):
        real_link = os.link
        calls = {"n": 0}

        def flaky(src, dst, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 3:
                self.release(self.lease)
                raise OSError("injected lease loss")
            return real_link(src, dst, *args, **kwargs)

        with mock.patch.object(os, "link", flaky):
            result = self.apply(self.project_payload())
        self.assert_bounded_failure(result)
        self.assertEqual("event_write_recovery_required", result["error"]["code"])
        self.assertNotIn("no event was written", result["error"]["message"])


class CatalogThreatTests(WriterSecurityBase):
    def test_catalog_hardlink_is_rejected(self):
        outside = self.root / "outside-catalog.json"
        outside.write_text(json.dumps({"projects": [{"id": "demo", "title": "Demo",
                                                     "mailbox_folder": "Projects/demo"}]}), encoding="utf-8")
        catalog = self.root / "memory/references/projects/projects.json"
        catalog.unlink()
        os.link(outside, catalog)
        self.assert_bounded_failure(self.apply(self.project_payload()))
        self.assert_no_event_dirs()

    def test_catalog_junction_parent_is_rejected(self):
        with tempfile.TemporaryDirectory() as outside:
            (Path(outside) / "projects.json").write_text(
                json.dumps({"projects": [{"id": "demo", "title": "Demo", "mailbox_folder": "Projects/demo"}]}),
                encoding="utf-8")
            projects = self.root / "memory/references/projects"
            import shutil
            shutil.rmtree(projects)
            if os.name == "nt":
                made = subprocess.run(["cmd", "/c", "mklink", "/J", str(projects), outside],
                                      capture_output=True).returncode == 0
            else:
                try:
                    projects.symlink_to(outside, target_is_directory=True)
                    made = True
                except OSError:
                    made = False
            if not made:
                self.skipTest("Platform does not permit synthetic reparse directories")
            self.assert_bounded_failure(self.apply(self.project_payload()))
            self.assertEqual({"projects.json"}, {entry.name for entry in Path(outside).iterdir()})
            self.assert_no_event_dirs()

    def test_catalog_reparse_attribute_is_rejected(self):
        catalog = (self.root / "memory/references/projects/projects.json").resolve()
        injected = []
        original = Path.lstat

        def lstat(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if str(path) == str(catalog):
                injected.append(path)
                return mock.Mock(st_mode=info.st_mode, st_file_attributes=0x400, st_nlink=1,
                                 st_dev=info.st_dev, st_ino=info.st_ino)
            return info

        with mock.patch.object(Path, "lstat", lstat):
            self.assert_bounded_failure(self.apply(self.project_payload()))
        self.assertTrue(injected)

    def test_root_alias_does_not_hide_child_reparse(self):
        real = self.root / "real"
        (real / "memory/references/projects").mkdir(parents=True)
        (real / "memory/references/projects/projects.json").write_text(
            json.dumps({"projects": [{"id": "demo", "title": "Demo", "mailbox_folder": "Projects/demo"}]}),
            encoding="utf-8")
        (real / "memory/evidence").mkdir(parents=True)
        with tempfile.TemporaryDirectory() as outside:
            ev = real / "memory/evidence"
            ev.rmdir()
            if os.name == "nt":
                made = subprocess.run(["cmd", "/c", "mklink", "/J", str(ev), outside],
                                      capture_output=True).returncode == 0
            else:
                try:
                    ev.symlink_to(outside, target_is_directory=True)
                    made = True
                except OSError:
                    made = False
            if not made:
                self.skipTest("Platform does not permit synthetic reparse directories")
            alias = self.root / "alias"
            if os.name == "nt":
                subprocess.run(["cmd", "/c", "mklink", "/J", str(alias), str(real)],
                               capture_output=True)
            else:
                alias.symlink_to(real, target_is_directory=True)
            lease = "synthetic-alias-root"
            self.acquire(lease, workspace=real)
            result = self.api.apply_event(self.project_payload(), workspace_root=alias, lease_id=lease,
                                          trusted_workspace_lock_root=str(LOCK_ROOT))
            self.assert_bounded_failure(result)
            self.assertEqual([], list(Path(outside).iterdir()))


class PublicationRaceTests(WriterSecurityBase):
    @staticmethod
    def _replace_with_reparse(path, target):
        path.rmdir()
        if os.name == "nt":
            return subprocess.run(["cmd", "/c", "mklink", "/J", str(path), str(target)],
                                  capture_output=True).returncode == 0
        try:
            path.symlink_to(target, target_is_directory=True)
            return True
        except OSError:
            return False

    def test_reparse_parent_introduced_after_preflight_is_detected(self):
        ev = self.root / "memory/evidence/projects/demo/events/2026-01-01-workshop"
        with tempfile.TemporaryDirectory() as outside:
            real_link = os.link
            calls = {"n": 0}
            state = {"made": False}

            def hijack(src, dst, *args, **kwargs):
                calls["n"] += 1
                result = real_link(src, dst, *args, **kwargs)
                if calls["n"] == 1:
                    state["made"] = self._replace_with_reparse(ev, Path(outside))
                return result

            with mock.patch.object(os, "link", hijack):
                result = self.apply(self.project_payload())
            if not state["made"]:
                self.skipTest("Platform does not permit synthetic reparse directories")
            self.assert_bounded_failure(result)
            self.assertEqual([], list(Path(outside).iterdir()))
            self.assertFalse((self.root / self.outputs()[0]).exists())

    def test_substituted_stage_is_not_deleted(self):
        real_link = os.link
        calls = {"n": 0}
        with tempfile.TemporaryDirectory() as outside:
            outside_file = Path(outside) / "keep.md"
            outside_file.write_text("keep", encoding="utf-8")
            substituted = {"path": None}

            def hijack(src, dst, *args, **kwargs):
                calls["n"] += 1
                if calls["n"] == 2:
                    Path(src).unlink()
                    real_link(outside_file, src)
                    substituted["path"] = Path(src)
                return real_link(src, dst, *args, **kwargs)

            with mock.patch.object(os, "link", hijack):
                result = self.apply(self.project_payload())
            self.assert_bounded_failure(result)
            self.assertEqual("keep", outside_file.read_text(encoding="utf-8"))
            self.assertIsNotNone(substituted["path"])
            self.assertTrue(substituted["path"].exists())

    def test_swapped_published_target_is_preserved(self):
        real_link = os.link
        calls = {"n": 0}

        def hijack(src, dst, *args, **kwargs):
            calls["n"] += 1
            result = real_link(src, dst, *args, **kwargs)
            if calls["n"] == 4:
                (self.root / self.outputs()[0]).write_text("swapped", encoding="utf-8")
            return result

        with mock.patch.object(os, "link", hijack):
            result = self.apply(self.project_payload())
        self.assert_bounded_failure(result)
        self.assertEqual("swapped", (self.root / self.outputs()[0]).read_text(encoding="utf-8"))

    def test_lease_revoked_during_rollback_stops_further_mutations(self):
        real_link = os.link
        real_unlink = os.unlink
        links = {"n": 0}
        revoked = {"done": False}

        def hijack_link(src, dst, *args, **kwargs):
            links["n"] += 1
            if links["n"] == 4:
                raise OSError("injected publish failure")
            return real_link(src, dst, *args, **kwargs)

        def hijack_unlink(path, *args, **kwargs):
            if not revoked["done"] and not str(path).endswith(".eventwriter-stage"):
                revoked["done"] = True
                self.release(self.lease)
            return real_unlink(path, *args, **kwargs)

        with mock.patch.object(os, "link", hijack_link), mock.patch.object(os, "unlink", hijack_unlink):
            result = self.apply(self.project_payload())
        self.assert_bounded_failure(result)
        self.assertTrue(revoked["done"])
        rels = self.outputs()
        remaining = [rel for rel in rels if (self.root / rel).is_file()]
        self.assertTrue(0 < len(remaining) < len(rels), remaining)

    def test_drift_at_last_link_fails_without_receipt(self):
        real_link = os.link
        calls = {"n": 0}

        def hijack(src, dst, *args, **kwargs):
            calls["n"] += 1
            result = real_link(src, dst, *args, **kwargs)
            if calls["n"] == 5:
                (self.root / self.outputs()[0]).write_text("drift", encoding="utf-8")
            return result

        with mock.patch.object(os, "link", hijack):
            result = self.apply(self.project_payload())
        self.assert_bounded_failure(result)
        self.assertNotIn("output_receipt", result)
        self.assertEqual("drift", (self.root / self.outputs()[0]).read_text(encoding="utf-8"))


class FinalOwnershipRegressionTests(WriterSecurityBase):
    def test_triage_booleans_show_need_not_completed_tasks(self):
        payload = self.project_payload()
        payload["content"]["reply_triage"][0]["needed"] = True
        payload["content"]["todo_triage"][0]["needed"] = False
        self.assertTrue(self.apply(payload)["ok"])
        text = (self.root / self.outputs()[1]).read_text(encoding="utf-8")
        self.assertIn("Bedarf: ja", text.split("## Reply-Triage", 1)[1].split("## Todo-Triage", 1)[0])
        self.assertIn("Bedarf: nein", text.split("## Todo-Triage", 1)[1])
        self.assertNotIn("[x]", text, "Need is not proof of task completion")

    def test_lease_loss_after_successful_link_does_not_unlink_stage(self):
        real_link = os.link

        def link_then_revoke(src, dst, *args, **kwargs):
            result = real_link(src, dst, *args, **kwargs)
            self.release(self.lease)
            return result

        with mock.patch.object(os, "link", link_then_revoke):
            result = self.apply(self.project_payload())
        self.assert_bounded_failure(result)
        self.assertEqual("event_write_recovery_required", result["error"]["code"])
        self.assertTrue(list(self.root.rglob("*.eventwriter-stage")),
                        "Publishing does not authorize cleanup after ownership was lost")

    def test_rollback_io_failure_is_truthfully_recovery_required(self):
        real_link, real_lstat = os.link, Path.lstat
        state = {"calls": 0, "rollback": False, "denied": False}

        def fail_fourth(src, dst, *args, **kwargs):
            state["calls"] += 1
            if state["calls"] == 4:
                state["rollback"] = True
                raise OSError("publish failure")
            return real_link(src, dst, *args, **kwargs)

        def deny_cleanup(path, *args, **kwargs):
            if state["rollback"] and path.name == "source-log.json":
                state["denied"] = True
                raise PermissionError("cleanup file inaccessible")
            return real_lstat(path, *args, **kwargs)

        with mock.patch.object(os, "link", fail_fourth), mock.patch.object(Path, "lstat", deny_cleanup):
            result = self.apply(self.project_payload())
        self.assertTrue(state["denied"], "The rollback I/O rejection must actually occur")
        self.assert_bounded_failure(result)
        self.assertEqual("event_write_recovery_required", result["error"]["code"])
        self.assertNotIn("no event was written", result["error"]["message"])

    def test_source_identifiers_are_inert_text_not_unsafe_code_spans(self):
        source = self.basis_source(sid="mail-`payload`")
        order = self.work_order(self.project_event(), [source])
        result = self.apply(self.payload(order, self.content([source["id"]])))
        self.assertTrue(result["ok"], result)
        index = (self.root / self.outputs()[0]).read_text(encoding="utf-8")
        self.assertIn("- mail-\\`payload\\` (mail)", index)

    def test_project_status_is_not_reinterpreted_as_topic_routing_state(self):
        # Characterization: project schema has no topic-style event registration
        # or active-only event-routing contract. Explicit archival drafts remain
        # catalog-bound; this does not enable automatic mail routing.
        self.write_projects([{"id": "demo", "title": "Demo", "mailbox_folder": "Projects/demo",
                              "status": "archived"}])
        self.assertTrue(self.apply(self.project_payload())["ok"])

    def test_lease_loss_during_fsync_never_unlinks_stage(self):
        def lose_lease(_descriptor):
            self.release(self.lease)
            raise OSError("fsync failed after lease loss")

        with mock.patch.object(os, "fsync", lose_lease):
            result = self.apply(self.project_payload())
        self.assert_bounded_failure(result)
        self.assertEqual("event_write_recovery_required", result["error"]["code"])
        self.assertTrue(list(self.root.rglob("*.eventwriter-stage")),
                        "A lease-less invocation must not clean even its own stage")

    def test_rollback_does_not_follow_replaced_evidence_parent(self):
        real_link, real_unlink = os.link, os.unlink
        calls = []
        moved = self.root / "moved-evidence"
        evidence = self.root / "memory/evidence"
        redirected = []

        def fail_fourth(src, dst, *args, **kwargs):
            calls.append(dst)
            if len(calls) == 4:
                raise OSError("publish failure")
            return real_link(src, dst, *args, **kwargs)

        def redirect_after_first_unlink(path, *args, **kwargs):
            result = real_unlink(path, *args, **kwargs)
            if not redirected and Path(path).name == "source-log.json":
                evidence.rename(moved)
                if os.name == "nt":
                    run = subprocess.run(["cmd", "/c", "mklink", "/J", str(evidence), str(moved)],
                                         capture_output=True)
                    self.assertEqual(0, run.returncode, run.stderr)
                else:
                    evidence.symlink_to(moved, target_is_directory=True)
                redirected.append(True)
            return result

        with mock.patch.object(os, "link", fail_fourth), mock.patch.object(os, "unlink", redirect_after_first_unlink):
            result = self.apply(self.project_payload())
        self.assertTrue(redirected, "The rollback-parent substitution must actually occur")
        self.assert_bounded_failure(result)
        self.assertEqual("event_write_recovery_required", result["error"]["code"])
        self.assertTrue((moved / "projects/demo/events/2026-01-01-workshop/action-items.md").is_file(),
                        "Rollback must not unlink through the replacement parent")

    def test_writer_refuses_runtime_mutation_of_its_own_bundle(self):
        # A byte-identical installed bundle in a temporary workspace, not a mock
        # of the writer's root check and never the actual repository.
        installed = self.root / "skills/event-documentation/scripts/event_documentation.py"
        installed.parent.mkdir(parents=True)
        installed.write_bytes(WRITER.read_bytes())
        spec = importlib.util.spec_from_file_location("event_documentation_bundle_test", installed)
        api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(api)
        result = api.apply_event(self.project_payload(), workspace_root=self.root, lease_id=self.lease,
                                 trusted_workspace_lock_root=str(LOCK_ROOT))
        self.assert_bounded_failure(result)
        self.assert_no_event_dirs()


if __name__ == "__main__":
    unittest.main()
